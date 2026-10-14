import tempfile
import unittest
from pathlib import Path

from car_market.db import Database
from car_market.parse import Listing, number


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.directory.name) / "market.sqlite3")

    def tearDown(self):
        self.db.conn.close()
        self.directory.cleanup()

    def car(self, source="a", identity="1", price=8000, registration=None):
        return Listing(
            source,
            identity,
            "https://example.test/" + identity,
            "BMW 218i Active Tourer",
            price=price,
            year=2015,
            mileage=150000,
            transmission="manual",
            fuel="petrol",
            kenteken=registration,
            city="Diemen",
        ).qualify()

    def test_strong_identity_merges_but_fingerprint_does_not(self):
        r = self.db.start_run()
        a = self.db.observe(r, self.car(), "2026-10-04T10:00:00+00:00")
        b = self.db.observe(r, self.car("b"), "2026-10-04T10:00:00+00:00")
        self.db.candidates()
        self.assertEqual(self.db.summary()["vehicle_identities"], 2)
        self.assertEqual(self.db.summary()["possible_duplicate_pairs"], 1)
        self.db.attach_identity(a, "GB178T")
        self.db.attach_identity(b, "GB178T")
        self.assertEqual(self.db.summary()["vehicle_identities"], 1)

    def test_price_history_and_repeated_observation(self):
        r = self.db.start_run()
        self.db.observe(r, self.car(), "2026-10-04T10:00:00+00:00")
        self.db.observe(r, self.car(), "2026-10-04T10:01:00+00:00")
        s = self.db.start_run()
        self.db.observe(s, self.car(price=7500), "2026-10-05T10:00:00+00:00")
        self.assertEqual(self.db.summary()["observations"], 2)
        self.assertEqual(
            self.db.conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind='price_eur_changed'"
            ).fetchone()[0],
            1,
        )

    def test_unchanged_scan_updates_presence_without_copying_observation(self):
        r = self.db.start_run()
        self.db.observe(r, self.car(), "2026-10-04T10:00:00+00:00", query_key="all")
        s = self.db.start_run()
        self.db.observe(s, self.car(), "2026-10-04T10:05:00+00:00", query_key="all")
        self.assertEqual(self.db.summary()["observations"], 1)
        self.assertEqual(
            self.db.conn.execute("SELECT last_seen FROM listings").fetchone()[0],
            "2026-10-04T10:05:00+00:00",
        )
        self.assertEqual(
            self.db.conn.execute("SELECT last_run_id FROM query_membership").fetchone()[0], s
        )

    def test_partial_scan_does_not_mark_missing_or_sold(self):
        r = self.db.start_run()
        self.db.observe(r, self.car(), "2026-10-04T10:00:00+00:00", query_key="all")
        s = self.db.start_run()
        self.db.finish_source(s, "a", "all", "now", "partial", 50, 2, 1, 1, "network_error")
        self.assertEqual(
            self.db.conn.execute("SELECT missed_complete_runs FROM query_membership").fetchone()[0],
            0,
        )
        t = self.db.start_run()
        self.db.finish_source(t, "a", "all", "now", "complete", 0, 0, 0, 1)
        self.assertEqual(
            self.db.conn.execute("SELECT missed_complete_runs FROM query_membership").fetchone()[0],
            1,
        )
        self.assertNotEqual(
            self.db.conn.execute("SELECT availability FROM listings").fetchone()[0], "sold"
        )

    def test_conflicting_registration_is_not_overwritten(self):
        r = self.db.start_run()
        a = self.db.observe(r, self.car(registration="GB178T"), "2026-10-04T10:00:00+00:00")
        with self.assertRaises(ValueError):
            self.db.attach_identity(a, "5XPB51")

    def test_budget_is_only_purchase_view(self):
        r = self.db.start_run()
        self.db.observe(r, self.car(price=22000), "2026-10-04T10:00:00+00:00")
        self.assertEqual(self.db.summary()["listings"], 1)
        self.assertEqual(self.db.summary()["purchase_candidates"], 0)

    def test_search_does_not_erase_detail_description(self):
        r = self.db.start_run()
        car = self.car()
        car.description = "Auto staat op Duits kenteken"
        self.db.observe(r, car, "2026-10-04T10:00:00+00:00")
        s = self.db.start_run()
        self.db.observe(s, self.car(), "2026-10-04T10:05:00+00:00", query_key="all")
        import json

        detail = json.loads(
            self.db.conn.execute("SELECT details_json FROM latest_observations").fetchone()[0]
        )
        self.assertEqual(detail["description"], "Auto staat op Duits kenteken")

    def test_dutch_numbers(self):
        self.assertEqual(number("213.258 km"), 213258)
        self.assertEqual(number("€ 5.999"), 5999)

    def test_rdw_model_conflict_survives_new_advertisement(self):
        import json

        r = self.db.start_run()
        a = self.db.observe(r, self.car(registration="GB178T"), "2026-10-04T10:00:00+00:00")
        vehicle = self.db.conn.execute(
            "SELECT vehicle_id FROM listings WHERE id=?", (a,)
        ).fetchone()[0]
        self.db.conn.execute(
            "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json) VALUES (?,?,?,?)",
            (
                vehicle,
                "2026-10-04T10:01:00+00:00",
                "rdw",
                json.dumps({"handelsbenaming": "218I GRAN TOURER"}),
            ),
        )
        self.db.conn.commit()
        s = self.db.start_run()
        self.db.observe(s, self.car(registration="GB178T"), "2026-10-05T10:00:00+00:00")
        self.assertEqual(
            self.db.conn.execute("SELECT scope FROM listings").fetchone()[0], "model_conflict"
        )
        self.assertEqual(self.db.summary()["purchase_candidates"], 0)


if __name__ == "__main__":
    unittest.main()
