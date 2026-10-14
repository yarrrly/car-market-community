"""Original own NL facts remain explicit across dedup, exclusions and identity holds."""

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
from car_market.opportunities import inventory
from car_market.parse import walk

ROOT = Path(__file__).parent / "fixtures/nl_publication_proof"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
AS = next(m for m in MANIFEST if m["id"] == 267194)
API = next(m for m in MANIFEST if m["id"] == 267841)
ID = "e8fbe6bc-57ac-4b12-9864-96c0d92c3620"
OTHER = "b80c76f4-a868-42cd-89af-442b32a071ee"


def body(meta):
    raw = (ROOT / meta["fixture"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == meta["fixture_sha256"]
    return raw.decode()


def derived(meta, edit):
    if meta["source"] == "autofirst":
        doc = json.loads(body(meta))
        edit(doc["data"][0], doc)
        return json.dumps(doc)
    soup = BeautifulSoup(body(meta), "html.parser")
    script = soup.select_one("script#__NEXT_DATA__")
    doc = json.loads(script.get_text())
    own = next(i for i in walk(doc) if i.get("id") == ID and isinstance(i.get("vehicle"), dict))
    edit(own, doc)
    script.string = json.dumps(doc)
    return str(soup)


def snapshot(db, source, identity):
    listing = db.conn.execute(
        "SELECT * FROM listings WHERE source=? AND external_id=?", (source, identity)
    ).fetchone()
    if not listing:
        return None
    observation = db.conn.execute(
        "SELECT * FROM latest_observations WHERE listing_id=?", (listing["id"],)
    ).fetchone()
    return dict(listing), dict(observation), json.loads(observation["details_json"])


def history(db):
    return {
        table: [tuple(r) for r in db.conn.execute("SELECT * FROM " + table + " ORDER BY id")]
        for table in ["fetches", "observations", "events", "vehicles"]
    }


def ingest(worker, meta, text, minutes=0):
    stamp = (datetime.fromisoformat(meta["fetched_at"]) + timedelta(minutes=minutes)).isoformat()
    run = worker.db.start_run(timestamp=stamp)
    worker.ingest_response(
        run,
        meta["source"],
        "native_projection",
        Response(
            meta["url"], meta["final_url"], 200, text, stamp, transport="native_projection_offline"
        ),
        queue=False,
    )
    return run, stamp


@pytest.mark.parametrize("meta", MANIFEST, ids=lambda m: str(m["id"]))
def test_native_projection_preserves_original_public_identity_and_price_facts(meta):
    assert meta["url"].startswith("https://") and meta["status"] == 200
    assert len(meta["content_hash"]) == len(meta["gzip_sha256"]) == 64
    assert "original_path" not in meta
    page = parse_market_page(meta["source"], body(meta), meta["final_url"])
    for expected in meta["selected_typed_publications"]:
        item = next(x for x in page.listings if x.external_id == expected["external_id"])
        assert {key: getattr(item, key) for key in expected} == expected


@pytest.mark.parametrize("fault", ["country", "structured_model", "visible_model", "URI", "sold"])
def test_new_bound_negative_facts_replace_eligibility_without_rebinding_history(tmp_path, fault):
    meta = API if fault == "sold" else AS
    identity = "104475" if fault == "sold" else ID
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}, "detail_limit": 0})
    ingest(worker, meta, body(meta))
    old = snapshot(db, meta["source"], identity)
    assert old and old[0]["scope"] == "target"
    past = history(db)

    def edit(own, doc):
        if fault == "country":
            own["location"]["countryCode"] = "BE"
        elif fault == "structured_model":
            own["vehicle"]["model"] = "220"
        elif fault == "visible_model":
            own["vehicle"]["motorTypeName"] = "220i"
        elif fault == "URI":
            own["url"] = next(
                i["url"]
                for i in walk(doc)
                if i.get("id") == OTHER and isinstance(i.get("vehicle"), dict)
            )
        else:
            own["is_sold"] = True

    changed = derived(meta, edit)
    run, stamp = ingest(worker, meta, changed, 1)
    current = snapshot(db, meta["source"], identity)
    assert current[0]["scope"] == "excluded" and current[0]["vehicle_id"] == old[0]["vehicle_id"]
    assert current[1]["id"] != old[1]["id"] and current[1]["observed_at"] == stamp
    assert not any(
        c["vehicle_id"] == old[0]["vehicle_id"] for c in inventory(db.conn, worker.config)
    )
    fetch = db.conn.execute(
        "SELECT * FROM fetches WHERE id=?", (current[1]["fetch_id"],)
    ).fetchone()
    assert fetch["run_id"] == run and fetch["fetched_at"] == stamp
    assert (
        hashlib.sha256(
            gzip.decompress((db.path.parent / fetch["raw_path"]).read_bytes())
        ).hexdigest()
        == hashlib.sha256(changed.encode()).hexdigest()
    )
    if fault == "country":
        assert current[2]["raw"]["location"]["countryCode"] == "BE"
    elif fault == "structured_model":
        assert current[2]["raw"]["vehicle"]["model"] == "220"
    elif fault == "sold":
        assert current[2]["raw"]["is_sold"] is True
    elif fault == "URI":
        assert current[0]["url"] == old[0]["url"] == current[2]["url"]
        assert (
            current[2]["raw"]["nl_publication_conflicts"]["identity"]["native_uri"]
            != old[2]["raw"]["url"]
        )
    # This is a publication exclusion; it does not infer the physical car's sale.
    assert current[0]["availability"] == "observed"
    for table, rows in past.items():
        for row in rows:
            assert (
                tuple(
                    db.conn.execute("SELECT * FROM " + table + " WHERE id=?", (row[0],)).fetchone()
                )
                == row
            )
    db.conn.close()


