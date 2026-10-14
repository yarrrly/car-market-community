"""Synthetic persisted clocks must not bypass or prolong source refusal holds."""

import copy
import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from car_market.configuration import validate
from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database
from car_market.parse import Listing
from car_market.search_profile import GENERIC_PROFILE, qualify

AS_OF = datetime(2026, 10, 12, 20, 30, tzinfo=UTC)
SOURCE = "marktplaats"
URL = "https://www.marktplaats.nl/l/auto-s/toyota/"
ITEM_URI = "https://www.marktplaats.nl/v/auto-s/toyota/m1000000001-synthetic-corolla"
CONFIG = {
    "schema_version": 1,
    "scope": {
        "country": "NL",
        "model_profile": GENERIC_PROFILE,
        "selector": {"make": "Toyota", "model_terms": [["Corolla"]]},
    },
    "queries": [
        {
            "source": SOURCE,
            "key": "synthetic-root",
            "url": URL,
            "pagination": "marktplaats",
        }
    ],
    "workers": 1,
    "max_pages": 1,
    "detail_limit": 1,
    "detail_batch_limit": 1,
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Refusal-clock controls must not contact a provider")

    monkeypatch.setattr("socket.socket.connect", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)


def freeze(monkeypatch, as_of=AS_OF):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return as_of.astimezone(tz) if tz else as_of.replace(tzinfo=None)

    monkeypatch.setattr("datetime.datetime", Clock)
    monkeypatch.setattr("car_market.db.now", lambda: as_of.isoformat())
    monkeypatch.setattr("car_market.collect.now", lambda: as_of.isoformat())


def collector(tmp_path, **updates):
    config = copy.deepcopy(CONFIG)
    config.update(updates)
    validate(config)
    db = Database(tmp_path / "synthetic.sqlite3")
    return db, ConfiguredCollector(db, config)


def put(db, clock, status=429, source=SOURCE, uri=URL):
    stamp = clock.isoformat() if isinstance(clock, datetime) else clock
    return db.save_fetch(
        None, source, uri, uri, status, None, stamp, error="synthetic_refusal"
    )


@pytest.mark.parametrize(
    ("noise", "noise_status", "valid_status"),
    [
        ("2026-10-12T20:25:00", 403, 429),
        ("2026-10-12T20:25:00", 429, 403),
        ("2026-10-12T24:00:00+00:00", 429, 403),
        ("not-a-timestamp", 429, 403),
        ((AS_OF + timedelta(days=2)).isoformat(), 403, 429),
        ((AS_OF + timedelta(days=2)).isoformat(), 429, 403),
        ((AS_OF + timedelta(microseconds=1)).isoformat(), 403, 429),
    ],
)
def test_ineligible_newer_refusal_cannot_mask_or_extend_valid_hold(
    tmp_path, monkeypatch, noise, noise_status, valid_status
):
    freeze(monkeypatch)
    db, worker = collector(tmp_path)
    valid = AS_OF - timedelta(minutes=10)
    put(db, valid, valid_status)
    put(db, noise, noise_status)
    before = [
        tuple(row) for row in db.conn.execute("SELECT * FROM fetches ORDER BY id")
    ]
    changes = db.conn.total_changes
    db.conn.execute("PRAGMA query_only=ON")
    assert worker._source_backoff_until(SOURCE) == valid + timedelta(minutes=60)
    assert db.conn.total_changes == changes
    assert before == [
        tuple(row) for row in db.conn.execute("SELECT * FROM fetches ORDER BY id")
    ]
    db.conn.close()


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("minutes", [1, 1.5, 60, 90])
def test_exact_expiry_with_future_noise_and_fractional_ttl(
    tmp_path, monkeypatch, delta, minutes
):
    freeze(monkeypatch)
    db, worker = collector(tmp_path, source_error_backoff_minutes=minutes)
    valid = AS_OF - timedelta(minutes=minutes) + timedelta(microseconds=delta)
    put(db, valid)
    put(db, AS_OF + timedelta(days=2), 403)
    expected = valid + timedelta(minutes=minutes) if delta > 0 else None
    assert worker._source_backoff_until(SOURCE) == expected
    db.conn.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_sql_millisecond_ties_use_exact_python_clock_not_insertion_id(
    tmp_path, monkeypatch, reverse
):
    freeze(monkeypatch)
    db, worker = collector(tmp_path)
    earlier = AS_OF - timedelta(microseconds=99)
    later = AS_OF - timedelta(microseconds=98)
    for clock in [earlier, later] if reverse else [later, earlier]:
        put(db, clock)
    sql_clocks = [
        row[0] for row in db.conn.execute("SELECT julianday(fetched_at) FROM fetches")
    ]
    assert sql_clocks[0] == sql_clocks[1]  # Actual SQLite precision collision.
    assert worker._source_backoff_until(SOURCE) == later + timedelta(minutes=60)
    db.conn.close()


@pytest.mark.parametrize(
    "offset", [timedelta(hours=5, minutes=30), timedelta(hours=-7)]
)
def test_offsets_source_only_scope_and_non_refusals_preserve_original_clock(
    tmp_path, monkeypatch, offset
):
    freeze(monkeypatch)
    db, worker = collector(tmp_path)
    valid = AS_OF - timedelta(minutes=10)
    put(db, valid.astimezone(timezone(offset)), uri=ITEM_URI)
    put(db, AS_OF - timedelta(minutes=1), 200)
    put(db, AS_OF - timedelta(minutes=2), 404)
    put(db, AS_OF, 403, source="autotrack")
    assert worker._source_backoff_until(SOURCE) == valid + timedelta(minutes=60)
    assert worker._source_backoff_until("autoscout24") is None
    db.conn.close()


@pytest.mark.parametrize("lane", ["root", "pending"])
@pytest.mark.parametrize("noise", ["naive", "hour24", "future"])
def test_restart_blocks_actual_dispatch_and_preserves_original_fact_history(
    tmp_path, monkeypatch, lane, noise
):
    freeze(monkeypatch)
    db, worker = collector(tmp_path)
    run = db.start_run("synthetic_seed", AS_OF.isoformat())
    item = Listing(
        SOURCE,
        "m1000000001",
        ITEM_URI,
        "Toyota Corolla",
        price=12345,
        year=2018,
        raw={"country_code": "NL", "synthetic_control": True},
    )
    qualify(item, CONFIG["scope"])
    db.observe(
        run, item, (AS_OF - timedelta(hours=2)).isoformat(), query_key="synthetic-own"
    )
    put(db, AS_OF - timedelta(minutes=10))
    bad_clock = {
        "naive": "2026-10-12T20:25:00",
        "hour24": "2026-10-12T24:00:00+00:00",
        "future": (AS_OF + timedelta(days=2)).isoformat(),
    }[noise]
    put(db, bad_clock, 403)
    if lane == "pending":
        candidate = dataclasses.replace(
            item,
            external_id="m1000000002",
            url=ITEM_URI.replace("m1000000001", "m1000000002"),
            raw={"synthetic_control": True},
        )
        db.conn.execute(
            "INSERT INTO monitor_state VALUES (?,?)",
            (
                "pending:" + SOURCE + ":" + candidate.external_id,
                json.dumps(
                    {
                        "candidate": dataclasses.asdict(candidate),
                        "observed_at": (AS_OF - timedelta(hours=2)).isoformat(),
                    }
                ),
            ),
        )
        db.conn.commit()
    tables = [
        "fetches",
        "listings",
        "observations",
        "events",
        "vehicles",
        "vehicle_aliases",
        "field_verifications",
        "query_membership",
        "monitor_state",
    ]
    before = {
        table: [
            tuple(row)
            for row in db.conn.execute(
                f"SELECT * FROM {table} ORDER BY {'key' if table == 'monitor_state' else 'rowid'}"
            )
        ]
        for table in tables
    }
    db.conn.close()
    db = Database(tmp_path / "synthetic.sqlite3")
    worker = ConfiguredCollector(db, copy.deepcopy(CONFIG))

    def no_get(*args, **kwargs):
        pytest.fail(
            "Restart must respect the valid persisted source refusal before GET"
        )

    monkeypatch.setattr(worker.fetcher, "get", no_get)
    if lane == "root":
        current = db.start_run("synthetic_restart", AS_OF.isoformat())
        worker.collect_query(current, CONFIG["queries"][0])
        receipt = db.conn.execute(
            "SELECT * FROM source_runs WHERE run_id=?", (current,)
        ).fetchone()
        assert (
            receipt["status"] == "backoff"
            and receipt["pages"]
            == receipt["received_count"]
            == receipt["target_count"]
            == 0
        )
        assert (
            receipt["error"]
            == "access_backoff_until:" + (AS_OF + timedelta(minutes=50)).isoformat()
        )
    else:
        result = worker._pending_details()
        assert result["attempted"] == 0 and result["deferred"] == 1
    assert before == {
        table: [
            tuple(row)
            for row in db.conn.execute(
                f"SELECT * FROM {table} ORDER BY {'key' if table == 'monitor_state' else 'rowid'}"
            )
        ]
        for table in tables
    }
    db.conn.close()


def test_recent_malformed_rows_have_no_arbitrary_candidate_limit(tmp_path, monkeypatch):
    freeze(monkeypatch)
    db, worker = collector(tmp_path)
    valid = AS_OF - timedelta(minutes=10)
    put(db, valid)
    db.conn.executemany(
        "INSERT INTO fetches(source,url,fetched_at,status) VALUES (?,?,?,?)",
        [(SOURCE, URL, "2026-10-12T20:25:00", 403)] * 1100,
    )
    db.conn.commit()
    assert worker._source_backoff_until(SOURCE) == valid + timedelta(minutes=60)
    db.conn.close()


def test_expired_and_future_history_is_outside_indexed_cursor_window(
    tmp_path, monkeypatch
):
    freeze(monkeypatch)
    db, worker = collector(tmp_path)
    db.conn.executemany(
        "INSERT INTO fetches(source,url,fetched_at,status) VALUES (?,?,?,?)",
        [(SOURCE, URL, (AS_OF - timedelta(days=1)).isoformat(), 403)] * 10000
        + [(SOURCE, URL, (AS_OF + timedelta(days=1)).isoformat(), 429)] * 10000,
    )
    db.conn.commit()
    progress_calls = []

    def bounded_vm():
        progress_calls.append(1)
        return int(len(progress_calls) > 30)

    db.conn.set_progress_handler(bounded_vm, 100)
    try:
        assert worker._source_backoff_until(SOURCE) is None
    finally:
        db.conn.set_progress_handler(None, 0)
    # The small refusal window must not traverse 20k unrelated-time rows.
    assert len(progress_calls) <= 30
    db.conn.close()
