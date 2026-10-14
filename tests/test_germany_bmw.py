"""Retained official BMW API responses and negative contract variations."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from car_market.germany_bmw import (
    BMW_SEED_URL,
    bmw_2er_page_request,
    bmw_broad_page_request,
    bmw_page_request,
    bmw_publication_id,
    browser_bmw_request,
    bundle_bmw_inventory,
    parse_bmw,
)

FIXTURES = Path(__file__).parent / "fixtures/germany_bmw"
MANIFEST = {
    entry["fixture"]: entry for entry in json.loads((FIXTURES / "manifest.json").read_text())
}
ACTIVE = "active-tourer-inventory.json"
CONTROL = "national-control-inventory.json"
DOC_ID = "LMPAOAEFGIJMI898P"
ID = "019dd382-6076-73d7-b2c0-d5e62d9f62c1"
REQUEST = json.loads((FIXTURES / "captured-request.json").read_text())


def document(name=ACTIVE):
    return json.loads((FIXTURES / name).read_text())


def parsed(doc=None, url=None):
    return parse_bmw(
        json.dumps(doc if doc is not None else document()), url or MANIFEST[ACTIVE]["final_url"]
    )


def first(doc=None):
    return parsed(doc).listings[0]


def vehicle(doc):
    return doc["hits"][0]["vehicle"]


def remove_cash(doc, net=False):
    own = vehicle(doc)
    own["price"].pop("grossSalesPrice", None)
    own["offering"]["offerPrices"]["51646"].pop("offerGrossPrice", None)
    if net:
        own["price"].pop("netSalesPrice", None)
        own["offering"]["offerPrices"]["51646"].pop("offerNetPrice", None)


def test_fixtures_retain_own_records_and_transport_labels():
    assert len(MANIFEST) == 5
    for name, entry in MANIFEST.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == entry["fixture_sha256"]
        assert entry["http_status"] == 200 and entry["method"] == "POST"
        assert entry["transport"] in {
            "browser_isolated_chrome_no_auth",
            "isolated_anonymous_Chrome_native_UI_action",
        }
        assert entry["fetched_at"].startswith("2026-10-07")
        own = document(name)
        assert len(own.get("bmw_native_inventory", own)["hits"]) == 12


def test_real_native_inventory_preserves_counters_and_all_control_models():
    page = parsed()
    assert (page.total, page.pages, page.page, len(page.listings)) == (1356, 113, 1, 12)
    assert page.search_filters["country"] == "DE"
    assert page.search_filters["coverage_status"] == "browser_pagination_unverified"
    assert sum(item.scope == "target" for item in page.listings) == 7
    assert sum(item.scope == "excluded" for item in page.listings) == 5
    assert {item.raw["generation"] for item in page.listings} == {"F45"}
    assert all(item.kenteken is None for item in page.listings)
    assert all("publication_reference" not in item.raw for item in page.listings)
    assert all(item.url == BMW_SEED_URL for item in page.listings)
    assert all(item.raw["publication_url_verified"] is False for item in page.listings)


def test_real_national_control_is_not_limited_to_U06_or_active_tourer():
    page = parsed(document(CONTROL), MANIFEST[CONTROL]["final_url"])
    assert (page.total, page.pages, len(page.listings)) == (24957, 2080, 12)
    assert sum(item.scope == "target" for item in page.listings) == 2
    assert {item.raw["generation"] for item in page.listings} >= {"F45", "F20", "E84"}


def test_real_purchase_price_is_not_list_price_or_installment():
    item = first()
    assert (item.external_id, item.price, item.price_type) == (ID, 9755, "advertised_price")
    assert item.raw["native_price"]["grossListPrice"] == 35349.99
    assert item.raw["native_price"]["vehicleGrossPrice"] == 35349.99
    assert item.raw["monthly_payments"][0]["amount"] == 89.15
    assert item.raw["monthly_payments"][0]["parameters"]["downPaymentAmount"]["amount"] == 1951
    assert item.raw["purchase_price_path"] == "offering.offerPrices.51646.offerGrossPrice"
    assert item.vin == "WBA2A31070V855929"
    assert item.raw["native_document_id"] == DOC_ID
    assert item.raw["vehicle_life_cycle"]["licencePlate"] == "V855929"
    assert item.raw["dealer_stock_id"] == "089466"
    assert (item.year, item.mileage, item.transmission, item.fuel, item.city) == (
        2017,
        149452,
        "manual",
        "petrol",
        "Plauen",
    )
    assert item.raw["detail_complete"] is False
    assert item.description == "Unfallfreiheit wird nicht zugesichert."
    assert len(item.images) == 16


def test_real_damage_and_production_year_are_not_exclusion_filters():
    item = parsed().listings[1]
    assert item.scope == "target" and item.price == 9770
    assert item.raw["vehicle_life_cycle"]["involvedInAccident"] is True
    assert item.raw["production_date"] == "2015-11-17"
    assert item.raw["first_registration"] == "2016-03-01"
    assert item.year == 2016


def test_real_own_family_description_qualifies_native_218I_without_title_suffix():
    item = parsed().listings[2]
    assert (
        vehicle(document())["vehicleSpecification"]["modelAndOption"]["marketingModelRange"]
        == "2_U06"
    )
    assert item.raw["generation"] == "F45"
    assert "Active Tourer" in item.raw["model_family"]
    assert item.scope == "target"


@pytest.mark.parametrize(
    "field,value", [("vin17", "V855929"), ("vin17", "WBA2A31070"), ("vin17", None)]
)
def test_partial_VIN_never_falls_back_to_vin7_vin10_or_stock(field, value):
    doc = document()
    vehicle(doc)["ordering"]["productionData"][field] = value
    item = first(doc)
    assert item.vin is None and item.kenteken is None
    assert item.external_id == ID


@pytest.mark.parametrize(
    "path,value",
    [
        ("vehicle", "NL"),
        ("outlet", "NL"),
        ("hit", "NL"),
        ("vehicle", ""),
        ("outlet", None),
    ],
)
def test_country_comes_from_own_vehicle_and_outlet_not_API_language(path, value):
    doc = document()
    if path == "vehicle":
        vehicle(doc)["country"] = value
    elif path == "outlet":
        vehicle(doc)["ordering"]["retailData"]["locationOutletAddress"]["countryCode"] = value
    else:
        doc["hits"][0]["country"] = value
    item = first(doc)
    if value == "NL":
        assert item.scope == "excluded" and "market_country" not in item.raw
        assert "country" not in parsed(doc).search_filters
    else:
        assert item.raw["market_country"] == "DE"


def test_no_country_proof_cannot_be_invented_from_the_endpoint():
    doc = document()
    for hit in doc["hits"]:
        hit["vehicle"].pop("country")
        hit["vehicle"]["ordering"]["retailData"]["locationOutletAddress"].pop("countryCode")
    page = parsed(doc)
    assert all("market_country" not in item.raw for item in page.listings)
    assert "country" not in page.search_filters
    assert page.listings[0].scope == "needs_confirmation"
    assert page.listings[6].scope == "excluded"


def test_generic_JSON_LD_or_nested_recommendations_are_not_native_inventory():
    doc = document()
    fake = copy.deepcopy(doc["hits"][0])
    fake["vehicle"]["documentId"] = "RECOMMENDATION00001"
    doc["recommendations"] = [fake]
    page = parsed(doc)
    assert "RECOMMENDATION00001" not in {item.external_id for item in page.listings}
    assert parsed({"@type": "ItemList", "itemListElement": [fake], "totalCount": 1}).total is None


@pytest.mark.parametrize(
    "body",
    [
        "<html>Forbidden</html>",
        '{"aggregateGeneric":[],"totalCount":1356}',
        '{"metadata":{"totalCount":0}}',
    ],
)
def test_non_inventory_response_never_becomes_verified_empty_stock(body):
    page = parse_bmw(body, REQUEST["url"])
    assert page.listings == [] and page.total is None and page.pages is None
    assert page.search_filters["parse_status"] == "not_inventory"


def test_counter_zero_and_unknown_are_distinct():
    page = parsed({"hits": [], "metadata": {"totalCount": 0}})
    assert (page.total, page.pages, page.page) == (0, 0, 1)
    page = parsed({"hits": [], "metadata": {}})
    assert page.total is None and page.pages is None


def test_unknown_native_ID_is_not_fabricated_from_VIN_or_vss():
    doc = document()
    vehicle(doc).pop("documentId")
    page = parsed(doc)
    assert len(page.listings) == 11
    assert page.total == 1356 and page.search_filters["parse_status"] == "partial_inventory"


def test_current_outlet_offer_and_native_sales_fallback_have_explicit_paths():
    doc = document()
    vehicle(doc)["offering"]["offerPrices"]["other"] = {"offerGrossPrice": 1}
    assert first(doc).price == 9755
    vehicle(doc)["offering"]["offerPrices"]["51646"].pop("offerGrossPrice")
    assert first(doc).price == 9755
    assert first(doc).raw["purchase_price_path"] == "price.grossSalesPrice"


def test_net_only_amount_is_kept_separately_from_purchase_cash():
    doc = document()
    remove_cash(doc)
    item = first(doc)
    assert (item.price, item.price_type) == (9755, "net_price")
    assert item.raw["price_warning"] == "net_price" and item.scope == "target"


def test_monthly_only_never_uses_list_price_downpayment_or_total_payments():
    doc = document()
    remove_cash(doc, net=True)
    item = first(doc)
    assert (item.price, item.price_type) == (89.15, "monthly_payment")
    assert item.raw["price_warning"] == "monthly_payment" and item.scope == "target"


def test_missing_sale_and_loan_price_remains_unknown_with_existing_list_price():
    doc = document()
    remove_cash(doc, net=True)
    vehicle(doc)["offering"]["sfOffers"] = []
    item = first(doc)
    assert item.price is None and item.price_type == "unknown"


def test_foreign_purchase_currency_is_not_relabelled_EUR():
    doc = document()
    vehicle(doc)["price"]["listPriceCurrency"] = "CHF"
    vehicle(doc)["offering"]["sfOffers"] = []
    item = first(doc)
    assert item.price is None and item.price_type == "foreign_currency"


@pytest.mark.parametrize(
    "generation,gear,year,mileage,price",
    [
        ("F45", "MANUAL", "2015-01-01", 200000, 90000),
        ("U06", "AUTOMATIC", "2026-01-01", 0, 45000),
    ],
)
def test_scope_interface_has_no_generation_price_age_mileage_or_gear_exclusion(
    generation, gear, year, mileage, price
):
    # A contract mutation, not a claim about a real U06 advertisement.
    doc = document()
    own = vehicle(doc)
    spec = own["vehicleSpecification"]["modelAndOption"]
    spec["modelRange"]["name"] = generation
    spec["modelRange"]["description"]["de_DE"] = "2er Active Tourer - " + generation
    spec["transmission"] = gear
    own["ordering"]["retailData"]["initialRegistrationDate"] = year
    own["vehicleLifeCycle"]["mileage"]["km"] = mileage
    own["offering"]["offerPrices"]["51646"]["offerGrossPrice"] = price
    item = first(doc)
    assert item.scope == "target" and item.price == price and item.mileage == mileage


@pytest.mark.parametrize(
    "brand,derivative,name,family",
    [
        ("MINI", "218I", "218i Active Tourer", "Active Tourer"),
        ("BMW", "218D", "218d", "Active Tourer"),
        ("BMW", "218I", "218i Gran Tourer", "Gran Tourer"),
        ("BMW", "218I", "218i Coupe", "Coupe"),
    ],
)
def test_own_model_controls_cannot_become_targets(brand, derivative, name, family):
    doc = document()
    spec = vehicle(doc)["vehicleSpecification"]["modelAndOption"]
    spec["brand"] = brand
    spec["model"]["derivative"] = derivative
    spec["model"]["modelName"] = name
    spec["modelRange"]["description"] = {"de_DE": family}
    assert first(doc).scope == "excluded"


def test_captured_post_pagination_preserves_body_hash_and_national_dealer_roster():
    original = copy.deepcopy(REQUEST)
    request = bmw_page_request(REQUEST["url"], 2, REQUEST["request_body"])
    assert parse_qs(urlsplit(request["url"]).query)["startIndex"] == ["12"]
    assert request["request_body"] == REQUEST["request_body"]
    assert len(request["request_body"]["searchContext"][0]["buNos"]) > 400
    assert request["method"] == "POST" and request["bootstrap_url"] == BMW_SEED_URL
    assert request["coverage_status"] == "browser_pagination_unverified"
    assert REQUEST == original
    request["request_body"]["searchContext"][0]["buNos"].clear()
    assert REQUEST == original


def test_broad_request_removes_entire_model_restriction_without_guessing_F45_code():
    request = bmw_broad_page_request(REQUEST["url"], 1, REQUEST["request_body"])
    context = request["request_body"]["searchContext"][0]
    assert set(context) == {"buNos"}
    assert context["buNos"] == REQUEST["request_body"]["searchContext"][0]["buNos"]
    assert request["request_body"]["resultsContext"] == REQUEST["request_body"]["resultsContext"]
    assert REQUEST["request_body"]["searchContext"][0]["model"]["marketingModelRange"]["value"] == [
        "2_U06"
    ]


def test_unknown_broad_filter_requires_review_rather_than_claiming_all_stock():
    body = copy.deepcopy(REQUEST["request_body"])
    body["searchContext"][0]["radius"] = 20
    with pytest.raises(ValueError, match="unreviewed"):
        bmw_broad_page_request(REQUEST["url"], 1, body)


@pytest.mark.parametrize("page", [0, -1, True, 1.5])
def test_page_number_must_be_positive_integer(page):
    with pytest.raises(ValueError):
        bmw_page_request(REQUEST["url"], page, REQUEST["request_body"])


@pytest.mark.parametrize(
    "change",
    [
        "startIndex=1",
        "maxResults=0",
        "maxResults=24",
        "brand=MINI",
        "foreign_market",
        "foreign_host",
        "user_info",
        "fragment",
    ],
)
def test_request_URI_cannot_drift_away_from_the_captured_contract(change):
    url = REQUEST["url"]
    if "=" in change:
        key = change.split("=")[0]
        previous = {"startIndex": "0", "maxResults": "12", "brand": "BMW"}[key]
        url = url.replace(key + "=" + previous, change)
    elif change == "foreign_market":
        url = url.replace("/de-de/", "/nl-nl/")
    elif change == "foreign_host":
        url = url.replace("vehicle-selection.prod.stolo.eu-central-1.aws.bmw.cloud", "evil.example")
    elif change == "user_info":
        url = url.replace("https://", "https://user@")
    else:
        url += "#other"
    with pytest.raises(ValueError):
        bmw_page_request(url, 2, REQUEST["request_body"])


def test_direct_HTTP403_evidence_is_not_recast_as_browser_success():
    for name in ("bmw-api-minimal.meta.json", "bmw-api-page2.meta.json"):
        record = json.loads((FIXTURES / name).read_text())
        assert record["status"] == 403
        assert record["transport"] == "http_urllib_no_auth"
        assert record["response_body_not_retained"] is True


def test_browser_contract_uses_anonymous_fetch_and_preserves_a_403_without_retry():
    calls = []
    response = {"status": 403, "final_url": REQUEST["url"], "body": ""}

    def evaluate(script, arguments):
        calls.append((script, arguments))
        return response

    page = SimpleNamespace(url=BMW_SEED_URL + "/results", evaluate=evaluate)
    request = bmw_page_request(REQUEST["url"], 2, REQUEST["request_body"])
    assert browser_bmw_request(page, request) is response
    assert len(calls) == 1 and "credentials: 'omit'" in calls[0][0]
    assert calls[0][1]["request_body"] == REQUEST["request_body"]
    request["headers"]["Authorization"] = "contract-only-placeholder"
    with pytest.raises(ValueError, match="anonymous"):
        browser_bmw_request(page, request)
    assert len(calls) == 1


def test_browser_bootstrap_cannot_be_an_arbitrary_site():
    page = SimpleNamespace(url="https://evil.example/")
    with pytest.raises(ValueError, match="bootstrap"):
        browser_bmw_request(page, bmw_page_request(REQUEST["url"], 1, REQUEST["request_body"]))


def test_multiple_monthly_products_do_not_select_an_arbitrary_rate():
    doc = document()
    remove_cash(doc, net=True)
    offers = vehicle(doc)["offering"]["sfOffers"]
    alternate = copy.deepcopy(offers[0]["calculations"][0])
    alternate["financialProductParameters"]["totalInstallment"]["amount"] = 123
    offers[0]["calculations"].append(alternate)
    item = first(doc)
    assert item.price is None and item.price_type == "monthly_payment"
    assert len(item.raw["monthly_payments"]) == 2


def test_broad_query_cannot_hide_a_price_or_location_restriction():
    with pytest.raises(ValueError, match="unreviewed"):
        bmw_broad_page_request(REQUEST["url"] + "&minPrice=10000", 1, REQUEST["request_body"])


@pytest.mark.parametrize("suffix", ["#other", "-unobserved"])
def test_browser_contract_rejects_non_public_origin_credentials_or_wrong_path(suffix):
    uri = "https://user@www.bmw.de/de-de/sl/gebrauchtwagen" + suffix
    page = SimpleNamespace(url=uri)
    with pytest.raises(ValueError, match="bootstrap"):
        browser_bmw_request(page, bmw_page_request(REQUEST["url"], 1, REQUEST["request_body"]))


def test_malformed_description_is_not_serialized_as_seller_text():
    doc = document()
    vehicle(doc)["vehicleLifeCycle"]["vehicleDamage"]["damageDescription"] = {"unrelated": "218d"}
    assert first(doc).description is None
    assert first(doc).scope == "target"


def test_short_last_page_keeps_search_filters_stable():
    doc = document()
    doc["hits"] = doc["hits"][:3]
    assert parsed(doc).search_filters == parsed().search_filters


def test_authoritative_facet_group_2_U06_contains_both_F45_and_U06():
    facet = document("series-2-facet.json")
    request = bmw_2er_page_request(
        REQUEST["url"], 1, REQUEST["request_body"], facet, active_only=True
    )
    assert request["model_ranges"] == ["2_U06"]
    assert request["generation_coverage"] == ["F45", "U06"]
    assert "2_F45" not in request["model_ranges"]
    request = bmw_2er_page_request(REQUEST["url"], 1, REQUEST["request_body"], facet)
    assert request["model_ranges"] == ["2_U06", "2_F44", "2_G42", "M2_F87", "2_F46", "2_F23"]
    assert request["transport"] == "browser_native_UI"


@pytest.mark.parametrize("number", [1, 2])
def test_real_own_UI_bundle_binds_stock_and_publication_IDs_to_actual_URI(number):
    name = f"all-2er-page{number}-own-UI-bundle.json"
    page = parse_bmw((FIXTURES / name).read_text(), MANIFEST[name]["final_url"])
    assert page.page == number and page.total == 2537
    assert len(page.listings) == 12
    assert all(item.raw["publication_url_verified"] for item in page.listings)
    assert all(bmw_publication_id(item.url) == item.external_id for item in page.listings)
    assert all(item.raw["publication_reference"]["url"] == item.url for item in page.listings)
    assert all(item.raw["native_document_id"] != item.external_id for item in page.listings)


def test_real_U06_native_numeric_hyphen_document_IDs_are_not_dropped():
    name = "all-2er-page10-native.json"
    page = parse_bmw((FIXTURES / name).read_text(), MANIFEST[name]["final_url"])
    assert len(page.listings) == 12 and page.search_filters["parse_status"] == "inventory"
    assert {item.raw["generation"] for item in page.listings} >= {"F45", "U06"}
    assert any(item.raw["native_document_id"] == "9295169-56178311" for item in page.listings)


def test_UI_link_cannot_be_guessed_from_vss_or_forged_reference_metadata():
    doc = document()
    doc["hits"][0]["vehicle"]["publication_reference"] = {
        "url": "https://www.bmw.de/de-de/sl/gebrauchtwagen/details/" + ID
    }
    page = parse_bmw(bundle_bmw_inventory(doc, "<html></html>"), REQUEST["url"])
    assert not any(item.raw["publication_url_verified"] for item in page.listings)


@pytest.mark.parametrize("change", ["host", "stock", "UUID"])
def test_mismatched_own_UI_link_does_not_transfer_publication_proof(change):
    name = "all-2er-page1-own-UI-bundle.json"
    doc = document(name)
    proof = doc["bmw_own_UI"]
    own = doc["bmw_native_inventory"]["hits"][0]["vehicle"]
    if change == "host":
        proof = proof.replace("https://www.bmw.de/", "https://evil.example/")
    elif change == "stock":
        proof = proof.replace(own["documentId"], "wrong-stock")
    else:
        proof = proof.replace(own["vssId"], "00000000-0000-4000-8000-000000000000")
    doc["bmw_own_UI"] = proof
    page = parse_bmw(json.dumps(doc), MANIFEST[name]["final_url"])
    assert page.listings[0].raw["publication_url_verified"] is False


@pytest.mark.parametrize(
    "url",
    [
        BMW_SEED_URL + "#" + ID,
        "https://www.bmw.de/de-de/sl/gebrauchtwagen/details/" + ID + "?x=1",
        "https://evil.example/de-de/sl/gebrauchtwagen/details/" + ID,
    ],
)
def test_anchor_and_foreign_URI_are_not_published_IDs(url):
    assert bmw_publication_id(url) is None
