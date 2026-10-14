# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1363: an extraction already committed to agent state survives a truncation.

**The mechanism, and why it is a fixed structure rather than model flakiness.**
With ``extraction.mode: advanced`` the section is extracted by a Strands agent
loop, and with ``extraction.confidence.mode: integrated`` that same loop is also
asked for per-field confidence — the agent calls ``provide_field_assessment``
AFTER the extraction tool call has already validated. Nothing bounds what that one
call may emit: the separate confidence pass caps a call at
``extraction.confidence.list_batch_size`` rows (25 by default) and the auto-sizer
at 50, while the integrated call is handed every row the shard holds, which on a
5-page slice of a transaction ledger is a couple of hundred. When the resulting
turn runs into the model's output ceiling, Strands raises
``MaxTokensReachedException`` out of the event loop instead of returning an
``AgentResult`` — so ``_invoke_agent_for_extraction`` never reached the code that
reads ``agent.state["current_extraction"]``, the exception propagated through the
shard, and the Distributed Map (which tolerates no shard failure) failed the whole
document. The rows were extracted, validated and sitting in agent state the entire
time.

Note what is NOT asserted here: nothing about how verbose any particular model is.
Both ``claude-sonnet-4-6`` and ``claude-sonnet-5`` resolve to a 128,000-token
output cap in ``config_library/model_config_limits.yaml``, so the budget is not
what differs between them. What these tests pin is the part that is deterministic
from code alone — that a truncated turn discarded a validated extraction, and no
longer does.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from idp_common.extraction.runtime import (
    _accumulate_metering,
    _shard_list_row_count,
    collate_shard_assessments,
)
from idp_common.utils.bedrock_utils import is_max_tokens_agent_error

# The real strands package, not the MagicMock stub ``tests/conftest.py`` installs
# for a bare environment — ``agentic_idp`` is the module under test and the
# exception it must recognise is Strands'. Same prelude as
# ``test_invalid_tool_use_sequence.py``, and for the same reason: a test under
# ``tests/unit/extraction/agentic_idp/`` would be skipped wherever the real
# packages are absent, and this behaviour has to be covered in CI.
for _name in [
    k for k in list(sys.modules) if k == "strands" or k.startswith("strands.")
]:
    if isinstance(sys.modules[_name], MagicMock):
        del sys.modules[_name]

_agentic = pytest.importorskip(
    "idp_common.extraction.agentic_idp",
    reason="strands-agents not installed (a stub does not count)",
)
_strands_exceptions = pytest.importorskip(
    "strands.types.exceptions",
    reason="strands-agents not installed (a stub does not count)",
)
MaxTokensReachedException = _strands_exceptions.MaxTokensReachedException

#: The message Strands puts on it, as it appeared in the #1363 shard logs.
_MESSAGE = "Agent has reached an unrecoverable state due to max_tokens limit."


class _Row(BaseModel):
    Date: str
    Description: str
    Amount: float


class _Statement(BaseModel):
    AccountNumber: str | None = None
    Transactions: list[_Row] = []


def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {
            "Date": "2024-01-01",
            "Description": f"PURCHASE SEQ{i:05d}",
            "Amount": float(i),
        }
        for i in range(n)
    ]


