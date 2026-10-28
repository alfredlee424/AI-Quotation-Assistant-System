"""Offline MSSQL protocol/catalog tests. No environment file, ODBC connection or network."""
from contextlib import ExitStack
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects.mssql.pyodbc import MSDialect_pyodbc
from sqlalchemy.engine import URL

from local_quote import backend, mssql_schema
from local_quote.backend import LocalDatabase, SchemaError, Settings, SettingsError


def url(**changes):
    values = dict(drivername="mssql+pyodbc", username="fixture_login", password="fixture_password",
                  host="localhost", database="isolated_fixture",
                  query={"driver": "ODBC Driver 18 for SQL Server"})
    values.update(changes)
    return URL.create(**values).render_as_string(hide_password=False)


@pytest.fixture(autouse=True)
def offline():
    with patch.object(backend, "dotenv_values", side_effect=AssertionError("private settings forbidden")), \
         patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
        yield


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "::1",
                                       "fixture-host", "FIXTURE-HOST", "localhost\\FIXTURE",
                                       "fixture-host\\FIXTURE_1"])
def test_local_sql_login_hosts(host):
    with patch.object(backend.socket, "gethostname", return_value="fixture-host"):
        validated = backend._validated_url(url(host=host))
    assert validated.drivername == "mssql+pyodbc"
    assert validated.query["Encrypt"] == "yes"
    assert validated.query["TrustServerCertificate"] == "no"
    assert validated.query["LongAsMax"] == "yes"
    assert "fixture_password" not in repr(Settings(url(), "103"))


@pytest.mark.parametrize("host", ["remote.invalid", "localhost.evil", "127.1", "127.0.0.2", ".",
    "(local)", "localhost,1433", "tcp:localhost", "localhost;Server=remote.invalid",
    "localhost\\bad-name", "localhost\\a\\b", "::1\\FIXTURE", "localhost\\",
    "localhost\\" + "x" * 17, "fixture-host.evil"])
def test_remote_alias_and_odbc_host_bypass_rejected(host):
    with patch.object(backend.socket, "gethostname", return_value="fixture-host"), pytest.raises(SettingsError):
        backend._validated_url(url(host=host))


@pytest.mark.parametrize("changes", [
    dict(username=None), dict(username=""), dict(password=None), dict(password=""), dict(database=""),
    dict(username="fixture;Server=remote.invalid"), dict(password="fixture};PWD=x"),
    dict(password="fixture\x00canary"), dict(password="fixture\x7fcanary"),
    dict(database="fixture;Server=remote.invalid"), dict(database="{fixture}"),
    dict(port=0), dict(port=65536), dict(host="localhost\\FIXTURE", port=1433),
    dict(drivername="mssql+pymssql"),
])
def test_invalid_fields_have_safe_errors(changes):
    with pytest.raises(SettingsError) as caught:
        backend._validated_url(url(**changes))
    assert "fixture" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("key,value", [
    ("odbc_connect", "DRIVER=x;SERVER=remote.invalid"), ("dsn", "fixture"), ("DSN", "fixture"),
    ("host", "remote.invalid"), ("port", "1433"), ("Server", "remote.invalid"),
    ("Trusted_Connection", "yes"), ("Authentication", "ActiveDirectoryPassword"),
    ("UID", "other"), ("PWD", "other"), ("MARS_Connection", "yes"),
    ("ApplicationIntent", "ReadOnly"), ("Encrypt", "no"), ("Encrypt", "yes;Server=x"),
    ("TrustServerCertificate", "yes;Server=x"), ("LongAsMax", "no"),
    ("driver", "SQL Server"), ("driver", "{ODBC Driver 18 for SQL Server}"),
    ("driver", ("ODBC Driver 18 for SQL Server", "ODBC Driver 18 for SQL Server")),
])
def test_query_allowlist(key, value):
    query = {"driver": "ODBC Driver 18 for SQL Server", key: value}
    with pytest.raises(SettingsError):
        backend._validated_url(url(query=query))


