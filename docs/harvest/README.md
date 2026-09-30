# Accounting harvest: implementation plan

This folder is the plan for bringing what an accounting practice learned in its own working
repositories into langclaw's Romanian accounting module. Ten "lens" passes read the practice's
private workspaces: client catch-up files, software migrations, year-end rescues, a method core and a
fleet orchestrator. Each pass pulled out one kind of object: formats, rules, filings, code, scenarios,
workflows, failure modes, portfolio patterns, plus a map of langclaw itself. This pass merged the lens
findings into **work packages (WPs)** and re-checked every "langclaw today" claim against the code on
branch `claude/fervent-gates-9q1f3l` (HEAD `9441429`). It then dropped or corrected what the code
disproved. Nothing here comes from client data. Scenarios and numbers are synthetic, sources are cited
by workspace alias (§6), and every legal value is `[de confirmat]` unless §6 says otherwise.

Files:

- `README.md` (this file): protocol, summary, index, decisions, triggers, rejections, provenance, log.
- [`WORK_PACKAGES.md`](WORK_PACKAGES.md): one section per WP, in execution order.
- [`CATALOGUES.md`](CATALOGUES.md): the reference material the WPs cite (formats, rules, filings,
  scenarios, failure modes, method and workflow patterns, code-porting notes).

---

## 0. Protocol for the implementing pass (read first)

You implement this plan in langclaw **one work package at a time**.

1. **Read first.** Read §0–§3 of this file, then the repo's `CLAUDE.md` and `AGENTS.md`. Their
   principles and conventions win over anything here: big picture first, design the developer/user
   surface first, be langclaw-native, centralize over scatter, don't cap, red/green TDD.
2. **Pick the next WP.** Take the first WP in §2 whose status is `todo` and whose `depends` are all
   `done` or `n-a`. If the WP carries a **decision** flag (§3), ask the user first. Offer the listed
   options, record the answer in the WP's "Decision" line and in §3, then continue. Never pick an option
   yourself.
3. **Re-verify the premise.** Each WP's "Langclaw today" bullet gives `file:line` evidence from the
   harvest date. The code may have moved, so check again. If the gap is already closed, set the status
   to `n-a`, log the evidence in §7 and move on. If the premise is partly wrong, fix the WP text first.
4. **Red → green → refactor.** Write the failing tests under "Tests" first, using synthetic
   Given/When/Then data. Then make the smallest change that passes, then clean up. Run, in order:
   - the targeted tests;
   - `uv run pytest tests/ -v` (Postgres-backed tests need `LANGCLAW_TEST_POSTGRES_DSN`, see
     `tests/test_accounting.py:141-142`, plus `uv sync --all-extras` for asyncpg/boto3/moto);
   - `uv run ruff check . --fix`;
   - `uv run ruff format .`;
   - `uv run pre-commit run --all-files`.
5. **Docs in the same PR.** Update what the WP names under "Docs": the `docs/guides/accounting.md`
   section and its **Limits** list, and the `CLAUDE.md` "Key File Locations" row. When the WP changes a
   tool list, also update `docs/guides/tenants.md` or `documents.md`.
6. **This plan in the same PR.** Set the WP's row in §2 to `done` with the PR link, update the status
   line in `WORK_PACKAGES.md`, and add one line to §7 (date · WP · PR · what changed · surprises).
7. **One WP per PR.** A WP marked **slices** ships as several PRs (`WP-nn.a`, `WP-nn.b`, …), and each
   slice leaves `main` green.
8. **Size guard.** Sizes: S ≈ under a day and a few hundred lines; M ≈ 1–3 days; L = several PRs. If a
   WP grows past 2× its size, stop. Split it here into lettered slices and ship the first one.
9. **No client data, ever.** Tests use invented numbers and invented tax IDs (generate valid check
   digits in the test). Do not copy anything from the practice workspaces into code, tests, fixtures,
   docs or commit messages. This includes names, tax IDs, IBANs, invoice numbers and amounts. This repo
   is public.
