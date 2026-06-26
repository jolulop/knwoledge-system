# ADR-0042 — Real-vault answer-quality eval: deterministic run/results contract

**Status:** Accepted. Design-locked **and implemented** 2026-06-26. The key-free core
`app/workers/eval_answers.py` (corpus loader + deterministic scorer + runner) backs `POST /evals/run` +
`GET /evals/results` in `app/backend/main.py` (with `_eval_query_fn` reusing the `/query` building blocks,
`save:false`); config gained `EVAL_MAX_QUESTIONS_DEFAULT`/`_HARD_CAP` + corpus/report paths;
`evals/golden_answers.example.yaml` (committed) + `.gitignore` for the local corpus; covered by
`tests/test_eval_answers.py`. This closes the deferred ADR-0036 decision-14 "answer-quality eval"
(`/evals/run`) gap: a key-required,
loopback-only, **vault-SoT-read-only** operator workflow (it may write its own eval artifacts + the LLM
cache, but nothing under `raw/`/`wiki/`/`normalized/`/`reviews/`/`raw/manifests/`/graph — decision 4) that
measures the cited-answer pipeline against a curated
real-vault golden Q&A corpus and **scores deterministically** (no LLM judge). It is distinct from the
ADR-0038 retrieval-relevance tuning *script* and the fake-adapter `evals/golden_questions.yaml` CI fixture.
**Extends:** ADR-0034 (`POST /query` cited answering — the surface under test), ADR-0036 (Phase 7;
decision 14 named `/evals/run`), ADR-0038 (retrieval eval; durable reproducibility-stamped report
pattern under `evals/reports/`), ADR-0027 (LLM `ResponseCache`), ADR-0033/0009 (cloud-key gate +
loopback-only no-auth posture), ADR-0025 (LLM adapter / `build_client`). Read `app/backend/main.py`
(`run_query_endpoint`, `_query_client`, `assert_safe_bind`), `app/backend/models.py` (`QueryResponse`),
`evals/golden_questions.yaml`, `scripts/eval_retrieval.py`.

## Context

The cited-answer pipeline (`POST /query`, ADR-0034) mechanically grounds every body claim and exposes
machine-checkable signals (`abstained`, grounded `claims`, `citations` with `source_id`s,
`unsourced_count`, `security_rejected_count`). There is no automated answer-quality eval — only a
fake-adapter structural fixture (`golden_questions.yaml`) and the separate retrieval-relevance script.
Operators need a **repeatable regression signal** for real answers over the real vault, without weakening
the key-free CI gate, leaking private source text, or incurring runaway LLM cost. This ADR locks that
contract narrowly.

## Decisions

**1. Deterministic, key-free scoring — no LLM judge (v1).**
The LLM key is used **only to generate** answers via the existing `/query` pipeline; scoring is mechanical
against per-question predicates in the corpus (no free-form ideal answers). Per question, score:
**expected sources cited**, **forbidden sources not cited**, **abstain expected-vs-actual**,
`unsourced_count == 0`, `security_rejected_count == 0`, **answer produced when expected**, and **citation
recall/precision** over `source_id`s. Aggregate = per-predicate pass rates + an overall. Because answer
*generation* is nondeterministic, the report is a **reproducibility-stamped snapshot** (a measurement
tool), **not** a CI pass/fail gate — it never runs in key-free CI. An **LLM-as-judge is deferred** to a
later opt-in "analysis lane" that may *annotate* results but must never become the gate.

**2. HTTP endpoints over a fake-client-testable core.**
A reusable core `app/workers/eval_answers.py` owns: load corpus → run the `/query` pipeline (injected LLM
client) → compute deterministic scores → write durable result files. It takes an **injected client**, so
CI unit-tests the core + scorer with a **fake client on a tiny fake corpus** (key-free, like the `/query`
evals). Thin wrappers: **`POST /evals/run`** (key-gated, see decision 3) and **`GET /evals/results`**
(key-free; lists/reads stored results). No standalone script in v1 (the ADR-0038 script stays the
retrieval-tuning tool; this is the operator product surface ADR-0036 named). `GET /evals/results` is
key-free but exposes `source_id`s / categories / run metadata; it **inherits the loopback-only, no-auth
posture** (ADR-0009) and is **not safe on a non-loopback bind without Phase-8 auth** — like every other
browser/JSON route here.

