# Phase 3.5b graph schema: edges authoritative and review-gated, node metadata in frontmatter

ADR-0029 fixed the direction — the SQLite graph is the source of truth and wiki backlinks
are a derived projection. Slice 2 of Phase 3.5b (Phase 3.5b Plan §4) makes that concrete,
and in doing so it must answer what the graph is authoritative *for*, how a relationship
that requires human review (an authored wikilink, an LLM-proposed edge) lives in a store
that also holds approved edges, and which relationship vocabulary it speaks. This ADR fixes
the schema and those rules; the producers (claims, concepts, entities) and the promotion
lifecycle build on it.

**Edges are authoritative; node metadata lives in frontmatter; the `nodes` table is a
derived index.** "Graph is source of truth" (ADR-0029) means **relationships**. Node
*metadata* already has an owner: a node's stable `id` is frozen in frontmatter (ADR-0021),
its `aliases` live there (ADR-0017), and its lifecycle `status`/`title`/`slug` are
frontmatter fields (ADR-0022). Duplicating any of those as a second authority in the graph
would let rename, status, and promotion diverge. So the graph's `nodes` table is a
**derived index** rebuilt from the pages — it carries each node's id, type, current slug,
and a mirrored status purely so edge queries and promotion counts are cheap — not a
competing record. One fact, one owner: relationships in the graph, node metadata in
frontmatter. Promotion (slice 5) computes over edges, *proposes* a status change that is
written to the page (the authority, review-gated for early promotion), then re-indexes.

**Schema (`db/graph.sqlite`, covered by backup, ADR-0014):**

```text
nodes(                                  -- DERIVED index, rebuilt from page frontmatter
    node_id     TEXT PRIMARY KEY,        -- src_/cpt_/ent_/clm_/syn_/qry_… (ADR-0021)
    node_type   TEXT NOT NULL,           -- Build Spec §6.1 vocabulary
    slug        TEXT,                    -- mirrors frontmatter; advisory, not authoritative
    status      TEXT,                    -- mirrors frontmatter lifecycle (ADR-0022)
    indexed_at  TEXT
)
edges(                                   -- AUTHORITATIVE for relationships
    edge_id            TEXT PRIMARY KEY,
    src_id             TEXT NOT NULL,
    dst_id             TEXT NOT NULL,
    edge_type          TEXT NOT NULL,    -- Build Spec §6.2 ONLY
    status             TEXT NOT NULL,    -- proposed | active | rejected | superseded
    asserted_by        TEXT NOT NULL,    -- deterministic | llm | human | authored_wikilink
    confidence         REAL,
    evidence_source_id TEXT,             -- evidence anchor for evidence-bearing edges
    evidence_char_start INTEGER,         --   (resolvable via ADR-0019/0020 grounding)
    evidence_char_end   INTEGER,
    review_id          TEXT,             -- the reviews/ item, while status=proposed
    job_id             TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT,
    UNIQUE(src_id, dst_id, edge_type)    -- upsert key; edges are id-keyed, never slug-keyed
)
```

**Governed vocabulary.** `node_type` is restricted to Build Spec §6.1 (`source`, `entity`,
`concept`, `claim`, `project`, `person`, `organization`, `tag`, `query`, `synthesis`) and
`edge_type` to §6.2 (`mentions`, `supports`, `contradicts`, `supersedes`, `duplicates`,
`derived_from`, `related_to`, `needs_review`). A `validate_graph` check rejects anything
outside these sets, so the source of truth never starts with an ungoverned vocabulary; a
new type requires an ADR and a Build Spec §6.2 update. The Phase-3.5b producers map onto
the governed set: source→concept/entity is `mentions`, claim→source is `derived_from`,
and aliases are frontmatter (ADR-0017), not an edge.

**Review-gated edges live in one table, distinguished by `status`.** There is no separate
candidate table: a proposed, an active, a rejected, and a superseded edge are all rows in
`edges`, separated by `status` and tagged with `asserted_by` provenance. The **projector
renders only `status=active` edges**, so a model-proposed edge or an authored wikilink
enters as `status=proposed` (with `asserted_by` and a `review_id`) and never appears as a
backlink until a human approves it — keeping semantic relationship changes human-reviewed
(ADR-0018, CLAUDE.md rule 9). Build Spec's `needs_review` intent is carried by
`status=proposed`, not a parallel edge type. (`supersedes` the *edge type* — node A
supersedes node B — is distinct from `status=superseded`, which marks a stale assertion
replaced by a newer one.)

**The backlink projector is a deterministic, pure function of the active graph.** For each
page it renders only links backed by an `active` edge, and omits the templates' placeholder
`[[…]]` when there is no such edge — the same "no dangling, no invented links" discipline
the Phase-3 backbone used (ADR-0016, CLAUDE.md rule 4). Backlinks are therefore synchronized
by construction (CLAUDE.md rules 6, 10): there is one writer of relationship truth, and the
projection carries no wall-clock so a re-render is byte-stable.

Consequences: rename/merge are id-level redirects that leave edges intact and re-index node
rows; promotion and backlink display are computations over the graph with a single authority
each; and review state, provenance, and evidence are first-class on every edge, so the
"authored wikilinks become reviewed edge candidates" rule (ADR-0029) is actually
enforceable. The graph holds durable human judgment (approve/reject decisions) and so is
backed up under `db/`, while the `nodes` index is regenerable from frontmatter. The costs
are a richer edges schema than a bare adjacency list, a projector and `validate_graph` to
keep pages and graph in lockstep, and the discipline of re-indexing node rows whenever
frontmatter metadata changes. The concrete column types may be tuned during slice-2
implementation; the authority split, the governed vocabulary, the single-table review-gated
`status` model, and the active-only projector are the load-bearing commitments.
