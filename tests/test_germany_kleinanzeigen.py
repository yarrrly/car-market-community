"""Real own responses and the boundary from discovery to DE observation."""

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from car_market.db import SCHEMA, Database
from car_market.fetch import Response
from car_market.germany import observe_german_item
from car_market.germany_kleinanzeigen import (
    GERMAN_STATES,
    collect_kleinanzeigen,
    kleinanzeigen_page_url,
    parse_kleinanzeigen,
)

FIXTURES = Path(__file__).parent / "fixtures" / "germany_kleinanzeigen"
MANIFEST = {x["fixture"]: x for x in json.loads((FIXTURES / "manifest.json").read_text())}
AD_ID = "3534143984"
SEARCH = MANIFEST["search.html"]["url"]
DETAIL = MANIFEST["detail.html"]["url"]
STAMP = "2026-10-07T20:02:16+00:00"


def body(name):
    return (FIXTURES / name).read_text()


def page(name):
    return parse_kleinanzeigen(body(name), MANIFEST[name]["final_url"])


def detail_soup():
    return BeautifulSoup(body("detail.html"), "html.parser")


def parsed_detail(soup):
    return parse_kleinanzeigen(str(soup), DETAIL)


def test_fixture_lineage_and_real_native_counters():
    for name, origin in MANIFEST.items():
        assert origin["status"] == 200 and origin["fetched_at"].startswith("2026-10-07")
        assert (
            hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == origin["fixture_sha256"]
        )
        assert len(origin["original_decoded_sha256"]) == 64
    first, second = page("search.html"), page("page2.html")
    assert (first.total, first.pages, first.page, len(first.listings)) == (598, 24, 1, 25)
    assert (second.total, second.page, len(second.listings)) == (598, 2, 25)
    assert first.search_filters["declared_range"] == [1, 25, 598]
    assert second.search_filters["declared_range"] == [26, 50, 598]
    assert not {x.external_id for x in first.listings} & {x.external_id for x in second.listings}


def test_search_is_unknown_country_discovery_even_with_german_image_alt():
    first = page("search.html")
    assert first.search_filters["country"] == "UNKNOWN"
    assert all(x.raw["market_country"] == "UNKNOWN" and x.kenteken is None for x in first.listings)
    own = first.listings[0]
    assert own.external_id == AD_ID and own.url == DETAIL
    assert (own.price, own.mileage, own.year, own.scope) == (
        24420,
        21650,
        2024,
        "needs_confirmation",
    )
    assert own.raw["country_evidence"]["image_alt_claims"]
    assert own.raw["placement"] == "unknown"
    assert first.listings[2].raw["placement"] == "unknown"  # No native tracker; do not guess.


def test_actual_last_and_after_last_keep_raw_ads_without_false_exhaustion():
    last, after = page("last.html"), page("after-last.html")
    assert (last.page, last.total, len(last.listings)) == (24, 598, 25)
    assert last.search_filters["declared_range"] == [576, 598, 598]
    assert last.search_filters["range_valid"] is True
    assert len(last.listings) > 598 - 576 + 1
    assert (after.page, len(after.listings)) == (25, 25)
    assert after.search_filters["declared_range"] == [601, 598, 598]
    assert after.search_filters["range_valid"] is False
    assert (
        len({x.external_id for x in last.listings} & {x.external_id for x in after.listings}) >= 20
    )


def test_pagination_native_path_preserves_query_and_native_counter_detects_replay():
    url = SEARCH + "?sortingField=SORTING_DATE"
    assert (
        kleinanzeigen_page_url(url, 2)
        == SEARCH.replace("/s-autos/", "/s-autos/seite:2/") + "?sortingField=SORTING_DATE"
    )
    assert kleinanzeigen_page_url(kleinanzeigen_page_url(url, 24), 1) == url
    replay = parse_kleinanzeigen(body("search.html"), kleinanzeigen_page_url(SEARCH, 2))
    assert replay.page == 1 and replay.search_filters["route_page"] == 2
    for value in [0, -1, True, "2"]:
        with pytest.raises(ValueError):
            kleinanzeigen_page_url(url, value)
    with pytest.raises(ValueError):
        kleinanzeigen_page_url(DETAIL, 2)


