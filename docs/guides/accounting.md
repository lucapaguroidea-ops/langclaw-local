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

- **vat_settlement** — a preview of the month's VAT settlement entry, for VAT
  payers (monthly, or at quarter end with `"vat_period": "quarterly"`). The
  balances of 4426 and 4427 up to the month's last day are cleared into
  4423 (payable) or 4424 (refundable):
  4427 = 4426 + 4423, or 4427 + 4424 = 4426.
  - **VAT on collection** (`"vat_on_collection": true`): invoices book their VAT
    on 4428. Each payment booked from a bank statement or with
    `cash_pay_invoice` moves the paid share of the invoice's VAT:
    D 4428 / C 4427 for a sale, D 4426 / C 4428 for a purchase. The settlement
    then clears only the VAT that became due, and unpaid VAT stays on 4428.
  - The month report's `vat` (the D300 draft) then counts what became due in
    the month, with `"basis": "payments"`: credits to 4427 and debits to 4426.
    That includes paid invoices, Z reports and receipts. The invoice-date
    totals stay under `by_invoice`. The per-rate rows still come from the
    invoices.
  - A partner offset (`partner_offset`) moves the offset share the same way,
    invoice by invoice.

- **depreciation** — the month's depreciation entry for the client's fixed
  assets, previewed here and posted at close: D 6811 / C the
  accumulated-depreciation account of each asset (2131 → 2813, 214 → 2814,
  205 → 2805). Register assets with
  `assets_add(name, account, value, in_service, life_months)`; `assets_list(period)`
  shows this month's amounts. Depreciation is linear. It starts the month after
  the asset goes into service, is `value / life_months` rounded to the ban, and
  the last month takes the rounding.

- **year_end**: in December, the preview of the year-end closing entry. Every
  class 6 and class 7 account, including income tax, is brought to zero against
  121, which then holds the year's result (credit for a profit, debit for a loss).
  The preview already includes December's depreciation. `accounting_results`
  ignores this entry, so the P&L still shows the year after the close.

The month report also has `anomalies`: accounts whose balance at month end
(everything posted so far) is on the side it normally can't be on. Each comes
with the likely reason:

- 5311 or 512x in credit (cash paid without a receipt, or a bank overdraft);
- 581 not zero;
- 542 in credit (the company owes the employee);
- 28x or 29x in debit;
- 401 or 404 in debit (a supplier paid more than invoiced);
- 4111 in credit (a customer paid more than invoiced);
- stock or fixed-asset accounts in credit.

They're for the accountant to check. They don't block the close, except
negative cash, which the close refuses anyway.

`accounting_period_close(period, closed_by=)` refuses while there are blockers,
expected documents are missing, or the balance is off. Otherwise it posts the
depreciation, the VAT settlement and, in December, the year-end entry, dated the
last day of the month. It then saves three files in the client's bucket:

- the report, as `reports/<period>/close.json`;
- the journal register, as `registru-jurnal.csv`;
- the trial balance, as `balanta.csv`.

The two registers include the closing entries, and their keys come back under
`registers`. If either register can't be written, the month stays open.
Finally it **locks** the month:
`journal_post` refuses any entry dated in it (`closed_periods` table in the
client's schema). There's no reopen tool yet — reopening is a database change
on purpose.

`accounting_d394(period)` gives the **D394** figures (the informative statement of
domestic supplies and purchases). It covers the month's invoices with a partner
tax ID. They are grouped by partner, direction (`out` supplies, `in` purchases),
VAT rate and type (`normal` / `reverse_charge`). Each group has the invoice
count, taxable base and VAT, and credit notes count negative. The rows are also
saved as `reports/<period>/d394.csv` in the client's bucket.

These are figures for the accountant to check and file, not the ANAF D300 XML;
generating the declaration files (D300 / D394 XML for DUKIntegrator) needs the
ANAF schemas and is a later slice.

## Bank statements and payments

Put a statement in the client's bucket (MT940 `.sta`/`.txt` or CAMT.053 `.xml`, as
exported from the bank) and call `bank_import(key)`:

1. It parses the statement — no model involved — and checks that opening +
   movements = closing. The statement is filed as a `bank_statement` document,
   dated its last day, so it satisfies `expected_documents` at month close. If the
   balances don't add up, it's filed as `needs_review`.
2. Movements go to `bank_transactions` in the client's schema. Each has a stable
   key, so importing the same file twice adds nothing.
