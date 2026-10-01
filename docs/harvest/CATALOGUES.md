# Catalogues: reference material for the work packages

This is the condensed reference material that [`WORK_PACKAGES.md`](WORK_PACKAGES.md) cites. Sources
are given as workspace aliases (see [`README.md`](README.md) §6), and client names in paths are
written `<client>`. Everything legal is `[de confirmat]` unless a row says "recorded as confirmed",
and even those still need an accountant's sign-off before a law-pack row is marked `verified`
(WP-06). All numbers in scenarios are invented.

Sections:

- §C formats
- §D rules and controls
- §E declarations, thresholds and legal epistemics
- §G scenarios (synthetic test specs)
- §I failure modes and review methods
- §B method patterns
- §H workflows and message patterns
- §F code-asset porting notes

---

## §C · Format contracts

| # | Format | Direction | Carrier | Status | Used by |
|---|---|---|---|---|---|
| F1 | SAGA note-contabilă import (`NC_*.dbf`) | emit | dBase III, cp1250 | layout recovered, never accepted by SAGA | WP-23 |
| F2 | SAGA journal as stored/exported | ingest | CSV (BOM) / SAGA `.xls` | live | WP-30 |
| F3 | SAGA take-on grid (*Preluare date contabile*) | emit | xls/csv or keyed by hand | `precedent` disputed (D4) | WP-24 |
| F4 | SAGA partner import (`Furnizori_/Clienti_*.dbf`) | emit | dBase, cp1250 | VERIFY | WP-23/24 |
| F5 | SAGA invoice XML | emit | UTF-8 XML zip | exists in langclaw, never imported; the practice uses SAGA's native SPV import | — |
| F6 | D406 XML (`D406_<id>_<dd-mm-yyyy>_Inf.XML`) | ingest / emit | XML | live, validated | WP-26 |
| F7 | SAF-T xlsx template set (third-party generator) | emit | xlsx | live | WP-26 |
| F8 | ANAF receipt PDF | ingest | PDF with text layer | live | WP-09 |
| F9 | Bank statement PDF | ingest | PDF with text layer | live; no MT940/CAMT in the portfolio | WP-27 |
| F10 | Payment-platform activity export | ingest | xlsx | live | WP-27/31 |
| F11 | SAGA report pack (trial balance, journals, partners, aging, stock, assets) | ingest | `.xls` (BIFF) | live | WP-24/30 |
| F12 | SPV invoice register (practice template) | track | xlsx | the same template in 5 workspaces | WP-29 |
| F13 | Expert (VFP) dataset | ingest | VFP DBF | live | WP-30 |
| F14 | SAP TB / GL / vendor line items | ingest | xlsx / text | live | WP-30 |
| F15 | Round-trip loader (EXPECTED vs ACTUAL) | ingest | csv/dbf/xlsx | designed, never run on SAGA | WP-30 |

### C-F1 · SAGA NC DBF (T1 `Facturi SPV/2026/NC_2026.dbf`; T1 `expert-to-saga-toolkit/expert2saga/exporters/gl.py`)

- **Header:** byte 0 = `0x03` (dBase III, no memo); byte 29 (language driver) = `0xC8` (cp1250).
- **Fields, in order:**

  | Field | Type | Meaning |
  |---|---|---|
  | `NDP` | C10 | note number, the grouping key |
  | `DATA` | D | `YYYYMMDD` |
  | `CONT_D` | C20 | debit account |
  | `CONT_C` | C20 | credit account |
  | `SUMA` | N15.2 | RON, positive |
  | `CURS` | N15.4 | 0 if unused |
  | `SUMA_VAL` | N14.2 | 0 if unused |
  | `EXPLICATIE` | C48 | explanation |
  | `GRUPA` | C16 | always blank |

- **Analytics** go in the account string (`401.<SUPPLIERCODE>`).
- **Multi-line entries** become D/C pairs under one NDP, repeating the single-side account. For
  example, "628 + 4426 = 401" becomes 628/401 (net) and 4426/401 (VAT).
- **Builder controls:** Σ debit = Σ credit, and Σ credit on 401 = invoice gross.
- **Known defects in the source code (do not copy):**
  - `CURS` is always 0 (dead attribute test);
  - `SUMA_VAL` is always 0;
  - every posting gets its own NDP;
  - single-sided legs are silently skipped;
  - silent truncation to C48/C20;
  - one builder folds diacritics to ASCII while the toolkit keeps cp1250.
