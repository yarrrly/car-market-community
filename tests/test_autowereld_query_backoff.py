"""Denied search requests must pause across fresh collector/database instances."""

from datetime import UTC, datetime, timedelta

import pytest

from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.parse import Listing

URL = "https://www.autowereld.nl/bmw/2-serie-active-tourer/_218i"
CAPTURE = datetime(2026, 10, 12, 15, 8, 0, 337105, tzinfo=UTC)


def reader(path, monkeypatch, evaluation):
    monkeypatch.setattr("car_market.collect.now", lambda: evaluation.isoformat())
    monkeypatch.setattr("car_market.db.now", lambda: evaluation.isoformat())
    collector = Collector.__new__(Collector)
    collector.db = Database(path)
    collector.country = "NL"
    collector.config = {"scope": {"country": "NL"}, "detail_error_backoff_hours": 6}
    collector.failures = []
    calls = []

    class Getter:
        def get(self, uri, **kwargs):
            calls.append(uri)
            return Response(
                uri, uri, 403, "Forbidden", evaluation.isoformat(), "HTTP403"
            )

    collector.fetcher = Getter()
    return collector, calls


def denied(collector, status=403, uri=URL, source="autowereld", clock=CAPTURE):
    run = collector.db.start_run("fast", timestamp=clock.isoformat())
    return collector.db.save_fetch(
        run,
        source,
        uri,
        uri,
        status,
        "Forbidden",
        clock.isoformat(),
        error="HTTP" + str(status),
    )


@pytest.mark.parametrize("status", [403, 429])
def test_fresh_instance_pauses_denied_query_without_renewing_rows_or_presence(
    tmp_path, monkeypatch, status
):
    path = tmp_path / "market.sqlite3"
    first, _unused = reader(path, monkeypatch, CAPTURE)
    run = first.db.start_run("fast", timestamp=CAPTURE.isoformat())
    listing = Listing(
        "autowereld",
        "owned-old-card",
        URL + "/owned",
        "BMW218i ActiveTourer",
        fuel="petrol",
        scope="target",
    )
    listing_id = first.db.observe(
        run, listing, CAPTURE.isoformat(), query_key="owned-query"
    )
    fetch_id = denied(first, status)
    original = tuple(
        first.db.conn.execute(
            "SELECT * FROM fetches WHERE id=?", (fetch_id,)
        ).fetchone()
    )
    old_counts = tuple(
        first.db.conn.execute(
            "SELECT (SELECT COUNT(*) FROM fetches),(SELECT COUNT(*) FROM observations),(SELECT COUNT(*) FROM events)"
        ).fetchone()
    )
    first.db.conn.close()
    second, calls = reader(path, monkeypatch, CAPTURE + timedelta(minutes=7))
    next_run = second.db.start_run(
        "fast", timestamp=(CAPTURE + timedelta(minutes=7)).isoformat()
    )
    second.collect_query(
        next_run,
        {"source": "autowereld", "key": "owned-query", "url": URL, "max_pages": 1},
    )
    assert calls == []
    row = second.db.conn.execute(
        "SELECT * FROM source_runs WHERE run_id=?", (next_run,)
    ).fetchone()
    assert (
        row["status"] == "partial"
        and row["pages"] == row["received_count"] == row["target_count"] == 0
    )
    assert "access_refusal_backoff_until:" in row["error"]
    assert (CAPTURE + timedelta(hours=6)).isoformat() in row["error"]
    assert (
        tuple(
            second.db.conn.execute(
                "SELECT * FROM fetches WHERE id=?", (fetch_id,)
            ).fetchone()
        )
        == original
    )
    assert (
        tuple(
            second.db.conn.execute(
                "SELECT (SELECT COUNT(*) FROM fetches),(SELECT COUNT(*) FROM observations),(SELECT COUNT(*) FROM events)"
            ).fetchone()
        )
        == old_counts
    )
    assert second.db.conn.execute(
        "SELECT availability,last_seen FROM listings WHERE id=?", (listing_id,)
    ).fetchone()[:] == ("observed", CAPTURE.isoformat())
    assert (
        second.db.conn.execute(
            "SELECT missed_complete_runs FROM query_membership WHERE listing_id=?",
            (listing_id,),
        ).fetchone()[0]
        == 0
    )
    second.db.conn.close()


@pytest.mark.parametrize(
    ("age", "expected_calls"),
    [
        (timedelta(hours=6) - timedelta(microseconds=1), 0),
        (timedelta(hours=6), 1),
        (timedelta(hours=6, seconds=1), 1),
    ],
)
def test_original_expiry_boundary_does_not_become_sliding(
    tmp_path, monkeypatch, age, expected_calls
):
    collector, calls = reader(tmp_path / "market.sqlite3", monkeypatch, CAPTURE + age)
    denied(collector)
    run = collector.db.start_run("fast")
    collector.collect_query(
        run, {"source": "autowereld", "key": "own", "url": URL, "max_pages": 1}
    )
    assert len(calls) == expected_calls
    collector.db.conn.close()


