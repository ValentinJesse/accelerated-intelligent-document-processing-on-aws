# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the web UI's `deleteDocument` resolver Lambda.

The resolver returns **one boolean** for a whole selection, and the UI sends the whole
multi-select in one call — so that boolean is the only thing a user sees about a delete
that did not finish. It used to answer `deleted_count > 0 or failed_count == 0`, which is
`True` whenever *any* document in the selection was removed: nine deleted and one
tracking row orphaned read exactly like ten deleted (#1238). These tests assert the
counts-to-boolean mapping directly, because that expression is where the truth this
module works to produce was being discarded.

The resolver is loaded from its Lambda source by path, the way the other resolver tests
here do it, with `boto3` patched so no client is constructed and no credential is used.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def resolver(monkeypatch):
    """The resolver module, with its module-level boto3 resources mocked out."""
    monkeypatch.setenv("TRACKING_TABLE_NAME", "tracking")
    monkeypatch.setenv("INPUT_BUCKET", "in")
    monkeypatch.setenv("OUTPUT_BUCKET", "out")
    path = os.path.join(
        os.path.dirname(__file__),
        "../../../../nested/api-resolvers/src/lambda/delete_document_resolver/index.py",
    )
    with patch("boto3.resource"), patch("boto3.client"):
        spec = importlib.util.spec_from_file_location("delete_document_resolver", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["delete_document_resolver"] = module
        spec.loader.exec_module(module)
    yield module
    sys.modules.pop("delete_document_resolver", None)


def _event(keys: list[str]) -> dict:
    return {
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "arguments": {"objectKeys": keys},
    }


def _results(*successes: bool) -> list:
    return [
        {
            "success": ok,
            "object_key": f"k{i}",
            "errors": [] if ok else ["ThrottlingException"],
        }
        for i, ok in enumerate(successes)
    ]


@pytest.mark.unit
class TestTheReportedOutcome:
    @pytest.mark.parametrize(
        "successes, succeeds",
        [
            ((True,), True),
            ((False,), False),
            ((True, True, True), True),
            ((True, True, False), False),
            ((False, True, True), False),
            ((False, False, False), False),
        ],
        ids=[
            "one-ok",
            "one-failed",
            "all-ok",
            "last-failed",
            "first-failed",
            "all-failed",
        ],
    )
    def test_any_document_that_failed_makes_the_call_fail(
        self, resolver, successes, succeeds
    ):
        """⚠️ **A failed delete raises; it does not return a value.**

        Returning `failed_count == 0` was a *successful* invocation whose result was
        `false`, and nothing between here and the user looked at that value: the REST
        dispatcher answers 200 for any value a resolver yields, and the UI logged the
        result at debug level and navigated away. So the shape in #1366 — version rows
        gone, list row and document record kept, two faults in the log — reached the
        browser as an untroubled 200.

        A raise is the only outcome the transport distinguishes, which is why the
        assertion here is about the exception rather than about a boolean.
        """
        keys = [f"k{i}" for i in range(len(successes))]
        with patch.object(
            resolver, "delete_single_document", side_effect=_results(*successes)
        ):
            if succeeds:
                assert resolver.handler(_event(keys), MagicMock()) is True
            else:
                with pytest.raises(Exception) as caught:
                    resolver.handler(_event(keys), MagicMock())
                assert not isinstance(caught.value, (PermissionError, ValueError)), (
                    "a failed delete must not be reported as a bad request or a "
                    "denial — the dispatcher maps those to 400/403"
                )

    def test_a_partial_failure_is_not_reported_as_a_complete_delete(self, resolver):
        """The measured defect, spelled out separately from the table above.

        Two of three documents were removed and the third's tracking-list row is still
        in the list. Multi-select is the ordinary UI path, so this is the shape a user
        meets — and the failure has to name which document and why, because the cause
        is normally a deployment-level fault identical for every document and
        actionable only if it is said out loud.
        """
        with patch.object(
            resolver,
            "delete_single_document",
            side_effect=_results(True, True, False),
        ) as deleter:
            with pytest.raises(RuntimeError) as caught:
                resolver.handler(_event(["a", "b", "c"]), MagicMock())

        assert deleter.call_count == 3, "a failure must not abandon the rest"
        message = str(caught.value)
        # The keys the CALLER asked about, which is what a user can act on — the
        # per-document result dict carries its own `object_key` and the resolver does
        # not read it back out.
        assert "c" in message, f"the failing document is not named: {message}"
        assert "ThrottlingException" in message, (
            f"the cause is not reported to the caller: {message}"
        )
        assert "2 of 3" in message, f"the partial count is not reported: {message}"

    def test_the_raise_carries_every_failure_not_just_the_first(self, resolver):
        """All of them, because a deployment fault fails every document the same way.

        Reporting only the first would make a selection of ten look like one bad
        document rather than one bad deployment.
        """
        with patch.object(
            resolver,
            "delete_single_document",
            side_effect=_results(False, True, False),
        ):
            with pytest.raises(RuntimeError) as caught:
                resolver.handler(_event(["a", "b", "c"]), MagicMock())

        message = str(caught.value)
        assert "a:" in message and "c:" in message, message
        assert "b:" not in message, f"a document that succeeded is named: {message}"

    def test_a_caller_outside_the_two_groups_is_refused(self, resolver):
        # Defence in depth, unchanged by this change and worth keeping pinned: the
        # boolean above is only reachable by an Admin or an Author.
        event = {
            "identity": {"claims": {"cognito:groups": ["Viewer"]}},
            "arguments": {"objectKeys": ["a"]},
        }
        with pytest.raises(PermissionError):
            resolver.handler(event, MagicMock())
