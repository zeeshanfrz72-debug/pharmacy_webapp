import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone

from .forms import FirmForm, LedgerTransactionForm, RepresentativeForm
from .models import Bill, Firm, LedgerEntry, Representative, TransactionBatch
from .services import (
    IdempotencyConflict,
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
    totals = LedgerEntry.objects.filter(is_deleted=False).aggregate(
        increase=Sum("increase"), decrease=Sum("decrease")
    )
    return (totals["increase"] or Decimal("0.00")) - (totals["decrease"] or Decimal("0.00"))


def _supplier_credit_total():
    credit_total = Decimal("0.00")
    for firm in Firm.objects.all():
        balance = Decimal(firm.current_debt())
        if balance < 0:
            credit_total += abs(balance)
    return credit_total


def _dashboard_payment_totals(today):
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    payment_entries = _active_activity_entries().filter(
        entry_type=LedgerEntry.EntryType.PAYMENT_MADE
    )

    def totals(start, end):
        qs = payment_entries.filter(date__gte=start, date__lte=end)
        return qs.count(), qs.aggregate(total=Sum("decrease"))["total"] or Decimal("0.00")

    return {
        "today_payment_count": payment_entries.filter(date=today).count(),
        "today_payment_total": totals(today, today)[1],
        "week_payment_count": totals(week_start, today)[0],
        "week_payment_total": totals(week_start, today)[1],
        "month_payment_count": totals(month_start, today)[0],
        "month_payment_total": totals(month_start, today)[1],
    }


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
            "previous_debt": str(summary["previous_debt"]),
            "remaining_debt": str(summary["remaining_debt"]),
            "total_debt": str(_total_net_debt()),
            **{key: str(value) if key.endswith("_total") else value for key, value in totals.items()},
        }
    )


@login_required
def add_firm(request):
    firms = Firm.objects.filter(is_deleted=False).order_by("name")
    archived_firms = Firm.objects.filter(is_deleted=True).order_by("name")
    return render(
        request,
        "ledger/add_firm.html",
        {
            "firms": firms,
            "archived_firms": archived_firms,
            "total_debt": _total_net_debt(),
        },
    )


@login_required
def add_representative(request):
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

    archived_representatives = Representative.objects.filter(
        Q(is_deleted=True) | Q(firm__is_deleted=True)
    ).select_related("firm").order_by("firm__name", "name")

    return render(
        request,
        "ledger/add_representative.html",
        {
            "source_type_choices": Firm.SourceType.choices,
            "selected_source_type": selected_source_type,
            "selected_firm_id": selected_firm_id,
            "firm_filter_options": firm_filter_options,
            "representatives": representatives,
            "archived_representatives": archived_representatives,
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
                "bill_date": bill.bill_date.strftime("%d %b %Y"),
                "is_latest": index == 0,
            }
            for index, bill in enumerate(firm.bills.order_by("-bill_date", "-id"))
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
    for bill in Bill.objects.select_related("firm").filter(
        firm__is_deleted=False,
    ).order_by("-bill_date", "-id"):
        bills_by_firm[bill.firm_id].append({
            "id": bill.pk,
            "bill_number": bill.bill_number,
            "bill_amount": str(bill.bill_amount),
            "bill_date": bill.bill_date.strftime("%d %b %Y"),
            "is_latest": firm_bill_counts[bill.firm_id] == 0,
        })
        firm_bill_counts[bill.firm_id] += 1

    context = {
        "form": form,
        "firm_form": FirmForm(),
        "rep_form": RepresentativeForm(),
        "saved_summary": saved_summary,
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
        start_date = date.fromisoformat(request.GET["start_date"]) if request.GET.get("start_date") else None
        end_date = date.fromisoformat(request.GET["end_date"]) if request.GET.get("end_date") else None
    except ValueError:
        return HttpResponse("Invalid date filter.", status=400)

    entries = LedgerEntry.objects.filter(is_deleted=False).select_related(
        "firm", "bill", "bill__representative", "payment", "payment__bill",
        "payment__representative", "transaction_batch", "transaction_batch__original_batch",
    ).order_by("date", "id")

    balances = defaultdict(lambda: Decimal("0.00"))
    grouped_rows = {}
    for entry in entries:
        balances[entry.firm_id] += Decimal(entry.increase) - Decimal(entry.decrease)
        batch = entry.transaction_batch
        row_key = ("batch", batch.pk) if batch else ("entry", entry.pk)
        row_date = batch.date if batch else entry.date
        row = grouped_rows.setdefault(row_key, {
            "batch": batch,
            "batch_id": batch.pk if batch else None,
            "date": row_date,
            "day": row_date.strftime("%A"),
            "firm": entry.firm,
            "source_type": entry.firm.get_source_type_display(),
            "entries": [],
            "representative_names": [],
            "related_bill_numbers": [],
            "payment_made": Decimal("0.00"),
            "remaining_debt": Decimal("0.00"),
        })
        row["entries"].append(entry)
        row["remaining_debt"] = balances[entry.firm_id]
        if entry.entry_type == LedgerEntry.EntryType.PAYMENT_MADE:
            row["payment_made"] += Decimal(entry.decrease)
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
        if start_date and row["date"] < start_date:
            continue
        if end_date and row["date"] > end_date:
            continue
        row["entries"].sort(key=lambda item: item.pk)
        row["is_reversible"] = bool(
            row["batch"] and row["batch"].kind == TransactionBatch.Kind.TRANSACTION
        )
        history_rows.append(row)
    history_rows.sort(key=lambda row: (row["date"], row["batch_id"] or 0), reverse=True)
    total_debt = _total_net_debt()

    context = {
        "history_rows": history_rows,
        "total_debt": total_debt,
        "start_date": request.GET.get("start_date", ""),
        "end_date": request.GET.get("end_date", ""),
    }
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("ajax"):
        return HttpResponse(render_to_string("ledger/_history_table.html", context, request=request))
    return render(request, "ledger/ledger_history.html", context)


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
            row["date"]: float(row["total"] or 0)
            for row in qs.values("date").annotate(total=Sum("decrease")).order_by("date")
        }
        for day in _day_series(start, end):
            labels.append(day.strftime("%d %b"))
            values.append(grouped.get(day, 0))
    else:
        grouped = {}
        for row in qs.annotate(bucket=TruncMonth("date")).values("bucket").annotate(total=Sum("decrease")):
            bucket = row["bucket"].date() if hasattr(row["bucket"], "date") else row["bucket"]
            grouped[bucket.replace(day=1)] = float(row["total"] or 0)
        for month in _month_series(start, end):
            labels.append(month.strftime("%b %Y"))
            values.append(grouped.get(month, 0))
    return JsonResponse({"labels": labels, "values": values})


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
            row["date"]: {"count": row["count"], "amount": float(row["total"] or 0)}
            for row in qs.values("date").annotate(count=Count("bill_id", distinct=True), total=Sum("increase"))
        }
        for day in _day_series(start, end):
            data = grouped.get(day, {"count": 0, "amount": 0})
            labels.append(day.strftime("%d %b"))
            count_values.append(data["count"])
            amount_values.append(data["amount"])
    else:
        grouped = {}
        for row in qs.annotate(bucket=TruncMonth("date")).values("bucket").annotate(
            count=Count("bill_id", distinct=True), total=Sum("increase")
        ):
            bucket = row["bucket"].date() if hasattr(row["bucket"], "date") else row["bucket"]
            grouped[bucket.replace(day=1)] = {"count": row["count"], "amount": float(row["total"] or 0)}
        for month in _month_series(start, end):
            data = grouped.get(month, {"count": 0, "amount": 0})
            labels.append(month.strftime("%b %Y"))
            count_values.append(data["count"])
            amount_values.append(data["amount"])
    return JsonResponse({"labels": labels, "count_values": count_values, "amount_values": amount_values})


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
            labels.append(day.strftime("%d %b"))
            values.append(float(current_total))
    else:
        by_month = defaultdict(list)
        for entry in entries:
            by_month[entry.date.replace(day=1)].append(entry)
        for month in _month_series(start, end):
            for entry in by_month[month]:
                balances[entry.firm_id] += Decimal(entry.increase) - Decimal(entry.decrease)
            current_total = sum(balances.values(), Decimal("0.00"))
            labels.append(month.strftime("%b %Y"))
            values.append(float(current_total))
    return JsonResponse({"labels": labels, "values": values})


