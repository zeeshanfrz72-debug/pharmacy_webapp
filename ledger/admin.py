from django.contrib import admin
from django.db.models import Q, Sum

from .models import Firm, Representative, Bill, Payment, LedgerEntry, TransactionBatch, DeletionGroup, DeletionMember
from .models import BillEditEvent, BillCarryForward
from .money import balance


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
    def has_add_permission(self, request):
        return False
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
    def has_add_permission(self, request):
        return False
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
            firm=obj.firm, is_deleted=False
        ).filter(
            Q(date__lt=obj.date) | Q(date=obj.date, id__lte=obj.id)
        )

        # Add all debt increases and debt decreases up to this row.
        return balance(entries_until_this_row)

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


@admin.register(DeletionGroup)
class DeletionGroupAdmin(TransactionBatchAdmin):
    list_display = ("id", "firm", "kind", "deleted_at", "restored_at")
    list_filter = ("kind", "deleted_at")
    readonly_fields = tuple(field.name for field in DeletionGroup._meta.fields)


@admin.register(DeletionMember)
class DeletionMemberAdmin(TransactionBatchAdmin):
    list_display = ("group", "batch", "reversal")
    list_filter = ()
    search_fields = ("group__firm__name",)
    readonly_fields = tuple(field.name for field in DeletionMember._meta.fields)


@admin.register(BillEditEvent)
class BillEditEventAdmin(TransactionBatchAdmin):
    list_display = ("id", "batch", "bill", "actor", "timestamp")
    list_filter = ("timestamp",)
    search_fields = ("bill__bill_number",)
    readonly_fields = tuple(field.name for field in BillEditEvent._meta.fields)


@admin.register(BillCarryForward)
class BillCarryForwardAdmin(TransactionBatchAdmin):
    list_display = ("id", "source", "destination", "amount", "created_at")
    list_filter = ("created_at",)
    search_fields = ("source__bill_number", "destination__bill_number")
    readonly_fields = tuple(field.name for field in BillCarryForward._meta.fields)