3. Each new movement is matched against open invoices by their **outstanding**
   amount (total minus what's already paid). Money in pays sales, money out
   pays purchases. The rules are tried in this order:
   1. **One invoice, certain.** The amount matches and there's a second
      signal: the invoice number in the description, the supplier's IBAN, or
      the partner's name.
   2. **Several invoices, certain.** Every invoice is named in the description
      and their amounts add up to the payment.
   3. **Several invoices, certain.** Exactly one combination of the partner's
      open invoices adds up (up to 12 invoices searched). If more than one
      combination fits, nothing is matched.
   4. **Partial.** One invoice is named in the description and it's worth more
      than the payment. The payment goes to that invoice.
   5. **Probable.** The amount alone matches a single invoice. It goes under
      `to_confirm` for a person to check with
      `bank_confirm_match(movement_key, bucket_key)`, which also accepts a
      partial amount.

   Applied payments are recorded on the invoice as `payments` (list),
   `paid_amount`, and, once nothing is left to pay, `paid_on` / `payment_ref` /
   `payment_tx`. Cash aging counts only what's still outstanding.

4. Each applied payment is **booked** as a journal entry, with no model
   involved:
   - Money in: D bank / C the customer account the invoice was booked on
     (4111 by default).
   - Money out: D the supplier account (401 by default) / C bank.

   The bank account is 5121 for RON and 5124 for foreign currency. To use
   another account for an IBAN, set it on the profile, e.g.
   `"bank_accounts": {"RO49…": "5121.01"}`.
5. An unmatched debit described as a bank fee ("comision", "taxa bancara"…) is
   booked D 627 / C bank.
6. A movement described as a cash deposit ("depunere numerar") or withdrawal
   ("retragere numerar", "ridicare numerar", ATM) goes through 581 (cash in
   transit): a deposit is D 581 / C 5311 and D bank / C 581, a withdrawal the
   reverse. It shows under `cash_transfers` and leaves the unmatched list.

A payment dated in a **closed** month is still applied to the invoice, but it
isn't booked. It's listed under `not_booked` with the reason, for the
accountant.

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
- **cash** — as of the month's last day, from invoices not yet `paid_on` (set by
  bank matching):
  - receivables and payables aged into not due / 1–30 / 31–60 / 61–90 / 90+
    days overdue, with the five largest overdue partners;
  - the bank balance, taken as the newest statement's closing balance for each IBAN;
  - what falls due in the next 30 days;
  - the balance projected over those 30 days. Overdue amounts are left out of
    the projection, because they may never be paid.

The **monthly_advice** template (`ui/templates/monthly_advice.graph.json`) runs
it, has the model write advice that must cite these facts, and pauses for a
person to approve or edit it before it reaches the client. The limits live in
`langclaw/accounting/outlook.py:LIMITS` — reference data for your accountant
to review, like the VAT table.

## Partner statements and balances

Invoices and payments both carry the partner's tax ID into the journal, so:

- `partner_statement(partner_cui, date_from, date_to)` returns the partner's
  statement (fișa partenerului). It has the opening balance, every movement on
  the partner accounts (401/404/408, 411/4111/418) with a running balance, and
  the closing balance. The balance is debit − credit, so a positive balance
  means the partner owes the client.
- `partner_balances(day)` returns every partner with an open balance on a day:
  what customers owe (41x) and what the client owes suppliers (40x). This is
  the list to send balance confirmations from.

Entries booked by hand without a partner tax ID don't appear here.

## Payment reminders

`receivables_overdue(day, min_days=7)` lists the client's customers with unpaid
sales invoices past due by at least `min_days`, largest first. Each customer
comes with its invoices, their due dates, days overdue, and what's left to pay
after partial payments.

The **payment_reminders** template (`ui/templates/payment_reminders.graph.json`)
works like this:

1. It runs `receivables_overdue`.
2. The model drafts one reminder per customer, in Romanian. Each reminder cites
   exactly those invoices, and the tone gets firmer with the delay.
3. The run pauses for a person to approve or edit the drafts.

After approval, `reminders_file` files each reminder:

- The text goes into the client's bucket under `reminders/<date>/`, as a
  `payment_reminder` document.
- Every invoice it cites gets the date added to its `reminders` history
  (`reminded_on` holds the latest).

On the next run, `receivables_overdue` shows `reminders_sent` and
`last_reminder` per invoice, and the model escalates: first notice, second
reminder, final notice.

