# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every Step 0 startup sweep must report whether it actually ran (#1434).

All six `cleanup_stale_*` reapers in `codebuild_deployment.py` shared one shape:
a single top-level `except Exception` whose body was one `print`, no return
value, and a bare call in `main()`. So a run whose enumeration was refused
considered ZERO targets, established nothing about the account, printed one `⚠️`
line into a six-figure-line build log, and ended green — indistinguishable from
a run that enumerated and found nothing to reap. #1433 removed that shape from
the bucket sweep; this module is the same removal asserted for the class.

**Parametrised off `STARTUP_SWEEPS`, not off a list written here.** The
registry in the module under test is the source of the membership, and
`test_every_cleanup_function_in_the_module_is_registered` derives the universe
from the module's own source, so a seventh reaper added later is either covered
by everything below or fails in a named way. The per-sweep *driving* data
(`_SWEEP_SPECS`) is unavoidably per-sweep — they call different APIs — and a
registered sweep with no spec fails rather than being quietly skipped.

Three directions per sweep, and the middle one is the direction a careless fix
breaks:

* an enumeration failing is a finding — asserted on the returned OUTCOME, never
  on a printed string, because a printed string is the thing that was already
  there and that nobody read. A sweep with two enumerations gets a case per
  enumeration, since a test that only fails the second misses half the surface;
* enumerating and finding nothing is still the quiet path with NO finding. A
  reaper that reports a problem on every green deploy trains the reader to ignore
  the row, which is how the warning it replaces came to be ignored;
* one resource that would not delete stays best effort — named, and not a
  finding, because one stuck resource must not cost a night's deploy.

And the decision that is per member rather than per class: `fails_run`. Two of
the six report without ending the run red, for two different reasons recorded in
the registry, and both directions are pinned — a sweep marked fatal must flip the
verdict, and a sweep marked non-fatal must not.

What this module does NOT pin, so a green run is not over-read: live behaviour.
Every fake here is a stand-in, so none of this says the deployed CodeBuild role
can make the calls. `test_stale_bucket_reaper.py` pins the one grant that is
checkable offline (`s3:ListAllMyBuckets`); the equivalent question for the other
five sweeps' calls needs account access no in-tree gate has.

The precedence of `main()`'s one-line `failure_reason` is also unpinned, as it
was in #1433 — but it is no longer *mutable* in the way that noted: the four
fatal sweeps spell it `failure_reason = failure_reason or reason`, which has no
guard to invert, where the shape #1433 left behind was `if not failure_reason:`.
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HARNESS = Path(__file__).resolve().parents[1] / "codebuild_deployment.py"

#: Older than every age gate in the module (the widest is the 30-day diagnostics
#: retention window), so one constant serves all six sweeps.
STALE_AGE_SECONDS = 31 * 24 * 3600

#: A run prefix whose stacks are all gone, so nothing protects its leftovers.
DEAD_RUN = "idp-0101-000000"


# ---------------------------------------------------------------------------
# One fake AWS surface for all six sweeps
#
# ⚠️ A double that cannot fail the enumeration call makes every assertion below
# vacuous. Failure is therefore expressed the way the API expresses it — the call
# raises — and `failing` names which call does, so the same object drives the
# clean, the enumeration-failure and the per-item-failure cases.
#
# The method names the six sweeps use do not collide, so one object can stand in
# for every service `boto3.client` is asked for, which is also what
# test_stale_bucket_reaper.py's fake does.
# ---------------------------------------------------------------------------


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return self._pages


