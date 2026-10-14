"""Publication and stock evidence checks: interface mocks and exact own AS projections."""

import copy
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from car_market import germany, germany_identity
from car_market.db import Database
from car_market.germany_identity import bridge_de_publications
from car_market.parse import Listing, Page

STAMP = "2026-10-07T10:00:00+00:00"
LATER = "2026-10-07T11:00:00+00:00"


@pytest.fixture
def paired(tmp_path, monkeypatch):
    db = Database(tmp_path / "data/market.sqlite3")
    state = SimpleNamespace(
        db=db, items={}, listings={}, fetches={}, bodies={}, outputs={}, calls=[]
    )

    def source_for_url(uri):
        return {
            "dealer.example": "dealer:dealer.example",
            "mobile.example": "mobile_de",
        }.get(urlsplit(uri).hostname)

    def canonical_de_id(source, uri):
        parsed = urlsplit(uri)
        if source == "mobile_de" and parsed.path == "/details.html":
            ids = parse_qs(parsed.query).get("id", [])
            return ids[0] if len(ids) == 1 and ids[0].isdigit() else None
        if source == "dealer:dealer.example" and parsed.path.startswith("/stock/"):
            return parsed.path.rsplit("/", 1)[-1]
        return None

    def parse(source, body, uri):
        state.calls.append((source, body, uri))
        return copy.deepcopy(state.outputs[body])

    monkeypatch.setattr(germany, "source_for_url", source_for_url)
    monkeypatch.setattr(germany, "canonical_de_id", canonical_de_id)
    monkeypatch.setattr(germany, "parse_germany_page", parse)
    target_uri = "https://mobile.example/details.html?id=900001"
    reference = {
        "source": "mobile_de",
        "url": target_uri,
        "external_id": "900001",
        "kind": "explicit_own_publication_link",
    }
    for key, source, identity, uri, stamp in (
        ("a", "dealer:dealer.example", "stock-a", "https://dealer.example/stock/stock-a", STAMP),
        ("b", "mobile_de", "900001", target_uri, LATER),
    ):
        raw = {"market_country": "DE"}
        if key == "a":
            raw["publication_reference"] = reference
        item = Listing(source, identity, uri, "mock parser model proof", scope="target", raw=raw)
        # Body tokens do not represent real market payload fields.
        body = "<head></head><body>parser-contract-" + key + "</body>"
        run = db.start_run(timestamp=stamp)
        fetch = db.save_fetch(run, source, uri, uri, 200, body, stamp)
        state.listings[key] = db.observe(run, copy.deepcopy(item), stamp, fetch)
        state.fetches[key] = fetch
        state.items[key] = item
        state.bodies[key] = body
        state.outputs[body] = Page([copy.deepcopy(item)])
    yield state
    db.conn.close()


def roots(state):
    return {
        key: state.db.conn.execute("SELECT vehicle_id FROM listings WHERE id=?", (lid,)).fetchone()[
            0
        ]
        for key, lid in state.listings.items()
    }


def snapshot(db):
    return list(db.conn.iterdump())


def replace_body(state, key, body):
    row = state.db.conn.execute(
        "SELECT * FROM fetches WHERE id=?", (state.fetches[key],)
    ).fetchone()
    digest = hashlib.sha256(body.encode()).hexdigest()
    path = state.db.raw / (digest + ".html.gz")
    path.write_bytes(gzip.compress(body.encode()))
    state.db.conn.execute(
        "UPDATE fetches SET content_hash=?,raw_path=? WHERE id=?",
        (digest, "raw/" + path.name, row["id"]),
    )
    state.db.conn.commit()
    state.outputs[body] = state.outputs[state.bodies[key]]
    state.bodies[key] = body


def reject_unchanged(state, error):
    before = snapshot(state.db)
    result = bridge_de_publications(state.db)
    assert result["merged"] == 0
    assert len(result["conflicts"]) == 1
    assert error in result["conflicts"][0]["error"]
    assert snapshot(state.db) == before
    assert not state.db.conn.execute("PRAGMA foreign_key_check").fetchall()
    return result


