"""階段 0：去識別化工單與人工編寫的候選驗收標註，不是解析器。

客戶、日期、單號與樓層已替換；技術用語、尺寸及歧義刻意保留。
不讀取私人原文、ERP 主檔或模型。所有語意標註尚待業務／工程核准，
不能拿本模組的期望值直接當作正式規格、價格或生產指示。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductCandidate:
    key: str
    line: int
    description: str
    dimension_raw: str
    quantity: int
    holes_per_item: int | None = None
    boxes_per_item: int | None = None


@dataclass(frozen=True)
class Requirement:
    key: str
    line: int
    excerpt: str
    kind: str
    targets: tuple[str, ...]
    expectation: str
    certainty: str = "candidate"


@dataclass(frozen=True)
class Clarification:
    key: str
    requirements: tuple[str, ...]
    owner: str
    question: str
    release_evidence: str
    category: str = "needs_confirmation"


@dataclass(frozen=True)
class WorkOrderCase:
    key: str
    family: str
    raw_text: str
    products: tuple[ProductCandidate, ...]
    requirements: tuple[Requirement, ...]
    clarifications: tuple[Clarification, ...]
    # 只對三張角色／數量可列出候選的環式桌設定；不是 ERP 金額預期。
    candidate_totals: tuple[tuple[str, int], ...] = ()
    annotation_status: str = "pending_business_engineering_review"
    approval_evidence: str | None = None
    schema_version: int = 1

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self.raw_text.splitlines())


P = ProductCandidate
R = Requirement
C = Clarification

# 每張單首行為合成來源資訊。scope=order 不表示條件已核准套到每張桌，
# 只表示它是工單層級、仍須依 expectation 與 clarifications 確認的要求。
CASES = (
    WorkOrderCase(
        key="ring_01", family="環式會議桌",
        raw_text="""(1) 20990101-1測試客戶A
彎角70X70削角-------------2  面無孔有桌下走線
平直70X120-----------------1   面1孔13.5X40單掀鋁
平直70X140-----------------6  面1孔13.5X40單掀鋁
平彎70X140-----------------2  面1孔13.5X40單掀鋁
單面置物版
孔靠單/格靠單
817胡/噴胡
腳粒鎖好/撕保護膜""",
        products=(P("p1", 2, "彎角削角", "70X70", 2, 0, 0),
                  P("p2", 3, "平直", "70X120", 1, None, 1),
                  P("p3", 4, "平直", "70X140", 6, None, 1),
                  P("p4", 5, "平彎", "70X140", 2, None, 1)),
        requirements=(
            R("source", 1, "20990101-1測試客戶A", "metadata", ("order",), "單號與客戶不參與尺寸／數量解析", "literal"),
            R("corner", 2, "彎角70X70削角-------------2", "product", ("p1",), "外型、尺寸、削角與尾端數量分開保存"),
            R("no_holes", 2, "面無孔", "negative", ("p1",), "彎角桌面禁止開孔，不等於禁止桌下走線", "literal"),
            R("under_wire", 2, "有桌下走線", "process", ("p1",), "保持桌下走線要求", "literal"),
            R("straight_short", 3, "平直70X120-----------------1", "product", ("p2",), "獨立尺寸與數量"),
            R("box_short", 3, "面1孔13.5X40單掀鋁", "accessory", ("p2",), "每件一組線盒候選；開孔工費包含關係待核准"),
            R("straight_long", 4, "平直70X140-----------------6", "product", ("p3",), "獨立尺寸與數量"),
            R("box_long", 4, "面1孔13.5X40單掀鋁", "accessory", ("p3",), "每件一組線盒候選"),
            R("curved", 5, "平彎70X140-----------------2", "product", ("p4",), "不得與同尺寸平直合併"),
            R("box_curved", 5, "面1孔13.5X40單掀鋁", "accessory", ("p4",), "每件一組線盒候選"),
            R("shelf", 6, "單面置物版", "component", ("order",), "版疑似板；尺寸、數量及套用範圍待確認", "unresolved"),
            R("hole_position", 7, "孔靠單", "drawing", ("order",), "需提供定位依據，不能取消彎角的無孔條件", "unresolved"),
            R("partition_position", 7, "格靠單", "drawing", ("order",), "格的部件定義及圖面待確認", "unresolved"),
            R("surface", 8, "817胡", "material", ("order",), "色號不等於完整基材、板厚及貼面方案", "unresolved"),
            R("paint", 8, "噴胡", "finish", ("order",), "確認噴塗部位與色號", "unresolved"),
            R("feet", 9, "腳粒鎖好", "process", ("order",), "保留鎖腳粒工序，含價政策待核准", "literal"),
            R("film", 9, "撕保護膜", "process", ("order",), "保留撕膜工序，不能靜默略過", "literal"),
        ),
        clarifications=(
            C("scope_and_drawing", ("shelf", "hole_position", "partition_position"), "工程", "置物板與定位適用哪些桌子？", "核准的部件範圍、數量及定位圖面"),
            C("material_and_labor", ("surface", "paint", "feet", "film", "box_short"), "業務／工程", "基材與工序是否已有標準含價規則？", "合法規格、正式用量及工費包含政策"),
        ),
        candidate_totals=(("products", 11), ("boxes", 9)),
    ),
    WorkOrderCase(
        key="ring_02", family="環式會議桌",
        raw_text="""(2)20990102-2測試客戶B/8樓
