# Single-source candidate nodes are allowed, promoted by recurrence or review

A graph node of any **promotable type** — concept, entity, person, organization, or
project (the entity family is subtyped per ADR-0021) — seen in only one source is neither
forbidden nor automatically first-class. It is created as a **candidate** (status
`candidate`/`stub`, low confidence) and excluded from promoted navigation and from
synthesis. It becomes `active` ("promoted") in one of two ways:

- **Recurrence** — automatically, once it is evidenced by two or more *independent*
  sources (see the threshold below). This is the deterministic, no-review path and
  matches the "≥2 sources before promotion" heuristic stated in CONTEXT.md.
- **Review** — a human may promote a single-source candidate early.

Both paths are mediated by a single review action, **`promote_candidate_node`** (gated in
`policies/review.yaml`), keyed by `{node_id}`. The promotion worker
(`app/workers/promote.py`, Phase 3.5b slice 5) is the one writer:

- **Early promotion (review):** if a node's `promote_candidate_node` item is already
  *approved* when the worker runs, the node promotes regardless of source count. This is
  the human early-promotion path; the loop is already closed, so no audit entry is added.
- **Recurrence:** if the independence threshold is met, the worker promotes and closes the
  loop deterministically — it ensures the review item exists (creating it if a legacy or
  hand-deleted state left it missing) and then resolves it `approved` with
  `decided_by: "recurrence"`, writing one `audit_log` entry.

Status lives on the **page frontmatter** as the authority (ADR-0030); the worker mirrors
it onto the `nodes` index, and re-extraction preserves an already-`active` status rather
than resetting it. The pass is idempotent: a rerun skips already-active nodes and adds no
duplicate audit entries.

Forbidding single-source concepts was rejected because it loses signal: a promising idea
appearing in one source could not even be tracked until a second source happened to
arrive. Requiring review for every nascent concept was rejected as too heavy — it would
put a human in the loop for routine extraction noise. The candidate model subsumes the
review option (review is simply the early-promotion path) and aligns with every existing
signal in the repository: the Build Spec's "promote only after recurrence or review",
the CONTEXT heuristic, the review-policy gate, and the maintenance lint check for
"concepts with fewer than two sources" (which now reads as "candidates awaiting a second
source or a promotion decision", not an error).

**What counts as an independent source** for the ≥2 threshold must be defined, or the
rule is trivially gamed. The threshold counts *distinct, independent* sources, not raw
mentions:

- **Exact duplicates count once.** Byte-identical copies already collapse to a single
  `source_id` (ADR-0007), so they are inherently one source and can never satisfy the
  threshold by themselves.
- **Same author / report family is not independent corroboration.** Two sources from the
  same author, publication, or report family (e.g. parts 1 and 2 of one report, or a
  press release and the article reprinting it) do not, on their own, establish
  recurrence. Independence is judged from manifest provenance
  (`author`, `publisher`, `report_family`, `canonical_url` — manifest-owned, never on the
  graph). Two sources are independent iff there is at least one *comparable* key (present
  on both) whose values differ **and** no comparable key is equal; non-comparable or
  unknown keys never establish independence. Manifests carry no provenance by default, so
  the gate is conservative — nothing auto-promotes until provenance is populated.
- **Values are canonicalized before comparison** so trivial variants don't read as
  independent: text keys are whitespace-collapsed and case-folded; `canonical_url` is
  additionally stripped of a `#fragment` and trailing slashes (scheme/host case folds via
  the text rule, but no `www.`/scheme normalization — that would risk false *merges*).
- **Confidence, not just count.** Promotion may consider evidence confidence — a node
  asserted weakly in two sources may stay `candidate` pending review, while strong,
  clearly independent corroboration auto-promotes. `source_count` and per-source
  confidence are recorded on the node (ADR-0022) so the rule is auditable.

Consequences: extraction can freely record candidate concepts without polluting the
promoted graph, so the system avoids both concept promiscuity and signal loss. The
linter distinguishes candidate from active and surfaces long-lived candidates for review
or cleanup rather than failing on them. A clear status lifecycle
(`candidate → active`, with `deprecated`/review states layered on later) gives Phase 4
graph and Phase 5 answering a principled rule for which concepts are citable. This is
implemented in Phase 3.5b: slice 4 records candidate nodes and files their
`promote_candidate_node` items, and slice 5 (`app/workers/promote.py`) is the promotion
worker described above.
