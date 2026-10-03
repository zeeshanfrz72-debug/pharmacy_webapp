# Aligned headers and compact page views

## Interface

- Table headers use one English/Urdu pair, aligned vertically in a 36px row.
  Desktop English is 14px/800 and Noori Nastaleeq is 18px. Compact table words
  retain the complete bilingual wording in their accessible names and tooltips.
  Full form labels and recorded data remain unchanged.
- The header is 64px high and shows both the original portrait and logo with
  their original proportions. The desktop sidebar is 200px, with Reps as its
  shorter navigation label. Desktop gutters are 12px; panels and gaps are smaller.
- Add forms remain visible. Firms and Reps use one desktop form row; Bills uses
  two, with a one-row resizable Notes field and expandable Help. History places
  its heading, date filters and Trash link together above debt and records.
- Default Firms, Reps, Bills and History views show active records. Their Trash
  buttons open `?view=trash`, which shows only the page's deleted records. The
  Back link preserves filters, and recovery stays in the filtered Trash view.
  Legacy `#page-trash` links navigate to the equivalent query-string view.
  The dedicated shared Trash page remains available.
- Ordinary desktop data rows remain approximately 36px. Long stored values wrap
  completely. Supplementary information remains available in keyboard-accessible
  Details on narrow layouts, including the record-review evidence tables.

## Verification

The populated fixture and empty-state fixture use separate SQLite copies under
`%TEMP%\pharmacy-density-qa`. Neither adds records to the cleared user preview.
The populated fixture includes ordinary records, long firm/representative names,
a long bill reference, an eight-digit amount and recoverable records.

At 1366×768, with normal Add forms visible and Analytics collapsed:

| Page | Fully visible ordinary rows |
| --- | ---: |
| Dashboard | 8 |
| Firms | 11 |
| History | 13 |
| Bills | 9 |
| Reps | 9 |

All table headers in these views measure 36px, with English and Urdu span centres
aligned. Populated and empty views were inspected at 390, 768, 1024, 1366 and
1920px. Populated records and Trash tables have no page/table horizontal overflow
or overflowing header labels. Portraits remain visible at every tested width.
1093px and 911px cover equivalent laptop reflow at 125% and 150%; native browser
zoom is not exposed by the in-app browser and remains a manual check.

Keyboard checks cover firm expansion, transaction menus, Delete/Recover dialog
focus, Escape, legacy Trash navigation and filter-preserving Back links. A browser
recovery in the isolated fixture leaves the user in the same filtered Trash view.
Incomplete representative submission returns visible validation errors rather
than raising a missing-firm exception.

- 69 Django tests pass, including the six page-view/header regressions.
- All ledger static JavaScript and 54 rendered inline scripts pass Node syntax
  checks across 12 page views.
- Migration checks report no changes or unapplied migrations; whitespace checks
  pass. This change adds no migration and performs no database reset.
- The cleared preview's eight business tables remain empty. Its server remains
  available at `http://127.0.0.1:8766/ledger/add-transaction/`.
