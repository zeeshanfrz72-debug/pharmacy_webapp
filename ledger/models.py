import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q, Sum
from django.db.models.functions import Round
from django.db.models.lookups import Exact
from django.utils import timezone
from .money import balance, MAX_AMOUNT


# Firm means the source/company account.
# It can be a direct company, distributor company, stockist, or local market seller.
class Firm(models.Model):
    # TextChoices creates fixed dropdown choices for source_type.
    # This is not a separate database table.
    class SourceType(models.TextChoices):
        DIRECT_COMPANY = "direct_company", "Direct Company"
        DISTRIBUTOR = "distributor", "Distributor Company"
        STOCKIST = "stockist", "Stockist Company"
        LOCAL_MARKET = "local_market", "Open / Local Market"

    # Name of the firm/company/source.
    # Example: Getz Pharma, ABC Distributor, XYZ Stockist.
    name = models.CharField(max_length=255)

    # Tells us what kind of source this firm is.
    source_type = models.CharField(max_length=30, choices=SourceType.choices)

    # Optional extra information.
    # blank=True means the form is allowed to leave this empty.
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    # Incremented inside each financial write to serialize balance checks on
    # SQLite (which does not implement SELECT ... FOR UPDATE).
    balance_version = models.PositiveBigIntegerField(default=0, editable=False)

    # Soft delete fields
    is_deleted = models.BooleanField(default=False, help_text="Soft delete flag")
    deleted_at = models.DateTimeField(null=True, blank=True, help_text="When this firm was soft deleted")

    # This calculates how much money is currently owed to this firm.
    # Formula: total bill/debt increases - total payment decreases.
    def current_debt(self):
        return balance(self.ledger_entries.filter(is_deleted=False))

    def clean(self):
        super().clean()
        if self.pk and self.source_type == self.SourceType.LOCAL_MARKET and self.representatives.exists():
            raise ValidationError({"source_type": "Move unused representatives to another supplier before changing this supplier to Local Market."})
        if self.pk and Firm.objects.filter(pk=self.pk).exclude(source_type=self.source_type).exists() and self.has_financial_history():
            raise ValidationError({"source_type": "This supplier has financial history. Create a new supplier for a different source type."})

    def has_financial_history(self):
        return self.ledger_entries.exists() or self.bills.exists() or self.payments.exists() or self.transaction_batches.exists()

    def save(self, *args, **kwargs):
        if not self.pk:
            return super().save(*args, **kwargs)
        with transaction.atomic():
            Firm.objects.filter(pk=self.pk).update(balance_version=models.F("balance_version") + 1)
            old = Firm.objects.select_for_update().get(pk=self.pk)
            if old.source_type != self.source_type and old.has_financial_history():
                raise ValidationError("A supplier with financial history cannot change source type.")
            if old.source_type != self.source_type and self.source_type == self.SourceType.LOCAL_MARKET and old.representatives.exists():
                raise ValidationError("A Local Market supplier cannot retain representatives.")
            self.balance_version = old.balance_version
            return super().save(*args, **kwargs)

    @property
    def current_credit(self):
        balance = self.current_debt()
        return abs(balance) if balance < 0 else 0

    # Controls how this firm appears in Django admin/dropdowns.
    def __str__(self):
        return f"{self.name} ({self.get_source_type_display()})"


