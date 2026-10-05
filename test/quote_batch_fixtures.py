"""依核准契約重建的去識別化四品項；數量為測試操作者明示配置，非自動候選。"""
from dataclasses import replace

from agent.multi_quote import from_work_order
from agent.work_orders import parse_work_orders
from agent.requirement_review import apply_review_command
from agent.conditions import apply_condition_command


SHARED = ("單面置物版", "孔靠單/格靠單", "817胡/噴胡", "腳粒鎖好/撕保護膜")
TEXT = "\n".join(("環式會議桌生產工單", "20990101-1去識別化案例A",
    "彎角70X70削角 面無孔有桌下走線-----2", "平直70X120 面1孔13.5X40單掀鋁-----1",
    "平直70X140 面1孔13.5X40單掀鋁-----6", "平彎70X140 面1孔13.5X40單掀鋁-----2", *SHARED))


def four_items(*, quantities=True, reviewed=True):
    batch = parse_work_orders(TEXT)
    draft = from_work_order(batch, batch.orders[0].order_id)
    if quantities:
        draft = replace(draft, lines=tuple(replace(line, configuration={**line.configuration, "qty": qty,
            "selections": {r"CMT1\A001": {"code": "合成規格" + str(i), "line_qty": 1}}})
            for i, (line, qty) in enumerate(zip(draft.lines, (2, 1, 6, 2)))))
    if reviewed:
        for record in draft.requirements:
            targets = list(record.targets)
            shared = record.text in SHARED
            if shared:
                targets = [line.line_id for line in draft.lines]
            draft = apply_review_command(draft, {
                "op": "record", "requirement_id": record.requirement_id,
                "targets": targets, "disposition": "production_note" if targets else "metadata", "bindings": [],
                "reference": "合成文字範圍-v1", "explanation": "僅核對描述逐張展示；不核准工程或費用",
            }, draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="核對展示範圍", source_batch=batch)
    return batch, draft


def condition(batch, draft, subject, state, targets, requirement, *, scope="line"):
    return apply_condition_command(draft, {
        "op": "record", "condition_id": "", "subject": subject, "state": state, "scope": scope,
        "group_label": "", "targets": list(targets), "value": "人工文字核對", "requirement_ids": [requirement],
        "bindings": [], "overrides": [], "reference": "合成条件-v1", "explanation": "不代表工程核准",
    }, draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="條件核對", source_batch=batch)
