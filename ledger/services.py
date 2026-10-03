"""Atomic ledger operations shared by views and tests."""

from decimal import Decimal
import hashlib
import json

from django.core.exceptions import ValidationError
from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone

from .batch_context import use_batch
from .models import Bill, BillCarryForward, BillEditEvent, DeletionGroup, DeletionMember, Firm, LedgerEntry, Payment, Representative, TransactionBatch
from .money import money, MoneySum


def bill_revision(bill):
    values = [bill.bill_number, str(bill.bill_date), str(bill.bill_amount), bill.representative_id, bill.notes, bill.is_disabled, str(bill.remaining_balance)]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _refresh_active_snapshots(firm):
    money(firm.current_debt())
    for row in LedgerEntry.objects.filter(firm=firm, is_deleted=False).annotate(
        invoice=Coalesce("bill_id", "payment__bill_id")
    ).exclude(invoice__isnull=True).values("invoice").annotate(increases=MoneySum("increase"), decreases=MoneySum("decrease")).order_by():
        money(row["increases"] - row["decreases"])
    entries = LedgerEntry.objects.filter(firm=firm, is_deleted=False).filter(
        models.Q(transaction_batch__isnull=True) | models.Q(transaction_batch__kind="transaction", transaction_batch__status="active") | models.Q(transaction_batch__kind__in=("carry_forward", "transfer_undo"))
    ).order_by("date", "transaction_batch_id", "id")
    roots = {batch.pk: batch for batch in firm.transaction_batches.filter(models.Q(kind="transaction", status="active") | models.Q(kind__in=("carry_forward", "transfer_undo")))}
    balance, visited = Decimal("0.00"), set()
    for entry in entries:
        batch = roots.get(entry.transaction_batch_id)
        if batch and batch.pk not in visited:
            batch.previous_balance = money(balance)
            visited.add(batch.pk)
        # A bill/payment action may briefly exceed the snapshot field range
        # between its entries. Only its stored before/after values are bounded.
        balance += money(entry.increase) - money(entry.decrease)
        if batch:
            batch.balance_after = balance
    for batch in roots.values():
        batch.balance_after = money(batch.balance_after)
    # Legacy snapshots had mixed meanings. Preserve that evidence; reports
    # compute corrected chronology independently and original receipts stay null.
    known_roots = {pk: root for pk, root in roots.items() if root.posting_receipt is not None}
    TransactionBatch.objects.bulk_update(list(known_roots.values()), ["previous_balance", "balance_after"])
    bills = list(Bill.objects.filter(creation_batch_id__in=visited & known_roots.keys()))
    for bill in bills:
        bill.previous_debt_at_bill_time = roots[bill.creation_batch_id].previous_balance
    Bill.objects.bulk_update(bills, ["previous_debt_at_bill_time"])


