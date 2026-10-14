"""Long provenance history keeps every distinct capture with linear identity work."""

from datetime import UTC, datetime, timedelta

import car_market.vehicle_catalog as vc
from car_market.db import Database
from car_market.parse import Listing


def test_long_odometer_history_keeps_all_origins_without_quadratic_json_work(
    tmp_path, monkeypatch
):
    count = 512
    clock = datetime(2026, 10, 11, 12, tzinfo=UTC)
    db = Database(tmp_path / "market.sqlite3")
    try:
        for index in range(count):
            stamp = (
                clock - timedelta(hours=1) + timedelta(microseconds=index)
            ).isoformat()
            run = db.start_run(timestamp=stamp)
            own = Listing(
                "fixture",
                "history",
                "https://example.test/history",
                "BMW 218i Active Tourer",
                price=10000,
                year=2018,
                mileage=100000 + index,
                fuel="petrol",
                transmission="manual",
                kenteken="GB178T",
            ).qualify()
            db.observe(run, own, stamp)
            db.finish_run(run, "complete", {})
        calls = 0
        original = vc._key

        def key(value):
            nonlocal calls
            calls += 1
            return original(value)

        monkeypatch.setattr(vc, "_key", key)
        config = {"as_of": clock.isoformat(), "freshness_hours": 13}
        card = vc.catalog(db.conn, config)[0]
        assert card["mileage_km"] == 100000 + count - 1
        assert len(card["field_alternatives"]["mileage_km"]) == count
        assert {
            item["value"] for item in card["field_alternatives"]["mileage_km"]
        } == set(range(100000, 100000 + count))
        assert calls < 12 * count + 200, (
            "Historical origin identity encoding became quadratic"
        )
        current = vc.catalog(db.conn, {**config, "include_history": False})[0]
        assert (
            current["year"] == card["year"]
            and current["fuel"] == card["fuel"]
            and current["transmission"] == card["transmission"]
        )
        assert current["field_conflicts"]["year"] == card["field_conflicts"]["year"]
        assert len(current["field_alternatives"]["mileage_km"]) == 1
        assert (
            db.conn.execute("SELECT count(*) FROM observations").fetchone()[0] == count
        )
    finally:
        db.conn.close()
