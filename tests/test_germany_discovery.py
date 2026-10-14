"""Retained public evidence and a fail-closed transition from frontier to own DE VDP."""

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from car_market.fetch import Response
from car_market.germany import german_detail_identity_matches, parse_germany_page
from car_market.germany_discovery import (
    GEBRAUCHTWAGEN_SEARCH,
    QUOKA_SEARCH,
    collect_discovery,
    discovery_id,
    discovery_page_url,
    discovery_queries,
    parse_discovery,
    parse_discovery_publisher,
    publisher_reference,
    quoka_native_partitions,
)

FIXTURES = Path(__file__).parent / "fixtures" / "germany_discovery"
MANIFEST = {r["fixture"]: r for r in json.loads((FIXTURES / "manifest.json").read_text())}
AS_FIXTURES = Path(__file__).parent / "fixtures" / "germany_autoscout"
AS_MANIFEST = {r["fixture"]: r for r in json.loads((AS_FIXTURES / "manifest.json").read_text())}
AS_DETAIL = "de-target-2e5a3510-4132-41b9-91b8-dec32375c254.html"
AS_ID = "2e5a3510-4132-41b9-91b8-dec32375c254"
STAMP = "2026-10-07T22:00:00+00:00"


def retained(name):
    source = "quoka_de" if name.startswith("quoka") else "gebrauchtwagen_de"
    return parse_discovery(source, (FIXTURES / name).read_text(), MANIFEST[name]["final_url"])


def quoka_soup(detail=False):
    return BeautifulSoup(
        (FIXTURES / ("quoka-detail.html" if detail else "quoka-search.html")).read_text(),
        "html.parser",
    )


def replace_document(soup, mutate):
    script = soup.select_one('script[type="application/ld+json"]')
    doc = json.loads(script.get_text())
    mutate(doc)
    script.string = json.dumps(doc)
    return str(soup)