def edit_bill(bill_id, data, *, user):
    _actor(user)
    from datetime import date
    if not isinstance(data.get("bill_date"), date) or not isinstance(data.get("bill_number"), str) or len(data["bill_number"]) > 100:
        raise ValidationError("A valid business date and bill reference of 1 to 100 characters are required.")
    if not isinstance(data.get("notes", ""), str) or len(data.get("notes", "")) > 10000 or not data.get("revision"):
        raise ValidationError("A current bill revision and notes of at most 10,000 characters are required.")
    original = Bill.objects.available().filter(pk=bill_id).first()
    if original is None:
        raise ValidationError("Bill was not found or has no creation transaction.")
    with transaction.atomic():
        firm = _lock_firm_for_write(original.firm_id)
        _actor(user)
        bill = Bill.objects.select_for_update().select_related("creation_batch").get(pk=bill_id)
        if bill.is_disabled:
            raise ValidationError("Undo this bill's carry-forward before editing it.")
        _bill_reference(firm, data["bill_number"])
        root = bill.creation_batch
        if not root or root.status != TransactionBatch.Status.ACTIVE:
            raise ValidationError("Recover this bill from Trash before editing it.")
        amount = money(data.get("bill_amount"), positive=True)
        representative = data.get("representative")
        representative_id = getattr(representative, "pk", None)
        if representative_id is None and firm.source_type != Firm.SourceType.LOCAL_MARKET:
            raise ValidationError("Choose an active representative belonging to this firm.")
        if representative_id is not None and not Representative.objects.filter(pk=representative_id, firm=firm).filter(
            models.Q(pk=bill.representative_id) | models.Q(is_active=True, is_deleted=False)
        ).exists():
            raise ValidationError("Choose an active representative belonging to this firm.")
        values = {"bill_number": data["bill_number"].strip(), "bill_date": data["bill_date"],
                  "bill_amount": amount, "representative_id": representative_id, "notes": data.get("notes", "")}
        if all(getattr(bill, key) == value for key, value in values.items()):
            return bill, False
        if data["revision"] != bill_revision(bill):
            raise ValidationError("This bill changed after you opened it. Reload and review the latest bill.")
        before = _edit_values(bill)
        old_amount, old_date, old_number = bill.bill_amount, bill.bill_date, bill.bill_number
        delta = amount - old_amount
        description = (f"Bill edited by {user.username} at {timezone.localtime():%d-%m-%Y %H:%M}: "
                       f"amount {old_amount} → {amount}; date {old_date:%d-%m-%Y} → {values['bill_date']:%d-%m-%Y}; "
                       f"number {old_number} → {values['bill_number']}")[:255]
        for key, value in values.items():
            setattr(bill, key, value)
        with use_batch(root):
            bill.save(update_fields=list(values))
        # Append the difference to the original batch so Delete/Recover continues
        # to reverse its entire current financial effect, including later edits.
        LedgerEntry.objects.create(firm=firm, transaction_batch=root, bill=bill,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT, date=bill.bill_date,
            increase=max(delta, Decimal("0.00")), decrease=max(-delta, Decimal("0.00")), description=description)
        paired_payments = list(Payment.objects.filter(ledger_entries__transaction_batch=root, ledger_entries__entry_type="payment_made", bill=bill).distinct())
        for payment in paired_payments:
            payment.payment_date = bill.bill_date
            payment.representative_id = representative_id
            with use_batch(root):
                payment.save(update_fields=["payment_date", "representative", "amount"])
        root.date = bill.bill_date
        root.save(update_fields=["date"])
        root.entries.update(date=bill.bill_date)
        _refresh_active_snapshots(firm)
        BillEditEvent.objects.create(batch=root, bill=bill, actor=user,
            actor_snapshot={"id": user.pk, "username": user.get_username()}, before=before, after=_edit_values(bill))
        return bill, True


def _edit_values(bill):
    def serialize(obj, fields):
        return {key: (str(getattr(obj, key)) if key in ("bill_date", "payment_date", "bill_amount", "amount") else getattr(obj, key)) for key in fields}
    return {"bill": serialize(bill, ("bill_number", "bill_date", "bill_amount", "representative_id", "notes")),
            "payments": [{"id": p.pk, **serialize(p, ("payment_date", "amount", "representative_id", "method", "notes"))} for p in Payment.objects.filter(ledger_entries__transaction_batch_id=bill.creation_batch_id, ledger_entries__entry_type="payment_made", bill=bill).distinct().order_by("id")]}


def _actor(user):
    from django.contrib.auth import get_user_model
    if not getattr(user, "pk", None) or not user.is_authenticated or not get_user_model().objects.filter(pk=user.pk, is_active=True, username=settings.APP_OWNER_USERNAME).exists() or user.get_username() != settings.APP_OWNER_USERNAME:
        raise ValidationError("The authenticated active application owner is required.")


class IdempotencyConflict(ValidationError):
    pass


def _bill_reference(firm, value):
    if not isinstance(value, str) or len(value) > 100 or (firm.source_type != Firm.SourceType.LOCAL_MARKET and not value.strip()):
        raise ValidationError("A bill number is required for this source type.")


def _active_transfers(bill_id):
    return BillCarryForward.objects.filter(batch__status="active").filter(models.Q(source_id=bill_id) | models.Q(destination_id=bill_id))


