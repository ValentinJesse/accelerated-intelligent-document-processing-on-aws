# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Bedrock pricing entry that omits a cache unit records why, per unit.

The owning test for ``idp_common.metering_units.EXPECTED_CACHE_UNIT_OMISSIONS``.
``config_library/pricing.yaml`` omits ``cacheReadInputTokens`` and
``cacheWriteInputTokens`` on eleven ``bedrock/`` entries, and an omitted unit was
priced at exactly $0.00 by both cost implementations on one service-wide
justification written for all eleven models at once — two of which recorded a
basis and nine of which recorded none. That is the defect shape this repository
keeps re-learning: one justification attached to a set, where the justification
is a property of individual members. Issue
[#1206](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1206).

**The universe is derived from ``pricing.yaml``, not from a list written here**,
which is the whole point: the eleven entries are today's instance and a twelfth
model added next month is the class. Three things follow and each is a test
below.

* Every ``(bedrock/<id>, cache unit)`` omission in the shipped file must be
  registered. A new row omitting a rate fails here.
* Every registered member must still be an omission in the shipped file. A rate
  added, or a row deleted, fails here — so a reason cannot outlive what it
  described and pre-approve whatever next occupies the key.
* The derived universe must be **non-empty**, and the bound is deliberately a
  floor on the number of ``bedrock/`` entries read rather than on the number
  omitting: an empty derived list makes every parametrised assertion collect as
  a *skip* rather than a failure, which reads green and proves nothing.

The per-member premise is checked rather than read, on both of its halves:

* the **price-list** half, against a committed digest of AWS's two public offer
  files (``bedrock_cache_dimension_digest.py``, refreshable with no credential).
  A member claiming AWS prices the dimension must name a model the digest shows a
  non-zero rate for, and a member claiming otherwise must name one it does not.
* the **reachability** half, against the two code constants that decide whether a
  cache token can be metered for a model at all —
  ``bedrock.client.CACHEPOINT_SUPPORTED_MODELS`` and
  ``bedrock.prompt_cache._IMPLICIT_CACHE_BASE_NAMES``. This is the half that can
  go stale in a single commit, and the whole register rests on it.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import yaml

from idp_common.metering_units import (
    CACHE_DIMENSION_READING_DATE,
    EXPECTED_CACHE_UNIT_OMISSIONS,
    NOT_CHARGEABLE,
    UNKNOWN,
    CacheUnitOmission,
    classify_absent_unit,
)

from .bedrock_cache_dimension_digest import (
    digest_offer_files,
    load_digest,
    published_cache_rates,
)

#: The two units this register is about. Not every token unit: a Bedrock entry
#: omitting ``inputTokens`` is a different and simpler failure, already refused by
#: ``test_pricing_lookup.py``, and folding it in here would blur what a member of
#: this register means.
CACHE_UNITS = ("cacheReadInputTokens", "cacheWriteInputTokens")

#: The pricing-key prefix this register covers. ``textract/`` and ``lambda_hook/``
#: entries never carry a cache unit and never meter one against a rate, so
#: requiring a basis from them would be an exemption surface with nothing behind
#: it.
BEDROCK_PREFIX = "bedrock/"

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..")
)
_PRICING_PATH = os.path.join(_REPO_ROOT, "config_library", "pricing.yaml")


def _shipped_bedrock_units() -> dict[str, set[str]]:
    """``{pricing key: {unit names}}`` for every ``bedrock/`` entry shipped.

    Read from the YAML rather than from a loaded configuration object so the
    answer is about the file a deployment installs, which is what both cost
    implementations ultimately resolve against.
    """
    with open(_PRICING_PATH) as handle:
        raw = yaml.safe_load(handle)
    return {
        entry["name"]: {unit["name"] for unit in (entry.get("units") or [])}
        for entry in raw["pricing"]
        if entry["name"].startswith(BEDROCK_PREFIX)
    }


def _omissions() -> set[tuple[str, str]]:
    """Every ``(pricing key, cache unit)`` the shipped file leaves unpriced."""
    return {
        (key, unit)
        for key, units in _shipped_bedrock_units().items()
        for unit in CACHE_UNITS
        if unit not in units
    }


