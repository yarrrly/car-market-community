"""Owned Autokopen model uncertainty reaches bounded details without promotion."""

import copy
import gzip
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from car_market.collect import Collector, parse_market_page
from car_market.db import Database
from car_market.fetch import Response
from car_market.model_profiles import BMW_PROFILE, MERCEDES_PROFILE, use_profile
from car_market.parse import Listing, json_documents, walk

ROOT = Path(__file__).parent / "fixtures/autokopen_native_scope"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(
            "Offline native scope controls must never contact a provider"
        )

    monkeypatch.setattr("socket.socket.connect", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)


def body(meta):
    raw = (ROOT / meta["fixture"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == meta["projected_body_sha256"]
    return raw.decode()


def derive(meta, edit, *, title=None, variant=None):
    soup = BeautifulSoup(body(meta), "html.parser")
    envelope = next(
        node
        for doc in json_documents(str(soup))
        for node in walk(doc)
        if "initialResults" in node
    )
    edit(envelope["initialResults"][0])
    if title is not None:
        soup.select_one("h3").string = title
    if variant is not None:
        soup.select_one("h3 + p").string = variant
    soup.find("script").string = (
        "self.__next_f.push("
        + json.dumps([1, "0:" + json.dumps(envelope) + "\n"])
        + ")"
    )
    return str(soup)


def ingest(worker, meta, text=None, *, minutes=0):
    stamp = (
        datetime.fromisoformat(meta["fetched_at"]) + timedelta(minutes=minutes)
    ).isoformat()
    run = worker.db.start_run(timestamp=stamp)
    page, count = worker.ingest_response(
        run,
        "autokopen",
        "native_scope",
        Response(
            meta["url"],
            meta["final_url"],
            200,
            text or body(meta),
            stamp,
            transport="offline_native_projection",
        ),
    )
    return run, stamp, page, count


@pytest.mark.parametrize("meta", MANIFEST, ids=lambda m: m["external_id"])
def test_native_incomplete_owned_models_are_pending_not_targets(meta):
    page = parse_market_page("autokopen", body(meta), meta["final_url"])
    assert len(page.listings) == 1
    item = page.listings[0]
    assert item.external_id == meta["external_id"]
    for field, value in meta["selected_public_facts"].items():
        if field != "scope":
            assert getattr(item, field) == value
    assert item.scope == "needs_confirmation"
    assert "original_path" not in meta
    assert (
        len(meta["original_content_sha256"]) == len(meta["original_gzip_sha256"]) == 64
    )


@pytest.mark.parametrize("meta", MANIFEST, ids=lambda m: m["external_id"])
def test_pending_native_publication_is_observed_and_queued_with_its_capture(
    tmp_path, meta
):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}, "detail_limit": 0})
    run, stamp, page, count = ingest(worker, meta)
    row = db.conn.execute("SELECT * FROM listings").fetchone()
    assert (
        row["external_id"] == meta["external_id"]
        and row["scope"] == "needs_confirmation"
    )
    assert count == 1 and len(page.listings) == 1
    assert list(worker.detail_jobs) == [row["url"]]
    observation = db.conn.execute("SELECT * FROM observations").fetchone()
    capture = db.conn.execute(
        "SELECT * FROM fetches WHERE id=?", (observation["fetch_id"],)
    ).fetchone()
    assert (
        observation["observed_at"]
        == capture["fetched_at"]
        == stamp
        == meta["fetched_at"]
    )
    assert capture["content_hash"] == meta["projected_body_sha256"]
    worker.details(run)
    assert db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 1
    assert worker.detail_stats["autokopen"]["attempted"] == 0
    db.conn.close()


@pytest.mark.parametrize(
    "engine", ["318i", "320i", "228i", "218d", "218xe", "218e", "216d", "225xe", "999i"]
)
def test_explicit_other_engine_is_excluded_before_detail_planning(tmp_path, engine):
    meta = MANIFEST[2]
    text = derive(meta, lambda own: own.update(variant=engine))
    assert (
        parse_market_page("autokopen", text, meta["final_url"]).listings[0].scope
        == "excluded"
    )
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    ingest(worker, meta, text)
    assert not worker.detail_jobs
    db.conn.close()