def _guard_source_changes(roots):
    for root in roots:
        for entry in root.entries.select_related("bill", "payment__bill"):
            bill = entry.bill or (entry.payment.bill if entry.payment_id else None)
            if bill and (bill.is_disabled or (entry.bill_id and _active_transfers(bill.pk).exists())):
                raise ValidationError("Undo the affected carry-forward before deleting or reversing this transaction.")


def carry_forward_bill(source_id, *, destination_id=None, new_bill=None, source_revision,
                       destination_revision="", user, request_id, payload_hash, reason="Disable and carry forward"):
    """Close an invoice by reallocating its whole unpaid balance, never re-posting it."""
    import uuid
    _actor(user)
    replay = committed_retry(request_id, user=user, payload_hash=payload_hash)
    if replay:
        if replay["batch"].kind != TransactionBatch.Kind.CARRY_FORWARD:
            raise IdempotencyConflict("This request ID belongs to another action.")
        return replay
    try:
        request_id = uuid.UUID(str(request_id))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("A valid request ID is required.")
    source = Bill.objects.enabled().filter(pk=source_id).first()
    if source is None:
        raise ValidationError("Choose an enabled source bill.")
    with transaction.atomic():
        firm = _lock_firm_for_write(source.firm_id)
        _actor(user)
        replay = committed_retry(request_id, user=user, payload_hash=payload_hash)
        if replay:
            return replay
        source = Bill.objects.enabled().select_for_update().filter(pk=source_id).first()
        if source is None:
            raise ValidationError("Choose an enabled source bill.")
        if source_revision != bill_revision(source):
            raise ValidationError("This bill changed. Refresh the carry-forward confirmation.")
        amount = money(source.remaining_balance)
        if amount < 0:
            raise ValidationError("Reconcile this bill's credit before disabling it.")
        if bool(destination_id) == bool(new_bill):
            raise ValidationError("Choose one destination bill or create a new bill.")
        created_destination = None
        if new_bill:
            created_destination = create_transaction_batch({
                "request_id": uuid.uuid5(request_id, "carry-forward-destination"), "firm": firm,
                "source_type": firm.source_type, "representative": new_bill.get("representative"),
                "bill_choice": "add_new", "new_bill_number": new_bill.get("bill_number", ""),
                "new_bill_amount": new_bill.get("bill_amount"), "bill_date": new_bill.get("bill_date"),
                "notes": new_bill.get("notes", ""), "payment_amount": None,
            }, user=user, payload_hash=hashlib.sha256((payload_hash + ":destination").encode()).hexdigest())
            destination = created_destination["bill"]
        else:
            destination = Bill.objects.enabled().select_for_update().filter(pk=destination_id, firm=firm).first()
            if destination is None or destination.pk == source.pk:
                raise ValidationError("Choose another enabled bill from the same firm.")
            if destination_revision != bill_revision(destination):
                raise ValidationError("The destination bill changed. Refresh the carry-forward confirmation.")
        before = money(firm.current_debt())
        source_before, destination_before = source.remaining_balance, destination.remaining_balance
        money(destination_before + amount)
        root = TransactionBatch.objects.create(request_id=request_id, payload_hash=payload_hash,
            firm=firm, created_by=user, kind="carry_forward", date=timezone.localdate(),
            previous_balance=before, balance_after=before, reason=_reason(reason))
        with use_batch(root):
            for bill, increase, decrease in ((source, Decimal("0.00"), amount), (destination, amount, Decimal("0.00"))):
                LedgerEntry.objects.create(firm=firm, bill=bill, transaction_batch=root, date=root.date,
                    entry_type="carry_forward", increase=increase, decrease=decrease,
                    description=f"Carry-forward #{source.pk} → #{destination.pk}")
            receipt = {"source_id": source.pk, "destination_id": destination.pk,
                "source_reference": source.display_reference, "destination_reference": destination.display_reference,
                "amount": str(amount), "source_before": str(source_before), "source_after": str(source.remaining_balance),
                "destination_before": str(destination_before), "destination_after": str(destination.remaining_balance),
                "firm_before": str(before), "firm_after": str(firm.current_debt()),
                "actor": {"id": user.pk, "username": user.get_username()}, "timestamp": timezone.now().isoformat(),
                "created_destination_batch_id": created_destination["batch"].pk if created_destination else None}
            BillCarryForward.objects.create(batch=root, source=source, destination=destination, amount=amount, receipt=receipt)
        Bill.objects.filter(pk=source.pk).update(is_disabled=True)
        if firm.current_debt() != before:
            raise ValidationError("Carry-forward did not reconcile. No changes were saved.")
        root.posting_receipt = {**_receipt(root), "carry_forward": receipt}
        root.save(update_fields=["posting_receipt"])
        _refresh_active_snapshots(firm)
        return _batch_summary(root, created=True)


