from decimal import Decimal

import pytest

from pricepulse.data.enrich import compute_alert_delta, price_delta
from pricepulse.data.normalize import PriceField


def field(amount, currency="RUB", unit_name=None, quantity=Decimal("1")):
    return PriceField("present", Decimal(amount), currency, 2, unit_name, quantity)


def test_delta_only_for_compatible_amounts():
    current = field("110")
    previous = field("100")
    assert price_delta(current, previous) == Decimal("10")

    assert price_delta(current, PriceField.unknown()) is None
    with pytest.raises(ValueError):
        price_delta(field("110"), field("100", currency="USD"))
    with pytest.raises(ValueError):
        price_delta(field("110", unit_name="l", quantity=Decimal("2")), field("100", unit_name="l"))


def test_alert_threshold_and_direction():
    assert compute_alert_delta(field("110"), field("100"), Decimal("10"))["direction"] == "increase"
    assert compute_alert_delta(field("100"), field("110"), Decimal("10"))["direction"] == "decrease"
    assert compute_alert_delta(field("109"), field("100"), Decimal("10")) is None
