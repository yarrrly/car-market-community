import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.germany_dealers import parse_dealer

FIXTURES = Path(__file__).parent / "fixtures" / "germany_dealers"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())
URLS = {row["fixture"]: row["metadata"]["final_url"] for row in PROVENANCE}
URL = URLS["autoland-live.html"]


def fixture(name):
    return (FIXTURES / name).read_text()


def schema():
    return json.loads(
        BeautifulSoup(fixture("autoland-live.html"), "html.parser")
        .select_one('script[type="application/ld+json"]')
        .string
    )


def body(item, *, extra=None, canonical=URL, heading=None):
    nodes = [item, *(extra or [])]
    return (
        '<html><head><link rel="canonical" href="'
        + canonical
        + '"></head><body><h1>'
        + (heading or item.get("name", ""))
        + '</h1><script type="application/ld+json">'
        + json.dumps(nodes)
        + "</script></body></html>"
    )


def test_real_own_car_with_bound_seller_and_german_registration():
    page = parse_dealer(fixture("autoland-live.html"), URL)
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.source == "dealer:autoland.de"
    assert item.url == item.external_id == URL
    assert item.scope == "target" and item.price == 14990
    assert item.price_type == "advertised_price"
    assert (item.year, item.mileage, item.transmission, item.fuel) == (
        2016,
        68618,
        "manual",
        "petrol",
    )
    assert item.raw["first_registration"] == "2016-09-01"
    assert item.seller == "Autoland Niederlassung Bad Hersfeld"
    assert item.city == "Bad Hersfeld"
    assert item.raw["market_country"] == "DE"
    assert item.raw["country_evidence"]["addressCountry"] == ["DE"]
    assert "ISOFIX" in item.description and len(item.images) == 1
    assert item.kenteken is None
    assert item.vin is None  # 17 hex digits in publisher schema do not prove VIN.
    assert item.raw["own_identifiers"]["vehicleIdentificationNumber"] == "59AB43C8261963637"
    assert item.raw["vin_warning"] == "hex_schema_identifier_unverified"
    assert page.search_filters["coverage_status"] == "detail_only"


def test_real_second_dealer_target_uses_own_model_not_recommendation_text():
    name = "autoland-manual.html"
    item = parse_dealer(fixture(name), URLS[name]).listings[0]
    assert item.scope == "target"
    assert (item.price, item.mileage, item.year) == (14490, 57864, 2016)
    assert item.raw["model_family"] == "218 Active Tourer"
    assert item.city == "Leipzig"


@pytest.mark.parametrize(
    "name",
    [
        "autoland.html",
        "stuttgart.html",
        "kuehnert-manual.html",
        "fritze.html",
        "bmw-faba-detail.html",
    ],
)
def test_real_catalogue_missing_country_missing_vehicle_or_wrong_canonical_is_rejected(name):
    page = parse_dealer(fixture(name), URLS[name])
    assert not page.listings
    assert page.search_filters["coverage_status"] == "unverified"


@pytest.mark.parametrize("country", [None, "NL", "AT", "CH", "de-DE"])
def test_country_is_bound_to_own_seller_not_global_dealer_or_language(country):
    item = schema()
    item["offers"]["seller"]["address"]["addressCountry"] = country
    unrelated = {"@type": "AutoDealer", "address": {"addressCountry": "DE"}}
    assert not parse_dealer(body(item, extra=[unrelated]), URL).listings


def test_conflicting_own_seller_countries_fail_closed():
    item = schema()
    other = json.loads(json.dumps(item["offers"]))
    other["seller"]["address"]["addressCountry"] = "NL"
    item["offers"] = [item["offers"], other]
    assert not parse_dealer(body(item), URL).listings


@pytest.mark.parametrize("field", ["url", "offer_url", "canonical", "heading", "id"])
def test_own_detail_binding_cannot_move_to_other_vehicle(field):
    item = schema()
    other_url = URL.replace("122718", "122719")
    kwargs = {}
    if field == "url":
        item["url"] = other_url
    elif field == "offer_url":
        item["offers"]["url"] = other_url
    elif field == "canonical":
        kwargs["canonical"] = other_url
    elif field == "heading":
        kwargs["heading"] = "BMW 218d Gran Tourer"
    else:
        item["@id"] = other_url + "#product"
    assert not parse_dealer(body(item, **kwargs), URL).listings


