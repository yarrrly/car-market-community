"""Offline relational reproduction of registry import, UUIDs, aliases and validation."""

import json
import sys

import pytest
from car_market.cli import main
from car_market.db import Database
from car_market.parse import Listing

STAMP = "2026-10-01T10:00:00+00:00"
VIN = "WBA12345678901234"
OTHER_VIN = "WBA12345678901235"


def ad(source, external, registration=None, vin=None):
    return Listing(
        source,
        external,
        "https://example.test/" + external,
        "BMW 218i Active Tourer",
        fuel="petrol",
        kenteken=registration,
        vin=vin,
    ).qualify()


def vehicle(db, listing):
    return db.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (listing,)).fetchone()[0]


def state(db):
    return {
        table: [tuple(r) for r in db.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in (
            "runs",
            "fetches",
            "vehicles",
            "vehicle_aliases",
            "listings",
            "observations",
            "events",
            "vehicle_checks",
            "identity_candidates",
        )
    }


def import_cli(monkeypatch, source, destination, tmp_path):
    source.conn.commit()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "car-market",
            "--db",
            str(destination.path),
            "--export-dir",
            str(tmp_path / "exports"),
            "import-db",
            str(source.path),
            "--historical",
        ],
    )
    main()


@pytest.mark.parametrize("registration,vin", [("GB178T", None), (None, VIN), (None, None)])
def test_registry_grouping_preserves_uuids_alias_evidence_checks_and_repeat(
    tmp_path, monkeypatch, registration, vin
):
    source = Database(tmp_path / "source/market.sqlite3")
    a = source.observe(source.start_run(), ad("gaspedaal", "a"), STAMP)
    b = source.observe(source.start_run(), ad("dealer:example.test", "b"), STAMP)
    canonical, former = vehicle(source, a), vehicle(source, b)
    if registration or vin:
        source.attach_identity(a, registration, vin)
        source.attach_identity(
            b, registration, vin, evidence={"kind": "portal_reference", "fetch_id": 19}
        )
        canonical = vehicle(source, a)
        source_alias = dict(
            source.conn.execute(
                "SELECT * FROM vehicle_aliases WHERE alias_id=?", (former,)
            ).fetchone()
        )
    else:
        source.conn.execute("UPDATE listings SET vehicle_id=? WHERE id=?", (canonical, b))
        source.conn.execute("DELETE FROM vehicles WHERE id=?", (former,))
        proof = json.dumps({"kind": "explicit_registry_component", "source_uuid": former})
        source.conn.execute(
            "INSERT INTO vehicle_aliases VALUES (?,?,?,?,?)",
            (former, canonical, STAMP, "explicit_registry_component", proof),
        )
        source_alias = dict(
            source.conn.execute(
                "SELECT * FROM vehicle_aliases WHERE alias_id=?", (former,)
            ).fetchone()
        )
    source.conn.commit()
    fetch = source.save_fetch(
        source.start_run(),
        "rdw",
        "https://rdw.test/car",
        "https://rdw.test/car",
        200,
        "{}",
        STAMP,
        retain_raw=False,
    )
    source.conn.execute(
        "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json,fetch_id) VALUES (?,?,?,?,?)",
        (canonical, STAMP, "rdw", json.dumps({"kenteken": registration, "vin": vin}), fetch),
    )
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    # An existing fetch with the same integer ID but different contents.
    destination.save_fetch(
        destination.start_run(),
        "other",
        "https://other.test/",
        "https://other.test/",
        200,
        "unrelated",
        STAMP,
        retain_raw=False,
    )
    before_source = state(source)
    import_cli(monkeypatch, source, destination, tmp_path)
    assert source.conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 1
    assert state(source) == before_source
    assert destination.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 1
    assert {r[0] for r in destination.conn.execute("SELECT vehicle_id FROM listings")} == {
        canonical
    }
    assert destination.resolve_vehicle_id(former) == canonical
    imported_alias = dict(
        destination.conn.execute(
            "SELECT * FROM vehicle_aliases WHERE alias_id=?", (former,)
        ).fetchone()
    )
    assert {k: v for k, v in imported_alias.items() if k != "evidence_json"} == {
        k: v for k, v in source_alias.items() if k != "evidence_json"
    }
    assert (
        json.loads(imported_alias["evidence_json"])["snapshot_origin"]["source_alias"]
        == source_alias
    )
    check = destination.conn.execute("SELECT * FROM vehicle_checks").fetchone()
    assert check["vehicle_id"] == canonical and check["checked_at"] == STAMP
    assert check["fetch_id"] != fetch
    assert (
        destination.conn.execute(
            "SELECT source FROM fetches WHERE id=?", (check["fetch_id"],)
        ).fetchone()[0]
        == "rdw"
    )
    assert all(
        r[0] is None and r[1] is None
        for r in destination.conn.execute("SELECT kenteken,vin FROM observations")
    )
    preserved = {
        t: state(destination)[t]
        for t in ("vehicles", "vehicle_aliases", "listings", "observations", "vehicle_checks")
    }
    import_cli(monkeypatch, source, destination, tmp_path)
    assert {t: state(destination)[t] for t in preserved} == preserved
    assert destination.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    source.conn.close()
    destination.conn.close()


