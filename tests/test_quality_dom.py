"""Regressions from live responses on 2026-10-07; full gzip captures remain outside Git."""

from pathlib import Path

import pytest
from car_market.dom import explicit_gearbox, parse_dom
from car_market.parse import parse_page

FIXTURES = Path(__file__).parent / "fixtures/quality_dom"


@pytest.mark.parametrize(
    "name,identity,kenteken,mileage,year,price",
    [
        ("mobiel-g111zg.html", "22437097", "G111ZG", 95525, 2020, 14845),
        ("mobiel-7xjr84.html", "19318832", "7XJR84", 118029, 2014, 10850),
    ],
)
def test_mobiel_detail_uses_own_specifications(name, identity, kenteken, mileage, year, price):
    page = parse_page(
        "nederlandmobiel",
        (FIXTURES / name).read_text(),
        "https://www.nederlandmobiel.nl/tweedehands-auto/bmw/example/" + identity,
    )
    assert len(page.listings) == 1
    ad = page.listings[0]
    assert (ad.external_id, ad.kenteken, ad.mileage, ad.year, ad.price) == (
        identity,
        kenteken,
        mileage,
        year,
        price,
    )
    assert ad.transmission == "manual"
    assert ad.fuel == "petrol"
    assert ad.seller and ad.city and ad.images
    assert ad.scope == "target"


def test_bmw_detail_reads_total_price_and_vehicle():
    page = parse_page(
        "bmw",
        (FIXTURES / "bmw-detail.html").read_text(),
        "https://occasions.bmw.nl/bmw/zoeken/resultaten/details/id/768961579",
    )
    ad = page.listings[0]
    assert (ad.price, ad.mileage, ad.year, ad.kenteken) == (14880, 112590, 2017, "NR482D")
    assert (ad.seller, ad.city, ad.transmission) == (
        "Hedin Automotive Waalwijk",
        "Waalwijk",
        "automatic",
    )
    assert ad.images and ad.scope == "target"


def test_bmw_counter_determines_pages_without_pagination_links():
    page = parse_dom("bmw", (FIXTURES / "bmw-search.html").read_text(), "https://occasions.bmw.nl/")
    assert (page.total, page.pages, len(page.listings)) == (156, 9, 18)
    assert all(a.images for a in page.listings)


def test_trovit_auto_call_to_action_is_not_a_gearbox():
    page = parse_page(
        "trovit", (FIXTURES / "trovit-cards.html").read_text(), "https://autos.trovit.nl/bmw-218i"
    )
    assert page.listings[0].transmission is None
    assert page.listings[2].transmission == "automatic"
    assert page.listings[2].kenteken == "7ZSX13"
    assert explicit_gearbox("Autobedrijf, handig, automatic climate control; Zie auto´s") is None


def test_marktnet_detail_recovers_seller_city_and_all_images():
    ad = parse_page(
        "marktnet",
        (FIXTURES / "marktnet-detail.xml").read_text(),
        "https://www.marktnet.nl/advertentie.xml?id=96585631",
    ).listings[0]
    assert (ad.price, ad.kenteken, ad.seller, ad.city) == (
        29900,
        "JRB73F",
        "Van der Krans-Auto's VOF",
        "HEERJANSDAM",
    )
    assert len(ad.images) == 10
    assert ad.transmission == "automatic"


@pytest.mark.parametrize(
    "identity,price,mileage,year,transmission,kenteken,seller,city",
    [
        ("60158836", 18999, 78852, 2021, "automatic", "N355JL", "Betag BV", "Beilen"),
        (
            "60163313",
            11950,
            194556,
            2018,
            "automatic",
            "N407RX",
            "Wesley Heeren Auto's",
            "Sint Willebrord",
        ),
        ("60163397", 10950, 111525, 2016, "manual", "TP900R", "ATL BMW & Mini", "Luttenberg"),
        (
            "60178421",
            5950,
            331787,
            2015,
            "automatic",
            "7ZSX13",
            "Achterberg Automotive",
            "Weerselo",
        ),
        ("60180286", 14990, 100388, 2021, "manual", "N449LJ", "De Rooy Auto's Dussen", "Dussen"),
    ],
)
def test_autoweek_detail_own_vehicle_fields(
    identity, price, mileage, year, transmission, kenteken, seller, city
):
    from car_market.dom import parse_autoweek_detail

    body = (FIXTURES / ("autoweek-detail-" + identity + ".html")).read_text()
    url = (
        "https://www.autoweek.nl/occasions/bmw/2-serie-tourer/" + identity + "/bmw-2-serie-tourer/"
    )
    result = parse_autoweek_detail(body, url)
    assert len(result.listings) == 1
    ad = result.listings[0]
    assert (ad.external_id, ad.price, ad.mileage, ad.year) == (identity, price, mileage, year)
    assert (ad.fuel, ad.transmission, ad.kenteken) == ("petrol", transmission, kenteken)
    assert (ad.seller, ad.city) == (seller, city)
    assert len(ad.description) > 2000 and ad.scope == "target"
    assert "218i" in ad.title and ad.images
    assert all("/" + identity + "/" in image for image in ad.images)


def test_autoweek_detail_rejects_other_id_and_missing_identity():
    from bs4 import BeautifulSoup
    from car_market.dom import parse_autoweek_detail

    body = (FIXTURES / "autoweek-detail-60163397.html").read_text()
    assert not parse_autoweek_detail(
        body, "https://www.autoweek.nl/occasions/bmw/2-serie-tourer/60180286/"
    ).listings
    soup = BeautifulSoup(body, "html.parser")
    for node in soup.select("[data-occasion-id]"):
        del node["data-occasion-id"]
    assert not parse_autoweek_detail(
        str(soup), "https://www.autoweek.nl/occasions/bmw/2-serie-tourer/60163397/"
    ).listings


def test_autoweek_description_keeps_seller_and_avoids_recommendations():
    from car_market.dom import parse_autoweek_detail

    body = (FIXTURES / "autoweek-detail-60163397.html").read_text()
    ad = parse_autoweek_detail(
        body, "https://www.autoweek.nl/occasions/bmw/2-serie-tourer/60163397/"
    ).listings[0]
    assert "www.atlautomotive.nl" in ad.description
    assert "Opties af-fabriek:" in ad.description
    assert "persoonlijk op de hoogte" not in ad.description.lower()
    assert "Meer BMW 2-serie" not in ad.description


def test_bmw_description_contains_only_own_equipment():
    ad = parse_page(
        "bmw",
        (FIXTURES / "bmw-detail.html").read_text(),
        "https://occasions.bmw.nl/bmw/zoeken/resultaten/details/id/768961579",
    ).listings[0]
    assert "ISOFIX bevestiging" in ad.description
    assert "Teleservices" in ad.description
    assert "privacy" not in ad.description.lower()
    assert "Service Inclusive" not in ad.description
