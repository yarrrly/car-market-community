"""Retained own AS/AT payloads must prove references without attribute matching."""

import copy
import gzip
import json
from pathlib import Path

import pytest
from car_market.db import Database
from car_market.parse import Listing, json_documents
from car_market.publisher_identity import (
    reference_from_payload,
    shared_publication_pairs,
    validate_shared_pair,
)

# Exact own/reference fields from retained AS fetch 67875 / AT fetch 100221.
# Seller/contact and unrelated payload sections are deliberately omitted.
UUID = "0386b7bd-f57d-45ed-9f4f-57cc0b9a7fe0"
OTHER = "00000000-0000-4000-8000-000000000001"
FIXTURES = Path(__file__).parent / "fixtures/quality_primary"
STAMP = "2026-10-07T10:00:00+00:00"
RETAINED = json.loads(
    (Path(__file__).parent / "fixtures/publisher_identity/shared-own-payloads.json").read_text()
)
AS_URI = "https://www.autoscout24.nl/aanbod/bmw-218-i-active-tourer-sport-line-1-hand-benzine-wit-cat_ma13mo20327-0386b7bd-f57d-45ed-9f4f-57cc0b9a7fe0"
AT_URI = "https://www.autotrader.nl/auto/voertuig/bmw-218-i-active-tourer-sport-line-1-hand-benzine-wit-cat_ma13mo20327/0386b7bd-f57d-45ed-9f4f-57cc0b9a7fe0"


def document(source):
    return {"props": {"pageProps": {"listingDetails": copy.deepcopy(RETAINED[source])}}}


def body(source, mutate=None, canonical=None, script_id="__NEXT_DATA__", duplicate=False):
    d = document(source)
    own = d["props"]["pageProps"]["listingDetails"]
    if mutate:
        mutate(own)
    script = '<script id="' + script_id + '" type="application/json">' + json.dumps(d) + "</script>"
    head = '<head><link rel="canonical" href="' + canonical + '"></head>' if canonical else ""
    return head + script + (script if duplicate else "")


@pytest.fixture
def paired(tmp_path):
    db = Database(tmp_path / "data/market.sqlite3")
    listings = {}
    fetches = {}
    for source, uri in [("autoscout24", AS_URI), ("autotrader", AT_URI)]:
        run = db.start_run()
        fetch = db.save_fetch(run, source, uri, uri, 200, body(source), STAMP)
        item = Listing(source, UUID, uri, "BMW 218i Active Tourer", fuel="petrol").qualify()
        listings[source] = db.observe(run, item, STAMP, fetch)
        fetches[source] = fetch
    yield db, listings, fetches
    db.conn.close()


def replace(db, fetch, body_text, status=200, source=None, uri=None):
    # Change the specific retained response; preserve observation history.
    import hashlib

    row = db.conn.execute("SELECT * FROM fetches WHERE id=?", (fetch,)).fetchone()
    digest = hashlib.sha256(body_text.encode()).hexdigest()
    target = db.raw / (digest + ".html.gz")
    target.write_bytes(gzip.compress(body_text.encode()))
    db.conn.execute(
        "UPDATE fetches SET content_hash=?,raw_path=?,status=?,source=?,final_url=? WHERE id=?",
        (
            digest,
            "raw/" + target.name,
            status,
            source or row["source"],
            uri or row["final_url"],
            fetch,
        ),
    )
    db.conn.commit()


def test_real_retained_payloads_prove_explicit_shared_full_webpage(paired):
    db, listings, fetches = paired
    before = list(db.conn.iterdump())
    pairs = shared_publication_pairs(db)
    assert len(pairs) == 1
    pair = pairs[0]
    proof = pair["evidence"][0]
    assert (
        pair["listing_a"] == listings["autoscout24"] and pair["listing_b"] == listings["autotrader"]
    )
    assert proof["reference_url"] == AS_URI
    assert (
        proof["a_fetch_id"] == fetches["autoscout24"]
        and proof["b_fetch_id"] == fetches["autotrader"]
    )
    assert proof["autoscout"]["own_uuid"] == proof["autotrader"]["own_uuid"] == UUID
    assert proof["autoscout"]["publisher_uri"] == proof["autotrader"]["publisher_uri"]
    assert validate_shared_pair(db, pair["listing_a"], pair["listing_b"], pair["evidence"])
    assert validate_shared_pair(db, pair["listing_b"], pair["listing_a"], proof)
    assert list(db.conn.iterdump()) == before
    assert not db.conn.in_transaction


