"""Relational catalog contracts; no collector, network, or notification calls."""

import dataclasses
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from car_market import vehicle_catalog
from car_market.db import SCHEMA
from car_market.parse import parse_page
from car_market.vehicle_catalog import catalog, get_vehicle

NOW = "2026-10-07T12:00:00+00:00"
RECENT = "2026-10-07T11:00:00+00:00"
OLD = "2026-10-05T10:00:00+00:00"
CONFIG = {"as_of": NOW}
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    yield connection
    connection.close()


def vehicle(conn, id="car", kenteken="AB123C", vin=None):
    conn.execute(
        "INSERT INTO vehicles(id,kenteken,vin,created_at) VALUES (?,?,?,?)",
        (id, kenteken, vin, OLD),
    )
    return id


def observation(conn, listing_id, timestamp=RECENT, *, fetch_id=None, **detail):
    payload = {"title": "BMW 218i Active Tourer", **detail}
    columns = {
        "title": payload["title"],
        "price_eur": payload.get("price"),
        "price_type": payload.get("price_type", "advertised_price"),
        "mileage_km": payload.get("mileage"),
        "year": payload.get("year"),
        "transmission": payload.get("transmission"),
        "fuel": payload.get("fuel"),
        "seller": payload.get("seller"),
        "city": payload.get("city"),
    }
    names = ["listing_id", "observed_at", "content_hash", "details_json", "fetch_id", *columns]
    values = [listing_id, timestamp, "fixture", json.dumps(payload), fetch_id, *columns.values()]
    return conn.execute(
        f"INSERT INTO observations({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
        values,
    ).lastrowid


def ad(
    conn,
    id="car",
    source="autoscout24",
    *,
    timestamp=RECENT,
    last_seen=None,
    availability="observed",
    scope="target",
    external_id=None,
    **fields,
):
    external = external_id or source + "-own-id"
    listing_id = conn.execute(
        "INSERT INTO listings(source,external_id,vehicle_id,url,first_seen,last_seen,availability,scope) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            source,
            external,
            id,
            f"https://{source}.test/{external}",
            OLD,
            last_seen or timestamp,
            availability,
            scope,
        ),
    ).lastrowid
    observation(conn, listing_id, timestamp, **fields)
    return listing_id


def test_linked_platforms_form_one_enriched_card_with_own_offers(conn):
    vehicle(conn)
    first = ad(conn, year=2016, price=8500, fuel="petrol")
    second = ad(
        conn,
        source="marktplaats",
        transmission="manual",
        mileage=123000,
        seller="Own dealer",
        city="Diemen",
        price=8300,
        price_type="FIXED",
    )
    cards = catalog(conn, CONFIG)
    assert len(cards) == 1
    card = cards[0]
    assert (card["id"], card["identity_status"]) == ("car", "confirmed")
    assert (card["year"], card["fuel"], card["transmission"], card["mileage_km"]) == (
        2016,
        "petrol",
        "manual",
        123000,
    )
    assert card["seller"] == "Own dealer" and card["city"] == "Diemen"
    assert card["field_evidence"]["year"][0]["listing_id"] == first
    assert card["field_evidence"]["transmission"][0]["listing_id"] == second
    assert [
        (o["listing_id"], o["source"], o["external_id"], o["price_eur"]) for o in card["offers"]
    ] == [
        (first, "autoscout24", "autoscout24-own-id", 8500),
        (second, "marktplaats", "marktplaats-own-id", 8300),
    ]
    assert card["offers"][0]["url"] == "https://autoscout24.test/autoscout24-own-id"
    assert card["offers"][1]["url"] == "https://marktplaats.test/marktplaats-own-id"
    assert card["price_min_eur"] == 8300 and card["price_max_eur"] == 8500
    assert card["active_offer_count"] == 2
    json.dumps(cards, allow_nan=False)


def test_unconfirmed_ids_never_merge_on_identical_year_mileage(conn):
    for id, source in [("one", "autoscout24"), ("two", "marktplaats")]:
        vehicle(conn, id, kenteken=None)
        ad(conn, id, source, year=2016, mileage=123000, transmission="manual")
    cards = catalog(conn, CONFIG)
    assert [c["id"] for c in cards] == ["one", "two"]
    assert all(c["identity_status"] == "provisional" and c["listing_count"] == 1 for c in cards)


