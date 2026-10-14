"""Native inventory and mutations of own item evidence boundaries."""

import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.germany_dealer_inventory import (
    ROOT,
    SOURCE,
    autoland_stock_id,
    collect_dealer_inventory,
    dealer_inventory_page_url,
    parse_dealer_inventory,
)
from car_market.germany_dealers import parse_dealer

FIXTURES = Path(__file__).parent / "fixtures" / "germany_dealer_inventory"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
META = {v["fixture"]: v["metadata"] for v in MANIFEST}


def fixture(name):
    return (FIXTURES / name).read_text()


def mutated(change, name="autoland-p1.html"):
    soup = BeautifulSoup(fixture(name), "html.parser")
    script = soup.select_one('script[type="application/ld+json"]')
    value = json.loads(script.get_text())
    change(soup, value)
    script.string = json.dumps(value)
    return str(soup)


def test_real_retained_census_all_pages_and_model_frontier():
    pages = [
        parse_dealer_inventory(fixture(f"autoland-p{i}.html"), dealer_inventory_page_url(ROOT, i))
        for i in range(1, 6)
    ]
    assert [len(p.listings) for p in pages] == [12, 12, 12, 12, 4]
    assert all((p.total, p.pages, p.page) == (52, 5, i) for i, p in enumerate(pages, 1))
    listings = [x for p in pages for x in p.listings]
    ids = {x.raw["dealer_stock_id"] for x in listings}
    assert len(ids) == 52
    assert Counter(x.scope for x in listings) == {
        "target": 12,
        "needs_confirmation": 1,
        "excluded": 39,
    }
    assert len([v for p in pages for v in p.search_filters["model_unconfirmed"]]) == 12
    assert all(x.url == x.external_id and "#" not in x.url for x in listings)
    assert all(autoland_stock_id(x.url) == x.raw["dealer_stock_id"] for x in listings)
    assert all(x.raw["country_evidence"]["addressCountry"] == ["DE"] for x in listings)
    assert all(
        x.raw["detail_complete"] is False and x.kenteken is None and x.vin is None for x in listings
    )
    target = [x for x in listings if x.scope == "target"]
    assert {x.transmission for x in target} == {"manual", "automatic"}
    assert min(x.year for x in target) == 2016 and max(x.year for x in target) == 2025
    assert min(x.price for x in target) == 14490 and max(x.price for x in target) == 27490
    # The domain and counter cannot replace the own country address.
    assert all(
        x.raw["identity_evidence"]["kind"] == "own_schema_position_and_native_card"
        for x in listings
    )


@pytest.mark.parametrize("country", [None, "NL", "AT", "de-DE"])
def test_foreign_or_missing_own_country_is_held_without_dropping_discovery(country):
    def change(soup, value):
        value["itemListElement"][2]["item"]["offers"]["seller"]["address"]["addressCountry"] = (
            country
        )
        value["unrelatedOrganization"] = {"addressCountry": "DE"}

    page = parse_dealer_inventory(mutated(change), ROOT)
    assert len(page.listings) == 11 and len(page.links) == 12
    assert page.search_filters["held"] == [
        {"stock_id": "100787", "reason": "own_seller_country_not_DE"}
    ]
    assert "100787" not in {x.raw["dealer_stock_id"] for x in page.listings}


@pytest.mark.parametrize(
    "field", ["position", "name", "url", "model", "price", "mileage", "date", "gear", "location"]
)
def test_schema_cannot_transfer_country_or_identity_to_another_native_card(field):
    def change(soup, value):
        element = value["itemListElement"][2]
        item = element["item"]
        if field == "position":
            element["position"] = 4
        elif field == "price":
            item["offers"]["price"] = 1
        elif field == "mileage":
            item["mileageFromOdometer"]["value"] = 1
        elif field == "date":
            item["dateVehicleFirstRegistered"] = "2026-01-01"
        elif field == "gear":
            item["vehicleTransmission"] = "Automatik"
        elif field == "location":
            item["offers"]["seller"]["name"] = "Autoland Niederlassung Berlin"
        else:
            item[field] = {"name": "Other BMW", "url": ROOT + "#4", "model": "218"}[field]

    page = parse_dealer_inventory(mutated(change), ROOT)
    assert len(page.listings) == 11
    assert page.search_filters["held"][0]["stock_id"] == "100787"
    assert len(page.search_filters["discovered"]) == 12


