# ADR-0060 — Wiki display aliases (Obsidian readability over id-keyed pages)

- **Status:** implemented (2026-07-10; **review round 1 applied at design-lock** — flagged
  default 1 revised into the two-layer label contract of decision 2a, defaults 2–4 accepted;
  **impl review round 2 fixed 2026-07-10:** the decision-2 frontmatter contract gained its hard
  backstop (`validate_frontmatter.py` requires claim `title:`+`aliases:` and source/synthesis/
  query `aliases:`; `validate_wiki.py` requires source `aliases:`), Tags removed from the two
  ADR-0060 scans (out-of-scope surface; `validate_wikilinks` still covers its link integrity),
  and the standalone validator's label parser aligned exactly with `labels._page_label`
  (quoted OR bare scalar titles) so producer and validator agree by construction)
- **Date:** 2026-07-10
- **Drivers:** UAT finding W2 — id-keyed pages read as node ids in Obsidian: `Claims/clm_…`,
  `Synthesis/syn_…`, `Sources/src_…` filenames surface as note titles, and generated pages emit
  bare `[[Claims/clm_…]]`-style links on several surfaces. Items pages are already slug-titled
  (ADR-0059) and Source pages already alias their Claims/Items links (`_link_list`), so the gap
  is the remaining bare-link surfaces plus searchability of id-named files.
- **Related:** ADR-0016/0029 (no invented/dangling links; graph is edge SoT — this ADR changes
  display text only, never link targets or edges), ADR-0021 (typed id prefixes untouched),
  ADR-0022 (page frontmatter), ADR-0030 (page frontmatter is node-metadata authority),
  ADR-0031 (synthesis filename = `syn_id` for cross-type uniqueness — reaffirmed here),
  ADR-0037 (report-only lint family — `display_alias_rot` joins it), ADR-0059 (item pages'
  `title:`/`aliases:` frontmatter is the model this ADR extends to the other families).

## Context

Obsidian derives a note's identity display from its **filename**: tabs, file explorer, graph
view, backlinks pane, and (by default) link text all show the filename. Our durable page
identity is deliberately the node id (`src_`/`clm_`/`syn_`/`qry_` — citation/audit
infrastructure, review subjects, graph slugs, keyword nav rows), so those surfaces show hashes.
Obsidian offers two display-layer remedies that do not touch identity: **display-text
wikilinks** `[[target|label]]` (readable in reading/live-preview mode) and **frontmatter
`aliases:`** (matched by the quick switcher and search). `validate_wikilinks` already strips
`|alias` and `#heading` when resolving targets, so aliased links are validator-compatible
today; `_link_list`/`_claim_alias` already produce wikilink-safe display text on Source pages.

The bare-link surfaces as of this ADR: Claim pages (`[[Sources/<sid>]]` evidence-table cells,
`[[Claims/<cid>]]` contradicts), Synthesis pages (`[[Claims/<cid>]]` supporting evidence and
disagreements), Item pages (`[[Sources/<sid>]]` mentioned-by), Query pages (`[[Sources/<sid>]]`
citation cells), and `wiki/index.md` rows (bare primary link). `wiki/log.md` contains no
wikilinks (plain count text). Claim pages carry no `title:`/`aliases:` frontmatter;
Synthesis/Source/Query pages carry `title:` but no `aliases:`.

## Decisions

### 1. Identity-keyed filenames are permanent; readability is a display-layer projection

`wiki/Sources/<source_id>.md`, `wiki/Claims/<claim_id>.md`, `wiki/Synthesis/<syn_id>.md`, and
`wiki/Queries/<query_id>.md` keep their id filenames. Renames were considered and rejected:
Source paths are citation/audit infrastructure; claims have no short unique human name (slugs
would be lossy and collision-prone); ADR-0031 chose `syn_id` filenames precisely to avoid topic
slug collisions; renames would churn link targets, graph slugs, keyword nav rows, and review
subjects. Readability is achieved exclusively by projecting display labels through
`[[<dir>/<id>|<label>]]` link aliases and frontmatter — a change to presentation, never to
identity, targets, or edges.

**Documented limitation (accepted):** Obsidian's file explorer, tab titles, graph view, and
backlinks pane still show id filenames. Aliases fix in-page reading, search, and the quick
switcher; nothing short of renaming fixes the rest, and we do not rename.