class _FakeAws:
    """Account contents plus a chosen failure, standing in for every client."""

    def __init__(self, contents=None, failing=None, breaking=None):
        self.contents = contents or {}
        self.failing = failing
        self.breaking = breaking
        self.deleted = []

    def _check(self, api):
        if self.failing == api:
            raise RuntimeError(f"AccessDenied: {api}")

    # --- enumerations -----------------------------------------------------
    def get_paginator(self, api):
        self._check(api)
        return _FakePaginator(self.contents.get(api, [{}]))

    def list_data_automation_projects(self):
        self._check("list_data_automation_projects")
        return {"projects": self.contents.get("projects", [])}

    def list_buckets(self):
        self._check("list_buckets")
        return {"Buckets": self.contents.get("buckets", [])}

    def describe_stacks(self, StackName):  # noqa: N803 - boto3 casing
        self._check("describe_stacks")
        # Terminal, so the BDA sweep reads the run as inactive and its
        # blueprints as stale. A describe that cannot answer is a different
        # question, handled by the sweep's own per-prefix handler.
        return {"Stacks": [{"StackStatus": "DELETE_COMPLETE"}]}

    # --- per-item actions -------------------------------------------------
    def _delete(self, api, name):
        if self.breaking == api:
            raise RuntimeError(f"{api} refused for {name}")
        self.deleted.append(name)

    def delete_blueprint(self, blueprintArn, blueprintVersion=None):  # noqa: N803
        if blueprintVersion is not None:
            # The sweep's own version-delete attempt, swallowed by design.
            return None
        self._delete("delete_blueprint", blueprintArn)
        return None

    def delete_data_automation_project(self, projectArn):  # noqa: N803
        self._delete("delete_data_automation_project", projectArn)

    def delete_stack(self, StackName):  # noqa: N803
        self._delete("delete_stack", StackName)

    def delete_bucket(self, Bucket):  # noqa: N803
        self._delete("delete_bucket", Bucket)

    def delete_object(self, Bucket, Key):  # noqa: N803
        self._delete("delete_object", Key)


class _FakeS3Resource:
    """`boto3.resource("s3")`, for the bucket sweep's empty-then-delete."""

    def __init__(self):
        self.emptied = []

    def Bucket(self, name):  # noqa: N802 - boto3 casing
        emptied = self.emptied

        class _Bucket:
            @property
            def object_versions(self):
                class _Versions:
                    def delete(self):
                        emptied.append(name)

                return _Versions()

        return _Bucket()


def _contents(cbd, sweep, with_target):
    """What the account holds so `sweep` has exactly one reapable target.

    Per sweep, because each enumerates something different. `with_target=False`
    is the clean case: every listing answers, and holds nothing this sweep wants.
    """
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(seconds=STALE_AGE_SECONDS)

    if sweep == "cleanup_stale_bda_blueprints":
        if not with_target:
            return {"list_blueprints": [{"blueprints": []}], "projects": []}
        return {
            "list_blueprints": [
                {
                    "blueprints": [
                        {
                            "blueprintName": DEAD_RUN,
                            "blueprintArn": f"arn:fake:blueprint/{DEAD_RUN}",
                        }
                    ]
                }
            ],
            "projects": [],
        }

    if sweep == "cleanup_stale_apigw_test_vpcs":
        names = [f"{DEAD_RUN}-apigw-vpc"] if with_target else []
        return {
            "list_stacks": [
                {
                    "StackSummaries": [
                        {"StackName": n, "CreationTime": old} for n in names
                    ]
                }
            ]
        }

    if sweep == "cleanup_stale_idp_stacks":
        names = [DEAD_RUN] if with_target else []
        return {
            "list_stacks": [
                {
                    "StackSummaries": [
                        {"StackName": n, "CreationTime": old} for n in names
                    ]
                }
            ]
        }

    if sweep == "cleanup_stale_idp_buckets":
        # No stacks at all, so nothing protects the bucket's run prefix.
        return {
            "list_stacks": [{"StackSummaries": []}],
            "buckets": (
                [{"Name": f"{DEAD_RUN}-inputbucket-abc", "CreationDate": old}]
                if with_target
                else []
            ),
        }

    if sweep == "cleanup_stale_idp_policies":
        policies = (
            [
                {
                    "PolicyName": f"{DEAD_RUN}-PermissionsBoundary",
                    "Arn": f"arn:fake:policy/{DEAD_RUN}-PermissionsBoundary",
                    "CreateDate": old,
                }
            ]
            if with_target
            else []
        )
        return {
            "list_stacks": [{"StackSummaries": []}],
            "list_policies": [{"Policies": policies}],
        }

    if sweep == "cleanup_stale_ci_diagnostics":
        objects = (
            # Keyed by build AND stack, the shape persist_diagnostics_bundle writes.
            [
                {
                    "Key": f"{cbd.DIAGNOSTICS_S3_PREFIX}/build-1/{DEAD_RUN}/evidence.json",
                    "LastModified": old,
                }
            ]
            if with_target
            else []
        )
        return {"list_objects_v2": [{"Contents": objects}]}

    raise AssertionError(f"no account contents written for {sweep}")


