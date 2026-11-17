"""以 CMTA 主檔匯出（docs/ord*.txt，含 CMT1 與 CMTA）驗證會議桌工單可完整對到主檔並算出成本。"""
import unittest
from decimal import Decimal
from pathlib import Path

from agent.order_logic import parse_ordqty, parse_ordspd, parse_ordspe, parse_ordstr
from local_quote.catalog import build_catalog
from local_quote.dynamic import analyze

DOCS = Path(__file__).resolve().parent.parent / "docs"
A, B = r"CMTA\A001", r"CMTA\B001"

ORDER = """(CMTA)
列席平直60*150------10
主席平直60*180------2
1/4圓60*60------2
列席 每桌1個40*13.5銀鋁單掀
主席 每桌1個40*13.5黑鋁單掀
主席及列席 每桌1個40*15木單掀
列席 每桌1個L型走線槽
列席 每桌1個桌面方孔(大)
主席 每桌1個雙層走線槽
主席 每桌1個桌面方孔(小)
主席及列席 每桌1個單邊置物板
主席及列席 每桌1個桌面6公分圓孔
主席及列席 隔板40高 隔板靠單
主席及列席 隔板40高 808白橡木
第3項：隔板40高 隔板置中
第3項：隔板40高 808白橡木
共用:
每桌1個6cm圓孔
規格 45寬環式腳
808白橡木
2317胡桃美耐板
白橡漆
3.5夾板
腳2317胡桃美耐板
腳3.5夾板"""

# 依正式報價單 QU26A30001-01001~01003（ordopt1）逐項配置的輸入。
FORMAL_ORDER = """(CMTA)
列席平直60*150------10
主席平直60*180------2
1/4圓60*60------2
列席 每桌1個40*13.5銀鋁單掀
主席 每桌1個40*13.5黑鋁單掀
主席及列席 每桌1個40*15木單掀
列席 每桌1個L型走線槽
列席 每桌1個桌面方孔(大)
主席 每桌1個雙層走線槽
主席 每桌1個桌面方孔(小)
主席及列席 每桌1個單邊置物板
主席及列席 每桌1個桌面6公分圓孔
主席及列席 隔板40高 隔板靠單
第3項：隔板40高 隔板置中
第1項：黑色漆
第2項：櫻桃漆
第3項：櫻桃漆
第1項：下板 4.75MDF
第2項：下板 3.5夾板
第3項：下板 4.75MDF
第1項：隔板40高 932楊胡桃
第1項：隔板40高 2317胡桃美耐板
第1項：隔板40高 4*8*3.6夾板
第1項：隔板40高 4*8*3mm MDF白橡紙
第2項：隔板40高 808白橡木
第2項：隔板40高 2317胡桃美耐板
第2項：隔板40高 4*8*3mm MDF
第2項：隔板40高 4*8*3mm MDF紅木紙
第3項：隔板40高 808白橡木
第3項：隔板40高 4*8*3mm MDF白橡紙
第1項：55寬環式腳
第2項：45寬環式腳
第3項：45寬環式腳
第1項：腳817胡桃木
第1項：腳2317胡桃美耐板
第1項：腳4*8*3mm MDF
第1項：腳4*8*3mm MDF紅木紙
第1項：腳保護膜
第2項：腳808白橡木
第2項：腳2317胡桃美耐板
第2項：腳4*8*3mm MDF
第2項：腳4*8*3mm MDF白橡紙
第2項：腳保護膜
第3項：腳340紅木
共用:
每桌1個6cm圓孔
808白橡木
2317胡桃美耐板
3.5夾板
第1項：腳美耐皿裁切
第1項：腳色紙貼合
第2項：美耐皿裁切
第3項：美耐皿裁切
第3項：美耐皿貼合
第3項：腳美耐板裁切
第3項：腳美耐板貼合"""

