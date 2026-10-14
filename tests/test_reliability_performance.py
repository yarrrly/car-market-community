"""Offline synthetic stress and interruption tests; no portal traffic or captures."""

import dataclasses
import json
import hashlib
import threading
import urllib.error
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from car_market.collect import Collector, bounded_responses
from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database, now
from car_market.fetch import Fetcher, Response
from car_market.parse import Listing, Page
from car_market.search_profile import GENERIC_PROFILE


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "synthetic.sqlite3")
    yield database
    database.conn.close()


def generic_config(**extra):
    return {
        "scope": {
            "country": "NL", "model_profile": GENERIC_PROFILE,
            "selector": {"make": "Toyota", "model_terms": [["Corolla"]]},
        },
        "queries": [], "workers": 1, "detail_limit": 2,
        "detail_batch_limit": 2, **extra,
    }


def synthetic_item(identity="m1", confirmed=False):
    return Listing(
        "marktplaats", identity,
        "https://www.marktplaats.nl/v/autos/toyota/" + identity,
        "Toyota Corolla", scope="target",
        raw={"country_code": "NL"} if confirmed else {},
    )


def put_pending(db, item, **state):
    key = "pending:" + item.source + ":" + item.external_id
    db.conn.execute(
        "INSERT OR REPLACE INTO monitor_state VALUES (?,?)",
        (key, json.dumps({"candidate": dataclasses.asdict(item), **state})),
    )
    db.conn.commit()
    return key


def install_http(monkeypatch, reply):
    calls = []

    class SyntheticReply:
        status = 200
        headers = Message()
        headers["Content-Type"] = "text/plain; charset=utf-8"

        def __init__(self, url, body, final_url):
            self.url, self.body = final_url or url, body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return self.body.encode()[:limit]

    def opened(request, **_kw):
        calls.append(request.full_url)
        result = reply(request.full_url, len(calls))
        if isinstance(result, Exception):
            raise result
        body, final_url = result
        return SyntheticReply(request.full_url, body, final_url)

    monkeypatch.setattr("urllib.request.urlopen", opened)
    return calls


def test_annotation_does_not_poison_http_cache(monkeypatch):
    calls = install_http(monkeypatch, lambda url, _: ("synthetic body", url))
    fetcher = Fetcher(delay=0)
    url = "https://synthetic.invalid/a"
    first = fetcher.get(url)
    first.error = "search_filters_changed"
    second = fetcher.get(url)
    assert second.error is None and second.body == "synthetic body"
    assert second.timestamp == first.timestamp and calls == [url]


def test_failed_refresh_invalidates_success_and_redirect_alias(monkeypatch):
    original = "https://synthetic.invalid/old"
    final = "https://synthetic.invalid/new"
    calls = install_http(monkeypatch, lambda url, n: (
        ("synthetic body", final) if n == 1 else
        urllib.error.HTTPError(url, 429, "synthetic rate limit", {}, None)
    ))
    fetcher = Fetcher(delay=0)
    assert fetcher.get(original).status == 200
    assert fetcher.get(original, refresh=True).status == 429
    assert fetcher.get(final).status == 429
    assert calls == [original, original, final]


def test_response_cache_bounds_body_memory_and_keeps_recent_hits(monkeypatch):
    calls = install_http(monkeypatch, lambda url, _: (url + "x" * 4096, url))
    fetcher = Fetcher(delay=0)
    fetcher._cache_limit_bytes = 32 * 1024
    for i in range(100):
        fetcher.get(f"https://synthetic.invalid/{i}")
    assert fetcher._cache_bytes <= 32 * 1024
    fetcher.get("https://synthetic.invalid/99")
    assert len(calls) == 100
    fetcher.get("https://synthetic.invalid/0")
    assert len(calls) == 101


