"""Atomic ledger operations shared by views and tests."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .batch_context import use_batch
from .models import Bill, Firm, LedgerEntry, Payment, TransactionBatch


class IdempotencyConflict(ValidationError):
    pass


def _lock_firm_for_write(firm_id):
    # The first database operation in the transaction is a write. This obtains
    # SQLite's writer lock before reading the balance; on row-locking databases
    # it also serializes this account's balance checks.
    updated = Firm.objects.filter(pk=firm_id, is_deleted=False).update(
        balance_version=F("balance_version") + 1
    )
    if not updated:
        raise ValidationError("This firm is archived and cannot receive transactions.")
    return Firm.objects.select_for_update().get(pk=firm_id)


def _batch_summary(batch, *, created):
    entries = list(batch.entries.select_related("bill", "payment").order_by("id"))
    bill = next((entry.bill for entry in entries if entry.bill_id), None)
    payment = next((entry.payment for entry in entries if entry.payment_id), None)
    return {
        "batch": batch,
        "firm": batch.firm,
        "bill": bill,
        "payment": payment,
        "previous_debt": batch.previous_balance,
        "remaining_debt": batch.balance_after,
        "created": created,
    }


def create_transaction_batch(cleaned_data, *, user, payload_hash):
    firm_id = cleaned_data["firm"].pk
    request_id = cleaned_data["request_id"]
    payment_amount = cleaned_data.get("payment_amount")
    payment_amount = Decimal(payment_amount) if payment_amount is not None else None
    new_bill_amount = cleaned_data.get("new_bill_amount")
    new_bill_amount = Decimal(new_bill_amount) if new_bill_amount is not None else None
    adding_new_bill = cleaned_data.get("bill_choice") == "add_new"

    try:
        with transaction.atomic():
            firm = _lock_firm_for_write(firm_id)

            existing = TransactionBatch.objects.filter(request_id=request_id).first()
            if existing:
                if (
                    existing.firm_id != firm_id
                    or existing.created_by_id != user.pk
                    or existing.payload_hash != payload_hash
                ):
                    raise IdempotencyConflict(
                        "This request ID was already used for different transaction data."
                    )
                return _batch_summary(existing, created=False)

            previous_balance = Decimal(firm.current_debt())
            if (
                payment_amount
                and firm.source_type != Firm.SourceType.LOCAL_MARKET
            ):
                available_balance = previous_balance
                if adding_new_bill and new_bill_amount:
                    available_balance += new_bill_amount
                if payment_amount > available_balance:
                    raise ValidationError(
                        "Payment amount cannot be higher than the remaining debt."
                    )

            batch = TransactionBatch.objects.create(
                request_id=request_id,
                payload_hash=payload_hash,
                firm=firm,
                created_by=user,
                date=timezone.localdate(),
                previous_balance=previous_balance,
            )

            bill = None
            payment = None
            representative = cleaned_data.get("representative")
            bill_choice = cleaned_data.get("bill_choice")

            with use_batch(batch):
                if firm.source_type == Firm.SourceType.LOCAL_MARKET and payment_amount:
                    bill = Bill.objects.create(
                        firm=firm,
                        representative=None,
                        bill_number="Local Market Purchase",
                        bill_date=batch.date,
                        bill_amount=payment_amount,
                        previous_debt_at_bill_time=previous_balance,
                    )
                elif adding_new_bill:
                    bill = Bill.objects.create(
                        firm=firm,
                        representative=representative,
                        bill_number=cleaned_data["new_bill_number"],
                        bill_date=batch.date,
                        bill_amount=new_bill_amount,
                        previous_debt_at_bill_time=previous_balance,
                    )
                elif bill_choice:
                    bill = Bill.objects.get(pk=bill_choice, firm=firm)

                if payment_amount:
                    payment = Payment.objects.create(
                        firm=firm,
                        representative=representative,
                        bill=bill,
                        payment_date=batch.date,
                        amount=payment_amount,
                    )

            if payment:
                LedgerEntry.objects.filter(payment=payment).update(transaction_batch=batch)

            batch.balance_after = Decimal(firm.current_debt())
            batch.save(update_fields=["balance_after"])
            return _batch_summary(batch, created=True)
    except IntegrityError:
        # A concurrent replay may win the unique request ID insert.
        existing = TransactionBatch.objects.filter(request_id=request_id).first()
        if (
            existing
            and existing.firm_id == firm_id
            and existing.created_by_id == user.pk
            and existing.payload_hash == payload_hash
        ):
            return _batch_summary(existing, created=False)
        raise IdempotencyConflict(
            "This request ID was already used for different transaction data."
        )


def _write_batch_offsets(*, original, kind, user, reason, source_batch):
    firm = original.firm
    event = TransactionBatch.objects.create(
        firm=firm,
        created_by=user,
        date=timezone.localdate(),
        kind=kind,
        original_batch=original,
        reason=reason,
        previous_balance=Decimal(firm.current_debt()),
    )
    source_entries = list(
        source_batch.entries.filter(is_deleted=False).order_by("id")
    )
    if not source_entries:
        raise ValidationError("There are no active ledger entries to reverse.")

    entry_type = (
        LedgerEntry.EntryType.REVERSAL
        if kind == TransactionBatch.Kind.REVERSAL
        else LedgerEntry.EntryType.RESTORATION
    )
    for source in source_entries:
        LedgerEntry.objects.create(
            firm=firm,
            entry_type=entry_type,
            date=event.date,
            bill=source.bill,
            payment=source.payment,
            transaction_batch=event,
            increase=source.decrease,
            decrease=source.increase,
            description=f"{event.get_kind_display()} of batch #{original.pk}: {reason}",
        )
    event.balance_after = Decimal(firm.current_debt())
    event.save(update_fields=["balance_after"])
    return event


def reverse_transaction_batch(batch_id, *, user, reason):
    with transaction.atomic():
        root = TransactionBatch.objects.filter(pk=batch_id).first()
        if root is None or root.kind != TransactionBatch.Kind.TRANSACTION:
            raise ValidationError("Transaction batch was not found.")
        _lock_firm_for_write(root.firm_id)
        root = TransactionBatch.objects.select_for_update().get(pk=batch_id)
        if root.status == TransactionBatch.Status.REVERSED:
            event = root.audit_events.filter(kind=TransactionBatch.Kind.REVERSAL).order_by("-id").first()
            return {"batch": root, "event": event, "changed": False}

        event = _write_batch_offsets(
            original=root,
            kind=TransactionBatch.Kind.REVERSAL,
            user=user,
            reason=reason.strip(),
            source_batch=root,
        )
        root.status = TransactionBatch.Status.REVERSED
        root.save(update_fields=["status"])
        return {"batch": root, "event": event, "changed": True}


def restore_transaction_batch(batch_id, *, user, reason):
    with transaction.atomic():
        root = TransactionBatch.objects.filter(pk=batch_id).first()
        if root is None or root.kind != TransactionBatch.Kind.TRANSACTION:
            raise ValidationError("Transaction batch was not found.")
        firm = _lock_firm_for_write(root.firm_id)
        root = TransactionBatch.objects.select_for_update().get(pk=batch_id)
        if root.status == TransactionBatch.Status.ACTIVE:
            event = root.audit_events.filter(kind=TransactionBatch.Kind.RESTORATION).order_by("-id").first()
            return {"batch": root, "event": event, "changed": False}

        reversal = root.audit_events.filter(kind=TransactionBatch.Kind.REVERSAL).order_by("-id").first()
        if reversal is None:
            raise ValidationError("This transaction has no reversal to undo.")
        event = _write_batch_offsets(
            original=root,
            kind=TransactionBatch.Kind.RESTORATION,
            user=user,
            reason=reason.strip(),
            source_batch=reversal,
        )
        root.status = TransactionBatch.Status.ACTIVE
        root.save(update_fields=["status"])
        return {"batch": root, "event": event, "changed": True, "firm": firm}
