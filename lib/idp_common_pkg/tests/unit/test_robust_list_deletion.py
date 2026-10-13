# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``create_document_resolver``'s own copy of the list-entry cleanup.

This module is a second implementation of the same shard cascade that lives in
``idp_common.delete_documents``, and it is **live**: ``create_document_resolver``
calls it when a document key is re-uploaded, to remove the previous list row so the
Document List does not show the document twice. It is duplicated rather than imported
because ``CreateDocumentResolverFunction`` declares no ``Layers`` and so has no
``idp_common`` — which is also why fixing the shared copy of #1366 did nothing for
this one.

Its failure mode here is quieter than the delete resolver's and still user visible: a
re-uploaded document whose previous list row sat under a different ``QueuedTime`` than
the one its record carries. Strategy 1 aims at the exact key and misses; strategies 2
and 3 are the ones that look for the row wherever it is, and both went through a
shard query whose ``FilterExpression`` named the sort key — which DynamoDB refuses in
a ``Query`` — so they raised, were caught, returned ``[]``, and the old row stayed.
The document then appears twice.

⚠️ **Both call sites matter and only one of them is obvious.** The initial query and
the query inside the ``while 'LastEvaluatedKey' in response`` loop carried the same
expression as two separate literals. Repairing the first alone leaves the paginated
path broken, and that is the harder half to notice: it needs a shard partition large
enough to page, so it appears on a long-lived stack and not on a fresh one.

This file had no tests at all before, which is the other half of how the defect
survived here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from idp_common.delete_documents import (
    list_entry_names_document as shared_predicate,
)

pytestmark = pytest.mark.unit

KEY = "batch-1/invoice.pdf"
QUEUED = "2025-09-10T12:03:27.256164+00:00"
#: Hour 12 falls in shard 03 — six four-hour shards per day.
SHARD_PK = "list#2025-09-10#s#03"

PROJECTION = "PK, SK, ObjectKey"


