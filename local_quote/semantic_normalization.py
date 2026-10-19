"""受控、離線的工單語意正規化。先收產品，再解析 scope；不讀主檔、不計價。

targets 為同 group 的品項序號（不是件數）；origin 永遠保留原行及片段位置。
effective/override/conflict 是每 child 的結果；shared 是 default 而非最後一項。
未知片段不丟棄；外部參考及未定單位不是 unknown。
"""
from copy import deepcopy
from decimal import Decimal
import re

from . import dynamic_v1 as v1


SHAPES = {"彎角": "corner", "平直": "straight", "平彎": "curved",
          "平直雙彎": "double_curved", "四邊稍導角": "chamfered", "1/4圓": "quarter_round"}
SHAPE_RE = r"平直雙彎|四邊稍導角|1/4圓|彎角|平直|平彎"
DIMENSION = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(台尺|臺尺|公分|毫米|cm|mm)?"
                       r"\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(台尺|臺尺|公分|毫米|cm|mm)?", re.I)


def parse(source, cat):
    products, ledger, pending, errors = [], [], [], []
    group, counts, seen, latest, block = "default", {}, set(), None, None

    def error(record, message):
        errors.append(v1._issue(f"source:{record['line']}", message, source_line=record["line"],
                                scope_group=record["group"] if products or group != "default" else None))

    def end_block():
        nonlocal block
        if block and not block["has_body"]:
            error(block["record"], "共用標頭沒有條件內容")
        block = None

    for number, raw in enumerate(source["text"].splitlines(), 1):
        text = raw.strip()
        record = dict(line=number, raw=raw, group=group, scope="unknown", child_ids=[])
        ledger.append(record)
        if not text:
            record["scope"] = "blank"
            continue  # 空行不終止 block
        header = (re.fullmatch(r"工單[：:]\s*(\S.*)", text) or v1._ORDER_HEADER.fullmatch(text)
                  or re.fullmatch(r"([123]-[12])", text))
        if header:
            end_block()
            group = header[1]
            if group in seen:
                error(record, "重複工單識別，請使用唯一群組名稱")
            seen.add(group)
            latest = None
            record.update(group=group, scope="header")
            continue
        if re.fullmatch(r"來源[：:].+", text):
            end_block()
            record["scope"] = "header"
            continue
        category_header = re.fullmatch(r"(.+?)[（(]([A-Za-z0-9_-]+)[）)]", text)
        if category_header:
            end_block()
            if (counts.get(group, 0) or category_header[2] != cat["prodkind"]
                    or v1._norm(category_header[1]) != v1._norm(cat["category"]["codsc"])):
                error(record, "產品類別標頭須在品項之前，且名稱與代碼必須符合所選主檔")
            else:
                record["scope"] = "category_header"
            continue
        shared = re.fullmatch(r"共用(?:\[除([\d,， ]+)\])?[：:]\s*(.*)", text)
        targeted = re.fullmatch(r"第([1-9]\d*)項(?:配件)?[：:]\s*(.+)", text)
        local = re.fullmatch(r"(?:局部|配件|每桌配件)[：:]\s*(.+)", text)
        selector = re.fullmatch(r"(主席及彎角桌|列席桌)(.+)", text)
        if shared or targeted or local or selector:
            end_block()
            excludes = [int(n) for n in re.findall(r"\d+", shared[1] or "")] if shared else []
            scope = "shared" if shared else "targeted" if selector else "local"
            target = (selector[1] if selector else int(targeted[1]) if targeted else latest)
            body = shared[2] if shared else targeted[2] if targeted else local[1] if local else selector[2]
            if shared and not body:
                record.update(scope="shared_header", excludes=excludes)
                block = dict(record=record, excludes=excludes, has_body=False)
            else:
                pending.append((record, body, scope, target, excludes))
            continue
        match = v1._ITEM.fullmatch(text)
        if match:
            end_block()
            counts[group] = counts.get(group, 0) + 1
            latest = counts[group]
            cid = "child-" + v1._digest([source["document_id"], cat["workgroup"], cat["prodkind"], group, latest])[:24]
            role = re.match(r"(主席|列席)", match[1].strip())
            shape = re.match(r"(?:主席|列席)?(" + SHAPE_RE + r")", match[1].strip())
            product = dict(id=cid, ordinal=len(products) + 1, group=group, group_ordinal=latest,
                           source_line=number, description=raw, product_qty=int(match[2]),
                           role={"主席": "chair", "列席": "attendee"}.get(role[1]) if role else None,
                           shape=SHAPES[shape[1]] if shape else None, source_fragments=[])
            products.append(product)
            record.update(scope="product", child_ids=[cid])
            # 保留舊片段形式，resolution identity 仍是原片段的行／hash。
            product["source_fragments"].append(dict(line=number, raw=raw, text=match[1] + " " + match[3],
                                                     scope="product", excludes=[], targets=[latest]))
        elif block:
            block["has_body"] = True
            pending.append((record, text, "shared", None, block["excludes"]))
        else:
            error(record, "未確認原文或作用範圍；請明示共用／第N項／配件：" + raw)
    end_block()
    for record, text, scope, target, excludes in pending:
        available = [p for p in products if p["group"] == record["group"]]
        def applies(p):
            if scope == "shared":
                return p["group_ordinal"] not in excludes
            if target == "主席及彎角桌":
                return p["role"] == "chair" or p["shape"] == "corner"
            if target == "列席桌":
                return p["role"] == "attendee"
            return p["group_ordinal"] == target
        targets = [p for p in available if applies(p)]
        if not targets or any(n not in {p["group_ordinal"] for p in available} for n in excludes):
            error(record, "條件的作用品項不存在或例外張次無效")
        record.update(scope=scope, child_ids=[p["id"] for p in targets], excludes=excludes)
        for p in targets:
            p["source_fragments"].append(dict(line=record["line"], raw=record["raw"], text=text, scope=scope,
                                              excludes=excludes, targets=[t["group_ordinal"] for t in targets]))
    if not products:
        errors.append(v1._issue("source:empty", "至少需要一個『品項-----正整數』"))
    return products, ledger, errors


