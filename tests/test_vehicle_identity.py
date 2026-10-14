import json

import pytest

from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.parse import Listing, Page


def ad(source, identity, registration=None, vin=None):
    return Listing(
        source,
        identity,
        "https://example.test/" + identity,
        "BMW 218i Active Tourer",
        fuel="petrol",
        kenteken=registration,
        vin=vin,
    ).qualify()


def vehicle(db, listing):
    return db.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (listing,)).fetchone()[0]


def test_exact_identity_merge_preserves_former_uuid_and_history(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    a = db.observe(run, ad("a", "1", "GB178T"), "2026-10-07T10:00:00+00:00")
    b = db.observe(run, ad("b", "2"), "2026-10-07T10:01:00+00:00")
    old = vehicle(db, b)
    winner = vehicle(db, a)
    db.observe(db.start_run(), ad("b", "2", "GB-178-T"), "2026-10-07T10:02:00+00:00")
    assert vehicle(db, b) == winner
    assert db.resolve_vehicle_id(old) == winner
    assert db.resolve_vehicle_id(winner) == winner
    assert db.resolve_vehicle_id("missing") is None
    assert db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM vehicle_aliases").fetchone()[0] == 1
    proof = json.loads(db.conn.execute("SELECT evidence_json FROM vehicle_aliases").fetchone()[0])
    assert proof["retired_vehicle"]["id"] == old
    assert proof["registration"] == "GB178T"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.conn.close()


def test_compatible_plate_vin_bridge_merges_two_established_ids(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    a = db.observe(run, ad("a", "1", "GB178T"), "2026-10-07T10:00:00+00:00")
    b = db.observe(run, ad("b", "2", vin="WBA12345678901234"), "2026-10-07T10:01:00+00:00")
    original_a, original_b = vehicle(db, a), vehicle(db, b)
    c = db.observe(run, ad("c", "3", "GB178T", "WBA12345678901234"), "2026-10-07T10:02:00+00:00")
    assert {vehicle(db, x) for x in (a, b, c)} == {original_a}
    assert db.resolve_vehicle_id(original_b) == original_a
    assert tuple(db.conn.execute("SELECT kenteken,vin FROM vehicles").fetchone()) == (
        "GB178T",
        "WBA12345678901234",
    )
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.conn.close()


def test_merge_of_already_merged_id_flattens_aliases_and_preserves_candidates(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    a = db.observe(run, ad("a", "1", vin="WBA12345678901234"), "2026-10-07T10:01:00+00:00")
    b = db.observe(run, ad("b", "2"), "2026-10-07T10:02:00+00:00")
    c = db.observe(run, ad("c", "3", "GB178T"), "2026-10-07T10:00:00+00:00")
    d = db.observe(run, ad("d", "4"), "2026-10-07T10:03:00+00:00")
    original_a, original_b, original_c, original_d = (vehicle(db, x) for x in (a, b, c, d))
    x, y = sorted((original_b, original_d))
    db.conn.execute(
        "INSERT INTO identity_candidates VALUES (?,?,?,?,?)",
        (x, y, "same_year_mileage_city", "2026-10-07T10:04:00+00:00", "unconfirmed"),
    )
    db.conn.commit()
    db.observe(db.start_run(), ad("b", "2", vin="WBA12345678901234"), "2026-10-07T10:05:00+00:00")
    db.observe(
        db.start_run(), ad("a", "1", "GB178T", "WBA12345678901234"), "2026-10-07T10:06:00+00:00"
    )
    assert db.resolve_vehicle_id(original_a) == db.resolve_vehicle_id(original_b) == original_c
    assert tuple(
        db.conn.execute("SELECT vehicle_a,vehicle_b FROM identity_candidates").fetchone()
    ) == tuple(sorted((original_c, original_d)))
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.conn.close()


def test_conflicting_two_identifier_bridge_is_atomic(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    db.observe(run, ad("a", "1", "GB178T", "WBA12345678901234"), "2026-10-07T10:00:00+00:00")
    db.observe(run, ad("b", "2", "AB123C", "WBA12345678901235"), "2026-10-07T10:00:00+00:00")
    before = [tuple(x) for x in db.conn.execute("SELECT * FROM vehicles ORDER BY id")]
    with pytest.raises(ValueError, match="Conflicting"):
        db.observe(
            db.start_run(), ad("c", "3", "GB178T", "WBA12345678901235"), "2026-10-07T10:02:00+00:00"
        )
    assert [tuple(x) for x in db.conn.execute("SELECT * FROM vehicles ORDER BY id")] == before
    assert db.conn.execute("SELECT COUNT(*) FROM vehicle_aliases").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2
    db.conn.close()


def test_v2_migration_keeps_all_existing_uuids_and_observations(tmp_path):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    db.observe(db.start_run(), ad("a", "1", "GB178T"), "2026-10-07T10:00:00+00:00")
    original = [tuple(x) for x in db.conn.execute("SELECT * FROM vehicles")]
    db.conn.execute("DROP TABLE vehicle_aliases")
    db.conn.execute("PRAGMA user_version=2")
    db.conn.commit()
    db.conn.close()
    migrated = Database(path)
    assert migrated.conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert [tuple(x) for x in migrated.conn.execute("SELECT * FROM vehicles")] == original
    assert migrated.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    migrated.conn.close()


def test_verified_gaspedaal_portal_reference_links_own_confirmed_detail(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    original = ad("gaspedaal", "1")
    url = "https://dealer.test/stock/1"
    original.raw["portalen"] = [{"klikUrl": url}]
    listing = db.observe(db.start_run(), original, "2026-10-07T10:00:00+00:00")
    former = vehicle(db, listing)
    detail = ad("dealer:dealer.test", "1", "GB178T")
    detail.url = url
    collector = Collector(db, {"detail_batch_limit": 1})
    collector.detail_jobs[url] = ("portal", listing, original)
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda _: Response(url, url, 200, "test", "2026-10-07T10:01:00+00:00"),
    )
    monkeypatch.setattr("car_market.collect.parse_page", lambda *args: Page([detail]))
    collector.details(db.start_run())
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 1
    assert db.resolve_vehicle_id(former) == vehicle(db, listing)
    proof = json.loads(
        db.conn.execute(
            "SELECT evidence_json FROM vehicle_aliases WHERE alias_id=?", (former,)
        ).fetchone()[0]
    )
    assert proof["reference_evidence"]["kind"] == "gaspedaal_portal_reference"
    assert proof["reference_evidence"]["fetch_id"] is not None
    db.conn.close()


def test_vehicle_exports_and_cli_resolve_one_enriched_car(tmp_path):
    import csv
    import subprocess
    import sys

    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    first = ad("a", "1", "GB178T")
    first.year = 2016
    first.price = 8500
    a = db.observe(run, first, "2026-10-07T10:00:00+00:00")
    second = ad("b", "2")
    b = db.observe(run, second, "2026-10-07T10:01:00+00:00")
    former = vehicle(db, b)
    second.kenteken = "GB178T"
    second.transmission = "manual"
    db.observe(db.start_run(), second, "2026-10-07T10:02:00+00:00")
    target = tmp_path / "exports"
    db.export(target)
    cards = json.loads((target / "vehicles.json").read_text())
    assert len(cards) == 1
    assert (cards[0]["id"], cards[0]["year"], cards[0]["transmission"]) == (
        vehicle(db, a),
        2016,
        "manual",
    )
    assert len(cards[0]["offers"]) == 2
    with (target / "vehicles.csv").open() as stream:
        assert len(list(csv.DictReader(stream))) == 1
    before = db.summary()
    result = subprocess.run(
        [sys.executable, "-m", "car_market.cli", "--db", str(db.path), "vehicle", former],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout)["id"] == cards[0]["id"]
    assert db.summary() == before
    db.conn.close()


def test_portal_reference_cannot_transfer_plate_inherited_from_an_old_detail(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    url = "https://dealer.test/stock/1"
    previous = ad("dealer:dealer.test", url, "GB178T")
    previous.url = url
    db.observe(db.start_run(), previous, "2026-10-07T09:00:00+00:00")
    original = ad("gaspedaal", "1")
    original.raw["portalen"] = [{"klikUrl": url}]
    listing = db.observe(db.start_run(), original, "2026-10-07T10:00:00+00:00")
    fresh = ad("dealer:dealer.test", "1")
    fresh.url = url
    collector = Collector(db, {"detail_batch_limit": 1, "detail_cache_hours": 0})
    collector.detail_jobs[url] = ("portal", listing, original)
    monkeypatch.setattr(
        collector.fetcher,
        "get",
        lambda _: Response(url, url, 200, "test", "2026-10-07T11:00:00+00:00"),
    )
    monkeypatch.setattr("car_market.collect.parse_page", lambda *args: Page([fresh]))
    collector.details(db.start_run())
    assert (
        db.conn.execute(
            "SELECT kenteken FROM vehicles WHERE id=?", (vehicle(db, listing),)
        ).fetchone()[0]
        is None
    )
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 2
    db.conn.close()
