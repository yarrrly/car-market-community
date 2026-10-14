"""Original AutoTrader own country claims at the legacy NL final observation boundary."""

import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from car_market.collect import Collector, parse_market_page
from car_market.db import Database
from car_market.fetch import Response
from car_market.nl_publication import qualify_publication

FIXTURES = Path(__file__).parent / "fixtures" / "nl_autotrader_country"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


def native_item(name):
    record = MANIFEST[name]
    body = gzip.decompress((FIXTURES / record["file"]).read_bytes())
    assert hashlib.sha256(body).hexdigest() == record["body_sha256"]
    page = parse_market_page(record["source"], body.decode(), record["final_url"])
    own = [item for item in page.listings if item.external_id == record["native_id"]]
    assert len(own) == 1
    return own[0], body, record


class NativeCountryTests(unittest.TestCase):
    def test_original_foreign_and_owned_nl_keep_all_native_fields(self):
        for name, expected in [
            ("autotrader_foreign_negative", "excluded"),
            ("autotrader_owned_NL_positive", "target"),
            ("autoscout_owned_foreign_control", "excluded"),
        ]:
            with self.subTest(name=name):
                item, _, _ = native_item(name)
                original = {
                    field: getattr(item, field)
                    for field in (
                        "source",
                        "external_id",
                        "url",
                        "price",
                        "mileage",
                        "year",
                        "fuel",
                        "transmission",
                        "vin",
                        "kenteken",
                    )
                }
                qualify_publication(item)
                self.assertEqual(expected, item.scope)
                self.assertEqual(
                    original, {field: getattr(item, field) for field in original}
                )

    def test_actual_ingestion_keeps_original_foreign_capture_without_target(self):
        for name, expected in [
            ("autotrader_foreign_negative", "excluded"),
            ("autotrader_owned_NL_positive", "target"),
        ]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                item, body, record = native_item(name)
                db = Database(Path(temporary) / "market.sqlite3")
                worker = object.__new__(Collector)
                worker.db, worker.country = db, "NL"
                worker.config = {
                    "scope": {
                        "country": "NL",
                        "model_profile": "bmw_218i_active_tourer",
                    }
                }
                worker.failures, worker.detail_jobs, worker.detail_stats = [], {}, {}
                db.conn.execute(
                    "CREATE TABLE monitor_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
                )
                db.conn.execute(
                    "INSERT INTO monitor_state VALUES ('market_scope',?)",
                    (json.dumps({"country": "NL"}),),
                )
                db.conn.commit()
                run = db.start_run("native-country-test")
                response = Response(
                    record["requested_url"],
                    record["final_url"],
                    200,
                    body.decode(),
                    record["captured_at"],
                    transport="retained_native_fixture",
                )
                page, _ = worker.ingest_response(
                    run, record["source"], name, response, queue=False
                )
                own = [
                    value
                    for value in page.listings
                    if value.external_id == item.external_id
                ]
                self.assertEqual(expected, own[0].scope)
                row = db.conn.execute(
                    "SELECT scope FROM listings WHERE source=? AND external_id=?",
                    (item.source, item.external_id),
                ).fetchone()
                if expected == "excluded":
                    self.assertIsNone(row)
                else:
                    self.assertEqual("target", row["scope"])
                capture = db.conn.execute("SELECT * FROM fetches").fetchone()
                self.assertEqual(record["captured_at"], capture["fetched_at"])
                self.assertEqual(record["body_sha256"], capture["content_hash"])
                self.assertEqual(
                    body,
                    gzip.decompress(
                        (Path(temporary) / capture["raw_path"]).read_bytes()
                    ),
                )
                self.assertEqual(0, len(worker.detail_jobs))
                db.conn.close()

    def test_explicit_wrong_id_uri_model_cannot_borrow_matching_country(self):
        item, _, _ = native_item("autotrader_owned_NL_positive")
        other = "00000000-1111-2222-3333-444444444444"
        faults = {
            "native_id": lambda raw: raw.update(id=other),
            "own_uri": lambda raw: raw.update(url="/auto/voertuig/bmw-218i/" + other),
            "publisher_uri": lambda raw: raw.update(
                webPage="https://www.autoscout24.nl/aanbod/bmw-218i-" + other
            ),
            "foreign_uri_same_id": lambda raw: raw.update(
                url="https://foreign.example/auto/voertuig/bmw/" + item.external_id
            ),
            "explicit_other_model": lambda raw: raw["vehicle"].update(model="220i"),
        }
        for name, fault in faults.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(item)
                fault(changed.raw)
                qualify_publication(changed)
                self.assertEqual("excluded", changed.scope)

    def test_optional_uri_and_same_uuid_publisher_reference_are_valid(self):
        item, _, _ = native_item("autotrader_owned_NL_positive")
        for publisher in [
            None,
            "https://www.autoscout24.nl/aanbod/bmw-218i-" + item.external_id,
        ]:
            with self.subTest(publisher=publisher):
                own = copy.deepcopy(item)
                own.raw.pop("url", None)
                own.raw.pop("webPage", None)
                if publisher:
                    own.raw["webPage"] = publisher
                self.assertEqual("target", qualify_publication(own).scope)

    def test_foreign_country_changes_are_exact_negative_claims(self):
        item, _, _ = native_item("autotrader_owned_NL_positive")
        for country in ["DE", "BE", "IT", "AT", "FR"]:
            with self.subTest(country=country):
                own = copy.deepcopy(item)
                own.raw["location"]["countryCode"] = country
                self.assertEqual("excluded", qualify_publication(own).scope)
                self.assertEqual(
                    country, own.raw["nl_publication_conflicts"]["country"]["value"]
                )

    def test_missing_country_after_enrichment_stays_pending_and_owned_nl_restores(self):
        item, _, _ = native_item("autotrader_owned_NL_positive")
        for missing, prior_foreign in [
            (value, foreign)
            for value in [None, "", "absent"]
            for foreign in [False, True]
        ]:
            with (
                self.subTest(missing=missing, prior_foreign=prior_foreign),
                tempfile.TemporaryDirectory() as temporary,
            ):
                db = Database(Path(temporary) / "market.sqlite3")
                db.conn.execute(
                    "CREATE TABLE monitor_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
                )
                db.conn.execute(
                    "INSERT INTO monitor_state VALUES ('market_scope',?)",
                    (json.dumps({"country": "NL"}),),
                )
                db.conn.commit()

                def observe(value, timestamp, db=db):
                    run = db.start_run("derived-native-country-boundary")
                    native_projection = json.dumps(value.raw, sort_keys=True)
                    fetch_id = db.save_fetch(
                        run,
                        value.source,
                        value.url,
                        value.url,
                        200,
                        native_projection,
                        timestamp,
                        "controlled_native_projection",
                        None,
                    )
                    return db.observe(run, value, timestamp, fetch_id), fetch_id

                listing_id, initial_fetch = observe(
                    copy.deepcopy(item), "2026-10-12T06:00:00+00:00"
                )
                old = db.conn.execute(
                    "SELECT details_json,fetch_id,observed_at FROM observations WHERE listing_id=? AND fetch_id=?",
                    (listing_id, initial_fetch),
                ).fetchone()
                if prior_foreign:
                    foreign = copy.deepcopy(item)
                    foreign.raw["location"]["countryCode"] = "DE"
                    observe(foreign, "2026-10-12T06:00:30+00:00")
                    self.assertEqual(
                        "excluded",
                        db.conn.execute(
                            "SELECT scope FROM listings WHERE id=?", (listing_id,)
                        ).fetchone()[0],
                    )
                missing_item = copy.deepcopy(item)
                missing_item.raw["location"].pop("countryCode", None)
                if missing != "absent":
                    missing_item.raw["location"]["countryCode"] = missing
                observe(missing_item, "2026-10-12T06:01:00+00:00")
                self.assertEqual(
                    "needs_confirmation",
                    db.conn.execute(
                        "SELECT scope FROM listings WHERE id=?", (listing_id,)
                    ).fetchone()[0],
                )
                restored, _ = observe(copy.deepcopy(item), "2026-10-12T06:02:00+00:00")
                self.assertEqual(listing_id, restored)
                self.assertEqual(
                    "target",
                    db.conn.execute(
                        "SELECT scope FROM listings WHERE id=?", (listing_id,)
                    ).fetchone()[0],
                )
                unchanged = db.conn.execute(
                    "SELECT details_json,fetch_id,observed_at FROM observations WHERE listing_id=? AND fetch_id=?",
                    (listing_id, initial_fetch),
                ).fetchone()
                self.assertEqual(tuple(old), tuple(unchanged))
                db.conn.close()


if __name__ == "__main__":
    unittest.main()
