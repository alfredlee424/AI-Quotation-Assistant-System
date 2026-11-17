"""對話式報價（app.py）離線測試：以 docs/ord*.txt 主檔匯出與 logs/sem.txt 驗證一次貼上與一問一答。"""
from decimal import Decimal
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from agent.order_logic import parse_ordqty, parse_ordspd, parse_ordspe, parse_ordstr
from local_quote.catalog import build_catalog
from local_quote.backend import LocalDatabase
from local_quote.chat import ChatSession
from local_quote.dynamic_storage import DynamicStore
from local_quote.storage import StorageError
from test_local_storage import SETTINGS, fixture_schema

ROOT = Path(__file__).resolve().parent.parent
DOCS, SEM = ROOT / "docs", ROOT / "logs" / "sem.txt"
B_PACKING, DIVIDER_68 = r"CMTA\B001\W001\W050", r"CMTA\A001\S049"
# 與本機資料庫 invdoc 相同：兩個類別、quo_rate 皆為 1。
CATEGORIES = [dict(prodkind="CMT1", codsc="環式會議桌", quo_rate="1"),
              dict(prodkind="CMTA", codsc="侯義興環式會議桌", quo_rate="1")]

# 一問一答訂製：每則訊息一個元素；最終結果須與 sem.txt 的一次貼上相同。
STEPS = [
    "我要訂侯義興環式會議桌",
    "列席 平直60*150 10張",
    "1",            # 桌面／美耐板顏色 → 2317胡桃美耐板
    "3.5夾板",      # 上板
    "4.75MDF",      # 下板
    "黑色漆",       # 桌緣噴漆
    "每桌1個：40*15木單掀、單邊置物板、桌面6公分圓孔",
    "每桌1個：40*13.5銀鋁單掀、L型走線槽、桌面方孔(大)",
    "808白橡木",
    "55寬環式腳",
    "腳：817胡桃木、2317胡桃美耐板、4*8*3mm MDF、4*8*3mm MDF紅木紙、保護膜、美耐皿裁切、色紙貼合",
    "每桌1個6cm圓孔",
    "隔板40高 隔板靠單",
    "隔板40高：932楊胡桃、2317胡桃美耐板、4*8*3.6夾板、4*8*3mm MDF白橡紙",
    "隔板68高：隔板靠單、3.5夾板、2317胡桃美耐板、874灰檀木、4*8*3 MDF胡桃紙",
    "主席 平直60*180 2張",
    "2317胡桃美耐板", "3.5夾板", "3.5夾板", "櫻桃漆",
    "每桌1個：40*13.5黑鋁單掀、雙層走線槽、桌面方孔(小)",
    "每桌1個：40*15木單掀、單邊置物板、桌面6公分圓孔",
    "808白橡木",
    "桌面 美耐皿裁切",
    "45寬環式腳",
    "腳：808白橡木、2317胡桃美耐板、4*8*3mm MDF、4*8*3mm MDF白橡紙、保護膜",
    "每桌1個6cm圓孔",
    "隔板40高 隔板靠單",
    "隔板40高：808白橡木、2317胡桃美耐板、4*8*3mm MDF、4*8*3mm MDF紅木紙",
    "轉角 1/4圓60*60-----2",
    "1", "3.5夾板", "4.75MDF", "櫻桃漆",
    "808白橡木\n美耐皿裁切、美耐皿貼合\n45寬環式腳\n腳：340紅木、美耐板裁切、美耐板貼合\n每桌1個6cm圓孔\n"
    "隔板40高 隔板置中\n隔板40高：808白橡木、4*8*3mm MDF白橡紙",
]


def load_tables():
    return {name: parser((DOCS / f"{name}.txt").read_text(encoding="utf-8"))
            for name, parser in (("ordstr", parse_ordstr), ("ordspe", parse_ordspe),
                                 ("ordqty", parse_ordqty), ("ordspd", parse_ordspd))}


class OfflineMaster:
    """MasterSource 的離線替身：只提供 categories／catalog，資料來自主檔匯出。"""
    def __init__(self):
        self.tables = load_tables()

    def categories(self):
        return [dict(c) for c in CATEGORIES]

    def catalog(self, code):
        category = next(c for c in CATEGORIES if c["prodkind"] == code)
        return build_catalog("103", category, self.tables, allow_code_variants=True)