def test_oversized_cache_entry_is_not_retained(monkeypatch):
    calls = install_http(monkeypatch, lambda url, _: ("x" * 2048, url))
    fetcher = Fetcher(delay=0)
    fetcher._cache_limit_bytes = 1024
    for _ in range(2):
        assert fetcher.get("https://synthetic.invalid/large").body == "x" * 2048
    assert len(calls) == 2 and fetcher._cache_bytes == 0


def test_throttle_reservations_are_per_host_and_stop_before_io(monkeypatch):
    current = [10.0]
    waits = []
    monkeypatch.setattr("car_market.fetch.time.monotonic", lambda: current[0])

    def sleep(seconds):
        waits.append(seconds)
        current[0] += seconds

    monkeypatch.setattr("car_market.fetch.time.sleep", sleep)
    fetcher = Fetcher(delay=1.5)
    fetcher._throttle("https://SYNTHETIC.invalid:443/1")
    fetcher._throttle("https://synthetic.invalid/2")
    fetcher._throttle("https://other.invalid/1")
    assert waits == [1.5]
    stopped = threading.Event()
    fetcher.stopped = stopped

    def interrupted_wait(seconds):
        stopped.set()
        return True

    monkeypatch.setattr(stopped, "wait", interrupted_wait)
    with pytest.raises(KeyboardInterrupt):
        fetcher.get("https://synthetic.invalid/3")


def test_request_window_does_not_submit_unbounded_backlog():
    calls = []
    release = threading.Event()

    def get(url):
        calls.append(url)
        assert release.wait(2)
        return url

    with bounded_responses(get, range(50000), 2) as responses:
        release.set()
        assert next(responses) == 0
    assert len(calls) <= 2


@pytest.mark.parametrize("fault", ["repeat", "wrong_number", "rate_limit"])
def test_hostile_pagination_stops_with_partial_history(db, monkeypatch, fault):
    collector = Collector(db, {"workers": 1, "max_pages": 200})
    calls = []

    def get(url, refresh=False):
        calls.append((url, refresh))
        page = int(parse_qs(urlsplit(url).query).get("page", [1])[0])
        if fault == "rate_limit" and page > 1:
            return Response(url, url, 429, None, now(), "http_429")
        return Response(url, url, 200, str(page), now())

    def parse(source, body, url):
        number = int(body)
        item = Listing(source, "synthetic-1", url, "BMW 218i Active Tourer").qualify()
        return Page([item], total=200, pages=200,
                    page=1 if fault == "wrong_number" else number)

    monkeypatch.setattr(collector.fetcher, "get", get)
    monkeypatch.setattr(collector, "_parse", parse)
    collector.collect_query(db.start_run("synthetic"), {
        "source": "dealer:synthetic.invalid", "key": "all",
        "url": "https://synthetic.invalid/search", "pagination": "page",
        "coverage": "enumerate", "infer_missing": False,
    })
    source = db.conn.execute("SELECT * FROM source_runs").fetchone()
    assert source["status"] == "partial" and source["received_count"] == 1
    assert len(calls) == (2 if fault == "rate_limit" else 3)
    assert db.conn.execute("SELECT availability FROM listings").fetchone()[0] == "observed"
    assert db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 2


def test_missing_route_does_not_renumber_following_page(db, monkeypatch):
    collector = Collector(db, {"workers": 1})
    monkeypatch.setattr("car_market.collect.page_url", lambda q, n, first:
                        None if n == 2 else q["url"] + "?page=3")
    monkeypatch.setattr(collector.fetcher, "get", lambda url, **kw:
                        Response(url, url, 200, "3" if "page=3" in url else "1", now()))
    monkeypatch.setattr(collector, "_parse", lambda source, body, url:
                        Page([Listing(source, body, url, "BMW 218i Active Tourer").qualify()],
                             total=3, pages=3, page=int(body)))
    collector.collect_query(db.start_run("synthetic"), {
        "source": "dealer:synthetic.invalid", "key": "all",
        "url": "https://synthetic.invalid/search", "pagination": "page",
        "coverage": "enumerate", "infer_missing": False,
    })
    source = db.conn.execute("SELECT * FROM source_runs").fetchone()
    assert source["status"] == "partial" and source["received_count"] == 2
    assert "pagination_route_missing" in source["error"]
    assert "page_number_mismatch" not in source["error"]


