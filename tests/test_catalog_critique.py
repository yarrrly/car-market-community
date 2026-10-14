"""Independent audit regressions: conflicts, text completeness and conditional prices.

Price source: retained quality_dealers/autokopen-detail-2.html and its
provenance.json. The real term “BETAAL NU 50% 11950...” is deliberately
moved from the title into the description with the amount preserved. The full-price
check combines a real credit template with retained Carteam €8499;
this tests classification, without adding a market record. Short aanbetaling/
maandbedrag cases below are minimal contract variations for the displayed amount.
"""

import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.db import Database
from car_market.parse import Listing
from car_market.provenance import value_hash
from car_market.quality_api_adapters import parse_public_detail
from car_market.quality_dealer_adapters import parse_quality_page
from car_market.sale_terms import classify
from car_market.vehicle_catalog import catalog, get_vehicle

EARLIER = "2026-10-07T15:00:00+00:00"
NOW = "2026-10-07T16:00:00+00:00"
OLD = "2026-10-05T10:00:00+00:00"
CONFIG = {"as_of": NOW}
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "market.sqlite3")
    yield database
    database.conn.close()


def car(source="autoscout24", **fields):
    values = {
        "price": 8000,
        "year": 2016,
        "mileage": 120000,
        "fuel": "petrol",
        "transmission": "manual",
        "kenteken": "AB123C",
    }
    values.update(fields)
    return Listing(
        source, "own", f"https://{source}.test/own", "BMW 218i Active Tourer", **values
    ).qualify()


def observe(db, item, timestamp=NOW, query_key=None):
    return db.observe(db.start_run(timestamp=timestamp), item, timestamp, query_key=query_key)


def card(db, listing_id):
    identity = db.conn.execute(
        "SELECT vehicle_id FROM listings WHERE id=?", (listing_id,)
    ).fetchone()[0]
    return get_vehicle(db.conn, identity, CONFIG)


@pytest.mark.parametrize("field", ["fuel", "transmission", "year", "mileage"])
def test_short_search_keeps_unresolved_conflict_and_original_time(db, field):
    lid = observe(
        db, car(**{field: None}, raw={"field_conflicts": {field: ["one", "two"]}}), EARLIER
    )
    observe(db, car("marktplaats"), EARLIER)
    observe(db, car(price=7900, **{field: None}), NOW, "search")
    result = card(db, lid)
    output_field = "mileage_km" if field == "mileage" else field
    assert result[output_field] is None
    conflict = result["field_conflicts"][output_field][0]
    assert conflict["alternatives"] == ["one", "two"]
    assert conflict["origin"]["observed_at"] == EARLIER
    assert conflict["origin"]["fresh"]
    stored = json.loads(
        db.conn.execute(
            "SELECT details_json FROM latest_observations WHERE listing_id=?", (lid,)
        ).fetchone()[0]
    )
    assert stored["raw"]["field_evidence"][field]["inherited"]


def test_search_does_not_refresh_inherited_conflict_or_block_newer_own_fact(db):
    lid = observe(db, car(fuel=None, raw={"field_conflicts": ["fuel"]}), OLD)
    observe(db, car("marktplaats"), NOW)
    observe(db, car(price=7900, fuel=None), EARLIER, "search")
    observe(db, car(price=7800, fuel=None), NOW, "search")
    result = card(db, lid)
    assert result["fuel"] == "petrol"
    conflict = result["field_conflicts"]["fuel"][0]
    assert conflict["origin"]["observed_at"] == OLD
    assert not conflict["origin"]["fresh"]


def test_own_unambiguous_value_resolves_retained_conflict(db):
    lid = observe(
        db, car(fuel=None, raw={"field_conflicts": {"fuel": ["petrol", "diesel"]}}), EARLIER
    )
    observe(db, car(), NOW, "search")
    result = card(db, lid)
    assert result["fuel"] == "petrol" and result["field_conflicts"]["fuel"] == []
    assert result["field_evidence"]["fuel"][0]["observed_at"] == NOW
    assert not result["field_evidence"]["fuel"][0]["inherited"]