def test_real_own_detail_model_price_service_photos_and_country_proof():
    result = page("detail.html")
    assert (result.total, len(result.listings)) == (1, 1)
    item = result.listings[0]
    assert (
        item.external_id,
        item.source,
        item.price,
        item.mileage,
        item.year,
        item.fuel,
        item.transmission,
        item.scope,
    ) == (AD_ID, "kleinanzeigen_de", 24420, 21650, 2024, "petrol", "automatic", "target")
    assert item.kenteken is None and item.vin is None
    assert len(item.images) == 24
    assert item.seller == "ahg Autohandelsgesellschaft mbH Pforzheim"
    assert "Scheckheftgepflegt" in item.description and "Garantie" in item.description
    assert item.raw["detail_complete"] is True
    assert item.raw["country_evidence"]["mapping"] == {"Baden-Württemberg": "DE"}
    assert item.raw["market_country"] == result.search_filters["country"] == "DE"
    assert item.raw["publication_reference"]["url"] == DETAIL
    assert "sold" not in item.raw  # data-soldlabel is a template, not an observed status.


@pytest.mark.parametrize("state", GERMAN_STATES)
def test_all_16_states_are_derived_only_from_own_location(state):
    soup = detail_soup()
    soup.select_one("#viewad-locality").string = "12345 " + state + " - Own locality"
    item = parsed_detail(soup).listings[0]
    assert item.raw["market_country"] == "DE"
    assert item.raw["country_evidence"]["mapping"] == {state: "DE"}


@pytest.mark.parametrize(
    "place,country",
    [
        ("1012 Amsterdam", "UNKNOWN"),
        ("Nederland - Amsterdam", "NL"),
        ("Österreich - Wien", "AT"),
        ("NL - Bayern", "CONFLICT"),
        ("75179 Deutschland - Pforzheim", "DE"),
    ],
)
def test_own_location_missing_or_foreign_never_borrows_dealer_country(place, country):
    soup = detail_soup()
    soup.select_one("#viewad-locality").string = place
    seller = soup.select_one("#viewad-contact")
    seller.append(
        BeautifulSoup('<span itemprop="addressCountry">DE</span> Bayern Deutschland', "html.parser")
    )
    item = parsed_detail(soup).listings[0]
    assert item.raw["market_country"] == country
    if country == "UNKNOWN":
        assert item.scope == "needs_confirmation"
    elif country != "DE":
        assert item.scope == "excluded"


def test_own_location_breadcrumb_can_prove_state_but_related_cards_cannot():
    soup = detail_soup()
    soup.select_one("#viewad-locality").string = "12345 Own locality"
    geo = soup.select_one('#vap-brdcrmb a[href^="/stadt/"]')
    geo.string = "Kleinanzeigen Bayern - Own locality"
    assert parsed_detail(soup).listings[0].raw["market_country"] == "DE"
    geo.string = "Kleinanzeigen Own locality"
    soup.select_one("article[data-adid]").append(
        BeautifulSoup('<span id="related-location">Bayern Deutschland</span>', "html.parser")
    )
    assert parsed_detail(soup).listings[0].raw["market_country"] == "UNKNOWN"


@pytest.mark.parametrize("change", ["canonical", "idbox", "missing_root", "duplicate_root"])
def test_current_own_identity_cannot_fall_back_to_recommendations(change):
    soup = detail_soup()
    if change == "canonical":
        soup.select_one('link[rel="canonical"]')["href"] = DETAIL.replace(AD_ID, "3533915315")
    elif change == "idbox":
        soup.select_one("#viewad-ad-id-box").string = "Anzeigen-ID 3533915315"
    elif change == "missing_root":
        soup.select_one("#viewad-main-info").decompose()
    else:
        soup.append(BeautifulSoup(str(soup.select_one("#viewad-main-info")), "html.parser"))
    assert parsed_detail(soup).listings == []


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/s-anzeige/foo/3534143984-216-9267",
        "https://evil@www.kleinanzeigen.de/s-anzeige/foo/3534143984-216-9267",
        "https://www.kleinanzeigen.de:8443/s-anzeige/foo/3534143984-216-9267",
        "https://www.kleinanzeigen.de/s-autos/3534143984",
    ],
)
def test_foreign_and_non_native_routes_rejected(url):
    assert parse_kleinanzeigen(body("detail.html"), url).listings == []


