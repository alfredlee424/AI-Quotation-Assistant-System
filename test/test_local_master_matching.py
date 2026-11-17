"""以 CMT1 主檔匯出（test/ord*.txt）驗證工單片語能對到實際主檔規格。"""
import unittest

from cmt1_fixture import load_cmt1_fixture
from local_quote.catalog import build_catalog
from local_quote.dynamic import analyze

SOURCE = """彎角3X3.25-----------2
主席平直3X7-------------1
列席平直3X5-------------2
列席平彎3X5-------------2
主席及彎角桌隔板孔位靠單
列席桌隔板孔位置中
共用:
每桌1個銀色單掀鋁毛刷13.5X40及1孔6公分圓孔
桌下走線/腳2片不挖走線孔
面808白橡/噴白橡
腳白橡紙"""


class MasterMatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cat = build_catalog("103", dict(prodkind="CMT1", codsc="會議桌", quo_rate="1.3"), load_cmt1_fixture())
        cls.child = analyze(SOURCE, cat)["children"][1]

    def requirement(self, kind, part=None):
        return next(r for r in self.child["normalized_requirements"]
                    if r["kind"] == kind and (part is None or r["part"] == part))

    def assertSelected(self, requirement, path, code):
        self.assertEqual(requirement["status"], "matched")
        self.assertEqual((requirement["selected"]["path"], requirement["selected"]["code"]), (path, code))

    def test_brush_satisfied_by_brush_box_node(self):
        self.assertSelected(self.requirement("box"), r"CMT1\A001\S019\S020", "C001")  # 40*13.5 銀鋁單掀

    def test_material_matches_by_color_code_and_sticker_board(self):
        self.assertSelected(self.requirement("material", "tabletop"), r"CMT1\A001\S005\S001", "C002")  # 808白橡木
        self.assertSelected(self.requirement("material", "legs"), r"CMT1\B001\S005\S008", "C003")  # MDF白橡紙

    def test_unspecified_cable_tray_lists_all_tray_types_for_operator(self):
        routing = self.requirement("routing")
        self.assertEqual([c["codsc"] for c in routing["candidates"]], ["L型走線槽", "雙層走線槽"])
        self.assertIsNone(routing["selected"])


class InputSyntaxTests(unittest.TestCase):
    """合成主檔：角色選擇、只寫代碼的類別標頭、整段主檔名稱、工費依面材、1/4圓尺寸。"""

    def setUp(self):
        from test_local_dynamic import fixture
        self.cat = fixture(material=True)
        self.root = r"CMT1\A001"

    def add(self, path, label, options):
        parent, optno = path.rsplit("\\", 1)
        self.cat["nodes"][path] = dict(pathf=parent, pathc=path, optnoc=optno,
                                       optnof=parent.split("\\")[-1], must_chose="Y")
        self.cat["labels"][optno] = label
        self.cat["options"][path] = [dict(path=path, code=c, codsc=d, compri=p) for c, d, p in options]
        self.cat["rules"][path] = []

    def test_role_selectors_and_code_only_category_header(self):
        source = ("(CMT1)\n主席平直70X120-----1\n列席平直70X120-----2\n平直70X120-----1\n"
                  "主席 孔置中\n列席 撕保護膜\n主席及列席 腳粒鎖好")
        result = analyze(source, self.cat)
        self.assertFalse(result["global_issues"])
        texts = [[f["text"] for f in c["source_fragments"] if f["scope"] == "targeted"] for c in result["children"]]
        self.assertEqual(texts, [["孔置中", "腳粒鎖好"], ["撕保護膜", "腳粒鎖好"], []])

    def test_whole_master_name_with_dimension_and_count(self):
        from test_local_dynamic import fixture
        self.cat = fixture()
        box = self.root + r"\S019\S020"
        self.cat["options"][box].append(dict(path=box, code="BLK", codsc="40*13.5黑鋁單掀", compri="800"))
        result = analyze("平直70X120-----2\n配件：每桌1個40*13.5黑鋁單掀", self.cat)
        c = result["children"][0]
        self.assertEqual(c["selections"][box]["code"], "BLK")
        self.assertEqual(next(i for i in c["items"] if i["path"] == box)["qty"], "2")
        self.assertEqual(c["selections"][self.root]["code"], "D120")  # 40*13.5 不是桌面尺寸
        self.assertTrue(c["ready"], c["issues"])

    def test_cut_labor_follows_selected_laminate(self):
        self.add(self.root + r"\W010", "裁切費", [("C1", "美耐板裁切", "200"), ("C2", "美耐皿裁切", "150")])
        result = analyze("平直70X120-----1\n共用：白美耐板", self.cat)
        c = result["children"][0]
        self.assertEqual(c["selections"][self.root + r"\W010"]["code"], "C1")
        result = analyze("平直70X120-----1\n共用：817胡桃木", self.cat)
        self.assertEqual(result["children"][0]["selections"][self.root + r"\W010"]["code"], "C2")

    def test_quarter_round_shape_picks_quarter_round_size(self):
        self.cat["options"][self.root] += [dict(path=self.root, code="Q60", codsc="1/4圓60*60", compri="0"),
                                           dict(path=self.root, code="S60", codsc="60*60", compri="0")]
        codes = [analyze(text, self.cat)["children"][0]["selections"].get(self.root, {}).get("code")
                 for text in ("1/4圓60*60-----1", "平直60*60-----1")]
        self.assertEqual(codes, ["Q60", "S60"])

    def test_same_name_on_several_required_nodes_selects_each(self):
        self.add(self.root + r"\S006", "上板", [("C1", "3.5夾板", "280"), ("C2", "4.75MDF", "200")])
        self.add(self.root + r"\S007", "下板", [("C1", "3.5夾板", "280"), ("C2", "4.75MDF", "200")])
        for line in ("3.5夾板", "規格 3.5夾板"):
            c = analyze("平直70X120-----1\n共用：" + line, self.cat)["children"][0]
            self.assertEqual([c["selections"].get(self.root + s, {}).get("code") for s in (r"\S006", r"\S007")],
                             ["C1", "C1"], line)

    def test_alternative_nodes_need_node_label_qualifier(self):
        for optno, label in (("S050", "隔板40高"), ("S049", "隔板68高")):
            self.add(self.root + "\\" + optno, label, [("C1", "隔板置中", "0"), ("C2", "隔板靠單", "0")])
            self.cat["nodes"][self.root + "\\" + optno]["must_chose"] = ""
        c = analyze("平直70X120-----1\n共用：隔板靠單", self.cat)["children"][0]
        self.assertNotIn(self.root + r"\S050", c["selections"])
        self.assertTrue(any(i["kind"] == "selection" for i in c["issues"]))
        c = analyze("平直70X120-----1\n共用：隔板40高 隔板靠單", self.cat)["children"][0]
        self.assertEqual(c["selections"][self.root + r"\S050"]["code"], "C2")
        self.assertNotIn(self.root + r"\S049", c["selections"])


if __name__ == "__main__":
    unittest.main()
