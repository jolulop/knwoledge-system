# Citations are structured objects, not free-text location strings

Every citation on a semantic page (claim, synthesis, query answer) is a **structured
object**, not a free-text "location" string. The current templates use placeholders like
`{{page_section_line_or_timestamp}}`, which cannot be machine-validated and cannot be
mechanically resolved back to evidence. This ADR replaces them with a fixed citation
schema built from the anchor vocabulary already fixed in ADR-0012 and
`policies/citation.yaml`, operationalizing the "Source + stable anchor" rule of ADR-0019.

A citation object has:

```yaml
- source_id: src_xxxxxxxxxxxxxxxx   # required — which source
  char_start: 0                     # required — anchor into normalized Markdown (authoritative)
  char_end: 0                       # required
  page: null                        # optional — 1-based source page (paginated formats)
  page_end: null                    # optional
  section: null                     # optional — heading/section path
  table_reference: null             # optional — repo-relative CSV path (table evidence)
  sheet_reference: null             # optional — sheet name (XLSX)
  chunk_id: null                    # optional, ADVISORY ONLY — convenience pointer, recomputed, never trusted as identity
  quote: null                       # optional — short evidence excerpt for display
```

The authoritative locator is `(source_id, char_start, char_end)` plus the optional
page/section/table refs — all stable, mechanically-derived coordinates that survive
re-chunking. `chunk_id` is carried only as a non-authoritative pointer (ADR-0019). Pages
store these objects in a frontmatter `citations:` list (the machine-readable record);
the body evidence table is a rendered *view* of that list (Source · page/section · char
range · quote), not a second source of truth.

This makes citations validatable by deterministic scripts: a citation verifier can
confirm the `source_id` resolves to a manifest, the `(char_start, char_end)` range is in
bounds for that source's normalized Markdown, the `page` is within `page_count`, and the
quoted text actually occurs at the anchor — closing the "hallucinated citation" risk
without trusting free text. It also gives Phase 5 cited answering a uniform evidence
shape to assemble and render.

Consequences: claim, synthesis, and query templates carry a typed `citations:` list and
a rendered evidence table derived from it; `validate_citations.py` is extended in Phase
3.5/Phase 5 to check the structured fields against the normalized layer. The cost is
that producing a citation now requires real anchor data (the extractor already provides
it via chunks), and free-text "see page 12-ish" citations are no longer expressible —
which is the point.
