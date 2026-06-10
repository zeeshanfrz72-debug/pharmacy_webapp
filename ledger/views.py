from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from django.http import JsonResponse, HttpResponse
from django.template.loader import render_to_string
from django.shortcuts import redirect, render, get_object_or_404
from django.utils import timezone
from django.contrib.auth.decorators import login_required
import json

from .forms import FirmForm, LedgerTransactionForm, RepresentativeForm
from .models import Bill, Firm, LedgerEntry, Payment, Representative
from django.contrib.auth.forms import UserCreationForm
from django.contrib import messages



# This page shows the firm balance table.
@login_required
def add_firm(request):
    firms = Firm.objects.filter(is_deleted=False).order_by("name")
    total_debt = sum((firm.current_debt() for firm in firms), 0)

    return render(
        request,
        "ledger/add_firm.html",
        {
            "firms": firms,
            "total_debt": total_debt,
        },
    )


# This page shows representatives with source type and firm filters.
@login_required
def add_representative(request):
    selected_source_type = request.GET.get("source_type", "")
    selected_firm_id = request.GET.get("firm", "")

    representatives = Representative.objects.filter(is_deleted=False).select_related("firm").order_by(
        "firm__source_type",
        "firm__name",
        "name",
    )

    if selected_source_type:
        representatives = representatives.filter(
            firm__source_type=selected_source_type
        )

    if selected_firm_id:
        representatives = representatives.filter(
            firm_id=selected_firm_id
        )

    firm_filter_options = Firm.objects.filter(is_deleted=False).order_by("name")

    if selected_source_type:
        firm_filter_options = firm_filter_options.filter(
            source_type=selected_source_type
        )

    return render(
        request,
        "ledger/add_representative.html",
        {
            "source_type_choices": Firm.SourceType.choices,
            "selected_source_type": selected_source_type,
            "selected_firm_id": selected_firm_id,
            "firm_filter_options": firm_filter_options,
            "representatives": representatives,
        },
    )


# This view switches a representative between active and inactive.
@login_required
def toggle_representative_active(request, representative_id):
    if request.method == "POST":
        representative = Representative.objects.get(id=representative_id)
        representative.is_active = not representative.is_active
        representative.save()

        # If the request was made via AJAX, return JSON so the frontend
        # can update the UI without a full page reload.
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({
                "id": representative.id,
                "is_active": representative.is_active,
            })

    return redirect("ledger:add_representative")


# This small view returns firms for one selected source type.
# JavaScript uses this to filter the Firm dropdown.
@login_required
def firms_for_source_type(request):
    source_type = request.GET.get("source_type")

    firms = []

    if source_type:
        matching_firms = Firm.objects.filter(
            source_type=source_type,
            is_deleted=False,
        ).order_by("name")

        firms = [
            {
                "id": firm.id,
                "name": firm.name,
            }
            for firm in matching_firms
        ]

    return JsonResponse(
        {
            "firms": firms,
        }
    )


# This small view returns representatives and bills for one selected firm.
# JavaScript uses this after the user selects a firm on the transaction page.
@login_required
def representatives_for_firm(request):
    firm_id = request.GET.get("firm_id")

    representatives = []
    source_type = ""
    bills = []

    if firm_id:
        firm = Firm.objects.filter(id=firm_id).first()

        if firm:
            source_type = firm.source_type

            firm_bills = firm.bills.order_by("-bill_date", "-id")

            for index, bill in enumerate(firm_bills):
                bills.append(
                    {
                        "id": bill.id,
                        "bill_number": bill.bill_number,
                        "bill_amount": str(bill.bill_amount),
                        "bill_date": bill.bill_date.strftime("%d %b %Y"),
                        "is_latest": index == 0,
                    }
                )

        reps = Representative.objects.filter(
            firm_id=firm_id,
            is_active=True,
            is_deleted=False,
        ).order_by("name")

        representatives = [
            {
                "id": rep.id,
                "name": rep.name,
            }
            for rep in reps
        ]

    return JsonResponse(
        {
            "representatives": representatives,
            "source_type": source_type,
            "bills": bills,
        }
    )


