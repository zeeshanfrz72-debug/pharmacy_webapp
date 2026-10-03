"""Read-only compatibility report. Never infer missing invoice allocations."""
from decimal import Decimal
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F, Q
from django.db.models.functions import Coalesce

from ledger.models import Bill, Firm, LedgerEntry, Payment, TransactionBatch
from ledger.money import MoneySum


class Command(BaseCommand):
    help = "Read-only reconciliation and legacy invoice-allocation anomaly counts."

    @transaction.atomic
    def handle(self, *args, **options):
        components = {}
        rows = LedgerEntry.objects.filter(is_deleted=False).annotate(invoice=Coalesce("bill_id", "payment__bill_id")).values("firm_id", "invoice").annotate(inc=MoneySum("increase"), dec=MoneySum("decrease")).order_by()
        credit_bills = unassigned_firms = 0
        for row in rows:
            amount = row["inc"] - row["dec"]
            components[row["firm_id"]] = components.get(row["firm_id"], Decimal(0)) + amount
            credit_bills += int(row["invoice"] is not None and amount < 0)
            unassigned_firms += int(row["invoice"] is None and amount != 0)
        mismatches = sum(f.current_debt() != components.get(f.pk, Decimal(0)) for f in Firm.objects.all())
        wrong_relationships = LedgerEntry.objects.filter(Q(bill_id__isnull=False) & ~Q(bill__firm_id=F("firm_id")) | Q(payment_id__isnull=False) & ~Q(payment__firm_id=F("firm_id")) | Q(payment__bill_id__isnull=False) & ~Q(payment__bill__firm_id=F("firm_id"))).count()
        report = {"reconciles": mismatches == 0 and wrong_relationships == 0,
            "total_bills": Bill.objects.count(), "usable_bills": Bill.objects.enabled().count(),
            "disabled_bills": Bill.objects.filter(is_disabled=True).count(),
            "payments_without_bill_link": Payment.objects.filter(bill_id__isnull=True).count(),
            "bills_without_creation_batch": Bill.objects.filter(creation_batch_id__isnull=True).count(),
            "bills_with_net_credit": credit_bills, "firms_with_unassigned_nonzero_balance": unassigned_firms,
            "balance_mismatches": mismatches, "relationship_mismatches": wrong_relationships,
            "legacy_posting_receipts_unavailable": TransactionBatch.objects.filter(posting_receipt__isnull=True).count()}
        self.stdout.write(json.dumps(report, sort_keys=True))
        if not report["reconciles"]:
            raise CommandError("Review relationship or balance anomalies before deploying. No records were changed.")
