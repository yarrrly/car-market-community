"""Owned native link + recorded redirect + own VDP; controlled refusals only."""

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from car_market import collect
from car_market import db as db_module
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.parse import parse_page

ROOT = Path(__file__).parent / "fixtures/gaspedaal_alias_policy"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
MAP = MANIFEST["redirect_mappings"][0]
ALIAS, DIRECT = MAP["url"], MAP["final_url"]
OTHER = MANIFEST["redirect_mappings"][1]
CLOCK = datetime.fromisoformat("2026-10-12T23:00:00+00:00")


@pytest.fixture(autouse=True)
def clock_and_no_network(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CLOCK.astimezone(tz) if tz else CLOCK.replace(tzinfo=None)

    monkeypatch.setattr(collect, "datetime", Clock)
    for module in (collect, db_module):
        monkeypatch.setattr(module, "now", lambda: CLOCK.isoformat())
    monkeypatch.setattr(
        "car_market.trovit_detail_policy.now", lambda: CLOCK.isoformat()
    )
    monkeypatch.setattr(
        "car_market.gaspedaal_detail_policy.now", lambda: CLOCK.isoformat()
    )

    def deny(*args, **kwargs):
        raise AssertionError("No provider HTTP")

    monkeypatch.setattr("socket.socket.connect", deny)
    monkeypatch.setattr("socket.create_connection", deny)


def seed(directory):
    db = Database(directory / "market.sqlite3")
    run = db.start_run(timestamp=CLOCK.isoformat())
    items = {}
    for record in MANIFEST["records"]:
        capture = record["original_capture"]
        body = (ROOT / record["fixture"]).read_text()
        fetch = db.save_fetch(
            run,
            record["source"],
            capture["url"],
            capture["final_url"],
            200,
            body,
            capture["fetched_at"],
            transport="native_projection",
        )
        page = parse_page(record["source"], body, capture["final_url"])
        assert len(page.listings) == 1
        item = page.listings[0]
        identifier = db.observe(run, item, capture["fetched_at"], fetch)
        if record["source"] == "autowereld":
            items["direct"] = item, identifier
        elif any(link["url"] == ALIAS for link in item.links):
            items["alias"] = item, identifier
        elif any(link["url"] == OTHER["url"] for link in item.links):
            items["other"] = item, identifier
    for mapping in MANIFEST["redirect_mappings"]:
        db.save_fetch(
            run,
            mapping["source"],
            mapping["url"],
            mapping["final_url"],
            mapping["status"],
            None,
            mapping["fetched_at"],
            error=mapping["error"],
        )
    assert set(items) == {"alias", "direct", "other"}
    return db, run, items


def refusal(db, run, *, uri=DIRECT, final=DIRECT, status=403, age=300, stamp=None):
    return db.save_fetch(
        run,
        "autowereld",
        uri,
        final,
        status,
        None,
        stamp or (CLOCK - timedelta(seconds=age)).isoformat(),
        error="http_" + str(status),
    )


def history(db):
    rows = {
        t: [tuple(r) for r in db.conn.execute("SELECT * FROM " + t + " ORDER BY id")]
        for t in ("observations", "events", "fetches")
    }
    rows["field_verifications"] = [
        tuple(r)
        for r in db.conn.execute(
            "SELECT * FROM field_verifications ORDER BY listing_id,field"
        )
    ]
    return rows


def exercise(
    db,
    items,
    labels=("alias",),
    *,
    status=403,
    cap=5,
    fast=False,
    workers=1,
    cache_hours=24,
):
    before = history(db)
    worker = Collector(
        db,
        {
            "scope": {"country": "NL"},
            "workers": workers,
            "detail_cache_hours": cache_hours,
            "detail_error_backoff_hours": 6,
            "detail_limit": cap,
            "detail_batch_limit": 5,
            "fast_detail_limit": 5,
        },
    )
    worker.fast = fast
    for label in labels:
        item, listing = items[label]
        worker.queue_details(copy.deepcopy(item), listing)
    selected = {"alias": ALIAS, "direct": DIRECT, "other": OTHER["url"]}
    worker.detail_jobs = {
        selected[label]: worker.detail_jobs[selected[label]] for label in labels
    }
    calls = []

    def getter(uri, **kwargs):
        assert uri in {ALIAS, DIRECT, OTHER["url"]}
        calls.append(uri)
        final = {ALIAS: DIRECT, DIRECT: DIRECT, OTHER["url"]: OTHER["final_url"]}[uri]
        return Response(
            uri, final, status, None, CLOCK.isoformat(), error="http_" + str(status)
        )

    worker.fetcher.get = getter
    worker.details(db.start_run(timestamp=CLOCK.isoformat()))
    after = history(db)
    assert all(after[t][: len(before[t])] == before[t] for t in before)
    assert len(after["observations"]) == len(before["observations"])
    assert after["field_verifications"] == before["field_verifications"]
    return calls, worker


@pytest.mark.parametrize("status", [403, 429])
def test_reopened_alias_honors_direct_original_refusal(tmp_path, status):
    db, run, items = seed(tmp_path)
    refusal(db, run, status=status)
    db.conn.close()
    db = Database(tmp_path / "market.sqlite3")
    calls, worker = exercise(db, items, status=status)
    assert not calls
    assert worker.detail_stats["gaspedaal"]["backoff"] == 1
    db.conn.close()


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("workers", [1, 4])
def test_same_owned_publication_shares_one_attempt(tmp_path, status, workers):
    db, _run, items = seed(tmp_path)
    calls, worker = exercise(
        db, items, ("direct", "alias"), status=status, workers=workers
    )
    assert len(calls) == 1
    assert sum(s["attempted"] for s in worker.detail_stats.values()) == 1
    assert len(worker.detail_jobs) == 2
    db.conn.close()


@pytest.mark.parametrize(
    "age,expected", [(21600, 0), (21600.000001, 1), (300, 0), (-1, 1)]
)
def test_error_expiry_preserves_original_actual_clock(tmp_path, age, expected):
    db, run, items = seed(tmp_path)
    refusal(db, run, age=age)
    calls, _worker = exercise(db, items)
    assert len(calls) == expected
    db.conn.close()


@pytest.mark.parametrize(
    "fault",
    [
        "unknown_mapping",
        "ambiguous_mapping",
        "future_mapping",
        "missing_body",
        "wrong_digest",
        "current_ID",
        "current_link",
        "foreign_country",
        "field_conflict",
    ],
)
def test_uncertain_or_conflicting_linkage_stays_separate(tmp_path, fault):
    db, run, items = seed(tmp_path)
    refusal(db, run)
    if fault in {"unknown_mapping", "future_mapping"}:
        db.conn.execute("DELETE FROM fetches WHERE url=?", (ALIAS,))
        if fault == "future_mapping":
            refusal(db, run, uri=ALIAS, age=-1)
    elif fault == "ambiguous_mapping":
        refusal(db, run, uri=ALIAS, final=OTHER["final_url"], age=30000)
    elif fault in {"missing_body", "wrong_digest"}:
        capture = db.conn.execute(
            "SELECT fetch_id FROM latest_observations WHERE listing_id=?",
            (items["alias"][1],),
        ).fetchone()[0]
        if fault == "missing_body":
            db.conn.execute("UPDATE fetches SET raw_path=NULL WHERE id=?", (capture,))
        else:
            db.conn.execute(
                "UPDATE fetches SET content_hash=? WHERE id=?", ("0" * 64, capture)
            )
    elif fault == "current_ID":
        items["alias"][0].raw["advertentieId"] = 1
    elif fault == "current_link":
        items["alias"][0].raw["portalen"] = []
    elif fault == "foreign_country":
        items["alias"][0].raw["country_code"] = "BE"
    else:
        items["alias"][0].raw["field_conflicts"] = {"identity": "controlled_conflict"}
    db.conn.commit()
    # A future exact-URI refusal is separately held by the legacy literal cache.
    # Disable that independent cache here to test only whether it grants an alias
    # publication key/another destination's refusal. No production guard changes.
    calls, _worker = exercise(
        db, items, cache_hours=0 if fault == "future_mapping" else 24
    )
    assert len(calls) == 1
    db.conn.close()


def test_other_native_publication_is_not_a_host_pause(tmp_path):
    db, run, items = seed(tmp_path)
    refusal(db, run)
    calls, _worker = exercise(db, items, ("other",))
    assert calls == [OTHER["url"]]
    db.conn.close()


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("cap,expected", [(0, 0), (1, 1), (2, 1)])
def test_full_fast_family_accounts_only_actual_allowance(tmp_path, fast, cap, expected):
    db, _run, items = seed(tmp_path)
    calls, worker = exercise(db, items, ("direct", "alias"), cap=cap, fast=fast)
    assert len(calls) == expected
    assert sum(s["attempted"] for s in worker.detail_stats.values()) == expected
    db.conn.close()


@pytest.mark.parametrize(
    "stamp", ["malformed", "2026-10-12T15:00:00", "2026-10-13T00:00:00+00:00"]
)
def test_invalid_mapping_clock_does_not_supply_alias_identity(tmp_path, stamp):
    db, run, items = seed(tmp_path)
    db.conn.execute("DELETE FROM fetches WHERE url=?", (ALIAS,))
    refusal(db, run, uri=ALIAS, stamp=stamp)
    refusal(db, run)
    db.conn.commit()
    calls, _worker = exercise(db, items, cache_hours=0)
    assert calls == [ALIAS]
    db.conn.close()


def test_mixed_offset_refusal_keeps_original_expiry(tmp_path):
    db, run, items = seed(tmp_path)
    refusal(db, run, stamp="2026-10-13T00:55:00+02:00")
    calls, _worker = exercise(db, items)
    assert not calls
    db.conn.close()


def test_current_own_aw_canonical_conflict_is_not_an_alias(tmp_path):
    db, run, items = seed(tmp_path)
    refusal(db, run)
    row = db.conn.execute(
        "SELECT id,details_json FROM latest_observations WHERE listing_id=?",
        (items["direct"][1],),
    ).fetchone()
    payload = json.loads(row["details_json"])
    payload["raw"]["identity_evidence"]["canonical"] = "https://review.invalid/not-own"
    db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?",
        (json.dumps(payload), row["id"]),
    )
    db.conn.commit()
    calls, _worker = exercise(db, items)
    assert calls == [ALIAS]
    db.conn.close()