10. **Law is data, not code.** Rates, thresholds, deadlines and treatments go through the law pack
    (WP-06). Each row carries its source, `verified_at` and certainty. Anything no accountant has
    confirmed stays `[de confirmat]` in docs **and** in tool outputs. Never upgrade a certainty on your
    own. Where CATALOGUES §E records two conflicting values, ship the row as `disputed` with both.
11. **Honest limits.** Keep wired separate from inert. An exporter or parser that was never
    round-tripped against the real target says so in its output (`unverified: true`). Name the
    limitation in the guide's Limits section.
12. **Sources.** Sources are cited by workspace alias (§6). You should not need them: each WP carries
    its spec, formula or steps. If a WP is ambiguous, ask the user; do not guess from the alias.

---

## 1. Executive summary

- The portfolio is mostly **takeover / catch-up, migration into SAGA and year-end rescue**. The steady
  monthly close that langclaw's module assumes is the minority case. SAGA C is the book of record
  everywhere, and SAP and Expert appear only as sources.
- Five **live bugs** come first (WP-01–05):
  - non-payer reverse-charge VAT is netted to zero, and the `vat_payer` defaults contradict each other;
  - an undated entry skips the closed-month lock;
  - an EUR statement is compared with RON ledger balances, and all non-RON IBANs share one 5124;
  - EUR payments, fees and cash transfers are booked as if they were RON;
  - `restore_books` ignores which client an archive belongs to.
- Next come cheap **foundations** (WP-06–11): a sourced law pack, a typed profile with provenance, one
  controls registry that gates close **and** export, a filings register with receipts, filed-aware and
  non-destructive corrections, and sealed closes.
- Then come **ledger controls** (WP-12–20): partner-aware openings and sub-ledger ties, take-on shapes,
  completeness checks, declared close steps, non-payer reverse charge, partner identity, 4424 layers,
  quarterly income tax and VAT-return ties.
- The **large features** follow, ordered by portfolio weight (WP-21–35): shadow mode alongside SAGA, a
  general correction-note tool, a SAGA note-contabilă export, a SAGA take-on grid, FX, D406/SAF-T, bank
  PDF and payment-platform parsers, a PII guard, an engagement backlog with request lists, onboarding
  importers, marketplace settlement, review options and decision records, workflow templates, version
  stamping, and OSS/VIES.
- Seven **decisions** (§3) gate specific WPs. Ask before starting them.
- Cash register, reminders and payment batches are already built and are rare in this portfolio. No
  WP extends them.

---

## 2. Work-package index (execution order)

Kinds: **explicit** = a spec or rule to implement; **implicit** = a need visible across workspaces;
**trigger** = worth exploring before committing. Value and size follow the portfolio weights (lens J).