def _batch_action_response(request, batch_id, operation):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required."}, status=405)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        return JsonResponse({"success": False, "error": "A reason is required."}, status=400)
    try:
        result = operation(batch_id, user=request.user, reason=reason)
    except ValidationError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({
            "success": True,
            "status": "reversed" if operation is reverse_transaction_batch else "restored",
            "changed": result["changed"],
            "total_debt": str(_total_net_debt()),
        })
    messages.success(request, "Transaction reversed." if operation is reverse_transaction_batch else "Transaction restored.")
    return redirect(request.POST.get("next") or "ledger:ledger_history")


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
    for firm in Firm.objects.all().order_by("name"):
        balance = Decimal(firm.current_debt())
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
        return JsonResponse({"success": True, "id": firm.pk, "name": firm.name})
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
        return JsonResponse({"success": True, "id": rep.pk, "name": rep.name})
    return JsonResponse({"success": False, "errors": form.errors.get_json_data()}, status=400)


@login_required
def edit_firm(request, firm_id):
    firm = get_object_or_404(Firm, pk=firm_id, is_deleted=False)
    form = FirmForm(request.POST or None, instance=firm)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("ledger:add_firm")
    return render(request, "ledger/edit_firm.html", {"form": form, "firm": firm})


@login_required
def archive_firm(request, firm_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    firm = get_object_or_404(Firm, pk=firm_id, is_deleted=False)
    firm.is_deleted = True
    firm.deleted_at = timezone.now()
    firm.save(update_fields=["is_deleted", "deleted_at"])
    messages.success(request, "Firm archived. Its balance and transaction history are retained.")
    return redirect("ledger:add_firm")


@login_required
def restore_firm(request, firm_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    firm = get_object_or_404(Firm, pk=firm_id, is_deleted=True)
    firm.is_deleted = False
    firm.deleted_at = None
    firm.save(update_fields=["is_deleted", "deleted_at"])
    messages.success(request, "Firm restored.")
    return redirect("ledger:add_firm")


@login_required
def edit_representative(request, rep_id):
    rep = get_object_or_404(Representative, pk=rep_id, is_deleted=False, firm__is_deleted=False)
    form = RepresentativeForm(request.POST or None, instance=rep)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("ledger:add_representative")
    return render(request, "ledger/edit_representative.html", {"form": form, "representative": rep})


@login_required
def archive_representative(request, rep_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    rep = get_object_or_404(Representative, pk=rep_id, is_deleted=False)
    rep.is_deleted = True
    rep.deleted_at = timezone.now()
    rep.save(update_fields=["is_deleted", "deleted_at"])
    messages.success(request, "Representative archived. Historical transaction links are retained.")
    return redirect("ledger:add_representative")


@login_required
def restore_representative(request, rep_id):
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    rep = get_object_or_404(Representative, pk=rep_id, is_deleted=True)
    rep.is_deleted = False
    rep.deleted_at = None
    rep.save(update_fields=["is_deleted", "deleted_at"])
    messages.success(request, "Representative restored.")
    return redirect("ledger:add_representative")