def _registered() -> dict[tuple[str, str], CacheUnitOmission]:
    return {
        (entry.pricing_key, entry.unit): entry
        for entry in EXPECTED_CACHE_UNIT_OMISSIONS
    }


def _model_id(pricing_key: str) -> str:
    """``us.amazon.nova-pro-v1:0`` from ``bedrock/us.amazon.nova-pro-v1:0``."""
    return pricing_key[len(BEDROCK_PREFIX) :]


# --------------------------------------------------------------------------- #
# Guard the guard                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_derived_universe_is_not_empty():
    """A parametrised suite over an empty list collects as skips, not failures.

    So the floor is on the number of ``bedrock/`` entries the derivation reads,
    not on the number that omit a unit — the latter legitimately falls to zero as
    rates are added, and pinning it would turn paying the debt down into a red
    gate. What must never happen is the YAML walk finding nothing (a moved file, a
    renamed top-level key, a prefix change), because then every assertion below
    holds over no members.
    """
    entries = _shipped_bedrock_units()
    assert len(entries) >= 50, (
        f"only {len(entries)} 'bedrock/' pricing entries found in "
        f"{_PRICING_PATH}; the derivation is broken and every assertion in this "
        "module would pass over an empty universe"
    )
    # One known key, so a walk that returns 50 rows of something else still fails.
    assert "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0" in entries
    # And the unit names have to be read, not just the keys.
    assert (
        "cacheReadInputTokens"
        in entries["bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"]
    )


@pytest.mark.unit
def test_the_register_is_not_empty_and_is_keyed_per_entry_and_unit():
    """Two members of the same model, one per unit, is the shape to preserve.

    A register keyed on the pricing entry alone would be one reason covering both
    units, which is the aggregation the issue is about one level down: Nova Pro's
    cache WRITE is published by AWS at exactly $0.00 while its cache READ is
    priced at $0.24/MTok, so a single reason for the pair is false for one of
    them.
    """
    register = _registered()
    assert len(register) == len(EXPECTED_CACHE_UNIT_OMISSIONS), (
        "two members share a (pricing_key, unit) key; one of them is shadowed and "
        "its basis is never read"
    )
    assert len(register) >= 10, f"the register holds only {len(register)} members"
    nova_pro = "bedrock/amazon.nova-pro-v1:0"
    assert register[(nova_pro, "cacheReadInputTokens")].priced_by_aws is True
    assert register[(nova_pro, "cacheWriteInputTokens")].priced_by_aws is False


# --------------------------------------------------------------------------- #
# Universe closure, in both directions                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_every_omission_in_the_shipped_file_is_registered():
    """The class, not the eleven instances: a new row omitting a rate fails here."""
    unregistered = sorted(_omissions() - set(_registered()))
    assert not unregistered, (
        "shipped bedrock pricing entries omit a cache unit with no recorded "
        f"basis: {unregistered}. Either add the rate to "
        "config_library/pricing.yaml, or add an entry to "
        "idp_common.metering_units.EXPECTED_CACHE_UNIT_OMISSIONS naming the "
        "price-list model you read and what it showed. Until one of those "
        "happens the unit is reported as an unpriced NULL cost rather than "
        "silently billed at $0.00 — which is the intended behaviour, not a "
        "second bug."
    )


@pytest.mark.unit
def test_every_registered_member_is_still_an_omission():
    """A reason that outlives what it described pre-approves its successor.

    Because the key is ``(pricing key, unit)`` with no rate in it, a member whose
    entry has since gained a rate does not go inert: it keeps shielding that unit
    on that key, so a later edit removing the rate again would be silent.
    """
    omissions = _omissions()
    shipped = _shipped_bedrock_units()
    stale = []
    for key, unit in sorted(_registered()):
        if key not in shipped:
            stale.append(f"{key}:{unit} (no such pricing entry)")
        elif (key, unit) not in omissions:
            stale.append(f"{key}:{unit} (the entry now prices this unit)")
    assert not stale, (
        f"EXPECTED_CACHE_UNIT_OMISSIONS members that no longer describe anything: "
        f"{stale}. Delete them — a rate added is the signal that the basis is "
        "done, and leaving it behind means the next person to drop that rate is "
        "not told."
    )


