# Ledger integrity repairs and remaining audit gaps

Implemented in the existing working tree in `D:\pharmacy_webapp-hardening`, 3 October 2026 (Asia/Karachi). Existing uncommitted features were retained. Original databases were read through SQLite snapshots and were never migrated, corrected, or reset. No production requests, uploads, or deployment occurred.

The original [accounting audit](accounting-audit.md) remains historical evidence. Its `confirmed` results describe reproduced defects before repair; they are not passing regression tests. Use the repair harnesses below against the repaired application.

## What changed

| Original findings | Repair and regression coverage |
|---|---|
| F01 / F09: changing supplier type or representative affiliation changes historical accounting | `Firm.save()` and `Representative.save()` acquire the same supplier write lock used by posting. Forms/admin reject historical changes. Unused representatives can transfer, but a supplier cannot become Local Market while retaining representatives. Representative saves reread the supplier inside the lock to reject stale affiliations. Financial history remains attached to its original supplier. |
| F04 / trusted alternate source writes | Bill/payment admin additions are disabled; financial records and edit evidence are read-only in admin. Direct Bill/Payment saves require the supported posting context. Service validation rereads the configured active owner inside financial writes and checks positive finite whole-cent amounts, complete actions, relationships, settlement rules, notes, dates, and references. Ordinary source saves cannot leave an orphan when a signal fails. |
| F02: acknowledgement before IndexedDB commit | Queue writes resolve only on `transaction.oncomplete`; abort/error rejects. Every form save path handles storage failure and retains the form and request ID. Real application browser tests verify this, beyond a queue-only fixture. |
| F03: duplicate legacy migration/synchronization | Legacy identity assignment is one IndexedDB read/write transaction, completed before HTTP. Web Locks serialize synchronization across tabs. Storage version 3 excludes obsolete v2 writers after upgrade; old open connections block upgrade with an explicit close-tabs message. New offline postings require the version-3 header; obsolete clients receive 428 without posting. Previously committed retries still receive the original acknowledgement. |
| F08: replays fail after referenced records change; one invalid item stops unrelated suppliers | Authenticated committed-ID lookup precedes mutable form/entity validation. A reversed or deleted original is acknowledged without a new financial effect. Queue failures remain visible with correction controls and export. A rejected item blocks subsequent actions for its supplier; other suppliers proceed. Ambiguous outcomes retain their original ID. Only conclusively rejected 400 responses permit correction with a new ID; 409 conflicts retain their identity and require committed-history review, with prior rejected data retained until acknowledgement/export. |
| F07 / additional precision probe | `ledger/money.py` converts individual bounded monetary values to integer cents before SQL aggregation; net debt sums signed cent differences. Individual values retain their existing `9,999,999,999.99` limit. Current and stored corrected chronological balances are validated inside the atomic write before acceptance. Database constraints protect cent precision, supported ranges, positive source amounts, and nonnegative single-sided ledger entries. Credit and zero audit adjustments remain valid. Matched actions may temporarily exceed the snapshot range between entries when their stored before/after balances remain representable. |
| F06: mixed snapshot meanings | New postings capture original receipt JSON: date, source, supplier, amounts, references, attribution, and before/after balance. Receipts remain unchanged through corrections, deletion, recovery, and retries. Active history separately shows corrected chronology. New batch snapshots refresh after posting, editing, deletion, and recovery. Legacy original receipts remain null/unavailable, and legacy stored snapshots are preserved. Active and Trash details expose original receipts separately. |
| F10: incomplete edit evidence | Immutable `BillEditEvent` records the original actor ID/username independently of the mutable account link, timestamp, and full before/after bill values plus paired payment amounts, dates, representatives, methods, and notes. Evidence failure rolls back the financial correction. |
| F11: admin includes soft-deleted entries | Admin running balances use the same exact-cent aggregation and exclude soft-deleted entries. |
| F12: unrelated database errors masquerade as request conflicts | Only a real conflicting existing request produces an idempotency conflict. Other integrity failures propagate as database failures and roll back; they do not receive a false success/conflict acknowledgement. |
| F13: recent-ten materializes an entire same-day ledger | Recent history selects action identities, loads their complete entries, and calculates the preceding balance without hydrating the rest of the day. A 30-action test hydrates exactly the 20 entries belonging to the selected ten bill/payment actions. |
| T01: midnight-dependent fixture | The second payment explicitly uses the fixture's business date. A fixture creating an invalid local-market representative now uses another valid source type. |

Business policy is unchanged: payments settle supplier debt rather than allocated invoice balances; duplicate references warn; local-market purchases remain paired; reducing/reversing a paid bill can create supplier credit; recovery restores recorded deletion membership, including supported credit linked to a reversed bill. There is no invoice-allocation or refund subsystem.

