from decimal import Decimal

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Bill, Payment, LedgerEntry


@receiver(post_save, sender=Bill)
def create_bill_ledger_entry(sender, instance, created, **kwargs):
    current_debt = instance.firm.current_debt()

    if created:
        if current_debt == 0 and instance.previous_debt_at_bill_time > 0:
            ledger_increase = instance.previous_debt_at_bill_time + instance.bill_amount
        else:
            ledger_increase = instance.bill_amount
            if instance.previous_debt_at_bill_time == 0:
                Bill.objects.filter(pk=instance.pk).update(
                    previous_debt_at_bill_time=current_debt
                )
    else:
        ledger_increase = instance.bill_amount

    LedgerEntry.objects.update_or_create(
        bill=instance,
        defaults={
            "firm": instance.firm,
            "entry_type": LedgerEntry.EntryType.BILL_CREATED,
            "date": instance.bill_date,
            "increase": ledger_increase,
            "decrease": Decimal("0.00"),
            "description": f"Bill {instance.bill_number} created",
        },
    )


@receiver(post_save, sender=Payment)
def create_payment_ledger_entry(sender, instance, created, **kwargs):
    LedgerEntry.objects.update_or_create(
        payment=instance,
        defaults={
            "firm": instance.firm,
            "entry_type": LedgerEntry.EntryType.PAYMENT_MADE,
            "date": instance.payment_date,
            "increase": Decimal("0.00"),
            "decrease": instance.amount,
            "description": f"Payment of {instance.amount} made to {instance.firm.name}",
        },
    )