def test_full_own_vin_only_and_visible_status_only():
    soup = detail_soup()
    desc = soup.select_one("#viewad-description-text")
    desc.append(" FIN: WBA2A31030V123456")
    assert parsed_detail(soup).listings[0].vin == "WBA2A31030V123456"
    desc.append(" VIN: WBA2A31030V654321")
    assert parsed_detail(soup).listings[0].vin is None
    status = soup.new_tag("span", id="viewad-status")
    status.string = "Verkauft"
    soup.select_one("#viewad-main-info").append(status)
    assert parsed_detail(soup).listings[0].raw["sold"] is True


def test_detail_monthly_and_net_terms_are_not_cash():
    for label, kind in [
        ("299 € / Monat", "monthly_price"),
        ("19.000 € exkl. MwSt.", "net_price"),
        ("Leasing 299 €", "lease_price"),
    ]:
        soup = detail_soup()
        soup.select_one("#viewad-price").string = label
        assert parsed_detail(soup).listings[0].price_type == kind


class MemoryCollector:
    def __init__(self, responses):
        self.country, self.config, self.failures = "DE", {}, []
        self.db = Database.__new__(Database)
        self.db._in_transaction = False
        self.db.conn = sqlite3.connect(":memory:")
        self.db.conn.row_factory = sqlite3.Row
        self.db.conn.executescript(SCHEMA)
        self.db.conn.execute("CREATE TABLE monitor_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.calls, self.observed = [], []
        self.responses = responses
        self.fetcher = SimpleNamespace(get=self.fetch)

    def fetch(self, url):
        self.calls.append(url)
        return self.responses.get(url, Response(url, url, 403, "Access denied", STAMP, "http_403"))

    def fetch_record(self, run, source, response, retain_raw=True):
        assert retain_raw is True
        cursor = self.db.conn.execute(
            "INSERT INTO fetches(run_id,source,url,final_url,fetched_at,status,content_hash,transport,error) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run,
                source,
                response.url,
                response.final_url,
                response.timestamp,
                response.status,
                hashlib.sha256((response.body or "").encode()).hexdigest(),
                response.transport,
                response.error,
            ),
        )
        self.db.conn.commit()
        return cursor.lastrowid

    def _observe(self, run, item, stamp, fetch_id=None, key=None):
        assert item.raw["market_country"] == "DE"
        self.observed.append(item)
        return observe_german_item(self.db, run, item, stamp, fetch_id, key)


def response(name, text=None):
    url = MANIFEST[name]["url"]
    return Response(url, url, 200, body(name) if text is None else text, STAMP)


def query(**kwargs):
    return dict(
        source="kleinanzeigen_de",
        key="external_real",
        url=SEARCH,
        max_pages=1,
        detail_limit=1,
        **kwargs,
    )


def test_special_collector_retains_discovery_then_observes_one_real_country_proven_car():
    collector = MemoryCollector({SEARCH: response("search.html"), DETAIL: response("detail.html")})
    run = collector.db.start_run(timestamp=STAMP)
    ledger = collect_kleinanzeigen(collector, run, query())
    assert collector.calls == [SEARCH, DETAIL]
    assert ledger["status"] == "partial"
    assert (
        ledger["expected_count"],
        ledger["received_count"],
        ledger["target_count"],
        ledger["detail_attempts"],
    ) == (598, 25, 1, 1)
    assert ledger["cards"][AD_ID]["market_country"] == "UNKNOWN"
    assert ledger["detail_results"][AD_ID]["market_country"] == "DE"
    stored = json.loads(
        collector.db.conn.execute(
            "SELECT value FROM monitor_state WHERE key='kleinanzeigen_discovery:external_real'"
        ).fetchone()[0]
    )
    assert stored["pages"][0]["fetch_id"] and len(stored["pages"][0]["organic_candidate_ids"]) == 25
    assert collector.db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert collector.db.conn.execute("SELECT kenteken FROM vehicles").fetchone()[0] is None
    assert "pagination_not_certified" in ledger["errors"]
    assert collector.db.conn.execute("SELECT status FROM source_runs").fetchone()[0] == "partial"


