import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from .money import MoneySum as Sum, balance, cents
from django.db.models.functions import Coalesce, TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET

from .forms import BillEditForm, BillForm, FirmForm, LedgerTransactionForm, RepresentativeForm
from .models import Bill, DeletionGroup, Firm, LedgerEntry, Representative, TransactionBatch
from .services import (
    IdempotencyConflict,
    committed_retry,
    delete_bill,
    recover_deletion_group,
    create_transaction_batch,
    restore_transaction_batch,
    reverse_transaction_batch,
)


def _active_activity_entries():
    """Source for operational dashboard activity, excluding reversed batches."""
    return LedgerEntry.objects.filter(is_deleted=False).filter(
        Q(transaction_batch__isnull=True)
        | Q(
            transaction_batch__kind=TransactionBatch.Kind.TRANSACTION,
            transaction_batch__status=TransactionBatch.Status.ACTIVE,
        )
    )


def _total_net_debt():
    return balance(LedgerEntry.objects.filter(is_deleted=False))


def _supplier_credit_total():
    return sum((abs(balance) for balance in _firm_balances().values() if balance < 0), Decimal("0.00"))


def _firm_balances():
    return {
        row["firm_id"]: (row["increase"] or Decimal("0.00")) - (row["decrease"] or Decimal("0.00"))
        for row in LedgerEntry.objects.filter(is_deleted=False).values("firm_id").annotate(
            increase=Sum("increase"), decrease=Sum("decrease")
        ).order_by()
    }


def _dashboard_payment_totals(today):
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    payment_entries = _active_activity_entries().filter(
        entry_type=LedgerEntry.EntryType.PAYMENT_MADE
    )

    aggregates = {}
    for period, start in (("today", today), ("week", week_start), ("month", month_start)):
        bounds = Q(date__gte=start, date__lte=today)
        aggregates[period + "_payment_count"] = Count("id", filter=bounds)
        aggregates[period + "_payment_total"] = Sum(Coalesce("payment__amount", "decrease"), filter=bounds)
    totals = payment_entries.aggregate(**aggregates)
    return {key: value if value is not None else Decimal("0.00") for key, value in totals.items()}


def _request_fingerprint(post_data):
    values = {
        key: post_data.getlist(key)
        for key in sorted(post_data.keys())
        if key not in {"csrfmiddlewaretoken", "request_id"}
    }
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _transaction_json(summary, totals):
    return JsonResponse(
        {
            "success": True,
            "status": "created" if summary["created"] else "duplicate",
            "batch_id": summary["batch"].pk,
            "firm": summary["firm"].name,
            "bill_number": summary["bill"].bill_number if summary["bill"] else None,
            "payment_amount": str(summary["payment"].amount) if summary["payment"] else None,
            "previous_debt": str(summary["previous_debt"]) if summary["previous_debt"] is not None else None,
            "remaining_debt": str(summary["remaining_debt"]) if summary["remaining_debt"] is not None else None,
            "action_state": summary["batch"].status,
            "posting_receipt": summary["batch"].posting_receipt,
            "total_debt": str(_total_net_debt()),
            **{key: str(value) if key.endswith("_total") else value for key, value in totals.items()},
        }
    )


def _page_views(request):
    """Keep list filters while switching between active records and their Trash."""
    query = request.GET.copy()
    query.pop("view", None)
    query.pop("ajax", None)
    records_link = request.path + ("?" + query.urlencode() if query else "")
    query["view"] = "trash"
    return {"is_trash": request.method == "GET" and request.GET.get("view") == "trash",
            "records_link": records_link, "trash_link": request.path + "?" + query.urlencode()}


@login_required
def add_firm(request):
    form = FirmForm(request.POST or None, prefix="firm")
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Firm added.")
        return redirect("ledger:add_firm")
    balances = _firm_balances()
    all_firms = list(Firm.objects.order_by("name"))
    for firm in all_firms:
        firm.display_balance = balances.get(firm.pk, Decimal("0.00"))
        firm.display_credit = abs(firm.display_balance)
    firms = [firm for firm in all_firms if not firm.is_deleted]
    trashed_firms = [firm for firm in all_firms if firm.is_deleted]
    return render(
        request,
        "ledger/add_firm.html",
        {
            **_page_views(request),
            "form": form,
            "firms": firms,
            "trashed_firms": trashed_firms,
            "total_debt": _total_net_debt(),
        },
    )


