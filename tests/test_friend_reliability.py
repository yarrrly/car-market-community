"""Fault cases found by an independent first-use and data review."""

import copy
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from car_market.analytics import analyze_market, comparable_groups
from car_market.configuration import bind
from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database
from car_market.parse import Listing
from car_market.portable import main
from car_market.search_profile import GENERIC_PROFILE, qualify


UUID = "11111111-2222-4333-8444-555555555555"
URL = "https://www.autoscout24.nl/aanbod/toyota-corolla-" + UUID


def config():
    return {
        "scope": {
            "model_profile": GENERIC_PROFILE,
            "country": "NL",
            "selector": {
                "make": "Toyota",
                "model_terms": [["Corolla"]],
                "fuels": ["petrol"],
            },
        },
        "workers": 1,
        "detail_limit": 5,
        "detail_batch_limit": 5,
    }


def stamp(hours=0):
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def publication(source="autoscout24", raw=None, **kwargs):
    return Listing(
        source,
        UUID,
        URL,
        "Toyota Corolla",
        price=20000,
        year=2020,
        mileage=70000,
        transmission="automatic",
        fuel="petrol",
        raw={"location": {"countryCode": "NL"}, **(raw or {})},
        **kwargs,
    )


def observe(db, c, item, at):
    run = db.start_run("review", timestamp=at)
    result = db.observe(run, qualify(item, c["scope"]), at)
    db.finish_run(run, "complete", {})
    return result


def analyze(db):
    return analyze_market(db.path, "NL", as_of=stamp())


@pytest.mark.parametrize("status", ["sold", "reserved"])
def test_unchanged_columns_do_not_discard_new_unavailable_state(tmp_path, status):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    bind(db, c)
    observe(db, c, publication(raw={"availability_status": "available"}), stamp(3))
    observe(db, c, publication(raw={"availability_status": status}), stamp(2))
    assert analyze(db)["priced_ids"] == 0
    # An incomplete later card cannot silently clear this own state.
    observe(db, c, publication(), stamp(1))
    assert analyze(db)["priced_ids"] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 1
    assert not db.conn.execute(
        "SELECT 1 FROM events WHERE kind='price_eur_changed'"
    ).fetchone()
    db.conn.close()


def test_damage_changes_survive_missing_flags_until_explicit_correction(tmp_path):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    bind(db, c)
    for hours, raw, damaged in [
        (4, {"vehicle": {"isCurrentlyDamaged": False}}, False),
        (3, {"vehicle": {"isCurrentlyDamaged": True}}, True),
        (2, {}, True),
        (1, {"vehicle": {"isCurrentlyDamaged": False}}, False),
    ]:
        observe(db, c, publication(raw=raw), stamp(hours))
        market = analyze(db)
        assert market["priced_ids"] == 1
        assert market["cars"][0]["damage_declared"] is damaged
        assert bool(comparable_groups({"NL": market}, minimum=1)) is not damaged
    db.conn.close()


def test_explicit_available_reconfirmation_releases_peer_hold_but_presence_does_not(
    tmp_path,
):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    bind(db, c)
    observe(
        db,
        c,
        publication(raw={"availability_status": "available"}, kenteken="AB123C"),
        stamp(4),
    )
    observe(
        db,
        c,
        publication("marktplaats", {"availability_status": "sold"}, kenteken="AB123C"),
        stamp(3),
    )
    observe(db, c, publication(kenteken="AB123C"), stamp(2))
    assert analyze(db)["priced_ids"] == 0
    observe(
        db,
        c,
        publication(raw={"availability_status": "available"}, kenteken="AB123C"),
        stamp(1),
    )
    market = analyze(db)
    assert market["priced_ids"] == market["known_identifier_ids"] == 1
    assert market["cars"][0]["source"] == "autoscout24"
    assert any("own_sold_or_reserved" in row["reasons"] for row in market["excluded"])
    db.conn.close()


def native(own):
    return json.dumps({"props": {"pageProps": {"listingDetails": own}}})


