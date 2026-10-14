"""Transfer explicit snapshot registry links without inventing observation fields."""

import json
from datetime import datetime

from .db import now
from .identity import normalized_vin, unsupported_legacy_registrations
from .parse import plate


def _identifiers(row, strict=True):
    registration = plate(row["kenteken"]) if row["kenteken"] else None
    chassis = normalized_vin(row["vin"])
    if strict and row["kenteken"] and not registration:
        raise ValueError("Invalid snapshot registry registration")
    if strict and row["vin"] and not chassis:
        raise ValueError("Invalid snapshot registry VIN")
    return registration or row["kenteken"] or None, chassis or row["vin"] or None


class SnapshotIdentity:
    def __init__(self, db, source, snapshot):
        if not db.conn.in_transaction:
            raise RuntimeError("Snapshot identity import requires a transaction")
        self.db = db
        self.snapshot = str(snapshot)
        self.vehicles = {r["id"]: dict(r) for r in source.execute("SELECT * FROM vehicles")}
        self.listings = {r["id"]: dict(r) for r in source.execute("SELECT * FROM listings")}
        self._legacy_quarantine = []
        for proof in unsupported_legacy_registrations(source):
            original = proof["original_vehicle"]
            self.vehicles[original["id"]]["kenteken"] = None
            self._legacy_quarantine.append(
                {
                    "vehicle_id": original["id"],
                    "field": "kenteken",
                    "value": original["kenteken"],
                    "quarantined_at": now(),
                    "reason": "audited_legacy_trovit_suffix_without_own_evidence",
                    "evidence_json": json.dumps(proof, ensure_ascii=False),
                }
            )
        has_aliases = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vehicle_aliases'"
        ).fetchone()
        self.aliases = (
            [dict(r) for r in source.execute("SELECT * FROM vehicle_aliases")]
            if has_aliases
            else []
        )
        self.mapping = {}
        self._destination_listing_ids = None
        self._prepare()

    def _prepare(self):
        conn = self.db.conn
        destination = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM vehicles")}
        destination_aliases = {
            r["alias_id"]: r["vehicle_id"] for r in conn.execute("SELECT * FROM vehicle_aliases")
        }
        if any(identity in destination for identity in destination_aliases):
            raise ValueError("Conflicting destination UUID/alias registry")
        parent = {}

        def root(node):
            parent.setdefault(node, node)
            if parent[node] != node:
                parent[node] = root(parent[node])
            return parent[node]

        def link(a, b):
            parent[root(a)] = root(b)

        ids = {}
        identifiers = {}
        for kind, records in [("source", self.vehicles), ("destination", destination)]:
            for identity, record in records.items():
                if not identity:
                    raise ValueError("Invalid snapshot registry UUID")
                node = kind, identity
                root(node)
                values = _identifiers(record, strict=kind == "source")
                identifiers[node] = values
                for field, value in zip(("kenteken", "vin"), values):
                    if value:
                        key = field, value
                        if key in ids:
                            link(node, ids[key])
                        ids[key] = node
        names = {identity: identity for identity in self.vehicles}
        for alias in self.aliases:
            if alias["vehicle_id"] not in self.vehicles or alias["alias_id"] in names:
                raise ValueError("Conflicting snapshot UUID/alias registry")
            # Validate JSON before transferring any links.
            json.loads(alias["evidence_json"])
            names[alias["alias_id"]] = alias["vehicle_id"]
        for identity, source_root in names.items():
            target = identity if identity in destination else destination_aliases.get(identity)
            if target:
                if target not in destination:
                    raise ValueError("Dangling destination UUID alias")
                link(("source", source_root), ("destination", target))
        ads = {
            (r["source"], r["external_id"]): r["vehicle_id"]
            for r in conn.execute("SELECT source,external_id,vehicle_id FROM listings")
        }
        for listing in self.listings.values():
            if listing["vehicle_id"] not in self.vehicles:
                raise ValueError("Snapshot listing has no explicit registry identity")
            target = ads.get((listing["source"], listing["external_id"]))
            if target:
                if target not in destination:
                    raise ValueError("Destination listing has no registry identity")
                link(("source", listing["vehicle_id"]), ("destination", target))
        groups = {}
        for node in list(parent):
            groups.setdefault(root(node), []).append(node)
        plans = []
        for nodes in groups.values():
            source_ids = [identity for kind, identity in nodes if kind == "source"]
            if not source_ids:
                continue
            dest_ids = [identity for kind, identity in nodes if kind == "destination"]
            for identity in dest_ids:
                _identifiers(destination[identity])
            values = [{v for node in nodes if (v := identifiers[node][i])} for i in range(2)]
            if any(len(v) > 1 for v in values):
                raise ValueError(
                    "Conflicting snapshot registration/VIN/UUID identities; manual review required"
                )
            registration, chassis = (next(iter(v), None) for v in values)
            established = [i for i in dest_ids if any(identifiers["destination", i])]
            choices = established or dest_ids or source_ids
            records = destination if dest_ids else self.vehicles
            winner = min(choices, key=lambda i: (records[i]["created_at"], i))
            members = [r for r in self.listings.values() if r["vehicle_id"] in source_ids]
            proof = {
                "kind": "snapshot_registry",
                "snapshot": self.snapshot,
                "source_vehicles": [self.vehicles[i] for i in source_ids],
                "source_listings": members,
                "source_aliases": [a for a in self.aliases if a["vehicle_id"] in source_ids],
            }
            plans.append((winner, source_ids, dest_ids, registration, chassis, proof))
        # Validate all components before the first mutation; the caller also holds a transaction.
        for winner, source_ids, dest_ids, registration, chassis, proof in plans:
            if winner not in destination:
                conn.execute(
                    "INSERT INTO vehicles(id,created_at) VALUES (?,?)",
                    (winner, self.vehicles[winner]["created_at"]),
                )
            for loser in dest_ids:
                if loser != winner:
                    self._merge(loser, winner, proof)
            conn.execute(
                "UPDATE vehicles SET kenteken=?,vin=? WHERE id=?", (registration, chassis, winner)
            )
            for source_id in source_ids:
                self.mapping[source_id] = winner
                self._alias(
                    source_id,
                    winner,
                    now(),
                    "snapshot_registry",
                    json.dumps(proof, ensure_ascii=False),
                )
        for alias in self.aliases:
            evidence = json.dumps(
                {"snapshot_origin": {"snapshot": self.snapshot, "source_alias": alias}},
                ensure_ascii=False,
            )
            self._alias(
                alias["alias_id"],
                self.mapping[alias["vehicle_id"]],
                alias["merged_at"],
                alias["reason"],
                evidence,
                source_alias=alias,
            )

    def _alias(self, identity, winner, timestamp, reason, evidence, source_alias=None):
        if identity == winner:
            return
        resolved = self.db.resolve_vehicle_id(identity)
        if resolved:
            if resolved != winner:
                raise ValueError("Conflicting existing snapshot UUID alias")
            if source_alias:
                stored = self.db.conn.execute(
                    "SELECT * FROM vehicle_aliases WHERE alias_id=?", (identity,)
                ).fetchone()
                proof = json.loads(stored["evidence_json"])
                if (stored["reason"], stored["merged_at"], stored["evidence_json"]) == (
                    reason,
                    timestamp,
                    evidence,
                ):
                    return
                proof = proof if isinstance(proof, dict) else {"destination_evidence": proof}
                if proof.get("snapshot_origin", {}).get("source_alias") == source_alias:
                    return
                if source_alias in proof.get("source_aliases", []):
                    return
                imported = proof.setdefault("snapshot_alias_imports", [])
                audit = {
                    "snapshot_origin": {"snapshot": self.snapshot, "source_alias": source_alias}
                }
                if audit not in imported:
                    imported.append(audit)
                    self.db.conn.execute(
                        "UPDATE vehicle_aliases SET evidence_json=? WHERE alias_id=?",
                        (json.dumps(proof, ensure_ascii=False), identity),
                    )
            return
        self.db.conn.execute(
            "INSERT INTO vehicle_aliases VALUES (?,?,?,?,?)",
            (identity, winner, timestamp, reason, evidence),
        )

    def _merge(self, loser, winner, proof):
        conn = self.db.conn
        candidates = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM identity_candidates WHERE vehicle_a=? OR vehicle_b=?", (loser, loser)
            )
        ]
        retired = dict(conn.execute("SELECT * FROM vehicles WHERE id=?", (loser,)).fetchone())
        for table in ("listings", "vehicle_checks", "vehicle_aliases"):
            conn.execute(f"UPDATE {table} SET vehicle_id=? WHERE vehicle_id=?", (winner, loser))
        quarantined = [
            dict(r)
            for r in conn.execute("SELECT * FROM identity_quarantine WHERE vehicle_id=?", (loser,))
        ]
        for row in quarantined:
            conn.execute(
                "INSERT OR IGNORE INTO identity_quarantine VALUES (?,?,?,?,?,?)",
                (
                    winner,
                    row["field"],
                    row["value"],
                    row["quarantined_at"],
                    row["reason"],
                    row["evidence_json"],
                ),
            )
        conn.execute("DELETE FROM identity_quarantine WHERE vehicle_id=?", (loser,))
        for candidate in candidates:
            other = (
                candidate["vehicle_b"]
                if candidate["vehicle_a"] == loser
                else candidate["vehicle_a"]
            )
            if other == winner:
                continue
            a, b = sorted((winner, other))
            conn.execute(
                "INSERT OR IGNORE INTO identity_candidates VALUES (?,?,?,?,?)",
                (a, b, candidate["reason"], candidate["created_at"], candidate["status"]),
            )
        conn.execute(
            "DELETE FROM identity_candidates WHERE vehicle_a=? OR vehicle_b=?", (loser, loser)
        )
        conn.execute("DELETE FROM vehicles WHERE id=?", (loser,))
        evidence = {
            **proof,
            "retired_vehicle": retired,
            "previous_candidates": candidates,
            "previous_quarantine": quarantined,
        }
        self._alias(
            loser, winner, now(), "snapshot_registry", json.dumps(evidence, ensure_ascii=False)
        )

    def listing(self, observation, payload):
        listing = self.listings.get(observation["listing_id"])
        if listing is None:
            raise ValueError("Snapshot observation has no registry listing")
        if (payload["source"], payload["external_id"]) != (
            listing["source"],
            listing["external_id"],
        ):
            raise ValueError("Snapshot observation publication disagrees with registry")
        return listing, self.db.resolve_vehicle_id(self.mapping[listing["vehicle_id"]])

    def checks(self, source, fetches):
        for row in source.execute("SELECT * FROM vehicle_checks ORDER BY id"):
            if row["vehicle_id"] not in self.mapping:
                raise ValueError("Snapshot check has no registry identity")
            winner = self.db.resolve_vehicle_id(self.mapping[row["vehicle_id"]])
            data = json.loads(row["data_json"])
            existing = self.db.conn.execute(
                "SELECT data_json FROM vehicle_checks WHERE vehicle_id=? AND checked_at=? AND provider=?",
                (winner, row["checked_at"], row["provider"]),
            )
            if any(json.loads(r["data_json"]) == data for r in existing):
                continue
            if row["fetch_id"] is not None and row["fetch_id"] not in fetches:
                raise ValueError("Snapshot vehicle check references a missing fetch")
            self.db.conn.execute(
                "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json,fetch_id) VALUES (?,?,?,?,?)",
                (
                    winner,
                    row["checked_at"],
                    row["provider"],
                    row["data_json"],
                    fetches.get(row["fetch_id"]),
                ),
            )

    def verifications(self, source, fetches):
        if not source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='field_verifications'"
        ).fetchone():
            return
        for row in source.execute("SELECT * FROM field_verifications"):
            listing = self.listings.get(row["listing_id"])
            if listing is None:
                raise ValueError("Snapshot field verification has no registry listing")
            target = self.db.conn.execute(
                "SELECT id FROM listings WHERE source=? AND external_id=?",
                (listing["source"], listing["external_id"]),
            ).fetchone()
            if target is None:
                raise ValueError("Snapshot field verification has no imported listing")
            incoming_time = datetime.fromisoformat(row["verified_at"])
            if incoming_time.tzinfo is None:
                raise ValueError("Snapshot field verification has an ambiguous timestamp")
            json.loads(row["evidence_json"])
            existing = self.db.conn.execute(
                "SELECT verified_at FROM field_verifications WHERE listing_id=? AND field=?",
                (target["id"], row["field"]),
            ).fetchone()
            if existing and datetime.fromisoformat(existing["verified_at"]) >= incoming_time:
                continue
            if row["fetch_id"] is not None and row["fetch_id"] not in fetches:
                raise ValueError("Snapshot field verification references a missing fetch")
            self.db.conn.execute(
                "INSERT INTO field_verifications(listing_id,field,verified_at,fetch_id,value_hash,evidence_json) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(listing_id,field) DO UPDATE SET "
                "verified_at=excluded.verified_at,fetch_id=excluded.fetch_id,"
                "value_hash=excluded.value_hash,evidence_json=excluded.evidence_json",
                (
                    target["id"],
                    row["field"],
                    row["verified_at"],
                    fetches.get(row["fetch_id"]),
                    row["value_hash"],
                    self._evidence(row, fetches),
                ),
            )

    def _evidence(self, row, fetches):
        if self._destination_listing_ids is None:
            destination = {
                (r["source"], r["external_id"]): r["id"]
                for r in self.db.conn.execute("SELECT id,source,external_id FROM listings")
            }
            self._destination_listing_ids = {
                source_id: destination[(listing["source"], listing["external_id"])]
                for source_id, listing in self.listings.items()
                if (listing["source"], listing["external_id"]) in destination
            }
        listing_ids = self._destination_listing_ids

        def remap(value):
            if isinstance(value, list):
                return [remap(item) for item in value]
            if not isinstance(value, dict):
                return value
            result = {}
            for key, item in value.items():
                if key == "snapshot_origin":
                    result[key] = item
                elif key == "fetch_id" and item is not None:
                    result[key] = fetches.get(item)
                elif (
                    key in ("listing_id", "trigger_listing_id", "detail_listing_id")
                    and item is not None
                ):
                    result[key] = listing_ids.get(item)
                elif key == "members" and isinstance(item, list):
                    result[key] = [
                        {**remap(member), "id": listing_ids.get(member["id"])}
                        if isinstance(member, dict)
                        and "source" in member
                        and "external_id" in member
                        and "id" in member
                        else remap(member)
                        for member in item
                    ]
                else:
                    result[key] = remap(item)
            return result

        evidence = remap(json.loads(row["evidence_json"]))
        if not isinstance(evidence, dict):
            evidence = {"evidence": evidence}
        evidence["snapshot_origin"] = {
            "snapshot": self.snapshot,
            "evidence_json": row["evidence_json"],
        }
        return json.dumps(evidence, ensure_ascii=False, sort_keys=True)

    def quarantine(self, source, fetches):
        has_table = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='identity_quarantine'"
        ).fetchone()
        rows = list(source.execute("SELECT * FROM identity_quarantine")) if has_table else []
        for row in [*rows, *self._legacy_quarantine]:
            if row["vehicle_id"] not in self.mapping:
                raise ValueError("Snapshot quarantine has no registry identity")
            winner = self.db.resolve_vehicle_id(self.mapping[row["vehicle_id"]])
            evidence = self._evidence(row, fetches)
            existing = self.db.conn.execute(
                "SELECT * FROM identity_quarantine WHERE vehicle_id=? AND field=? AND value=?",
                (winner, row["field"], row["value"]),
            ).fetchone()
            if existing:
                proof = json.loads(existing["evidence_json"])
                original = json.loads(evidence)["snapshot_origin"]
                if isinstance(proof, dict) and proof.get("snapshot_origin") == original:
                    continue
                proof = proof if isinstance(proof, dict) else {"destination_evidence": proof}
                imported = proof.setdefault("snapshot_imports", [])
                if original not in imported:
                    imported.append(original)
                    self.db.conn.execute(
                        "UPDATE identity_quarantine SET evidence_json=? WHERE vehicle_id=? AND field=? AND value=?",
                        (json.dumps(proof, ensure_ascii=False), winner, row["field"], row["value"]),
                    )
            else:
                self.db.conn.execute(
                    "INSERT INTO identity_quarantine VALUES (?,?,?,?,?,?)",
                    (
                        winner,
                        row["field"],
                        row["value"],
                        row["quarantined_at"],
                        row["reason"],
                        evidence,
                    ),
                )