def _load():
    path = os.path.join(
        os.path.dirname(__file__),
        "../../../../nested/api-resolvers/src/lambda/create_document_resolver/"
        "robust_list_deletion.py",
    )
    spec = importlib.util.spec_from_file_location("robust_list_deletion", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["robust_list_deletion"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rld():
    return _load()


def _row(marker: str, pk: str = SHARD_PK, object_key: str | None = None) -> dict:
    row = {"PK": pk, "SK": f"ts#{marker}#id#{object_key or KEY}"}
    if object_key is not None:
        row["ObjectKey"] = object_key
    return row


def _table() -> MagicMock:
    table = MagicMock()
    table.query.return_value = {"Items": []}
    table.delete_item.return_value = {}
    return table


class TestTheShardQuery:
    def test_it_names_no_key_attribute_in_a_filter(self, rld):
        """The repair. A ``FilterExpression`` here is refused by DynamoDB outright."""
        table = _table()
        rld.query_shard_for_object_key(table, SHARD_PK, KEY)
        kwargs = table.query.call_args.kwargs
        assert "FilterExpression" not in kwargs
        assert kwargs["ProjectionExpression"] == PROJECTION
        assert "KeyConditionExpression" in kwargs

    def test_both_shapes_of_list_entry_are_selected(self, rld):
        by_sort_key = _row("a")
        by_attribute = {
            "PK": SHARD_PK,
            "SK": "ts#b#id#something-else",
            "ObjectKey": KEY,
        }
        other = _row("c", object_key="other/doc.pdf")
        table = _table()
        table.query.return_value = {"Items": [by_sort_key, by_attribute, other]}

        found = rld.query_shard_for_object_key(table, SHARD_PK, KEY)

        assert found == [by_sort_key, by_attribute], (
            "both shapes must be found and another document's row must not be"
        )

    def test_the_paginated_call_is_repaired_too(self, rld):
        """⚠️ The second call site, which the first fix misses.

        The pagination query was a separate literal carrying the same rejected
        expression, so a shard large enough to page kept failing after the initial
        call was repaired. Asserted on **every** call's arguments rather than the
        first, which is what makes this distinguishable from the single-page case.
        """
        first, second = _row("a"), _row("b")
        # ⚠️ A non-matching row on the SECOND page, which is what makes this test see
        # the predicate rather than only the pagination. With every row on both pages
        # matching, a paginated branch that appends unconditionally produces an
        # identical result — measured: that exact mutation survived this suite until
        # this row was added.
        stranger = _row("z", object_key="someone/else.pdf")
        table = _table()
        table.query.side_effect = [
            {"Items": [first], "LastEvaluatedKey": {"PK": SHARD_PK, "SK": "cursor"}},
            {"Items": [second, stranger]},
        ]

        found = rld.query_shard_for_object_key(table, SHARD_PK, KEY)

        assert found == [first, second], (
            "the second page was not collected, or its rows were not filtered"
        )
        assert table.query.call_count == 2
        for index, call in enumerate(table.query.call_args_list):
            assert "FilterExpression" not in call.kwargs, (
                f"call {index} still carries a FilterExpression"
            )
            assert call.kwargs["ProjectionExpression"] == PROJECTION
        assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {
            "PK": SHARD_PK,
            "SK": "cursor",
        }

    def test_a_bare_mock_response_does_not_paginate_for_ever(self, rld):
        """The double, not DynamoDB, is what makes this loop dangerous.

        ``MagicMock().get("LastEvaluatedKey")`` auto-creates a **truthy** value, so a
        loop written with ``.get()`` never terminates against a bare mock and the
        collected list grows without bound. This loop reads membership, which answers
        ``False``. Asserted with a hard call cap so a regression fails fast instead of
        exhausting the host.
        """
        table = MagicMock()
        calls = {"n": 0}

        def query(**_kwargs):
            calls["n"] += 1
            assert calls["n"] < 50, "the pagination loop did not terminate"
            return MagicMock()

        table.query.side_effect = query
        rld.query_shard_for_object_key(table, SHARD_PK, KEY)
        assert calls["n"] == 1


class TestTheCascade:
    def test_strategy_two_finds_and_deletes_the_row(self, rld):
        table = _table()
        # Strategy 1 aims at the exact computed key and misses; strategy 2 finds it.
        table.delete_item.side_effect = [{}, {"Attributes": {"x": 1}}]
        table.query.return_value = {"Items": [_row("elsewhere")]}

        assert (
            rld.delete_list_entries_robust(table, KEY, {"QueuedTime": QUEUED}) is True
        )

    def test_strategy_three_searches_the_adjacent_shards(self, rld):
        """The case the cascade exists for: the row landed one shard over.

        This is the path that was wholly unreachable while the query raised, and the
        one a re-upload under a drifted timestamp depends on.
        """
        table = _table()
        table.delete_item.side_effect = [{}, {"Attributes": {"x": 1}}]
        table.query.side_effect = [
            {"Items": []},  # computed shard: nothing
            {"Items": [_row("n", pk="list#2025-09-10#s#02")]},  # neighbour: hit
            {"Items": []},
        ]

        assert (
            rld.delete_list_entries_robust(table, KEY, {"QueuedTime": QUEUED}) is True
        )
        assert table.query.call_count >= 2, "the adjacent shards were never searched"

    def test_no_metadata_means_no_strategy_runs(self, rld):
        table = _table()
        assert rld.delete_list_entries_robust(table, KEY, None) is False
        table.delete_item.assert_not_called()


class TestParityWithTheSharedImplementation:
    """The duplication is deliberate; the two diverging is not.

    ``robust_list_deletion`` cannot import ``idp_common`` — its Lambda has no layer —
    so the predicate exists twice. A test is the only thing that can hold them in
    step, and the selection is the part where a divergence silently changes which rows
    a cleanup touches.
    """

    CASES = [
        {"ObjectKey": KEY},
        {"SK": f"ts#{QUEUED}#id#{KEY}"},
        {"SK": f"ts#{QUEUED}#id#{KEY}", "ObjectKey": KEY},
        {"ObjectKey": "other.pdf", "SK": f"ts#{QUEUED}#id#other.pdf"},
        {"ObjectKey": f"{KEY}.bak", "SK": f"ts#{QUEUED}#id#{KEY}.bak"},
        {},
        {"SK": None},
        {"SK": 7},
        {"ObjectKey": None},
        {"SK": f"ts#{QUEUED}#id#prefix-{KEY}"},
    ]

    def test_the_case_matrix_is_not_empty(self):
        """A parametrisation over an empty list collects as a skip, not a failure."""
        assert len(self.CASES) >= 10

    @pytest.mark.parametrize("entry", CASES, ids=range(len(CASES)))
    def test_the_two_predicates_agree(self, rld, entry):
        assert rld.list_entry_names_document(entry, KEY) is shared_predicate(
            entry, KEY
        ), (
            f"the vendored predicate and idp_common's disagree on {entry!r}; "
            "they decide which rows a cleanup deletes"
        )

    def test_the_matrix_exercises_both_answers(self, rld):
        """Otherwise agreement could be agreement on one constant answer."""
        answers = {rld.list_entry_names_document(e, KEY) for e in self.CASES}
        assert answers == {True, False}


def test_the_vendored_copy_still_has_no_idp_common_layer():
    """Why the duplication is still necessary, asserted rather than remembered.

    If ``CreateDocumentResolverFunction`` gains the ``idp_common`` layer, the right
    change is to delete this module and import the shared one — and the parity tests
    above would keep passing over two copies nobody needs. This fails at that point
    and says so.
    """
    import yaml

    repo = Path(__file__).resolve()
    while not (repo / "nested" / "api-resolvers").is_dir():
        repo = repo.parent

    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {f"Fn::{suffix}": loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {f"Fn::{suffix}": loader.construct_sequence(node, deep=True)}
        return {f"Fn::{suffix}": loader.construct_mapping(node, deep=True)}

    _Loader.add_multi_constructor("!", _tag)
    template = yaml.load(
        (repo / "nested" / "api-resolvers" / "template.yaml").read_text(),
        Loader=_Loader,
    )
    properties = template["Resources"]["CreateDocumentResolverFunction"]["Properties"]
    assert "Layers" not in properties, (
        "CreateDocumentResolverFunction now has a layer: if it is the idp_common "
        "base layer, delete robust_list_deletion.py and import "
        "idp_common.delete_documents instead of keeping two copies in step"
    )
