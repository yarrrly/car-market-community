import json
from pathlib import Path

import pytest
from car_market.parse import parse_page

FIXTURES = Path(__file__).parent / "fixtures" / "quality_primary"


@pytest.mark.parametrize(
    "identity,price,mileage,year,transmission,registration,city",
    [
        ("m2450755184", 4950, 206491, 2015, "manual", "GX803F", "Eindhoven"),
        ("m2431176534", 23500, 97500, 2023, "automatic", None, "Amsterdam"),
        ("m2448936451", 9499, 180491, 2016, "automatic", "KX186B", "Tilburg"),
        ("m2440130736", 7350, 168205, 2015, "manual", "6ZSK83", "Dronten"),
        ("m2443040920", 10250, 178606, 2015, "automatic", "2ZLS81", "Dronten"),
    ],
)
def test_marktplaats_live_detail(identity, price, mileage, year, transmission, registration, city):
    body = (FIXTURES / (identity + ".html")).read_text()
    page = parse_page(
        "marktplaats", body, "https://www.marktplaats.nl/v/auto-s/bmw/" + identity + "-bmw"
    )
    assert len(page.listings) == 1
    ad = page.listings[0]
    assert ad.external_id == identity
    assert (ad.price, ad.mileage, ad.year) == (price, mileage, year)
    assert (ad.transmission, ad.fuel, ad.kenteken) == (transmission, "petrol", registration)
    assert ad.city == city
    assert ad.seller and ad.description and ad.images
    assert ad.price_type == ("MIN_BID" if identity == "m2431176534" else "FIXED")
    assert ad.vin is None
    assert ad.scope == "target"


def test_marktplaats_unknowns_do_not_come_from_description_or_other_vehicle():
    body = (FIXTURES / "m2448936451.html").read_text()
    prefix, state = body.split("window.__CONFIG__ = ", 1)
    payload, end = json.JSONDecoder().raw_decode(state)
    listing = payload["listing"]
    listing.pop("licensePlate")
    listing["carAttributes"] = {}
    listing["priceInfo"] = {"priceType": "SEE_DESCRIPTION"}
    body = prefix + "window.__CONFIG__ = " + json.dumps(payload) + state[end:]
    ad = parse_page(
        "marktplaats", body, "https://www.marktplaats.nl/v/auto-s/bmw/m2448936451-bmw"
    ).listings[0]
    assert (ad.price, ad.mileage, ad.year, ad.transmission, ad.fuel, ad.kenteken) == (None,) * 6
    assert ad.scope == "needs_confirmation"
    assert not parse_page(
        "marktplaats", body, "https://www.marktplaats.nl/v/auto-s/bmw/m999999999-bmw"
    ).listings


@pytest.mark.parametrize("source", ["autoscout24", "autotrader"])
def test_primary_live_details_keep_full_price_and_plate(source):
    identity = "789ada1c-ded9-465d-93ff-f0783b445b2e"
    body = (FIXTURES / (source + "-" + identity + ".html")).read_text()
    ad = parse_page(source, body, "https://www." + source + ".nl/aanbod/" + identity).listings[0]
    assert (ad.price, ad.mileage, ad.year, ad.kenteken) == (3425, 200316, 2015, "GD431V")
    assert (ad.transmission, ad.fuel, ad.scope) == ("automatic", "petrol", "target")


def test_gaspedaal_live_search_sample():
    page = parse_page(
        "gaspedaal",
        (FIXTURES / "gaspedaal-search.html").read_text(),
        "https://www.gaspedaal.nl/bmw/2-serie-active-tourer/218i",
    )
    expected = {
        "142106622": (5950, 331787, 2015, "automatic"),
        "140557409": (34450, 15890, 2025, "automatic"),
        "141221637": (6450, 193646, 2014, "manual"),
        "140052008": (7350, 168205, 2015, "manual"),
        "129271506": (7495, 235896, 2015, "automatic"),
    }
    assert len(page.listings) == 5
    for ad in page.listings:
        assert (ad.price, ad.mileage, ad.year, ad.transmission) == expected[ad.external_id]
        assert ad.fuel == "petrol"
        assert ad.scope == "target"
        assert ad.kenteken is None and ad.vin is None


@pytest.mark.parametrize("block", ["missing", "unclosed", "empty", "full"])
def test_marktplaats_detail_complete_requires_closed_full_block(block):
    body = (FIXTURES / "m2448936451.html").read_text()
    before, description = body.split('<div data-collapsable="description">', 1)
    blocks = {
        "missing": "",
        "unclosed": '<div data-collapsable="description">' + description.removesuffix("</div>"),
        "empty": '<div data-collapsable="description"></div>',
        "full": '<div data-collapsable="description">' + description,
    }
    ad = parse_page(
        "marktplaats",
        before + blocks[block],
        "https://www.marktplaats.nl/v/auto-s/bmw/m2448936451-bmw",
    ).listings[0]
    assert ad.raw["detail_complete"] is (block == "full")
    assert ad.description


@pytest.mark.parametrize("shape", ["string", "dict", "list"])
def test_marktplaats_schema_images_are_always_a_list(shape):
    body = (FIXTURES / "m2448936451.html").read_text()
    start = body.index(">") + 1
    schema, end = json.JSONDecoder().raw_decode(body[start:])
    url = schema["image"][0]
    schema["image"] = {"string": url, "dict": {"url": url}, "list": [url, {"url": url}]}[shape]
    body = body[:start] + json.dumps(schema) + body[start + end :]
    ad = parse_page(
        "marktplaats", body, "https://www.marktplaats.nl/v/auto-s/bmw/m2448936451-bmw"
    ).listings[0]
    assert ad.images == ([url, url] if shape == "list" else [url])
