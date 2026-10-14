"""Retained native search filters must belong to the requested generic query."""

import copy
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database
from car_market.fetch import Response
from car_market.mercedes_autoscout import (
    mercedes_autoscout_page_url,
    parse_mercedes_autoscout,
)
from car_market.parse import with_query
from car_market.search_profile import GENERIC_PROFILE, search_context

ROOT = Path(__file__).parent / "fixtures"
CAPTURES = ROOT / "autoscout_query_binding"
CASES = json.loads((CAPTURES / "manifest.json").read_text())
SEARCH = {
    row["file"]: row
    for row in json.loads(
        (ROOT / "mercedes_autoscout/search-manifest.json").read_text()
    )
}


def scope(country, make="Mercedes-Benz", model="C180"):
    return {
        "country": country,
        "model_profile": GENERIC_PROFILE,
        "selector": {"make": make, "model_terms": [[model]]},
    }


def captured(case):
    blob = (CAPTURES / case["file"]).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == case["fixture_sha256"]
    body = gzip.decompress(blob)
    assert hashlib.sha256(body).hexdigest() == case["decoded_sha256"]
    return json.loads(body)


def parse(doc, url, country):
    with search_context(scope(country)):
        return parse_mercedes_autoscout(json.dumps(doc), url, country)


def run_query(tmp_path, country, url=None, mutate=None):
    first = SEARCH[country.lower() + "-search-page1.json"]
    query = {
        "source": "autoscout24_de" if country == "DE" else "autoscout24",
        "key": "wanted",
        "url": url or first["url"],
        "pagination": "mercedes_autoscout_next",
        "coverage": "enumerate",
        "infer_missing": False,
    }
    cfg = {
        "schema_version": 1,
        "scope": scope(country),
        "queries": [query],
        "max_pages": 3,
        "workers": 1,
        "detail_limit": 0,
    }
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, cfg)
    requested = []

    def get(url, refresh=False):
        number = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
        case = SEARCH[f"{country.lower()}-search-page{number}.json"]
        doc = json.loads((ROOT / "mercedes_autoscout" / case["file"]).read_text())
        if mutate:
            mutate(
                doc["props"]["pageProps"] if "props" in doc else doc["pageProps"],
                number,
            )
        requested.append(url)
        return Response(url, url, 200, json.dumps(doc), case["captured_at"])

    collector.fetcher = SimpleNamespace(get=get)
    run = db.start_run()
    collector.collect_query(run, query)
    health = dict(
        db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    )
    return db, collector, health, requested


@pytest.mark.parametrize("country", ["NL", "DE"])
@pytest.mark.parametrize("filter,value", [("fregfrom", "2020"), ("cat", "ma70mo2052")])
def test_normal_collector_does_not_credit_ignored_root_filters(
    tmp_path, country, filter, value
):
    first = SEARCH[country.lower() + "-search-page1.json"]
    db, collector, health, requested = run_query(
        tmp_path, country, with_query(first["url"], **{filter: value})
    )
    assert health["status"] not in {"complete", "zero_results"}
    assert health["received_count"] == 0
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
    assert (
        db.conn.execute(
            "SELECT count(*) FROM query_membership WHERE query_key='wanted'"
        ).fetchone()[0]
        == 0
    )
    assert (
        db.conn.execute(
            "SELECT count(*) FROM fetches WHERE raw_path IS NOT NULL"
        ).fetchone()[0]
        == 1
    )
    assert collector.detail_jobs == {} and len(requested) == 1
    db.conn.close()


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_conflicting_empty_envelope_is_not_a_verified_empty_search(tmp_path, country):
    first = SEARCH[country.lower() + "-search-page1.json"]

    def empty(state, number):
        state.update(listings=[], numberOfResults=0, numberOfPages=0)

    db, _collector, health, _requested = run_query(
        tmp_path, country, with_query(first["url"], fregfrom=2020), empty
    )
    assert health["status"] not in {"complete", "zero_results"}
    assert health["expected_count"] is None
    db.conn.close()


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_ignored_page_filter_never_adds_that_page_membership(tmp_path, country):
    def change(state, number):
        if number > 1:
            state["pageQuery"]["fregfrom"] = "2020"

    db, _collector, health, _requested = run_query(tmp_path, country, mutate=change)
    assert health["status"] == "partial" and health["received_count"] == 20
    assert (
        db.conn.execute(
            "SELECT count(*) FROM query_membership WHERE query_key='wanted'"
        ).fetchone()[0]
        == 20
    )
    db.conn.close()


@pytest.mark.parametrize("case", CASES, ids=lambda row: row["case"])
def test_twenty_native_first_page_slots_keep_identity_facts_and_normalized_taxonomy(
    case,
):
    doc = captured(case)
    original = copy.deepcopy(doc)
    page = parse(doc, case["final_url"], case["country"])
    assert len(page.listings) == 20
    assert [item.external_id for item in page.listings] == case["native_ids"]
    own = doc["props"]["pageProps"]
    assert page.total == own["numberOfResults"] and page.pages == own["numberOfPages"]
    for item, native in zip(page.listings, own["listings"]):
        assert item.raw["vehicle"] == native["vehicle"]
        assert item.raw["price"] == native["price"]
        assert item.raw["location"] == native["location"]
        assert item.raw.get("market_country") == native["location"]["countryCode"]
    assert doc == original


