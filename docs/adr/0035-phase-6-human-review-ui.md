# Phase 6: Human Review UI — server-rendered review ledger over the deterministic FastAPI app

Phase 6 (Build Spec §13/§14, "Human Review UI") is the first **user-facing** surface and the
operational home for the human-approval gates CLAUDE.md rule 9 mandates (deprecation, contradiction
resolution, entity/concept merge, deletion, duplicate/low-confidence relationship changes). The Phase
3.5 workers already file these as durable artifacts under `reviews/pending/` (ADR-0018,
`policies/review.yaml`, `reviews.create_review_item`/`review_id`/`resolve_review_item`/
`withdraw_review_item`), but nothing surfaces or actions them. Phase 6 adds the surface — without a
parallel decision/apply code path.

A load-bearing fact shapes the whole phase: **decide and apply are already decoupled.**
`resolve_review_item` only *records* a decision (moves `pending/`→`approved/`|`rejected/` + writes
`audit_log/`); the *effect* is applied later by deterministic, idempotent, **key-free** worker passes
that scan `approved/` — `synthesis.apply_resolved_syntheses`, `promote.promote_candidates`,
`contradictions.apply_resolved_contradictions`/`_execute_supersede`. Phase 6 preserves that split.

## The load-bearing decisions

**1. Server-rendered HTML on the existing FastAPI app, over a shared JSON/service read model.** No
SPA, no JS build step, no second server — the review UI is HTML rendered server-side beside the JSON
API (`GET /reviews`, `GET /reviews/{id}` return JSON; `/ui/reviews`, `/ui/reviews/{id}` render HTML;
forms `POST` to the decision/apply endpoints). The Build Spec permits "React or server-rendered"; a
SPA is unnecessary weight for a single-user local tool, and a pure CLI does not satisfy "UI" (a thin
CLI may be added later over the same JSON/service primitives — not in v1). **The HTML layer is never
authority**: it calls the same review-service functions as the JSON path, so the API and the UI can
never diverge.

**2. A deterministic, robust read model.** `GET /reviews?status=&type=&priority=&limit=&offset=`
returns `{count, by_type, items[]}` from `reviews/<status>/`; each item carries its **explicit
`status`** (a `deferred` item lives in `pending/` but is *not* semantically pending). Deterministic
sort: **priority desc, then `created_at` asc when present, falling back cleanly to `review_id`** (a
missing/malformed `created_at` never breaks ordering). `GET /reviews/{id}` returns the full item plus a
**preview** — defined as a *normalized read projection, not a computed mutation diff*: affected page
paths, node ids, current status, proposed status/action, and warnings (`apply_deferred`,
`executor_missing`). Preview is mandatory (decision 6) without promising a dry-run engine in v1. The
read model is robust to filesystem state: a **malformed/corrupt review JSON is skipped and reported,
never crashes the queue**.

**3. The decision ledger is type-complete; decisions are record-only.** `POST /reviews/{id}/approve`,
`/reject`, `/defer` record the human decision **only** (approve/reject via `resolve_review_item`;
defer keeps the item in `pending/` with `status: deferred`) and write `audit_log/`; the response says
`decision_recorded: true` (+ `apply_required` when applicable). The UI **lists and lets the human
decide every pending review type** — the ledger is the governance surface, so `change_entity_subtype`,
`deprecate_wiki_page`, etc. are never hidden just because apply is incomplete.

**4. Apply is an explicit, deterministic, executor-backed action.** `POST /reviews/apply` runs only the
existing key-free apply passes — `apply_resolved_syntheses` (`propose_synthesis`), `promote_candidates`
(`promote_candidate_node`), `apply_resolved_contradictions` (`resolve_contradiction`) — plus a new
tightly-scoped deprecation executor (decision 5), re-rendering affected pages + graph, rebuilding
`wiki/index.md`, running validators. It returns a **typed summary that includes unapplied approved
items by type/reason** (so coverage gaps are honest, not silent). Apply is **idempotent**, **never
triggers LLM generation** (only the deterministic review-application portion of any pass runs), and
**never touches `raw/`**. The HTML detail view shows "Approved, pending apply" until `/reviews/apply`
runs. This avoids a per-type apply path inside the approve handler and reuses the verified executors
verbatim.