def test_exact_link_merges_provisional_roots_without_fact_or_freshness_changes(paired):
    state = paired
    original = roots(state)
    tables = (
        "observations",
        "fetches",
        "field_verifications",
        "events",
        "runs",
        "query_membership",
    )
    before = {
        table: [tuple(row) for row in state.db.conn.execute(f"SELECT * FROM {table}")]
        for table in tables
    }
    listings = [
        tuple(row)
        for row in state.db.conn.execute(
            "SELECT id,source,external_id,url,first_seen,last_seen,availability,scope FROM listings ORDER BY id"
        )
    ]
    assert bridge_de_publications(state.db) == {"verified_pairs": 1, "merged": 1, "conflicts": []}
    assert set(roots(state).values()) == {original["a"]}
    assert state.db.resolve_vehicle_id(original["b"]) == original["a"]
    assert state.db.conn.execute("SELECT kenteken,vin FROM vehicles").fetchone()[:] == (None, None)
    assert {
        table: [tuple(row) for row in state.db.conn.execute(f"SELECT * FROM {table}")]
        for table in tables
    } == before
    assert [
        tuple(row)
        for row in state.db.conn.execute(
            "SELECT id,source,external_id,url,first_seen,last_seen,availability,scope FROM listings ORDER BY id"
        )
    ] == listings
    alias = state.db.conn.execute("SELECT * FROM vehicle_aliases").fetchone()
    evidence = json.loads(alias["evidence_json"])
    assert alias["reason"] == "explicit_DE_publication_reference"
    assert evidence["reference"] == state.items["a"].raw["publication_reference"]
    assert evidence["a"]["fetch_id"] == state.fetches["a"]
    assert evidence["b"]["fetch_id"] == state.fetches["b"]
    assert evidence["a"]["content_hash"] == hashlib.sha256(state.bodies["a"].encode()).hexdigest()
    assert evidence["b"]["market_country"] == "DE"
    assert len(state.calls) == 2
    assert not state.db.conn.execute("PRAGMA foreign_key_check").fetchall()
    after = snapshot(state.db)
    assert bridge_de_publications(state.db) == {"verified_pairs": 1, "merged": 0, "conflicts": []}
    assert snapshot(state.db) == after


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize(
    "field,value,error",
    [
        ("status", 403, "missing_successful_own_fetch"),
        ("error", "blocked", "missing_successful_own_fetch"),
        ("source", "mobile", "own_fetch_source_mismatch"),
        ("raw_path", None, "missing_retained_raw"),
        ("raw_path", "../outside.gz", "unsafe_raw_path"),
        ("raw_path", "/outside.gz", "unsafe_raw_path"),
        ("raw_path", "raw/../outside.gz", "unsafe_raw_path"),
        ("content_hash", "wrong", "retained_raw_hash_mismatch"),
        ("final_url", "https://evil.example/details.html?id=900001", "publication_URI"),
        ("url", "https://evil.example/details.html?id=900001", "publication_URI"),
    ],
)
def test_both_fetches_must_be_retained_own_and_hash_verified(paired, side, field, value, error):
    paired.db.conn.execute(
        f"UPDATE fetches SET {field}=? WHERE id=?", (value, paired.fetches[side])
    )
    paired.db.conn.commit()
    reject_unchanged(paired, error)


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("kind", ["file", "raw_root", "corrupt", "missing"])
def test_raw_escapes_corruption_and_missing_bytes_reject(paired, tmp_path, side, kind):
    row = paired.db.conn.execute(
        "SELECT raw_path FROM fetches WHERE id=?", (paired.fetches[side],)
    ).fetchone()
    path = paired.db.path.parent / row[0]
    if kind == "file":
        outside = tmp_path / "outside.gz"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    elif kind == "raw_root":
        outside = tmp_path / "external-raw"
        paired.db.raw.rename(outside)
        paired.db.raw.symlink_to(outside, target_is_directory=True)
    elif kind == "corrupt":
        path.write_bytes(b"not gzip")
    else:
        path.unlink()
    reject_unchanged(
        paired, "unreadable_retained_raw" if kind == "corrupt" else "raw_path_escape_or_missing"
    )


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("country", [None, "NL", "D", ""])
def test_stored_DE_does_not_substitute_for_reparsed_country(paired, side, country):
    paired.outputs[paired.bodies[side]].listings[0].raw["market_country"] = country
    reject_unchanged(paired, "own_payload_country_not_DE")


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize(
    "change,error",
    [
        ("multiple", "not_exactly_one_own_publication"),
        ("empty", "not_exactly_one_own_publication"),
        ("total", "not_exactly_one_own_publication"),
        ("identity", "own_payload_registry_ID_mismatch"),
        ("source", "own_payload_registry_ID_mismatch"),
        ("url", "publication_URI"),
        ("kenteken", "DE_payload_has_kenteken"),
        ("vin", "invalid_own_VIN"),
        ("model", "own_payload_model_out_of_scope"),
        ("filter", "own_payload_country_filter_conflict"),
    ],
)
def test_own_model_and_identity_interface_is_required(paired, side, change, error):
    page = paired.outputs[paired.bodies[side]]
    if change == "multiple":
        page.listings.append(copy.deepcopy(page.listings[0]))
    elif change == "empty":
        page.listings.clear()
    elif change == "total":
        page.total = 20
    elif change == "filter":
        page.search_filters = {"country": "NL"}
    else:
        field, value = {
            "identity": ("external_id", "other"),
            "source": ("source", "autoscout24_de"),
            "url": ("url", "https://evil.example/stock/stock-a"),
            "kenteken": ("kenteken", "BAB1234"),
            "vin": ("vin", "WBA1234"),
            "model": ("scope", "excluded"),
        }[change]
        setattr(page.listings[0], field, value)
    reject_unchanged(paired, error)


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize(
    "head,error",
    [
        ('<link rel="canonical" href="https://evil.example/stock/stock-a">', "publication_URI"),
        (
            '<link rel="canonical" href="https://mobile.example/details.html?id=900002">',
            "publication_URI",
        ),
        ('<link rel="canonical" href="">', "empty_head_canonical"),
        (
            '<link rel="canonical" href="/one"><link rel="canonical" href="/two">',
            "ambiguous_head_canonical",
        ),
    ],
)
def test_head_canonical_is_checked_even_when_mocked_parser_accepts(paired, side, head, error):
    replace_body(paired, side, "<head>" + head + "</head><body>canonical-contract</body>")
    reject_unchanged(paired, error)