@pytest.mark.parametrize(
    "case",
    [
        "different_URI",
        "other_source",
        "foreign_URI",
        "future",
        "status404",
        "status200",
        "DE_profile",
        "Mercedes_profile",
        "configured_profile",
        "disabled",
    ],
)
def test_unrelated_or_untrusted_denial_does_not_pause_this_query(
    tmp_path, monkeypatch, case
):
    collector, calls = reader(
        tmp_path / "market.sqlite3", monkeypatch, CAPTURE + timedelta(minutes=7)
    )
    uri, source, clock, status = URL, "autowereld", CAPTURE, 403
    if case == "different_URI":
        uri += "?model=different"
    if case == "other_source":
        source = "autotrack"
    if case == "foreign_URI":
        uri = "https://foreign.example/bmw"
    if case == "future":
        clock += timedelta(days=1)
    if case == "status404":
        status = 404
    if case == "status200":
        status = 200
    if case == "DE_profile":
        collector.country = "DE"
    if case == "Mercedes_profile":
        collector.config["scope"]["model_profile"] = "mercedes_c180_c205"
    if case == "configured_profile":
        collector.config["scope"]["model_profile"] = "configured_vehicle"
    if case == "disabled":
        collector.config["query_error_backoff_hours"] = 0
    denied(collector, status, uri, source, clock)
    run = collector.db.start_run("fast")
    collector.collect_query(
        run, {"source": "autowereld", "key": "own", "url": URL, "max_pages": 1}
    )
    assert calls == [URL]
    collector.db.conn.close()


def test_denial_on_exact_final_URI_is_bound_to_own_query(tmp_path, monkeypatch):
    collector, calls = reader(
        tmp_path / "market.sqlite3", monkeypatch, CAPTURE + timedelta(minutes=7)
    )
    old = collector.db.start_run("fast")
    collector.db.save_fetch(
        old,
        "autowereld",
        "https://api.gaspedaal.nl/redirect/vehicle/123",
        URL,
        403,
        "Forbidden",
        CAPTURE.isoformat(),
        error="HTTP403",
    )
    run = collector.db.start_run("fast")
    collector.collect_query(
        run, {"source": "autowereld", "key": "own", "url": URL, "max_pages": 1}
    )
    assert not calls
    collector.db.conn.close()


def test_newer_capture_clock_wins_over_larger_insert_ID(tmp_path, monkeypatch):
    collector, calls = reader(
        tmp_path / "market.sqlite3", monkeypatch, CAPTURE + timedelta(minutes=7)
    )
    denied(collector, clock=CAPTURE + timedelta(minutes=1))
    denied(collector, clock=CAPTURE - timedelta(hours=1))
    run = collector.db.start_run("fast")
    collector.collect_query(
        run, {"source": "autowereld", "key": "own", "url": URL, "max_pages": 1}
    )
    row = collector.db.conn.execute(
        "SELECT error FROM source_runs WHERE run_id=?", (run,)
    ).fetchone()
    assert not calls and (CAPTURE + timedelta(hours=6, minutes=1)).isoformat() in row[0]
    collector.db.conn.close()


@pytest.mark.parametrize("bad_clock", ["invalid", "2026-10-12T15:09:00"])
def test_invalid_or_naive_capture_does_not_hide_older_valid_refusal(
    tmp_path, monkeypatch, bad_clock
):
    collector, calls = reader(
        tmp_path / "market.sqlite3", monkeypatch, CAPTURE + timedelta(minutes=7)
    )
    denied(collector)
    old = collector.db.start_run("fast")
    collector.db.save_fetch(
        old, "autowereld", URL, URL, 403, "Forbidden", bad_clock, error="HTTP403"
    )
    run = collector.db.start_run("fast")
    collector.collect_query(
        run, {"source": "autowereld", "key": "own", "url": URL, "max_pages": 1}
    )
    assert not calls
    collector.db.conn.close()


def test_repeated_fast_runs_keep_expiry_and_fetches_unchanged(tmp_path, monkeypatch):
    collector, calls = reader(
        tmp_path / "market.sqlite3", monkeypatch, CAPTURE + timedelta(minutes=7)
    )
    denied(collector)
    for index in range(3):
        run = collector.db.start_run("fast")
        collector.collect_query(
            run, {"source": "autowereld", "key": "own", "url": URL, "max_pages": 1}
        )
    assert (
        not calls
        and collector.db.conn.execute("SELECT count(*) FROM fetches").fetchone()[0] == 1
    )
    expires = {
        row[0] for row in collector.db.conn.execute("SELECT error FROM source_runs")
    }
    assert expires == {
        "access_refusal_backoff_until:" + (CAPTURE + timedelta(hours=6)).isoformat()
    }
    collector.db.conn.close()