**5. A tightly-scoped `apply_approved_deprecations` executor (new in v1).** `deprecate_wiki_page` is
the most common pending type, so record-only would leave the UI's dominant action unable to complete.
The executor is constrained to items with `type == "deprecate_wiki_page"`,
`proposal.to_status == "deprecated_candidate"`, `subject.page` under a known wiki subdir, and
`context.node_type` matching the page type — **no raw deletion/archive/hide behavior**. It marks the
page `deprecated_candidate` + `review_status: approved` via an **explicit `review_status` input to the
deterministic render path** (not brittle frontmatter string surgery), preserves citations/evidence and
summary callouts, mirrors the graph node status, rebuilds the index, runs validators, is idempotent,
and **reports skipped items with reasons** rather than guessing. The deprecation is reversible
(`deprecated_candidate`, never deleted). `change_entity_subtype` (identity re-keying, merge/split-class
complexity) and the raw-touching types (`delete_raw_file`/`archive_raw_file`/`hide_content`, no
producers/executors) stay **record-only / apply-deferred** in v1.

**6. Mandatory proposal preview before approve.** A human must see the item's `subject`, `proposal`,
`context`, affected pages/nodes, and any winner/loser (contradiction) — the normalized projection of
decision 2 — before the approve action. This is a human-readable rendering of the proposal payload, not
a filesystem/wiki diff engine (deferred).

**7. Safety rests on the loopback-only, no-auth bind (ADR-0009).** The HTML views and the mutating
`POST` endpoints live on the same FastAPI app, gated by `assert_safe_bind` (loopback only; a non-loopback
bind requires an explicit acknowledged override). Mutating HTML forms are **`POST`-only**. A same-app
CSRF token is **deferred**, acceptable **only while bound to loopback** — this ADR makes that
conditional explicit: any move to LAN/public bind or added auth **must revisit form safety (CSRF) and
the no-auth assumption first**. No destructive action ever occurs without a recorded decision, and v1
apply is reversible + `raw/`-free.

**8. Deterministic, key-free tests.** No LLM in the review read/decide/apply path (the executors are
deterministic), so the whole phase is CI-gated key-free: `TestClient` for the JSON + HTML routes, a
fixture `reviews/` queue, and coverage of list/filter, detail+preview, approve/reject/defer (correct
dir + audit + `deferred` semantics), apply (executors run, page re-rendered + graph mirrored + index
rebuilt + validators pass + typed summary incl. unapplied), idempotent apply, **malformed-review-JSON
robustness**, HTML render + form-POST round-trip, and no path leak.

## Consequences

Phase 6 makes the rule-9 approval gates *operable* without inventing a second decision or apply path:
the UI is a thin governance surface over the existing review service + the verified deterministic
executors, the ledger is auditable, and coverage gaps are reported rather than hidden. The phase ships
one genuinely new executor (tightly-scoped deprecation apply) because the dominant review type would
otherwise be un-actionable. The standing trades: the UI is server-rendered and minimal (no rich
client, no live diff/dry-run — deferred); safety rests entirely on the loopback bind until auth/CSRF
are added (explicitly flagged); and identity-changing reviews (`change_entity_subtype`) plus
raw-touching reviews remain record-only until their executors exist. The load-bearing commitments —
server-rendered HTML over a shared JSON/service read model, a type-complete record-only decision ledger,
an explicit deterministic executor-backed apply step (+ scoped deprecation executor), loopback-only
safety, and key-free deterministic tests — are fixed here; the HTML styling, the exact read-model
filters/pagination, and the apply summary shape are tuned during implementation.

## Addenda (2026-06-22 grill — implementation contracts)

A phase-gate grill against the real review/executor code resolved the shapes the body left "tuned
during implementation". These are durable because they fix API contracts and a producer-internal
refactor boundary.

