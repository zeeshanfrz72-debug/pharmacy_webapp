# Adversarial supplier-ledger audit

> Historical evidence from the pre-repair working tree. Repairs and their current verification are documented in [accounting-repair.md](accounting-repair.md). The findings below describe the original tested behavior.

Audited 3 October 2026, Asia/Karachi, against the **current uncommitted working tree** in `D:\pharmacy_webapp-hardening`. This is an audit and repair specification, not an implementation of the repairs. Application source, original databases, and deployment configuration were not changed.

## What can actually go wrong

The strongest reproduced accounting failure is reachable through normal owner screens: reclassify a distributor as Local Market, then edit its existing bill. The app treats a previously recorded payment as an automatically adjustable cash payment, invents a larger payment, and removes the debt. Separately, the offline client can acknowledge a transaction it did not persist, and overlapping migration of an old queue item can submit one action with two different idempotency IDs.

The normal transaction service has useful safeguards: tested payment races did not overpay; duplicate IDs committed once; stale competing edits were rejected; injected failures inside transaction services rolled back. The vulnerabilities cluster around changing historical entity meaning, alternate admin/maintenance write paths, offline persistence, and inconsistent balance snapshots. Passing the existing suite does not cover these boundaries.

### Evidence and interpretation

The Python harness contains named, minimal reproductions. The browser harness loads the **unchanged** `offline_sync.js` in real headless Chromium with real IndexedDB. Its loopback JSON server models commit/replay acknowledgements; it is not the deployed Django server. Python cases independently verify the relevant Django acceptance/rejection behavior.

Evidence files:

- `accounting-audit-results.json`: baseline suite, focused cases, race outcomes, seeded accounting sequences, source hashes, read-only database inspection, and stress results.
- `accounting-audit-additional.json`: additional focused cases, including the owner-screen debt-erasing reproduction.
- `accounting-audit-browser-results.json`: six real-browser queue scenarios.
- `accounting-audit-focused.json`: initial baseline and focused cases before the local date changed.
- `accounting-audit-clock-check.json`: controlled-clock diagnosis of the subsequent date-sensitive test failure.

The harness records current bugs as reproduced observations. A `confirmed` result means the attack reproduced, **not** that a fix passed. `boundary` means a trusted service/ORM/legacy path is required; `policy` means the observed behavior needs a business decision. Severity is P1 for data loss, invented money, or duplicated actions; P2 for important correctness, reporting, and reliability defects; P3 for misleading diagnostics. No P0 production compromise was demonstrated.

## Confirmed defects and concrete repairs

### F01 — P1: changing a supplier type can invent payments and erase debt

**Access:** configured owner, ordinary supplier and bill edit screens. **Reproductions:** `A11`, `A19`.

1. Enter a distributor bill for Rs 1,000 and a simultaneous payment of Rs 100. Debt is Rs 900.
2. Edit the supplier's type to Local Market.
3. Edit the existing bill to Rs 1,200 with no representative.

Both edits return HTTP 302. The existing `Payment.amount` becomes **Rs 1,200**, with no separately entered payment. Debt becomes **zero**. The reverse type change also changes how old cash bills are treated: a previously balanced cash purchase acquires debt after an edit.

**Expected:** changing a supplier classification must not reinterpret the settlement mode of already posted transactions. An ordinary bill correction should not manufacture an extra Rs 1,100 paid.

**Cause:** `FirmForm` permits unrestricted source-type changes; `edit_bill()` decides whether to rewrite paired payments from the firm's **current** type. See `ledger/forms.py:15`, `ledger/views.py:772`, `ledger/services.py:56`, and `ledger/services.py:87`.

**Repair:** immediately reject source-type changes for firms with financial history, including in admin. If reclassification is required later, introduce an explicit migration/reclassification workflow and persist the original settlement mode on transactions; historical edits must use that mode. Existing records need reconciliation before any backfill; don't assume their current supplier type is their original type.

