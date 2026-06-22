# Phase 6 Plan — Human Review UI

**Status:** Planned (design-locked 2026-06-19 via grill gate). No code yet.
**Governing ADR:** [ADR-0035](adr/0035-phase-6-human-review-ui.md). Read it first.
**Predecessors:** Phases 1–5 complete. The Phase 3.5 workers already file review items under
`reviews/pending/` (ADR-0018); Phase 6 surfaces + actions them.

> [!summary]
> Phase 6 adds the Human Review UI: server-rendered HTML on the existing FastAPI app over a
> deterministic JSON read model. The decision **ledger is type-complete** (list + approve/reject/defer
> every pending review type, record-only via the review service); a separate explicit
> `POST /reviews/apply` runs the existing **key-free deterministic executors** (synthesis, promotion,
> contradiction) plus a new tightly-scoped deprecation executor, returning a typed summary with honest
> unapplied-by-type gaps. Mandatory proposal preview before approve; loopback-only safety; key-free
> deterministic tests.

---

## 1. Scope
**In:** read model (`GET /reviews`, `GET /reviews/{id}`); decision endpoints
(`POST /reviews/{id}/approve|reject|defer`, record-only); `POST /reviews/apply` (deterministic,
executor-backed) + a tightly-scoped `apply_approved_deprecations`; server-rendered HTML
(`/ui/reviews`, `/ui/reviews/{id}`, apply view); fixture-based key-free tests.

**Out / deferred:** SPA / rich client; live filesystem/wiki **diff/dry-run** preview; apply for
`change_entity_subtype` and the raw-touching types (`delete_raw_file`/`archive_raw_file`/`hide_content`);
CSRF token + any auth (loopback-only assumption); a CLI review tool (thin, later, over the same
JSON/service).

**Invariants:** HTML is never authority (calls the same review-service primitives as JSON); decide and
apply stay decoupled; apply is deterministic + key-free + idempotent + never touches `raw/`; no
destructive action without a recorded decision; the surface inherits the loopback-only no-auth bind.

---

## 2. Read model (`GET /reviews`, `GET /reviews/{id}`)
- **`GET /reviews?status=pending&type=&priority=&limit=&offset=`** → `{count, by_type, parse_errors,
  schema_errors, items[]}`. **Filter on the explicit `status` field, not the directory** (ADR-0035 A3): `pending`/
  `deferred` both scan `reviews/pending/` then filter `item.status`; `approved`/`rejected` scan their own
  dirs. **Default (no `status`) = `pending` only** — deferred excluded (the queue stays actionable;
  deferred reachable via `?status=deferred`). `count` and `by_type` are computed over the **full filtered
  set (status+type+priority) before `limit`/`offset`**; `items[]` is the sorted window after pagination.
  Deterministic sort: **priority desc → `created_at` asc (when present) → `review_id`** (malformed/missing
  `created_at` falls back to `review_id`).
- **`GET /reviews/{id}`** → the full item + a **preview** built by a **per-type projection registry**
  (ADR-0035 A1; no generic-first extraction, no raw passthrough). Each projector returns one normalized
  model: `{review_id, type, status, summary, affected_paths[], node_ids[], current_status, proposed_status,
  proposed_action, warnings[], apply:{…}, details{}}`. Record-only types reuse a shared
  `record_only_preview(...)` helper. The `apply` block carries the **read-time-derived effect state**
  (ADR-0035 A2): `{supported, executor, effect_status ∈ {pending_apply, effected, apply_deferred,
  unknown, no_effect_required}, effected, warnings[]}` — best-effort read of actual wiki/graph state,
  `unknown` on inconsistency, never a tracked applied-marker. `no_effect_required` marks a decided item
  that owes no world change (rejected promote / rejected in-scope deprecate); rejected synthesis/
  contradiction keep ordinary derivation. Effect checks read the **full** required state (supersede →
  edge + `supersedes` + loser deprecation; synthesis → node + page; in-scope deprecate → page + graph
  mirror, else `unknown`). *Not* a computed mutation diff. **Projectors are strictly read-only**
  (ADR-0035 A2): they read pages/frontmatter/review files/graph but never init DBs, create dirs, repair
  pages, or call producer/apply code.
