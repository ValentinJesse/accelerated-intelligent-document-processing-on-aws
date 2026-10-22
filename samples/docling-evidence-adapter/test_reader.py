# SPDX-License-Identifier: MIT-0
"""Preflight rejects unsafe bytes before invoking a converter."""

import hashlib
import io
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
import reader
from adapter import SourceBinding
from reader import DOCX, PDF, ReaderRejected, ReadLimits, inspect_source, read_evidence


def binding(data, mime=DOCX):
    return SourceBinding(
        "tenant",
        "company",
        "source",
        "revision",
        hashlib.sha256(data).hexdigest(),
        mime,
    )


def docx_bytes(extra=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document/>")
        for key, value in (extra or {}).items():
            z.writestr(key, value)
    return data.getvalue()


def test_signature_and_hash_do_not_trust_declared_mime():
    data = docx_bytes()
    assert inspect_source(data, binding(data), ReadLimits()) == DOCX
    with pytest.raises(ReaderRejected, match="mime_mismatch"):
        inspect_source(data, binding(data, PDF), ReadLimits())
    with pytest.raises(ReaderRejected, match="hash_mismatch"):
        inspect_source(data, replace(binding(data), sha256="b" * 64), ReadLimits())
    with pytest.raises(ReaderRejected, match="unsupported_detected_mime"):
        inspect_source(
            b"not an invoice.pdf", binding(b"not an invoice.pdf", PDF), ReadLimits()
        )


def test_archive_expansion_and_entry_count_are_bounded():
    data = docx_bytes({"word/large.xml": "x" * 10000})
    assert len(data) < 10000
    with pytest.raises(ReaderRejected, match="archive_limit"):
        inspect_source(data, binding(data), ReadLimits(archive_bytes=1000))
    with pytest.raises(ReaderRejected, match="archive_limit"):
        inspect_source(data, binding(data), ReadLimits(archive_entries=1))


@pytest.mark.parametrize(
    "mime", ["application/octet-stream", "application/zip", "application/x-zip-compressed"]
)
def test_generic_metadata_does_not_hide_a_verified_docx(mime):
    data = docx_bytes()
    source = binding(data, mime)
    assert inspect_source(data, source, ReadLimits()) == DOCX
    assert source.mime_type == mime  # Preserve the provider's original metadata.
    with pytest.raises(ReaderRejected, match="archive_limit"):
        inspect_source(data, source, ReadLimits(archive_entries=1))


def test_generic_mime_does_not_bypass_byte_detection():
    data = b"not a supported document"
    with pytest.raises(ReaderRejected, match="unsupported_detected_mime"):
        inspect_source(data, binding(data, "application/octet-stream"), ReadLimits())


def test_source_limit_precedes_converter(monkeypatch, tmp_path):
    def forbidden(*args):
        pytest.fail("Converter must not run for rejected bytes")

    monkeypatch.setattr(reader, "_make_converter", forbidden)
    data = docx_bytes()
    with pytest.raises(ReaderRejected, match="source_byte_limit"):
        read_evidence(
            data, binding(data), models=tmp_path, limits=ReadLimits(source_bytes=1)
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pages": 0},
        {"pages": True},
        {"conversion_seconds": 241},
        {"archive_entries": 10001},
    ],
)
def test_policy_values_cannot_disable_bounds(kwargs):
    with pytest.raises(ValueError, match="Invalid reader limits"):
        ReadLimits(**kwargs)


def test_models_must_be_explicit_local_directory():
    data = docx_bytes()
    with pytest.raises(ReaderRejected, match="local_models_unavailable"):
        read_evidence(data, binding(data), models=Path("https://remote/model"))


@pytest.mark.parametrize(
    "status,errors,timed_out,pages,expected,reason",
    [
        ("partial_success", [], False, {1: {}}, 1, "conversion_incomplete"),
        ("success", ["unreadable part"], False, {1: {}}, 1, "conversion_incomplete"),
        ("success", [], True, {1: {}}, 1, "conversion_timeout"),
        ("success", [], False, {1: {}}, 2, "conversion_page_coverage"),
    ],
)
def test_partial_timeout_and_missing_pages_never_pass(
    status, errors, timed_out, pages, expected, reason
):
    from types import SimpleNamespace

    result = SimpleNamespace(
        status=SimpleNamespace(value=status),
        errors=errors,
        document=SimpleNamespace(pages=pages),
        has_timeout_errors=lambda: timed_out,
    )
    with pytest.raises(ReaderRejected, match=reason):
        reader.validate_conversion(result, expected)


def test_pdf_page_limit_rejects_whole_source_before_reading(monkeypatch, tmp_path):
    import pypdfium2

    output = io.BytesIO()
    with pypdfium2.PdfDocument.new() as pdf:
        pdf.new_page(200, 200).close()
        pdf.new_page(200, 200).close()
        pdf.save(output)
    data = output.getvalue()
    monkeypatch.setattr(
        reader,
        "_make_converter",
        lambda *args: pytest.fail("Must not read a truncated page range"),
    )
    with pytest.raises(ReaderRejected, match="pdf_page_limit"):
        read_evidence(
            data, binding(data, PDF), models=tmp_path, limits=ReadLimits(pages=1)
        )


def test_extreme_pdf_dimensions_reject_before_model_load(monkeypatch, tmp_path):
    import pypdfium2

    output = io.BytesIO()
    with pypdfium2.PdfDocument.new() as pdf:
        pdf.new_page(5000, 5000).close()
        pdf.save(output)
    data = output.getvalue()
    monkeypatch.setattr(
        reader,
        "_make_converter",
        lambda *args: pytest.fail("Must not rasterize extreme pages"),
    )
    with pytest.raises(ReaderRejected, match="pdf_page_dimensions"):
        read_evidence(data, binding(data, PDF), models=tmp_path)
