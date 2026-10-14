"""Unit tests for the integration deploy's IAM teardown and its verification.

The defect these pin (issue #1391) is a cleanup that *announced* itself without
happening: the build printed that cleanup was handled by the deployment script
while the `<stack>-iam` pre-stack stayed CREATE_COMPLETE and the
`<stack>-PermissionsBoundary` managed policy survived. Managed policies are a
per-account quota, so the eventual failure lands on an unrelated deploy.

**Every assertion here is about the state of a fake account, never about
whether a cleanup function was called.** A test that asserted the call would
reproduce the defect one level up — "the cleanup ran" is precisely the claim
that was false. So `_FakeIam` and `_FakeCfn` below are small in-memory accounts:
`delete_policy` removes an entry, `delete_stack` removes a stack, and the
verification path reads back through `list_policies` / `describe_stacks`. A
cleanup that deletes nothing leaves the resources in the fake and the test sees
them; one that reports success while the resource is still there is a finding.

They need no AWS: boto3 is monkeypatched at `boto3.client`.
"""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit

ACCOUNT = "111122223333"
STACK = "idp-0710-120000"
BOUNDARY = f"{STACK}-PermissionsBoundary"
BOUNDARY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/{BOUNDARY}"
PASSROLE = f"{STACK}-iam-PassRolePolicy"
PASSROLE_ARN = f"arn:aws:iam::{ACCOUNT}:policy/{PASSROLE}"
SHARED_ARN = f"arn:aws:iam::{ACCOUNT}:policy/idp-cicd-shared-PermissionsBoundary"


