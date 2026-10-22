# SPDX-License-Identifier: MIT-0
"""Bounded local Docling invocation; execute in an isolated, supervised worker."""

import hashlib
import io
import time
import zipfile
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import filetype
from adapter import SourceBinding, build_evidence

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ReaderRejected(ValueError):
    """Stable accounting code, without document text, filenames or library logs."""


@dataclass(frozen=True)
class ReadLimits:
    source_bytes: int = 20 * 1024 * 1024
    output_bytes: int = 20 * 1024 * 1024
    pages: int = 10
    page_edge_points: int = 4096
    page_area_points: int = 4_000_000
    archive_entries: int = 10000
    archive_bytes: int = 128 * 1024 * 1024
    conversion_seconds: int = 120

    def __post_init__(self):
        ceilings = (
            100 * 1024 * 1024,
            20 * 1024 * 1024,
            100,
            14400,
            16_000_000,
            10000,
            256 * 1024 * 1024,
            240,
        )
        for value, ceiling in zip(self.__dict__.values(), ceilings, strict=True):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("Invalid reader limits")


def inspect_source(data: bytes, source: SourceBinding, limits: ReadLimits) -> str:
    """Inspect bytes, not extension or product/company-specific naming patterns."""
    if not isinstance(data, bytes) or not 0 < len(data) <= limits.source_bytes:
        raise ReaderRejected("source_byte_limit")
    if hashlib.sha256(data).hexdigest() != source.sha256:
        raise ReaderRejected("source_hash_mismatch")
    detected = filetype.guess_mime(data)
    if detected not in (PDF, DOCX):
        raise ReaderRejected("unsupported_detected_mime")
    generic_mime = source.mime_type == "application/octet-stream" or (
        source.mime_type in ("application/zip", "application/x-zip-compressed")
        and detected == DOCX
    )
    if detected != source.mime_type and not generic_mime:
        raise ReaderRejected("source_mime_mismatch")
    if detected == DOCX:
        started = time.monotonic()
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > limits.archive_entries
                    or sum(e.file_size for e in entries) > limits.archive_bytes
                ):
                    raise ReaderRejected("archive_limit")
                names = [entry.filename for entry in entries]
                if len(set(names)) != len(names):
                    raise ReaderRejected("archive_duplicate_entry")
                if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                    raise ReaderRejected("invalid_docx_container")
                total = 0
                for entry in entries:
                    if entry.flag_bits & 1:
                        raise ReaderRejected("encrypted_archive")
                    with archive.open(entry) as stream:
                        while chunk := stream.read(65536):
                            total += len(chunk)
                            if total > limits.archive_bytes:
                                raise ReaderRejected("archive_limit")
                            if time.monotonic() - started > limits.conversion_seconds:
                                raise ReaderRejected("archive_time_limit")
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
            raise ReaderRejected("invalid_docx_container") from exc
    return detected


def _make_converter(models: Path, limits: ReadLimits):
    # Lazy imports let preflight reject malformed/unsupported bytes before ML load.
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        artifacts_path=models,
        do_ocr=False,
        do_table_structure=True,
        enable_remote_services=False,
        allow_external_plugins=False,
        document_timeout=limits.conversion_seconds,
        accelerator_options=AcceleratorOptions(
            num_threads=2, device=AcceleratorDevice.CPU
        ),
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )


def validate_conversion(result, expected_pages: int | None) -> None:
    """Partial conversion is an explicit failure, never usable complete evidence."""
    if result.has_timeout_errors():
        raise ReaderRejected("conversion_timeout")
    if result.status.value != "success" or result.errors:
        raise ReaderRejected("conversion_incomplete")
    if expected_pages is not None and len(result.document.pages) != expected_pages:
        raise ReaderRejected("conversion_page_coverage")


