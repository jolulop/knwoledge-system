# ADR-0037 — Lint quality heuristics: summary-rot & stale-claim

**Status:** Accepted. Decisions 1–5 (`summary_rot` + `stale_claim_citation`) **implemented** (commit
`8958fe3`). **Decision 6** (`synthesis_rot`; concept/entity rot dropped) design-locked 2026-06-24 via a
follow-up grill gate — design only, not yet implemented.
**Extends:** ADR-0036 (Phase 7 autonomous maintenance; this is the deferred "lint heuristics" follow-up
to slice 7-1), ADR-0016 (deterministic stub vs LLM-enriched summary), ADR-0025/0027 (enrichment artifacts
+ input fingerprints), ADR-0019/0020/0026 (structured citations + grounding), ADR-0029/0030 (graph is the
source of truth for edges). Read `app/workers/lint.py`, `app/workers/enrich.py`, `app/workers/claims.py`,
`app/workers/enrichment_artifact.py`, `app/workers/citations.py`.

## Context

Phase 7 lint (`/jobs/lint`) detects structural/governance defects (missing raw, under-supported concept,
uncited claim) but not two *quality-drift* defects the Build Spec/CLAUDE.md name: **summary rot** (an
LLM summary that no longer reflects its source) and **stale claims** (a citation whose evidence has
drifted). ADR-0036 deferred these to a later "lint heuristics" slice. This ADR design-locks them as two
new **deterministic, key-free, report-only** checks inside `/jobs/lint`. No graph-identity rewrites, no
new review vocabulary, no executors.

## Decisions

**1. Two new checks, both deterministic + key-free, reusing existing durable machinery.**
- **`summary_rot`** — a Source's enriched summary is stale when the enrichment artifact's stored
  fingerprint no longer matches the inputs the *current* enrich pass would use:
  `normalized/enrichment/<sid>.json.input_fingerprint != artifact_fingerprint(current normalized markdown,
  current configured summary model_ref)`. The signal *is* enrich's own freshness check (it already skips
  re-summarizing on a fingerprint match), so rot = "the current enrich pass would regenerate this." The
  fingerprint bundles content + model + schema + prompt versions, so a model/prompt/schema bump correctly
  counts as rot (high-volume but accurate — see decision 3). **Source pages only.** A missing artifact or
  `summary_status: stub` is **not** rot (ADR-0016). Uses the **current configured** `model_ref` (settings,
  key-free) — never the artifact's stored `model_ref`.
- **`stale_claim_citation`** — a claim's cited anchor no longer supports the stored claim. Enumerate the
  durable `normalized/enrichment/<sid>.claims.json` citations (each `{source_id, char_start, char_end,
  quote}`, the `quote` frozen at extraction time) and re-ground the **stored** quote against the current
  `normalized/markdown/<sid>.md` via `citations.ground_citation(..., require_quote=True)`. The stored
  artifact quote is essential: claim *pages* reconstruct the quote from the current span
  (`recompose_claim`), so graph/page-only re-grounding is **circular** and would falsely pass after drift.
  The finding means "this citation anchor is stale," **not** "the claim is false."

**2. Active-edge exact match for stale claims (not active-node).** Only reground an artifact citation that
exactly matches an **active graph `derived_from` edge** on `(claim_id, source_id, char_start, char_end)`.
Filtering by active claim *node* is too loose — a claim can remain active via another source while this
source's edge is superseded. This check is therefore **graph-gated**.

**3. Report-only — no review ledger entry (governance vs maintenance boundary).** Neither finding asks a
human to decide semantic truth or approve a destructive action; both have **mechanical remedies** (re-run
a producer). Routing them through `reviews/` would blur a *human governance decision* with an *operator
maintenance task* — architectural drift for Phase 6/7. So v1 emits **structured findings + counts only**:
no new review types, projectors, or executors; `wiki/log.md` records aggregate counts. The remedy is the
operator re-running `enrich` (rot) or `extract`+`claims` (stale; the claims worker auto-retracts/regrounds
stale evidence). Remediation is **metadata, never an apply action.**

**4. Severity + health: low/medium, never `failing`; `degraded` only on expectation-mismatch coverage.**
- `summary_rot` = **low**; `stale_claim_citation` = **medium**. Neither is `high`, so neither flips lint to
  `failing` — that stays reserved for a `validate_*` hard-fail or a high-severity structural/governance
  defect. A model/prompt/schema bump creates visible **maintenance debt** (via `by_check` counts), not a
  red board. No thresholds, no fourth state in v1.
- `summary_rot` is **graph-independent** (runs even when the graph is absent). `stale_claim_citation` is
  graph-gated → when the graph is absent/schema-mismatched the existing `graph_unavailable` finding +
  `degraded` path covers it (do **not** also emit `claim_evidence_unverifiable` for graph absence).
