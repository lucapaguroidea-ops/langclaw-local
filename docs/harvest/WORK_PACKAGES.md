# Work packages

Read [`README.md`](README.md) §0 (the protocol) first. WPs are listed in execution order. Each one
has these parts:

- a status line;
- **Why**: evidence, as a workspace alias and a client-neutral path;
- **Langclaw today**: verified `file:line` on HEAD `9441429`;
- **Design**: the public surface first;
- **Steps**;
- **Tests**: red first, with synthetic data;
- **Docs**;
- **Risks**;
- **Done when**.

Reference specs live in [`CATALOGUES.md`](CATALOGUES.md) (§B method, §C formats, §D rules, §E law,
§F code, §G scenarios, §H workflows, §I failure modes).

Conventions used throughout:

- `T:` = `langclaw/accounting/tools.py`, `J:` = `langclaw/accounting/journal.py`,
  `P:` = `langclaw/accounting/period.py`.
- Money is `Decimal`. Tools return `{"error": ...}` and never raise.
- New tools are closures in `build_accounting_tools`. They are wrapped in `except _ERRORS` (T:94) and
  appended to `fns` (T:2021).
- A read-only tool the client may see is added to `CLIENT_TOOLS` (`langclaw/accounting/roles.py`), and
  `tests/test_roles.py` pins that list.
- A new per-client table uses its own `_ready` key suffix, is added to `ARCHIVED_TABLES`
  (`langclaw/accounting/archive.py:27-35`), and its tests clear `Journal._ready` after a schema drop.

---

## Phase A: live bugs

### WP-01 · One VAT-regime resolver; stop netting non-payer reverse charge

`status: todo · size: S · depends: — · decision: —`

**Why.**
- Y1 `2025/VERIFICARE_TVA_2025.md` §2, §5–6 found a non-VAT payer whose software ran payer mechanics
  for a year. The self-assessed reverse-charge VAT vanished in a monthly 4427/4426 "close"
  (CATALOGUES §I-01).
- langclaw reproduces that result in its reporting. It also reads `vat_payer` with contradictory
  defaults, so a profile without the key is a payer at check time and a non-payer at close (§I-02).

**Langclaw today.**
- `P:197-241` `vat_summary` adds the VAT of a category-AE purchase to **both** `collected` and
  `deductible` (P:212-216, P:229-230), whatever the client's regime. A non-payer's reverse-charge VAT
  therefore nets to 0.
- The month report always shows this D300-shaped figure (T:484, T:530-534).
- `vat_payer` defaults:
  - `True` in `checks.py:88` and T:1949;
  - falsy in `P:326` (`settles_vat`) and `outlook.py:64` (`deadlines`);
  - `thresholds` treats only an explicit `False` as a non-payer (`outlook.py:78`);
  - the console checkbox defaults to unchecked (`ui/app.py:1202`).

**Design.**
- New pure function `langclaw/accounting/regime.py: vat_regime(profile) -> VatRegime`. Its fields:
  - `payer: bool`;
  - `period: "monthly" | "quarterly"`;
  - `on_collection: bool`;
  - `source: "profile" | "missing"`.

  It raises `ProfileError(ValueError)` when `vat_payer` is absent. `checks.check_proposal`,
  `period.settles_vat`, `outlook.deadlines`, `outlook.thresholds` and the `cash_receipt` deduction
  (T:1949) all call it instead of reading the key themselves.
- `vat_summary(invoices, *, payer: bool = True)`:
  - A **payer** keeps today's output.
  - A **non-payer** gets `collected` and `deductible` as 0 for AE lines, plus a new block
    `reverse_charge_due: {taxable, vat, form: "D301" [de confirmat]}`, and
    `payable = reverse_charge_due.vat`.
  - Both regimes get a `regime` field in the output.
- `_period_report` passes `payer=vat_regime(profile).payer`. When the regime is missing, it returns
  the `{"error": ...}` "set vat_payer on the client (Clients page)".

**Steps.**
1. Write the red tests.
2. Add `regime.py`.
3. Replace the five direct reads.
4. Make `vat_summary` regime-aware.
5. In `_period_report`, emit `vat.regime` and pass the regime through.
6. Console: `ui/app.py` profile form. The `vat_payer` control needs a tri-state (unset / yes / no),
   because a checkbox cannot mean "unset".

**Tests** (pure, in `tests/test_period.py` and `tests/test_checks.py` or their current home).
- Given profile `{}`, when `vat_regime` runs, then `ProfileError`. When `check_proposal` and
  `settles_vat` run, then both surface the same error.
- Given `{vat_payer: false}` and one AE purchase (taxable 100, rate 21), when `vat_summary(...,
  payer=False)` runs, then `deductible == 0`, `reverse_charge_due.vat == 21` and `payable == 21`.
- Given `{vat_payer: true}` and the same invoice, then today's output is unchanged: collected ==
  deductible, payable 0. This test guards against a regression.
- Postgres test: a tenant with no `vat_payer` gets an error from `accounting_period_report` naming the
  key.

**Docs.**
- `docs/guides/accounting.md` "The checks" and "Closing a month": the regime is required, and
  non-payer reverse charge shows as due.
- Fix the stale Limits sentence about profit distribution (`docs/guides/accounting.md:752-753`,
  already handled by `accounting_result_carry`).
- `CLAUDE.md`: add `regime.py` to the Accounting row.

**Risks / [de confirmat].**
- The form name (D301) and the article are `[de confirmat]`; they come from the law pack once WP-06
  lands.
- Existing tenants without the key start getting errors: mention this in the PR and give a one-line
  fix.
- The posting side (the 4-line template) is WP-16, not this WP.

**Done when.**
- The five call sites use one resolver.
- A non-payer's AE VAT shows as due, not netted.
- A missing key is an explicit error everywhere.

---

### WP-02 · Every journal entry needs a date

`status: todo · size: S · depends: — · decision: —`

**Why.** An entry without a date is never checked against `closed_periods`. It also drops out of
every date-ranged register and report, so the books silently disagree with the entry list (§I-04).

**Langclaw today.**
- `J:25` `entry_date DATE` is nullable.
- `J:117-123` checks the lock only `if entry_date is not None`.
- The `BETWEEN` queries skip NULLs (J:219, J:258, J:293).
- `journal_post` never requires a date (T:232-238).
- `check_proposal` skips the rate check without a date (`checks.py:140-141`).

**Design.**
- `Journal.post` raises `JournalError("An entry needs a date (document_date).")` when `_date(...)` is
  None.
- `check_proposal` adds the problem "The invoice has no issue date: the VAT rate can't be checked."
- Migration: before `ALTER TABLE … ALTER COLUMN entry_date SET NOT NULL`, count the NULL rows. If there
  are any, raise a clear `JournalError` naming the keys, so nothing is changed silently. The ALTER goes
  in `_TABLES` guarded by a `DO $$ … $$` block that only runs when no NULLs exist.

**Steps.**
1. Red tests.
2. Guard in `post`.
3. Problem text in `check_proposal`.
4. Guarded migration.

**Tests.**
- Given a closed 2026-03 and a document without `document_date`, when `Journal.post` runs, then
  `JournalError` "needs a date".
- Given an invoice without an issue date, when `accounting_check` runs, then `ok: false` with the
  date problem.

**Docs.** Guide "The journal": entries are always dated.

**Risks.** Tenants may hold legacy NULL rows. The migration must report them, not fail obscurely.

**Done when.** No entry can be posted without a date, and the column is NOT NULL on clean schemas.

---

### WP-03 · Honest bank reconciliation for non-RON accounts

`status: todo · size: S · depends: — · decision: —`

**Why.**
- C4 found a USD account unknown to the books, hidden inside the RON total of 5124.
- C1's review states the rule "reconcile which balance is right first, then revalue" (§D-05, §I-06,
  §G-17/G-18).

**Langclaw today.**
- `bank_account` maps every non-RON IBAN to one `5124` unless `profile.bank_accounts` maps it
  (`langclaw/accounting/bank/booking.py:29-34`).
- `_bank_check` compares the statement's closing balance (in the statement's currency) with
  `balance_until(day, account)` (the ledger in RON), at T:436-445. The close refuses on any
  disagreement (T:591-596).
- The guide admits FX isn't covered (`docs/guides/accounting.md:738-739`, `746-748`).

**Design (a guard until WP-25).**
- `reconcile_account(..., currency: str = "RON")`: for a non-RON statement, return
  `{"status": "not_comparable_currency", "agrees": None, "hint": "…FX not supported yet (WP-25)…"}`
  and no difference.
- `_bank_check`:
  - collect non-RON IBANs that have no `profile.bank_accounts` mapping, and IBANs whose mapped account
    is shared by two currencies;
  - report `bank.unmapped` / `bank.shared_account`;
  - `agrees` is False when either list is non-empty.
- The close gate: "map each foreign-currency IBAN to its own analytic (e.g. 5124.01) in
  profile.bank_accounts". A non-comparable account does **not** block the close by itself: it shows as
  a warning, until WP-25 makes it comparable.

**Steps.**
1. Red tests.
2. `reconcile_account` currency parameter.
3. `_bank_check` lists.
4. Close message.
5. Console alert in `ui/editor.py:overview_alerts`.

**Tests.**
- Given an EUR statement closing at 100 and a ledger 5124 of 500, when `reconcile_account` runs, then
  status `not_comparable_currency` (not "disagrees by 400").
- Given EUR and USD statements with no mapping, when the period report runs, then
  `bank.shared_account` names 5124 and both IBANs, and the close refuses with the mapping message.

**Docs.** Guide "Bank statements", the Limits line on FX.

**Risks.** Tenants with one EUR account and no mapping get a new blocker. That is intended, and the
error text says how to fix it.

**Done when.** No non-RON statement is ever reported as agreeing or disagreeing with a RON ledger
figure.

---

### WP-04 · Stop booking foreign-currency bank movements as RON

`status: todo · size: S · depends: WP-03 · decision: —`

**Why.**
- An EUR garnishment fee on the FX account is booked as a RON amount (§G-09).
- The same applies to payments and cash transfers.

**Langclaw today.**
- Fees: `fee_entry(str(abs(t.amount)), bank=bank)` with `bank = bank_account(statement.iban,
  t.currency, …)` (T:954-957).
- Cash transfers: the same, at T:938-941.
- Payments: `_book_payment` books the allocation amount in the statement's currency (T:856-872,
  `payment_entry` in `bank/booking.py:48-87`).
- The journal has no currency column (J:34-42).

**Design (a guard until WP-25).**
- In `bank_import`, a movement whose currency ≠ RON is **not booked**. `set_match` marks it
  `match_kind="fx_pending"` with `because="foreign-currency movement: needs a RON rate (WP-25)"`.
- Certain matches still apply the payment to the invoice's `fields.payments`, since that is a
  document-level fact in the invoice currency. They post no entry.
- The tool result lists `fx_pending: [...]`.
- `bank_movements` shows them.
- The month report's `bank.unbooked` includes them, so `reconcile_account` explains the difference
  once WP-25 lands.
- Add `"fx_pending"` to the documented `match_kind` values (`bank/store.py`).

**Steps.**
1. Red tests.
2. Branch in the three booking paths inside `bank_import` and in `bank_confirm_match`.
3. Result key.
4. Docs.

**Tests.**
- Given an EUR CAMT statement with a −10 "comision" line, when `bank_import` runs, then no `bank/<tx>/fee`
  entry exists and the result lists it under `fx_pending`.
- Given an EUR payment that certainly matches an EUR invoice, then the invoice's `payments` is updated
  and no `bank/<tx>/<invoice>` entry is posted.

