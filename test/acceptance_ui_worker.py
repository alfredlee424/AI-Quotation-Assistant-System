"""AppTest 專用隔離子程序；成功路徑不替換業務答案。"""
import hashlib
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0]))
        if any(part.startswith(".env") for part in path.parts) or path.is_relative_to(ROOT / "logs"):
            raise AssertionError("禁止私人設定或真工單")
    if event in {"socket.connect", "socket.bind", "socket.sendto", "socket.getaddrinfo"}:
        raise AssertionError("AppTest 不需要網路")
    if event == "import" and args[0].split(".")[0] in {"app", "openai"}:
        raise AssertionError("禁止正式入口或模型")


sys.addaudithook(audit)
from acceptance.bootstrap import initialize_directory, isolate_process, verify_directory

directory = (initialize_directory(sys.argv[2]) if sys.argv[1] == "initialize"
             else verify_directory(isolate_process(sys.argv[2])))
os.chdir(directory)

from contextlib import closing
from dataclasses import asdict, is_dataclass
import sqlite3
import streamlit as st
from streamlit.testing.v1 import AppTest
from acceptance import business
from acceptance.workflow import Workflow

# 觀察真實呼叫，委派原始函式；可在完成一次計算後鎖死以偵測隱式查價。
counts = {"build": 0, "master": 0, "calculation": 0, "price_select": 0}
no_pricing = False
original_build = business.build_request
original_master = business._Master
from engine import quote_batch_trial
original_calculation = quote_batch_trial._calculation


def watched(name, original):
    def call(*args, **kwargs):
        counts[name] += 1
        if no_pricing:
            raise AssertionError("不允許重新查價")
        return original(*args, **kwargs)
    return call


business.build_request = watched("build", original_build)
business._Master = watched("master", original_master)
quote_batch_trial._calculation = watched("calculation", original_calculation)
original_connect = sqlite3.connect


def traced_connect(*args, **kwargs):
    db = original_connect(*args, **kwargs)
    def trace(sql):
        if sql.lstrip().upper().startswith("SELECT") and "FROM acceptance_synthetic_catalog" in sql:
            counts["price_select"] += 1
    db.set_trace_callback(trace)
    return db


sqlite3.connect = sqlite3.dbapi2.connect = traced_connect

# AppTest 尚未提供 download_button 點擊 API；擷取傳給真正元件的 bytes，仍完整渲染元件。
downloads = {}
original_download = st.download_button


def capture_download(label, data, file_name=None, mime=None, **kwargs):
    downloads[kwargs["key"]] = {"data": data, "file_name": file_name, "mime": mime}
    return original_download(label, data, file_name, mime, **kwargs)


st.download_button = capture_download


def app_start():
    return AppTest.from_file(str(ROOT / "acceptance/ui.py"), default_timeout=30).run()


def clean(app):
    assert not app.exception, str(app.exception)
    assert not app.error, [error.value for error in app.error]
    return app


def click(app, key):
    assert not app.button(key=key).disabled, key
    app.button(key=key).click().run()
    return clean(app)


def initialized():
    app = clean(app_start())
    assert not tables("master.sqlite")
    assert not tables("workflow.sqlite")
    assert app.text_area(key="raw").value == ""
    assert not app.checkbox
    assert {button.key for button in main_elements(app, "button")} == {"create"}
    assert {field.key for field in app.text_input} == {"discount_rate"}
    return app


def prepared(app, raw=None):
    # 明確輸入來源，不用 session_state 預製試算／保存文件。
    app.text_area(key="raw").set_value(business.SAMPLE if raw is None else raw).run()
    app.text_input(key="discount_rate").set_value("0.05").run()
    clean(app)
    return app


def created(app):
    click(app, "create")
    assert counts == {"build": 1, "master": 1, "calculation": 4, "price_select": 1}, counts
    return current(app)


def current(app):
    workflow = Workflow(directory)
    try:
        return workflow.retrieve(app.session_state.active_batch)
    finally:
        workflow.close()


def history(app, batch_id):
    app.selectbox(key="history_batch").select(batch_id).run()
    click(app, "load_history")
    assert app.session_state.active_batch == batch_id
    assert app.text_area(key="raw").value == current(app)["context"].raw_text
    return current(app)


