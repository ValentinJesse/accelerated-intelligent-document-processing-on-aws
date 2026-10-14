# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The CloudFormation side of the spend controls (GitHub issue #934, items 1 and 5).

Two features, and the reason they share a file is that neither is testable through
any other gate here: ``make cfn-lint`` validates shape, not intent, and nothing
deploys this template in CI.

**Item 5 — a ``MaxValue`` on ``MaxConcurrentWorkflows``.** The hazard is not the
constraint, it is the constraint's *value*. CloudFormation validates parameter
constraints against the value an ``update-stack`` submits, including the previous
value under ``UsePreviousValue``, so a ceiling below a live deployment's current
setting makes that stack's next update unsubmittable. The ceiling is therefore
asserted against values this repository itself uses rather than against a number
written twice, and against a floor that keeps it above the account-level Lambda
concurrency limit — a ceiling that could bind before Lambda does would be a
capacity opinion rather than the typo guard it is meant to be.

**Item 1 — the optional monthly cost budget.** Four properties, each a way the
feature could look installed and do nothing:

* **Default off, and completely off.** With ``MonthlyBudgetUSD`` unset no budget,
  no SNS topic policy and no KMS grant is created. The topic policy matters most:
  creating an ``AWS::SNS::TopicPolicy`` *replaces* the default policy SNS wrote,
  so one created unconditionally would rewrite the alerts topic's access policy on
  every existing deployment.
* **The owner-account statement is restated.** Same reason, from the other side:
  the policy that does get created must carry SNS's own default statement, or
  creating it revokes Subscribe/Publish from this account's own principals —
  taking the admin email subscription and every Lambda publisher with it.
* **Budgets can actually publish.** The alerts topic is encrypted with the
  stack's customer-managed key, so the budgets service principal needs both the
  topic policy and a KMS grant. Without the grant the budget is created, shows
  the right spend in the console, and drops the notification silently — the
  failure the unconditional CloudWatch-Alarms grant in the same key policy
  already exists for.
* **GovCloud.** ``AWS::Budgets::Budget`` does not exist in the ``aws-us-gov``
  partition, and a resource type absent from the region is rejected even behind a
  false ``Condition``, so the ``--govcloud`` transform has to physically remove
  it. The parameter has to go with it: one left behind would accept a value and
  create nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = REPO / "template.yaml"

BUDGET_PARAM = "MonthlyBudgetUSD"
BUDGET_CONDITION = "ShouldCreateMonthlyBudget"
BUDGET_TYPE = "AWS::Budgets::Budget"
TOPIC_POLICY_ID = "AlertsTopicBudgetsPolicy"
KEY_ID = "CustomerManagedEncryptionKey"
CONCURRENCY_PARAM = "MaxConcurrentWorkflows"

