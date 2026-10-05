"""T30 開發唯讀投影。只有完整 T29 read 可簽發 handle，沒有匯入／保存 API。"""
from dataclasses import asdict, dataclass, field
import hashlib
import hmac
import json
import secrets

from engine.development_atomic_contract import (
    DevelopmentSnapshotIdentity, SCHEMA_VERSION, canonical, digest, validate_identity,
)
from engine.development_atomic_snapshot import DevelopmentAtomicSnapshotService


VIEW_VERSION = "development-readonly-view-v1"
BATCH_TYPE = "development-readonly-batch"
CHILD_TYPE = "development-readonly-child"
WARNING = "僅供開發唯讀查閱，不是正式報價、身份授權或工程核准，不得交付客戶。"
RESERVATION_WARNING = "開發保留號，不代表開立"
CONFIDENTIAL = "含完整來源、成本、價格及商業機密；逐張檔也含整份來源工單，須依公司資料政策保管。"
FRESHNESS = "這是明確查閱當次已完整核驗的固定快照；資料庫後續異動須再次按唯讀查閱驗證，不會隨重繪或下載自動更新。"
TOTAL_MEANING = "整批原始固定總額，非本張金額；不再次折扣、課稅或重算，未稅加稅不等於含稅的原捨入差額保留。"


@dataclass(frozen=True, slots=True)
class VerifiedDevelopmentSnapshot:
    """程序內固定 handle；公開構造不等於已驗證，須通過發行 reader 的封印。"""
    identity: DevelopmentSnapshotIdentity
    _payload: str = field(repr=False)
    _seal: str = field(repr=False)


