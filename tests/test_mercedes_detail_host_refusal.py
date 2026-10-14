"""Native-derived Mercedes generic VDPs respect durable exact-origin refusals."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from car_market.db import Database
from car_market.fetch import Response
from car_market.mercedes_collect import MercedesCollector
from car_market.model_profiles import MERCEDES_PROFILE

ROOT = Path(__file__).resolve().parents[1] / "fixtures/mercedes_de_sources"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())


def native(name):
    meta = next(row for row in MANIFEST if row["fixture"] == name)
    body = (ROOT / name).read_text()
    assert hashlib.sha256(body.encode()).hexdigest() == meta["fixture_sha256"]
    return body, meta


def setup(tmp_path, **options):
    db = Database(tmp_path / "market.sqlite3")
    config = {
        "scope": {"country": "DE", "model_profile": MERCEDES_PROFILE},
        "queries": [],
        "workers": 1,
        "detail_limit": 50,
        "detail_cache_hours": 6,
        "detail_error_backoff_hours": 6,
        **options,
    }
    worker = MercedesCollector(db, config)
    body, meta = native("quoka-c180.html")
    cards = worker._parse("quoka_de", body, meta["final_url"]).listings
    # Existing native IDs exercise transport refusal independently of qualification.
    assert len(cards) == 20 and any(item.scope == "needs_confirmation" for item in cards)
    return db, worker, cards, config


def queue(worker, cards):
    for item in cards:
        worker.detail_jobs[item.url] = (item.source, None, copy.deepcopy(item))


def history(db):
    return {
        table: [tuple(row) for row in db.conn.execute("SELECT * FROM " + table + " ORDER BY rowid")]
        for table in [
            "listings",
            "observations",
            "events",
            "field_verifications",
            "query_membership",
        ]
    }


def captured(db):
    return [tuple(row) for row in db.conn.execute("SELECT * FROM fetches ORDER BY id")]


def health(db, run):
    return db.conn.execute(
        "SELECT * FROM source_runs WHERE run_id=? AND source='portal_details'", (run,)
    ).fetchone()


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("fast", [False, True])
def test_native_refusal_blocks_generic_phase_and_reopen_without_charging(
    tmp_path, status, reopen, fast
):
    db, worker, cards, config = setup(tmp_path)
    worker._secondary_detail_attempts = 0
    worker._secondary_detail_deferrals = []
    worker._secondary_get = lambda url, **kwargs: Response(
        url,
        url,
        status,
        "Controlled native VDP refusal",
        datetime.now(UTC).isoformat(),
        "http_" + str(status),
    )
    run = db.start_run("native-refusal")
    response = worker._secondary_detail_get(run, "quoka_de", cards[0].url, original=cards[0])
    assert response.status == status and worker._secondary_detail_attempts == 1
    before = captured(db)
    old_history = history(db)
    if reopen:
        db.conn.close()
        db = Database(tmp_path / "market.sqlite3")
        worker = MercedesCollector(db, config)
    worker.fast = fast
    queue(worker, cards[1:4])
    worker._detail_get = lambda url: pytest.fail("Native host refusal must block generic VDPs")
    later = db.start_run("generic-after-native")
    worker.details(later)
    stats = worker.detail_stats["quoka_de"]
    assert stats["attempted"] == stats.get("parsed", 0) == 0 and stats["backoff"] == 3
    assert getattr(worker, "_secondary_detail_attempts", 0) == (0 if reopen else 1)
    assert captured(db) == before and history(db) == old_history
    row = health(db, later)
    assert row["status"] == "partial" and row["received_count"] == row["pages"] == 0
    assert "3 backoff" in row["error"]
    db.conn.close()


@pytest.mark.parametrize("status", [403, 429])
def test_generic_refusal_stops_later_same_origin_and_refills_other_host_budget(
    tmp_path, monkeypatch, status
):
    from car_market import mercedes_collect

    # Force a controlled blocked-before-other-host ordering; production fairness is unchanged.
    monkeypatch.setattr(mercedes_collect, "fair_detail_jobs", lambda jobs: jobs)
    db, worker, cards, _ = setup(tmp_path, detail_limit=2)
    other_body, other_meta = native("pkw-detail-142208737556145407.json")
    other = worker._parse("pkw_de", other_body, other_meta["final_url"]).listings[0]
    queue(worker, cards[:3] + [other])
    calls = []

    def get(url):
        calls.append(url)
        if url == other.url:
            return Response(url, url, 200, other_body, datetime.now(UTC).isoformat())
        return Response(
            url,
            url,
            status,
            "Controlled generic VDP refusal",
            datetime.now(UTC).isoformat(),
            "http_" + str(status),
        )

    worker._detail_get = get
    run = db.start_run("generic-refusal")
    worker.details(run)
    assert calls == [cards[0].url, other.url]
    assert worker.detail_stats["quoka_de"]["attempted"] == 1
    assert worker.detail_stats["quoka_de"]["backoff"] == 2
    assert worker.detail_stats["pkw_de"]["attempted"] == 1
    assert sum(stats["attempted"] for stats in worker.detail_stats.values()) == 2
    row = health(db, run)
    assert row["received_count"] == row["pages"] == 2 and row["status"] == "partial"
    db.conn.close()


@pytest.mark.parametrize(
    "final,age,blocked",
    [
        ("https://www.quoka.de", 1, True),
        ("HTTPS://WWW.QUOKA.DE:0443?denied=1#capture", 1, True),
        ("https://www.quoka.de:443/#blocked", 1, True),
        ("http://www.quoka.de", 1, False),
        ("https://www.quoka.de:8443", 1, False),
        ("https://suche.quoka.de/blocked", 1, False),
        ("https://www.quoka.de.evil.invalid/blocked", 1, False),
        ("https://www.quoka.de:bad/blocked", 1, False),
        ("not a URL", 1, False),
        ("https://www.quoka.de/expired", 361, False),
        ("https://www.quoka.de/future", -5, False),
    ],
)
def test_exact_actual_origin_and_clock_controls_keep_provenance(tmp_path, final, age, blocked):
    db, worker, cards, _ = setup(tmp_path)
    run = db.start_run("redirect-refusal")
    stamp = (
        (datetime.now(UTC) - timedelta(minutes=age))
        .astimezone(timezone(timedelta(hours=2)))
        .isoformat()
    )
    db.save_fetch(
        run,
        "other_logical_source",
        "https://portal.invalid/native-ref",
        final,
        429,
        "Controlled redirect refusal",
        stamp,
        error="http_429",
    )
    # A newer unrelated denial must not hide the older matching origin.
    db.save_fetch(
        run,
        "quoka_de",
        "https://unrelated.invalid/blocked",
        "https://unrelated.invalid/blocked",
        403,
        "Unrelated origin",
        datetime.now(UTC).isoformat(),
        error="http_403",
    )
    before = captured(db)
    old_history = history(db)
    calls = []
    worker._detail_get = lambda url: (
        calls.append(url)
        or Response(
            url, url, 404, "Controlled unavailable VDP", datetime.now(UTC).isoformat(), "http_404"
        )
    )
    queue(worker, cards[:1])
    later = db.start_run("generic-origin-scope")
    worker.details(later)
    assert calls == ([] if blocked else [cards[0].url])
    assert captured(db)[: len(before)] == before and history(db) == old_history
    assert worker.detail_stats["quoka_de"]["attempted"] == (0 if blocked else 1)
    db.conn.close()


def test_native_and_generic_shared_cap_zero_does_not_fetch(tmp_path):
    db, worker, cards, _ = setup(tmp_path, detail_limit=1)
    worker._secondary_detail_attempts = 1
    queue(worker, cards[:3])
    worker._detail_get = lambda url: pytest.fail("Spent native allowance must not fetch")
    run = db.start_run("spent-native-budget")
    worker.details(run)
    assert worker.detail_stats["quoka_de"]["attempted"] == 0
    assert worker.detail_stats["quoka_de"]["deferred"] == 3
    assert captured(db) == [] and health(db, run)["received_count"] == 0
    db.conn.close()


def test_refusal_limits_new_inflight_to_workers(tmp_path):
    db, worker, cards, _ = setup(tmp_path, workers=2)
    queue(worker, cards[:4])
    calls = []
    worker._detail_get = lambda url: (
        calls.append(url)
        or Response(
            url,
            url,
            429,
            "Controlled simultaneous refusals",
            datetime.now(UTC).isoformat(),
            "http_429",
        )
    )
    run = db.start_run("bounded-inflight")
    worker.details(run)
    assert len(calls) == 2 and worker.detail_stats["quoka_de"]["attempted"] == 2
    assert worker.detail_stats["quoka_de"]["backoff"] == 2
    assert health(db, run)["received_count"] == health(db, run)["pages"] == 2
    db.conn.close()


@pytest.mark.parametrize("host_denied", [False, True])
def test_accepted_native_cache_keeps_original_clocks_and_never_charges(tmp_path, host_denied):
    db, worker, _, _ = setup(tmp_path)
    body, meta = native("autohero-c205-detail.html")
    (item,) = worker._parse("autohero_de", body, meta["final_url"]).listings
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    run = db.start_run("accepted-native-cache")
    fetch = db.save_fetch(run, item.source, item.url, meta["final_url"], 200, body, old)
    assert worker._observe(run, copy.deepcopy(item), old, fetch) is not None
    if host_denied:
        db.save_fetch(
            run,
            item.source,
            "https://www.autohero.com/another-vdp",
            "https://www.autohero.com/another-vdp",
            403,
            "Controlled later refusal",
            datetime.now(UTC).isoformat(),
            error="http_403",
        )
    before = captured(db)
    old_history = history(db)
    queue(worker, [item])
    worker._detail_get = lambda url: pytest.fail("Fresh accepted cache must not fetch")
    worker.details(db.start_run("generic-cache"))
    assert captured(db) == before and history(db) == old_history
    assert worker.detail_stats[item.source]["attempted"] == 0
    assert worker.detail_stats[item.source].get("backoff", 0) == int(host_denied)
    assert worker.detail_stats[item.source].get("cached", 0) == int(not host_denied)
    db.conn.close()


def test_explicit_cap_zero_keeps_candidates_and_metadata(tmp_path):
    db, worker, cards, _ = setup(tmp_path, detail_limit=0)
    queue(worker, cards[:3])
    before = copy.deepcopy(worker.detail_jobs)
    worker._detail_get = lambda url: pytest.fail("Cap zero must not fetch")
    run = db.start_run("cap-zero")
    worker.details(run)
    assert worker.detail_jobs == before and captured(db) == []
    assert worker.detail_stats["quoka_de"]["attempted"] == 0
    assert worker.detail_stats["quoka_de"]["deferred"] == 3
    assert health(db, run)["received_count"] == health(db, run)["pages"] == 0
    db.conn.close()


def test_generic_host_lookup_uses_only_recent_indexed_refusals(tmp_path):
    db, worker, cards, _ = setup(tmp_path)
    run = db.start_run("indexed-native-host")
    clock = datetime.now(UTC)
    expired = (clock - timedelta(days=3)).isoformat()
    db.conn.executemany(
        "INSERT INTO fetches(run_id,source,url,final_url,fetched_at,status) VALUES (?,?,?,?,?,?)",
        [(run, "quoka_de", "https://www.quoka.de/old", "https://www.quoka.de/old", expired, 429)]
        * 12000
        + [
            (
                run,
                "quoka_de",
                "https://www.quoka.de/ok",
                "https://www.quoka.de/ok",
                clock.isoformat(),
                200,
            )
        ]
        * 12000,
    )
    db.save_fetch(
        run,
        "another_logical_source",
        "https://portal.invalid/ref",
        "https://www.quoka.de",
        403,
        "Controlled recent native refusal",
        (clock - timedelta(minutes=1)).isoformat(),
        error="http_403",
    )
    db.conn.commit()
    before = captured(db)
    real = db.conn
    plans = []

    class ObservedConnection:
        def execute(self, sql, args=()):
            if "status IN (403,429)" in sql:
                plans.extend(row[3] for row in real.execute("EXPLAIN QUERY PLAN " + sql, args))
            return real.execute(sql, args)

        def __getattr__(self, name):
            return getattr(real, name)

    db.conn = ObservedConnection()
    queue(worker, cards[:1])
    worker._detail_get = lambda url: pytest.fail("Recent origin refusal must not fetch")
    worker.details(db.start_run("indexed-generic-host"))
    assert plans and all(
        "SEARCH fetches USING INDEX fetch_blocked_actual_time" in plan for plan in plans
    )
    assert not any("TEMP B-TREE" in plan or "SCAN" in plan for plan in plans)
    assert captured(db) == before and worker.detail_stats["quoka_de"]["attempted"] == 0
    db.conn.close()
