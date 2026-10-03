# Multiple bills, installments, and debt carry-forward

Each new payment settles one enabled, available bill. Older enabled bills remain selectable. Installments are bounded by both the selected bill's remaining debt and the firm's net payable balance. Historical credit and unassigned legacy payments remain intact; no invoice assignments are guessed.

Local-market purchases start unpaid. Their active same-firm representative and supplier reference are optional. Missing references are stored as an empty string and displayed as **Local Market Bill #ID / مقامی مارکیٹ کا بل #ID**. Explicit payments retain their amounts when bills are corrected, including historical cash pairs. Other source types still require references and representatives.

## Financial meanings

| Display | Meaning |
| --- | --- |
| New charges | Current corrected purchase amount, excluding carried debt |
| Carried debt | Incoming transfers still in effect |
| Actual payments | Payments less reversals, including restorations |
| Transferred out | Outgoing transfers still in effect |
| Remaining debt | New charges + carried debt − actual payments − transferred out |
| Original posting receipt | Immutable amounts, references, actor and balances at posting |
| Corrected chronological balance | Current corrected actions in business-date order |

Previous-debt snapshots never constitute additional purchases. Firm balances also include opening balances and legacy entries without invoice assignments. Deleted bills can retain historical credit from independently recorded payments; their evidence remains in Trash and the firm balance.

## Disable and undo

The **Disable & carry forward** action displays the remaining amount and confirmation in English and Urdu. Select another enabled same-firm bill or create a destination with **new charges only**. Cancel changes nothing. Saving obtains the firm's write lock, checks both displayed revisions, posts equal transfer-out/in legs, stores an immutable receipt, and disables the source atomically. A new destination purchase is a separate posting inside that same database transaction; failure rolls back both.

Moving 6,000 remaining debt to 8,000 new charges makes the destination payable for 14,000. Transfers change neither firm debt nor purchase/payment totals. Fully paid sources transfer zero; sources with credit require reconciliation.

Undo later transfer chains first. The destination must retain enough remaining debt to return the transferred amount; reverse dependent payments first when necessary. Undo appends opposite entries and re-enables the source. A destination purchase created during carry-forward remains a purchase after undo. Active transfer participants cannot be deleted. Disabled sources cannot receive payments, financial edits, or payment reversals/restorations until undo.

## Compatibility

Migration **0012** adds disabled state, blank-reference support, action types and protected transfer receipts. It does not rewrite historical bills, amounts, payments, links, receipts or snapshots.

IndexedDB remains schema version **3**. The posting protocol changes to `posting_rules_version=2` and `X-Ledger-Queue-Version: 4`. Matching committed requests are acknowledged before current relationship, client-version, deletion/disable and reversal checks. Uncommitted older forms/queues receive 428 and remain available for explicit correction/export. Editing a conclusively rejected item records its prior payload and obtains a new request ID; response loss keeps its original identity. Blocked suppliers' later actions wait while independent suppliers synchronize. Carry-forward and undo are online only. Close older app tabs and reload; do not clear browser storage.

`python manage.py audit_bill_balances` produces a read-only aggregate compatibility report: reconciliation, relationship defects, legacy receipt gaps, unassigned payments and credit. It does not repair or assign records.

## Verification

Use Python 3.12 and pinned `requirements.txt` in an isolated environment. Test and audit databases are disposable; original SQLite files remain excluded from Git.

```text
python manage.py test --noinput
python manage.py makemigrations --check --dry-run
python scripts/accounting_bill_sequences.py --seeds 100 --steps 200 --workers 4
python scripts/accounting_bill_races.py
python scripts/accounting_bill_browser.py
python scripts/accounting_repair_app_browser.py
python scripts/isolated_stress_test.py
git diff --check
```

The independent Decimal oracle derives expectations from accepted actions rather than application balance helpers. It checks every bill, firm balance, purchase/payment totals, corrected history and immutable receipts after **20,000** operations (100 seeds × 200), covering installments, edits, deletions, exact deletion-group recovery, transfers, undo, rejected actions and replay. The file database harness covers **40** controlled races. Chromium verifies real HTTP forms, bilingual confirmation, optional references/representatives, carry-forward destinations, queue review and persisted balances. Separate queue tests cover 11 storage, replay, upgrade, authentication, export and cross-tab cases. HTTP stress covers 5/10/20/40 clients with no lock errors or HTTP 500s. Migration rehearsals compare all original financial columns and unchanged source hashes.

Evidence: [sequences](accounting-bill-sequences.json), [races and migration rehearsals](accounting-bill-races.json), [application browser](accounting-bill-browser.json), [queue browser](accounting-bill-queue-browser.json).

## Production

Follow [the deployment procedure](../DEPLOYMENT_SECURITY.md). Rehearse migration 0012 against a fresh consistent production snapshot and compare every original financial column and supplier balance before/after. Verify backups with integrity/foreign-key checks. Then pause writes, take another verified backup, apply additive migrations, collect static assets, switch the release and reload. Perform read-only smoke checks; never post synthetic accounting actions to production. Keep the old release, WSGI/config and verified backup privately for rollback. Supplier statement reconciliation and offsite restoration require separate evidence.
