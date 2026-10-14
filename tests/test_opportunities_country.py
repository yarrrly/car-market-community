"""Country veto for legacy stored targets; native evidence and synthetic clocks stay distinct."""

import copy
import dataclasses
import gzip
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from car_market.collect import parse_market_page
from car_market.db import SCHEMA
from car_market.model_profiles import MERCEDES_PROFILE, own_country
from car_market.opportunities import evaluate, inventory
from car_market.provenance import value_hash
from car_market.vehicle_catalog import catalog

CLOCK = "2026-10-13T02:34:15.750753+00:00"
RECENT = "2026-10-13T01:34:15.750753+00:00"
OLD = "2026-10-11T22:54:46.986728+00:00"
FIXTURES = Path(__file__).parent / "fixtures/nl_autotrader_country"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Country consumer regression attempted network access")

    monkeypatch.setattr("socket.socket.connect", forbidden)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    yield connection
    connection.close()


def config(country="NL", as_of=CLOCK):
    return {"scope": {"country": country}, "as_of": as_of, "include_history": False}


def native_payload():
    record = json.loads((FIXTURES / "manifest.json").read_text())[
        "autotrader_foreign_negative"
    ]
    compressed = (FIXTURES / record["file"]).read_bytes()
    body = gzip.decompress(compressed)
    assert hashlib.sha256(body).hexdigest() == record["projected_body_sha256"]
    assert hashlib.sha256(compressed).hexdigest() == record["projected_gzip_sha256"]
    assert record["original_capture_body_sha256"] == (
        "0928ceecabc9adc9558982882217bfafe1233c81a0095d1623a0f523e97d75ee"
    )
    assert record["original_capture_id"] == 266483 and record["captured_at"] == OLD
    page = parse_market_page(record["source"], body.decode(), record["final_url"])
    own = next(
        item for item in page.listings if item.external_id == record["native_id"]
    )
    assert own_country(own.raw)[0] == "DE"
    assert (own.price, own.year, own.mileage, own.transmission) == (
        21871,
        2023,
        30889,
        "automatic",
    )
    return dataclasses.asdict(own), record


def seed(
    conn,
    key,
    *,
    raw=None,
    payload=None,
    price=9000,
    year=2018,
    mileage=100000,
    seen=RECENT,
    observed=RECENT,
    verification=None,
    scope="target",
    availability="observed",
    identity=None,
    confirmed=False,
):
    """Reconstruct stored labels with SQL; do not bypass a live ingestion guard."""
    data = (
        copy.deepcopy(payload)
        if payload
        else {
            "source": "fixture",
            "external_id": key,
            "url": "https://example.test/" + key,
            "title": "BMW 218i Active Tourer",
            "price": price,
            "price_type": "advertised_price",
            "year": year,
            "mileage": mileage,
            "fuel": "petrol",
            "transmission": "manual",
            "raw": copy.deepcopy(raw or {}),
        }
    )
    root = identity or key
    conn.execute(
        "INSERT OR IGNORE INTO vehicles(id,vin,created_at) VALUES (?,?,?)",
        (root, "synthetic-confirmed-" + root if confirmed else None, observed),
    )
    listing = conn.execute(
        "INSERT INTO listings(source,external_id,vehicle_id,url,first_seen,last_seen,availability,scope) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            data["source"],
            data["external_id"],
            root,
            data["url"],
            observed,
            seen,
            availability,
            scope,
        ),
    ).lastrowid
    fetch = conn.execute(
        "INSERT INTO fetches(source,url,final_url,fetched_at,status,content_hash,transport) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            data["source"],
            data["url"],
            data["url"],
            observed,
            200,
            "a" * 64,
            "isolated_metadata",
        ),
    ).lastrowid
    data["scope"] = scope
    data["raw"].setdefault("field_evidence", {})["price"] = {
        "source": data["source"],
        "external_id": data["external_id"],
        "listing_id": listing,
        "fetch_id": fetch,
        "observed_at": observed,
        "inherited": False,
    }
    columns = (
        "listing_id,observed_at,content_hash,title,price_eur,price_type,year,mileage_km,"
        "transmission,fuel,scope,details_json,fetch_id"
    )
    conn.execute(
        f"INSERT INTO observations({columns}) VALUES ({','.join('?' for _ in range(13))})",
        (
            listing,
            observed,
            "isolated-structured-state",
            data["title"],
            data["price"],
            data["price_type"],
            data["year"],
            data["mileage"],
            data["transmission"],
            data["fuel"],
            scope,
            json.dumps(data),
            fetch,
        ),
    )
    if verification:
        # A new confirmation is synthetic unless explicitly using original capture time.
        proof_fetch = conn.execute(
            "INSERT INTO fetches(source,url,final_url,fetched_at,status,content_hash,transport) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                data["source"],
                data["url"],
                data["url"],
                verification,
                200,
                "b" * 64,
                "synthetic_confirmation",
            ),
        ).lastrowid
        proof = {
            "source": data["source"],
            "listing_id": listing,
            "external_id": data["external_id"],
            "fetch_id": proof_fetch,
            "observed_at": verification,
            "inherited": False,
            "conflict": False,
            "legacy_origin_time_unverified": False,
        }
        conn.execute(
            "INSERT INTO field_verifications VALUES (?,?,?,?,?,?)",
            (
                listing,
                "price",
                verification,
                proof_fetch,
                value_hash(data["price"]),
                json.dumps(proof),
            ),
        )
    return listing


