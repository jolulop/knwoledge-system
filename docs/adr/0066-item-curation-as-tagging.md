# ADR-0066 — Item curation as tagging: per-document keep/drop, tags exist through use

- **Status:** accepted — design-locked by grill-phase 2026-10-24. **Part 1 implemented
  2026-10-25** (decisions 1–5 plus the decision 4 merge/split rules; see "Implementation — part 1"
  at the end), hardened by **review rounds 2–7** (2026-10-25/27); round 2 also brought
  **decision 9** forward. **Part 2 (decisions 7–8) design-locked by grill-phase 2026-10-27 and
  implemented 2026-10-27** (see "Part 2 — implementation decisions" and "Implementation — part 2"),
  hardened by **review round 8** (user authority over aliases and lifecycle; unfinished "use X")
  and the **UAT round 8 fixes** (2026-10-28: blocked tags out of merge proposals, alias edit of a
  kept tag, shared-name notes, "applied" in the queue, plain merge errors), reviewed in **review
  round 9** (stale alias forms, "already in effect", status-specific merge advice) and **review
  round 10** (Apply serialized with tagging; the applied column uses the executors' checks).
  Decisions 6, 10 and 11 are not implemented. **Part 3 (decision 6, extended: effective title in
  every prompt, Enrich tags as suggestions, tick-box confirmation) design-locked by grill-phase
  2026-10-28** (see "Part 3 — implementation decisions") and fitted onto ADR-0068 by **implementation
  grill 2** (2026-10-29: suggestions in the Enrich artifact, Authors pages in publication's index
  step, name identity and filenames, the Details save model, four slices). **Slice 1 implemented
  2026-10-29**; **slice 2 design-locked by its grill and implemented 2026-10-29** (plan freshness and
  the stored raw list, the both-lists match, `keywords:`); **slice 3 design-locked by its grill
  2026-10-30** (form-level refusals and the baseline token, the provenance shape, reconciliation
  notices, the no-JavaScript form). Item
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
- **Two authority layers in the manifest:** *(amended 2026-10-29, implementation grill 2 Q1: the
  suggestions live in the Enrich artifact, not the manifest)*
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
    slug alike get a short hash suffix of the folded name. *(Amended 2026-10-29, implementation
    grill 2 Q3: a dedicated name fold, a reserved `--<hash>` suffix for lossy names only, the
    identity stored in the page.)*
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
- **Alias hygiene** (added 2026-10-26 after UAT round 7, user-approved). Aliases become matching
  keys here and in the drop filter (decision 3), so a loose alias silently files a later document
  under the wrong tag. In r7 the model gave *Continual Learning* the aliases "lifelong learning" and
  "continual pretraining" (found only in bibliography titles), "mortal learning" (a related concept
  the paper proposes) and "plastic learning" (not in the text at all). Three measures:
  - **(a) Prompt:** an alias is another name for the *same* thing (acronym, spelling variant,
    short/long form), used in the body text, never a related or broader/narrower term and never
    taken only from references. Covered by the items prompt bump of decision 7.
  - **(b) Code check after extraction:** an alias is kept only if its `merge_key` occurs in the
    document's non-reference text (the same reference carve-out the prompt uses); dropped aliases
    are recorded in the artifact's `filtered[]`. It catches invented aliases, not related terms
    that do occur in the text, so (a) is still needed.
  - **(c) Document screen:** when keeping a candidate, the aliases field is prefilled with the
    suggested aliases and editable, so the user can delete wrong ones before they become keys.
    Editing the aliases of an already-active tag stays deferred (ADR-0058 rename-of-active).
  - **Unchanged:** a merge still adds the absorbed tag's title and aliases to the survivor
    (ADR-0050 alias union) — that is how real duplicates teach resolution. (The odd aliases on
    *Continual Learning* in r7 were partly that union, from a deliberately artificial test merge.)
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
  paid Enrich pass plus one paid items pass, plus the user's review time. **Part 3 (Q1)** adds one
  paid claims pass: the claims prompt now takes the Enrich title.

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

## Part 2 — implementation decisions (grill-phase 2026-10-27)

Decisions 7 and 8 left seven implementation questions open. All seven were resolved with the user.

**P1. Prompt ceiling vs cap setting.** The items prompt asks for a centrality-ranked list of **at
most 20** — a prompt constant (changing it is a prompt-version bump). The **cap setting** (default
**15**) is applied only in code. It is not in the prompt and not in the freshness fingerprint. So
changing the cap restales nothing and re-bills nothing: a `--cache-only` Re-suggest replays the
stored responses and re-cuts them. Names past the cut are recorded in the artifact's `filtered[]`
with reason `over_cap`. The prompt-cache key hashes the prompt text (`app/llm/cache.py`), which is
why the setting is kept out of it.

**P2. Domains first.** The prompt asks first for 1–3 broad domain tags at bookmark-folder level,
with examples (AI, Business, Health, Philosophy, Finance, Software). Only the prompt can judge
"broad": in UAT r7 the model also typed narrow areas as `domain`. Code moves the **first three**
`domain`-typed suggestions to the front before the cut, so the cap never removes them. Any further
`domain`-typed suggestions keep their rank and compete like other tags; they are not cut, because
the user kept narrower ones such as *Philosophy of Mind*. A job counter `no_domain_sources` reports
documents with no domain suggestion (like `topic_starved`). Code never invents one.

**P3. Ambiguous matches.**
- *Exact name wins.* If the suggestion's own id is an active or unused tag, it links there and no
  resolution runs.
- *Otherwise resolve:* the suggestion's `merge_key` is compared with the titles and aliases of
  active and unused tags only. Candidates nobody kept are excluded, so one unreviewed suggestion
  cannot attract others.
  - One match: the suggestion links to that tag.
  - Several matches (ambiguous): the suggestion stays its own candidate link, and its artifact entry
    gets `ambiguous_with: [ids]`. Nothing goes into the ledger until the user decides. Ambiguity
    is recomputed at each extraction.
- *Screen:* the row says it matches several existing tags and offers **Use *X*** (one per matching
  tag, with its type and keep count), **Keep as new tag**, **Drop** and **Never suggest**.
- *Recording.* **Use *X*** keeps X on this document (a ledger keep plus a human link, like a
  human-add of an existing tag). It also drops the suggestion, and the drop record carries the
  suggested name, so re-extraction filters it. It writes one `tag-resolved {suggestion, chosen}`
  audit entry. **Keep as new** is a normal keep; the exact name then wins from now on.
- *Rejected alternative:* a pending choice stored in the ledger with no candidate node. "Keep as
  new" would then need a new tag-creation path.

**P4. The reference cut for the alias check.** The UAT PDFs extract as long paragraphs with no
headings, and their chunks carry no section metadata, so a heading search does not work.
- *The cut:* the **last** capitalised, standalone `References` / `Bibliography` / `Works Cited` /
  `Literature Cited` that meets all three conditions:
  - it lies in the second half of the text;
  - a year (`19xx`/`20xx`) or a bracketed number (`[1]`) follows within about 200 characters;
  - it is capitalised, which skips JSTOR's lower-case "This article references…" footer.
- *Fallback:* with no qualifying marker, the whole text counts as body.
- *The check:* an alias survives only if its `merge_key` occurs in the body text (case-,
  punctuation- and plural-insensitive). Removed aliases go into the artifact per suggestion
  (`aliases_filtered`). The artifact also records `reference_cut_at` (a character offset or
  `null`).
- *Scope:* footnotes and endnotes inside the body are left to the prompt rule. The deferred F4 work
  (reference-chunk down-ranking) can reuse this cut.

**P5. The alias field on keep.**
- It **replaces** the alias list (the existing approve-with-amendments semantics) and is
  **prefilled** with the tag's current aliases, after the P4 check. Aliases belong to the tag, so a
  deletion removes the alias from the tag everywhere.
- An unchanged field records no `aliases` amendment.
- The field becomes a **one-alias-per-line textarea**. A comma-split field would silently split a
  prefilled alias that contains a comma. The JSON route keeps its list.
- If the title is amended, the old title still joins the aliases.
- The field still appears only on a first keep. Editing the aliases of an active tag stays deferred
  (ADR-0058 rename-of-active).

**P6. The pipeline, in order** (applied to the ranked list of up to 20):
1. Clean the names, and apply the P4 alias check.
2. Resolve each suggestion (P3).
3. Filter drops and blocks against **both** the suggested name and the tag it resolved to: a drop of
   *LLM* also filters a later *Large Language Models* that resolves to it, and a blocked *LLM*
   blocks it too.
4. Move domains to the front (P2).
5. Cut at the cap. **Tags this document already keeps count toward the cap**, so a document with
   12 kept tags gets at most 3 new suggestions. Kept links are never removed by the cut.
6. A suggestion that resolves to a tag of a different type routes to the existing tag, which keeps
   its type, and files `change_item_type` (today's exact-name rule, extended to resolved matches).

**P7. Rollout.** Part 2 bumps only the items prompt. What the prompt receives is unchanged: the
title stays the filename-derived one, and whether part 3's Enrich title feeds it is a part-3
question. Part 2's UAT can use either a fresh clone (the UAT Guide flow; the user is happy to re-tag
all 9 documents) or a forced re-extraction of the r7 clone. Kept links and drops survive
re-extraction, so the code path is the same; the choice is made at UAT time. Decision 10's clean
restart still happens once all prompt bumps exist, before the golden-list measurement.

## Part 3 — implementation decisions (grill-phase 2026-10-28)

Decision 6 left the title's reach (P7) and the confirmation flow open. The user resolved five
questions; Q5 bundled the smaller points and was accepted as recommended.

Evidence from the 12-document UAT corpus:
- **PDF metadata titles:** 7 usable; 2 garbage ("Day_1_v3.pdf", a "(6) " browser-tab prefix);
  3 missing or only repeating the filename.
- **PDF metadata authors:** present on only 4 documents, and the manifesto lists only its first
  author. Metadata is a hint, never the answer.
- **Enrich tags vs suggestions:** of the 80 free-text Enrich tags, only 34 (42%) were already among
  the items suggestions. The rest were often the most descriptive topics. BIRD had none suggested:
  "logical data model", "banking reporting", "collateral", "securitization".

**Q1 (P7). The document title is inferred first and used everywhere.**
- **What it replaces:** a filename such as "1 s2.0 S0306437926000529 main" carries no information.
- **Where it is used:** the title replaces the filename-derived title in the Source page, the review
  screens and Obsidian, and in the **Enrich, claims and items prompts**.
- **How it is found:** extraction (deterministic, free) reads the PDF metadata title and author into
  the extraction log as a hint. Enrich, the first LLM stage, which already runs before claims and
  items, infers the title from the text plus that hint.
- **Order of preference** (the *effective title*): confirmed title > Enrich title > PDF metadata
  title > filename. The PDF metadata title is used only if a deterministic filter passes it: drop a
  value that looks like a filename, strip a browser-tab "(n) " prefix, skip a title that only
  repeats the filename. The filename is the last resort.
- **Cost:** decision 10's restart now also re-bills **claims** (73 calls on the UAT corpus), not
  only Enrich and items.

**Q2. The prompts take the Enrich title; corrections are display-only.**
- **Prompt input:** claims and items receive the **Enrich title**, or the fallback chain without the
  confirmed title when Enrich has no output.
- **Corrections:** a user correction changes the Source page, the screens, Obsidian and the
  Authors pages only. It never restales or re-bills claims or items, so it cannot replace reviewed
  claims or suggestions. Corrections are usually cosmetic, and the model already had a meaningful
  title.
- **Knock-on cost:** an Enrich re-run that *changes* the title restales and re-bills that
  document's claims and items. That is the accepted cost of the dependency.

**Q3. Enrich's tags become tag suggestions.** This is the user's call: the descriptive Enrich tags
are often the best tags for a bookmark.
- **One list per document:** Enrich's tags join the items suggestions in one per-document list, with
  the same keep / drop / never suggest, the same resolution (decision 8), and the same drop-record
  and blocked-list filters.
- **No separate tags list:** the Source page's free-text `tags:` frontmatter goes away. The page
  shows kept tags (linked) and the other suggestions (plain text), as today.
- **Typed:** the Enrich prompt bump outputs an `item_type` for each tag, so a keep needs no manual
  type choice (decision 2's sentinel gate still applies to anything coerced to the sentinel).
- **Order and cap:** Enrich's tags come **first** because they summarize the whole document, then
  the items suggestions in their P2 order (domains first). One cap (15, P1) applies to the combined
  list. A name in both lists appears once, at its Enrich position.
- **Search:** the suggested names stay indexed as keyword terms on the document, so search loses
  nothing.
- **Citations:** a tag link is navigation, not a factual claim. CLAUDE.md rule 3 does not require
  an evidence span for it.
- **Implementation defaults** (recorded here, open to the implementation review):
  - **Where the merge runs:** in code, in the P6 pipeline of `extract_items`. It reads the Enrich
    artifact, with no extra model call, and the items artifact records each suggestion's `origin`
    (`enrich` / `items`).
  - **Name in both lists:** it takes the items stage's `item_type` and aliases, since that prompt
    carries the taxonomy priority order.
  - **Enrich tags have no aliases:** the strict alias rule (P4) does not apply to them.
  - **Freshness:** a changed Enrich artifact re-plans the document's suggestions. With an unchanged
    title, a `--cache-only` replay suffices.

**Q4. Details are confirmed with an explicit tick box.**
- **The Details section:** the document screen gets a section above the tags, prefilled from
  `provenance_suggested`: title, authors (ordered, one per line, each with a "show page" tick) and
  publisher (with its own "show page" tick).
- **One Save:** the existing Save covers tags and details. The details become confirmed
  `provenance` **only if "These details are correct" is ticked**. A tags-only save never confirms
  them, so unreviewed authors cannot make two documents look independent.
- **"Show page":** a per-document decision, like a keep. An author who already has a page is
  pre-ticked as a suggestion on later documents, and the link counts once saved. Pages are built
  from **confirmed** details only, so a ticked name takes effect once that document's details are
  confirmed.

**Q5. Smaller points (accepted as recommended).**
1. **Name variants:** the Authors field offers the names already confirmed in the vault, so the user
   can pick the existing spelling. Nothing merges automatically, and author aliases stay deferred.
   Enrich is not given the existing names: that list grows with every document and would restale
   every Enrich call.
2. **The PDF hint** lives in the extraction log. Only the Enrich prompt reads it.
3. **The title fallback** without Enrich is the filtered PDF metadata title, then the filename (Q1).
4. **Enrich tags:** at most 7 per document, cleaned like other names ("planning-taxonomy" becomes
   "planning taxonomy").
5. **The knock-on re-bill** of Q2 is accepted.
6. **Clean restart:** part 3 carries the last prompt bump. Decision 10's backup-gated restart
   follows part 3's UAT, and part 4's scorer measures the rebuilt vault.
7. **Two demo-day gaps** (2026-10-28) are added to the deferred list below.

**Versions:** the Enrich schema and prompt version and the claims and items prompt versions all
bump (new outputs, new title input).

## Part 3 — implementation grill 2 (2026-10-29, after ADR-0068)

ADR-0068 (one write lock, local publication, preview tokens, the producer journal) landed between
part 3's design and its implementation. This pass fits part 3 onto it.

**Q1. Suggested provenance lives in the Enrich artifact** (amends decision 6, "two authority
layers in the manifest"). The suggested title, ordered authors and publisher are written to
`normalized/enrichment/<sid>.json`, beside the summary and the typed tags; there is no
`provenance_suggested` in the manifest. Confirmed `provenance` stays manifest authority.
- **The boundary:**
  - Enrich replaces its suggestions only with a successful, validated result; a failed run keeps
    the previous artifact.
  - **Producers never overwrite confirmed provenance**, and re-enrichment never clears a human
    confirmation. (Manifests do receive producer-written extraction and lifecycle fields; the
    rule is about confirmed provenance, not about who may write a manifest.)
  - Confirming copies the selected values into the manifest through `set_provenance`, under the
    write lock.
  - Authors pages derive from confirmed provenance only, never from suggestions.
  - The artifact's canonical typed-tag list is the only copy; the suggestions carry no second
    tag list.
- **Why:** the Enrich commit stays a single artifact write (no artifact/manifest pair to keep
  consistent across a crash, so no journal record), and the suggestions are regenerable output
  (from the response cache). The "Enrich now suggests …" note compares the artifact with the
  manifest at render time.
- **Required tests:** re-enrichment preserves a confirmation; a failed Enrich keeps the previous
  artifact; Authors pages exclude unconfirmed suggestions.

**Q2. `wiki/Authors/` is rebuilt in publication's `index` step** (ADR-0068 decision 4), as a
whole-folder pass beside `wiki/index.md`; no new queue field (a Details save records a provisional
intent for its document, with `index=True`).
- **One inventory:** a single validated inventory built from confirmed provenance and the
  show-page choices; the Authors pages and the `index.md` "Authors & publishers" section both
  render from it.
- **Order:** Authors pages → `index.md` → keyword search. If the Authors or index part fails, the
  entry keeps both its index and keyword work, so a retry can't leave search stale.
- **Pruning only what code owns:** a page is removed only if it carries the dedicated generator
  marker and schema of an Authors page (not just ordinary frontmatter fields) and nothing in the
  inventory references it any more. Removing a wholly generated, unreferenced page is projection
  cleanup and needs no governance review (CLAUDE.md rule 9 concerns content, not regenerable
  projections).
- **Handwritten pages are never overwritten:** a filename collision with a page that lacks the
  marker is reported as a conflict, and the publication stays pending.
- **Fail before pruning:** if the manifests can't be read reliably, the step fails before
  removing anything; an unreadable manifest never looks like a removed reference.
- Writes are suppressed when content is unchanged.
- **Required tests:** a rename or an unticked "show page" removes the old generated page; a name
  shared by several documents stays while any references it; a handwritten page is preserved,
  including on a filename collision; a malformed manifest prevents pruning; recovery after a
  partial render failure.

**Q3. Page identity is the normalized name; filenames never depend on other names** (amends
decision 6's "two names that slug alike get a short hash suffix").
- **A dedicated name fold, not `merge_key`:** NFKC, casefold and whitespace normalization only.
  `merge_key` singularizes the last token ("Robin Woods" and "Robin Wood" would share a page),
  which is wrong for names. Accents and non-Latin characters stay in the identity.
- **Pages per normalized name, across the author and publisher roles** (a page lists each role's
  documents). Not "per person": an identical name doesn't establish that two people or
  institutions are the same referent. "D. C. Dennett" vs "Daniel C. Dennett" stay two pages
  (author aliases remain deferred).
- **Filename:** the plain slug when the fold is lossless (only `a–z`, `0–9` and spaces, which slug
  one-to-one); otherwise `<slug>--<8 hex of sha256(fold)>`, with an empty slug as `unnamed`. The
  `--` separator is reserved: a plain slug can never contain it (the slugifier collapses runs of
  separators), so the plain and hashed namespaces never collide. (Without it, "José" →
  `jos-d994e1d0` and the ASCII name "jos d994e1d0" would share a filename.)
- **The full folded identity is stored in the page's generated frontmatter.** Eight hex characters
  can still collide: a page whose stored identity differs from the name being rendered refuses
  publication (a reported conflict, publication pending). Names are never combined, and a page
  is never overwritten, because filenames match.
- **Title:** the most common confirmed spelling among the page's documents (ties: the first
  alphabetically); every confirmed spelling goes into `aliases` (ADR-0060 quick switcher).
- **Required tests:** "Robin Woods" / "Robin Wood" get two pages; accented names; non-Latin names;
  a plain slug against a hashed one; a forced hash collision refuses rather than merges; adding
  names never renames an existing page.

**Q4. A Details save is one validated commit; confirmation is never silently undone.**
- **One commit for the whole Save** (ADR-0068 screen action: one hold, one publication intent,
  `sources=[sid]`, `index=True`). Tag decisions and details are validated together: title not
  empty, each name non-empty and normalizable, no duplicate normalized names among the authors.
  Any error writes **nothing**, and the form shows the errors with the values kept. *(Refined by
  the slice-3 grill Q1: any **form-level** error writes no authority changes; per-row tag skips
  stay non-fatal.)*
- **Revalidated under the lock against what the form displayed:** the source's lifecycle, its tag
  decisions and its existing confirmation. A concurrent Details edit (or tag change) since the
  form was rendered refuses the save as a stale form, never overwriting it. The form carries
  what it showed, so the stored artifact fingerprint is **the one the human saw**, not whichever
  artifact is newest at commit time.
- **A confirmation stores** (through `set_provenance`): `title`; `authors` as an ordered list of
  `{name, show_page}`; `publisher` as `{name, show_page}` or none; `confirmed_at`;
  `confirmed_from_artifact` (the Enrich fingerprint shown, for "Enrich now suggests …").
- **No silent un-confirming:** a Save without the tick never changes or clears an existing
  confirmation; changing confirmed details needs the tick again.
- **Clear confirmed details** is an explicit button and its own commit phase. It removes the
  confirmed title, authors, publisher and the confirmation metadata, and keeps every unrelated
  manifest field. `report_family` and `canonical_url` still take part in independence (ADR-0018,
  unchanged), so after a Clear independence rests on whatever confirmed fields remain; it is not
  necessarily unknown.
- **Independence with several authors** (adapts `manifests.independent_sources`): authors compare
  as sets of normalized identities (Q3's fold), ignoring order, spelling variants and
  `show_page`. Sharing any author means the author field can't prove independence; disjoint sets
  can. Merely unequal lists are not enough. The publisher compares by its normalized identity.
  Confirmation timestamps and artifact fingerprints never affect independence.
- **Producers reconsider on their next run, visibly:** a save never re-runs synthesis or
  contradiction detection. *(Amended by the slice-3 grill Q3:)* both producers already recompute
  independence from current provenance on every run (synthesis eligibility; contradiction
  candidate pairs, with pairs that left the set superseded), so a provenance change is never
  skipped as "fresh" and no model fingerprint changes. Recorded per-document digests only report
  what is still **awaiting reconciliation** (on the document screen after the save, and by the
  maintenance report) until that run. A topic that becomes ineligible is retracted by synthesis
  step 3.
- **Legacy manifests:** a string `author` is read as a one-name list; manifests are not
  rewritten (the clean restart follows).
- **Required tests:** combined validation failure writes nothing; concurrent Details saves (the
  second is a stale form); re-enrichment between display and Save stores the fingerprint shown;
  a Save without the tick preserves a confirmation; Clear with remaining `report_family` /
  `canonical_url`; reordered and overlapping author lists; synthesis and contradiction detection
  reconsider after a provenance change.

**Q5. Four slices, then one UAT, then the clean restart.** Each slice is reviewed and committed
before the next:
1. **Title and Enrich output:** the PDF metadata hint in the extraction log with the deterministic
   title filter; the effective-title chain; **every prompt bump of part 3** (Enrich: title,
   authors, publisher, typed tags ≤ 7, stored in the artifact per Q1; claims and items: the Enrich
   title as input, included in their freshness checks).
2. **Enrich tags as suggestions:** the merge in `extract_items` (origin, order, combined cap of
   15) and the removal of the Source page's free-text `tags:`.
3. **Details confirmation:** the screen section, the save model and Clear (Q4), independence with
   author lists, the independence freshness inputs and the awaiting-reconciliation report.
4. **Authors pages:** the inventory, pages and index section in the `index` step (Q2) with the
   identity and filename rules (Q3), `validate_wiki` coverage, and the Obsidian `path:Authors`
   colour group in the docs.

- **One prompt-bump round holds only if** slice 1 fixes the final prompt and response contracts,
  and slice 2's merging, origin tracking, ordering and cap are deterministic processing. If later
  work changes a prompt's meaning anyway, that prompt's version bumps again; an inaccurate cache
  identity is never kept.
- **Display corrections stay apart from extraction inputs:** claims and items freshness includes
  the Enrich title they receive; confirming a different display title never restales them.
  Independence changes get their own freshness inputs (slice 3).
- **Paid calls:** none during development (fake adapters; the main `wiki/` is empty). **UAT** runs
  once, after all four slices pass review and their gates, on the `/tmp/ks-uat` clone: first the
  deterministic extraction, so the logs carry the new PDF hints, then the paid stages
  (estimated ~12 Enrich, ~73 claims, ~12 items calls; retries and windowing change the totals).
  The resulting response cache is preserved (as on 2026-10-28, under `~/ks-saved/<date>/`) before
  decision 10's backup-gated clean restart, which bills the real corpus once more.

## Implementation — part 3 slice 1 (2026-10-29): titles and Enrich output

- **The PDF hint:** the PDF extractor reads the document-info title and author
  (`Extraction.metadata`, an untrusted hint; a broken info dictionary is no hint, never an error)
  and extraction writes it to the log as `metadata`. `EXTRACT_CODE_VERSION` is 2. It stays
  observability only, so the UAT clone is re-extracted with `--force` before the paid run (Q5).
- **Titles** (`app/workers/titles.py`): `filter_pdf_title` (drops filename-like values, an editor's
  "Microsoft Word - …", "Untitled", a value that only repeats the filename; strips a browser
  tab's "(n) "), `prompt_title` (Enrich > filtered PDF > filename) and `effective_title`
  (confirmed > prompt title). The confirmed title is read from the manifest's
  `provenance.title`; its write path arrives with slice 3. `name_identity` is grill 2 Q3's fold.
- **Enrich v2** (`SCHEMA_VERSION = "enrich-v2"`, `PROMPT_VERSION = "enrich-prompt-v2"`): the
  prompt receives the file-name title and the PDF hint inside an untrusted `<file_metadata>`
  block (escaped like the title) and returns `title`, ordered `authors`, `publisher`, `summary`
  and typed `tags` (unknown values are `""` / `[]`). The artifact stores them cleaned: authors
  de-duplicated by `name_identity`, tags cleaned like other names ("planning-taxonomy" →
  "planning taxonomy"), de-duplicated, at most 7 (`ENRICH_TAGS_PROMPT_MAX`), an invalid type
  coerced to the review sentinel. The item-type guide is now one constant shared with the items
  prompt, whose text stays byte-identical.
- **Prompt inputs in freshness:** `_fingerprint` takes a `prompt_input` component (every
  model-visible input besides the text), recorded in each artifact as `prompt_input` so
  render-side freshness recomputes it. Enrich's is the file title plus the PDF hint (a
  re-extraction that finds a hint restales it); claims' and items' is the prompt title. Claims and
  items compute the prompt title before their freshness check and prompt with it
  (`CLAIM_PROMPT_VERSION = "enrich-claims-prompt-v4"`, `ITEMS_PROMPT_VERSION =
  "enrich-items-prompt-v5"`). Lint's summary-rot check uses the same Enrich input
  (`enrich_prompt_input_for`), so it never flags a fresh artifact.
- **Surfaces:** the Source page (frontmatter `title` and heading) takes the effective title; the
  review screens read it back from the Source page's frontmatter (the document list and the
  document screen's heading, with the source id and filename beside it). Until slice 2, the
  Source page's free-text `tags:` lists the Enrich tags' names.
- **Tests:** `tests/test_titles.py` (24): the filter cases from the UAT corpus, both chains and a
  junk PDF title falling through, the name fold (plurals, accents, scripts), the hint in the log,
  Enrich's prompt and cleaned suggestions, unknown values, re-enrichment preserving confirmed
  provenance, a failed Enrich keeping the previous artifact, a new hint restaling Enrich, the
  Source page and review screens, claims and items prompted with the Enrich title, the fallback
  title, a confirmed title never restaling them, an Enrich title change restaling them, and lint
  agreeing with Enrich. Existing fakes moved to the v2 response shape; the version pins and the
  title-encoding test followed (plus a hint-escaping test).
- **Verification:** 1771 passed + 2 skipped; ruff clean. 18 mutation checks, all caught (the
  unfiltered-PDF-title one after the junk-title test).
- **Review round 1 on slice 1 (2 P1, 1 P2, fixed):**
  1. *Commits rechecked only the text:* a hint changed during the Enrich call, or a prompt title
     changed during the claims or items call, could commit a result for inputs the model never
     saw. Each pass now captures its prompt inputs once (Enrich no longer reads the hint twice:
     the messages and the fingerprint come from one capture) and, under the commit lock, before
     any write, recomputes the current inputs (`enrich_prompt_input_for`, `prompt_title_for`) and
     writes nothing if they differ (`skipped_input_changed` / `input_changed_sources`). Tests: a
     hint changed during the Enrich call; a title changed during the claims call and during the
     items call; an Enrich re-run retitling the document during the items call.
  2. *Readers accepted stale prompt inputs:* `_load_fresh` recomputed freshness with the
     artifact's own recorded `prompt_input`, so a new hint left rendering and the prompt title on
     the old Enrich title while lint called it stale. `load_fresh` / `load_fresh_claims` now take
     the **current** expected input (keyword-only, required); the recorded model and strategy
     behaviour is unchanged. Test: after a hint change the Source page falls back to the filtered
     PDF title with a stub summary, and the prompt title follows, until Enrich runs again.
  3. *Titles could assert wikilinks:* the Source page title (frontmatter and heading) is now
     neutralised with `_delink` like other generated text. Test: an Enrich title and a PDF
     fallback title carrying `[[itm_…|…]]` and `[[Items/x]]` render as plain text.
  5 mutation checks on these fixes, all caught. `tests/test_titles.py` now has 31 tests; one
  existing claim-windows test now passes the current prompt title. Gate: 1778 passed + 2 skipped.

## Part 3 slice 2 — grill (2026-10-29): Enrich tags into the suggestions

Part 3 Q3 fixed the design (one list, Enrich first, combined cap of 15, the same resolution and
filters, `origin`, a name in both lists takes the items type and aliases). This pass settles how it
stays fresh and deterministic.

**Q1. Model freshness and plan freshness are separate; the raw model output is kept.**
- **The items artifact stores the validated raw `items` list** as the model returned it, before
  merging, capping or any human decision. Every re-plan reapplies the current keep/drop/block
  rules, the alias protections and the cap to it.
- **`merge_input`:** a hash of the canonical, ordered Enrich tags the plan actually used, their
  types included, beside a deterministic **merge-version marker** (a change to the merge rules
  bumps the marker, never a prompt version). Absent or stale Enrich output is an explicit empty
  input, so Enrich tags disappearing also triggers a re-plan.
- **Re-plan without a model call:** when `input_fingerprint` is fresh but `merge_input` or the
  merge version differs, the run re-plans from the stored raw list. That needs neither the
  provider nor the response cache. It is an ordinary per-document commit: the write lock, the
  producer journal record, publication, and the same filters.
- **Under the commit lock,** before any write, both the model inputs (slice 1) and the merge input
  are recomputed; if either changed during the run, nothing is written.
- **The raw list survives recovery:** it stays in the artifact through journal recovery and
  repeated interruptions (a replayed commit re-plans from it).
- **A valid empty raw list is not missing data.** An older artifact without the raw list (or a
  malformed one) never re-plans from `[]`, which would withdraw every suggestion: the run reports
  that regeneration is needed, and a `--cache-only` replay recovers it where the cache still has
  the response.
- **Required tests:** a tag-only Enrich change re-plans with the response cache unavailable and an
  adapter that fails if called, and keeps, drops and blocks survive it; Enrich tags removed;
  their order changed; a legacy artifact without the raw list; the merge input changing during a
  run.

**Q2. "The same name in both lists" is matched before resolution; the items name is kept.**
- **Matching:** an Enrich tag matches an items suggestion when its `tag_key` (the `merge_key`
  fold used for keep/drop and block keys) equals that suggestion's name key or the key of one of
  its **body-checked** aliases (aliases the P4 check rejected never match). Example: Enrich "LLM"
  and items "Large Language Model" with the body alias "LLM".
- **When:** the merge completes before resolution and before the combined cap.
- **The merged entry:** at the Enrich position, with the items suggestion's name, type and
  aliases, and `origin: both`. Unmatched Enrich tags keep their own name and type, no aliases,
  `origin: enrich`; items-only suggestions are `origin: items`.
- **The matched items entry is consumed:** it doesn't appear again in the items tail. Several
  Enrich forms matching the same items entry yield one result, at their earliest Enrich position.
  An Enrich tag matching several items suggestions merges with the first in items order; the
  others go through normal resolution.
- **Every contributing name key is checked against the decisions:** keep/drop/block filters see
  the Enrich form's key as well as the items name's, so taking the items name never bypasses a
  drop or block recorded against the original form.
- **After resolution,** entries that collapse to one existing tag union their origins. The
  existing ambiguity checks and governed-type protections are unchanged: a suggested items type
  never silently retypes an existing tag (a differing production type files `change_item_type`,
  as today).
- **Required tests:** acronym matching through an alias; a rejected alias never matches; several
  Enrich forms matching one item; several items matching one Enrich tag; a drop and a block
  recorded under the Enrich form.

**Q3. A `keywords:` property replaces the Source page's `tags:`** (clarifies part 3 Q3's
"search loses nothing"). The keyword index's navigation row reads only the title, `aliases`,
`tags` and the summary callout, never the body's Suggested section, so dropping `tags:` alone
would have removed the Enrich tag names from search.
- **What it holds:** the names from the same projection the document screen uses: every kept
  tag (all keeps are preserved, so the list can exceed the suggestion cap) plus the current
  eligible suggestions, capped by the existing rule. Dropped, blocked, hidden and over-cap
  suggestions are excluded. Names are de-duplicated deterministically, in the agreed order.
- **The index:** for Source pages, `keywords` is read into the existing tags column whenever the
  property exists, **even when empty**; the legacy `tags` is read only when `keywords` is
  absent, so an intentional empty list can never revive old terms. Other page types keep their
  `tags` indexing unchanged. No schema change.
- **Obsidian:** `keywords:` is an ordinary property, not a tag, so unreviewed names stay out of
  the tag pane.
- **"Search loses nothing" means:** every eligible displayed name stays searchable; a name
  deliberately filtered or removed disappears from the index.
- **Required tests:** a keyword-only match finds the document; filtered names are not indexed;
  the legacy `tags` fallback; an empty `keywords` overriding legacy tags; a keep or drop
  updating search at once (through publication's keyword refresh).

**Implementation notes (from part 3's wording, not new decisions):**
- The domains-first move (P2) applies to the items tail only; Enrich's tags keep their own order
  at the head ("then the items suggestions in their P2 order (domains first)").
- The raw cap on the model's items list (`_MAX_ITEMS`) applies to that list before the merge, so
  the Enrich head never pushes items suggestions out of the raw list; the combined list is then
  cut by the suggestion cap as today.
- An Enrich-origin suggestion is navigation, not a factual claim: like any suggestion it carries
  an evidence anchor only when its name is mechanically locatable (CLAUDE.md rule 3 does not
  apply, part 3 Q3).

## Implementation — part 3 slice 2 (2026-10-29): Enrich tags into the suggestions

- **The merge** (`items._merge_enrich`, step 0 of `plan_suggestions`): items candidates are built
  first (the `_MAX_ITEMS` raw cap, name cleaning and the P4 body-alias check apply to the items
  list alone); each Enrich tag then merges into the first candidate whose name key or body-checked
  alias key equals its `tag_key` (items name, type and aliases; `origin: both`; the Enrich form's
  key and id kept as `extra_keys` / `extra_ids`), or becomes its own head entry (`origin:
  enrich`). Matched candidates leave the tail; later Enrich forms for the same candidate only add
  their key. Resolution then runs per entry as before; drop and block checks see every
  contributing key and id; entries collapsing onto one tag union their origins; domains-first
  moves only the items tail. Emitted rows and the artifact's `nodes` carry `origin`.
- **Plan freshness** (`items.merge_input`, `MERGE_VERSION = "enrich-merge-v1"`): the merge input
  is a hash of the fresh Enrich artifact's ordered `{name, item_type}` tags
  (`titles.enrich_tags_for`, `[]` when absent or stale). The items artifact stores `raw_items`
  (the validated model list), `merge_input` and `merge_version`. A model-fresh document whose
  plan differs re-plans from `raw_items` (`replanned_sources`), with no provider and no cache; one
  without a valid raw list is reported (`replan_unavailable_sources`, status `partial`,
  `scripts/extract_items.py` prints the `--cache-only` / `--force` remedy and exits 1) and left
  untouched. The commit rechecks the merge input under the lock with the text and title.
- **Interrupted commits** (an implementation choice, flagged for review): an items commit's
  producer-journal record now carries `fingerprint` = a hash of the raw list it plans from
  (`_raw_hash`). A resumed commit replays the stored `raw_items` only when the record's
  fingerprint equals their hash, i.e. the interrupted attempt was committing exactly that list (a
  re-plan, or a model run that returned the same output). An interrupted forced re-extraction,
  whose newer output was never stored, goes back to the model (or the response cache) as before:
  replaying the older list would have revived the tag it was retiring (caught by
  `test_an_interrupted_re_extraction_with_fewer_tags_retires_the_lost_one`). A repaired journal
  (no fingerprint) never replays.
- **`keywords:`** (`wiki._keywords`, `templates/source.md`): the kept tags (projection order)
  then the current suggestions in the items artifact's plan order, hidden tags left out,
  de-duplicated by name; the free-text `tags:` and its Enrich-tag rendering are gone.
  `keyword_index._search_terms` reads a Source page's `keywords` whenever present (even empty),
  else its legacy `tags`; other page types are unchanged.
- **Enrich tag names** are now also stripped of wikilink syntax and brackets when stored
  (`titles.clean_tag_name`), since they become tag page titles.
- **Tests:** `tests/test_enrich_tags.py` (21) covers every test the grill required (acronym
  matching, a rejected alias, several Enrich forms on one item, several items for one Enrich tag,
  drops and blocks under the Enrich form, the ordering rules, the raw and combined caps, origin
  union; re-plans with no cache and an adapter that fails if called with keeps, drops and blocks
  surviving, tags removed and reordered, a stale Enrich artifact as empty input, a legacy and a
  malformed raw list, a merge-version change, a merge-input change during the call, an
  interrupted re-plan replayed; `keywords:` contents, a keyword-only search hit, a dropped name
  leaving search, the index's fallback rules). Three existing tests moved to the new contract
  (`origin` in artifact rows; Enrich tags in the artifact rather than `tags:`).
- **Verification:** 1799 passed + 2 skipped; ruff clean. 21 mutation checks, all caught.
- **Review round 1 on slice 2 (1 P1, 2 P2, fixed; the journal raw-list hash accepted):**
  1. *The merge could bypass governance:* hidden/retired/redirected outcomes were checked only for
     the items name, so a hidden "LLM" merged into "Large Language Model" (alias LLM) surfaced as
     `origin: both`, and two tags whose merge was rejected collapsed into one suggestion. Now
     every entry is resolved first (`_resolve`, the former inline step), and `_may_merge` allows a
     merge only when the Enrich form names no hidden/retired/ambiguous tag, either names no live
     or decided tag (`new`, or an unreviewed candidate) or resolves to the candidate's own target,
     and the two targets are not a rejected merge pair. Otherwise the form stays its own entry,
     resolved and filtered on its own. Tests: hidden and retired forms (filtered alone, the items
     suggestion `origin: items`), a redirected form (follows its merge), a rejected pair of active
     tags, and a rejected pair where the form is only a candidate node.
  2. *A provider-free re-plan reported `skipped`:* the status now follows the work done:
     `skipped` only when nothing was re-planned; `partial` when some documents still wait for a
     provider; otherwise `succeeded`, so `publication.job_status` can still turn a failed refresh
     into `saved_publication_pending`. Tests: an unavailable adapter with no cache re-plans to
     `succeeded`, and to `saved_publication_pending` when the end refresh fails; an offline run
     with nothing to re-plan stays `skipped`.
  3. *The printed recovery did not recover:* a `--cache-only` run now recovers an artifact
     without a stored raw list by replaying the cached response (a miss is reported as a cache
     miss); the script prints exactly `uv run python scripts/extract_items.py --cache-only <sid>`
     (`RECOVER_COMMAND`). Test: the printed command, run as printed, restores the raw list.
  6 mutation checks on these fixes, all caught (the rejected-pair one after the candidate-node
  test). `tests/test_enrich_tags.py` now has 29 tests. Gate: 1807 passed + 2 skipped.

## Part 3 slice 3 — grill (2026-10-30): Details confirmation

Grill 2 Q4 fixed the save model. This pass settles what it left open.

**Q1. Form-level refusals abort the Save; row-level tag skips stay non-fatal.**
- **Form-level refusals** write no authority changes, tags included; the form shows the reason
  with every submitted value preserved:
  - **invalid Details** (an empty title, an empty or unnormalizable name, a duplicate normalized
    author);
  - **a stale form:** the form carries a **baseline token**, a digest of what it displayed —
    the source's lifecycle status, this document's tag-ledger state, its confirmed provenance,
    and the **actual displayed Details values and suggestions** (the Enrich output and the
    fallback values shown), not merely Enrich's input fingerprint, since a forced Enrich run can
    return a different title or authors under the same fingerprint;
  - **Details edited without the tick:** "tick *These details are correct* to save the details,
    or undo your edits"; the edits are neither saved nor silently dropped.
- **"Touched" is judged against the displayed baseline:** the tick is set, or a submitted Details
  field differs from the value the form displayed (carried with the form), never from values
  freshly loaded during the POST, so a concurrent update can't make an untouched tags-only
  submission look like a Details edit. Details are validated only when touched: unchanged or
  absent Details never obstruct pure tagging, and a tags-only Save keeps today's per-row
  revalidation with no new stale-form refusal.
- **Under the write lock, one commit phase:**
  1. if Details are touched, validate the baseline token, the fields and the tick, before any
     tag or Details write;
  2. on refusal, write nothing and report the form-level reason, values preserved;
  3. otherwise process the tag rows with today's revalidation and skip reporting, save the
     confirmed Details, and publish through one intent.
- **Atomicity is at the validation level:** the lock makes the checks and writes one serialized
  phase, but the writes span several files (ledgers, graph, manifest) and are not crash-atomic.
  A crash part-way can leave some tag decisions saved without the Details; the publication queue
  covers the projections, and the user saves the Details again.
- **Required tests:** a tags-only submission after a concurrent Details change is accepted (rows
  revalidated, no stale refusal); a same-fingerprint Enrich output change makes a Details save
  stale; Details edited without the tick are refused with nothing written; a valid Details save
  containing one skipped tag row saves the Details and reports the skip.

**Q2. Confirmed Details in the manifest's `provenance`** (`set_provenance` stays the one write
path).
- **Keys:** `title` (string); `authors` (a new plural key: an ordered list of `{name,
  show_page}`); `publisher` (`{name, show_page}`; the same key, which still reads a legacy
  string); `confirmed_at`; `confirmed_baseline` (the Q1 digest of what the person saw, replacing
  grill 2's `confirmed_from_artifact`). `report_family` and `canonical_url` are unchanged.
- **Precedence and legacy values:** `authors` wins whenever present, **even an empty list**; the
  legacy string `author` is read (as `[{name, show_page: false}]`) only when `authors` is absent.
  A legacy string `publisher` reads as `{name, show_page: false}`. Legacy values were set by a
  person (ADR-0018), so they count as confirmed. Nothing is rewritten until a confirmation: the
  first one writes the new keys and removes the legacy `author`.
- **Independence** (`manifests.independent_sources`): authors compare as sets of `name_identity`
  values, and **any shared author** means the author field establishes shared provenance:
  {Alice, Bob} and {Alice, Carol} are not independent, and merely unequal sets are not enough.
  The publisher compares by its identity. The existing rule is kept: independent only if at
  least one comparable field differs and no comparable field is shared. `show_page`,
  `confirmed_at` and `confirmed_baseline` never take part. **`confirmed_baseline` is an audit
  record only**: never an independence input, never a producer-freshness input.
- **Clear** removes `title`, `authors`, `publisher`, the legacy `author`, `confirmed_at` and
  `confirmed_baseline`, and keeps `report_family`, `canonical_url` and everything else. Independence
  may therefore stay established through those fields: the UI never promises that a Clear makes
  it unknown.
- **Validation matches Q1:** a confirmed title is non-empty after cleaning; each author and the
  publisher name is non-empty after cleaning; duplicate normalized authors are rejected;
  `show_page` must be a boolean. An explicit clear (Clear, or `None` through `set_provenance`) is
  distinct from malformed input, which is rejected.
- **Required tests:** overlapping and reordered author sets; `authors` vs legacy `author`
  precedence (an empty `authors` included); mixed publisher shapes; strict validation; Clear
  keeping unrelated provenance.

**Q3. "Awaiting reconciliation" from per-document digests the producers record.**
- **One shared normalization** (used by `independent_sources` and the digest alike): a document's
  independence inputs are its normalized author identity set, publisher identity,
  `report_family` and `canonical_url`. Author order, spelling variants covered by
  `name_identity`, `show_page`, the title and confirmation timestamps never change the digest.
- **Recorded from what the run actually used:** each `generate_synthesis` and
  `detect_contradictions` run captures the digests of the provenance it used for its eligibility
  decisions (not current provenance read at job completion: Details may change during a model
  call). Before acknowledging, it verifies those inputs under the write lock; a document whose
  inputs changed meanwhile stays awaiting reconciliation (or is recomputed and reconciled first).
  Only documents covered by a **completed** reconciliation are acknowledged: failed, partial or
  out-of-scope work never clears a notice. The acknowledged digests go in the run's job metadata.
- **Awaiting reconciliation,** per producer independently: the document's current digest differs
  from the one that producer last acknowledged. A Clear is a transition to the remaining inputs,
  detected the same way. Only a document with **no provenance and no prior reconciliation
  history** gets no warning just because no producer has run yet. Clearing the last provenance
  field is still a change: when a producer acknowledged a non-empty digest before, the empty
  state differs from it, so the document is awaiting reconciliation (the next run may need to
  withdraw assertions that relied on the old independence). A document with provenance whose
  acknowledging job history is missing or pruned is conservatively *not known to be reconciled*.
- **Shown:** on the document screen after a Details save or Clear, naming whichever producer is
  pending ("Synthesis hasn't run since these details changed", and/or the same for contradiction
  detection); and in the maintenance report (`lint`), listing documents and the producer to run.
  Report-only: no validator fails.
- **Required tests:** a provenance change during a run stays awaiting; a partial run acknowledges
  only what it completed; Clear; **Clear to empty** (the last provenance field removed after an
  acknowledged run shows awaiting reconciliation); display-only edits (title, `show_page`, author order or
  spelling variants) raise no notice; each producer acknowledges independently.

**Q4. The Details form, without JavaScript.**
- **Prefill:** the confirmed values when there are any; otherwise Enrich's fresh suggestions;
  otherwise the fallback title (filtered PDF title or filename) with no authors or publisher.
- **Fields:** the title (one input); the authors as one row each, in order (a name input, a
  **show page** checkbox and a small order-number field), plus three empty rows; the publisher
  (a name input and a **show page** checkbox).
- **Draft-only actions** (POSTs that re-render the form and write nothing — no tag decision, no
  provenance, no publication):
  - **Add author rows** adds three empty rows. (Using a Save without the tick for this would
    clash with Q1's refusal of unconfirmed Details edits.)
  - **Use these**, beside "Enrich now suggests …" (shown when confirmed details differ from the
    current Enrich suggestion), replaces the Details fields with the suggestion, preserves the
    pending tag selections and every other form value, and leaves the tick unset.
- **Checkbox defaults only at server-rendered prefill:** a name that already has an Authors page
  is pre-ticked "show page" when the server fills the row. A `<datalist>` of the vault's confirmed
  names offers existing spellings, but can't tick anything when a name is typed or picked. A
  submitted choice is preserved exactly, unchecked included (each row carries a hidden marker, so
  an absent checkbox reads as unchecked); an existing Authors page never overrides it.
- **Authors parsing:** blank rows (cleared ones included) are dropped before validation; the
  remaining names are validated and de-duplicated, then ordered by the number field, with row order
  as the tie-breaker.
- **Status line:** "Details confirmed on <date>" with the **Clear confirmed details** button; or,
  when unconfirmed, "Suggestions from Enrich" only when fresh suggestions exist, otherwise naming
  the fallback ("Title from the PDF metadata" / "Title from the file name"). Clear is its own form,
  outside the main form (HTML forms can't nest), or a button tied to a separate form through the
  `form` attribute.
- **The tick box** "These details are correct" sits beside the single Save.
- **Required tests:** draft actions preserve tag selections and write nothing; cleared author
  rows; stable ordering; checkbox choices preserved (an unchecked box against an existing page);
  editing Details without confirming.

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
  - queue content column, grouping, bulk actions and local time;
  - **a "not yet tagged" marker on the document list plus a "next untagged document" link**
    (demo day 2026-10-28: a document with 14 suggestions was never saved, and nothing showed it).
- **Conflicting `change_item_type` proposals on one item** (demo day 2026-10-28: *Multi-Agent
  System* had an approved retype to `ai_topic_area` and a pending one to `architecture_pattern`).
  When a new retype is filed while another is pending or approved for the same item, flag the clash
  on both, or replace the older pending one. Needs a short grill.
- **Author aliases** (part 3 Q5.1): "D. C. Dennett" vs "Daniel C. Dennett" stay two names. The
  user picks an existing spelling from the field's suggestions; nothing merges automatically.
- **Claims-prompt quality from the real title** is assumed, not measured. If part 4's eval shows
  no gain, the title input to claims can be revisited.
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

## Implementation — part 1 (2026-10-25)

Decisions 1–5 and the decision 4 identity-operation rules.

**Code:**
- `app/workers/tag_links.py` (new): the per-document ledger, keep/drop/undo-drop, deletion,
  the Unused tags list and **Delete unused**, block/unblock, and the merge/split ledger fixups.
- `graph.py`: the `unused` status, `kept_sources_for_node`, `source_tag_links`, `delete_node`.
- `promote.activate_node`: the single promotion writer, shared by keep and the approved-item
  executor.
- Extraction filters (`items.extract_items`), the human-add rewrite, merge/split changes, the
  per-source read model and screen, and the new routes.

**Routes:** `POST /ui/reviews/sources/{id}/decide` (`link_<node_id>` = keep/drop/block),
`POST /reviews/sources/{id}/links` (JSON), `POST /ui/reviews/sources/{id}/undrop`,
`/ui/reviews/tags/unused` (+ `/delete`), `/ui/reviews/tags/blocked` (+ `/unblock`).

**Tests:** `tests/test_tag_links.py` (new); the source-flow, promote, items, reconcile, merge, split
and synthesis tests are updated to the new model.

Implementation choices not spelled out above:
- **Keep, drop and block take effect immediately**, like the ADR-0058 human-add, with no apply
  step. They change only the document's own links. Deletion happens only as a consequence of
  human drops or the explicit **Delete unused**. Each action writes an audit entry
  (`tag-kept`, `tag-dropped`, `tag-deleted`, `tag-blocked`, `tag-unblocked`, `tag-drop-undone`).
  Type changes and retirements on the same screen are still recorded and applied later.
- **Ledger file:** `reviews/tag_links/<source_id>.json` holds `{kept: {node_id: {at, by}},
  drops: [{key, keys, name, item_id, at}]}`. `scripts/rebuild_tag_links.py` restores the graph
  mirror from it (review round 2).
- **Blocked list:** `policies/blocked_tags.txt`, one name per line with `#` comments, chosen so it
  is hand-editable and comment-preserving. **Re-suggest** is the printed command
  `scripts/extract_items.py --force --cache-only <source_id>…` (review round 2: cache-only, zero
  provider calls), because the API has no items-extraction job.
- **Suggestion counts:** a tag shared by two documents is two rows, because the decisions are
  per document.
- **Legacy compatibility:**
  - An item activated before ADR-0066 (no human mention in any status) is not demoted to `unused`
    by a recompute.
  - The flat-queue `promote_candidate_node` executor still works for approved items.
  - A never-kept candidate that re-extraction stops suggesting keeps the ADR-0057 tombstone plus
    `deprecate_wiki_page` behaviour (the "Retired by re-extraction" section).
- **Merge:** a rejected `mentions` collision no longer blocks a merge. A kept (human) link
  resurrects the dropped one (keep wins); a suggestion moving onto a dropped link is collapsed
  (the drop stands).
- **Split:** B's promotion accounting (`spinoff_promote_slot_taken`, "promotion accounted for") is
  retired.
- **Not in part 1:**
  - ~~The Source page Items section still lists suggestions as well as kept tags~~ — fixed in
    review round 6 (kept-only projections). Hiding candidate/unused tags in Obsidian and the
    index is still Slice B.
  - ~~Lint's `under_supported_item` still runs until part 4~~ — retired in review round 2
    (decision 9 brought forward).

## Review round 2 (2026-10-25)

An external code review of the part 1 implementation, backed by in-memory probes: six blocking
issues, four non-blocking. The user accepted every recommendation ("apply all"), including bringing
decision 9 forward.

| # | Issue | Resolution |
|---|---|---|
| B1 | A damaged ledger loaded as empty and could be overwritten; unlocked read-modify-write lost a concurrent keep | `LedgerCorrupt` on any unparseable or mis-shaped ledger; every action returns `ledger_corrupt` without writing, extraction skips the document with an error, merge/split skip `tag_ledger_corrupt`, the screen shows a banner. All ledger and blocked-list updates run under `tag_links.ledger_lock` (process lock + `flock` on `reviews/tag_links/.lock`). Every action writes the ledger before its graph mirror |
| B2 | A merge restored a dropped suggestion when evidence spans differed (and, the mirror case, kept the survivor's own suggestion where the absorbed tag was dropped) | `on_merge` now applies per-document precedence independently of anchors: on a document that dropped the survivor (directly or via the moved drop record) and does not keep it, every `llm` link to the survivor is rejected, before the survivor renders |
| B3 | Renaming a tag defeated drop persistence | A drop record stores the tag id plus every name it answered to (`keys`: title + aliases); extraction filters a name whose key matches or whose derived id is a dropped id (`tag_links.drop_filter`) |
| B4 | Immediate actions left the keyword index stale | `finish` also runs the incremental `keyword_index.reindex` and returns `{index_rebuilt, keyword_reindexed, warnings}`; the decide, undo, Delete unused, JSON and human-add paths surface a failure (banner / `warnings`) |
| B5 | Re-suggest could call the provider; documents extracted while blocked were not found | `LLMClient.parse(cache_only=True)` raises `CacheMiss` before any adapter call; `extract_items(cache_only=True)` / `--cache-only` replays under the artifact's recorded chain member, needs no key, and reports `cache_miss_sources` (left untouched). The items artifact gains `filtered: [{name, key, reason}]`, which `suggesting_sources` reads |
| B6 | Undecided suggestions fed synthesis | Synthesis eligibility uses `kept_sources_for_node`, and the ADR-0062 item-type ranking bridge (`graph_read.source_item_types`) counts only `asserted_by = 'human'` links; no fallback for never-kept legacy actives (decision 10 restarts clean) |
| N1 | No recovery path | `tag_links.repair` + `scripts/rebuild_tag_links.py` (dry run, `--apply`); `tag_links.consistency_errors` + `scripts/validate_tag_links.py` (the 12th validator) |
| N2 | Amendment validation regressed | `promote.normalize_amendments` is the one rule set: the review route, the JSON tag-link route (400), the per-source form and `tag_links.keep` (`invalid_amendments`) |
| N3 | Single-source lint rule still ran | Decision 9 implemented: `under_supported_item` removed; lint reports `unused_tag` (low, report-only) and withdraws still-pending items the old rule filed (audit note `rule_retired`); CLAUDE.md and the maintenance skill updated |
| N4 | Trailing blank line | Fixed |

Choices made while implementing:
- **Lock granularity:** one lock for all ledgers, not one per document. It serializes the rare
  cross-document operations (merge, repair, block) with per-document actions without nesting
  locks; a single-user vault sees no contention. Re-entrant per thread.
- **Repair direction:** the ledger is the authority, so a human link with no keep is rejected, not
  added to the ledger. A keep naming a gone or merged tag is reported (`invalid_keeps`), not
  repaired.
- **Contradiction blocking unchanged:** `item_ids_for_source` (contradiction candidate blocking)
  still reads every active link. It is a recall heuristic whose every output is human-reviewed,
  and claims are not linked to tags.
- `reviews/tag_links/` is git-ignored like the rest of the review ledger (it was missing in part 1).

Tests (`tests/test_tag_links.py`, `test_lint.py`, `test_synthesis.py`,
`test_item_type_faceting.py`): concurrent keeps on one document; damaged ledgers (unparseable and
mis-shaped) refused and never overwritten, including extraction and human-add; a damaged ledger
blocks a merge; an interrupted drop detected by the validator and repaired; merge with a dropped tag
at a different evidence span, both directions; amend → keep → drop → re-extract under the original
and the new name; keep/drop/delete keep the keyword index consistent; a failed refresh is shown; unblock
finds a document extracted while blocked; cache-only replay and miss make zero provider calls;
amendment validation through both HTTP routes; an undecided suggestion contributes no synthesis
evidence; the retired lint rule files nothing and its pending items are withdrawn idempotently.

## Review round 3 (2026-10-25)

A follow-up review of round 2: three blocking gaps in recovery and serialization, confirmed by
probes. The user accepted every recommendation ("apply all").

| # | Issue | Resolution |
|---|---|---|
| B1 | Repair rejected the kept links of a document whose ledger was damaged (it read as "no keeps"); `{}` passed as an empty ledger | `_plan_repair` excludes damaged documents from every mutation (`skipped_damaged`). A ledger must carry `source_id`, `kept` and `drops`, and each keep an `at`; drop records without `keys` (part 1) stay valid |
| B2 | An interrupted first keep (ledger written, candidate not activated) was `invalid_keeps`; its amendments were lost; an interrupted revival went unnoticed | A keep of a candidate records its normalized `amendments` in the ledger entry. A kept tag still `candidate` is an `interrupted_activation` (repair re-runs `promote.activate_node` with those amendments; a still-blocked gate is reported, never guessed); a kept tag still `unused` is an `interrupted_revival`. The validator reports both |
| B3 | Extraction wrote suggestions outside the lock from decisions read before the model call; merges and splits changed graph links before their locked ledger fixups | Extraction re-reads the document's drop records and the blocked list **under the lock** after the model call and writes that document's links + artifact before releasing it (the final recompose also runs under it). `apply_merges` / `apply_splits` hold the lock for each whole operation and run **ledger first**: journal → `move_merge_decisions` / `on_split` → graph re-point → `enforce_drops` → render |

**Resumable identity operations:** `app/workers/identity_journal.py` writes
`reviews/in_flight/<review_id>.json` (`{review_id, op, nodes}`) before a merge's or split's first write
and removes it after the last. While it exists:
- the next apply **resumes** the operation: a merge adopts its own tombstone and finishes the tail
  (drop enforcement, survivor render, Source-page re-render of every linked document); a split adopts
  its spin-off slot without requiring the lineage page, moves what is still on the primary and renders;
- repair **defers** the operation's tags (every tag, if an entry is unreadable) and the validator says
  to run apply first, because repairing a half-done merge from the ledgers would partly undo it.

Every step after the journal entry is idempotent, so a crash at any point leaves a state the next
apply completes. `reviews/in_flight/` is git-ignored and backed up with `reviews/`.

Tests (`tests/test_tag_links.py`): repair leaves a damaged document's links untouched while repairing
the others; `{}`, missing `source_id` and a keep without `at` are rejected; an interrupted first keep
of an unclassified suggestion is finished with the chosen type and title; an interrupted revival is
finished; a drop and a block made during the model call win over that extraction; a merge interrupted
at the re-point and at the survivor render is deferred by repair and resumed by apply (then a true
no-op); a split interrupted mid-re-point is resumed; a keep issued during a merge waits for the whole
merge.

## Review round 4 (2026-10-25)

A follow-up review of round 3: two blocking gaps in the in-flight handling. The user accepted every
recommendation ("apply all").

| # | Issue | Resolution |
|---|---|---|
| B1 | Normal actions could change the tags of an unfinished merge/split: a merge moved B's keep to A in the ledger and stopped; the screen still showed B kept; a drop of B succeeded; the resume restored the keep on A and the drop was lost | **Refuse, never reconcile** (a decision made against a half-finished screen can't be ranked against the operation's): keep, drop, undo-drop and human-add on a tag the journal lists return `identity_operation_in_flight` (with the `review_id`); block refuses when the name matches such a tag (nothing written, not even the list entry); Delete unused skips it; the JSON route answers 409; the document screen shows an "unfinished merge/split — run Apply" banner. Extraction skips a document linked (in any status) to such a tag, before the model call (no cost) and again under the lock after it (also catching an operation that started during the call), reporting `in_flight_sources` |
| B2 | The executors removed their marker before the caller re-rendered Source pages and indexes, so a crash in between left stale projections with no marker | The executors mark the entry `graph_done` and return `journaled`; `run_apply` closes it only after the Source-page re-render, the `wiki/index.md` rebuild and the keyword reindex succeeded. On a refresh failure the marker stays (`identity_refresh_pending`, a non-clean apply) and the next apply resumes the idempotent operation — a resumed merge re-renders every document linked to either tag — and refreshes again |

Trade-off accepted: while a refresh keeps failing, actions on those tags stay refused — visible, and
cleared by the next successful apply.

Tests (`tests/test_tag_links.py`): a drop through the form and the JSON route on an unfinished merge is
refused and the ledger is unchanged, and after the resuming apply it works; human-add and block are
refused; Delete unused skips an unfinished survivor; extraction skips the linked documents before the
model call and re-checks under the lock when an operation starts during the call; human-add, drop and
extraction against an unfinished split; an apply interrupted before the Source-page refresh and one
whose keyword reindex fails both keep the marker, and the next apply finishes, re-renders and closes it.
Executor-level tests play the caller with `tests/tagging.close_journals`.

## Review round 5 (2026-10-25)

| # | Issue | Resolution |
|---|---|---|
| B1 | `generate_wiki` reports a per-page render failure in `error_details` instead of raising; apply ignored it and closed the in-flight markers, leaving a stale Source page with no recovery marker | `run_apply` reads the result: any failed page adds `source_page_refresh_failed` (a warning for every executor), and a failed page among the merge/split affected documents keeps ALL of this apply's markers (`identity_refresh_pending`, non-clean). The same check in `tag_links.finish` adds `source_page_refresh_failed:<source_id>` to the immediate actions' warnings (form banner, JSON `warnings`) |

Tests: one Source page's renderer fails inside `generate_wiki` during apply → the marker survives and
the next apply re-renders the page and closes it; the same failure after a keep reaches the form and
the JSON route.

## Review round 6 (2026-10-26)

External review of part 1 at `705d029`: four blocking issues, five non-blocking. The user approved the
fixes and asked to take them one step at a time; this round covers blocking 1–2 and the small
non-blocking fixes. Blocking 3 (backup snapshot consistency) and 4 (browser request protection)
followed on 2026-10-27: the ADR-0039 snapshot-consistency amendment and ADR-0067 (request-origin
guard).

| # | Issue | Resolution |
|---|---|---|
| B1 | Unconfirmed suggestions appear as document tags: Source pages and item backlinks projected every active `mentions` edge, so keeping *Topic* on A made B's unkept suggestion read as B's tag and as a Topic backlink | **Kept-only projections.** `graph.tag_projection` / `graph.source_tag_projection` split curated links (kept, or every active mention of a **legacy** tag — active with no human mention in any status, activated before ADR-0066 or by the flat-queue executor) from suggestions. Item pages link only keeping documents in **Mentioned By** and list the rest under **Suggested On** as plain text; Source pages link only kept tags (frontmatter `items:` too) and list the rest under **Suggested (not reviewed)** as plain text — no wikilink, so no Obsidian graph edge either. `validate_projection` checks the same split. A legacy tag's first keep re-renders every document that suggests it. Lifecycle (candidate/deprecated) still counts all active mentions |
| B2 | An interrupted drop could leave a tag `active` with no keeps (edge rejected, status refresh never ran); validation and repair saw nothing, because they only compared ledger and edges | **Repair covers derived state.** New categories in `tag_links.consistency_errors` / `repair` (and so `validate_tag_links.py` / `rebuild_tag_links.py`): `stale_status` (status vs the ledgers' keeps; legacy exempt), `unwithdrawn_blocks` (a blocked tag still suggested — a block stopped after the list write; repair withdraws, and deletes a never-kept candidate as the block would), `interrupted_deletions` (revised in round 7: resumed only from a recorded deletion intent), `stale_item_pages` / `stale_source_pages` (status or tag links on a page disagree with the graph — repair re-renders; the script then refreshes `index.md` and the keyword index). Tags linked from a damaged ledger and nodes of an in-flight merge/split (and their Source pages) are skipped |
| N1 | `safe_child` returned the resolved path without a containment check, so an in-directory symlink could point outside the base | The resolved path must stay under `base` |
| N2 | The Unused tags list showed no type: `nodes_of_type` did not select `item_type` | Selected |
| N3 | Build Specification drift: independent-source auto-promotion, "all ten types" | §11 promotion row → keep-only (ADR-0066); §6.1 note → the six ADR-0059 node types; snapshot pipeline gains the tagging step; `docs/Workflow.md` ground rule fixed |
| N4 | Graph budgets bound results, not traversal work (high-degree frontier expansion) | Deferred — not a problem at the current vault size; revisit with real growth |
| N5 | Recovery complexity | Acknowledged; B2 is the first consolidation: every action's derived state (status, pages, blocked withdrawals, deletions) is now re-derivable from the ledgers |

Tests: an unkept suggestion never shows as a Source-page tag or an item backlink; a legacy tag links
its documents until its first keep; interrupted last drop → `unused` after repair; interrupted drop of
one of two keeps → pages re-rendered; interrupted block → suggestions withdrawn and the never-kept tag
deleted; interrupted deletion finished; the Unused tags list carries the type; `safe_child` rejects an
escaping symlink.

## Review round 7 (2026-10-27)

Follow-up review of round 6: blocking 1–2 verified fixed; one new blocking issue and one gap, both
introduced by the round 6 recovery code.

| # | Issue | Resolution |
|---|---|---|
| B1 | Repair assumed deletion approval: an unused tag (or a never-kept candidate) whose page was missing counted as an "interrupted deletion", and repair deleted its node — without a human Delete unused, and bypassing the merge-target/synthesis protections. A missing file can mean corruption or an incomplete restore | **A durable deletion-intent record.** `delete_tag` writes `reviews/tag_links/deleting/<item_id>.json` (`{node_id, reason, slug, title, at}`) before its first change and removes it after its last; it is reached only after the human action's own checks (Delete unused, a drop of a never-kept tag, a block). Repair resumes **only** a recorded deletion, and first re-checks `deletion_blocker` (kept by any ledger, merge-redirect target, owns a synthesis, still live): if the tag is no longer deletable the record is dropped and nothing is deleted (`cancelled_deletions`); if the node is already gone only the tail runs (withdraw review items, audit). A live tag with no page and no record is **`missing_pages`**: report-only (restore from backup — the page holds the title and aliases), and the script exits non-zero. Tags linked from a damaged ledger are skipped |
| G1 | `rebuild_tag_links.py` refreshed `index.md` and the keyword index only when Source pages were affected, so a repair touching only tag pages (an unused tag with no documents) left them stale | `repair` returns `pages_changed` (a tag page re-rendered or deleted); the script calls `finish(..., pages_changed=...)` whenever either is set |

Tests: a hand-deleted page is reported and nothing is deleted; an interrupted Delete unused resumes
from its record; a record whose tag was kept meanwhile is dropped without deleting; a repair that
re-renders only an unused tag's page still refreshes the indexes.

## Implementation — part 2 (2026-10-27)

Decisions 7–8 as resolved by P1–P7.

**Code:**
- `app/llm/prompts.py`: the items prompt asks for one centrality-ranked list of at most
  `ITEMS_PROMPT_MAX = 20`, 1–3 broad domains first, the naming style and the strict alias rule.
  `ITEMS_PROMPT_VERSION` is now `enrich-items-prompt-v4` (the schema is unchanged).
- `app/workers/items.py`:
  - `plan_suggestions` runs the P6 pipeline before any write;
  - `reference_cut` is P4;
  - `_resolution_index` reads active/unused tags' titles and aliases from their pages;
  - `_rejected_merge_pairs` reads rejected `merge_items`.
  - `extract_items(cap=…)` takes the setting. The artifact gains per-node `resolution`
    (`new`/`existing`/`exact`/`resolved`), `aliases_filtered` and `ambiguous_with`, plus top-level
    `cap` and `reference_cut_at`. `filtered[]` gains `over_cap`, and a row filtered through its
    resolved tag carries `resolved_to`/`resolved_title`.
  - New job counters: `suggestion_cap`, `suggestions_over_cap`, `suggestions_resolved`,
    `suggestions_ambiguous`, `aliases_filtered`, `no_domain`/`no_domain_sources`.
- `app/workers/merge_candidates.py`: `fold_text`/`occurs_in`, the same fold as `merge_key` applied
  to running text with every token singularised.
- `app/workers/tag_links.py`:
  - `use_existing` is **Use X**: keep X, then drop the suggestion, then a `tag-resolved` audit
    entry. The keep runs first because a keep clears drop records matching the kept tag's names.
  - `suggesting_sources` also finds a row blocked through its resolved tag.
- **Screen:**
  - `review_read` reads `ambiguous_with` from the items artifact. It shows only choices that are
    still active/unused, with their type and keep count.
  - `review_html` renders **use *X*** / **keep as new tag**, and the one-alias-per-line textarea
    prefilled with the current aliases.
  - `main.py` accepts `use:<itm_id>` on the form and `{action: "use", chosen}` on the JSON route,
    checked server-side against the row's matches (otherwise `not_an_ambiguous_match`). An
    unchanged alias field records no amendment.
- **Setting:** `ENRICH_ITEMS_CAP` (default 15, `Settings.enrich_items_cap`). `scripts/extract_items.py`
  passes it and prints the new counters.

Implementation choices not spelled out above:
- **Resolved suggestions add nothing to the tag:** a resolved suggestion's aliases are not unioned
  into the tag it resolved to; only merges teach aliases (decision 8). (Exact-name suggestions of a
  live tag first kept the additive union; review round 8 below stopped that.)
- **A live tag keeps its slug and status on re-extraction.** An exact-name suggestion of an
  active/unused tag no longer rewrites the graph slug from the extracted name. Before this fix, a
  tag renamed by a keep amendment could get a second page under its original slug. Its status is
  also no longer briefly set to `candidate`, so a later document in the same run still resolves to
  it.
- **Domains** are judged by the model's suggested type; the cut counts each emitted non-kept row,
  ambiguous ones included.
- **Frontmatter lists:** the page parsers (`wiki_render._parse_value`, `rebuild_index.parse_value`)
  now keep a comma inside a double-quoted list item. Before, an alias such as "Bias, Variance" was
  split into two on the next read, which P5 relies on not happening.

**Tests:** `tests/test_tag_suggestions.py` (new, 20 tests): the reference cut, the fold, the
alias check, domains first, the cap (kept tags count, cache-only re-cut, no restale), resolution
(title/alias, candidates excluded, exact wins), ambiguity and Use X (form and JSON), rejected
merge pairs, drop/block through the resolved tag, the resolved type conflict, and the prefilled
alias field, and the renamed-tag slug fix. The items, prompt-encoding and source-flow tests were
updated to the new contract.

**Rollout (P7):** existing artifacts are stale under the prompt bump, so the next
`extract_items.py` run re-extracts and bills every document. Kept links and drops survive.

## Review round 8 (2026-10-27) — the user's decisions outrank re-extraction

Architecture review of the uncommitted part 2; all answers user-approved.

**Blocking 1 — aliases are the user's after the first keep.** Extraction unioned the body-checked
aliases of an exact-name suggestion into the tag's page, so an alias the user deleted on keep came
back with the next document that used it.
- **Now:** extraction adds aliases only to a tag nobody has kept yet (a candidate). It adds nothing
  to an active, unused, hidden, merged or retired tag, and proposes nothing (no review item). The
  document's artifact still records what the model proposed, as evidence only.
- A deleted alias therefore cannot come back, and cannot redirect a later suggestion: matching is
  built from the pages.
- **Unchanged:** a merge still unions the absorbed tag's names into the survivor (ADR-0050) — that is
  a human approval.
- **Follow-up (same day):** the first version checked ownership when extraction buffered a
  suggestion, but the pages are written at the end of the run. A keep landing in between (the
  user editing while a run is going), or a first keep that stopped after its ledger write (the tag
  still `candidate`), got the buffered aliases unioned in — repair then activated the tag with them.
  **Ownership is now decided at the final page write, under the ledger lock** every keep takes:
  extraction adds buffered aliases only to a tag that is still `candidate` **and kept by no
  document's ledger** (`tag_links.kept_anywhere`; the ledger keep is written first, so it is durable
  first-keep evidence, an interrupted keep included). The aliases on the page at that moment are the
  confirmed list. A damaged ledger may hold a keep, so while one exists no buffered alias is added to
  any candidate (`aliases_withheld_ledger_damaged`, printed by `extract_items.py`).

**Blocking 2 — extraction undid lifecycle decisions.** Extraction rewrote any existing tag that was
not active/unused to `candidate` (the graph upsert in `_emit` and the page status in
`_recompose_node`); repair then read "a ledger keeps a candidate" as an interrupted keep and
activated it. That turned *hidden → candidate → active*, and the same path undid approved
deprecations and merge tombstones. (The bug predates part 2.)
- **Decided statuses** (`items.decided_status`): `hidden`; `merged`; `archive_candidate`,
  `archived`, `delete_candidate`, `deleted`; and `deprecated_candidate` **only** when the page's
  `review_status` is `approved`. The automatic no-mentions tombstone (`deprecated_candidate`,
  pending) and `stale_candidate` may still revive (ADR-0057).
- **A suggestion naming a decided tag** — by its own id first, then (when no live tag answers to the
  name) by the decided tags' titles and aliases:
  - hidden → filtered silently, counted (`filtered[]` reason `hidden`,
    `suggestions_filtered_hidden`);
  - approved deprecation / archive / delete → filtered (`retired`, `suggestions_filtered_retired`);
  - merged → follows `merged_into` (up to 8 hops) to the survivor, **independently of the
    survivor's current aliases** — removing an alias stops ordinary matching, it does not revoke the
    merge (`resolution: redirected`, `suggestions_redirected`). A hidden survivor filters the
    suggestion; a retired or missing one counts as `retired`.
  - Several decided tags behind one name: hidden wins, then a single survivor; several survivors
    leave the suggestion ambiguous.
- `_recompose_node` never re-derives a decided status: a merge tombstone is left as it is, a hidden or
  retired tag re-renders at its own status and review_status (its suggestion list may change).
- **Repair** leaves the keeps of a hidden tag alone (they are kept for unhide) instead of reporting
  them as invalid keeps; it never treats a decided tag as an interrupted keep.

**Non-blocking — an interrupted "use X" stays visibly pending.**
- `use_existing` writes a pending record `reviews/tag_links/resolving/<source>/<suggestion>.json`
  (`{source_id, node_id, name, chosen, chosen_title, chosen_was_kept, op_id, at}`) before its first
  change and removes it after its last: record → keep X → drop the suggestion → `tag-resolved` audit
  (carrying `op_id`) → remove the record.
- **Retry** (the same choice again) skips every step already done: the keep when X is already kept
  here, the drop when the suggestion's drop record exists or the suggestion was deleted, the audit
  when one with that `op_id` exists. Nothing is decided or audited twice.
- **Shown** on the document screen: on the suggestion row while it is still there ("unfinished:
  use X started …"), otherwise in an **Unfinished choices** list with *finish* / *cancel*. The JSON
  route takes `{action: "cancel"}`, and `use` accepts the pending choice as a retry.
- **Changing the decision** — another option on that row, a different *use Y*, a drop of X, or
  *cancel* — cancels it (`tag_links.cancel_resolutions`, audited `tag-resolve-cancelled` with
  `retained_keep`). What already happened stays: if the operation kept X here, X stays an ordinary
  kept tag and the result says so, so the user can drop it explicitly.
- **Repair** reports pending records (`pending_resolutions`, report-only; `rebuild_tag_links.py`
  prints them) and never resumes them. They are not validation errors: each primitive is complete.
  (Repair still completes a keep that itself stopped half-way — that is the keep's own recovery,
  not the use-X operation.)

**Non-blocking — the vocabulary scan.** `_resolution_index` reads every live and decided tag's page
once per document, under the ledger lock. Measured on a synthetic vault (WSL2, warm page cache):

| tags (live + decided) | per document (lock held) | over 600 documents |
|---|---|---|
| 500 + 50 | 12 ms | 7 s |
| 2,000 + 200 | 51 ms | 31 s |
| 5,000 + 500 | 135 ms | 81 s |

At the expected scale (hundreds to ~2,000 tags) a UI action waits at most ~50 ms, and the scan is a
small fraction of a billed extraction (one model call per document). No cache now; if the
vocabulary passes ~3,000 tags, cache the index per job and invalidate it with a counter that every
tag action bumps.

**Tests:** `tests/test_tag_authority.py` (14): the alias freeze (re-extraction while active and
while unused; no review item filed; a deleted alias no longer matches; a keep landing between the
buffering and the final write; an interrupted first keep, then repair; a damaged ledger withholds); a hidden tag through
extraction (own name and a plural variant) and repair; an approved deprecation through extraction;
the merge redirect without the survivor's alias, and a hidden survivor; "use X" interrupted after
each durable step (record, keep, drop, audit) — pending on the screen, reported and not resumed by
repair, finished by the retry with exactly one `tag-resolved`, `tag-kept` and `tag-dropped`; a
different decision cancels it and keeps X visibly; cancel after the suggestion is gone; a comma
inside an alias round-trips through both frontmatter parsers. The decided-status and alias-ownership
tests fail without their fixes.

## UAT round 8 fixes (2026-10-28)

The part-2 UAT (9 documents, one forced re-extraction under prompt v4) found no resolution bug: a
suggestion that looked new ("Agentic AI") linked to an existing tag of that exact title, which wins
over an alias match. It did show three overlapping tags (AI, AI Agent, Agentic AI) sharing the name
"agentic AI" through extraction aliases accepted unedited at their first keep, and with no way to
correct them afterwards. The user decided: aliases may include acronyms and the prompt stays as is;
AI Agent and Agentic AI stay distinct tags; AI goes entirely. Fixes:

1. **No merge proposal involves a blocked tag.** `merge_candidates.find_merge_candidates(blocked_keys=)`
   leaves out every tag whose title folds (merge key) onto a blocked name — on **either** side, not
   only as survivor: a blocked tag is one the user wants gone, and absorbing it would hand its names
   to the other tag as aliases (with AI blocked, "Agentic AI ← AI" would make "AI" an alias of
   Agentic AI). The lint passes `policies/blocked_tags.txt`. A merge the user files is still allowed;
   an already-filed proposal is left for the user to decide.
2. **A kept tag's aliases are editable.** After the first keep the aliases are the user's (round 8),
   so the document screen's **Kept tags** rows get an *edit aliases* box (one per line; replaces the
   tag's aliases everywhere) and the JSON route an `aliases` action. `tag_links.edit_aliases` (under
   the ledger lock): only an in-use tag that this document keeps, not during an unfinished merge/split,
   the shared amendment limits; it re-renders the page through `recompose_semantic_node_page(aliases=)`
   and writes a `tag-aliases-edited` audit record `{source_id, node_id, from, to}`. An unchanged box
   records nothing; a drop in the same submit wins. The alias freeze still holds — extraction never
   undoes the edit.
3. **Shared names are flagged.** Each tag row on the document screen notes every name (title or
   alias) that another live tag (candidate, active or unused) also carries, e.g. "“agentic AI” is
   also the title of “Agentic AI” (suggested, not kept yet)" — the shared key that makes the
   duplicate check propose a merge, shown while the aliases can still be fixed.
4. **The approved queue says what apply carried out.** Approved items stay in `reviews/approved/`
   after apply, which read as "not applied". The approved view adds an *applied* column for the
   executors that leave a per-review record (`merge_items` → `merged`, `change_item_type` →
   `retyped`, `split_item` → `split`): the time of the latest record at or after the approval, or
   **not yet**. Other types re-assert a status on every apply and leave no such record (—).
5. **A blocked human merge says why in plain words**, e.g. "'financial-reporting' is not in use yet
   (status: candidate) — keep it on a document first, then merge (survivor_not_active)"; the typed
   reason stays at the end for scripts.

Not changed: the type-change proposals for AI Agent and Agentic AI disagreed because the model typed
two different tags differently in two documents — each is decided on its own.

**Tests:** `tests/test_uat_round8.py` (10). The blocked-tag, collision and applied-marker tests fail
without their fixes.

## Review round 9 (2026-10-28) — the UAT round 8 fixes

External review of the uncommitted UAT round 8 fixes; the user approved the three fixes as proposed.

**Blocking — an untouched stale form could undo a newer alias edit.** Browsers submit every
*edit aliases* box, collapsed or not, and the route compared each box with the tag's **current**
aliases. A page opened before another document removed an alias therefore put it back on its next
submit, recorded as a deliberate `tag-aliases-edited` edit (reproduced: open A → remove
"Foundation Model" through B → submit A untouched → it returns).
- **Now:** each box carries a hidden `shown_aliases_<id>` (the list as displayed, JSON). A box equal
  to what it showed is untouched and records nothing, whatever the current aliases are.
- A changed box passes the shown list to `tag_links.edit_aliases(expected=)`, which compares it with
  the page's aliases **under the ledger lock** and refuses with `aliases_changed_since_shown` (the
  current list in the result; the screen says "reload and edit again") if they differ. Nothing is
  written.
- A form without a valid shown list (a page from before this change, or hand-built): a box that
  differs from the current aliases is refused with "this page is out of date"; an equal one is
  silent.
- The JSON route's `aliases` action takes an optional `expected_aliases` with the same check (409).
  Without it the call is an explicit replace — a script call is always deliberate.

**Non-blocking 1 — "not yet" forever after a re-approval.** Reopening and re-approving an applied
type change makes the older executor record predate the approval; apply then has nothing to do and
writes no new record. `review_read.applied_state` reads the live state when no record follows the
approval: a type change whose tag already has the target type (page `item_type`, as the executor
checks), or a merge whose absorbed tag this review already merged (`merged` is terminal — there is
no un-merge), shows **already in effect**. A split, or any type when the graph is unavailable, with
only an older record shows "applied ‹time›, before this approval — Apply re-checks it". **not yet**
now means apply has work to do.

**Non-blocking 2 — merge advice that couldn't be followed.** "Keep it on a document first" covered
every status that isn't in use. The advice now follows the status: a suggestion — keep it first; a
hidden tag — it must be unhidden first (an approved and applied `unhide_semantic_page` review —
there is no button for filing one yet, ADR-0047); an absorbed tag already merged — "already merged
into ‹survivor title› — nothing to do" (the survivor is read from the tombstone's `merged_into`);
retired statuses (deprecated/archive/delete candidates, archived, deleted) — can't be merged. A
merged survivor also names the tag it went into. The typed reason stays at the end.

**Tests:** `tests/test_uat_round8.py` (+6, 16 in all): the reviewer's stale-form sequence, a refused
edit on a stale page, a form without the shown list, the JSON `expected_aliases` check, "already in
effect" for a re-approved retype and merge (and "applied earlier" for a split / no graph), and
status-specific merge advice; the existing alias-edit tests now submit the hidden field as a browser
does. Mutation checks: comparing with the current aliases, dropping the primitive's check, removing
the in-effect check and restoring the generic advice each fail their tests.

## Review round 10 (2026-10-28) — Apply and tagging may overlap

External review of round 9; the user approved the recommendations as proposed.

**Blocking — a concurrent Apply could undo a successful alias edit.** The alias writer holds the
ledger lock, but the retype executor read the item page and wrote it back without it. Reproduced:
retype reads the aliases → an alias edit removes "Foundation Model" → the retype writes its earlier
copy, restoring it. **Decision: Apply may overlap tagging** (two tabs, a maintenance script) and the
code — not the user — keeps that safe.
- **Every item-page writer runs under the ledger lock**, from reading the page to writing it. Merges,
  splits, extraction and tag actions already did. Now also: `retypes.apply_retypes`,
  `deprecations.apply_approved_deprecations`, semantic-page hide/unhide,
  `duplicates.apply_marked_duplicates` and `promote.promote_candidates` (also run by
  `scripts/promote.py`). Each takes the lock itself; it is re-entrant, so inside Apply this is a no-op.
- **Lock order: the ledger lock first, then the graph.** `run_apply` takes the ledger lock before its
  first graph write and holds it through the graph executors, the commit and promotion, releasing it
  before the source-status executors, the index rebuild and the validators. Before, Apply wrote to the
  graph first and took the ledger lock only at the merge step, while a tag action takes the ledger
  lock and then writes to the graph: when the two overlapped, the tag action timed out on "database is
  locked" instead of waiting its turn. **Rule for any future writer of item pages or tag ledgers:
  take the ledger lock before the first graph write.**
- Claim, synthesis and Source-page writers are unchanged: tag actions don't edit those pages'
  human-owned fields (Source pages are re-rendered from the graph and manifests).

**Non-blocking — "already in effect" could overstate completion.** Round 9's check read the absorbed
node's status without the tombstone's destination or an unfinished journal, and fell back to the
graph when a retyped tag's page was missing (which Apply skips). The approved queue now uses the
**executors' own completion checks**, moved into shared functions so the two can't drift:
`retypes.retype_check` (skip / done / todo — the executor's guards and no-op test) and
`merges.merge_state` (todo / resuming / done / elsewhere / unverified — the executor's journal and
tombstone rules). Labels:
- **already in effect** — the check says done (a retype's page already has the target type; the
  absorbed tag is a tombstone pointing at this survivor with no open journal);
- **not yet — run Apply** — the check says there is work to do;
- **cannot verify — Apply re-checks it** — anything else: a missing page, a tombstone pointing
  elsewhere or naming no survivor, an interrupted operation, a split (no state check), or no graph;
  with the time of the last earlier record when there is one.

**Tests:** `tests/test_apply_tag_serialization.py` (new, 3): a retype Apply with an alias edit arriving
from another thread mid-operation (the edit waits; both changes survive); two processes editing the
same aliases from the same displayed list (one wins, the other is refused — the cross-process
`flock`); Apply holding the ledger lock at its first graph executor. `tests/test_uat_round8.py`: the
applied-column test now builds real tombstones and covers a missing page, a wrong merge destination,
an open journal, a tombstone without destination, a split and no graph (it replaces round 9's test,
whose merged tag had no tombstone). Mutation checks: removing the retype executor's lock, Apply's
lock, the `flock`, or the tombstone check each fail their tests; the cross-process test passed 10 of
10 repeated runs.
