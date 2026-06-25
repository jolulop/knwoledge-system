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


def _dash(v: Any) -> str:
    return "-" if v is None else str(v)


# --- vault build + run -----------------------------------------------------------------------------

def _build_corpus_vault(corpus_dir: Path, work_root: Path, embedder: Any, settings: Any) -> None:
    from app.workers import extract, intake, wiki
    inbox = work_root / "raw" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for md in sorted(corpus_dir.glob("*.md")):
        shutil.copy(md, inbox / md.name)
    jobs = work_root / "db" / "jobs.sqlite"
    intake.scan_inbox(work_root, jobs_db=jobs)
    extract.extract_sources(work_root, jobs_db=jobs)
    # Source wiki pages carry the source-status rows the keyword nav table needs; without them
    # `_source_status_map` returns nothing, so the default retention filter drops EVERY evidence hit
    # (ADR-0029/0032 §8) — the cause of an all-zero baseline. Deterministic + key-free.
    wiki.generate_wiki(work_root, jobs_db=jobs, templates_dir=ROOT / "templates",
                       rebuild_index=False, record_job=False)
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


def _best_channel_rank(evidence: list[dict[str, Any]], sources: set[str], channel: str) -> int | None:
    """Best (lowest) 1-based rank a channel gave any chunk from `sources` — None if it surfaced none."""
    ranks = [e["channels"][channel]["rank"] for e in evidence
             if e.get("source_id") in sources and channel in (e.get("channels") or {})]
    return min(ranks) if ranks else None


def _channel_pref(rel: int | None, irr: int | None) -> str:
    if rel is None and irr is None:
        return "none"
    if irr is None:
        return "relevant"
    if rel is None:
        return "irrelevant"
    return "relevant" if rel < irr else ("irrelevant" if irr < rel else "tie")


# (keyword_pref, vector_pref) -> label. The single-channel cases (one channel silent) are the COMMON
# real failure shape and are decision-relevant: "<active>_prefers_<x>_<other>_silent". RRF re-weighting
# can only plausibly help the cross-disagreement cases (one channel prefers relevant, the other the
# distractor); every "prefers_irrelevant" / silent shape means there's no relevant signal to up-weight.
_CHANNEL_LABELS = {
    ("relevant", "irrelevant"): "keyword_prefers_relevant_vector_prefers_irrelevant",
    ("irrelevant", "relevant"): "vector_prefers_relevant_keyword_prefers_irrelevant",
    ("irrelevant", "irrelevant"): "both_prefer_irrelevant",
    ("relevant", "relevant"): "both_prefer_relevant",
    ("none", "none"): "no_channel_signal",
    ("none", "irrelevant"): "vector_prefers_irrelevant_keyword_silent",
    ("irrelevant", "none"): "keyword_prefers_irrelevant_vector_silent",
    ("none", "relevant"): "vector_prefers_relevant_keyword_silent",
    ("relevant", "none"): "keyword_prefers_relevant_vector_silent",
}


def channel_diagnostics(evidence: list[dict[str, Any]], relevant: set[str],
                        irrelevant: set[str]) -> dict[str, Any]:
    """Per-channel source-level best ranks + an interpretive label for a negative case (ADR-0038).

    Read-only over `run_search` `evidence[].channels` — never changes retrieval. Answers whether a
    discrimination failure is **fusion-balance** (one channel ranks relevant first, the other flips it →
    weighted RRF *might* help) or **semantic ambiguity** (both channels prefer the distractor → RRF can't).
    """
    d: dict[str, Any] = {}
    for ch in ("keyword", "vector"):
        d[f"{ch}_relevant_rank"] = _best_channel_rank(evidence, relevant, ch)
        d[f"{ch}_irrelevant_rank"] = _best_channel_rank(evidence, irrelevant, ch)
    kw = _channel_pref(d["keyword_relevant_rank"], d["keyword_irrelevant_rank"])
    vec = _channel_pref(d["vector_relevant_rank"], d["vector_irrelevant_rank"])
    d["label"] = _CHANNEL_LABELS.get((kw, vec), "mixed_or_tied")
    return d


