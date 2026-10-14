import copy
import hashlib
import json
from pathlib import Path

import pytest
from car_market.fetch import Response
from car_market.mercedes_de_sources import (
    AUTOHERO_API,
    PKW_SEARCH,
    collect_mercedes_de_source,
    mercedes_de_page_request,
    parse_mercedes_de_sources,
    wiveda_mercedes_payload_url,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/mercedes_de_sources"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


def fixture(name):
    meta = next(x for x in MANIFEST if x["fixture"] == name)
    return (FIXTURES / name).read_text(), meta


def parsed(name):
    body, meta = fixture(name)
    return parse_mercedes_de_sources(meta["source"], body, meta["final_url"])


@pytest.mark.parametrize("meta", MANIFEST, ids=lambda m: m["fixture"])
def test_fixture_integrity_and_original_capture_provenance(meta):
    assert (
        hashlib.sha256((FIXTURES / meta["fixture"]).read_bytes()).hexdigest()
        == meta["fixture_sha256"]
    )
    assert len(meta["wire_sha256"]) == 64
    assert meta["captured_at"].startswith("2026-10-08T")
    assert meta["url"].startswith("https://")
    assert meta["status"] == 200


def test_pkw_own_brand_model_contract_and_all_bodies_retained():
    page = parsed("pkw-c180-api.json")
    assert (page.total, page.pages, page.page, len(page.listings)) == (183, 10, 1, 20)
    assert page.search_filters["brand_model"] == ["49_522"]
    item = page.listings[0]
    assert item.raw["own_inventory_record"]["brand"]["id"] == 49
    assert item.raw["own_inventory_record"]["model"]["id"] == 522
    assert item.raw["body_type"] == "Kombi"
    assert item.raw.get("generation") is None
    assert item.raw["first_registration"] == "2021-12-01"
    assert item.raw["own_dealer_address"]["zip"] == "26203"
    assert item.scope == "unknown"


def test_pkw_native_next_link_does_not_erase_model_filter():
    request = mercedes_de_page_request("pkw_de", PKW_SEARCH, 2)
    assert "brand_model=49_522" in request["url"]
    assert "source=autokatalog" in request["url"]
    assert "page=2" in request["url"]
    with pytest.raises(ValueError, match="c180"):
        mercedes_de_page_request("pkw_de", PKW_SEARCH.replace("49_522", "9_2326"), 2)


def test_foreign_pkw_country_is_preserved_and_country_not_inferred_from_host():
    body, meta = fixture("pkw-c180-api.json")
    doc = json.loads(body)
    doc["results"] = [doc["results"][0]]
    doc["results"][0]["location"]["country"] = "NLD"
    page = parse_mercedes_de_sources("pkw_de", json.dumps(doc), meta["final_url"])
    assert page.listings[0].raw["market_country"] == "NLD"
    doc["results"][0]["location"].pop("country")
    page = parse_mercedes_de_sources("pkw_de", json.dumps(doc), meta["final_url"])
    assert "market_country" not in page.listings[0].raw


def test_real_pkw_coupe_detail_does_not_infer_generation_from_registration():
    page = parsed("pkw-detail-142208737556145407.json")
    assert len(page.listings) == 1
    item = page.listings[0]
    assert (item.year, item.fuel, item.transmission) == (2017, "petrol", "automatic")
    assert item.raw["body_type"] == "Coupé/Sportwagen"
    assert not item.raw.get("generation")
    assert not item.raw.get("built_year")
    assert item.scope == "unknown"


def test_autohero_observed_post_payload_and_pagination_are_immutable():
    _, meta = fixture("autohero-complete-ui-3.json")
    template = copy.deepcopy(meta["request_body"])
    request = mercedes_de_page_request(
        "autohero_de", AUTOHERO_API, 2, request_body=template
    )
    assert request["method"] == "POST"
    assert request["body"]["variables"]["search"]["offset"] == 24
    assert template == meta["request_body"]
    terms = request["body"]["variables"]["search"]["filter"]["value"]
    assert {"field": "bodyType", "op": "eq", "value": [1008]} in terms
    assert {"field": "firstRegistrationYear", "op": "gte", "value": "2016"} in terms
    assert {"field": "gearType", "op": "eq", "value": ["1139", "1140", "1141"]} in terms
    assert (
        request["body"]["variables"]["search"]["properties"]["firstPublishedDays"]
        == -30
    )
    template["variables"]["search"]["filter"]["op"] = "or"
    with pytest.raises(ValueError, match="mercedes_filter"):
        mercedes_de_page_request("autohero_de", AUTOHERO_API, 1, request_body=template)


def test_autohero_detail_preserves_own_c205_vin_body_and_build_year():
    page = parsed("autohero-c205-detail.html")
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.vin == "W1K2053761G104242"
    assert item.raw["body_type"] == "Coupé"
    assert item.raw["built_year"] == 2021
    assert item.raw["market_country"] == "DE"
    assert item.raw["first_registration"].startswith("2021-")
    assert item.raw["mercedes_own_fields"]["dealer_address"]
    assert item.scope == "unknown"


def test_autohero_filtered_inventory_keeps_non_c180_models():
    page = parsed("autohero-complete-ui-3.json")
    assert (page.total, len(page.listings)) == (14, 14)
    assert any("C 300" in x.title or "C 200" in x.title for x in page.listings)
    assert all(x.scope == "unknown" for x in page.listings)
    assert all(x.raw.get("body_type") is None for x in page.listings)


def test_klein_native_counter_organic_and_unknown_country():
    page = parsed("klein-c180-coupe-date-1.html")
    assert (page.total, page.pages, page.page, len(page.listings)) == (252, 11, 1, 27)
    assert sum(x.raw["placement"] == "organic" for x in page.listings) == 25
    assert page.search_filters["promotion_classification"] == "bound_native_tracker"
    assert all(
        x.scope == "unknown" and "market_country" not in x.raw for x in page.listings
    )
    last = parsed("klein-c180-coupe-date-11.html")
    assert last.page == 11
    assert len(last.listings) == 2
    assert (
        "seite%3A"
        not in mercedes_de_page_request(
            "kleinanzeigen_de", fixture("klein-c180-coupe-date-1.html")[1]["url"], 11
        )["url"]
    )


def test_klein_detail_keeps_body_registration_and_country_proof():
    item = parsed("klein-modern-detail.html").listings[0]
    assert item.year == 2017
    assert item.raw["first_registration"]
    assert item.raw["body_type"] == item.raw["own_details"]["Fahrzeugtyp"]
    assert item.raw.get("generation") is None
    assert item.raw["source_country_evidence"]
    assert item.raw["own_dealer_address"]["location"]


def test_instamotion_own_native_fields_and_actual_counter_gap():
    page = parsed("instamotion-c180.html")
    assert page.total == 41
    assert len(page.listings) == 18
    assert page.search_filters["native_query"]["filters"] == ["mercedes-benz", "c-180"]
    item = page.listings[0]
    assert item.raw["body_type"] == "Kombi"
    assert item.raw["production_date"] == "2025-09-01"
    assert item.raw["own_dealer_address"]["location_post_code"] == "58706"
    assert item.raw["market_country"] == "DE"
    assert parsed("instamotion-c180-3.html").page == 3


def test_wiveda_records_keep_all_bodies_and_native_country():
    page = parsed("wiveda-c180-payload.json")
    assert (page.total, page.pages, len(page.listings)) == (56, 5, 12)
    assert page.listings[0].raw["body_type"] == "limousine"
    assert page.listings[0].raw["market_country"] == "DE"
    detail = parsed("wiveda-detail-2bf1a5cf-7949-4dea-bdb4-0846a5d15104-payload.json")
    assert len(detail.listings) == 1
    assert detail.listings[0].year == 2016
    assert detail.listings[0].raw["body_type"] == "sportwagen"
    assert detail.listings[0].raw.get("generation") is None
    assert detail.listings[0].raw["own_dealer_address"]


def test_wiveda_payload_uri_binds_own_route_page_and_build():
    body, meta = fixture("wiveda-native-payload-link.html")
    uri = wiveda_mercedes_payload_url(body, meta["url"])
    assert "/mercedes-benz/c-180/_payload.json?_b=" in uri
    with pytest.raises(ValueError, match="owner_mismatch"):
        wiveda_mercedes_payload_url(body, meta["url"] + "?page=2")


def test_carwow_native_filters_and_unknown_stock_country():
    page = parsed("carwow-native-c-class.html")
    assert (page.total, page.pages, len(page.listings)) == (3, 1, 3)
    assert page.search_filters["native_filters"]["filter-brand-value"] == "mercedes"
    assert all("market_country" not in x.raw for x in page.listings)


def test_twelve_native_counter_is_discovery_not_stock_country():
    page = parsed("twelve-coupe.html")
    assert (page.total, page.pages, len(page.listings)) == (220, 11, 20)
    assert page.search_filters["counter_conflict"] is False
    assert all(
        x.scope == "unknown" and "market_country" not in x.raw for x in page.listings
    )


def test_autoland_schema_card_binding_and_alternative_own_native_routes():
    root = parsed("autoland-c-class.html")
    page4 = parsed("autoland-c-class-4.html")
    assert (root.total, root.pages, len(root.listings), len(page4.listings)) == (
        53,
        5,
        12,
        12,
    )
    assert any("/detail/index/sArticle/" in x.url for x in page4.listings)
    assert root.listings[0].raw["body_type"] == "Kombi"
    assert root.listings[0].raw["own_dealer_address"][0]["addressCountry"] == "DE"
    body, meta = fixture("autoland-c-class.html")
    modified = body.replace('data-article-id="122309"', 'data-article-id="1"', 1)
    assert (
        len(parse_mercedes_de_sources(meta["source"], modified, meta["url"]).listings)
        == 11
    )


def test_weller_stock_id_and_own_dealer_frontier():
    page = parsed("weller-c-class.html")
    assert page.total == 13
    assert len(page.listings) == 12
    assert all("/fahrzeuge/mercedes-benz/" in x.url for x in page.listings)
    assert all("market_country" not in x.raw for x in page.listings)


def test_markt_empty_search_does_not_turn_missing_contract_into_zero():
    page = parsed("markt-c180.html")
    assert page.total == 0
    assert page.pages == 1
    assert page.search_filters["native_empty_search"] is True
    assert (
        parse_mercedes_de_sources(
            "markt_de", "<h1>Mercedes C180</h1>", "https://www.markt.de/"
        ).total
        is None
    )


@pytest.mark.parametrize("page", [0, -1, True, 201, 1.5, "2"])
def test_request_bounds_fail_closed(page):
    with pytest.raises(ValueError):
        mercedes_de_page_request("pkw_de", PKW_SEARCH, page)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.pkw.de/",
        "https://www.pkw.de.evil.test/",
        "https://user@www.pkw.de/",
        "https://www.pkw.de:8080/",
        "file:///tmp/native.json",
    ],
)
def test_public_host_credentials_scheme_and_port_boundaries(url):
    assert not parse_mercedes_de_sources("pkw_de", "{}", url).listings
    with pytest.raises(ValueError):
        mercedes_de_page_request("pkw_de", url)


