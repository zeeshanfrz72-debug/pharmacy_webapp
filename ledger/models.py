from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone


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

    # Soft delete fields
    is_deleted = models.BooleanField(default=False, help_text="Soft delete flag")
    deleted_at = models.DateTimeField(null=True, blank=True, help_text="When this firm was soft deleted")

    # This calculates how much money is currently owed to this firm.
    # Formula: total bill/debt increases - total payment decreases.
    def current_debt(self):
        # Exclude soft-deleted ledger entries so deletions reduce the debt.
        totals = self.ledger_entries.filter(is_deleted=False).aggregate(
            total_increase=Sum("increase"),
            total_decrease=Sum("decrease"),
        )

        increase = totals["total_increase"] or 0
        decrease = totals["total_decrease"] or 0

        return increase - decrease

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
        if self.firm and self.firm.source_type == Firm.SourceType.LOCAL_MARKET:
            raise ValidationError("Local market firms should not have representatives.")

    def __str__(self):
        return f"{self.name} - {self.firm.name}"


# Bill means invoice/purchase bill received from a firm.
# A bill increases the debt.
class Bill(models.Model):
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


# LedgerEntry is the financial history table.
# It records every increase and decrease in debt.
class LedgerEntry(models.Model):
    # Fixed dropdown choices for what kind of ledger event happened.
    class EntryType(models.TextChoices):
        OPENING_BALANCE = "opening_balance", "Opening Balance"
        BILL_CREATED = "bill_created", "Bill Created"
        PAYMENT_MADE = "payment_made", "Payment Made"
        ADJUSTMENT = "adjustment", "Adjustment"

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