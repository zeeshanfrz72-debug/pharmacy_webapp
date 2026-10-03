"""Verify a consistent backup and rehearse upgrades without migrating the source.

python scripts/rehearse_bill_upgrade.py --config /private/production-config.json \
    --backup /private/new-backup.sqlite3 --rehearsal /private/rehearsal.sqlite3

All sensitive configuration and accounting records stay on the current host.
Only the selected rehearsal database receives migrations or smoke-test sessions.
"""
import argparse
from contextlib import closing
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def financial_rows(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ledger_%'")]
        return {name: [dict(row) for row in db.execute(f'SELECT * FROM "{name}" ORDER BY id')] for name in tables}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--rehearsal', type=Path, required=True)
    parser.add_argument('--skip-tests', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    os.environ.update({key: str(value) for key, value in config.items()})
    os.environ['DJANGO_SETTINGS_MODULE'] = 'pharmacy_app.settings'
    source = Path(config['DJANGO_DB_NAME']).resolve(strict=True)
    backup, rehearsal = args.backup.resolve(), args.rehearsal.resolve()
    if source in (backup, rehearsal) or backup == rehearsal or backup.exists() or rehearsal.exists():
        raise ValueError('Choose new, distinct backup and rehearsal filenames.')
    if ROOT in backup.parents or ROOT in rehearsal.parents:
        raise ValueError('Keep private databases outside the release checkout.')
    sys.path.insert(0, str(ROOT))
    import django
    django.setup()
    from ledger.backup import create_snapshot, verify_snapshot
    source_hash = digest(source)
    verified = create_snapshot(backup)
    os.chmod(backup, 0o600)
    before = financial_rows(backup)
    shutil.copyfile(backup, rehearsal); os.chmod(rehearsal, 0o600)
    env = os.environ.copy(); env['DJANGO_DB_NAME'] = str(rehearsal)
    log_path = rehearsal.with_suffix('.log')
    report = {'backup_verified': verified, 'backup_sha256': digest(backup), 'commands': []}
    with log_path.open('w', encoding='utf-8') as log:
        os.chmod(log_path, 0o600)
        commands = [['migrate', '--noinput'], ['makemigrations', '--check', '--dry-run'],
            ['check'], ['check', '--deploy'], ['audit_bill_balances']]
        if not args.skip_tests:
            commands.append(['test', '--noinput'])
        for command in commands:
            result = subprocess.run([sys.executable, 'manage.py', *command], cwd=ROOT, env=env,
                stdout=log, stderr=log, check=True)
            report['commands'].append({'command': command, 'exit_code': result.returncode})
    after = financial_rows(rehearsal)
    for table, rows in before.items():
        if not rows:
            if after[table]:
                raise AssertionError(f'Migration added historical rows to {table}')
        elif [{key: row[key] for key in rows[0]} for row in after[table]] != rows:
            raise AssertionError(f'Migration changed historical evidence in {table}')
    if after.get('ledger_billcarryforward'):
        raise AssertionError('Migration invented carry-forward records')
    report.update(legacy_columns_preserved=True, restored_snapshot_verified=verify_snapshot(rehearsal),
        source_unchanged=digest(source) == source_hash)
    proof_path = rehearsal.with_suffix('.report.json')
    proof_path.write_text(json.dumps(report, indent=2), encoding='utf-8'); os.chmod(proof_path, 0o600)
    from django.db import connections
    connections.close_all()
    print(json.dumps({'backup_verified': True, 'legacy_columns_preserved': True,
        'source_unchanged': report['source_unchanged'], 'tests_run': not args.skip_tests,
        'report': str(proof_path)}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