def sem_parts():
    text = SEM.read_text(encoding="utf-8")
    source = text.split("============== Input")[1].split("============= Result")[0].strip()
    expected = {}
    # Result 之後可能還有其他區段（例如「正式報價單」）；只取 13 欄明細列。
    result = text.split("============= Result")[1].split("\n=====")[0]
    for line in result.strip().splitlines():
        f = line.split("\t")
        expected.setdefault(f[1], {})[f[2]] = dict(code=f[4], codsc=f[5], stdqty=Decimal(f[6]),
                                                    stdpar=Decimal(f[7]), compri=Decimal(f[8]))
    return source, [expected[ref] for ref in sorted(expected)]


def session():
    master = OfflineMaster()
    return ChatSession(master.categories(), master.catalog, document_id="test-document")


def isolated_store(case):
    """隔離的記憶體 SQLite（既有七表 fixture）；不連真資料庫。"""
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    fixture_schema(engine)
    case.addCleanup(engine.dispose)
    return engine, DynamicStore(LocalDatabase(SETTINGS, engine=engine, testing=True))


def selections(chat):
    return [{i["path"]: (i["code"], i["codsc"], i["qty"], i["part_cost"]) for i in c["items"]} for c in chat.children]


@unittest.skipUnless((DOCS / "ordstr.txt").exists() and SEM.exists(), "主檔匯出或 logs/sem.txt 不存在")
class ChatQuoteTests(unittest.TestCase):
    def test_category_is_decided_before_anything_else(self):
        chat = session()
        self.assertIn("請先決定產品類別", chat.messages[0]["content"])
        reply = chat.send("列席 平直60*150 10張")
        self.assertIn("還沒決定產品類別", reply)
        self.assertIsNone(chat.quote())
        self.assertEqual(chat.lines, [])
        self.assertIn("CMTA 侯義興環式會議桌", chat.send("2"))
        self.assertEqual(chat.category["prodkind"], "CMTA")
        other = session()
        other.send("環式會議桌")  # 完全相符的名稱優先於較長名稱
        self.assertEqual(other.category["prodkind"], "CMT1")

    def test_one_shot_matches_sem_result(self):
        source, expected = sem_parts()
        chat = session()
        reply = chat.send(source)
        self.assertIn("已新增 列席 ×10、主席 ×2、轉角 ×2", reply)
        view = chat.quote()
        self.assertEqual([(i["name"], i["qty"], i["ready"], i["gaps"]) for i in view["items"]],
                         [("列席", 10, True, []), ("主席", 2, True, []), ("轉角", 2, True, [])])
        sem_cost = Decimal(0)
        for child, item, rows in zip(chat.children, view["items"], expected):
            got = {i["path"]: i for i in child["items"]}
            self.assertEqual(set(got), set(rows))  # 明細列與 sem.txt 完全同一組 path
            for path, row in rows.items():
                if path == B_PACKING:
                    # 已知差異 1：sem.txt 木腳包裝費未選（空白、0 元）；規則對唯一必選候選自動帶入 50 元。
                    self.assertEqual((row["code"], row["compri"], got[path]["code"], got[path]["compri"]),
                                     ("", 0, "C001", "50.0"))
                elif path == DIVIDER_68:
                    # 已知差異 2：sem.txt 隔板68高本身未選位置，輸入指定「隔板靠單」；0 元，不影響金額。
                    self.assertEqual((row["code"], got[path]["code"], got[path]["compri"]), ("", "C002", "0.0"))
                elif row["code"]:
                    self.assertEqual((got[path]["code"], got[path]["codsc"], Decimal(got[path]["compri"])),
                                     (row["code"], row["codsc"], row["compri"]), path)
                else:
                    self.assertTrue(got[path]["structural"], path)
            unit = sum(r["stdqty"] / r["stdpar"] * r["compri"] for r in rows.values())
            sem_cost += unit * child["product_qty"]
            self.assertEqual(Decimal(item["amount"]),
                             ((unit + 50) * child["product_qty"]).quantize(Decimal("0.01")))
        totals = view["totals"]
        self.assertEqual((totals["complete"], totals["untaxed"], totals["tax_rate"], totals["tax"], totals["total"]),
                         (True, "162727.89", "0.05", "8136.39", "170864.28"))
        self.assertEqual(Decimal(totals["untaxed"]) - sem_cost.quantize(Decimal("0.01")), 700)  # 50 元 × 14 桌
        self.assertEqual(len(chat.detail_rows()), sum(len(rows) for rows in expected))

    def test_step_by_step_reaches_the_same_quote(self):
        source, _ = sem_parts()
        one_shot = session()
        one_shot.send(source)
        chat = session()
        for step in STEPS:
            reply = chat.send(step)
            self.assertNotIn("未加入", reply, step)
            self.assertNotIn("衝突", reply, step)
        self.assertIn("✅ 全部 3 項已完整", reply)
        self.assertEqual([chat.name(c) for c in chat.children], ["列席", "主席", "轉角"])
        self.assertEqual(selections(chat), selections(one_shot))
        self.assertEqual(chat.quote()["totals"], one_shot.quote()["totals"])
        self.assertEqual(len(chat.messages), 1 + 2 * len(STEPS))

    def test_questions_are_asked_one_at_a_time_and_gaps_are_listed(self):
        chat = session()
        chat.send("CMTA")
        reply = chat.send("列席 平直60*150 10張")
        self.assertIn("尚有缺漏：列席 4 項", reply)
        self.assertIn("【列席】請選擇 桌面／美耐板顏色", reply)
        self.assertEqual(reply.count("請選擇"), 1)
        view = chat.quote()
        self.assertFalse(view["totals"]["complete"])
        self.assertEqual(view["items"][0]["gaps"], ["缺規格：桌面／美耐板顏色", "缺規格：桌面／上板",
                                                    "缺規格：桌面／下板", "缺規格：桌面／桌緣噴漆"])
        self.assertEqual([r["label"] for r in view["items"][0]["rows"] if r["missing"]],
                         ["美耐板顏色", "上板", "下板", "桌緣噴漆"])
        reply = chat.send("客訂美耐板顏色")
        self.assertIn("【列席】桌面／美耐板顏色＝客訂美耐板顏色", reply)
        self.assertIn("請選擇 桌面／上板", reply)

    def test_unnamed_items_are_numbered_and_later_lines_follow_the_latest_item(self):
        chat = session()
        chat.send("(CMTA)\n平直60*150-----1")
        chat.send("櫻桃漆")
        reply = chat.send("平直60*180 3張")
        self.assertIn("已新增 第2項 ×3", reply)
        self.assertEqual([(i["name"], i["qty"]) for i in chat.quote()["items"]], [("第1項", 1), ("第2項", 3)])
        reply = chat.send("黑色漆")
        self.assertIn("【第2項】", reply)
        paint = r"CMTA\A001\S015"
        self.assertEqual([c["selections"][paint]["codsc"] for c in chat.children], ["櫻桃漆", "黑色漆"])
        self.assertIn("已新增 主席桌 ×2", chat.send("主席桌 平直60*180 2張"))
        chat.send("主席桌：胡桃漆")
        self.assertEqual(chat.children[2]["selections"][paint]["codsc"], "胡桃漆")
        self.assertIn("第1項：櫻桃漆", chat.source_text)

    def test_single_line_unknown_is_rejected_and_conflict_replaces_previous_line(self):
        chat = session()
        chat.send("CMTA\n列席 平直60*150 10張")
        before = chat.source_text
        reply = chat.send("火星木皮")
        self.assertIn("主檔找不到「火星木皮」，未加入", reply)
        self.assertEqual(chat.source_text, before)
        chat.send("共用：櫻桃漆")
        self.assertIn("已套用到【列席】", chat.send("黑色漆"))  # 品項條件優先於共用
        paint = r"CMTA\A001\S015"
        self.assertEqual(chat.children[0]["selections"][paint]["codsc"], "黑色漆")
        reply = chat.send("胡桃漆")  # 改變心意：取代同一品項先前的單一條件
        self.assertIn("已取代先前條件：第1項：黑色漆", reply)
        self.assertEqual(chat.children[0]["selections"][paint]["codsc"], "胡桃漆")
        self.assertFalse([i for i in chat.children[0]["issues"] if i["kind"] == "conflict"])
        self.assertIn("已復原上一步", chat.send("復原"))
        self.assertEqual(chat.children[0]["selections"][paint]["codsc"], "黑色漆")

    def test_loose_sentence_fixes_an_answer_and_adds_new_specs(self):
        """使用者回報的案例：答錯上板後，用一句口語同時修正並補上其他規格。"""
        chat = session()
        chat.send("CMTA\n列席 平直60*150 10張")
        chat.send("1")
        chat.send("2")  # 上板誤選 4.75MDF
        reply = chat.send("上版改 3.5. 下版用 4.75 + 黑色漆")
        self.assertNotIn("主檔找不到", reply)
        for understood in ("已理解為：上板 3.5夾板", "已理解為：下板 4.75MDF", "已理解為：黑色漆"):
            self.assertIn(understood, reply)
        self.assertIn("✅ 全部 1 項已完整", reply)
        chosen = chat.children[0]["selections"]
        self.assertEqual([chosen[p]["codsc"] for p in (r"CMTA\A001\S005\S000\S006", r"CMTA\A001\S005\S007",
                                                       r"CMTA\A001\S015")], ["3.5夾板", "4.75MDF", "黑色漆"])
        self.assertIn("已取代先前條件：第1項：下板 4.75MDF", chat.send("下板換成3.5的"))
        self.assertEqual(chat.children[0]["selections"][r"CMTA\A001\S005\S007"]["codsc"], "3.5夾板")
        chat.send("復原")
        chat.send("復原")  # 整句口語只算一步
        self.assertEqual(chat.source_text, "列席 平直60*150-----10")

    def test_loose_sentence_never_guesses(self):
        chat = session()
        chat.send("CMTA\n列席 平直60*150 10張")
        before = chat.source_text
        reply = chat.send("腳用胡桃 + 亂寫一通")  # 腳的「胡桃」有多個規格；另一句完全不認得
        self.assertIn("不確定是哪一個腳的規格", reply)
        self.assertIn("817胡桃木", reply)
        self.assertIn("主檔找不到「亂寫一通」，未加入", reply)
        self.assertEqual(chat.source_text, before)
        reply = chat.send("列席：上版改3.5，桌緣噴漆換白橡漆")  # 有指定對象的口語
        self.assertIn("已理解為：列席：上板 3.5夾板", reply)
        self.assertEqual(chat.children[0]["selections"][r"CMTA\A001\S015"]["codsc"], "白橡漆")
        self.assertIn("主檔找不到「火星木皮」", chat.send("火星木皮"))

    def test_add_word_before_item_and_part_with_first_spec_before_colon(self):
        """使用者回報：『加 主席 …』名稱應為主席；『隔板40高 隔板靠單：A、B』應整行歸隔板40高。"""
        chat = session()
        chat.send("CMTA\n平直60*150-----10")
        reply = chat.send("加 主席 平直60*180------2")
        self.assertIn("已新增 主席 ×2", reply)
        self.assertNotIn("客製化新件", reply)
        view = chat.quote()["items"]
        self.assertEqual([(i["name"], i["description"]) for i in view], [("第1項", "平直60*150"), ("主席", "平直60*180")])
        self.assertIn("已新增 第3項 ×1", chat.send("再加一張 平直60*120 1張"))
        chat.send("復原")
        reply = chat.send("每桌1個6cm圓孔、808白橡木、2317胡桃美耐板、3.5夾板\n"
                          "櫻桃漆、下板 3.5夾板、45寬環式腳、美耐皿裁切\n"
                          "隔板40高 隔板靠單：808白橡木、2317胡桃美耐板、4*8*3mm MDF、4*8*3mm MDF紅木紙\n"
                          "腳：808白橡木、2317胡桃美耐板、4*8*3mm MDF、4*8*3mm MDF白橡紙、保護膜")
        self.assertNotIn("⚠️", reply)
        self.assertTrue(chat.children[1]["ready"], chat.quote()["items"][1]["gaps"])  # 主席整項完整
        chosen = chat.children[1]["selections"]
        divider = r"CMTA\A001\S050"
        self.assertEqual([chosen[divider + tail]["codsc"] for tail in
                          ("", r"\S005\S001", r"\S005\S000\S051", r"\S005\S008")],
                         ["隔板靠單", "808白橡木", "4*8*3mm MDF", "4*8*3mm MDF紅木紙"])
        self.assertIn("第2項：隔板40高 隔板靠單\n第2項：隔板40高：808白橡木", chat.source_text)
        solo = session()
        solo.send("CMTA\n列席 平直60*150 10張")
        solo.send("列席 隔板68高 隔板置中：874灰檀木")  # 前面有對象時同樣拆成兩行
        self.assertEqual(solo.children[0]["selections"][r"CMTA\A001\S049"]["codsc"], "隔板置中")
        self.assertEqual(solo.children[0]["selections"][r"CMTA\A001\S049\S005\S001"]["codsc"], "874灰檀木")

    def test_part_specific_spec_wins_over_unqualified_name_on_the_same_item(self):
        """使用者回報：同一項目寫「3.5夾板」（上板、下板都適用）又寫「下板 4.75MDF」，不應衝突。"""
        chat = session()
        chat.send("CMTA\n平直60*150-----10")
        reply = chat.send("每桌1個6cm圓孔、808白橡木、2317胡桃美耐板、3.5夾板\n黑色漆、下板 4.75MDF、55寬環式腳\n"
                          "腳：817胡桃木、2317胡桃美耐板、4*8*3mm MDF、4*8*3mm MDF紅木紙、保護膜、美耐皿裁切、色紙貼合")
        self.assertIn("✅ 全部 1 項已完整", reply)
        chosen = chat.children[0]["selections"]
        self.assertEqual((chosen[r"CMTA\A001\S005\S000\S006"]["codsc"], chosen[r"CMTA\A001\S005\S007"]["codsc"]),
                         ("3.5夾板", "4.75MDF"))
        # 兩個都指明下板、規格不同：仍是真正的衝突，不猜。
        other = session()
        other.send("CMTA\n平直60*150-----10")
        other.send("下板 3.5夾板、下板 4.75MDF\n黑色漆")
        self.assertTrue([i for i in other.children[0]["issues"] if i["kind"] == "conflict"])

    def test_each_conversation_writes_its_own_log_file(self):
        with TemporaryDirectory() as directory:
            master = OfflineMaster()
            chat = ChatSession(master.categories(), master.catalog, log_dir=directory)
            self.assertEqual(list(Path(directory).iterdir()), [])  # 還沒有使用者訊息：不建立空檔
            chat.send("CMTA")
            first = chat.log_path
            self.assertRegex(first.name, r"^chat_quote_\d{8}_\d{6}(_\d+)?\.log$")
            chat.send("列席 平直60*150 10張")
            chat.send("火星木皮")
            chat.undo()
            text = first.read_text(encoding="utf-8")
            self.assertIn("助理：請先決定產品類別", text)
            self.assertIn("使用者：列席 平直60*150 10張", text)
            self.assertIn("助理：主檔找不到「火星木皮」，未加入", text)
            self.assertIn("操作：（按鈕）復原上一步", text)
            self.assertIn("狀態：類別 CMTA｜列席 ×10 缺4項", text)
            self.assertEqual(len(re.findall(r"使用者：", text)), 3)  # 每則只記一次
            chat.send("重新開始")  # 新的對話 → 新的紀錄檔（同一秒則加序號）
            self.assertIn("使用者：重新開始", first.read_text(encoding="utf-8"))
            chat.send("2")
            self.assertNotEqual(chat.log_path, first)
            self.assertEqual(len(list(Path(directory).glob("chat_quote_*.log"))), 2)
            self.assertNotIn("火星木皮", chat.log_path.read_text(encoding="utf-8"))  # 不含上一場對話的內容
            self.assertEqual(first.read_text(encoding="utf-8").count("使用者：2"), 0)
        blocked = ChatSession(master.categories(), master.catalog, log_dir="/dev/null/不能建立")
        self.assertIn("CMTA", blocked.send("CMTA"))  # 寫檔失敗不影響對話

    def test_statement_overrides_an_earlier_answer(self):
        chat = session()
        chat.send("CMTA\n列席 平直60*150 10張")
        chat.send("1")
        chat.send("3.5夾板")
        chat.send("4.75MDF")  # 下板以回答選定
        lower = r"CMTA\A001\S005\S007"
        self.assertEqual(chat.children[0]["selections"][lower]["codsc"], "4.75MDF")
        chat.send("下板 3.5夾板")  # 之後明講的條件取代先前回答
        self.assertEqual(chat.children[0]["selections"][lower]["codsc"], "3.5夾板")
        self.assertFalse([i for i in chat.children[0]["issues"] if i["kind"] in {"conflict", "stale_input"}])

    def test_accessory_without_quantity_asks_how_many_per_table(self):
        chat = session()
        chat.send("CMTA\n列席 平直60*150 10張")
        for answer in ("1", "3.5夾板", "4.75MDF", "黑色漆"):
            chat.send(answer)
        reply = chat.send("40*15木單掀")
        self.assertIn("每桌幾個", reply)
        self.assertFalse(chat.quote()["totals"]["complete"])
        reply = chat.send("2")
        self.assertIn("每桌 2 個", reply)
        self.assertIn("✅ 全部 1 項已完整", reply)
        item = next(i for i in chat.children[0]["items"] if i["path"] == r"CMTA\A001\S025")
        self.assertEqual((item["qty"], item["part_cost"]), ("20", "30000.00"))

    def test_shared_conditions_may_come_before_the_first_item(self):
        chat = session()
        chat.send("CMTA")
        reply = chat.send("共用：櫻桃漆、3.5夾板、2317胡桃美耐板")
        self.assertIn("已記下條件", reply)
        self.assertNotIn("⚠️", reply)
        self.assertEqual(chat.quote()["items"], [])
        self.assertIn("✅ 全部 1 項已完整", chat.send("平直60*150 2張"))
        self.assertEqual(chat.children[0]["selections"][r"CMTA\A001\S015"]["codsc"], "櫻桃漆")
        self.assertIn("未加入", session().send("CMTA\n櫻桃漆"))  # 沒有品項也沒有範圍：不加入

    def test_save_gives_each_item_its_own_number_and_locks_the_session(self):
        engine, store = isolated_store(self)
        chat = session()
        chat.send("CMTA\n列席 平直60*150 10張")
        with self.assertRaisesRegex(ValueError, "尚有缺漏"):
            chat.save(store, actor="amy")
        chat.undo()
        chat.send(sem_parts()[0])
        with self.assertRaises(StorageError):
            chat.save(store, actor="名字超過八個字元的人")
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM ordqdt_ai")).scalar(), 0)
        saved = chat.save(store, actor="amy")
        refs = [saved[c["id"]]["ref_no"] for c in chat.children]
        self.assertRegex(refs[0], r"^DQ\d{14}_001$")  # 同一時間前綴，流水號依項目順序連續
        self.assertEqual(refs, [refs[0][:-3] + f"{n:03d}" for n in (1, 2, 3)])
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT ref_no, seq_no, path, usrno FROM ordqdt_ai ORDER BY ref_no, seq_no")).all()
            documents = conn.execute(text("SELECT ref_no FROM quote_snapshot_document ORDER BY ref_no")).scalars().all()
        self.assertEqual(documents, sorted(refs))
        for child, ref, expected in zip(chat.children, refs, sem_parts()[1]):
            own = [r for r in rows if r.ref_no == ref]
            self.assertEqual([r.path for r in own], [i["path"] for i in child["items"]])
            self.assertEqual(len(own), len(expected))  # 每張的明細列數與 sem.txt 對應單號相同
            self.assertEqual(own[0].seq_no, "00001")
            self.assertEqual(store.read(saved[child["id"]]["confirmation_id"])["ref_no"], ref)
        self.assertEqual({r.usrno for r in rows}, {"amy"})
        self.assertEqual([i["ref_no"] for i in chat.quote()["items"]], refs)
        self.assertEqual(list(dict.fromkeys(r["ref_no"] for r in chat.detail_rows())), refs)
        self.assertTrue(all(ref in chat.messages[-1]["content"] for ref in refs))
        lines = chat.source_text
        self.assertIn("不能再修改", chat.send("列席：白橡漆"))
        self.assertIn("不能復原", chat.send("復原"))
        self.assertEqual(chat.source_text, lines)
        with self.assertRaisesRegex(ValueError, "已保存"):
            chat.save(store, actor="amy")
        chat.send("重新開始")
        self.assertFalse(chat.saved_items)

    def test_partial_failure_keeps_saved_items_and_retry_saves_only_the_rest(self):
        engine, store = isolated_store(self)
        chat = session()
        chat.send(sem_parts()[0])
        real, calls = store._allocate_ref, []

        def second_fails(conn, batch_time=None):
            calls.append(1)
            if len(calls) == 2:
                raise StorageError("模擬失敗")
            return real(conn, batch_time)

        with patch.object(store, "_allocate_ref", side_effect=second_fails):
            with self.assertRaises(StorageError):
                chat.save(store, actor="amy")
        first, second = chat.children[0]["id"], chat.children[1]["id"]
        self.assertEqual(list(chat.saved_items), [first])
        self.assertFalse(chat.saved)
        self.assertTrue(chat.quote()["locked"])
        self.assertIn("不能再修改", chat.send("主席：白橡漆"))  # 部分保存後同樣鎖定
        first_ref, pending_uuid = chat.saved_items[first]["ref_no"], chat._frozen[second][1].confirmation_id
        with self.assertRaisesRegex(ValueError, "同一操作人"):
            chat.save(store, actor="bob")
        saved = chat.save(store, actor="amy")
        self.assertEqual(saved[first]["ref_no"], first_ref)
        self.assertEqual([saved[c["id"]]["ref_no"] for c in chat.children],
                         [first_ref[:-3] + f"{n:03d}" for n in (1, 2, 3)])  # 重試後仍與第一張連號
        self.assertEqual(saved[second]["confirmation_id"], pending_uuid)  # 同一固定 UUID 重試
        self.assertTrue(chat.saved)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM quote_snapshot_document")).scalar(), 3)

    def test_other_category_header_is_ignored_and_reset_clears_everything(self):
        chat = session()
        chat.send("CMTA")
        reply = chat.send("(CMT1)\n平直60*150-----1")
        self.assertIn("已忽略標頭", reply)
        self.assertEqual(chat.category["prodkind"], "CMTA")
        self.assertEqual(len(chat.children), 1)
        self.assertIn("請先決定產品類別", chat.send("重新開始"))
        self.assertIsNone(chat.category)
        self.assertIsNone(chat.quote())


