# LLM enrichment treats source text as untrusted data: pure-function, verified, reviewed

The moment an LLM reads chunk text, the untrusted-input contract (CLAUDE.md rule 2,
AGENTS.md) becomes load-bearing in a new way: a source document could contain text
crafted to look like instructions. Phase 3.5 defends in depth rather than trusting the
model to ignore such content.

**The model is a pure text→data function with no acting tools.** Enrichment passes never
give the model bash, code execution, file writes, or any side-effecting tool. Every pass
goes through the provider-agnostic `LLMClient.parse` contract (ADR-0025), which returns a
schema-valid object or raises — using native schema-constrained decoding where the
provider offers it and a bounded in-adapter retry otherwise. So the model can only return
schema-valid data (a summary, a tag list, candidate concepts/claims) regardless of
provider — it cannot "execute" anything, because the harness only ever does deterministic
things with that data (write a wiki page, run a validator), and output that never
validates is dropped, not surfaced. Source text is passed as clearly-delimited **data**,
with a system instruction stating that the delimited content is untrusted source material
to be analyzed, never instructions to follow.

**Mechanical grounding gates what becomes wiki content.** A claim's structured citation
(ADR-0019/0020) must resolve against the source's normalized Markdown: the cited
`(source_id, char_start, char_end)` must be in bounds and the evidence quote must occur
verbatim (whitespace-normalized) at that location. Claims whose citations don't resolve
are dropped and logged — they are treated as hallucinated or injected, not surfaced.
This makes the extraction prompt's contract explicit: assert a claim *and* supply a
real, locatable evidence quote, or the claim does not survive.

**Semantic and destructive changes stay human-reviewed.** Concept promotion, entity
merge/split, contradiction resolution, and deprecation remain gated (ADR-0018,
`policies/review.yaml`) — an LLM may *propose* these, never execute them.

Consequences: an injection attempt's worst case is bad *data* — a malformed extraction
that fails verification and is dropped, or a proposal that a human rejects — never code
execution, a raw-file mutation, or an unreviewed semantic change. The cost is that
strict verbatim grounding will drop legitimate-but-paraphrased claims the model failed
to quote correctly (surfaced as a count in the enrichment log, recoverable by prompt
tuning), and that generated prose (summaries, concept descriptions) is still
model-authored content shown to humans — labelled as generated, and subject to the lint
and review passes, but not itself citation-verifiable the way claims are. A summary on a
single-source page therefore may contain uncited factual prose: it carries **page-level
provenance** — attributed wholly to that one source and labelled generated/unverified —
rather than span-level citations. Span-level, verbatim-grounded evidence is required only
of Claims; if a fact in a summary needs to be treated as evidence, it must be promoted to
a Claim and grounded. This reconciles with the Build Spec / AGENTS rule that "every
generated factual *claim* must cite raw evidence where possible": a summary is not a
Claim node — it is non-authoritative generated prose — and its single source is cited at
page level. To keep that distinction enforceable rather than aspirational, an enriched
summary must carry a **machine-checkable generated/unverified marker** (`summary_status:
enriched` in frontmatter plus a labelled `> [!summary]` callout), and the linter enforces
the marker's presence — it does not demand span citations on summary prose, but it does
fail an enriched summary that is unlabelled or claims authority it has not earned.

**Encoding of the delimited data (ADR-0061).** "Clearly-delimited data" above is a
contract on *placement*; the *encoding* is specified by ADR-0061: untrusted text in
XML-like prompt blocks is entity-escaped (`&`, `<`, `>`) so a source cannot close its
delimiter and become instruction-adjacent, JSON payloads (the query evidence pack) use
`json.dumps`, and no untrusted value is interpolated raw. Where escaped source is quoted
back for grounding, the quote is `html.unescape()`-d once before `locate_quote` so the
verbatim gate here is unaffected and the stored quote stays source-faithful.
