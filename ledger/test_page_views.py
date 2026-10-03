"""Page-local Trash navigation and compact bilingual header regressions."""
from urllib.parse import parse_qs, urlsplit

from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Bill, Firm, Representative
from .services import reverse_transaction_batch
from .templatetags.bilingual import table_heading
from .tests import LedgerWorkflowTests as Helpers


@override_settings(APP_OWNER_USERNAME="owner", SECURE_SSL_REDIRECT=False,
                   PASSWORD_HASHERS=("django.contrib.auth.hashers.MD5PasswordHasher",))
class PageViewTests(TestCase):
    setUp = Helpers.setUp
    transaction_data = Helpers.transaction_data
    post_transaction = Helpers.post_transaction
    create_batch = Helpers.create_batch

    def test_default_pages_have_forms_and_active_tables_without_trash_tables(self):
        for name in ["add_firm", "add_representative", "bills", "ledger_history"]:
            page = self.client.get(reverse("ledger:" + name))
            self.assertEqual(page.status_code, 200)
            self.assertFalse(page.context["is_trash"])
            self.assertNotContains(page, 'id="page-trash"')
            self.assertIn("view=trash", page.context["trash_link"])
            if name != "ledger_history":
                self.assertContains(page, 'class="form-submit"')
            self.assertContains(page, "portraits.png")

    def test_trash_views_hide_add_forms_and_active_tables(self):
        for name in ["add_firm", "add_representative", "bills", "ledger_history"]:
            page = self.client.get(reverse("ledger:" + name), {"view": "trash"})
            self.assertEqual(page.status_code, 200)
            self.assertTrue(page.context["is_trash"])
            self.assertContains(page, 'id="page-trash"')
            self.assertNotContains(page, 'class="form-submit"')
            self.assertNotContains(page, 'class="data-table history-data-table"')
            self.assertContains(page, "Back to records")

    def test_representative_trash_links_preserve_and_apply_filters(self):
        other = Firm.objects.create(name="Other", source_type="stockist")
        Representative.objects.create(firm=other, name="Wrong source", is_deleted=True)
        self.rep.is_deleted = True
        self.rep.save()
        query = {"view": "trash", "source_type": self.firm.source_type, "firm": self.firm.pk}
        page = self.client.get(reverse("ledger:add_representative"), query)
        self.assertEqual(list(page.context["trashed_representatives"]), [self.rep])
        back = parse_qs(urlsplit(page.context["records_link"]).query)
        self.assertEqual(back, {"source_type": [self.firm.source_type], "firm": [str(self.firm.pk)]})
        self.assertContains(page, page.context["trash_link"].replace("&", "&amp;"))
        recovered = self.client.post(reverse("ledger:restore_representative", args=[self.rep.pk]), {"next": page.context["trash_link"]})
        self.assertRedirects(recovered, page.context["trash_link"])
        self.assertEqual(self.client.get(page.context["trash_link"]).context["trashed_representatives"].count(), 0)

    def test_history_trash_filters_preserve_complete_recovery_group(self):
        root, _ = self.create_batch(bill_choice="add_new", bill_amount="500")
        group = reverse_transaction_batch(root.pk, user=self.owner, reason="Test")["group"]
        query = {"view": "trash", "firm_id": self.firm.pk, "start_date": root.date.strftime("%d-%m-%Y"), "end_date": root.date.isoformat()}
        page = self.client.get(reverse("ledger:ledger_history"), query)
        self.assertEqual([item.pk for item in page.context["transaction_trash_groups"]], [group.pk])
        self.assertNotIn("view", parse_qs(urlsplit(page.context["records_link"]).query))
        self.assertContains(page, page.context["trash_link"].replace("&", "&amp;"))
        recovered = self.client.post(reverse("ledger:recover_trash", args=[group.pk]), {"reason": "Restore", "next": page.context["trash_link"]})
        self.assertRedirects(recovered, page.context["trash_link"])
        self.assertEqual(self.firm.current_debt(), 500)
        self.assertEqual(self.client.get(page.context["trash_link"]).context["transaction_trash_groups"], [])

    def test_invalid_post_from_trash_url_shows_visible_form_errors(self):
        for name, data in [("add_firm", {"firm-name": "", "firm-source_type": "distributor"}),
                           ("add_representative", {"add_rep-name": "", "add_rep-source_type": "distributor"}),
                           ("bills", {"bill-bill_number": "", "bill-source_type": "distributor"})]:
            page = self.client.post(reverse("ledger:" + name) + "?view=trash", data)
            self.assertEqual(page.status_code, 200)
            self.assertFalse(page.context["is_trash"])
            self.assertContains(page, 'class="form-submit"')
            self.assertContains(page, "This field is required.")

    def test_compact_headers_preserve_full_accessible_labels_and_ordinary_form_labels(self):
        for key, english, urdu, full in [("Representative", "Rep", "نمائندہ", "Representative"),
                                         ("Payment Amount", "Paid", "ادائیگی", "Payment Amount"),
                                         ("Source Type", "Source", "ذریعہ", "Source Type"),
                                         ("Actions", "More", "مزید", "Actions")]:
            output = str(table_heading(key))
            self.assertIn(f'aria-label="{full}', output)
            self.assertIn(f'>{english}</span>', output)
            self.assertIn(f'>{urdu}</span>', output)
            self.assertIn('lang="ur" dir="rtl"', output)
        regular = Template("{% load bilingual %}{% bi 'Payment Amount' %}").render(Context())
        self.assertIn(">Payment Amount</span>", regular)
        self.assertNotIn(">Paid</span>", regular)
