"""ADR-0074 decision 4 (slice 3a): the read discipline for Item, Claim and Synthesis pages.

Behavioural proof per family (decision 4.7): a page that can't be read, isn't valid UTF-8, has no
frontmatter or lacks a required durable field is never re-rendered from defaults — the writer
refuses and reports it — and a verified-absent page is re-created only where the family's domain
allows (decision 4.1); otherwise it is lost authority, left for the operator to restore."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.backend import authority_read, graph
from app.workers import claims, items, synthesis
from app.workers.wiki_render import NODE_DIR
from tests.test_synthesis import (SynthAdapter, _build, _EmptyClaims, _gconn, _gen, _sid,
                                  _syn_page, TIER2)

NOW = "2026-11-12T00:00:00Z"


def _strip_line(path: Path, key: str) -> bytes:
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(rf"(?m)^{key}:.*\n", "", text, count=1), encoding="utf-8")
    return path.read_bytes()


def _set_line(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(rf"(?m)^{key}:.*$", f"{key}: {value}", text, count=1), encoding="utf-8")


def _unreadable(path: Path) -> None:
    path.unlink()
    path.mkdir()                                      # an entry that is not a regular file


# --- the shared reader ----------------------------------------------------------------------------


def test_the_shared_reader_distinguishes_the_four_states(tmp_path):
    f = tmp_path / "x.md"
    assert authority_read.read_text(f).state == authority_read.ABSENT
    f.write_bytes(b"\xff\xfe")
    assert authority_read.read_text(f).error == "invalid_utf8"
    f.unlink()
    f.symlink_to(tmp_path / "gone")
    assert authority_read.read_text(f).error == "dangling_symlink"        # never "absent"
    f.unlink()
    f.mkdir()
    assert authority_read.read_text(f).error == "not_a_regular_file"
    assert authority_read.read_text(tmp_path / "nodir" / "x.md").state == authority_read.ABSENT
    (tmp_path / "file").write_text("x")
    assert authority_read.read_text(tmp_path / "file" / "x.md").state == authority_read.UNREADABLE


# --- Item pages -----------------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path):
    jobs = _build(tmp_path)
    page = next((tmp_path / "wiki" / NODE_DIR["item"]).glob("*.md"))
    nid = re.search(r'(?m)^item_id:\s*"(itm_[0-9a-f]{16})"', page.read_text()).group(1)
    return tmp_path, jobs, page, nid


def _recompose(root, nid, **kw):
    gconn = _gconn(root)
    try:
        return items._recompose_node(gconn, node_id=nid, wiki_dir=root / "wiki",
                                     reviews_dir=root / "reviews", now=NOW, **kw)
    finally:
        gconn.close()


@pytest.mark.parametrize("damage", ["status", "title", "frontmatter", "utf8", "aliases"])
def test_an_item_writer_never_rerenders_a_damaged_page_from_defaults(vault, damage):
    root, _jobs, page, nid = vault
    if damage in ("status", "title"):
        before = _strip_line(page, damage)
    elif damage == "aliases":
        _set_line(page, "aliases", "not-a-list")
        before = page.read_bytes()
    elif damage == "frontmatter":
        page.write_text(page.read_text().replace("---\n", "", 2))
        before = page.read_bytes()
    else:
        page.write_bytes(page.read_bytes().replace(b'title: "', b'title: "\xff', 1))
        before = page.read_bytes()
    read, meta = items.read_item_page(page)
    assert read.state == authority_read.CORRUPT and meta is None
    assert _recompose(root, nid) == "page_corrupt"
    # Without frontmatter (or decodable bytes) the page's owner can't be established either, so the
    # ownership check (ADR-0070 B1) refuses first — a refusal all the same.
    expected = "page_conflict" if damage in ("frontmatter", "utf8") else "page_corrupt"
    gconn = _gconn(root)
    try:
        assert items.recompose_semantic_node_page(
            gconn, node_id=nid, wiki_dir=root / "wiki", status="active",
            review_status="none", now=NOW) == expected
        assert graph.get_node(gconn, nid)["status"] == "active"         # the mirror is untouched
    finally:
        gconn.close()
    assert page.read_bytes() == before                                   # never reset
    assert items.page_skip_reason(page) == "page_corrupt"


def test_an_unreadable_item_page_is_reported_not_raised(vault):
    root, _jobs, page, nid = vault
    _unreadable(page)
    assert _recompose(root, nid) == "page_unreadable"
    assert items.page_skip_reason(page) == "page_unreadable" and page.is_dir()


def test_a_missing_page_of_a_kept_tag_is_lost_authority_never_recreated(vault):
    root, _jobs, page, nid = vault
    page.unlink()                         # the tag is kept on both documents (human authority)
    assert _recompose(root, nid, text_hint={"title": "X", "aliases": [], "item_type": None}) == \
        "lost_authority"
    assert not page.exists()
    gconn = _gconn(root)
    try:
        assert not items.page_may_be_created(gconn, graph.get_node(gconn, nid))
    finally:
        gconn.close()


def test_a_new_suggestion_may_be_created_until_a_human_acts_on_it(tmp_path):
    gdb = tmp_path / "db" / "graph.sqlite"
    graph.init_db(gdb)
    gconn = graph.connect(gdb)
    try:
        nid, sid = "itm_00000000000000aa", "src_00000000000000aa"
        graph.upsert_node(gconn, node_id=sid, node_type="source", slug=sid, status="active", now=NOW)
        graph.upsert_node(gconn, node_id=nid, node_type="item", slug="x", status="candidate",
                          item_type="method_technique", now=NOW)
        graph.upsert_assertion(gconn, src_id=sid, dst_id=nid, edge_type="mentions",
                               asserted_by="llm", status="active", now=NOW)
        assert items.page_may_be_created(gconn, graph.get_node(gconn, nid))
        graph.upsert_assertion(gconn, src_id=sid, dst_id=nid, edge_type="mentions",
                               asserted_by="human", status="active", now=NOW)
        assert not items.page_may_be_created(gconn, graph.get_node(gconn, nid))   # a keep
        graph.upsert_node(gconn, node_id="itm_00000000000000bb", node_type="item", slug="y",
                          status="active", item_type="method_technique", now=NOW)
        assert not items.page_may_be_created(gconn, graph.get_node(gconn, "itm_00000000000000bb"))
    finally:
        gconn.close()


def test_a_damaged_tombstone_is_treated_as_a_decided_retirement(vault):
    root, _jobs, page, nid = vault
    page.write_text("no frontmatter at all\n")
    node = {"node_id": nid, "slug": page.stem, "status": "deprecated_candidate"}
    assert items.decided_status(root / "wiki", node) == "retired"          # never resurrected
    page.unlink()
    assert items.decided_status(root / "wiki", node) is None                # absent: undecided


def test_a_legacy_item_page_without_optional_fields_is_not_damage(tmp_path):
    page = tmp_path / "legacy.md"
    page.write_text('---\ntype: item\nitem_id: "itm_00000000000000aa"\ntitle: "Legacy"\n'
                    "status: candidate\n---\n\n# Legacy\n", encoding="utf-8")
    read, meta = items.read_item_page(page)
    assert read.ok and meta["title"] == "Legacy" and meta["aliases"] == []
    assert meta["item_type"] is None and meta["description"] is None and meta["split_from"] is None


# --- Claim pages ----------------------------------------------------------------------------------


def _claim(root):
    page = next((root / "wiki" / "Claims").glob("clm_*.md"))
    return page, page.stem


def _recompose_claim(root, cid, **kw):
    gconn = _gconn(root)
    try:
        return claims.recompose_claim(gconn, cid=cid, claims_dir=root / "wiki" / "Claims",
                                      reviews_dir=root / "reviews", now=NOW,
                                      markdown_dir=root / "normalized" / "markdown", **kw)
    finally:
        gconn.close()


def _mirror(root, cid, status):
    gconn = _gconn(root)
    try:
        graph.upsert_node(gconn, node_id=cid, node_type="claim", slug=cid, status=status, now=NOW)
        gconn.commit()
    finally:
        gconn.close()


def test_a_hidden_claim_whose_status_was_lost_is_never_unhidden(vault):
    root, *_ = vault
    page, cid = _claim(root)
    _set_line(page, "status", "hidden")
    _mirror(root, cid, "hidden")
    before = _strip_line(page, "status")
    assert claims.page_authority(page, cid)["state"] == claims.PAGE_OK      # identity still holds
    assert _recompose_claim(root, cid) == claims.PAGE_CORRUPT
    assert page.read_bytes() == before
    gconn = _gconn(root)
    try:
        assert graph.get_node(gconn, cid)["status"] == "hidden"
    finally:
        gconn.close()


@pytest.mark.parametrize("damage", ["frontmatter", "utf8", "unreadable"])
def test_a_damaged_claim_page_is_refused_and_reported(vault, damage):
    root, *_ = vault
    page, cid = _claim(root)
    if damage == "frontmatter":
        page.write_text("just prose\n")
        expected = claims.MARKER_UNREADABLE          # ADR-0073 decision 16's state, kept (4.5)
    elif damage == "utf8":
        page.write_bytes(b"---\n\xff\n---\n")
        expected = claims.PAGE_CORRUPT
    else:
        _unreadable(page)
        expected = claims.PAGE_UNREADABLE
    assert _recompose_claim(root, cid) == expected                           # never raises
    gconn = _gconn(root)
    try:
        assert claims.mirror_page_status(gconn, cid=cid, claims_dir=root / "wiki" / "Claims",
                                         now=NOW) == expected               # publication excludes it
    finally:
        gconn.close()
    if damage != "unreadable":
        assert claims.page_authority(page, cid)["state"] == expected


def test_a_lost_claim_page_is_recreated_only_for_a_producer_default(vault):
    root, *_ = vault
    page, cid = _claim(root)
    wording = claims._read_claim_text(page)
    page.unlink()
    assert _recompose_claim(root, cid, text_hint=wording) == "written" and page.exists()   # active
    _mirror(root, cid, "hidden")
    page.unlink()
    assert _recompose_claim(root, cid, text_hint=wording) == claims.LOST_AUTHORITY
    assert not page.exists()


def test_a_claims_run_reports_refused_pages(tmp_path, monkeypatch):
    from tests.test_manifest_damage_callers import _claims_run, _claims_setup
    sid, _c = _claims_setup(tmp_path)
    page = next((tmp_path / "wiki" / "Claims").glob("clm_*.md"))
    before = _strip_line(page, "status")
    out = _claims_run(tmp_path, force=True)
    assert {"claim_id": page.stem, "reason": "page_corrupt"} in out["claim_pages_refused"]
    assert out["status"] == "partial" and page.read_bytes() == before


# --- Synthesis pages ------------------------------------------------------------------------------


def _ineligible(root, jobs):
    from app.llm.cache import ResponseCache
    from app.llm.client import LLMClient
    fresh = LLMClient({"anthropic": _EmptyClaims()}, cache=ResponseCache(root / "db" / "f.sqlite"))
    claims.extract_claims(root, client=fresh, model_ref=TIER2, jobs_db=jobs,
                          source_ids=[_sid(root, "b.md")], force=True, rebuild_index=False)


@pytest.mark.parametrize("damage", ["frontmatter", "status", "topic", "unreadable"])
def test_a_damaged_synthesis_page_is_never_retracted_or_regenerated(vault, damage):
    root, jobs, *_ = vault
    _gen(root, SynthAdapter(), jobs)
    page = _syn_page(root)
    syn_id = page.stem
    if damage == "frontmatter":
        page.write_text("prose only\n")
    elif damage == "status":
        _strip_line(page, "status")
    elif damage == "topic":
        _set_line(page, "topic_node", "itm_00000000000000ff")
    else:
        _unreadable(page)
    expected = "page_unreadable" if damage == "unreadable" else "page_corrupt"
    before = None if damage == "unreadable" else page.read_bytes()
    out = _gen(root, SynthAdapter(), jobs, force=True)              # regeneration refused
    assert {"synthesis_id": syn_id, "reason": expected} in out["synthesis_pages_refused"]
    assert out["status"] == "partial"
    _ineligible(root, jobs)
    out = _gen(root, SynthAdapter(), jobs)                          # retraction refused
    assert out["retracted"] == 0
    assert {"synthesis_id": syn_id, "reason": expected} in out["synthesis_pages_refused"]
    if before is not None:
        assert page.read_bytes() == before


def test_a_lost_approved_synthesis_page_is_never_recreated(vault):
    root, jobs, *_ = vault
    _gen(root, SynthAdapter(), jobs)
    page = _syn_page(root)
    gconn = _gconn(root)
    try:
        graph.upsert_node(gconn, node_id=page.stem, node_type="synthesis", slug=page.stem,
                          status="active", now=NOW)                  # an approved synthesis
        gconn.commit()
    finally:
        gconn.close()
    page.unlink()
    out = _gen(root, SynthAdapter(), jobs, force=True)
    assert {"synthesis_id": page.stem, "reason": "lost_authority"} in out["synthesis_pages_refused"]
    assert not page.exists()
    read, fm = synthesis.read_synthesis_page(root / "wiki" / "Synthesis", page.stem)
    assert read.state == authority_read.ABSENT and fm is None


# --- review round 1 (3a): every Synthesis writer, and split recovery ------------------------------


def _syn_status(root, syn_id):
    gconn = _gconn(root)
    try:
        return graph.get_node(gconn, syn_id)["status"]
    finally:
        gconn.close()


def _set_syn_mirror(root, syn_id, status):
    gconn = _gconn(root)
    try:
        graph.upsert_node(gconn, node_id=syn_id, node_type="synthesis", slug=syn_id, status=status,
                          now=NOW)
        gconn.commit()
    finally:
        gconn.close()


def test_fan_out_and_publication_never_reset_a_hidden_synthesis_with_a_damaged_page(vault):
    from app.workers import publication
    root, jobs, *_ = vault
    _gen(root, SynthAdapter(), jobs)
    page = _syn_page(root)
    syn_id = page.stem
    _set_line(page, "status", "hidden")
    _set_syn_mirror(root, syn_id, "hidden")
    before = _strip_line(page, "status")
    gconn = _gconn(root)
    try:
        out = synthesis.rerender_synthesis_page(          # the claim-visibility fan-out's writer
            gconn, syn_id, synthesis_dir=root / "wiki" / "Synthesis",
            enrichment_dir=root / "normalized" / "enrichment", now=NOW)
        assert out is None
        failed: dict[str, str] = {}
        publication._render_syntheses(gconn, [syn_id], wiki_dir=root / "wiki", root=root,
                                      now=NOW, failed=failed)
        assert syn_id in failed                            # reported, never rendered
        with pytest.raises(synthesis.SynthesisPageRefused):   # the backstop in the shared renderer
            synthesis._render_page(gconn, syn_id=syn_id, topic_node="x", title="t", summary="s",
                                   synthesis_text="b", confidence=0.5, status="active",
                                   review_status="approved", synthesis_dir=root / "wiki" / "Synthesis",
                                   now=NOW)
    finally:
        gconn.close()
    assert page.read_bytes() == before and _syn_status(root, syn_id) == "hidden"


def test_the_visibility_executor_refuses_a_damaged_synthesis_page(vault):
    root, jobs, *_ = vault
    _gen(root, SynthAdapter(), jobs)
    page = _syn_page(root)
    syn_id = page.stem
    _set_line(page, "status", "hidden")
    _set_syn_mirror(root, syn_id, "hidden")
    before = _strip_line(page, "status")
    d = root / "reviews" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rev_u.json").write_text(__import__("json").dumps({
        "review_id": "rev_u", "type": "unhide_synthesis", "status": "approved",
        "subject": {"node_id": syn_id, "page": f"Synthesis/{syn_id}.md"},
        "proposal": {"to_status": "active"}, "context": {"node_type": "synthesis"}}))
    gconn = _gconn(root)
    try:
        out = synthesis.apply_unhidden_syntheses(
            gconn, root / "reviews", synthesis_dir=root / "wiki" / "Synthesis",
            enrichment_dir=root / "normalized" / "enrichment", now=NOW)
    finally:
        gconn.close()
    assert out["applied"] == 0
    assert [o["reason"] for o in out["outcomes"]] == ["page_corrupt"]
    assert page.read_bytes() == before and _syn_status(root, syn_id) == "hidden"


def test_an_approval_never_recreates_a_lost_retired_synthesis_page(vault):
    import json

    from app.workers import reviews
    from tests.test_synthesis import _propose_rid, _shown
    root, jobs, *_ = vault
    _gen(root, SynthAdapter(), jobs)
    page = _syn_page(root)
    syn_id = page.stem
    reviews.resolve_review_item(root / "reviews", _propose_rid(root), decision="approved",
                                decided_by="human", version_digest=_shown(root))
    _set_syn_mirror(root, syn_id, "deprecated_candidate")        # retracted meanwhile
    page.unlink()
    gconn = _gconn(root)
    try:
        out = synthesis.apply_resolved_syntheses(
            gconn, root / "reviews", synthesis_dir=root / "wiki" / "Synthesis",
            enrichment_dir=root / "normalized" / "enrichment", now=NOW)
    finally:
        gconn.close()
    assert out["promoted"] == 0
    assert [o["reason"] for o in out["outcomes"]] == ["lost_authority"]
    assert not page.exists() and _syn_status(root, syn_id) == "deprecated_candidate"
    assert any(json.loads(p.read_text())["type"] == "propose_synthesis"
               for p in (root / "reviews" / "approved").glob("*.json"))     # the decision stays


def test_split_recovery_never_renders_over_a_damaged_spin_off_page(tmp_path, monkeypatch):
    from app.workers import identity_journal, splits, tag_links
    from tests.test_tag_links import (_approve_split, _extract, _g, _item, _keep, _node, _run, _sids,
                                      _titles, _view)
    from tests.test_tag_links import _build as build_tags
    build_tags(tmp_path, {"a.md": "alpha body", "b.md": "beta body", "c.md": "gamma body"})
    sids = _sids(tmp_path)
    c = sids["c.md"]
    _extract(tmp_path, {"alpha": [_item("Mercury")], "beta": [_item("Mercury")],
                        "gamma": [_item("Mercury")]})
    for f in ("a.md", "b.md", "c.md"):
        _keep(tmp_path, sids[f], "Mercury")
    _approve_split(tmp_path, "Mercury", "Mercury Planet", [c])
    real, calls = graph.repoint_edge, {"n": 0}

    def crash_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power cut")
        return real(*args, **kwargs)
    monkeypatch.setattr(graph, "repoint_edge", crash_second)
    gconn = _g(tmp_path)
    with pytest.raises(RuntimeError):
        splits.apply_splits(gconn, tmp_path / "reviews", wiki_dir=tmp_path / "wiki")
    gconn.close()
    monkeypatch.undo()
    assert identity_journal.is_open(tmp_path / "reviews", "rev_s")

    # The spin-off's page exists but its status is gone (it can't be rendered over).
    b = _node(tmp_path, "Mercury Planet")
    b_page = tmp_path / "wiki" / NODE_DIR["item"] / f"{b['slug']}.md"
    b_page.write_text(f'---\ntype: item\nitem_id: "{b["node_id"]}"\ntitle: "Mercury Planet"\n'
                      f"item_type: method_technique\naliases: []\n---\n\n# Mercury Planet\n")
    before_page = b_page.read_bytes()
    before_links = (_titles(_view(tmp_path, c)["kept"]), tag_links.load_ledger(tmp_path / "reviews", c))
    gconn = _g(tmp_path)
    try:
        before_edges = sorted((e["edge_id"], e["dst_id"], e["status"]) for e in
                              gconn.execute("SELECT * FROM edges WHERE src_id = ?", (c,)))
    finally:
        gconn.close()

    out = _run(tmp_path, splits.apply_splits)
    assert out["applied"] == 0
    assert [s["reason"] for s in out["skipped"]] == ["page_corrupt"]
    assert b_page.read_bytes() == before_page                        # never rendered over
    assert identity_journal.is_open(tmp_path / "reviews", "rev_s")   # recovery stays queued
    assert (_titles(_view(tmp_path, c)["kept"]),
            tag_links.load_ledger(tmp_path / "reviews", c)) == before_links   # nothing moved
    gconn = _g(tmp_path)
    try:
        assert sorted((e["edge_id"], e["dst_id"], e["status"]) for e in gconn.execute(
            "SELECT * FROM edges WHERE src_id = ?", (c,))) == before_edges
    finally:
        gconn.close()
