import hashlib
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from car_market.quality_specific_dealers import parse_specific_dealer_detail
from car_market.trovit_details import parse_trovit_detail

FIXTURES = Path(__file__).parent / "fixtures" / "final_source_details"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())


def fixture(name):
    filename = name + ".html"
    retained = (FIXTURES / filename).read_bytes()
    assert (
        hashlib.sha256(retained).hexdigest() == PROVENANCE[filename]["fixture_sha256"]
    )
    body = retained.decode()
    return body, PROVENANCE[filename]["url"]


def car_schema(soup):
    return next(
        n
        for n in soup.select('script[type="application/ld+json"]')
        if json.loads(n.string).get("@type") == "Car"
    )


@pytest.mark.parametrize(
    "name,year,km,price,kenteken,vin,city",
    [
        ("schalk", 2020, 79500, 19450, "G185XN", "WBA6S110807E67117", "Etten-Leur"),
        ("driveworks", 2015, 125277, 12495, None, "WBA2A31010V404804", "Maastricht"),
    ],
)
def test_retained_stock_details_bind_despite_homepage_schema(
    name, year, km, price, kenteken, vin, city
):
    body, url = fixture(name)
    item = parse_specific_dealer_detail(body, url).listings[0]
    assert (item.source, item.external_id, item.url) == ("dealer", url, url)
    assert (item.year, item.mileage, item.price) == (year, km, price)
    assert (item.kenteken, item.vin, item.city) == (kenteken, vin, city)
    assert (item.fuel, item.transmission, item.scope) == (
        "petrol",
        "automatic",
        "target",
    )
    assert item.raw["detail_complete"] is True
    assert item.raw["specs"]["BTW / Marge"] == "Marge"
    assert item.raw["specs"]["APK"]
    assert (
        "Options on chassis number" in item.description
        if name == "driveworks"
        else "volledige onderhoudshistorie" in item.description
    )


@pytest.mark.parametrize("name", ["schalk", "driveworks"])
@pytest.mark.parametrize(
    "mutation",
    [
        "request_stock",
        "canonical_stock",
        "canonical_host",
        "canonical_scheme",
        "schema_image",
        "schema_name",
        "schema_url",
        "missing_heading",
        "missing_specs",
        "price",
        "year",
        "mileage",
        "ambiguous_schema",
    ],
)
def test_stock_details_reject_unbound_and_conflicting_bodies(name, mutation):
    body, url = fixture(name)
    soup = BeautifulSoup(body, "html.parser")
    stock = (
        soup.select_one('link[rel="canonical"]')["href"]
        .split("/occasions-kopen/")[1]
        .split("-")[0]
    )
    script = car_schema(soup)
    car = json.loads(script.string)
    if mutation == "request_stock":
        url = url.replace(stock, "99999999")
    elif mutation == "canonical_stock":
        soup.select_one('link[rel="canonical"]')["href"] = url.replace(
            stock, "99999999"
        )
    elif mutation == "canonical_host":
        soup.select_one('link[rel="canonical"]')["href"] = url.replace(
            "www.", "unrelated."
        )
    elif mutation == "canonical_scheme":
        soup.select_one('link[rel="canonical"]')["href"] = url.replace("https:", "ftp:")
    elif mutation == "schema_image":
        car["image"] = car["image"].replace(stock, "99999999")
    elif mutation == "schema_name":
        car["name"] = "BMW 218i Active Tourer belonging to another seller"
    elif mutation == "schema_url":
        car["url"] = url.replace(stock, "99999999")
    elif mutation == "missing_heading":
        soup.select_one(".aanbod-car-dtls-title, .aanbod-top-left").decompose()
    elif mutation == "missing_specs":
        soup.select_one(".tab-popup-area .tab-popup-content").decompose()
    elif mutation == "price":
        car["offers"]["price"] += 100
    elif mutation == "year":
        car["productionDate"] = "01-01-2019"
    elif mutation == "mileage":
        car["mileageFromOdometer"]["value"] = "42"
    else:
        soup.head.append(BeautifulSoup(str(script), "html.parser"))
    script.string = json.dumps(car)
    assert parse_specific_dealer_detail(str(soup), url) is None


@pytest.mark.parametrize("name", ["schalk", "driveworks"])
def test_homepage_and_schema_only_never_authorize_stock_detail(name):
    body, url = fixture(name)
    soup = BeautifulSoup(body, "html.parser")
    home = json.loads(car_schema(soup).string)["url"]
    assert parse_specific_dealer_detail(body, home) is None
    assert parse_specific_dealer_detail(str(car_schema(soup)), url) is None
    assert parse_specific_dealer_detail(body, url.replace("www.", "unknown.")) is None


@pytest.mark.parametrize("name", ["schalk", "driveworks"])
def test_stock_related_text_plate_and_trim_never_override_own_fields(name):
    body, url = fixture(name)
    before = parse_specific_dealer_detail(body, url).listings[0]
    soup = BeautifulSoup(body, "html.parser")
    trap = '<aside><h3>BMW 218i Active Tourer Gran Tourer 218d</h3><p>Kenteken: TP-900-R</p><li>Uitvoering<strong>POISON-TRIM</strong></li><li>Prijs<strong>€ 1.000</strong></li><img src="https://example.com/wrong.jpg"><p>Full invoice history for another vehicle</p></aside>'
    soup.main.append(BeautifulSoup(trap, "html.parser"))
    after = parse_specific_dealer_detail(str(soup), url).listings[0]
    assert (
        after.title,
        after.kenteken,
        after.price,
        after.images,
        after.description,
        after.raw["specs"],
    ) == (
        before.title,
        before.kenteken,
        before.price,
        before.images,
        before.description,
        before.raw["specs"],
    )
    assert "trim" not in after.raw and "vehicleConfiguration" not in after.raw


