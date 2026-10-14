"""Merge publications only after independently verified publisher references."""

import json
from contextlib import contextmanager

from .db import now
from .identity import normalized_vin
from .provenance import timestamp


@contextmanager
def _merge_transaction(db):
    if not db._in_transaction:
        with db.transaction():
            yield
        return
    db.conn.execute("SAVEPOINT publisher_bridge")
    try:
        yield
    except BaseException:
        db.conn.execute("ROLLBACK TO SAVEPOINT publisher_bridge")
        db.conn.execute("RELEASE SAVEPOINT publisher_bridge")
        raise
    else:
        db.conn.execute("RELEASE SAVEPOINT publisher_bridge")


def merge_publications(db, listing_a, listing_b, evidence):
    """Internal writer operation; caller provides a verified pair of own payloads."""
    with _merge_transaction(db):
        from .publisher_identity import validate_shared_pair

        validate_shared_pair(db, listing_a, listing_b, evidence)
        rows = db.conn.execute(
            "SELECT l.id AS listing_id,l.source,l.external_id,l.scope AS publication_scope,v.* FROM listings l "
            "JOIN vehicles v ON v.id=l.vehicle_id WHERE l.id IN (?,?)",
            (listing_a, listing_b),
        ).fetchall()
        if len(rows) != 2 or {row["source"] for row in rows} != {"autoscout24", "autotrader"}:
            raise ValueError("Publisher bridge needs both exact source publications")
        if len({row["external_id"] for row in rows}) != 1:
            raise ValueError("Publisher bridge has different publication UUIDs")
        if rows[0]["id"] == rows[1]["id"]:
            return rows[0]["id"]
        values = {}
        for field in ("kenteken", "vin"):
            distinct = {row[field] for row in rows if row[field]}
            if len(distinct) > 1:
                raise ValueError("Conflicting publisher registration/VIN identities")
            values[field] = next(iter(distinct), None)
        if values["vin"] and normalized_vin(values["vin"]) != values["vin"]:
            raise ValueError("Invalid publisher VIN identity")
        established = [row for row in rows if row["kenteken"] or row["vin"]]
        winner = min(
            established or rows,
            key=lambda row: (
                timestamp(row["created_at"]) is None,
                timestamp(row["created_at"]),
                row["id"],
            ),
        )
        loser = next(row for row in rows if row["id"] != winner["id"])
        winner_id, loser_id = winner["id"], loser["id"]
        existing_model_conflict = (
            db.conn.execute(
                "SELECT 1 FROM listings WHERE vehicle_id IN (?,?) AND scope='model_conflict' LIMIT 1",
                (winner_id, loser_id),
            ).fetchone()
            is not None
        )
        check = db.conn.execute(
            "SELECT data_json FROM vehicle_checks WHERE vehicle_id IN (?,?) AND provider='rdw' "
            "ORDER BY julianday(checked_at) DESC,id DESC LIMIT 1",
            (winner_id, loser_id),
        ).fetchone()
        rdw = json.loads(check[0]) if check else {}
        if not isinstance(rdw, dict):
            raise TypeError("Invalid publisher RDW evidence")
        name = str(rdw.get("handelsbenaming") or "").upper()
        members = [
            dict(row)
            for row in db.conn.execute(
                "SELECT id,source,external_id FROM listings WHERE vehicle_id=?", (loser_id,)
            )
        ]
        candidates = [
            dict(row)
            for row in db.conn.execute(
                "SELECT * FROM identity_candidates WHERE vehicle_a=? OR vehicle_b=?",
                (loser_id, loser_id),
            )
        ]
        proof = {
            "kind": "exact_shared_publisher_page",
            "retired_vehicle": {key: loser[key] for key in ("id", "kenteken", "vin", "created_at")},
            "winner_at_merge": winner_id,
            "members": members,
            "previous_candidates": candidates,
            "reference_evidence": evidence,
        }
        for table in ("listings", "vehicle_checks", "vehicle_aliases"):
            db.conn.execute(
                f"UPDATE {table} SET vehicle_id=? WHERE vehicle_id=?", (winner_id, loser_id)
            )
        db.conn.execute(
            "INSERT OR IGNORE INTO identity_quarantine SELECT ?,field,value,quarantined_at,reason,evidence_json FROM identity_quarantine WHERE vehicle_id=?",
            (winner_id, loser_id),
        )
        db.conn.execute("DELETE FROM identity_quarantine WHERE vehicle_id=?", (loser_id,))
        db.conn.execute(
            "INSERT INTO vehicle_aliases VALUES (?,?,?,?,?)",
            (
                loser_id,
                winner_id,
                now(),
                "exact_publisher_reference",
                json.dumps(proof, ensure_ascii=False),
            ),
        )
        for candidate in candidates:
            other = (
                candidate["vehicle_b"]
                if candidate["vehicle_a"] == loser_id
                else candidate["vehicle_a"]
            )
            if other == winner_id:
                continue
            a, b = sorted((winner_id, other))
            db.conn.execute(
                "INSERT OR IGNORE INTO identity_candidates VALUES (?,?,?,?,?)",
                (a, b, candidate["reason"], candidate["created_at"], candidate["status"]),
            )
        db.conn.execute(
            "DELETE FROM identity_candidates WHERE vehicle_a=? OR vehicle_b=?", (loser_id, loser_id)
        )
        db.conn.execute("DELETE FROM vehicles WHERE id=?", (loser_id,))
        db.conn.execute(
            "UPDATE vehicles SET kenteken=?,vin=? WHERE id=?",
            (values["kenteken"], values["vin"], winner_id),
        )
        if (
            "GRAN TOURER" in name
            or "GRAN COUPE" in name
            or "218D" in name
            or existing_model_conflict
        ):
            db.conn.execute(
                "UPDATE listings SET scope='model_conflict' WHERE vehicle_id=?", (winner_id,)
            )
        return winner_id


def bridge_shared_publications(db):
    from .publisher_identity import shared_publication_pairs

    pairs = shared_publication_pairs(db)
    result = {"verified_pairs": len(pairs), "merged": 0, "conflicts": []}
    for pair in pairs:
        try:
            merge_publications(db, pair["listing_a"], pair["listing_b"], pair["evidence"])
            result["merged"] += 1
        except (ValueError, TypeError) as error:
            result["conflicts"].append(
                {
                    "listing_a": pair["listing_a"],
                    "listing_b": pair["listing_b"],
                    "error": str(error),
                    "evidence": pair["evidence"],
                }
            )
    return result
