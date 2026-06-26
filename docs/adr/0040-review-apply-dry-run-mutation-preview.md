# ADR-0040 — Review apply dry-run: sandbox-on-copy mutation preview before commit

**Status:** Accepted. Design-locked **and implemented** 2026-06-26. `run_apply(settings)` + the
`GraphUnavailable` gate were extracted in `app/backend/main.py`; the sandbox builder + semantic differ
live in `app/backend/apply_sandbox.py`; `POST /reviews/apply/dry-run` and the UI gating
(`render_apply_dry_run`) are wired; covered by `tests/test_apply_dry_run.py`. Scope is narrowed to the
dry-run/preview path; the CLI review tool and supersede-via-UI are **deferred**.
**Extends:** ADR-0035 (Phase 6 human review UI; decide/apply decoupling, the per-type `ReviewPreview`
projector registry), ADR-0036 (Phase 7 maintenance + `archive_source` executor), ADR-0029/0030 (graph is
the source of truth for edges), ADR-0024/0009 (raw immutability + untrusted on-disk data). Read
`app/backend/main.py` (`apply_reviews`), `app/workers/{synthesis,contradictions,deprecations,retention}.py`,
`app/workers/promote.py`, `app/backend/review_read.py`.

## Context

`POST /reviews/apply` deterministically realizes approved decisions, but it mutates durable state
(graph edges/nodes, wiki pages, `reviews/` file moves, manifest status) **before** a human can see what it
will do. The five executors (`apply_resolved_syntheses`, `apply_contradiction_decisions`,
`apply_approved_deprecations`, `promote_candidates`, `apply_archive_sources`) write as they go and are
**non-transactional** (a manifest `set_status`, a `wiki.generate_wiki` re-render, and a graph upsert all
land mid-pass). There is already a per-type `ReviewPreview` *projector* (ADR-0035 A1), but it is a
**second implementation** of apply behavior — a read-model projection that can silently drift from the
executors (`review_read.py:213` explicitly calls its scope counts "scope, *not* a dry-run"). The Build
Spec's "review before mutation" posture and the upcoming governance executors need a preview that **cannot
drift** from what apply actually does. This ADR locks that preview as a sandbox-on-copy dry-run.

## Decisions

**1. Dry-run is apply-on-a-copy, not an independent projection (the no-drift mechanism).**
The dry-run **copies the mutable vault state into a throwaway sandbox root, runs the real executors against
the sandbox unchanged, then diffs sandbox-vs-live to emit a semantic plan, and discards the sandbox.** No
parallel "planner" re-implements executor behavior. **Executor behavior cannot drift** — the preview runs
the *same* mutation code on a copy (the semantic differ and review-id attribution are a separate, testable
read-only layer that could still have bugs; what is guaranteed is that the *mutations* match). The existing
`ReviewPreview` projectors remain useful single-item UI hints but are explicitly **rejected as the aggregate
dry-run mechanism** (they are a drift-prone twin path).

**2. Sandbox = a fully self-contained copy; no symlink or bind-mount path back to live.**
A snapshot-and-fail guard on a symlinked read domain is *after-the-fact* — by the time it fires, a
write-through has already mutated live state, breaking the contract and the raw/wiki separation posture
(AGENTS.md). So the sandbox is a **full copy** with **no live path reachable from it** (review round 1):
- **Copied, writable:** `db/` (graph + jobs, **excluding** `llm_cache.sqlite`), `reviews/`, `wiki/`,
  `raw/manifests/`.
- **Copied, read-only inputs the orchestration touches:** `scripts/` (validators + `rebuild_index.py` are
  discovered/run from `root/scripts/`), `templates/`, `policies/`, `normalized/`.
- **Copied raw bytes — manifest-referenced only:** `relative_raw_path` + every `occurrences[].relative_path`
  (reuse ADR-0039 `_catalogued_raw_index`), preserving paths under `raw/`. **Un-manifested `raw/inbox/`
  staging is not copied.** Unlike the ADR-0039 *backup* (which hard-fails on a missing catalogued raw file),
  the dry-run **must faithfully reflect live**: if a catalogued raw file is absent live, it is simply not
  copied, so `validate_raw_integrity` in the sandbox reports **the same condition live apply would** — the
  dry-run's job is to predict live, not to assert its own integrity bar.
- **Not copied:** `indexes/` (vector + keyword), the vector DB, `llm_cache`. Dry-run lets the orchestration
  regenerate `indexes/keyword/` inside the sandbox if it needs them, then **excludes `indexes/` from the
  semantic diff**.