**Regression:** attempt both type-change directions after partial/full payments, deletion/recovery, and edits. Reclassification is rejected without changes, or preserves original money movements through an explicit audited workflow.

### F02 — P1: offline “saved” can mean nothing was stored

**Access:** normal offline user; triggered by IndexedDB transaction failure. **Reproduction:** `B01`.

The browser aborts the actual IndexedDB write. `await saveToOfflineQueue(...)` nevertheless resolves successfully and the queue contains **zero items**. The transaction form trusts that resolved promise, shows a local-save message, and resets the fields.

**Expected:** acknowledge local persistence only after the IndexedDB transaction commits. On abort/error, retain the form and show a failure.

**Cause:** `store.add(payload)` is not awaited and its transaction has no completion/abort/error rejection path. See `ledger/static/ledger/offline_sync.js:23` and `ledger/templates/ledger/add_transaction.html:366`.

**Repair:** resolve the enqueue promise from `transaction.oncomplete`; reject it from `onerror` and `onabort`. Reset the form only after that acknowledgement. Handle failures in the offline branch as well as the network-error fallback. IndexedDB completion is an application acknowledgement, not a guarantee against device/power failure.

**Regression:** abort the write, simulate storage errors/quota failure, and verify no success message or reset; successful writes survive page reload.

### F03 — P1: one legacy queued action can become two committed actions

**Access:** user with an old queue item lacking account/request identity and overlapping sync attempts. **Reproduction:** `B03`; Django accepts equivalent new-bill submissions with different IDs in `P03`.

Two sync calls read the same legacy item before its identity is persisted. Each generates a different UUID. Real Chromium sends **two distinct request IDs**; the commit-contract fixture accepts both and removes the single queue item. Server idempotency cannot deduplicate different IDs. An equivalent bill submission with distinct IDs is accepted twice by Django.

**Expected:** one queued action obtains one stable identity before any send, including across overlapping tabs.

**Cause:** the legacy claim writes a `put()` without awaiting its transaction or coordinating competing readers. See `ledger/static/ledger/offline_sync.js:75`.

**Repair:** migrate/claim legacy rows in an atomic readwrite transaction, reading and updating the row within that transaction; send only the persisted identity after completion. Serialize sync within a page and coordinate across tabs. A page-local boolean alone does not fix cross-tab migration. Preserve IDs through failures and retries.

**Regression:** two pages claim the same old item concurrently; exactly one stable ID and one financial action result. The ordinary stable-ID queue already passed the concurrent replay test (`B04`).

### F04 — P1: admin creates money movements that the ordinary app rejects

**Access:** configured owner with Django admin model-add permissions. **Reproductions:** `A01`, `A02`, `A03`, `A21`.

- A **negative Rs 100 payment** against Rs 1,000 debt is accepted and increases debt to Rs 1,100.
- A Rs 1,000 payment against Rs 100 debt is accepted, yielding Rs 900 credit despite the service's overpayment rule.
- Negative and zero bills are accepted; a negative Rs 100 bill creates Rs 100 credit.
- A Local Market bill for Rs 500 is accepted **without its cash payment**, leaving Rs 500 debt.

**Expected:** all supported financial entry screens enforce the same signed-amount, representative, overpayment, and cash-purchase rules. Supplier credit created by a deliberate correction is distinct from bypassing those rules.

**Cause:** admin add forms save models directly; model money fields have no positive validators, and `clean()` checks only relationships. Signals post ledger effects without the main service's checks. See `ledger/admin.py:36`, `ledger/admin.py:57`, `ledger/models.py:135`, `ledger/models.py:208`, and `ledger/signals.py:10`.

**Repair:** disable financial adds in admin or replace them with explicitly supported service-backed workflows. Make monetary validation authoritative in the write service and add compatible database checks. Keep adjustment signs expressed through nonnegative increase/decrease fields; do not ban legitimate net supplier credit.

**Regression:** submit the same invalid inputs through every supported entry point; all reject atomically. Local Market purchases always produce the required bill/payment pair.

### F05 — P2: admin-created batches omit financial snapshots and attribution

