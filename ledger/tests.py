import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from axes.models import AccessAttempt

from .models import Bill, Firm, LedgerEntry, Payment, Representative, TransactionBatch
from .services import create_transaction_batch


@override_settings(
    APP_OWNER_USERNAME="owner",
    SECURE_SSL_REDIRECT=False,
    SECURE_HSTS_SECONDS=0,
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    PASSWORD_HASHERS=("django.contrib.auth.hashers.MD5PasswordHasher",),
)
class LedgerWorkflowTests(TestCase):
    def setUp(self):
        self.owner_password = uuid.uuid4().hex + "!Aa9"
        self.owner = get_user_model().objects.create_user("owner", password=self.owner_password)
        self.client.force_login(self.owner)
        self.firm = Firm.objects.create(
            name="Test Distributor",
            source_type=Firm.SourceType.DISTRIBUTOR,
        )
        self.rep = Representative.objects.create(firm=self.firm, name="Test Rep")

    def transaction_data(
        self,
        *,
        bill_amount=None,
        payment_amount=None,
        bill_choice="",
        request_id=None,
        firm=None,
        representative=None,
    ):
        firm = firm or self.firm
        data = {
            "request_id": str(request_id or uuid.uuid4()),
            "source_type": firm.source_type,
            "firm": str(firm.pk),
            "representative": str((representative or (None if firm.source_type == Firm.SourceType.LOCAL_MARKET else self.rep)).pk)
            if (representative or (None if firm.source_type == Firm.SourceType.LOCAL_MARKET else self.rep))
            else "",
            "bill_choice": bill_choice,
            "new_bill_number": "B-100" if bill_choice == "add_new" else "",
            "new_bill_amount": str(bill_amount) if bill_amount is not None else "",
            "custom_payment_amount": "",
            "payment_choice": "",
        }
        if payment_amount is not None:
            payment_amount = Decimal(payment_amount)
            common = {Decimal("1000"), Decimal("1500"), Decimal("2000"), Decimal("2500"), Decimal("3000")}
            if payment_amount in common:
                data["payment_choice"] = str(int(payment_amount))
            else:
                data["payment_choice"] = "other"
                data["custom_payment_amount"] = str(payment_amount)
        return data

    def post_transaction(self, data):
        return self.client.post(
            reverse("ledger:add_transaction"),
            data,
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def create_batch(self, **kwargs):
        response = self.post_transaction(self.transaction_data(**kwargs))
        self.assertEqual(response.status_code, 200, response.content[:500])
        payload = response.json()
        self.assertTrue(payload["success"], payload)
        return TransactionBatch.objects.get(pk=payload["batch_id"]), payload

    def test_bill_with_partial_and_full_payments_is_atomic_and_has_one_batch_per_submit(self):
        batch, _ = self.create_batch(bill_choice="add_new", bill_amount="5000", payment_amount="2000")
        self.assertEqual(batch.entries.count(), 2)
        self.assertEqual(self.firm.current_debt(), Decimal("3000.00"))

        bill = batch.entries.get(bill__isnull=False).bill
        second, _ = self.create_batch(bill_choice=str(bill.pk), payment_amount="3000")
        self.assertEqual(second.entries.count(), 1)
        self.assertEqual(self.firm.current_debt(), Decimal("0.00"))
        self.assertEqual(TransactionBatch.objects.filter(kind=TransactionBatch.Kind.TRANSACTION).count(), 2)

    def test_local_market_purchase_creates_paired_bill_and_payment(self):
        market = Firm.objects.create(name="Local Market", source_type=Firm.SourceType.LOCAL_MARKET)
        batch, _ = self.create_batch(firm=market, payment_amount="1500")
        self.assertEqual(batch.entries.count(), 2)
        self.assertEqual(batch.entries.filter(entry_type=LedgerEntry.EntryType.BILL_CREATED).count(), 1)
        self.assertEqual(batch.entries.filter(entry_type=LedgerEntry.EntryType.PAYMENT_MADE).count(), 1)
        self.assertEqual(market.current_debt(), Decimal("0.00"))

    def test_reversal_and_undo_append_offsets_and_restore_dashboard_totals(self):
        batch, _ = self.create_batch(bill_choice="add_new", bill_amount="2500", payment_amount="1000")
        before = LedgerEntry.objects.count()
        response = self.client.post(
            reverse("ledger:reverse_batch", args=[batch.pk]),
            {"reason": "Wrong bill amount"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        batch.refresh_from_db()
        self.assertEqual(batch.status, TransactionBatch.Status.REVERSED)
        self.assertEqual(self.firm.current_debt(), Decimal("0.00"))
        self.assertEqual(LedgerEntry.objects.count(), before + 2)

        dashboard = self.client.get(reverse("ledger:add_transaction"), secure=True)
        self.assertEqual(dashboard.context["today_payment_count"], 0)
        self.assertEqual(dashboard.context["today_payment_total"], Decimal("0.00"))
        chart = self.client.get(reverse("ledger:dashboard_business_trend"), {"period": "month"}, secure=True).json()
        self.assertEqual(sum(chart["amount_values"]), 0)

        response = self.client.post(
            reverse("ledger:restore_batch", args=[batch.pk]),
            {"reason": "Original entry was correct"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        batch.refresh_from_db()
        self.assertEqual(batch.status, TransactionBatch.Status.ACTIVE)
        self.assertEqual(self.firm.current_debt(), Decimal("1500.00"))
        self.assertEqual(batch.audit_events.filter(kind=TransactionBatch.Kind.REVERSAL).count(), 1)
        self.assertEqual(batch.audit_events.filter(kind=TransactionBatch.Kind.RESTORATION).count(), 1)
        dashboard = self.client.get(reverse("ledger:add_transaction"), secure=True)
        self.assertEqual(dashboard.context["today_payment_count"], 1)
        self.assertEqual(dashboard.context["today_payment_total"], Decimal("1000.00"))

    def test_reversing_bill_after_payment_displays_supplier_credit(self):
        bill_batch, _ = self.create_batch(bill_choice="add_new", bill_amount="2000")
        self.create_batch(bill_choice=str(bill_batch.entries.get(bill__isnull=False).bill_id), payment_amount="1000")
        self.client.post(
            reverse("ledger:reverse_batch", args=[bill_batch.pk]),
            {"reason": "Bill was entered in error"},
            secure=True,
        )
        self.assertEqual(self.firm.current_debt(), Decimal("-1000.00"))
        page = self.client.get(reverse("ledger:add_firm"), secure=True)
        self.assertContains(page, "Supplier Credit")
        credit = page.context["firms"].get(pk=self.firm.pk).current_credit
        self.assertEqual(credit, Decimal("1000.00"))
        self.assertContains(page, str(credit))
        breakdown = self.client.get(reverse("ledger:dashboard_debt_breakdown"), secure=True).json()
        self.assertEqual(breakdown["credits"], [{"label": "Test Distributor", "value": 1000.0}])

    def test_archived_firm_keeps_balance_and_is_restorable(self):
        self.create_batch(bill_choice="add_new", bill_amount="1200")
        response = self.client.post(reverse("ledger:archive_firm", args=[self.firm.pk]), secure=True)
        self.assertEqual(response.status_code, 302)
        self.firm.refresh_from_db()
        self.assertTrue(self.firm.is_deleted)
        self.assertEqual(self.firm.current_debt(), Decimal("1200.00"))
        page = self.client.get(reverse("ledger:add_firm"), secure=True)
        self.assertEqual(page.context["total_debt"], Decimal("1200.00"))
        self.assertContains(page, "Archived Firms")
        transaction_form = self.client.get(reverse("ledger:add_transaction"), secure=True)
        self.assertNotIn(self.firm, transaction_form.context["form"].fields["firm"].queryset)

        response = self.client.post(reverse("ledger:restore_firm", args=[self.firm.pk]), secure=True)
        self.assertEqual(response.status_code, 302)
        self.firm.refresh_from_db()
        self.assertFalse(self.firm.is_deleted)

    def test_archived_representative_keeps_historical_link_and_can_be_restored(self):
        batch, _ = self.create_batch(bill_choice="add_new", bill_amount="1000")
        bill = batch.entries.get(bill__isnull=False).bill
        self.client.post(reverse("ledger:archive_representative", args=[self.rep.pk]), secure=True)
        self.rep.refresh_from_db()
        self.assertTrue(self.rep.is_deleted)
        bill.refresh_from_db()
        self.assertEqual(bill.representative_id, self.rep.pk)
        self.assertNotIn(self.rep, self.client.get(reverse("ledger:add_transaction"), secure=True).context["form"].fields["representative"].queryset)
        history = self.client.get(reverse("ledger:ledger_history"), secure=True)
        self.assertContains(history, "Test Rep")

        self.client.post(reverse("ledger:restore_representative", args=[self.rep.pk]), secure=True)
        self.rep.refresh_from_db()
        self.assertFalse(self.rep.is_deleted)

    def test_archive_reverse_and_undo_controls_render_with_mobile_targets(self):
        firm_page = self.client.get(reverse("ledger:add_firm"), secure=True)
        self.assertContains(firm_page, reverse("ledger:archive_firm", args=[self.firm.pk]))
        self.assertContains(firm_page, 'class="archive-button"')

        representative_page = self.client.get(reverse("ledger:add_representative"), secure=True)
        self.assertContains(
            representative_page,
            reverse("ledger:archive_representative", args=[self.rep.pk]),
        )

        batch, _ = self.create_batch(bill_choice="add_new", bill_amount="800")
        history_page = self.client.get(reverse("ledger:ledger_history"), secure=True)
        reverse_url = reverse("ledger:reverse_batch", args=[batch.pk])
        self.assertContains(history_page, reverse_url)
        self.assertContains(history_page, "Correction reason")
        self.assertContains(history_page, ".batch-action-form button { width:100%; }")
        self.assertContains(
            history_page,
            ".entity-actions .archive-button { width: 100%; min-height: 42px; }",
        )

        self.client.post(reverse_url, {"reason": "Mobile control rendering check"}, secure=True)
        undone_page = self.client.get(reverse("ledger:ledger_history"), secure=True)
        self.assertContains(undone_page, reverse("ledger:restore_batch", args=[batch.pk]))
        self.assertContains(undone_page, "Undo")

    def test_guest_login_has_centered_shell_without_ledger_navigation(self):
        response = Client().get(reverse("login"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="app-guest"')
        self.assertContains(response, "Welcome Back")
        self.assertNotContains(response, '<nav class="bottom-nav"')

    def test_firm_and_representative_tables_render_search_and_accessible_sort_controls(self):
        firm_page = self.client.get(reverse("ledger:add_firm"), secure=True)
        self.assertContains(firm_page, 'id="firm-table-search"')
        self.assertContains(firm_page, "Search name, type, or phone")
        self.assertContains(firm_page, 'data-sort-key="balance"')
        self.assertContains(firm_page, 'aria-sort="none"')
        self.assertContains(firm_page, "No firms match your search.")
        self.assertContains(firm_page, "Remaining Debt")
        self.assertContains(firm_page, reverse("ledger:archive_firm", args=[self.firm.pk]))

        representative_page = self.client.get(reverse("ledger:add_representative"), secure=True)
        self.assertContains(representative_page, 'id="representative-table-search"')
        self.assertContains(representative_page, "Search name, firm, phone, or status")
        self.assertContains(representative_page, 'data-sort-key="date"')
        self.assertContains(representative_page, 'aria-sort="none"')
        self.assertContains(representative_page, "No representatives match your search.")
        self.assertContains(representative_page, 'aria-label="Active representatives"')
        self.assertContains(
            representative_page,
            reverse("ledger:toggle_representative_active", args=[self.rep.pk]),
        )

    def test_desktop_shell_and_short_dashboard_rules_are_present(self):
        response = self.client.get(reverse("ledger:add_transaction"), secure=True)
        self.assertContains(response, 'class="app-authenticated"')
        self.assertContains(response, 'aria-label="Main navigation"')
        self.assertContains(response, "@media (min-width: 1100px)")
        self.assertContains(response, "@media (min-width: 1280px) and (min-height: 900px)")
        self.assertContains(response, ".data-table-wrap")

    def test_ajax_added_firms_and_representatives_return_dropdown_cache_data(self):
        firm_response = self.client.post(
            reverse("ledger:ajax_add_firm"),
            {
                "name": "Dynamic Dropdown Firm",
                "source_type": Firm.SourceType.STOCKIST,
                "phone": "",
            },
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(firm_response.status_code, 200, firm_response.content)
        firm_data = firm_response.json()
        self.assertTrue(firm_data["success"])
        self.assertEqual(firm_data["source_type"], Firm.SourceType.STOCKIST)

        representative_response = self.client.post(
            reverse("ledger:ajax_add_representative"),
            {
                "source_type": Firm.SourceType.STOCKIST,
                "firm": firm_data["id"],
                "name": "Dynamic Dropdown Rep",
                "phone": "",
                "deactivate_previous": "no",
            },
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(representative_response.status_code, 200, representative_response.content)
        representative_data = representative_response.json()
        self.assertTrue(representative_data["success"])
        self.assertEqual(representative_data["firm_id"], firm_data["id"])
        self.assertEqual(representative_data["source_type"], Firm.SourceType.STOCKIST)
        self.assertIn(
            {"id": representative_data["id"], "name": "Dynamic Dropdown Rep"},
            representative_data["representatives"],
        )

    def test_transaction_form_updates_entity_caches_without_delayed_reload(self):
        response = self.client.get(reverse("ledger:add_transaction"), secure=True)
        self.assertContains(response, "upsertFirmInCache(data.source_type, data)")
        self.assertContains(response, "replaceRepresentativesInCache(resp.firm_id, resp.representatives)")
        self.assertNotContains(response, "firmSelect.value = data.id;")
        self.assertNotContains(response, "representativeSelect.value = resp.id;")

    def test_same_day_bill_and_payment_render_as_one_history_action(self):
        batch, _ = self.create_batch(bill_choice="add_new", bill_amount="4000", payment_amount="1000")
        response = self.client.get(reverse("ledger:ledger_history"), secure=True)
        rows = response.context["history_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["batch_id"], batch.pk)
        self.assertEqual(len(rows[0]["entries"]), 2)

    def test_repeated_request_id_is_idempotent_and_conflicting_payload_is_rejected(self):
        request_id = uuid.uuid4()
        data = self.transaction_data(bill_choice="add_new", bill_amount="1800", request_id=request_id)
        first = self.post_transaction(data)
        second = self.post_transaction(data)
        self.assertEqual(first.json()["status"], "created")
        self.assertEqual(second.json()["status"], "duplicate")
        self.assertEqual(TransactionBatch.objects.filter(request_id=request_id).count(), 1)
        self.assertEqual(Bill.objects.filter(firm=self.firm).count(), 1)

        changed = dict(data, new_bill_amount="1900")
        conflict = self.post_transaction(changed)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(Bill.objects.filter(firm=self.firm).count(), 1)

    def test_invalid_payment_fails_without_creating_partial_bill_or_batch(self):
        response = self.post_transaction(
            self.transaction_data(bill_choice="add_new", bill_amount="1000", payment_amount="2000")
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TransactionBatch.objects.count(), 0)
        self.assertEqual(Bill.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)

    def test_legacy_soft_deleted_entry_keeps_its_existing_excluded_balance_effect(self):
        LedgerEntry.objects.create(
            firm=self.firm,
            entry_type=LedgerEntry.EntryType.BILL_CREATED,
            increase=Decimal("800.00"),
            is_deleted=True,
        )
        self.assertEqual(self.firm.current_debt(), Decimal("0.00"))
        batch, _ = self.create_batch(bill_choice="add_new", bill_amount="300")
        self.assertEqual(batch.previous_balance, Decimal("0.00"))
        self.assertEqual(self.firm.current_debt(), Decimal("300.00"))

    def test_asia_karachi_business_date_is_used_at_utc_day_boundary(self):
        utc_boundary = datetime(2026, 9, 30, 20, 0, tzinfo=datetime_timezone.utc)
        with timezone.override("Asia/Karachi"), patch("django.utils.timezone.now", return_value=utc_boundary):
            batch, _ = self.create_batch(bill_choice="add_new", bill_amount="100")
            self.assertEqual(batch.date.isoformat(), "2026-10-01")
            self.assertEqual(timezone.localdate().isoformat(), "2026-10-01")

    def test_csrf_is_required_and_signup_is_disabled(self):
        csrf_client = Client(enforce_csrf_checks=True)
        login_page = csrf_client.get(reverse("login"), secure=True)
        csrf_token = csrf_client.cookies["csrftoken"].value
        self.assertEqual(login_page.status_code, 200)
        csrf_client.force_login(self.owner)
        rejected = csrf_client.post(reverse("ledger:archive_firm", args=[self.firm.pk]), secure=True)
        self.assertEqual(rejected.status_code, 403)
        csrf_client.logout()
        accepted_for_auth_check = csrf_client.post(
            reverse("ledger:add_transaction"),
            {"csrfmiddlewaretoken": csrf_token},
            secure=True,
            HTTP_X_CSRFTOKEN=csrf_token,
            HTTP_X_OFFLINE_SYNC="1",
        )
        self.assertEqual(accepted_for_auth_check.status_code, 401)
        self.assertEqual(accepted_for_auth_check.json()["error"], "authentication_required")
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.get("/accounts/signup/", secure=True).status_code, 404)

    def test_non_owner_authenticated_user_is_denied(self):
        other = get_user_model().objects.create_user("other", password=uuid.uuid4().hex)
        client = Client()
        client.force_login(other)
        self.assertEqual(client.get(reverse("ledger:add_transaction"), secure=True).status_code, 403)

    def test_authenticated_html_is_private_and_has_nonce_based_csp(self):
        response = self.client.get(reverse("ledger:add_transaction"), secure=True)
        self.assertIn("no-store", response["Cache-Control"])
        policy = response["Content-Security-Policy"]
        self.assertIn("script-src 'self' 'nonce-", policy)
        self.assertIn('nonce="', response.content.decode("utf-8"))

    def test_five_failed_password_attempts_lock_out_login(self):
        AccessAttempt.objects.all().delete()
        login_url = reverse("login")
        for _ in range(5):
            self.client.post(
                login_url,
                {"username": "owner", "password": uuid.uuid4().hex},
                secure=True,
            )
        locked = self.client.post(
            login_url,
            {"username": "owner", "password": self.owner_password},
            secure=True,
        )
        self.assertEqual(locked.status_code, 429)


@override_settings(
    APP_OWNER_USERNAME="owner",
    SECURE_SSL_REDIRECT=False,
    SECURE_HSTS_SECONDS=0,
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    PASSWORD_HASHERS=("django.contrib.auth.hashers.MD5PasswordHasher",),
)
class ConcurrentBalanceValidationTests(TransactionTestCase):
    reset_sequences = True

    def test_simultaneous_payments_cannot_overpay_a_firm(self):
        owner = get_user_model().objects.create_user("owner", password=uuid.uuid4().hex)
        firm = Firm.objects.create(name="Concurrent Test", source_type=Firm.SourceType.DISTRIBUTOR)
        rep = Representative.objects.create(firm=firm, name="Concurrency Rep")
        LedgerEntry.objects.create(
            firm=firm,
            entry_type=LedgerEntry.EntryType.OPENING_BALANCE,
            increase=Decimal("1000.00"),
        )

        def submit():
            close_old_connections()
            try:
                return create_transaction_batch(
                    {
                        "firm": firm,
                        "representative": rep,
                        "bill_choice": "",
                        "payment_amount": Decimal("800.00"),
                        "request_id": uuid.uuid4(),
                    },
                    user=owner,
                    payload_hash=uuid.uuid4().hex,
                )
            except Exception as exc:
                return exc
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))

        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(failures), 1, results)
        firm.refresh_from_db()
        self.assertEqual(firm.current_debt(), Decimal("200.00"))
        self.assertEqual(Payment.objects.filter(firm=firm).count(), 1)