# This view shows the transaction form and handles saving it.
def add_transaction(request):
    # Stores the success summary after saving a transaction.
    saved_summary = None

    if request.method == "POST":
        form = LedgerTransactionForm(request.POST)

        if form.is_valid():
            saved_summary = form.save()
            form = LedgerTransactionForm()
    else:
        form = LedgerTransactionForm()

    # We need these dates for daily, weekly, and monthly payment stats.
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    # Today's payments.
    today_payments = Payment.objects.filter(payment_date=today)
    today_payment_count = today_payments.count()
    today_payment_total = today_payments.aggregate(total=Sum("amount"))["total"] or 0

    # This week's payments starting Monday.
    week_payments = Payment.objects.filter(payment_date__gte=week_start, payment_date__lte=today)
    week_payment_count = week_payments.count()
    week_payment_total = week_payments.aggregate(total=Sum("amount"))["total"] or 0

    # This month's payments starting on the 1st.
    month_payments = Payment.objects.filter(payment_date__gte=month_start, payment_date__lte=today)
    month_payment_count = month_payments.count()
    month_payment_total = month_payments.aggregate(total=Sum("amount"))["total"] or 0

    # Total debt across all firms.
    firms = Firm.objects.filter(is_deleted=False)
    total_debt = sum((firm.current_debt() for firm in firms), 0)

    # Prepare data for offline mode JS preloading
    all_firms = Firm.objects.filter(is_deleted=False).order_by("name")
    firms_by_source = defaultdict(list)
    for f in all_firms:
        firms_by_source[f.source_type].append({"id": f.id, "name": f.name})
    
    all_reps = Representative.objects.filter(is_active=True, is_deleted=False).order_by("name")
    reps_by_firm = defaultdict(list)
    for r in all_reps:
        reps_by_firm[r.firm_id].append({"id": r.id, "name": r.name})

    all_bills = Bill.objects.select_related('firm').filter(firm__is_deleted=False).order_by("-bill_date", "-id")
    bills_by_firm = defaultdict(list)
    firm_bill_counts = defaultdict(int)
    for b in all_bills:
        is_latest = (firm_bill_counts[b.firm_id] == 0)
        bills_by_firm[b.firm_id].append({
            "id": b.id,
            "bill_number": b.bill_number,
            "bill_amount": str(b.bill_amount),
            "bill_date": b.bill_date.strftime("%d %b %Y"),
            "is_latest": is_latest
        })
        firm_bill_counts[b.firm_id] += 1

    offline_data_json = json.dumps({
        "firms_by_source": dict(firms_by_source),
        "reps_by_firm": dict(reps_by_firm),
        "bills_by_firm": dict(bills_by_firm)
    })

    # If the transaction was just saved via AJAX, return JSON summary so the
    # frontend can update the UI without a full reload.
    if saved_summary and request.headers.get("x-requested-with") == "XMLHttpRequest":
        response = {
            "firm": saved_summary["firm"].name if saved_summary.get("firm") else None,
            "bill_number": saved_summary.get("bill").bill_number if saved_summary.get("bill") else None,
            "payment_amount": str(saved_summary.get("payment").amount) if saved_summary.get("payment") else None,
            "previous_debt": str(saved_summary.get("previous_debt")),
            "remaining_debt": str(saved_summary.get("remaining_debt")),
            "today_payment_count": today_payment_count,
            "today_payment_total": str(today_payment_total),
            "week_payment_count": week_payment_count,
            "week_payment_total": str(week_payment_total),
            "month_payment_count": month_payment_count,
            "month_payment_total": str(month_payment_total),
            "total_debt": str(total_debt),
        }

        return JsonResponse(response)

    return render(
        request,
        "ledger/add_transaction.html",
        {
            "form": form,
            "firm_form": FirmForm(),
            "rep_form": RepresentativeForm(),
            "saved_summary": saved_summary,
            "today_payment_count": today_payment_count,
            "today_payment_total": today_payment_total,
            "week_payment_count": week_payment_count,
            "week_payment_total": week_payment_total,
            "month_payment_count": month_payment_count,
            "month_payment_total": month_payment_total,
            "total_debt": total_debt,
            "offline_data_json": offline_data_json,
        },
    )
