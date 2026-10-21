"""Small explicit SQL fragments; never rewrite arbitrary caller SQL."""
from sqlalchemy import Unicode, bindparam, text

from .backend import REQUIRED_COLUMNS


def is_mssql(db):
    # Existing MySQL doubles may have a Mock dialect name, not a string.
    return getattr(getattr(getattr(db, "engine", None), "dialect", None), "name", None) == "mssql"


def limited_select(db, columns, source, count):
    """columns/source/count are internal SQL constants, never user input."""
    if is_mssql(db):
        return f"SELECT TOP {count} {columns} FROM {source}"
    return f"SELECT {columns} FROM {source} LIMIT {count}"


def lock_table(db, table):
    return table + (" WITH (UPDLOCK,HOLDLOCK)" if is_mssql(db) else "")


def lock_suffix(db):
    return "" if is_mssql(db) or db.testing else " FOR UPDATE"


def numeric_column(db, column):
    return f"CAST({column} AS FLOAT(53))" if is_mssql(db) else f"{column} + 0e0"


def insert_statement(db, table, row):
    """Use real column names and bind all text, including nullable audit fields."""
    statement = text(f"INSERT INTO {table} (" + ",".join(row) + ") VALUES (" +
                     ",".join(":" + name for name in row) + ")")
    if is_mssql(db):
        statement = statement.bindparams(*(bindparam(name, type_=Unicode())
                                          for name in row
                                          if REQUIRED_COLUMNS[table][name][0] in ("varchar", "text")))
    return statement
