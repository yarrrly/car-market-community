"""Generic detail priority on native cards and bounded offline queue controls."""

import copy
import gzip
import json
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import pytest

from car_market.collect import Collector
from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database, now
from car_market.fetch import Response
from car_market.parse import Listing
from car_market.search_profile import GENERIC_PROFILE


def settings(**extra):
    return {
        "scope": {
            "country": "NL",
            "model_profile": GENERIC_PROFILE,
            "selector": {
                "make": "BMW",
                "model_terms": [["218i", "218 i"], ["Active Tourer"]],
                "exclude_terms": ["Gran Tourer"],
            },
        },
        "queries": [],
        "workers": 1,
        "detail_limit": 50,
        "detail_batch_limit": 50,
        "fast_detail_limit": 25,
        "detail_cache_hours": 24,
        **extra,
    }


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / "market.sqlite3")
    yield value
    value.conn.close()


def candidate(identity, source="first.invalid", *, target=True, country="NL"):
    # Synthetic publications isolate scheduling; native cards are tested below.
    return Listing(
        "dealer:" + source,
        identity,
        f"https://{source}/car/{identity}",
        "BMW 218i Active Tourer" if target else "BMW 2 Series",
        raw={"country_code": country} if country else {},
    )


def queue(worker, items):
    run = worker.db.start_run()
    for item in items:
        identity = worker._observe(run, item, now())
        if identity:
            worker.queue_details(item, identity)


def plan(worker, monkeypatch):
    selected = []

    @contextmanager
    def intercept(get, urls, workers):
        selected.extend(urls)
        yield iter(())

    monkeypatch.setattr("car_market.collect.bounded_responses", intercept)
    monkeypatch.setattr(
        worker.fetcher, "get", lambda *a, **k: pytest.fail("planner attempted HTTP")
    )
    worker.details(worker.db.start_run())
    return [worker.detail_jobs[url][2] for url in selected]


@pytest.mark.parametrize("cache", [0, 24])
@pytest.mark.parametrize("fast", [False, True])
def test_native_bmw_targets_precede_broad_confirmation_cards(
    db, monkeypatch, cache, fast
):
    worker = ConfiguredCollector(db, settings(detail_cache_hours=cache, detail_limit=4))
    worker.fast = fast
    root = Path(__file__).parent / "fixtures/autoscout_query_binding"
    meta = next(
        row
        for row in json.loads((root / "manifest.json").read_text())
        if row["case"] == "nl-bmw-218i"
    )
    body = gzip.decompress((root / meta["file"]).read_bytes()).decode()
    page = worker._parse("autoscout24", body, meta["final_url"])
    assert Counter(item.scope for item in page.listings) == {
        "target": 19,
        "needs_confirmation": 1,
    }
    queue(worker, sorted(page.listings, key=lambda item: item.scope == "target"))
    originals = [copy.deepcopy(item.raw) for item in page.listings]
    selected = plan(worker, monkeypatch)
    assert len(selected) == 4 and all(item.scope == "target" for item in selected)
    assert len(worker.detail_jobs) == len({item.url for item in page.listings}) == 17
    assert [item.raw for item in page.listings] == originals
    assert db.conn.execute("SELECT count(*) FROM query_membership").fetchone()[0] == 0


@pytest.mark.parametrize("cache", [0, 24])
def test_default_fifty_budget_keeps_all_four_targets_and_other_candidates(
    db, monkeypatch, cache
):
    worker = ConfiguredCollector(db, settings(detail_cache_hours=cache))
    items = [candidate(f"pending-{i}", target=False) for i in range(280)]
    items.extend(candidate(f"target-{i}") for i in range(4))
    queue(worker, items)
    before = set(worker.detail_jobs)
    selected = plan(worker, monkeypatch)
    assert Counter(item.scope for item in selected) == {
        "target": 4,
        "needs_confirmation": 46,
    }
    assert [item.scope for item in selected[:4]] == ["target"] * 4
    assert set(worker.detail_jobs) == before and len(before) == 284
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == 284


