"""Unit tests for the rule engine. Run: python3 -m pytest tests -q"""

from decimal import Decimal

import pytest

from pfx.rules import (
    RuleError,
    RuleSet,
    apply_rounding,
    evaluate_condition,
    to_decimal,
)

D = Decimal


# ---------------------------------------------------------------- coercion

def test_to_decimal_handles_strings_with_separators():
    assert to_decimal("1,234.50") == D("1234.50")
    assert to_decimal(" 99 ") == D("99")


def test_to_decimal_float_has_no_binary_drift():
    # Decimal(0.1) would be 0.1000000000000000055511151231257827.
    assert to_decimal(0.1) == D("0.1")
    assert to_decimal(0.1) + to_decimal(0.2) == D("0.3")


def test_to_decimal_rejects_garbage():
    for bad in ("", "N/A", None, True, {}):
        with pytest.raises(RuleError):
            to_decimal(bad)


# --------------------------------------------------------------- conditions

ROW = {
    "sku": "A-100",
    "category": "Premium",
    "cost": "40.00",
    "stockDays": 200,
    "attributes": {"brandTier": "GOLD"},
}


def test_condition_shorthand_and_case_insensitivity():
    assert evaluate_condition({"category": "premium"}, ROW)
    assert evaluate_condition({"category": {"eq": "PREMIUM"}}, ROW)
    assert not evaluate_condition({"category": "budget"}, ROW)


def test_condition_dotted_field_access():
    assert evaluate_condition({"attributes.brandTier": "gold"}, ROW)


def test_condition_logical_operators():
    assert evaluate_condition(
        {"all": [{"category": "Premium"}, {"stockDays": {"gt": 180}}]}, ROW
    )
    assert not evaluate_condition(
        {"all": [{"category": "Premium"}, {"stockDays": {"gt": 900}}]}, ROW
    )
    assert evaluate_condition({"any": [{"category": "X"}, {"category": "Premium"}]}, ROW)
    assert evaluate_condition({"not": {"category": "Budget"}}, ROW)


def test_numeric_compare_works_on_string_numbers():
    assert evaluate_condition({"cost": {"gte": 40}}, ROW)
    assert not evaluate_condition({"cost": {"gt": 40}}, ROW)


def test_missing_field_does_not_match_and_does_not_throw():
    assert not evaluate_condition({"nope": {"gt": 1}}, ROW)
    assert evaluate_condition({"nope": {"is_null": True}}, ROW)


def test_unknown_operator_is_a_load_error():
    with pytest.raises(RuleError):
        evaluate_condition({"cost": {"greater_than": 1}}, ROW)


# ------------------------------------------------------- YAML boolean traps

def test_yaml_parses_on_as_a_boolean_key_and_we_recover_it():
    """`on: annualVolume` in YAML 1.1 becomes {True: 'annualVolume'}.

    Left alone, the tier driver is silently ignored and every SKU is banded on
    the wrong number with no error anywhere. This is the regression guard.
    """
    import yaml
    parsed = yaml.safe_load("tiers:\n  on: annualVolume\n")
    assert True in parsed["tiers"]          # confirm the trap is real
    assert "on" not in parsed["tiers"]

    spec = {
        "base": {"field": "cost"},
        "pipeline": ["base", "tiers"],
        "tiers": {**parsed["tiers"], "mode": "percent",
                  "bands": [{"name": "big", "min": 100, "value": 10}], "default": 90},
    }
    rs = RuleSet(spec)
    assert rs.spec["tiers"]["on"] == "annualVolume"
    # Driver 500 -> 'big' band (+10%), NOT the default (+90%).
    assert rs.price_row({"sku": "X", "cost": "10", "annualVolume": 500}).price == D("11.0")


def test_driver_is_accepted_as_the_safer_alias_for_on():
    rs = RuleSet({"base": {"field": "cost"}, "pipeline": ["base", "tiers"],
                  "tiers": {"driver": "annualVolume", "mode": "percent",
                            "bands": [{"name": "big", "min": 100, "value": 10}],
                            "default": 90}})
    assert rs.price_row({"sku": "X", "cost": "10", "annualVolume": 500}).price == D("11.0")


def test_yaml_boolean_value_still_matches_the_data_it_meant():
    # `hazmat: no` parses as False, but the source data says "N".
    assert evaluate_condition({"hazmat": False}, {"hazmat": "N"})
    assert evaluate_condition({"hazmat": True}, {"hazmat": "Y"})
    assert not evaluate_condition({"hazmat": True}, {"hazmat": "N"})


