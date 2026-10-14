"""The native BMW Wiveda pair belongs to one queued own publication."""

import copy
import gzip
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace

import pytest

from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.germany_dealer_inventory import WIVEDA_SOURCE, parse_wiveda_bundle
from car_market.parse import Listing

FIXTURES = Path(__file__).parent / "fixtures" / "wiveda_native_text"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
NOW = datetime.fromisoformat(MANIFEST[1]["fetched_at"]) + timedelta(seconds=1)


def retained(meta):
    packed = (FIXTURES / meta["fixture"]).read_bytes()
    assert hashlib.sha256(packed).hexdigest() == meta["fixture_sha256"]
    decoded = gzip.decompress(packed)
    assert hashlib.sha256(decoded).hexdigest() == meta["decoded_sha256"]
    return decoded.decode()


def native():
    html, payload = MANIFEST
    return {
        "kind": "wiveda_native",
        "html_url": html["public_url"],
        "html": retained(html),
        "payload_url": payload["public_url"],
        "payload": json.loads(retained(payload)),
    }


def history(db):
    return {
        name: [
            tuple(row)
            for row in db.conn.execute("SELECT * FROM " + name + " ORDER BY rowid")
        ]
        for name in ["observations", "events", "fetches"]
    }


def setup(tmp_path, monkeypatch, *, cap=2, fast=False, workers=1):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr("car_market.collect.datetime", Clock)
    monkeypatch.setattr("car_market.collect.now", lambda: NOW.isoformat())
    monkeypatch.setattr("car_market.db.now", lambda: NOW.isoformat())
    monkeypatch.setattr("car_market.bmw_detail_policy.now", lambda: NOW.isoformat())
    config = {
        "scope": {"country": "DE"},
        "queries": [],
        "workers": workers,
        "detail_cache_hours": 6,
        "detail_error_backoff_hours": 6,
        "detail_limit": cap,
        "detail_batch_limit": cap,
        "fast_detail_limit": cap,
    }
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, config)
    worker.fast = fast
    item = parse_wiveda_bundle(native(), MANIFEST[0]["public_url"]).listings[0]
    # A controlled pending projection of the retained own record has no accepted
    # fetch proof. It must acquire a real bound detail proof or remain pending.
    item.raw["detail_complete"] = False
    listing_id = worker._observe(
        db.start_run(), item, MANIFEST[0]["fetched_at"], None, "pending_projection"
    )
    worker.queue_details(item, listing_id)
    calls = []
    writer = get_ident()
    fail = {}

    def get(url, **kwargs):
        assert get_ident() == writer
        calls.append(url)
        meta = next(m for m in MANIFEST if m["public_url"] == url)
        status = fail.get(url, 200)
        return Response(
            url,
            url,
            status,
            retained(meta),
            meta["fetched_at"],
            error=None if status == 200 else "http_" + str(status),
            transport="native_fixture_offline",
        )

    worker.fetcher = SimpleNamespace(get=get)
    return db, worker, item, listing_id, calls, fail


def portal(db, run):
    return db.conn.execute(
        "SELECT * FROM source_runs WHERE run_id=? AND source='portal_details'", (run,)
    ).fetchone()


@pytest.mark.parametrize("workers", [1, 4])
def test_generic_owns_native_pair_on_writer_with_original_payload_clock(
    tmp_path, monkeypatch, workers
):
    db, worker, item, _, calls, _ = setup(tmp_path, monkeypatch, workers=workers)
    before = history(db)
    run = db.start_run()
    worker.details(run)
    assert calls == [m["public_url"] for m in MANIFEST]
    assert worker.specialized_detail_policy(run).used == 2
    stats = worker.detail_stats[WIVEDA_SOURCE]
    assert stats["attempted"] == 2 and stats["parsed"] == 1 and stats["errors"] == 0
    for table, rows in before.items():
        assert history(db)[table][: len(rows)] == rows
    own = db.conn.execute(
        "SELECT o.observed_at,o.fetch_id,o.details_json,f.fetched_at,f.transport FROM observations o JOIN fetches f ON f.id=o.fetch_id ORDER BY o.id DESC LIMIT 1"
    ).fetchone()
    assert own["observed_at"] == own["fetched_at"] == MANIFEST[1]["fetched_at"]
    details = json.loads(own["details_json"])
    assert (
        details["source"] == item.source and details["external_id"] == item.external_id
    )
    assert details["raw"]["market_country"] == "DE"
    assert details["raw"]["native_vehicle"]["brand"] == "BMW"
    captures = db.conn.execute(
        "SELECT fetched_at,status,error FROM fetches WHERE run_id=? ORDER BY id", (run,)
    ).fetchall()
    assert [r["fetched_at"] for r in captures] == [
        MANIFEST[0]["fetched_at"],
        MANIFEST[1]["fetched_at"],
        MANIFEST[1]["fetched_at"],
    ]
    assert all(r["status"] == 200 and r["error"] is None for r in captures)
    row = portal(db, run)
    assert (
        row["status"] == "complete"
        and row["received_count"] == row["expected_count"] == 1
    )
    assert row["pages"] == 2


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("cap", [0, 1, 2])
def test_native_pair_uses_two_actual_slots_or_retains_pending(
    tmp_path, monkeypatch, fast, cap
):
    db, worker, _, _, calls, _ = setup(tmp_path, monkeypatch, cap=cap, fast=fast)
    jobs = copy.deepcopy(worker.detail_jobs)
    old = history(db)
    run = db.start_run()
    worker.details(run)
    assert (
        len(calls)
        == worker.specialized_detail_policy(run).used
        == (2 if cap == 2 else 0)
    )
    assert worker.detail_jobs == jobs
    if cap < 2:
        assert history(db) == old
        assert portal(db, run)["status"] == "partial"
        assert worker.detail_stats[WIVEDA_SOURCE]["deferred"] == 1