@pytest.mark.parametrize(
    "kenteken,vin,status",
    [
        (None, None, "provisional"),
        ("AB123C", None, "confirmed"),
        (None, "WBA2A11000V123456", "confirmed"),
    ],
)
def test_empty_placeholders_have_stable_serializable_shape(conn, kenteken, vin, status):
    vehicle(conn, kenteken=kenteken, vin=vin)
    card = catalog(conn, CONFIG)[0]
    assert card["id"] == "car" and card["identity_status"] == status
    assert card["offers"] == [] and card["photos"] == [] and card["descriptions"] == []
    assert not card["is_live"] and card["price_min_eur"] is None
    assert card["year"] is None and card["description"] is None
    ad(conn, year=2016)
    assert card.keys() == catalog(conn, CONFIG)[0].keys()
    json.dumps(card, allow_nan=False)


@pytest.mark.parametrize(
    "availability,scope,last_seen,live",
    [
        ("historical_only", "target", RECENT, False),
        ("observed", "historical_only", RECENT, False),
        ("removed", "target", RECENT, False),
        ("sold", "target", RECENT, False),
        ("observed", "target", OLD, False),
        ("observed", "target", "2026-10-06T23:00:00+00:00", True),
        ("observed", "target", "2026-10-06T22:59:59+00:00", False),
    ],
)
def test_availability_and_default_13_hour_cutoff(conn, availability, scope, last_seen, live):
    vehicle(conn)
    ad(conn, availability=availability, scope=scope, last_seen=last_seen, price=8000)
    card = catalog(conn, CONFIG)[0]
    assert card["is_live"] is live
    assert card["offers"][0]["is_live"] is live
    assert card["offers"][0]["availability"] == availability
    assert card["price_min_eur"] == (8000 if live else None)


@pytest.mark.parametrize(
    "price,price_type",
    [
        (4500, "export_price"),
        (4500, "conditional_price"),
        (4500, "unknown"),
        (0, "advertised_price"),
        (-1, "FIXED"),
        (None, "FIXED"),
        (float("inf"), "advertised_price"),
    ],
)
def test_only_positive_current_retail_prices_enter_range(conn, price, price_type):
    vehicle(conn)
    ad(conn, price=price, price_type=price_type)
    ad(conn, source="marktplaats", price=8200)
    card = catalog(conn, CONFIG)[0]
    assert card["price_min_eur"] == card["price_max_eur"] == 8200
    assert not card["offers"][0]["current_price"]
    json.dumps(card, allow_nan=False)


def test_recent_presence_does_not_refresh_old_price_or_inherited_field(conn):
    vehicle(conn)
    ad(conn, price=5000, fuel="diesel", timestamp=OLD, last_seen=RECENT)
    ad(conn, source="marktplaats", fuel="petrol", price=8000)
    card = catalog(conn, CONFIG)[0]
    assert card["fuel"] == "petrol"
    assert card["price_min_eur"] == 8000
    assert card["offers"][0]["is_live"]
    assert not card["offers"][0]["current_price"]


def test_inherited_old_fuel_loses_to_fresh_own_statement_and_keeps_fetch_origin(conn):
    vehicle(conn)
    old_fetch = conn.execute(
        "INSERT INTO fetches(source,url,fetched_at,status) VALUES (?,?,?,200)",
        ("autoscout24", "https://autoscout24.test/detail", OLD),
    ).lastrowid
    new_fetch = conn.execute(
        "INSERT INTO fetches(source,url,fetched_at,status) VALUES (?,?,?,200)",
        ("autoscout24", "https://autoscout24.test/search", RECENT),
    ).lastrowid
    ad(
        conn,
        fuel="diesel",
        fetch_id=new_fetch,
        raw={
            "field_evidence": {
                "fuel": {
                    "source": "autoscout24",
                    "fetch_id": old_fetch,
                    "observed_at": OLD,
                    "inherited": True,
                }
            }
        },
    )
    ad(conn, source="marktplaats", fuel="petrol")
    card = catalog(conn, CONFIG)[0]
    assert card["fuel"] == "petrol" and card["field_conflicts"]["fuel"] == []
    old = next(s for s in card["field_alternatives"]["fuel"] if s["value"] == "diesel")
    assert old["origin"]["observed_at"] == OLD and not old["origin"]["fresh"]
    assert old["origin"]["fetch_id"] == old_fetch
    assert old["origin"]["fetch_url"] == "https://autoscout24.test/detail"
    assert old["origin"]["fetched_at"] == OLD


