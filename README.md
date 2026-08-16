# Pricefx Price List Automation

A one-shot runner that builds ready-to-publish Pricefx price lists: it pulls
product/cost data, splits SKUs into segment lists by category, applies
rule-driven price calculations, creates the price lists and line items, triggers
the Pricefx calculation, and exits. No daemon, no auth refresh loop.

```
source data ──> segment ──> price (rules) ──> PriceList + PriceListItems ──> calculate
   Pricefx P        by            declarative        Pricefx REST              Pricefx
   or CSV        category           YAML rules
```

---

## Status

**The pricing engine and orchestration are complete and tested** — 63 unit and
integration tests covering the rule engine, segmentation, line-item mapping,
paging, and error handling. Run `python3 -m pytest tests -q`.

**The tenant-facing bits are written but not yet run against a live Pricefx
partition** — I do not have tenant access at the time of writing. Specifically:
the exact operation names in `ENDPOINTS` (`pfx/client.py`), the login token
field, and your tenant's custom field names all need one verification pass.
That is what `probe` is for — see *First run* below. Every one of those is a
config value, not a code change.

The `groovy/PriceListLogic.groovy` logic is provided as installable source; it
has not been executed inside a tenant either.

---

## Install

```bash
python3 -m pip install -r requirements.txt      # requests, PyYAML
```

Python 3.10+. No other dependencies — this is deliberately small enough to drop
onto a cron box.

## Configure

```bash
cp config.example.yaml config.yaml
cp rules.example.yaml  rules.yaml
export PFX_USER=integration.user
export PFX_PASSWORD='...'
```

Credentials are read from the environment via `${VAR}` placeholders, so
`config.yaml` stays safe to commit. Nothing secret is ever written to a file by
this tool.

## First run

Do these in order. Do not skip to step 4.

```bash
# 1. Does the tenant answer, and what do its objects actually look like?
python3 -m pfx.cli probe --config config.yaml

# 2. Price everything, create nothing. Writes a per-SKU preview CSV.
python3 -m pfx.cli run --config config.yaml --dry-run --preview preview.csv

# 3. Explain a single SKU's number, stage by stage.
python3 -m pfx.cli test --config config.yaml --csv data/products.csv --sku A-1008

# 4. For real. Creates price lists in the partition named in config.yaml.
python3 -m pfx.cli run --config config.yaml
```

`preview.csv` has one row per SKU with the full calculation trail. Sign that off
before step 4 — creating price lists on a live partition is not trivially
undone.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Everything priced and published |
| 1 | Partial — some rows skipped or unmatched (still published the rest) |
| 2 | Failed — config error, auth failure, or a Pricefx error |

Non-zero on partial is deliberate. Under cron, exit 0 means "nobody looks", and
4,000 silently skipped SKUs is exactly the failure you want to hear about.

---

## The rules file

`rules.yaml` is where your pricing logic lives. It is declarative — nobody needs
to touch Python to change a band or add an uplift.

Five stages, run in order. Delete a stage to skip it.

| Stage | Does |
|-------|------|
| `base` | Picks the starting number, with fallback fields |
| `tiers` | Band-based markup driven by any numeric field |
| `uplifts` | if/then adjustments, accumulating or first-match |
| `bounds` | Margin floor, absolute floors/ceilings |
| `rounding` | Charm pricing, price-point endings, increments |

### Condition syntax

Used by `when:` in rules and `filter:` in segments — same syntax in both places.

```yaml
{ brandTier: GOLD }                       # equals, case-insensitive
{ costPrice: { gt: 100 } }                # gt gte lt lte
{ category: { in: [PPE, CHEMICALS] } }    # in / not_in
{ description: { contains: "PRO" } }      # contains startswith endswith regex
{ contractPrice: { exists: true } }       # exists / is_null
{ all: [ ... ] }  { any: [ ... ] }  { not: { ... } }
{ "attributes.brandTier": GOLD }          # dotted access into nested fields
```

### Rounding methods

| Method | Example | 99.40 becomes |
|--------|---------|---------------|
| `nearest` / `up` / `down` | `{method: nearest, increment: 0.05}` | 99.40 |
| `decimals` | `{method: decimals, decimals: 2}` | 99.40 |
| `charm` | `{method: charm, increment: 1, ending: 0.99}` | 99.99 |
| `charm` + `direction: down` | never rounds up | 98.99 |
| `endings` | `{method: endings, unit: 1, endings: ["0.00","0.49","0.99"]}` | 99.49 |

### Taking a value from the row

A leading `@` reads the operand off the source row instead of using a literal:

```yaml
- name: contract-price-wins
  when: { contractPrice: { exists: true } }
  then: { set: "@contractPrice" }
```

If the row has no `contractPrice`, the rule simply does not apply — it is not an
error.

### Extending it

* **New rounding method** — add a branch to `apply_rounding()` in `pfx/rules.py`,
  and the mirror branch in `applyRounding()` in the Groovy logic.
* **New operator** — add one entry to the `OPERATORS` dict in `pfx/rules.py`.
* **New data source** — add a branch to `load_source()` in `pfx/pricelist.py`.
* **New stage** — add it to `PIPELINE_STAGES` and write a `_stage_x` method.