### Additional account-boundary findings

**P2 audit-attribution risk; account-administrator access required.** A mutable account foreign key cannot preserve the original actor name after account rename/deletion. Expected original attribution must remain identifiable; displaying the current linked account or null can change or lose that evidence. Receipts now capture the original actor ID/username, and `BillEditEvent.actor_snapshot` preserves it independently of the nullable account link. The account rename/delete regression verifies unchanged original attribution. Original legacy attribution that was never recorded remains unavailable.

**Stale-service authorization boundary; concurrent account maintenance required.** A cached authenticated user object can still say `is_active=True` after its database account is deactivated. Supported services now query current owner status and repeat that check after acquiring the financial write lock. The regression deliberately keeps the stale object and verifies that posting is rejected without financial writes. Reversal/recovery no-ops also require owner authentication. Code: `ledger/services.py` (`_actor`, posting/correction/offset services) and `ledger/models.py` (`BillEditEvent`).

## API and client compatibility

Existing request IDs and `created` / `duplicate` acknowledgements remain. Responses add `action_state` and `posting_receipt`. `previous_debt` and `remaining_debt` describe the original receipt, including on replay; unavailable legacy original values are null. Corrected chronology is labelled separately in history. Existing chart numeric arrays remain for presentation; additive `monetary_values` / `monetary_amount_values` return exact monetary strings.

New queued submissions send `X-Offline-Sync: 1` and `X-Ledger-Queue-Version: 3`. Obsolete offline clients cannot post fresh actions; an authenticated matching retry is acknowledged before the version check. Normal online submissions retain their existing protocol. Static cache/query versions are bumped. IndexedDB keeps the same pending store and upgrades the database version, preserving pending records.

Older open tabs must close before the storage upgrade can finish. Do not clear browser storage to resolve this: closing/reopening the updated application retains the queue. A browser without Web Locks retains the queue rather than synchronizing without coordination. Device queues are outside the server database backup; export them before replacing a device or changing account identity.

## Additional confirmed operational defect: backup selection and consistency

**Severity:** P1 recovery risk, operator/backup-job access required; this is not an ordinary-user web exploit.

**Trigger:** configure a populated database through `DJANGO_DB_NAME`, then invoke the old Drive helper, which selected hardcoded `db.sqlite3`; or copy an SQLite main file while committed changes reside in its WAL.

**Expected:** back up the configured complete committed ledger and make failures visible. **Original behavior:** hardcoded selection, raw file upload, and swallowed exceptions. The repository's default `db.sqlite3` is empty, while its populated preview databases are distinct. No actual cloud upload or production loss is claimed.

**Financial impact:** restoration from the wrong/empty/stale file can lose the accounting book or recent transactions.

**Repair:** `ledger/backup.py` selects `settings.DATABASES['default']`, uses SQLite's consistent backup API, verifies integrity/foreign keys/application tables, and publishes a new destination exclusively after verification. Existing files are never overwritten. Failed verification/publication removes temporary output. `manage.py backup_ledger --output <new-path>` exposes this locally. The optional Drive helper lazily imports its provider and uploads the verified snapshot; failures now propagate.

**Evidence:** configured-path and corrupt/incomplete-file regression tests; injected publication failure; controlled WAL writer checks showing that the raw main file misses committed updates while the snapshot includes them; restoration to an isolated application's system checks and balance queries. PyDrive2 is not in the pinned runtime, and Drive credentials, remote uploads, retention, and offsite restoration remain unverified.

## Verification and reproducibility

All application tests use disposable databases and pinned `requirements.txt` dependencies (Python 3.12.10 / Django 6.0.8). The repair verification records command output and original database hashes.

- Conventional suite: 96 tests, including money validation, affiliation guards, receipt immutability, structured edits, stale/committed retries, credit/recovery, rollback faults, raw-SQL constraints, escaping, authorization, expired sessions, and month/year report boundaries.
- Independent Decimal oracle: 100 distinct seeds × 200 operations, zero balance mismatches, zero chronological-history mismatches, zero stale new-batch snapshots. The 4,037 observations of active payments referencing reversed bills are supported credit states, not corruption.
- Precision: a synthetic maintenance load of 40,000 maximum-sized cancelling entries plus one cent reconciles to exactly `0.01` through application balance/report helpers. This requires maintenance/ORM access to construct efficiently; the query defect is exercised at application level.
- Real file races: competing payments, competing edits, edit/delete, delete/recover, duplicate requests, supplier delete/write, and repeated supplier/representative affiliation races. Persisted effects and relationships are verified, not just HTTP codes.
- Stress ramps: 5/10/20/40 clients competing for a 5,000 debt accept exactly five 1,000 payments, reject excess payments, and leave zero debt; no lock-error or HTTP-500 observations.
- Chromium: eleven real IndexedDB/loopback-contract checks, including cross-tab claims, aborts, conclusive correction/export, committed-ID conflict quarantine, lost responses, and mixed v2/v3 clients. Three additional browser checks run the actual Django form/server and verify three 100 bills, three 10 payments, and final debt `270.00`.
- Upgrade rehearsal: both populated original snapshots migrate through 0011, preserving every prior ledger-column value and leaving original hashes unchanged. Comparisons use column names because SQLite table rebuilds may reorder columns. The empty default database remains untouched. Invalid legacy fractional money stops migration before changing evidence; correcting the disposable fixture permits retry.
- Operational/security probes: held writer lock with successful retry, CSRF rejection, unauthorized-owner rejection, escaped stored content, malformed/non-finite/oversized input rejection, failed backup publication, corrupt/incomplete snapshots, and isolated restore.