@pytest.mark.parametrize("date", ["31-02-2020", "2020/01/17", "17", "not a date"])
def test_unsupported_stock_dates_remain_unknown(date):
    body, url = fixture("schalk")
    soup = BeautifulSoup(body, "html.parser")
    script = car_schema(soup)
    car = json.loads(script.string)
    car["productionDate"] = car["vehicleModelDate"] = date
    script.string = json.dumps(car)
    for row in soup.select(".tab-popup-area .tab-popup-content > ul > li"):
        if row.contents[0] == "Bouwjaar":
            row.strong.string = date
    item = parse_specific_dealer_detail(str(soup), url).listings[0]
    assert item.year is None


def test_real_trovit_listing_keeps_own_facts_and_truncation():
    body, url = fixture("trovit-detail")
    item = parse_trovit_detail(body, url).listings[0]
    assert (item.source, item.external_id, item.url) == (
        "trovit",
        "i1U161T1i1Oq1G",
        url,
    )
    assert (item.kenteken, item.year, item.mileage, item.transmission, item.price) == (
        "TP900R",
        2016,
        111525,
        "manual",
        10950,
    )
    assert (item.fuel, item.scope, item.city) == ("petrol", "target", "Luttenberg")
    assert item.seller is None and item.raw["upstream_portal"] == "Marktnet"
    assert item.description.endswith("...")
    assert (
        item.raw["description_truncated"] is True
        and item.raw["detail_complete"] is False
    )
    assert item.raw["specs"]["Inbegrepen afleverpakket"].startswith("Standaard:")
    assert item.raw["specs"]["APK"] == "gekeurd tot sep. 2027"
    assert len(item.images) == 1


@pytest.mark.parametrize("name", ["schalk", "driveworks"])
def test_missing_stock_description_cannot_claim_complete_seller_history(name):
    body, url = fixture(name)
    soup = BeautifulSoup(body, "html.parser")
    soup.select_one(".dtl-tab-sec #tab3").decompose()
    item = parse_specific_dealer_detail(str(soup), url).listings[0]
    assert item.description is None
    assert item.raw["description_source"] is None
    assert item.raw["detail_complete"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "request_id",
        "body_id",
        "hero_id",
        "missing_own",
        "duplicate_own",
        "duplicate_price",
        "script_id",
        "conflicting_plate",
    ],
)
def test_trovit_rejects_wrong_or_ambiguous_own_listing(mutation):
    body, url = fixture("trovit-detail")
    soup = BeautifulSoup(body, "html.parser")
    own_id = soup.body["data-id"]
    if mutation == "request_id":
        url = url.replace(own_id, "wrongID")
    elif mutation == "body_id":
        soup.body["data-id"] = "wrongID"
    elif mutation == "hero_id":
        image = soup.select_one('[data-test="hero-image"] > img')
        image["src"] = image["src"].replace(own_id, "wrongID")
    elif mutation == "missing_own":
        soup.select_one(".ap_traffic").decompose()
    elif mutation == "duplicate_own":
        soup.select_one("#content_main").append(
            BeautifulSoup(str(soup.select_one(".ap_traffic")), "html.parser")
        )
    elif mutation == "duplicate_price":
        soup.select_one("#info > .price").append(
            BeautifulSoup('<span class="amount">€ 1.000</span>', "html.parser")
        )
    elif mutation == "script_id":
        soup.body.append(
            BeautifulSoup('<script>trovit.adId = "wrongID";</script>', "html.parser")
        )
    else:
        soup.select_one("#description > p").append(
            BeautifulSoup("<br>Kenteken: G-185-XN", "html.parser")
        )
    assert parse_trovit_detail(str(soup), url) is None


def test_trovit_never_guesses_registration_from_recommendations_or_free_text():
    body, url = fixture("trovit-detail")
    body = body.replace("Kenteken: TP-900-R", "Seller ref: TP-900-R")
    soup = BeautifulSoup(body, "html.parser")
    soup.body.append(
        BeautifulSoup(
            '<aside id="recommendations"><p>Kenteken: G-185-XN; Bouwjaar: 2020; Transmissie: Automaat</p><span class="amount">€ 1.000</span></aside>',
            "html.parser",
        )
    )
    item = parse_trovit_detail(str(soup), url).listings[0]
    assert item.kenteken is None
    assert (item.year, item.transmission, item.price) == (2016, "manual", 10950)
    assert "G-185-XN" not in item.description


def test_trovit_home_search_and_unknown_body_fail_closed():
    body, url = fixture("trovit-detail")
    for unsupported in (
        "https://autos.trovit.nl/",
        "https://autos.trovit.nl/bmw-218i",
        url.replace("autos.trovit.nl", "example.com"),
    ):
        assert parse_trovit_detail(body, unsupported) is None
    assert (
        parse_trovit_detail(
            "<h1>BMW 218i Active Tourer</h1><p>Kenteken: TP-900-R</p>", url
        )
        is None
    )