def test_driver17_explicit_local_certificate_option_and_odbc_translation():
    validated = backend._validated_url(url(query={"driver": "ODBC Driver 17 for SQL Server",
                                                   "encrypt": "YES", "trustservercertificate": "yes"}))
    args, kwargs = MSDialect_pyodbc().create_connect_args(validated)
    assert "DRIVER={ODBC Driver 17 for SQL Server}" in args[0]
    assert "Encrypt=yes" in args[0] and "TrustServerCertificate=yes" in args[0]
    assert "Trusted_Connection" not in args[0]
    assert "LongAsMax" not in args[0]
    assert not kwargs
    for query in ({}, {"driver": "ODBC Driver 17 for SQL Server", "LongAsMax": "yes"},
                  {"driver": "ODBC Driver 18 for SQL Server", "Encrypt": "yes", "encrypt": "yes"}):
        with pytest.raises(SettingsError):
            backend._validated_url(url(query=query))


@pytest.mark.parametrize("port", [None, 1433])
def test_ipv6_odbc_server_is_bracketed(port):
    validated = backend._validated_url(url(host="::1", port=port))
    args, _ = MSDialect_pyodbc().create_connect_args(validated)
    assert ("Server=[::1]" + (",1433" if port else "") + ";") in args[0]


def test_settings_loader_remains_noninterpolating_and_secret_free():
    with patch.object(backend, "dotenv_values", return_value={"DB_CONN_STR": url(), "WORKGROUP": "103"}) as values:
        settings = backend.load_settings("synthetic-fixture.env")
    values.assert_called_once_with("synthetic-fixture.env", interpolate=False)
    assert settings.workgroup == "103"
    assert "fixture" not in repr(settings)


def test_engine_timeout_and_driver_failure_are_safe():
    engine = Mock()
    engine.dialect.name = "mssql"
    with patch.object(backend, "create_engine", return_value=engine) as create, \
         patch.object(backend.event, "listen") as listen:
        db = LocalDatabase(Settings(url(), "103"))
    assert create.call_args.kwargs == dict(pool_pre_ping=True, hide_parameters=True,
                                         echo=False, connect_args={"timeout": 5})
    listen.assert_called_once_with(engine, "connect", LocalDatabase._mssql_timeout)
    dbapi = Mock()
    listen.call_args.args[2](dbapi, None)
    assert dbapi.timeout == 15
    with pytest.raises(SchemaError):
        _ = db.numeric_profile
    for error in (ModuleNotFoundError("fixture secret"), ImportError("fixture secret"), RuntimeError("fixture secret")):
        with patch.object(backend, "create_engine", side_effect=error), pytest.raises(SettingsError) as caught:
            LocalDatabase(Settings(url(), "103"))
        assert "fixture" not in str(caught.value)
    engine.connect.side_effect = RuntimeError("fixture secret")
    with pytest.raises(SettingsError) as caught:
        with db.read():
            pass
    assert "fixture" not in str(caught.value)


