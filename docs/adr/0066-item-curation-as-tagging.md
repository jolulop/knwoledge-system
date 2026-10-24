# ADR-0066 — Item curation as tagging: per-document keep/drop, tags exist through use

- **Status:** accepted — design-locked by grill-phase 2026-10-24, **not implemented**. Item
  consolidation Part B ("Slice A" of the post-UAT-r6 plan); follows ADR-0064 (Part A). **Review
  round 1 applied 2026-10-24** (see the section at the end): durable keep ledger, merge/split and
  unused-status rules, ambiguous-match path, sentinel gate, confirmed-vs-suggested metadata, the
  Authors page contract, unblock semantics, scoring units.
- **Date:** 2026-10-24
- **Drivers:** UAT round 6 (fresh `/tmp/ks-uat`, 9 sources) and the user's stated goal. The system
  replaces their Firefox-bookmarks workflow: a document plus a few tags, with tags browsable by
  type. In their words the current system is "pretty close, just that it's too verbose". The r6
  measurement (`evals/golden_item_tags.yaml`, the user's own tags):

  | Measure | r6 |
  |---|---|
  | Suggestions per document | ~26 (239 links, 230 distinct items) |
  | Approved | ~47% (100 approved, 111 rejected, 19 undecided) |
  | Golden tags among suggestions (recall) | 38% (19 of 50) |
  | Golden tags active at the end | 28% (14 of 50) |
  | `deprecate_wiki_page` tasks after maintenance | 93, nearly all on approved items |

  Three defects of the current model surfaced:
  1. **Rejection is global.** The `promote_candidate_node` review is keyed on the item alone
     (ADR-0058 decision 2: "first decision resolves globally"). The user rejected *Claude* on a
     McKinsey document only to unlink it from that document. That killed *Claude* vault-wide, and
     the human-add path then refused to add it back (terminal rejected slot). Rejection also left
     the McKinsey → Claude `mentions` edge in place.
  2. **The single-source deprecation rule contradicts curation.** Lint's `under_supported_item`
     (ADR-0036, CLAUDE.md "Items with fewer than two sources") proposes deprecating every active
     item mentioned by fewer than 2 distinct visible sources (`lint._check_graph`, which counts
     distinct `source_id`s, not provenance). In a curated vault most tags are single-document
     tags, so nearly all of the user's approvals were proposed for deprecation.
  3. **Authorship is missing.** The user browses by author and publisher (McKinsey, Dennett,
     BIRD). No stage extracts the title, author or publisher. ADR-0059 decision 8 named the
     manifest as the authority but left it empty, which is also why ADR-0018 auto-promotion never
     fires.
- **Related:** ADR-0018 (promotion lifecycle; partly superseded here), ADR-0021 (name-hashed
  identity), ADR-0036/0037 (lint), ADR-0045 (reopen), ADR-0050 (merge, alias union), ADR-0057
  (reconciliation), ADR-0058 (per-source review flow; decision 2 and the human-add rejected-slot
  block superseded here), ADR-0059 (taxonomy; decision 8 extended here), ADR-0060 (display
  aliases), ADR-0064 (`merge_key`, merge candidates), ADR-0065 (item-type management, deferred).

## Context

In the user's mental model a knowledge item **is a tag**, and tagging is a per-document act.
Removing a tag from one bookmark doesn't delete the tag, and a tag that exists doesn't attach
itself to every bookmark it could apply to. The current model works the other way. It decides an
item's existence once, globally, through a promotion review, while the per-document link (`mentions`
edge) is written by extraction and never decided by anyone.

The graph already has what a per-document model needs: `mentions` edges carry a status, and
`EDGE_STATUSES` includes `rejected`, which nothing uses today.

## Decisions

### 1. The per-document decision is keep/drop of the link

On a document's review screen each suggested tag is a **tag link** (the document → item `mentions`
edge), decided **keep** or **drop**.
- **Keep** makes the link active: the document is tagged with this tag.
- **Drop** marks the link rejected and writes a drop record (decision 3). It affects only this
  document. The tag stays available everywhere else.
- **No global effect** from either decision.
- **Untouched rows stay undecided** (unchanged ADR-0058 batch-form rule).

An existing tag that appears in a new document is **only a suggestion** there. It is linked only
when the user keeps it. Otherwise common tags would attach to too many documents and browsing
would stop being useful. This applies whatever the tag's status, including active tags with many
kept links.

