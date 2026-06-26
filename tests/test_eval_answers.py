"""ADR-0042 real-vault answer-quality eval: worker core (key-free) + endpoint gate + end-to-end.

The deterministic scorer + runner are tested with a fake query_fn (no LLM key). The endpoints are
tested over a TestClient: the cost/key gate (confirm_cost/dry_run/503), and one end-to-end run with a
fake cite-all LLM client over a searchable vault — asserting privacy (no prose/prompt/absolute paths),
save:false, and read-only-over-vault-SoT.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.backend import keyword_index
from app.backend import main as main_module
from app.backend.config import get_settings
from app.workers import eval_answers
from app.workers.eval_answers import Case

SRC_A = "src_aaaaaaaaaaaaaaaa"
SRC_B = "src_bbbbbbbbbbbbbbbb"
GHOST = "src_00000000deadbeef"


# --- worker core (key-free) ------------------------------------------------


def _case(**over) -> Case:
    base = dict(id="c1", category="grounded", question="q?", mode="auto",
                expected_source_ids=[SRC_A], forbidden_source_ids=[], should_abstain=False,
                expect_answer=True)
    base.update(over)
    return Case(**base)


def _sig(**over):
    base = dict(abstained=False, cited_source_ids=[SRC_A], unsourced_count=0,
                security_rejected_count=0)
    base.update(over)
    return base


def test_load_corpus_parses_cases():
    text = """version: 1
cases:
  -
    id: g1
    category: grounded
    question: what is x?
    mode: auto
    expected_source_ids:
      - src_aaaaaaaaaaaaaaaa
    should_abstain: false
  -
    id: a1
    category: abstention
    question: unknown?
    should_abstain: true
"""
    cases = eval_answers.load_corpus(text)
    assert [c.id for c in cases] == ["g1", "a1"]
    assert cases[0].expected_source_ids == [SRC_A] and cases[0].expect_answer is True
    assert cases[1].should_abstain is True and cases[1].expect_answer is False  # derived default


def test_validate_case_shape_and_existence():
    # canonical-shape failure (always checked) — the raw (untrusted) value is NOT echoed, only field+index
    errs = eval_answers.validate_case(_case(expected_source_ids=["nope"]))
    assert "expected_source_ids[0] not canonical (src_<16 hex>)" in errs
    assert not any("nope" in e for e in errs)
    # existence failure when known set provided (BOTH expected and forbidden checked)
    errs = eval_answers.validate_case(_case(forbidden_source_ids=[GHOST]), known_source_ids={SRC_A})
    assert any("forbidden_source_id" in e and "not in vault" in e for e in errs)
    # contradictory abstain + expected
    assert any("contradictory" in e for e in eval_answers.validate_case(
        _case(should_abstain=True)))
    # valid
    assert eval_answers.validate_case(_case(), known_source_ids={SRC_A}) == []


def test_score_case_predicates_pass_and_fail():
    ok = eval_answers.score_case(_case(), _sig())
    assert ok["pass"] and ok["fail_reasons"] == []
    assert ok["citation_recall"] == 1.0 and ok["citation_precision"] == 1.0
    # missing expected source -> expected_cited fails
    miss = eval_answers.score_case(_case(), _sig(cited_source_ids=[SRC_B]))
    assert not miss["pass"] and "expected_cited" in miss["fail_reasons"]
    # forbidden cited -> fails
    forb = eval_answers.score_case(_case(forbidden_source_ids=[SRC_B]),
                                   _sig(cited_source_ids=[SRC_A, SRC_B]))
    assert "forbidden_not_cited" in forb["fail_reasons"]
    # abstain mismatch + unsourced + security
    bad = eval_answers.score_case(
        _case(), _sig(abstained=True, cited_source_ids=[], unsourced_count=2, security_rejected_count=1))
    assert set(bad["fail_reasons"]) >= {"expected_cited", "abstain_match", "no_unsourced_claims",
                                        "no_security_rejections"}


def test_score_abstention_case_passes_when_expected():
    ab = eval_answers.score_case(
        _case(expected_source_ids=[], should_abstain=True, expect_answer=False),
        _sig(abstained=True, cited_source_ids=[]))
    assert ab["pass"]


def test_run_eval_limit_skip_and_cache_counts():
    cases = [_case(id="g1"), _case(id="g2"), _case(id="bad", expected_source_ids=["nope"])]
    signals = {"g1": _sig(cache_hit=False), "g2": _sig(cache_hit=True)}
    rep = eval_answers.run_eval(cases, lambda c: signals[c.id], limit=5, known_source_ids={SRC_A})
    assert rep["n_corpus"] == 3 and rep["n_valid"] == 2 and rep["n_run"] == 2 and rep["n_skipped"] == 1
    assert rep["n_passed"] == 2 and rep["cache_hits"] == 1 and rep["cache_misses"] == 1
    assert rep["skipped"][0]["id"] == "bad"
    # limit clamps how many valid cases run
    rep2 = eval_answers.run_eval(cases, lambda c: signals.get(c.id, _sig()), limit=1,
                                 known_source_ids={SRC_A})
    assert rep2["n_run"] == 1


# --- endpoint: cost / key gate ---------------------------------------------


@pytest.fixture
def bare_client(tmp_path, monkeypatch):
    settings = get_settings(tmp_path)
    (tmp_path / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "settings", settings)
    return TestClient(main_module.app), tmp_path


def _write_corpus(tmp_path: Path, text: str) -> None:
    (tmp_path / "evals").mkdir(parents=True, exist_ok=True)
    (tmp_path / "evals" / "golden_answers.local.yaml").write_text(text, encoding="utf-8")


def test_missing_corpus_is_404_with_example_path(bare_client):
    client, _ = bare_client
    r = client.post("/evals/run", json={"dry_run": True})
    assert r.status_code == 404 and "golden_answers.example.yaml" in r.json()["detail"]


def test_missing_confirm_cost_is_400_before_any_llm(bare_client):
    client, tmp_path = bare_client
    _write_corpus(tmp_path, "version: 1\ncases:\n  -\n    id: g\n    question: q\n")
    r = client.post("/evals/run", json={})
    assert r.status_code == 400 and "confirm_cost" in r.json()["detail"]


def test_dry_run_validates_corpus_without_llm(bare_client):
    client, tmp_path = bare_client
    # one valid (existing manifest) + one invalid (ghost source) case
    (tmp_path / "raw" / "manifests" / f"{SRC_A}.json").write_text(
        json.dumps({"source_id": SRC_A}), encoding="utf-8")
    _write_corpus(tmp_path, f"""version: 1
