"""
End-to-end tests for segmentation and publishing, against a fake Pricefx.

FakeClient records every call, so these prove the orchestration without a
tenant: the right SKUs land on the right lists, unmatched rows are surfaced
rather than dropped, and a Pricefx envelope failure is not mistaken for success.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from pfx.client import PricefxClient, PricefxError
from pfx.pricelist import (
    build_segments,
    price_segment,
    publish_segment,
    segment_rows,
    summarise,
    write_preview,
)
from pfx.rules import RuleSet

D = Decimal

ROWS = [
    {"sku": "R-1", "channel": "RETAIL", "costPrice": "20", "brandTier": "GOLD"},
    {"sku": "R-2", "channel": "retail", "costPrice": "150", "brandTier": "SILVER"},
    {"sku": "W-1", "channel": "WHOLESALE", "costPrice": "150", "lifecycle": "ACTIVE"},
    {"sku": "W-2", "channel": "WHOLESALE", "costPrice": "80", "lifecycle": "DISCONTINUED"},
    {"sku": "C-1", "channel": "RETAIL", "costPrice": "60", "lifecycle": "CLEARANCE",
     "stockDays": 400},
    {"sku": "X-1", "channel": "INTERNAL", "costPrice": "10"},          # matches nothing
    {"sku": "B-1", "channel": "RETAIL", "costPrice": ""},              # unpriceable
]

RULES = RuleSet({
    "sku_field": "sku",
    "pipeline": ["base", "tiers", "uplifts", "bounds", "rounding"],
    "base": {"field": "costPrice", "on_missing": "skip"},
    "tiers": {"on": "base", "mode": "percent",
              "bands": [{"name": "small", "max": 100, "value": 45},
                        {"name": "large", "min": 100, "value": 25}]},
    "uplifts": {"mode": "accumulate", "rules": [
        {"name": "gold", "when": {"brandTier": "GOLD"}, "then": {"percent": 8}}]},
    "bounds": {"cost_field": "costPrice", "min_margin_percent": 15},
    "rounding": {"default": {"method": "charm", "increment": 1, "ending": "0.99"}},
})

CFG = {
    "sku_field": "sku",
    "segments": [
        {"name": "CLEARANCE", "label": "Clearance", "filter": {"lifecycle": "CLEARANCE"}},
        {"name": "RETAIL", "label": "Retail", "filter": {"channel": "RETAIL"}},
        {"name": "WHOLESALE", "label": "Wholesale", "filter": {
            "all": [{"channel": "WHOLESALE"}, {"lifecycle": {"ne": "DISCONTINUED"}}]}},
    ],
    "price_list": {"name_template": "PL_{segment}_{YYYYMM}", "status": "DRAFT",
                   "currency": "EUR", "calculate": True, "on_exists": "fail"},
    "line_item": {"price_field": "resultPrice", "segment_field": "attribute10",
                  "audit_field": "attribute11",
                  "fields": {"attribute1": "channel", "attribute2": "=AUTOGEN"}},
}


class FakeClient(PricefxClient):
    """Pricefx stand-in: records calls, returns plausible payloads."""

    def __init__(self, *, existing_price_lists=(), fail_calculate=False):
        super().__init__("https://fake.pricefx.eu", "demo", token="t", auth_mode="token")
        self.calls = []
        self.added = {}
        self.existing = list(existing_price_lists)
        self.fail_calculate = fail_calculate
        self._next_id = 100

    def fetch_all(self, type_code, *, criteria=None, limit=None, **kw):
        self.calls.append(("fetch", type_code, criteria))
        if type_code == "PL" and criteria:
            wanted = criteria["criteria"][0]["value"]
            return [p for p in self.existing if p["uniqueName"] == wanted]
        return []

    def add(self, type_code, record):
        self.calls.append(("add", type_code, record))
        self._next_id += 1
        out = {**record, "id": self._next_id}
        self.added.setdefault(type_code, []).append(out)
        return out

    def add_many(self, type_code, records, *, chunk=200):
        return [self.add(type_code, r) for r in records]

    def post(self, key, payload=None, *, what=None, **fmt):
        self.calls.append(("post", key, fmt))
        if key == "pricelist_calculate" and self.fail_calculate:
            raise PricefxError("calculate: Pricefx returned status -1: logic not found")
        return {}


def build_runs(cfg=CFG, rows=ROWS, multi_match="first"):
    segments = build_segments(cfg, RULES)
    runs, unmatched = segment_rows(rows, segments, multi_match=multi_match)
    for run in runs.values():
        price_segment(run)
    return runs, unmatched


# ------------------------------------------------------------- segmentation

def test_each_sku_lands_on_exactly_one_segment_in_first_match_mode():
    runs, unmatched = build_runs()
    assert [r["sku"] for r in runs["CLEARANCE"].rows] == ["C-1"]
    # C-1 is channel RETAIL too, but CLEARANCE is listed first and wins.
    assert [r["sku"] for r in runs["RETAIL"].rows] == ["R-1", "R-2", "B-1"]
    assert [r["sku"] for r in runs["WHOLESALE"].rows] == ["W-1"]


def test_segment_filter_matching_is_case_insensitive():
    runs, _ = build_runs()
    assert "R-2" in [r["sku"] for r in runs["RETAIL"].rows]  # channel was "retail"


def test_discontinued_wholesale_is_excluded_by_the_ne_filter():
    runs, unmatched = build_runs()
    assert "W-2" not in [r["sku"] for r in runs["WHOLESALE"].rows]
    assert "W-2" in [r["sku"] for r in unmatched]


def test_unmatched_rows_are_reported_not_silently_dropped():
    runs, unmatched = build_runs()
    assert sorted(r["sku"] for r in unmatched) == ["W-2", "X-1"]
    placed = sum(len(r.rows) for r in runs.values())
    assert placed + len(unmatched) == len(ROWS)  # nothing vanishes


def test_multi_match_all_puts_a_row_on_every_matching_list():
    runs, _ = build_runs(multi_match="all")
    assert "C-1" in [r["sku"] for r in runs["CLEARANCE"].rows]
    assert "C-1" in [r["sku"] for r in runs["RETAIL"].rows]


def test_unpriceable_row_is_skipped_without_killing_its_segment():
    runs, _ = build_runs()
    retail = runs["RETAIL"]
    assert [r.sku for r in retail.skipped] == ["B-1"]
    assert [r.sku for r in retail.priced] == ["R-1", "R-2"]


# ----------------------------------------------------------------- publish

def test_publish_creates_one_list_per_segment_with_the_templated_name():
    runs, _ = build_runs()
    client = FakeClient()
    for run in runs.values():
        publish_segment(client, run, cfg=CFG, today=date(2026, 8, 16))
    names = sorted(pl["uniqueName"] for pl in client.added["PL"])
    assert names == ["PL_CLEARANCE_202608", "PL_RETAIL_202608", "PL_WHOLESALE_202608"]


def test_line_items_carry_price_segment_audit_and_mapped_fields():
    runs, _ = build_runs()
    client = FakeClient()
    publish_segment(client, runs["RETAIL"], cfg=CFG, today=date(2026, 8, 16))

    items = client.added["PLI"]
    assert [i["sku"] for i in items] == ["R-1", "R-2"]
    r1 = items[0]
    # 20 -> +45% = 29 -> +8% gold = 31.32 -> charm -> 31.99
    assert r1["resultPrice"] == 31.99
    assert r1["attribute10"] == "Retail"        # segment stamp
    assert r1["attribute1"] == "RETAIL"         # mapped from the row
    assert r1["attribute2"] == "AUTOGEN"        # literal
    assert "tiers:small" in r1["attribute11"]   # audit trail
    assert r1["priceListId"] == client.added["PL"][0]["id"]


def test_skipped_rows_never_become_line_items():
    runs, _ = build_runs()
    client = FakeClient()
    publish_segment(client, runs["RETAIL"], cfg=CFG, today=date(2026, 8, 16))
    assert "B-1" not in [i["sku"] for i in client.added["PLI"]]


def test_calculate_is_triggered_on_the_new_list():
    runs, _ = build_runs()
    client = FakeClient()
    publish_segment(client, runs["RETAIL"], cfg=CFG, today=date(2026, 8, 16))
    calc = [c for c in client.calls if c[0] == "post" and c[1] == "pricelist_calculate"]
    assert len(calc) == 1
    assert calc[0][2]["id"] == client.added["PL"][0]["id"]


def test_a_failed_calculate_is_recorded_as_an_error_not_swallowed():
    runs, _ = build_runs()
    client = FakeClient(fail_calculate=True)
    run = publish_segment(client, runs["RETAIL"], cfg=CFG, today=date(2026, 8, 16))
    assert run.errors and "calculate failed" in run.errors[0]


def test_existing_price_list_aborts_by_default():
    runs, _ = build_runs()
    client = FakeClient(existing_price_lists=[
        {"id": 7, "uniqueName": "PL_RETAIL_202608"}])
    with pytest.raises(PricefxError, match="already exists"):
        publish_segment(client, runs["RETAIL"], cfg=CFG, today=date(2026, 8, 16))


def test_on_exists_suffix_picks_a_free_name():
    cfg = {**CFG, "price_list": {**CFG["price_list"], "on_exists": "suffix"}}
    runs, _ = build_runs(cfg)
    client = FakeClient(existing_price_lists=[
        {"id": 7, "uniqueName": "PL_RETAIL_202608"}])
    publish_segment(client, runs["RETAIL"], cfg=cfg, today=date(2026, 8, 16))
    assert client.added["PL"][0]["uniqueName"] == "PL_RETAIL_202608_2"


def test_on_exists_reuse_adds_items_to_the_existing_list():
    cfg = {**CFG, "price_list": {**CFG["price_list"], "on_exists": "reuse"}}
    runs, _ = build_runs(cfg)
    client = FakeClient(existing_price_lists=[
        {"id": 7, "uniqueName": "PL_RETAIL_202608"}])
    publish_segment(client, runs["RETAIL"], cfg=cfg, today=date(2026, 8, 16))
    assert "PL" not in client.added                    # no new header
    assert all(i["priceListId"] == 7 for i in client.added["PLI"])


# ------------------------------------------------------------- envelope safety

class _Resp:
    def __init__(self, body, status_code=200, ctype="application/json"):
        self._body = body
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = {"content-type": ctype}
        self.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


def test_http_200_carrying_a_negative_status_is_treated_as_a_failure():
    c = PricefxClient("https://x", "p", token="t")
    with pytest.raises(PricefxError, match="status -3"):
        c._unwrap(_Resp({"response": {"status": -3, "data": "logic error"}}), what="add PL")


def test_http_200_with_status_zero_returns_the_data():
    c = PricefxClient("https://x", "p", token="t")
    out = c._unwrap(_Resp({"response": {"status": 0, "data": [{"id": 1}]}}), what="fetch")
    assert out == [{"id": 1}]


def test_an_html_error_page_is_reported_clearly_not_as_json_garbage():
    c = PricefxClient("https://x", "p", token="t")
    with pytest.raises(PricefxError, match="expected JSON"):
        c._unwrap(_Resp("<html>502 Bad Gateway</html>", 502, "text/html"), what="fetch")


def test_401_says_authentication_not_something_cryptic():
    c = PricefxClient("https://x", "p", token="t")
    with pytest.raises(PricefxError, match="authentication rejected"):
        c._unwrap(_Resp({}, 401), what="fetch")


# ------------------------------------------------------------------ paging

def test_fetch_pages_past_the_page_size_and_stops_on_a_short_page():
    """A full page means 'there is more', not 'that is all'."""
    c = PricefxClient("https://x", "p", token="t", page_size=2)
    pages = [[{"id": 1}, {"id": 2}], [{"id": 3}, {"id": 4}], [{"id": 5}]]
    seen = []

    def fake_post(key, payload=None, *, what=None, **fmt):
        seen.append((fmt["start"], fmt["end"]))
        return pages.pop(0) if pages else []

    c.post = fake_post
    assert [r["id"] for r in c.fetch("P")] == [1, 2, 3, 4, 5]
    assert seen == [(0, 1), (2, 3), (4, 5)]


# ----------------------------------------------------------------- reporting

def test_preview_csv_lists_priced_skipped_and_unmatched_rows(tmp_path):
    runs, unmatched = build_runs()
    path = tmp_path / "preview.csv"
    write_preview(runs, unmatched, str(path))
    text = path.read_text()
    assert "R-1" in text and "priced" in text
    assert "B-1" in text and "skipped" in text
    assert "X-1" in text and "unmatched" in text


def test_summary_flags_unmatched_rows():
    runs, unmatched = build_runs()
    out = summarise(runs, unmatched)
    assert "unmatched=2" in out
    assert "did NOT land on any list" in out