# Above this, the ceiling cannot be the first thing a growing deployment hits:
# AWS Lambda's default account-level concurrent-execution limit is 1000, and one
# workflow holds several concurrent invocations, so Lambda binds long before a
# ceiling at or above this does. A ``MaxValue`` below it would be a capacity
# opinion rather than a typo guard, and an opinion is the thing that wedges an
# update.
CEILING_FLOOR = 1000


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags.

    Subclasses ``SafeLoader``, so the ``python/object`` constructors that make
    ``yaml.load`` dangerous are never registered, and the multi-constructor below
    only ever returns plain scalars, lists and dicts.
    """


def _tag_to_python(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> dict:
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _load(path: Path) -> dict:
    loader = _CfnLoader(path.read_text(encoding="utf-8"))
    try:
        return loader.get_single_data() or {}
    finally:
        loader.dispose()


@pytest.fixture(scope="module")
def template() -> dict:
    return _load(MAIN_TEMPLATE)


def _blob(node: Any) -> str:
    """A node as sorted JSON, for substring checks that do not depend on the
    loader's spelling of a short-form tag (``!Condition`` arrives as
    ``Fn::Condition`` here and as ``Condition`` in a packaged template)."""
    return json.dumps(node, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# Item 5: the MaxConcurrentWorkflows ceiling                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_max_concurrent_workflows_has_a_ceiling(template):
    param = template["Parameters"][CONCURRENCY_PARAM]
    assert "MaxValue" in param, (
        f"{CONCURRENCY_PARAM} has no MaxValue, so a slipped digit (100000 for "
        f"100) is accepted and becomes the deployment's spend rate"
    )
    assert param["MinValue"] == 1, "the existing floor must not move"


@pytest.mark.unit
def test_the_shipped_default_is_still_accepted(template):
    """The other direction, and the one that breaks a deployment rather than
    merely failing to guard it. A ceiling below the ``Default`` makes a
    *create*-stack with no parameter overrides unsubmittable."""
    param = template["Parameters"][CONCURRENCY_PARAM]
    default = int(param["Default"])
    assert param["MinValue"] <= default <= param["MaxValue"], (
        f"Default={default} falls outside [{param['MinValue']}, "
        f"{param['MaxValue']}] — every deployment that does not override this "
        f"parameter would be rejected at submit time"
    )


@pytest.mark.unit
def test_no_value_this_repository_uses_exceeds_the_ceiling(template):
    """Derived from the tree, not written down twice.

    A ``MaxValue`` below a value a live stack is running makes that stack's next
    ``update-stack`` fail validation — including when the operator passes nothing
    and CloudFormation reuses the previous value. Nothing in this repository can
    see a customer's deployed value, so the best available proxy is every value
    the repository itself configures, deploys or documents. If a future change
    lowers the ceiling under one of them, that is the signal.
    """
    pattern = re.compile(
        rf"(?:{CONCURRENCY_PARAM}|max_concurrent)[\"']?\s*[:=]\s*[\"']?(\d+)"
    )
    ceiling = int(template["Parameters"][CONCURRENCY_PARAM]["MaxValue"])
    offenders = []
    found_any = False
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in {
            ".py",
            ".yaml",
            ".yml",
            ".json",
            ".md",
            ".sh",
        }:
            continue
        rel = path.relative_to(REPO).as_posix()
        if (
            rel.startswith(("node_modules/", ".git/", "build/"))
            or "/node_modules/" in rel
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for value in pattern.findall(text):
            found_any = True
            if int(value) > ceiling:
                offenders.append(f"{rel}: {value}")
    assert found_any, (
        "the scan found no concurrency value anywhere, so this assertion is "
        "vacuous — the naming convention it matches must have changed"
    )
    assert not offenders, (
        f"MaxValue={ceiling} is below a value this repository already uses, so "
        f"an update-stack on a deployment at that value fails validation: "
        f"{offenders}"
    )


@pytest.mark.unit
def test_the_ceiling_is_a_typo_guard_not_a_capacity_limit(template):
    ceiling = int(template["Parameters"][CONCURRENCY_PARAM]["MaxValue"])
    assert ceiling >= CEILING_FLOOR, (
        f"MaxValue={ceiling} is at or below the account-level Lambda "
        f"concurrency limit, so this constraint could become the binding limit "
        f"on a real workload instead of just rejecting a slipped digit"
    )


# --------------------------------------------------------------------------- #
# Item 1: the optional monthly cost budget                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_budget_parameter_is_optional_and_numeric(template):
    param = template["Parameters"][BUDGET_PARAM]
    assert param["Type"] == "String", (
        "a String with an empty default is how every other optional parameter "
        "here spells 'unset'; a Number cannot distinguish unset from zero"
    )
    assert param["Default"] == "", "the budget must be off by default"

    allowed = re.compile(param["AllowedPattern"])
    for ok in ("", "500", "500.00", "0", "1.5", "999999999"):
        assert allowed.fullmatch(ok), f"{ok!r} should be an accepted budget"
    for bad in ("abc", "-5", "1e6", "500.123", "$500", "1,000", " 500"):
        assert not allowed.fullmatch(bad), f"{bad!r} should be rejected"


@pytest.mark.unit
def test_the_budget_has_no_maxvalue_of_its_own(template):
    """Deliberate asymmetry with ``MaxConcurrentWorkflows``, and the reason is the
    hazard that parameter's ceiling was chosen around. A ceiling on a *budget*
    guards nothing — an absurdly large budget simply never alarms — while every
    ceiling is a future ``update-stack`` validation failure for whoever sits above
    it. Cost with no benefit, so there is none. The ``AllowedPattern`` already
    bounds the shape to at most nine digits and two decimals."""
    param = template["Parameters"][BUDGET_PARAM]
    assert "MaxValue" not in param and "MinValue" not in param


@pytest.mark.unit
def test_the_budget_exists_only_when_the_parameter_is_set(template):
    budgets = {
        name: res
        for name, res in template["Resources"].items()
        if res.get("Type") == BUDGET_TYPE
    }
    assert len(budgets) == 1, f"expected exactly one {BUDGET_TYPE}, got {list(budgets)}"
    name, res = next(iter(budgets.items()))
    assert res.get("Condition") == BUDGET_CONDITION, (
        f"{name} has Condition {res.get('Condition')!r}; an unconditional budget "
        f"would be created on every existing deployment"
    )


@pytest.mark.unit
def test_the_condition_gates_on_both_the_parameter_and_the_partition(template):
    cond = template["Conditions"][BUDGET_CONDITION]
    blob = _blob(cond)
    assert "Fn::And" in blob, f"{BUDGET_CONDITION} must be a conjunction: {blob}"
    assert BUDGET_PARAM in blob, (
        f"{BUDGET_CONDITION} does not read {BUDGET_PARAM}, so the feature is not "
        f"opt-in: {blob}"
    )
    assert "IsGovCloudPartition" in blob, (
        f"{BUDGET_CONDITION} has no GovCloud leg, so a GovCloud deployment would "
        f"try to create a resource type that partition does not have: {blob}"
    )


@pytest.mark.unit
def test_nothing_budget_related_is_unconditional(template):
    """Every resource that mentions the budget parameter or condition carries the
    condition. This is what makes "unset parameter yields the template that
    deploys today" true of the whole feature rather than only of the budget: the
    SNS topic policy is the dangerous one, because creating it replaces the
    default policy SNS wrote for the alerts topic."""
    offenders = []
    for name, res in template["Resources"].items():
        if not isinstance(res, dict):
            continue
        if res.get("Type") == BUDGET_TYPE or name == TOPIC_POLICY_ID:
            if res.get("Condition") != BUDGET_CONDITION:
                offenders.append(name)
    assert not offenders, (
        f"budget resources missing Condition {BUDGET_CONDITION}: {offenders}"
    )

    # The KMS grant is a statement inside a shared key policy rather than its own
    # resource, so it has to be gated with an Fn::If on the same condition.
    key_policy = _blob(template["Resources"][KEY_ID]["Properties"]["KeyPolicy"])
    assert "budgets." in key_policy, "the budgets KMS grant is missing entirely"
    statements = template["Resources"][KEY_ID]["Properties"]["KeyPolicy"]["Statement"]
    gated = [
        s
        for s in statements
        if isinstance(s, dict) and "Fn::If" in s and s["Fn::If"][0] == BUDGET_CONDITION
    ]
    assert len(gated) == 1, (
        f"expected exactly one key-policy statement behind "
        f"Fn::If[{BUDGET_CONDITION}], found {len(gated)}"
    )
    assert "budgets." in _blob(gated[0]["Fn::If"][1])


@pytest.mark.unit
def test_the_budget_notifies_the_alerts_topic_at_80_and_100_percent(template):
    res = next(
        r for r in template["Resources"].values() if r.get("Type") == BUDGET_TYPE
    )
    budget = res["Properties"]["Budget"]
    assert budget["BudgetType"] == "COST"
    assert budget["TimeUnit"] == "MONTHLY"
    assert budget["BudgetLimit"]["Unit"] == "USD"
    assert _blob(budget["BudgetLimit"]["Amount"]).find(BUDGET_PARAM) != -1, (
        "the budget limit must come from the parameter"
    )

    notifications = res["Properties"]["NotificationsWithSubscribers"]
    thresholds = {n["Notification"]["Threshold"] for n in notifications}
    assert thresholds == {80, 100}, f"expected 80% and 100%, got {thresholds}"
    for n in notifications:
        notif = n["Notification"]
        assert notif["NotificationType"] == "ACTUAL", (
            "FORECASTED on an upload-driven workload swings wildly early in a "
            "month; the first requirement of a spend alert is believability"
        )
        assert notif["ThresholdType"] == "PERCENTAGE"
        assert notif["ComparisonOperator"] == "GREATER_THAN", (
            "AWS Budgets offers only GREATER_THAN / LESS_THAN / EQUAL_TO"
        )
        assert [s["SubscriptionType"] for s in n["Subscribers"]] == ["SNS"]
        assert "AlertsTopic" in _blob(n["Subscribers"][0]["Address"])


@pytest.mark.unit
def test_the_budget_waits_for_the_topic_policy(template):
    """``CreateBudget`` validates that it can reach its SNS subscriber, so the
    topic policy has to exist first. Without ``DependsOn`` the budget's creation
    depends on resource ordering CloudFormation does not promise."""
    res = next(
        r for r in template["Resources"].values() if r.get("Type") == BUDGET_TYPE
    )
    depends = res.get("DependsOn")
    depends = [depends] if isinstance(depends, str) else (depends or [])
    assert TOPIC_POLICY_ID in depends, (
        f"the budget must DependsOn {TOPIC_POLICY_ID}; got {depends}"
    )


@pytest.mark.unit
def test_the_topic_policy_admits_the_budgets_principal(template):
    policy = template["Resources"][TOPIC_POLICY_ID]
    assert policy["Type"] == "AWS::SNS::TopicPolicy"
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    budget_stmts = [s for s in statements if "budgets." in _blob(s.get("Principal"))]
    assert len(budget_stmts) == 1, (
        "exactly one statement should name the budgets service principal; "
        "without it CreateBudget fails with 'Invalid SNS topic'"
    )
    stmt = budget_stmts[0]
    assert stmt["Effect"] == "Allow"
    actions = stmt["Action"]
    actions = [actions] if isinstance(actions, str) else actions
    assert [a.lower() for a in actions] == ["sns:publish"], (
        f"the budgets grant must be publish-only, got {actions}"
    )
    # Bounded to this account's own budgets, as AWS documents for this grant.
    conditions = _blob(stmt.get("Condition"))
    assert "aws:SourceAccount" in conditions and "aws:SourceArn" in conditions


@pytest.mark.unit
def test_the_topic_policy_restates_the_sns_default_statement(template):
    """Creating an ``AWS::SNS::TopicPolicy`` REPLACES the policy SNS wrote, it
    does not add to it. A policy carrying only the budgets grant therefore
    revokes Subscribe/Publish/SetTopicAttributes from this account's own
    principals — which silently takes out the admin email subscription (#922) and
    every Lambda that publishes to the alerts topic. That is a one-line omission
    that a review reads as tightening the policy.
    """
    statements = template["Resources"][TOPIC_POLICY_ID]["Properties"]["PolicyDocument"][
        "Statement"
    ]
    owner = [
        s
        for s in statements
        if "SourceOwner" in _blob(s.get("Condition"))
        and "budgets." not in _blob(s.get("Principal"))
    ]
    assert len(owner) == 1, (
        "the owner-account default statement is missing from the replacement policy"
    )
    actions = {a.lower() for a in owner[0]["Action"]}
    for required in (
        "sns:publish",
        "sns:subscribe",
        "sns:settopicattributes",
        "sns:gettopicattributes",
        "sns:listsubscriptionsbytopic",
        "sns:addpermission",
        "sns:removepermission",
        "sns:deletetopic",
    ):
        assert required in actions, (
            f"{required} is in the SNS default policy and missing here, so "
            f"creating this policy revokes it from this account"
        )


@pytest.mark.unit
def test_the_topic_policy_denies_non_tls_access(template):
    """The transport control every other resource policy in this template carries.

    ``scripts/tests/test_secure_transport_deny.py`` deliberately left
    ``AWS::SNS::TopicPolicy`` unjudged, with its ``AWS::SNS::Topic`` exemption
    naming the alerts work as "the likely occasion" on which somebody would add
    the first one and have to decide. This is that occasion; the decision is that
    the deny applies, and this pins it. Nothing publishing here uses plain HTTP —
    the budgets and cloudwatch service principals call SNS over TLS, as does every
    SDK publisher in the stack — so the deny costs nothing and closes the one
    thing a replacement topic policy could otherwise silently permit.

    ⚠️ The classification in that gate's own register is **not** updated by this
    change, so two of its assertions fail until somebody moves
    ``AWS::SNS::TopicPolicy`` into ``POLICY_FOR_RESOURCE`` and corrects the
    now-false ``AWS::SNS::Topic`` reason. That is recorded in the pull request
    rather than worked around here.
    """
    statements = template["Resources"][TOPIC_POLICY_ID]["Properties"]["PolicyDocument"][
        "Statement"
    ]
    denies = [
        s
        for s in statements
        if s.get("Effect") == "Deny" and "SecureTransport" in _blob(s.get("Condition"))
    ]
    assert len(denies) == 1, (
        "the replacement topic policy must carry the aws:SecureTransport deny"
    )
    stmt = denies[0]
    assert stmt["Principal"] == "*"
    assert stmt["Action"] == "SNS:*", (
        "the deny has to cover every action, not just publish — a narrower one "
        "leaves Subscribe and SetTopicAttributes reachable without TLS"
    )
    assert _blob(stmt["Condition"]) == _blob({"Bool": {"aws:SecureTransport": False}})


@pytest.mark.unit
def test_the_kms_grant_carries_no_condition_key(template):
    """The grant that lets AWS Budgets encrypt a message for the alerts topic
    must not be narrowed by a condition key nobody has verified is populated.

    An IAM condition on a context key the request does not carry evaluates to
    DENY, which here means the budget is created, reads the right spend, and
    drops the notification with nothing anywhere saying so — the exact silent
    failure this statement exists to prevent. The SNS topic policy's conditions
    are a different case: AWS documents those verbatim for this grant. The
    sibling CloudWatch-Alarms statement in the same key policy carries none
    either, for the same reason.
    """
    statements = template["Resources"][KEY_ID]["Properties"]["KeyPolicy"]["Statement"]
    gated = next(
        s["Fn::If"][1]
        for s in statements
        if isinstance(s, dict) and "Fn::If" in s and s["Fn::If"][0] == BUDGET_CONDITION
    )
    assert "Condition" not in gated, (
        f"the budgets KMS grant gained a condition block: {gated.get('Condition')}"
    )
    actions = set(gated["Action"])
    assert actions == {"kms:Decrypt", "kms:GenerateDataKey*"}, (
        f"an SSE-SNS publisher needs exactly these two; got {actions}"
    )


@pytest.mark.unit
def test_service_principals_are_partition_portable(template):
    """``make check-arn-partitions`` reads ARNs. A service principal is a
    hostname, so it does not, and a literal ``budgets.amazonaws.com`` would be
    invisible to that gate."""
    blobs = [
        _blob(template["Resources"][TOPIC_POLICY_ID]),
        _blob(template["Resources"][KEY_ID]["Properties"]["KeyPolicy"]),
    ]
    for blob in blobs:
        assert "budgets." in blob
        assert "budgets.amazonaws.com" not in blob, (
            "use budgets.${AWS::URLSuffix}, not a hardcoded hostname"
        )


@pytest.mark.unit
def test_the_parameter_is_listed_in_the_console_interface(template):
    groups = template["Metadata"]["AWS::CloudFormation::Interface"]["ParameterGroups"]
    listed = {p for g in groups for p in g.get("Parameters", [])}
    assert BUDGET_PARAM in listed, (
        "a parameter absent from every ParameterGroup is still accepted but is "
        "shown in an unlabelled 'Other parameters' bucket in the console"
    )


# --------------------------------------------------------------------------- #
# Item 1: GovCloud                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_govcloud_transform_strips_the_budget_entirely():
    """The real transform over the real template.

    ``AWS::Budgets::Budget`` is reported ``isNotAvailableIn`` for both GovCloud
    regions — GovCloud usage is billed through the linked standard account — and
    a resource type absent from the region is rejected even behind a false
    ``Condition``, which is why this repository physically deletes the CloudFront
    resources rather than conditioning them. So the budget has to go, and so does
    the parameter: one left behind would be accepted by a GovCloud deploy and
    create nothing, which reads as a configured budget and is not one.
    """
    from idp_sdk._core.template_transform import GovCloudTemplateTransformer

    transformer = GovCloudTemplateTransformer()
    result = transformer.apply_transforms(_load(MAIN_TEMPLATE))

    types = {r.get("Type") for r in result["Resources"].values() if isinstance(r, dict)}
    assert BUDGET_TYPE not in types, f"{BUDGET_TYPE} survived the GovCloud transform"
    assert TOPIC_POLICY_ID not in result["Resources"], (
        "the budgets-only topic policy survived, so the GovCloud template would "
        "replace the alerts topic's default access policy with a grant to a "
        "service that is not in the partition"
    )
    assert BUDGET_PARAM not in result.get("Parameters", {})
    assert BUDGET_CONDITION not in result.get("Conditions", {})

    # No dangling reference to either, anywhere — a Ref to a removed parameter or
    # an Fn::If on a removed condition is a template-validation failure, not a
    # silent no-op.
    blob = _blob(result)
    assert BUDGET_CONDITION not in blob, (
        f"{BUDGET_CONDITION} is still referenced after removal"
    )
    assert BUDGET_PARAM not in blob, f"{BUDGET_PARAM} is still referenced after removal"
    assert "budgets." not in blob, "a grant to the budgets principal survived"

    assert transformer.validate_no_cloudfront(result), (
        "the transform's own post-conditions failed"
    )


@pytest.mark.unit
def test_the_headless_transform_strips_the_budget_too():
    """``--headless`` is not obviously a GovCloud concern, and it is one.

    The headless template is the API-only variant used for **GovCloud batch
    deployments**, so the repository lints it against a GovCloud region as well
    (``lib/idp_sdk/tests/unit/test_template_transform.py::
    test_real_template_headless_passes_govcloud_region_cfn_lint``) — and that lint
    is where the first version of this change failed, which is the empirical
    confirmation that cfn-lint E3006 fires on this resource type for
    ``us-gov-west-1`` regardless of its ``Condition``.

    It is also the right answer on the merits rather than only a way to satisfy a
    gate: the headless transform removes ``AdminEmail``, so a headless deployment
    subscribes nothing to the alerts topic the budget would publish to. A budget
    whose only delivery path has no subscriber is the shape of control this
    repository keeps finding — installed, and silent.
    """
    from idp_sdk._core.template_transform import HeadlessTemplateTransformer

    result = HeadlessTemplateTransformer().apply_transforms(_load(MAIN_TEMPLATE))

    types = {r.get("Type") for r in result["Resources"].values() if isinstance(r, dict)}
    assert BUDGET_TYPE not in types
    assert TOPIC_POLICY_ID not in result["Resources"]
    assert BUDGET_PARAM not in result.get("Parameters", {})
    assert BUDGET_CONDITION not in result.get("Conditions", {})
    blob = _blob(result)
    assert BUDGET_PARAM not in blob and BUDGET_CONDITION not in blob, (
        "a dangling Ref to a removed parameter, or an Fn::If on a removed "
        "condition, is rejected by CloudFormation up front — the whole template, "
        "not just the dead reference"
    )

    # The console interface must not advertise a parameter that is gone.
    groups = (
        result.get("Metadata", {})
        .get("AWS::CloudFormation::Interface", {})
        .get("ParameterGroups", [])
    )
    listed = {p for g in groups for p in g.get("Parameters", [])}
    assert BUDGET_PARAM not in listed


@pytest.mark.unit
def test_the_budget_type_is_registered_as_govcloud_unsupported():
    """The transform's validator and its removal step must agree. The validator
    is what fails the publish if a GovCloud-unsupported type survives; a type
    removed but not registered would pass vacuously, and a type registered but
    not removed fails the publish — both are better than silence, and agreement
    is better than either."""
    from idp_sdk._core.template_transform import GovCloudTemplateTransformer

    assert BUDGET_TYPE in GovCloudTemplateTransformer.GOVCLOUD_UNSUPPORTED_TYPES
    assert BUDGET_TYPE in GovCloudTemplateTransformer.BUDGET_RESOURCE_TYPES
    assert BUDGET_PARAM in GovCloudTemplateTransformer.BUDGET_PARAMETERS
    assert BUDGET_CONDITION in GovCloudTemplateTransformer.BUDGET_CONDITIONS


@pytest.mark.unit
@pytest.mark.parametrize(
    "transformer_name", ["GovCloudTemplateTransformer", "HeadlessTemplateTransformer"]
)
def test_a_budget_with_no_condition_is_still_removed(transformer_name):
    """The removal's **type** arm, exercised on its own.

    ``_remove_budget_resources`` removes a resource if its type is a budget type
    *or* its condition is the budget condition, and on today's template both
    budget resources carry the condition — so the type arm is redundant and the
    two assertions above pass with it deleted. A mutation run found exactly that:
    breaking the type check left every test green.

    The type arm is the leg that matters for the next budget resource somebody
    adds, which may well be unconditional (an ``AWS::Budgets::BudgetsAction``, a
    second budget). Left unproven it is decoration, and the failure it would
    hide is a GovCloud publish that fails cfn-lint E3006 for a reason nobody
    changed. So it gets a synthetic template with a budget and no ``Condition``
    at all.
    """
    import importlib

    module = importlib.import_module("idp_sdk._core.template_transform")
    transformer = getattr(module, transformer_name)()

    synthetic = {
        "Parameters": {"Unrelated": {"Type": "String", "Default": ""}},
        "Conditions": {"Unrelated": {"Fn::Equals": [{"Ref": "Unrelated"}, "x"]}},
        "Resources": {
            "UnconditionalBudget": {
                "Type": BUDGET_TYPE,
                "Properties": {"Budget": {"BudgetType": "COST"}},
            },
            "KeepMe": {
                "Type": "AWS::SNS::Topic",
                "Condition": "Unrelated",
                "Properties": {},
            },
        },
    }
    result = transformer._remove_budget_resources(synthetic)

    assert "UnconditionalBudget" not in result["Resources"], (
        "a budget carrying no Condition was not removed, so the removal relies "
        "entirely on the condition arm"
    )
    assert "KeepMe" in result["Resources"], (
        "the removal took a resource that is neither a budget type nor "
        "budget-conditioned"
    )
    assert "Unrelated" in result["Conditions"]
    assert "Unrelated" in result["Parameters"]


@pytest.mark.unit
def test_the_transform_leaves_an_unrelated_conditional_resource_alone():
    """The removal walks resources by type *and* by condition, which is a wide
    enough instrument to take something it should not. A resource carrying a
    different condition must survive."""
    from idp_sdk._core.template_transform import GovCloudTemplateTransformer

    before = _load(MAIN_TEMPLATE)
    keep = {
        name
        for name, res in before["Resources"].items()
        if isinstance(res, dict)
        and res.get("Condition")
        and res.get("Condition") != BUDGET_CONDITION
        and res.get("Type") != BUDGET_TYPE
    }
    assert keep, "no other conditional resource to compare against"

    result = GovCloudTemplateTransformer().apply_transforms(_load(MAIN_TEMPLATE))
    # The CloudFront and LWA strips legitimately remove some of these, so compare
    # against what those steps are declared to take rather than against nothing.
    known_removals = GovCloudTemplateTransformer.CLOUDFRONT_RESOURCES | set(
        GovCloudTemplateTransformer.CLOUDFRONT_CONDITIONS
    )
    lost = {
        name
        for name in keep
        if name not in result["Resources"]
        and name not in known_removals
        and before["Resources"][name].get("Condition")
        not in GovCloudTemplateTransformer.CLOUDFRONT_CONDITIONS
    }
    # Anything else removed must have been an AWS::Lambda::Url, an LWA-dependent
    # function or an InvokeFunctionUrl permission.
    unexplained = {
        name
        for name in lost
        if before["Resources"][name].get("Type")
        not in {
            "AWS::Lambda::Url",
            "AWS::Lambda::Permission",
            "AWS::Serverless::Function",
        }
    }
    assert not unexplained, (
        f"the budget strip took unrelated conditional resources: {unexplained}"
    )


# --------------------------------------------------------------------------- #
# Item 3 is NOT built                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_no_spend_enforcement_path_exists(template):
    """Item 3 of #934 — the circuit breaker wired to a spend alarm — is an open
    product decision, and the decision recorded on the issue is that the metric
    is delivered first and that enforcement, when it comes, throttles rather than
    halts. So this change must not have created one by accident: no alarm may
    watch a cost metric, and nothing cost-shaped may reach the breaker topic or
    the concurrency parameter.
    """
    cost_metric_names = {"DocumentEstimatedCost", "UnpricedMeteringRows"}
    offenders = []
    for name, res in template["Resources"].items():
        if not isinstance(res, dict) or res.get("Type") != "AWS::CloudWatch::Alarm":
            continue
        blob = _blob(res)
        if any(m in blob for m in cost_metric_names):
            offenders.append(name)
    assert not offenders, (
        f"alarms watching the cost metric: {offenders}. Item 3 of #934 is not "
        f"authorized — the metric goes to the maintainer before anything acts on "
        f"it."
    )
    # And nothing in the budget's own wiring reaches an enforcement surface.
    budget_blob = _blob(
        [
            r
            for n, r in template["Resources"].items()
            if r.get("Type") == BUDGET_TYPE or n == TOPIC_POLICY_ID
        ]
    )
    for token in ("CircuitBreaker", CONCURRENCY_PARAM, "ConcurrencyTable"):
        assert token not in budget_blob, (
            f"{token} appears in the budget wiring; the budget notifies and must "
            f"not act"
        )
