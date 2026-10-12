# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A response the model did not finish is reported (#1361).

An extraction whose output was cut off mid-answer holds a PREFIX of the data: the
scalars and the rows that arrived are correct, the JSON is well formed, every
accuracy and confidence signal is computed over what came back, and the document
completes. ``output_truncated`` already recorded this — into section metadata that
no report, no status path and no UI read. This pins the two halves that make it
visible:

* the flag is set from the response's own ``stopReason``, not only from a JSON
  parse failure, so a ceiling hit whose text still parses is seen; and
* ``_build_extraction_issues`` emits ``extraction_output_truncated``, so the
  section carries the finding instead of completing with nothing recorded.

⚠️ **A clean ``stopReason`` is not evidence that the output is complete, and the
measurements behind #1361 are the reason to say so here.** On a 17-page, 800-row
statement the short Simple-mode runs spent ~2,290-2,477 extraction output tokens
where the complete runs on the SAME model spent ~38,333-38,535
(``benchmarks/results/v0.6.11/coresynth__extraction-model-sonnet5``), and
``_invoke_extraction_model`` passes ``max_tokens=None`` so the client requests the
model's maximum. No ceiling was involved in those runs: the model abbreviated and
stopped with ``end_turn``. ``extraction_rows_below_ocr_estimate`` (#843) is what
covers that shape, and ``TestAStopReasonIsNotACompletenessCheck`` holds both
statements in one place so neither check is read as doing the other's job.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from idp_common.config.models import IDPConfig
from idp_common.discovery.rules_discovery import RulesDiscovery
from idp_common.extraction.service import (
    ExtractionResult,
    ExtractionService,
    SectionInfo,
)
from idp_common.models import Document, Section, Status
from idp_common.utils.bedrock_utils import (
    OUTPUT_TRUNCATION_STOP_REASONS,
    is_output_token_truncation,
)

CODE = ExtractionService.OUTPUT_TRUNCATED_CODE
ROW_SHORTFALL_CODE = ExtractionService.ROW_SHORTFALL_CODE

#: The stop reasons are read from the module that defines them rather than restated,
#: so a value added there is exercised here without an edit. That the derived set is
#: non-empty gets its own test
#: (``TestTheMatcher::test_the_stop_reason_vocabulary_is_not_empty``) rather than
#: being left to the parametrisations, because an empty ``parametrize`` collects as a
#: **skip** — it would void every assertion below while reporting no failure.

ROW = {
    "type": "object",
    "properties": {
        "Date": {"type": "string"},
        "Description": {"type": "string"},
        "Amount": {"type": "number"},
    },
}
SCHEMA = {
    "type": "object",
    "properties": {
        "Account Number": {"type": "string"},
        "Transactions": {"type": "array", "items": ROW},
    },
}


def _svc(*, agentic: bool = False) -> ExtractionService:
    cfg = IDPConfig(
        **{
            "extraction": {
                "mode": "advanced" if agentic else "simple",
                "agentic": {"enabled": agentic},
            }
        }
    )
    svc = ExtractionService(config=cfg)
    svc._reset_context()
    svc._class_schema = SCHEMA
    return svc


def _rows(n: int) -> list[dict]:
    return [
        {
            "Date": "2024-01-01",
            "Description": f"PURCHASE SEQ{i:05d}",
            "Amount": float(i),
        }
        for i in range(n)
    ]


def _ocr(rows: int, pages: int = 17) -> str:
    """Textract-``TABLES``-like Markdown: a 3-column ledger reprinted per page."""
    per = -(-rows // pages)
    blocks = []
    for p in range(pages):
        lines = ["| Date | Description | Amount |", "|---|---|---|"]
        for i in range(p * per, min(rows, (p + 1) * per)):
            lines.append(f"| 2024-01-01 | PURCHASE SEQ{i:05d} | {i}.00 |")
        blocks.append("\n".join(lines))
    return "\n\nPage break.\n\n".join(blocks)


def _section_info() -> SectionInfo:
    return SectionInfo(
        class_label="Statement",
        sorted_page_ids=["1"],
        page_indices=[0],
        output_bucket="out",
        output_key="d.pdf/sections/1/result.json",
        output_uri="s3://out/d.pdf/sections/1/result.json",
        start_page=1,
        end_page=1,
    )


def _response(text: str, stop_reason: str) -> dict:
    """The envelope ``bedrock.invoke_model`` returns, with a chosen stop reason."""
    return {
        "response": {
            "stopReason": stop_reason,
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "usage": {"inputTokens": 10, "outputTokens": 20},
        },
        "metering": {"Extraction/bedrock/m": {"outputTokens": 20, "requests": 1}},
    }


def _invoke(svc: ExtractionService, text: str, stop_reason: str) -> ExtractionResult:
    """Drive the REAL ``_invoke_extraction_model`` over a chosen response."""
    with patch(
        "idp_common.extraction.service.bedrock.invoke_model",
        return_value=_response(text, stop_reason),
    ):
        return svc._invoke_extraction_model(
            content=[{"text": "prompt"}],
            system_prompt="sys",
            section_info=_section_info(),
        )


def _save(svc: ExtractionService, result: ExtractionResult):
    """Persist ``result`` through the real tail; returns (write mock, doc, section)."""
    doc = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
    )
    section = Section(section_id="1", classification="Statement", page_ids=["1"])
    doc.sections = [section]
    with patch("idp_common.extraction.service.s3.write_content") as write:
        svc._save_results(doc, section, result, _section_info(), "1", 0.0)
    return write, doc, section


def _codes(issues) -> list[str]:
    return [i.code for i in issues or []]


COMPLETE_JSON = json.dumps({"Account Number": "1", "Transactions": _rows(43)})
#: A response cut off mid-row: unparseable, and the repair reader can see why.
CUT_OFF_JSON = COMPLETE_JSON[: COMPLETE_JSON.index("SEQ00030")]


class TestTheStopReasonIsRead:
    """``_invoke_extraction_model`` must read the model's own verdict.

    Before this, ``output_truncated`` was set in ONE place — the ``except`` branch
    around ``json.loads``. A ceiling hit whose text still parses never reaches that
    branch, so the flag stayed False for a response the model had said it did not
    finish.
    """

    @pytest.mark.parametrize("stop_reason", list(OUTPUT_TRUNCATION_STOP_REASONS))
    def test_a_ceiling_stop_is_seen_even_when_the_text_parses(self, stop_reason):
        svc = _svc()
        result = _invoke(svc, COMPLETE_JSON, stop_reason)
        assert result.parsing_succeeded, (
            "the fixture must parse cleanly, or this test measures the repair "
            "branch instead of the stop-reason read"
        )
        assert result.output_truncated is True, (
            f"stopReason={stop_reason!r} with parseable text must still be "
            f"recorded as truncated"
        )

    def test_a_finished_response_is_not_marked_truncated(self):
        svc = _svc()
        result = _invoke(svc, COMPLETE_JSON, "end_turn")
        assert result.parsing_succeeded
        assert result.output_truncated is False

    def test_the_repair_readers_verdict_still_stands_on_its_own(self):
        """The pre-existing path: unparseable text, an ordinary stop reason."""
        svc = _svc()
        result = _invoke(svc, CUT_OFF_JSON, "end_turn")
        assert result.output_truncated is True
        assert result.output_repaired, "the fixture must exercise the repair reader"

    def test_a_ceiling_stop_survives_a_successful_repair(self):
        """Both sources agree; the OR must not let the second overwrite the first."""
        svc = _svc()
        result = _invoke(svc, CUT_OFF_JSON, "max_tokens")
        assert result.output_truncated is True

    def test_a_ceiling_stop_survives_a_repair_that_reports_nothing(self):
        """The direction the OR exists for: repair says ``was_truncated: False``.

        Assigning rather than ORing would erase the model's own verdict here, and
        this is the shape that does it — the text does not parse, so the branch
        runs, and the reader's answer is False.
        """
        svc = _svc()
        with patch(
            "idp_common.extraction.service.repair_truncated_json",
            return_value=({"Account Number": "1"}, {"was_truncated": False}),
        ):
            result = _invoke(svc, "not json at all", "max_tokens")
        assert result.output_truncated is True


class TestTheFindingReachesTheSection:
    """A flag nothing reads is not a signal. This is the half that reports it."""

    def test_a_truncated_result_carries_the_issue(self):
        svc = _svc()
        write, doc, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        assert CODE in _codes(section.processing_issues), _codes(
            section.processing_issues
        )
        issue = next(i for i in section.processing_issues if i.code == CODE)
        assert issue.severity == "warning"
        assert issue.stage == "extraction"
        assert issue.details["truncated_list_fields"] == ["Transactions"]
        # Durable, not merely in memory: this is what a reader opens.
        persisted = write.call_args.args[0]["metadata"]["processing_issues"]
        assert [i for i in persisted if i["code"] == CODE]

    def test_a_complete_result_carries_no_truncation_issue(self):
        svc = _svc()
        _write, _doc, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=False,
            ),
        )
        assert CODE not in _codes(section.processing_issues)

    def test_the_message_names_a_remedy_and_says_why_nothing_else_flags_it(self):
        svc = _svc()
        _w, _d, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        msg = next(i for i in section.processing_issues if i.code == CODE).message
        assert "Advanced" in msg and "shards" in msg
        assert "confidence" in msg
        assert "Transactions" in msg

    def test_advanced_mode_is_not_told_to_switch_to_advanced_mode(self):
        svc = _svc(agentic=True)
        _w, _d, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        msg = next(i for i in section.processing_issues if i.code == CODE).message
        assert "use Advanced" not in msg
        assert "max_pages_per_shard" in msg

    def test_the_warning_does_not_fail_the_section(self):
        """Severity is ``warning``, always — the scope line of this change.

        ``extraction.row_shortfall_action`` governs ``ROW_SHORTFALL_CODE`` only, so
        a deployment that has opted into ``fail`` must not start failing runs that
        complete today because of a code it never chose.
        """
        cfg = IDPConfig(
            **{
                "extraction": {
                    "mode": "simple",
                    "agentic": {"enabled": False},
                    "row_shortfall_action": "fail",
                }
            }
        )
        svc = ExtractionService(config=cfg)
        svc._reset_context()
        svc._class_schema = SCHEMA
        # No OCR text, so the row-shortfall check cannot fire and the only issue
        # under test is this one.
        _w, doc, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        issue = next(i for i in section.processing_issues if i.code == CODE)
        assert issue.severity == "warning"
        assert doc.errors == []


