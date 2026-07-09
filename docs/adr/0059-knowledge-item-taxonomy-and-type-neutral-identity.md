# ADR-0059 — Knowledge-item taxonomy (15 types) and type-neutral semantic identity

- **Status:** implemented (2026-07-09, commit `fa9c593`; the clean-repository wipe ran the
  same day — UAT-round UI + label revisions ride the working tree)
- **Date:** 2026-07-08; **revised 2026-07-09** (review round 1 + user taxonomy v2: `evaluation_benchmark_metric`
  and `research_publication` removed, `ai_model_family` and `sub_domain` added — still 15 production
  types; complete 15-step priority order supplied by the user). **Label revision (UAT round,
  2026-07-09): `sub_domain` → `ai_topic_area`, `ai_model_family` → `model_family_architecture` —
  labels only, semantics/grouping/priority position unchanged; rides `enrich-items-v2` /
  `enrich-items-prompt-v2` (zero restale cost on the empty post-wipe vault).**
- **Precedence:** where any older ADR, CONTEXT.md entry, or Build Spec wording conflicts with this
  ADR, **this ADR is dominant** for the post-restart vault; layered supersession notes elsewhere are
  historical context, not co-equal authority.
- **Drivers:** user redesign of the semantic ontology — the Person/Organization/Project/Concept/Entity
  vocabulary classifies by grammatical/named-entity type, which does not match retrieval needs;
  finding F5 (ADR-0056 §6 rollout) proved the concept/entity array boundary loses themes to
  misrouting; the user directed a clean-repository restart, so this is a greenfield decision with
  no migration constraint
- **Related / supersedes:** supersedes ADR-0017 and the semantic-node half of ADR-0021 (typed id
  prefixes) for the new item family; retires ADR-0051's `change_entity_subtype` (replaced by a
  non-rekeying `change_item_type`); amends ADR-0055/0056 (extraction contract restated over one
  `items` array), ADR-0018 (promotion applies to items unchanged), ADR-0030 (graph `NODE_TYPES` +
  `item_type` column, `SCHEMA_VERSION` 2), ADR-0031 (synthesis/contradiction topics are items),
  ADR-0058 (amendments gain `item_type`; the subtype section becomes the item-type section).
  `src_`/`clm_`/`syn_`/`qry_` identity and the claims pipeline are untouched.

## Context

The tier-2 semantic layer classifies extracted things as `concept` or `entity`
(subtyped `entity|person|organization|project`). Three forces broke this:

1. **The taxonomy answers the wrong question.** "Person, organization, project, concept, or
   entity?" is a named-entity-recognition question. Retrieval needs "method, architecture, model,
   tool, use case, benchmark, risk, protocol, data asset, or governance issue?" — classification by
   *knowledge-object role*, not grammatical type.
2. **The concept/entity array boundary loses information (F5, proven).** The nursing-AI PDF's model
   response filed its themes ("AI adoption in nursing", "workflow redesign") as generic `entity`
   items; the ADR-0055 v3 entity-noise boundary then rightly suppressed them — themes lost
   entirely. A hard two-bucket schema turns a model-side placement error into architectural truth.
3. **`Concept` absorbs everything and `Entity` is a trash bin.** Neither supports filtering,
   browsing, or typed retrieval.

The user additionally decided: **all existing data is deleted — the restart wipes everything
including `raw/`** (verified 2026-07-08: the review ledger holds zero human decisions — 1142
pending, 0 approved, 0 rejected — so no human work is lost). No migration machinery, no legacy
shims, no rename executors for old data.

**Terminology:** the new node family is the **knowledge item** ("item" for short, `itm_` prefix).
"Item" alone collides with the established "review item"; docs and UI must use *knowledge item*
where ambiguity is possible. Review artifacts remain "review items".

## Decisions

### 1. The 15-type knowledge-item taxonomy

One-level classification by knowledge-object role. System enum values are lowercase snake_case;
TitleCase is a display/docs convention.