@pytest.mark.parametrize("empty", [False, True])
def test_wrong_initial_page_cannot_claim_complete_or_verified_empty(db, monkeypatch, empty):
    collector = Collector(db, {"workers": 1})
    monkeypatch.setattr(collector.fetcher, "get", lambda url:
                        Response(url, url, 200, "synthetic page", now()))
    monkeypatch.setattr(collector, "_parse", lambda source, body, url:
                        Page([] if empty else [Listing(source, "1", url, "BMW 218i Active Tourer").qualify()],
                             total=0 if empty else 1, pages=1, page=2))
    collector.collect_query(db.start_run("synthetic"), {
        "source": "dealer:synthetic.invalid", "key": "all",
        "url": "https://synthetic.invalid/search", "pagination": "page",
        "coverage": "enumerate", "infer_missing": False,
    })
    source = db.conn.execute("SELECT * FROM source_runs").fetchone()
    assert source["status"] == "partial" and "page_number_mismatch:1" in source["error"]


def test_source_backoff_survives_old_archive_and_blocks_all_detail_paths(db, monkeypatch):
    config = generic_config()
    item = synthetic_item(confirmed=True)
    collector = ConfiguredCollector(db, config)
    run = db.start_run("synthetic")
    listing_id = collector._observe(run, item, now())
    collector.queue_details(item, listing_id)
    put_pending(db, synthetic_item("m2"))
    db.save_fetch(run, item.source, item.url, item.url, 429, None, now(), error="http_429")
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    db.save_fetch(run, item.source, item.url, item.url, 403, None, old, error="http_403")
    monkeypatch.setattr(collector.fetcher, "get", lambda *_a, **_kw:
                        pytest.fail("Persisted source denial must block enrichment"))
    collector.collect_query(run, {"source": item.source, "key": "all"})
    collector.details(run)
    report = collector._pending_details()
    assert report["attempted"] == 0 and report["deferred"] == 1
    assert collector.detail_stats[item.source]["backoff"] == 1
    assert db.conn.execute("SELECT availability FROM listings").fetchone()[0] == "observed"
    assert db.conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "partial"


def test_pending_attempt_is_checkpointed_before_interruption(db, monkeypatch):
    config = generic_config()
    collector = ConfiguredCollector(db, config)
    key = put_pending(db, synthetic_item())
    calls = []

    def interrupted(url, **kw):
        calls.append(url)
        state = json.loads(db.conn.execute("SELECT value FROM monitor_state WHERE key=?", (key,)).fetchone()[0])
        assert state["last_attempt_at"]
        raise KeyboardInterrupt("synthetic interruption")

    monkeypatch.setattr(collector.fetcher, "get", interrupted)
    with pytest.raises(KeyboardInterrupt):
        collector._pending_details()
    restarted = ConfiguredCollector(db, config)
    monkeypatch.setattr(restarted.fetcher, "get", lambda *_a, **_kw:
                        pytest.fail("Restart ignored interrupted-attempt backoff"))
    report = restarted._pending_details()
    assert report["attempted"] == 0 and report["deferred"] == 1 and len(calls) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0


