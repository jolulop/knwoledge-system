# ADR-0064 — Merge-candidate producer + human-filed merge (item consolidation, Part A)

- **Status:** implemented (2026-10-23; design-locked the same day by grill-phase) —
  `app/workers/merge_candidates.py` (producer), `lint._check_merge_candidates` (the `/jobs/lint`
  check), `app/workers/human_merge.py` + `POST /reviews/merge` + `GET /ui/reviews/merge/preview` →
  `POST /ui/reviews/merge` (queue-page form, two-step), `merges.conflicting_merges` (shared conflict
  rule); tests `tests/test_merge_candidates.py`, `tests/test_human_merge.py`. **Review round 1
  applied 2026-10-23** (see the section at the end): pair-level rejection for human filing too,
  merge-conflict guard (filing + approve + executor), direct-match-only proposals, a key budget, and
  a no-write confirm step before the pre-approval.
- **Date:** 2026-10-23
- **Drivers:** UAT round 4 (fresh `/tmp/ks-uat`, 5 sources): 114 items, of which only **4 are shared**
  across sources (agentic-ai ×3; large-language-models, cursor, mckinsey-company ×2) — the item layer
  is fragmented. Item identity is `sha256(_normalize_name(title))` (lowercase + whitespace collapse,
  `app/workers/items.py`), so case/whitespace twins cannot exist, but alias-level twins do: *Agentic AI*
  ↔ *AI Agents and Multi-agent Systems* (alias "agentic AI"), *McKinsey* ↔ *McKinsey & Company* (both
  directions), plus one false alias (*Theory of Mind in AI* → "agent phenomenology"). The
  `merge_items` executor (ADR-0050/0059) and `mark_semantic_duplicate` (ADR-0041) are shipped, preview-
  and dry-run-backed — but **no producer, API or UI files either review type**, so consolidation is
  unreachable in practice.
- **Related:** ADR-0036/0037 (lint = detect-and-propose; idempotent `_file_review`), ADR-0041
  (`mark_semantic_duplicate`), ADR-0045 (reopen), ADR-0050 (merge model, subject, guards, withdrawal
  matcher, alias union), ADR-0057 (stored `reason_code` provenance), ADR-0058 (human-add pre-approved
  producer path + collision routing precedent; per-source lens keeps identity types global), ADR-0059
  (item family), ADR-0030 (page frontmatter owns title/aliases).

## Context

Part A of the item-consolidation slice makes the existing merge executor *reachable*: a deterministic
machine producer that proposes merges, and a human action to start one the producer didn't detect.
Part B (existing-item vocabulary in the extraction prompt, naming rules, salience threshold,
alias-aware candidate routing) is a separate, eval-measured slice with its own ADR. Fuzzier identity
normalization (re-hashing ids) is **rejected** — it would rekey every item and hide referent decisions
inside a hash.

## Decisions

1. **Review type = `merge_items` only.** The producer files the identity-surgery type that actually
   shrinks the item set (B → tombstone, edges re-pointed to A). `mark_semantic_duplicate` stays an
   annotation executor with no producer (unchanged); manual duplicate filing is a deferred follow-up.

2. **Surface = a new graph-gated check in `/jobs/lint`** (ADR-0036 "detect and propose"): check name
   `merge_candidate`, run inside the existing graph-gated block, honouring `file_review_items`, job
   recording, the `wiki/log.md` line and `review_items_filed`/`review_items_existing`. Key-free,
   deterministic, no model call, no embedder. Findings are severity **low** (never make lint
   `failing`).

3. **Match signal = alias cross-match with conservative variant folding.** Each item X contributes a
   key set `K(X) = {merge_key(title)} ∪ {merge_key(a) for a in aliases}`; a pair matches iff
   `K(X) ∩ K(Y) ≠ ∅` (empty keys ignored). `merge_key(s)`, applied identically to titles and aliases:
   1. Unicode **NFKC**, then `items._normalize_name` (lowercase, whitespace collapse);
   2. `&` → ` and `; apostrophes deleted; hyphens and all other punctuation → space; collapse spaces;
   3. **plural fold on the LAST token only**: strip one trailing `s` iff the token is ≥4 chars and
      does not end in `ss`, `us` or `is` (`large language models` = `large language model`,
      `llms` = `llm`; `analysis`, `business`, `status` unchanged).
   A false fold (e.g. `news` → `new`) costs one human reject. **Not in v1 (deferred, eval-gated):**
   token containment (`change-management` ⊂ `ai-change-management` — poor precision: `ai-fluency` vs
   `ai-fluency-hiring` are distinct), embedding similarity, any fold beyond the above. Measured
   baseline: on UAT r4 the fold adds **0** pairs over plain alias cross-match; it is kept as cheap
   insurance for corpora with plural/ampersand variants. Implementation check (read-only, UAT r4
   vault): 5 proposals — the 3 known pairs plus 2 **shared-alias** pairs (*Change Management* ↔
   *AI change management* via "ai adoption"; *AI organizational transformation* ↔ *AI Transformation
   in Healthcare* via "ai transformation") that a title↔alias-only scan missed.
   **Authority:** node set + status come from the graph `nodes` table; title + aliases are read from
   the item page frontmatter (`wiki/Items/<slug>.md`, ADR-0030). A node whose page is missing or
   unparseable is skipped (projection integrity is `validate_projection`'s job, not lint's).

