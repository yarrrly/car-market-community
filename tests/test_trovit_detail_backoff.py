"""Retained own native Trovit cards, rotated signed requests and durable errors."""

import dataclasses
import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from car_market.collect import Collector, parse_market_page
from car_market.db import Database
from car_market.fetch import Response
from car_market.trovit_detail_policy import in_error_backoff

FIXTURES = Path(__file__).parent / "fixtures/trovit_backoff"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
CLOCK = datetime(2026, 10, 11, 23, 43, 33, tzinfo=UTC)


def body(capture):
    content = (FIXTURES / capture["fixture"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == capture["fixture_gzip_sha256"]
    raw = gzip.decompress(content)
    assert hashlib.sha256(raw).hexdigest() == capture["content_hash"]
    return raw.decode()


def native_card(pair, *, current):
    capture = MANIFEST["current_root" if current else "older_root"]
    page = parse_market_page("trovit", body(capture), capture["final_url"])
    return next(item for item in page.listings if item.external_id == pair["native_id"])


def save(db, run, capture, **changes):
    values = {**capture, **changes}
    return db.save_fetch(
        run,
        values["source"],
        values["url"],
        values["final_url"],
        values["status"],
        body(capture),
        values["fetched_at"],
        "retained_native_offline",
        values["error"],
    )


def history(db):
    return {
        table: [
            tuple(row) for row in db.conn.execute(f"SELECT * FROM {table} ORDER BY id")
        ]
        for table in ["observations", "fetches", "events"]
    }


def setup(tmp_path, monkeypatch, pair, *, seed_error=True, **config):
    clock = {"now": CLOCK}
    monkeypatch.setattr(
        "car_market.trovit_detail_policy.now", lambda: clock["now"].isoformat()
    )
    monkeypatch.setattr("car_market.collect.now", lambda: clock["now"].isoformat())
    monkeypatch.setattr(
        "car_market.collect.datetime",
        SimpleNamespace(
            now=lambda zone: clock["now"], fromisoformat=datetime.fromisoformat
        ),
    )
    db = Database(tmp_path / "market.sqlite3")
    cfg = {
        "scope": {"country": "NL"},
        "detail_cache_hours": 24,
        "detail_error_backoff_hours": 6,
        "detail_limit": 1,
        "detail_batch_limit": 1,
        "workers": 1,
        **config,
    }
    run = db.start_run(timestamp=MANIFEST["older_root"]["fetched_at"])
    root = save(db, run, MANIFEST["older_root"])
    old = native_card(pair, current=False)
    listing = db.observe(
        run, old, MANIFEST["older_root"]["fetched_at"], root, "native_root"
    )
    denied = save(db, run, pair["previous"]) if seed_error else None
    collector = Collector(db, cfg)
    calls = []
    collector.fetcher = SimpleNamespace(
        get=lambda url: (
            calls.append(url)
            or Response(
                url,
                # Synthetic nonchallenge outcome: these scheduling controls
                # require an uncertain parser response, not an access refusal.
                "https://autos.trovit.nl/listing/control." + pair["native_id"] + "/",
                200,
                body(pair["current"]),
                pair["current"]["fetched_at"],
                transport="synthetic_nonchallenge_native_body",
            )
        )
    )
    return db, collector, listing, denied, calls, clock


def reopen(db, collector):
    path = db.path
    db.conn.close()
    db = Database(path)
    current = Collector(db, collector.config)
    current.fetcher = collector.fetcher
    return db, current


def fresh_search(db, pair):
    run = db.start_run(timestamp=MANIFEST["current_root"]["fetched_at"])
    root = save(db, run, MANIFEST["current_root"])
    item = native_card(pair, current=True)
    listing = db.observe(
        run, item, MANIFEST["current_root"]["fetched_at"], root, "native_root"
    )
    return item, listing


@pytest.mark.parametrize("pair", MANIFEST["pairs"], ids=lambda row: row["native_id"])
def test_legacy_native_error_is_found_after_reopen_across_signed_rotation(
    tmp_path, monkeypatch, pair
):
    db, collector, _, denied, calls, _ = setup(tmp_path, monkeypatch, pair)
    db, collector = reopen(db, collector)
    item, listing = fresh_search(db, pair)
    before = history(db)
    collector.queue_details(item, listing)
    run = db.start_run()
    collector.details(run)
    assert not calls
    assert history(db) == before
    state = json.loads(
        db.conn.execute(
            "SELECT value FROM monitor_state WHERE key LIKE 'trovit_detail_attempt:%'"
        ).fetchone()[0]
    )
    assert state == {
        "source": "trovit",
        "external_id": pair["native_id"],
        "fetch_id": denied,
        "content_hash": pair["previous"]["content_hash"],
        "fetched_at": pair["previous"]["fetched_at"],
    }
    row = db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    assert row["status"] == "partial" and row["received_count"] == 0
    assert "1 deferred" in row["error"] and "1 in backoff" in row["error"]
    db.conn.close()


@pytest.mark.parametrize("pair", MANIFEST["pairs"], ids=lambda row: row["native_id"])
def test_new_attempt_reference_preserves_original_clocks_after_reopen(
    tmp_path, monkeypatch, pair
):
    db, collector, listing, _, calls, _ = setup(
        tmp_path, monkeypatch, pair, seed_error=False
    )
    item = dataclasses.replace(
        native_card(pair, current=False), url=pair["previous"]["url"]
    )
    collector.fetcher = SimpleNamespace(
        get=lambda url: (
            calls.append(url)
            or Response(
                url,
                pair["previous"]["final_url"],
                200,
                body(pair["previous"]),
                pair["previous"]["fetched_at"],
                transport="retained_native_offline",
            )
        )
    )
    collector.queue_details(item, listing)
    collector.details(db.start_run())
    assert len(calls) == 1
    reference = db.conn.execute(
        "SELECT value FROM monitor_state WHERE key LIKE 'trovit_detail_attempt:%'"
    ).fetchone()[0]
    db, collector = reopen(db, collector)
    item, listing = fresh_search(db, pair)
    before = history(db)
    collector.queue_details(item, listing)
    collector.details(db.start_run())
    assert len(calls) == 1 and history(db) == before
    assert (
        db.conn.execute(
            "SELECT value FROM monitor_state WHERE key LIKE 'trovit_detail_attempt:%'"
        ).fetchone()[0]
        == reference
    )
    db.conn.close()


@pytest.mark.parametrize(
    "age,blocked",
    [
        (0, True),
        (6 * 3600 - 1, True),
        (6 * 3600, False),
        (6 * 3600 + 1, False),
        (-1, False),
    ],
)
@pytest.mark.parametrize("offset", ["UTC", "offset"])
def test_actual_clock_boundaries_and_future_capture(
    tmp_path, monkeypatch, age, blocked, offset
):
    pair = MANIFEST["pairs"][0]
    db, collector, _, denied, calls, _ = setup(tmp_path, monkeypatch, pair)
    stamp = CLOCK - timedelta(seconds=age)
    if offset == "offset":
        from datetime import timezone

        stamp = stamp.astimezone(timezone(timedelta(hours=2)))
    db.conn.execute(
        "UPDATE fetches SET fetched_at=? WHERE id=?", (stamp.isoformat(), denied)
    )
    db.conn.commit()
    item, listing = fresh_search(db, pair)
    collector.queue_details(item, listing)
    collector.details(db.start_run())
    assert bool(calls) is not blocked
    db.conn.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "gaspedaal"),
        ("final_url", "https://autos.trovit.nl.evil.example/captcha"),
        ("final_url", "https://autos.trovit.nl:8443/captcha"),
        ("final_url", "http://autos.trovit.nl/captcha"),
        ("url", "https://rd.clk.thribee.com/id.OTHER/origin.2/"),
        ("fetched_at", "malformed"),
    ],
)
def test_unrelated_or_malformed_attempt_does_not_create_backoff(
    tmp_path, monkeypatch, field, value
):
    pair = MANIFEST["pairs"][0]
    db, collector, _, denied, calls, _ = setup(tmp_path, monkeypatch, pair)
    db.conn.execute(f"UPDATE fetches SET {field}=? WHERE id=?", (value, denied))
    db.conn.commit()
    item, listing = fresh_search(db, pair)
    collector.queue_details(item, listing)
    collector.details(db.start_run())
    assert len(calls) == 1
    db.conn.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "gaspedaal"),
        ("external_id", "OTHER"),
        ("url", "https://rd.clk.thribee.com/id.OTHER/origin.2/"),
    ],
)
def test_wrong_current_payload_cannot_credit_own_publication_binding(
    tmp_path, monkeypatch, field, value
):
    pair = MANIFEST["pairs"][0]
    db, collector, listing, _, _, _ = setup(tmp_path, monkeypatch, pair)
    row = db.conn.execute(
        "SELECT id,details_json FROM latest_observations WHERE listing_id=?", (listing,)
    ).fetchone()
    payload = json.loads(row["details_json"])
    payload[field] = value
    db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?",
        (json.dumps(payload), row["id"]),
    )
    db.conn.commit()
    item = native_card(pair, current=True)
    assert not in_error_backoff(collector, item.url, item)
    db.conn.close()