**3. `POST /evals/run` cost/safety gate.**
- **Explicit opt-in:** require `confirm_cost: true` in the body — an empty/accidental POST is a `400`,
  never an LLM call.
- **Bounded budget:** a config default `EVAL_MAX_QUESTIONS_DEFAULT` governs a normal run; an optional
  per-request `limit` is **clamped to a hard ceiling** `EVAL_MAX_QUESTIONS_HARD_CAP` (the request can never
  raise cost past the ceiling).
- **Key gate:** an unconfigured/failed LLM → controlled **`503`** (no detail leak), the same posture as
  `/query`. Configuring a provider key *is* the cloud opt-in (no separate silent cloud path).
- **Loopback-only:** inherited from `assert_safe_bind` (ADR-0009).
- **`dry_run: true` pre-flight:** validate the corpus (well-formed; **both `expected_source_ids` and
  `forbidden_source_ids` resolvable** against the graph/manifests) and report how many questions *would*
  run — **no LLM calls** (key-free), so curation can be checked cheaply.
- **Recorded metadata:** run id, timestamp, scoring version, model provider/ref/config identity,
  questions requested/run/skipped, graph schema version, `cache_mode`, and a **vault root fingerprint /
  system label — never an absolute path** (`/home/...` must not leak into a durable artifact).
- **Controlled error posture (like `/query`):** any per-case LLM `ParseError`/`AdapterError`/`ConfigError`
  or search `HTTPException` **aborts the whole run with a controlled `503`** (no raw detail) and writes
  **no report** — a partial answer-quality snapshot is easy to misread, and all stored reports therefore
  represent complete runs over the selected valid cases.
- **Collision-safe `run_id`:** reports are durable snapshots, so a same-second second run gets a distinct
  `-N`-suffixed `run_id`/files (never an overwrite). A negative `limit` is a `400`.
- **Mode is curation-validated:** a case `mode` outside the evidence-producing set
  (`auto`/`keyword`/`vector`) is a **curation skip** (reported), never a silent fallback to `auto`.
- **Sanitized curation errors:** a non-canonical (untrusted, possibly path-like) source id is reported by
  **field + index only** — never echoed verbatim — so it can't leak into the durable artifact; a canonical
  but absent id (a content hash, not a path) is safe to name.

**4. Read-only boundary + caching: `ResponseCache` by default; `fresh` bypasses.**
**"Read-only" means read-only with respect to the vault source-of-truth + semantic state** — the eval
writes **nothing** under `raw/`, `normalized/`, `wiki/`, `reviews/`, `raw/manifests/`, or the graph, and
always calls `/query` with **`save: false`** (no `wiki/Queries/` page). It **may** write its own eval
artifacts (`evals/reports/answers/`) and **may populate the normal LLM `ResponseCache`
(`db/llm_cache.sqlite`) on a miss** — the cache is an execution artifact, not vault SoT, and using it
matches the real `/query` operator path. (The earlier "never mutates the cache" wording was inconsistent
and is corrected here.)
By default the run uses the cache (affordable, reproducible): same `model_ref` + unchanged vault/prompt →
hit (free); any retrieval/prompt/grounding change, `model_ref` bump, or vault change → natural **miss** →
fresh answer (and a cache write), so the eval still catches the regressions it exists for. An opt-in
`fresh: true` / `use_cache: false` **bypasses both cache lookup and write** — a clean provider-drift run
under an identical `model_ref`, with no cache side effect (still gated by `confirm_cost` + the hard cap).
Results record `cache_mode` + `cache_hits`/`cache_misses` (no "cache unchanged" promise) — the cached
path wraps the `ResponseCache` in a counting subclass so a replay (`get` returns a row) is a **hit** and a
`get` miss (the client then generates + `put`s) is a **miss**, recorded per run.