**Invariant: a kept link stays until the user drops it explicitly**, even when re-extraction no
longer suggests that tag. Re-extraction updates suggestions and never touches decisions. Extraction
`mentions` assertions are replaced on every run (their identity includes evidence spans), so the
decision cannot live on them:
- **Authority: a per-document tag-decision ledger** under `reviews/` (one file per document: the
  kept item ids and the drop records of decision 3). It is file-based like the rest of the review
  ledger, so it survives graph rebuilds and is covered by backup/restore.
- **Graph mirror:** a keep also writes an `asserted_by: human` anchorless `mentions` edge (the
  ADR-0058 human-add contract). Re-extraction supersedes only `asserted_by: llm` mentions
  (`graph.supersede_mentions_for_source`), so the kept edge survives it; the ledger can rebuild it.
- **Suggestions stay `llm` edges** and are replaced on every extraction. Changed evidence spans,
  an omitted suggestion or a changed cap (decision 7) never remove a keep.
- **A changed file does not inherit decisions.** Its content hash, and so its `source_id`, is new
  (ADR-0007): it is a different document and is reviewed from scratch. A "previous version kept:
  X, Y — keep all?" hint for a new source sharing a raw path with a retired one is deferred.

### 2. A tag is active once it has one kept link

A tag becomes active as soon as one document link to it is kept, by a keep on a document screen or
by a human-add. **The keep is the promotion.** There's no separate step.
- **Suggested, never kept:** the item stays a `candidate`. It appears only as a suggestion on the
  screens of documents that mention it. It is not shown in browsing, Obsidian navigation or the
  index's promoted view.
- **No auto-promotion:** an AI suggestion never becomes active without a human keep, however many
  sources mention it.
- **The ADR-0018 independent-source threshold no longer decides whether an item exists.** It stays
  only for synthesis eligibility (≥2 independent sources), where it already applies.
- **`promote_candidate_node` is no longer filed for extraction suggestions.** The tag-link decision
  replaces it. Its remaining uses (if any) are settled at implementation; the human-add
  pre-approved path collapses into "add a kept link".
- **Classification gate preserved.** A suggestion typed `unclassified_review_required` can't be
  kept as-is: keeping it requires choosing a real production `item_type` on that row (the
  ADR-0059 approve-with-amendment rule). The `validate_graph` invariant "no active item carries the
  sentinel" stays unchanged.

### 3. Dropped, never-kept tags are deleted; a per-document drop record stays

A tag with zero kept links whose suggested links have all been dropped adds no information.
- **Deleted:** the system deletes its node, page and links.
- **Drop record kept:** a small record of **(document, tag name)** survives in the document's
  tag-decision ledger (decision 1), with the name compared by `merge_key` (case, punctuation,
  `&`/and, plural folded). Only an explicit human drop creates one (a block does not, decision 5).
  A drop record only filters new suggestions; it never removes a kept link.
- **Re-extraction:** when the same document is re-extracted (prompt/model bump or file change),
  the drop record filters that suggestion silently, so the user isn't asked again.
- **Other documents:** may suggest the tag again, to be kept or dropped there.
- **Visibility:** drop records are not tags. They are invisible to browsing, Obsidian and the
  index. A document's screen lists them as "dropped earlier", with undo.
- **CLAUDE.md rule 9 (deletion needs human approval):** the user's drops are that approval. The
  system deletes only tags with zero kept links, and only as a result of human drops.

### 4. An active tag that loses its last kept link becomes unused, not deleted

The last kept link can go two ways: the user drops it from the only document that kept it, or that
document leaves the vault. In either case the tag becomes **unused**. Active tags can carry human
work: an amended title, aliases, a description, a type change, and merge tombstones pointing at
them. Deleting them automatically would discard that work and break merge redirects.
- **Hidden:** an unused tag is not shown in browsing, Obsidian navigation or the index, like a
  candidate.
- **Listed:** it appears on an **Unused tags** list with one bulk **Delete unused** action. That
  explicit human step is the deletion approval.
- **Revived:** keeping it on any document makes it active again, with its edits intact.
- **A new node status, `unused`**, added to `NODE_STATUSES` and excluded from default
  retrieval/navigation like `candidate`.