def test_fixtures_are_bound_by_hash_without_session_material():
    assert len(MANIFEST) == 13
    for name, meta in MANIFEST.items():
        data = (FIXTURES / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == meta["fixture_sha256"]
        assert meta["http_status"] == 200 and meta["fetched_at"].startswith(
            ("2026-10-07", "2026-10-08")
        )
        assert b"authenticity_token" not in data and b"csrf-token" not in data
        assert b"AIza" not in data


def test_twelve_real_cards_native_ids_prices_and_independent_counters():
    first, second = retained("12gebrauchtwagen-base.html"), retained("12gebrauchtwagen-page2.html")
    assert (first.total, first.pages, first.page, len(first.listings)) == (247, 13, 1, 21)
    assert (second.total, second.pages, second.page, len(second.listings)) == (247, 13, 2, 20)
    assert first.search_filters["source_counters"] == {
        "heading": 247,
        "narrative": 246,
        "aggregate_offer": 247,
    }
    assert (
        first.search_filters["counter_conflict"] and not second.search_filters["counter_conflict"]
    )
    assert all(
        "market_country" not in x.raw and x.kenteken is None and x.vin is None
        for x in first.listings
    )
    own = next(x for x in first.listings if x.external_id == "385390779")
    assert (own.price, own.mileage, own.year, own.transmission, own.fuel, own.scope) == (
        22680,
        21995,
        2022,
        "automatic",
        "petrol",
        "needs_confirmation",
    )
    assert own.raw["publication_reference"]["external_id"] == own.external_id
    assert own.raw["publisher_label"] == "Besuche Carwow ➚"
    assert first.listings[0].price_type == "lease_price" and first.listings[0].price == 305
    assert any(x.raw["placement"] == "promoted" for x in first.listings)


def test_twelve_broader_petrol_control_is_not_exact_active_tourer_total():
    page = retained("12gebrauchtwagen-benzin.html")
    assert (page.total, page.pages, len(page.listings)) == (2743, 138, 21)
    assert page.search_filters["source_counters"]["narrative"] == 2492
    assert any(x.scope == "excluded" for x in page.listings)
    assert all(x.scope != "target" for x in page.listings)


def test_twelve_url_attribute_identity_mismatch_is_not_a_card():
    soup = BeautifulSoup((FIXTURES / "12gebrauchtwagen-base.html").read_text(), "html.parser")
    card = soup.select(".offer-card")[2]
    for node in card.select("[data-car-favorite-offer-id]"):
        node["data-car-favorite-offer-id"] = "123456"
    page = parse_discovery("gebrauchtwagen_de", str(soup), GEBRAUCHTWAGEN_SEARCH)
    assert "385390779" not in {x.external_id for x in page.listings}


def test_quoka_public_rows_have_own_ids_explicit_native_links_and_unknown_country():
    first, second = retained("quoka-search.html"), retained("quoka-page2.html")
    assert (first.total, first.pages, first.page, len(first.listings)) == (751, 38, 1, 20)
    assert (second.total, second.pages, second.page, len(second.listings)) == (751, 38, 2, 20)
    assert first.search_filters["country"] == "UNKNOWN"
    assert (
        len({x.external_id for x in first.listings + second.listings}) == 39
    )  # A real repetition at the boundary.
    own = first.listings[0]
    assert own.external_id == "45i0f0dgf83e7i78e62i1f7726i26340"
    assert discovery_id("quoka_de", own.url) == own.external_id
    reference = own.raw["publication_reference"]
    assert (reference["source"], reference["external_id"], reference["kind"]) == (
        "autoscout24_de",
        "f7949613-b7a3-405c-be27-0dae3ff4ef0a",
        "explicit_own_publication_link",
    )
    assert all(
        "market_country" not in x.raw and x.kenteken is None and x.vin is None
        for x in first.listings
    )


def test_quoka_real_own_detail_country_is_from_own_location_not_domain():
    page = retained("quoka-detail.html")
    assert (page.total, len(page.listings), page.search_filters["country"]) == (1, 1, "DE")
    own = page.listings[0]
    assert (own.city, own.price, own.mileage, own.year, own.fuel, own.transmission, own.scope) == (
        "Marktsteft",
        24295,
        43900,
        2022,
        "petrol",
        "automatic",
        "target",
    )
    assert own.raw["country_evidence"][0]["postcode"] == "97342"
    assert own.raw["country_evidence"][0]["state"] == "Bayern"
    assert own.vin is None and own.raw["own_details"]["Chassis-Serie"] == "f7949613-b7a3-405"
    assert own.kenteken is None


def test_quoka_footer_state_and_country_of_origin_never_restore_unknown_country():
    soup = quoka_soup(detail=True)
    soup.select_one(".detail-info").decompose()
    footer = soup.new_tag("footer")
    footer.string = "Deutschland Bayern Marktsteft Deutsches Fahrzeug FIN: WBA21BX0X07T40980"
    soup.body.append(footer)
    own = parse_discovery(
        "quoka_de", str(soup), MANIFEST["quoka-detail.html"]["final_url"]
    ).listings[0]
    assert "market_country" not in own.raw and own.scope == "needs_confirmation"
    assert own.vin is None


def test_conflicting_own_country_and_german_location_fail_closed():
    data = replace_document(
        quoka_soup(detail=True), lambda d: d.update(address={"addressCountry": "NL"})
    )
    own = parse_discovery("quoka_de", data, MANIFEST["quoka-detail.html"]["final_url"]).listings[0]
    assert own.raw["geography_conflict"] is True and "market_country" not in own.raw
    assert own.scope == "needs_confirmation"


def test_own_full_vin_is_allowed_but_related_vin_and_canonical_mismatch_are_not():
    data = replace_document(
        quoka_soup(detail=True), lambda d: d.update(vehicleIdentificationNumber="WBA21BX0X07T40980")
    )
    url = MANIFEST["quoka-detail.html"]["final_url"]
    assert parse_discovery("quoka_de", data, url).listings[0].vin == "WBA21BX0X07T40980"
    soup = quoka_soup(detail=True)
    soup.select_one('link[rel="canonical"]')["href"] = retained("quoka-page2.html").listings[1].url
    assert parse_discovery("quoka_de", str(soup), url).listings == []


def test_multiple_own_publisher_ids_are_ambiguous_not_fuzzy_merged():
    def mutate(doc):
        own = doc["itemListElement"][0]
        own["description"] += '<a href="' + AS_MANIFEST[AS_DETAIL]["url"] + '">Weitere Anzeige</a>'

    data = replace_document(quoka_soup(), mutate)
    own = parse_discovery("quoka_de", data, QUOKA_SEARCH).listings[0]
    assert own.raw["publication_reference_status"] == "ambiguous"
    assert "publication_reference" not in own.raw


@pytest.mark.parametrize(
    "url",
    [
        "https://www.autoscout24.de/",
        "https://www.autoscout24.de.evil.example/angebote/car-2e5a3510-4132-41b9-91b8-dec32375c254",
        "https://user:password@www.autoscout24.de/angebote/car-2e5a3510-4132-41b9-91b8-dec32375c254",
        "https://www.autohero.com/nl/car/id/2e5a3510-4132-41b9-91b8-dec32375c254/",
        "https://angebote.carwow.de/deals/item",
    ],
)
def test_publisher_mapping_rejects_homepage_credentials_foreign_route_and_unimplemented_source(url):
    assert publisher_reference(url) is None


def test_native_pagination_preserves_search_filters_and_two_actual_source_routes():
    q = discovery_page_url("quoka_de", QUOKA_SEARCH + "&commercial=false", 38)
    assert parse_qs(urlsplit(q).query) == {
        "q": ["bmw 218i active tourer"],
        "commercial": ["false"],
        "pag": ["38"],
    }
    assert discovery_page_url("gebrauchtwagen_de", GEBRAUCHTWAGEN_SEARCH, 13).endswith("?page=13")
    queries = discovery_queries()
    assert {x["source"] for x in queries} == {"quoka_de", "gebrauchtwagen_de"}
    assert all(x["infer_missing"] is False and x["max_pages"] == 2 for x in queries)
    for bad in [0, 201, True, "2"]:
        with pytest.raises(ValueError):
            discovery_page_url("quoka_de", QUOKA_SEARCH, bad)


class MemoryDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE monitor_state(key TEXT PRIMARY KEY,value TEXT)")

    def finish_source(self, *args, **kwargs):
        self.finished = {"args": args, **kwargs}


class OfflineCollector:
    def __init__(self, responder):
        self.country = "DE"
        self.config = {}
        self.db = MemoryDB()
        self.failures, self.requests, self.observed, self.retained = [], [], [], []
        self.responder = responder
        self.fetcher = SimpleNamespace(get=self.get)

    def get(self, url):
        self.requests.append(url)
        return self.responder(url)

    def fetch_record(self, run, source, response, retain_raw=True):
        self.retained.append((source, response, retain_raw))
        return len(self.retained)

    def _parse(self, source, body, url):
        return parse_germany_page(source, body, url)

    def _detail_matches(self, source, item_url, final_url, body, identity):
        return german_detail_identity_matches(source, item_url, final_url, body, identity)

    def _observe(self, run, item, stamp, fetch_id, key):
        stored = self.db.conn.execute("SELECT value FROM monitor_state").fetchone()
        assert stored and json.loads(stored[0])["cards"]  # The frontier was already persisted.
        assert item.raw.get("market_country") == "DE" and item.kenteken is None
        self.observed.append(item)
        return len(self.observed)


def response(url, body, final=None, status=200, problem=None):
    return Response(url, final or url, status, body, STAMP, problem)


def linked_search():
    # Test-only linkage between the frontier and an existing real native fixture.
    def mutate(doc):
        doc["itemListElement"][0]["description"] = (
            '<a href="' + AS_MANIFEST[AS_DETAIL]["url"] + '">Details</a>'
        )

    return replace_document(quoka_soup(), mutate)


def test_unknown_search_writes_only_frontier_then_verified_native_de_item():
    def responder(url):
        if "quoka.de" in url:
            return response(url, linked_search())
        return response(url, (AS_FIXTURES / AS_DETAIL).read_text())

    collector = OfflineCollector(responder)
    ledger = collect_discovery(
        collector,
        1,
        {
            "source": "quoka_de",
            "key": "q",
            "url": QUOKA_SEARCH,
            "max_pages": 1,
            "detail_limit": 1,
            "request_limit": 2,
            "quoka_own_details": False,
        },
    )
    assert len(collector.observed) == ledger["target_count"] == 1
    own = collector.observed[0]
    assert (own.source, own.external_id) == ("autoscout24_de", AS_ID)
    assert own.raw["discovery_origin"]["source"] == "quoka_de"
    assert ledger["expected_count"] == 751 and ledger["received_count"] == 20
    assert all(x["market_country"] == "UNKNOWN" for x in ledger["cards"].values())
    assert ledger["status"] == "partial" and collector.db.finished["infer_missing"] is False
    assert {x[0] for x in collector.retained} == {"quoka_de", "autoscout24_de"}


@pytest.mark.parametrize("problem", ["foreign", "identity", "blocked"])
def test_native_unknown_foreign_redirect_or_block_does_not_write_identity(problem):
    def responder(url):
        if "quoka.de" in url:
            return response(url, linked_search())
        body = (AS_FIXTURES / AS_DETAIL).read_text()
        if problem == "foreign":
            body = body.replace('"country":"DE"', '"country":"NL"').replace(
                '"countryCode":"DE"', '"countryCode":"NL"'
            )
            # Independent of fixture formatting: the parser cannot establish DE.
            return response(url, body)
        if problem == "identity":
            return response(
                url, body, final=url.replace(AS_ID, "c7564ef4-c21e-4f86-9007-d35234728a54")
            )
        return response(url, "Forbidden", status=403, problem="http_403")

    collector = OfflineCollector(responder)
    if problem == "foreign":
        original = collector._parse

        def foreign_parse(source, body, url):
            parsed = original(source, body, url)
            for item in parsed.listings:
                item.raw.pop("market_country", None)
            return parsed

        collector._parse = foreign_parse
    ledger = collect_discovery(
        collector,
        1,
        {
            "source": "quoka_de",
            "key": "q",
            "url": QUOKA_SEARCH,
            "max_pages": 1,
            "detail_limit": 1,
            "quoka_own_details": False,
        },
    )
    assert not collector.observed and ledger["target_count"] == 0
    assert ledger["status"] == "partial" and collector.db.finished["infer_missing"] is False
    assert any(x["status"] == "unconfirmed" for x in ledger["detail_results"].values())


def test_twelve_partner_redirect_is_saved_then_native_request_has_own_provenance():
    def responder(url):
        if "/c/partner" in url:
            return response(url, "Redirected native page", final=AS_MANIFEST[AS_DETAIL]["url"])
        if "12gebrauchtwagen.de" in url:
            return response(url, (FIXTURES / "12gebrauchtwagen-base.html").read_text())
        return response(url, (AS_FIXTURES / AS_DETAIL).read_text())

    collector = OfflineCollector(responder)
    ledger = collect_discovery(
        collector,
        1,
        {
            "source": "gebrauchtwagen_de",
            "key": "t",
            "url": GEBRAUCHTWAGEN_SEARCH,
            "max_pages": 1,
            "detail_limit": 1,
            "request_limit": 3,
        },
    )
    assert ledger["target_count"] == 1 and len(collector.requests) == 3
    assert collector.retained[1][0] == "gebrauchtwagen_de"
    assert collector.retained[1][1].url.endswith("offer_id=385390779")
    assert collector.retained[2][0] == "autoscout24_de"
    assert collector.retained[2][1].url == AS_MANIFEST[AS_DETAIL]["url"]
    assert collector.observed[0].source == "autoscout24_de"
    assert "source_counter_conflict" in ledger["errors"]


def test_carwow_language_only_response_is_retained_without_invented_de_stock():
    def responder(url):
        if "/c/partner" in url:
            return response(
                url,
                "German language, Deutschland",
                final="https://angebote.carwow.de/deals/2d152d8d9def83030c91adc779b99071",
            )
        return response(url, (FIXTURES / "12gebrauchtwagen-base.html").read_text())

    collector = OfflineCollector(responder)
    ledger = collect_discovery(
        collector,
        1,
        {
            "source": "gebrauchtwagen_de",
            "key": "t",
            "url": GEBRAUCHTWAGEN_SEARCH,
            "max_pages": 1,
            "detail_limit": 1,
        },
    )
    assert ledger["target_count"] == 0 and not collector.observed
    assert len(collector.requests) == 3 and len(collector.retained) == 3
    assert any("native_own_identity_unverified" in x for x in ledger["errors"])


def test_retained_frontier_with_zero_detail_budget_never_creates_identity():
    collector = OfflineCollector(
        lambda url: response(url, (FIXTURES / "quoka-search.html").read_text())
    )
    ledger = collect_discovery(
        collector,
        1,
        {"source": "quoka_de", "key": "q", "url": QUOKA_SEARCH, "max_pages": 1, "detail_limit": 0},
    )
    assert ledger["received_count"] == 20 and ledger["target_count"] == 0
    assert not collector.observed and len(collector.requests) == 1
    stored = json.loads(collector.db.conn.execute("SELECT value FROM monitor_state").fetchone()[0])
    assert len(stored["cards"]) == 20


@pytest.mark.parametrize(
    "options",
    [{"request_limit": True}, {"request_limit": 1001}, {"detail_limit": -1}, {"max_pages": 201}],
)
def test_invalid_budgets_do_not_request_any_source(options):
    collector = OfflineCollector(lambda url: pytest.fail("No request allowed"))
    with pytest.raises(ValueError):
        collect_discovery(
            collector, 1, {"source": "quoka_de", "key": "q", "url": QUOKA_SEARCH, **options}
        )
    assert not collector.requests


def test_fresh_quoka_html_entities_are_decoded_without_losing_original_name():
    own = retained("quoka-own-encoded-title.html").listings[0]
    assert "TÜV NEU" in own.title and own.scope == "target"
    assert "T&#220;V NEU" in own.raw["own_payload"]["name"]
    assert own.raw["market_country"] == "DE" and own.kenteken is None


def test_fresh_quoka_native_partitions_keep_query_and_native_independent_counts():
    body = (FIXTURES / "quoka-native-partitions.html").read_text()
    partitions = quoka_native_partitions(body, QUOKA_SEARCH)
    assert [(p["commercial"], p["native_count"]) for p in partitions] == [
        ("false", 94),
        ("true", 668),
    ]
    assert all(
        parse_qs(urlsplit(p["url"]).query)["q"] == ["bmw 218i active tourer"] for p in partitions
    )
    assert quoka_native_partitions(body, QUOKA_SEARCH.replace("218i", "220i")) == []
    assert quoka_native_partitions(body.replace("668", "669", 1), QUOKA_SEARCH) == []


def test_carwow_own_price_locality_and_id_are_preserved_without_domain_country_inference():
    name = "carwow-own-country-unknown.html"
    url = MANIFEST[name]["final_url"]
    own = parse_discovery_publisher(
        "dealer:angebote.carwow.de", (FIXTURES / name).read_text(), url
    ).listings[0]
    assert (own.price, own.price_type, own.city, own.scope) == (
        39900,
        "advertised_price",
        "Rostock",
        "needs_confirmation",
    )
    assert own.mileage is None and own.raw["mileage_label"] == "< 100 km"
    assert own.year is None and own.vin is None and "market_country" not in own.raw
    assert "619,00" in own.raw["leasing_label"]
    assert own.external_id == own.url == url.split("?")[0]
    assert own.raw["own_deal_id"] == "539f4327a07030e149e7d45d14f40053"


@pytest.mark.parametrize("problem", ["id", "duplicate_id", "cash_link", "net", "duplicate_fact"])
def test_carwow_mutations_cannot_replace_item_or_confuse_cash_with_monthly(problem):
    name = "carwow-own-country-unknown.html"
    soup = BeautifulSoup((FIXTURES / name).read_text(), "html.parser")
    if problem == "id":
        soup.select_one(".deal-id").string = "Angebotsnummer: " + "a" * 32
    elif problem == "duplicate_id":
        soup.body.append(BeautifulSoup(str(soup.select_one(".deal-id")), "html.parser"))
    elif problem == "cash_link":
        soup.select_one('[data-interaction-element="Cash"]')["data-deal-path"] = (
            "/deals/" + "a" * 32
        )
    elif problem == "net":
        soup.select_one(".deal-pricing__tax-rate").string = "netto zzgl. MwSt"
    else:
        row = soup.select_one(".key-details .summary-list__item")
        row.parent.append(BeautifulSoup(str(row), "html.parser"))
    page = parse_discovery_publisher(
        "dealer:angebote.carwow.de", str(soup), MANIFEST[name]["final_url"]
    )
    if problem in {"id", "duplicate_id", "duplicate_fact"}:
        assert not page.listings
    elif problem == "cash_link":
        assert page.listings[0].price is None
    else:
        assert page.listings[0].price_type == "net_price"


def test_autoland_native_route_and_bound_own_country_are_supported():
    name = "autoland-own.html"
    url = MANIFEST[name]["final_url"]
    ref = publisher_reference(url + "?utm_source=12gebrauchtwagen")
    assert (ref["source"], ref["url"], ref["external_id"]) == ("dealer:autoland.de", url, url)
    own = parse_discovery_publisher(
        ref["source"], (FIXTURES / name).read_text(), ref["url"]
    ).listings[0]
    assert own.scope == "target" and own.raw["market_country"] == "DE"
    assert own.raw["country_evidence"]["addressCountry"] == ["DE"]
    assert own.vin is None  # Native schema hex ID is not a proved VIN.
    assert own.raw["dealer_stock_id"] == "103897"
    assert publisher_reference("https://autoland.de/bmw/2er-reihe") is None
    assert (
        publisher_reference("https://angebote.carwow.de/deals/" + "a" * 32 + "/enquiry/new") is None
    )


@pytest.mark.parametrize("status", [403, 429])
def test_source_block_stops_same_origin_details_and_additional_pages(status):
    def responder(url):
        if "pag=2" in url:
            return response(url, "Blocked", status=status)
        return response(url, (FIXTURES / "quoka-search.html").read_text())

    collector = OfflineCollector(responder)
    ledger = collect_discovery(
        collector,
        1,
        {
            "source": "quoka_de",
            "key": "blocked",
            "url": QUOKA_SEARCH,
            "max_pages": 3,
            "detail_limit": 20,
        },
    )
    assert len(collector.requests) == ledger["requests"] == 2
    assert ledger["detail_attempts"] == 0 and not collector.observed
    assert ledger["received_count"] == 20 and ledger["status"] == "partial"


def test_real_own_quoka_replay_writes_verified_item_to_temporary_german_database(tmp_path):
    from car_market.collect import Collector
    from car_market.db import Database

    name = "quoka-own-active.html"
    url = MANIFEST[name]["final_url"]
    # Fresh native card belongs to the real own detail, no invented publisher link.
    fresh = retained("quoka-native-partitions.html")
    assert fresh.listings[0].url == url
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"scope": {"country": "DE"}})
    requested = []

    def get(uri):
        requested.append(uri)
        if "/anzeige/" in uri:
            return Response(
                uri, url, 200, (FIXTURES / name).read_text(), MANIFEST[name]["fetched_at"]
            )
        return Response(
            uri,
            uri,
            200,
            (FIXTURES / "quoka-native-partitions.html").read_text(),
            MANIFEST["quoka-native-partitions.html"]["fetched_at"],
        )

    collector.fetcher = SimpleNamespace(get=get)
    run = db.start_run("replay")
    ledger = collect_discovery(
        collector,
        run,
        {
            "source": "quoka_de",
            "key": "own",
            "url": QUOKA_SEARCH,
            "max_pages": 1,
            "detail_limit": 1,
            "request_limit": 2,
        },
    )
    assert len(requested) == ledger["requests"] == 2
    assert ledger["target_count"] == ledger["observed_count"] == 1
    row = db.conn.execute("SELECT * FROM observations").fetchone()
    item = json.loads(row["details_json"])
    assert item["source"] == "quoka_de" and item["raw"]["market_country"] == "DE"
    assert row["observed_at"] == MANIFEST[name]["fetched_at"]  # Replay keeps source time.
    assert item["raw"]["own_publication_reference"]["url"] == url
    assert (
        db.conn.execute("SELECT count(*) FROM vehicles WHERE kenteken IS NOT NULL").fetchone()[0]
        == 0
    )
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (
        db.conn.execute("SELECT count(*) FROM fetches WHERE raw_path IS NOT NULL").fetchone()[0]
        == 2
    )
    db.conn.close()