def test_exact_unchanged_body_keeps_observation_and_uses_original_archive(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    ingest(worker, AS, body(AS))
    old = snapshot(db, "autoscout24", ID)
    ingest(worker, AS, body(AS), 1)
    current = snapshot(db, "autoscout24", ID)
    assert current[1]["id"] == old[1]["id"]
    fetch = db.conn.execute("SELECT * FROM fetches ORDER BY id DESC LIMIT 1").fetchone()
    assert fetch["raw_path"] is None
    assert (db.raw / (fetch["content_hash"] + ".html.gz")).exists()
    assert current[0]["scope"] == "target"
    db.conn.close()


def test_database_comparison_preserves_unmapped_own_native_vehicle_facts(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    ingest(worker, AS, body(AS))
    old = snapshot(db, "autoscout24", ID)
    changed = derived(
        AS,
        lambda own, doc: own["vehicle"].__setitem__(
            "native_binding_revision", "controlled-unmapped-native-fact"
        ),
    )
    # Every original market scalar is unchanged; DB semantic projection still differs.
    _run, stamp = ingest(worker, AS, changed, 1)
    current = snapshot(db, "autoscout24", ID)
    assert current[0]["scope"] == "target" and current[1]["id"] != old[1]["id"]
    assert (
        current[2]["raw"]["vehicle"]["native_binding_revision"] == "controlled-unmapped-native-fact"
    )
    assert current[1]["observed_at"] == stamp
    db.conn.close()


def test_conclusive_exclusion_needs_its_exact_successful_capture_clock(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    ingest(worker, AS, body(AS))
    old = snapshot(db, "autoscout24", ID)
    negative = next(
        x
        for x in worker._parse("autoscout24", body(AS), AS["final_url"]).listings
        if x.external_id == ID
    )
    negative.scope = "excluded"
    run = db.start_run()
    clock = (datetime.fromisoformat(AS["fetched_at"]) + timedelta(minutes=1)).isoformat()
    fetch = db.save_fetch(run, "autoscout24", AS["url"], AS["final_url"], 200, body(AS), clock)
    for missing_proof in [None, fetch]:
        # Mismatched capture time is not a direct native exclusion.
        assert db.observe(run, copy.deepcopy(negative), AS["fetched_at"], missing_proof) is None
    assert snapshot(db, "autoscout24", ID)[0]["scope"] == "target"
    assert snapshot(db, "autoscout24", ID)[1]["id"] == old[1]["id"]
    db.conn.close()


def test_older_negative_capture_cannot_displace_newer_positive_proof(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    ingest(worker, AS, body(AS), 2)
    old = snapshot(db, "autoscout24", ID)
    changed = derived(AS, lambda own, doc: own["location"].__setitem__("countryCode", "BE"))
    ingest(worker, AS, changed, 1)
    current = snapshot(db, "autoscout24", ID)
    assert current[0]["scope"] == "target" and current[0]["last_seen"] == old[0]["last_seen"]
    assert current[1]["id"] == old[1]["id"]
    db.conn.close()


def test_new_positive_owned_record_clears_prior_exclusion(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}})
    ingest(worker, AS, body(AS))
    old = snapshot(db, "autoscout24", ID)
    changed = derived(AS, lambda own, doc: own["location"].__setitem__("countryCode", "BE"))
    ingest(worker, AS, changed, 1)
    assert snapshot(db, "autoscout24", ID)[0]["scope"] == "excluded"
    ingest(worker, AS, body(AS), 2)
    current = snapshot(db, "autoscout24", ID)
    assert current[0]["scope"] == "target" and current[0]["vehicle_id"] == old[0]["vehicle_id"]
    assert current[2]["raw"]["location"]["countryCode"] == "NL"
    assert "nl_publication_conflicts" not in current[2]["raw"]
    db.conn.close()


def test_direct_detail_VIN_conflict_retains_native_bytes_and_old_identity(tmp_path):
    meta = next(m for m in MANIFEST if m["id"] == 267958)
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(
        db,
        {
            "scope": {"country": "NL"},
            "workers": 1,
            "detail_limit": 1,
            "detail_batch_limit": 1,
            "detail_cache_hours": 0,
        },
    )
    ingest(worker, meta, body(meta))
    item = worker._parse(meta["source"], body(meta), meta["final_url"]).listings[0]
    old = snapshot(db, item.source, item.external_id)
    soup = BeautifulSoup(body(meta), "html.parser")
    script = soup.select_one('script[type="application/ld+json"]')
    doc = json.loads(script.get_text())
    doc["vehicleIdentificationNumber"] = "WBA21BX0807M41257"
    script.string = json.dumps(doc)
    changed = str(soup)
    calls = []
    stamp = (datetime.fromisoformat(meta["fetched_at"]) + timedelta(minutes=1)).isoformat()
    worker.fetcher.get = lambda url: (
        calls.append(url) or Response(url, meta["final_url"], 200, changed, stamp)
    )
    worker.detail_jobs[item.url] = (item.source, old[0]["id"], item)
    worker.details(db.start_run())
    current = snapshot(db, item.source, item.external_id)
    fetch = db.conn.execute("SELECT * FROM fetches ORDER BY id DESC LIMIT 1").fetchone()
    assert len(calls) == 1 and current[2]["vin"] == old[2]["vin"] == "WBA21BX0807M41256"
    assert (
        hashlib.sha256(
            gzip.decompress((db.path.parent / fetch["raw_path"]).read_bytes())
        ).hexdigest()
        == hashlib.sha256(changed.encode()).hexdigest()
    )
    db.conn.close()