def read_evidence(
    data: bytes,
    source: SourceBinding,
    *,
    models: Path,
    limits: ReadLimits = ReadLimits(),
) -> dict:
    """Bind exact immutable bytes to complete reader evidence, without accepting it.

    Docling's timeout is cooperative. The calling sandbox must independently
    enforce wall time, memory, network denial and credential isolation. This
    function uses local models without fallback; the caller owns persistence.
    """
    started = time.monotonic()
    detected = inspect_source(data, source, limits)
    if not models.is_absolute() or not models.is_dir():
        raise ReaderRejected("local_models_unavailable")
    page_count = None
    if detected == PDF:
        import pypdfium2

        try:
            with pypdfium2.PdfDocument(data) as pdf:
                page_count = len(pdf)
                if not 0 < page_count <= limits.pages:
                    raise ReaderRejected("pdf_page_limit")
                for index in range(page_count):
                    width, height = pdf.get_page_size(index)
                    if not (
                        0 < width <= limits.page_edge_points
                        and 0 < height <= limits.page_edge_points
                        and width * height <= limits.page_area_points
                    ):
                        raise ReaderRejected("pdf_page_dimensions")
        except pypdfium2.PdfiumError as exc:
            raise ReaderRejected("unreadable_pdf") from exc

    from docling.datamodel.base_models import DocumentStream

    converter = _make_converter(models, limits)
    result = converter.convert(
        DocumentStream(
            name="source.pdf" if detected == PDF else "source.docx",
            stream=io.BytesIO(data),
        ),
        raises_on_error=False,
        max_num_pages=limits.pages,
        max_file_size=limits.source_bytes,
    )
    validate_conversion(result, page_count)
    evidence = build_evidence(
        result.document,
        source,
        reader_version=version("docling"),
        max_pages=limits.pages,
        max_output_bytes=limits.output_bytes,
    )
    unrepresented_pages = [
        int(page)
        for page, hook in evidence["pageHooks"].items()
        if not any(
            block.get("Text", "").strip() for block in hook["textractBlocks"]["Blocks"]
        )
    ]
    has_text = any(item.text.strip() for item in result.document.texts) or any(
        cell.text.strip()
        for table in result.document.tables
        for cell in table.data.table_cells
    )
    evidence["reading"] = {
        "detectedMime": detected,
        "byteSize": len(data),
        "ocrEnabled": False,
        "sourceHashVerified": True,
        "conversionStatus": result.status.value,
        "pagesWithoutAnchoredText": unrepresented_pages,
        "requiresReadingReview": bool(unrepresented_pages) or not has_text,
        "seconds": round(time.monotonic() - started, 3),
        "limits": dict(limits.__dict__),
    }
    # Metadata is part of the same envelope budget.
    import json

    if (
        len(json.dumps(evidence, ensure_ascii=False, allow_nan=False).encode())
        > limits.output_bytes
    ):
        raise ReaderRejected("output_byte_limit")
    return evidence


def main(argv=None):
    """Credential-free CLI for a supervising caller; source authority stays outside."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--source-json")
    parser.add_argument("--expected-size", type=int)
    parser.add_argument("--models", type=Path)
    args = parser.parse_args(argv)
    limits = ReadLimits(
        source_bytes=100 * 1024 * 1024,
        output_bytes=8 * 1024 * 1024,
        pages=100,
        conversion_seconds=240,
    )
    if args.describe:
        if any(
            v is not None for v in (args.source_json, args.expected_size, args.models)
        ):
            parser.error("Describe does not accept source inputs")
        print(
            json.dumps(
                {
                    "reader": "docling",
                    "version": version("docling"),
                    "maxSourceBytes": limits.source_bytes,
                    "maxPages": limits.pages,
                    "maxOutputBytes": limits.output_bytes,
                    "ocrEnabled": False,
                }
            )
        )
        return 0
    try:
        if not args.source_json or len(args.source_json.encode()) > 16384:
            raise ValueError("Invalid source descriptor")
        if (
            args.expected_size is None
            or not 0 < args.expected_size <= limits.source_bytes
        ):
            raise ValueError("Invalid source size")
        source = SourceBinding(**json.loads(args.source_json))
        data = sys.stdin.buffer.read(args.expected_size + 1)
        if len(data) != args.expected_size:
            raise ValueError("Source byte length changed")
        evidence = read_evidence(data, source, models=args.models, limits=limits)
        wire = json.dumps(evidence, ensure_ascii=False, allow_nan=False)
        if len(wire.encode()) > limits.output_bytes:
            raise ReaderRejected("output_byte_limit")
        print(wire)
        return 0
    except ReaderRejected as error:
        # Library-defined fixed codes only; no document content or path is logged.
        print(json.dumps({"state": "reader_rejected", "reason": str(error)}))
        return 2
    except Exception:
        print("reader_execution_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