class _AwsError(Exception):
    """A stand-in for botocore's ClientError, carrying the same payload shape."""

    def __init__(self, code, message):
        super().__init__(f"An error occurred ({code}) ... : {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return self._pages


class _FakeIam:
    """An in-memory IAM holding customer-managed policies by ARN."""

    def __init__(self, policies=None, *, refuse_delete=(), deny_list=False):
        # {arn: {"PolicyName", "Arn", "CreateDate", "Versions", "Roles"}}
        self.policies = dict(policies or {})
        self.refuse_delete = set(refuse_delete)
        self.deny_list = deny_list
        self.delete_calls = []
        self.version_deletes = []

    # -- reads ---------------------------------------------------------- #
    def get_paginator(self, name):
        assert name == "list_policies"
        if self.deny_list:
            raise _AwsError(
                "AccessDenied", "not authorized to perform iam:ListPolicies"
            )
        return _Paginator([{"Policies": list(self.policies.values())}])

    def list_policy_versions(self, PolicyArn):  # noqa: N803
        policy = self.policies.get(PolicyArn)
        if policy is None:
            raise _AwsError("NoSuchEntity", f"{PolicyArn} not found")
        return {"Versions": policy.get("Versions", [])}

    def list_entities_for_policy(self, PolicyArn):  # noqa: N803
        roles = self.policies.get(PolicyArn, {}).get("Roles", [])
        return {"PolicyRoles": [{"RoleName": r} for r in roles], "PolicyUsers": []}

    # -- writes --------------------------------------------------------- #
    def delete_policy_version(self, PolicyArn, VersionId):  # noqa: N803
        self.version_deletes.append((PolicyArn, VersionId))
        policy = self.policies.get(PolicyArn)
        if policy is None:
            raise _AwsError("NoSuchEntity", f"{PolicyArn} not found")
        policy["Versions"] = [
            v for v in policy.get("Versions", []) if v["VersionId"] != VersionId
        ]

    def delete_policy(self, PolicyArn):  # noqa: N803
        self.delete_calls.append(PolicyArn)
        if PolicyArn not in self.policies:
            raise _AwsError("NoSuchEntity", f"{PolicyArn} not found")
        if PolicyArn in self.refuse_delete:
            # What IAM says while the policy is still a role's boundary. The
            # policy stays in the account, which is the whole point.
            raise _AwsError("DeleteConflict", "policy is attached to entities")
        if any(
            not v.get("IsDefaultVersion")
            for v in self.policies[PolicyArn].get("Versions", [])
        ):
            raise _AwsError("DeleteConflict", "policy has non-default versions")
        del self.policies[PolicyArn]


class _FakeCfn:
    """An in-memory CloudFormation holding top-level stacks by name."""

    def __init__(self, stacks=None, *, delete_raises=None, wait_raises=False):
        # {name: status}
        self.stacks = dict(stacks or {})
        self.delete_raises = delete_raises
        self.wait_raises = wait_raises
        self.delete_calls = []
        self.waits = []

    def describe_stacks(self, StackName):  # noqa: N803
        if StackName not in self.stacks:
            raise _AwsError(
                "ValidationError", f"Stack with id {StackName} does not exist"
            )
        return {
            "Stacks": [{"StackName": StackName, "StackStatus": self.stacks[StackName]}]
        }

    def delete_stack(self, StackName):  # noqa: N803
        # Only *starts* the delete, as CloudFormation does. The waiter below is
        # what completes it -- so a wait that fails leaves the stack standing,
        # which is the state the verification has to notice.
        self.delete_calls.append(StackName)
        if self.delete_raises:
            raise self.delete_raises
        if StackName in self.stacks:
            self.stacks[StackName] = "DELETE_IN_PROGRESS"

    def get_waiter(self, name):
        assert name == "stack_delete_complete"
        outer = self

        class _W:
            def wait(self, StackName, WaiterConfig=None):  # noqa: N803
                outer.waits.append(StackName)
                if outer.wait_raises:
                    # A WaiterError is NOT a ClientError. Carrying no response
                    # payload is the point: this is the exception class that
                    # used to skip the boundary-policy delete entirely.
                    raise RuntimeError(
                        "Waiter StackDeleteComplete failed: max attempts"
                    )
                outer.stacks.pop(StackName, None)

        return _W()


class _FakeSts:
    def get_caller_identity(self):
        return {"Account": ACCOUNT}


def _policy(name, arn, *, age_hours=48, versions=None, roles=()):
    return {
        "PolicyName": name,
        "Arn": arn,
        "CreateDate": datetime.now(tz=timezone.utc) - timedelta(hours=age_hours),
        "Versions": list(versions or [{"VersionId": "v1", "IsDefaultVersion": True}]),
        "Roles": list(roles),
    }


def _install(cbd, monkeypatch, iam, cfn):
    """Point boto3 at the fake account."""

    def _client(name, *args, **kwargs):
        if name == "iam":
            return iam
        if name == "cloudformation":
            return cfn
        if name == "sts":
            return _FakeSts()
        raise AssertionError(f"unexpected boto3 client: {name}")

    monkeypatch.setattr(cbd.boto3, "client", _client)


# --------------------------------------------------------------------------- #
# cleanup_iam_resources: the deletes are issued, and verified against the
# account afterwards.
# --------------------------------------------------------------------------- #
def test_both_resources_are_deleted_and_verified_gone(cbd, monkeypatch):
    iam = _FakeIam({BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN)})
    cfn = _FakeCfn({f"{STACK}-iam": "CREATE_COMPLETE"})
    _install(cbd, monkeypatch, iam, cfn)

    findings = cbd.cleanup_iam_resources(STACK)

    # The EFFECT, read off the fake account — not that a cleanup was called.
    assert f"{STACK}-iam" not in cfn.stacks
    assert BOUNDARY_ARN not in iam.policies
    assert findings == []


def test_a_policy_left_behind_is_reported(cbd, monkeypatch):
    # IAM refuses the delete while the policy is still a role's boundary. The
    # old code printed a warning and the run reported success.
    iam = _FakeIam(
        {BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN, roles=["idp-0710-120000-Role"])},
        refuse_delete={BOUNDARY_ARN},
    )
    cfn = _FakeCfn({f"{STACK}-iam": "CREATE_COMPLETE"})
    _install(cbd, monkeypatch, iam, cfn)

    findings = cbd.cleanup_iam_resources(STACK)

    assert BOUNDARY_ARN in iam.policies  # still there in the fake account
    assert any("LEFT BEHIND" in f and BOUNDARY in f for f in findings), findings
    # The explanatory detail is attached, so the finding is actionable.
    assert any("idp-0710-120000-Role" in f for f in findings), findings


