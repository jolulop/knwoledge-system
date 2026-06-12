# Chunk schema and mechanical citation anchors

Phase 2 splits each normalized document into chunks using a heading-aware strategy: it
breaks on Markdown heading boundaries, caps chunk size, and never splits mid-sentence.
Each chunk is one JSON object on a line of `normalized/chunks/<source_id>.jsonl` with,
at minimum: `chunk_id` (`<source_id>::<ordinal>`), `source_id`, `ordinal`,
`heading_path` (the section it belongs to), `text`, and `char_start`/`char_end`
offsets into the normalized Markdown. Paginated formats add `page` (or a page range);
table-derived chunks add `table_reference`/`sheet_reference`. These fields populate the
anchor vocabulary already fixed in `policies/citation.yaml`.

Every anchor must be mechanically derived, never estimated. Section path and character
offsets come directly from the normalized text and are always present. Page numbers are
produced only by tracking per-page text spans during extraction (e.g. pypdf page by
page) and mapping a chunk's character range back to the originating page(s); when a
format has no pages (DOCX, HTML, Markdown) `page` is null and the section/character
anchor stands in. Heuristic or proportional page estimates are explicitly forbidden,
matching the `forbidden: invented_page_numbers/invented_line_numbers` rules in the
citation policy and CLAUDE.md.

Consequences: any later cited answer can point at a chunk anchor that resolves to a
real, verifiable location in the source, which is the foundation of Phase 5 cited
answering. The cost is extra bookkeeping in the extractor — page-span tracking and
offset accounting must be carried through normalization — and chunks for non-paginated
formats will cite section/offset rather than a page. This anchor contract is hard to
change after chunks and citations exist downstream, so it is fixed here.
