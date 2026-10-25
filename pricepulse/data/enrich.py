from __future__ import annotations

from decimal import Decimal
from typing import Any

from pricepulse.data.normalize import PriceField
from pricepulse.domain.money import MoneyAmount


def price_delta(current: PriceField, previous: PriceField) -> Decimal | None:
    if current.state != "present" or previous.state != "present":
        return None
    left = MoneyAmount(current.amount or Decimal("0"), current.currency or "", "sale")
    right = MoneyAmount(previous.amount or Decimal("0"), previous.currency or "", "sale")
    return left.delta(right)


def compute_alert_delta(current: PriceField, previous: PriceField, threshold: Decimal) -> dict[str, Any] | None:
    delta = price_delta(current, previous)
    if delta is None or abs(delta) < abs(threshold):
        return None
    return {
        "delta_amount": str(delta),
        "currency": current.currency,
        "direction": "increase" if delta > 0 else "decrease",
    }