**Access:** financial admin add permissions. **Reproduction:** `A03`.

The Rs 500 admin bill's batch reports `balance_after=0` and `created_by=None`. The bill accepts a manually entered `previous_debt_at_bill_time=123`, even though the supplier starts at zero.

**Expected:** supported postings have trustworthy before/after values and identify the actor; computed financial snapshots are not editable inputs.

**Cause:** the signal fallback creates a batch but never finalizes its balance/actor; admin exposes the bill's previous-debt field. See `ledger/signals.py:10`, `ledger/admin.py:36`.

**Repair:** use the same posting service for supported admin operations, pass the actor, calculate snapshots, and make derived fields read-only. Reconcile existing fallback-created records before filling missing data; do not invent attribution.

**Regression:** an admin posting reconciles its ledger effect, before/after balance, bill snapshot, and audit actor with the normal posting path.

### F06 — P2: balance snapshots switch meaning depending on the last operation

**Access:** ordinary owner posting/backdating/correction workflows. **Reproductions:** `A12`, `A13`.

Post Rs 100 today, then a Rs 200 bill yesterday. The backdated receipt says previous debt **100**, remaining debt **300**, while its chronological history row says **200**. The later bill still says previous debt **0**. Separately, deleting an earlier Rs 100 bill leaves the later Rs 200 batch's snapshot at **300** while history displays **200**; editing only its notes changes that snapshot to **200**.

**Expected:** a field has one documented meaning. If it is a posting-time receipt, notes-only edits must not rewrite it. If it is current chronological debt, all relevant writes must refresh it. The existing bill-edit specification explicitly describes current chronological snapshots.

**Cause:** create stores current posting-time balances; edit refreshes active roots chronologically; deletion/recovery does not apply the same refresh. See `ledger/services.py:21`, `ledger/services.py:175`, `ledger/services.py:238`, `ledger/services.py:302`, and `ledger/views.py:426`.

**Repair:** separate immutable posting-time receipt values from derived chronological values. Use distinct names and return values; compute chronological history consistently or maintain it after every affecting operation. Preserve old values as evidence until their meanings can be reconciled.

**Regression:** backdated additions, reversals, recovery, and notes-only edits cannot silently change the meaning of the displayed before/after balance. Match values between receipts, bill details, history, and API responses according to the selected meaning.

### F07 — P2: accepted cumulative balances make financial records unreadable

**Access:** ordinary transaction form; requires unusually large balances. **Reproduction:** `A14`.

Two separately valid bills of **Rs 9,999,999,999.99** each succeed. Debt is Rs 19,999,999,999.98. Reading the persisted transaction batches or history raises `decimal.InvalidOperation`; a subsequent valid bill request returns HTTP 500 and rolls back. The individual bill limit did not protect cumulative snapshots.

**Expected:** reject out-of-range balances before posting, or store them in fields wide enough for supported totals. Accepted records must remain readable.

**Cause:** cumulative batch and bill snapshot fields use the same 12-digit precision as individual source amounts; SQLite accepts stored totals that Django cannot deserialize. See `ledger/models.py:138`, `ledger/models.py:267`, `ledger/services.py:238`.

**Repair:** define separate aggregate limits/precision, check the resulting balance inside the transaction, and widen snapshot fields via a reviewed migration if needed. Verify actual existing magnitudes using raw read-only SQL before relying on ORM deserialization. Handle limit failures as validation errors.

**Regression:** totals around both positive and negative limits, individual maxima, repeated postings, edits, and recovery remain readable or fail without partial writes. The demonstrated threshold is far above typical pharmacy balances; do not describe it as a frequent everyday failure.

### F08 — P2: committed retries can be rejected and block all later offline work

**Access:** normal user replay after related records change. **Reproductions:** `A08`, `A09`, `B02`.

A request commits successfully. Delete its representative, or delete its selected bill. Replaying the **same request ID and payload** now returns HTTP 400 instead of acknowledging the earlier commit. The unchanged client keeps the item and stops at the first rejected row. The browser proof leaves a valid later action unsent and both rows queued.