def test_missing_inherited_time_is_unknown_not_new_search_time(conn):
    vehicle(conn)
    ad(conn, fuel="diesel", raw={"field_evidence": {"fuel": {"inherited": True}}})
    ad(conn, source="marktplaats", fuel="petrol")
    card = catalog(conn, CONFIG)[0]
    assert card["fuel"] == "petrol"
    assert card["field_alternatives"]["fuel"][0]["origin"]["observed_at"] is None


def test_price_freshness_uses_field_time_not_recent_observation(conn):
    vehicle(conn)
    ad(
        conn,
        price=4500,
        raw={
            "field_evidence": {
                "price": {
                    "observed_at": OLD,
                    "inherited": True,
                }
            }
        },
    )
    card = catalog(conn, CONFIG)[0]
    assert card["offers"][0]["is_live"]
    assert card["offers"][0]["price_eur"] == 4500
    assert not card["offers"][0]["current_price"]
    assert card["price_min_eur"] is None


@pytest.mark.parametrize(
    "field,first,second",
    [
        ("year", 2015, 2016),
        ("fuel", "petrol", "diesel"),
        ("transmission", "manual", "automatic"),
    ],
)
def test_fresh_stable_disagreement_is_unknown_even_with_majority(conn, field, first, second):
    vehicle(conn)
    for source, value in [("autoscout24", first), ("marktplaats", first), ("autotrack", second)]:
        ad(conn, source=source, **{field: value})
    card = catalog(conn, CONFIG)[0]
    assert card[field] is None
    assert card["field_evidence"][field] == []
    assert {s["value"] for s in card["field_alternatives"][field]} == {first, second}
    assert card["field_conflicts"][field][0]["kind"] == "contradictory_statements"


@pytest.mark.parametrize(
    "conflict", [{"fuel": {"own_table": "petrol", "schema": "diesel"}}, ["fuel"]]
)
def test_source_conflict_never_refills_sql_residue_or_complementary_ad(conn, conflict):
    vehicle(conn)
    listing = ad(conn, fuel="diesel", raw={"field_conflicts": conflict})
    row = conn.execute(
        "SELECT details_json FROM observations WHERE listing_id=?", (listing,)
    ).fetchone()
    payload = json.loads(row[0])
    payload["fuel"] = None  # Reproduce the legacy SQL COALESCE residue.
    conn.execute(
        "UPDATE observations SET details_json=? WHERE listing_id=?", (json.dumps(payload), listing)
    )
    ad(conn, source="marktplaats", fuel="petrol")
    card = catalog(conn, CONFIG)[0]
    assert card["fuel"] is None
    assert len(card["field_alternatives"]["fuel"]) == 1
    assert card["field_conflicts"]["fuel"][0]["kind"] == "source_field_conflict"


def test_latest_valid_mileage_and_dated_alternatives_are_not_stable_conflicts(conn):
    vehicle(conn)
    first = ad(conn, mileage=90000, timestamp=OLD)
    observation(conn, first, "2026-10-07T08:00:00+00:00", mileage=100000)
    observation(conn, first, "2026-10-07T10:00:00+00:00", mileage=-1)
    ad(
        conn,
        source="marktplaats",
        mileage=99000,
        raw={
            "field_evidence": {
                "mileage": {
                    "observed_at": "2026-10-07T07:00:00+00:00",
                    "inherited": True,
                }
            }
        },
    )
    card = catalog(conn, CONFIG)[0]
    assert card["mileage_km"] == 100000
    assert {s["value"] for s in card["field_alternatives"]["mileage_km"]} == {90000, 99000, 100000}
    assert card["field_conflicts"]["mileage_km"] == []
    assert card["field_evidence"]["mileage_km"][0]["observed_at"] == "2026-10-07T08:00:00+00:00"


def test_explicit_mileage_conflict_does_not_restore_prior_good_value(conn):
    vehicle(conn)
    listing = ad(conn, mileage=90000, timestamp=OLD)
    observation(conn, listing, mileage=None, raw={"field_conflicts": ["mileage"]})
    card = catalog(conn, CONFIG)[0]
    assert card["mileage_km"] is None and card["field_conflicts"]["mileage_km"]