def test_inherited_value_cannot_resolve_current_own_conflict(db):
    lid = observe(db, car(fuel=None, raw={"field_conflicts": ["fuel"]}), EARLIER)
    observe(
        db,
        car(price=7900, raw={"field_evidence": {"fuel": {"observed_at": OLD, "inherited": True}}}),
        NOW,
        "search",
    )
    result = card(db, lid)
    assert result["fuel"] is None and result["field_conflicts"]["fuel"]
    assert (
        db.conn.execute(
            "SELECT fuel FROM latest_observations WHERE listing_id=?", (lid,)
        ).fetchone()[0]
        is None
    )


def test_legacy_conflict_does_not_borrow_lineage_of_prior_good_value(db):
    lid = observe(db, car(fuel=None, raw={"field_conflicts": ["fuel"]}), EARLIER)
    row = db.conn.execute(
        "SELECT id,details_json FROM latest_observations WHERE listing_id=?", (lid,)
    ).fetchone()
    payload = json.loads(row["details_json"])
    payload["raw"]["field_evidence"]["fuel"] = {"observed_at": OLD, "inherited": True}
    db.conn.execute(
        "UPDATE observations SET details_json=? WHERE id=?", (json.dumps(payload), row["id"])
    )
    db.conn.commit()
    observe(db, car(price=7900, fuel=None), NOW, "search")
    assert card(db, lid)["field_conflicts"]["fuel"][0]["origin"]["observed_at"] == EARLIER


def viabovag(body=None):
    directory = FIXTURES / "quality_api"
    name = "viabovag-52fa6e90-54c3-4b25-9c2b-57061a2da815.html"
    url = json.loads((directory / "provenance.json").read_text())[name]["final_url"]
    return parse_public_detail("viabovag", body or (directory / name).read_text(), url)


def test_new_incomplete_real_body_does_not_inherit_previous_completeness(db):
    previous = viabovag()
    assert previous.raw["detail_complete"] is True
    lid = observe(db, previous, EARLIER)
    body = (FIXTURES / "quality_api/viabovag-52fa6e90-54c3-4b25-9c2b-57061a2da815.html").read_text()
    soup = BeautifulSoup(body, "html.parser")
    comments = soup.select_one(
        '[data-testid="vdp-dealer-comments-mobile"] [data-testid="animated-height-wrapper"]'
    )
    comments["data-truncated"] = "true"
    comments.append(" Nieuwe aanvullende informatie waarvan de rest nog niet geladen is.")
    current = viabovag(str(soup))
    assert current.raw["detail_complete"] is None
    observe(db, current)
    result = card(db, lid)
    assert result["description"] == current.description != previous.description
    assert not result["detail_complete"]
    assert not result["description_evidence"][0]["inherited"]
    assert any(
        d["text"] == previous.description and d["detail_complete"] for d in result["descriptions"]
    )


def test_missing_description_keeps_completeness_of_retained_text(db):
    lid = observe(
        db,
        car(description="Complete retained seller text.", raw={"detail_complete": True}),
        EARLIER,
    )
    observe(
        db,
        car(price=7900, raw={"detail_complete": False, "description_truncated": True}),
        NOW,
        "search",
    )
    result = card(db, lid)
    assert result["description"] == "Complete retained seller text." and result["detail_complete"]
    assert not result["description_may_be_truncated"]
    assert result["description_evidence"][0]["observed_at"] == EARLIER


def test_truncation_warning_belongs_to_retained_text(db):
    lid = observe(
        db,
        car(
            description="A seller excerpt without terminal dots",
            raw={"detail_complete": False, "description_truncated": True},
        ),
        EARLIER,
    )
    observe(db, car(price=7900), NOW, "search")
    result = card(db, lid)
    assert result["description_may_be_truncated"] and not result["detail_complete"]
    assert len(result["descriptions"][0]["evidence_ids"]) == 1


def dealer_fixture(source, label="detail-1"):
    directory = FIXTURES / "quality_dealers"
    name = f"{source}-{label}.html"
    url = json.loads((directory / "provenance.json").read_text())[name]["final_url"]
    body = (directory / name).read_text()
    return body, parse_quality_page(source, body, url).listings[0]


def test_real_partial_payment_excerpt_in_description_is_not_retail_price():
    body, item = dealer_fixture("autokopen", "detail-2")
    text = (
        BeautifulSoup(body, "html.parser")
        .find("h1")
        .find_next_sibling("p")
        .get_text(" ", strip=True)
    )
    assert text == "218i ACTIE! BETAAL NU 50% 11950 DE REST IN 2JR RENTEVRIJ LENEN"
    # Move the retained term into the description; the amount comes from the same text.
    item.title = "BMW 2-Serie Active Tourer 218i"
    item.description = text
    item.price = 11950
    item.price_type = "advertised_price"
    item.raw.pop("price_warning", None)
    classify(item)
    assert item.price == 11950 and item.price_type == "conditional_price"
    assert item.raw["price_warning"] == "conditional_or_instalment_price"