- **Merge-redirect targets are protected.** A tag that is the `merged_into` survivor of any merge
  tombstone is excluded from **Delete unused** (listed, with the reason shown). The projection
  validator's "survivor must be active" rule (`validate_projection`, ADR-0050) becomes "active or
  unused".

Rejected alternative: auto-delete unless the tag carries human edits. That gives two behaviours for
the same user action, which is harder to predict.

**Identity operations preserve link decisions.** The merge and split executors change:
- **Merge (B into A):** B's kept links, suggestions and drop-record ownership move to A, per
  document. When one document has kept B and dropped A (or the reverse), **the keep wins**: the
  merge is the later human statement that both are the same tag. A was unused and gains a kept link
  → active.
- **Split (A → A + B):** each moved link keeps its decision (kept stays kept, a suggestion stays a
  suggestion). B is **active if any moved link was kept**, otherwise a candidate. **No
  `promote_candidate_node` review is filed** for B (replacing the ADR-0052 promotion accounting).
- **Drop records are name-keyed**, so neither operation rewrites them.

### 5. "Never suggest" is a blocked-tag list

Some tags are noise wherever they appear. A vault-wide **Never suggest** action sits on each row of
the document screen, next to keep/drop.
- **Storage:** an editable **blocked-tag list**, a plain human-editable file under `policies/`. It
  is matched by `merge_key`.
- **Filtering:** suggestions matching a blocked tag are filtered out **after** extraction. The
  prompt doesn't change, so blocking or unblocking needs no re-extraction and doesn't break the
  response cache.
- **Pending suggestions** of that tag on other documents are **withdrawn** (suggestion edge
  removed). A block creates **no drop records**: drop records mean "the user dropped this here",
  and only an explicit drop writes one. A candidate left with zero links after withdrawal is
  deleted like any never-kept tag (decision 3).
- **Kept links are never touched.** If the tag is kept elsewhere, blocking only stops new
  suggestions. Removing those links is a separate, explicit per-document drop.
- **Human-add is not blocked:** an explicit add of a blocked name is allowed, because the list only
  filters machine suggestions.
- **Unblock:** delete the line from the file, or use unblock on a small **Blocked tags** list.
  Unblocking affects future extractions. The unblock action also offers **Re-suggest**, which
  re-runs items extraction on the documents whose suggestions were withdrawn. That replays from
  the response cache, so it costs nothing, and explicit human drops still filter as usual.

This replaces the role of the global terminal rejection and the human-add rejected-slot block
(ADR-0058). The ADR-0045 reopen path is no longer needed for tag decisions: a drop is undone on the
document screen.

### 6. Authors and publishers: document metadata plus keep-to-show pages

ADR-0059 decision 8 stands: people are never knowledge items, and the manifest is the authorship
authority.
- **Enrich extracts** the document's **title, author(s) and publisher** from the text, with PDF
  metadata as a hint, and renders them on the Source page.
- **Two authority layers in the manifest:**
  - `provenance_suggested`: the Enrich output. Rewritten on every re-enrichment.
  - `provenance`: the values the user **confirmed or corrected** on the document screen (the
    existing ADR-0018 fields). **Re-enrichment never overwrites it**; a human correction always
    wins over a later suggestion. A new suggestion that differs from the confirmed value is shown
    on the screen as "Enrich now suggests …", never applied silently.
- **Shape and normalization:** `author` becomes an **ordered list** of names (document order);
  `publisher` is one name. Display names are NFKC- and whitespace-normalized; comparison uses the
  `merge_key` fold.
- **Independence uses confirmed metadata only.** Unconfirmed suggestions never establish source
  independence. Two sources are **not** independent if they share any confirmed author or the
  confirmed publisher. The same gate applies to **synthesis eligibility and contradiction
  detection**.
- **Real title:** the extracted title replaces the filename-derived Source page title. The same
  title appears on the review screens and in Obsidian (the Slice B "real titles" work builds on
  this).
- **Confirmation:** the user confirms or corrects these values on the document screen.
- **Browsing — keep-to-show:**
  - Enrich suggests every author and the publisher.
  - The ones the user **keeps** get a generated page under `wiki/Authors/<name>.md` and a node in
    the Obsidian graph. The page lists that author's or publisher's documents, with backlinks both
    ways.
  - Names not kept stay plain metadata text: searchable, but with no page or node. A paper's eight
    co-authors don't clutter the graph.
  - Once a name has a page, it is suggested on later documents and linked only when kept, as
    tags are (decision 1).