class _State:
    """``AgentState``'s read/write surface, enough for the loop under test."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = dict(values)

    def get(self, key: str) -> Any:
        return self._values.get(key)

    def set(self, key: str, value: Any) -> None:
        self._values[key] = value


class _Metrics:
    """Stands in for ``EventLoopMetrics``: the shape ``_accumulate_token_usage`` reads."""

    def __init__(self, usage: dict[str, int]) -> None:
        self.accumulated_usage = usage


class _Agent:
    def __init__(self, state: dict[str, Any], usage: dict[str, int] | None = None):
        self.state = _State(state)
        self.event_loop_metrics = _Metrics(
            usage or {"inputTokens": 80_000, "outputTokens": 128_000}
        )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _invoke(agent, *, raises: BaseException, data_format=_Statement):
    """Drive the real ``_invoke_agent_for_extraction`` over a failing agent turn."""

    async def _boom(agent, input):  # noqa: ARG001 - signature parity
        raise raises

    with patch.object(_agentic, "invoke_agent_with_retry", _boom):
        return _run(
            _agentic._invoke_agent_for_extraction(
                agent=agent,
                prompt_content=[],
                data_format=data_format,
                max_extraction_retries=3,
                model_id="us.anthropic.claude-sonnet-4-6",
            )
        )


class TestTheSalvage:
    """The product's own failure, reproduced from code and configuration alone."""

    def test_a_validated_extraction_survives_a_max_tokens_death(self):
        """239 rows, validated by the class model, were thrown away.

        This is the #1363 shard: ``finalize_table_extraction`` had already logged
        "All data validated successfully", so ``current_extraction`` holds rows the
        class model accepts, and the turn that died is the ``provide_field_assessment``
        one after it.
        """
        agent = _Agent({"current_extraction": {"Transactions": _rows(239)}})
        response, result = _invoke(agent, raises=MaxTokensReachedException(_MESSAGE))
        assert result is not None, (
            "a validated extraction in agent state must not be discarded because "
            "a later turn ran into the model's output ceiling"
        )
        assert len(result.Transactions) == 239
        assert isinstance(response, _agentic.TruncatedAgentRun)

    def test_the_salvaged_run_still_meters_the_tokens_it_spent(self):
        """A truncated turn is expensive; a salvage that reported zero would hide it."""
        agent = _Agent(
            {"current_extraction": {"Transactions": _rows(3)}},
            usage={"inputTokens": 1_234, "outputTokens": 128_000},
        )
        response, _result = _invoke(agent, raises=MaxTokensReachedException(_MESSAGE))
        usage = _agentic._initialize_token_usage()
        _agentic._accumulate_token_usage(response, usage)
        assert usage["outputTokens"] == 128_000
        assert usage["inputTokens"] == 1_234

    def test_the_stand_in_response_reads_as_the_reason(self):
        """``str(response)`` becomes the returned message text, so it must say why."""
        agent = _Agent({"current_extraction": {"Transactions": _rows(1)}})
        response, _result = _invoke(agent, raises=MaxTokensReachedException(_MESSAGE))
        assert "max output tokens" in str(response)

    def test_the_log_names_the_remedy(self, caplog):
        agent = _Agent({"current_extraction": {"Transactions": _rows(5)}})
        with caplog.at_level("ERROR"):
            _invoke(agent, raises=MaxTokensReachedException(_MESSAGE))
        text = caplog.text
        assert "max_pages_per_shard" in text
        assert "integrated" in text


class TestWhatIsNotSalvaged:
    """The salvage is bounded on both axes: the error, and what state holds."""

    def test_an_empty_state_still_raises(self):
        """Nothing was committed, so there is nothing to keep."""
        with pytest.raises(MaxTokensReachedException):
            _invoke(_Agent({}), raises=MaxTokensReachedException(_MESSAGE))

    def test_state_the_class_model_rejects_still_raises(self):
        """A half-written row is not an extraction; failing is the honest answer."""
        agent = _Agent(
            {"current_extraction": {"Transactions": [{"Date": "2024-01-01"}]}}
        )
        with pytest.raises(MaxTokensReachedException):
            _invoke(agent, raises=MaxTokensReachedException(_MESSAGE))

    def test_a_different_failure_is_not_salvaged(self):
        """Only an output-ceiling stop. Anything else keeps failing the section."""
        agent = _Agent({"current_extraction": {"Transactions": _rows(4)}})
        with pytest.raises(RuntimeError):
            _invoke(agent, raises=RuntimeError("model stream broke"))

    def test_no_empty_result_is_synthesised(self):
        """Every leaf of the shard transport model is nullable (#782/#1078), so an
        empty object validates. Keeping one would complete the document with no
        fields, which is worse than failing it."""

        class _AllNullable(BaseModel):
            AccountNumber: str | None = None

        with pytest.raises(MaxTokensReachedException):
            _invoke(
                _Agent({"current_extraction": {}}),
                raises=MaxTokensReachedException(_MESSAGE),
                data_format=_AllNullable,
            )


class TestThePredicate:
    """One matcher for Strands' truncation exception, shared with rules discovery."""

    def test_the_real_exception_matches(self):
        assert is_max_tokens_agent_error(MaxTokensReachedException(_MESSAGE)) is True

    def test_the_type_name_alone_is_enough(self):
        """Matched by name so this module need not import strands (lean Lambdas)."""

        class MaxTokensReachedException(Exception):  # noqa: N818 - mirrors Strands
            pass

        assert is_max_tokens_agent_error(MaxTokensReachedException("anything")) is True

    @pytest.mark.parametrize(
        "message",
        [
            "Agent has reached an unrecoverable state due to max_tokens limit.",
            "stopped due to max_tokens",
        ],
    )
    def test_the_message_alone_is_enough(self, message):
        assert is_max_tokens_agent_error(Exception(message)) is True

    @pytest.mark.parametrize(
        "message", ["ThrottlingException", "Input is too long", "end_turn", ""]
    )
    def test_an_unrelated_failure_does_not_match(self, message):
        assert is_max_tokens_agent_error(Exception(message)) is False

    def test_rules_discovery_answers_through_the_same_matcher(self):
        """Two copies of this predicate drift; the delegate is what stops that."""
        from idp_common.discovery.rules_discovery import _is_max_tokens_error

        assert _is_max_tokens_error(MaxTokensReachedException(_MESSAGE)) is True
        assert _is_max_tokens_error(Exception("ThrottlingException")) is False