Run from the repository using the pinned Python environment:

```powershell
python scripts/accounting_repair_verify.py --quick --output docs/accounting-repair-final-checks.json
python scripts/accounting_repair_sequences.py --workers 4 --seeds 100 --steps 200
python scripts/accounting_repair_edges.py
python scripts/accounting_repair_app_browser.py
node scripts/accounting_repair_browser.cjs "C:\Program Files\Google\Chrome\Application\chrome.exe"
python scripts/isolated_stress_test.py
```

The browser scripts use the bundled Node/Playwright runtime when available; set `NODE_PATH` to its `node_modules` directory for the standalone Node command. The Python application-browser launcher sets it automatically. These scripts launch only their own disposable loopback servers. They do not contact production. The application-browser fixture temporarily contains an authentication cookie and is removed with the disposable environment; evidence never includes it.

Evidence:

- [Final suite, migrations, file races, and original hashes](accounting-repair-final-checks.json)
- [Final independent sequence run and source hashes](accounting-repair-sequences.json)
- [Final contention stress run](accounting-repair-stress.json)
- [Earlier repair verification](accounting-repair-results.json)
- [Eleven Chromium queue checks](accounting-repair-browser-results.json)
- [Actual Django application browser checks](accounting-repair-app-browser.json)
- [Wider lock, affiliation, WAL, restore, request, and migration probes](accounting-repair-edges.json)

## Remaining gaps and practical limits

No reproduced ordinary-user balance-corruption case remains in these tested paths. That is a scoped result, not proof that every possible accounting or operational failure is eliminated.

1. **Actual business reconciliation:** no supplier statements, cash/bank evidence, or opening-balance source documents were provided. Software consistency cannot detect a real payment never entered, a fabricated bill, or an incorrect opening balance. Compare supplier statements and payment evidence against the corrected ledger and receipts before treating the book as externally reconciled.
2. **Production and offsite recovery:** actual environment selection, filesystem permissions/capacity, remote credentials, backup retention/encryption, scheduling, loss of the server/device, and a restore from a real offsite copy are not established. The local snapshot/restore procedure passed; the remote provider did not run. Select the intended populated database explicitly at deployment rather than accidentally initializing the empty default file.
3. **Deployment security:** no live HTTPS/proxy/cookie/header checks, production account review, network penetration test, dependency/supply-chain audit, or host hardening was performed. Local permission, CSRF, escaping, session, and malformed-input tests passed. Production configuration remains a separate verification task.
4. **Upgrade and browser limits:** tested storage failures and blocked version upgrades preserve data. Abrupt power loss, actual disk exhaustion, all browser/OS combinations, device replacement, and queues modified by obsolete clients before this boundary was installed remain outside the established evidence. The mixed-version barrier protects the tested upgrade going forward; it does not reconstruct financial actions lost or duplicated before repair.
5. **Scale:** exact-cent query checks cover large cancelling totals and the tested contention/history sizes. SQLite integer aggregates have a finite capacity; histories large enough to exceed signed 64-bit cent totals, multi-hour workloads, and alternative database backends have not been validated. Existing supported monetary limits are intentionally preserved. Near-limit actions that would make stored chronological/reversal snapshots unreadable are rejected atomically.
6. **Privileged database access:** model/admin/service guards and database amount constraints protect supported paths. A maintenance operator using raw SQL, queryset updates, or altered application code can still change relationships/evidence or fabricate financial data. No application-only audit log is tamper-proof against a database administrator. Synthetic/raw-SQL tests are labelled accordingly.

Legacy unknown original posting balances stay unavailable. Migration preflight preserves evidence and stops on incompatible values; it does not silently round, delete, or repair financial history. Review such anomalies on a snapshot before any deliberate data correction. No automatic statement reconciliation or new refund/allocation policy was added.

Deployment, live migrations, offsite uploads, and business-data corrections are separate steps. This implementation and all rehearsals leave the original databases unchanged.