@pytest.mark.parametrize("schema", [2, 3])
def test_identifierless_singleton_publication_anchors_source_uuid(tmp_path, monkeypatch, schema):
    source = Database(tmp_path / "source/market.sqlite3")
    s = source.observe(source.start_run(), ad("dealer:example.test", "a"), STAMP)
    source_id = vehicle(source, s)
    if schema == 2:
        source.conn.execute("DROP TABLE vehicle_aliases")
        source.conn.execute("PRAGMA user_version=2")
        source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    d = destination.observe(destination.start_run(), ad("dealer:example.test", "a"), STAMP)
    canonical = vehicle(destination, d)
    import_cli(monkeypatch, source, destination, tmp_path)
    assert destination.resolve_vehicle_id(source_id) == canonical
    assert destination.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 1
    assert destination.conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
    source.conn.close()
    destination.conn.close()


def test_explicit_no_identifier_component_merges_existing_publication_roots(tmp_path, monkeypatch):
    source = Database(tmp_path / "source/market.sqlite3")
    a = source.observe(source.start_run(), ad("a", "1"), STAMP)
    b = source.observe(source.start_run(), ad("b", "2"), STAMP)
    root, retired = vehicle(source, a), vehicle(source, b)
    source.conn.execute("UPDATE listings SET vehicle_id=? WHERE id=?", (root, b))
    source.conn.execute("DELETE FROM vehicles WHERE id=?", (retired,))
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    x = destination.observe(destination.start_run(), ad("a", "1"), STAMP)
    y = destination.observe(destination.start_run(), ad("b", "2"), STAMP)
    x_id, y_id = vehicle(destination, x), vehicle(destination, y)
    import_cli(monkeypatch, source, destination, tmp_path)
    assert destination.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 1
    assert (
        destination.resolve_vehicle_id(root)
        == destination.resolve_vehicle_id(x_id)
        == destination.resolve_vehicle_id(y_id)
    )
    proof = json.loads(
        destination.conn.execute(
            "SELECT evidence_json FROM vehicle_aliases WHERE alias_id=?", (root,)
        ).fetchone()[0]
    )
    assert len(proof["source_listings"]) == 2
    source.conn.close()
    destination.conn.close()