def tables(name):
    with closing(sqlite3.connect((directory / name).as_uri() + "?mode=ro", uri=True)) as db:
        return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def snapshot_counts():
    with closing(sqlite3.connect((directory / "snapshot.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        return {table: db.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]
                for table in tables("snapshot.sqlite")}


def main_elements(app, kind):
    """只取技術區以外的真實元件，不用 app.text 把收合內容當主畫面。"""
    def walk(node):
        if node.type == "expander":
            assert node.label.startswith("進階｜")
            assert node.proto.expanded is False
            return
        if node.type == kind:
            yield node
        for child in getattr(node, "children", {}).values():
            yield from walk(child)
    return list(walk(app.main))


def verified_tables(app):
    view = current(app)
    assert not main_elements(app, "json")
    assert not main_elements(app, "download_button")
    frames = [element.value for element in main_elements(app, "arrow_data_frame")]
    # AppTest Dataframe 的 type 可能與 protobuf 欄位名不同，禁止默默略過。
    if not frames:
        frames = [element.value for element in main_elements(app, "dataframe")]
    cost = [frame for frame in frames if "成本序號" in frame.columns]
    assert len(cost) == 4
    column_map = {"成本序號": "seq_no", "部位名稱": "part_desc", "完整路徑": "path",
                  "規格代碼": "spc_code", "規格說明": "spdsc", "單位": "unit",
                  "每件項目數": "line_qty", "本張總項目數": "qty", "用料量": "stdqty",
                  "裁切量": "stdpar", "採購單價": "compri", "明細成本": "part_cost"}
    visible = "\n".join(element.value for element in main_elements(app, "text"))
    children = view["document"]["children"]
    overview = next(frame for frame in frames if "驗收單號" in frame.columns)
    tabs = main_elements(app, "tab")
    assert len(tabs) == 4
    for tab in tabs:
        first = next(iter(tab.children.values()))
        assert first.type == "text"
        assert first.value == "規格／成本明細（合成價格；保留零價結構與含價工序）"
    for index, (child, frame) in enumerate(zip(children, cost, strict=True)):
        fixed = child["fixed_child"]
        assert frame.to_dict("records") == [
            {label: str(row[key]) if row[key] != "" else "（空白）" for label, key in column_map.items()}
            for row in fixed["rows"]]
        assert fixed["description"] not in visible and fixed["shared_description"] not in visible
        assert overview.iloc[index]["驗收單號"] == child["ref_no"]
        assert overview.iloc[index]["成品數"] == str(fixed["product_qty"])
        assert len(frame) == len(fixed["rows"])
        assert any(row["compri"] == "0" for row in fixed["rows"])
    assert all("條件" not in frame.columns for frame in frames)
    assert view["id"] not in visible and view["preview_digest"] not in visible
    assert "快照保存時間：" not in visible
    for receipt in view["confirmations"]:
        assert receipt["token"] not in visible
    assert all(button.key in {"save", "create"} for button in main_elements(app, "button"))
    assert not app.checkbox
    return view


def verified_downloads(app):
    view = verified_tables(app)
    assert view["saved"] is not None
    saved = view["saved"]
    children = view["document"]["children"]
    assert [c["suffix"] for c in children] == ["001", "002", "003", "004"]
    numbers = [c["ref_no"] for c in children]
    assert len({number.rsplit("_", 1)[0] for number in numbers}) == 1
    assert [c["product_qty"] for c in saved["fixed_request"]["children"]] == [2, 1, 6, 2]
    assert view["document"]["fixed_request"] == saved["fixed_request"]
    visible = "\n".join(element.value for element in app.text)
    fingerprints = {}
    for child in children:
        number = child["ref_no"]
        raw_json = downloads["json_" + number]["data"]
        text = downloads["text_" + number]["data"]
        data = json.loads(raw_json)
        assert data["fixed_child"] == child["fixed_child"]
        assert data["source"] == saved["fixed_request"]["source"]
        assert data["policy"] == json.loads(saved["fixed_request"]["policy_json"])
        assert data["snapshot_created_at"] == saved["created_at"]
        assert not data["formal_approved"] and data["synthetic_only"] and not data["importable"]
        assert not data["can_quote"] and not data["can_save"]
        assert len(data["source_history"]["formal_gaps"]) == 8
        assert all(not gap["formal_approved"] for gap in data["source_history"]["formal_gaps"])
        assert text in visible  # 原完整文字仍保留在收合技術區，主畫面另逐欄驗證。
        for source in ("單面置物版", "孔靠單/格靠單", "817胡/噴胡", "腳粒鎖好/撕保護膜"):
            assert source in text and source in data["fixed_child"]["shared_description"]
        fingerprints[number] = [hashlib.sha256(value.encode()).hexdigest() for value in (raw_json, text)]
    first = children[0]["fixed_child"]
    assert any(selection["path"].endswith("\\WIRE") for selection in first["selections"])
    assert not any(selection["path"].endswith(("\\HOLE", "\\BOX")) for selection in first["selections"])
    assert [json.loads(c["fixed_child"]["dimensions_json"])["source"]["size_code"]
            for c in children] == ["G7070", "S70120", "S70140", "C70140"]
    for child in children[1:]:
        paths = {selection["path"].rsplit("\\", 1)[-1] for selection in child["fixed_child"]["selections"]}
        assert {"HOLE", "BOX"} <= paths and "WIRE" not in paths
    from decimal import Decimal
    for key, total in json.loads(saved["fixed_request"]["totals_json"]).items():
        assert Decimal(total) == sum(Decimal(c["fixed_child"]["amounts"][key]) for c in children)
    for child in saved["fixed_request"]["children"]:
        assert child["rows"][0]["seq_no"] == "00001"
    assert len(app.get("download_button")) >= 8
    return {"batch_id": view["id"], "digest": saved["final_digest"], "downloads": fingerprints,
            "snapshot_counts": snapshot_counts()}


def emit(value):
    print(json.dumps(value, ensure_ascii=False, default=lambda item: asdict(item) if is_dataclass(item) else item))


exec(compile(sys.stdin.read(), "<synthetic-acceptance-ui-test>", "exec"))