def test_real_financing_template_does_not_block_ordinary_full_price():
    body, _item = dealer_fixture("autokopen", "detail-2")
    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text(" ", strip=True)
    start = text.index("Financiering voor deze")
    end = text.index("Je gaat naar Geld.nl", start)
    template = text[start:end]
    assert "zonder aanbetaling" in template and "per maand" in template
    _, full_price_item = dealer_fixture("carteam")
    assert full_price_item.price == 8499
    full_price_item.description += " " + template
    classify(full_price_item)
    assert full_price_item.price_type == "advertised_price"
    assert not full_price_item.raw.get("price_warning")


@pytest.mark.parametrize(
    "description",
    [
        "Dit bedrag is uitsluitend de aanbetaling.",
        "Aanbetaling: € 8.000. De rest volgt later.",
        "Het maandbedrag is € 8.000 per maand.",
    ],
)
def test_explicit_displayed_amount_condition_is_not_a_purchase_candidate(db, description):
    # Minimal contract terms, not real source observations.
    item = car(description=description)
    lid = observe(db, item)
    result = card(db, lid)
    assert not result["offers"][0]["current_price"] and result["price_min_eur"] is None
    assert not db.conn.execute("SELECT 1 FROM purchase_candidates").fetchone()


@pytest.mark.parametrize(
    "first_source,second_source", [("autoscout24", "marktplaats"), ("marktplaats", "autoscout24")]
)
def test_same_time_different_mileages_are_unknown_regardless_of_source(
    db, first_source, second_source
):
    lid = observe(db, car(first_source, mileage=120000))
    observe(db, car(second_source, mileage=70000))
    result = card(db, lid)
    assert result["mileage_km"] is None and result["field_evidence"]["mileage_km"] == []
    assert {s["value"] for s in result["field_alternatives"]["mileage_km"]} == {70000, 120000}
    assert {s["value"] for s in result["field_conflicts"]["mileage_km"][0]["statements"]} == {
        70000,
        120000,
    }


def test_later_mileage_resolves_earlier_tie_and_keeps_alternatives(db):
    lid = observe(db, car(mileage=120000), EARLIER)
    observe(db, car("marktplaats", mileage=70000), EARLIER)
    observe(db, car(mileage=71000), NOW)
    result = card(db, lid)
    assert result["mileage_km"] == 71000 and result["field_conflicts"]["mileage_km"] == []
    assert {s["value"] for s in result["field_alternatives"]["mileage_km"]} == {
        70000,
        71000,
        120000,
    }


def test_equal_mileage_at_same_time_is_not_a_conflict(db):
    lid = observe(db, car())
    observe(db, car("marktplaats"))
    result = card(db, lid)
    assert result["mileage_km"] == 120000 and result["field_conflicts"]["mileage_km"] == []
    assert len(result["field_evidence"]["mileage_km"]) == 2


def verification(db, listing_id, field, value, verified_at=NOW, *, evidence=None, fetch_id=None):
    origin = {"observed_at": verified_at, "fetch_id": fetch_id, "inherited": False}
    origin.update(evidence or {})
    db.conn.execute(
        "INSERT OR REPLACE INTO field_verifications"
        "(listing_id,field,verified_at,fetch_id,value_hash,evidence_json) VALUES (?,?,?,?,?,?)",
        (listing_id, field, verified_at, fetch_id, value_hash(value), json.dumps(origin)),
    )
    db.conn.commit()