@pytest.mark.parametrize("raw_country", ["DE", "BE"])
def test_new_explicit_foreign_context_is_not_inherited_from_old_NL(
    tmp_path, monkeypatch, raw_country
):
    pair = MANIFEST["pairs"][0]
    db, collector, _, _, _, _ = setup(tmp_path, monkeypatch, pair)
    item = native_card(pair, current=True)
    item.raw["market_context_country"] = raw_country
    assert not in_error_backoff(collector, item.url, item)
    db.conn.close()


@pytest.mark.parametrize(
    "state",
    [
        "broken-json",
        "[]",
        '{"source":"trovit","external_id":"31D1-1HF1R1Op","fetch_id":true}',
        '{"source":"trovit","external_id":"31D1-1HF1R1Op","fetch_id":999,"content_hash":"foreign","fetched_at":"future"}',
    ],
)
def test_malformed_reference_recovers_only_from_truthful_native_history(
    tmp_path, monkeypatch, state
):
    pair = MANIFEST["pairs"][0]
    db, collector, _, denied, _, _ = setup(tmp_path, monkeypatch, pair)
    key = (
        "trovit_detail_attempt:"
        + hashlib.sha256(pair["native_id"].encode()).hexdigest()
    )
    db.conn.execute("INSERT INTO monitor_state VALUES (?,?)", (key, state))
    db.conn.commit()
    item = native_card(pair, current=True)
    assert in_error_backoff(collector, item.url, item)
    assert (
        json.loads(
            db.conn.execute(
                "SELECT value FROM monitor_state WHERE key=?", (key,)
            ).fetchone()[0]
        )["fetch_id"]
        == denied
    )
    db.conn.close()