def test_shared_prior_use_cannot_spend_the_last_half_pair(tmp_path, monkeypatch):
    db, worker, _, _, calls, _ = setup(tmp_path, monkeypatch, cap=3)
    run = db.start_run()
    worker.specialized_detail_policy(run).used = 2
    worker.details(run)
    assert calls == [] and worker.specialized_detail_policy(run).used == 2
    assert portal(db, run)["status"] == "partial"


@pytest.mark.parametrize(
    "fault",
    [
        "queued_source",
        "requested_UUID",
        "external_id",
        "item_source",
        "item_url",
        "stock_id",
        "registry_id",
    ],
)
def test_unrelated_queued_ownership_is_held_before_get(tmp_path, monkeypatch, fault):
    db, worker, item, listing_id, calls, _ = setup(tmp_path, monkeypatch)
    url, source = item.url, item.source
    if fault == "queued_source":
        source = "portal"
    elif fault == "requested_UUID":
        url = url.replace("6e3c5760", "7e3c5760")
    elif fault == "external_id":
        item.external_id = "wrong-native-publication"
    elif fault == "item_source":
        item.source = "dealer:other.invalid"
    elif fault == "item_url":
        item.url = (
            "https://unrelated.invalid/fahrzeug/6e3c5760-ce76-4ffa-b247-30a3300912e8"
        )
    elif fault == "stock_id":
        item.raw["dealer_stock_id"] = "7e3c5760-ce76-4ffa-b247-30a3300912e8"
    else:
        listing_id += 1000
    worker.detail_jobs = {url: (source, listing_id, item)}
    before = history(db)
    run = db.start_run()
    worker.details(run)
    assert calls == [] and history(db) == before
    assert portal(db, run)["status"] == "partial"
    assert worker.detail_stats[item.source]["attempted"] == 0


@pytest.mark.parametrize(
    "case",
    ["no_details", "conclusive_exclusion", "host403", "host429", "old_unparsed_shell"],
)
def test_existing_refusal_flags_and_error_clock_still_hold(tmp_path, monkeypatch, case):
    db, worker, item, _, calls, _ = setup(tmp_path, monkeypatch)
    if case == "no_details":
        worker.specialized_details_enabled = False
    elif case == "conclusive_exclusion":
        item.scope = "excluded"
        item.raw["market_country"] = "BE"
    else:
        status = 200 if case == "old_unparsed_shell" else int(case[-3:])
        error = "detail_unparsed" if status == 200 else "http_" + str(status)
        response = Response(
            item.url,
            item.url if status == 200 else "https://wiveda.de",
            status,
            retained(MANIFEST[0]),
            MANIFEST[0]["fetched_at"],
            error=error,
        )
        worker.fetch_record(
            db.start_run(),
            WIVEDA_SOURCE if status == 200 else "controlled_logical_source",
            response,
        )
    before = history(db)
    run = db.start_run()
    worker.details(run)
    assert calls == [] and history(db) == before
    assert portal(db, run)["status"] == "partial"


def test_restart_cap0_accepted_cache_keeps_original_id_clock_and_all_history(
    tmp_path, monkeypatch
):
    db, worker, _, _, calls, _ = setup(tmp_path, monkeypatch)
    run = db.start_run()
    worker.details(run)
    before, jobs = history(db), copy.deepcopy(worker.detail_jobs)
    seen = [tuple(r) for r in db.conn.execute("SELECT id,last_seen FROM listings")]
    db.conn.close()
    reopened = Database(tmp_path / "market.sqlite3")
    worker = Collector(reopened, {**worker.config, "detail_limit": 0})
    worker.detail_jobs = jobs
    worker.fetcher = SimpleNamespace(
        get=lambda url, **kwargs: pytest.fail("Accepted cache is free")
    )
    run = reopened.start_run()
    worker.details(run)
    assert (
        history(reopened) == before
        and [
            tuple(r) for r in reopened.conn.execute("SELECT id,last_seen FROM listings")
        ]
        == seen
    )
    assert worker.detail_stats[WIVEDA_SOURCE]["cached"] == 1
    assert worker.detail_stats[WIVEDA_SOURCE]["attempted"] == 0
    assert portal(reopened, run)["status"] == "complete"
    assert (
        portal(reopened, run)["received_count"] == 1
        and portal(reopened, run)["pages"] == 0
    )
    assert len(calls) == 2


