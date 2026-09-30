"""
Poarta Primară (working package ``langclaw_acct``) — documente primare walked
through a versioned *Catalog Cale* by four LangGraph graphs, gated before any
statutory sink.

Source of truth for the design: ``docs/poarta_primara/`` (00_LAW.md first).
The catalogs this package executes live in ``langclaw_acct/catalog/``.

Built so far (see docs/poarta_primara/BUILD.md):

- :mod:`langclaw_acct.types` — the WP-00 types (strict: extra keys refused,
  money and fiscal dates as strings).
- :mod:`langclaw_acct.catalog` — the WP-01 loader: every YAML, additive files
  merged, cross-references checked, unknown ids refused.
- :mod:`langclaw_acct.sinks` — the SagaEye read protocol and its pre-Mouth-0
  witness: a Registru Jurnal exported from the client's accounting system.
"""