Customer emails come from the e-Factura invoices. The UBL parser reads each
party's `cac:Contact/cbc:ElectronicMail`, and sync stores it as `customer_email`
/ `supplier_email`. `receivables_overdue` shows the email per customer.

When Gmail is connected with write access (`LANGCLAW__TOOLS__GMAIL__ENABLED=true`,
`…__READONLY=false`), `reminders_file` also creates a **Gmail draft** per
reminder, addressed to the customer. A person still presses send. Customers
without an email are filed and reported with `"draft": "no email address for
this customer"`. Without Gmail, reminders are only filed.

## Cash register (raport Z)

`cash_z_report(day, lines)` books a day's Z report from its gross sales per VAT
rate, e.g. `[{"rate": 21, "gross": 1210}]`. The entry is:

- D 5311 for the total;
- C the revenue account for the net, i.e. the profile's `cash_revenue_account`
  (707 by default, 704 for services);
- C 4427 for the VAT, worked out per rate from the gross.

Rates must be valid on the day, and a day can be booked only once. A closed
month refuses it.

The report is filed as a `z_report` document. It counts towards
`expected_documents`, e.g. `["z_report"]`, and its VAT goes into the month's
VAT summary (the D300 draft). It doesn't go into D394, which lists invoices
with a partner tax ID. Cash deposited at or withdrawn from the bank is booked
through 581 by `bank_import` (see the bank section).

### Invoices paid in cash

`cash_pay_invoice(bucket_key, amount, day, document)` records an invoice paid
or collected in cash, with the chitanță or dispoziție de plată number:

- a supplier invoice is booked D 401 / C 5311, and a sale D 5311 / C 4111
  (using the partner account the invoice was booked on);
- if `amount` is empty, it pays whatever is left, and it refuses more than
  that;
- the invoice's payments are updated, and `paid_on` is set once nothing is
  left;
- the same document can't be booked twice.

If the profile sets `cash_payment_limit`, a warning is returned when the cash
paid to or received from one partner on one day goes above it. The check adds up
all of that partner's invoices, using their tax ID. No legal limit is built in,
so set the one that applies to the client.

### Cash receipts without an invoice (bon fiscal)

`cash_receipt(day, amount, account, vat_rate, document, description,
deduct_vat)` books a purchase paid in cash with only a receipt, such as fuel or
small supplies:

- D the cost account (6xx expense, 3xx stock or 2xx asset);
- D 4426 for the VAT;
- C 5311.

The VAT is deducted only when the client is a VAT payer and `deduct_vat` is
true. Set it to false when the receipt doesn't show the client's tax ID; the
VAT then stays in the cost. The rate must be valid on the day, and a receipt
number can be booked once.

The receipt is filed as a `cash_receipt` document, and its deductible VAT goes
into the month's VAT summary. Whether a particular receipt qualifies for
deduction is left to the accountant.

### Employee cash advances (avans de trezorerie, 542)

- `cash_advance(day, amount, employee, document)` gives an employee cash:
  D 542 / C 5311.
- `cash_receipt(..., employee="Ana Pop")` books a receipt paid from that
  advance: C 542 instead of 5311.
- `cash_advance(..., returned=true)` takes back what the employee didn't spend:
  D 5311 / C 542. It refuses more than the employee still has open.
- `advances_open(day)` lists what each employee still has to settle.
  - A negative figure means they spent more than they were given, so the
    company owes them.

Employees are identified by the name as written, so use the same spelling
every time.

### Cash book (registru de casă)

`cash_book(period)` reads 5311 back from the journal. It gives the opening
balance, then for each day the receipts, payments, closing balance and the
entries behind them, and finally the month's closing balance.

`problems` lists each day where:

- the cash went **negative**, which usually means a receipt is missing or was
  booked late;
- the cash was above the profile's `cash_limit`, if one is set. Nothing is
  checked by default.

The month report (`accounting_period_report`) has a `cash` section with:

- the opening and closing cash;
- the same problems;
- `open_advances`, the employees' unsettled 542 advances at month end.

`accounting_period_close` refuses a month in which the cash went negative. Book
the missing Z reports or receipts first. Days above `cash_limit` and open
advances are reported but don't block the close.

## Paying suppliers

`payables_due(day, days=7)` lists the supplier invoices to pay: unpaid, and due
within `days`. Overdue ones are included and flagged. The list is grouped by
supplier and shows the IBAN from the invoice, what's left after partial
payments, and the invoice numbers.

