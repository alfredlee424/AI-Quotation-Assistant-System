"""真解析到 typed request；隔離 hook 僅在子程序，不污染 pytest 全套。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import textwrap

import pytest

from acceptance.business import ASSUMPTIONS, SAMPLE, inspect_source


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv" / "bin" / "python"

# 明確保留舊來源與全部負例；新版 SAMPLE 不得讓 replace 退化成未修改輸入。
LEGACY_SAMPLE = "\n".join((
    "環式會議桌生產工單", "20990101-1 星河合成辦公有限公司",
    "彎角70X70削角面無孔有桌下走線-----2",
    "平直70X120面1孔13.5X40單掀鋁-----1",
    "平直70X140面1孔13.5X40單掀鋁-----6",
    "平彎70X140面1孔13.5X40單掀鋁-----2",
    "單面置物版", "孔靠單/格靠單/817胡/噴胡", "腳粒鎖好/撕保護膜",
))


def worker(tmp_path, body):
    code = '''
import json, sqlite3, sys, socket, hashlib
from pathlib import Path
from contextlib import closing
from dataclasses import asdict, replace
from decimal import Decimal
from acceptance.bootstrap import initialize_directory, verify_directory
directory = initialize_directory(sys.argv[1])
from acceptance import business as b
from engine.development_atomic_contract import validate_request, decode_request
def forbidden(*args, **kwargs):
    raise AssertionError("不得查正式主檔或連線")
socket.socket.connect = forbidden
socket.create_connection = forbidden
from database import repository
repository.get_product_category = forbidden
repository.get_quantity_rules = forbidden
repository.expand_ordstr_tree = forbidden
repository.get_options_by_path = forbidden
def build(**kwargs):
    args = dict(actor="合成操作人", accepted_assumptions=[a["id"] for a in b.ASSUMPTIONS], source_confirmed=True)
    args.update(kwargs)
    return b.build_request(directory, args.pop("raw", b.SAMPLE), **args)
def rejected(call):
    try:
        call()
    except (ValueError, TypeError) as exc:
        return str(exc)
    raise AssertionError("應整批 fail closed")
'''
    result = subprocess.run([str(PYTHON), "-c", textwrap.dedent(code) + "\n" + textwrap.dedent(body),
                             str(tmp_path / "private")], cwd=ROOT,
                            env={"PATH": "/usr/bin:/bin", "PYTHON_DOTENV_DISABLED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_inspect_source_without_price_lookup(monkeypatch):
    from database import repository
    def forbidden(*args, **kwargs):
        pytest.fail("inspect_source 不得查價")
    monkeypatch.setattr(repository, "get_product_category", forbidden)
    monkeypatch.setattr(repository, "get_options_by_path", forbidden)
    result = inspect_source(SAMPLE)
    assert [s["product_qty"] for s in result["children"]] == [2, 1, 6, 2]
    assert [s["width"] for s in result["children"]] == [70, 120, 140, 140]
    assert [s["shape"] for s in result["children"]] == ["彎角", "平直", "平直", "平彎"]
    assert result["children"][0]["no_hole"]
    assert not any(s["no_hole"] for s in result["children"][1:])
    assert {s["disposition"] for s in result["dispositions"]} == {"metadata", "condition", "specification", "included_process"}
    assert len(result["formal_gaps"]) == len(ASSUMPTIONS) == 8
    assert not result["can_quote"] and not result["can_save"]
    assert all(g["sources"] and g["required_approver"] and not g["formal_approved"] for g in result["formal_gaps"])


@pytest.mark.parametrize("raw", [
    None, {}, {"document_type": "independent_quote_batch_internal_trial_only"},
    LEGACY_SAMPLE + "\n未知施工規格", LEGACY_SAMPLE + "\n面無孔", LEGACY_SAMPLE + "\n無置物板",
    LEGACY_SAMPLE.replace("單面置物版\n", ""), LEGACY_SAMPLE + "\n單面置物板",
    LEGACY_SAMPLE.replace("/格靠單", ""), LEGACY_SAMPLE.replace("/撕保護膜", ""),
    LEGACY_SAMPLE.replace("平直70X120面1孔13.5X40單掀鋁-----1\n", ""),
    LEGACY_SAMPLE + "\n平直70X120面1孔13.5X40單掀鋁-----1",
    LEGACY_SAMPLE.replace("平直70X120", "平直70X140"),
    LEGACY_SAMPLE.replace("70X70", "70X80"), LEGACY_SAMPLE.replace("-----6", "-----5"),
    LEGACY_SAMPLE.replace("-----2", "-----2.0", 1), LEGACY_SAMPLE.replace("-----2", "-----0", 1),
    LEGACY_SAMPLE.replace("面無孔有桌下走線", "面1孔13.5X40單掀鋁"),
    LEGACY_SAMPLE.replace("面無孔有桌下走線", "面無孔有桌下走線面1孔"),
    LEGACY_SAMPLE.replace("13.5X40", "13.5X45"), LEGACY_SAMPLE.replace("/噴胡", "/噴黑"),
    LEGACY_SAMPLE.replace("有限公司", "有限公司面無孔"),
    LEGACY_SAMPLE.replace("生產工單", "生產工單新增未知條件"),
    LEGACY_SAMPLE.replace("\n", "\r\n"), LEGACY_SAMPLE + "\x00",
])
def test_invalid_source_fails_closed(raw):
    assert raw != LEGACY_SAMPLE
    with pytest.raises(ValueError):
        inspect_source(raw)


def test_source_not_fixture_copy_and_complete_spans():
    raw = SAMPLE.replace("星河合成", "晴空示範家具公司").replace("20990101-1", "20990202-42")
    raw = raw.replace("70X", "70×").replace("單面置物版", "單面置物板").replace("/", "／") + "\n\n"
    result = inspect_source(raw)
    assert result["raw_text"] == raw and result["original_order_no"] == "20990202-42"
    for number, line in enumerate(raw.split("\n"), 1):
        segments = [s for s in result["dispositions"] if s["source_line_no"] == number]
        assert "".join(s["text"] for s in segments) == line
        assert all(line[s["start"]:s["end"]] == s["text"] for s in segments)


@pytest.mark.parametrize("header", [
    "20990202-42 晴空合成", "(12)20990202-42 晴空合成",
    "（12）209902021-42晴空合成", "209902021-42 晴空示範家具公司",
    "(3)209902021-42 星河合成辦公有限公司", "20990202-42 DemoClient",
])
@pytest.mark.parametrize("section", ["", "環式會議桌生產工單\n"])
def test_nine_line_source_header_and_optional_section_keep_original(header, section):
    from agent.work_orders import parse_work_orders
    raw = section + header + "\n" + SAMPLE.split("\n", 1)[1]
    original = parse_work_orders(raw)
    result = inspect_source(raw)
    assert result["raw_text"] == raw
    assert result["original_order_no"] == original.orders[0].source_ref_raw
    assert original.orders[0].family_hint == ("環式會議桌" if section else "")
    assert len(raw.splitlines()) == (10 if section else 9)
    assert [s["source_line_no"] for s in result["children"]] == list(range(3 if section else 2, 7 if section else 6))
    assert result["shared_line_numbers"] == list(range(7 if section else 6, 11 if section else 10))
    assert [item.description_raw for item in original.orders[0].items] == [
        "彎角70X70削角", "平直70X120", "平直70X140", "平彎70X140"]
    assert [item.suffix_raw.strip() for item in original.orders[0].items] == [
        "面無孔有桌下走線", *["面1孔13.5X40單掀鋁"] * 3]
    for number, line in enumerate(raw.splitlines(), 1):
        segments = [s for s in result["dispositions"] if s["source_line_no"] == number]
        cursor = 0
        for segment in segments:
            assert segment["start"] == cursor
            assert segment["text"] == line[cursor:segment["end"]]
            cursor = segment["end"]
        assert cursor == len(line)
        assert "".join(s["text"] for s in segments) == line
    for child in result["children"]:
        segments = [s for s in result["dispositions"] if s["source_line_no"] == child["source_line_no"]]
        face = "面無孔" if child["no_hole"] else "面1孔"
        assert next(s for s in segments if s["text"].startswith(face))["start"] == child["raw"].index(face)
        assert any("-----" + child["quantity_raw"] in s["text"] for s in segments)


@pytest.mark.parametrize("old,new", [
    ("有桌下走線", ""), ("有桌下走線", "不要桌下走線"),
    ("有桌下走線", "有桌下走線新增未知條件"),
    ("面無孔", "面1孔13.5X40單掀鋁"),
    ("面1孔13.5X40單掀鋁", "面無孔有桌下走線"),
    ("面1孔13.5X40單掀鋁", "面2孔13.5X40單掀鋁"),
    ("面1孔13.5X40單掀鋁", "面1孔13.5X40單掀鋁加厚"),
    ("面1孔13.5X40單掀鋁", ""), ("13.5X40", "13.5X45"),
    ("平直70X120", "平直70X140"), ("平直70X120", "平彎70X120"),
    ("70X70", "70X80"), ("削角", ""), ("-----6", "-----5"),
    ("-----2", "-----2.0"), ("-----2", "-----0"), ("-----2", "-----2e1"),
    ("-----1 面1孔", "面1-----1 孔"),
    ("-----2 面無孔有桌下走線", "面無-----2 孔有桌下走線"),
    ("-----6", "未知需求-----6"), ("單面置物版\n", ""),
    ("/格靠單", ""), ("817胡", "818胡"), ("噴胡", "噴黑"),
    ("/撕保護膜", ""), ("孔靠單", "不要孔靠單"),
    ("單面置物版", "單面置物版/單面置物板"),
    ("腳粒鎖好", "腳粒鎖好/未知需求"),
])
def test_nine_line_unknown_missing_conflicting_or_wrong_quantity_rejected(old, new):
    raw = SAMPLE.replace(old, new)
    assert raw != SAMPLE
    with pytest.raises(ValueError):
        inspect_source(raw)


@pytest.mark.parametrize("header", [
    "20990202-42", "20990202-42 星", "20990202-42 " + "星" * 25,
    "20990202-42 星河/合成", "20990202-42 星河 合成", "20990202-42 星河_合成",
    "20990202-42 星河合成加厚", "20990202-42 星河合成公司面1孔",
    "20990202-42 星河合成未知需求", "20990202-42 星河合成新增施工",
    "20990202-42 星河合成13.5X40", "20990202-42 星河合成817胡",
    "20990202-42 星河合成孔靠單", "20990202-42 星河合成/免費",
    "20990202-42 星河合成不要走線", "20990202-42 星河合成噴胡",
    "20990202-42 星河合成規格", "20990202-42 星河合成材質",
    "20990202-42 星河合成撕保護膜", "20990202-42 星河合成格靠單",
    "2099020-42 星河合成", "2099020201-42 星河合成", "(A)20990202-42 星河合成",
])
def test_nine_line_header_is_not_a_requirement_escape_hatch(header):
    with pytest.raises(ValueError):
        inspect_source(header + "\n" + SAMPLE.split("\n", 1)[1])


@pytest.mark.parametrize("raw", [
    SAMPLE + "\n未知施工規格", SAMPLE + "\n" + SAMPLE.splitlines()[2],
    "\n".join(SAMPLE.splitlines()[:2] + SAMPLE.splitlines()[3:]),
    "餐桌生產工單\n" + SAMPLE,
    "環式會議桌生產工單新增未知條件\n" + SAMPLE,
    "環式會議桌生產工單\n環式會議桌生產工單\n" + SAMPLE,
])
def test_nine_line_count_and_section_whitelist_remain_strict(raw):
    with pytest.raises(ValueError):
        inspect_source(raw)


def test_legacy_source_still_parses_without_rewriting():
    result = inspect_source(LEGACY_SAMPLE)
    assert result["raw_text"] == LEGACY_SAMPLE
    assert result["shared_line_numbers"] == [7, 8, 9]
    assert [s["product_qty"] for s in result["children"]] == [2, 1, 6, 2]
    for number, line in enumerate(LEGACY_SAMPLE.splitlines(), 1):
        assert "".join(s["text"] for s in result["dispositions"] if s["source_line_no"] == number) == line


def test_missing_product_category_is_real_gap_and_explicit_synthetic_setting():
    from acceptance import business
    from acceptance.workflow import InputContext, Workflow
    result = inspect_source(SAMPLE)
    structure = next(g for g in result["formal_gaps"] if g["id"] == "structure")
    assert "正式產品分類未提供或未核准" in structure["missing"]
    assert not structure["formal_approved"] and structure["formal_reference"] is None
    setting = next(a for a in result["assumptions"] if a["id"] == "structure")
    assert "明確選用合成產品分類SYNTH4" in setting["setting"]
    assert len(result["assumptions"]) == len(result["formal_gaps"]) == 8
    context = InputContext(SAMPLE, "合成操作人", (), False)
    assert context.business_version == business.VERSION
    assert context.master_version == business.MASTER_VERSION
    assert context == Workflow.context(SAMPLE, actor=context.actor, accepted_assumptions=[], source_confirmed=False)
    assert business._master_document()["assumptions"] == result["assumptions"]


@pytest.mark.parametrize("raw", [LEGACY_SAMPLE, SAMPLE.replace("有桌下走線", "桌下走線"),
    SAMPLE.replace("-----2 面無孔有桌下走線", "面無孔-----2 有桌下走線")])
def test_legacy_and_nine_line_wiring_wording_build_real_request(tmp_path, raw):
    worker(tmp_path, f'''
b.initialize_master(directory)
r = build(raw={raw!r})
assert r.source.raw_text == {raw!r}
assert [c.amounts.total_cost for c in r.children] == ["1822.40", "1275.20", "8282.40", "2888.80"]
assert all(s.path.split(chr(92))[0] == b.PRODUCT for c in r.children for s in c.selections)
validate_request(r)
''')


def test_core_evidence_is_captured_in_the_same_calculation(tmp_path):
    worker(tmp_path, '''
b.initialize_master(directory)
import engine.quote_batch_trial as trial
original = trial.calculate_quote
captured = []
def recording(*args, **kwargs):
    sink = kwargs.pop("evidence_sink")
    def observe(raw):
        captured.append(dict(raw))
        sink(raw)
    return original(*args, **kwargs, evidence_sink=observe)
trial.calculate_quote = recording
r = build(discount_rate="0.05")
assert len(captured) == 4
for child, raw in zip(r.children, captured):
    assert json.loads(child.amounts.raw_amounts_json) == {k: b._decimal(v) for k, v in raw.items()}
assert not hasattr(b, "_raw_evidence")  # builder 不得複製核心公式
''')


def test_evidence_sink_cannot_change_core_result(tmp_path):
    worker(tmp_path, '''
from engine.calculator import QuoteItem, calculate_quote
items = [QuoteItem("S", "合成", "SYNTH4\\\\BOARD", "S", "合成", 2, 3, 7, 19)]
expected = asdict(calculate_quote(items, discount_rate=0.05))
actual = calculate_quote(items, discount_rate=0.05, evidence_sink=lambda raw: raw.update(total_price=-1))
assert asdict(actual) == expected
''')


def test_real_build_costs_evidence_and_readonly_master(tmp_path):
    worker(tmp_path, '''
b.initialize_master(directory)
before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in directory.iterdir()}
r = build()
assert decode_request(validate_request(r)) == r
assert len(r.children) == 4 and sum(c.product_qty for c in r.children) == 11
assert [c.amounts.total_cost for c in r.children] == ["1822.40", "1275.20", "8282.40", "2888.80"]
for index, c in enumerate(r.children):
    assert c.description == b.SAMPLE.splitlines()[index + 1]
    assert c.shared_description == "\\n".join(b.SAMPLE.splitlines()[5:])
    rows = {row.path: row for row in c.rows}
    assert (b._path("HOLE") in rows) == (index != 0)
    assert (b._path("BOX") in rows) == (index != 0)
    assert (b._path("WIRE") in rows) == (index == 0)
    assert rows[b._path("FEET")].line_qty == "4.0"
    assert rows[b._path("LOCK")].part_cost == "0.00"
    assert rows[b._path("PEEL")].part_cost == "0.00"
    assert rows[b._path("HOLE")].compri == "80.0" if index else True
    assert [row.seq_no for row in c.rows] == [f"{i:05d}" for i in range(1, len(c.rows)+1)]
    assert [row.path for row in c.rows] == [s.path for s in c.selections]
    reqs = json.loads(c.requirements_json)
    assert all(g["status"] == "FORMAL_APPROVAL_MISSING" for g in reqs["formal_gaps"])
    assert reqs["formal_blockers"] and not json.loads(c.drawings_json)["geometry_verified"]
    assert "production_note" not in {s["disposition"] for s in reqs["dispositions"]}
    for row in c.rows:
        evidence = json.loads(row.quantity_evidence_json)
        assert evidence["master_digest"]
        assert evidence["input"]["qty"] == row.qty
        if Decimal(row.compri):
            assert row.quantity_source == "ordqty" and evidence["rules"]
            assert evidence["driver_code"] == ["G7070", "S70120", "S70140", "C70140"][index]
        assert json.loads(row.price_evidence_json)["synthetic_only"]
assert len({c.selections for c in r.children}) == 4
history = json.loads(r.source.history_json)
assert history["input_context"]["raw_text"] == b.SAMPLE
assert history["input_digest"] == b._digest(b._json(history["input_context"]))
assert not history["can_quote"] and not history["formal_approved"]
assert {p.name: (p.stat().st_ino, p.read_bytes()) for p in directory.iterdir()} == before
verify_directory(directory)
''')


def test_initialization_is_explicit_and_never_overwrites(tmp_path):
    worker(tmp_path, '''
assert "尚未明確載入" in rejected(build)
manifest = (directory / "manifest.json").read_bytes()
b.initialize_master(directory)
before = (directory / "master.sqlite").read_bytes()
assert "不覆寫" in rejected(lambda: b.initialize_master(directory))
assert (directory / "master.sqlite").read_bytes() == before
assert (directory / "manifest.json").read_bytes() == manifest
verify_directory(directory)
''')


@pytest.mark.parametrize("mutation", [
    'doc["catalog"]["options"][b._path("BOARD")].clear()',
    'doc["catalog"]["options"][b._path("BOX")][0].pop("compri")',
    'doc["catalog"]["options"][b._path("SHELF")][0]["compri"] = "-1"',
    'doc["quantity_rules"].pop(b._path("SHELF"))',
    'doc["quantity_rules"][b._path("BOARD")][0]["stdpar"] = "0"',
    'doc["quantity_rules"][b._path("BOARD")].pop(2)',
    'doc["category"].pop("quo_rate")',
    'doc["catalog"]["nodes"][b._path("HOLE")]["must_chose"] = "Y"',
])
def test_master_corruption_even_rehashed_never_prices(tmp_path, mutation):
    worker(tmp_path, '''
b.initialize_master(directory)
with closing(sqlite3.connect(directory / "master.sqlite")) as db:
    doc = json.loads(db.execute("SELECT payload FROM acceptance_synthetic_catalog").fetchone()[0])
    MUTATION
    payload = b._json(doc)
    db.execute("UPDATE acceptance_synthetic_catalog SET payload=?, digest=?", (payload, b._digest(payload)))
    db.commit()
from engine import quote_batch_trial
quote_batch_trial._calculation = forbidden
assert "合成主檔" in rejected(build)
'''.replace("MUTATION", mutation))


def test_context_assumptions_and_new_identities(tmp_path):
    worker(tmp_path, '''
b.initialize_master(directory)
for kwargs in (
    {"source_confirmed": False}, {"source_confirmed": 1}, {"actor": " "},
    {"accepted_assumptions": []}, {"accepted_assumptions": ["dimensions"] * 8},
    {"accepted_assumptions": {a["id"]: True for a in b.ASSUMPTIONS}},
    {"accepted_assumptions": [a["id"] for a in b.ASSUMPTIONS] + ["unknown"]},
    {"discount_rate": "0.11"}, {"discount_rate": 0.05}, {"discount_rate": "NaN"},
    {"discount_rate": "-0"}, {"discount_rate": "1e-2"},
    {"revision": 1}, {"revision": True}, {"predecessor": "a" * 32},
    {"raw": {}}, {"raw": {"document_type": "review"}},
):
    rejected(lambda: build(**kwargs))
first = build()
second = build(revision=1, predecessor=first.request_id, actor="新操作人", discount_rate="0.05")
third = build()
assert len({getattr(r, k) for r in (first, second, third) for k in ("batch_id", "request_id", "preview_identity", "save_id")}) == 12
assert not {c.child_quote_id for c in first.children} & {c.child_quote_id for c in third.children}
history = json.loads(second.source.history_json)
assert history["input_context"]["predecessor"] == first.request_id
assert history["input_context"]["actor"] == "新操作人"
assert history["input_digest"] != json.loads(first.source.history_json)["input_digest"]
assert {c.amounts.discount_rate for c in second.children} == {"0.05"}
assert "\\n".join(line.text for line in second.source.lines) == b.SAMPLE
''')


def test_missing_ledger_and_conflicting_condition_never_prices(tmp_path):
    worker(tmp_path, '''
b.initialize_master(directory)
original = b._review
def missing(*args, **kwargs):
    draft = original(*args, **kwargs)
    return replace(draft, requirements=draft.requirements[:-1])
b._review = missing
rejected(build)
def conflict(*args, **kwargs):
    draft = original(*args, **kwargs)
    first = draft.conditions[0]
    opposite = replace(first, condition_id="f" * 32, state="REQUIRED")
    return replace(draft, conditions=(*draft.conditions, opposite))
b._review = conflict
assert "條件" in rejected(build)
def stale(*args, **kwargs):
    draft = original(*args, **kwargs)
    return replace(draft, requirements=(replace(draft.requirements[0], status="UNREVIEWED"), *draft.requirements[1:]))
b._review = stale
assert "需求" in rejected(build)
''')


def test_independent_rounding_matches_core_and_decimal_totals(tmp_path):
    worker(tmp_path, '''
b.initialize_master(directory)
from engine.calculator import QuoteItem, calculate_quote
for discount in ("0", "0.05", "0.1"):
    r = build(discount_rate=discount)
    totals = json.loads(r.totals_json)
    for child in r.children:
        inputs = []
        for row in child.rows:
            data = json.loads(row.quantity_evidence_json)["input"]
            for key in ("qty", "stdqty", "stdpar", "compri"):
                data[key] = float(data[key])
            inputs.append(QuoteItem(**data))
        expected = calculate_quote(inputs, discount_rate=float(discount), markup_rate=0.3, tax_rate=0.05)
        for key in totals:
            assert getattr(child.amounts, key) == format(Decimal(str(getattr(expected, key))), ".2f")
        assert Decimal(child.amounts.tax_rounding_difference) == Decimal(child.amounts.total_price) - Decimal(child.amounts.after_discount) - Decimal(child.amounts.tax_amount)
    for key in totals:
        assert Decimal(totals[key]) == sum(Decimal(getattr(c.amounts, key)) for c in r.children)
''')


def test_trailing_blank_lines_and_company_are_preserved(tmp_path):
    worker(tmp_path, '''
b.initialize_master(directory)
raw = b.SAMPLE.replace("星河合成", "海岸示範公司") + "\\n\\n"
r = build(raw=raw)
assert r.source.raw_text == raw
assert r.source.lines[-1].kind == r.source.lines[-2].kind == "blank"
assert "海岸示範公司" in r.source.lines[0].text
validate_request(r)
''')


def test_request_can_reserve_save_retry_and_read_after_restart(tmp_path):
    output = worker(tmp_path, '''
b.initialize_master(directory)
r = build(discount_rate="0.05")
from acceptance.bootstrap import snapshot_engine
from engine.development_atomic_contract import build_development_reservation_request, identity_of
from engine.quote_number_reservation import QuoteNumberReservationService
from engine.development_atomic_snapshot import DevelopmentAtomicSnapshotService
engine = snapshot_engine(directory)
registry = QuoteNumberReservationService(engine)
reservation = build_development_reservation_request(r)
reserved = registry.reserve(reservation)
assert registry.reserve(reservation) == reserved
service = DevelopmentAtomicSnapshotService(engine)
saved = service.save(r)
assert service.save(r) == saved == service.read(identity_of(r))
assert [c["suffix"] for c in saved["children"]] == ["001", "002", "003", "004"]
assert len({c["ref_no"].rsplit("_", 1)[0] for c in saved["children"]}) == 1
assert saved["fixed_request"] == json.loads(validate_request(r))
assert not saved["can_quote"] and not saved["can_save"]
with engine.connect() as connection:
    for table, count in (("development_atomic_batch", 1), ("development_atomic_child", 4),
                         ("development_atomic_source_link", 4), ("development_atomic_cost", sum(len(c.rows) for c in r.children))):
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM " + table).scalar_one() == count
engine.dispose()
print(json.dumps({"identity": asdict(identity_of(r)), "digest": saved["final_digest"]}))
''')
    record = json.loads(output)
    code = '''
import json, sys
from acceptance.bootstrap import isolate_process, snapshot_engine
directory = isolate_process(sys.argv[1])
from engine.development_atomic_contract import DevelopmentSnapshotIdentity, decode_request, canonical
from engine.development_atomic_snapshot import DevelopmentAtomicSnapshotService
from acceptance import business
def forbidden(*args, **kwargs):
    raise AssertionError("歷史查回不得重算")
business.build_request = business._Master = forbidden
expected = json.loads(sys.argv[2])
engine = snapshot_engine(directory)
service = DevelopmentAtomicSnapshotService(engine)
saved = service.read(DevelopmentSnapshotIdentity(**expected["identity"]))
assert saved["final_digest"] == expected["digest"]
assert service.save(decode_request(canonical(saved["fixed_request"]))) == saved
assert saved["fixed_request"]["source"]["raw_text"] == business.SAMPLE
engine.dispose()
'''
    result = subprocess.run([str(PYTHON), "-c", textwrap.dedent(code), str(tmp_path / "private"), json.dumps(record)],
                            cwd=ROOT, env={"PATH": "/usr/bin:/bin", "PYTHON_DOTENV_DISABLED": "1"},
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_document_mutual_rejection_is_unchanged(tmp_path):
    worker(tmp_path, '''
b.initialize_master(directory)
r = build()
from engine.development_atomic_contract import build_development_reservation_request
from agent.quote_batch import build_quote_batch
from engine.quote_batch_trial import build_batch_trial
from engine.preview import freeze_preview
inspection = b.inspect_source(b.SAMPLE)
for doc in (inspection, asdict(r), {"document_type": "independent_quote_batch_internal_trial_only"},
            {"document_type": "independent_quote_batch_review_only"}):
    rejected(lambda: build(raw=doc))
    rejected(lambda: validate_request(doc))
    rejected(lambda: build_development_reservation_request(doc))
rejected(lambda: build_quote_batch(r, actor="合成操作人"))
rejected(lambda: build_batch_trial(r, actor="合成操作人"))
rejected(lambda: freeze_preview(asdict(r)))
''')