class DevelopmentSnapshotReader:
    """明確注入隔離 Engine，只暴露完整 read。每個實例為不同來源／信任範圍。

    source_context 是開發 caller 的不透明上下文標記，不是路徑或授權 token。
    context 變更必須新建 reader；不得重新指向既有 engine 的連線來源。
    不自行建庫或強化 DB 權限，caller 應注入實際唯讀連線。
    """
    __slots__ = ("__service", "__key", "__context")

    def __init__(self, engine, *, source_context=None):
        self.__service = DevelopmentAtomicSnapshotService(engine)
        self.__key = secrets.token_bytes(32)
        self.__context = source_context if source_context is not None else object()

    @property
    def source_context(self):
        return self.__context

    def read(self, identity):
        validate_identity(identity)
        # 唯一 SQL 入口：同 transaction 核驗 registry、批次、全部子張／來源／成本。
        document = self.__service.read(identity)
        if document is None:
            return None
        payload = canonical(document)
        seal = hmac.new(self.__key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return VerifiedDevelopmentSnapshot(identity, payload, seal)

    def checked(self, snapshot):
        """只驗證當次固定內容的程序封印；不宣稱重新核驗目前 DB。"""
        if type(snapshot) is not VerifiedDevelopmentSnapshot:
            raise ValueError("僅接受完整 read 簽發的開發唯讀 handle，不接受字典或下載檔")
        validate_identity(snapshot.identity)
        if type(snapshot._payload) is not str or type(snapshot._seal) is not str:
            raise ValueError("唯讀 handle 格式錯誤")
        expected = hmac.new(self.__key, snapshot._payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, snapshot._seal):
            raise ValueError("唯讀 handle 非本 reader 簽發或內容已改變")
        document = json.loads(snapshot._payload)
        if document["identity"] != asdict(snapshot.identity):
            raise ValueError("唯讀 handle 身份已改變")
        return document  # 每次新深層複本，不能從展示資料改動固定 handle。


def _checked(reader, snapshot):
    if type(reader) is not DevelopmentSnapshotReader:
        raise ValueError("須注入獨立開發唯讀 reader，不接受任意 read/dict 替身")
    return reader.checked(snapshot)


def _summary(document):
    request = document["fixed_request"]
    return {
        "child_count": len(document["children"]),
        "product_qty": sum(c["fixed_child"]["product_qty"] for c in document["children"]),
        "batch_totals_json": request["totals_json"],
        "batch_totals": json.loads(request["totals_json"]),
        "batch_total_meaning": TOTAL_MEANING,
        "children": [{
            "child_quote_id": c["fixed_child"]["child_quote_id"],
            "source_item_id": c["fixed_child"]["source_item_id"],
            "position": c["fixed_child"]["position"],
            "label": c["fixed_child"]["label"],
            "product_qty": c["fixed_child"]["product_qty"],
            "development_reserved_ref_no": c["ref_no"],
            "reservation_meaning": RESERVATION_WARNING,
            "amounts": c["fixed_child"]["amounts"],
        } for c in document["children"]],
    }


def _envelope(kind, content):
    result = {
        "document_type": kind, "document_version": VIEW_VERSION,
        "can_quote": False, "can_confirm": False, "can_save": False,
        "warning": WARNING, "reservation_meaning": RESERVATION_WARNING,
        "confidentiality": CONFIDENTIAL, "freshness": FRESHNESS,
        "content": content,
    }
    result["content_digest"] = digest(canonical(result))
    return result


def batch_view(reader, snapshot):
    document = _checked(reader, snapshot)
    return _envelope(BATCH_TYPE, {"summary": _summary(document), "saved_snapshot": document})


def child_view(reader, snapshot, child_quote_id):
    document = _checked(reader, snapshot)
    # 只能在已驗證整批內選；不接受 caller 的 row/ref、不另查單張。
    for position, child in enumerate(document["children"]):
        if child["fixed_child"]["child_quote_id"] == child_quote_id:
            request = document["fixed_request"]
            return _envelope(CHILD_TYPE, {
                "batch_context": {k: v for k, v in document.items()
                                  if k not in ("fixed_request", "children", "reservation")},
                "fixed_batch": {k: v for k, v in request.items() if k != "children"},
                "batch_total_meaning": TOTAL_MEANING,
                "child": child,
                "reservation_child": document["reservation"]["children"][position],
            })
    raise ValueError("子張不在當次完整核驗批次中")


@dataclass(frozen=True, slots=True)
class DevelopmentExport:
    filename: str
    mime: str
    data: bytes = field(repr=False)


def _plain_text(view):
    content = view["content"]
    if view["document_type"] == BATCH_TYPE:
        saved = content["saved_snapshot"]
        children, request = saved["children"], saved["fixed_request"]
        heading = f"整批開發唯讀總覽｜{content['summary']['child_count']} 張／{content['summary']['product_qty']} 件"
    else:
        children, request = [content["child"]], content["fixed_batch"]
        heading = "逐張開發唯讀內容（並非客戶報價單）"
    lines = [heading, WARNING, CONFIDENTIAL, FRESHNESS,
             "文件版本：" + VIEW_VERSION, "內容摘要：" + view["content_digest"],
             "完整來源工單原文：", request["source"]["raw_text"],
             "固定政策原值：", request["policy_json"],
             TOTAL_MEANING, request["totals_json"]]
    for child in children:
        fixed = child["fixed_child"]
        lines.extend([
            f"第 {fixed['position']} 張｜{fixed['label']}｜成品數 {fixed['product_qty']}",
            f"{RESERVATION_WARNING}：{child['ref_no']}",
            "完整專屬描述：", fixed["description"], "完整共用描述：", fixed["shared_description"],
            "本張原始精確金額與捨入差額（不湊數）：", json.dumps(fixed["amounts"], ensure_ascii=False, indent=2),
            "完整配置／每列五位序號／每件數／用量及價格證據／條件圖面見下列完整原值。",
        ])
    # 純文字不執行標記／連結／公式。完整 JSON 保留所有原字串（包括內層 JSON 空白）。
    lines.extend(["完整唯讀文件原值（含來源座標、配置、成本列、政策及圖面條件）：",
                  json.dumps(view, ensure_ascii=False, indent=2, allow_nan=False)])
    return "\n".join(lines) + "\n"


def export_snapshot(reader, snapshot, *, child_quote_id=None, format="json"):
    """只從已核驗 handle 產生下載；不接受可自行篡改的 view dict。"""
    view = (batch_view(reader, snapshot) if child_quote_id is None
            else child_view(reader, snapshot, child_quote_id))
    identity = snapshot.identity  # 經 reader.checked 驗證過的固定身份。
    stem = f"development-readonly-{identity.workgroup}-{identity.batch_id}"
    if child_quote_id is not None:
        stem += "-" + child_quote_id  # 來自已驗證整批的 typed child 識別。
    if format == "json":
        return DevelopmentExport(stem + ".json", "application/json", canonical(view).encode("utf-8"))
    if format == "txt":
        return DevelopmentExport(stem + ".txt", "text/plain; charset=utf-8", _plain_text(view).encode("utf-8"))
    raise ValueError("僅提供開發 JSON／純文字，不提供正式客戶格式")
