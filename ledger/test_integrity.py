"""Regression attacks against supported writes, plus labelled maintenance probes."""
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import uuid
from unittest.mock import patch
from contextlib import closing

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .admin import LedgerEntryAdmin
from .backup import create_snapshot, verify_snapshot
from .forms import FirmForm, RepresentativeForm
from .models import Bill, BillEditEvent, Firm, LedgerEntry, Payment, Representative, TransactionBatch
from .money import MAX_AMOUNT, MoneySum
from .batch_context import use_batch
from .services import bill_revision, create_transaction_batch, delete_bill, edit_bill, recover_deletion_group, reverse_transaction_batch
from .tests import LedgerWorkflowTests as WorkflowHelpers
from .views import _history_rows, _total_net_debt


@override_settings(APP_OWNER_USERNAME="owner", SECURE_SSL_REDIRECT=False,
                   PASSWORD_HASHERS=("django.contrib.auth.hashers.MD5PasswordHasher",))
class IntegrityTests(TestCase):
    setUp = WorkflowHelpers.setUp
    transaction_data = WorkflowHelpers.transaction_data
    post_transaction = WorkflowHelpers.post_transaction
    create_batch = WorkflowHelpers.create_batch

    def values(self, **changes):
        values = dict(firm=self.firm, representative=self.rep, request_id=uuid.uuid4(),
                      bill_choice="add_new", new_bill_number="REFERENCE", new_bill_amount=Decimal("100"))
        values.update(changes)
        return values

    def post(self, **changes):
        return create_transaction_batch(self.values(**changes), user=self.owner, payload_hash="test")

    def edit(self, bill, **changes):
        bill.refresh_from_db()
        data = dict(revision=bill_revision(bill), bill_number=bill.bill_number, bill_date=bill.bill_date,
                    bill_amount=bill.bill_amount, representative=bill.representative, notes=bill.notes)
        data.update(changes)
        return edit_bill(bill.pk, data, user=self.owner)

    def counts(self):
        return [m.objects.count() for m in (Bill, Payment, LedgerEntry, TransactionBatch, BillEditEvent)]

    def test_historical_source_and_representative_cannot_move(self):
        self.post()
        other = Firm.objects.create(name="Other", source_type="stockist")
        self.assertFalse(FirmForm({"name": "Edited", "source_type": "local_market"}, instance=self.firm).is_valid())
        self.assertFalse(RepresentativeForm({"source_type": "stockist", "firm": other.pk, "name": "Moved"}, instance=self.rep).is_valid())
        self.firm.source_type = "local_market"
        with self.assertRaises(ValidationError):
            self.firm.save()
        self.rep.firm = other
        with self.assertRaises(ValidationError):
            self.rep.save()
        self.firm.refresh_from_db(); self.rep.refresh_from_db()
        self.assertEqual(self.firm.source_type, "distributor")
        self.assertEqual(self.rep.firm_id, self.firm.pk)

    def test_local_market_reclassification_and_stale_rep_creation_keep_affiliations_valid(self):
        self.firm.source_type = "local_market"
        self.firm.save()
        unused = Firm.objects.create(name="Unused", source_type="stockist")
        stale = Firm.objects.get(pk=unused.pk)
        unused.source_type = "local_market"; unused.save()
        rep = Representative.objects.create(firm=stale, name="Stale affiliation")
        self.assertEqual(rep.firm_id, unused.pk)

    def test_financial_admin_and_direct_model_save_are_closed(self):
        for model in (Bill, Payment, LedgerEntry, TransactionBatch, BillEditEvent):
            self.assertFalse(admin.site._registry[model].has_add_permission(None))
        before = self.counts()
        for model, fields in ((Bill, {"bill_number": "bad", "bill_amount": 100}), (Payment, {"amount": 100})):
            with self.assertRaises(ValidationError):
                model.objects.create(firm=self.firm, **fields)
        self.assertEqual(before, self.counts())

    def test_service_invalid_amounts_leave_no_records(self):
        for value in ("0", "-1", "0.001", "99999999999", "NaN", "Infinity", "bad"):
            for field in ("new_bill_amount", "payment_amount"):
                before = self.counts()
                with self.subTest(value=value, field=field), self.assertRaises(ValidationError):
                    self.post(**{field: value})
                self.assertEqual(before, self.counts())

    def test_empty_incomplete_or_wrong_relationship_action_rejected(self):
        other = Firm.objects.create(name="Other", source_type="stockist")
        rep = Representative.objects.create(firm=other, name="Other")
        for changes in ({"bill_choice": "", "new_bill_amount": None}, {"new_bill_amount": None},
                        {"new_bill_number": ""}, {"representative": rep}, {"bill_choice": "bad", "new_bill_amount": None, "payment_amount": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.post(**changes)
        self.assertEqual(self.counts(), [0] * 5)

    def test_cumulative_and_backdated_balances_reject_before_commit(self):
        self.post(new_bill_amount=MAX_AMOUNT)
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.post(new_bill_amount=Decimal("0.01"))
        self.assertEqual(before, self.counts())

        self.assertEqual(self.firm.current_debt(), MAX_AMOUNT)
        self.post(bill_choice=str(Bill.objects.get(firm=self.firm).pk), new_bill_amount=None, payment_amount=MAX_AMOUNT)
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.post(new_bill_amount=Decimal("1"), bill_date=__import__('django').utils.timezone.localdate() - timedelta(days=1))
        self.assertEqual(before, self.counts())

    def test_matched_near_limit_action_accepts_readable_batch_balances(self):
        self.post(new_bill_amount=MAX_AMOUNT)
        result = self.post(new_bill_amount=MAX_AMOUNT, payment_amount=MAX_AMOUNT)
        self.assertEqual(self.firm.current_debt(), MAX_AMOUNT)
        result["batch"].refresh_from_db()
        self.assertEqual(result["batch"].previous_balance, MAX_AMOUNT)
        self.assertEqual(result["batch"].balance_after, MAX_AMOUNT)
        market = Firm.objects.create(name="Legacy Market", source_type="local_market")
        LedgerEntry.objects.create(firm=market, entry_type="opening_balance", increase=MAX_AMOUNT)
        cash = self.post(firm=market, representative=None, new_bill_amount=MAX_AMOUNT, payment_amount=MAX_AMOUNT)
        self.assertEqual(market.current_debt(), MAX_AMOUNT)
        self.assertEqual(cash["bill"].bill_amount, cash["payment"].amount)

    def test_immutable_receipt_and_complete_edit_evidence(self):
        result = self.post(payment_amount=Decimal("10"), notes="Before")
        bill, root = result["bill"], result["batch"]
        receipt = root.posting_receipt
        new_rep = Representative.objects.create(firm=self.firm, name="New")
        self.edit(bill, bill_amount=Decimal("50"), bill_number="Changed", representative=new_rep, notes="After")
        root.refresh_from_db()
        self.assertEqual(root.posting_receipt, receipt)
        event = BillEditEvent.objects.get(batch=root)
        self.assertEqual(event.before["bill"]["notes"], "Before")
        self.assertEqual(event.after["bill"]["notes"], "After")
        self.assertEqual(event.before["payments"][0]["representative_id"], self.rep.pk)
        self.assertEqual(event.after["payments"][0]["representative_id"], new_rep.pk)
        with self.assertRaises(ValidationError):
            event.save()
        root.posting_receipt = {"forged": True}
        with self.assertRaises(ValidationError):
            root.save()

    def test_original_actor_identity_survives_account_rename_and_deletion(self):
        result = self.post()
        self.edit(result["bill"], notes="Correction")
        event = BillEditEvent.objects.get(batch=result["batch"])
        actor_id = self.owner.pk
        self.owner.username = "Renamed"; self.owner.save()
        self.owner.delete()
        event.refresh_from_db(); result["batch"].refresh_from_db()
        self.assertIsNone(event.actor_id)
        self.assertEqual(event.actor_snapshot, {"id": actor_id, "username": "owner"})
        self.assertEqual(result["batch"].posting_receipt["actor"], {"id": actor_id, "username": "owner"})

    def test_stale_actor_and_nonowner_service_calls_are_rejected(self):
        from django.contrib.auth import get_user_model
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        before = self.counts()
        self.assertTrue(self.owner.is_active)
        with self.assertRaises(ValidationError):
            self.post()
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=True)
        stranger = get_user_model().objects.create_user("Other")
        with self.assertRaises(ValidationError):
            create_transaction_batch(self.values(), user=stranger, payload_hash="test")
        self.assertEqual(before, self.counts())

    def test_retries_acknowledge_reversed_action_despite_deleted_relationships(self):
        data = self.transaction_data(bill_choice="add_new", bill_amount="100", payment_amount="10")
        first = self.post_transaction(data).json()
        root = TransactionBatch.objects.get(pk=first["batch_id"])
        reverse_transaction_batch(root.pk, user=self.owner, reason="Correction")
        self.rep.is_deleted = True; self.rep.save()
        self.firm.is_deleted = True; self.firm.save()
        before = self.counts()
        retry = self.post_transaction(data)
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.json()["status"], "duplicate")
        self.assertEqual(retry.json()["action_state"], "reversed")
        self.assertEqual(retry.json()["posting_receipt"], first["posting_receipt"])
        self.assertEqual(before, self.counts())

    def test_selected_bill_retry_precedes_mutable_form_validation(self):
        bill = self.post()["bill"]
        data = self.transaction_data(bill_choice=str(bill.pk), payment_amount="10")
        self.assertEqual(self.post_transaction(data).status_code, 200)
        delete_bill(bill.pk, user=self.owner, reason="Deleted")
        before = self.counts()
        self.assertEqual(self.post_transaction(data).json()["status"], "duplicate")
        self.assertEqual(before, self.counts())

    def test_delete_recover_refresh_chronology_and_preserve_credit(self):
        original = self.post()
        later = self.post(bill_choice=str(original["bill"].pk), new_bill_amount=None, payment_amount=Decimal("80"))
        group = reverse_transaction_batch(original["batch"].pk, user=self.owner, reason="Correction")["group"]
        self.assertEqual(self.firm.current_debt(), Decimal("-80"))
        later["batch"].refresh_from_db()
        self.assertEqual(later["batch"].previous_balance, 0)
        self.assertEqual(later["batch"].balance_after, Decimal("-80"))
        recover_deletion_group(group.pk, user=self.owner, reason="Restore")
        later["batch"].refresh_from_db()
        self.assertEqual(later["batch"].balance_after, Decimal("20"))

    def test_non_request_integrity_failure_is_not_a_request_conflict(self):
        before = self.counts()
        with patch("ledger.services.Payment.objects.create", side_effect=IntegrityError("injected unrelated constraint")):
            with self.assertRaises(IntegrityError):
                self.post(payment_amount=Decimal("10"))
        self.assertEqual(before, self.counts())
        self.post(payment_amount=Decimal("10"))
        self.assertEqual(self.firm.current_debt(), Decimal("90"))

    def test_partial_receipt_and_edit_failures_roll_back_all_money(self):
        before = self.counts()
        with patch("ledger.services._receipt", side_effect=RuntimeError("injected receipt failure")):
            with self.assertRaises(RuntimeError):
                self.post()
        self.assertEqual(before, self.counts())
        bill = self.post()["bill"]
        before = self.counts()
        with patch("ledger.services.BillEditEvent.objects.create", side_effect=RuntimeError("evidence failure")):
            with self.assertRaises(RuntimeError):
                self.edit(bill, bill_amount=Decimal("50"))
        self.assertEqual(before, self.counts())
        bill.refresh_from_db()
        self.assertEqual(bill.bill_amount, Decimal("100"))

    def test_partial_deletion_failure_rolls_back_group_offsets_and_members(self):
        original = self.post(payment_amount=Decimal("10"))
        before = self.counts()
        with patch("ledger.services.DeletionMember.objects.create", side_effect=RuntimeError("membership failure")):
            with self.assertRaises(RuntimeError):
                delete_bill(original["bill"].pk, user=self.owner, reason="Fault")
        self.assertEqual(before, self.counts())
        original["batch"].refresh_from_db()
        self.assertEqual(original["batch"].status, "active")
        self.assertFalse(original["batch"].deletion_memberships.exists())
        self.assertEqual(self.firm.current_debt(), 90)

    def test_expired_ajax_session_returns_authentication_status_and_keeps_money(self):
        self.client.logout()
        before = self.counts()
        response = self.post_transaction(self.transaction_data(bill_choice="add_new", bill_amount="100"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(before, self.counts())

    def test_obsolete_offline_client_cannot_post_but_committed_retry_is_acknowledged(self):
        data = self.transaction_data(bill_choice="add_new", bill_amount="100")
        before = self.counts()
        response = self.client.post(reverse("ledger:add_transaction"), data, HTTP_X_REQUESTED_WITH="XMLHttpRequest", HTTP_X_OFFLINE_SYNC="1")
        self.assertEqual(response.status_code, 428)
        self.assertEqual(before, self.counts())
        first = self.post_transaction(data)
        self.assertEqual(first.status_code, 200)
        before = self.counts()
        retry = self.client.post(reverse("ledger:add_transaction"), data, HTTP_X_REQUESTED_WITH="XMLHttpRequest", HTTP_X_OFFLINE_SYNC="1")
        self.assertEqual(retry.json()["status"], "duplicate")
        self.assertEqual(before, self.counts())

    def test_report_strings_keep_exact_cents_across_year_boundary(self):
        self.post(new_bill_amount=Decimal("1.23"), payment_amount=Decimal("0.01"), bill_date=date(2025, 12, 31))
        self.post(new_bill_amount=Decimal("1.23"), payment_amount=Decimal("0.01"), bill_date=date(2026, 1, 1))
        with patch("ledger.views.timezone.localdate", return_value=date(2026, 1, 1)):
            payments = self.client.get(reverse("ledger:dashboard_payments_trend"), {"period": "year"}).json()
            bills = self.client.get(reverse("ledger:dashboard_business_trend"), {"period": "year"}).json()
            debt = self.client.get(reverse("ledger:dashboard_debt_over_time"), {"period": "year"}).json()
        self.assertEqual(payments["monetary_values"], ["0.01"])
        self.assertEqual(bills["monetary_amount_values"], ["1.23"])
        self.assertEqual(debt["monetary_values"], ["2.44"])
        self.assertEqual(self.firm.current_debt(), Decimal("2.44"))

    def test_recent_actions_hydrate_only_selected_complete_batches(self):
        for _ in range(30):
            self.post(payment_amount=Decimal("1"))
        original = LedgerEntry.from_db
        hydrated = []
        def observe(*args):
            entry = original(*args); hydrated.append(entry.pk); return entry
        with patch.object(LedgerEntry, "from_db", side_effect=observe):
            rows = _history_rows(limit=10, activity_only=True)
        self.assertEqual(len(rows), 10)
        self.assertEqual(len(hydrated), 20)
        self.assertEqual(rows[0]["remaining_debt"], Decimal("2970"))

    def test_admin_excludes_soft_deleted_entries(self):
        # Maintenance-only legacy fixture; opening entries have no posting batch.
        LedgerEntry.objects.create(firm=self.firm, entry_type="opening_balance", increase=100, is_deleted=True)
        entry = LedgerEntry.objects.create(firm=self.firm, entry_type="opening_balance", increase=10)
        self.assertEqual(LedgerEntryAdmin(LedgerEntry, admin.site).running_balance(entry), Decimal("10"))

    def test_legacy_evidence_remains_unknown_and_unchanged_after_new_post(self):
        root = TransactionBatch.objects.create(firm=self.firm, previous_balance=999, balance_after=888)
        with use_batch(root):
            legacy = Bill.objects.create(firm=self.firm, representative=self.rep, bill_number="Legacy", bill_amount=100, previous_debt_at_bill_time=777)
        self.post()
        root.refresh_from_db(); legacy.refresh_from_db()
        self.assertIsNone(root.posting_receipt)
        self.assertEqual((root.previous_balance, root.balance_after, legacy.previous_debt_at_bill_time), (999, 888, 777))
        self.assertEqual(_history_rows(firm=self.firm, limit=10)[-1]["remaining_debt"], 100)

    def test_legacy_soft_deleted_bill_cannot_accept_payment_or_edit(self):
        result = self.post()
        LedgerEntry.objects.filter(transaction_batch=result["batch"], entry_type="bill_created").update(is_deleted=True)
        self.assertFalse(Bill.objects.available().filter(pk=result["bill"].pk).exists())
        with self.assertRaises(ValidationError):
            self.post(bill_choice=str(result["bill"].pk), new_bill_amount=None, payment_amount=1)
        with self.assertRaises(ValidationError):
            self.edit(result["bill"], bill_amount=Decimal("200"))

    def test_application_level_large_cancelling_totals_keep_one_cent(self):
        # Synthetic maintenance load reproduces the aggregate query defect without 40k UI posts.
        for _ in range(20):
            LedgerEntry.objects.bulk_create([LedgerEntry(firm=self.firm, entry_type="opening_balance", increase=MAX_AMOUNT if i % 2 == 0 else 0, decrease=MAX_AMOUNT if i % 2 else 0) for i in range(2000)])
        LedgerEntry.objects.create(firm=self.firm, entry_type="opening_balance", increase=Decimal("0.01"))
        oracle = sum((e.increase - e.decrease for e in LedgerEntry.objects.filter(firm=self.firm)), Decimal("0"))
        self.assertEqual(oracle, Decimal("0.01"))
        self.assertEqual(self.firm.current_debt(), oracle)
        self.assertEqual(_total_net_debt(), oracle)

    def test_database_constraints_reject_raw_negative_and_fractional_money(self):
        # Direct SQL requires database access; this is defence in depth.
        entry = LedgerEntry.objects.create(firm=self.firm, entry_type="adjustment", increase=0)
        for amount in (-1, 0.001, float('inf')):
            with self.subTest(amount=amount), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE ledger_ledgerentry SET increase=%s WHERE id=%s", [amount, entry.pk])
        LedgerEntry.objects.create(firm=self.firm, entry_type="adjustment", increase=0, decrease=0)

    def test_stored_content_remains_escaped_and_nonowner_cannot_post(self):
        bill = self.post(notes='<script>alert("stored")</script>')["bill"]
        self.edit(bill, notes='<img src=x onerror="alert(1)">')
        response = self.client.get(reverse("ledger:ledger_history"))
        self.assertNotContains(response, '<img src=x onerror="alert(1)">')
        self.assertContains(response, '&lt;img')
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user("outsider")
        self.client.force_login(other)
        before = self.counts()
        self.assertEqual(self.post_transaction(self.transaction_data(bill_choice="add_new", bill_amount="100")).status_code, 403)
        self.assertEqual(before, self.counts())

    def test_backup_uses_configured_database_and_rejects_incomplete_corrupt_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "configured.sqlite3", Path(directory) / "backup.sqlite3"
            with closing(sqlite3.connect(source)) as db:
                for table in ("django_migrations", "ledger_firm", "ledger_bill", "ledger_payment", "ledger_ledgerentry"):
                    db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO ledger_ledgerentry VALUES (1)")
                db.commit()
            with override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": source}}):
                self.assertEqual(create_snapshot(target)["ledger_entries"], 1)
                with self.assertRaises(ValueError):
                    create_snapshot(source)
                with self.assertRaises(ValueError):
                    create_snapshot(target)
                failed = Path(directory) / "unpublished.sqlite3"
                with patch("ledger.backup.os.link", side_effect=OSError("injected storage failure")), self.assertRaises(OSError):
                    create_snapshot(failed)
                self.assertFalse(failed.exists())
                self.assertEqual(list(Path(directory).glob(".ledger-backup-*")), [])
            self.assertEqual(verify_snapshot(target)["ledger_entries"], 1)
            bad = Path(directory) / "bad.sqlite3"; bad.write_bytes(b"corrupt")
            with self.assertRaises(sqlite3.DatabaseError):
                verify_snapshot(bad)
            empty = Path(directory) / "empty.sqlite3"; empty.touch()
            with self.assertRaises(ValueError):
                verify_snapshot(empty)