@pytest.mark.parametrize(
    "conflict", ["uuid", "alias", "plate", "vin", "publication", "late_component"]
)
def test_conflicting_registry_import_rejects_every_table_atomically(
    tmp_path, monkeypatch, conflict
):
    source = Database(tmp_path / "source/market.sqlite3")
    s = source.observe(source.start_run(), ad("a", "1", "GB178T", VIN), STAMP)
    source_id = vehicle(source, s)
    destination = Database(tmp_path / "dest/market.sqlite3")
    if conflict in ("plate", "late_component"):
        d = destination.observe(destination.start_run(), ad("b", "2", "GB178T", OTHER_VIN), STAMP)
    elif conflict == "vin":
        d = destination.observe(destination.start_run(), ad("b", "2", "AB123C", VIN), STAMP)
    else:
        d = destination.observe(
            destination.start_run(),
            ad(
                "a" if conflict == "publication" else "b",
                "1" if conflict == "publication" else "2",
                "AB123C",
                OTHER_VIN,
            ),
            STAMP,
        )
    if conflict == "uuid":
        old = vehicle(destination, d)
        destination.conn.execute("PRAGMA defer_foreign_keys=ON")
        destination.conn.execute("UPDATE vehicles SET id=? WHERE id=?", (source_id, old))
        destination.conn.execute(
            "UPDATE listings SET vehicle_id=? WHERE vehicle_id=?", (source_id, old)
        )
        destination.conn.commit()
    elif conflict == "alias":
        destination.conn.execute(
            "INSERT INTO vehicle_aliases VALUES (?,?,?,?,?)",
            (source_id, vehicle(destination, d), STAMP, "old", "{}"),
        )
        destination.conn.commit()
    elif conflict == "late_component":
        source.observe(source.start_run(), ad("early", "safe"), STAMP)
    before = state(destination)
    with pytest.raises(ValueError, match="Conflicting"):
        import_cli(monkeypatch, source, destination, tmp_path)
    assert state(destination) == before
    assert destination.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    source.conn.close()
    destination.conn.close()


def test_own_observation_conflict_after_registry_preparation_rolls_back(tmp_path, monkeypatch):
    source = Database(tmp_path / "source/market.sqlite3")
    source.observe(source.start_run(), ad("a", "1", "GB178T", VIN), STAMP)
    row = source.conn.execute("SELECT id,details_json FROM observations").fetchone()
    payload = json.loads(row["details_json"])
    payload["kenteken"] = "AB123C"
    source.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?", (json.dumps(payload), row["id"])
    )
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    before = state(destination)
    with pytest.raises(ValueError, match="Conflicting"):
        import_cli(monkeypatch, source, destination, tmp_path)
    assert state(destination) == before
    source.conn.close()
    destination.conn.close()


def test_complementary_bridge_keeps_destination_ids_checks_candidates(tmp_path, monkeypatch):
    source = Database(tmp_path / "source/market.sqlite3")
    s = source.observe(source.start_run(), ad("source", "1", "GB178T", VIN), STAMP)
    source_id = vehicle(source, s)
    destination = Database(tmp_path / "dest/market.sqlite3")
    a = destination.observe(destination.start_run(), ad("plate", "a", "GB178T"), STAMP)
    b = destination.observe(destination.start_run(), ad("vin", "b", vin=VIN), STAMP)
    c = destination.observe(destination.start_run(), ad("unrelated", "c"), STAMP)
    roots = [vehicle(destination, x) for x in (a, b, c)]
    for root in roots[:2]:
        pair = sorted((root, roots[2]))
        destination.conn.execute(
            "INSERT INTO identity_candidates VALUES (?,?,?,?,?)",
            (*pair, "possible", STAMP, "unconfirmed"),
        )
        destination.conn.execute(
            "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json) VALUES (?,?,?,?)",
            (root, STAMP, "rdw", json.dumps({"origin": root})),
        )
    destination.conn.commit()
    import_cli(monkeypatch, source, destination, tmp_path)
    canonical = destination.resolve_vehicle_id(source_id)
    assert all(destination.resolve_vehicle_id(r) == canonical for r in roots[:2])
    assert destination.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 2
    assert tuple(
        destination.conn.execute(
            "SELECT kenteken,vin FROM vehicles WHERE id=?", (canonical,)
        ).fetchone()
    ) == ("GB178T", VIN)
    assert {r[0] for r in destination.conn.execute("SELECT vehicle_id FROM vehicle_checks")} == {
        canonical
    }
    assert tuple(
        destination.conn.execute("SELECT vehicle_a,vehicle_b FROM identity_candidates").fetchone()
    ) == tuple(sorted((canonical, roots[2])))
    assert destination.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    source.conn.close()
    destination.conn.close()


