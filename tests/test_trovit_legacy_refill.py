"""Private legacy alias refill preserves actual caps, concurrency and durable cursors."""

import copy
import json
from threading import Barrier
from types import SimpleNamespace

import pytest
import test_trovit_detail_backoff as native

from car_market.fetch import Response
from car_market.parse import Listing


def setup(tmp_path, monkeypatch, *, fast=False, workers=1, cache=0, cap=2):
    pair = native.MANIFEST["pairs"][0]
    db, worker, first_id, _, _, _ = native.setup(
        tmp_path,
        monkeypatch,
        pair,
        seed_error=False,
        detail_cache_hours=cache,
        detail_limit=cap,
        detail_batch_limit=cap,
        fast_detail_limit=cap,
        workers=workers,
    )
    worker.fast = fast
    root = db.conn.execute(
        "SELECT fetch_id,run_id FROM observations WHERE listing_id=?", (first_id,)
    ).fetchone()
    listings = {pair["native_id"]: first_id}
    for other in native.MANIFEST["pairs"][1:3]:
        item = native.native_card(other, current=False)
        listings[item.external_id] = db.observe(
            root["run_id"],
            item,
            native.MANIFEST["older_root"]["fetched_at"],
            root["fetch_id"],
            "native_root",
        )
    calls = []

    def get(url):
        calls.append(url)
        own = next(
            pair for pair in native.MANIFEST["pairs"] if pair["native_id"] in url
        )
        capture = own["current"]
        return Response(
            url,
            # Refill is tested with a synthetic uncertain nonchallenge route.
            # Real CAPTCHA routes have their own refusal/admission controls.
            "https://autos.trovit.nl/listing/control." + own["native_id"] + "/",
            200,
            native.body(capture),
            capture["fetched_at"],
            transport="synthetic_nonchallenge_native_body",
        )

    worker.fetcher = SimpleNamespace(get=get)
    return db, worker, listings, calls


def queue_aliases(worker, listings):
    first, second = native.MANIFEST["pairs"][:2]
    for item in [
        native.native_card(first, current=False),
        native.native_card(first, current=True),
        native.native_card(second, current=False),
    ]:
        worker.queue_details(item, listings[item.external_id])


def facts(db):
    return {
        table: [
            tuple(row)
            for row in db.conn.execute("SELECT * FROM " + table + " ORDER BY rowid")
        ]
        for table in [
            "listings",
            "observations",
            "events",
            "field_verifications",
            "query_membership",
        ]
    }


def cursor(db):
    row = db.conn.execute(
        "SELECT value FROM monitor_state WHERE key='detail_queue_cursor'"
    ).fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("workers", [1, 4])
@pytest.mark.parametrize("cache", [0, 24])
def test_reproduced_native_alias_slot_refills_next_publication(
    tmp_path, monkeypatch, fast, workers, cache
):
    db, worker, listings, calls = setup(
        tmp_path, monkeypatch, fast=fast, workers=workers, cache=cache
    )
    queue_aliases(worker, listings)
    before = facts(db)
    captures = native.history(db)["fetches"]
    frontier = copy.deepcopy(worker.detail_jobs)
    run = db.start_run()
    worker.details(run)
    assert len(calls) == 2 and any(
        native.MANIFEST["pairs"][1]["native_id"] in url for url in calls
    )
    assert worker.detail_stats["trovit"]["attempted"] == 2
    assert worker.detail_stats["trovit"]["duplicate_publication_deferred"] == 1
    row = db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    assert row["status"] == "partial" and row["expected_count"] == 3
    assert row["received_count"] == row["pages"] == 2
    assert facts(db) == before and worker.detail_jobs == frontier
    assert native.history(db)["fetches"][: len(captures)] == captures
    db.conn.close()


@pytest.mark.parametrize("cap", [0, 1, 2, 3])
@pytest.mark.parametrize("fast", [False, True])
def test_zero_and_actual_cap_keep_all_native_candidates(
    tmp_path, monkeypatch, cap, fast
):
    db, worker, listings, calls = setup(
        tmp_path, monkeypatch, cap=cap, fast=fast, workers=4
    )
    queue_aliases(worker, listings)
    before = facts(db)
    run = db.start_run()
    worker.details(run)
    assert len(calls) == min(cap, 2)
    assert worker.detail_stats["trovit"]["attempted"] == min(cap, 2)
    assert worker.detail_stats["trovit"].get(
        "duplicate_publication_deferred", 0
    ) == int(cap > 0)
    assert len(worker.detail_jobs) == 3 and facts(db) == before
    row = db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    assert (
        row["received_count"] == row["pages"] == min(cap, 2)
        and row["expected_count"] == 3
    )
    db.conn.close()