**Docs.** Guide "Bank statements": foreign-currency movements wait for FX.

**Risks.** Clients who relied on the wrong postings will see fewer entries. Mention it in the PR, and
suggest `journal_reverse` for past wrong fee entries.

**Done when.** No non-RON amount reaches `journal_lines` through the bank paths.

---

### WP-05 · Client identity at every intake

`status: todo · size: S · depends: — · decision: —`

**Why.**
- A client's SPV list carried another client's invoice row (T1).
- A year-end workspace held a second company's full database (Y3).
- The practice's rule: a receipt with a different tax ID is not the client's (C1
  `Client/declaratii/README.md`) (§I-05).

**Langclaw today.**
- `restore_books` discards the manifest (`_, tables = read_archive(data)`, `archive.py:116`) although
  `archive_books` writes `client` (`archive.py:57-58`). Client A's archive therefore restores into
  empty client B.
- e-Factura sync sets `direction = "out" if supplier is us else "in"`
  (`langclaw/documents/efactura/sync.py:69-70`), without checking that the customer is the client.
  Scans do check this (`invoice_facts.py:75-84`).
- `TenantRegistry.save` checks only that chats are unique (`langclaw/tenants.py:123-134`), not the
  `tax_id`.
- Any IBAN maps to 5121/5124 by default (`bank/booking.py:29-34`).

**Design.**
- `restore_books(store, data, *, allow_other_client: bool = False)` raises `ArchiveError("This archive
  is client X's books, not Y's")` when `manifest.client != store.schema.removeprefix("tenant_")`.
  `accounting_archive_restore(key, from_client="")` must name the source client explicitly to move
  books across clients.
- e-Factura: when neither the supplier nor the customer digits equal the tenant's tax ID, file the
  invoice with `status: needs_review` and the problem "not this client's invoice (neither party is
  <own>)".
- `TenantRegistry.save` refuses a `tax_id` already used by another tenant (compare on digits).
- `bank_import`: when `profile.bank_accounts` is set and the statement IBAN is not in it, file the
  statement as `needs_review` with the problem "IBAN not in the client's bank accounts". Book nothing
  from it.

**Steps.**
1. Red tests.
2. The four guards.
3. A shared helper `langclaw/accounting/identity.py: same_tax_id(a, b)` (digits compare). WP-17 grows
   it.

**Tests.**
- Given an archive made for tenant `acme`, when it is restored into empty tenant `beta`, then
  `ArchiveError` naming both.
- Given a UBL invoice where neither party is the tenant, when it is synced, then status `needs_review`.
- Given two tenants with the same tax ID digits, when the second is saved, then `ValueError`.
- Given a statement for an unmapped IBAN while `bank_accounts` is set, then `needs_review` and 0
  entries.

**Docs.** `docs/guides/tenants.md` (tax ID unique), the guide's "Archiving a client's books" section,
`documents.md` (e-Factura).

**Risks.** Legitimate book moves between tenants need the explicit override.

**Done when.** No intake path can file another client's archive, invoice or statement silently.

---

## Phase B: foundations

### WP-06 · Law pack: legal reference data as sourced, dated, pinned data

`status: todo · size: M (slices) · depends: — · decision: —`

**Why.**
- The method treats every rate, threshold, deadline and treatment as provisional until the law is
  pinned, citing each one with its act, article, period and status (M0 `x orchestrator x/README.md`
  §2–3).
- Later rounds cite dated library rows (`verified_at`) instead of memory (OR `calendar-scadente-*`).
- langclaw ships these values as Python constants with no source or as-of date in its outputs
  (§E epistemics, §I-15).

**Langclaw today.**
- `RO_VAT_PERIODS` (`langclaw/accounting/vat.py:26-35`; source only in the docstring, lines 1-8).
- `LIMITS` has year granularity (`outlook.py:28-46`), so it cannot express the 2025-09-01 threshold
  change.
- `WARN_AT` (`outlook.py:48`).
- `_next_25th` has no weekend roll (`outlook.py:52-54`).
- `PROFIT_TAX_RATE` and `DEFAULT_MICRO_RATE` (`results.py:19-20`).
- `legal_basis` strings are hardcoded (e.g. P:376).
- No output carries a source, `as_of` or certainty.

**Design.**
- Package `langclaw/accounting/reference/`:
  - `__init__.py` with `LAW_PACKS` (registry) and `load_law_pack(pack_id: str = "") -> LawPack`, where
    empty means the latest;
  - `ro/<pack_id>.json` files, immutable once shipped.
- Pack metadata: `{id: "ro-2026.09.0", as_of, max_age_days, sha256 (computed), rows: [...]}`.
- Row: `{key, value, unit, valid_from, valid_to, applies, source: {act, article, url?}, verified_at,
  certainty: "verified" | "practitioner" | "disputed" | "dc", alternatives?: [...], note}`.
  - `applies` is a tiny profile predicate, e.g. `{"vat_payer": false}`.
  - A `disputed` row carries both values in `alternatives`.
- `LawPack` API:
  - `value(key, on: date, profile) -> Basis`, where `Basis = {key, value, source, verified_at,
    certainty, pack, flag}` and `flag` is `"[de confirmat]"` unless the certainty is `verified` and
    the row is fresh and in force;
  - `vat_rates(on)`;
  - `limits(on, profile)`;
  - `deadline_rule(form)`;
  - `holidays(year)`;
  - `next_working_day(d)`.
- Profile key `law_pack` (optional pin). Outputs note `unpinned` when it is unset.
- Every output that uses a law value adds `basis: [Basis, …]`. The first consumers are:
  - `check_proposal` rate problems;
  - `deadlines`;
  - `thresholds`;
  - `tax_estimate`;
  - the `vat` section of the month report.
- The `monthly_advice` template prompt (`ui/templates/monthly_advice.graph.json`) must repeat
  `[de confirmat]` markers and never upgrade them.

**Slices.**
- **a.** The seam plus the VAT rates moved from `vat.py`: `vat.allowed_vat_rates` becomes a thin
  wrapper. Output unchanged except for the added `basis`.
- **b.** `LIMITS` → date-effective rows (`valid_from`/`valid_to`), with the 2025-09-01 VAT-registration
  change and the micro ceiling rows; `thresholds` uses them (CATALOGUES §E-2).
- **c.** Deadline rules, the RO public-holiday list per year and `next_working_day`. `deadlines()`
  keeps its output keys and adds `basis` and the rolled `due`. Uses the deadline table in §E-1.
- **d.** Tax rates (profit, micro, dividend), with the WP-19 micro quarter rule as data.
- **e.** A CI test that every row has `source` and `verified_at`, plus a `law_pack_audit()` report
  listing `dc`, `disputed` and stale rows. This is the retro-review method from §I-M02.

**Steps (per slice).** Red test first; move one table; keep old import paths as wrappers; add `basis`;
document it.

**Tests.**
- Slice a: `allowed_vat_rates(date(2025, 7, 31))` and `date(2025, 8, 1)` return the same sets as today,
  and the `basis.pack` equals the loaded pack id.
- Slice b: a non-payer with RON revenue checked on 2025-08-31 uses the row valid then; on 2025-09-01,
  the next row applies (values from the pack, not hardcoded in the test).
- Slice c: `deadlines("2026-06", {...payer})` gives `due == "2026-07-27"` when the 25th is a Saturday.
  Use the pack's rule and the real calendar, and assert the weekday roll, not the legal date.
- Slice e: a pack row without `verified_at` fails the CI test.

**Docs.** New guide section "Reference data (law pack)"; update Limits; add a `CLAUDE.md` row for
`reference/`.

**Risks / [de confirmat].**
- Every shipped row starts as `dc` or `practitioner` except where CATALOGUES §E records a confirmation
  with a source. Disputed rows (D101, D394, financial-statement and micro-Q4 dates) ship as `disputed`
  with both values.
- The holiday list needs yearly maintenance: the audit flags a missing year.

**Done when.** No legal value is a Python literal outside `reference/`, and every output that uses one
carries `basis` and a flag.

---

### WP-07 · Typed tenant profile with provenance and `tenant_check`

`status: todo · size: M · depends: WP-01 · decision: —`

**Why.**
- In four of six method clients a basic profile fact was overturned mid-engagement by a primary
  document (J-07). Examples: VAT status overturned by the fiscal vector, "no revenue" overturned by a
  bank inflow.
- The practice's fix: each fact carries its source and date, and the fiscal vector is read early.
- A code-reading defect: defaults for the same key disagree between modules (§I-02, §I-07).

**Langclaw today.**
- `Tenant.profile: dict[str, Any]` is unvalidated (`langclaw/tenants.py:68`).
- The console edits four fields plus free JSON (`ui/editor.py:314`).
- `fiscal_year_start` is a UI hint that no code reads (lens A §2).
- Profile keys read in code: `vat_payer`, `vat_on_collection`, `vat_period`, `tax_regime`,
  `micro_rate`, `employees`, `eur_ron`, `expected_documents`, `bank_accounts`, `cash_limit`,
  `cash_payment_limit`, `cash_revenue_account`, `tax_id`, `caen`.

**Design.**
- `langclaw/accounting/profile.py`: a Pydantic `AccountingProfile` with `extra="allow"`, so unknown
  keys survive. It holds one documented default per key and a validator on `TenantRegistry.save`,
  reached through a hook: `tenants.py` stays generic, and accounting registers a profile validator.
- New optional keys, each read by a later WP:
  - `legal_form`;
  - `book_of_record` (WP-21);
  - `period_policy` (WP-10);
  - `law_pack` (WP-06);
  - `go_live` and `declaration_cutoff` (WP-13);
  - `intra_eu`, `oss_registered`, `amef`, `pays_nonresidents`, `distributes_dividends` (WP-09);
  - `vat_liability_account` (WP-16);
  - `clearing_accounts` (WP-31);
  - `subledger_accounts` (WP-12);
  - `csv_dialect`.
- Provenance: any key may be given as `{value, source, as_of, status: "assumed" | "confirmed"}`.
  `profile_value(profile, key)` unwraps it, and `profile_basis(...)` lists the `assumed` keys a result
  depended on. Reports add `assumptions: [...]`.
- Tool `tenant_check()` (read-only) returns `[{check, ok, fix}]` for:
  - the required keys (`vat_payer`, `tax_regime`);
  - `review_chat` set;
  - opening balances posted;
  - `bank_accounts` covering the imported IBANs;
  - the law-pack pin;
  - assumed keys older than N days.

**Steps.**
1. Red tests.
2. Model and hook.
3. `profile_value` used by `_profile()` readers (T:118).
4. `tenant_check` tool.
5. Console Clients form: show provenance and the checks.

**Tests.**
- Given a profile `{"vat_payer": {"value": false, "source": "fiscal vector", "as_of": "2026-01-10",
  "status": "confirmed"}}`, when `vat_regime` runs, then `payer is False`.
- Given `tax_regime: "micro"` marked `assumed`, when the results tool runs, then `assumptions` lists it.
- Given a tenant with no opening entry, then `tenant_check` reports `opening_balances: ok=false`.

**Docs.** `docs/guides/tenants.md` gets a profile schema table (key, meaning, default, read by).
Update the guide.

**Risks.** Validation must not reject existing free-form profiles: warn first, and reject only the
typed keys with wrong types.

**Done when.** One documented schema exists, `_profile()` readers go through it, and `tenant_check`
runs from chat and the console.

---

### WP-08 · Controls registry: one gate for close, export, template and firm row

`status: todo · size: M · depends: — · decision: —`

**Why.**
- The migration toolkit gates export on a control report: blocking vs advisory controls, a
  `hard_failures` count, and an approval bound to a data snapshot (T1
  `expert-to-saga-toolkit/expert2saga/reconcile.py`, `docs/RECONCILIATION_CONTROLS.md`).
