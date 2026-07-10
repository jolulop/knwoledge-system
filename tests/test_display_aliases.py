"""ADR-0060 wiki display aliases: two-layer label contract, alias-shape validator, rot lint.

Covers the ADR's pinned test list: renderer frontmatter (`title:`/`aliases:`) per family,
aliased link emission through the shared `display_link_label` seam, the blocking
`validate_link_aliases.py` shape matrix (bare-with-label fails, aliased passes, whitespace
alias fails, no-label bare passes, dangling stays validate_wikilinks' contract, heading
links, index.md scanned), the report-only `display_alias_rot` lint (drift flagged; rendered-
label comparison so long-title caps never false-positive; retitle never fails validators),
hostile-label sanitisation, dependency-free local-copy parity, and byte-stable re-render.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.workers import labels, lint
from app.workers.wiki_render import (
    _claim_title,
    display_link_label,
    parse_frontmatter,
    render_claim_page,
    render_item_page,
    render_query_page,
    render_synthesis_page,
)


def _run_validator(script: str, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), str(root)],
                          capture_output=True, text=True)


def _page(tmp_path: Path, rel: str, text: str) -> Path:
    p = tmp_path / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _source_page(tmp_path: Path, sid: str, title: str) -> Path:
    return _page(tmp_path, f"Sources/{sid}.md",
                 f'---\ntype: source\nsource_id: "{sid}"\ntitle: "{title}"\n'
                 f'aliases: ["{title}"]\nstatus: active\n---\n\n# {title}\n\n'
                 "> [!summary] s\n> body.\n")


SID = "src_" + "a" * 16
CID = "clm_" + "b" * 16


# --- renderer frontmatter: the two-layer label contract ----------------------


def test_claim_page_gains_display_title_and_single_alias():
    text = render_claim_page({
        "claim_id": CID, "claim_text": "Solar deployment doubled in 2025. More detail follows.",
        "confidence": "low",
        "citations": [{"source_id": SID, "char_start": 0, "char_end": 10, "quote": "Solar depl"}],
    })
    fm = parse_frontmatter(text)
    # display-only projection: derived title + EXACTLY one alias (the derived title);
    # claim_text stays the untouched wording authority.
    assert fm["title"] == _claim_title("Solar deployment doubled in 2025. More detail follows.")
    assert fm["title"] == "Solar deployment doubled in 2025"  # terminator dropped by _claim_title
    assert fm["aliases"] == [fm["title"]]
    assert fm["claim_text"].startswith("Solar deployment doubled in 2025.")


def test_synthesis_and_query_pages_gain_full_title_alias():
    long_title = "How does retrieval-augmented generation change the economics of " \
                 "enterprise knowledge management across regulated industries?"  # > 78 chars
    syn = render_synthesis_page({
        "synthesis_id": "syn_" + "c" * 16, "title": long_title, "status": "candidate",
        "topic_node": "itm_" + "d" * 16, "summary": "s", "synthesis_text": "t",
        "claim_ids": [], "disagreements": [],
    })
    fm = parse_frontmatter(syn)
    assert fm["aliases"] == [long_title]          # FULL title in frontmatter — never truncated
    assert len(long_title) > 78
    qry = render_query_page({
        "query_id": "qry_" + "e" * 16, "question": "What changed?", "answer": "Nothing.",
        "citations": [], "retrieval_modes": ["keyword"],
    })
    qfm = parse_frontmatter(qry)
    assert qfm["aliases"] == [qfm["title"]]


def test_rendered_link_label_caps_what_frontmatter_keeps_full(tmp_path):
    long_title = "T" + "x" * 100                   # 101 chars, no sentence break
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\ntitle: "{long_title}"\n---\n\n# c\n')
    resolved = labels.display_labels(tmp_path / "wiki", [f"Claims/{CID}"])
    assert resolved[f"Claims/{CID}"] == long_title             # resolution keeps the full label
    syn = render_synthesis_page({
        "synthesis_id": "syn_" + "c" * 16, "title": "Topic", "status": "candidate",
        "topic_node": "itm_" + "d" * 16, "summary": "s", "synthesis_text": "t",
        "claim_ids": [CID], "disagreements": [],
    }, labels=resolved)
    capped = display_link_label(long_title)
    assert len(capped) == 78 and capped.endswith("…")
    assert f"[[Claims/{CID}|{capped}]]" in syn                 # link position: capped


# --- aliased link emission through the renderers ------------------------------


def test_claim_evidence_and_contradicts_links_alias(tmp_path):
    _source_page(tmp_path, SID, "Annual Solar Report")
    other = "clm_" + "f" * 16
    _page(tmp_path, f"Claims/{other}.md",
          f'---\ntype: claim\nclaim_id: "{other}"\nclaim_text: "Coal fell. Extra."\n---\n\n# c\n')
    resolved = labels.display_labels(
        tmp_path / "wiki", [f"Sources/{SID}", f"Claims/{other}"])
    text = render_claim_page({
        "claim_id": CID, "claim_text": "Solar rose.", "confidence": "low",
        "citations": [{"source_id": SID, "char_start": 0, "char_end": 5, "quote": "Solar"}],
        "contradicts": [other],
    }, labels=resolved)
    assert f"[[Sources/{SID}|Annual Solar Report]]" in text
    # claim label fallback: no title: on the partner page -> derived from claim_text
    assert f"[[Claims/{other}|Coal fell]]" in text


def test_synthesis_disagreement_links_alias(tmp_path):
    a, b = "clm_" + "1" * 16, "clm_" + "2" * 16
    for cid, t in ((a, "Alpha claim"), (b, "Beta claim")):
        _page(tmp_path, f"Claims/{cid}.md",
              f'---\ntype: claim\nclaim_id: "{cid}"\ntitle: "{t}"\n---\n\n# c\n')
    resolved = labels.display_labels(tmp_path / "wiki", [f"Claims/{a}", f"Claims/{b}"])
    syn = render_synthesis_page({
        "synthesis_id": "syn_" + "c" * 16, "title": "Topic", "status": "candidate",
        "topic_node": "itm_" + "d" * 16, "summary": "s", "synthesis_text": "t",
        "claim_ids": [a, b], "disagreements": [(a, b)],
    }, labels=resolved)
    assert f"[[Claims/{a}|Alpha claim]] contradicts [[Claims/{b}|Beta claim]]" in syn


def test_item_mentioned_by_links_alias(tmp_path):
    _source_page(tmp_path, SID, "Annual Solar Report")
    resolved = labels.display_labels(tmp_path / "wiki", [f"Sources/{SID}"])
    page = render_item_page({
        "node_id": "itm_" + "9" * 16, "item_type": "domain", "title": "Solar",
        "aliases": [], "confidence": "low", "source_ids": [SID], "status": "candidate",
    }, labels=resolved)
    assert f"[[Sources/{SID}|Annual Solar Report]]" in page


def test_query_citation_links_alias(tmp_path):
    _source_page(tmp_path, SID, "Annual Solar Report")
    resolved = labels.display_labels(tmp_path / "wiki", [f"Sources/{SID}"])
    qry = render_query_page({
        "query_id": "qry_" + "e" * 16, "question": "What rose?", "answer": "Solar rose.",
        "citations": [{"source_id": SID, "char_start": 0, "char_end": 5, "quote": "Solar",
                       "page": None}],
        "retrieval_modes": ["keyword"],
    }, labels=resolved)
    assert f"[[Sources/{SID}|Annual Solar Report]]" in qry


def test_render_is_byte_stable():
    kwargs = dict(labels={f"Sources/{SID}": "Report"})
    claim = {"claim_id": CID, "claim_text": "Solar rose.", "confidence": "low",
             "citations": [{"source_id": SID, "char_start": 0, "char_end": 5, "quote": "Solar"}]}
    assert render_claim_page(claim, **kwargs) == render_claim_page(claim, **kwargs)


# --- hostile labels (both positions, all families) ----------------------------


def test_hostile_labels_sanitised_in_link_position_and_frontmatter(tmp_path):
    hostile = 'Evil | [[Items/x]] [bracket]\nnewline "quote"'
    _page(tmp_path, f"Sources/{SID}.md",
          f'---\ntype: source\nsource_id: "{SID}"\ntitle: "{hostile}"\n---\n\n# s\n')
    resolved = labels.display_labels(tmp_path / "wiki", [f"Sources/{SID}"])
    page = render_item_page({
        "node_id": "itm_" + "9" * 16, "item_type": "domain", "title": "Solar",
        "aliases": [], "confidence": "low", "source_ids": [SID], "status": "candidate",
    }, labels=resolved)
    line = next(ln for ln in page.splitlines() if ln.startswith(f"- [[Sources/{SID}|"))
    label = line.split("|", 1)[1].rsplit("]]", 1)[0]
    assert "[" not in label and "]" not in label and "|" not in label and "\n" not in label
    assert line.count("[[") == 1 and line.endswith("]]")       # no link injection
    # claim title derived from hostile claim_text round-trips through frontmatter
    hostile_claim = render_claim_page({
        "claim_id": CID, "claim_text": 'Pipes | and [[links]] and "quotes" everywhere.',
        "confidence": "low", "citations": []})
    fm = parse_frontmatter(hostile_claim)
    assert fm["title"]                                          # parseable, non-empty
    assert "[[" not in str(fm["aliases"])


# --- validate_link_aliases.py: the shape matrix -------------------------------


def test_validator_bare_link_fails_when_label_resolvable(tmp_path):
    _source_page(tmp_path, SID, "Annual Solar Report")
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\ntitle: "T"\n---\n\n# c\n\n[[Sources/{SID}]]\n')
    proc = _run_validator("validate_link_aliases.py", tmp_path)
    assert proc.returncode == 1 and "missing display alias" in proc.stdout


def test_validator_aliased_and_heading_links_pass(tmp_path):
    _source_page(tmp_path, SID, "Annual Solar Report")
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\ntitle: "T"\n---\n\n# c\n\n'
          f"[[Sources/{SID}|Annual Solar Report]]\n[[Sources/{SID}#Quote|Readable source]]\n")
    proc = _run_validator("validate_link_aliases.py", tmp_path)
    assert proc.returncode == 0, proc.stdout


def test_validator_whitespace_alias_fails(tmp_path):
    _source_page(tmp_path, SID, "Annual Solar Report")
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\ntitle: "T"\n---\n\n# c\n\n[[Sources/{SID}|   ]]\n')
    proc = _run_validator("validate_link_aliases.py", tmp_path)
    assert proc.returncode == 1 and "blank display alias" in proc.stdout


def test_validator_bare_link_passes_when_no_label_resolves(tmp_path):
    _page(tmp_path, f"Sources/{SID}.md", "# untitled page, no frontmatter\n")
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\ntitle: "T"\n---\n\n# c\n\n[[Sources/{SID}]]\n')
    proc = _run_validator("validate_link_aliases.py", tmp_path)
    assert proc.returncode == 0, proc.stdout


def test_validator_dangling_target_is_not_its_contract(tmp_path):
    # Missing target -> validate_wikilinks' dangling failure, never double-reported here.
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\ntitle: "T"\n---\n\n# c\n\n[[Sources/{SID}]]\n')
    assert _run_validator("validate_link_aliases.py", tmp_path).returncode == 0
    assert _run_validator("validate_wikilinks.py", tmp_path).returncode == 1


def test_validator_scans_index_md(tmp_path):
    _source_page(tmp_path, SID, "Annual Solar Report")
    _page(tmp_path, "index.md", f"# Index\n\n- [[Sources/{SID}]] · active\n")
    proc = _run_validator("validate_link_aliases.py", tmp_path)
    assert proc.returncode == 1 and "index.md" in proc.stdout


def test_validator_ignores_code_fences_and_claim_fallback_labels(tmp_path):
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\nclaim_text: "Coal fell. Extra."\n---\n\n# c\n')
    # bare link to that claim: label resolvable via claim_text derivation -> fails
    other = "clm_" + "f" * 16
    _page(tmp_path, f"Claims/{other}.md",
          f'---\ntype: claim\nclaim_id: "{other}"\ntitle: "T"\n---\n\n# c\n\n[[Claims/{CID}]]\n')
    assert _run_validator("validate_link_aliases.py", tmp_path).returncode == 1
    # the same bare link inside a fenced code block is ignored
    _page(tmp_path, f"Claims/{other}.md",
          f'---\ntype: claim\nclaim_id: "{other}"\ntitle: "T"\n---\n\n# c\n\n'
          f"```\n[[Claims/{CID}]]\n```\n")
    assert _run_validator("validate_link_aliases.py", tmp_path).returncode == 0


# --- display_alias_rot lint ----------------------------------------------------


def _lint_findings(tmp_path):
    report = lint.run_lint(tmp_path, record_job=False, file_review_items=False)
    return [f for f in report["findings"] if f["check"] == "display_alias_rot"]


def test_alias_drift_is_lint_only_and_validators_stay_green(tmp_path):
    _source_page(tmp_path, SID, "New Title After Retitle")
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\ntitle: "T"\n---\n\n# c\n\n'
          f"[[Sources/{SID}|Old Stale Title]]\n")
    # shape validator: alias present -> passes; drift is lint's business
    assert _run_validator("validate_link_aliases.py", tmp_path).returncode == 0
    findings = _lint_findings(tmp_path)
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "low"
    assert f["data"]["target"] == f"Sources/{SID}"
    assert f["data"]["current_label"] == "New Title After Retitle"
    assert f["data"]["remediation"] == "rerun_render_chain"


def test_capped_alias_on_long_title_is_not_rot(tmp_path):
    long_title = "T" + "x" * 100
    _page(tmp_path, f"Sources/{SID}.md",
          f'---\ntype: source\nsource_id: "{SID}"\ntitle: "{long_title}"\n---\n\n# s\n')
    capped = display_link_label(long_title)
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\ntitle: "T"\n---\n\n# c\n\n'
          f"[[Sources/{SID}|{capped}]]\n")
    assert _lint_findings(tmp_path) == []      # rendered-label comparison: capped == current


def test_current_alias_is_not_rot(tmp_path):
    _source_page(tmp_path, SID, "Annual Solar Report")
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\ntitle: "T"\n---\n\n# c\n\n'
          f"[[Sources/{SID}|Annual Solar Report]]\n")
    assert _lint_findings(tmp_path) == []


# --- dependency-free local-copy parity ------------------------------------------


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""),
                                                  ROOT / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rebuild_index_label_helper_parity():
    rebuild_index = _load_script("rebuild_index.py")
    cases = ["Plain Title", "T" + "x" * 100, "Pipes | and [[links]]", "  spaced\t\ntitle  ",
             "[bracket] mix"]
    for case in cases:
        assert rebuild_index.display_link_label(case) == display_link_label(case)


def test_validator_claim_title_parity():
    validator = _load_script("validate_link_aliases.py")
    cases = ["One sentence. Two.", "No terminator here", "X" * 200, "  padded .  "]
    for case in cases:
        assert validator._claim_title(case) == _claim_title(case)


# --- review round 1: validator backstops, scope, parser parity -------------------


def test_validate_frontmatter_requires_claim_title_and_aliases(tmp_path):
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\nstatus: active\nreview_status: none\n'
          "confidence: low\n---\n\n> [!summary] s\n> c.\n")
    proc = _run_validator("validate_frontmatter.py", tmp_path)
    assert proc.returncode == 1
    assert "missing required frontmatter field `title`" in proc.stdout
    assert "missing required frontmatter field `aliases`" in proc.stdout


def test_validate_frontmatter_requires_aliases_on_all_id_titled_families(tmp_path):
    _page(tmp_path, f"Sources/{SID}.md",
          f'---\ntype: source\nsource_id: "{SID}"\ntitle: "T"\nrelative_raw_path: "raw/x"\n'
          'normalized_path: "normalized/x"\nsha256: "d"\nfile_type: "md"\nstatus: active\n'
          'ingestion_status: "extracted"\nsummary_status: stub\ngeneration_status: deterministic\n'
          'input_fingerprint: "f"\n---\n\n> [!summary] Extractive excerpt\n> s.\n')
    _page(tmp_path, "Synthesis/syn_" + "c" * 16 + ".md",
          '---\ntype: synthesis\nsynthesis_id: "syn_' + "c" * 16 + '"\ntitle: "T"\n'
          "status: candidate\nreview_status: pending\n---\n\n> [!summary] s\n> x.\n")
    _page(tmp_path, "Queries/qry_" + "e" * 16 + ".md",
          '---\ntype: query\nquery_id: "qry_' + "e" * 16 + '"\ntitle: "T"\nquestion: "Q?"\n'
          "status: active\nreview_status: none\n---\n\n> [!summary] s\n> x.\n")
    proc = _run_validator("validate_frontmatter.py", tmp_path)
    assert proc.returncode == 1
    assert proc.stdout.count("missing required frontmatter field `aliases`") == 3


def test_rendered_pages_pass_frontmatter_backstop(tmp_path):
    # The renderers' output satisfies the new required keys (backstop pins the contract,
    # never fights the renderer).
    _page(tmp_path, f"Claims/{CID}.md", render_claim_page({
        "claim_id": CID, "claim_text": "Solar rose.", "confidence": "low",
        "citations": [{"source_id": SID, "char_start": 0, "char_end": 5, "quote": "Solar"}]}))
    _page(tmp_path, "Synthesis/syn_" + "c" * 16 + ".md", render_synthesis_page({
        "synthesis_id": "syn_" + "c" * 16, "title": "Topic", "status": "candidate",
        "topic_node": "itm_" + "d" * 16, "summary": "s", "synthesis_text": "t",
        "claim_ids": [], "disagreements": []}))
    _page(tmp_path, "Queries/qry_" + "e" * 16 + ".md", render_query_page({
        "query_id": "qry_" + "e" * 16, "question": "Q?", "answer": "A.",
        "citations": [], "retrieval_modes": ["keyword"]}))
    proc = _run_validator("validate_frontmatter.py", tmp_path)
    assert proc.returncode == 0, proc.stdout


def test_source_template_pins_full_title_alias():
    template = (ROOT / "templates" / "source.md").read_text(encoding="utf-8")
    assert 'aliases: ["{{title}}"]' in template     # full title, never truncated (ADR-0060 2a)


def test_validate_link_aliases_does_not_scan_tags(tmp_path):
    _source_page(tmp_path, SID, "Annual Solar Report")
    # a bare link with a resolvable label — but on a Tag page, which is out of ADR-0060 scope
    _page(tmp_path, "Tags/topic.md",
          f'---\ntype: tag\ntitle: "Topic"\n---\n\n# t\n\n[[Sources/{SID}]]\n')
    assert _run_validator("validate_link_aliases.py", tmp_path).returncode == 0
    report = lint.run_lint(tmp_path, record_job=False, file_review_items=False)
    assert not any(f["check"] == "display_alias_rot" for f in report["findings"])


def test_validator_label_resolution_parity_quoted_and_bare(tmp_path):
    validator = _load_script("validate_link_aliases.py")
    quoted = _page(tmp_path, f"Sources/{SID}.md",
                   f'---\ntype: source\nsource_id: "{SID}"\ntitle: "Quoted Title"\n---\n\n# s\n')
    bare_sid = "src_" + "9" * 16
    bare = _page(tmp_path, f"Sources/{bare_sid}.md",
                 f'---\ntype: source\nsource_id: "{bare_sid}"\ntitle: Bare Title\n---\n\n# s\n')
    for page, target in ((quoted, f"Sources/{SID}"), (bare, f"Sources/{bare_sid}")):
        assert (validator.display_label(page, target)
                == labels._page_label(page, target) != "")
    # and the shape gate actually fires on a bare link to the UNQUOTED-title page
    _page(tmp_path, f"Claims/{CID}.md",
          f'---\ntype: claim\nclaim_id: "{CID}"\ntitle: "T"\n---\n\n# c\n\n[[Sources/{bare_sid}]]\n')
    assert _run_validator("validate_link_aliases.py", tmp_path).returncode == 1