@pytest.mark.parametrize("country", ["NL", "DE"])
@pytest.mark.parametrize("key", ["atype", "ocs_listing", "sort", "desc", "cat", "mmmv"])
def test_requested_inventory_or_ordering_key_cannot_lose_its_native_echo(country, key):
    case = next(row for row in CASES if row["country"] == country)
    doc = captured(case)
    query = doc["props"]["pageProps"]["pageQuery"]
    url = with_query(case["url"], **{key: query[key]})
    query.pop(key)
    result = parse(doc, url, country)
    assert result.listings == [] and result.total is None


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_existing_c205_searches_keep_original_field_timestamps_and_memberships(
    tmp_path, country
):
    db, _collector, health, _requested = run_query(tmp_path, country)
    assert health["status"] == "complete"
    assert health["received_count"] == (44 if country == "DE" else 33)
    fetches = {
        row["id"]: row["fetched_at"]
        for row in db.conn.execute("SELECT id,fetched_at FROM fetches")
    }
    for row in db.conn.execute("SELECT details_json FROM latest_observations"):
        own = json.loads(row[0])
        for origin in own["raw"]["field_evidence"].values():
            assert origin["observed_at"] == fetches[origin["fetch_id"]]
    assert all(stamp.startswith("2026-10-08T15:25:") for stamp in fetches.values())
    db.conn.close()


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_include_defaults_cannot_accept_an_explicit_native_exclusion(country):
    case = next(row for row in CASES if row["country"] == country)
    doc = captured(case)
    doc["props"]["pageProps"]["pageQuery"]["damaged_listing"] = "exclude"
    assert parse(doc, case["url"], country).listings == []


def test_only_observed_nl_include_absence_is_allowed():
    for country in ["NL", "DE"]:
        case = next(row for row in CASES if row["country"] == country)
        doc = captured(case)
        doc["props"]["pageProps"]["pageQuery"].pop("damaged_listing", None)
        result = parse(doc, case["url"], country)
        assert len(result.listings) == (20 if country == "NL" else 0)
        assert (
            parse(
                doc, with_query(case["url"], damaged_listing="exclude"), country
            ).listings
            == []
        )


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_wrong_declared_model_route_and_inconsistent_native_make_taxonomy_reject(
    country,
):
    case = next(row for row in CASES if row["country"] == country)
    doc = captured(case)
    wrong_route = case["url"].replace("/toyota/corolla", "/volkswagen/golf")
    assert parse(doc, wrong_route, country).listings == []
    doc["props"]["pageProps"]["pageQuery"]["mmmv"] = "74|2084||"
    assert parse(doc, case["url"], country).listings == []


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_pagination_cannot_reuse_a_first_page_for_different_inventory(country):
    case = next(row for row in CASES if row["country"] == country)
    first = parse(captured(case), case["url"], country)
    with pytest.raises(ValueError):
        mercedes_autoscout_page_url(
            {"url": with_query(case["url"], fregfrom="2020")}, 2, first
        )


@pytest.mark.parametrize(
    "fault", ["missing_path", "malformed_path", "taxonomy_prefix", "model_id"]
)
def test_modern_route_and_taxonomy_cannot_silently_lose_binding(fault):
    case = next(row for row in CASES if row["case"] == "nl-corolla")
    doc = captured(case)
    state = doc["props"]["pageProps"]
    if fault == "missing_path":
        state.pop("pagePath")
    elif fault == "malformed_path":
        state["pagePath"] = "https://[invalid/path"
    elif fault == "taxonomy_prefix":
        state["pageQuery"]["mcat"] = "ma70mo205"
    else:
        state["pageQuery"]["mmmv"] = "70|2084||"
    result = parse(doc, case["url"], "NL")
    assert result.listings == [] and result.total is None


def test_repeated_query_filter_values_do_not_bind_to_one_native_value():
    case = next(row for row in CASES if row["case"] == "nl-corolla")
    result = parse(captured(case), case["url"] + "&atype=C", "NL")
    assert result.listings == [] and result.total is None


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_native_year_narrowing_cannot_certify_a_request_without_the_year(country):
    case = SEARCH[country.lower() + "-search-page1.json"]
    doc = json.loads((ROOT / "mercedes_autoscout" / case["file"]).read_text())
    parsed = urlsplit(case["url"])
    query = parse_qs(parsed.query)
    query.pop("fregfrom")
    url = parsed._replace(query=urlencode(query, doseq=True)).geturl()
    result = parse(doc, url, country)
    assert result.listings == [] and result.total is None
