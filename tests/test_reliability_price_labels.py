"""Unknown units cannot become a false claim of either EUR or a foreign currency."""

import pytest

from car_market.germany import classify_german_price
from car_market.parse import Listing


@pytest.mark.parametrize("currency,warning", [("UNKNOWN", "currency_unconfirmed"), ("CONFLICT", "currency_conflict")])
def test_unknown_currency_is_not_labeled_as_proven_foreign_currency(currency, warning):
    item = Listing("autoscout24_de", "one", "https://example.test/one", "Toyota Corolla", price=20000, raw={"price_currency": currency})
    classify_german_price(item)
    assert item.price is None
    assert item.price_type == "currency_unconfirmed"
    assert item.raw["advertised_amount"] == 20000
    assert item.raw["price_warning"] == warning


def test_proven_foreign_currency_keeps_the_explicit_foreign_label():
    item = Listing("autoscout24_de", "one", "https://example.test/one", "Toyota Corolla", price=20000, raw={"price_currency": "CHF"})
    classify_german_price(item)
    assert item.price is None
    assert item.price_type == "foreign_currency"
    assert item.raw["price_currency"] == "CHF"