class TestAStopReasonIsNotACompletenessCheck:
    """The two checks answer different questions, and #1361's runs prove it.

    Measured on ``large_narrow.pdf`` (800 rows, 17 pages) at v0.6.11: the short
    Simple-mode runs used ~2.3k extraction output tokens against complete runs'
    ~38.5k on the same model, with ``max_tokens=None`` requesting the model's
    maximum — so those runs stopped voluntarily and no stop-reason check can see
    them. Each check must cover its own case and neither may be read as covering
    the other's.
    """

    def test_a_voluntary_stop_losing_most_rows_is_caught_by_the_row_check_only(self):
        svc = _svc()
        svc._document_text = _ocr(800)
        result = _invoke(svc, COMPLETE_JSON, "end_turn")
        assert result.output_truncated is False
        _w, _d, section = _save(svc, result)
        codes = _codes(section.processing_issues)
        assert CODE not in codes, (
            "a voluntary stop must not be reported as a ceiling hit — that would "
            "be a false cause on the one issue a reader opens for the cause"
        )
        assert ROW_SHORTFALL_CODE in codes, codes

    def test_both_fire_together_when_both_are_true(self):
        """Overlap is deliberate: one names the cause, the other counts the rows."""
        svc = _svc()
        svc._document_text = _ocr(800)
        result = _invoke(svc, COMPLETE_JSON, "max_tokens")
        _w, _d, section = _save(svc, result)
        codes = _codes(section.processing_issues)
        assert CODE in codes and ROW_SHORTFALL_CODE in codes, codes

    def test_truncation_is_reported_with_no_ocr_text_at_all(self):
        """The reach the row check does not have.

        ``extraction_rows_below_ocr_estimate`` needs OCR that emits Markdown tables
        (Textract ``TABLES`` or BDA) and a table whose width matches the list item.
        A LAYOUT-only or table-less OCR configuration leaves it inert; this check
        needs neither OCR nor a list.
        """
        svc = _svc()
        svc._document_text = ""
        result = _invoke(svc, COMPLETE_JSON, "max_tokens")
        _w, _d, section = _save(svc, result)
        codes = _codes(section.processing_issues)
        assert CODE in codes
        assert ROW_SHORTFALL_CODE not in codes, (
            "the row check must stay inert without table OCR, or this test is "
            "measuring that one instead"
        )


