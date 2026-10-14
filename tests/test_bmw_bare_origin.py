"""Native getter refusals bind the parsed origin, including an empty URL path."""

import importlib.util
from pathlib import Path

import pytest

from car_market.bmw_detail_policy import DetailDeferred
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.germany_dealer_inventory import WIVEDA_SOURCE
from car_market.germany_mobile import DEALER_SOURCE
from car_market.provenance import timestamp

SPEC = importlib.util.spec_from_file_location(
    "bare_origin_native_setup", Path(__file__).with_name("test_bmw_dedicated_policy.py")
)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)

ORIGINS = [
    "https://wiveda.de",
    "https://wiveda.de:443",
    "HTTPS://WIVEDA.DE?native=1",
    "https://WIVEDA.DE:443#blocked",
    "https://wiveda.de:0443?native=1#blocked",
    "https://wiveda.de/?native=1#blocked",
]


@pytest.mark.parametrize("final", ORIGINS)
@pytest.mark.parametrize("status", [403, 429])
def test_bare_origin_refusal_blocks_native_same_pass_and_reopened_getter(
    tmp_path, monkeypatch, final, status
):
    db, collector, calls, queries, _, _, getter = native.setup_native(
        tmp_path, monkeypatch, [DEALER_SOURCE, WIVEDA_SOURCE], detail_limit=3
    )
    request = next(
        url for url in native.native_routes(DEALER_SOURCE) if native.is_detail(url)
    )
    original = getter.get

    def refused(url, refresh=False):
        if url == request:
            calls.append((url, refresh))
            return Response(
                url,
                final,
                status,
                "Synthetic refusal",
                native.CLOCK,
                "http_" + str(status),
            )
        return original(url, refresh=refresh)

    getter.get = refused
    queries[0]["max_details"] = 1
    run = db.start_run()
    collector.collect_query(run, queries[0])
    refusal = db.conn.execute(
        "SELECT * FROM fetches WHERE status=?", (status,)
    ).fetchone()
    saved = tuple(refusal)
    before = len(calls)
    collector.collect_query(run, queries[1])
    assert len(calls[before:]) == 4 and not native.vdp_calls(calls[before:])
    assert collector.specialized_detail_policy(run).used == 1
    assert (
        tuple(
            db.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (refusal["id"],)
            ).fetchone()
        )
        == saved
    )
    assert len(native.ledger(db, queries[1])["census"]) == 21
    path = db.path
    db.conn.close()
    db = Database(path)
    restarted = Collector(db, collector.config)
    restarted.fetcher = getter
    before = len(calls)
    later = native.collect(restarted, queries[1])
    assert len(calls[before:]) == 4 and not native.vdp_calls(calls[before:])
    assert restarted.specialized_detail_policy(later).used == 0
    assert (
        tuple(
            db.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (refusal["id"],)
            ).fetchone()
        )
        == saved
    )
    deferred = native.ledger(db, queries[1])["details"]
    assert deferred and all(
        r["reason"] == "detail_host_backoff:" + str(status)
        and r["fetch_id"] == refusal["id"]
        for r in deferred
    )
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "final,target,blocked",
    [
        ("https://wiveda.de?x=1#p", "https://WIVEDA.DE:443/next", True),
        ("https://wiveda.de:443#p", "https://wiveda.de?next=1", True),
        ("https://WIVEDA.DE:0443", "https://wiveda.de/next", True),
        ("http://wiveda.de:80", "http://WIVEDA.DE/next", True),
        ("https://[::1]:443?x=1", "https://[::1]/next", True),
        ("https://wiveda.de:8443", "https://wiveda.de/next", False),
        ("https://wiveda.de", "https://wiveda.de:8443/next", False),
        ("http://wiveda.de", "https://wiveda.de/next", False),
        ("https://wiveda.de", "http://wiveda.de/next", False),
        ("https://wiveda.de.evil.example?x=1", "https://wiveda.de/next", False),
        ("https://sub.wiveda.de#p", "https://wiveda.de/next", False),
        (
            "https://foreign.example?next=https://wiveda.de",
            "https://wiveda.de/next",
            False,
        ),
        ("https://foreign.example#https://wiveda.de", "https://wiveda.de/next", False),
        ("https://wiveda.de@foreign.example", "https://wiveda.de/next", False),
        ("https://wiveda.de:invalid", "https://wiveda.de/next", False),
    ],
)
def test_origin_equivalence_preserves_exact_other_boundaries(
    tmp_path, monkeypatch, final, target, blocked
):
    db, collector, calls, _, *_ = native.setup_native(
        tmp_path, monkeypatch, [WIVEDA_SOURCE]
    )
    run = db.start_run()
    refusal = db.save_fetch(
        run,
        DEALER_SOURCE,
        "https://original.example/request",
        final,
        429,
        "Synthetic refusal",
        native.CLOCK,
        "offline",
        "http_429",
    )
    before = tuple(
        db.conn.execute("SELECT * FROM fetches WHERE id=?", (refusal,)).fetchone()
    )
    policy = collector.specialized_detail_policy(run)
    if blocked:
        with pytest.raises(DetailDeferred) as error:
            policy._host_backoff(WIVEDA_SOURCE, target, timestamp(native.CLOCK))
        assert error.value.capture["fetch_id"] == refusal
    else:
        policy._host_backoff(WIVEDA_SOURCE, target, timestamp(native.CLOCK))
    assert (
        tuple(
            db.conn.execute("SELECT * FROM fetches WHERE id=?", (refusal,)).fetchone()
        )
        == before
    )
    assert not calls


def test_newer_unrelated_refusal_cannot_hide_older_matching_bare_origin(
    tmp_path, monkeypatch
):
    db, collector, calls, _, *_ = native.setup_native(
        tmp_path, monkeypatch, [WIVEDA_SOURCE]
    )
    run = db.start_run()
    older = db.save_fetch(
        run,
        DEALER_SOURCE,
        "https://original.example/request",
        "https://wiveda.de",
        403,
        "Refused",
        "2026-10-08T05:29:59Z",
        "offline",
        "http_403",
    )
    db.save_fetch(
        run,
        DEALER_SOURCE,
        "https://foreign.example/request",
        "https://foreign.example",
        429,
        "Refused",
        native.CLOCK,
        "offline",
        "http_429",
    )
    with pytest.raises(DetailDeferred) as error:
        collector.specialized_detail_policy(run)._host_backoff(
            WIVEDA_SOURCE, "https://wiveda.de/next", timestamp(native.CLOCK)
        )
    assert error.value.capture["fetch_id"] == older
    assert not calls