Every one of those has a test next to it in `tests/` showing the shape.

---

## Two traps this code already handles

These both cost real money when they slip through, so they are called out rather
than buried:

**1. YAML parses `on:` as the boolean `true`.** In YAML 1.1 (what PyYAML
implements) the bare words `on`, `off`, `yes`, `no` are booleans. A rules file
saying `on: annualVolume` silently becomes `{True: 'annualVolume'}`, the tier
driver is ignored, and every SKU gets banded on the wrong number — with no error
anywhere. `normalize_yaml_keys()` maps those keys back, and `_norm()` folds
boolean *values* onto the `Y`/`N` your data actually contains. Prefer `driver:`
over `on:` anyway. Test: `test_yaml_parses_on_as_a_boolean_key_and_we_recover_it`.

**2. Pricefx returns HTTP 200 on failure.** The real status lives in
`response.status` inside the envelope — `0` is success, negative is failure.
Checking `resp.ok` alone will cheerfully report a failed price list creation as
a success. `_unwrap()` reads the envelope every time. Test:
`test_http_200_carrying_a_negative_status_is_treated_as_a_failure`.

Two more worth knowing about:

* **Segment order matters.** In `multi_match: first`, list the narrow segments
  first. A clearance SKU is usually also a retail SKU — put `RETAIL` first and
  your `CLEARANCE` list comes out empty. The run summary prints per-segment row
  counts so you catch it immediately.
* **A page size is not a total.** Pricefx caps rows per fetch. A page that comes
  back full means "there is more", not "that is all". `fetch()` pages until a
  short page arrives.

---

## Endpoints touched

All relative to `{base_url}/pricefx/{partition}/`. Every path is overridable via
the `endpoints:` block in `config.yaml` — operation names have drifted between
Pricefx versions, and `probe` tells you which ones answer on your partition.

| Purpose | Method | Path | When |
|---------|--------|------|------|
| Login | POST | `user.login` | Once at start (token mode) |
| Logout | POST | `user.logout` | Once at exit |
| Fetch products | POST | `fetch/P/{start}/{end}` | Sourcing, paged |
| Find existing list | POST | `fetch/PL/{start}/{end}` | Before create, per segment |
| Create price list | POST | `add/PL` | Once per segment |
| Add line items | POST | `add/PLI` | Chunked, 200 per request |
| Calculate | POST | `pricelistmanager.calculate/{id}` | If `price_list.calculate` |
| Submit for approval | POST | `workflow.submitforapproval/PL/{id}` | If `submit_for_approval` |

Object type codes: `P` Product, `PL` PriceList, `PLI` PriceListItem,
`PX` ProductExtension.

Auth is either a bearer token from `user.login`, or
`Authorization: Basic base64(partition/user:password)` — `auth.mode` in the
config picks. One login, one logout, clean exit.

---

## Native Pricefx logic (Groovy)

`groovy/PriceListLogic.groovy` is the same calculation as a PriceList
Calculation logic, split into named elements (`BaseCost`, `TierMarkup`,
`Uplifts`, `Bounds`, `ResultPrice`, `AuditTrail`).

There are two places the maths can live and they are **not** equivalent:

* **In the runner** (default). Fast to iterate, fully unit-tested, works against
  any feed. But a user who edits a line item in the Pricefx UI and hits
  recalculate does *not* get your logic — the price is just data.
* **In the tenant** (this Groovy). The UI recalculate button works, results are
  reproducible inside the platform, and analysts see each element's
  contribution. Slower to iterate.

Recommended: Groovy for the maths, runner for sourcing/segmenting/orchestration.
Both read the **same** rules JSON so they cannot silently disagree:

```bash
python3 -m pfx.cli export-rules --rules rules.yaml --output rules.json
```

Paste that into the `PricingRules` company parameter table, then set
`price_list.calculation_logic` in `config.yaml`. Install steps are in the header
comment of the Groovy file.

---

## Scheduling

```cron
# 06:15 on the 1st of each month
15 6 1 * * cd /opt/pfx && PFX_USER=svc PFX_PASSWORD="$(cat /etc/pfx.pw)" \
  /usr/bin/python3 -m pfx.cli run --config config.yaml \
  --preview /var/log/pfx/preview-$(date +\%Y\%m).csv >> /var/log/pfx/run.log 2>&1
```

Keep the preview CSV — it is your audit record of what was priced and why.

---

## Layout

```
pfx/rules.py       rule engine (pure, no network) — the part you will edit
pfx/client.py      Pricefx REST client, envelope handling, paging, auth
pfx/pricelist.py   sourcing, segmenting, price list + line item creation
pfx/cli.py         run / test / probe / export-rules
groovy/            native PriceList Calculation logic
tests/             63 tests — run these after any change
data/              sample product feed for the dry run
config.example.yaml, rules.example.yaml
```

## Tests

```bash
python3 -m pytest tests -q
```

`tests/test_pipeline.py` runs the whole flow against a `FakeClient` that records
every call, so segmentation, field mapping, name templating, `on_exists`
handling and error propagation are all covered without a tenant.