@pytest.mark.parametrize(
    "status,error",
    [(200, None), (200, "detail_unparsed"), (403, "http_403"), (429, "http_429")],
)
def test_newer_terminal_outcome_and_source_revision(
    tmp_path, monkeypatch, status, error
):
    pair = MANIFEST["pairs"][0]
    db, collector, _, _, _, _ = setup(tmp_path, monkeypatch, pair)
    save(
        db,
        db.start_run(),
        pair["previous"],
        status=status,
        error=error,
        # This control tests recoverable parser outcomes on a nonchallenge route.
        # Native CAPTCHA refusal and its original route have separate regressions.
        final_url="https://autos.trovit.nl/listing/owned.ABC/",
        fetched_at=(CLOCK - timedelta(seconds=30)).isoformat(),
    )
    item = native_card(pair, current=True)
    expected = error is not None
    assert in_error_backoff(collector, item.url, item) is expected
    collector.source_parser_refresh_since["trovit"] = (
        CLOCK - timedelta(seconds=10)
    ).isoformat()
    assert in_error_backoff(collector, item.url, item) is (status in (403, 429))
    db.conn.close()


@pytest.mark.parametrize("cache_hours", [0, 24])
@pytest.mark.parametrize("budget", [0, 1])
def test_error_backoff_is_independent_of_cache_and_keeps_zero_budget_frontier(
    tmp_path, monkeypatch, cache_hours, budget
):
    pair = MANIFEST["pairs"][0]
    db, collector, listing, _, calls, _ = setup(
        tmp_path, monkeypatch, pair, detail_cache_hours=cache_hours, detail_limit=budget
    )
    collector.queue_details(native_card(pair, current=True), listing)
    collector.details(db.start_run())
    assert not calls
    assert collector.detail_stats["trovit"]["backoff"] == 1
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 1
    db.conn.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://RD.CLK.THRIBEE.COM/id.31D1-1HF1R1Op/changed-token/",
        "https://rd.clk.thribee.com:443/id.31D1-1HF1R1Op/changed-token/",
    ],
)
def test_equivalent_native_origin_uses_observed_wire_namespace(
    tmp_path, monkeypatch, url
):
    pair = MANIFEST["pairs"][0]
    db, collector, _, _, _, _ = setup(tmp_path, monkeypatch, pair)
    item = dataclasses.replace(native_card(pair, current=True), url=url)
    assert in_error_backoff(collector, url, item)
    db.conn.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://rd.clk.thribee.com/id.31D1-1HF1R1Op/a/",
        "https://rd.clk.thribee.com:0/id.31D1-1HF1R1Op/a/",
        "https://rd.clk.thribee.com:8443/id.31D1-1HF1R1Op/a/",
        "https://rd.clk.thribee.com.evil.example/id.31D1-1HF1R1Op/a/",
        "https://rd.clk.thribee.com@evil.example/id.31D1-1HF1R1Op/a/",
        "https://user@rd.clk.thribee.com/id.31D1-1HF1R1Op/a/",
        "https://rd.clk.thribee.com/id.31D1-1HF1R1OpOTHER/a/",
        "https://rd.clk.thribee.com/id./a/",
        "https://rd.clk.thribee.com/id.31D1%2F1HF1R1Op/a/",
        "https://[broken",
    ],
)
def test_unrelated_namespace_or_malformed_uri_cannot_share_backoff(
    tmp_path, monkeypatch, url
):
    pair = MANIFEST["pairs"][0]
    db, collector, _, _, _, _ = setup(tmp_path, monkeypatch, pair)
    item = dataclasses.replace(native_card(pair, current=True), url=url)
    assert not in_error_backoff(collector, url, item)
    db.conn.close()


