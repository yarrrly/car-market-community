"""Live public responses and regression checks for stable, scoped enumeration."""

import json
from pathlib import Path

import pytest
from car_market.collect import Collector, page_url, parse_market_page
from car_market.db import Database
from car_market.fetch import Response
from car_market.parse import parse_page

FIXTURES = Path(__file__).parent / "fixtures/source_completion"


def test_marktplaats_public_search_keeps_counts_and_native_listing_facts():
    body = (FIXTURES / "marktplaats-api.json").read_text()
    data = json.loads(body)
    url = "https://www.marktplaats.nl/lrp/api/search?limit=100&offset=0"
    page = parse_page("marktplaats", body, url)
    assert (page.total, page.pages, page.page) == (550, 6, 1)
    assert page.listings[0].external_id == "m2430756614"
    assert len(page.listings) == 3
    assert page.search_filters["searchQuery"] == "218i"
    assert page.search_filters["categories"]["l2Categories"][0]["id"] == 96
    assert page.listings[0].price == data["listings"][0]["priceInfo"]["priceCents"] / 100
    assert page_url({"url": url, "pagination": "marktplaats_api"}, 3, page).endswith("offset=200")
    data["searchRequest"]["pagination"]["offset"] = 500
    assert parse_page("marktplaats", json.dumps(data), url).page == 6


def test_marktplaats_api_failure_is_not_an_empty_catalogue():
    with pytest.raises(ValueError, match="unavailable"):
        parse_page("marktplaats", '{"hasErrors":true}', "https://www.marktplaats.nl/lrp/api/search")


def test_autouncle_articles_separate_own_price_from_discount_and_previous_price():
    page = parse_market_page(
        "autouncle",
        (FIXTURES / "autouncle-articles.html").read_text(),
        "https://www.autouncle.nl/nl-nl/gebruikte-auto/BMW/218%20Active%20Tourer",
    )
    assert page.total == 270
    first, second = page.listings
    assert (first.price, first.mileage, first.year) == (4950, 206491, 2015)
    assert (first.fuel, first.transmission) == ("petrol", "manual")
    assert (second.price, second.mileage, second.year) == (27999, 20579, 2023)
    assert second.transmission == "automatic"
    assert first.external_id == first.url
    assert first.seller is None  # Auto Trader is a source portal, not the seller.
    assert not first.raw["detail_complete"]
    assert all("images.autouncle.com/" in image for image in first.images)


def test_changed_search_filters_are_quarantined_before_observation(tmp_path):
    data = json.loads((FIXTURES / "marktplaats-api.json").read_text())
    data["maxAllowedPageNumber"] = 2
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"workers": 1})
    calls = []

    def fetch(url, refresh=False):
        calls.append((url, refresh))
        changed = json.loads(json.dumps(data))
        if "offset=100" in url:
            changed["searchRequest"]["pagination"]["offset"] = 100
            changed["searchRequest"]["searchQuery"] = "other inventory"
            changed["listings"][0]["itemId"] = "m9999999999"
        return Response(url, url, 200, json.dumps(changed), "2026-10-07T10:00:00+00:00")

    collector.fetcher.get = fetch
    collector.collect_query(
        db.start_run(),
        {
            "source": "marktplaats",
            "key": "control",
            "url": "https://example.test/search?limit=100&offset=0",
            "pagination": "marktplaats_api",
            "coverage": "enumerate",
        },
    )
    assert len(calls) == 3  # One ordinary refresh, then preserve the failure.
    assert not db.conn.execute("SELECT 1 FROM listings WHERE external_id='m9999999999'").fetchone()
    row = db.conn.execute("SELECT status,error FROM source_runs").fetchone()
    assert row["status"] == "partial"
    assert "search_filters_changed:2" in row["error"]
    assert (
        db.conn.execute("SELECT error FROM fetches ORDER BY id DESC LIMIT 1").fetchone()[0]
        == "search_filters_changed"
    )
    db.conn.close()


def test_autouncle_own_inventory_counter_overrides_stale_seo_title():
    body = (FIXTURES / "autouncle-inventory.html").read_text()
    url = "https://www.autouncle.nl/nl-nl/gebruikte-auto/BMW/218%20Active%20Tourer"
    assert parse_page("autouncle", body, url).total == 270
    # Another model's ItemList must not replace the requested inventory counter.
    assert parse_page("autouncle", body, url + "/other").total == 271


def test_autoscout_detail_without_seller_description_still_parses_own_vehicle():
    body = (FIXTURES / "autoscout-null-description.html").read_text()
    data = json.loads(body.split(">", 1)[1].split("</script>", 1)[0])["props"]["pageProps"][
        "listingDetails"
    ]
    page = parse_page("autoscout24", body, "https://www.autoscout24.nl/aanbod/" + data["id"])
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.external_id == data["id"]
    assert item.description == ""
    assert item.price == 4500
    assert item.raw["description"] is None


def test_autouncle_visible_own_specs_also_enrich_existing_jsonld():
    body = (FIXTURES / "autouncle-inventory.html").read_text() + (
        FIXTURES / "autouncle-articles.html"
    ).read_text()
    page = parse_market_page(
        "autouncle", body, "https://www.autouncle.nl/nl-nl/gebruikte-auto/BMW/218%20Active%20Tourer"
    )
    assert len(page.listings) == 2
    assert page.listings[0].scope == "target"
    assert (page.listings[0].price, page.listings[0].fuel, page.listings[0].transmission) == (
        4950,
        "petrol",
        "manual",
    )
    assert page.listings[1].price == 27999