def test_the_prestack_left_behind_is_reported(cbd, monkeypatch):
    iam = _FakeIam({BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN)})
    cfn = _FakeCfn(
        {f"{STACK}-iam": "CREATE_COMPLETE"},
        delete_raises=_AwsError("AccessDenied", "not authorized to DeleteStack"),
    )
    _install(cbd, monkeypatch, iam, cfn)

    findings = cbd.cleanup_iam_resources(STACK)

    assert f"{STACK}-iam" in cfn.stacks
    assert any("LEFT BEHIND" in f and f"{STACK}-iam" in f for f in findings), findings


def test_a_failed_stack_delete_does_not_skip_the_policy_delete(cbd, monkeypatch):
    # The original shape of the leak: the two deletes shared one try block under
    # an `except ClientError`, so a WaiterError from the stack-delete wait
    # skipped the boundary delete and left the policy standing, with "Failed to
    # delete IAM stack" as the only trace.
    iam = _FakeIam({BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN)})
    cfn = _FakeCfn({f"{STACK}-iam": "DELETE_IN_PROGRESS"}, wait_raises=True)
    _install(cbd, monkeypatch, iam, cfn)

    findings = cbd.cleanup_iam_resources(STACK)

    assert BOUNDARY_ARN in iam.delete_calls  # the delete was still issued
    assert BOUNDARY_ARN not in iam.policies  # and it took effect
    # The stack is still mid-delete, so that half is reported.
    assert any(f"{STACK}-iam" in f for f in findings), findings
    assert not any(BOUNDARY in f for f in findings), findings


def test_a_policy_with_extra_versions_is_still_deleted(cbd, monkeypatch):
    # DeletePolicy refuses while non-default versions exist, which is how a
    # CloudFormation-managed policy that outlived its stack usually presents.
    versions = [
        {"VersionId": "v1", "IsDefaultVersion": False},
        {"VersionId": "v2", "IsDefaultVersion": True},
    ]
    iam = _FakeIam({BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN, versions=versions)})
    cfn = _FakeCfn({f"{STACK}-iam": "CREATE_COMPLETE"})
    _install(cbd, monkeypatch, iam, cfn)

    findings = cbd.cleanup_iam_resources(STACK)

    assert (BOUNDARY_ARN, "v1") in iam.version_deletes
    assert BOUNDARY_ARN not in iam.policies
    assert findings == []


def test_an_unexpected_delete_error_does_not_count_as_gone(cbd, monkeypatch):
    # Not NoSuchEntity and not DeleteConflict -- a throttle, a network drop, a
    # policy that bogged the API down. The policy IS still in the account; the
    # verification must notice.
    class _ThrottledIam(_FakeIam):
        def delete_policy(self, PolicyArn):  # noqa: N803
            self.delete_calls.append(PolicyArn)
            raise _AwsError("Throttling", "Rate exceeded")

    iam = _ThrottledIam({BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN)})
    cfn = _FakeCfn({f"{STACK}-iam": "CREATE_COMPLETE"})
    _install(cbd, monkeypatch, iam, cfn)

    findings = cbd.cleanup_iam_resources(STACK)

    assert BOUNDARY_ARN in iam.policies  # delete did not actually succeed
    assert any("LEFT BEHIND" in f and BOUNDARY in f for f in findings), findings


def test_an_unreadable_prestack_is_reported_as_unverified(cbd, monkeypatch):
    # describe_stacks can fail with something that is NOT "does not exist"
    # (AccessDenied, throttle). Reading "stack present" vs "stack absent" off
    # that is a confident wrong answer, so it must land as UNVERIFIED.
    class _Blind(_FakeCfn):
        def describe_stacks(self, StackName):  # noqa: N803
            raise _AwsError("Throttling", "Rate exceeded")

    _install(cbd, monkeypatch, _FakeIam({}), _Blind({f"{STACK}-iam": "CREATE_COMPLETE"}))

    findings = cbd.verify_iam_resources_gone(STACK)

    assert any("UNVERIFIED" in f and f"{STACK}-iam" in f for f in findings), findings


def test_a_missing_policy_is_not_a_finding(cbd, monkeypatch):
    # A probe run uses the shared boundary, so no per-run policy exists. Absent
    # is the success state, not an error.
    iam = _FakeIam(
        {SHARED_ARN: _policy("idp-cicd-shared-PermissionsBoundary", SHARED_ARN)}
    )
    cfn = _FakeCfn({})
    _install(cbd, monkeypatch, iam, cfn)

    findings = cbd.cleanup_iam_resources(STACK)

    assert findings == []
    assert SHARED_ARN in iam.policies  # the shared boundary is never touched


