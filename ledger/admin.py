from django.contrib import admin
from django.db.models import Q, Sum

from .models import Firm, Representative, Bill, Payment, LedgerEntry, TransactionBatch


@admin.register(Firm)
class FirmAdmin(admin.ModelAdmin):
    list_display = ("name", "source_type", "phone", "remaining_debt")
    list_filter = ("source_type",)
    search_fields = ("name", "phone")
    readonly_fields = ("is_deleted", "deleted_at", "balance_version")

    def has_delete_permission(self, request, obj=None):
        return False

    def remaining_debt(self, obj):
        # This shows the firm's final/current debt right now.
        return obj.current_debt()

    remaining_debt.short_description = "Remaining Debt"


@admin.register(Representative)
class RepresentativeAdmin(admin.ModelAdmin):
    list_display = ("name", "firm", "phone", "is_active")
    list_filter = ("firm", "is_active")
    search_fields = ("name", "firm__name", "phone")
    readonly_fields = ("is_deleted", "deleted_at", "created_at")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Bill)
class BillAdmin(admin.ModelAdmin):
    list_display = (
        "bill_number",
        "firm",
        "representative",
        "bill_date",
        "bill_amount",
        "previous_debt_at_bill_time",
        "total_debt_at_bill_time",
    )
    list_filter = ("firm", "bill_date")
    search_fields = ("bill_number", "firm__name", "representative__name")

    def has_change_permission(self, request, obj=None):
        return obj is None

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "firm",
        "representative",
        "bill",
        "payment_date",
        "amount",
        "method",
    )
    list_filter = ("firm", "payment_date", "method")
    search_fields = ("firm__name", "representative__name", "bill__bill_number")

    def has_change_permission(self, request, obj=None):
        return obj is None

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "firm",
        "entry_type",
        "bill",
        "payment",
        "increase",
        "decrease",
        "running_balance",
    )
    list_filter = ("firm", "entry_type", "date")
    search_fields = ("firm__name", "description")
    readonly_fields = (
        "firm", "entry_type", "date", "bill", "payment", "transaction_batch",
        "increase", "decrease", "description", "is_deleted", "deleted_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def running_balance(self, obj):
        # obj means the current ledger row being displayed in admin.
        # Example: one specific bill entry or one specific payment entry.

        # We want the balance after THIS row, not the firm's final balance today.
        # So we collect all ledger entries for the same firm that happened:
        # 1. before this row's date
        # 2. or on the same date but with id less than or equal to this row
        entries_until_this_row = LedgerEntry.objects.filter(
            firm=obj.firm
        ).filter(
            Q(date__lt=obj.date) | Q(date=obj.date, id__lte=obj.id)
        )

        # Add all debt increases and debt decreases up to this row.
        totals = entries_until_this_row.aggregate(
            total_increase=Sum("increase"),
            total_decrease=Sum("decrease"),
        )

        # If no total exists, Django may return None.
        # The "or 0" prevents math errors.
        increase = totals["total_increase"] or 0
        decrease = totals["total_decrease"] or 0

        # Running balance means:
        # all increases so far - all decreases so far.
        return increase - decrease

    running_balance.short_description = "Running Balance"


@admin.register(TransactionBatch)
class TransactionBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "firm", "kind", "status", "created_by")
    list_filter = ("kind", "status", "date")
    search_fields = ("firm__name", "reason")
    readonly_fields = tuple(field.name for field in TransactionBatch._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