def normalize(key, text, source):
    """以受控片語消耗字元；未消耗區間獨立保留。沒有依詞數或最近品項猜部位。"""
    result, consumed = [], [False] * len(text)

    def emit(match, kind, part, value, quantity=None, attribute=None):
        start, end = match.span()
        if any(consumed[start:end]):
            return
        consumed[start:end] = [True] * (end - start)
        raw = source["raw"]
        original = match[0]
        column = raw.find(original)
        origin = dict(source, text=original, span=[start, end],
                      column=column + 1 if column >= 0 else None,
                      end_column=column + len(original) + 1 if column >= 0 else None)
        result.append(dict(id=f"{key}:typed:{len(result)}", kind=kind, part=part,
                           attribute=attribute or kind, targets=source.get("targets", []), value=value,
                           quantity=quantity, basis="per_product", origin=origin,
                           priority=10 if source["scope"] == "shared" else 20,
                           effective=True, override=[], conflict=[], status="parsed"))

    def scan(pattern, kind, part, value, quantity=None, attribute=None):
        for m in re.finditer(pattern, text):
            emit(m, kind, part, value(m) if callable(value) else value,
                 quantity(m) if callable(quantity) else quantity, attribute)

    # 明示既有路徑是獨立契約；讓核心原有嚴格候選檢查處理。
    scan(r"規格\[[^\]]+\]=[^\s;；，]+", "legacy", "explicit_path", lambda m: m[0])
    scan(r"主檔配置", "configuration", "product", "master")
    scan(r"^(主席|列席)", "role", "product", lambda m: {"主席": "chair", "列席": "attendee"}[m[1]])
    scan(r"(?:" + SHAPE_RE + r")", "shape", "tabletop", lambda m: SHAPES[m[0]], "1")
    scan(r"削角", "edge", "tabletop", "cut", "1")
    # 先吃盒與孔；13.5X40、6公分不會變成桌面尺寸。
    scan(r"面\s*([1-9]\d*)孔\s*(\d+(?:\.\d+)?[Xx×*]\d+(?:\.\d+)?)\s*(單掀鋁)",
         "box", "tabletop_accessory", lambda m: dict(type="single_flip_aluminum", dimensions=m[2], color=None, brush=False),
         lambda m: m[1])
    # 上述明示面孔另帶矩形挖孔需求，不與圓孔共用計數。
    for r in list(result):
        if r["kind"] == "box":
            hole = deepcopy(r)
            hole.update(id=r["id"] + ":cutout", kind="hole", part="tabletop_hole", attribute="rectangular_hole",
                        value=dict(shape="rectangle", dimensions=r["value"]["dimensions"]))
            result.append(hole)
    scan(r"(?:每(?:桌|件))?\s*([1-9]\d*)個(銀色)?單掀鋁(毛刷)?\s*(\d+(?:\.\d+)?[Xx×*]\d+(?:\.\d+)?)",
         "box", "tabletop_accessory", lambda m: dict(type="single_flip_aluminum", color="silver" if m[2] else None,
                                                       brush=bool(m[3]), dimensions=m[4]), lambda m: m[1])
    scan(r"([1-9]\d*)孔\s*(\d+(?:\.\d+)?)公分圓(?:孔)?", "hole", "tabletop_hole",
         lambda m: dict(shape="round", diameter=m[2], unit="cm"), lambda m: m[1], "round_hole")
    scan(r"(?:面)?無孔", "constraint", "tabletop_hole", "forbidden", "0", "holes_allowed")
    scan(r"無木腳", "constraint", "legs", "forbidden", "0", "legs_allowed")
    scan(r"腳\s*([1-9]\d*|兩)片不挖走[線縣]孔", "constraint", "leg_hole", "forbidden",
         lambda m: "2" if m[1] == "兩" else m[1], "holes_allowed")
    scan(r"(?:每(?:桌|件)\s*([1-9]\d*)\s*(?:個|組)?)?(?:(?:有)?桌下走線|桌下面走線)",
         "routing", "under_table", "cable_routing", lambda m: m[1] or "1")
    scan(r"單面置物[版板]", "shelf", "shelf", "single_sided", "1")
    scan(r"腳粒鎖好", "assembly", "legs", "foot_lock", "1")
    scan(r"撕保護膜", "assembly", "tabletop", "remove_protective_film", "1")
    scan(r"(?:面)?817胡(?:桃)?", "material", "tabletop", "817_walnut", "1")
    scan(r"(?:面)?紅胡340", "material", "tabletop", "340_red_walnut", "1")
    scan(r"面808白橡", "material", "tabletop", "808_white_oak", "1")
    scan(r"腳白橡紙", "material", "legs", "white_oak_paper", "1")
    scan(r"噴胡(?:桃)?", "coating", "tabletop", "walnut", "1")
    scan(r"噴紅木", "coating", "tabletop", "red_wood", "1")
    scan(r"噴白橡", "coating", "tabletop", "white_oak", "1")
    scan(r"隔板孔位(?:置)?靠單", "external_confirmation", "divider_hole", {"reference": "order"}, attribute="position")
    scan(r"隔板孔位(?:置)?中", "position", "divider_hole", "center", attribute="position")
    scan(r"孔靠單", "external_confirmation", "tabletop_hole", {"reference": "order"}, attribute="position")
    scan(r"格靠單", "external_confirmation", "divider", {"reference": "order"}, attribute="position")
    scan(r"孔置中", "position", "tabletop_hole", "center", attribute="position")
    scan(r"格置中", "position", "divider", "center", attribute="position")
    scan(r"格在走線下方", "position", "divider", "below_routing", attribute="vertical_position")
    scan(r"照圖挖孔", "external_confirmation", "tabletop_hole", {"reference": "drawing"}, attribute="drawing")
    for m in DIMENSION.finditer(text):
        units = [u.lower().replace("臺尺", "台尺").replace("公分", "cm").replace("毫米", "mm") for u in (m[2], m[4]) if u]
        unit = units[-1] if units else None
        pending = "台尺" in units or len(set(units)) > 1 or (not unit and min(Decimal(m[1]), Decimal(m[3])) < 10)
        scale = Decimal("0.1") if unit == "mm" and not pending else Decimal(1)
        value = dict(axes=[str(Decimal(m[1]) * scale), str(Decimal(m[3]) * scale)],
                     unit=unit if pending else "cm" if unit else "catalog_label", confirmation_required=pending)
        emit(m, "dimension", "tabletop", value, "1")
    # 分隔字元只在完整語句邊界消耗；未知詞仍必須列出，不能被吞入備註。
    remainder = "".join(" " if used else ch for ch, used in zip(text, consumed))
    for m in re.finditer(r"[^\s/／;；，()（）]+(?:\s+[^\s/／;；，()（）]+)*", remainder):
        if m[0].strip() == "及":
            continue
        emit(m, "unknown", "unassigned", text[m.start():m.end()].strip())
    return result


