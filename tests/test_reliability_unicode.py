"""A native truncated emoji must not drop its publication or the rest of its page."""

import hashlib
import json
from pathlib import Path

import pytest

from car_market.configured_collect import ConfiguredCollector
from car_market.db import Database, now
from car_market.fetch import Response
from car_market.portable import emit, main
from car_market.provenance import value_hash
from car_market.text_encoding import json_text, scalar_text

ROOT = Path(__file__).parent / "fixtures/reliability_nl"


def test_native_search_keeps_every_card_and_original_malformed_field(tmp_path, capsys):
    name = "marktplaats-corolla-unicode.json"
    metadata = json.loads((ROOT / "provenance.json").read_text())[name]
    body = (ROOT / name).read_text()
    assert hashlib.sha256(body.encode()).hexdigest() == metadata["sha256"]
    native = json.loads(body)
    url = metadata["original"]["final_url"]
    assert main(["--workspace", str(tmp_path), "init", "--make", "Toyota", "--model", "Corolla", "--url", url]) == 0
    capsys.readouterr()
    cfg = json.loads((tmp_path / "search.json").read_text())
    db = Database(tmp_path / "data/market.sqlite3")
    try:
        collector = ConfiguredCollector(db, cfg)
        run = db.start_run("native_unicode_replay")
        page, _ = collector.ingest_response(run, "marktplaats", "search_1",
            Response(url, url, 200, body, now()), queue=False)
        expected = {r["itemId"] for r in native["listings"]}
        assert {r.external_id for r in page.listings} == expected
        assert {r[0] for r in db.conn.execute("SELECT external_id FROM listings")} == expected
        fault = next(r for r in native["listings"] if "categorySpecificDescription" in r)
        row = db.conn.execute("SELECT o.details_json,o.content_hash FROM observations o "
            "JOIN listings l ON l.id=o.listing_id WHERE l.external_id=?", (fault["itemId"],)).fetchone()
        details = json.loads(row[0])
        assert details["raw"]["categorySpecificDescription"] == fault["categorySpecificDescription"]
        assert details["description"] == fault["categorySpecificDescription"]
        assert row[1] == hashlib.sha256(row[0].encode()).hexdigest()
        assert db.conn.execute("SELECT content_hash FROM fetches").fetchone()[0] == metadata["sha256"]
        field = db.conn.execute("SELECT value_hash FROM field_verifications v JOIN listings l "
            "ON l.id=v.listing_id WHERE l.external_id=? AND v.field='description'", (fault["itemId"],)).fetchone()
        assert field[0] == value_hash(fault["categorySpecificDescription"])
        emit(details)
        assert json.loads(capsys.readouterr().out)["description"] == details["description"]
        db.export(tmp_path / "exports")
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.conn.close()


@pytest.mark.parametrize("value", ["Toyota 🚗", "€ 20 000", {"title": "Toyota 🚗"}])
def test_well_formed_unicode_keeps_existing_hash_and_encoding(value):
    assert json_text(value, sort_keys=True) == json.dumps(value, sort_keys=True, ensure_ascii=False)


def test_display_repair_does_not_change_original_evidence():
    value = "Toyota \ud83d"
    assert scalar_text(value) == "Toyota �"
    assert json.loads(json_text(value)) == value


def test_malformed_title_and_seller_are_safe_display_columns(tmp_path):
    cfg = {
        "scope": {"country": "NL", "model_profile": "configured_vehicle",
                  "selector": {"make": "Toyota", "model_terms": [["Corolla"]]}},
        "queries": [],
    }
    from car_market.parse import Listing
    db = Database(tmp_path / "synthetic.sqlite3")
    try:
        collector = ConfiguredCollector(db, cfg)
        item = Listing("marktplaats", "m1", "https://www.marktplaats.nl/v/auto-s/toyota/m1",
            "Toyota Corolla \ud83d", seller="Seller \ud83d", city="City \ud83d", raw={"country_code": "NL"})
        run = db.start_run("synthetic_unicode_fault")
        collector._observe(run, item, now())
        row = db.conn.execute("SELECT title,seller,city,details_json FROM observations").fetchone()
        assert tuple(row[:3]) == ("Toyota Corolla �", "Seller �", "City �")
        assert json.loads(row[3])["title"] == item.title
    finally:
        db.conn.close()