def undo_carry_forward(transfer_id, *, user, request_id, payload_hash, reason="Undo carry forward"):
    _actor(user)
    replay = committed_retry(request_id, user=user, payload_hash=payload_hash)
    if replay:
        if replay["batch"].kind != "transfer_undo":
            raise IdempotencyConflict("This request ID belongs to another action.")
        return replay
    import uuid
    try:
        request_id = uuid.UUID(str(request_id))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("A valid request ID is required.")
    transfer = BillCarryForward.objects.select_related("batch", "source", "destination").filter(pk=transfer_id).first()
    if transfer is None:
        raise ValidationError("Carry-forward was not found.")
    with transaction.atomic():
        firm = _lock_firm_for_write(transfer.source.firm_id)
        _actor(user)
        replay = committed_retry(request_id, user=user, payload_hash=payload_hash)
        if replay:
            return replay
        transfer = BillCarryForward.objects.select_related("batch", "source", "destination").get(pk=transfer_id)
        if transfer.batch.status != "active":
            raise ValidationError("This carry-forward was already undone.")
        if not Bill.objects.available().filter(pk__in=[transfer.source_id, transfer.destination_id]).count() == 2:
            raise ValidationError("Both bills must be available before undoing carry-forward.")
        if transfer.destination.is_disabled:
            raise ValidationError("Undo later carry-forwards first.")
        if transfer.destination.remaining_balance < transfer.amount:
            raise ValidationError("Reverse dependent payments before returning this carried debt.")
        source_before, destination_before = transfer.source.remaining_balance, transfer.destination.remaining_balance
        event = _write_batch_offsets(original=transfer.batch, kind="transfer_undo", user=user,
            reason=_reason(reason), source_batch=transfer.batch, request_id=request_id, payload_hash=payload_hash,
            receipt_extra={"carry_forward": transfer.receipt, "undo_of": transfer.pk,
                "undo_balances": {"source_before": str(source_before), "source_after": str(source_before + transfer.amount),
                    "destination_before": str(destination_before), "destination_after": str(destination_before - transfer.amount)}})
        transfer.batch.status = "reversed"
        transfer.batch.save(update_fields=["status"])
        Bill.objects.filter(pk=transfer.source_id).update(is_disabled=False)
        _refresh_active_snapshots(firm)
        return _batch_summary(event, created=True)


def _lock_firm_for_write(firm_id, *, allow_deleted=False):
    # The first database operation in the transaction is a write. This obtains
    # SQLite's writer lock before reading the balance; on row-locking databases
    # it also serializes this account's balance checks.
    firms = Firm.objects.filter(pk=firm_id)
    if not allow_deleted:
        firms = firms.filter(is_deleted=False)
    updated = firms.update(
        balance_version=F("balance_version") + 1
    )
    if not updated:
        raise ValidationError("This firm is in Trash and cannot receive new transactions.")
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
        "previous_debt": Decimal(batch.posting_receipt["previous_balance"]) if batch.posting_receipt else None,
        "remaining_debt": Decimal(batch.posting_receipt["balance_after"]) if batch.posting_receipt else None,
        "created": created,
    }


def committed_retry(request_id, *, user, payload_hash):
    _actor(user)
    import uuid
    try:
        request_id = uuid.UUID(str(request_id))
    except (ValueError, TypeError, AttributeError):
        return None
    existing = TransactionBatch.objects.filter(request_id=request_id).first()
    if existing:
        if existing.created_by_id != user.pk or existing.payload_hash != payload_hash:
            raise IdempotencyConflict("This request ID was already used for different transaction data.")
        return _batch_summary(existing, created=False)
    return None