def resolve(requirements):
    """每 child 獨立做屬性優先級；保留被覆寫來源，等級相同的真矛盾阻擋。"""
    groups = {}
    for r in requirements:
        if r["kind"] in {"unknown", "legacy", "configuration", "role"}:
            continue
        slot = (r["part"], r["attribute"])
        # 不同 assembly 是不同工序，不是互相覆寫。
        if r["kind"] == "assembly":
            slot += (r["value"],)
        groups.setdefault(slot, []).append(r)
    for rows in groups.values():
        priority = max(r["priority"] for r in rows)
        top = [r for r in rows if r["priority"] == priority]
        for r in rows:
            if r not in top:
                r.update(effective=False, status="overridden")
                for winner in top:
                    winner["override"].append(r["id"])
        first = top[0]
        for r in top[1:]:
            if (r["value"], r["quantity"], r["basis"]) == (first["value"], first["quantity"], first["basis"]):
                r.update(effective=False, status="duplicate")
            else:
                for other in top:
                    other.update(conflict=[t["id"] for t in top if t is not other], status="conflict")
    bans = [r for r in requirements if r["effective"] and r["kind"] == "constraint" and r["value"] == "forbidden"]
    for ban in bans:
        for r in requirements:
            if not r["effective"] or r["part"] != ban["part"] or r is ban:
                continue
            if r["kind"] in {"position", "external_confirmation"} and r["priority"] < ban["priority"]:
                r.update(effective=False, status="not_applicable")
            elif r["kind"] == "hole":
                if r["priority"] < ban["priority"]:
                    r.update(effective=False, status="overridden")
                    ban["override"].append(r["id"])
                elif r["priority"] > ban["priority"]:
                    ban.update(effective=False, status="overridden")
                    r["override"].append(ban["id"])
                else:
                    r["conflict"].append(ban["id"])
                    ban["conflict"].append(r["id"])
    return requirements