@pytest.mark.parametrize("status", [403, 429])
def test_actual_native_tracker_origin_refusal_keeps_source_provenance(
    tmp_path, monkeypatch, status
):
    pair = MANIFEST["pairs"][0]
    db, collector, _, denied, _, _ = setup(tmp_path, monkeypatch, pair)
    db.conn.execute(
        "UPDATE fetches SET source='dealer:rd.clk.thribee.com',final_url=url,status=?,error=? WHERE id=?",
        (status, "http_" + str(status), denied),
    )
    db.conn.commit()
    original = tuple(
        db.conn.execute("SELECT * FROM fetches WHERE id=?", (denied,)).fetchone()
    )
    item = native_card(pair, current=True)
    assert in_error_backoff(collector, item.url, item)
    assert (
        tuple(db.conn.execute("SELECT * FROM fetches WHERE id=?", (denied,)).fetchone())
        == original
    )
    db.conn.close()


@pytest.mark.parametrize("workers", [1, 4])
def test_two_native_aliases_share_one_actual_HTTP_and_honest_pending_slot(
    tmp_path, monkeypatch, workers
):
    pair = MANIFEST["pairs"][0]
    db, collector, listing, _, calls, _ = setup(
        tmp_path,
        monkeypatch,
        pair,
        seed_error=False,
        detail_limit=2,
        detail_batch_limit=2,
        workers=workers,
    )
    old = native_card(pair, current=False)
    new = native_card(pair, current=True)
    assert old.url != new.url and old.external_id == new.external_id
    collector.queue_details(old, listing)
    collector.queue_details(new, listing)
    run = db.start_run()
    collector.details(run)
    assert len(calls) == 1
    row = db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    assert (
        row["status"] == "partial"
        and row["expected_count"] == 2
        and row["received_count"] == 1
    )
    assert "1 deferred" in row["error"]
    assert collector.detail_stats["trovit"]["duplicate_publication_deferred"] == 1
    assert collector.detail_stats["trovit"].get("backoff", 0) == 0
    db.conn.close()


