"""Native geographic partitions include unpriced ads beyond the national cap."""

import json
from pathlib import Path

import pytest
from car_market.germany_kleinanzeigen import (
    kleinanzeigen_page_url,
    kleinanzeigen_region_queries,
    parse_kleinanzeigen,
)

FIXTURES = Path(__file__).parent / "fixtures/germany_klein_recovery"
MANIFEST = {row["fixture"]: row for row in json.loads((FIXTURES / "manifest.json").read_text())}


def own(name):
    m = MANIFEST[name + ".html"]
    return (FIXTURES / (name + ".html")).read_text(), m["origin"]["final_url"]


def test_native_16_regions_preserve_model_sort_and_do_not_assign_country():
    body, url = own("national")
    queries = kleinanzeigen_region_queries(body, url)
    assert len(queries) == 16
    assert all("bmw-active-tourer" in q["url"] and "sortierung:neuste" in q["url"] for q in queries)
    assert all("/preis:" not in q["url"] for q in queries)
    assert parse_kleinanzeigen(body, url).search_filters["country"] == "UNKNOWN"


@pytest.mark.parametrize(
    "name,count,pages", [("active_region_bayern", 460, 19), ("active_region_bremen", 13, 1)]
)
def test_real_regional_counters_and_native_organic_membership(name, count, pages):
    body, url = own(name)
    page = parse_kleinanzeigen(body, url)
    assert (page.total, page.pages, page.page) == (count, pages, 1)
    assert page.search_filters["promotion_classification"] == "bound_native_tracker"
    assert sum(x.raw["placement"] == "organic" for x in page.listings) == min(25, count)
    assert all(x.raw["market_country"] == "UNKNOWN" for x in page.listings)


def test_actual_native_price_link_pagination_order():
    body, url = own("price-low")
    page = parse_kleinanzeigen(body, url)
    assert page.total == 547
    assert page.search_filters["price_bounds"] == [None, 10000]
    assert (
        kleinanzeigen_page_url(url, 2)
        == "https://www.kleinanzeigen.de/s-autos/sortierung:neuste/preis::10000/seite:2/bmw-active-tourer/k0c216"
    )
    assert kleinanzeigen_page_url(kleinanzeigen_page_url(url, 2), 1) == url


def test_native_price_echo_conflict_vetoes_range():
    body, url = own("price-low")
    page = parse_kleinanzeigen(body.replace('value="10000"', 'value="20000"'), url)
    assert page.search_filters["price_filter_conflict"] == "maxPrice"
    assert page.search_filters["range_valid"] is False


@pytest.mark.parametrize("path", ["preis::", "preis:20000:10000", "preis:-1:10000"])
def test_invalid_bound_routes_reject(path):
    with pytest.raises(ValueError):
        kleinanzeigen_page_url(
            "https://www.kleinanzeigen.de/s-autos/" + path + "/bmw-active-tourer/k0c216", 2
        )


def test_paid_feature_is_not_placement_of_this_native_impression():
    body, url = own("paid-feature-organic")
    page = parse_kleinanzeigen(body, url)
    assert page.search_filters["promotion_error"] is None
    paid = [
        item
        for item in page.listings
        if "TOP_AD" in item.raw["native_preview"]["appliedFeatures"]
        and item.raw["native_preview"]["topAd"] is False
    ]
    assert paid and all(item.raw["placement"] == "organic" for item in paid)
    assert sum(item.raw["native_preview"]["organic_present"] for item in page.listings) == 25


def test_same_publication_has_two_proven_native_impressions_not_two_cars():
    body, url = own("same-publication-two-placements")
    page = parse_kleinanzeigen(body, url)
    assert page.search_filters["promotion_error"] is None
    duplicates = [
        item for item in page.listings if len(item.raw["native_preview"]["native_occurrences"]) == 2
    ]
    assert len(duplicates) == 1
    preview = duplicates[0].raw["native_preview"]
    assert preview["placement"] == "promoted" and preview["organic_present"]
    assert [x["placement"] for x in preview["native_occurrences"]] == ["promoted", "organic"]
    assert sum(item.raw["native_preview"]["organic_present"] for item in page.listings) == 23


@pytest.mark.parametrize("name", ["own-opening-advantage", "own-opening-automatic"])
def test_real_short_title_is_resolved_only_by_bound_own_opening(name, tmp_path):
    import hashlib

    from car_market.collect import Collector
    from car_market.db import Database

    body, url = own(name)
    assert (
        hashlib.sha256((FIXTURES / (name + ".html")).read_bytes()).hexdigest()
        == MANIFEST[name + ".html"]["fixture_sha256"]
    )
    item = parse_kleinanzeigen(body, url).listings[0]
    assert item.scope == "target" and item.raw["model_variant"] == "218i"
    proof = item.raw["own_model_evidence"]
    assert proof["external_id"] == item.external_id and proof["offset"][0] == 0
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"scope": {"country": "DE"}})
    collector._observe(db.start_run(), item, MANIFEST[name + ".html"]["origin"]["fetched_at"])
    assert db.conn.execute("select scope from listings").fetchone()[0] == "target"
    db.conn.close()


@pytest.mark.parametrize("prefix", ["Weitere Angebote: ", "Ähnliche Fahrzeuge: ", "Alternativ: "])
def test_promotion_of_218i_does_not_resolve_own_short_title(prefix):
    from bs4 import BeautifulSoup

    body, url = own("own-opening-automatic")
    soup = BeautifulSoup(body, "html.parser")
    node = soup.select_one("#viewad-description-text")
    node.string = prefix + node.get_text()
    item = parse_kleinanzeigen(str(soup), url).listings[0]
    assert item.scope == "needs_confirmation"
    assert "own_model_evidence" not in item.raw


def test_other_models_in_later_promotions_do_not_override_own_opening():
    from bs4 import BeautifulSoup

    body, url = own("own-opening-advantage")
    soup = BeautifulSoup(body, "html.parser")
    node = soup.select_one("#viewad-description-text")
    node.string = node.get_text() + " Weitere Angebote: BMW 220i und BMW 218d."
    assert parse_kleinanzeigen(str(soup), url).listings[0].scope == "target"
