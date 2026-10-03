from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class DeletionBackfillTests(TransactionTestCase):
    def test_backfill_preserves_latest_reversal_time_and_accounting(self):
        executor = MigrationExecutor(connection)
        before = [("ledger", "0006_transaction_batches_and_balance_lock")]
        after = [("ledger", "0007_timed_deletion_groups")]
        executor.migrate(before)
        try:
            apps = executor.loader.project_state(before).apps
            Firm = apps.get_model("ledger", "Firm")
            Bill = apps.get_model("ledger", "Bill")
            Batch = apps.get_model("ledger", "TransactionBatch")
            Entry = apps.get_model("ledger", "LedgerEntry")
            firm = Firm.objects.create(name="Legacy", source_type="distributor")
            root = Batch.objects.create(firm=firm, date=timezone.localdate(), status="reversed")
            bill = Bill.objects.create(firm=firm, bill_number="LEGACY", bill_amount=Decimal("500"))
            Entry.objects.create(firm=firm, bill=bill, transaction_batch=root,
                                 entry_type="bill_created", increase=500)
            old = Batch.objects.create(firm=firm, date=timezone.localdate(), kind="reversal", original_batch=root)
            restored = Batch.objects.create(firm=firm, date=timezone.localdate(), kind="restoration", original_batch=root)
            latest = Batch.objects.create(firm=firm, date=timezone.localdate(), kind="reversal", original_batch=root, reason="Latest correction")
            timestamp = timezone.now() - timedelta(days=31)
            Batch.objects.filter(pk=old.pk).update(created_at=timestamp - timedelta(days=2))
            Batch.objects.filter(pk=restored.pk).update(created_at=timestamp - timedelta(days=1))
            Batch.objects.filter(pk=latest.pk).update(created_at=timestamp)
            Entry.objects.create(firm=firm, bill=bill, transaction_batch=latest,
                                 entry_type="reversal", decrease=500)
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            new_apps = executor.loader.project_state(after).apps
            group = new_apps.get_model("ledger", "DeletionGroup").objects.get(firm_id=firm.pk)
            self.assertEqual(group.deleted_at, timestamp)
            self.assertEqual(group.reason, "Latest correction")
            self.assertEqual(new_apps.get_model("ledger", "Bill").objects.get(pk=bill.pk).creation_batch_id, root.pk)
            member = new_apps.get_model("ledger", "DeletionMember").objects.get(group_id=group.pk)
            self.assertEqual(member.reversal_id, latest.pk)
            entries = new_apps.get_model("ledger", "LedgerEntry").objects.filter(firm_id=firm.pk)
            self.assertEqual(sum(entry.increase - entry.decrease for entry in entries), Decimal("0"))
            self.assertEqual(entries.count(), 2)
        finally:
            MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_invoice_migration_preserves_cash_pairs_and_unassigned_payments(self):
        executor = MigrationExecutor(connection)
        before = [("ledger", "0011_immutable_actor_identity")]
        after = [("ledger", "0012_bill_is_disabled_alter_bill_bill_number_and_more")]
        executor.migrate(before)
        try:
            apps = executor.loader.project_state(before).apps
            Firm, Bill, Payment, Batch, Entry = [apps.get_model("ledger", name) for name in ("Firm", "Bill", "Payment", "TransactionBatch", "LedgerEntry")]
            firm = Firm.objects.create(name="Historical Market", source_type="local_market")
            root = Batch.objects.create(firm=firm, date=timezone.localdate())
            bill = Bill.objects.create(firm=firm, bill_number="CASH-LEGACY", bill_amount=500, creation_batch=root)
            paired = Payment.objects.create(firm=firm, bill=bill, amount=500)
            unlinked = Payment.objects.create(firm=firm, amount=10)
            Entry.objects.create(firm=firm, bill=bill, transaction_batch=root, entry_type="bill_created", increase=500)
            Entry.objects.create(firm=firm, payment=paired, transaction_batch=root, entry_type="payment_made", decrease=500)
            Entry.objects.create(firm=firm, payment=unlinked, entry_type="payment_made", decrease=10)
            old = {name: list(apps.get_model("ledger", name).objects.order_by("pk").values()) for name in ("Bill", "Payment", "TransactionBatch", "LedgerEntry")}
            MigrationExecutor(connection).migrate(after)
            new_apps = MigrationExecutor(connection).loader.project_state(after).apps
            for name, rows in old.items():
                fresh = list(new_apps.get_model("ledger", name).objects.order_by("pk").values())
                self.assertEqual([{key: row[key] for key in rows[0]} for row in fresh], rows)
            self.assertFalse(new_apps.get_model("ledger", "Bill").objects.get(pk=bill.pk).is_disabled)
            self.assertIsNone(new_apps.get_model("ledger", "Payment").objects.get(pk=unlinked.pk).bill_id)
            self.assertEqual(new_apps.get_model("ledger", "BillCarryForward").objects.count(), 0)
        finally:
            MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
