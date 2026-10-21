# SPDX-License-Identifier: MIT-0
"""Contract tests against real Docling models and unchanged upstream consumers."""

from dataclasses import replace

import pytest
from adapter import SourceBinding, build_evidence
from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.base import BoundingBox, CoordOrigin, Size
from docling_core.types.doc.common.reference import ProvenanceItem
from docling_core.types.doc.items.table.table_data import TableCell, TableData

from idp_common.ocr.service import OcrService

SOURCE = SourceBinding(
    "tenant", "company", "source", "revision", "a" * 64, "application/pdf"
)


def document_with_page():
    doc = DoclingDocument(name="synthetic")
    doc.add_page(1, Size(width=200, height=400))
    doc.add_text(
        label=DocItemLabel.TEXT,
        text="Payment due in 30 days",
        prov=ProvenanceItem(
            page_no=1,
            charspan=(0, 22),
            bbox=BoundingBox(
                l=20, r=180, t=360, b=340, coord_origin=CoordOrigin.BOTTOMLEFT
            ),
        ),
    )
    cell = TableCell(
        text="EUR 100.00",
        start_row_offset_idx=0,
        end_row_offset_idx=1,
        start_col_offset_idx=0,
        end_col_offset_idx=1,
        bbox=BoundingBox(l=20, r=80, t=100, b=120, coord_origin=CoordOrigin.TOPLEFT),
    )
    doc.add_table(
        data=TableData(num_rows=1, num_cols=1, table_cells=[cell]),
        prov=ProvenanceItem(
            page_no=1,
            charspan=(0, 0),
            bbox=BoundingBox(
                l=20, r=80, t=100, b=120, coord_origin=CoordOrigin.TOPLEFT
            ),
        ),
    )
    return doc


def build(doc, source=SOURCE, **kwargs):
    return build_evidence(doc, source, reader_version="2.141.0", **kwargs)


def test_upstream_artifacts_preserve_body_table_geometry_and_unknown_confidence():
    doc = document_with_page()
    evidence = build(doc)
    assert evidence["document"] == doc.export_to_dict()
    assert "Payment due in 30 days" in evidence["markdown"]
    assert "EUR 100.00" in evidence["markdown"]
    response = evidence["pageHooks"]["1"]
    # No constructor: these pure consumers do not need an AWS client/config.
    service = object.__new__(OcrService)
    raw, confidence = service._extract_bedrock_ocr_artifacts(response)
    text = response["output"]["message"]["content"][0]["text"]
    page = service._build_page_data(raw, text, "bedrock")
    assert "N/A" in confidence["text"]
    lines = [b for b in raw["Blocks"] if b["BlockType"] == "LINE"]
    assert [b["Text"] for b in lines] == ["Payment due in 30 days", "EUR 100.00"]
    assert all("Confidence" not in b for b in lines)
    assert lines[0]["Geometry"]["BoundingBox"] == {
        "Left": 0.1,
        "Top": 0.1,
        "Width": 0.8,
        "Height": 0.05,
    }
    assert len(page["lines"]) == 2
    assert page["lines"][0]["geometry"]["boundingBox"]["top"] == 0.1


def test_docx_structural_anchors_without_fabricated_pages():
    doc = DoclingDocument(name="synthetic-docx")
    doc.add_text(label=DocItemLabel.TEXT, text="Sample only — no sale")
    evidence = build(
        doc,
        replace(
            SOURCE,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    )
    assert evidence["pageHooks"] == {}
    assert evidence["document"]["texts"][0]["self_ref"] == "#/texts/0"
    assert "Sample only" in evidence["markdown"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", "r2"),
        ("company_id", "other"),
        ("tenant_id", "other"),
        ("sha256", "b" * 64),
    ],
)
def test_block_identity_is_bound_to_source_and_scope(field, value):
    doc = document_with_page()
    first = build(doc)["pageHooks"]["1"]["textractBlocks"]["Blocks"]
    second = build(doc, replace(SOURCE, **{field: value}))["pageHooks"]["1"][
        "textractBlocks"
    ]["Blocks"]
    assert {b["Id"] for b in first}.isdisjoint(b["Id"] for b in second)
    assert first == build(doc)["pageHooks"]["1"]["textractBlocks"]["Blocks"]


def test_bounds_and_invalid_geometry_fail_instead_of_truncating():
    doc = document_with_page()
    with pytest.raises(ValueError, match="limit"):
        build(doc, max_output_bytes=1)
    doc.texts[0].prov[0].bbox.l = -1
    with pytest.raises(ValueError, match="outside page"):
        build(doc)


def test_missing_page_and_bad_character_span_fail():
    doc = document_with_page()
    doc.texts[0].prov[0].page_no = 2
    with pytest.raises(ValueError, match="missing page"):
        build(doc)
    doc.texts[0].prov[0].page_no = 1
    doc.texts[0].prov[0].charspan = (0, 200)
    with pytest.raises(ValueError, match="character span"):
        build(doc)


def test_explicit_source_binding_required():
    with pytest.raises(ValueError, match="explicit"):
        replace(SOURCE, revision="")
    with pytest.raises(ValueError, match="SHA-256"):
        replace(SOURCE, sha256="unknown")


def test_multipage_table_keeps_structure_without_guessing_cell_page():
    doc = document_with_page()
    doc.add_page(2, Size(width=200, height=400))
    doc.tables[0].prov.append(doc.tables[0].prov[0].model_copy(update={"page_no": 2}))
    evidence = build(doc)
    assert evidence["document"]["tables"] == doc.export_to_dict()["tables"]
    assert evidence["itemsWithoutUnambiguousPage"] == ["#/tables/0"]
    assert not any(
        "table_cells" in a["provenancePointer"]
        for a in evidence["blockAnchors"].values()
    )
    with pytest.raises(ValueError, match="limit"):
        build(doc, max_pages=1)


def test_geometry_keeps_original_structural_provenance():
    doc = document_with_page()
    evidence = build(doc)
    blocks = evidence["pageHooks"]["1"]["textractBlocks"]["Blocks"]
    body = next(b for b in blocks if b.get("Text", "").startswith("Payment"))
    assert evidence["blockAnchors"][body["Id"]] == {
        "documentPointer": "#/texts/0",
        "provenancePointer": "#/texts/0/prov/0",
        "page": 1,
        "granularity": "text-item-span",
    }
    assert (
        evidence["document"]["texts"][0]["prov"][0]["bbox"]["coord_origin"]
        == "BOTTOMLEFT"
    )
