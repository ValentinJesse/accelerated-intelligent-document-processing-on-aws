# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The published security snapshot must reach the verdict the gate reached.

``security/test-results/<version>/srt.md`` and the MANIFEST row above it are an
audit artifact: they are what a reader is told the release's security scan found.
``scripts/security/curate_results.py`` writes them, ``scripts/srt/run.py`` gates
on them, and for three consecutive releases they disagreed — the snapshot read
``SRT — FAIL ❌ (7 open/reopened HIGH)`` for a tree whose gate was green (#1365).

The cause is the shape this repository keeps rediscovering: **one of two rules
copied, the other not.** ``run.py`` partitions the HIGH, undispositioned findings
twice — once by whether CI's checkout can see the file at all, and once by the
B105/B106 identifier-name scope decision — and the curator reimplemented only the
first. Neither reimplementation was wrong; the second was simply absent, and
nothing compared the two verdicts.

So what is asserted here is not that the curator contains a call. It is that the
*verdict* obeys the gate's rule, on findings whose classification is derived from
this tree by the gate's own classifier:

* a name-heuristic finding in test code that no deployment artifact is built from
  does not fail the snapshot — the case that was being published as a FAIL;
* the same finding in a **test-shaped file inside a ``CodeUri``** still does, and
  this is the half an exemption written as "test files" would have got wrong:
  ``sam build`` copies those directories verbatim, so a credential in one reaches
  a customer's account;
* the same finding anywhere else still does, and so does any *other* HIGH check
  on a demoted path — the scope is per check and per path, not per path alone;
* and over a mixed report the curator's gate-blocking count equals the count the
  composition in ``run.py`` produces.

Each of those is parametrized over paths **derived from the tree**, with the
derived lists asserted non-empty: an empty parametrization is collected as a skip
rather than a failure, which is how a guarantee goes quiet here.

Two things this does not claim, both of them measured rather than supposed. Of
161 behaviour-changing mutations of ``curate_srt`` and ``ci_paths`` — every
effective line of both, deleted one at a time, plus thirteen chosen to attack the
verdict — this catches 145, and the sixteen survivors are exactly these two
shapes:

* **It compares verdicts, not documents.** Fourteen of the sixteen are the
  ``lines.append(...)`` rows and per-source counters that build the markdown: the
  published tables go wrong or empty and the verdict does not change. The one
  document property that *is* asserted is that a finding scoped out of the gate is
  still named in the snapshot, because an audit artifact that agrees with the gate
  by withholding the evidence is the worse of the two failures.
* **Both sides of a comparison derived from one classifier move together.** The
  other two survivors narrow ``ci_paths._has_test_shape`` itself. The path classes
  below come from that function, so a change to it reclassifies the fixtures and
  the curator in step and the agreement still holds. That is a property of an
  agreement gate and not a hole in it: what holds the classifier against an
  *independent* fact is ``scripts/srt/tests/test_name_heuristic_scope.py``, whose
  closure assertion is stated against the ``CodeUri`` directories the templates
  declare.

It also does not run SRT, so it says nothing about whether the scanner's own
output is complete — ``scripts/srt/scanner_health.py`` is that check.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "scripts" / "srt"))
import ci_paths  # noqa: E402

sys.path.pop(0)

pytestmark = pytest.mark.unit

#: How many paths of each class to drive the curator with. The classes hold
#: hundreds of members; what matters is that each is represented and that the
#: choice is reproducible, so it is a fixed seed over the sorted list rather than
#: a hand-picked pair that could be the two the implementation happens to suit.
SAMPLE = 4
SEED = 1365