def _bill_delete_previews(bills):
    """Build bill deletion previews in bulk, including complete linked batches."""
    ids = [bill.pk for bill in bills]
    batches = TransactionBatch.objects.filter(
        kind=TransactionBatch.Kind.TRANSACTION, status=TransactionBatch.Status.ACTIVE,
    ).filter(Q(created_bills__pk__in=ids) | Q(entries__payment__bill_id__in=ids)).distinct().prefetch_related(
        "entries__payment", "created_bills"
    ).order_by("id")
    affected = defaultdict(list)
    for batch in batches:
        linked = {bill.pk for bill in batch.created_bills.all()}
        linked.update(entry.payment.bill_id for entry in batch.entries.all() if entry.payment_id)
        for bill_id in linked.intersection(ids):
            affected[bill_id].append(batch)
    for bill in bills:
        roots = affected[bill.pk]
        bill.delete_count = len(roots)
        bill.delete_ids = ", ".join(str(root.pk) for root in roots)
        bill.delete_effect = sum((entry.decrease - entry.increase for root in roots for entry in root.entries.all() if not entry.is_deleted), Decimal("0.00"))
    return bills


@login_required
@require_GET
def firm_details(request, firm_id):
    firm = get_object_or_404(Firm, pk=firm_id)
    today = timezone.localdate()
    week_start, month_start = today - timedelta(days=today.weekday()), today.replace(day=1)
    batches = TransactionBatch.objects.filter(
        firm=firm, kind=TransactionBatch.Kind.TRANSACTION, status=TransactionBatch.Status.ACTIVE,
        entries__is_deleted=False, entries__entry_type__in=("bill_created", "payment_made"),
    )
    counts = batches.aggregate(
        today=Count("id", distinct=True, filter=Q(date=today)),
        week=Count("id", distinct=True, filter=Q(date__gte=week_start, date__lte=today)),
        month=Count("id", distinct=True, filter=Q(date__gte=month_start, date__lte=today)),
    )
    page = Paginator(firm.bills.available().select_related("firm", "representative").order_by("-bill_date", "-id"), 20).get_page(request.GET.get("page"))
    page.object_list = _bill_delete_previews(list(page.object_list))
    context = {
        "firm": firm, "bills_page": page, "counts": counts, "today": today,
        "week_start": week_start, "month_start": month_start,
    }
    template = "ledger/_firm_details.html" if request.headers.get("x-requested-with") == "XMLHttpRequest" else "ledger/firm_details.html"
    return render(request, template, context)


@login_required
@require_GET
def dashboard_review(request):
    from .insights import record_review

    context = record_review(_active_activity_entries(), timezone.localdate())
    template = "ledger/_record_review.html" if request.headers.get("x-requested-with") == "XMLHttpRequest" else "ledger/record_review.html"
    return render(request, template, context)


@login_required
def add_representative(request):
    form = RepresentativeForm(request.POST or None, prefix="add_rep")
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Representative added.")
        return redirect("ledger:add_representative")
    selected_source_type = request.GET.get("source_type", "")
    selected_firm_id = request.GET.get("firm", "")

    representatives = Representative.objects.filter(
        is_deleted=False,
        firm__is_deleted=False,
    ).select_related("firm").order_by("firm__source_type", "firm__name", "name")

    if selected_source_type:
        representatives = representatives.filter(firm__source_type=selected_source_type)
    if selected_firm_id:
        representatives = representatives.filter(firm_id=selected_firm_id)

    firm_filter_options = Firm.objects.filter(is_deleted=False).order_by("name")
    if selected_source_type:
        firm_filter_options = firm_filter_options.filter(source_type=selected_source_type)

    trashed_representatives = Representative.objects.filter(
        Q(is_deleted=True) | Q(firm__is_deleted=True)
    ).select_related("firm").order_by("firm__name", "name")
    if selected_source_type:
        trashed_representatives = trashed_representatives.filter(firm__source_type=selected_source_type)
    if selected_firm_id:
        trashed_representatives = trashed_representatives.filter(firm_id=selected_firm_id)

    return render(
        request,
        "ledger/add_representative.html",
        {
            **_page_views(request),
            "add_rep_form": form,
            "source_type_choices": Firm.SourceType.choices,
            "selected_source_type": selected_source_type,
            "selected_firm_id": selected_firm_id,
            "firm_filter_options": firm_filter_options,
            "representatives": representatives,
            "trashed_representatives": trashed_representatives,
        },
    )


@login_required
def toggle_representative_active(request, representative_id):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required."}, status=405)
    representative = get_object_or_404(
        Representative.objects.select_related("firm"),
        pk=representative_id,
        is_deleted=False,
        firm__is_deleted=False,
    )
    representative.is_active = not representative.is_active
    representative.save(update_fields=["is_active"])
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"id": representative.pk, "is_active": representative.is_active})
    return redirect("ledger:add_representative")


