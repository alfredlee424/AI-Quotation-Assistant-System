"""SQL Server 2016+ 唯讀 sys catalog 檢查；不建表、不修表。

既有 SQL login 的有效 default schema 必須為 dbo。每次 read 及保存交易的
inspect_schema 都驗證此條件，讓既有未限定 schema 的查詢不會解析到同名私表。
主檔允許舊 codepage；寫表支援 Unicode、UTF-8、cp950 及 cp1252。
保存層必須先嚴格核驗編碼往返／bytes 容量，以 Unicode 綁定，提交前精確讀回。
"""
from __future__ import annotations

from sqlalchemy import text

from .backend import PRIMARY_KEYS, REQUIRED_COLUMNS, WRITE_TABLES, SchemaError


# 這些 literal 只來自程式固定合約，不接受使用者 SQL / identifier。
_TABLE_NAMES = ", ".join("'" + name + "'" for name in REQUIRED_COLUMNS)
_SCOPE = "s.name = 'dbo' AND t.name IN (" + _TABLE_NAMES + ")"
_JOIN = " FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id "


def check_context(conn):
    """完整 metadata 可見性與有效 default schema；不要用 metadata 空集合當成功。"""
    try:
        row = conn.execute(text(
            "SELECT SCHEMA_NAME() AS default_schema, "
            "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION') AS can_view"
        )).mappings().one()
        if row["default_schema"] != "dbo" or row["can_view"] != 1:
            raise ValueError
    except Exception:
        raise SchemaError("MSSQL 必須使用 dbo 預設結構並具資料庫 VIEW DEFINITION 權限") from None


