"""主檔缺口盤點，不是配置求解、報價試算或產品線核准。"""
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math

from config import WORKGROUP
from database.master_inventory import read_product_master, validate_product_code
from engine.configuration import number
from engine.pricing import PER_ITEM_OPTIONS


REPORT_VERSION = "product-master-inventory-v1"
TABLES = ("categories", "edges", "options", "quantities", "definitions")


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_number": str(value)}
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _valid_number(value, *, positive=False):
    try:
        number(value, "數值", positive=positive)
    except ValueError:
        return False
    return True


def analyze_product_master(snapshot: dict) -> dict:
    """只分析已讀到的資料；不以測試樣本或名稱猜產品線正式政策。"""
    product = snapshot["prodkind"]
    validate_product_code(product)
    issues, paths = [], []

    def issue(kind, message, path="", code="", *, level="data_error"):
        issues.append({"kind": kind, "level": level, "path": path, "code": code, "message": message})

    categories = snapshot["categories"]
    category = categories[0] if len(categories) == 1 else {}
    if len(categories) != 1:
        issue("category_count", "產品類別缺少或去除尾端空白後不唯一。")
    if category and category.get("ordkind") != "1":
        issue("not_quotation_category", "產品類別不是報價產品。")
    if not _valid_number(category.get("quo_rate"), positive=True):
        issue("invalid_quote_rate", "缺少正有限報價係數；不使用預設係數。")

    edges, options, quantities = snapshot["edges"], snapshot["options"], snapshot["quantities"]
    by_child, children, specs, rules, definitions = (defaultdict(list) for _ in range(5))
    for edge in edges:
        parent, child = edge["pathf"], edge["pathc"]
        by_child[child].append(edge)
        children[parent].append(child)
        if (not child.startswith(product + "\\") or not child.split("\\")[-1]
                or "\\\\" in child or child.rsplit("\\", 1)[0] != parent):
            issue("invalid_edge", "結構父子路徑不一致或跨產品；不可用去重掩蓋。", child)
        if edge.get("optnoc") != child.rsplit("\\", 1)[-1]:
            issue("node_code_mismatch", "子節點代碼與完整路徑不一致。", child)
        if edge.get("must_chose") not in (None, "", "Y", "N"):
            issue("invalid_required_flag", "必選標記不是已支援的 Y／N／空值。", child)
    if not edges:
        issue("missing_structure", "沒有產品結構主檔。")
    for child, rows in by_child.items():
        if len(rows) != 1:
            issue("duplicate_node", "相同子節點有多筆結構，現行路徑配置無法唯一識別。", child)
        for edge in rows:
            if edge["pathf"] != product and edge["pathf"] not in by_child:
                issue("missing_parent", "父節點未存在於產品結構。", child)
    reachable, queue = {product}, deque([product])
    while queue:
        for child in children[queue.popleft()]:
            if child not in reachable:
                reachable.add(child)
                queue.append(child)
    for path in sorted(set(by_child) - reachable):
        issue("unreachable_node", "節點無法從產品根部連接，遞迴選單可能漏讀。", path)
    for spec in options:
        specs[spec["path"]].append(spec)
        if spec["path"] not in by_child:
            issue("orphan_option", "規格沒有對應的結構節點。", spec["path"], spec["code"])
    for rule in quantities:
        rules[rule["path"]].append(rule)
        if rule["path"] not in by_child:
            issue("orphan_quantity", "用量規則沒有對應的結構節點。", rule["path"], rule["code"])
    for definition in snapshot["definitions"]:
        definitions[definition["optno"]].append(definition)

    for path in sorted(set(by_child) | set(specs) | set(rules)):
        optno = path.rsplit("\\", 1)[-1]
        labels = definitions[optno]
        label = labels[0].get("optdesc") or "" if len(labels) == 1 else ""
        if len(labels) != 1 or not label:
            issue("missing_definition", "部位名稱定義缺少、不唯一或空白，須人工核對。", path, level="review")
        path_specs, path_rules = specs[path], rules[path]
        counts = Counter(spec["code"] for spec in path_specs)
        for code, count in counts.items():
            if count != 1:
                issue("duplicate_option", "相同部位及代碼有多筆規格，現行配置不能唯一選擇。", path, code)
        positive, zero, invalid = 0, 0, 0
        spec_rows = []
        for spec in path_specs:
            cost = spec.get("compri")
            if not _valid_number(cost):
                status = "invalid_or_missing"
                invalid += 1
                issue("invalid_cost", "採購單價缺少、負值或非有限值，不能當成零價。", path, spec["code"])
            elif number(cost, "採購單價") == 0:
                status = "zero"
                zero += 1
            else:
                status = "positive"
                positive += 1
            spec_rows.append({"code": spec["code"], "description": spec.get("codsc") or "", "cost_status": status})
        structural = not path_specs and bool(children[path])
        if not path_specs and not structural:
            issue("missing_leaf_options", "葉節點沒有正式規格；不能假設免費。", path)
        driver, missing_codes = "", []
        if path_rules:
            mode = "quantity_rules"
            drivers = {r.get("part_path") for r in path_rules}
            if len(drivers) != 1 or not next(iter(drivers)):
                issue("ambiguous_driver", "用量驅動部位為空或不唯一。", path)
            else:
                driver = next(iter(drivers))
                if driver not in by_child:
                    issue("invalid_driver", "用量驅動部位不在本產品結構。", path)
                else:
                    driver_counts = Counter(s["code"] for s in specs[driver])
                    if any(count != 1 for count in driver_counts.values()):
                        issue("ambiguous_driver_options", "用量驅動部位的規格代碼不唯一。", path)
                    expected = set(driver_counts) or ({""} if children[driver] else set())
                    if not expected:
                        issue("missing_driver_options", "用量驅動部位沒有可選規格。", path)
                    actual = Counter(r["code"] for r in path_rules)
                    missing_codes = sorted(expected - set(actual))
                    if missing_codes:
                        issue("missing_quantity_codes", "驅動規格缺少用量規則：" + "、".join(missing_codes), path)
                    for code in sorted(set(actual) - expected):
                        issue("unknown_quantity_code", "用量代碼不在驅動部位規格中，可能是舊規則。", path, code)
                    if driver != path and not path.startswith(driver + "\\"):
                        issue("driver_activation_review", "跨分支驅動須檢查實際配置是否啟用，不以表格覆蓋推論相容。", path, level="review")
            for code, count in Counter(r["code"] for r in path_rules).items():
                if count > 1:
                    issue("duplicate_quantity", "相同驅動代碼有重複用量規則。", path, code)
            for rule in path_rules:
                if not _valid_number(rule.get("stdqty")):
                    issue("invalid_numerator", "用料量缺少、負值或非有限值；零用量合法。", path, rule["code"])
                if not _valid_number(rule.get("stdpar"), positive=True):
                    issue("invalid_denominator", "裁切量必須是正有限值，不可用預設值取代。", path, rule["code"])
        elif positive:
            if product == "CMT1" and optno in PER_ITEM_OPTIONS:
                mode = "existing_cmt1_per_item_policy"
                issue("per_item_policy_review", "現行環式程式有按件政策；僅限完全無用量表，不代表本次業務核准。", path, level="review")
            else:
                mode = "missing_quantity_rules"
                issue("missing_quantity_rules", "正價規格缺少用量規則，本產品不可預設為 1／1。", path)
        else:
            mode = "structural_container" if structural else "zero_cost_only" if zero and not invalid else "unresolved"
        labor = "W001" in path.split("\\") or "工費" in label
        if labor:
            issue("labor_inclusion_review", "工費候選須核對含工、含開孔及重複收費政策；名稱不能證明已含價。", path, level="review")
        paths.append({"path": path, "label": label, "reachable": path in reachable,
                      "has_children": bool(children[path]), "structural_container": structural,
                      "required_flags": sorted({edge.get("must_chose") or "" for edge in by_child[path]}),
                      "option_count": len(path_specs), "positive_cost_count": positive,
                      "zero_cost_count": zero, "invalid_cost_count": invalid,
                      "quantity_rule_count": len(path_rules), "quantity_mode": mode,
                      "driver_path": driver, "missing_driver_codes": missing_codes,
                      "labor_candidate": labor, "options": sorted(spec_rows, key=lambda row: (row["code"], row["description"]))})

    # 目前主檔沒有能證明下列工程／業務核准完成的權威欄位，不能從資料齊全推導核准。
    for kind, message in (
        ("material_policy_review", "材料互斥與上下板方案須依產品線核准；不將環式政策套用其他產品。"),
        ("compatibility_review", "腳架、板件、配件及尺寸相容性尚無完整核准關係可驗證。"),
        ("included_process_review", "線盒含開孔、貼面含工與撕膜等含價政策尚待業務核對。"),
        ("engineering_approval_review", "本次只盤點主檔，不驗證來源需求、圖面、實際組合或正式工程核准。"),
    ):
        issue(kind, message, level="review")
    issues.sort(key=lambda row: (row["level"], row["path"], row["kind"], row["code"]))
    for item in issues:
        item["issue_id"] = _digest({"product": product, "workgroup": snapshot["workgroup"], **item})[:24]
    return {"prodkind": product, "product_name": category.get("codsc") or "",
            "quote_rate": str(category.get("quo_rate")) if category.get("quo_rate") is not None else None,
            "source_digest": _digest(snapshot), "source_counts": {name: len(snapshot[name]) for name in TABLES},
            "paths": paths, "issues": issues, "data_error_count": sum(i["level"] == "data_error" for i in issues),
            "review_count": sum(i["level"] == "review" for i in issues), "can_quote": False}