@pytest.mark.parametrize(
    "mutation",
    [
        lambda own: own.update(id=OTHER),
        lambda own: own.update(vehicle=None),
        lambda own: own.pop("webPage", None),
        lambda own: own.update(webPage=own["webPage"].replace(UUID, OTHER)),
        lambda own: own.update(webPage="https://evil.test/aanbod/" + UUID),
        lambda own: own.update(webPage="https://www.autoscout24.nl.evil.test/aanbod/" + UUID),
        lambda own: own.update(webPage=own["webPage"] + "?different_full_uri=1"),
        lambda own: own.update(webPage="https://www.autotrader.nl/auto/voertuig/fixture/" + UUID),
    ],
)
def test_id_alone_or_changed_reference_does_not_prove_shared_publication(paired, mutation):
    db, _, fetches = paired
    replace(db, fetches["autotrader"], body("autotrader", mutation))
    assert shared_publication_pairs(db) == []


@pytest.mark.parametrize(
    "canonical", [AS_URI.replace(UUID, OTHER), "https://evil.test/aanbod/" + UUID]
)
def test_wrong_own_canonical_is_rejected(paired, canonical):
    db, _, fetches = paired
    replace(db, fetches["autoscout24"], body("autoscout24", canonical=canonical))
    assert shared_publication_pairs(db) == []


@pytest.mark.parametrize(
    "source,uri",
    [
        ("autotrader", AS_URI),
        ("autoscout24", AT_URI),
        ("autoscout24", "https://attacker@www.autoscout24.nl/aanbod/" + UUID),
        ("autoscout24", "https://www.autoscout24.nl:8443/aanbod/" + UUID),
        ("autoscout24", "https://www.autoscout24.nl/lst/" + UUID),
    ],
)
def test_pure_reference_rejects_wrong_route_source_or_credentials(source, uri):
    own = document("autoscout24")["props"]["pageProps"]["listingDetails"]
    assert reference_from_payload(source, own, uri) is None


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate_next",
        "untyped_script",
        "related_only",
        "bad_source",
        "not200",
        "hash",
        "unsafe_path",
        "symlink",
    ],
)
def test_unowned_or_unsafe_retained_response_is_not_evidence(paired, kind, tmp_path):
    db, _, fetches = paired
    fetch = fetches["autotrader"]
    if kind == "duplicate_next":
        replace(db, fetch, body("autotrader", duplicate=True))
    elif kind == "untyped_script":
        replace(db, fetch, body("autotrader", script_id="related_inventory"))
    elif kind == "related_only":
        doc = document("autotrader")
        own = doc["props"]["pageProps"].pop("listingDetails")
        doc["props"]["pageProps"]["relatedListings"] = [own]
        replace(
            db,
            fetch,
            '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(doc) + "</script>",
        )
    elif kind == "bad_source":
        replace(db, fetch, body("autotrader"), source="autoscout24")
    elif kind == "not200":
        replace(db, fetch, body("autotrader"), status=403)
    elif kind == "hash":
        db.conn.execute("UPDATE fetches SET content_hash=? WHERE id=?", ("wrong", fetch))
        db.conn.commit()
    elif kind == "unsafe_path":
        db.conn.execute("UPDATE fetches SET raw_path=? WHERE id=?", ("../elsewhere.html.gz", fetch))
        db.conn.commit()
    elif kind == "symlink":
        row = db.conn.execute("SELECT raw_path FROM fetches WHERE id=?", (fetch,)).fetchone()
        original = db.path.parent / row["raw_path"]
        outside = tmp_path / "outside.gz"
        outside.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(outside)
    assert shared_publication_pairs(db) == []


def test_exact_url_fallback_without_observation_is_still_verified(paired):
    db, _, _ = paired
    db.conn.execute("UPDATE observations SET fetch_id=NULL")
    db.conn.commit()
    pair = shared_publication_pairs(db)[0]
    assert all(
        pair["evidence"][0][source]["retrieval"] == "indexed_exact_url_fallback"
        for source in ["autoscout", "autotrader"]
    )
    assert validate_shared_pair(db, pair["listing_a"], pair["listing_b"], pair["evidence"])