def inspect_schema(conn):
    """僅 SELECT，正規化七表、雙向 FK、trigger 與 schema 副作用 facts。"""
    check_context(conn)

    def rows(sql):
        return conn.execute(text(sql)).mappings().all()

    try:
        tables = rows(
            "SELECT t.name AS table_name, t.temporal_type, t.is_memory_optimized, "
            "t.is_filetable, t.is_tracked_by_cdc, t.is_replicated, t.is_merge_published, "
            "t.is_sync_tran_subscribed" + _JOIN + "WHERE " + _SCOPE)
        facts = {}
        for row in tables:
            name = row["table_name"]
            if name in REQUIRED_COLUMNS:
                facts[name] = dict(engine="mssql", columns={}, pk=[], unique=[], fk=[],
                                   triggers=[], indexes=[], checks=[], security=[],
                                   table_risk=any(row[k] for k in (
                                       "temporal_type", "is_memory_optimized", "is_filetable", "is_tracked_by_cdc",
                                       "is_replicated", "is_merge_published", "is_sync_tran_subscribed")))
        for row in rows(
            "SELECT t.name AS table_name, c.name AS column_name, ty.name AS type_name, "
            "ty.is_user_defined, c.max_length, c.precision, c.is_nullable, "
            "c.is_identity, c.is_computed, c.generated_always_type, c.is_hidden, "
            "c.is_filestream, c.is_sparse, c.is_column_set, c.encryption_type, "
            "c.default_object_id, c.rule_object_id, "
            "COLLATIONPROPERTY(c.collation_name, 'CodePage') AS codepage" + _JOIN +
            "JOIN sys.columns c ON c.object_id=t.object_id "
            "JOIN sys.types ty ON ty.user_type_id=c.user_type_id WHERE " + _SCOPE):
            if row["table_name"] not in facts:
                continue
            kind, size = row["type_name"].lower(), row["max_length"]
            facts[row["table_name"]]["columns"][row["column_name"]] = dict(
                type=kind, length=(size // 2 if kind in {"nchar", "nvarchar"} and size != -1
                                   else size) if kind in {"char", "varchar", "nchar", "nvarchar"} else None,
                max_bytes=size, precision=row["precision"], nullable=bool(row["is_nullable"]),
                codepage=row["codepage"], default=bool(row["default_object_id"]),
                generated=bool(row["is_identity"] or row["is_computed"] or
                               row["generated_always_type"] or row["is_hidden"] or
                               kind in {"timestamp", "rowversion"}),
                special=bool(row["is_user_defined"] or row["is_filestream"] or
                             row["is_sparse"] or row["is_column_set"] or
                             row["encryption_type"] or row["rule_object_id"]))
        indexes = {}
        for row in rows(
            "SELECT t.name AS table_name, i.index_id, i.type AS index_type, "
            "i.is_primary_key, i.is_unique, i.is_disabled, i.has_filter, "
            "i.is_hypothetical, i.ignore_dup_key, ic.key_ordinal, c.name AS column_name" + _JOIN +
            "JOIN sys.indexes i ON i.object_id=t.object_id "
            "LEFT JOIN sys.index_columns ic ON ic.object_id=i.object_id AND "
            "ic.index_id=i.index_id AND ic.key_ordinal>0 "
            "LEFT JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id "
            "WHERE " + _SCOPE + " AND i.index_id>0 ORDER BY t.name, i.index_id, ic.key_ordinal"):
            if row["table_name"] not in facts:
                continue
            key = row["table_name"], row["index_id"]
            index = indexes.setdefault(key, dict(
                columns=[], primary=bool(row["is_primary_key"]), unique=bool(row["is_unique"]),
                unsafe=bool(row["is_disabled"] or row["has_filter"] or row["is_hypothetical"]
                            or row["ignore_dup_key"] or row["index_type"] not in (1, 2))))
            if row["key_ordinal"]:
                index["columns"].append(row["column_name"])
        for (table, _), index in indexes.items():
            facts[table]["indexes"].append(index)
            if not index["unsafe"]:
                if index["primary"]:
                    facts[table]["pk"] = index["columns"]
                if index["unique"]:
                    facts[table]["unique"].append(index["columns"])
        # Disabled / untrusted FK 與 trigger 也不忽略；啟用狀態不代表不存在風險。
        for row in rows(
            "SELECT t.name AS table_name" + _JOIN +
            "JOIN sys.foreign_keys f ON f.parent_object_id=t.object_id "
            "OR f.referenced_object_id=t.object_id WHERE " + _SCOPE):
            if row["table_name"] in facts:
                facts[row["table_name"]]["fk"].append(True)
        for catalog, join, key in (
            ("sys.triggers", "x.parent_id=t.object_id", "triggers"),
            ("sys.check_constraints", "x.parent_object_id=t.object_id", "checks"),
            ("sys.security_predicates", "x.target_object_id=t.object_id", "security"),
        ):
            for row in rows("SELECT t.name AS table_name" + _JOIN +
                            "JOIN " + catalog + " x ON " + join + " WHERE " + _SCOPE):
                if row["table_name"] in facts:
                    facts[row["table_name"]][key].append(True)
        return facts
    except Exception:
        raise SchemaError("MSSQL 無法取得完整七表結構資訊") from None


def _float_profile(col):
    if col["type"] == "real" and col["precision"] == 24:
        return "binary32"
    if col["type"] == "float" and col["precision"] in (24, 53):
        return "binary32" if col["precision"] == 24 else "binary64"
    return None


