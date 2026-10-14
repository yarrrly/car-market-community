"""Native live fields retained during the first-use review, plus route controls."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from car_market.mercedes_autoscout import parse_mercedes_autoscout
from car_market.portable import main
from car_market.configuration import bind, preflight
from car_market.db import Database
from car_market.search_profile import GENERIC_PROFILE, search_context


def fixture():
    return json.loads(
        (
            Path(__file__).parent / "fixtures/configured_autoscout/smyle-corolla.json"
        ).read_text()
    )


def parse(payload):
    scope = {
        "model_profile": GENERIC_PROFILE,
        "country": "DE",
        "selector": {
            "make": "Toyota",
            "model_terms": [["Corolla"]],
            "fuels": ["hybrid"],
        },
    }
    with search_context(scope):
        return parse_mercedes_autoscout(
            json.dumps(payload), payload["_fixture_provenance"]["source_url"], "DE"
        )


def test_native_smyle_corolla_uses_own_fields_and_primary_additional_fuels():
    page = parse(fixture())
    assert len(page.listings) == 1
    candidate = page.listings[0]
    assert candidate.scope == "target"
    assert (candidate.price, candidate.year, candidate.mileage) == (28790, 2023, 67654)
    assert (candidate.fuel, candidate.transmission) == ("hybrid", "automatic")
    assert candidate.raw["market_country"] == "DE"
    assert (
        candidate.raw["purchase_price_path"] == "properData.carDetails.price.value.raw"
    )
    assert candidate.kenteken is None and candidate.vin is None
    assert candidate.raw["price_currency"] == "EUR"


@pytest.mark.parametrize("mutation", ["guid", "id", "available", "status"])
def test_smyle_rejects_route_substitution_or_unconfirmed_own_record(mutation):
    payload = fixture()
    state = payload["props"]["pageProps"]
    if mutation in {"guid", "id"}:
        target = (
            state["properData"]
            if mutation == "guid"
            else state["properData"]["carDetails"]
        )
        target[mutation] = "11111111-2222-4333-8444-555555555555"
    elif mutation == "available":
        state["listingAvailable"] = False
    else:
        state["properData"]["carDetails"]["ocsInfo"]["status"] = "INACTIVE"
    assert not parse(payload).listings


@pytest.mark.parametrize("component", ["primary", "additional"])
def test_smyle_unknown_fuel_component_cannot_be_substituted_from_title(component):
    payload = fixture()
    vehicle = payload["props"]["pageProps"]["properData"]["carDetails"]["vehicle"]
    if component == "primary":
        vehicle["primaryFuel"]["formatted"] = "unknown"
    else:
        vehicle["additionalFuel"][0]["value"]["formatted"] = "unknown"
    candidate = parse(payload).listings[0]
    assert candidate.fuel is None and candidate.scope == "needs_confirmation"


def test_smyle_foreign_own_country_is_retained_as_negative_evidence():
    payload = fixture()
    payload["props"]["pageProps"]["properData"]["carDetails"]["ocsInfo"]["location"][
        "countryCode"
    ] = "NL"
    candidate = parse(payload).listings[0]
    assert candidate.scope == "excluded"
    assert "foreign_stock_country" in candidate.raw["model_scope_evidence"]["reasons"]


def test_known_invalid_autoscout_country_url_fails_before_collection(tmp_path, capsys):
    args = [
        "--workspace",
        str(tmp_path / "bad"),
        "init",
        "--make",
        "Toyota",
        "--model",
        "Corolla",
        "--country",
        "DE",
        "--url",
        "https://www.autoscout24.de/lst/toyota/corolla?cy=DE",
    ]
    assert main(args) == 2
    assert "cy=D" in capsys.readouterr().err
    assert not (tmp_path / "bad/search.json").exists()


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="Unix non-root permissions required",
)
@pytest.mark.parametrize(
    "name",
    ["daemon.lock", "market.sqlite3.lock", "market.sqlite3-wal", "market.sqlite3-shm"],
)
def test_existing_unwritable_lock_or_sqlite_sidecar_is_not_healthy(
    tmp_path, capsys, name
):
    root = tmp_path / "workspace"
    base = ["--workspace", str(root)]
    assert main(base + ["init", "--make", "Toyota", "--model", "Corolla"]) == 0
    path = root / "data" / name
    path.parent.mkdir(exist_ok=True)
    path.touch()
    path.chmod(0o444)
    capsys.readouterr()
    assert main(base + ["doctor"]) == 5
    assert json.loads(capsys.readouterr().out)["write_permissions"][name] is False
    path.chmod(0o644)


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="Unix non-root permissions required",
)
def test_standalone_wal_mode_backup_in_readonly_directory_is_diagnosable(
    tmp_path, capsys
):
    root = tmp_path / "workspace"
    base = ["--workspace", str(root)]
    assert main(base + ["init", "--make", "Toyota", "--model", "Corolla"]) == 0
    c = json.loads((root / "search.json").read_text())
    path = root / "data/market.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(tmp_path / "live.sqlite3")
    bind(db, c)
    with closing(sqlite3.connect(path)) as snapshot:
        db.conn.backup(snapshot)
    db.conn.close()
    assert not Path(str(path) + "-wal").exists()
    path.parent.chmod(0o555)
    try:
        capsys.readouterr()
        preflight(path, c)
        reader = Database.read_only(path)
        assert reader.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        reader.conn.close()
        assert main(base + ["doctor"]) == 5
        result = json.loads(capsys.readouterr().out)
        assert (
            result["database"] == "ok" and result["write_permissions"]["data"] is False
        )
        assert not Path(str(path) + "-wal").exists()
        assert not Path(str(path) + "-shm").exists()
    finally:
        path.parent.chmod(0o755)


def test_readonly_reader_keeps_committed_live_wal_records(tmp_path):
    path = tmp_path / "live.sqlite3"
    db = Database(path)
    db.conn.execute("PRAGMA wal_autocheckpoint=0")
    db.conn.execute("CREATE TABLE reader_probe (value TEXT)")
    db.conn.commit()
    db.conn.execute("INSERT INTO reader_probe VALUES ('committed-in-wal')")
    db.conn.commit()
    assert Path(str(path) + "-wal").stat().st_size > 0
    reader = Database.read_only(path)
    try:
        assert (
            reader.conn.execute("SELECT value FROM reader_probe").fetchone()[0]
            == "committed-in-wal"
        )
    finally:
        reader.conn.close()
        db.conn.close()
