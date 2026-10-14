"""Real contract terms and year contradictions cannot become a vehicle price."""

import json
from pathlib import Path

from car_market.germany import qualify_german_item
from car_market.parse import Listing

CASES = json.loads((Path(__file__).parent / "fixtures/germany_buyer_terms.json").read_text())


def item(index):
    return Listing(**CASES[index]["listing"], raw={"market_country": "DE"})


def test_real_contract_transfer_is_not_cash_car_price():
    car = qualify_german_item(item(0))
    assert car.price == 9060
    assert car.price_type == "lease_transfer"
    assert car.raw["offer_role"] == "lease_transfer"
    assert car.raw["price_warning"] == "lease_transfer"


def test_optional_financing_does_not_replace_own_gross_price():
    car = item(0)
    car.title = "BMW 218i Active Tourer"
    car.description = "Kaufpreis 9.060 EUR. Leasing möglich."
    qualify_german_item(car)
    assert car.price_type == "advertised_price"
    assert "price_warning" not in car.raw


def test_real_year_disagreement_keeps_both_own_claims():
    car = qualify_german_item(item(1))
    assert car.year == 2026
    assert car.raw["field_conflicts"]["year"]["own_description"] == 2016


def test_buyer_view_flags_real_year_disagreement(tmp_path):
    from car_market.collect import Collector
    from car_market.db import Database, now
    from car_market.germany import observe_german_item
    from car_market.opportunities import evaluate

    db = Database(tmp_path / "market.sqlite3")
    config = {"scope": {"country": "DE"}}
    Collector(db, config)
    run = db.start_run()
    observe_german_item(db, run, item(1), now())
    offers = evaluate(db.conn, config)["cars"]
    assert len(offers) == 1
    assert "conflicting_year" in offers[0]["risk_matches"]