def test_forged_stored_reference_is_not_evidence(paired):
    paired.outputs[paired.bodies["a"]].listings[0].raw.pop("publication_reference")
    reject_unchanged(paired, "missing_explicit_own_publication_reference")


def test_changed_reparsed_reference_does_not_reuse_stored_candidate(paired):
    paired.outputs[paired.bodies["a"]].listings[0].raw["publication_reference"]["url"] += (
        "&tracking=changed"
    )
    reject_unchanged(paired, "stored_reference_not_proven_by_own_payload")


@pytest.mark.parametrize(
    "reference,error",
    [
        (
            {
                "source": "mobile_de",
                "external_id": "900001",
                "url": "https://evil.example/details.html?id=900001",
                "kind": "explicit_own_publication_link",
            },
            "publication_URI",
        ),
        (
            {
                "source": "mobile",
                "external_id": "900001",
                "url": "https://mobile.example/details.html?id=900001",
                "kind": "explicit_own_publication_link",
            },
            "non_DE_source",
        ),
        (
            {
                "source": "mobile_de",
                "external_id": "900002",
                "url": "https://mobile.example/details.html?id=900001",
                "kind": "explicit_own_publication_link",
            },
            "publication_URI",
        ),
        (
            {
                "source": "mobile_de",
                "external_id": "900002",
                "url": "https://mobile.example/details.html?id=900002",
                "kind": "explicit_own_publication_link",
            },
            "referenced_publication_not_in_registry",
        ),
        (
            {
                "source": "mobile_de",
                "external_id": "900001",
                "url": "https://mobile.example/details.html?id=900001",
                "kind": "similar_specs",
            },
            "missing_explicit_own_publication_reference",
        ),
    ],
)
def test_reference_source_site_ID_and_kind_are_required(paired, reference, error):
    row = paired.db.conn.execute(
        "SELECT id,details_json FROM observations WHERE listing_id=?", (paired.listings["a"],)
    ).fetchone()
    details = json.loads(row["details_json"])
    details["raw"]["publication_reference"] = reference
    paired.db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?", (json.dumps(details), row["id"])
    )
    paired.db.conn.commit()
    reject_unchanged(paired, error)


def test_missing_latest_raw_never_falls_back_to_older_proof(paired):
    db = paired.db
    run = db.start_run(timestamp=LATER)
    uri = paired.items["a"].url
    fetch = db.save_fetch(run, paired.items["a"].source, uri, uri, 200, None, LATER)
    db.observe(run, copy.deepcopy(paired.items["a"]), LATER, fetch, preserve_observation=True)
    reject_unchanged(paired, "missing_retained_raw")


@pytest.mark.parametrize("location", ["root", "payload"])
def test_conflicting_VIN_blocks_verified_pair_without_partial_transfer(paired, location):
    identities = roots(paired)
    for key, vin in (("a", "WBA12345678901234"), ("b", "WBA12345678901235")):
        if location == "root":
            paired.db.conn.execute("UPDATE vehicles SET vin=? WHERE id=?", (vin, identities[key]))
        else:
            paired.outputs[paired.bodies[key]].listings[0].vin = vin
    paired.db.conn.commit()
    assert reject_unchanged(paired, "conflicting_VIN_identities")["verified_pairs"] == 1


def test_reparsed_VIN_does_not_create_registry_VIN(paired):
    paired.outputs[paired.bodies["a"]].listings[0].vin = "WBA12345678901234"
    assert bridge_de_publications(paired.db)["merged"] == 1
    assert paired.db.conn.execute("SELECT vin FROM vehicles").fetchone()[0] is None


def test_root_transfer_preserves_relations_and_existing_model_conflict(paired):
    db = paired.db
    identities = roots(paired)
    loser, winner = identities["a"], identities["b"]
    db.conn.execute("UPDATE vehicles SET vin=? WHERE id=?", ("WBA12345678901234", winner))
    db.conn.execute("INSERT INTO vehicles VALUES ('third',NULL,NULL,?)", (STAMP,))
    db.conn.execute(
        "INSERT INTO vehicle_aliases VALUES (?,?,?,?,?)",
        ("old-root", loser, STAMP, "prior-proof", '{"keep":true}'),
    )
    db.conn.execute(
        "INSERT INTO vehicle_checks(vehicle_id,checked_at,provider,data_json,fetch_id) VALUES (?,?,?,?,?)",
        (loser, STAMP, "contract-check", "{}", paired.fetches["a"]),
    )
    db.conn.execute(
        "INSERT INTO identity_quarantine VALUES (?,?,?,?,?,?)",
        (loser, "vin", "partial", STAMP, "review", "{}"),
    )
    first, second = sorted((loser, "third"))
    db.conn.execute(
        "INSERT INTO identity_candidates VALUES (?,?,?,?,?)",
        (first, second, "review-only", STAMP, "unconfirmed"),
    )
    db.conn.execute(
        "UPDATE listings SET scope='model_conflict' WHERE id=?", (paired.listings["a"],)
    )
    db.conn.commit()
    assert bridge_de_publications(db)["merged"] == 1
    assert db.resolve_vehicle_id(loser) == db.resolve_vehicle_id("old-root") == winner
    assert db.conn.execute(
        "SELECT reason,evidence_json FROM vehicle_aliases WHERE alias_id='old-root'"
    ).fetchone()[:] == ("prior-proof", '{"keep":true}')
    assert db.conn.execute("SELECT vehicle_id,fetch_id FROM vehicle_checks").fetchone()[:] == (
        winner,
        paired.fetches["a"],
    )
    assert db.conn.execute("SELECT vehicle_id FROM identity_quarantine").fetchone()[0] == winner
    assert set(
        db.conn.execute("SELECT vehicle_a,vehicle_b FROM identity_candidates").fetchone()
    ) == {winner, "third"}
    assert {row[0] for row in db.conn.execute("SELECT scope FROM listings")} == {"model_conflict"}
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