def test_bounded_collection_stops_before_second_request_and_keeps_sedan():
    body, meta = fixture("pkw-c180-api.json")
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(url, url, 200, body, meta["captured_at"])

    result = collect_mercedes_de_source(
        get, {"source": "pkw_de", "url": PKW_SEARCH}, max_pages=10, max_requests=1
    )
    assert len(calls) == 1
    assert result["status"] == "partial"
    assert "request_limit_reached" in result["errors"]
    assert len(result["listings"]) == 20
    assert result["listings"][0].raw["body_type"] == "Kombi"
    assert result["infer_missing"] is False


def test_bounded_collection_detects_repeated_source_page():
    body, meta = fixture("pkw-c180-api.json")

    def get(url, **kwargs):
        data = json.loads(body)
        data["page"] = 1 if "page=1" in url else 2
        return Response(url, url, 200, json.dumps(data), meta["captured_at"])

    result = collect_mercedes_de_source(
        get, {"source": "pkw_de", "url": PKW_SEARCH}, max_pages=2
    )
    assert "repeated_native_page" in result["errors"]
    assert result["enumeration_status"] == "partial"


def test_disabled_query_never_requests_network():
    def forbidden(*args, **kwargs):
        raise AssertionError("network called")

    assert (
        collect_mercedes_de_source(forbidden, {"enabled": False})["status"]
        == "disabled"
    )


