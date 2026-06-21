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