def test_reconfirmed_unchanged_price_has_fresh_proof_without_new_observation(db):
    first = car()
    run = db.start_run(timestamp=OLD)
    old_fetch = db.save_fetch(
        run, first.source, first.url, first.url, 200, "offline", OLD, retain_raw=False
    )
    lid = db.observe(run, first, OLD, old_fetch, query_key="search")
    latest = car()
    run = db.start_run(timestamp=NOW)
    new_fetch = db.save_fetch(
        run, latest.source, latest.url, latest.url, 200, "offline", NOW, retain_raw=False
    )
    db.observe(run, latest, NOW, new_fetch, query_key="search")
    assert (
        db.conn.execute("SELECT COUNT(*) FROM observations WHERE listing_id=?", (lid,)).fetchone()[
            0
        ]
        == 1
    )
    result = card(db, lid)
    assert result["price_min_eur"] == 8000
    origin = result["offers"][0]["price_evidence"]
    assert origin["observed_at"] == NOW and origin["fetch_id"] == new_fetch
    assert origin["fetched_at"] == NOW and origin["fetch_url"] == latest.url
    assert origin["fresh"] and not origin["inherited"]


@pytest.mark.parametrize(
    "field,value,output", [("price", 8000.0, "price"), ("mileage", 120000.0, "mileage_km")]
)
def test_numeric_hash_normalization_and_original_identity_lineage(db, field, value, output):
    lid = observe(db, car(), OLD)
    verification(
        db,
        lid,
        field,
        value,
        evidence={
            "source": "autoscout24",
            "external_id": "own",
            "url": "https://other.test/",
            "listing_id": lid,
        },
    )
    result = card(db, lid)
    origin = (
        result["offers"][0]["price_evidence"]
        if output == "price"
        else result["field_evidence"][output][0]
    )
    assert origin["observed_at"] == NOW
    assert origin["source"] == "autoscout24" and origin["external_id"] == "own"
    assert origin["url"] == "https://autoscout24.test/own" and origin["listing_id"] == lid


def test_verification_for_old_mileage_cannot_refresh_history_or_changed_current_value(db):
    lid = observe(db, car(mileage=120000), OLD)
    observe(db, car(mileage=70000), EARLIER)
    verification(db, lid, "mileage", 120000)
    result = card(db, lid)
    assert result["mileage_km"] == 70000
    old = next(s for s in result["field_alternatives"]["mileage_km"] if s["value"] == 120000)
    assert old["origin"]["observed_at"] == OLD and not old["origin"]["fresh"]
    assert result["field_evidence"]["mileage_km"][0]["observed_at"] == EARLIER


@pytest.mark.parametrize("matches_conflict", [True, False])
def test_conflict_flag_must_match_before_overlay(db, matches_conflict):
    lid = observe(db, car(fuel=None, raw={"field_conflicts": ["fuel"]}), EARLIER)
    verification(
        db,
        lid,
        "fuel",
        None,
        evidence={"conflict": matches_conflict, "conflict_hash": value_hash(None)},
    )
    origin = card(db, lid)["field_conflicts"]["fuel"][0]["origin"]
    assert origin["observed_at"] == (NOW if matches_conflict else EARLIER)


def test_null_value_verification_cannot_refresh_different_conflict_alternatives(db):
    lid = observe(db, car(fuel=None, raw={"field_conflicts": {"fuel": ["petrol", "diesel"]}}), OLD)
    verification(
        db,
        lid,
        "fuel",
        None,
        evidence={"conflict": True, "conflict_hash": value_hash(["petrol", "hybrid"])},
    )
    origin = card(db, lid)["field_conflicts"]["fuel"][0]["origin"]
    assert origin["observed_at"] == OLD and not origin["fresh"]


def test_legacy_unbound_conflict_proof_does_not_refresh_unknown_alternatives(db):
    lid = observe(db, car(fuel=None, raw={"field_conflicts": ["fuel"]}), OLD)
    verification(db, lid, "fuel", None, evidence={"conflict": True})
    assert card(db, lid)["field_conflicts"]["fuel"][0]["origin"]["observed_at"] == OLD


def test_changed_conflict_keeps_current_alternatives_and_own_proof(db):
    lid = observe(db, car(fuel=None, raw={"field_conflicts": {"fuel": ["petrol", "diesel"]}}), OLD)
    current = ["petrol", "hybrid"]
    observe(db, car(fuel=None, raw={"field_conflicts": {"fuel": current}}), EARLIER)
    observe(db, car(fuel=None, raw={"field_conflicts": {"fuel": current}}), NOW)
    result = card(db, lid)
    assert result["fuel"] is None
    assert len(result["field_conflicts"]["fuel"]) == 1
    conflict = result["field_conflicts"]["fuel"][0]
    assert conflict["alternatives"] == current and conflict["origin"]["observed_at"] == NOW