#: Per sweep: the enumerations whose failure leaves it with nothing to sweep (one
#: parametrised case each), the per-item call whose failure must stay best
#: effort, and the line its quiet path prints.
#:
#: ⚠️ `enumerations` is the part to get right. The bucket and policy sweeps each
#: make TWO listings, and the protected-prefix listing runs FIRST — without it
#: there is no safe selection to make — so a case that only fails the second
#: covers half the surface. Every registered sweep needs an entry here; one
#: without is a failure, not a skip.
_SWEEP_SPECS = {
    "cleanup_stale_bda_blueprints": {
        "enumerations": ("list_blueprints", "list_data_automation_projects"),
        "per_item": "delete_blueprint",
        "quiet_line": "✅ No stale BDA resources found",
    },
    "cleanup_stale_apigw_test_vpcs": {
        "enumerations": ("list_stacks",),
        "per_item": None,  # delete_apigw_test_vpc returns False; see below
        "quiet_line": "✅ No stale apigw test VPC stacks to reap",
    },
    "cleanup_stale_idp_stacks": {
        "enumerations": ("list_stacks",),
        "per_item": "delete_stack",
        "quiet_line": "✅ No stale idp- test stacks to reap",
    },
    "cleanup_stale_idp_buckets": {
        "enumerations": ("list_stacks", "list_buckets"),
        "per_item": "delete_bucket",
        "quiet_line": "✅ No stale idp- test buckets to reap",
    },
    "cleanup_stale_idp_policies": {
        "enumerations": ("list_stacks", "list_policies"),
        "per_item": None,  # _delete_managed_policy reports (False, detail)
        "quiet_line": "✅ No orphaned idp- IAM managed policies to reap",
    },
    "cleanup_stale_ci_diagnostics": {
        "enumerations": ("list_objects_v2",),
        "per_item": "delete_object",
        "quiet_line": "✅ No expired diagnostics bundles",
    },
}


def _install(cbd, monkeypatch, sweep, *, with_target, failing=None, breaking=None):
    """Point the harness at the fake account and return it."""
    aws = _FakeAws(_contents(cbd, sweep, with_target), failing, breaking)
    monkeypatch.setattr(cbd.boto3, "client", lambda *a, **k: aws)
    monkeypatch.setattr(cbd.boto3, "resource", lambda *a, **k: _FakeS3Resource())
    # The BDA sweep paces its deletes; nothing here is waiting on AWS.
    monkeypatch.setattr(cbd.time, "sleep", lambda *_a, **_k: None)

    # Only the diagnostics sweep reads SOURCE_BUCKET as its target store. For the
    # stack sweep an unset value keeps the retention-marker read out of the way,
    # which is a separate handler and a separate claim.
    if sweep == "cleanup_stale_ci_diagnostics":
        monkeypatch.setenv("SOURCE_BUCKET", "a-bucket")
    else:
        monkeypatch.delenv("SOURCE_BUCKET", raising=False)

    # Two sweeps delegate the per-item action to a module function that already
    # swallows its own failures and reports the outcome in its return value.
    # Patched to the SUCCESS answer here; the best-effort direction drives them
    # to the failure answer in its own test.
    monkeypatch.setattr(cbd, "delete_apigw_test_vpc", lambda _name: True)
    monkeypatch.setattr(cbd, "_delete_managed_policy", lambda _c, _arn: (True, ""))
    return aws


def _run(cbd, sweep):
    return getattr(cbd, sweep)()


def _sweep_names():
    import codebuild_deployment as module

    return [s.func for s in module.STARTUP_SWEEPS]


# ---------------------------------------------------------------------------
# The registry is closed over the module, so nothing can be added uncovered
# ---------------------------------------------------------------------------


def _module_cleanup_functions():
    """Every top-level `cleanup_stale_*` function in the harness, from source.

    Read from the source rather than from the imported module: the question is
    what a reader of the file would call a startup sweep, and that is the set a
    new one joins.
    """
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("cleanup_stale_")
    }