@login_required
def firms_for_source_type(request):
    source_type = request.GET.get("source_type")
    matching_firms = Firm.objects.filter(is_deleted=False)
    if source_type:
        matching_firms = matching_firms.filter(source_type=source_type)
    return JsonResponse({
        "firms": list(matching_firms.order_by("name").values("id", "name")),
    })


@login_required
def representatives_for_firm(request):
    firm_id = request.GET.get("firm_id")
    firm = Firm.objects.filter(pk=firm_id, is_deleted=False).first() if firm_id else None
    representatives = []
    bills = []
    source_type = ""
    if firm:
        source_type = firm.source_type
        representatives = list(
            Representative.objects.filter(
                firm=firm,
                is_active=True,
                is_deleted=False,
            ).order_by("name").values("id", "name")
        )
        bills = [
            {
                "id": bill.pk,
                "bill_number": bill.bill_number,
                "bill_amount": str(bill.bill_amount),
                "bill_date": bill.bill_date.strftime("%d-%m-%Y"),
                "is_latest": index == 0,
            }
            for index, bill in enumerate(firm.bills.available().order_by("-bill_date", "-id"))
        ]
    return JsonResponse({
        "representatives": representatives,
        "source_type": source_type,
        "bills": bills,
    })


@login_required
def add_transaction(request):
    saved_summary = None
    if request.method == "POST":
        try:
            saved_summary = committed_retry(request.POST.get("request_id"), user=request.user, payload_hash=_request_fingerprint(request.POST))
        except IdempotencyConflict as exc:
            return JsonResponse({"success": False, "error": str(exc)}, status=409)
        if saved_summary:
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return _transaction_json(saved_summary, _dashboard_payment_totals(timezone.localdate()))
            return redirect("ledger:add_transaction")
        if request.headers.get("X-Offline-Sync") == "1" and request.headers.get("X-Ledger-Queue-Version") != "3":
            return JsonResponse({"success": False, "error": "Update this page before synchronizing queued transactions. Pending data remains on this device."}, status=428)
        form = LedgerTransactionForm(request.POST)
        if form.is_valid():
            try:
                saved_summary = form.save(
                    user=request.user,
                    payload_hash=_request_fingerprint(request.POST),
                )
            except (ValidationError, IdempotencyConflict) as exc:
                form.add_error(None, exc)
                if request.headers.get("x-requested-with") == "XMLHttpRequest":
                    return JsonResponse(
                        {"success": False, "error": str(exc)},
                        status=409 if isinstance(exc, IdempotencyConflict) else 400,
                    )
            else:
                if request.headers.get("x-requested-with") == "XMLHttpRequest":
                    today = timezone.localdate()
                    return _transaction_json(saved_summary, _dashboard_payment_totals(today))
                form = LedgerTransactionForm()
        elif request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse(
                {"success": False, "errors": form.errors.get_json_data()},
                status=400,
            )
    else:
        form = LedgerTransactionForm()

    today = timezone.localdate()
    totals = _dashboard_payment_totals(today)
    firms_by_source = defaultdict(list)
    for firm in Firm.objects.filter(is_deleted=False).order_by("name"):
        firms_by_source[firm.source_type].append({"id": firm.pk, "name": firm.name})

    reps_by_firm = defaultdict(list)
    for rep in Representative.objects.filter(
        is_active=True,
        is_deleted=False,
        firm__is_deleted=False,
    ).order_by("name"):
        reps_by_firm[rep.firm_id].append({"id": rep.pk, "name": rep.name})

    bills_by_firm = defaultdict(list)
    firm_bill_counts = defaultdict(int)
    for bill in Bill.objects.available().select_related("firm").filter(
        firm__is_deleted=False,
    ).order_by("-bill_date", "-id"):
        bills_by_firm[bill.firm_id].append({
            "id": bill.pk,
            "bill_number": bill.bill_number,
            "bill_amount": str(bill.bill_amount),
            "bill_date": bill.bill_date.strftime("%d-%m-%Y"),
            "is_latest": firm_bill_counts[bill.firm_id] == 0,
        })
        firm_bill_counts[bill.firm_id] += 1

    context = {
        "form": form,
        "firm_form": FirmForm(auto_id="ajax-firm-%s"),
        "rep_form": RepresentativeForm(auto_id="ajax-rep-%s"),
        "saved_summary": saved_summary,
        "history_rows": _history_rows(limit=10, activity_only=True),
        "compact_history": True,
        "history_id_prefix": "recent-",
        "history_return_url": request.path,
        **totals,
        "total_debt": _total_net_debt(),
        "supplier_credit_total": _supplier_credit_total(),
        "today_date": today.isoformat(),
        "offline_data_json": json.dumps({
            "user_id": request.user.pk,
            "firms_by_source": dict(firms_by_source),
            "reps_by_firm": dict(reps_by_firm),
            "bills_by_firm": dict(bills_by_firm),
        }),
    }
    return render(request, "ledger/add_transaction.html", context)


