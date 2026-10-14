"""Native regression controls for retained frontiers, membership and host refusals."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from car_market.bmw_dedicated import persist_frontier
from car_market.bmw_detail_policy import DetailDeferred
from car_market.collect import Collector
from car_market.germany_dealer_inventory import WIVEDA_SOURCE
from car_market.germany_mobile import DEALER_SOURCE

SPEC = importlib.util.spec_from_file_location(
    "dedicated_native_setup", Path(__file__).with_name("test_bmw_dedicated_policy.py")
)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


def restart(db, collector, getter, **config):
    current = Collector(db, {**collector.config, **config})
    current.fetcher = getter
    return current


@pytest.mark.parametrize("phase", ["html", "payload"])
@pytest.mark.parametrize("status", [403, 429])
def test_partial_page_retains_old_proofs_but_not_current_counts_or_membership(
    tmp_path, monkeypatch, phase, status
):
    db, collector, calls, queries, routes, failures, getter = native.setup_native(
        tmp_path, monkeypatch, [WIVEDA_SOURCE], detail_limit=0
    )
    native.collect(collector, queries[0])
    before = native.ledger(db, queries[0])
    old = {r["stock_id"]: r for r in before["census"]}
    refused = next(
        url
        for url in routes
        if "page=2" in url and ("_payload.json" in url) == (phase == "payload")
    )
    failures[refused] = status
    for url, (body, stamp) in list(routes.items()):
        if "page=2" not in url and not native.is_detail(url):
            routes[url] = (body, native.CLOCK)
    # Repeated partial attempts must carry the same older references, not credit them again.
    for _ in range(2):
        current = restart(db, collector, getter)
        run = native.collect(current, queries[0])
        state = native.ledger(db, queries[0])
        seen = {r["stock_id"] for r in state["census"]}
        held = {r["stock_id"]: r for r in state["retained_census"]}
        assert len(seen) == 12 and len(held) == 9 and not (seen & held.keys())
        assert set(old) == seen | held.keys()
        for identity, record in held.items():
            assert record == old[identity]
        assert not state["census_complete"]
        health = db.conn.execute(
            "SELECT received_count,status FROM source_runs WHERE run_id=?", (run,)
        ).fetchone()
        assert tuple(health) == (12, "partial")
        credited = db.conn.execute(
            "SELECT l.external_id FROM query_membership m JOIN listings l ON l.id=m.listing_id "
            "WHERE m.source=? AND m.query_key=? AND m.last_run_id=?",
            (WIVEDA_SOURCE, queries[0]["key"], run),
        ).fetchall()
        assert all(row[0].rsplit("/", 1)[-1] in seen for row in credited)
        assert not native.vdp_calls(calls)
        for record in held.values():
            fetch = db.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (record["search_fetch_id"],)
            ).fetchone()
            assert fetch["fetched_at"] == record["search_observed_at"]
    failures.clear()
    queries[0]["refresh_root"] = True
    current = restart(db, collector, getter)
    native.collect(current, queries[0])
    recovered = native.ledger(db, queries[0])
    assert recovered["census_complete"] and len(recovered["census"]) == 21
    assert not recovered.get("retained_census") and not recovered.get(
        "retained_details"
    )
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


def test_partial_page_keeps_prior_pending_detail_reference_separate(
    tmp_path, monkeypatch
):
    db, collector, _, queries, routes, failures, getter = native.setup_native(
        tmp_path, monkeypatch, [WIVEDA_SOURCE]
    )
    native.collect(collector, queries[0])
    old = native.ledger(db, queries[0])
    root_ids = {r["stock_id"] for r in old["census"][:12]}
    previous = next(r for r in old["details"] if r["stock_id"] not in root_ids)
    failures[
        next(url for url in routes if "page=2" in url and "_payload" not in url)
    ] = 403
    current = restart(db, collector, getter, detail_limit=0)
    run = native.collect(current, queries[0])
    state = native.ledger(db, queries[0])
    retained = next(
        r for r in state["retained_details"] if r["stock_id"] == previous["stock_id"]
    )
    assert retained == previous
    assert all(r["stock_id"] != previous["stock_id"] for r in state["census"])
    assert (
        db.conn.execute(
            "SELECT received_count FROM source_runs WHERE run_id=?", (run,)
        ).fetchone()[0]
        == 12
    )


def test_retained_missing_ID_is_resolved_only_by_complete_enumeration(
    tmp_path, monkeypatch
):
    db, collector, _, queries, *_ = native.setup_native(
        tmp_path, monkeypatch, [WIVEDA_SOURCE], detail_limit=0
    )
    native.collect(collector, queries[0])
    old = copy.deepcopy(native.ledger(db, queries[0]))
    disappeared = old["census"][-1]
    # A controlled 20-ID current census, derived exclusively from retained native IDs.
    current = {
        "source": WIVEDA_SOURCE,
        "census": old["census"][:-1],
        "details": [],
        "census_complete": False,
    }
    persist_frontier(collector, queries[0]["key"], current)
    assert native.ledger(db, queries[0])["retained_census"] == [disappeared]
    current["census_complete"] = True
    persist_frontier(collector, queries[0]["key"], current)
    state = native.ledger(db, queries[0])
    assert len(state["census"]) == 20 and not state.get("retained_census")
    assert disappeared not in state["census"]


def test_cached_membership_credits_only_current_native_IDs_and_changes_no_clocks(
    tmp_path, monkeypatch
):
    from car_market.coverage import report

    db, collector, calls, queries, routes, _, getter = native.setup_native(
        tmp_path, monkeypatch, [DEALER_SOURCE]
    )
    native.collect(collector, queries[0])
    old = {
        name: [
            tuple(r) for r in db.conn.execute("SELECT * FROM " + name + " ORDER BY id")
        ]
        for name in ["observations", "events", "listings"]
    }
    first = native.ledger(db, queries[0])
    proofs = {r["id"]: (r["fetch_id"], r["observed_at"]) for r in first["frontier"]}
    body, _stamp = routes[queries[0]["url"]]
    state = json.JSONDecoder().raw_decode(body.split(" = ")[1])[0]
    listing = state["preload_data"]["settings"]["listify"]["listings"][:2]
    state["preload_data"]["settings"]["listify"]["listings"] = listing
    state["preload_data"]["settings"]["pager"]["total_rows"] = 2
    body = "<script>window.FWP_JSON = " + json.dumps(state) + ";</script>"
    routes[queries[0]["url"]] = (body, native.CLOCK)
    current = restart(db, collector, getter, detail_limit=0)
    before = len(calls)
    run = native.collect(current, queries[0])
    assert not native.vdp_calls(calls[before:])
    memberships = db.conn.execute(
        "SELECT l.external_id FROM query_membership m JOIN listings l ON l.id=m.listing_id "
        "WHERE m.source=? AND m.query_key=? AND m.last_run_id=?",
        (DEALER_SOURCE, queries[0]["key"], run),
    ).fetchall()
    wanted = {str(record["id"]) for record in listing}
    assert {r[0] for r in memberships} == wanted
    assert report(db.conn, current.config)["queries"][0]["target_ads"] == 2
    for name, rows in old.items():
        assert [
            tuple(r) for r in db.conn.execute("SELECT * FROM " + name + " ORDER BY id")
        ] == rows
    accepted = native.ledger(db, queries[0])["frontier"]
    assert {r["id"]: (r["fetch_id"], r["observed_at"]) for r in accepted} == {
        identity: proofs[identity] for identity in wanted
    }


@pytest.mark.parametrize(
    "final,target,stamp,status,blocked",
    [
        ("https://wiveda.de/denied", "https://wiveda.de/next", native.CLOCK, 403, True),
        (
            "https://wiveda.de:443/denied",
            "https://wiveda.de/next",
            native.CLOCK,
            429,
            True,
        ),
        (
            "https://WIVEDA.DE/denied",
            "https://wiveda.de:443/next",
            native.CLOCK,
            403,
            True,
        ),
        (
            "https://wiveda.de/denied",
            "https://wiveda.de/next",
            "2026-10-08T07:29:59+02:00",
            429,
            True,
        ),
        (
            "https://wiveda.de:8443/denied",
            "https://wiveda.de:8443/next",
            native.CLOCK,
            403,
            True,
        ),
        (
            "https://wiveda.de:8443/denied",
            "https://wiveda.de/next",
            native.CLOCK,
            403,
            False,
        ),
        (
            "https://wiveda.de/denied",
            "https://wiveda.de:8443/next",
            native.CLOCK,
            429,
            False,
        ),
        (
            "https://foreign.example/denied",
            "https://wiveda.de/next",
            native.CLOCK,
            403,
            False,
        ),
        (
            "https://wiveda.de.evil.example/denied",
            "https://wiveda.de/next",
            native.CLOCK,
            429,
            False,
        ),
        (
            "https://sub.wiveda.de/denied",
            "https://wiveda.de/next",
            native.CLOCK,
            403,
            False,
        ),
        ("http://wiveda.de/denied", "https://wiveda.de/next", native.CLOCK, 403, False),
        (
            "https://wiveda.de/denied",
            "https://wiveda.de/next",
            native.CLOCK,
            503,
            False,
        ),
        (
            "https://wiveda.de/denied",
            "https://wiveda.de/next",
            "2026-10-07T23:30:00Z",
            403,
            False,
        ),
        (
            "https://wiveda.de/denied",
            "https://wiveda.de/next",
            "2026-10-08T05:30:01Z",
            429,
            False,
        ),
    ],
)
def test_cross_source_actual_origin_and_clock_scope(
    tmp_path, monkeypatch, final, target, stamp, status, blocked
):
    from car_market.provenance import timestamp

    db, collector, calls, _, *_ = native.setup_native(
        tmp_path, monkeypatch, [WIVEDA_SOURCE]
    )
    run = db.start_run()
    request = next(
        url for url in native.native_routes(DEALER_SOURCE) if native.is_detail(url)
    )
    fetch = db.save_fetch(
        run,
        DEALER_SOURCE,
        request,
        final,
        status,
        "Injected failure",
        stamp,
        "controlled_redirect",
        "http_" + str(status),
    )
    policy = collector.specialized_detail_policy(run)
    if blocked:
        with pytest.raises(DetailDeferred, match="detail_host_backoff") as failure:
            policy._host_backoff(WIVEDA_SOURCE, target, timestamp(native.CLOCK))
        assert failure.value.capture["fetch_id"] == fetch
    else:
        policy._host_backoff(WIVEDA_SOURCE, target, timestamp(native.CLOCK))
    assert not calls
    stored = db.conn.execute(
        "SELECT source,url,final_url,fetched_at FROM fetches WHERE id=?", (fetch,)
    ).fetchone()
    assert tuple(stored) == (DEALER_SOURCE, request, final, stamp)


def test_cross_source_lookup_scans_blocked_index_not_10000_successes(
    tmp_path, monkeypatch
):
    from car_market.provenance import timestamp

    db, collector, _, _, *_ = native.setup_native(
        tmp_path, monkeypatch, [WIVEDA_SOURCE]
    )
    run = db.start_run()
    db.conn.executemany(
        "INSERT INTO fetches(run_id,source,url,final_url,fetched_at,status,transport) VALUES (?,?,?,?,?,?,?)",
        [
            (
                run,
                WIVEDA_SOURCE,
                "https://wiveda.de/ok/" + str(i),
                "https://wiveda.de/ok/" + str(i),
                native.CLOCK,
                200,
                "synthetic_success",
            )
            for i in range(10000)
        ],
    )
    request = next(
        url for url in native.native_routes(DEALER_SOURCE) if native.is_detail(url)
    )
    denied = db.save_fetch(
        run,
        DEALER_SOURCE,
        request,
        "https://wiveda.de/denied",
        429,
        "Injected refusal",
        "2026-10-08T07:29:59+02:00",
        "offline",
        "http_429",
    )
    db.conn.commit()
    statements = []
    db.conn.set_trace_callback(statements.append)
    with pytest.raises(DetailDeferred) as failure:
        collector.specialized_detail_policy(run)._host_backoff(
            WIVEDA_SOURCE, "https://wiveda.de/next", timestamp(native.CLOCK)
        )
    db.conn.set_trace_callback(None)
    assert failure.value.capture["fetch_id"] == denied
    query = next(
        sql
        for sql in statements
        if sql.startswith("SELECT * FROM fetches WHERE status")
    )
    plan = [row[3] for row in db.conn.execute("EXPLAIN QUERY PLAN " + query)]
    assert any("fetch_blocked_actual_time" in step for step in plan), plan
    assert not any(step.startswith("SCAN fetches") for step in plan), plan
