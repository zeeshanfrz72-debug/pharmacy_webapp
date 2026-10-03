"""Exact cent arithmetic. SQL sums integers, never SQLite floating totals."""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db.models import BigIntegerField, F, Sum, Value
from django.db.models.functions import Cast, Round

MAX_AMOUNT = Decimal("9999999999.99")
ZERO = Decimal("0.00")


def money(value, *, positive=False):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or abs(amount) > MAX_AMOUNT or amount != amount.quantize(Decimal("0.01")):
            raise ValueError
        if positive and amount <= 0:
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError("Amounts must be finite whole cents within 9,999,999,999.99; postings must be positive.")
    return amount.quantize(Decimal("0.01"))


def cents(expression):
    expression = F(expression) if isinstance(expression, str) else expression
    return Cast(Round(expression * Value(100)), BigIntegerField())


class MoneySum(Sum):
    def __init__(self, expression, **kwargs):
        super().__init__(cents(expression), output_field=BigIntegerField(), **kwargs)

    def convert_value(self, value, expression, connection):
        return None if value is None else Decimal(value) / 100


def balance(entries):
    result = entries.aggregate(net=Sum(cents("increase") - cents("decrease")))["net"]
    return Decimal(result or 0) / 100
