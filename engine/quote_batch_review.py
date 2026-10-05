"""T28 非正式內容核對基礎；無配號、身份認證、工程核准或保存服務。

Session 僅保存目前狀態摘要並序列化操作，拒絕舊狀態重放；不是持久 registry。
下載不支援匯入，程序／工作階段結束即失效。外部異動須由呼叫端提供目前上下文。
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
from threading import RLock
from uuid import uuid4

from agent.quote_batch import _digest, _json
from engine.quote_batch_trial import checked_batch_trial, MONEY_FIELDS

DOCUMENT_TYPE = "fixed_batch_content_review_nonformal_only"
REVIEW_VERSION = "fixed-batch-content-review-v1"
WARNING = "非正式整批內容核對演練：不是客戶報價、非工程核准、非開立；不配號、不正式確認、不保存。"
_KEY = secrets.token_bytes(32)
FORMAL_PENDING = (
    ("business", "業務來源語意、數量／單位與適用範圍正式核准未完成"),
    ("engineering", "工程、幾何、圖面受控版本與規則核准未完成"),
    ("product_lines", "三產品線主檔、用量與相容規則正式驗收未完成"),
    ("included_price", "標準含價與工序不重複／不漏報規則核准未完成"),
    ("authorization", "身份驗證與核准授權未實作；手填人名／依據不等於核准"),
    ("dba", "正式資料庫遷移、跨 DB 併發與回退尚待 DBA 核准"),
    ("formal_preview_numbering", "正式關卡通過後的 T47 保留銜接及正式固定預覽未實作"),
    ("atomic_save", "T29 同版整批原子保存及正式查閱未實作"),
)


def _mac(value):
    return hmac.new(_KEY, _json(value).encode(), hashlib.sha256).hexdigest()


def review_policy():
    return {"version": REVIEW_VERSION, "same_actor_required": True,
            "formal_pending": [list(gate) for gate in FORMAL_PENDING],
            "retry": "reject_without_new_event", "scope": "nonformal_content_only"}


def _seal(document):
    result = deepcopy(document)
    result.pop("state_digest", None)
    result.pop("process_seal", None)
    result["state_digest"] = _digest(result)
    result["process_seal"] = _mac(result)
    return result


def _check_seal(document):
    if not isinstance(document, dict) or document.get("document_type") != DOCUMENT_TYPE:
        raise ValueError("僅接受本版非正式整批核對文件。")
    expected = _seal(document)
    if (document.get("state_digest") != expected["state_digest"]
            or not hmac.compare_digest(str(document.get("process_seal", "")), expected["process_seal"])
            or document.get("content_digest") != _digest(document["content"])):
        raise ValueError("固定文件或核對狀態被修改，必須重新建立非正式核對。")


def _complete(trial):
    expansion = trial["expansion"]
    requirements = trial["trial_context"]["requirements"]
    children = trial["children"]
    if (not children or trial["status"] != "ALL_CHILDREN_CALCULATED"
            or trial["child_count"] != len(children) or trial["calculated_count"] != len(children)
            or not trial["totals"] or not expansion["source_item_coverage_complete"]
            or not expansion["expansion_review_complete"] or expansion["issues"]
            or not requirements["source_coverage_complete"] or not requirements["all_records_reviewed"]
            or trial["trial_context"]["all_conditions"]["issues"]
            or trial["trial_context"]["all_drawings"]["issues"]):
        raise ValueError("沒有有效完整逐張試算／來源帳本，請返回試算；不得排除未完成品項。")
    for child in children:
        calculation = child["calculation"]
        if (child["status"] != "CALCULATED" or child["blockers"] or not calculation
                or not calculation["items"] or not calculation["cost_inputs"]
                or any(calculation.get(key) is None for key in MONEY_FIELDS)):
            raise ValueError("有缺張、阻擋或缺金額／成本用量證據，不能建立整批核對。")


def _gate_report(trial):
    context = trial["trial_context"]
    report = {
        "version": REVIEW_VERSION,
        "internal": [
            {"gate": "all_configurations_calculated", "status": "RECORDED_INTERNAL_ONLY",
             "evidence": [{"child_quote_id": c["child_quote_id"], "digest": c["content_digest"],
                           "status": c["status"], "blockers": c["blockers"],
                           "cost_rows": len(c["calculation"]["items"])} for c in trial["children"]]},
            {"gate": "source_ledger_manual_disposition", "status": "RECORDED_INTERNAL_ONLY",
             "evidence": context["requirements"], "coverage": trial["expansion"]["coverage"],
             "warning": "只證明來源帳本人工處置與範圍，非正式語意／含價／工程核准。"},
            {"gate": "conditions_drawings", "status": "RECORDED_INTERNAL_ONLY",
             "conditions": context["all_conditions"], "drawings": context["all_drawings"],
             "warning": "無已登記問題不等於所有工程需求已辨識或核准。"},
        ],
        "formal": [{"gate": key, "status": "PENDING", "reason": reason} for key, reason in FORMAL_PENDING],
        "formal_blockers": deepcopy(trial["formal_blockers"]),
        "child_formal_blockers": [{"child_quote_id": c["child_quote_id"], "blockers": c["formal_blockers"]}
                                 for c in trial["children"]],
        "policy": deepcopy(context["policy"]),
        "policy_fingerprint": context["policy_fingerprint"],
        "actor_is_authenticated": False,
    }
    report["report_digest"] = _digest(report)
    return report


class BatchReviewSession:
    """每個 UI 草稿的短生命週期控制器；回傳 deepcopy，不修改傳入文件。

    成功操作更新唯一 head；重試一律拒絕，不多寫事件。觀察到上下文失效即永久撤銷
    本次 head，即使改回也不復活。呼叫端修改草稿／清除 trial 時亦須 invalidate。
    """

    def __init__(self):
        self._head = None
        self._lock = RLock()

    def invalidate(self):
        with self._lock:
            self._head = None

    def freeze(self, draft, current_trial, *, actor, source_batch):
        with self._lock:
            self._head = None  # 重新建立失敗不得沿用舊核對。
            trial = checked_batch_trial(draft, current_trial, actor=actor, source_batch=source_batch)
            _complete(trial)
            content = {"version": REVIEW_VERSION, "review_id": uuid4().hex,
                       "created_at": datetime.now(timezone.utc).isoformat(), "warning": WARNING,
                       "actor": actor.strip(), "same_actor_required": True,
                       "review_policy": review_policy(),
                       "trial": trial, "source_batch": asdict(source_batch),
                       "gate_report": _gate_report(trial)}
            # 固定純 JSON 內容與可演進核對狀態分開計算摘要。
            content = json.loads(_json(content))
            document = {"document_type": DOCUMENT_TYPE, "document_version": REVIEW_VERSION,
                        "content": content, "content_digest": _digest(content),
                        "review_state": {"stage": 0, "history": [], "next_token": secrets.token_hex(32)},
                        "can_quote": False, "can_confirm": False, "can_save": False}
            document = _seal(document)
            self._head = document["state_digest"]
            return deepcopy(document)

    def checked(self, document, draft, current_trial, *, actor, source_batch):
        with self._lock:
            try:
                _check_seal(document)
                content = document["content"]
                # 必須驗證呼叫端的目前試算，不是一直驗證文件裡的舊 trial。
                trial = checked_batch_trial(draft, current_trial, actor=actor, source_batch=source_batch)
                if (content["version"] != REVIEW_VERSION or content["actor"] != actor.strip()
                        or content["review_policy"] != review_policy()
                        or _json(content["trial"]) != _json(trial)
                        or _json(content["source_batch"]) != _json(asdict(source_batch))):
                    raise ValueError("目前試算、來源、操作人或政策已變更，兩次核對均失效。")
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                self._head = None
                raise ValueError("非正式核對驗證失敗，舊核對不可復活；請重新建立。") from exc
            if self._head is None or document["state_digest"] != self._head:
                # 過時操作不能覆寫／撤銷另一個已成功的新狀態。
                raise ValueError("核對狀態已失效或為舊版本重放，請重新建立。")
            return deepcopy(document)

    def record(self, document, draft, current_trial, *, step, token, actor, source_batch):
        with self._lock:
            result = self.checked(document, draft, current_trial, actor=actor, source_batch=source_batch)
            state = result["review_state"]
            if type(step) is not int or step not in (1, 2) or step != state["stage"] + 1:
                raise ValueError("須依序獨立記錄第一次、第二次核對；同一步重試拒絕，不增加歷史。")
            if not isinstance(token, str) or not hmac.compare_digest(token, state["next_token"]):
                raise ValueError("核對 token 不屬於目前同版／同人／同順序狀態。")
            state["history"].append({"event_id": uuid4().hex, "step": step,
                "action": "NONFORMAL_CONTENT_REVIEW_RECORDED", "actor": actor.strip(),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "review_id": result["content"]["review_id"], "content_digest": result["content_digest"],
                "previous_state_digest": result["state_digest"],
                "trial_id": current_trial["trial_id"], "trial_digest": current_trial["content_digest"],
                "warning": "時間與人名僅為核對紀錄，非身份認證、非工程核准、非開立。"})
            state["stage"] = step
            state["next_token"] = secrets.token_hex(32) if step == 1 else None
            result = _seal(result)
            self._head = result["state_digest"]
            return deepcopy(result)

    def download(self, document, draft, current_trial, *, actor, source_batch):
        return json.dumps(self.checked(document, draft, current_trial, actor=actor, source_batch=source_batch),
                          ensure_ascii=False, indent=2, allow_nan=False)
