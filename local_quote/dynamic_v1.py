"""任意張數的純業務核心（無 DB、UI、config、agent 或 LLM 相依）。

API
===
analyze(source, catalog, choices=None, customs=None, overrides=None,
        resolutions=None) -> JSON-compatible dict
source 是原文字串，或 {"text": 原文, "document_id": 呼叫端穩定文件識別}。
catalog 是 build_catalog / MasterSource.catalog 的 detached snapshot。
所有人工輸入皆為 {child_id: {identity: value}}，不可用張次當 key：
* choices 的 identity 是 requirement.id 或 "node:" + 完整 path；value
  僅含 {path, code}，必須在該需求合法候選中。node 選擇可明示啟用選配。
* customs 的 identity 同上；value 是 {path, code: "TMP:...", codsc,
  compri, actor, reason, time}，只替換該既有部位的當次規格，不新增結構邊。
* overrides 的 identity 是完整 path；value 是 {values: {compri/stdqty/
  stdpar/line_qty: 數值}, actor, reason, time}。line_qty 依 quantity_basis，
  per_product 為每件量、total 為該張總量；不從共用總量推算平均。
* resolutions 的 identity 是 gap.resolution_id 或 requirement.source.fragment_id；value 是
  {text: 精確重述, actor, reason, time}。重述仍要通過同一解析器，不能忽略。
time 必須是含時區 ISO-8601；人工記錄由呼叫端提供，分析本身不讀時鐘。

最小明確語法
============
工單：A（切換群組；共用條件只在此群組）
亦接受 Trello 原始標頭，如「(1)20260921-3客戶／樓層」；只作識別，不推定日期。
主檔配置-----2
配件：每桌2個線盒（歸最近一項；數量未知則保持空白）
主檔配置-----3
共用：面無孔
第2項：規格[CMT1\\A001]=C010（群組內張次）
共用[除1,2]：無木腳（明示例外；不默認局部覆蓋共用）
規格[完整路徑]=代碼、每桌2個線盒、線盒共6個均支援。
未標示範圍的獨立行會阻擋；所有原文行含空行保存在 source_ledger。
台尺不自動換算；公分/mm 可精確正規化，無單位採主檔標籤比對，
台尺或混合單位需人工重述；數字近似、不明外型均不猜測。

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
from datetime import datetime, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import re
import unicodedata

from .catalog import build_catalog, entries, material_groups


VERSION = "dynamic-1"
PER_ITEM_OPTIONS = frozenset({"S010", "S015", "S020", "S022", "S025", "S030",
                             "S035", "S040", "S049", "S050", "S055", "S061",
                             "W010", "W020", "W030", "W040", "W050", "W060", "W070"})
_INPUTS = ("choices", "customs", "overrides", "resolutions")
_ITEM = re.compile(r"^(.+?)\s*[-─－—]{2,}\s*([1-9]\d*)(?![\d.])\s*(.*)$")
_ORDER_HEADER = re.compile(r"^(?:[（(]\s*\d+\s*[）)]\s*)?(\d{8,9}-\d+\s*[^\d\s].*)$")
_DIM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)(?![\d.])")
_AUDIT = {"actor", "reason", "time"}


def _plain(value):
    """可序列化 detached 值；無效數值保留成文字供草稿列出缺口。"""
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("mapping key 必須為字串")
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (float, Decimal)):
        return str(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError("輸入只能包含 JSON 資料及 Decimal")


def _digest(value):
    return hashlib.sha256(json.dumps(_plain(value), sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _number(value, *, positive=False):
    if isinstance(value, bool) or value is None:
        raise ValueError("必須為有限數值，不可空白")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("必須為有限數值，不可空白") from None
    if (not result.is_finite() or result < 0 or (positive and result == 0)
            or abs(result) > Decimal("3.4028234663852886e38")
            or (result and result.adjusted() < -38)):
        raise ValueError("必須為範圍內的正有限數值" if positive else "必須為範圍內的非負有限數值")
    return result


def _money(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _audit(value):
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k].strip()
                                          for k in _AUDIT):
        raise ValueError("人工輸入必須提供 actor/reason/time")
    try:
        instant = datetime.fromisoformat(value["time"].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("time 必須為含時區 ISO-8601") from None
    if instant.utcoffset() is None:
        raise ValueError("time 必須含時區")
    return {k: value[k] for k in sorted(_AUDIT)}


def _catalog(snapshot, *, allow_code_variants=False):
    """即使收到手工更改的 detached dict 也重新驗證完整結構。"""
    try:
        wg, code = snapshot["workgroup"], snapshot["prodkind"]
        if snapshot["category"]["prodkind"] != code:
            raise ValueError("類別識別不一致")
        nodes, options, rules = snapshot["nodes"], snapshot["options"], snapshot["rules"]
        if set(nodes) != set(options) or set(nodes) != set(rules):
            raise ValueError("快照節點、候選、用量路徑不完整")
        for path, node in nodes.items():
            if (node["pathc"] != path or node.get("workgroup", wg) != wg
                    or not path.startswith(code + "\\")):
                raise ValueError("結構路徑或工作群不一致")
        for mapping in (options, rules):
            for path, values in mapping.items():
                if any(v.get("path", path) != path or v.get("workgroup", wg) != wg for v in values):
                    raise ValueError("快照規格／用量識別不一致")
        tables = {
            "ordstr": [dict(n, workgroup=wg) for n in nodes.values()],
            "ordspd": [dict(workgroup=wg, optno=k, optdesc=v)
                       for k, v in snapshot["labels"].items()],
            "ordspe": [dict(o, path=p, workgroup=wg) for p, values in options.items() for o in values],
            "ordqty": [dict(r, path=p, workgroup=wg) for p, values in rules.items() for r in values],
        }
        return _plain(build_catalog(wg, snapshot["category"], tables, allow_code_variants=allow_code_variants))
    except (KeyError, TypeError, AttributeError):
        raise ValueError("catalog 必須為完整 detached snapshot") from None


def _source(source):
    if isinstance(source, str):
        return {"text": source, "document_id": "local-document"}
    if (not isinstance(source, dict) or set(source) != {"text", "document_id"}
            or not isinstance(source["text"], str)
            or not isinstance(source["document_id"], str) or not source["document_id"].strip()):
        raise ValueError("source 必須為字串或 {text, document_id}")
    return deepcopy(source)


def _issue(key, message, *, child_id=None, kind="confirmation", candidates=(), **extra):
    return dict(id=key, child_id=child_id, kind=kind, message=message,
                candidates=list(candidates), **extra)


def _parse(source, cat):
    products, ledger, pending, errors = [], [], [], []
    group, counts, seen_groups = "default", {}, set()
    latest = None
    for number, raw in enumerate(source["text"].splitlines(), 1):
        text = raw.strip()
        record = dict(line=number, raw=raw, group=group, scope="unknown", child_ids=[])
        ledger.append(record)
        if not text:
            record["scope"] = "blank"
            continue
        header = re.fullmatch(r"工單[：:]\s*(\S.*)", text) or _ORDER_HEADER.fullmatch(text)
        if header:
            group = header[1]
            if group in seen_groups:
                errors.append(_issue(f"source:{number}", "重複工單識別，請使用唯一群組名稱"))
            seen_groups.add(group)
            latest = None
            record.update(group=group, scope="header")
            continue
        if re.fullmatch(r"來源[：:].+", text):
            record["scope"] = "header"
            continue
        # 範圍指示優先於橫線；配件總量不可誤拆成下一張成品。
        shared = re.fullmatch(r"共用(?:\[除([\d,， ]+)\])?[：:]\s*(.+)", text)
        targeted = re.fullmatch(r"第([1-9]\d*)項(?:配件)?[：:]\s*(.+)", text)
        local = re.fullmatch(r"(?:局部|配件|每桌配件)[：:]\s*(.+)", text)
        if shared or targeted or local:
            scope = "shared" if shared else "local"
            excludes = [int(n) for n in re.findall(r"\d+", shared[1] or "")] if shared else []
            target = int(targeted[1]) if targeted else latest
            body = shared[2] if shared else targeted[2] if targeted else local[1]
            pending.append((record, body, scope, target, excludes))
            continue
        match = _ITEM.fullmatch(text)
        if match:
            counts[group] = counts.get(group, 0) + 1
            latest = counts[group]
            cid = "child-" + _digest([source["document_id"], cat["workgroup"],
                                       cat["prodkind"], group, latest])[:24]
            product = dict(id=cid, ordinal=len(products) + 1, group=group, group_ordinal=latest,
                           source_line=number, description=raw, product_qty=int(match[2]),
                           source_fragments=[])
            products.append(product)
            record.update(scope="product", child_ids=[cid])
            product["source_fragments"].append(dict(line=number, raw=raw, text=match[1] + " " + match[3],
                                                      scope="product", excludes=[]))
        else:
            errors.append(_issue(f"source:{number}", "未確認原文或作用範圍；請明示共用／第N項／配件：" + raw,
                                 source_line=number, scope_group=group if group != "default" or products else None))
    for record, text, scope, target, excludes in pending:
        available = [p for p in products if p["group"] == record["group"]]
        targets = [p for p in available if (p["group_ordinal"] not in excludes if scope == "shared"
                                          else p["group_ordinal"] == target)]
        if not targets or any(n not in {p["group_ordinal"] for p in available} for n in excludes):
            errors.append(_issue(f"source:{record['line']}", "條件的作用品項不存在或例外張次無效",
                                 scope_group=record["group"]))
        record.update(scope=scope, child_ids=[p["id"] for p in targets], excludes=excludes)
        for product in targets:
            product["source_fragments"].append(dict(line=record["line"], raw=record["raw"], text=text,
                                                      scope=scope, excludes=excludes))
    if not products:
        errors.append(_issue("source:empty", "至少需要一個『品項-----正整數』"))
    return products, ledger, errors


def _norm(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).lower().replace("×", "*").replace("x", "*")


def _identity(option):
    return {"path": option["path"], "code": option["code"]}


class _Child:
    def __init__(self, product, catalog, inputs):
        self.cat, self.inputs = catalog, inputs
        self.child = dict(product, selections={}, requirements=[], items=[], issues=[], ready=False)
        self.used = {name: set() for name in _INPUTS}
        self.no_holes = self.no_legs = False
        self.active = set()

    def issue(self, key, message, kind="confirmation", candidates=(), **extra):
        issue = _issue(key, message, child_id=self.child["id"], kind=kind, candidates=candidates, **extra)
        if issue not in self.child["issues"]:
            self.child["issues"].append(issue)

    def options(self, path=None):
        return [o for p in self.cat["nodes"] if path is None or p == path for o in entries(self.cat, p)]

    def forbidden(self, path):
        parts = path.split("\\")
        labels = [self.cat["labels"].get(p, "") for p in parts[1:]]
        holes = bool(set(parts[2:]) & {"S025", "S030", "S035"}) or any("孔" in s for s in labels)
        legs = (len(parts) > 1 and parts[1] == "B001") or any("木腳" in s for s in labels)
        return (self.no_holes and holes) or (self.no_legs and legs)

    def accessory(self, path):
        label = self.cat["labels"][self.cat["nodes"][path]["optnoc"]]
        return (self.cat["nodes"][path]["optnoc"] in {"S020", "S022", "S025", "S030", "S035", "S040"}
                or any(s in label for s in ("線盒", "孔", "走線", "置物", "配件")))

    def put(self, option, key, qty, basis, origin, audit=None, quantity_source="BOM_per_product_policy"):
        path = option["path"]
        if self.forbidden(path):
            self.issue(key + ":forbidden", "所選／必選部位與無孔或無木腳衝突：" + path, "conflict")
            return
        existing = self.child["selections"].get(path)
        if existing:
            if (existing["code"] != option["code"] or existing["quantity_basis"] != basis
                    or existing["line_qty"] != qty):
                self.issue(key + ":conflict", "同一部位規格或數量衝突，不合併也不猜平均：" + path, "conflict")
            elif key not in existing["requirements"]:
                existing["requirements"].append(key)
            return
        self.child["selections"][path] = dict(
            path=path, code=option["code"], codsc=option["codsc"], compri=option.get("compri"),
            structural=bool(option.get("structural")), custom=origin == "temporary_custom",
            optno=self.cat["nodes"][path]["optnoc"], requirements=[key], line_qty=qty,
            quantity_basis=basis, quantity_source=quantity_source, selection_source=origin, audit=audit)

    def option_identity(self, option):
        return _identity(option)

    def request(self, key, text, options, *, qty="1", basis="per_product", auto=False,
                source=None, custom_paths=None, kind="selection", quantity_source="BOM_per_product_policy"):
        options = [o for o in options if not self.forbidden(o["path"])]
        candidates = [dict(self.option_identity(o), codsc=o["codsc"]) for o in options]
        requirement = dict(id=key, text=text, kind=kind, candidates=candidates, source=source,
                           line_qty=qty, quantity_basis=basis, quantity_source=quantity_source,
                           custom_paths=custom_paths, resolution_id=source.get("fragment_id") if source else None,
                           selected=None)
        self.child["requirements"].append(requirement)
        chosen, origin, audit = None, "unique_master", None
        if key in self.inputs["choices"] and key in self.inputs["customs"]:
            self.used["choices"].add(key)
            self.used["customs"].add(key)
            self.issue(key, "同一需求不可同時指定主檔與臨時件", "conflict")
            return
        if key in self.inputs["choices"]:
            self.used["choices"].add(key)
            value = self.inputs["choices"][key]
            matched = [o for o in options if value == self.option_identity(o)]
            if len(matched) == 1:
                chosen, origin = matched[0], "operator_choice"
            else:
                self.issue(key, "人工選擇不在此需求的合法候選內", "invalid_choice", candidates)
                return
        elif key in self.inputs["customs"]:
            self.used["customs"].add(key)
            value = self.inputs["customs"][key]
            try:
                audit = _audit(value)
                if set(value) - (_AUDIT | {"path", "code", "codsc", "compri"}):
                    raise ValueError("臨時件含不允許欄位")
                path = value.get("path")
                if (not isinstance(path, str) or path not in self.cat["nodes"]
                        or (custom_paths is not None and path not in custom_paths)):
                    raise ValueError("臨時件必須明確掛到此需求允許的既有部位")
                if (not isinstance(value.get("code"), str) or not value["code"].startswith("TMP:")
                        or not value["code"][4:].strip() or not isinstance(value.get("codsc"), str)
                        or not value["codsc"].strip()):
                    raise ValueError("臨時件需要 TMP: 代碼與名稱")
                chosen, origin = value, "temporary_custom"
            except ValueError as exc:
                self.issue(key, str(exc), "invalid_custom")
                return
        elif auto and len(options) == 1:
            chosen = options[0]
        if chosen is None:
            self.issue(key, "請人工選擇合法候選：" + text if options else "需要建立客製化新件：" + text,
                       "selection" if options else "custom_required", candidates, custom_paths=custom_paths,
                       resolution_id=requirement["resolution_id"], source=source)
            return
        requirement["selected"] = self.option_identity(chosen)
        self.put(chosen, key, qty, basis, origin, audit, quantity_source)

    def fragments(self):
        """分號／逗號分片，保留片段 identity；resolution 不可吞掉未辨識內容。"""
        result = []
        for source in self.child["source_fragments"]:
            # 只拆中文詞之間的斜線，保留 1/4圓、cm/mm、規格代碼等原樣。
            for index, text in enumerate(re.split(r"[;；，]|(?<=[\u4e00-\u9fff])[/／](?=[\u4e00-\u9fff])", source["text"])):
                if not text.strip():
                    continue
                key = f"line:{source['line']}:{index}:" + _digest(text)[:12]
                original = text.strip()
                resolution = self.inputs["resolutions"].get(key)
                if resolution is not None:
                    self.used["resolutions"].add(key)
                    try:
                        _audit(resolution)
                        if (set(resolution) != _AUDIT | {"text"} or not isinstance(resolution["text"], str)
                                or not resolution["text"].strip()):
                            raise ValueError("重述必須提供非空 text，不能將未知原文忽略")
                        text = resolution["text"]
                    except ValueError as exc:
                        self.issue(key, str(exc), "invalid_resolution")
                evidence = dict(source, original=original, resolution=resolution, fragment_id=key)
                result.append((key, text.strip(), evidence))
        self.no_holes = any(re.search(r"(?:面)?無孔", t) for _, t, _ in result)
        self.no_legs = any("無木腳" in t for _, t, _ in result)
        return result

    def semantics(self, key, text, source):
        root = self.cat["prodkind"] + "\\A001"
        serial = 0

        def request(label, options, *, qty="1", basis="per_product", auto=True, paths=None,
                    quantity_source="BOM_per_product_policy"):
            nonlocal serial
            serial += 1
            self.request(key + f":{serial}", label, options, qty=qty, basis=basis, auto=auto,
                         source=source, custom_paths=paths, quantity_source=quantity_source)

        # 台尺與逐軸混用單位都不推定；只能由有稽核的 resolution 重述。
        units = set(re.findall(r"台尺|臺尺|公分|毫米|cm|mm", text, re.I))
        units = {u.lower().replace("公分", "cm").replace("毫米", "mm").replace("臺尺", "台尺") for u in units}
        if "台尺" in units or len(units) > 1:
            self.child["requirements"].append(dict(id=key, text=text, kind="units", source=source, candidates=[]))
            self.issue(key, "尺寸單位不明／混合台尺公分，需人工重述，不可猜測", "units",
                       resolution_id=key, source=source)
            return

        def explicit(match):
            path, code = match[1], match[2]
            opts = [o for o in self.options(path) if o["code"] == code]
            request(match[0], opts, qty=None if path in self.cat["nodes"] and self.accessory(path) else "1",
                    paths=[path])
            return " "

        text = re.sub(r"規格\[([^\]]+)\]=([^\s;；，]+)", explicit, text)
        for phrase in ("面無孔", "無孔", "無木腳", "主檔配置"):
            if phrase in text:
                self.child["requirements"].append(dict(id=key + ":" + phrase, text=phrase,
                                                        kind="constraint", source=source, candidates=[]))
                text = text.replace(phrase, " ")
        if self.cat["prodkind"] == "CMT1":
            def hole(match):
                quantity, dim, box = match[1], match[2], match[3]
                path = root + "\\S019\\S035"
                opts = self.options(path)
                if dim:
                    opts = [o for o in opts if _same_dimensions(o["codsc"], dim, reverse=True)]
                request(match[0], opts, qty=quantity, auto=bool(dim), paths=[path], quantity_source="source_requirement")
                if box:
                    path = root + "\\S019\\S020"
                    opts = [o for o in self.options(path) if "單掀" in o["codsc"] and "鋁" in o["codsc"]
                            and (not dim or _same_dimensions(o["codsc"], dim, reverse=True))]
                    request("單掀鋁線盒", opts, qty=quantity, auto=False, paths=[path], quantity_source="source_requirement")
                return " "

            text = re.sub(r"面\s*([1-9]\d*)\s*孔\s*(\d+(?:\.\d+)?\s*[Xx×*]\s*\d+(?:\.\d+)?)?\s*(單掀鋁)?", hole, text)
            for shape in ("平直雙彎", "四邊稍導角", "1/4圓", "平直", "平彎", "彎角", "削角"):
                if text.lstrip().startswith(shape):
                    path = root + "\\S010"
                    request(shape, [o for o in self.options(path) if _norm(o["codsc"]) == _norm(shape)], paths=[path])
                    text = text.lstrip()[len(shape):]
                    break
            dim = _DIM.search(text)
            if dim:
                dimension = dim[0]
                tail = text[dim.end():]
                unit = re.match(r"\s*(cm|mm|公分|毫米)\b", tail, re.I)
                if unit:
                    dimension += unit[0]
                request(dimension, [o for o in self.options(root) if _same_dimensions(o["codsc"], dimension)], paths=[root])
                text = text[:dim.start()] + " " + tail[len(unit[0]):] if unit else text[:dim.start()] + " " + tail
        text = text.strip(" /\t")
        if not text:
            return
        qty, basis = None, "per_product"
        per = re.match(r"每(?:桌|件)\s*([1-9]\d*(?:\.\d+)?)\s*(?:個|組|件|片|孔)?\s*(.+)", text)
        total = re.fullmatch(r"(.+?)\s*共\s*([1-9]\d*(?:\.\d+)?)\s*(?:個|組|件|片)?", text)
        if per:
            qty, text = per[1], per[2]
        elif total:
            text, qty, basis = total[1], total[2], "total"
            if source["scope"] == "shared":
                self.issue(key + ":quantity", "共用配件總量未分配到各張，不可猜平均；請逐項明示", "quantity")
                qty = None
        opts, exact = self.semantic_candidates(text)
        # 獨立未知文字不能因為某必選項恰好唯一而消失。
        if opts and not any(self.accessory(o["path"]) for o in opts) and qty is None:
            qty = "1"
        self.request(key, text, opts, qty=qty, basis=basis, auto=exact, source=source,
                     kind="semantic", quantity_source="source_requirement" if per or total else
                     "BOM_per_product_policy" if qty is not None else "unknown")

    def semantic_candidates(self, text):
        norm = _norm(text)
        exact = [o for o in self.options() if _norm(o["codsc"]) == norm and not o.get("structural")]
        if exact:
            return exact, True
        # 本機、明列同義詞；只作候選，不假稱 LLM 或自動選第一筆。
        aliases = {"有桌下走線": ("走線", "桌下槽"), "桌下走線": ("走線", "桌下槽"),
                   "單面置物版": ("單邊置物板", "單面置物板"),
                   "單面置物板": ("單邊置物板", "單面置物板"),
                   "817胡": ("817胡桃",), "噴胡": ("胡桃漆",)}
        words = aliases.get(text, (text,))
        opts = [o for o in self.options() if not o.get("structural")
                and any(_norm(w) in _norm(o["codsc"]) or _norm(w) in _norm(o["optdesc"]) for w in words)]
        return opts, False

    def complete(self):
        nodes, selected = self.cat["nodes"], self.child["selections"]
        # 精準 node 選取可啟用選配，但不替代來源需求的語意匹配。
        for key in sorted(set(self.inputs["choices"]) | set(self.inputs["customs"])):
            if not key.startswith("node:"):
                continue
            path = key[5:]
            self.request(key, path, self.options(path), qty=None if path in nodes and self.accessory(path) else "1",
                         auto=False, custom_paths=[path])
        active = {self.cat["prodkind"]}
        for path in selected:
            while path in nodes:
                active.add(path)
                path = nodes[path]["pathf"]
        groups, visited = material_groups(self.cat), set()
        for _ in range(len(nodes) + 1):
            before = set(active)
            for path, node in nodes.items():
                if node["pathf"] in active and str(node.get("must_chose") or "").upper() == "Y":
                    active.add(path)
            for parent, branches in groups.items():
                if parent not in active:
                    continue
                picked = set(branches) & active
                if len(picked) > 1:
                    self.issue("material:" + parent, "材料方案互斥：" + parent, "conflict")
                elif not picked and "material:" + parent not in visited:
                    key = "material:" + parent
                    visited.add(key)
                    self.request(key, "材料方案：" + parent, [o for p in branches for o in self.options(p)],
                                 custom_paths=branches)
                    active.update(p for p in selected if p in branches)
            for path in self.ordered(active & nodes.keys()):
                if path in selected or path in visited:
                    continue
                visited.add(path)
                if self.forbidden(path):
                    self.issue("node:" + path, "必選主檔與無孔／無木腳衝突：" + path, "conflict")
                    continue
                # 明確 BOM 預設每產品一份；配件不猜每桌數量。
                qty = None if self.accessory(path) else "1"
                self.request("node:" + path, path, self.options(path), qty=qty, auto=True, custom_paths=[path])
            if active == before:
                break
        self.active = active & nodes.keys()
        self.child["active_paths"] = self.ordered(self.active)
        for path in self.active - selected.keys():
            self.issue("missing:" + path, "啟用部位缺少規格，父子結構尚未完整：" + path, "structure")
        for parent, branches in groups.items():
            if parent in active and len(set(branches) & active) != 1:
                self.issue("material:" + parent, "啟用的材料容器必須恰有一個合法材料方案", "conflict")

    def ordered(self, paths):
        def key(path):
            node = self.cat["nodes"][path]
            try:
                seq = int(node.get("seq") or 0)
            except (ValueError, TypeError):
                seq = 0
            parent = node["pathf"]
            return (key(parent) if parent in self.cat["nodes"] else ()) + ((seq, path),)
        return sorted(paths, key=key)

    def calculate(self):
        selected, nodes = self.child["selections"], self.cat["nodes"]
        known = Decimal(0)
        by_path = {}
        try:
            factor = _number(self.cat["category"].get("quo_rate"), positive=True)
        except ValueError:
            factor = None
            self.issue("quote_rate", "需要建立部件成本：主檔報價係數缺少或無效", "cost")
        for path in self.ordered(self.active):
            selection = selected.get(path)
            item = dict(path=path, parent_path=nodes[path]["pathf"], depth=path.count("\\"),
                        label=self.cat["labels"][nodes[path]["optnoc"]], selected=selection is not None,
                        compri=None, stdqty=None, stdpar=None, line_qty=None, qty=None,
                        product_qty=self.child["product_qty"], quantity_basis="per_product",
                        part_cost=None, amount=None, sources={}, driver_path=None, driver_code=None,
                        audit=[], code=None, codsc=None)
            self.child["items"].append(item)
            by_path[path] = item
            if selection is None:
                continue
            item.update(code=selection["code"], codsc=selection["codsc"], quantity_basis=selection["quantity_basis"],
                        structural=selection["structural"], custom=selection["custom"], requirements=selection["requirements"])
            values = dict(compri=selection["compri"], stdqty=None, stdpar=None, line_qty=selection["line_qty"])
            item["sources"].update(compri="temporary_custom" if selection["custom"] else "ordspe",
                                   line_qty=selection["quantity_source"] if values["line_qty"] is not None else "unknown")
            # 配件子部件沿最近配件父層繼承實際每桌量／總量；明示子件數量不覆寫。
            # 只繼承已計算父列，故父列人工數量補填亦可正確驅動其下用料。
            if selection["quantity_source"] == "BOM_per_product_policy" and not self.accessory(path):
                parent = nodes[path]["pathf"]
                while parent in nodes:
                    if self.accessory(parent) and parent in by_path:
                        parent_item = by_path[parent]
                        values["line_qty"] = parent_item["line_qty"]
                        item["quantity_basis"] = parent_item["quantity_basis"]
                        item["sources"]["line_qty"] = "parent_accessory:" + parent
                        break
                    parent = nodes[parent]["pathf"]
            if selection["audit"]:
                item["audit"].append(selection["audit"])
            rules = self.cat["rules"][path]
            if rules:
                drivers = {r.get("part_path") for r in rules}
                driver = next(iter(drivers)) if len(drivers) == 1 else None
                driver_code = selected.get(driver, {}).get("code")
                matched = [r for r in rules if r.get("code") == driver_code] if driver in nodes and driver_code else []
                item.update(driver_path=driver, driver_code=driver_code)
                if len(matched) == 1:
                    for field in ("stdqty", "stdpar"):
                        values[field] = matched[0].get(field)
                        item["sources"][field] = "ordqty"
                else:
                    item["sources"].update(stdqty="unresolved_ordqty_driver", stdpar="unresolved_ordqty_driver")
            elif self.cat["prodkind"] == "CMT1" and nodes[path]["optnoc"] in PER_ITEM_OPTIONS:
                values.update(stdqty="1", stdpar="1")
                item["sources"].update(stdqty="CMT1_existing_per_item_policy", stdpar="CMT1_existing_per_item_policy")
            if path in self.inputs["overrides"]:
                self.used["overrides"].add(path)
                override = self.inputs["overrides"][path]
                try:
                    audit = _audit(override)
                    changes = override.get("values")
                    if (set(override) != _AUDIT | {"values"} or not isinstance(changes, dict) or not changes
                            or set(changes) - set(values)):
                        raise ValueError("補填只允許 compri/stdqty/stdpar/line_qty")
                    # 全筆先驗證才套用，任何壞值均不能部分套用。
                    normalized = {f: str(_number(v, positive=f in {"stdpar", "line_qty"})) for f, v in changes.items()}
                    audit = dict(audit, before={f: values[f] for f in normalized}, after=normalized)
                    values.update(normalized)
                    item["audit"].append(audit)
                    item["sources"].update({f: "operator_override" for f in normalized})
                except ValueError as exc:
                    self.issue("override:" + path, str(exc), "invalid_override")
            parsed = {}
            for field, value in values.items():
                try:
                    parsed[field] = _number(value, positive=field in {"stdpar", "line_qty"})
                    item[field] = str(parsed[field])
                except ValueError:
                    parsed[field] = None
            # 明確零成本結構列可不需要用量；不替空白製造 1/1。
            zero_structure = (parsed["compri"] == 0 and not selection["custom"]
                              and (selection["structural"] or any(n["pathf"] == path for n in nodes.values())))
            missing = [f for f, value in parsed.items() if value is None
                       and not (zero_structure and not rules and f in {"stdqty", "stdpar"})]
            if zero_structure:
                item["sources"]["zero_cost"] = "explicit_zero_structural_master"
            if missing:
                kind = "quantity" if missing == ["line_qty"] else "cost"
                self.issue("cost:" + path, "需要建立部件成本：" + path + "（" + ",".join(missing) + "）",
                           kind, fields=missing)
                continue
            try:
                product_quantity = _number(self.child["product_qty"], positive=True)
                quantity = parsed["line_qty"] * (product_quantity if item["quantity_basis"] == "per_product" else 1)
                cost = Decimal(0) if zero_structure else quantity * parsed["stdqty"] / parsed["stdpar"] * parsed["compri"]
                _number(quantity, positive=True)
                _number(cost)
                if factor is not None:
                    _number(cost * factor)
            except ValueError:
                self.issue("cost:" + path, "需要建立部件成本：成品數量／計算結果超出支援範圍", "cost")
                continue
            item.update(qty=str(quantity), part_cost=_money(cost), raw_cost=str(cost))
            if factor is not None:
                item["amount"] = _money(cost * factor)
            known += cost
        for name in _INPUTS:
            for key in sorted(set(self.inputs[name]) - self.used[name]):
                self.issue(name + ":" + key, "過期、多餘或未啟用的人工輸入：" + key, "stale_input")
        if not self.child["items"]:
            self.issue("empty", "沒有啟用的完整產品結構", "structure")
        self.child["ready"] = not self.child["issues"]
        calc = dict(known_cost=_money(known), complete=self.child["ready"], quote_rate=str(factor) if factor else None,
                    tax_rate="0.05", rounding="Decimal precision=80; ROUND_HALF_UP; round totals after summation",
                    pricing_policy="成本×invdoc.quo_rate；折扣0%；外加5%稅")
        if self.child["ready"]:
            subtotal = known * factor
            calc.update(total_cost=_money(known), subtotal=_money(subtotal), tax_amount=_money(subtotal * Decimal("0.05")),
                        total_price=_money(subtotal * Decimal("1.05")))
        self.child["calc"] = calc


def _same_dimensions(label, text, reverse=False):
    def dimensions(value):
        matches = list(_DIM.finditer(value))
        if len(matches) != 1 or re.search(r"台尺|臺尺", value):
            return None
        match = matches[0]
        scale = Decimal("0.1") if re.search(r"mm|毫米", value, re.I) else Decimal(1)
        return tuple(Decimal(match[i]) * scale for i in (1, 2))
    left, right = dimensions(label), dimensions(text)
    return left is not None and right is not None and (left == right or (reverse and left == right[::-1]))


def analyze(source, catalog, choices=None, customs=None, overrides=None, resolutions=None):
    """純函式。非法 envelope 拒絕；業務缺口仍回傳逐張草稿及完整階層。"""
    source, cat = _source(source), _catalog(catalog)
    inputs = dict(zip(_INPUTS, (choices, customs, overrides, resolutions)))
    inputs = {name: _plain({} if value is None else value) for name, value in inputs.items()}
    products, ledger, global_issues = _parse(source, cat)
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
            ctx = _Child(product, cat, scoped)
            ctx.child["issues"].extend(dict(i, child_id=product["id"]) for i in global_issues
                                       if i.get("scope_group") in (None, product["group"]))
            for key, text, evidence in ctx.fragments():
                ctx.semantics(key, text, evidence)
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
    if not isinstance(context, dict) or set(context) != {"version", "source", "catalog_snapshot", "inputs"}:
        raise ValueError("缺少可重建的完整 context")
    if context["version"] != VERSION or set(context["inputs"]) != set(_INPUTS):
        raise ValueError("不支援的核心版本或輸入欄位")
    return analyze(context["source"], context["catalog_snapshot"], **context["inputs"])


def validate(child, context):
    """保存前重建單張；ready、summary、hash 全都不是驗證依據。失敗拋 ValueError。"""
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
    if (not isinstance(document, dict) or set(document) != {"document_type", "version", "child", "context",
                                                           "frozen_audit", "frozen_digest"}
            or document["document_type"] != "local_dynamic_child" or document["version"] != VERSION):
        raise ValueError("不是支援的單張凍結文件")
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
