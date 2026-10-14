"""Reference identity merges preserve history and never publish stronger own facts."""

import json
import sqlite3

import pytest
import test_publisher_identity as publisher_cases
from car_market.identity_merge import bridge_shared_publications, merge_publications
from car_market.publisher_identity import shared_publication_pairs

paired = publisher_cases.paired


def roots(db, listings):
    return {
        source: db.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (lid,)).fetchone()[0]
        for source, lid in listings.items()
    }


def proof(db):
    return shared_publication_pairs(db)[0]


def test_provisional_publications_merge_without_changing_own_fields_or_history(paired):
    db, listings, _ = paired
    ids = roots(db, listings)
    original = [tuple(row) for row in db.conn.execute("SELECT * FROM observations ORDER BY id")]
    field_proofs = [
        tuple(row)
        for row in db.conn.execute("SELECT * FROM field_verifications ORDER BY listing_id,field")
    ]
    pair = proof(db)
    winner = merge_publications(db, pair["listing_a"], pair["listing_b"], pair["evidence"])
    assert winner == min(ids.values())
    assert set(roots(db, listings).values()) == {winner}
    assert all(db.resolve_vehicle_id(identity) == winner for identity in ids.values())
    assert [
        tuple(row) for row in db.conn.execute("SELECT * FROM observations ORDER BY id")
    ] == original
    assert [
        tuple(row)
        for row in db.conn.execute("SELECT * FROM field_verifications ORDER BY listing_id,field")
    ] == field_proofs
    assert db.conn.execute("SELECT kenteken,vin FROM vehicles").fetchone()[:] == (None, None)
    alias = db.conn.execute("SELECT * FROM vehicle_aliases").fetchone()
    assert alias["reason"] == "exact_publisher_reference"
    assert json.loads(alias["evidence_json"])["reference_evidence"] == pair["evidence"]
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "field,values",
    [("kenteken", ("AB123C", "CD456E")), ("vin", ("WBA12345678901234", "WBA12345678901235"))],
)
def test_conflicting_strong_identifiers_reject_entire_merge(paired, field, values):
    db, listings, _ = paired
    pair = proof(db)
    ids = roots(db, listings)
    for identity, value in zip(ids.values(), values):
        db.conn.execute(f"UPDATE vehicles SET {field}=? WHERE id=?", (value, identity))
    db.conn.commit()
    original = list(db.conn.iterdump())
    with pytest.raises(ValueError, match="Conflicting"):
        merge_publications(db, pair["listing_a"], pair["listing_b"], pair["evidence"])
    assert list(db.conn.iterdump()) == original


def test_reference_merge_moves_previous_alias_checks_candidates_and_quarantine(paired):
    db, listings, fetches = paired
    pair = proof(db)
    ids = roots(db, listings)
    winner, loser = ids["autoscout24"], ids["autotrader"]
    db.conn.execute("UPDATE vehicles SET kenteken=? WHERE id=?", ("AB123C", winner))
    db.conn.execute(
        "INSERT INTO vehicles VALUES (?,?,?,?)", ("third", None, None, "2026-10-07T10:00:00+00:00")
    )
    db.conn.execute(
        "INSERT INTO vehicle_aliases VALUES (?,?,?,?,?)",
        ("old", loser, "2026-10-07T10:00:00+00:00", "exact_registration", "{}"),
    )
    db.conn.execute(
        "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json,fetch_id) VALUES (?,?,?,?,?)",
        (
            loser,
            "2026-10-07T10:00:00+00:00",
            "rdw",
            '{"handelsbenaming":"218I ACTIVE TOURER"}',
            fetches["autotrader"],
        ),
    )
    a, b = sorted((loser, "third"))
    db.conn.execute(
        "INSERT INTO identity_candidates VALUES (?,?,?,?,?)",
        (a, b, "same specifications", "2026-10-07T10:00:00+00:00", "unconfirmed"),
    )
    db.conn.execute(
        "INSERT INTO identity_quarantine VALUES (?,?,?,?,?,?)",
        (loser, "kenteken", "invalid", "2026-10-07T10:00:00+00:00", "legacy", "{}"),
    )
    db.conn.commit()
    assert merge_publications(db, pair["listing_a"], pair["listing_b"], pair["evidence"]) == winner
    assert db.resolve_vehicle_id("old") == db.resolve_vehicle_id(loser) == winner
    assert db.conn.execute("SELECT vehicle_id,fetch_id FROM vehicle_checks").fetchone()[:] == (
        winner,
        fetches["autotrader"],
    )
    assert db.conn.execute("SELECT vehicle_id FROM identity_quarantine").fetchone()[0] == winner
    assert set(
        db.conn.execute("SELECT vehicle_a,vehicle_b FROM identity_candidates").fetchone()
    ) == {winner, "third"}
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("rdw", [True, False])
def test_existing_model_conflict_never_returns_to_target_through_publication_bridge(paired, rdw):
    db, listings, _ = paired
    pair = proof(db)
    ids = roots(db, listings)
    db.conn.execute("UPDATE vehicles SET kenteken=? WHERE id=?", ("JZ820Z", ids["autoscout24"]))
    if rdw:
        db.conn.execute(
            "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json) VALUES (?,?,?,?)",
            (
                ids["autoscout24"],
                "2026-10-07T10:00:00+00:00",
                "rdw",
                '{"handelsbenaming":"218I GRAN TOURER"}',
            ),
        )
    else:
        db.conn.execute(
            "UPDATE listings SET scope='model_conflict' WHERE id=?", (listings["autoscout24"],)
        )
    db.conn.commit()
    merge_publications(db, pair["listing_a"], pair["listing_b"], pair["evidence"])
    assert {row["scope"] for row in db.conn.execute("SELECT scope FROM listings")} == {
        "model_conflict"
    }
    assert db.conn.execute("SELECT COUNT(*) FROM purchase_candidates").fetchone()[0] == 0