def test_same_attributes_do_not_link_unrelated_registry_components(tmp_path, monkeypatch):
    source = Database(tmp_path / "source/market.sqlite3")
    for publisher in ("a", "b"):
        item = ad(publisher, publisher)
        item.year = 2017
        item.mileage = 100000
        item.city = "Diemen"
        source.observe(source.start_run(), item, STAMP)
    destination = Database(tmp_path / "dest/market.sqlite3")
    import_cli(monkeypatch, source, destination, tmp_path)
    assert destination.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 2
    assert destination.conn.execute("SELECT count(*) FROM vehicle_aliases").fetchone()[0] == 0
    source.conn.close()
    destination.conn.close()


def test_source_payload_publication_mismatch_is_atomic(tmp_path, monkeypatch):
    source = Database(tmp_path / "source/market.sqlite3")
    source.observe(source.start_run(), ad("a", "1"), STAMP)
    row = source.conn.execute("SELECT id,details_json FROM observations").fetchone()
    payload = json.loads(row["details_json"])
    payload["external_id"] = "different"
    source.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?", (json.dumps(payload), row["id"])
    )
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    before = state(destination)
    with pytest.raises(ValueError, match="publication disagrees"):
        import_cli(monkeypatch, source, destination, tmp_path)
    assert state(destination) == before
    source.conn.close()
    destination.conn.close()


def test_existing_alias_retains_both_audit_records_without_repeat_growth(tmp_path, monkeypatch):
    source = Database(tmp_path / "source/market.sqlite3")
    source.observe(source.start_run(), ad("a", "1", "GB178T"), STAMP)
    b = source.observe(source.start_run(), ad("b", "2"), STAMP)
    former = vehicle(source, b)
    source.attach_identity(b, "GB178T", evidence={"kind": "source_evidence"})
    source.conn.commit()
    original = dict(
        source.conn.execute("SELECT * FROM vehicle_aliases WHERE alias_id=?", (former,)).fetchone()
    )
    destination = Database(tmp_path / "dest/market.sqlite3")
    d = destination.observe(destination.start_run(), ad("a", "1", "GB178T"), STAMP)
    destination.conn.execute(
        "INSERT INTO vehicle_aliases VALUES (?,?,?,?,?)",
        (
            former,
            vehicle(destination, d),
            STAMP,
            "existing",
            json.dumps({"kind": "destination_evidence"}),
        ),
    )
    destination.conn.commit()
    import_cli(monkeypatch, source, destination, tmp_path)
    after = destination.conn.execute(
        "SELECT evidence_json FROM vehicle_aliases WHERE alias_id=?", (former,)
    ).fetchone()[0]
    proof = json.loads(after)
    assert proof["kind"] == "destination_evidence"
    assert proof["snapshot_alias_imports"][0]["snapshot_origin"]["source_alias"] == original
    import_cli(monkeypatch, source, destination, tmp_path)
    assert (
        destination.conn.execute(
            "SELECT evidence_json FROM vehicle_aliases WHERE alias_id=?", (former,)
        ).fetchone()[0]
        == after
    )
    source.conn.close()
    destination.conn.close()


def verification_table(db):
    db.conn.execute("""CREATE TABLE IF NOT EXISTS field_verifications(
        listing_id INTEGER REFERENCES listings(id), field TEXT NOT NULL, verified_at TEXT NOT NULL,
        fetch_id INTEGER REFERENCES fetches(id),value_hash TEXT NOT NULL,evidence_json TEXT NOT NULL,
        PRIMARY KEY(listing_id,field))""")
    db.conn.commit()