@pytest.mark.parametrize(
    "style",
    [
        "Coupe",
        "Coupé",
        "Cabrio",
        "Cabriolet",
        "Gran Tourer",
        "Gran Coupe",
        "Sedan",
        "Touring",
    ],
)
def test_explicit_other_body_is_excluded_even_with_218i(style):
    meta = MANIFEST[0]
    text = derive(meta, lambda own: own.update(model=style))
    assert (
        parse_market_page("autokopen", text, meta["final_url"]).listings[0].scope
        == "excluded"
    )


@pytest.mark.parametrize("style", ["Hatchback", "MPV"])
def test_generic_native_body_taxonomy_does_not_prove_a_contrary_body(style):
    meta = MANIFEST[-1]
    text = derive(meta, lambda own: own.update(body_type=style))
    assert (
        parse_market_page("autokopen", text, meta["final_url"]).listings[0].scope
        == "needs_confirmation"
    )


def test_owned_bmw_without_any_current_required_model_part_is_only_pending():
    meta = MANIFEST[-1]
    text = derive(
        meta,
        lambda own: own.update(title="BMW", model=None, variant=None),
        title="BMW",
        variant="",
    )
    assert (
        parse_market_page("autokopen", text, meta["final_url"]).listings[0].scope
        == "needs_confirmation"
    )


@pytest.mark.parametrize("engine", ["318i", "320i", "228i", "218xe"])
def test_explicit_other_engine_in_own_visible_title_cannot_become_pending(engine):
    meta = MANIFEST[-1]
    text = derive(meta, lambda own: None, title="BMW " + engine)
    assert (
        parse_market_page("autokopen", text, meta["final_url"]).listings[0].scope
        == "excluded"
    )


@pytest.mark.parametrize(
    "contrary", ["Motor: 318i", "Motor: 220i", "Carrosserie: Sedan"]
)
def test_explicit_contrary_detail_within_own_dom_card_is_not_hidden_by_incomplete_heading(
    contrary,
):
    meta = MANIFEST[-1]
    soup = BeautifulSoup(body(meta), "html.parser")
    field = soup.new_tag("p")
    field.string = contrary
    soup.select_one(".vehicle-card").append(field)
    assert (
        parse_market_page("autokopen", str(soup), meta["final_url"]).listings[0].scope
        == "excluded"
    )


def test_explicit_foreign_and_nl_country_claims_are_not_treated_as_missing():
    meta = MANIFEST[-1]
    text = derive(
        meta,
        lambda own: own.update(market_country="NL", location={"countryCode": "DE"}),
    )
    assert (
        parse_market_page("autokopen", text, meta["final_url"]).listings[0].scope
        == "excluded"
    )


def test_explicit_nl_country_does_not_establish_missing_model_or_new_country_facts():
    meta = MANIFEST[-1]
    text = derive(meta, lambda own: own.update(location={"countryCode": "NL"}))
    item = parse_market_page("autokopen", text, meta["final_url"]).listings[0]
    assert item.scope == "needs_confirmation" and "market_country" not in item.raw


@pytest.mark.parametrize(
    "fault",
    [
        "brand",
        "diesel",
        "hybrid",
        "electric",
        "foreign_location",
        "foreign_country",
        "wrong_slug_ID",
        "foreign_URI",
        "noncar",
    ],
)
def test_owned_contradictions_remain_excluded_and_unqueued(tmp_path, fault):
    meta = MANIFEST[0]

    def edit(own):
        if fault == "brand":
            own["brand"] = "Audi"
        elif fault in {"diesel", "hybrid", "electric"}:
            own["fuel"] = {
                "diesel": "Diesel",
                "hybrid": "Hybride",
                "electric": "Elektrisch",
            }[fault]
        elif fault == "foreign_location":
            own["location"] = {"countryCode": "DE"}
        elif fault == "foreign_country":
            own["country_code"] = "BE"
        elif fault == "wrong_slug_ID":
            own["slug"] = own["slug"].replace(meta["external_id"], "12877110")
        elif fault == "noncar":
            own["vehicle_type"] = "motorcycle"

    text = derive(meta, edit)
    if fault == "foreign_URI":
        soup = BeautifulSoup(text, "html.parser")
        soup.select_one('a[href*="/auto/detail/"]')["href"] = (
            "https://unrelated.invalid/auto/detail/bmw-2017-" + meta["external_id"]
        )
        text = str(soup)
    assert (
        parse_market_page("autokopen", text, meta["final_url"]).listings[0].scope
        == "excluded"
    )
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    ingest(worker, meta, text)
    assert not worker.detail_jobs
    db.conn.close()


