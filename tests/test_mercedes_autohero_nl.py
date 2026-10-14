import copy
import hashlib
import json
from pathlib import Path

import pytest
from car_market.mercedes_autohero_nl import (
    AUTOHERO_API,
    autohero_nl_page_request,
    parse_mercedes_autohero_nl,
)

ROOT = Path(__file__).parent / "fixtures/mercedes_autohero_nl"
DETAIL_URL = (
    "https://www.autohero.com/nl/mercedes-benz-c-klasse/id/1259063a-731a-4230-a424-c269d810e650/"
)


def fixture(name):
    return json.loads((ROOT / name).read_text())


def parse_detail(doc):
    return parse_mercedes_autohero_nl(json.dumps(doc), DETAIL_URL)


def detail_record(doc):
    return next(iter(doc["ROOT_QUERY"].values()))


def coupe_document():
    # Synthetic positive mutation is deliberately distinct from live Cabrio.
    doc = fixture("c180-detail.json")
    own = detail_record(doc)
    own["bodyType"] = "Coupé"
    own["vin"] = "WDD2053401F701985"
    return doc


def test_lossless_native_fixtures_have_own_http_provenance():
    for name, meta in fixture("provenance.json").items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == meta["sha256"]
        assert meta["original"]["status"] == 200
        assert meta["original"]["parser_url"].startswith("https://www.autohero.com/")
        assert len(meta["original"]["sha256"]) == 64


def test_live_cclass_native_post_three_rows_and_c180_one():
    page = parse_mercedes_autohero_nl(
        (ROOT / "cclass-search.json").read_text(), AUTOHERO_API + "?pageNumber=1"
    )
    assert (page.total, page.pages, page.page, len(page.listings)) == (3, 1, 1, 3)
    assert {i.source for i in page.listings} == {"autohero"}
    assert all(i.raw["country_code"] == "NL" for i in page.listings)
    assert [i.scope for i in page.listings].count("needs_confirmation") == 1
    c180 = parse_mercedes_autohero_nl((ROOT / "c180-search.json").read_text(), AUTOHERO_API)
    assert c180.total == 1
    assert c180.listings[0].scope == "needs_confirmation"
    assert c180.listings[0].raw["body_type"] is None
    assert c180.listings[0].raw["generation"] is None
    assert c180.listings[0].raw["own_first_registration"] == "2018-02-27"
    assert c180.listings[0].price == 26250
    assert c180.listings[0].raw["monthly_payment"]["amountMinorUnits"] != 2625000


def test_actual_c180_detail_is_cabrio_full_vin_own_nl_date_address():
    doc = fixture("c180-detail.json")
    item = parse_detail(doc).listings[0]
    assert item.scope == "excluded"
    assert item.vin == "WDD2054401F701985"
    assert item.raw["body_type"] == "Cabrio"
    assert item.raw["country_code"] == "NL"
    assert item.raw["own_first_registration"] == "2018-02-27"
    assert item.kenteken == "RZ894H"
    assert item.raw["own_dealer_address"]["street"] == "Karolusstraat 5"
    assert item.raw["own_inventory_record"] == detail_record(doc)
    assert item.price == 26250


def test_own_vin_2053_and_coupe_positive_without_context_manager():
    assert parse_detail(coupe_document()).listings[0].scope == "target"


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"vin": None}, "needs_confirmation"),
        ({"vin": "WDD2043401F701985"}, "excluded"),
        ({"bodyType": None}, "needs_confirmation"),
        ({"bodyType": "Sedan"}, "excluded"),
        ({"retailCountry": "DE"}, "excluded"),
        ({"retailCountry": None}, "needs_confirmation"),
        ({"registration": None, "builtYear": 2018}, "needs_confirmation"),
        ({"registration": "2015-12-31"}, "excluded"),
        ({"registration": "2099-01-01"}, "needs_confirmation"),
        ({"subType": "C 200"}, "excluded"),
        ({"published": False}, "excluded"),
        ({"state": "sold"}, "excluded"),
    ],
)
def test_own_evidence_gates(changes, expected):
    doc = coupe_document()
    detail_record(doc).update(changes)
    assert parse_detail(doc).listings[0].scope == expected


