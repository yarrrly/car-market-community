"""TRI01: logical source denial cannot suppress an unrequested native sibling."""

import copy
import dataclasses
import json

import pytest
import test_trovit_detail_backoff as native
import test_trovit_legacy_refill as refill

from car_market.fetch import Response
from car_market.parse import Listing


def shared_jobs(worker, ids):
    first, second = native.MANIFEST["pairs"][:2]
    wrapped = dataclasses.replace(
        native.native_card(first, current=True), source="gaspedaal"
    )
    wrapped.raw = copy.deepcopy(wrapped.raw)
    own_b = native.native_card(second, current=False)
    own_a = native.native_card(first, current=False)
    worker.detail_jobs[wrapped.url] = ("portal", ids[first["native_id"]], wrapped)
    worker.detail_jobs[own_b.url] = ("portal", ids[second["native_id"]], own_b)
    worker.detail_jobs[own_a.url] = ("portal", ids[first["native_id"]], own_a)
    return wrapped, own_b, own_a


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("fast", [False, True])
def test_independent_TRI01_promotes_allowed_native_sibling_before_any_HTTP(
    tmp_path, monkeypatch, status, fast
):
    db, worker, ids, calls = refill.setup(
        tmp_path, monkeypatch, cap=3, workers=1, fast=fast
    )
    # The portal association/denied host are synthetic. Every family key still
    # binds an actual native Trovit publication/root/capture/ID.
    denied = Listing(
        "gaspedaal",
        "routing-denial-control",
        "https://denied.invalid/ad",
        "Scheduling control",
    )
    worker.detail_jobs[denied.url] = ("portal", None, denied)
    wrapped, own_b, own_a = shared_jobs(worker, ids)
    before = refill.facts(db)
    original_fetches = native.history(db)["fetches"]
    native_get = worker.fetcher.get
    attempts = []

    def get(url):
        attempts.append(url)
        if url == denied.url:
            return Response(
                url, url, status, None, native.CLOCK.isoformat(), "http_" + str(status)
            )
        return native_get(url)

    worker.fetcher.get = get
    run = db.start_run()
    worker.details(run)
    assert attempts == [denied.url, own_b.url, own_a.url]
    assert wrapped.url not in attempts and calls == [own_b.url, own_a.url]
    assert (
        worker.detail_stats["gaspedaal"]["attempted"] == 1
        and worker.detail_stats["gaspedaal"]["backoff"] == 1
    )
    assert (
        worker.detail_stats["trovit"]["attempted"] == 2
        and worker.detail_stats["trovit"].get("backoff", 0) == 0
    )
    health = db.conn.execute(
        "SELECT * FROM source_runs WHERE run_id=?", (run,)
    ).fetchone()
    assert (
        health["received_count"] == health["pages"] == 3
        and health["expected_count"] == 4
        and health["status"] == "partial"
    )
    latest = db.conn.execute(
        "SELECT * FROM fetches ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert latest["url"] == own_a.url and latest["source"] == "trovit"
    assert refill.cursor(db)["source"] == "trovit"
    assert refill.facts(db) == before and len(worker.detail_jobs) == 4
    assert native.history(db)["fetches"][: len(original_fetches)] == original_fetches
    db.conn.close()


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("fast", [False, True])
def test_real_native_refusal_never_retries_allowed_logical_alias_in_pass_or_reopen(
    tmp_path, monkeypatch, status, fast
):
    db, worker, ids, _ = refill.setup(
        tmp_path, monkeypatch, cap=3, workers=1, fast=fast
    )
    first = native.MANIFEST["pairs"][0]
    wrapped = dataclasses.replace(
        native.native_card(first, current=True), source="gaspedaal"
    )
    wrapped.raw = copy.deepcopy(wrapped.raw)
    own = native.native_card(first, current=False)
    worker.detail_jobs[wrapped.url] = ("portal", ids[first["native_id"]], wrapped)
    worker.detail_jobs[own.url] = ("portal", ids[first["native_id"]], own)
    attempts = []

    def denied(url):
        attempts.append(url)
        return Response(
            url,
            "https://autos.trovit.nl/captcha",
            status,
            None,
            native.CLOCK.isoformat(),
            "http_" + str(status),
        )

    worker.fetcher.get = denied
    before = refill.facts(db)
    worker.details(db.start_run())
    assert attempts == [wrapped.url]
    assert (
        worker.detail_stats["gaspedaal"]["attempted"] == 1
        and worker.detail_stats["trovit"]["attempted"] == 0
    )
    assert worker.detail_stats["trovit"]["backoff"] == 1 and refill.facts(db) == before
    capture = db.conn.execute(
        "SELECT * FROM fetches WHERE status=? ORDER BY id DESC LIMIT 1", (status,)
    ).fetchone()
    original_capture = tuple(capture)
    reference = db.conn.execute(
        "SELECT value FROM monitor_state WHERE key LIKE 'trovit_detail_attempt:%'"
    ).fetchone()[0]
    assert json.loads(reference)["fetch_id"] == capture["id"]
    db, worker = native.reopen(db, worker)
    worker.fast = fast
    worker.detail_jobs[own.url] = ("portal", ids[first["native_id"]], own)
    worker.detail_jobs[wrapped.url] = ("portal", ids[first["native_id"]], wrapped)
    worker.details(db.start_run())
    assert (
        attempts == [wrapped.url]
        and sum(stat["attempted"] for stat in worker.detail_stats.values()) == 0
    )
    assert (
        tuple(
            db.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (capture["id"],)
            ).fetchone()
        )
        == original_capture
    )
    assert (
        db.conn.execute(
            "SELECT value FROM monitor_state WHERE key LIKE 'trovit_detail_attempt:%'"
        ).fetchone()[0]
        == reference
    )
    assert refill.facts(db) == before
    db.conn.close()


