"""Consistent SQLite snapshots, published only after structural verification."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tempfile

from django.conf import settings


def verify_snapshot(path):
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Backup failed SQLite integrity verification.")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Backup has broken foreign keys.")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"django_migrations", "ledger_firm", "ledger_bill", "ledger_payment", "ledger_ledgerentry"} <= tables:
            raise ValueError("Backup is incomplete: expected application tables are missing.")
        return {"integrity": "ok", "ledger_entries": db.execute("SELECT COUNT(*) FROM ledger_ledgerentry").fetchone()[0],
                "migrations": db.execute("SELECT COUNT(*) FROM django_migrations").fetchone()[0]}


def create_snapshot(destination):
    config = settings.DATABASES["default"]
    if config["ENGINE"] != "django.db.backends.sqlite3" or str(config["NAME"]) == ":memory:":
        raise ValueError("A configured file SQLite database is required.")
    source = Path(config["NAME"]).resolve(strict=True)
    destination = Path(destination).resolve()
    if source == destination or destination.exists():
        raise ValueError("Choose a new backup filename; existing files are never overwritten.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ledger-backup-", suffix=".sqlite3", dir=destination.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
            with closing(sqlite3.connect(temporary)) as copy:
                db.backup(copy, pages=256)
        result = verify_snapshot(temporary)
        # Exclusive publication prevents concurrent backups overwriting one another.
        os.link(temporary, destination)
        return result
    finally:
        Path(temporary).unlink(missing_ok=True)
