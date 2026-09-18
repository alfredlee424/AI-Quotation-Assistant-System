"""保守的規則式需求解析；完整路徑候選，不猜價格、不盲選第一筆。

parse_and_update 只修改傳入的工作副本；Agent 會將差異轉為提案再驗證。
獨立 extract_* API 保留給既有呼叫端，正式流程以目前產品 catalog 為準。
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import re
from typing import Optional

from config import WORKGROUP, PRODUCT_PREFIX
from database import repository as repo
from engine.configuration import load_catalog, normalize_selections


CONFIRM_KEYWORDS = ["是", "是的", "確認", "建立", "沒問題", "ok", "好", "可以", "對", "正確",
                    "建立報價", "建立報價單", "確認建立", "確認建立報價", "確認建立報價單"]
CANCEL_KEYWORDS = ["取消", "取消報價", "取消報價單", "取消目前報價", "算了", "重來"]
BOARD_KEYWORDS = {"MDF": "MDF", "mdf": "MDF", "密集板": "MDF", "夾板": "夾板", "合板": "夾板"}
COLOR_KEYWORDS = {word: word for word in ("胡桃", "白橡", "紅木", "美耐板", "美耐皿", "色紙")}
LEG_KEYWORDS = {word: word for word in ("木腳", "環式腳", "45寬", "55寬", "60寬", "75寬")}
_ALIASES = ("圓孔", "線盒", "單掀", "雙掀", "銀鋁", "黑鋁", "MDF", "夾板", "胡桃", "白橡",
            "美耐板", "美耐皿", "色紙", "環式腳", "45寬", "55寬", "60寬", "75寬")
_DIMENSION = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(mm|cm|公分|毫米)?\s*[xX×*＊]\s*"
                        r"(\d+(?:\.\d+)?)\s*(mm|cm|公分|毫米)?(?![\d.])", re.IGNORECASE)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", str(value).casefold()).replace("×", "*").replace("＊", "*").replace("x", "*")


def is_confirm(text: str) -> bool:
    return re.fullmatch(r"\s*(?:" + "|".join(map(re.escape, CONFIRM_KEYWORDS)) + r")[。！!．.]*\s*",
                        text, re.IGNORECASE) is not None


def is_cancel(text: str) -> bool:
    return re.fullmatch(r"\s*(?:" + "|".join(map(re.escape, CANCEL_KEYWORDS)) + r")[。！!．.]*\s*",
                        text, re.IGNORECASE) is not None


def extract_quantity(text: str) -> Optional[int]:
    """只接受產品的張數或明確產品數量語境，不能把配件的個數當成桌數。"""
    # 「每張2個」不是產品數量；「2張桌子」才是。
    values = re.findall(r"(?<![\d.*×xX＊])([0-9]+)\s*張(?!\s*(?:色紙|板材|貼紙))", text)
    values += re.findall(r"(?:產品|桌子|辦公桌|會議桌)(?:的)?\s*數量\s*(?:改成|改為|改|要|共|是|為|[:：])?\s*(\d+)", text)
    values += re.findall(r"(?<![\d.])(\d+)\s*(?:個|件|套|台)\s*(?:產品|桌子|辦公桌|會議桌)", text)
    for clause in re.split(r"[，,；;。\n]", text):
        product_count = re.fullmatch(r"\s*(?:產品|桌子|辦公桌|會議桌)\s*(?:要|共|改成|改為|改|為|是)?\s*(\d+)\s*(?:個|件|套|台)\s*", clause)
        if product_count:
            values.append(product_count[1])
        # 只有獨立數量子句才允許省略產品名稱。
        direct = re.fullmatch(r"\s*(?:產品)?數量\s*(?:改成|改為|改|是|為|[:：])?\s*(\d+)\s*(?:張)?[。！!]?\s*", clause)
        if direct:
            values.append(direct.group(1))
    unique = {int(value) for value in values}
    return unique.pop() if len(unique) == 1 else None


def _option_entry(path: str, option: dict, optdesc: str = "") -> dict:
    return {"optno": path.rsplit("\\", 1)[-1], "optdesc": optdesc or path.rsplit("\\", 1)[-1],
            "path": path, "code": option.get("code", ""), "codsc": option.get("codsc", ""),
            "compri": option.get("compri"), "line_qty": option.get("line_qty", 1)}


def _candidate_result(entries: list[dict], *, exact: bool = False) -> Optional[dict]:
    unique = {(e["path"], e["code"]): e for e in entries}
    entries = list(unique.values())
    if not entries:
        return None
    if exact and len(entries) == 1:
        return entries[0]
    return {"candidate_options": entries, "requires_confirmation": True}


def _match_options_in_text(text: str, path: str, options: list[dict], optdesc: str) -> Optional[dict]:
    query = _normalize(text)
    exact = [o for o in options if _normalize(o.get("codsc", "")) and _normalize(o.get("codsc", "")) in query]
    if exact:
        return _candidate_result([_option_entry(path, o, optdesc) for o in exact], exact=True)
    # 部分名稱只提供候選，尤其規格含厚度、顏色等未指定資訊時不可自動選取。
    similar = []
    for option in options:
        name = _normalize(option.get("codsc", ""))
        tokens = list(_ALIASES) + re.findall(r"[一-龥]{2,}", str(option.get("codsc", "")))
        if any(_normalize(token) in query and _normalize(token) in name for token in tokens):
            similar.append(_option_entry(path, option, optdesc))
    return _candidate_result(similar)


def _size_match(text: str, path: str, options: list[dict]) -> Optional[dict]:
    """原始公分先比對；明確 mm 直接換算，無單位且兩邊 >=600 才後備換算。"""
    matches = []
    for size in _DIMENSION.finditer(text):
        width, height = Decimal(size[1]), Decimal(size[3])
        unit1, unit2 = (size[2] or "").casefold(), (size[4] or "").casefold()
        has_unit = bool(unit1 or unit2)
        if has_unit:
            unit1, unit2 = unit1 or unit2, unit2 or unit1
            width /= 10 if unit1 in ("mm", "毫米") else 1
            height /= 10 if unit2 in ("mm", "毫米") else 1
        pairs = [(width, height)]
        if not has_unit and width >= 600 and height >= 600:
            pairs.append((width / 10, height / 10))
        for wanted in pairs:
            exact = []
            for option in options:
                spec = _DIMENSION.fullmatch(str(option.get("codsc", "")).strip())
                if not spec:
                    continue
                a, b = Decimal(spec[1]), Decimal(spec[3])
                u1, u2 = (spec[2] or spec[4] or "").casefold(), (spec[4] or spec[2] or "").casefold()
                a /= 10 if u1 in ("mm", "毫米") else 1
                b /= 10 if u2 in ("mm", "毫米") else 1
                if (a, b) in (wanted, tuple(reversed(wanted))):
                    exact.append(_option_entry(path, option, "桌面尺寸"))
            if exact:
                matches.extend(exact)
                break
    return _candidate_result(matches, exact=True)


def extract_size(text: str) -> Optional[dict]:
    path = f"{PRODUCT_PREFIX}\\A001"
    options = repo.get_options_by_path(path, workgroup=WORKGROUP)
    result = _size_match(text, path, options)
    if result:
        return result
    if any(word in text for word in ("大一點", "比較大", "大桌", "尺寸")):
        return _candidate_result([_option_entry(path, o, "桌面尺寸") for o in options])
    return None


def _lookup_option_by_optno(keyword: str, optno: str) -> Optional[dict]:
    path = f"{PRODUCT_PREFIX}\\{optno}"
    return _match_options_in_text(keyword, path, repo.get_options_by_path(path, workgroup=WORKGROUP), optno)


def _lookup_option_by_path_prefix(keyword: str, path_prefix: str) -> Optional[dict]:
    """保留 API，但限定精確路徑，不再以全域模糊搜尋挑第一筆。"""
    path = path_prefix if "\\" in path_prefix else f"{PRODUCT_PREFIX}\\{path_prefix}"
    return _match_options_in_text(keyword, path, repo.get_options_by_path(path, workgroup=WORKGROUP), "")


def _fuzzy_selection(text: str, path: str, optno: str, optdesc: str) -> Optional[dict]:
    options = repo.get_options_by_path(path, workgroup=WORKGROUP)
    return _match_options_in_text(text, path, options, optdesc)


def _size_area(value: str) -> float:
    match = _DIMENSION.search(value)
    return float(match[1]) * float(match[3]) if match else -1


def extract_board(text: str) -> Optional[dict]:
    path = f"{PRODUCT_PREFIX}\\A001\\S004"
    return _match_options_in_text(text, path, repo.get_options_by_path(path, workgroup=WORKGROUP), "板材")


def extract_color(text: str) -> Optional[dict]:
    catalog = load_catalog(PRODUCT_PREFIX, WORKGROUP)
    results = []
    for path, options in catalog["options"].items():
        if "\\A001\\S005\\" in path:
            result = _match_options_in_text(text, path, options, catalog["labels"].get(path.rsplit("\\", 1)[-1], ""))
            if result:
                results.extend(result.get("candidate_options", [result]))
    # 同色但跨不同材料分支仍需指定方案。
    return _candidate_result(results)


def extract_leg(text: str) -> Optional[dict]:
    path = f"{PRODUCT_PREFIX}\\B001"
    return _match_options_in_text(text, path, repo.get_options_by_path(path, workgroup=WORKGROUP), "木腳")


def _path_in_context(clause: str, path: str, catalog: dict) -> bool:
    """用當句部件／材料方案限制候選，不能把桌面同名 MDF 套到腳板。"""
    parts = path.split("\\")
    explicit = [p for p in catalog["nodes"] if p.casefold() in clause.casefold()]
    if explicit:
        return any(path == p or path.startswith(p + "\\") for p in explicit)
    # 僅在句中只有一種主要部件語境時限縮；桌面與配件同句仍可各自命中。
    table = any(w in clause for w in ("桌面", "上板", "下板"))
    leg = any(w in clause for w in ("腳架", "桌腳", "木腳", "腳板", "鐵腳"))
    if table and not leg and len(parts) > 1 and parts[1].startswith("B"):
        return False
    if leg and not table and len(parts) > 1 and parts[1] == "A001":
        return False
    if "下板" in clause and "S005" in parts and "S007" not in parts:
        return False
    if "上板" in clause and "S007" in parts:
        return False
    schemes = {"美耐板": "S000", "美耐皿": "S001", "色紙": "S008"}
    named = [code for word, code in schemes.items() if word in clause]
    if catalog["prodkind"] == "CMT1" and len(named) == 1 and "S005" in parts:
        branches = set(parts) & {"S000", "S001", "S008"}
        if branches and named[0] not in branches:
            return False
    return True


def _catalog_matches(clause: str, catalog: dict, *, include_size: bool = True) -> list[dict]:
    results = []
    for path, options in catalog["options"].items():
        if "\\W001" in path or not _path_in_context(clause, path, catalog):
            continue
        if path == catalog["prodkind"] + "\\A001":
            result = _size_match(clause, path, options) if include_size else None
        else:
            result = _match_options_in_text(clause, path, options, catalog["labels"].get(path.rsplit("\\", 1)[-1], ""))
        if result:
            results.append(result)
    # 同一描述在不同 path 命中是歧義，不可自動套用全部。
    ambiguous = set()
    for i, left in enumerate(results):
        left_entries = left.get("candidate_options", [left])
        for j in range(i + 1, len(results)):
            right_entries = results[j].get("candidate_options", [results[j]])
            if any(_normalize(a.get("codsc", "")) == _normalize(b.get("codsc", ""))
                   for a in left_entries for b in right_entries):
                ambiguous.update((i, j))
    if ambiguous:
        candidates = [entry for i in sorted(ambiguous)
                      for entry in results[i].get("candidate_options", [results[i]])]
        results = [r for i, r in enumerate(results) if i not in ambiguous]
        results.append({"candidate_options": candidates, "requires_confirmation": True})
    return results


def extract_additional_options(text: str, quote_draft: dict) -> list[dict]:
    product = quote_draft.get("prodkind")
    if not product:
        return []
    catalog = load_catalog(product, quote_draft.get("workgroup", WORKGROUP))
    return _catalog_matches(text, catalog, include_size=False)


def _accessory_quantity(clause: str, entry: dict) -> tuple[Optional[int], bool]:
    """只對明確配件對應數量；多種配件或總量語意不明時追問。"""
    name = str(entry.get("codsc", "")) + str(entry.get("optdesc", ""))
    families = (("孔",), ("線盒", "掀", "毛刷"), ("插座",), ("配件",))
    identity = [words for words in families if any(word in name for word in words)]
    counts = list(re.finditer(r"(?<![\d.])(\d+)\s*(?:個|件|組|套)", clause))
    if not identity or not counts:
        return None, False
    if not re.search(r"每\s*(?:張|件|個|台)(?:桌子|產品|桌)?", clause):
        return None, True
    matches = []
    for index, count in enumerate(counts):
        segment = clause[count.end():counts[index + 1].start() if index + 1 < len(counts) else len(clause)]
        # 「圓孔2個」也接受，但多數量的前置描述不猜對應關係。
        if len(counts) == 1 and not any(word in segment for words in identity for word in words):
            segment = clause[:count.start()]
        if any(word in segment for words in identity for word in words):
            named_families = [words for words in families if any(word in segment for word in words)]
            if len(named_families) > 1:
                return None, True
            matches.append(int(count[1]))
    return (matches[0], False) if len(matches) == 1 else (None, True)


def parse_and_update(user_input: str, quote_draft: dict) -> tuple[dict, list[str]]:
    """完整路徑解析到工作草稿；候選容器不能扁平化成已選項。"""
    found = []
    quote_draft["selections"] = normalize_selections(quote_draft.get("selections", {}))
    quote_draft.pop("pending_options", None)
    quote_draft["questions"] = []
    qty = extract_quantity(user_input)
    if qty is not None:
        quote_draft["qty"] = qty
        found.append(f"產品數量：{qty}")
    if not quote_draft.get("prodkind"):
        quote_draft["questions"].append("請先選擇產品類別。")
        return quote_draft, found
    catalog = load_catalog(quote_draft["prodkind"], quote_draft.get("workgroup", WORKGROUP))
    selections = quote_draft["selections"]

    def store(result: dict, clause: str, *, explicit: bool = False) -> None:
        candidates = result.get("candidate_options")
        if candidates:
            # 保留每件數量在候選上，之後按編號選擇也不能遺失用量。
            candidates = deepcopy(candidates)
            for candidate in candidates:
                quantity, ambiguous = _accessory_quantity(clause, candidate)
                if quantity is not None:
                    candidate["line_qty"] = quantity
                if ambiguous:
                    quote_draft["questions"].append("請確認配件個數是每件產品用量或全部總量。")
            quote_draft.setdefault("pending_options", []).extend(deepcopy(candidates))
            return
        entry = deepcopy(result)
        path = entry.get("path")
        if not path:
            return
        removal = re.search(r"(?:不要|不用|移除|刪除|取消)", clause)
        if removal and re.search(r"改成|改為|換成|換為", clause):
            quote_draft["questions"].append("請分別指定要移除的舊部件與要設定的新規格。")
            return
        if removal:
            for old_path in list(selections):
                if old_path == path or old_path.startswith(path + "\\"):
                    del selections[old_path]
            found.append(f"移除：{path}")
            return
        quantity, ambiguous = _accessory_quantity(clause, entry)
        if ambiguous:
            quote_draft["questions"].append(f"請確認 {path} 的配件個數及是否為每件產品用量。")
            return
        if quantity is not None:
            entry["line_qty"] = quantity
        elif path in selections:
            entry["line_qty"] = selections[path].get("line_qty", 1)
        selections[path] = entry
        found.append(f"{entry.get('optdesc', path)}：{entry.get('codsc', '')}")

    for clause in re.split(r"[，,；;。\n]|另外|加上|並且", user_input):
        if not clause.strip():
            continue
        # 移除已選分支可不帶葉規格；只在唯一上下文中移除，絕不刪掉同名兄弟。
        if re.search(r"不要|不用|移除|刪除|取消", clause):
            if re.search(r"改成|改為|換成|換為", clause):
                quote_draft["questions"].append("請分別指定要移除的舊部件與要設定的新規格。")
                continue
            # 完整路徑的邊界包含反斜線；不能把 CMT1\A001 視為
            # CMT1\A001\S019\S020 的另一個移除目標。
            branch_paths = [p for p in set(catalog["nodes"]) | set(selections) if re.search(
                r"(?<![A-Za-z0-9_\\])" + re.escape(p) + r"(?![A-Za-z0-9_\\])",
                clause, re.IGNORECASE)]
            if not branch_paths and re.fullmatch(r"\s*(?:不要|不用|移除|刪除|取消)\s*(?:木腳|桌腳|腳架)\s*", clause):
                branch_paths = [p for p in selections if p.rsplit("\\", 1)[-1] == "B001"]
            if not branch_paths:
                branch_paths = [p for p in selections if _path_in_context(clause, p, catalog)
                                and any(label and label in clause for label in (
                                    catalog["labels"].get(p.rsplit("\\", 1)[-1], ""),
                                    str(selections[p].get("codsc", "")),
                                ))]
            # 父路徑已指定時，不重複把所有後代當成不同候選。
            branch_paths = [p for p in branch_paths if not any(p.startswith(other + "\\") for other in branch_paths if other != p)]
            if len(branch_paths) == 1:
                path = branch_paths[0]
                for old in list(selections):
                    if old == path or old.startswith(path + "\\"):
                        del selections[old]
                found.append(f"移除：{path}")
                continue
            if len(branch_paths) > 1:
                quote_draft["questions"].append("移除目標不唯一，請提供完整部件路徑。")
                continue
        explicit_paths = set()
        # 逐一以 catalog 的完整路徑比對正式代碼，不限定產品前綴或 Cnnn。
        for path, options in catalog["options"].items():
            pair = re.search(re.escape(path) + r"\s+([A-Za-z0-9_-]+)(?![A-Za-z0-9_-])", clause, re.IGNORECASE)
            if not pair:
                continue
            exact = [o for o in options if str(o.get("code", "")).casefold() == pair[1].casefold()]
            if len(exact) == 1:
                store(_option_entry(path, exact[0], catalog["labels"].get(path.rsplit("\\", 1)[-1], "")), clause, explicit=True)
                explicit_paths.add(path)
            else:
                quote_draft["questions"].append(f"請確認 {path} 的正式規格代碼。")
        matches = _catalog_matches(clause, catalog)
        for result in matches:
            if result.get("path") in explicit_paths:
                continue
            store(result, clause)
        if not matches and not explicit_paths and extract_quantity(clause) is None:
            if _DIMENSION.search(clause) or re.search(r"改|換|不要|不用|移除|刪除|取消|顏色|材質|配件", clause):
                quote_draft["questions"].append("部分變更尚未對應到合法規格，請提供完整部件路徑與規格。")
    pending = quote_draft.get("pending_options", [])
    if pending:
        quote_draft["pending_options"] = list({(e["path"], e["code"]): e for e in pending}.values())
    if re.search(r"\d+\s*(?:個|件|組|套)", user_input) and not found and not pending:
        quote_draft["questions"].append("請指定配件規格及每件產品的配件數量。")
    return quote_draft, found


def format_missing_prompt(missing_fields: list[str], current_draft: dict,
                          missing_options: Optional[dict[str, list[dict]]] = None) -> str:
    lines = []
    if current_draft.get("qty"):
        lines.append(f"產品數量：{current_draft['qty']}")
    if missing_fields:
        lines.append("還缺少以下資訊：")
        lines.extend(f"  • {field}" for field in missing_fields)
    # 一次聚焦第一組，編號與 allowed option shortcut 的解讀一致。
    allowed = missing_options or {}
    if allowed:
        path, options = next(iter(allowed.items()))
        lines.append(f"請先選擇：{path}")
        if not options:
            lines.append("資料庫目前沒有可供選擇的規格。")
        for index, option in enumerate(options, 1):
            lines.append(f"  {index}. {option.get('codsc', '')}（代碼：{option.get('code', '')}，"
                         f"路徑：{option.get('path') or path}）")
    lines.append("請補充完整規格，或以編號選擇上述第一組候選。")
    return "\n".join(lines)


def format_candidate_confirmation(candidates: list[dict], prompt: str = "") -> str:
    lines = [prompt or "找到多個可能規格，請指定正確的部件路徑與規格："]
    for index, candidate in enumerate(candidates, 1):
        lines.append(f"  {index}. {candidate.get('codsc', '')}（代碼：{candidate.get('code', '')}，"
                     f"路徑：{candidate.get('path', '')}）")
    lines.append("請回覆編號，或提供更完整的部件與規格描述。")
    return "\n".join(lines)


def select_candidate(text: str, candidates: list[dict]) -> Optional[dict]:
    """整句編號或唯一精確代碼／名稱才算選擇，確認不等於第一筆。"""
    value = text.strip().casefold()
    match = re.fullmatch(r"(?:第\s*)?(\d+)(?:\s*[個項號])?", value)
    if match:
        index = int(match[1]) - 1
        return deepcopy(candidates[index]) if 0 <= index < len(candidates) else None
    matched = [c for c in candidates if value and value in (
        str(c.get("code", "")).casefold(), str(c.get("codsc", "")).casefold(),
        f"{c.get('path', '')} {c.get('code', '')}".casefold(),
    )]
    return deepcopy(matched[0]) if len(matched) == 1 else None
