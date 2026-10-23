# SPDX-License-Identifier: MIT-0
"""CLI trust boundary: bounded input, complete evidence and private failures."""

import hashlib
import io
import json
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest
import reader
from adapter import SourceBinding


def invoke(monkeypatch, data, **changes):
    source = asdict(
        SourceBinding(
            "tenant",
            "company",
            "source",
            "revision",
            hashlib.sha256(data).hexdigest(),
            "application/pdf",
        )
    )
    source.update(changes.pop("source", {}))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))
    return reader.main(
        [
            "--source-json",
            json.dumps(source),
            "--expected-size",
            str(changes.pop("size", len(data))),
            "--models",
            "/models",
        ]
        + changes.pop("flags", [])
    )


def test_cli_passes_bound_identity_bytes_and_limits(monkeypatch, capsys):
    calls = []

    def read(data, source, **kwargs):
        calls.append((data, source, kwargs))
        return {"body": "complete text", "tables": ["cell"]}

    monkeypatch.setattr(reader, "read_evidence", read)
    assert invoke(monkeypatch, b"PDF bytes") == 0
    assert json.loads(capsys.readouterr().out) == {
        "body": "complete text",
        "tables": ["cell"],
    }
    data, source, kwargs = calls[0]
    assert source.sha256 == hashlib.sha256(data).hexdigest()
    assert kwargs["limits"].source_bytes == 104857600
    assert kwargs["limits"].conversion_seconds == 240
    assert "ocr" not in kwargs


def test_cli_forwards_explicit_ocr_without_fallback(monkeypatch, capsys):
    calls = []

    def read(data, source, **kwargs):
        calls.append(kwargs["ocr"])
        raise RuntimeError("private OCR exception")

    monkeypatch.setattr(reader, "read_evidence", read)
    assert (
        invoke(
            monkeypatch,
            b"PDF bytes",
            flags=["--ocr-language", "latin", "--ocr-mode", "full_page"],
        )
        == 1
    )
    assert calls == [reader.LocalOcr("latin", "full_page")]
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == "reader_execution_failed\n"


@pytest.mark.parametrize("size", [0, 104857601, 2, 4])
def test_size_mismatch_never_invokes_reader(monkeypatch, capsys, size):
    monkeypatch.setattr(
        reader, "read_evidence", lambda *a, **k: pytest.fail("must not read")
    )
    assert invoke(monkeypatch, b"abc", size=size) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == "reader_execution_failed\n"


def test_cli_does_not_log_untrusted_exceptions(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("private document content")

    monkeypatch.setattr(reader, "read_evidence", fail)
    assert invoke(monkeypatch, b"abc") == 1
    assert capsys.readouterr().err == "reader_execution_failed\n"


def test_cli_rejects_invalid_source_before_read(monkeypatch, capsys):
    monkeypatch.setattr(
        reader, "read_evidence", lambda *a, **k: pytest.fail("must not read")
    )
    assert invoke(monkeypatch, b"abc", source={"sha256": "not a hash"}) == 1
    assert capsys.readouterr().err == "reader_execution_failed\n"


def test_cli_hash_failure_is_not_success(monkeypatch, capsys):
    assert invoke(monkeypatch, b"abc", source={"sha256": "a" * 64}) == 2
    assert json.loads(capsys.readouterr().out) == {
        "state": "reader_rejected",
        "reason": "source_hash_mismatch",
    }


def test_describe_requires_no_source_and_reports_installed_version(monkeypatch, capsys):
    monkeypatch.setattr(reader, "version", lambda name: "2.141.0")
    assert reader.main(["--describe"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == "2.141.0"
    with pytest.raises(SystemExit):
        reader.main(["--describe", "--expected-size", "1"])


def test_conversion_diagnostics_cannot_corrupt_json_or_erase_reading_warning(
    monkeypatch, capsys
):
    import logging

    def read(*_args, **_kwargs):
        print("upstream conversion diagnostic")
        logging.getLogger("reader-conversion-test").warning("conversion table warning")
        return {
            "reading": {"requiresReadingReview": False},
            "document": {"body": "complete"},
        }

    monkeypatch.setattr(reader, "read_evidence", read)
    assert invoke(monkeypatch, b"PDF bytes") == 0
    captured = capsys.readouterr()
    value = json.loads(captured.out)
    assert value["reading"] == {"requiresReadingReview": True, "warningCount": 1}
    assert "upstream conversion diagnostic" in captured.err
    assert "conversion table warning" in captured.err


def test_failed_conversion_keeps_stdout_empty_and_restores_logging(monkeypatch, capsys):
    import logging

    root = logging.getLogger()
    original_handlers = list(root.handlers)

    def fail(*_args, **_kwargs):
        logging.getLogger("reader-conversion-test").warning("conversion failed warning")
        raise RuntimeError("private exception body")

    monkeypatch.setattr(reader, "read_evidence", fail)
    assert invoke(monkeypatch, b"PDF bytes") == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "conversion failed warning" in captured.err
    assert "reader_execution_failed" in captured.err
    assert "private exception body" not in captured.err
    assert root.handlers == original_handlers