class TestTheTruncationMarkerSurvivesTheMerge:
    """``_output_truncated`` rides in metering, which is what the merge carries."""

    def test_it_is_not_summed_like_a_token_counter(self):
        merged: dict[str, Any] = {}
        _accumulate_metering(merged, {"m": {"outputTokens": 5}})
        _accumulate_metering(
            merged, {"m": {"outputTokens": 7}, "_output_truncated": True}
        )
        assert merged["m"]["outputTokens"] == 12
        assert merged["_output_truncated"] is True

    def test_one_truncated_shard_marks_the_section(self):
        """Logical OR, in both orders: a later clean shard must not clear it."""
        first: dict[str, Any] = {}
        _accumulate_metering(first, {"_output_truncated": True})
        _accumulate_metering(first, {"m": {"outputTokens": 1}})
        assert first["_output_truncated"] is True

        second: dict[str, Any] = {}
        _accumulate_metering(second, {"m": {"outputTokens": 1}})
        _accumulate_metering(second, {"_output_truncated": True})
        assert second["_output_truncated"] is True

    def test_an_untruncated_section_carries_no_marker(self):
        merged: dict[str, Any] = {}
        _accumulate_metering(merged, {"m": {"outputTokens": 1}})
        assert "_output_truncated" not in merged


class TestAGapIsNotCollated:
    """A shard with no assessment must not shift its neighbours' confidence.

    ``merge_assessment_dicts`` concatenates the list assessments of the shards that
    have one, and ``reconcile_assessment_to_data`` only truncates or pads at the
    TAIL — so a middle shard going unassessed moves every later shard's per-row
    confidence (and bounding box) onto earlier rows, with the lengths agreeing and
    nothing reporting it. Reachable before #1363 too (an agent that simply does not
    call ``provide_field_assessment`` on one shard), and the salvage above makes it
    more reachable, so the collation refuses the gap instead.
    """

    def _shard(self, rows: list[dict], assessed: bool, page_start: int):
        data = _Statement(Transactions=rows)
        response: dict[str, Any] = {"metering": {}}
        if assessed:
            response["_shard_assessment"] = {
                "assessment": {
                    "Transactions": [{"Amount": {"confidence": 0.9}} for _ in rows]
                },
                "alerts": [],
                "page_start": page_start,
                "page_end": page_start + 1,
            }
        return (data, response)

    def test_every_shard_assessed_collates_in_page_order(self):
        results = [
            self._shard(_rows(2), True, 0),
            self._shard(_rows(3), True, 1),
        ]
        collated = collate_shard_assessments(results, _Statement)
        assert collated is not None
        merged, _alerts = collated
        assert len(merged["Transactions"]) == 5

    def test_a_row_bearing_shard_with_no_assessment_voids_the_collation(self):
        results = [
            self._shard(_rows(2), True, 0),
            self._shard(_rows(3), False, 1),
            self._shard(_rows(2), True, 2),
        ]
        assert collate_shard_assessments(results, _Statement) is None, (
            "collating 4 assessment rows against 7 data rows would attribute the "
            "third shard's confidence to the second shard's rows"
        )

    def test_the_refusal_says_what_will_score_the_section(self, caplog):
        results = [
            self._shard(_rows(2), True, 0),
            self._shard(_rows(3), False, 1),
        ]
        with caplog.at_level("WARNING"):
            collate_shard_assessments(results, _Statement)
        assert "standalone Assessment step" in caplog.text

    def test_a_shard_with_no_rows_and_no_assessment_shifts_nothing(self):
        """So it must not void an otherwise complete collation."""
        results = [
            self._shard(_rows(2), True, 0),
            self._shard([], False, 1),
        ]
        collated = collate_shard_assessments(results, _Statement)
        assert collated is not None
        merged, _alerts = collated
        assert len(merged["Transactions"]) == 2

    def test_no_shard_assessed_is_still_none(self):
        results = [self._shard(_rows(2), False, 0)]
        assert collate_shard_assessments(results, _Statement) is None

    @pytest.mark.parametrize(
        "data,expected",
        [
            (_Statement(Transactions=[]), 0),
            (
                _Statement(
                    Transactions=[{"Date": "d", "Description": "x", "Amount": 1}]
                ),
                1,
            ),
            ({"Transactions": [1, 2, 3]}, 3),
            (None, 0),
        ],
    )
    def test_the_row_count_reads_both_shapes(self, data, expected):
        """The SFN merge holds model instances, the in-process path the same —
        but a plain dict is what a test or a future caller is most likely to pass."""
        assert _shard_list_row_count(data, {"Transactions"}) == expected
