import json
from pathlib import Path

import pytest
from car_market.parse import Listing, Page, dutch_listing, parse_page
from car_market.quality_api_adapters import (
    enrich_api_listing,
    major_price,
    parse_public_detail,
    validate_inventory,
)

FIXTURES = Path(__file__).parent / "fixtures" / "quality_api"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())


@pytest.mark.parametrize(
    "fixture,price,year,kenteken,seller,transmission",
    [
        ("autotrack-59767268.html", 4950, 2016, "ZS627L", "W.T.B. Automotive", "manual"),
        ("autotrack-58815003.html", 34999, 2025, "JXF21B", "Gewi Auto's", "automatic"),
        (
            "viabovag-a7d28d8a-0581-4c3c-958b-ec32e3112c63.html",
            8900,
            2014,
            "5XZR17",
            "Auto Knobben",
            "manual",
        ),
        (
            "viabovag-52fa6e90-54c3-4b25-9c2b-57061a2da815.html",
            51900,
            2026,
            "JXD90G",
            "Ekris Veenendaal",
            "automatic",
        ),
    ],
)
def test_real_detail_identity_price_registration_and_seller(
    fixture, price, year, kenteken, seller, transmission
):
    source = fixture.split("-", 1)[0]
    item = parse_public_detail(
        source, (FIXTURES / fixture).read_text(), PROVENANCE[fixture]["final_url"]
    )
    assert item is not None
    assert (item.price, item.year, item.kenteken, item.seller, item.transmission) == (
        price,
        year,
        kenteken,
        seller,
        transmission,
    )
    assert item.scope == "target"
    assert "218i" in item.title.lower()
    assert item.description and item.images and item.city
    assert item.vin is None


def test_anwb_canonical_detail_retains_advertisement_identity():
    fixture = "autotrack-59767268.html"
    item = parse_public_detail(
        "anwb", (FIXTURES / fixture).read_text(), PROVENANCE[fixture]["final_url"]
    )
    assert item.source == "anwb"
    assert item.external_id == "59767268"
    assert item.fuel == "petrol"


def test_anwb_real_media_are_not_lost():
    ad = json.loads((FIXTURES / "anwb-card.json").read_text())
    item = parse_page("anwb", json.dumps({"hits": [ad]}), "https://api.anwb.nl/").listings[0]
    fixed = enrich_api_listing(item)
    assert len(fixed.images) == 18
    assert (
        fixed.images[0]
        == "https://cdn.autotrack.nl/60151394/0-81858609f223043f464d3c223b7efa80.jpg"
    )
    assert item.images == fixed.images
    assert fixed is not item


def test_autotrack_real_media_are_not_lost():
    ad = json.loads((FIXTURES / "autotrack-card.json").read_text())
    fixed = enrich_api_listing(dutch_listing("autotrack", ad, "https://www.autotrack.nl/"))
    assert len(fixed.images) == 18


def test_autohero_minor_units_do_not_use_monthly_or_registration_timestamp():
    ad = json.loads((FIXTURES / "autohero-card.json").read_text())
    item = Listing("autohero", ad["id"], "https://www.autohero.com/", "BMW", raw=ad)
    fixed = enrich_api_listing(item)
    assert fixed.price == 24950
    assert fixed.price != ad["monthlyPayment"]["amountMinorUnits"] / 100
    assert fixed.kenteken is None


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"amountMinorUnits": 100},
        {"amountMinorUnits": 100, "conversionMajor": 0, "currency": "EUR"},
        {"amountMinorUnits": 100, "conversionMajor": 100, "currency": "USD"},
    ],
)
def test_unknown_or_invalid_price_stays_unknown(value):
    assert major_price(value) is None


def test_validation_redirect_does_not_count_as_complete_inventory():
    page = Page([], total=222973, pages=7433, page=1)
    assert validate_inventory(
        page, 3, 459, "https://www.autotrack.nl/aanbod?other.toast=Validation"
    ) == ["filter_validation_redirect", "page_number_mismatch", "counter_changed"]


def test_canonical_detail_does_not_use_a_different_car():
    fixture = "autotrack-59767268.html"
    assert (
        parse_public_detail(
            "autotrack", (FIXTURES / fixture).read_text(), "https://www.autotrack.nl/a/bmw-123"
        )
        is None
    )


@pytest.mark.parametrize(
    "fixture",
    [
        "autotrack-59767268.html",
        "viabovag-52fa6e90-54c3-4b25-9c2b-57061a2da815.html",
    ],
)
def test_detail_complete_requires_explicitly_closed_seller_comments(fixture):
    source = fixture.split("-", 1)[0]
    body = (FIXTURES / fixture).read_text()
    url = PROVENANCE[fixture]["final_url"]
    item = parse_public_detail(source, body, url)
    assert item.raw["detail_complete"] is True

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body, "html.parser")
    if source == "viabovag":
        comments = soup.select_one('[data-testid="vdp-dealer-comments-mobile"]')
    else:
        comments = soup.find("h3", string="Wat vertelt de aanbieder over deze auto?").parent
    comments.decompose()
    item = parse_public_detail(source, str(soup), url)
    assert item.raw["detail_complete"] is None

    # Real excerpt with its comments cut before their closing div. The parser
    # can still read text; repaired HTML must not certify completeness.
    start = (
        body.index("Modeljaar:")
        if source == "viabovag"
        else body.index("Lees de volledige omschrijving")
        if "Lees de volledige omschrijving" in body
        else body.index("Wat vertelt de aanbieder over deze auto?")
    )
    truncated = body[: start + 100]
    item = parse_public_detail(source, truncated, url)
    assert item.raw["detail_complete"] is None
