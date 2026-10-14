"""Retained promotions and redirects keep publication IDs and unknown variants."""

import hashlib
import json
from pathlib import Path

import pytest
from car_market.collect import Collector, canonical_id
from car_market.db import Database, now
from car_market.fetch import Response
from car_market.parse import Listing, parse_page

ROOT = Path(__file__).parent / "fixtures/nederlandmobiel_promotions"
META = json.loads((ROOT / "provenance.json").read_text())
TROVIT = Path(__file__).parent / "fixtures/final_source_details"
TMETA = json.loads((TROVIT / "provenance.json").read_text())


def fixture(name):
    body = (ROOT / name).read_bytes()
    assert hashlib.sha256(body).hexdigest() == META[name]["retained_sha256"]
    return body.decode(), META[name].get("final_url", META[name].get("url"))


def test_filtered_inventory_includes_real_matching_promotion_without_wrong_bmw_models():
    body, url = fixture("inventory.html")
    page = parse_page("nederlandmobiel", body, url)
    assert len(page.listings) == page.total == 15
    byid = {item.external_id: item for item in page.listings}
    assert "22591411" not in byid and "16430381" not in byid
    item = byid["22771213"]
    assert item.scope == "needs_confirmation" and "218i" not in item.title.lower()
    assert (item.price, item.mileage, item.year, item.fuel) == (8900, 197877, 2015, "petrol")


@pytest.mark.parametrize("suffix", ["?bouwjaar_van=2022", "?bouwjaar_tot=2014"])
def test_promoted_publication_outside_year_filter_is_not_counted(suffix):
    body, url = fixture("inventory.html")
    assert "22771213" not in {
        item.external_id for item in parse_page("nederlandmobiel", body, url + suffix).listings
    }


def test_unfiltered_promotion_is_not_assumed_to_be_primary_inventory():
    body, _ = fixture("inventory.html")
    assert "22771213" not in {
        item.external_id
        for item in parse_page("nederlandmobiel", body, "https://www.nederlandmobiel.nl/").listings
    }


@pytest.mark.parametrize(
    "title,fuel,scope",
    [
        ("BMW 2-Serie Active Tourer 1.5 benzine", "petrol", "needs_confirmation"),
        ("BMW 216i Active Tourer 1.5", "petrol", "excluded"),
        ("BMW 220i Active Tourer 1.5", "petrol", "excluded"),
        ("BMW 218d Active Tourer 1.5", "petrol", "excluded"),
        ("BMW 2-Serie Gran Tourer 1.5 benzine", "petrol", "excluded"),
        ("BMW 2-Serie Active Tourer 1.5 diesel", "diesel", "excluded"),
        ("BMW 2-Serie Active Tourer 1.5 hybride", "hybrid", "excluded"),
        ("Renault Active Tourer 1.5 benzine", "petrol", "excluded"),
    ],
)
def test_missing_218i_variant_is_a_candidate_without_inventing_target(title, fuel, scope):
    item = Listing("fixture", "one", "https://example.test/one", title, fuel=fuel).qualify()
    assert item.scope == scope


@pytest.mark.parametrize(
    "source,original,final,identity",
    [
        (
            "nederlandmobiel",
            "https://www.nederlandmobiel.nl/tweedehands-auto/bmw/vehicle/22771213?merk[]=29&i=1",
            "https://www.nederlandmobiel.nl/tweedehands-auto/bmw/new-slug/22771213",
            "22771213",
        ),
        (
            "trovit",
            "https://rd.clk.thribee.com/id.i1U161T1i1Oq1G/origin.2/section.1",
            "https://autos.trovit.nl/listing/own.i1U161T1i1Oq1G",
            "i1U161T1i1Oq1G",
        ),
    ],
)
def test_canonical_publication_key_survives_tracking_and_redirect(
    source, original, final, identity
):
    assert canonical_id(source, original) == canonical_id(source, final) == identity


