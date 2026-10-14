"""Fixture regressions for identity integrity and pagination failures."""

import json

import pytest
from car_market.collect import Collector
from car_market.db import Database
from car_market.fetch import Response
from car_market.parse import Listing


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "market.sqlite3")
    yield database
    database.conn.close()


def car(identity, registration=None, vin=None):
    return Listing(
        "fixture",
        identity,
        "https://example.test/" + identity,
        "BMW 218i Active Tourer",
        price=8500,
        fuel="petrol",
        transmission="manual",
        kenteken=registration,
        vin=vin,
    ).qualify()


@pytest.mark.parametrize("match", ["registration", "vin", "existing_registration", "existing_vin"])
def test_identity_merge_rejects_conflicting_other_identifier(db, match):
    run = db.start_run()
    if match == "registration":
        first, second, incoming = (
            car("a", "GB178T", "WBA12345678901234"),
            car("b"),
            car("b", "GB178T", "WBA12345678901235"),
        )
    elif match == "vin":
        first, second, incoming = (
            car("a", "GB178T", "WBA12345678901234"),
            car("b"),
            car("b", "5XPB51", "WBA12345678901234"),
        )
    elif match == "existing_registration":
        first, second, incoming = (
            car("a", "GB178T", "WBA12345678901234"),
            car("b", "5XPB51"),
            car("b", vin="WBA12345678901234"),
        )
    else:
        first, second, incoming = (
            car("a", "GB178T", "WBA12345678901234"),
            car("b", vin="WBA12345678901235"),
            car("b", "GB178T"),
        )
    db.observe(run, first, "2026-10-05T08:00:00+00:00")
    db.observe(run, second, "2026-10-05T08:00:00+00:00")
    before = [tuple(row) for row in db.conn.execute("SELECT * FROM vehicles ORDER BY id")]
    with pytest.raises(ValueError, match="conflict|Conflicting"):
        db.observe(db.start_run(), incoming, "2026-10-05T08:05:00+00:00")
    assert [tuple(row) for row in db.conn.execute("SELECT * FROM vehicles ORDER BY id")] == before
    assert db.summary()["observations"] == 2
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def search_body(identity, page=1, total=2, pages=2):
    return (
        '<script type="application/json">'
        + json.dumps(
            {
                "props": {
                    "pageProps": {
                        "numberOfResults": total,
                        "numberOfPages": pages,
                        "pageQuery": {"page": page},
                        "listings": [
                            {
                                "id": identity,
                                "url": "/aanbod/" + identity,
                                "vehicle": {
                                    "motorTypeName": "218i",
                                    "variant": "Active Tourer",
                                    "fuel": "Benzine",
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


@pytest.mark.parametrize("match", ["registration", "vin"])
def test_compatible_merge_preserves_complementary_identity(db, match):
    run = db.start_run()
    if match == "registration":
        first, second, incoming = (
            car("a", "GB178T"),
            car("b", vin="WBA12345678901234"),
            car("b", "GB178T"),
        )
    else:
        first, second, incoming = (
            car("a", vin="WBA12345678901234"),
            car("b", "GB178T"),
            car("b", vin="WBA12345678901234"),
        )
    db.observe(run, first, "2026-10-05T08:00:00+00:00")
    db.observe(run, second, "2026-10-05T08:00:00+00:00")
    db.observe(db.start_run(), incoming, "2026-10-05T08:05:00+00:00")
    vehicles = db.conn.execute("SELECT kenteken,vin FROM vehicles").fetchall()
    assert [tuple(row) for row in vehicles] == [("GB178T", "WBA12345678901234")]
    assert len({row[0] for row in db.conn.execute("SELECT vehicle_id FROM listings")}) == 1


@pytest.mark.parametrize("status", [403, 429, 500])
def test_failed_second_page_is_recorded_without_immediate_retry(db, status):
    collector = Collector(db, {"workers": 1})
    calls = []

    def fetch(url, refresh=False):
        calls.append((url, refresh))
        if "page=2" in url:
            return Response(
                url, url, status, None, "2026-10-05T08:00:00+00:00", "http_" + str(status)
            )
        return Response(url, url, 200, search_body("a"), "2026-10-05T08:00:00+00:00")

    collector.fetcher.get = fetch
    collector.collect_query(
        db.start_run(),
        {
            "source": "autoscout24",
            "key": "all",
            "url": "https://example.test/lst",
            "pagination": "page",
            "coverage": "enumerate",
        },
    )
    assert len(calls) == 2
    assert all(not refresh for _, refresh in calls)
    assert db.conn.execute("SELECT status FROM source_runs").fetchone()[0] == "partial"
    assert (
        db.conn.execute("SELECT COUNT(*) FROM fetches WHERE status=?", (status,)).fetchone()[0] == 1
    )
    assert db.summary()["listings"] == 1