# --------------------------------------------------------------------------- #
# The price-list half of each premise                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_committed_digest_describes_a_populated_price_list():
    """An empty or broken digest would make every absence claim below vacuous."""
    digest = load_digest()
    assert digest["model_count"] >= 100, (
        f"the digest describes {digest['model_count']} price-list models; that is "
        "not the offer files, and the 'no cache dimension' claims below would "
        "hold vacuously over it"
    )
    assert len(digest["sources"]) == 2, (
        "the digest was built from one offer file. Anthropic's models are in "
        "AmazonBedrockFoundationModels and everything else is in AmazonBedrock, "
        "so a single-file digest shows two Claude 3 Haiku products and no cache "
        "dimension for a reason that has nothing to do with caching"
    )
    # The extractor has to be able to FIND a cache rate, or "no cache dimension"
    # is a statement about the regex rather than about AWS.
    with_reads = [
        model
        for model, record in digest["models"].items()
        if any(float(rate) > 0 for rate in record["cacheReadInputTokens"])
    ]
    with_writes = [
        model
        for model, record in digest["models"].items()
        if any(float(rate) > 0 for rate in record["cacheWriteInputTokens"])
    ]
    assert len(with_reads) >= 10, (
        f"only {len(with_reads)} price-list models carry a non-zero cache-read "
        "rate; the dimension extraction is broken"
    )
    assert len(with_writes) >= 10, (
        f"only {len(with_writes)} price-list models carry a non-zero cache-write "
        "rate; the dimension extraction is broken"
    )
    assert CACHE_DIMENSION_READING_DATE == digest["retrieved"], (
        "the date metering_units records for its price-list reading "
        f"({CACHE_DIMENSION_READING_DATE}) is not the date the committed digest "
        f"was retrieved ({digest['retrieved']}). A price is a dated fact and the "
        "two have to be the same reading"
    )


