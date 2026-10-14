"""Bounded own confirmations refresh diagnostics without growing observations."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from car_market.db import Database
from car_market.parse import parse_page
from car_market.provenance import value_hash
from car_market.quality import report


@pytest.fixture
def native(db):
    path = Path(__file__).parent / "fixtures/quality_anwb_verification.json"
    native = json.loads(path.read_text())
    proof = native["capture"]
    assert (
        proof["original_decoded_sha256"]
        == "a7ba4d7bde849df1c5d320ab3bc8ac3d0ac1b0596bc5ea7eacb45f2b71d8d6cd"
    )
    # Search-envelope counters are test scaffolding for the one retained card.
    body = json.dumps(
        {"total": 1, "pageSize": 1, "pageNumber": 1, "hits": [native["native_card"]]}
    )
    card = next(
        item
        for item in parse_page("anwb", body, proof["url"]).listings
        if item.scope == "target" and item.price is not None
    )
    return body, proof, card


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / "isolated.sqlite3")
    yield value
    value.conn.close()


def observe(db, native, at, *, retain_raw=True):
    body, proof, card = native
    run = db.start_run()
    fetch = db.save_fetch(
        run, "anwb", proof["url"], proof["url"], 200, body, at, retain_raw=retain_raw
    )
    identity = db.observe(
        run, copy.deepcopy(card), at, fetch, query_key="native_search"
    )
    return identity, fetch


def seed(db, native):
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    recent = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    identity, fetch = observe(db, native, old)
    return identity, fetch, old, recent


def source(db):
    changes = db.conn.total_changes
    result = next(
        row
        for row in report(db.conn, {"queries": [], "freshness_hours": 13})["sources"]
        if row["source"] == "anwb"
    )
    assert changes == db.conn.total_changes
    return result


def history(db):
    return {
        name: [
            tuple(row)
            for row in db.conn.execute("SELECT * FROM " + name + " ORDER BY id")
        ]
        for name in ("observations", "events")
    }


@pytest.mark.parametrize("retain_raw", [True, False])
def test_same_native_value_refreshes_own_confirmation_without_observation_growth(
    db, native, retain_raw
):
    identity, _, old, recent = seed(db, native)
    before = history(db)
    payload = db.conn.execute(
        "SELECT details_json FROM latest_observations WHERE listing_id=?", (identity,)
    ).fetchone()[0]
    assert source(db)["fields"]["price"]["fresh_confirmations"] == 0
    new_identity, fetch = observe(db, native, recent, retain_raw=retain_raw)
    assert new_identity == identity
    assert history(db) == before
    assert (
        db.conn.execute(
            "SELECT details_json FROM latest_observations WHERE listing_id=?",
            (identity,),
        ).fetchone()[0]
        == payload
    )
    assert json.loads(payload)["raw"]["field_evidence"]["price"]["observed_at"] == old
    stored = db.conn.execute(
        "SELECT * FROM field_verifications WHERE listing_id=? AND field='price'",
        (identity,),
    ).fetchone()
    assert stored["verified_at"] == recent and stored["fetch_id"] == fetch
    assert stored["value_hash"] == value_hash(native[2].price)
    assert source(db)["fields"]["price"]["fresh_confirmations"] == 1


def test_many_identical_native_passes_keep_bounded_verifications_and_history(
    db, native
):
    identity, _, _, recent = seed(db, native)
    before = history(db)
    count = db.conn.execute("SELECT count(*) FROM field_verifications").fetchone()[0]
    for i in range(8):
        at = (datetime.fromisoformat(recent) + timedelta(seconds=i)).isoformat()
        observe(db, native, at, retain_raw=False)
    assert history(db) == before
    assert (
        db.conn.execute("SELECT count(*) FROM field_verifications").fetchone()[0]
        == count
    )
    assert source(db)["fields"]["price"]["fresh_confirmations"] == 1
    assert (
        db.conn.execute(
            "SELECT count(*) FROM listings WHERE id=?", (identity,)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "broken",
    [
        "hash",
        "source",
        "missing_source",
        "listing_id",
        "string_id",
        "external_id",
        "inherited",
        "conflict",
        "legacy",
        "origin_clock",
        "future",
        "cache_clock",
        "fetch_source",
        "fetch_denied",
        "fetch_error",
        "fetch_id",
        "missing_fetch",
        "missing_hash",
        "json",
        "list",
    ],
)
def test_invalid_or_foreign_verification_cannot_refresh_old_current_value(
    db, native, broken
):
    identity, _, _, recent = seed(db, native)
    _, fetch = observe(db, native, recent, retain_raw=False)
    row = db.conn.execute(
        "SELECT * FROM field_verifications WHERE listing_id=? AND field='price'",
        (identity,),
    ).fetchone()
    proof = json.loads(row["evidence_json"])
    if broken == "hash":
        db.conn.execute(
            "UPDATE field_verifications SET value_hash=? WHERE listing_id=? AND field='price'",
            (value_hash(native[2].price + 1), identity),
        )
    elif broken in {
        "source",
        "missing_source",
        "listing_id",
        "string_id",
        "external_id",
        "inherited",
        "conflict",
        "legacy",
        "origin_clock",
        "fetch_id",
        "json",
        "list",
    }:
        if broken == "source":
            proof["source"] = "autoscout24"
        elif broken == "missing_source":
            proof.pop("source")
        elif broken == "listing_id":
            proof["listing_id"] = identity + 1
        elif broken == "string_id":
            proof["listing_id"] = str(identity)
        elif broken == "external_id":
            proof["external_id"] = "wrong-native-id"
        elif broken == "inherited":
            proof["inherited"] = True
        elif broken == "conflict":
            proof["conflict"] = True
        elif broken == "legacy":
            proof["legacy_origin_time_unverified"] = True
        elif broken == "origin_clock":
            proof["observed_at"] = (
                datetime.fromisoformat(recent) - timedelta(hours=1)
            ).isoformat()
        elif broken == "fetch_id":
            proof["fetch_id"] = fetch + 1
        encoded = (
            "{bad"
            if broken == "json"
            else "[]"
            if broken == "list"
            else json.dumps(proof)
        )
        db.conn.execute(
            "UPDATE field_verifications SET evidence_json=? WHERE listing_id=? AND field='price'",
            (encoded, identity),
        )
    elif broken == "future":
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        proof["observed_at"] = future
        db.conn.execute(
            "UPDATE field_verifications SET verified_at=?,evidence_json=? WHERE listing_id=? AND field='price'",
            (future, json.dumps(proof), identity),
        )
        db.conn.execute("UPDATE fetches SET fetched_at=? WHERE id=?", (future, fetch))
    elif broken == "cache_clock":
        db.conn.execute(
            "UPDATE fetches SET fetched_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(days=2)).isoformat(), fetch),
        )
    elif broken == "fetch_source":
        db.conn.execute("UPDATE fetches SET source='autoscout24' WHERE id=?", (fetch,))
    elif broken == "fetch_denied":
        db.conn.execute("UPDATE fetches SET status=403 WHERE id=?", (fetch,))
    elif broken == "fetch_error":
        db.conn.execute(
            "UPDATE fetches SET error='detail_unparsed' WHERE id=?", (fetch,)
        )
    elif broken == "missing_fetch":
        db.conn.execute(
            "UPDATE field_verifications SET fetch_id=NULL WHERE listing_id=? AND field='price'",
            (identity,),
        )
    elif broken == "missing_hash":
        db.conn.execute("UPDATE fetches SET content_hash=NULL WHERE id=?", (fetch,))
    assert source(db)["fields"]["price"]["fresh_confirmations"] == 0


def test_stale_verification_hash_cannot_refresh_changed_current_price(db, native):
    identity, _, old, recent = seed(db, native)
    observe(db, native, recent)
    row = db.conn.execute(
        "SELECT id,details_json FROM latest_observations WHERE listing_id=?",
        (identity,),
    ).fetchone()
    payload = json.loads(row["details_json"])
    payload["price"] += 123
    db.conn.execute(
        "UPDATE observations SET price_eur=?,details_json=? WHERE id=?",
        (payload["price"], json.dumps(payload), row["id"]),
    )
    assert payload["raw"]["field_evidence"]["price"]["observed_at"] == old
    assert source(db)["fields"]["price"]["fresh_confirmations"] == 0


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("source", "autoscout24"),
        ("external_id", "different-owned-publication"),
        ("source", None),
        ("external_id", None),
    ],
)
def test_current_payload_must_bind_the_direct_confirmation_to_its_publication(
    db, native, field, replacement
):
    identity, _, _, recent = seed(db, native)
    observe(db, native, recent, retain_raw=False)
    assert source(db)["fields"]["price"]["fresh_confirmations"] == 1
    row = db.conn.execute(
        "SELECT id,details_json FROM latest_observations WHERE listing_id=?",
        (identity,),
    ).fetchone()
    payload = json.loads(row["details_json"])
    if replacement is None:
        payload.pop(field)
    else:
        payload[field] = replacement
    db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?",
        (json.dumps(payload), row["id"]),
    )
    assert source(db)["fields"]["price"]["fresh_confirmations"] == 0


def test_last_seen_and_cached_original_clock_do_not_renew_price(db, native):
    _, _, old, recent = seed(db, native)
    db.conn.execute("UPDATE listings SET last_seen=?", (recent,))
    observe(db, native, old, retain_raw=False)
    assert source(db)["fields"]["price"]["fresh_confirmations"] == 0


def test_missing_current_price_is_not_replaced_by_old_verification(db, native):
    _, _, _, recent = seed(db, native)
    observe(db, native, recent)
    payload = json.loads(
        db.conn.execute("SELECT details_json FROM latest_observations").fetchone()[0]
    )
    payload["price"] = None
    db.conn.execute(
        "UPDATE observations SET price_eur=NULL,details_json=?", (json.dumps(payload),)
    )
    diagnostic = source(db)
    assert diagnostic["fields"]["price"]["missing"] == 1
    assert diagnostic["fields"]["price"]["fresh_confirmations"] == 0


def test_legacy_reader_without_verification_table_falls_back_safely(db, native):
    _, _, _, recent = seed(db, native)
    observe(db, native, recent)
    db.conn.execute("DROP TABLE field_verifications")
    assert source(db)["fields"]["price"]["fresh_confirmations"] == 0
    payload = json.loads(
        db.conn.execute("SELECT details_json FROM latest_observations").fetchone()[0]
    )
    payload["raw"]["field_evidence"]["price"]["observed_at"] = recent
    db.conn.execute("UPDATE observations SET details_json=?", (json.dumps(payload),))
    assert source(db)["fields"]["price"]["fresh_confirmations"] == 1


def test_verifications_are_loaded_once_and_report_does_not_mutate(db, native):
    seed(db, native)
    observe(db, native, (datetime.now(UTC) - timedelta(minutes=5)).isoformat())
    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        source(db)
    finally:
        db.conn.set_trace_callback(None)
    assert sum("FROM field_verifications" in sql for sql in statements) == 1