@pytest.mark.parametrize(
    "verified_at,evidence",
    [
        ("bad-time", {}),
        ("2026-10-07T16:00:00", {}),
        ("2026-10-08T16:00:00+00:00", {}),
        (NOW, {"observed_at": "bad-time"}),
        (OLD, {"observed_at": NOW}),
        (NOW, {"inherited": True}),
        (NOW, {"legacy_origin_time_unverified": True}),
    ],
)
def test_invalid_or_inherited_verification_does_not_make_price_fresh(db, verified_at, evidence):
    lid = observe(db, car(), OLD)
    db.conn.execute("UPDATE listings SET last_seen=? WHERE id=?", (NOW, lid))
    verification(db, lid, "price", 8000, verified_at, evidence=evidence)
    result = card(db, lid)
    assert result["price_min_eur"] is None
    assert result["offers"][0]["price_evidence"]["observed_at"] == OLD


def test_maximum_valid_verification_uses_real_time_not_lexical_offset_order(db):
    lid = observe(db, car(), OLD)
    # A separate legacy table without uniqueness verifies selection of the latest date.
    db.conn.executescript(
        "DROP TABLE field_verifications; CREATE TABLE field_verifications("
        "listing_id INTEGER,field TEXT,verified_at TEXT,fetch_id INTEGER,value_hash TEXT,evidence_json TEXT);"
    )
    verification(db, lid, "price", 8000, NOW)
    verification(db, lid, "price", 8000, "2026-10-07T17:00:00+02:00")
    verification(db, lid, "price", 8000, "2026-10-08T16:00:00+00:00")
    assert card(db, lid)["offers"][0]["price_evidence"]["observed_at"] == NOW


def test_optional_verification_table_keeps_old_connection_read_only(db):
    lid = observe(db, car(), NOW)
    db.conn.execute("DROP TABLE field_verifications")
    db.conn.commit()
    db.conn.execute("PRAGMA query_only=ON")
    changes = db.conn.total_changes
    assert card(db, lid)["price_min_eur"] == 8000
    assert db.conn.total_changes == changes


def test_verification_overlay_fetches_are_bulk_and_five_queries_remain_read_only(db):
    fetch_id = db.conn.execute(
        "INSERT INTO fetches(source,url,fetched_at,status) VALUES ('proof','https://proof.test/',?,200)",
        (NOW,),
    ).lastrowid
    for n in range(30):
        lid = observe(db, car(f"offline{n}", kenteken=None), OLD)
        db.conn.execute("UPDATE listings SET last_seen=? WHERE id=?", (NOW, lid))
        verification(db, lid, "price", 8000, fetch_id=fetch_id)
    db.conn.commit()
    db.conn.execute("PRAGMA query_only=ON")
    changes, factory, transaction = (
        db.conn.total_changes,
        db.conn.row_factory,
        db.conn.in_transaction,
    )
    queries = []
    db.conn.set_trace_callback(queries.append)
    try:
        results = catalog(db.conn, CONFIG)
    finally:
        db.conn.set_trace_callback(None)
    assert len(results) == 30 and all(c["price_min_eur"] == 8000 for c in results)
    assert all(c["offers"][0]["price_evidence"]["fetched_at"] == NOW for c in results)
    assert len(queries) == 5 and all(q.lstrip().upper().startswith("SELECT") for q in queries)
    assert db.conn.total_changes == changes and db.conn.row_factory is factory
    assert db.conn.in_transaction == transaction


def test_rc01_new_short_own_correction_precedes_long_historical_description(db):
    original = "Motor werkt goed. Onderhoud volledig aantoonbaar, alle facturen aanwezig en auto kan direct probleemloos gebruikt worden."
    correction = "Motor defect. Auto niet rijdbaar."
    lid = observe(db, car(description=original, raw={"detail_complete": True}), EARLIER)
    observe(db, car(description=correction, raw={"detail_complete": True}), NOW)
    result = card(db, lid)
    assert result["description"] == correction
    assert result["description_evidence"][0]["observed_at"] == NOW
    historical = next(d for d in result["descriptions"] if d["text"] == original)
    assert all(
        result["media_evidence"][ref]["observed_at"] == EARLIER
        for ref in historical["evidence_ids"]
    )


