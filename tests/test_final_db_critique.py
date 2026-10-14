"""Regression oracles for audited identity, provenance and writer failures."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from car_market import cli
from car_market.collect import Collector, identify_source, parse_market_page
from car_market.coverage import report
from car_market.db import Database
from car_market.infra_support import FileLock
from car_market.parse import Listing, model_year, parse_page
from car_market.provenance import value_hash

STAMP = "2026-10-07T10:00:00+00:00"
LATER = "2026-10-07T11:00:00+00:00"


def ad(key="one", **kwargs):
    return Listing(
        "fixture",
        key,
        "https://example.test/" + key,
        "BMW 218i Active Tourer",
        price=8000,
        year=2016,
        mileage=120000,
        fuel="petrol",
        transmission="manual",
        **kwargs,
    ).qualify()


@pytest.mark.parametrize(
    "vin", ["UNKNOWN", "unknown", "   ", "VIN-A", "WBA1234567890I234", "WBA1234567890Q234"]
)
def test_invalid_vin_cannot_join_distinct_cars(tmp_path, vin):
    db = Database(tmp_path / "market.sqlite3")
    db.observe(db.start_run(), ad("a", vin=vin), STAMP)
    db.observe(db.start_run(), ad("b", vin=vin), STAMP)
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles WHERE vin IS NOT NULL").fetchone()[0] == 0
    db.conn.close()


def test_valid_vin_case_and_surrounding_space_match_exactly(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    db.observe(db.start_run(), ad("a", vin=" wba12345678901234 "), STAMP)
    db.observe(db.start_run(), ad("b", vin="WBA12345678901234"), STAMP)
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 1
    db.conn.close()


def test_direct_unchanged_confirmations_are_bounded_and_not_inherited(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    lid = db.observe(db.start_run(), ad(description="Original complete description"), STAMP)
    db.observe(db.start_run(), ad(), LATER, query_key="search")
    assert db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    rows = {
        row["field"]: row
        for row in db.conn.execute("SELECT * FROM field_verifications WHERE listing_id=?", (lid,))
    }
    assert rows["price"]["verified_at"] == LATER
    assert rows["description"]["verified_at"] == STAMP
    db.observe(db.start_run(), ad(), "2026-10-06T00:00:00+00:00")
    assert (
        db.conn.execute(
            "SELECT verified_at FROM field_verifications WHERE listing_id=? AND field='price'",
            (lid,),
        ).fetchone()[0]
        == LATER
    )
    assert value_hash(8000) == value_hash(8000.0)
    db.conn.close()


def test_new_unknown_field_conflict_is_a_meaningful_observation(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    item = ad()
    item.fuel = None
    db.observe(db.start_run(), item, STAMP)
    conflict = ad()
    conflict.fuel = None
    conflict.raw["field_conflicts"] = {"fuel": ["petrol", "diesel"]}
    db.observe(db.start_run(), conflict, LATER)
    assert db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    details = json.loads(
        db.conn.execute("SELECT details_json FROM latest_observations").fetchone()[0]
    )
    assert details["raw"]["field_conflicts"]["fuel"] == ["petrol", "diesel"]
    db.conn.close()


@pytest.mark.parametrize("finished_at", ["2026-07-01T00:00:00+00:00", None])
def test_archive_health_keeps_original_timestamp_and_recent_failure(tmp_path, finished_at):
    db = Database(tmp_path / "market.sqlite3")
    run = db.start_run()
    db.finish_source(run, "fixture", "all", STAMP, "error", None, 0, 0, 1, finished_at=LATER)
    db.finish_source(
        db.start_run(),
        "fixture",
        "all",
        "2026-07-01T00:00:00+00:00",
        "complete",
        1,
        1,
        1,
        1,
        finished_at=finished_at,
        infer_missing=False,
    )
    assert (
        db.conn.execute("SELECT finished_at FROM source_runs ORDER BY id DESC LIMIT 1").fetchone()[
            0
        ]
        == finished_at
    )
    assert db.conn.execute("SELECT status FROM source_health").fetchone()[0] == "error"
    db.conn.close()


def test_unknown_source_finish_time_is_stale(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    db.finish_source(
        db.start_run(), "fixture", "all", STAMP, "complete", 1, 1, 1, 1, finished_at=None
    )
    cfg = {"queries": [{"source": "fixture", "key": "all", "url": "https://example.test"}]}
    result = report(db.conn, cfg)["queries"][0]
    assert result["stale"] and result["age_seconds"] is None
    db.conn.close()


def test_busy_writer_does_not_create_or_migrate_database(tmp_path):
    path = tmp_path / "market.sqlite3"
    args = SimpleNamespace(db=path)
    with FileLock(tmp_path / "market.sqlite3.lock", blocking=False):
        assert cli.collect_once(args, {}) == {"status": "already_running"}
        assert not path.exists()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=1")
    with FileLock(tmp_path / "market.sqlite3.lock", blocking=False):
        cli.collect_once(args, {})
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()


@pytest.mark.parametrize(
    "raw,expected",
    [("17-01-2020", 2020), ("2015-02-01", 2015), ("2016", 2016), ("17", None), ("unknown", None)],
)
def test_schema_model_year_is_not_first_day(raw, expected):
    assert model_year(raw) == expected


def test_source_adapters_are_connected_to_collector():
    from pathlib import Path

    root = Path(__file__).parent / "fixtures/final_source_details"
    provenance = json.loads((root / "provenance.json").read_text())
    for name in ("schalk", "driveworks"):
        url = provenance[name + ".html"]["url"]
        page = parse_market_page(identify_source(url), (root / (name + ".html")).read_text(), url)
        assert len(page.listings) == 1
        assert page.listings[0].url == url
        assert page.listings[0].source == identify_source(url)
    url = provenance["trovit-detail.html"]["url"]
    assert identify_source(url) == "trovit"
    item = parse_page("trovit", (root / "trovit-detail.html").read_text(), url).listings[0]
    assert item.kenteken == "TP900R"


def test_backoff_does_not_report_completed_enrichment(tmp_path, monkeypatch):
    from car_market.db import now

    db = Database(tmp_path / "market.sqlite3")
    item = ad()
    item.source = "dealer:example.test"
    lid = db.observe(db.start_run(), item, now())
    db.save_fetch(
        db.start_run(), item.source, item.url, item.url, 403, None, now(), error="http_403"
    )
    collector = Collector(db, {"detail_cache_hours": 24})
    collector.queue_details(item, lid)
    monkeypatch.setattr(collector.fetcher, "get", lambda _: pytest.fail("denial bypassed"))
    run = db.start_run()
    collector.details(run)
    health = db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    assert health["status"] == "partial" and health["expected_count"] == 1
    assert health["received_count"] == 0
    assert collector.detail_stats[item.source]["backoff"] == 1
    db.conn.close()


@pytest.mark.parametrize(
    "guard", ["none", "own_observation", "rdw_check", "different_source", "different_uuid"]
)
def test_known_legacy_registration_quarantine_is_guarded_and_keeps_id(tmp_path, guard):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    identity = "dcf8d73f-3576-4af1-ba1c-f389fe46c1d8" if guard != "different_uuid" else "unrelated"
    db.conn.execute("INSERT INTO vehicles VALUES (?,?,?,?)", (identity, "RG856VTE", None, STAMP))
    item = ad()
    item.source = "trovit" if guard != "different_source" else "dealer:example.test"
    lid = db.observe(db.start_run(), item, STAMP, vehicle_id_hint=identity)
    if guard == "own_observation":
        db.conn.execute("UPDATE observations SET kenteken=? WHERE listing_id=?", ("RG856VTE", lid))
    if guard == "rdw_check":
        db.conn.execute(
            "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json) VALUES (?,?,?,?)",
            (identity, STAMP, "rdw", "{}"),
        )
    db.conn.commit()
    db.conn.close()
    reopened = Database(path)
    plate = reopened.conn.execute(
        "SELECT kenteken FROM vehicles WHERE id=?", (identity,)
    ).fetchone()[0]
    assert plate == (None if guard == "none" else "RG856VTE")
    assert (
        reopened.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (lid,)).fetchone()[0]
        == identity
    )
    assert reopened.conn.execute("SELECT COUNT(*) FROM identity_quarantine").fetchone()[0] == (
        1 if guard == "none" else 0
    )
    assert reopened.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.conn.close()
    repeated = Database(path)
    assert repeated.conn.execute("SELECT COUNT(*) FROM identity_quarantine").fetchone()[0] == (
        1 if guard == "none" else 0
    )
    repeated.conn.close()


def test_busy_full_pass_keeps_full_slot_without_six_hour_delay(tmp_path, monkeypatch):
    import sys

    path = tmp_path / "market.sqlite3"
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"queries": [], "fast_sources": ["s"]}))
    monkeypatch.setattr(
        sys, "argv", ["car-market", "--db", str(path), "--config", str(cfg), "daemon"]
    )
    ticks = {"value": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: ticks["value"])
    monkeypatch.setattr(
        cli.time, "sleep", lambda seconds: ticks.update(value=ticks["value"] + seconds)
    )
    calls = []

    def collect(args, config):
        calls.append(args.fast)
        if len(calls) == 1:
            return {"status": "already_running"}
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "collect_once", collect)
    with pytest.raises(KeyboardInterrupt):
        cli.main()
    assert calls == [False, False]


@pytest.mark.parametrize("vin", ["00000000000000000", "AAAAAAAAAAAAAAAAA"])
def test_repeated_vin_placeholders_are_not_identity(vin):
    from car_market.identity import normalized_vin

    assert normalized_vin(vin) is None


def test_registry_hint_resolves_alias_without_provisional_uuid(tmp_path, monkeypatch):
    db = Database(tmp_path / "market.sqlite3")
    db.conn.execute("INSERT INTO vehicles VALUES (?,?,?,?)", ("root", None, None, STAMP))
    db.conn.execute(
        "INSERT INTO vehicle_aliases VALUES (?,?,?,?,?)",
        ("old-root", "root", STAMP, "exact_vin", "{}"),
    )
    db.conn.commit()
    monkeypatch.setattr("car_market.db.uuid.uuid4", lambda: pytest.fail("provisional UUID minted"))
    item = ad()
    lid = db.observe(db.start_run(), item, STAMP, vehicle_id_hint="old-root")
    assert (
        db.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (lid,)).fetchone()[0]
        == "root"
    )
    assert item.kenteken is None and item.vin is None
    stored = json.loads(
        db.conn.execute(
            "SELECT details_json FROM observations WHERE listing_id=?", (lid,)
        ).fetchone()[0]
    )
    assert stored["kenteken"] is None and stored["vin"] is None
    db.conn.close()


def test_import_hint_conflict_rolls_back_precreated_registry_and_fetches(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    lid = db.observe(db.start_run(), ad(), STAMP)
    original = list(db.conn.iterdump())
    with pytest.raises(ValueError, match="conflicts"), db.transaction():
        db.conn.execute("INSERT INTO vehicles VALUES (?,?,?,?)", ("other", None, None, STAMP))
        run = db.start_run()
        fetch = db.save_fetch(
            run, "fixture", "https://example.test", "https://example.test", 200, None, LATER
        )
        db.observe(run, ad(), LATER, fetch, vehicle_id_hint="other")
    assert list(db.conn.iterdump()) == original
    assert db.conn.execute("SELECT id FROM listings").fetchone()[0] == lid
    db.conn.close()


def test_status_under_writer_lock_does_not_migrate_database(tmp_path, monkeypatch, capsys):
    import sys

    path = tmp_path / "market.sqlite3"
    db = Database(path)
    db.conn.execute("DROP TABLE field_verifications")
    db.conn.execute("PRAGMA user_version=3")
    db.conn.commit()
    db.conn.close()
    monkeypatch.setattr(sys, "argv", ["car-market", "--db", str(path), "status"])
    with FileLock(tmp_path / "market.sqlite3.lock", blocking=False):
        cli.main()
    assert json.loads(capsys.readouterr().out)["vehicle_identities"] == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='field_verifications'"
        ).fetchone()


@pytest.mark.parametrize("command", ["enrich", "export"])
def test_other_writers_lock_before_schema_initialization(tmp_path, monkeypatch, command):
    import sys

    path = tmp_path / "market.sqlite3"
    monkeypatch.setattr(sys, "argv", ["car-market", "--db", str(path), command])
    with FileLock(tmp_path / "market.sqlite3.lock", blocking=False), pytest.raises(BlockingIOError):
        cli.main()
    assert not path.exists()


def test_source_health_orders_actual_time_across_timezone_offsets(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    db.finish_source(
        db.start_run(),
        "fixture",
        "all",
        STAMP,
        "error",
        None,
        0,
        0,
        1,
        finished_at="2026-10-07T11:00:00+00:00",
    )
    db.finish_source(
        db.start_run(),
        "fixture",
        "all",
        STAMP,
        "complete",
        1,
        1,
        1,
        1,
        finished_at="2026-10-07T12:00:00+02:00",
        infer_missing=False,
    )
    assert db.conn.execute("SELECT status FROM source_health").fetchone()[0] == "error"
    db.conn.close()
