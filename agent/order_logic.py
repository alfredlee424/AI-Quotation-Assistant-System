"""CMT1 訂單 TXT 資料的解析、關聯與歷史快照驗證。

這個模組不負責從目前主檔重新計算歷史訂單。``ord.txt`` 已經是訂單
明細快照；主檔資料只用來驗證規格、結構與用量規則是否仍然存在。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PRODUCT_PREFIX = "CMT1"


def _text(value: str | None) -> str:
    return (value or "").strip()


def _float(value: str, *, file_name: str, line_no: int) -> float:
    try:
        return float(value.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            f"{file_name} 第 {line_no} 列數字欄位無效：{value!r}"
        ) from exc


def _rows(text: str, expected: int, file_name: str) -> Iterable[tuple[int, list[str]]]:
    for line_no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        columns = raw.rstrip("\r\n").split("\t")
        if len(columns) != expected:
            raise ValueError(
                f"{file_name} 第 {line_no} 列應有 {expected} 欄，實際為 {len(columns)} 欄"
            )
        yield line_no, [_text(value) for value in columns]


def parse_ord(text: str, file_name: str = "ord.txt") -> list[dict[str, Any]]:
    """解析正式訂單明細：13 欄，保留 ord.txt 的歷史數值。"""
    result = []
    for line_no, c in _rows(text, 13, file_name):
        result.append({
            "line_no": line_no,
            "workgroup": c[0], "ref_no": c[1], "path": c[2],
            "qty": _float(c[3], file_name=file_name, line_no=line_no),
            "spc_code": c[4], "spdsc": c[5],
            "stdqty": _float(c[6], file_name=file_name, line_no=line_no),
            "stdpar": _float(c[7], file_name=file_name, line_no=line_no),
            "compri": _float(c[8], file_name=file_name, line_no=line_no),
            "abndat": c[9], "abntim": c[10], "usrno": c[11], "prgno": c[12],
        })
    return result


def parse_ordstr(text: str, file_name: str = "ordstr.txt") -> list[dict[str, Any]]:
    """解析產品結構邊表。"""
    result = []
    for line_no, c in _rows(text, 16, file_name):
        result.append({
            "line_no": line_no, "workgroup": c[0], "pathf": c[1], "pathc": c[2],
            "optnof": c[3], "optnoc": c[4], "must_chose": c[5],
            "has_name": c[6], "has_inname": c[7],
            "seq": int(c[8]) if c[8] else None, "dmark": c[9],
            "adddate": c[10], "addusrno": c[11], "usrno": c[12],
            "prgno": c[13], "abndat": c[14], "abntim": c[15],
        })
    return result


def parse_ordspe(text: str, file_name: str = "ordspe.txt") -> list[dict[str, Any]]:
    """解析規格價格檔。"""
    result = []
    for line_no, c in _rows(text, 11, file_name):
        result.append({
            "line_no": line_no, "workgroup": c[0], "path": c[1], "code": c[2],
            "codsc": c[3], "compri": _float(c[4], file_name=file_name, line_no=line_no),
            "adddate": c[5], "addusrno": c[6], "abndat": c[7], "abntim": c[8],
            "usrno": c[9], "prgno": c[10],
        })
    return result


def parse_ordspd(text: str, file_name: str = "ordspd.txt") -> list[dict[str, Any]]:
    """解析選項類別定義檔。"""
    result = []
    for line_no, c in _rows(text, 11, file_name):
        result.append({
            "line_no": line_no, "workgroup": c[0], "kind": c[1], "optno": c[2],
            "optdesc": c[3], "code": c[4], "adddate": c[5], "addusrno": c[6],
            "abndat": c[7], "abntim": c[8], "usrno": c[9], "prgno": c[10],
        })
    return result


def parse_ordqty(text: str, file_name: str = "ordqty.txt") -> list[dict[str, Any]]:
    """解析 BOM 用量規則；實際檔案的第 4 欄 codsc 可為空白。"""
    result = []
    for line_no, c in _rows(text, 13, file_name):
        result.append({
            "line_no": line_no, "workgroup": c[0], "path": c[1], "code": c[2],
            "codsc": c[3] or None, "part_path": c[4],
            "stdqty": _float(c[5], file_name=file_name, line_no=line_no),
            "stdpar": _float(c[6], file_name=file_name, line_no=line_no),
            "adddate": c[7], "addtime": c[8], "abndat": c[9], "abntim": c[10],
            "usrno": c[11], "prgno": c[12],
        })
    return result


@dataclass
class Cmt1OrderData:
    orders: list[dict[str, Any]]
    structure: list[dict[str, Any]]
    options: list[dict[str, Any]]
    categories: list[dict[str, Any]]
    quantities: list[dict[str, Any]]

    def order_refs(self) -> list[str]:
        return sorted({row["ref_no"] for row in self.orders})

    def validate(self, ref_no: str | None = None) -> list[str]:
        """回傳問題清單；不改寫訂單的歷史欄位。"""
        warnings: list[str] = []
        orders = [r for r in self.orders if ref_no is None or r["ref_no"] == ref_no]
        option_map = {(r["workgroup"], r["path"], r["code"]): r for r in self.options}
        quantity_map = {(r["workgroup"], r["path"], r["code"]): r for r in self.quantities}
        edge_paths = {(r["workgroup"], r["pathf"], r["pathc"]) for r in self.structure}
        known_paths = {key[1] for key in option_map} | {key[1] for key in quantity_map}
        order_by_path = {(r["ref_no"], r["path"]): r for r in orders}
        for row in orders:
            line = row["line_no"]
            path = row["path"]
            if not path.startswith(f"{PRODUCT_PREFIX}\\"):
                warnings.append(f"ord.txt 第 {line} 列 path 非 CMT1：{path}")
                continue
            if row["spc_code"]:
                option = option_map.get((row["workgroup"], path, row["spc_code"]))
                if option is None:
                    warnings.append(f"ord.txt 第 {line} 列找不到 ordspe：{path}/{row['spc_code']}")
                elif option["codsc"] != row["spdsc"]:
                    warnings.append(f"ord.txt 第 {line} 列規格名稱已漂移：{path}/{row['spc_code']}")
                path_parts = path.split("\\")
                root_path = "\\".join(path_parts[:2]) if len(path_parts) > 1 else path
                root_row = order_by_path.get((row["ref_no"], root_path))
                driver_code = (root_row or {}).get("spc_code") or row["spc_code"]
                quantity = quantity_map.get((row["workgroup"], path, driver_code))
                if quantity is None and path in known_paths:
                    warnings.append(f"ord.txt 第 {line} 列找不到 ordqty：{path}/{driver_code}")
                elif quantity is not None:
                    if row["stdqty"] != quantity["stdqty"] or row["stdpar"] != quantity["stdpar"]:
                        warnings.append(
                            f"ord.txt 第 {line} 列用量快照與 ordqty 不同："
                            f"{path}/{driver_code}"
                        )
            if path != PRODUCT_PREFIX and not any(p[1] == path or p[2] == path for p in edge_paths):
                warnings.append(f"ord.txt 第 {line} 列 path 不在 ordstr 結構樹：{path}")
        return warnings


def load_cmt1_order_data(directory: str | Path = "test") -> Cmt1OrderData:
    """從五個實際 TXT 檔載入 CMT1 資料。"""
    base = Path(directory)
    read = lambda name: (base / name).read_text(encoding="utf-8")
    return Cmt1OrderData(
        orders=parse_ord(read("ord.txt")),
        structure=parse_ordstr(read("ordstr.txt")),
        options=parse_ordspe(read("ordspe.txt")),
        categories=parse_ordspd(read("ordspd.txt")),
        quantities=parse_ordqty(read("ordqty.txt")),
    )
