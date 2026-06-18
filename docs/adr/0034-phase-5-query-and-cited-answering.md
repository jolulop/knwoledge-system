# Phase 5: Query & Cited Answering — LLM answer synthesis grounded on retrieved chunk evidence

Phase 5 ("Query and Cited Answering", Build Spec §8, §15) is the **first LLM-in-the-loop retrieval
surface**. It adds `POST /query`: an LLM synthesizes a cited answer over evidence retrieved by the
now-complete Phase 4 stack (`GET /search` — keyword/vector RRF evidence + graph + navigation), and
every asserted factual claim must cite real raw evidence or the answer abstains. Like the Phase 3/3.5
split (ADR-0013/0028), the deterministic retrieval layer (Phase 4) lands and is tested **key-free
underneath** before this first key-requiring answer surface.

Much is already scaffolded and reused as-is: `templates/query.md` (the Query page), `policies/
citation.yaml` (`query_answer` requires citations; `"No source found in vault."`; authoritative
anchor; forbidden inventions), `app/workers/citations.py::ground_citation` (the deterministic verbatim
gate), `scripts/validate_citations.py::_check_query` (already validates `wiki/Queries/` pages),
`app/llm` (the ADR-0025 `LLMClient.parse` seam + response cache, ADR-0027), and `evals/
golden_questions.yaml` (the answer-shaped eval seed). This ADR fixes the load-bearing decisions; the
slicing lives in `docs/Phase 5 Plan.md`.

## The load-bearing decisions

**1. The answer is a set of mechanically-grounded claims; the LLM never emits or computes a citation
anchor.** `POST /query` retrieves Phase 4 evidence and builds an **evidence pack** — the retrieved
citable chunks, each with a stable in-request `evidence_id` plus its *authoritative* anchor
(`source_id`, `char_start`, `char_end`) and text/quote. The synthesis pass (ADR-0025 `LLMClient.parse`)
returns **structured data: ordered claims, each `{text, evidence_ids[]}`** — referencing the pack by
ID only, never producing offsets, page numbers, filenames, or quotes (`citation.yaml: forbidden:
invented_*`). The **harness builds the citation objects from the *retrieved* evidence** (not from
model output) and runs `ground_citation(..., require_quote=True)`: the authoritative anchor must be
in-bounds and the evidence quote must occur verbatim. A claim enters the answer body only if **≥1 of
its citations grounds**. This is the ADR-0026 contract end to end — the model is a pure text→data
function with no acting tools, and mechanical grounding gates what becomes content.

**2. `max_answer_unsourced_claims: 0` governs the *answer body*; ungrounded output is audit, not
fact.** Claims the model asserts but the harness can't ground (no resolving citation) are **excluded
from `## Answer`** and recorded in the template's **`## Unsourced Claims`** section as
rejected/diagnostic content — never presented as answer evidence. When **zero** claims ground, the
answer body **abstains** with `"No source found in vault."` (`citation.yaml`/`retrieval.yaml`
fallback). Invariant: *saved/served answers contain zero unsourced claims in the asserted body; the
Unsourced Claims section is an audit/rejection log, not answer evidence.*

**3. Citations come only from *citable chunk evidence*; graph/navigation never become answer
citations.** The synthesis evidence pack is the RRF **chunk evidence** (`/search` `evidence[]`) only.
Graph neighborhoods and navigation page hits are discovery/context surfaces (`/search` `graph[]`/
`navigation[]`), and **`candidate`/`deprecated_candidate` wiki-node prose is navigable but never
citable** (the ADR-0032 retrieval-eligibility invariant). So the answer is grounded strictly in
source chunks with real anchors; node prose never enters the citation set. (Graph-aware answer context
is a deferred enhancement.)

**4. Retrieved source text is untrusted data, not instructions.** Reusing ADR-0026: the evidence pack
is passed as **clearly-delimited untrusted source material**, with a system instruction stating the
delimited content is data to be analyzed, never commands to follow. The model returns only
schema-valid claims (no bash/file/tool surface), and any output that fails the grounding gate is
dropped — so a prompt-injection string inside a chunk cannot exfiltrate, act, or smuggle an
ungrounded claim into the answer body. Absolute filesystem paths are never placed in the pack or the
response (ADR-0009); citations expose only repository-relative source references + anchors.