**Expected:** distinguish an already committed replay from a new posting that references an unavailable entity. Invalid queued items must remain visible/correctable without indefinitely starving independent items.

**Cause:** fresh form/entity validation occurs before looking up the stored idempotent result; offline sync breaks on every application validation error. See `ledger/views.py:315`, `ledger/forms.py:81`, `ledger/services.py:146`, and `ledger/static/ledger/offline_sync.js:130`.

**Repair:** validate/authenticate the request identity, actor, and payload fingerprint before revalidating mutable relationships for a committed replay. Return its persisted outcome and current action state without posting again. Introduce explicit rejected/conflict queue states and a correction/export UI; proceed with independent items but preserve explicit dependencies and order where necessary.

**Regression:** lost-response replay after bill/representative/firm deletion, inactivation, and edits acknowledges the existing action once. New invalid requests still fail. A blocked item does not silently hold unrelated transactions indefinitely.

### F09 — P2: transferring a representative breaks historical supplier links

**Access:** ordinary representative edit screen. **Reproduction:** `A10`.

After a bill/payment with a representative exists, edit that representative to another firm. The edit succeeds; both historical records now reference a representative whose firm differs from their own. Historical display uses the live representative object.

**Expected:** moving today's collector cannot rewrite the supplier affiliation of yesterday's financial evidence.

**Cause:** `RepresentativeForm` exposes the firm; relationship validation does not examine existing bills/payments that reference the representative. See `ledger/forms.py:22`, `ledger/views.py:806`, `ledger/models.py:93`.

**Repair:** reject firm changes for representatives with history; create a new representative record for a new affiliation. If transfers are required, use effective-dated affiliations and immutable financial references rather than modifying the existing relationship.

**Regression:** transfer through UI and admin is rejected without changes when historical links exist. Historical representatives can be archived without breaking their original relationships.

### F10 — P2: bill-edit evidence omits changed collector and previous notes

**Access:** ordinary bill edit screen. **Reproduction:** `A20`.

Change only a bill's representative and notes. The appended zero-value adjustment records amount/date/number, but preserves neither the previous notes nor the old representative. The paired payment's representative is also overwritten.

**Expected:** the application can explain who collected a recorded payment and what was changed, without relying on the latest mutable values.

**Cause:** the description is a truncated string containing only three fields; related objects are edited in place. See `ledger/services.py:70`, `ledger/services.py:76`, and `ledger/services.py:84`.

**Repair:** add immutable structured edit events with actor/time and full before/after values for every changed field and affected payment. Preserve existing adjustment entries; backfill only evidence actually available. Clearly distinguish retrospective cash correction from a new payment.

**Regression:** collector, notes, date, number, and amount edits can be reconstructed after repeated edits/deletion/recovery; long values cannot truncate required evidence.

### F11 — P2: admin running balances count excluded legacy rows

**Access:** admin ledger reporting with legacy soft-deleted entries. **Reproduction:** `A23`.

An excluded Rs 100 opening entry plus an active Rs 50 entry produces a current debt of **50**, but the active entry's admin running balance displays **150**.

**Expected:** admin balance reporting uses the same soft-deletion inclusion rule as the ledger, while still allowing excluded rows to be inspected as audit records.

**Cause:** `LedgerEntryAdmin.running_balance()` omits `is_deleted=False`. See `ledger/admin.py:101`, compared with `ledger/models.py:44`.

**Repair:** apply the canonical balance inclusion rule in the admin calculation; keep historical visibility separate from financial contribution.

**Regression:** excluded opening/bill/payment rows contribute zero to displayed balances; reversal/restoration offsets remain included.

### F12 — P3: unrelated database errors are reported as duplicate-request conflicts

**Access:** any posting experiencing an integrity failure; reproduced by fault injection. **Reproduction:** `A18`.

