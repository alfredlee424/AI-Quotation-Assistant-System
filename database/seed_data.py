"""
database/seed_data.py - SQLite 範例假資料

灌入「辦公桌」產品的完整測試資料，包含：
  - ordspd：選項定義（桌面尺寸、材質、顏色、腳架類型）
  - ordspe：可選項目（各規格與採購單價）
  - ordqty：部件用量規則（桌面、木腳、金屬腳、裝飾條等）

執行方式：
  python -m database.seed_data
  或由 app.py 啟動時自動呼叫
"""

from __future__ import annotations

from database.connection import get_db
from database.models import Ordspd, Ordspe, Ordqty, init_db
from config import WORKGROUP

WG = WORKGROUP  # 事業別


# ============================================================
# ordspd — 選項定義
# ============================================================

ORDSPD_DATA: list[dict] = [
    # 桌面尺寸
    {"workgroup": WG, "kind": "S", "optno": "SIZE",   "optdesc": "桌面尺寸",   "code": "SIZE"},
    # 桌面材質
    {"workgroup": WG, "kind": "M", "optno": "MATS",   "optdesc": "桌面材質",   "code": "MATS"},
    # 顏色
    {"workgroup": WG, "kind": "C", "optno": "COLOR",  "optdesc": "桌面顏色",   "code": "COLOR"},
    # 腳架類型
    {"workgroup": WG, "kind": "L", "optno": "LEG",    "optdesc": "腳架類型",   "code": "LEG"},
    # 桌面外型
    {"workgroup": WG, "kind": "T", "optno": "TOP",    "optdesc": "桌面外型",   "code": "TOP"},
]


# ============================================================
# ordspe — 可選項目（採購單價為成本）
#
# path 設計：DESK\{類型}\{代碼}
#   DESK\SIZE\1200600  → 1200×600 桌面
#   DESK\MATS\CLMT     → 美耐板桌面
#   DESK\COLOR\WHITE   → 白色
#   DESK\LEG\WLEG      → 木腳
#   DESK\LEG\MLEG      → 金屬腳
# ============================================================

ORDSPE_DATA: list[dict] = [
    # ── 桌面尺寸 ──────────────────────────────────────────
    {"workgroup": WG, "path": "DESK\\SIZE", "code": "S1200600", "codsc": "1200×600mm", "compri": 480.0},
    {"workgroup": WG, "path": "DESK\\SIZE", "code": "S1500600", "codsc": "1500×600mm", "compri": 560.0},
    {"workgroup": WG, "path": "DESK\\SIZE", "code": "S1800600", "codsc": "1800×600mm", "compri": 650.0},
    {"workgroup": WG, "path": "DESK\\SIZE", "code": "S1200750", "codsc": "1200×750mm", "compri": 520.0},

    # ── 桌面材質 ──────────────────────────────────────────
    {"workgroup": WG, "path": "DESK\\MATS", "code": "CLMT",   "codsc": "美耐板",   "compri": 500.0},
    {"workgroup": WG, "path": "DESK\\MATS", "code": "WOOD",   "codsc": "實木貼皮", "compri": 850.0},
    {"workgroup": WG, "path": "DESK\\MATS", "code": "GLASS",  "codsc": "強化玻璃", "compri": 1200.0},
    {"workgroup": WG, "path": "DESK\\MATS", "code": "MELAMINE","codsc": "密集板",  "compri": 320.0},

    # ── 顏色 ──────────────────────────────────────────────
    {"workgroup": WG, "path": "DESK\\COLOR", "code": "WHITE",  "codsc": "白色",     "compri": 0.0},
    {"workgroup": WG, "path": "DESK\\COLOR", "code": "BLACK",  "codsc": "黑色",     "compri": 0.0},
    {"workgroup": WG, "path": "DESK\\COLOR", "code": "BEIGE",  "codsc": "米色",     "compri": 0.0},
    {"workgroup": WG, "path": "DESK\\COLOR", "code": "WALNUT", "codsc": "胡桃木色", "compri": 50.0},

    # ── 腳架類型 ──────────────────────────────────────────
    {"workgroup": WG, "path": "DESK\\LEG", "code": "WLEG",  "codsc": "木腳",     "compri": 120.0},
    {"workgroup": WG, "path": "DESK\\LEG", "code": "MLEG",  "codsc": "金屬腳",   "compri": 180.0},
    {"workgroup": WG, "path": "DESK\\LEG", "code": "SLEG",  "codsc": "鋼管腳",   "compri": 150.0},
    {"workgroup": WG, "path": "DESK\\LEG", "code": "ULEG",  "codsc": "U型腳",    "compri": 200.0},

    # ── 桌面外型 ──────────────────────────────────────────
    {"workgroup": WG, "path": "DESK\\TOP", "code": "RECT",  "codsc": "標準長方形", "compri": 0.0},
    {"workgroup": WG, "path": "DESK\\TOP", "code": "CURVE", "codsc": "弧形前緣",   "compri": 80.0},
    {"workgroup": WG, "path": "DESK\\TOP", "code": "LSHAPE","codsc": "L型桌面",    "compri": 200.0},
]