# Representative means salesman/collector/rep.
# A representative belongs to exactly one firm.
class Representative(models.Model):
    # ForeignKey means this representative is connected to one Firm.
    # One firm can have many representatives.
    firm = models.ForeignKey(
        Firm,
        on_delete=models.CASCADE,
        related_name="representatives",
    )

    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=30, blank=True)

    # Stores when this representative was added to the system.
    # auto_now_add=True means Django fills this once when the rep is created.
    created_at = models.DateTimeField(auto_now_add=True)

    # Useful if a rep stops working but you do not want to delete old history.
    is_active = models.BooleanField(default=True)

    # Soft delete fields
    is_deleted = models.BooleanField(default=False, help_text="Soft delete flag")
    deleted_at = models.DateTimeField(null=True, blank=True, help_text="When this representative was soft deleted")

    # Validation rule:
    # local market dealings should not have representatives.
    def clean(self):
        super().clean()
        if self.pk and Representative.objects.filter(pk=self.pk).exclude(firm_id=self.firm_id).exists() and (self.bills.exists() or self.payments.exists()):
            raise ValidationError({"firm": "This representative has financial history. Create a new representative for the new affiliation."})
        if self.firm_id and self.firm.source_type == Firm.SourceType.LOCAL_MARKET:
            raise ValidationError("Local market firms should not have representatives.")

    def save(self, *args, **kwargs):
        old_firm = Representative.objects.filter(pk=self.pk).values_list("firm_id", flat=True).first() if self.pk else None
        with transaction.atomic():
            for firm_id in sorted({i for i in (old_firm, self.firm_id) if i is not None}):
                Firm.objects.filter(pk=firm_id).update(balance_version=models.F("balance_version") + 1)
            if self.pk:
                old = Representative.objects.select_for_update().get(pk=self.pk)
                if old.firm_id != self.firm_id and (old.bills.exists() or old.payments.exists()):
                    raise ValidationError("A representative with financial history cannot transfer suppliers. Create a new representative.")
            # Re-read inside the same account lock; the caller may hold an old
            # cached Firm object while another request changes its type.
            self.firm = Firm.objects.get(pk=self.firm_id)
            self.clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} - {self.firm.name}"


# Bill means invoice/purchase bill received from a firm.
# A bill increases the debt.
class BillQuerySet(models.QuerySet):
    def available(self):
        deleted_sources = LedgerEntry.objects.filter(entry_type="bill_created", is_deleted=True, bill_id__isnull=False).values("bill_id")
        return self.filter(Q(creation_batch__isnull=True) | Q(creation_batch__status="active")).exclude(pk__in=deleted_sources)


class Bill(models.Model):
    def save(self, *args, **kwargs):
        from .batch_context import current_batch
        if current_batch() is None:
            raise ValidationError("Use the supported bill posting or correction service.")
        return super().save(*args, **kwargs)

    objects = BillQuerySet.as_manager()
    creation_batch = models.ForeignKey(
        "TransactionBatch", on_delete=models.PROTECT, null=True, blank=True,
        related_name="created_bills", editable=False,
    )
    # Every bill belongs to one firm.
    firm = models.ForeignKey(
        Firm,
        on_delete=models.CASCADE,
        related_name="bills",
    )

    # The rep who brought this bill.
    # Optional because local market has no rep.
    representative = models.ForeignKey(
        Representative,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bills",
    )

    bill_number = models.CharField(max_length=100)
    bill_date = models.DateField(default=timezone.now)

    # Total amount of this new bill.
    bill_amount = models.DecimalField(max_digits=12, decimal_places=2)

    # Debt that already existed before this bill was added.
    # This helps display: previous debt + new bill = total due at that time.
    previous_debt_at_bill_time = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
    )

    notes = models.TextField(blank=True)

    # Default ordering when bills are shown.
    # The minus sign means newest first.
    class Meta:
        ordering = ["-bill_date", "-id"]
        constraints = [models.CheckConstraint(condition=Q(bill_amount__gt=0, bill_amount__lte=MAX_AMOUNT), name="bill_positive_supported_amount"), models.CheckConstraint(condition=Exact(Round("bill_amount", 2), models.F("bill_amount")), name="bill_exact_cents"), models.CheckConstraint(condition=Q(previous_debt_at_bill_time__gte=-MAX_AMOUNT, previous_debt_at_bill_time__lte=MAX_AMOUNT) & Exact(Round("previous_debt_at_bill_time", 2), models.F("previous_debt_at_bill_time")), name="bill_supported_snapshot")]

    # This is a calculated value, not a database column.
    @property
    def total_debt_at_bill_time(self):
        return self.previous_debt_at_bill_time + self.bill_amount

    # Validation rules for bills.
    def clean(self):
        # A rep selected for a bill must belong to the same firm as the bill.
        if self.representative and self.representative.firm_id != self.firm_id:
            raise ValidationError("Representative must belong to the selected firm.")

        # Local market bills should not have reps.
        if self.firm and self.firm.source_type == Firm.SourceType.LOCAL_MARKET:
            if self.representative:
                raise ValidationError("Local market bills should not have representatives.")

    def __str__(self):
        return f"{self.firm.name} - Bill {self.bill_number}"