- The practice rule is "never plug" (§D-01, §F-01, §I-M05).

**Langclaw today.**
- The close gate is seven ad-hoc `if`s with no ids and no report of passed controls (T:574-601).
- The same logic is repeated in the `accounting_month` template's `can_close` branch
  (`ui/templates/accounting_month.graph.json`, branch after `recheck`) and in
  `overview.firm_row.ready_to_close` (`langclaw/accounting/overview.py:79`).
- `accounting_export` has **no gate** (T:330-375): it exports posted invoices of open or unreconciled
  months.

**Design.**
- `langclaw/accounting/controls.py`:
  - `Control(id, title, blocking: bool, run: Callable[[ControlContext], ControlResult])`;
  - `ControlResult(id, status: "PASS" | "FAIL" | "INFO" | "SKIP", target, actual, diff, detail,
    fix)`;
  - registry `CONTROLS` (one declaration each);
  - `run_controls(report, ctx) -> {controls: [...], hard_failures: int, checked_at, journal_hash}`.
- Initial controls: the seven existing gates, re-expressed with ids.

  | id | control |
  |---|---|
  | `CLOSE.ALREADY` | already closed |
  | `DOC.BLOCKERS` | invoices without an entry |
  | `DOC.EXPECTED` | expected documents missing |
  | `TB.BALANCED` | trial balance doesn't balance |
  | `BANK.CHAIN` | statement chain |
  | `BANK.AGREES` | bank vs ledger |
  | `CASH.NEGATIVE` | negative cash |

  Plus `ANOM.WRONG_SIDE` (from `balance_anomalies`), which is advisory now and blocking only in the
  template, as today.
- The month report gets a `controls` section. `accounting_period_close` refuses when
  `hard_failures > 0` and names the first failure plus a `controls` list. `can_close` in the template
  branches on `report.controls.hard_failures == 0`. `firm_row.ready_to_close` reads the same number.
- Export gate: `accounting_export(..., period="")` runs the controls for every month covered by the
  batch, and refuses when any has `hard_failures > 0`, naming it. The gate is skipped only with an
  explicit `force_reason` argument, which is logged in the documents' fields.
- `journal_hash`: SHA-256 over the period's entries and lines (ordered by id, canonical JSON, the same
  serialisation as `archive.py:51-53`). Stored in `reports/<p>/close.json` and reused by WP-11.

**Steps.**
1. Red tests.
2. `controls.py` with the seven controls.
3. Report section.
4. Rewire the close, the template branch and `firm_row`.
5. Export gate.
6. Update `tests/test_outlook.py:95-111`, which pins the template's tool order, if any node changes.

**Tests.**
- Given an open month with an invoice without an entry, when `accounting_export` runs for that month,
  then an error naming `DOC.BLOCKERS` and nothing exported.
- Given a clean month, when the report runs, then every control is PASS and `hard_failures == 0`.
- Given the template spec, when parsed, then the `can_close` branch reads `controls.hard_failures`.
  A pure test pins it.

**Docs.**
- Guide "Closing a month": list the control ids.
- Guide "Export to SAGA / NextUp": the gate.
- `CLAUDE.md`: a row for `controls.py`, and update the Accounting row ("close gates").

**Risks.** The export gate changes behaviour for existing users: mention the `force_reason` escape in
the error.

**Done when.**
- One registry decides the close, the template branch, the firm row and export.
- Adding a control is one declaration.

---

### WP-09 · Filings register, calendar and receipts

`status: todo · size: L (slices) · depends: WP-06, WP-07 · decision: —`

**Why.**
- Five of eight client cases archive filing receipts, and every takeover starts with a matrix of
  obligation × filed × books (J-02, weight 3).
- Only an ANAF receipt proves a filing. A locally validated file does not (C2), and inferring earlier
  filings from a later receipt is inference, not proof (C3).
- The correction route depends on whether a period was filed (§E-3, §G-01, §I-08, §H-11).

**Langclaw today.**
- `deadlines()` (`outlook.py:57-72`) knows D300/D394 (payers), D112 (employees) and D100 (micro,
  quarter end), all due on the 25th.
- It has no D406, D101, D301, D390, D398, D100-profit, D205, D207, financial statements or F4109.
- There is no notion of "filed" anywhere in `langclaw/accounting/` (grep for filings/receipts: none).

**Design.**
- Per-tenant table `filings` (created like `closed_periods`, listed in `ARCHIVED_TABLES`), with columns:
  - `id TEXT PK` ("ANAF/D300/2026-05", "ANAF/D406/2026-Q2", "ANAF/D101/2025");
  - `form`, `period`, `period_kind 'M'|'Q'|'Y'|'event'`;
  - `obligation 'required'|'not_required'|'unknown'`;
  - `obligation_basis` (`vector:<bucket_key>` | `profile` | `rule:<pack key>`);
  - `due DATE NULL`, `due_basis JSONB` (the WP-06 `Basis`);
  - `zero_filing BOOL`;
  - `receipt_key TEXT NULL`, `registration_no`, `upload_index`, `filed_on DATE`, `validation`,
    `receipt_tax_id`;
  - `evidence 'receipt'|'inferred'|'none'`;
  - `rectifies TEXT NULL`;
  - `recorded_by`, `updated_at`.
- Derived `state(today)`:
  - `filed`: needs `receipt_key` and a `receipt_tax_id` equal to the tenant's;
  - `not_required`;
  - `unknown`;
  - `overdue` when `due < today`;
  - `due` when within 7 days;
  - `soon_due` when within 14 days;
  - `upcoming` otherwise.

  Evidence `inferred` never counts as `filed`.
- Tools:
  - `filings_calendar(period="")` generates or refreshes the rows from the law pack and the profile,
    and is idempotent. `deadlines()` becomes a wrapper that keeps its keys.
  - `filing_record_receipt(bucket_key)` parses the receipt PDF text (CATALOGUES §C-F8), matches
    (form, period) and refuses a foreign tax ID.
  - `filings_status(period_from="", period_to="")` is read-only and client-visible.
  - `filing_mark(id, obligation, reason)` is for "not required" decisions, logged.
- Month report: a `filings` section with the rows touching the period. `accounting_outlook` uses the
  calendar.
- The agent never files. The tools only record.

**Slices.**
- **a.** Table, states, `filings_status`, `filing_mark`, and a manual receipt record (fields typed in).
- **b.** `filings_calendar` from the law-pack rules for the forms in CATALOGUES §E-1, with weekend and
  holiday roll and the new profile keys.
- **c.** `filing_record_receipt` PDF text parser, through the documents intake (doc_type
  `anaf_receipt`).

**Tests.**
- Given a receipt whose tax ID ≠ the tenant's, when it is recorded, then an error and nothing stored.
- Given a D301 month with no reverse-charge purchases, when the calendar runs, then the row is
  `not_required` (the rule is `dc`; assert that the rule comes from the pack).
- Given a non-payer with profit tax, when the calendar runs for 2026-03, then the rows D100 Q1 and
  D406 Q1 exist and are `unknown`/`upcoming`, never `filed`, until a receipt is recorded (§G-01).
- Given the 25th on a Saturday, then `due` is the next Monday (slice b).

**Docs.** A new guide section "Filings and receipts"; add `filings` to the archive list; add a
`CLAUDE.md` row.

**Risks / [de confirmat].**
- Every deadline rule and applicability rule is `[de confirmat]` except the rows CATALOGUES §E marks
  as recorded-verified.
- Receipt numbers are identifying data. They stay in the tenant schema and are never echoed to shared
  chats (WP-28).

**Done when.**
- Filings are rows with a receipt-backed state.
- The calendar covers the §E-1 catalogue.
- Nothing is "filed" without a matching receipt.

---

### WP-10 · Filed-aware, non-destructive corrections

`status: todo · size: M · depends: WP-09 · decision: **D1**`

**Why.**
- Method: a closed period is never rewritten, and corrections are reversals dated today (M0
  `…/Checklist inchidere luna….md` §0, §3).
- Practice routing: filed → storno; not filed → reopen allowed (C1 audit, C3 review) (§D-14, §G-19,
  §I-08, §B-03).

**Langclaw today.**
- `reopen_period` needs only a reason. It **DELETEs** `close/<period>/%` entries (J:421-425) and drops
  the lock row (J:426-428).
- `reverse` renames the original's `bucket_key` with an `UPDATE` outside the posting transaction and
  never checks the original's month (J:163-169). Registers filed at close then stop matching live
  keys.
- `accounting_period_reopen` accepts `""` and resolves it to last month (T:670, `P:39-52`).

**Design** (the shape depends on D1; A is shown).
- Profile `period_policy: "storno_only" | "reopen_unless_filed"`. The default is set by D1.
- `reopen_period`:
  - refuses under `storno_only`, pointing to `journal_reverse` / `journal_note` (WP-22) dated today;
  - under `reopen_unless_filed`, refuses when any `filings` row covering the period is `filed`, unless
    `rectification=True` is passed with a reason (then it logs `rectifies`);
  - never deletes: the close entries move to a `voided_entries` table (row JSON, lines JSON, reason,
    actor, at) in the same transaction;
  - requires a non-empty `period`, so a blank no longer means last month.
- `reverse`: no rename when the original's month is closed. It adds a column `reverses TEXT NULL` on
  `journal_entries` pointing to the original key, and the document's status handling uses that link.
  The rename, when it still happens (open month), runs inside the same transaction as the reversal.
- The router `correction_route(period) -> {"route": "storno" | "reopen", "because": [...]}` is pure:
  filed → storno; policy `storno_only` → storno; filing status unknown → storno ("safe default"); else
  reopen.

**Steps.**
1. Ask D1.
2. Red tests.
3. `voided_entries` table (archived).
4. Policy checks.
5. `reverses` column plus the in-transaction rename.
6. Router exposed in `accounting_period_report` as `correction_route`.

**Tests.**
- Given 2026-03 closed and D300 2026-03 recorded filed, when it is reopened, then an error pointing to
  storno.
- Given `storno_only`, when any reopen is attempted, then refused.
- Given a reopen allowed, then the `close/2026-03/*` rows are in `voided_entries` and absent from the
  journal.
- Given an original posted in closed 2026-02, when it is reversed on 2026-04-10, then the original's
  key is unchanged and the reversal carries `reverses`.
- Given `accounting_period_reopen("")`, then an error "name the month".

**Docs.** Guide "Correcting a posted entry" and "Closing a month"; Limits.

**Risks.** Existing data with `#reversed-n` keys keeps working: read both conventions.

**Done when.**
- No closed-period row is ever deleted or renamed.
- Reopening respects the policy and filed returns.

---

### WP-11 · Sealed closes and superseded reports

`status: todo · size: S · depends: WP-10 · decision: —`

**Why.**
- Method: the close is an immutable named point, approval is a reviewed diff, and audit is author,
  time and hash (M0 checklist §0, §2.6).
- A stale artefact outliving its correction is a recurring failure (C5 `_STALE/`, C1 pinned-charter
  regression) (§B-04, §I-12).

**Langclaw today.**
- `reports/<p>/close.json` is overwritten on re-close (T:636-639). The registers are too (T:640-648).
- After a reopen these files still describe the old close.
- `closed_by` is a free argument (T:559). No content fingerprint exists.

**Design.**
- `closed_periods.seal TEXT`, `period_log.seal TEXT`: the WP-08 `journal_hash` at close time.
- The close writes `reports/<p>/close-<n>.json` (never overwritten) plus a `reports/<p>/current.json`
  pointer.
