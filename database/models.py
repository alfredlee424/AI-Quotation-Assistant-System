"""
database/models.py - ORM 資料模型定義

依設計文件欄位定義以下四張資料表：
  - Ordspd  : 選項定義檔
  - Ordspe  : 可選項目檔（含採購單價）
  - Ordqty  : 產品部位用量檔（BOM 用量規則）
  - ordqdt_ai  : 報價規格明細檔（快照，建議新增）

主要設計原則：
  - 使用 SQLAlchemy 2.x Mapped 型別標註
  - 欄位長度與型態依文件規格
  - SQLite 相容（String 代替 NCHAR/NVARCHAR）
  - MSSQL 切換時欄位對應不變
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import String, Float, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database.connection import Base


# ============================================================
# ordspd — 選項定義檔
# PK: (workgroup, optno)
# ============================================================

class Ordspd(Base):
    """
    選項定義檔。
    定義系統中的選項類別（如桌面尺寸、木腳尺寸、顏色材質等）。
    """
    __tablename__ = "ordspd"

    # --- PK ---
    workgroup: Mapped[str] = mapped_column(String(3), primary_key=True, comment="事業別")
    optno: Mapped[str] = mapped_column(String(10), primary_key=True, comment="選項代碼")

    # --- 資料欄位 ---
    kind: Mapped[Optional[str]] = mapped_column(String(1), nullable=True, comment="類別")
    optdesc: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, comment="選項說明")
    code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="選項相關編碼")

    # --- 稽核欄位 ---
    adddate: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="建立日期")
    addusrno: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="建立人員")
    abndat: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="異動日期")
    abntim: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="異動時間")
    usrno: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="異動人員")
    prgno: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="異動程式")

    def __repr__(self) -> str:
        return f"<Ordspd workgroup={self.workgroup!r} optno={self.optno!r} desc={self.optdesc!r}>"


# ============================================================
# ordspe — 可選項目記錄檔
# PK: (workgroup, path, code, codsc)
# ============================================================

class Ordspe(Base):
    """
    可選項目檔。
    定義某選項底下有哪些可選項目，以及各項目的採購成本。
    path 欄位具有階層式路徑特性（如 CMT1\\A001\\W001）。
    """
    __tablename__ = "ordspe"

    # --- PK ---
    workgroup: Mapped[str] = mapped_column(String(3), primary_key=True, comment="事業別")
    path: Mapped[str] = mapped_column(String(100), primary_key=True, comment="選項階層路徑")
    code: Mapped[str] = mapped_column(String(10), primary_key=True, comment="項目代號")
    codsc: Mapped[str] = mapped_column(String(40), primary_key=True, comment="項目名稱")

    # --- 資料欄位 ---
    compri: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="採購成本")

    # --- 稽核欄位 ---
    adddate: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="建立日期")
    addusrno: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="建立人員")
    abndat: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="異動日期")
    abntim: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="異動時間")
    usrno: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="異動人員")
    prgno: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="異動程式")

    def __repr__(self) -> str:
        return f"<Ordspe path={self.path!r} code={self.code!r} compri={self.compri}>"


# ============================================================
# ordqty — 產品部位用量記錄檔（BOM 用量規則）
# PK: (workgroup, path, code, codsc)
# ============================================================

class Ordqty(Base):
    """
    產品部位用量檔。
    描述產品部位與其標準用量、裁切量，是 BOM 的核心。

    注意：真實 MSSQL 資料庫中 codsc 欄位全部為 NULL，
    因此 ORM 模型中 codsc 設為 nullable，不納入 PK，
    查詢只依 (workgroup, path, code) 三欄比對。
    """
    __tablename__ = "ordqty"

    # --- PK（僅三欄，codsc 在真實資料全為 NULL）---
    workgroup: Mapped[str] = mapped_column(String(3), primary_key=True, comment="事業別")
    path: Mapped[str] = mapped_column(String(100), primary_key=True, comment="項目階層路徑")
    code: Mapped[str] = mapped_column(String(10), primary_key=True, comment="項目代號")
    codsc: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, comment="項目名稱（真實資料全為 NULL）")

    # --- 資料欄位 ---
    part_path: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="產品部位路徑")
    stdqty: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="標準用量")
    stdpar: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="裁切用量")

    # --- 稽核欄位 ---
    adddate: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="建立日期")
    addusrno: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="建立人員")
    abndat: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="異動日期")
    abntim: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="異動時間")
    usrno: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="異動人員")
    prgno: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="異動程式")

    def __repr__(self) -> str:
        return (
            f"<Ordqty path={self.path!r} code={self.code!r} "
            f"stdqty={self.stdqty} stdpar={self.stdpar}>"
        )


# ============================================================
# ordqdt_ai — 報價規格明細檔（快照，建議新增）
# PK: (workgroup, ref_no, seq_no)
# ============================================================

class ordqdt_ai(Base):
    """
    報價規格明細檔（報價快照）。
    保存「本次報價建立當下」所選規格、成本、售價，
    確保歷史報價不受主檔異動影響。
    """
    __tablename__ = "ordqdt_ai"

    # --- PK ---
    workgroup: Mapped[str] = mapped_column(String(3), primary_key=True, comment="事業別")
    ref_no: Mapped[str] = mapped_column(String(21), primary_key=True, comment="報價單號")
    seq_no: Mapped[str] = mapped_column(String(5), primary_key=True, comment="明細序號")

    # --- 選擇結果 ---
    part_code: Mapped[str] = mapped_column(String(10), nullable=False, comment="部件代碼")
    part_desc: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, comment="部件名稱（快照）")
    path: Mapped[str] = mapped_column(String(100), nullable=False, comment="選配路徑")
    opt_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="選項代碼（ordspd.optno）")
    opt_desc: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, comment="選項說明（快照）")
    spc_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="規格代碼")
    spdsc: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, comment="規格說明（快照）")

    # --- 數量與用量快照 ---
    qty: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="實際數量")
    stdqty: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="標準用量快照")
    stdpar: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="裁切量快照")

    # --- 成本與報價快照 ---
    compri: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="採購成本快照")
    unit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="客戶報價單價")
    amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="明細報價金額")
    unit: Mapped[Optional[str]] = mapped_column(String(5), nullable=True, comment="單位（PCS/SET/M）")

    # --- 狀態 ---
    status: Mapped[Optional[str]] = mapped_column(String(1), nullable=True, comment="狀態（D草稿/C確認/X作廢）")
    transferred: Mapped[Optional[str]] = mapped_column(String(1), nullable=True, default="N", comment="是否已轉入正式報價（Y已轉入/N未轉入），預設 N")

    # --- 稽核欄位 ---
    adddate: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="建立日期")
    addusrno: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="建立人員")
    abndat: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="異動日期")
    abntim: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="異動時間")
    usrno: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, comment="異動人員")
    prgno: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="異動程式")

    __table_args__ = (
        UniqueConstraint("workgroup", "ref_no", "seq_no", name="uq_ordqdt_ai_pk"),
    )

    def __repr__(self) -> str:
        return (
            f"<ordqdt_ai ref_no={self.ref_no!r} seq_no={self.seq_no!r} "
            f"part={self.part_code!r} amount={self.amount}>"
        )


# ============================================================
# 建立所有資料表（若不存在）
# ============================================================

def init_db() -> None:
    """
    建立所有 ORM 定義的資料表（若不存在則建立）。
    通常在應用啟動時呼叫一次。
    """
    from database.connection import engine
    Base.metadata.create_all(bind=engine)
