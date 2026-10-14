import tempfile
import unittest
from pathlib import Path

from car_market.collect import Collector
from car_market.db import Database, now
from car_market.opportunities import evaluate, publish
from car_market.parse import Listing


class OpportunityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "market.sqlite3")
        self.run = self.db.start_run()
        self.timestamp = now()

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def car(
        self,
        i,
        price=10000,
        mileage=130000,
        description=None,
        source="a",
        price_type="advertised_price",
        vin=None,
    ):
        item = Listing(
            source,
            str(i),
            "https://example.test/" + str(i),
            "BMW 218i Active Tourer",
            price=price,
            year=2015,
            mileage=mileage,
            transmission="manual",
            fuel="petrol",
            description=description,
            price_type=price_type,
            vin=vin,
        ).qualify()
        return self.db.observe(self.run, item, self.timestamp)

    def peers(self):
        for i in range(8):
            self.car(i, mileage=125000 + i * 500, vin=f"WBA2A31010V{i:06d}")

    def test_defective_cheapest_car_is_not_an_opportunity(self):
        self.peers()
        self.car(90, 5000, description="motor loopt soms niet goed")
        self.assertFalse(evaluate(self.db.conn)["opportunities"])

    def test_german_registration_is_not_a_ready_nl_gem(self):
        self.peers()
        self.car(
            90,
            7500,
            description="Auto staat op Duits kenteken. Overige importkosten natuurlijk wel.",
        )
        self.assertFalse(evaluate(self.db.conn)["opportunities"])

    def test_bid_price_and_outlier_are_excluded(self):
        self.peers()
        self.car(90, 3000, price_type="MIN_BID")
        self.car(91, 7500000)
        self.assertEqual(evaluate(self.db.conn)["comparison_cars"], 8)

    def test_duplicate_ads_do_not_increase_comparable_count(self):
        self.peers()
        self.car(0, source="b", mileage=125000, vin="WBA2A31010V000000")
        self.car(90, 7500, mileage=132000)
        r = evaluate(self.db.conn)
        self.assertEqual(r["comparison_cars"], 9)
        self.assertEqual(r["opportunities"][0]["peer_count"], 8)

    def test_same_registration_with_different_ad_year_is_one_comparison(self):
        a = self.car(1, mileage=130000)
        self.db.attach_identity(a, "GB178T")
        self.db.conn.commit()
        item = Listing(
            "b",
            "2",
            "https://example.test/2",
            "BMW 218i Active Tourer",
            price=10000,
            year=2014,
            mileage=131000,
            transmission="manual",
            kenteken="GB178T",
        ).qualify()
        self.db.observe(self.run, item, self.timestamp)
        self.assertEqual(evaluate(self.db.conn)["comparison_cars"], 1)

    def test_alert_is_not_repeated_but_price_drop_is_new(self):
        self.peers()
        self.car(90, 7500, mileage=132000)
        publish(self.db, Path(self.tmp.name) / "signals")
        publish(self.db, Path(self.tmp.name) / "signals")
        self.assertEqual(
            self.db.conn.execute("select count(*) from opportunity_signals").fetchone()[0], 1
        )
        self.run = self.db.start_run()
        self.car(90, 7000, mileage=132000)
        publish(self.db, Path(self.tmp.name) / "signals")
        self.assertEqual(
            self.db.conn.execute("select count(*) from opportunity_signals").fetchone()[0], 2
        )

    def test_recent_detail_success_skips_network(self):
        i = self.car(90, 7500)
        from car_market.db import now

        self.db.save_fetch(
            self.run, "a", "https://example.test/90", "https://example.test/90", 200, "{}", now()
        )
        co = Collector(self.db, {"detail_cache_hours": 24})
        item = Listing("a", "90", "https://example.test/90", "BMW 218i Active Tourer", price=7500)
        co.queue_details(item, i)
        co.fetcher.get = lambda _: self.fail("A recent detail must be cached")
        co.details(self.run)


if __name__ == "__main__":
    unittest.main()