# ============================================================
# ordqty — 部件用量規則（BOM）
#
# 每張桌子的部件：
#   桌面板  × 1
#   木腳    × 4（選木腳時）
#   金屬腳  × 4（選金屬腳時）
#   橫樑    × 2
#   螺絲組  × 1套
# ============================================================

ORDQTY_DATA: list[dict] = [
    # ── 桌面板（對應材質選項）────────────────────────────
    {
        "workgroup": WG, "path": "DESK\\MATS", "code": "CLMT",
        "codsc": "美耐板", "part_path": "DESK\\PARTS\\TOP",
        "stdqty": 1.0, "stdpar": 1.05,
    },
    {
        "workgroup": WG, "path": "DESK\\MATS", "code": "WOOD",
        "codsc": "實木貼皮", "part_path": "DESK\\PARTS\\TOP",
        "stdqty": 1.0, "stdpar": 1.08,
    },
    {
        "workgroup": WG, "path": "DESK\\MATS", "code": "GLASS",
        "codsc": "強化玻璃", "part_path": "DESK\\PARTS\\TOP",
        "stdqty": 1.0, "stdpar": 1.02,
    },
    {
        "workgroup": WG, "path": "DESK\\MATS", "code": "MELAMINE",
        "codsc": "密集板", "part_path": "DESK\\PARTS\\TOP",
        "stdqty": 1.0, "stdpar": 1.05,
    },

    # ── 木腳 × 4 ─────────────────────────────────────────
    {
        "workgroup": WG, "path": "DESK\\LEG", "code": "WLEG",
        "codsc": "木腳", "part_path": "DESK\\PARTS\\LEG",
        "stdqty": 4.0, "stdpar": 4.0,
    },
    # ── 金屬腳 × 4 ───────────────────────────────────────
    {
        "workgroup": WG, "path": "DESK\\LEG", "code": "MLEG",
        "codsc": "金屬腳", "part_path": "DESK\\PARTS\\LEG",
        "stdqty": 4.0, "stdpar": 4.0,
    },
    # ── 鋼管腳 × 4 ───────────────────────────────────────
    {
        "workgroup": WG, "path": "DESK\\LEG", "code": "SLEG",
        "codsc": "鋼管腳", "part_path": "DESK\\PARTS\\LEG",
        "stdqty": 4.0, "stdpar": 4.0,
    },
    # ── U型腳 × 2（U型腳每支包含兩腳） ──────────────────
    {
        "workgroup": WG, "path": "DESK\\LEG", "code": "ULEG",
        "codsc": "U型腳", "part_path": "DESK\\PARTS\\LEG",
        "stdqty": 2.0, "stdpar": 2.0,
    },

    # ── 橫樑 × 2（材質不影響橫樑，這裡以通用 TOP 路徑掛）
    {
        "workgroup": WG, "path": "DESK\\PARTS", "code": "BEAM",
        "codsc": "橫樑", "part_path": "DESK\\PARTS\\BEAM",
        "stdqty": 2.0, "stdpar": 2.0,
    },
    # ── 螺絲組 × 1套 ─────────────────────────────────────
    {
        "workgroup": WG, "path": "DESK\\PARTS", "code": "SCREW",
        "codsc": "螺絲組", "part_path": "DESK\\PARTS\\SCREW",
        "stdqty": 1.0, "stdpar": 1.0,
    },
]


# ──────────────────────────────────────────────────────────────
# 補充 ordspe：橫樑與螺絲組的採購成本
# ──────────────────────────────────────────────────────────────

ORDSPE_PARTS_DATA: list[dict] = [
    {"workgroup": WG, "path": "DESK\\PARTS", "code": "BEAM",  "codsc": "橫樑",  "compri": 80.0},
    {"workgroup": WG, "path": "DESK\\PARTS", "code": "SCREW", "codsc": "螺絲組", "compri": 25.0},
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

        # ordspe（選項 + 部件採購成本）
        for row in ORDSPE_DATA + ORDSPE_PARTS_DATA:
            db.add(Ordspe(**row))

        # ordqty
        for row in ORDQTY_DATA:
            db.add(Ordqty(**row))

        db.commit()
        print(
            f"[seed] 已灌入範例資料："
            f"ordspd={len(ORDSPD_DATA)} 筆，"
            f"ordspe={len(ORDSPE_DATA)+len(ORDSPE_PARTS_DATA)} 筆，"
            f"ordqty={len(ORDQTY_DATA)} 筆。"
        )
    except Exception as exc:
        db.rollback()
        print(f"[seed] 灌入失敗：{exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