def test_real_last_quoka_page_keeps_literal_newlines_in_native_json_strings():
    body = (FIXTURES / "quoka-private-last.html").read_text()
    script = BeautifulSoup(body, "html.parser").select_one('script[type="application/ld+json"]')
    with pytest.raises(ValueError):
        json.loads(script.get_text())
    page = retained("quoka-private-last.html")
    assert (page.total, page.page, len(page.listings)) == (94, 5, 14)
    assert all(x.raw.get("market_country") is None for x in page.listings)
    # An actually truncated document cannot become an item list.
    broken = body.replace('"itemListElement"', '"itemListElement', 1)
    assert (
        parse_discovery(
            "quoka_de", broken, MANIFEST["quoka-private-last.html"]["final_url"]
        ).listings
        == []
    )


def test_real_weller_publisher_binds_native_id_seller_country_and_own_full_vin():
    name = "weller-own.html"
    url = MANIFEST[name]["final_url"]
    own = parse_discovery_publisher(
        "dealer:wellergruppe.de", (FIXTURES / name).read_text(), url
    ).listings[0]
    assert (own.price, own.year, own.mileage, own.fuel, own.transmission, own.scope) == (
        23490,
        2022,
        28001,
        "petrol",
        "automatic",
        "target",
    )
    assert own.external_id == url.rstrip("/")
    assert own.vin == "WBA21BX0207L72032" and own.kenteken is None
    assert own.raw["dealer_stock_id"] == "14170260"
    assert own.raw["own_inventory_record"]["vehicleId"] == 636906219
    assert own.raw["country_evidence"]["addressCountry"] == ["DE"]
    assert own.price_type == "advertised_price"
    assert own.raw["own_inventory_record"]["purchaseOptions"]["financingMin"] == 219