# This page shows the ledger history with optional date filtering.
def ledger_history(request):
    start_date = request.GET.get("start_date", "")
    end_date = request.GET.get("end_date", "")

    start_date_obj = None
    end_date_obj = None

    if start_date:
        start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()

    if end_date:
        end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()

    entries = LedgerEntry.objects.filter(is_deleted=False).select_related(
        "firm",
        "bill",
        "bill__representative",
        "payment",
        "payment__bill",
        "payment__representative",
    ).order_by("date", "id")

    balances = {}
    grouped_rows = {}

    for entry in entries:
        firm = entry.firm
        firm_id = firm.id
        date = entry.date

        current_balance = balances.get(firm_id, Decimal("0.00"))
        current_balance = current_balance + entry.increase - entry.decrease
        balances[firm_id] = current_balance

        entry_date = date
        if hasattr(entry_date, "date"):
            entry_date = entry_date.date()

        show_entry = True

        if start_date_obj and entry_date < start_date_obj:
            show_entry = False

        if end_date_obj and entry_date > end_date_obj:
            show_entry = False

        if not show_entry:
            continue

        group_key = (firm_id, entry_date)

        if group_key not in grouped_rows:
            grouped_rows[group_key] = {
                "date": entry_date,
                "day": entry_date.strftime("%A"),
                "firm": firm,
                "source_type": firm.get_source_type_display(),
                "representative_names": [],
                "new_bill_numbers": [],
                "related_bill_numbers": [],
                "payment_made": Decimal("0.00"),
                "remaining_debt": current_balance,
                "latest_entry_id": entry.id,
            }

        row = grouped_rows[group_key]

        if entry.id > row["latest_entry_id"]:
            row["latest_entry_id"] = entry.id

        if entry.bill:
            bill_number = entry.bill.bill_number

            if bill_number not in row["new_bill_numbers"]:
                row["new_bill_numbers"].append(bill_number)

            if bill_number not in row["related_bill_numbers"]:
                row["related_bill_numbers"].append(bill_number)

            if entry.bill.representative:
                rep_name = entry.bill.representative.name
                if rep_name not in row["representative_names"]:
                    row["representative_names"].append(rep_name)

        if entry.payment:
            row["payment_made"] += entry.decrease

            if entry.payment.bill:
                bill_number = entry.payment.bill.bill_number
                if bill_number not in row["related_bill_numbers"]:
                    row["related_bill_numbers"].append(bill_number)

            if entry.payment.representative:
                rep_name = entry.payment.representative.name
                if rep_name not in row["representative_names"]:
                    row["representative_names"].append(rep_name)

        row["remaining_debt"] = current_balance

    history_rows = list(grouped_rows.values())
    history_rows.sort(key=lambda row: (row["date"], row["latest_entry_id"]), reverse=True)

    total_debt = sum(balances.values(), Decimal("0.00"))

    # If AJAX request (or ajax=1 query param), return the table fragment only
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("ajax"):
        html = render_to_string(
            "ledger/_history_table.html",
            {"history_rows": history_rows, "total_debt": total_debt, "start_date": start_date, "end_date": end_date},
            request=request,
        )

        return HttpResponse(html)

    return render(
        request,
        "ledger/ledger_history.html",
        {
            "history_rows": history_rows,
            "total_debt": total_debt,
            "start_date": start_date,
            "end_date": end_date,
        },
    )
def _period_bounds(period):
    # Returns the start/end date and whether the chart should be daily or monthly.
    today = timezone.localdate()

    if period == "week":
        return today - timedelta(days=today.weekday()), today, "day"

    if period == "month":
        return today.replace(day=1), today, "day"

    if period == "year":
        return today.replace(month=1, day=1), today, "month"

    if period == "5years":
        try:
            return today.replace(year=today.year - 4, month=1, day=1), today, "month"
        except ValueError:
            return today - timedelta(days=365 * 4), today, "month"

    return today.replace(day=1), today, "day"


def _day_series(start, end):
    # Yields every day in the selected date range.
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _month_series(start, end):
    # Yields the first day of every month in the selected range.
    current = start.replace(day=1)
    end_month = end.replace(day=1)

    while current <= end_month:
        yield current
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)