| ID | Title | Kind | Value | Size | Depends | Status | Decision? |
|---|---|---|---|---|---|---|---|
| WP-01 | One VAT-regime resolver; stop netting non-payer reverse charge | explicit (bug) | H | S | — | todo | — |
| WP-02 | Every journal entry needs a date | explicit (bug) | H | S | — | todo | — |
| WP-03 | Honest bank reconciliation for non-RON accounts | explicit (bug) | H | S | — | todo | — |
| WP-04 | Stop booking foreign-currency bank movements as RON | explicit (bug) | H | S | WP-03 | todo | — |
| WP-05 | Client identity at every intake (archive, e-Factura, tax ID, IBAN) | explicit (bug) | H | S | — | todo | — |
| WP-06 | Law pack: legal reference data as sourced, dated, pinned data | explicit | H | M (slices) | — | todo | — |
| WP-07 | Typed tenant profile with provenance and `tenant_check` | implicit | H | M | WP-01 | todo | — |
| WP-08 | Controls registry: one gate for close, export, template and firm row | explicit | H | M | — | todo | — |
| WP-09 | Filings register, calendar and receipts | explicit | H | L (slices) | WP-06, WP-07 | todo | — |
| WP-10 | Filed-aware, non-destructive corrections | explicit | H | M | WP-09 | todo | **D1** |
| WP-11 | Sealed closes and superseded reports | explicit | M | S | WP-10 | todo | — |
| WP-12 | Partner-aware openings and sub-ledger ties | explicit | H | M | WP-08 | todo | **D2** |
| WP-13 | Take-on shapes: off-balance memo, year-end vs mid-year cut, go-live | explicit | H | M | WP-12 | todo | **D7** |
| WP-14 | Completeness checks and must-be-zero anomalies | implicit | H | M | WP-07, WP-08 | todo | — |
| WP-15 | Declared close steps and chronological close | explicit | H | M | WP-08 | todo | — |
| WP-16 | Non-payer reverse-charge posting and D301 figures | explicit | H | M | WP-01, WP-06 | todo | **D3** |
| WP-17 | Partner identity: tax-ID normalization, check digits, blank-ID partners | explicit | M | S | WP-05 | todo | — |
| WP-18 | Recoverable VAT (4424) by origin year, with prescription alert | explicit | M | S | WP-06 | todo | — |
| WP-19 | Income tax: quarterly profit-tax step, micro rate by quarter of crossing | explicit | M | M | WP-06, WP-15 | todo | — |
| WP-20 | VAT-return ties: filed D300 chain and the 4428 tie | explicit | M | S | WP-08, WP-09 | todo | — |
| WP-21 | Book of record: shadow mode alongside SAGA | implicit | H | M | WP-07, WP-08 | todo | **D5** |
| WP-22 | General journal note (correction) tool | implicit | H | M | WP-02, WP-08, WP-10 | todo | — |
| WP-23 | SAGA note-contabilă DBF exporter | explicit | H | M | WP-08, WP-21, WP-22 | todo | — |
| WP-24 | SAGA take-on grid and open-items export | explicit | M | S | WP-12, WP-13 | todo | **D4** |
| WP-25 | FX: currency on lines, BNR rates, month-end revaluation | explicit | H | L (slices) | WP-03, WP-04, WP-06, WP-15 | todo | **D6** |
| WP-26 | D406 / SAF-T: read, readiness, validate, then emit | explicit | H | L (slices) | WP-09, WP-12; WP-25 for the emitter | todo | — |
| WP-27 | Bank statement parser registry: PDF text and payment-platform exports | explicit | H | M | WP-04 | todo | — |
| WP-28 | Outbound PII guard for shared projections | explicit | M | S | — | todo | — |
| WP-29 | Engagement backlog, request lists by party, digest | implicit | H | L (slices) | WP-09, WP-28 | todo | — |
| WP-30 | Onboarding importers registry and external trial-balance compare | explicit | H | L (slices) | WP-13, WP-21 | todo | — |
| WP-31 | Marketplace clearing-account settlement | implicit | M | M | WP-16, WP-25, WP-27 | todo | — |
| WP-32 | Review options, approver role and decision records | explicit | H | M | — | todo | — |
| WP-33 | Workflow templates pack | explicit | H | M (slices) | per slice | todo | — |
| WP-34 | Rules and workflow version stamped on runs, entries and reports | implicit | M | S | WP-06 | todo | — |
| WP-35 | Cross-border: OSS switch, VIES classification, D390 rows | trigger | M | L | WP-06, WP-09, WP-17 | todo | — |

---

## 3. Decisions the user must make first

Ask the user before starting a blocked WP. Record the answer here and in the WP.

### D1 · Correcting a closed month: storno-only, or reopen allowed? (blocks WP-10, WP-11)

