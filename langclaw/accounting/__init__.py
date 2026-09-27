"""
Accounting (Romania) — proposals a model makes, checks code enforces.

The loop for one invoice: gather context deterministically (the invoice, how this
supplier was booked before, the client's VAT regime, the VAT rates valid on the
invoice date) → a model proposes a journal entry → :func:`.checks.check_proposal`
verifies it → a person reviews it when in doubt → :class:`.journal.Journal`
posts it, refusing anything unbalanced or unchecked. The model never posts.
"""
