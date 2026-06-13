# Single-source concepts are allowed as candidates, promoted by recurrence or review

A concept seen in only one source is neither forbidden nor automatically first-class. It
is created as a **candidate** (status `candidate`/`stub`, low confidence) and excluded
from promoted navigation and from synthesis. It becomes `active` ("promoted") in one of
two ways:

- **Recurrence** — automatically, once it is evidenced by two or more sources. This is
  the deterministic, no-review path and matches the "≥2 sources before promotion"
  heuristic already stated in CONTEXT.md.
- **Review** — a human may promote a single-source candidate early, the
  `promote_single_source_claim_to_concept` action already gated in
  `policies/review.yaml`.

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
  recurrence. Until family detection exists, a candidate whose second source shares
  author/publisher/family metadata is **flagged for review** rather than auto-promoted.
- **Confidence, not just count.** Promotion considers evidence confidence — a concept
  asserted weakly in two sources may stay `candidate` pending review, while strong,
  clearly independent corroboration auto-promotes. `source_count` and per-source
  confidence are recorded on the concept (ADR-0022) so the rule is auditable.

Consequences: extraction can freely record candidate concepts without polluting the
promoted graph, so the system avoids both concept promiscuity and signal loss. The
linter distinguishes candidate from active and surfaces long-lived candidates for review
or cleanup rather than failing on them. A clear status lifecycle
(`candidate → active`, with `deprecated`/review states layered on later) gives Phase 4
graph and Phase 5 answering a principled rule for which concepts are citable. Like
ADR-0017 this is a deferred-phase decision; the Phase 3 backbone creates no concepts yet.