@pytest.mark.parametrize(
    "problem", ["native_id", "route_id", "stock_id", "foreign", "vin", "canonical", "locality"]
)
def test_weller_mutations_cannot_borrow_country_identity_or_vin_from_another_record(problem):
    name = "weller-own.html"
    soup = BeautifulSoup((FIXTURES / name).read_text(), "html.parser")
    node = soup.select_one("script#__NEXT_DATA__")
    state = json.loads(node.get_text())
    own = state["props"]["pageProps"]["vehicle"]
    if problem == "native_id":
        own["vehicleId"] += 1
    elif problem == "route_id":
        state["query"]["vehicleId"] = "123456789"
    elif problem == "stock_id":
        own["meta"]["sourceId"] = "123456789"
    elif problem == "foreign":
        ld = soup.select_one('script[type="application/ld+json"]')
        vehicle = json.loads(ld.get_text())
        vehicle["offers"][0]["seller"]["address"]["addressCountry"] = "NL"
        ld.string = json.dumps(vehicle)
    elif problem == "vin":
        own["meta"]["vin"] = "WBA21BX0X07T40980"
    elif problem == "canonical":
        soup.select_one('link[rel="canonical"]')["href"] = "https://wellergruppe.de/"
    else:
        own["locations"][0]["city"] = "Amsterdam"
    node.string = json.dumps(state)
    page = parse_discovery_publisher(
        "dealer:wellergruppe.de", str(soup), MANIFEST[name]["final_url"]
    )
    if problem == "foreign":
        assert len(page.listings) == 1 and page.listings[0].scope == "excluded"
        assert page.listings[0].raw["market_country"] == "NL"
    else:
        assert not page.listings