彎角3X3.25-----------2
主席平直3X7-------------1
列席平直3X5-------------2
列席平彎3X5-------------2
主席及彎角桌隔板孔位靠單
列席桌隔板孔位置中
每桌1個銀色單掀鋁毛刷13.5X40及1孔6公分圓孔
桌下走線/腳2片不挖走線孔
面808白橡/噴白橡
腳白橡紙
撕保護膜腳粒鎖好""",
        products=(P("p1", 2, "彎角", "3X3.25", 2, 1, 1),
                  P("p2", 3, "主席平直", "3X7", 1, 1, 1),
                  P("p3", 4, "列席平直", "3X5", 2, 1, 1),
                  P("p4", 5, "列席平彎", "3X5", 2, 1, 1)),
        requirements=(
            R("source", 1, "20990102-2測試客戶B/8樓", "metadata", ("order",), "合成樓層不直接產生搬運加價", "literal"),
            R("corner", 2, "彎角3X3.25-----------2", "product", ("p1",), "保留台尺候選，不先換算"),
            R("chair", 3, "主席平直3X7-------------1", "product", ("p2",), "主席角色獨立"),
            R("guest_straight", 4, "列席平直3X5-------------2", "product", ("p3",), "列席平直獨立"),
            R("guest_curved", 5, "列席平彎3X5-------------2", "product", ("p4",), "列席平彎不與平直合併"),
            R("drawing_position", 6, "主席及彎角桌隔板孔位靠單", "drawing", ("p1", "p2"), "僅主席及彎角依圖定位", "literal"),
            R("center_position", 7, "列席桌隔板孔位置中", "position", ("p3", "p4"), "只套用列席，定位基準仍待核准", "literal"),
            R("boxes", 8, "每桌1個銀色單掀鋁毛刷13.5X40", "accessory", ("p1", "p2", "p3", "p4"), "每件一組線盒；確認毛刷與挖孔包含關係"),
            R("holes", 8, "及1孔6公分圓孔", "accessory", ("p1", "p2", "p3", "p4"), "每件一個圓孔；6公分是孔徑不是桌面尺寸"),
            R("under_wire", 9, "桌下走線", "process", ("order",), "桌下走線與腳板挖孔分開", "literal"),
            R("no_leg_holes", 9, "腳2片不挖走線孔", "negative", ("order",), "兩片腳板不挖孔，不能刪除桌面圓孔", "literal"),
            R("top_surface", 10, "面808白橡", "material", ("order",), "桌面貼面材質待確認", "unresolved"),
            R("paint", 10, "噴白橡", "finish", ("order",), "噴塗部位待確認", "unresolved"),
            R("leg_paper", 11, "腳白橡紙", "material", ("order",), "腳板貼紙不得套成桌面方案", "literal"),
            R("film", 12, "撕保護膜", "process", ("order",), "保留撕膜工序", "literal"),
            R("feet", 12, "腳粒鎖好", "process", ("order",), "保留鎖腳粒工序", "literal"),
        ),
        clarifications=(
            C("taiwan_foot", ("corner", "chair", "guest_straight", "guest_curved"), "業務", "尺寸是否台尺，換算及取整慣例為何？", "核准的單位與換算規則版本"),
            C("position", ("drawing_position", "center_position", "no_leg_holes"), "工程", "隔板定位與腳板禁孔如何落圖？", "定位圖、部位範圍與無衝突配置"),
            C("finish_policy", ("top_surface", "paint", "leg_paper", "film", "feet", "boxes"), "業務／工程", "部位材質與工費是否有合法規則？", "正式規格、用量與含價依據"),
        ),
        candidate_totals=(("products", 7), ("boxes", 7), ("holes", 7)),
    ),
    WorkOrderCase(
        key="ring_03", family="環式會議桌",
        raw_text="""(3)20990103-3測試客戶C
