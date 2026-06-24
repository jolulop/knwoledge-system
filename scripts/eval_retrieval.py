#!/usr/bin/env python3
"""Retrieval RELEVANCE eval (ADR-0038) — opt-in, NOT a CI gate.

Measures whether ``run_search`` surfaces the right citable chunks/sources for a query, scored against
human-curated source-level judgments (``evals/golden_retrieval_relevance.yaml``) over the committed
corpus (``evals/corpus/``). Metrics: recall@k / MRR / hit@k (= success@k) on the fused ``evidence[]``
(mode auto), plus the negative signals neg@k + discrimination for disambiguation cases.

Requires the **configured real embedder** (the `local_http` seam) — no `ANTHROPIC_API_KEY` is needed
(relevance scoring touches no LLM). Builds keyword + vector indexes over the corpus with an **empty
graph** (so this stays separate from graph-boost tuning). The fake-embedder *structural* eval
(``tests/test_retrieval_evals.py``) remains the key-free CI gate; this one never runs in CI.

Usage:
    uv run python scripts/eval_retrieval.py            # over the committed corpus
    uv run python scripts/eval_retrieval.py --vault /path/to/vault --out report.md -k 5 -k 10
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import embeddings, graph, keyword_index, search, vector_index
from app.backend.config import get_settings
from app.backend.manifests import list_manifests
from app.backend.policy import load_retrieval_policy, load_yaml

CORPUS_DIR = ROOT / "evals" / "corpus"
GOLDEN = ROOT / "evals" / "golden_retrieval_relevance.yaml"


# --- pure scoring helpers (key-free; unit-tested) --------------------------------------------------

def evidence_sources(result: dict[str, Any]) -> list[str]:
    """Unique source_ids in fused-evidence order (first occurrence wins)."""
    out: list[str] = []
    for e in result.get("evidence", []):
        sid = e.get("source_id")
        if sid and sid not in out:
            out.append(sid)
    return out


def score_case(ranked: list[str], relevant: set[str], irrelevant: set[str], ks: list[int]) -> dict[str, Any]:
    """Per-query, source-level: recall@k / hit@k, plus the negative signals (ADR-0038):

    `neg@k` = a listed `irrelevant` distractor appeared in top-k. `discriminated` = the first relevant
    source ranks *above* the first irrelevant one (the cleaner disambiguation signal); `has_neg` flags
    cases that actually carry distractors so the negative metrics aggregate over the right subset.
    """
    first = next((i + 1 for i, s in enumerate(ranked) if s in relevant), None)
    first_irr = next((i + 1 for i, s in enumerate(ranked) if s in irrelevant), None)
    out: dict[str, Any] = {
        "first_rank": first, "first_irrel_rank": first_irr, "rr": (1.0 / first if first else 0.0),
        "has_neg": bool(irrelevant),
        "discriminated": 1.0 if (first is not None and (first_irr is None or first < first_irr)) else 0.0,
    }
    for k in ks:
        topk = ranked[:k]
        found = sum(1 for s in relevant if s in topk)
        out[f"recall@{k}"] = found / len(relevant) if relevant else 0.0
        out[f"hit@{k}"] = 1.0 if found else 0.0
        out[f"neg@{k}"] = 1.0 if any(s in topk for s in irrelevant) else 0.0
    return out


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# --- vault build + run -----------------------------------------------------------------------------

def _build_corpus_vault(corpus_dir: Path, work_root: Path, embedder: Any, settings: Any) -> None:
    from app.workers import extract, intake
    inbox = work_root / "raw" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for md in sorted(corpus_dir.glob("*.md")):
        shutil.copy(md, inbox / md.name)
    jobs = work_root / "db" / "jobs.sqlite"
    intake.scan_inbox(work_root, jobs_db=jobs)
    extract.extract_sources(work_root, jobs_db=jobs)
    keyword_index.reindex(work_root, force=True)
    vector_index.reindex(work_root, embedder, embedding_model_ref=settings.embedding_model_ref,
                         distance_metric=settings.embedding_distance_metric, force=True)
    graph.init_db(work_root / "db" / "graph.sqlite")  # empty graph: relevance scores evidence only


def _filename_to_source(manifests_dir: Path) -> dict[str, str]:
    return {m["original_filename"]: m["source_id"]
            for m in list_manifests(manifests_dir)
            if m.get("original_filename") and m.get("source_id")}


def _resolve(names: list[str], fmap: dict[str, str]) -> tuple[set[str], list[str]]:
    """Map golden filenames -> source_ids; return (resolved set, unresolved filenames)."""
    resolved, missing = set(), []
    for n in names or []:
        sid = fmap.get(n)
        (resolved.add(sid) if sid else missing.append(n))
    return resolved, missing


def run(root: Path, settings: Any, embedder: Any, cases: list[dict], ks: list[int],
        *, gconn: Any, graph_present: bool) -> dict[str, Any]:
    """Score each golden case over `run_search` evidence. The caller owns `gconn` (an empty graph for the
    committed corpus; the vault's real graph, or a throwaway empty one, for `--vault`)."""
    metric = settings.embedding_distance_metric
    policy = load_retrieval_policy(settings.retrieval_policy_path)
    kconn = keyword_index.connect(root / keyword_index.DB_RELPATH)
    fmap = _filename_to_source(root / "raw" / "manifests")
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    try:
        for case in cases:
            relevant, miss_r = _resolve(case.get("relevant"), fmap)
            if not relevant:  # unresolved/empty judgments — a curation bug for the corpus; skip + report
                skipped.append(f"{case.get('id')} (unresolved: {miss_r})")
                continue
            irrelevant, _ = _resolve(case.get("irrelevant"), fmap)
            q = case["query"]

            def vector_search(*, limit: int, _q: str = q) -> list[dict[str, Any]]:
                return vector_index.search(root, embedder.embed([_q])[0], limit=limit, metric=metric)

            res = search.run_search(q=q, mode=case.get("mode", "auto"), keyword_conn=kconn,
                                    graph_conn=gconn, policy=policy, vector_search=vector_search)
            ranked = evidence_sources(res)
            rows.append({"id": case.get("id"), "category": case.get("category", "?"),
                         **score_case(ranked, relevant, irrelevant, ks)})
    finally:
        kconn.close()
    return {"rows": rows, "skipped": skipped, "rrf_k": policy.cap("rrf_k"),
            "graph_present": graph_present}


# --- report ----------------------------------------------------------------------------------------

def _aggregate(rows: list[dict[str, Any]], ks: list[int]) -> dict[str, Any]:
    neg_rows = [r for r in rows if r.get("has_neg")]
    agg: dict[str, Any] = {"MRR": _mean([r["rr"] for r in rows])}
    for k in ks:
        agg[f"recall@{k}"] = _mean([r[f"recall@{k}"] for r in rows])
        agg[f"hit@{k}"] = _mean([r[f"hit@{k}"] for r in rows])
        agg[f"neg@{k}"] = _mean([r[f"neg@{k}"] for r in neg_rows])  # over distractor-bearing cases only
    agg["discrimination"] = _mean([r["discriminated"] for r in neg_rows]) if neg_rows else None
    agg["neg_n"] = len(neg_rows)
    return agg


def render_report(result: dict[str, Any], *, settings: Any, ks: list[int], source_label: str) -> str:
    rows = result["rows"]
    ks_cols = [f"recall@{k}" for k in ks] + [f"hit@{k}" for k in ks] + [f"neg@{k}" for k in ks]
    agg = _aggregate(rows, ks)
    disc = "n/a" if agg["discrimination"] is None else f"{agg['discrimination']:.3f}"
    lines = ["# Retrieval relevance report (ADR-0038)", "",
             f"- generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             f"- corpus: {source_label}",
             f"- embedding_model_ref: {settings.embedding_model_ref}",
             f"- index: vector_schema={vector_index.INDEX_SCHEMA_VERSION} "
             f"embed_code={vector_index.EMBED_CODE_VERSION} metric={settings.embedding_distance_metric}",
             f"- rrf_k: {result['rrf_k']}   graph_present: {str(result['graph_present']).lower()}   "
             "graph_boosts: none",
             f"- cases scored: {len(rows)}   skipped: {len(result['skipped'])}   "
             f"negative cases: {agg['neg_n']}", ""]
    metrics = ["MRR", *[f"recall@{k}" for k in ks], *[f"hit@{k}" for k in ks], *[f"neg@{k}" for k in ks]]
    lines += ["## Aggregate", "", "| metric | value |", "|---|---|",
              *(f"| {m} | {agg[m]:.3f} |" for m in metrics),
              f"| discrimination (rel<irrel, {agg['neg_n']} neg cases) | {disc} |", ""]
    cats = sorted({r["category"] for r in rows})
    lines += ["## By category", "", "| category | n | " + " | ".join(["MRR", *ks_cols]) + " |",
              "|" + "---|" * (len(ks_cols) + 3)]
    for c in cats:
        sub = [r for r in rows if r["category"] == c]
        a = _aggregate(sub, ks)
        lines.append(f"| {c} | {len(sub)} | " + " | ".join(
            f"{a[m]:.3f}" for m in ["MRR", *ks_cols]) + " |")
    lines += ["", "## Per query", "", "| id | category | first_rank | " + " | ".join(ks_cols) + " |",
              "|" + "---|" * (len(ks_cols) + 3)]
    for r in rows:
        lines.append(f"| {r['id']} | {r['category']} | {r['first_rank'] if r['first_rank'] else '-'} | "
                     + " | ".join(f"{r[m]:.2f}" for m in ks_cols) + " |")
    neg_rows = [r for r in rows if r.get("has_neg")]
    if neg_rows:
        lines += ["", "## Discrimination (negative cases — relevant must rank above the distractor)", "",
                  "| id | first_relevant | first_irrelevant | relevant_wins |",
                  "|---|---|---|---|"]
        for r in neg_rows:
            lines.append(f"| {r['id']} | {r['first_rank'] or '-'} | {r['first_irrel_rank'] or '-'} | "
                         f"{'yes' if r['discriminated'] else 'NO'} |")
    if result["skipped"]:
        lines += ["", "## Skipped (unresolved judgments)", "", *(f"- {s}" for s in result["skipped"])]
    return "\n".join(lines) + "\n"


def _vector_problems(root: Path, settings: Any) -> list[str]:
    """Reuse /search's vector-index freshness checks (ADR-0033): for `--vault` the index must be present,
    coherent, model-matched, and not stale — otherwise the eval would compare a stale ranking."""
    expected = vector_index.VectorMeta(
        embedding_model_ref=settings.embedding_model_ref,
        embedding_code_version=vector_index.EMBED_CODE_VERSION,
        distance_metric=settings.embedding_distance_metric,
        dimension=settings.embedding_dimension,
        index_schema_version=vector_index.INDEX_SCHEMA_VERSION)
    st = vector_index.status(root, expected=expected)
    if not st.present:
        return ["no vector index"]
    if not st.coherent:
        return ["incoherent/model-mismatched: " + "; ".join(st.issues)]
    if st.stale_or_missing_chunks or st.removed_chunks:
        return [f"stale ({st.stale_or_missing_chunks} changed/missing, {st.removed_chunks} removed)"]
    return []


def _open_graph(root: Path, *, is_vault: bool) -> tuple[Any, bool, Any]:
    """Return (gconn, graph_present, tmp). For `--vault` with no graph, use a throwaway empty graph so we
    never write `db/graph.sqlite` into the operator's vault. `graph_present` reflects the real state."""
    gpath = root / "db" / "graph.sqlite"
    if is_vault and not gpath.exists():
        tmp = tempfile.TemporaryDirectory(prefix="ks_reteval_g_")
        gp = Path(tmp.name) / "graph.sqlite"
        graph.init_db(gp)
        return graph.connect(gp), False, tmp
    if not gpath.exists():
        graph.init_db(gpath)
    gconn = graph.connect(gpath)
    return gconn, bool(graph.node_ids(gconn)), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieval relevance eval (ADR-0038, opt-in).")
    parser.add_argument("--vault", type=Path, help="run over an existing vault root instead of evals/corpus/")
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    parser.add_argument("-k", type=int, action="append", dest="ks", help="cutoff(s); default 5 and 10")
    parser.add_argument("--out", type=Path, help="write the full markdown report here")
    args = parser.parse_args(argv)
    ks = sorted(set(args.ks or [5, 10]))

    settings = get_settings(ROOT)
    if not vector_index.lancedb_available():
        print("error: the 'vector' extra (LanceDB) is not installed — `uv sync --extra vector`.",
              file=sys.stderr)
        return 2
    try:
        embedder = embeddings.client_from_settings(settings)
    except embeddings.EmbeddingError as exc:
        print(f"error: embedding config invalid: {exc}", file=sys.stderr)
        return 2
    if embedder is None:
        print("error: no embedder configured. Set EMBEDDING_BASE_URL + EMBEDDING_MODEL_REF (a running "
              "local embedding server). This eval is opt-in and needs real semantics.", file=sys.stderr)
        return 2

    cases = (load_yaml(args.golden.read_text(encoding="utf-8")) or {}).get("cases") or []

    cleanups: list[Any] = []
    try:
        if args.vault:
            root, label, is_vault = args.vault, f"vault:{args.vault}", True
            problems = _vector_problems(root, settings)
            if problems:
                print("error: vault vector index unusable for eval: " + "; ".join(problems)
                      + " — rebuild with `scripts/reindex_vector.py --force`.", file=sys.stderr)
                return 2
        else:
            tmp = tempfile.TemporaryDirectory(prefix="ks_reteval_")
            cleanups.append(tmp)
            root, label, is_vault = Path(tmp.name), "evals/corpus/", False
            _build_corpus_vault(CORPUS_DIR, root, embedder, settings)
        gconn, graph_present, gtmp = _open_graph(root, is_vault=is_vault)
        if gtmp is not None:
            cleanups.append(gtmp)
        try:
            result = run(root, settings, embedder, cases, ks, gconn=gconn, graph_present=graph_present)
        finally:
            gconn.close()
    finally:
        for c in cleanups:
            c.cleanup()

    report = render_report(result, settings=settings, ks=ks, source_label=label)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    agg = _aggregate(result["rows"], ks)
    print(f"scored {len(result['rows'])} cases | MRR {agg['MRR']:.3f} | "
          + " | ".join(f"{m} {agg[m]:.3f}" for m in [f"recall@{k}" for k in ks]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