def test_every_cleanup_function_in_the_module_is_registered(cbd):
    """Universe closure, in both directions.

    Without this the parametrisation below is only as complete as whoever last
    edited the registry, and a seventh reaper would be reported on by nothing
    while every test here stayed green — which is the shape of the defect this
    whole change is about, one level up.
    """
    registered = set(_sweep_names())
    discovered = _module_cleanup_functions()

    assert discovered - registered == set(), (
        f"{sorted(discovered - registered)} is a Step 0 reaper the module "
        f"defines and STARTUP_SWEEPS does not list, so nothing decides whether "
        f"its failure to complete fails the run and nothing gives it a table row"
    )
    assert registered - discovered == set(), (
        f"STARTUP_SWEEPS lists {sorted(registered - discovered)}, which the "
        f"module no longer defines — the entry is stale and its row would never "
        f"render"
    )
    for name in registered:
        assert callable(getattr(cbd, name, None)), f"{name} is not callable"


def test_every_registered_sweep_has_driving_data_here():
    """A registered sweep with no `_SWEEP_SPECS` entry fails rather than skips.

    A missing spec would make every parametrised case below silently narrower,
    and a test that quietly stops covering a member is worse than one that was
    never written, because the green mark claims the coverage.
    """
    missing = sorted(set(_sweep_names()) - set(_SWEEP_SPECS))
    assert not missing, f"no driving data in _SWEEP_SPECS for {missing}"


def test_the_fails_run_decision_is_per_member_not_per_class(cbd):
    """Each entry carries its OWN reason, and they are not the same reason.

    The defect class this repository's exemption registry exists to prevent is
    one justification attached to a set where the justification is a property of
    individual members. Six entries sharing one sentence would read as a
    considered decision and be exactly that mistake, so the reasons must differ.
    """
    reasons = [s.why for s in cbd.STARTUP_SWEEPS]
    assert all(len(r) > 80 for r in reasons), "a one-liner is not a reason"
    assert len(set(reasons)) == len(reasons), (
        "two STARTUP_SWEEPS entries carry the same `why`, so one reason is "
        "standing in for two members whose consequences differ"
    )
    # Both verdicts are actually exercised by the registry as it stands. If they
    # ever were not, the pinning below would be half vacuous.
    verdicts = {s.fails_run for s in cbd.STARTUP_SWEEPS}
    assert verdicts == {True, False}


# ---------------------------------------------------------------------------
# Direction 1: an enumeration failure is a finding, not a completed sweep
# ---------------------------------------------------------------------------


def _enumeration_cases():
    return [
        (sweep, api)
        for sweep in _sweep_names()
        for api in _SWEEP_SPECS[sweep]["enumerations"]
    ]


@pytest.mark.parametrize(
    ("sweep", "api"), _enumeration_cases(), ids=lambda v: str(v).replace(" ", "")
)
def test_a_failed_enumeration_is_a_finding(cbd, monkeypatch, capsys, sweep, api):
    aws = _install(cbd, monkeypatch, sweep, with_target=True, failing=api)

    findings = _run(cbd, sweep)

    assert findings, (
        f"{sweep} could not {api} — it considered zero targets and established "
        f"nothing about the account — yet returned no finding, so the run cannot "
        f"tell it apart from a sweep that enumerated and found nothing. That is "
        f"the defect itself, not a cosmetic difference"
    )
    assert all(f.startswith(cbd.SWEEP_NOT_COMPLETED) for f in findings), findings
    assert aws.deleted == [], f"{sweep} deleted something after {api} failed"
    # And it must not ALSO claim the clean outcome, which is the line a reader
    # skims for.
    assert _SWEEP_SPECS[sweep]["quiet_line"] not in capsys.readouterr().out


@pytest.mark.parametrize("sweep", _sweep_names())
def test_a_failed_enumeration_still_does_not_raise(cbd, monkeypatch, sweep):
    """Step 0 never raises, and that contract is load-bearing.

    These run before the deploy, the suite and the teardown, so an exception here
    costs every signal the night would have produced. The failure has to reach
    the verdict through the return value instead.
    """
    api = _SWEEP_SPECS[sweep]["enumerations"][0]
    _install(cbd, monkeypatch, sweep, with_target=True, failing=api)
    assert _run(cbd, sweep)  # must return a finding rather than propagate