- **Pages are not items.** Author/publisher pages don't use the item taxonomy and carry no
  `item_type`. They have their own folder and graph colour group (`path:Authors`).
- **Page contract:**
  - **Navigation surface only, not SQLite graph nodes.** The pages are a deterministic projection
    of the manifests' confirmed `provenance` plus the kept-name flags. The manifest owns the
    relationship. No people enter the graph, so decision 8 of ADR-0059 stays clean.
  - **Kept-name storage:** the "show as page" flag lives with the confirmed name in the manifest.
  - **Safe filenames:** the slug comes from the existing slugifier plus the ADR-0009
    path-containment check. A raw extracted name never becomes a filesystem path. Two names that
    slug alike get a short hash suffix of the folded name.
  - **Rebuild:** the `wiki/Authors/` folder is code-owned and fully rebuilt on each wiki pass. A
    correction moves the document to the new name's page. A page nothing references any more
    disappears on the next rebuild (a derived projection, not a human-curated page).
  - **Rendering:** each page has frontmatter and a `> [!summary]` callout (CLAUDE.md rule 5) and
    lists its documents. The Source page links to it, so backlinks run both ways.
  - **Validators and index:** `validate_wiki` covers the folder (frontmatter, summary callout,
    backlinks). `wiki/index.md` gets an **Authors & publishers** section.
- **BIRD:** BIRD the standard (a topic tag) and BIRD the publisher (an author/publisher page) are
  two entries in two lists. This resolves the r6 identity collision and gives the user both
  browsing paths.
- **Side effect:** confirmed provenance makes the ADR-0018 independence check work where it still
  matters (synthesis and contradictions).
- **Enrich prompt version bump** (new output fields). Cached summary responses can't supply them.

Rejected alternative: authors as items with a "by" edge. It gives one node per name, but it
reverses decision 8 for people, and BIRD could only be browsed under one type.

### 7. At most 15 suggestions per document, broad domains first

The items prompt today asks for 3–10 thematic plus up to ~25 named items. The new contract:
- **Domains:** always **1–3 broad domain tags**, at bookmark-folder level (AI, business, health,
  philosophy, financial, software). These were the user's most-missed tags in r6.
- **Then centrality:** the **most central** tags of any other type, ranked by centrality.
- **One total cap of 15 per document**, stated in the prompt **and** enforced in code, which cuts
  the ranked list after resolution (decision 8), blocked-list filtering (decision 5) and
  drop-record filtering (decision 3).
- **The cap is a setting**, tuned against the golden list (decision 10).
- **Prompt version bump.** Items are re-extracted at the new version.

### 8. Deterministic suggestion resolution with strict aliases

- **Naming style in the prompt:** singular; the common short form or acronym ("LLM", "NLP"); no
  leading article; no qualifiers.
- **Resolution in code, after extraction:**
  - Each suggestion is compared with the titles **and aliases** of existing tags (active and
    unused), using `merge_key` (ADR-0064).
  - On a match, the suggestion becomes a link to that existing tag. "LLMs" resolves to "LLM".
  - Identity hashing (`_normalize_name`) is **unchanged**. Resolution happens before an id is
    minted, so no existing id is rekeyed.
- **Strict alias rule:** only exact `merge_key` matches. No fuzzy, edit-distance or embedding
  similarity. A wrong automatic match would put a document under the wrong tag silently.
- **Ambiguous matches ask the user.** When the folded key matches **more than one** existing tag
  (several tags can share a folded title or alias), the resolver doesn't pick one. The document
  screen shows the row with the matching tags and the user chooses one, or keeps it as a new tag.
  Two tags the user decided were distinct (a rejected `merge_items` pair, ADR-0064) always count as
  ambiguous and are never auto-resolved to each other.
- **Learning through merges:**
  - A long form that doesn't match ("Large Language Models" vs "LLM") becomes a separate
    suggestion.
  - The user merges it (ADR-0064 merge candidate or human-filed merge), and the merge's alias union
    (ADR-0050) records it as an alias of the survivor.
  - Later extractions of that wording resolve to the survivor automatically.
- **Rejected alternative:** injecting the vault's tag vocabulary into the prompt. It would catch
  more variants on the first pass, but every new tag would change the prompt, which breaks the
  response cache, re-bills extraction and grows the prompt with the vault.

