"""Honest source denominators and operator freshness in partial collection."""

from datetime import UTC, datetime, timedelta

from car_market.coverage import report
from car_market.db import Database, now


def search(key, **kwargs):
    return {"source": "autoscout24", "key": key, "url": "https://www.autoscout24.nl/lst/toyota/corolla", **kwargs}


def test_partial_pass_has_query_fraction_without_inventing_whole_market_share(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    try:
        run = db.start_run()
        db.finish_source(run, "autoscout24", "current", now(), "partial", 1000, 300, 250, 15, "page_limit_exceeded; count_mismatch:300/1000")
        result = report(db.conn, {"queries": [search("current")]})
        assert result["queries"][0]["query_enumeration_percent"] == 30
        assert result["whole_market_coverage_percent"] is None
        assert not result["market_100_percent_verified"]
        assert not result["all_configured_queries_complete_and_fresh"]
    finally:
        db.conn.close()


def test_disabled_query_and_empty_config_cannot_misrepresent_collection(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    try:
        db.finish_source(db.start_run(), "autoscout24", "active", now(), "complete", 10, 10, 10, 1, None)
        result = report(db.conn, {"queries": [search("active"), search("disabled", enabled=False)]})
        assert len(result["queries"]) == 1
        assert result["all_configured_queries_complete_and_fresh"]
        assert not report(db.conn, {"queries": []})["all_configured_queries_complete_and_fresh"]
    finally:
        db.conn.close()


def test_zero_drifting_and_overcounted_denominators_do_not_claim_a_percentage(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    try:
        run = db.start_run()
        for key, expected, received, status, error in [
            ("empty", 0, 0, "zero_results", None),
            ("drift", 10, 8, "partial", "counter_changed:10->9"),
            ("overcount", 10, 11, "partial", "count_mismatch:11/10"),
            ("unknown", None, 8, "partial", "counter_unavailable"),
        ]:
            db.finish_source(run, "autoscout24", key, now(), status, expected, received, received, 1, error)
        result = report(db.conn, {"queries": [search(key) for key in ["empty", "drift", "overcount", "unknown"]]})
        assert all(q["query_enumeration_percent"] is None for q in result["queries"])
        assert result["queries"][0]["empty_query_verified"]
        assert not any(q["empty_query_verified"] for q in result["queries"][1:])
    finally:
        db.conn.close()


def test_fast_query_freshness_obeys_its_configured_interval(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    try:
        finished = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
        db.finish_source(db.start_run(), "autoscout24", "slow", finished, "complete", 1, 1, 1, 1, None, finished_at=finished)
        cfg = {"queries": [search("slow")], "fast_sources": ["autoscout24"], "fast_interval_minutes": 60}
        assert report(db.conn, cfg)["all_configured_queries_complete_and_fresh"]
        cfg["fast_interval_minutes"] = 5
        assert not report(db.conn, cfg)["all_configured_queries_complete_and_fresh"]
    finally:
        db.conn.close()
