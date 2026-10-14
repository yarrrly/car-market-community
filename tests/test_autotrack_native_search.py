"""Reconcile the actual AutoTrack route migration and reject substituted state."""

import copy
import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from car_market.collect import page_url
from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database
from car_market.fetch import Response
from car_market.germany_autoscout import _state
from car_market.parse import parse_page
from car_market.search_profile import GENERIC_PROFILE, search_context

ROOT = Path(__file__).parent / "fixtures/autotrack_native_search"
META = json.loads((ROOT / "provenance.json").read_text())
SCOPE = {
    "country": "NL",
    "model_profile": GENERIC_PROFILE,
    "selector": {"make": "Toyota", "model_terms": [["Corolla"]]},
}


def body(index):
    blob = (ROOT / META[index]["filename"]).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == META[index]["compressed_sha256"]
    decoded = gzip.decompress(blob)
    assert hashlib.sha256(decoded).hexdigest() == META[index]["decoded_sha256"]
    return decoded.decode()


def parsed(index, payload=None, url=None):
    with search_context(SCOPE):
        return parse_page(
            "autotrack", payload or body(index), url or META[index]["final_url"]
        )


def test_native_pages_reconcile_exact_own_ids_and_keep_source_facts():
    first, second = parsed(0), parsed(1)
    assert (first.total, first.pages, first.page) == (836, 42, 1)
    assert (second.total, second.pages, second.page) == (836, 42, 2)
    for page, meta in ((first, META[0]), (second, META[1])):
        assert len(page.listings) == 20
        assert [x.raw["own_uuid"] for x in page.listings] == meta["native_ids"]
        assert all(
            x.source == "autotrack" and x.external_id == x.url for x in page.listings
        )
        assert all(
            x.scope == "target" and x.raw["market_country"] == "NL"
            for x in page.listings
        )
    assert first.search_filters == second.search_filters
    assert (
        first.listings[0].price,
        first.listings[0].year,
        first.listings[0].mileage,
    ) == (16995, 2021, 153321)
    assert len({x.external_id for x in first.listings + second.listings}) == 40


@pytest.mark.parametrize(
    "fault", ["page", "country", "cat", "host", "build", "foreign_url"]
)
def test_native_query_mismatches_never_fall_back_to_schema_or_recommendations(fault):
    state = copy.deepcopy(_state(body(1))[0])
    envelope = {"pageProps": state}
    url = META[1]["final_url"]
    if fault == "page":
        state["pageQuery"]["page"] = "3"
    elif fault == "country":
        state["pageQuery"]["cy"] = "D"
    elif fault == "cat":
        state["pageQuery"]["cat"] = "different-model"
    elif fault == "host":
        state["pageQuery"]["host"] = "www.autoscout24.nl"
    elif fault == "build":
        envelope["buildId"] = "other-build"
    else:
        url = url.replace("www.autotrack.nl", "www.autoscout24.nl")
    assert parsed(1, json.dumps(envelope), url).listings == []


@pytest.mark.parametrize("fault", ["id", "foreign_card", "missing_country"])
def test_own_card_binding_and_country_are_independent_of_query_country(fault):
    state = copy.deepcopy(_state(body(1))[0])
    own = state["listings"][0]
    if fault == "id":
        own["id"] = state["listings"][1]["id"]
    elif fault == "foreign_card":
        own["url"] = "https://www.autoscout24.nl" + own["url"]
    else:
        own["location"].pop("countryCode")
    result = parsed(1, json.dumps({"pageProps": state}))
    if fault == "missing_country":
        assert len(result.listings) == 20
        assert result.listings[0].raw.get("market_country") is None
    else:
        assert len(result.listings) == 19


