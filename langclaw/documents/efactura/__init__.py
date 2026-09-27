"""
e-Factura (Romania) — invoices from ANAF's SPV, parsed without a model.

Since 2024 Romanian B2B invoices travel through RO e-Factura as UBL 2.1 XML
(CIUS-RO). That XML is structured and legally authoritative, so importing it
needs no OCR and no LLM: :mod:`.ubl` parses it, :mod:`.spv` fetches it (from ANAF
or a demo source), and :mod:`.sync` files it per client.
"""
