"""Native-derived Quoka frontier retention without current census/membership credit."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from car_market.db import Database
from car_market.fetch import Response
from car_market.mercedes_collect import MercedesCollector
from car_market.model_profiles import MERCEDES_PROFILE

ROOT = Path(__file__).resolve().parents[1] / "fixtures/mercedes_de_sources"
META = next(
    r for r in json.loads((ROOT / "manifest.json").read_text()) if r["fixture"] == "quoka-c180.html"
)
BODY = (ROOT / META["fixture"]).read_text()
SOURCE = "quoka_de"
URL = META["url"]
CLOCK = META["captured_at"]


def setup(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    query = {
        "source": SOURCE,
        "key": "native_frontier",
        "url": URL,
        "max_pages": 1,
        "detail_limit": 0,
    }
    config = {
        "scope": {"country": "DE", "model_profile": MERCEDES_PROFILE},
        "queries": [query],
        "workers": 1,
        "detail_limit": 0,
    }
    return db, MercedesCollector(db, config), query, config


def state(db, query):
    row = db.conn.execute(
        "SELECT value FROM monitor_state WHERE key=?",
        ("model_discovery:" + SOURCE + ":" + query["key"],),
    ).fetchone()
    return json.loads(row[0])


def collect(db, worker, query, blocked=False):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if blocked:
            return Response(url, url, 429, None, CLOCK, "http_429")
        assert url == URL
        return Response(url, META["final_url"], 200, BODY, CLOCK)

    worker.fetcher.get = get
    run = db.start_run("offline-native-frontier")
    worker._collect_secondary(run, query)
    assert calls == [query["url"]]
    return run


def history(db):
    return {
        name: [tuple(r) for r in db.conn.execute("SELECT * FROM " + name + " ORDER BY rowid")]
        for name in [
            "listings",
            "observations",
            "events",
            "field_verifications",
            "query_membership",
        ]
    }


def raw_hashes(db):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in db.raw.glob("*.gz")}


def test_native_interruption_keeps_all_previous_ids_and_original_proofs(tmp_path):
    db, worker, query, _ = setup(tmp_path)
    collect(db, worker, query)
    previous = state(db, query)
    old = {r["external_id"]: r for r in previous["listings"]}
    assert len(old) == 20 and any(r["scope"] == "needs_confirmation" for r in old.values())
    before = history(db)
    raw = raw_hashes(db)
    run = collect(db, worker, query, blocked=True)
    current = state(db, query)
    assert current["listings"] == [] and current["received"] == 0 and current["expected"] is None
    assert current["enumeration_status"] == current["status"] == "partial"
    retained = {r["external_id"]: r for r in current["retained_listings"]}
    assert set(retained) == set(old)
    for eid, row in old.items():
        assert all(retained[eid][k] == v for k, v in row.items())
        assert retained[eid]["discovery_run_id"] == previous["run_id"]
    assert history(db) == before and raw_hashes(db) == raw
    health = db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    assert health["received_count"] == health["target_count"] == 0 and health["status"] == "partial"
    db.conn.close()


def test_restart_and_repeated_empty_partial_keep_old_frontier_without_new_clocks(tmp_path):
    db, worker, query, config = setup(tmp_path)
    collect(db, worker, query)
    collect(db, worker, query, blocked=True)
    previous = state(db, query)
    path = db.path
    before = history(db)
    raw = raw_hashes(db)
    db.conn.close()
    db = Database(path)
    worker = MercedesCollector(db, config)
    for _ in range(2):
        collect(db, worker, query, blocked=True)
        current = state(db, query)
        assert (
            current["retained_listings"] == previous["retained_listings"]
            and current["listings"] == []
        )
    assert history(db) == before and raw_hashes(db) == raw
    db.conn.close()


def test_reappearing_native_ids_leave_retained_lane_and_keep_current_counter(tmp_path):
    db, worker, query, _ = setup(tmp_path)
    collect(db, worker, query)
    collect(db, worker, query, blocked=True)
    collect(db, worker, query)
    current = state(db, query)
    assert len(current["listings"]) == current["received"] == 20
    assert current["expected"] > 20 and current["status"] == "partial"
    assert current["retained_listings"] == []
    db.conn.close()


def test_legacy_v2_pending_rows_restore_after_same_query_429(tmp_path):
    db, worker, query, _ = setup(tmp_path)
    collect(db, worker, query)
    previous = state(db, query)
    previous.pop("query_context", None)
    previous.pop("retained_listings", None)
    previous["schema_version"] = 2
    db.conn.execute("DELETE FROM monitor_state WHERE key LIKE 'model_discovery_retained:%'")
    db.conn.execute(
        "UPDATE monitor_state SET value=? WHERE key=?",
        (json.dumps(previous), "model_discovery:" + SOURCE + ":" + query["key"]),
    )
    db.conn.commit()
    collect(db, worker, query, blocked=True)
    current = state(db, query)
    assert {r["external_id"] for r in current["retained_listings"]} == {
        r["external_id"] for r in previous["listings"]
    }
    assert all(
        r["fetched_at"] == CLOCK and r["country"] is None for r in current["retained_listings"]
    )
    db.conn.close()


@pytest.mark.parametrize("changed", ["commercial", "query_key"])
def test_other_query_context_never_acquires_retained_ids(tmp_path, changed):
    db, worker, query, _ = setup(tmp_path)
    collect(db, worker, query)
    collect(db, worker, query, blocked=True)
    other = copy.deepcopy(query)
    if changed == "commercial":
        other["url"] = URL.replace("commercial=true", "commercial=false")
    else:
        other["key"] = "other_frontier"
    collect(db, worker, other, blocked=True)
    assert state(db, other)["retained_listings"] == []
    db.conn.close()


def test_serialized_frontier_remains_minimal(tmp_path):
    db, worker, query, _ = setup(tmp_path)
    collect(db, worker, query)
    previous = state(db, query)
    for row in previous["listings"]:
        row["raw"] = {"oversized": "x" * 100000}
        row["description"] = "x" * 100000
        for name in ["qualification_generation", "accepted_detail_cache", "discovery_capture"]:
            row[name] = {**(row.get(name) or {}), "raw": {"oversized": "x" * 100000}}
    db.conn.execute(
        "UPDATE monitor_state SET value=? WHERE key=?",
        (
            json.dumps(previous),
            "model_discovery_retained:"
            + SOURCE
            + ":"
            + query["key"]
            + ":"
            + previous["query_context"],
        ),
    )
    db.conn.execute(
        "UPDATE monitor_state SET value=? WHERE key=?",
        (json.dumps(previous), "model_discovery:" + SOURCE + ":" + query["key"]),
    )
    db.conn.commit()
    collect(db, worker, query, blocked=True)
    current = state(db, query)
    assert len(current["retained_listings"]) == 20 and len(json.dumps(current)) < 20000
    assert all("raw" not in r and "description" not in r for r in current["retained_listings"])
    db.conn.close()


def test_legacy_already_empty_partial_recovers_previous_native_capture_without_HTTP(tmp_path):
    db, worker, query, _ = setup(tmp_path)
    first_run = collect(db, worker, query)
    original = state(db, query)
    db.save_fetch(
        first_run,
        SOURCE,
        URL,
        META["final_url"],
        200,
        BODY,
        "2026-10-08T15:30:00+00:00",
        "synthetic-later-duplicate",
        None,
    )
    collect(db, worker, query, blocked=True)
    legacy = state(db, query)
    legacy.pop("query_context", None)
    legacy.pop("retained_listings", None)
    legacy["schema_version"] = 2
    db.conn.execute("DELETE FROM monitor_state WHERE key LIKE 'model_discovery_retained:%'")
    db.conn.execute(
        "UPDATE monitor_state SET value=? WHERE key=?",
        (json.dumps(legacy), "model_discovery:" + SOURCE + ":" + query["key"]),
    )
    db.conn.commit()
    before = history(db)
    raw = raw_hashes(db)
    collect(db, worker, query, blocked=True)
    current = state(db, query)
    assert {r["external_id"] for r in current["retained_listings"]} == {
        r["external_id"] for r in original["listings"]
    }
    assert all(
        r["fetch_id"] == original["listings"][0]["fetch_id"] and r["fetched_at"] == CLOCK
        for r in current["retained_listings"]
    )
    assert current["received"] == 0 and current["expected"] is None and current["listings"] == []
    assert history(db) == before and raw_hashes(db) == raw
    db.conn.close()


def test_filter_context_switch_back_preserves_each_own_frontier(tmp_path):
    db, worker, query, _ = setup(tmp_path)
    collect(db, worker, query)
    old = state(db, query)
    other = {**query, "url": URL.replace("commercial=true", "commercial=false")}
    collect(db, worker, other, blocked=True)
    assert state(db, other)["retained_listings"] == []
    collect(db, worker, query, blocked=True)
    assert {r["external_id"] for r in state(db, query)["retained_listings"]} == {
        r["external_id"] for r in old["listings"]
    }
    assert state(db, query)["received"] == 0
    db.conn.close()


def test_equivalent_query_parameter_order_keeps_context(tmp_path):
    db, worker, query, _ = setup(tmp_path)
    collect(db, worker, query)
    changed = {
        **query,
        "url": URL.replace(
            "q=mercedes+c180+coupe&commercial=true", "commercial=true&q=mercedes+c180+coupe"
        ),
    }
    collect(db, worker, changed, blocked=True)
    assert (
        len(state(db, changed)["retained_listings"]) == 20 and state(db, changed)["received"] == 0
    )
    db.conn.close()


@pytest.mark.parametrize("value", ["null", "[]", '"invalid envelope"', "{invalid"])
def test_malformed_prior_state_stays_partial_without_fabricated_frontier(tmp_path, value):
    db, worker, query, _ = setup(tmp_path)
    db.conn.execute(
        "INSERT INTO monitor_state VALUES (?,?)",
        ("model_discovery:" + SOURCE + ":" + query["key"], value),
    )
    db.conn.commit()
    collect(db, worker, query, blocked=True)
    current = state(db, query)
    assert (
        current["listings"] == current["retained_listings"] == [] and current["status"] == "partial"
    )
    db.conn.close()


@pytest.mark.parametrize("fault", ["clock", "hash", "fetch_type", "source"])
def test_wrong_retained_capture_metadata_is_never_rebound_or_freshened(tmp_path, fault):
    db, worker, query, _ = setup(tmp_path)
    collect(db, worker, query)
    previous = state(db, query)
    row = previous["listings"][0]
    if fault == "clock":
        row["fetched_at"] = "2026-10-11T23:00:00+00:00"
    elif fault == "hash":
        row["content_hash"] = "0" * 64
    elif fault == "fetch_type":
        row["fetch_id"] = {"invalid": 1}
    else:
        row["fetch_id"] = db.save_fetch(
            previous["run_id"],
            "autoscout24_de",
            URL,
            URL,
            200,
            BODY,
            CLOCK,
            "explicit-wrong-source",
            None,
        )
    context_key = (
        "model_discovery_retained:" + SOURCE + ":" + query["key"] + ":" + previous["query_context"]
    )
    db.conn.execute(
        "UPDATE monitor_state SET value=? WHERE key=?", (json.dumps(previous), context_key)
    )
    db.conn.commit()
    before = history(db)
    collect(db, worker, query, blocked=True)
    current = state(db, query)
    assert len(current["retained_listings"]) == 19
    assert row["external_id"] not in {r["external_id"] for r in current["retained_listings"]}
    assert current["received"] == 0 and current["listings"] == [] and current["status"] == "partial"
    assert history(db) == before
    db.conn.close()


@pytest.mark.parametrize("proof", ["future", "foreign"])
def test_opaque_old_future_or_foreign_proof_never_refreshes_current_market(tmp_path, proof):
    db, worker, query, _ = setup(tmp_path)
    collect(db, worker, query)
    previous = state(db, query)
    row = previous["listings"][0]
    # An existing historical presence row makes last_seen preservation non-vacuous.
    db.conn.execute(
        "INSERT INTO listings(source,external_id,url,first_seen,last_seen,scope) VALUES (?,?,?,?,?,?)",
        (SOURCE, row["external_id"], row["url"], CLOCK, CLOCK, "needs_confirmation"),
    )
    if proof == "future":
        stamp = "2999-01-01T00:00:00+00:00"
        row["fetch_id"] = db.save_fetch(
            previous["run_id"], SOURCE, URL, URL, 200, BODY, stamp, "explicit-future-proof", None
        )
        row["fetched_at"] = stamp
    else:
        row["country"] = "NL"
        row["scope"] = "excluded"
    context_key = (
        "model_discovery_retained:" + SOURCE + ":" + query["key"] + ":" + previous["query_context"]
    )
    db.conn.execute(
        "UPDATE monitor_state SET value=? WHERE key=?", (json.dumps(previous), context_key)
    )
    db.conn.commit()
    before = history(db)
    collect(db, worker, query, blocked=True)
    current = state(db, query)
    saved = next(r for r in current["retained_listings"] if r["external_id"] == row["external_id"])
    assert saved["fetch_id"] == row["fetch_id"] and saved["fetched_at"] == row["fetched_at"]
    assert saved["country"] == row["country"]
    assert current["received"] == 0 and current["listings"] == [] and current["status"] == "partial"
    assert history(db) == before
    assert db.conn.execute("SELECT last_seen FROM listings").fetchone()[0] == CLOCK
    assert worker._secondary_detail_attempts == 0
    db.conn.close()


def test_id_absent_from_controlled_complete_census_stays_separate(tmp_path, monkeypatch):
    from car_market.mercedes_de_sources import collect_mercedes_de_source

    db, worker, query, _ = setup(tmp_path)
    collect(db, worker, query)
    previous = state(db, query)
    response = Response(URL, META["final_url"], 200, BODY, CLOCK)
    result = collect_mercedes_de_source(
        lambda *args, **kwargs: response, query, max_pages=1, max_details=0
    )
    dropped = result["listings"].pop(0).external_id
    # Explicit native-layer control: a later census is complete with this one ID absent.
    # This tests retained-state bookkeeping, not the fixture's original175 counter.
    result["expected"] = result["received"] = 19
    result["enumeration_status"] = "complete"
    result["enumeration_errors"] = []
    result["errors"] = []
    result["pages"][0]["total"] = result["pages"][0]["received"] = 19
    monkeypatch.setattr(
        "car_market.mercedes_de_sources.collect_mercedes_de_source",
        lambda *args, **kwargs: copy.deepcopy(result),
    )
    before = history(db)
    worker._collect_secondary(db.start_run("controlled-complete-census"), query)
    current = state(db, query)
    assert current["enumeration_status"] == "complete"
    assert current["received"] == current["expected"] == len(current["listings"]) == 19
    assert {r["external_id"] for r in current["retained_listings"]} == {dropped}
    original = next(r for r in previous["listings"] if r["external_id"] == dropped)
    retained = current["retained_listings"][0]
    assert all(retained[k] == v for k, v in original.items())
    assert history(db) == before
    assert worker._secondary_detail_attempts == 0
    db.conn.close()