def test_unknown_country_detail_is_raw_discovery_only_with_zero_identity_writes():
    soup = detail_soup()
    soup.select_one("#viewad-locality").string = "12345 Own locality"
    collector = MemoryCollector(
        {SEARCH: response("search.html"), DETAIL: response("detail.html", str(soup))}
    )
    run = collector.db.start_run(timestamp=STAMP)
    ledger = collect_kleinanzeigen(collector, run, query())
    assert ledger["received_count"] == 25 and ledger["target_count"] == 0
    assert ledger["detail_results"][AD_ID]["status"] == "unconfirmed"
    assert collector.observed == []
    assert collector.db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0
    assert collector.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert collector.db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 2


def test_detail_redirect_and_budget_fail_closed_but_keep_native_search_ledger():
    wrong = Response(DETAIL, DETAIL.replace(AD_ID, "3533915315"), 200, body("detail.html"), STAMP)
    collector = MemoryCollector({SEARCH: response("search.html"), DETAIL: wrong})
    run = collector.db.start_run(timestamp=STAMP)
    ledger = collect_kleinanzeigen(collector, run, query())
    assert "detail_redirect_identity_mismatch" in ledger["detail_results"][AD_ID]["error"]
    assert not collector.observed
    collector = MemoryCollector({SEARCH: response("search.html")})
    run = collector.db.start_run(timestamp=STAMP)
    ledger = collect_kleinanzeigen(collector, run, query(request_limit=1))
    assert collector.calls == [SEARCH] and ledger["requests"] == 1
    assert ledger["detail_attempts"] == 0 and "request_limit" in ledger["errors"]


def test_failed_search_is_not_zero_or_complete_and_wrong_country_collector_cannot_start():
    collector = MemoryCollector({})
    run = collector.db.start_run(timestamp=STAMP)
    ledger = collect_kleinanzeigen(collector, run, query())
    assert ledger["status"] == "partial" and ledger["expected_count"] is None
    assert collector.db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    collector.country = "NL"
    with pytest.raises(ValueError):
        collect_kleinanzeigen(collector, run, query())


def test_two_real_search_pages_enumerate_control_ids_without_unknown_observations():
    second_url = kleinanzeigen_page_url(SEARCH, 2)
    collector = MemoryCollector(
        {
            SEARCH: response("search.html"),
            second_url: response("page2.html"),
            DETAIL: response("detail.html"),
        }
    )
    run = collector.db.start_run(timestamp=STAMP)
    settings = query()
    settings["max_pages"] = 2
    ledger = collect_kleinanzeigen(collector, run, settings)
    assert collector.calls == [SEARCH, second_url, DETAIL]
    assert len(ledger["pages"]) == 2 and ledger["received_count"] == 50
    assert ledger["target_count"] == 1
    assert all(x["market_country"] == "UNKNOWN" for x in ledger["cards"].values())
    assert len(collector.observed) == 1 and collector.observed[0].raw["market_country"] == "DE"


def test_explicit_promotion_marker_is_retained_separately_from_unmarked_candidates():
    soup = BeautifulSoup(body("search.html"), "html.parser")
    soup.select_one("article[data-adid]")["data-placement"] = "promoted"
    collector = MemoryCollector(
        {SEARCH: response("search.html", str(soup)), DETAIL: response("detail.html")}
    )
    run = collector.db.start_run(timestamp=STAMP)
    ledger = collect_kleinanzeigen(collector, run, query())
    assert ledger["pages"][0]["promoted_ids"] == []
    assert ledger["cards"][AD_ID]["raw"]["display_promotion_hint"] is True
    assert len(ledger["pages"][0]["unknown_placement_ids"]) == 25
    assert "organic_range_not_reconciled:1" in ledger["errors"]
    assert ledger["status"] == "partial"


def native_soup():
    return BeautifulSoup(body("active-date-first.html"), "html.parser")


def native_props(soup):
    node = next(x for x in soup.select("astro-island[props]") if "resultAds" in x["props"])
    return node, json.loads(node["props"])