- **Path rooting:** every executor/validator path is rooted at the sandbox via an explicit settings object;
  no hidden `Path.cwd()` / global-settings call may reach live. Because nothing live is reachable, isolation
  is by construction, not by a post-run check.

**3. Shared orchestration: extract `run_apply(settings)`; expose dry-run as its own endpoint.**
The orchestration currently inline in the `apply_reviews()` handler is extracted into one
`run_apply(settings)` that owns the **full sequence**: 5 executors → rebuild `wiki/index.md` → keyword
reindex → validator suite (decision 5). It takes an **explicit settings/root object** (no globals/`cwd`).
- `POST /reviews/apply` → `run_apply(live)` + commit + persist; returns the applied result (unchanged
  contract).
- `POST /reviews/apply/dry-run` → build sandbox → `run_apply(sandbox)` → diff(sandbox, live) → **discard
  sandbox**; returns the dry-run result; **never mutates live**.
- A GET verb is **rejected** (the work is heavy and executes mutation code — not a cacheable/idempotent
  read); a `dry_run` flag on `POST /reviews/apply` is **rejected** for this slice (a missing flag would
  cause real mutation; a separate route gives clearer logging/permissions/UI wiring).
- The endpoint decides live-vs-sandbox and response formatting; `run_apply` owns the sequence **and the
  blocking conditions** — in particular the graph-availability gate (decision 6) lives *inside* `run_apply`,
  so live and dry-run refuse on exactly the same condition and only the presentation differs.
- **`GET /ui/reviews/apply` runs a full dry-run on page load** (intentional, local-first): the step-1 page
  *is* the preview, so it builds a sandbox and runs the orchestration every load. Acceptable because apply
  is an explicit, human-triggered, low-frequency action; revisit only if it becomes a hot path.

**4. Output: domain-grouped semantic diff + appliable-items provenance.**
The dry-run result carries two views — *what* changes in durable state (grouped by domain) and *why* (traced
to the review item). Shape:

```json
{
  "diff": {
    "graph": {
      "edges_added":          [{"src": "...", "rel": "...", "dst": "...", "status": "...", "review_id": "..."}],
      "edges_removed":        [{"src": "...", "rel": "...", "dst": "...", "status": "..."}],
      "edges_status_changed": [{"src": "...", "rel": "...", "dst": "...", "from": "...", "to": "...", "review_id": "..."}],
      "nodes_status_changed": [{"id": "...", "type": "...", "from": "...", "to": "..."}],
      "nodes_added":          [{"id": "...", "type": "...", "status": "..."}]
    },
    "wiki":      [{"path": "wiki/Claims/...", "unified_diff": "...", "review_ids": ["..."]}],
    "reviews":   [{"review_id": "...", "from_dir": "pending", "to_dir": "approved"}],
    "manifests": [{"source_id": "...", "field": "status", "from": "active", "to": "archive_candidate"}]
  },
  "items": [{"review_id": "...", "type": "...", "targets": ["..."], "effects": ["graph.edges_added", "wiki"]}],
  "not_appliable": [{"review_id": "...", "type": "...", "reason": "record_only"}],
  "validators": {"passed": true, "checks": []},
  "warnings": []
}
```

Constraints locked in: `items[]` is **provenance, not the authoritative diff**; `diff.graph` uses **stable
graph ids + relationship labels** and is **edge/node-semantic, never raw SQLite row diffs**. The edge
snapshot is **keyed by `edge_id` and covers all governed statuses** (`{proposed, active, rejected,
superseded}`), so a review-driven transition that never touches the *active* set — e.g. a rejected
contradiction flipping `proposed → rejected`, or an `active → superseded` — still surfaces as an
`edges_status_changed` entry (not silently dropped). Graph **node lifecycle/status changes are semantic and
included** (deprecation/synthesis flip claim/concept status that has no manifest — distinct from the
manifest-owned source status); wiki changes are
**path-scoped unified text diffs**; review changes are **workflow movement/status** (not raw JSON noise);
manifest diffs are **field-level, limited to fields the executor is expected to mutate**. Record-only /
unsupported types produce a clear **`not_appliable`** entry with a reason — never a fabricated diff.

**5. Full-orchestration fidelity: dry-run also runs validators.**
Because dry-run answers "is it safe to apply?", it exercises the **same post-apply contract** as live apply:
executors → index rebuild → keyword reindex → validators, all on the sandbox. The response includes
`validators: {passed, checks|failures}`. **If validators fail in the sandbox, the dry-run is a failed
preview and the UI does not offer Apply by default.** Validators are read-only, so running them on the
sandbox is side-effect-free; `indexes/` regenerated for fidelity stays out of the durable diff. Dry-run
passing is **advisory, not a lock** — live apply still runs validators after mutation (apply stays the
ADR-0035 non-transactional, HTTP-200-on-`validation_failed` contract). Making validators optional is
deferred until performance is a proven problem.