# 與 FORMAL_ORDER 相同配置的簡寫：品項名稱當對象、「部位：A、B」、「每桌1個：A、B」。
SHORT_ORDER = """(CMTA)
列席 平直60*150------10
主席 平直60*180------2
轉角 1/4圓60*60------2
共用：每桌1個6cm圓孔、808白橡木、2317胡桃美耐板、3.5夾板
主席及列席 每桌1個：40*15木單掀、單邊置物板、桌面6公分圓孔
主席及列席：隔板40高 隔板靠單
列席 每桌1個：40*13.5銀鋁單掀、L型走線槽、桌面方孔(大)
列席：黑色漆、下板 4.75MDF、55寬環式腳
列席 隔板40高：932楊胡桃、2317胡桃美耐板、4*8*3.6夾板、4*8*3mm MDF白橡紙
列席 腳：817胡桃木、2317胡桃美耐板、4*8*3mm MDF、4*8*3mm MDF紅木紙、保護膜、美耐皿裁切、色紙貼合
主席 每桌1個：40*13.5黑鋁單掀、雙層走線槽、桌面方孔(小)
主席：櫻桃漆、下板 3.5夾板、45寬環式腳、美耐皿裁切
主席 隔板40高：808白橡木、2317胡桃美耐板、4*8*3mm MDF、4*8*3mm MDF紅木紙
主席 腳：808白橡木、2317胡桃美耐板、4*8*3mm MDF、4*8*3mm MDF白橡紙、保護膜
轉角：櫻桃漆、下板 4.75MDF、45寬環式腳、隔板40高 隔板置中、美耐皿裁切、美耐皿貼合
轉角 隔板40高：808白橡木、4*8*3mm MDF白橡紙
轉角 腳：340紅木、美耐板裁切、美耐板貼合"""


def load_master():
    return {
        "ordstr": parse_ordstr((DOCS / "ordstr.txt").read_text(encoding="utf-8")),
        "ordspe": parse_ordspe((DOCS / "ordspe.txt").read_text(encoding="utf-8")),
        "ordqty": parse_ordqty((DOCS / "ordqty.txt").read_text(encoding="utf-8")),
        "ordspd": parse_ordspd((DOCS / "ordspd.txt").read_text(encoding="utf-8")),
    }


@unittest.skipUnless((DOCS / "ordstr.txt").exists(), "docs/ord*.txt 主檔匯出不存在")
class CmtaMasterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # quo_rate 以 1 計，金額即純成本，方便與正式報價逐項核對。
        cls.cat = build_catalog("103", dict(prodkind="CMTA", codsc="環式會議桌", quo_rate="1"), load_master())
        cls.children = analyze(ORDER, cls.cat)["children"]

    def code(self, child, path):
        return self.children[child]["selections"].get(path, {}).get("code")

    def unit_cost(self, child):
        c = self.children[child]
        return (Decimal(c["calc"]["known_cost"]) / c["product_qty"]).quantize(Decimal("0.01"))

    def test_whole_order_is_ready_without_operator_choice(self):
        for c in self.children:
            self.assertTrue(c["ready"], [i["message"] for i in c["issues"]])

    def test_roles_pick_their_own_accessories(self):
        self.assertEqual([self.code(i, A + r"\S020") for i in range(3)], ["C001", "C003", None])  # 銀鋁／黑鋁／無
        self.assertEqual([self.code(i, A + r"\S022") for i in range(3)], ["C001", "C002", None])  # L型／雙層
        self.assertEqual([self.code(i, A + r"\S035") for i in range(3)], ["C001", "C002", None])  # 大孔／小孔

    def test_quarter_round_size_and_shape(self):
        self.assertEqual((self.code(2, A), self.code(2, A + r"\S010")), ("C001", "C001"))  # 1/4圓60*60、1/4圓

    def test_plywood_applies_to_upper_and_lower_board(self):
        for i in range(3):
            self.assertEqual((self.code(i, A + r"\S005\S000\S006"), self.code(i, A + r"\S005\S007")), ("C001", "C001"))

    def test_partition_height_and_legs_follow_qualifiers(self):
        self.assertEqual([self.code(i, A + r"\S050") for i in range(3)], ["C002", "C002", "C001"])  # 靠單／靠單／置中
        self.assertTrue(all(self.code(i, A + r"\S049") is None for i in range(3)))
        self.assertEqual(self.code(0, A + r"\S050\S005\S001"), "C002")  # 隔板 808白橡木
        self.assertEqual(self.code(0, A + r"\S005\S001"), "C002")  # 桌面 808白橡木
        self.assertIsNone(self.code(0, B + r"\S005\S001"))  # 未寫「腳」不套到木腳
        self.assertEqual((self.code(0, B), self.code(0, B + r"\S005\S000\S052"), self.code(0, B + r"\S005\S000\S051")),
                         ("C001", "C001", "C001"))  # 45寬環式腳、腳胡桃美耐板、腳3.5夾板

    def test_labor_follows_laminate(self):
        for path in (A + r"\W001\W010", A + r"\W001\W020", B + r"\W001\W010", B + r"\W001\W020"):
            self.assertEqual(self.code(0, path), "C001", path)  # 美耐板裁切／貼合

    def test_unit_costs(self):
        self.assertEqual([self.unit_cost(i) for i in range(3)],
                         [Decimal("9805.08"), Decimal("9705.08"), Decimal("4341.66")])

    def test_unqualified_partition_position_stays_ambiguous(self):
        c = analyze(ORDER.replace("主席及列席 隔板40高 隔板靠單", "主席及列席 隔板靠單"), self.cat)["children"][0]
        self.assertFalse(c["ready"])
        issue = next(i for i in c["issues"] if i["kind"] == "selection" and "隔板靠單" in i["message"])
        self.assertEqual(sorted(x["path"] for x in issue["candidates"]), [A + r"\S049", A + r"\S050"])