def test_collector_follows_resolved_native_route_and_retains_all_page_two_ids(tmp_path):
    legacy_url = "https://www.autotrack.nl/aanbod?data.merkModel.filter.0.slug=toyota&data.merkModel.filter.0.models.0.slug=corolla&pageSize=30&pageNumber=1"
    query = {
        "source": "autotrack",
        "key": "corolla",
        "url": legacy_url,
        "pagination": "autotrack",
        "coverage": "enumerate",
        "infer_missing": False,
    }
    config = {
        "schema_version": 1,
        "scope": SCOPE,
        "queries": [query],
        "max_pages": 2,
        "workers": 1,
        "detail_limit": 0,
    }
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, config)
    requested = []

    class NativeFetcher:
        def get(self, url, **kwargs):
            requested.append(url)
            index = 0 if url == legacy_url else 1
            if index == 1:
                assert (
                    urlsplit(url).path
                    == "/_next/data/" + META[0]["build_id"] + "/lst.json"
                )
                assert parse_qs(urlsplit(url).query)["page"] == ["2"]
                assert parse_qs(urlsplit(url).query)["cat"] == ["ma70mo2052"]
            return Response(
                url,
                META[0]["final_url"] if index == 0 else url,
                200,
                body(index),
                META[index]["timestamp"],
            )

    collector.fetcher = NativeFetcher()
    run = db.start_run()
    collector.collect_query(run, query)
    health = db.conn.execute(
        "SELECT * FROM source_runs WHERE run_id=?", (run,)
    ).fetchone()
    assert health["expected_count"] == 836
    assert health["received_count"] == 40 and health["target_count"] == 40
    assert health["status"] == "partial" and "page_limit_exceeded" in health["error"]
    assert len(requested) == 2
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 40
    assert db.conn.execute("SELECT count(*) FROM fetches").fetchone()[0] == 2
    assert db.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()
    db.conn.close()


def test_native_route_builder_preserves_path_bound_model_and_rejects_page_201():
    first = parsed(0, url="https://www.autotrack.nl/aanbod/toyota/corolla")
    route = page_url(
        {
            "pagination": "autotrack",
            "url": "https://www.autotrack.nl/aanbod/toyota/corolla",
        },
        2,
        first,
    )
    assert parse_qs(urlsplit(route).query)["cat"] == ["ma70mo2052"]
    with pytest.raises(ValueError):
        page_url({"pagination": "autotrack", "url": META[0]["final_url"]}, 201, first)


def test_different_model_path_cannot_use_the_native_corolla_inventory():
    assert not parsed(0, url="https://www.autotrack.nl/aanbod/volkswagen/golf").listings


@pytest.mark.parametrize("fault", ["counter", "listings", "duplicate", "invalid"])
def test_damaged_native_envelope_cannot_fall_back_to_unbound_schema(fault):
    soup = BeautifulSoup(body(0), "html.parser")
    script = soup.select_one("script#__NEXT_DATA__")
    doc = json.loads(script.string)
    if fault in {"counter", "listings"}:
        doc["props"]["pageProps"].pop(
            "numberOfResults" if fault == "counter" else "listings"
        )
        script.string = json.dumps(doc)
    elif fault == "invalid":
        script.string = "{invalid-json"
    payload = str(soup)
    if fault == "duplicate":
        payload += '<script id="__NEXT_DATA__" type="application/json">{}</script>'
    assert not parsed(0, payload).listings


def test_malformed_counter_text_cannot_be_substring_parsed_into_complete_census():
    state = copy.deepcopy(_state(body(1))[0])
    state.update(numberOfResults="unknown 20 records", numberOfPages="unknown 1 pages")
    page = parsed(1, json.dumps({"pageProps": state}))
    assert len(page.listings) == 20
    assert page.total is None and page.pages is None


def test_one_malformed_native_url_does_not_drop_the_other_nineteen_cards():
    state = copy.deepcopy(_state(body(1))[0])
    state["listings"][0]["url"] = "https://[invalid-ipv6/a/car"
    page = parsed(1, json.dumps({"pageProps": state}))
    assert [x.raw["own_uuid"] for x in page.listings] == META[1]["native_ids"][1:]


def test_typed_native_fuel_label_preserves_hybrid_classification():
    state = copy.deepcopy(_state(body(1))[0])
    vehicle = state["listings"][0]["vehicle"]
    vehicle["fuel"] = {"label": "Elektro/Benzine"}
    vehicle["transmission"] = {"label": "Handgeschakeld"}
    page = parsed(1, json.dumps({"pageProps": state}))
    assert (page.listings[0].fuel, page.listings[0].transmission) == (
        "hybrid",
        "manual",
    )
