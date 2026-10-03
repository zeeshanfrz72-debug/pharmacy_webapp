"""Read raw values so Django cannot hide legacy fractional cents by rounding."""
from decimal import Decimal, InvalidOperation


def verify(apps, schema_editor):
    connection = schema_editor.connection
    problems = []
    fields = {"Bill": ("bill_amount", "previous_debt_at_bill_time"),
              "Payment": ("amount",), "LedgerEntry": ("increase", "decrease"),
              "TransactionBatch": ("previous_balance", "balance_after")}
    with connection.cursor() as cursor:
        for name, columns in fields.items():
            table = connection.ops.quote_name(apps.get_model("ledger", name)._meta.db_table)
            cursor.execute(f"SELECT id, {', '.join(connection.ops.quote_name(c) for c in columns)} FROM {table}")
            for row in cursor:
                values = []
                for column, raw in zip(columns, row[1:]):
                    try:
                        value = Decimal(str(raw))
                        valid = value.is_finite() and abs(value) <= Decimal("9999999999.99") and value == value.quantize(Decimal("0.01"))
                        if (name, column) in (("Bill", "bill_amount"), ("Payment", "amount")):
                            valid = valid and value > 0
                        if name == "LedgerEntry":
                            valid = valid and value >= 0
                        values.append(value)
                    except (InvalidOperation, ValueError):
                        valid = False
                    if not valid and len(problems) < 20:
                        problems.append(f"{name} #{row[0]} {column}")
                if name == "LedgerEntry" and len(values) == 2 and all(v.is_finite() and v > 0 for v in values) and len(problems) < 20:
                    problems.append(f"LedgerEntry #{row[0]} has two money sides")
    if problems:
        raise RuntimeError("Ledger migration stopped without changing financial evidence. Review invalid legacy values on a disposable snapshot: " + "; ".join(problems))