def test_weller_net_and_conflicting_price_flags_do_not_become_gross_purchase_price():
    name = "weller-own.html"
    for problem in ("net", "conflict"):
        soup = BeautifulSoup((FIXTURES / name).read_text(), "html.parser")
        node = soup.select_one("script#__NEXT_DATA__")
        state = json.loads(node.get_text())
        price = state["props"]["pageProps"]["vehicle"]["purchaseOptions"]["price"]
        if problem == "net":
            price.update(vatInclusive=False, vatType="MwSt")
        else:
            price["value"] += 1000
        node.string = json.dumps(state)
        own = parse_discovery_publisher(
            "dealer:wellergruppe.de", str(soup), MANIFEST[name]["final_url"]
        ).listings[0]
        assert own.price == 23490
        if problem == "net":
            assert own.price_type == "net_price" and own.raw["price_warning"] == "net_price"
        else:
            assert own.raw["price_warning"] == "own_price_conflict"
            assert own.raw["field_conflicts"]["price"] == {"structured": 23490, "native": 24490}


def test_quoka_bound_payload_cannot_use_another_own_ad_url_with_the_same_title():
    name = "quoka-own-active.html"
    soup = BeautifulSoup((FIXTURES / name).read_text(), "html.parser")
    body = replace_document(
        soup, lambda x: x.update(url=MANIFEST["quoka-detail.html"]["final_url"])
    )
    assert not parse_discovery("quoka_de", body, MANIFEST[name]["final_url"]).listings


def test_rate_limit_is_shared_by_native_queries_in_the_same_collector_pass():
    collector = OfflineCollector(lambda url: response(url, "Blocked", status=429))
    first = collect_discovery(
        collector,
        1,
        {
            "source": "quoka_de",
            "key": "private",
            "url": QUOKA_SEARCH + "&commercial=false",
            "max_pages": 1,
            "detail_limit": 1,
        },
    )
    second = collect_discovery(
        collector,
        1,
        {
            "source": "quoka_de",
            "key": "commercial",
            "url": QUOKA_SEARCH + "&commercial=true",
            "max_pages": 1,
            "detail_limit": 1,
        },
    )
    assert len(collector.requests) == first["requests"] == 1
    assert second["requests"] == 0 and "search:1:discovery_origin_blocked" in second["errors"]
    assert second["status"] == "partial" and not collector.observed