- **Conditional `degraded`** — only when coverage was *expected* but unavailable, via two new low-severity
  coverage findings (never `failing`):
  - `summary_unverifiable` — a Source page marked `summary_status: enriched` whose artifact is
    missing/unreadable, or whose current normalized markdown is missing (can't recompute the fingerprint).
  - `claim_evidence_unverifiable` — the graph (readable) shows active claim evidence from a source whose
    `.claims.json` artifact or normalized markdown can't be checked.
  A fresh deterministic-only vault (stub summaries, no enrichment/claims artifacts, no active claims) stays
  **`healthy`** — nothing is expected, nothing is missing.
- **Page reads are coverage-only.** Findings (`summary_rot`/`stale_claim_citation`) come from the durable
  artifacts; the narrow Source-page probe exists *solely* to detect unverifiable coverage, never to emit a
  rot finding.

**5. Finding payload: optional structured `data` + stable remediation codes.** Add an optional
`data: dict` (default `{}`) to `LintFinding` (backward-compatible). The flat `subject`/`detail` stay; `data`
carries machine-actionable fields:
- `summary_rot` → `subject=<source_id>`, `data={"source_id", "remediation": "rerun_enrich"}`.
- `stale_claim_citation` → `subject=<claim_id>`,
  `data={"claim_id", "source_id", "char_start", "char_end", "remediation": "rerun_extract_claims"}`.
Remediation is a **stable code**, not only prose. Operator docs map the codes:
`rerun_enrich` → run enrichment for the affected source(s); `rerun_extract_claims` → re-run
extraction/claim maintenance for the affected source(s).

**6. Follow-up (design-locked 2026-06-24): `synthesis_rot`; concept/entity rot deliberately dropped.**
Extending the same pattern to graph-composed pages — synthesis only.
- **`synthesis_rot`** (low, report-only, never `failing`; graph-gated): an **active** synthesis whose
  durable artifact (`normalized/enrichment/<topic_id>.synthesis.json`) `input_fingerprint` ≠
  `synthesis._fingerprint(current topic, settings.enrich_model_heavy)`. This *is* the producer's existing
  `stale_active` comparison (synthesis.py), surfaced by lint — fully deterministic/key-free. **Topic-driven
  enumeration via `eligible_topics(...)`** (provenance built key-free from `valid_manifests`+
  `get_provenance`): for each still-reconstructable topic, look up `synthesis_id(topic_id)`; active node +
  artifact present + fingerprint drift → `synthesis_rot`; active node + artifact missing/unreadable →
  `synthesis_unverifiable` (low → `degraded`). **Evidence-gone is out of scope:** a topic that no longer
  reconstructs (active evidence collapsed) is simply absent from `eligible_topics`, so it is never visited
  — that is a **verifiable governance-lifecycle** condition owned by the synthesis producer's deprecation
  flow, **not** rot and **not** `degraded`. `synthesis_unverifiable` is reserved strictly for
  missing/unreadable inputs needed to perform the check, never for a successful check that shows
  ineligibility. Payload: `subject=<synthesis_id>` (the stale object is the synthesis node/page),
  `data={"synthesis_id", "topic_node_id", "remediation": "rerun_synthesis"}` for both findings
  (`rerun_synthesis` → run `scripts/generate_synthesis.py`, needs the configured provider key).
- **No Concept / Entity rot check — deliberately dropped (not deferred).** Those pages are
  `generation_status: deterministic` graph **projections** with **no LLM-authored summary** to drift;
  their page↔graph consistency is already enforced by **`validate_projection`** (a hard validator → lint
  `failing`), and title/aliases are page-authoritative (ADR-0030). A "composed input fingerprint" rot
  heuristic would duplicate a stronger existing invariant. The remedy for projection drift is a **key-free
  re-render** (`generate_wiki`/reindex), not provider-keyed regeneration. **Revisit trigger:** only if
  Concept/Entity pages ever gain **LLM-authored** summaries/prose. Any actual gap in `validate_projection`
  is fixed *in the validator*, not via a parallel lint heuristic.

## Scope (v1) / deferred

**In (v1, decisions 1–5):** `summary_rot` + `stale_claim_citation` as report-only checks in `/jobs/lint`;
the two coverage findings; `LintFinding.data`; enumeration over durable `normalized/enrichment/*.json` +
`*.claims.json`; key-free, deterministic, no review vocabulary, no graph writes.

**Follow-up (decision 6):** `synthesis_rot` + `synthesis_unverifiable` — same report-only pattern, for the
one graph-composed page type that *has* an LLM artifact with a freshness fingerprint.

**Dropped — not deferred (decision 6):** Concept / Entity summary rot — deterministic projections with no
LLM artifact, already covered by `validate_projection`; a rot heuristic would duplicate it. Revisit only
if those pages gain LLM-authored prose.

**Deferred:** any auto-remediation executor (re-summarize / re-extract / re-synthesize need a model key, so
they stay operator-run); severity thresholds / a fourth health state.

## Consequences

- Maintenance debt (stale summaries / drifted citations) becomes visible and machine-actionable without
  expanding the governance ledger or weakening key-free CI.
- A model/prompt/schema version bump surfaces as a (potentially large) `summary_rot` count — expected, not
  a failure; the operator clears it by re-running enrich.
- Detection is fully reproducible from durable artifacts + current normalized markdown; the gitignored
  wiki pages are read only for coverage probing.
