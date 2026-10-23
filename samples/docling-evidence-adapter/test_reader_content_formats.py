# SPDX-License-Identifier: MIT-0
"""Exercise the maintained HTML reader and explicit immutable format identity."""

import hashlib
from dataclasses import replace

import pytest
from adapter import SourceBinding
from reader import (
    CONTENT_FORMATS,
    DOCX,
    HTML,
    PDF,
    ReaderRejected,
    ReadLimits,
    read_evidence,
)


def binding(data, mime=PDF):
    return SourceBinding(
        "tenant",
        "company",
        "source",
        "revision",
        hashlib.sha256(data).hexdigest(),
        mime,
    )


def markup():
    return b"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Fixture</title></head>
    <body><h1>Invoice EXAMPLE-1</h1><p>EUR 120.00</p>
    <table><tr><th>Product</th><th>Amount</th></tr><tr><td>Example</td><td>120.00</td></tr></table>
    <img src="https://example.invalid/private"><img src="file:///etc/passwd">
    <script>throw Error("MUST_NOT_EXECUTE")</script></body></html>"""


def test_html_with_wrong_pdf_metadata_retains_source_and_structure(tmp_path):
    data = markup()
    source = binding(data)
    evidence = read_evidence(data, source, models=tmp_path, content_formats=True)
    assert evidence["source"]["mime_type"] == PDF
    assert evidence["source"]["sha256"] == source.sha256
    assert evidence["reading"]["detectedMime"] == HTML
    assert evidence["reading"]["formatDetection"] == CONTENT_FORMATS
    assert evidence["reading"]["mimeConflict"] is True
    assert evidence["document"]["pages"] == {}
    assert evidence["pageHooks"] == {}
    assert evidence["document"]["tables"]
    assert all(not item["prov"] for item in evidence["document"]["texts"])
    assert all(not item["prov"] for item in evidence["document"]["tables"])
    cells = [
        c["text"]
        for t in evidence["document"]["tables"]
        for c in t["data"]["table_cells"]
    ]
    assert "120.00" in cells and "Example" in cells
    assert evidence["limitations"]["financialAcceptance"] is False


def test_matching_html_and_generic_metadata_are_explicit(tmp_path):
    data = markup()
    for mime in (HTML, "application/octet-stream", "application/xhtml+xml"):
        evidence = read_evidence(
            data, binding(data, mime), models=tmp_path, content_formats=True
        )
        assert evidence["source"]["mime_type"] == mime
        assert evidence["reading"]["mimeConflict"] is (mime != HTML)


def test_html_policy_disables_every_resource_fetch(tmp_path):
    from docling.datamodel.base_models import InputFormat
    from reader import _make_converter

    options = (
        _make_converter(tmp_path, ReadLimits(), content_formats=True)
        .format_to_options[InputFormat.HTML]
        .backend_options
    )
    assert not options.render_page
    assert not options.fetch_images
    assert not options.enable_local_fetch
    assert not options.enable_remote_fetch


def test_legacy_route_still_rejects_html_before_conversion(tmp_path):
    data = markup()
    with pytest.raises(ReaderRejected, match="unsupported_detected_mime"):
        read_evidence(data, binding(data), models=tmp_path)


def test_hash_and_byte_limits_still_precede_detection(tmp_path):
    data = markup()
    with pytest.raises(ReaderRejected, match="source_hash_mismatch"):
        read_evidence(
            data,
            replace(binding(data), sha256="b" * 64),
            models=tmp_path,
            content_formats=True,
        )
    with pytest.raises(ReaderRejected, match="source_byte_limit"):
        read_evidence(
            data,
            binding(data),
            models=tmp_path,
            content_formats=True,
            limits=ReadLimits(source_bytes=1),
        )


def test_ocr_cannot_silently_enter_content_route(tmp_path):
    from reader import LocalOcr

    data = markup()
    with pytest.raises(ValueError, match="qualified separately"):
        read_evidence(
            data,
            binding(data),
            models=tmp_path,
            content_formats=True,
            ocr=LocalOcr("latin"),
        )


def test_unknown_bytes_cannot_launch_pdf_or_office_pipeline(tmp_path):
    from docling.datamodel.base_models import InputFormat
    from reader import _make_converter

    assert _make_converter(
        tmp_path, ReadLimits(), content_formats=True
    ).allowed_formats == [InputFormat.HTML]
    assert _make_converter(
        tmp_path, ReadLimits(), content_formats=True, detected_format=PDF
    ).allowed_formats == [InputFormat.PDF]
    assert _make_converter(
        tmp_path, ReadLimits(), content_formats=True, detected_format=DOCX
    ).allowed_formats == [InputFormat.DOCX]


@pytest.mark.parametrize(
    "data",
    [
        b"plain text without markup",
        b'<?xml version="1.0"?><root>data</root>',
        b"\x00\x01\x02unknown",
        b"   %PDF-1.7\ntruncated",
    ],
)
def test_unrecognized_or_malformed_input_never_returns_complete_evidence(
    tmp_path, data
):
    with pytest.raises((ReaderRejected, ValueError, RuntimeError)):
        read_evidence(data, binding(data), models=tmp_path, content_formats=True)


def test_html_never_reads_external_resources_or_entities(tmp_path, monkeypatch):
    import json
    import sys

    marker = "PRIVATE_RESOURCE_MUST_NOT_BE_READ"
    canary = tmp_path / "canary.txt"
    canary.write_text(marker)
    accesses = []
    active = [True]

    def audit(event, args):
        if active[0] and event == "open" and str(args[0]) == str(canary):
            accesses.append(event)
            raise AssertionError("HTML attempted local resource access")
        if active[0] and event == "socket.connect":
            accesses.append(event)
            raise AssertionError("HTML attempted network access")

    sys.addaudithook(audit)
    document = f'''<!DOCTYPE html [<!ENTITY external SYSTEM="{canary.as_uri()}">]>
    <html><head><link rel="stylesheet" href="https://example.invalid/style.css">
    <style>@import url(https://example.invalid/other.css); body {{background:url({canary.as_uri()})}}</style></head>
    <body><p>Invoice example &external;</p><img src="{canary.as_uri()}">
    <img src="https://example.invalid/remote"><object data="{canary.as_uri()}"></object>
    <iframe src="https://example.invalid/frame"></iframe><script src="https://example.invalid/script.js"></script>
    <table><tr><td>EUR</td><td>12.00</td></tr></table></body></html>'''.encode()
    try:
        evidence = read_evidence(
            document, binding(document), models=tmp_path, content_formats=True
        )
    finally:
        active[0] = False
    assert accesses == []
    assert marker not in json.dumps(evidence)
