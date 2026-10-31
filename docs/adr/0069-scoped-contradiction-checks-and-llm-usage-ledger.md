# ADR-0069 — Scoped contradiction checks and the LLM usage ledger

- **Status:** accepted — design-locked 2026-10-31 (grill Q1–Q10); **not implemented**. Implementation
  is a separate step, in the slice order of decision 10.
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
