"""Native routes and response-only facets cannot certify another inventory."""

import json
from types import SimpleNamespace

import pytest

from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database
from car_market.fetch import Response
from test_autoscout_query_binding import CASES, captured


def collect(tmp_path, case, doc, requested=None, final=None):
    source = "autoscout24_de" if case["country"] == "DE" else "autoscout24"
    url = requested or case["url"]
    query = {
        "source": source,
        "key": "native",
        "url": url,
        "pagination": "mercedes_autoscout_next",
        "coverage": "enumerate",
        "infer_missing": False,
    }
    cfg = {
        "schema_version": 1,
        "scope": {
            "country": case["country"],
            "model_profile": "configured_vehicle",
            "selector": {"make": "Volkswagen", "model_terms": [["Golf"]]},
        },
        "queries": [query],
        "max_pages": 1,
        "detail_limit": 0,
        "workers": 1,
    }
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, cfg)
    body = json.dumps(doc)
    collector.fetcher = SimpleNamespace(
        get=lambda requested, refresh=False: Response(
            requested, final or url, 200, body, case["observed_at"]
        )
    )
    run = db.start_run(timestamp=case["observed_at"])
    collector.collect_query(run, query)
    health = dict(
        db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    )
    return db, health


@pytest.mark.parametrize("country", ["NL", "DE"])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        "different_model_route",
        "contradictory_envelope_slug",
        "added_fuel",
        "unknown_inventory_axis",
    ],
)
def test_native_inventory_contradictions_remain_uncredited(
    tmp_path, country, empty, fault
):
    case = next(row for row in CASES if row["case"] == country.lower() + "-golf")
    doc = captured(case)
    state = doc["props"]["pageProps"]
    requested = final = case["url"]
    if fault == "different_model_route":
        corolla = next(
            row for row in CASES if row["case"] == country.lower() + "-corolla"
        )
        requested = final = corolla["url"]
        state["pagePath"] = captured(corolla)["props"]["pageProps"]["pagePath"]
    elif fault == "contradictory_envelope_slug":
        doc["query"]["slug"] = ["toyota", "corolla"]
    else:
        key = "fuel" if fault == "added_fuel" else "unknown_inventory_axis"
        state["pageQuery"][key] = "B"
        doc["query"][key] = "B"
        final += "&" + key + "=B"
        state["pagePath"] += "&" + key + "=B"
    if empty:
        state.update(listings=[], numberOfResults=0, numberOfPages=0)
    db, health = collect(tmp_path, case, doc, requested, final)
    assert health["status"] not in {"complete", "zero_results"}
    assert health["expected_count"] is None and health["received_count"] == 0
    assert db.conn.execute("SELECT count(*) FROM query_membership").fetchone()[0] == 0
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    proof = db.conn.execute("SELECT fetched_at,raw_path FROM fetches").fetchall()
    assert len(proof) == 1 and proof[0]["raw_path"]
    assert proof[0]["fetched_at"] == case["observed_at"]
    db.conn.close()


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_bound_native_empty_still_proves_zero_results(tmp_path, country):
    case = next(row for row in CASES if row["case"] == country.lower() + "-golf")
    doc = captured(case)
    doc["props"]["pageProps"].update(listings=[], numberOfResults=0, numberOfPages=0)
    db, health = collect(tmp_path, case, doc)
    assert health["status"] == "zero_results" and health["expected_count"] == 0
    db.conn.close()