def test_pending_budget_fetches_only_selected_candidate_bodies(db, monkeypatch):
    collector = ConfiguredCollector(db, generic_config())
    item = synthetic_item()
    db.conn.executemany("INSERT INTO monitor_state VALUES (?,?)", (
        (f"pending:marktplaats:m{i:06}", json.dumps({"candidate": dataclasses.asdict(
            dataclasses.replace(item, external_id=f"m{i}", raw={"synthetic_padding": "x" * 256})
        )})) for i in range(5000)
    ))
    db.conn.commit()
    monkeypatch.setattr(collector.fetcher, "get", lambda url, **kw:
                        Response(url, url, 503, None, now(), "synthetic_503"))
    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        report = collector._pending_details()
    finally:
        db.conn.set_trace_callback(None)
    assert report["attempted"] == 2 and report["deferred"] == 5000
    candidate_reads = [sql for sql in statements if sql.startswith("SELECT value FROM monitor_state WHERE key=")]
    assert len(candidate_reads) == 2
    assert not any("SELECT key,value FROM monitor_state" in sql for sql in statements)


def test_detail_budget_is_shared_with_pending_enrichment(db, monkeypatch):
    collector = ConfiguredCollector(db, generic_config(detail_limit=1, detail_batch_limit=2))
    item = synthetic_item(confirmed=True)
    run = db.start_run("synthetic")
    listing_id = collector._observe(run, item, now())
    collector.queue_details(item, listing_id)
    put_pending(db, synthetic_item("m2"))
    calls = []
    monkeypatch.setattr(collector.fetcher, "get", lambda url, **kw:
                        calls.append(url) or Response(url, url, 503, None, now(), "synthetic_503"))
    collector.details(run)
    report = collector._pending_details()
    assert len(calls) == 1 and report["attempted"] == 0 and report["deferred"] == 1


def test_successful_pending_retry_drops_previous_attempt_error(db, monkeypatch):
    collector = ConfiguredCollector(db, generic_config())
    item = synthetic_item()
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    key = put_pending(db, item, last_attempt_at=old, attempt_error="old timeout",
                      last_attempt_fetch_id=99999)
    monkeypatch.setattr(collector.fetcher, "get", lambda url, **kw:
                        Response(url, url, 200, "synthetic own body", now()))
    monkeypatch.setattr(collector, "_parse", lambda *_a: Page([item]))
    monkeypatch.setattr(collector, "_detail_matches", lambda *_a: True)
    report = collector._pending_details()
    state = json.loads(db.conn.execute("SELECT value FROM monitor_state WHERE key=?", (key,)).fetchone()[0])
    assert report["unconfirmed"] == 1 and report["errors"] == 0
    assert state["attempt_status"] == "country_unconfirmed" and "attempt_error" not in state
    assert state["last_attempt_fetch_id"] == db.conn.execute("SELECT id FROM fetches").fetchone()[0]


def test_new_detail_denial_stops_remaining_source_requests(db, monkeypatch):
    collector = Collector(db, {"workers": 1, "detail_batch_limit": 20})
    run = db.start_run("synthetic")
    for i in range(20):
        url = f"https://synthetic.invalid/car/{i}"
        item = Listing("dealer:synthetic.invalid", str(i), url, "BMW 218i Active Tourer").qualify()
        collector.queue_details(item, db.observe(run, item, now()))
    calls = []
    monkeypatch.setattr(collector.fetcher, "get", lambda url:
                        calls.append(url) or Response(url, url, 429, None, now(), "http_429"))
    collector.details(run)
    assert len(calls) == 1
    source = db.conn.execute("SELECT * FROM source_runs").fetchone()
    assert source["expected_count"] == 20 and source["received_count"] == 1
    assert source["status"] == "partial"
    assert db.conn.execute("SELECT COUNT(*) FROM listings WHERE availability='observed'").fetchone()[0] == 20