@pytest.mark.parametrize(
    "field", ["stock", "page", "counter", "canonical", "filter", "range", "duplicate", "card_count"]
)
def test_malformed_native_page_never_claims_an_inventory(field):
    def change(soup, value):
        card = soup.select(".ivl-search--article")[2]
        if field == "stock":
            card["data-article-id"] = "100788"
        elif field == "page":
            card["data-page"] = "2"
        elif field == "counter":
            soup.select_one(".ivl-search--number-count").string = "53"
        elif field == "canonical":
            soup.select_one('link[rel="canonical"]')["href"] = ROOT + "?page=2"
        elif field == "filter":
            soup.select_one('input[name="gear[]"]')["checked"] = "checked"
        elif field == "range":
            soup.select_one('input[name="minRegistration"]')["value"] = "2020"
        elif field == "duplicate":
            cards = soup.select(".ivl-search--article")
            cards[2].replace_with(BeautifulSoup(str(cards[1]), "html.parser"))
        else:
            card.decompose()

    page = parse_dealer_inventory(mutated(change), ROOT)
    assert not page.listings and page.total is None
    assert page.search_filters["coverage_status"] == "unverified"


@pytest.mark.parametrize(
    "url",
    [
        ROOT + "?minRegistration=2020",
        ROOT + "?page=1&page=2",
        ROOT.replace("autoland.de", "evil.example"),
        ROOT + "#1",
        ROOT + "/bad-hersfeld/122718/car",
    ],
)
def test_pagination_contract_never_accepts_unobserved_filters_or_other_hosts(url):
    with pytest.raises(ValueError):
        dealer_inventory_page_url(url, 2)
    assert not parse_dealer_inventory(fixture("autoland-p1.html"), url).listings


def test_existing_dispatcher_ingests_own_inventory_but_not_a_different_source():
    assert len(parse_dealer(SOURCE, fixture("autoland-p1.html"), ROOT).listings) == 12
    assert not parse_dealer("dealer:elsewhere.de", fixture("autoland-p1.html"), ROOT).listings
    assert not parse_dealer(fixture("autoland-p1.html"), ROOT + "?page=2").listings


@pytest.mark.parametrize("name", ["wiveda-detail.html", "muenchen-detail.html"])
def test_live_second_platform_country_hold_survives_own_product_identity(name):
    page = parse_dealer(fixture(name), META[name]["final_url"])
    assert not page.listings
    assert page.search_filters["parse_status"] == (
        "own_seller_country_not_DE"
        if name == "wiveda-detail.html"
        else "own_vehicle_missing_or_ambiguous"
    )


def test_native_218ia_stays_pending_with_original_variant_and_proven_stock():
    name = "autoland-detail-123515.html"
    item = parse_dealer(fixture(name), META[name]["final_url"]).listings[0]
    from car_market.germany import qualify_german_item

    qualify_german_item(item)
    assert item.scope == "needs_confirmation" and item.raw["model_variant_native"] == "218iA"
    assert item.raw["dealer_stock_id"] == "123515" and item.vin is None


def test_bounded_public_fixture_provenance_hashes():
    for row in MANIFEST:
        assert (
            hashlib.sha256((FIXTURES / row["fixture"]).read_bytes()).hexdigest()
            == row["fixture_sha256"]
        )
        assert row["metadata"]["status"] == 200
        assert row["metadata"]["fetched_at"].startswith("2026-10-08")
        assert row["metadata"]["request"] == {"method": "GET"}


def collector_with_fixtures(tmp_path, mutate_response=None):
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"scope": {"country": "DE"}})
    calls = []

    def get(url, refresh=False):
        name = (
            "autoland-refresh.html"
            if refresh
            else f"autoland-p{int(url.rsplit('=', 1)[1]) if '?' in url else 1}.html"
        )
        meta = META[name]
        response = Response(
            url,
            meta["final_url"],
            200,
            fixture(name),
            meta["fetched_at"],
            transport="retained_ordinary_HTTP_replay",
        )
        calls.append((url, refresh))
        return mutate_response(response, name) if mutate_response else response

    collector.fetcher = SimpleNamespace(get=get)
    return db, collector, calls


def query(**values):
    return {"source": SOURCE, "key": "autoland_bmw_2er_all", "url": ROOT, **values}


