from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal


PRICE_KINDS = Literal["sale", "list", "unit"]


@dataclass(frozen=True, slots=True)
class MoneyAmount:
    amount: Decimal
    currency: str
    kind: PRICE_KINDS
    unit_quantity: Decimal = Decimal("1")
    unit_name: str | None = None

    def __post_init__(self) -> None:
        if not self.currency or len(self.currency) != 3 or self.currency != self.currency.upper():
            raise ValueError("currency must be an uppercase three-letter code")
        if self.amount <= 0:
            raise ValueError("confirmed money amount must be positive")
        if self.unit_quantity <= 0:
            raise ValueError("unit quantity must be positive")
        if self.kind == "unit" and not self.unit_name:
            raise ValueError("unit price requires a unit name")

    def compatible(self, other: "MoneyAmount") -> bool:
        return (
            self.currency == other.currency
            and self.kind == other.kind
            and self.unit_name == other.unit_name
            and self.unit_quantity == other.unit_quantity
        )

    def delta(self, previous: "MoneyAmount") -> Decimal:
        if not self.compatible(previous):
            raise ValueError("money amounts are not comparable")
        return self.amount - previous.amount


@dataclass(frozen=True, slots=True)
class PriceValue:
    state: Literal["present", "missing", "unknown"]
    amount: MoneyAmount | None = None

    def __post_init__(self) -> None:
        if self.state == "present" and self.amount is None:
            raise ValueError("present price requires MoneyAmount")
        if self.state != "present" and self.amount is not None:
            raise ValueError("missing or unknown price cannot contain MoneyAmount")

    @classmethod
    def parse(cls, raw: object, *, currency: str, kind: PRICE_KINDS, unit_name: str | None = None) -> "PriceValue":
        from pricepulse.data.normalize import parse_decimal

        if raw is None:
            return cls("missing")
        text = str(raw).strip().lower()
        if text in {"", "-", "n/a", "na", "none", "null", "unknown"}:
            return cls("missing" if text in {"-", ""} else "unknown")
        try:
            amount = parse_decimal(raw)
            return cls("present", MoneyAmount(amount, currency, kind, unit_name=unit_name))
        except (ValueError, InvalidOperation, TypeError):
            return cls("unknown")