@pytest.mark.parametrize(
    "fault",
    [
        "no_native_record",
        "duplicate_native_record",
        "wrong_native_ID",
        "missing_brand",
        "malformed_brand",
    ],
)
def test_pending_rule_never_uses_untyped_or_ambiguous_fallback_cards(fault):
    meta = MANIFEST[-1]
    soup = BeautifulSoup(body(meta), "html.parser")
    if fault == "no_native_record":
        soup.find("script").decompose()
    else:
        envelope = next(
            node
            for doc in json_documents(str(soup))
            for node in walk(doc)
            if "initialResults" in node
        )
        own = envelope["initialResults"][0]
        if fault == "duplicate_native_record":
            envelope["initialResults"].append(copy.deepcopy(own))
        elif fault == "wrong_native_ID":
            own["id"] = "12877110"
        elif fault == "missing_brand":
            own.pop("brand")
        else:
            own["brand"] = ["BMW"]
        soup.find("script").string = (
            "self.__next_f.push("
            + json.dumps([1, "0:" + json.dumps(envelope) + "\n"])
            + ")"
        )
    assert (
        parse_market_page("autokopen", str(soup), meta["final_url"]).listings[0].scope
        == "excluded"
    )


def test_current_uncertainty_is_reapplied_after_old_target_enrichment(tmp_path):
    meta = MANIFEST[0]
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    complete = derive(
        meta,
        lambda own: own.update(
            model="2-Serie Active Tourer", title="BMW 2-Serie Active Tourer"
        ),
        title="BMW 2-Serie Active Tourer",
    )
    ingest(worker, meta, complete, minutes=-1)
    old = dict(db.conn.execute("SELECT * FROM observations").fetchone())
    assert old["scope"] == "target"
    worker.detail_jobs.clear()
    _run, stamp, _page, _count = ingest(worker, meta)
    latest = db.conn.execute("SELECT * FROM latest_observations").fetchone()
    assert latest["scope"] == "needs_confirmation"
    assert latest["observed_at"] == stamp
    assert (
        dict(
            db.conn.execute(
                "SELECT * FROM observations WHERE id=?", (old["id"],)
            ).fetchone()
        )
        == old
    )
    assert len(worker.detail_jobs) == 1
    db.conn.close()


def test_pending_details_use_existing_shared_limit_and_require_owned_positive_body(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(
        db, {"scope": {"country": "NL"}, "workers": 1, "detail_limit": 1}
    )
    for meta in MANIFEST[:2]:
        run, _stamp, _page, _count = ingest(worker, meta)
    assert len(worker.detail_jobs) == 2
    calls = []

    def controlled_get(url):
        calls.append(url)
        # Controlled positive own-PDP schema: not a claim of another native capture.
        own = {
            "@type": "Car",
            "@id": url,
            "sku": url.rsplit("-", 1)[-1],
            "name": "BMW 2-Serie Active Tourer",
            "vehicleConfiguration": "218i",
            "fuelType": "Benzine",
            "offers": {"price": 16950},
        }
        text = '<script type="application/ld+json">' + json.dumps(own) + "</script>"
        stamp = (
            datetime.fromisoformat(MANIFEST[1]["fetched_at"]) + timedelta(minutes=1)
        ).isoformat()
        return Response(
            url, url, 200, text, stamp, transport="controlled_native_derived"
        )

    monkeypatch.setattr(worker.fetcher, "get", controlled_get)
    worker.details(run)
    assert len(calls) == 1
    assert (
        worker.detail_stats["autokopen"]["attempted"]
        == worker.detail_stats["autokopen"]["parsed"]
        == 1
    )
    assert sorted(row[0] for row in db.conn.execute("SELECT scope FROM listings")) == [
        "needs_confirmation",
        "target",
    ]
    assert db.conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0] == 3
    assert any(failure == "detail_backlog:1" for failure in worker.failures)
    db.conn.close()