def test_a_cfn_managed_policy_is_reported_not_deleted(cbd, monkeypatch):
    # <stack>-iam-PassRolePolicy belongs to CloudFormation. Deleting it out from
    # under a stack that outlived its delete attempt would trade a leak for
    # drift, so this layer reports it and the orphan reaper takes it later.
    iam = _FakeIam(
        {
            BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN),
            PASSROLE_ARN: _policy(PASSROLE, PASSROLE_ARN),
        }
    )
    cfn = _FakeCfn({f"{STACK}-iam": "CREATE_COMPLETE"})
    _install(cbd, monkeypatch, iam, cfn)

    findings = cbd.cleanup_iam_resources(STACK)

    assert PASSROLE_ARN not in iam.delete_calls
    assert any(PASSROLE in f for f in findings), findings


# --------------------------------------------------------------------------- #
# verify_iam_resources_gone: non-vacuity. A check that cannot read its targets
# has not found them absent.
# --------------------------------------------------------------------------- #
def test_verification_passes_only_on_a_clean_account(cbd, monkeypatch, capsys):
    _install(cbd, monkeypatch, _FakeIam({}), _FakeCfn({}))

    assert cbd.verify_iam_resources_gone(STACK) == []

    # The success message is reserved for an actually clean account. Pinned
    # here because an inverted conditional on this branch (printing it when
    # findings is non-empty) is otherwise behaviourally silent.
    assert "✅ Verified gone" in capsys.readouterr().out


def test_the_success_message_is_withheld_when_the_account_is_not_clean(
    cbd, monkeypatch, capsys
):
    iam = _FakeIam(
        {BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN, roles=["r"])},
        refuse_delete={BOUNDARY_ARN},
    )
    _install(cbd, monkeypatch, iam, _FakeCfn({}))

    assert cbd.verify_iam_resources_gone(STACK), "a leftover policy must produce a finding"
    assert "✅ Verified gone" not in capsys.readouterr().out


def test_verification_reports_a_denied_policy_listing_as_unverified(cbd, monkeypatch):
    # The failure mode #1391's second defect is made of: a cleanup step that
    # cannot enumerate its targets reporting as though it had.
    _install(cbd, monkeypatch, _FakeIam({}, deny_list=True), _FakeCfn({}))

    findings = cbd.verify_iam_resources_gone(STACK)

    assert findings, "a denied listing must not read as 'nothing left behind'"
    assert any("UNVERIFIED" in f for f in findings), findings


def test_verification_reports_an_unreadable_stack_as_unverified(cbd, monkeypatch):
    class _Blind(_FakeCfn):
        def describe_stacks(self, StackName):  # noqa: N803
            raise _AwsError("AccessDenied", "not authorized to DescribeStacks")

    _install(cbd, monkeypatch, _FakeIam({}), _Blind({}))

    findings = cbd.verify_iam_resources_gone(STACK)

    assert any("UNVERIFIED" in f and f"{STACK}-iam" in f for f in findings), findings


def test_verification_ignores_another_runs_resources(cbd, monkeypatch):
    other = f"arn:aws:iam::{ACCOUNT}:policy/idp-0711-090000-PermissionsBoundary"
    iam = _FakeIam({other: _policy("idp-0711-090000-PermissionsBoundary", other)})
    _install(
        cbd, monkeypatch, iam, _FakeCfn({"idp-0711-090000-iam": "CREATE_COMPLETE"})
    )

    assert cbd.verify_iam_resources_gone(STACK) == []


# --------------------------------------------------------------------------- #
# teardown_primary_stack: reached on BOTH the success and the failure path.
# --------------------------------------------------------------------------- #
def _stub_teardown(cbd, monkeypatch, findings=(), retain=False):
    calls = {"cleanup": [], "retain": []}

    def _cleanup(result):
        calls["cleanup"].append(result.get("stack_name"))
        return list(findings)

    def _retain(stack_name):
        calls["retain"].append(stack_name)
        return retain

    monkeypatch.setattr(cbd, "cleanup_stack", _cleanup)
    monkeypatch.setattr(cbd, "retain_failed_stack", _retain)
    return calls