The practice's method never rewrites a closed period. An error found after the close is fixed by a
reversal **dated today**. langclaw's `accounting_period_reopen` (`langclaw/accounting/journal.py:397-437`)
deletes the month's `close/<period>/%` entries and unlocks the month. `Journal.reverse`
(`journal.py:145-185`) renames the original entry's key even when that entry's month is closed. The
practice's rule for routing a correction is: filed → storno today; not filed → reopen allowed; filing
status unknown → storno today. The legal basis for storno-only as an obligation is not recorded
anywhere `[de confirmat]`.

- **A. Policy per tenant (recommended):** `profile.period_policy = "storno_only" | "reopen_unless_filed"`,
  with `storno_only` as the default. In both modes a reopen voids entries instead of deleting them, and
  a reversal never touches a row dated in a closed month.
- **B. Storno-only everywhere:** remove reopen as a tool, and keep it only as an admin repair.
- **C. Keep reopen as it is:** only add the "refuse when filed" guard.

### D2 · Do 461/462 belong in the sub-ledger ties? (blocks WP-12)

The migration toolkit's code excludes 461/462 from the partner ties. The reason recorded there is that
they are "wash" accounts: they net to about zero at the synthetic level but not per partner, so they
fail spuriously. The same client's written control list still includes them. The marketplace pattern
(WP-31) also parks payouts on 462 per payer.