def test_repeated_inheritance_does_not_multiply_identical_field_origins(conn):
    vehicle(conn)
    listing = ad(
        conn, mileage=90000, description="One seller description", images=["https://img.test/a"]
    )
    for timestamp in ["2026-10-07T11:10:00+00:00", "2026-10-07T11:20:00+00:00"]:
        observation(
            conn,
            listing,
            timestamp,
            mileage=90000,
            description="One seller description",
            images=["https://img.test/a"],
            raw={
                "field_evidence": {
                    field: {"observed_at": RECENT, "inherited": True}
                    for field in ["mileage", "description", "images"]
                }
            },
        )
    card = catalog(conn, CONFIG)[0]
    assert len(card["field_alternatives"]["mileage_km"]) == 1
    assert len(card["field_evidence"]["mileage_km"]) == 1
    assert len(card["photos"]) == 1 and len(card["photos"][0]["evidence_ids"]) == 1
    assert len(card["descriptions"]) == 1 and len(card["descriptions"][0]["evidence_ids"]) == 1
    assert card["field_evidence"]["mileage_km"][0]["observed_at"] == RECENT


def test_descriptions_and_photos_union_with_all_origins_and_truthful_completeness(conn):
    vehicle(conn)
    first = ad(
        conn,
        description="Full seller text with service invoices.",
        images=["https://img.test/a.jpg", "https://img.test/a.jpg"],
        raw={"detail_complete": True},
    )
    second = ad(
        conn,
        source="marktplaats",
        description="Full  seller text with service invoices.",
        images=["https://img.test/a.jpg", "https://img.test/b.jpg"],
        raw={"detail_complete": True},
    )
    observation(
        conn,
        second,
        NOW,
        description="Much longer seller description with many words but truncated...",
        images=["https://img.test/b.jpg"],
        raw={"detail_complete": True},
    )
    card = catalog(conn, CONFIG)[0]
    assert len(card["descriptions"]) == 2
    common = next(d for d in card["descriptions"] if d["text"].startswith("Full"))
    assert {card["media_evidence"][key]["listing_id"] for key in common["evidence_ids"]} == {
        first,
        second,
    }
    assert card["images"] == ["https://img.test/a.jpg", "https://img.test/b.jpg"]
    assert len(card["photos"][0]["evidence_ids"]) == 2
    assert card["description"].startswith("Much longer")
    assert card["description_may_be_truncated"] and not card["detail_complete"]


def test_real_marktnet_excerpt_never_becomes_complete(conn):
    from car_market.marktnet import parse_xml

    item = parse_xml((FIXTURES / "marktnet_detail_quality/94560620.xml").read_text()).listings[0]
    vehicle(conn, kenteken=item.kenteken)
    payload = dataclasses.asdict(item)
    payload.pop("source")
    payload.pop("external_id")
    ad(conn, source="marktnet", **payload)
    card = catalog(conn, CONFIG)[0]
    assert card["description"] == item.description.strip()
    assert card["description_may_be_truncated"] and not card["detail_complete"]


def test_real_marktplaats_export_offer_keeps_own_price_but_not_current_range(conn):
    from car_market.sale_terms import classify

    item = parse_page(
        "marktplaats",
        (FIXTURES / "quality_primary/m2450755184.html").read_text(),
        "https://www.marktplaats.nl/v/auto-s/bmw/m2450755184-bmw",
    ).listings[0]
    classify(item)
    vehicle(conn, kenteken=item.kenteken)
    payload = dataclasses.asdict(item)
    payload.pop("source")
    payload.pop("external_id")
    ad(conn, source="marktplaats", **payload)
    card = catalog(conn, CONFIG)[0]
    assert card["offers"][0]["price_eur"] == 4950
    assert card["offers"][0]["price_type"] == "export_price"
    assert card["price_min_eur"] is None and not card["offers"][0]["current_price"]


def test_v3_flattened_aliases_resolve_without_migration(conn):
    vehicle(conn)
    ad(conn, price=8000)
    assert get_vehicle(conn, "car", CONFIG) == catalog(conn, CONFIG)[0]
    assert get_vehicle(conn, "absent", CONFIG) is None
    conn.executescript("""CREATE TABLE IF NOT EXISTS vehicle_aliases(
        alias_id TEXT PRIMARY KEY,vehicle_id TEXT,merged_at TEXT,reason TEXT,evidence_json TEXT);
        INSERT INTO vehicle_aliases VALUES
        ('old','car','2026-10-07','plate','{}'),('older','car','2026-10-07','vin','{}');""")
    for id in ["old", "older"]:
        assert get_vehicle(conn, id, CONFIG) == get_vehicle(conn, "car", CONFIG)
    assert get_vehicle(conn, "absent", CONFIG) is None
    assert len(catalog(conn, CONFIG)) == 1