@pytest.mark.parametrize(
    "destination_time", [None, "2026-10-01T12:00:00+02:00", "2026-10-02T10:00:00+00:00"]
)
def test_field_verification_preserves_original_clock_maxdate_and_fetch_mapping(
    tmp_path, monkeypatch, destination_time
):
    source = Database(tmp_path / "source/market.sqlite3")
    a = source.observe(source.start_run(), ad("a", "1", "GB178T"), STAMP)
    verification_table(source)
    fetch = source.save_fetch(
        source.start_run(),
        "own",
        "https://example.test/1",
        "https://example.test/1",
        200,
        "own",
        STAMP,
        retain_raw=False,
    )
    source_verified = "2026-10-01T11:00:00+00:00"
    source.conn.execute(
        "INSERT OR REPLACE INTO field_verifications VALUES (?,?,?,?,?,?)",
        (
            a,
            "kenteken",
            source_verified,
            fetch,
            "plate-hash",
            json.dumps(
                {
                    "source": "own",
                    "fetch_id": fetch,
                    "listing_id": a,
                    "nested": {"detail_listing_id": a},
                }
            ),
        ),
    )
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    destination.observe(destination.start_run(), ad("other", "other"), STAMP)
    d = destination.observe(destination.start_run(), ad("a", "1", "GB178T"), STAMP)
    verification_table(destination)
    destination.save_fetch(
        destination.start_run(),
        "unrelated",
        "https://example.test/other",
        "https://example.test/other",
        200,
        "other",
        STAMP,
        retain_raw=False,
    )
    if destination_time:
        destination.conn.execute(
            "INSERT OR REPLACE INTO field_verifications VALUES (?,?,?,?,?,?)",
            (d, "kenteken", destination_time, None, "existing-hash", "{}"),
        )
        destination.conn.commit()
    import_cli(monkeypatch, source, destination, tmp_path)
    record = dict(
        destination.conn.execute(
            "SELECT * FROM field_verifications WHERE listing_id=? AND field=?", (d, "kenteken")
        ).fetchone()
    )
    if destination_time == "2026-10-02T10:00:00+00:00":
        assert record["verified_at"] == destination_time and record["value_hash"] == "existing-hash"
    else:
        assert record["verified_at"] == source_verified and record["value_hash"] == "plate-hash"
        assert record["listing_id"] == d and d != a
        assert record["fetch_id"] != fetch
        evidence = json.loads(record["evidence_json"])
        assert evidence["fetch_id"] == record["fetch_id"]
        assert evidence["listing_id"] == d
        assert evidence["nested"]["detail_listing_id"] == d
        original = json.loads(evidence["snapshot_origin"]["evidence_json"])
        assert original["listing_id"] == a and original["fetch_id"] == fetch
        assert (
            destination.conn.execute(
                "SELECT source FROM fetches WHERE id=?", (record["fetch_id"],)
            ).fetchone()[0]
            == "own"
        )
    import_cli(monkeypatch, source, destination, tmp_path)
    assert (
        dict(
            destination.conn.execute(
                "SELECT * FROM field_verifications WHERE listing_id=? AND field=?", (d, "kenteken")
            ).fetchone()
        )
        == record
    )
    source.conn.close()
    destination.conn.close()


@pytest.mark.parametrize("finished_at", [STAMP, None])
def test_historical_source_health_keeps_original_clock_without_missing_inference(
    tmp_path, monkeypatch, finished_at
):
    source = Database(tmp_path / "source/market.sqlite3")
    run = source.start_run()
    source.observe(run, ad("a", "1", "GB178T"), STAMP)
    source.finish_source(run, "a", "218i_all", STAMP, "partial", 10, 1, 1, 1, "archive")
    source.conn.execute("UPDATE source_runs SET finished_at=?", (finished_at,))
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    recent = destination.start_run()
    destination.observe(recent, ad("a", "1", "GB178T"), STAMP, query_key="218i_all")
    destination.finish_source(
        recent, "a", "218i_all", "2026-10-07T16:00:00+00:00", "complete", 1, 1, 1, 1
    )
    destination.conn.execute("UPDATE source_runs SET finished_at=?", ("2026-10-07T16:01:00+00:00",))
    destination.conn.commit()
    import_cli(monkeypatch, source, destination, tmp_path)
    imported = destination.conn.execute(
        "SELECT started_at,finished_at FROM source_runs WHERE error='archive'"
    ).fetchone()
    assert tuple(imported) == (STAMP, finished_at)
    health = destination.conn.execute(
        "SELECT status,finished_at FROM source_health WHERE source='a' AND query_key='218i_all'"
    ).fetchone()
    assert tuple(health) == ("complete", "2026-10-07T16:01:00+00:00")
    assert (
        destination.conn.execute(
            "SELECT count(*) FROM events WHERE kind='not_seen_in_complete_query'"
        ).fetchone()[0]
        == 0
    )
    source.conn.close()
    destination.conn.close()