def test_teardown_runs_on_the_success_path(cbd, monkeypatch):
    calls = _stub_teardown(cbd, monkeypatch, retain=True)

    outcome = cbd.teardown_primary_stack({"stack_name": STACK, "success": True}, STACK)

    assert calls["cleanup"] == [STACK]
    # Retention is a failure-path affordance; it must not even be consulted on a
    # run that passed, or a retain-everything setting would leak every run.
    assert calls["retain"] == []
    assert outcome == {"retained": False, "findings": []}


def test_teardown_runs_on_the_failure_path_when_not_retaining(cbd, monkeypatch):
    calls = _stub_teardown(cbd, monkeypatch, retain=False)

    outcome = cbd.teardown_primary_stack({"stack_name": STACK, "success": False}, STACK)

    assert calls["cleanup"] == [STACK]
    assert outcome == {"retained": False, "findings": []}


def test_a_retained_failure_is_reported_as_retained_not_as_verified(cbd, monkeypatch):
    # Retention keeps the pre-stack and boundary on purpose (the retained
    # stack's roles carry that boundary). The distinction that matters is that
    # it must not read as "verified gone".
    calls = _stub_teardown(cbd, monkeypatch, retain=True)

    outcome = cbd.teardown_primary_stack({"stack_name": STACK, "success": False}, STACK)

    assert calls["cleanup"] == []
    assert outcome == {"retained": True, "findings": []}


def test_teardown_carries_the_findings_through(cbd, monkeypatch):
    _stub_teardown(cbd, monkeypatch, findings=["LEFT BEHIND: managed policy x"])

    outcome = cbd.teardown_primary_stack({"stack_name": STACK, "success": True}, STACK)

    assert outcome["findings"] == ["LEFT BEHIND: managed policy x"]


def test_cleanup_stack_reaches_the_iam_cleanup_after_a_failed_stack_delete(
    cbd, monkeypatch
):
    # cleanup_stack's own body used to hold the IAM cleanup inside the try, so
    # anything that raised above it returned through "Cleanup task failed" with
    # the pre-stack and the policy untouched.
    seen = []

    def _boom(*args, **kwargs):
        raise RuntimeError("credentials expired mid-teardown")

    monkeypatch.setattr(cbd, "run_command", _boom)
    monkeypatch.setattr(cbd, "cancel_bedrock_ingestion_jobs", _boom)
    monkeypatch.setattr(cbd, "cleanup_iam_resources", lambda s: seen.append(s) or [])

    findings = cbd.cleanup_stack({"stack_name": STACK})

    assert seen == [STACK]
    assert findings == []


# --------------------------------------------------------------------------- #
# The consolidated summary renders the verification, and a leak fails the run.
# --------------------------------------------------------------------------- #
def _summary(cbd, teardown):
    result = {"stack_name": STACK, "success": True}
    if teardown is not None:
        result["teardown"] = teardown
    return cbd.build_consolidated_summary(STACK, result, [], True)


def test_summary_fails_overall_on_a_teardown_finding(cbd):
    text = _summary(cbd, {"retained": False, "findings": ["LEFT BEHIND: policy x"]})
    assert "OVERALL: FAIL" in text
    assert "LEFT BEHIND: policy x" in text


def test_summary_passes_overall_when_verified(cbd):
    text = _summary(cbd, {"retained": False, "findings": []})
    assert "OVERALL: PASS" in text
    assert "Verified gone" in text


def test_summary_does_not_claim_verified_for_a_retained_stack(cbd):
    text = _summary(cbd, {"retained": True, "findings": []})
    assert "OVERALL: PASS" in text
    assert "Verified gone" not in text
    assert "Retained for inspection" in text


def test_summary_does_not_claim_verified_when_teardown_never_ran(cbd):
    text = _summary(cbd, None)
    assert "Verified gone" not in text
    assert "Not reached" in text


# --------------------------------------------------------------------------- #
# cleanup_stale_idp_policies: the convergence half. A policy whose run is gone
# is reaped; one whose run still has a stack (in-flight, or retained) is not.
# --------------------------------------------------------------------------- #
class _PrefixCfn:
    """Just enough CloudFormation for _live_idp_run_prefixes."""

    def __init__(self, names):
        self._names = list(names)

    def get_paginator(self, name):
        assert name == "list_stacks"
        return _Paginator([{"StackSummaries": [{"StackName": n} for n in self._names]}])