def test_different_verified_publications_keep_existing_parallel_budget(
    tmp_path, monkeypatch
):
    pair = MANIFEST["pairs"][0]
    db, collector, listing, _, calls, _ = setup(
        tmp_path,
        monkeypatch,
        pair,
        seed_error=False,
        detail_limit=2,
        detail_batch_limit=2,
        workers=4,
    )
    second = native_card(MANIFEST["pairs"][1], current=False)
    root = db.conn.execute(
        "SELECT fetch_id,run_id FROM observations WHERE listing_id=?", (listing,)
    ).fetchone()
    second_listing = db.observe(
        root["run_id"],
        second,
        MANIFEST["older_root"]["fetched_at"],
        root["fetch_id"],
        "native_root",
    )
    collector.queue_details(native_card(pair, current=True), listing)
    collector.queue_details(second, second_listing)
    collector.details(db.start_run())
    assert len(calls) == 2
    assert not collector.detail_stats["trovit"].get("duplicate_publication_deferred")
    db.conn.close()


def test_warm_reference_never_rescans_expired_publication_history(
    tmp_path, monkeypatch
):
    pair = MANIFEST["pairs"][0]
    db, collector, _, _, _, _ = setup(tmp_path, monkeypatch, pair)
    item = native_card(pair, current=True)
    statements = []
    db.conn.set_trace_callback(statements.append)
    assert in_error_backoff(collector, item.url, item)
    db.conn.set_trace_callback(None)
    query = next(s for s in statements if s.startswith("SELECT * FROM fetches WHERE ("))
    plan = [r[3] for r in db.conn.execute("EXPLAIN QUERY PLAN " + query)]
    assert any("fetch_url_time" in s and "SEARCH" in s for s in plan), plan
    db.conn.executemany(
        "INSERT INTO fetches(source,url,final_url,fetched_at,status,error) VALUES ('trovit',?,?,'2026-10-10T00:00:00Z',200,'detail_unparsed')",
        [(pair["previous"]["url"], pair["previous"]["final_url"])] * 12000,
    )
    db.conn.commit()
    db, collector = reopen(db, collector)
    statements.clear()
    ticks = [0]
    db.conn.set_trace_callback(statements.append)
    db.conn.set_progress_handler(lambda: ticks.__setitem__(0, ticks[0] + 1) or 0, 10)
    assert in_error_backoff(collector, item.url, item)
    db.conn.set_trace_callback(None)
    db.conn.set_progress_handler(None, 0)
    assert not any(s.startswith("SELECT * FROM fetches WHERE (") for s in statements)
    assert any("SELECT * FROM fetches WHERE id=" in s for s in statements)
    assert ticks[0] * 10 < 2000
    db.conn.close()
