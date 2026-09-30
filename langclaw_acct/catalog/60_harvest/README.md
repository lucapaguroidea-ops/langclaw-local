# 60_harvest — additive Catalog Cale

These files **add** articole. They do not replace `10_`–`50_`.

Loader rule: merge by `catalog:` name. Additive `rows` / `kinds` / `controls` / `filings` append. Duplicate `id` is an error.

| File | Adds |
|---|---|
| ARTICOLE_CONTROLS_v1.yaml | Layer 1 / prefile poartă ids (C0, C1, C2, M1.1, M1.2, M1.8, T_regime, P_*) |
| ARTICOLE_FILING_v1.yaml | D300/D301/D390/D394/D100/D112/D406 as gates + receipts, not producers |
| ARTICOLE_CODIT_AXES_v1.yaml | book_of_record, fx, oss, engagement, marketplace, certainty |
| ARTICOLE_SOURCE_DOC_ADD_v1.yaml | extras_statement_pdf, spv_register |
| ARTICOLE_HITL_ADD_v1.yaml | decision_menu (server stamps), codit_premise, filing_receipt, control_disposition |
| ARTICOLE_EXTRAS_GRAIN_v1.yaml | WP-13: line Jobs, not statement-total match |

Parked on purpose: ArticolBon stays in `30_cale/` and is not referenced by BUILD v1.