# ---------------------------------------------------------------- rounding

def test_nearest_up_down_to_increment():
    assert apply_rounding(D("10.234"), {"method": "nearest", "increment": "0.05"}) == D("10.25")
    assert apply_rounding(D("10.21"), {"method": "up", "increment": "0.05"}) == D("10.25")
    assert apply_rounding(D("10.24"), {"method": "down", "increment": "0.05"}) == D("10.20")


def test_decimals_is_half_up_not_bankers():
    # Python's default round() gives 2.66 here (banker's rounding). Pricing does not.
    assert apply_rounding(D("2.665"), {"method": "decimals", "decimals": 2}) == D("2.67")


def test_charm_rounding_forces_the_ending():
    r = {"method": "charm", "increment": "1", "ending": "0.99"}
    assert apply_rounding(D("99.40"), r) == D("99.99")
    assert apply_rounding(D("99.99"), r) == D("99.99")
    assert apply_rounding(D("100.00"), r) == D("100.99")


def test_charm_rounding_downwards_never_exceeds_input():
    r = {"method": "charm", "increment": "1", "ending": "0.99", "direction": "down"}
    assert apply_rounding(D("99.40"), r) == D("98.99")
    assert apply_rounding(D("99.99"), r) == D("99.99")


def test_charm_with_ending_larger_than_increment_is_rejected():
    with pytest.raises(RuleError):
        apply_rounding(D("10"), {"method": "charm", "increment": "1", "ending": "1.50"})


def test_endings_snaps_to_the_nearest_allowed_price_point():
    r = {"method": "endings", "unit": "1", "endings": ["0.00", "0.49", "0.99"]}
    assert apply_rounding(D("10.30"), r) == D("10.49")
    assert apply_rounding(D("10.80"), r) == D("10.99")
    assert apply_rounding(D("10.10"), r) == D("10.00")


def test_endings_direction_down_never_rounds_up():
    r = {"method": "endings", "unit": "1", "endings": ["0.49", "0.99"], "direction": "down"}
    assert apply_rounding(D("10.80"), r) == D("10.49")


def test_large_rounded_prices_never_come_out_in_scientific_notation():
    # Decimal.normalize() turns 5600 into 5.6E+3, which would reach Pricefx as
    # a string no pricing system will accept.
    for value, inc in [("5599.80", 10), ("1633.82", 10), ("120000", 1000)]:
        out = apply_rounding(D(value), {"method": "nearest", "increment": inc})
        assert "E" not in str(out), f"{value} -> {out}"
    assert apply_rounding(D("5599.80"), {"method": "nearest", "increment": 10}) == D("5600")


def test_unknown_rounding_method_is_rejected():
    with pytest.raises(RuleError):
        apply_rounding(D("10"), {"method": "magic"})


# ------------------------------------------------------------------- tiers

TIER_SPEC = {
    "sku_field": "sku",
    "base": {"field": "cost"},
    "tiers": {
        "on": "base",
        "mode": "percent",
        "bands": [
            {"name": "small", "max": 50, "value": 45},
            {"name": "mid", "min": 50, "max": 200, "value": 35},
            {"name": "large", "min": 200, "value": 25},
        ],
    },
}


def test_tier_bands_are_half_open_so_boundaries_land_in_exactly_one_band():
    rs = RuleSet(TIER_SPEC)
    # 50 must be 'mid' (35%), not 'small' -- an inclusive max would double-match.
    r = rs.price_row({"sku": "X", "cost": "50"})
    assert r.price == D("67.50")
    assert [s.rule for s in r.steps if s.stage == "tiers"] == ["mid"]

    assert rs.price_row({"sku": "X", "cost": "49.99"}).price == D("72.4855")
    assert rs.price_row({"sku": "X", "cost": "200"}).price == D("250")


def test_tier_driver_can_be_a_separate_field():
    spec = dict(TIER_SPEC)
    spec["tiers"] = dict(TIER_SPEC["tiers"], on="annualVolume")
    rs = RuleSet(spec)
    r = rs.price_row({"sku": "X", "cost": "10", "annualVolume": 500})
    assert r.price == D("12.50")  # banded on volume 500 -> 'large' 25%


