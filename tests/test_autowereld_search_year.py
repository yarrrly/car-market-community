"""Regress the month/year fields in a retained publisher search response."""

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from car_market.collect import parse_market_page
from car_market.db import Database
from car_market.dom import autowereld_build_year
from car_market.quality import report

FIXTURE = Path(__file__).parent / "fixtures/autowereld_search"


def test_native_search_uses_calendar_year_without_changing_publication_ids():
    meta = json.loads((FIXTURE / "provenance.json").read_text())
    compressed = (FIXTURE / meta["fixture"]).read_bytes()
    assert hashlib.sha256(compressed).hexdigest() == meta["fixture_sha256"]
    body = gzip.decompress(compressed)
    assert hashlib.sha256(body).hexdigest() == meta["content_hash"]
    page = parse_market_page("autowereld", body.decode(), meta["final_url"])
    assert len(page.listings) == 15
    assert all(item.source == "autowereld" for item in page.listings)
    expected = {
        "43844184": 2017,
        "43838959": 2015,
        "43832704": 2016,
        "43824166": 2018,
        "43817887": 2014,
    }
    actual = {
        key: item.year
        for item in page.listings
        for key in expected
        if key + "/details.html" in item.url
    }
    assert actual == expected
    assert all(2014 <= item.year <= 2026 for item in page.listings)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("08-2016", 2016),
        ("2017", 2017),
        ("7/2015", 2015),
        ("", None),
        ("12", None),
        ("00-2018", None),
        ("13-2020", None),
        ("2014 / 2015", None),
    ],
)
def test_build_year_refuses_incomplete_or_ambiguous_dates(value, expected):
    assert autowereld_build_year(value) == expected


def test_quality_exposes_historical_month_without_rewriting_the_observation(tmp_path):
    meta = json.loads((FIXTURE / "provenance.json").read_text())
    body = gzip.decompress((FIXTURE / meta["fixture"]).read_bytes()).decode()
    page = parse_market_page("autowereld", body, meta["final_url"])
    item = next(x for x in page.listings if "43844184/details.html" in x.url)
    item.year = 8  # The prior parser stored the native build month as a year.
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    db.observe(run, item, meta["fetched_at"])
    before = [tuple(x) for x in db.conn.execute("SELECT * FROM observations")]
    quality = report(db.conn, {"scope": {"country": "NL"}, "queries": []})
    source = next(x for x in quality["sources"] if x["source"] == "autowereld")
    assert source["invalid_calendar_year_ads"] == 1
    assert source["fields"]["year"]["present"] == 1
    assert source["examples_invalid_calendar_year"][0]["year"] == 8
    assert [tuple(x) for x in db.conn.execute("SELECT * FROM observations")] == before
    db.conn.close()