class TestTheMatcher:
    def test_the_stop_reason_vocabulary_is_not_empty(self):
        """Every parametrisation in this module is derived from this set.

        An empty one would collect as a skip, so the emptiness has to be a
        failure somewhere, and this is where.
        """
        assert len(OUTPUT_TRUNCATION_STOP_REASONS) > 0

    @pytest.mark.parametrize("stop_reason", list(OUTPUT_TRUNCATION_STOP_REASONS))
    def test_every_declared_stop_reason_matches_in_both_shapes(self, stop_reason):
        assert is_output_token_truncation({"stopReason": stop_reason}) is True
        assert (
            is_output_token_truncation({"response": {"stopReason": stop_reason}})
            is True
        )

    @pytest.mark.parametrize(
        "value", ["end_turn", "tool_use", "stop_sequence", "content_filtered", "", None]
    )
    def test_an_ordinary_stop_does_not_match(self, value):
        assert is_output_token_truncation({"stopReason": value}) is False
        assert is_output_token_truncation({"response": {"stopReason": value}}) is False

    @pytest.mark.parametrize("value", [None, "max_tokens", 7, [], object()])
    def test_a_shape_it_cannot_read_is_not_a_truncation(self, value):
        assert is_output_token_truncation(value) is False

    def test_a_wrapper_whose_response_member_is_not_a_dict_falls_back(self):
        """``{"response": None}`` must not be read as "no stop reason"."""
        assert (
            is_output_token_truncation({"response": None, "stopReason": "max_tokens"})
            is True
        )

    @pytest.mark.parametrize("stop_reason", list(OUTPUT_TRUNCATION_STOP_REASONS))
    def test_rules_discovery_answers_through_the_same_matcher(self, stop_reason):
        assert (
            RulesDiscovery._response_was_truncated(
                {"response": {"stopReason": stop_reason}}
            )
            is True
        )
        assert (
            RulesDiscovery._response_was_truncated({"stopReason": "end_turn"}) is False
        )