@pytest.mark.parametrize("status", [403, 429, None])
@pytest.mark.parametrize("workers", [1, 4])
def test_denied_or_None_slots_refill_other_source_without_charges(
    tmp_path, monkeypatch, status, workers
):
    db, worker, listings, calls = setup(tmp_path, monkeypatch, cap=3, workers=workers)
    # Synthetic source boundaries isolate scheduling; all Trovit requests use native pairs.
    for number in range(2):
        item = Listing(
            "dealer:a-refill.invalid",
            str(number),
            f"https://a-refill.invalid/car/{number}",
            "Scheduling control",
        )
        worker.queue_details(item, None)
    for pair in native.MANIFEST["pairs"][:3]:
        item = native.native_card(pair, current=False)
        worker.queue_details(item, listings[item.external_id])
    native_get = worker.fetcher.get
    extra_calls = []

    def get(url):
        if "a-refill.invalid" not in url:
            return native_get(url)
        extra_calls.append(url)
        if status is None:
            return None
        return Response(
            url, url, status, None, native.CLOCK.isoformat(), "http_" + str(status)
        )

    worker.fetcher.get = get
    before = facts(db)
    original_cursor = {
        "source": "dealer:0.invalid",
        "verification_phase": 4,
        "target_source": "prior-target",
        "verification_source": "prior-pending",
    }
    db.conn.execute(
        "INSERT OR REPLACE INTO monitor_state VALUES ('detail_queue_cursor',?)",
        (json.dumps(original_cursor),),
    )
    db.conn.commit()
    run = db.start_run()
    worker.details(run)
    actual = sum(stats["attempted"] for stats in worker.detail_stats.values())
    source_attempts = worker.detail_stats["dealer:a-refill.invalid"]["attempted"]
    assert actual == 3 and len(calls) == 3 - source_attempts
    if status is None:
        assert source_attempts == 0
    else:
        # Already in-flight requests may finish; later submissions remain refused.
        assert 1 <= source_attempts <= min(2, workers)
    assert (
        worker.detail_stats["dealer:a-refill.invalid"].get("backoff", 0)
        >= 2 - source_attempts
    )
    assert len(extra_calls) <= (1 + max(0, workers - 1) if status else 2)
    saved = cursor(db)
    latest_recorded_source = db.conn.execute(
        "SELECT source FROM fetches ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert saved["source"] == latest_recorded_source
    assert {key: saved[key] for key in original_cursor if key != "source"} == {
        key: value for key, value in original_cursor.items() if key != "source"
    }
    assert facts(db) == before and len(worker.detail_jobs) == 5
    row = db.conn.execute("SELECT * FROM source_runs WHERE run_id=?", (run,)).fetchone()
    assert (
        row["received_count"] == row["pages"] == 3
        and row["expected_count"] == 5
        and row["status"] == "partial"
    )
    db.conn.close()


def test_two_native_publications_remain_parallel_after_alias_dedup(
    tmp_path, monkeypatch
):
    db, worker, listings, calls = setup(tmp_path, monkeypatch, workers=2)
    queue_aliases(worker, listings)
    barrier = Barrier(2, timeout=2)
    get = worker.fetcher.get

    def parallel(url):
        barrier.wait()
        return get(url)

    worker.fetcher.get = parallel
    worker.details(db.start_run())
    assert len(calls) == 2
    db.conn.close()


def test_paid_source_cursor_checkpoint_survives_interrupt_without_phase_reset(
    tmp_path, monkeypatch
):
    db, worker, listings, calls = setup(tmp_path, monkeypatch, workers=1)
    for pair in native.MANIFEST["pairs"][:2]:
        worker.queue_details(
            native.native_card(pair, current=False), listings[pair["native_id"]]
        )
    initial = {
        "source": "dealer:0.invalid",
        "verification_phase": 3,
        "target_source": "kept",
        "verification_source": "kept-too",
    }
    db.conn.execute(
        "INSERT OR REPLACE INTO monitor_state VALUES ('detail_queue_cursor',?)",
        (json.dumps(initial),),
    )
    db.conn.commit()
    get = worker.fetcher.get
    before = facts(db)

    def interrupted(url):
        if calls:
            raise KeyboardInterrupt("Controlled native stop after one paid response")
        return get(url)

    worker.fetcher.get = interrupted
    with pytest.raises(KeyboardInterrupt):
        worker.details(db.start_run())
    saved = cursor(db)
    assert saved == {**initial, "source": "trovit"}
    assert (
        len(calls) == worker.detail_stats["trovit"]["attempted"] == 1
        and facts(db) == before
    )
    db.conn.close()


def test_foreign_current_alias_does_not_borrow_native_publication_dedup(
    tmp_path, monkeypatch
):
    db, worker, listings, calls = setup(tmp_path, monkeypatch, workers=1)
    first = native.MANIFEST["pairs"][0]
    own = native.native_card(first, current=False)
    foreign = native.native_card(first, current=True)
    foreign.raw["market_context_country"] = "BE"
    worker.queue_details(own, listings[own.external_id])
    worker.queue_details(foreign, listings[foreign.external_id])
    before = facts(db)
    worker.details(db.start_run())
    assert len(calls) == worker.detail_stats["trovit"]["attempted"] == 2
    assert worker.detail_stats["trovit"].get("duplicate_publication_deferred", 0) == 0
    assert facts(db) == before and foreign.raw["market_context_country"] == "BE"
    db.conn.close()