def catalog_rows():
    """Independent fixture specification, not generated from REQUIRED_COLUMNS."""
    specs = {
        "invdoc": ("workgroup prodkind", "workgroup:3 prodkind:12 codsc:30 ordkind:1", "quo_rate"),
        "ordstr": ("workgroup pathf pathc", "workgroup:3 pathf:100 pathc:100 optnof:10 optnoc:10 must_chose:1 has_name:1 has_inname:1", ""),
        "ordspd": ("workgroup optno", "workgroup:3 optno:10 kind:1 optdesc:30 code:10", ""),
        "ordspe": ("workgroup path code codsc", "workgroup:3 path:100 code:10 codsc:40", "compri"),
        "ordqty": ("workgroup path code", "workgroup:3 path:100 code:10 codsc:40 part_path:100", "stdqty stdpar"),
        "ordqdt_ai": ("workgroup ref_no seq_no", "workgroup:3 ref_no:21 seq_no:5 part_code:10 part_desc:40 path:100 opt_code:10 opt_desc:40 spc_code:10 spdsc:40 unit:5 status:1 transferred:1 adddate:10 addusrno:8 abndat:10 abntim:8 usrno:8 prgno:10", "qty stdqty stdpar compri unit_price amount"),
        "quote_snapshot_document": ("workgroup preview_id", "workgroup:3 preview_id:32 ref_no:21", ""),
    }
    tables, columns, indexes = [], [], []
    for table, (pk, strings, floats) in specs.items():
        tables.append(dict(table_name=table, temporal_type=0, is_memory_optimized=0, is_filetable=0,
                           is_tracked_by_cdc=0, is_replicated=0, is_merge_published=0, is_sync_tran_subscribed=0))
        required = set(pk.split()) | ({"optnof", "optnoc"} if table == "ordstr" else
                   {"part_code", "path"} if table == "ordqdt_ai" else
                   {"ref_no", "payload"} if table == "quote_snapshot_document" else set())
        definitions = [(s.split(":")[0], "nvarchar", int(s.split(":")[1]) * 2, 0) for s in strings.split()]
        definitions += [(name, "float", 8, 53) for name in floats.split()]
        if table == "ordstr":
            definitions += [("seq", "int", 4, 10), ("dmark", "text", 16, 0)]
        if table == "quote_snapshot_document":
            definitions += [("payload", "nvarchar", -1, 0)]
        for name, kind, size, precision in definitions:
            columns.append(dict(table_name=table, column_name=name, type_name=kind, is_user_defined=0,
                                max_length=size, precision=precision, is_nullable=int(name not in required),
                                is_identity=0, is_computed=0, generated_always_type=0, is_hidden=0,
                                is_filestream=0, is_sparse=0, is_column_set=0, encryption_type=None,
                                default_object_id=0, rule_object_id=0, codepage=950))
        unique = [pk.split()]
        if table == "quote_snapshot_document":
            unique.append(["workgroup", "ref_no"])
        for index_id, names in enumerate(unique, 1):
            for ordinal, name in enumerate(names, 1):
                indexes.append(dict(table_name=table, index_id=index_id, index_type=1 if index_id == 1 else 2,
                                    is_primary_key=int(index_id == 1), is_unique=1, is_disabled=0,
                                    has_filter=0, is_hypothetical=0, ignore_dup_key=0,
                                    key_ordinal=ordinal, column_name=name))
    return dict(tables=tables, columns=columns, indexes=indexes, foreign_keys=[], triggers=[],
                check_constraints=[], security_predicates=[])


class CatalogConnection:
    def __init__(self, rows=None, *, default_schema="dbo", can_view=1):
        self.rows = catalog_rows() if rows is None else rows
        self.context = dict(default_schema=default_schema, can_view=can_view)
        self.queries = []

    def execute(self, statement):
        sql = str(statement)
        self.queries.append(sql)
        assert sql.startswith("SELECT ")
        result = Mock()
        if "HAS_PERMS_BY_NAME" in sql:
            result.mappings.return_value.one.return_value = self.context
            return result
        assert "s.name = 'dbo'" in sql
        assert "t.name IN (" in sql
        for name in ("indexes", "columns", "foreign_keys", "triggers", "check_constraints", "security_predicates", "tables"):
            if "sys." + name + " " in sql:
                if name == "columns" and "CONVERT(int, COLLATIONPROPERTY(c.collation_name, 'CodePage')) AS codepage" not in sql:
                    # 模擬 pyodbc 不支援 sql_variant 的 fetch 錯誤及其反向 args。
                    result.mappings.return_value.all.side_effect = RuntimeError(
                        "ODBC SQL type -150 is not yet supported. secret-canary", "HY106")
                    return result
                result.mappings.return_value.all.return_value = self.rows[name]
                return result
        raise AssertionError("unexpected SQL")


def facts(rows=None):
    return LocalDatabase._mssql_schema(CatalogConnection(rows))


def column(rows, table, name):
    return next(c for c in rows["columns"] if (c["table_name"], c["column_name"]) == (table, name))