**5. Durable result artifact — scores + ids + flags + metadata only (privacy).**
`evals/reports/` is durable (may be committed/backed-up/shared), and evidence packs + answer prose can
contain private source text, so the artifact stores **NO prompt/evidence pack, NO answer prose, NO raw
source text**. It stores per question: id/category, expected & forbidden `source_id`s, **cited
`source_id`s** (content-hash ids, not text), `abstained`, `unsourced_count`, `security_rejected_count`, the
deterministic metric values + per-predicate pass/fail reasons; plus the run metadata of decision 3.
Canonical **JSON** (consumed by `GET /evals/results`) + a rendered **Markdown** human summary. A later,
optional, **local-only debug mode** may write answer prose to a gitignored path — never the durable
reference report.

**6. Corpus + outputs: gitignored local data, committed schema.**
Real-vault questions + `source_id`s can reveal private vault topics/inventory, and the vault itself is
gitignored, so:
- **Committed:** `evals/golden_answers.example.yaml` (documents the schema with fake ids) + a tiny fake
  fixture for the core tests. Schema per question: `id`, `category`, `question`, `mode`,
  `expected_source_ids`, optional `forbidden_source_ids`, `should_abstain`, `expect_answer`.
- **Gitignored local:** the real corpus `evals/golden_answers.local.yaml`. A missing corpus → a **clear
  error** from the endpoint naming the example path to copy from.
- **Gitignored outputs:** real-vault run results under a dedicated `evals/reports/answers/` subtree (kept
  out of git by default), distinct from the **committed** ADR-0038 retrieval baselines that live at the
  `evals/reports/` top level. A single sanitized/labeled reference report may be committed later if wanted.

**Out of scope (v1):** LLM-as-judge / prose grading, scheduled/cron eval runs, Phase-8 auth/CSRF or a
non-loopback bind, automated baseline-diff/regression-gating (v1 emits stamped snapshots; cross-run
comparison is the operator's job or a later slice), and any vault mutation.

## Consequences

Operators get a repeatable, cheap-to-re-run, privacy-safe measurement of real answer quality over the live
vault, reusing the exact `/query` operator path and the established stamped-report idiom — with cost
bounded by an explicit opt-in + hard cap, the key-free CI gate untouched (the core is fake-client tested),
and no private source text in durable artifacts. Costs: new config (`EVAL_MAX_QUESTIONS_DEFAULT`/
`_HARD_CAP`), two endpoints + a worker core, a corpus schema + example + `.gitignore` entries, and the
deterministic scorer. The deliberate v1 limitation is that deterministic scoring measures
citation/abstention/grounding behavior, **not** subjective prose correctness — that is the explicitly
deferred judge lane.

## Tests (design intent; written at implementation)

- **Core is key-free testable:** the scorer + runner over a tiny fake corpus with a fake LLM client
  produces deterministic per-predicate results (expected-cited, forbidden-not-cited, abstain match,
  recall/precision, zero unsourced/security).
- **`POST /evals/run` gate:** missing `confirm_cost` → 400; unconfigured LLM → 503 (no leak); `limit`
  above the ceiling is clamped; `dry_run:true` validates the corpus and runs **no** LLM call.
- **Privacy:** a result artifact contains no prompt/evidence/answer text and **no absolute path** — only
  ids, flags, scores, metadata (root fingerprint, not `/home/...`).
- **`save:false`:** an eval run leaves `wiki/Queries/` unchanged.
- **Caching:** a default (cache-enabled) run records `cache_misses` and **may populate** the cache; a
  second identical run records `cache_hits`. `fresh:true` **bypasses lookup and write** (no cache side
  effect). `cache_mode` is recorded.
- **Corpus:** a missing local corpus → a clear error naming the example path; a question whose
  `expected_source_id` **or `forbidden_source_id`** is unresolvable is reported as a skipped curation
  error (not a silent pass).
- **`GET /evals/results`:** lists stored runs + reads one run's scores; key-free; no source text.
- **Read-only over vault SoT:** a run mutates nothing under `raw/`, `wiki/`, `reviews/`, `raw/manifests/`,
  `normalized/`, or the graph DB (cache + `evals/reports/answers/` writes are allowed).
