"""Observed native PRICE ranges and a controlled source-coverage mutation."""

import copy
import json
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, unquote

import pytest
from car_market.collect import Collector
from car_market.db import Database
from car_market.germany_bmw import bmw_price_partition_url, validate_bmw_price_request
from car_market.germany_bmw_collect import collect_bmw_price_partitions

FILES = Path(__file__).parent / "fixtures/germany_bmw_partitions"
ORIGINS = {x["fixture"]: x for x in json.loads((FILES / "manifest.json").read_text())}


@pytest.mark.parametrize(
    "name,low,high,count",
    [
        ("low", None, 20000, 126),
        ("mid", 20000, 25000, 292),
        ("mid2", 25000, 27500, 230),
        ("mid3", 27500, 30000, 318),
        ("high", 30000, None, 394),
    ],
)
def test_observed_native_price_ranges_are_applied(name, low, high, count):
    doc = json.loads((FILES / (name + ".json")).read_text())
    origin = ORIGINS[name + ".json"]
    assert doc["bmw_native_inventory"]["metadata"]["totalCount"] == count
    assert validate_bmw_price_request(origin["request_body"], low, high)
    parsed = parse_qs(bmw_price_partition_url(origin["ui_url"], low, high).split("?", 1)[1])
    filters = json.loads(unquote(parsed["filters"][0]))
    assert filters["PRICE"] == [low, high]
    assert filters["MARKETING_MODEL_RANGE"] == ["2_U06"]
    assert filters["IS_INSTALLMENT"] is False


@pytest.mark.parametrize("change", ["ignored", "monthly", "currency"])
def test_native_filter_mutations_reject(change):
    body = copy.deepcopy(ORIGINS["low.json"]["request_body"])
    if change == "ignored":
        body["searchContext"][0].pop("price")
    if change == "monthly":
        body["isMonthlyInstallmentFilterSelected"] = True
    if change == "currency":
        body["searchContext"][0]["price"][0]["maxValue"]["currency"] = "USD"
    with pytest.raises(ValueError):
        validate_bmw_price_request(body, None, 20000)


@pytest.mark.parametrize("defect", [None, "ignored", "foreign"])
def test_union_requires_native_echo_and_country_and_retains_raw(tmp_path, monkeypatch, defect):
    import car_market.germany_bmw_collect as module

    original = ORIGINS["low.json"]
    base = json.loads((FILES / "low.json").read_text())
    base["bmw_native_inventory"]["hits"] = base["bmw_native_inventory"]["hits"][:2]
    base["bmw_native_inventory"]["metadata"]["totalCount"] = 2
    calls = []

    @contextmanager
    def session(request):
        filters = json.loads(unquote(parse_qs(request["ui_url"].split("?", 1)[1])["filters"][0]))
        bounds = filters.get("PRICE", [None, None])
        calls.append(bounds)
        doc = copy.deepcopy(base)
        body = copy.deepcopy(original["request_body"])
        body["searchContext"][0].pop("price", None)
        if bounds != [None, None]:
            native = {}
            if bounds[0] is not None:
                native["minValue"] = {"amount": bounds[0], "currency": "EUR"}
            if bounds[1] is not None:
                native["maxValue"] = {"amount": bounds[1], "currency": "EUR"}
            body["searchContext"][0]["price"] = [native]
            if defect == "ignored":
                body["searchContext"][0].pop("price")
            if bounds[0] is not None:
                doc["bmw_native_inventory"].update(hits=[], metadata={"totalCount": 0})
        if defect == "foreign":
            for hit in doc["bmw_native_inventory"]["hits"]:
                hit["vehicle"]["country"] = "NL"
        native = json.dumps(doc["bmw_native_inventory"])
        yield (
            object(),
            {
                "status": 200,
                "body": json.dumps(doc),
                "native_body": native,
                "final_url": original["api_url"],
                "request_body": body,
            },
        )

    monkeypatch.setattr(module, "bmw_ui_session", session)
    db = Database(tmp_path / "market.sqlite3")
    collector = Collector(db, {"scope": {"country": "DE"}})
    root = bmw_price_partition_url(original["ui_url"])
    result = collect_bmw_price_partitions(
        collector,
        db.start_run(),
        {"source": "bmw_de", "key": "controlled", "url": root, "price_partition_cuts": [20000]},
    )
    assert db.conn.execute("select count(*) from fetches").fetchone()[0] > 0
    if defect:
        assert result["status"] == "partial"
        if defect == "foreign":
            assert db.conn.execute("select count(*) from listings").fetchone()[0] == 0
    else:
        assert result["status"] == "complete" and result["received"] == 2
        assert len(result["ids"]) == len(set(result["ids"])) == 2
        assert calls == [[None, None], [None, 20000], [20000, None], [None, None]]
    assert not db.conn.execute("pragma foreign_key_check").fetchall()
