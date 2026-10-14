"""Malformed native text holds one own card while preserving the full census."""

import hashlib
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market import germany_dealer_inventory as inventory

FIXTURES = Path(__file__).parent / "fixtures/germany_dealer_inventory"
MANIFEST = json.loads(
    (
        Path(__file__).parent / "fixtures/wiveda_native_text/autoland-text-contract-manifest.json"
    ).read_text()
)


def retained(number):
    path = FIXTURES / ("autoland-p" + str(number) + ".html")
    meta = next(m for m in MANIFEST if m["fixture"].endswith(path.name))
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == meta["fixture_sha256"]
    assert meta["public_url"].startswith("https://autoland.de/bmw/2er-reihe")
    assert meta["status"] == 200 and meta["fetched_at"].startswith("2026-10-08T")
    assert "original_path" not in meta
    return content.decode()


@pytest.mark.parametrize("field", ["name", "vehicleConfiguration", "vehicleTransmission"])
@pytest.mark.parametrize("value", [1, 0, 1.25, True, False, None, ["native"], {"native": "text"}])
def test_malformed_json_text_holds_only_its_exact_owned_card(field, value):
    soup = BeautifulSoup(retained(1), "html.parser")
    script = soup.select_one('script[type="application/ld+json"]')
    payload = json.loads(script.get_text())
    original_ids = {
        card["data-article-id"] for card in soup.select(".ivl-search--article[data-article-id]")
    }
    payload["itemListElement"][2]["item"][field] = value
    script.string = json.dumps(payload)

    page = inventory.parse_dealer_inventory(str(soup), inventory.ROOT)

    assert len(page.listings) == 11
    assert len(page.links) == len(page.search_filters["discovered"]) == 12
    assert {x["stock_id"] for x in page.search_filters["discovered"]} == original_ids
    assert {inventory.autoland_stock_id(x) for x in page.links} == original_ids
    assert {inventory.autoland_stock_id(x.url) for x in page.listings} == original_ids - {"100787"}
    assert len(page.search_filters["held"]) == 1
    assert page.search_filters["held"][0]["stock_id"] == "100787"
    assert all(x.raw["market_country"] == "DE" for x in page.listings)


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, "1"),
        (0, ""),
        (1.25, "1.25"),
        (True, "True"),
        (False, ""),
        (None, ""),
        (["H&amp;k"], "['H&amp;k']"),
        ({"name": "H<b>&amp;</b>k"}, "{'name': 'H<b>&amp;</b>k'}"),
    ],
)
def test_json_scalar_and_container_conversion_is_literal(value, expected):
    assert inventory._normal(value) == expected


@pytest.mark.parametrize("value", ["H&k", "H&amp;k", "H<b>&amp;</b>k"])
def test_decoded_native_strings_are_never_html_parsed(value):
    assert inventory._normal(value) == value


def test_existing_dom_node_text_contract_and_navigable_string():
    soup = BeautifulSoup("<h3>H<b>&amp;</b>k</h3>", "html.parser")
    assert inventory._normal(soup.h3) == "H & k"
    assert inventory._normal(soup.h3.b.string) == "&"
    assert inventory._normal(soup) == "H & k"
    assert inventory._normal(BeautifulSoup("", "html.parser")) == ""


@pytest.mark.parametrize("number", range(1, 6))
def test_retained_autoland_native_census_is_unchanged(number):
    body = retained(number)
    page = inventory.parse_dealer_inventory(
        body, inventory.dealer_inventory_page_url(inventory.ROOT, number)
    )
    soup = BeautifulSoup(body, "html.parser")
    own_ids = {
        card["data-article-id"] for card in soup.select(".ivl-search--article[data-article-id]")
    }
    assert len(page.listings) == len(own_ids)
    assert {inventory.autoland_stock_id(x.url) for x in page.listings} == own_ids
    assert {x["stock_id"] for x in page.search_filters["discovered"]} == own_ids
    assert not page.search_filters["held"]