**A1 — Per-type preview projection registry (decision 2/6 refined).** `GET /reviews/{id}` builds its
mandatory preview from a **registry of one small projector per review type** (no generic-first
extraction, no raw-JSON passthrough — the payloads encode different governance decisions and a generic
extractor would hide the semantics). Each projector returns one normalized model:
`{review_id, type, status, summary, affected_paths[], node_ids[], current_status, proposed_status,
proposed_action, warnings[], apply:{…}, details{}}`. Record-only types reuse a shared
`record_only_preview(...)` helper, so the registry is type-complete without 13 hand-written large
functions. Unknown/unhandled type → generic fallback + `executor_missing` warning.

**A2 — Effect state is derived at read time, not tracked (decision 4 refined).** Approved items stay in
`approved/` permanently (executors are idempotent and never move/mark the file), so there is **no
applied-marker on disk** — and none is added (a second source of truth would drift). The per-type
projector instead does a **best-effort read of actual wiki/graph state** to populate an `apply` block:
`{supported: bool, executor: str|null, effect_status, effected: bool|null, warnings[]}` where
`effect_status ∈ {pending_apply, effected, apply_deferred, unknown, no_effect_required}`.
Missing/inconsistent state → `unknown` + warnings, never a guess. Record-only types →
`{supported:false, executor:null, effect_status:"apply_deferred", effected:null,
warnings:["executor_missing"]}` (never implies a *failed* apply). **`no_effect_required`** marks a
*decided* item whose decision owes no world change at all — a **rejected promotion** (the node stays
candidate) or a **rejected in-scope deprecation** (the page is left as-is) — so the UI never shows a
misleading "effected" badge on a do-nothing rejection. It is applied **narrowly**: a rejected
`propose_synthesis` and a rejected `resolve_contradiction` *do* have an executor reject-effect (node →
`deprecated_candidate`, edge → `rejected`), so those keep ordinary world-state derivation
(`effected`/`pending_apply`/`unknown`), not `no_effect_required`. The per-type effect checks read the
**full** required world state, not a partial signal: a contradiction **supersede** (the approved item
names a `winner`) is `effected` only when the `contradicts` edge is active **and** an active
`supersedes` edge winner→loser exists **and** the loser is `deprecated_candidate` (edge-active alone is
`pending_apply`); a synthesis is `effected` only when **both** the graph node and the `Synthesis/<id>.md`
page reach the target status; an in-scope deprecation is `effected` only when the page is marked **and**
the graph node mirror is confirmed (an unreadable graph / missing node → `unknown`, never a guess).
**Strictly read-only:** projectors may read pages, frontmatter, review files, and graph state, but must
**not** initialize missing DBs, create directories, repair pages, or call any producer/apply code —
absent or inconsistent state yields `unknown` + warnings, never a side effect. Per-type effect checks: synthesis → node+page in target status; promote → node `active` + item
approved; contradiction → edge `active`/`rejected` (+ supersede: loser `deprecated_candidate` +
`supersedes` edge); deprecate → page `deprecated_candidate` + `review_status: approved` + graph mirror.

**A3 — List semantics (decision 2 refined).** `GET /reviews` filters on the **explicit `status` field**,
not the directory: `pending`/`deferred` both scan `reviews/pending/` then filter `item.status`;
`approved`/`rejected` scan their own dirs. Default (no `status`) = `pending` only (deferred excluded —
the default queue stays actionable; deferred reachable via `?status=deferred`). `count` and `by_type` are
computed over the **full filtered set (status+type+priority) before `limit`/`offset`**; `items[]` is the
sorted window after pagination. Two top-level skip counters keep the queue crash-proof **and**
diagnosable: **`parse_errors`** (unreadable / invalid / non-object JSON) and **`schema_errors`** (a
valid JSON object that is *not* a usable ReviewItem — missing `review_id`/`type`/`status`, or a non-dict
`subject`/`proposal`/`context`). They are kept separate because a misbehaving producer (bad shape) and a
corrupt file on disk have different causes and fixes; both are skipped from `items[]` and would otherwise
500 the response-model validation. `GET /reviews/{id}` likewise 404s a schema-invalid file (the read
helper returns a `schema_error: true` marker for diagnostics, mirroring `parse_error`). `by_type` spans
the filtered queue only — global ledger composition, if ever needed, is a separate `/reviews/summary`
surface (deferred).

