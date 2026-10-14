"""Independent conflicts survive current Autokopen model uncertainty."""

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.autokopen_scope import qualify_search
from car_market.collect import Collector, parse_market_page
from car_market.db import Database
from car_market.fetch import Response
from car_market.model_profiles import BMW_PROFILE, bind_profile
from car_market.parse import Listing, Page, json_documents, walk

ROOT = Path(__file__).parent / "fixtures/autokopen_native_scope"
META = json.loads((ROOT / "manifest.json").read_text())[0]
BODY = (ROOT / META["fixture"]).read_text()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("This retained-native/synthetic registry test is offline")

    monkeypatch.setattr("socket.socket.connect", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)


@pytest.mark.parametrize("registered_model", ["218D GRAN TOURER", "218I ACTIVE TOURER"])
@pytest.mark.parametrize("previously_queued", [False, True])
def test_final_bound_rdw_conflict_and_matching_rdw_are_not_overwritten(
    tmp_path, registered_model, previously_queued
):
    db = Database(tmp_path / "market.sqlite3")
    config = {
        "scope": {"country": "NL", "model_profile": BMW_PROFILE},
        "detail_limit": 0,
    }
    bind_profile(db, config)
    worker = Collector(db, config)
    clock = datetime.fromisoformat(META["fetched_at"])
    early = (clock - timedelta(minutes=1)).isoformat()
    uri = META["selected_public_facts"]["url"]
    run = db.start_run("synthetic_prior_publication", early)
    seed = Listing(
        "autokopen",
        META["external_id"],
        uri,
        "BMW 218i Active Tourer",
        price=14950,
        year=2017,
        fuel="petrol",
        kenteken="AA123B",
        scope="target",
        raw={"detail_complete": True},
    )
    fetch = db.save_fetch(
        run, "autokopen", uri, uri, 200, "synthetic prior own detail", early
    )
    listing_id = worker._observe(run, seed, early, fetch)
    if previously_queued:
        worker.queue_details(copy.deepcopy(seed), listing_id)
    old = dict(
        db.conn.execute(
            "SELECT * FROM observations WHERE listing_id=?", (listing_id,)
        ).fetchone()
    )
    root = db.conn.execute(
        "SELECT vehicle_id FROM listings WHERE id=?", (listing_id,)
    ).fetchone()[0]
    # Exact-plate official-source-shaped proof is explicitly synthetic, not this live car.
    rdw = {"kenteken": "AA123B", "merk": "BMW", "handelsbenaming": registered_model}
    rdw_uri = "https://opendata.rdw.nl/resource/m9d7-ebf2.json?kenteken=AA123B"
    rdw_fetch = db.save_fetch(
        run, "rdw", rdw_uri, rdw_uri, 200, json.dumps([rdw]), early
    )
    db.conn.execute(
        "INSERT INTO vehicle_checks(vehicle_id,provider,checked_at,data_json,fetch_id) VALUES (?,?,?,?,?)",
        (root, "rdw", early, json.dumps(rdw), rdw_fetch),
    )
    db.conn.commit()
    second = db.start_run("unchanged_current_native_projection", clock.isoformat())
    page, count = worker.ingest_response(
        second,
        "autokopen",
        "same_own_ID",
        Response(META["url"], META["final_url"], 200, BODY, META["fetched_at"]),
    )
    expected = (
        "model_conflict"
        if registered_model == "218D GRAN TOURER"
        else "needs_confirmation"
    )
    current = db.conn.execute(
        "SELECT * FROM listings WHERE id=?", (listing_id,)
    ).fetchone()
    latest = db.conn.execute(
        "SELECT * FROM latest_observations WHERE listing_id=?", (listing_id,)
    ).fetchone()
    assert current["scope"] == latest["scope"] == page.listings[0].scope == expected
    assert latest["observed_at"] == META["fetched_at"] and current["vehicle_id"] == root
    assert count == (0 if expected == "model_conflict" else 1)
    assert len(worker.detail_jobs) == (0 if expected == "model_conflict" else 1)
    assert (
        dict(
            db.conn.execute(
                "SELECT * FROM observations WHERE id=?", (old["id"],)
            ).fetchone()
        )
        == old
    )
    assert db.conn.execute("SELECT checked_at,fetch_id FROM vehicle_checks").fetchone()[
        :
    ] == (
        early,
        rdw_fetch,
    )
    db.conn.close()


