"""Reachable invoice settlement and immutable carry-forward regressions."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
import json
import uuid
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import BillEditForm, BillForm, CarryForwardForm
from .models import Bill, BillCarryForward, Firm, LedgerEntry, Payment, Representative, TransactionBatch
from .services import (bill_revision, carry_forward_bill, create_transaction_batch, delete_bill, edit_bill,
                       reverse_transaction_batch, undo_carry_forward)
from .tests import LedgerWorkflowTests as Helpers
from .views import _history_rows, _dashboard_payment_totals


@override_settings(APP_OWNER_USERNAME="owner", SECURE_SSL_REDIRECT=False,
                   PASSWORD_HASHERS=("django.contrib.auth.hashers.MD5PasswordHasher",))
class CarryForwardTests(TestCase):
    setUp = Helpers.setUp
    transaction_data = Helpers.transaction_data
    post_transaction = Helpers.post_transaction
    create_batch = Helpers.create_batch

    def bill(self, amount="100", firm=None, representative=None, number="B", payment=None):
        firm = firm or self.firm
        return create_transaction_batch(dict(request_id=uuid.uuid4(), firm=firm,
            representative=representative if representative is not None else (self.rep if firm == self.firm else None),
            bill_choice="add_new", new_bill_number=number, new_bill_amount=Decimal(amount),
            payment_amount=Decimal(payment) if payment else None), user=self.owner, payload_hash="create")["bill"]

    def pay(self, bill, amount):
        return create_transaction_batch(dict(request_id=uuid.uuid4(), firm=bill.firm, representative=bill.representative,
            bill_choice=str(bill.pk), payment_amount=Decimal(amount)), user=self.owner, payload_hash="payment")

    def transfer(self, source, destination=None, **changes):
        source.refresh_from_db()
        if destination:
            destination.refresh_from_db()
        data = dict(destination_id=destination.pk if destination else None, source_revision=bill_revision(source),
            destination_revision=bill_revision(destination) if destination else "", request_id=uuid.uuid4(),
            user=self.owner, payload_hash="transfer")
        data.update(changes)
        return carry_forward_bill(source.pk, **data)

    def undo(self, root, **changes):
        data = dict(user=self.owner, request_id=uuid.uuid4(), payload_hash="undo")
        data.update(changes)
        return undo_carry_forward(root.carry_forward.pk, **data)

    def edit(self, bill, amount):
        bill.refresh_from_db()
        return edit_bill(bill.pk, dict(revision=bill_revision(bill), bill_number=bill.bill_number,
            bill_date=bill.bill_date, bill_amount=Decimal(amount), representative=bill.representative, notes=bill.notes), user=self.owner)

    def counts(self):
        return tuple(model.objects.count() for model in (Bill, Payment, TransactionBatch, LedgerEntry, BillCarryForward))

    def test_older_and_newer_bills_support_installments_and_invoice_limit(self):
        older, newer = self.bill("100"), self.bill("200")
        self.pay(older, "30"); self.pay(newer, "50"); self.pay(older, "70")
        self.assertEqual(older.remaining_balance, 0)
        self.assertEqual(newer.remaining_balance, 150)
        self.assertEqual(self.firm.current_debt(), 150)
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.pay(older, "0.01")
        self.assertEqual(before, self.counts())

    def test_firm_credit_limits_settlement_without_erasing_credit(self):
        credit, payable = self.bill("100", payment="100"), self.bill("200")
        self.edit(credit, "10")
        self.assertEqual(credit.remaining_balance, -90)
        with self.assertRaises(ValidationError):
            self.pay(payable, "111")
        self.pay(payable, "110")
        self.assertEqual(self.firm.current_debt(), 0)
        self.assertEqual(payable.remaining_balance, 90)
        with self.assertRaises(ValidationError):
            self.transfer(credit, payable)

    def test_transfer_moves_remaining_only_and_preserves_actual_totals(self):
        source, target = self.bill("10000", payment="4000"), self.bill("8000")
        receipt = source.creation_batch.posting_receipt
        root = self.transfer(source, target)["batch"]
        source.refresh_from_db()
        self.assertTrue(source.is_disabled)
        self.assertEqual((source.remaining_balance, target.remaining_balance, self.firm.current_debt()), (0, 14000, 14000))
        self.assertEqual((target.bill_amount, target.carried_debt, source.transferred_out), (8000, 6000, 6000))
        self.assertEqual(sum(e.increase - e.decrease for e in root.entries.all()), 0)
        self.assertEqual(root.carry_forward.receipt["destination_after"], "14000.00")
        source.creation_batch.refresh_from_db()
        self.assertEqual(source.creation_batch.posting_receipt, receipt)
        rows = _history_rows()
        self.assertEqual(sum(r["bill_amount"] for r in rows), 18000)
        self.assertEqual(sum(r["payment_made"] for r in rows), 4000)
        self.assertEqual(_dashboard_payment_totals(timezone.localdate())["today_payment_total"], 4000)

    def test_disabled_source_and_participant_deletions_are_protected(self):
        source, target = self.bill(payment="10"), self.bill()
        payment = self.pay(source, "10")
        root = self.transfer(source, target)["batch"]
        for action in [lambda: self.pay(source, "1"), lambda: self.edit(source, "200"),
                       lambda: delete_bill(source.pk, user=self.owner, reason="test"),
                       lambda: delete_bill(target.pk, user=self.owner, reason="test"),
                       lambda: reverse_transaction_batch(payment["batch"].pk, user=self.owner, reason="test")]:
            before = self.counts()
            with self.assertRaises(ValidationError):
                action()
            self.assertEqual(before, self.counts())
        self.undo(root)
        source.refresh_from_db()
        self.assertFalse(source.is_disabled)
        self.pay(source, "1")

    def test_chains_require_reverse_order_and_payment_dependency(self):
        a, b, c = self.bill(), self.bill(), self.bill()
        first = self.transfer(a, b)["batch"]
        second = self.transfer(b, c)["batch"]
        with self.assertRaises(ValidationError):
            self.undo(first)
        payment = self.pay(c, "201")
        with self.assertRaises(ValidationError):
            self.undo(second)
        reverse_transaction_batch(payment["batch"].pk, user=self.owner, reason="undo payment")
        self.undo(second); self.undo(first)
        self.assertEqual([bill.remaining_balance for bill in (a, b, c)], [100, 100, 100])

    def test_zero_paid_source_and_immutable_receipt(self):
        source, target = self.bill(payment="100"), self.bill()
        root = self.transfer(source, target)["batch"]
        self.assertEqual(root.carry_forward.amount, 0)
        with self.assertRaises(ValidationError):
            root.carry_forward.save()
        self.undo(root)
        self.assertEqual(self.firm.current_debt(), 100)

    def test_stale_source_and_destination_quotes_change_nothing(self):
        source, target = self.bill(), self.bill()
        source_quote, target_quote = bill_revision(source), bill_revision(target)
        self.pay(source, "1")
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.transfer(source, target, source_revision=source_quote)
        self.pay(target, "1")
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.transfer(source, target, destination_revision=target_quote)
        self.assertEqual(before, self.counts())

    def test_wrong_firm_and_deleted_destinations_rejected(self):
        source = self.bill()
        market = Firm.objects.create(name="Other", source_type="local_market")
        other = self.bill(firm=market, number="")
        with self.assertRaises(ValidationError):
            self.transfer(source, other)
        target = self.bill()
        delete_bill(target.pk, user=self.owner, reason="deleted")
        with self.assertRaises(ValidationError):
            self.transfer(source, target)

    def test_atomic_new_local_destination_and_rollback_at_receipt_write(self):
        market = Firm.objects.create(name="Market", source_type="local_market")
        source = self.bill(firm=market, number="", amount="60")
        new = dict(bill_number="", bill_amount=Decimal("80"), bill_date=timezone.localdate(), representative=None)
        before = self.counts()
        with patch("ledger.services.BillCarryForward.objects.create", side_effect=RuntimeError("receipt failure")):
            with self.assertRaises(RuntimeError):
                self.transfer(source, new_bill=new)
        source.refresh_from_db()
        self.assertEqual(before, self.counts())
        self.assertFalse(source.is_disabled)
        root = self.transfer(source, new_bill=new)["batch"]
        target = root.carry_forward.destination
        self.assertEqual((target.bill_number, target.bill_amount, target.remaining_balance), ("", 80, 140))
        self.assertEqual(market.current_debt(), 140)
        self.assertEqual(Payment.objects.count(), 0)

    def test_transfer_and_undo_committed_retries_acknowledge_after_state_changes(self):
        source, target = self.bill(), self.bill()
        request = uuid.uuid4()
        root = self.transfer(source, target, request_id=request)["batch"]
        before = self.counts()
        self.assertFalse(self.transfer(source, target, request_id=request)["created"])
        self.assertEqual(before, self.counts())
        undo_request = uuid.uuid4()
        self.undo(root, request_id=undo_request)
        self.assertFalse(self.undo(root, request_id=undo_request)["created"])
        self.assertFalse(self.transfer(source, target, request_id=request)["created"])
        with self.assertRaises(ValidationError):
            self.transfer(source, target, request_id=request, payload_hash="changed")

    def test_optional_local_number_representative_and_fallback_everywhere(self):
        market = Firm.objects.create(name="Market", source_type="local_market")
        rep = Representative.objects.create(firm=market, name="Local rep")
        a, b = self.bill(firm=market, number=""), self.bill(firm=market, representative=rep, number="")
        self.assertNotEqual(a.display_reference, b.display_reference)
        self.assertEqual(Payment.objects.count(), 0)
        self.pay(b, "10")
        lookup = self.client.get(reverse("ledger:representatives_for_firm"), {"firm_id": market.pk}).json()
        self.assertEqual({row["display_reference"] for row in lookup["bills"]}, {a.display_reference, b.display_reference})
        self.assertEqual(lookup["representatives"][0]["id"], rep.pk)
        for route in ("bills", "ledger_history"):
            self.assertContains(self.client.get(reverse("ledger:" + route)), a.display_reference)
        self.assertFalse(Bill.objects.filter(firm=market).exclude(bill_number="").exists())
        with self.assertRaises(ValidationError):
            self.bill(number="")

    def test_online_confirmation_cancel_and_api_history(self):
        source, target = self.bill(), self.bill()
        endpoint = reverse("ledger:carry_forward", args=[source.pk])
        page = self.client.get(endpoint)
        self.assertContains(page, "You are disabling a bill with remaining debt")
        self.assertContains(page, "آپ یہ بل غیر فعال کر رہے ہیں")
        before = self.counts()
        data = dict(request_id=str(uuid.uuid4()), source_revision=bill_revision(source), destination=str(target.pk), destination_revision=bill_revision(target))
        self.assertEqual(self.client.post(endpoint, data).status_code, 200)
        self.assertEqual(before, self.counts())
        data["confirm"] = "on"
        self.assertEqual(self.client.post(endpoint, data, HTTP_X_OFFLINE_SYNC="1").status_code, 400)
        self.assertEqual(self.client.post(endpoint, data).status_code, 302)
        self.assertEqual(self.client.post(endpoint, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["status"], "duplicate")

    def test_old_queue_review_and_committed_legacy_retry(self):
        data = self.transaction_data(bill_choice="add_new", bill_amount="100")
        self.assertEqual(self.post_transaction(data).status_code, 200)
        self.assertEqual(self.client.post(reverse("ledger:add_transaction"), data, HTTP_X_OFFLINE_SYNC="1", HTTP_X_LEDGER_QUEUE_VERSION="3", HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["status"], "duplicate")
        data["request_id"] = str(uuid.uuid4()); data.pop("posting_rules_version")
        before = self.counts()
        self.assertEqual(self.post_transaction(data).status_code, 428)
        self.assertEqual(before, self.counts())

    def test_undo_failure_rolls_back_source_state_and_offsets(self):
        source, target = self.bill(), self.bill()
        root = self.transfer(source, target)["batch"]
        before = self.counts()
        with patch("ledger.services._refresh_active_snapshots", side_effect=RuntimeError("interrupted undo")):
            with self.assertRaises(RuntimeError):
                self.undo(root)
        source.refresh_from_db(); root.refresh_from_db()
        self.assertEqual(before, self.counts())
        self.assertTrue(source.is_disabled)
        self.assertEqual(root.status, "active")

    def test_interrupted_second_transfer_leg_rolls_back_first_leg(self):
        source, target = self.bill(), self.bill()
        before = self.counts()
        original = LedgerEntry.objects.create
        legs = []
        def interrupted(**values):
            legs.append(values)
            if len(legs) == 2:
                raise RuntimeError("interrupted between transfer legs")
            return original(**values)
        with patch("ledger.services.LedgerEntry.objects.create", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.transfer(source, target)
        source.refresh_from_db()
        self.assertEqual(before, self.counts())
        self.assertFalse(source.is_disabled)
        self.assertEqual((source.remaining_balance, target.remaining_balance), (100, 100))

    def test_backdated_destination_edit_keeps_corrected_and_posted_histories_distinct(self):
        source, target = self.bill(), self.bill()
        root = self.transfer(source, target)["batch"]
        receipt = root.posting_receipt
        target.refresh_from_db()
        edit_bill(target.pk, dict(revision=bill_revision(target), bill_number=target.bill_number,
            bill_amount=Decimal("200"), bill_date=timezone.localdate() - timedelta(days=370),
            representative=target.representative, notes="year boundary"), user=self.owner)
        root.refresh_from_db()
        self.assertEqual(root.posting_receipt, receipt)
        self.assertEqual(target.remaining_balance, 300)
        self.assertEqual(_history_rows()[0]["remaining_debt"], 300)
        self.assertEqual(sum(row["bill_amount"] for row in _history_rows()), 300)

    def test_read_only_reconciliation_reports_unassigned_legacy_evidence(self):
        self.bill()
        # Maintenance-only fixture represents a historical unlinked payment.
        from .batch_context import use_batch
        root = TransactionBatch.objects.create(firm=self.firm, created_by=self.owner)
        with use_batch(root):
            Payment.objects.create(firm=self.firm, amount=10)
        before = self.counts()
        output = StringIO()
        call_command("audit_bill_balances", stdout=output)
        report = json.loads(output.getvalue())
        self.assertTrue(report["reconciles"])
        self.assertEqual(report["payments_without_bill_link"], 1)
        self.assertEqual(report["firms_with_unassigned_nonzero_balance"], 1)
        self.assertEqual(before, self.counts())
        self.assertEqual(self.firm.current_debt(), 90)

    def test_bill_edit_reference_is_optional_only_for_local_market(self):
        company = self.bill()
        values = dict(bill_number="", bill_date=company.bill_date, bill_amount="100", representative=self.rep.pk,
            notes="", revision=bill_revision(company))
        self.assertFalse(BillEditForm(values, instance=company).is_valid())
        market = Firm.objects.create(name="Unnamed correction", source_type="local_market")
        local = self.bill(firm=market, number="OLD-NUMBER")
        values.update(representative="", revision=bill_revision(local))
        form = BillEditForm(values, instance=local)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(user=self.owner)
        local.refresh_from_db()
        self.assertEqual(local.bill_number, "")