def test_real_native_27_cards_are_2_top_plus_25_organic_and_last_page_is_7():
    first, last = page("active-date-first.html"), page("218i-date-last.html")
    assert first.search_filters["promotion_error"] is None
    assert (first.total, len(first.listings)) == (2215, 27)
    assert [x.external_id for x in first.listings if x.raw["placement"] == "promoted"] == [
        "3455523509",
        "3480469884",
    ]
    assert len([x for x in first.listings if x.raw["placement"] == "organic"]) == 25
    assert all(x.raw["market_country"] == "UNKNOWN" for x in first.listings)
    assert first.listings[0].raw["native_preview"]["topAd"] is True
    assert first.listings[0].raw["native_preview"]["appliedFeatures"] == ["TOP_AD"]
    assert first.listings[0].raw["native_preview"]["own_location_claims"]["locationName"] == "71334"
    assert (last.total, last.page, len(last.listings)) == (1082, 44, 7)
    assert all(x.raw["placement"] == "organic" for x in last.listings)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_tracker",
        "duplicate_id",
        "wrong_uri",
        "wrong_id",
        "unsupported_tag",
        "boolean_string",
        "missing_top",
        "missing_row",
    ],
)
def test_ambiguous_native_promotion_keeps_discovery_but_never_certifies_organic(mutation):
    soup = native_soup()
    node, props = native_props(soup)
    row = props["resultAds"][1][0][1]["organicAdPreview"][1]
    if mutation == "duplicate_tracker":
        soup.append(BeautifulSoup(str(node), "html.parser"))
    elif mutation == "unsupported_tag":
        props["resultAds"][0] = 9
    elif mutation == "missing_row":
        props["resultAds"][1].pop(0)
    elif mutation == "duplicate_id":
        other = props["resultAds"][1][1][1]["organicAdPreview"][1]
        other["id"] = row["id"]
    elif mutation == "wrong_uri":
        row["seoLink"] = [0, "https://evil.example/3534143984"]
    elif mutation == "wrong_id":
        row["id"] = [0, 3534143984]
    elif mutation == "boolean_string":
        row["topAd"] = [0, "true"]
    else:
        del row["topAd"]
    if mutation != "duplicate_tracker":
        node["props"] = json.dumps(props)
    parsed = parse_kleinanzeigen(str(soup), MANIFEST["active-date-first.html"]["url"])
    assert len(parsed.listings) == 27
    assert parsed.search_filters["promotion_error"]
    assert all(
        x.raw["placement"] == "unknown" and not x.raw["promotion_verified"] for x in parsed.listings
    )


def test_native_country_field_is_retained_as_claim_without_inventing_de_stock():
    soup = native_soup()
    node, props = native_props(soup)
    props["resultAds"][1][0][1]["organicAdPreview"][1]["countryCode"] = [0, "DE"]
    node["props"] = json.dumps(props)
    parsed = parse_kleinanzeigen(str(soup), MANIFEST["active-date-first.html"]["url"])
    assert parsed.listings[0].raw["native_preview"]["own_location_claims"]["countryCode"] == "DE"
    assert parsed.listings[0].raw["market_country"] == "UNKNOWN"


def test_sorted_native_redirect_path_is_supported_without_accepting_other_sorts():
    url = "https://www.kleinanzeigen.de/s-autos/sortierung:neuste/seite:44/bmw-218i/k0c216?tracking=kept"
    assert kleinanzeigen_page_url(url, 2) == url.replace("seite:44", "seite:2")
    assert kleinanzeigen_page_url(url, 1) == url.replace("seite:44/", "")
    parsed = parse_kleinanzeigen(body("218i-date-last.html"), url)
    assert (parsed.page, len(parsed.listings)) == (44, 7)
    with pytest.raises(ValueError):
        kleinanzeigen_page_url(url.replace("neuste", "unknown"), 2)


def test_complete_uses_native_organic_unique_count_instead_of_all_promoted_ids(monkeypatch):
    from car_market import germany_kleinanzeigen as adapter

    soup = native_soup()
    soup.select_one("#srp-breadcrumb-summary").string = "1 - 25 von 25 Gebrauchtwagen"
    url = MANIFEST["active-date-first.html"]["url"]
    collector = MemoryCollector({url: Response(url, url, 200, str(soup), STAMP)})
    settings = {
        "source": "kleinanzeigen_de",
        "key": "native_control",
        "url": url,
        "max_pages": 1,
        "detail_limit": 1,
        "pagination_certified": True,
    }
    monkeypatch.setattr(
        adapter, "_relevant", lambda title: False
    )  # Verify the ledger for control-only responses.
    run = collector.db.start_run(timestamp=STAMP)
    ledger = collect_kleinanzeigen(collector, run, settings)
    assert (ledger["received_count"], ledger["organic_count"], ledger["expected_count"]) == (
        27,
        25,
        25,
    )
    assert ledger["status"] == "complete" and ledger["details_reconciled"] is True
    settings["pagination_certified"] = False
    run = collector.db.start_run(timestamp=STAMP)
    assert collect_kleinanzeigen(collector, run, settings)["status"] == "partial"


