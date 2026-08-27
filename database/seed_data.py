"""
database/seed_data.py - SQLite 範例假資料（對齊真實 CMT1\\optno 結構）

資料模型設計原則（依真實 MSSQL 資料庫結構）：
  - ordspd.optno  → 選項類別代碼（如 A001=桌面尺寸、B001=木腳尺寸）
  - ordspe.path   = {PRODUCT_PREFIX}\\{optno}（如 CMT1\\A001）
  - ordqty.path   = {PRODUCT_PREFIX}\\{optno}[\\{子optno}]（如 CMT1\\A001\\W030）
  - ordqty.codsc  = NULL（真實資料庫此欄位全為 NULL，查詢只用 path+code）

選項類別與成本說明：
  - A001 桌面尺寸：compri = 0（尺寸本身無成本，材質決定成本）
  - S002 材質    ：compri = 成本（美耐板 500、實木 850）
  - B001 木腳尺寸：compri = 成本（木腳 120/支，stdqty=4）
  - B002 鐵腳    ：compri = 成本（鐵腳 150/支，stdqty=4）
  - S005 顏色材質：compri = 0 或小額加費（白色 0、胡桃木色 50）
  - W030 木工費  ：compri = 工費金額（標準木工 300）

標準範例：
  qty=20，選 A001/C001（60*120）、S002/C001（美耐板）、B001/C001（木腳）
  S005/C001（白色）、W030/C001（標準木工費）
  → 材料成本 = 20×1×500 + 20×4×120 + 20×0 + 20×1×300 = 25,600
  → 加成 30%：33,280；稅 5%：34,944

執行方式：
  python -m database.seed_data
  或由 app.py 啟動時自動呼叫
"""

from __future__ import annotations

from database.connection import get_db
from database.models import Ordspd, Ordspe, Ordqty, init_db
from config import WORKGROUP, PRODUCT_PREFIX

WG = WORKGROUP          # 事業別
PFX = PRODUCT_PREFIX    # 路徑前綴（CMT1）


def _path(optno: str) -> str:
    """組合 ordspe 路徑：CMT1\\{optno}"""
    return f"{PFX}\\{optno}"


def _qpath(optno: str, sub: str = "") -> str:
    """
    組合 ordqty 路徑。
    若有 sub（子 optno），格式為 CMT1\\{optno}\\{sub}；
    否則為 CMT1\\{optno}。
    """
    return f"{PFX}\\{optno}\\{sub}" if sub else f"{PFX}\\{optno}"


# ============================================================
# ordspd — 選項類別定義
# ============================================================

ORDSPD_DATA: list[dict] = [
    # 桌面尺寸（code=TOP 為系統 code，對應 ordspe.path 的 optno=A001）
    {"workgroup": WG, "kind": "2", "optno": "A001", "optdesc": "桌面尺寸", "code": "TOP"},
    # 材質
    {"workgroup": WG, "kind": "3", "optno": "S002", "optdesc": "材質",     "code": "MATERIAL"},
    # 木腳尺寸（規格）
    {"workgroup": WG, "kind": "2", "optno": "B001", "optdesc": "木腳尺寸", "code": "LEG"},
    # 鐵腳
    {"workgroup": WG, "kind": "2", "optno": "B002", "optdesc": "鐵腳",     "code": "B002"},
    # 顏色材質
    {"workgroup": WG, "kind": "3", "optno": "S005", "optdesc": "顏色材質", "code": "S005"},
    # 木工費
    {"workgroup": WG, "kind": "3", "optno": "W030", "optdesc": "木工費",   "code": "W030"},
    # 裁切費
    {"workgroup": WG, "kind": "3", "optno": "W010", "optdesc": "裁切費",   "code": "W010"},
]


# ============================================================
# ordspe — 可選項目（採購成本）
#
# path 格式：CMT1\\{optno}
# code/codsc 為該選項的代號與名稱
# compri 為採購成本；桌面尺寸本身 compri=0，成本在材質
# ============================================================