def test_catalog_positive_normalization_and_query_scopes():
    conn = CatalogConnection()
    normalized = LocalDatabase._mssql_schema(conn)
    report = backend._check_schema(normalized)
    assert report["ok"] and report["numeric_profile"] == "binary64"
    assert report["write_capacities"]["ordqdt_ai"]["part_desc"] == {
        "encoding": "utf-16-le", "max_bytes": 80, "nullable": True}
    assert len(conn.queries) == 8
    fk_query = next(sql for sql in conn.queries if "sys.foreign_keys" in sql)
    assert "f.parent_object_id=t.object_id OR f.referenced_object_id=t.object_id" in fk_query
    assert not any("is_disabled=0" in sql for sql in conn.queries)


@pytest.mark.parametrize("catalog,stage", [
    ("tables", "資料表"), ("columns", "欄位"), ("indexes", "索引"),
    ("foreign_keys", "外鍵"), ("triggers", "觸發器"),
    ("check_constraints", "檢查約束"), ("security_predicates", "列級安全政策"),
])
def test_metadata_failure_identifies_stage_without_echoing_driver(catalog, stage):
    from sqlalchemy.exc import ProgrammingError
    conn = CatalogConnection()
    execute = conn.execute
    def fail(statement):
        sql = str(statement)
        # Every query joins sys.tables; select its standalone query only.
        target = "sys." + catalog + " " in sql
        if catalog == "tables":
            target = "t.temporal_type" in sql
        if target:
            raise ProgrammingError("secret-canary SQL", {"p": "secret-canary"},
                                   RuntimeError("42S22", "secret-canary (207) (SQLExecDirectW)"))
        return execute(statement)
    with patch.object(conn, "execute", side_effect=fail), pytest.raises(SchemaError) as caught:
        LocalDatabase._mssql_schema(conn)
    message = str(caught.value)
    assert "階段：" + stage in message and "錯誤 207" in message and "2016" in message
    assert "secret-canary" not in message and caught.value.__suppress_context__


@pytest.mark.parametrize("error,fragment", [
    (RuntimeError("42000", "secret-canary (229)"), "權限遭拒"),
    (RuntimeError("42S02", "secret-canary (208)"), "不代表業務七表缺表"),
    (RuntimeError("42000", "secret-canary (102)"), "語法不相容"),
    (RuntimeError("42000", "secret-canary (156)"), "語法不相容"),
    (RuntimeError("HYT00", "secret-canary"), "逾時"),
    (RuntimeError("HYT01", "secret-canary"), "逾時"),
    (RuntimeError("08S01", "secret-canary"), "連線中斷"),
    (RuntimeError("42000", "secret-canary (2070)"), "遭伺服器拒絕"),
    (RuntimeError("secret-canary", "secret-canary (207)"), "未分類"),
    (KeyError("secret-canary"), "結構資訊處理失敗"),
    (RuntimeError(), "未分類"),
])
def test_metadata_error_categories_are_fixed_and_secret_free(error, fragment):
    message = mssql_schema._metadata_failure(error)
    assert fragment in message and "secret-canary" not in message


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_metadata_unsupported_type_sqlstate_in_either_position(wrapped, reverse):
    from sqlalchemy.exc import NotSupportedError
    args = ("HY106", "ODBC SQL type -150 is not yet supported. secret-canary")
    original = RuntimeError(*(args[::-1] if reverse else args))
    error = NotSupportedError("secret-canary SQL", {"p": "secret-canary"}, original) if wrapped else original
    message = mssql_schema._metadata_failure(error)
    assert "HY106" in message and "回傳型別" in message
    assert "secret-canary" not in message


def test_uncast_variant_fixture_reproduces_safe_column_failure():
    conn = CatalogConnection()
    execute = conn.execute
    def remove_cast(statement):
        sql = str(statement).replace(
            "CONVERT(int, COLLATIONPROPERTY(c.collation_name, 'CodePage'))",
            "COLLATIONPROPERTY(c.collation_name, 'CodePage')")
        return execute(text(sql))
    with patch.object(conn, "execute", side_effect=remove_cast), pytest.raises(SchemaError) as caught:
        LocalDatabase._mssql_schema(conn)
    message = str(caught.value)
    assert "階段：欄位" in message and "HY106" in message
    assert "secret-canary" not in message


