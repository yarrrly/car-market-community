"""Denial and interruption regressions for completed generic detail fairness."""

import copy
import gzip
import hashlib
import json
from pathlib import Path
from threading import Event

import pytest
from test_generic_detail_priority import candidate, queue, settings

from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database, now
from car_market.fetch import Response
from car_market.parse import Listing


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / "market.sqlite3")
    yield value
    value.conn.close()


def cursor(database):
    row = database.conn.execute(
        "SELECT value FROM monitor_state WHERE key='detail_queue_cursor'"
    ).fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("fast", [False, True])
def test_same_pass_denial_refills_owed_confirmation_within_five_attempts(
    db, monkeypatch, status, fast
):
    offered = (
        [candidate(f"a{i}", "a.invalid") for i in range(4)]
        + [candidate(f"b{i}", "b.invalid") for i in range(8)]
        + [
            candidate("ap", "a.invalid", target=False),
            candidate("cp", "c.invalid", target=False),
        ]
    )
    served = []
    for unused in range(2):
        worker = ConfiguredCollector(db, settings(detail_limit=9, detail_cache_hours=0))
        worker.fast = fast
        queue(worker, copy.deepcopy(offered))
        ids = set(worker.detail_jobs)

        def response(url, worker=worker):
            original = worker.detail_jobs[url][2]
            served.append((original.scope, original.source))
            code = status if original.source == "dealer:a.invalid" else 200
            return Response(url, url, code, None, now(), error="controlled_empty")

        monkeypatch.setattr(worker.fetcher, "get", response)
        worker.details(db.start_run())
        assert set(worker.detail_jobs) == ids
    first_confirmation = next(
        (index + 1 for index, (scope, _) in enumerate(served) if scope != "target"),
        None,
    )
    assert first_confirmation is not None and first_confirmation <= 5
    assert sum(source == "dealer:a.invalid" for _, source in served) == 1


@pytest.mark.parametrize("fast", [False, True])
def test_native_stop_after_four_targets_checkpoints_and_restarts_with_confirmation(
    db, monkeypatch, fast
):
    worker = ConfiguredCollector(db, settings(detail_cache_hours=0))
    root = Path(__file__).parent / "fixtures/autoscout_query_binding"
    meta = next(
        row
        for row in json.loads((root / "manifest.json").read_text())
        if row["case"] == "nl-bmw-218i"
    )
    compressed = (root / meta["file"]).read_bytes()
    body = gzip.decompress(compressed)
    assert hashlib.sha256(compressed).hexdigest() == meta["fixture_sha256"]
    assert hashlib.sha256(body).hexdigest() == meta["decoded_sha256"]
    seed = db.start_run()
    fetch = db.save_fetch(
        seed,
        "autoscout24",
        meta["url"],
        meta["final_url"],
        200,
        body.decode(),
        meta["observed_at"],
    )
    native = worker._parse("autoscout24", body.decode(), meta["final_url"])
    for item in native.listings:
        worker._observe(seed, item, meta["observed_at"], fetch)
    original_rows = {
        table: [
            tuple(row) for row in db.conn.execute(f"SELECT * FROM {table} ORDER BY id")
        ]
        for table in ("listings", "observations", "events", "fetches")
    }
    served = []
    for attempt in range(2):
        stop = Event()
        worker = ConfiguredCollector(db, settings(detail_cache_hours=0), stopped=stop)
        for row in db.conn.execute(
            "SELECT l.id,o.details_json FROM listings l "
            "JOIN latest_observations o ON o.listing_id=l.id"
        ):
            worker.queue_details(Listing(**json.loads(row["details_json"])), row["id"])
        assert len(worker.detail_jobs) == 17
        count = 0

        def response(url, worker=worker, stop=stop):
            nonlocal count
            if count == 4:
                stop.set()
                worker.fetcher._check_stopped()
            count += 1
            served.append(worker.detail_jobs[url][2].scope)
            return Response(url, url, 200, None, now(), error="controlled_empty")

        monkeypatch.setattr(worker.fetcher, "get", response)
        with pytest.raises(KeyboardInterrupt):
            worker.run(details=True, checks=False, fast=fast)
        if attempt == 0:
            state = cursor(db)
            assert state and state["verification_phase"] == 4
            assert state["target_source"] == "autoscout24"
    assert served[:5] == ["target"] * 4 + ["needs_confirmation"]
    for table, rows in original_rows.items():
        assert [
            tuple(row) for row in db.conn.execute(f"SELECT * FROM {table} ORDER BY id")
        ][: len(rows)] == rows


@pytest.mark.parametrize("phase", [0, 4])
def test_none_response_does_not_spend_budget_or_owed_confirmation(
    db, monkeypatch, phase
):
    worker = ConfiguredCollector(
        db, settings(detail_limit=6, detail_cache_hours=0, workers=4)
    )
    queue(
        worker,
        [candidate(f"target-{i}") for i in range(8)]
        + [candidate(f"pending-{i}", target=False) for i in range(3)],
    )
    db.conn.execute(
        "INSERT OR REPLACE INTO monitor_state VALUES ('detail_queue_cursor',?)",
        (json.dumps({"verification_phase": phase}),),
    )
    attempted = []
    calls = []

    def response(url):
        calls.append(url)
        if len(calls) == 1:
            return None
        attempted.append(worker.detail_jobs[url][2].scope)
        return Response(url, url, 200, None, now(), error="controlled_empty")

    monkeypatch.setattr(worker.fetcher, "get", response)
    worker.details(db.start_run())
    assert len(attempted) == 6 and len(calls) == 7
    assert "needs_confirmation" in attempted[: 1 if phase == 4 else 5]
    assert db.conn.execute("SELECT count(*) FROM fetches").fetchone()[0] == 6
    assert len(worker.detail_jobs) == 11


@pytest.mark.parametrize("remaining", [0, 2])
@pytest.mark.parametrize("fast", [False, True])
def test_refill_respects_remaining_de_allowance_and_never_charges_none(
    db, monkeypatch, remaining, fast
):
    config = settings(detail_cache_hours=0, workers=4)
    config["scope"]["country"] = "DE"
    worker = ConfiguredCollector(db, config)
    worker.fast = fast
    queue(
        worker,
        [candidate(f"target-{i}", country="DE") for i in range(8)]
        + [candidate(f"pending-{i}", country="DE", target=False) for i in range(3)],
    )
    db.conn.execute(
        "INSERT OR REPLACE INTO monitor_state VALUES ('detail_queue_cursor',?)",
        (json.dumps({"verification_phase": 4}),),
    )
    run = db.start_run()
    policy = worker.specialized_detail_policy(run)
    allowance = 25 if fast else 50
    policy.used = allowance - remaining
    calls = []
    attempts = []

    def response(url):
        calls.append(url)
        if len(calls) == 1:
            return None
        attempts.append(worker.detail_jobs[url][2].scope)
        return Response(url, url, 200, None, now(), error="controlled_empty")

    monkeypatch.setattr(worker.fetcher, "get", response)
    worker.details(run)
    assert len(attempts) == remaining and policy.used == allowance
    assert len(calls) == (3 if remaining else 0)
    assert db.conn.execute("SELECT count(*) FROM fetches").fetchone()[0] == remaining
    if remaining:
        assert attempts[0] == "needs_confirmation"
    else:
        assert cursor(db) == {"verification_phase": 4}
    assert len(worker.detail_jobs) == 11
