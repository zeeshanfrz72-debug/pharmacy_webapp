"""Disposable repair verification. Retains the original audit as historical evidence.

python scripts/accounting_repair_verify.py --seeds 100 --steps 200 --stress
Never migrates or writes original databases. --quick omits seeded sequences.
"""
from contextlib import closing, ExitStack
from pathlib import Path
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

import accounting_audit as audit

ROOT = audit.ROOT


def command(args, database, timeout=600):
    environment = os.environ.copy()
    environment["DJANGO_DB_NAME"] = str(database)
    result = subprocess.run([sys.executable, *args], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=timeout)
    return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def rows(database):
    with closing(sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0].startswith("ledger_")]
        return {table: db.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall() for table in tables}


def named_rows(database):
    with closing(sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0].startswith("ledger_")]
        return {table: [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY id')] for table in tables}


def migration_rehearsal(source, temp):
    before = audit.digest(source)
    copy = temp / ("restore-" + source.name)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(copy)) as target:
            original.backup(target)
    old_rows = named_rows(copy)
    if not old_rows:
        return {"status": "no application schema", "source_unchanged": audit.digest(source) == before}
    result = command(["manage.py", "migrate", "--noinput"], copy)
    assert result["exit_code"] == 0, result
    new_rows = named_rows(copy)
    # SQLite table rebuilds can reorder columns; compare values by column name.
    preserved = all([{key: row[key] for key in old[0]} for row in new_rows[table]] == old if old else not new_rows[table] for table, old in old_rows.items())
    assert preserved, "Migration modified legacy financial evidence"
    check = command(["manage.py", "check"], copy)
    assert check["exit_code"] == 0, check
    assert audit.digest(source) == before
    return {"status": "migrated isolated snapshot", "source_unchanged": True, "legacy_values_preserved": preserved,
            "migration": result, "application_check": check}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=100)
    parser.add_argument("--steps", type=int, default=200)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--stress", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/accounting-repair-results.json")
    args = parser.parse_args()
    evidence = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "migrations": {}}
    def checkpoint():
        args.output.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    originals = sorted(set(ROOT.glob("*.sqlite3")) | set((ROOT / "backups").glob("*.sqlite3")))
    hashes = {str(p.relative_to(ROOT)): audit.digest(p) for p in originals}
    with tempfile.TemporaryDirectory(prefix="accounting-repair-") as directory, ExitStack() as cleanup:
        from django.db import connections
        cleanup.callback(connections.close_all)
        temp = Path(directory)
        audit.setup(temp / "synthetic.sqlite3")
        evidence["snapshot_hashes_before"] = hashes
        evidence["tests"] = command(["manage.py", "test", "ledger", "--noinput", "--verbosity", "1"], temp / "test.sqlite3")
        assert evidence["tests"]["exit_code"] == 0, evidence["tests"]
        evidence["migration_drift"] = command(["manage.py", "makemigrations", "--check", "--dry-run"], temp / "synthetic.sqlite3")
        assert evidence["migration_drift"]["exit_code"] == 0
        checkpoint()
        for source in originals:
            evidence["migrations"][str(source.relative_to(ROOT))] = migration_rehearsal(source, temp)
        print("Snapshot migrations preserve original values", flush=True)
        audit.races()
        evidence["races"] = audit.RESULTS["races"]
        for race in evidence["races"]:
            assert not any(outcome["status"] not in ("ok", "ValidationError") for outcome in race.get("outcomes", [])), race
            result = race.get("evidence", {})
            if race["name"] == "payment_payment": assert result["balance"] == 20 and result["payments"] == 1
            if race["name"] == "edit_edit": assert result["balance"] in (200, 300) and result["adjustments"] == 1
            if race["name"] == "edit_delete": assert result["balance"] == 0 and result["status"] == "reversed"
            if race["name"] == "delete_recover": assert result["balance"] == (100 if result["status"] == "active" else 0)
            if race["name"] == "duplicate_request": assert result["balance"] == 100 and result["bills"] == result["batches"] == 1
        checkpoint()
        if not args.quick:
            audit.sequences(args.seeds, args.steps)
            evidence["sequences"] = audit.RESULTS["sequences"]
            for key in ("balance_mismatches", "history_mismatches", "stale_snapshot_observations"):
                assert evidence["sequences"][key] == 0, evidence["sequences"]
            evidence["sequences"]["relationship_note"] = "Active payments referencing reversed bills are supported supplier credit, not relationship corruption."
            checkpoint()
        if args.stress:
            evidence["stress"] = command(["scripts/isolated_stress_test.py"], temp / "stress.sqlite3", timeout=900)
            assert evidence["stress"]["exit_code"] == 0, evidence["stress"]
        from django.db import connections
        connections.close_all()
    evidence["original_databases_unchanged"] = all(audit.digest(ROOT / name) == value for name, value in hashes.items())
    assert evidence["original_databases_unchanged"]
    evidence["completed"] = time.strftime("%Y-%m-%d %H:%M:%S")
    checkpoint()
    print(f"Verified repairs; results: {args.output}", flush=True)


if __name__ == "__main__":
    main()
