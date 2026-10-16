# Monthly overage audit calculation correction

The report's purpose remains the review of the previous month's finalized usage billing. Credit availability is separate supporting context, not monthly consumption.

## Credit measures

- **Credits Granted (All-Time):** sum of positive additions in the complete posted mParticle Credit ledger through the billing close returned by Metronome. Includes historical, paid, waived, refund and rollover grants; it is not a current-contract commitment or cash amount.
- **Credits Unavailable (Used / Expired / Other):** credits granted minus remaining posted credits. Includes consumption, expiration and other reductions. Monthly invoice usage is kept separate in dry-run diagnostics.
- **Remaining Credits (at Billing Close):** Metronome historical `listEntries.ending_balance.excluding_pending` at the first day of the following month. No run-time balance substitution.
- **Credits Unavailable %:** unavailable divided by granted. An expired grant can result in 100% without full consumption or any overage.

All credit amounts stay in mParticle Credits. Only a USD-cents invoice is divided by 100. The report preserves full source precision and displays two decimals.

The complete, paginated ledger must reconcile to the API ending balance, and invoice-linked deductions must reconcile to finalized invoice credit adjustments. Unknown units, contradictory pagination or a reconciliation failure stop generation before writing a report. Negative invoices cannot be silently netted against positive invoices; duplicate invoice IDs are counted once and conflicting versions stop the run.

### Expiration at the boundary

The API can include an expiration entry at the exact `ending_before` timestamp while its ending balance excludes that expiration. Identify an expiration by the explicit "Credits expired" reason, a non-positive amount and absence of an invoice ID; a grant description mentioning expiry is not an expiration. Exclude these expiration entries exactly at the cutoff from the ledger reconciliation; retain closing invoice deductions at the cutoff. This distinction was verified across 326 historical customer-month ledger responses and is covered by a regression test. Do not apply a blanket timestamp filter to both types of entry.

## Monthly billing measures

Finalized PLAN_ARREARS invoice totals are the billing control after grants and invoice adjustments. Their usage-period start must fall within the requested month. The positive finalized amount due is the usage overage amount; do not subtract the original grant again or infer an overage from an expired balance. Unknown currency, unsupported finalized invoice types or cross-month periods, missing finalized usage invoices, any negative invoice, or drafts require review. A missing finalized invoice does not establish a zero obligation.

This establishes the Metronome billing amount, not a decision to collect from an inactive customer, recognize revenue, waive a charge, or override an agreed account exception.

## Historical migration scope

The October 16 historical correction updates only credit columns C:E and H on the existing nine monthly tabs. Invoice totals, overage amounts, invoice counts, YES/NO flags, Summary, original run dates and row order are preserved because earlier account exceptions remain in scope. Clear headers explain the credit measures; source evidence is retained outside the spreadsheet. No cell notes or comments are added. The proposed finalized billing recalculation is separate evidence, not a historical billing rewrite.

The recurring script changes require deployment before future scheduled runs use the corrected logic. Merging the script does not backfill historical tabs. Existing populated month tabs are protected by default. An intentional full-month regeneration requires `--replace-existing`; it regenerates billing fields and must not be used as a shortcut for the credit-only historical migration. Existing customer row order is preserved so manually maintained annotations stay with their customer. Removing an annotated customer, internal blank rows, or a conflicting Summary column F stops publication.

## Publication and review status

The monthly table, formatting and its Summary row are published in one atomic Sheets batch, followed by a value readback. There is no clear-before-write step. A preparation-time data change or validation/smart-chip constraint stops publication. Other Summary months and all cells beyond the managed monthly columns are left intact.

Summary adds **Rows Needing Review** in column F. A month with unresolved billing rows leaves its aggregate overage amount blank rather than displaying a partial sum as the complete total. Historical review counts stay blank until that month is deliberately regenerated; blank is not zero. New months append to Summary, preserving existing row order.

## Verification

Run `python -m unittest discover -s tests -v`. Use `python overage_audit.py --month YYYY-MM --dry-run` for read-only live-source validation without Google Sheets access.

Official API model reference: https://github.com/Metronome-Industries/metronome-node/blob/main/src/resources/v1/credit-grants.ts
