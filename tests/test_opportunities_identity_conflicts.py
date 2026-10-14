"""Own native year conflicts keep prices/identity but cannot supply comparison peers."""

import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from car_market.db import Database
from car_market.opportunities import evaluate, inventory
from car_market.parse import Listing, parse_page
from car_market.vehicle_catalog import get_vehicle

CLOCK = datetime(2026, 10, 11, 14, 58, tzinfo=UTC)
CONFIG = {"as_of": CLOCK.isoformat(), "freshness_hours": 13}
FIXTURES = Path(__file__).parent / "fixtures/identity_conflicts"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Identity conflict regression attempted a network request")

    monkeypatch.setattr("socket.socket.connect", forbidden)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "market.sqlite3")
    yield database
    database.conn.close()


def native(source, plate):
    meta = next(
        row for row in MANIFEST if row["source"] == source and row["plate"] == plate
    )
    compressed = (FIXTURES / meta["filename"]).read_bytes()
    assert hashlib.sha256(compressed).hexdigest() == meta["compressed_sha256"]
    body = gzip.decompress(compressed)
    assert hashlib.sha256(body).hexdigest() == meta["decoded_sha256"]
    page = parse_page(source, body.decode(), meta["url"])
    own = next(
        item for item in page.listings if item.external_id == meta["external_id"]
    )
    assert own.kenteken == plate and own.year == meta["native_year"]
    return own, body.decode(), meta


def native_observe(db, source, plate, *, stale=False):
    own, body, meta = native(source, plate)
    stamp = (CLOCK - timedelta(hours=14)).isoformat() if stale else meta["captured_at"]
    run = db.start_run(timestamp=stamp)
    fetch = db.save_fetch(run, source, meta["url"], meta["url"], 200, body, stamp)
    listing = db.observe(run, own, stamp, fetch)
    identity = db.conn.execute(
        "SELECT vehicle_id FROM listings WHERE id=?", (listing,)
    ).fetchone()[0]
    return listing, identity


def clean_observe(
    db, key, *, price=10000, mileage=175000, year=2015, kenteken=None, vin=None
):
    own = Listing(
        "fixture",
        key,
        "https://example.test/" + key,
        "BMW 218i Active Tourer",
        price=price,
        mileage=mileage,
        year=year,
        fuel="petrol",
        transmission="manual",
        kenteken=kenteken,
        vin=vin,
    ).qualify()
    listing = db.observe(
        db.start_run(timestamp=CLOCK.isoformat()), own, CLOCK.isoformat()
    )
    return db.conn.execute(
        "SELECT vehicle_id FROM listings WHERE id=?", (listing,)
    ).fetchone()[0]


def peers(db):
    for i in range(6):
        clean_observe(db, "peer-" + str(i), vin=f"WBA2A31010V{i:06d}")


def stored_state(db):
    return {
        table: [
            tuple(row)
            for row in db.conn.execute("SELECT * FROM " + table + " ORDER BY 1")
        ]
        for table in (
            "vehicles",
            "listings",
            "observations",
            "fetches",
            "field_verifications",
        )
    }


@pytest.mark.parametrize("plate", ["7ZHL92", "6ZFK13"])
@pytest.mark.parametrize(
    "order", [("marktplaats", "marktnet"), ("marktnet", "marktplaats")]
)
def test_native_year_disagreement_keeps_one_priced_identity_and_original_claims(
    db, plate, order
):
    identities = [native_observe(db, source, plate)[1] for source in order]
    assert identities[0] == identities[1]
    before = stored_state(db)
    car = inventory(db.conn, CONFIG)[0]
    assert car["vehicle_id"] == identities[0] and car["ad_count"] == 2
    assert car["price_eur"] == (7950 if plate == "7ZHL92" else 6499)
    assert car["comparison_fields"]["year"] is None
    assert get_vehicle(db.conn, identities[0], CONFIG)["year"] is None
    assert car["year"] in {2014, 2015}
    result = evaluate(db.conn, CONFIG)
    assert result["comparison_cars"] == result["confirmed_comparison_cars"] == 1
    assert result["cars"][0]["peer_count"] == 0
    assert result["cars"][0]["priority"] == "ordinary" and not result["opportunities"]
    assert stored_state(db) == before
    assert {
        json.loads(row[0])["year"]
        for row in db.conn.execute("SELECT details_json FROM observations")
    } == {2014, 2015}


def test_native_disputed_identity_cannot_increase_a_clean_candidates_peer_count(db):
    for source in ("marktplaats", "marktnet"):
        native_observe(db, source, "6ZFK13")
    peers(db)
    identity = clean_observe(
        db, "candidate", price=6000, mileage=177000, kenteken="GB178T"
    )
    candidate = next(
        car
        for car in evaluate(db.conn, CONFIG)["cars"]
        if car["vehicle_id"] == identity
    )
    assert candidate["peer_count"] == 6 and candidate["peer_median_eur"] == 10000
    assert (
        candidate["discount_pct"] == 40 and candidate["priority"] == "inspect_urgently"
    )


@pytest.mark.parametrize("state", ["stale", "removed"])
def test_inactive_native_disagreement_does_not_override_fresh_clean_claim(db, state):
    _, identity = native_observe(db, "marktplaats", "6ZFK13")
    listing, same = native_observe(db, "marktnet", "6ZFK13", stale=state == "stale")
    assert identity == same
    if state == "removed":
        db.conn.execute(
            "UPDATE listings SET availability='removed' WHERE id=?", (listing,)
        )
        db.conn.commit()
    car = inventory(db.conn, CONFIG)[0]
    assert (
        car["vehicle_id"] == identity
        and car["year"] == 2014
        and car["price_eur"] == 6499
    )
    assert not car.get("comparison_field_conflicts")


def test_clean_native_claim_and_clean_alert_control_retain_eligibility(db):
    _, native_id = native_observe(db, "marktplaats", "7ZHL92")
    peers(db)
    candidate_id = clean_observe(
        db, "candidate", price=6000, mileage=177000, kenteken="GB178T"
    )
    before = stored_state(db)
    cars = {car["vehicle_id"]: car for car in evaluate(db.conn, CONFIG)["cars"]}
    assert cars[native_id]["year"] == 2014 and cars[native_id]["price_eur"] == 7950
    assert not cars[native_id].get("comparison_field_conflicts")
    assert cars[candidate_id]["priority"] == "inspect_urgently"
    assert cars[candidate_id]["peer_count"] == 6
    assert stored_state(db) == before


def test_single_publication_year_correction_is_history_not_a_current_disagreement(db):
    identity = clean_observe(db, "correction", year=2014, kenteken="GB178T")
    before_history = db.conn.execute(
        "SELECT details_json FROM observations"
    ).fetchone()[0]
    same = clean_observe(db, "correction", year=2015, kenteken="GB178T")
    assert identity == same
    car = inventory(db.conn, CONFIG)[0]
    assert car["year"] == 2015 and not car.get("comparison_field_conflicts")
    assert (
        db.conn.execute(
            "SELECT details_json FROM observations ORDER BY id LIMIT 1"
        ).fetchone()[0]
        == before_history
    )
