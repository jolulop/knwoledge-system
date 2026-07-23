from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backup  # noqa: E402
import validate_wiki  # noqa: E402

from app.backend import db, manifests
from app.llm.cache import ResponseCache
from app.llm.client import LLMClient
from app.workers import enrich, extract, intake, wiki
from app.workers.wiki_render import parse_frontmatter
from tests import fixtures

TEMPLATES = ROOT / "templates"
MODEL_REF = "anthropic:claude-haiku-4-5"


class FakeAdapter:
    name = "anthropic"
    supports_batch = False

    def __init__(self, response=None, *, available=True):
        self.calls = 0
        self._response = response or {"summary": "A faithful generated summary.", "tags": ["alpha", "beta"]}
        self._available = available

    def available(self):
        return self._available

    def parse(self, messages, schema, model_id, *, max_tokens):
        self.calls += 1
        return dict(self._response)


def _build(tmp_path: Path) -> Path:
    """Two extracted markdown sources with real prose (both enrichable)."""
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    fixtures.write_markdown(inbox / "doc.md")
    (inbox / "doc2.md").write_text(
        "# Second Document\n\nA different opening paragraph with enough real prose to "
        "summarize and tag.\n",
        encoding="utf-8",
    )
    intake.scan_inbox(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    extract.extract_sources(tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite")
    return tmp_path


def _client(tmp_path, adapter):
    cache = ResponseCache(tmp_path / "db" / "llm_cache.sqlite")
    return LLMClient({"anthropic": adapter}, cache=cache)


def _enrich(tmp_path, adapter, **kw):
    return enrich.enrich_sources(
        tmp_path, client=_client(tmp_path, adapter), model_ref=MODEL_REF,
        jobs_db=tmp_path / "db" / "jobs.sqlite", **kw,
    )


def _gen(tmp_path, **kw):
    return wiki.generate_wiki(
        tmp_path, jobs_db=tmp_path / "db" / "jobs.sqlite",
        templates_dir=TEMPLATES, rebuild_index=False, **kw,
    )


def _sid(tmp_path, name):
    for m in manifests.list_manifests(tmp_path / "raw" / "manifests"):
        if m["original_filename"] == name:
            return m["source_id"]
    raise AssertionError(name)


def _page_fm(tmp_path, sid):
    page = tmp_path / "wiki" / "Sources" / f"{sid}.md"
    return page, parse_frontmatter(page.read_text(encoding="utf-8"))


def test_enrich_writes_artifact_and_records_job(tmp_path):
    _build(tmp_path)
    fake = FakeAdapter()
    summary = _enrich(tmp_path, fake)

    assert summary["enriched"] == 2  # both markdown sources
    sid = _sid(tmp_path, "doc.md")
    art = tmp_path / "normalized" / "enrichment" / f"{sid}.json"
    assert art.exists()

    conn = db.connect(tmp_path / "db" / "jobs.sqlite")
    try:
        job = db.get_job(conn, summary["job_id"])
    finally:
        conn.close()
    assert job["job_type"] == "enrich" and job["status"] == "succeeded"


def test_enriched_summary_is_composed_with_generated_label(tmp_path):
    _build(tmp_path)
    _enrich(tmp_path, FakeAdapter())
    _gen(tmp_path)

    sid = _sid(tmp_path, "doc.md")
    page, fm = _page_fm(tmp_path, sid)
    text = page.read_text(encoding="utf-8")
    assert fm["summary_status"] == "enriched"
    assert fm["generation_status"] == "enriched"
    assert fm["tags"] == ["alpha", "beta"]
    assert "A faithful generated summary." in text
    # Machine-checkable generated/unverified label, and the validator accepts it (ADR-0026).
    summary_line = next(ln for ln in text.splitlines() if ln.strip().startswith("> [!summary]"))
    assert "unverified" in summary_line.lower()
    assert validate_wiki.main([str(tmp_path)]) == 0


def test_enrichment_preserved_across_deterministic_regen(tmp_path):
    _build(tmp_path)
    _enrich(tmp_path, FakeAdapter())
    _gen(tmp_path)
    sid = _sid(tmp_path, "doc.md")
    _, fm1 = _page_fm(tmp_path, sid)
    assert fm1["summary_status"] == "enriched"

    # A later deterministic generate_wiki run must NOT revert the page to a stub: it
    # re-composes the still-fresh enrichment artifact (ADR-0028).
    second = _gen(tmp_path)
    assert second["skipped_unchanged"] == 2  # both pages unchanged, none reverted
    _, fm2 = _page_fm(tmp_path, sid)
    assert fm2["summary_status"] == "enriched"


def test_stale_artifact_reverts_to_stub_until_reenriched(tmp_path):
    _build(tmp_path)
    _enrich(tmp_path, FakeAdapter())
    _gen(tmp_path)
    sid = _sid(tmp_path, "doc.md")

    # Change the normalized text: the artifact's fingerprint no longer matches, so the
    # renderer must fall back to the deterministic stub rather than show a stale summary.
    md = tmp_path / "normalized" / "markdown" / f"{sid}.md"
    md.write_text("# T\n\nCompletely different body text that the summary no longer matches.\n",
                  encoding="utf-8")
    _gen(tmp_path)
    _, fm = _page_fm(tmp_path, sid)
    assert fm["summary_status"] == "stub"


def test_no_api_key_skips_enrichment_and_pages_stay_stub(tmp_path):
    _build(tmp_path)
    fake = FakeAdapter(available=False)
    summary = _enrich(tmp_path, fake)

    assert summary["skipped_no_key"] == 2
    assert summary["enriched"] == 0
    assert fake.calls == 0
    enr_dir = tmp_path / "normalized" / "enrichment"
    assert not (enr_dir.exists() and list(enr_dir.glob("*.json")))

    # The whole run was gated on a missing key -> recorded as a 'skipped' job, not 'succeeded'.
    assert summary["status"] == "skipped"
    conn = db.connect(tmp_path / "db" / "jobs.sqlite")
    try:
        job = db.get_job(conn, summary["job_id"])
    finally:
        conn.close()
    assert job["status"] == "skipped"

    _gen(tmp_path)
    sid = _sid(tmp_path, "doc.md")
    _, fm = _page_fm(tmp_path, sid)
    assert fm["summary_status"] == "stub"


def test_generated_wikilinks_are_neutralised(tmp_path):
    _build(tmp_path)
    # A model that emits a wikilink in the summary and bracket syntax in a tag.
    adapter = FakeAdapter(response={
        "summary": "Discusses [[Post-Merger Integration]] and [[concepts/x|synergy]] at length.",
        "tags": ["[[merger]]", "integration"],
    })
    _enrich(tmp_path, adapter)
    _gen(tmp_path)

    sid = _sid(tmp_path, "doc.md")
    page, fm = _page_fm(tmp_path, sid)
    text = page.read_text(encoding="utf-8")
    assert "[[" not in text and "]]" not in text  # no active wikilinks anywhere on the page
    assert "Post-Merger Integration" in text and "synergy" in text  # de-linked to plain text
    assert fm["tags"] == ["merger", "integration"]  # bracket syntax stripped from tags
    assert validate_wiki.main([str(tmp_path)]) == 0


def test_response_cache_excluded_from_backup_when_opted_out(tmp_path, monkeypatch):
    _build(tmp_path)
    _enrich(tmp_path, FakeAdapter())
    assert (tmp_path / "db" / "llm_cache.sqlite").exists()

    monkeypatch.setenv("BACKUP_EXCLUDE_LLM_CACHE", "1")
    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())
    assert "db/llm_cache.sqlite" not in names  # opted out
    assert "db/jobs.sqlite" in names           # other db files still backed up


def test_schema_failure_drops_source_and_keeps_stub(tmp_path):
    _build(tmp_path)
    bad = FakeAdapter(response={"summary": "missing tags field"})  # invalid: no tags
    summary = _enrich(tmp_path, bad)

    assert summary["enriched"] == 0
    assert summary["errors"] == 2
    sid = _sid(tmp_path, "doc.md")
    assert not (tmp_path / "normalized" / "enrichment" / f"{sid}.json").exists()


def test_enrich_is_idempotent_and_force_reenriches(tmp_path):
    _build(tmp_path)
    first = _enrich(tmp_path, FakeAdapter())
    assert first["enriched"] == 2

    second = _enrich(tmp_path, FakeAdapter())
    assert second["enriched"] == 0
    assert second["skipped_fresh"] == 2

    forced = _enrich(tmp_path, FakeAdapter(), force=True)
    assert forced["enriched"] == 2


def test_response_cache_is_backed_up_but_artifact_is_not(tmp_path):
    _build(tmp_path)
    _enrich(tmp_path, FakeAdapter())
    assert (tmp_path / "db" / "llm_cache.sqlite").exists()
    assert list((tmp_path / "normalized" / "enrichment").glob("*.json"))

    out = backup.create_backup(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())
    # The cache is the durable record (backed up); the artifact is derived state in the
    # not-backed-up normalized/ layer (ADR-0025/0027).
    assert "db/llm_cache.sqlite" in names
    assert not any(n.startswith("normalized/") for n in names)


def test_artifact_regenerates_from_cache_without_provider_call(tmp_path):
    import shutil

    _build(tmp_path)
    fake = FakeAdapter()
    _enrich(tmp_path, fake)
    calls_after_first = fake.calls
    assert calls_after_first == 2

    # Simulate a restore: the cache survives (it is backed up), the derived artifacts do
    # not. Re-enriching must replay the cache — no new provider calls — and rebuild them.
    shutil.rmtree(tmp_path / "normalized" / "enrichment")
    summary = _enrich(tmp_path, fake)
    assert summary["enriched"] == 2
    assert fake.calls == calls_after_first  # pure cache replay, zero provider calls

    _gen(tmp_path)
    sid = _sid(tmp_path, "doc.md")
    _, fm = _page_fm(tmp_path, sid)
    assert fm["summary_status"] == "enriched"


# --- ADR-0063 sticky-to-chain freshness -------------------------------------


def test_chain_fresh_helper():
    from app.workers.enrichment_artifact import chain_fresh
    def recompute(m):
        return f"fp-for-{m}"
    art_a = {"model_ref": "anthropic:a", "input_fingerprint": "fp-for-anthropic:a"}
    assert chain_fresh(art_a, ["local:x", "anthropic:a"], recompute) is True   # in chain + fp matches
    assert chain_fresh(art_a, ["local:x"], recompute) is False                 # recorded left the chain
    stale = {"model_ref": "anthropic:a", "input_fingerprint": "old"}
    assert chain_fresh(stale, ["anthropic:a"], recompute) is False             # own-model fp drifted
    assert chain_fresh({"input_fingerprint": "x"}, ["anthropic:a"], recompute) is False  # no recorded model


def test_enrich_sticky_to_chain_availability_flip_does_not_re_enrich(tmp_path):
    # ADR-0063: an artifact produced by a still-in-chain model is NOT restaled when the run resolves to a
    # DIFFERENT (also in-chain) model because local availability changed.
    _build(tmp_path)
    cache = ResponseCache(tmp_path / "db" / "llm_cache.sqlite")
    hosted = FakeAdapter()
    c1 = LLMClient({"anthropic": hosted, "local": FakeAdapter(available=False)}, cache=cache)
    r1 = enrich.enrich_sources(tmp_path, client=c1, model_ref="anthropic:h",
                               jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert r1["enriched"] == 2 and hosted.calls == 2   # artifacts recorded anthropic:h

    # Local now up; chain is local-first "local:x,anthropic:h" -> resolves to local, but the existing
    # artifacts recorded anthropic:h which is STILL a chain member -> sticky-fresh, not re-enriched.
    local = FakeAdapter()
    c2 = LLMClient({"anthropic": FakeAdapter(), "local": local}, cache=cache)
    r2 = enrich.enrich_sources(tmp_path, client=c2, model_ref="local:x,anthropic:h",
                               jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert r2["skipped_fresh"] == 2 and r2["enriched"] == 0
    assert local.calls == 0   # the flip alone never restales / re-derives


def test_enrich_re_enriches_when_recorded_model_leaves_chain(tmp_path):
    # ADR-0063: if the recorded model is no longer a chain member, the artifact IS stale and re-derives.
    _build(tmp_path)
    cache = ResponseCache(tmp_path / "db" / "llm_cache.sqlite")
    enrich.enrich_sources(tmp_path, client=LLMClient({"anthropic": FakeAdapter()}, cache=cache),
                          model_ref="anthropic:h", jobs_db=tmp_path / "db" / "jobs.sqlite")
    local = FakeAdapter()
    c2 = LLMClient({"anthropic": FakeAdapter(), "local": local}, cache=cache)
    r = enrich.enrich_sources(tmp_path, client=c2, model_ref="local:x",   # anthropic:h dropped
                              jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert r["enriched"] == 2 and local.calls == 2   # re-derived on the new (only) chain member


def test_local_configured_but_unreachable_selects_local_then_errors_no_fallback(tmp_path):
    # ADR-0063 NB3: available() = base URL CONFIGURED, not a reachability probe. A configured-but-down
    # local server is SELECTED; its call then fails -> ParseError/skip per no-failover (decision 4/5),
    # NOT an automatic hosted fallback.
    from app.llm.adapters import AdapterError
    _build(tmp_path)

    class UnreachableLocal(FakeAdapter):        # available()=True (configured) but every call errors
        def parse(self, messages, schema, model_id, *, max_tokens):
            raise AdapterError("connection refused")

    hosted = FakeAdapter()
    client = LLMClient({"local": UnreachableLocal(), "anthropic": hosted},
                       cache=ResponseCache(tmp_path / "db" / "llm_cache.sqlite"), max_retries=0)
    r = enrich.enrich_sources(tmp_path, client=client, model_ref="local:x,anthropic:h",
                              jobs_db=tmp_path / "db" / "jobs.sqlite")
    assert r["enriched"] == 0 and r["errors"] == 2   # both sources errored on the selected local model
    assert hosted.calls == 0                          # no hosted fallback — operator fixes + reruns