def test_missing_tier_driver_falls_back_to_base_and_says_so():
    spec = dict(TIER_SPEC)
    spec["tiers"] = dict(TIER_SPEC["tiers"], on="annualVolume")
    r = RuleSet(spec).price_row({"sku": "X", "cost": "10"})
    assert r.price == D("14.50")  # base 10 -> 'small' 45%
    assert any("annualVolume missing" in s.note for s in r.steps)


def test_overlapping_bands_are_rejected_at_load_time():
    with pytest.raises(RuleError):
        RuleSet({"base": {"field": "cost"},
                 "tiers": {"bands": [{"min": 200, "max": 50, "value": 10}]}})


# ----------------------------------------------------------------- uplifts

UPLIFT_SPEC = {
    "base": {"field": "cost"},
    "pipeline": ["base", "uplifts"],
    "uplifts": {
        "mode": "accumulate",
        "rules": [
            {"name": "premium", "when": {"category": "Premium"}, "then": {"percent": 10}},
            {"name": "slow-mover", "when": {"stockDays": {"gt": 180}}, "then": {"percent": -20}},
        ],
    },
}


def test_accumulate_applies_every_matching_rule_in_order():
    r = RuleSet(UPLIFT_SPEC).price_row(
        {"sku": "X", "cost": "100", "category": "Premium", "stockDays": 200}
    )
    assert r.price == D("88.00")  # 100 * 1.10 * 0.80
    assert [s.rule for s in r.steps if s.stage == "uplifts"] == ["premium", "slow-mover"]


def test_first_match_stops_after_the_first_hit():
    spec = dict(UPLIFT_SPEC)
    spec["uplifts"] = dict(UPLIFT_SPEC["uplifts"], mode="first_match")
    r = RuleSet(spec).price_row(
        {"sku": "X", "cost": "100", "category": "Premium", "stockDays": 200}
    )
    assert r.price == D("110.00")
    assert [s.rule for s in r.steps if s.stage == "uplifts"] == ["premium"]


def test_uplift_can_take_its_value_from_a_row_field():
    rs = RuleSet({"base": {"field": "cost"}, "pipeline": ["base", "uplifts"],
                  "uplifts": {"rules": [
                      {"name": "contract", "when": {"contractPrice": {"exists": True}},
                       "then": {"set": "@contractPrice"}}]}})
    assert rs.price_row({"sku": "X", "cost": "100", "contractPrice": "72.50"}).price == D("72.50")


def test_row_field_operand_missing_makes_the_rule_a_noop_not_a_crash():
    rs = RuleSet({"base": {"field": "cost"}, "pipeline": ["base", "uplifts"],
                  "uplifts": {"rules": [
                      {"name": "contract", "when": True, "then": {"set": "@contractPrice"}},
                      {"name": "after", "when": True, "then": {"percent": 10}}]}})
    r = rs.price_row({"sku": "X", "cost": "100"})
    assert not r.skipped
    assert r.price == D("110.00")  # first rule no-ops, second still runs
    assert any("skipped" in s.note for s in r.steps)


def test_uplift_with_two_actions_is_rejected_at_load_time():
    with pytest.raises(RuleError):
        RuleSet({"base": {"field": "c"}, "pipeline": ["base", "uplifts"],
                 "uplifts": {"rules": [{"name": "bad", "when": True,
                                        "then": {"percent": 5, "absolute": 2}}]}})


# ------------------------------------------------------------------ bounds

def test_margin_floor_lifts_a_price_that_fell_below_cost():
    rs = RuleSet({
        "base": {"field": "listPrice"},
        "pipeline": ["base", "bounds"],
        "bounds": {"cost_field": "cost", "min_margin_percent": 20},
    })
    r = rs.price_row({"sku": "X", "listPrice": "40", "cost": "40"})
    assert r.price == D("50")  # 40 / (1 - 0.20)


def test_absolute_floor_cannot_undercut_the_margin_floor():
    rs = RuleSet({
        "base": {"field": "listPrice"},
        "pipeline": ["base", "bounds"],
        "bounds": {"cost_field": "cost", "min_margin_percent": 20, "floor": "10"},
    })
    r = rs.price_row({"sku": "X", "listPrice": "5", "cost": "40"})
    assert r.price == D("50")