def derived(updates, *, remove_dom_fuel=False):
    soup = BeautifulSoup(BODY, "html.parser")
    envelope = next(
        node
        for document in json_documents(BODY)
        for node in walk(document)
        if isinstance(node.get("initialResults"), list)
    )
    envelope["initialResults"][0].update(updates)
    soup.find("script").string = (
        "self.__next_f.push("
        + json.dumps([1, "0:" + json.dumps(envelope) + "\n"])
        + ")"
    )
    if remove_dom_fuel:
        for text in list(soup.select_one(".vehicle-card").find_all(string=True)):
            text.replace_with(str(text).replace("Benzine", ""))
    return str(soup)


@pytest.mark.parametrize("fuel_slug", ["diesel", "electric", "hybrid"])
def test_explicit_fuel_slug_excludes_when_native_and_dom_labels_are_missing(
    tmp_path, fuel_slug
):
    text = derived({"fuel": None, "fuelSlug": fuel_slug}, remove_dom_fuel=True)
    item = parse_market_page("autokopen", text, META["final_url"]).listings[0]
    assert item.fuel is None and item.scope == "excluded"
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    worker.ingest_response(
        db.start_run(),
        "autokopen",
        "fuel_slug",
        Response(META["url"], META["final_url"], 200, text, META["fetched_at"]),
    )
    assert not worker.detail_jobs
    db.conn.close()


@pytest.mark.parametrize(
    "claims",
    [
        {"country_code": "DE", "countryCode": "NL"},
        {"countryCode": "NL", "country": "BE"},
    ],
)
def test_root_country_aliases_do_not_erase_a_contrary_owned_claim(claims):
    item = parse_market_page("autokopen", derived(claims), META["final_url"]).listings[
        0
    ]
    assert (
        item.scope == "excluded" and "country" in item.raw["nl_publication_conflicts"]
    )


@pytest.mark.parametrize(
    "scope", ["model_conflict", "country_conflict", "identity_conflict"]
)
def test_search_uncertainty_does_not_override_an_independent_conflict_scope(scope):
    item = parse_market_page("autokopen", BODY, META["final_url"]).listings[0]
    item.scope = scope
    assert qualify_search(item).scope == scope


@pytest.mark.parametrize("scope", ["target", "needs_confirmation", "excluded"])
def test_declared_configured_profile_is_not_requalified_as_legacy_bmw(scope):
    item = parse_market_page("autokopen", BODY, META["final_url"]).listings[0]
    item.scope = scope
    item.raw["model_profile"] = "configured_vehicle"
    assert qualify_search(item).scope == scope


@pytest.mark.parametrize("field", ["model", "fuel", "year"])
def test_independent_field_conflict_never_gains_a_new_pending_promotion(field):
    item = parse_market_page("autokopen", BODY, META["final_url"]).listings[0]
    item.scope = "excluded"
    item.raw["field_conflicts"] = {field: ["controlled", "contradictory"]}
    assert qualify_search(item).scope == "excluded"


def test_final_independent_field_conflict_cannot_enter_ordinary_detail_queue(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    item = parse_market_page("autokopen", BODY, META["final_url"]).listings[0]
    item.raw["field_conflicts"] = {"model": ["controlled", "contradictory"]}
    monkeypatch.setattr(worker, "_parse", lambda *args: Page([copy.deepcopy(item)]))
    page, count = worker.ingest_response(
        db.start_run(),
        "autokopen",
        "field_conflict",
        Response(META["url"], META["final_url"], 200, BODY, META["fetched_at"]),
    )
    assert not worker.detail_jobs and count == 0
    assert page.listings[0].raw["field_conflicts"]
    db.conn.close()


def test_unknown_non_bmw_profile_never_gains_a_legacy_pending_override():
    item = parse_market_page("autokopen", BODY, META["final_url"]).listings[0]
    item.scope = "excluded"
    item.raw["model_profile"] = "controlled_other_profile"
    assert qualify_search(item).scope == "excluded"
