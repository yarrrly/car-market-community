"""Offline acceptance from independent own AS24 DE responses, 2026-10-07."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from car_market.germany_autoscout import (
    autoscout_de_page_url,
    autoscout_de_search_urls,
    parse_autoscout_de,
)

FIXTURES = Path(__file__).parent / "fixtures" / "germany_autoscout"
MANIFEST = {
    x["fixture"]: x for x in json.loads((FIXTURES / "manifest.json").read_text())
}
MANUAL = "de-target-2e5a3510-4132-41b9-91b8-dec32375c254.html"
AUTO = "de-target-c7564ef4-c21e-4f86-9007-d35234728a54.html"
MANUAL_ID = "2e5a3510-4132-41b9-91b8-dec32375c254"


def retained(name):
    return parse_autoscout_de(
        (FIXTURES / name).read_text(), MANIFEST[name]["final_url"]
    )


def document(name):
    text = (FIXTURES / name).read_text()
    if name.endswith(".html"):
        text = re.search(r"<script[^>]*>(.*?)</script>", text, re.DOTALL)[1]
    return json.loads(text)


def own_document():
    doc = document(MANUAL)
    return doc, doc["props"]["pageProps"]["listingDetails"]


def render(doc):
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(doc)
        + "</script>"
    )


def parsed_own(doc, url=None):
    return parse_autoscout_de(render(doc), url or MANIFEST[MANUAL]["final_url"])


def test_fixtures_are_bound_by_hash():
    assert len(MANIFEST) == 16
    for index, (name, origin) in enumerate(MANIFEST.items()):
        assert origin["http_status"] == 200
        assert origin["fetched_at"].startswith("2026-10-07")
        assert (
            hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
            == origin["fixture_sha256"]
        )


def test_real_de_search_uses_own_rows_and_independent_counters():
    page = retained("de-search.html")
    assert (page.total, page.pages, page.page, len(page.listings)) == (2429, 122, 1, 20)
    assert page.search_filters["country"] == "DE"
    assert page.search_filters["cy"] == "D"
    assert page.search_filters["build_id"] == "as24-search-funnel_main-20261007192840"
    assert all(
        x.source == "autoscout24_de" and x.kenteken is None for x in page.listings
    )
    manual = next(x for x in page.listings if x.external_id == MANUAL_ID)
    assert (
        manual.price,
        manual.mileage,
        manual.year,
        manual.transmission,
        manual.fuel,
        manual.scope,
    ) == (16999, 11240, 2020, "manual", "petrol", "target")
    diesel = next(
        x
        for x in page.listings
        if x.external_id == "254967cf-ccf7-4b0e-a652-4f8caaf8e522"
    )
    assert diesel.scope == "excluded"
    assert diesel.price_type == "conditional_price"
    gran = next(
        x
        for x in page.listings
        if x.external_id == "3ecd2ec7-4293-45ba-bae3-5efabcae0f7d"
    )
    assert gran.scope == "excluded"


def test_real_search_html_and_next_json_page_two_agree_with_stable_filters():
    html_page = retained("de-page2.html")
    next_page = retained("de-next2.json")
    assert (next_page.total, next_page.pages, next_page.page) == (2429, 122, 2)
    assert {x.external_id for x in html_page.listings} == {
        x.external_id for x in next_page.listings
    }
    assert (
        html_page.search_filters
        == next_page.search_filters
        == retained("de-search.html").search_filters
    )
    assert not {"page", "query_id", "sort", "desc"} & next_page.search_filters.keys()


def test_international_country_view_keeps_de_own_stock():
    page = retained("com-de-search.json")
    assert (page.total, page.pages, len(page.listings)) == (2381, 120, 20)
    assert all(
        x.raw["market_country"] == "DE" and "/offers/" in x.url for x in page.listings
    )
    item = retained("com-target.html").listings[0]
    assert (item.external_id, item.price, item.scope) == (MANUAL_ID, 16999, "target")


def test_real_last_page_and_source_cap_have_distinct_meanings():
    last = retained("de-page122.json")
    assert (last.total, last.pages, last.page, len(last.listings)) == (
        2429,
        122,
        122,
        9,
    )
    empty = retained("de-page123.json")
    assert (empty.total, empty.pages, empty.page, empty.listings) == (
        2429,
        122,
        123,
        [],
    )
    capped = retained("de-group-all.json")
    assert (capped.total, capped.pages) == (5790, 200)
    assert capped.search_filters["damaged_listing"] == "include"
    assert len(retained("de-group-page200.json").listings) == 20
    assert retained("de-group-page201.json").total is None
    low, high = retained("de-group-low.json"), retained("de-group-high.json")
    assert (low.total, low.pages, high.total, high.pages) == (2469, 124, 3321, 167)
    assert low.total + high.total == capped.total
    assert low.search_filters["priceto"] == "20000"
    assert high.search_filters["pricefrom"] == "20001"


def test_page_url_preserves_country_and_split_bounds_and_stops_at_200():
    url = MANIFEST["de-group-high.json"]["url"]
    query = parse_qs(urlsplit(autoscout_de_page_url(url, 167)).query)
    assert query == {
        "cy": ["D"],
        "atype": ["C"],
        "cat": ["ma13gr100098"],
        "damaged_listing": ["include"],
        "pricefrom": ["20001"],
        "page": ["167"],
    }
    assert parse_qs(
        urlsplit(autoscout_de_page_url(MANIFEST["de-next2.json"]["url"], 3)).query
    )["page"] == ["3"]
    for page in [0, 201, True, "2"]:
        with pytest.raises(ValueError):
            autoscout_de_page_url(url, page)
    with pytest.raises(ValueError):
        autoscout_de_page_url(url.replace("cy=D", "cy=NL"), 1)


def test_broad_seeds_do_not_apply_purchase_or_variant_restrictions():
    seeds = autoscout_de_search_urls()
    assert len(seeds) == 2
    assert "cat=ma13gr100098&" in seeds[0]
    for url in seeds:
        query = parse_qs(urlsplit(url).query)
        assert query["cy"] == ["D"] and query["damaged_listing"] == ["include"]
        assert (
            not {"fregfrom", "fregto", "pricefrom", "priceto", "gear", "fuel"}
            & query.keys()
        )
        assert "va426" not in url and "mt713" not in url


@pytest.mark.parametrize(
    "name,price,gear", [(MANUAL, 16999, "manual"), (AUTO, 12979, "automatic")]
)
def test_own_detail_preserves_gross_cash_claims_photos_and_description(
    name, price, gear
):
    page = retained(name)
    assert (page.total, page.pages, len(page.listings)) == (1, 1, 1)
    item = page.listings[0]
    assert (
        item.source,
        item.price,
        item.price_type,
        item.transmission,
        item.scope,
    ) == (
        "autoscout24_de",
        price,
        "advertised_price",
        gear,
        "target",
    )
    assert item.kenteken is None and item.vin is None
    assert item.images and item.description and "<br" not in item.description
    assert item.raw["detail_complete"] is True
    assert item.raw["origin"]["country"] == {
        "value": "DE",
        "path": "location.countryCode",
    }
    assert item.raw["publication_reference"] == {
        "source": "autoscout24_de",
        "url": item.url,
        "external_id": item.external_id,
        "kind": "explicit_own_publication_link",
    }
    assert item.raw["origin"]["own_stock"]["path"] == "pageProps.listingDetails"
    if name == MANUAL:
        assert item.raw["prices"]["public"]["netPriceRaw"] == 14285
        assert item.raw["own_metadata_fields"]["service_history"] is True
        assert item.raw["own_metadata_fields"]["warranty_exists"] is True
        assert (
            item.raw["own_metadata_fields"]["dealer_stock_reference"]
            == "54-7D86731_1828"
        )
        assert item.raw["own_metadata_fields"]["equipment"]


@pytest.mark.parametrize("bad", ["NL", "AT", None, "D"])
def test_country_requires_actual_own_de_proof_in_detail_and_search(bad):
    doc, own = own_document()
    own["location"]["countryCode"] = bad
    assert parsed_own(doc).listings == []
    doc = document("de-search.html")
    doc["props"]["pageProps"]["listings"][0]["location"]["countryCode"] = bad
    assert len(parsed_own(doc, MANIFEST["de-search.html"]["url"]).listings) == 19
    assert retained("de-unknown-country.json").listings == []


@pytest.mark.parametrize(
    "change",
    [
        "uuid",
        "webpage",
        "missing_webpage",
        "missing_vehicle",
        "inactive",
        "missing_status",
        "recommended_only",
    ],
)
def test_detail_never_falls_back_to_recommendations_or_matching_nested_uuid(change):
    doc, own = own_document()
    state = doc["props"]["pageProps"]
    other = "c7564ef4-c21e-4f86-9007-d35234728a54"
    state["recommendations"] = [json.loads(json.dumps(own))]
    if change == "uuid":
        own["id"] = other
    elif change == "webpage":
        own["webPage"] = own["webPage"].replace(MANUAL_ID, other)
    elif change == "missing_webpage":
        del own["webPage"]
    elif change == "missing_vehicle":
        own["vehicle"] = None
    elif change == "inactive":
        own["status"] = "Deleted"
    elif change == "missing_status":
        del own["status"]
    else:
        del state["listingDetails"]
    assert parsed_own(doc).listings == []


@pytest.mark.parametrize(
    "url",
    [
        "https://www.autoscout24.nl/aanbod/" + MANUAL_ID,
        "https://www.autoscout24.de.evil.test/angebote/" + MANUAL_ID,
        "https://evil@www.autoscout24.de/angebote/" + MANUAL_ID,
        "https://www.autoscout24.de:8443/angebote/" + MANUAL_ID,
        "https://www.autoscout24.de/lst/" + MANUAL_ID,
    ],
)
def test_detail_rejects_foreign_or_ambiguous_routes(url):
    doc, _ = own_document()
    assert parsed_own(doc, url).listings == []


def test_canonical_disagreement_and_multiple_next_states_reject_detail():
    doc, _ = own_document()
    body = render(doc)
    url = MANIFEST[MANUAL]["url"]
    assert (
        parse_autoscout_de(
            body
            + '<link rel="canonical" href="https://www.autoscout24.de/angebote/c7564ef4-c21e-4f86-9007-d35234728a54">',
            url,
        ).listings
        == []
    )
    assert parse_autoscout_de(body + body, url).listings == []
    assert (
        parse_autoscout_de(
            body.replace("__NEXT_DATA__", "recommendations"), url
        ).listings
        == []
    )


@pytest.mark.parametrize(
    "vin,expected",
    [
        ("WBA2A31030V123456", "WBA2A31030V123456"),
        ("v123456", None),
        ("WBA2A31030V12345*", None),
        ("WBA2A31030I123456", None),
        ("00000000000000000", None),
    ],
)
def test_vin_only_full_own_17_and_never_german_plate(vin, expected):
    doc, own = own_document()
    own["vehicle"].update(vin=vin, licensePlate="AB123C")
    own["recommendations"] = [{"vin": "WBA2A31030V654321"}]
    item = parsed_own(doc).listings[0]
    assert item.vin == expected and item.kenteken is None


@pytest.mark.parametrize(
    "date_text,year",
    [
        ("07/2016", 2016),
        ("31.12.2015", 2015),
        ("2024-02-29", 2024),
        ("13/2016", None),
        ("31.02.2016", None),
    ],
)
def test_german_dates_are_not_day_month_guesses(date_text, year):
    doc, own = own_document()
    own["vehicle"]["firstRegistrationDateRaw"] = date_text
    assert parsed_own(doc).listings[0].year == year


@pytest.mark.parametrize(
    "label,kind",
    [
        ("€ 199 monatlich", "monthly_payment"),
        ("Leasing € 199 / Monat", "lease_price"),
        ("€ 199 netto exkl. MwSt.", "net_price"),
        ("Export € 199", "export_price"),
    ],
)
def test_non_cash_prices_keep_their_amount_and_classification(label, kind):
    doc, own = own_document()
    own["prices"] = {}
    own["price"] = {"priceRaw": 199, "priceFormatted": label}
    item = parsed_own(doc).listings[0]
    assert (item.price, item.price_type) == (199, kind)


def test_on_request_and_missing_description_do_not_invent_completion():
    doc, own = own_document()
    own["prices"]["public"]["onRequestOnly"] = True
    own["description"] = None
    item = parsed_own(doc).listings[0]
    assert (item.price, item.price_type) == (None, "price_on_request")
    assert item.raw["detail_complete"] is False
    assert item.description is None and item.scope == "target"


def test_net_only_payload_is_retained_without_inventing_gross_vat():
    doc, own = own_document()
    own["prices"] = {"public": {"netPriceRaw": 14285, "vatRate": 19}}
    own["price"] = {}
    item = parsed_own(doc).listings[0]
    assert (item.price, item.price_type) == (14285, "net_price")


@pytest.mark.parametrize("country", [["D"], {"code": "DE"}, None])
def test_malformed_search_country_does_not_raise_or_gain_de_proof(country):
    doc = document("de-search.html")
    doc["props"]["pageProps"]["pageQuery"]["cy"] = country
    assert parsed_own(doc, MANIFEST["de-search.html"]["url"]).listings == []


@pytest.mark.parametrize(
    "price,year,damaged", [(45000, "2026-01-01", True), (500, "2014-01-01", True)]
)
def test_target_scope_does_not_apply_price_year_or_damage_purchase_filters(
    price, year, damaged
):
    doc, own = own_document()
    own["prices"]["public"]["priceRaw"] = price
    own["vehicle"].update(
        firstRegistrationDateRaw=year,
        hadAccident=damaged,
        isCurrentlyDamaged=damaged,
        offerType="N",
    )
    item = parsed_own(doc).listings[0]
    assert (item.price, item.scope) == (price, "target")
    assert item.raw["vehicle"]["isCurrentlyDamaged"] is True


def test_unknown_motor_variants_are_retained_for_existing_qualifier():
    doc = document("de-search.html")
    state = doc["props"]["pageProps"]
    state["listings"] = [next(x for x in state["listings"] if x["id"] == MANUAL_ID)]
    state["listings"][0]["vehicle"].update(
        motorTypeName=None, modelVersionInput="218 Active Tourer", variant=None
    )
    item = parsed_own(doc, MANIFEST["de-search.html"]["url"]).listings[0]
    assert item.scope == "needs_confirmation"


@pytest.mark.parametrize(
    "body",
    [
        "{}",
        "null",
        "{broken",
        '<script id="__NEXT_DATA__">[]</script>',
        '<script type="application/ld+json">{"id":"' + MANUAL_ID + '"}</script>',
        "CAPTCHA",
        "Access denied",
    ],
)
def test_untyped_or_blocked_responses_never_parse(body):
    assert parse_autoscout_de(body, MANIFEST[MANUAL]["url"]).listings == []