def test_sparse_historical_payload_is_preserved_and_repeat_is_exact(tmp_path, monkeypatch):
    source = Database(tmp_path / "source/market.sqlite3")
    first = ad("a", "1", "GB178T")
    first.description = "Original own detail"
    first.price = 8000
    source.observe(source.start_run(), first, STAMP)
    later = "2026-10-01T11:00:00+00:00"
    sparse = ad("a", "1")
    sparse.price = 7500
    source.observe(source.start_run(), sparse, later)
    row = source.conn.execute(
        "SELECT id,details_json FROM observations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(row["details_json"])
    payload["kenteken"] = None
    payload["description"] = None
    payload["raw"]["field_evidence"].pop("kenteken", None)
    payload["raw"]["field_evidence"].pop("description", None)
    source.conn.execute(
        "UPDATE observations SET details_json=?,kenteken=NULL WHERE id=?",
        (json.dumps(payload), row["id"]),
    )
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    import_cli(monkeypatch, source, destination, tmp_path)
    own = destination.conn.execute(
        "SELECT kenteken,details_json FROM observations WHERE observed_at=?", (later,)
    ).fetchone()
    assert own["kenteken"] is None
    assert json.loads(own["details_json"])["description"] is None
    assert destination.conn.execute("SELECT kenteken FROM vehicles").fetchone()[0] == "GB178T"
    before = state(destination)
    import_cli(monkeypatch, source, destination, tmp_path)
    assert {
        t: state(destination)[t]
        for t in ["vehicles", "vehicle_aliases", "listings", "observations", "vehicle_checks"]
    } == {
        t: before[t]
        for t in ["vehicles", "vehicle_aliases", "listings", "observations", "vehicle_checks"]
    }
    source.conn.close()
    destination.conn.close()


def test_quarantine_remaps_grouping_and_retains_audit_on_repeat(tmp_path, monkeypatch):
    source = Database(tmp_path / "source/market.sqlite3")
    a = source.observe(source.start_run(), ad("a", "1"), STAMP)
    b = source.observe(source.start_run(), ad("b", "2"), STAMP)
    root, retired = vehicle(source, a), vehicle(source, b)
    source.conn.execute("UPDATE listings SET vehicle_id=? WHERE id=?", (root, b))
    source.conn.execute("DELETE FROM vehicles WHERE id=?", (retired,))
    source.conn.execute(
        "INSERT INTO identity_quarantine VALUES (?,?,?,?,?,?)",
        (root, "kenteken", "bad-input", STAMP, "source_quarantine", json.dumps({"listing_id": a})),
    )
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    x = destination.observe(destination.start_run(), ad("a", "1"), STAMP)
    y = destination.observe(destination.start_run(), ad("b", "2"), STAMP)
    old = vehicle(destination, y)
    destination.conn.execute(
        "INSERT INTO identity_quarantine VALUES (?,?,?,?,?,?)",
        (old, "vin", "old-placeholder", STAMP, "destination_quarantine", "{}"),
    )
    destination.conn.commit()
    import_cli(monkeypatch, source, destination, tmp_path)
    canonical = destination.resolve_vehicle_id(root)
    records = [
        dict(r)
        for r in destination.conn.execute("SELECT * FROM identity_quarantine ORDER BY value")
    ]
    assert len(records) == 2 and {r["vehicle_id"] for r in records} == {canonical}
    assert destination.resolve_vehicle_id(old) == canonical
    imported = next(r for r in records if r["value"] == "bad-input")
    assert imported["quarantined_at"] == STAMP and imported["reason"] == "source_quarantine"
    assert json.loads(imported["evidence_json"])["listing_id"] == x
    import_cli(monkeypatch, source, destination, tmp_path)
    assert [
        dict(r)
        for r in destination.conn.execute("SELECT * FROM identity_quarantine ORDER BY value")
    ] == records
    assert destination.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    source.conn.close()
    destination.conn.close()


@pytest.mark.parametrize("invalid", ["UNKNOWN", "   ", "VIN-A"])
def test_invalid_registry_vin_is_atomic_without_seeding_strong_identity(
    tmp_path, monkeypatch, invalid
):
    source = Database(tmp_path / "source/market.sqlite3")
    a = source.observe(source.start_run(), ad("a", "1"), STAMP)
    source.conn.execute("UPDATE vehicles SET vin=? WHERE id=?", (invalid, vehicle(source, a)))
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    before = state(destination)
    with pytest.raises(ValueError, match="Invalid snapshot registry VIN"):
        import_cli(monkeypatch, source, destination, tmp_path)
    assert state(destination) == before
    source.conn.close()
    destination.conn.close()


def test_roundtrip_preserves_source_registry_uuid_checks_and_numeric_verification(
    tmp_path, monkeypatch
):
    from car_market.provenance import value_hash

    source = Database(tmp_path / "source/market.sqlite3")
    own = ad("a", "1", "GB178T")
    own.price = 8500
    a = source.observe(source.start_run(), own, STAMP)
    b = source.observe(source.start_run(), ad("b", "2"), STAMP)
    canonical, former = vehicle(source, a), vehicle(source, b)
    source.attach_identity(b, "GB178T", evidence={"kind": "explicit_portal"})
    source.conn.commit()
    source.conn.execute(
        "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json) VALUES (?,?,?,?)",
        (canonical, STAMP, "rdw", json.dumps({"kenteken": "GB178T"})),
    )
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    import_cli(monkeypatch, source, destination, tmp_path)
    snapshot = Database(tmp_path / "exports/market.sqlite3")
    third = Database(tmp_path / "roundtrip/market.sqlite3")
    import_cli(monkeypatch, snapshot, third, tmp_path / "roundtrip-exports")
    assert third.resolve_vehicle_id(canonical) == third.resolve_vehicle_id(former) == canonical
    assert third.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 1
    assert third.conn.execute("SELECT count(*) FROM vehicle_checks").fetchone()[0] == 1
    row = third.conn.execute(
        "SELECT verified_at,value_hash FROM field_verifications WHERE field='price'"
    ).fetchone()
    assert tuple(row) == (STAMP, value_hash(8500.0))
    before = {
        t: state(third)[t]
        for t in ["vehicles", "vehicle_aliases", "listings", "observations", "vehicle_checks"]
    }
    import_cli(monkeypatch, snapshot, third, tmp_path / "roundtrip-exports")
    assert {t: state(third)[t] for t in before} == before
    assert third.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    source.conn.close()
    destination.conn.close()
    snapshot.conn.close()
    third.conn.close()


def test_all_historical_source_health_rows_survive_actual_finish_chronology_and_repeat(
    tmp_path, monkeypatch
):
    source = Database(tmp_path / "source/market.sqlite3")
    rows = [
        ("2026-10-01T09:00:00+00:00", "2026-10-01T13:00:00+00:00", "error"),
        ("2026-10-01T10:00:00+00:00", "2026-10-01T12:00:00+00:00", "complete"),
        ("2026-10-01T11:00:00+00:00", None, "partial"),
    ]
    for started, finished, status in rows:
        source.finish_source(
            source.start_run(timestamp=started),
            "a",
            "218i_all",
            started,
            status,
            2,
            1,
            1,
            1,
            finished_at=finished,
            infer_missing=False,
        )
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    import_cli(monkeypatch, source, destination, tmp_path)
    records = [
        tuple(r)
        for r in destination.conn.execute(
            "SELECT started_at,finished_at,status FROM source_runs ORDER BY started_at"
        )
    ]
    assert records == rows
    assert (
        destination.conn.execute("SELECT count(DISTINCT run_id) FROM source_runs").fetchone()[0]
        == 3
    )
    assert destination.conn.execute("SELECT status FROM source_health").fetchone()[0] == "error"
    assert (
        destination.conn.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0]
        == 0
    )
    assert {
        r[0]
        for r in destination.conn.execute(
            "SELECT started_at FROM runs WHERE id IN (SELECT run_id FROM source_runs)"
        )
    } == {r[0] for r in rows}
    import_cli(monkeypatch, source, destination, tmp_path)
    assert [
        tuple(r)
        for r in destination.conn.execute(
            "SELECT started_at,finished_at,status FROM source_runs ORDER BY started_at"
        )
    ] == rows
    source.conn.close()
    destination.conn.close()


