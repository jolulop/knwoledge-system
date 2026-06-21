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
- **`GET /reviews?status=pending&type=&priority=&limit=&offset=`** → `{count, by_type, items[]}` from
  `reviews/<status>/`. Each item exposes its **explicit `status`** (`deferred` lives in `pending/` but
  is not semantically pending). Deterministic sort: **priority desc → `created_at` asc (when present) →
  `review_id`** (malformed/missing `created_at` falls back to `review_id`).
- **`GET /reviews/{id}`** → the full item + a **preview** = normalized read projection (affected page
  paths, node ids, current status, proposed status/action, warnings `apply_deferred`/`executor_missing`).
  *Not* a computed mutation diff.
- **Robustness:** a malformed/corrupt review JSON is skipped and reported (e.g. a `parse_errors` count /
  list), never crashes the queue.

---

## 3. Decision endpoints (record-only; ADR-0035 decision 3)
- `POST /reviews/{id}/approve` · `/reject` → `resolve_review_item(decision=...)` (pending → approved/
  rejected + `audit_log/`). `POST /reviews/{id}/defer` → keep in `pending/` with `status: deferred`
  (a new review-service `defer_review_item`, since `resolve_review_item` only does approved|rejected).
- Response: `{decision_recorded: true, status, apply_required}`. **No effect is applied here.** Works
  for every review type (type-agnostic governance ledger).

---

## 4. Apply (`POST /reviews/apply`; ADR-0035 decisions 4–5)
- Runs the existing deterministic key-free passes over `approved/`:
  `apply_resolved_syntheses` · `promote_candidates` · `apply_resolved_contradictions`, **plus** the new
  `apply_approved_deprecations`. Re-renders affected pages + mirrors graph node status, rebuilds
  `wiki/index.md`, runs validators.
- **Never triggers LLM generation** (only the deterministic review-application portion of any pass);
  **never touches `raw/`**; **idempotent**.
- Returns a **typed summary**: e.g. `{syntheses:{promoted,rejected}, promotions:{promoted},
  contradictions:{acknowledged,rejected,superseded}, deprecations:{applied,skipped[]},
  pages_changed, validators_ok, unapplied:[{type,count,reason}]}`.
- **`apply_approved_deprecations`** (new): only items with `type==deprecate_wiki_page`,
  `proposal.to_status==deprecated_candidate`, `subject.page` under a known wiki subdir, `context.node_type`
  matching the page type, no raw delete/archive/hide. Marks the page `deprecated_candidate` +
  `review_status: approved` via an **explicit `review_status` input to the deterministic render path**
  (claim/concept renderers gain a `review_status` arg — not frontmatter string surgery), preserving
  citations/evidence + summary callouts, mirroring graph node status, idempotent, reporting skips with
  reasons.

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
| **6-3** | Apply: `POST /reviews/apply` wiring the 3 existing executors + new `apply_approved_deprecations` (render-path `review_status` input); typed summary incl. unapplied. Tests (apply, idempotent, skip-reasons). |
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
