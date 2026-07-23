"""Phase 5-4 query-answer eval harness (ADR-0034 decision 7).

Loads the golden cases from ``evals/golden_questions.yaml`` and runs each end-to-end through
``POST /query`` (TestClient) against a small programmatic **keyword-only** fixture vault, with a
deterministic fake ``LLMClient`` injected (``main._query_client``). The real ``ground_citation`` gate
runs; assertions are STRUCTURAL — cited-answering behaviour, not semantic relevance.

Key-free and **not LanceDB-gated**: the fixture configures no embedder, so a ``mode=auto`` conceptual
question degrades *silently* to keyword evidence (no degradation note) — Phase 5 evals gate cited
answering, not vector availability (that is the 4e-3 retrieval-eval's job). The fake maps a question to
``{claims:[{text, evidence_ids}]}`` by reading the evidence pack out of the prompt and executing the
per-case directive.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.backend import keyword_index  # noqa: E402
from app.backend import main as main_module  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.backend.policy import load_yaml  # noqa: E402
from app.workers.citations import ground_citation  # noqa: E402

EVALS_FILE = ROOT / "evals" / "golden_questions.yaml"
SRC_A = "src_aaaaaaaaaaaaaaaa"
SRC_B = "src_bbbbbbbbbbbbbbbb"
CATEGORIES = {"grounded_answer", "multi_citation", "abstention", "ungrounded", "security", "save"}


def _write_source(root: Path, sid: str, texts: list[str]) -> None:
    """Write a source's chunks + matching normalized Markdown + manifest + active source page, so
    run_search retrieves citable evidence, answer_query grounds quotes, and saved pages validate."""
    recs, md = [], ""
    for i, text in enumerate(texts):
        start = len(md)
        md += text
        recs.append({"chunk_id": f"{sid}::{i:04d}", "source_id": sid, "ordinal": i, "kind": "prose",
                     "heading_path": [], "section": None, "text": text, "char_start": start,
                     "char_end": start + len(text), "page": 1, "page_end": 1,
                     "table_reference": None, "sheet_reference": None})
        md += "\n\n"
    ch = root / "normalized" / "chunks" / f"{sid}.jsonl"
    ch.parent.mkdir(parents=True, exist_ok=True)
    ch.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    mdp = root / "normalized" / "markdown" / f"{sid}.md"
    mdp.parent.mkdir(parents=True, exist_ok=True)
    mdp.write_text(md, encoding="utf-8")
    man = root / "raw" / "manifests" / f"{sid}.json"
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps({"source_id": sid}), encoding="utf-8")
    sp = root / "wiki" / "Sources" / f"{sid}.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(f"---\ntype: source\nsource_id: {sid}\ntitle: Doc\nstatus: active\nlanguage: en\n"
                  "---\n\n# Doc\n\n> [!summary]\n> doc\n", encoding="utf-8")


@pytest.fixture(scope="module")
def vault(tmp_path_factory):
    root = tmp_path_factory.mktemp("query_evals")
    _write_source(root, SRC_A, ["Summary-first navigation reads the index before opening pages.",
                                "Citations anchor every generated claim to raw evidence."])
    _write_source(root, SRC_B, ["Graph navigation traverses edges between concepts."])
    keyword_index.reindex(root, force=True)
    return root


class _FakeDirectiveClient:
    """Deterministic, not 'smart': reads the evidence pack from the prompt and emits claims per the
    per-case directive. The real grounding gate decides what survives."""

    def __init__(self, strategy: str, ids: list[str] | None = None):
        self.strategy = strategy
        self.ids = ids or []

    def provider_available(self, model_ref):
        return True

    def resolve_run_model(self, chain):  # ADR-0063 chain contract
        ref = chain.split(",")[0].strip()
        return ref, self.provider_available(ref)

    def chain_available(self, chain):
        return self.resolve_run_model(chain)[1]

    def parse(self, messages, schema, model_ref, **kwargs):
        pack = json.loads(messages[-1]["content"].split("EVIDENCE:\n", 1)[1])
        eids = [e["evidence_id"] for e in pack]
        if self.strategy == "cite_all":
            return {"claims": [{"text": f"Grounded claim for {e}.", "evidence_ids": [e]} for e in eids]}
        if self.strategy == "cite":
            return {"claims": [{"text": "Multi-cited claim.",
                                "evidence_ids": [i for i in self.ids if i in eids]}]}
        if self.strategy == "bogus":
            return {"claims": [{"text": "Unsupported aside.", "evidence_ids": ["e999"]}]}
        if self.strategy == "path_leak":
            first = eids[:1]
            return {"claims": [{"text": "Grounded claim.", "evidence_ids": first},
                               {"text": "See /home/secret.txt for more.", "evidence_ids": first}]}
        if self.strategy == "no_claims":
            return {"claims": []}
        raise ValueError(f"unknown fake strategy: {self.strategy}")


def _client_for(vault, monkeypatch, fake):
    # Force keyword-only explicitly — do NOT trust ambient env. If the dev/CI environment has
    # EMBEDDING_* set + LanceDB installed, mode=auto would try vector capability against the
    # index-less temp vault and emit a degradation note. Null the embedder so vector never runs.
    settings = dataclasses.replace(get_settings(vault), embedding_base_url=None,
                                   embedding_model_ref=None, embedding_api_key=None)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "_query_client", lambda: fake)
    return TestClient(main_module.app)


def _load_cases():
    return load_yaml(EVALS_FILE.read_text(encoding="utf-8"))["cases"]


def _assert_global_invariants(body, resp, vault):
    # Run for every (non-error) case regardless of category.
    assert body["notes"] == []                       # silent keyword-only degradation; no LanceDB leak
    assert str(vault) not in resp.text               # no server/generated filesystem path
    md_dir = vault / "normalized" / "markdown"
    for claim in body["claims"]:
        assert claim["citations"], "answer-body claim must be cited (zero-unsourced body)"
        for c in claim["citations"]:
            md = (md_dir / f"{c['source_id']}.md").read_text(encoding="utf-8")
            assert ground_citation(c, md, require_quote=True) == []  # every body citation grounds


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_question(case, vault, monkeypatch):
    fake = _FakeDirectiveClient(case["fake"]["strategy"], case["fake"].get("ids"))
    client = _client_for(vault, monkeypatch, fake)
    payload = {"question": case["question"], "mode": case.get("mode", "auto"),
               "save": bool(case.get("save"))}
    resp = client.post("/query", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    exp = case["expect"]

    assert body["abstained"] is exp["abstained"]
    if exp["abstained"]:
        assert body["answer"] == "No source found in vault."  # pin the answer-layer fallback contract
    if "cited_source_ids" in exp:
        assert {c["source_id"] for c in body["citations"]} == set(exp["cited_source_ids"])
    if exp.get("must_include_citations"):
        assert body["citations"]
    elif "must_include_citations" in exp:  # False -> abstained answers carry no citations
        assert body["citations"] == []
    # Diagnostic counts default to 0 unless a case explicitly expects otherwise (so an unexpected
    # count can never silently slip through).
    assert body["unsourced_count"] == exp.get("unsourced_count", 0)
    assert body["security_rejected_count"] == exp.get("security_rejected_count", 0)

    if case.get("save"):
        qid = body["query_id"]
        assert qid and body["navigation_stale"] is True
        assert (vault / "wiki" / "Queries" / f"{qid}.md").exists()
        r = subprocess.run([sys.executable, "scripts/validate_citations.py", str(vault)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr  # saved page grounds + validates

    _assert_global_invariants(body, resp, vault)


def test_eval_file_covers_all_categories():
    assert {c["category"] for c in _load_cases()} == CATEGORIES


def test_query_evals_stay_keyword_only_even_with_embedding_env(vault, monkeypatch):
    # Guard: even if the environment advertises an embedder + LanceDB, the eval forces keyword-only,
    # so a conceptual mode=auto query never attempts vector and emits no degradation note.
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("EMBEDDING_MODEL_REF", "bge-m3")
    client = _client_for(vault, monkeypatch, _FakeDirectiveClient("cite_all"))
    body = client.post("/query", json={"question": "summary first navigation", "mode": "auto"}).json()
    assert body["notes"] == [] and "vector" not in body["retrieval_path"]