- **Acceptance:** none. The register shows 0 of 8 imported, and every round-trip gate is unticked.
  SAGA's own exports use the same lower-case field names (`ndp, cont_d, cont_c, suma_val, cod_valuta,
  curs, suma, tva, explicatie`), which corroborates the layout.

### C-F2 · SAGA journal as stored (C5 re-export of the SAGA database, UTF-8 with BOM)

- Columns: `ID_NOTA, VALIDAT, CONT_D, CONT_C, SUMA, COD_VALUTA, CURS, SUMA_VAL, TVA, EXPLICATIE, DATA
  (ISO), CATEGORIE, SURSA`.
- **Compound entries:**
  - header row: `CONT_D=<acct>`, `CONT_C='%'`, `SUMA` = total;
  - continuation rows: blank `CONT_D`, `CONT_C=<acct>`, partial `SUMA`, blank `VALIDAT`;
  - the continuations sum to the header;
  - one note can mix simple pairs and `%` groups.
- The reader expands each `%` group into D/C pairs.

### C-F3 · SAGA take-on grid (T4 screenshot of the menu; T3 runbook §1, §3, §7)

- **Screen:** start month and year, plus a currency selector.
- **Grid columns:** `Cont | Denumire | Tip A/P/B | Debit inițial an | Credit inițial an | Debit
  precedent | Credit precedent | Sold debitor | Sold creditor` (the last two are computed).
- **Buttons:** `Validez`, `N.C sd. inc. an`, `N.C rulaje prel.`, `Devalidez`, `Preluare automată…`.
- **Database fields:** `DEB_INIT, CRED_INIT, DEB_PREC, CRED_PREC` on the chart table. Opening balances
  are also stored as signed `SOLD` and `SOLD_VAL` per account and month.
- **Rules (T3, "verified against the SAGA C manual"):**
  - year-start cut: all four columns = closing balance;
  - mid-year cut: `inițial an` = 1 January balance, and `precedent` = **total sums** to the end of the
    previous month;
  - load classes 6/7, and do not roll 121;
  - D must equal C, or `Validare` blocks;
  - FX: enter RON first, then the currency amount on the same analytic.
- **Conflict (D4):** T4 sets `precedent` = previous + current turnover, **without** the opening. Its
  SAP files satisfy `Totals = CF + Prev + Curr` on every leaf row.
- **Disputed:** whether the grid can be imported from a file.

### C-F4 · SAGA partner import DBF (T1 `expert2saga/partners.py`)

- Encoding cp1250. SAGA de-duplicates by tax ID.
- **Furnizori:** `COD C8, DENUMIRE C48, COD_FISCAL C13, ADRESA C48, ANALITIC C16, ZS N3, BANCA C48,
  CONT_BANCA C36, FILIALA C36, GRUPA C16`.
- **Clienti:** `COD C8, DENUMIRE C48, COD_FISCAL C16, ADRESA C48, REG_COM C16, ANALITIC C16, ZS N3,
  DISCOUNT N5.2, EXTERN L, DELEGAT C36, BI_SERIE C2, BI_NUMAR C8`.
- `COD` is assigned sequentially.
- SAGA's internal partner table adds `TIP_TERT, TARA, LOCALITATE, IS_TVA, DATA_V_TVA, DATA_S_TVA,
  IS_EFACT, ID_EFACT, C_SAFT`. `C_SAFT` is probably the D406 partner id `[de confirmat]`.
- **Load order at take-on (T1 reference):**
  1. company;
  2. chart + analytics (one per IBAN and per currency);
  3. stock locations, then article types, then articles;
  4. partners;
  5. the synthetic grid;
  6. partner items per partner **and per document** (Σ = grid line);
  7. stock per article (Σ = 371/30x);
  8. fixed assets (Σ = 21x and 28x);
  9. payroll starting from an empty list.
- **Grid carve-out** (to avoid double counting): `401 403 404 408 409 411 4111 413 418 419 461 462
  371 301 302 303 308 381`. Classes 8/9 are memorandum.

### C-F6 · D406 as filed (structure of 8 SAGA-produced and about 12 Expert-produced files)

- **Root and header:**
  - Root `AuditFile`, namespace `mfp:anaf:dgti:d406:declaratie:v1`.
  - `AuditFileVersion` is `2.0` from SAGA and `1.0` from Expert. Whether that is a schema change is
    `[de confirmat]`.
  - `Header`: Company{RegistrationNumber, Name, Address, Contact, TaxRegistration, BankAccount},
    `DefaultCurrencyCode=RON`, `SelectionCriteria{Start, End}`, **`HeaderComment` = `L` (monthly) or
    `T` (quarterly)**, `TaxAccountingBasis=A`.
- **MasterFiles:**
  - `GeneralLedgerAccounts/Account{AccountID, AccountDescription, StandardAccountID (carries
    analytics), AccountType ∈ Activ|Pasiv|Bifunctional, AccountCreationDate}`, with one Opening and one
    Closing Debit/Credit balance;
  - `Customers` / `Suppliers{CompanyStructure{RegistrationNumber, Name, Address}, ID,
    SelfBillingIndicator, AccountID, balances}`;
  - `TaxTable{TaxType, TaxCode, TaxPercentage, BaseRate, Country}`;
  - `UOMTable`;
  - `AnalysisTypeTable` (a single placeholder);
  - `Products`;
  - `Owners`, `Assets` and `MovementTypeTable` are empty in monthly/quarterly files.
- **`GeneralLedgerEntries`:** `{NumberOfEntries, TotalDebit, TotalCredit, Journal{JournalID, Type,
  Transaction{TransactionID, Period, PeriodYear, TransactionDate, SystemEntryDate, GLPostingDate,
  CustomerID, SupplierID, BatchID, SystemID, TransactionLine{RecordID, AccountID, Analysis, CustomerID,
  SupplierID, Description, DebitAmount|CreditAmount{Amount, CurrencyCode, CurrencyAmount,
  ExchangeRate}, TaxInformation{TaxType, TaxCode, TaxAmount}}}}}`.
- **`SourceDocuments`:**
  - `PurchaseInvoices` / `SalesInvoices{Invoice{InvoiceNo, SupplierInfo, AccountID, InvoiceDate,
    InvoiceType 380, TransactionID, InvoiceLine{…, Quantity, UnitPrice, TaxPointDate,
    DebitCreditIndicator, TaxInformation{…, TaxDeclarationPeriod}}, InvoiceDocumentTotals}}`;
  - `Payments{Payment{PaymentRefNo, TransactionID, TransactionDate, PaymentMethod, PaymentLine{AccountID,
    partner, DebitCreditIndicator, PaymentLineAmount, SourceDocumentID}}}`.
- **Conventions and traps:**
  1. **Partner id** = a 2-digit prefix + an identifier. Seen: `00` + RO tax ID; `01` + ISO-2 country +
     VAT number; `03`/`08` + 13 digits; `04`; `06` + alphanumeric. A third-party guide says `09` + up to
     13 digits (first digit ≠ 0) for non-residents registered in RO. All meanings other than 00 and 09
     are `[de confirmat]`.
  2. SAGA writes **negative amounts** (red storno) in Debit/CreditAmount and on invoice and payment
     lines. It also puts wrong-side balances as a negative on the natural-side element.
  3. GL lines always carry `Analysis` and `TaxInformation` (TaxType `000`, TaxCode `000000` when there
     is no tax). Codes seen: TaxType `300, 301, 100010, 100020`; TaxCode `301104, 306319, 308302,
     380200`. Their meanings are `[de confirmat]`.
  4. Dates are ISO; amounts have 2 decimals; rates have 4.
  5. SAGA uses one JournalID; Expert uses short alphabetic codes.
- **Gap map vs langclaw:**
  - Fillable now:
    - transactions ← `journal_entries` (+ `created_at` as SystemEntryDate);
    - lines ← `journal_lines`;
    - account balances ← `trial_balance_sheet`;
    - partner balances ← `partner_balances`;
    - invoices ← documents `fields`;
    - payments ← bank entries. AccountID = the counterpart, not the bank. The indicator is the cash
      direction (D = receipt), and 531x = cash.
  - Missing:
    - line currency/amount/rate (WP-25);
    - tax type/code per line;
    - account master (description, type; the class fallback is a heuristic);
    - partner city/country (the UBL parser flattens the address);
    - journal code (derive it from the `bucket_key` prefix);
    - invoice↔GL and payment↔invoice links;
    - red storno (`journal_lines` has a CHECK for one positive side).
- **Companion files:**
  - `D406.txt` = `ok` (the validator's verdict);
  - a `.log` with timestamped steps, then `VALIDATION FOR TYPE [L|T]` and one `SECTION DETECTED` per
    section.

### C-F7 · SAF-T xlsx template contract (T2 `saft/spec/templates_spec.json`; `saft/pipeline/saft_validate.py`; chunks C11/C11b)

- **Sheet names:** each sheet name must equal the ANAF entity (`GeneralLedgerAccounts`,
  `GeneralLedgerEntries`, `Payments`, `Customers`, `Suppliers`, `Owners`, `PurchaseInvoices`,
  `SalesInvoices`, `Products`, `Stock`, `Assets`, `AssetTransactions`, `MovementsOfGoods`,
  `AnalysisTypes`). Otherwise the import fails. Column A is the join key.
- **Journal template, columns A–S:** transaction no., description, document date, system date, posting
  date, customer code, supplier code, currency code, journal code, account, line no., line description,
  debit, credit, currency amount, tax amount, tax amount in currency, tax type, tax code.
- **Rules:**
  - (tx, line) unique;
  - exactly one of debit/credit ≠ 0;
  - empty numerics are written as 0;
  - RON → currency amount = amount;
  - account type 1/2/3 = bifunctional/asset/liability: an asset holds only debit balances, a liability
    only credit balances;
  - a natural person cannot be a VAT payer; the VAT-payer flag is 1/2;
  - partner city and account are mandatory;
  - `Products` needs at least one row (a dummy row is allowed);
  - journal code ∈ {Jurnale Diverse, Jurnal Încasări, Jurnal Plăți};
  - payments: indicator ∈ {D, C}, and amount ≠ 0.
- **Spec shape:** `{file: {sheet_name, columns: [{col, name, required, type ∈
  text|date|decimal|integer}]}}`.
- **Trap:** the vendor guide inverts the "non-resident" flag. The Help sheets (1 = non-resident) win.

### C-F8 · ANAF receipt PDF (C4 receipts register; C1 `Client/declaratii/`)

- A text layer of about 300 characters. Labels: `Index încărcare` (the upload index), `numărul
  înregistrare`, `data` (dd.mm.yyyy), `perioada raportare`, `există erori validare`.
- Filenames vary by era: `Recipisa D<nnn>_<id>_<YYYY>_<MM>.pdf`,
  `<CO>_RECIPISA_D406_<MMYYYY>_<id>.PDF`, `D300_<id>_<YYYY>_<MM>.pdf`, and Expert's
  `dec_<form>_<L|T>_<n>_<year>.xml`.
- **Register columns (C4):** declaration, reporting period, registration no., upload index, filing
  date, validation, file.

### C-F9 · Bank statement PDF (one bank, C4)

- One text-layer PDF per account and month, named `Statements_<IBAN>_<from>_<to>_<CO>.PDF` or
  `<MMYYYY>_<IBAN>_CURRENT.pdf`.
- **Layout:**
  - statement number and period `dd/mm/yyyy - dd/mm/yyyy`;
  - opening balance;
  - rows `date | description | debit | credit`;
  - daily `RULAJ ZI` / `SOLD` lines;
  - `SOLD FINAL`.
- **Numbers and dates:** amounts `99,999.99`, with dates in three shapes. A non-statement PDF with
  `999,99` amounts sat in the same folder, so detection is needed.
- **SAGA's bank/cash journal export:** `data, sold_prec, incasari, plati, sold_zi, baza_tva_i, tva_i,
  baza_tva_p, tva_p, cont`.

### C-F10 · Payment-platform activity export (C1)

- Sheet `Download`, columns: `Date, Time, TimeZone, Name, Type, Status, Currency, Gross, Fee, Net, …,
  Transaction ID, Reference Txn ID, Invoice Number, Balance`.
- The fee is netted from Gross, so each row needs two movements: gross, plus the fee to 627.

### C-F11 · SAGA report pack headers (takeover "first-read" pack; headers at rows 5–11, located by label)

- **Trial balance**, RON **and** EUR separately, 6 pairs: `Solduri inițiale an | Solduri inițiale
  perioadă | Rulaje perioadă | Total rulaje | Sume totale | Solduri finale`, each split into
  `Debitoare/Creditoare`. langclaw's sheet has 5 pairs (`period.py:79-118`): it lacks *solduri
  inițiale perioadă* and *total rulaje*.
- **Journal register:** `Nr. crt | Data | Explicație | Nr. doc | Cont debitor | Cont creditor | Debit
  | Credit | Tip`.
- **Purchase/sales journal:** document (date, no.) | partner (name, tax id) | total incl. VAT | base
  | VAT | paid | chargeable (base, VAT) | not yet chargeable (base, VAT).
- **Supplier situation:** `Cod | Denumire | Neachitat inițial | Total intrări | Plăți | Neachitat`.
- **Customer aging:** `Nr. document | Data | Scadent | Zile scadență | ID încărcare SPV | Total |
  Neachitat final`.
- **Fixed-asset register:** `Nr. inv | Denumire | Clasa | Data intrării | Nr. doc | Valoare intrare |
  Durata funct. | Val. rămasă | Durata rămasă | Amortizare lunară | Amortizare înregistrată | Data
  ieșirii`.

### C-F12 · SPV invoice register (the same template in T1 and C1–C4)

- Columns: `Companie | Trimestru | Data facturii | Numar factura | Numele furnizorului | Suma net per %
  de TVA | Cota TVA | TVA | Gross | Status | Obs re status | Ordine`, one row per invoice and VAT rate.
- `Status` goes from "De înregistrat" to "Înregistrat în SAGA".
- **Validation:** net × rate = VAT to the cent; net + VAT = gross; invoice numbers unique; one company
  in scope.

### C-F13 · Expert (VFP) dataset (T1 source; Y1–Y3)

- **Headers:** VFP `0x30`, language driver `0x03` (cp1252, which lacks comma-below ș/ț). Read with a
  fallback cp852 → cp1250 → latin1, and log the undecodable bytes.
- **Journal** = `REGJURN.DBF` ∪ `Arhiva/AREGJURN.DBF`:
  - `DATA_DOC D, FELDOC C16, NRDOC C20`;
  - `CNTDEBIT/CNTCREDIT N10` (keys → `CONTURI.NRCONT` → `CONT C10` symbol, `TIPCONT` A/P/B,
    `COD_VALUTA`);
  - `SUMA`, `COTATVA C3`, `EXPLICATIE C254`;
  - per leg: partner `PRIMITOR` (debit) / `PREDATOR` (credit), plus currency, rate and currency amount.
- **Other files:**
  - `PARTENER`: `COD` (float-typed), `CODUNIC N13`, `STABILIT` (UE/NonUE), `COD_TARA`.
  - `SOLD0101`: yearly opening, with several rows per account (one per currency) that must be
    **summed**.
  - Also `NOTECONT`, `EXCONT` (bank/cash), `STERTI` (open items), `NOMMIF` (fixed assets).
- **Rules:**
  - rebuild balances from the journal, and use `SOLD0101` only as a cross-check;
  - keep **both** leg partners. The source reader kept `PRIMITOR or PREDATOR` and lost the credit-side
    partner.

### C-F14 · SAP exports (T2 adapters; T3/T4 scripts)

- **TB columns:** `Company Code | G/L Account | Description | Balance CF | Prev Periods | Curr Period |
  Totals | Balance`, each D/C, with `Totals = CF + Prev + Curr`. Leaf accounts match `d-dd-d-dddddd`;
  other rows are subtotals. Find the header by its `G/L Account` label.
- **Account normalization:** strip `-` and `.`, and take the first 4 digits. If the 4th digit is `0`,
  use the 3-digit synthetic; otherwise keep 4 digits. Unknown prefixes are marked CONFIRM.
- **Posting key → side:**

  | Keys | Side | Partner type |
  |---|---|---|
  | 1, 2, 4, 5, 9 | D | customer |
  | 11, 12, 14, 15, 19 | C | customer |
  | 21, 22, 24, 25, 29 | D | vendor |
  | 31, 32, 34, 35, 39 | C | vendor |
  | 40 / 50 | D / C | GL |
  | 70 / 75 | D / C | assets |

  An unknown key falls back to the amount's sign.
- **Payments:** a document is a payment when a line's account starts with `512, 531, 541, 542, 519,
  581`. The treasury line is the max |D−C|. The indicator is D when it is debited. 531 means cash.
- **Text numbers:** `14,075.41-` → −14075.41 (thousands `,`, trailing minus).
- **Vendor line items:** the document key is the join key; the reference field holds the real invoice
  number; types KR/RK/KG are invoices. One report has no document key and cannot be joined.

### C-F15 · Round-trip loader (T1 `expert2saga/roundtrip.py`)

- **Header matching:** case-insensitive candidate headers, first hit wins:
  - account: `cont|simbol|symbol|cont_simbol`;
  - debit: `sold final debitor|sfd|debitor|sold_debitor|debit`;
  - credit: the same, for the credit side;
  - partner: `cui|cod_fiscal`.
- **Balances:** `signed = debit − credit`, summed per key, keeping |v| ≥ 0.005.
- **Verdicts:** PASS / MISMATCH / MISSING_IN_TARGET / EXTRA_IN_TARGET at tolerance 0.01. Partners are
  keyed by tax ID, falling back to the name.
- **Outputs:** `result.json`, `discrepancies.csv`, `reconciliation.md`.
- **Traps:** the source parser reads `99,999.99` as 99.99999, and `1.234` as 1.234. Use explicit
  separators per profile.

### C-F16 · langclaw's emitted formats vs practice conventions

- langclaw CSVs: UTF-8 without BOM, `;` delimiter, `.` decimals, ISO dates, English headers.
- Practice files: often `,` delimiter, Romanian headers, UTF-8 **with** BOM, and `dd.mm.yyyy` in
  SAGA-facing files.
- Proposal: a per-tenant `csv_dialect` (delimiter, BOM, header language, date format).

---

## §D · Rules and controls

Conventions: `bal(a)` = debit − credit; ε = 0.01 RON unless stated.

### D-01 · Control registry (T1 `expert2saga/reconcile.py`, `docs/RECONCILIATION_CONTROLS.md`; T2 asset control)

- **Record:** `Control{id, check, target, actual, diff, status PASS|FAIL|INFO, blocking}`, with
  `hard_failures = count(blocking ∧ FAIL)`.
- **Snapshot:** the report carries a snapshot hash. Export refuses unless the *current* snapshot has
  `hard_failures == 0`. Hash the content, not the file sizes; the source hashed sizes, which is a bug.
- **Blocking controls:**

  | Control | Rule |
  |---|---|
  | C0 | classes 1–7: \|ΣD − ΣC\| < ε |
  | C0b | class 8/9 net = the acknowledged memorandum amount. The source defaults the target to the actual, so it always passes: require an explicit acknowledgement. |
  | C1 | continuity (D-04) |
  | M1.1 | payables sub-ledger tie (D-02) |
  | M1.2 | receivables sub-ledger tie (D-02) |
  | M1.8 | Σ 4428 of open VAT-on-collection documents = bal(4428) |
  | M3.1 | asset register ↔ GL |
  | M4.2 | Σ net pay = bal(421) |

- **M3.1 in detail:** categories by the first two digits: 20/21/23 cost, 26 financial (outside the
  register), 28 depreciation, 29 impairment. Σ register cost = GL cost, and Σ register depreciation =
  GL 28. Expected differences: 231 and assets the GL expensed. Never load at gross.
- **Advisory controls:**
  - bank vs statement;
  - Σ VAT registers = bal(4426/4427);
  - 4423/4424 = the last D300;
  - intra-EU = the last D390;
  - partner de-duplication;
  - stock Σ qty × price = 3xx;
  - bal(444) = the last D112.
- **Sign-off:** every blocking control passes, and every advisory FAIL has a disposition and an owner.

### D-02 · Sub-ledger ties and opening ownership (T1 toolkit; T3 runbook §7–8)

- **Tie:** `Σ_partner bal == bal(synthetic)` per group, blocking at ε.
  - Payables: 401, 403, 404, 408, 409.
  - Receivables: 4111, 411, 413, 418, 419.
  - 461/462 are excluded as "wash" accounts, which fail spuriously per partner. The client's own
    control doc still lists them (D2).
- **Keys:** partners are keyed by an internal code, not the tax ID.
- **Take-on:**
  - load partner balances as open invoices dated before the take-on, so gross two-sided balances
    survive (an advance plus a payable on one supplier);
  - never load both the synthetic and the detail;
  - an intercompany receivable at entity A should equal the payable at entity B (advisory).

### D-03 · Never plug; handle off-balance separately (T1 P3, P8)

- The trial balance never gets a balancing line.
- Classes 1–7 must balance within ε.
- The class 8/9 net must equal the acknowledged `{amount, account_hint}`. A drift means a new
  one-sided posting.
- Class 8 loads in its own step, possibly against an `899x` contra (D7).

### D-04 · Continuity and movement tests (T1 reader; T2 "method A"; T4 pre-close checks)

- **Continuity:** `opening[Y+1] == opening[Y] + mov(Y)` per account. Count the |Δ| > 0.01 and keep
  `max_abs_diff`. Stored openings with the same (year, account) are **summed**, because overwriting
  drops the FX leg.
- **Method A**, per month and account:
  - `TBmov(M) = closing(M) − closing(M−1)`, with `closing(0)` = the year's opening;
  - `GLmov(M)` = Σ signed GL, attributed to the **longest TB account that is a prefix**;
  - Δ = GL − TB;
  - verdicts: all |Δ| < ε → OK; rows differ but ΣΔ (plus unassigned) < ε → "analytic reclassification
    only"; otherwise → REAL GAP, stop;
  - check each month in isolation, because YTD hides offsetting errors;
  - TB rows repeating the same account are summed.
- **Structural TB checks** (EPS 0.005; 1–8 blocking, 9 INFO):
  1. Tot = CF + Prev + Curr (Dr and Cr);
  2. Balance = Dr − Cr;
  3. Σ leaf current Dr = Cr;
  4. Σ leaf balance Dr = Cr;
  5. each aggregate = Σ its direct children;
  6. one company, no duplicate accounts;
  7. CF + Prev(P) = Tot(P−1) per leaf, and a new account has CF + Prev = 0;
  8. CF constant across months;
  9. accounts appearing or disappearing.

### D-05 · Bank per currency (C4 bank reconciliation; C1 audit)

1. Every statement IBAN maps to exactly one ledger analytic. An unmapped IBAN blocks.
2. **Implied rate:** `bal_RON(analytic) / bal_currency(statement)` must lie within the BNR range for
   the period. If it doesn't, fix the balances **before** revaluing.
3. Ledger debit turnover = 0 while the statement shows inflows → an unbooked receipt.
4. Compare in currency units, and in RON after revaluation.

### D-06 · FX revaluation (C1 audit and review; Y2 annual preparation; T4)

- **Scope:** monetary foreign-currency items: 5124/5314, 411x, 401x/404, 4551, 461x/462x, loans. One
  list also includes stock 357/371, which is non-monetary: flag it as a conflict.
- **Rule:** `diff = bal_currency × BNR(last banking day) − bal_RON`; a gain is D item / C 765(1), a
  loss is D 665(1) / C item.
- **Order:** before closing 6/7 into 121. Do not revalue again at a year-boundary take-on.
- **581 residue** from FX transfers → 6651 (debit) or 7651 (credit).
- **Errors seen:** only the bank was revalued, not the receivables; six months without revaluation.
- **Certainty:** monthly revaluation is recorded as confirmed in C1's review, citing the accounting
  regulation (OMFP 1802/2014 pct. 325) via a method note. The same client's audit marks it
  `[de confirmat]`.

### D-07 · Non-payer reverse charge (Y1 VAT re-verification; C1, C2)

- **Template per invoice** (VAT = base × the rate valid on the invoice date):

  | Debit | Credit | Amount |
  |---|---|---|
  | 628x | 401 | base |
  | 4426 | 4427 | VAT |
  | 635x | 4426 | VAT |
  | 4427 | liability (D3: 446x or 4423) | VAT |

  4426 and 4427 return to 0. Domestic purchases are booked gross to 6xx with no 4426. The returns are
  D301 and D390 code S, never D300 `[de confirmat]`.
- **Detection rules** for a non-payer:
  - (a) D4427/C4426 "VAT close" → self-assessed VAT cancelled;
  - (b) any D4424 → a fictitious recovery;
  - (c) D4423/C4424 → a debt extinguished without payment;
  - (d) D4426 against a domestic supplier → a forbidden deduction.
- **Year-end fix:** D 635/628 / C 446x.
- **Cross-check:** opening + Σ self-assessed − Σ payments = bal(liability).
- Late-payment accessories from the ANAF statement go to 6581/4481.

### D-08 · 4424 layered by origin year (Y2 §8)

- Each D4424 opens `{origin_year, amount}`, and each C4424 consumes the oldest layer first.
- A layer expires on 31 Dec of `origin_year + 5`, as recorded, citing the fiscal procedure code art.
  219. The start date is disputed.
- Report the layers, what expires within 12 months and what has lapsed. Action: a refund request on
  D300 before the oldest layer expires.

### D-09 · VAT ties (C4 reconciliation TB × D300 × purchase journal; T1 controls)

- rd.30 ≈ debit turnover of 4426.
- rd.16/19 ≈ credit turnover of 4427.
- rd.45 ≈ bal(4424).
- **Chain:** `rd.41(M) == rd.45(M−1)` exactly.
- **Tolerance:** D300 is in whole lei, so ≤ 1 leu per row. The cumulative drift is not booked.
- The opening 4423/4424 = the last filed D300 (advisory).
- **M1.8:** Σ unpaid VAT share of the open VAT-on-collection documents = bal(4428).
- Row numbers depend on the form version `[de confirmat]`.

### D-10 · OSS and B2B/B2C (C5 knowledge base and scripts)

- **Classification:** a VAT number that is VIES-valid **on the invoice date** (keep the consultation
  id) → B2B: reverse charge, 0%, D390, outside OSS. Anything else → B2C.
- **The switch:**
  - sort the B2C intra-EU sales (net, EUR, all countries) by date and accumulate them;
  - the invoice that **crosses** the threshold, and everything after it that year, is taxed at
    destination on its full value;
  - the next year is at destination from the first euro.
- **Gap decomposition** (register vs OSS journal): B2B correctly excluded / credit notes (gross vs net)
  / B2B wrongly included / residual ≈ 0.
- **Signals:** the partner-master VAT flag, the SAF-T partner prefix, and the country cross-checked
  against the address.
- An offline VAT-number checksum (one member state's mod-11 rule) exists in the source. It never
  replaces VIES.

### D-11 · Take-on shapes (T3 runbook; T1 SCENARIOS, P13)

- **Three blocking D=C gates:** the 1 January opening, the cumulative position (opening + YTD) and the
  closing.
- **Year-end cut (31 Dec):** classes 6/7 = 0, and no second revaluation.
- **Mid-year cut:**
  - 6/7 **must** carry the YTD result (a zero net means "you were handed a closed file");
  - you need both `initial an` and `precedent`;
  - 121 is loaded as found. A pending prior-year 121 → 117 is a normal entry after the take-on;
  - 581 ≈ 0;
  - payroll and VAT YTD seeds are required.
- **`go_live` ≠ cutover:** periods before go-live were filed from the old system, and their
  generators are locked.
- **Two-sided partner balances** (debit and credit on one supplier) hide an advance: split it to 409.

### D-12 · Close order (C3 review; C1 audit; T4)

- **Order** (each step a no-op when it doesn't apply): documents → treasury → stock → depreciation →
  payroll → **FX revaluation** → **VAT close** → **profit tax (quarter end)** → **6/7 → 121** → trial
  balance → returns (human only).
- **Automated checks (T4):**
  - movement on 121 = expenses closed − revenues closed;
  - each P&L account is closed by exactly its balance;
  - Σ monthly results = total 121 movement.
- **Sequence:** close chronologically and reopen in reverse.

### D-13 · Profit tax (C1 audit; C3 review citing a method note; Y2)

- `tax_Q = max(0, rate × taxable_YTD) − Σ tax already booked this year`.
- `taxable_YTD = result YTD + non-deductible − deductions − non-taxable income − loss carried forward`.
- Post D 691 / C 4411 at quarter end, before 6/7 → 121. A loss gives tax 0, but the return is still
  filed.
- Micro: D 698 / C 4418 on revenue.
- 121 → 117 happens the following year, after the shareholders' decision.
- Legal reserve: 5% of gross profit, capped at 20% of share capital, as recorded in Y2
  `[de confirmat]`.

### D-14 · Storno or reopen: route by filing status (C1 audit and review; C3 review)

- **Current year:** a return covering the period has been filed (D300/D301/D100, or the D406 of its
  quarter) → storno dated today, one line per affected month. Otherwise → reopen in reverse order and
  re-close.
- **Filing status unknown** → the current date (the safe default).
- **Prior year:**
  - statements not filed → restate the prior year;
  - statements filed → correct in the current year (1174 if material) and file rectifying returns
    `[de confirmat]`.
- **Cut-off:** a prior-year service goes to 408 in the prior year, with a storno in the current year.

### D-15 · Balance rules (Y2; C1, C2 reconciliations)

- **Must be zero:**
  - 473 at every close;
  - 4426/4427 after a payer's settlement;
  - 6/7 after year end;
  - 121 after the result carry;
  - P&L accounts in a closed month under monthly closing.
- **Wrong side or stale:**
  - 411 credit;
  - 409/419 carried from earlier periods;
  - 542 past its term;
  - 461/462 residuals;
  - equal-and-opposite mirror pairs (e.g. a tax account at −X and a sundry creditor at +X).
- **Revenue gap:** class-6 turnover > 0, class-7 = 0 and bank inflows > 0.
- **Balance-sheet gap:** equity ≠ assets − liabilities. In one case the gap was exactly the 581 residue
  plus one unclosed P&L account.

### D-16 · Marketplace clearing (C1 audit and reconciliation)

- **Entries:**
  - payout: D 5124 / C 462.x;
  - fee invoice (reverse charge): D 628 / C 401.platform;
  - offset: D 401.platform / C 462.x;
  - monthly gross revenue from the platform statement (≈ net + fee, adjusted for refunds and
    settlement lag): D 462.x / C 70x.
- **Controls:** bal(462.x) ≈ opening + rounding, and bal(401.platform) = 0.
- Taxes the platform collects are not revenue `[de confirmat]`. 704 vs 707 is `[de confirmat]`.
- Net-only recognition was rejected, because it double-counts the fee.

### D-17 · Partner identity (T1 P9; C1; C5)

- Key partners by an internal code, and de-duplicate on (country, tax id). Foreign partners with a
  blank ID never merge.
- The client's own VAT ID on received invoices must equal the tenant's tax ID. In one case a platform
  invoiced another tax code of the same business.
- Partner roles come from journal usage: a leg on 411/4111/413/418/461/419 → client; on
  401/403/404/408/409/462 → supplier. Both are allowed.

### D-18 · Completeness of expected documents (C4, C2 reconciliations)

- The SPV list minus the journal = invoices still to book. A credit-note-plus-reissue pair nets to 0.
- Recurring suppliers have a cadence (monthly rent, a quarterly fee). A missing invoice in the cadence
  is flagged.
- The supplier aging per partner must equal bal(401).

---

## §E · Declarations, thresholds and legal epistemics

**Certainty legend:**

- **NCLIB-verified**: recorded as verified 2026-07-15 in OR's deadline calendar, citing the practice's
  note/deadline library.
- **recorded as confirmed**: a workspace review confirmed it, citing the source shown.
- **practitioner**: stated in a workspace without a cited check.
- **dc**: `[de confirmat]`.
- **disputed**: workspaces disagree.

Nothing here is verified by this pass.

### E-1 · Declaration catalogue

| Form | What | Who files (as recorded) | Periodicity | Deadline rule (as recorded) | Certainty | langclaw today |
|---|---|---|---|---|---|---|
| D300 | VAT return | VAT payers | monthly / quarterly | 25th of the following month | NCLIB-verified | present (`outlook.py:64-66`), no weekend roll |
| D394 | domestic supplies/acquisitions | VAT payers | per VAT period | **disputed:** 30th of the following month (NCLIB) vs "with D300", the 25th (method checklist) | disputed | 25th (`outlook.py:66-67`) |
| D301 | special VAT return (reverse charge) | non-payers with reverse-charge acquisitions | monthly, only months with operations | 25th of the following month | dc | absent |
| D390 | intra-EU recapitulative (code S = services received) | anyone with intra-EU B2B | monthly, only months with operations | 25th of the following month | NCLIB-verified (term) | absent |
| D406 | SAF-T | all double-entry small taxpayers, incl. non-payers, from 2025 | = VAT period; a non-payer files quarterly | last calendar day of the following month; no grace from 2026 | NCLIB-verified (term); applicability recorded as confirmed in C5 | absent |
| D100 (profit) | profit-tax prepayment | profit-tax payers | quarterly; Q4 settled via D101 | 25th of the month after the quarter | NCLIB-verified | absent |
| D100 (micro) | micro tax | micro | quarterly | 25th after the quarter; Q4 recorded as due 25 June | practitioner; **conflicts** with langclaw's 25 January | 25th of the next month |
| D100 (non-resident WHT) | withholding on non-residents | payers of such income | monthly | 25th | dc | absent |
| D101 | annual profit tax | profit-tax payers | annual | **disputed:** 25 March (several workspaces) vs 25 June (NCLIB; adopted in one review) | disputed | absent |
| SF (F10/F20/F30/F40) | annual financial statements | all | annual | 31 May (NCLIB, citing a 2025 ministry order) vs "about 150 days" | disputed | absent |
| D205 | dividend WHT informative | distributors | annual | last day of February | practitioner | absent |
| D207 | non-resident informative | payers to non-residents | annual | around 28 February | dc | absent |
| D112 | payroll | employers | monthly | 25th | NCLIB-verified | present (`outlook.py:68-69`) |
| D398 | OSS special return | OSS-registered | quarterly, **also nil** | end of the month after the quarter | NCLIB-verified | absent |
| F4109 | unused fiscal cash devices | device holders | monthly, only unused devices | 20th | NCLIB-verified | absent |
| D700 / art. 317 | special VAT code for intra-EU services | non-payers buying/selling intra-EU services | event (before the first acquisition) | — | dc | absent |
| 010 / 096 | registration mentions / removal; VAT cancellation | event | event | — | dc | absent |
| e-Factura | invoice transmission (B2C from 2025, only supplies whose place of supply is RO) | RO-established taxable persons regardless of VAT status | continuous | 5 working days from issue | NCLIB-verified; scope corrected in a review | not checked |

**Zero-filing rules as recorded:**

- D100 is filed even at zero (practitioner).
- D398 is filed with no operations (NCLIB-verified).
- D301/D390 are filed only for months with operations (dc).

**Deferral:** a deadline on a non-working day moves to the next working day (NCLIB-verified). Example
recorded: the 25th on a Saturday → the following Monday.

### E-2 · Thresholds, rates, limits

| Item | Value (as recorded) | Certainty | langclaw today |
|---|---|---|---|
| RO standard VAT | 19% → 21% from 2025-08-01 | recorded as confirmed in C5 review R1, citing Law 141/2025 | matches (`vat.py:26-35`) |
| Reduced rates | 5/9 → 11%; a transitional 9% (housing) until 2026-07-31 | not detailed in the workspaces | `vat.py`, no source field |
| VAT-registration threshold | 300,000 RON → 395,000 RON **from 2025-09-01** | recorded as confirmed in C5 R1, citing OG 22/2025 | year granularity applies 395k to all of 2025 (`outlook.py:39-41`) |
| Micro rate 2025 | 1% if cumulative revenue ≤ 60,000 EUR, else 3% from the quarter of crossing | recorded as confirmed in C5 R1 (adversarial round) | flat `micro_rate` (`results.py:20, 63-64`) |
| Micro rate 2026 | single 1% | recorded as confirmed (practitioner web check), citing OUG 89/2025 | matches the default |
| Micro revenue ceiling | 100,000 EUR for 2026 | recorded as confirmed in C5 | matches (`outlook.py:44-45`); the 2025 row of 250k EUR (`outlook.py:42-43`) has **no workspace support** |
| EUR rate for the micro ceiling | not recorded | dc | profile `eur_ron` |
| Profit tax | 16%, quarterly on cumulative profit less tax booked | cited through a method note (NCLIB), "verified 2026-07-16" | 16% on YTD, no "minus booked" (`results.py:19, 69`) |
| Dividend tax | 10% for 2025 distributions; 16% from 2026-01-01 | recorded as confirmed in C5 R1, citing Law 141/2025 | absent |
| OSS threshold | 10,000 EUR/year EU-wide, B2C cross-border, net, cumulative; a switch, not a band; 2-year test; no reset | recorded as confirmed in C5 R1 | absent |
| Foreign VAT rates for OSS | a per-country table; one member state's island list changed in 2026 | recorded as confirmed with a dated update | absent |
| Loss carry-forward | 7 years | practitioner | absent |
| VAT-refund prescription | 5 years (fiscal procedure code art. 219); year-N layer expires 31.12.(N+5) | practitioner, **with a dispute** on the start date | absent |
| Fines | SAF-T 1,000–5,000 lei; e-Factura B2C 1,000–2,500 lei from 2025-07-01 | practitioner | absent |
| Legal reserve | 5% of gross profit up to 20% of share capital | practitioner | absent |
| Cash limits | no statutory values in the workspaces | — | profile-only (correct design) |

### E-3 · Obligation-state model (C1, C2, C4, T1)

- **Id** `ANAF/<form>/<period>`.
- **Lifecycle** `{todo, blocked, in-progress, done, n-a}` × `anaf:{efectuat, overdue, due, soon-due,
  de-confirmat}`. `efectuat` is allowed only with a receipt.
- **Three axes per row:** must file (fiscal vector) × filed (receipt) × books ready.
- **Receipt fields:** see §C-F8.
- **Invariants:**
  - a receipt with a different tax ID does not belong to the client;
  - never re-file or regenerate a period that has a receipt;
  - "a later receipt in a series implies the earlier ones" is **inference**, not proof;
  - filing is strictly human.

### E-4 · Legal epistemics

- **M0:** every legal value is cited point-in-time as `[act, article, in force <period> · file ·
  status · corpus:<tag>]`. The corpus is pinned by a lock (`corpus_release`, `commit_sha`,
  `pinned_at`). While the lock is unpinned, every rate, threshold or term is "provisional", and
  milestones are never keyed to fiscal due dates.
- **OR/NCLIB workaround:** a dated citation (`verified_at`) plus a freshness pre-flight before each
  client round: header dates, open "legislative change" items, a monthly routine.
- **C5 review R1:** each claim gets a verdict CONFIRMED / REFUTED / INCONSISTENT / UNVERIFIABLE.
  Round 2 has verifiers try to overturn each correction, then a coherence sweep finds fixes that did
  not propagate.
- **Recorded corrections:**
  - "OSS extension from 2026-07-01" refuted: minor changes come in 2027, the major extension on
    2028-07-01;
  - D301 2026 rate 19% → 21%;
  - "power of attorney max 3 years" refuted;
  - the e-Factura B2C scope was limited to supplies whose place of supply is RO.

  All of these stay practitioner-level until a law-pack sign-off.

---

## §G · Scenarios as synthetic test specs

These are abstracted from real cases. Every number is invented. Flags:

| Flag | Meaning |
|---|---|
| TK | takeover |
| NVP / VP | non-VAT payer / VAT payer |
| MKT | marketplace |
| XB | cross-border |
| FX | foreign currency |
| MIG | migration |
| YE | year end |
| FIL | filings |
| MIC / PRF | micro / profit regime |
| DOR | dormant |

Langclaw verdicts are as of HEAD `9441429`.

| ID | Flags | Given | When | Then | Verdict today | WP |
|---|---|---|---|---|---|---|
| G-01 | TK FIL | `{tax_regime: profit, vat_payer: false, intra_eu: true}`, no receipts | outlook / filings for 2026-03 | D100-profit Q1, D406 Q1, D301, D390 listed as `unknown`/`upcoming`, never filed | partial | WP-09 |
| G-02 | TK MIG | opening with a 4111 open item {cui X, doc A, 1000} | `partner_balances`, `receivables_overdue` | X shows 1000, and doc A is listed | unsupported | WP-12 |
| G-03 | MIG | `{5121: 300, 1012: -300, 8035: 50}` | opening | classes 1–7 posted, 8035 kept as a memo, TB balanced | unsupported | WP-13 |
| G-04 | TK DOR NVP YE | non-payer, 2026-01-01 balances {4428: 25, 627: 10} | report | anomalies "VAT account at non-payer", "P&L carried into new year" | partial | WP-14 |
| G-05 | TK MKT NVP PRF FX | 3 receipts booked 5124/462, no class 7 | close | refused / warned "receipts on 462, no revenue" | unsupported | WP-14/31 |
| G-06 | MKT XB NVP | payout 800, fee invoice 200 open on 401, gross sales 1000 | `marketplace_settle` | 70x = 1000; 401-platform and 462 at 0 | unsupported | WP-31 |
| G-07 | TK VP PRF | an unmatched domestic credit, no sales invoice within ±60 days | report | `unbooked_income_candidate` plus a request "sales invoice" | partial | WP-14 |
| G-08 | TK MKT XB | tenant `tax_ids: [A, B (vat-only)]`, a UBL invoice to B | `efactura_sync` | filed `in` with `finding: second_tax_id` | unsupported | WP-17 |
| G-09 | FX TK | EUR CAMT with a −10 "comision" line | `bank_import` | not booked as RON: `fx_pending`, or booked at rate × 10 with the currency kept | **conflicts** (bug) | WP-04/25 |
| G-10 | TK PRF | 1171 = 300, 461 = 5000 | decision menu for 461 | the dividend variant is `infeasible: distributable 300 < 5000` | unsupported | WP-22/32 |
| G-11 | VP YE | 4424 layers {2021: 800, 2025: 50}, today 2026-10-01 | outlook | "2021 layer prescribes 2026-12-31: request refund" | partial | WP-18 |
| G-12 | TK XB VP | a 4111 credit with no partner; an opening 4428 = 60 with no open document | report | `unallocated` row; "4428 without open VAT-on-collection invoices" | partial | WP-12/20 |
| G-13 | NVP XB FIL | non-payer, AE invoice base 100 at 21% | post the 4-line template | accepted; `d301.vat = 21`, deductible 0 | **conflicts** | WP-01/16 |
| G-14 | XB NVP | 5 accruals on 635/446 | reverse via notes, with a reason | 446 goes debit; "overpayment to recover" | unsupported | WP-22 |
| G-15 | XB MIC NVP FIL | B2C invoices to one country totalling 12k EUR, in date order | thresholds / OSS band | `oss_crossed_on` = the crossing invoice's date; later ones "needs destination VAT" | unsupported | WP-35 |
| G-16 | MIC YE FIL | quarterly revenue crossing the pack threshold in Q4 | quarterly results | Q4 at the higher rate; booked-vs-computed difference flagged | unsupported | WP-19 |
| G-17 | FX TK | 5124 = 10,000 RON / 1,000 EUR, rate 5.0 | revaluation | refused "implied rate 10.0 deviates >20%: reconcile first" | unsupported | WP-25 |
| G-18 | FX TK YE | EUR and USD statements, no mapping | `bank_import` / report | "unmapped currency account USD: map to 5124.xx" | unsupported | WP-03 |
| G-19 | FIL TK | 2026-03 closed; D406 2026-Q1 filed | reopen 2026-03 | refused (post a dated correction, or file a rectification) | **conflicts** | WP-10 |
| G-20 | YE PRF FIL | an invoice dated 2026-02 for services 2025-06..12; 2025 not filed | context | `cutoff_warning` proposes the restatement pair | unsupported | WP-22 (trigger) |
| G-21 | PRF VP | a sales invoice with lines 4111 D / 418 C | `journal_post` | accepted, "clears prior-year accrual", no revenue | unverified | WP-22 |
| G-22 | PRF XB FIL | 457 debited by a payment with no 446 withholding line | report | finding "dividend paid without withholding record" | unsupported | WP-14 (later) |
| G-23 | VP NVP | a 19% invoice dated after the change, its credit note, a 21% reissue | `vat_summary` / check | the 19% pair nets to 0; the check flags the 19% invoice | **handled** | — |
| G-24 | TK | 446 = −400, 462.02 = +400; 473 = 1000 unmoved 90 days | report | anomalies: a mirror pair, "stale suspense 473" | partial | WP-14 |
| G-25 | TK FIL | 7 SPV invoices, 5 posted, a filed D406 | report | 2 blockers plus "filed D406 covers period: rectification likely" | handled (the book case) | WP-09/29 |
| G-26 | MIG XB | two foreign invoices with empty IDs and different names | `partner_balances` | 2 partners | unsupported | WP-17 |
| G-27 | MIG YE | `{401/X: D 100, C 110}` at take-on | take-on validation | "two-sided partner balance: split 409" | unsupported | WP-13 |
| G-28 | YE PRF VP FX | January–November closed, no December documents | outlook for 2026-07 | "2025 not closed: D101/SF overdue"; the December close blocked | partial | WP-09/33g |

---

## §I · Failure modes and review methods

| ID | Failure class | How it was caught | langclaw today | WP |
|---|---|---|---|---|
| I-01 | Non-payer reverse charge booked with payer mechanics (VAT cancelled, fictitious recovery or set-off, forbidden deduction) | year-end VAT re-verification (Y1) | **conflicts:** `period.py:212-216, 229-230` | WP-01/16 |
| I-02 | `vat_payer` default contradicts itself | code reading | **conflicts:** `checks.py:88` vs `period.py:326`, `outlook.py:64` | WP-01 |
| I-03 | "Balanced/validated" taken for "complete": TB with December empty; SAF-T with a month missing; bank account with zero receipts | Y2, C1 review, C4 reconciliation | absent | WP-14 |
| I-04 | Undated entry skips the closed-month lock and vanishes from reports | code reading | **conflicts:** `journal.py:117-123` | WP-02 |
| I-05 | Cross-client contamination: a client's list carrying another client's row; another company's database in a year-end folder; archive restored into the wrong client | structure scan, a hash match | partial: `archive.py:116` | WP-05 |
| I-06 | FX compared across currencies; revalued before reconciling | C1 review precondition; T1 adversarial review | **conflicts:** `tools.py:439-443` | WP-03/25 |
| I-07 | Profile assumption contradicted by evidence ("no sales" vs a bank inflow; micro vs no employees) | C4 checklist annex; C1 | absent | WP-07/14 |
| I-08 | Validated ≠ filed; the reopen-vs-storno choice depends on filed | C3, C1, C2 reviews | absent | WP-09/10 |
| I-09 | Silent row drop gives a false green (the validator skipped malformed rows and a "template" guess skipped validation) | C2 maintenance review | partial: `bank/reconcile.py:33`, `journal.py:324` | WP-14 |
| I-10 | Blank-ID foreign partners collapse; no sub-ledger tie | T1 adversarial review | partial | WP-12/17 |
| I-11 | Plugged or one-sided openings; class 8; booking synthetic and detail both | T1 principles | partial: `period.py:151-152` | WP-12/13 |
| I-12 | Stale artefact outlives its correction: corrected sources beside stale PDFs; a pinned stale doctrine regressed a config; reopened months keep old close files | C5 R3, C1 rollout | partial: reports stay in `reports/<p>/` | WP-11 |
| I-13 | Target format assumed, never rehearsed | T1 adversarial review | present only as a documented limit | WP-23/24 |
| I-14 | Re-filing or re-transmitting periods before go-live | T1 | absent: `efactura/sync.py:32-43` has no floor | WP-13 |
| I-15 | Deadlines from memory; no weekend roll; non-payer forms missing | C3, C1 reviews | partial: `outlook.py:52-54, 64` | WP-06/09 |
| I-16 | PII in generated projections; another tenant's name passes a per-tenant denylist | C1 rollout reports | unverified | WP-28 |
| I-17 | Wrong close order; FX and tax steps missing | C3 review | partial: `tools.py:602-635` | WP-15/19/25 |

**Review methods, as workflow patterns:**

- **I-M01 · Adversarial review.** Three lenses (accounting, operations, fiscal) look for failures "in
  practice". Each defect records a severity (BLOCKER…LOW), the concrete failure, a resolution folded
  into the plan, and a tick when checked against the data. There is also a "confirmed correct, do not
  fix" list. Every BLOCKER ends as fixed, a rehearsal gate, or a named sign-off. As a template: 3
  subagent nodes → merge → one `human_review` per BLOCKER.
- **I-M02 · Multi-auditor integrity review.** Auditors split by domain: figures recomputed from raw
  data, law against sources, the process DAG recomputed, identity against registry documents.
  Verdicts: CONFIRMED / REFUTED / INCONSISTENT / UNVERIFIABLE (with the steps to close it). A conflict
  between auditors goes to the recompute. Round 2 has verifiers try to overturn each correction, a
  coherence auditor look for partial propagation, and an action sweep. Output schema: `{claim, verdict,
  evidence, correction, propagated_to[]}`. Applies to law-pack audits (WP-06e).
- **I-M03 · Fresh-eyes review.** It covers what nobody audited: client-facing text vs the canonical
  position (overpromises, contradictory figures, a lapsed deadline described as "later"), staleness,
  hygiene. As a gate: a pre-send check of mailer drafts against the tenant's state.
- **I-M04 · Independent recompute / two-path closure.** One figure is reached by two independent paths
  (ledger vs obligations − payments), and the two must be equal (blocking).
- **I-M05 · Rehearsal + round-trip gate.** EXPECTED vs ACTUAL, exported back from the target, gives
  PASS / MISMATCH / MISSING / EXTRA step by step. Export refuses unless `hard_failures == 0` for the
  current snapshot.
- **I-M06 · No-information-lost proof.** File-count accounting (1:1 / absorbed with a pointer /
  excluded), cell-identical regeneration and a test count.
- **I-M07 · Report before correction.** The report is committed before any fix. Divergence classes:
  - (i) the client departs from the reference note;
  - (ii) correct but uncited (the dominant class);
  - (iii) a gap in the library;
  - (iv) an error in the library;
  - (v) blocked on a human, a document or a human filing.

  Each decision gets a menu (alternatives, governing note, the document that settles it). The human
  arbitrates, then execution runs.
- **I-M08 · Append-only correction + quarantine.** Corrections are annotated, never silently
  overwritten. Stale files are quarantined under a banner, and decision logs are "never tacit".

---

## §B · Method patterns

- **B-01 · Engagement backlog as the single source of truth (M0 tracker).**
  - Row: `ID | text | blocked_by | from whom | due | state | ref`.
  - IDs: `C#` finding, `N#` accounting note, `P#` assumption, `ANAF/<form>/<period>`,
    `DOC/<SOURCE>/<slug>`. Never reused, and no PII in an ID.
  - State: `todo|blocked|in-progress|done|n-a`, plus an optional `saga:{da,de,conf}` (booked / to book
    / to book after confirmation), `anaf:{…}` or `needs:<source>`.
  - Rules: every `blocked_by` id must exist; a money pattern in a row is a warning (move it to `ref`);
    a done item migrates to the "done" section in the same change.
  - The digest is deterministic and uses no clock: actionable, blocked, grouped by source, and inputs
    ranked by how many items they unblock (top 6).
  - A reporting taxonomy that fits: S1 booked · S2 to book with certainty · S3 filed · S4 due, unfiled
    · S5 to book after confirmation.
  - **Lesson:** the practice's own check was non-blocking, so invalid rows merged. Validation must
    block writes.
