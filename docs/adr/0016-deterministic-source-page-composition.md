# Deterministic Source-page composition: extractive summary stub, full-template placeholders, relative paths

A Phase 3 Source page keeps the full structure of `templates/source.md` so the page
shape is stable across phases, and fills it deterministically:

- **Summary callout (required by CLAUDE.md rule 5).** With no LLM available, the
  `> [!summary]` callout is filled extractively — the first meaningful paragraph (≈ the
  first one or two sentences) of the source's normalized Markdown. Frontmatter carries
  `summary_status: stub`. The later semantic phase replaces the text with an LLM summary
  and flips the flag to `enriched`; the maintenance linter treats `stub` as expected
  ("pending enrichment"), not as summary rot. A `partial`/`needs_ocr` source with too
  little extractable text falls back to a structural summary line (title, page/chunk
  counts, section list). Extracted source text appears in the callout strictly as
  displayed data, never as instructions (untrusted-input contract). Because the text is
  unverified untrusted source content, the callout is **explicitly labelled** as an
  extractive excerpt rather than presented as a vetted summary — the rendered form is
  `> [!summary] Extractive excerpt (auto-generated, unverified)` above the quoted text —
  so neither a human nor a later agent mistakes raw source prose for a semantic summary.

- **LLM-only fields and sections.** Tags, concepts, entities, people, organizations,
  projects, key points, and claims cannot be produced deterministically. Their
  frontmatter lists are emitted empty (`tags: []`, `concepts: []`, …) and their body
  sections are rendered with an explicit `_Pending semantic enrichment_` placeholder.
  Critically, placeholders contain **no wikilinks** — the template's example links such
  as `[[Claims/{{claim_id}}]]` are omitted in backbone mode so `validate_wikilinks`
  never sees a dangling link. The linter validates this backbone contract, and the
  semantic phase later replaces placeholders with real, cited content and backlinks.

- **Repository-relative paths only (ADR-0009).** A wiki page is a portable artifact, so
  its frontmatter uses `relative_raw_path`, not an absolute `raw_path`. `templates/source.md`
  is corrected to use the repository-relative path (the absolute `raw_path` it previously
  carried in both frontmatter and body was a portability/citation contradiction).
  Deterministic frontmatter fields are: `source_id`, `title`, `aliases`,
  `relative_raw_path`, `normalized_path`, `sha256`, `file_type`, `language`,
  `page_count`, `chunk_count`, `summary_status`, an `input_fingerprint` for
  deterministic freshness (ADR-0023), plus the shared lifecycle fields of ADR-0022
  (`status`, `ingestion_status` mirrored read-only from the manifest,
  `generation_status`, `created`, `ingested`). Deterministic Source pages carry no
  wall-clock timestamp (ADR-0023).

- **Backbone link invariant.** The Phase 3 backbone records **no semantic backlinks**.
  Bidirectional linking between sources and concepts/entities/claims (Build Spec §3.5)
  begins in Phase 3.5 with the semantic layer. The only relationships the backbone
  represents are deterministic and already known: a source's provenance to its
  normalized artifacts, and exact-duplicate occurrences recorded on the manifest
  (ADR-0007). This is a stated, validated invariant — "no semantic backlinks yet" — not
  an accidental omission, so the deferred bidirectional-link dependency is explicitly
  modelled rather than silently missing.

Consequences: a Source page never asserts anything it cannot mechanically verify, yet
its structure is identical before and after enrichment, so regeneration is a stable,
idempotent overwrite and diffs stay clean. The linter can enforce one Source-page
contract across phases. The trade is that backbone pages visibly carry several
"pending enrichment" sections until Phase 3.5 runs, which is honest about what has and
has not yet been derived.