ORDSPE_DATA: list[dict] = [
    # ── A001 桌面尺寸（compri=0，尺寸決定材料規格，成本在 S002） ──
    {"workgroup": WG, "path": _path("A001"), "code": "C001", "codsc": "60*120", "compri": 0.0},
    {"workgroup": WG, "path": _path("A001"), "code": "C002", "codsc": "60*150", "compri": 0.0},
    {"workgroup": WG, "path": _path("A001"), "code": "C003", "codsc": "75*120", "compri": 0.0},
    {"workgroup": WG, "path": _path("A001"), "code": "C004", "codsc": "75*180", "compri": 0.0},

    # ── S002 材質（compri = 材質採購成本） ──────────────────────
    {"workgroup": WG, "path": _path("S002"), "code": "C001", "codsc": "美耐板",   "compri": 500.0},
    {"workgroup": WG, "path": _path("S002"), "code": "C002", "codsc": "實木貼皮", "compri": 850.0},
    {"workgroup": WG, "path": _path("S002"), "code": "C003", "codsc": "強化玻璃", "compri": 1200.0},
    {"workgroup": WG, "path": _path("S002"), "code": "C004", "codsc": "密集板",   "compri": 320.0},

    # ── B001 木腳尺寸（compri = 每支腳採購成本，stdqty=4 支/張） ─
    {"workgroup": WG, "path": _path("B001"), "code": "C001", "codsc": "標準木腳",   "compri": 120.0},
    {"workgroup": WG, "path": _path("B001"), "code": "C002", "codsc": "加長木腳",   "compri": 150.0},

    # ── B002 鐵腳（compri = 每支腳採購成本，stdqty=4 支/張） ────
    {"workgroup": WG, "path": _path("B002"), "code": "C001", "codsc": "標準鐵腳",   "compri": 180.0},
    {"workgroup": WG, "path": _path("B002"), "code": "C002", "codsc": "鋼管腳",     "compri": 150.0},

    # ── S005 顏色材質（白色/黑色 compri=0，特殊色加費） ─────────
    {"workgroup": WG, "path": _path("S005"), "code": "C001", "codsc": "白色",     "compri": 0.0},
    {"workgroup": WG, "path": _path("S005"), "code": "C002", "codsc": "黑色",     "compri": 0.0},
    {"workgroup": WG, "path": _path("S005"), "code": "C003", "codsc": "胡桃木色", "compri": 50.0},
    {"workgroup": WG, "path": _path("S005"), "code": "C004", "codsc": "米色",     "compri": 0.0},

    # ── W030 木工費 ───────────────────────────────────────────
    {"workgroup": WG, "path": _path("W030"), "code": "C001", "codsc": "標準木工費", "compri": 300.0},
    {"workgroup": WG, "path": _path("W030"), "code": "C002", "codsc": "特殊木工費", "compri": 450.0},

    # ── W010 裁切費 ───────────────────────────────────────────
    {"workgroup": WG, "path": _path("W010"), "code": "C001", "codsc": "標準裁切費", "compri": 80.0},
]


# ============================================================
# ordqty — 部件用量規則（BOM）
#
# 重要：codsc 設為 None（對齊真實資料庫 NULL，查詢僅用 path+code）
# stdqty：每張桌子需要的數量
# part_path：此部件所屬的產品路徑
# ============================================================