- On reopen, earlier artefacts get `superseded_by` in their documents rows and move under
  `reports/<p>/superseded/<ts>/`.
- `accounting_period_verify(period)`, read-only, recomputes the hash and reports `sealed_ok`, or which
  entries changed.
- Record the approving review id on the close when it runs from a workflow (`current_actor()` plus the
  review key, when available).

**Tests.**
- Given close → reopen → re-close, when the files are listed, then `close-1.json` is superseded and
  `close-2.json` is current.
- Given a closed period, when `accounting_period_verify` runs, then `sealed_ok: true`. After a direct
  SQL update in the test, it is `false` with the changed key.

**Docs.** Guide "Closing a month" and "Archiving" (add the seal to the manifest).

**Done when.** Every close is fingerprinted, verifiable and never overwritten.

---

## Phase C: ledger controls and regime rules

### WP-12 · Partner-aware openings and sub-ledger ties

`status: todo · size: M · depends: WP-08 · decision: **D2**`

**Why.**
- Takeovers bring partner open items. In one case the supplier aging differed from 401 by exactly one
  invoice (C3).
- The migration toolkit blocks on Σ partner balances = synthetic balance (M1.1/M1.2) and loads
  openings per partner and per document (T1 `docs/PRINCIPLES.md` P7, P9, P10) (§D-02, §G-02, §G-12,
  §I-10).

**Langclaw today.**
- The partner is stored per **entry** (`J:131-132`), and `partner_balances` filters
  `partner_cui <> ''` (J:324).
- The opening entry has no partner (T:1739-1741, `P:124-154`), so opening 401/4111 never reach
  statements, aging, offsets or confirmations.
- The month report has no tie between synthetic and partner balances.

**Design.**
- `accounting_opening_balances(day, balances, open_items=None)`:
  - `open_items: [{partner_cui, partner_name, doc_no, date, due, amount, direction: "in"|"out",
    account?}]`;
  - each item posts `opening/<day>/<partner_key>/<doc_no>` against the take-on contra (D7 / WP-13)
    **and** creates a `documents` row (doc_type `invoice`, status `posted`, `fields.opening=true`,
    amount, due date), so `outstanding()`, matching, aging and confirmations see it;
  - the synthetic `balances` must then **not** also carry those accounts. An error names the double
    count (T1 "posting ownership").
- Control `SUBLEDGER.TIE` (WP-08 registry), blocking. For the accounts in
  `profile.subledger_accounts`:
  - defaults per D2: payables 401, 403, 404, 408, 409; receivables 4111, 411, 413, 418, 419;
  - check `balance_until(day, acct)` == Σ partner balances on the same accounts;
  - list the entries on those accounts with an empty `partner_cui` as `unattributed`;
  - tolerance 0.01.
- `partner_balances` keeps its signature, but gains an `unattributed` row instead of dropping blank
  partners silently.

**Tests.**
- Given an opening with a 4111 open item `{cui: X, doc: A, amount: 1000}`, when `partner_balances` and
  `receivables_overdue` run, then X shows 1000 and doc A is listed (§G-02).
- Given opening balances `{"401": -200, "5121": 200}` with no open items, when the controls run, then
  `SUBLEDGER.TIE` FAIL with diff 200, and `opening/<day>` listed as unattributed.
- Given open items for 401 and a synthetic "401" in `balances`, then an error "posted twice".

**Docs.** Guide "Opening balances" (new subsection) and "Partner statements"; Limits.

**Risks.** The partner key for foreign partners without a tax ID comes from WP-17. Until then, use
`name:<normalized name>`.

**Done when.** Opening partner items behave like invoices, and the tie gates the close.

---

### WP-13 · Take-on shapes: off-balance memo, year-end vs mid-year cut, go-live

`status: todo · size: M · depends: WP-12 · decision: **D7**`

**Why.**
- A take-on's classes 1–7 foot, but class-8 memo accounts are one-sided by design. The rule is "never
  plug" (T1 P3, P8).
- A mid-year cut must carry classes 6/7 year to date, plus both the 1 January opening and the
  cumulative turnover (T3 runbook §1, §4, §9).
- Periods before go-live were filed from the old system and must not be regenerated (T1 P13)
  (§D-03, §D-11, §G-03, §G-27, §I-11, §I-14).

**Langclaw today.**
- `opening_entry` rejects a non-zero sum, class 8 included (P:151-152).
- It takes one net balance per account, with no year-to-date turnover (P:124-154).
- There is no class-6/7 check against the date.
- There is no `go_live`: `sync_efactura(days=60)` has no floor (`efactura/sync.py:32-43`).

**Design.**
- `opening_entry(balances, *, day, turnover_ytd=None, off_balance="memo")`:
  - Classes 8/9 go into `off_balance: {account: amount}`. They are not posted under D7-A, or they are
    posted against `profile.off_balance_contra` under D7-B. `balanced` checks classes 1–7 only.
  - On a 31 December date, refuse classes 6/7 ≠ 0: "classes 6/7 must be closed at a year-end opening".
  - Mid-year (any other date): require `turnover_ytd = {account: {debit, credit}}` when classes 6/7 ≠
    0. Check the three balanced totals: opening, cumulative and closing. Warn when classes 6/7 net to
    zero on a mid-year date ("closed export?").
  - Two-sided partner balances: when an open item nets a debit and a credit on one partner, warn and
    split to 409/419.
- Profile `go_live` / `declaration_cutoff`: `efactura_sync`, `filings_calendar`, `accounting_d394`
  and exports skip or flag anything dated earlier. `tenant_check` (WP-07) asks for them.

**Tests.**
- Given `{"5121": 100, "1012": -100, "8035": 50}`, when it is posted, then the result shows
  `off_balance: {"8035": 50}`, nothing is raised, and the trial balance is `balanced: true` (§G-03).
- Given `day="2025-12-31"` and `{"628": 500, "121": -500}`, then the class-6/7 error.
- Given a mid-year day with a class-6 balance and no `turnover_ytd`, then an error naming it.
- Given `go_live=2026-01-01`, when `efactura_sync` sees an invoice dated 2025-12-20, then it is filed
  as `needs_review` "before go-live".

**Docs.** Guide "Opening balances"; tenants profile table.

**Risks / [de confirmat].** Contra-account choice (D7). The SAGA column semantics are WP-24.

**Done when.**
- Real take-on shapes post without plugs.
- Pre-go-live periods are protected.

---

### WP-14 · Completeness checks and must-be-zero anomalies

`status: todo · size: M · depends: WP-07, WP-08 · decision: —`

**Why.**
- "Balanced" or "validated" was repeatedly mistaken for "complete". Examples: a trial balance with
  December empty (Y2); a validated SAF-T with a month missing (C1); a bank account with zero receipts
  while the statement had an inflow (C4).
- Months were closed with receipts parked on 462 and no revenue (C1).
- Residues stayed on 473 and 581, and P&L accounts were left open (C2) (§I-03, §I-07, §I-09, §D-15,
  §G-04, §G-05, §G-07, §G-24).

**Langclaw today.**
- A month with zero entries and no `expected_documents` closes (T:574-601).
- Bank agreement only covers IBANs with imported statements (T:423-436).
- `_BALANCE_RULES` has 11 wrong-side rules and no must-be-zero rules (P:159-171).
- Anomalies do not block the close.
- `statement_chain` silently skips statements without iban, date or opening (`bank/reconcile.py:33`).

**Design.**
- A report section `completeness` (and matching WP-08 controls):
  - `COMP.EMPTY_MONTH`: entries in the month = 0 → FAIL unless acknowledged.
  - `COMP.LAST_ENTRY`: days since the last dated entry.
  - `COMP.BANK_NO_STATEMENT`: each IBAN in `profile.bank_accounts` without a statement covering the
    month → FAIL.
  - `COMP.BANK_TURNOVER`: statement credits and debits vs ledger turnover on the mapped account → INFO.
  - `COMP.REVENUE_GAP`: class-6 turnover > 0, class-7 = 0 and bank inflows > 0 → FAIL.
  - `COMP.CLEARING_PARKED`: credits on 46x from bank receipts with no class 7 in the month → INFO.
- Acknowledgement: `accounting_acknowledge(period, control_id, reason)` stores it in `period_log`
  (action `acknowledged`). An acknowledged FAIL counts as passed, and the reason shows in the report.
- `_BALANCE_RULES` gains a `"zero"` side with an optional context:
  - 473 (blocking at close);
  - 581 (already "any", now blocking);
  - 4426/4427 after the settlement for payers;
  - 6/7 after `year-end`;
  - 121 after `result-carry`;
  - 411 credit;
  - 442x at a non-payer (links WP-01);
  - class 6/7 carried into a new year.
- Rules that need history ("stale 473/409/419", mirror pairs) come later: INFO with a profile-set age.
- `profile_assertions` (INFO):
  - profile says no revenue + a customer-like bank credit;
  - `vat_payer: false` + 4426/4424 turnover;
  - `employees: 0` + `tax_regime: micro` `[de confirmat]`.
- No silent skips: `statement_chain` and `partner_balances` return `skipped: [{key, why}]`, and the
  report surfaces them.

**Tests.**
- Given an empty month with a mapped IBAN and no statement, when it closes, then an error naming
  `COMP.BANK_NO_STATEMENT` and `COMP.EMPTY_MONTH` (§I-03).
- Given three receipts booked 5124/462 and no class 7, then `COMP.REVENUE_GAP` FAIL (§G-05).
- Given a 473 balance at month end, then close refused "473 must be cleared".
- Given `vat_payer: false` with 4428 = 25 and 627 = 10 carried on 2026-01-01, then anomalies "VAT
  account at non-payer" and "P&L balance carried into new year" (§G-04).
- Given two statements, one missing its opening, then `bank.chain_skipped` lists it.

**Docs.** Guide "Closing a month" (controls list, acknowledgement); Limits.

**Done when.** The empty, parked and residue cases in §I-03 cannot close silently.

---

### WP-15 · Declared close steps and chronological close

`status: todo · size: M · depends: WP-08 · decision: —`

**Why.**
- The practice's close order: documents → treasury → stock → depreciation → payroll → **FX
  revaluation** → **VAT close** → **profit tax** (quarter end) → **6/7 → 121** → trial balance →
  returns (human).
- Months close chronologically and reopen in reverse (C3 review §2.1, C1 audit N10, T4 closing
  scripts) (§D-12, §I-17).

**Langclaw today.**
- The close posts depreciation → VAT settlement → year end (December only), inline (T:602-635).
- There is no FX or tax step.
- `close_period` never checks that the previous month with activity is closed (J:377-395).
- Reverse-order reopen is enforced (J:417-420).

**Design.**
- `langclaw/accounting/close_steps.py`: `CloseStep(id, applies(period, profile, report) -> bool,
  build(ctx) -> entry | None, key_suffix)` and `CLOSE_STEPS = (depreciation, fx_revaluation,
  vat_settlement, profit_tax, year_end)`. The `fx_revaluation` and `profit_tax` steps are declared
  inert (`applies → False`, with a reason) until WP-25 and WP-19 fill them.
- Each step posts `close/<p>/<id>`, and the report re-runs after each posting step.
- `entries_between(without_invoices=True)` already matches `close/%`, so no LIKE-list change is needed
  (J:219-222).
- Gate `CLOSE.PREVIOUS_OPEN`: the most recent earlier month that has any entry must be closed.
  Otherwise: "close 2026-02 first". Opening-only months count as closed.
- Post-close check `CLOSE.RESULT_TIE` (December): movement on 121 = expenses closed − revenues closed.
  Each P&L account is closed by exactly its balance.