# Payment means money paid to a firm.
# A payment decreases the debt.
class Payment(models.Model):
    def save(self, *args, **kwargs):
        from .batch_context import current_batch
        if current_batch() is None:
            raise ValidationError("Use the supported payment posting or correction service.")
        return super().save(*args, **kwargs)

    # Fixed dropdown choices for payment method.
    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank"
        OTHER = "other", "Other"

    # Every payment belongs to one firm.
    firm = models.ForeignKey(
        Firm,
        on_delete=models.CASCADE,
        related_name="payments",
    )

    # Optional link to a bill.
    # Payments may be made against the latest bill or just against total firm debt.
    bill = models.ForeignKey(
        Bill,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
    )

    # Optional rep who collected the payment.
    representative = models.ForeignKey(
        Representative,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
    )

    payment_date = models.DateField(default=timezone.now)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-payment_date", "-id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0, amount__lte=MAX_AMOUNT), name="payment_positive_supported_amount"), models.CheckConstraint(condition=Exact(Round("amount", 2), models.F("amount")), name="payment_exact_cents")]

    # Validation rules for payments.
    def clean(self):
        # The selected rep must belong to the selected firm.
        if self.representative and self.representative.firm_id != self.firm_id:
            raise ValidationError("Representative must belong to the selected firm.")

        # If a bill is selected, it must belong to the same firm.
        if self.bill and self.bill.firm_id != self.firm_id:
            raise ValidationError("Bill must belong to the selected firm.")

        # Local market payments should not have reps.
        if self.firm and self.firm.source_type == Firm.SourceType.LOCAL_MARKET:
            if self.representative:
                raise ValidationError("Local market payments should not have representatives.")

    def __str__(self):
        return f"{self.firm.name} payment - {self.amount}"


class TransactionBatch(models.Model):
    """One user action and its append-only reversal/restoration events."""

    class Kind(models.TextChoices):
        TRANSACTION = "transaction", "Transaction"
        REVERSAL = "reversal", "Reversal"
        RESTORATION = "restoration", "Restoration"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REVERSED = "reversed", "Reversed"

    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payload_hash = models.CharField(max_length=64, blank=True, default="")
    firm = models.ForeignKey(
        Firm,
        on_delete=models.PROTECT,
        related_name="transaction_batches",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pharmacy_transaction_batches",
    )
    date = models.DateField(default=timezone.localdate)
    previous_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.TRANSACTION)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    original_batch = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    reason = models.TextField(blank=True)
    # Captured only for new postings. Legacy original values are unknowable.
    posting_receipt = models.JSONField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    deletion_group = models.ForeignKey(
        "DeletionGroup", on_delete=models.PROTECT, null=True, blank=True,
        related_name="deleted_batches", editable=False,
    )

    class Meta:
        ordering = ["date", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(previous_balance__gte=-MAX_AMOUNT, previous_balance__lte=MAX_AMOUNT, balance_after__gte=-MAX_AMOUNT, balance_after__lte=MAX_AMOUNT), name="batch_supported_balances"),
            models.CheckConstraint(condition=Exact(Round("previous_balance", 2), models.F("previous_balance")) & Exact(Round("balance_after", 2), models.F("balance_after")), name="batch_exact_cent_balances"),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            original = TransactionBatch.objects.filter(pk=self.pk).values_list("posting_receipt", flat=True).first()
            if original is not None and original != self.posting_receipt:
                raise ValidationError("Original posting receipts are immutable.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk} - {self.firm.name}"


class DeletionGroup(models.Model):
    class Kind(models.TextChoices):
        TRANSACTION = "transaction", "Transaction"
        BILL = "bill", "Bill and linked payments"

    firm = models.ForeignKey(Firm, on_delete=models.PROTECT)
    bill = models.ForeignKey(Bill, on_delete=models.PROTECT, null=True, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.TRANSACTION)
    deleted_at = models.DateTimeField(default=timezone.now, db_index=True)
    restored_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    reason = models.CharField(max_length=500)


class BillEditEvent(models.Model):
    batch = models.ForeignKey(TransactionBatch, on_delete=models.PROTECT, related_name="edit_events")
    bill = models.ForeignKey(Bill, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    actor_snapshot = models.JSONField(null=True, editable=False)
    timestamp = models.DateTimeField(auto_now_add=True)
    before = models.JSONField()
    after = models.JSONField()

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Edit evidence is immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Edit evidence is immutable.")

class DeletionMember(models.Model):
    group = models.ForeignKey(DeletionGroup, on_delete=models.CASCADE, related_name="members")
    batch = models.ForeignKey(TransactionBatch, on_delete=models.PROTECT, related_name="deletion_memberships")
    reversal = models.ForeignKey(TransactionBatch, on_delete=models.PROTECT, related_name="deletion_offsets")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["group", "batch"], name="unique_deletion_member")]


