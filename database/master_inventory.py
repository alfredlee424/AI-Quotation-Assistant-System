"""受限的主檔唯讀取樣；不呼叫初始化、不補預設用量、不使用 ORM 實體去重。"""
import re

from sqlalchemy import func, or_

from config import WORKGROUP
from database import repository
from database.models import Invdoc, Ordstr, Ordspe, Ordqty, Ordspd


MAX_TABLE_ROWS = 20000


def validate_product_code(prodkind):
    if not isinstance(prodkind, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,12}", prodkind):
        raise ValueError("產品代碼須為 1 至 12 字的英數字、底線或連字號；不接受路徑或查詢萬用字元。")


def read_product_master(prodkind: str, workgroup: str = WORKGROUP) -> dict:
    """一次工作階段讀五張主檔；不保證資料庫跨查詢快照隔離。"""
    validate_product_code(prodkind)
    if not isinstance(workgroup, str) or not re.fullmatch(r"[A-Za-z0-9]{1,3}", workgroup):
        raise ValueError("事業別格式不正確。")
    db = repository._session()
    try:
        def in_product(column):
            return or_(func.rtrim(column) == prodkind, column.startswith(prodkind + "\\", autoescape=True))

        def read(model, fields, *filters):
            columns = [getattr(model, name) for name in fields]
            with db.no_autoflush:
                rows = db.query(*columns).filter(model.workgroup == workgroup, *filters).limit(MAX_TABLE_ROWS + 1).all()
            if len(rows) > MAX_TABLE_ROWS:
                raise ValueError("單一主檔超過盤點筆數上限，本次不產生不完整報告。")
            return [{name: value.strip() if isinstance(value, str) else value for name, value in zip(fields, row)}
                    for row in rows]

        categories = read(Invdoc, ("prodkind", "codsc", "ordkind", "quo_rate"), func.rtrim(Invdoc.prodkind) == prodkind)
        edges = read(Ordstr, ("pathf", "pathc", "optnof", "optnoc", "must_chose", "seq"),
                     or_(in_product(Ordstr.pathf), in_product(Ordstr.pathc)))
        options = read(Ordspe, ("path", "code", "codsc", "compri"), in_product(Ordspe.path))
        quantities = read(Ordqty, ("path", "code", "part_path", "stdqty", "stdpar"), in_product(Ordqty.path))
        # 定義表屬事業別共用主檔；SQL 查詢有上限，回傳只包含本次產品使用的代碼。
        used = {p.rsplit("\\", 1)[-1] for p in [*(r["pathc"] for r in edges), *(r["path"] for r in options)]}
        definitions = [row for row in read(Ordspd, ("optno", "optdesc", "kind")) if row["optno"] in used]
        return {"workgroup": workgroup, "prodkind": prodkind, "categories": categories, "edges": edges,
                "options": options, "quantities": quantities, "definitions": definitions}
    finally:
        db.close()