def test_typed_own_generation_requires_disclosure_not_numeric_mapping():
    doc = coupe_document()
    own = detail_record(doc)
    own["vin"] = None
    own["modelGeneration"] = {"raw": 12, "formatted": "205"}
    assert parse_detail(doc).listings[0].scope == "target"
    own["modelGeneration"] = {"raw": 12}
    assert parse_detail(doc).listings[0].scope == "needs_confirmation"


@pytest.mark.parametrize(
    "change", [{"adId": "b4aed1b4-b4ee-4d87-be55-8c25958e556d"}, {"carUrlTitle": "another-car"}]
)
def test_detail_uri_identity_and_slug_must_match(change):
    doc = fixture("c180-detail.json")
    detail_record(doc).update(change)
    assert parse_detail(doc).listings == []


def test_domain_query_and_country_of_origin_never_supply_sale_country():
    doc = fixture("c180-search.json")
    row = doc["data"]["searchAdV9AdsV2"]["data"][0]
    row.pop("countryCode")
    row["countryOfOrigin"] = "Nederland"
    page = parse_mercedes_autohero_nl(
        json.dumps(doc), AUTOHERO_API + "?country=NL&bodyType=Coupe&generation=C205"
    )
    item = page.listings[0]
    assert item.raw["country_code"] is None
    assert item.raw["body_type"] is None
    assert item.raw["generation"] is None
    assert item.scope == "needs_confirmation"


def test_price_monthly_net_and_callback_facts_are_separate():
    doc = coupe_document()
    own = detail_record(doc)
    own.update(
        price=None,
        monthlyPayment={"amountMinorUnits": 35000, "currency": "EUR", "conversionMajor": 100},
        isNetPrice=True,
        callbackAvailable=True,
    )
    item = parse_detail(doc).listings[0]
    assert item.price is None
    assert item.price_type == "net_price"
    assert item.raw["monthly_payment"]["amountMinorUnits"] == 35000
    assert item.raw["public_callbacks"] == {"callbackAvailable": True}
    assert item.raw["vat_type"] == 1053


def test_page_request_clones_observed_body_preserves_ui_properties():
    body = fixture("request-cclass.json")
    original = copy.deepcopy(body)
    request = autohero_nl_page_request(AUTOHERO_API, 3, body)
    assert body == original
    assert request["method"] == "POST"
    assert request["url"] == AUTOHERO_API + "?pageNumber=3"
    assert request["body"]["variables"]["search"]["offset"] == 48
    request["body"]["variables"]["search"]["offset"] = 0
    assert request["body"] == original


@pytest.mark.parametrize(
    "field,value", [("countryCode", "DE"), ("make", "130"), ("model", ["2er"])]
)
def test_request_refuses_bmw_de_or_unverified_model(field, value):
    body = fixture("request-cclass.json")

    def mutate(node):
        if isinstance(node, dict):
            if node.get("field") == field:
                node["value"] = value
            else:
                for child in node.values():
                    mutate(child)
        elif isinstance(node, list):
            for child in node:
                mutate(child)

    mutate(body)
    with pytest.raises(ValueError):
        autohero_nl_page_request(AUTOHERO_API, 1, body)


def test_error_or_foreign_host_not_a_public_inventory():
    assert (
        parse_mercedes_autohero_nl('{"errors":[{"message":"blocked"}]}', AUTOHERO_API).listings
        == []
    )
    assert (
        parse_mercedes_autohero_nl(
            (ROOT / "c180-search.json").read_text(), "https://other.invalid/nl/search/"
        ).listings
        == []
    )