def test_null_original_runs_keep_distinct_health_and_observation_rows(tmp_path, monkeypatch):
    source = Database(tmp_path / "source/market.sqlite3")
    source.observe(source.start_run(), ad("a", "1", "GB178T"), STAMP)
    second = ad("a", "1", "GB178T")
    second.price = 7500
    source.observe(source.start_run(), second, "2026-10-01T11:00:00+00:00")
    source.conn.execute("UPDATE observations SET run_id=NULL")
    for index, status in enumerate(["error", "partial"]):
        source.conn.execute(
            "INSERT INTO source_runs(run_id,source,query_key,started_at,finished_at,status) VALUES (NULL,?,?,?,?,?)",
            ("a", "218i_all", f"2026-10-01T{12 + index}:00:00+00:00", None, status),
        )
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    import_cli(monkeypatch, source, destination, tmp_path)
    assert destination.conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
    assert (
        destination.conn.execute("SELECT count(DISTINCT run_id) FROM observations").fetchone()[0]
        == 2
    )
    assert destination.conn.execute("SELECT count(*) FROM source_runs").fetchone()[0] == 2
    assert (
        destination.conn.execute("SELECT count(DISTINCT run_id) FROM source_runs").fetchone()[0]
        == 2
    )
    assert {r[0] for r in destination.conn.execute("SELECT run_id FROM observations")}.isdisjoint(
        {r[0] for r in destination.conn.execute("SELECT run_id FROM source_runs")}
    )
    import_cli(monkeypatch, source, destination, tmp_path)
    assert destination.conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
    assert destination.conn.execute("SELECT count(*) FROM source_runs").fetchone()[0] == 2
    source.conn.close()
    destination.conn.close()


