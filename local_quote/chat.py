"""對話式報價核心（無 DB、UI、LLM）：把對話累積成工單原文，規則一律交給 dynamic.analyze。

ChatSession(categories, load_catalog)
  categories   : [{prodkind, codsc}]，由呼叫端唯讀查出。
  load_catalog : callable(prodkind) -> detached catalog（MasterSource.catalog）。
send(text) -> 助理回覆；同時記入 messages。quote() -> 報價單檢視（項目、明細、缺漏、合計）。

流程
====
1. 先決定產品類別：輸入編號／代碼／名稱，或直接貼上含「(CMTA)」標頭的整份工單。
2. 之後每則訊息的每一行都轉成工單原文的一行：
   * 品項行「名稱 外型尺寸-----N」（亦接受「…… N張」）新增一個項目。
   * 已標示範圍者（共用：／第N項：／主席：／主席及列席：…）原樣加入。
   * 未標示範圍者歸「目前項目」：剛新增的項目，或助理正在詢問的項目。
3. 仍有缺漏時逐題詢問（一問一答）。回答編號或候選名稱即為人工選擇（analyze 的
   choices）；配件缺每桌數量時回答數字（overrides.line_qty）。
4. 單行訊息若主檔找不到，先以「口語寬鬆比對」重試：依 ＋／，／、／句點 拆成數句，
   每句「部位＋動詞（改／換／用…）＋規格片段」對主檔名稱做唯一比對（上版→上板、3.5→3.5夾板），
   唯一才改寫成正式條件；多個可能則列出請使用者指明，絕不猜。仍找不到、或與既有條件
   衝突且無法取代者不加入並說明。整份貼上則全部保留，缺漏由報價單紅字提示。

5. save(store, actor=...)：全部完整後逐項保存，每個項目各自一個單號（DynamicStore，DQ 單號，
   寫入 ordqdt_ai 與 quote_snapshot_document）。任一項保存後即鎖定，須「重新開始」才能建立新報價。

一次貼上與一問一答走同一個 analyze；金額、明細沒有第二套規則。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import re
from uuid import uuid4

from . import dynamic
from . import semantic_normalization as normalization
from .catalog import option_identity
from .dynamic_presentation import report_rows
from .dynamic_v1 import _ITEM, _norm

RESET_WORDS = {"重新開始", "重來", "reset"}
UNDO_WORDS = {"復原", "上一步", "undo"}
MAX_LISTED = 30
_HEADER = re.compile(r"(.*?)[（(]([A-Za-z0-9_-]+)[）)]")
# 「列席 平直60*150 10張」→「列席 平直60*150-----10」；需含尺寸，且單位限成品量詞。
_LOOSE_ITEM = re.compile(r"(.*\d\s*[xX×*]\s*\d.*?)\s*[,，]?\s*(?:共|[xX×])?\s*([1-9]\d*)\s*(?:張|桌|台|組)")
_SCOPED = re.compile(r"(?:共用|第[1-9]\d*項|局部|配件|每桌配件|每(?:桌|件))")
_ACTOR = "chat"
# 品項行前的口語（加／再加一張／新增…）不是品項名稱。
_ADD_WORDS = re.compile(r"(?:再|另外|還要|請|幫我)*(?:加上|追加|新增|增加|加)(?:一[張項個桌])?\s*[：:]?\s*(.+)")
# 口語寬鬆比對：分句符號（小數點不算）、部位後的動詞／助詞、句尾語助詞。
_CLAUSE_SPLIT = re.compile(r"\s*(?:[+＋，,、;；。]|\.(?=\s|$)|\s(?:和|跟|還有|另外|然後)\s?)\s*")
_FILLER = re.compile(r"^(?:\s|的|要|請|都|也|就|改成|改為|改用|換成|更改為|改|換|用|選|為|是|：|:|=|＝)+")
_TAIL = re.compile(r"(?:\s|的|吧|喔|哦|謝謝|就好|即可)+$")


def _money(value):
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class ChatSession:
    def __init__(self, categories, load_catalog, *, document_id=None):
        self.categories = [dict(prodkind=str(c["prodkind"]), codsc=str(c["codsc"])) for c in categories]
        self._load = load_catalog
        self._document_id = document_id
        self.messages = []
        self._clear()
        self._say(self._greeting())

    # ── 狀態 ──────────────────────────────────────────────
    def _clear(self):
        self.document_id = self._document_id or uuid4().hex
        self.category = None
        self.catalog = None
        self.lines = []
        self.choices = {}
        self.overrides = {}
        self.analysis = None
        self.pending = None
        self.focus = None
        self.saved_items = {}  # child_id -> {ref_no, confirmed_at, confirmation_id, actor}
        self._frozen = {}      # child_id -> (actor, FrozenDynamicQuote)；重試沿用同一 UUID
        self._batch_time = None  # 同一份報價各項目共用的單號時間前綴
        self._history = []

    def _snapshot(self):
        return deepcopy(dict(lines=self.lines, choices=self.choices, overrides=self.overrides,
                             analysis=self.analysis, pending=self.pending, focus=self.focus))

    def _restore(self, snapshot):
        for name, value in deepcopy(snapshot).items():
            setattr(self, name, value)
        self._frozen = {}

    def _say(self, text):
        self.messages.append(dict(role="assistant", content=text))
        return text

    @property
    def saved(self):
        """全部項目都已保存時為 True；部分保存（中途失敗）時為 False，但工作階段同樣鎖定。"""
        return bool(self.children) and all(c["id"] in self.saved_items for c in self.children)

    def _refs(self):
        return "、".join(f"{self.name(c)} {self.saved_items[c['id']]['ref_no']}"
                        for c in self.children if c["id"] in self.saved_items)

    @property
    def source_text(self):
        return "\n".join(self.lines)

    @property
    def children(self):
        return self.analysis["children"] if self.analysis else []

    def _child(self, child_id):
        return next((c for c in self.children if c["id"] == child_id), None)

    def name(self, child):
        """使用者給的名稱（主席／列席／轉角…）；沒有名稱或名稱重複時用「第N項」區分。"""
        label = child.get("label")
        if not label:
            return f"第{child['ordinal']}項"
        same = [c for c in self.children if c.get("label") == label]
        return label if len(same) == 1 else f"{label}（第{child['ordinal']}項）"

    # ── 對話入口 ──────────────────────────────────────────
    def send(self, text):
        text = (text or "").strip()
        if not text:
            return ""
        self.messages.append(dict(role="user", content=text))
        return self._say(self._handle(text))

    def reset(self):
        self._clear()
        self.messages = []
        return self._say(self._greeting())

    def undo(self):
        return self._say(self._undo())

    def _undo(self):
        if self.saved_items:
            return f"此報價已保存（{self._refs()}），不能復原。"
        if not self._history:
            return "沒有可復原的步驟。"
        self._restore(self._history.pop())
        return "已復原上一步。\n" + (self._status() if self.lines else self._item_hint())

    def _handle(self, text):
        if text.lower() in RESET_WORDS:
            self._clear()
            return "已清空。\n" + self._greeting()
        if text.lower() in UNDO_WORDS:
            return self._undo()
        if self.saved_items:
            return f"此報價已保存（{self._refs()}），不能再修改。要建立新報價請輸入「重新開始」。"
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        notes = []
        if self.category is None:
            found, consumed = self._detect(lines[0])
            if found is None:
                return "還沒決定產品類別，請先選擇。\n" + self._category_list()
            try:
                catalog = dynamic._catalog(self._load(found["prodkind"]))
            except Exception:
                return f"無法讀取 {found['prodkind']} 的主檔，請確認資料庫連線與主檔內容後再試。"
            self.category, self.catalog = found, catalog
            notes.append(f"產品類別：{found['prodkind']} {found['codsc']}")
            if consumed:
                lines = lines[1:]
            if not lines:
                return "\n".join(notes + [self._item_hint()])
        return self._apply(lines, notes)

    # ── 產品類別 ──────────────────────────────────────────
    def _category_list(self):
        return "\n".join(f"{n}. {c['prodkind']} {c['codsc']}" for n, c in enumerate(self.categories, 1))

    def _greeting(self):
        return ("請先決定產品類別（輸入編號、代碼或名稱）：\n" + self._category_list()
                + "\n也可以直接貼上含「(類別代碼)」標頭的整份工單，一次產出報價。")

    def _item_hint(self):
        return ("請輸入品項與數量，例如「列席 平直60*150 10張」或「平直60*150-----10」"
                "（名稱可省略，省略時以第1項、第2項稱呼）；也可以一次貼上整份工單。")

    def _detect(self, line):
        """回傳 (類別, 此行是否只用來指定類別)。"""
        text = line.strip()
        codes = {c["prodkind"].lower(): c for c in self.categories}
        header = _HEADER.fullmatch(text)
        if header and header[2].lower() in codes:
            return codes[header[2].lower()], True
        if text.lower() in codes:
            return codes[text.lower()], True
        if text.isdigit() and 1 <= int(text) <= len(self.categories):
            return self.categories[int(text) - 1], True
        consumed = not re.search(r"\d\s*[xX×*]\s*\d", text)
        mentioned = [c for c in self.categories
                     if re.search(r"(?<![A-Za-z0-9])" + re.escape(c["prodkind"]) + r"(?![A-Za-z0-9])", text, re.I)]
        if len(mentioned) == 1:
            return mentioned[0], consumed
        named = sorted((c for c in self.categories if _norm(c["codsc"]) in _norm(text)),
                       key=lambda c: len(c["codsc"]), reverse=True)
        if named and (len(named) == 1 or len(named[0]["codsc"]) > len(named[1]["codsc"])):
            return named[0], consumed
        return None, False

    # ── 行 → 工單原文 ─────────────────────────────────────
    def _scope(self, line, items):
        """以解析器本身判斷該行範圍；只帶入既有品項行，避免受前文未結束的共用區塊影響。"""
        source = dict(text="\n".join(items + [line]), document_id="probe")
        return normalization.parse(source, self.catalog)[1][-1]["scope"]

    def _target(self):
        child = self._child(self.pending["child_id"]) if self.pending else None
        child = child or self._child(self.focus)
        if child is None and self.children:
            child = self.children[-1]
        return child

    # ── 口語寬鬆比對 ──────────────────────────────────────
    def _loose_clause(self, clause):
        """一句口語 → ("ok", 正式條件) ／("ambiguous", 說明)／None（看不懂，原樣交回）。

        只接受對主檔名稱的唯一比對：完全相同優先，其次為唯一包含該片段的名稱。"""
        for text in dict.fromkeys((clause, clause.replace("版", "板"))):
            body, qualifier = text.strip(), None
            labels = {_norm(v): v for v in self.catalog["labels"].values() if v}
            known = {**labels, **{_norm(k): k for k in normalization.NAME_AREAS}}
            for key in sorted(known, key=len, reverse=True):
                if _norm(body).startswith(key) and len(_norm(body)) > len(key):
                    qualifier = known[key]
                    # 依正規化後長度切掉部位詞（原文可能夾空白）。
                    cut = next(n for n in range(1, len(body) + 1) if _norm(body[:n]) == key)
                    body = body[cut:]
                    break
            value = _TAIL.sub("", _FILLER.sub("", body))
            if len(_norm(value)) < 2:
                continue
            pool = []
            for path, options in self.catalog["options"].items():
                area, _, ancestors = normalization.path_traits(self.catalog, path)
                if qualifier and not (normalization.NAME_AREAS.get(qualifier) == area
                                      or qualifier in ancestors.split("/")):
                    continue
                pool.extend(o["codsc"] for o in options)
            exact = sorted({n for n in pool if _norm(n) == _norm(value)})
            names = exact or sorted({n for n in pool if _norm(value) in _norm(n)})
            if len(names) == 1:
                return "ok", f"{qualifier} {names[0]}" if qualifier else names[0]
            if names:
                where = f"{qualifier}的" if qualifier else ""
                return "ambiguous", (f"「{clause.strip()}」不確定是哪一個{where}規格："
                                     + "、".join(names[:12]) + "。請寫完整名稱" + ("" if qualifier else "或加上部位") + "。")
        return None

    def _loosen(self, line):
        """整行口語拆句並改寫；回傳 [(狀態, 文字)]，沒有任何一句能改寫時回傳 None。"""
        prefix, body = "", line
        head = re.match(r"(.+?[：:])\s*(.+)$", line)
        items = [r["raw"].strip() for r in (self.analysis or {}).get("source_ledger", []) if r["scope"] == "product"]
        if head and self._scope(head[1] + "x", items) in {"shared", "targeted", "local"}:
            prefix, body = head[1], head[2]
        clauses = [c for c in _CLAUSE_SPLIT.split(body) if c and c.strip()]
        results = [self._loose_clause(c) or ("raw", c.strip()) for c in clauses]
        if all(kind == "raw" for kind, _ in results) and len(results) < 2:
            return None
        return [(kind, prefix + text if kind != "ambiguous" else text) for kind, text in results]

    def _apply_loosened(self, clauses, notes):
        """逐句套用（每句都是可被後續取代的單一條件）；整則訊息只算一步復原。"""
        mark = len(self._history)
        for kind, text in clauses:
            if kind == "ambiguous":
                notes.append("⚠️ " + text)
                continue
            reply = self._apply([text], [], loosened=True)
            if "未加入" not in reply:
                notes.append(("已理解為：" if kind == "ok" else "已加入：") + text)
            if reply:
                notes.append(reply)
        del self._history[mark + 1:]
        return "\n".join(notes + ([self._status()] if self.children else [self._item_hint()]))

    def _tidy(self, line):
        """送進解析器前的整理；回傳一或多行。

        * 「加 主席 平直60*180------2」→ 去掉品項行前的「加／再加一張／新增」。
        * 「隔板40高 隔板靠單：A、B」→「隔板40高 隔板靠單」＋「隔板40高：A、B」
          （部位後面先寫一個規格、再用冒號列其他規格）。前面可以有對象（列席／第2項：／共用：）。"""
        added = _ADD_WORDS.fullmatch(line)
        if added and (_ITEM.fullmatch(added[1]) or _LOOSE_ITEM.fullmatch(added[1])):
            return [added[1].strip()]
        if _ITEM.fullmatch(line):
            return [line]
        names = "|".join(map(re.escape, sorted({c["label"] for c in self.children if c.get("label")}
                                               | {"主席", "列席"}, key=len, reverse=True)))
        parts = "|".join(map(re.escape, sorted({v for v in self.catalog["labels"].values() if v}
                                               | set(normalization.NAME_AREAS), key=len, reverse=True)))
        found = re.fullmatch(rf"((?:(?:共用|第[1-9]\d*項|局部)[：:]\s*|(?:{names})(?:及(?:{names}))*[：:\s]\s*)?)"
                             rf"({parts})\s+([^：:、，,]+?)\s*[：:]\s*(\S.*)", line)
        if found and _norm(found[2] + found[3]) not in {_norm(v) for v in self.catalog["labels"].values() if v}:
            scope, part, first, rest = found.groups()
            return [f"{scope}{part} {first}", f"{scope}{part}：{rest}"]
        return [line]

    def _apply(self, lines, notes, loosened=False):
        lines = [tidy for line in lines for tidy in self._tidy(line)]
        before = self._snapshot()
        single = len(lines) == 1
        items = [r["raw"].strip() for r in (self.analysis or {}).get("source_ledger", []) if r["scope"] == "product"]
        group = (self.analysis or {}).get("source_ledger", [{}])[-1].get("group", "default") if self.lines else "default"
        target, new, added, in_block, answered, prefixed = self._target(), [], 0, False, False, False
        for line in lines:
            header = _HEADER.fullmatch(line)
            if header and header[2].lower() in {c["prodkind"].lower() for c in self.categories}:
                if header[2].lower() != self.category["prodkind"].lower():
                    notes.append(f"目前類別是 {self.category['prodkind']}，已忽略標頭「{line}」；要換類別請輸入「重新開始」。")
                continue
            if single and self.pending and self._answer(line, notes):
                answered = True
                continue
            loose = _LOOSE_ITEM.fullmatch(line)
            if loose and not _ITEM.fullmatch(line) and not _SCOPED.match(line):
                line = f"{loose[1].strip()}-----{loose[2]}"
            scope = self._scope(line, items)
            if scope == "product":
                items.append(line)
                added, in_block = added + 1, False
            elif scope == "shared_header":
                in_block = True
            elif scope != "unknown":
                in_block = False
            elif not in_block:
                if added:
                    line = "局部：" + line  # 歸本則訊息剛新增的品項
                elif target is not None and target["group"] == group:
                    line, prefixed = f"第{target['group_ordinal']}項：{line}", True
                else:
                    notes.append(f"尚未有可套用的品項，未加入「{line}」。" + self._item_hint())
                    continue
            new.append(line)
        if not new and not answered:
            return "\n".join(notes) if notes else "沒有可加入的內容。"
        first = len(self.lines) + 1
        self.lines = self.lines + new
        self._refresh()
        if single and new and not added:
            rejected = self._reject_or_supersede(before, first, notes)
            if rejected:
                self._restore(before)
                clauses = None if loosened or "主檔找不到" not in rejected else self._loosen(lines[0])
                return self._apply_loosened(clauses, notes) if clauses else rejected
        self._history.append(before)
        if added:
            created = self.children[-added:]
            self.focus = created[-1]["id"]
            notes.append("已新增 " + "、".join(f"{self.name(c)} ×{c['product_qty']}" for c in created))
        elif prefixed and not loosened:
            notes.append(f"已套用到【{self.name(target)}】")
        if self.children:
            notes.extend(self._new_problems(first))
        return "\n".join(notes if loosened else notes + [self._status()])

    def _inputs(self):
        ids = {p["id"] for p in normalization.parse(dict(text=self.source_text, document_id=self.document_id),
                                                    self.catalog)[0]}
        for mapping in (self.choices, self.overrides):
            for child_id in [k for k in mapping if k not in ids or not mapping[k]]:
                del mapping[child_id]
        return dict(choices=self.choices, overrides=self.overrides)

    def _refresh(self):
        """重新分析；被新條件取代、或已不適用的人工選擇自動移除。"""
        source = dict(text=self.source_text, document_id=self.document_id)
        for _ in range(20):
            self.analysis = dynamic.analyze(source, self.catalog, **self._inputs())
            self._frozen = {}
            dropped = False
            for child in self.analysis["children"]:
                chosen = self.choices.get(child["id"], {})
                for issue in child["issues"]:
                    if issue["kind"] == "stale_input":
                        name, key = issue["id"].split(":", 1)
                        dropped |= getattr(self, name, {}).get(child["id"], {}).pop(key, None) is not None
                    elif issue["kind"] in {"conflict", "invalid_choice"}:
                        key = issue["id"][:-len(":conflict")] if issue["id"].endswith(":conflict") else issue["id"]
                        dropped |= chosen.pop(key, None) is not None
            if not dropped:
                break
        self.pending = None

    @staticmethod
    def _conflicts(analysis):
        return [(c["id"], i["id"]) for c in (analysis or {}).get("children", [])
                for i in c["issues"] if i["kind"] == "conflict"]

    def _reject_or_supersede(self, before, line_no, notes):
        """單行訊息：主檔找不到 → 不加入；與先前單一條件衝突 → 以新條件取代；其餘衝突 → 不加入。"""
        for child in self.children:
            for issue in child["issues"]:
                if issue["kind"] == "custom_required" and (issue.get("source") or {}).get("line") == line_no:
                    text = issue["message"].split("：", 1)[-1]
                    return f"主檔找不到「{text}」，未加入。請確認名稱，或換一種寫法。"
        old = set(self._conflicts(before["analysis"]))
        fresh = [c for c in self._conflicts(self.analysis) if c not in old]
        if not fresh:
            return None
        ledger = self.analysis["source_ledger"]
        earlier = set()
        for child in self.children:
            involved = [i for i in child["issues"] if i["kind"] == "conflict"]
            keys = [i["id"] for i in involved]
            keys += [k for i in involved for sel in child["selections"].values()
                     if i["message"].endswith("：" + sel["path"]) for k in sel["requirements"]]
            keys += [k for r in child["normalized_requirements"] if r["conflict"] for k in [r["id"], *r["conflict"]]]
            earlier.update(int(n) for key in keys for n in re.findall(r"line:(\d+):", key))
        replaceable = [n for n in sorted(earlier) if n < line_no and ledger[n - 1]["scope"] in {"targeted", "local"}
                       and len(ledger[n - 1]["child_ids"]) == 1
                       and not re.search(r"[、，,;；]", self.lines[n - 1].split("：", 1)[-1])]
        if replaceable:
            replaced = [self.lines[n - 1] for n in replaceable]
            for n in replaceable:
                self.lines[n - 1] = ""  # 留空行：不改動其他行號與既有選擇的識別
            self._refresh()
            if not [c for c in self._conflicts(self.analysis) if c not in old]:
                notes.append("已取代先前條件：" + "、".join(replaced))
                return None
        messages = [self._humanize(i["message"]) for c in self.children for i in c["issues"]
                    if (c["id"], i["id"]) in fresh]
        return ("與既有條件衝突，未加入：" + "；".join(dict.fromkeys(messages))
                + "\n若要改掉整份貼上的條件，請用「復原」或「重新開始」。")

    # ── 回答提問 ──────────────────────────────────────────
    def _answer(self, line, notes):
        pending, child = self.pending, self._child(self.pending["child_id"])
        if child is None:
            return False
        if pending["kind"] == "quantity":
            if not re.fullmatch(r"[1-9]\d*", line):
                return False
            stamp = datetime.now(timezone.utc).isoformat()
            self.overrides.setdefault(child["id"], {})[pending["path"]] = dict(
                values={"line_qty": line}, actor=_ACTOR, reason="對話補填每桌數量", time=stamp)
            notes.append(f"【{self.name(child)}】{pending['label']}：每桌 {line} 個")
            return True
        candidates = pending["candidates"]
        if re.fullmatch(r"[1-9]\d*", line) and int(line) <= len(candidates):
            matched = [candidates[int(line) - 1]]
        else:
            matched = [c for c in candidates if _norm(c["codsc"]) == _norm(line)]
        if len(matched) != 1:
            return False
        self.choices.setdefault(child["id"], {})[pending["key"]] = option_identity(self.catalog, matched[0])
        notes.append(f"【{self.name(child)}】{pending['label']}＝{matched[0]['codsc']}")
        return True

    def _question(self):
        ordered = sorted(self.children, key=lambda c: c["id"] != self.focus)
        for child in ordered:
            if child["ready"]:
                continue
            for issue in child["issues"]:
                if issue["kind"] == "selection" and issue["candidates"]:
                    key = issue["id"]
                    label = (self.part(key[5:]) if key.startswith("node:") and key[5:] in self.catalog["nodes"]
                             else "「" + issue["message"].split("：", 1)[-1] + "」")
                    return dict(kind="choice", child_id=child["id"], key=key, label=label,
                                candidates=issue["candidates"])
            for issue in child["issues"]:
                if issue["kind"] == "quantity" and issue["id"].startswith("cost:"):
                    path = issue["id"][5:]
                    item = next(i for i in child["items"] if i["path"] == path)
                    return dict(kind="quantity", child_id=child["id"], path=path,
                                label=f"{self.part(path)}（{item.get('codsc')}）")
        return None

    def _ask(self, question):
        child = self._child(question["child_id"])
        if question["kind"] == "quantity":
            return f"【{self.name(child)}】{question['label']} 每桌幾個？（輸入數字）"
        candidates = question["candidates"]
        one_part = len({c["path"] for c in candidates}) == 1
        listed = [f"{n}. " + (c["codsc"] if one_part else f"{self.part(c['path'])}／{c['codsc']}")
                  for n, c in enumerate(candidates[:MAX_LISTED], 1)]
        if len(candidates) > MAX_LISTED:
            listed.append(f"…共 {len(candidates)} 個，可直接輸入名稱")
        return (f"【{self.name(child)}】請選擇 {question['label']}（輸入編號或名稱）：\n" + "\n".join(listed))

    # ── 回覆內容 ──────────────────────────────────────────
    def part(self, path):
        """精簡部位名稱：略過沒有規格的純容器層（顏色材質／美耐板／工費），例：桌面／隔板40高／上下板。"""
        parts = path.split("\\")
        paths = ["\\".join(parts[:n]) for n in range(2, len(parts) + 1)]
        labels = [self.catalog["labels"][self.catalog["nodes"][p]["optnoc"]] for p in paths
                  if p == path or self.catalog["options"][p]]
        if len(labels) > 1 and labels[0].endswith("尺寸") and len(labels[0]) > 2:
            labels[0] = labels[0][:-2]
        return "／".join(labels)

    def _humanize(self, message):
        """把訊息內的主檔路徑換成部位名稱。"""
        pattern = re.escape(self.catalog["prodkind"]) + r"(?:\\[A-Za-z0-9_]+)+"
        return re.sub(pattern, lambda m: self.part(m[0]) if m[0] in self.catalog["nodes"] else m[0],
                      message)

    def gaps(self, child):
        """單一項目的缺漏說明（報價單紅字）；同一部位只列一次。"""
        missing = [i["path"] for i in child["items"] if not i["selected"]]
        result = ["缺規格：" + self.part(p) for p in missing]
        for issue in child["issues"]:
            key = issue["id"]
            if issue["kind"] == "structure" and key.startswith("missing:"):
                continue
            if issue["kind"] == "selection" and key.startswith("node:") and key[5:] in missing:
                continue
            result.append(self._humanize(issue["message"]))
        return list(dict.fromkeys(result))

    def _new_problems(self, first):
        """本則訊息新增的行所造成、且無法用提問解決的問題。"""
        found = []
        for child in self.children:
            for issue in child["issues"]:
                line = (issue.get("source") or {}).get("line") or issue.get("source_line") or 0
                if line >= first and not (issue["kind"] == "selection" and issue["candidates"]):
                    found.append("⚠️ " + self._humanize(issue["message"]))
        return list(dict.fromkeys(found))[:8]

    def _status(self):
        if not self.children:
            noted = "已記下條件，新增品項後才會套用。\n" if any(self.lines) else ""
            return noted + self._item_hint()
        self.pending = self._question()
        if self.pending:
            self.focus = self.pending["child_id"]
        open_items = [f"{self.name(c)} {len(self.gaps(c))} 項" for c in self.children if not c["ready"]]
        if not open_items:
            totals = self.quote()["totals"]
            return (f"✅ 全部 {len(self.children)} 項已完整：未稅 {totals['untaxed']}，"
                    f"稅額 {totals['tax']}，總額 {totals['total']}。可繼續新增品項或補充條件。")
        lines = ["尚有缺漏：" + "、".join(open_items) + "。"]
        if self.pending:
            lines.append(self._ask(self.pending))
        else:
            lines.append("這些缺漏無法以選項回答，請依報價單紅字補充說明，或輸入「復原」。")
        return "\n".join(lines)

    # ── 報價單檢視 ────────────────────────────────────────
    def quote(self):
        """項目名稱／數量、逐項明細（缺漏標示）、未稅金額、稅率與總額。尚無類別時回傳 None。"""
        if self.category is None:
            return None
        children = self.children
        tax_rate = Decimal(children[0]["calc"]["tax_rate"]) if children else Decimal("0.05")
        items, untaxed = [], Decimal(0)
        for child in children:
            rate = child["calc"].get("quote_rate")
            known = sum((Decimal(i["raw_cost"]) for i in child["items"] if i.get("raw_cost") is not None), Decimal(0))
            amount = known * Decimal(rate) if rate else None
            untaxed += amount or 0
            rows = [dict(depth=i["depth"], label=i["label"], path=i["path"], code=i.get("code"), codsc=i.get("codsc"),
                         qty=i.get("qty"), stdqty=i.get("stdqty"), stdpar=i.get("stdpar"), compri=i.get("compri"),
                         amount=i.get("amount"),
                         missing=None if i["selected"] and i.get("part_cost") is not None else
                         "待選規格" if not i["selected"] else "待補數量／成本") for i in child["items"]]
            head = _ITEM.fullmatch(child["description"].strip())
            description = " ".join(part.strip() for part in (head[1], head[3]) if part.strip())
            if child.get("label") and description.startswith(child["label"]):
                description = description[len(child["label"]):].strip()  # 名稱已在「項目」欄，內容不重複
            items.append(dict(id=child["id"], name=self.name(child), description=description,
                              qty=child["product_qty"], ready=child["ready"], rows=rows, gaps=self.gaps(child),
                              ref_no=self.saved_items.get(child["id"], {}).get("ref_no"),
                              amount=None if amount is None else _money(amount),
                              unit_price=None if amount is None else _money(amount / child["product_qty"])))
        complete = bool(children) and all(c["ready"] for c in children) and not (self.analysis or {}).get("global_issues")
        totals = dict(complete=complete, untaxed=_money(untaxed), tax_rate=str(tax_rate),
                      tax=_money(untaxed * tax_rate), total=_money(untaxed * (1 + tax_rate)))
        return dict(category=dict(self.category), items=items, totals=totals,
                    saved=self.saved, locked=bool(self.saved_items))

    def detail_rows(self):
        """13 欄明細（與 ordqdt_ai／sem.txt 同欄位）；已保存的項目帶入各自單號、保存時間與操作人。"""
        rows = []
        for child in self.children:
            part = report_rows(child, self.catalog)
            saved = self.saved_items.get(child["id"])
            if saved:
                instant = datetime.fromisoformat(saved["confirmed_at"])
                for row in part:
                    row.update(ref_no=saved["ref_no"], abndat=instant.strftime("%Y.%m.%d"),
                               abntim=instant.strftime("%H:%M:%S"), usrno=saved["actor"])
            rows.extend(part)
        return rows

    # ── 保存 ──────────────────────────────────────────────
    def save(self, store, *, actor):
        """每個項目各自一個單號保存（同 ERP 一項一單）。未完整、操作人不合法或已全部保存都拒絕。

        各項目是獨立交易：中途失敗時已保存的項目保留單號，再呼叫一次只補存剩下的項目，
        並沿用同一固定 UUID，不會重複建單。回傳 {child_id: 保存紀錄}。"""
        from .dynamic_storage import prepare_document, validate_actor
        if self.saved:
            raise ValueError("此報價已保存：" + self._refs())
        if (not self.children or self.analysis["global_issues"]
                or not all(c["ready"] for c in self.children)):
            raise ValueError("報價尚有缺漏，不可保存")
        validate_actor(actor)
        if any(v["actor"] != actor for v in self.saved_items.values()):
            raise ValueError("其餘項目須由同一操作人保存：" + next(iter(self.saved_items.values()))["actor"])
        from .storage import _now
        self._batch_time = self._batch_time or _now()  # 各項目單號同一時間前綴、流水號連續；重試也沿用
        for child in self.children:
            cid = child["id"]
            if cid in self.saved_items:
                continue
            if cid not in self._frozen or self._frozen[cid][0] != actor:
                document = dynamic.freeze(self.analysis, cid, actor=actor, reason="對話式報價保存")
                self._frozen[cid] = (actor, prepare_document(document))
            receipt = store.save(self._frozen[cid][1], confirmed=True, current=self.analysis,
                                 trusted_catalog_digest=self.analysis["catalog_digest"],
                                 batch_time=self._batch_time)
            self.saved_items[cid] = dict(ref_no=receipt["ref_no"], confirmed_at=receipt["confirmed_at"],
                                         confirmation_id=receipt["confirmation_id"], actor=actor)
            self._history = []
        self._say(f"已保存到資料庫（ordqdt_ai），每個項目各一個單號：{self._refs()}。要建立新報價請輸入「重新開始」。")
        return deepcopy(self.saved_items)