@pytest.mark.parametrize("old_state", ["removed", "stale", "old_origin_fresh_presence"])
def test_live_newer_inherited_description_precedes_removed_or_stale_own_text(db, old_state):
    old_text = "Old long seller description with reassuring statements that must remain only as historical evidence."
    live_text = "Current live seller correction."
    old_time = EARLIER if old_state == "removed" else OLD
    lid = observe(db, car(description=old_text, raw={"detail_complete": True}), old_time)
    if old_state == "removed":
        db.conn.execute("UPDATE listings SET availability='removed' WHERE id=?", (lid,))
    elif old_state == "old_origin_fresh_presence":
        db.conn.execute("UPDATE listings SET last_seen=? WHERE id=?", (NOW, lid))
    db.conn.commit()
    origin_time = "2026-10-07T15:30:00+00:00"
    observe(
        db,
        car(
            "marktplaats",
            description=live_text,
            raw={
                "detail_complete": True,
                "field_evidence": {
                    "description": {
                        "source": "marktplaats",
                        "observed_at": origin_time,
                        "inherited": True,
                    }
                },
            },
        ),
        NOW,
    )
    result = card(db, lid)
    assert result["description"] == live_text
    origin = result["description_evidence"][0]
    assert origin["active"] and origin["fresh"] and origin["inherited"]
    assert origin["observed_at"] == origin_time
    historical = next(d for d in result["descriptions"] if d["text"] == old_text)
    assert all(
        result["media_evidence"][ref]["observed_at"] == old_time
        for ref in historical["evidence_ids"]
    )


def test_rc02_same_real_text_new_incomplete_assessment_replaces_own_old_complete(db):
    initial = viabovag()
    lid = observe(db, initial, EARLIER)
    body = (FIXTURES / "quality_api/viabovag-52fa6e90-54c3-4b25-9c2b-57061a2da815.html").read_text()
    soup = BeautifulSoup(body, "html.parser")
    block = soup.select_one(
        '[data-testid="vdp-dealer-comments-mobile"] [data-testid="animated-height-wrapper"]'
    )
    block["data-truncated"] = "true"
    current = viabovag(str(soup))
    assert current.description == initial.description and current.raw["detail_complete"] is None
    observe(db, current, NOW)
    result = card(db, lid)
    assert result["description"] == initial.description and not result["detail_complete"]
    assert len(result["descriptions"]) == 1
    assert {o["detail_complete"] for o in result["description_evidence"]} == {True, False}
    assert {o["observed_at"] for o in result["description_evidence"]} == {EARLIER, NOW}


@pytest.mark.parametrize(
    "proof",
    [
        {"source": "other"},
        {"source": None},
        {"listing_id": 999},
        {"listing_id": "1"},
        {"listing_id": True},
        {"external_id": "unrelated"},
        {"external_id": 1},
        {"inherited": 0},
        {"conflict": "false"},
        {"legacy_origin_time_unverified": 0},
    ],
)
def test_rc03_foreign_or_untyped_proof_cannot_refresh_price(db, proof):
    from car_market.opportunities import inventory

    lid = observe(db, car(), OLD)
    db.conn.execute("UPDATE listings SET last_seen=? WHERE id=?", (NOW, lid))
    verification(db, lid, "price", 8000, evidence=proof)
    result = card(db, lid)
    assert result["price_min_eur"] is None
    assert result["offers"][0]["price_evidence"]["observed_at"] == OLD
    assert not inventory(db.conn, CONFIG)


@pytest.mark.parametrize("direct_confirmation", [False, True])
def test_rc04_legacy_origin_only_becomes_current_with_valid_own_confirmation(
    db, direct_confirmation
):
    from car_market.opportunities import inventory

    lid = observe(
        db,
        car(
            raw={
                "field_evidence": {
                    "price": {
                        "observed_at": NOW,
                        "inherited": True,
                        "legacy_origin_time_unverified": True,
                    }
                }
            }
        ),
        NOW,
    )
    if direct_confirmation:
        verification(
            db,
            lid,
            "price",
            8000,
            evidence={"source": "autoscout24", "listing_id": lid, "external_id": "own"},
        )
    result = card(db, lid)
    origin = result["offers"][0]["price_evidence"]
    assert origin["fresh"] is direct_confirmation
    assert result["offers"][0]["current_price"] is direct_confirmation
    assert result["price_min_eur"] == (8000 if direct_confirmation else None)
    assert bool(inventory(db.conn, CONFIG)) is direct_confirmation
