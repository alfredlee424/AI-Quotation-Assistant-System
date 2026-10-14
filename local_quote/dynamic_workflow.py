"""UI 無關的新版工作階段；所有公開狀態均為 detached 值。

set_input(source, category=None) -> 是否建立新草稿
load_catalog(detached_catalog 或有 catalog(category) 的唯讀 master) -> analysis
edit(child_id, choices=..., customs=..., overrides=..., resolutions=...) -> analysis
analyze() -> analysis；freeze(child_id, actor=..., reason=..., time=None) -> FrozenDynamicQuote
confirm(child_id, store, confirmed=True) -> 持久化 receipt

edit 各 keyword 為該張完整 identity mapping，未提供者不變，{} 清空該類。
分析不查 DB。load_catalog 是唯一可呼叫 master 的邊界；UI 必須保管此
workspace 而非讓不可信客戶端任意替換 current/catalog_digest。人工 actor
不是登入憑證；真正的登入、授權、操作確認由 UI／應用層負責。
"""
from __future__ import annotations

from copy import deepcopy
import json
from uuid import uuid4

from . import dynamic
from .dynamic_storage import prepare_document, validate_actor


class DynamicWorkspace:
    def __init__(self):
        self._source = {"text": "", "document_id": uuid4().hex}
        self._external_source_id = False
        self._category = None
        self._catalog = None
        self._analysis = None
        self._inputs = {k: {} for k in ("choices", "customs", "overrides", "resolutions")}
        self._frozen = {}
        self._saved = {}
        self._history = {}
        self._revoked = {}
        self.notice = "新的草稿；尚未保存"

    @property
    def source(self):
        return deepcopy(self._source)

    @property
    def category(self):
        return self._category

    @property
    def catalog(self):
        return deepcopy(self._catalog)

    @property
    def analysis(self):
        return deepcopy(self._analysis)

    @property
    def frozen(self):
        return dict(self._frozen)

    @property
    def saved(self):
        return deepcopy(self._saved)

    @property
    def history(self):
        """本工作階段所有已保存 receipt（含之前來源）；DB 歷史仍由 store 讀取。"""
        return deepcopy(self._history)

    @property
    def revoked(self):
        return dict(self._revoked)

    def _new_draft(self):
        self._revoked = {cid: "來源或主檔已變更，請重新固定" for cid in self._frozen}
        self._inputs = {k: {} for k in self._inputs}
        self._analysis = None
        self._frozen = {}
        self._saved = {}
        self.notice = "來源或主檔已變更：這是新的草稿；全部未保存固定內容已撤銷，既有歷史未刪除"

    def set_input(self, source, category=None):
        """文字來源變更新 UUID；明示 document_id 保留原識別。相同輸入為 no-op。"""
        normalized = dynamic._source(source)
        category = self._category if category is None else category
        if category is not None and (not isinstance(category, str) or not category.strip()):
            raise ValueError("category 必須為非空產品類別代碼")
        same_source = (normalized["text"] == self._source["text"] and
                       (isinstance(source, str) or normalized["document_id"] == self._source["document_id"]))
        if same_source and category == self._category:
            return False
        category_changed = category != self._category
        self._new_draft()
        self._external_source_id = isinstance(source, dict)
        self._source = (normalized if self._external_source_id else
                        dict(text=normalized["text"], document_id=uuid4().hex))
        self._category = category
        if category_changed:
            self._catalog = None
        return True

    def load_catalog(self, catalog):
        """明確載入 detached 主檔；可接受 master.catalog(category)，保存不會重查。"""
        if not isinstance(catalog, dict):
            if not self._category:
                raise ValueError("請先指定產品類別")
            catalog = catalog.catalog(self._category)
        normalized = dynamic._catalog(catalog)
        if self._category is not None and normalized["prodkind"] != self._category:
            raise ValueError("主檔類別與當前輸入不符")
        if self._catalog is not None and self._catalog != normalized:
            self._new_draft()
            if not self._external_source_id:
                self._source["document_id"] = uuid4().hex
        self._category = normalized["prodkind"]
        self._catalog = normalized
        return self.analyze()

    def _build(self, inputs=None):
        if self._catalog is None:
            raise ValueError("請先載入 detached 主檔")
        return dynamic.analyze(self._source, self._catalog, **(self._inputs if inputs is None else inputs))

    def _reconcile(self, analysis):
        documents = {cid: json.loads(f.payload) for cid, f in self._frozen.items() if cid not in self._saved}
        result = dynamic.reconcile_frozen(analysis, documents)
        self._revoked = result["revoked"]
        self._frozen = {cid: value for cid, value in self._frozen.items()
                        if cid in self._saved or cid in result["retained"]}
        self._analysis = analysis

    def analyze(self):
        self._reconcile(self._build())
        return self.analysis

    def _child(self, child_id):
        if self._analysis is None:
            raise ValueError("請先分析草稿")
        matches = [c for c in self._analysis["children"] if c["id"] == child_id]
        if len(matches) != 1:
            raise ValueError("不存在當前草稿的 child_id")
        return matches[0]

    def can_edit(self, child_id):
        self._child(child_id)
        return child_id not in self._saved

    def edit(self, child_id, **changes):
        self._child(child_id)
        if not self.can_edit(child_id):
            raise ValueError("此張已保存，禁止修改；請建立新的來源草稿，不改既有歷史")
        if not changes or set(changes) - set(self._inputs):
            raise ValueError("只允許 choices/customs/overrides/resolutions")
        inputs = deepcopy(self._inputs)
        for name, mapping in changes.items():
            if not isinstance(mapping, dict):
                raise ValueError("人工輸入必須是該張的 identity mapping")
            if mapping:
                inputs[name][child_id] = deepcopy(mapping)
            else:
                inputs[name].pop(child_id, None)
        analysis = self._build(inputs)  # 非法 envelope 不得破壞原 state
        self._inputs = inputs
        self._reconcile(analysis)
        return self.analysis

    def freeze(self, child_id, *, actor, reason, time=None):
        self._child(child_id)
        if child_id in self._saved:
            raise ValueError("此張已保存，禁止再次固定")
        self.analyze()
        validate_actor(actor)
        # 重複點擊不能產生新 UUID；要更改稽核須先修改草稿使舊固定撤銷。
        if child_id in self._frozen:
            audit = json.loads(self._frozen[child_id].payload)["frozen_audit"]
            if actor != audit["actor"] or reason != audit["reason"] or time is not None and time != audit["time"]:
                raise ValueError("本張已有固定人工稽核，不可靜默換人／換原因；請建立新草稿")
            return self._frozen[child_id]
        document = dynamic.freeze(self._analysis, child_id, actor=actor, reason=reason, time=time)
        frozen = prepare_document(document)
        self._frozen[child_id] = frozen
        return frozen

    def confirm(self, child_id, store, *, confirmed):
        self._child(child_id)
        if confirmed is not True:
            raise ValueError("必須人工明確確認")
        if child_id in self._saved:
            return deepcopy(self._saved[child_id])
        self.analyze()
        if child_id not in self._frozen:
            raise ValueError("本張尚未固定或固定已失效")
        frozen = self._frozen[child_id]
        # 失敗時不改 frozen / UUID；提交結果未知亦使用同一識別重試。
        receipt = store.save(frozen, confirmed=True, current=self._analysis,
                             trusted_catalog_digest=self._analysis["catalog_digest"])
        if receipt["confirmation_id"] != frozen.confirmation_id or receipt["child_id"] != child_id:
            raise ValueError("存檔 receipt 身份不符；保留原 UUID 供查核")
        self._saved[child_id] = deepcopy(receipt)
        self._history[receipt["confirmation_id"]] = deepcopy(receipt)
        self.notice = "已保存單張報價：" + receipt["ref_no"]
        return deepcopy(receipt)