彎角3X3.25---------2(1孔6公分圓)
平直3X8-------------1(1孔6公分圓)
平直3X6--------------4(2孔6公分圓)
平彎3X6---------------2(2孔6公分圓)
孔置中/格置中/格在走線下方
紅胡340/噴紅木
照圖挖孔/桌下走線
腳2片不挖走縣孔""",
        products=(P("p1", 2, "彎角", "3X3.25", 2, 1),
                  P("p2", 3, "平直", "3X8", 1, 1),
                  P("p3", 4, "平直", "3X6", 4, 2),
                  P("p4", 5, "平彎", "3X6", 2, 2)),
        requirements=(
            R("source", 1, "20990103-3測試客戶C", "metadata", ("order",), "只作來源識別", "literal"),
            R("corner", 2, "彎角3X3.25---------2", "product", ("p1",), "台尺尺寸與成品數分離"),
            R("corner_hole", 2, "1孔6公分圓", "accessory", ("p1",), "括號每件一孔的候選解讀"),
            R("long", 3, "平直3X8-------------1", "product", ("p2",), "獨立長尺寸"),
            R("long_hole", 3, "1孔6公分圓", "accessory", ("p2",), "括號每件一孔的候選解讀"),
            R("straight", 4, "平直3X6--------------4", "product", ("p3",), "平直四件候選"),
            R("straight_holes", 4, "2孔6公分圓", "accessory", ("p3",), "括號每件兩孔，不改成品數"),
            R("curved", 5, "平彎3X6---------------2", "product", ("p4",), "平彎兩件候選"),
            R("curved_holes", 5, "2孔6公分圓", "accessory", ("p4",), "括號每件兩孔候選"),
            R("hole_center", 6, "孔置中", "position", ("order",), "仍須定位基準與圖面", "unresolved"),
            R("partition_center", 6, "格置中", "position", ("order",), "格的部件定義與置中基準待確認", "unresolved"),
            R("below_wire", 6, "格在走線下方", "position", ("order",), "保留部件相對位置，間距待確認", "literal"),
            R("surface", 7, "紅胡340", "material", ("order",), "色號與基材不能混同", "unresolved"),
            R("paint", 7, "噴紅木", "finish", ("order",), "噴漆部位與含價依據待核定", "unresolved"),
            R("drawing", 8, "照圖挖孔", "drawing", ("order",), "缺圖不可視為孔位已完整", "literal"),
            R("under_wire", 8, "桌下走線", "process", ("order",), "保留桌下走線", "literal"),
            R("typo_negative", 9, "腳2片不挖走縣孔", "negative", ("order",), "走縣疑似走線；修正仍保留腳板否定條件", "unresolved"),
        ),
        clarifications=(
            C("units_and_counts", ("corner", "corner_hole", "long_hole", "straight_holes", "curved_holes"), "業務", "台尺規則與括號孔數是否為每件用量？", "單位規則及逐筆孔數確認"),
            C("drawing_and_typo", ("drawing", "hole_center", "partition_center", "below_wire", "typo_negative"), "工程", "定位、格的間距與錯字修正為何？", "圖面及明確的部件／禁止條件核准"),
            C("material", ("surface", "paint"), "業務／工程", "材料與噴塗方案是否完整？", "合法規格、用量與工費"),
        ),
        candidate_totals=(("products", 9), ("holes", 15)),
    ),
    WorkOrderCase(
        key="meeting_01", family="會議桌",
        raw_text="""(1)209901321-4測試客戶D