@pytest.mark.parametrize("source", ["nederlandmobiel", "trovit"])
def test_real_owned_detail_updates_original_search_listing_without_new_publication(
    tmp_path, monkeypatch, source
):
    db = Database(tmp_path / "market.sqlite3")
    if source == "nederlandmobiel":
        body, final = fixture("detail.html")
        search, url = fixture("inventory.html")
        original = next(
            item
            for item in parse_page(source, search, url).listings
            if item.external_id == "22771213"
        )
    else:
        body = (TROVIT / "trovit-detail.html").read_text()
        final = TMETA["trovit-detail.html"]["url"]
        original = Listing(
            source,
            "i1U161T1i1Oq1G",
            "https://rd.clk.thribee.com/id.i1U161T1i1Oq1G/origin.2/",
            "BMW 218i Active Tourer",
            fuel="petrol",
        ).qualify()
    lid = db.observe(db.start_run(), original, now())
    old_id = db.conn.execute("SELECT vehicle_id FROM listings").fetchone()[0]
    collector = Collector(db, {})
    collector.queue_details(original, lid)
    monkeypatch.setattr(
        collector.fetcher, "get", lambda url: Response(url, final, 200, body, now())
    )
    collector.details(db.start_run())
    assert collector.detail_stats[source]["parsed"] == 1
    rows = db.conn.execute("SELECT * FROM listings").fetchall()
    assert (
        len(rows) == 1 and rows[0]["id"] == lid and rows[0]["external_id"] == original.external_id
    )
    assert rows[0]["vehicle_id"] == old_id
    own = json.loads(db.conn.execute("SELECT details_json FROM latest_observations").fetchone()[0])
    assert own["kenteken"] == ("H460TP" if source == "nederlandmobiel" else "TP900R")
    assert db.conn.execute("SELECT COUNT(*) FROM vehicle_aliases").fetchone()[0] == 0
    db.conn.close()


@pytest.mark.parametrize(
    "title",
    [
        "BMW 216 Active Tourer 1.5 benzine",
        "BMW 220 Active Tourer 1.5 benzine",
        "BMW 216 D Active Tourer 1.5",
        "BMW 220d Active Tourer 1.5",
    ],
)
def test_explicit_other_variant_cannot_enter_ambiguous_15_candidate(title):
    assert (
        Listing("fixture", "other", "https://example.test/other", title, fuel="petrol")
        .qualify()
        .scope
        == "excluded"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "other_route",
        "missing_body_identity",
        "duplicate_canonical",
        "foreign_canonical",
        "missing_own_heading",
    ],
)
def test_nederland_owned_body_identity_is_independent_from_delivery_route(mutation):
    from bs4 import BeautifulSoup

    body, url = fixture("detail.html")
    soup = BeautifulSoup(body, "html.parser")
    if mutation == "other_route":
        url = url.replace("22771213", "22716718")
    elif mutation == "missing_body_identity":
        soup.select_one('link[rel="canonical"]').decompose()
        for node in soup.select('a[href*="redirect.php"]'):
            node.decompose()
    elif mutation == "duplicate_canonical":
        soup.head.append(
            BeautifulSoup(str(soup.select_one('link[rel="canonical"]')), "html.parser")
        )
    elif mutation == "foreign_canonical":
        soup.select_one('link[rel="canonical"]')["href"] = (
            "https://evil.test/tweedehands-auto/bmw/own/22771213"
        )
    else:
        for node in soup.select("h1.detail-title"):
            node.decompose()
    assert parse_page("nederlandmobiel", str(soup), url).listings == []


def test_nederland_wrong_stock_response_cannot_attach_its_registration(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    search, url = fixture("inventory.html")
    original = next(
        x
        for x in parse_page("nederlandmobiel", search, url).listings
        if x.external_id == "22716718"
    )
    lid = db.observe(db.start_run(), original, now())
    old = db.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (lid,)).fetchone()[0]
    body, _ = fixture("detail.html")
    collector = Collector(db, {})
    collector.queue_details(original, lid)
    monkeypatch.setattr(collector.fetcher, "get", lambda url: Response(url, url, 200, body, now()))
    collector.details(db.start_run())
    assert collector.detail_stats["nederlandmobiel"]["parsed"] == 0
    assert collector.detail_stats["nederlandmobiel"]["errors"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert (
        db.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (lid,)).fetchone()[0] == old
    )
    assert db.conn.execute("SELECT kenteken FROM vehicles WHERE id=?", (old,)).fetchone()[0] is None
    db.conn.close()
