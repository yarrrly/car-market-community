"""Own responses plus country, identity, price and traversal mutations."""

import copy
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from car_market.db import Database
from car_market.fetch import Response
from car_market.germany import bind_market
from car_market.germany_additional import (
    CARWOW_SEARCH,
    INSTAMOTION_SEARCH,
    additional_queries,
    collect_additional,
    parse_additional,
)
from car_market.germany_additional_public import (
    INSTAMOTION_MODEL,
    MARKT_SEARCH,
    AdditionalPublicGetter,
    PublicResponse,
    additional_page_request,
    apply_country_publication,
    get_additional_page,
)

FIXTURES = Path(__file__).parent / "fixtures" / "germany_additional"
MANIFEST = {x["fixture"]: x for x in json.loads((FIXTURES / "manifest.json").read_text())}


def fixture(name):
    file = FIXTURES / (name + ".html")
    return (file if file.exists() else FIXTURES / (name + ".json")).read_text()


def parsed(name, source="instamotion_de"):
    key = name + ".html" if name + ".html" in MANIFEST else name + ".json"
    return parse_additional(source, fixture(name), MANIFEST[key]["final_uri"])


def document(name="instamotion"):
    return json.loads(
        BeautifulSoup(fixture(name), "html.parser").find(id="__NEXT_DATA__").get_text()
    )


def render(doc, name="instamotion"):
    soup = BeautifulSoup(fixture(name), "html.parser")
    soup.find(id="__NEXT_DATA__").string = json.dumps(doc)
    return str(soup)