def test_current_same_root_and_changed_external_uuid_are_not_pairs(paired):
    db, listings, _ = paired
    root = db.conn.execute(
        "SELECT vehicle_id FROM listings WHERE id=?", (listings["autoscout24"],)
    ).fetchone()[0]
    db.conn.execute("UPDATE listings SET vehicle_id=? WHERE id=?", (root, listings["autotrader"]))
    db.conn.commit()
    assert shared_publication_pairs(db) == []
    db.conn.execute("UPDATE listings SET external_id=? WHERE id=?", (OTHER, listings["autotrader"]))
    db.conn.commit()
    assert shared_publication_pairs(db) == []


def test_model_conflict_is_only_existing_warning_no_classification_mutation(paired):
    db, listings, _ = paired
    db.conn.execute(
        "UPDATE listings SET scope='model_conflict' WHERE id=?", (listings["autoscout24"],)
    )
    db.conn.commit()
    before = list(db.conn.iterdump())
    pair = shared_publication_pairs(db)[0]
    assert pair["warnings"] == ["existing_model_conflict"]
    assert pair["scope_b"] == "target"
    assert list(db.conn.iterdump()) == before


@pytest.mark.parametrize(
    "mutation",
    [
        lambda proof: proof.update(a_fetch_id=proof["b_fetch_id"]),
        lambda proof: proof.update(reference_url=AS_URI.replace(UUID, OTHER)),
        lambda proof: proof["autotrader"].update(fetched_at="2030-01-01T00:00:00+00:00"),
        lambda proof: proof["autoscout"].update(content_hash="forged"),
        lambda proof: proof["autoscout"].update(raw_path="raw/forged.gz"),
    ],
)
def test_direct_validator_rejects_wrong_fetch_or_forged_proof(paired, mutation):
    db, _, _ = paired
    pair = shared_publication_pairs(db)[0]
    proof = copy.deepcopy(pair["evidence"][0])
    mutation(proof)
    before = list(db.conn.iterdump())
    with pytest.raises(ValueError):
        validate_shared_pair(db, pair["listing_a"], pair["listing_b"], proof)
    assert list(db.conn.iterdump()) == before


def test_validation_rereads_raw_and_never_commits_caller_transaction(paired):
    db, _, fetches = paired
    pair = shared_publication_pairs(db)[0]
    before = list(db.conn.iterdump())
    with pytest.raises(RuntimeError), db.transaction():
        db.conn.execute(
            "INSERT INTO vehicles(id,created_at) VALUES (?,?)", ("transaction-marker", STAMP)
        )
        assert shared_publication_pairs(db)
        assert db.conn.in_transaction
        assert validate_shared_pair(db, pair["listing_a"], pair["listing_b"], pair["evidence"])
        observer = Database.read_only(db.path)
        assert (
            observer.conn.execute("SELECT 1 FROM vehicles WHERE id='transaction-marker'").fetchone()
            is None
        )
        observer.conn.close()
        raise RuntimeError("rollback boundary")
    assert list(db.conn.iterdump()) == before
    row = db.conn.execute(
        "SELECT raw_path FROM fetches WHERE id=?", (fetches["autotrader"],)
    ).fetchone()
    (db.path.parent / row["raw_path"]).write_bytes(gzip.compress(b"changed raw"))
    with pytest.raises(ValueError):
        validate_shared_pair(db, pair["listing_a"], pair["listing_b"], pair["evidence"])


def test_raw_directory_symlink_escape_is_rejected(paired, tmp_path):
    db, _, _ = paired
    outside = tmp_path / "outside-raw"
    db.raw.rename(outside)
    db.raw.symlink_to(outside, target_is_directory=True)
    assert shared_publication_pairs(db) == []


@pytest.mark.parametrize("suffix", ["?tracking=1", "#fragment"])
def test_shared_publisher_uri_rejects_query_and_fragment(suffix):
    own = copy.deepcopy(RETAINED["autoscout24"])
    own["webPage"] += suffix
    assert reference_from_payload("autoscout24", own, AS_URI) is None


def test_trimmed_real_fixture_without_webpage_is_not_bridge_evidence():
    body = (FIXTURES / "autoscout24-789ada1c-ded9-465d-93ff-f0783b445b2e.html").read_text()
    own = next(
        d["props"]["pageProps"]["listingDetails"]
        for d in json_documents(body)
        if isinstance(d, dict) and d.get("props", {}).get("pageProps", {}).get("listingDetails")
    )
    assert (
        reference_from_payload(
            "autoscout24",
            own,
            "https://www.autoscout24.nl/aanbod/789ada1c-ded9-465d-93ff-f0783b445b2e",
        )
        is None
    )