@login_required
def ledger_history(request):
    try:
        def parse_date(value):
            from datetime import datetime
            return datetime.strptime(value, "%d-%m-%Y").date() if len(value) == 10 and value[2] == "-" else date.fromisoformat(value)
        start_date = parse_date(request.GET["start_date"]) if request.GET.get("start_date") else None
        end_date = parse_date(request.GET["end_date"]) if request.GET.get("end_date") else None
    except ValueError:
        return HttpResponse("Invalid date filter.", status=400)

    firm = None
    if request.GET.get("firm_id"):
        try:
            firm = get_object_or_404(Firm, pk=int(request.GET["firm_id"]))
        except ValueError:
            return HttpResponse("Invalid firm filter.", status=400)
    history_rows = _history_rows(firm=firm, start_date=start_date, end_date=end_date)
    context = {
        **_page_views(request),
        "history_rows": history_rows,
        "transaction_trash_groups": _deletion_groups_for_page(firm=firm, start_date=start_date, end_date=end_date),
        "total_debt": _total_net_debt(),
        "start_date": start_date.strftime("%d-%m-%Y") if start_date else "",
        "end_date": end_date.strftime("%d-%m-%Y") if end_date else "",
        "selected_firm": firm,
        "history_id_prefix": "modal-" if request.GET.get("ajax") else "",
    }
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("ajax"):
        return HttpResponse(render_to_string("ledger/_history_table.html", context, request=request))
    return render(request, "ledger/ledger_history.html", context)


def _history_rows(*, firm=None, start_date=None, end_date=None, limit=None, activity_only=False):
    entries = _active_activity_entries().select_related(
        "firm", "bill", "bill__representative", "payment", "payment__bill",
        "payment__representative", "transaction_batch", "transaction_batch__deletion_group",
    ).prefetch_related("transaction_batch__audit_events", "transaction_batch__edit_events").order_by("date", "transaction_batch_id", "id")

    if firm:
        entries = entries.filter(firm=firm)
    balances = defaultdict(lambda: Decimal("0.00"))
    if limit:
        # Choose action identities, then retrieve their complete entries. Same-day
        # histories stay bounded even when thousands of actions share a date.
        roots = TransactionBatch.objects.filter(kind="transaction", status="active",
            entries__is_deleted=False, entries__entry_type__in=("bill_created", "payment_made"))
        if firm:
            roots = roots.filter(firm=firm)
        candidates = [(day, pk, "batch") for day, pk in roots.order_by("-date", "-id").values_list("date", "id").distinct()[:limit]]
        candidates += [(day, pk, "entry") for day, pk in entries.filter(transaction_batch__isnull=True,
            entry_type__in=("bill_created", "payment_made")).order_by("-date", "-id").values_list("date", "id")[:limit]]
        candidates.sort(key=lambda x: (x[0], x[1] if x[2] == "batch" else 0, x[1]), reverse=True)
        candidates = candidates[:limit]
        if not candidates:
            return []
        selected = Q(transaction_batch_id__in=[pk for _, pk, kind in candidates if kind == "batch"]) | Q(pk__in=[pk for _, pk, kind in candidates if kind == "entry"])
        entries = entries.filter(selected)
    grouped_rows = {}
    for entry in entries:
        batch = entry.transaction_batch
        if limit and batch and ("batch", batch.pk) not in grouped_rows:
            earlier = _active_activity_entries().filter(firm_id=entry.firm_id).filter(Q(date__lt=entry.date) | Q(date=entry.date, transaction_batch__isnull=True) | Q(date=entry.date, transaction_batch_id__lt=batch.pk))
            balances[entry.firm_id] = balance(earlier)
        elif limit and not batch:
            earlier = _active_activity_entries().filter(firm_id=entry.firm_id).filter(Q(date__lt=entry.date) | Q(date=entry.date, transaction_batch__isnull=True, id__lt=entry.pk))
            balances[entry.firm_id] = balance(earlier)
        balances[entry.firm_id] += Decimal(entry.increase) - Decimal(entry.decrease)
        row_key = ("batch", batch.pk) if batch else ("entry", entry.pk)
        row_date = batch.date if batch else entry.date
        row = grouped_rows.setdefault(row_key, {
            "batch": batch,
            "deleted": False,
            "group": None,
            "batch_id": batch.pk if batch else None,
            "date": row_date,
            "day": row_date.strftime("%A"),
            "firm": entry.firm,
            "source_type": entry.firm.get_source_type_display(),
            "entries": [],
            "representative_names": [],
            "related_bill_numbers": [],
            "payment_made": Decimal("0.00"),
            "bill_amount": Decimal("0.00"),
            "remaining_debt": Decimal("0.00"),
        })
        row["entries"].append(entry)
        row["remaining_debt"] = balances[entry.firm_id]
        if entry.entry_type == LedgerEntry.EntryType.PAYMENT_MADE:
            row["payment_made"] += Decimal(entry.decrease)
        if entry.entry_type == LedgerEntry.EntryType.BILL_CREATED:
            row["bill_amount"] += Decimal(entry.increase)
        if entry.entry_type == LedgerEntry.EntryType.ADJUSTMENT:
            if entry.bill_id:
                row["bill_amount"] += entry.increase - entry.decrease
            if entry.payment_id:
                row["payment_made"] += entry.decrease - entry.increase
        for source in (entry.bill, entry.payment):
            if source:
                rep = getattr(source, "representative", None)
                if rep and rep.name not in row["representative_names"]:
                    row["representative_names"].append(rep.name)
                bill = source if isinstance(source, Bill) else getattr(source, "bill", None)
                if bill and bill.bill_number not in row["related_bill_numbers"]:
                    row["related_bill_numbers"].append(bill.bill_number)

    history_rows = []
    for row in grouped_rows.values():
        if activity_only and not any(entry.entry_type in ("bill_created", "payment_made") for entry in row["entries"]):
            continue
        if start_date and row["date"] < start_date:
            continue
        if end_date and row["date"] > end_date:
            continue
        row["entries"].sort(key=lambda item: item.pk)
        row["delete_effect"] = sum((entry.decrease - entry.increase for entry in row["entries"]), Decimal("0.00"))
        row["is_reversible"] = bool(
            row["batch"] and row["batch"].kind == TransactionBatch.Kind.TRANSACTION
        )
        history_rows.append(row)
    history_rows.sort(key=lambda row: (row["date"], row["batch_id"] or 0), reverse=True)
    return history_rows[:limit] if limit else history_rows