def test_observed_combined_autohero_post_has_petrol_body_year_and_gear():
    page = parsed("autohero-combined-ui-4.json")
    _, meta = fixture("autohero-combined-ui-4.json")
    assert (page.total, len(page.listings)) == (12, 12)
    assert {"field": "fuelType", "op": "eq", "value": ["1039"]} in meta["request_body"][
        "variables"
    ]["search"]["filter"]["value"]
    assert all(v.scope == "unknown" for v in page.listings)


def test_complete_census_remains_distinct_from_unrequested_details():
    body, meta = fixture("autohero-combined-ui-4.json")

    def get(url, **kwargs):
        return Response(url, url, 200, body, meta["captured_at"])

    query = {
        "source": "autohero_de",
        "url": AUTOHERO_API,
        "request_body": meta["request_body"],
    }
    result = collect_mercedes_de_source(get, query, max_pages=2, max_details=0)
    assert result["enumeration_status"] == "complete"
    assert result["detail_status"] == result["status"] == "partial"
    assert result["details_expected"] == 12
    assert result["details_received"] == 0


def test_detail_403_stops_host_requests_without_invalidating_query_census():
    body, meta = fixture("autohero-combined-ui-4.json")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(
            url,
            url,
            200 if kwargs.get("request_body") else 403,
            body if kwargs.get("request_body") else "access denied",
            meta["captured_at"],
        )

    query = {
        "source": "autohero_de",
        "url": AUTOHERO_API,
        "request_body": meta["request_body"],
    }
    result = collect_mercedes_de_source(get, query, max_details=12)
    assert len(calls) == 2
    assert result["enumeration_status"] == "complete"
    assert result["detail_status"] == result["status"] == "partial"
    assert result["enumeration_errors"] == []
    assert result["detail_errors"][0].startswith("http_403:")
    assert len(result["listings"]) == 12