- **B-02 · One writer, generated projections, a fail-closed guard.**
  - The projection is one issue per item, with a hidden id marker. Sync is idempotent: create what's
    missing, patch diffs, close orphans, dry-run mode.
  - The guard runs before any write and blocks: amounts; the tax ID; name words joined by `.{0,6}`
    minus legal-form suffixes; a per-client denylist.
  - The denylist itself became PII, so the terms belong on the tenant record.
- **B-03 · Storno-only correction** of closed periods, dated today. A project that touches a frozen
  period posts its correction current-dated. See D1.
- **B-04 · Sealed closes.** Close = an immutable named point; approval = a reviewed diff; no force
  push; correction = storno; audit = author + time + hash. Projects get their own freeze point.
- **B-05 · Pinning.**
  - Lock fields: `state` (unpinned / pinned / stamped), release, commit, pinned-at, source.
  - Invariants: the mount equals the lock, and a re-pin moves both in one change. The release register
    is append-only.
  - Lessons: pinned content needs a machine-readable "superseded-by" pointer; tooling copied per client
    drifts (a path-filter bug was fixed three times).
- **B-06 · Unpinned law as a governance state.** Every legal value is provisional, with a banner at
  session start; items touching an unconfirmed claim get a label.
- **B-07 · Value provenance and an independent verifier (T2 C12/C13).**
  - Each value is *from source*, *derived* (documented, deterministic) or *external* (left blank,
    listed in a gaps report).
  - The model orchestrates and scripts write the values. The source is never overwritten. Merges use
    exact keys.
  - The verifier recomputes from the source:
    1. row counts;
    2. ΣD = ΣC;
    3. **Σ(D + C) of the output = Σ|amount| of the source**;
    4. accounts ⊆ the source's accounts;
    5. line numbers unique;
    6. mandatory numerics present;
    7. sheet names;
    8. treasury on payments;
    9. a dummy product row.

    Tolerance 0.01; any FAIL stops delivery.
  - Export is refused without a passing reconcile marker for the current snapshot hash, and re-runs
    are byte-identical.