Inject an unrelated `IntegrityError` during payment creation. The service rolls back correctly but reports `IdempotencyConflict` with “request ID already used,” even though no matching request exists. This misleading permanent-conflict response can stall an offline item.

**Cause:** the broad `except IntegrityError` always maps a nonmatching failure to an idempotency conflict. See `ledger/services.py:241`.

**Repair:** identify an actual existing conflicting request before classifying it; propagate/log other integrity errors with a separate operational failure response. Do not disclose database internals to the client.

**Regression:** request-ID races retain correct replay/conflict handling; unrelated injected constraint failures remain distinct, roll back, and support an appropriate retry/remediation decision.

### F13 — P2: the recent-history limit does not limit loaded financial records

**Access:** dashboard/recent history as the ledger grows. **Reproduction:** `A25`.

A valid synthetic ledger with 1,000 same-day transactions requests ten recent actions. The function returns ten rows but materializes **all 1,000 ledger entries**, along with related models and audit data. This is a measured work-amplification defect, not a demonstrated production timeout.

**Cause:** the limit chooses a date cutoff, loads every entry on/after that date, and slices only after building history. See `ledger/views.py:432` and `ledger/views.py:513`.

**Repair:** select the actual recent action identities first, aggregate prior balances separately, and fetch only those actions' complete entries. Handle legacy unbatched entries and date/ID ties explicitly; do not SQL-limit individual rows in a way that splits a bill/payment batch.

**Regression:** 10 requested actions load only their bounded complete postings, even with thousands of transactions on one date, and preserve independently verified running balances.

## Trusted write and legacy boundaries

These are reproduced integrity weaknesses, **not demonstrated unauthenticated or ordinary HTTP exploits**.

- **BND01 — Service caller validation (`A04`, `A05`, `A22`).** Direct calls accept negative amounts, zero bills, and empty actions. Fractional-cent values can disagree between independently summed deserialized rows and SQLite aggregates: a 0.005 bill/0.004 payment reads as 0.00/0.00 while `current_debt()` reports 0.001. HTTP forms reject those values. Oversized/non-finite/malformed service values produced errors and rolled back in the tested matrix. Fix by validating finite cent-precision amounts and complete action shape at the service boundary, with compatible database checks. Regression: identical guarantees for every supported caller.
- **BND02 — ORM updates and signal atomicity (`A15`, `A24`).** A direct `Bill.save()` changes source amount 100 to 200 while ledger debt stays 100. Under autocommit, an injected signal failure leaves a persisted bill with no ledger entry. Django admin wraps its change form in a transaction, so the orphan demonstration is specifically a direct ORM/maintenance risk, not an admin rollback defect. Fix by enforcing audited posting/correction APIs, restricting source mutation, and validating import/maintenance tools. Regression: authorized corrections post the exact delta; failed creates leave no orphan.
- **BND03 — Missing last-line financial constraints (`A16`).** A direct ORM insert accepts an invented entry type, a cross-firm bill link, and negative ledger increase, creating a -70 balance. Model `clean()` is not automatically called by `save()`, and there are no corresponding checks. Fix nonnegative/single-sided entry constraints with allowances for documented zero audit adjustments; enforce relationships in posting services and reconciliation checks. Regression: invalid inserts are blocked or explicitly quarantined, including bulk/import paths. Do not assume portable SQL CHECK constraints can enforce joins across tables.
- **BND04 — Legacy visibility/recovery mismatch (`A17`, `A07`).** Soft-deleting a bill's creation entry excludes its debt but leaves the bill available; deleting it then fails with “no active ledger entries to reverse.” Legacy/admin Local Market bills without a same-batch payment also cannot preserve zero debt when edited. Fix migration reconciliation and availability rules based on a complete recorded posting, without silently restoring old excluded debt. Regression: legacy excluded records remain nonfinancial and recover only through a defined correction workflow.

## Policy gaps, not arithmetic corruption

