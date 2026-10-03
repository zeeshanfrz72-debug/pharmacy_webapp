"""Read-only, explainable review signals from existing accounting records."""
from collections import defaultdict
from bisect import bisect_left
from datetime import datetime, time, timedelta
from statistics import median

from django.utils import timezone

from .models import Bill, TransactionBatch


def record_review(active_entries, today):
    start = today - timedelta(days=29)
    duplicates = defaultdict(list)
    for bill in Bill.objects.available().select_related("firm").order_by("-bill_date", "-id"):
        key = (bill.firm_id, bill.bill_number.strip().casefold(), bill.bill_date, bill.bill_amount)
        if key[1]:
            duplicates[key].append(bill)
    duplicate_groups = [bills for bills in duplicates.values() if len(bills) > 1]

    records = defaultdict(list)
    entries = active_entries.filter(
        entry_type__in=("bill_created", "payment_made"),
        date__gte=start - timedelta(days=90), date__lte=today,
    ).select_related("firm", "bill", "payment", "payment__bill").order_by("date", "id")
    # Each bill/payment is one observation, even if legacy data links it twice.
    seen = set()
    for entry in entries:
        key = (entry.entry_type, entry.bill_id if entry.entry_type == "bill_created" else entry.payment_id)
        if key[1] is None or key in seen:
            continue
        seen.add(key)
        records[(entry.firm_id, entry.entry_type)].append(entry)
    outliers, insufficient, recent_entry_count = [], 0, 0
    for series in records.values():
        dates = [entry.date for entry in series]
        for entry in series:
            if entry.date < start:
                continue
            recent_entry_count += 1
            previous = series[bisect_left(dates, entry.date - timedelta(days=90)):bisect_left(dates, entry.date)]
            if len(previous) < 10:
                insufficient += 1
                continue
            amount = entry.bill.bill_amount if entry.entry_type == "bill_created" else entry.payment.amount
            baseline = median(item.bill.bill_amount if item.entry_type == "bill_created" else item.payment.amount for item in previous)
            if amount > 3 * baseline:
                outliers.append({
                    "entry": entry, "amount": amount, "median": baseline,
                    "start": entry.date - timedelta(days=90), "end": entry.date - timedelta(days=1),
                    "sample_size": len(previous),
                })
    outliers.sort(key=lambda row: (row["entry"].date, row["entry"].pk), reverse=True)

    start_time = timezone.make_aware(datetime.combine(start, time.min))
    end_time = timezone.make_aware(datetime.combine(today + timedelta(days=1), time.min))
    events = list(TransactionBatch.objects.filter(
        kind__in=(TransactionBatch.Kind.REVERSAL, TransactionBatch.Kind.RESTORATION),
        created_at__gte=start_time, created_at__lt=end_time,
    ).select_related("firm", "original_batch", "original_batch__deletion_group").order_by("-created_at", "-id"))
    return {
        "review_start": start, "review_end": today,
        "duplicate_groups": duplicate_groups,
        "duplicate_bill_count": sum(len(group) for group in duplicate_groups),
        "outliers": outliers, "insufficient_count": insufficient,
        "recent_entry_count": recent_entry_count,
        "review_events": events,
        "deleted_event_count": sum(event.kind == TransactionBatch.Kind.REVERSAL for event in events),
        "recovered_event_count": sum(event.kind == TransactionBatch.Kind.RESTORATION for event in events),
        "affected_count": len({event.original_batch_id for event in events}),
    }