# ---------------------------------------------------------------------------
# Direction 2: a clean sweep is still the quiet path
#
# ⚠️ The direction a careless fix breaks, and the one a failure-only test passes
# on. Making every green deploy report a cleanup problem is worse than the
# silence it replaces: it trains the reader to ignore the row that matters, which
# is how the `⚠️` line this fix removes came to be ignored.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sweep", _sweep_names())
def test_an_enumerated_sweep_with_nothing_to_do_reports_no_finding(
    cbd, monkeypatch, capsys, sweep
):
    aws = _install(cbd, monkeypatch, sweep, with_target=False)

    findings = _run(cbd, sweep)

    assert findings == [], (
        f"{sweep} enumerated its targets and found nothing to reap, which is a "
        f"COMPLETED sweep, yet reported {findings}"
    )
    assert aws.deleted == []
    out = capsys.readouterr().out
    assert _SWEEP_SPECS[sweep]["quiet_line"] in out
    assert cbd.SWEEP_NOT_COMPLETED not in out


@pytest.mark.parametrize("sweep", _sweep_names())
def test_a_sweep_that_reaped_everything_it_found_reports_no_finding(
    cbd, monkeypatch, sweep
):
    """The other clean direction: targets found, all taken, nothing to report."""
    aws = _install(cbd, monkeypatch, sweep, with_target=True)

    findings = _run(cbd, sweep)

    assert findings == [], f"{sweep} reaped its target and still reported {findings}"
    if _SWEEP_SPECS[sweep]["per_item"]:
        assert aws.deleted, f"{sweep} reported clean without deleting its target"


# ---------------------------------------------------------------------------
# Direction 3: one stuck resource stays best effort
#
# The asymmetry the fix has to preserve. One resource that will not delete must
# not fail a deploy: the loop names it, the others are still reaped, the next run
# tries again. The only thing separating it from an enumeration failure is which
# `except` catches it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sweep", [s for s in _sweep_names() if _SWEEP_SPECS[s]["per_item"]]
)
def test_a_per_item_failure_stays_best_effort(cbd, monkeypatch, capsys, sweep):
    api = _SWEEP_SPECS[sweep]["per_item"]
    _install(cbd, monkeypatch, sweep, with_target=True, breaking=api)

    findings = _run(cbd, sweep)

    assert findings == [], (
        f"{sweep} turned one failed {api} into a finding, which would fail a "
        f"deploy over a single stuck resource the next run would take"
    )
    out = capsys.readouterr().out
    assert cbd.SWEEP_NOT_COMPLETED not in out
    assert DEAD_RUN in out, "the stuck resource is not named anywhere"


def test_a_stuck_apigw_vpc_stack_stays_best_effort(cbd, monkeypatch, capsys):
    """The VPC sweep's per-item action reports through a return value.

    `delete_apigw_test_vpc` swallows its own failures and answers False, so the
    best-effort direction has to be driven there rather than by raising.
    """
    _install(cbd, monkeypatch, "cleanup_stale_apigw_test_vpcs", with_target=True)
    monkeypatch.setattr(cbd, "delete_apigw_test_vpc", lambda _name: False)

    findings = cbd.cleanup_stale_apigw_test_vpcs()

    assert findings == []
    out = capsys.readouterr().out
    assert "left stuck" in out and DEAD_RUN in out
    assert cbd.SWEEP_NOT_COMPLETED not in out


def test_a_policy_that_will_not_delete_stays_best_effort(cbd, monkeypatch, capsys):
    """Same shape: `_delete_managed_policy` answers `(False, detail)`."""
    _install(cbd, monkeypatch, "cleanup_stale_idp_policies", with_target=True)
    monkeypatch.setattr(cbd, "_delete_managed_policy", lambda _c, _a: (False, "held"))

    findings = cbd.cleanup_stale_idp_policies()

    assert findings == []
    out = capsys.readouterr().out
    assert "NOT deleted" in out and DEAD_RUN in out
    assert cbd.SWEEP_NOT_COMPLETED not in out