def state(conn):
    return list(conn.iterdump())


def read_inventory(conn, settings):
    conn.commit()
    conn.execute("PRAGMA query_only=ON")
    before, changes = state(conn), conn.total_changes
    result = inventory(conn, settings)
    assert conn.total_changes == changes and state(conn) == before
    return result


COUNTRY_CASES = [
    ({}, True, True),
    ({"market_context_country": "NL"}, True, True),
    ({"market_context_country": "DE"}, True, True),
    ({"recommended": [{"location": {"countryCode": "BE"}}]}, True, True),
    ({"location": {"countryCode": "NL"}}, True, False),
    ({"location": {"countryCode": "DE"}}, False, True),
    ({"location": {"countryCode": "BE"}}, False, False),
    ({"location": {"countryCode": None}}, True, True),
    ({"country_code": "NLD"}, True, False),
    ({"market_country": "Deutschland"}, False, True),
    ({"dealer": {"address": {"country": {"code": "DEU"}}}}, False, True),
    ({"seller": {"address": {"country": "Nederland"}}}, True, False),
    ({"address": {"countryCode": "NL"}}, True, False),
    ({"dealerAddress": {"postalCode": "1234 AB"}}, True, False),
    ({"location": {"countryCode": "DE"}, "country_code": "NL"}, False, False),
    (
        {"location": {"countryCode": "NL"}, "seller": {"countryCode": "BE"}},
        False,
        False,
    ),
]


@pytest.mark.parametrize("country", ["NL", "DE"])
@pytest.mark.parametrize("raw,allow_nl,allow_de", COUNTRY_CASES)
def test_conclusive_foreign_or_conflict_veto_but_unknown_policy_is_unchanged(
    conn, country, raw, allow_nl, allow_de
):
    seed(conn, "country-control", raw=raw)
    expected = allow_de if country == "DE" else allow_nl
    assert len(read_inventory(conn, config(country))) == int(expected)


def test_no_country_config_preserves_default_NL_and_vetoes_owned_DE(conn):
    seed(conn, "own-DE", raw={"location": {"countryCode": "DE"}})
    assert not read_inventory(conn, {"as_of": CLOCK})