@pytest.mark.parametrize("default_schema,permission", [("private_fixture", 1), ("dbo", 0), ("dbo", None), (None, 1)])
def test_effective_schema_and_database_metadata_permission_fail_closed(default_schema, permission):
    conn = CatalogConnection(default_schema=default_schema, can_view=permission)
    with pytest.raises(SchemaError) as caught:
        LocalDatabase._mssql_schema(conn)
    assert "private_fixture" not in str(caught.value)
    assert len(conn.queries) == 1


@pytest.mark.parametrize("kind", ["char", "nchar", "varchar", "nvarchar"])
def test_cp950_and_unicode_master_text_supported(kind):
    rows = catalog_rows()
    c = column(rows, "invdoc", "codsc")
    c.update(type_name=kind, max_length=60 if kind.startswith("n") else 30)
    assert backend._check_schema(facts(rows))["ok"]


@pytest.mark.parametrize("kind,codepage", [("nvarchar", 950), ("ntext", 950), ("varchar", 65001), ("text", 65001), ("text", 950), ("varchar", 1252)])
def test_payload_max_and_legacy_lob_unicode_or_utf8(kind, codepage):
    rows = catalog_rows()
    column(rows, "quote_snapshot_document", "payload").update(
        type_name=kind, max_length=16 if kind.endswith("text") else -1, codepage=codepage)
    assert backend._check_schema(facts(rows))["ok"]


@pytest.mark.parametrize("kind,precision,profile", [("float", 53, "binary64"), ("float", 24, "binary32"), ("real", 24, "binary32")])
def test_all_six_numeric_columns_must_have_consistent_profile(kind, precision, profile):
    rows = catalog_rows()
    for c in rows["columns"]:
        if c["table_name"] == "ordqdt_ai" and c["type_name"] == "float":
            c.update(type_name=kind, precision=precision)
    assert backend._check_schema(facts(rows))["numeric_profile"] == profile


@pytest.mark.parametrize("change", [
    dict(type_name="varchar", max_length=40, codepage=932), dict(type_name="char", max_length=40, codepage=65001),
    dict(max_length=78), dict(is_nullable=0), dict(is_identity=1), dict(is_computed=1),
    dict(generated_always_type=1), dict(is_hidden=1), dict(is_user_defined=1),
    dict(is_sparse=1), dict(is_column_set=1), dict(is_filestream=1), dict(encryption_type=1),
    dict(rule_object_id=1), dict(default_object_id=1), dict(type_name="timestamp"),
])
def test_write_column_contract_negative(change):
    rows = catalog_rows()
    column(rows, "ordqdt_ai", "part_desc").update(change)
    with pytest.raises(SchemaError):
        backend._check_schema(facts(rows))


@pytest.mark.parametrize("change", [dict(type_name="real", precision=24), dict(precision=25),
                                    dict(type_name="decimal"), dict(type_name="int")])
def test_mixed_or_unsupported_numeric_types_rejected(change):
    rows = catalog_rows()
    column(rows, "ordqdt_ai", "qty").update(change)
    with pytest.raises(SchemaError):
        backend._check_schema(facts(rows))


@pytest.mark.parametrize("change", [dict(type_name="text", max_length=16, codepage=932),
                                    dict(type_name="varchar", max_length=-1, codepage=None),
                                    dict(max_length=8000), dict(is_nullable=1)])
def test_payload_unsafe_codepage_bounded_or_nullable_rejected(change):
    rows = catalog_rows()
    column(rows, "quote_snapshot_document", "payload").update(change)
    with pytest.raises(SchemaError):
        backend._check_schema(facts(rows))


@pytest.mark.parametrize("key", ["is_disabled", "has_filter", "is_hypothetical", "ignore_dup_key"])
def test_unsafe_unique_index_not_counted(key):
    rows = catalog_rows()
    for index in rows["indexes"]:
        if index["table_name"] == "quote_snapshot_document" and index["index_id"] == 2:
            index[key] = 1
    normalized = facts(rows)
    assert ["workgroup", "ref_no"] not in normalized["quote_snapshot_document"]["unique"]
    with pytest.raises(SchemaError):
        backend._check_schema(normalized)