| `item_type` | Meaning (one line) | Examples |
|---|---|---|
| `domain` | Broad knowledge area, industry, or subject field | AI, finance, legal AI, healthcare AI, robotics, quantum computing |
| `ai_topic_area` | Field, specialty, or set of techniques/tools within a broader domain | Semantics, Agents, Models, Coding, Tools, AI Research, Hardware and Infrastructure, Data, Regulation and Ethics, Economics, World Models |
| `problem_risk` | Pain point, limitation, failure mode, threat, bottleneck, unresolved challenge | hallucination, agent failure, token cost escalation, data quality, MCP security weakness |
| `use_case` | Concrete application of technology to a business/research/operational problem | agentic RAG for enterprise search, AI code review, regulatory reporting automation, virtual try-on |
| `method_technique` | Method, algorithmic approach, design/prompting/training/analytical technique | RAG fusion, chunking, LoRA, RLHF, GraphRAG, self-reflection, test-time scaling |
| `architecture_pattern` | System structure, stack design, integration pattern, conceptual architecture | semantic layer, ontology stack, agent harness, hub-and-spoke MDM, agent memory architecture |
| `technology_capability` | Generic technical capability or technology class (not a specific product) | embeddings, OCR, vector database, knowledge graph, VLM, speech-to-text, computer use |
| `model` | **Named/branded** AI model, model family, or foundation model | GPT, Claude, Gemini, Qwen, DeepSeek, Phi, bge-m3, AlphaFold, ModernBERT |
| `model_family_architecture` | **Generic** model family, type, approach, or algorithm class | Transformers, LFMs, dLLMs, VLMs, Distills, LLMs, SLMs, Reasoning, Agentic, SSMs, HRM/TRM, LSTM, Diffusion, RFMs, MoE, RLM |
| `product_tool_platform` | Named commercial/open-source tool, library, product, SaaS, platform, repo | Ollama, LangGraph, LlamaIndex, Docling, Pinecone, Claude Code, Codex, AutoGen, Vertex AI, Bedrock |
| `data_ontology_asset` | Dataset, ontology, schema, corpus, knowledge graph, taxonomy, semantic model | FIBO, UMLS, BIRD schema, enterprise ontology, data catalog, semantic contract |
| `standard_protocol_interface` | Protocol, standard, API style, interface language, interoperability mechanism | MCP, A2A, SPARQL, GraphQL, Cypher, PDDL, OSCAL, OpenAPI |
| `infrastructure_hardware` | Physical/cloud infrastructure, chips, accelerators, runtime, storage, networking | GPU, RTX 5090, HBM, CXL, Trainium, Inferentia, Cerebras, vLLM, CUDA, Kubernetes |
| `governance_regulation` | Governance, compliance, legal, policy, security, privacy, regulatory constructs | EU AI Act, AI sandbox, data governance, model governance, privacy, auditability |
| `provider_institution` | Named company, lab, regulator, standards body, university — as substantive actor | OpenAI, Anthropic, NVIDIA, Microsoft, Palantir, Databricks, EDMC, EU, MIT, Google DeepMind |

Plus one **sentinel** (decision 5): `unclassified_review_required` — QA-only, never a production
category.

**Model vs `model_family_architecture` boundary:** `model` is for *named/branded* things (Claude,
Qwen); `model_family_architecture` is for *generic classes/approaches* (transformers, MoE,
diffusion). The priority order (decision 4) puts `model` first, so a branded family never falls
through to the generic class.

**Taxonomy v2 revision notes (2026-07-09):** `evaluation_benchmark_metric` and
`research_publication` were **removed** by the user. Benchmarks/metrics now classify by role via
the priority order (a benchmark library → `product_tool_platform`; a benchmark corpus/leaderboard →
`data_ontology_asset`; an evaluation method → `method_technique`). Named publications are **no
longer items at all** — see the noise boundary in decision 4. `McKinsey report` appeared in the
user's `sub_domain` examples but is a publication, not a field — **dropped from the examples here**
(flagged for user veto; keeping it would teach the model to route publications into `sub_domain`).

**Grouping (drives band guidance + the starvation guard, decision 6):**
- *Thematic* (9): `domain`, `ai_topic_area`, `problem_risk`, `use_case`, `method_technique`,
  `architecture_pattern`, `technology_capability`, `model_family_architecture`,
  `governance_regulation`.