def test_same_page_recommendation_list_never_becomes_own_vehicle():
    item = schema()
    catalogue = {"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "item": item}]}
    assert not parse_dealer(body(catalogue, heading=item["name"]), URL).listings


def test_unrelated_recommendation_cannot_supply_any_own_fact():
    item = schema()
    other = schema()
    other.update(
        url=URL.replace("122718", "122719"),
        name="OTHER CAR",
        description="RECOMMENDATION",
        vehicleIdentificationNumber="WBA2A11070V123456",
        image="https://example.com/other.jpg",
    )
    other["offers"]["price"] = 99
    parsed = parse_dealer(body(item, extra=[other]), URL).listings[0]
    assert parsed.price == 14990 and parsed.vin is None
    assert "RECOMMENDATION" not in parsed.description
    assert "https://example.com/other.jpg" not in parsed.images


def test_different_own_products_with_same_canonical_are_ambiguous():
    item = schema()
    other = schema()
    other["offers"]["price"] = 99
    assert not parse_dealer(body(item, extra=[other]), URL).listings


def test_reference_graph_main_entity_and_seller_address_stay_linked():
    item = schema()
    seller = item["offers"]["seller"]
    seller["@id"] = URL + "#seller"
    item["@type"] = ["Product", "Vehicle"]
    item["@id"] = URL + "#product"
    item["offers"]["seller"] = {"@id": seller["@id"]}
    webpage = {"@type": "WebPage", "url": URL, "mainEntity": {"@id": item["@id"]}}
    document = {"@context": "https://schema.org", "@graph": [webpage, item, seller]}
    assert parse_dealer(body(document, heading=item["name"]), URL).listings[0].price == 14990
    seller["address"]["addressCountry"] = "NL"
    assert not parse_dealer(body(document, heading=item["name"]), URL).listings


@pytest.mark.parametrize(
    "vin,expected",
    [
        ("WBA2A11070V123456", "WBA2A11070V123456"),
        ("V123456", None),
        ("WBA2A11070I123456", None),
        ("AAAAAAAAAAAAAAAAA", None),
    ],
)
def test_full_own_vin_only_and_german_plate_is_never_kenteken(vin, expected):
    item = schema()
    item["vehicleIdentificationNumber"] = vin
    item["licensePlate"] = "HEF-AB 123"
    result = parse_dealer(body(item), URL).listings[0]
    assert result.vin == expected and result.kenteken is None


def test_two_full_own_vins_conflict():
    item = schema()
    item["vehicleIdentificationNumber"] = "WBA2A11070V123456"
    item["vin"] = "WBA2A11070V123457"
    assert not parse_dealer(body(item), URL).listings


@pytest.mark.parametrize(
    "kind,expected", [("Leasing", "monthly_price"), ("Nettopreis", "net_price")]
)
def test_payment_kind_is_not_blended_with_purchase_price(kind, expected):
    item = schema()
    item["offers"]["name"] = kind
    result = parse_dealer(body(item), URL).listings[0]
    assert result.price_type == expected
    if expected == "monthly_price":
        assert result.price is None


def test_mixed_purchase_and_leasing_uses_own_full_cash_and_retains_both():
    item = schema()
    lease = json.loads(json.dumps(item["offers"]))
    lease.update(name="Leasing", price=299)
    item["offers"] = [item["offers"], lease]
    result = parse_dealer(body(item), URL).listings[0]
    assert result.price == 14990 and len(result.raw["price_components"]) == 2


def test_explicit_own_mobile_link_only_not_footer_or_recommendation():
    item = schema()
    link = "https://suchen.mobile.de/auto-inserat/bmw-218i-active-tourer/453531393.html"
    item["sameAs"] = link
    result = parse_dealer(body(item), URL).listings[0]
    assert result.raw["publication_reference"] == {
        "source": "mobile_de",
        "url": link,
        "external_id": "453531393",
        "kind": "explicit_own_publication_link",
    }
    del item["sameAs"]
    assert (
        "publication_reference"
        not in parse_dealer(body(item) + '<footer><a href="' + link + '">Mobile</a></footer>', URL)
        .listings[0]
        .raw
    )


def test_current_core_three_argument_call_is_compatible_without_core_edits():
    page = parse_dealer("dealer:autoland.de", fixture("autoland-live.html"), URL)
    assert page.listings[0].source == "dealer:autoland.de"
    assert not parse_dealer("dealer:other.de", fixture("autoland-live.html"), URL).listings


def test_duplicate_canonical_and_missing_head_are_rejected():
    text = body(schema())
    duplicate = text.replace("</head>", '<link rel="canonical" href="' + URL + '"></head>')
    assert not parse_dealer(duplicate, URL).listings
    assert not parse_dealer(text.replace("<head>", "").replace("</head>", ""), URL).listings


@pytest.mark.parametrize(
    "registration,expected",
    [
        ("25.08.2017", "2017-08-25"),
        ("03.2021", "2021-03"),
        ("3/2021", "2021-03"),
        ("31.02.2021", None),
    ],
)
def test_german_registration_date_and_manuell_are_normalized(registration, expected):
    item = schema()
    item["dateVehicleFirstRegistered"] = registration
    item["vehicleTransmission"] = "Manuell"
    result = parse_dealer(body(item), URL).listings[0]
    assert result.raw["first_registration"] == expected
    assert result.transmission == "manual"


def test_without_kilometre_unit_mileage_is_unknown():
    item = schema()
    item["mileageFromOdometer"]["unitCode"] = "SMI"
    assert parse_dealer(body(item), URL).listings[0].mileage is None


def test_native_autoland_stock_is_bound_in_own_inventory_and_detail():
    other = Path(__file__).parent / "fixtures" / "germany_dealer_inventory"
    name = "autoland-detail-100787.html"
    manifest = json.loads((other / "manifest.json").read_text())
    url = next(x["metadata"]["final_url"] for x in manifest if x["fixture"] == name)
    detail = (other / name).read_text()
    item = parse_dealer(detail, url).listings[0]
    assert item.raw["dealer_stock_id"] == "100787"
    assert item.raw["identity_evidence"]["kind"] == "own_detail_native_id_and_uri"
    assert not parse_dealer(
        detail.replace('data-article-id="100787"', 'data-article-id="100788"'), url
    ).listings
