"""Missing optional own-PDP URL differs from a contradictory native identity claim."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from car_market.collect import Collector, parse_market_page
from car_market.db import Database
from car_market.fetch import Response
from car_market.nl_publication import qualify_publication

ROOT = Path(__file__).parent / "fixtures/quality_primary"
NAME = "autoscout24-789ada1c-ded9-465d-93ff-f0783b445b2e.html"
ID = "789ada1c-ded9-465d-93ff-f0783b445b2e"
OTHER = "318b8b64-e047-49c8-8971-ff7918cfb69b"
META = json.loads(
    (
        Path(__file__).parent
        / "fixtures/nl_publication_proof/primary-owned-uri-provenance.json"
    ).read_text()
)


def item():
    raw = (ROOT / NAME).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == META["fixture_sha256"]
    own = parse_market_page("autoscout24", raw.decode(), META["url"]).listings[0]
    assert own.external_id == own.raw["id"] == ID and own.scope == "target"
    assert own.raw["location"]["countryCode"] == "NL"
    assert "url" not in own.raw and "webPage" not in own.raw
    return own


@pytest.mark.parametrize(
    "reference",
    ["absent", "original_webPage", "URL_null", "relative_URL", "absolute_URL"],
)
def test_owned_PDP_keeps_optional_native_URL_contract(reference):
    own = item()
    if reference == "original_webPage":
        own.raw["webPage"] = META["original_owned_webPage"]
    elif reference == "URL_null":
        own.raw["url"] = None
    elif reference == "relative_URL":
        own.raw["url"] = "/aanbod/" + ID
    elif reference == "absolute_URL":
        own.raw["url"] = META["url"]
    before = copy.deepcopy(own)
    qualify_publication(own)
    assert own.scope == "target" and "nl_publication_conflicts" not in own.raw
    assert (own.external_id, own.url, own.kenteken, own.price) == (
        before.external_id,
        before.url,
        before.kenteken,
        before.price,
    )
    if reference == "absent":
        assert "url" not in own.raw


@pytest.mark.parametrize(
    "fault",
    [
        "native_ID",
        "native_URL",
        "malformed_URL",
        "native_webPage",
        "parsed_ID",
        "root_route",
        "foreign_host",
    ],
)
def test_optional_URL_never_substitutes_wrong_owned_identity(fault):
    own = item()
    if fault == "native_ID":
        own.raw["id"] = OTHER
    elif fault == "native_URL":
        own.raw["url"] = "/aanbod/" + OTHER
    elif fault == "malformed_URL":
        own.raw["url"] = {"url": own.url}
    elif fault == "native_webPage":
        own.raw["webPage"] = "https://www.autoscout24.nl/aanbod/" + OTHER
    elif fault == "parsed_ID":
        own.url = own.url.replace(ID, OTHER)
    elif fault == "root_route":
        own.url = "https://www.autoscout24.nl/lst/bmw/218?cy=NL"
    else:
        own.url = own.url.replace("www.autoscout24.nl", "www.autoscout24.nl.evil.test")
    qualify_publication(own)
    assert own.scope == "excluded" and "identity" in own.raw["nl_publication_conflicts"]


def test_retained_owned_PDP_survives_actual_legacy_observation_boundary(tmp_path):
    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}, "detail_limit": 0})
    body = (ROOT / NAME).read_text()
    assert worker._parse("autoscout24", body, META["url"]).listings[0].scope == "target"
    run = db.start_run(timestamp=META["fetched_at"])
    worker.ingest_response(
        run,
        "autoscout24",
        "owned_native_PDP",
        Response(META["url"], META["url"], 200, body, META["fetched_at"]),
        queue=False,
    )
    row = db.conn.execute(
        "SELECT l.external_id,l.url,l.scope,o.observed_at,o.details_json FROM listings l JOIN latest_observations o ON o.listing_id=l.id"
    ).fetchone()
    assert (
        row
        and row["external_id"] == ID
        and row["scope"] == "target"
        and row["url"] == META["url"]
    )
    assert row["observed_at"] == META["fetched_at"]
    payload = json.loads(row["details_json"])
    assert "url" not in payload["raw"] and "webPage" not in payload["raw"]
    db.conn.close()


def test_newly_declared_owned_URI_survives_raw_and_observation_dedup(tmp_path):
    from datetime import datetime, timedelta

    from bs4 import BeautifulSoup

    db = Database(tmp_path / "market.sqlite3")
    worker = Collector(db, {"scope": {"country": "NL"}, "detail_limit": 0})
    before = (ROOT / NAME).read_text()
    soup = BeautifulSoup(before, "html.parser")
    node = soup.select_one('script[type="application/json"]')
    doc = json.loads(node.get_text())
    doc["props"]["pageProps"]["listingDetails"]["webPage"] = META[
        "original_owned_webPage"
    ]
    node.string = json.dumps(doc)
    after = str(soup)
    later = (
        datetime.fromisoformat(META["fetched_at"]) + timedelta(minutes=1)
    ).isoformat()
    for body, stamp in [(before, META["fetched_at"]), (after, later)]:
        run = db.start_run(timestamp=stamp)
        worker.ingest_response(
            run,
            "autoscout24",
            "owned_native_PDP",
            Response(META["url"], META["url"], 200, body, stamp),
            queue=False,
        )
    row = db.conn.execute(
        "SELECT l.scope,o.observed_at,o.details_json,f.raw_path FROM listings l JOIN latest_observations o ON o.listing_id=l.id JOIN fetches f ON f.id=o.fetch_id"
    ).fetchone()
    assert row["scope"] == "target" and row["observed_at"] == later and row["raw_path"]
    assert (
        json.loads(row["details_json"])["raw"]["webPage"]
        == META["original_owned_webPage"]
    )
    assert db.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    db.conn.close()