@pytest.mark.parametrize("country,expected", [("NL", 0), ("DE", 1)])
@pytest.mark.parametrize("fresh_confirmation", [False, True])
def test_carried_native_DE_quote_with_historical_target_label(
    conn, country, expected, fresh_confirmation
):
    payload, record = native_payload()
    seen = RECENT if fresh_confirmation else record["captured_at"]
    verification = RECENT if fresh_confirmation else None
    as_of = (
        CLOCK
        if fresh_confirmation
        else (
            datetime.fromisoformat(record["captured_at"]) + timedelta(minutes=4)
        ).isoformat()
    )
    seed(
        conn,
        "native-foreign",
        payload=payload,
        observed=record["captured_at"],
        seen=seen,
        verification=verification,
    )
    assert len(read_inventory(conn, config(country, as_of))) == expected


@pytest.mark.parametrize("country", ["NL", "DE"])
def test_original_native_quote_remains_visible_without_current_eligibility(
    conn, country
):
    payload, record = native_payload()
    seed(
        conn,
        "old-native",
        payload=payload,
        observed=record["captured_at"],
        seen=record["captured_at"],
    )
    assert not read_inventory(conn, config(country))
    card = catalog(conn, config(country))[0]
    assert card["offers"][0]["price_eur"] == payload["price"]
    assert card["offers"][0]["observed_at"] == record["captured_at"]
    assert not card["offers"][0]["current_price"] and card["price_min_eur"] is None


@pytest.mark.parametrize("country", ["NL", "DE"])
@pytest.mark.parametrize("latest_matches", [False, True])
def test_country_veto_reads_latest_publication_not_an_older_own_country(
    conn, country, latest_matches
):
    foreign = "DE" if country == "NL" else "NL"
    prior_country, latest_country = (
        (foreign, country) if latest_matches else (country, foreign)
    )
    listing = seed(
        conn,
        "publication",
        raw={"location": {"countryCode": prior_country}},
        observed=OLD,
    )
    previous = conn.execute(
        "SELECT * FROM observations WHERE listing_id=?", (listing,)
    ).fetchone()
    payload = json.loads(previous["details_json"])
    payload["raw"] = {"location": {"countryCode": latest_country}}
    conn.execute(
        "INSERT INTO observations(listing_id,observed_at,content_hash,title,price_eur,price_type,"
        "year,mileage_km,transmission,fuel,scope,details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            listing,
            RECENT,
            "synthetic-new-own-location",
            payload["title"],
            payload["price"],
            payload["price_type"],
            payload["year"],
            payload["mileage"],
            payload["transmission"],
            payload["fuel"],
            "target",
            json.dumps(payload),
        ),
    )
    assert len(read_inventory(conn, config(country))) == int(latest_matches)
    assert (
        conn.execute(
            "SELECT details_json FROM observations WHERE id=?", (previous["id"],)
        ).fetchone()[0]
        == previous["details_json"]
    )


@pytest.mark.parametrize("country,foreign", [("NL", "DE"), ("DE", "NL")])
def test_foreign_cheaper_alias_is_filtered_before_same_root_price_choice(
    conn, country, foreign
):
    own = seed(
        conn,
        "own",
        raw={"location": {"countryCode": country}},
        identity="one-root",
        price=12000,
    )
    seed(
        conn,
        "foreign",
        raw={"location": {"countryCode": foreign}},
        identity="one-root",
        price=8000,
    )
    car = read_inventory(conn, config(country))[0]
    assert (
        car["listing_id"] == own and car["price_eur"] == 12000 and car["ad_count"] == 1
    )
    assert car["urls"] == ["https://example.test/own"]


@pytest.mark.parametrize(
    "country,peer_country,expected_peers",
    [
        ("NL", "DE", 0),
        ("DE", "NL", 0),
        ("NL", "NL", 6),
        ("DE", "DE", 6),
        ("NL", None, 6),
        ("DE", None, 6),
    ],
)
def test_country_guard_applies_to_peers_but_preserves_unknown_and_own_controls(
    conn, country, peer_country, expected_peers
):
    candidate = seed(conn, "candidate", raw={"location": {"countryCode": country}})
    for n in range(6):
        raw = {"location": {"countryCode": peer_country}} if peer_country else {}
        seed(conn, f"peer-{n}", price=12000, raw=raw, confirmed=True)
    conn.commit()
    conn.execute("PRAGMA query_only=ON")
    before = state(conn)
    result = evaluate(conn, config(country))
    own = next(car for car in result["cars"] if car["listing_id"] == candidate)
    assert own["peer_count"] == expected_peers
    assert own["peer_median_eur"] == (12000 if expected_peers else None)
    assert own["priority"] == (
        "inspect_urgently" if country == "NL" and expected_peers else "ordinary"
    )
    assert state(conn) == before