### 2. Display labels and frontmatter per family

| Family | Display label (deterministic) | Frontmatter change |
|---|---|---|
| Source | `title_from_filename(original_filename)` — existing `title:` | gains `aliases: ["<full title>"]` |
| Claim | derived from `claim_text` via the existing `_claim_title` truncation (≤ 78 chars — inherent to deriving a title from prose, not a display cap) | gains `title:` **and** `aliases: ["<derived title>"]` — exactly one alias entry, the derived title |
| Synthesis | existing `title:` (topic title) | gains `aliases: ["<full title>"]` |
| Query | existing `title:` | gains `aliases: ["<full title>"]` |
| Item | existing `title:` + `aliases:` (ADR-0059) | unchanged |

The claim `title:` is **display-only projection**; `claim_text` remains the wording authority
(ADR-0030). `aliases:` exists because Obsidian's quick switcher matches filenames and aliases —
not `title:` — so id-named files are unsearchable by content without it.

### 2a. Two-layer label contract (review round 1 — replaces the single-cap default)

The frontmatter/search label and the rendered link alias are **different contracts** and are
never conflated:

- **Frontmatter label** (search surface): `title:` and `aliases:` carry the **full** readable
  title for Source/Synthesis/Query — no length truncation, so long source filenames and
  question-shaped query titles stay quick-switcher-matchable end to end. "Full" means
  *unbounded length, not unsanitised text*: values still pass the existing frontmatter
  sanitisation (`_fm_quote` / `_render_tag_list` — quotes, brackets, newlines), exactly as item
  pages already do. Claims use the `_claim_title`-derived title in both fields.
- **Rendered link label** (body-link surface): every alias embedded in a `[[target|label]]`
  link is produced by one shared helper `display_link_label(...)` — the existing `_claim_alias`
  behaviour (de-linked, no `[ ] |`, collapsed whitespace, ≤ 78 chars) promoted from
  claim-specific helper to the family-wide seam — keeping table cells and list rows bounded.
- **`display_alias_rot` compares against the rendered link label**, i.e.
  `display_link_label(target's current title)`, never the raw frontmatter `title:` — otherwise
  every long title would false-positive against its capped link alias.

### 3. Alias shape is a blocking validator; freshness is not

A new validator enforces, over every wikilink in a **generated page body** (whole body — Notes
sections are renderer-owned and overwritten on re-render, so there is no human-editable carve-out;
code fences and frontmatter excluded):

- Strip heading and alias exactly as `validate_wikilinks` does: `[[Claims/clm_x#Evidence|text]]`
  → target `Claims/clm_x`, alias `text`.
- If the target page **exists** and has a **resolvable display label**, the link MUST carry a
  non-empty alias. Whitespace-only aliases fail.
- If the target exists but no label resolves, a bare link is allowed.
- A missing target remains `validate_wikilinks`' dangling-link failure — not this validator's.
- The alias is **not** required to equal the target's current label (that is rot, decision 4).

Label resolution is deterministic and page-local: the target page's frontmatter `title:`;
for Claim targets, fall back to deriving from frontmatter `claim_text` (covers pages rendered
before the `title:` field existed). No LLM artifacts, no graph reads.

Scope: pages under `wiki/Sources|Claims|Items|Synthesis|Queries` plus `wiki/index.md`.
`wiki/log.md` is out of scope (append-only audit surface; carries no wikilinks today).

### 4. Alias/label drift is report-only lint: `display_alias_rot`

An ADR-0037-family `/jobs/lint` check (report-only, never flips `failing`, **not graph-gated** —
it reads pages only): a link whose alias no longer matches `display_link_label(target's current
title)` (the rendered link label, decision 2a — never the raw frontmatter `title:`).
Severity **low**; remediation = the free re-render chain (§6); no review item filed. Rationale:
alias text is cosmetic projection embedded in another page's bytes — a retitle (re-extraction
changes `claim_text`; synthesis regenerates) must never hard-fail every referrer between
producer run and re-render.

### 5. `index.md` rows carry the alias on the primary link

`rebuild_index.py` emits `[[Sources/src_x|Readable Title]]` (via its existing `page_title`
resolution) as the row's primary link; no duplicated title text elsewhere in the row. index.md
is **not** exempt from the alias validator — it is the most-read navigation surface.

