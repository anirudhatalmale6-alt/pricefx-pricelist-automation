"""
Rule engine for Pricefx price list calculations.

Everything here is pure: it takes a dict describing one product/price row and a
parsed rule set, and returns a Decimal plus a full audit trail. No network, no
Pricefx dependency -- which is exactly why it is unit-testable and why your team
can extend it without a tenant to test against.

Evaluation order is fixed and explicit (see PIPELINE_STAGES). Each stage is
optional; a stage that is absent from the rule file is skipped.

  1. base      pick the starting number off the source row
  2. tiers     band-based adjustment driven by some numeric field
  3. uplifts   if/then conditional adjustments
  4. bounds    floor / ceiling clamps (incl. margin floor)
  5. rounding  final presentation rounding

Money is Decimal end to end. Never float -- 0.1 + 0.2 problems show up as
one-cent drift across 40k SKUs and nobody can reproduce them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Callable

PIPELINE_STAGES = ("base", "tiers", "uplifts", "bounds", "rounding")

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


class RuleError(Exception):
    """Raised for a malformed rule file. Fail loudly at load time, not per-row."""


class SkipRow(Exception):
    """Raised when a row cannot be priced and the rule set says to skip it."""


# --------------------------------------------------------------------------
# value helpers
# --------------------------------------------------------------------------

def to_decimal(value: Any, *, field_name: str = "value") -> Decimal:
    """Coerce whatever the source system handed us into a Decimal.

    Pricefx and most ERP feeds happily return numbers as strings, sometimes with
    thousands separators. float(x) on "1,234.50" throws; Decimal(str) on a float
    carries binary noise. So: normalise the string, then Decimal it.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass -- catch before int
        raise RuleError(f"{field_name}: boolean is not a price")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace(" ", "")
        if cleaned in ("", "-", "null", "NULL", "None"):
            raise RuleError(f"{field_name}: empty numeric value {value!r}")
        try:
            return Decimal(cleaned)
        except Exception as exc:  # noqa: BLE001 - want the field name in the message
            raise RuleError(f"{field_name}: cannot parse {value!r} as a number") from exc
    raise RuleError(f"{field_name}: cannot parse {value!r} ({type(value).__name__}) as a number")


def _operand(spec: Any, row: dict, label: str) -> Decimal:
    """Resolve a rule operand to a Decimal.

    A literal (``8``, ``"4.50"``) is used as-is. A string starting with ``@`` is
    read off the row instead -- ``set: "@contractPrice"`` means "use whatever
    contract price this SKU carries". If that field is missing the rule is
    treated as not applying, which is what you want for optional overrides.
    """
    if isinstance(spec, str) and spec.startswith("@"):
        field_name = spec[1:]
        raw = get_field(row, field_name)
        if raw is None or raw == "":
            raise SkipRow(f"{label}: row has no {field_name}")
        return to_decimal(raw, field_name=field_name)
    return to_decimal(spec, field_name=label)


def get_field(row: dict, name: str, default: Any = None) -> Any:
    """Read a field from a source row.

    Supports dotted access into nested dicts (``attributes.brandTier``) because
    Pricefx fetch payloads nest custom fields under their own object depending on
    how the tenant is configured.
    """
    if name in row:
        return row[name]
    cur: Any = row
    for part in name.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


# --------------------------------------------------------------------------
# conditions
# --------------------------------------------------------------------------

def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _cmp_numeric(op: Callable[[Decimal, Decimal], bool]) -> Callable[[Any, Any], bool]:
    def run(actual: Any, expected: Any) -> bool:
        if actual is None:
            return False
        try:
            return op(to_decimal(actual), to_decimal(expected))
        except RuleError:
            return False
    return run


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, e: _norm(a) == _norm(e),
    "ne": lambda a, e: _norm(a) != _norm(e),
    "in": lambda a, e: _norm(a) in [_norm(x) for x in _as_list(e)],
    "not_in": lambda a, e: _norm(a) not in [_norm(x) for x in _as_list(e)],
    "gt": _cmp_numeric(lambda a, e: a > e),
    "gte": _cmp_numeric(lambda a, e: a >= e),
    "lt": _cmp_numeric(lambda a, e: a < e),
    "lte": _cmp_numeric(lambda a, e: a <= e),
    "contains": lambda a, e: a is not None and str(e).lower() in str(a).lower(),
    "startswith": lambda a, e: a is not None and str(a).lower().startswith(str(e).lower()),
    "endswith": lambda a, e: a is not None and str(a).lower().endswith(str(e).lower()),
    "regex": lambda a, e: a is not None and re.search(str(e), str(a)) is not None,
    "exists": lambda a, e: (a is not None and a != "") is bool(e),
    "is_null": lambda a, e: (a is None or a == "") is bool(e),
}