**5. `/query` is the first key-requiring surface; deterministic retrieval stays key-free; answers are
cache-replayable.** Synthesis needs a model via `LLMClient` (ADR-0025) — a configurable `QUERY_MODEL`
(`provider:model_id`, default the standard tier), provider-agnostic, fake-adapter-testable. With **no
model configured, `POST /query` returns 503** (the underlying `/search` retrieval and the rest of
Phase 4 remain fully key-free). The LLM answer is **non-reproducible** (ADR-0027), so it is recorded
in the response cache keyed by `hash(question + evidence pack + model_ref + schema)`: an identical
question over identical retrieved evidence **replays** the stored answer (free, no provider call).
Retrieval itself degrades gracefully (4e): if the vector channel is unavailable the answer is
synthesized from keyword evidence; only a *missing model* 503s.

**6. `POST /query` is read-only; saving to `wiki/Queries/` is explicit, and a saved query is an
answer artifact, not graph authority.** `/query` returns the grounded answer and persists nothing by
default. Saving is an **explicit** action (a `save` flag / action, per CLAUDE.md "save useful answers
when requested") that renders `wiki/Queries/<query_id>.md` from `templates/query.md` — the grounded
citations live in the frontmatter `citations:` block (the machine-readable record), `type: query`,
`answer_eligible: false`, `derived_from: []` (reserved). A saved query enters the **navigation/derived
nodes index** (discoverable in `index.md`) but creates **no graph edges** and needs **no review gate**
(its asserted claims already passed the verbatim gate). It does *not* mint new claim/concept nodes.
`derived_from query→source` would also violate the ADR-0030 edge-endpoint contract (`query` is not an
allowed `derived_from` src), and pulling ephemeral Q&A into the curated semantic graph is undesirable.
Saved pages are derived/regenerable from the cached answer and are stale-checked by
`validate_citations.py::_check_query` (a re-extracted source can break a saved citation → flagged).
*Invariant: saved queries are discoverable + citation-auditable, but add no graph authority.*

**7. The answer-eval gate is a deterministic fake LLM adapter + structural assertions; real-model
quality is opt-in.** `tests/test_query_evals.py` loads `evals/golden_questions.yaml`, builds a small
fixture vault + Phase 4 indexes, and drives the answer pipeline with a **deterministic fake
`LLMClient`** returning structured claims that reference retrieved evidence IDs — while running the
**real** citation grounding gate. Assertions are **structural** (ADR-0028 key-free discipline,
mirroring the 4e `FakeEmbedder` evals): expected sources are cited; every answer-body claim grounds;
ungrounded claims are excluded from the body; abstention emits `"No source found in vault."`; the
Unsourced Claims section is diagnostic only; no absolute paths or system-prompt text leak; a saved
Query page round-trips the template. **No LLM-judge in CI.** Real-model answer quality is a manual /
env-gated smoke eval, replayable through the response cache.

## Consequences

Phase 5 turns the Phase 4 evidence layer into cited answers without trusting the model for
correctness: faithfulness is enforced **mechanically** (the LLM references evidence by ID; the harness
builds anchors from retrieved evidence and grounds them verbatim), so a hallucinated or injected claim
cannot enter the answer body — it abstains or is logged as unsourced. It honors the invariants:
retrieval stays key-free and only the answer surface needs a model; citations are always
source-anchored and never node prose; saved queries are leaf artifacts that add no graph authority;
and the CI gate stays deterministic and key-free via a fake adapter, with real-model quality an opt-in
concern. The standing trades: structural evals can't catch *semantic* answer-quality regressions
(accepted for the gate; covered by opt-in smoke); answers are only as good as the retrieved evidence
(no graph-derived reasoning in v1); and a saved answer can go stale when its cited source is
re-extracted (surfaced by `validate_citations`, repaired by re-answering). The evidence-ID-referenced
grounded-claim contract, citable-chunks-only citation set, untrusted evidence pack, explicit
non-graph saved Queries, model-required 503, and the fake-adapter structural eval gate are the
load-bearing commitments; the prompt wording, the exact claim/answer JSON schema, the `QUERY_MODEL`
default, and the rendered-answer prose style are tuned during implementation.