def _offer(offer_code: str, products: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The smallest structure ``digest_offer_files`` reads, for the probe below."""
    return {
        "offerCode": offer_code,
        "version": "1",
        "publicationDate": "2026-10-13T00:00:00Z",
        "products": {
            sku: {"attributes": attributes} for sku, attributes in products.items()
        },
        "terms": {
            "OnDemand": {
                sku: {
                    f"{sku}.term": {
                        "priceDimensions": {
                            f"{sku}.dim": {
                                "unit": attributes.pop("_unit", "1K tokens"),
                                "pricePerUnit": {"USD": attributes.pop("_usd")},
                            }
                        }
                    }
                }
                for sku, attributes in products.items()
            }
        },
    }


@pytest.mark.unit
def test_the_digest_extraction_reads_both_dimension_spellings():
    """The derivation itself, probed offline against a synthetic offer file.

    ``--refresh`` is the only thing that normally runs ``digest_offer_files``, so
    without this the extraction rules are committed data whose producer nothing
    exercises — and the rules are where the mistakes are. Four of them matter and
    each is asserted here:

    * **the two spellings.** The same dimension is
      ``NovaLite-cache-read-input-token-count`` in the ``AmazonBedrock`` file and
      ``MP:UGW1_CacheReadInputTokenCount-Units`` in the Anthropic one, so matching
      is done on a ``-``/``_``/``:``-stripped lowercase form. A rule that read only
      one spelling would report "no cache dimension" for every Claude model.
    * **the tier exclusion.** Nova Premier publishes a cache-write dimension at
      flex and priority only. Folding those in would record a rate for a tier this
      project cannot invoke.
    * **the per-token conversion.** One file prices in ``1K tokens`` and the other
      in ``1M tokens``, a factor of a thousand apart, and a dimension priced in
      anything else (an image, a model-hour) must be dropped rather than read as a
      token rate.
    * **the union across files**, since a model can appear in both.
    """
    bedrock = _offer(
        "AmazonBedrock",
        {
            "nova": {
                "model": "Toy Nova",
                "usagetype": "USE1-ToyNova-cache-read-input-token-count",
                "_usd": "0.0000150000",
            },
            "nova_write": {
                "model": "Toy Nova",
                "usagetype": "USE1-ToyNova-cache-write-input-token-count",
                "_usd": "0.0000000000",
            },
            "nova_flex": {
                "model": "Toy Nova",
                "usagetype": "USE1-ToyNova-cache-read-input-token-count-flex",
                "_usd": "0.0009990000",
            },
            "nova_image": {
                "model": "Toy Nova",
                "usagetype": "USE1-ToyNova-cache-read-input-token-count-image",
                "_unit": "image",
                "_usd": "0.0400000000",
            },
            "plain": {
                "model": "Toy Plain",
                "usagetype": "USE1-ToyPlain-input-tokens",
                "_usd": "0.0001000000",
            },
        },
    )
    anthropic = _offer(
        "AmazonBedrockFoundationModels",
        {
            "claude": {
                "model": "Toy Claude",
                "usagetype": "MP:UGW1_CacheReadInputTokenCount-Units",
                "_unit": "1M tokens",
                "_usd": "0.3600000000",
            },
            "claude_1h": {
                "model": "Toy Claude",
                "usagetype": "MP:UGW1_CacheWrite1hInputTokenCount-Units",
                "_unit": "1M tokens",
                "_usd": "7.2000000000",
            },
            # Same model as the other file carries, so the union is exercised.
            "nova_eu": {
                "model": "Toy Nova",
                "usagetype": "EUW1-ToyNova-cache-read-input-token-count",
                "_usd": "0.0000172500",
            },
        },
    )

    digest = digest_offer_files([bedrock, anthropic])

    assert digest["model_count"] == 3
    assert [source["offer_code"] for source in digest["sources"]] == [
        "AmazonBedrock",
        "AmazonBedrockFoundationModels",
    ]

    nova = digest["models"]["Toy Nova"]
    # 1.5e-08 from us-east-1 and 1.725e-08 from the second file, unioned; the flex
    # tier excluded; the image dimension dropped for not being priced in tokens.
    assert nova["cacheReadInputTokens"] == ["1.5e-08", "1.725e-08"]
    assert nova["cacheWriteInputTokens"] == ["0"], (
        "a dimension AWS publishes at exactly zero must be recorded as '0' and not "
        "dropped: that is what tells a reader the unit is free rather than unread"
    )

    claude = digest["models"]["Toy Claude"]
    assert claude["cacheReadInputTokens"] == ["3.6e-07"], (
        "the CamelCase spelling was not matched, or the 1M-tokens divisor was not "
        "applied"
    )
    assert claude["cacheWriteInputTokens"] == [], (
        "the one-hour-TTL cache write was counted; IDP writes five-minute cache "
        "points only"
    )

    plain = digest["models"]["Toy Plain"]
    assert plain["cacheReadInputTokens"] == []
    assert plain["token_dimension_count"] == 1, (
        "token_dimension_count is what makes an empty cache list readable as a "
        "measured absence rather than as a failed read"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry", EXPECTED_CACHE_UNIT_OMISSIONS, ids=lambda e: f"{e.pricing_key}:{e.unit}"
)
def test_priced_by_aws_matches_the_published_price_list(entry: CacheUnitOmission):
    """``priced_by_aws`` is a reading of the offer files, so read them.

    ``published_cache_rates`` raises ``KeyError`` for a model the digest does not
    describe, and that is deliberate: a basis naming a model AWS's price list does
    not carry is an unread claim, not a weaker one, and the empty list it would
    otherwise return reads as "no cache dimension". The price-list spelling is the
    easy thing to get wrong — ``nvidia.nemotron-nano-12b-v2`` is published as
    ``NVIDIA Nemotron Nano 2 VL``, while ``NVIDIA Nemotron Nano 2`` is the 9B text
    model.
    """
    digest = load_digest()
    assert entry.price_list_model in digest["models"], (
        f"{entry.pricing_key}:{entry.unit} records price-list model "
        f"{entry.price_list_model!r}, which the digest does not describe. Check "
        "the spelling of the offer file's 'model' attribute, or refresh the "
        "digest"
    )
    record = digest["models"][entry.price_list_model]
    assert record["token_dimension_count"] > 0, (
        f"the digest found no token-priced dimension at all for "
        f"{entry.price_list_model!r}, so it cannot support a claim either way"
    )
    rates = published_cache_rates(digest, entry.price_list_model, entry.unit)
    chargeable = any(float(rate) > 0 for rate in rates)
    assert chargeable == entry.priced_by_aws, (
        f"{entry.pricing_key}:{entry.unit} records priced_by_aws="
        f"{entry.priced_by_aws}, but AWS's price list publishes "
        f"{rates or 'no dimension'} for {entry.price_list_model!r}:{entry.unit}. "
        "A member whose dimension AWS prices must say so, because that is what "
        "makes the unit reported rather than zeroed"
    )


# --------------------------------------------------------------------------- #
# The reachability half of each premise                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry", EXPECTED_CACHE_UNIT_OMISSIONS, ids=lambda e: f"{e.pricing_key}:{e.unit}"
)
def test_a_registered_model_cannot_meter_the_unit_it_omits(entry: CacheUnitOmission):
    """Both ways a model can emit cache units, ruled out per member.

    A model absent from ``CACHEPOINT_SUPPORTED_MODELS`` is never sent a Converse
    ``cachePoint`` block — but the models in ``_IMPLICIT_CACHE_BASE_NAMES`` cache
    without one and ``prompt_cache`` records ``openai.gpt-6-astra`` reporting
    ``cacheReadInputTokens=2707`` while absent from that list. The two are
    genuinely disjoint concepts, so checking only the first would let an implicit
    cacher be registered and its cache reads cost $0.00.

    This is asserted for **every** member, including the ones whose price-list
    basis already says the dimension costs nothing. The reason is that the two
    halves answer different questions: AWS not pricing a dimension today does not
    mean the model will never report one, and a model that starts reporting a unit
    AWS has started charging for is the exact sequence nobody is watching for.
    """
    from idp_common.bedrock.client import CACHEPOINT_SUPPORTED_MODELS
    from idp_common.bedrock.prompt_cache import (
        _IMPLICIT_CACHE_BASE_NAMES,
        model_caches_implicitly,
    )

    # Probe both import targets before drawing a conclusion from either: a
    # renamed or emptied list, or a predicate answering the same way for every
    # input, would make the assertions below pass vacuously.
    assert len(CACHEPOINT_SUPPORTED_MODELS) > 20, (
        "CACHEPOINT_SUPPORTED_MODELS looks wrong "
        f"({len(CACHEPOINT_SUPPORTED_MODELS)} entries); this test would pass "
        "vacuously"
    )
    assert model_caches_implicitly(f"us.{_IMPLICIT_CACHE_BASE_NAMES[0]}-v1:0")
    assert not model_caches_implicitly("us.anthropic.claude-sonnet-4-5-20250929-v1:0")

    model_id = _model_id(entry.pricing_key)
    assert model_id not in CACHEPOINT_SUPPORTED_MODELS, (
        f"{model_id} is in CACHEPOINT_SUPPORTED_MODELS, so a cachePoint IS sent "
        f"to it and it can meter {entry.unit} — which its pricing entry does not "
        "price. Add the rate to config_library/pricing.yaml and delete this "
        "register member; the omission's basis has stopped being true"
    )
    assert not model_caches_implicitly(model_id), (
        f"{model_id} caches implicitly, so it can meter {entry.unit} with no "
        "cachePoint sent at all. Add the rate to config_library/pricing.yaml and "
        "delete this register member"
    )


@pytest.mark.unit
def test_every_member_records_an_authority_and_a_basis():
    """A member with no sourcing is a line added to a list."""
    for entry in EXPECTED_CACHE_UNIT_OMISSIONS:
        where = f"{entry.pricing_key}:{entry.unit}"
        assert entry.unit in CACHE_UNITS, f"{where} is not a cache unit"
        assert entry.pricing_key.startswith(BEDROCK_PREFIX), f"{where} is not Bedrock"
        assert entry.authority.startswith("https://pricing."), (
            f"{where} names authority {entry.authority!r}. Every member of this "
            "register rests on a reading of a published offer file, so there is "
            "no JUDGEMENT shape here — unlike "
            "NON_CHARGEABLE_METERING_UNITS, where two entries legitimately have "
            "no price list that could settle them"
        )
        assert len(entry.basis) >= 120, (
            f"{where} records a {len(entry.basis)}-character basis. Say what was "
            "read and what it showed"
        )


# --------------------------------------------------------------------------- #
# What the register does to a cost, which is the thing that matters            #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry", EXPECTED_CACHE_UNIT_OMISSIONS, ids=lambda e: f"{e.pricing_key}:{e.unit}"
)
def test_the_runtime_verdict_follows_the_recorded_basis(entry: CacheUnitOmission):
    """``priced_by_aws`` decides $0.00 against reported, and nothing else does."""
    verdict = classify_absent_unit(entry.pricing_key, entry.unit, 1_000_000)
    expected = UNKNOWN if entry.priced_by_aws else NOT_CHARGEABLE
    assert verdict == expected, (
        f"{entry.pricing_key}:{entry.unit} records priced_by_aws="
        f"{entry.priced_by_aws} but classifies as {verdict}"
    )
    # A zero count is free for every member, priced dimension or not: cost is
    # count x rate and there is no spend for a $0.00 to hide.
    assert classify_absent_unit(entry.pricing_key, entry.unit, 0) == NOT_CHARGEABLE


@pytest.mark.unit
@pytest.mark.parametrize("unit", CACHE_UNITS)
def test_an_unregistered_bedrock_entry_omitting_a_cache_unit_is_reported(unit: str):
    """The tenth omission, at runtime rather than in a gate.

    The static test above fails a new ``pricing.yaml`` row that omits a rate, and
    that is the primary catch. This is the second one, and it covers the case the
    gate cannot see at all: a **user-supplied** pricing table, edited through the
    UI pricing editor or loaded from a custom configuration, whose Bedrock entry
    omits a cache rate. Before #1206 both of those priced a cache read at exactly
    $0.00.
    """
    assert (
        classify_absent_unit(
            f"{BEDROCK_PREFIX}some.model-nobody-registered", unit, 5_000
        )
        == UNKNOWN
    )


@pytest.mark.unit
def test_the_product_cost_implementation_reports_an_unregistered_omission():
    """The rule asserted through its caller, not by reading that it calls it.

    This is the product's cost — ``SaveReportingData._get_unit_cost``. The other
    implementation, the benchmark harness's ``price_metering``, is asserted the
    same way in ``benchmarks/tests/test_unpriced_metering.py``, and the two are
    checked separately on purpose: they diverged once over exactly this unit
    (#926, a substring fallback that bound ``cacheReadInputTokens`` to the row's
    ``inputTokens`` price and overcharged cache reads by up to 10x), so "both
    import the same module" is not the property worth having.
    """
    from idp_common.reporting.save_reporting_data import SaveReportingData

    pricing: dict[str, dict[str, Any]] = {
        # Shaped like a real row: priced input and output, no cache rate, and no
        # register entry for the key.
        "bedrock/vendor.unregistered-model-v1:0": {
            "inputTokens": 1e-6,
            "outputTokens": 5e-6,
        },
    }
    reporter = SaveReportingData("test-bucket")
    reporter._pricing_cache = pricing

    for unit in CACHE_UNITS:
        assert (
            reporter._get_unit_cost(
                "bedrock/vendor.unregistered-model-v1:0", unit, 1_000_000
            )
            is None
        ), f"_get_unit_cost priced an unregistered absent {unit} rather than NULL"
        # The priced units still resolve, so this is a unit-level report and not
        # the entry-level one #1146 already covers.
        assert (
            reporter._get_unit_cost(
                "bedrock/vendor.unregistered-model-v1:0", "inputTokens", 1_000_000
            )
            == 1e-6
        )