def test_ceiling_clamps_down():
    rs = RuleSet({"base": {"field": "p"}, "pipeline": ["base", "bounds"],
                  "bounds": {"ceiling": "99"}})
    assert rs.price_row({"sku": "X", "p": "150"}).price == D("99")


# ------------------------------------------------------------- base / skip

def test_missing_base_price_skips_the_row_with_a_reason():
    rs = RuleSet({"base": {"field": "cost"}, "pipeline": ["base"]})
    r = rs.price_row({"sku": "X"})
    assert r.skipped and r.price is None and "no base price" in r.reason


def test_fallback_field_is_used_when_primary_is_empty():
    rs = RuleSet({"base": {"field": "cost", "fallback_field": ["lastPrice"]},
                  "pipeline": ["base"]})
    assert rs.price_row({"sku": "X", "cost": "", "lastPrice": "12.5"}).price == D("12.5")


def test_unparseable_base_price_is_skipped_not_crashed():
    rs = RuleSet({"base": {"field": "cost"}, "pipeline": ["base"]})
    r = rs.price_row({"sku": "X", "cost": "N/A"})
    assert r.skipped and "cannot parse" in r.reason


def test_bad_pipeline_stage_is_rejected():
    with pytest.raises(RuleError):
        RuleSet({"base": {"field": "c"}, "pipeline": ["base", "magic"]})


# ------------------------------------------------------- end to end + audit

FULL_SPEC = {
    "sku_field": "sku",
    "pipeline": ["base", "tiers", "uplifts", "bounds", "rounding"],
    "base": {"field": "cost", "on_missing": "skip"},
    "tiers": {"on": "base", "mode": "percent",
              "bands": [{"name": "low", "max": 100, "value": 40},
                        {"name": "high", "min": 100, "value": 25}]},
    "uplifts": {"mode": "accumulate", "rules": [
        {"name": "gold-brand", "when": {"brandTier": "GOLD"}, "then": {"percent": 8}},
        {"name": "clearance", "when": {"all": [{"lifecycle": "CLEARANCE"},
                                               {"stockDays": {"gt": 180}}]},
         "then": {"percent": -30}},
    ]},
    "bounds": {"cost_field": "cost", "min_margin_percent": 15},
    "rounding": {
        "default": {"method": "charm", "increment": "1", "ending": "0.99"},
        "overrides": [{"name": "bulk-nearest-5c", "when": {"uom": "PALLET"},
                       "rule": {"method": "nearest", "increment": "0.05"}}],
    },
}


def test_full_pipeline_end_to_end():
    rs = RuleSet(FULL_SPEC)
    r = rs.price_row({"sku": "A-1", "cost": "80", "brandTier": "GOLD"})
    # 80 -> +40% = 112 -> +8% = 120.96 -> margin floor 94.11 (no-op) -> 120.99
    assert r.price == D("120.99")
    assert [s.stage for s in r.steps] == ["base", "tiers", "uplifts", "rounding"]


def test_clearance_price_is_still_held_above_the_margin_floor():
    rs = RuleSet(FULL_SPEC)
    r = rs.price_row({"sku": "A-2", "cost": "80", "lifecycle": "CLEARANCE", "stockDays": 400})
    # 80 -> 112 -> -30% = 78.40, below the 15% margin floor of 94.1176...
    assert any(s.rule == "min_margin" for s in r.steps)
    assert r.price == D("94.99")


def test_rounding_override_wins_over_default():
    rs = RuleSet(FULL_SPEC)
    r = rs.price_row({"sku": "A-3", "cost": "80", "uom": "PALLET"})
    assert r.price == D("112")  # nearest 0.05, not charm .99


def test_audit_trail_is_complete_and_reconciles():
    rs = RuleSet(FULL_SPEC)
    r = rs.price_row({"sku": "A-1", "cost": "80", "brandTier": "GOLD"})
    # Every step's `after` must be the next step's `before` -- no hidden maths.
    for prev, nxt in zip(r.steps, r.steps[1:]):
        assert prev.after == nxt.before
    assert r.steps[-1].after == r.price


def test_price_rows_processes_a_batch_and_isolates_failures():
    rs = RuleSet(FULL_SPEC)
    results = rs.price_rows([
        {"sku": "OK", "cost": "80"},
        {"sku": "BAD", "cost": "not-a-number"},
        {"sku": "OK2", "cost": "500"},
    ])
    assert [r.skipped for r in results] == [False, True, False]
    assert results[2].price == D("625.99")