@unittest.skipUnless((DOCS / "ordstr.txt").exists(), "docs/ord*.txt 主檔匯出不存在")
class CmtaFormalQuoteTests(unittest.TestCase):
    """與正式報價 ordopt1 核對：正式未稅單價＝明細成本×1.1（取整數）。"""

    @classmethod
    def setUpClass(cls):
        cat = build_catalog("103", dict(prodkind="CMTA", codsc="環式會議桌", quo_rate="1.1"), load_master())
        cls.children = analyze(FORMAL_ORDER, cat)["children"]

    def test_item_and_role_lines_override_shared_names(self):
        sel = [c["selections"] for c in self.children]
        self.assertEqual([s[A + r"\S005\S007"]["code"] for s in sel], ["C002", "C001", "C002"])  # 下板
        self.assertEqual([s[A + r"\S015"]["code"] for s in sel], ["C006", "C001", "C001"])  # 黑色漆／櫻桃漆
        self.assertEqual(sel[0][B + r"\S005\S001"]["code"], "C003")  # 腳817胡桃木 不是桌面面材
        self.assertEqual(sel[0][A + r"\S005\S001"]["code"], "C002")  # 桌面仍為 808白橡木
        self.assertEqual([sel[0][B + r"\W001\W010"]["code"], sel[0][B + r"\W001\W020"]["code"]], ["C002", "C002"])

    def test_upper_lower_board_without_ordqty_counts_one_per_table(self):
        item = next(i for i in self.children[0]["items"] if i["path"] == A + r"\S050\S005\S000\S051")
        self.assertEqual((item["stdqty"], item["stdpar"], item["part_cost"]), ("1", "1", "2600.00"))

    def test_costs_match_formal_detail(self):
        # 正式明細成本：13296.42／10460.42／4071.44。差異只在正式報價的資料缺口：
        # 木腳包裝未選（-50），01001 另含未選隔板68高的材料（+2666）。
        costs = [(Decimal(c["calc"]["known_cost"]) / c["product_qty"]).quantize(Decimal("0.01")) for c in self.children]
        self.assertEqual(costs, [Decimal("13296.42") - 2666 + 50, Decimal("10460.42") + 50, Decimal("4071.44") + 50])
        self.assertTrue(all(c["ready"] for c in self.children))

    def test_short_form_matches_long_form(self):
        cat = build_catalog("103", dict(prodkind="CMTA", codsc="環式會議桌", quo_rate="1.1"), load_master())
        short = analyze(SHORT_ORDER, cat)["children"]
        self.assertEqual([c["shape"] for c in short], ["straight", "straight", "quarter_round"])
        for long_child, short_child in zip(self.children, short):
            self.assertTrue(short_child["ready"], [i["message"] for i in short_child["issues"]])
            self.assertEqual({p: s.get("code") for p, s in short_child["selections"].items()},
                             {p: s.get("code") for p, s in long_child["selections"].items()})
            self.assertEqual(short_child["calc"]["known_cost"], long_child["calc"]["known_cost"])


if __name__ == "__main__":
    unittest.main()
