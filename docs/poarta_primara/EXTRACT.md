# Extract adaptor

WP-02 / WP-13. One contract for every backend (`ubl`, `mt940`, `document_ai`, `docling`).

Under the source object's bucket prefix:

```
normalized/markdown.md      text the graph may read
normalized/tables.json      list of {headers, rows} as strings
normalized/extract_meta.json
```

`extract_meta.json`:

```
{
  "backend": "document_ai|ubl|mt940|docling|none",
  "source_hash": "...",
  "model_or_version": "...",
  "needs_ocr": false,
  "identity_ok": true
}
```

Rules:

- UBL CIUS-RO does not go through OCR. Parse XML → CanonicalDocument fields.
- `skip_if: [has_text_layer, is_ubl]` is on the SourceDoc row.
- Graph state stores field strings, not raw model JSON.
- Swap Docling later without changing node names. Same three files.
- extras PDF uses `document_ai` until WP-13. It does not emit a Job per statement total. See `catalog/60_harvest/ARTICOLE_EXTRAS_GRAIN_v1.yaml`.