- **Payments referencing a deleted bill (`A06`).** A payment independently deleted before bill deletion can later be recovered alone; the bill remains unavailable and the supplier acquires credit. This is reachable and inconsistent with new-payment selection rules, but the app already explicitly supports bill reversal after payment as supplier credit (`ledger/tests.py:143`). Its monetary effect is accurate. Decide whether recovery should allow an explicit credit-with-historical-reference, require recovering the bill, or detach the live allocation while retaining audit links. Do not silently discard the payment or automatically restore unrelated deletions.
- **Reducing a fully paid bill (`P01`).** A 1,000 paid bill reduced to 500 creates 500 supplier credit. Existing credit support makes this a valid correction possibility, not proof of a bug. Define supporting-document requirements and distinguish this from a cash refund, which is not implemented.
- **Restated history versus historical event snapshots (`P02`).** The app deliberately moves bill creation dates and paired cash dates on edits and uses current source amounts for summaries. Define whether reports show restated business activity or what was known/paid at that time. F06 concerns mixing those meanings in the same fields, rather than rejecting all backdating.
- **Duplicate invoice references (`P03`).** Two different request IDs for identical supplier/reference/date/amount both post. A diagnostic flags duplicates, but there is no reference uniqueness rule. Decide the supplier/reference identity policy, including corrected/reused references, before introducing a uniqueness constraint. The legacy offline identity defect F03 is independently confirmed.
- **Bill-linked payments are supplier-level settlements.** Current code caps payments by total supplier debt, not the selected bill's remaining amount. Project documentation explicitly defers invoice allocations/overdue accounting. Do not call this an invoice-overpayment bug or invent FIFO rules without a new allocation requirement.
- **Supplier deletion/write race.** A controlled race committed a transaction while supplier deletion was paused, then retained that debt in the deleted supplier. This is consistent with write-before-delete ordering and the documented retention policy; it is a visibility/confirmation concern, not demonstrated lost money.

## Existing data and operational limits

The configured default `db.sqlite3` is **zero bytes and has no Django schema**. Both available preview/backup databases passed SQLite integrity checks and foreign-key checks. No negative source amounts, cross-firm financial links, orphan source postings, or active-payment/deleted-bill cases were found by the exported inspection queries. One backup contains one unbatched ledger row; nullable legacy/opening entries are supported, so that alone is not corruption. These local preview files do not establish the condition of production.

Each source database's SHA-256 was recorded before inspection, after snapshot creation, and at completion. Original files are opened read-only; snapshots use SQLite's backup API. Business names, notes, credentials, and real balances are not exported. The snapshot query checks are structural and do not prove agreement with external supplier statements.

The optional `ledger/drive_backup.py` utility is not a verified backup facility: its PyDrive2 import is absent from the pinned requirements, it hardcodes `db.sqlite3` instead of the configured database, and it uploads the raw file rather than a consistent SQLite snapshot. No authentication/upload was attempted. A remote backup or restore failure was not reproduced. A repair should use the configured database, produce a verified consistent snapshot, and prove restoration in isolation before claiming backup protection.

Actual power loss, full disk, browser/device destruction, production configuration, remote storage, external supplier documents, and exhaustive cross-browser/multi-tab behavior were not tested. The queue abort and lost-response tests use controlled fault injection. These limits are separate from the deterministic defects above.

## Validation record

| Check | Result |
|---|---|
| Existing Django suite, initial 2 October run | 69/69 passed |
| Same suite after local date changed to 3 October | 68 passed; 1 date-dependent fixture failure |
| Controlled-clock diagnosis of that failure | Targeted test passed with the 2 October business date |
| Django system checks | Passed |
| Focused Python scenarios | 33 completed; no harness errors; includes controls, boundaries, and policy cases |
| Browser scenarios | 6 completed; 3 defects reproduced and 3 controls passed |
| Independent seeded accounting model | 100 seeds x 200 steps = 20,000 attempted operations; 1,584 steps skipped when an action's preconditions were absent |
| Balance and chronological-history mismatches in seeded valid-amount streams | 0 and 0 |
| Active-payment/deleted-bill state observations | 4,037 operation steps; repeated observations of the documented credit/policy gap, not 4,037 independent defects |
| Active snapshot/chronological balance disagreement observations | 7,293 operation steps; repeated observations of F06, not distinct bugs |
| Controlled file-SQLite races | 6 scenarios; inspected balances preserved the tested posting effects |
| Isolated HTTP stress ramps | 5, 10, 20, and 40 clients; five permitted payments at each ramp, excess payments rejected; all balances correct |
| Stress SQLite lock errors / HTTP 500s | 0 / 0 |
| Application source and original database hashes | Unchanged |

