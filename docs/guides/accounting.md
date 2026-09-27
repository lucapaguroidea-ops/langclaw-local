# Accounting proposals (Romania)

For every filed invoice (typically imported from [e-Factura](documents.md#e-factura-romania)),
a model **proposes** the journal entry, **code decides** whether it's right, a
**person** reviews it when there's doubt, and only then is it **posted** to the
client's journal. The model never posts.

```bash
LANGCLAW__DOCUMENTS__ACCOUNTING__ENABLED=true
```

Then save the **Accounting proposal** template from the console (as
`accounting_proposal`) and, in a client's chat, ask the bot to queue the
waiting invoices (`accounting_queue`) — or schedule that daily.

## The workflow

| Step | What happens |
|---|---|
| `context` | Code gathers the facts: the invoice (parties, totals, VAT per rate, lines), the client's profile (VAT payer, VAT on collection), how this partner was booked before (from the journal), and the VAT rates valid on the invoice date. |
| `propose` | The model proposes the lines (`account`, `debit`, `credit`, `explanation`), the legal basis, a short reasoning, and a confidence. |
| `check` | Deterministic checks (below). |
| `route` | Checks failed, or confidence under 0.9 → a person; otherwise post. |
| `review` | Approve, edit the entry, or reject (→ `needs_manual_entry`). An edited entry is checked again; it only goes on once it passes. |
| `post` | `journal_post` re-runs the checks and writes the entry in one transaction; the invoice becomes `posted`. |

## The checks

- every line is a chart account (`605`, `4426`, `401.1`) and either a debit or a credit;
- the entry balances;
- a purchase credits the supplier (401/404/408/462) with the invoice total and
  debits classes 2, 3 or 6; a sale debits the customer (4111/418/461) and credits class 7;
- VAT: the invoice's VAT is booked on the right account for the client's regime —
  4426 (deductible) / 4427 (collected), 4428 when the client applies VAT on
  collection, none when the client isn't a VAT payer (the VAT is part of the cost);
- every VAT rate on the invoice was valid **on its date**
  (`langclaw/accounting/vat.py` — 19/9/5% until 31 July 2025, 21/11% after, plus
  the transitional 9% for some housing until 31 July 2026).

## The journal

`journal_entries` + `journal_lines` in the client's own schema: one entry per
document, non-negative amounts, one side per line, balance re-checked inside the
posting transaction. `partner_history` in the context comes from these entries,
so proposals get more consistent as the accountant approves them.

## Closing a month

`accounting_period_report(period="2026-09")` gives, for one client and month:

- **blockers** — invoices dated in the month that still have no entry (filed,
  needs review, deferred for manual booking);
- **trial_balance** — debit / credit turnover and balance per account, from the
  posted journal lines, and whether it balances;
- **vat** — the D300 draft figures: sales and purchases by rate, reverse charge
  (counted as both collected and deductible), collected, deductible, payable or
  refundable. Credit notes count negative.

- **documents** — which of the client's expected monthly documents are in. Set
  them on the client's profile, e.g.
  `"expected_documents": ["bank_statement", {"doc_type": "payroll", "label": "State de plată"}]`;
  a type counts as present when any document of that `doc_type` is dated in the
  month. Also lists the month's documents still marked `needs_review`.

`accounting_period_close(period, closed_by=)` refuses while there are blockers,
expected documents are missing, or the balance is off; otherwise it saves the report to
`reports/<period>/close.json` in the client's bucket and **locks** the month:
`journal_post` refuses any entry dated in it (`closed_periods` table in the
client's schema). There's no reopen tool yet — reopening is a database change
on purpose.

These are figures for the accountant to check and file, not the ANAF D300 XML;
generating the declaration file (DUKIntegrator) is a later slice.

## Bank statements and payments

Put a statement in the client's bucket (MT940 `.sta`/`.txt` or CAMT.053 `.xml`, as
exported from the bank) and call `bank_import(key)`:

1. It parses the statement — no model involved — and checks that opening +
   movements = closing. The statement is filed as a `bank_statement` document,
   dated its last day, so it satisfies `expected_documents` at month close. If the
   balances don't add up, it's filed as `needs_review`.
2. Movements go to `bank_transactions` in the client's schema. Each has a stable
   key, so importing the same file twice adds nothing.
3. Each new movement is matched to an open invoice:
   - Money in only pays sales invoices, and money out only pays purchases.
   - The amount must equal the invoice total.
   - The amount plus one more signal is a **certain** match. The signal is the
     invoice number in the description, the supplier's IBAN, or the partner's
     name. The invoice gets `paid_on` / `payment_ref` / `payment_tx`.
   - The amount alone, with only one candidate invoice, is **probable**. It's
     listed under `to_confirm` for a person to confirm with
     `bank_confirm_match(movement_key, bucket_key)`.
   - With several candidates, nothing is matched.

`bank_movements(unmatched_only=True)` lists what's still open.

## Advice: what's coming

`accounting_outlook(period, months=6)` computes, for one client, the facts to
advise on — nothing here is written by a model:

- **deadlines** — returns due after the month, all on the 25th of the next
  month: D300 and D394 (VAT payers; at quarter end when the profile has
  `"vat_period": "quarterly"`), D112 (profile `employees`), D100 (profile
  `"tax_regime": "micro"`, at quarter end).
- **thresholds** — the year's net sales against the limits that would change
  the client's regime: VAT registration (395,000 RON, for `"vat_payer": false`)
  and the micro-enterprise ceiling (250,000 EUR in 2025, 100,000 EUR from 2026;
  needs `eur_ron` on the profile). Flagged `warn` from 80%.
- **vat_trend** — net VAT per month over the last *months*, the latest against
  the average before it.
- **unbooked_invoices** — the month's invoices still without an entry.

The **monthly_advice** template (`ui/templates/monthly_advice.graph.json`) runs
it, has the model write advice that must cite these facts, and pauses for a
person to approve or edit it before it reaches the client. The limits live in
`langclaw/accounting/outlook.py:LIMITS` — reference data for your accountant
to review, like the VAT table.

## Export to SAGA / NextUp

`accounting_export(target="saga", date_from=, date_to=, again=False)` takes the
client's **posted** invoices and builds one batch, stored in the client's bucket
under `exports/<target>/<timestamp>-...` with a 24-hour download link. Exported
invoices get status `exported` and aren't exported again unless `again=True`.

- **`saga`** — a zip of SAGA C. "Import facturi XML" files
  (`<Facturi><Factura><Antet>…<Detalii><Continut><Linie>`), one
  `F_<cif>_<numar>_<dd-mm-yyyy>.xml` per invoice, in `intrari/` (the client is
  `ClientCIF`) and `iesiri/` (the client is `FurnizorCIF`). SAGA books the
  invoices itself; the langclaw journal entries aren't in the file. Invoices
  missing a number, date, CIF or lines are listed under `skipped`.
- **`nextup`** — **not implemented.** NextUp's API documentation wasn't
  available, so the target fails with a clear error instead of guessing an API
  (`langclaw/accounting/export/nextup.py` is the place to wire it).

Targets live in one registry (`langclaw/accounting/export/__init__.py:EXPORTERS`);
a new one is a class with `name` and `build(rows, own_cif) -> ExportBatch`.

## Limits

- The checks work at the level of account classes and the usual counterparts, not
  the full OMFP 1802/2014 chart: choosing between, say, 604 and 628 stays with the
  model and the reviewer.
- The VAT table is reference data to be reviewed by your accountant; update it
  when the law changes.
- Entries are single-currency (the invoice's); FX translation, fixed-asset
  depreciation, and non-invoice documents (receipts, bank statements) aren't
  covered yet.
- The SAGA file follows the published import layout but hasn't been imported
  into a real SAGA install yet — try one batch before relying on it.
- Period VAT uses the rate on each invoice's VAT breakdown; a reverse-charge
  line that carries 0% needs the rate filled in before it adds up.
- Advice covers deadlines, regime limits and the VAT trend only — cash flow,
  payments and profit forecasts need bank data that isn't imported yet.
- Bank matching is one movement to one invoice. Partial payments, one payment
  for several invoices, and FX movements stay with the accountant
  (`bank_movements`).
