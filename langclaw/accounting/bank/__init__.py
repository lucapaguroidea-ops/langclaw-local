"""
Bank statements — deterministic import and payment matching.

- :mod:`.parse`: MT940 and CAMT.053 (ISO 20022) statements → :class:`.parse.Statement`,
  checked (opening + movements = closing).
- :mod:`.match`: a movement pays an invoice when the amount matches exactly and
  a second signal agrees (invoice number in the description, the supplier's
  IBAN, the partner's name) → ``certain``; the amount alone, with a single
  candidate → ``probable`` (for a person to confirm); several candidates → none.
- :mod:`.store`: per-client ``bank_transactions`` (idempotent by a transaction key).
"""