# Minimal detail payload with the fields this parser reads; other branches are omitted.
NORMAL_AS_DETAIL = {
    "id": "d355620c-ac23-420e-8b61-c66de137c387",
    "status": "Active",
    "webPage": "https://www.autoscout24.de/angebote/bmw-2er-i-active-tourer-pano-ahk-dab-led-shz-benzin-schwarz-cat_ma13gr100098ge352va426mt713tr479989-d355620c-ac23-420e-8b61-c66de137c387",
    "identifier": {
        "offerReference": "208472",
        "legacyId": None,
        "crossReferenceId": "BmwHo-6772775",
    },
    "location": {
        "countryCode": "DE",
        "zip": "83026",
        "city": "Rosenheim",
        "street": "Sepp-Heindl-Straße 2",
        "latitude": 47.84327,
        "longitude": 12.08391,
    },
    "seller": {"id": 26852, "type": "Dealer", "isDealer": True},
    "vehicle": {
        "make": "BMW",
        "model": "218",
        "motorTypeName": "218i",
        "variant": "Active Tourer",
        "modelVersionInput": "i Active Tourer Pano. AHK DAB LED Shz.",
        "fuelCategory": {"raw": "B", "formatted": "Benzin"},
        "transmissionType": "Automatik",
        "firstRegistrationDateRaw": "2025-07-01",
        "mileageInKmRaw": 2821,
    },
}
STOCK_CLOCK = "2026-10-07T21:40:00+00:00"
NORMAL_STAMP = "2026-10-07T21:09:00.246959+00:00"
SMYLE_STAMP = "2026-10-07T20:48:31.873634+00:00"


@pytest.fixture
def stock_pair(tmp_path, monkeypatch):
    db = Database(tmp_path / "data/market.sqlite3")
    state = SimpleNamespace(db=db, items={}, listings={}, fetches={}, bodies={}, documents={})
    normal = {"props": {"pageProps": {"listingDetails": copy.deepcopy(NORMAL_AS_DETAIL)}}}
    smyle = json.loads((Path(__file__).parent / "fixtures/germany_smyle/detail.json").read_text())
    meta = json.loads((Path(__file__).parent / "fixtures/germany_smyle/manifest.json").read_text())
    smyle_uri = next(row["url"] for row in meta if row["fixture"] == "detail.json")
    monkeypatch.setattr(germany_identity, "now", lambda: STOCK_CLOCK)
    for key, document, uri, stamp in [
        ("a", normal, NORMAL_AS_DETAIL["webPage"], NORMAL_STAMP),
        ("b", smyle, smyle_uri, SMYLE_STAMP),
    ]:
        body = json.dumps(document, ensure_ascii=False)
        item = germany.parse_germany_page("autoscout24_de", body, uri).listings[0]
        run = db.start_run(timestamp=stamp)
        fetch = db.save_fetch(run, "autoscout24_de", uri, uri, 200, body, stamp)
        state.listings[key] = db.observe(run, copy.deepcopy(item), stamp, fetch)
        state.fetches[key] = fetch
        state.items[key] = item
        state.documents[key] = document
        state.bodies[key] = body
    yield state
    db.conn.close()


def stock_own(state, key):
    document = state.documents[key]
    return (
        document["props"]["pageProps"]["listingDetails"]
        if key == "a"
        else document["pageProps"]["properData"]["carDetails"]
    )


def replace_stock_document(state, key):
    body = json.dumps(state.documents[key], ensure_ascii=False)
    digest = hashlib.sha256(body.encode()).hexdigest()
    path = state.db.raw / (digest + ".html.gz")
    path.write_bytes(gzip.compress(body.encode()))
    state.db.conn.execute(
        "UPDATE fetches SET content_hash=?,raw_path=? WHERE id=?",
        (digest, "raw/" + path.name, state.fetches[key]),
    )
    state.db.conn.commit()
    state.bodies[key] = body