`payables_batch(day, days)` writes those payments as a CSV in the client's bucket
at `payments/<date>-batch.csv`, and files it as a `payment_batch` document. It
returns a 24-hour download link. The columns are `beneficiary; tax_id; iban;
amount; currency; details`, where the details read "Plata fact. …".

Suppliers without an IBAN are left out and listed under `missing_iban`. Nothing
is paid from langclaw. The accountant uploads the file to internet banking, or
copies it into the bank's own import format, since each bank's format differs.
The payments are booked when the next statement is imported.

## Results and income tax

`accounting_results(period)` computes the profit and loss for the month and the
year to date from the journal:

- **Revenue:** class 7, so 709 discounts reduce it.
- **Expenses:** class 6, without the income-tax accounts 691/697/698.
- **Result:** revenue minus expenses, plus the income tax already booked.

It also returns an **income-tax estimate** for the year so far:

- **Micro-enterprise** (`"tax_regime": "micro"`): revenue × `micro_rate` (1% by
  default; set 3% on the profile when it applies).
- **Profit tax:** 16% of a positive result.

It's a planning figure only. Non-deductible expenses, loss carry-forward,
sponsorship credits and micro revenue exclusions stay with the accountant.

`accounting_outlook` includes it as `results_ytd`, so the monthly advice can talk
about the year's result and the tax to set aside.

## The monthly loop

The **accounting_month** template (`ui/templates/accounting_month.graph.json`)
does a client's month in one run:

1. `efactura_sync` imports new invoices from SPV.
2. `accounting_queue` starts `accounting_proposal` for every filed invoice
   without an entry. These runs go on in parallel and ask for review where they
   should.
3. `accounting_period_report` produces the month's report: blockers, expected
   documents, the trial balance and VAT.
4. `accounting_outlook` produces the outlook: deadlines, limits and cash.
5. The model drafts a status for the accountant and advice for the client.
6. The run pauses for a person to approve or edit.

With an empty `period` it works on **last month**. The period tools take `""`
too (`resolve_period`), so a schedule never needs updating.

To run it every month, create the workflow from the template in the console
(New workflow → template). Then, **in the client's chat**, ask for it to be
scheduled, e.g. "run accounting_month on the 1st of every month at 08:00". The
`cron` tool stores `workflow_name`, and the run belongs to the client linked to
that chat.

Because the queued proposals run alongside the loop, the month report is taken
when they start. Its blockers include the invoices just queued. Run the report
again, or open the console's Client overview, once they're reviewed.

## Correcting a posted entry (stornare)

`journal_reverse(bucket_key, reason, day="")` undoes a wrong entry without
deleting anything:

- It posts the same lines with debit and credit swapped, dated `day`, under
  `reverse/<n>/<key>`. That month must be open.
- It moves the original to `<key>#reversed-<n>`. Both entries stay in the
  journal and the registers.
- The document goes back to status `reversed`, so it shows as a blocker until
  the correct entry is posted with `journal_post`.
- A reason is required, and it is written into the journal.

## Balance confirmations (confirmări de sold)

`partner_confirmations(day)` writes one letter per partner with an open balance
on `day`, usually 31 December. Each letter, in Romanian, states what the partner
owes the client and what the client owes them, and asks the partner to confirm
or send the differences. Letters are filed as `confirmations/<day>/<cui>.txt`.

With Gmail connected, it also creates an email draft to each partner, using the
address from their invoices. Nothing is sent. Partners without an email are
listed with `"draft": "no email address for this partner"`.

## Offsetting a partner (compensare)

When a partner both owes the client (41x) and is owed by them (40x),
`partner_offset(partner_cui, day, amount="")` nets the two:

- It posts D 401 / C 4111 for the smaller balance, or for `amount` if given
  (no more than that), under `offset/<day>/<cui>`, once per partner and day.
- It applies the same amount to the partner's open invoices on both sides,
  oldest first. The invoices then read as paid, and reminders and payment
  batches skip them.
- If the partner has nothing to offset, it says so.

The offset entry appears in the journal register's non-invoice file. The
signed confirmation (proces-verbal de compensare) stays with the accountant.
Only the first 200 invoices of each kind are searched.

## In the console

The **Client overview** page shows the chosen client and month. It uses
`GET /v1/accounting/overview`, which runs the same tools as the agent.

