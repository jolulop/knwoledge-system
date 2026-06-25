# Wiki page frontmatter: a consistent lifecycle contract, with page status separate from extraction state

All generated wiki pages share a consistent set of lifecycle frontmatter fields, and the
**wiki page status** is kept strictly separate from a source's **extraction state**.
Today `templates/source.md` overloads a single `status: active` field, which is
ambiguous against the manifest's `ingestion_status` (`extracted | partial | error`) —
two different concepts (is this page live in the wiki? vs. did extraction succeed?)
sharing one word.

The shared lifecycle fields (present where applicable to the node type) are:

```yaml
status:            active        # wiki lifecycle: active | candidate | deprecated_candidate | archive_candidate | archived (policies/retention.yaml; candidate per ADR-0018)
review_status:     none          # PAGE review state: none | pending | approved | rejected (app/workers/wiki_render.py::REVIEW_STATUSES)
generation_status: deterministic # how the content was produced: deterministic | enriched | human_edited
confidence:        low           # low | medium | high  (semantic nodes only)
source_count:      0             # count of independent sources (concepts/entities/synthesis; see ADR-0018)
derived_from:      []            # provenance: list of source_ids this page is derived from
```

**Page `review_status` vs. the review ledger.** The page frontmatter `review_status` is the *derived
page view* of review state and takes one of `none | pending | approved | rejected`. It is **distinct from
the review ledger's item `status`** (`policies/review.yaml`, ADR-0035), which adds a non-terminal
`deferred` ("a human has seen this item but left it unresolved in the queue"). `deferred` is a
review-ledger-only workflow state and **does not produce a distinct page frontmatter value**: the ledger
is the system of record for review workflow, pages are regenerated artifacts, and a page may be the target
of several review items at once, so "the page is deferred" is not well-defined. A page reflecting an
unresolved review item (including a deferred one) stays/derives as `pending`, never `deferred`. The two
sets overlap on `pending|approved|rejected`; the *page* set additionally has `none` (no review item), the
*ledger* set additionally has `deferred` (non-terminal). The contract's invariant — *any emitted
`review_status` is in the page set* — is **machine-gated by `scripts/validate_frontmatter.py`**: it is
**required** on every page type that renders it (claim, concept, entity, person, organization, project,
synthesis, query); if present it **must be in-set**; **Source pages must not carry it at all** (review
state lives in the ledger, owned by no renderer); and **Tag pages are out of scope** until a tag
renderer/lifecycle exists. Producers uphold the invariant two ways: *dynamic* emitters (claim, concept,
entity, person, organization, project, synthesis) route the value through
`wiki_render._resolve_review_status`, which rejects out-of-set values; the *constant* emitter (Query)
writes a fixed in-set literal (`none`). The validator is the backstop for both.

Deterministic pages (Source pages, ADR-0016) do **not** carry a wall-clock
`last_compiled_at`; it is superseded by an `input_fingerprint` so the page stays
byte-stable and freshness is content-keyed (ADR-0023). Non-deterministic semantic pages
introduced later may reintroduce a compile timestamp if useful.

Source pages additionally carry `ingestion_status` **mirrored read-only from the
manifest** (the authoritative copy stays on the manifest, ADR-0011) so the two axes are
explicit and never conflated: `status` answers "what is this page's lifecycle state in
the wiki," `ingestion_status` answers "what is the extraction state of the underlying
source." A page can be `status: active` while its source is `ingestion_status: partial`
(needs_ocr), and those facts must not collide.

`generation_status` records provenance of the content itself — `deterministic` for the
Phase 3 backbone, `enriched` once an LLM has filled the semantic sections, `human_edited`
if a reviewer has hand-curated it — which lets the linter and later regeneration know
whether overwriting a page would destroy human work. It composes with the
`summary_status` (`stub | enriched`) field of ADR-0016, which tracks the summary callout
specifically.

Consequences: every page type exposes the same auditable lifecycle vocabulary, so
validators, retention, and review logic read one consistent contract instead of
per-template ad hoc fields. Separating `status` from `ingestion_status` removes a real
ambiguity before any pages exist. The cost is more frontmatter per page and a rule that
`ingestion_status` on a Source page is a read-only mirror — the manifest remains its
system of record.