@pytest.mark.parametrize(
    "identity,publisher",
    [
        ("3526227598", "e78eddba-9e3b-496a-bbdf-8034025025ee"),
        ("3533829352", "6bd1e96d-39eb-448c-ad85-01b9b85d738d"),
    ],
)
def test_real_own_autohero_primary_link_preserves_self_and_queues_publisher(identity, publisher):
    item = page("autohero-syndication-" + identity + ".html").listings[0]
    reference = item.raw["publication_reference"]
    assert reference == {
        "source": "autohero_de",
        "url": "https://www.autohero.com/de/bmw-2-er/id/" + publisher + "/",
        "external_id": publisher,
        "kind": "explicit_own_publication_link",
    }
    assert item.raw["own_publication_reference"]["source"] == "kleinanzeigen_de"
    assert item.raw["own_publication_reference"]["external_id"] == identity
    assert item.source == "kleinanzeigen_de" and item.external_id == identity
    evidence = item.raw["reference_evidence"]
    start, end = evidence["url_span"]
    assert item.description[start:end] == evidence["original_url"]
    assert "?MID=" in evidence["original_url"] and "?" not in reference["url"]
    assert item.links == [{"label": "publisher", "url": reference["url"]}]


@pytest.mark.parametrize(
    "change",
    [
        "arbitrary_intro",
        "other_intro",
        "wrong_model",
        "wrong_title",
        "multiple_urls",
        "homepage",
        "foreign_host",
        "userinfo",
        "port",
        "foreign_country",
    ],
)
def test_arbitrary_or_ambiguous_description_urls_never_create_publication_bridge(change):
    name = "autohero-syndication-3526227598.html"
    soup = BeautifulSoup(body(name), "html.parser")
    desc = soup.select_one("#viewad-description-text")
    text = desc.get_text(" ", strip=True)
    if change == "arbitrary_intro":
        text = "Siehe Empfehlungen: " + text
    elif change == "other_intro":
        text = text.replace("Europas größter freier Händler", "Ein anderer Händler")
    elif change == "wrong_model":
        text = text.replace(
            "Schaue dir diesen BMW 2er 218i Active Tourer",
            "Schaue dir diesen BMW 2er 218d Active Tourer",
        )
    elif change == "wrong_title":
        soup.select_one("#viewad-title").string = "BMW 218d Active Tourer"
    elif change == "multiple_urls":
        text += " https://www.autohero.com/de/bmw-2-er/id/6bd1e96d-39eb-448c-ad85-01b9b85d738d/"
    elif change == "homepage":
        text = text.replace("/de/bmw-2-er/id/e78eddba-9e3b-496a-bbdf-8034025025ee/", "/de/")
    elif change == "foreign_host":
        text = text.replace("https://www.autohero.com/", "https://evil.example/")
    elif change == "userinfo":
        text = text.replace("https://www.autohero.com/", "https://user@www.autohero.com/")
    elif change == "port":
        text = text.replace("https://www.autohero.com/", "https://www.autohero.com:8443/")
    else:
        soup.select_one("#viewad-locality").string = "Nederland - Amsterdam"
    desc.string = text
    item = parse_kleinanzeigen(str(soup), MANIFEST[name]["url"]).listings[0]
    assert item.raw["publication_reference"]["source"] == "kleinanzeigen_de"
    assert "own_publication_reference" not in item.raw and "reference_evidence" not in item.raw
    assert item.links == []


def test_real_publisher_link_reaches_parent_detail_queue():
    from car_market.collect import Collector

    item = page("autohero-syndication-3526227598.html").listings[0]
    collector = SimpleNamespace(country="DE", detail_jobs={})
    Collector.queue_details(collector, item, 42)
    assert item.url in collector.detail_jobs
    publisher = item.raw["publication_reference"]["url"]
    assert publisher in collector.detail_jobs
    assert collector.detail_jobs[publisher][1:] == (42, item)


