"""Independent invoice/firm Decimal oracle. Only disposable SQLite databases.

python scripts/accounting_bill_sequences.py --seeds 100 --steps 200 --workers 4
Each rejected action must leave financial evidence unchanged. Failure output
includes the seed and action prefix for deterministic reduction/reproduction.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import uuid

import accounting_audit as audit

D = Decimal


def sequence(seed, steps):
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from django.utils import timezone
    from ledger.models import Bill, BillCarryForward, LedgerEntry, Payment, TransactionBatch
    from ledger import services as s
    from ledger.views import _history_rows, _prepare_bill_balances
    firm, rep = audit.account(local=seed % 2 == 0)
    if seed % 4 == 0:
        from ledger.models import Representative
        rep = Representative.objects.create(firm=firm, name="Optional local rep")
    rng, bills, roots, transfers, groups, receipts, counts, trace = random.Random(seed), {}, {}, {}, {}, {}, Counter(), []

    def remaining(bid):
        value = sum((root["effects"].get(bid, D(0)) for root in roots.values() if root["active"]), D(0))
        for t in transfers.values():
            if t["active"]:
                value += t["amount"] * (int(t["destination"] == bid) - int(t["source"] == bid))
        return value

    def total():
        return sum((remaining(bid) for bid in bills), D(0))

    def active(bid):
        return roots[bills[bid]["root"]]["active"]

    def participant(bid):
        return any(t["active"] and bid in (t["source"], t["destination"]) for t in transfers.values())

    def snapshot():
        return {m.__name__: list(m.objects.filter(firm=firm).order_by("pk").values()) for m in (Bill, Payment, TransactionBatch, LedgerEntry)}

    def remember(result, kind, effects, data=None):
        root = result["batch"]
        roots[root.pk] = dict(kind=kind, effects=effects, active=True, data=data)
        receipts[root.pk] = root.posting_receipt
        if kind == "bill":
            b = result["bill"]
            bills[b.pk] = {"root": root.pk, "disabled": False, "amount": next(iter(effects.values()))}

    for step in range(steps):
        operation = rng.choice(["bill"] * 3 + ["payment"] * 4 + ["edit", "transfer", "undo", "reverse", "delete", "recover", "replay"])
        if not bills:
            operation = "bill"
        if (operation == "transfer" and len(bills) < 2) or (operation == "undo" and not transfers) or (operation == "recover" and not groups):
            operation = "bill"
        expected, result, record = True, None, {"operation": operation}
        before = snapshot()
        try:
            if operation == "bill":
                amount = D(rng.randrange(1, 100000)) / 100
                data = dict(request_id=uuid.uuid4(), firm=firm, representative=rep, bill_choice="add_new",
                    new_bill_number="" if seed % 2 == 0 and rng.choice([True, False]) else f"B-{seed}-{step}", new_bill_amount=amount)
                record["amount"] = str(amount)
                result = s.create_transaction_batch(data, user=audit.owner, payload_hash=str(data["request_id"]))
                remember(result, "bill", {result["bill"].pk: amount}, data)
            elif operation == "payment":
                bid = rng.choice(list(bills)); b = Bill.objects.get(pk=bid)
                amount = D(rng.randrange(1, 80000)) / 100
                expected = active(bid) and not bills[bid]["disabled"] and amount <= remaining(bid) and amount <= total()
                record.update(bill=bid, amount=str(amount))
                data = dict(request_id=uuid.uuid4(), firm=firm, representative=rep, bill_choice=str(bid), payment_amount=amount)
                result = s.create_transaction_batch(data, user=audit.owner, payload_hash=str(data["request_id"]))
                remember(result, "payment", {bid: -amount}, data)
            elif operation == "edit":
                bid = rng.choice(list(bills)); b = Bill.objects.get(pk=bid)
                amount = D(rng.randrange(1, 100000)) / 100
                expected = active(bid) and not bills[bid]["disabled"]
                record.update(bill=bid, amount=str(amount))
                data = audit.edit_values(b, bill_amount=amount)
                s.edit_bill(bid, data, user=audit.owner)
                roots[bills[bid]["root"]]["effects"][bid] = amount
                bills[bid]["amount"] = amount
            elif operation == "transfer":
                if len(bills) < 2:
                    counts["skipped"] += 1
                    continue
                src, dst = rng.sample(list(bills), 2)
                a, b = Bill.objects.get(pk=src), Bill.objects.get(pk=dst)
                expected = active(src) and active(dst) and not bills[src]["disabled"] and not bills[dst]["disabled"] and remaining(src) >= 0
                amount = remaining(src)
                record.update(source=src, destination=dst, amount=str(amount))
                result = s.carry_forward_bill(src, destination_id=dst, source_revision=s.bill_revision(a), destination_revision=s.bill_revision(b),
                    request_id=uuid.uuid4(), user=audit.owner, payload_hash=f"transfer-{seed}-{step}")
                t = result["batch"].carry_forward
                transfers[t.pk] = dict(source=src, destination=dst, amount=amount, active=True)
                bills[src]["disabled"] = True
                receipts[result["batch"].pk] = result["batch"].posting_receipt
            elif operation == "undo":
                if not transfers:
                    counts["skipped"] += 1
                    continue
                tid = rng.choice(list(transfers)); t = transfers[tid]
                expected = t["active"] and active(t["source"]) and active(t["destination"]) and not bills[t["destination"]]["disabled"] and remaining(t["destination"]) >= t["amount"]
                record["transfer"] = tid
                result = s.undo_carry_forward(tid, request_id=uuid.uuid4(), user=audit.owner, payload_hash=f"undo-{seed}-{step}")
                t["active"] = False; bills[t["source"]]["disabled"] = False
                receipts[result["batch"].pk] = result["batch"].posting_receipt
            elif operation in ("reverse", "delete"):
                rid = rng.choice(list(roots)); root = roots[rid]; bid = next(iter(root["effects"]))
                if operation == "delete":
                    rid = bills[bid]["root"]; root = roots[rid]
                expected = not root["active"] or (not bills[bid]["disabled"] and (root["kind"] == "payment" or not participant(bid)))
                record.update(root=rid, bill=bid)
                if operation == "delete":
                    expected = not root["active"] or not participant(bid)
                    affected = [pk for pk, r in roots.items() if r["active"] and bid in r["effects"]]
                    result = s.delete_bill(bid, user=audit.owner, reason="Oracle deletion")
                else:
                    affected = [rid]
                    result = s.reverse_transaction_batch(rid, user=audit.owner, reason="Oracle reversal")
                if result["changed"]:
                    for pk in affected:
                        roots[pk]["active"] = False
                    group = result["group"]
                    assert set(group.members.values_list("batch_id", flat=True)) == set(affected)
                    groups[group.pk] = dict(roots=affected, restored=False)
            elif operation == "recover":
                if not groups:
                    counts["skipped"] += 1
                    continue
                gid = rng.choice(list(groups)); group = groups[gid]
                expected = group["restored"] or all(not bills[next(iter(roots[pk]["effects"]))]["disabled"] and
                    (roots[pk]["kind"] == "payment" or not participant(next(iter(roots[pk]["effects"])))) for pk in group["roots"])
                record["group"] = gid
                result = s.recover_deletion_group(gid, user=audit.owner, reason="Oracle recovery")
                if result["changed"]:
                    for pk in group["roots"]:
                        roots[pk]["active"] = True
                    group["restored"] = True
            else:
                rid = rng.choice(list(roots)); data = roots[rid]["data"]
                record["root"] = rid
                result = s.create_transaction_batch(data, user=audit.owner, payload_hash=str(data["request_id"]))
                assert not result["created"] and snapshot() == before
            assert expected, f"Accepted an invalid action: {record}"
            counts[operation + "_accepted"] += 1
        except ValidationError:
            assert not expected, f"Rejected a valid action: {record}"
            assert snapshot() == before, f"Rejected action changed financial records: {record}"
            counts[operation + "_rejected"] += 1
        trace.append(record)
        assert firm.current_debt() == total(), (seed, step, "firm", total(), firm.current_debt())
        for b in _prepare_bill_balances(list(Bill.objects.filter(firm=firm))):
            assert b.remaining_balance == remaining(b.pk), (seed, step, "bill", b.pk, b.remaining_balance, remaining(b.pk))
            assert b.is_disabled == bills[b.pk]["disabled"]
        rows = _history_rows(firm=firm)
        if rows:
            assert rows[0]["remaining_debt"] == total(), (seed, step, "history")
        expected_purchases = sum((sum(r["effects"].values()) for r in roots.values() if r["active"] and r["kind"] == "bill"), D(0))
        expected_payments = -sum((sum(r["effects"].values()) for r in roots.values() if r["active"] and r["kind"] == "payment"), D(0))
        assert sum(r["bill_amount"] for r in rows) == expected_purchases
        assert sum(r["payment_made"] for r in rows) == expected_payments
        assert all(root.posting_receipt == receipts[root.pk] for root in TransactionBatch.objects.filter(pk__in=receipts))
    return {"seed": seed, "operations": sum(v for k, v in counts.items() if k != "skipped"), "counts": counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', type=int, default=100)
    parser.add_argument('--steps', type=int, default=200)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--first-seed', type=int, default=0)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--output', type=Path, default=audit.ROOT / 'docs/accounting-bill-sequences.json')
    args = parser.parse_args(); started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='invoice-oracle-') as directory:
        temp = Path(directory)
        if args.worker:
            audit.setup(temp / 'oracle.sqlite3')
            from django.db import connections, transaction
            results = []
            try:
                for seed in range(args.first_seed, args.first_seed + args.seeds):
                    with transaction.atomic():
                        results.append(sequence(seed, args.steps))
                        transaction.set_rollback(True)
                    print(f'Invoice oracle seed {seed} passed', flush=True)
            finally:
                connections.close_all()
        else:
            segments = [(args.first_seed + i * args.seeds // args.workers, (i + 1) * args.seeds // args.workers - i * args.seeds // args.workers) for i in range(args.workers)]
            def run(segment):
                first, count = segment; output = temp / f'{first}.json'
                subprocess.run([sys.executable, __file__, '--worker', '--first-seed', str(first), '--seeds', str(count), '--steps', str(args.steps), '--output', str(output)], cwd=audit.ROOT, check=True)
                return json.loads(output.read_text())['results']
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                results = [record for group in pool.map(run, segments) for record in group]
    args.output.write_text(json.dumps({'seeds': args.seeds, 'steps_per_seed': args.steps, 'results': results,
        'checked_operations': sum(r['operations'] for r in results), 'seconds': round(time.monotonic() - started, 2)}, indent=2), encoding='utf-8')
    print(f'Passed independent Decimal invoice audit: {args.output}', flush=True)


if __name__ == '__main__':
    main()
