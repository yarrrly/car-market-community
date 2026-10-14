import json
import unittest

from car_market.collect import page_url
from car_market.parse import Listing, parse_page


class ParserTests(unittest.TestCase):
    def test_active_tourer_coupe_and_diesel_are_distinct(self):
        self.assertEqual(
            Listing("x", "1", "u", "BMW 218i Active Tourer", fuel="petrol").qualify().scope,
            "target",
        )
        self.assertEqual(
            Listing("x", "1", "u", "BMW 218d Active Tourer", fuel="diesel").qualify().scope,
            "excluded",
        )
        self.assertEqual(
            Listing("x", "1", "u", "BMW 218i Gran Coupe", fuel="petrol").qualify().scope, "excluded"
        )
        self.assertEqual(
            Listing("x", "1", "u", "BMW 218 Active Tourer", fuel="petrol").qualify().scope,
            "needs_confirmation",
        )

    def test_next_data_preserves_id_price_and_gear(self):
        state = {
            "props": {
                "pageProps": {
                    "numberOfResults": 1,
                    "numberOfPages": 1,
                    "pageQuery": {},
                    "listings": [
                        {
                            "id": "abc",
                            "url": "/aanbod/abc",
                            "vehicle": {
                                "motorTypeName": "218i",
                                "variant": "Active Tourer",
                                "fuel": "Benzine",
                                "transmission": "Automaat",
                                "mileageInKm": "123.247 km",
                            },
                            "price": {"priceRaw": 12500},
                            "seller": {"companyName": "Example"},
                            "location": {"city": "Diemen"},
                        }
                    ],
                }
            }
        }
        p = parse_page(
            "autoscout24",
            '<script type="application/json">' + json.dumps(state) + "</script>",
            "https://www.autoscout24.nl/lst/bmw/218",
        )
        self.assertEqual(p.total, 1)
        self.assertEqual(p.listings[0].external_id, "abc")
        self.assertEqual(p.listings[0].price, 12500)
        self.assertEqual(p.listings[0].mileage, 123247)
        self.assertEqual(p.listings[0].transmission, "automatic")

    def test_anwb_public_inventory_prices_and_identity(self):
        payload = {
            "total": 420,
            "pageNumber": 1,
            "pageSize": 30,
            "hits": [
                {
                    "advertisementId": 42,
                    "url": "https://www.autotrack.nl/a/bmw-42",
                    "licencePlate": "GB178T",
                    "price": {"totalIncludingTax": 8500},
                    "carData": {
                        "general": {
                            "brandName": "BMW",
                            "modelName": "2-Serie Active Tourer",
                            "editon": "218i",
                            "fuelType": "BENZINE",
                            "transmissionType": "HANDMATIG",
                        },
                        "history": {"constructionYear": 2015, "mileage": 120000},
                    },
                    "seller": {"sellerData": {"name": "Example", "city": "Diemen"}},
                }
            ],
        }
        p = parse_page("anwb", json.dumps(payload), "https://api.anwb.nl/")
        self.assertEqual(p.pages, 14)
        self.assertEqual(p.listings[0].scope, "target")
        self.assertEqual(p.listings[0].kenteken, "GB178T")
        self.assertEqual(p.listings[0].price, 8500)

    def test_bmw_js_string_with_embedded_quotes_is_decoded(self):
        data = {
            "vehicles": [
                {
                    "vehicleId": "42",
                    "name": "BMW 218i Active Tourer",
                    "price": "8500",
                    "mileage": "120000",
                    "datePartOne": "2015-01-01",
                    "fuel": "Benzine",
                    "transmission": "Handgeschakeld",
                    "description": 'A "quoted" feature',
                    "licensePlateScreen": "GB-178-T",
                }
            ]
        }
        encoded = json.dumps(json.dumps(data))[1:-1]
        body = "<script>Action.loadVehicles($.parseJSON('" + encoded + "'));</script>"
        p = parse_page("bmw", body, "https://occasions.bmw.nl/bmw/zoeken/resultaten")
        self.assertEqual(len(p.listings), 1)
        self.assertEqual(p.listings[0].kenteken, "GB178T")

    def test_marktnet_detail_keeps_trade_price_warning(self):
        body = '<h1>BMW 218i Active Tourer</h1><span itemprop="price">€ 7.950</span><div>Beschrijving Bouwjaar: dec. 2015 Tellerstand: 133.769 km Kenteken: 7-ZHL-92 Benzine Handgeschakeld Belangrijk: De verkoopprijs betreft de handelsprijs Andere advertenties</div>'
        p = parse_page("marktnet", body, "https://www.marktnet.nl/autos/bmw/bmw-42.htm")
        self.assertEqual(p.listings[0].kenteken, "7ZHL92")
        self.assertIn("handelsprijs", p.listings[0].description)
        self.assertTrue(p.listings[0].raw["detail_complete"])

    def test_repeated_search_page_is_refetched_before_ingestion(self):
        import tempfile
        from pathlib import Path

        from car_market.collect import Collector
        from car_market.db import Database
        from car_market.fetch import Response

        def body(identity, page):
            return (
                '<script type="application/json">'
                + json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "numberOfResults": 2,
                                "numberOfPages": 2,
                                "pageQuery": {"page": page},
                                "listings": [
                                    {
                                        "id": identity,
                                        "url": "/aanbod/" + identity,
                                        "vehicle": {
                                            "motorTypeName": "218i",
                                            "variant": "Active Tourer",
                                        },
                                        "price": {"priceRaw": 8500},
                                    }
                                ],
                            }
                        }
                    }
                )
                + "</script>"
            )

        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "m.sqlite3")
            co = Collector(db, {"workers": 1})
            calls = []

            def fetch(url, refresh=False):
                calls.append(refresh)
                page = 2 if "page=2" in url else 1
                identity = "b" if refresh else "a"
                return Response(url, url, 200, body(identity, page), "2026-10-04T10:00:00+00:00")

            co.fetcher.get = fetch
            co.collect_query(
                db.start_run(),
                {
                    "source": "autoscout24",
                    "key": "all",
                    "url": "https://example.test/lst",
                    "pagination": "page",
                    "coverage": "enumerate",
                },
            )
            self.assertEqual(calls, [False, False, True])
            self.assertEqual(
                db.conn.execute("SELECT status FROM source_runs").fetchone()[0], "complete"
            )
            self.assertEqual(db.summary()["listings"], 2)
            db.conn.close()

    def test_search_raw_is_retained_on_change_and_not_on_unchanged_poll(self):
        import tempfile
        from pathlib import Path

        from car_market.collect import Collector
        from car_market.db import Database
        from car_market.fetch import Response

        def body(price):
            return (
                '<script type="application/json">'
                + json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "numberOfResults": 1,
                                "numberOfPages": 1,
                                "pageQuery": {},
                                "listings": [
                                    {
                                        "id": "a",
                                        "url": "/aanbod/a",
                                        "vehicle": {
                                            "motorTypeName": "218i",
                                            "variant": "Active Tourer",
                                        },
                                        "price": {"priceRaw": price},
                                    }
                                ],
                            }
                        }
                    }
                )
                + "</script>"
            )

        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "m.sqlite3")
            co = Collector(db, {})
            for price, timestamp in [
                (8500, "2026-10-04T10:00:00+00:00"),
                (8500, "2026-10-04T10:05:00+00:00"),
                (8000, "2026-10-04T10:10:00+00:00"),
            ]:
                response = Response(
                    "https://example.test/lst",
                    "https://example.test/lst",
                    200,
                    body(price),
                    timestamp,
                )
                co.ingest_response(db.start_run(), "autoscout24", "all", response, queue=False)
            self.assertEqual(
                db.conn.execute(
                    "SELECT COUNT(*) FROM fetches WHERE raw_path IS NOT NULL"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(db.summary()["observations"], 2)
            self.assertEqual(
                db.conn.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='price_eur_changed'"
                ).fetchone()[0],
                1,
            )
            db.conn.close()

    def test_second_page_is_a_real_distinct_route(self):
        self.assertEqual(
            page_url(
                {
                    "url": "https://www.viabovag.nl/auto/merk-bmw/model-2-serie-active-tourer",
                    "pagination": "viabovag",
                },
                2,
                None,
            ),
            "https://www.viabovag.nl/auto/merk-bmw/model-2-serie-active-tourer/pagina-2",
        )

    def test_react_stream_decoding_reads_hidden_registration(self):
        ad = {
            "advertentieId": 42,
            "url": "https://www.autotrack.nl/a/bmw-42",
            "autogegevens": {
                "algemeen": {
                    "merknaam": "BMW",
                    "modelnaam": "2-Serie Active Tourer",
                    "uitvoering": "218i",
                    "brandstofsoort": "BENZINE",
                    "transmissietype": "HANDMATIG",
                },
                "geschiedenis": {"bouwjaar": 2015, "kilometerstand": 120000},
            },
            "prijs": {"totaalInclusiefBtw": 8500},
            "kenteken": "GB178T",
        }
        stream = "2a:" + json.dumps({"hits": [ad]}) + "\n"
        body = "<script>self.__next_f.push(" + json.dumps([1, stream]) + ")</script>"
        p = parse_page("autotrack", body, "https://www.autotrack.nl/")
        self.assertEqual(p.listings[0].kenteken, "GB178T")
        self.assertEqual(p.listings[0].scope, "target")


if __name__ == "__main__":
    unittest.main()
