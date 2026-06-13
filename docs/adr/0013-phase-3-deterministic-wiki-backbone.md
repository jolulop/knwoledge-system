# Phase 3 is a deterministic wiki backbone; LLM semantic generation is deferred

Phase 3 (Filing-Cabinet Wiki Layer) generates the wiki **Source** pages and the
machinery around them — `index.md` rebuild, `log.md` append, backlink/validator
infrastructure — entirely deterministically from the Phase 2 normalized layer and the
manifests. Like Phases 1 and 2 it runs offline, needs no API keys, and produces
byte-stable output for byte-stable input. It does **not** call an LLM and does not
generate the semantic node types (summaries beyond an extractive stub, tags, concepts,
entities, people, organizations, projects, claims, or synthesis). Those are the first
LLM-dependent work in the system and are deferred to a later sub-phase (Phase 3.5+).

The Build Specification's ingestion pipeline lists "generate summary, tags, entities,
candidate claims" alongside Source-page creation, but those steps carry properties the
deterministic phases deliberately avoid: non-determinism, cloud API keys and cost, and
— most importantly — a prompt-injection surface, because the moment an LLM reads chunk
text the untrusted-input contract (CLAUDE.md rules 2 and AGENTS.md) becomes load-bearing
in a new way. Splitting Phase 3 keeps the wiki *backbone* fully deterministic and
testable, and lets the semantic layer be designed against a proven, stable substrate
rather than co-developed with it.

Concretely, Phase 3 produces a Source page for every source whose manifest reports an
`extracted` or `partial` ingestion status. A `partial`/`needs_ocr` source whose
normalized text is too short for an extractive summary falls back to a structural
summary line; `error` and `new` sources get no page until they are (re-)extracted. The
page asserts only mechanically-derived facts and citation anchors carried from Phase 2;
sections that require an LLM are rendered as explicit pending-enrichment placeholders
(see ADR-0016) so the page shape is stable across phases.

Consequences: the wiki backbone stays reproducible, offline, and cheap to re-run and
test, and the high-risk semantic/LLM work is isolated behind a clear boundary. The cost
is that until Phase 3.5 lands, the wiki has navigable Source pages but no concept/claim/
entity graph and only extractive summaries — the "compile knowledge, don't rediscover
it" payoff is partial. Both Phase 4 (search/graph) and Phase 5 (cited answering) can
still build on the deterministic Source layer in the meantime.