@pytest.mark.parametrize(
    "field", ["price", "vin", "parser_revision", "global_parser_revision"]
)
def test_current_own_facts_or_revision_cannot_be_replaced_by_old_cache_at_cap0(
    tmp_path, monkeypatch, field
):
    db, worker, item, listing_id, _, _ = setup(tmp_path, monkeypatch)
    worker.details(db.start_run())
    if field == "parser_revision":
        worker.source_parser_refresh_since[WIVEDA_SOURCE] = NOW.isoformat()
    elif field == "global_parser_revision":
        worker.parser_refresh_since = NOW.isoformat()
    else:
        item = copy.deepcopy(item)
        setattr(
            item, field, item.price + 100 if field == "price" else "WBA21BX0407T55765"
        )
        if field == "price":
            worker._observe(
                db.start_run(), item, NOW.isoformat(), None, "new_pending_projection"
            )
        # The original identity is immutable: a conflicting incoming queued VIN
        # stays unresolved rather than rewriting the stored root in this control.
        worker.queue_details(item, listing_id)
    worker.config["detail_limit"] = 0
    worker.fetcher = SimpleNamespace(
        get=lambda url, **kwargs: pytest.fail("No paid quota")
    )
    before = history(db)
    worker.details(run := db.start_run())
    assert history(db) == before and portal(db, run)["status"] == "partial"
    assert worker.detail_stats[WIVEDA_SOURCE]["deferred"] == 1


@pytest.mark.parametrize("workers", [1, 4])
@pytest.mark.parametrize("native_first", [False, True])
def test_mixed_source_batches_share_actual_slots_and_free_cache_tail(
    tmp_path, monkeypatch, workers, native_first
):
    db, worker, _, _, calls, _ = setup(tmp_path, monkeypatch, cap=3, workers=workers)
    native_get = worker.fetcher.get
    host = "zz-paid.invalid" if native_first else "a-paid.invalid"
    extra = Listing(
        "dealer:" + host,
        "one",
        "https://" + host + "/car/one",
        "Controlled scheduling job",
    )
    worker.queue_details(extra, None)
    generic_calls = []

    def get(url, **kwargs):
        if "wiveda.de" in url:
            return native_get(url, **kwargs)
        generic_calls.append(url)
        return Response(url, url, 404, None, NOW.isoformat(), error="http_404")

    worker.fetcher = SimpleNamespace(get=get)
    worker.details(run := db.start_run())
    assert len(calls) == 2 and len(generic_calls) == 1
    assert (
        worker.specialized_detail_policy(run).used == 3
        and portal(db, run)["pages"] == 3
    )
    # The earlier generic request exhausts paid quota before a cached native tail.
    worker = Collector(db, {**worker.config, "detail_limit": 1})
    worker.queue_details(extra, None)
    item = parse_wiveda_bundle(native(), MANIFEST[0]["public_url"]).listings[0]
    listing_id = db.conn.execute(
        "SELECT id FROM listings WHERE source=?", (WIVEDA_SOURCE,)
    ).fetchone()[0]
    worker.queue_details(item, listing_id)

    # Make the earlier generic failure older than the strict age>0 boundary.
    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            later = NOW + timedelta(seconds=1)
            return later if tz else later.replace(tzinfo=None)

    monkeypatch.setattr("car_market.collect.datetime", LaterClock)
    worker.config["detail_error_backoff_hours"] = 0
    worker.fetcher = SimpleNamespace(get=get)
    worker.details(run := db.start_run())
    assert worker.detail_stats[WIVEDA_SOURCE]["cached"] == 1
    assert len(calls) == 2 and worker.specialized_detail_policy(run).used == 1


