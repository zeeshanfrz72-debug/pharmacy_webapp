from decimal import Decimal

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.core.exceptions import ValidationError

from .batch_context import current_batch
from .models import Bill, Payment, LedgerEntry, TransactionBatch


def _batch_for_source(instance):
    batch = current_batch()
    if batch is not None:
        return batch
    raise ValidationError("Financial records must be posted through an atomic ledger service.")


@receiver(post_save, sender=Bill)
def create_bill_ledger_entry(sender, instance, created, **kwargs):
    if created:
        batch = _batch_for_source(instance)
        LedgerEntry.objects.create(
            bill=instance,
            firm=instance.firm,
            transaction_batch=batch,
            entry_type=LedgerEntry.EntryType.BILL_CREATED,
            date=instance.bill_date,
            increase=instance.bill_amount,
            decrease=Decimal("0.00"),
            description=f"Bill {instance.bill_number} created",
        )
        Bill.objects.filter(pk=instance.pk).update(creation_batch=batch)
        instance.creation_batch = batch


@receiver(post_save, sender=Payment)
def create_payment_ledger_entry(sender, instance, created, **kwargs):
    if created:
        batch = _batch_for_source(instance)
        LedgerEntry.objects.create(
            payment=instance,
            firm=instance.firm,
            transaction_batch=batch,
            entry_type=LedgerEntry.EntryType.PAYMENT_MADE,
            date=instance.payment_date,
            increase=Decimal("0.00"),
            decrease=instance.amount,
            description=f"Payment of {instance.amount} made to {instance.firm.name}",
        )