- **Robustness:** unusable files are skipped + counted, never crashing the queue — `parse_errors`
  (unreadable/invalid/non-object JSON) vs `schema_errors` (valid JSON, not a usable ReviewItem shape);
  `GET /reviews/{id}` 404s either.

---

## 3. Decision endpoints (record-only; ADR-0035 decision 3)
- `POST /reviews/{id}/approve` · `/reject` → `resolve_review_item(decision=...)` (pending → approved/
  rejected + `audit_log/`). `POST /reviews/{id}/defer` → keep in `pending/` with `status: deferred`
  (a new review-service `defer_review_item`, since `resolve_review_item` only does approved|rejected).
- Response: `{decision_recorded: true, status, apply_required}`. **No effect is applied here.** Works
  for every review type (type-agnostic governance ledger).

---

## 4. Apply (`POST /reviews/apply`; ADR-0035 decisions 4–5, addenda A4–A6)
- Composes **extracted key-free apply orchestrators** (ADR-0035 A4) — `apply_synthesis_decisions` ·
  `apply_contradiction_decisions` · the new `apply_approved_deprecations` · `promote_candidates(
  rebuild_index=False)`. The two extracted orchestrators pull the deterministic apply portion (executor →
  recompose affected pages → mirror graph → `{changed_pages, graph_changed, summary}`) **out of** the LLM
  producers (`detect_contradictions`/`generate_synthesis`) so both the producers and the endpoint call the
  same code (bare executors alone don't re-project pages / rebuild the index). Calling producers with a
  no-key client, and inline re-implementation in the endpoint, are both rejected. **No behavior change** to
  existing producer entrypoints; existing tests stay the regression guard + new direct orchestrator tests.
- Then rebuilds `wiki/index.md` **once** (only if something changed) and runs validators **once**.
- **Never triggers LLM generation** (only the deterministic review-application portion of any pass);
  **never touches `raw/`**; **idempotent**.
- **Non-transactional; validators report, never roll back (A6):** effects are written before validation
  and cannot be rolled back. Runs the **full validator suite once at the end** (discovered like
  `scripts/validate_all.py`, each a subprocess `[sys.executable, script, root]`). On any failure returns
  **HTTP 200** with a clear top-level `status` (`"applied"` | `"validation_failed"`) plus `{applied:true,
  validators_ok:false, failed_validators:[{name, returncode, stdout_tail, stderr_tail}], summary:{…}}` —
  clients read `status` directly, not nested fields. HTTP 500 only for unexpected infrastructure errors in
  the route's own control flow.
- Returns a **typed summary**: e.g. `{status, applied, validators_ok, failed_validators[], summary:{
  syntheses:{promoted,rejected}, promotions:{promoted}, contradictions:{acknowledged,rejected,superseded},
  deprecations:{applied,normalized,skipped[]}, pages_changed, unapplied:[{type,count,reason}]}}`.
- **`apply_approved_deprecations`** (new; A5): only items with `type==deprecate_wiki_page`,
  `proposal.to_status==deprecated_candidate`, `subject.page` under an in-scope subdir, `context.node_type`
  matching the page type, no raw delete/archive/hide. **In scope:** `Claims/`, `Concepts/`, `Entities/`,
  `People/`, `Organizations/`, `Projects/`; **out of scope:** `Synthesis/` (→ `skipped[{reason:
  handled_by_synthesis_executor}]`), `Sources/`, `Queries/`. Marks the page `deprecated_candidate` +
  `review_status: approved` via an **explicit `review_status` input to the deterministic render path** —
  the render seam is **mandatory** because the renderers derive `review_status: pending` for a no-evidence
  claim tombstone / no-mention concept and cannot otherwise express an approved deprecation.
  `render_claim_page`/`render_concept_page` gain an optional `review_status: str | None = None` (default
  `None` = today's derived behavior); claims reuse `recompose_claim(deprecate=True, review_status=
  "approved")`, concepts/entity-family use the new **`recompose_semantic_node_page(...)`** helper in
  `concepts.py`. Preserves citations/evidence + summary callouts, mirrors graph node status, reports skips
  with reasons. **Idempotency:** a true no-op needs page status **and** `review_status` **and** graph
  mirror to match; if only `review_status` differs (e.g. an auto-approved contradiction-supersede
  deprecation), it performs a **normalization apply** (flip `review_status`, mirror graph, count as
  `normalized`) rather than skipping.

---

## 5. HTML UI (server-rendered; ADR-0035 decisions 1, 6)
- `/ui/reviews` — the queue (default pending; filter links by type/priority; counts), each row linking
  to detail. `/ui/reviews/{id}` — detail with the **mandatory preview** (subject/proposal/context/
  affected pages-nodes/winner-loser) and approve/reject/defer **`POST` forms**. An apply view
  (`/ui/reviews/apply` or a button) runs `POST /reviews/apply` and shows the typed summary.
- Rendered server-side (Jinja2 or hand-rolled), no SPA/JS build. "Approved, pending apply" until applied.
- Mutating forms are `POST`-only; inherits `assert_safe_bind` (loopback-only). No new bind surface.

---

## 6. Sub-slices (each committable + validated)
| Slice | Deliverable |
|---|---|
| **6-1** | Read model: review-service read helpers (list/get, malformed-robust, deterministic sort, explicit status) + `GET /reviews` + `GET /reviews/{id}` JSON + response models. Tests. |
| **6-2** | Decision endpoints: `defer_review_item` + `POST /reviews/{id}/approve|reject|defer` (record-only, audit). Tests. |
| **6-3** | Apply: extract `apply_synthesis_decisions`/`apply_contradiction_decisions` (key-free, no producer-behavior change) + new `apply_approved_deprecations` (render-path `review_status` arg + `recompose_semantic_node_page`); `POST /reviews/apply` composes them + `promote_candidates(rebuild_index=False)`, rebuilds index once, runs full validator suite once (200 + `validators_ok` on failure); typed summary incl. `normalized`/`unapplied`. Tests (apply, idempotent/normalization, skip-reasons, validator-failure → 200, extracted-orchestrator direct tests). |
| **6-4** | HTML UI: `/ui/reviews` + `/ui/reviews/{id}` + apply view (server-rendered, mandatory preview, `POST` forms). TestClient HTML tests. |

---

## 7. Testing posture (key-free, deterministic)
- `TestClient` for JSON + HTML; a fixture `reviews/` queue (write `pending/` items across types).
- Cover: list/filter + by_type counts + deterministic sort; detail + preview projection;
  approve/reject (→ correct dir + `audit_log/`) + defer (→ `pending/` `status: deferred`); apply
  (executors run, pages re-rendered + graph mirrored + index rebuilt + validators pass + typed summary
  incl. `unapplied`); idempotent apply; **malformed review JSON skipped/reported without crashing**;
  HTML renders + form-POST round-trip; no absolute/server path leak.
- No LLM anywhere in the path.

---

## 8. Success criteria (Phase 6 done when)
- Every pending review type is listed and decidable (approve/reject/defer) through the UI + JSON API,
  record-only, audited.
- `POST /reviews/apply` deterministically applies synthesis/promotion/contradiction + scoped
  deprecation decisions, re-renders pages + graph, rebuilds the index, runs validators, and reports
  unapplied types honestly; idempotent; key-free; `raw/`-free.
- Mandatory proposal preview before approve; loopback-only bind inherited; malformed queue state never
  crashes the read model.
- Full suite + ruff + validators green. → Phase 7 (Autonomous Maintenance).