def _install_reaper(cbd, monkeypatch, iam, live_stacks):
    cfn = _PrefixCfn(live_stacks)

    def _client(name, *args, **kwargs):
        return iam if name == "iam" else cfn

    monkeypatch.setattr(cbd.boto3, "client", _client)


def test_policy_reaper_deletes_an_orphan(cbd, monkeypatch):
    iam = _FakeIam({BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN, age_hours=48)})
    _install_reaper(cbd, monkeypatch, iam, [])

    cbd.cleanup_stale_idp_policies()

    assert BOUNDARY_ARN not in iam.policies


def test_policy_reaper_spares_a_run_that_still_has_a_stack(cbd, monkeypatch):
    # Covers an in-flight concurrent run AND a stack deliberately retained for
    # inspection, whose roles still carry this boundary.
    iam = _FakeIam({BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN, age_hours=48)})
    _install_reaper(cbd, monkeypatch, iam, [f"{STACK}-iam"])

    cbd.cleanup_stale_idp_policies()

    assert BOUNDARY_ARN in iam.policies
    assert iam.delete_calls == []


def test_policy_reaper_spares_a_young_policy(cbd, monkeypatch):
    age = cbd.IDP_POLICY_STALE_AGE_SECONDS / 3600 - 1
    iam = _FakeIam({BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN, age_hours=age)})
    _install_reaper(cbd, monkeypatch, iam, [])

    cbd.cleanup_stale_idp_policies()

    assert BOUNDARY_ARN in iam.policies


def test_policy_reaper_spares_the_shared_probe_boundary(cbd, monkeypatch):
    # It is named for no run, so it carries no run prefix and can never match.
    # Concurrent probes depend on it surviving.
    iam = _FakeIam(
        {
            SHARED_ARN: _policy(
                cbd.SHARED_BOUNDARY_POLICY_NAME, SHARED_ARN, age_hours=900
            )
        }
    )
    _install_reaper(cbd, monkeypatch, iam, [])

    cbd.cleanup_stale_idp_policies()

    assert SHARED_ARN in iam.policies
    assert iam.delete_calls == []


def test_policy_reaper_spares_an_unrelated_policy(cbd, monkeypatch):
    arn = f"arn:aws:iam::{ACCOUNT}:policy/LimitedIAMAccess"
    iam = _FakeIam({arn: _policy("LimitedIAMAccess", arn, age_hours=9000)})
    _install_reaper(cbd, monkeypatch, iam, [])

    cbd.cleanup_stale_idp_policies()

    assert arn in iam.policies


def test_policy_reaper_takes_a_leftover_cfn_policy(cbd, monkeypatch):
    # <stack>-iam-PassRolePolicy, left by a pre-stack delete that did not
    # complete. No stack owns it any more, so nothing else can reach it.
    iam = _FakeIam({PASSROLE_ARN: _policy(PASSROLE, PASSROLE_ARN, age_hours=48)})
    _install_reaper(cbd, monkeypatch, iam, [])

    cbd.cleanup_stale_idp_policies()

    assert PASSROLE_ARN not in iam.policies


def test_policy_reaper_reports_a_policy_it_could_not_delete(cbd, monkeypatch, capsys):
    iam = _FakeIam(
        {BOUNDARY_ARN: _policy(BOUNDARY, BOUNDARY_ARN, age_hours=48, roles=["r"])},
        refuse_delete={BOUNDARY_ARN},
    )
    _install_reaper(cbd, monkeypatch, iam, [])

    cbd.cleanup_stale_idp_policies()

    out = capsys.readouterr().out
    assert BOUNDARY_ARN in iam.policies
    assert "NOT deleted" in out
    assert "0/1" in out


def test_policy_reaper_never_raises(cbd, monkeypatch):
    class _Boom:
        def get_paginator(self, name):
            raise _AwsError(
                "AccessDenied", "not authorized to perform iam:ListPolicies"
            )

    _install_reaper(cbd, monkeypatch, _Boom(), [])
    cbd.cleanup_stale_idp_policies()  # must not raise