def check_schema(schema):
    """維持既有七表欄位、PK / unique 合約；錯誤只含固定合約表欄名稱。"""
    errors, capacities, profiles = [], {}, set()
    try:
        for table, expected in REQUIRED_COLUMNS.items():
            actual = schema.get(table)
            if actual is None:
                errors.append(f"{table}: missing table")
                continue
            if actual["engine"] != "mssql" or actual["table_risk"]:
                errors.append(f"{table}: unsupported table")
            if tuple(actual["pk"]) != PRIMARY_KEYS[table]:
                errors.append(f"{table}: primary key")
            if actual["fk"] or actual["triggers"] or actual["security"]:
                errors.append(f"{table}: foreign key/trigger/security risk")
            if any(index["unsafe"] for index in actual["indexes"]):
                errors.append(f"{table}: disabled/filtered/unsupported index")
            unique = {tuple(cols) for cols in actual["unique"]}
            allowed = {PRIMARY_KEYS[table]}
            if table == "quote_snapshot_document":
                allowed.add(("workgroup", "ref_no"))
                if ("workgroup", "ref_no") not in unique:
                    errors.append(f"{table}: missing unique reference")
            if table in WRITE_TABLES:
                capacities[table] = {}
                if unique - allowed or actual["checks"]:
                    errors.append(f"{table}: unexpected unique/check constraint")
                for name, col in actual["columns"].items():
                    # 不輸出非預期欄位名、default SQL、trigger 名或 constraint 名。
                    if col["generated"] or col["special"] or col["default"]:
                        errors.append(f"{table}: generated/special/default column risk")
                    if name not in expected and not col["nullable"]:
                        errors.append(f"{table}: unexpected required column")
            for name, (kind, length, nullable) in expected.items():
                col = actual["columns"].get(name)
                if col is None:
                    errors.append(f"{table}.{name}: missing column")
                    continue
                valid = col["nullable"] == nullable and not col["generated"] and not col["special"]
                if kind == "varchar":
                    types = {"nvarchar", "varchar"} if table in WRITE_TABLES else {
                        "char", "nchar", "varchar", "nvarchar"}
                    valid &= col["type"] in types and col["length"] == length
                    valid &= col["max_bytes"] == length * (2 if col["type"].startswith("n") else 1)
                elif kind == "text":
                    valid &= (col["type"] in {"ntext", "text"} or
                              col["type"] in {"nvarchar", "varchar"} and col["max_bytes"] == -1)
                elif kind == "float":
                    profile = _float_profile(col)
                    valid &= profile is not None
                    if table in WRITE_TABLES:
                        profiles.add(profile)
                else:
                    valid &= col["type"] == kind
                if not valid:
                    errors.append(f"{table}.{name}: type/length/nullability/precision")
                if table in WRITE_TABLES and kind in {"varchar", "text"}:
                    unicode = col["type"] in {"nvarchar", "ntext"}
                    encoding = "utf-16-le" if unicode else {
                        65001: "utf-8", 950: "cp950", 1252: "cp1252"}.get(col["codepage"])
                    if encoding is None:
                        errors.append(f"{table}.{name}: unsupported write codepage")
                    max_bytes = (2**31 - (2 if unicode else 1) if kind == "text" else col["max_bytes"])
                    capacities[table][name] = dict(encoding=encoding,
                                                   max_bytes=max_bytes, nullable=nullable)
        if profiles not in ({"binary32"}, {"binary64"}):
            errors.append("ordqdt_ai: six numeric columns require consistent binary32 or binary64 precision")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SchemaError("MSSQL 結構資訊不完整或不符合七表合約") from None
    if errors:
        raise SchemaError("結構檢查未通過：" + "; ".join(errors))
    return dict(ok=True, tables=tuple(REQUIRED_COLUMNS), testing=False,
                numeric_profile=next(iter(profiles)), write_capacities=capacities)


def validate_write_values(capacities, table, values):
    """實際編碼 bytes 與嚴格往返；禁止替代字、best-fit、截斷及超量。"""
    if capacities is None:
        raise SchemaError("必須先完成 MSSQL 結構檢查才能驗證文字容量")
    if table not in WRITE_TABLES or not isinstance(values, dict):
        raise SchemaError("MSSQL 文字容量檢查只接受合約寫表及欄位")
    if any(name not in REQUIRED_COLUMNS[table] for name in values):
        raise SchemaError(f"{table}: unexpected write column")
    for name, spec in capacities[table].items():
        if name not in values:
            raise SchemaError(f"{table}.{name}: missing write value")
        value = values[name]
        if value is None and spec["nullable"]:
            continue
        try:
            if not isinstance(value, str):
                raise ValueError
            encoded = value.encode(spec["encoding"], errors="strict")
            if len(encoded) > spec["max_bytes"] or encoded.decode(spec["encoding"], errors="strict") != value:
                raise ValueError
        except (UnicodeError, ValueError):
            raise SchemaError(f"{table}.{name}: text encoding/capacity") from None