### 6. Renderers stay IO-free; rollout is free

Display-label maps (e.g. `{claim_id → title}` for a synthesis's evidence links, `{source_id →
title}` for evidence-table cells) are assembled by the orchestrating worker/script and passed
into the renderers, preserving the no-IO renderer contract. Alias text enters page bytes and
therefore `input_fingerprint` — deterministic and byte-stable per render, as before.

Rollout: the vault is empty at design-lock, so shipping before the next ingest costs nothing —
all pages render readable from first generation. For a populated vault the refresh is the
documented opt-in chain: `extract_claims.py --force` (cache-replay) → `extract_items.py --force`
→ `generate_synthesis.py --force` → `generate_wiki.py --force` → `reindex_keyword.py --force` →
`rebuild_index.py` → `validate_all.py`.

## Flagged defaults — dispositioned (review round 1, 2026-07-10)

1. **Label cap — VETOED as written, revised:** the single 78-char cap conflated the
   frontmatter/search label with the rendered link alias. Replaced by the **two-layer label
   contract** (decision 2a): full sanitised titles in frontmatter, shared
   `display_link_label(...)` (≤ 78) in link position, rot compared against the rendered label.
2. **Validator packaging — ACCEPTED:** a NEW `scripts/validate_link_aliases.py` (validator count
   10 → 11), kept separate from `validate_wikilinks.py` — dangling-link integrity and
   display-readability shape are different contracts; parsing/normalisation helpers may be
   shared, the checks are never merged.
3. **Table cells — ACCEPTED:** evidence/citation table `[[Sources/…]]` cells alias like any
   generated body link, no carve-out; the rendered-link cap bounds table width. Hostile-label
   tests required for pipes, brackets, newlines, and embedded wikilinks.
4. **Query pages — ACCEPTED:** Queries gain `aliases:` and are validator-scoped (generated pages
   with existing `title:` and source citation links). **Display metadata only:** this must not
   change `answer_eligible`, citations, graph edges, or retrieval eligibility.

## Tests (pinned at design-lock)

- Bare `[[Claims/clm_x]]` fails the alias validator when the target has a resolvable label;
  `[[Claims/clm_x|Readable claim]]` passes; `[[Claims/clm_x|   ]]` fails.
- Bare link passes when the target exists but no label resolves; missing target stays a
  `validate_wikilinks` dangling-link failure (not double-reported).
- Heading links follow the same rule: `[[Sources/src_x#Quote|Readable source]]`.
- Retitling a target does not fail `validate_all`; the stale alias appears in
  `display_alias_rot` lint output with remediation pointing at the re-render chain.
- `display_alias_rot` compares against `display_link_label(current title)`: a long-titled
  target whose link alias equals the capped rendered label is NOT flagged (no
  full-title-vs-capped-alias false positive).
- Claim page: frontmatter gains `title:` + single-entry `aliases:`; `claim_text` unchanged as
  authority; derived title matches `_claim_title(claim_text)`.
- Synthesis/Source/Query pages gain `aliases: ["<full title>"]` — a > 78-char title survives
  untruncated in frontmatter while the same page's inbound link aliases are capped by
  `display_link_label`.
- Synthesis supporting-evidence and disagreement links, claim contradicts links, claim/query
  evidence-table source cells, and item mentioned-by links all render aliased when labels resolve.
- `rebuild_index.py` emits `[[Sources/src_x|Readable Source]]`, not `[[Sources/src_x]]`; no
  duplicated title text; Claim/Synthesis/Item rows alias too; existing status/summary counts
  unchanged.
- Hostile label text — pipes, brackets, newlines, embedded wikilinks — in `claim_text`,
  original filenames, synthesis/query titles: sanitised by `display_link_label` in link position
  (no link injection, table cells stay intact) AND by the frontmatter sanitisers in
  `title:`/`aliases:` (round-trips through `parse_frontmatter`), across all families.
- Byte-stable re-render: same inputs → identical page bytes including aliases (fingerprint
  discipline preserved).

## Out of scope / deferred

- File renames of any id-keyed family (rejected, decision 1) — includes Obsidian
  explorer/graph/tab readability, which is unfixable without renames.
- Extractor-side alias resolution (ADR-0058's named hazard) — unchanged.
- Any link-target or graph change: this ADR is display-text only.