@pytest.mark.parametrize("catalog", ["foreign_keys", "triggers", "check_constraints", "security_predicates"])
def test_side_effect_catalogs_reject_without_leaking_names(catalog):
    rows = catalog_rows()
    rows[catalog] = [dict(table_name="ordqdt_ai", private_name="secret-canary")]
    with pytest.raises(SchemaError) as caught:
        backend._check_schema(facts(rows))
    assert "secret-canary" not in str(caught.value)


def test_missing_table_column_wrong_pk_and_extra_unique():
    initial = facts()
    cases = []
    missing = deepcopy(initial)
    missing.pop("invdoc")
    cases.append(missing)
    missing_column = deepcopy(initial)
    missing_column["ordstr"]["columns"].pop("pathf")
    cases.append(missing_column)
    wrong_pk = deepcopy(initial)
    wrong_pk["ordqdt_ai"]["pk"] = ["secret-canary"]
    cases.append(wrong_pk)
    extra_unique = deepcopy(initial)
    extra_unique["ordqdt_ai"]["unique"].append(["secret-canary"])
    cases.append(extra_unique)
    for changed in cases:
        with pytest.raises(SchemaError) as caught:
            backend._check_schema(changed, dialect="mssql")
        assert "secret-canary" not in str(caught.value)
    with pytest.raises(SchemaError):
        backend._check_schema({}, dialect="mssql")


@pytest.mark.parametrize("change", [dict(is_identity=1), dict(is_computed=1), dict(type_name="timestamp"),
                                    dict(default_object_id=1), dict(is_nullable=0)])
def test_extra_write_columns_do_not_hide_side_effects(change):
    rows = catalog_rows()
    extra = dict(column(rows, "ordqdt_ai", "part_desc"), column_name="secret-canary")
    extra.update(change)
    rows["columns"].append(extra)
    with pytest.raises(SchemaError) as caught:
        backend._check_schema(facts(rows))
    assert "secret-canary" not in str(caught.value)


@pytest.mark.parametrize("flag", ["temporal_type", "is_memory_optimized", "is_filetable", "is_tracked_by_cdc", "is_replicated",
                                  "is_merge_published", "is_sync_tran_subscribed"])
def test_special_tables_rejected(flag):
    rows = catalog_rows()
    rows["tables"][0][flag] = 1
    with pytest.raises(SchemaError):
        backend._check_schema(facts(rows))


def test_malformed_catalog_and_dbapi_errors_are_sanitized():
    conn = Mock()
    conn.execute.side_effect = RuntimeError("secret-canary SQL connection info")
    with pytest.raises(SchemaError) as caught:
        LocalDatabase._mssql_schema(conn)
    assert "secret-canary" not in str(caught.value)
    conn = CatalogConnection()
    conn.rows["columns"][0].pop("precision")
    with pytest.raises(SchemaError):
        LocalDatabase._mssql_schema(conn)


def mssql_db():
    engine = Mock()
    engine.dialect.name = "mssql"
    with patch.object(backend, "create_engine", return_value=engine), patch.object(backend.event, "listen"):
        return LocalDatabase(Settings(url(), "103"))


def test_inspect_sets_profile_and_invalidates_cached_contract_after_failure():
    db = mssql_db()
    assert db.inspect_schema(CatalogConnection())["ok"]
    assert db.numeric_profile == "binary64"
    with pytest.raises(SchemaError):
        db.inspect_schema(CatalogConnection(can_view=0))
    with pytest.raises(SchemaError):
        _ = db.numeric_profile
    with pytest.raises(SchemaError):
        db.validate_write_values("ordqdt_ai", {})