def test_actual_cap_redirect_records_partial_requested_page_mismatch(monkeypatch):
    from car_market import germany_kleinanzeigen as adapter

    url = MANIFEST["active-date-first.html"]["url"]
    fifty = (
        "https://www.kleinanzeigen.de/s-autos/sortierung:neuste/seite:50/bmw-active-tourer/k0c216"
    )
    collector = MemoryCollector({})
    calls = []

    def fetch(requested):
        calls.append(requested)
        return Response(requested, requested if len(calls) <= 50 else fifty, 200, "native", STAMP)

    def parse(text, final):
        match = re.search(r"/seite:(\d+)/", final)
        number = int(match[1]) if match else 1
        item = SimpleNamespace(
            external_id=str(number),
            url=DETAIL,
            title="BMW 220i Active Tourer",
            scope="excluded",
            price=None,
            price_type="advertised_price",
            raw={"placement": "organic", "promotion_verified": True},
        )
        return SimpleNamespace(
            page=number,
            total=2215,
            pages=89,
            listings=[item],
            search_filters={
                "declared_range": [(number - 1) * 25 + 1, number * 25, 2215],
                "range_valid": True,
                "promotion_error": None,
            },
        )

    monkeypatch.setattr(adapter, "parse_kleinanzeigen", parse)
    collector.fetcher = SimpleNamespace(get=fetch)
    run = collector.db.start_run(timestamp=STAMP)
    ledger = collect_kleinanzeigen(
        collector,
        run,
        {
            "source": "kleinanzeigen_de",
            "key": "cap",
            "url": url,
            "max_pages": 51,
            "detail_limit": 1,
        },
    )
    assert ledger["status"] == "partial"
    assert len(calls) == 51
    assert "native_source_cap:50" in ledger["errors"]
    assert any("native_page_requested_mismatch:51:50" in error for error in ledger["errors"])


OG_FIXTURE = "bound-og-3533982859.html"


def og_detail(soup=None):
    return parse_kleinanzeigen(
        body(OG_FIXTURE) if soup is None else str(soup), MANIFEST[OG_FIXTURE]["final_url"]
    )


def test_real_bound_product_og_recovers_country_from_own_bremerhaven_detail():
    result = og_detail()
    item = result.listings[0]
    assert (item.external_id, item.title, item.price, item.mileage, item.year, item.scope) == (
        "3533982859",
        "BMW 218 218 i",
        8450,
        149000,
        2015,
        "target",
    )
    assert item.raw["own_details"]["Modell"] == "218 Active Tourer"
    assert item.raw["own_location"] == "27578 Häfen - Bremerhaven"
    assert item.kenteken is None and item.vin is None
    assert item.raw["market_country"] == result.search_filters["country"] == "DE"
    proof = item.raw["country_evidence"]
    assert proof["kind"] == "bound_own_product_og_location"
    assert proof["og_country_normalized"] == "DE"
    assert proof["mapping"] == {"Bremen": "DE"}
    assert proof["own_identity"]["external_id"] == item.external_id
    assert proof["locality_binding"] == {
        "visible": "27578 Häfen - Bremerhaven",
        "without_postcode": "Häfen - Bremerhaven",
        "og": "Häfen - Bremerhaven",
    }
    assert not any(
        "Bremen" in row["value"] for row in proof["sources"] if not row["path"].startswith("head")
    )


def test_og_adid_binding_allows_native_region_suffix_change_and_optional_region():
    soup = BeautifulSoup(body(OG_FIXTURE), "html.parser")
    soup.select_one('meta[property="og:url"]')["content"] = MANIFEST[OG_FIXTURE]["url"].replace(
        "-216-54", "-216-9999"
    )
    soup.select_one('meta[property="og:region"]').decompose()
    item = og_detail(soup).listings[0]
    assert item.raw["market_country"] == "DE"
    assert item.url == MANIFEST[OG_FIXTURE]["url"]
    assert item.raw["country_evidence"]["mapping"] == {}


