"""Barrier-controlled writes on disposable file SQLite, plus migration rehearsal."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import uuid

import accounting_audit as audit
from accounting_repair_verify import migration_rehearsal


def main():
    records = []
    with tempfile.TemporaryDirectory(prefix='invoice-races-') as directory:
        temp = Path(directory)
        audit.setup(temp / 'race.sqlite3')
        from django.core.exceptions import ValidationError
        from django.db import close_old_connections, connections
        from ledger.models import BillCarryForward
        from ledger import services as s

        def race(actions):
            barrier = threading.Barrier(len(actions))
            def run(action):
                close_old_connections()
                try:
                    barrier.wait(timeout=20)
                    result = action()
                    return 'created' if result.get('created', result.get('changed', False)) else 'duplicate'
                except ValidationError:
                    return 'rejected'
                finally:
                    close_old_connections()
            with ThreadPoolExecutor(max_workers=len(actions)) as pool:
                return list(pool.map(run, actions))

        for iteration in range(5):
            for case in ['payment_payment', 'transfer_payment', 'transfer_transfer', 'transfer_delete', 'undo_payment', 'duplicate_transfer', 'edit_transfer', 'supplier_delete_transfer']:
                firm, rep = audit.account()
                source = audit.create(firm, rep, '100')['bill']
                target = audit.create(firm, rep, '100')['bill']
                source_quote, target_quote = s.bill_revision(source), s.bill_revision(target)
                rid = uuid.uuid4()
                def transfer(destination=target, request_id=None):
                    return s.carry_forward_bill(source.pk, destination_id=destination.pk, source_revision=source_quote,
                        destination_revision=target_quote, request_id=request_id or uuid.uuid4(), user=audit.owner, payload_hash='transfer')
                pay_source = lambda: audit.create(firm, rep, payment='80', bill=source)
                if case == 'payment_payment':
                    actions = [pay_source, pay_source]
                elif case == 'transfer_payment':
                    actions = [transfer, pay_source]
                elif case == 'transfer_transfer':
                    actions = [transfer, transfer]
                elif case == 'duplicate_transfer':
                    actions = [lambda: transfer(request_id=rid), lambda: transfer(request_id=rid)]
                elif case == 'transfer_delete':
                    actions = [transfer, lambda: s.delete_bill(source.pk, user=audit.owner, reason='Concurrent deletion')]
                elif case == 'undo_payment':
                    root = transfer()['batch']
                    actions = [lambda: s.undo_carry_forward(root.carry_forward.pk, user=audit.owner, request_id=uuid.uuid4(), payload_hash='undo'),
                        lambda: audit.create(firm, rep, payment='150', bill=target)]
                elif case == 'edit_transfer':
                    values = audit.edit_values(source, bill_amount=Decimal('200'))
                    def edit():
                        _, changed = s.edit_bill(source.pk, values, user=audit.owner)
                        return {'changed': changed}
                    actions = [transfer, edit]
                else:
                    def archive():
                        entity = audit.Firm.objects.get(pk=firm.pk); entity.is_deleted = True; entity.save()
                        return {'changed': True}
                    actions = [transfer, archive]
                outcomes = race(actions)
                source.refresh_from_db(); target.refresh_from_db()
                totals = audit.sql_balance(firm)
                active = list(BillCarryForward.objects.filter(batch__firm=firm, batch__status='active'))
                assert firm.current_debt() == totals == source.remaining_balance + target.remaining_balance
                assert len(active) <= 1
                assert source.is_disabled == bool(active)
                if case == 'payment_payment':
                    assert outcomes.count('created') == 1 and totals == 120
                elif case == 'duplicate_transfer':
                    assert sorted(outcomes) == ['created', 'duplicate'] and totals == 200
                elif case == 'undo_payment':
                    assert outcomes.count('created') == 1 and totals in (50, 200)
                elif case == 'edit_transfer':
                    assert outcomes.count('created') == 1 and totals in (200, 300)
                elif case == 'supplier_delete_transfer':
                    assert totals == 200 and 'created' in outcomes
                else:
                    assert outcomes.count('created') == 1
                    assert totals == (100 if case == 'transfer_delete' and not active else 120 if case == 'transfer_payment' and not active else 200)
                records.append({'case': case, 'iteration': iteration, 'outcomes': outcomes,
                    'firm_balance': str(totals), 'source_balance': str(source.remaining_balance), 'destination_balance': str(target.remaining_balance)})
        snapshots = sorted(set(audit.ROOT.glob('*.sqlite3')) | set((audit.ROOT / 'backups').glob('*.sqlite3')))
        migrations = {str(p.relative_to(audit.ROOT)): migration_rehearsal(p, temp) for p in snapshots}
        connections.close_all()
    output = audit.ROOT / 'docs/accounting-bill-races.json'
    output.write_text(json.dumps({'database': 'disposable file SQLite', 'races': records, 'migrations': migrations}, indent=2, default=str), encoding='utf-8')
    print(f'Passed {len(records)} barrier-controlled races and snapshot migrations: {output}', flush=True)


if __name__ == '__main__':
    main()
