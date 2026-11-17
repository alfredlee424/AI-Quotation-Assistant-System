"""不拆單工作階段：沿用逐品項分析／人工編輯，固定與保存改為整份一次。

freeze_combined(actor=..., reason=..., time=None) -> FrozenCombinedQuote
confirm_combined(store, confirmed=True) -> receipt（一個單號）
任何品項人工編輯或來源變更都撤銷未保存的合併固定；保存後整份鎖定。
"""
from __future__ import annotations

from copy import deepcopy
import json

from . import combined
from .combined_storage import prepare_document
from .dynamic_storage import validate_actor
from .dynamic_workflow import DynamicWorkspace


class CombinedWorkspace(DynamicWorkspace):
    def __init__(self):
        self._combined_frozen = None
        self._combined_saved = None
        self.combined_revoked = None
        super().__init__()

    @property
    def combined_frozen(self):
        return self._combined_frozen

    @property
    def combined_saved(self):
        return deepcopy(self._combined_saved)

    @property
    def saved(self):
        # 保存後所有品項都視為已保存，讓共用 UI 回呼拒絕再編輯。
        if self._combined_saved is None:
            return {}
        return {cid: deepcopy(self._combined_saved) for cid in self._combined_saved["child_ids"]}

    @property
    def frozen(self):
        return {}

    def blocks(self):
        return combined.blocks(self._analysis) if self._analysis else []

    def summary(self):
        return combined.summary(self._analysis) if self._analysis else None

    def _new_draft(self):
        super()._new_draft()
        if self._combined_frozen is not None:
            self.combined_revoked = "來源或主檔已變更，請重新固定"
        self._combined_frozen = None
        self._combined_saved = None

    def _reconcile(self, analysis):
        self._analysis = analysis
        if self._combined_frozen is None or self._combined_saved is not None:
            return
        try:
            combined.validate_document(json.loads(self._combined_frozen.payload), current=analysis)
        except ValueError as exc:
            self._combined_frozen = None
            self.combined_revoked = str(exc)

    def can_edit(self, child_id):
        self._child(child_id)
        return self._combined_saved is None

    def freeze(self, child_id, **_):
        raise ValueError("合併報價不逐張固定；請使用 freeze_combined")

    def confirm(self, child_id, store, **_):
        raise ValueError("合併報價不逐張保存；請使用 confirm_combined")

    def freeze_combined(self, *, actor, reason, time=None):
        if self._analysis is None:
            raise ValueError("請先分析草稿")
        if self._combined_saved is not None:
            raise ValueError("本工單已保存，禁止再次固定")
        self.analyze()
        validate_actor(actor)
        if self._combined_frozen is not None:
            audit = json.loads(self._combined_frozen.payload)["frozen_audit"]
            if actor != audit["actor"] or reason != audit["reason"] or time is not None and time != audit["time"]:
                raise ValueError("本工單已有固定人工稽核，不可靜默換人／換原因；請建立新草稿")
            return self._combined_frozen
        document = combined.freeze(self._analysis, actor=actor, reason=reason, time=time)
        self._combined_frozen = prepare_document(document)
        self.combined_revoked = None
        return self._combined_frozen

    def confirm_combined(self, store, *, confirmed):
        if confirmed is not True:
            raise ValueError("必須人工明確確認")
        if self._combined_saved is not None:
            return deepcopy(self._combined_saved)
        self.analyze()
        if self._combined_frozen is None:
            raise ValueError("合併報價尚未固定或固定已失效")
        frozen = self._combined_frozen
        receipt = store.save(frozen, confirmed=True, current=self._analysis,
                             trusted_catalog_digest=self._analysis["catalog_digest"])
        if (receipt["confirmation_id"] != frozen.confirmation_id
                or receipt["child_ids"] != [c["id"] for c in self._analysis["children"]]):
            raise ValueError("存檔 receipt 身份不符；保留原 UUID 供查核")
        self._combined_saved = deepcopy(receipt)
        self._history[receipt["confirmation_id"]] = deepcopy(receipt)
        self.notice = "已保存合併報價：" + receipt["ref_no"]
        return deepcopy(receipt)
