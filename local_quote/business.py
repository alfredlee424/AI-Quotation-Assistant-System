"""本機主檔四張報價：唯讀來源、保守解析、候選白名單、明確計價。

公開入口：MasterSource(db).categories()/catalog(code)、analyze、generate、
input_digest。catalog 是同一次 read 取得的五檔 detached snapshot；generate
不另外查價。UI 保存時只傳本模組產生的 FrozenPreview 給 storage。

最小來源語法（剛好四行，可有「來源：...」標頭）：
    平直70X120-----2 面無孔
    平彎70X150-----1
    平直60X120-----3
    平直90X180-----1
每行尚缺的主檔必選規格由候選補足。四行之後的已知條件作共用需求；
未知字句一律阻擋。非 CMT1 可用「主檔配置-----1」逐項選取必選主檔，
不套用 CMT1 中文語意或按件政策。精確主檔指示語法為
「規格[完整路徑]=代碼」，只能選合法主檔，不接受價格或用量。

choices: gap.key -> gap.candidates[n].value（JSON token），或只含 path/code
的 identity dict。key 綁定完整原文、類別、工作群及需求位置；過期、多餘、
改寫金額／數量的 choice 都阻擋。逐輪 analyze 不計價。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import text

from .catalog import build_catalog, entries, material_groups, number, path_label
from .storage import PREVIEW_TYPE, StorageError, freeze_preview


# engine/pricing.py 的既有白名單；避免 import 該模組而載入 config/repository。
PER_ITEM_OPTIONS = frozenset({"S010", "S015", "S020", "S022", "S025", "S030", "S035",
                            "S040", "S049", "S050", "S055", "S061", "W010", "W020",
                            "W030", "W040", "W050", "W060", "W070"})


class MasterSource:
    """唯一 SQL 邊界。只使用呼叫者注入的 db.read()，不建立一般 DB 連線。"""
    def __init__(self, db, *, allow_code_variants=False):
        self.db = db
        self.allow_code_variants = allow_code_variants

    def categories(self):
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute(text(
                "SELECT prodkind, codsc, quo_rate FROM invdoc "
                "WHERE workgroup=:wg AND ordkind='1' ORDER BY prodkind"
            ), {"wg": self.db.workgroup}).mappings().all()]

    def catalog(self, category_code):
        if not isinstance(category_code, str) or not category_code or "\\" in category_code:
            raise ValueError("請先選擇產品類別")
        # 完整 prefix 參數比對，不用 LIKE（MySQL 會把反斜線解讀為 escape）。
        columns = {
            "ordstr": "workgroup,pathf,pathc,optnof,optnoc,must_chose,seq",
            "ordspd": "workgroup,optno,optdesc,kind,code",
            "ordspe": "workgroup,path,code,codsc,compri",
            "ordqty": "workgroup,path,code,part_path,stdqty,stdpar",
        }
        with self.db.read() as conn:
            categories = conn.execute(text(
                "SELECT prodkind,codsc,quo_rate FROM invdoc "
                "WHERE workgroup=:wg AND ordkind='1' AND prodkind=:category"
            ), {"wg": self.db.workgroup, "category": category_code}).mappings().all()
            if len(categories) != 1:
                raise ValueError("所選產品類別不存在或不唯一")
            tables = {}
            for table, fields in columns.items():
                path_column = "pathc" if table == "ordstr" else "path"
                prefix_filter = "" if table == "ordspd" else f" AND substr({path_column},1,:size)=:prefix"
                rows = conn.execute(text(
                    f"SELECT {fields} FROM {table} WHERE workgroup=:wg{prefix_filter} LIMIT 20001"
                ), {"wg": self.db.workgroup, "prefix": category_code + "\\", "size": len(category_code) + 1}).mappings().all()
                if len(rows) > 20000:
                    raise ValueError("所選主檔超過單次讀取上限；不能以截斷資料報價")
                tables[table] = [dict(r) for r in rows]
        return build_catalog(self.db.workgroup, dict(categories[0]), tables,
                             allow_code_variants=self.allow_code_variants)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=lambda v: str(v) if isinstance(v, Decimal) else _bad_json())


def _bad_json():
    raise ValueError("輸入只能包含 JSON 值")


def input_digest(source, category, choices=None):
    """UI 請傳完整 catalog 或含 workgroup 的 category；code-only 不隱取環境。

    code-only 的工作群為空，故為獨立 UI 失效 key；保存驗證應使用 analysis
    回傳的 input_digest，或以完整 catalog 重算，不能混用 code-only 摘要。
    """
    if not isinstance(source, str):
        raise ValueError("來源必須為字串")
    if isinstance(category, dict):
        cat = category.get("category", category)
        identity = {"category": {k: cat.get(k) for k in ("prodkind", "codsc", "quo_rate")},
                    "workgroup": category.get("workgroup", cat.get("workgroup", ""))}
    else:
        identity = {"category": category, "workgroup": ""}
    return hashlib.sha256(_json({"source": source, "category": identity,
                               "choices": {} if choices is None else choices}).encode()).hexdigest()


def _token(option):
    return _json({"path": option["path"], "code": option["code"]})


def _candidate(catalog, option):
    return {"value": _token(option), "label": f"{path_label(catalog, option['path'])}：{option['codsc']}",
            "path": option["path"], "code": option["code"], "codsc": option["codsc"]}


def _norm(value):
    return re.sub(r"\s+", "", value).replace("×", "*").replace("X", "*").replace("x", "*")


_DIM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[Xx×*]\s*(\d+(?:\.\d+)?)(?![\d.])")
_ITEM = re.compile(r"^(.*?)\s*-{2,}\s*([1-9]\d*)(?![\d.])\s*(.*)$")
_HEADER = re.compile(r"^(?:來源[：:].+|(?:\(\d+\))?\d{8}-\d+\s+\S.*)$")
_SHAPES = ("平直雙彎", "四邊稍導角", "1/4圓", "平直", "平彎")
_FACE_HOLES = {"S025", "S030", "S035"}


def _parse(source):
    products, shared, errors = [], [], []
    header_seen = False
    for line_no, raw in enumerate(source.splitlines(), 1):
        value = raw.strip()
        if not value:
            continue
        match = _ITEM.fullmatch(value)
        if match and match[1].strip():
            products.append({"ordinal": len(products) + 1, "source_line": line_no,
                             "description": value, "product_qty": int(match[2]),
                             "text": match[1] + " " + match[3]})
        elif not products and not shared and not header_seen and _HEADER.fullmatch(value):
            header_seen = True
        else:
            shared.append((line_no, value))
    if len(products) != 4:
        errors.append("來源必須剛好有四個『描述-----正整數』品項；不能略過或合併品項")
    if products and any(line_no < products[-1]["source_line"] for line_no, _ in shared):
        errors.append("非品項條件只能放在四個品項之後作共用需求；夾行或前置條件作用範圍不明")
    return products, shared, errors


class _Analysis:
    def __init__(self, source, catalog, choices):
        self.catalog = catalog
        self.choices = choices
        self.used = set()
        self.scope = input_digest(source, catalog)[:20]
        self.questions = []

    def gap(self, child, name, label, options=(), *, auto=False):
        key = f"{self.scope}:{child['ordinal']}:{name}"
        candidates = [_candidate(self.catalog, o) for o in options]
        gap = {"key": key, "label": label, "candidates": candidates}
        self.questions.append(gap)
        chosen = None
        if key in self.choices:
            self.used.add(key)
            value = self.choices[key]
            if isinstance(value, dict) and set(value) == {"path", "code"}:
                value = _json(value)
            matches = [o for o in options if _token(o) == value]
            if len(matches) == 1:
                chosen = matches[0]
            else:
                gap = dict(gap, label=label + "（選擇不在當次候選白名單）")
        elif auto and len(options) == 1:
            chosen = options[0]
        if chosen is None:
            child["gaps"].append(gap)
        return chosen, "operator_choice" if key in self.choices else "unique_master"

    def hard(self, child, name, label):
        self.gap(child, name, label)

    def put(self, child, option, origin, line_qty=1):
        path = option["path"]
        if self.forbidden(child, path):
            self.hard(child, "forbidden:" + path, "所選分支違反無孔／無木腳要求：" + path)
            return
        previous = child["selections"].get(path)
        if previous and (previous["code"] != option["code"] or previous["line_qty"] != line_qty):
            self.hard(child, "conflict:" + path, "同一路徑需求衝突：" + path)
            return
        try:
            price = number(option.get("compri"), path + " 採購單價")
        except ValueError as exc:
            self.hard(child, "price:" + path, str(exc))
            return
        child["selections"][path] = {
            "path": path, "code": option["code"], "codsc": option["codsc"],
            "optno": option["optno"], "optdesc": option["optdesc"], "compri": price,
            "line_qty": line_qty, "selection_source": origin,
            "structural": bool(option.get("structural")),
        }

    def forbidden(self, child, path):
        segments = path.split("\\")
        hole_path = (bool(set(segments[2:]) & _FACE_HOLES)
                     or any("孔" in self.catalog["labels"].get(s, "") for s in segments[2:]))
        return ((child.get("no_holes") and len(segments) > 2 and segments[1] == "A001"
                 and hole_path)
                or (child.get("no_legs") and len(segments) > 1 and segments[1] == "B001"))

    def request(self, child, name, label, options, *, auto=False, line_qty=1):
        options = [o for o in options if not self.forbidden(child, o["path"])]
        child["requirements"].append({"key": name, "label": label, "line_qty": line_qty})
        option, origin = self.gap(child, name, label, options, auto=auto)
        if option is not None:
            self.put(child, option, origin, line_qty)

    def all_options(self, predicate):
        return [o for p in self.catalog["nodes"] if predicate(p)
                for o in entries(self.catalog, p)]

    def semantics(self, child, line_no, value):
        """只消耗明列語法；所有殘餘字句逐行列為硬缺項。"""
        cat = self.catalog
        prefix = f"line:{line_no}:"
        serial = 0

        def request(label, options, auto=False, qty=1):
            nonlocal serial
            serial += 1
            self.request(child, prefix + str(serial), label, options, auto=auto, line_qty=qty)

        def explicit(match):
            path, code = match[1], match[2]
            opts = [o for o in entries(cat, path) if o["code"] == code] if path in cat["nodes"] else []
            request(match[0], opts, auto=True)
            return " "

        value = re.sub(r"規格\[([^\]]+)\]=([^\s;/，；]+)", explicit, value)
        value = value.replace("主檔配置", " ")
        if cat["prodkind"] != "CMT1":
            if value.strip(" /;；，\t"):
                self.hard(child, prefix + "unknown", "此類別尚無語意映射：" + value.strip())
            return
        root = cat["prodkind"] + "\\A001"

        def at(suffix):
            path = root + suffix
            return entries(cat, path) if path in cat["nodes"] else []

        # 先處理孔的尺寸，以免將它當成桌面尺寸。線盒與開孔分別計量。
        def hole(match):
            qty = int(match[1])
            dim = (Decimal(match[2]), Decimal(match[3])) if match[2] else None
            box = bool(match[4])
            options = at("\\S019\\S035")
            if dim:
                options = [o for o in options if _dimensions_equal(o["codsc"], dim, reversible=True)]
            else:
                # 未提供尺寸只能列出孔規格請人選，不能默選小孔。
                options = [o for o in options if o["code"]]
            request("每件面孔數／尺寸：" + match[0], options, qty=qty)
            if box:
                boxes = [o for o in at("\\S019\\S020") if "單掀" in o["codsc"]
                         and "鋁" in o["codsc"] and dim
                         and _dimensions_equal(o["codsc"], dim, reversible=True)]
                request("每件單掀鋁線盒（請選色）：" + match[0], boxes, qty=qty)
            return " "

        value = re.sub(r"面\s*([1-9]\d*)\s*孔(?:\s*(\d+(?:\.\d+)?)\s*[Xx×*]\s*(\d+(?:\.\d+)?))?\s*(單掀鋁)?", hole, value)
        value = re.sub(r"(?:面)?無孔", " ", value)
        value = value.replace("無木腳", " ")
        for phrase, suffix in (("有桌下走線", "\\S019\\S022"),
                               ("桌下走線", "\\S019\\S022"),
                               ("單面置物版", "\\S019\\S040"),
                               ("單面置物板", "\\S019\\S040")):
            if phrase in value:
                opts = at(suffix)
                if "置物" in phrase:
                    opts = [o for o in opts if "單邊" in o["codsc"] or "單面" in o["codsc"]]
                request(phrase + "（需核准主檔映射）", opts)
                value = value.replace(phrase, " ", 1)
        if "木腳" in value:
            path = cat["prodkind"] + "\\B001"
            request("木腳尺寸／寬度", entries(cat, path) if path in cat["nodes"] else [])
            value = value.replace("木腳", " ", 1)
        for phrase in ("817胡", "噴胡"):
            if phrase in value:
                opts = self.all_options(lambda p: p.endswith(("\\S001", "\\S052")) if phrase == "817胡"
                                        else p.endswith("\\S015"))
                opts = [o for o in opts if "胡桃" in o["codsc"]
                        and (phrase != "817胡" or "817" in o["codsc"])]
                request(phrase + "：請指定完整作用部位", opts)
                value = value.replace(phrase, " ", 1)
        # shape 必須精確，不把彎角、削角等詞映射為圓／平彎。
        found_shape = None
        for shape in _SHAPES:
            if value.lstrip().startswith(shape):
                found_shape = shape
                request("桌面外型：" + shape,
                        [o for o in at("\\S010") if _norm(o["codsc"]) == shape], auto=True)
                value = value.lstrip()[len(shape):]
                break
        dim_match = _DIM.search(value)
        if dim_match:
            before = value[:dim_match.start()].strip()
            # 與尺寸相連的未知外型也是硬缺項，即使可選其他主檔外型也不可消除。
            dim = (Decimal(dim_match[1]), Decimal(dim_match[2]))
            opts = [o for o in at("") if _dimensions_equal(o["codsc"], dim)
                    and (("1/4" in o["codsc"]) == (found_shape == "1/4圓"))]
            request("桌面尺寸：" + dim_match[0], opts, auto=True)
            value = before + " " + value[dim_match.end():]
        remainder = value.strip(" /;；，\t")
        if remainder:
            self.hard(child, prefix + "unknown", "未理解原文，不能轉成生產備註：" + remainder)

    def complete_tree(self, child):
        cat = self.catalog
        nodes = cat["nodes"]
        active = {cat["prodkind"]}
        for path in child["selections"]:
            while path in nodes:
                active.add(path)
                path = nodes[path]["pathf"]
        groups = material_groups(cat)
        visited = set()
        # 每輪啟用祖先／必選邊；未啟用的選配子樹不列為必選。
        for _ in range(len(nodes) + 1):
            before = set(active)
            for path, node in nodes.items():
                if node["pathf"] in active and str(node.get("must_chose") or "").upper() == "Y":
                    active.add(path)
            for parent, branches in groups.items():
                if parent not in active:
                    continue
                selected = [p for p in branches if p in active]
                if len(selected) > 1:
                    self.hard(child, "exclusive:" + parent, "材料方案互斥：" + parent)
                elif not selected and parent not in visited:
                    visited.add(parent)
                    option, origin = self.gap(child, "material:" + parent, "請選材料方案：" + path_label(cat, parent),
                                              [o for p in branches for o in entries(cat, p)])
                    if option is not None:
                        self.put(child, option, origin)
                        active.add(option["path"])
            for path in sorted(active & nodes.keys()):
                if path in child["selections"] or ("spec:" + path) in visited:
                    continue
                # group 分支只能經互斥選擇或明示需求啟用。
                visited.add("spec:" + path)
                if self.forbidden(child, path):
                    self.hard(child, "forbidden:" + path, "必選主檔與無孔／無木腳衝突：" + path)
                    continue
                opts = entries(cat, path)
                option, origin = self.gap(child, "spec:" + path, "請選規格：" + path_label(cat, path), opts, auto=True)
                if option is not None:
                    self.put(child, option, "structural_master" if option.get("structural") else origin)
            if active == before:
                break
        child["active_paths"] = sorted(active & nodes.keys())
        # 避免未知 material policy／空 group 被視為可跳過的材料。
        for parent, branches in groups.items():
            if parent in active and not branches:
                self.hard(child, "empty_material:" + parent, "材料容器缺少合法方案")


def _dimensions_equal(description, dim, reversible=False):
    matches = list(_DIM.finditer(description))
    if len(matches) != 1:
        return False
    actual = (Decimal(matches[0][1]), Decimal(matches[0][2]))
    return actual == dim or (reversible and actual == dim[::-1])


def analyze(source_text, catalog, choices=None):
    """不計價、不讀庫；保留每張需求、配置及所有阻擋原因。"""
    if not isinstance(catalog, dict) or not catalog.get("category") or not catalog.get("workgroup"):
        raise ValueError("請先選擇產品類別並讀取主檔")
    if choices is None:
        choices = {}
    if not isinstance(choices, dict) or any(not isinstance(k, str) for k in choices):
        raise ValueError("choices 必須為穩定 key 對候選 identity 的 mapping")
    digest = input_digest(source_text, catalog, choices)
    ctx = _Analysis(source_text, catalog, choices)
    products, shared, parse_errors = _parse(source_text)
    global_gaps = [{"key": f"{ctx.scope}:source:{i}", "label": msg, "candidates": []}
                   for i, msg in enumerate(parse_errors)]
    children = []
    for product in products[:4]:
        child = {k: v for k, v in product.items() if k != "text"}
        child.update(selections={}, requirements=[], gaps=[])
        relevant = [(product["source_line"], product["text"]), *shared]
        child["no_holes"] = any(re.search(r"(?:面)?無孔", t) for _, t in relevant)
        child["no_legs"] = any("無木腳" in t for _, t in relevant)
        for line_no, value in relevant:
            ctx.semantics(child, line_no, value)
        ctx.complete_tree(child)
        child["gaps"] = list({g["key"]: g for g in child["gaps"]}.values())
        children.append(child)
    for ordinal in range(len(children) + 1, 5):
        children.append({"ordinal": ordinal, "source_line": None, "description": "缺少來源品項",
                         "product_qty": None, "selections": {}, "requirements": [], "gaps": [
                             {"key": f"{ctx.scope}:{ordinal}:missing", "label": "缺少來源品項", "candidates": []}]})
    for key in sorted(set(choices) - ctx.used):
        global_gaps.append({"key": key, "label": "過期、多餘或尚未啟用需求的選擇；必須移除後重新確認", "candidates": []})
    return {"children": children, "gaps": global_gaps + [g for c in children for g in c["gaps"]],
            "global_gaps": global_gaps,
            "questions": list({g["key"]: g for g in ctx.questions}.values()), "input_digest": digest}


def _money(value):
    number(value, "計算金額")
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _calculate(child, catalog):
    """與 existing calculator 相同 Decimal 單列成本、float 累加及 HALF_UP 政策。"""
    rate = number(catalog["category"].get("quo_rate"), "報價係數", positive=True)
    factor = number(1 + (rate - 1), "加成後報價係數", positive=True)
    qty = number(child["product_qty"], "成品數", positive=True)
    selections = child["selections"]
    items, total = [], 0.0
    for path in sorted(selections):
        selection = selections[path]
        price = number(selection["compri"], path + " 單價")
        line_qty = number(selection["line_qty"], path + " 每件項目數", positive=True)
        stdqty, stdpar, driver_path, driver_code, source = 1.0, 1.0, "", "", "zero_cost"
        if price > 0:
            rules = catalog["rules"].get(path, [])
            if rules:
                drivers = {r.get("part_path") for r in rules}
                if len(drivers) != 1 or next(iter(drivers)) not in catalog["nodes"]:
                    raise ValueError(path + " 的用量驅動路徑缺少、不唯一或不在本產品")
                driver_path = next(iter(drivers))
                driver_code = selections.get(driver_path, {}).get("code", "")
                matched = [r for r in rules if r.get("code") == driver_code]
                if not driver_code or len(matched) != 1:
                    raise ValueError(path + " 缺少當張尺寸的唯一用量規則：" + driver_path + " / " + driver_code)
                stdqty = number(matched[0].get("stdqty"), path + " 用料量")
                stdpar = number(matched[0].get("stdpar"), path + " 裁切量", positive=True)
                source = "ordqty"
            elif catalog["prodkind"] == "CMT1" and selection["optno"] in PER_ITEM_OPTIONS:
                source = "per_item_policy"
            else:
                raise ValueError(path + " 正價缺少用量規則，不能默認 1／1")
        quantity = qty * line_qty
        raw_cost = float(Decimal(str(quantity)) * Decimal(str(stdqty)) / Decimal(str(stdpar)) * Decimal(str(price)))
        unit_cost = raw_cost / quantity
        unit_price = unit_cost * factor
        items.append({"path": path, "optno": selection["optno"], "optdesc": selection["optdesc"],
                      "part_code": selection["code"], "part_desc": selection["codsc"],
                      "spc_code": selection["code"], "spdsc": selection["codsc"],
                      "qty": quantity, "product_qty": child["product_qty"], "line_qty": line_qty,
                      "stdqty": stdqty, "stdpar": stdpar, "compri": price,
                      "source": source, "driver_path": driver_path, "driver_code": driver_code,
                      "part_cost": _money(raw_cost), "unit_cost": _money(unit_cost),
                      "unit_price": _money(unit_price), "amount": _money(unit_price * quantity), "unit": "PCS"})
        total += raw_cost
    if not items:
        raise ValueError("沒有完整成本列")
    subtotal = total * factor
    tax = subtotal * 0.05
    # markup_rate 在 rate < 1 時為負；storage DTO 不接受負 rate 欄位，改以說明保存。
    result = {"items": items, "total_cost": _money(total), "subtotal": _money(subtotal),
              "quote_rate": rate, "discount_rate": 0.0, "discount_amount": 0.0,
              "after_discount": _money(subtotal), "tax_rate": 0.05, "tax_amount": _money(tax),
              "total_price": _money(subtotal + tax),
              "pricing_policy": "成本乘主檔係數；折扣固定0%；未稅小計外加5%稅；總額含稅。",
              "rounding": "ROUND_HALF_UP; totals rounded after summation; existing calculator float intermediates"}
    if rate >= 1:
        result["markup_rate"] = rate - 1
    return result


def generate(source_text, catalog, choices=None):
    """唯一計價入口；四張全部成功才固定，絕不執行保存。"""
    analysis = analyze(source_text, catalog, choices)
    children = deepcopy(analysis["children"])
    for child in children:
        child["configuration"] = {"prodkind": catalog["prodkind"], "workgroup": catalog["workgroup"],
                                  "selections": deepcopy(child["selections"])}
        blockers = analysis["global_gaps"] + child["gaps"]
        if blockers:
            child["error"] = "；".join(g["label"] for g in blockers)
            continue
        try:
            child["calc"] = _calculate(child, catalog)
        except (ValueError, InvalidOperation, OverflowError) as exc:
            child["error"] = str(exc) if isinstance(exc, ValueError) else "計算數值超出支援範圍"
    result = {"analysis": analysis, "children": children, "frozen": None}
    if all("calc" in c for c in children):
        total = sum((Decimal(str(c["calc"]["total_price"])) for c in children), Decimal(0))
        preview = {"document_type": PREVIEW_TYPE, "document_version": 1, "preview_id": uuid4().hex,
                   "workgroup": catalog["workgroup"], "created_at": datetime.now(timezone.utc).isoformat(),
                   "category": deepcopy(catalog["category"]), "source": source_text,
                   "input_digest": analysis["input_digest"], "batch_total": str(total), "children": children}
        try:
            result["frozen"] = freeze_preview(preview)
        except StorageError as exc:
            result["error"] = "無法固定完整四張：" + str(exc)
    return result