**A4 — `POST /reviews/apply` composes extracted key-free orchestrators (decision 4 refined).** The bare
executors are *not* a complete apply — the affected-page re-projection + index rebuild currently live
inside the LLM producer wrappers (`detect_contradictions`, `generate_synthesis`). Slice 6-3 therefore
**extracts** those deterministic apply portions into key-free orchestrators
`apply_contradiction_decisions(...)` and `apply_synthesis_decisions(...)` (each:
executor → recompose affected pages → mirror graph → return `{changed_pages, graph_changed, summary}`,
index rebuild **deferrable to the caller**), called by **both** the existing producers and the endpoint.
This makes "key-free apply" a real API boundary, not the side effect of an absent key (calling producers
with a no-key client is **rejected** — it would silently run LLM detection on a configured machine; inline
re-implementation in the endpoint is **rejected** — the projection/mirror logic is exactly what drifts
when copied). Constraints: **no behavior change** to existing producer entrypoints; existing
producer/promote tests remain the regression guard **plus** new direct tests for the extracted
orchestrators. The endpoint composes the orchestrators + the new `apply_approved_deprecations` +
`promote_candidates(rebuild_index=False)`, rebuilds `wiki/index.md` **once** (only if something changed),
then validates **once**.

**A5 — Deprecation executor mechanism + scope (decision 5 refined).** The render seam is **mandatory, not
optional**: the claim/concept renderers derive `review_status` from node state, and that derivation
yields `pending` for a no-evidence claim tombstone and a no-mention concept — there is no node-state input
that expresses an *approved* deprecation. So `render_claim_page`/`render_concept_page` gain an optional
`review_status: str | None = None` override (default `None` preserves today's derived behavior; explicit
`"approved"` used only by the deprecation apply path). Concepts have no recompose helper today; the
executor adds **`recompose_semantic_node_page(...)`** in `concepts.py` (named for the family — it serves
`concept/entity/person/organization/project`, all of which flow through `render_concept_page`/`NODE_DIR`).
Claims reuse `recompose_claim(deprecate=True, review_status="approved")`. **In scope (v1):** `Claims/`,
`Concepts/`, `Entities/`, `People/`, `Organizations/`, `Projects/`. **Out of scope:** `Synthesis/`
(reported `skipped[{reason: handled_by_synthesis_executor}]`), `Sources/`, `Queries/`, and any
raw-delete/archive/hide (record-only). Scope guard per item: `type==deprecate_wiki_page`,
`proposal.to_status==deprecated_candidate`, page under an in-scope dir, `context.node_type` matches the
page type. **Idempotency / normalization-apply:** a true no-op requires page status **and**
`review_status` **and** graph mirror to already match; if everything matches *except* `review_status`
(e.g. an auto-approved contradiction-supersede deprecation left the loser at `review_status` ≠ `approved`),
the executor performs a **normalization apply** — flip `review_status` to `approved`, mirror graph, count
it as applied/normalized rather than skipping.

**A6 — Apply is non-transactional; validators report, never roll back (new).** `POST /reviews/apply`
writes effects before validating and cannot roll back. It runs the **full validator suite once at the
end** (after the single index rebuild), discovered exactly as `scripts/validate_all.py` does and each run
as a subprocess `[sys.executable, script, root]`, capturing per-validator results. On any validator
failure it returns **HTTP 200** with a clear **top-level `status`** (`"applied"` when clean,
`"validation_failed"` when a validator failed) alongside `{applied:true, validators_ok:false,
failed_validators:[{name, returncode, stdout_tail, stderr_tail}], summary:{…}}` — so clients read the
outcome directly, not by inferring from nested fields. A 500 would falsely imply the whole operation
failed when the real state is "apply ran; validation found follow-up work." HTTP 500 is reserved for
unexpected infrastructure errors that prevent the route's own control flow. No targeted-subset shortcut in
v1 (explicit human-triggered governance action — full-suite latency is acceptable and avoids guessing
which invariants were touched).