# ---------------------------------------------------------------------------
# The two sweeps whose own handlers must NOT become findings
# ---------------------------------------------------------------------------


def test_an_unreadable_retention_marker_is_not_a_sweep_finding(cbd, monkeypatch):
    """The stack sweep's marker read is protection, not enumeration.

    Its failure leaves no stack unconsidered — the sweep still enumerates and
    reaps — it narrows the protection the age gate is applied with, which is a
    different fact, reported where it happens. Folding it in here would report a
    completed sweep as incomplete, and the clean path has to stay quiet.
    """
    _install(cbd, monkeypatch, "cleanup_stale_idp_stacks", with_target=False)
    monkeypatch.setenv("SOURCE_BUCKET", "a-bucket")
    monkeypatch.setattr(
        cbd,
        "_expired_retention_markers",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("AccessDenied: GetObject")
        ),
    )

    findings = cbd.cleanup_stale_idp_stacks()

    assert findings == []


def test_the_diagnostics_sweep_with_no_bucket_reports_neither_way(cbd, monkeypatch):
    """`None`, not `[]`: it did not run, which is not "ran and found nothing".

    With `SOURCE_BUCKET` unset there is no store to enumerate, so there is no
    claim to make in either direction. Returning `[]` would assert a completed
    sweep over a bucket that was never named — the same conflation one level up.
    """
    monkeypatch.delenv("SOURCE_BUCKET", raising=False)
    assert cbd.cleanup_stale_ci_diagnostics() is None


# ---------------------------------------------------------------------------
# The status table has to surface it, per sweep and per decision
# ---------------------------------------------------------------------------


def _summary(cbd, reported):
    primary = {
        "stack_name": DEAD_RUN,
        "success": True,
        "step_results": {"Step 3: Default config": {"status": "passed", "error": ""}},
        "teardown": {"retained": False, "findings": []},
    }
    return cbd.build_consolidated_summary(DEAD_RUN, primary, [], True, reported)


def _row_for(cbd, text, sweep):
    label = next(s.label for s in cbd.STARTUP_SWEEPS if s.func == sweep)
    rows = [line for line in text.splitlines() if label in line]
    assert len(rows) == 1, (label, rows)
    return rows[0].strip()


@pytest.mark.parametrize("sweep", _sweep_names())
def test_the_table_renders_a_finding_and_applies_this_sweeps_decision(cbd, sweep):
    """The row, and whether the run goes red — two separate claims.

    The verdict alone is not enough in either direction: a row ticked `✅` beside
    a finding still flips OVERALL, so the verdict passes a table that ticks a
    sweep it is failing the run for; and a `❌` on a clean sweep flips nothing, so
    the verdict passes a table that reports a problem on every green deploy.
    """
    entry = next(s for s in cbd.STARTUP_SWEEPS if s.func == sweep)
    text = _summary(cbd, {sweep: [f"{cbd.SWEEP_NOT_COMPLETED} could not enumerate"]})

    assert "could not enumerate" in text, "the finding's text is not in the table"
    row = _row_for(cbd, text, sweep)
    if entry.fails_run:
        assert "OVERALL: FAIL" in text, (
            f"{sweep} is registered fails_run=True and a finding left the table "
            f"reading PASS — the inert sweep is now merely a quieter inert sweep"
        )
        assert row.startswith("❌"), row
    else:
        assert "OVERALL: PASS" in text, (
            f"{sweep} is registered fails_run=False and a finding failed the "
            f"table anyway, which is a decision this registry did not take"
        )
        assert row.startswith("⚠️"), row


@pytest.mark.parametrize("sweep", _sweep_names())
def test_the_table_passes_a_completed_sweep_without_a_warning(cbd, sweep):
    text = _summary(cbd, {s: [] for s in _sweep_names()})

    assert "OVERALL: PASS" in text
    assert cbd.SWEEP_NOT_COMPLETED not in text
    assert _row_for(cbd, text, sweep).startswith("✅")


