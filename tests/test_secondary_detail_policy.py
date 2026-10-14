"""Native secondary census, conservative fanout, and persistent attempt policy."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from car_market.db import Database
from car_market.fetch import Response
from car_market.mercedes_collect import MercedesCollector
from car_market.mercedes_de_sources import (
    PKW_SEARCH,
    collect_mercedes_de_source,
    parse_mercedes_de_sources,
)
from car_market.model_profiles import MERCEDES_PROFILE, use_profile
from car_market.parse import Listing, Page

ROOT = Path(__file__).resolve().parents[1] / "fixtures/mercedes_de_sources"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())


def native(name):
    meta = next(row for row in MANIFEST if row["fixture"] == name)
    body = (ROOT / name).read_text()
    assert hashlib.sha256(body.encode()).hexdigest() == meta["fixture_sha256"]
    return body, meta


def collector(tmp_path, **options):
    db = Database(tmp_path / "data/market.sqlite3")
    worker = MercedesCollector(
        db,
        {
            "scope": {"country": "DE", "model_profile": MERCEDES_PROFILE},
            "queries": [],
            "detail_limit": 2,
            "detail_cache_hours": 6,
            "detail_error_backoff_hours": 6,
            **options,
        },
    )
    worker._secondary_detail_attempts = 0
    worker._secondary_detail_deferrals = []
    return worker


@pytest.mark.parametrize(
    "name,source,excluded_count,relevant_count",
    [
        ("pkw-c180-api.json", "pkw_de", 20, 0),
        ("instamotion-c180.html", "instamotion_de", 18, 0),
        ("autohero-broad-api-1.json", "autohero_de", 21, 3),
    ],
)
def test_native_census_survives_profile_filter_before_fanout(
    tmp_path,
    name,
    source,
    excluded_count,
    relevant_count,
):
    body, meta = native(name)
    worker = collector(tmp_path)
    with use_profile(MERCEDES_PROFILE, "DE"):
        page = parse_mercedes_de_sources(source, body, meta["final_url"])
        excluded = {
            x.external_id
            for x in page.listings
            if not worker._secondary_detail_eligible(x)
        }
        relevant = {x.external_id for x in page.listings} - excluded
        assert (len(excluded), len(relevant)) == (excluded_count, relevant_count)
        calls = []

        def get(url, **kwargs):
            return Response(url, url, 200, body, meta["captured_at"])

        def detail(url, *, original):
            calls.append(original.external_id)
            return Response(
                url, url, 404, "not available", meta["captured_at"], "http_404"
            )

        query = {
            "source": source,
            "url": meta["url"],
            "request_body": meta.get("request_body"),
        }
        result = collect_mercedes_de_source(
            get,
            query,
            max_pages=1,
            max_details=2,
            detail_eligible=worker._secondary_detail_eligible,
            detail_getter=detail,
        )
    assert {x.external_id for x in result["listings"]} == {
        x.external_id for x in page.listings
    }
    assert result["expected"] == page.total
    assert result["received"] == len({x.external_id for x in page.listings})
    assert set(calls) <= relevant
    assert len(calls) == min(2, relevant_count)
    assert set(result["details_excluded"]) == excluded
    assert result["details_expected"] == len(relevant)
    worker.db.conn.close()


@pytest.mark.parametrize(
    "title", ["C-Klasse", "Mercedes-Benz C-Klasse", "Mercedes-Benz C180"]
)
def test_underspecified_search_card_is_not_pruned(tmp_path, title):
    worker = collector(tmp_path)
    pending = Listing(
        "pkw_de",
        "unknown",
        "https://www.pkw.de/auto/unknown",
        title,
        fuel="petrol",
        transmission="automatic",
        year=2018,
    )
    assert worker._secondary_detail_eligible(pending)
    assert pending.scope == "unknown"
    worker.db.conn.close()


def test_unbound_secondary_caller_keeps_its_own_detail_frontier():
    body, meta = native("pkw-c180-api.json")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(
            url,
            url,
            200 if len(calls) == 1 else 404,
            body,
            meta["captured_at"],
            None if len(calls) == 1 else "http_404",
        )

    result = collect_mercedes_de_source(
        get, {"source": "pkw_de", "url": PKW_SEARCH}, max_pages=1, max_details=1
    )
    assert len(calls) == 2
    assert result["details_excluded"] == []
    assert result["details_expected"] == 20


def own_detail():
    body, meta = native("pkw-detail-142208737556145407.json")
    (item,) = parse_mercedes_de_sources("pkw_de", body, meta["final_url"]).listings
    return body, meta, item


def test_secondary_backoff_survives_new_collector_and_preserves_capture(tmp_path):
    _body, meta, item = own_detail()
    worker = collector(tmp_path)
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    run = worker.db.start_run("test")
    fetch = worker.db.save_fetch(
        run,
        "pkw_de",
        meta["final_url"],
        meta["final_url"],
        429,
        "rate limited",
        old,
        error="http_429",
    )
    before = tuple(
        worker.db.conn.execute(
            "SELECT fetched_at,content_hash FROM fetches WHERE id=?", (fetch,)
        ).fetchone()
    )
    worker.db.conn.close()
    restored = collector(tmp_path)
    restored._secondary_get = lambda *a, **k: pytest.fail("backoff must not fetch")
    with pytest.raises(ValueError, match="detail_host_backoff:429"):
        restored._secondary_detail_get(
            run + 1, "pkw_de", meta["final_url"], original=item
        )
    assert restored._secondary_detail_deferrals[0]["fetch_id"] == fetch
    assert (
        tuple(
            restored.db.conn.execute(
                "SELECT fetched_at,content_hash FROM fetches WHERE id=?", (fetch,)
            ).fetchone()
        )
        == before
    )
    assert restored.db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 1
    restored.db.conn.close()


def test_cached_native_success_keeps_original_proof_without_new_observation(tmp_path):
    body, meta, item = own_candidate()
    worker = collector(tmp_path)
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    run = worker.db.start_run("test")
    fetch = worker.db.save_fetch(
        run, item.source, item.url, meta["final_url"], 200, body, old
    )
    assert worker._observe(run, copy.deepcopy(item), old, fetch) is not None
    worker._secondary_get = lambda *a, **k: pytest.fail(
        "fresh retained VDP must not refetch"
    )
    response = worker._secondary_detail_get(run, item.source, item.url, original=item)
    accepted = response.secondary_cached_accepted
    assert response.timestamp == accepted["fetched_at"] == old
    assert response.secondary_fetch_id == accepted["fetch_id"] == fetch
    assert accepted["content_hash"] == hashlib.sha256(body.encode()).hexdigest()
    assert (
        worker._secondary_detail_attempts == 0
        and worker._secondary_detail_deferrals == []
    )
    assert (
        worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    )
    assert (
        worker.db.conn.execute("SELECT observed_at FROM observations").fetchone()[0]
        == old
    )
    worker.db.conn.close()


def test_secondary_backoff_does_not_block_another_provider_host(tmp_path):
    body, meta, item = own_detail()
    worker = collector(tmp_path)
    run = worker.db.start_run("test")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    other = "https://suche.pkw.de/blocked"
    worker.db.save_fetch(
        run, "pkw_de", other, other, 429, "rate limited", old, error="http_429"
    )
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(url, url, 200, body, datetime.now(UTC).isoformat())

    worker._secondary_get = get
    worker._secondary_detail_get(run, "pkw_de", meta["final_url"], original=item)
    assert calls == [meta["final_url"]]
    worker.db.conn.close()


def test_blocked_host_lookup_uses_partial_index_and_actual_clock_order(tmp_path):
    _body, meta, item = own_detail()
    worker = collector(tmp_path)
    run = worker.db.start_run("test")
    clock = datetime.now(UTC)
    worker.db.conn.executemany(
        "INSERT INTO fetches(run_id,source,url,final_url,fetched_at,status) VALUES (?,?,?,?,?,200)",
        [
            (
                run,
                "pkw_de",
                f"https://www.pkw.de/api/v1/cars/bench-{i}",
                f"https://www.pkw.de/api/v1/cars/bench-{i}",
                clock.isoformat(),
            )
            for i in range(10000)
        ],
    )
    older_local = (
        (clock - timedelta(minutes=20))
        .astimezone(timezone(timedelta(hours=2)))
        .isoformat()
    )
    worker.db.save_fetch(
        run,
        "pkw_de",
        meta["final_url"],
        meta["final_url"],
        403,
        "older",
        older_local,
        error="http_403",
    )
    fresh = (clock - timedelta(minutes=1)).isoformat()
    expected = worker.db.save_fetch(
        run,
        "pkw_de",
        meta["final_url"],
        meta["final_url"],
        429,
        "latest matching host",
        fresh,
        error="http_429",
    )
    worker.db.save_fetch(
        run,
        "pkw_de",
        "https://suche.pkw.de/blocked",
        "https://suche.pkw.de/blocked",
        403,
        "other host",
        clock.isoformat(),
        error="http_403",
    )
    real = worker.db.conn
    plans = []

    class ObservedConnection:
        def execute(self, sql, args=()):
            if "status IN (403,429)" in sql:
                plans.extend(
                    row[3] for row in real.execute("EXPLAIN QUERY PLAN " + sql, args)
                )
            return real.execute(sql, args)

        def __getattr__(self, name):
            return getattr(real, name)

    worker.db.conn = ObservedConnection()
    worker._secondary_get = lambda *a, **k: pytest.fail(
        "matching blocked host must not fetch"
    )
    with pytest.raises(ValueError, match="detail_host_backoff:429"):
        worker._secondary_detail_get(run, "pkw_de", meta["final_url"], original=item)
    assert worker._secondary_detail_deferrals[0]["fetch_id"] == expected
    assert any("fetch_source_blocked_actual_time" in plan for plan in plans)
    assert not any("TEMP B-TREE" in plan for plan in plans)
    worker.db.conn.close()


def test_secondary_cache_without_raw_retries_instead_of_retiming_old_body(tmp_path):
    body, meta, item = own_detail()
    worker = collector(tmp_path)
    run = worker.db.start_run("test")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    fetch = worker.db.save_fetch(
        run,
        "pkw_de",
        meta["final_url"],
        meta["final_url"],
        200,
        body,
        old,
        retain_raw=False,
    )
    fresh = datetime.now(UTC).isoformat()
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(url, url, 200, body, fresh)

    worker._secondary_get = get
    response = worker._secondary_detail_get(
        run, "pkw_de", meta["final_url"], original=item
    )
    assert calls == [meta["final_url"]]
    assert response.timestamp == fresh
    assert (
        worker.db.conn.execute(
            "SELECT fetched_at FROM fetches WHERE id=?", (fetch,)
        ).fetchone()[0]
        == old
    )
    assert (
        worker.db.conn.execute(
            "SELECT fetched_at,raw_path FROM fetches WHERE id=?",
            (response.secondary_fetch_id,),
        ).fetchone()[0]
        == fresh
    )
    worker.db.conn.close()


def test_secondary_budget_applies_across_urls_and_retains_attempt_once(tmp_path):
    body, meta, item = own_detail()
    worker = collector(tmp_path, detail_limit=1)
    run = worker.db.start_run("test")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(url, url, 200, body, datetime.now(UTC).isoformat())

    worker._secondary_get = get
    response = worker._secondary_detail_get(
        run, "pkw_de", meta["final_url"], original=item
    )
    assert response.secondary_fetch_id
    another = copy.deepcopy(item)
    another.external_id = "987654321"
    with pytest.raises(ValueError, match="detail_budget_deferred"):
        worker._secondary_detail_get(
            run, "pkw_de", "https://www.pkw.de/api/v1/cars/987654321", original=another
        )
    assert len(calls) == 1
    assert worker.db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 1
    worker.db.conn.close()


def test_portal_429_backoff_is_partial_with_original_expected_work(tmp_path):
    _body, meta, item = own_detail()
    worker = collector(tmp_path)
    run = worker.db.start_run("test")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    fetch = worker.db.save_fetch(
        run,
        "pkw_de",
        meta["final_url"],
        meta["final_url"],
        429,
        "rate limited",
        old,
        error="http_429",
    )
    worker.detail_jobs = {meta["final_url"]: ("pkw_de", None, item)}
    worker._detail_get = lambda *a, **k: pytest.fail("429 backoff must not fetch")
    worker.details(run)
    health = worker.db.conn.execute(
        "SELECT * FROM source_runs WHERE source='portal_details'"
    ).fetchone()
    assert (health["status"], health["expected_count"], health["received_count"]) == (
        "partial",
        1,
        0,
    )
    assert "1 backoff" in health["error"]
    assert worker.detail_stats["pkw_de"]["backoff"] == 1
    assert (
        worker.db.conn.execute(
            "SELECT fetched_at FROM fetches WHERE id=?", (fetch,)
        ).fetchone()[0]
        == old
    )
    worker.db.conn.close()


def test_discovery_state_keeps_census_and_capture_refs_without_raw_duplication(
    tmp_path,
):
    body, meta = native("pkw-c180-api.json")
    worker = collector(tmp_path)
    worker._secondary_get = lambda url, **kwargs: Response(
        url, url, 200, body, meta["captured_at"]
    )
    run = worker.db.start_run("test")
    worker._collect_secondary(
        run,
        {
            "source": "pkw_de",
            "key": "native",
            "url": PKW_SEARCH,
            "max_pages": 1,
            "detail_limit": 0,
        },
    )
    raw = worker.db.conn.execute(
        "SELECT value FROM monitor_state WHERE key='model_discovery:pkw_de:native'"
    ).fetchone()[0]
    ledger = json.loads(raw)
    assert ledger["schema_version"] == 2 and ledger["received"] == 20
    assert ledger["expected"] == 183 and len(ledger["listings"]) == 20
    assert all("raw" not in item for item in ledger["listings"])
    for item in ledger["listings"]:
        fetch = worker.db.conn.execute(
            "SELECT content_hash,fetched_at FROM fetches WHERE id=?",
            (item["fetch_id"],),
        ).fetchone()
        assert item["content_hash"] == fetch["content_hash"]
        assert item["fetched_at"] == fetch["fetched_at"] == meta["captured_at"]
        assert "generation_unconfirmed" in item["qualification_missing"]
    assert len(raw) < len(body)
    worker.db.conn.close()


def own_candidate():
    body, meta = native("autohero-c205-detail.html")
    (item,) = parse_mercedes_de_sources("autohero_de", body, meta["final_url"]).listings
    return body, meta, item


@pytest.mark.parametrize("path", ["portal", "secondary"])
@pytest.mark.parametrize("revision_kind", ["global", "source"])
def test_parser_revision_forces_fresh_wire_capture_in_both_detail_paths(
    tmp_path,
    path,
    revision_kind,
):
    body, meta, item = own_candidate()
    worker = collector(tmp_path)
    run = worker.db.start_run("test")
    clock = datetime.now(UTC)
    old = (
        (clock - timedelta(minutes=5))
        .astimezone(timezone(timedelta(hours=2)))
        .isoformat()
    )
    fetch = worker.db.save_fetch(
        run, item.source, item.url, meta["final_url"], 200, body, old
    )
    listing_id = worker._observe(run, copy.deepcopy(item), old, fetch)
    assert listing_id is not None
    revision = (clock - timedelta(minutes=1)).isoformat()
    if revision_kind == "global":
        worker.parser_refresh_since = revision
    else:
        worker.source_parser_refresh_since[item.source] = revision
    worker.fetcher._cache[item.url] = Response(
        item.url, meta["final_url"], 200, body, old
    )
    calls = []
    fresh = clock.isoformat()

    def get(url, **kwargs):
        assert kwargs["refresh"] is True
        calls.append(url)
        return Response(url, meta["final_url"], 200, body, fresh)

    worker.fetcher.get = get
    if path == "secondary":
        response = worker._secondary_detail_get(
            run, item.source, item.url, original=item
        )
        assert response.timestamp == fresh
    else:
        worker.detail_jobs = {item.url: (item.source, listing_id, item)}
        worker.details(run)
        assert worker.detail_stats[item.source]["parsed"] == 1
        assert worker.detail_stats[item.source].get("cached", 0) == 0
        assert (
            worker.db.conn.execute("SELECT last_seen FROM listings").fetchone()[0]
            == fresh
        )
        assert (
            worker.db.conn.execute(
                "SELECT observed_at FROM latest_observations"
            ).fetchone()[0]
            == old
        )
    assert calls == [item.url]
    assert (
        worker.db.conn.execute(
            "SELECT fetched_at FROM fetches ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        == fresh
    )
    assert (
        worker.db.conn.execute(
            "SELECT fetched_at FROM fetches WHERE id=?", (fetch,)
        ).fetchone()[0]
        == old
    )
    worker.db.conn.close()


@pytest.mark.parametrize("path", ["portal", "secondary"])
def test_parser_revision_does_not_bypass_fresh_429_backoff(tmp_path, path):
    _body, meta, item = own_candidate()
    worker = collector(tmp_path)
    run = worker.db.start_run("test")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    fetch = worker.db.save_fetch(
        run,
        item.source,
        item.url,
        meta["final_url"],
        429,
        "limited",
        old,
        error="http_429",
    )
    revision = datetime.now(UTC).isoformat()
    worker.parser_refresh_since = revision
    worker.source_parser_refresh_since[item.source] = revision
    worker.fetcher.get = lambda *a, **k: pytest.fail(
        "revision must not bypass denial backoff"
    )
    if path == "secondary":
        with pytest.raises(ValueError, match="detail_host_backoff:429"):
            worker._secondary_detail_get(run, item.source, item.url, original=item)
    else:
        worker.detail_jobs = {item.url: (item.source, None, item)}
        worker.details(run)
        assert (
            worker.db.conn.execute("SELECT status FROM source_runs").fetchone()[0]
            == "partial"
        )
        assert worker.detail_stats[item.source]["backoff"] == 1
    assert (
        worker.db.conn.execute(
            "SELECT fetched_at FROM fetches WHERE id=?", (fetch,)
        ).fetchone()[0]
        == old
    )
    worker.db.conn.close()


@pytest.mark.parametrize("path", ["portal", "secondary"])
@pytest.mark.parametrize("revision", ["invalid", "future"])
def test_invalid_or_future_revision_does_not_refresh_accepted_cache(
    tmp_path, path, revision
):
    body, meta, item = own_candidate()
    worker = collector(tmp_path)
    run = worker.db.start_run("test")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    fetch = worker.db.save_fetch(
        run, item.source, item.url, meta["final_url"], 200, body, old
    )
    listing_id = worker._observe(run, copy.deepcopy(item), old, fetch)
    assert listing_id is not None
    cutoff = (
        "invalid"
        if revision == "invalid"
        else (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    )
    worker.parser_refresh_since = cutoff
    worker.source_parser_refresh_since[item.source] = cutoff
    worker.fetcher.get = lambda *a, **k: pytest.fail(
        "invalid/future revision must not force HTTP"
    )
    if path == "secondary":
        response = worker._secondary_detail_get(
            run, item.source, item.url, original=item
        )
        assert response.secondary_cached_accepted["fetch_id"] == fetch
    else:
        worker.detail_jobs = {item.url: (item.source, listing_id, item)}
        worker.details(run)
        assert worker.detail_stats[item.source]["cached"] == 1
    worker.db.conn.close()


@pytest.mark.parametrize("detail_limit", [0, 1])
def test_pending_portal_job_cannot_be_satisfied_by_an_unaccepted_cached_body(
    tmp_path, detail_limit
):
    body, meta, item = own_candidate()
    worker = collector(tmp_path, detail_limit=detail_limit)
    run = worker.db.start_run("test")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    fetch = worker.db.save_fetch(
        run, item.source, item.url, meta["final_url"], 200, body, old
    )
    assert (
        worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    )
    worker.detail_jobs = {item.url: (item.source, None, item)}
    fresh = datetime.now(UTC).isoformat()
    calls = []

    def get(url, **kwargs):
        assert kwargs["refresh"] is True
        calls.append(url)
        return Response(url, meta["final_url"], 200, body, fresh)

    worker.fetcher.get = get
    worker.details(run)
    health = worker.db.conn.execute("SELECT * FROM source_runs").fetchone()
    assert (health["status"], health["expected_count"], health["received_count"]) == (
        "complete" if detail_limit else "partial",
        1,
        detail_limit,
    )
    assert len(calls) == detail_limit
    assert worker.detail_stats[item.source]["parsed"] == detail_limit
    assert worker.detail_stats[item.source].get("cached", 0) == 0
    assert (
        worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        == detail_limit
    )
    if detail_limit:
        observation = worker.db.conn.execute(
            "SELECT observed_at,fetch_id FROM observations"
        ).fetchone()
        assert observation["observed_at"] == fresh and observation["fetch_id"] != fetch
    assert (
        worker.db.conn.execute(
            "SELECT fetched_at FROM fetches WHERE id=?", (fetch,)
        ).fetchone()[0]
        == old
    )
    worker.db.conn.close()


@pytest.mark.parametrize("detail_limit", [0, 1])
def test_unaccepted_secondary_cache_requests_fresh_under_shared_budget(
    tmp_path, detail_limit
):
    body, meta, item = own_candidate()
    worker = collector(tmp_path, detail_limit=detail_limit)
    run = worker.db.start_run("test")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    fetch = worker.db.save_fetch(
        run, item.source, item.url, meta["final_url"], 200, body, old
    )
    fresh = datetime.now(UTC).isoformat()
    calls = []

    def get(url, **kwargs):
        assert kwargs["refresh"] is True
        calls.append(url)
        return Response(url, meta["final_url"], 200, body, fresh)

    worker.fetcher.get = get
    if detail_limit:
        response = worker._secondary_detail_get(
            run, item.source, item.url, original=item
        )
        assert response.timestamp == fresh and response.secondary_fetch_id != fetch
    else:
        with pytest.raises(ValueError, match="detail_budget_deferred"):
            worker._secondary_detail_get(run, item.source, item.url, original=item)
    assert len(calls) == detail_limit
    assert (
        worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    )
    assert (
        worker.db.conn.execute(
            "SELECT fetched_at FROM fetches WHERE id=?", (fetch,)
        ).fetchone()[0]
        == old
    )
    worker.db.conn.close()


@pytest.mark.parametrize("path", ["portal", "secondary"])
def test_identical_new_capture_can_use_an_accepted_older_observation(tmp_path, path):
    body, meta, item = own_candidate()
    worker = collector(tmp_path)
    old_run = worker.db.start_run("test")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    old_fetch = worker.db.save_fetch(
        old_run, item.source, item.url, meta["final_url"], 200, body, old
    )
    listing_id = worker._observe(old_run, copy.deepcopy(item), old, old_fetch)
    assert listing_id is not None
    run = worker.db.start_run("test")
    newer = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    fetch = worker.db.save_fetch(
        run, item.source, item.url, meta["final_url"], 200, body, newer
    )
    assert worker._observe(run, copy.deepcopy(item), newer, fetch) == listing_id
    assert (
        worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    )
    worker.fetcher.get = lambda *a, **k: pytest.fail(
        "identical accepted native proof should respect TTL"
    )
    if path == "secondary":
        response = worker._secondary_detail_get(
            run, item.source, item.url, original=item
        )
        accepted = response.secondary_cached_accepted
        assert (accepted["fetch_id"], accepted["fetched_at"]) == (fetch, newer)
        assert (accepted["accepted_fetch_id"], accepted["accepted_observed_at"]) == (
            old_fetch,
            old,
        )
    else:
        worker.detail_jobs = {item.url: (item.source, listing_id, item)}
        worker.details(run)
        assert worker.detail_stats[item.source]["cached"] == 1
        assert (
            worker.db.conn.execute("SELECT status FROM source_runs").fetchone()[0]
            == "complete"
        )
    observation = worker.db.conn.execute(
        "SELECT observed_at,fetch_id FROM observations"
    ).fetchone()
    assert tuple(observation) == (old, old_fetch)
    worker.db.conn.close()


def test_rejected_portal_observation_stays_partial(tmp_path, monkeypatch):
    body, meta, item = own_detail()
    worker = collector(tmp_path)
    run = worker.db.start_run("test")
    worker.detail_jobs = {meta["final_url"]: (item.source, None, item)}
    stamp = datetime.now(UTC).isoformat()
    worker.fetcher.get = lambda url, **kwargs: Response(
        url, meta["final_url"], 200, body, stamp
    )
    monkeypatch.setattr(worker, "_observe", lambda *args, **kwargs: None)
    worker.details(run)
    health = worker.db.conn.execute("SELECT * FROM source_runs").fetchone()
    assert (health["status"], health["expected_count"], health["received_count"]) == (
        "partial",
        1,
        1,
    )
    assert worker.detail_stats[item.source]["parsed"] == 0
    assert worker.detail_stats[item.source]["errors"] == 1
    assert (
        worker.db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    )
    fetch = worker.db.conn.execute("SELECT fetched_at,error FROM fetches").fetchone()
    assert tuple(fetch) == (stamp, "detail_not_accepted")
    worker.db.conn.close()


def test_accepted_cache_satisfies_secondary_details_without_retiming_history(
    tmp_path, monkeypatch
):
    # One-publication counter control; the owned VDP body/item are unmodified native proof.
    import car_market.mercedes_de_sources as sources

    body, meta, item = own_detail()
    worker = collector(tmp_path)
    old_run = worker.db.start_run("test")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    fetch = worker.db.save_fetch(
        old_run, item.source, meta["final_url"], meta["final_url"], 200, body, old
    )
    assert worker._observe(old_run, copy.deepcopy(item), old, fetch) is not None
    before = [
        tuple(row) for row in worker.db.conn.execute("SELECT * FROM observations")
    ]
    real_parse = sources.parse_mercedes_de_sources

    def parse(source, text, url):
        if "/cars/search/basic" in url:
            return Page([copy.deepcopy(item)], 1, 1, 1)
        return real_parse(source, text, url)

    monkeypatch.setattr(sources, "parse_mercedes_de_sources", parse)
    fresh = datetime.now(UTC).isoformat()
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert "/cars/search/basic" in url
        return Response(url, url, 200, body, fresh)

    worker.fetcher.get = get
    run = worker.db.start_run("test")
    worker._collect_secondary(
        run,
        {
            "source": item.source,
            "key": "accepted",
            "url": PKW_SEARCH,
            "max_pages": 1,
            "detail_limit": 1,
        },
    )
    ledger = json.loads(
        worker.db.conn.execute(
            "SELECT value FROM monitor_state WHERE key='model_discovery:pkw_de:accepted'"
        ).fetchone()[0]
    )
    assert (
        ledger["status"],
        ledger["enumeration_status"],
        ledger["detail_status"],
    ) == ("complete",) * 3
    assert (
        ledger["expected"],
        ledger["received"],
        ledger["details_expected"],
        ledger["details_received"],
    ) == (1, 1, 1, 1)
    assert (
        ledger["errors"] == ledger["detail_errors"] == ledger["detail_deferrals"] == []
    )
    assert ledger["details_cached"] == [item.external_id]
    assert (
        ledger["details_attempted"]
        == ledger["details_deferred"]
        == ledger["details_budget_deferred"]
        == []
    )
    (row,) = ledger["listings"]
    assert (row["fetch_id"], row["fetched_at"]) == (fetch, old)
    assert row["accepted_detail_cache"]["accepted_fetch_id"] == fetch
    assert row["discovery_capture"]["fetched_at"] == fresh
    assert row["discovery_capture"]["fetch_id"] != fetch
    assert len(calls) == 1 and worker._secondary_detail_attempts == 0
    assert worker.db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 2
    assert [
        tuple(row) for row in worker.db.conn.execute("SELECT * FROM observations")
    ] == before
    assert worker.db.conn.execute("SELECT last_seen FROM listings").fetchone()[0] == old
    worker.db.conn.close()


def test_shared_budget_retains_all_native_pending_ids_and_qualification_missing(
    tmp_path,
):
    body, meta = native("autohero-broad-api-1.json")
    worker = collector(tmp_path, detail_limit=1)
    page = worker._parse("autohero_de", body, meta["final_url"])
    relevant = {
        item.external_id
        for item in page.listings
        if worker._secondary_detail_eligible(item)
    }
    assert len(relevant) == 3
    run = worker.db.start_run("test")
    worker._secondary_policy_run = run
    worker._secondary_detail_attempts = (
        1  # An earlier query has spent the shared run budget.
    )
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert url == meta["url"]
        return Response(url, meta["final_url"], 200, body, meta["captured_at"])

    worker.fetcher.get = get
    worker._collect_secondary(
        run,
        {
            "source": "autohero_de",
            "key": "budget",
            "url": meta["url"],
            "request_body": meta["request_body"],
            "max_pages": 1,
            "detail_limit": 2,
        },
    )
    ledger = json.loads(
        worker.db.conn.execute(
            "SELECT value FROM monitor_state WHERE key='model_discovery:autohero_de:budget'"
        ).fetchone()[0]
    )
    assert ledger["details_expected"] == 3 and ledger["details_received"] == 0
    assert ledger["detail_status"] == ledger["status"] == "partial"
    assert (
        set(ledger["details_budget_deferred"])
        == set(ledger["details_deferred"])
        == relevant
    )
    assert len(ledger["details_selected"]) == 2
    assert ledger["details_attempted"] == ledger["details_cached"] == []
    assert {row["external_id"] for row in ledger["detail_deferrals"]} <= relevant
    assert len(ledger["detail_deferrals"]) == 2
    native_items = {item.external_id: item for item in page.listings}
    for row in ledger["listings"]:
        owned = native_items[row["external_id"]]
        assert (
            row["qualification_missing"] == owned.raw["model_scope_evidence"]["missing"]
        )
        assert row["qualification_excluded"] == (owned.scope == "excluded")
        if row["external_id"] in relevant:
            assert set(row["qualification_missing"]) == {
                "coupe_unconfirmed",
                "generation_unconfirmed",
            }
    assert (
        len(ledger["listings"]) == ledger["received"] == 24
        and ledger["expected"] == 176
    )
    assert len(calls) == 1 and worker._secondary_detail_attempts == 1
    worker.db.conn.close()


def test_changed_own_price_forces_secondary_refresh_and_counts_one_attempt(tmp_path):
    body, meta, item = own_candidate()
    worker = collector(tmp_path)
    run = worker.db.start_run("test")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    fetch = worker.db.save_fetch(
        run, item.source, item.url, meta["final_url"], 200, body, old
    )
    assert worker._observe(run, copy.deepcopy(item), old, fetch) is not None
    worker.fetcher._cache[item.url] = Response(
        item.url, meta["final_url"], 200, body, old
    )
    original = copy.deepcopy(item)
    original.price += 100
    calls = []
    fresh = datetime.now(UTC).isoformat()

    def get(url, **kwargs):
        assert kwargs["refresh"] is True
        calls.append(url)
        return Response(url, meta["final_url"], 200, body, fresh)

    worker.fetcher.get = get
    response = worker._secondary_detail_get(
        run, item.source, item.url, original=original
    )
    assert response.timestamp == fresh and calls == [item.url]
    assert not getattr(response, "secondary_cached_accepted", None)
    assert worker._secondary_detail_attempts == 1
    worker.db.conn.close()