### 9. Retire the single-source deprecation rule

- **Removed:** lint no longer files `deprecate_wiki_page` for an item because of its source count
  (`under_supported_item`).
- **Replacement:** the lint check becomes **Unused tags (zero kept links)**, which feeds the decision
  4 list and files no review items.
- **CLAUDE.md** lint line "Items with fewer than two sources" changes accordingly. That edit ships
  **with** the implementation, so the instructions never describe a rule the code doesn't run.
- **Synthesis:** independent-source count stays a synthesis-eligibility input only.
- **Existing open tasks:** ADR-0057 reconciliation deliberately does **not** withdraw lint's
  under-supported deprecations (it keys on stored `reason_code`, and lint deprecates active,
  mentioned nodes). The implementation adds a one-off audited withdrawal of open `deprecate_wiki_page`
  items filed by `under_supported_item`, with reason **`rule_retired`**. That covers any vault that
  ran the old lint, so the user never decides them by hand.

### 10. Clean restart

Old decisions don't map onto the new model.
- **Old global rejections** would become wrong per-document drops; the *Claude* misclick would stay
  wrong.
- **Old global approvals** would become kept links the user never confirmed per document.

As in ADR-0059 decision 9, the rollout is a **backup-gated clean restart**: back up, re-run the
pipeline from `raw/`, then keep or drop on each document screen. No conversion script is written.
- The only real data is the UAT corpus, and `evals/golden_item_tags.yaml` records the user's intent
  for it.
- The response cache replays unchanged stages. Items re-extract because of the items prompt bump
  (decision 7) and Enrich re-runs because of its prompt bump (decision 6), so the cost is about one
  paid Enrich pass plus one paid items pass, plus the user's review time.

### 11. Success measure

A deterministic scorer, `scripts/eval_item_tags.py` (no LLM calls), reads
`evals/golden_item_tags.yaml` and the vault.
- **Matching:** golden tags are matched to suggestions with the decision 8 rules (`merge_key` plus
  aliases).
- **Report per document:** suggestions, kept %, golden recall, and author/publisher hits (the
  golden `provider_institution` entries are scored against the decision 6 suggestions, not against
  items).
- **Manual run:** the user runs it after a UAT. It is not a test-suite gate, because it needs a
  real vault and real LLM output.
- **Scoring units:**
  - **Golden recall** = golden tags matched by a suggestion ÷ golden tags, per document and
    overall. A tag the golden list gives under several types (BIRD, APM) counts **once** per
    document (deduplicated by `merge_key`); the type is not scored.
  - **Expected misses** (marked in the golden file, e.g. "University") are excluded from the
    denominator and reported separately.
  - **Kept %** = kept rows ÷ **decided** rows (kept + dropped). Undecided rows are excluded and
    reported as their own count.
  - **Author/publisher hits** are scored separately from tag recall.
  - **r6 is a rough proxy only.** Its numbers come from global approvals, not per-document keeps,
    so they are not directly comparable.

Slice A targets:

| Measure | r6 | Target |
|---|---|---|
| Suggestions per document | ~26 | ≤ 15 (enforced by the cap) |
| Golden recall | 38% | **≥ 80%** |
| Kept | ~47% | **≥ 70%** |

- **Low recall:** the next step is prompt tuning, not raising the cap.
- **Kept % should rise over time:** as the vocabulary grows, more suggestions resolve to tags the
  user already keeps (decision 8). The user expects that too.
- **Known golden-list miss:** "University" as the provider of *Climbing towards NLU* names no
  extractable publisher.

## Consequences

- The review queue for a document becomes ≤15 keep/drop rows plus its author/publisher
  confirmation, with no vault-wide promotion decisions.
- **Visibility is not a Slice A outcome.** Slice A makes `candidate`/`unused` well defined and
  keeps them out of default retrieval and promoted navigation. Hiding them from Obsidian (which
  shows every Markdown file under `wiki/`) and from the index listing is Slice B. Until then the
  Obsidian status filter workaround stays.
- Code-level work (not designed here):
  - edge-level decisions in the ledger and the per-source form;
  - recompose that respects rejected edges and drop records;
  - the blocked-tag file and its filter;
  - Enrich title/author/publisher extraction and manifest write-back;
  - the Authors page renderer;
  - the resolution step;
  - the prompt-version bump;
  - lint and reconciliation changes;
  - the scorer.