def _receipt(batch):
    return {"request_id": str(batch.request_id), "firm_id": batch.firm_id, "firm_name": batch.firm.name, "source_type": batch.firm.source_type,
            "actor": {"id": batch.created_by_id, "username": batch.created_by.get_username() if batch.created_by_id else None},
            "date": str(batch.date), "previous_balance": str(batch.previous_balance), "balance_after": str(batch.balance_after),
            "entries": [{"entry_id": e.pk, "bill_id": e.bill_id, "payment_id": e.payment_id,
                         "bill_number": e.bill.bill_number if e.bill_id else e.payment.bill.bill_number if e.payment_id and e.payment.bill_id else None,
                         "display_reference": e.bill.display_reference if e.bill_id else e.payment.bill.display_reference if e.payment_id and e.payment.bill_id else None,
                         "source": _edit_values(e.bill)["bill"] if e.bill_id else {"amount": str(e.payment.amount), "date": str(e.payment.payment_date), "method": e.payment.method, "notes": e.payment.notes, "bill_id": e.payment.bill_id} if e.payment_id else None,
                         "representative_id": (e.bill.representative_id if e.bill_id else e.payment.representative_id if e.payment_id else None),
                         "increase": str(e.increase), "decrease": str(e.decrease)} for e in batch.entries.select_related("bill", "payment", "payment__bill").order_by("id")]}


def create_transaction_batch(cleaned_data, *, user, payload_hash):
    _actor(user)
    retry = committed_retry(cleaned_data.get("request_id"), user=user, payload_hash=payload_hash)
    if retry:
        if retry["batch"].kind != "transaction":
            raise IdempotencyConflict("This request ID belongs to another action.")
        return retry
    import uuid
    from datetime import date
    try:
        uuid.UUID(str(cleaned_data.get("request_id")))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("A valid request ID is required.")
    if not isinstance(cleaned_data.get("firm"), Firm) or not cleaned_data["firm"].pk:
        raise ValidationError("Choose a saved supplier.")
    if cleaned_data.get("bill_date") is not None and not isinstance(cleaned_data["bill_date"], date):
        raise ValidationError("A valid business date is required.")
    if not isinstance(cleaned_data.get("notes", ""), str) or len(cleaned_data.get("notes", "")) > 10000:
        raise ValidationError("Notes cannot exceed 10,000 characters.")
    firm_id = cleaned_data["firm"].pk
    request_id = cleaned_data["request_id"]
    payment_amount = cleaned_data.get("payment_amount")
    payment_amount = money(payment_amount, positive=True) if payment_amount is not None else None
    new_bill_amount = cleaned_data.get("new_bill_amount")
    new_bill_amount = money(new_bill_amount, positive=True) if new_bill_amount is not None else None
    adding_new_bill = cleaned_data.get("bill_choice") == "add_new"
    if not payment_amount and not adding_new_bill:
        raise ValidationError("Enter a new bill or a payment.")
    if adding_new_bill and (new_bill_amount is None or not isinstance(cleaned_data.get("new_bill_number", ""), str) or len(cleaned_data.get("new_bill_number", "")) > 100):
        raise ValidationError("A new bill requires a positive amount and reference of 1 to 100 characters.")
    if new_bill_amount is not None and not adding_new_bill:
        raise ValidationError("A bill amount requires a new bill.")

    try:
        with transaction.atomic():
            firm = _lock_firm_for_write(firm_id)
            _actor(user)

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

            representative = cleaned_data.get("representative")
            if firm.source_type != cleaned_data.get("source_type", firm.source_type):
                raise ValidationError("The firm does not belong to the selected source type.")
            if (representative is not None or firm.source_type != Firm.SourceType.LOCAL_MARKET) and not Representative.objects.filter(
                pk=getattr(representative, "pk", None), firm=firm, is_active=True, is_deleted=False,
            ).exists():
                raise ValidationError("Choose an active representative belonging to this firm.")
            if adding_new_bill:
                _bill_reference(firm, cleaned_data.get("new_bill_number", ""))
            selected_bill = None
            if cleaned_data.get("bill_choice") not in (None, "", "add_new"):
                try:
                    selected_bill = Bill.objects.enabled().filter(pk=int(cleaned_data["bill_choice"]), firm=firm).first()
                except (ValueError, TypeError):
                    raise ValidationError("Choose a valid bill.")
                if selected_bill is None:
                    raise ValidationError("This bill is disabled, deleted or does not belong to the selected firm.")

            previous_balance = money(firm.current_debt())
            if payment_amount:
                if not adding_new_bill and selected_bill is None:
                    raise ValidationError("Choose one bill for this payment.")
                invoice_balance = new_bill_amount if adding_new_bill else selected_bill.remaining_balance
                if payment_amount > invoice_balance:
                    raise ValidationError("Payment amount cannot be higher than this bill's remaining debt.")
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
                date=cleaned_data.get("bill_date") or timezone.localdate(),
                previous_balance=previous_balance,
            )

            bill = None
            payment = None
            representative = cleaned_data.get("representative")
            bill_choice = cleaned_data.get("bill_choice")

            with use_batch(batch):
                if adding_new_bill:
                    bill = Bill.objects.create(
                        firm=firm,
                        representative=representative,
                        bill_number=cleaned_data.get("new_bill_number", "").strip(),
                        bill_date=batch.date,
                        bill_amount=new_bill_amount,
                        previous_debt_at_bill_time=previous_balance,
                        notes=cleaned_data.get("notes", ""),
                    )
                elif bill_choice:
                    bill = selected_bill

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

            batch.balance_after = money(firm.current_debt())
            batch.posting_receipt = _receipt(batch)
            batch.save(update_fields=["balance_after", "posting_receipt"])
            _refresh_active_snapshots(firm)
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
        if existing:
            raise IdempotencyConflict("This request ID was already used for different transaction data.")
        raise