@pytest.mark.parametrize("status", [403, 429])
def test_real_pair_refusal_is_retained_and_suppresses_restart(
    tmp_path, monkeypatch, status
):
    db, worker, _, _, calls, fail = setup(tmp_path, monkeypatch)
    fail[MANIFEST[1]["public_url"]] = status
    worker.details(run := db.start_run())
    assert len(calls) == worker.specialized_detail_policy(run).used == 2
    row = db.conn.execute("SELECT * FROM fetches WHERE status=?", (status,)).fetchone()
    assert (
        row
        and row["fetched_at"] == MANIFEST[1]["fetched_at"]
        and row["error"] == "http_" + str(status)
    )
    before = history(db)
    worker = Collector(db, worker.config)
    item = parse_wiveda_bundle(native(), MANIFEST[0]["public_url"]).listings[0]
    worker.queue_details(item, db.conn.execute("SELECT id FROM listings").fetchone()[0])
    worker.fetcher = SimpleNamespace(
        get=lambda url, **kwargs: pytest.fail("Actual origin refusal held")
    )
    worker.details(db.start_run())
    assert history(db) == before and worker.detail_stats[WIVEDA_SOURCE]["backoff"] == 1


@pytest.mark.parametrize(
    "fault",
    [
        "country",
        "native_UUID",
        "malformed_payload",
        "future_payload_clock",
        "login_HTML",
        "payload_redirect",
    ],
)
def test_failed_native_components_never_become_accepted_facts(
    tmp_path, monkeypatch, fault
):
    db, worker, _, _, calls, _ = setup(tmp_path, monkeypatch)
    getter = worker.fetcher.get
    before = history(db)

    def get(url, **kwargs):
        response = getter(url, **kwargs)
        if fault == "login_HTML" and url == MANIFEST[0]["public_url"]:
            response.body = "<title>Login</title>" + response.body
        if url == MANIFEST[1]["public_url"]:
            if fault in {"country", "native_UUID"}:
                from test_provider_owned_detail_bindings import native_field

                bundle = native()
                native_field(
                    bundle,
                    "country" if fault == "country" else "id",
                    "NL"
                    if fault == "country"
                    else "7e3c5760-ce76-4ffa-b247-30a3300912e8",
                )
                response.body = json.dumps(bundle["payload"])
            elif fault == "malformed_payload":
                response.body = "{not JSON"
            elif fault == "future_payload_clock":
                response.timestamp = (NOW + timedelta(days=1)).isoformat()
            elif fault == "payload_redirect":
                response.final_url = "https://unrelated.invalid/_payload.json"
        return response

    worker.fetcher = SimpleNamespace(get=get)
    worker.details(run := db.start_run())
    assert history(db)["observations"] == before["observations"]
    assert history(db)["events"] == before["events"]
    assert (
        portal(db, run)["status"] == "partial"
        and portal(db, run)["received_count"] == 0
    )
    assert len(calls) == (1 if fault == "login_HTML" else 2)
    assert worker.specialized_detail_policy(run).used == len(calls)


def test_later_same_SHA_200_without_accepted_observation_is_not_old_accepted_cache(
    tmp_path, monkeypatch
):
    db, worker, item, _, _, _ = setup(tmp_path, monkeypatch)
    worker.details(db.start_run())
    last = db.conn.execute(
        "SELECT * FROM fetches WHERE transport='ordinary_HTTP_own_HTML_payload_bundle'"
    ).fetchone()
    packed = (db.path.parent / last["raw_path"]).read_bytes()
    body = gzip.decompress(packed).decode()
    worker.fetch_record(
        db.start_run(),
        WIVEDA_SOURCE,
        Response(
            item.url, item.url, 200, body, NOW.isoformat(), transport=last["transport"]
        ),
    )
    worker.config["detail_limit"] = 0
    worker.fetcher = SimpleNamespace(
        get=lambda url, **kwargs: pytest.fail(
            "Unaccepted proof cannot satisfy a pending job for free"
        )
    )
    before = history(db)
    worker.details(run := db.start_run())
    assert history(db) == before and portal(db, run)["status"] == "partial"
    assert worker.detail_stats[WIVEDA_SOURCE].get("cached", 0) == 0


@pytest.mark.parametrize("delta", [-0.001, 0, 0.001])
def test_accepted_cache_TTL_uses_exact_original_payload_clock(
    tmp_path, monkeypatch, delta
):
    db, worker, _, _, _, _ = setup(tmp_path, monkeypatch)
    worker.details(db.start_run())
    before = history(db)
    moment = datetime.fromisoformat(MANIFEST[1]["fetched_at"]) + timedelta(
        hours=6, seconds=delta
    )
    monkeypatch.setattr("car_market.bmw_detail_policy.now", lambda: moment.isoformat())
    monkeypatch.setattr("car_market.db.now", lambda: moment.isoformat())
    worker.config["detail_limit"] = 0
    worker.fetcher = SimpleNamespace(
        get=lambda url, **kwargs: pytest.fail("TTL probe has no allowance")
    )
    worker.details(run := db.start_run())
    assert history(db) == before
    assert portal(db, run)["status"] == ("complete" if delta < 0 else "partial")
    assert worker.detail_stats[WIVEDA_SOURCE].get("cached", 0) == int(delta < 0)
