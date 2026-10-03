"""Reproducible adversarial audit; never writes the configured/live database.

Run with the pinned project environment:
    python scripts/accounting_audit.py --output docs/accounting-audit-results.json
Each run forces a fresh temporary SQLite DB. --snapshot reads db.sqlite3 through
SQLite's backup API; only aggregate, non-identifying evidence is exported.
Historical defect reproductions expect the pre-repair behavior. For the repaired
working tree, use accounting_repair_verify.py, accounting_repair_sequences.py,
and the accounting_repair_*browser scripts; the old focused attacks may now fail.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
ZERO = D("0.00")
RESULTS = {"focused": [], "sequences": {}, "races": [], "snapshot": {}}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_snapshot(temp, original=None):
    original = original or ROOT / "db.sqlite3"
    if not original.exists():
        return {"status": "missing"}
    before = digest(original)
    target = temp / ("snapshot-" + uuid.uuid4().hex + ".sqlite3")
    with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
    with closing(sqlite3.connect(target)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        data = {"integrity_check": db.execute("PRAGMA integrity_check").fetchall(),
                "foreign_key_errors": len(db.execute("PRAGMA foreign_key_check").fetchall()),
                "source_sha256_before": before, "source_sha256_after": digest(original)}
        if "django_migrations" not in tables:
            return {**data, "status": "no Django schema", "table_count": len(tables)}
        def count(name, sql):
            try:
                data[name] = db.execute(sql).fetchone()[0]
            except sqlite3.Error as exc:
                data[name] = "not inspectable: " + str(exc)
        for table in sorted(tables):
            if table.startswith("ledger_"):
                count(table + "_count", 'SELECT COUNT(*) FROM "' + table + '"')
        count("negative_source_amounts", "SELECT (SELECT COUNT(*) FROM ledger_bill WHERE bill_amount<=0)+(SELECT COUNT(*) FROM ledger_payment WHERE amount<=0)")
        count("bill_rep_cross_firm", "SELECT COUNT(*) FROM ledger_bill b JOIN ledger_representative r ON b.representative_id=r.id WHERE b.firm_id<>r.firm_id")
        count("payment_cross_firm", "SELECT COUNT(*) FROM ledger_payment p LEFT JOIN ledger_bill b ON p.bill_id=b.id LEFT JOIN ledger_representative r ON p.representative_id=r.id WHERE p.firm_id<>b.firm_id OR p.firm_id<>r.firm_id")
        count("ledger_cross_firm", "SELECT COUNT(*) FROM ledger_ledgerentry e LEFT JOIN ledger_bill b ON e.bill_id=b.id LEFT JOIN ledger_payment p ON e.payment_id=p.id LEFT JOIN ledger_transactionbatch t ON e.transaction_batch_id=t.id WHERE e.firm_id<>b.firm_id OR e.firm_id<>p.firm_id OR e.firm_id<>t.firm_id")
        count("ledger_without_batch", "SELECT COUNT(*) FROM ledger_ledgerentry WHERE transaction_batch_id IS NULL")
        count("bills_without_creation_batch", "SELECT COUNT(*) FROM ledger_bill WHERE creation_batch_id IS NULL")
        count("invalid_ledger_sides", "SELECT COUNT(*) FROM ledger_ledgerentry WHERE increase<0 OR decrease<0 OR (increase>0 AND decrease>0)")
        count("reversed_without_deletion_group", "SELECT COUNT(*) FROM ledger_transactionbatch WHERE kind='transaction' AND status='reversed' AND deletion_group_id IS NULL")
        count("active_payment_deleted_bill", "SELECT COUNT(DISTINCT p.id) FROM ledger_payment p JOIN ledger_bill b ON p.bill_id=b.id JOIN ledger_transactionbatch bt ON b.creation_batch_id=bt.id JOIN ledger_ledgerentry e ON e.payment_id=p.id JOIN ledger_transactionbatch pt ON e.transaction_batch_id=pt.id WHERE bt.status='reversed' AND pt.kind='transaction' AND pt.status='active' AND e.entry_type='payment_made'")
        count("unbatched_bill_ledger", "SELECT COUNT(*) FROM ledger_bill b WHERE NOT EXISTS (SELECT 1 FROM ledger_ledgerentry e WHERE e.bill_id=b.id AND e.entry_type='bill_created')")
        count("payments_without_ledger", "SELECT COUNT(*) FROM ledger_payment p WHERE NOT EXISTS (SELECT 1 FROM ledger_ledgerentry e WHERE e.payment_id=p.id AND e.entry_type='payment_made')")
        count("duplicate_bill_reference_groups", "SELECT COUNT(*) FROM (SELECT firm_id,lower(trim(bill_number)),bill_date FROM ledger_bill GROUP BY firm_id,lower(trim(bill_number)),bill_date HAVING COUNT(*)>1)")
        data["applied_ledger_migrations"] = [r[0] for r in db.execute("SELECT name FROM django_migrations WHERE app='ledger' ORDER BY name")]
        if "ledger_transactionbatch" in tables:
            count("source_creation_batches_zero_after", "SELECT COUNT(*) FROM ledger_transactionbatch t WHERE t.kind='transaction' AND t.balance_after=0 AND t.status='active' AND (SELECT COALESCE(SUM(e.increase-e.decrease),0) FROM ledger_ledgerentry e WHERE e.transaction_batch_id=t.id AND e.is_deleted=0)<>0")
        return data


def configure(database):
    os.environ.update(DJANGO_SETTINGS_MODULE="pharmacy_app.settings", DJANGO_DB_NAME=str(database),
                      DJANGO_SECRET_KEY="audit-synthetic-key-" + "x" * 64,
                      DJANGO_ALLOWED_HOSTS="testserver,127.0.0.1,localhost", APP_OWNER_USERNAME="audit-owner",
                      DJANGO_DEBUG="True", DJANGO_SECURE_SSL_REDIRECT="False",
                      DJANGO_SESSION_COOKIE_SECURE="False", DJANGO_CSRF_COOKIE_SECURE="False",
                      DJANGO_HSTS_SECONDS="0", DJANGO_SQLITE_TIMEOUT="20")


def setup(database):
    configure(database)
    sys.path.insert(0, str(ROOT))
    import django
    django.setup()
    from django.core.management import call_command
    from django.test.utils import setup_test_environment
    call_command("migrate", interactive=False, verbosity=0)
    setup_test_environment()
    global Firm, Representative, Bill, Payment, Entry, Batch, Group, Member, services, views, owner, client, today
    from django.contrib.auth import get_user_model
    from django.test import Client
    from django.utils import timezone
    from ledger import services, views
    from ledger.models import Firm, Representative, Bill, Payment, LedgerEntry as Entry, TransactionBatch as Batch, DeletionGroup as Group, DeletionMember as Member
    today = timezone.localdate()
    owner = get_user_model().objects.create_superuser("audit-owner", password=uuid.uuid4().hex)
    client = Client()
    client.force_login(owner)


def account(local=False):
    firm = Firm.objects.create(name="Synthetic " + uuid.uuid4().hex[:10], source_type="local_market" if local else "distributor")
    rep = None if local else Representative.objects.create(firm=firm, name="Synthetic rep")
    return firm, rep


def data(firm, rep, amount=None, payment=None, bill=None, day=None, rid=None):
    return dict(firm=firm, representative=rep, source_type=firm.source_type,
                request_id=rid or uuid.uuid4(), bill_choice="add_new" if amount is not None else str(bill.pk) if bill else "",
                new_bill_number="AUDIT-" + uuid.uuid4().hex[:8], new_bill_amount=D(amount) if amount is not None else None,
                payment_amount=D(payment) if payment is not None else None, bill_date=day or today)


def create(firm, rep, amount=None, payment=None, bill=None, day=None, rid=None):
    values = data(firm, rep, amount, payment, bill, day, rid)
    return services.create_transaction_batch(values, user=owner, payload_hash=str(values["request_id"]))


def edit_values(bill, **changes):
    bill.refresh_from_db()
    values = dict(revision=services.bill_revision(bill), bill_number=bill.bill_number,
                  bill_date=bill.bill_date, bill_amount=bill.bill_amount,
                  representative=bill.representative, notes=bill.notes)
    values.update(changes)
    return values


def http_data(firm, rep, amount=None, payment=None, bill=None, rid=None):
    return dict(request_id=str(rid or uuid.uuid4()), source_type=firm.source_type, firm=str(firm.pk),
                representative=str(rep.pk) if rep else "", bill_choice="add_new" if amount is not None else str(bill.pk) if bill else "",
                new_bill_number="AUDIT-HTTP", new_bill_amount=str(amount) if amount is not None else "",
                payment_choice="other" if payment is not None else "", custom_payment_amount=str(payment) if payment is not None else "")


def post(values):
    return client.post("/ledger/add-transaction/", values, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


def sql_balance(firm):
    # Independent Decimal sum of row values, not Firm.current_debt()/SQL SUM.
    return sum((inc - dec for inc, dec in Entry.objects.filter(firm=firm, is_deleted=False).values_list("increase", "decrease")), ZERO)


def state(firm):
    return {model.__name__: list(model.objects.filter(firm=firm).order_by("pk").values())
            for model in (Bill, Payment, Entry, Batch, Group)}


def scenario(name, fn, status="confirmed"):
    started = time.perf_counter()
    try:
        evidence = fn()
        record = dict(name=name, status=status, evidence=evidence, seconds=round(time.perf_counter()-started, 3))
    except Exception as exc:
        record = dict(name=name, status="harness_error", error=repr(exc), traceback=traceback.format_exc())
    RESULTS["focused"].append(record)
    print(name, record["status"], json.dumps(record.get("evidence", record.get("error")), default=str), flush=True)


def focused():
    def negative_admin():
        firm, rep = account()
        create(firm, rep, "1000")
        response = client.post("/admin/ledger/payment/add/", dict(firm=firm.pk, representative=rep.pk,
            bill="", payment_date=today.isoformat(), amount="-100", method="cash", notes="", _save="Save"))
        assert response.status_code == 302 and sql_balance(firm) == 1100
        return dict(http_status=302, entered_payment="-100.00", balance_before="1000.00", balance_after=sql_balance(firm))
    scenario("A01_admin_negative_payment_increases_debt", negative_admin)

    def admin_overpay():
        firm, rep = account()
        create(firm, rep, "100")
        response = client.post("/admin/ledger/payment/add/", dict(firm=firm.pk, representative=rep.pk,
            bill="", payment_date=today.isoformat(), amount="1000", method="cash", notes="", _save="Save"))
        assert response.status_code == 302 and sql_balance(firm) == -900
        return dict(http_status=302, debt="100.00", payment="1000.00", balance_after=sql_balance(firm))
    scenario("A02_admin_overpayment_bypasses_service", admin_overpay)

    def admin_cash_bill():
        firm, rep = account(True)
        response = client.post("/admin/ledger/bill/add/", dict(firm=firm.pk, representative="", bill_number="CASH",
            bill_date=today.isoformat(), bill_amount="500", previous_debt_at_bill_time="123", notes="", _save="Save"))
        assert response.status_code == 302 and Payment.objects.filter(firm=firm).count() == 0 and sql_balance(firm) == 500
        batch = Batch.objects.get(firm=firm)
        return dict(balance=sql_balance(firm), payments=0, batch_balance_after=batch.balance_after,
                    batch_created_by=batch.created_by_id, bill_previous_debt=Bill.objects.get(firm=firm).previous_debt_at_bill_time)
    scenario("A03_admin_local_market_bill_and_incomplete_snapshots", admin_cash_bill)

    def negative_service():
        firm, rep = account()
        create(firm, rep, "100")
        create(firm, rep, payment="-50")
        create(firm, rep, "-20")
        assert sql_balance(firm) == 130
        empty = create(firm, rep)
        return dict(balance=sql_balance(firm), negative_payments=Payment.objects.filter(firm=firm, amount__lt=0).count(),
                    negative_bills=Bill.objects.filter(firm=firm, bill_amount__lt=0).count(), empty_batch_entries=empty["batch"].entries.count())
    scenario("A04_service_accepts_negative_and_empty_actions", negative_service)

    def invalid_http():
        firm, rep = account()
        codes = {}
        for value in ("-1", "0", "0.001", "10000000000.00", "NaN", "Infinity", "abc"):
            response = post(http_data(firm, rep, amount=value))
            assert response.status_code == 400
            codes[value] = response.status_code
        other, other_rep = account()
        bad = http_data(firm, other_rep, amount="100")
        assert post(bad).status_code == 400
        assert not Entry.objects.filter(firm=firm).exists()
        return dict(rejected_values=codes, wrong_supplier_representative=400, ledger_entries=0)
    scenario("C01_HTTP_invalid_amounts_and_cross_firm_rejected", invalid_http, "passed")

    def fractional_service():
        firm, rep = account()
        out = create(firm, rep, "0.005", "0.004")
        out["bill"].refresh_from_db(); out["payment"].refresh_from_db()
        return dict(stored_bill=out["bill"].bill_amount, stored_payment=out["payment"].amount,
                    decimal_row_balance=sql_balance(firm), application_balance=firm.current_debt(),
                    requested_effect=D("0.005")-D("0.004"))
    scenario("A05_service_fractional_cent_rounding", fractional_service)

    def paid_recovery():
        firm, rep = account()
        original = create(firm, rep, "1000")
        payment = create(firm, rep, payment="200", bill=original["bill"])
        independent = services.reverse_transaction_batch(payment["batch"].pk, user=owner, reason="Independent")["group"]
        group = services.delete_bill(original["bill"].pk, user=owner, reason="Delete bill")["group"]
        response = client.post(f"/ledger/trash/{independent.pk}/recover/", {"reason": "Recover payment"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        assert response.status_code == 200 and not Bill.objects.available().filter(pk=original["bill"].pk).exists()
        assert sql_balance(firm) == -200
        return dict(recovery_http_status=200, bill_available=False, active_payment=True,
                    balance=sql_balance(firm), bill_deletion_group=group.pk)
    scenario("A06_recover_payment_while_bill_deleted", paid_recovery)

    def local_recovery_edit():
        firm, rep = account(True)
        # Separate payment is a trusted/admin legacy path, reachable via admin.
        response = client.post("/admin/ledger/bill/add/", dict(firm=firm.pk, representative="", bill_number="LEGACY",
            bill_date=today.isoformat(), bill_amount="100", previous_debt_at_bill_time="0", notes="", _save="Save"))
        assert response.status_code == 302
        bill = Bill.objects.get(firm=firm)
        create(firm, rep, payment="100")
        services.edit_bill(bill.pk, edit_values(bill, bill_amount=D("200")), user=owner)
        return dict(balance=sql_balance(firm), original_bill_paired_payments=bill.payments.count())
    scenario("A07_local_bill_without_paired_payment_edit", local_recovery_edit, "boundary")

    def replay_stale_rep():
        firm, rep = account()
        values = http_data(firm, rep, amount="100")
        first = post(values)
        assert first.status_code == 200
        client.post(f"/ledger/representative/{rep.pk}/delete/")
        retry = post(values)
        assert retry.status_code == 400 and Batch.objects.filter(firm=firm).count() == 1
        return dict(first_status=200, replay_status=400, batches=1, replay_error=retry.json())
    scenario("A08_committed_retry_rejected_after_rep_deletion", replay_stale_rep)

    def replay_bill_deleted():
        firm, rep = account()
        bill = create(firm, rep, "1000")["bill"]
        values = http_data(firm, rep, payment="200", bill=bill)
        assert post(values).status_code == 200
        services.delete_bill(bill.pk, user=owner, reason="Removed")
        retry = post(values)
        assert retry.status_code == 400
        return dict(replay_status=400, balance=sql_balance(firm), replay_error=retry.json())
    scenario("A09_committed_retry_rejected_after_bill_deletion", replay_bill_deleted)

    def move_rep():
        firm, rep = account(); other, _ = account()
        bill = create(firm, rep, "1000", "100")["bill"]
        response = client.post(f"/ledger/representative/{rep.pk}/edit/", dict(source_type=other.source_type, firm=other.pk, name=rep.name, phone=""))
        rep.refresh_from_db(); bill.refresh_from_db()
        assert response.status_code == 302 and rep.firm_id == other.pk and bill.representative.firm_id != bill.firm_id
        return dict(http_status=302, historical_bill_rep_cross_firm=True,
                    payment_rep_cross_firm=Payment.objects.get(firm=firm).representative.firm_id != firm.pk)
    scenario("A10_representative_transfer_breaks_historical_links", move_rep)

    def type_change():
        firm, rep = account(True)
        original = create(firm, rep, payment="100")
        response = client.post(f"/ledger/firm/{firm.pk}/edit/", dict(name=firm.name, source_type="distributor", phone=""))
        firm.refresh_from_db()
        assert response.status_code == 302
        new_rep = Representative.objects.create(firm=firm, name="Later rep")
        bill = original["bill"]
        services.edit_bill(bill.pk, edit_values(bill, bill_amount=D("200"), representative=new_rep), user=owner)
        assert sql_balance(firm) == 100 and Payment.objects.get(firm=firm).amount == 100
        return dict(change_source_status=302, historical_cash_bill_after_edit="200.00", historical_cash_payment="100.00", debt=sql_balance(firm))
    scenario("A11_source_type_change_alters_historical_edit_semantics", type_change)

    def full_paid_edit_credit():
        firm, rep = account()
        bill = create(firm, rep, "1000", "1000")["bill"]
        services.edit_bill(bill.pk, edit_values(bill, bill_amount=D("500")), user=owner)
        assert sql_balance(firm) == -500
        return dict(balance=sql_balance(firm), supplier_credit=firm.current_credit)
    scenario("P01_reducing_paid_bill_creates_supplier_credit", full_paid_edit_credit, "policy")

    def backdate_snapshot():
        firm, rep = account()
        first = create(firm, rep, "100", day=today)
        older = create(firm, rep, "200", day=today-timedelta(days=1))
        rows = {row["batch_id"]: row for row in views._history_rows(firm=firm)}
        assert older["batch"].previous_balance == 100 and rows[older["batch"].pk]["remaining_debt"] == 200
        return dict(backdated_receipt_previous=older["batch"].previous_balance, backdated_receipt_after=older["batch"].balance_after,
                    same_action_history_after=rows[older["batch"].pk]["remaining_debt"], later_bill_previous=Bill.objects.get(pk=first["bill"].pk).previous_debt_at_bill_time)
    scenario("A12_backdated_create_snapshots_disagree_with_history", backdate_snapshot)

    def historical_edit_offsets():
        firm, rep = account()
        original = create(firm, rep, "100", day=today-timedelta(days=2))
        root = original["batch"]
        services.reverse_transaction_batch(root.pk, user=owner, reason="Cycle")
        services.restore_transaction_batch(root.pk, user=owner, reason="Cycle")
        services.edit_bill(original["bill"].pk, edit_values(original["bill"], bill_amount=D("200")), user=owner)
        rows = {row["batch_id"]: row for row in views._history_rows(firm=firm)}
        return dict(current_balance=sql_balance(firm), history_after=rows[root.pk]["remaining_debt"],
                    offsets=list(root.audit_events.values_list("kind", "previous_balance", "balance_after")))
    scenario("P02_restatement_preserves_balance_but_old_event_snapshots", historical_edit_offsets, "policy")

    def snapshot_replay_edit():
        firm, rep = account()
        a = create(firm, rep, "100"); b = create(firm, rep, "200")
        services.reverse_transaction_batch(a["batch"].pk, user=owner, reason="Remove earlier bill")
        before = Batch.objects.get(pk=b["batch"].pk).balance_after
        row_before = views._history_rows(firm=firm)[0]["remaining_debt"]
        # Even a notes-only edit refreshes financial snapshots for all active roots.
        services.edit_bill(b["bill"].pk, edit_values(b["bill"], notes="Notes only"), user=owner)
        after = Batch.objects.get(pk=b["batch"].pk).balance_after
        assert before == 300 and row_before == 200 and after == 200
        return dict(snapshot_before_notes_edit=before, displayed_history_before=row_before, snapshot_after_notes_edit=after)
    scenario("A13_snapshot_meaning_changes_on_notes_only_edit", snapshot_replay_edit)

    def cumulative_overflow():
        firm, rep = account()
        evidence = []
        safe_client = type(client)(raise_request_exception=False); safe_client.force_login(owner)
        for i in range(3):
            values = http_data(firm, rep, amount="9999999999.99")
            values["new_bill_number"] = f"MAX-{i}"
            response = safe_client.post("/ledger/add-transaction/", values, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            evidence.append(dict(status=response.status_code, bills=Bill.objects.filter(firm=firm).count(),
                                 batches=Batch.objects.filter(firm=firm).count()))
        errors = {}
        for label, fn in (("read_batch", lambda: list(Batch.objects.filter(firm=firm))),
                          ("history", lambda: views._history_rows(firm=firm))):
            try: fn(); errors[label] = "readable"
            except Exception as exc: errors[label] = type(exc).__name__
        assert any(item["status"] == 500 for item in evidence) or "InvalidOperation" in errors.values()
        return dict(submissions=evidence, read_errors=errors, balance=sql_balance(firm))
    scenario("A14_cumulative_balance_overflow", cumulative_overflow)

    def signal_failure():
        firm, rep = account()
        with patch("ledger.signals.LedgerEntry.objects.create", side_effect=RuntimeError("Injected ledger failure")):
            try: Bill.objects.create(firm=firm, representative=rep, bill_number="ORPHAN", bill_amount=100)
            except RuntimeError: pass
        assert Bill.objects.filter(firm=firm).count() == 1 and not Entry.objects.filter(firm=firm).exists()
        return dict(bills=1, ledger_entries=0, balance=sql_balance(firm), path="direct ORM autocommit, not admin transaction")
    scenario("A15_direct_ORM_signal_failure_leaves_orphan", signal_failure, "boundary")

    def fault_rollback():
        from django.db import transaction
        evidence = {}
        for fault in ("ledger", "payment", "batch_final_save"):
            firm, rep = account(); before = state(firm)
            if fault == "ledger": context = patch("ledger.signals.LedgerEntry.objects.create", side_effect=RuntimeError("Injected"))
            elif fault == "payment": context = patch("ledger.services.Payment.objects.create", side_effect=RuntimeError("Injected"))
            else:
                original = Batch.save
                def fail_saved(instance, *args, **kwargs):
                    if kwargs.get("update_fields") == ["balance_after"]: raise RuntimeError("Injected")
                    return original(instance, *args, **kwargs)
                context = patch.object(Batch, "save", fail_saved)
            with context:
                try: create(firm, rep, "100", "10")
                except RuntimeError: pass
                else: raise AssertionError("Failure was not injected")
            assert state(firm) == before
            create(firm, rep, "100", "10"); assert sql_balance(firm) == 90
            evidence[fault] = "rolled back; retry correct"
        firm, rep = account(); original = create(firm, rep, "100", "10")
        create(firm, rep, payment="10", bill=original["bill"])
        before = state(firm)
        with patch("ledger.services.DeletionMember.objects.create", side_effect=RuntimeError("Injected")):
            try: services.delete_bill(original["bill"].pk, user=owner, reason="Injected")
            except RuntimeError: pass
        assert state(firm) == before
        group = services.delete_bill(original["bill"].pk, user=owner, reason="Retry")["group"]
        before = state(firm)
        with patch("ledger.services._write_batch_offsets", side_effect=RuntimeError("Injected")):
            try: services.recover_deletion_group(group.pk, user=owner, reason="Injected")
            except RuntimeError: pass
        assert state(firm) == before
        services.recover_deletion_group(group.pk, user=owner, reason="Retry"); assert sql_balance(firm) == 80
        evidence["deletion_member_and_recovery_offset"] = "rolled back; retries correct"
        # Django admin wraps changeform saves in atomic even though signals do not.
        firm, rep = account()
        with transaction.atomic():
            try:
                with transaction.atomic(), patch("ledger.signals.LedgerEntry.objects.create", side_effect=RuntimeError("Injected")):
                    Bill.objects.create(firm=firm, representative=rep, bill_number="ADMIN", bill_amount=100)
            except RuntimeError: pass
        assert not Bill.objects.filter(firm=firm).exists()
        return evidence
    scenario("C02_service_write_and_recovery_failure_rollback", fault_rollback, "passed")

    def raw_constraints():
        firm, rep = account(); other, other_rep = account()
        bill = create(other, other_rep, "100")["bill"]
        Entry.objects.create(firm=firm, bill=bill, entry_type="made_up", increase=-50, decrease=20)
        assert sql_balance(firm) == -70
        return dict(cross_firm_link=True, invalid_entry_type=True, negative_increase=True, balance=sql_balance(firm), path="trusted ORM/maintenance only")
    scenario("A16_missing_database_financial_constraints", raw_constraints, "boundary")

    def credits_no_payment():
        firm, rep = account()
        original = create(firm, rep, "100", "100")
        services.edit_bill(original["bill"].pk, edit_values(original["bill"], bill_amount=D("50")), user=owner)
        before = state(firm)
        response = post(http_data(firm, rep, payment="1"))
        assert response.status_code == 400 and state(firm) == before
        return dict(balance=sql_balance(firm), payment_status=400, unchanged=True)
    scenario("C03_HTTP_payment_on_supplier_credit_rejected", credits_no_payment, "passed")

    def duplicate_reference():
        firm, rep = account()
        assert post(http_data(firm, rep, amount="100")).status_code == 200
        assert post(http_data(firm, rep, amount="100")).status_code == 200
        return dict(identical_bill_references=2, balance=sql_balance(firm), request_ids="different")
    scenario("P03_duplicate_invoice_reference_allowed", duplicate_reference, "policy")

    def deleted_legacy_bill():
        firm, rep = account()
        original = create(firm, rep, "100")
        original["batch"].entries.update(is_deleted=True)
        assert Bill.objects.available().filter(pk=original["bill"].pk).exists()
        error = None
        try: services.delete_bill(original["bill"].pk, user=owner, reason="Legacy")
        except Exception as exc: error = str(exc)
        return dict(bill_available=True, balance=sql_balance(firm), delete_error=error, path="legacy/synthetic soft deletion")
    scenario("A17_legacy_soft_deleted_source_bill_remains_available", deleted_legacy_bill, "boundary")

    def misclassified_integrity():
        from django.db import IntegrityError
        firm, rep = account()
        error = None
        with patch("ledger.services.Payment.objects.create", side_effect=IntegrityError("Unrelated DB constraint failure")):
            try: create(firm, rep, "100", "10")
            except Exception as exc: error = type(exc).__name__ + ": " + str(exc)
        assert error.startswith("IdempotencyConflict") and not Entry.objects.filter(firm=firm).exists()
        return dict(error=error, real_failure="unrelated payment integrity error", rollback=True)
    scenario("A18_all_integrity_errors_misreported_as_request_conflict", misclassified_integrity)

    def type_change_invents_cash():
        firm, rep = account()
        bill=create(firm,rep,"1000","100")["bill"]
        assert sql_balance(firm)==900
        response=client.post(f"/ledger/firm/{firm.pk}/edit/",dict(name=firm.name,source_type="local_market",phone=""))
        assert response.status_code==302
        values=edit_values(bill,bill_amount=D("1200"),representative=None)
        payload={**values,"bill_date":values["bill_date"].strftime("%d-%m-%Y"),"representative":""}
        response=client.post(f"/ledger/bill/{bill.pk}/edit/",payload)
        assert response.status_code==302 and sql_balance(firm)==0
        return dict(source_change_status=302,bill_edit_status=302,actual_recorded_payment_before="100.00",
                    recorded_payment_after=Payment.objects.get(firm=firm).amount,bill_after="1200.00",balance_before="900.00",balance_after=sql_balance(firm))
    scenario("A19_source_type_change_invents_payment_erases_debt",type_change_invents_cash)

    def lost_field_history():
        firm,rep=account(); new_rep=Representative.objects.create(firm=firm,name="Changed rep")
        bill=create(firm,rep,"100","10")["bill"]
        bill.notes="Original supplier instruction";bill.save(update_fields=["notes"])
        services.edit_bill(bill.pk,edit_values(bill,notes="Replacement instruction",representative=new_rep),user=owner)
        adjustment=Entry.objects.get(firm=firm,entry_type="adjustment")
        assert "Original supplier instruction" not in adjustment.description
        assert rep.name not in adjustment.description
        return dict(adjustment_description=adjustment.description,old_notes_preserved=False,
                    old_representative_preserved=False,paired_payment_representative_changed=True)
    scenario("A20_bill_edits_lose_prior_notes_and_representative",lost_field_history)

    def admin_negative_bill():
        firm,rep=account()
        codes=[]
        for value in ("-100","0"):
            response=client.post("/admin/ledger/bill/add/",dict(firm=firm.pk,representative=rep.pk,bill_number="ADMIN-"+value,
                bill_date=today.isoformat(),bill_amount=value,previous_debt_at_bill_time="0",notes="",_save="Save"))
            codes.append(response.status_code)
        assert codes==[302,302] and sql_balance(firm)==-100
        return dict(statuses=codes,bill_amounts=["-100","0"],balance=sql_balance(firm))
    scenario("A21_admin_negative_and_zero_bills",admin_negative_bill)

    def service_amount_matrix():
        rows=[]
        for value in ("0","-1","0.001","10000000000.00","NaN","Infinity","abc"):
            firm,rep=account(); before=state(firm)
            try:
                create(firm,rep,amount=value)
                status="accepted"
            except Exception as exc: status=type(exc).__name__
            rows.append(dict(value=value,status=status,bills=Bill.objects.filter(firm=firm).count(),
                             rows=Entry.objects.filter(firm=firm).count(),unchanged=state(firm)==before))
        return rows
    scenario("A22_service_invalid_amount_matrix",service_amount_matrix,"boundary")

    def conflicting_replay():
        firm,rep=account();values=http_data(firm,rep,amount="100")
        first=post(values);assert first.status_code==200
        duplicate=post(values);assert duplicate.status_code==200 and duplicate.json()["status"]=="duplicate"
        before=state(firm)
        response=post({**values,"new_bill_amount":"200"});assert response.status_code==409 and state(firm)==before
        return dict(duplicate_status="duplicate",conflict_http_status=409,unchanged=True)
    scenario("C04_HTTP_idempotency_conflict_rolls_back",conflicting_replay,"passed")

    def access_checks():
        from django.test import Client
        from django.contrib.auth import get_user_model
        firm,rep=account();values=http_data(firm,rep,amount="100")
        guest=Client();unauth=guest.post("/ledger/add-transaction/",values,HTTP_X_OFFLINE_SYNC="1")
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(owner)
        no_token=csrf.post("/ledger/add-transaction/",values)
        stranger=get_user_model().objects.create_user("audit-stranger",password=uuid.uuid4().hex)
        wrong=Client();wrong.force_login(stranger);denied=wrong.post("/ledger/add-transaction/",values)
        assert (unauth.status_code,no_token.status_code,denied.status_code)==(401,403,403)
        assert not Entry.objects.filter(firm=firm).exists()
        return dict(unauthenticated_offline=401,missing_csrf=403,other_account=403,financial_rows=0)
    scenario("C05_HTTP_owner_authentication_and_CSRF",access_checks,"passed")

    def admin_soft_deleted_balance():
        from django.contrib import admin
        firm,rep=account()
        Entry.objects.create(firm=firm,entry_type="opening_balance",date=today,increase=100,is_deleted=True)
        visible=Entry.objects.create(firm=firm,entry_type="opening_balance",date=today,increase=50)
        admin_balance=admin.site._registry[Entry].running_balance(visible)
        assert admin_balance==150 and sql_balance(firm)==50
        return dict(admin_running_balance=admin_balance,ledger_balance=sql_balance(firm),path="admin reporting on legacy soft-deleted entries")
    scenario("A23_admin_running_balance_includes_soft_deleted_entries",admin_soft_deleted_balance)

    def direct_edit_desync():
        firm,rep=account();bill=create(firm,rep,"100")["bill"]
        bill.bill_amount=D("200");bill.save(update_fields=["bill_amount"])
        assert sql_balance(firm)==100
        return dict(bill_amount=Bill.objects.get(pk=bill.pk).bill_amount,ledger_balance=sql_balance(firm),path="trusted ORM update; UI edit uses explicit adjustments")
    scenario("A24_direct_model_edit_leaves_ledger_unchanged",direct_edit_desync,"boundary")

    def recent_history_materialization():
        firm,rep=account();count=1000
        batches=Batch.objects.bulk_create([Batch(firm=firm,created_by=owner,date=today,previous_balance=i,balance_after=i+1) for i in range(count)])
        bills=Bill.objects.bulk_create([Bill(firm=firm,representative=rep,creation_batch=batch,bill_number=f"VOLUME-{i}",bill_date=today,
            bill_amount=1,previous_debt_at_bill_time=i) for i,batch in enumerate(batches)])
        Entry.objects.bulk_create([Entry(firm=firm,bill=bill,transaction_batch=batch,entry_type="bill_created",date=today,increase=1)
                                  for bill,batch in zip(bills,batches)])
        original=Entry.__init__;hydrated=0
        def counted(instance,*args,**kwargs):
            nonlocal hydrated
            hydrated+=1;return original(instance,*args,**kwargs)
        with patch.object(Entry,"__init__",counted): rows=views._history_rows(firm=firm,limit=10,activity_only=True)
        assert len(rows)==10 and hydrated==1000
        return dict(same_day_transactions=count,requested_limit=10,returned_rows=10,materialized_ledger_entries=hydrated,
                    fixture="valid bulk-built postings; no public bulk-write path is claimed")
    scenario("A25_recent_history_materializes_all_same_day_entries",recent_history_materialization)


def sequences(seeds, steps, first_seed=0):
    """State machine oracle holds source amounts/statuses independently of DB."""
    # Sequential arithmetic is independent of physical persistence. Keep the
    # focused/race cases on real files, but avoid 20,000 Windows fsync cycles.
    from django.db import connections
    from django.core.management import call_command
    from django.contrib.auth import get_user_model
    from django.test import Client
    global owner, client
    connections.close_all()
    connections["default"].settings_dict["NAME"] = ":memory:"
    call_command("migrate", interactive=False, verbosity=0)
    owner=get_user_model().objects.create_superuser("audit-owner",password=uuid.uuid4().hex)
    client=Client(); client.force_login(owner)
    from django.core.exceptions import ValidationError
    start = time.perf_counter()
    summary = dict(backend="in-memory SQLite; races separately use file SQLite", seeds=seeds, steps_per_seed=steps, completed_operations=0,
                   balance_mismatches=0, history_mismatches=0, relationship_violations=0,
                   stale_snapshot_observations=0, examples=[], operations={})
    for seed in range(first_seed, first_seed + seeds):
        rng = random.Random(seed)
        # One local-market and one company stream per seed, alternating.
        firm, rep = account(local=bool(seed % 2))
        roots, groups = {}, {}
        trace = []
        def expected_balance():
            return sum((r["bill_amount"]-r["payment_amount"] for r in roots.values() if r["active"]), ZERO)
        for step in range(steps):
            operation = rng.choice(("bill", "payment", "edit", "reverse", "recover", "delete_bill", "replay"))
            active = [key for key, r in roots.items() if r["active"]]
            active_bills = [key for key in active if roots[key]["bill_id"] and roots[key]["owns_bill"]]
            action = {"step": step, "op": operation}
            try:
                if operation == "bill" or not roots:
                    amount = D(rng.randrange(1, 100000))/100
                    paid = amount if rep is None else ZERO
                    if rep:
                        available = expected_balance()+amount
                        if available > 0: paid = min(amount, available, D(rng.randrange(0,10000))/100)
                    values = data(firm, rep, amount, paid if paid else None, day=today-timedelta(days=rng.randrange(8)))
                    result = services.create_transaction_batch(values, user=owner, payload_hash=str(values["request_id"]))
                    key = result["batch"].pk
                    roots[key] = dict(bill_id=result["bill"].pk, owns_bill=True, bill_amount=amount,
                                      payment_amount=paid, active=True, date=values["bill_date"], data=values)
                    action.update(root=key, amount=str(amount), paid=str(paid))
                elif operation == "payment":
                    if rep is None or expected_balance() <= 0: operation="skipped"
                    else:
                        amount=min(expected_balance(),D(rng.randrange(1,10000))/100)
                        chosen=rng.choice(active_bills) if active_bills and rng.choice((True,False)) else None
                        bill=Bill.objects.get(pk=roots[chosen]["bill_id"]) if chosen else None
                        values=data(firm,rep,payment=amount,bill=bill,day=today-timedelta(days=rng.randrange(8)))
                        result=services.create_transaction_batch(values,user=owner,payload_hash=str(values["request_id"]))
                        roots[result["batch"].pk]=dict(bill_id=bill.pk if bill else None,owns_bill=False,
                            bill_amount=ZERO,payment_amount=amount,active=True,date=values["bill_date"],data=values)
                        action.update(root=result["batch"].pk,amount=str(amount))
                elif operation == "edit":
                    if not active_bills: operation="skipped"
                    else:
                        key=rng.choice(active_bills); root=roots[key]; bill=Bill.objects.get(pk=root["bill_id"])
                        amount=D(rng.randrange(1,100000))/100; day=today-timedelta(days=rng.randrange(8))
                        services.edit_bill(bill.pk,edit_values(bill,bill_amount=amount,bill_date=day),user=owner)
                        root["bill_amount"]=amount; root["date"]=day
                        if rep is None: root["payment_amount"]=amount
                        action.update(root=key,amount=str(amount),date=str(day))
                elif operation == "reverse":
                    key=rng.choice(list(roots)); result=services.reverse_transaction_batch(key,user=owner,reason="State machine")
                    if roots[key]["active"]:
                        roots[key]["active"]=False; groups[result["group"].pk]=dict(members=[key],restored=False)
                    action["root"]=key
                elif operation == "delete_bill":
                    candidates=[key for key,r in roots.items() if r["owns_bill"]]
                    if not candidates: operation="skipped"
                    else:
                        key=rng.choice(candidates); root=roots[key]
                        result=services.delete_bill(root["bill_id"],user=owner,reason="State machine")
                        if root["active"]:
                            members=[k for k,r in roots.items() if r["active"] and (k==key or (not r["owns_bill"] and r["bill_id"]==root["bill_id"]))]
                            groups[result["group"].pk]=dict(members=members,restored=False)
                            for k in members: roots[k]["active"]=False
                        action["root"]=key
                elif operation == "recover":
                    if not groups: operation="skipped"
                    else:
                        key=rng.choice(list(groups)); group=groups[key]
                        services.recover_deletion_group(key,user=owner,reason="State machine")
                        if not group["restored"]:
                            for k in group["members"]: roots[k]["active"]=True
                            group["restored"]=True
                        action["group"]=key
                elif operation == "replay":
                    key=rng.choice(list(roots)); values=roots[key]["data"]
                    result=services.create_transaction_batch(values,user=owner,payload_hash=str(values["request_id"]))
                    assert not result["created"]
                    action["root"]=key
            except ValidationError as exc:
                # Only expected rejection is ordinary payment after a debt change.
                action["rejected"]=str(exc)
            trace.append(action)
            summary["completed_operations"]+=1
            summary["operations"][operation]=summary["operations"].get(operation,0)+1
            expected=expected_balance(); stored=sql_balance(firm); app=firm.current_debt()
            if stored != expected or app != expected:
                summary["balance_mismatches"]+=1
                if len(summary["examples"])<8: summary["examples"].append(dict(kind="balance",seed=seed,step=step,expected=expected,stored=stored,app=app,trace=trace[-12:]))
            rows=views._history_rows(firm=firm)
            oracle_running=ZERO; expected_rows={}
            for key,root in sorted(roots.items(),key=lambda item:(item[1]["date"],item[0])):
                if root["active"]:
                    oracle_running+=root["bill_amount"]-root["payment_amount"]
                    expected_rows[key]=oracle_running
            if any(row["remaining_debt"]!=expected_rows.get(row["batch_id"]) for row in rows):
                summary["history_mismatches"]+=1
                if len(summary["examples"])<8: summary["examples"].append(dict(kind="history",seed=seed,step=step,trace=trace[-12:]))
            by_bill={r["bill_id"]:r["active"] for r in roots.values() if r["owns_bill"]}
            orphan=[key for key,r in roots.items() if r["active"] and not r["owns_bill"] and r["bill_id"] and not by_bill[r["bill_id"]]]
            if orphan:
                summary["relationship_violations"]+=1
                if len(summary["examples"])<8: summary["examples"].append(dict(kind="active_payment_deleted_bill",seed=seed,step=step,trace=trace[-12:]))
            if any(batch.balance_after!=expected_rows.get(batch.pk) for batch in Batch.objects.filter(firm=firm,kind="transaction",status="active")):
                summary["stale_snapshot_observations"]+=1
        if seed % 10 == 0: print(f"Sequences: seed {seed+1}/{seeds}; {summary['completed_operations']} operations",flush=True)
    summary["seconds"]=round(time.perf_counter()-start,3)
    RESULTS["sequences"]=summary


def races():
    from django.db import close_old_connections
    from django.core.exceptions import ValidationError
    def race(label, first, second, verify):
        barrier=threading.Barrier(2)
        def worker(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=20)
                result=fn()
                return {"status":"ok","changed":result.get("changed") if isinstance(result,dict) else None}
            except Exception as exc: return {"status":type(exc).__name__,"error":str(exc)}
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(worker,fn) for fn in (first,second)]
            outcomes=[future.result(timeout=60) for future in futures]
        evidence=verify()
        result=dict(name=label,outcomes=outcomes,evidence=evidence)
        RESULTS["races"].append(result); print("Race",label,json.dumps(result,default=str),flush=True)
    firm,rep=account(); create(firm,rep,"100")
    race("payment_payment",lambda:create(firm,rep,payment="80"),lambda:create(firm,rep,payment="80"),lambda:dict(balance=sql_balance(firm),payments=Payment.objects.filter(firm=firm).count()))
    firm,rep=account(); original=create(firm,rep,"100"); bill=original["bill"]
    one=edit_values(bill,bill_amount=D("200")); two=edit_values(bill,bill_amount=D("300"))
    race("edit_edit",lambda:services.edit_bill(bill.pk,one,user=owner),lambda:services.edit_bill(bill.pk,two,user=owner),lambda:dict(balance=sql_balance(firm),adjustments=Entry.objects.filter(firm=firm,entry_type="adjustment").count()))
    firm,rep=account(); original=create(firm,rep,"100"); bill=original["bill"]; change=edit_values(bill,bill_amount=D("200"))
    race("edit_delete",lambda:services.edit_bill(bill.pk,change,user=owner),lambda:services.delete_bill(bill.pk,user=owner,reason="Race"),lambda:dict(balance=sql_balance(firm),status=Batch.objects.get(pk=original["batch"].pk).status))
    firm,rep=account(); original=create(firm,rep,"100"); key=original["batch"].pk
    group=services.reverse_transaction_batch(key,user=owner,reason="Initial")["group"]
    race("delete_recover",lambda:services.reverse_transaction_batch(key,user=owner,reason="Race"),lambda:services.recover_deletion_group(group.pk,user=owner,reason="Race"),lambda:dict(balance=sql_balance(firm),status=Batch.objects.get(pk=key).status))
    firm,rep=account(); shared=data(firm,rep,"100")
    race("duplicate_request",lambda:services.create_transaction_batch(shared,user=owner,payload_hash="same"),lambda:services.create_transaction_batch(shared,user=owner,payload_hash="same"),lambda:dict(balance=sql_balance(firm),bills=Bill.objects.filter(firm=firm).count(),batches=Batch.objects.filter(firm=firm).count()))
    # A controlled stale read reproduces the entity lifecycle race in the actual view.
    firm,rep=account(); entered=threading.Event(); release=threading.Event()
    original_save=Firm.save
    def paused_save(instance,*args,**kwargs):
        if instance.pk==firm.pk and kwargs.get("update_fields")==["is_deleted","deleted_at"]:
            entered.set(); assert release.wait(20)
        return original_save(instance,*args,**kwargs)
    def remove():
        close_old_connections()
        try:
            from django.test import Client
            c=Client(); c.force_login(owner)
            return c.post(f"/ledger/firm/{firm.pk}/delete/").status_code
        finally: close_old_connections()
    with patch.object(Firm,"save",paused_save),ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(remove); assert entered.wait(20)
        out=create(firm,rep,"100"); release.set(); status=future.result(timeout=30)
    firm.refresh_from_db()
    RESULTS["races"].append(dict(name="supplier_delete_write",delete_status=status,firm_deleted=firm.is_deleted,committed_bill=out["bill"].pk,balance=sql_balance(firm),classification="serializable write-before-delete, retained history; policy/UX risk"))


def run_command(args, timeout):
    started=time.perf_counter()
    result=subprocess.run(args,cwd=ROOT,env=os.environ.copy(),capture_output=True,text=True,timeout=timeout)
    return dict(exit_code=result.returncode,seconds=round(time.perf_counter()-started,3),stdout=result.stdout,stderr=result.stderr)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"docs/accounting-audit-results.json")
    parser.add_argument("--seeds",type=int,default=100); parser.add_argument("--steps",type=int,default=200)
    parser.add_argument("--baseline",action="store_true"); parser.add_argument("--stress",action="store_true")
    parser.add_argument("--snapshot",action="store_true"); parser.add_argument("--races",action="store_true")
    args=parser.parse_args()
    source_hashes={str(path.relative_to(ROOT)):digest(path) for folder in ("ledger","pharmacy_app") for path in (ROOT/folder).rglob("*") if path.is_file() and path.suffix in (".py",".js",".html")}
    RESULTS["environment"]=dict(python=sys.version,packages={name:importlib.metadata.version(name) for name in ("Django","django-axes","asgiref","sqlparse","tzdata")},source_sha256=source_hashes)
    with tempfile.TemporaryDirectory(prefix="pharmacy-accounting-audit-") as directory:
        temp=Path(directory)
        if args.snapshot:
            originals=sorted(set(ROOT.glob("*.sqlite3")) | set((ROOT/"backups").glob("*.sqlite3")))
            RESULTS["snapshot"]={str(path.relative_to(ROOT)):inspect_snapshot(temp,path) for path in originals}
        setup(temp/"synthetic.sqlite3")
        if args.baseline:
            print("Running existing suite...",flush=True)
            RESULTS["baseline"]=run_command([sys.executable,"manage.py","test","ledger","--noinput","--verbosity","1"],300)
            print("Baseline",RESULTS["baseline"]["exit_code"],RESULTS["baseline"]["stderr"][-1800:],flush=True)
            RESULTS["checks"]=run_command([sys.executable,"manage.py","check"],60)
        focused()
        if args.races: races()
        if args.seeds: sequences(args.seeds,args.steps)
        if args.stress:
            print("Running isolated stress script...",flush=True)
            RESULTS["stress"]=run_command([sys.executable,"scripts/isolated_stress_test.py"],600)
            print("Stress",RESULTS["stress"]["exit_code"],RESULTS["stress"]["stdout"][-2500:],flush=True)
        RESULTS["source_files_unchanged"]=all(digest(ROOT/path)==value for path,value in source_hashes.items())
        if args.snapshot:
            for path, details in RESULTS["snapshot"].items(): details["source_sha256_final"]=digest(ROOT/path)
        from django.db import connections
        connections.close_all()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(RESULTS,indent=2,default=str),encoding="utf-8")
    errors=sum(r["status"]=="harness_error" for r in RESULTS["focused"])
    print(f"Saved {args.output}; focused harness errors: {errors}",flush=True)
    return 1 if errors else 0


if __name__=="__main__":
    raise SystemExit(main())