3X6長方斜刀單鋁13.5X40------1
932陽胡/噴黑
T型鐵腳""",
        products=(P("p1", 2, "長方斜刀", "3X6", 1),),
        requirements=(
            R("source", 1, "209901321-4測試客戶D", "metadata", ("order",), "合成九碼日期異常，不能自行修正", "unresolved"),
            R("size", 2, "3X6", "dimension", ("p1",), "保留台尺候選", "unresolved"),
            R("shape", 2, "長方", "shape", ("p1",), "與斜刀邊型分離", "literal"),
            R("edge", 2, "斜刀", "process", ("p1",), "核對正式邊型及工費"),
            R("box", 2, "單鋁13.5X40", "accessory", ("p1",), "單鋁正式規格及數量待確認", "unresolved"),
            R("quantity", 2, "------1", "quantity", ("p1",), "行尾成品數量候選為一件"),
            R("surface", 3, "932陽胡", "material", ("p1",), "不能由色號猜基材或板厚", "unresolved"),
            R("paint", 3, "噴黑", "finish", ("p1",), "噴黑部位待確認", "unresolved"),
            R("legs", 4, "T型鐵腳", "component", ("p1",), "腳架數量、型號及塗裝包含關係待確認", "unresolved"),
        ),
        clarifications=(
            C("source_number", ("source",), "業務", "來源單號是否正確？", "保留原文並記錄人工確認的識別資訊"),
            C("standard_configuration", ("size", "edge", "box", "surface", "paint", "legs"), "業務／工程", "請確認單位、基材板厚、噴塗部位與五金數量。", "完整標準配置、用量與工費包含規則"),
        ),
    ),
    WorkOrderCase(
        key="meeting_02", family="會議桌",
        raw_text="""(2)20990105-5測試客戶E
20X360(120X180X2)船型-----------2
船型弧度120-20=100
單掀鋁13.5X60，線槽位置如圖面
V+中桶
面腳全貼9070T
噴白橡，撕保護膜""",
        products=(P("p1", 2, "船型", "20X360", 2),),
        requirements=(
            R("source", 1, "20990105-5測試客戶E", "metadata", ("order",), "只作來源識別", "literal"),
            R("overall_size", 2, "20X360", "dimension", ("p1",), "尺寸有矛盾，不得自動補成120X360", "unresolved"),
            R("panels", 2, "120X180X2", "assembly", ("p1",), "分片尺寸及片數候選，不等於成品數", "unresolved"),
            R("shape_quantity", 2, "船型-----------2", "product", ("p1",), "行尾二件為候選，須與括號片數區分"),
            R("curve", 3, "船型弧度120-20=100", "dimension", ("p1",), "數值用途與曲線幾何待工程定義", "unresolved"),
            R("box", 4, "單掀鋁13.5X60", "accessory", ("p1",), "每件數量未明，不能預設一個", "unresolved"),
            R("drawing", 4, "線槽位置如圖面", "drawing", ("p1",), "依圖定位，不能猜線槽位置", "literal"),
            R("legs", 5, "V+中桶", "assembly", ("p1",), "複合支撐數量、規格與連接結構待核定", "unresolved"),
            R("surface", 6, "面腳全貼9070T", "material", ("p1",), "桌面與腳分部位記錄全貼範圍"),
            R("paint", 7, "噴白橡", "finish", ("p1",), "噴塗部位待确认", "unresolved"),
            R("film", 7, "撕保護膜", "process", ("p1",), "保留撕膜工序", "literal"),
        ),
        clarifications=(
            C("size_conflict", ("overall_size", "panels", "curve", "shape_quantity"), "業務／工程", "總尺寸、分片與成品數如何解讀？", "人工核准的尺寸、片數、成品數與船型圖面", "conflict"),
            C("drawing_and_hardware", ("box", "drawing", "legs"), "工程", "線槽與支撐結構是否已定義？", "定位圖與每件五金、支撐的合法配置"),
            C("custom_process", ("panels", "surface", "paint", "film"), "工程", "拼接與曲面加工有核准用量嗎？", "核准的標準規則；若仍非標則維持工程核定阻擋", "engineering_review"),
        ),
    ),
    WorkOrderCase(
        key="meeting_03", family="會議桌",
        raw_text="""(3)20990106-6測試客戶F