UI_SCRIPT = "from local_quote.chat_ui import main\nmain()"


@unittest.skipUnless((DOCS / "ordstr.txt").exists() and SEM.exists(), "主檔匯出或 logs/sem.txt 不存在")
class ChatUiTests(unittest.TestCase):
    """真 Streamlit AppTest；主檔為離線替身，不連資料庫。"""

    def setUp(self):
        from streamlit.testing.v1 import AppTest
        from local_quote import chat_ui
        self.engine, self.store = isolated_store(self)
        services = patch.object(chat_ui, "services", return_value=(OfflineMaster(), self.store))
        services.start()
        self.addCleanup(services.stop)
        logs = TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        self.logs = Path(logs.name)
        log_dir = patch.object(chat_ui, "LOG_DIR", self.logs)  # 測試不寫入專案 logs/
        log_dir.start()
        self.addCleanup(log_dir.stop)
        self.app = AppTest.from_string(UI_SCRIPT, default_timeout=30).run()

    def say(self, text):
        self.app.chat_input[0].set_value(text).run()
        self.assertFalse(self.app.exception)
        return self.app

    def metrics(self):
        return {m.label: m.value for m in self.app.metric}

    def test_one_shot_paste_shows_items_details_and_totals(self):
        self.assertIn("請先決定產品類別", self.app.chat_message[0].markdown[0].value)
        self.assertFalse(self.app.dataframe)
        app = self.say(sem_parts()[0])
        summary = app.dataframe[0].value
        self.assertEqual(summary["項目"].tolist(), ["列席", "主席", "轉角"])
        self.assertEqual(summary["數量"].tolist(), [10, 2, 2])
        self.assertEqual(summary["小計（未稅）"].tolist(), ["133,464.17", "21,020.83", "8,242.89"])
        self.assertEqual(summary["狀態"].tolist(), ["", "", ""])
        self.assertEqual(self.metrics(), {"未稅金額": "162,727.89", "稅率": "5%", "總額（含稅）": "170,864.28"})
        switch = app.radio[0]
        self.assertEqual(switch.options, ["列席", "主席", "轉角"])
        first = app.dataframe[1].value
        self.assertIn("60*150", first["規格"].tolist())
        switch.set_value(app.session_state.chat.children[2]["id"]).run()
        third = self.app.dataframe[1].value
        self.assertIn("1/4圓60*60", third["規格"].tolist())
        self.assertNotIn("60*150", third["規格"].tolist())

    def test_step_by_step_marks_gaps_then_completes(self):
        self.say("CMTA")
        app = self.say("列席 平直60*150 10張")
        self.assertEqual(app.dataframe[0].value["狀態"].tolist(), ["缺 4 項"])
        detail = app.dataframe[1].value
        self.assertEqual(detail.loc[detail["缺漏"] != "", "部位"].str.strip("　").tolist(),
                         ["美耐板顏色", "上板", "下板", "桌緣噴漆"])
        reds = "".join(m.value for m in app.markdown if "color:" in m.value)
        self.assertIn("缺規格：桌面／桌緣噴漆", reds)
        self.assertIn("不是完整報價", reds)
        for answer in ("1", "3.5夾板", "4.75MDF", "黑色漆"):
            app = self.say(answer)
        self.assertEqual(app.dataframe[0].value["狀態"].tolist(), [""])
        self.assertFalse([m for m in app.markdown if "color:" in m.value])
        self.assertEqual(self.metrics()["未稅金額"], "26,812.50")
        app.button(key="chat_undo").click().run()
        self.assertEqual(self.app.dataframe[0].value["狀態"].tolist(), ["缺 1 項"])
        self.app.button(key="chat_reset").click().run()
        self.assertFalse(self.app.dataframe)
        self.assertEqual(len(self.app.chat_message), 1)
        logs = list(self.logs.glob("chat_quote_*.log"))
        self.assertEqual(len(logs), 1)  # 這場對話一個紀錄檔
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("使用者：列席 平直60*150 10張", text)
        self.assertIn("操作：（按鈕）復原上一步", text)
        self.assertIn("操作：（按鈕）重新開始", text)

    def test_save_button_needs_complete_quote_actor_and_confirmation(self):
        self.say("CMTA")
        app = self.say("列席 平直60*150 10張")
        self.assertTrue(app.button(key="chat_save").disabled)
        self.assertTrue(app.checkbox(key="chat_confirm").disabled)
        for answer in ("1", "3.5夾板", "4.75MDF", "黑色漆"):
            app = self.say(answer)
        self.assertTrue(app.button(key="chat_save").disabled)
        app.text_input(key="chat_actor").set_value("amy").run()
        self.assertTrue(self.app.button(key="chat_save").disabled)
        self.app.checkbox(key="chat_confirm").check().run()
        self.assertFalse(self.app.button(key="chat_save").disabled)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM ordqdt_ai")).scalar(), 0)
        self.say("主席 平直60*180 2張")
        for answer in ("1", "3.5夾板", "3.5夾板", "櫻桃漆"):
            self.say(answer)
        self.assertNotIn("報價單號", [c for c in self.app.dataframe[0].value["報價單號"] if c] or ["無"])
        self.assertFalse(self.app.checkbox(key="chat_confirm").value)  # 內容變動後須重新勾選
        self.assertEqual(self.app.text_input(key="chat_actor").value, "amy")  # 操作人不因對話而清空
        self.assertTrue(self.app.button(key="chat_save").disabled)
        self.app.checkbox(key="chat_confirm").check().run()
        self.app.button(key="chat_save").click().run()
        self.assertFalse(self.app.exception)
        self.assertFalse([e.value for e in self.app.error])
        with self.engine.connect() as conn:
            refs = conn.execute(text("SELECT DISTINCT ref_no FROM ordqdt_ai ORDER BY ref_no")).scalars().all()
        self.assertEqual(len(refs), 2)  # 兩個項目、兩個單號
        self.assertEqual(sorted(self.app.dataframe[0].value["報價單號"].tolist()), refs)
        for ref in refs:
            self.assertIn(ref, self.app.success[0].value)
            self.assertIn(ref, self.app.chat_message[-1].markdown[0].value.replace("\\", ""))
        self.assertFalse([b for b in self.app.button if b.key == "chat_save"])
        app = self.say("列席：白橡漆")
        self.assertIn("不能再修改", app.chat_message[-1].markdown[0].value)


if __name__ == "__main__":
    unittest.main()