- *Named* (6): `model`, `product_tool_platform`, `data_ontology_asset`,
  `standard_protocol_interface`, `infrastructure_hardware`, `provider_institution`.
- `model_family_architecture` → thematic is a **flagged default** (it is a generic class like
  `technology_capability`, not a named singular thing), not a grilled choice.

**Dropped types:** `person` (decision 8), `organization` (→ `provider_institution`), `project`
(split across `use_case`/`product_tool_platform`/`method_technique` by role), `concept` and
`entity` (deleted — the misrouting boundary itself), and from taxonomy v2
`evaluation_benchmark_metric` + `research_publication` (see revision notes above).

### 2. Type-neutral identity: one structural family, classification is metadata

The id no longer encodes classification. One structural node family:

```text
node_type   = "item"                      # structural family (graph nodes table)
item_id     = itm_<sha256(normalized_canonical_name)[:16]>
item_type   = model | use_case | ...      # 15-value taxonomy + sentinel; MUTABLE, governed
```

- **Mint key** = the creation-time normalized canonical name (ADR-0021's normalization carries
  over). The id is **frozen**: renames never rehash (unchanged principle). Same-name-different-
  referent (Claude-the-model-family vs Claude.ai-the-product when both are written "Claude") is
  resolved by `split` (ADR-0052 pattern) — **type is never the disambiguator**.