def test_schema3_unsupported_registration_is_filtered_before_identity_matching(
    tmp_path, monkeypatch
):
    legacy_id = "dcf8d73f-3576-4af1-ba1c-f389fe46c1d8"
    source = Database(tmp_path / "source/market.sqlite3")
    a = source.observe(source.start_run(), ad("trovit", "legacy"), STAMP)
    old = vehicle(source, a)
    source.conn.execute("PRAGMA defer_foreign_keys=ON")
    source.conn.execute(
        "UPDATE vehicles SET id=?,kenteken=? WHERE id=?", (legacy_id, "RG856VTE", old)
    )
    source.conn.execute("UPDATE listings SET vehicle_id=? WHERE vehicle_id=?", (legacy_id, old))
    source.conn.execute("DROP TABLE identity_quarantine")
    source.conn.execute("DROP TABLE field_verifications")
    source.conn.execute("PRAGMA user_version=3")
    source.conn.commit()
    destination = Database(tmp_path / "dest/market.sqlite3")
    unrelated = destination.observe(destination.start_run(), ad("other", "1", "RG856VTE"), STAMP)
    unrelated_id = vehicle(destination, unrelated)
    import_cli(monkeypatch, source, destination, tmp_path)
    assert destination.conn.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 2
    assert destination.resolve_vehicle_id(legacy_id) != unrelated_id
    assert (
        destination.conn.execute(
            "SELECT kenteken FROM vehicles WHERE id=?", (legacy_id,)
        ).fetchone()[0]
        is None
    )
    assert (
        source.conn.execute("SELECT kenteken FROM vehicles WHERE id=?", (legacy_id,)).fetchone()[0]
        == "RG856VTE"
    )
    quarantine = dict(
        destination.conn.execute(
            "SELECT * FROM identity_quarantine WHERE vehicle_id=?", (legacy_id,)
        ).fetchone()
    )
    assert quarantine["value"] == "RG856VTE"
    proof = json.loads(quarantine["evidence_json"])
    new_listing = destination.conn.execute(
        "SELECT id FROM listings WHERE source='trovit'"
    ).fetchone()[0]
    assert proof["members"][0]["id"] == new_listing
    assert json.loads(proof["snapshot_origin"]["evidence_json"])["members"][0]["id"] == a
    import_cli(monkeypatch, source, destination, tmp_path)
    assert (
        dict(
            destination.conn.execute(
                "SELECT * FROM identity_quarantine WHERE vehicle_id=?", (legacy_id,)
            ).fetchone()
        )
        == quarantine
    )
    assert destination.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    source.conn.close()
    destination.conn.close()