def semantic_equal(left, right):
    """比較已生效需求（排除原文、行號、identity/hash 與主檔處理狀態）。"""
    def projection(children):
        return [(c["product_qty"], c["role"], c["shape"], sorted(
            (v1._digest({k: r[k] for k in ("kind", "part", "attribute", "targets", "value", "quantity", "basis")})
             for r in c["normalized_requirements"] if r["effective"]))) for c in children]
    return projection(left) == projection(right)


def path_traits(cat, path):
    """部位取自實際結構祖先及 label，絕不製造預設 CMT1 路徑。"""
    labels, cursor = [], path
    while cursor in cat["nodes"]:
        node = cat["nodes"][cursor]
        labels.append(cat["labels"][node["optnoc"]])
        cursor = node["pathf"]
    label = labels[0]
    ancestors = "/".join(labels)
    if any(w in ancestors for w in ("木腳", "桌腳", "腳材", "腳架")):
        area = "legs"
    elif any(w in ancestors for w in ("隔板", "擋板")):
        area = "divider"
    else:
        area = "tabletop"
    return area, label, ancestors


def paths_for(cat, r):
    kind, part = r["kind"], r["part"]
    result = []
    for path in cat["nodes"]:
        area, label, ancestors = path_traits(cat, path)
        if part in {"legs", "leg_hole"} and area != "legs":
            continue
        if part.startswith("divider") and area != "divider":
            continue
        if part.startswith("tabletop") and area != "tabletop":
            continue
        if kind == "dimension":
            matched = "尺寸" in label and "孔" not in label
        elif kind == "shape":
            matched = label in {"外型", "外形", "桌面外型", "桌面形狀"}
        elif kind == "edge":
            matched = any(w in label for w in ("修邊", "削角", "邊加工", "導角"))
        elif kind == "box":
            matched = "線盒" in label
        elif kind == "hole":
            matched = "孔" in label and not any(w in label for w in ("位置", "孔位", "走線"))
            if r["value"]["shape"] == "round":
                matched = matched and not any(w in label for w in ("方", "矩形"))
            else:
                matched = matched and "圓" not in label
        elif kind == "routing":
            matched = area == "tabletop" and "孔" not in label and any(w in label for w in ("走線", "桌下槽"))
        elif kind == "shelf":
            matched = "置物" in label
        elif kind == "material":
            matched = any(w in label for w in ("美耐板", "美耐皿", "貼皮", "面材", "飾面", "木紋紙", "腳材", "板材"))
        elif kind == "coating":
            matched = any(w in label for w in ("塗裝", "油漆", "噴漆", "面漆"))
        elif kind == "assembly":
            matched = (any(w in label for w in ("腳粒", "腳墊", "組裝")) if r["value"] == "foot_lock"
                       else any(w in label for w in ("保護膜", "包裝", "組裝")))
        elif kind == "position":
            matched = any(w in label for w in ("位置", "孔位"))
            if part.endswith("hole"):
                matched = matched and "孔" in label
            else:
                matched = matched and "孔" not in label
            if r["attribute"] == "vertical_position":
                matched = matched and any(w in label for w in ("垂直", "上下"))
            else:
                matched = matched and not any(w in label for w in ("垂直", "上下"))
        else:
            matched = False
        if matched:
            result.append(path)
    return result


