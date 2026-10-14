"""Acceptance of own DE public payloads and negative identity boundaries."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from car_market.germany_secondary import (
    AUTOHERO_SEARCH,
    PKW_SEARCH,
    parse_secondary,
    secondary_page_request,
    secondary_page_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "germany_secondary"
MANIFEST = {x["fixture"]: x for x in json.loads((FIXTURES / "manifest.json").read_text())}


def retained(name, source):
    return parse_secondary(source, (FIXTURES / name).read_text(), MANIFEST[name]["final_url"])


def pkw_document():
    return json.loads((FIXTURES / "pkw-api-basic.json").read_text())


def autohero_state(name="autohero-first.html"):
    body = (FIXTURES / name).read_text()
    match = re.search(r'window\.__APOLLO_STATE__\s*=\s*("(?:[^"\\]|\\.)*")', body)
    return json.loads(json.loads(match[1]))


def render(doc):
    return "<script>window.__APOLLO_STATE__ = " + json.dumps(json.dumps(doc)) + ";</script>"


def test_fixtures_are_bound_by_hash():
    assert len(MANIFEST) == 6
    for name, meta in MANIFEST.items():
        assert meta["status"] == 200
        assert meta["fetched_at"].startswith("2026-10-07")
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == meta["fixture_sha256"]


def test_pkw_real_national_pages_and_country_proof():
    first = retained("pkw-api-basic.json", "pkw_de")
    second = retained("pkw-api-page2.json", "pkw_de")
    assert (first.total, first.pages, first.page, len(first.listings)) == (78, 4, 1, 20)
    assert (second.total, second.pages, second.page, len(second.listings)) == (78, 4, 2, 20)
    assert first.search_filters["country"] == second.search_filters["country"] == "DE"
    assert not {x.external_id for x in first.listings} & {x.external_id for x in second.listings}
    assert all(x.kenteken is None and x.raw["market_country"] == "DE" for x in first.listings)
    assert any(x.fuel == "diesel" and x.scope == "excluded" for x in first.listings)
    assert any(x.fuel == "petrol" for x in first.listings)
    for item in first.listings:
        assert item.raw["country_evidence"]["value"] == "DEU"
        reference = item.raw["publication_reference"]
        assert reference["external_id"] == item.external_id
        assert reference["url"] == item.url
        assert reference["kind"] == "explicit_own_publication_link"


def test_pkw_own_detail_retains_cash_net_stock_and_unknown_motor():
    page = retained("pkw-api-direct.json", "pkw_de")
    assert (page.total, page.pages, page.page, len(page.listings)) == (1, 1, 1, 1)
    item = page.listings[0]
    assert (item.external_id, item.price, item.mileage, item.year) == (
        "635878100259431844",
        16950,
        53115,
        2020,
    )
    assert (item.city, item.fuel, item.transmission, item.scope) == (
        "Hamm",
        "petrol",
        "automatic",
        "needs_confirmation",
    )
    assert item.vin is None and item.kenteken is None
    assert item.raw["dealer_stock_number"] == "62-N1"
    assert item.raw["price_net_eur"] == pytest.approx(14243.697478991597)
    assert item.raw["price_monthly_eur"] == 0
    assert item.price_type == "advertised_price" and item.description


def test_pkw_rejects_foreign_country_unbound_url_and_related_car_vin():
    doc = pkw_document()
    own = doc["results"][0]
    own["name"] = "BMW 218i Active Tourer"
    own["location"]["country"] = "NL"
    own["vin"] = "NOT-A-VIN"
    doc["recommendations"] = [{"vin": "WBA21BX0X07T40980", "country": "DE"}]
    item = parse_secondary("pkw_de", json.dumps(doc), PKW_SEARCH).listings[0]
    assert item.scope == "excluded" and item.raw["country_excluded"] == "NL"
    assert item.vin is None and item.kenteken is None
    own["url"] = doc["results"][1]["url"]
    assert own["id"] not in {
        x.external_id for x in parse_secondary("pkw_de", json.dumps(doc), PKW_SEARCH).listings
    }


def test_unknown_country_does_not_inherit_de_from_seller_or_page():
    doc = pkw_document()
    own = doc["results"][0]
    own["name"] = "BMW 218i Active Tourer"
    own["location"].pop("country")
    own["fueltype"]["name"] = "Benzin"
    item = parse_secondary("pkw_de", json.dumps(doc), PKW_SEARCH).listings[0]
    assert item.scope == "needs_confirmation"
    assert "market_country" not in item.raw and item.raw["geography_status"] == "needs_confirmation"


@pytest.mark.parametrize(
    "kind,field,expected",
    [("monthly_price", "monthly_cost", 299), ("net_price", "netto_price", 14900)],
)
def test_monthly_and_net_only_offers_are_not_cash(kind, field, expected):
    doc = pkw_document()
    own = doc["results"][0]
    own["price"] = {"customer": None, field: expected}
    item = parse_secondary("pkw_de", json.dumps(doc), PKW_SEARCH).listings[0]
    assert item.price_type == kind and item.price == expected


def test_pkw_pagination_keeps_seed_filters_not_lossy_api_next_link():
    doc = pkw_document()
    assert "brand_model" not in doc["links"][1]["href"]
    query = parse_qs(urlsplit(secondary_page_url("pkw_de", PKW_SEARCH, 4)).query)
    assert query == {"brand_model": ["9_2326"], "source": ["autokatalog"], "page": ["4"]}
    assert secondary_page_request("pkw_de", PKW_SEARCH, 2)["method"] == "GET"
    for bad in [0, True, "2"]:
        with pytest.raises(ValueError):
            secondary_page_url("pkw_de", PKW_SEARCH, bad)
    with pytest.raises(ValueError):
        secondary_page_url("pkw_de", PKW_SEARCH.replace("9_2326", "9_1"), 2)


def test_real_autohero_batches_keep_exact_model_separate_from_control_total():
    first = retained("autohero-first.html", "autohero_de")
    last = retained("autohero-last.json", "autohero_de")
    assert (first.total, first.pages, first.page, len(first.listings)) == (34, 2, 1, 24)
    assert (last.total, last.pages, last.page, len(last.listings)) == (34, 2, None, 10)
    assert last.search_filters["api_page_requires_request_context"]
    all_items = first.listings + last.listings
    assert len({x.external_id for x in all_items}) == 34
    assert sum(x.scope == "target" for x in all_items) == 13
    assert all(x.raw["market_country"] == "DE" and x.kenteken is None for x in all_items)
    assert any(x.scope == "excluded" and "Gran" in x.title for x in all_items)
    assert first.search_filters["properties"]["firstPublishedDays"] == -30
    for item in first.listings:
        ref = item.raw.get("publication_reference")
        if ref:
            assert ref["url"] == item.url and ref["external_id"] == item.external_id
        else:
            assert item.raw["url_evidence"] == "derived_from_own_id_and_carUrlTitle"


def test_real_autohero_own_detail_has_own_vin_country_stock_and_location():
    page = retained("autohero-detail.html", "autohero_de")
    assert len(page.listings) == 1
    item = page.listings[0]
    assert (item.external_id, item.price, item.year, item.mileage) == (
        "041db339-3d37-40b6-b7ae-02ba4d074401",
        12910,
        2018,
        110518,
    )
    assert (item.city, item.vin, item.transmission, item.fuel, item.scope) == (
        "Hemau",
        "WBA2A310807A61318",
        "automatic",
        "petrol",
        "target",
    )
    assert item.kenteken is None and item.raw["country_evidence"]["path"] == "retailCountry"
    assert item.raw["dealer_stock_number"] == "WW51682"
    assert item.raw["publication_reference"]["external_id"] == item.external_id


def test_autohero_detail_ignores_recommendations_and_country_of_origin():
    doc = autohero_state("autohero-detail.html")
    root = doc["ROOT_QUERY"]
    key = next(k for k in root if k.startswith("getCarDetailsStoreAd("))
    own = root[key]
    own.pop("retailCountry")
    own["countryOfOrigin"] = "DE"
    own["vin"] = "invalid"
    own["licensePlate"] = "DE-AB 123"
    root["recommendation"] = {"vin": "WBA21BX0X07T40980", "countryCode": "DE"}
    url = MANIFEST["autohero-detail.html"]["final_url"]
    item = parse_secondary("autohero_de", render(doc), url).listings[0]
    assert item.scope == "needs_confirmation" and "market_country" not in item.raw
    assert item.vin is None and item.kenteken is None
    assert item.raw["registration_identifier"] == "DE-AB 123"
    own["adId"] = "823b0eec-9595-4420-b6dd-5be448c89195"
    assert parse_secondary("autohero_de", render(doc), url).listings == []


def test_autohero_pagination_matches_actual_ui_request():
    seed = MANIFEST["autohero-first.html"]["requested_url"]
    request = secondary_page_request("autohero_de", seed, 2)
    assert request["body"] == MANIFEST["autohero-last.json"]["request_body"]
    assert request["method"] == "POST" and request["page"] == 2
    assert request["body"]["variables"]["search"]["offset"] == 24
    assert (
        secondary_page_request("autohero_de", AUTOHERO_SEARCH, 1)["body"]["variables"]["search"][
            "filter"
        ]["value"][0]["value"]
        == "DE"
    )
    with pytest.raises(ValueError):
        secondary_page_request(
            "autohero_de", AUTOHERO_SEARCH + "&subType=218i%20active%20tourer", 2
        )


@pytest.mark.parametrize(
    "source,url",
    [
        ("autohero_de", "https://www.autohero.com/nl/search/"),
        ("pkw_de", "https://www.pkw.de.evil.example/fahrzeuge"),
        ("autouncle_de", "https://www.autouncle.de/"),
    ],
)
def test_unsupported_or_foreign_routes_do_not_become_german_inventory(source, url):
    page = parse_secondary(source, (FIXTURES / "autohero-first.html").read_text(), url)
    assert page.listings == [] and "country" not in page.search_filters