def test_real_detail_generation_proof_and_missing_generation_under_parent_context():
    from car_market.model_profiles import (
        MERCEDES_PROFILE,
        qualify_mercedes,
        use_profile,
    )

    with use_profile(MERCEDES_PROFILE, "DE"):
        own = parsed("autohero-c205-detail.html").listings[0]
        qualify_mercedes(own, "DE")
        assert own.scope == "target"
        assert own.raw["model_scope_evidence"]["generation"]["kind"] == "own_vin_series"
        unproven = parsed("pkw-detail-142208737556145407.json").listings[0]
        qualify_mercedes(unproven, "DE")
        assert unproven.scope == "needs_confirmation"
        assert (
            "generation_unconfirmed" in unproven.raw["model_scope_evidence"]["missing"]
        )


def test_config_separates_research_scope_and_disabled_routes():
    config = json.loads(
        (
            FIXTURES.parents[1]
            / "tests/fixtures/legacy-config/sources-mercedes-de.json"
        ).read_text()
    )
    assert config["scope"]["country"] == "DE"
    assert config["scope"]["model_profile"] == "mercedes_c180_c205"
    assert config["scope"]["transmissions"] == ["automatic"]
    assert config["scope"]["generation"] == "C205"
    assert config["scope"]["price_ceiling"] is None
    own_queries = [
        q
        for q in config["queries"]
        if q.get("collection_callable")
        == "car_market.mercedes_de_sources.collect_mercedes_de_source"
    ]
    assert len(own_queries) == 24
    assert all(q["infer_missing"] is False and q["status"] for q in own_queries)
    assert all(
        q["enabled"] is False
        for q in own_queries
        if q["coverage_status"] == "unverified_disabled"
    )
    assert not any(
        "autoscout" in q["source"] or "mercedes_benz_" in q["source"]
        for q in own_queries
    )


def test_real_other_engine_own_body_and_generation_frontier_are_retained():
    from car_market.model_profiles import qualify_mercedes

    item = parsed("autohero-other-engine-detail.html").listings[0]
    assert "C 300" in item.title
    assert item.raw["body_type"] == "Coupé"
    assert item.vin == "W1KWJ8EBXPG130181"
    assert item.raw["built_year"] == 2023
    assert item.raw.get("generation") is None
    assert item.scope == "unknown"
    qualify_mercedes(item, "DE")
    assert item.scope == "excluded"
    assert item.raw["own_inventory_record"]["subType"].startswith("C 300")


def test_foreign_native_autohero_country_is_not_changed_to_de_by_context():
    from car_market.model_profiles import qualify_mercedes

    body, meta = fixture("autohero-combined-ui-4.json")
    document = json.loads(body)
    document["data"]["searchAdV9AdsV2"]["data"] = [
        document["data"]["searchAdV9AdsV2"]["data"][0]
    ]
    document["data"]["searchAdV9AdsV2"]["data"][0]["countryCode"] = "NL"
    item = parse_mercedes_de_sources(
        "autohero_de", json.dumps(document), meta["url"]
    ).listings[0]
    assert item.raw["source_country_evidence"]["value"] == "NL"
    qualify_mercedes(item, "DE")
    assert item.raw["market_country"] == "NL"
    assert item.scope == "excluded"