class TestOneVocabulary:
    """No second copy of the stop-reason set anywhere in the package.

    Three call sites asked this question with three inline copies of the same
    tuple. A fourth copy is how they drift, and a drifted copy is invisible: each
    site stays internally consistent and its own tests keep passing. Source-read
    rather than imported, so the assertion is about what is committed.
    """

    PKG = Path(__file__).resolve().parents[3] / "idp_common"

    def test_the_literal_set_is_declared_exactly_once(self):
        files = sorted(self.PKG.rglob("*.py"))
        assert files, f"no sources found under {self.PKG}"
        hits = []
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Tuple, ast.Set, ast.List)):
                    continue
                values = {
                    el.value
                    for el in node.elts
                    if isinstance(el, ast.Constant) and isinstance(el.value, str)
                }
                if "max_tokens" in values and "incomplete" in values:
                    hits.append(str(path.relative_to(self.PKG)))
        assert hits == ["utils/bedrock_utils.py"], hits


class TestBothDocTiersCarryTheCode:
    """A warning a user can see is documented in both tiers."""

    #: Asserted rather than assumed: a wrong depth here would turn every
    #: assertion below into a ``FileNotFoundError`` about the wrong tree.

    REPO = Path(__file__).resolve().parents[5]

    def test_the_repository_root_resolves(self):
        assert (self.REPO / "CHANGELOG.md").is_file(), self.REPO

    @pytest.mark.parametrize(
        "rel",
        [
            "docs/extraction-and-confidence.md",
            "lib/idp_common_pkg/idp_common/extraction/README.md",
        ],
    )
    def test_the_code_is_described(self, rel):
        text = (self.REPO / rel).read_text(encoding="utf-8")
        assert CODE in text, f"{rel} does not mention {CODE}"

    @pytest.mark.parametrize(
        "rel",
        [
            "docs/extraction-and-confidence.md",
            "lib/idp_common_pkg/idp_common/extraction/README.md",
        ],
    )
    def test_no_tier_claims_the_stop_reason_detects_an_abbreviated_list(self, rel):
        """The over-claim this change must not ship.

        A reader told that extraction now detects truncation will stop looking for
        the row check, and the measured #1361 runs are exactly the ones a stop
        reason cannot see.
        """
        text = (self.REPO / rel).read_text(encoding="utf-8")
        for para in re.split(r"\n\s*\n", text):
            if CODE not in para:
                continue
            assert not re.search(
                r"\b(all|any|every)\b[^.]{0,80}\btruncat", para, re.IGNORECASE
            ), para[:400]
