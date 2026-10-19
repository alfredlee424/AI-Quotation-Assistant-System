"""任意張數的純業務核心（無 DB、UI、config、agent 或 LLM 相依）。

API
===
analyze(source, catalog, choices=None, customs=None, overrides=None,
        resolutions=None) -> JSON-compatible dict
source 是原文字串，或 {"text": 原文, "document_id": 呼叫端穩定文件識別}。
catalog 是 build_catalog / MasterSource.catalog 的 detached snapshot。
所有人工輸入皆為 {child_id: {identity: value}}，不可用張次當 key：
* choices 的 identity 是 requirement.id 或 "node:" + 完整 path；value
  唯一代碼使用 {path, code}，同部位代碼有不同名稱時必須使用 {path, code, codsc}，
  不接受價格等其他欄位，且必須在該需求合法候選中。node 選擇可明示啟用選配。
* customs 的 identity 同上；value 是 {path, code: "TMP:...", codsc,
  compri, actor, reason, time}，只替換該既有部位的當次規格，不新增結構邊。
* overrides 的 identity 是完整 path；value 是 {values: {compri/stdqty/
  stdpar/line_qty: 數值}, actor, reason, time}。line_qty 依 quantity_basis，
  per_product 為每件量、total 為該張總量；不從共用總量推算平均。
* resolutions 的 identity 是 gap.resolution_id 或 requirement.source.fragment_id；value 是
  {text: 精確重述, actor, reason, time}。重述仍要通過同一解析器，不能忽略。
time 必須是含時區 ISO-8601；人工記錄由呼叫端提供，分析本身不讀時鐘。

dynamic-2 每 child 新增 role、shape、normalized_requirements。
每筆 normalized 需求含 kind/part/attribute/targets/value/quantity/basis/origin，
以及 priority/effective/override/conflict/status/candidates/custom_paths/selected/requirement_ids。
targets 是同工單 group_ordinal，不是件數；origin 包含原行、原片段、span、column、
fragment_id 與 resolution 稽核。requirement_ids 連回既有人工候選需求。
status 區分 parsed/matched、pending_master、pending_units、pending_external、
unrecognized、overridden/not_applicable/duplicate/conflict；不把缺主檔誤稱未知原文。
pending_external 保留「靠單／照圖」參考，不提供假坐標或自動核准。
舊 dynamic-1 文件交由獨立 dynamic_v1 引擎原樣重建，沒有原地移轉。

最小明確語法
============
工單：A（切換群組；共用條件只在此群組）
亦接受 Trello 原始標頭，如「(1)20260921-3客戶／樓層」；只作識別，不推定日期。
主檔配置-----2
配件：每桌2個線盒（歸最近一項；數量未知則保持空白）
主檔配置-----3
共用：面無孔
第2項：規格[CMT1\\A001]=C010（群組內張次）
共用[除1,2]：無木腳（明示例外；局部同屬性優先於共用預設）
規格[完整路徑]=代碼、每桌2個線盒、線盒共6個均支援。
共用空標頭開啟 block，空行不終止；明示 scope／新品項／工單終止。
未標示範圍且不在 block 的獨立行會阻擋；所有原文行含空行保存在 source_ledger。
台尺不自動換算；公分/mm 可精確正規化，無單位採主檔標籤比對，
台尺、混合單位或 3X3.25 等小數值未定單位需人工重述；數字近似、不明外型均不猜測。

validate(child, context) 重建並比對單張所有衍生欄位；freeze(analysis,
child_id, actor=..., reason=..., time=...) 產出 detached 單張文件。
validate_document(document, current=None, trusted_catalog_digest=None) 重建
結構、需求、成本與金額；current 傳最新 analyze 結果可拒絕過期凍結。
revision 是內容版本雜湊（非遞增計數）。原文任一變更使所有 revision
失效；只改一張人工輸入則只影響該張。核心無狀態，不自行保存凍結。
每次編輯後呼叫 reconcile_frozen(最新 analysis, {child_id: document})，
以回傳 retained 取代原集合（revoked 為撤銷原因）；即使之後改回舊文字，
已移除的凍結也不會自動恢復。禁止跳過編輯事件或重新掛回撤銷文件。

信任邊界：自包含快照可驗證一致性，不是簽章或主檔真實性證明。保存端
必須保管可信 context / catalog digest 及最新輸入版本；不可讓不可信
客戶端同時換掉輸入、快照與 digest。無可信快照時由呼叫端重讀主檔後
重新 analyze / freeze；本模組絕不擅自查 DB。身份權限由呼叫端檢查。
金額與數量輸出為十進位字串，缺值為 None；未完整只有 known_cost
已知成本小計，不輸出 total_cost/subtotal/total_price。
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
from datetime import datetime, timezone
from decimal import Context, ROUND_HALF_UP, localcontext

from . import dynamic_v1 as _v1
from . import semantic_normalization as normalization
from .dynamic_v1 import (_INPUTS, _Child, _audit, _digest, _plain, _source)
from .catalog import option_identity

VERSION = "dynamic-2"


def _catalog(snapshot):
    return _v1._catalog(snapshot, allow_code_variants=True)


class _TypedChild(_Child):
    """Normalized 需求直接限制 request/必選展開/成本，不是展示用旁路。"""

    def __init__(self, product, catalog, inputs):
        super().__init__(product, catalog, inputs)
        self.blocked_paths = set()
        self.leg_holes_forbidden = False

    def option_identity(self, option):
        return option_identity(self.cat, option)

    def put(self, option, key, qty, basis, origin, audit=None, quantity_source="BOM_per_product_policy"):
        previous = self.child["selections"].get(option["path"])
        if previous and previous["code"] == option["code"] and previous["codsc"] != option["codsc"]:
            self.issue(key + ":conflict", "同一部位同代碼但規格名稱不同，不能合併需求：" + option["path"], "conflict")
            return
        return super().put(option, key, qty, basis, origin, audit, quantity_source)

    def forbidden(self, path):
        if path not in self.cat["nodes"]:
            return False
        area, label, ancestors = normalization.path_traits(self.cat, path)
        # 面無孔不禁止腳孔；腳兩片禁走線孔也不等於整桌無孔。
        return ((self.no_holes and area == "tabletop" and "孔" in ancestors)
                or (self.no_legs and area == "legs")
                or (self.leg_holes_forbidden and area == "legs" and "孔" in ancestors
                    and any(word in ancestors for word in ("走線", "走縣"))))

    def request(self, key, text, options, **kwargs):
        if key.startswith("node:") and key[5:] in self.blocked_paths:
            self.issue(key, "此部位有未滿足的型別化需求；不可由必選預設或 node 選擇取代", "requirement_pending")
            return
        if key.startswith("node:") and key[5:] in self.cat["nodes"]:
            path = key[5:]
            # 線盒的真正必選子孔沿結構繼承盒數，不再產生另一筆圓孔需求。
            # 不跨兄弟節點推測，也不為缺少的結構製造 path。
            node = self.cat["nodes"][path]
            _, label, _ = normalization.path_traits(self.cat, path)
            if str(node.get("must_chose") or "").upper() == "Y" and "孔" in label:
                parent = node["pathf"]
                while parent in self.cat["nodes"]:
                    selection = self.child["selections"].get(parent)
                    parent_label = self.cat["labels"][self.cat["nodes"][parent]["optnoc"]]
                    if selection and "線盒" in parent_label:
                        kwargs.update(qty=selection["line_qty"], basis=selection["quantity_basis"],
                                      quantity_source="parent_accessory:" + parent)
                        break
                    parent = self.cat["nodes"][parent]["pathf"]
        return super().request(key, text, options, **kwargs)

    def normalized(self):
        rows = []
        for key, text, evidence in self.fragments():
            parsed = normalization.normalize(key, text, evidence)
            # resolution 不可把已辨識需求改成「主檔配置」等空操作。
            if evidence.get("resolution"):
                original = normalization.normalize(key, evidence["original"], evidence)
                needed = Counter((r["part"], r["kind"], r["attribute"]) for r in original
                                 if r["kind"] not in {"unknown", "configuration", "role"})
                supplied = Counter((r["part"], r["kind"], r["attribute"]) for r in parsed)
                if needed - supplied:
                    self.issue(key, "重述不能移除已解析的部位／需求型別", "invalid_resolution", source=evidence)
                    parsed = original
            rows.extend(parsed)
        self.child["normalized_requirements"] = normalization.resolve(rows)
        active = [r for r in rows if r["effective"]]
        self.no_holes = any(r["kind"] == "constraint" and r["part"] == "tabletop_hole" for r in active)
        self.no_legs = any(r["kind"] == "constraint" and r["part"] == "legs" for r in active)
        self.leg_holes_forbidden = any(r["kind"] == "constraint" and r["part"] == "leg_hole" for r in active)
        for r in rows:
            r.update(candidates=[], custom_paths=[], selected=None, requirement_ids=[])
            if not r["effective"]:
                continue
            key, source = r["id"], r["origin"]
            if r["conflict"]:
                r["status"] = "conflict"
                self.issue(key, "同優先級的部位需求衝突，請明示局部例外", "conflict", source=source)
                self.blocked_paths.update(normalization.paths_for(self.cat, r))
                continue
            kind = r["kind"]
            if kind in {"configuration", "role"}:
                r["status"] = "parsed"
                continue
            if kind == "constraint":
                self.child["requirements"].append(dict(id=key, text=source["text"], kind="constraint", source=source, candidates=[]))
                r["requirement_ids"] = [key]
                continue
            if kind == "external_confirmation" or (kind == "dimension" and r["value"]["confirmation_required"]):
                gap_kind = "external_confirmation" if kind == "external_confirmation" else "units"
                # 未定單位不允許必選尺寸自動啟用並驅動其他部位成本。
                self.blocked_paths.update(normalization.paths_for(self.cat, r))
                r["status"] = "pending_external" if kind == "external_confirmation" else "pending_units"
                self.child["requirements"].append(dict(id=key, text=source["text"], kind=gap_kind, source=source,
                                                       candidates=[], resolution_id=source["fragment_id"]))
                self.issue(source["fragment_id"] if gap_kind == "units" else key,
                           "已解析，待圖面／訂單核對，不推定坐標" if kind == "external_confirmation" else
                           "已解析尺寸，單位待確認；不推定換算或成本", gap_kind,
                           source=source, resolution_id=source["fragment_id"], normalized_id=key)
                r["requirement_ids"] = [key]
                continue
            before = len(self.child["requirements"])
            if kind in {"legacy", "unknown"}:
                # 舊明示語法及完整主檔名稱保留契約；未知仍走阻擋需求，不能靠 must_chose 消失。
                unknowns = [t for t in rows if t["kind"] == "unknown"
                            and t["origin"]["fragment_id"] == source["fragment_id"]]
                self.semantics(source["fragment_id"] if kind == "unknown" and len(unknowns) == 1 else key,
                               r["value"], source)
                r["status"] = "unrecognized" if kind == "unknown" else "parsed"
            else:
                paths = normalization.paths_for(self.cat, r)
                options = [o for p in paths for o in self.options(p) if normalization.matches(r, o)]
                r["custom_paths"] = paths
                self.request(key, source["text"], options, qty=r["quantity"] or "1", auto=True,
                             source=source, custom_paths=paths, kind="normalized",
                             quantity_source="source_requirement" if r["quantity"] is not None else "BOM_per_product_policy")
            emitted = self.child["requirements"][before:]
            r["requirement_ids"] = [e["id"] for e in emitted]
            r["candidates"] = [c for e in emitted for c in e.get("candidates", [])]
            selected = [e.get("selected") for e in emitted if e.get("selected")]
            r["selected"] = selected[0] if len(selected) == 1 else None
            if kind not in {"legacy", "unknown"}:
                r["status"] = "matched" if selected else "pending_master"
                if not selected:
                    self.blocked_paths.update(r["custom_paths"])


def analyze(source, catalog, choices=None, customs=None, overrides=None, resolutions=None):
    """純函式。非法 envelope 拒絕；業務缺口仍回傳逐張草稿及完整階層。"""
    source, cat = _source(source), _catalog(catalog)
    inputs = dict(zip(_INPUTS, (choices, customs, overrides, resolutions)))
    inputs = {name: _plain({} if value is None else value) for name, value in inputs.items()}
    products, ledger, global_issues = normalization.parse(source, cat)
    ids = {p["id"] for p in products}
    for name, mapping in inputs.items():
        if not isinstance(mapping, dict) or any(k not in ids or not isinstance(v, dict) for k, v in mapping.items()):
            raise ValueError(name + " 必須以當次 child id 綁定 identity mapping，禁止跨張／過期輸入")
    context = dict(version=VERSION, source=source, catalog_snapshot=cat, inputs=inputs)
    source_digest, catalog_digest = _digest(source), _digest(cat)
    children = []
    with localcontext(Context(prec=80, rounding=ROUND_HALF_UP)):
        for product in products:
            scoped = {name: mapping.get(product["id"], {}) for name, mapping in inputs.items()}
            ctx = _TypedChild(product, cat, scoped)
            ctx.child["issues"].extend(dict(i, child_id=product["id"]) for i in global_issues
                                       if i.get("scope_group") in (None, product["group"]))
            ctx.normalized()
            ctx.complete()
            ctx.calculate()
            digest = _digest(dict(version=VERSION, source_digest=source_digest, catalog_digest=catalog_digest,
                                  child_id=product["id"], inputs=scoped))
            ctx.child.update(revision=digest, input_digest=digest, source_digest=source_digest,
                             catalog_digest=catalog_digest, semantic_engine="local_rules_no_llm",
                             source_audit=dict(document_id=source["document_id"], lines=deepcopy(ledger), inputs=scoped))
            children.append(ctx.child)
    return dict(version=VERSION, children=children, gaps=global_issues + [g for c in children for g in c["issues"]],
                global_issues=global_issues, source_ledger=ledger, source_digest=source_digest,
                catalog_digest=catalog_digest, input_digest=_digest(context), context=context)


def _rebuild(context):
    if isinstance(context, dict) and context.get("version") == _v1.VERSION:
        return _v1._rebuild(context)
    if not isinstance(context, dict) or set(context) != {"version", "source", "catalog_snapshot", "inputs"}:
        raise ValueError("缺少可重建的完整 context")
    if context["version"] != VERSION or set(context["inputs"]) != set(_INPUTS):
        raise ValueError("不支援的核心版本或輸入欄位")
    return analyze(context["source"], context["catalog_snapshot"], **context["inputs"])


def validate(child, context):
    """保存前重建單張；ready、summary、hash 全都不是驗證依據。失敗拋 ValueError。"""
    if isinstance(context, dict) and context.get("version") == _v1.VERSION:
        return _v1.validate(child, context)
    if not isinstance(child, dict):
        raise ValueError("缺少單張內容")
    rebuilt = _rebuild(context)
    matches = [c for c in rebuilt["children"] if c["id"] == child.get("id")]
    if len(matches) != 1 or _plain(child) != matches[0]:
        raise ValueError("單張內容、結構、來源稽核或重算金額與輸入不一致")
    if not matches[0]["ready"]:
        raise ValueError("單張仍有待確認／成本缺口，不可凍結或保存")
    return deepcopy(matches[0])


def freeze(analysis, child_id, *, actor, reason, time=None):
    """回傳可 JSON 保存的 detached 單張文件；不寫檔、不配號、不連庫。"""
    if analysis.get("version") == _v1.VERSION:
        return _v1.freeze(analysis, child_id, actor=actor, reason=reason, time=time)
    if analysis.get("version") != VERSION or analysis.get("context", {}).get("version") != VERSION:
        raise ValueError("分析與重建引擎版本不一致")
    audit = _audit(dict(actor=actor, reason=reason, time=time or datetime.now(timezone.utc).isoformat()))
    matches = [c for c in analysis["children"] if c["id"] == child_id]
    if len(matches) != 1:
        raise ValueError("不存在唯一的 child id")
    child = validate(matches[0], analysis["context"])
    context = deepcopy(analysis["context"])
    # 只攜帶本張人工輸入；其他张編輯不改本張版本，也不洩露其他張人工成本。
    context["inputs"] = {name: {child_id: mapping[child_id]} if child_id in mapping else {}
                         for name, mapping in context["inputs"].items()}
    validate(child, context)
    document = dict(document_type="local_dynamic_child", version=VERSION, child=child,
                    context=context, frozen_audit=audit, frozen_digest=_digest(child))
    return deepcopy(document)


def validate_document(document, *, current=None, trusted_catalog_digest=None):
    """結構快照重建核驗；current 應由保存端依最新可信輸入重建，不信任客戶端。"""
    if isinstance(document, dict) and document.get("version") == _v1.VERSION:
        return _v1.validate_document(document, current=current, trusted_catalog_digest=trusted_catalog_digest)
    if (not isinstance(document, dict) or set(document) != {"document_type", "version", "child", "context",
                                                           "frozen_audit", "frozen_digest"}
            or document["document_type"] != "local_dynamic_child" or document["version"] != VERSION):
        raise ValueError("不是支援的單張凍結文件")
    if not isinstance(document["context"], dict) or document["context"].get("version") != VERSION:
        raise ValueError("文件與重建引擎版本不一致")
    _audit(document["frozen_audit"])
    child = validate(document["child"], document["context"])
    if document["frozen_digest"] != _digest(child):
        raise ValueError("凍結內容已變更")
    if trusted_catalog_digest is not None and child["catalog_digest"] != trusted_catalog_digest:
        raise ValueError("快照不符可信主檔版本；需由呼叫端重新讀取主檔並分析")
    if current is not None:
        rebuilt = _rebuild(current["context"])
        latest = next((c for c in rebuilt["children"] if c["id"] == child["id"]), None)
        if latest is None or latest["revision"] != child["revision"] or latest != child:
            raise ValueError("本張輸入或整份來源已變更，凍結已撤銷")
    return child


def reconcile_frozen(analysis, documents):
    """編輯事件的純狀態轉移；保存端必須以 retained 替換舊凍結集合。"""
    if not isinstance(documents, dict):
        raise ValueError("documents 必須是 child id -> frozen document")
    retained, revoked = {}, {}
    current = _rebuild(analysis["context"])
    for child_id, document in documents.items():
        try:
            rebuilt = validate_document(document, current=current)
            if rebuilt["id"] != child_id:
                raise ValueError("凍結集合的 child id 不一致")
            retained[child_id] = deepcopy(document)
        except ValueError as exc:
            revoked[child_id] = str(exc)
    return dict(retained=retained, revoked=revoked)