**Tests.**
- Given 2026-02 open with entries, when 2026-03 closes, then the error "close 2026-02 first".
- Given the declared order, when December closes, then the entries are posted in order: depreciation,
  then vat-settlement, then year-end. Assert via `created_at`/id order.
- Given an inert step, then the report lists it under `close_steps` with `applies: false, why`.

**Docs.** Guide "Closing a month": the step table; `CLAUDE.md` row.

**Done when.** Close steps are declared data in the practice's order, and months close in sequence.

---

### WP-16 · Non-payer reverse-charge posting and D301 figures

`status: todo · size: M · depends: WP-01, WP-06 · decision: **D3**`

**Why.**
- Two of eight cases are non-payers buying EU services (platform fees, marketing).
- The correct template (Y1 §6, CATALOGUES §D-07) cannot pass langclaw's checks.
- The errors found in practice fall into four detectable classes (§G-13, §I-01, §E-7).

**Langclaw today.**
- For a non-payer, `check_proposal` refuses any 442x line (`checks.py:127-131`).
- For a payer, an AE invoice with `total_vat` 0 trips "VAT booked ≠ invoice VAT"
  (`checks.py:133-134`).
- There is no D301 block. The report shows D300 figures only (T:484).

**Design.**
- `check_proposal` for `payer=False` and an invoice whose `vat_breakdown` has category AE accepts
  exactly this pattern:
  - D 6xx / C 401 for the base (+ the domestic gross lines);
  - D 4426 / C 4427 for the VAT;
  - D 635x / C 4426 for the VAT;
  - D 4427 / C `vat_liability_account`.

  The VAT must equal base × the rate valid on the invoice date (law pack). 4426 and 4427 must net to
  zero **inside the entry**.

  With D3-C, a missing `vat_liability_account` is the problem "set vat_liability_account (D3)".
- For a payer, an AE invoice gets VAT = Σ taxable × rate, instead of comparing with the invoice's 0
  `total_vat`. Booked D 4426 / C 4427 passes.
- Report block `vat.d301` (non-payer): `{taxable, vat, liability_account, liability_balance}`.
  Cross-check: opening + Σ self-assessed − Σ payments = balance of the liability account.
- Anomaly section `nonpayer_vat`, applied to journal lines for a non-payer:
  - (a) D 4427 / C 4426 in a settlement → "self-assessed VAT cancelled";
  - (b) any D 4424 → "fictitious recovery";
  - (c) D 4423 / C 4424 → "set-off without payment";
  - (d) D 4426 against a domestic supplier's 401 → "forbidden deduction".

**Tests.**
- Given `vat_payer: false` and an AE invoice (base 100, rate 21), when the four-line proposal is
  checked, then no problems. When the report runs, then `vat.d301.vat == 21` (§G-13).
- Given the same client and a later entry D 4427 / C 4426, then `nonpayer_vat` reports rule (a).
- Given a payer and an AE invoice with `total_vat` 0, when D 4426 / C 4427 = 21 is booked, then it is
  accepted.

**Docs.** Guide "The checks" (a reverse-charge subsection); remove the Limits line about the 0%
reverse-charge rate once it is handled.

**Risks / [de confirmat].** The liability account (D3), the form (D301) and the articles are all
`[de confirmat]` and come from the law pack or profile.

**Done when.** Both regimes can book reverse charge through the normal proposal path, and the four
error classes are reported.

---

### WP-17 · Partner identity: tax-ID normalization, check digits, blank-ID partners

`status: todo · size: S · depends: WP-05 · decision: —`

**Why.**
- Merging partners by tax ID collapses every foreign partner that has none (T1 adversarial review
  CO-3).
- A marketplace invoiced the client under another tax code of the same business (C1).
- langclaw stores the partner as free text (§D-17, §G-08, §G-26, §I-10).

**Langclaw today.**
- `partner_cui` is free text (J:131). Scans force `RO<digits>` (`invoice_facts.py`, lens A §8).
- Only e-Factura compares digits (`efactura/spv.py` `cif_digits`).
- There is no check-digit validation anywhere (grep).

**Design.**
- `identity.py`:
  - `normalize_tax_id(raw, country="RO") -> TaxId(key, country, digits, valid: bool | None, vat_prefix:
    bool)`.
  - `partner_key(tax_id, country, name) -> str`: the key is `RO:<digits>` for RO,
    `<CC>:<vat number>` for EU, and `name:<normalized name>` when there is no ID, so blank IDs never
    merge.
  - The RO check digit uses weights 7,5,3,2,1,7,5,3,2 on the left-padded body, then ×10 mod 11, with
    10 → 0. This is generic knowledge, **not** taken from a workspace: write tests with invented valid
    and invalid IDs, and have an accountant confirm the rule.
- The partner key is used by e-Factura sync, scans, `bank_book_advance`, `partner_offset` and the WP-12
  open items.
- Problem `wrong_buyer_id` on intake, when the invoice's customer ID ≠ the tenant's tax ID but the
  names match.
- Optional `profile.tax_ids: [{id, role: "vat_only" | …}]` for the two-IDs case (§G-08). Invoices to a
  secondary ID are filed `needs_review` with `finding: second_tax_id`.

**Tests.**
- Given two foreign invoices with empty IDs and different names, when `partner_balances` runs, then
  two rows (§G-26).
- Given an invented RO ID with a wrong check digit, then `valid is False`. With a valid one (computed
  in the test), `valid is True`.

**Docs.** Guide "Partner statements"; `documents.md`.

**Risks.** Existing `partner_cui` values need a one-off normalization view, not a rewrite of history.

**Done when.** One partner-key function is used everywhere, and blank IDs never merge.

---

### WP-18 · Recoverable VAT (4424) by origin year, with prescription alert

`status: todo · size: S · depends: WP-06 · decision: —`

**Why.** A year-end workspace layers the 4424 balance by the year it arose. It consumes the layers
FIFO and warns before the oldest one prescribes (Y2 year-end preparation note §8)
(§D-08, §E-8, §G-11).

**Langclaw today.**
- `carried = balance_until(4424)` is one lump (T:470).
- `vat_settlement` consumes it without layers (P:360-362).

**Design.**
- `vat_recoverable_layers(lines, on) -> [{origin_year, amount, expires}]`:
  - every D 4424 opens a layer;
  - every C 4424 consumes the oldest first;
  - `expires` comes from the law-pack rule `vat_refund_prescription_years`, whose start date is
    disputed.
- Report `vat.carried_layers`.
- `accounting_outlook` warns when a layer expires within `profile.warn_days` (default 180): "request
  a refund in D300 before <date>".

**Tests.** Given debits 100 (2021) and 50 (2022) and a credit of 30 (2023), when the layers are
computed at 2026-07-01, then `[{2021: 70}, {2022: 50}]` and a warning for 2021.

**Docs.** Guide "Closing a month" (the VAT carry paragraph).

**Risks / [de confirmat].** Both the prescription term and when it starts are recorded as disputed.

**Done when.** The carry is shown by layer, and the oldest layer triggers an alert.

---

### WP-19 · Income tax: quarterly profit-tax step, micro rate by quarter of crossing

`status: todo · size: M · depends: WP-06, WP-15 · decision: —`

**Why.**
- Profit tax is booked quarterly on the year-to-date base, minus the tax already booked, and before
  6/7 → 121 (C1 audit, C3 review, Y2).
- For 2025 the micro rate stepped from 1% to 3% from the quarter in which cumulative revenue crossed a
  EUR threshold. A workspace records this as confirmed after an adversarial review (C5 R1).
- (§D-13, §E-6, §G-16.)

**Langclaw today.**
- `tax_estimate` returns one figure and never posts it (`results.py:61-78`).
- It has no "minus booked" and no loss carry-forward.
- The micro rate is a flat `profile.micro_rate`, defaulting to 1 (`results.py:20, 63-64`).

**Design.**
- `tax_estimate(pl, profile, *, booked=Decimal(0), quarter=None, pack)`:
  - **profit:** `tax_q = max(0, rate × taxable_ytd) − booked`, where taxable_ytd = result YTD +
    `non_deductible` − `deductions` − `loss_carried` (inputs, default 0);
  - **micro:** per-quarter rows `{quarter, revenue, cumulative_eur, rate, tax}`, with the rate from
    the pack rule (switch from the crossing quarter onward).

  It returns `basis`.
- Close step `profit_tax` (WP-15): at quarter end for `tax_regime: profit` it posts D 691 / C 4411
  = `tax_q` when > 0, before `year_end`. For micro it posts D 698 / C 4418 only when
  `profile.post_micro_tax` is true.
- A warning when `accounting_result_carry` reserves exceed the pack's reserve cap `[de confirmat]`.

**Tests.**
- Given a Q1 YTD profit of 1,000 with 160 booked and a Q2 YTD profit of 1,500, when Q2 closes at a
  16% pack rate, then 80 is posted.
- Given quarterly revenue that crosses the pack's threshold in Q4, then Q4 uses the higher rate and
  Q1–Q3 the lower one.

**Docs.** Guide "Results and income tax".

**Risks / [de confirmat].** Rates, threshold, EUR conversion rate, the YTD rule and the reserve cap all
come from the pack.

**Done when.** Quarterly tax is posted by the close, and the 2025 micro switch is reproducible.

---

### WP-20 · VAT-return ties: filed D300 chain and the 4428 tie

`status: todo · size: S · depends: WP-08, WP-09 · decision: —`

**Why.** The practice ties each filed D300 to the books, and the returns to each other:

- rd.30 ≈ debit turnover of 4426;
- rd.16/19 ≈ credit turnover of 4427;
- rd.45 ≈ the 4424 balance;
- rd.41(M) = rd.45(M−1) exactly;
- ≤ 1 leu per row, because D300 is in whole lei;
- Σ unpaid VAT share of the open invoices on VAT on collection = 4428 (M1.8).

(C4 `reconcilieri/… TB × D300 × Jurnal cumparari.md`, T1 controls) (§D-09.)

**Langclaw today.**
- The D300 draft exists (P:197-241).
- Filed values are never captured.
- There is no chain check and no 4428 tie.

**Design.**
- `filing_record_values(id, values: {row: amount})` stores the filed figures on the `filings` row
  (WP-09).
- Controls `VAT.RETURN_TIE` (INFO: Δ per row, ≤ 1 leu passes) and `VAT.CHAIN` (FAIL when rd.41(M) ≠
  rd.45(M−1)).
- `VAT.PENDING_TIE` (profile `vat_on_collection`): Σ outstanding × VAT/gross vs
  `balance_until(4428)`, blocking at 0.01.
- Row numbers are pack data per form version `[de confirmat]`.

**Tests.**
- Given March rd.45 = X and April rd.41 = X + 5, then `VAT.CHAIN` FAIL.
- Given turnover 100.40 vs rd.30 = 100, then PASS.
- Given an opening 4428 = 60 with no open VAT-on-collection invoice, then `VAT.PENDING_TIE` FAIL
  (§G-12).

**Done when.** Filed VAT figures are tied to the books and to each other.

---

## Phase D: large features (portfolio weights)

### WP-21 · Book of record: shadow mode alongside SAGA

`status: todo · size: M · depends: WP-07, WP-08 · decision: **D5**`

**Why.**
- SAGA C is the book of record for every live client. SAP and Expert appear only as sources.
- The practice's doctrine is "the agent prepares, checks and reports; it does not post in the books
  and does not file" (OR `calendar-scadente-*`; J-03).
- Round-trip needs: **in**, SAGA trial balance, journal, partners; **out**, notes and partners
  (J-02 weight 1).

**Langclaw today.**
- `Journal` is the only posting path (`CLAUDE.md` Accounting row).
- `journal_post(..., approved_by="")` lets the agent post with no person named (T:222-224).
- Close always locks (T:649).

