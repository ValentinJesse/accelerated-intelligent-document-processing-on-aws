# SPDX-License-Identifier: MIT-0
"""Lossless local reader envelope plus the upstream structured OCR hook view."""

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from importlib.metadata import version
from typing import Any

from docling_core.types.doc import DoclingDocument, TableItem, TextItem
from docling_core.types.doc.common.content_layer import ContentLayer


@dataclass(frozen=True)
class SourceBinding:
    """Provided by an authorized caller, never inferred from document contents."""

    tenant_id: str
    company_id: str
    source_id: str
    revision: str
    sha256: str
    mime_type: str

    def __post_init__(self):
        for value in asdict(self).values():
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError("Source identity must be explicit and bounded")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("Source SHA-256 must be lowercase hexadecimal")


def _geometry(bbox, page) -> dict[str, Any]:
    width, height = page.size.width, page.size.height
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise ValueError("Invalid page dimensions")
    box = bbox.to_top_left_origin(height)
    values = (box.l, box.t, box.r, box.b)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Non-finite source coordinates")
    # Do not clamp invalid boxes into apparently valid evidence.
    if not (0 <= box.l <= box.r <= width and 0 <= box.t <= box.b <= height):
        raise ValueError("Source coordinates outside page")
    return {
        "BoundingBox": {
            "Left": box.l / width,
            "Top": box.t / height,
            "Width": (box.r - box.l) / width,
            "Height": (box.b - box.t) / height,
        }
    }


def build_evidence(
    document: DoclingDocument,
    source: SourceBinding,
    *,
    reader_version: str,
    max_pages: int = 100,
    max_output_bytes: int = 20 * 1024 * 1024,
) -> dict[str, Any]:
    """Return evidence only. No network, OCR, classification or financial writes.

    Page hooks are views, not replacements for the retained complete document.
    Table cells get geometry only when a single page provenance makes their page
    unambiguous. Multi-page tables keep their original cell data in the envelope.
    """
    if not reader_version.strip():
        raise ValueError("Reader version is required")
    if max_pages < 1 or max_output_bytes < 1 or len(document.pages) > max_pages:
        raise ValueError("Evidence limit exceeded")
    layers = set(ContentLayer)
    identity = asdict(source)
    namespace = json.dumps(identity, sort_keys=True, separators=(",", ":"))

    def block_id(anchor: str) -> str:
        return hashlib.sha256((namespace + "\n" + anchor).encode()).hexdigest()

    blocks: dict[int, list[dict[str, Any]]] = {}
    anchors: dict[str, dict[str, Any]] = {}
    unlocated: list[str] = []
    for number, page in document.pages.items():
        if number < 1 or page.page_no != number:
            raise ValueError("Inconsistent source page identity")
        if not all(
            math.isfinite(v) and v > 0 for v in (page.size.width, page.size.height)
        ):
            raise ValueError("Invalid page dimensions")
        blocks[number] = [
            {"BlockType": "PAGE", "Id": block_id(f"page:{number}"), "Page": number}
        ]

    def add_line(number: int, text: str, anchor: str, bbox=None):
        if number not in blocks:
            raise ValueError("Source anchor references a missing page")
        if not text:
            return
        block = {
            "BlockType": "LINE",
            "Id": block_id(anchor),
            "Page": number,
            "Text": text,
        }
        if bbox is not None:
            block["Geometry"] = _geometry(bbox, document.pages[number])
        anchors[block["Id"]] = {
            "documentPointer": anchor.split("/prov/")[0]
            if "/prov/" in anchor
            else anchor,
            "provenancePointer": anchor,
            "page": number,
            "granularity": "table-cell"
            if "/table_cells/" in anchor
            else "text-item-span",
        }
        blocks[number].append(block)

    for item, _ in document.iterate_items(
        traverse_pictures=True, included_content_layers=layers
    ):
        if isinstance(item, TextItem):
            if not item.prov and item.text:
                unlocated.append(item.self_ref)
            for index, prov in enumerate(item.prov):
                start, end = prov.charspan
                if not 0 <= start <= end <= len(item.text):
                    raise ValueError("Invalid source character span")
                add_line(
                    prov.page_no,
                    item.text[start:end],
                    f"{item.self_ref}/prov/{index}",
                    prov.bbox,
                )
        elif isinstance(item, TableItem):
            if len(item.prov) != 1:
                unlocated.append(item.self_ref)
            for prov in item.prov:
                if prov.page_no not in blocks:
                    raise ValueError("Table anchor references a missing page")
            if len(item.prov) == 1:
                for index, cell in enumerate(item.data.table_cells):
                    add_line(
                        item.prov[0].page_no,
                        cell.text,
                        f"{item.self_ref}/data/table_cells/{index}",
                        cell.bbox,
                    )

    hooks = {}
    for number, page_blocks in blocks.items():
        children = [block["Id"] for block in page_blocks[1:]]
        if children:
            page_blocks[0]["Relationships"] = [{"Type": "CHILD", "Ids": children}]
        hooks[str(number)] = {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "text": document.export_to_markdown(
                                page_no=number,
                                included_content_layers=layers,
                                traverse_pictures=True,
                            )
                        }
                    ],
                }
            },
            "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
            "textractBlocks": {"DocumentMetadata": {"Pages": 1}, "Blocks": page_blocks},
        }
    evidence = {
        "schemaVersion": "docling.evidence.v1",
        "source": identity,
        "reader": {
            "name": "docling",
            "version": reader_version,
            "coreVersion": version("docling-core"),
        },
        "document": document.export_to_dict(),
        "markdown": document.export_to_markdown(
            included_content_layers=layers,
            traverse_pictures=True,
        ),
        "pageHooks": hooks,
        "blockAnchors": anchors,
        "itemsWithoutUnambiguousPage": unlocated,
        "limitations": {
            "confidenceAvailable": False,
            "pageGeometryAvailable": any(
                "Geometry" in block for page in blocks.values() for block in page
            ),
            "financialAcceptance": False,
        },
    }
    if (
        len(json.dumps(evidence, ensure_ascii=False, allow_nan=False).encode())
        > max_output_bytes
    ):
        raise ValueError("Serialized evidence exceeds limit")
    return evidence