def test_reference_batch_repeat_has_no_new_aliases_or_facts(paired):
    db, _, _ = paired
    assert bridge_shared_publications(db)["merged"] == 1
    original = list(db.conn.iterdump())
    assert bridge_shared_publications(db) == {"verified_pairs": 0, "merged": 0, "conflicts": []}
    assert list(db.conn.iterdump()) == original


def test_outer_transaction_can_roll_back_successful_reference_merge(paired):
    db, _, _ = paired
    pair = proof(db)
    original = list(db.conn.iterdump())
    with pytest.raises(RuntimeError, match="caller"), db.transaction():
        merge_publications(db, pair["listing_a"], pair["listing_b"], pair["evidence"])
        assert db.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 1
        raise RuntimeError("caller failure")
    assert list(db.conn.iterdump()) == original


def test_sql_failure_inside_caught_merge_does_not_leak_partial_updates(paired):
    db, _, _ = paired
    pair = proof(db)
    db.conn.execute(
        "CREATE TRIGGER fail_alias BEFORE INSERT ON vehicle_aliases BEGIN SELECT RAISE(ABORT,'alias unavailable'); END"
    )
    db.conn.commit()
    original = list(db.conn.iterdump())
    with db.transaction():
        with pytest.raises(sqlite3.IntegrityError, match="alias unavailable"):
            merge_publications(db, pair["listing_a"], pair["listing_b"], pair["evidence"])
        assert list(db.conn.iterdump()) == original
        assert db.conn.in_transaction
    assert list(db.conn.iterdump()) == original


@pytest.mark.parametrize("evidence", [None, [], {}, [{}, {}]])
def test_id_alone_never_authorizes_merge(paired, evidence):
    db, listings, _ = paired
    original = list(db.conn.iterdump())
    with pytest.raises(ValueError):
        merge_publications(db, listings["autoscout24"], listings["autotrader"], evidence)
    assert list(db.conn.iterdump()) == original


def test_model_conflict_of_third_member_protects_entire_merged_root(paired):
    from car_market.db import now
    from car_market.parse import Listing

    db, listings, _ = paired
    pair = proof(db)
    ids = roots(db, listings)
    db.conn.execute("UPDATE vehicles SET kenteken=? WHERE id=?", ("AB123C", ids["autoscout24"]))
    db.conn.commit()
    third = Listing(
        "nederlandmobiel",
        "third",
        "https://example.test/third",
        "BMW 218i Active Tourer",
        fuel="petrol",
        kenteken="AB123C",
        price=8000,
        transmission="manual",
    ).qualify()
    lid = db.observe(db.start_run(), third, now())
    db.conn.execute("UPDATE listings SET scope='model_conflict' WHERE id=?", (lid,))
    db.conn.commit()
    merge_publications(db, pair["listing_a"], pair["listing_b"], pair["evidence"])
    assert {r["scope"] for r in db.conn.execute("SELECT scope FROM listings")} == {"model_conflict"}
    assert db.conn.execute("SELECT COUNT(*) FROM purchase_candidates").fetchone()[0] == 0