@login_required
@require_GET
def dashboard_recent(request):
    context = {
        "history_rows": _history_rows(limit=10, activity_only=True),
        "compact_history": True,
        "history_id_prefix": "recent-",
        "history_return_url": "/ledger/add-transaction/",
    }
    return JsonResponse({
        "html": render_to_string("ledger/_history_table.html", context, request=request),
        "total_debt": str(_total_net_debt()),
        "supplier_credit_total": str(_supplier_credit_total()),
        "today_date": timezone.localdate().isoformat(),
        **{key: str(value) if key.endswith("_total") else value for key, value in _dashboard_payment_totals(timezone.localdate()).items()},
    })


def _period_bounds(period):
    today = timezone.localdate()
    if period == "week":
        return today - timedelta(days=today.weekday()), today, "day"
    if period == "month":
        return today.replace(day=1), today, "day"
    if period == "year":
        return today.replace(month=1, day=1), today, "month"
    if period == "5years":
        return today.replace(year=today.year - 4, month=1, day=1), today, "month"
    return today.replace(day=1), today, "day"


def _day_series(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _month_series(start, end):
    current = start.replace(day=1)
    end_month = end.replace(day=1)
    while current <= end_month:
        yield current
        current = current.replace(year=current.year + (current.month == 12), month=1 if current.month == 12 else current.month + 1)


@login_required
def dashboard_payments_trend(request):
    start, end, grain = _period_bounds(request.GET.get("period", "month"))
    qs = _active_activity_entries().filter(
        entry_type=LedgerEntry.EntryType.PAYMENT_MADE,
        date__gte=start,
        date__lte=end,
    )
    labels, values = [], []
    if grain == "day":
        grouped = {
            row["date"]: (row["total"] or Decimal("0.00"))
            for row in qs.values("date").annotate(total=Sum(Coalesce("payment__amount", "decrease"))).order_by("date")
        }
        for day in _day_series(start, end):
            labels.append(day.strftime("%d-%m-%Y"))
            values.append(grouped.get(day, 0))
    else:
        grouped = {}
        for row in qs.annotate(bucket=TruncMonth("date")).values("bucket").annotate(total=Sum(Coalesce("payment__amount", "decrease"))):
            bucket = row["bucket"].date() if hasattr(row["bucket"], "date") else row["bucket"]
            grouped[bucket.replace(day=1)] = (row["total"] or Decimal("0.00"))
        for month in _month_series(start, end):
            labels.append(month.strftime("%b %Y"))
            values.append(grouped.get(month, 0))
    return JsonResponse({"labels": labels, "values": [float(value) for value in values], "monetary_values": [format(value, ".2f") for value in values]})


@login_required
def dashboard_business_trend(request):
    start, end, grain = _period_bounds(request.GET.get("period", "month"))
    qs = _active_activity_entries().filter(
        entry_type=LedgerEntry.EntryType.BILL_CREATED,
        date__gte=start,
        date__lte=end,
    )
    labels, count_values, amount_values = [], [], []
    if grain == "day":
        grouped = {
            row["date"]: {"count": row["count"], "amount": (row["total"] or Decimal("0.00"))}
            for row in qs.values("date").annotate(count=Count("bill_id", distinct=True), total=Sum(Coalesce("bill__bill_amount", "increase")))
        }
        for day in _day_series(start, end):
            data = grouped.get(day, {"count": 0, "amount": 0})
            labels.append(day.strftime("%d-%m-%Y"))
            count_values.append(data["count"])
            amount_values.append(data["amount"])
    else:
        grouped = {}
        for row in qs.annotate(bucket=TruncMonth("date")).values("bucket").annotate(
            count=Count("bill_id", distinct=True), total=Sum(Coalesce("bill__bill_amount", "increase"))
        ):
            bucket = row["bucket"].date() if hasattr(row["bucket"], "date") else row["bucket"]
            grouped[bucket.replace(day=1)] = {"count": row["count"], "amount": (row["total"] or Decimal("0.00"))}
        for month in _month_series(start, end):
            data = grouped.get(month, {"count": 0, "amount": 0})
            labels.append(month.strftime("%b %Y"))
            count_values.append(data["count"])
            amount_values.append(data["amount"])
    return JsonResponse({"labels": labels, "count_values": count_values, "amount_values": [float(value) for value in amount_values], "monetary_amount_values": [format(value, ".2f") for value in amount_values]})


@login_required
def dashboard_debt_over_time(request):
    start, end, grain = _period_bounds(request.GET.get("period", "year"))
    balances = defaultdict(lambda: Decimal("0.00"))
    before = LedgerEntry.objects.filter(is_deleted=False, date__lt=start).order_by("date", "id")
    for entry in before:
        balances[entry.firm_id] += Decimal(entry.increase) - Decimal(entry.decrease)
    current_total = sum(balances.values(), Decimal("0.00"))
    entries = LedgerEntry.objects.filter(is_deleted=False, date__gte=start, date__lte=end).order_by("date", "id")
    labels, values = [], []
    if grain == "day":
        by_day = defaultdict(list)
        for entry in entries:
            by_day[entry.date].append(entry)
        for day in _day_series(start, end):
            for entry in by_day[day]:
                balances[entry.firm_id] += Decimal(entry.increase) - Decimal(entry.decrease)
            current_total = sum(balances.values(), Decimal("0.00"))
            labels.append(day.strftime("%d-%m-%Y"))
            values.append(current_total)
    else:
        by_month = defaultdict(list)
        for entry in entries:
            by_month[entry.date.replace(day=1)].append(entry)
        for month in _month_series(start, end):
            for entry in by_month[month]:
                balances[entry.firm_id] += Decimal(entry.increase) - Decimal(entry.decrease)
            current_total = sum(balances.values(), Decimal("0.00"))
            labels.append(month.strftime("%b %Y"))
            values.append(current_total)
    return JsonResponse({"labels": labels, "values": [float(value) for value in values], "monetary_values": [format(value, ".2f") for value in values]})


def _batch_action_response(request, batch_id, operation):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required."}, status=405)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = "ledger:ledger_history"

    def action_error(message, status=400):
        if is_ajax:
            return JsonResponse({"success": False, "error": message}, status=status)
        messages.error(request, message)
        return redirect(next_url)

    reason = request.POST.get("reason", "").strip()
    if not reason:
        return action_error("A reason is required.")
    if len(reason) > 500:
        return action_error("The reason must be 500 characters or fewer.")
    try:
        result = operation(batch_id, user=request.user, reason=reason)
    except ValidationError as exc:
        return action_error(" ".join(exc.messages))
    if is_ajax:
        return JsonResponse({
            "success": True,
            "status": "reversed" if operation in (reverse_transaction_batch, delete_bill) else "restored",
            "changed": result["changed"],
            "total_debt": str(_total_net_debt()),
        })
    messages.success(
        request,
        "Moved to Trash. You can recover this item at any time."
        if operation in (reverse_transaction_batch, delete_bill)
        else "Recovered from Trash. The item's financial effect has been restored.",
    )
    return redirect(next_url)


@login_required
def reverse_batch(request, batch_id):
    return _batch_action_response(request, batch_id, reverse_transaction_batch)


@login_required
def restore_batch(request, batch_id):
    return _batch_action_response(request, batch_id, restore_transaction_batch)


@login_required
def dashboard_debt_breakdown(request):
    firm_rows, credit_rows = [], []
    source_totals = defaultdict(Decimal)
    balances = _firm_balances()
    for firm in Firm.objects.all().order_by("name"):
        balance = balances.get(firm.pk, Decimal("0.00"))
        if balance > 0:
            firm_rows.append({"label": firm.name, "value": float(balance)})
        elif balance < 0:
            credit_rows.append({"label": firm.name, "value": float(abs(balance))})
        source_totals[firm.source_type] += balance
    firm_rows.sort(key=lambda row: row["value"], reverse=True)
    credit_rows.sort(key=lambda row: row["value"], reverse=True)
    source_rows = [
        {"label": label, "value": float(source_totals[source])}
        for source, label in Firm.SourceType.choices
        if source_totals[source] > 0
    ]
    return JsonResponse({"firms": firm_rows, "credits": credit_rows, "source_types": source_rows})


@login_required
def ajax_add_firm(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required."}, status=405)
    form = FirmForm(request.POST)
    if form.is_valid():
        firm = form.save()
        return JsonResponse({
            "success": True,
            "id": firm.pk,
            "name": firm.name,
            "source_type": firm.source_type,
        })
    return JsonResponse({"success": False, "errors": form.errors.get_json_data()}, status=400)


@login_required
def ajax_add_representative(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required."}, status=405)
    form = RepresentativeForm(request.POST)
    if form.is_valid():
        rep = form.save()
        if request.POST.get("deactivate_previous") == "yes":
            Representative.objects.filter(firm=rep.firm, is_active=True).exclude(pk=rep.pk).update(is_active=False)
        active_representatives = list(
            rep.firm.representatives.filter(is_active=True, is_deleted=False)
            .order_by("name")
            .values("id", "name")
        )
        return JsonResponse({
            "success": True,
            "id": rep.pk,
            "name": rep.name,
            "firm_id": rep.firm_id,
            "firm_name": rep.firm.name,
            "source_type": rep.firm.source_type,
            "representatives": active_representatives,
        })
    return JsonResponse({"success": False, "errors": form.errors.get_json_data()}, status=400)


@login_required
def edit_firm(request, firm_id):
    firm = get_object_or_404(Firm, pk=firm_id, is_deleted=False)
    form = FirmForm(request.POST or None, instance=firm)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return redirect("ledger:add_firm")
    return render(request, "ledger/edit_firm.html", {"form": form, "firm": firm})


@login_required
def delete_firm(request, firm_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    firm = get_object_or_404(Firm, pk=firm_id, is_deleted=False)
    firm.is_deleted = True
    firm.deleted_at = timezone.now()
    firm.save(update_fields=["is_deleted", "deleted_at"])
    messages.success(request, "Firm moved to Trash. Its balance and transaction history are retained.")
    return redirect("ledger:add_firm")


@login_required
def restore_firm(request, firm_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    firm = get_object_or_404(Firm, pk=firm_id, is_deleted=True)
    firm.is_deleted = False
    firm.deleted_at = None
    firm.save(update_fields=["is_deleted", "deleted_at"])
    messages.success(request, "Firm recovered from Trash.")
    return _record_recovery_redirect(request, "ledger:add_firm")


@login_required
def edit_representative(request, rep_id):
    rep = get_object_or_404(Representative, pk=rep_id, is_deleted=False, firm__is_deleted=False)
    form = RepresentativeForm(request.POST or None, instance=rep)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return redirect("ledger:add_representative")
    return render(request, "ledger/edit_representative.html", {"form": form, "representative": rep})


@login_required
def delete_representative(request, rep_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    rep = get_object_or_404(Representative, pk=rep_id, is_deleted=False)
    rep.is_deleted = True
    rep.deleted_at = timezone.now()
    rep.save(update_fields=["is_deleted", "deleted_at"])
    messages.success(request, "Representative moved to Trash. Historical transaction links are retained.")
    return redirect("ledger:add_representative")


@login_required
def restore_representative(request, rep_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    rep = get_object_or_404(Representative, pk=rep_id, is_deleted=True)
    rep.is_deleted = False
    rep.deleted_at = None
    rep.save(update_fields=["is_deleted", "deleted_at"])
    messages.success(request, "Representative recovered from Trash.")
    return _record_recovery_redirect(request, "ledger:add_representative")


def _record_recovery_redirect(request, default_route):
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(default_route)


@login_required
def bills_page(request):
    retry_error = None
    if request.method == "POST":
        try:
            retry = committed_retry(request.POST.get("bill-request_id"), user=request.user, payload_hash=_request_fingerprint(request.POST))
        except IdempotencyConflict as exc:
            retry_error = exc
            retry = None
        if retry:
            messages.success(request, "This bill was already saved.")
            return redirect("ledger:bills")
    form = BillForm(request.POST or None, prefix="bill")
    if retry_error:
        form.add_error(None, retry_error)
    if request.method == "POST" and form.is_valid():
        try:
            summary = form.save(user=request.user, payload_hash=_request_fingerprint(request.POST))
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Bill added." if summary["created"] else "This bill was already saved.")
            return redirect("ledger:bills")
    bills = list(Bill.objects.available().select_related("firm", "representative").order_by("-bill_date", "-id"))
    _bill_delete_previews(bills)
    bill_groups = DeletionGroup.objects.filter(restored_at__isnull=True).filter(
        Q(kind=DeletionGroup.Kind.BILL)
        | Q(members__batch__created_bills__isnull=False)
    ).select_related("firm", "bill").prefetch_related(
        "members__batch__entries", "members__batch__audit_events",
        "members__batch__created_bills",
    ).distinct().order_by("-deleted_at", "-id")
    bill_groups = [_trash_group_details(group) for group in bill_groups]
    return render(request, "ledger/bills.html", {
        **_page_views(request),
        "form": form, "bills": bills, "bill_trash_groups": bill_groups,
    })


@login_required
def delete_bill_view(request, bill_id):
    return _batch_action_response(request, bill_id, delete_bill)


@login_required
def edit_bill_view(request, bill_id):
    bill = get_object_or_404(Bill.objects.available().select_related("firm", "representative"), pk=bill_id, firm__is_deleted=False)
    form = BillEditForm(request.POST or None, instance=bill)
    if request.method == "POST" and form.is_valid():
        try:
            saved, changed = form.save(user=request.user)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Bill updated." if changed else "This bill was already updated.")
            return redirect("ledger:bills")
    return render(request, "ledger/edit_bill.html", {"form": form, "bill": bill})


def _deletion_groups_for_page(*, firm=None, start_date=None, end_date=None):
    groups = DeletionGroup.objects.filter(
        restored_at__isnull=True,
    ).select_related("firm", "bill").prefetch_related(
        "members__batch__entries", "members__batch__audit_events",
        "members__batch__created_bills",
    ).order_by("-deleted_at", "-id")
    if firm:
        groups = groups.filter(firm=firm)
    # Match a member's business date, but always show/recover its complete group.
    member_dates = Q()
    if start_date:
        member_dates &= Q(members__batch__date__gte=start_date)
    if end_date:
        member_dates &= Q(members__batch__date__lte=end_date)
    if member_dates:
        groups = groups.filter(member_dates).distinct()
    return [_trash_group_details(group) for group in groups]


def _trash_group_details(group):
    members = list(group.members.all())
    entries = [entry for member in members for entry in member.batch.entries.all() if not entry.is_deleted]
    group.transaction_ids = ", ".join(str(member.batch_id) for member in members)
    group.amount = sum((entry.increase - entry.decrease for entry in entries), Decimal("0.00"))
    group.payment_amount = sum((entry.decrease for entry in entries if entry.entry_type == "payment_made"), Decimal("0.00"))
    created_bills = [bill for member in members for bill in member.batch.created_bills.all()]
    if group.bill_id:
        group.title = "Bill " + group.bill.bill_number
    elif created_bills:
        group.title = "Bill " + ", ".join(bill.bill_number for bill in created_bills)
    else:
        group.title = "Transaction #" + str(members[0].batch_id)
    return group


@login_required
def trash_page(request):
    groups = DeletionGroup.objects.filter(restored_at__isnull=True).select_related(
        "firm", "bill",
    ).prefetch_related(
        "members__batch__entries", "members__batch__audit_events",
        "members__batch__created_bills",
    ).order_by("-deleted_at", "-id")
    groups = [_trash_group_details(group) for group in groups]
    return render(request, "ledger/trash.html", {
        "groups": groups,
        "trashed_firms": Firm.objects.filter(is_deleted=True).order_by("name"),
        "trashed_representatives": Representative.objects.filter(
            Q(is_deleted=True) | Q(firm__is_deleted=True)
        ).select_related("firm").order_by("firm__name", "name"),
    })


@login_required
def recover_trash(request, group_id):
    return _batch_action_response(request, group_id, recover_deletion_group)
