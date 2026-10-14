"""獨立整合審查：檢查實際 UI 產物，不讀私人設定或真工單。"""
from test_acceptance_ui import worker
from test_acceptance_workflow import worker as workflow_worker


def test_ui_source_only_persists_in_managed_databases_and_downloads(tmp_path):
    directory = tmp_path.resolve() / "private-review"
    result = worker(directory, '''
        # 記錄初始化後的 Python 檔案寫入；SQLite 原生 sidecar 另以目錄清冊核對。
        writes = set()
        def observe_writes(event, args):
            if event == "open" and isinstance(args[0], (str, bytes)):
                mode, flags = args[1:3]
                if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                        isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
                    writes.add(Path(os.fsdecode(args[0])).absolute())
        sys.addaudithook(observe_writes)
        source = business.SAMPLE.replace("星河合成", "SECRET合成隱私哨兵公司")
        assert source != business.SAMPLE
        app = prepared(initialized(), raw=source)
        view = created(app)
        no_pricing = True
        from engine.development_atomic_snapshot import DevelopmentAtomicSnapshotService
        original = DevelopmentAtomicSnapshotService.save
        def fail(self, request):
            raise RuntimeError("SECRET合成隱私哨兵公司 SQL " + source)
        DevelopmentAtomicSnapshotService.save = fail
        app.button(key="save").click().run()
        assert app.error and not app.exception
        assert all("SECRET" not in error.value for error in app.error)
        DevelopmentAtomicSnapshotService.save = original
        clean(app.run())
        assert current(app)["phase"] == "SAVE_FAILED"
        click(app, "save")
        result = verified_downloads(app)
        assert current(app)["saved"]["fixed_request"]["source"]["raw_text"] == source
        assert all(source in downloads["text_" + child["ref_no"]]["data"]
                   for child in view["document"]["children"])
        # 未送出錯誤原文至持久狀態；原文只存在預期的來源/證據欄位。
        with closing(sqlite3.connect(directory / "workflow.sqlite")) as db:
            state = json.loads(db.execute("SELECT state FROM acceptance_workflow_batch").fetchone()[0])
            assert state["last_error"] is None
            assert "SQL " + source not in json.dumps(state, ensure_ascii=False)
        allowed = {".four-quote-acceptance", "manifest.json", "master.sqlite", "snapshot.sqlite", "workflow.sqlite"}
        assert {path.name for path in directory.iterdir()} == allowed
        for name in (".four-quote-acceptance", "manifest.json", "master.sqlite"):
            assert b"SECRET" not in (directory / name).read_bytes()
        assert all(path == Path(os.devnull) or path.is_relative_to(directory) for path in writes), writes
        assert counts == {"build": 1, "master": 1, "calculation": 4, "price_select": 1}
        emit(result)
    ''')
    restored = worker(directory, f'''
        no_pricing = True
        app = clean(app_start())
        assert app.text_area(key="raw").value == ""
        history(app, {result['batch_id']!r})
        assert "SECRET合成隱私哨兵公司" in app.text_area(key="raw").value
        assert all(value == 0 for value in counts.values())
        emit(verified_downloads(app))
    ''', initialize=False)
    assert restored == result


def test_actual_acceptance_documents_cannot_enter_legacy_save_even_with_flags(tmp_path):
    workflow_worker(tmp_path.resolve() / "private-review", '''
        from copy import deepcopy
        from database import repository
        from engine import preview, pricing
        from engine.snapshot import build_snapshot_items, create_quote_snapshot
        v = confirmed(create())
        saved = w.save(v["id"], v["context"])
        documents = [b.inspect_source(b.SAMPLE), v["document"],
                     v["document"]["reservation"], saved["saved"],
                     *[child["structured"] for child in w.child_documents(v["id"])]]
        def deny(*args, **kwargs):
            raise AssertionError("不得查價或進入舊保存連線")
        repository._session = deny
        preview.calculate_configuration = pricing.calculate_configuration = deny
        b.build_request = b._Master = deny
        for original in documents:
            for flags in (False, True):
                doc = deepcopy(original)
                doc.update(can_quote=flags, can_confirm=flags, can_save=flags,
                           formal_approved=flags)
                for operation in (
                    lambda: preview.freeze_preview(doc),
                    lambda: preview.checked_preview({"preview": doc}, "fake"),
                    lambda: build_snapshot_items(doc, {}, "fake"),
                    lambda: create_quote_snapshot(None, {}, preview=doc),
                    lambda: repository.save_quote_snapshot(doc),
                    lambda: repository.save_quote_snapshot([doc]),
                    lambda: repository.save_quote_snapshot([{"ref_no": "fake"}], preview=doc),
                    lambda: w.create(doc, **options()),
                    lambda: w.save(doc, v["context"]),
                ):
                    rejected(operation)
        assert w.retrieve(v["id"])["saved"] == saved["saved"]
        assert len(w.list_batches()) == 1
    ''', initialize=True)