**Design** (for D5-A).
- `profile.book_of_record: "langclaw" | "saga" | "sap" | "expert"` (default `langclaw`).
- When the value is not `langclaw`:
  1. Journal entries imported from the external book (WP-30) are keyed `ext/<system>/<entry no>`,
     flagged `source`, and read-only. `journal_post` for invoices becomes "propose": it files the
     checked proposal on the document (`fields.proposal`, status `proposed`) and returns it for export
     (WP-23).
  2. `accounting_period_close` runs the controls and files the report, but **does not** lock. Its
     result is `{verified: true, locked: false}`, because the lock lives in SAGA.
  3. Any posting tool requires a non-empty `approved_by`, whatever the mode:
     `{"error": "name who approved it"}`. This rule is global only if D5 says so. Otherwise it applies
     to shadow mode only.
- The console shows the mode on the Client overview.

**Tests.**
- Given `book_of_record: "saga"`, when `journal_post` runs with an empty `approved_by`, then an error.
  With a name, the proposal is stored and no journal row is written.
- Given shadow mode, when the close runs, then the report is filed and `closed_periods` is unchanged.

**Docs.** A new guide section "Working alongside SAGA"; the tenants profile table.

**Risks.** Existing behaviour must stay identical for `langclaw` tenants.

**Done when.** A SAGA-kept client can use langclaw for checks, proposals and exports without langclaw
claiming to be the ledger.

---

### WP-22 · General journal note (correction) tool

`status: todo · size: M · depends: WP-02, WP-08, WP-10 · decision: —`

**Why.**
- Most catch-up work is a correction note: reclassification, accrual, storno, revaluation, revenue
  recognition. It is not an invoice.
- About half the real scenarios (§G) break at "post the fix", because every langclaw posting path is
  tied to an invoice, bank, cash, opening or close step.

**Langclaw today.** The posting paths are `journal_post` (invoice only: T:233 `_invoice`), bank
(T:851), cash, opening (T:1741), close steps (T:608-631), offsets, advances and result carry. There is
no free, balanced note.

**Design.**
- Tool `journal_note(day, lines, reason, approved_by, decision_id="", note_type="correction",
  partner_cui="", partner_name="")`:
  - `lines` are `{account, debit, credit, explanation}`;
  - it posts `note/<day>/<n>` through `Journal.post` (balanced, dated, lock-checked);
  - the checks are the account regex and balance, plus 442x at a non-payer unless the WP-16 pattern
    applies, plus classes 6/7 on a closed year;
  - `reason` and `approved_by` are required;
  - in shadow mode (WP-21), it stores the note as a proposal for export instead of posting.
- `journal_reverse` extends to any key, not only document keys (`note/…`, `opening/…`).
- `entries_between(without_invoices=True)` gains `note/%` in its LIKE list (J:219-222), so the
  register and the NC export see the notes.
- Optional: `journal_note_preview(...)` returns the check problems and the effect on the balances
  without posting. The decision menus use it (WP-32).

**Tests.**
- Given 1171 = 300 and 461 = 5000, when a note D 4551 / C 461 = 5000 is posted with a reason, then it
  is posted and the partner balance moves (§G-10).
- Given a note dated in a closed month, then `JournalError`.
- Given five accruals on 635/446, when they are reversed through notes, then 446 goes to debit (§G-14).

**Docs.** A new guide section "Correction notes (note contabile)"; the `CLAUDE.md` Accounting row.

**Done when.** Any balanced correction can be proposed, checked, approved and posted (or exported)
through one tool.

---

### WP-23 · SAGA note-contabilă DBF exporter

`status: todo · size: M · depends: WP-08, WP-21, WP-22 · decision: —`

**Why.** SAGA round-trip carries portfolio weight 1. The NC import layout was recovered at byte level
(T1 `Facturi SPV/2026/NC_2026.dbf`, `expert-to-saga-toolkit/expert2saga/exporters/gl.py`), but it
was **never accepted** by a live SAGA (§C-F1, §F-03, §F-04, §I-13).

**Langclaw today.**
- The guide says there is no NC import "because the note-contabile import format wasn't available"
  (`docs/guides/accounting.md:684-686`).
- `Exporter.build(rows, *, own_cif)` takes document rows, not journal entries
  (`export/__init__.py:38-41`).
- `accounting_journal_register(without_invoices=True)` already selects the entries worth exporting
  (T:1597-1640), as CSV.
- Zips use `writestr` with the current time (`export/saga.py`, `archive.py:54`), so identical exports
  differ byte-wise.

**Design.**
- A second protocol in the registry: `JournalExporter{name, build_entries(entries, *, profile) ->
  ExportBatch}`. `EXPORTERS["saga_nc"]` implements it.
- `accounting_export(target="saga_nc", date_from, date_to)` reads `Journal.entries_between(...,
  without_invoices=True)` plus shadow-mode proposals (WP-21). It is gated by WP-08.
- The layout is in CATALOGUES §C-F1:
  - one `NDP` per entry;
  - legs decomposed into D/C pairs (one credit leg: pair each debit with it; one debit leg: mirror;
    many-to-many: greedy split, with Σ checks);
  - partner analytic `401.<code>` from `profile.saga_partner_codes`;
  - cp1250 with the language-driver byte 0xC8;
  - an **error** instead of silent truncation (C48/C20);
  - diacritics kept (cp1250), with an optional ASCII fold per `profile.csv_dialect`.
- A small `export/dbf.py: write_dbf(spec, rows, codepage) -> bytes`, with no third-party dependency
  if feasible. Otherwise add an optional extra `langclaw[saga-dbf]`.
- `export/_zip.py: deterministic_zip(entries)`: sorted names, a fixed `date_time=(1980,1,1,0,0,0)`,
  fixed attributes. Used by all SAGA exporters.
- The result carries `unverified: true` and a `verified_against: null` field until a round-trip is
  recorded.

**Tests.**
- Golden: a synthetic 3-entry journal → the expected DBF bytes (header byte 0 = 0x03, byte 29 = 0xC8,
  9 fields in order).
- A 628 + 4426 = 401 entry produces two rows under one NDP.
- A 49-character explanation → an error naming the entry.
- Run twice more than 1 s apart → identical bytes.

**Docs.** Guide "Export to SAGA / NextUp" (the NC target, the rehearsal warning); replace the
"format wasn't available" sentence.

**Risks / VERIFY.** Whether SAGA accepts `%` rows, diacritics and this grouping is unknown (§C-F1).
Rehearse on a scratch company (README §4 trigger 3).

**Done when.** Notes and close entries export as a deterministic NC DBF, clearly labelled unverified.

---

### WP-24 · SAGA take-on grid and open-items export

`status: todo · size: S · depends: WP-12, WP-13 · decision: **D4**`

**Why.**
- Migrating a client into SAGA means keying the *Preluare date contabile* grid and per-partner open
  items.
- The grid's `precedent` column semantics are disputed between two migrations (T3 runbook §1, §3, §7;
  T4 `map_sap_to_saga.py`) (§C-F3, §C-F4).

**Langclaw today.**
- `trial_balance_sheet` already computes the opening and total pairs (P:82-118).
- There is no SAGA-shaped output.

**Design.**
- Tool `accounting_saga_takeon(start_month)`: from the sheet of the month before, it writes
  `cont;denumire;tip;deb_init;cred_init;deb_prec;cred_prec` (CSV per `csv_dialect`, plus xlsx if cheap):
  - `init` = 1 January balance;
  - `prec` per D4;
  - `tip` A/P/B from the account class (pack or profile map);
  - the carve-out prefixes (partners, stock) are left to the open-items file.
- It also writes `solduri_terti.csv` from `outstanding()` and the WP-12 open items (partner code, tax
  ID, account, document, date, due, amount, side).
- Checks: each pair balances; the grid plus the carve-outs equals the langclaw trial balance.
- Also add the SAGA-style six-pair trial-balance profile (`solduri inițiale perioadă`, `total rulaje`)
  as an option of `accounting_trial_balance` (§C-F11).

**Tests.** Given a synthetic year with an opening and turnover, when the grid is built for July, then
each pair balances, and `deb_prec − cred_prec` matches D4's rule.

**Docs.** Guide "Working alongside SAGA".

**Risks / VERIFY.** D4, and whether the grid can be imported from a file (disputed).

**Done when.** A take-on grid plus open items can be handed to a person keying SAGA.

---

### WP-25 · FX: currency on lines, BNR rates, month-end revaluation

`status: todo · size: L (slices) · depends: WP-03, WP-04, WP-06, WP-15 · decision: **D6**`

**Why.**
- FX carries portfolio weight 2 (5/8 cases).
- The practice revalues foreign-currency monetary items at month end at the BNR rate of the last
  banking day: gain D item / C 765; loss D 665 / C item. This is done **after** checking the implied
  rate, and **before** the result is closed.
- One workspace records monthly revaluation as confirmed, citing the accounting regulation via a
  method note (C1 review C-4). The same client's audit still marks it `[de confirmat]` (§D-05, §D-06,
  §G-17, §G-18, §I-06).

**Langclaw today.**
- `journal_lines` has no currency (J:34-42).
- There is no 665/765 (grep).
- `documents.currency` exists, and the e-Factura parser keeps only `DocumentCurrencyCode`.
- The Limits say FX isn't covered (`docs/guides/accounting.md:738-739`).

**Design** (for D6-A).
- `journal_lines` gains `currency TEXT NOT NULL DEFAULT 'RON'`, `amount_currency NUMERIC(18,2) NULL`
  and `fx_rate NUMERIC(18,6) NULL`. `Journal.post` accepts them per line. RON remains the booked
  debit/credit.
- Rates: `langclaw/accounting/fx/rates.py` with a `RATE_SOURCES` registry. The first source is
  `manual` (per-tenant table `fx_rates(day, currency, rate, source, recorded_by)`); a BNR feed comes
  later, behind the network rule. `rate_on(currency, day)` uses the last banking day, with
  `next/prev_working_day` from the pack.
