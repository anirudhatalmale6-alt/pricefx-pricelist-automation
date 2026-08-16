"""
Orchestration: source rows -> segments -> priced rows -> Pricefx price list.

The flow, end to end:

  1. load()      pull source rows (Pricefx Product master, or a CSV/DB feed)
  2. segment()   split rows into segment buckets using the category filters
  3. price()     run the rule engine per row -> price + audit trail
  4. publish()   create the PriceList header, add line items, calculate,
                 optionally submit for approval

Each step is separately callable, so `--dry-run` can run 1-3 and write a preview
CSV without ever touching the tenant. That preview is how you sign off on the
numbers before a single object is created in Pricefx.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from .client import (
    PricefxClient,
    PricefxError,
    TYPE_PRICE_LIST,
    TYPE_PRICE_LIST_ITEM,
    TYPE_PRODUCT,
)
from .rules import Result, RuleSet, evaluate_condition, get_field

log = logging.getLogger("pfx.pricelist")


@dataclass
class Segment:
    """One target price list: a name, a filter, and optionally its own rules."""
    name: str
    filter: dict | None = None
    label: str | None = None
    rules: RuleSet | None = None
    price_list_name: str | None = None
    extra_fields: dict = field(default_factory=dict)

    def matches(self, row: dict) -> bool:
        return evaluate_condition(self.filter, row)


@dataclass
class SegmentRun:
    segment: Segment
    rows: list[dict] = field(default_factory=list)
    results: list[Result] = field(default_factory=list)
    price_list: dict | None = None
    items_created: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def priced(self) -> list[Result]:
        return [r for r in self.results if not r.skipped]

    @property
    def skipped(self) -> list[Result]:
        return [r for r in self.results if r.skipped]


# ---------------------------------------------------------------- sourcing

def load_from_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def load_from_pricefx(
    client: PricefxClient,
    *,
    type_code: str = TYPE_PRODUCT,
    criteria: dict | None = None,
    limit: int | None = None,
) -> list[dict]:
    rows = client.fetch_all(type_code, criteria=criteria, limit=limit)
    log.info("Fetched %d rows of %s from Pricefx", len(rows), type_code)
    return rows


def load_source(client: PricefxClient | None, cfg: dict) -> list[dict]:
    """Dispatch on source.type. Add a branch here to plug in a new feed."""
    src = cfg.get("source") or {}
    kind = str(src.get("type", "pricefx")).lower()

    if kind == "csv":
        path = src.get("path")
        if not path:
            raise PricefxError("source.type is csv but source.path is not set")
        rows = load_from_csv(path)
        log.info("Loaded %d rows from %s", len(rows), path)
        return rows

    if kind == "pricefx":
        if client is None:
            raise PricefxError("source.type is pricefx but no client was supplied")
        criteria = src.get("criteria")
        if src.get("filter_field") and src.get("filter_value") is not None:
            criteria = PricefxClient.criteria(
                PricefxClient.crit(src["filter_field"], "equals", src["filter_value"])
            )
        return load_from_pricefx(
            client,
            type_code=src.get("type_code", TYPE_PRODUCT),
            criteria=criteria,
            limit=src.get("limit"),
        )

    raise PricefxError(f"unknown source.type {kind!r}. Supported: pricefx, csv")


# -------------------------------------------------------------- segmenting

def build_segments(cfg: dict, default_rules: RuleSet | None) -> list[Segment]:
    segments: list[Segment] = []
    for spec in cfg.get("segments") or []:
        if not spec.get("name"):
            raise PricefxError("every segment needs a `name`")
        rules = default_rules
        if spec.get("rules_file"):
            from .rules import load_ruleset
            rules = load_ruleset(spec["rules_file"])
        segments.append(Segment(
            name=spec["name"],
            filter=spec.get("filter"),
            label=spec.get("label"),
            rules=rules,
            price_list_name=spec.get("price_list_name"),
            extra_fields=spec.get("fields") or {},
        ))
    if not segments:
        raise PricefxError("no segments defined -- nothing to build")
    return segments


def segment_rows(rows: Iterable[dict], segments: list[Segment], *,
                 multi_match: str = "first") -> tuple[dict[str, SegmentRun], list[dict]]:
    """Bucket rows into segments.

    multi_match:
      first -- a row lands on the first matching segment only (default; this is
               what "each SKU ends up on the right segment list" usually means)
      all   -- a row lands on every matching segment

    Rows matching nothing are returned separately rather than silently dropped:
    an unclassified SKU is a data problem you need to see, not a rounding error.
    """
    runs = {s.name: SegmentRun(segment=s) for s in segments}
    unmatched: list[dict] = []
    for row in rows:
        hit = False
        for seg in segments:
            if seg.matches(row):
                runs[seg.name].rows.append(row)
                hit = True
                if multi_match == "first":
                    break
        if not hit:
            unmatched.append(row)
    for name, run in runs.items():
        log.info("Segment %-24s %5d rows", name, len(run.rows))
    if unmatched:
        log.warning("%d rows matched no segment (see the unmatched report)", len(unmatched))
    return runs, unmatched


# ----------------------------------------------------------------- pricing

def price_segment(run: SegmentRun) -> SegmentRun:
    rules = run.segment.rules
    if rules is None:
        raise PricefxError(f"segment {run.segment.name} has no rule set")
    run.results = rules.price_rows(run.rows)
    log.info("Segment %-24s priced %d, skipped %d",
             run.segment.name, len(run.priced), len(run.skipped))
    return run


# -------------------------------------------------------------- publishing

def render_name(template: str, *, segment: Segment, today: date, cfg: dict) -> str:
    return template.format(
        segment=segment.name,
        label=segment.label or segment.name,
        YYYY=today.strftime("%Y"),
        YYYYMM=today.strftime("%Y%m"),
        YYYYMMDD=today.strftime("%Y%m%d"),
        MM=today.strftime("%m"),
        DD=today.strftime("%d"),
        partition=cfg.get("tenant", {}).get("partition", ""),
    )


def create_price_list(
    client: PricefxClient,
    *,
    segment: Segment,
    cfg: dict,
    today: date,
) -> dict:
    """Create the PriceList header object and return it (with its id)."""
    pl_cfg = cfg.get("price_list") or {}
    template = segment.price_list_name or pl_cfg.get("name_template", "PL_{segment}_{YYYYMM}")
    name = render_name(template, segment=segment, today=today, cfg=cfg)

    record: dict[str, Any] = {
        "uniqueName": name,
        "label": name,
        "targetDate": today.isoformat(),
        "status": pl_cfg.get("status", "DRAFT"),
    }
    if pl_cfg.get("calculation_logic"):
        record["pricingParameters"] = pl_cfg.get("pricing_parameters") or {}
        record["calculationLogic"] = pl_cfg["calculation_logic"]
    if pl_cfg.get("currency"):
        record["currency"] = pl_cfg["currency"]
    record.update(pl_cfg.get("fields") or {})
    record.update(segment.extra_fields)

    existing = _find_price_list(client, name)
    if existing:
        policy = str(pl_cfg.get("on_exists", "fail")).lower()
        if policy == "fail":
            raise PricefxError(
                f"price list {name!r} already exists (id {existing.get('id')}). "
                "Set price_list.on_exists to `reuse` or `suffix` if that is expected."
            )
        if policy == "reuse":
            log.info("Reusing existing price list %s (id %s)", name, existing.get("id"))
            return existing
        if policy == "suffix":
            for n in range(2, 100):
                candidate = f"{name}_{n}"
                if not _find_price_list(client, candidate):
                    record["uniqueName"] = record["label"] = name = candidate
                    break
            else:
                raise PricefxError(f"could not find a free name based on {name!r}")
        else:
            raise PricefxError(f"unknown price_list.on_exists {policy!r}")

    created = client.add(TYPE_PRICE_LIST, record)
    log.info("Created price list %s (id %s)", name, created.get("id"))
    return created


def _find_price_list(client: PricefxClient, unique_name: str) -> dict | None:
    if client.dry_run:
        return None
    try:
        rows = client.fetch_all(
            TYPE_PRICE_LIST,
            criteria=PricefxClient.criteria(
                PricefxClient.crit("uniqueName", "equals", unique_name)
            ),
            limit=1,
        )
    except PricefxError as exc:
        log.warning("existence check for %s failed (%s); continuing", unique_name, exc)
        return None
    return rows[0] if rows else None


def build_line_item(
    result: Result,
    row: dict,
    *,
    price_list: dict,
    cfg: dict,
    segment: Segment,
) -> dict:
    """Map one priced row onto a PriceListItem payload.

    Field names are config-driven (`line_item.fields`) because tenants name
    their custom fields differently. Nothing here is hard-coded to my guesses
    about your schema.
    """
    li_cfg = cfg.get("line_item") or {}
    mapping = li_cfg.get("fields") or {}

    item: dict[str, Any] = {
        "priceListId": price_list.get("id"),
        "sku": get_field(row, cfg.get("sku_field", "sku")),
    }
    price_field = li_cfg.get("price_field", "resultPrice")
    item[price_field] = _money(result.price, li_cfg.get("decimals", 2))

    for target, source in mapping.items():
        # "attribute3: category" -> copy row.category into attribute3.
        # "attribute9: =SEGMENT" -> literal value after the '='.
        if isinstance(source, str) and source.startswith("="):
            item[target] = source[1:]
        else:
            item[target] = get_field(row, str(source))

    if li_cfg.get("segment_field"):
        item[li_cfg["segment_field"]] = segment.label or segment.name

    if li_cfg.get("audit_field"):
        item[li_cfg["audit_field"]] = " | ".join(
            f"{s.stage}:{s.rule}={s.after}" for s in result.steps
        )[: int(li_cfg.get("audit_max_chars", 500))]

    return {k: v for k, v in item.items() if v is not None}


def _money(value: Decimal | None, decimals: int) -> float | None:
    if value is None:
        return None
    # Serialise as a plain number with fixed scale. str() would be safer against
    # float drift, but Pricefx expects a JSON number on price fields.
    return float(round(value, decimals))


def publish_segment(
    client: PricefxClient,
    run: SegmentRun,
    *,
    cfg: dict,
    today: date,
) -> SegmentRun:
    if not run.priced:
        log.warning("Segment %s has no priced rows -- skipping publish", run.segment.name)
        return run

    run.price_list = create_price_list(client, segment=run.segment, cfg=cfg, today=today)

    by_sku = {get_field(r, cfg.get("sku_field", "sku")): r for r in run.rows}
    items = [
        build_line_item(res, by_sku.get(res.sku, {}), price_list=run.price_list,
                        cfg=cfg, segment=run.segment)
        for res in run.priced
    ]
    created = client.add_many(TYPE_PRICE_LIST_ITEM, items,
                             chunk=int((cfg.get("line_item") or {}).get("chunk", 200)))
    run.items_created = len(created)
    log.info("Segment %s: added %d line items to %s",
             run.segment.name, run.items_created, run.price_list.get("uniqueName"))

    pl_cfg = cfg.get("price_list") or {}
    pl_id = run.price_list.get("id")
    if pl_cfg.get("calculate") and pl_id and not client.dry_run:
        try:
            client.post("pricelist_calculate", {}, what="calculate price list", id=pl_id)
            log.info("Triggered Pricefx calculation on price list %s", pl_id)
        except PricefxError as exc:
            run.errors.append(f"calculate failed: {exc}")
            log.error("Calculation trigger failed: %s", exc)

    if pl_cfg.get("submit_for_approval") and pl_id and not client.dry_run:
        try:
            client.post("pricelist_submit", {}, what="submit for approval",
                        tc=TYPE_PRICE_LIST, id=pl_id)
            log.info("Submitted price list %s for approval", pl_id)
        except PricefxError as exc:
            run.errors.append(f"submit failed: {exc}")
            log.error("Submit for approval failed: %s", exc)

    return run


# ------------------------------------------------------------- reporting

def write_preview(runs: dict[str, SegmentRun], unmatched: list[dict], path: str,
                  *, sku_field: str = "sku") -> None:
    """Write the full pricing preview: every row, every stage, every number."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["segment", "sku", "status", "price", "reason", "audit_trail"])
        for name, run in runs.items():
            for res in run.results:
                writer.writerow([
                    name,
                    res.sku,
                    "skipped" if res.skipped else "priced",
                    "" if res.price is None else str(res.price),
                    res.reason,
                    " | ".join(f"{s.stage}:{s.rule} {s.before}->{s.after}" for s in res.steps),
                ])
        for row in unmatched:
            writer.writerow(["(unmatched)", get_field(row, sku_field), "unmatched", "", "", ""])
    log.info("Preview written to %s", path)


def summarise(runs: dict[str, SegmentRun], unmatched: list[dict]) -> str:
    lines = ["", "=" * 68, "RUN SUMMARY", "=" * 68]
    total_priced = total_skipped = total_items = 0
    for name, run in runs.items():
        pl = (run.price_list or {}).get("uniqueName", "-")
        lines.append(
            f"  {name:<24} rows={len(run.rows):<6} priced={len(run.priced):<6} "
            f"skipped={len(run.skipped):<5} items={run.items_created:<6} list={pl}"
        )
        for err in run.errors:
            lines.append(f"      ! {err}")
        total_priced += len(run.priced)
        total_skipped += len(run.skipped)
        total_items += run.items_created
    lines.append("-" * 68)
    lines.append(f"  TOTAL priced={total_priced} skipped={total_skipped} "
                 f"line items={total_items} unmatched={len(unmatched)}")
    if unmatched:
        lines.append("  ! Unmatched SKUs did NOT land on any list. Check the preview CSV.")
    lines.append("=" * 68)
    return "\n".join(lines)
