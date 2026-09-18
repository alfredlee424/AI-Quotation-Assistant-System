import pytest

from agent.quotation_importer import parse_pasted_quote, quotation_to_natural_language


SAMPLE = (
    "103 QU26824010-01001 CMT1\\A001 1 C005 60*180 1.0 1.0 0.0 "
    "2026.08.24 16:07:34 hou orda60\n"
    "103 QU26824010-01001 CMT1\\A001\\S004 1 C001 25mm MDF 4.5 24.0 700.0 "
    "2026.08.24 16:07:34 hou orda60\n"
    "103 QU26824010-01001 CMT1\\B001\\S004 1 C001 纖維板4*8*18mm 2.0 120.0 480.0 "
    "2026.08.24 16:07:34 hou orda60"
)


def test_parse_supports_descriptions_with_spaces_and_non_tsv_rows():
    rows, warnings = parse_pasted_quote(SAMPLE)

    assert len(rows) == 3
    assert not warnings
    assert rows[1]["path"] == "CMT1\\A001\\S004"
    assert rows[1]["spec_desc"] == "25mm MDF"
    assert rows[1]["source_compri"] == 700.0


def test_parse_supports_tsv_rows():
    tsv = "\t".join(
        [
            "103", "Q1", "CMT1\\A001", "1", "C005", "60*180",
            "1.0", "1.0", "0.0", "2026.08.24", "16:07:34", "hou", "orda60",
        ]
    )
    rows, _ = parse_pasted_quote(tsv)
    assert rows[0]["ref_no"] == "Q1"
    assert rows[0]["qty"] == 1.0


def test_parse_rejects_mixed_quote_numbers():
    mixed = SAMPLE.replace("QU26824010-01001 CMT1\\B001", "OTHER-QUOTE CMT1\\B001")
    with pytest.raises(ValueError, match="只有一張報價單"):
        parse_pasted_quote(mixed)


def test_quotation_to_natural_language_groups_product_trees():
    rows, _ = parse_pasted_quote(SAMPLE)
    sentence = quotation_to_natural_language(rows)

    assert "產品訂購數量另行指定" in sentence
    assert "60*180" in sentence
    assert "25mm MDF" in sentence
    assert r"CMT1\B001\S004" in sentence
    assert "需要 1 張辦公桌" not in sentence
    assert "480.0" not in sentence


def test_source_column_positions_and_values_are_stable(cmt1_db):
    from copy import deepcopy
    from agent.quotation_importer import import_pasted_quote
    rows, _ = parse_pasted_quote(SAMPLE)
    before = deepcopy(rows)
    assert (rows[1]["qty"], rows[1]["stdqty"], rows[1]["stdpar"], rows[1]["source_compri"]) == (1, 4.5, 24, 700)
    imported = import_pasted_quote(SAMPLE, product_qty=7)
    assert imported.rows == before
    assert imported.draft["qty"] == 7
    assert imported.draft["ref_no"] is None
    assert imported.draft["prodkind"] == "CMT1"
    assert imported.draft["imported_quote"]["source_ref_no"] == "QU26824010-01001"
    item = imported.draft["selections"][r"CMT1\A001\S004"]
    assert (item["line_qty"], item["source_stdqty"], item["source_stdpar"], item["source_compri"]) == (1, 4.5, 24, 700)
    assert "compri" not in item


def test_product_qty_is_never_inferred_from_source_rows(cmt1_db):
    from agent.quotation_importer import import_pasted_quote
    imported = import_pasted_quote(SAMPLE)
    assert imported.draft["qty"] is None
    assert imported.draft["selections"][r"CMT1\B001\S004"]["line_qty"] == 1


def test_two_source_holes_keep_line_qty_but_product_qty_is_explicit(cmt1_db):
    from agent.quotation_importer import import_pasted_quote
    text = "103 Q1 CMT1\\A001\\S019\\S030 2 C001 6公分圓孔 1 1 350 2026.08.24 16:07:34 hou orda60"
    result = import_pasted_quote(text, product_qty=5)
    assert result.draft["qty"] == 5
    assert result.draft["selections"][r"CMT1\A001\S019\S030"]["line_qty"] == 2


def test_invalid_current_paths_are_retained_and_block_new_quote(cmt1_db):
    from agent.quotation_importer import import_pasted_quote
    from engine.configuration import resolve_configuration
    from engine.preview import freeze_preview
    text = SAMPLE.replace(r"CMT1\B001\S004", r"CMT1\B001\HISTORICAL")
    draft = import_pasted_quote(text, product_qty=1).draft
    assert r"CMT1\B001\HISTORICAL" in draft["selections"]
    resolved = resolve_configuration(draft)
    assert not resolved["valid"]
    assert any("HISTORICAL" in error for error in resolved["errors"])
    with pytest.raises(ValueError):
        freeze_preview(draft)


def test_name_drift_creates_question_instead_of_silent_acceptance(cmt1_db):
    from agent.quotation_importer import import_pasted_quote
    draft = import_pasted_quote(SAMPLE.replace("25mm MDF", "舊名 MDF"), product_qty=1).draft
    assert draft["questions"]
    assert any(r"CMT1\A001\S004" in warning for warning in draft["imported_quote"]["warnings"])


def test_historical_restore_is_noop_after_explicit_modification(cmt1_db):
    from copy import deepcopy
    from agent.quotation_importer import import_pasted_quote, restore_historical_matches
    draft = import_pasted_quote(SAMPLE, product_qty=1).draft
    draft["selections"][r"CMT1\A001"]["code"] = "C003"
    before = deepcopy(draft)
    assert restore_historical_matches("和舊單一樣", draft) == []
    assert draft == before


@pytest.mark.parametrize("index,value", [(3, "0"), (3, "-1"), (6, "-1"), (7, "0"), (8, "-1"),
                                        (3, "nan"), (6, "inf"), (7, "nan"), (8, "inf")])
def test_invalid_source_numbers_rejected(index, value):
    fields = ["103", "Q1", r"CMT1\A001", "1", "C005", "60*180", "1", "1", "0",
              "2026.08.24", "16:07:34", "hou", "orda60"]
    fields[index] = value
    with pytest.raises(ValueError):
        parse_pasted_quote("\t".join(fields))


@pytest.mark.parametrize("qty", [0, -1, float("nan"), float("inf"), True])
def test_invalid_explicit_product_qty_rejected(cmt1_db, qty):
    from agent.quotation_importer import import_pasted_quote
    with pytest.raises(ValueError):
        import_pasted_quote(SAMPLE, product_qty=qty)
