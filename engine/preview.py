"""固定報價預覽：草稿指紋、計算結果、版本與內容完整性。確認時不再查主檔。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from config import WORKGROUP
from engine.configuration import normalize_selections, invalidate_preview
from engine.pricing import calculate_configuration


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def draft_identity(draft: dict) -> dict:
    selections = normalize_selections(draft.get("selections", {}))
    return {"workgroup": draft.get("workgroup", WORKGROUP), "prodkind": draft.get("prodkind"),
            "product_name": draft.get("product_name"), "qty": draft.get("qty"),
            "discount_rate": draft.get("discount_rate", 0), "revision": draft.get("revision", 0),
            "questions": draft.get("questions", []), "pending_options": draft.get("pending_options", []),
            "selections": {p: {k: s.get(k) for k in ("code", "line_qty", "automatic")}
                           for p, s in selections.items()}}


def freeze_preview(draft: dict) -> dict:
    if draft.get("ref_no") or draft.get("status") in ("SNAPSHOT_CREATED", "CREATING", "COMPLETED"):
        raise ValueError("已建立報價不能重新試算，請建立新草稿")
    invalidate_preview(draft)
    result, resolved = calculate_configuration(draft)
    draft["selections"] = deepcopy(resolved["selections"])
    draft["missing_fields"] = []
    draft["allowed_options"] = {}
    preview_id = uuid4().hex
    preview = {
        "preview_id": preview_id, "revision": draft.get("revision", 0),
        "ref_no": "Q" + datetime.now(timezone.utc).strftime("%Y%m%d") + preview_id[:12],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "formula_version": "usage-divided-by-yield-v1",
        "rounding": "ROUND_HALF_UP; totals rounded after summation",
        "identity": draft_identity(draft),
        "selections": deepcopy(resolved["selections"]),
        "calc": asdict(result),
        "source_ref_no": draft.get("imported_quote", {}).get("source_ref_no"),
    }
    preview["digest"] = digest(preview)
    draft["preview"] = preview
    draft["calc_result"] = deepcopy(preview["calc"])
    draft["status"] = "PREVIEW"
    return deepcopy(preview)


def checked_preview(draft: dict, preview_id: str) -> dict:
    preview = draft.get("preview")
    if not preview or not preview_id or preview.get("preview_id") != preview_id:
        raise ValueError("預覽版本不存在或已失效，請重新試算並確認")
    if draft.get("status") not in ("PREVIEW", "SNAPSHOT_CREATED"):
        raise ValueError("目前狀態不允許確認報價")
    content = {k: v for k, v in preview.items() if k != "digest"}
    if digest(content) != preview.get("digest") or draft_identity(draft) != preview["identity"]:
        invalidate_preview(draft)
        raise ValueError("草稿或預覽內容已變動，請重新試算並確認")
    return deepcopy(preview)