def _write_batch_offsets(*, original, kind, user, reason, source_batch, request_id=None, payload_hash="", receipt_extra=None):
    _actor(user)
    firm = original.firm
    event = TransactionBatch.objects.create(
        **({"request_id": request_id} if request_id else {}), payload_hash=payload_hash,
        firm=firm,
        created_by=user,
        date=timezone.localdate(),
        kind=kind,
        original_batch=original,
        reason=reason,
        previous_balance=money(firm.current_debt()),
    )
    source_entries = list(
        source_batch.entries.filter(is_deleted=False).order_by("id")
    )
    if not source_entries:
        raise ValidationError("There are no active ledger entries to reverse.")

    entry_type = "transfer_undo" if kind == "transfer_undo" else LedgerEntry.EntryType.REVERSAL if kind == TransactionBatch.Kind.REVERSAL else LedgerEntry.EntryType.RESTORATION
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
    event.balance_after = money(firm.current_debt())
    event.posting_receipt = {**_receipt(event), **(receipt_extra or {})}
    event.save(update_fields=["balance_after", "posting_receipt"])
    return event


def _reason(reason):
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise ValidationError("Enter a reason of 1 to 500 characters.")
    return reason


def _delete_roots(roots, *, user, reason, bill=None):
    _guard_source_changes(roots)
    group = DeletionGroup.objects.create(
        firm=roots[0].firm, bill=bill, created_by=user, reason=reason,
        kind=DeletionGroup.Kind.BILL if bill else DeletionGroup.Kind.TRANSACTION,
    )
    for root in roots:
        event = _write_batch_offsets(
            original=root, kind=TransactionBatch.Kind.REVERSAL,
            user=user, reason=reason, source_batch=root,
        )
        DeletionMember.objects.create(group=group, batch=root, reversal=event)
        root.status = TransactionBatch.Status.REVERSED
        root.deletion_group = group
        root.save(update_fields=["status", "deletion_group"])
    _refresh_active_snapshots(roots[0].firm)
    return {"batch": roots[0], "group": group, "event": event, "changed": True}


