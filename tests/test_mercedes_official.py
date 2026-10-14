"""Byte-exact own captures and mutations of their actual fields, no made-up stock."""

import copy
import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from car_market.mercedes_official import (
    DE_HOST,
    MAX_BODY,
    mercedes_official_page_request,
    mercedes_official_publication_id,
    parse_mercedes_official,
)
from car_market.model_profiles import MERCEDES_PROFILE, use_profile

FIXTURES = Path(__file__).parent / "fixtures" / "mercedes_official"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


def capture(name):
    meta = MANIFEST["fixtures"][name]
    return gzip.decompress((FIXTURES / meta["fixture"]).read_bytes()).decode(), meta[
        "url"
    ]


def parse(name, body=None, url=None, country=None):
    actual_body, actual_url = capture(name)
    url = actual_url if url is None else url
    country = country or ("DE" if urlsplit(url).hostname == DE_HOST else "NL")
    with use_profile(MERCEDES_PROFILE, country):
        return parse_mercedes_official(actual_body if body is None else body, url)


@pytest.mark.parametrize("name", list(MANIFEST["fixtures"]))
def test_fixture_bytes_match_the_manifest(name):
    meta = MANIFEST["fixtures"][name]
    raw = gzip.decompress((FIXTURES / meta["fixture"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == meta["fixture_sha256"]
    archive = (FIXTURES / meta["fixture"]).read_bytes()
    assert hashlib.sha256(archive).hexdigest() == meta["fixture_archive_sha256"]
    assert meta["captured_at"].startswith("2026-10-08T")
    assert meta["method"] in {"GET", "POST"}


def test_de_four_native_pages_cover_own_broad_control():
    names = [
        "de-verified-broad-page1",
        "de-verified-broad-page2-post",
        "de-native-broad-page3",
        "de-native-broad-page4",
    ]
    pages = [parse(name) for name in names]
    assert [p.page for p in pages] == [1, 2, 3, 4]
    assert [len(p.listings) for p in pages] == [12, 12, 12, 5]
    assert all(p.total == 41 and p.pages == 4 for p in pages)
    urls = {x.url for page in pages for x in page.listings}
    assert len(urls) == 41
    assert all(
        x.raw.get("market_country") is None for page in pages for x in page.listings
    )
    assert all(x.raw["generation"] is None for page in pages for x in page.listings)
    assert all(x.transmission is None for page in pages for x in page.listings)
    assert all(x.scope != "target" for page in pages for x in page.listings)


def test_de_exact_request_does_not_supply_country_gearbox_or_generation():
    page = parse("de-verified-target-page1")
    assert page.total == 4 and page.pages == 1
    for item in page.listings:
        assert item.raw["body_type"] == "Coupé/Sportwagen"
        assert item.raw.get("market_country") is None
        assert item.raw["model_generation"] is None
        assert item.transmission is None
        assert item.scope == "needs_confirmation"


@pytest.mark.parametrize(
    "name",
    [
        "de-target-detail1",
        "de-target-detail2",
        "de-target-detail3",
        "de-target-detail4",
    ],
)
def test_de_details_retain_own_specifications_and_bound_dealer_proof(name):
    page = parse(name)
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.raw["body_type"] == "Coupé/Sportwagen"
    assert item.raw["market_country"] == "DE"
    assert item.raw["dealer_address"]
    assert item.raw["native_country_evidence"]["phone"].startswith("+49")
    assert item.raw["first_registration"][:4] == str(item.year)
    assert item.transmission == "automatic" and item.fuel == "petrol"
    assert item.raw["generation"] is None
    assert item.raw["production_date"] is None
    assert item.raw["detail_complete"] is True
    assert item.raw["identity_evidence"]["canonical"] == item.url


def test_de_retains_native_model_year_separate_from_first_registration():
    item = parse("de-target-detail1").listings[0]
    assert item.year == 2016 and item.raw["first_registration"] == "2016-06-29"
    assert item.raw["model_year"] == 2019
    assert item.price == 24490
    assert "inkl. MwSt." in item.raw["price_components"]["basis_label"]


def test_de_card_link_mutation_rejects_entire_ad_binding():
    body, _ = capture("de-verified-target-page1")
    soup = BeautifulSoup(body, "html.parser")
    card = soup.select_one("[id^=resultListItem]")
    bad_id = card["id"].removeprefix("resultListItem")
    link = card.select_one('a[href*="/vehicle?"]')
    link["href"] = link["href"].replace("vehicle=" + bad_id, "vehicle=19145492")
    result = parse("de-verified-target-page1", str(soup))
    assert len(result.listings) == 3
    assert all(
        x.raw["own_identifiers"]["publication_id"] != bad_id for x in result.listings
    )


@pytest.mark.parametrize(
    "replacement",
    [
        "https://evil.test/vehicle?vehicle=19102410",
        "https://gebrauchtwagen.mercedes-benz.de/vehicle?vehicle=19065900",
    ],
)
def test_de_detail_canonical_is_bound_to_requested_ad(replacement):
    body, _ = capture("de-target-detail1")
    soup = BeautifulSoup(body, "html.parser")
    soup.select_one("link[rel=canonical]")["href"] = replacement
    assert not parse("de-target-detail1", str(soup)).listings


def test_de_contact_phone_is_required_with_five_digit_postal_claim():
    body, _ = capture("de-target-detail1")
    soup = BeautifulSoup(body, "html.parser")
    for node in soup.select('a[href^="tel:"]'):
        node["href"] = "tel:0031"
    item = parse("de-target-detail1", str(soup)).listings[0]
    assert item.raw.get("market_country") is None
    assert item.scope != "target"


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Preis netto zzgl. MwSt.", "net_only"),
        ("Monatliche Rate inkl. MwSt.", "monthly_or_conditional"),
        ("Preis ab / nur bei Inzahlungnahme", "conditional_price"),
    ],
)
def test_de_price_basis_mutations_never_promote_net_monthly_or_conditional(
    label, expected
):
    body, _ = capture("de-target-detail1")
    soup = BeautifulSoup(body, "html.parser")
    soup.select_one(".vehicle-price-label-vehicle").string = label
    item = parse("de-target-detail1", str(soup)).listings[0]
    assert item.price is None
    assert item.price_type == expected
    assert item.raw["price_components"]["original_price_text"].startswith("24.490")


def test_native_post_refusal_is_not_empty_inventory():
    page = parse("de-verified-broad-page3-post")
    assert not page.listings and page.total is None and page.pages is None
    assert page.search_filters["parse_status"] in {
        "native_request_rejected_or_not_inventory",
        "not_inventory",
    }


def test_native_post_page_conflict_rejected():
    body, _ = capture("de-verified-broad-page2-post")
    doc = json.loads(body)
    doc["data"]["page"] = 3
    page = parse("de-verified-broad-page2-post", json.dumps(doc))
    assert page.total is None and not page.listings


def test_hedin_inventory_country_and_registration_are_own_native_fields():
    page = parse("hedin-native-UI-1")
    assert page.total == 1 and page.pages == 1 and page.page == 1
    item = page.listings[0]
    assert item.price == 20900 and item.raw["price_basis"] == "unspecified"
    assert item.raw["price_currency"] is None
    assert item.raw["price_components"]["original_retail"]["includingVat"] is False
    assert item.raw["price_components"]["native_prices"]["finance"]["value"] == 254
    assert item.raw["market_country"] == "NL"
    assert item.year == 2017 and item.raw["first_registration"] == "2017-11-27"
    assert item.raw["production_year"] == 2017
    assert item.raw["production_year_field"] == "attributes.bouwjaar"
    assert item.raw["generation"] is None
    assert item.raw["body_type"] == "Coupé"
    assert item.raw["own_identifiers"]["publication_id"] == "84100937"


def test_hedin_schema_and_widget_detail_bind_id_and_preserve_vin():
    item = parse("hedin-target-detail").listings[0]
    assert item.vin == "WDD2053401F675905"
    assert item.kenteken == "XD655L"
    assert item.raw["production_date"] == "2017"
    assert item.raw["model_year"] == 2017
    assert item.raw["first_registration"] is None and item.year is None
    assert item.raw["body_type"] == "Coupé"
    assert item.raw["number_of_doors"] == 2
    assert item.raw["market_country"] == "NL"
    assert "3902HR" in item.raw["dealer_address"]
    assert item.price == 20900


@pytest.mark.parametrize(
    "country,expected", [("be", "BE"), ("de", "DE"), ("", None), ("unknown", None)]
)
def test_hedin_country_cannot_be_supplied_by_nl_domain_or_query(country, expected):
    body, _ = capture("hedin-c-coupe")
    doc = json.loads(body)
    doc["items"][0]["attributes"]["land"]["value"] = country
    item = parse("hedin-c-coupe", json.dumps(doc)).listings[0]
    assert item.raw.get("market_country") == expected
    assert item.scope != "target"


@pytest.mark.parametrize("path", ["url", "id", "slug", "voertuignr_hexon"])
def test_hedin_native_ad_identity_mutations_fail_closed(path):
    body, _ = capture("hedin-c-coupe")
    doc = json.loads(body)
    row = doc["items"][0]
    if path == "voertuignr_hexon":
        row["attributes"][path]["value"] = "25594519"
    else:
        row[path] = "invalid"
    assert not parse("hedin-c-coupe", json.dumps(doc)).listings


def test_hedin_native_gross_and_original_finance_are_distinct():
    item = parse("hedin-native").listings[0]
    assert item.price == 86090
    assert item.raw["price_basis"] == "gross"
    assert item.raw["price_currency"] == "EUR"
    prices = item.raw["price_components"]["native_prices"]
    assert prices["sale"]["includingVat"] is True
    assert prices["finance"]["value"] == 961
    assert item.price != prices["finance"]["value"]


@pytest.mark.parametrize("mutation", ["net", "monthly_only", "non_eur", "zero", "nan"])
def test_hedin_price_mutations_keep_original_components_without_cash_fallback(mutation):
    body, _ = capture("hedin-native")
    doc = json.loads(body)
    prices = doc["items"][0]["prices"]
    if mutation == "net":
        prices["sale"]["includingVat"] = False
    elif mutation == "monthly_only":
        prices.pop("sale")
    elif mutation == "non_eur":
        prices["sale"]["currency"] = "USD"
    elif mutation == "zero":
        prices["sale"]["value"] = 0
    else:
        prices["sale"]["value"] = "NaN"
    item = parse("hedin-native", json.dumps(doc)).listings[0]
    assert item.price is None
    assert item.raw["price_components"]["native_prices"] == prices
    assert item.raw["price_components"]["original_retail"]["value"] == 86090


def test_hedin_detail_related_sku_or_price_cannot_be_own():
    body, _ = capture("hedin-target-detail")
    soup = BeautifulSoup(body, "html.parser")
    node = soup.select_one("#vehicle-intro .compare-toggle[data-vehicle]")
    payload = json.loads(node["data-vehicle"])
    payload["id"] = "32ef76e3-82d0-4b05-a68d-b81063aee53b"
    node["data-vehicle"] = json.dumps(payload)
    assert not parse("hedin-target-detail", str(soup)).listings


def test_no_generation_or_first_registration_from_modern_query_or_equipment():
    body, _ = capture("hedin-target-detail")
    soup = BeautifulSoup(body, "html.parser")
    node = soup.select_one('script[type="application/ld+json"]')
    schema = json.loads(node.text)
    schema.pop("vehicleIdentificationNumber")
    schema.pop("productionDate")
    schema.pop("vehicleModelDate")
    node.string = json.dumps(schema)
    item = parse("hedin-target-detail", str(soup)).listings[0]
    assert item.raw["generation"] is None and item.raw["production_year"] is None
    assert item.year is None
    assert item.scope == "needs_confirmation"


@pytest.mark.parametrize(
    "field,value",
    [
        ("carrosserie", "Sedan"),
        ("brandstof", "Diesel"),
        ("transmissie", "Handgeschakeld"),
        ("datum_deel_1", "2015-11-27T00:00:00"),
    ],
)
def test_native_model_scope_conflicts_are_retained_for_profile_exclusion(field, value):
    body, _ = capture("hedin-c-coupe")
    doc = json.loads(body)
    doc["items"][0]["attributes"][field]["value"] = value
    item = parse("hedin-c-coupe", json.dumps(doc)).listings[0]
    assert item.scope == "excluded"
    assert item.raw["native_vehicle"]["attributes"][field]["value"] == value


def test_mossel_broad_controls_come_from_own_ssr_and_exact_ui_links():
    page = parse("nl-mossel-browser")
    assert page.total == 2 and len(page.listings) == 2
    assert {x.raw["own_identifiers"]["publication_id"] for x in page.listings} == {
        "326253586-1",
        "482267150-1",
    }
    assert [x.price for x in page.listings] == [26900, 57700]
    assert all(x.price_type == "basis_unknown" for x in page.listings)
    assert all(
        x.raw["market_country"] == "NL" and x.scope != "target" for x in page.listings
    )
    assert all(x.raw["generation"] is None for x in page.listings)
    assert page.search_filters["paging_certified"] is False


def test_mossel_ssr_native_country_missing_does_not_inherit_country_filter():
    body, _ = capture("nl-mossel-browser")
    soup = BeautifulSoup(body, "html.parser")
    node = soup.select_one("#__NUXT_DATA__")
    doc = json.loads(node.text)
    advertiser = next(
        x
        for x in doc
        if isinstance(x, dict) and "advertiserText" in x and "country" in x
    )
    doc[advertiser["country"]] = ""
    node.string = json.dumps(doc)
    assert all(
        x.raw.get("market_country") is None
        for x in parse("nl-mossel-browser", str(soup)).listings
    )


def test_mossel_request_filters_must_match_own_ssr_route():
    body, url = capture("nl-mossel-browser")
    assert not parse(
        "nl-mossel-browser", body, url.replace("country=nl", "country=de")
    ).listings


def test_mossel_missing_own_ui_link_rejects_corresponding_record():
    body, _ = capture("nl-mossel-browser")
    soup = BeautifulSoup(body, "html.parser")
    for node in soup.select('a[href*="326253586-1"]'):
        node["href"] = "/mercedes-benz/voorraad/99999999-1-unrelated"
    page = parse("nl-mossel-browser", str(soup))
    assert len(page.listings) == 1
    assert page.listings[0].raw["own_identifiers"]["publication_id"] == "482267150-1"


def test_national_nl_native_broad_and_exact_zero_are_portal_bound():
    for name in ["nl-native-broad-page1", "nl-native-exact-page1"]:
        page = parse(name)
        assert page.total == 0 and not page.listings
        assert page.search_filters["portal_scope"] == "Certified"
        assert page.search_filters["coverage_complete"] is False
        assert page.search_filters["paging_certified"] is False
    assert parse("nl-certified-cars").total is None


def test_national_nl_sedan_detail_has_own_registration_and_postal_proof():
    item = parse("nl-national-sedan-detail").listings[0]
    assert item.raw["first_registration"] == "2024-02-07"
    assert item.raw["number_of_doors"] == 4
    assert item.raw["market_country"] == "NL"
    assert item.kenteken == "KTG81G"
    assert item.price == 41950
    assert item.raw["generation"] is None
    assert item.scope != "target"


def test_national_nl_wrong_ad_request_is_not_related_detail():
    body, url = capture("nl-national-sedan-detail")
    assert not parse(
        "nl-national-sedan-detail", body, url.replace("22022571", "22022903")
    ).listings


@pytest.mark.parametrize(
    "url",
    [
        "http://gebrauchtwagen.mercedes-benz.de/vehicles",
        "https://gebrauchtwagen.mercedes-benz.de.evil.test/vehicles",
        "https://user@gebrauchtwagen.mercedes-benz.de/vehicles",
        "https://gebrauchtwagen.mercedes-benz.de:444/vehicles",
        "https://gebrauchtwagen.mercedes-benz.de/vehicles#login",
        "https://gebrauchtwagen.mercedes-benz.de/login",
        "https://www.hedinautomotive.nl/voorraad-api/vehiclelist/88/vehicles.json?webspaceKey=bmw&locale=nl&brand[]=BMW",
        "https://www.vanmossel.nl/mercedes-benz/voorraad?token=secret",
    ],
)
def test_safe_request_guard_and_parser_reject_uncaptured_routes(url):
    with pytest.raises(ValueError):
        mercedes_official_page_request(url, 1)
    assert parse_mercedes_official("{}", url).total is None


@pytest.mark.parametrize("page", [0, -1, True, 1.5, 201, "2"])
def test_helper_has_bounded_typed_paging(page):
    _, url = capture("de-verified-target-page1")
    with pytest.raises(ValueError):
        mercedes_official_page_request(url, page)


def test_get_helper_preserves_captured_filters_and_has_no_price_or_state_cap():
    _, url = capture("de-verified-target-page1")
    request = mercedes_official_page_request(url, 2)
    q = parse_qs(urlsplit(request["url"]).query)
    assert q["modtyp"] == ["c180"] and q["bbodty"] == ["cou"]
    assert (
        q["gearty"] == ["2"] and q["firrey[min]"] == ["2016"] and q["enginp"] == ["1"]
    )
    assert q["page"] == ["2"]
    assert "ofprgr[min]" not in q and "cpoveh" not in q


def test_multipart_helper_uses_only_captured_native_form_fields():
    meta = MANIFEST["fixtures"]["de-verified-broad-page2-post"]
    fields = dict(
        re.findall(r'name="([^"]+)"\r\n\r\n(.*?)\r\n', meta["request_body"], re.DOTALL)
    )
    old = copy.deepcopy(fields)
    request = mercedes_official_page_request(meta["url"], 3, fields)
    assert request["encoding"] == "multipart/form-data"
    assert request["method"] == "POST"
    assert request["form_data"] == {**old, "page": "3"}
    assert fields == old
    with pytest.raises(ValueError):
        mercedes_official_page_request(
            meta["url"], 3, {**fields, "Authorization": "not_allowed"}
        )


def test_no_invented_paging_contract_for_nl_branches():
    for name in ["nl-mossel-browser", "nl-native-exact-page1"]:
        _, url = capture(name)
        with pytest.raises(ValueError, match="not_captured"):
            mercedes_official_page_request(url, 2)


def test_hedin_helper_retains_observed_duplicate_brand_and_purchase_modes():
    _, url = capture("hedin-native-UI-1")
    request = mercedes_official_page_request(url, 2)
    q = parse_qs(urlsplit(request["url"]).query)
    assert q["brand[]"] == ["Mercedes-Benz", "Mercedes-Benz"]
    assert q["aanschafvormen[]"] == ["buy"]
    assert q["page"] == ["2"]


@pytest.mark.parametrize("suffix", ["&vehicle=19065900", "#related"])
def test_de_publication_identity_rejects_ambiguous_query_or_fragment(suffix):
    assert (
        mercedes_official_publication_id(
            "https://" + DE_HOST + "/vehicle?vehicle=19102410" + suffix
        )
        is None
    )


def test_body_boundary_is_fail_closed():
    assert (
        parse_mercedes_official(b"\xff", "https://" + DE_HOST + "/vehicles").total
        is None
    )
    assert (
        parse_mercedes_official(
            "x" * (MAX_BODY + 1), "https://" + DE_HOST + "/vehicles"
        ).total
        is None
    )


def test_actual_de_http_page2_matches_native_browser_post_identity_set():
    get = parse("de-native-broad-page2-get")
    post = parse("de-verified-broad-page2-post")
    assert get.total == post.total == 41
    assert get.page == post.page == 2
    assert get.pages == post.pages == 4
    assert {x.url for x in get.listings} == {x.url for x in post.listings}


def test_actual_hedin_second_page_is_own_distinct_inventory():
    first, second = parse("hedin-native"), parse("hedin-native-page2")
    assert first.total == second.total == 770
    assert first.pages == second.pages == 35
    assert first.page == 1 and second.page == 2
    assert len(second.listings) == 22
    assert not {x.url for x in first.listings} & {x.url for x in second.listings}
    assert second.search_filters["coverage_complete"] is False


def test_hedin_conflicting_duplicates_are_not_arbitrarily_chosen():
    body, _ = capture("hedin-c-coupe")
    doc = json.loads(body)
    duplicate = copy.deepcopy(doc["items"][0])
    duplicate["attributes"]["land"]["value"] = "be"
    doc["items"].append(duplicate)
    page = parse("hedin-c-coupe", json.dumps(doc))
    assert not page.listings
    assert page.search_filters["rejected_records"] == 2


@pytest.mark.parametrize(
    "name", ["de-verified-broad-page2-post", "de-verified-broad-page3-post"]
)
def test_http_multipart_getter_detects_actual_stock_or_noninventory_reply(
    monkeypatch, name
):
    from car_market.mercedes_official import fetch_mercedes_official

    meta = MANIFEST["fixtures"]["de-verified-broad-page2-post"]
    fields = dict(
        re.findall(r'name="([^"]+)"\r\n\r\n(.*?)\r\n', meta["request_body"], re.DOTALL)
    )
    body, _ = capture(name)
    observed = []

    class NativeReply:
        url, status = meta["url"], 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, bound):
            return body.encode()[:bound]

    class Opener:
        def open(self, request, timeout):
            observed.append(request)
            return NativeReply()

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    result = fetch_mercedes_official(meta["url"], fields)
    assert result.status == 200
    assert observed[0].get_method() == "POST"
    sent = dict(
        re.findall(
            r'name="([^"]+)"\r\n\r\n(.*?)\r\n', observed[0].data.decode(), re.DOTALL
        )
    )
    assert sent == fields
    assert (
        "Authorization" not in observed[0].headers
        and "Cookie" not in observed[0].headers
    )
    if name == "de-verified-broad-page2-post":
        assert result.error is None
        assert parse(name, result.body).page == 2
    else:
        assert result.error == "native_response_not_inventory"