cases:
  -
    id: ok
    question: q
    expected_source_ids:
      - {SRC_A}
  -
    id: ghost
    question: q2
    expected_source_ids:
      - {GHOST}
""")
    r = client.post("/evals/run", json={"dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "dry_run"
    assert body["dry_run"]["n_valid"] == 1 and body["dry_run"]["would_run"] == 1
    assert any(s["id"] == "ghost" for s in body["dry_run"]["skipped"])


def test_confirm_cost_without_llm_key_is_503(bare_client, monkeypatch):
    client, tmp_path = bare_client
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _write_corpus(tmp_path, "version: 1\ncases:\n  -\n    id: g\n    question: q\n")
    r = client.post("/evals/run", json={"confirm_cost": True})
    assert r.status_code == 503 and "configured LLM" in r.json()["detail"]


# --- end-to-end run with a fake cite-all client + a searchable vault --------


class _CiteAllClient:
    def provider_available(self, model_ref):
        return True

    def parse(self, messages, schema, model_ref, **kwargs):
        pack = json.loads(messages[-1]["content"].split("EVIDENCE:\n", 1)[1])
        return {"claims": [{"text": f"Grounded claim for {e['evidence_id']}.",
                            "evidence_ids": [e["evidence_id"]]} for e in pack]}


def _write_source(root: Path, sid: str, texts: list[str]) -> None:
    recs, md = [], ""
    for i, text in enumerate(texts):
        start = len(md)
        md += text
        recs.append({"chunk_id": f"{sid}::{i:04d}", "source_id": sid, "ordinal": i, "kind": "prose",
                     "heading_path": [], "section": None, "text": text, "char_start": start,
                     "char_end": start + len(text), "page": 1, "page_end": 1,
                     "table_reference": None, "sheet_reference": None})
        md += "\n\n"
    (root / "normalized" / "chunks").mkdir(parents=True, exist_ok=True)
    (root / "normalized" / "chunks" / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    (root / "normalized" / "markdown").mkdir(parents=True, exist_ok=True)
    (root / "normalized" / "markdown" / f"{sid}.md").write_text(md, encoding="utf-8")
    (root / "raw" / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "raw" / "manifests" / f"{sid}.json").write_text(
        json.dumps({"source_id": sid}), encoding="utf-8")
    sp = root / "wiki" / "Sources" / f"{sid}.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(f"---\ntype: source\nsource_id: {sid}\ntitle: Doc\nstatus: active\nlanguage: en\n"
                  "---\n\n# Doc\n\n> [!summary]\n> doc\n", encoding="utf-8")


@pytest.fixture
def vault_client(tmp_path, monkeypatch):
    _write_source(tmp_path, SRC_A, ["Summary-first navigation reads the index before opening pages.",
                                    "Citations anchor every generated claim to raw evidence."])
    keyword_index.reindex(tmp_path, force=True)
    settings = get_settings(tmp_path)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "_eval_client", lambda fresh: (_CiteAllClient(), None))
    return TestClient(main_module.app), tmp_path


def test_end_to_end_run_scores_grounded_and_abstention(vault_client):
    client, tmp_path = vault_client
    _write_corpus(tmp_path, f"""version: 1