def test_temp_database_integration_keeps_proofs_census_and_frontier_separate(tmp_path):
    db, collector, calls = collector_with_fixtures(tmp_path)
    result = collect_dealer_inventory(collector, db.start_run(), query())
    assert (result["expected"], result["received"], result["pages"], result["targets"]) == (
        52,
        52,
        5,
        12,
    )
    assert result["enumeration_status"] == "complete"
    assert result["status"] == "partial" and len(result["model_unconfirmed"]) == 12
    assert result["errors"] == ["dealer_inventory_unconfirmed_target_models"]
    assert len(calls) == 6 and calls[-1] == (ROOT, True)
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 13
    assert (
        db.conn.execute(
            "SELECT count(*) FROM observations WHERE json_extract(details_json,'$.raw.market_country')<>'DE'"
        ).fetchone()[0]
        == 0
    )
    assert (
        db.conn.execute(
            "SELECT count(*) FROM vehicles WHERE vin IS NOT NULL OR kenteken IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    assert (
        db.conn.execute(
            "SELECT count(*) FROM events WHERE kind='not_seen_in_complete_query'"
        ).fetchone()[0]
        == 0
    )
    assert db.conn.execute("SELECT count(*) FROM fetches").fetchone()[0] == 6
    assert db.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "failure", ["budget", "refusal", "redirect", "repeat", "drift", "country", "refresh_country"]
)
def test_census_never_exhausts_on_budget_refusal_repetition_drift_or_country(tmp_path, failure):
    def change(response, name):
        if name == "autoland-p2.html":
            if failure == "refusal":
                response.status, response.body, response.error = 403, "Refused", "http_403"
            elif failure == "redirect":
                response.final_url = ROOT
            elif failure == "repeat":
                response.body = fixture("autoland-p1.html")
            elif failure == "drift":
                response.body = response.body.replace(">52<", ">53<").replace(
                    'data-article-count="52"', 'data-article-count="53"'
                )
        if (failure == "country" and name == "autoland-p1.html") or (
            failure == "refresh_country" and name == "autoland-refresh.html"
        ):
            response.body = response.body.replace(
                '"addressCountry":"DE"', '"addressCountry":"NL"', 1
            )
        return response

    db, collector, calls = collector_with_fixtures(tmp_path, change)
    result = collect_dealer_inventory(
        collector, db.start_run(), query(max_pages=1 if failure == "budget" else 10)
    )
    assert result["status"] == result["enumeration_status"] == "partial"
    assert (
        db.conn.execute(
            "SELECT count(*) FROM events WHERE kind='not_seen_in_complete_query'"
        ).fetchone()[0]
        == 0
    )
    if failure == "refusal":
        assert len(calls) == 2
        assert db.conn.execute(
            "SELECT status,error FROM fetches ORDER BY id DESC LIMIT 1"
        ).fetchone()[:] == (403, "http_403")
    if failure == "country":
        assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 13
    assert (
        db.conn.execute(
            "SELECT count(*) FROM observations WHERE json_extract(details_json,'$.raw.market_country')<>'DE'"
        ).fetchone()[0]
        == 0
    )


def wiveda_bundle(name="root"):
    return json.loads(fixture("wiveda-" + name + "-bundle.json"))


def native_records(bundle):
    from car_market.germany_dealer_inventory import _devalue

    return _devalue(bundle["payload"])


def mutate_native_field(bundle, identifier, field, value):
    # Mutate the own typed record without borrowing country from the global node.
    array = bundle["payload"]
    record = next(
        v
        for v in array
        if isinstance(v, dict)
        and isinstance(v.get("id"), int)
        and v["id"] >= 0
        and array[v["id"]] == identifier
    )
    record[field] = len(array)
    array.append(value)


def test_wiveda_real_native_country_individual_ids_not_dealer_headquarters():
    from car_market.germany_dealer_inventory import parse_wiveda_bundle

    pages = [
        parse_wiveda_bundle(b := wiveda_bundle(name), b["html_url"]) for name in ["root", "p2"]
    ]
    assert [len(v.listings) for v in pages] == [12, 9]
    items = [x for p in pages for x in p.listings]
    assert len({x.raw["dealer_stock_id"] for x in items}) == 21
    assert Counter(x.scope for x in items) == {"target": 15, "needs_confirmation": 2, "excluded": 4}
    assert all(x.raw["country_evidence"]["kind"] == "own_native_vehicle_country" for x in items)
    assert all(x.raw["country_evidence"]["country"] == "DE" for x in items)
    # This stock city differs from the dealer address; DE comes from vehicle.country.
    different = next(
        x for x in items if x.raw["dealer_stock_id"] == "99ad8d68-ec4b-4147-b2c4-a6e06ce1d922"
    )
    assert different.city == "73037 Göppingen"
    assert different.raw["native_vehicle"]["dealer"]["location"] == "73111 Lauterstein"
    assert different.scope == "needs_confirmation"


