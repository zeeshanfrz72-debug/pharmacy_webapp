"""Presentation regressions: bilingual UI must preserve stored values and accounting."""
import re
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.forms import Form, CharField
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse

from .bilingual import formatted, label, plain
from .forms import LedgerTransactionForm
from .models import Firm, Representative, TransactionBatch
from .templatetags.bilingual import bilingual_errors, bilingual_select


@override_settings(APP_OWNER_USERNAME="owner", SECURE_SSL_REDIRECT=False,
                   PASSWORD_HASHERS=("django.contrib.auth.hashers.MD5PasswordHasher",))
class BilingualTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password="test-password")
        self.client.force_login(self.owner)
        self.firm = Firm.objects.create(name="Save & Net Balance فارم", source_type="distributor")
        self.rep = Representative.objects.create(firm=self.firm, name="Delete نمائندہ")

    def visible_html(self, response):
        self.assertEqual(response.status_code, 200)
        return re.sub(r"<script\b[^>]*>.*?</script>", "", response.content.decode(), flags=re.S)

    def test_renderer_direction_glossary_and_safe_interpolated_values(self):
        output = str(label("Net Balance"))
        self.assertIn('lang="en" dir="ltr"', output)
        self.assertIn('lang="ur" dir="rtl"', output)
        self.assertIn("Total Debt", output)
        self.assertIn("کُل قرضہ", output)
        self.assertNotIn("Net Balance", output)
        receipt = str(formatted("receipt", status="Saved", status_ur="محفوظ", firm='<img src=x onerror="bad">', previous="-25.50", remaining="-10.50"))
        self.assertNotIn("<img", receipt)
        self.assertIn("&lt;img", receipt)
        self.assertIn("\u2068-25.50\u2069", receipt)
        self.assertEqual(plain("Recover"), "Recover — بحال کریں")

    def test_native_choices_keep_values_and_database_names_verbatim(self):
        form = LedgerTransactionForm()
        choices_before = [value for value, text in form.fields["source_type"].choices]
        html = str(bilingual_select(form["source_type"]))
        self.assertEqual([value for value, text in form.fields["source_type"].choices], choices_before)
        self.assertIn('value="distributor"', html)
        self.assertIn("Distributor Company — ڈسٹری بیوٹر کمپنی", html)
        rendered = str(bilingual_select(form["firm"]))
        self.assertIn("Save &amp; Net Balance فارم", rendered)
        self.assertNotIn("محفوظ کریں", rendered)
        self.assertIn(f'value="{self.firm.pk}"', rendered)

    def test_error_dict_translates_messages_instead_of_field_keys(self):
        class RequiredForm(Form):
            name = CharField()
        form = RequiredForm({})
        rendered = str(bilingual_errors(form.errors))
        self.assertIn("This field is required.", rendered)
        self.assertIn('lang="ur"', rendered)
        self.assertNotIn(">name<", rendered)
        long_error = str(label("Ensure this value is greater than or equal to 0.01."))
        self.assertIn('lang="ur"', long_error)
        self.assertIn("\u20680.01\u2069", long_error)

    def test_main_pages_and_edit_forms_render_visible_bilingual_labels(self):
        pages = ["add_transaction", "add_firm", "add_representative", "bills", "ledger_history", "trash"]
        for name in pages:
            with self.subTest(page=name):
                html = self.visible_html(self.client.get(reverse("ledger:" + name)))
                self.assertIn('class="bi-en"', html)
                self.assertIn('class="bi-ur"', html)
                self.assertNotIn("Net Balance</", html)
                self.assertNotIn("Current Net Balance</", html)
        for name, item in [("edit_firm", self.firm), ("edit_representative", self.rep)]:
            html = self.visible_html(self.client.get(reverse("ledger:" + name, args=[item.pk])))
            self.assertIn('for="id_name"', html)
            self.assertIn('name="name"', html)
            self.assertIn('class="bi-ur"', html)

    def test_auth_fields_and_validation_are_bilingual(self):
        self.client.logout()
        html = self.visible_html(self.client.get("/accounts/login/"))
        self.assertIn(str(label("Username")), html)
        self.assertIn(str(label("Password")), html)
        self.assertIn(str(label("Sign In")), html)
        rendered = Template("{% load bilingual %}{{ errors|bilingual_errors }}").render(Context({"errors": ["Please enter a correct username and password. Note that both fields may be case-sensitive."]}))
        self.assertIn('lang="ur"', rendered)

    def test_ajax_save_duplicate_delete_recover_keeps_accounting_and_partial_labels(self):
        data = {"request_id": str(uuid.uuid4()), "source_type": "distributor", "firm": self.firm.pk,
                "representative": self.rep.pk, "bill_choice": "add_new", "new_bill_number": "B-اردو-12",
                "new_bill_amount": "1250.50", "payment_choice": "1000", "custom_payment_amount": "", "posting_rules_version": "2"}
        endpoint = reverse("ledger:add_transaction")
        saved = self.client.post(endpoint, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertTrue(saved["success"])
        self.assertEqual(self.firm.current_debt(), Decimal("250.50"))
        self.assertEqual(self.client.post(endpoint, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["status"], "duplicate")
        batch = TransactionBatch.objects.get(pk=saved["batch_id"])
        recent = self.client.get(reverse("ledger:dashboard_recent"))
        self.assertEqual(recent.status_code, 200)
        history = recent.json()["html"]
        self.assertIn("Save &amp; Net Balance فارم", history)
        self.assertIn("Delete نمائندہ", history)
        self.assertIn("B-اردو-12", history)
        self.assertIn(str(label("Corrected chronological balance")), history)
        for operation, expected in [("reverse_batch", "0.00"), ("restore_batch", "250.50")]:
            response = self.client.post(reverse("ledger:" + operation, args=[batch.pk]), {"reason": "Test وجہ"})
            self.assertIn(response.status_code, [200, 302])
            self.assertEqual(self.firm.current_debt(), Decimal(expected))
            if operation == "reverse_batch":
                trash = self.visible_html(self.client.get(reverse("ledger:ledger_history"), {"view": "trash"}))
                self.assertIn(str(label("Recover")), trash)
                self.assertIn("Test وجہ", trash)

    def test_signed_debt_and_supplier_credit_values_are_unchanged(self):
        from .models import LedgerEntry
        LedgerEntry.objects.create(firm=self.firm, entry_type="opening_balance", date="2026-10-02", decrease=Decimal("25.50"))
        page = self.client.get(reverse("ledger:add_transaction"))
        self.assertEqual(page.context["total_debt"], Decimal("-25.50"))
        self.assertEqual(page.context["supplier_credit_total"], Decimal("25.50"))
        html = self.visible_html(page)
        self.assertIn(str(label("Total Debt")), html)
        self.assertNotIn(str(label("Supplier Credits")), html)
        self.assertIn("******", html)  # Debt visibility preference still applies.