def test_optional_alias_table_and_legacy_malformed_chains(conn):
    vehicle(conn)
    conn.execute("DROP TABLE IF EXISTS vehicle_aliases")
    assert get_vehicle(conn, "old", CONFIG) is None
    # A legacy/unconstrained relation can contain chains, dangling IDs and cycles.
    conn.executescript("""CREATE TABLE vehicle_aliases(alias_id TEXT PRIMARY KEY,vehicle_id TEXT);
        INSERT INTO vehicle_aliases VALUES
        ('old','car'),('older','old'),('loop1','loop2'),('loop2','loop1'),('dangling','missing');""")
    assert get_vehicle(conn, "older", CONFIG)["id"] == "car"
    for id in ["loop1", "dangling", "absent"]:
        assert get_vehicle(conn, id, CONFIG) is None


def test_query_count_is_bounded_with_many_vehicles_and_read_only_connection(conn):
    fetch = conn.execute(
        "INSERT INTO fetches(source,url,fetched_at) VALUES ('fixture','https://test',?)", (RECENT,)
    ).lastrowid
    for n in range(80):
        id = vehicle(conn, str(n), kenteken=None)
        ad(conn, id, f"source{n}", fuel="petrol", fetch_id=fetch)
    conn.commit()
    before = list(conn.iterdump())
    changes, factory, transaction = conn.total_changes, conn.row_factory, conn.in_transaction
    conn.execute("PRAGMA query_only=ON")
    queries = []
    conn.set_trace_callback(queries.append)
    cards = catalog(conn, CONFIG)
    conn.set_trace_callback(None)
    assert len(cards) == 80
    assert len(queries) == 5 and all(q.lstrip().upper().startswith("SELECT") for q in queries)
    assert conn.total_changes == changes and conn.row_factory is factory
    assert conn.in_transaction == transaction and list(conn.iterdump()) == before


def test_config_clock_accepts_datetime_offsets_and_interval_compatibility(conn):
    vehicle(conn)
    ad(conn, price=8000, last_seen="2026-10-07T00:00:00+02:00")
    assert not catalog(conn, CONFIG)[0]["is_live"]
    assert catalog(conn, {"as_of": datetime(2026, 10, 7, 12, tzinfo=UTC), "interval_hours": 7})[0][
        "is_live"
    ]
    assert catalog(conn, {"as_of": NOW, "freshness_hours": 15})[0]["is_live"]
    with pytest.raises(ValueError):
        catalog(conn, {"as_of": "invalid"})


def test_missing_observation_malformed_details_and_row_factory_are_supported(conn):
    vehicle(conn)
    listing = ad(conn, price=8000, year=2016)
    conn.execute("UPDATE observations SET details_json='not json' WHERE listing_id=?", (listing,))
    conn.execute(
        "INSERT INTO listings(source,external_id,vehicle_id,url,first_seen,last_seen,scope) "
        "VALUES ('empty','id','car','https://empty',?,?,'target')",
        (OLD, OLD),
    )
    conn.row_factory = sqlite3.Row
    card = catalog(conn, CONFIG)[0]
    assert card["year"] == 2016 and len(card["offers"]) == 2
    assert card["offers"][1]["observation_id"] is None
    json.dumps(card, allow_nan=False)


def test_sql_projection_drops_large_provider_payloads_without_changing_cards(conn, monkeypatch):
    vehicle(conn)
    listing = ad(
        conn,
        mileage=90000,
        price=8000,
        year=2016,
        fuel="petrol",
        description="Original complete seller description",
        images=["https://img.test/one"],
        raw={"detail_complete": True, "ownNext": {"translations": "x" * 75000}},
    )
    observation(
        conn,
        listing,
        NOW,
        mileage=91000,
        price=7900,
        year=2016,
        fuel="petrol",
        description="Longer seller description with invoices but truncated...",
        images=["https://img.test/one", "https://img.test/two"],
        raw={
            "detail_complete": True,
            "description_truncated": True,
            "description_may_be_truncated": True,
            "historical_only": False,
            "price_warning": None,
            "field_conflicts": {"transmission": ["manual", "automatic"]},
            "field_evidence": {"fuel": {"observed_at": OLD, "inherited": True}},
            "ownNext": {"translations": "y" * 75000},
        },
        all_translations={"nl": "z" * 75000},
    )
    projected_cards = catalog(conn, CONFIG)
    for table in ["observations", "latest_observations"]:
        rows = conn.execute(vehicle_catalog.SELECT_AD.format(observations=table)).fetchall()
        column = [
            d[0]
            for d in conn.execute(vehicle_catalog.SELECT_AD.format(observations=table)).description
        ].index("details_json")
        for row in rows:
            projected = json.loads(row[column])
            assert set(projected) == {"description", "images", "raw"}
            assert set(projected["raw"]) == {
                "field_evidence",
                "field_conflicts",
                "description_may_be_truncated",
                "description_truncated",
                "detail_complete",
                "historical_only",
                "price_warning",
            }
            assert projected["raw"]["detail_complete"] is True
            assert len(row[column]) < 1000
            assert "ownNext" not in row[column] and "all_translations" not in row[column]
    full_select = """SELECT l.*,l.id AS listing_id,o.id AS observation_id,
        o.observed_at,o.fetch_id,o.details_json,o.title,o.year,o.mileage_km,
        o.fuel,o.transmission,o.seller,o.city,o.price_eur,o.price_type
        FROM listings l LEFT JOIN {observations} o ON o.listing_id=l.id"""
    monkeypatch.setattr(vehicle_catalog, "SELECT_AD", full_select)
    assert projected_cards == catalog(conn, CONFIG)


