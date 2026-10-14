"""四張合成驗收業務核心；不是任意文件轉型器、核准器或持久 workflow。

頂層僅標準庫；inspect_source 不讀資料庫。initialize_master/build_request
須在 bootstrap 管理的新程序使用。全部主檔由單一唯讀 transaction 固定，
不使用正式 repository、不讀私人來源、不寫日誌。公開展示資料不具授權能力。
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import re
import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from engine.development_atomic_contract import DevelopmentAtomicRequest


VERSION = "four-quote-business-v1"
TEST_VERSION = "four-quote-business-single-test-v1"
LEGACY_CONFIRMATION = "legacy-two-step"
TEST_CONFIRMATION = "single-test-save"
MASTER_VERSION = "synthetic-four-catalog-v1"
PRODUCT = "SYNTH4"
WARNING = "非正式驗收報價／合成主檔與價格；不得向客戶承諾價格，真實規格、工程、含價及正式部署授權均未核准。"
SAMPLE = "\n".join((
    "(1)20990101-1 星河合成",
    "彎角70X70削角-----2 面無孔有桌下走線",
    "平直70X120-----1 面1孔13.5X40單掀鋁",
    "平直70X140-----6 面1孔13.5X40單掀鋁",
    "平彎70X140-----2 面1孔13.5X40單掀鋁",
    "單面置物版", "孔靠單/格靠單", "817胡/噴胡", "腳粒鎖好/撕保護膜",
))

# 公開清單供 UI 顯示；安全決策使用私有固定版本，不信任呼叫者修改展示物件。
_SETTINGS = (
    ("dimensions", "數量、單位與軸序", "成品數2/1/6/2；尺寸cm，深X寬，示範公差±0.1cm。", "業務及工程", "正式成品數、尺寸單位、軸序及允差"),
    ("structure", "產品分類、外型及必選結構", "明確選用合成產品分類SYNTH4，不推定正式產品分類；彎角70X70削角、平直70X120/140、平彎70X140；合成25mm板、鋼腳架、每件4腳粒。", "產品工程", "正式產品分類未提供或未核准；須核定產品分類、合法外型、削角/平彎幾何、板厚、腳架及必選項"),
    ("wirebox", "開孔、線盒及走線", "第一張面無孔、每件1組桌下線槽；其餘每件1孔及1只13.5X40cm單掀鋁盒，開孔與線盒分列計費。", "產品工程", "正式線盒規格、單位、孔尺寸、含孔政策及走線方案"),
    ("shelf", "單面置物板", "四張各每件1片單面置物板；合成深20cm、寬為桌寬減20cm，獨立成本與用量。", "產品工程及採購", "置物板正式部位、尺寸、數量、範圍及用量"),
    ("position", "定位及無孔例外", "孔靠單採合成中心後緣孔位；第一張孔位不適用；格靠單四張均採合成後側單格。未做CAD/幾何驗證。", "工程/圖面核准人", "原單/圖面識別與版本、孔位及格位基準，無孔與格位須分開核對"),
    ("finish", "材質色號及噴塗", "817胡為示範桌板胡桃貼面含於板價；噴胡為示範腳架胡桃噴塗，另列工費。", "工程及採購", "817胡/噴胡正式色號、材質、加工部位與用量"),
    ("process", "含價工序", "腳粒鎖好含於腳架組裝；撕保護膜含於桌板加工，保留零價工序列，不另重複收費。", "製造及成本核准人", "腳粒鎖好、撕保護膜及各加工的標準含價依據"),
    ("pricing", "合成價格政策與授權界線", "僅使用本版合成成本/裁切量/係數1.30、共同折扣0至10%、稅5%；逐張依舊核心獨立捨入。", "採購/業務主管/工程/DBA", "正式主檔版本、成本/裁切量/係數、折扣稅率政策及身份/工程/部署授權"),
)


def _assumptions():
    return [{"id": key, "label": label, "setting": setting, "version": MASTER_VERSION,
             "required": True, "synthetic_only": True, "formal_approved": False}
            for key, label, setting, _, _ in _SETTINGS]


ASSUMPTIONS = _assumptions()
_SHARED = {
    "單面置物版": ("shelf", "specification", ("SHELF",)),
    "單面置物板": ("shelf", "specification", ("SHELF",)),
    "孔靠單": ("position", "condition", ()),
    "格靠單": ("position", "condition", ()),
    "817胡": ("finish", "specification", ("COLOR",)),
    "噴胡": ("finish", "specification", ("SPRAY",)),
    "腳粒鎖好": ("process", "included_process", ("LOCK",)),
    "撕保護膜": ("process", "included_process", ("PEEL",)),
}
_TOKEN = re.compile("|".join(map(re.escape, _SHARED)))
_SEPARATOR = re.compile(r"[\s/／、,，;；()（）]*\Z")
# 標頭僅接受短、連續的中英文字客戶識別，非任意備註欄。
# 不以「公司」結尾作為安全判斷，需求詞即使黏在名稱中仍拒絕。
_CUSTOMER = re.compile(r"[A-Za-z\u4e00-\u9fff]{2,24}\Z")
_HEADER_REQUIREMENT = re.compile(
    r"孔|線|桌|板|格|腳|膜|鋁|噴|厚|削|鎖|尺寸|數量|規格|要求|需求|未知|新增|"
    r"修改|改為|不要|必須|需要|加厚|減|含價|免費|折扣|工序|加工|施工|定位|顏色|色號|材質|"
    r"寬|深|高度|長度|置物|平直|平彎|彎角|備註|交期|取消",
    re.I,
)
_ITEM = re.compile(
    r"\s*(?P<shape>彎角|平直|平彎)\s*(?P<depth>[0-9]+)\s*[Xx×*]\s*(?P<width>[0-9]+)"
    r"(?P<corner>削角)?\s*(?P<face>面無孔有?桌下走線|面1孔13\.5\s*[Xx×*]\s*40單掀鋁)\s*"
)
_VARIANTS = (("彎角", 70, 70, "削角", 2, "G7070"),
             ("平直", 70, 120, "", 1, "S70120"),
             ("平直", 70, 140, "", 6, "S70140"),
             ("平彎", 70, 140, "", 2, "C70140"))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _decimal(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("非有限計算證據")
    return "0" if number == 0 else format(number, "f")


def _fixed(value):
    """浮點只可能來自既有核心，固定證據使用非科學記號字串。"""
    if isinstance(value, float):
        return _decimal(value)
    if isinstance(value, dict):
        return {key: _fixed(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_fixed(item) for item in value]
    return value


def _parse(raw):
    from agent.work_orders import _HEADER, parse_work_orders

    if type(raw) is not str or "\x00" in raw or "\r" in raw:
        raise ValueError("只接受 LF 純文字来源，不接受 trial/review dict、上傳或控制字元")
    # splitlines 會吃掉末尾換行；只容許 LF，末尾空行稍後完整保存。
    if any(char in raw for char in ("\v", "\f", "\x85", "\u2028", "\u2029")):
        raise ValueError("不接受非 LF 換行")
    batch = parse_work_orders(raw)
    if len(batch.orders) != 1 or len(batch.orders[0].items) != 4:
        raise ValueError("來源必須恰有一張工單及四個不同品項，不接受少項、重複或拆批")
    order = batch.orders[0]
    sections = [line for line in batch.lines if line.kind == "section"]
    if len(sections) > 1 or (sections and (
            sections[0].family_hint != "環式會議桌" or order.family_hint != "環式會議桌")):
        raise ValueError("來源分類可省略；若提供僅接受單一環式會議桌分類標題")
    # 不允許寬鬆分類器把標題尾端未知規格吞成來源識別。
    if sections and not re.fullmatch(r"\s*(?:(?:Trello|Terllo)\s*內\s*)?環式會議桌\s*生產工單\s*", sections[0].raw, re.I):
        raise ValueError("分類標題含未知內容")
    by_line = {line.line_id: line for line in batch.lines}
    specs, segments, shared_lines = [], [], []
    def segment(line, start, end, assumption, disposition, paths=(), targets=()):
        segments.append({"source_line_id": line.line_id, "source_line_no": line.number,
                         "start": start, "end": end, "text": line.raw[start:end],
                         "assumption_id": assumption, "disposition": disposition,
                         "parts": list(paths), "targets": list(targets)})

    for item, expected in zip(order.items, _VARIANTS):
        # 僅匹配時連接；來源、候選、需求帳本與保存內容均保留原文及數量位置。
        suffix = item.suffix_raw.strip()
        description = item.description_raw + suffix
        match = _ITEM.fullmatch(description)
        if match is None:
            raise ValueError("品項含未知需求或衝突：只支援本案明確外型、尺寸及孔/走線條件")
        shape, depth, width, corner = match["shape"], int(match["depth"]), int(match["width"]), match["corner"] or ""
        if (shape, depth, width, corner, item.quantity_candidate) != expected[:5]:
            raise ValueError("品項順序、外型、尺寸或數量不符；重複或不同尺寸必須重新核定")
        no_hole = match["face"].startswith("面無孔")
        if no_hole != (shape == "彎角"):
            raise ValueError("第一張必須面無孔有桌下走線，其餘三張每件1孔13.5X40單掀鋁")
        # 只允許數量在完整面條件前，或無孔條件與走線條件之間；不可拆開規格詞。
        boundaries = {description[:match.start("face")].rstrip()}
        if no_hole:
            boundaries.add(description[:match.start("face") + len("面無孔")].rstrip())
        if suffix and item.description_raw not in boundaries:
            raise ValueError("品項含未知需求或衝突：只支援本案明確外型、尺寸及孔/走線條件")
        line = by_line[item.source_line_id]
        face_start = line.raw.index("面無孔" if no_hole else "面1孔")
        # 數量／分隔符可能在面條件前或後，隨原始連續片段完整保存。
        segment(line, 0, face_start, "structure", "specification", ("A001", "BOARD", "LEGS", "FEET"), (item.item_id,))
        if no_hole:
            middle = face_start + len("面無孔")
            segment(line, face_start, middle, "wirebox", "condition", (), (item.item_id,))
            segment(line, middle, len(line.raw), "wirebox", "specification", ("WIRE",), (item.item_id,))
        else:
            segment(line, face_start, len(line.raw), "wirebox", "specification", ("HOLE", "BOX"), (item.item_id,))
        specs.append({"source_item_id": item.item_id, "source_line_id": item.source_line_id,
                      "source_line_no": line.number, "raw": line.raw, "shape": shape,
                      "depth": depth, "width": width, "corner": corner, "product_qty": item.quantity_candidate,
                      "size_code": expected[5], "no_hole": no_hole, "quantity_raw": item.quantity_raw})

    keys = [spec["source_item_id"] for spec in specs]
    seen = []
    for line in batch.lines:
        if line.kind in ("item", "blank"):
            continue
        if line.kind in ("section", "order"):
            # 不要求公司後綴；標頭不能夾帶需求而當 metadata。
            if line.kind == "order":
                header = _HEADER.fullmatch(line.raw)
                customer = header["tail"].strip() if header else ""
                if (header is None or not re.fullmatch(r"[0-9]{8,9}-[0-9]+", header["ref"])
                        or not _CUSTOMER.fullmatch(customer) or _HEADER_REQUIREMENT.search(customer)
                        or any(token in customer for token in _SHARED)):
                    raise ValueError("來源標題須為日期式單號及短純客戶識別；未知/混合需求不可當來源識別")
            segment(line, 0, len(line.raw), "dimensions", "metadata")
            continue
        matches = list(_TOKEN.finditer(line.raw))
        if not matches:
            raise ValueError("來源有未知行，不得忽略")
        previous = 0
        for match in matches:
            if not _SEPARATOR.fullmatch(line.raw[previous:match.start()]):
                raise ValueError("共用來源含未知或相反要求")
            token = match.group()
            normalized = "單面置物版" if token == "單面置物板" else token
            if normalized in seen:
                raise ValueError("來源共用需求重複，不自動合併")
            seen.append(normalized)
            assumption, disposition, paths = _SHARED[token]
            segment(line, previous, match.end(), assumption, disposition, paths, keys)
            previous = match.end()
        if not _SEPARATOR.fullmatch(line.raw[previous:]):
            raise ValueError("共用來源有未處置尾段")
        segments[-1]["end"] = len(line.raw)
        segments[-1]["text"] = line.raw[segments[-1]["start"]:]
        shared_lines.append(line.number)
    if set(seen) != {"單面置物版", "孔靠單", "格靠單", "817胡", "噴胡", "腳粒鎖好", "撕保護膜"}:
        raise ValueError("共用需求少項，不能以預設需求補齊")
    segments.sort(key=lambda s: (s["source_line_no"], s["start"]))
    # 正反向核對：每個非空白來源字元恰被一段覆蓋，每段恰指回原文，含數量。
    for line in batch.lines:
        cursor = 0
        for seg in (s for s in segments if s["source_line_id"] == line.line_id):
            if (seg["start"] != cursor or not cursor < seg["end"] <= len(line.raw)
                    or seg["text"] != line.raw[cursor:seg["end"]]):
                raise ValueError("來源帳本漏項或無效")
            cursor = seg["end"]
        if line.raw.strip() and cursor != len(line.raw):
            raise ValueError("來源帳本漏項或無效")
    return batch, specs, segments, shared_lines


def _gaps(specs, segments):
    gaps = []
    for key, label, _, owner, missing in _SETTINGS:
        relevant = [s for s in segments if s["assumption_id"] == key]
        if key == "dimensions":
            relevant = []  # 數量/尺寸缺口引用明細，不以公司名稱充當尺寸依據。
        # 數量/價格橫跨全單；保留各張原文，不捏造不存在的價格要求片段。
        quotes = relevant or [{"source_line_no": s["source_line_no"], "text": s["raw"]} for s in specs]
        gaps.append({"id": key, "label": label, "missing": missing, "required_approver": owner,
                     "sources": [{"line_no": s["source_line_no"], "text": s["text"]} for s in quotes],
                     "applies_to": [s["source_item_id"] for s in specs], "positions": [1, 2, 3, 4],
                     "parts": sorted({part for s in relevant for part in s["parts"]}),
                     "status": "FORMAL_APPROVAL_MISSING", "formal_approved": False,
                     "formal_reference": None, "formal_version": None,
                     "synthetic_reference": MASTER_VERSION})
    return gaps


def inspect_source(raw):
    """純來源解析展示；成功也不代表規格/成本核准，不查價、不建庫。"""
    batch, specs, segments, shared = _parse(raw)
    return {"document_type": "four-quote-source-inspection-only", "version": VERSION,
            "warning": WARNING, "raw_text": raw, "source_digest": batch.source_digest,
            "original_order_no": batch.orders[0].source_ref_raw, "children": specs,
            "dispositions": segments, "shared_line_numbers": shared,
            "assumptions": _assumptions(), "formal_gaps": _gaps(specs, segments),
            "can_quote": False, "can_confirm": False, "can_save": False}


def _path(part):
    return PRODUCT + "\\" + ({"BOARD": "A001\\BOARD", "BOX": "HOLE\\BOX"}.get(part, part))


def _master_document():
    """自有合成規格，不是四份配置答案；由來源尺寸選碼及成本用量規則解析。"""
    labels = {"A001": "合成外型及深X寬cm", "BOARD": "25mm桌板", "LEGS": "鋼腳架",
              "FEET": "腳粒", "HOLE": "桌面開孔工費", "BOX": "13.5X40cm單掀鋁盒",
              "WIRE": "桌下走線槽", "SHELF": "單面置物板", "COLOR": "817胡桌板貼面",
              "SPRAY": "胡桃色腳架噴塗", "LOCK": "腳粒鎖好", "PEEL": "撕保護膜"}
    prices = {"BOARD": "480", "LEGS": "320", "FEET": "3", "HOLE": "80", "BOX": "210",
              "WIRE": "125", "SHELF": "95", "COLOR": "0", "SPRAY": "60", "LOCK": "0", "PEEL": "0"}
    nodes, options, rules = {}, {}, {}
    for part in labels:
        path = _path(part)
        nodes[path] = {"pathc": path, "pathf": path.rsplit("\\", 1)[0],
                       "must_chose": "N" if part in ("HOLE", "WIRE") else "Y"}
        if part == "A001":
            options[path] = [{"code": code, "codsc": f"合成{shape}{depth}X{width}{corner}", "compri": "0"}
                             for shape, depth, width, corner, _, code in _VARIANTS]
        else:
            options[path] = [{"code": "SYN_" + part, "codsc": "合成/" + labels[part], "compri": prices[part]}]
        # 每個付費部位都必須有顯式尺寸驅動規則，沒有 per-item fallback。
        if part in prices and Decimal(prices[part]) > 0:
            rules[path] = []
            for shape, depth, width, corner, _, code in _VARIANTS:
                usage, divisor = "1", "1"
                if part == "BOARD":
                    usage, divisor = str(depth * width), "10000"
                elif part == "SHELF":
                    usage, divisor = str(20 * (width - 20)), "1000"
                elif part == "LEGS":
                    usage, divisor = ("1.2" if shape in ("彎角", "平彎") else "1"), "1"
                rules[path].append({"part_path": _path("A001"), "code": code,
                                    "stdqty": usage, "stdpar": divisor,
                                    "reference": MASTER_VERSION, "synthetic_only": True})
    return {"version": MASTER_VERSION, "synthetic_only": True, "workgroup": "103",
            "catalog": {"prodkind": PRODUCT, "nodes": nodes, "options": options, "labels": labels},
            "category": {"prodkind": PRODUCT, "quo_rate": "1.30"}, "quantity_rules": rules,
            "assumptions": _assumptions()}


def _directory(directory):
    from acceptance.bootstrap import isolate_process, verify_directory
    return verify_directory(isolate_process(directory))


def initialize_master(directory):
    """明確一次載入；已存在（含損壞/空表）一律拒絕，不 upsert、不改檔案識別。"""
    directory = _directory(directory)
    document = _master_document()
    payload = _json(document)
    with closing(sqlite3.connect((directory / "master.sqlite").as_uri() + "?mode=rw", uri=True)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE acceptance_synthetic_catalog (version TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL)")
            db.execute("INSERT INTO acceptance_synthetic_catalog VALUES (?, ?, ?)", (MASTER_VERSION, payload, _digest(payload)))
            db.commit()
        except sqlite3.Error as exc:
            db.rollback()
            raise ValueError("合成主檔已載入或庫結構不符；不覆寫、不自動修復") from exc
    return {"version": MASTER_VERSION, "digest": _digest(payload), "warning": WARNING}


class _Master:
    """只提供 pricing 所需兩個查詢；資料已由唯一唯讀交易固定。"""
    def __init__(self, directory):
        with closing(sqlite3.connect((directory / "master.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            try:
                rows = db.execute("SELECT version, payload, digest FROM acceptance_synthetic_catalog").fetchall()
            except sqlite3.Error as exc:
                raise ValueError("尚未明確載入合成主檔") from exc
        expected = _master_document()
        # 本版沒有編輯主檔介面。摘要不是授權；另比對可信版本，連重算摘要也不能偷改成本/規格。
        if len(rows) != 1 or rows[0][0] != MASTER_VERSION or _digest(rows[0][1]) != rows[0][2] or rows[0][1] != _json(expected):
            raise ValueError("合成主檔缺規格/成本/用量、遭修改或版本不符；拒絕計價")
        self.document, self.digest = json.loads(rows[0][1]), rows[0][2]
        self.catalog = self.document["catalog"]

    def get_product_category(self, prodkind, *, workgroup):
        if (prodkind, workgroup) != (PRODUCT, "103"):
            raise ValueError("合成主檔產品或事業別不符")
        return deepcopy(self.document["category"])

    def get_quantity_rules(self, path, *, workgroup):
        if workgroup != "103" or path not in self.catalog["nodes"]:
            raise ValueError("用量路徑或事業別不符")
        return deepcopy(self.document["quantity_rules"].get(path, []))


def _configuration(line, spec, master):
    from engine.configuration import resolve_configuration
    parts = ["A001", "BOARD", "LEGS", "FEET", "SHELF", "COLOR", "SPRAY", "LOCK", "PEEL"]
    parts += ["WIRE"] if spec["no_hole"] else ["HOLE", "BOX"]
    config = deepcopy(line.configuration)  # 舊 questions/gates 不刪除、不設正式 flags。
    config.update(prodkind=PRODUCT, workgroup="103", qty=spec["product_qty"], discount_rate=0,
                  selections={_path(part): {"path": _path(part), "code": spec["size_code"] if part == "A001" else "SYN_" + part,
                                             "line_qty": 4 if part == "FEET" else 1} for part in parts})
    resolved = resolve_configuration(config, catalog=master.catalog)
    if not resolved["valid"]:
        raise ValueError("；".join(resolved["errors"] + resolved["missing"]))
    if set(resolved["selections"]) != {_path(part) for part in parts}:
        raise ValueError("配置啟用項與來源/合成設定不一致")
    config["selections"] = resolved["selections"]
    return replace(line, configuration=config), resolved


def _review(draft, specs, segments, actor, master, *, test_defaults=False):
    from agent.requirement_review import RequirementRecord, SpecBinding, _evidence, _record_id, _requirements_digest
    from agent.conditions import ConditionBinding, ConditionRecord, _parent_digest
    records = []
    by_id = {line.line_id: line for line in draft.lines}
    for seg in segments:
        targets = tuple(seg["targets"])
        bindings = []
        for target in targets:
            for part in seg["parts"]:
                path = _path(part)
                selected = by_id[target].configuration["selections"].get(path)
                matches = [o for o in master.catalog["options"].get(path, []) if selected and o["code"] == selected["code"]]
                if len(matches) != 1:
                    raise ValueError("需求對照缺合法選配，不得只寫生產備註")
                bindings.append(SpecBinding(target, path, selected["code"], selected["line_qty"]))
        # 條件不假裝已通過規格；舊帳本使用 engineering_review，真實缺口仍阻擋。
        disposition = "engineering_review" if seg["disposition"] == "condition" else seg["disposition"]
        record = RequirementRecord(_record_id(draft, seg["source_line_id"], seg["start"], seg["end"]),
            seg["source_line_id"], seg["start"], seg["end"], seg["text"], targets, tuple(bindings), disposition,
            "NEEDS_ENGINEERING" if disposition == "engineering_review" else "RECORDED",
            (_evidence(draft, targets, actor, MASTER_VERSION + ":" + seg["assumption_id"],
                       "自動套用測試預設合成設定，未逐項人工核准。" if test_defaults else
                       "已明確接受合成示範設定；不代表正式工程核准。"),))
        records.append(record)
    draft = replace(draft, requirements=tuple(records))
    requirements = {r.requirement_id: r for r in records}
    conditions = []
    for spec in specs:
        target = spec["source_item_id"]
        def condition(subject, state, part, text, value):
            keys = tuple(r.requirement_id for r in records if text in r.text and target in r.targets)
            if not keys or _path(part) not in master.catalog["nodes"]:
                raise ValueError("條件缺來源需求或合法部位")
            conditions.append(ConditionRecord(uuid4().hex, subject, state, "line", "", (target,), value,
                keys, (ConditionBinding(target, _path(part), PRODUCT),), (),
                (_evidence(draft, (target,), actor, MASTER_VERSION + ":position", "合成驗收條件；正式定位/圖面缺口保留。"),),
                _requirements_digest(requirements, keys), _parent_digest((), ())))
        condition("surface_hole", "FORBIDDEN" if spec["no_hole"] else "REQUIRED", "HOLE",
                  "面無孔" if spec["no_hole"] else "面1孔", "無孔" if spec["no_hole"] else "每件1孔")
        if spec["no_hole"]:
            condition("underdesk_wiring", "REQUIRED", "WIRE", "桌下走線", "每件1組合成桌下線槽")
        condition("hole_position", "NOT_APPLICABLE" if spec["no_hole"] else "REQUIRED", "HOLE", "孔靠單",
                  "無孔所以孔位不適用" if spec["no_hole"] else "合成中心後緣孔位，未驗證幾何")
        condition("partition_position", "REQUIRED", "SHELF", "格靠單", "合成後側單格，未驗證幾何")
    return replace(draft, conditions=tuple(conditions))


def _validate_review(draft, batch, specs, segments, actor, resolved):
    from agent.quote_batch import build_quote_batch
    from agent.requirement_review import requirement_report
    from agent.conditions import condition_report, validate_condition_configuration
    expansion = build_quote_batch(draft, actor=actor, source_batch=batch)
    report = requirement_report(draft)
    if not report["source_coverage_complete"] or len(draft.requirements) != len(segments):
        raise ValueError("來源帳本漏項或無效")
    for record, seg in zip(draft.requirements, segments):
        expected = "NEEDS_ENGINEERING" if seg["disposition"] == "condition" else "RECORDED"
        disposition = "engineering_review" if seg["disposition"] == "condition" else seg["disposition"]
        expected_bindings = {(target, _path(part)) for target in seg["targets"] for part in seg["parts"]}
        if (record.status != expected or record.targets != tuple(seg["targets"]) or record.text != seg["text"]
                or record.disposition != disposition
                or {(b.line_id, b.path) for b in record.bindings} != expected_bindings):
            raise ValueError("逐項需求未完成本輪合成核對或已失效")
    conditions = condition_report(draft)
    # 只有「有設定但未驗幾何」是本版明列的合成假設，不能放行衝突/缺部位等其他 issue。
    if any(issue["kind"] != "position_review" for issue in conditions["issues"]):
        raise ValueError("條件衝突、缺選配、來源或定位依據失效")
    expected_positions = {(s["source_item_id"], "partition_position") for s in specs}
    expected_positions |= {(s["source_item_id"], "hole_position") for s in specs if not s["no_hole"]}
    actual_positions = {(c.targets[0], c.subject) for c in draft.conditions if c.state == "REQUIRED" and c.subject.endswith("position")}
    if expected_positions != actual_positions or len(conditions["issues"]) != 7:
        raise ValueError("必要定位條件缺漏，不能因無孔解除格位")
    expected_conditions = set()
    for spec in specs:
        target = spec["source_item_id"]
        expected_conditions.add((target, "surface_hole", "FORBIDDEN" if spec["no_hole"] else "REQUIRED", _path("HOLE")))
        expected_conditions.add((target, "hole_position", "NOT_APPLICABLE" if spec["no_hole"] else "REQUIRED", _path("HOLE")))
        expected_conditions.add((target, "partition_position", "REQUIRED", _path("SHELF")))
        if spec["no_hole"]:
            expected_conditions.add((target, "underdesk_wiring", "REQUIRED", _path("WIRE")))
    actual_conditions = {(c.targets[0], c.subject, c.state, c.bindings[0].path) for c in draft.conditions
                         if len(c.targets) == len(c.bindings) == 1 and c.status == "RECORDED"}
    if actual_conditions != expected_conditions or len(draft.conditions) != len(expected_conditions):
        raise ValueError("必要孔/走線/定位條件缺漏或重複")
    for line, result in zip(draft.lines, resolved):
        validate_condition_configuration(draft, line.line_id, line.configuration, result["active_paths"])
    return expansion, conditions


def build_request(directory, raw, *, actor, accepted_assumptions, source_confirmed,
                  discount_rate="0", revision=0, predecessor=None,
                  confirmation_mode=LEGACY_CONFIRMATION) -> DevelopmentAtomicRequest:
    """只從本次純文字、已接受設定與私有合成主檔新建 typed request。

    accepted_assumptions: 不重複的全部必要 id 清單。predecessor: 前版 request_id
    （32位hex），只作追溯；歸屬/持久版本由 workflow 核對。每次呼叫全新身份，
    重試必須重送 workflow 保存的原 request，不得再呼叫本函式。
    """
    directory = _directory(directory)
    from engine.development_atomic_contract import (
        DevelopmentAmounts, DevelopmentAtomicChild, DevelopmentAtomicRequest, DevelopmentCostRow,
        DevelopmentSelection, DevelopmentSource, DevelopmentSourceLine, MONEY_FIELDS,
        canonical, decimal_string, integer, text, validate_request,
    )
    text(actor, 80)
    actor = actor.strip()
    integer(revision)
    test_defaults = confirmation_mode == TEST_CONFIRMATION
    if confirmation_mode not in (LEGACY_CONFIRMATION, TEST_CONFIRMATION):
        raise ValueError("未知確認契約")
    if test_defaults and (actor != "本機測試" or accepted_assumptions or source_confirmed is not False):
        raise ValueError("單次測試使用預設設定，不記錄逐項人工接受")
    if not test_defaults and source_confirmed is not True:
        raise ValueError("必須明確確認本次來源、四張順序、尺寸與成品數")
    if not test_defaults and (type(accepted_assumptions) not in (list, tuple, set, frozenset)
            or any(type(key) is not str for key in accepted_assumptions)
            or len(set(accepted_assumptions)) != len(accepted_assumptions)
            or set(accepted_assumptions) != {row[0] for row in _SETTINGS}):
        raise ValueError("必要合成設定未全部接受，或包含未知/重複設定")
    rate = decimal_string(discount_rate)
    if rate > Decimal("0.1"):
        raise ValueError("共同折扣只允許0至10%")
    if Decimal(str(float(rate))) != rate:
        raise ValueError("折扣精度超出既有單品核心，拒絕以不同實際折扣計算")
    if (revision == 0 and predecessor is not None) or (revision > 0 and (
            type(predecessor) is not str or not re.fullmatch(r"[0-9a-f]{32}", predecessor))):
        raise ValueError("新版須追溯前版 request_id；初版不得冒用前版")
    batch, specs, segments, shared_lines = _parse(raw)
    master = _Master(directory)
    from agent.multi_quote import from_work_order
    from engine.quote_batch_trial import _calculation, _sum, FORMULA_VERSION, POLICY_VERSION
    draft = from_work_order(batch, batch.orders[0].order_id)
    lines, resolutions = zip(*[_configuration(line, spec, master) for line, spec in zip(draft.lines, specs)])
    draft = replace(draft, lines=tuple(lines))
    draft = _review(draft, specs, segments, actor, master, test_defaults=test_defaults)
    expansion, conditions = _validate_review(draft, batch, specs, segments, actor, resolutions)
    gaps = _gaps(specs, segments)
    policy = {"formula_version": FORMULA_VERSION, "rounding_version": POLICY_VERSION,
              "pricing_version": MASTER_VERSION, "currency": "TWD", "discount_rate": _decimal(rate), "tax_rate": "0.05"}
    children = []
    for position, (line, spec, resolved) in enumerate(zip(draft.lines, specs, resolutions), 1):
        child_id = uuid4().hex
        raw_amounts = {}
        calc = _calculation(line.configuration, resolved, line.line_id, child_id, float(rate), 0.05,
                            repository=master, evidence_sink=raw_amounts.update)
        selections, rows = [], []
        for index, item in enumerate(calc["items"], 1):
            path = item["path"]
            configuration = canonical(_fixed(resolved["selections"][path]))
            line_qty = _decimal(item["line_qty"])
            selections.append(DevelopmentSelection(path, item["spc_code"], line_qty, configuration))
            quantity_evidence = {"version": MASTER_VERSION, "master_digest": master.digest,
                "input": _fixed(calc["cost_inputs"][index - 1]), "driver_path": item["driver_path"],
                "driver_code": item["driver_code"], "rules": master.get_quantity_rules(path, workgroup="103"),
                "product_qty": spec["product_qty"], "line_qty": line_qty,
                "zero_cost_reason": "合成尺寸/含價工序；不代表正式免費" if item["source"] == "zero_cost" else None}
            rows.append(DevelopmentCostRow(f"{index:05d}", path, item["part_code"], item["part_desc"],
                item["optno"], item["optdesc"], item["spc_code"], item["spdsc"], item["unit"], spec["product_qty"],
                line_qty, _decimal(item["qty"]), _decimal(item["qty"]), _decimal(item["stdqty"]), _decimal(item["stdpar"]),
                _decimal(item["compri"]), item["part_cost"], item["unit_cost"], item["unit_price"], item["amount"],
                item["source"], canonical(quantity_evidence), canonical({"version": MASTER_VERSION,
                    "master_digest": master.digest, "synthetic_only": True, "category": master.document["category"],
                    "option": master.catalog["options"][path], "warning": WARNING}), configuration))
        with localcontext() as ctx:
            ctx.prec = 100
            tax_difference = _decimal(Decimal(calc["total_price"]) - Decimal(calc["after_discount"]) - Decimal(calc["tax_amount"]))
        amounts = DevelopmentAmounts(**{key: calc[key] for key in MONEY_FIELDS}, quote_rate=calc["quote_rate"],
            discount_rate=policy["discount_rate"], tax_rate=policy["tax_rate"],
            raw_amounts_json=canonical({key: _decimal(value) for key, value in raw_amounts.items()}),
            cost_display_difference=calc["rounding_differences"]["display_cost_rows_minus_total_cost"], tax_rounding_difference=tax_difference)
        target = spec["source_item_id"]
        children.append(DevelopmentAtomicChild(child_id, target, line.line_id, spec["source_line_no"], position,
            spec["product_qty"], revision, PRODUCT, line.label, spec["raw"],
            "\n".join(l.raw for l in batch.lines if l.number in shared_lines), tuple(selections), tuple(rows), amounts,
            canonical({"source": spec, "unit": "cm", "axes": "depth,width", "tolerance_cm": "0.1", "synthetic_only": True}),
            canonical({"records": _fixed([asdict(r) for r in draft.requirements if target in r.targets or r.disposition == "metadata"]),
                "dispositions": [s for s in segments if target in s["targets"] or s["disposition"] == "metadata"],
                "assumptions": _assumptions(), "formal_gaps": gaps, "formal_blockers": expansion["children"][position - 1]["formal_blockers"], "warning": WARNING}),
            canonical({"records": _fixed([asdict(c) for c in draft.conditions if target in c.targets]),
                       "issues": [i for i in conditions["issues"] if i["line_id"] == target], "formal_approved": False}),
            canonical({"status": "SYNTHETIC_POSITION_ONLY", "reference": MASTER_VERSION + ":position",
                       "formal_drawing": None, "geometry_verified": False, "warning": WARNING})))
    all_ids = tuple(s["source_item_id"] for s in specs)
    items_by_no = {s["source_line_no"]: s for s in specs}
    source_lines = []
    for number, value in enumerate(raw.split("\n"), 1):
        spec = items_by_no.get(number)
        kind = "item" if spec else "shared" if number in shared_lines else "context" if value.strip() else "blank"
        source_lines.append(DevelopmentSourceLine(number, value, kind, spec["source_item_id"] if spec else None,
            (spec["source_item_id"],) if spec else all_ids if kind == "shared" else ()))
    context = {"raw_text": raw, "actor": actor, "accepted_assumptions": sorted(accepted_assumptions),
               "source_confirmed": source_confirmed, "discount_rate": policy["discount_rate"], "revision": revision,
               "predecessor": predecessor, "business_version": TEST_VERSION if test_defaults else VERSION, "master_version": MASTER_VERSION,
               "master_digest": master.digest}
    history = {"input_context": context, "input_digest": _digest(canonical(context)),
               "created_at": datetime.now(timezone.utc).isoformat(), "assumptions": _assumptions(),
               "formal_gaps": gaps, "warning": WARNING, "formal_approved": False,
               "source_digest": batch.source_digest, "parser_version": batch.parser_version,
               "source_validation": {"expansion_digest": expansion["content_digest"], "coverage": expansion["coverage"]},
               "requirements": _fixed([asdict(r) for r in draft.requirements]),
               "formal_blockers": expansion["formal_blockers"], "can_quote": False, "can_confirm": False, "can_save": False}
    source = DevelopmentSource(batch.batch_id, batch.orders[0].order_id, batch.orders[0].source_ref_raw,
        VERSION, batch.parser_version, raw, tuple(source_lines), canonical(history))
    request = DevelopmentAtomicRequest("103", uuid4().hex, uuid4().hex, uuid4().hex, uuid4().hex, revision,
        VERSION, source, tuple(children), canonical({key: _sum(getattr(c.amounts, key) for c in children) for key in MONEY_FIELDS}), canonical(policy))
    validate_request(request)
    return request