4. **Lifecycle scope = active ↔ active only** (the executor's ADR-0050 guard — every filed item is
   executable). A matching pair where either side is `candidate` → report-only finding
   `merge_candidate_unpromoted` (low), never filed; candidate-time consolidation belongs to Part B.
   `merged`/`deprecated_candidate`/hidden/other statuses are ignored entirely.

5. **Survivor = most-supported wins** (deterministic, byte-stable on reruns). Rank by
   (a) live support desc — the same count `under_supported_item` uses:
   `graph.count_independent_sources(gconn, id, source_statuses=search.RETENTION_DEFAULT_STATUSES)`;
   (b) inbound alias pointers desc — how many other members carry this item's `merge_key(title)` in
   their alias keys; (c) `item_id` asc. Approving the item applies ADR-0050 unchanged (survivor keeps
   its title + `item_type`; aliases unioned).

6. **Pair-keyed rejection memory (machine-side).** An unordered pair `{X, Y}` is **suppressed** if any
   `merge_items` item with subject `{X, Y}` in **either direction** exists in `pending/` (status
   `pending` or `deferred`), `approved/` or `rejected/`. A rejection therefore means "not a merge" for
   the pair, regardless of later survivor flips. Withdrawn items (removed from `pending/`) don't
   suppress. `review_id` hashing (`type|subject`) is **unchanged**. *(Review round 1: the human-filed
   action honours the same pair semantics for rejections — decision 10.)*

7. **In-flight nodes are frozen.** A node that is survivor or absorbed in an unresolved merge
   (`pending`/`deferred`) or in an approved merge whose absorbed node is still `active` is excluded
   from new filing this run — proposals never stack on undecided ones, so a survivor flip can't create
   a contradictory second proposal.