- **Alerts:** invoices without an entry, missing documents, limits close to being
  crossed, overdue receivables, unmatched bank movements, days with negative
  cash (the month can't close), days above `cash_limit`, and open employee
  advances.
- **Close tab:** the VAT position, the expected documents, any balances on the
  wrong side, and the trial balance.
- **Outlook tab:** deadlines, limits, the bank balance, the 30-day projection
  and aging.
- **Results tab:** the month's and the year-to-date profit and loss, plus the
  income-tax estimate.
- **Partners tab:** open partner balances on the month's last day.
- **Bank tab:** the open movements.
- **Cash tab:** opening and closing cash, the problem days, the cash book day
  by day and the open employee advances.
- **Files tab:** what's saved under `reports/<period>/`, with 24-hour download
  links. That's the close report, the journal register and the trial balance
  from the close, plus any ledgers or D394 draft made that month. The same list
  comes from the `accounting_reports(period)` tool.

The page is read-only. Posting, closing a month and confirming a match happen
in chat or in workflows.

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

### Journal register (registrul-jurnal)

`accounting_journal_register(period, without_invoices=False)` writes every
posted entry of the month as a CSV to
`reports/<period>/registru-jurnal.csv`, with a 24-hour link. There is one row
per line: `nr;date;document;explanation;account;debit;credit`. The result gives
the entry count, the debit and credit totals, and whether they balance.

With `without_invoices=True`, the file is `registru-jurnal-other.csv` and holds
only the entries langclaw made itself: bank (`bank/`), cash (`cash/`) and month
close (`close/`). SAGA's invoice import doesn't carry these, so the accountant
enters them in SAGA as *note contabile*. There is no direct SAGA import for them
yet, because the note-contabile import format wasn't available.

### Trial balance (balanța de verificare)

`accounting_trial_balance(period)` gives each account's five column pairs, each
split into debit and credit:

- the opening balance at 1 January;
- turnover earlier in the year;
- the month's turnover;
- total sums;
- the closing balance.

Balances go on their debit or credit side. `balanced` checks that every pair's
debit and credit totals agree. The sheet is saved as a CSV at
`reports/<period>/balanta.csv`, with a 24-hour link. The month report's
`trial_balance` still gives only the month's turnover.

The opening balance comes from everything posted before 1 January. For a
client whose earlier years aren't in langclaw, post their balances once with
`accounting_opening_balances(day, balances)`:

- Date it the day before the first month kept here, e.g. `2025-12-31`.
- Give the balances as debit minus credit, so credit balances are negative,
  e.g. `{"5121": 1000, "1012": -800, "401": -200}`.
- The balances must sum to 0, and zero balances are skipped.
- It is posted once, as `opening/<day>`, and appears in the "other" journal
  register next to the bank, cash and close entries.

### Account ledger (fișa contului)

`accounting_account_ledger(account, period)` shows one account for the month.
It covers the account's analytic sub-accounts too, so 5121 includes 5121.01.

- It gives the opening balance, then each posted line with its `counterpart`
  (the entry's accounts on the other side, e.g. `704,4427` for a sale on
  4111) and a running balance, then the totals and the closing balance.
- Balances are debit minus credit, so a negative figure is a credit balance,
  as usual for 401 or 4427. `side` says which it is.
- The ledger is saved as a CSV at `reports/<period>/fisa-<account>.csv`, with a
  24-hour link.

Targets live in one registry (`langclaw/accounting/export/__init__.py:EXPORTERS`);
a new one is a class with `name` and `build(rows, own_cif) -> ExportBatch`.

## Limits

- The checks work at the level of account classes and the usual counterparts, not
  the full OMFP 1802/2014 chart: choosing between, say, 604 and 628 stays with the
  model and the reviewer.
- The VAT table is reference data to be reviewed by your accountant; update it
  when the law changes.
- Entries are single-currency (the invoice's). FX translation isn't covered
  yet.
- The SAGA file follows the published import layout but hasn't been imported
  into a real SAGA install yet — try one batch before relying on it.
- Period VAT uses the rate on each invoice's VAT breakdown; a reverse-charge
  line that carries 0% needs the rate filled in before it adds up.
- Cash figures are only as complete as the imported statements and the
  invoices' due dates; an invoice without a due date counts as not due.
- Bank matching doesn't handle foreign-currency movements, fees netted out of a
  payment, or partial payments that don't name the invoice. Those stay in
  `bank_movements` for the accountant.
- Fixed assets depreciate linearly only. Disposals, revaluations, degressive or
  accelerated methods, and assets bought in a closed month stay with the
  accountant.
- The year-end entry assumes the financial year is the calendar year. The profit
  distribution (129 / 1061 / 117) stays with the accountant.