- Bank paths (lifting WP-04's guard): a foreign-currency movement is booked at the day's rate, with
  `amount_currency`. `reconcile_account` compares in currency units on the mapped analytic.
- Revaluation: `fx.revaluation_entry(positions, rates) -> entry` for the monetary accounts in the pack
  list (5124, 5314, 411x, 401x/404, 4551, 461x/462x, loans):
  - `diff = Σ amount_currency × rate(last banking day) − Σ RON`;
  - gain → D item / C 765, loss → D 665 / C item.

  Close step `fx_revaluation` (WP-15) runs before the VAT settlement and year end. The close refuses
  when a position exists and no rate is recorded.
- Guard `FX.IMPLIED_RATE`: the RON balance / currency balance must lie within ±X% (profile, default
  20) of the BNR rate, or the revaluation is refused: "reconcile the balance first" (§G-17).
- A 581 FX residue is cleared to 6651/7651 (T4 step 1), as an optional step.

**Slices.**
- **a.** Schema plus `post` accepting currency; reports unchanged.
- **b.** The rates table and the `rate_on` tool (`fx_rate_record`).
- **c.** Bank booking in currency, and reconciliation in currency.
- **d.** The revaluation close step with the implied-rate guard.
- **e.** Invoices in currency: e-Factura `TaxCurrencyCode`/rate, when present.

**Tests.**
- Given 1,000 EUR on 5124.01 booked at 4,970 RON and a rate of 5.00, when the month closes, then
  D 5124.01 / C 765 = 30.00 is posted before `year-end`.
- Given 5124 = 10,000 RON / 1,000 EUR and a rate of 5.0, then the refusal "implied rate 10.0 deviates
  >20%".
- Given an EUR fee of 10 at a rate of 5, when it is booked, then 50 RON, currency EUR,
  amount_currency 10.

**Docs.** A new guide section "Foreign currency"; remove the Limits lines; `CLAUDE.md` row.

**Risks / [de confirmat].** The revaluation scope (stock appears in one list; it is non-monetary, so
flag the conflict), the rate source and the frequency all come from the pack.

**Done when.**
- The ledger carries currency.
- Bank paths book at a rate.
- The close revalues in the practice's order, with the implied-rate guard.

---

### WP-26 · D406 / SAF-T: read, readiness, validate, then emit

`status: todo · size: L (slices) · depends: WP-09, WP-12; WP-25 for slice d · decision: —`

**Why.**
- D406 is a live filing in 5 of 8 cases (weight 4).
- Received D406 files are the richest takeover source: the chart with balances, the partners with
  balances, every GL line, invoices and payments.
- One workspace has a validator and a template spec for a third-party generator (T2 `saft/`)
  (§C-F6, §C-F7, §F-09).

**Langclaw today.** Absent: no `d406`/`saf-t` string in `langclaw/` (grep).

**Design (slices).**
- **a. Reader:** `langclaw/accounting/saft/parse.py` → Decimal dataclasses for the header, accounts,
  partners, transactions, invoices and payments. It reads the tree in CATALOGUES §C-F6 and does not
  persist `Header/Contact`. Tool `accounting_import_saft(bucket_key)` produces an **opening proposal**
  for human review:
  - closing D − C at the end date → `accounting_opening_balances` input;
  - customer/supplier balances → WP-12 open items;
  - a chain of monthly files → continuity check closing(M) = opening(M+1).
- **b. Readiness report:** `accounting_saft_readiness(period)` lists which D406 sections langclaw can
  fill today and what is missing (CATALOGUES §C-F6 gap map: line currency, tax codes, account master,
  partner city/country, journal code, invoice↔GL links, red storno).
- **c. Validator:** `saft/validate.py`, ported from the rules in CATALOGUES §C-F7 (account type
  vs balance side; partner city and account; journal (tx, line) unique; exactly one of D/C; payments
  indicator ∈ {D, C}). Shared by b and d.
- **d. Emitter:** `EXPORTERS["d406"]`, only after WP-25 and an XSD/validator trigger (README §4.6).
  Output `unverified` until validated by the official validator.

**Tests.**
- Slice a: a synthetic two-account, one-partner D406 fixture (written in the test, invented IDs) →
  opening proposal balances; negative natural-side balances move to the other side; a two-file chain
  with a gap → continuity error.
- Slice c: seeded violations → exact messages.

**Docs.** A new guide section "SAF-T (D406)"; `CLAUDE.md` row.

**Risks / [de confirmat].** Partner-ID prefix meanings (other than 00 and 09), TaxType/TaxCode
meanings, and whether 1.0 → 2.0 is a schema change are all `[de confirmat]`. Monthly vs quarterly
(`HeaderComment` L/T) comes from the filings rules.

**Done when.** Received D406 files seed openings and history, and readiness is visible per period.

---

### WP-27 · Bank statement parser registry: PDF text and payment-platform exports

`status: todo · size: M · depends: WP-04 · decision: —`

**Why.**
- Bank intake carries weight 5. The portfolio holds no MT940 or CAMT file, only text-layer PDFs (one
  per account and month) and a payment platform's activity sheet with Gross/Fee/Net columns (C4
  `extrase bancare*/`, C1 platform reports) (§C-F9, §C-F10).

**Langclaw today.** `parse_statement` sniffs only CAMT.053 and MT940, decodes UTF-8 and rejects
everything else (`langclaw/accounting/bank/parse.py:61-75`).

**Design.**
- `STATEMENT_PARSERS` registry (`name`, `sniff(data, filename) -> bool`, `parse -> Statement`). It
  keeps `camt053` and `mt940`, and adds:
  - `pdf_text:<profile>`, which uses `documents/text.py` extraction plus a per-bank profile. A profile
    is data: regexes for the period, the opening/closing labels, the row layout `date | description |
    debit | credit`, the number format (`99,999.99` vs `999,99`), and the date formats. Documents that
    aren't statements are rejected by detection.
  - `payment_activity`: an xlsx/csv sheet with the columns in §C-F10. Each row → a gross movement plus
    a fee movement (627). Currency per row.
- Every parser must pass `Statement.check()` (opening + Σ = closing). Otherwise the file is filed as
  `needs_review`.
- Explicit number parsing per profile. Never guess `,` vs `.` (the §F-05 trap).

**Tests.**
- A synthetic PDF text (a fixture string, not a real statement) → the statement balances.
- A seeded wrong closing → `needs_review`.
- `"99,999.99"` under the `en` profile → 99999.99.
- A payment-activity row with gross 100, fee 3 → movements +100 and −3, with the fee booked to 627
  (RON) or `fx_pending` (non-RON before WP-25).

**Docs.** Guide "Bank statements" (formats, adding a profile).

**Risks.** Layouts come from one bank. Ask whether the banks offer structured exports first (README
§4.4).

**Done when.** PDF statements and platform exports import through the same checks as CAMT.

---

### WP-28 · Outbound PII guard for shared projections

`status: todo · size: S · depends: — · decision: —`

**Why.**
- The practice's projection pipeline blocks, fail-closed, before any write that leaves the client zone.
  Its rules: amounts, the client's tax ID, the client's name words and a per-client denylist.
- Even so, PII leaked into generated digests, into a hand-made issue list and into a released method
  core (M0 `.claude/tools/tracker.py`, `.github/scripts/sync_issues.py`; §B-02, §B-11, §I-16).

**Langclaw today.**
- `PIIMiddleware` is a re-export for model I/O (`langclaw/middleware/guardrails.py:20-23`), not a
  publish gate.
- Review notices go to `reply_to` and to a configured review chat, which may be shared across tenants.
  Their content is unverified: check it first.

**Design.**
- `langclaw/accounting/guard.py: projection_guard(tenant, text, *, others=()) -> list[Violation]`:
  - the tenant's tax ID (digits, word-bounded);
  - the tenant name's words minus legal-form suffixes (SRL/SA/…), joined by `.{0,6}` and
    case-insensitive;
  - `profile.pii_terms`;
  - IBAN and tax-ID regexes;
  - optionally other tenants' names (`others`), to catch cross-tenant leaks.
- `redact(...)` masks instead of blocking, for previews.
- Apply it fail-closed:
  - wherever a notice or digest goes to a chat not owned by that tenant (the review channel);
  - the firm-level console rows when shown to a non-accountant role;
  - any future issue/tracker projection (WP-29).
- Guard terms live on the tenant, never in shared config: the denylist itself became PII in the
  practice.

**Tests.**
- Given tenant name "Alfa Beta SRL" and tax ID digits D, when a notice text "Alfa-Beta: invoice …"
  goes to a shared chat, then it is blocked or masked.
- Given the same text to the tenant's own chat, then it is allowed.

**Docs.** `docs/guides/tenants.md` "Privacy"; guide "In the console".

**Done when.** No tenant identifier reaches a shared channel unguarded.

---

### WP-29 · Engagement backlog, request lists by party, digest

`status: todo · size: L (slices) · depends: WP-09, WP-28 · decision: —`

**Why.**
- Most blocked items wait on a **document from a named party**. Across three method clients, 62
  blocker tags split: client ~40%, preparer ~18%, ANAF-SPV ~16%, bank ~15%.
- The practice runs one strict backlog with `blocked_by`, groups the open items by source, and ranks
  documents by how many items each unblocks. It reads that digest first every session.
- (M0 `tracker.py`; C1 `stare/DIGEST.md`; §B-01, §B-10, §H-02, §H-04, §F-13; J-08 weight 6.)

**Langclaw today.**
- `document_state` lists missing expected doc types (P:289-318), with no party, no dependency and no
  request text.
- Reminders exist only for receivables (`reminders_file`, T:1428-1491).

**Design.**
- Per-tenant table `engagement_items`: `id` (regex `^(C\d+|N\d+|P\d+|ANAF(/[A-Za-z0-9-]+)+|DOC/[A-Z]+/[a-z0-9-]+)$`,
  never reused), `kind`, `text` (no amounts: warn on the money regex), `blocked_by TEXT[]`, `source`
  (`client|bank|marketplace|previous_accountant|anaf|software|preparer|legal`), `due`, `state`
  (`todo|blocked|in-progress|done|n-a`), `substate`, `ref`, `evidence_key`, `recorded_by`,
  `updated_at`.
- Validation **blocks** writes: every `blocked_by` must exist, and the states must be valid. The
  practice's own non-blocking check let invalid rows through.
- Pure `engagement_digest(items) -> {actionable, blocked, by_source, ranked_inputs}`:
  - actionable = todo/in-progress with all deps done;
  - `ranked_inputs` = `DOC/*` ids by how many items name them in `blocked_by` (desc, then id), top 6;
  - deterministic, with no clock.
- Links: a `filings` row reaching `filed` closes its `ANAF/<form>/<period>` item. A WP-22 note posted
  with `item_id` closes its `N#` item.
- `expected_documents` entries may carry `{source, contact_ref, cadence, supplier_cui}`. Missing ones
  become `DOC/…` items.
- Tools: `engagement_items(filter)`, `engagement_update(id, …)`, `engagement_digest()`, and
  `documents_request(period)`, which groups the missing inputs by source and drafts one message per
  source through the optional mailer. It files a `document_request` document with `requested_on[]` for
  escalation.
- Console: an "Engagement" tab. The firm row adds `requests_by_party` and `next_input`.

**Slices.**
- **a.** Table, validation, digest and tools.
- **b.** `expected_documents` sources plus `documents_request`.
- **c.** Console tab and firm-row columns.
- **d.** Optional: a `wait_for` workflow node that resumes when a matching document is filed (design
  call, §H-04).

**Tests.**
- Given items N1 blocked by DOC/CLIENT/contract and N2, N3 blocked by DOC/BANK/statement-03, then
  `ranked_inputs[0] == "DOC/BANK/statement-03"`.
- Given `blocked_by` naming an unknown id, then the write is refused.
- Given a D300 filing recorded filed, then item `ANAF/D300/2026-05` becomes done with `evidence_key`.

**Docs.** A new guide section "Engagement backlog and requests"; `CLAUDE.md` row.

**Risks.** Contacts and party names are PII, so they are stored by reference in the tenant profile and
never in templates.

**Done when.** The per-tenant digest answers "what can move now, what waits on whom, which document
unblocks most".

---

### WP-30 · Onboarding importers registry and external trial-balance compare

`status: todo · size: L (slices) · depends: WP-13, WP-21 · decision: —`

**Why.**
- Takeovers and migrations are 4 of 8 cases (weight 8).
- Every migration job ended as a tested, config-driven adapter with blocking gates (T1 toolkit, T2
  pipeline, T3 script) (§C-F2, §C-F11, §C-F13, §C-F14, §F-02, §F-05, §F-08, §F-10, §F-11, §F-12,
  §D-04; J-06).

**Langclaw today.**
- `EXPORTERS` exists (`export/__init__.py:44-51`), but there is no importer counterpart.
- Nothing compares langclaw's books with an external trial balance.

**Design.**
- `langclaw/accounting/onboarding/` with `IMPORTERS` (registry + `make_importer`). Each importer
  yields normalized `TakeOn{accounts: {acct: {opening, turnover_debit, turnover_credit, closing}},
  partners: [...], open_items: [...], journal: [...], report: [checks]}`.
