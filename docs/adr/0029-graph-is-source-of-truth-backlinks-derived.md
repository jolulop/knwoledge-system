# The SQLite graph is the source of truth; wiki backlinks are a derived projection

Phase 3.5b introduces the system's first real relationships — bidirectional backlinks
between sources and concepts/entities/claims (Build Spec §3.5), the dependency the Phase 3
backbone explicitly recorded as "no semantic backlinks yet" (ADR-0016). That forces a
decision the deterministic backbone could defer: when a `[[wikilink]]` rendered into a
Markdown page and an edge row disagree, which one is authoritative? Phase 3.5b fixes the
**SQLite graph under `db/` as the single source of truth**, with the wikilinks and
backlink sections in wiki pages as a **derived, regenerable projection** rendered from it.

**Edges key on stable typed ids, which only the graph holds canonically.** Graph edges
reference `concept_id`/`entity_id`/`claim_id`/`source_id` (ADR-0021), not slugs. A rename
or a merge is therefore an id-level redirect plus a gated relink, not graph surgery — the
property ADR-0017/0021 exist to provide. If Markdown wikilinks (which carry slugs/titles)
were authoritative, a rename would silently break edges and a merge would orphan them;
keeping the canonical edges id-keyed in SQLite makes both operations safe and makes
backlink rendering a pure re-projection.

**Backlinks rendered into pages are presentation, not data.** The entire `wiki/` layer is
already gitignored, regenerable, derived runtime data made durable by backup, not git
(ADR-0014); a Source page is a pure function of its inputs (ADR-0025/0023). Backlink
sections are part of that derived view. A small deterministic projector rebuilds them from
the graph, so bidirectional backlinks are **synchronized by construction** (CLAUDE.md
rules 6 and 10) rather than by parsing Markdown in both directions and reconciling drift.

**Authored wikilinks are edge *candidates*, not authority.** A wikilink written in prose
(by a human, or proposed by an LLM enrichment pass) is a signal: it is validated
(resolvable target, allowed relationship) and absorbed into the graph as a candidate edge,
subject to the same review gates as other low-confidence semantic changes (ADR-0018), not
treated as a fact the moment it appears in text. The graph remains the place edges are
asserted, confirmed, and counted (e.g. toward concept promotion, ADR-0018).

Consequences: rename/merge/split stay cheap and safe (id-level redirect + re-projection),
backlinks cannot drift out of sync because there is one writer of the relationship truth,
and the wiki stays a disposable view consistent with ADR-0014. The cost is that the graph
must be populated and migrated as a real datastore (schema, indices, backup — already in
scope via `db/`, ADR-0027), that backlink display requires a projection/render step rather
than living natively in the Markdown, and that wikilinks authored in prose need a
validate-and-absorb path rather than being trusted directly. This ADR fixes the authority
direction only; the concrete edge schema and projector land in Phase 3.5b/Phase 4.