@pytest.mark.parametrize("sweep", _sweep_names())
def test_the_table_does_not_claim_a_sweep_nobody_reported(cbd, sweep):
    """`None` and an absent key both mean "no result", never "clean"."""
    for reported in ({s: None for s in _sweep_names()}, {}):
        text = _summary(cbd, reported)
        row = _row_for(cbd, text, sweep)
        assert "not reported" in row, row
        assert not row.startswith("✅"), row
        assert "enumerated, swept" not in text


# ---------------------------------------------------------------------------
# main() must consult each outcome, and apply each sweep's own decision
#
# A return value nothing reads is the same inert control one level up, and that
# is the recurring defect class here. Asserted over main()'s syntax tree: the
# question is whether the value is bound and then used to decide the run, which a
# substring search cannot answer.
# ---------------------------------------------------------------------------


def _main_function():
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    return main


def _bound_name(main, sweep):
    """The local main() binds `sweep()`'s return value to, or None."""
    for node in ast.walk(main):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == sweep
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            return node.targets[0].id
    return None


def _names_in(node):
    return {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}


def _guards_assigning_false(main, name):
    """`if <name>...:` blocks in main() that set `stack_success = False`."""
    return [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.If)
        and name in _names_in(node.test)
        and any(
            isinstance(stmt, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "stack_success"
                for t in stmt.targets
            )
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is False
            for stmt in ast.walk(node)
        )
    ]


@pytest.mark.parametrize("sweep", _sweep_names())
def test_main_binds_every_sweep_result_instead_of_discarding_it(sweep):
    assert _bound_name(_main_function(), sweep), (
        f"main() calls {sweep}() as a bare statement, so its findings go nowhere "
        f"and the sweep's outcome cannot reach the verdict or the table — the "
        f"shape #1434 removes"
    )


@pytest.mark.parametrize("sweep", _sweep_names())
def test_main_applies_each_sweeps_registered_decision(cbd, sweep):
    """Both directions, because only pinning one leaves the other free.

    A fatal sweep whose outcome never flips the verdict is the original defect
    with a return value added. A non-fatal sweep that flips it anyway is a
    decision the registry did not take, and it would end a night's run red over
    month-old diagnostics objects.
    """
    main = _main_function()
    name = _bound_name(main, sweep)
    assert name
    entry = next(s for s in cbd.STARTUP_SWEEPS if s.func == sweep)
    guarded = _guards_assigning_false(main, name)

    if not entry.fails_run:
        assert not guarded, (
            f"{sweep} is registered fails_run=False — see its `why` — but main() "
            f"fails the run on it anyway"
        )
        return

    assert guarded, (
        f"no `if {name}:` in main() sets stack_success = False, so {sweep} "
        f"considering zero targets still leaves the run green"
    )
    # And it must carry a reason, so the 💥 line and the failure email name the
    # sweep rather than just reporting that something went wrong.
    assert any(
        isinstance(stmt, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "failure_reason" for t in stmt.targets
        )
        for node in guarded
        for stmt in ast.walk(node)
    ), f"the `if {name}:` block in main() sets no failure_reason"


@pytest.mark.parametrize("sweep", _sweep_names())
def test_main_routes_every_sweep_result_into_the_status_table(sweep):
    """Keyed by sweep, and the mapping reaches every table call.

    Stronger than "the name appears in some argument": a mapping that drops one
    key renders that sweep as `not reported` on every run, which is the state the
    table is built to make visible rather than one to arrive at silently.
    """
    main = _main_function()
    name = _bound_name(main, sweep)
    assert name

    mappings = {
        target.id
        for node in ast.walk(main)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
        for key, value in zip(node.value.keys, node.value.values)
        if isinstance(key, ast.Constant)
        and key.value == sweep
        and isinstance(value, ast.Name)
        and value.id == name
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert mappings, (
        f"no dict in main() maps {sweep!r} to {name}, so the status table has no "
        f"way to pair this sweep's outcome with its row"
    )

    calls = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_consolidated_summary"
    ]
    assert calls, "main() builds no consolidated summary"
    uncovered = [
        call
        for call in calls
        if not mappings
        & _names_in(ast.Tuple(elts=[*call.args, *(kw.value for kw in call.keywords)]))
    ]
    assert not uncovered, (
        f"{len(uncovered)} of {len(calls)} build_consolidated_summary() calls in "
        f"main() are not given the sweep mapping, so the in-progress and interim "
        f"tables would claim nothing reported this sweep"
    )