# LedgerEntry is the financial history table.
# It records every increase and decrease in debt.
class LedgerEntry(models.Model):
    # Fixed dropdown choices for what kind of ledger event happened.
    class EntryType(models.TextChoices):
        OPENING_BALANCE = "opening_balance", "Opening Balance"
        BILL_CREATED = "bill_created", "Bill Created"
        PAYMENT_MADE = "payment_made", "Payment Made"
        ADJUSTMENT = "adjustment", "Adjustment"
        REVERSAL = "reversal", "Reversal"
        RESTORATION = "restoration", "Restoration"

    # Every ledger entry belongs to one firm.
    firm = models.ForeignKey(
        Firm,
        on_delete=models.CASCADE,
        related_name="ledger_entries",
    )

    entry_type = models.CharField(max_length=30, choices=EntryType.choices)
    date = models.DateField(default=timezone.now)

    # Optional link to the bill that created this ledger entry.
    bill = models.ForeignKey(
        Bill,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )

    # Optional link to the payment that created this ledger entry.
    payment = models.ForeignKey(
        Payment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )

    transaction_batch = models.ForeignKey(
        TransactionBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entries",
    )

    # increase means debt went up.
    # Example: new bill of 50,000.
    increase = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # decrease means debt went down.
    # Example: payment of 10,000.
    decrease = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    description = models.CharField(max_length=255, blank=True)

        # ADD THESE TWO LINES AFTER YOUR EXISTING FIELDS, BEFORE class Meta:
    is_deleted = models.BooleanField(default=False, help_text="Soft delete flag")
    deleted_at = models.DateTimeField(null=True, blank=True, help_text="When this entry was soft deleted")

    class Meta:
        ordering = ["date", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(increase__gte=0, increase__lte=MAX_AMOUNT, decrease__gte=0, decrease__lte=MAX_AMOUNT), name="ledger_nonnegative_supported_amounts"),
            models.CheckConstraint(condition=Q(increase=0) | Q(decrease=0), name="ledger_one_money_side"),
            models.CheckConstraint(condition=Exact(Round("increase", 2), models.F("increase")) & Exact(Round("decrease", 2), models.F("decrease")), name="ledger_exact_cents"),
        ]

    # Validation rules for ledger entries.
    def clean(self):
        # If linked to a bill, that bill must belong to the same firm.
        if self.bill and self.bill.firm_id != self.firm_id:
            raise ValidationError("Bill must belong to the selected firm.")

        # If linked to a payment, that payment must belong to the same firm.
        if self.payment and self.payment.firm_id != self.firm_id:
            raise ValidationError("Payment must belong to the selected firm.")

    def __str__(self):
        return f"{self.firm.name} - {self.get_entry_type_display()}"