def test_structured_only_model_change_is_retained_before_observation_dedup(tmp_path):
    meta = MANIFEST[2]
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    ingest(worker, meta)
    old = dict(db.conn.execute("SELECT * FROM latest_observations").fetchone())
    changed = derive(meta, lambda own: own.update(variant="318i"))
    worker.detail_jobs.clear()
    _run, stamp, _page, _count = ingest(worker, meta, changed, minutes=1)
    latest = db.conn.execute("SELECT * FROM latest_observations").fetchone()
    assert latest["scope"] == "excluded" and latest["id"] != old["id"]
    assert latest["observed_at"] == stamp and not worker.detail_jobs
    capture = db.conn.execute(
        "SELECT * FROM fetches ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert capture["raw_path"] is not None
    assert (
        hashlib.sha256(
            gzip.decompress((db.path.parent / capture["raw_path"]).read_bytes())
        ).hexdigest()
        == capture["content_hash"]
    )
    assert (
        dict(
            db.conn.execute(
                "SELECT * FROM observations WHERE id=?", (old["id"],)
            ).fetchone()
        )
        == old
    )
    db.conn.close()


def test_another_record_never_supplies_the_missing_own_model():
    meta = MANIFEST[-1]
    soup = BeautifulSoup(body(meta), "html.parser")
    envelope = next(
        node
        for doc in json_documents(str(soup))
        for node in walk(doc)
        if "initialResults" in node
    )
    other = copy.deepcopy(envelope["initialResults"][0])
    other.update(
        id="12877110",
        slug="bmw-218i-active-tourer-2020-12877110",
        title="BMW 218i Active Tourer",
        variant="218i",
    )
    envelope["initialResults"].append(other)
    soup.find("script").string = (
        "self.__next_f.push("
        + json.dumps([1, "0:" + json.dumps(envelope) + "\n"])
        + ")"
    )
    page = parse_market_page("autokopen", str(soup), meta["final_url"])
    assert len(page.listings) == 1
    assert (
        page.listings[0].external_id == meta["external_id"]
        and page.listings[0].scope == "needs_confirmation"
    )


def test_slug_and_query_target_words_are_not_current_model_proof():
    meta = MANIFEST[-1]
    text = derive(
        meta,
        lambda own: own.update(
            slug="bmw-218i-active-tourer-2020-" + meta["external_id"]
        ),
    )
    soup = BeautifulSoup(text, "html.parser")
    for link in soup.select('a[href*="/auto/detail/"]'):
        link["href"] = "/auto/detail/bmw-218i-active-tourer-2020-" + meta["external_id"]
    assert (
        parse_market_page(
            "autokopen", str(soup), "https://autokopen.nl/auto/bmw/218i-active-tourer"
        )
        .listings[0]
        .scope
        == "needs_confirmation"
    )


def test_complete_owned_positive_stays_target_and_other_providers_keep_existing_guard():
    meta = MANIFEST[0]
    text = derive(
        meta,
        lambda own: own.update(
            model="2-Serie Active Tourer", title="BMW 2-Serie Active Tourer"
        ),
        title="BMW 2-Serie Active Tourer",
    )
    assert (
        parse_market_page("autokopen", text, meta["final_url"]).listings[0].scope
        == "target"
    )
    assert (
        Listing("other", "1", "https://example.invalid/1", "BMW 2-Serie", fuel="petrol")
        .qualify()
        .scope
        == "excluded"
    )


def test_mercedes_and_explicit_german_context_are_not_relaxed():
    meta = MANIFEST[-1]
    with use_profile(MERCEDES_PROFILE, "NL"):
        assert (
            parse_market_page("autokopen", body(meta), meta["final_url"])
            .listings[0]
            .scope
            == "excluded"
        )
    with use_profile(BMW_PROFILE, "NL"):
        item = parse_market_page("autokopen", body(meta), meta["final_url"]).listings[0]
        item.raw["market_context_country"] = "DE"
        item.scope = "excluded"
        from car_market.nl_publication import qualify_publication

        assert qualify_publication(item).scope == "excluded"


@pytest.mark.parametrize("model", ["3-Serie", "X1"])
def test_explicit_other_native_bmw_family_is_not_incomplete_218i_active_tourer(model):
    meta = MANIFEST[-1]
    text = derive(meta, lambda own: own.update(model=model))
    assert (
        parse_market_page("autokopen", text, meta["final_url"]).listings[0].scope
        == "excluded"
    )


@pytest.mark.parametrize("field", ["model", "variant", "fuel"])
def test_pending_shape_does_not_certify_malformed_structured_vehicle_fields(field):
    meta = MANIFEST[-1]
    text = derive(meta, lambda own: own.update({field: {"untyped": "controlled"}}))
    assert (
        parse_market_page("autokopen", text, meta["final_url"]).listings[0].scope
        == "excluded"
    )
