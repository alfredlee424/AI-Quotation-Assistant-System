"""
database/seed_data.py - SQLite 範例假資料（對齊真實巢狀樹路徑結構）

資料模型設計原則（依真實 MSSQL 資料庫結構）：
  - ordspd.optno  → 選項類別代碼（如 A001=桌面尺寸、B001=木腳）
  - ordspe.path   → 完整巢狀路徑（如 CMT1\\A001、CMT1\\A001\\S004）
  - ordqty.path   → 子部件完整路徑（如 CMT1\\A001\\S004）
  - ordqty.code   → 驅動根節點所選代碼（如 A001 選 C003=60*180，非子部件自身代碼）
  - ordqty.part_path → 驅動根節點路徑（如 CMT1\\A001）
  - ordqty.codsc  → NULL（真實資料庫此欄位全為 NULL，查詢只用 path+code）

ordqty 查詢規則（已用 QU26731001 / QU26824014 雙重驗證）：
  同一子部件（如 CMT1\\A001\\S004）的 stdqty 依「驅動根節點所選代碼」不同而異：
    ordqty(path="CMT1\\A001\\S004", code="C001") → stdqty=4.0（60*120 小桌）
    ordqty(path="CMT1\\A001\\S004", code="C002") → stdqty=4.5（60*150 中桌）
    ordqty(path="CMT1\\A001\\S004", code="C003") → stdqty=4.5（60*180 大桌）

產品樹結構：
  CMT1\\A001                  桌面尺寸根節點（compri=0，不計入成本）
  CMT1\\A001\\S004            板材（25mm MDF）
  CMT1\\A001\\S005\\S001      表板（揚胡桃色紙）
  CMT1\\A001\\W001\\W010      裁切費
  CMT1\\A001\\W001\\W030      木工費

  CMT1\\B001                  木腳根節點（compri=0，不計入成本）
  CMT1\\B001\\S004            腳板材（MDF 18mm）
  CMT1\\B001\\S005\\S008      包覆色紙

標準驗算（qty=1，A001 選 C003=60*180，B001 選 C001=45寬）：
  CMT1\\A001\\S004  stdqty=4.5  compri=700  → 成本 4.5×700 = 3,150
  CMT1\\A001\\S005\\S001 stdqty=1.0 compri=513 → 成本 1×513 = 513
  CMT1\\A001\\W001\\W010 stdqty=1.0 compri=150 → 成本 150
  CMT1\\A001\\W001\\W030 stdqty=1.0 compri=300 → 成本 300
  CMT1\\B001\\S004  stdqty=8.0  compri=480  → 成本 8×480 = 3,840
  CMT1\\B001\\S005\\S008 stdqty=4.0 compri=140 → 成本 4×140 = 560
  材料總成本 = 8,513
  加成 30%：11,066.9；稅 5%：11,620.25

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


def _path(*parts: str) -> str:
    """組合完整路徑：CMT1\\part1\\part2\\..."""
    return "\\".join([PFX] + list(parts))


# ============================================================
# ordspd — 選項類別定義
# ============================================================

ORDSPD_DATA: list[dict] = [
    {"workgroup": WG, "kind": "2", "optno": "A001", "optdesc": "桌面尺寸", "code": "TOP"},
    {"workgroup": WG, "kind": "2", "optno": "B001", "optdesc": "木腳",     "code": "LEG"},
    {"workgroup": WG, "kind": "3", "optno": "S004", "optdesc": "板材",     "code": "S004"},
    {"workgroup": WG, "kind": "3", "optno": "S005", "optdesc": "表板",     "code": "S005"},
    {"workgroup": WG, "kind": "3", "optno": "S001", "optdesc": "色紙",     "code": "S001"},
    {"workgroup": WG, "kind": "3", "optno": "S008", "optdesc": "包覆色紙", "code": "S008"},
    {"workgroup": WG, "kind": "3", "optno": "W010", "optdesc": "裁切費",   "code": "W010"},
    {"workgroup": WG, "kind": "3", "optno": "W030", "optdesc": "木工費",   "code": "W030"},
]


# ============================================================
# ordspe — 可選項目（採購成本）
#
# path 格式：完整巢狀路徑（如 CMT1\\A001\\S004）
# compri：根節點 compri=0，子部件有成本
# ============================================================

ORDSPE_DATA: list[dict] = [
    # ── A001 桌面尺寸根節點（compri=0，尺寸驅動子部件 stdqty） ──
    {"workgroup": WG, "path": _path("A001"), "code": "C001", "codsc": "60*120", "compri": 0.0},
    {"workgroup": WG, "path": _path("A001"), "code": "C002", "codsc": "60*150", "compri": 0.0},
    {"workgroup": WG, "path": _path("A001"), "code": "C003", "codsc": "60*180", "compri": 0.0},

    # ── A001\\S004 板材（compri=材質成本） ──────────────────────
    {"workgroup": WG, "path": _path("A001", "S004"), "code": "C001", "codsc": "25mm MDF",   "compri": 700.0},
    {"workgroup": WG, "path": _path("A001", "S004"), "code": "C002", "codsc": "18mm 夾板",  "compri": 580.0},

    # ── A001\\S005\\S001 表板色紙 ────────────────────────────────
    {"workgroup": WG, "path": _path("A001", "S005", "S001"), "code": "C001", "codsc": "817胡桃", "compri": 513.0},
    {"workgroup": WG, "path": _path("A001", "S005", "S001"), "code": "C003", "codsc": "932揚胡桃", "compri": 513.0},

    # ── A001\\W001\\W010 裁切費 ──────────────────────────────────
    {"workgroup": WG, "path": _path("A001", "W001", "W010"), "code": "C001", "codsc": "裁切", "compri": 150.0},
    {"workgroup": WG, "path": _path("A001", "W001", "W010"), "code": "C002", "codsc": "美耐板裁切", "compri": 200.0},

    # ── A001\\W001\\W030 木工費 ──────────────────────────────────
    {"workgroup": WG, "path": _path("A001", "W001", "W030"), "code": "C001", "codsc": "木工", "compri": 300.0},

    # ── B001 木腳根節點（compri=0，腳型驅動子部件 stdqty） ──────
    {"workgroup": WG, "path": _path("B001"), "code": "C001", "codsc": "45寬環式腳", "compri": 0.0},
    {"workgroup": WG, "path": _path("B001"), "code": "C002", "codsc": "55寬環式腳", "compri": 0.0},
    {"workgroup": WG, "path": _path("B001"), "code": "C003", "codsc": "60寬環式腳", "compri": 0.0},
    {"workgroup": WG, "path": _path("B001"), "code": "C004", "codsc": "75寬環式腳", "compri": 0.0},

    # ── B001\\S004 腳板材 ─────────────────────────────────────────
    {"workgroup": WG, "path": _path("B001", "S004"), "code": "C001", "codsc": "MDF 4*8*18mm", "compri": 480.0},

    # ── B001\\S005\\S008 包覆色紙 ─────────────────────────────────
    {"workgroup": WG, "path": _path("B001", "S005", "S008"), "code": "C001", "codsc": "4*8*3mm MDF胡桃紙", "compri": 140.0},
    {"workgroup": WG, "path": _path("B001", "S005", "S008"), "code": "C002", "codsc": "4*8*3mm MDF白橡紙", "compri": 140.0},
]


# ============================================================
# ordqty — 部件用量規則（BOM）
#
# 重要：
#   - path    = 子部件完整路徑（如 CMT1\\A001\\S004）
#   - code    = 驅動根節點所選代碼（如 A001 選 C003=60*180）
#   - codsc   = None（真實資料庫全為 NULL）
#   - part_path = 驅動根節點路徑（如 CMT1\\A001）
#
# stdqty 隨「驅動節點選擇」不同而異（桌子尺寸越大，用料越多）：
#   A001\\S004 + C001(60*120) → stdqty=4.0
#   A001\\S004 + C002(60*150) → stdqty=4.5
#   A001\\S004 + C003(60*180) → stdqty=4.5
# ============================================================

ORDQTY_DATA: list[dict] = [
    # ── A001\\S004 板材用量（依桌面尺寸不同） ────────────────────
    # A001 選 C001（60*120）→ 板材需 4.0 片
    {
        "workgroup": WG,
        "path": _path("A001", "S004"), "code": "C001",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 4.0, "stdpar": 24.0,
    },
    # A001 選 C002（60*150）→ 板材需 4.5 片
    {
        "workgroup": WG,
        "path": _path("A001", "S004"), "code": "C002",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 4.5, "stdpar": 24.0,
    },
    # A001 選 C003（60*180）→ 板材需 4.5 片
    {
        "workgroup": WG,
        "path": _path("A001", "S004"), "code": "C003",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 4.5, "stdpar": 24.0,
    },

    # ── A001\\S005\\S001 表板色紙用量（stdqty=1.0，與尺寸無關） ──
    {
        "workgroup": WG,
        "path": _path("A001", "S005", "S001"), "code": "C001",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 2.0,
    },
    {
        "workgroup": WG,
        "path": _path("A001", "S005", "S001"), "code": "C002",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 2.0,
    },
    {
        "workgroup": WG,
        "path": _path("A001", "S005", "S001"), "code": "C003",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 2.0,
    },

    # ── A001\\W001\\W010 裁切費用量（stdqty=1.0） ─────────────────
    {
        "workgroup": WG,
        "path": _path("A001", "W001", "W010"), "code": "C001",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.0,
    },
    {
        "workgroup": WG,
        "path": _path("A001", "W001", "W010"), "code": "C002",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.0,
    },
    {
        "workgroup": WG,
        "path": _path("A001", "W001", "W010"), "code": "C003",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.0,
    },

    # ── A001\\W001\\W030 木工費用量（stdqty=1.0） ─────────────────
    {
        "workgroup": WG,
        "path": _path("A001", "W001", "W030"), "code": "C001",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.0,
    },
    {
        "workgroup": WG,
        "path": _path("A001", "W001", "W030"), "code": "C002",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.0,
    },
    {
        "workgroup": WG,
        "path": _path("A001", "W001", "W030"), "code": "C003",
        "codsc": None, "part_path": _path("A001"),
        "stdqty": 1.0, "stdpar": 1.0,
    },

    # ── B001\\S004 腳板材用量（各腳型均 8.0，依腳型代碼索引） ─────
    # B001 選 C001（45寬環式腳）
    {
        "workgroup": WG,
        "path": _path("B001", "S004"), "code": "C001",
        "codsc": None, "part_path": _path("B001"),
        "stdqty": 8.0, "stdpar": 120.0,
    },
    # B001 選 C002（55寬環式腳）
    {
        "workgroup": WG,
        "path": _path("B001", "S004"), "code": "C002",
        "codsc": None, "part_path": _path("B001"),
        "stdqty": 8.0, "stdpar": 120.0,
    },
    # B001 選 C003（60寬環式腳）
    {
        "workgroup": WG,
        "path": _path("B001", "S004"), "code": "C003",
        "codsc": None, "part_path": _path("B001"),
        "stdqty": 8.0, "stdpar": 120.0,
    },
    # B001 選 C004（75寬環式腳）
    {
        "workgroup": WG,
        "path": _path("B001", "S004"), "code": "C004",
        "codsc": None, "part_path": _path("B001"),
        "stdqty": 8.0, "stdpar": 120.0,
    },

    # ── B001\\S005\\S008 包覆色紙用量（stdqty=4.0/腳） ───────────
    {
        "workgroup": WG,
        "path": _path("B001", "S005", "S008"), "code": "C001",
        "codsc": None, "part_path": _path("B001"),
        "stdqty": 4.0, "stdpar": 6.0,
    },
    {
        "workgroup": WG,
        "path": _path("B001", "S005", "S008"), "code": "C002",
        "codsc": None, "part_path": _path("B001"),
        "stdqty": 4.0, "stdpar": 6.0,
    },
    {
        "workgroup": WG,
        "path": _path("B001", "S005", "S008"), "code": "C003",
        "codsc": None, "part_path": _path("B001"),
        "stdqty": 4.0, "stdpar": 6.0,
    },
    {
        "workgroup": WG,
        "path": _path("B001", "S005", "S008"), "code": "C004",
        "codsc": None, "part_path": _path("B001"),
        "stdqty": 4.0, "stdpar": 6.0,
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
            "[seed] 標準驗算（qty=1，A001/C003=60*180，B001/C001=45寬）："
            "A001\\S004 stdqty=4.5 × $700 + "
            "A001\\S005\\S001 stdqty=1 × $513 + "
            "A001\\W001\\W010 stdqty=1 × $150 + "
            "A001\\W001\\W030 stdqty=1 × $300 + "
            "B001\\S004 stdqty=8 × $480 + "
            "B001\\S005\\S008 stdqty=4 × $140 "
            "= 材料成本 $8,513"
        )
    except Exception as exc:
        db.rollback()
        print(f"[seed] 灌入失敗：{exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