def dashboard_payments_trend(request):
    # Returns total payment amount over time.
    period = request.GET.get("period", "month")
    start, end, grain = _period_bounds(period)

    qs = Payment.objects.filter(payment_date__gte=start, payment_date__lte=end)

    labels = []
    values = []

    if grain == "day":
        grouped = {
            row["payment_date"]: float(row["total"] or 0)
            for row in qs.values("payment_date").annotate(total=Sum("amount")).order_by("payment_date")
        }

        for day in _day_series(start, end):
            labels.append(day.strftime("%d %b"))
            values.append(grouped.get(day, 0))
    else:
        grouped = {}
        for row in qs.annotate(bucket=TruncMonth("payment_date")).values("bucket").annotate(total=Sum("amount")).order_by("bucket"):
            bucket = row["bucket"]
            if hasattr(bucket, "date"):
                bucket = bucket.date()
            bucket = bucket.replace(day=1)
            grouped[bucket] = float(row["total"] or 0)

        for month in _month_series(start, end):
            labels.append(month.strftime("%b %Y"))
            values.append(grouped.get(month, 0))

    return JsonResponse({"labels": labels, "values": values})


def dashboard_business_trend(request):
    # Returns bill count and bill amount over time, so the chart can toggle between them.
    period = request.GET.get("period", "month")
    start, end, grain = _period_bounds(period)

    qs = Bill.objects.filter(bill_date__gte=start, bill_date__lte=end)

    labels = []
    count_values = []
    amount_values = []

    if grain == "day":
        grouped = {
            row["bill_date"]: {
                "count": int(row["count"] or 0),
                "amount": float(row["total"] or 0),
            }
            for row in qs.values("bill_date").annotate(count=Count("id"), total=Sum("bill_amount")).order_by("bill_date")
        }

        for day in _day_series(start, end):
            labels.append(day.strftime("%d %b"))
            data = grouped.get(day, {"count": 0, "amount": 0})
            count_values.append(data["count"])
            amount_values.append(data["amount"])
    else:
        grouped = {}
        for row in qs.annotate(bucket=TruncMonth("bill_date")).values("bucket").annotate(count=Count("id"), total=Sum("bill_amount")).order_by("bucket"):
            bucket = row["bucket"]
            if hasattr(bucket, "date"):
                bucket = bucket.date()
            bucket = bucket.replace(day=1)
            grouped[bucket] = {
                "count": int(row["count"] or 0),
                "amount": float(row["total"] or 0),
            }

        for month in _month_series(start, end):
            labels.append(month.strftime("%b %Y"))
            data = grouped.get(month, {"count": 0, "amount": 0})
            count_values.append(data["count"])
            amount_values.append(data["amount"])

    return JsonResponse(
        {
            "labels": labels,
            "count_values": count_values,
            "amount_values": amount_values,
        }
    )


def dashboard_debt_over_time(request):
    # Returns running total debt across all firms over time.
    period = request.GET.get("period", "year")
    start, end, grain = _period_bounds(period)

    balances = defaultdict(Decimal)

    # Carry-forward balance from before the selected window.
    for entry in LedgerEntry.objects.filter(date__lt=start).order_by("date", "id"):
        balances[entry.firm_id] += entry.increase - entry.decrease

    current_total = sum(balances.values(), Decimal("0.00"))
    labels = []
    values = []

    entries = LedgerEntry.objects.filter(date__gte=start, date__lte=end).order_by("date", "id")

    if grain == "day":
        entries_by_day = defaultdict(list)
        for entry in entries:
            entries_by_day[entry.date].append(entry)

        for day in _day_series(start, end):
            for entry in entries_by_day.get(day, []):
                balances[entry.firm_id] += entry.increase - entry.decrease
                current_total = sum(balances.values(), Decimal("0.00"))

            labels.append(day.strftime("%d %b"))
            values.append(float(current_total))
    else:
        entries_by_month = defaultdict(list)
        for entry in entries:
            month_key = entry.date.replace(day=1)
            entries_by_month[month_key].append(entry)

        for month in _month_series(start, end):
            for entry in entries_by_month.get(month, []):
                balances[entry.firm_id] += entry.increase - entry.decrease
                current_total = sum(balances.values(), Decimal("0.00"))

            labels.append(month.strftime("%b %Y"))
            values.append(float(current_total))

    return JsonResponse({"labels": labels, "values": values})