def test_price_history_is_scanned_once_and_still_refreshes_changed_offer(db, monkeypatch):
    collector = ConfiguredCollector(db, generic_config(detail_cache_hours=24, detail_limit=1))
    run = db.start_run("synthetic")
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    items = []
    for i in range(30):
        item = synthetic_item(f"m{i}", confirmed=True)
        listing_id = collector._observe(run, item, old)
        collector.queue_details(item, listing_id)
        db.save_fetch(run, item.source, item.url, item.url, 200, "synthetic", old, retain_raw=False)
        items.append((item, listing_id))
    changed, listing_id = items[17]
    db.conn.execute("INSERT INTO events(run_id,listing_id,observed_at,kind) VALUES (?,?,?,?)",
                    (run, listing_id, now(), "price_eur_changed"))
    db.conn.commit()
    calls = []
    monkeypatch.setattr(collector.fetcher, "get", lambda url:
                        calls.append(url) or Response(url, url, 503, None, now(), "synthetic_503"))
    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        collector.details(db.start_run("synthetic"))
    finally:
        db.conn.set_trace_callback(None)
    assert calls == [changed.url]
    assert len([sql for sql in statements if sql.startswith("SELECT listing_id,MAX(observed_at) FROM events")]) == 1


@pytest.mark.parametrize("failure", ["navigate", "stop", "no_response", "http_404"])
def test_browser_is_closed_before_driver_exit_on_failure(monkeypatch, failure):
    fetcher = Fetcher(delay=0, timeout=3, browser=True)
    stopped = threading.Event()
    fetcher.stopped = stopped
    events = []
    caller_thread = threading.get_ident()

    class SyntheticPage:
        url = "https://synthetic.invalid/car/1"

        def set_default_timeout(self, timeout):
            assert timeout == 3000

        def goto(self, url, **kw):
            assert kw["timeout"] == 3000
            events.append(("goto", threading.get_ident()))
            if failure == "navigate":
                raise TimeoutError("synthetic timeout")
            if failure == "no_response":
                return None
            return SimpleNamespace(status=404 if failure == "http_404" else 200)

        def wait_for_timeout(self, *_a):
            if failure == "stop":
                stopped.set()

        def get_by_role(self, *_a, **_kw):
            return SimpleNamespace(count=lambda: 0)

        def content(self):
            return "synthetic body"

    page = SyntheticPage()
    browser = SimpleNamespace(
        new_context=lambda **kw: SimpleNamespace(new_page=lambda: page, cookies=lambda: []),
        close=lambda: events.append(("close", threading.get_ident())),
    )

    @contextmanager
    def driver():
        try:
            yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: browser))
        finally:
            events.append(("driver_exit", threading.get_ident()))

    monkeypatch.setattr("playwright.sync_api.sync_playwright", driver)
    if failure == "stop":
        with pytest.raises(KeyboardInterrupt):
            fetcher.browser_get(page.url)
    else:
        response = fetcher.browser_get(page.url)
        if failure == "no_response":
            assert response.status is None and response.error == "navigation_response_unavailable"
        elif failure == "http_404":
            assert response.status == 404 and response.error == "http_404"
        else:
            assert response.error.startswith("browser_unavailable:")
    assert [event for event, _ in events] == ["goto", "close", "driver_exit"]
    assert all(thread != caller_thread for _, thread in events)


def test_reused_collector_does_not_keep_previous_run_errors(db):
    collector = ConfiguredCollector(db, generic_config())
    collector.failures.append("previous_run_failed")
    collector.queue_details(synthetic_item(), 1)
    result = collector.run(details=False, checks=False)
    assert result["collector_errors"] == [] and result["status"] == "complete"
    assert collector.detail_jobs == {}


