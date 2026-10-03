"""File races, lock faults, backup/restore, malformed requests and upgrade rehearsal."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import sys
import tempfile
import threading
import uuid

import accounting_audit as audit
from accounting_repair_verify import command, rows


def main():
    results = {}
    with tempfile.TemporaryDirectory(prefix="ledger-repair-edges-") as directory:
        temp = Path(directory)
        database = temp / "synthetic.sqlite3"
        audit.setup(database)
        from django.db import close_old_connections, connections
        from django.test import Client, override_settings
        from django.core.exceptions import ValidationError
        from ledger.backup import create_snapshot, verify_snapshot

        def relationship_race(kind):
            firm, rep = audit.account()
            other, _ = audit.account()
            barrier = threading.Barrier(2)
            def worker(change):
                close_old_connections()
                try:
                    barrier.wait(timeout=20)
                    if not change:
                        audit.create(firm, rep, "100")
                    elif kind == "supplier_type":
                        entity = audit.Firm.objects.get(pk=firm.pk); entity.source_type = "local_market"; entity.save()
                    else:
                        entity = audit.Representative.objects.get(pk=rep.pk); entity.firm = other; entity.save()
                    return "accepted"
                except ValidationError:
                    return "rejected"
                finally:
                    close_old_connections()
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(worker, [False, True]))
            firm.refresh_from_db(); rep.refresh_from_db()
            posted = audit.Bill.objects.filter(firm=firm).count()
            assert outcomes.count("accepted") == 1, outcomes
            if posted:
                assert firm.source_type == "distributor" and rep.firm_id == firm.pk
            return {"outcomes": outcomes, "posted_bills": posted, "financial_relationships_preserved": True}
        for kind in ("supplier_type", "representative_transfer"):
            results[kind] = [relationship_race(kind) for _ in range(5)]

        firm, rep = audit.account()
        original = audit.create(firm, rep, "100", "10")
        data = audit.data(firm, rep, payment="10")
        before = audit.state(firm)
        connections.close_all()
        connections["default"].settings_dict["OPTIONS"]["timeout"] = 0.1
        with closing(sqlite3.connect(database)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            try:
                audit.services.create_transaction_batch(data, user=audit.owner, payload_hash="lock-retry")
                raise AssertionError("Write unexpectedly succeeded under a held writer lock")
            except __import__('django').db.OperationalError as exc:
                results["writer_lock"] = {"rejected": str(exc), "records_unchanged": audit.state(firm) == before}
                assert results["writer_lock"]["records_unchanged"]
            finally:
                blocker.rollback()
        audit.services.create_transaction_batch(data, user=audit.owner, payload_hash="lock-retry")
        assert audit.sql_balance(firm) == 80
        results["writer_lock"]["retry_balance"] = "80.00"
        connections.close_all()
        connections["default"].settings_dict["OPTIONS"]["timeout"] = 20

        secure_client = Client(enforce_csrf_checks=True); secure_client.force_login(audit.owner)
        response = secure_client.post("/ledger/add-transaction/", audit.http_data(firm, rep, "10"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        assert response.status_code == 403
        results["csrf"] = {"status": response.status_code, "balance_unchanged": audit.sql_balance(firm) == 80}
        for name, value in (("malformed", "NaN"), ("oversized", "x" * 3000000)):
            values = audit.http_data(firm, rep, "10")
            values["new_bill_amount" if name == "malformed" else "extra"] = value
            with override_settings(DEBUG=False):
                response = audit.post(values)
            assert response.status_code == 400
            results[name] = {"status": response.status_code, "balance_unchanged": audit.sql_balance(firm) == 80}

        # Backup during an uncommitted WAL write must contain one consistent version.
        connections.close_all()
        with closing(sqlite3.connect(database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE ledger_firm SET notes='new version' WHERE id=?", [firm.pk])
            writer.execute("UPDATE ledger_bill SET notes='new version' WHERE id=?", [original["bill"].pk])
            backup = temp / "consistent-backup.sqlite3"
            details = create_snapshot(backup)
            with closing(sqlite3.connect(backup)) as read:
                before_notes = [read.execute("SELECT notes FROM ledger_firm WHERE id=?", [firm.pk]).fetchone()[0], read.execute("SELECT notes FROM ledger_bill WHERE id=?", [original["bill"].pk]).fetchone()[0]]
            assert before_notes == ["", ""], before_notes
            writer.commit()
            # Keep the writer open: committed changes still live in the WAL.
            with closing(sqlite3.connect(database.as_uri() + "?immutable=1", uri=True)) as main_file:
                main_only_notes = main_file.execute("SELECT notes FROM ledger_firm WHERE id=?", [firm.pk]).fetchone()[0]
            assert main_only_notes == "", main_only_notes
            later = temp / "later.sqlite3"; create_snapshot(later)
        with closing(sqlite3.connect(later)) as read:
            after_notes = [read.execute("SELECT notes FROM ledger_firm WHERE id=?", [firm.pk]).fetchone()[0], read.execute("SELECT notes FROM ledger_bill WHERE id=?", [original["bill"].pk]).fetchone()[0]]
        assert after_notes == ["new version", "new version"]
        results["wal_backup"] = {"verification": details, "before_commit": before_notes, "after_commit": after_notes, "raw_main_file_misses_committed_wal": main_only_notes == ""}
        cli_backup = temp / "command-backup.sqlite3"
        results["backup_command"] = command(["manage.py", "backup_ledger", "--output", str(cli_backup)], database)
        assert results["backup_command"]["exit_code"] == 0
        verify_snapshot(cli_backup)
        results["restore_application"] = command(["manage.py", "check"], backup)
        assert results["restore_application"]["exit_code"] == 0
        ledger_read = command(["manage.py", "shell", "-c", "from ledger.models import Firm; print([(f.pk, str(f.current_debt())) for f in Firm.objects.all()])"], backup)
        assert ledger_read["exit_code"] == 0
        results["restored_balances"] = ledger_read

        legacy = temp / "legacy-upgrade.sqlite3"
        with closing(sqlite3.connect(backup)) as src:
            with closing(sqlite3.connect(legacy)) as dst:
                src.backup(dst)
        assert command(["manage.py", "migrate", "ledger", "0007", "--noinput"], legacy)["exit_code"] == 0
        with closing(sqlite3.connect(legacy)) as bad:
            bad.execute("UPDATE ledger_bill SET bill_amount=0.001 WHERE id=?", [original["bill"].pk]); bad.commit()
        evidence_before = rows(legacy)
        failed = command(["manage.py", "migrate", "--noinput"], legacy)
        assert failed["exit_code"] != 0 and "Review invalid legacy values" in failed["stderr"]
        assert rows(legacy) == evidence_before
        with closing(sqlite3.connect(legacy)) as repair:
            repair.execute("UPDATE ledger_bill SET bill_amount=100 WHERE id=?", [original["bill"].pk]); repair.commit()
        retry = command(["manage.py", "migrate", "--noinput"], legacy)
        assert retry["exit_code"] == 0
        results["migration_failure"] = {"clear_failure": True, "evidence_unchanged": True, "retry_succeeded": True, "failure": failed["stderr"][-1600:]}
        connections.close_all()
    output = audit.ROOT / "docs/accounting-repair-edges.json"
    output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Verified wider edge cases: {output}", flush=True)


if __name__ == "__main__":
    main()