def test_real_AS_normal_SMYLE_stock_proof_merges_without_fact_changes(stock_pair):
    state, db = stock_pair, stock_pair.db
    original = roots(state)
    untouched = (
        "observations",
        "field_verifications",
        "events",
        "fetches",
        "runs",
        "query_membership",
    )
    before = {
        table: [tuple(row) for row in db.conn.execute(f"SELECT * FROM {table}")]
        for table in untouched
    }
    assert bridge_de_publications(db) == {"verified_pairs": 1, "merged": 1, "conflicts": []}
    winner = original["b"]
    assert set(roots(state).values()) == {winner}
    assert all(db.resolve_vehicle_id(identity) == winner for identity in original.values())
    assert {
        table: [tuple(row) for row in db.conn.execute(f"SELECT * FROM {table}")]
        for table in untouched
    } == before
    assert db.conn.execute("SELECT kenteken,vin FROM vehicles").fetchone()[:] == (None, None)
    alias = db.conn.execute("SELECT * FROM vehicle_aliases").fetchone()
    assert alias["reason"] == "exact_own_AS_dealer_stock"
    proof = json.loads(alias["evidence_json"])
    assert proof["kind"] == "exact_own_AS_dealer_stock"
    assert (proof["account_id"], proof["stock_reference"]) == ("26852", "208472")
    assert proof["location"] == {
        "countryCode": "DE",
        "zip": "83026",
        "street": "Sepp-Heindl-Straße 2",
        "city": "Rosenheim",
    }
    assert proof["common_fields"] == {
        "year": 2025,
        "fuel": "petrol",
        "transmission": "automatic",
        "scope": "target",
    }
    assert (
        proof["a"]["fetch_id"] == state.fetches["a"]
        and proof["b"]["fetch_id"] == state.fetches["b"]
    )
    assert proof["a"]["field_paths"]["seller_id"] == "pageProps.listingDetails.seller.id"
    assert (
        proof["b"]["field_paths"]["seller_id"]
        == "pageProps.properData.carDetails.ocsInfo.seller.id"
    )
    after = snapshot(db)
    assert bridge_de_publications(db) == {"verified_pairs": 1, "merged": 0, "conflicts": []}
    assert snapshot(db) == after
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize(
    "field,value",
    [("status", 403), ("content_hash", "wrong"), ("source", "autoscout24"), ("raw_path", None)],
)
def test_stock_bridge_revalidates_both_owned_fetches(stock_pair, side, field, value):
    stock_pair.db.conn.execute(
        f"UPDATE fetches SET {field}=? WHERE id=?", (value, stock_pair.fetches[side])
    )
    stock_pair.db.conn.commit()
    before = snapshot(stock_pair.db)
    result = bridge_de_publications(stock_pair.db)
    assert result["merged"] == 0 and len(result["conflicts"]) == 1
    assert snapshot(stock_pair.db) == before


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("field", ["fetched_at", "observed_at", "last_seen"])
@pytest.mark.parametrize(
    "value", ["2026-10-07T08:39:59+00:00", "2026-10-07T21:40:01+00:00", "not-a-date"]
)
def test_stock_proof_requires_actual_13h_observation_fetch_and_presence_dates(
    stock_pair, side, field, value
):
    db = stock_pair.db
    if field == "fetched_at":
        db.conn.execute(
            "UPDATE fetches SET fetched_at=? WHERE id=?", (value, stock_pair.fetches[side])
        )
    elif field == "observed_at":
        db.conn.execute(
            "UPDATE observations SET observed_at=? WHERE listing_id=?",
            (value, stock_pair.listings[side]),
        )
    else:
        db.conn.execute(
            "UPDATE listings SET last_seen=? WHERE id=?", (value, stock_pair.listings[side])
        )
    db.conn.commit()
    reject_unchanged(stock_pair, "AS_stock_not_current_13h")


@pytest.mark.parametrize(
    "state", ["seller_marked_sold", "seller_reserved", "removed", "not_seen_in_complete_query"]
)
def test_stock_current_presence_must_be_active(stock_pair, state):
    stock_pair.db.conn.execute(
        "UPDATE listings SET availability=? WHERE id=?", (state, stock_pair.listings["b"])
    )
    stock_pair.db.conn.commit()
    reject_unchanged(stock_pair, "AS_stock_publication_not_active")


@pytest.mark.parametrize(
    "field,value",
    [
        ("zip", "83025"),
        ("street", "Sepp-Heindl-Straße 3"),
        ("city", "Munich"),
        ("zip", ""),
        ("street", "Sepp-Heindl-Straße"),
    ],
)
def test_same_stock_at_another_or_incomplete_branch_never_merges(stock_pair, field, value):
    stock_own(stock_pair, "b")["ocsInfo"]["location"][field] = value
    replace_stock_document(stock_pair, "b")
    before = snapshot(stock_pair.db)
    result = bridge_de_publications(stock_pair.db)
    assert result["merged"] == 0 and len(result["conflicts"]) == 1
    assert snapshot(stock_pair.db) == before