def test_legacy_unavailable_state_keeps_its_original_clock_after_upgrade(tmp_path):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    bind(db, c)
    original_clock = stamp(3)
    observe(db, c, publication(raw={"availability_status": "sold"}), original_clock)
    previous = db.conn.execute(
        "SELECT id,details_json FROM latest_observations"
    ).fetchone()
    payload = json.loads(previous["details_json"])
    del payload["raw"]["availability_evidence"]
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    db.conn.execute(
        "UPDATE observations SET details_json=?,content_hash=? WHERE id=?",
        (content, hashlib.sha256(content.encode()).hexdigest(), previous["id"]),
    )
    db.conn.commit()
    observe(db, c, publication(), stamp(1))
    latest = json.loads(
        db.conn.execute("SELECT details_json FROM latest_observations").fetchone()[0]
    )
    assert latest["raw"]["availability_evidence"]["value"] == "sold"
    assert latest["raw"]["availability_evidence"]["observed_at"] == original_clock
    assert analyze(db)["priced_ids"] == 0
    assert (
        db.conn.execute("SELECT availability FROM listings").fetchone()[0]
        == "seller_marked_sold"
    )
    db.conn.close()


def test_unchanged_generic_search_keeps_each_exact_response_proof(tmp_path):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, c)
    own = {
        "id": UUID,
        "webPage": URL,
        "status": "Active",
        "url": URL,
        "location": {"countryCode": "NL"},
        "price": {"priceRaw": 20000, "currency": "EUR"},
        "vehicle": {
            "make": "Toyota",
            "model": "Corolla",
            "motorTypeName": "Corolla",
            "fuelCategory": {"formatted": "Benzine"},
        },
    }
    for index in range(2):
        # Same semantic search signature; a new response has different raw bytes.
        body = native({**own, "responseNonce": index})
        response = SimpleNamespace(
            url=URL,
            final_url=URL,
            status=200,
            body=body,
            timestamp=stamp(2 - index),
            transport="http",
            error=None,
            cached=False,
        )
        run = db.start_run("review")
        collector.ingest_response(run, "autoscout24", "search", response, queue=False)
        db.finish_run(run, "complete", {})
    rows = db.conn.execute("SELECT content_hash,raw_path FROM fetches").fetchall()
    assert len(rows) == 2 and rows[0]["content_hash"] != rows[1]["content_hash"]
    for row in rows:
        assert row["raw_path"]
        with gzip.open(db.path.parent / row["raw_path"], "rb") as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == row["content_hash"]
    db.conn.close()


