# ADR-0069 — Scoped contradiction checks and the LLM usage ledger

- **Status:** accepted — design-locked 2026-10-31 (grill Q1–Q10). **Slice 0 implemented 2026-10-31**
  (see "Implementation — slice 0"); **slice 1 implemented 2026-10-31** (see "Implementation — slice
  1"); **slice 2(a) implemented 2026-10-31** (see "Implementation — slice 2(a)"); slices 2(b) and 3
  not implemented, in the order of decision 10.
- **Date:** 2026-10-31
- **Drivers:** part-3 UAT (2026-10-30, fresh clone at `de91d28`, 12 PDFs, ~995 active claims, 37 kept
  tags), findings #1 and #2:
  1. **Contradiction detection did not scale.** `detect_contradictions.py` (no arguments) built
     **10,500 claim pairs** and spent **one tier-3 (Opus) call per pair**. It was stopped after 3,045
     calls (~1.5–2.5M input / ~0.2–0.3M output tokens, *estimated* from rebuilt prompts — see
     driver 2). ADR-0031 decision 1 assumed graph-neighbourhood blocking "bounds the pair set
     naturally". That held when items were narrow extracted concepts. Under ADR-0066, items are the
     user's **tags** — few, broad, for browsing (*Business Strategy*, *Neuroscience*) — so two
     independent documents sharing one broad tag produce every cross-pair of their claims
     (~100 × 100).
  2. **LLM usage is invisible.** `app/llm/adapters.py` discards `response.usage`; the response cache
     stores only parsed responses; `jobs.sqlite` has no usage columns; failed retries leave no trace.
     A run's cost can only be reconstructed by hand or read from the provider console.
- **Related:** ADR-0018 (independence), ADR-0025 (LLM adapter seam), ADR-0027 (response cache),
  ADR-0031 (contradiction detection — **amended**), ADR-0063 (tier chains — **not used** by the judge),
  ADR-0066 part 3 slice 3 (awaiting reconciliation — **retired for contradictions**), ADR-0068
  (write lock, publication, Apply preview tokens), ADR-0042 (manual evals).

## Context

The user is unsure contradiction detection is useful in general. Checking a chosen set of documents
occasionally is worthwhile; a default sweep of the whole corpus is not. Any check must make its cost
a deliberate, visible choice, and must be able to run on a local model without silently falling back
to a paid provider.

## Decision

### 1. Contradiction detection is occasional and scoped (Q1)

Never part of the default pipeline, never a whole-vault sweep. It runs only when the user
deliberately starts it on a set of documents.

### 2. Scope = an explicit document list (Q2)

- At least **two distinct, valid source ids**. The selection is never expanded to other documents.
- Each **unordered document pair** is compared **once**, over cross-document claims only (never
  within one document, never against the rest of the vault).
- The **shared-kept-tag requirement is dropped** inside the selected set: the selection is the
  narrowing signal.
- Before any model call, show: the selected documents, eligible document pairs, and **skipped pairs
  with their actual reason** — `shared author <name>`, `same publisher <name>`, or
  `provenance unknown (details not confirmed)` (ADR-0018 as implemented in
  `manifests.independent_sources`: unknown inputs never prove independence, so a document needs
  confirmed Details to take part).
- The **existing independence policy is kept for now**. Whether a shared *publisher* should mean
  "not independent" is an open question (a publisher may distribute independent research) — see
  Deferred.
- A **tag shortcut** may come later, only if it resolves to a visible, fixed document list the user
  confirms.

### 3. Pairing = batched judging per document pair, provider-neutral (Q3)

- One judge call compares **claim list A against claim list B** and returns contradicting pairs.
- Long lists are split into **windows**. Exhaustive coverage needs **every window of A against every
  window of B**; comparing only corresponding windows would miss contradictions. The plan and the
  estimate state this cross-product explicitly — cost is not described as linear.
- **Validation establishes scope, not correctness.** A returned pair must name two supplied claims,
  one from each list; that proves the model stayed in scope, not that the claims contradict.
- Output stays as ADR-0031 decision 2: one sorted-pair `contradicts` assertion, born `proposed`,
  plus a `resolve_contradiction` review item. The reviewer sees both claims' text, their citations
  and the model's explanation. Human review is unchanged.
- **Cache identity** of a judge call: the selected documents, the exact window contents, the claim
  text and evidence fingerprints, the model reference, the prompt version and the generation
  settings (including the output limit). ADR-0031 decision 4's per-claim-pair fingerprint is
  superseded by this window-level identity.
- An interrupted run **resumes completed windows** from cache and reports **incomplete coverage**.
- A response cut off by the output limit is a **failed window**, never "no contradictions found".
- Embedding-similarity narrowing (grill option B) is recorded as a later **efficiency** alternative;
  it can discard real contradictions, so it is not a recall improvement.

### 4. The judge model: one dedicated setting, no chain, explicit paid authorization (Q4)

- **Precedence:** `--model` → `CONTRADICTION_MODEL` → **refuse** ("choose a judge model").
- Exactly **one** `provider:model` reference; **chains are rejected**. ADR-0063 chain resolution
  never applies to the judge, so there is nothing to fall back through.
- Every **non-`local:`** provider is labelled **"requires paid authorization"** (actual billing can
  vary) and needs **`--allow-paid` on that run**. Credentials or configuration never imply
  authorization. `--allow-paid` authorizes the **selected model only**; it never authorizes
  substitution and never lifts the budget.
- `local:` is described as **a configured endpoint**, not proof that execution is on-device or free.
- An unconfigured or unreachable adapter fails with **that model's error**; no other model is
  substituted.
- `CONTRADICTION_MODEL` has **no default** until the evaluation gate of decision 9 passes.

### 5. Estimate, then execute a saved token (Q5)

- **`detect_contradictions.py estimate <sid> <sid> …`** is read-only apart from writing its saved
  estimate. It prints: documents, eligible and skipped pairs with reasons, the window plan (the
  A×B cross-product), total windows and **expected cache hits** separately, the effective
  model/provider endpoint and its authorization requirement, approximate input and output tokens,
  the per-call output limit and the budget check — then a **token**.
- **`detect_contradictions.py run --token <id> [--allow-paid] [--max-calls N] [--max-input-tokens N]`**
  executes exactly that estimate. The token is contradiction-specific (Apply's design principles,
  its own validation scope) and binds: selected documents, claim and evidence contents, independence
  inputs, windows, model and provider endpoint, prompt version, generation settings and effective
  limits. Relevant changes refuse it; unrelated vault edits do not.
- The token is **consumed atomically when execution is accepted**. Missing paid authorization or a
  failed validation leaves it usable. An interrupted run needs a **fresh estimate**; completed
  windows remain cacheable.
- **Limits:** configured caps (`CONTRADICTION_MAX_CALLS`, `CONTRADICTION_MAX_INPUT_TOKENS`, a per-call
  output limit); a run may **lower** `--max-calls` and `--max-input-tokens`; **raising** either needs
  a new estimate. **Attempts — retries included — count against the call cap.**
- **Honest limit contract:** the **hard** caps are on **requests** and **input characters**, enforced
  deterministically. The token budget is **approximate** unless the selected model has a validated
  tokenizer or conservative bound. The explicit **per-call output limit** is part of the plan and
  shown in the estimate (input limits alone do not bound generation).
- An **oversized plan is refused before execution** (the cross-product is known up front). If runtime
  accounting forces a stop, the run reports **exactly which windows completed and which remain**;
  partial coverage is never presented as a complete comparison.

### 6. One shared per-attempt LLM usage ledger (Q6)

- A ledger (`llm_usage`, in `db/jobs.sqlite`) for **every LLM producer** — Enrich, claims, items,
  synthesis, contradictions and `/query` — recorded in the **shared client/adapter path** so no
  producer can omit it.
- **Attempt lifecycle:** each provider attempt gets an **attempt id**, is recorded **started before
  sending**, and is **finalized** afterwards. A crash or timeout leaves an observable attempt with
  unknown usage.
- **Usage is preserved before parsing or validation**: a schema-invalid response still consumed
  tokens; a transport error may have consumed tokens even when no usage arrives.
- **Nullable token fields, each independently** (input, output, cache read, cache write, …): partially
  reported usage stays partial; one `usage_reported` boolean is not enough.
- **Provider categories stay distinct** (Anthropic cache read/creation vs OpenAI-wire prompt tokens);
  totals never double-count.
- Each attempt links to **logical work**: job/invocation id, producer, operation or window id, retry
  number, model ref, outcome (ok / schema error / adapter error / output-limit), input characters.
- **Cache hits** are counted separately, per model, as **avoided calls** — not as zero-token attempts.
- `/query` gets a **durable invocation id** even when the answer is not saved.
- Summaries (CLI, `/jobs`) say **"reported tokens"** and show unknown-usage attempts alongside.
- Stores **metadata and counts only** — no prompts, responses, credentials or source excerpts. **No
  price table** (prices change; billing varies; the provider console stays the cost authority).
- A separate infrastructure slice: it does not change producer workflows and does not take the
  write lock during provider calls.

### 7. Unknown usage: reserve, then continue under separate allowances (Q7)

- **Before each attempt**, reserve its call count, exact input characters, **estimated** input tokens
  and **maximum** output tokens. If the reservations exceed the approved plan, nothing is sent.
  **Retries need new reservations.**
- **After** the attempt, keep provider-reported usage where available; otherwise keep the estimated
  and reserved amounts. Reports distinguish **reported usage**, **estimated input** and **reserved
  output**.
- Input and output are **separate allowances**: estimated input charges the approximate input-token
  budget; the full output limit is reserved against a separate output allowance.
- The pessimistic characters-per-token ratio (e.g. 2) yields an **estimate, not a bound** — labelled
  "estimated, not reported".
- **Hard guarantees remain attempt count and input characters.** Output is bounded only where the
  endpoint enforces the configured limit; an **observed output-limit violation stops further calls**
  and marks coverage incomplete.
- Missing provider usage therefore does **not** make local runs unusable.

### 8. The old whole-vault run and its duties (Q8)

- **No arguments prints usage.** `detect_contradictions.py maintain` is explicit; **neither path ever
  runs a judge sweep**.
- **Apply alone** executes human resolutions (the run-time duplicate `apply_resolved_contradictions`
  call is dropped from the job; `apply_contradiction_decisions` stays Apply's executor).
- **Evidence-based retirement:** each verdict keeps window-level provenance — document ids plus the
  claim and evidence fingerprints behind it. Changed claim text or evidence **invalidates** the old
  verdict even when claim ids survive. Leaving a selected scope, dropping a tag or changed window
  boundaries do **not**. Two documents that are no longer independent also retire their proposed
  assertions. Human decisions and audit history are preserved; **maintenance never reverses an
  approved resolution**.
- **Targeted maintenance** runs automatically, without model calls, after **Details changes** and
  **claim re-extraction**, re-checking only the assertions those documents or claims affect.
  Explicit `maintain` is the full repair path, under the existing publication / failure-reporting
  contract (ADR-0068).
- In a scoped run the **token is validated and accepted before maintenance changes anything**;
  maintenance never invalidates the run's own token or alters its approved windows; relevant
  intervening changes require a fresh estimate.
- **Contradiction reconciliation debt is retired** (`awaiting_reconciliation` for
  `detect_contradictions`, ADR-0066 part 3 slice 3) — but only once targeted maintenance and its
  failure reporting are in place (decision 10). **Synthesis reconciliation is unchanged.**
- Reporting keeps the distinction visible: **"maintenance is current" ≠ "these documents were
  checked for contradictions"**. Scoped runs keep **coverage records** (documents, document pairs,
  windows completed / remaining, model, prompt version, date).
- Contradiction judging is removed from **default pipelines**: `docs/Workflow.md`,
  `docs/Operations.md`, the UAT Guide (which gains a short "occasional contradiction check" section;
  the user's own notes are untouched), automation examples and job entry points. (No `/jobs` API
  endpoint runs contradictions today; the removal is CLI and docs.)
- Old per-claim-pair cache entries (incl. the UAT's 3,045 verdicts) are left intact under
  review-gated retention (stale-check reports TTL candidates; nothing is auto-purged).

### 9. Evaluation gate for a judge model (Q9)

- A small **hand-authored synthetic fixture** (`evals/contradiction_judge.yaml`): 2–3 synthetic
  documents of claims with fake ids, citations and quotes; labels `contradiction`, `agreement`,
  `apparent_conflict` (different dates, conditions or scopes) and `unrelated`; enough claims to
  force windowing, with some contradictions only across **non-corresponding windows**. Labels are
  **drafted, then human-reviewed** before they count.
- **Every cross-document pair in the fixture is labelled**, or predictions on unlabelled pairs are
  reported as **unscored**.
- **Absolute thresholds first:** minimum contradiction recall and maximum false-positive counts are
  agreed **before** any model is evaluated. Matching a hosted judge is not sufficient on its own.
  The **relative** criterion is additional: a local model's contradiction recall ≥ the hosted
  judge's, and its `apparent_conflict` false positives ≤ the hosted judge's.
- False positives are reported separately for `agreement`, `unrelated` and `apparent_conflict`.
- **Coverage gates correctness:** incomplete coverage, an exhausted budget, parse errors or truncated
  output prevent a passing result.
- **Repeatability:** fixed settings plus **claim-order permutations** to expose order sensitivity.
  Each result records model/version, prompt version, window plan, settings, cache status and usage.
- `scripts/eval_contradictions.py --model <ref> [--allow-paid]` uses the **production planner, judge,
  validation and accounting path**, writes **no vault state**, and is subject to paid authorization
  and budgets. Manual run; results under `evals/results/`.
- Until labels and thresholds are approved and the evaluation passes, **`CONTRADICTION_MODEL` stays
  unset**. A pass supports a documented recommendation for this task; it does **not** establish
  real-corpus accuracy.

### 10. Slices and order (Q10)

0. **Interim guard (tiny, first):** remove the old judge sweep — **no legacy flag**. Judging is
   disabled through **every entry point** (CLI, any API job, automation, docs). No arguments prints
   usage. Apply's execution of human decisions is preserved.
1. **Usage ledger** (decisions 6–7 accounting): shared client/adapter path, `llm_usage`, attempt
   lifecycle, cache-hit counts, producer summaries, `/jobs` totals, `/query` invocation id.
2. **Scoped contradiction job:**
   - **(a) deterministic:** `maintain`, evidence-based retirement, targeted maintenance after Details
     saves and claim re-extraction, pipeline/docs removal; retire contradiction reconciliation
     **only once** targeted maintenance and its failure reporting are in place.
   - **(b) judging:** window planner, `estimate` + saved estimate + token, batch-judge prompt,
     `run --token` with reservations and budgets, coverage records, UAT Guide section. Must pass
     **deterministic tests** for window coverage, token validation, reservations, retries and
     paid-provider refusal **before any live judging**. The model stays unset.
3. **Evaluation gate** (decision 9): draft fixture → user reviews labels and sets thresholds →
   `eval_contradictions.py` → run candidate models → document a recommendation, or keep the model
   unset.

## Acceptance criteria

- **Slice 0:** no entry point can start a judge sweep; no-argument CLI prints usage and makes no
  model call; Apply still executes approved `resolve_contradiction` items; default-pipeline docs no
  longer list contradiction judging.
- **Slice 1:** every provider attempt from every producer and `/query` leaves a ledger row started
  before sending and finalized after (or visibly unfinished); schema-invalid and transport-failed
  attempts are recorded; token fields are independently nullable; cache hits are counted
  separately; summaries show reported tokens and unknown-usage attempts; no prompt/response content
  is stored; no producer workflow or lock order changes.
- **Slice 2(a):** `maintain` and targeted maintenance retire exactly the assertions whose evidence
  changed or whose documents are no longer independent, never an approved resolution; failures are
  reported through the publication contract; contradiction reconciliation notices are gone only
  after that; synthesis reconciliation unchanged.
- **Slice 2(b):** deterministic tests prove: complete A×B window coverage; token binding, atomic
  consumption and refusal on relevant change; reservations before every attempt, retries
  re-reserved and counted; refusal of oversized plans; output-limit violations stop and mark
  incomplete; **missing model, chain value, missing `--allow-paid` and local connection failure each
  produce zero hosted calls**; truncated output is a failed window; coverage records list completed
  and remaining windows.
- **Slice 3:** labels human-reviewed and thresholds agreed before evaluation; the evaluator writes no
  vault state; results record model/version, prompt version, window plan, settings, cache status,
  usage and permutation results.

## Deferred

- Whether a **shared publisher** should mean "not independent" (revisit ADR-0018 explicitly).
- A **tag shortcut** for selection (must resolve to a fixed, confirmed list).
- A UI for selecting documents and running estimate → run (CLI first).
- **Embedding-similarity narrowing** as an efficiency alternative (can lose recall).
- A validated tokenizer or conservative bound per model (would make the token budget non-approximate).
- Default values for window size, call/input caps and the output limit (set in slice 2(b)).
- The fixture's thresholds (set by the user in slice 3, before any evaluation).
- A price table or cost display (explicitly out of scope).

## Consequences

- Contradiction detection stops being a background cost; every judge call is the result of an
  explicit estimate the user saw, under hard request and input-character caps.
- A local judge is possible without a silent paid fallback; paid use is an explicit per-run choice.
- Usage becomes observable for every LLM producer, which also answers "how many tokens did this job
  use?" for Enrich, claims, items, synthesis and `/query`.
- Accepted limitations: the token budget is approximate; output is bounded only where the endpoint
  enforces the limit; batched judging may miss contradictions in long windows (measured by the
  decision-9 fixture, not proven absent); the independence policy may exclude legitimate pairs
  (same publisher) until revisited.
- **Amends ADR-0031:** decision 1 (graph-neighbourhood blocking → explicit document scope + windowed
  batch judging) and decision 4 (per-claim-pair fingerprint → window-level identity) for
  contradictions; synthesis (3.5c-2) is unchanged. **Amends ADR-0066 part 3 slice 3:** reconciliation
  covers synthesis only. **ADR-0063** tier chains do not apply to the judge.

## Implementation — slice 0 (2026-10-31)

- **Worker** (`app/workers/contradictions.py`): `detect_contradictions()` (the whole-vault run),
  `candidate_pairs()` (graph-neighbourhood blocking) and `_still_as_shown()` (its under-lock
  re-check) are removed; the module no longer imports the LLM client. Kept: Apply's
  `apply_contradiction_decisions` / `apply_resolved_contradictions`, `_execute_supersede`,
  `_reproject_claim_pages` and `_claim_context` (reused by slice 2). The old per-pair prompt
  (`prompts.build_contradiction_messages`, `CONTRADICTION_SCHEMA`) and its version constants stay
  inert, still covered by the ADR-0061 encoding tests, until slice 2(b) replaces them.
- **CLI** (`scripts/detect_contradictions.py`): prints usage and runs nothing; exit 0 for
  `-h`/`--help`, **2 otherwise** (so automation still calling it fails visibly); it imports no model
  code. No `/jobs` endpoint ever ran contradictions; a route-name test guards that none is added
  (the stronger guarantee is that no judging function exists for a handler to call).
- **Reconciliation:** the `detect_contradictions` notice stays (decision 10) with honest wording —
  screen: "Proposed contradictions involving this document weren't re-checked after these details
  changed; nothing re-checks them yet (contradiction maintenance arrives with ADR-0069 slice 2(a))";
  lint remediation "none yet: …". Per-producer texts live in `reconciliation.SCREEN_NOTICES` /
  `LINT_TEXT`. Acknowledgements recorded by pre-slice-0 runs still count. Synthesis is unchanged.
- **Docs/skills:** judging removed from the default pipeline in `README.md`, `docs/Workflow.md`,
  `docs/Operations.md`, the UAT Guide (§6 run + note, §9.0.1 bullet, checklist line; the user's
  notes untouched), `REANCHOR.md`, the `vault-maintenance` and `vault-ingest` skills; ADR-0069
  pointers in the Build Spec and the Phase 3.5c plan.
- **Tests:** sweep tests removed (blocking, verdicts, cache replay, stale supersession, no-key skip,
  under-lock re-checks, prompt cache key, confidence clamp — slice 2 brings their successors);
  Apply-path tests now seed a proposed contradiction exactly as the old worker filed it and resolve
  it through `apply_contradiction_decisions`; reconciliation tests use synthesis as the acknowledging
  producer and assert the contradiction notice stays. New: the sweep is gone from worker and CLI (no
  model client built), no `/jobs` route runs contradictions, the lint/screen wording. Gate **1875
  passed + 2 skipped** (was 1890; −15 removed sweep tests), ruff clean, 15 validators; 3/3 mutants
  caught (no-arg exit 0, sweep re-added, old notice wording).

## Implementation — slice 1 (2026-10-31)

- **Tables** (`app/backend/db.py`, `db/jobs.sqlite`, additive `CREATE TABLE IF NOT EXISTS`):
  `llm_usage` (attempt id, job/invocation id, producer, operation, retry, model ref, provider,
  status `started`/`ok`/`schema_error`/`adapter_error`/`output_limit`, input characters, max output
  tokens, `usage_family`, nullable `input_tokens`/`output_tokens`/`cache_read_tokens`/
  `cache_write_tokens`, started/finished timestamps), `llm_cache_hits` (job, model, count) and
  `llm_ledger_meta` (`started_at`, written once). No content, error text or prices.
- **Attribution:** `db.insert_job(status="running")` makes that job the context's current job
  (`db.current_job()`) until `update_job` records a terminal status — so every producer's attempts
  are attributed with no producer change. It is a per-context stack (a `ContextVar`): a nested job
  is current until it ends and then its parent is again, in whatever order they end; concurrent
  threads and tasks each see their own; `db.bound_invocation(id, producer)` covers
  `/query` (`qry_…`, returned as `invocation_id`) and `/evals/run` (`eva_…`, in the report meta).
  `operation` is passed at the call sites: source id (Enrich, items), `<sid>#window<i>/<n>`
  (claims), topic id (synthesis).
- **Adapters** (`app/llm/adapters.py`): `parse_with_usage` returns `(data, usage)`; usage is read
  from the response before parsing; `AdapterError` carries `outcome` and `usage`. Anthropic
  `max_tokens` and OpenAI-wire `finish_reason=length` are `output_limit`. Families keep their
  categories (Anthropic input excludes cache tokens; OpenAI-wire input includes cached tokens).
  An adapter without `parse_with_usage` (test doubles) reports no usage. Both SDK clients are built
  with `max_retries=0`: `LLMClient` owns every retry, so each HTTP request is a recorded attempt
  (the SDKs' automatic retries would otherwise send unrecorded requests and defeat the request caps
  of decision 5).
- **Client** (`app/llm/client.py`): with a ledger (`build_client` always wires
  `UsageLedger(settings.jobs_db_path)`), each attempt is `start`ed before sending — if it cannot be
  recorded, `UsageLedgerError` is raised and nothing is sent — and `finish`ed after with its outcome
  and usage (schema-invalid responses keep their usage; an interrupt is finalized as
  `adapter_error` and re-raised). A `finish` failure never loses a response: the row stays
  `started` (unfinished, usage unknown) and a warning is logged. Cache replays call `cache_hit`.
- **Surfacing** (`app/llm/usage.py`): `summarize` / `summarize_many` (per model: attempts by
  outcome, unfinished, cache hits, reported sums and per-field unknown counts) and `report_lines`
  ("reported tokens; prices not tracked"). Usage that was **not recorded** (`None`: a database
  without the ledger tables, or a job created before `llm_ledger_meta.started_at`, when the
  database began recording) and an unreadable ledger (`LedgerUnavailable`, read-only connection)
  are told apart from a job with no model attempt: the CLI says "not recorded" or "unknown", never
  "no model attempts". The Enrich, claims, items and synthesis CLIs print it; `GET /jobs` and
  `GET /jobs/{id}` return `llm_usage` with `llm_usage_status` (`recorded`, `not_recorded`,
  `unavailable`); `docs/Operations.md` documents it.
- **Not in this slice:** reservations and budgets (decision 7) arrive with the contradiction run in
  slice 2(b); the ledger already records the input characters and output limit they need.
- **Tests:** `tests/test_llm_usage.py` (19): started-before-send, failed/retried attempts with usage,
  no-usage adapters, crash leaves `started`, interrupt finalized, unrecordable attempt not sent,
  cache hits apart, no content stored, job and invocation attribution, provider categories and
  partial reports, OpenAI `length`, Anthropic non-JSON usage, summaries, `/jobs`, `/query`
  invocation id, a real producer run; after review, the real Anthropic and OpenAI SDKs over a mocked
  HTTP transport (each HTTP request is exactly one ledger row, also with client retries off),
  not-recorded and unreadable ledgers (CLI and `/jobs`), nested jobs ending in either order,
  concurrent threads, and a fault-injected `finish` (response kept, row `started`, warning). 28
  tests; `tests/conftest.py` gives each test a fresh attribution stack (an interrupted producer
  leaves its job `running` — UAT finding #3 — and the test session shares one context). Gate
  **1903 passed + 2 skipped**, ruff clean, 16 validators; **11/11 mutants** caught (usage dropped
  on schema error, cache hit uncounted, unknown coerced to 0 — caught after adding a partial-report
  case, attribution removed, send despite ledger failure; SDK retries left on for either SDK, read
  errors reported as empty, pre-ledger jobs reported as recorded, nested end clearing the parent,
  finalize failure raised).

## Implementation — slice 2(a) (2026-10-31)

- **Check and retirement** (`app/workers/contradictions.py`): `check_assertions` (read-only) walks
  the **proposed** `contradicts` assertions only — an `active` (approved) one is never read, so
  maintenance cannot reverse it — optionally scoped to documents (`sources`) or claims
  (`claims_scope`). In order: a proposal whose review a human already approved or rejected is left
  for Apply (`awaiting_apply`); a pending review marked `retiring` is an interrupted retirement
  (`retirement_unfinished`, below); then the **judged record is validated first** — a missing or
  unreadable review, or one recording no judged evidence or not both claims' fingerprints, makes the
  proposal **unverifiable** whatever else changed; only then an endpoint claim without active
  evidence (`evidence_gone`), no independent document pair (`not_independent`, the same
  `manifests.independent_sources` rule), or a claim's current context differing from what was
  judged (`evidence_changed`). Nothing reads tags, scopes or window boundaries, so dropping a tag
  or leaving a scope retires nothing.
- **Recoverable retirement** (review round 1): a retirement is three effects — edge superseded,
  audit entry, pending review removed — and an interruption between them must stay discoverable.
  `maintain_assertions` therefore (0) marks the pending review `retiring` (`{id, reason, at}`,
  written atomically) before any effect, (1) supersedes the edge, (2) withdraws the review:
  `withdraw_review_item` now writes the audit entry **first** (atomically; for every caller), under
  `audit_key` = the marker id when given, then removes the file. An interruption anywhere leaves the
  marked review, which the check reports — also when its edge is already superseded, through a scan
  of marked pending reviews — and the next maintenance completes with the **original** reason,
  keeping an audit entry already written (same key, never a second). A re-proposal files a fresh,
  unmarked review, so it gets its own audit entry.
- **No decision on a retiring review** (review round 2): its edge may already be superseded, and
  Apply activates only proposed edges, so a decision could never take effect. The ledger
  primitives refuse it — `resolve_review_item` (approve/reject) and `defer_review_item` raise
  `ReviewRetiring` for a pending item carrying `retiring`, under the write lock — and
  `_record_decision`, which every human path uses (the JSON routes, the review form, the
  per-source batch), answers **409 `maintenance_required`** naming `detect_contradictions.py
  maintain`, with nothing recorded. A form opened before the retirement started is refused the same
  way, since the check reads the item at decision time.
- **Durable recovery records** (review round 2): `reviews.write_json_durably` writes the temp file,
  fsyncs it, renames it and fsyncs the directory (the publication queue's `_write_durably` /
  `_sync_dir`). The marker is durable (file and `pending/`) before the edge is superseded; the
  withdrawal audit is durable (file and `audit_log/`) before the review is deleted, and `pending/`
  is fsynced after the delete. A power loss can therefore not keep the supersession and lose the
  marker, nor keep the deletion and lose the audit; a failed directory sync stops before the effect
  it guards. **A retry repeats the barrier** (review round 3): the record an interrupted run renamed
  into place may be exactly what failed to flush, so on a retry `reviews.sync_durably` fsyncs the
  existing marker and `pending/` before the edge step, and an existing withdrawal audit and
  `audit_log/` before the deletion; a sync that fails again stops the retry at the same point.
- **Evidence fingerprints:** `evidence_fingerprint` = the claim wording plus each citation's
  document and quote; **character offsets are excluded** (the same quote at a shifted offset is the
  same evidence). `judged_fingerprints` reads `context.judged_evidence` (`{claim_id: fingerprint}`,
  which slice 2(b)'s scoped run will record per verdict), else derives it from the `proposal.sides`
  the removed sweep stored, so legacy proposals are maintained too. A proposal whose review item is
  missing, unreadable or records neither is **unverifiable**: reported, never retired.
- **Targeted maintenance** (`targeted_maintenance`, inside the same commit phase, never raising):
  after a Details **save** (`_source_decide`, after `confirm_details`) and **clear**
  (`_source_details_clear`), scoped to that document; after each document's claim commit in
  `extract_claims` (`_compose`, scoped to the claims it recomposed — so a claim id that survives
  with new evidence is caught). The Details result shows "N proposed contradiction(s) retired", or
  a failed row naming the error and `detect_contradictions.py maintain` (the Details stay saved);
  the claims summary carries `contradictions_retired` and `contradiction_maintenance_errors`; a
  maintenance error keeps the claims but makes the job `partial` and `extract_claims.py` exit 1.
- **Failure reporting from the evidence:** instead of a pending-work marker, the read-only check
  itself is the report. Lint adds `contradiction_maintenance_needed` and
  `contradiction_unverifiable` (graph-gated, report-only, low); the Source screen shows a
  "Contradiction maintenance" notice while a proposal of that document would be retired
  (`notice_needed`). A failed or skipped targeted run, a crash, a restore from backup or an
  out-of-band edit therefore stays visible until `maintain` runs, with nothing to keep in sync.
- **Full repair:** `scripts/detect_contradictions.py maintain` (run guard `detect_contradictions`;
  a `detect_contradictions` job with `mode: maintain`; exit 0 done, 1 failed, 2 for any other
  arguments, 3 busy). It never builds a model client, never calls Apply, and says "This does not
  check documents for contradictions". No argument still prints usage.
- **No page changes, so no publication:** pages render only `active` contradictions (ADR-0030), so
  retiring a proposal changes no Claim, Source or synthesis page and maintenance records no
  publication intent. "Failures reported through the publication contract" (acceptance criteria)
  is met by the job status and exit code plus the evidence-based reports above; the Details save's
  own publication is unchanged.
- **Contradiction reconciliation retired** (decision 10, now that targeted maintenance and its
  failure reporting exist): `reconciliation.PRODUCERS`, `SCREEN_NOTICES` and `LINT_TEXT` are
  synthesis-only; acknowledgements recorded by old `detect_contradictions` jobs are ignored.
  Synthesis reconciliation is unchanged.
- **Docs:** `docs/Operations.md` (a contradiction-maintenance paragraph), the UAT Guide (§ pipeline
  note, the Details bullet, the checklist line), the vault-maintenance skill, REANCHOR.md and
  CONTEXT.md (the awaiting-reconciliation entry).
- **Tests:** `tests/test_contradiction_maintenance.py` (22): unchanged proposals kept; not
  independent → retired with an audit note, idempotent; same claim id with a new quote retired on
  re-extraction, same evidence kept; fingerprint ignores offsets only; recorded judged evidence
  wins; a decision awaiting Apply left for Apply; an approved contradiction never reversed;
  unverifiable proposals reported, not retired; targeted scope; Details save and clear retire in
  the same save; a failed targeted run reported and still visible on the screen and in lint until
  `maintain`; a lint run; the CLI (no model client, job recorded, failure exit 1, other arguments
  exit 2); no page changes; after review round 1, interruption at each retirement step (marking,
  edge supersession, audit write, review deletion) completed on retry with the original reason and
  one audit entry, a re-proposal's own audit entry, extraction maintenance failure → `partial` and
  CLI exit 1 with the claims kept, and unverifiable proposals (missing review, no sides, one side
  judged) never retired despite lost independence or changed evidence; after review round 2,
  decisions on a retiring review refused by the primitives, by every API route (after marking and
  after supersession) and by a review form opened before the retirement started, an unmarked
  review still decidable, the persistence order (marker durable → edge → audit durable → delete →
  directory sync) and failed directory syncs stopping before the guarded effect; after review round
  3, the retry order after each failed directory sync (the existing marker, and the existing audit,
  flushed again before the effect each guards) and a sync failing twice stopping both attempts
  (51 tests).
  `tests/test_reconciliation.py` and `tests/test_contradictions.py` updated. **34/34 mutants**
  caught (independence check, evidence check, awaiting-Apply skip, active included, offsets in the
  fingerprint, scope ignored, targeted failure raised, screen notice off, claims retired count
  lost, claims scope emptied, Details clear hook, Details save hook, lint check unregistered,
  contradiction reconciliation restored; marker skipped, marked-review scan removed, marked
  proposal not recognised, random audit key, unverifiable-review check dropped, one-side judged
  accepted, audit written after delete, claims status ignoring maintenance errors, CLI exit
  ignoring them; approve/reject refusal, defer refusal, API mapping, marker not durable, directory
  sync after rename, directory sync after delete, file fsync; retry skipping the marker flush,
  retry skipping the audit flush, `sync_durably` without the file fsync, without the directory
  fsync). Gate **1954 passed + 2 skipped**, ruff clean, `validate_all` (15 validators) passing.