@pytest.mark.parametrize(
    "change",
    [
        "country_wrong",
        "country_duplicate",
        "country_missing",
        "url_missing",
        "url_wrong_ad",
        "url_foreign_host",
        "url_userinfo",
        "url_port",
        "url_http",
        "url_relative",
        "url_duplicate",
        "locality_missing",
        "locality_mismatch",
        "locality_duplicate",
        "type_missing",
        "type_wrong",
        "type_duplicate",
        "region_foreign",
        "region_duplicate",
        "head_country_only",
        "body_meta_only",
        "foreign_visible",
        "multiple_foreign_visible",
        "visible_locality_duplicate",
    ],
)
def test_bound_product_og_rejects_unbound_ambiguous_and_foreign_mutations(change):
    soup = BeautifulSoup(body(OG_FIXTURE), "html.parser")

    def meta(field):
        return soup.select_one(f'head meta[property="og:{field}"]')

    def duplicate(node):
        node.parent.append(BeautifulSoup(str(node), "html.parser"))

    if change.endswith("_missing"):
        meta(
            {"country": "country-name"}.get(change.split("_")[0], change.split("_")[0])
        ).decompose()
    elif change in {
        "country_duplicate",
        "url_duplicate",
        "locality_duplicate",
        "type_duplicate",
        "region_duplicate",
    }:
        duplicate(meta({"country": "country-name"}.get(change.split("_")[0], change.split("_")[0])))
    elif change == "country_wrong":
        meta("country-name")["content"] = "Netherlands"
    elif change == "url_wrong_ad":
        meta("url")["content"] = MANIFEST[OG_FIXTURE]["url"].replace("3533982859", "3534143984")
    elif change.startswith("url_"):
        replacements = {
            "url_foreign_host": ("www.kleinanzeigen.de", "evil.example"),
            "url_userinfo": ("www.kleinanzeigen.de", "evil@www.kleinanzeigen.de"),
            "url_port": ("www.kleinanzeigen.de", "www.kleinanzeigen.de:8443"),
            "url_http": ("https://", "http://"),
            "url_relative": ("https://www.kleinanzeigen.de", ""),
        }
        meta("url")["content"] = meta("url")["content"].replace(*replacements[change])
    elif change == "locality_mismatch":
        meta("locality")["content"] = "Bremerhaven"
    elif change == "type_wrong":
        meta("type")["content"] = "website"
    elif change == "region_foreign":
        meta("region")["content"] = "Noord-Holland"
    elif change == "head_country_only":
        for field in ("type", "url", "locality", "region"):
            meta(field).decompose()
    elif change == "body_meta_only":
        for node in list(soup.select('head meta[property^="og:"]')):
            soup.body.append(node.extract())
    elif change in {"foreign_visible", "multiple_foreign_visible"}:
        place = (
            "Nederland - Amsterdam"
            if change == "foreign_visible"
            else "Nederland - Österreich - Wien"
        )
        soup.select_one("#viewad-locality").string = "12345 " + place
        meta("locality")["content"] = place  # Even exact agreement cannot override foreign proof.
    else:
        duplicate(soup.select_one("#viewad-locality"))
    item = og_detail(soup).listings[0]
    assert item.raw["market_country"] != "DE"
    assert item.scope != "target"
    if change != "foreign_visible":
        assert item.raw["market_country"] == "UNKNOWN"
        assert item.raw["country_evidence"]["bound_og_rejection"]


@pytest.mark.parametrize("change", ["canonical", "idbox", "title_missing", "root_duplicate"])
def test_valid_og_cannot_repair_unverified_own_identity(change):
    soup = BeautifulSoup(body(OG_FIXTURE), "html.parser")
    if change == "canonical":
        soup.select_one('link[rel="canonical"]')["href"] = DETAIL
    elif change == "idbox":
        soup.select_one("#viewad-ad-id-box").string = "Anzeigen-ID 3534143984"
    elif change == "title_missing":
        soup.select_one("#viewad-title").decompose()
    else:
        soup.main.append(BeautifulSoup(str(soup.select_one("#viewad-main-info")), "html.parser"))
    assert og_detail(soup).listings == []


def test_og_country_cannot_requalify_another_own_model():
    soup = BeautifulSoup(body(OG_FIXTURE), "html.parser")
    soup.select_one("#viewad-title").string = "BMW 218d Gran Tourer"
    for row in soup.select(".addetailslist--detail"):
        if row.get_text(" ", strip=True).startswith("Modell "):
            row.select_one(".addetailslist--detail--value").string = "218 Gran Tourer"
    item = og_detail(soup).listings[0]
    assert item.raw["market_country"] == "DE" and item.scope == "excluded"