def test_fixtures_are_bound_by_hash_without_session_material():
    assert len(MANIFEST) == 25
    for name, meta in MANIFEST.items():
        raw = (FIXTURES / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == meta["fixture_sha256"]
        assert meta["fetched_at"].startswith("2026-10-08") and meta["status"] in {200, 303}
        assert b"AIza" not in raw and b"authenticity_token" not in raw
        assert b"csrf-token" not in raw and b"catalogKey" not in raw


def test_real_instamotion_three_pages_are_50_ids_against_56_not_complete():
    pages = [parsed(n) for n in ["instamotion", "instamotion-page2", "instamotion-page3"]]
    assert [(p.total, p.pages, p.page, len(p.listings)) for p in pages] == [
        (56, 3, 1, 18),
        (56, 3, 2, 18),
        (56, 3, 3, 14),
    ]
    ids = [i.external_id for p in pages for i in p.listings]
    assert len(ids) == len(set(ids)) == 50
    targets = [i for p in pages for i in p.listings if i.scope == "target"]
    assert [i.external_id for i in targets] == ["87605782", "85563135", "83421296"]
    assert [i.price for i in targets] == [27499, 27099, 28399]
    assert all(i.raw["country_evidence"]["path"] == "location_country.iso_code" for i in targets)
    assert all(i.kenteken is None and len(i.vin) == 17 for i in targets)
    assert all(i.raw["publication_reference"]["external_id"] == i.external_id for i in targets)


def test_real_own_details_and_older_body_remain_unconfirmed():
    detail = parsed("instamotion-detail").listings[0]
    assert (detail.price, detail.year, detail.mileage, detail.transmission, detail.fuel) == (
        27499,
        2025,
        10000,
        "automatic",
        "petrol",
    )
    assert detail.vin == "WBA21BX0907R95485" and detail.raw["market_country"] == "DE"
    assert detail.seller == "Autohaus Unterberger GmbH - Rosenheim - BMW"
    older = parsed("instamotion-f45-probe").listings[0]
    assert (older.year, older.transmission, older.price) == (2021, "manual", 19999)
    assert older.raw["model_status"] == "body_unconfirmed" and older.scope != "target"


@pytest.mark.parametrize("country,scope", [(None, "needs_confirmation"), ("NL", "excluded")])
def test_origin_seller_and_query_do_not_replace_own_location_country(country, scope):
    doc = document("instamotion-detail")
    own = doc["props"]["pageProps"]["carData"]
    own["location_country"] = {} if country is None else {"iso_code": country}
    own["origin_country"] = own["seller"]["country"] = {"iso_code": "DE"}
    doc["recommendations"] = [{"vin": "WBA21BX0407T55765", "location_country": {"iso_code": "DE"}}]
    item = parse_additional(
        "instamotion_de",
        render(doc, "instamotion-detail"),
        MANIFEST["instamotion-detail.html"]["final_uri"],
    ).listings[0]
    assert item.scope == scope
    assert item.raw.get("market_country") != "DE"


def test_vin_is_full_own_only_and_explicit_publisher_link_is_preserved():
    doc = document("instamotion-detail")
    own = doc["props"]["pageProps"]["carData"]
    own["vin"] = "R95485"
    own["description"] = "Related car VIN WBA21BX0407T55765; kenteken AB-123-C"
    own["external_url"] = "https://www.pkw.de/fahrzeuge/details/123"
    doc["recommendations"] = [{"vin": "WBA21BX0407T55765"}]
    item = parse_additional(
        "instamotion_de",
        render(doc, "instamotion-detail"),
        MANIFEST["instamotion-detail.html"]["final_uri"],
    ).listings[0]
    assert item.vin is None and item.kenteken is None
    assert item.raw["explicit_publisher_url"] == own["external_url"]


@pytest.mark.parametrize("mutation", ["own_id", "slug", "car_id", "route_id"])
def test_detail_mismatch_cannot_ingest_related_vehicle(mutation):
    doc = document("instamotion-detail")
    props = doc["props"]["pageProps"]
    if mutation == "own_id":
        props["carData"]["id"] = "85563135"
    elif mutation == "slug":
        props["carData"]["slug"] = "bmw-other"
    elif mutation == "car_id":
        props["carId"] = "85563135"
    else:
        props["routerState"]["query"]["carId"] = "85563135"
    assert not parse_additional(
        "instamotion_de",
        render(doc, "instamotion-detail"),
        MANIFEST["instamotion-detail.html"]["final_uri"],
    ).listings


def test_price_comes_from_displayed_nice_gross_with_net_separate():
    doc = document()
    own = doc["props"]["pageProps"]["searchResults"]["cars"][0]
    assert own["price_information"]["price_data"]["price"] != own["price"]
    original = parsed("instamotion").listings[0]
    assert original.price == own["price"]
    assert original.raw["price_net_eur"] == own["price_without_vat"]
    nice = own["price_information"]["nice_price_data"]
    nice["price"] = None
    own["price"] = 77777
    net = parse_additional("instamotion_de", render(doc), INSTAMOTION_SEARCH).listings[0]
    assert net.price_type == "net_price" and net.price == nice["price_without_vat"]
    nice["price"] = True
    nice["price_without_vat"] = -1
    assert (
        parse_additional("instamotion_de", render(doc), INSTAMOTION_SEARCH).listings[0].price
        is None
    )


def test_foreign_currency_and_hidden_own_offer_do_not_become_purchase():
    doc = document("instamotion-detail")
    props = doc["props"]["pageProps"]
    props["carData"]["price_information"]["currency"]["name"] = "CZK"
    props["carUnavailable"] = True
    item = parse_additional(
        "instamotion_de",
        render(doc, "instamotion-detail"),
        MANIFEST["instamotion-detail.html"]["final_uri"],
    ).listings[0]
    assert item.price is None and item.price_type == "foreign_currency"
    assert item.raw["inventory_status"] == "unavailable_or_unverified"


def test_structured_export_flag_keeps_offer_out_of_gross_purchase():
    doc = document("instamotion-detail")
    doc["props"]["pageProps"]["carData"]["only_for_export"] = True
    item = parse_additional(
        "instamotion_de",
        render(doc, "instamotion-detail"),
        MANIFEST["instamotion-detail.html"]["final_uri"],
    ).listings[0]
    assert item.price == 27499 and item.price_type == "export_price"
    assert item.raw["price_warning"] == "export_only"


def test_search_stale_page_and_missing_own_link_are_detected():
    doc = document("instamotion-page2")
    assert not parse_additional(
        "instamotion_de", render(doc, "instamotion-page2"), INSTAMOTION_SEARCH + "?page=3"
    ).listings
    soup = BeautifulSoup(fixture("instamotion"), "html.parser")
    for a in soup.select('a[href*="/87605782/"]'):
        a.decompose()
    assert "87605782" not in {
        i.external_id
        for i in parse_additional("instamotion_de", str(soup), INSTAMOTION_SEARCH).listings
    }


def test_carwow_native_frame_counter_ids_and_unregistered_default_removed():
    shell = parsed("carwow-active-stock", "carwow_de")
    assert parse_qs(urlsplit(shell.search_filters["native_payload_url"]).query) == {
        "brand_slug": ["bmw"],
        "model_slug": ["2er-active-tourer"],
        "vehicle_state_group": ["all"],
    }
    inventory = parsed("carwow-active-cards", "carwow_de")
    assert (inventory.total, inventory.pages, inventory.page, len(inventory.listings)) == (
        136,
        6,
        1,
        24,
    )
    ids = inventory.search_filters["retained_ids"]
    assert ids == [i.external_id for i in inventory.listings]
    assert len(set(ids)) == 24 and all(len(i) == 32 for i in ids)
    assert all(i.vin is None and i.kenteken is None and i.price is None for i in inventory.listings)
    assert all("market_country" not in i.raw for i in inventory.listings)


def test_carwow_actual_f45_vdp_gross_price_still_held_without_country():
    item = parsed("carwow-own-detail", "carwow_de").listings[0]
    assert (item.price, item.year, item.mileage, item.transmission, item.fuel) == (
        10882,
        2016,
        109649,
        "automatic",
        "petrol",
    )
    assert "218i Advantage" in item.title and item.scope == "needs_confirmation"
    assert item.vin is None and item.raw["country_evidence"] is None
    assert item.raw["publication_reference"]["external_id"] == item.external_id
    non_target = parsed("carwow-publisher", "carwow_de").listings[0]
    assert "220i" in non_target.title and non_target.scope == "excluded"


def test_carwow_own_deal_identity_not_from_request_or_site_country():
    url = MANIFEST["carwow-own-detail.html"]["final_uri"]
    assert not parse_additional(
        "carwow_de",
        fixture("carwow-own-detail"),
        url.replace("d79cc0b6c415fdbb36ed8c65d9d75222", "a" * 32),
    ).listings
    for bad in [
        url + "/enquiry/new",
        "https://angebote.carwow.de/users/sign_in",
        "https://angebote.carwow.de.evil.example/deals/" + "a" * 32,
    ]:
        assert not parse_additional("carwow_de", fixture("carwow-own-detail"), bad).listings
    soup = BeautifulSoup(fixture("carwow-own-detail"), "html.parser")
    soup.select_one(".deal-pricing__tax-rate").string = "zzgl. Mehrwertsteuer"
    item = parse_additional("carwow_de", str(soup), url).listings[0]
    assert item.price is None and item.price_type == "unknown"


class ReplayCollector:
    country = "DE"

    def __init__(self, path, overrides=None):
        self.db = Database(path)
        self.db.conn.execute("CREATE TABLE monitor_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        bind_market(self.db, {"scope": {"country": "DE"}})
        self.requests = []
        self.overrides = overrides or {}
        self.fetcher = SimpleNamespace(get=self.get)

    def get(self, url):
        self.requests.append(url)
        key = urlsplit(url)
        for name, meta in MANIFEST.items():
            candidates = [meta["request"]["url"], meta["final_uri"]]
            if any(
                urlsplit(u).path == key.path and parse_qs(urlsplit(u).query) == parse_qs(key.query)
                for u in candidates
            ):
                body = self.overrides.get(name, (FIXTURES / name).read_text())
                return Response(url, meta["final_uri"], 200, body, meta["fetched_at"])
        return Response(url, url, 403, "blocked", "2026-10-08T12:00:00+00:00", "fixture_missing")

    def fetch_record(self, run, source, response, retain_raw=True):
        return self.db.save_fetch(
            run,
            source,
            response.url,
            response.final_url,
            response.status,
            response.body,
            response.timestamp,
            error=response.error,
            retain_raw=retain_raw,
        )


def test_temp_de_database_real_replay_keeps_counter_gap_and_only_own_de(tmp_path):
    collector = ReplayCollector(tmp_path / "first-car-de" / "data" / "market.sqlite3")
    try:
        run = collector.db.start_run("replay")
        query = additional_queries()[0]
        # The unspecified 2021 body is verified by its own detail, without a VIN decoder.
        result = collect_additional(collector, run, query)
        assert (result["expected_count"], result["received_count"], result["target_count"]) == (
            56,
            50,
            3,
        )
        assert "counter_gap:56:50" in result["errors"] and result["coverage_status"] == "partial"
        assert result["requests"] == 7 and result["infer_missing"] is False
        assert len(result["cards"]) == 50 and len(result["details"]) == 4
        assert {entry["status"] for entry in result["details"].values()} == {"own_de"}
        assert collector.db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 3
        assert (
            collector.db.conn.execute(
                "SELECT COUNT(*) FROM vehicles WHERE kenteken IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            collector.db.conn.execute("SELECT status FROM source_runs").fetchone()[0] == "partial"
        )
        assert collector.db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert collector.db.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        for row in collector.db.conn.execute("SELECT details_json FROM observations"):
            assert json.loads(row[0])["raw"]["market_country"] == "DE"
    finally:
        collector.db.conn.close()


def test_carwow_temp_database_frontier_and_explicit_reference_survive_without_writes(tmp_path):
    collector = ReplayCollector(tmp_path / "first-car-de" / "data" / "market.sqlite3")
    try:
        run = collector.db.start_run("replay")
        query = additional_queries()[1] | {"max_pages": 1, "request_limit": 4}
        result = collect_additional(collector, run, query)
        assert (result["expected_count"], result["received_count"], result["target_count"]) == (
            136,
            24,
            0,
        )
        assert result["requests"] == 4 and result["observation_status"] == "held_country"
        identity = "d79cc0b6c415fdbb36ed8c65d9d75222"
        assert result["details"][identity]["status"] == "held_country"
        assert result["cards"][identity]["publication_reference"]["url"].endswith(
            "/deals/" + identity
        )
        assert collector.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    finally:
        collector.db.conn.close()


def test_retention_removes_session_material_before_raw_persistence(tmp_path):
    doc = document()
    doc["props"]["session"] = {"access_token": "UNTRUSTED_SESSION_CANARY"}
    doc["props"]["pageProps"]["userDelivery"] = {"personal": "UNTRUSTED_DELIVERY_CANARY"}
    body = render(doc) + '<input name="authenticity_token" value="UNTRUSTED_CSRF_CANARY">'
    collector = ReplayCollector(
        tmp_path / "first-car-de" / "data" / "market.sqlite3", {"instamotion.html": body}
    )
    try:
        run = collector.db.start_run("replay")
        result = collect_additional(collector, run, additional_queries()[0] | {"request_limit": 1})
        for file in collector.db.raw.iterdir():
            retained = gzip.decompress(file.read_bytes())
            assert b"UNTRUSTED_" not in retained and b"authenticity_token" not in retained
        capture = result["captures"][0]
        assert capture["source_body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
        assert capture["source_body_sha256"] != capture["retained_body_sha256"]
        assert len(result["cards"]) == 18 and result["target_count"] == 1
    finally:
        collector.db.conn.close()


def test_pagination_mutation_rejects_filter_drift_before_followup(tmp_path):
    changed = fixture("instamotion").replace("direction=desc", "direction=desc&max-price=15000")
    collector = ReplayCollector(
        tmp_path / "first-car-de" / "data" / "market.sqlite3", {"instamotion.html": changed}
    )
    try:
        run = collector.db.start_run("replay")
        result = collect_additional(collector, run, additional_queries()[0] | {"request_limit": 1})
        assert len(collector.requests) == 1
        assert any("native_next_filter_drift" in e for e in result["errors"])
        assert len(result["cards"]) == 18 and result["coverage_status"] == "partial"
    finally:
        collector.db.conn.close()


def test_nl_collector_and_unverified_seed_are_rejected_before_network():
    collector = SimpleNamespace(country="NL")
    with pytest.raises(ValueError, match="german_collector"):
        collect_additional(collector, 1, additional_queries()[0])
    collector.country = "DE"
    for change in [{"url": CARWOW_SEARCH.replace("=all", "=new")}, {"request_limit": True}]:
        query = copy.deepcopy(additional_queries()[1]) | change
        with pytest.raises(ValueError):
            collect_additional(collector, 1, query)


def test_second_round_native_api_model_control_and_markt_full_frontier():
    native = parsed("instamotion-native-page2", "instamotion_de")
    assert (native.total, native.page, len(native.listings)) == (56, 2, 18)
    assert native.search_filters["native_paging"]
    assert {i.external_id for i in native.listings} == {
        i.external_id for i in parsed("instamotion-page2").listings
    }
    control = parsed("instamotion-model218")
    assert (control.total, control.pages, len(control.listings)) == (19, 1, 17)
    assert control.search_filters["native_model"] == "MAKE_BMW-MODELFAMILY_218"
    pages = [parsed("markt-search-page" + str(n), "markt_de") for n in range(1, 7)]
    assert [(p.total, p.page) for p in pages] == [(105, n) for n in range(1, 7)]
    ids = [i.external_id for p in pages for i in p.listings]
    assert len(ids) == len(set(ids)) == 105
    assert all("market_country" not in i.raw for p in pages for i in p.listings)


def test_own_markt_country_is_bound_to_expose_id_not_platform_hq_or_search():
    item = parsed("markt-own-herne", "markt_de").listings[0]
    assert item.external_id == "2fb04801" and item.raw["market_country"] == "DE"
    assert item.raw["country_evidence"]["claim"]["state"] == "Nordrhein-Westfalen"
    assert (item.year, item.mileage, item.price) == (2016, 109649, 10882)
    for field, value in [("advertId", "00000000"), ("pageClass", "SEARCH")]:
        soup = BeautifulSoup(fixture("markt-own-herne"), "html.parser")
        node = soup.find(id="additional-own-geography")
        doc = json.loads(node.get_text())
        doc[0][field] = value
        node.string = json.dumps(doc)
        assert not parse_additional("markt_de", str(soup), item.url).listings


class PublicFixtureGetter(AdditionalPublicGetter):
    def _request(self, url, method="GET", body=None, follow=True):
        self.http_requests += 1
        for name, meta in MANIFEST.items():
            if meta["request"]["method"] != method:
                continue
            if any(
                urlsplit(u).path == urlsplit(url).path
                and parse_qs(urlsplit(u).query) == parse_qs(urlsplit(url).query)
                for u in [meta["request"]["url"], meta["final_uri"]]
            ):
                response = PublicResponse(
                    url,
                    meta["final_uri"],
                    meta["status"],
                    (FIXTURES / name).read_text(),
                    meta["fetched_at"],
                    source_body_sha256=meta["source_wire_sha256"],
                    request_method=method,
                    request_body=body,
                )
                if name == "markt-herne-publisher-response.html":
                    response.redirect_to = json.loads(
                        (FIXTURES / "followup-publisher-hop.json").read_text()
                    )["redirect_to"]
                self.captures.append(response)
                return response
        raise ValueError("uncaptured_public_request")


def test_country_recovery_uses_actual_explicit_publisher_id_and_foreign_location_wins():
    url = MANIFEST["carwow-own-detail.html"]["final_uri"]
    getter = PublicFixtureGetter(
        country_publications={
            "d79cc0b6c415fdbb36ed8c65d9d75222": MANIFEST["markt-own-herne.html"]["final_uri"]
        }
    )
    response = getter.get(url)
    item = parse_additional("carwow_de", response.body, response.final_url).listings[0]
    apply_country_publication(item, response)
    assert item.scope == "target" and item.raw["market_country"] == "DE"
    proof = item.raw["country_evidence"]
    assert proof["claim_id"] == "2fb04801" and proof["native_id"] == item.external_id
    assert len(proof["proofs"]) == 3 and all(
        len(x["source_body_sha256"]) == 64 for x in proof["proofs"]
    )
    item.raw["own_detail_values"]["Fahrzeugstandort Land"] = "NL"
    apply_country_publication(item, response)
    assert item.scope == "excluded" and item.raw["market_country"] == "NL"
    response.country_publication["native_id"] = "0" * 32
    other = parsed("carwow-own-detail", "carwow_de").listings[0]
    apply_country_publication(other, response)
    assert "market_country" not in other.raw


def test_public_pagination_and_facebook_scope_fail_closed():
    first = parsed("markt-search-page1", "markt_de")
    request = additional_page_request("markt_de", MARKT_SEARCH, 3, first)
    assert request["method"] == "POST" and set(request["body"]) == {"targetUrl"}
    native_body = BeautifulSoup(fixture("instamotion"), "html.parser")
    node = native_body.find(id="__NEXT_DATA__")
    doc = json.loads(node.get_text())
    doc["buildId"] = urlsplit(MANIFEST["instamotion-native-page2.json"]["final_uri"]).path.split(
        "/"
    )[3]
    node.string = json.dumps(doc)
    page, response = get_additional_page(
        PublicFixtureGetter(),
        "instamotion_de",
        INSTAMOTION_SEARCH,
        2,
        parsed("instamotion"),
        str(native_body),
    )
    assert page.page == 2 and len(page.listings) == 18 and response.status == 200
    fb = parsed("facebook-unfiltered", "facebook_de")
    assert len(fb.listings) == 24 and fb.search_filters["query_verified"] is False
    assert all("market_country" not in i.raw for i in fb.listings)
    assert additional_queries(True)[-1]["enabled"] is False
    assert additional_queries(True)[2]["url"] == INSTAMOTION_MODEL


def test_temp_db_second_round_country_proof_fetches_and_unfiltered_frontier(tmp_path):
    collector = ReplayCollector(tmp_path / "first-car-de" / "data" / "market.sqlite3")
    collector.fetcher = PublicFixtureGetter(
        country_publications={
            "d79cc0b6c415fdbb36ed8c65d9d75222": MANIFEST["markt-own-herne.html"]["final_uri"]
        }
    )
    try:
        run = collector.db.start_run("replay")
        result = collect_additional(
            collector, run, additional_queries()[1] | {"max_pages": 1, "request_limit": 4}
        )
        assert result["target_count"] == 1 and result["received_count"] == 24
        assert collector.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        detail = result["details"]["d79cc0b6c415fdbb36ed8c65d9d75222"]
        assert all(x.get("fetch_id") for x in detail["raw"]["country_evidence"]["proofs"])
        assert (
            detail["raw"]["field_evidence"]["price"]["advert_id"]
            == detail["raw"]["publication_reference"]["external_id"]
        )
        fb = collect_additional(collector, run, additional_queries(True)[-1])
        assert len(fb["cards"]) == 24 and fb["target_count"] == 0
        assert "target_query_scope_unverified" in fb["errors"]
        assert collector.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    finally:
        collector.db.conn.close()


@pytest.mark.parametrize("status", [403, 429])
def test_public_host_block_stops_further_http_without_stopping_other_hosts(status):
    import io
    import urllib.error
    from contextlib import nullcontext

    getter = AdditionalPublicGetter(max_requests=20, request_delay=0)
    calls = []

    def open_request(request, timeout):
        calls.append(request.full_url)
        if "instamotion.com" in request.full_url:
            raise urllib.error.HTTPError(
                request.full_url, status, "blocked", {}, io.BytesIO(b"blocked")
            )
        return nullcontext(
            SimpleNamespace(read=lambda size: b"<html></html>", status=200, headers={})
        )

    getter.opener = SimpleNamespace(open=open_request)
    response = getter.get(INSTAMOTION_SEARCH)
    assert response.status == status and getter.captures[-1].status == status
    with pytest.raises(ValueError, match="public_host_blocked"):
        getter.get(INSTAMOTION_MODEL)
    assert getter.get(CARWOW_SEARCH).status == 200
    assert calls == [INSTAMOTION_SEARCH, CARWOW_SEARCH]
    assert getter.http_requests == 2


def test_actual_markt_redirect_binds_country_discovery_to_one_carwow_publication():
    from car_market.germany_additional_public import (
        carwow_publication_redirect,
        markt_carwow_navigation,
    )

    own_url = MANIFEST["markt-own-herne.html"]["final_uri"]
    request = markt_carwow_navigation(fixture("markt-own-herne"), own_url)
    assert request["claim_id"] == "2fb04801"
    getter = PublicFixtureGetter()
    response = getter.post_public_navigation(request["url"], request["body"])
    publication = carwow_publication_redirect(response)
    assert publication["external_id"] == "d79cc0b6c415fdbb36ed8c65d9d75222"
    assert parsed("markt-own-herne", "markt_de").listings[0].scope == "needs_confirmation"


@pytest.mark.parametrize(
    "mutation", ["duplicate", "unrelated_button", "foreign_country", "wrong_id", "foreign_form"]
)
def test_country_mapping_requires_bound_single_own_publisher_button(mutation):
    from car_market.germany_additional_public import markt_carwow_navigation

    body = fixture("markt-own-herne")
    soup = BeautifulSoup(body, "html.parser")
    button = next(
        x
        for x in soup.select("button[data-targeturl]")
        if x.get_text(strip=True) == "Jetzt bei Carwow mehr erfahren!"
    )
    if mutation == "duplicate":
        button.insert_after(copy.copy(button))
    elif mutation == "unrelated_button":
        button.string = "Weitere Angebote auf Carwow"
    elif mutation == "foreign_form":
        soup.find("form", id=button["form"])["action"] = (
            "https://foreign.example/urlMaskingRedirect.htm"
        )
    else:
        node = soup.find(id="additional-own-geography")
        doc = json.loads(node.get_text())
        doc[0]["country" if mutation == "foreign_country" else "advertId"] = (
            "NL" if mutation == "foreign_country" else "00000000"
        )
        node.string = json.dumps(doc)
    assert markt_carwow_navigation(str(soup), MANIFEST["markt-own-herne.html"]["final_uri"]) is None


@pytest.mark.parametrize(
    "target",
    [
        "https://foreign.example/deals/" + "a" * 32,
        "https://user@angebote.carwow.de/deals/" + "a" * 32,
        "https://angebote.carwow.de/deals/" + "a" * 24,
        "https://angebote.carwow.de/stock_cars",
    ],
)
def test_publisher_redirect_rejects_foreign_or_unbound_identity(target):
    from car_market.germany_additional_public import carwow_publication_redirect

    response = SimpleNamespace(status=303, redirect_to=target)
    assert carwow_publication_redirect(response) is None


def test_markt_collection_persists_observed_mapping_and_keeps_model_unconfirmed(
    tmp_path, monkeypatch
):
    import car_market.germany_additional as module
    from car_market.parse import Listing, Page

    own_url = MANIFEST["markt-own-herne.html"]["final_uri"]
    original = module.parse_additional

    def parse(source, body, url):
        if url == MARKT_SEARCH:
            return Page(
                [
                    Listing(
                        "markt_de",
                        "2fb04801",
                        own_url,
                        "BMW 2er Active Tourer",
                        raw={"discovery_only": True},
                    )
                ],
                1,
                1,
                1,
            )
        return original(source, body, url)

    monkeypatch.setattr(module, "parse_additional", parse)
    collector = ReplayCollector(tmp_path / "market.sqlite3")
    collector.fetcher = PublicFixtureGetter()
    result = collect_additional(
        collector,
        collector.db.start_run(),
        {
            "source": "markt_de",
            "key": "own-publication",
            "url": MARKT_SEARCH,
            "max_pages": 1,
            "detail_limit": 1,
            "request_limit": 10,
        },
    )
    assert result["details"]["2fb04801"]["scope"] == "needs_confirmation"
    state = json.loads(
        collector.db.conn.execute(
            "select value from monitor_state where key='germany_country_publications:carwow_de'"
        ).fetchone()[0]
    )
    proof = state["d79cc0b6c415fdbb36ed8c65d9d75222"]
    assert proof["claim_url"] == own_url
    assert (
        collector.db.conn.execute(
            "select status from fetches where id=?", (proof["navigation_fetch_id"],)
        ).fetchone()[0]
        == 303
    )
    assert (
        collector.db.conn.execute("select scope from listings").fetchone()[0]
        == "needs_confirmation"
    )
    collector.db.conn.close()