def test_http_getter_access_denied_does_not_retry_or_switch_to_browser(monkeypatch):
    from urllib.error import HTTPError

    from car_market.mercedes_official import fetch_mercedes_official

    _, url = capture("nl-native-exact-page1")
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    response = fetch_mercedes_official(url)
    assert response.status == 403 and response.error == "http_403"
    assert response.body is None
    assert len(calls) == 1


def test_redirect_guard_does_not_fetch_an_unrelated_host_or_login():
    from urllib.request import Request

    from car_market.mercedes_official import _OfficialRedirect

    _, url = capture("de-verified-target-page1")
    for target in ["https://evil.test/vehicles", "https://" + DE_HOST + "/login"]:
        with pytest.raises(ValueError, match="unsafe_official_redirect"):
            _OfficialRedirect().redirect_request(
                Request(url), None, 302, "Found", {}, target
            )


@pytest.mark.parametrize(
    "name", ["hedin-browser-pre-control", "hedin-browser-pre-exact"]
)
def test_browser_getter_keeps_native_json_instead_of_chromium_pre_document(
    monkeypatch, name
):
    import sys
    import types

    from car_market.mercedes_official import _browser_official_get

    wrapper, url = capture(name)
    native_json = BeautifulSoup(wrapper, "html.parser").select_one("pre").get_text()
    native = types.SimpleNamespace(status=200, text=lambda: native_json)
    page = types.SimpleNamespace(
        url=url,
        goto=lambda *args, **kwargs: native,
        content=lambda: wrapper,
        wait_for_timeout=lambda *args: None,
    )
    browser = types.SimpleNamespace(
        new_context=lambda **kwargs: types.SimpleNamespace(new_page=lambda: page),
        close=lambda: None,
    )

    class Session:
        def __enter__(self):
            return types.SimpleNamespace(
                chromium=types.SimpleNamespace(launch=lambda **kwargs: browser)
            )

        def __exit__(self, *args):
            return None

    sync = types.ModuleType("playwright.sync_api")
    sync.sync_playwright = Session
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync)
    request = mercedes_official_page_request(url)
    response = _browser_official_get(request, 30)
    assert response.error is None
    assert response.body == native_json
    accepted = parse(name, response.body, response.final_url)
    assert accepted.total == 1 and accepted.page == 1 and accepted.pages == 1
    assert accepted.listings[0].raw["own_identifiers"]["publication_id"] == "84100937"
    assert accepted.listings[0].raw["first_registration"] == "2017-11-27"
    rejected = parse(name, wrapper, url)
    assert rejected.total is None and not rejected.listings
