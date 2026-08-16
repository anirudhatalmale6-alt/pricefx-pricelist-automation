"""
Command line entry point.

  python3 -m pfx.cli run   --config config.yaml --dry-run
  python3 -m pfx.cli run   --config config.yaml
  python3 -m pfx.cli probe --config config.yaml
  python3 -m pfx.cli test  --config config.yaml --sku ABC-123

Exit codes: 0 ok, 1 partial (some rows skipped / non-fatal errors), 2 failed.
Non-zero on partial matters if you schedule this -- cron treats 0 as "fine" and
you will never hear about 4,000 skipped SKUs otherwise.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date

from .client import PricefxClient, PricefxError
from .pricelist import (
    build_segments,
    load_source,
    price_segment,
    publish_segment,
    segment_rows,
    summarise,
    write_preview,
)
from .rules import RuleSet, load_ruleset

log = logging.getLogger("pfx")

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(obj, missing: list[str] | None = None):
    """Expand ${VAR} / ${VAR:-default} anywhere in the config.

    Credentials belong in the environment, not in a YAML file that ends up in
    git. The config ships with ${PFX_PASSWORD} placeholders for exactly this.

    An unset variable becomes None and is recorded in `missing` rather than
    raising here: offline commands (`test`) read the same config file but never
    touch the tenant, and should not demand credentials they will not use. The
    failure surfaces at login, where it means something.
    """
    if isinstance(obj, str):
        holes: list[str] = []

        def sub(m):
            val = os.environ.get(m.group(1))
            if val is None:
                if m.group(2) is not None:
                    return m.group(2)
                holes.append(m.group(1))
                if missing is not None:
                    missing.append(m.group(1))
                return ""
            return val

        out = ENV_PATTERN.sub(sub, obj)
        # A value that was ONLY a placeholder becomes None, so downstream
        # `if not username` checks fire instead of seeing an empty string.
        return None if (holes and out == "") else out
    if isinstance(obj, dict):
        return {k: expand_env(v, missing) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v, missing) for v in obj]
    return obj


def load_config(path: str) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    from .rules import normalize_yaml_keys
    # Segment filters use the same condition syntax as the rules file, so they
    # need the same protection from YAML parsing `on:`/`no:` as booleans.
    raw = normalize_yaml_keys(raw)
    missing: list[str] = []
    cfg = expand_env(raw, missing)
    if missing:
        log.debug("unset environment variables in config: %s", ", ".join(sorted(set(missing))))
    cfg["_missing_env"] = sorted(set(missing))
    return cfg


def make_client(cfg: dict, *, dry_run: bool) -> PricefxClient:
    tenant = cfg.get("tenant") or {}
    auth = cfg.get("auth") or {}
    for key in ("base_url", "partition"):
        if not tenant.get(key):
            raise PricefxError(f"tenant.{key} is required in the config")
    if cfg.get("_missing_env") and not (auth.get("token") or auth.get("password")):
        raise PricefxError(
            "credentials are unset. Export these first: "
            + ", ".join(cfg["_missing_env"])
        )
    return PricefxClient(
        base_url=tenant["base_url"],
        partition=tenant["partition"],
        username=auth.get("username"),
        password=auth.get("password"),
        token=auth.get("token"),
        auth_mode=auth.get("mode", "token"),
        timeout=int(tenant.get("timeout", 60)),
        page_size=int(tenant.get("page_size", 500)),
        endpoints=cfg.get("endpoints"),
        verify_tls=bool(tenant.get("verify_tls", True)),
        dry_run=dry_run,
    )


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )


# ------------------------------------------------------------------ commands

def cmd_run(args) -> int:
    cfg = load_config(args.config)
    dry = args.dry_run
    rules_path = args.rules or cfg.get("rules_file")
    if not rules_path:
        raise PricefxError("no rules file: pass --rules or set rules_file in the config")
    default_rules: RuleSet = load_ruleset(rules_path)
    segments = build_segments(cfg, default_rules)
    today = date.fromisoformat(args.date) if args.date else date.today()

    source_needs_tenant = str((cfg.get("source") or {}).get("type", "pricefx")).lower() == "pricefx"
    client = make_client(cfg, dry_run=dry) if (source_needs_tenant or not dry) else None

    def _work(c: PricefxClient | None) -> int:
        rows = load_source(c, cfg)
        if not rows:
            log.error("Source returned 0 rows -- nothing to price. Check source.criteria.")
            return 2
        runs, unmatched = segment_rows(
            rows, segments,
            multi_match=str(cfg.get("multi_match", "first")).lower(),
        )
        for run in runs.values():
            price_segment(run)

        if args.preview:
            write_preview(runs, unmatched, args.preview,
                          sku_field=cfg.get("sku_field", "sku"))

        if dry:
            log.info("Dry run: no objects created in Pricefx.")
        else:
            for run in runs.values():
                publish_segment(c, run, cfg=cfg, today=today)

        print(summarise(runs, unmatched))
        skipped = sum(len(r.skipped) for r in runs.values())
        errors = sum(len(r.errors) for r in runs.values())
        if errors:
            return 2
        return 1 if (skipped or unmatched) else 0

    if client is None:
        return _work(None)
    with client as c:
        return _work(c)


def cmd_test(args) -> int:
    """Price a single row from a JSON blob or a CSV, and print the full trail.

    This is the fastest way for your team to check "why did SKU X come out at
    that number" without running anything against the tenant.
    """
    cfg = load_config(args.config) if args.config else {}
    rules = load_ruleset(args.rules or cfg.get("rules_file"))

    if args.json:
        row = json.loads(args.json)
    elif args.csv and args.sku:
        from .pricelist import load_from_csv
        sku_field = cfg.get("sku_field", "sku")
        matches = [r for r in load_from_csv(args.csv) if str(r.get(sku_field)) == args.sku]
        if not matches:
            log.error("SKU %s not found in %s", args.sku, args.csv)
            return 2
        row = matches[0]
    else:
        log.error("pass --json '<row>' or --csv <file> --sku <sku>")
        return 2

    result = rules.price_row(row)
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if not result.skipped else 1


def cmd_export_rules(args) -> int:
    """Emit the rule set as JSON for the Groovy logic's PricingRules parameter.

    Both implementations then read the same rules, so the Python runner and the
    in-tenant Groovy logic cannot silently drift apart.
    """
    import yaml
    rules_path = args.rules or (load_config(args.config) if args.config else {}).get("rules_file")
    if not rules_path:
        raise PricefxError("pass --rules <file> or a --config with rules_file set")
    with open(rules_path, "r", encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    RuleSet(spec)  # validate before emitting -- never ship a broken ruleset
    out = json.dumps(spec, indent=2 if args.pretty else None, sort_keys=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        log.info("Wrote %s (%d bytes)", args.output, len(out))
    else:
        print(out)
    return 0


def cmd_probe(args) -> int:
    """Verify each configured endpoint against the live tenant.

    Operation names have drifted between Pricefx versions and tenants. Rather
    than guessing and failing mid-run, this tells you exactly which endpoints
    answer on YOUR partition, so any that differ can be overridden in config.
    """
    cfg = load_config(args.config)
    with make_client(cfg, dry_run=True) as client:
        print(f"\nTenant : {client.base_url}")
        print(f"Partition: {client.partition}")
        print(f"Auth   : {client.auth_mode}  -> login OK\n")
        checks = [
            ("fetch Product (P)", lambda: client.fetch_all("P", limit=1)),
            ("fetch PriceList (PL)", lambda: client.fetch_all("PL", limit=1)),
            ("fetch PriceListItem (PLI)", lambda: client.fetch_all("PLI", limit=1)),
        ]
        failed = 0
        for label, fn in checks:
            try:
                rows = fn()
                sample = sorted(rows[0])[:12] if rows else []
                print(f"  OK    {label:<28} rows={len(rows)} fields={sample}")
            except PricefxError as exc:
                failed += 1
                print(f"  FAIL  {label:<28} {exc}")
        print()
        for key, path in sorted(client.endpoints.items()):
            print(f"  endpoint {key:<22} -> {path}")
        print()
        return 1 if failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pfx", description="Pricefx price list automation")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="build and publish price lists")
    p_run.add_argument("--config", required=True)
    p_run.add_argument("--rules")
    p_run.add_argument("--dry-run", action="store_true",
                       help="price everything, create nothing")
    p_run.add_argument("--preview", help="write a per-SKU preview CSV to this path")
    p_run.add_argument("--date", help="target date, YYYY-MM-DD (default: today)")
    p_run.set_defaults(func=cmd_run)

    p_test = sub.add_parser("test", help="price one row and print the audit trail")
    p_test.add_argument("--config")
    p_test.add_argument("--rules")
    p_test.add_argument("--json")
    p_test.add_argument("--csv")
    p_test.add_argument("--sku")
    p_test.set_defaults(func=cmd_test)

    p_exp = sub.add_parser("export-rules",
                           help="emit the rules as JSON for the Groovy logic")
    p_exp.add_argument("--config")
    p_exp.add_argument("--rules")
    p_exp.add_argument("--output")
    p_exp.add_argument("--pretty", action="store_true", default=True)
    p_exp.set_defaults(func=cmd_export_rules)

    p_probe = sub.add_parser("probe", help="check auth and endpoints against the tenant")
    p_probe.add_argument("--config", required=True)
    p_probe.set_defaults(func=cmd_probe)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except PricefxError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("interrupted")
        return 2


if __name__ == "__main__":
    sys.exit(main())