**6. Failure & edge semantics.** A sandbox executor exception is a **failed dry-run with diagnostics**
(which item/type, the error), returned as a structured HTTP-200 result (consistent with apply reporting
`validation_failed` at 200), never an uncaught 500 — and the UI suppresses Apply. A no-op (nothing approved)
returns an **empty diff**, not an error.

**Graph-unavailable mirrors live (no drift).** The graph-availability gate lives **inside `run_apply`**:
when the graph is unavailable (absent / schema-mismatched) *and* approved graph-required items are waiting,
`run_apply` raises a typed `GraphUnavailable`. The endpoints translate the *same* refusal differently —
`POST /reviews/apply` → **HTTP 503** (today's behavior); `POST /reviews/apply/dry-run` → a structured
**blocked** preview (`status: "blocked"`, `reason: "graph_unavailable"`, **empty `diff`**, the blocked
graph-required items listed under `not_appliable`, **no archive preview produced** in that mixed queue). The
dry-run does **not** partial-preview the graph-free (archive) work while live apply would refuse before doing
it — that would be exactly the drift this feature exists to eliminate. (Note: because the sandbox graph is a
*copy* of live, it is unavailable iff live's is, so the gate fires identically. Relaxing live apply to
partial-apply graph-free items during a graph outage is a separate governance decision, **out of scope
here.**)

## Consequences

A human (and later an agent) can see the exact durable mutations — graph edges/nodes, wiki page diffs,
review-file moves, manifest status flips — **and** whether validators would pass, before any live write, via
a route that runs the identical orchestration on a throwaway copy. The governance-critical executors stay
byte-identical (no risky refactor of load-bearing code as a "safety" slice), and drift is impossible by
construction rather than policed by tests. Costs: a per-preview sandbox build (a **full copy** of
`db`(minus `llm_cache`)/`reviews`/`wiki`/`raw/manifests`/`scripts`/`templates`/`policies`/`normalized` plus
the manifest-referenced raw bytes — heavier, but apply is explicit and human-triggered, not a hot path), a
read-only semantic differ (graph row→semantic, wiki unified diff, review movement, manifest field-level), the
one-time extraction of `run_apply(settings)` (incl. the `GraphUnavailable` gate) from the handler, and the
isolation guarantee (no live path reachable from the sandbox; no `cwd`/global leak). Out of scope/deferred: the
CLI review tool, supersede-via-UI, a `--prune`-style cleanup, and any plan/execute refactor of the executors
(reconsider once the dry-run reveals the common mutation shapes).

## Tests (design intent; written at implementation)

- **Dry-run leaves live byte-identical** — the whole live tree (incl. `raw/**` bytes, `normalized/`,
  `templates/`, `policies/`) and `db/graph.sqlite` are unchanged after a dry-run, while a subsequent real
  apply still works.
- **Raw fidelity** — with live raw present, dry-run validators (incl. `validate_raw_integrity`) run on the
  copied catalogued raw and pass; live raw stays byte-identical. A catalogued raw file *missing* live is
  reflected (not copied) and the sandbox validator reports the **same** condition live would.
- **Sandbox includes `scripts/`** — assert the validator suite actually runs (non-empty) and the index
  rebuild is **not** silently `missing`.
- **Write-through trap** — a test executor that attempts to write `normalized/`/`templates/`/`policies/`
  (or `raw/`) leaves the live tree unchanged (the sandbox has no live path).
- **Dry-run/apply parity** for ≥1 graph+wiki executor — the reported graph semantic diff + final wiki
  unified diff match what live apply produces.
- **Graph-unavailable mixed queue** — one graph-required item + one `archive_source` with graph down:
  dry-run returns `status:"blocked"`/`graph_unavailable` (empty diff, no archive preview) and live apply
  503s — same refusal.
- **Affected durable domains surfaced** — response names `db`/`graph`, `wiki`, `reviews` (and `manifests`)
  for a graph+wiki mutation case.
- **Unsupported/record-only types** appear in `not_appliable` with a reason and never fabricate a diff.
- **Diff shape** handles the no-op, single-page, and combined graph+wiki cases.
- **Validators-fail preview** → `validators.passed == false` and the UI gates Apply.
