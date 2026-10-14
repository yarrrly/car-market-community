"""Unknown native country cannot be promoted by retained title/fuel enrichment."""

import gzip
import hashlib
from datetime import datetime, timedelta

import pytest
from test_nl_publication_proof import AS, ID, body, derived, history, ingest, snapshot

from car_market.collect import Collector
from car_market.db import Database
from car_market.opportunities import inventory


@pytest.fixture
def evaluation_config():
    # Captures keep their original clocks. Eligibility assertions evaluate one
    # minute after the last controlled replay, independent of the wall clock.
    return {
        "scope": {"country": "NL"},
        "detail_limit": 0,
        "as_of": (datetime.fromisoformat(AS["fetched_at"]) + timedelta(minutes=4)).isoformat(),
    }


def confirmations(db, listing_id):
    return [
        tuple(r)
        for r in db.conn.execute(
            "SELECT * FROM field_verifications WHERE listing_id=? ORDER BY field",
            (listing_id,),
        )
    ]


@pytest.mark.parametrize("negative_before", [False, True])
@pytest.mark.parametrize("missing", [None, "", "absent"])
def test_current_unknown_country_stays_pending_after_enrichment(
    tmp_path, negative_before, missing, evaluation_config
):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, evaluation_config)
    ingest(worker, AS, body(AS))
    if negative_before:
        foreign = derived(AS, lambda own, _doc: own["location"].__setitem__("countryCode", "BE"))
        ingest(worker, AS, foreign, 1)
        assert snapshot(db, "autoscout24", ID)[0]["scope"] == "excluded"
    old = snapshot(db, "autoscout24", ID)
    rows = history(db)
    verified = confirmations(db, old[0]["id"])

    def remove(own, _doc):
        if missing == "absent":
            own["location"].pop("countryCode")
        else:
            own["location"]["countryCode"] = missing

    changed = derived(AS, remove)
    _run, stamp = ingest(worker, AS, changed, 2)
    current = snapshot(db, "autoscout24", ID)
    assert current[0]["scope"] == current[1]["scope"] == current[2]["scope"] == "needs_confirmation"
    assert current[0]["vehicle_id"] == old[0]["vehicle_id"]
    assert current[1]["observed_at"] == stamp and current[1]["id"] != old[1]["id"]
    assert confirmations(db, old[0]["id"]) == verified
    assert not any(
        car["vehicle_id"] == old[0]["vehicle_id"] for car in inventory(db.conn, worker.config)
    )
    location = current[2]["raw"]["location"]
    if missing == "absent":
        assert "countryCode" not in location
    else:
        assert location["countryCode"] == missing
    assert current[2]["raw"]["nl_publication_conflicts"]["country"]["value"] in (
        None,
        "",
    )
    capture = db.conn.execute(
        "SELECT * FROM fetches WHERE id=?", (current[1]["fetch_id"],)
    ).fetchone()
    assert capture["fetched_at"] == stamp and capture["status"] == 200 and capture["error"] is None
    raw = gzip.decompress((db.path.parent / capture["raw_path"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(changed.encode()).hexdigest()
    for table, before in rows.items():
        for row in before:
            assert (
                tuple(
                    db.conn.execute("SELECT * FROM " + table + " WHERE id=?", (row[0],)).fetchone()
                )
                == row
            )
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()
    # An explicit owned NL capture, rather than inherited text, restores eligibility.
    _run, restored_at = ingest(worker, AS, body(AS), 3)
    restored = snapshot(db, "autoscout24", ID)
    assert restored[0]["scope"] == restored[2]["scope"] == "target"
    assert any(
        car["vehicle_id"] == old[0]["vehicle_id"] for car in inventory(db.conn, worker.config)
    )
    assert any(r[2] == restored_at for r in confirmations(db, old[0]["id"]))
    db.conn.close()


@pytest.mark.parametrize("model", [None, "", "unmapped-native-model"])
def test_owned_motor_text_remains_positive_for_unmapped_structured_model(
    tmp_path, model, evaluation_config
):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, evaluation_config)
    ingest(worker, AS, body(AS))
    changed = derived(AS, lambda own, _doc: own["vehicle"].__setitem__("model", model))
    _run, stamp = ingest(worker, AS, changed, 1)
    current = snapshot(db, "autoscout24", ID)
    assert current[0]["scope"] == current[2]["scope"] == "target"
    assert current[2]["raw"]["vehicle"]["model"] == model
    assert current[1]["observed_at"] == stamp
    assert any(
        car["vehicle_id"] == current[0]["vehicle_id"] for car in inventory(db.conn, worker.config)
    )
    db.conn.close()


@pytest.mark.parametrize(
    "offset,eligible",
    [
        (timedelta(hours=13) - timedelta(microseconds=1), True),
        (timedelta(hours=13), True),
        (timedelta(hours=13, microseconds=1), False),
        (-timedelta(microseconds=1), False),
    ],
    ids=["before_default_expiry", "exact_default_expiry", "after_default_expiry", "future_capture"],
)
def test_original_native_capture_keeps_strict_default_freshness_boundary(
    tmp_path, evaluation_config, offset, eligible
):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, evaluation_config)
    _run, captured_at = ingest(worker, AS, body(AS))
    assert captured_at == AS["fetched_at"]
    current = snapshot(db, "autoscout24", ID)
    assert current[1]["observed_at"] == captured_at
    capture = db.conn.execute(
        "SELECT fetched_at FROM fetches WHERE id=?", (current[1]["fetch_id"],)
    ).fetchone()
    assert capture["fetched_at"] == captured_at
    as_of = (datetime.fromisoformat(captured_at) + offset).isoformat()
    assert "freshness_hours" not in evaluation_config
    offered = inventory(db.conn, {**evaluation_config, "as_of": as_of})
    assert any(car["vehicle_id"] == current[0]["vehicle_id"] for car in offered) is eligible
    assert snapshot(db, "autoscout24", ID)[1]["observed_at"] == captured_at
    db.conn.close()