5X14(5X7X2)船形單鋁13.5X60---------2
9842美/噴紅木
腳紅木V+中桶(中桶挖走線孔)""",
        products=(P("p1", 2, "船形", "5X14", 2),),
        requirements=(
            R("source", 1, "20990106-6測試客戶F", "metadata", ("order",), "只作來源識別", "literal"),
            R("overall_size", 2, "5X14", "dimension", ("p1",), "總尺寸台尺候選，不先換算", "unresolved"),
            R("panels", 2, "5X7X2", "assembly", ("p1",), "每件兩片的候選，不當成兩張桌", "unresolved"),
            R("shape", 2, "船形", "shape", ("p1",), "船形／船型可為詞彙候選，幾何仍待核准"),
            R("box", 2, "單鋁13.5X60", "accessory", ("p1",), "規格及每件數量待確認", "unresolved"),
            R("quantity", 2, "---------2", "quantity", ("p1",), "成品兩件候選，與分片數分離"),
            R("surface", 3, "9842美", "material", ("p1",), "美的材質簡稱不可直接猜測", "unresolved"),
            R("paint", 3, "噴紅木", "finish", ("p1",), "確認噴塗部位", "unresolved"),
            R("legs", 4, "腳紅木V+中桶", "assembly", ("p1",), "紅木外觀與複合支撐結構分開"),
            R("drum_hole", 4, "中桶挖走線孔", "process", ("p1",), "只作用於中桶，不作用於所有腳板", "literal"),
        ),
        clarifications=(
            C("units_and_panels", ("overall_size", "panels", "quantity"), "業務", "台尺換算、成品數與分片數為何？", "核准單位及數量層級"),
            C("custom_geometry", ("shape", "panels", "legs", "drum_hole"), "工程", "拼接、船型弧度與中桶孔規格為何？", "圖面與合法尺寸用量／加工規則", "engineering_review"),
            C("materials_and_box", ("surface", "paint", "box"), "業務／工程", "材質簡稱、噴塗部位與線盒數量為何？", "正式材料與五金配置及成本規則"),
        ),
    ),
    WorkOrderCase(
        key="dining_01", family="餐桌",
        raw_text="""(1)20990107-7測試客戶G
9尺圓胡桃假厚36MM封黑塑膠條-------1
9尺用八角腳-------------------------1
庫存6尺胡面鎖2.5鋁(背面加釘薄木板再鎖鋁合金)---1""",
        # 三筆來源交付項目候選，不表示三張桌；套組關係仍未定。
        products=(P("p1", 2, "圓桌面", "9尺圓", 1),
                  P("p2", 3, "八角腳", "9尺用", 1),
                  P("p3", 4, "庫存桌面改裝", "6尺", 1)),
        requirements=(
            R("source", 1, "20990107-7測試客戶G", "metadata", ("order",), "只作來源識別", "literal"),
            R("diameter", 2, "9尺圓", "dimension", ("p1",), "直徑語意與台尺慣例待確認", "unresolved"),
            R("surface", 2, "胡桃", "material", ("p1",), "基材、實際板厚與表面方案待確認", "unresolved"),
            R("false_thickness", 2, "假厚36MM", "assembly", ("p1",), "假厚不等於整片36毫米板", "literal"),
            R("edge", 2, "封黑塑膠條", "process", ("p1",), "圓周封邊含接頭及損耗規則待確認"),
            R("top_quantity", 2, "-------1", "quantity", ("p1",), "桌面數量一件候選"),
            R("base", 3, "9尺用八角腳-------------------------1", "assembly", ("p1", "p2"), "腳座與主桌配套，不算另一張成品桌"),
            R("stock", 4, "庫存6尺胡面", "inventory", ("p3",), "庫存用途未知，不能預設轉盤或免費", "unresolved"),
            R("aluminum", 4, "鎖2.5鋁", "accessory", ("p3",), "2.5的單位與鋁件用途未知", "unresolved"),
            R("reinforcement", 4, "背面加釘薄木板", "process", ("p3",), "保留補強板、五金與工序", "literal"),
            R("sequence", 4, "再鎖鋁合金", "process", ("p3",), "補強後再鎖鋁件，順序不可遺失", "literal"),
            R("stock_quantity", 4, "---1", "quantity", ("p3",), "改裝項目一件候選，不加成新桌數"),
        ),
        clarifications=(
            C("assembly_and_units", ("diameter", "base", "stock", "aluminum"), "業務／工程", "尺寸單位、6尺面用途及套組關係為何？", "核准的單位、部件用途、數量及相容規格"),
            C("stock_and_custom_cost", ("stock", "reinforcement", "sequence", "false_thickness", "edge", "surface"), "業務／工程", "庫存成本政策與假厚／改裝用量為何？", "受控庫存成本與工程核價；第一版未支援時維持阻擋", "engineering_review"),
        ),
    ),
    WorkOrderCase(
        key="dining_02", family="餐桌",
        raw_text="""(2)20990108-8測試客戶H-木心板12尺圓桌