@pytest.mark.parametrize("country", [None, "NL", "AT", "de-DE"])
def test_wiveda_own_country_mutation_cannot_inherit_dom_map_or_dealer_country(country):
    from car_market.germany_dealer_inventory import parse_wiveda_bundle

    bundle = wiveda_bundle()
    identifier = "9ec24de5-36ee-4387-a4bc-b942bfc27e23"
    mutate_native_field(bundle, identifier, "country", country)
    page = parse_wiveda_bundle(bundle, bundle["html_url"])
    assert len(page.listings) == 11
    assert page.search_filters["held"] == [
        {"stock_id": identifier, "reason": "own_vehicle_country_not_DE"}
    ]
    assert len(page.links) == 12
    detail = wiveda_bundle("detail")
    mutate_native_field(detail, identifier, "country", country)
    assert not parse_wiveda_bundle(detail, detail["html_url"]).listings


@pytest.mark.parametrize("field", ["id", "html_url", "payload_url", "link", "country_key"])
def test_wiveda_native_response_cannot_bind_other_vehicle_or_unobserved_route(field):
    from car_market.germany_dealer_inventory import parse_wiveda_bundle

    bundle = wiveda_bundle("detail")
    url = bundle["html_url"]
    identifier = "9ec24de5-36ee-4387-a4bc-b942bfc27e23"
    if field == "id":
        mutate_native_field(bundle, identifier, "id", "3b376bb2-8493-4329-bee6-501480542936")
    elif field == "html_url":
        bundle["html_url"] += "?other=1"
    elif field == "payload_url":
        bundle["payload_url"] = bundle["payload_url"].replace("wiveda.de", "example.com")
    elif field == "link":
        bundle["html"] = bundle["html"].replace("/_payload.json", "/../else/_payload.json")
    else:
        data = bundle["payload"]
        node = next(v for v in data if isinstance(v, dict) and "vehicle-" + identifier in v)
        node["vehicle-other"] = node.pop("vehicle-" + identifier)
    assert not parse_wiveda_bundle(bundle, url).listings


def test_wiveda_detail_full_own_vin_stock_and_gross_net_remain_separate():
    from car_market.germany_dealer_inventory import WIVEDA_SOURCE

    detail = wiveda_bundle("detail")
    page = parse_dealer(WIVEDA_SOURCE, json.dumps(detail), detail["html_url"])
    item = page.listings[0]
    assert item.vin == "WBA21BX0X07T40980" and item.kenteken is None
    assert item.raw["dealer_stock_id"] == "9ec24de5-36ee-4387-a4bc-b942bfc27e23"
    assert item.scope == "target" and item.price == 27490 and item.price_type == "advertised_price"
    assert item.raw["price_components"]["net_price"] == 23100.84
    assert item.raw["first_registration"] == "2025-06"
    manual = wiveda_bundle("manual")
    other = parse_dealer(WIVEDA_SOURCE, json.dumps(manual), manual["html_url"]).listings[0]
    assert other.scope == "target" and other.year == 2016 and other.transmission == "manual"
    assert other.vin is None


def test_wiveda_conflicting_own_vin_fails_closed():
    from car_market.germany_dealer_inventory import parse_wiveda_bundle

    bundle = wiveda_bundle("detail")
    mutate_native_field(bundle, "9ec24de5-36ee-4387-a4bc-b942bfc27e23", "vin", "WBA2A11070V123456")
    page = parse_wiveda_bundle(bundle, bundle["html_url"])
    assert not page.listings and page.search_filters["parse_status"] == "conflicting_own_vin"


@pytest.mark.parametrize("shape", ["out_of_bounds", "cycle", "wrong_type"])
def test_wiveda_devalue_pointer_errors_do_not_turn_indices_into_facts(shape):
    from car_market.germany_dealer_inventory import parse_wiveda_bundle

    bundle = wiveda_bundle()
    if shape == "out_of_bounds":
        bundle["payload"][0]["data"] = 999999
    elif shape == "cycle":
        bundle["payload"][0]["data"] = 0
    else:
        bundle["payload"] = {"country": "DE"}
    page = parse_wiveda_bundle(bundle, bundle["html_url"])
    assert not page.listings and page.total is None


