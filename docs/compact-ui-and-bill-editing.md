# Compact tables, bill editing and preview reset

## Interface

- Bills have an Edit action in the row's `⋯` menu, including the firm drilldown.
  Number, date, representative, amount and notes can change. The firm/source stay
  fixed because transferring a bill between accounts is a separate operation.
- Firms has its own Add Firm form. New firms start outside Trash with zero debt.
- The dashboard Total Debt card no longer includes Supplier Credits. Signed debt
  values and separate credit analytics retain their existing accounting meaning.
- Tables use fixed layouts, readable 14px values, darker 14px/800 English headers,
  and heavier page headings. Urdu retains the bundled Noori font, with a slight
  text stroke on headings. Ordinary desktop rows are about 36px; unusually long
  recorded names/references wrap without truncation.
- Row menus contain Edit/Delete/Recover and supplementary Details. Less essential
  columns move into Details at narrower widths. The table itself does not require
  horizontal scrolling. Native disclosures work without JavaScript; the enhanced
  Delete/Recover dialog retains reason validation and keyboard focus handling.
- Full displayed dates and date-entry fields use `DD-MM-YYYY`. ISO date inputs and
  existing drilldown URL parameters remain accepted for compatibility.

## Accounting when a bill changes

The original creation entries remain intact. An adjustment containing the amount
difference and an edit description is appended to the original transaction batch.
Delete/Recover therefore reverses/restores the full current effect of that batch.
An unchanged repeated edit creates no second adjustment. A revision token rejects
a differing edit submitted from a stale page.

For Local Market bills, the creation batch's cash payment changes by the same
amount, keeping the bill fully paid and its debt effect zero. Company payments
keep their amounts. Later linked payment transactions retain their dates and
representatives. Updating a bill's date moves its creation batch to that business
date; current chronological debt snapshots are refreshed under the firm lock.
Payment/purchase summaries and record-review amounts use the current bill/payment
amounts while activity counts continue counting each creation transaction once.

## Local preview reset on 2 October 2026

Only `D:\pharmacy_webapp-hardening\preview-20261001-135817.sqlite3` was reset.
Before deleting data, a full SQLite backup was created and integrity checked:

`D:\pharmacy_webapp-hardening\backups\preview-before-reset-20261002-220531.sqlite3`

The reset removed 5 firms, 4 representatives, 8 bills, 7 payments, 26 transaction
batches, 40 ledger entries, 10 deletion groups and 10 deletion members. This also
removed old opening entries and recoverable demo records. The eight business tables
are empty, with zero debt. Login accounts/passwords, sessions, migrations and ID
sequences were preserved. Keeping ID sequences prevents old queued firm/bill IDs
from referring to newly added records. Authentication/security tables and other
databases were not reset. Preview authentication accounts were preserved; local
login credentials are not included in this document.

To restore the backup, stop the port 8766 preview server before replacing the
preview database with the backup. Restart using the same preview database path.
Do not copy a live SQLite file while a server is writing to it.

## Verification

- 63 Django tests pass, including company/local cash edits, duplicate and stale
  edits, metadata/date changes, grouped Delete/Recover, prior independently deleted
  payments, relationships, positive amounts, login, CSRF and firm creation.
- Migration checks report no pending or newly required migration. Whitespace and
  syntax checks pass for all static ledger JavaScript and rendered inline scripts.
- Populated pages were checked in a separate preview copy with long firm/rep names,
  a long bill reference and an eight-digit amount. Browser bill editing and edited
  transaction Delete/Recover worked. Cancelling the dialog restored focus to Delete;
  Escape closed its outer menu and focused the `⋯` control.
- Tables were inspected at 390, 768, 1024, 1366 and 1920px, and at 1093/911px for
  equivalent laptop reflow at 125%/150%. Native browser zoom is not exposed by the
  in-app browser and remains a manual check. Empty primary-preview pages were
  checked after reset. The primary preview is running on port 8766.
