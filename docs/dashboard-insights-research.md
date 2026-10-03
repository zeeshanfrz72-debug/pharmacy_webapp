# Dashboard insights from the pharmacy ledger

The dashboard is primarily an entry workspace. Keep transaction entry and recent saves visible; put investigation behind expandable summaries. IBM's [data-table guidance](https://www.carbondesignsystem.com/building-blocks/core/components/data-table/guidelines) supports expandable rows for supplementary information and deferring detail queries until needed. Microsoft's [vendor payments workspace](https://learn.microsoft.com/en-us/dynamics365/finance/accounts-payable/vendor-payments-workspace) separates operational work from analytics. These are design references, not requirements to copy their interfaces.

## Reliable foundation

Available data includes firms and source types, representatives, bill dates/numbers/amounts, payments with optional bill links, original transaction batches, opening balances, and timestamped reversal/restoration events. There are no invoice due dates, payment terms, inventory lines, sales, or product costs.

Use two distinct calculations:

- **Activity:** non-deleted original bill/payment entries whose transaction is currently active. A combined bill/payment save counts as one transaction. Audit offsets never count as new purchasing or payment activity.
- **Balance:** all non-deleted accounting entries, including reversal/restoration offsets and opening balances. Firm soft deletion does not remove its liabilities. Separate positive balances (amount owed) from negative balances (supplier credits); their difference is the net balance.

Use Decimal for money, Asia/Karachi calendar periods, Monday-start weeks, business dates for activity, and actual timestamps for audit activity. Legacy migration created batches per ledger entry: old records may not preserve the original combined-save boundary. Show the recorded batch count rather than inventing a grouping.

## First release: explainable record review

| Signal | Calculation and evidence | What it can reveal | Limits |
| --- | --- | --- | --- |
| Possible duplicate bills | Group available bills by firm, trimmed case-insensitive number, bill date, and exact amount. Display every member with its ID. | Accidental repeat entry or duplicate supplier documents. | Matching documents can be legitimate. No automatic blocking, deletion, or fraud claim. |
| Unusually large bill/payment | Review the last 30 calendar days. For each entry, compare its amount with 3× the median of that firm's active records of the same type in the preceding 90 calendar days. Require at least 10 preceding records. | A typing error, unusual purchase, or unusually large settlement worth checking. | Three times the median is an explicit product heuristic, not a validated risk score. Same-day records are excluded from the baseline. Business seasonality and debt settlements can explain large amounts. |
| Deletion/recovery activity | Count reversal and restoration events timestamped within the last 30 calendar days; separately count distinct original batches affected. Show firm, reason, transaction, and time. | Repeated corrections, entry workflow difficulties, or records needing document verification. | One grouped deletion produces an event for every affected batch. Repeat cycles increase event counts, not distinct transaction counts. It is not an employee-performance measure. |

Example: ten earlier bills with a median of PKR 2,000 make a PKR 6,000 bill ordinary under this rule and a PKR 6,001 bill a review suggestion. With only nine preceding bills, show insufficient history.

SAP's [duplicate-invoice documentation](https://help.sap.com/docs/SUPPORT_CONTENT/fiaccounting/3361878522.html) compares supplier, reference, date, and amount among its criteria. This app has one accounting context and no currency field; its smaller rule uses only the data actually recorded.

## Ranked next opportunities

| Priority | Insight | Formula / presentation | Interpretation and limits |
| --- | --- | --- | --- |
| 1 | Supplier debt concentration | Each firm's positive current balance ÷ total positive balances; show top five and their combined share, with credits separately. | Reveals where obligations are concentrated. For example, a top-five share of 80% directs document reconciliation toward those firms. It does not establish supplier reliability. Include firms in Trash because their balances remain effective. |
| 2 | Purchases versus payments | Sum active bill increases and active payment decreases by period/firm. Show both and their difference, alongside balance movement. | Purchases consistently exceeding payments indicate growing recorded obligations. Difference is not profit or bank cash flow; opening balances and adjustments explain differences from total balance movement. |
| 3 | Source mix and activity | Bill totals by source type ÷ all bill totals; distinct active transaction batches by firm/day/week/month. | Reveals sourcing dependence and changes in transaction workload. Amount and count must be separate: many small saves differ from a few large purchases. Representative involvement shows recorded activity, not effectiveness. |
| 4 | Repeated corrections and document matches | Distinct originals corrected ÷ originals created in the same creation cohort, plus raw event counts. | Helps identify recurring record-quality issues. Cohort and event date must be explicit; dividing recent correction events by unrelated recent saves is misleading. |
| 5 | Unallocated payments | Sum active payments without a bill link, with count and share of payments. | Reveals how much payment activity cannot even be associated with a bill. A link alone does not prove complete settlement allocation; do not infer unpaid bill balances from it. |

For trends, compare complete comparable periods or matched elapsed periods. Do not compare a partial current month to a full prior month without labeling the difference. If the comparison denominator is zero, show the amounts rather than an undefined percentage. Pair every signal with links to the underlying records and an empty/insufficient-history state.

## Additional data needed

Microsoft's [accounts payable analytics](https://learn.microsoft.com/en-us/dynamics365/business-central/payables-reports) describes vendor balance analysis, top-vendor reports, payment practices, and aging by document/due/posting dates. Applying these to this ledger requires care:

- **Overdue bills and days-to-pay:** add due dates/payment terms and explicit allocation of each payment or credit to bill amounts. Do not assume FIFO settlement. Document age can be measured now, but document age is not overdue age.
- **Payment forecasting:** needs due schedules and reliable settlement allocations; cash availability also needs bank/cash-account data.
- **Profit, medicine margins, inventory turnover, and stockouts:** need item-level purchases, sales, costs, quantities, and stock movements.
- **Supplier delivery performance:** needs order and delivery dates, quantities, and returns.

No external analytics service or model is necessary for the first release. Keep calculations read-only and transparent, measure query/runtime cost as records grow, and avoid persisting derived scores before the rules have proved useful.
