"""面向使用者的部件名稱；完整路徑與 ERP 代碼仍只作內部識別。"""
from __future__ import annotations
from collections import Counter
import re


def display_text(text: str) -> str:
    """尺寸乘號不是 Markdown 強調符號；資料庫原文不改寫。"""
    return re.sub(r"(?<=\d)\s*[*＊xXｘ×]\s*(?=\d)", "×", text)


def short_part(label: str) -> str:
    parts = label.split("／")
    if len(parts) == 1:
        return label
    root = parts[0].removesuffix("尺寸")
    leaf = parts[-1].replace("顏色材質", "材質")
    return leaf if leaf.startswith(root) else root + leaf


def path_label(path: str, labels: dict[str, str]) -> str:
    parts = path.split("\\")[1:]
    names = []
    for part in parts:
        name = labels.get(part)
        names.append(name if name and name != part else "未命名部件")
    return "／".join(names) or "產品規格"


def candidate_label(candidate: dict, *, compact: bool = False) -> str:
    name = candidate.get("codsc") or "未命名規格"
    part = candidate.get("display_path") or candidate.get("optdesc")
    if compact:
        name = display_text(name)
        part = short_part(part) if part else None
    return f"{name}（{part}）" if part else name


def menu_labels(candidates: list[dict]) -> list[str]:
    names = [display_text(c.get("codsc") or "未命名規格") for c in candidates]
    counts = Counter(names)
    return [candidate_label(c, compact=True) if counts[name] > 1 else name
            for c, name in zip(candidates, names)]


def open_question(question: str, labels: dict[str, str]) -> str:
    """呈現一個具體問題，不把模型提供的金額或技術內容當作報價。"""
    if re.search(r"[$＄€¥]|價格|報價|成本|單價|金額|折扣|稅額|https?://|<|>|SQL", question, re.I):
        return "請補充尚未確認的規格或每件配件用量。"
    for path in sorted(labels, key=len, reverse=True):
        question = question.replace(path, short_part(labels[path]))
    question = re.sub(r"[A-Za-z0-9]+(?:\\[A-Za-z0-9]+)+", "指定部件", question)
    return display_text(" ".join(question.split()))