def test_request_requires_mandatory_and_filters_and_positive_page():
    body = fixture("request-cclass.json")
    body["variables"]["search"]["filter"]["op"] = "or"
    with pytest.raises(ValueError):
        autohero_nl_page_request(AUTOHERO_API, 1, body)
    for page in (0, -1, True, "1", 201):
        with pytest.raises(ValueError):
            autohero_nl_page_request(AUTOHERO_API, page, fixture("request-cclass.json"))
    assert (
        autohero_nl_page_request(AUTOHERO_API, 200, fixture("request-cclass.json"))["body"][
            "variables"
        ]["search"]["offset"]
        == 4776
    )


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"state": "sold"}, "sold"),
        ({"retailAdState": "reserved"}, "reserved"),
        ({"availabilityStatus": "sold"}, "sold"),
        ({"sold": True}, "sold"),
        ({"reserved": True}, "reserved"),
        ({"published": False}, "historical_only"),
        ({"state": "archived"}, "historical_only"),
        ({"historicalOnly": True}, "historical_only"),
    ],
)
def test_inactive_raw_markers_survive_outer_requalification(changes, status):
    from car_market.model_profiles import qualify_mercedes

    doc = coupe_document()
    detail_record(doc).update(changes)
    item = parse_detail(doc).listings[0]
    assert item.scope == "excluded"
    assert item.raw["availability_status"] == status
    assert item.raw["sold"] is (status == "sold")
    assert item.raw["reserved"] is (status == "reserved")
    qualify_mercedes(item, "NL")
    # Collector/catalog must consume these durable flags, even after requalify.
    assert item.raw["historical_only"] is True
    assert item.raw["own_availability_exclusion"] is True


def test_actual_detail_equipment_text_and_native_gallery_are_lossless():
    own = detail_record(fixture("c180-detail.json"))
    item = parse_detail(fixture("c180-detail.json")).listings[0]
    assert "BURMESTER Geluidssysteem" in item.description
    assert "Cabriodak Soft" in item.description
    assert item.raw["own_description_fields"]["features"] == own["features"]
    assert item.raw["own_gallery"] == own["carDetailsImageComposites"]
    assert own["carDetailsImageComposites"]["exterior"][0]["ahUrl"] in item.images
    assert len(item.images) == len(set(item.images)) > 20
    assert item.raw["image_urls_are_native_templates"] is True


def test_active_native_record_has_no_historical_marker():
    item = parse_detail(coupe_document()).listings[0]
    assert item.scope == "target"
    assert not item.raw.get("historical_only")
    assert not item.raw.get("own_availability_exclusion")


def test_query_scope_stays_partial_even_when_counter_fits_one_page():
    page = parse_mercedes_autohero_nl((ROOT / "cclass-search.json").read_text(), AUTOHERO_API)
    assert page.total == len(page.listings) == 3
    assert page.search_filters["scope_partial"] is True
    assert "prospective_stock_included" in page.search_filters["scope_limitations"]


def test_c180_request_retains_verified_native_subtype_filter():
    body = fixture("request-c180.json")
    request = autohero_nl_page_request(AUTOHERO_API, 1, body)
    assert request["body"] == body


def test_search_missing_slug_never_invents_publication_uri():
    doc = fixture("c180-search.json")
    doc["data"]["searchAdV9AdsV2"]["data"][0].pop("carUrlTitle")
    assert parse_mercedes_autohero_nl(json.dumps(doc), AUTOHERO_API).listings == []


def test_html_apollo_and_json_string_inventory_are_supported():
    doc = fixture("c180-search.json")
    payload = doc["data"]["searchAdV9AdsV2"]
    doc["data"]["searchAdV9AdsV2"] = json.dumps(payload)
    assert parse_mercedes_autohero_nl(json.dumps(doc), AUTOHERO_API).total == 1
    key = "searchAdV9AdsV2:" + json.dumps(
        {"search": {"filter": {"field": "countryCode", "op": "eq", "value": "NL"}}}
    )
    state = json.dumps(json.dumps({"ROOT_QUERY": {key: payload}}))
    html = "<script>window.__APOLLO_STATE__=" + state + ";</script>"
    assert parse_mercedes_autohero_nl(html, "https://www.autohero.com/nl/search/").total == 1
