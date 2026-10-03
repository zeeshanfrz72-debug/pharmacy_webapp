"""Deletion groups, Trash recovery, and bill entry regressions."""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import BillForm, LedgerTransactionForm
from .models import Bill, DeletionGroup, Firm, LedgerEntry, Payment, Representative, TransactionBatch
from .services import (create_transaction_batch, delete_bill,
                       recover_deletion_group, reverse_transaction_batch, restore_transaction_batch)
from .tests import LedgerWorkflowTests as WorkflowHelpers


@override_settings(APP_OWNER_USERNAME="owner", SECURE_SSL_REDIRECT=False,
                   PASSWORD_HASHERS=("django.contrib.auth.hashers.MD5PasswordHasher",), SESSION_COOKIE_AGE=90 * 86400)
class TrashRecoveryTests(TestCase):
    setUp = WorkflowHelpers.setUp
    transaction_data = WorkflowHelpers.transaction_data
    post_transaction = WorkflowHelpers.post_transaction
    create_batch = WorkflowHelpers.create_batch

    def bill_data(self, **changes):
        data = {"posting_rules_version": "2", "request_id": str(uuid.uuid4()), "source_type": self.firm.source_type,
                "firm": self.firm.pk, "representative": self.rep.pk,
                "bill_number": "DEDICATED-1", "bill_date": "2026-09-15",
                "bill_amount": "1000.50", "notes": "Delivery note"}
        data.update(changes)
        return data

    def bill_post(self, data):
        return self.client.post(reverse("ledger:bills"), {"bill-" + key: value for key, value in data.items()})

    def act(self, operation, item):
        return operation(item.pk, user=self.owner, reason="Correct entry")

    def test_company_bill_entry_and_duplicate_payloads(self):
        data = self.bill_data()
        self.assertEqual(self.bill_post(data).status_code, 302)
        bill = Bill.objects.get(bill_number="DEDICATED-1")
        self.assertEqual(bill.bill_amount, Decimal("1000.50"))
        self.assertEqual(bill.notes, "Delivery note")
        self.assertEqual(str(bill.bill_date), "2026-09-15")
        self.assertIsNotNone(bill.creation_batch_id)
        self.assertEqual(self.firm.current_debt(), Decimal("1000.50"))
        self.assertEqual(self.bill_post(data).status_code, 302)
        self.assertEqual(Bill.objects.count(), 1)
        data["bill_amount"] = "2000"
        self.assertEqual(self.bill_post(data).status_code, 200)
        self.assertEqual(Bill.objects.count(), 1)
        page = self.client.get(reverse("ledger:bills"))
        self.assertContains(page, "1000.50")
        self.assertContains(page, "Affected transactions:")

    def test_bill_relationships_and_positive_amounts(self):
        other = Firm.objects.create(name="Other", source_type=Firm.SourceType.STOCKIST)
        other_rep = Representative.objects.create(firm=other, name="Other rep")
        inactive = Representative.objects.create(firm=self.firm, name="Inactive", is_active=False)
        for changes in ({"source_type": ""}, {"firm": ""}, {"representative": ""},
                        {"source_type": other.source_type}, {"representative": other_rep.pk},
                        {"representative": inactive.pk}, {"bill_amount": "0"},
                        {"bill_amount": "-1"}, {"bill_amount": "bad"}):
            with self.subTest(changes=changes):
                self.assertFalse(BillForm(self.bill_data(**changes)).is_valid())
        self.rep.is_deleted = True
        self.rep.save()
        self.assertFalse(BillForm(self.bill_data()).is_valid())
        self.rep.is_deleted = False
        self.rep.save()
        self.firm.is_deleted = True
        self.firm.save()
        self.assertFalse(BillForm(self.bill_data()).is_valid())

    def test_local_market_bill_starts_unpaid_and_has_optional_representative(self):
        market = Firm.objects.create(name="Market", source_type=Firm.SourceType.LOCAL_MARKET)
        data = self.bill_data(firm=market.pk, source_type=market.source_type, representative="")
        self.assertTrue(BillForm(data).is_valid())
        self.assertEqual(self.bill_post(data).status_code, 302)
        bill = Bill.objects.get(firm=market)
        self.assertFalse(Payment.objects.filter(bill=bill).exists())
        self.assertIsNone(bill.representative_id)
        self.assertEqual(market.current_debt(), bill.bill_amount)
        data["representative"] = self.rep.pk
        self.assertFalse(BillForm(data).is_valid())

    def test_representative_form_is_separate_and_new_reps_are_active(self):
        self.rep.is_active = False
        self.rep.save()
        page = self.client.get(reverse("ledger:add_representative"))
        self.assertContains(page, 'id="id_add_rep-source_type"')
        self.assertContains(page, 'id="source_type"')
        data = {"add_rep-source_type": self.firm.source_type, "add_rep-firm": self.firm.pk,
                "add_rep-name": "New representative", "add_rep-phone": "0300000000"}
        self.assertEqual(self.client.post(reverse("ledger:add_representative"), data).status_code, 302)
        self.assertTrue(Representative.objects.get(name="New representative").is_active)
        self.rep.refresh_from_db()
        self.assertFalse(self.rep.is_active)
        data["add_rep-source_type"] = Firm.SourceType.STOCKIST
        data["add_rep-name"] = "Invalid"
        self.assertEqual(self.client.post(reverse("ledger:add_representative"), data).status_code, 200)
        self.assertFalse(Representative.objects.filter(name="Invalid").exists())

    def test_bill_deletion_groups_active_linked_payments_and_preserves_earlier_deletion(self):
        creation, _ = self.create_batch(bill_choice="add_new", bill_amount="1000", payment_amount="100")
        bill = Bill.objects.get(creation_batch=creation)
        earlier, _ = self.create_batch(bill_choice=str(bill.pk), payment_amount="100")
        first, _ = self.create_batch(bill_choice=str(bill.pk), payment_amount="200")
        second, _ = self.create_batch(bill_choice=str(bill.pk), payment_amount="300")
        independent = self.act(reverse_transaction_batch, earlier)["group"]
        self.assertEqual(self.firm.current_debt(), Decimal("400"))
        group = self.act(delete_bill, bill)["group"]
        self.assertEqual(set(group.members.values_list("batch_id", flat=True)), {creation.pk, first.pk, second.pk})
        self.assertEqual(self.firm.current_debt(), Decimal("0"))
        self.assertFalse(Bill.objects.available().filter(pk=bill.pk).exists())
        history = self.client.get(reverse("ledger:ledger_history"), secure=True)
        self.assertEqual(history.context["history_rows"], [])
        self.assertEqual(
            {item.pk for item in history.context["transaction_trash_groups"]},
            {independent.pk, group.pk},
        )
        self.assertEqual(self.act(delete_bill, bill)["changed"], False)
        self.assertEqual(self.act(reverse_transaction_batch, first)["group"].pk, group.pk)
        self.assertEqual(DeletionGroup.objects.count(), 2)
        # Undo on any member restores all members, but not the independent deletion.
        self.act(restore_transaction_batch, second)
        self.assertEqual(self.firm.current_debt(), Decimal("400"))
        self.assertEqual(TransactionBatch.objects.get(pk=earlier.pk).deletion_group_id, independent.pk)
        self.assertTrue(Bill.objects.available().filter(pk=bill.pk).exists())
        self.assertFalse(self.act(recover_deletion_group, group)["changed"])
        next_group = self.act(delete_bill, bill)["group"]
        self.assertNotEqual(next_group.pk, group.pk)
        self.assertFalse(self.act(recover_deletion_group, group)["changed"])
        self.assertEqual(self.firm.current_debt(), Decimal("0"))
        self.act(recover_deletion_group, next_group)
        self.assertEqual(self.firm.current_debt(), Decimal("400"))

    def test_old_and_latest_transaction_delete_preserve_later_activity(self):
        first, _ = self.create_batch(bill_choice="add_new", bill_amount="500")
        later, _ = self.create_batch(bill_choice="add_new", bill_amount="800")
        self.act(reverse_transaction_batch, first)
        self.assertEqual(self.firm.current_debt(), Decimal("800"))
        self.act(restore_transaction_batch, first)
        self.act(reverse_transaction_batch, later)
        self.assertEqual(self.firm.current_debt(), Decimal("500"))
        self.act(restore_transaction_batch, later)
        self.assertEqual(self.firm.current_debt(), Decimal("1300"))

    def test_deleted_transactions_move_to_trash_and_recover_at_any_time(self):
        root, _ = self.create_batch(bill_choice="add_new", bill_amount="500")
        group = self.act(reverse_transaction_batch, root)["group"]
        original_time = group.deleted_at
        self.act(reverse_transaction_batch, root)
        group.refresh_from_db()
        self.assertEqual(group.deleted_at, original_time)
        page = self.client.get(reverse("ledger:ledger_history"))
        self.assertEqual(page.context["history_rows"], [])
        self.assertEqual(len(page.context["transaction_trash_groups"]), 1)
        self.assertContains(page, "Recover")
        self.assertNotContains(page, 'class="deleted-transaction"')
        trash_page = self.client.get(reverse("ledger:trash"), secure=True)
        self.assertEqual(trash_page.status_code, 200, trash_page.content[:300])
        self.assertEqual(len(trash_page.context["groups"]), 1)
        bill_page = self.client.get(reverse("ledger:bills"), secure=True)
        self.assertEqual(len(bill_page.context["bill_trash_groups"]), 1)
        with patch("django.utils.timezone.now", return_value=group.deleted_at + timedelta(days=365)):
            self.client.force_login(self.owner)
            trash_page = self.client.get(reverse("ledger:trash"), secure=True)
            self.assertEqual(trash_page.status_code, 200, trash_page.content[:300])
            self.assertEqual(len(trash_page.context["groups"]), 1)
            response = self.client.post(
                reverse("ledger:recover_trash", args=[group.pk]),
                {"reason": "Keep this transaction"}, secure=True,
            )
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.firm.current_debt(), Decimal("500"))

    def test_deleted_transaction_is_hidden_from_main_history_but_kept_in_trash(self):
        root, _ = self.create_batch(bill_choice="add_new", bill_amount="500")
        group = self.act(reverse_transaction_batch, root)["group"]
        count = LedgerEntry.objects.count()
        page = self.client.get(reverse("ledger:ledger_history"), {"ajax": 1})
        self.assertNotContains(page, 'data-batch-id="%s"' % root.pk)
        self.assertContains(self.client.get(reverse("ledger:trash")), f"#{root.pk}")
        self.assertEqual(LedgerEntry.objects.count(), count)
        self.assertEqual(self.firm.current_debt(), Decimal("0"))

    def test_deleted_bill_is_removed_from_dropdowns_and_queued_payload_rejected(self):
        root, _ = self.create_batch(bill_choice="add_new", bill_amount="500")
        bill = Bill.objects.get(creation_batch=root)
        data = self.transaction_data(bill_choice=str(bill.pk), payment_amount="100")
        form = LedgerTransactionForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.act(delete_bill, bill)
        endpoint = self.client.get(reverse("ledger:representatives_for_firm"), {"firm_id": self.firm.pk})
        self.assertEqual(endpoint.json()["bills"], [])
        self.assertNotContains(self.client.get(reverse("ledger:add_transaction")), '"bill_number": "B-100"')
        self.assertNotContains(self.client.get(reverse("ledger:bills")), '<strong>B-100</strong>')
        self.assertFalse(LedgerTransactionForm(data).is_valid())
        with self.assertRaises(ValidationError):
            create_transaction_batch(form.cleaned_data, user=self.owner, payload_hash="queued")
        self.assertEqual(Payment.objects.count(), 0)

    def test_trashed_firms_allow_historical_corrections(self):
        root, _ = self.create_batch(bill_choice="add_new", bill_amount="500")
        bill = Bill.objects.get(creation_batch=root)
        self.firm.is_deleted = True
        self.firm.save()
        group = self.act(delete_bill, bill)["group"]
        self.assertEqual(self.firm.current_debt(), Decimal("0"))
        self.act(recover_deletion_group, group)
        self.assertEqual(self.firm.current_debt(), Decimal("500"))
        self.assertFalse(BillForm(self.bill_data()).is_valid())

    def test_group_deletion_is_atomic_on_offset_failure(self):
        root, _ = self.create_batch(bill_choice="add_new", bill_amount="500")
        bill = Bill.objects.get(creation_batch=root)
        self.create_batch(bill_choice=str(bill.pk), payment_amount="100")
        from .services import _write_batch_offsets
        calls = 0
        def fail_second(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValidationError("Injected failure")
            return _write_batch_offsets(**kwargs)
        with patch("ledger.services._write_batch_offsets", side_effect=fail_second):
            with self.assertRaises(ValidationError):
                self.act(delete_bill, bill)
        self.assertEqual(DeletionGroup.objects.count(), 0)
        self.assertEqual(self.firm.current_debt(), Decimal("400"))
        self.assertFalse(TransactionBatch.objects.filter(status="reversed").exists())

    def test_authentication_post_only_csrf_and_reason(self):
        root, _ = self.create_batch(bill_choice="add_new", bill_amount="500")
        bill = Bill.objects.get(creation_batch=root)
        group = self.act(reverse_transaction_batch, root)["group"]
        routes = [reverse("ledger:bills"), reverse("ledger:trash"),
                  reverse("ledger:delete_bill", args=[bill.pk]), reverse("ledger:recover_trash", args=[group.pk])]
        anonymous = Client()
        for url in routes:
            self.assertEqual(anonymous.get(url).status_code, 302)
        for url in routes[2:]:
            self.assertEqual(self.client.get(url).status_code, 405)
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.owner)
        for url in [routes[0], *routes[2:]]:
            self.assertEqual(secure.post(url, {"reason": "Correct"}).status_code, 403)
        for reason in ("", "   ", "a" * 501):
            response = self.client.post(routes[3], {"reason": reason}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.firm.current_debt(), Decimal("0"))