cases:
  -
    id: grounded
    category: grounded
    question: summary first navigation index
    expected_source_ids:
      - {SRC_A}
    should_abstain: false
  -
    id: abstain
    category: abstention
    question: zzzznomatchquux
    should_abstain: true
    expect_answer: false
""")
    r = client.post("/evals/run", json={"confirm_cost": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["summary"]["n_run"] == 2 and body["summary"]["n_passed"] == 2
    assert body["meta"]["model_ref"] and body["summary"]["cache_mode"] == "cached"
    # report file written under the gitignored answers dir
    rep_path = tmp_path / body["report_path"]
    assert rep_path.exists() and rep_path.parts[-2] == "answers"

    # PRIVACY: the stored artifact has no answer prose, no evidence text, no absolute path
    raw = rep_path.read_text(encoding="utf-8")
    assert "Grounded claim for" not in raw and "Summary-first navigation" not in raw
    assert str(tmp_path) not in raw and "/home/" not in raw

    # save:false -> no query page written
    assert not (tmp_path / "wiki" / "Queries").exists()

    # GET /evals/results lists the run and reads it back (key-free)
    runs = client.get("/evals/results").json()["runs"]
    assert len(runs) == 1 and runs[0]["n_passed"] == 2
    one = client.get("/evals/results", params={"run_id": runs[0]["run_id"]}).json()["report"]
    assert one["n_run"] == 2


def test_results_run_id_rejects_traversal(vault_client):
    client, _ = vault_client
    assert client.get("/evals/results", params={"run_id": "../../etc/passwd"}).status_code == 400
    assert client.get("/evals/results", params={"run_id": "nope"}).status_code == 404


def test_counting_cache_counts_hits_and_misses(tmp_path):
    cache = main_module._CountingCache(tmp_path / "c.sqlite")
    assert cache.get("missing") is None and cache.misses == 1 and cache.hits == 0
    cache.put("k", provider="p", model_id="m", response={"x": 1},
              created_at="2026-01-01T00:00:00+00:00")
    assert cache.get("k") == {"x": 1} and cache.hits == 1 and cache.misses == 1


class _CacheUsingClient:
    """A fake that replays from / writes to the injected cache like the real client, so the eval's
    counting cache records real hits/misses."""

    def __init__(self, cache):
        self.cache = cache

    def provider_available(self, model_ref):
        return True

    def parse(self, messages, schema, model_ref, **kwargs):
        from app.llm.cache import cache_key
        key = cache_key(messages, model_ref, schema)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        pack = json.loads(messages[-1]["content"].split("EVIDENCE:\n", 1)[1])
        result = {"claims": [{"text": f"Grounded claim for {e['evidence_id']}.",
                              "evidence_ids": [e["evidence_id"]]} for e in pack]}
        self.cache.put(key, provider="fake", model_id="fake", response=result,
                       created_at="2026-01-01T00:00:00+00:00")
        return result


def _cache_vault(tmp_path, monkeypatch):
    _write_source(tmp_path, SRC_A, ["Summary-first navigation reads the index before opening pages."])
    keyword_index.reindex(tmp_path, force=True)
    settings = get_settings(tmp_path)
    monkeypatch.setattr(main_module, "settings", settings)
    _write_corpus(tmp_path, f"version: 1\ncases:\n  -\n    id: g\n    question: summary navigation index\n"
                            f"    expected_source_ids:\n      - {SRC_A}\n")
    return TestClient(main_module.app)


def test_cached_run_records_misses_then_hits(tmp_path, monkeypatch):
    # Each run builds a fresh counting cache over the SAME db file (like the endpoint does), so the first
    # run misses + populates and a second identical run replays as a hit (ADR-0042 decision 4).
    monkeypatch.setattr(main_module, "_eval_client",
                        lambda fresh: (lambda c: (_CacheUsingClient(c), c))(
                            main_module._CountingCache(main_module.settings.response_cache_path)))
    client = _cache_vault(tmp_path, monkeypatch)
    first = client.post("/evals/run", json={"confirm_cost": True}).json()
    assert first["summary"]["cache_mode"] == "cached"
    assert first["summary"]["cache_misses"] == 1 and first["summary"]["cache_hits"] == 0
    second = client.post("/evals/run", json={"confirm_cost": True}).json()
    assert second["summary"]["cache_hits"] == 1 and second["summary"]["cache_misses"] == 0


def test_fresh_run_uses_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_eval_client", lambda fresh: (_CiteAllClient(), None))
    client = _cache_vault(tmp_path, monkeypatch)
    body = client.post("/evals/run", json={"confirm_cost": True, "fresh": True}).json()
    assert body["summary"]["cache_mode"] == "fresh"
    assert body["summary"]["cache_hits"] == 0 and body["summary"]["cache_misses"] == 0
    assert not (tmp_path / "db" / "llm_cache.sqlite").exists()  # cacheless: nothing written


def test_same_second_runs_produce_distinct_files(vault_client, monkeypatch):
    client, tmp_path = vault_client
    _write_corpus(tmp_path, f"version: 1\ncases:\n  -\n    id: g\n    question: summary navigation\n"
                            f"    expected_source_ids:\n      - {SRC_A}\n")
    monkeypatch.setattr(main_module.manifests, "iso_now", lambda: "2026-06-26T12:00:00+00:00")
    p1 = client.post("/evals/run", json={"confirm_cost": True}).json()["report_path"]
    p2 = client.post("/evals/run", json={"confirm_cost": True}).json()["report_path"]
    assert p1 != p2 and (tmp_path / p1).exists() and (tmp_path / p2).exists()


def test_invalid_mode_is_curation_skip(bare_client):
    client, tmp_path = bare_client
    _write_corpus(tmp_path, "version: 1\ncases:\n  -\n    id: g\n    question: q\n    mode: graph\n")
    dry = client.post("/evals/run", json={"dry_run": True}).json()["dry_run"]
    assert dry["n_valid"] == 0
    assert any("unsupported mode" in r for s in dry["skipped"] for r in s["reasons"])


def test_invalid_source_id_value_not_echoed_in_report(bare_client):
    # A path-like invalid id must NOT appear verbatim in the (durable) skip report — only field+index.
    client, tmp_path = bare_client
    _write_corpus(tmp_path, "version: 1\ncases:\n  -\n    id: g\n    question: q\n"
                            "    expected_source_ids:\n      - /home/secret/leak\n")
    body = client.post("/evals/run", json={"dry_run": True})
    assert "/home/secret" not in body.text
    reasons = body.json()["dry_run"]["skipped"][0]["reasons"]
    assert any("not canonical" in r for r in reasons)


def test_llm_failure_aborts_with_503_and_no_report(tmp_path, monkeypatch):
    from app.llm.client import ParseError

    class _FailingClient:
        def provider_available(self, model_ref):
            return True

        def parse(self, messages, schema, model_ref, **kwargs):
            raise ParseError("synthesis blew up")

    monkeypatch.setattr(main_module, "_eval_client", lambda fresh: (_FailingClient(), None))
    client = _cache_vault(tmp_path, monkeypatch)  # searchable vault -> evidence -> parse is reached
    r = client.post("/evals/run", json={"confirm_cost": True})
    assert r.status_code == 503
    assert not list((tmp_path / "evals" / "reports" / "answers").glob("*")) \
        if (tmp_path / "evals" / "reports" / "answers").exists() else True


def test_malformed_client_setup_maps_to_controlled_503(tmp_path, monkeypatch):
    from app.llm.client import ConfigError

    def boom(fresh):
        raise ConfigError("unknown provider in QUERY_MODEL")
    monkeypatch.setattr(main_module, "_eval_client", boom)
    client = _cache_vault(tmp_path, monkeypatch)
    r = client.post("/evals/run", json={"confirm_cost": True})
    assert r.status_code == 503 and "temporarily unavailable" in r.json()["detail"]
    answers = tmp_path / "evals" / "reports" / "answers"
    assert not (answers.exists() and list(answers.glob("*")))  # no report written


def test_run_does_not_mutate_vault_sot(vault_client):
    client, tmp_path = vault_client
    _write_corpus(tmp_path, f"version: 1\ncases:\n  -\n    id: g\n    question: summary navigation\n"
                            f"    expected_source_ids:\n      - {SRC_A}\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*")
              if p.is_file() and "evals/reports" not in str(p) and "db/llm_cache" not in str(p)}
    client.post("/evals/run", json={"confirm_cost": True})
    after = {p: p.read_bytes() for p in tmp_path.rglob("*")
             if p.is_file() and "evals/reports" not in str(p) and "db/llm_cache" not in str(p)}
    # no vault-SoT file changed/added (eval artifacts + cache excluded above)
    assert {k: v for k, v in before.items()} == {k: v for k, v in after.items() if k in before}
    assert not [p for p in after if p not in before and "evals" not in str(p) and "db" not in str(p)]