def inventory_products(products, *, source_label, workgroup=WORKGROUP):
    if (not isinstance(products, tuple) or not 1 <= len(products) <= 3
            or any(not isinstance(p, str) for p in products) or len(set(products)) != len(products)):
        raise ValueError("請明確指定 1 至 3 個不重複產品代碼。")
    for product in products:
        validate_product_code(product)
    if not isinstance(source_label, str) or not source_label.strip() or len(source_label) > 200:
        raise ValueError("請填寫 1 至 200 字的資料來源／副本版本說明。")
    started = datetime.now(timezone.utc).isoformat()
    reports = [analyze_product_master(read_product_master(product, workgroup)) for product in products]
    return {"document_type": "product_master_inventory_only", "schema_version": 1,
            "analyzer_version": REPORT_VERSION, "workgroup": workgroup, "source_label": source_label.strip(),
            "read_started_at": started, "read_finished_at": datetime.now(timezone.utc).isoformat(),
            "cross_query_snapshot_guaranteed": False, "products": reports,
            "source_digest": _digest({"workgroup": workgroup, "products": [
                {"prodkind": p["prodkind"], "source_digest": p["source_digest"]} for p in reports]}),
            "policy_context": {"cmt1_per_item_options": sorted(PER_ITEM_OPTIONS),
                               "other_product_policy_inherited": False, "approval_verified": False},
            "can_quote": False}