def soft_delete_ledger_entry(request, entry_id):
    if request.method == "POST":
        entry = get_object_or_404(LedgerEntry, id=entry_id)

        entry.is_deleted = True
        entry.deleted_at = timezone.now()

        reason = request.POST.get("reason", "")
        if reason:
            entry.description = f"{entry.description} [DELETED: {reason}]"
        else:
            entry.description = f"{entry.description} [DELETED]"

        entry.save()

        # Compute updated total debt after deletion.
        total_debt = sum((f.current_debt() for f in Firm.objects.filter(is_deleted=False)), Decimal("0.00"))

        # If AJAX, return a JSON response so the frontend can remove the row
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"id": entry_id, "status": "deleted", "total_debt": str(total_debt)})

        next_url = request.POST.get("next", "ledger:ledger_history")
        return redirect(next_url)

    return redirect("ledger:ledger_history")


def dashboard_debt_breakdown(request):
    # Returns debt grouped by firm and source type.
    firms = Firm.objects.filter(is_deleted=False).order_by("name")

    firm_rows = []
    source_totals = defaultdict(Decimal)

    for firm in firms:
        debt = firm.current_debt()

        if debt > 0:
            firm_rows.append(
                {
                    "label": firm.name,
                    "value": float(debt),
                }
            )

        source_totals[firm.source_type] += debt

    firm_rows.sort(key=lambda row: row["value"], reverse=True)

    source_rows = []
    for source_value, source_label in Firm.SourceType.choices:
        value = float(source_totals.get(source_value, Decimal("0.00")))
        if value > 0:
            source_rows.append(
                {
                    "label": source_label,
                    "value": value,
                }
            )

    return JsonResponse(
        {
            "firms": firm_rows,
            "source_types": source_rows,
        }
    )


def signup(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Account created. You can now sign in.')
            return redirect('login')
    else:
        form = UserCreationForm()

    return render(request, 'registration/signup.html', {'form': form})

@login_required
def ajax_add_firm(request):
    if request.method == "POST":
        form = FirmForm(request.POST)
        if form.is_valid():
            firm = form.save()
            return JsonResponse({"success": True, "id": firm.id, "name": firm.name})
        else:
            return JsonResponse({"success": False, "errors": form.errors})
    return JsonResponse({"success": False, "error": "Invalid method"})

@login_required
def ajax_add_representative(request):
    if request.method == "POST":
        form = RepresentativeForm(request.POST)
        if form.is_valid():
            rep = form.save()
            deactivate_previous = request.POST.get("deactivate_previous") == "yes"
            if deactivate_previous:
                Representative.objects.filter(
                    firm=rep.firm,
                    is_active=True,
                ).exclude(
                    id=rep.id
                ).update(
                    is_active=False
                )
            return JsonResponse({"success": True, "id": rep.id, "name": rep.name})
        else:
            return JsonResponse({"success": False, "errors": form.errors})
    return JsonResponse({"success": False, "error": "Invalid method"})

@login_required
def edit_firm(request, firm_id):
    firm = get_object_or_404(Firm, id=firm_id, is_deleted=False)
    if request.method == "POST":
        form = FirmForm(request.POST, instance=firm)
        if form.is_valid():
            form.save()
            return redirect("ledger:add_firm")
    else:
        form = FirmForm(instance=firm)
    return render(request, "ledger/edit_firm.html", {"form": form, "firm": firm})

@login_required
def soft_delete_firm(request, firm_id):
    if request.method == "POST":
        firm = get_object_or_404(Firm, id=firm_id)
        firm.is_deleted = True
        firm.deleted_at = timezone.now()
        firm.save()
    return redirect("ledger:add_firm")

@login_required
def edit_representative(request, rep_id):
    rep = get_object_or_404(Representative, id=rep_id, is_deleted=False)
    if request.method == "POST":
        form = RepresentativeForm(request.POST, instance=rep)
        if form.is_valid():
            form.save()
            return redirect("ledger:add_representative")
    else:
        form = RepresentativeForm(instance=rep)
    return render(request, "ledger/edit_representative.html", {"form": form, "representative": rep})

@login_required
def soft_delete_representative(request, rep_id):
    if request.method == "POST":
        rep = get_object_or_404(Representative, id=rep_id)
        rep.is_deleted = True
        rep.deleted_at = timezone.now()
        rep.save()
    return redirect("ledger:add_representative")