@pytest.mark.parametrize("country", ["NL", "DE"])
@pytest.mark.parametrize(
    "fault",
    [
        "stale_presence",
        "future_presence",
        "old_price",
        "future_price",
        "wrong_price_hash",
        "excluded",
        "pending",
        "removed",
        "bid",
        "price_conflict",
    ],
)
def test_country_match_does_not_bypass_existing_admission_criteria(
    conn, country, fault
):
    payload = {
        "source": "fixture",
        "external_id": fault,
        "url": "https://example.test/" + fault,
        "title": "BMW 218i Active Tourer",
        "price": 9000,
        "price_type": "advertised_price",
        "year": 2018,
        "mileage": 100000,
        "fuel": "petrol",
        "transmission": "manual",
        "raw": {"location": {"countryCode": country}},
    }
    future = (datetime.fromisoformat(CLOCK) + timedelta(seconds=1)).isoformat()
    observed = (
        OLD if fault in {"old_price", "future_price", "wrong_price_hash"} else RECENT
    )
    seen = (
        OLD
        if fault == "stale_presence"
        else future
        if fault == "future_presence"
        else RECENT
    )
    verification = (
        future
        if fault == "future_price"
        else RECENT
        if fault == "wrong_price_hash"
        else None
    )
    if fault == "bid":
        payload["price_type"] = "MIN_BID"
    if fault == "price_conflict":
        payload["raw"]["field_conflicts"] = {"price": [9000, 12000]}
    seed(
        conn,
        fault,
        payload=payload,
        observed=observed,
        seen=seen,
        verification=verification,
        scope="excluded"
        if fault == "excluded"
        else "needs_confirmation"
        if fault == "pending"
        else "target",
        availability="removed" if fault == "removed" else "observed",
    )
    if fault == "wrong_price_hash":
        conn.execute("UPDATE field_verifications SET value_hash=?", (value_hash(1),))
    assert not read_inventory(conn, config(country))


@pytest.mark.parametrize("country", ["NL", "DE"])
@pytest.mark.parametrize(
    "age_seconds,expected",
    [(13 * 3600 - 1, 1), (13 * 3600, 1), (13 * 3600 + 1, 0), (-1, 0)],
)
def test_country_match_preserves_exact_price_and_presence_freshness_boundary(
    conn, country, age_seconds, expected
):
    capture = (
        datetime.fromisoformat(CLOCK) - timedelta(seconds=age_seconds)
    ).isoformat()
    seed(
        conn,
        "freshness-edge",
        raw={"location": {"countryCode": country}},
        observed=capture,
        seen=capture,
    )
    assert len(read_inventory(conn, config(country))) == expected


@pytest.mark.parametrize("country,expected", [("NL", 0), ("DE", 1)])
def test_unknown_country_does_not_change_existing_market_specific_numeric_bounds(
    conn, country, expected
):
    seed(conn, "unknown-values", year=None, mileage=None, price=100)
    assert len(read_inventory(conn, config(country))) == expected


def test_bound_Mercedes_profile_still_disables_opportunity_evaluation(conn):
    seed(conn, "own-country", raw={"location": {"countryCode": "NL"}})
    conn.execute("CREATE TABLE monitor_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO monitor_state VALUES ('market_scope',?)",
        (json.dumps({"country": "NL", "model_profile": MERCEDES_PROFILE}),),
    )
    conn.commit()
    conn.execute("PRAGMA query_only=ON")
    assert evaluate(conn, config()) == {
        "opportunities": [],
        "status": "not_configured_for_model_profile",
    }