ALIASES = {
    "corner": ("彎角",), "straight": ("平直",), "curved": ("平彎",),
    "double_curved": ("平直雙彎",), "chamfered": ("四邊稍導角",), "quarter_round": ("1/4圓",),
    "cut": ("削角", "切角"), "cable_routing": ("桌下走線", "桌下槽", "桌下槽走線"),
    "single_sided": ("單面置物板", "單邊置物板", "單面置物版"),
    "817_walnut": ("817胡", "817胡桃", "817胡桃木"),
    "340_red_walnut": ("紅胡340", "340紅胡", "340紅胡桃"),
    "808_white_oak": ("808白橡", "面808白橡"), "white_oak_paper": ("白橡紙", "腳白橡紙"),
    "walnut": ("噴胡", "胡桃漆", "噴胡桃"), "red_wood": ("噴紅木", "紅木漆"),
    "white_oak": ("噴白橡", "白橡漆"), "foot_lock": ("腳粒鎖好",),
    "remove_protective_film": ("撕保護膜",), "center": ("置中", "孔置中", "格置中", "隔板孔位置中"),
    "below_routing": ("格在走線下方",),
}


def matches(r, option):
    value, label = r["value"], option["codsc"]
    if option.get("structural"):
        return False
    if r["kind"] == "dimension":
        return not value["confirmation_required"] and v1._same_dimensions(label, "X".join(value["axes"]))
    if r["kind"] == "box":
        return ("單掀" in label and "鋁" in label and v1._same_dimensions(label, value["dimensions"], reverse=True)
                and (not value["color"] or "銀" in label) and (not value["brush"] or "毛刷" in label))
    if r["kind"] == "hole":
        if value["shape"] == "rectangle":
            return v1._same_dimensions(label, value["dimensions"], reverse=True)
        diameter = re.search(r"(\d+(?:\.\d+)?)\s*(公分|cm|毫米|mm)", label, re.I)
        return bool("圓" in label and diameter and Decimal(diameter[1]) *
                    (Decimal("0.1") if diameter[2].lower() in {"mm", "毫米"} else 1) == Decimal(value["diameter"]))
    return v1._norm(label) in {v1._norm(s) for s in ALIASES.get(value, ())}