- **B-08 · Decision records.**
  - Taken: `# | subject | decision | rationale | consequence`.
  - Open: `# | subject | what must be decided | how it gets settled | impact while open`.
  - The log is canonical and an issue is only a mirror. Record the rejected alternatives, and use
    `superseded by` rather than re-litigating silently.
  - Principles are written as statement → why → where enforced → violation symptom.
- **B-09 · A filing closes by its receipt, not by its date.**
- **B-10 · Deterministic rehydration.** A session-start hook, read-only and always exiting 0, prints
  the schema check, the digest and the legal status. Trigger prompt: "Where are we?" → context,
  actionable set, most-unblocking documents.
- **B-11 · PII zoning is an absence rule.** Identifying data never sits outside the client zone. Leaks
  were seen in backlog headers, commit messages, hand-made issues and frozen archives. Erasure must
  reach the archives too.
- **B-12 · Onboarding against a golden reference.**
  - Birth: mount the core, write thin loaders, fill only the client delta, run the full checklist on
    the new instance.
  - Bootstrap: verify → reconcile (divergence → action table) → start. Report and propose; never
    rewrite.
- **B-13 · Module library.**
  - A module = a template with `{{PLACEHOLDERS}}` · a pointer to the instance · internal notes never
    sent · a data map · a generative prompt plus a modularity test · two-way traceability.
  - Only complete modules are admitted, and they are cited by number, never by path.
  - A chunk header: What / Does / For / Prerequisites / Postconditions / Implementation / Status.