@pytest.mark.parametrize("fast", [False, True])
def test_getter_None_does_not_promote_after_native_request_boundary(
    tmp_path, monkeypatch, fast
):
    db, worker, ids, _ = refill.setup(
        tmp_path, monkeypatch, cap=2, workers=1, fast=fast
    )
    first = native.MANIFEST["pairs"][0]
    wrapped = dataclasses.replace(
        native.native_card(first, current=True), source="gaspedaal"
    )
    wrapped.raw = copy.deepcopy(wrapped.raw)
    own = native.native_card(first, current=False)
    worker.detail_jobs[wrapped.url] = ("portal", ids[first["native_id"]], wrapped)
    worker.detail_jobs[own.url] = ("portal", ids[first["native_id"]], own)
    attempts = []
    worker.fetcher.get = lambda url: attempts.append(url) or None
    before = native.history(db)
    worker.details(db.start_run())
    assert (
        attempts == [wrapped.url]
        and sum(stat["attempted"] for stat in worker.detail_stats.values()) == 0
    )
    assert native.history(db) == before and len(worker.detail_jobs) == 2
    db.conn.close()


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("fast", [False, True])
def test_promoted_native_denial_has_original_own_capture_and_blocks_reopen(
    tmp_path, monkeypatch, status, fast
):
    db, worker, ids, _ = refill.setup(
        tmp_path, monkeypatch, cap=3, workers=1, fast=fast
    )
    routing = Listing(
        "gaspedaal", "routing", "https://denied.invalid/ad", "Scheduling control"
    )
    worker.detail_jobs[routing.url] = ("portal", None, routing)
    wrapped, own_b, own_a = shared_jobs(worker, ids)
    attempts = []
    native_get = worker.fetcher.get

    def get(url):
        attempts.append(url)
        if url == routing.url:
            return Response(
                url, url, status, None, native.CLOCK.isoformat(), "http_" + str(status)
            )
        if url == own_a.url:
            return Response(
                url,
                "https://autos.trovit.nl/captcha",
                status,
                None,
                native.CLOCK.isoformat(),
                "http_" + str(status),
            )
        return native_get(url)

    worker.fetcher.get = get
    facts = refill.facts(db)
    worker.details(db.start_run())
    assert attempts == [routing.url, own_b.url, own_a.url]
    capture = db.conn.execute(
        "SELECT * FROM fetches WHERE url=? ORDER BY id DESC LIMIT 1", (own_a.url,)
    ).fetchone()
    assert capture["source"] == "trovit" and capture["status"] == status
    assert capture["fetched_at"] == native.CLOCK.isoformat()
    original = tuple(capture)
    assert refill.facts(db) == facts
    db, worker = native.reopen(db, worker)
    worker.fast = fast
    worker.detail_jobs[wrapped.url] = ("portal", ids[own_a.external_id], wrapped)
    worker.detail_jobs[own_a.url] = ("portal", ids[own_a.external_id], own_a)
    worker.details(db.start_run())
    assert attempts == [routing.url, own_b.url, own_a.url]
    assert sum(stat["attempted"] for stat in worker.detail_stats.values()) == 0
    assert (
        tuple(
            db.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (capture["id"],)
            ).fetchone()
        )
        == original
    )
    assert refill.facts(db) == facts
    db.conn.close()
