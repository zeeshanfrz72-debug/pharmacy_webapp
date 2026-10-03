# PythonAnywhere security and rollout steps

The repository has no deployment secrets. `pharmacy_app/settings.py` now requires `DJANGO_SECRET_KEY`, uses `DEBUG=False` by default, rejects wildcard hosts, and reads the single permitted username from `APP_OWNER_USERNAME`.

## Set private configuration first

Use PythonAnywhere's web app WSGI configuration or a private environment file outside Git. Set:

```text
DJANGO_SECRET_KEY=<new random value generated privately>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=z33shan.pythonanywhere.com
APP_OWNER_USERNAME=<the existing owner's exact username>
DJANGO_DB_NAME=<absolute path to the existing populated production SQLite database>
DJANGO_SESSION_COOKIE_SECURE=True
DJANGO_CSRF_COOKIE_SECURE=True
DJANGO_SECURE_SSL_REDIRECT=False
DJANGO_HSTS_SECONDS=0
```

Generate the Django key in a private PythonAnywhere console with `python -c "import secrets; print(secrets.token_urlsafe(64))"` and put it directly into PythonAnywhere configuration. Do not commit it or send it in chat. Rotating it invalidates existing sessions. The owner should also change the password that was shared in this conversation directly through the app before deployment.

The former Django key was committed in the public Git history. Removing it from the current settings file does not remove old Git objects; the new private key must be independent of it. Consider a coordinated history purge if the repository's published history must also be scrubbed.

## HTTPS and HSTS gate

Earlier production checks found `200 OK` on plain HTTP; current production redirect behavior has not been reverified. In the PythonAnywhere Web tab, turn on **Force HTTPS**, reload the web app, and verify from outside PythonAnywhere that an `http://` request redirects to the matching `https://` URL and that the HTTPS login page loads. Only then set `DJANGO_HSTS_SECONDS=31536000` and reload again. Verify the HTTPS response includes `Strict-Transport-Security` and that the plain HTTP request still redirects. Leave HSTS at `0` until those checks pass.

## Back up, validate, then deploy

1. Use Python 3.12 or newer, as required by the pinned Django 6.0.8 release. Compare the PythonAnywhere virtualenv's `pip freeze` with `requirements.txt`, then install the exact pinned requirements. Confirm WSGI uses that virtualenv and the private production configuration. The optional Drive helper is not part of the pinned dependencies or verified offsite recovery.
2. Set `DJANGO_DB_NAME` explicitly in both the console and WSGI configuration. Confirm it selects the existing populated production database; this checkout's default `db.sqlite3` is empty. Do not initialize a replacement empty database accidentally.
3. Obtain a consistent SQLite snapshot outside Git using SQLite's backup API, not a raw copy of a live main file. From the release checkout, `python manage.py backup_ledger --output <new-absolute-backup-path>` selects the configured database and verifies integrity, foreign keys, and required application tables before publishing. It refuses to overwrite an existing file. Verify permissions, free space, and access to the resulting backup. Keep a separate offsite copy and establish restoration independently; cloud upload and offsite recovery have not been rehearsed.
4. Restore the snapshot to a separate rehearsal database and override `DJANGO_DB_NAME` for every rehearsal command. Record financial row values and current supplier balances before migration. Run `python manage.py migrate --noinput`, `python manage.py check --deploy`, `python manage.py test ledger --noinput`, and `python manage.py collectstatic --noinput` in an isolated checkout. Review deployment warnings against the real hosting configuration; a successful command alone does not establish HTTPS or proxy behavior.
5. Confirm the rehearsal reaches migration `0012`, preserves every preexisting financial value, receipt, payment link and legitimate credit, and renders the ledger with the expected current balances. Run `python manage.py audit_bill_balances` on the rehearsal database; report legacy unlinked payments and credit without assigning them to bills. The automated local harness covers supplied local snapshots; repeat the comparison on a fresh production copy before live migration.
6. After the PR is reviewed and merged, pause financial writes, record the previous application commit, and take a fresh verified production snapshot. Check out the approved commit, run `migrate --noinput`, run `collectstatic --noinput`, and reload the PythonAnywhere web app. Any preflight or migration error stops this procedure; preserve the backup and failing data for review rather than rounding, deleting, or bypassing constraints.
7. Perform read-only sign-in, page, balance, and static-asset checks after reload. If rollback is needed, stop writes and restore the matching previous code and pre-upgrade database together. The previous snapshot loses any writes made after it was taken, so account for those before restoring. Do not downgrade schema or replay destructive audit requests on production.

## Migration compatibility

- `0007_timed_deletion_groups` adds creation links and recorded deletion membership for supported recovery. It follows the existing transaction-batch migration `0006`.
- `0008_ledger_integrity` introduces posting receipts, structured bill edit evidence, and basic monetary constraints. Raw-value preflight stops on incompatible legacy source values without silently correcting financial evidence.
- `0009_exact_cent_constraints` requires supported monetary values to have whole-cent precision.
- `0010_snapshot_bounds` validates the supported ranges and precision of stored balance snapshots while retaining legitimate negative supplier balances.
- `0011_immutable_actor_identity` adds independently preserved actor identity for new edit events.

Existing rows are not given invented original posting receipts or actor snapshots. Legacy unavailable values remain null. Review any preflight failure on an isolated snapshot before deciding on a documented business-data correction. Do not disable constraints to force an upgrade. Full migration and repair details are in [the accounting repair report](docs/accounting-repair.md).

The app deliberately fails closed for logged-in accounts other than `APP_OWNER_USERNAME`. Keep that variable set to the existing owner account, and do not create public accounts. Login failures are tracked in the database by django-axes with a five-failure, fifteen-minute rolling window.

## Offline queue behavior

The service worker caches same-origin static assets only; it never stores page or API responses. Pending transactions remain in account-scoped IndexedDB through sign-out and session expiry. Export pending actions before an upgrade or device replacement and retain the export privately outside Git. Do not clear browser storage to unblock an upgrade.

Queue storage remains at version 3 and preserves pending records. Close older open app tabs and reload. New postings include `posting_rules_version=2`; offline postings send `X-Offline-Sync: 1` and `X-Ledger-Queue-Version: 4`. Obsolete clients receive HTTP 428 for uncommitted actions, retained for explicit review under bill-specific installment rules. Matching committed retries still receive their original acknowledgement, including reversed actions. Synchronization requires Web Locks across tabs. Carry-forward and undo are online-only. Do not clear browser storage to resolve compatibility errors.

Queue saves acknowledge only after IndexedDB commits. Sync removes an item only after a JSON acknowledgement and durable queue deletion. A conclusively rejected 400 remains available for correction/export and holds later actions for the same supplier; independent suppliers can proceed. A 409 request conflict stays quarantined with its original identity for committed-history review. Network/5xx failures preserve the same request ID for retry, while authentication and CSRF failures pause synchronization until sign-in. Verify the actual owner's pending queue after updating; the migration cannot reconstruct actions previously lost by older clients.
