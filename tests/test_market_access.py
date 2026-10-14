"""Real XML/API snapshots and evidence boundaries for interval completeness."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from car_market.collect import Collector, canonical_id
from car_market.db import Database
from car_market.dom import parse_dom
from car_market.marktnet import parse_xml
from car_market.parse import Page, parse_page
from car_market.partitions import collect_price_partitions
from defusedxml.common import EntitiesForbidden

FIXTURES = Path(__file__).parent / "fixtures/market_access"


def test_marktnet_rss_reads_actual_counts_plate_and_price():
    page = parse_xml((FIXTURES / "marktnet-search.xml").read_text())
    assert (page.total, page.pages, page.page, len(page.listings)) == (410, 21, 1, 20)
    ad = page.listings[0]
    assert (ad.price, ad.year, ad.mileage, ad.kenteken) == (4950, 2015, 206491, "GX803F")
    assert ad.scope == "target"
    assert ad.links[0]["url"].endswith("id=96732271")
    assert page.listings[1].scope == "excluded"  # Gran Tourer.


def test_full_xml_detail_keeps_description_and_stable_identity():
    page = parse_xml((FIXTURES / "marktnet-detail.xml").read_text())
    ad = page.listings[0]
    assert (ad.external_id, ad.price, ad.kenteken) == ("96585631", 29900, "JRB73F")
    assert not ad.raw["detail_complete"]
    assert ad.raw["description_may_be_truncated"]
    for url in (
        "https://www.marktnet.nl/96585631",
        "https://www.marktnet.nl/autos/bmw/x-96585631.htm",
        "https://www.marktnet.nl/advertentie.xml?id=96585631",
    ):
        assert canonical_id("marktnet", url) == ad.external_id


def test_xml_entities_are_rejected():
    with pytest.raises(EntitiesForbidden):
        parse_xml('<!DOCTYPE ad [<!ENTITY x "payload">]><ad id="1"><title>&x;</title></ad>')


def test_bmw_counter_is_an_input_value():
    page = parse_dom(
        "bmw", (FIXTURES / "bmw-counter.html").read_text(), "https://occasions.bmw.nl/"
    )
    assert page.total == 157


def test_vakgarage_bootstrap_separates_price_from_monthly_finance():
    page = parse_page(
        "vakgarage", (FIXTURES / "vakgarage.html").read_text(), "https://www.vakgarage.nl/occasions"
    )
    assert (page.total, page.pages) == (43, 1)
    assert page.listings[0].price == 29900
    assert page.listings[0].fuel == "hybrid"
    assert page.listings[1].scope == "target"
    assert canonical_id("vakgarage", page.listings[0].url) == page.listings[0].external_id


def test_autofirst_public_api_meta_and_sold_flag():
    body = (FIXTURES / "autofirst.json").read_text()
    page = parse_page(
        "autofirst", body, "https://www.autofirst-nederland.nl/api/v1/medium/occasions"
    )
    assert (page.total, page.pages, page.page) == (2044, 137, 1)
    assert page.listings[0].price == 7950
    assert page.listings[0].scope == "excluded"  # Nissan Note.
    assert canonical_id("autofirst", page.listings[0].url) == page.listings[0].external_id


def test_autofirst_seller_sold_vehicle_does_not_become_a_purchase_candidate():
    data = json.loads((FIXTURES / "autofirst-bmw.json").read_text())
    page = parse_page(
        "autofirst", json.dumps(data), "https://www.autofirst-nederland.nl/api/v1/medium/occasions"
    )
    assert page.listings[0].scope == "target"
    data["data"][0]["is_sold"] = True
    page = parse_page(
        "autofirst", json.dumps(data), "https://www.autofirst-nederland.nl/api/v1/medium/occasions"
    )
    assert page.listings[0].scope == "excluded"


def test_carteam_partial_excerpt_is_not_a_complete_catalogue():
    page = parse_page(
        "carteam",
        (FIXTURES / "carteam.html").read_text(),
        "https://www.carteam.nl/occasions?f1=1948",
    )
    assert page.total == 29
    assert len(page.listings) == 2
    assert page.pages is None
    assert page.listings[0].price is None


@pytest.mark.parametrize("broken", [False, True])
def test_partitions_require_child_counters_and_unique_ids_to_match(tmp_path, broken):
    # Authentic RSS prices and IDs; a small capacity models truncated results.
    ads = parse_xml((FIXTURES / "marktnet-search.xml").read_text()).listings[:2]
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {})
    collector.fetcher.get = lambda url: url

    def ingest(run, source, key, url):
        params = parse_qs(urlsplit(url).query)
        if "minprice" in params:
            return Page([ads[1]], total=2 if broken else 1), 1
        if "maxprice" in params:
            return Page([ads[0]], total=1), 1
        return Page(ads, total=2), 2

    collector.ingest_response = ingest
    query = {
        "source": "marktnet",
        "key": "full",
        "url": "https://example.test/feed",
        "partition_page_size": 1,
        "partition_request_limit": 3,
    }
    result = collect_price_partitions(collector, db.start_run(), query)
    assert result["status"] == ("partial" if broken else "complete")
    if broken:
        assert any("partition_counter_mismatch" in error for error in result["errors"])
    else:
        assert result["received"] == result["expected"] == 2
    db.conn.close()
