# Claims cite a Source page plus a stable mechanical anchor, not a chunk ordinal

A claim's evidence references the **Source page** plus a **stable, mechanically-derived
anchor** — a page number, a section/heading path, and/or a character range into the
source's normalized Markdown (the anchor vocabulary fixed in ADR-0012 and
`policies/citation.yaml`). It does **not** store a chunk ordinal (`<source_id>::0007`)
as the authoritative citation. A `chunk_id` may be carried alongside as a
non-authoritative convenience pointer.

The reason is durability versus the volatility of chunks. Chunks are the retrieval
substrate: they are gitignored regenerable data (ADR-0014), and `chunk_id` ordinals are
deterministic only for a fixed set of chunking parameters — change the size cap and
re-extract, and `::0007` now points at different text. A claim, by contrast, can carry
durable human judgment (a confidence, a status, a resolved contradiction) that is not
cheaply regenerated. Pinning such a claim to a volatile chunk ordinal would let it
silently dangle after a re-chunk. Page numbers, section paths, and character ranges into
the normalized Markdown are stable for stable extraction and survive re-chunking, so
they are the right citation coordinates.

Chunks remain how Phases 4 and 5 *find* and *verify* evidence — retrieval ranks chunks,
and a verifier can resolve a claim's anchor back into the chunk(s) that overlap its
character range to confirm the cited text supports the claim. The split is deliberate:
chunks are the ephemeral retrieval/verification unit; the Source page plus a stable
anchor is the durable citation of record. This matches the `templates/claim.md` evidence
table (Source | Location | Evidence) and keeps citations resolvable even though the
normalized/chunk layer is regenerable.

Consequences: citations survive re-extraction and re-chunking, and the citation
validator checks anchors against the source/normalized layer rather than against a
volatile id. The cost is one level of indirection at verification time (resolve anchor →
overlapping chunk) instead of a direct id lookup, and a convention that the optional
`chunk_id` pointer is advisory and must be recomputed, never trusted as identity. This is
a deferred-phase decision; the Phase 3 backbone emits no claims yet (ADR-0016).