ORDQTY_DATA: list[dict] = [
    # ── A001 桌面尺寸 × 1 片 ──────────────────────────────────
    {
        "workgroup": WG, "path": _path("A001"), "code": "C001",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.05,
    },
    {
        "workgroup": WG, "path": _path("A001"), "code": "C002",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.05,
    },
    {
        "workgroup": WG, "path": _path("A001"), "code": "C003",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.05,
    },
    {
        "workgroup": WG, "path": _path("A001"), "code": "C004",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.05,
    },

    # ── S002 材質 × 1 片（stdpar 含損耗） ────────────────────
    {
        "workgroup": WG, "path": _path("S002"), "code": "C001",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.05,
    },
    {
        "workgroup": WG, "path": _path("S002"), "code": "C002",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.08,
    },
    {
        "workgroup": WG, "path": _path("S002"), "code": "C003",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.02,
    },
    {
        "workgroup": WG, "path": _path("S002"), "code": "C004",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.05,
    },

    # ── B001 木腳 × 4 支 ─────────────────────────────────────
    {
        "workgroup": WG, "path": _path("B001"), "code": "C001",
        "codsc": None, "part_path": _path("B001"),
        "stdqty": 4.0, "stdpar": 4.0,
    },
    {
        "workgroup": WG, "path": _path("B001"), "code": "C002",
        "codsc": None, "part_path": _path("B001"),
        "stdqty": 4.0, "stdpar": 4.0,
    },

    # ── B002 鐵腳 × 4 支 ─────────────────────────────────────
    {
        "workgroup": WG, "path": _path("B002"), "code": "C001",
        "codsc": None, "part_path": _path("B002"),
        "stdqty": 4.0, "stdpar": 4.0,
    },
    {
        "workgroup": WG, "path": _path("B002"), "code": "C002",
        "codsc": None, "part_path": _path("B002"),
        "stdqty": 4.0, "stdpar": 4.0,
    },

    # ── S005 顏色材質 × 1（無額外用量，compri=0 時自動略過計算）─
    {
        "workgroup": WG, "path": _path("S005"), "code": "C001",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.0,
    },
    {
        "workgroup": WG, "path": _path("S005"), "code": "C002",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.0,
    },
    {
        "workgroup": WG, "path": _path("S005"), "code": "C003",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.0,
    },
    {
        "workgroup": WG, "path": _path("S005"), "code": "C004",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.0,
    },

    # ── W030 木工費 × 1 次 ────────────────────────────────────
    {
        "workgroup": WG, "path": _path("W030"), "code": "C001",
        "codsc": None, "part_path": _path("W030"),
        "stdqty": 1.0, "stdpar": 1.0,
    },
    {
        "workgroup": WG, "path": _path("W030"), "code": "C002",
        "codsc": None, "part_path": _path("W030"),
        "stdqty": 1.0, "stdpar": 1.0,
    },

    # ── W010 裁切費 × 1 次 ────────────────────────────────────
    {
        "workgroup": WG, "path": _path("W010"), "code": "C001",
        "codsc": None, "part_path": _path("W010"),
        "stdqty": 1.0, "stdpar": 1.0,
    },
]


# ============================================================
# 主程式
# ============================================================

def seed() -> None:
    """建立資料表並灌入範例假資料（若資料已存在則略過）。"""
    init_db()

    db = get_db()
    try:
        existing = db.query(Ordspd).count()
        if existing > 0:
            print(f"[seed] 資料已存在（ordspd 共 {existing} 筆），略過灌入。")
            return

        # ordspd
        for row in ORDSPD_DATA:
            db.add(Ordspd(**row))

        # ordspe
        for row in ORDSPE_DATA:
            db.add(Ordspe(**row))

        # ordqty
        for row in ORDQTY_DATA:
            db.add(Ordqty(**row))

        db.commit()
        print(
            f"[seed] 已灌入範例資料（路徑前綴：{PFX}，事業別：{WG}）："
            f"ordspd={len(ORDSPD_DATA)} 筆，"
            f"ordspe={len(ORDSPE_DATA)} 筆，"
            f"ordqty={len(ORDQTY_DATA)} 筆。"
        )
        print(
            f"[seed] 標準範例計算預期：qty=20 × S002/C001(美耐板 $500×1) + "
            f"B001/C001(木腳 $120×4) + W030/C001(木工費 $300×1) = 材料成本 $25,600"
        )
    except Exception as exc:
        db.rollback()
        print(f"[seed] 灌入失敗：{exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