@pytest.mark.parametrize(
    "field,value", [("account", "26853"), ("stock", "208473"), ("stock", ""), ("account", True)]
)
def test_stored_stock_key_cannot_replace_the_reparsed_seller_and_offerReference(
    stock_pair, field, value
):
    own = stock_own(stock_pair, "b")
    if field == "account":
        own["ocsInfo"]["seller"]["id"] = value
    else:
        own["identifier"]["offerReference"] = value
    replace_stock_document(stock_pair, "b")
    before = snapshot(stock_pair.db)
    result = bridge_de_publications(stock_pair.db)
    assert result["merged"] == 0 and len(result["conflicts"]) == 1
    assert snapshot(stock_pair.db) == before


@pytest.mark.parametrize(
    "change", ["inactive", "country", "guid", "identity", "scope", "year", "fuel", "gear"]
)
def test_stock_payload_common_facts_and_own_identity_are_required(stock_pair, change):
    own = stock_own(stock_pair, "b")
    if change == "inactive":
        own["ocsInfo"]["status"] = "INACTIVE"
    elif change == "country":
        own["ocsInfo"]["location"]["countryCode"] = "NL"
    elif change == "guid":
        stock_pair.documents["b"]["pageProps"]["properData"]["guid"] = "wrong"
    elif change == "identity":
        own["id"] = "wrong"
    elif change == "scope":
        own["model"] = "218d"
    elif change == "year":
        own["vehicle"]["firstRegistrationDateRaw"] = "2024-07-01"
    elif change == "fuel":
        own["vehicle"]["primaryFuel"]["formatted"] = "Diesel"
    else:
        own["vehicle"]["transmissionType"] = "Schaltgetriebe"
    replace_stock_document(stock_pair, "b")
    before = snapshot(stock_pair.db)
    result = bridge_de_publications(stock_pair.db)
    assert result["merged"] == 0 and len(result["conflicts"]) == 1
    assert snapshot(stock_pair.db) == before


def test_prices_and_mileage_do_not_participate_in_stock_identity(stock_pair):
    own = stock_own(stock_pair, "b")
    own["price"]["value"]["raw"] = 50000
    own["vehicle"]["mileageInKmRaw"] = 4000
    replace_stock_document(stock_pair, "b")
    assert bridge_de_publications(stock_pair.db)["merged"] == 1


def test_opaque_crossReferenceId_is_never_a_stock_fallback(stock_pair):
    db = stock_pair.db
    row = db.conn.execute(
        "SELECT id,details_json FROM observations WHERE listing_id=?", (stock_pair.listings["b"],)
    ).fetchone()
    details = json.loads(row["details_json"])
    details["raw"]["identifier"] = {"crossReferenceId": "BmwHo-6772775"}
    db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?", (json.dumps(details), row["id"])
    )
    db.conn.commit()
    before = snapshot(db)
    assert bridge_de_publications(db) == {"verified_pairs": 0, "merged": 0, "conflicts": []}
    assert snapshot(db) == before


def test_ambiguous_stock_three_publications_are_reported_without_any_root_move(stock_pair):
    db = stock_pair.db
    item = copy.deepcopy(stock_pair.items["a"])
    item.external_id = "11111111-2222-4333-8444-555555555555"
    item.url = item.url.replace(NORMAL_AS_DETAIL["id"], item.external_id)
    item.raw.pop("publication_reference")
    run = db.start_run(timestamp=NORMAL_STAMP)
    db.observe(run, item, NORMAL_STAMP)
    reject_unchanged(stock_pair, "ambiguous_AS_dealer_stock_publications")


def test_stock_VIN_conflict_preserves_all_aliases_and_observations(stock_pair):
    for key, vin in [("a", "WBA12345678901234"), ("b", "WBA12345678901235")]:
        stock_pair.db.conn.execute(
            "UPDATE vehicles SET vin=? WHERE id=?", (vin, roots(stock_pair)[key])
        )
    stock_pair.db.conn.commit()
    assert reject_unchanged(stock_pair, "conflicting_VIN_identities")["verified_pairs"] == 1


def test_stock_existing_model_conflict_is_not_upgraded_to_target(stock_pair):
    stock_pair.db.conn.execute(
        "UPDATE listings SET scope='model_conflict' WHERE id=?", (stock_pair.listings["a"],)
    )
    stock_pair.db.conn.commit()
    reject_unchanged(stock_pair, "AS_stock_existing_model_conflict")


def test_stock_savepoint_rolls_back_failed_transfer_inside_caller_transaction(stock_pair):
    db = stock_pair.db
    db.conn.execute(
        "CREATE TRIGGER fail_stock_alias BEFORE INSERT ON vehicle_aliases BEGIN SELECT RAISE(ABORT,'stock alias rejected'); END"
    )
    db.conn.commit()
    with db.transaction():
        db.conn.execute("INSERT INTO vehicles VALUES ('pending',NULL,NULL,?)", (NORMAL_STAMP,))
        before = snapshot(db)
        result = bridge_de_publications(db)
        assert result["merged"] == 0 and "stock alias rejected" in result["conflicts"][0]["error"]
        assert db.conn.in_transaction and snapshot(db) == before


def test_plate_root_blocks_merge(paired):
    paired.db.conn.execute(
        "UPDATE vehicles SET kenteken='BAB1234' WHERE id=?", (roots(paired)["b"],)
    )
    paired.db.conn.commit()
    reject_unchanged(paired, "DE_root_has_kenteken")


