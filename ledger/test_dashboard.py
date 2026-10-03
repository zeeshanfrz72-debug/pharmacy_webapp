import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .insights import record_review
from .models import Bill, Firm, LedgerEntry, Representative, TransactionBatch
from .services import create_transaction_batch, delete_bill, reverse_transaction_batch, restore_transaction_batch
from .views import _active_activity_entries


@override_settings(APP_OWNER_USERNAME="owner", SECURE_SSL_REDIRECT=False,
                   PASSWORD_HASHERS=("django.contrib.auth.hashers.MD5PasswordHasher",))
class DashboardTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password="test-password")
        self.client.force_login(self.owner)
        self.firm = Firm.objects.create(name="Company", source_type=Firm.SourceType.DISTRIBUTOR)
        self.rep = Representative.objects.create(firm=self.firm, name="Representative")
        self.today = date(2026, 10, 2)

    def batch(self, amount="100", payment=None, day=None, number=None):
        data = {
            "request_id": uuid.uuid4(), "firm": self.firm, "representative": self.rep,
            "source_type": self.firm.source_type, "bill_choice": "add_new",
            "new_bill_number": number or str(uuid.uuid4()), "new_bill_amount": Decimal(amount),
            "payment_amount": Decimal(payment) if payment is not None else None,
            "bill_date": day or self.today,
        }
        return create_transaction_batch(data, user=self.owner, payload_hash=str(uuid.uuid4()))

    def details(self, **query):
        with patch("ledger.views.timezone.localdate", return_value=self.today):
            return self.client.get(reverse("ledger:firm_details", args=[self.firm.pk]), query)

    def review(self):
        return record_review(_active_activity_entries(), self.today)

    def test_firm_period_counts_grouped_activity_and_calendar_boundaries(self):
        for day in (self.today, date(2026, 10, 1), date(2026, 9, 28), date(2026, 9, 27), date(2026, 10, 3)):
            self.batch(payment="10", day=day)
        opening = TransactionBatch.objects.create(firm=self.firm, date=self.today)
        LedgerEntry.objects.create(firm=self.firm, transaction_batch=opening, entry_type="opening_balance", date=self.today, increase=10)
        page = self.details()
        self.assertEqual(page.context["counts"], {"today": 1, "week": 3, "month": 2})
        self.assertContains(page, "firm_id=" + str(self.firm.pk))
        self.assertContains(page, "start_date=2026-09-28")

    def test_firm_deletion_preview_includes_linked_payment_batches(self):
        first = self.batch(amount="500", payment="100")
        payment = create_transaction_batch({
            "request_id": uuid.uuid4(), "firm": self.firm, "representative": self.rep,
            "bill_choice": str(first["bill"].pk), "payment_amount": Decimal("50"), "bill_date": self.today,
        }, user=self.owner, payload_hash="second")
        bill = self.details().context["bills_page"].object_list[0]
        self.assertEqual(bill.delete_count, 2)
        self.assertEqual(bill.delete_effect, Decimal("-350"))
        self.assertEqual(bill.delete_ids, f'{first["batch"].pk}, {payment["batch"].pk}')
        delete_bill(bill.pk, user=self.owner, reason="Test deletion")
        self.assertEqual(self.details().context["bills_page"].paginator.count, 0)
        self.assertEqual(self.details().context["counts"]["today"], 0)
        restore_transaction_batch(first["batch"].pk, user=self.owner, reason="Restore")
        self.assertEqual(self.details().context["counts"]["today"], 2)

    def test_pagination_and_queries_do_not_grow_per_bill(self):
        self.batch()
        with CaptureQueriesContext(connection) as queries:
            self.details()
        small = len(queries)
        for index in range(24):
            self.batch(number=f"B-{index}")
        with CaptureQueriesContext(connection) as queries:
            page = self.details()
        self.assertEqual(len(queries), small)
        self.assertEqual(len(page.context["bills_page"].object_list), 20)
        self.assertEqual(len(self.details(page=2).context["bills_page"].object_list), 5)
        self.assertEqual(self.details(page="bad").context["bills_page"].number, 1)

    def test_contextual_history_filters_preserve_balance_before_range(self):
        self.batch(amount="100", day=self.today - timedelta(days=1))
        second = self.batch(amount="200")
        other = Firm.objects.create(name="Other", source_type=Firm.SourceType.LOCAL_MARKET)
        create_transaction_batch({"request_id": uuid.uuid4(), "firm": other, "bill_choice": "add_new", "new_bill_number": "", "new_bill_amount": Decimal("999"), "payment_amount": Decimal("999"), "bill_date": self.today}, user=self.owner, payload_hash="other")
        page = self.client.get(reverse("ledger:ledger_history"), {"firm_id": self.firm.pk, "start_date": self.today.isoformat()})
        self.assertEqual(len(page.context["history_rows"]), 1)
        self.assertEqual(page.context["history_rows"][0]["remaining_debt"], Decimal("300"))
        self.assertContains(page, f'id="batch-{second["batch"].pk}"')
        self.assertContains(page, 'name="firm_id"')
        self.assertEqual(self.client.get(reverse("ledger:ledger_history"), {"firm_id": "invalid"}).status_code, 400)

    def test_recent_ten_refresh_balances_and_hidden_analytics(self):
        batches = [self.batch(payment="10") for _ in range(12)]
        page = self.client.get(reverse("ledger:add_transaction"))
        self.assertEqual(len(page.context["history_rows"]), 10)
        self.assertContains(page, 'id="dashboard-analytics"')
        self.assertNotContains(page, 'id="dashboard-analytics" open')
        reverse_transaction_batch(batches[-1]["batch"].pk, user=self.owner, reason="Delete")
        result = self.client.get(reverse("ledger:dashboard_recent")).json()
        self.assertNotIn(f'data-batch-id="{batches[-1]["batch"].pk}"', result["html"])
        self.assertEqual(Decimal(result["total_debt"]), Decimal("990"))
        restore_transaction_batch(batches[-1]["batch"].pk, user=self.owner, reason="Restore")
        result = self.client.get(reverse("ledger:dashboard_recent")).json()
        self.assertEqual(Decimal(result["total_debt"]), Decimal("1080"))

    def test_duplicates_normalize_reference_but_require_all_fields(self):
        first = self.batch(number=" Inv-7 ")
        self.batch(number="inv-7")
        self.batch(number="INV-7", amount="101")
        self.batch(number="INV-7", day=self.today - timedelta(days=1))
        self.assertEqual(self.review()["duplicate_bill_count"], 2)
        reverse_transaction_batch(first["batch"].pk, user=self.owner, reason="Delete")
        self.assertEqual(self.review()["duplicate_bill_count"], 0)
        restore_transaction_batch(first["batch"].pk, user=self.owner, reason="Restore")
        self.assertEqual(self.review()["duplicate_bill_count"], 2)

    def test_recent_balances_include_aggregated_older_activity(self):
        for days in range(12, 0, -1):
            self.batch(amount="100", payment="10", day=self.today - timedelta(days=days))
        page = self.client.get(reverse("ledger:add_transaction"))
        rows = page.context["history_rows"]
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["remaining_debt"], Decimal("1080"))
        self.assertEqual(rows[-1]["remaining_debt"], Decimal("270"))

    def test_payment_and_bill_baselines_are_separate(self):
        for days in range(30, 40):
            self.batch(amount="100", payment="5", day=self.today - timedelta(days=days))
        large = self.batch(amount="100", payment="16")
        result = self.review()
        self.assertEqual(len(result["outliers"]), 1)
        row = result["outliers"][0]
        self.assertEqual(row["entry"].transaction_batch_id, large["batch"].pk)
        self.assertEqual(row["entry"].entry_type, "payment_made")
        self.assertEqual(row["median"], Decimal("5"))

    def test_outliers_strict_threshold_minimum_history_and_date_bounds(self):
        for days in range(30, 40):
            self.batch(amount="2000", day=self.today - timedelta(days=days))
        self.batch(amount="6000")
        large = self.batch(amount="6000.01")
        self.batch(amount="50000", day=self.today + timedelta(days=1))
        result = self.review()
        self.assertEqual(len(result["outliers"]), 1)
        self.assertEqual(result["outliers"][0]["entry"].transaction_batch_id, large["batch"].pk)
        self.assertEqual(result["outliers"][0]["median"], Decimal("2000"))
        self.assertEqual(result["outliers"][0]["sample_size"], 10)
        old = TransactionBatch.objects.filter(date=self.today - timedelta(days=39)).first()
        reverse_transaction_batch(old.pk, user=self.owner, reason="Reduce baseline")
        self.assertEqual(self.review()["outliers"], [])
        self.assertEqual(self.review()["insufficient_count"], 2)

    def test_audit_cycles_count_events_and_distinct_transactions(self):
        batch = self.batch()["batch"]
        reverse_transaction_batch(batch.pk, user=self.owner, reason="First deletion")
        restore_transaction_batch(batch.pk, user=self.owner, reason="Recovery")
        reverse_transaction_batch(batch.pk, user=self.owner, reason="Second deletion")
        batch.audit_events.update(created_at=timezone.make_aware(datetime.combine(self.today, time(12))))
        result = self.review()
        self.assertEqual((result["deleted_event_count"], result["recovered_event_count"], result["affected_count"]), (2, 1, 1))
        boundary = timezone.make_aware(datetime.combine(self.today - timedelta(days=29), time.min))
        event = batch.audit_events.order_by("id").first()
        TransactionBatch.objects.filter(pk=event.pk).update(created_at=boundary - timedelta(microseconds=1))
        self.assertEqual(self.review()["deleted_event_count"], 1)
        TransactionBatch.objects.filter(pk=event.pk).update(created_at=boundary)
        self.assertEqual(self.review()["deleted_event_count"], 2)

    def test_review_queries_empty_states_and_authenticated_get_fallbacks(self):
        for route, args in (("firm_details", [self.firm.pk]), ("dashboard_review", []), ("dashboard_recent", [])):
            url = reverse("ledger:" + route, args=args)
            self.assertIn(Client().get(url).status_code, (302, 401))
            self.assertEqual(self.client.post(url).status_code, 405)
            self.assertEqual(self.client.get(url).status_code, 200)
        with CaptureQueriesContext(connection) as queries:
            self.review()
        self.assertEqual(len(queries), 3)
        self.batch()
        with CaptureQueriesContext(connection) as queries:
            self.review()
        self.assertEqual(len(queries), 3)
        ajax = self.client.get(reverse("ledger:dashboard_review"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertNotContains(ajax, "<html")
        self.assertContains(ajax, "Insufficient history")
