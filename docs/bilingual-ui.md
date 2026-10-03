# English–Urdu interface

Essential interface text is always bilingual. English is on the left and Urdu on
the right. The application and table column order remain left-to-right. Native
options use `English — اردو` because separate columns are unavailable there.

`ledger/bilingual.py` owns the glossary, debt terminology aliases, parameterized
messages and standard validation translations. `ledger/templatetags/bilingual.py`
renders labels, form fields, errors and the escaped browser catalog.
`ledger/static/ledger/bilingual.js` uses that same catalog for refreshed history,
dialogs, source choices, receipts, review content and offline status.

Stored names, references, notes and reasons are never searched or automatically
translated. Template escaping and JavaScript `textContent` protect interpolated
values. Parameterized values have directional isolates. Unknown messages remain
readable in English rather than receiving an invented translation.

Visible net-balance labels now say **Total Debt / کُل قرضہ**. Remaining, previous
and transaction debt use the approved glossary. Supplier Credits remains separate.
No database field, JSON key, posted value, service or calculation was renamed or
changed for this work. Existing indefinite, immediate per-page Trash behavior is
preserved.

## Typography and font provenance

Urdu uses **Jameel Noori Nastaleeq Regular**: 18px labels, 16px compact actions
and summary labels, and 24px headings. Urdu remains regular weight with no letter
spacing or forced uppercase. English retains its current font and size. Long
labels wrap within their language column; tables scroll horizontally.

The original 10,784,980-byte TTF is self-hosted unchanged at
`ledger/static/ledger/fonts/JameelNooriNastaleeq.ttf`. Its identity is Jameel Noori
Nastaleeq, Regular, Version 1 (2008), designer Urdu Lover. SHA-256:
`39c54f1646a6a4f68408f3a26400e457cb1e52226c284d8c4ab36a3363520e0f`.

The font's copyright/license strings say **“This Font Is Free Of Charge For Urdu
Lovers”**. Its OS/2 embedding flags are `fsType=4` (Preview & Print). Those notices
are retained in `fonts/NOTICE.txt`. The upstream repository's MIT license is not
treated as the font's license. The file was not converted or subsetted.

This local preview uses the font for display labels. The inspected metadata does
**not establish an express commercial web-redistribution grant**. Obtain an
express grant from the font rights holder before public/commercial distribution.
No production deployment was performed.

`font-display: swap` keeps English controls available while the font loads or if
loading fails. The service worker precaches the font on installation and caches
same-origin static requests. A failed font precache does not block installation.
Pages and authenticated API responses are excluded from this static cache.

## Verification performed

- All 55 Django tests pass, including seven bilingual regressions. They verify
  escaped interpolation, language direction, unchanged native option values and
  stored names, linked form fields, readable validation, duplicate saves,
  Delete/Recover accounting, negative debt and Supplier Credits.
- Django system, pending-migration and migration-generation checks pass against
  an isolated SQLite backup of the preview database. No migration was added for
  bilingual text.
- Static JavaScript and server-rendered inline JavaScript pass Node syntax checks.
- A Node VM harness checked bilingual rendering/errors and service-worker initial
  font precaching, repeat requests, cached offline availability, uncached failure,
  and failed precaching without blocking the English renderer. These are simulated
  network checks, not a browser offline-mode test.
- Browser layout checks covered Dashboard, Firms, Representatives, Bills, History
  and Trash at 390, 768, 1024, 1366 and 1920px. No page-wide horizontal overflow,
  hidden table headers or overlapping label pairs were found. Populated forms,
  tables, Trash, firm expansion and dashboard history were visually inspected at
  phone and desktop widths; dashboard also at tablet widths.
- The Noori font loaded successfully and joined Urdu correctly in the preview.
  The Delete dialog opens with Enter, focuses the reason field and restores focus
  after Escape. Review and history loaded dynamically with bilingual headings.
- Whitespace checks pass. Existing unrelated line-ending notices remain.

Native 125% and 150% browser zoom is not exposed by the in-app browser controls.
Equivalent viewport reflow checks supplement the width tests; actual native zoom
still needs a manual check before release. Browser offline mode and visual font
failure also remain manual checks; their cache/error paths were checked in the
isolated JavaScript harness.

## Vocabulary references

- [State Bank payment-system information in Urdu](https://www.sbp.org.pk/ur/our-operations/payment-systems)
- [State Bank Urdu credit explanations](https://www.sbp.org.pk/ur/faqs/faqs-credit-information-bureau-cib)
- [W3C direction and bidirectional text](https://www.w3.org/International/questions/qa-html-dir)
- [CLE Nastaleeq font study](https://cle.org.pk/Publication/papers/2011/HLTD201103.pdf)
- [Exact font download source](https://github.com/mrafsarnoori/Urdu-Nastaleeq-Font/blob/main/Jameel-Noori-Nastaleeq-Regular.ttf)

The CLE study is typography background; it is not used as a license grant.