_LOGICAL = ("all", "any", "not")


# YAML 1.1 turns these bare words into booleans on the VALUE side too, so
# `hazmat: no` in a rule file arrives as False while the data says "N".
# Fold both representations onto one key so the comparison behaves as written.
_TRUTHY = {"y", "yes", "true", "t", "1"}
_FALSY = {"n", "no", "false", "f", "0"}


def _norm(value: Any) -> Any:
    """Case-insensitive, whitespace-tolerant comparison key.

    Category codes come back from different systems as 'Premium', 'PREMIUM',
    ' premium '. Matching those as distinct segments is the single most common
    way a SKU silently lands on the wrong list.
    """
    if isinstance(value, bool):
        return "__true__" if value else "__false__"
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUTHY:
            return "__true__"
        if text in _FALSY:
            return "__false__"
        return text
    return value


def evaluate_condition(cond: Any, row: dict) -> bool:
    """Evaluate a condition tree against one row.

    Shapes accepted::

        {"field": {"op": expected, ...}}      # all ops on that field must pass
        {"field": "literal"}                  # shorthand for eq
        {"all": [ ... ]} / {"any": [ ... ]} / {"not": { ... }}
        True / None                           # always matches
    """
    if cond is None or cond is True:
        return True
    if cond is False:
        return False
    if not isinstance(cond, dict):
        raise RuleError(f"condition must be a mapping, got {type(cond).__name__}")

    results: list[bool] = []
    for key, spec in cond.items():
        if key == "all":
            results.append(all(evaluate_condition(c, row) for c in _as_list(spec)))
        elif key == "any":
            results.append(any(evaluate_condition(c, row) for c in _as_list(spec)))
        elif key == "not":
            results.append(not evaluate_condition(spec, row))
        else:
            actual = get_field(row, key)
            if isinstance(spec, dict):
                for op, expected in spec.items():
                    if op not in OPERATORS:
                        raise RuleError(
                            f"unknown operator {op!r} on field {key!r}. "
                            f"Known: {', '.join(sorted(OPERATORS))}"
                        )
                    results.append(OPERATORS[op](actual, expected))
            else:
                results.append(OPERATORS["eq"](actual, spec))
    return all(results)


# --------------------------------------------------------------------------
# rounding
# --------------------------------------------------------------------------

def _quantize_to_increment(value: Decimal, increment: Decimal, rounding: str) -> Decimal:
    if increment <= ZERO:
        raise RuleError("rounding increment must be > 0")
    steps = value / increment
    steps = steps.quantize(Decimal("1"), rounding=rounding)
    result = steps * increment
    # Present at the increment's own scale. .normalize() would turn 5600 into
    # 5.6E+3, which then reaches Pricefx as a string in scientific notation.
    exponent = min(increment.as_tuple().exponent, 0)
    return result.quantize(Decimal(1).scaleb(exponent))


def apply_rounding(value: Decimal, rule: dict) -> Decimal:
    """Apply one rounding rule.

    method:
      nearest / up / down  -- to `increment` (default 0.01)
      decimals             -- half-up to N decimal places
      charm                -- round DOWN to `increment`, then force `ending`
                              (99.40 with increment 1 + ending .99 -> 99.99;
                               use direction: down to get 98.99 instead)
      endings              -- snap to the nearest allowed ending in `endings`
    """
    method = str(rule.get("method", "nearest")).lower()

    if method == "decimals":
        places = int(rule.get("decimals", 2))
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)

    if method in ("nearest", "up", "down"):
        increment = to_decimal(rule.get("increment", "0.01"), field_name="increment")
        mode = {"nearest": ROUND_HALF_UP, "up": ROUND_CEILING, "down": ROUND_FLOOR}[method]
        return _quantize_to_increment(value, increment, mode)

    if method == "charm":
        increment = to_decimal(rule.get("increment", "1"), field_name="increment")
        ending = to_decimal(rule.get("ending", "0.99"), field_name="ending")
        if ending >= increment:
            raise RuleError(
                f"charm rounding: ending {ending} must be smaller than increment {increment}"
            )
        floor_val = _quantize_to_increment(value, increment, ROUND_FLOOR)
        candidate = floor_val + ending
        direction = str(rule.get("direction", "up")).lower()
        if direction == "down" and candidate > value:
            candidate = floor_val - increment + ending
        if candidate < ZERO:
            candidate = ending
        return candidate

    if method == "endings":
        endings = [to_decimal(e, field_name="endings") for e in rule.get("endings", [])]
        if not endings:
            raise RuleError("rounding method 'endings' requires a non-empty `endings` list")
        unit = to_decimal(rule.get("unit", "1"), field_name="unit")
        whole = _quantize_to_increment(value, unit, ROUND_FLOOR)
        candidates = [whole + e for e in endings] + [whole + unit + e for e in endings]
        if str(rule.get("direction", "nearest")).lower() == "down":
            below = [c for c in candidates if c <= value]
            return max(below) if below else min(candidates)
        return min(candidates, key=lambda c: (abs(c - value), c))

    raise RuleError(
        f"unknown rounding method {method!r}. "
        "Known: nearest, up, down, decimals, charm, endings"
    )