def test_policy_preparation_stays_on_sqlite_writer_thread(tmp_path, monkeypatch):
    import threading

    from car_market import gaspedaal_detail_policy

    db, _run, items = seed(tmp_path)
    writer = threading.get_ident()
    actual = gaspedaal_detail_policy.prepare
    called = []

    def guarded(collector, jobs):
        assert threading.get_ident() == writer
        changes = collector.db.conn.total_changes
        result = actual(collector, jobs)
        assert collector.db.conn.total_changes == changes
        called.append(True)
        return result

    monkeypatch.setattr(gaspedaal_detail_policy, "prepare", guarded)
    calls, _worker = exercise(db, items, ("direct", "alias"), workers=4)
    assert called == [True] and len(calls) == 1
    db.conn.close()


@pytest.mark.parametrize(
    "country,profile",
    [
        ("DE", "bmw_218i_active_tourer"),
        ("NL", "mercedes_c180_c205"),
        ("NL", "configured_vehicle"),
    ],
)
def test_alias_hint_does_not_cross_collector_profile(tmp_path, country, profile):
    from car_market.gaspedaal_detail_policy import prepare

    db, _run, items = seed(tmp_path)
    worker = Collector(db, {"scope": {"country": "NL"}})
    worker.country = country
    worker.config["scope"]["model_profile"] = profile
    item, listing = items["alias"]
    assert prepare(worker, [(ALIAS, ("autowereld", listing, item))]) == {}
    db.conn.close()


def test_active_mercedes_profile_does_not_receive_a_bmw_alias_hint(tmp_path):
    from car_market.gaspedaal_detail_policy import prepare
    from car_market.model_profiles import MERCEDES_PROFILE, use_profile

    db, _run, items = seed(tmp_path)
    worker = Collector(db, {"scope": {"country": "NL"}})
    item, listing = items["alias"]
    with use_profile(MERCEDES_PROFILE, "NL"):
        assert prepare(worker, [(ALIAS, ("autowereld", listing, item))]) == {}
    db.conn.close()


@pytest.mark.parametrize("portals", [1, {"klikUrl": ALIAS}, [ALIAS], None])
def test_malformed_current_portal_container_does_not_grant_alias(tmp_path, portals):
    db, run, items = seed(tmp_path)
    refusal(db, run)
    items["alias"][0].raw["portalen"] = portals
    calls, _worker = exercise(db, items)
    assert calls == [ALIAS]
    db.conn.close()