# ---------------------------------------------------------------------------
# The one-line root cause: which sweep it names, and which reason wins
#
# #1433's mutation sample left one survivor here — the precedence of `main()`'s
# one-line `failure_reason` — and recorded it as unpinned, because asserting it
# meant running `main()` end to end. Both halves of it are pinned below instead,
# by naming the two decisions: the sweep a reason is attributed to, and which of
# two reasons is kept. Inverting either mis-sends a reader without changing
# whether the run fails, which is why nothing else notices.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sweep", _sweep_names())
def test_the_one_line_reason_names_the_sweep_it_is_about(cbd, capsys, sweep):
    entry = next(s for s in cbd.STARTUP_SWEEPS if s.func == sweep)
    other_labels = [s.label for s in cbd.STARTUP_SWEEPS if s.func != sweep]

    reason = cbd._report_sweep_not_completed(sweep, ["could not enumerate"])

    assert entry.label in reason, reason
    assert "could not enumerate" in reason, reason
    assert not any(label in reason for label in other_labels), (
        f"the reason for {sweep} names another sweep, so the 💥 line and the "
        f"failure email send a reader after the wrong one: {reason}"
    )
    # Printed unconditionally: `main()` may keep an earlier reason, but a sweep
    # that did not complete has to say so in the log either way.
    assert reason in capsys.readouterr().out


def test_the_first_root_cause_wins_over_a_later_one(cbd):
    """A real deploy failure must not be masked by a Step 0 sweep.

    And the reverse: with nothing else to report, the sweep's reason has to be
    the one that reaches the 💥 line, or the run goes red naming nothing.
    """
    assert cbd._first_failure_reason("publish/build failed: boom", "sweep") == (
        "publish/build failed: boom"
    )
    assert cbd._first_failure_reason("", "sweep did not complete") == (
        "sweep did not complete"
    )


# ---------------------------------------------------------------------------
# The interaction between the stack sweep and the bucket sweep
#
# #1434 asks whether an inert stack sweep makes the bucket sweep's
# protected-prefix set read SMALLER. Measured here, because the direction decides
# whether the bucket sweep's own report can be trusted while the stack sweep is
# broken — and it is the opposite way round. `_live_idp_run_prefixes` protects a
# bucket whose run still has ANY stack, so stacks the stack sweep failed to reap
# keep protecting their buckets: the live set reads LARGER, and the bucket sweep
# reaps FEWER buckets while still truthfully reporting a completed sweep.
#
# The leak is real either way; the attribution is not. It belongs to the stack
# sweep, which is why that one is registered fatal.
# ---------------------------------------------------------------------------


def test_stacks_the_stack_sweep_left_behind_protect_their_buckets(cbd, monkeypatch):
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(seconds=STALE_AGE_SECONDS)
    bucket = f"{DEAD_RUN}-inputbucket-abc"

    def _reap(leftover_stacks):
        aws = _FakeAws(
            {
                "list_stacks": [
                    {
                        "StackSummaries": [
                            {"StackName": n, "CreationTime": old}
                            for n in leftover_stacks
                        ]
                    }
                ],
                "buckets": [{"Name": bucket, "CreationDate": old}],
            }
        )
        monkeypatch.setattr(cbd.boto3, "client", lambda *a, **k: aws)
        monkeypatch.setattr(cbd.boto3, "resource", lambda *a, **k: _FakeS3Resource())
        return cbd.cleanup_stale_idp_buckets(), aws.deleted

    # The stack sweep did its job: the run has no stacks left.
    findings, deleted = _reap([])
    assert findings == [] and deleted == [bucket]

    # The stack sweep was inert: the run's stacks are still there, so the bucket
    # sweep protects them. Fewer buckets reaped, and still a completed sweep —
    # the protected set reads LARGER, not smaller.
    findings, deleted = _reap([DEAD_RUN, f"{DEAD_RUN}-iam"])
    assert findings == [], (
        "the bucket sweep considered every bucket and skipped one by its own "
        "protection rule, which is a completed sweep — reporting a finding here "
        "would blame it for the stack sweep's failure"
    )
    assert deleted == []