def test_actual_utf16_and_utf8_bytes_no_lossy_or_truncated_write():
    db = mssql_db()
    rows = catalog_rows()
    column(rows, "ordqdt_ai", "opt_desc").update(type_name="varchar", max_length=40, codepage=65001)
    db.inspect_schema(CatalogConnection(rows))
    values = {name: "" for name in db._write_capacities["ordqdt_ai"]}
    values.update(part_desc="😀" * 20, opt_desc="界" * 13 + "x")
    db.validate_write_values("ordqdt_ai", values)
    for key, value in (("part_desc", "😀" * 21), ("opt_desc", "界" * 14),
                       ("part_desc", "\ud800"), ("path", None), ("part_desc", b"fixture")):
        changed = dict(values, **{key: value})
        with pytest.raises(SchemaError) as caught:
            db.validate_write_values("ordqdt_ai", changed)
        assert "fixture" not in str(caught.value)
    with pytest.raises(SchemaError):
        db.validate_write_values("ordqdt_ai", dict(values, **{"secret-canary": "x"}))
    with pytest.raises(SchemaError):
        db.validate_write_values("secret-canary", values)
    with pytest.raises(SchemaError):
        db.validate_write_values("ordqdt_ai", {})
    snapshot = dict(workgroup="103", preview_id="0" * 32, ref_no="fixture", payload="中文😀" * 10000)
    db.validate_write_values("quote_snapshot_document", snapshot)


def test_cp950_and_cp1252_preflight_strict_roundtrip_and_byte_capacity():
    db = mssql_db()
    rows = catalog_rows()
    column(rows, "ordqdt_ai", "part_desc").update(type_name="varchar", max_length=40, codepage=950)
    column(rows, "ordqdt_ai", "opt_desc").update(type_name="varchar", max_length=40, codepage=1252)
    db.inspect_schema(CatalogConnection(rows))
    values = {name: "" for name in db._write_capacities["ordqdt_ai"]}
    values.update(part_desc="中" * 20, opt_desc="café")
    db.validate_write_values("ordqdt_ai", values)
    for key, value in (("part_desc", "中" * 21), ("part_desc", "😀"), ("opt_desc", "中")):
        with pytest.raises(SchemaError):
            db.validate_write_values("ordqdt_ai", dict(values, **{key: value}))


@pytest.mark.parametrize("state,fragment", [
    ("IM002", "ODBC"), ("IM003", "ODBC"), ("28000", "登入失敗"),
    ("08001", "TLS"), ("HYT00", "逾時"), ("HYT01", "逾時"), ("unknown", "無法連線"),
])
def test_connection_sqlstate_diagnostics_never_echo_raw_error(state, fragment):
    from local_quote.bootstrap import startup_error
    db = mssql_db()
    db.engine.connect.side_effect = RuntimeError(state, "secret-canary-server-password-query")
    with pytest.raises(backend.ConnectionSettingsError) as caught:
        with db.read():
            pytest.fail("must not yield a failed connection")
    message = startup_error(caught.value)
    assert fragment in message and "secret-canary" not in message
    assert "secret-canary" not in startup_error(RuntimeError("secret-canary"))


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("native,fragment", [
    ("-2146893019", "TLS 憑證鏈不受信任"),
    ("-2146893022", "TLS 憑證名稱與連線目標不符"),
])
def test_tls_native_diagnostics_are_safe_and_do_not_retry(wrapped, native, fragment):
    from sqlalchemy.exc import OperationalError
    from local_quote.bootstrap import startup_error
    original = RuntimeError("08001", f"secret-canary 憑證 certificate ({native}) (SQLDriverConnect)")
    error = OperationalError("secret-canary SQL", {"password": "secret-canary"}, original) if wrapped else original
    db = mssql_db()
    db.engine.connect.side_effect = error
    with pytest.raises(backend.ConnectionSettingsError) as caught:
        with db.read():
            pytest.fail("must not yield a failed connection")
    message = startup_error(caught.value)
    assert fragment in message and "SQLSTATE 08001" in message
    assert "secret-canary" not in message
    assert caught.value.__suppress_context__
    db.engine.connect.assert_called_once_with()