def test_failed_transfer_uses_savepoint_and_preserves_callers_pending_writes(paired):
    db = paired.db
    db.conn.execute(
        "CREATE TRIGGER fail_DE_alias BEFORE INSERT ON vehicle_aliases BEGIN SELECT RAISE(ABORT,'injected alias failure'); END"
    )
    db.conn.commit()
    with db.transaction():
        db.conn.execute("INSERT INTO vehicles VALUES ('pending',NULL,NULL,?)", (STAMP,))
        before = snapshot(db)
        result = bridge_de_publications(db)
        assert db.conn.in_transaction
        assert result["merged"] == 0 and result["verified_pairs"] == 1
        assert "injected alias failure" in result["conflicts"][0]["error"]
        assert snapshot(db) == before
    assert db.conn.execute("SELECT 1 FROM vehicles WHERE id='pending'").fetchone()


def test_successful_bridge_does_not_commit_caller_transaction(paired):
    before = snapshot(paired.db)
    paired.db.conn.execute("BEGIN")
    assert bridge_de_publications(paired.db)["merged"] == 1
    assert paired.db.conn.in_transaction
    paired.db.conn.rollback()
    assert snapshot(paired.db) == before


def test_no_fuzzy_or_stock_only_merge(paired):
    db = paired.db
    row = db.conn.execute(
        "SELECT id,details_json FROM observations WHERE listing_id=?", (paired.listings["a"],)
    ).fetchone()
    details = json.loads(row["details_json"])
    details["raw"].pop("publication_reference")
    details["raw"]["dealer_stock"] = "same-stock"
    db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?", (json.dumps(details), row["id"])
    )
    db.conn.commit()
    before = snapshot(db)
    assert bridge_de_publications(db) == {"verified_pairs": 0, "merged": 0, "conflicts": []}
    assert snapshot(db) == before
    assert not paired.calls


def add_reverse_reference(state):
    reference = {
        "source": state.items["a"].source,
        "url": state.items["a"].url,
        "external_id": state.items["a"].external_id,
        "kind": "explicit_own_publication_link",
    }
    state.outputs[state.bodies["b"]].listings[0].raw["publication_reference"] = reference
    row = state.db.conn.execute(
        "SELECT id,details_json FROM observations WHERE listing_id=?", (state.listings["b"],)
    ).fetchone()
    details = json.loads(row["details_json"])
    details["raw"]["publication_reference"] = reference
    state.db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?", (json.dumps(details), row["id"])
    )
    state.db.conn.commit()


def test_reciprocal_links_produce_one_pair_and_one_alias(paired):
    add_reverse_reference(paired)
    assert bridge_de_publications(paired.db) == {"verified_pairs": 1, "merged": 1, "conflicts": []}
    assert paired.db.conn.execute("SELECT count(*) FROM vehicle_aliases").fetchone()[0] == 1
    assert len(paired.calls) == 2


def test_verified_conflict_with_reciprocal_link_is_reported_once(paired):
    add_reverse_reference(paired)
    paired.db.conn.execute(
        "UPDATE vehicles SET kenteken='BAB1234' WHERE id=?", (roots(paired)["b"],)
    )
    paired.db.conn.commit()
    assert reject_unchanged(paired, "DE_root_has_kenteken")["verified_pairs"] == 1


@pytest.mark.parametrize("country", [None, "NL"])
def test_other_members_of_a_root_cannot_bypass_country_guard(paired, country):
    db = paired.db
    run = db.start_run(timestamp=LATER)
    item = Listing(
        "dealer:dealer.example",
        "stock-c",
        "https://dealer.example/stock/stock-c",
        "mock parser model proof",
        scope="target",
        raw={"market_country": country},
    )
    db.observe(run, item, LATER, vehicle_id_hint=roots(paired)["b"])
    reject_unchanged(paired, "root_member_country_not_DE")


def test_non_DE_member_of_an_existing_root_blocks_transfer(paired):
    db = paired.db
    run = db.start_run(timestamp=LATER)
    item = Listing(
        "autoscout24",
        "NL-contract",
        "https://www.autoscout24.nl/aanbod/NL-contract",
        "mock parser model proof",
        scope="target",
        raw={"market_country": "DE"},
    )
    db.observe(run, item, LATER, vehicle_id_hint=roots(paired)["b"])
    reject_unchanged(paired, "non_DE_source")


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("vin", "partial", "invalid_registry_VIN"),
        ("kenteken", "", "DE_root_has_kenteken"),
    ],
)
def test_malformed_root_identifiers_are_not_silently_normalized(paired, field, value, error):
    paired.db.conn.execute(f"UPDATE vehicles SET {field}=? WHERE id=?", (value, roots(paired)["b"]))
    paired.db.conn.commit()
    reject_unchanged(paired, error)