- **Superseded, for extraction suggestions:**
  - ADR-0018 auto-promotion and the review-only promotion path;
  - ADR-0058 decision 2 ("first decision resolves globally");
  - the ADR-0058 human-add terminal-rejected-slot block;
  - the ADR-0052 split promotion accounting for the spin-off;
  - lint `under_supported_item`.
- **Changed executors:** merge and split preserve link decisions (decision 4); the projection
  validator accepts an `unused` merge survivor.
- **Unchanged:**
  - item identity (ADR-0021/0059);
  - the 15-type taxonomy (ADR-0065 still deferred);
  - the retype executor;
  - claims;
  - synthesis eligibility;
  - the dry-run-previewed apply (ADR-0040).

## Deferred / out of scope (Slice B and later)

- **Review screens:**
  - real titles on the review screens (built on decision 6);
  - a readable apply preview with reopen links that hides already-applied approvals;
  - an untouched-row warning plus "next document";
  - queue content column, grouping, bulk actions and local time.
- **Obsidian/index:** hiding candidate/unused/dropped tags in Obsidian and the index (stated as
  a consequence here; delivered in Slice B).
- **UAT Guide:** the §10.1 step 3 wording fix ("reject in the UI *or* with curl").
- **Secondary types for browsing** (BIRD/APM under several types): ADR-0065 question 6.
- **Richer `provenance.people[]` roles** (speaker, interviewee, …): stays the ADR-0059 named
  deferral. This ADR only covers author and publisher.

## Review round 1 (2026-10-24)

One external design review: six blocking issues, five non-blocking. The user resolved four
questions (kept links survive re-extraction; ambiguous matches need a human choice; merge-redirect
targets are protected from unused-tag deletion; independence waits for confirmed metadata) and
accepted the recommendations for the rest ("apply all").

| # | Issue | Resolution |
|---|---|---|
| B1 | Keep/drop must survive extraction-edge replacement | Decision 1: file ledger authority + `asserted_by: human` mirror edge; keeps last until dropped; a changed file (new `source_id`) does not inherit decisions |
| B2 | `unused` vs merge redirects; merge/split link handling | Decision 4: `unused` status, validator accepts an unused survivor, redirect targets protected from bulk delete; merge moves decisions (keep beats drop); split keeps decisions, no promotion review |
| B3 | Alias ambiguity | Decision 8: several matches → the user chooses; rejected merge pairs are always ambiguous |
| B4 | Keep vs the sentinel gate | Decision 2: keeping an unclassified suggestion requires a real `item_type`; invariant unchanged |
| B5 | Extracted vs confirmed metadata | Decision 6: `provenance_suggested` vs confirmed `provenance`; corrections win; author list; independence (synthesis + contradictions) on confirmed values only |
| B6 | Author page identity/schema | Decision 6: navigation projection, not graph nodes; safe slug + containment + hash suffix; code-owned rebuild; summary callout, validator, index section |
| N1 | Lint diagnosis wrong | Drivers corrected: lint counts distinct visible `source_id`s, not provenance |
| N2 | Unblock behaviour | Decision 5: a block withdraws suggestions and writes no drop records; unblock offers a cache-replayed Re-suggest |
| N3 | Visibility claimed for Slice A | Consequences reworded: Obsidian/index hiding is Slice B |
| N4 | Scoring units | Decision 11: denominators, once-per-document dedup, expected misses, undecided rows |
| N5 | Restart cost | Decision 10: Enrich prompt bump re-bills Enrich as well as items |

### Tests the implementation must include

- Kept links survive re-extraction, omitted suggestions, changed evidence spans and cap changes.
- Decisions stay isolated per document and survive backup/restore and a graph rebuild from the
  ledger.
- Block/unblock, Re-suggest, drop undo and explicit add of a blocked name behave consistently.
- Unused tags revive correctly; bulk delete skips merge-redirect targets.
- Merge and split preserve link decisions (keep beats drop on merge); a sentinel item can't
  become active by a keep.
- Ambiguous matches require a choice and respect rejected merge pairs.
- Confirmed metadata survives re-enrichment; unconfirmed metadata never establishes independence.
- Author filenames resist traversal and collisions; Authors pages, backlinks and the index rebuild
  deterministically.
- Scorer fixtures cover undecided rows, duplicate golden entries and expected misses.