@pytest.mark.parametrize("detail", [
    "secret-canary (53)", "secret-canary (-21468930190)",
    "secret-canary -2146893019", "secret-canary certificate 憑證", None,
])
def test_unknown_08001_does_not_claim_certificate_failure(detail):
    message = str(backend._connection_error(RuntimeError("08001", detail)))
    assert "僅此錯誤碼無法判定" in message
    assert "Encrypt=yes" in message and "TrustServerCertificate=no" in message
    assert "secret-canary" not in message


def test_tls_native_code_does_not_override_other_sqlstates():
    message = str(backend._connection_error(RuntimeError("28000", "secret-canary (-2146893019)")))
    assert "登入失敗" in message and "TLS" not in message
    assert "secret-canary" not in message


@pytest.fixture
def read_db():
    # Real SQLAlchemy events/transactions; only MSSQL isolation/context protocol is faked.
    engine = create_engine("sqlite:///:memory:")
    db = LocalDatabase(Settings(url(), "103"), engine=engine, testing=True)
    db.testing = False
    recorded = []
    event.listen(engine, "before_cursor_execute", lambda c, u, s, p, x, m: recorded.append(s))
    rollbacks = []
    event.listen(engine, "rollback", lambda c: rollbacks.append(True))
    with patch.object(engine.dialect, "name", "mssql"), \
         patch.object(engine.dialect, "_assert_and_set_isolation_level") as isolation, \
         patch.object(mssql_schema, "check_context") as context:
        yield db, recorded, rollbacks, isolation, context
    engine.dispose()


def test_mssql_read_consistent_transaction_no_mysql_command_and_final_rollback(read_db):
    db, recorded, rollbacks, isolation, context = read_db
    with db.read() as conn:
        assert conn.in_transaction()
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
    assert recorded == ["SELECT 1"]
    assert len(rollbacks) == 1
    assert isolation.call_args_list[0].args[1] == "SERIALIZABLE"
    context.assert_called_once()
    assert not any("START TRANSACTION" in sql for sql in recorded)


@pytest.mark.parametrize("sql", ["INSERT INTO x VALUES (1)", "DELETE FROM x", "UPDATE x SET a=1",
    "SELECT * INTO x FROM y", "EXEC fixture", "SELECT 1; COMMIT", "SELECT 1; DELETE FROM x",
    "SELECT * FROM x WITH (UPDLOCK)", "SELECT * FROM x WITH (HOLDLOCK)",
    "SELECT * FROM x WITH (NOLOCK)", "SELECT * FROM x WITH (TABLOCKX)",
    "SELECT * FROM x WITH (READPAST)", "SELECT * FROM x WITH (READCOMMITTEDLOCK)",
    "SELECT * FROM x FOR UPDATE", "SELECT NEXT VALUE FOR fixture", "SELECT * FROM OPENQUERY(x,'x')",
    "SELECT * FROM OPENROWSET('x')", "SELECT 1 -- hidden\n EXEC fixture", "SELECT 1 /* hidden */",
    "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED", "COMMIT", "ROLLBACK"])
def test_read_query_guard(read_db, sql):
    db = read_db[0]
    with pytest.raises(ValueError, match="唯讀"):
        with db.read() as conn:
            conn.execute(text(sql))


@pytest.mark.parametrize("method", ["commit", "rollback"])
def test_read_prevents_caller_transaction_control(read_db, method):
    db = read_db[0]
    with ExitStack() as cleanup:
        with pytest.raises(ValueError, match="唯讀"):
            with db.read() as conn:
                # Spy 存活到 cleanup，包含明確 DBAPI rollback 與 pool reset。
                proxy = conn.connection
                rollback = cleanup.enter_context(patch.object(proxy, "rollback", wraps=proxy.rollback))
                getattr(conn, method)()
        assert rollback.call_count == 2


def test_read_rollback_on_body_or_context_error(read_db):
    db, _, rollbacks, _, context = read_db
    with pytest.raises(RuntimeError):
        with db.read() as conn:
            conn.execute(text("SELECT 1"))
            raise RuntimeError("fixture")
    assert len(rollbacks) == 1
    context.side_effect = SchemaError("context blocked")
    with pytest.raises(SchemaError):
        with db.read():
            pytest.fail("must not yield")
    assert len(rollbacks) == 2