@pytest.mark.parametrize("field", ["country", "price", "repeat", "counter"])
def test_wiveda_item_country_price_and_counter_proof_cannot_be_swapped(field):
    from car_market.germany_dealer_inventory import parse_wiveda_bundle

    bundle = wiveda_bundle()
    if field == "price":
        mutate_native_field(bundle, "9ec24de5-36ee-4387-a4bc-b942bfc27e23", "price", 100)
    elif field == "country":
        mutate_native_field(bundle, "9ec24de5-36ee-4387-a4bc-b942bfc27e23", "country", None)
    elif field == "repeat":
        node = next(
            v for v in bundle["payload"] if isinstance(v, dict) and "items" in v and "per_page" in v
        )
        items = bundle["payload"][node["items"]]
        items[1] = items[0]
    else:
        node = next(
            v for v in bundle["payload"] if isinstance(v, dict) and "items" in v and "per_page" in v
        )
        node["total"] = len(bundle["payload"])
        bundle["payload"].append(50)
    page = parse_wiveda_bundle(bundle, bundle["html_url"])
    if field in {"country", "price"}:
        assert len(page.listings) == 11 and len(page.search_filters["held"]) == 1
    else:
        assert not page.listings and page.total is None


def wiveda_collector(tmp_path, failure=None):
    from car_market.germany_dealer_inventory import WIVEDA_ROOT, WIVEDA_SOURCE

    bundles = [wiveda_bundle(name) for name in ["root", "p2", "detail", "manual"]]
    routes = {}
    for bundle, name in zip(bundles, ["root", "p2", "detail", "manual"]):
        row = next(v for v in MANIFEST if v["fixture"] == "wiveda-" + name + "-bundle.json")
        routes[bundle["html_url"]] = (bundle["html"], row["html_metadata"]["fetched_at"])
        routes[bundle["payload_url"]] = (
            json.dumps(bundle["payload"]),
            row["metadata"]["fetched_at"],
        )
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"scope": {"country": "DE"}})
    calls = []

    def get(url, refresh=False):
        calls.append((url, refresh))
        if failure == "403" and "?page=2" in url:
            return Response(url, url, 403, "Refused", "2026-10-08T07:00:00Z", "http_403")
        body, stamp = routes[url]
        return Response(url, url, 200, body, stamp, transport="retained_ordinary_HTTP_replay")

    collector.fetcher = SimpleNamespace(get=get)
    query_ = {
        "source": WIVEDA_SOURCE,
        "key": "wiveda_used_218_active",
        "url": WIVEDA_ROOT,
        "max_pages": 3,
        "max_details": 2,
        "refresh_root": False,
    }
    return db, collector, calls, query_


def test_wiveda_temp_db_own_country_bound_inventory_details_and_vin(tmp_path):
    from car_market.germany_dealer_inventory import collect_wiveda_inventory

    db, collector, calls, query_ = wiveda_collector(tmp_path)
    result = collect_wiveda_inventory(collector, db.start_run(), query_)
    assert (
        result["expected"],
        result["received"],
        result["own_DE_records"],
        result["targets"],
        result["model_pending"],
        result["details"],
    ) == (21, 21, 21, 15, 2, 2)
    assert result["status"] == "partial"
    assert result["errors"] == [
        "wiveda_live_root_control_not_captured",
        "wiveda_unconfirmed_engine_variants",
    ]
    assert len(calls) == result["HTTP_requests"] == 8
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 17
    assert db.conn.execute("SELECT count(*) FROM vehicles WHERE vin IS NOT NULL").fetchone()[0] == 1
    assert (
        db.conn.execute(
            "SELECT count(*) FROM observations WHERE coalesce(json_extract(details_json,'$.raw.market_country'),'')<>'DE'"
        ).fetchone()[0]
        == 0
    )
    assert (
        db.conn.execute("SELECT count(*) FROM vehicles WHERE kenteken IS NOT NULL").fetchone()[0]
        == 0
    )
    assert (
        db.conn.execute(
            "SELECT count(*) FROM events WHERE kind='not_seen_in_complete_query'"
        ).fetchone()[0]
        == 0
    )
    assert db.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


def test_wiveda_stops_at_refusal_and_keeps_native_bounded_frontier(tmp_path):
    from car_market.germany_dealer_inventory import collect_wiveda_inventory

    db, collector, calls, query_ = wiveda_collector(tmp_path, "403")
    result = collect_wiveda_inventory(collector, db.start_run(), query_)
    assert result["status"] == "partial" and result["received"] == 12
    assert len(calls) == 3 and result["details"] == 0
    assert "http_403" in result["errors"]
    assert db.conn.execute("SELECT status,error FROM fetches ORDER BY id DESC LIMIT 1").fetchone()[
        :
    ] == (403, "http_403")
    assert (
        db.conn.execute(
            "SELECT count(*) FROM events WHERE kind='not_seen_in_complete_query'"
        ).fetchone()[0]
        == 0
    )
