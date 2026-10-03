"""Bill corrections preserve accounting, idempotency, and Trash membership."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import BillEditForm, BillForm
from .models import Bill, Firm, LedgerEntry, Payment, Representative
from .services import (bill_revision, delete_bill, edit_bill, recover_deletion_group,
                       reverse_transaction_batch, restore_transaction_batch)
from .tests import LedgerWorkflowTests as WorkflowHelpers
from .views import _history_rows


@override_settings(APP_OWNER_USERNAME="owner", SECURE_SSL_REDIRECT=False,
                   PASSWORD_HASHERS=("django.contrib.auth.hashers.MD5PasswordHasher",))
class BillEditTests(TestCase):
    setUp = WorkflowHelpers.setUp
    transaction_data = WorkflowHelpers.transaction_data
    post_transaction = WorkflowHelpers.post_transaction
    create_batch = WorkflowHelpers.create_batch

    def new_bill(self, **changes):
        options = dict(bill_choice="add_new", bill_amount="1000", payment_amount="100")
        options.update(changes)
        root, _ = self.create_batch(**options)
        return Bill.objects.get(creation_batch=root)

    def data(self, bill, **changes):
        bill.refresh_from_db()
        values = dict(revision=bill_revision(bill), bill_number=bill.bill_number,
                      bill_date=bill.bill_date, bill_amount=bill.bill_amount,
                      representative=bill.representative, notes=bill.notes)
        values.update(changes)
        return values

    def post_data(self, bill, **changes):
        values = self.data(bill, **changes)
        values["bill_date"] = values["bill_date"].strftime("%d-%m-%Y")
        values["representative"] = getattr(values["representative"], "pk", "")
        return values

    def test_edit_amount_keeps_original_entries_and_later_transaction_balances(self):
        bill = self.new_bill()
        later, _ = self.create_batch(bill_choice=str(bill.pk), payment_amount="200")
        original = bill.creation_batch.entries.get(entry_type="bill_created")
        for amount, debt in [("1500", "1200"), ("800", "500")]:
            edit_bill(bill.pk, self.data(bill, bill_amount=Decimal(amount)), user=self.owner)
            self.assertEqual(self.firm.current_debt(), Decimal(debt))
            rows = {row["batch_id"]: row for row in _history_rows()}
            self.assertEqual(rows[bill.creation_batch_id]["bill_amount"], Decimal(amount))
            self.assertEqual(rows[later.pk]["remaining_debt"], Decimal(debt))
            later.refresh_from_db()
            self.assertEqual(later.previous_balance, Decimal(amount) - 100)
            self.assertEqual(later.balance_after, Decimal(debt))
        original.refresh_from_db()
        self.assertEqual(original.increase, Decimal("1000"))
        self.assertEqual(bill.creation_batch.entries.filter(entry_type="adjustment").count(), 2)
        business = self.client.get(reverse("ledger:dashboard_business_trend"), {"period": "month"}).json()
        self.assertEqual(sum(business["amount_values"]), 800)
        self.assertEqual(sum(business["count_values"]), 1)
        recent = self.client.get(reverse("ledger:dashboard_recent")).json()
        self.assertEqual(Decimal(recent["today_payment_total"]), 300)

    def test_local_market_cash_edit_preserves_actual_payments_and_exposes_credit(self):
        market = Firm.objects.create(name="Market", source_type="local_market")
        root, _ = self.create_batch(firm=market, bill_choice="add_new", bill_amount="1500", payment_amount="1500")
        bill = Bill.objects.get(creation_batch=root)
        for amount in ["2000", "500"]:
            edit_bill(bill.pk, self.data(bill, bill_amount=Decimal(amount)), user=self.owner)
            self.assertEqual(market.current_debt(), Decimal(amount) - 1500)
            self.assertEqual(Payment.objects.get(bill=bill).amount, Decimal("1500"))
            row = _history_rows()[0]
            self.assertEqual(row["bill_amount"], Decimal(amount))
            self.assertEqual(row["payment_made"], Decimal("1500"))
            recent = self.client.get(reverse("ledger:dashboard_recent")).json()
            self.assertEqual(Decimal(recent["today_payment_total"]), Decimal("1500"))
            self.assertEqual(recent["today_payment_count"], 1)
        group = delete_bill(bill.pk, user=self.owner, reason="Test")["group"]
        self.assertEqual(self.client.get(reverse("ledger:dashboard_recent")).json()["today_payment_count"], 0)
        recover_deletion_group(group.pk, user=self.owner, reason="Test")
        self.assertEqual(market.current_debt(), -1000)
        self.assertEqual(Decimal(self.client.get(reverse("ledger:dashboard_recent")).json()["today_payment_total"]), 1500)

    def test_edited_bill_group_recovery_keeps_independently_deleted_payment_deleted(self):
        bill = self.new_bill()
        earlier, _ = self.create_batch(bill_choice=str(bill.pk), payment_amount="100")
        later, _ = self.create_batch(bill_choice=str(bill.pk), payment_amount="200")
        reverse_transaction_batch(earlier.pk, user=self.owner, reason="Independent")
        # Edit after an earlier complete delete/recover cycle, then delete again.
        reverse_transaction_batch(bill.creation_batch_id, user=self.owner, reason="Test")
        restore_transaction_batch(bill.creation_batch_id, user=self.owner, reason="Test")
        edit_bill(bill.pk, self.data(bill, bill_amount=Decimal("1500")), user=self.owner)
        group = delete_bill(bill.pk, user=self.owner, reason="Edited bill")["group"]
        self.assertEqual(set(group.members.values_list("batch_id", flat=True)), {bill.creation_batch_id, later.pk})
        self.assertEqual(self.firm.current_debt(), 0)
        recover_deletion_group(group.pk, user=self.owner, reason="Restore edited bill")
        self.assertEqual(self.firm.current_debt(), Decimal("1200"))
        earlier.refresh_from_db()
        self.assertEqual(earlier.status, "reversed")
        delete_bill(bill.pk, user=self.owner, reason="Again")
        self.assertEqual(self.firm.current_debt(), 0)

    def test_date_number_rep_and_notes_update_only_creation_transaction(self):
        bill = self.new_bill()
        later, _ = self.create_batch(bill_choice=str(bill.pk), payment_amount="200")
        new_rep = Representative.objects.create(firm=self.firm, name="New rep")
        day = timezone.localdate() - timedelta(days=2)
        edit_bill(bill.pk, self.data(bill, bill_date=day, bill_number="Updated", representative=new_rep, notes="Note"), user=self.owner)
        bill.refresh_from_db()
        self.assertEqual(bill.bill_number, "Updated")
        self.assertEqual(bill.notes, "Note")
        self.assertEqual(bill.representative_id, new_rep.pk)
        paired = Payment.objects.get(ledger_entries__transaction_batch=bill.creation_batch, ledger_entries__entry_type="payment_made")
        self.assertEqual(paired.payment_date, day)
        self.assertEqual(paired.representative_id, new_rep.pk)
        self.assertEqual(Payment.objects.get(ledger_entries__transaction_batch=later).representative_id, self.rep.pk)
        self.assertEqual(set(bill.creation_batch.entries.values_list("date", flat=True)), {day})
        self.assertEqual(self.firm.current_debt(), 700)

    def test_duplicate_edit_and_stale_different_edit_do_not_apply_twice(self):
        bill = self.new_bill()
        data = self.data(bill, bill_amount=Decimal("1500"))
        self.assertTrue(edit_bill(bill.pk, data, user=self.owner)[1])
        self.assertFalse(edit_bill(bill.pk, data, user=self.owner)[1])
        count = LedgerEntry.objects.count()
        with self.assertRaisesMessage(ValidationError, "changed after you opened"):
            edit_bill(bill.pk, dict(data, bill_amount=Decimal("1600")), user=self.owner)
        self.assertEqual(LedgerEntry.objects.count(), count)
        self.assertEqual(self.firm.current_debt(), 1400)

    def test_bill_edit_form_permissions_validation_and_csrf(self):
        bill = self.new_bill()
        endpoint = reverse("ledger:edit_bill", args=[bill.pk])
        self.assertContains(self.client.get(endpoint), "Update Bill")
        self.assertEqual(Client().get(endpoint).status_code, 302)
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.owner)
        self.assertEqual(guarded.post(endpoint, self.post_data(bill)).status_code, 403)
        inactive = Representative.objects.create(firm=self.firm, name="Inactive", is_active=False)
        other = Firm.objects.create(name="Other", source_type="distributor")
        wrong = Representative.objects.create(firm=other, name="Wrong")
        for changes in [{"bill_amount": "0"}, {"bill_amount": "-1"}, {"representative": ""}, {"representative": inactive.pk}, {"representative": wrong.pk}]:
            data = self.post_data(bill)
            data.update(changes)
            self.assertFalse(BillEditForm(data, instance=Bill.objects.get(pk=bill.pk)).is_valid())
        self.rep.is_active = False
        self.rep.save()
        self.assertEqual(self.client.post(endpoint, self.post_data(bill, notes="Allowed retained rep")).status_code, 302)
        delete_bill(bill.pk, user=self.owner, reason="Test")
        self.assertEqual(self.client.get(endpoint).status_code, 404)
        with self.assertRaises(ValidationError):
            edit_bill(bill.pk, self.data(bill, notes="Blocked"), user=self.owner)
        self.firm.is_deleted = True
        self.firm.save()
        self.assertEqual(self.client.get(endpoint).status_code, 404)

    def test_add_firm_on_firms_page_validates_and_creates_active_record(self):
        endpoint = reverse("ledger:add_firm")
        self.assertContains(self.client.get(endpoint), 'id="id_firm-name"')
        self.assertEqual(self.client.post(endpoint, {"firm-name": "New Firm", "firm-source_type": "stockist", "firm-phone": "03001234567"}).status_code, 302)
        firm = Firm.objects.get(name="New Firm")
        self.assertFalse(firm.is_deleted)
        self.assertEqual(firm.current_debt(), 0)
        self.assertEqual(self.client.post(endpoint, {"firm-name": "Invalid", "firm-source_type": "invalid"}).status_code, 200)
        self.assertFalse(Firm.objects.filter(name="Invalid").exists())

    def test_day_month_year_form_display_and_history_filter_compatibility(self):
        bill = self.new_bill()
        self.assertContains(self.client.get(reverse("ledger:edit_bill", args=[bill.pk])), bill.bill_date.strftime("%d-%m-%Y"))
        for day in [bill.bill_date.strftime("%d-%m-%Y"), bill.bill_date.isoformat()]:
            page = self.client.get(reverse("ledger:ledger_history"), {"start_date": day, "end_date": day})
            self.assertEqual(page.status_code, 200)
            self.assertEqual(len(page.context["history_rows"]), 1)
            self.assertContains(page, bill.bill_date.strftime("%d-%m-%Y"))
        data = {"source_type": "distributor", "firm": self.firm.pk, "representative": self.rep.pk,
                "bill_number": "B", "bill_amount": "1", "bill_date": "31-12-2026",
                "request_id": self.transaction_data()["request_id"], "posting_rules_version": "2"}
        form = BillForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["bill_date"], date(2026, 12, 31))