def _load_curator():
    """Load the curator by path — it is a script, not an importable package."""
    path = REPO_ROOT / "scripts" / "security" / "curate_results.py"
    spec = importlib.util.spec_from_file_location("curate_results", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["curate_results"] = module
    spec.loader.exec_module(module)
    return module


curate = _load_curator()


@functools.lru_cache(maxsize=1)
def _path_classes() -> tuple[list[str], list[str], list[str]]:
    """Tracked ``.py`` paths split by what the gate's classifier says about them.

    Derived, never listed: ``is_test_only_path`` is the authority, and the
    ``CodeUri``/``ContentUri`` directories it consults are parsed out of the
    templates. A list of example paths written here would stop describing the tree
    the first time a Lambda's source directory moved.
    """
    packaged = ci_paths.packaged_source_dirs(REPO_ROOT)
    assert packaged is not None, (
        "the packaged-source-directory set could not be established, so every "
        "finding would keep gating and the classes below are not meaningful"
    )
    tracked = ci_paths.tracked_files(REPO_ROOT)
    assert tracked, "git listed no tracked files"

    demoted: list[str] = []
    shipped_test: list[str] = []
    ordinary: list[str] = []
    for rel in sorted(tracked):
        if not rel.endswith(".py"):
            continue
        if ci_paths.is_test_only_path(rel, packaged):
            demoted.append(rel)
        elif ci_paths._has_test_shape(rel):  # noqa: SLF001 - the authority's own rule
            shipped_test.append(rel)
        else:
            ordinary.append(rel)
    return demoted, shipped_test, ordinary


def _sample(paths: list[str]) -> list[str]:
    return random.Random(SEED).sample(paths, min(SAMPLE, len(paths)))


def _finding(path: str, check_id: str = "B105") -> dict:
    """One SRT finding, in the shape the committed register actually uses.

    ``"High"`` rather than ``"HIGH"`` on purpose: that is how SRT spells it, and
    both the gate and the curator normalise the case themselves.
    """
    return {
        "source": "Bandit",
        "path": path,
        "line": 42,
        "issue": "Possible hardcoded password: 'secret'",
        "check_id": check_id,
        "priority": "High",
        "status": "Open",
    }


def _curate(issues: list[dict], tmp_path: Path) -> tuple[str, dict]:
    report = tmp_path / "issues.json"
    report.write_text(json.dumps(issues), encoding="utf-8")
    return curate.curate_srt(report)


def _gate_verdict(issues: list[dict]) -> list[dict]:
    """The findings ``scripts/srt/run.py`` would block on, by its composition.

    Its order is: take the HIGH findings in a gating status, drop the ones CI's
    checkout cannot see, then drop the name-heuristic findings in unpackaged test
    code. Everything left blocks the build.
    """
    high_open = [
        issue
        for issue in issues
        if (issue.get("priority") or "").upper() == "HIGH"
        and ci_paths.is_gating_status(issue.get("status"))
    ]
    visible, _ = ci_paths.partition_by_ci_visibility(high_open, REPO_ROOT)
    gating, _ = ci_paths.partition_by_name_heuristic_scope(visible, REPO_ROOT)
    return gating


# --------------------------------------------------------------------------- #
# the derived classes are non-empty, or everything below is vacuous
# --------------------------------------------------------------------------- #
def test_every_path_class_this_module_needs_exists_in_the_tree() -> None:
    demoted, shipped_test, ordinary = _path_classes()
    assert demoted, (
        "no tracked .py file classifies as test code outside a packaged source "
        "directory, so the parametrizations below would collect nothing and the "
        "scope decision would be asserted against an empty set"
    )
    assert shipped_test, (
        "no tracked .py file is test-shaped AND inside a CodeUri/ContentUri tree. "
        "That is the half of the scope decision that keeps a credential in a "
        "shipped test fixture gating; with no member, nothing checks it"
    )
    assert ordinary, "no non-test-shaped tracked .py file found"


# --------------------------------------------------------------------------- #
# the verdict obeys the gate's rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("check_id", ci_paths.NAME_HEURISTIC_EXEMPT)
@pytest.mark.parametrize("path", _sample(_path_classes()[0]))
def test_a_name_heuristic_finding_in_unpackaged_test_code_does_not_fail_the_snapshot(
    path: str, check_id: str, tmp_path: Path
) -> None:
    """The case that was published as a FAIL for three releases.

    `make srt-scan` reports these and exits 0. A snapshot that calls the same tree
    a failure is not a stricter reading of the same evidence; it is a different
    verdict on it, and the reader has no way to tell which one the release was
    actually gated on.
    """
    _, meta = _curate([_finding(path, check_id)], tmp_path)
    assert meta["gate"] == "pass", (
        f"the curator fails the snapshot on {check_id} in {path}, which the gate "
        f"does not block on. open_high={meta['open_high']}"
    )
    assert meta["name_scoped"] == 1, (
        "the finding was neither blocked nor reported as scoped out, so the "
        "snapshot has silently dropped it"
    )


@pytest.mark.parametrize("check_id", ci_paths.NAME_HEURISTIC_EXEMPT)
@pytest.mark.parametrize("path", _sample(_path_classes()[1]))
def test_the_same_finding_in_a_packaged_test_file_still_fails_the_snapshot(
    path: str, check_id: str, tmp_path: Path
) -> None:
    """Test-shaped is not the same as does-not-ship, and the difference ships.

    ``sam build`` copies a ``CodeUri`` directory verbatim and nothing here prunes
    it, so a hardcoded credential in one of these runs in a customer's account.
    An exemption phrased "test files" demotes 99 of them; the gate's classifier
    does not, and neither may the curator.
    """
    _, meta = _curate([_finding(path, check_id)], tmp_path)
    assert meta["gate"] == "fail", (
        f"the curator passes the snapshot on {check_id} in {path}, which sits "
        "inside a directory a deployment artifact is built from and which the "
        "gate does block on"
    )


@pytest.mark.parametrize("path", _sample(_path_classes()[2]))
def test_a_name_heuristic_finding_outside_test_code_still_fails_the_snapshot(
    path: str, tmp_path: Path
) -> None:
    _, meta = _curate([_finding(path)], tmp_path)
    assert meta["gate"] == "fail", (
        f"the curator passes the snapshot on B105 in {path}, which is not test code"
    )


@pytest.mark.parametrize("path", _sample(_path_classes()[0]))
def test_another_high_check_on_the_same_path_still_fails_the_snapshot(
    path: str, tmp_path: Path
) -> None:
    """The scope is per (check, path), and a path-only reading of it is a hole.

    B608 is Bandit's SQL-injection check: a finding of it in a test file is a
    finding about code, not about an identifier's name, and demoting it because
    of where it sits would blind the snapshot to every HIGH in every test tree.
    """
    _, meta = _curate([_finding(path, "B608")], tmp_path)
    assert meta["gate"] == "fail", (
        f"the curator passes the snapshot on B608 in {path}. Only the two "
        f"identifier-name checks {list(ci_paths.NAME_HEURISTIC_EXEMPT)} are scoped "
        "out, and only in unpackaged test code"
    )


@pytest.mark.parametrize("status", ci_paths.GATING_STATUSES)
@pytest.mark.parametrize("path", _sample(_path_classes()[2]))
def test_every_status_the_gate_blocks_on_fails_the_snapshot_too(
    path: str, status: str, tmp_path: Path
) -> None:
    """``reopened`` blocks the same as ``open``, and only one of them is obvious.

    SRT assigns ``reopened`` to a finding it had recorded as resolved or
    suppressed and has detected again, and counts it in its own "N issues need
    attention" line; ``resolved`` is not a sticky disposition. Gating on ``open``
    alone published a PASS with a re-detected HIGH sitting in the matrix one
    section below. The statuses are taken from ``ci_paths.GATING_STATUSES`` rather
    than written out here, so the two cannot be changed apart.
    """
    _, meta = _curate([dict(_finding(path, "B608"), status=status)], tmp_path)
    assert meta["gate"] == "fail", (
        f"a HIGH finding with status {status!r} does not fail the snapshot, but "
        f"ci_paths.GATING_STATUSES says the gate blocks on it"
    )


@pytest.mark.parametrize(
    "status", ["suppressed", "resolved"], ids=["suppressed", "resolved"]
)
@pytest.mark.parametrize("path", _sample(_path_classes()[2])[:1])
def test_a_dispositioned_finding_does_not_fail_the_snapshot(
    path: str, status: str, tmp_path: Path
) -> None:
    """The other direction, so the status filter cannot be widened to everything."""
    _, meta = _curate([dict(_finding(path, "B608"), status=status)], tmp_path)
    assert meta["gate"] == "pass", (
        f"a {status} finding fails the snapshot; the gate accepts that disposition"
    )


@functools.lru_cache(maxsize=1)
def _an_ignored_path() -> str:
    """A path git ignores, taken from git rather than from a guess.

    The curated snapshot reconciles to CI's view by dropping findings in files a
    clean checkout does not contain — a local tree that has been built carries
    ``.aws-sam/`` artifacts and vendored trees that ``srt assess`` scans happily
    and CI never sees. That is the partition the curator *did* implement, and it
    had no test either.
    """
    candidates = [
        ".aws-sam/packaged.yaml",
        "scratch/probe.py",
        "src/ui/node_modules/probe.js",
        ".srt/issues.json",
    ]
    ignored = curate._git_ignored_set(candidates)  # noqa: SLF001 - the curator's own rule
    assert ignored, (
        "none of the candidate paths is git-ignored, so the CI-visibility half of "
        f"the reconciliation cannot be exercised: {candidates}"
    )
    return sorted(ignored)[0]


def test_a_finding_in_a_file_cis_checkout_cannot_see_does_not_fail_the_snapshot(
    tmp_path: Path,
) -> None:
    _, meta = _curate([_finding(_an_ignored_path(), "B608")], tmp_path)
    assert meta["gate"] == "pass", (
        f"a HIGH finding in {_an_ignored_path()} fails the snapshot. That file is "
        "git-ignored, so CI's clean checkout does not contain it and the gate "
        "cannot see the finding at all"
    )
    assert meta["total"] == 0, (
        "the finding was counted among the CI-visible findings as well"
    )


def test_the_two_verdicts_agree_on_a_mixed_report(tmp_path: Path) -> None:
    """The comparison the three bad snapshots needed, on a report with both kinds.

    Both halves are required to be non-trivial: a report that produced no demoted
    findings, or none that gate, would be satisfied by a curator that had either
    rule and not the other.
    """
    demoted, shipped_test, ordinary = _path_classes()
    issues = (
        [_finding(path) for path in _sample(demoted)]
        + [_finding(path, "B106") for path in _sample(shipped_test)]
        + [_finding(path, "B608") for path in _sample(ordinary)]
        + [
            dict(_finding(path, "B105"), status="suppressed")
            for path in _sample(ordinary)
        ]
    )
    gating = _gate_verdict(issues)
    _, meta = _curate(issues, tmp_path)
    assert len(gating) > 0 and meta["name_scoped"] > 0, (
        "the mixed report does not exercise both rules: "
        f"{len(gating)} gating, {meta['name_scoped']} scoped out"
    )
    assert meta["open_high"] == len(gating), (
        "the curated snapshot and the CI gate disagree on how many findings block: "
        f"snapshot {meta['open_high']}, gate {len(gating)}"
    )
    assert meta["gate"] == ("fail" if gating else "pass")
    # Scoping a finding out of the GATE must not take it out of the REPORT. Every
    # path above is tracked, so all of them are visible to CI's checkout and all of
    # them belong in the priority × status matrix; applying the scope decision to
    # the whole finding list rather than to the gating subset would still reach the
    # right verdict while quietly shrinking the counts the snapshot publishes.
    assert meta["total"] == len(issues), (
        f"the snapshot counts {meta['total']} CI-visible findings from a report of "
        f"{len(issues)}, all of which are tracked files"
    )


def test_the_snapshot_names_the_findings_it_scoped_out(tmp_path: Path) -> None:
    """Not gating is not the same as not reporting.

    The gate prints every demoted finding under its own heading. A snapshot that
    merely stopped counting them would agree with the gate's verdict and withhold
    the evidence for it, which for an audit artifact is the worse of the two
    failures.
    """
    paths = _sample(_path_classes()[0])
    markdown, meta = _curate([_finding(path) for path in paths], tmp_path)
    assert meta["name_scoped"] == len(paths)
    for path in paths:
        assert path in markdown, (
            f"{path} was scoped out of the gate and does not appear in the "
            "published document"
        )


def test_the_curator_uses_the_gate_s_own_classifier_rather_than_a_copy() -> None:
    """One definition, two consumers — the property a copy satisfies today.

    A private reimplementation in the curator would pass every assertion above on
    the day it was written and diverge on the next change to the rule, which is
    exactly how this defect arrived: the git-ignored partition *was* reimplemented
    here, correctly, and the second rule was then added to only one of the two.
    """
    function = curate.partition_by_name_heuristic_scope
    assert function.__module__ == "ci_paths", (
        "curate_results.partition_by_name_heuristic_scope comes from "
        f"{function.__module__!r}, not from scripts/srt/ci_paths.py. The scope "
        "decision has been copied rather than shared, and the copy will drift"
    )
    assert function is ci_paths.partition_by_name_heuristic_scope
