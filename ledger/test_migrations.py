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