- `item_type` is a **governed page/graph attribute**, not identity. This fixes the ADR-0021
  architectural mismatch where classification was baked into identity, and moves the most common
  correction under a fuzzy 15-type taxonomy from the rekeying class to the cheap non-rekeying
  class (ADR-0041's own risk axis: danger = rewriting what an id means).
- Graph: `nodes` gains an `item_type` column (NULL for non-item rows); `NODE_TYPES` becomes
  `{source, item, claim, tag, query, synthesis}`; `EDGE_ENDPOINTS` `derived_from` src set becomes
  `{claim, synthesis, item}`; `SAME_TYPE_EDGES` keep node_type granularity (two items may be
  `duplicates`/`supersedes` partners regardless of `item_type` — merge survivor keeps its own
  `item_type`). **`SCHEMA_VERSION` 1 → 2.** Build Spec §6.1 is annotated per its §15 convention.
- Wiki: **one flat directory `wiki/Items/<slug>.md`** for all 15 types (`NODE_DIR` retires
  `Concepts/Entities/People/Organizations/Projects`); one `templates/item.md` replaces
  `concept.md`/`entity.md`. `index.md` and Source pages group items by `item_type` for browsing.
  Frontmatter: `type: item`, `item_id`, `item_type`, plus the existing lifecycle contract
  (ADR-0022 unchanged).
- `ANSWER_ELIGIBLE_TYPES` becomes `{item, claim, synthesis}` (same active-only rule).

### 3. One node per canonical referent; conflicts are governed, nothing auto-retypes

When a later extraction classifies an existing name under a different `item_type`:

1. First observed classification mints the node (priority rules are the tie-break, decision 4).
2. The later mention **routes to the existing node** (evidence, aliases, promotion accounting stay
   unified — the existing single-id probe replaces the old cross-prefix probe).
3. A **`change_item_type` review item** is filed (`subject: {node_id, to_item_type}`, mirroring
   ADR-0051's identity-change-as-subject rule so one rejected retype never locks out a different
   future retype).
4. **Nothing auto-retypes.** Approval applies a **metadata flip** — frontmatter `item_type` +
   nodes-table update + audit — no id change, no page move, no edge re-point, no tombstone.
   `change_entity_subtype` and its rekey executor are **retired for items**; ADR-0051 remains as
   history. Merge (ADR-0050) and split (ADR-0052) are **the only remaining identity surgery**, and
   both become single ops over the item family (`merge_items`, `split_item`) — the old
   "cross-type merge" deferral dissolves structurally (all items share `node_type`).

### 4. Extraction contract: one `items` array, priority rules in the prompt

Replaces the ADR-0055/0056 two-array contract. Tier-2 items output:

```json
{"items": [{"name": "Claude", "item_type": "model", "aliases": ["Claude 3.5 Sonnet"]}]}
```

- Schema: root requires `items`; each item requires exactly `name`, `item_type` (16-value enum:
  15 + sentinel), `aliases`; `additionalProperties: false`. **No** description/rationale/
  confidence/evidence fields in v1 — items are interpretive topic labels, not grounded assertions;
  provenance stays in graph mentions + Source links.
- **One full-document call** — ADR-0056's `full-doc-v1` strategy, input cap
  (`ENRICH_ITEMS_INPUT_MAX_CHARS`, renamed from `ENRICH_CONCEPT_INPUT_MAX_CHARS`),
  `coverage: truncated` marker, and strategy-ref/cache
  identity plumbing carry over unchanged.
- The **15-step priority order** (user-supplied, taxonomy v2 — every production type appears
  exactly once) lives in the system prompt as the tie-break when several types could apply:
  1. `domain` → 2. `model` → 3. `ai_topic_area` → 4. `architecture_pattern` →
  5. `model_family_architecture` → 6. `method_technique` → 7. `technology_capability` →
  8. `use_case` → 9. `problem_risk` → 10. `product_tool_platform` →
  11. `standard_protocol_interface` → 12. `data_ontology_asset` → 13. `governance_regulation` →
  14. `infrastructure_hardware` → 15. `provider_institution`.
- **Substrate carve-out (accepted, review round 2):** the user's example table files vLLM, CUDA,
  and Kubernetes under `infrastructure_hardware`, but the order above reaches
  `product_tool_platform` (10) before `infrastructure_hardware` (14), so those examples would
  classify as products. The carve-out is therefore **encoded as an exclusion clause inside the
  `product_tool_platform` rule itself** (not prose after the priority list): *software whose role
  in the document is compute/deployment/runtime substrate (inference runtimes, compute layers,
  orchestrators) is `infrastructure_hardware`, not a product* — tools you build **with** are
  products; substrate you run **on** is infrastructure. To prevent models over-indexing on the
  word "hardware", the `infrastructure_hardware` rule text must lead with
  **"infrastructure / runtime / hardware"** (the type includes software runtimes/orchestrators by
  locked decision).
- **Band guidance by group, not by array** (the F5 fix — an item cannot be lost by landing in the
  wrong array): thematic types roughly 3–10 central items; named types only when substantively
  central, usually fewer, up to ~25. Never pad, never invent, most-central first.
- **Noise boundaries retained and extended** (ADR-0055 decision 3): bibliography / byline /
  affiliation / acknowledgment-only names are never items; `provider_institution` only when the
  actor is substantively discussed; and — with `research_publication` removed in taxonomy v2 —
  **named publications/papers/reports are never items at all**: a publication enters the vault
  only by being ingested as a source (`src_` node), and the host document itself is never an item
  (a self-referential item is forbidden). This also aligns with F4 (bibliography noise).
- New prompt/schema identity: `ITEMS_PROMPT_VERSION = enrich-items-prompt-v1`, new schema version —
  a fresh cache/fingerprint lineage by construction (moot under the clean restart, but the
  versioning discipline of ADR-0027/0056 stands).
- Claims and tier-1 (summaries/tags) are **out of scope** — the claims windowed pipeline
  (ADR-0056) and the tag surface are unchanged.

### 5. `unclassified_review_required`: sentinel, quarantined candidate

The schema without an escape hatch forces the model to misclassify silently (the F5 signature).
The sentinel is allowed in extraction output, and:

- The worker creates the normal `itm_` node + `wiki/Items/` page + `mentions` edge + candidate
  review item — **evidence and review visibility preserved**.
- **Allowed states:** `candidate + unclassified_review_required` only. `active` with the sentinel
  is **forbidden** (validator invariant).
- **Excluded from recurrence auto-promotion.** A `promote_candidate_node` approval **without** an
  `item_type` amendment is blocked (`missing_required_item_type`); with a real 15-value
  `item_type` it applies the metadata flip + promotes. Approve-with-amendments (ADR-0058) gains
  `item_type` as an amendable field for exactly this path.
- Never a production taxonomy group (amended, implementation review round: the **QA-bucket
  semantics** are the locked contract). On `index.md` and Source-page groupings the sentinel's
  items render ONLY under the explicitly QA-labeled bucket **"Unclassified (review required)"**,
  ordered LAST — never under a taxonomy-type heading. This preserves the full-listing convention
  of `index.md` and the validator-enforced bidirectional Source-page mention projection (no
  sentinel special-case hole in `validate_projection`). The sentinel counts toward NEITHER the
  thematic nor the named group (decision 6) and is never answer-eligible while candidate
  (candidates never are; `active` + sentinel is validator-forbidden). Review queues, job
  summaries, and lint counts remain its primary surfaces.
- Volume is observable: job-summary counter + a report-only lint check (ADR-0037 family).
- The **human-add** path (ADR-0058) requires a real 15-value type — the sentinel is model-only.

### 6. Starvation guard redefined: `topic_starved`

`concept_starved` (ADR-0055 decision 4) keys on a node family that no longer exists. Replacement,
preserving the guard's intent ("a substantive source produced no topic layer"):

```text
topic_starved = thematic_item_count == 0
                AND (named_item_count >= 5 OR claim_count >= 1)
```

- Counts use the decision-1 grouping; the sentinel counts toward **neither** group.
- Threshold 5 stays a module constant (ADR-0055 default carried over; flagged as a default, not a
  grilled choice).
- Two layers as before: job-summary flag + report-only lint (remediation `rerun_extract_items`).
  Never gates, never flips `failing`.

### 7. Downstream semantics generalize unchanged

- **Promotion (ADR-0018):** candidate → active at ≥2 mutually-independent sources (manifest-
  provenance independence test unchanged) or by human review; single promotion writer; the
  sentinel exclusion (decision 5) is the only new rule.
- **Synthesis (ADR-0031):** one candidate synthesis per `active` item with ≥2 grounded active
  claims from ≥2 independent sources; review-only promotion — unchanged, topic family is items.
- **Contradiction blocking (ADR-0031):** candidate pairs via co-mentioned active **items** —
  unchanged mechanics.
- **Per-source review flow (ADR-0058):** the lens is unchanged; the `change_entity_subtype`
  section becomes the `change_item_type` section; amendments = title/aliases/description **+
  item_type**; retired-section predicate (`H == {S}`) and batch-decide semantics carry over.
- **Reconciliation (ADR-0057):** unchanged; the legacy-prose sweep shim becomes moot under the
  clean restart (harmless to keep).

### 8. People are provenance, never knowledge items (v1)

`person` is dropped from the taxonomy. The items prompt states people are provenance/metadata,
never items — no person is extracted regardless of centrality. This keeps ADR-0055's person-flood
fix intact.

- **Author authority stays the manifest** (`provenance.author`/`publisher`/…, the ADR-0018
  independence surface); Source pages render it; the item graph never holds people.
- A person-centric document produces claims + other typed items but no person node — an accepted,
  documented gap.
- A richer `provenance.people[] {name, role, source}` surface (author/speaker/interviewee/founder/
  quoted_person) is a **named deferral** — a source-metadata slice feeding Source pages and the
  independence gate, never item promotion/reviews.
- The `provider_institution` escape hatch for people is **rejected**.

### 9. Rollout: clean-repository restart (destructive, backup-gated)

The user directed the maximal wipe. **Sequence:** ADR-0039 backup (mandatory, verified) → wipe
`raw/` (including manifests), `normalized/`, `wiki/`, `db/` (graph, jobs, llm_cache), `indexes/`,
`reviews/` (pending + audit_log; zero human decisions verified 2026-07-08) → user re-drops a
re-curated corpus into `raw/inbox/` → normal pipeline (intake → extract → generate_wiki → items →
claims → promote → reindex_keyword → reindex_vector → rebuild_index → validate_all).

- Fresh manifests mean fresh `discovered_at` — the per-source review order resets by design.
- Full re-enrichment is **billable** (fresh cache); this is the operator's explicit choice.
- The committed eval surfaces are unaffected: `evals/corpus/` + golden files are repo assets, and
  the ADR-0038 runner builds its own scratch vault. The fake-adapter structural eval is updated by
  the implementation slice (tests change with the schema).
- Human approval for this deletion is the user's directive recorded here; execution still takes an
  explicit operator "go" at run time (destructive act, CLAUDE.md rule 9).
- **Honesty note:** shipping this slice makes the pre-restart vault fail the new validators **by
  design** (`NODE_TYPES`, id shapes, and directories all change); the wipe is the first post-ship
  operational act, and no window exists where new code must read old artifacts — therefore
  producers/artifacts are **renamed immediately** (`extract_items.py`, `<sid>.items.json`,
  `ITEMS_PROMPT_VERSION`) with **no compatibility names** (review round 1, question 3).

## Tests (implementation slice)

- Items schema/enum pin (16 values incl. sentinel; `additionalProperties: false`); prompt-contract
  pin: **all 15 production types appear exactly once in the priority order** (this pin would have
  caught the review-round-1 blocking finding), band-by-group wording, the substrate carve-out, and
  the people/bibliography/publication/host-document exclusions.
- Mint/probe: single-id probe; conflicting later classification routes mention + files
  `change_item_type` (subject `{node_id, to_item_type}`); nothing auto-retypes.
- `change_item_type` executor: metadata flip only (id, page path, edges untouched); competing
  pending retypes withdrawn; reject = no-op.
- Sentinel matrix: candidate-with-sentinel valid; active-with-sentinel fails validators; approval
  without `item_type` amendment blocked (`missing_required_item_type`); with amendment → flip +
  promote; excluded from recurrence auto-promote; human-add rejects the sentinel.
- `topic_starved` predicate matrix (thematic-zero × named/claims thresholds; sentinel counts
  toward neither).
- Graph: `SCHEMA_VERSION` 2; `item_type` column round-trip; `NODE_TYPES`/`EDGE_ENDPOINTS` gates;
  validator invariants (no active sentinel; item pages ↔ nodes-table projection).
- Wiki: flat `Items/` render, `index.md` **and Source-page** grouping by `item_type` (the
  sentinel renders only under the QA bucket "Unclassified (review required)", last — never a
  taxonomy-type heading on either surface), template swap; XSS fixtures carry over.
- Merge/split over items (`merge_items`, `split_item`): same-family by construction; survivor
  keeps its `item_type`.
- Operational-refs drift guards updated (`_APPLY_TYPES`↔docs parity, rollout chain naming).

## Deferred (named)

- **Retrieval-side `item_type` faceting** (`/search`/`/query` filters, typed browse endpoints) —
  the taxonomy's retrieval payoff, own slice, eval-gated per ADR-0038 discipline.
- **`provenance.people[]` roles slice** (decision 8).
- **Taxonomy evolution governance** — adding/renaming a type = ADR + prompt-version bump +
  enum/validator/test updates; explicitly not a config knob.
- **Sentinel-volume lint threshold tuning** (ship as counter first).
- **Cross-builder untrusted-metadata hardening** (carried from ADR-0056's out-of-scope list and
  explicitly covering the NEW items builder): `Title:` sits outside the untrusted
  `<source_document>` delimiter in every prompt builder — fixing it bumps every prompt version
  (vault-wide restale), its own slice.
- ADR-0058's named deferrals carry over (guarded sweep shortcut, rename-of-active, JSON twins).
- W2 Obsidian readability (display-text links/aliases) rides the new `Items/` pages when picked.

## Rejected alternatives

- **Type-as-identity** (15 typed prefixes / two nodes per name): fragments evidence, doubles
  review volume, recreates the cross-type duplicate/merge problem ADR-0050 deliberately scoped
  out, and turns the most common correction into ADR-0051 rekey ceremony.
- **Typed facets** (primary + secondary types): new schema/UI/validator surface with no consumer;
  alternates live in review context/audit instead.
- **Two re-labeled arrays** (thematic[]/named[]): keeps the F5 misrouting boundary alive at the
  schema level.
- **Per-type wiki directories with type-neutral ids**: path re-encodes type; retype becomes a page
  move + fan-out + link question — the accidental coupling this ADR removes.
- **Forced 15-way classification (no sentinel)**: hides model uncertainty as fact.
- **People as `provider_institution`**: re-opens the person flood; provenance ≠ content.