def reverse_transaction_batch(batch_id, *, user, reason):
    _actor(user)
    reason = _reason(reason)
    root = TransactionBatch.objects.filter(pk=batch_id, kind=TransactionBatch.Kind.TRANSACTION).first()
    if root is None:
        raise ValidationError("Transaction batch was not found.")
    with transaction.atomic():
        _lock_firm_for_write(root.firm_id, allow_deleted=True)
        root = TransactionBatch.objects.select_for_update().select_related("deletion_group").get(pk=batch_id)
        if root.status == TransactionBatch.Status.REVERSED:
            return {"batch": root, "group": root.deletion_group, "changed": False}
        return _delete_roots([root], user=user, reason=reason)


def bill_deletion_batches(bill):
    return TransactionBatch.objects.filter(
        firm_id=bill.firm_id, kind=TransactionBatch.Kind.TRANSACTION,
        status=TransactionBatch.Status.ACTIVE,
    ).filter(
        models.Q(pk=bill.creation_batch_id) | models.Q(entries__payment__bill_id=bill.pk)
    ).distinct().order_by("id")


def delete_bill(bill_id, *, user, reason):
    _actor(user)
    reason = _reason(reason)
    bill = Bill.objects.filter(pk=bill_id).first()
    if bill is None or not bill.creation_batch_id:
        raise ValidationError("Bill was not found or has no creation transaction.")
    with transaction.atomic():
        _lock_firm_for_write(bill.firm_id, allow_deleted=True)
        root = TransactionBatch.objects.select_for_update().select_related("deletion_group").get(pk=bill.creation_batch_id)
        if root.status == TransactionBatch.Status.REVERSED:
            return {"batch": root, "group": root.deletion_group, "changed": False}
        roots = list(bill_deletion_batches(bill))
        if _active_transfers(bill.pk).exists():
            raise ValidationError("Undo this bill's carry-forward before deleting it.")
        return _delete_roots(roots, user=user, reason=reason, bill=bill)


def recover_deletion_group(group_id, *, user, reason):
    _actor(user)
    reason = _reason(reason)
    group = DeletionGroup.objects.filter(pk=group_id).first()
    if group is None:
        raise ValidationError("This Trash item was not found.")
    with transaction.atomic():
        firm = _lock_firm_for_write(group.firm_id, allow_deleted=True)
        group = DeletionGroup.objects.select_for_update().get(pk=group_id)
        members = list(group.members.select_related("batch", "reversal").order_by("batch_id"))
        if group.restored_at is not None:
            return {"batch": members[0].batch, "group": group, "changed": False, "firm": firm}
        _guard_source_changes([member.batch for member in members])
        # Restore precisely the membership recorded by this deletion, never unrelated earlier deletions.
        for member in members:
            root = member.batch
            if root.status != TransactionBatch.Status.REVERSED or root.deletion_group_id != group.pk:
                raise ValidationError("This item's deletion state has changed. Refresh Trash and try again.")
            event = _write_batch_offsets(
                original=root, kind=TransactionBatch.Kind.RESTORATION, user=user,
                reason=reason, source_batch=member.reversal,
            )
            root.status = TransactionBatch.Status.ACTIVE
            root.deletion_group = None
            root.save(update_fields=["status", "deletion_group"])
        group.restored_at = timezone.now()
        group.save(update_fields=["restored_at"])
        _refresh_active_snapshots(firm)
        return {"batch": members[0].batch, "group": group, "event": event, "changed": True, "firm": firm}


def restore_transaction_batch(batch_id, *, user, reason):
    _actor(user)
    root = TransactionBatch.objects.filter(pk=batch_id, kind=TransactionBatch.Kind.TRANSACTION).first()
    if root is None:
        raise ValidationError("Transaction batch was not found.")
    if root.status == TransactionBatch.Status.ACTIVE:
        _reason(reason)
        return {"batch": root, "changed": False}
    if not root.deletion_group_id:
        raise ValidationError("This transaction has no deletion to undo.")
    return recover_deletion_group(root.deletion_group_id, user=user, reason=reason)