@pytest.mark.parametrize("country", ["NL", "DE"])
@pytest.mark.parametrize("mutation", ["fuel", "country", "damage"])
def test_verified_own_detail_records_negative_facts_without_new_identity(
    tmp_path, mutation, country
):
    c = config()
    c["scope"]["country"] = country
    source = "autoscout24" if country == "NL" else "autoscout24_de"
    ad_url = URL if country == "NL" else URL.replace(".nl/aanbod/", ".de/angebote/")
    db = Database(tmp_path / "market.sqlite3")
    collector = ConfiguredCollector(db, c)
    own = {
        "id": UUID,
        "webPage": ad_url,
        "status": "Active",
        "url": ad_url,
        "location": {"countryCode": country},
        "price": {"priceRaw": 20000, "currency": "EUR"},
        "vehicle": {
            "make": "Toyota",
            "model": "Corolla",
            "motorTypeName": "Corolla",
            "fuelCategory": {"formatted": "Benzine"},
            "transmission": "Automaat",
            "firstRegistrationDateRaw": "2020-01-01",
            "mileageInKmRaw": 70000,
        },
    }
    first = collector._parse(source, native(own), ad_url).listings[0]
    run = db.start_run("search", timestamp=stamp(2))
    listing_id = collector._observe(run, first, stamp(2), key="search")
    db.finish_run(run, "complete", {})
    identity = db.conn.execute("SELECT vehicle_id FROM listings").fetchone()[0]
    changed = copy.deepcopy(own)
    if mutation == "fuel":
        changed["vehicle"]["fuelCategory"]["formatted"] = "Diesel"
    elif mutation == "country":
        changed["location"]["countryCode"] = "DE" if country == "NL" else "NL"
        changed["vehicle"]["vin"] = "WBA1K11060J123456"
        changed["licensePlate"] = "AB123C"
    else:
        changed["vehicle"]["isCurrentlyDamaged"] = True
    collector.detail_jobs = {ad_url: (source, listing_id, first)}
    collector.fetcher.get = lambda url: SimpleNamespace(
        url=url,
        final_url=url,
        status=200,
        body=native(changed),
        timestamp=stamp(1),
        transport="http",
        error=None,
        cached=False,
    )
    run = db.start_run("detail")
    collector.details(run)
    db.finish_run(run, "complete", {})
    assert not collector.failures
    assert db.conn.execute("SELECT error FROM fetches").fetchone()[0] is None
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 1
    assert db.conn.execute("SELECT vehicle_id FROM listings").fetchone()[0] == identity
    market = analyze_market(db.path, country, as_of=stamp())
    if mutation == "damage":
        assert market["priced_ids"] == market["damage_declared_ids"] == 1
        assert not comparable_groups({"NL": market}, minimum=1)
    else:
        assert market["priced_ids"] == 0
        assert db.conn.execute("SELECT scope FROM listings").fetchone()[0] == "excluded"
        if mutation == "country":
            registry = db.conn.execute("SELECT kenteken,vin FROM vehicles").fetchone()
            assert registry["kenteken"] is None and registry["vin"] is None
            ConfiguredCollector(db, c)
    db.conn.close()


def test_same_plate_contradictory_make_holds_price_and_identifier(tmp_path):
    c = config()
    db = Database(tmp_path / "market.sqlite3")
    bind(db, c)
    observe(db, c, publication(kenteken="AB123C"), stamp(3))
    observe(db, c, publication("marktplaats", kenteken="AB123C"), stamp(2))
    peer = publication("marktplaats", {"model_brand": "Honda"}, kenteken="AB123C")
    peer.title = "Honda Civic"
    observe(db, c, peer, stamp(1))
    market = analyze(db)
    assert market["priced_ids"] == market["known_identifier_ids"] == 0
    assert market["provisional_ids"] == 1
    db.conn.close()


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="Unix non-root permissions required",
)
def test_readonly_restore_is_unhealthy_and_daemon_error_is_visible(tmp_path, capsys):
    root = tmp_path / "workspace"
    base = ["--workspace", str(root)]
    assert main(base + ["init", "--make", "Toyota", "--model", "Corolla"]) == 0
    c = json.loads((root / "search.json").read_text())
    db = Database(root / "data/market.sqlite3")
    bind(db, c)
    db.conn.close()
    database_path = root / "data/market.sqlite3"
    original_mode = database_path.stat().st_mode
    database_path.chmod(0o444)
    capsys.readouterr()
    assert main(base + ["doctor"]) == 5
    diagnosis = json.loads(capsys.readouterr().out)
    assert diagnosis["write_permissions"]["database"] is False
    process = subprocess.Popen(
        [sys.executable, "-m", "car_market.portable", *base, "daemon"],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 8
        heartbeat = root / "data/heartbeat.json"
        beat = {}
        while time.monotonic() < deadline:
            if heartbeat.exists():
                beat = json.loads(heartbeat.read_text())
                if beat.get("state") == "error":
                    break
            if process.poll() is not None:
                pytest.fail("Daemon exited before exposing the failed pass")
            time.sleep(0.05)
        assert beat.get("state") == "error", beat
        assert "readonly" in beat["last_error"]
        assert main(base + ["doctor", "--require-daemon"]) == 5
    finally:
        process.terminate()
        process.communicate(timeout=10)
        database_path.chmod(original_mode)
    assert process.returncode == 0
    assert main(base + ["doctor"]) == 0