def run(root: Path, settings: Any, embedder: Any, cases: list[dict], ks: list[int],
        *, gconn: Any, graph_present: bool, keyword_readonly: bool = False) -> dict[str, Any]:
    """Score each golden case over `run_search` evidence. The caller owns `gconn` (an empty graph for the
    committed corpus; the vault's real graph, or a throwaway empty one, for `--vault`). `keyword_readonly`
    opens the keyword index read-only — set for `--vault` so the eval never mutates an operator vault."""
    metric = settings.embedding_distance_metric
    policy = load_retrieval_policy(settings.retrieval_policy_path)
    kpath = root / keyword_index.DB_RELPATH
    kconn = keyword_index.connect_readonly(kpath) if keyword_readonly else keyword_index.connect(kpath)
    fmap = _filename_to_source(root / "raw" / "manifests")
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    try:
        for case in cases:
            relevant, miss_r = _resolve(case.get("relevant"), fmap)
            irrelevant, miss_i = _resolve(case.get("irrelevant"), fmap)
            # Skip + report on ANY unresolved judgment (relevant OR irrelevant): a silently-dropped
            # relevant doc would score against a SMALLER oracle (inflating recall/MRR) and a dropped
            # distractor would make discrimination look better than it is. Never score a partial case —
            # `miss_r` (a typo'd relevant filename) must skip too, not just `not relevant`/`miss_i`.
            if not relevant or miss_r or miss_i:
                missing = miss_r + miss_i
                skipped.append(f"{case.get('id')} (unresolved: {missing})" if missing
                               else f"{case.get('id')} (no relevant)")
                continue
            q = case["query"]

            def vector_search(*, limit: int, _q: str = q) -> list[dict[str, Any]]:
                return vector_index.search(root, embedder.embed([_q])[0], limit=limit, metric=metric)

            res = search.run_search(q=q, mode=case.get("mode", "auto"), keyword_conn=kconn,
                                    graph_conn=gconn, policy=policy, vector_search=vector_search)
            ranked = evidence_sources(res)
            row = {"id": case.get("id"), "category": case.get("category", "?"),
                   **score_case(ranked, relevant, irrelevant, ks)}
            if irrelevant:  # per-channel diagnostics only make sense for negative cases (ADR-0038)
                row["diag"] = channel_diagnostics(res.get("evidence") or [], relevant, irrelevant)
            rows.append(row)
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
    # Per-channel diagnostics for the FAILURES (relevant_wins = NO): is it fusion-balance or semantics?
    failed = [r for r in rows if r.get("has_neg") and not r["discriminated"] and r.get("diag")]
    if failed:
        lines += ["", "## Channel Diagnostics (failed disambiguation — fusion-balance vs semantic "
                  "ambiguity)", "", "| id | kw_rel | kw_irr | vec_rel | vec_irr | label |",
                  "|---|---|---|---|---|---|"]
        for r in failed:
            d = r["diag"]
            lines.append(f"| {r['id']} | {_dash(d['keyword_relevant_rank'])} | "
                         f"{_dash(d['keyword_irrelevant_rank'])} | {_dash(d['vector_relevant_rank'])} | "
                         f"{_dash(d['vector_irrelevant_rank'])} | {d['label']} |")
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


def _keyword_problems(root: Path) -> list[str]:
    """For `--vault`: the keyword index must ALREADY exist AND be usable. The eval is read-only over an
    operator vault, but `keyword_index.connect` mkdirs + creates an empty SQLite file when the DB is
    missing — the vault-mutation boundary `_open_graph` guards for the graph. We open strictly read-only
    and reuse `keyword_index.consistency_errors` — the SAME 'usable index' definition as
    `validate_index_consistency` (schema + core tables + fingerprint freshness): a missing/stale/
    incomplete index would otherwise crash search or score a stale ranking under a clean-looking report."""
    path = root / keyword_index.DB_RELPATH
    if not path.exists():
        return ["no keyword index"]
    conn = keyword_index.connect_readonly(path)  # read-only (mode=ro): never creates/mutates the vault
    try:
        return keyword_index.consistency_errors(root, conn)
    finally:
        conn.close()


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
            kw_problems = _keyword_problems(root)
            if kw_problems:
                print("error: vault keyword index unusable for eval: " + "; ".join(kw_problems)
                      + " — build it first (`POST /jobs/reindex`). The eval is read-only and will not "
                      "create indexes in your vault.", file=sys.stderr)
                return 2
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
            result = run(root, settings, embedder, cases, ks, gconn=gconn, graph_present=graph_present,
                         keyword_readonly=is_vault)
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