8. **Clusters = a star around one survivor.** Build the match graph over active, non-in-flight items,
   **removing suppressed pairs first** (so a rejected false alias can't bridge two clusters), take
   connected components of size ≥2, pick ONE survivor per component (decision 5), and file
   `survivor absorbs M` for each other member M **that directly matches the survivor** (review round
   1 — an indirect member waits: once `A←B` applies, the alias union gives A B's names and the next
   run proposes `A←C` with real A–C evidence, never a transitive guess). Never a chain: in a chain `A←B, B←C`, applying `A←B` withdraws the pending `B←C`
   (ADR-0050 `superseded_by_merge`, it references absorbed B) and C is silently dropped.
   **Item shape:** `subject = {survivor_node_id, absorbed_node_id}`; `proposal = {to_status: merged,
   reason_code: "alias_cross_match", matched_keys: [...]}` (ADR-0057 stored provenance);
   `context = {survivor/absorbed title + slug + support, match evidence [{key, survivor_field,
   absorbed_field}], component_size}`; priority low. Per-source review lens (ADR-0058) is unchanged —
   `merge_items` stays a global flat-queue type.
   **Key budget (review round 1):** a merge key carried by more than `max_items_per_key` active items
   (default 5, `policies/review.yaml` `merge_candidates.max_items_per_key`, integer ≥2) pairs nothing
   and is reported once as `merge_key_too_common` (low) — bounds pair generation (benchmarked: 1000
   items sharing one alias → 0 proposals, 1 finding).

9. **Human-filed merge = pre-approved producer act (ADR-0058 style).** One worker
   (`app/workers/human_merge.py`: write-free `plan_merge` + `file_merge`) behind thin routes. UI is
   **two-step** (review round 1): the `/ui/reviews` queue form (`survivor`/`absorbed`, each an `itm_`
   id or an Items slug, resolved server-side — no dropdown of every item) GETs
   `/ui/reviews/merge/preview`, which **writes nothing** and shows the routing outcome (file / approve
   in place / withdrawals / block) plus the blast radius (absorbed active-edge count, sources, survivor
   aliases after the union); its Confirm button POSTs `/ui/reviews/merge` (re-planned at submit, so a
   stale preview can't bypass a block; 303 to `/ui/reviews/{rid}`, where ADR-0045 reopen is
   available). The JSON route stays single-step for scripts:
   `POST /reviews/merge` (JSON `{survivor, absorbed}` → `{review_id, outcome, withdrawn}`). Filing
   **is** the approval: create the item, then `reviews.resolve_review_item(decision="approved",
   decided_by="human")`, plus a purpose-named `audit_log/<rid>-human-filed-merge-<hex>.json` entry.
   `proposal.reason_code: "human_filed"`. Apply is **unchanged** — explicit, queue-wide,
   dry-run-previewed (ADR-0040). Graph-required (graph unavailable → 503 / unavailable page).
   **Filing-time guards** (blocked → nothing written, typed reason): unknown/ambiguous ref, not an
   item, self-merge, survivor or absorbed not `active`, survivor already `merged`.

10. **Human-filed collision routing** *(as amended by review round 1)*. Two merges **conflict** iff
    they share the absorbed node, or one's absorbed node is the other's survivor (a chain — the
    reverse direction is the two-node case); a shared survivor is a compatible star
    (`merges.merges_conflict`).
    - a **rejection covers the pair**: a rejected merge in **either** direction → **BLOCK**
      `merge_previously_rejected` (message names who/when/review id, and says whether it was the
      reverse) — never bypassed. "Wrong survivor" is expressed explicitly: reopen the rejection
      (ADR-0045, audited, back to pending), then file the other direction, which withdraws the reopened
      item (audited) — no new ledger status needed; the reopen reason records why;
    - same direction `approved` → no-op `already_approved`;
    - a conflicting merge `approved` and not yet effected (absorbed node not `merged`) → **BLOCK**
      `conflicting_merge_approved` (apply or reopen it first);
    - same direction `pending`/`deferred` → **approve that item in place** (no parallel subject);
    - conflicting merges `pending`/`deferred` → **withdrawn** (`superseded_by_human_merge`), then the
      human's direction is filed.

## Consequences

- The existing merge executor becomes reachable from both machine and human paths with no
  subject-contract, validator or migration change; the executor gains one typed skip (below).
- Apply-order hazard (documented, not new): lint may file `deprecate_wiki_page` (under-supported) on
  an item it also proposes as absorbed. Approving the merge first withdraws the pending deprecate
  (ADR-0050); applying the deprecate first leaves B `deprecated_candidate`, and the merge then skips
  `absorbed_not_active`. Operator guidance: decide merges before deprecations.
- False aliases from extraction surface as merge proposals; one reject retires the pair permanently
  (decision 6). Fixing the alias itself is a Part B / rename concern.
- **Acceptance (fixture, not the live corpus):** a disposable vault reproducing the UAT r4 shapes
  files *Agentic AI* ⟵ *AI Agents and Multi-agent Systems* and one McKinsey star, files the false
  Theory-of-Mind pair, and after that pair is rejected a rerun files nothing new for it in either
  direction; reruns are byte-stable; a candidate-side match is reported, not filed.

## Deferred

Manual `mark_semantic_duplicate` filing; token-containment and embedding signals (eval-gated);
merging candidates; Part B (prompt vocabulary, naming rules, salience, alias-aware candidate routing);
live un-merge (ADR-0050 forward-only stands).

## Review round 1 (2026-10-23)

Applied as recommended:

1. **Conflicting approvals (blocking).** Two approved merges could share an absorbed node (`A←B` +
   `C←B`) or chain — the executor's approved-unapplied gate then blocks both, or, if one applied
   first, the other became a silent no-op whose projector reads `UNKNOWN partial_merge_state`. Fix:
   the shared conflict rule (decision 10) is enforced at **human filing** (block approved-unapplied,
   withdraw pending), at the generic **approve** path (`POST /reviews/{id}/approve` and every UI
   decide route → 409 naming the conflicting approved merge; reject/defer stay allowed), and in the
   **executor** — an absorbed node already merged into a *different* survivor is a typed skip
   `absorbed_merged_elsewhere`, never silent (amends ADR-0050's "completed merge is a true no-op",
   which now holds only for a tombstone pointing at this survivor).
2. **Reverse of a rejected pair (blocking).** Human filing now treats a rejection as covering the pair,
   matching the producer (decision 10); flip-the-survivor goes through reopen.
3. **Indirect star members.** Direct-match-only filing (decision 8).
4. **No candidate budget.** `max_items_per_key` (decision 8).
5. **Approval before preview.** Product decision: keep "the filing is the approval", but the UI shows
   a write-free preview first (decision 9); the JSON route stays single-step.
6. **Docs lag.** Build Specification §15 endpoint list, REANCHOR, Operations, CONTEXT updated.