@pytest.mark.parametrize("fast", [False, True])
def test_verification_gets_bounded_service_under_continuous_target_backlog(
    db, monkeypatch, fast
):
    worker = ConfiguredCollector(db, settings(detail_limit=50, detail_cache_hours=0))
    worker.fast = fast
    queue(
        worker,
        [candidate(f"target-{i}") for i in range(60)]
        + [candidate(f"pending-{i}", target=False) for i in range(60)],
    )
    selected = plan(worker, monkeypatch)
    assert [item.scope for item in selected[:4]] == ["target"] * 4
    budget = 25 if fast else 50
    assert len(selected) == budget
    assert Counter(item.scope for item in selected) == {
        "target": budget * 4 // 5,
        "needs_confirmation": budget // 5,
    }


@pytest.mark.parametrize("fast", [False, True])
def test_budget_one_restarts_rotate_targets_and_still_verify_pending(
    db, monkeypatch, fast
):
    items = [
        candidate("a-target", "a.invalid"),
        candidate("b-target", "b.invalid"),
        candidate("c-pending", "c.invalid", target=False),
        candidate("d-pending", "d.invalid", target=False),
    ]
    config = settings(detail_limit=1, detail_cache_hours=0)
    selected = []
    for unused in range(10):
        worker = ConfiguredCollector(db, config)
        worker.fast = fast
        queue(worker, items)

        def response(url, worker=worker):
            selected.append(worker.detail_jobs[url][2])
            return Response(url, url, 200, None, now(), error="controlled_no_body")

        monkeypatch.setattr(worker.fetcher, "get", response)
        worker.details(db.start_run())
    assert [item.scope for item in selected[:5]] == ["target"] * 4 + [
        "needs_confirmation"
    ]
    assert Counter(item.scope for item in selected) == {
        "target": 8,
        "needs_confirmation": 2,
    }
    assert {item.source for item in selected if item.scope == "target"} == {
        "dealer:a.invalid",
        "dealer:b.invalid",
    }
    assert {item.source for item in selected if item.scope != "target"} == {
        "dealer:c.invalid",
        "dealer:d.invalid",
    }


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("limit", [0, 2])
def test_planning_keeps_cursor_and_unknown_country_queue(db, monkeypatch, fast, limit):
    worker = ConfiguredCollector(db, settings(detail_limit=limit))
    worker.fast = fast
    queue(
        worker,
        [
            candidate("target"),
            candidate("pending", target=False),
            candidate("unknown", country=None),
        ],
    )
    db.conn.execute(
        "INSERT OR REPLACE INTO monitor_state VALUES ('detail_queue_cursor',?)",
        (json.dumps({"source": "dealer:first.invalid", "verification_phase": 4}),),
    )
    before = list(
        db.conn.execute(
            "SELECT key,value FROM monitor_state WHERE key LIKE 'pending:%' OR key='detail_queue_cursor' ORDER BY key"
        )
    )
    assert len(before) == 2
    selected = plan(worker, monkeypatch)
    assert len(selected) == limit
    assert all(item.raw["market_country"] == "NL" for item in selected)
    assert (
        list(
            db.conn.execute(
                "SELECT key,value FROM monitor_state WHERE key LIKE 'pending:%' OR key='detail_queue_cursor' ORDER BY key"
            )
        )
        == before
    )
    assert len(worker.detail_jobs) == 2


@pytest.mark.parametrize("status", [403, 429])
def test_priority_cannot_bypass_source_denial_or_fresh_detail_cache(
    db, monkeypatch, status
):
    worker = ConfiguredCollector(db, settings(detail_limit=5))
    items = [
        candidate("blocked", "a.invalid"),
        candidate("cached", "b.invalid"),
        candidate("pending", "c.invalid", target=False),
    ]
    queue(worker, items)
    for item, code in zip(items[:2], [status, 200]):
        db.save_fetch(
            db.start_run(),
            item.source,
            item.url,
            item.url,
            code,
            "own retained body",
            now(),
            error="http_denied" if code != 200 else None,
        )
    selected = plan(worker, monkeypatch)
    assert [item.external_id for item in selected] == ["pending"]
    assert worker.detail_stats[items[0].source]["backoff"] == 1
    assert set(worker.detail_jobs) == {item.url for item in items}


def test_legacy_collector_keeps_existing_source_rotation(db, monkeypatch):
    worker = Collector(db, {"detail_limit": 2, "workers": 1})
    items = [
        candidate("pending", "a.invalid", target=False),
        candidate("target", "b.invalid"),
    ]
    queue(worker, items)
    assert [item.external_id for item in plan(worker, monkeypatch)] == [
        "pending",
        "target",
    ]