@pytest.mark.parametrize("side", ["a", "b"])
def test_matching_head_canonical_and_country_filter_are_valid(paired, side):
    replace_body(
        paired,
        side,
        '<head><link rel="canonical" href="'
        + paired.items[side].url
        + '"></head><body>valid-canonical-contract</body>',
    )
    paired.outputs[paired.bodies[side]].search_filters = {"country": "D"}
    assert bridge_de_publications(paired.db)["merged"] == 1


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize(
    "uri",
    [
        "https://user:password@mobile.example/details.html?id=900001",
        "https://mobile.example:444/details.html?id=900001",
        "https://mobile.example/details.html?id=900001#recommended",
        "file:///details.html?id=900001",
    ],
)
def test_malformed_or_non_public_fetch_URI_rejects(paired, side, uri):
    paired.db.conn.execute("UPDATE fetches SET final_url=? WHERE id=?", (uri, paired.fetches[side]))
    paired.db.conn.commit()
    reject_unchanged(paired, "publication_URI")


def test_model_conflict_from_reparsed_payload_is_preserved(paired):
    paired.outputs[paired.bodies["a"]].listings[0].scope = "model_conflict"
    assert bridge_de_publications(paired.db)["merged"] == 1
    assert {row[0] for row in paired.db.conn.execute("SELECT scope FROM listings")} == {
        "model_conflict"
    }


def test_self_reference_is_an_own_URI_binding_not_a_pair_or_conflict(paired):
    db = paired.db
    row = db.conn.execute(
        "SELECT id,details_json FROM observations WHERE listing_id=?", (paired.listings["a"],)
    ).fetchone()
    details = json.loads(row["details_json"])
    details["raw"]["publication_reference"] = {
        "source": paired.items["a"].source,
        "url": paired.items["a"].url,
        "external_id": paired.items["a"].external_id,
        "kind": "explicit_own_publication_link",
    }
    db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?", (json.dumps(details), row["id"])
    )
    db.conn.commit()
    before = snapshot(db)
    assert bridge_de_publications(db) == {"verified_pairs": 0, "merged": 0, "conflicts": []}
    assert snapshot(db) == before
    assert not paired.calls


def test_target_own_self_reference_does_not_block_incoming_dealer_link(paired):
    state = paired
    reference = {
        "source": state.items["b"].source,
        "url": state.items["b"].url,
        "external_id": state.items["b"].external_id,
        "kind": "explicit_own_publication_link",
    }
    state.outputs[state.bodies["b"]].listings[0].raw["publication_reference"] = reference
    row = state.db.conn.execute(
        "SELECT id,details_json FROM observations WHERE listing_id=?", (state.listings["b"],)
    ).fetchone()
    details = json.loads(row["details_json"])
    details["raw"]["publication_reference"] = reference
    state.db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?", (json.dumps(details), row["id"])
    )
    state.db.conn.commit()
    assert bridge_de_publications(state.db) == {"verified_pairs": 1, "merged": 1, "conflicts": []}


def test_symlink_loop_is_a_pair_conflict_without_mutation(paired):
    row = paired.db.conn.execute(
        "SELECT raw_path FROM fetches WHERE id=?", (paired.fetches["b"],)
    ).fetchone()
    path = paired.db.path.parent / row[0]
    path.unlink()
    path.symlink_to(path.name)
    reject_unchanged(paired, "raw_path_escape_or_missing")


def test_failed_pair_does_not_rollback_an_independent_valid_pair(paired):
    state, db = paired, paired.db
    a_root, b_root = roots(state)["a"], roots(state)["b"]
    ref = {
        "source": "mobile_de",
        "url": "https://mobile.example/details.html?id=900002",
        "external_id": "900002",
        "kind": "explicit_own_publication_link",
    }
    extra = []
    for source, identity, uri, raw in (
        (
            "dealer:dealer.example",
            "stock-c",
            "https://dealer.example/stock/stock-c",
            {"market_country": "DE", "publication_reference": ref},
        ),
        ("mobile_de", "900002", ref["url"], {"market_country": "DE"}),
    ):
        item = Listing(source, identity, uri, "mock parser model proof", scope="target", raw=raw)
        body = "<head></head><body>independent-contract-" + identity + "</body>"
        run = db.start_run(timestamp=LATER)
        fetch = db.save_fetch(run, source, uri, uri, 200, body, LATER)
        state.outputs[body] = Page([copy.deepcopy(item)])
        extra.append(db.observe(run, item, LATER, fetch))
    db.conn.execute(
        "CREATE TRIGGER reject_first_pair BEFORE INSERT ON vehicle_aliases WHEN NEW.alias_id='"
        + b_root
        + "' BEGIN SELECT RAISE(ABORT,'first pair rejected'); END"
    )
    db.conn.commit()
    result = bridge_de_publications(db)
    assert result["verified_pairs"] == 2 and result["merged"] == 1, result
    assert len(result["conflicts"]) == 1
    assert "first pair rejected" in result["conflicts"][0]["error"]
    assert roots(state) == {"a": a_root, "b": b_root}
    assert (
        len(
            {
                row[0]
                for row in db.conn.execute(
                    "SELECT vehicle_id FROM listings WHERE id IN (?,?)", extra
                )
            }
        )
        == 1
    )
    assert not db.conn.execute("PRAGMA foreign_key_check").fetchall()