- **A.** Exclude 461/462 from the blocking tie and report them as INFO (the toolkit's choice).
- **B.** Include them as blocking.
- **C.** Make it a per-tenant list, `profile.subledger_accounts`, defaulting to A.

### D3 · Which account holds a non-payer's reverse-charge VAT liability: 4423 or 446x? (blocks WP-16)

For a non-VAT-payer buying EU services, the practice records this template: 628x/401 base; 4426/4427
self-assessment; 635x/4426 VAT to expense; 4427/**liability**. The workspaces disagree on the liability
account. One year-end verification recommends a 446x analytic ("TVA de plată", as used for D301), while
the same company's catch-up work books 635 against 4423.

- **A.** 446x analytic (e.g. `4463`-style "other taxes"), configurable.
- **B.** 4423.
- **C.** A profile key, `vat_liability_account`, with no default: the tool refuses until it is set.

### D4 · SAGA take-on grid, column "precedent": with or without the opening? (blocks WP-24)

SAGA's *Preluare date contabile* grid has `Debit/Credit inițial an` and `Debit/Credit precedent`. One
runbook, recorded as "verified against the SAGA manual", sets `precedent` = **total sums** to the end of
the previous month (opening + turnover). A script written for another migration sets it to turnover
**without** the opening. The two differ on every account that has an opening balance. Neither was
confirmed on a live SAGA install.

- **A.** Opening + turnover (the runbook).
- **B.** Turnover only (the script).
- **C.** Emit both variants, marked `unverified`, until one is rehearsed on a scratch SAGA company.

### D5 · Is langclaw the book of record, or a shadow alongside SAGA? (blocks WP-21, WP-23, WP-30)

Every live client in the portfolio is posted in SAGA C by a person. The practice's doctrine is that the
agent prepares, checks and reports, and never posts in the books or files a return. langclaw treats
`Journal` as the only posting path, and `journal_post` accepts an empty `approved_by`
(`langclaw/accounting/tools.py:222-224`).

- **A. Per-tenant `book_of_record: langclaw | saga | …` (recommended):** in the non-langclaw modes, the
  journal mirrors imported exports, proposals are exported as SAGA note batches, close verifies but does
  not lock, and every post needs a named approver.
- **B. langclaw is always the book:** SAGA exports are only a convenience.
- **C. Shadow only:** langclaw never posts, and it keeps proposals plus reconciliations.

### D6 · FX data model (blocks WP-25)

- **A. Line columns (recommended):** `journal_lines` gains `currency`, `amount_currency` and
  `fx_rate`; RON stays the booked amount.
- **B.** A separate `fx_positions` table per account analytic, with lines staying RON-only.
- Also decide: whether FX goes into the D406 emitter (WP-26) from the start, and which BNR rate source
  to use (a feed vs a rate entered by the accountant).

### D7 · Take-on contra and off-balance handling (blocks WP-13)

- **Contra account for a take-on journal:** one runbook uses 891-style suspense, another script offers
  891 or 899x. **A.** No contra: post a single balanced entry. **B.** Suspense that must net to 0.00.
- **Class 8/9 memo balances:** **A.** Keep them out of `journal_lines`, in a memo list on the opening.
  **B.** Post them against an off-balance contra from `profile.off_balance_contra`.

---

## 4. Triggers worth exploring

1. **The practice's accounting-note and deadline library.** The practice keeps a separate library of
   numbered accounting notes (`NC-###`, each with a `verified_at` date) and a filings/deadline register
   (recorded as "verified 2026-07-15"). The fleet orchestrator cites it by id; it was not in this
   workspace. Attach it read-only. It is the richest source for law-pack rows (WP-06), rule ids on
   checks (WP-34) and the close order (WP-15). Import only the rules and generic data, never a client
   row.
2. **A legal corpus repository.** The method pins law through a `legal.lock` that is still unpinned
   (NEPINUIT), so every citation stays provisional. A pinned corpus would let law-pack rows move from
   `[de confirmat]` to `verified`, with a reproducible release id.
3. **Scratch SAGA company rehearsal.** One import round-trip each for the NC DBF (WP-23), the partner
   DBF, the take-on grid (D4) and the invoice XML the export already produces. Until then, every SAGA
   format stays `unverified`.
4. **Banks' structured exports.** No MT940 or CAMT file exists anywhere in the portfolio, only PDFs.
   Before building PDF parsing past one profile (WP-27), ask whether each client's bank offers
   MT940/CAMT or CSV downloads.
5. **SAGA "first-read" XLS pack.** Three takeovers received the same SAGA report pack (trial balance,
   journal register, ledger, purchase/sales/bank/cash journals, fixed-asset register). Profile its
   headers once, synthetically, for WP-30.
6. **The D406 validator (DUKIntegrator) rules and XSD.** Neither was in the workspaces. They are
   needed before the WP-26 emitter slice can claim validity.
7. **A per-tenant brief injected into each chat turn.** A `TenantBriefMiddleware` after the channel
   context would prepend the digest (WP-29), the legal status (WP-06) and the pins (WP-34). It must go
   through the PII guard (WP-28) when the chat is shared.
8. **Erasure and retention.** List every place a tenant's data lives (schema, bucket prefix, run index,
   archives, logs) and add an erasure hook. The method's frozen archives also needed erasure.
9. **Template metadata and fixture runs.** Give every shipped `.graph.json` a required `meta` block
   (purpose, inputs, pre/post, status, test), and run each template against fixtures in CI.
10. **Third-party SAF-T template tools.** Some clients file D406 through a third-party generator fed by
    14 xlsx templates (CATALOGUES §C-F7). Emitting that template set may be cheaper than a full XML
    emitter.

---

## 5. Rejected or already present

These lens items were checked against the code (HEAD `9441429`) and dropped, merged or corrected.

| Item (lens) | Verdict | Evidence |
|---|---|---|
| VAT rate valid on the invoice date (G-23 rate change, credit note and reissue) | already present | `langclaw/accounting/checks.py:140-153`; credit notes count negative in `period.py:208-211` |
| e-Factura vs books when langclaw is the book (G-25) | already present | `blockers` stop the close (`period.py:280-286`, `tools.py:576-580`); only the external-books case remains, in WP-21/WP-30 |
| Reverse-order reopen | already present | `journal.py:417-420` |
| Reversal date's month must be open | already present | `reverse` posts through `post` (`journal.py:179-181`), which checks the date's month |
| Profit distribution 121 → 1061/457/117 "left to the accountant" | already present; the guide is stale | `accounting_result_carry` (`tools.py:1746`); fix the sentence in `docs/guides/accounting.md:752-753` in WP-01's docs step |
| Cash register, Z reports, reminders, payment batches | present, and rare in the portfolio | no WP (lens J: done-enough) |
| Micro tax 1% default for 2026 | matches the recorded 2026 rate | `results.py:20`; only the 2025 quarter switch is missing (WP-19) |
| Lens F: "close refuses only on blockers and missing documents (tools.py:560-579)" | **premise corrected** | the close checks seven gates (`tools.py:574-601`): already closed, blockers, missing documents, trial balance, bank chain, bank agreement, negative cash |
| Lens A: `cron` default timezone at `schema.py:515` | citation corrected | `langclaw/config/schema.py:516` (`timezone: str = "UTC"`); folded into WP-33 docs |
| Lens G: "the currency path of `_book_payment` is unverified" | **verified as a bug** | `tools.py:856-872` books the allocation amount (statement currency) on `bank_account(...)` (`bank/booking.py:29-34`), so payments share the fee/cash bug (WP-04) |
| Separate "centralize the close gates" WP (lens A) | merged into WP-08 | `tools.py:574-601`, `ui/templates/accounting_month.graph.json` `can_close` branch, `overview.py:79` |
| Separate "silent skips" WP (I-09) | merged into WP-14 | `bank/reconcile.py:33`, `journal.py:324` |
| Separate FX-revaluation WPs (D-05, D-06, G-17, G-18, I-06) | merged into WP-03, WP-04 and WP-25 | — |
| Separate "4428 tie" and "D300 chain" WPs | merged into WP-20 | — |
| `wait_for` workflow node (H-04) as its own WP | folded into WP-29 as an option; the design call is left to the implementer | `langclaw/workflows/graph/spec.py:177-193` (only `human_review` pauses) |
| Porting the tracker's Markdown parser (F-13) | rejected | langclaw is database-backed; only the digest algorithm is ported (WP-29) |
| Porting the OSS scripts and the SAP closing-journal scripts (F-16, F-12 "post" part) | rejected | hard-coded paths, float money and embedded client data; only their rules are kept (CATALOGUES §D, §F) |
| Porting the SAF-T "deterministic save" as-is (F-06) | rejected as-is | it leaks a timestamp; the pattern goes into WP-23 (deterministic zip) with the fix |
| NextUp exporter | out of scope | placeholder by design (`export/nextup.py`) |
| Micro ceiling of 250k EUR for 2025 in `LIMITS` | not rejected, but unsupported | no workspace supports the value (`outlook.py:42-43`); it becomes a `[de confirmat]` law-pack row in WP-06 |
| Cross-client misfiling seen in one year-end workspace | practice-side issue | langclaw's structural isolation holds; its content-level guard is WP-05 |

---

## 6. Provenance

**Harvest date:** 2026-09-29 → 2026-09-30. Langclaw was verified at branch `claude/fervent-gates-9q1f3l`,
HEAD `9441429`.

**Lens roster:**

| Lens | Extracted | Main outputs used here |
|---|---|---|
| A | langclaw baseline: data model, tools, profile keys, reference data, seams, test harness, hazards | "Langclaw today" lines, seams, test harness in §0 |
| B | method and governance: backlog, digest, projections, storno-only, sealing, pinning, provenance, decisions | WP-10/11/28/29/32/34; CATALOGUES §B |
| C | format contracts: SAGA NC/partner/take-on/journal, D406, receipts, bank PDF, payment platform, Expert, SAP | WP-23/24/26/27/30; CATALOGUES §C |
| D | rules and controls: registry, sub-ledger ties, continuity, FX, reverse charge, 4424 layers, close order | WP-08/12–20/25/31; CATALOGUES §D |
| E | law and filings: declaration catalogue, thresholds with certainty, obligation-state model, law pack | WP-06/09/18/19/35; CATALOGUES §E |
| F | code assets: control engine, DBF I/O, round-trip reconciler, SAP adapters, validators, tracker | WP-08/23/30; CATALOGUES §F |
| G | 28 real cases as synthetic scenarios, with langclaw verdicts | tests in every WP; CATALOGUES §G |
| H | human workflows and client messages | WP-29/32/33; CATALOGUES §H |
| I | failure modes and review methods | the WP-01–05 bugs; WP-14; CATALOGUES §I |
| J | portfolio patterns and priority weights | §1, ordering of §2 |

**Workspace aliases** (archetypes only; the workspaces are private):

| Alias | Archetype |
|---|---|
| L0 | this framework (public) |
| M0 | the practice's method core: engagement backlog schema, digest and projection tooling, close checklist, lock files; pinned by the method clients |
| OR | the practice's PII-free fleet orchestrator: deadline calendar, fleet register, cross-client rules |
| NCLIB | the practice's accounting-note and deadline library (cited through OR only; not available to this pass) |
| T1 | migration from Expert (DBF) to SAGA, with a reusable, tested toolkit; SPV invoice register and note builder |
| T2 | SAP → SAF-T (D406) mapping pipeline for two companies, with validators and a test suite |
| T3 | SAP → SAGA opening-balance migration: year-end trial plus a mid-year take-on runbook and script |
| T4 | SAP trial balance → SAGA closing journals (scripts plus workbooks) |
| C1 | pinned-method client: marketplace seller, non-VAT payer with reverse charge on EU services, FX |
| C2 | pinned-method client: audit and catch-up plan, reconciliation of SPV invoices and filings |
| C3 | pinned-method client (joint-stock company): catch-up, review campaign, issues procedure |
| C4 | pre-pin method client: profit tax, FX accounts in two currencies, month-close checklist, filing-receipts register |
| C5 | takeover without a prior system: micro, non-payer, cross-border B2C services, OSS/VIES; module library; integrity reviews |
| C6 | new-client onboarding: received documents plus an onboarding checklist |
| Y1 | year-end workspace for C1's company: Expert data plus a VAT re-verification |
| Y2 | year-end workspace for C4's company: Expert data, financial statements and annual profit-tax preparation |
| Y3 | year-end workspace for T1's company: Expert/SAGA data dumps |

Paths in the WPs are client-neutral: any client or company name is written as `<client>`.

**Certainty of legal values.** Values recorded "as confirmed" in a workspace are marked that way in
CATALOGUES §E, with the source that workspace cites. Everything else is `[de confirmat]`. The method's
own `legal.lock` is unpinned, so even "confirmed" rows stay practitioner-level until an accountant
signs off a law-pack row.

**Limits of this pass:**

- **Denied or absent sources:**
  - C5's review and canonical-state folders were denied to lens B (sensitive), so B's review taxonomy
    comes from C5's README; lens I read them.
  - Several C5 instance files were denied to lens E, so the 2025 micro ceiling stays unverified.
  - T2's decision log was denied to lens G (lens B and lens I read it).
  - The per-repo manifests were denied to lens B.
  - One raw D406 read was denied to lens C; structure came from the other files.
  - A repo-wide grep for a tax-ID checksum implementation was denied to lens D.
  - NCLIB, the legal corpus, the D406 XSD and the validator rules were not in the workspace.
  - Bank statements that sit in client mailboxes were not visible.
- **Unverified in langclaw:**
  - what review notices send to a review chat shared across tenants (WP-28 starts by checking it);
  - whether `check_proposal` accepts 4111/418 on a sales invoice (scenario G-21).
- **Counts:** portfolio counts are per case (n = 8) and not weighted by fee or volume.
- **Unrehearsed formats:** every SAGA format is from specs and files, never from a live import.

---

## 7. Execution log

| Date | WP | PR | What changed | Surprises |
|---|---|---|---|---|
| | | | | |
