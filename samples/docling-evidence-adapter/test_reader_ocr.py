# SPDX-License-Identifier: MIT-0
"""Explicit OCR configuration cannot silently change native evidence or authority."""

import io
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest
import reader
from reader import PDF, LocalOcr, ReaderRejected, ReadLimits
from test_adapter import document_with_page
from test_reader import binding, docx_bytes


def pdf_bytes():
    import pypdfium2

    output = io.BytesIO()
    with pypdfium2.PdfDocument.new() as pdf:
        pdf.new_page(200, 400).close()
        pdf.save(output)
    return output.getvalue()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"language": ""},
        {"language": ["en", "fr"]},
        {"language": "../models"},
        {"language": "en,fr"},
        {"language": "latin", "mode": "auto"},
        {"language": "latin", "mode": "unknown"},
    ],
)
def test_settings_are_explicit_and_bounded(kwargs):
    with pytest.raises(ValueError, match="Invalid local OCR settings"):
        LocalOcr(**kwargs)


def test_ocr_rejects_office_before_loading_models(monkeypatch, tmp_path):
    monkeypatch.setattr(
        reader, "_make_converter", lambda *a: pytest.fail("Must not load OCR")
    )
    data = docx_bytes()
    with pytest.raises(ReaderRejected, match="ocr_unsupported_detected_mime"):
        reader.read_evidence(
            data, binding(data), models=tmp_path, ocr=LocalOcr("latin")
        )


@pytest.mark.parametrize("mode", [None, "pdf_aware_layout_regions", "full_page"])
def test_real_docling_options_preserve_offline_cpu_bounds(mode, tmp_path):
    from docling.datamodel.accelerator_options import AcceleratorDevice
    from docling.datamodel.base_models import InputFormat

    ocr = LocalOcr("latin", mode) if mode else None
    converter = reader._make_converter(tmp_path, ReadLimits(), ocr)
    options = converter.format_to_options[InputFormat.PDF].pipeline_options
    assert options.do_ocr is (mode is not None)
    assert options.do_table_structure is True
    assert options.enable_remote_services is False
    assert options.allow_external_plugins is False
    assert options.artifacts_path == tmp_path
    assert options.document_timeout == ReadLimits().conversion_seconds
    assert options.accelerator_options.device == AcceleratorDevice.CPU
    assert options.accelerator_options.num_threads == 2
    if ocr:
        assert options.ocr_options.backend == "onnxruntime"
        assert options.ocr_options.lang == ["latin"]
        assert options.ocr_options.mode.value == mode


@pytest.mark.parametrize("ocr", [None, LocalOcr("latin", "full_page")])
def test_reading_records_ocr_identity_without_changing_source(
    monkeypatch, tmp_path, ocr
):
    calls = []
    result = SimpleNamespace(
        document=document_with_page(),
        status=SimpleNamespace(value="success"),
        errors=[],
        has_timeout_errors=lambda: False,
    )
    monkeypatch.setattr(reader, "version", lambda name: "test-" + name)

    def converter(*args):
        calls.append(args)
        return SimpleNamespace(convert=lambda *a, **k: result)

    monkeypatch.setattr(reader, "_make_converter", converter)
    data = pdf_bytes()
    source = binding(data, PDF)
    evidence = reader.read_evidence(data, source, models=tmp_path, ocr=ocr)
    assert evidence["reading"]["sourceHashVerified"] is True
    assert evidence["reading"]["ocrEnabled"] is (ocr is not None)
    assert evidence["reading"]["byteSize"] == len(data)
    assert "Payment due in 30 days" in evidence["markdown"]
    assert "EUR 100.00" in evidence["markdown"]
    if ocr:
        assert calls[0][2] == ocr
        assert evidence["reading"]["ocr"] == {
            "engine": "rapidocr",
            "backend": "onnxruntime",
            "language": "latin",
            "mode": "full_page",
            "rapidocrVersion": "test-rapidocr",
            "onnxruntimeVersion": "test-onnxruntime",
        }
    else:
        assert len(calls[0]) == 2
        assert "ocr" not in evidence["reading"]


def test_missing_ocr_dependency_fails_before_conversion(monkeypatch, tmp_path):
    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(reader, "version", missing)
    monkeypatch.setattr(
        reader, "_make_converter", lambda *a: pytest.fail("No hidden fallback")
    )
    data = pdf_bytes()
    with pytest.raises(PackageNotFoundError):
        reader.read_evidence(
            data, binding(data, PDF), models=tmp_path, ocr=LocalOcr("latin")
        )


def test_cli_requires_language_for_mode(capsys):
    with pytest.raises(SystemExit):
        reader.main(["--describe", "--ocr-mode", "full_page"])
    assert "requires an explicit language" in capsys.readouterr().err


def test_cli_reports_explicit_ocr_versions(monkeypatch, capsys):
    import json

    monkeypatch.setattr(reader, "version", lambda name: "test-" + name)
    assert reader.main(["--describe", "--ocr-language", "latin"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ocrEnabled"] is True
    assert result["ocr"]["mode"] == "pdf_aware_layout_regions"
    assert result["ocr"]["onnxruntimeVersion"] == "test-onnxruntime"


def test_describe_missing_dependency_returns_private_failure(monkeypatch, capsys):
    def missing(name):
        raise PackageNotFoundError("private installation path")

    monkeypatch.setattr(reader, "version", missing)
    assert reader.main(["--describe", "--ocr-language", "latin"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == "reader_dependencies_unavailable\n"
