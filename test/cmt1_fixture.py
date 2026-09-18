from __future__ import annotations

from pathlib import Path

from agent.order_logic import parse_ordqty, parse_ordspe, parse_ordspd, parse_ordstr


TEST_DIR = Path(__file__).parent


def load_cmt1_fixture() -> dict[str, list[dict]]:
    return {
        "ordstr": parse_ordstr((TEST_DIR / "ordstr.txt").read_text(encoding="utf-8")),
        "ordspe": parse_ordspe((TEST_DIR / "ordspe.txt").read_text(encoding="utf-8")),
        "ordqty": parse_ordqty((TEST_DIR / "ordqty.txt").read_text(encoding="utf-8")),
        "ordspd": parse_ordspd((TEST_DIR / "ordspd.txt").read_text(encoding="utf-8")),
    }


def current_selections() -> dict[str, dict]:
    """現行 60*180 美耐皿桌面＋45寬色紙木腳；不複製歷史快照值。"""
    codes = {
        r"CMT1\A001": "C005",
        r"CMT1\A001\S004": "C001",
        r"CMT1\A001\S005\S001": "C001",
        r"CMT1\A001\S005\S007": "C001",
        r"CMT1\A001\S010": "C001",
        r"CMT1\A001\S015": "C001",
        r"CMT1\B001": "C001",
        r"CMT1\B001\S004": "C001",
        r"CMT1\B001\S005\S008": "C001",
    }
    return {path: {"path": path, "code": code, "line_qty": 1} for path, code in codes.items()}