- **B-14 · Archive freeze.** An archive is never updated or cited. Anything needed from it is ported
  clean, with a list of which archived claims are wrong.

---

## §H · Workflows and message patterns

| ID | Workflow | Trigger | Human gates | langclaw today | WP |
|---|---|---|---|---|---|
| H-01 | Monthly close, end to end | new month's batch / cron | review, filing, seal | partial (`accounting_month`) | WP-15/25/33j |
| H-02 | Source request and chase loop | missing expected documents | who to ask, send | partial (`document_state`) | WP-29b/33b |
| H-03 | Client takeover / onboarding | new mandate | consent, POA, partner sign-off | absent | WP-33f |
| H-04 | ANAF access as a dependency graph | takeover | client authenticates the POA; a human files | absent | WP-29d |
| H-05 | Decision menu and arbitration | an item needing judgement | choose an option | approve/edit/reject only | WP-32 |
| H-06 | Correction routing (dated in period vs current) | error after close or filing | pick the route; post in the software | partial | WP-10/33d |
| H-07 | Review campaign ("report before correction") | a scheduled round | arbitration | absent | WP-33h |
| H-08 | Migration / take-on cutover with GO/NO-GO | software change | GO/NO-GO, dual sign-off | minimal | WP-30/33e |
| H-09 | e-Invoice worklist × ground truth × software status | a worklist arrives | verify in the software | partial | WP-30 |
| H-10 | Year end: close → FS → annual return | December closed | tax result, FS signature, filing | partial | WP-33g |
| H-11 | Filing submission and receipt | a return prepared | strictly human submit | absent | WP-09/33c |