def autotrack_pending_search(db):
    """Inject missing search-country evidence using sanitized public own fields."""
    root = Path(__file__).parent / "fixtures/reliability_nl"
    name = "autotrack-corolla-detail.html"
    metadata = json.loads((root / "provenance.json").read_text())[name]
    body = (root / name).read_text()
    assert hashlib.sha256(body.encode()).hexdigest() == metadata["sha256"]
    url = metadata["original"]["final_url"]
    config = generic_config(queries=[{
        "source": "autotrack", "key": "all", "url": "https://www.autotrack.nl/aanbod/toyota/corolla",
        "coverage": "enumerate", "infer_missing": False,
    }])
    collector = ConfiguredCollector(db, config)
    own, = collector._parse("autotrack", body, url).listings
    candidate = dataclasses.replace(own, raw={}, kenteken=None, vin=None)
    stamp = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    run = db.start_run("synthetic_search_country_fault", timestamp=stamp)
    proof = db.save_fetch(run, "autotrack", config["queries"][0]["url"], config["queries"][0]["url"],
                         200, json.dumps({"synthetic_search_card": dataclasses.asdict(candidate)}), stamp)
    collector._observe(run, candidate, stamp, proof, "all")
    db.finish_source(run, "autotrack", "all", stamp, "complete", 1, 1, 0, 1, infer_missing=False)
    db.finish_run(run, "complete", {})
    key = "pending:autotrack:" + candidate.external_id
    return collector, own, candidate, body, url, run, proof, key


@pytest.mark.parametrize("legacy", [False, True])
def test_pending_country_resolution_keeps_original_search_coverage(db, monkeypatch, legacy):
    from car_market.coverage import report as coverage_report

    collector, own, candidate, body, url, searched, proof, key = autotrack_pending_search(db)
    if legacy:
        state = json.loads(db.conn.execute("SELECT value FROM monitor_state WHERE key=?", (key,)).fetchone()[0])
        state.pop("query_fetch_ids")
        db.conn.execute("UPDATE monitor_state SET value=? WHERE key=?", (json.dumps(state), key))
        db.conn.commit()
    monkeypatch.setattr(collector.fetcher, "get", lambda requested, **kw:
                        Response(requested, url, 200, body, now()))
    result = collector._pending_details()
    assert result["resolved"] == 1 and result["errors"] == result["deferred"] == 0
    membership = db.conn.execute("SELECT last_run_id FROM query_membership").fetchone()[0]
    observation = db.conn.execute("SELECT run_id,fetch_id FROM observations").fetchone()
    assert membership == searched and observation["run_id"] != searched
    detail_fetch = db.conn.execute("SELECT run_id FROM fetches WHERE id=?", (observation["fetch_id"],)).fetchone()[0]
    assert detail_fetch == observation["run_id"]
    assert coverage_report(db.conn, collector.config)["queries"][0]["target_ads"] == 1
    assert db.conn.execute("SELECT target_count FROM source_runs WHERE run_id=?", (searched,)).fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM monitor_state WHERE key=?", (key,)).fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["missing", "wrong_source", "wrong_query", "detail_run", "denied_proof"])