**T01: date-dependent test fixture.** `ledger/test_dashboard.py:29` fixes its business date to 2 October 2026, while the second payment at line 58 omits `bill_date` and uses the real date. Its final assertion at line 70 expects both actions on the fixed date. It passed before midnight and failed after midnight (`1 != 2`); the controlled-clock rerun passed. This is a reproduced test defect, not a recovery arithmetic failure. Repair the fixture by explicitly assigning the same business date to that payment or freezing the relevant clock for the whole test.


Focused rollback injections covered ledger creation, payment creation, final batch save, deletion membership, and restoration offsets. Rejected HTTP amounts and wrong-firm representatives left no ledger entries. Owner authentication, missing CSRF, and another authenticated account were rejected without money writes. Races covered payment/payment, edit/edit, edit/delete, delete/recover, duplicate requests, and supplier deletion/write. The suite tests migration backfill/recovery; these are not a production migration rehearsal.

## Repair order and acceptance gates

1. **Prevent invented/lost money:** block historical supplier-type/representative reassignment; disable bypassing financial admin adds; acknowledge offline persistence only after commit; make legacy queue identities atomic and stable.
2. **Make retry/recovery dependable:** recognize authenticated committed replays before mutable form validation; expose blocked queue rows and preserve dependencies; document the deleted-bill payment-credit policy.
3. **Unify the financial write boundary:** finite positive cent-precision source amounts, complete actions, actor attribution, amount limits, compatible database constraints, and audited correction APIs for maintenance/imports.
4. **Repair financial evidence:** distinct immutable posting snapshots versus current chronology, full structured edit events, consistent admin balances, and reconciliation of existing records. Do not bulk-rewrite source data or snapshots without preserving evidence and checking supplier statements.
5. **Verify rollout:** run the existing suite plus conventional regression tests asserting the repaired behavior; repeat seeded invariants and file-based races; migrate and reconcile an isolated consistent copy; prove backup restoration. Only then review a production change separately.

Required API/model changes belong to the repair phase: service-backed admin writes, replay acknowledgement semantics, queued error states, aggregate field capacity/limits, immutable settlement mode if reclassification is supported, and structured edit-event records. None were added during this audit.

## Reproduce

Use an isolated environment with `requirements.txt` installed; the audit used Python 3.12.10 and all exact pins, including Django 6.0.8 and django-axes 8.3.1.

```powershell
python scripts/accounting_audit.py --seeds 100 --steps 200 --races --snapshot --baseline --stress --output docs/accounting-audit-results.json
```

The script overrides database and authentication settings with synthetic values, creates disposable databases, and closes its connections before cleanup. Focused cases and races use file SQLite; seeded arithmetic sequences use in-memory SQLite. `--snapshot` only reads the local database files and creates temporary consistent copies. `--seeds 0` runs focused cases without random sequences. Omit `--snapshot` to skip inspecting existing files. Environment variables inherited by the stress subprocess are replaced with that script's own disposable setup.

For browser reproductions, provide Playwright and a Chromium executable:

```powershell
node scripts/accounting_audit_browser.cjs 'C:\Program Files\Google\Chrome\Application\chrome.exe'
```

The audit used the Codex bundled Node/Playwright runtime via `NODE_PATH`; no project package manifest or dependencies were changed. Browser contexts and the loopback fixture are closed at the end. Inspect named cases in the JSON evidence to distinguish confirmed defects from checks that passed or require policy decisions.