**Key sequences.**

- **H-01 monthly close:**
  1. collect sources into a new dated batch, never edited;
  2. record invoices and statements;
  3. reconcile: bank per RON and FX account; purchase/sales journals ↔ D300; 401/411 = open items;
     FX revaluation;
  4. closing entries: VAT settlement, then 6/7 → 121; check the trial balance;
  5. returns, human only; save the receipts;
  6. export the registers and seal the month.
- **H-03 takeover:**
  1. intake the received documents: founder ID, founding act, registration certificates ("check
     current"), bank and contact;
  2. a "to obtain" list by party:
     - client: power of attorney, the previous accountant's contact;
     - previous accountant: last closed TB, tax situation;
     - internal: the price offer;
  3. a "to verify" list: VAT code vs tax ID, stale administrator data, regime vs current rules;
  4. **two keys:** a consent note, which unlocks the handover, and a notarised POA with an SPV mandate,
     which unlocks ANAF access;
  5. the handover request, sendable only once consent is signed. It asks for: TB, ledger, journals and
     a software backup; filed returns **with receipts**; the fiscal vector; the payroll file and
     register contract; the fixed-asset register; SPV correspondence; and who files last year's FS;
  6. set up the software, populate the backlog, generate the digest.
- **H-04 dependency graph:**
  - Nodes have semantic ids (`core.poa`, `f150.submit`, `spv.approved`), `in`/`out`, a value or a
    pending input, and a status (known ✓ / blocked ⚠ / generated ◻).
  - The graph is acyclic, and its sinks are approvals, not submissions.
  - A shared core feeds parallel leaves, and the POA node has alternative sub-paths.
  - A "frontier" table lists each pending root, what it blocks, and the single input that unblocks the
    most. Recorded durations are `[de confirmat]`.
- **H-06 correction routing:** returns not filed → unlock in the software, post dated in the month,
  re-close. Filed → never unlock; post current-dated. Unknown → current-dated.
  - The human step guide per correction: why; amounts; variant A (now) / B (conditional) with menu
    paths; verification ("the account card shows X"); dependencies; order.
- **H-08 cutover:**
  - T-7: probe, and identify who filed.
  - T-5: hash-stamped freeze plus reconcile; the accountant signs the prior close.
  - T-4: rehearsal on a test company → GO/NO-GO.
  - T-3: dry run.
  - T-2: legal archive.
  - T-1: backup (the rollback anchor).
  - T-0: load, with a backup after each module ties out.
  - T+1: reconcile, sign, set `declaration_cutoff = go_live`, lock earlier generators.

  Software operations are not idempotent: restore the last green backup before a retry. Each module
  gets a completion record: anchors (hash, backup id), step log, controls, exceptions register with
  owners, declaration ownership by period, and operator + accountant sign-off.
- **H-09 worklist reconciliation:**
  1. tie each row (net + VAT = gross);
  2. match against ground truth and the software's status: posted / not posted / **contradiction**;
  3. contradictions → a quick human check in the software;
  4. assess the impact on returns already filed;
  5. produce a "to record" table with the dating gate.
- **H-10 year end**, in lanes (accountant / software / ANAF): A record December → B close (revaluation,
  VAT, 6/7 → 121) → C December returns → D annual tax (fiscal register, adjustments, carried loss,
  legal reserve, 691 = 4411) → E final TB + FS → F cross-checks (assets = liabilities; FS result =
  121) and the annual return filed.
- **H-11 filing:** prepare the XML (tool) → validate (external) → sign and submit on SPV (**human
  only**) → archive the receipt (upload → tool) → close the item.

**Message patterns.**

- **P-1 · Modular message.** A template with `{{PLACEHOLDERS}}` in the client's language; a pointer to
  the single ready-to-send text; **internal notes never sent** (legal basis, strategy, price rationale);
  a placeholder → data-source map; and a generative prompt that rebuilds the module for any client or
  country.
- **P-2 · Send package with a "blockers before send" table:** `# | blocker | type (internal decision /
  data / client) | resolved by | can send without? (no / partial / yes)`, then a conclusion naming the
  hard blockers. Approval stays disabled while hard blockers are open.
- **P-3 · Question tracker by addressee:** `Q# | question | arose at | priority | status | addressee`.
  Only client-addressed questions go into the client message.
- **P-4 · Steps grouped by actor:** "First, we do / Then, you do / Then, we do", ending with "your only
  in-person step is …". Legal citations and jargon stay internal.
- **P-5 · Hypothesis-framed questions:** "blank sheets ≠ no activity" is split into (a) no activity /
  (b) activity not invoiced / (c) invoiced but not sent. It is asked as support, not reproach, and
  followed by a completeness check (bank receipts ↔ invoices issued).
- **P-6 · Status digest with two audiences:** context in ≤ 5 lines; actionable now; blocked, grouped
  by source; the next inputs ranked by what each unblocks. langclaw's `advise` node already separates
  the accountant `status` from the client `summary`.
- **P-7 · Handover pair:** the consent note must be signed before the handover request is sendable.
- **P-8 · A human step guide for external software:** see H-06.

---

## §F · Code-asset porting notes

Nothing ports as-is: every source uses float money, and some embed client data or absolute paths. Port
the algorithms, re-implement with `Decimal`, and write synthetic tests.

| Source (alias) | What | Verdict | Target WP |
|---|---|---|---|
| T1 control engine + snapshot marker | control records, `hard_failures`, an export gate bound to a snapshot | **adapt:** hash the content, not the sizes; make C0b's acknowledgement explicit | WP-08 |
| T1 Expert reader: TB from journal, continuity, summed stored openings | reader + checks | **port** (optional `dbfread` extra) | WP-30 |
| T1 DBF I/O | read honouring the language-driver byte; write cp1250, remove before rewrite | adapt: log decode losses, error on truncation | WP-23 |
| T1 NC exporter / per-invoice NC builder | layout constants, grouping | adapt: fix CURS/SUMA_VAL, group by entry, no silent skips, Decimal | WP-23 |
| T1 round-trip reconciler | EXPECTED vs ACTUAL with a header-candidate profile | **port**, with explicit number separators | WP-30 |
| T1 partner handling | code assignment, role from journal usage, `extern` flag | adapt | WP-17/30 |
| T2 SAP adapters | account normalization, posting keys, payments, text numbers | **port** as pure functions | WP-30 |
| T2 validators + spec conformance harness | D406 template rules; spec ↔ template ↔ output tests | **port** the validators; adopt the conformance pattern for SAGA XML now | WP-26 |
| T2 GL↔TB "method A" | monthly movement tie, longest-prefix, residual verdicts | **port** | WP-30 |
| T2 deterministic save | sorted zip entries, fixed timestamps | **fix, then port**: it leaks the workbook's modified time, so pin document properties | WP-23 |
| T2 non-destructive regeneration and fill-empty-only merge | refuse to overwrite human input; fill only empty cells; ambiguous keys excluded | adapt as a rule for document field enrichment (machine fills nulls only) | WP-30 (note) |
| T3 take-on script | layout autodetect, three D=C gates, mid-year detector, fail-closed self-test | **adapt** into the opening proposal; drop its client mapping table | WP-13/30 |
| T4 pre-close TB checks | 9 structural checks | port the checks, not the file (dead paths) | WP-30 |
| M0 tracker trio | schema validation, digest, PII-guarded projection | adapt the digest and guard algorithms; do not port the Markdown parser | WP-28/29 |
| C5 OSS scripts | cumulative band, island scan | **drop:** keep only the rules (§D-10) | WP-35 |

**Test patterns worth copying:**

- **(a)** Tiny synthetic binary fixtures written by the test itself, with edge rows: a one-sided class-8
  leg; RON and EUR opening rows on one account.
- **(b)** Golden reproduction of a delivered file.
- **(c)** Known-divergence pins: a heuristic's disagreements must be exactly a listed set.
- **(d)** A fail-closed self-test against known totals.
- **(e)** Idempotence: the second run changes nothing.
- **(f)** Seeded-error localization: a mutated export yields the exact discrepancy keys.
- **(g)** Run-twice identical bytes, with a gap longer than 1 s.
- **(h)** Spec conformance.
