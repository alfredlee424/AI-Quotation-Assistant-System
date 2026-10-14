"""實際 UI → 解析/配置/計算 → 配號/核對/保存 → 新程序歷史及下載。"""
import json
from pathlib import Path
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
WORKER = ROOT / "test/acceptance_ui_worker.py"


def worker(directory, body, *, initialize=True):
    result = subprocess.run(
        [str(PYTHON), "-I", "-B", str(WORKER), "initialize" if initialize else "open", str(directory)],
        input=textwrap.dedent(body), text=True, capture_output=True, cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1"}, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SECRET" not in result.stdout + result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def test_real_ui_save_and_another_process_history_downloads(tmp_path):
    directory = tmp_path.resolve() / "private # UI 驗收 ?"
    expected = worker(directory, '''
        app = prepared(initialized())
        assert app.text_area(key="raw").value == business.SAMPLE
        assert len(business.SAMPLE.splitlines()) == 9
        assert "生產工單" not in business.SAMPLE
        view = created(app)
        assert view["document"]["fixed_request"]["source"]["raw_text"] == business.SAMPLE
        assert snapshot_counts()["quote_number_reservation"] == 1
        assert view["stage"] == 0
        verified_tables(app)
        assert not app.button(key="save").disabled
        no_pricing = True
        verified_tables(app)
        verified_tables(app)
        before = dict(counts)
        clean(app.run())
        click(app, "save")
        result = verified_downloads(app)
        history(app, result["batch_id"])
        assert verified_downloads(app) == result
        assert counts == before
        assert result["snapshot_counts"]["development_atomic_batch"] == 1
        assert result["snapshot_counts"]["development_atomic_child"] == 4
        emit(result)
    ''')
    actual = worker(directory, f'''
        no_pricing = True
        app = clean(app_start())
        assert app.text_area(key="raw").value == ""
        history(app, {expected['batch_id']!r})
        assert app.text_area(key="raw").value == business.SAMPLE
        assert current(app)["context"].actor == "本機測試"
        assert app.text_input(key="discount_rate").value == "0.05"
        assert not app.checkbox
        clean(app.run())
        emit(verified_downloads(app))
        assert counts == {{"build": 0, "master": 0, "calculation": 0, "price_select": 0}}
    ''', initialize=False)
    assert actual == expected


def test_initial_blank_sample_and_two_buttons_no_manual_prerequisites(tmp_path):
    worker(tmp_path.resolve() / "private", r'''
        app = initialized()
        assert app.text_input(key="discount_rate").value == "0"
        assert app.button(key="create").disabled
        click(app, "load_sample")
        assert app.text_area(key="raw").value == business.SAMPLE
        assert not app.button(key="create").disabled
        assert all(value == 0 for value in counts.values())
        assert not tables("master.sqlite") and not tables("workflow.sqlite")
        created(app)
        assert {b.key for b in main_elements(app, "button")} == {"create", "save"}
        assert [b.label for b in main_elements(app, "button")] == ["產生四張成本明細", "確認並完成報價（測試存檔）"]
        no_pricing = True
        click(app, "create")  # 同內容雙擊不重算或配第二组號碼。
        view = verified_tables(app)
        assert view["context"].accepted_assumptions == ()
        assert view["context"].source_confirmed is False
        assert view["context"].actor == "本機測試"
        assert not view["confirmations"]
        click(app, "save")
        view = verified_tables(app)
        assert [r["event"] for r in view["confirmations"]] == ["確認並測試保存"]
        assert "已完成測試報價，共4張" in [e.value for e in app.success]
        assert snapshot_counts()["development_atomic_child"] == 4
        assert "ordqdt_ai" not in tables("snapshot.sqlite")
    ''')


@pytest.mark.parametrize("change", ["missing", "dimension", "duplicate", "shared", "conflict", "header"])
def test_invalid_sources_are_parsed_not_four_canned_answers(tmp_path, change):
    worker(tmp_path.resolve() / "private", f'''
        app = initialized()
        raw = business.SAMPLE
        change = {change!r}
        if change == "missing":
            raw = "\\n".join(raw.split("\\n")[:4] + raw.split("\\n")[5:])
        elif change == "dimension":
            raw = raw.replace("70X120", "70X121")
        elif change == "duplicate":
            raw = raw.replace(raw.split("\\n")[4], raw.split("\\n")[3])
        elif change == "shared":
            raw = raw.replace("腳粒鎖好/撕保護膜", "腳粒鎖好")
        elif change == "header":
            raw = "\\n".join(raw.split("\\n")[1:])
        else:
            raw = raw.replace("面無孔有桌下走線", "面1孔13.5X40單掀鋁")
        assert raw != business.SAMPLE
        app.text_area(key="raw").set_value(raw).run()
        app.button(key="create").click().run()
        assert app.error and not app.exception
        if change == "header":
            assert "完整標頭行" in app.error[0].value and "不會自動補單號" in app.error[0].value
        clean(app.run())
        assert not app.button(key="create").disabled
        assert counts["master"] == counts["calculation"] == counts["price_select"] == 0
        assert all(value == 0 for value in snapshot_counts().values())
    ''')


@pytest.mark.parametrize("field", ["raw", "discount_rate"])
def test_edit_permanently_revokes_even_after_switch_back(tmp_path, field):
    worker(tmp_path.resolve() / "private", f'''
        app = prepared(initialized())
        old = created(app)
        no_pricing = True
        field = {field!r}
        widget = app.text_area(key="raw") if field == "raw" else app.text_input(key="discount_rate")
        original = widget.value
        widget.set_value(original + "\\n" if field == "raw" else "0.04").run()
        widget = app.text_area(key="raw") if field == "raw" else app.text_input(key="discount_rate")
        widget.set_value(original).run()
        clean(app)
        now = current(app)
        assert now["invalidated"] and now["stage"] == 0
        assert now["document"] == old["document"]
        history(app, old["id"])
        assert current(app)["invalidated"]
        assert "save" not in {{b.key for b in app.button}}
        assert "來源或折扣已修改" in "\\n".join(e.value for e in app.info)
        assert not main_elements(app, "dataframe")
        assert counts == {{"build": 1, "master": 1, "calculation": 4, "price_select": 1}}
        assert snapshot_counts()["development_atomic_batch"] == 0
    ''')


def test_explicit_new_version_traces_old_identity(tmp_path):
    worker(tmp_path.resolve() / "private", '''
        app = prepared(initialized())
        old = created(app)
        app.text_input(key="discount_rate").set_value("0.04").run()
        click(app, "create")
        new = current(app)
        assert new["id"] != old["id"] and new["predecessor"] == old["id"] and new["version"] == 1
        assert new["stage"] == 0 and not new["invalidated"]
        assert new["document"]["children"][0]["ref_no"] != old["document"]["children"][0]["ref_no"]
        assert counts == {"build": 2, "master": 2, "calculation": 8, "price_select": 2}
        assert len(app.selectbox(key="history_batch").options) == 2
        history(app, old["id"])
        assert current(app)["invalidated"]
    ''')


@pytest.mark.parametrize("half", ["master", "workflow"])
def test_partial_initialization_can_resume_without_overwrite(tmp_path, half):
    worker(tmp_path.resolve() / "private", f'''
        if {half!r} == "master":
            business.initialize_master(directory)
        else:
            Workflow.initialize(directory).close()
        file = directory / ({half!r} + ".sqlite")
        before = file.read_bytes()
        app = clean(app_start())
        assert file.read_bytes() == before
        app.text_area(key="raw").set_value(business.SAMPLE).run()
        click(app, "create")
        if {half!r} == "master":
            assert file.read_bytes() == before
        assert current(app)["stage"] == 0
        assert len(current(app)["document"]["children"]) == 4
    ''')


@pytest.mark.parametrize("phase", ["reserve_before", "reserve_after", "save_after"])
def test_failure_restart_resumes_original_identity_without_pricing(tmp_path, phase):
    directory = tmp_path.resolve() / "private"
    old = worker(directory, f'''
        app = prepared(initialized())
        from engine.quote_number_reservation import QuoteNumberReservationService
        from engine.development_atomic_snapshot import DevelopmentAtomicSnapshotService
        phase = {phase!r}
        if phase.startswith("reserve"):
            original = QuoteNumberReservationService.reserve
            def fail(self, request):
                if phase == "reserve_after":
                    original(self, request)
                raise RuntimeError("SECRET SQL source")
            QuoteNumberReservationService.reserve = fail
            app.button(key="create").click().run()
            assert app.error and not app.exception
            assert app.session_state.active_batch
            no_pricing = True
            # 保持失敗 hook，重繪仍必須成功：不能自動呼叫 reserve。
            clean(app.run())
        else:
            created(app)
            original = DevelopmentAtomicSnapshotService.save
            def fail(self, request):
                original(self, request)
                raise RuntimeError("SECRET SQL source")
            DevelopmentAtomicSnapshotService.save = fail
            app.button(key="save").click().run()
            assert app.error and not app.exception
            no_pricing = True
            clean(app.run())
        view = current(app)
        emit({{"id": view["id"], "identity": view["identity"], "document": view["document"], "counts": counts}})
    ''')
    worker(directory, f'''
        expected = {old!r}
        no_pricing = True
        app = clean(app_start())
        view = history(app, expected["id"])
        assert view["identity"] == expected["identity"]
        if view["saved"] is None:
            click(app, "create")
            view = current(app)
            if expected["document"] is not None:
                assert view["document"] == expected["document"]
            click(app, "save")
        verified_downloads(app)
        assert all(value == 0 for value in counts.values())
        assert snapshot_counts()["quote_number_reservation"] == 1
        assert snapshot_counts()["development_atomic_batch"] == 1
    ''', initialize=False)


@pytest.mark.parametrize("kind", ["ValueError", "RuntimeError"])
def test_unknown_exception_is_sanitized_without_traceback_or_logs(tmp_path, kind):
    worker(tmp_path.resolve() / "private", f'''
        app = initialized()
        def fail(raw):
            raise {kind}("SECRET private source and SQL")
        business._parse = fail
        app.text_area(key="raw").set_value(business.SAMPLE).run()
        app.button(key="create").click().run()
        assert app.error and not app.exception
        assert all("SECRET" not in error.value for error in app.error)
        assert app.session_state.active_batch is None
        assert counts["calculation"] == 0
    ''')


@pytest.mark.parametrize("position", [2, 4])
def test_real_sql_failure_has_no_half_batch_then_restart_retries(tmp_path, position):
    directory = tmp_path.resolve() / "private"
    old = worker(directory, f'''
        app = prepared(initialized())
        view = created(app)
        no_pricing = True
        # 真實 SQLite 在第 2/4 張 INSERT 時中止，不 mock 保存答案。
        with closing(sqlite3.connect(directory / "snapshot.sqlite")) as db:
            db.execute("CREATE TRIGGER acceptance_test_fail BEFORE INSERT ON development_atomic_child "
                       "WHEN NEW.position = {position} BEGIN SELECT RAISE(ABORT, 'SECRET simulated SQL'); END")
            db.commit()
        app.button(key="save").click().run()
        assert app.error and not app.exception
        clean(app.run())
        failed = current(app)
        assert failed["phase"] == "SAVE_FAILED" and failed["stage"] == 1
        assert failed["document"] == view["document"]
        sizes = snapshot_counts()
        assert sizes["quote_number_reservation"] == 1 and sizes["quote_number_reserved_child"] == 4
        assert all(count == 0 for table, count in sizes.items() if table.startswith("development_atomic_"))
        emit({{"id": view["id"], "document": view["document"]}})
    ''')
    worker(directory, f'''
        expected = {old!r}
        no_pricing = True
        with closing(sqlite3.connect(directory / "snapshot.sqlite")) as db:
            db.execute("DROP TRIGGER acceptance_test_fail")
            db.commit()
        app = clean(app_start())
        view = history(app, expected["id"])
        assert view["stage"] == 1 and view["document"] == expected["document"]
        click(app, "save")
        verified_downloads(app)
        assert all(value == 0 for value in counts.values())
    ''', initialize=False)


def test_initialization_failure_then_new_process_only_finishes_missing_half(tmp_path):
    directory = tmp_path.resolve() / "private"
    before = worker(directory, '''
        app = clean(app_start())
        original = business.initialize_master
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("SECRET partial initialize")
        business.initialize_master = fail
        app.text_area(key="raw").set_value(business.SAMPLE).run()
        app.button(key="create").click().run()
        assert app.error and not app.exception
        assert tables("master.sqlite") == ["acceptance_synthetic_catalog"]
        assert tables("workflow.sqlite") == []
        emit(hashlib.sha256((directory / "master.sqlite").read_bytes()).hexdigest())
    ''')
    worker(directory, f'''
        app = clean(app_start())
        app.text_area(key="raw").set_value(business.SAMPLE).run()
        click(app, "create")
        assert hashlib.sha256((directory / "master.sqlite").read_bytes()).hexdigest() == {before!r}
        verified_tables(app)
    ''', initialize=False)


@pytest.mark.parametrize("fault", ["discount", "master"])
def test_configuration_price_or_policy_failure_never_persists_identity(tmp_path, fault):
    worker(tmp_path.resolve() / "private", f'''
        app = prepared(initialized())
        if {fault!r} == "discount":
            app.text_input(key="discount_rate").set_value("0.11").run()
        else:
            business.initialize_master(directory)
            with closing(sqlite3.connect(directory / "master.sqlite")) as db:
                db.execute("UPDATE acceptance_synthetic_catalog SET payload='{{}}'")
                db.commit()
        app.button(key="create").click().run()
        assert app.error and not app.exception
        assert app.session_state.active_batch is None
        assert counts["calculation"] == 0
        assert all(value == 0 for value in snapshot_counts().values())
        workflow = Workflow(directory)
        assert workflow.list_batches() == []
        workflow.close()
    ''')


def test_main_ui_changed_source_generates_new_preview_and_single_confirmation(tmp_path):
    worker(tmp_path.resolve() / "private", r'''
        app = prepared(initialized())
        old = created(app)
        app.text_area(key="raw").set_value(business.SAMPLE.replace("星河合成", "月河合成")).run()
        assert "save" not in {b.key for b in main_elements(app, "button")}
        click(app, "create")
        new = current(app)
        assert new["id"] != old["id"] and new["predecessor"] == old["id"]
        assert not new["confirmations"]
        no_pricing = True
        click(app, "save")
        verified_downloads(app)
        assert len(current(app)["confirmations"]) == 1
        assert {b.key for b in main_elements(app, "button")} == {"create"}
    ''')


def test_untrusted_legacy_actor_stays_in_details_not_main_cost_tabs(tmp_path):
    worker(tmp_path.resolve() / "private", r'''
        actor = '<img src="https://example.invalid/x"> ![圖](https://example.invalid/y)'
        workflow = Workflow.prepare(directory)
        view = workflow.create(business.SAMPLE, actor=actor,
            accepted_assumptions=[a["id"] for a in business.ASSUMPTIONS], source_confirmed=True)
        for stage in (1, 2):
            view = workflow.confirm(view["id"], stage, view["token"], view["context"])
        workflow.save(view["id"], view["context"])
        workflow.close()
        no_pricing = True
        app = clean(app_start())
        history(app, view["id"])
        verified_downloads(app)
        assert actor not in "\n".join(e.value for e in main_elements(app, "text"))
        assert not main_elements(app, "imgs")
        for markdown in app.markdown:
            assert not markdown.proto.allow_html
            assert actor not in markdown.value
        assert current(app)["document"]["context"]["actor"] == actor
        assert current(app)["confirmation_mode"] == "legacy-two-step"
        assert "save" not in {b.key for b in app.button}
    ''')


def test_cost_renderer_preserves_hostile_strings_and_hides_source_details(tmp_path):
    worker(tmp_path.resolve() / "private", r'''
        from acceptance import presentation as p
        app = prepared(initialized())
        view = created(app)
        no_pricing = True
        raw = '<script>alert(1)</script> ![圖](https://example.invalid/x)'
        original = p.fixed_summary
        def hostile(view):
            result = original(view)
            # 只替換顯示轉接輸出以測渲染器；不改請求、主檔或 workflow。
            for child in result["children"]:
                child["description"] = child["shared_description"] = raw
                child["rows"][0]["規格說明"] = raw
            return result
        p.fixed_summary = hostile
        clean(app.run())
        assert raw not in "\n".join(e.value for e in main_elements(app, "text"))
        costs = [frame.value for frame in app.dataframe if "成本序號" in frame.value.columns]
        assert len(costs) == 4 and all(frame.iloc[0]["規格說明"] == raw for frame in costs)
        for frame in app.dataframe:
            configuration = json.loads(frame.proto.columns)
            assert all(column["type_config"]["type"] == "text" for key, column in configuration.items() if key != "_index")
            for name in frame.value.columns:
                longest = max(len(str(value).split("\n")[0]) for value in frame.value[name])
                assert configuration[name]["width"] >= longest * 16 + 32
        assert not main_elements(app, "imgs") and not main_elements(app, "html")
        for markdown in app.markdown:
            assert not markdown.proto.allow_html and raw not in markdown.value
        assert current(app)["document"] == view["document"]
        assert not app.button(key="save").disabled
    ''')