@pytest.mark.parametrize("payload", [None, "not json", '{"raw":', "null", "[]", '"string"', "42"])
def test_projection_guards_malformed_json_and_nonobject_roots(conn, payload):
    vehicle(conn)
    listing = ad(conn, year=2016, price=8000)
    if payload is not None:  # details_json is NOT NULL; no observation covers SQL NULL.
        conn.execute(
            "UPDATE observations SET details_json=? WHERE listing_id=?", (payload, listing)
        )
    else:
        conn.execute("DELETE FROM observations WHERE listing_id=?", (listing,))
    card = catalog(conn, CONFIG)[0]
    assert card["description"] is None and card["images"] == []
    assert card["year"] == (2016 if payload is not None else None)
    assert card["price_min_eur"] == (8000 if payload is not None else None)
    json.dumps(card, allow_nan=False)


def test_latest_checks_per_provider_retain_original_time_and_identity_without_changing_year(conn):
    vehicle(conn)
    vehicle(conn, "other", kenteken=None)
    ad(conn, year=2016)
    fetch = conn.execute(
        "INSERT INTO fetches(source,url,fetched_at) VALUES ('rdw','https://rdw.test/vehicle',?)",
        (OLD,),
    ).lastrowid
    for id, provider, timestamp, data in [
        ("car", "rdw", "2026-10-04T08:00:00+00:00", {"datum_eerste_toelating": "20140101"}),
        (
            "car",
            "rdw",
            OLD,
            {"datum_eerste_toelating": "20150101", "tellerstandoordeel": "logisch"},
        ),
        ("car", "inspection", RECENT, {"status": "reported"}),
        ("other", "rdw", NOW, {"datum_eerste_toelating": "20220101"}),
    ]:
        conn.execute(
            "INSERT INTO vehicle_checks(vehicle_id,provider,checked_at,fetch_id,data_json) VALUES (?,?,?,?,?)",
            (id, provider, timestamp, fetch, json.dumps(data)),
        )
    card = get_vehicle(conn, "car", CONFIG)
    assert card["year"] == 2016
    assert card["checks"] == [
        {
            "provider": "inspection",
            "checked_at": RECENT,
            "fetch_id": fetch,
            "data": {"status": "reported"},
            "fresh": True,
        },
        {
            "provider": "rdw",
            "checked_at": OLD,
            "fetch_id": fetch,
            "data": {"datum_eerste_toelating": "20150101", "tellerstandoordeel": "logisch"},
            "fresh": False,
        },
    ]
    assert (
        get_vehicle(conn, "other", CONFIG)["checks"][0]["data"]["datum_eerste_toelating"]
        == "20220101"
    )
    assert catalog(conn, CONFIG)[0]["checks"] == card["checks"]
    json.dumps(card, allow_nan=False)


def test_photo_evidence_is_stored_once_and_referenced_by_multiple_urls(conn):
    vehicle(conn)
    ad(conn, images=["https://img.test/a", "https://img.test/b"])
    card = catalog(conn, CONFIG)[0]
    assert len(card["media_evidence"]) == 1
    assert card["photos"][0]["evidence_ids"] == card["photos"][1]["evidence_ids"]
    assert "origins" not in card["photos"][0]
    reference = card["photos"][0]["evidence_ids"][0]
    assert card["media_evidence"][reference]["source"] == "autoscout24"
