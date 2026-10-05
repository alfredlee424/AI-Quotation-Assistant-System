"""只使用去識別化来源及隔離合成主檔；人工處置僅為測試，不代表工程核准。"""
from copy import deepcopy
from dataclasses import replace

import pytest

from agent.multi_quote import from_work_order
from agent.work_orders import parse_work_orders
from agent.requirement_review import apply_review_command
from quote_batch_fixtures import four_items, SHARED
from test_multi_trial import mixed_products


def review_all(batch, draft):
    for record in draft.requirements:
        targets = list(record.targets)
        if record.text in SHARED:
            targets = [line.line_id for line in draft.lines]
        draft = apply_review_command(draft, {"op": "record", "requirement_id": record.requirement_id,
            "targets": targets, "disposition": "production_note" if targets else "metadata", "bindings": [],
            "reference": "合成非正式金額核對-v1", "explanation": "僅建立範圍測試，不核准實際工序含價或工程"},
            draft_id=draft.draft_id, expected_revision=draft.revision, actor="測試", reason="測試核對", source_batch=batch)
    return draft


@pytest.fixture
def priced_pair(mixed_products):
    batch = parse_work_orders("環式會議桌生產工單\n20990101-1去識別化\n平直60X120-----1\n平直60X140-----1")
    draft = from_work_order(batch, batch.orders[0].order_id)
    draft = replace(draft, lines=tuple(replace(line, configuration=deepcopy(configured.configuration))
        for line, configured in zip(draft.lines, mixed_products.lines)))
    return batch, review_all(batch, draft)


@pytest.fixture
def priced_four(current_draft):
    batch, draft = four_items(reviewed=False)
    draft = replace(draft, lines=tuple(replace(line, configuration={**line.configuration,
        "prodkind": "CMT1", "product_name": "合成測試桌",
        "selections": deepcopy(current_draft["selections"])}) for line in draft.lines))
    return batch, review_all(batch, draft)
