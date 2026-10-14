"""Requested root filters must survive an HTTP redirect before membership credit."""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database
from car_market.fetch import Response
from car_market.parse import with_query

ROOT = Path(__file__).parent / "fixtures" / "mercedes_autoscout"
CAPTURES = {
    row["file"]: row for row in json.loads((ROOT / "search-manifest.json").read_text())
}


def collect(tmp_path, country, changed_filter=None):
    first = CAPTURES[country.lower() + "-search-page1.json"]
    requested_root = (
        with_query(first["url"], **changed_filter) if changed_filter else first["url"]
    )
    source = "autoscout24_de" if country == "DE" else "autoscout24"
    query = {
        "source": source,
        "key": "requested",
        "url": requested_root,
        "pagination": "mercedes_autoscout_next",
        "coverage": "enumerate",
        "infer_missing": False,
    }
    config = {
        "schema_version": 1,
        "scope": {
            "country": country,
            "model_profile": "configured_vehicle",
            "selector": {"make": "Mercedes-Benz", "model_terms": [["C180"]]},
        },
        "queries": [query],
        "workers": 1,
        "max_pages": 3,
        "detail_limit": 0,
    }
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, config)
    requests = []

    def get(url, refresh=False):
        number = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        case = CAPTURES[f"{country.lower()}-search-page{number}.json"]
        final_url = case["url"]
        # Real native response and original clock; only redirect routing is controlled.
        requests.append(url)
        return Response(
            url, final_url, 200, (ROOT / case["file"]).read_text(), case["captured_at"]
        )

    collector.fetcher = SimpleNamespace(get=get)
    run = db.start_run()
    collector.collect_query(run, query)
    health = dict(
        db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    )
    return db, health, requests


@pytest.mark.parametrize("country", ["NL", "DE"])
@pytest.mark.parametrize(
    "changed_filter", [{"fregfrom": "2020"}, {"cat": "ma70mo2052"}]
)
def test_redirect_cannot_credit_a_different_inventory_query(
    tmp_path, country, changed_filter
):
    db, health, requests = collect(tmp_path, country, changed_filter)
    assert health["status"] not in {"complete", "zero_results"}
    assert health["received_count"] == 0 and health["expected_count"] is None
    assert db.conn.execute("SELECT count(*) FROM query_membership").fetchone()[0] == 0
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    evidence = db.conn.execute(
        "SELECT url,final_url,raw_path,fetched_at FROM fetches"
    ).fetchall()
    assert len(evidence) == 1 and evidence[0]["raw_path"]
    assert evidence[0]["url"] != evidence[0]["final_url"]
    assert evidence[0]["fetched_at"].startswith("2026-10-08T15:25:")
    assert len(requests) == 1
    db.conn.close()


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_equivalent_native_search_urls_keep_memberships(tmp_path, country):
    db, health, _requests = collect(tmp_path, country)
    assert health["status"] == "complete"
    assert health["received_count"] == (33 if country == "NL" else 44)
    assert (
        db.conn.execute("SELECT count(*) FROM query_membership").fetchone()[0]
        == health["received_count"]
    )
    db.conn.close()


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_redirect_validation_exception_retains_raw_evidence(
    tmp_path, country, monkeypatch
):
    def fail(*args):
        raise ValueError("controlled_native_query_parse_failure")

    monkeypatch.setattr(ConfiguredCollector, "_parse", fail)
    db, health, requests = collect(tmp_path, country, {"fregfrom": "2020"})
    assert health["status"] == "error" and len(requests) == 1
    proof = db.conn.execute("SELECT fetched_at,raw_path FROM fetches").fetchall()
    assert len(proof) == 1 and proof[0]["raw_path"]
    assert proof[0]["fetched_at"].startswith("2026-10-08T15:25:")
    assert db.conn.execute("SELECT count(*) FROM query_membership").fetchone()[0] == 0
    db.conn.close()