# --------------------------------------------------------------------------
# rule set
# --------------------------------------------------------------------------

@dataclass
class Step:
    """One line of the audit trail for a single row."""
    stage: str
    rule: str
    before: Decimal
    after: Decimal
    note: str = ""

    @property
    def delta(self) -> Decimal:
        return self.after - self.before

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "rule": self.rule,
            "before": str(self.before),
            "after": str(self.after),
            "delta": str(self.delta),
            "note": self.note,
        }


@dataclass
class Result:
    sku: Any
    price: Decimal | None
    steps: list[Step] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "price": None if self.price is None else str(self.price),
            "skipped": self.skipped,
            "reason": self.reason,
            "steps": [s.as_dict() for s in self.steps],
        }


def normalize_yaml_keys(obj: Any, _path: str = "") -> Any:
    """Undo YAML 1.1's boolean-key surprise.

    In YAML 1.1 (which PyYAML implements) the bare keys `on`, `off`, `yes` and
    `no` parse as booleans, not strings. So `on: annualVolume` in a rules file
    silently becomes `{True: 'annualVolume'}`, the code looks up "on", finds
    nothing, falls back to its default and prices every SKU off the wrong
    driver -- with no error anywhere. Map those keys back to their text form.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key is True:
                key = "on"
            elif key is False:
                key = "off"
            out[key] = normalize_yaml_keys(value, f"{_path}.{key}")
        return out
    if isinstance(obj, list):
        return [normalize_yaml_keys(v, _path) for v in obj]
    return obj


class RuleSet:
    """A parsed, validated rule file."""

    def __init__(self, spec: dict):
        if not isinstance(spec, dict):
            raise RuleError("rule file must be a mapping at the top level")
        spec = normalize_yaml_keys(spec)
        self.spec = spec
        self.sku_field = spec.get("sku_field", "sku")
        self.pipeline = [s for s in spec.get("pipeline", PIPELINE_STAGES)]
        unknown = [s for s in self.pipeline if s not in PIPELINE_STAGES]
        if unknown:
            raise RuleError(
                f"unknown pipeline stage(s) {unknown}. Known: {list(PIPELINE_STAGES)}"
            )
        self._validate()

    # -- validation ------------------------------------------------------
    def _validate(self) -> None:
        """Fail at load time on anything structurally wrong.

        A rule file that blows up on row 12,000 of a production run is a much
        worse outcome than one that refuses to start.
        """
        base = self.spec.get("base")
        if "base" in self.pipeline:
            if not base or not base.get("field"):
                raise RuleError("stage 'base' is in the pipeline but base.field is not set")
            on_missing = str(base.get("on_missing", "skip")).lower()
            if on_missing not in ("skip", "error", "zero"):
                raise RuleError("base.on_missing must be one of: skip, error, zero")

        tiers = self.spec.get("tiers")
        if tiers:
            mode = str(tiers.get("mode", "percent")).lower()
            if mode not in ("percent", "absolute", "multiplier"):
                raise RuleError("tiers.mode must be one of: percent, absolute, multiplier")
            bands = tiers.get("bands") or []
            if not bands and "default" not in tiers:
                raise RuleError("tiers has neither `bands` nor a `default`")
            for i, band in enumerate(bands):
                if "value" not in band:
                    raise RuleError(f"tiers.bands[{i}] has no `value`")
                lo = band.get("min")
                hi = band.get("max")
                if lo is not None and hi is not None and to_decimal(lo) >= to_decimal(hi):
                    raise RuleError(
                        f"tiers.bands[{i}]: min {lo} must be less than max {hi}"
                    )

        uplifts = self.spec.get("uplifts")
        if uplifts:
            mode = str(uplifts.get("mode", "accumulate")).lower()
            if mode not in ("accumulate", "first_match"):
                raise RuleError("uplifts.mode must be one of: accumulate, first_match")
            for i, rule in enumerate(uplifts.get("rules") or []):
                then = rule.get("then")
                if not then:
                    raise RuleError(f"uplifts.rules[{i}] ({rule.get('name')}) has no `then`")
                keys = set(then) & {"percent", "absolute", "multiplier", "set"}
                if len(keys) != 1:
                    raise RuleError(
                        f"uplifts.rules[{i}] ({rule.get('name')}): `then` must have exactly "
                        "one of percent / absolute / multiplier / set"
                    )
                # Surface a bad condition now rather than mid-run.
                evaluate_condition(rule.get("when"), {})

        rounding = self.spec.get("rounding")
        if rounding:
            probe = Decimal("123.456")
            if rounding.get("default"):
                apply_rounding(probe, rounding["default"])
            for i, ov in enumerate(rounding.get("overrides") or []):
                if "rule" not in ov:
                    raise RuleError(f"rounding.overrides[{i}] has no `rule`")
                evaluate_condition(ov.get("when"), {})
                apply_rounding(probe, ov["rule"])

    # -- stages ----------------------------------------------------------
    def _stage_base(self, row: dict, steps: list[Step]) -> Decimal:
        cfg = self.spec["base"]
        candidates = [cfg["field"]] + _as_list(cfg.get("fallback_field") or [])
        for name in candidates:
            raw = get_field(row, name)
            if raw is None or raw == "":
                continue
            value = to_decimal(raw, field_name=name)
            steps.append(Step("base", name, ZERO, value, "source field"))
            return value

        on_missing = str(cfg.get("on_missing", "skip")).lower()
        tried = ", ".join(candidates)
        if on_missing == "error":
            raise RuleError(f"no base price found (tried: {tried})")
        if on_missing == "zero":
            steps.append(Step("base", "missing", ZERO, ZERO, f"no value in: {tried}"))
            return ZERO
        raise SkipRow(f"no base price (tried: {tried})")

    def _stage_tiers(self, value: Decimal, row: dict, steps: list[Step]) -> Decimal:
        cfg = self.spec.get("tiers")
        if not cfg:
            return value

        # `driver` is the preferred key; `on` is accepted for readability and is
        # rescued from YAML's boolean-key trap by normalize_yaml_keys().
        on = cfg.get("driver") or cfg.get("on", "base")
        if on == "base":
            driver = value
        else:
            raw = get_field(row, on)
            if raw is None or raw == "":
                driver = value
                steps.append(Step("tiers", "driver-fallback", value, value,
                                  f"{on} missing, banded on base instead"))
            else:
                driver = to_decimal(raw, field_name=on)

        chosen = None
        label = "default"
        for band in cfg.get("bands") or []:
            lo = band.get("min")
            hi = band.get("max")
            # Bands are half-open [min, max) so adjacent bands never both match.
            if lo is not None and driver < to_decimal(lo, field_name="band.min"):
                continue
            if hi is not None and driver >= to_decimal(hi, field_name="band.max"):
                continue
            chosen = to_decimal(band["value"], field_name="band.value")
            label = band.get("name") or f"[{lo if lo is not None else '-inf'},{hi if hi is not None else 'inf'})"
            break

        if chosen is None:
            if "default" not in cfg:
                steps.append(Step("tiers", "no-band", value, value,
                                  f"driver {driver} matched no band and no default set"))
                return value
            chosen = to_decimal(cfg["default"], field_name="tiers.default")

        mode = str(cfg.get("mode", "percent")).lower()
        if mode == "percent":
            after = value * (ONE + chosen / HUNDRED)
            note = f"driver={driver} {chosen:+}%"
        elif mode == "multiplier":
            after = value * chosen
            note = f"driver={driver} x{chosen}"
        else:
            after = value + chosen
            note = f"driver={driver} {chosen:+} abs"
        steps.append(Step("tiers", label, value, after, note))
        return after

    def _stage_uplifts(self, value: Decimal, row: dict, steps: list[Step]) -> Decimal:
        cfg = self.spec.get("uplifts")
        if not cfg:
            return value
        first_match_only = str(cfg.get("mode", "accumulate")).lower() == "first_match"

        for i, rule in enumerate(cfg.get("rules") or []):
            name = rule.get("name") or f"rule[{i}]"
            if not evaluate_condition(rule.get("when"), row):
                continue
            then = rule["then"]
            before = value
            try:
                if "percent" in then:
                    pct = _operand(then["percent"], row, f"{name}.percent")
                    value = value * (ONE + pct / HUNDRED)
                    note = f"{pct:+}%"
                elif "multiplier" in then:
                    mult = _operand(then["multiplier"], row, f"{name}.multiplier")
                    value = value * mult
                    note = f"x{mult}"
                elif "absolute" in then:
                    amt = _operand(then["absolute"], row, f"{name}.absolute")
                    value = value + amt
                    note = f"{amt:+} abs"
                else:
                    value = _operand(then["set"], row, f"{name}.set")
                    note = "set"
            except SkipRow as exc:
                # A rule that points at a missing row field is a no-op, not a
                # crash: "@contractPrice" simply does not apply to rows without one.
                steps.append(Step("uplifts", name, before, before, f"skipped: {exc}"))
                continue
            steps.append(Step("uplifts", name, before, value, note))
            if first_match_only:
                break
        return value

    def _stage_bounds(self, value: Decimal, row: dict, steps: list[Step]) -> Decimal:
        cfg = self.spec.get("bounds")
        if not cfg:
            return value

        # Margin floor is applied first: it is a hard commercial constraint, and
        # an absolute floor set below cost should not be able to override it.
        margin = cfg.get("min_margin_percent")
        cost_field = cfg.get("cost_field")
        if margin is not None and cost_field:
            raw_cost = get_field(row, cost_field)
            if raw_cost not in (None, ""):
                cost = to_decimal(raw_cost, field_name=cost_field)
                pct = to_decimal(margin, field_name="min_margin_percent")
                if pct >= HUNDRED:
                    raise RuleError("bounds.min_margin_percent must be below 100")
                floor_price = cost / (ONE - pct / HUNDRED)
                if value < floor_price:
                    steps.append(Step("bounds", "min_margin", value, floor_price,
                                      f"cost {cost} needs {pct}% margin"))
                    value = floor_price

        for key, label in (("floor", "floor"), ("min", "floor"), ("ceiling", "ceiling"), ("max", "ceiling")):
            if key not in cfg:
                continue
            limit = to_decimal(cfg[key], field_name=f"bounds.{key}")
            if label == "floor" and value < limit:
                steps.append(Step("bounds", "floor", value, limit, f"clamped up to {limit}"))
                value = limit
            elif label == "ceiling" and value > limit:
                steps.append(Step("bounds", "ceiling", value, limit, f"clamped down to {limit}"))
                value = limit

        for key, label in (("floor_field", "floor"), ("ceiling_field", "ceiling")):
            if key not in cfg:
                continue
            raw = get_field(row, cfg[key])
            if raw in (None, ""):
                continue
            limit = to_decimal(raw, field_name=cfg[key])
            if label == "floor" and value < limit:
                steps.append(Step("bounds", f"floor:{cfg[key]}", value, limit, "row floor"))
                value = limit
            elif label == "ceiling" and value > limit:
                steps.append(Step("bounds", f"ceiling:{cfg[key]}", value, limit, "row ceiling"))
                value = limit
        return value

    def _stage_rounding(self, value: Decimal, row: dict, steps: list[Step]) -> Decimal:
        cfg = self.spec.get("rounding")
        if not cfg:
            return value
        for ov in cfg.get("overrides") or []:
            if evaluate_condition(ov.get("when"), row):
                after = apply_rounding(value, ov["rule"])
                steps.append(Step("rounding", ov.get("name", "override"), value, after,
                                  str(ov["rule"])))
                return after
        if cfg.get("default"):
            after = apply_rounding(value, cfg["default"])
            steps.append(Step("rounding", "default", value, after, str(cfg["default"])))
            return after
        return value

    # -- entry points ----------------------------------------------------
    def price_row(self, row: dict) -> Result:
        sku = get_field(row, self.sku_field)
        steps: list[Step] = []
        runner = {
            "base": lambda v: self._stage_base(row, steps),
            "tiers": lambda v: self._stage_tiers(v, row, steps),
            "uplifts": lambda v: self._stage_uplifts(v, row, steps),
            "bounds": lambda v: self._stage_bounds(v, row, steps),
            "rounding": lambda v: self._stage_rounding(v, row, steps),
        }
        value = ZERO
        try:
            for stage in self.pipeline:
                value = runner[stage](value)
        except SkipRow as exc:
            return Result(sku=sku, price=None, steps=steps, skipped=True, reason=str(exc))
        except RuleError as exc:
            return Result(sku=sku, price=None, steps=steps, skipped=True,
                          reason=f"rule error: {exc}")
        return Result(sku=sku, price=value, steps=steps)

    def price_rows(self, rows) -> list[Result]:
        return [self.price_row(r) for r in rows]


def load_ruleset(path: str) -> RuleSet:
    import yaml  # imported here so the module stays usable without PyYAML

    with open(path, "r", encoding="utf-8") as fh:
        return RuleSet(yaml.safe_load(fh))