- Importers, one per slice:
  - `saga_tb_xls`: the SAGA balanță, headers located by label, both the 5- and 6-pair variants.
  - `saga_journal`: expands `%` compound rows into D/C pairs.
  - `expert_dbf`: needs the optional `dbfread` extra. It rebuilds the trial balance from the journal,
    sums multi-row stored openings, and uses stored openings only as a cross-check.
  - `sap_tb`: header detection, account normalization, `Totals = CF + Prev + Curr`.
  - `saft_d406`: reuses WP-26a.
- Every importer runs the structural checks from §F-12 (the TB checks 1–8 blocking, 9 INFO) and the
  continuity check opening(Y+1) = opening(Y) + movements(Y).
- `accounting_trial_balance_compare(period, bucket_key, profile)` runs "method A" (§D-04) against an
  external trial balance: per month, per account, with the longest-prefix match. Verdicts: OK /
  analytic reclassification only / REAL GAP, plus the row kinds MISMATCH / MISSING_IN_EXTERNAL /
  EXTRA_IN_EXTERNAL.
- Output feeds `accounting_opening_balances` (WP-12/13) as a **proposal** reviewed through
  `human_review`, never posted directly.
- Numbers: explicit decimal and thousands separators per profile (§F-05 trap).

**Tests.**
- Synthetic fixtures built inside the tests (tiny DBF/XLS written by the test; no real data):
  - an external TB with 401 = −100 and 4011 = +100 against a journal with neither → verdict "analytic
    reclassification only";
  - a seeded missing month → REAL GAP;
  - an Expert-like DBF with RON and EUR opening rows on one account → summed.

**Docs.** A new guide section "Taking on a client from another program"; `CLAUDE.md` row.

**Risks.** The source layouts are from the practice's software versions. Keep the header detection
tolerant, and fail loudly.

**Done when.** A takeover pack can be read into a reviewed opening proposal with its checks, and
langclaw can prove it agrees with SAGA's trial balance.

---

### WP-31 · Marketplace clearing-account settlement

`status: todo · size: M · depends: WP-16, WP-25, WP-27 · decision: —`

**Why.**
- A marketplace seller's payouts were parked on 462 and months were closed with no revenue, producing
  an artificial loss.
- Recognising only the net payout was rejected because it counts the fee twice (C1 audit) (§D-16,
  §G-05, §G-06).

**Langclaw today.**
- Bank matching excludes "fees netted out of a payment" (`docs/guides/accounting.md:746-748`).
- An unmatched receipt becomes an advance or stays open.
- There is no settlement parser.

**Design.**
- `profile.clearing_accounts: {payer_iban_or_name: "462.xx"}` routes the bank bookings of that payer
  to the clearing analytic.
- Tool `marketplace_settle(period, statement_key)` reads the platform's period statement (WP-27
  `payment_activity` or a settlement report) and proposes a WP-22 note:
  - gross sales: D 462.x / C 70x (the account is set per profile `[de confirmat]` 704 vs 707);
  - refunds: D 70x / C 462.x;
  - fee offset: D 401.platform / C 462.x;
  - taxes the platform collected are not revenue `[de confirmat]`.
- Control `CLEAR.RESIDUAL`: the 462.x balance stays near opening plus rounding, and a growing credit
  means unrecognised revenue. Also `401.platform` = 0.

**Tests.** Given payout 800, a fee invoice of 200 open on 401 and gross sales of 1,000, when
`marketplace_settle` runs, then 70x = 1,000 and both 401.platform and 462.x are 0.

**Docs.** A new guide subsection "Marketplace payouts".

**Done when.** Marketplace months close with gross revenue and a zero clearing residue.

---

### WP-32 · Review options, approver role and decision records

`status: todo · size: M · depends: — · decision: —`

**Why.**
- Judgement calls are presented as menus. Each row gives the thread, the decision, the alternatives
  (with the recommended one marked), the governing reference and what it unblocks.
- The chosen option is recorded with who confirmed it and when. Decisions are never tacit and never
  silently re-litigated (T2 `saft/docs/decizii.md` pattern, C3 review §5).
- Sign-offs need a named role, e.g. the operator plus the accountant (§B-08, §H-05, §I-M07).

**Langclaw today.**
- A review answer is only `approve | edit | reject` (`langclaw/workflows/graph/steps.py:36`).
- `HumanReviewNode` has `message, show, editable, on_reject` and no options
  (`langclaw/workflows/graph/spec.py:177-187`).
- `ControlPlane.answer_review` accepts any `by` (`langclaw/gateway/control.py:603-608`).

**Design.**
- `HumanReviewNode.options: list[{id, label, recommended?: bool, preview?: str}]`, where the preview
  is a state key, e.g. a WP-22 `journal_note_preview`.
- A new decision action, `"choose"`, with `data.option`. Branches can route on `review.option`. The
  run index stores the option next to `by`/`via`.
- `HumanReviewNode.approver_role: str | None`: the answerer's RBAC role must match, otherwise
  `ConflictError`-style refusal "needs <role>".
- Per-tenant `decisions` table: `id D#, subject, question, options JSON, how_resolved,
  impact_while_open, status open|taken|superseded, decision, rationale, rejected_alternatives,
  decided_by, review_key, at, superseded_by`. A `choose` answer on a review tagged
  `decision_id` settles it. Open decisions can block engagement items (WP-29).
- Telegram: one button per option (payload `wfr:c:<key>:<option>`). The console `_review_card`
  (`ui/app.py`) renders the options.

**Tests.**
- Given a review node with options a/b/c (b recommended), when answered `choose b`, then the branch on
  `review.option == "b"` is taken and the run index records the option.
- Given `approver_role: "accountant"` and an answer from a `client` role, then refused.
- Given a decision settled once, when a second `choose` arrives, then 409 naming who answered.

**Docs.** `docs/guides/workflows.md` (human_review options); the accounting guide (decisions).

**Done when.** Workflows can offer menus, record choices as decisions, and restrict who answers.

---

### WP-33 · Workflow templates pack

`status: todo · size: M (slices) · depends: per slice · decision: —`

**Why.**
- The practice runs eleven recurring human workflows. langclaw templates only the middle of one: post,
  report, close.
- The monthly loop template is missing from the console picker, even though the guide says to create
  it there (§H-01 … §H-11; J-09).

**Langclaw today.**
- The picker lists five templates (`ui/editor.py:87-95`). `accounting_month`, `monthly_advice` and
  `payment_reminders` are missing, contrary to `docs/guides/accounting.md` "The monthly loop".
- The cron default timezone is `"UTC"` (`langclaw/config/schema.py:516`).
- `tests/test_outlook.py:95-112` pins the tool order of `accounting_month`.

**Slices** (each: a `ui/templates/<name>.graph.json`, a `parse_graph_spec(..., available_tools=names)`
test, a picker row and a guide paragraph):
- **a.** Picker fix: add `accounting_month`, `monthly_advice` and `payment_reminders`. Docs: cron
  examples use `Europe/Bucharest` (`LANGCLAW__CRON__TIMEZONE`).
- **b.** `document_requests` (after WP-29b): `missing → draft (llm) → human_review → file`,
  cron-ready for day 1.
- **c.** `filing` (after WP-09): `prepare → human_review ("submit on SPV, then upload the receipt") →
  record`. It never submits.
- **d.** `correction` (after WP-10, WP-22): `propose → route (correction_route) → human_review →
  journal_note | reopen request`. In shadow mode it outputs `steps_for_human` (variants, SAGA menu
  path, verification) instead of posting.
- **e.** `takeon` (after WP-30): `import → checks → human_review (GO?) → opening → recheck →
  human_review (approver_role accountant)`, then store `go_live`.
- **f.** `client_onboarding`: `document_intake` over the received pack → profile draft (llm) →
  `human_review` (editable profile) → save, then requests (b).
- **g.** `year_end` (after WP-19): December month flow → `accounting_results` → `human_review`
  (adjustments) → filings for the annual return and statements (c).
- **h.** `books_review` (§H-07, §I-M07): `inventory → classify (llm, divergence classes i–v) →
  human_review with options (WP-32) → execute approved items`. The report is filed **before** any
  correction.
- **i.** `spv_sweep` / `deadline_digest` (J-09): the e-Factura sync plus the filings check around the
  18th–20th, and a D-5 digest across tenants sorted by due date.
- **j.** Extend `accounting_month` with `collect` (b) before `sync`, and `filings` (c) after `close`.
  Update the pinned test.

**Docs.** Each slice updates the guide and `CLAUDE.md` "Key File Locations" (templates list).

**Done when.** Each practice workflow in §H has a template or a documented reason why not.

---

### WP-34 · Rules and workflow version stamped on runs, entries and reports

`status: todo · size: S · depends: WP-06 · decision: —`

**Why.**
- The method pins its logic and law per client, and re-pins deliberately in one commit.
- A stale pinned document once made an agent regress a correct configuration.
- Run records must say which rules produced them (§B-05, §B-06, J-04, J-12).

**Langclaw today.**
- `WorkflowFiles` keeps versions but prunes them at 50 (`langclaw/workflows/files.py`, lens B).
- Run records don't store the workflow version.
- Reports carry no rules version.
- `Tenant.profile` is overwritten, with no history (`langclaw/tenants.py:123-145`).

**Design.**
- `rules_version = {langclaw: __version__, law_pack: id, law_pack_sha256}`, added to:
  - the month report;
  - `close-<n>.json` (WP-11);
  - exports;
  - `journal_entries.method_ref` (a new column).
- The run index stores `workflow_version`. Pruning never drops a version that a run references.
- `profile_history` rows (append-only) on each tenant save.
- `accounting_period_report(period, law_pack="")` recomputes with a given pack, for retro-review.

**Tests.** Given a close, when `close-1.json` is read, then it contains `rules_version.law_pack`. Given
a profile save, then a history row is appended.

**Done when.** Every output says which code and law produced it.

---

### WP-35 · Cross-border: OSS switch, VIES classification, D390 rows

`status: todo · size: L · depends: WP-06, WP-09, WP-17 · decision: —`
(Kind: trigger. Build after asking the user whether a cross-border client is active.)

**Why.** One takeover sold B2C services across the EU. The OSS threshold was crossed mid-year, which
switched taxation to destination from the crossing invoice onward. B2B customers had to be separated by
a VIES-valid number checked at the invoice date (C5 knowledge base; recorded as confirmed in C5 review
R1, citing the EU VAT directive) (§D-10, §E-9, §G-15).

**Langclaw today.** No OSS entry in `LIMITS` (`outlook.py:39-46`). No VIES, no per-country rates and
no D390/D398.

**Design.**
- `classify_customer(vat_id, day, vies=None)`: B2B when the ID is VIES-valid on that day (the
  consultation id is kept). The client is injectable and off by default.
- `oss_band(invoices, year, pack)`: sort the cross-border B2C invoices by date and accumulate the net
  EUR. The invoice that **crosses** the pack's threshold, and all later ones that year, are
  `destination` at full value. The next year is destination from the first euro. It returns
  `oss_crossed_on`.
- Pack rows: the OSS threshold, the country rate table and the D398/D390 rules.
- `filings_calendar` adds D390 (months with intra-EU B2B) and D398 (quarterly, even when nil).
- Reconciliation: invoice register vs OSS journal, split into B2B excluded / credit notes / B2B wrongly
  included / residual ≈ 0.

**Tests.** Given B2C invoices of 6,000 and 5,000 EUR against a 10,000 pack threshold, when the band is
computed, then the first is origin and the second is destination for its full 5,000 (§G-15).

**Done when.** OSS crossing and B2B/B2C classification are reproducible from invoices and pack data.