def test_pending_resolution_cannot_invent_unproved_search_membership(db, monkeypatch, fault):
    collector, own, candidate, body, url, searched, proof, key = autotrack_pending_search(db)
    state = json.loads(db.conn.execute("SELECT value FROM monitor_state WHERE key=?", (key,)).fetchone()[0])
    if fault == "missing":
        state["fetch_id"] = 999999
        state["query_fetch_ids"] = {"all": 999999}
    elif fault == "wrong_source":
        db.conn.execute("UPDATE fetches SET source='marktplaats' WHERE id=?", (proof,))
    elif fault == "wrong_query":
        state["query_key"] = "unproved"
        state["query_fetch_ids"] = {"unproved": proof}
    elif fault == "detail_run":
        db.conn.execute("UPDATE runs SET kind='discovery_enrichment' WHERE id=?", (searched,))
    else:
        old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        db.conn.execute("UPDATE fetches SET status=403,error='http_403',fetched_at=? WHERE id=?", (old, proof))
    db.conn.execute("UPDATE monitor_state SET value=? WHERE key=?", (json.dumps(state), key))
    db.conn.commit()
    monkeypatch.setattr(collector.fetcher, "get", lambda requested, **kw:
                        Response(requested, url, 200, body, now()))
    result = collector._pending_details()
    assert result["resolved"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM query_membership").fetchone()[0] == 0


@pytest.mark.parametrize("newer", [False, True])
def test_pending_replay_preserves_later_presence_and_missing_history(db, monkeypatch, newer):
    collector, own, candidate, body, url, searched, proof, key = autotrack_pending_search(db)
    state = db.conn.execute("SELECT value FROM monitor_state WHERE key=?", (key,)).fetchone()[0]
    stamp = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    presence = db.start_run("synthetic_newer_search", timestamp=stamp) if newer else searched
    if newer:
        db.finish_source(presence, "autotrack", "all", stamp, "complete", 1, 1, 1, 1, infer_missing=False)
    listing_id = collector._observe(presence, own, stamp, key="all")
    db.conn.execute("UPDATE query_membership SET missed_complete_runs=3 WHERE listing_id=?", (listing_id,))
    db.conn.execute("INSERT INTO monitor_state VALUES (?,?)", (key, state))
    db.conn.commit()
    monkeypatch.setattr(collector.fetcher, "get", lambda requested, **kw:
                        Response(requested, url, 200, body, now()))
    assert collector._pending_details()["resolved"] == 1
    membership = db.conn.execute("SELECT last_run_id,missed_complete_runs FROM query_membership").fetchone()
    assert tuple(membership) == (presence, 3)


def test_unknown_detail_refresh_keeps_all_original_query_proofs(db):
    collector, own, candidate, body, url, searched, proof, key = autotrack_pending_search(db)
    second_key = "second_search"
    second_run = db.start_run("synthetic_second_search")
    second_proof = db.save_fetch(second_run, "autotrack", "https://www.autotrack.nl/aanbod/toyota", url,
                                 200, "synthetic second search", now())
    collector._observe(second_run, candidate, now(), second_proof, second_key)
    detail_run = db.start_run("discovery_enrichment")
    detail_proof = db.save_fetch(detail_run, "autotrack", url, url, 200, body, now())
    collector._observe(detail_run, candidate, now(), detail_proof)
    state = json.loads(db.conn.execute("SELECT value FROM monitor_state WHERE key=?", (key,)).fetchone()[0])
    assert state["query_fetch_ids"] == {"all": proof, second_key: second_proof}
    assert detail_proof not in state["query_fetch_ids"].values()
    assert state["query_key"] == second_key


def test_pending_resolution_membership_and_deletion_commit_together(db, monkeypatch):
    collector, own, candidate, body, url, searched, proof, key = autotrack_pending_search(db)
    monkeypatch.setattr(collector.fetcher, "get", lambda requested, **kw:
                        Response(requested, url, 200, body, now()))

    def failed(*_a):
        raise RuntimeError("synthetic membership write failure")

    monkeypatch.setattr(collector, "_restore_pending_memberships", failed)
    result = collector._pending_details()
    assert result["errors"] == result["deferred"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM query_membership").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM monitor_state WHERE key=?", (key,)).fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 2


def test_gaspedaal_without_own_vdp_does_not_consume_detail_budget(db, monkeypatch):
    collector = ConfiguredCollector(db, generic_config(detail_limit=1))
    for i, url in enumerate([
        "https://www.gaspedaal.nl/toyota/corolla#123",
        "https://www.gaspedaal.nl/toyota/corolla?page=2",
        "https://www.gaspedaal.nl/toyota/corolla",
    ]):
        put_pending(db, Listing("gaspedaal", str(i), url, "Toyota Corolla"))
    requested = synthetic_item()
    put_pending(db, requested)
    calls = []
    monkeypatch.setattr(collector.fetcher, "get", lambda url, **kw:
                        calls.append(url) or Response(url, url, 503, None, now(), "synthetic_503"))
    result = collector._pending_details()
    assert result["attempted"] == 1 and calls == [requested.url]
    assert result["deferred"] == 4
    for row in db.conn.execute("SELECT value FROM monitor_state WHERE key LIKE 'pending:gaspedaal:%'"):
        assert "last_attempt_at" not in json.loads(row[0])
    assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0