12尺圓胡2317-------1
1/4圓合併，木心板，桌緣假厚3.6公分，黑色膠條封邊
CNC做1/4圓模板，確認直角
9尺轉盤胡2317---------1
1/2圓，木心板，假厚3.6公分，黑色膠條封邊，
轉盤支撐鐵架
4尺鋁合金鎖木心板上(木心板要合鐵架實內尺寸)
12尺用胡桃色圓桶腳------1""",
        products=(P("p1", 2, "主桌", "12尺圓", 1),
                  P("p2", 5, "轉盤", "9尺", 1),
                  P("p3", 9, "圓桶腳", "12尺用", 1)),
        requirements=(
            R("source", 1, "20990108-8測試客戶H", "metadata", ("order",), "只作來源識別", "literal"),
            R("title_spec", 1, "木心板12尺圓桌", "product", ("p1",), "標題含規格，不得將整行當作無關資訊"),
            R("top", 2, "12尺圓胡2317-------1", "product", ("p1",), "主桌尺寸、色號與數量分離"),
            R("quarter", 3, "1/4圓合併", "assembly", ("p1",), "四片合整圓只是候選；不得當成0.25張桌", "unresolved"),
            R("top_board", 3, "木心板", "material", ("p1",), "實際板厚仍待確認", "literal"),
            R("top_false_thickness", 3, "桌緣假厚3.6公分", "assembly", ("p1",), "區分實厚與完成邊厚", "literal"),
            R("top_edge", 3, "黑色膠條封邊", "process", ("p1",), "哪些分片邊界要封邊須核定"),
            R("template", 4, "CNC做1/4圓模板", "process", ("p1",), "可能為整單一次性工費，不能直接按桌數倍增", "unresolved"),
            R("right_angle", 4, "確認直角", "process", ("p1",), "保留模板檢驗要求", "literal"),
            R("turntable", 5, "9尺轉盤胡2317---------1", "product", ("p2",), "轉盤是子組件，不等同另一張主桌"),
            R("half", 6, "1/2圓", "assembly", ("p2",), "兩片合圓須確認，不當成0.5件產品", "unresolved"),
            R("turntable_board", 6, "木心板", "material", ("p2",), "轉盤用板獨立於主桌", "literal"),
            R("turntable_false_thickness", 6, "假厚3.6公分", "assembly", ("p2",), "轉盤假厚與主桌分開計料", "literal"),
            R("turntable_edge", 6, "黑色膠條封邊", "process", ("p2",), "轉盤封邊範圍待核定"),
            R("frame", 7, "轉盤支撐鐵架", "component", ("p2",), "數量、承重與尺寸未完整", "unresolved"),
            R("aluminum", 8, "4尺鋁合金鎖木心板上", "assembly", ("p2",), "鋁件規格及固定方式待核定", "unresolved"),
            R("measured_inside", 8, "木心板要合鐵架實內尺寸", "measurement", ("p2",), "需實測內尺寸與安裝餘量，不能從4尺猜出", "literal"),
            R("base", 9, "12尺用胡桃色圓桶腳------1", "assembly", ("p1", "p3"), "圓桶腳與主桌配套，仍須相容性確認"),
        ),
        clarifications=(
            C("units_and_segmentation", ("top", "quarter", "turntable", "half", "base"), "業務／工程", "尺數定義、分片數與套組關係為何？", "核准單位及成品／子件／分片結構"),
            C("measurement", ("frame", "aluminum", "measured_inside"), "工程", "鐵架實測內尺寸、公差與承重為何？", "量測紀錄、核准圖面及相容性檢查", "engineering_review"),
            C("custom_labor", ("template", "right_angle", "top_board", "top_false_thickness", "top_edge", "turntable_false_thickness", "turntable_edge"), "工程", "模板計費基準、假厚與封邊用量為何？", "核准加工用量及一次性工費政策；第一版仍非標則阻擋", "engineering_review"),
        ),
    ),
)


def combined_input() -> str:
    """提供未來批次拆單測試的輸入；不回傳解析或報價結果。"""
    groups = []
    for family in ("環式會議桌", "會議桌", "餐桌"):
        orders = "\n\n".join(case.raw_text for case in CASES if case.family == family)
        groups.append(f"{family}生產工單\n\n{orders}")
    return "\n\n".join(groups)
