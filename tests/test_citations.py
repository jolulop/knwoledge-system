from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_citations  # noqa: E402

from app.workers.citations import ground_citation, is_grounded, parse_citations

SID = "src_0123456789abcdef"
MD = "The quick brown fox jumps over the lazy dog.\n"
START = MD.index("quick")
END = START + len("quick brown")
QUOTE = MD[START:END]  # "quick brown"


def _cite(**over):
    base = {"source_id": SID, "char_start": START, "char_end": END, "quote": QUOTE}
    base.update(over)
    return base


# --- grounding gate ---------------------------------------------------------


def test_valid_citation_grounds():
    assert ground_citation(_cite(), MD) == []
    assert is_grounded(_cite(), MD)


def test_out_of_bounds_range_fails():
    assert any("out of bounds" in p for p in ground_citation(_cite(char_end=10_000), MD))
    assert any("out of bounds" in p for p in ground_citation(_cite(char_start=-1, char_end=3), MD))
    assert ground_citation(_cite(char_start=10, char_end=10), MD)  # empty/inverted range


def test_quote_mismatch_fails():
    problems = ground_citation(_cite(quote="something the source never said"), MD)
    assert any("quote does not match" in p for p in problems)


def test_quote_whitespace_is_normalised():
    # Same text, different internal whitespace -> still grounds (ADR-0026).
    assert ground_citation(_cite(quote="quick   brown"), MD) == []


def test_missing_quote_only_fails_when_required():
    no_quote = {"source_id": SID, "char_start": START, "char_end": END}
    assert ground_citation(no_quote, MD) == []
    assert any("missing evidence quote" in p for p in ground_citation(no_quote, MD, require_quote=True))


def test_malformed_source_id_fails():
    assert any("source_id" in p for p in ground_citation(_cite(source_id="not-a-source"), MD))
    assert any("source_id" in p for p in ground_citation(_cite(source_id=None), MD))


def test_page_bounds_checked():
    assert ground_citation(_cite(page=2), MD, page_count=4) == []
    assert any("exceeds page_count" in p for p in ground_citation(_cite(page=9), MD, page_count=4))
    assert any("positive integer" in p for p in ground_citation(_cite(page=0), MD))


def test_chunk_id_is_advisory_and_never_grounds():
    # A valid anchor with an advisory chunk_id still grounds (chunk_id ignored)...
    assert ground_citation(_cite(chunk_id=f"{SID}::0007"), MD) == []
    # ...but chunk_id cannot rescue an out-of-bounds authoritative range.
    bad = _cite(char_start=10_000, char_end=10_010, chunk_id=f"{SID}::0007")
    assert ground_citation(bad, MD)


def test_parse_citations_block():
    fm = (
        "type: claim\n"
        "citations:\n"
        f'  - source_id: "{SID}"\n'
        f"    char_start: {START}\n"
        f"    char_end: {END}\n"
        '    page: 2\n'
        f'    quote: "{QUOTE}"\n'
        '    chunk_id: null\n'
        '  - source_id: "src_aaaaaaaaaaaaaaaa"\n'
        "    char_start: 0\n"
        "    char_end: 3\n"
        "supports: []\n"
    )
    cites = parse_citations(fm)
    assert len(cites) == 2
    assert cites[0] == {"source_id": SID, "char_start": START, "char_end": END,
                        "page": 2, "quote": QUOTE, "chunk_id": None}
    assert cites[1]["source_id"] == "src_aaaaaaaaaaaaaaaa"


# --- validator over claim pages --------------------------------------------


def _setup(tmp_path, citations_lines, *, md=MD, evidence=True, claim_id="clm_0123456789abcdef"):
    if md is not None:
        p = tmp_path / "normalized" / "markdown" / f"{SID}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
        # The citation must resolve to a real source (manifest), not just a normalized file.
        man = tmp_path / "raw" / "manifests" / f"{SID}.json"
        man.parent.mkdir(parents=True, exist_ok=True)
        man.write_text("{}", encoding="utf-8")
    body = "\n## Evidence\n\n| Source | ... |\n" if evidence else "\n"
    text = f"---\ntype: claim\nclaim_id: {claim_id}\n{citations_lines}\n---\n\n# Claim{body}"
    cp = tmp_path / "wiki" / "Claims" / f"{claim_id}.md"
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(text, encoding="utf-8")


def _cit_yaml(**over):
    c = {"source_id": SID, "char_start": START, "char_end": END, "quote": QUOTE}
    c.update(over)
    lines = ["citations:"]
    first = True
    for k, v in c.items():
        prefix = "  - " if first else "    "
        first = False
        val = f'"{v}"' if isinstance(v, str) else ("null" if v is None else str(v))
        lines.append(f"{prefix}{k}: {val}")
    return "\n".join(lines)


def test_validator_passes_grounded_claim(tmp_path):
    _setup(tmp_path, _cit_yaml())
    assert validate_citations.main([str(tmp_path)]) == 0


def test_validator_fails_out_of_bounds(tmp_path):
    _setup(tmp_path, _cit_yaml(char_end=10_000))
    assert validate_citations.main([str(tmp_path)]) == 1


def test_validator_fails_quote_mismatch(tmp_path):
    _setup(tmp_path, _cit_yaml(quote="never said this"))
    assert validate_citations.main([str(tmp_path)]) == 1


def test_validator_fails_missing_normalized_source(tmp_path):
    _setup(tmp_path, _cit_yaml(source_id="src_ffffffffffffffff"))  # valid shape, no md file
    assert validate_citations.main([str(tmp_path)]) == 1


def test_validator_fails_malformed_source_id(tmp_path):
    _setup(tmp_path, _cit_yaml(source_id="garbage"))
    assert validate_citations.main([str(tmp_path)]) == 1


def test_validator_fails_when_quote_missing(tmp_path):
    # Claims require a quote (ADR-0026): a citation with only the range fails.
    yaml = f'citations:\n  - source_id: "{SID}"\n    char_start: {START}\n    char_end: {END}\n'
    _setup(tmp_path, yaml)
    assert validate_citations.main([str(tmp_path)]) == 1


def test_validator_accepts_no_source_marker(tmp_path):
    body = "\n## Evidence\n\nNo source found in vault.\n"
    cp = tmp_path / "wiki" / "Claims" / "clm_nosrc.md"
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(f"---\ntype: claim\nclaim_id: clm_nosrc\n---\n{body}", encoding="utf-8")
    assert validate_citations.main([str(tmp_path)]) == 0


def test_validator_fails_uncited_claim(tmp_path):
    cp = tmp_path / "wiki" / "Claims" / "clm_bare.md"
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text("---\ntype: claim\nclaim_id: clm_bare\n---\n\n# Claim\n\n## Evidence\n", encoding="utf-8")
    assert validate_citations.main([str(tmp_path)]) == 1
