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

## Limits

- The checks work at the level of account classes and the usual counterparts, not
  the full OMFP 1802/2014 chart: choosing between, say, 604 and 628 stays with the
  model and the reviewer.
- The VAT table is reference data to be reviewed by your accountant; update it
  when the law changes.
- Entries are single-currency (the invoice's); FX translation, fixed-asset
  depreciation, and non-invoice documents (receipts, bank statements) aren't
  covered yet.
