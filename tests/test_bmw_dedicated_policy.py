"""Retained native dealer inventory versus direct VDP policy across runs."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.germany_dealer_inventory import WIVEDA_ROOT, WIVEDA_SOURCE
from car_market.germany_mobile import DEALER_SEARCH, DEALER_SOURCE

FIXTURES = Path(__file__).parent / "fixtures"
SOURCES = [DEALER_SOURCE, WIVEDA_SOURCE]
CLOCK = "2026-10-08T05:30:00+00:00"


def native_query(source, key=None):
    if source == DEALER_SOURCE:
        return {
            "source": source,
            "key": key or "native_strauss",
            "url": DEALER_SEARCH,
            "max_details": 14,
        }
    return {
        "source": source,
        "key": key or "native_wiveda",
        "url": WIVEDA_ROOT,
        "inventory_contract": "wiveda_used_218_active",
        "max_pages": 3,
        "max_details": 2,
        "refresh_root": False,
    }


def native_routes(source):
    routes = {}
    if source == DEALER_SOURCE:
        folder = FIXTURES / "germany_mobile"
        manifest = json.loads((folder / "recovery-provenance.json").read_text())
        rows = manifest if isinstance(manifest, list) else manifest["captures"]
        for row in rows:
            body = (folder / row["fixture"]).read_text()
            assert hashlib.sha256(body.encode()).hexdigest() == row["fixture_sha256"]
            routes[row["metadata"]["requested_url"]] = (
                body,
                row["metadata"]["fetched_at"],
            )
    else:
        folder = FIXTURES / "germany_dealer_inventory"
        manifest = json.loads((folder / "manifest.json").read_text())
        for name in ["root", "p2", "detail", "manual"]:
            filename = f"wiveda-{name}-bundle.json"
            body = (folder / filename).read_text()
            row = next(row for row in manifest if row["fixture"] == filename)
            assert hashlib.sha256(body.encode()).hexdigest() == row["fixture_sha256"]
            bundle = json.loads(body)
            routes[bundle["html_url"]] = (
                bundle["html"],
                row["html_metadata"]["fetched_at"],
            )
            routes[bundle["payload_url"]] = (
                json.dumps(bundle["payload"]),
                row["metadata"]["fetched_at"],
            )
    return routes


def is_detail(url):
    return "/fahrzeug/" in url or (
        "auto-straussberger.de" in url and "/suche/" not in url
    )


def setup_native(tmp_path, monkeypatch, sources, **config):
    monkeypatch.setattr("car_market.bmw_detail_policy.now", lambda: CLOCK)
    queries = [native_query(source) for source in sources]
    config = {
        "scope": {"country": "DE"},
        "queries": queries,
        "workers": 1,
        "detail_cache_hours": 6,
        "detail_error_backoff_hours": 6,
        **config,
    }
    routes = {
        url: value for source in sources for url, value in native_routes(source).items()
    }
    calls, failures = [], {}
    db = Database(tmp_path / "market.sqlite3")

    def get(url, refresh=False):
        calls.append((url, refresh))
        body, stamp = routes[url]
        if url in failures:
            return Response(
                url, url, failures[url], "Refused", CLOCK, "http_" + str(failures[url])
            )
        return Response(url, url, 200, body, stamp, transport="retained_native_offline")

    getter = SimpleNamespace(get=get)
    collector = Collector(db, config)
    collector.fetcher = getter
    return db, collector, calls, queries, routes, failures, getter


def collect(collector, query, *, fast=False):
    collector.fast = fast
    run = collector.db.start_run("fast" if fast else "live")
    collector.collect_query(run, query)
    return run


def ledger(db, query):
    return json.loads(
        db.conn.execute(
            "SELECT value FROM monitor_state WHERE key=?",
            ("bmw_dedicated:" + query["key"],),
        ).fetchone()[0]
    )


def vdp_calls(calls):
    return [url for url, _ in calls if is_detail(url)]


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("cap", [0, 1, 2])
def test_actual_detail_HTTP_respects_global_full_fast_cap(
    tmp_path, monkeypatch, source, fast, cap
):
    db, collector, calls, queries, *_ = setup_native(
        tmp_path,
        monkeypatch,
        [source],
        detail_limit=cap,
        detail_batch_limit=10,
        fast_detail_limit=2,
    )
    run = collect(collector, queries[0], fast=fast)
    count = len(vdp_calls(calls))
    assert count <= cap
    assert collector.specialized_detail_policy(run).used == count
    proof = ledger(db, queries[0])
    entries = proof.get("frontier", proof.get("census"))
    assert len(entries) == (14 if source == DEALER_SOURCE else 21)
    assert all(entry.get("search_observed_at") for entry in entries)
    assert count == (cap if source == DEALER_SOURCE else cap - cap % 2)
    assert (
        db.conn.execute(
            "SELECT status FROM source_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        == "partial"
    )
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("source", SOURCES)
def test_run_no_details_keeps_native_census_without_any_VDP(
    tmp_path, monkeypatch, source
):
    db, collector, calls, queries, *_ = setup_native(tmp_path, monkeypatch, [source])
    monkeypatch.setattr(
        "car_market.germany_identity.bridge_de_publications",
        lambda db: {"conflicts": 0},
    )
    monkeypatch.setattr(
        "car_market.identity_merge.bridge_shared_publications",
        lambda db: {"conflicts": 0},
    )
    collector.run(details=False, checks=False)
    assert not vdp_calls(calls)
    assert collector.specialized_details_enabled is True
    proof = ledger(db, queries[0])
    entries = proof.get("frontier", proof.get("census"))
    assert len(entries) == (14 if source == DEALER_SOURCE else 21)
    records = proof.get("frontier", proof.get("details"))
    assert records and all(record["reason"] == "details_disabled" for record in records)


@pytest.mark.parametrize("source", SOURCES)
def test_query_zero_retains_frontier_without_network_details(
    tmp_path, monkeypatch, source
):
    db, collector, calls, queries, *_ = setup_native(tmp_path, monkeypatch, [source])
    queries[0]["max_details"] = 0
    collect(collector, queries[0])
    assert not vdp_calls(calls)
    proof = ledger(db, queries[0])
    assert len(proof.get("frontier", proof.get("census"))) == (
        14 if source == DEALER_SOURCE else 21
    )


@pytest.mark.parametrize("source", SOURCES)
def test_restart_accepted_cache_retains_exact_fetch_ID_and_clock(
    tmp_path, monkeypatch, source
):
    db, collector, calls, queries, _, _, getter = setup_native(
        tmp_path, monkeypatch, [source]
    )
    collect(collector, queries[0])
    first = ledger(db, queries[0])
    records = first.get("frontier", first.get("details"))
    accepted = {
        r["id"]: (r["fetch_id"], r["observed_at"])
        for r in records
        if r["status"] == "accepted"
    }
    assert len(accepted) == (14 if source == DEALER_SOURCE else 2)
    before = {
        name: db.conn.execute(f"SELECT * FROM {name} ORDER BY id").fetchall()
        for name in ["observations", "fetches", "events"]
    }
    count = len(vdp_calls(calls))
    restarted = Collector(db, {**collector.config, "detail_limit": 0})
    restarted.fetcher = getter
    collect(restarted, queries[0])
    assert len(vdp_calls(calls)) == count
    current = ledger(db, queries[0])
    records = current.get("frontier", current.get("details"))
    cached = {
        r["id"]: (r["fetch_id"], r["observed_at"])
        for r in records
        if r["status"] == "accepted"
    }
    assert cached == accepted
    assert all(r["cached"] for r in records if r["status"] == "accepted")
    # New search evidence is allowed; all previously persisted history is immutable.
    for name, rows in before.items():
        assert (
            rows
            == db.conn.execute(
                f"SELECT * FROM {name} WHERE id<=? ORDER BY id", (rows[-1]["id"],)
            ).fetchall()
            if rows
            else True
        )
    assert restarted.fetcher is getter


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("revision", ["source", "global", "ttl"])
def test_revision_or_expired_proof_cannot_satisfy_zero_budget(
    tmp_path, monkeypatch, source, revision
):
    db, collector, calls, queries, _, _, getter = setup_native(
        tmp_path, monkeypatch, [source]
    )
    collect(collector, queries[0])
    restarted = Collector(db, {**collector.config, "detail_limit": 0})
    restarted.fetcher = getter
    if revision == "ttl":
        monkeypatch.setattr(
            "car_market.bmw_detail_policy.now", lambda: "2026-10-08T12:00:00Z"
        )
    elif revision == "source":
        restarted.source_parser_refresh_since[source] = CLOCK
    else:
        restarted.parser_refresh_since = CLOCK
    count = len(vdp_calls(calls))
    collect(restarted, queries[0])
    assert len(vdp_calls(calls)) == count
    proof = ledger(db, queries[0])
    records = proof.get("frontier", proof.get("details"))
    assert records and not any(r["status"] == "accepted" for r in records)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("phase", ["within_pass", "persisted", "foreign_host"])
def test_actual_host_refusal_controls_later_VDPs(
    tmp_path, monkeypatch, source, status, phase
):
    db, collector, calls, queries, routes, failures, getter = setup_native(
        tmp_path, monkeypatch, [source]
    )
    own = next(url for url in routes if is_detail(url))
    if phase == "within_pass":
        failures[own] = status
    else:
        target = own if phase == "persisted" else "https://foreign.example/blocked"
        db.save_fetch(
            db.start_run(),
            source,
            target,
            target,
            status,
            "Refused",
            CLOCK,
            "offline",
            "http_" + str(status),
        )
    collect(collector, queries[0])
    count = len(vdp_calls(calls))
    assert count == (
        0
        if phase == "persisted"
        else 1
        if phase == "within_pass"
        else 14
        if source == DEALER_SOURCE
        else 4
    )
    assert collector.fetcher is getter
    if phase == "within_pass":
        failures.clear()
        restarted = Collector(db, collector.config)
        restarted.fetcher = getter
        collect(restarted, queries[0])
        assert len(vdp_calls(calls)) == count
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("first", SOURCES)
@pytest.mark.parametrize("fast", [False, True])
def test_direct_sources_and_later_generic_share_remaining_HTTP(
    tmp_path, monkeypatch, first, fast
):
    sources = [first, next(source for source in SOURCES if source != first)]
    db, collector, calls, queries, *_ = setup_native(
        tmp_path, monkeypatch, sources, detail_limit=2
    )
    collector.fast = fast
    run = db.start_run("fast" if fast else "live")
    for query in queries:
        collector.collect_query(run, query)
    assert len(vdp_calls(calls)) == 2
    assert collector.specialized_detail_policy(run).remaining() == 0
    rows = db.conn.execute(
        "SELECT l.id,o.details_json FROM listings l JOIN latest_observations o ON o.listing_id=l.id WHERE l.scope IN ('target','needs_confirmation')"
    ).fetchall()
    from car_market.parse import Listing

    for row in rows:
        collector.queue_details(Listing(**json.loads(row["details_json"])), row["id"])
    count = len(calls)
    collector.details(run)
    assert len(calls) == count


@pytest.mark.parametrize("source", SOURCES)
def test_later_unaccepted_same_body_200_is_pending_under_zero_budget(
    tmp_path, monkeypatch, source
):
    db, collector, calls, queries, _, _, getter = setup_native(
        tmp_path, monkeypatch, [source]
    )
    collect(collector, queries[0])
    proof = ledger(db, queries[0])
    accepted = next(
        r
        for r in proof.get("frontier", proof.get("details"))
        if r["status"] == "accepted"
    )
    original = db.conn.execute(
        "SELECT * FROM fetches WHERE id=?", (accepted["fetch_id"],)
    ).fetchone()
    from car_market.germany_retained import _raw_body

    body = _raw_body(db.path.parent, original["raw_path"], original["content_hash"])
    new_id = db.save_fetch(
        db.start_run(),
        source,
        original["url"],
        original["final_url"],
        200,
        body,
        CLOCK,
        "unaccepted_test_200",
        None,
        retain_raw=False,
    )
    assert new_id != accepted["fetch_id"]
    restarted = Collector(db, {**collector.config, "detail_limit": 0})
    restarted.fetcher = getter
    count = len(vdp_calls(calls))
    collect(restarted, queries[0])
    proof = ledger(db, queries[0])
    current = next(
        r
        for r in proof.get("frontier", proof.get("details"))
        if r["id"] == accepted["id"]
    )
    assert (
        current["status"] == "deferred"
        and current["reason"] == "detail_budget_deferred"
    )
    assert len(vdp_calls(calls)) == count
    assert not db.conn.execute(
        "SELECT id FROM observations WHERE fetch_id=?", (new_id,)
    ).fetchall()


@pytest.mark.parametrize("stage", ["html", "payload"])
@pytest.mark.parametrize("status", [403, 429])
def test_wiveda_first_detail_refusal_persists_only_actual_attempts(
    tmp_path, monkeypatch, stage, status
):
    db, collector, calls, queries, routes, failures, _ = setup_native(
        tmp_path, monkeypatch, [WIVEDA_SOURCE]
    )
    url = next(
        url
        for url in routes
        if is_detail(url) and ("_payload.json" in url) == (stage == "payload")
    )
    failures[url] = status
    run = collect(collector, queries[0])
    count = len(vdp_calls(calls))
    assert count == (1 if stage == "html" else 2)
    assert collector.specialized_detail_policy(run).used == count
    assert (
        db.conn.execute(
            "SELECT count(*) FROM fetches WHERE url=? AND status=?", (url, status)
        ).fetchone()[0]
        == 1
    )
    records = ledger(db, queries[0])["details"]
    assert all(record["status"] != "accepted" for record in records)
    assert collector.fetcher.get is not None


@pytest.mark.parametrize("source", SOURCES)
def test_refused_search_keeps_previous_census_with_old_clocks(
    tmp_path, monkeypatch, source
):
    db, collector, calls, queries, _, failures, getter = setup_native(
        tmp_path, monkeypatch, [source], detail_limit=0
    )
    collect(collector, queries[0])
    first = ledger(db, queries[0])
    field = "frontier" if source == DEALER_SOURCE else "census"
    failures[queries[0]["url"]] = 403
    restarted = Collector(db, collector.config)
    restarted.fetcher = getter
    collect(restarted, queries[0])
    current = ledger(db, queries[0])
    # A rejected query cannot renew any of the previous ID/clock bindings.
    assert current.get("retained_" + field, current.get(field)) == first[field]
    assert not vdp_calls(calls)


@pytest.mark.parametrize("source", SOURCES)
def test_search_queries_do_not_consume_VDP_allowance(tmp_path, monkeypatch, source):
    db, collector, calls, queries, *_ = setup_native(
        tmp_path, monkeypatch, [source], detail_limit=0
    )
    run = collect(collector, queries[0])
    assert len(calls) == (1 if source == DEALER_SOURCE else 4)
    assert collector.specialized_detail_policy(run).used == 0
    records = ledger(db, queries[0]).get(
        "frontier", ledger(db, queries[0]).get("census")
    )
    for record in records:
        row = db.conn.execute(
            "SELECT * FROM fetches WHERE id=?", (record["search_fetch_id"],)
        ).fetchone()
        assert row["source"] == source and row["status"] == 200
        assert row["fetched_at"] == record["search_observed_at"]


def test_wiveda_new_native_price_conflict_cannot_restore_cached_old_detail(
    tmp_path, monkeypatch
):
    db, collector, calls, queries, routes, _, getter = setup_native(
        tmp_path, monkeypatch, [WIVEDA_SOURCE]
    )
    collect(collector, queries[0])
    folder = FIXTURES / "germany_dealer_inventory"
    root = json.loads((folder / "wiveda-root-bundle.json").read_text())
    identity = "9ec24de5-36ee-4387-a4bc-b942bfc27e23"
    node = next(
        v
        for v in root["payload"]
        if isinstance(v, dict)
        and isinstance(v.get("id"), int)
        and root["payload"][v["id"]] == identity
    )
    price = 27400
    root["payload"].append(price)
    node["price"] = len(root["payload"]) - 1
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(root["html"], "html.parser")
    link = soup.select_one(f'a[href="/fahrzeug/{identity}"]')
    card = link.find_parent(attrs={"data-testid": "vehicle-card"})
    card.select_one('[data-testid="vehicle-card-price"]').string = "27.400 €"
    root["html"] = str(soup)
    routes[root["html_url"]] = (root["html"], CLOCK)
    routes[root["payload_url"]] = (json.dumps(root["payload"]), CLOCK)
    restarted = Collector(db, {**collector.config, "detail_limit": 0})
    restarted.fetcher = getter
    count = len(vdp_calls(calls))
    collect(restarted, queries[0])
    assert len(vdp_calls(calls)) == count
    records = ledger(db, queries[0])["details"]
    current = next(r for r in records if r["stock_id"] == identity)
    assert (
        current["status"] == "deferred"
        and current["reason"] == "detail_budget_deferred"
    )
    latest = db.conn.execute(
        "SELECT price_eur FROM latest_observations o JOIN listings l ON l.id=o.listing_id WHERE l.source=? AND l.external_id=?",
        (WIVEDA_SOURCE, "https://wiveda.de/fahrzeug/" + identity),
    ).fetchone()
    assert latest[0] == price


@pytest.mark.parametrize("source", SOURCES)
def test_interrupt_preserves_every_search_ID_and_restores_getter(
    tmp_path, monkeypatch, source
):
    db, collector, _calls, queries, _, _, getter = setup_native(
        tmp_path, monkeypatch, [source]
    )
    original = getter.get

    def interrupted(url, refresh=False):
        if is_detail(url) and (source == DEALER_SOURCE or "_payload.json" in url):
            raise KeyboardInterrupt("isolated control")
        return original(url, refresh=refresh)

    getter.get = interrupted
    run = db.start_run()
    with pytest.raises(KeyboardInterrupt, match="isolated control"):
        collector.collect_query(run, queries[0])
    assert collector.fetcher is getter
    current = ledger(db, queries[0])
    entries = current.get("frontier", current.get("census"))
    assert len(entries) == (14 if source == DEALER_SOURCE else 21)
    assert collector.specialized_detail_policy(run).used == (
        1 if source == DEALER_SOURCE else 2
    )
    records = current.get("frontier", current.get("details"))
    assert not any(record["status"] == "accepted" for record in records)
    assert all(entry["search_observed_at"] for entry in entries)
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("source", SOURCES)
def test_native_accepted_observations_bind_every_proof_to_original_capture(
    tmp_path, monkeypatch, source
):
    db, collector, _calls, queries, *_ = setup_native(tmp_path, monkeypatch, [source])
    collect(collector, queries[0])
    records = ledger(db, queries[0])
    accepted = [
        r
        for r in records.get("frontier", records.get("details"))
        if r["status"] == "accepted"
    ]
    assert len(accepted) == (14 if source == DEALER_SOURCE else 2)
    from car_market.germany_retained import _raw_body

    for proof in accepted:
        fetch = db.conn.execute(
            "SELECT * FROM fetches WHERE id=?", (proof["fetch_id"],)
        ).fetchone()
        assert fetch["source"] == source and fetch["status"] == 200
        assert fetch["fetched_at"] == proof["observed_at"]
        body = _raw_body(db.path.parent, fetch["raw_path"], fetch["content_hash"])
        assert hashlib.sha256(body.encode()).hexdigest() == fetch["content_hash"]
        rows = db.conn.execute(
            "SELECT o.observed_at,l.external_id,o.details_json FROM observations o JOIN listings l ON l.id=o.listing_id WHERE o.fetch_id=?",
            (proof["fetch_id"],),
        ).fetchall()
        assert rows
        for row in rows:
            assert (
                row["observed_at"] == proof["observed_at"]
                and row["external_id"] == proof["id"]
            )
            detail = json.loads(row["details_json"])
            assert detail["source"] == source and detail["external_id"] == proof["id"]
            assert detail["raw"]["market_country"] == "DE"
    assert db.conn.execute("SELECT count(*) FROM listings").fetchone()[0] == (
        14 if source == DEALER_SOURCE else 17
    )
    assert (
        db.conn.execute(
            "SELECT count(*) FROM vehicles WHERE kenteken IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    assert (
        db.conn.execute(
            "SELECT count(*) FROM events WHERE kind='not_seen_in_complete_query'"
        ).fetchone()[0]
        == 0
    )
