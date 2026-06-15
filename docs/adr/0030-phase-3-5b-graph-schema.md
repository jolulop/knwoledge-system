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
**derived index** rebuilt from each node's authority — concept/entity/claim/synthesis nodes
from their page frontmatter, and `source` nodes from the **manifests** (the authoritative
source listing, ADR-0008), since a source need not always have a Source page. It carries
each node's id, type, current slug, and a mirrored status purely so edge queries and
promotion counts are cheap — not a competing record. One fact, one owner: relationships in
the graph, node metadata in frontmatter (sources in manifests). Promotion (slice 5) computes over edges, *proposes* a status change that is
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
edges(                                   -- AUTHORITATIVE; one row per relationship ASSERTION
    edge_id            TEXT PRIMARY KEY,
    src_id             TEXT NOT NULL,     -- node ids, never slugs
    dst_id             TEXT NOT NULL,
    edge_type          TEXT NOT NULL,     -- Build Spec §6.2 minus needs_review (see below)
    status             TEXT NOT NULL,     -- proposed | active | rejected | superseded
    asserted_by        TEXT NOT NULL,     -- deterministic | llm | human | authored_wikilink
    confidence         REAL,
    evidence_source_id TEXT,              -- evidence anchor (resolvable via ADR-0019/0020)
    evidence_char_start INTEGER,
    evidence_char_end   INTEGER,
    review_id          TEXT,              -- the reviews/ item while status=proposed
    job_id             TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT,
    -- assertion identity: distinct spans / asserters coexist; a re-run upserts the same
    -- assertion rather than duplicating it. Edges are id-keyed, never slug-keyed.
    UNIQUE(src_id, dst_id, edge_type, asserted_by,
           evidence_source_id, evidence_char_start, evidence_char_end)
)
```

**A row is one *assertion* of a relationship, not the relationship itself.** A relationship
(e.g. source X `mentions` concept Y) is the *set* of its assertion rows; it **exists and
projects iff it has at least one `active` assertion**. This is deliberate: a source can
mention the same concept at several spans, and an LLM proposal, an authored wikilink, and a
human decision about the same relationship must coexist rather than overwrite each other.
The `UNIQUE` key is therefore the *assertion* identity (who asserted it, over which evidence
span), not the bare `(src, dst, edge_type)` triple, so re-running a pass upserts the same
assertion idempotently while genuinely distinct spans/asserters remain separate rows.
Promotion (slice 5) counts the distinct *independent sources* among a concept's `active`
`mentions` assertions, and the projector renders a backlink once any `active` assertion
exists.

**Governed vocabulary.** `node_type` is restricted to Build Spec §6.1 (`source`, `entity`,
`concept`, `claim`, `project`, `person`, `organization`, `tag`, `query`, `synthesis`) and
`edge_type` to Build Spec §6.2 **minus `needs_review`** — `mentions`, `supports`,
`contradicts`, `supersedes`, `duplicates`, `derived_from`, `related_to`. A `validate_graph`
check rejects anything outside these sets (including a literal `needs_review` edge), so the
source of truth never starts with an ungoverned vocabulary; a new type requires an ADR and a
Build Spec §6.2 update. This is one intentional, recorded deviation from §6.2:
`needs_review` is a review *state*, not a semantic relationship, so it is carried by an
assertion's `status=proposed`, never as an edge type. The Phase-3.5b producers map onto the
governed set: source→concept/entity is `mentions`, claim→source is `derived_from`, and
aliases are frontmatter (ADR-0017), not an edge. (`supersedes` the edge type — node A
supersedes node B — is distinct from `status=superseded`, which marks a stale assertion.)

**Review-gated assertions live in one table, distinguished by `status`.** There is no
separate candidate table: proposed, active, rejected, and superseded assertions are all
rows in `edges`, separated by `status` and tagged with `asserted_by` provenance. The
**projector renders only `status=active` assertions**, so a model-proposed assertion or an
authored wikilink enters as `status=proposed` (with `asserted_by` and a `review_id`) and
never appears as a backlink until a human approves it — keeping semantic relationship
changes human-reviewed (ADR-0018, CLAUDE.md rule 9). The mapping from a review item's
outcome is explicit: an assertion stays `proposed` while its review item is pending **or
deferred** (deferred never activates and never deletes); only an *approved* review flips it
to `active`, and a *rejected* one to `rejected`. So a deferred decision leaves the
assertion invisible-but-retained, exactly as a pending one does.

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
implementation; the authority split, the governed vocabulary, the per-assertion single-table
review-gated `status` model, and the active-only projector are the load-bearing commitments.
