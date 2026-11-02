#!/usr/bin/env python3
"""Contradiction-judge evaluation gate (ADR-0069 decision 9, slice 3): a manual run, never in CI.

Builds a throwaway vault in a temporary directory from the hand-authored synthetic fixture
(`evals/contradiction_judge.yaml`): three documents, their claims and quotes, and distinct authors so
every pair is independent. It then runs the **production** contradiction check against that vault:
`contradiction_check.estimate` and then `run --token`, with the same planner, window packing, batch
prompt, schema validation, scope check, budget guard, usage ledger and filing. Every claim-order
permutation gets its own copy of the vault. The first permutation is claim-id order and the others
are seeded shuffles (`order_seed`). The proposals each run files are scored against the fixture's
labels.

It **writes no vault state**. The response cache, usage ledger, jobs, saved estimates and review
items all live in the temporary vault, which is deleted afterwards (`--keep-workdir` keeps it). The
only file written outside it is the result JSON under `evals/results/` (`--out-dir`). Provider
settings (credentials, local base URL) and the contradiction limits come from the real configuration.

Gates, checked before any model call:
- the fixture is valid: every cross-document pair within a topic is labelled, every document gets
  at least two windows, and every permutation has a contradiction that sits only across
  non-corresponding windows;
- `review.labels_reviewed: true` and every threshold is set (they must be agreed before any model
  is evaluated);
- a non-`local:` model needs `--allow-paid`; a chain or an unconfigured model is refused;
- the whole evaluation fits the configured budget: the planned calls and estimated input tokens of
  all permutations together stay within `CONTRADICTION_MAX_CALLS` and
  `CONTRADICTION_MAX_INPUT_TOKENS`. Each run is then given only what is left.

Verdict: `pass` only if every permutation's run is a clean success (complete coverage, with no
exhausted budget, parse error, truncation or output-limit violation), the worst permutation's
contradiction recall reaches `min_contradiction_recall`, and the worst permutation's false
positives stay within each `max_false_positives` (agreement, apparent_conflict and unrelated are
reported separately). A `local:` model must also match a hosted judge's result on the same fixture
(`--baseline`): recall at least the hosted judge's, and `apparent_conflict` false positives at most
the hosted judge's. Without a baseline its verdict is `incomplete`. A pass supports a documented
recommendation for this task only. It does not establish real-corpus accuracy.

Usage:
    uv run --no-sync python scripts/eval_contradictions.py --model REF --dry-run  # no call
    uv run --no-sync python scripts/eval_contradictions.py --model REF [--allow-paid] [--baseline RESULT]
        [--fixture PATH] [--out-dir DIR] [--keep-workdir]

`--no-sync` runs in the existing environment, which needs the `eval` extra and the provider SDK
extras `enrich` / `enrich-openai` installed; a syncing `uv run` can remove the out-of-lock GPU
overlay (docs/Environment Setup v0.1.md).

Exit status: 0 for a pass or a dry run; 1 for a fail or an incomplete verdict (a result file is
written); 2 when the fixture, a gate or a refusal stops the evaluation before any model call.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph, write_lock  # noqa: E402
from app.backend.config import get_settings  # noqa: E402
from app.backend.manifests import list_manifests, set_provenance  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.workers import claims, extract, intake, wiki  # noqa: E402
from app.workers import contradiction_check as cc  # noqa: E402

FIXTURE = ROOT / "evals" / "contradiction_judge.yaml"
RESULTS_DIR = ROOT / "evals" / "results"
TEMPLATES = ROOT / "templates"
LABELS = ("contradiction", "agreement", "apparent_conflict", "unrelated")
FP_LABELS = ("agreement", "apparent_conflict", "unrelated")
FIXTURE_CLAIMS_MODEL = "anthropic:fixture-claims"   # a scripted in-process adapter, never sent
RESULT_VERSION = 1


class FixtureError(ValueError):
    """The fixture is malformed or lacks a property the evaluation needs."""


class GateError(RuntimeError):
    """A gate that must hold before any model is evaluated does not."""


# --- the fixture -----------------------------------------------------------------------------------


def load_fixture(path: Path) -> dict[str, Any]:
    try:
        fx = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FixtureError(f"cannot read the fixture {path}: {exc}") from exc
    problems = validate_fixture(fx)
    if problems:
        raise FixtureError("the fixture is invalid:\n  - " + "\n  - ".join(problems))
    return fx


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _claims(fx: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`{fixture claim id: claim | {"doc": document id}}`."""
    return {c["id"]: c | {"doc": d["id"]} for d in fx["documents"] for c in d["claims"]}


def validate_fixture(fx: Any) -> list[str]:
    """Structural problems, in reading order (empty when the fixture can be evaluated)."""
    if not isinstance(fx, dict):
        return ["the fixture is not a mapping"]
    out: list[str] = []
    if fx.get("version") != 1:
        out.append("version must be 1")
    docs = fx.get("documents")
    if not isinstance(docs, list) or not 2 <= len(docs) <= 3:
        return out + ["documents must list two or three documents"]
    seen_ids: set[str] = set()
    texts: dict[str, str] = {}
    for key in ("id", "filename", "title", "author"):
        values = [d.get(key) if isinstance(d, dict) else None for d in docs]
        if not all(isinstance(v, str) and v.strip() for v in values):
            out.append(f"every document needs a non-empty {key}")
        elif len(set(values)) != len(values):
            out.append(f"document {key}s must be distinct (authors keep every pair independent)")
    for d in docs:
        if not isinstance(d, dict) or not isinstance(d.get("claims"), list) or not d["claims"]:
            out.append(f"document {d.get('id') if isinstance(d, dict) else d!r} has no claims")
            continue
        filename = d.get("filename")
        if isinstance(filename, str) and (filename in (".", "..") or "/" in filename
                                         or "\\" in filename or Path(filename).is_absolute()):
            out.append(f"document {d.get('id')} filename must be a single safe basename")
        for c in d["claims"]:
            cid = c.get("id") if isinstance(c, dict) else None
            if not isinstance(cid, str) or not cid:
                out.append(f"a claim of document {d.get('id')} has no id")
                continue
            if cid in seen_ids:
                out.append(f"claim id {cid} is used twice")
            seen_ids.add(cid)
            for key in ("topic", "text", "quote"):
                if not isinstance(c.get(key), str) or not c[key].strip():
                    out.append(f"claim {cid} needs a non-empty {key}")
            if isinstance(c.get("text"), str):
                norm = _norm(c["text"])
                if norm in texts:   # one wording is one claim: it would be shared, never compared
                    out.append(f"claims {texts[norm]} and {cid} have the same wording")
                texts.setdefault(norm, cid)
    if out:
        return out
    claim = _claims(fx)
    labels = fx.get("labels")
    if not isinstance(labels, dict):
        return out + ["labels must be a mapping"]
    default = labels.get("default")
    if default is not None and default not in LABELS:
        out.append(f"labels.default must be one of {', '.join(LABELS)} (or absent)")
    listed: set[frozenset[str]] = set()
    for i, row in enumerate(labels.get("pairs") or []):
        if not isinstance(row, list) or len(row) not in (3, 4):
            out.append(f"labels.pairs[{i}] must be [claim, claim, label, why]")
            continue
        a, b, label = row[0], row[1], row[2]
        if a not in claim or b not in claim:
            out.append(f"labels.pairs[{i}] names an unknown claim ({a}, {b})")
            continue
        if claim[a]["doc"] == claim[b]["doc"]:
            out.append(f"labels.pairs[{i}] pairs two claims of document {claim[a]['doc']}")
        if label not in LABELS:
            out.append(f"labels.pairs[{i}] has an unknown label {label!r}")
        key = frozenset((a, b))
        if key in listed:
            out.append(f"the pair {a}/{b} is labelled twice")
        listed.add(key)
    for a, b in combinations(sorted(claim), 2):
        ca, cb = claim[a], claim[b]
        if ca["doc"] != cb["doc"] and ca["topic"] == cb["topic"] \
                and frozenset((a, b)) not in listed:
            out.append(f"{a} and {b} share the topic {ca['topic']!r} but their pair is not "
                       "labelled")
    if not any(row[2] == "contradiction" for row in labels.get("pairs") or []
               if isinstance(row, list) and len(row) >= 3):
        out.append("the fixture labels no contradiction")
    st = fx.get("settings") or {}
    for key, low in (("window_chars", 1), ("permutations", 1)):
        v = st.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < low:
            out.append(f"settings.{key} must be a positive integer")
    return out


def gate_problems(fx: dict[str, Any]) -> list[str]:
    """Why the fixture may not be evaluated yet: its labels unreviewed or a threshold unset."""
    out: list[str] = []
    if (fx.get("review") or {}).get("labels_reviewed") is not True:
        out.append("the labels are a draft: a human must review them and set "
                   "review.labels_reviewed: true")
    th = fx.get("thresholds") or {}
    recall = th.get("min_contradiction_recall")
    if not isinstance(recall, (int, float)) or isinstance(recall, bool) or not 0 <= recall <= 1:
        out.append("thresholds.min_contradiction_recall must be set (0.0–1.0)")
    fps = th.get("max_false_positives") or {}
    for label in FP_LABELS:
        v = fps.get(label)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            out.append(f"thresholds.max_false_positives.{label} must be set (a count ≥ 0)")
    return out


def fixture_digest(fx: dict[str, Any]) -> str:
    """What the result was measured on: documents, labels and settings (not the review record or
    the thresholds, so a baseline stays comparable after thresholds are set)."""
    data = json.dumps({k: fx.get(k) for k in ("version", "documents", "labels", "settings")},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def label_map(fx: dict[str, Any]) -> tuple[dict[frozenset[str], str], str | None]:
    return ({frozenset((r[0], r[1])): r[2] for r in fx["labels"].get("pairs") or []},
            fx["labels"].get("default"))


def document_markdown(doc: dict[str, Any]) -> str:
    return f"# {doc['title']}\n\n" + "\n\n".join(c["quote"] for c in doc["claims"]) + "\n"


# --- the throwaway vault ---------------------------------------------------------------------------


class FixtureClaims:
    """The claims "model" of the throwaway vault: returns the fixture's claims of the document in
    the prompt. In-process and scripted; nothing is sent anywhere."""
    name = "anthropic"

    def __init__(self, fx: dict[str, Any]) -> None:
        self.claims = [c for d in fx["documents"] for c in d["claims"]]

    def available(self) -> bool:
        return True

    def parse(self, messages, schema, model_id, *, max_tokens):
        body = messages[-1]["content"]
        return {"claims": [{"claim": c["text"], "quote": c["quote"]} for c in self.claims
                           if c["quote"] in body]}


def eval_settings(real: Any, work: Path, fx: dict[str, Any]) -> Any:
    """The real configuration (providers, limits) with every vault location inside `work`."""
    tmp = get_settings(work)
    moved = {f.name: getattr(tmp, f.name) for f in dataclasses.fields(tmp)
             if isinstance(getattr(tmp, f.name), Path)
             and Path(getattr(tmp, f.name)).is_relative_to(work)}
    return dataclasses.replace(real, **moved, contradiction_model=None,
                               contradiction_window_chars=fx["settings"]["window_chars"])


def materialize(fx: dict[str, Any], work: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Build the fixture's vault in `work` through the production pipeline (intake, extraction,
    wiki, provenance, claims); return `({document id: source id}, {fixture claim id: claim id})`."""
    work.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATES, work / "templates", dirs_exist_ok=True)
    inbox = work / "raw" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for d in fx["documents"]:
        (inbox / d["filename"]).write_text(document_markdown(d), encoding="utf-8")
    jobs = work / "db" / "jobs.sqlite"
    intake.scan_inbox(work, jobs_db=jobs)
    extract.extract_sources(work, jobs_db=jobs)
    wiki.generate_wiki(work, jobs_db=jobs, templates_dir=work / "templates", rebuild_index=False)
    by_name = {m.get("original_filename"): m["source_id"]
               for m in list_manifests(work / "raw" / "manifests")}
    sids: dict[str, str] = {}
    for d in fx["documents"]:
        if d["filename"] not in by_name:
            raise FixtureError(f"document {d['id']} ({d['filename']}) was not ingested")
        sids[d["id"]] = by_name[d["filename"]]
    with write_lock.hold(work / "reviews"):
        for d in fx["documents"]:
            set_provenance(work / "raw" / "manifests", sids[d["id"]], author=d["author"])
    claims.extract_claims(work, client=LLMClient({"anthropic": FixtureClaims(fx)}),
                          model_ref=FIXTURE_CLAIMS_MODEL, jobs_db=jobs, rebuild_index=False)
    ids = {c["id"]: claims.claim_id(c["text"]) for d in fx["documents"] for c in d["claims"]}
    gconn = graph.connect(work / "db" / "graph.sqlite")
    try:
        active = set(graph.active_node_ids_of_type(gconn, "claim"))
        for d in fx["documents"]:
            of_doc = set(graph.claims_for_source(gconn, sids[d["id"]]))
            for c in d["claims"]:
                if ids[c["id"]] not in active or ids[c["id"]] not in of_doc:
                    raise FixtureError(f"claim {c['id']} was not extracted for document {d['id']} "
                                       "(is its quote verbatim in the document?)")
    finally:
        gconn.close()
    return sids, ids


# --- scoring ---------------------------------------------------------------------------------------


def window_index(window_id: str) -> tuple[int, int]:
    """`(x, y)` of a window id `…#a{x}/{n}xb{y}/{m}`."""
    m = re.search(r"#a(\d+)/\d+xb(\d+)/\d+$", window_id)
    if not m:
        raise ValueError(f"not a window id: {window_id}")
    return int(m.group(1)), int(m.group(2))


def window_plan(plan: dict[str, Any], back: dict[str, str]) -> list[dict[str, Any]]:
    return [{"id": w["id"], "claims_a": [back.get(c, c) for c in w["claims_a"]],
             "claims_b": [back.get(c, c) for c in w["claims_b"]], "input_chars": w["input_chars"]}
            for w in plan["windows"]]


def noncorresponding_contradictions(fx: dict[str, Any], plan: dict[str, Any],
                                    ids: dict[str, str]) -> list[str]:
    """The labelled contradictions whose two claims meet only in a window a{x} × b{y} with x ≠ y."""
    labels, _default = label_map(fx)
    out: list[str] = []
    for pair, label in sorted(labels.items(), key=lambda kv: sorted(kv[0])):
        if label != "contradiction":
            continue
        a, b = (ids[c] for c in sorted(pair))
        for w in plan["windows"]:
            members = set(w["claims_a"]) | set(w["claims_b"])
            if {a, b} <= members and (a in w["claims_a"]) != (b in w["claims_a"]):
                x, y = window_index(w["id"])
                if x != y:
                    out.append("/".join(sorted(pair)))
    return out


def score(fx: dict[str, Any], predicted: list[tuple[str, str]]) -> dict[str, Any]:
    """Predictions (fixture claim-id pairs) against the labels."""
    labels, default = label_map(fx)
    truth = {p for p, lab in labels.items() if lab == "contradiction"}
    found: set[frozenset[str]] = set()
    fps: dict[str, list[str]] = {lab: [] for lab in FP_LABELS}
    unscored: list[str] = []
    for a, b in sorted({tuple(sorted(p)) for p in predicted}):
        key = frozenset((a, b))
        label = labels.get(key, default)
        name = f"{a}/{b}"
        if label is None:
            unscored.append(name)
        elif label == "contradiction":
            found.add(key)
        else:
            fps[label].append(name)
    return {"contradictions": len(truth), "found": sorted("/".join(sorted(p)) for p in found),
            "missed": sorted("/".join(sorted(p)) for p in truth - found),
            "recall": (len(found) / len(truth)) if truth else None,
            "false_positives": fps, "false_positive_counts": {k: len(v) for k, v in fps.items()},
            "unscored": unscored}


def _proposals(reviews_dir: Path, back: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted((reviews_dir / "pending").glob("*.json")):
        item = json.loads(p.read_text(encoding="utf-8"))
        if item.get("type") != "resolve_contradiction":
            continue
        subj, prop = item.get("subject") or {}, item.get("proposal") or {}
        out.append({"claims": [back.get(subj.get("claim_a"), subj.get("claim_a")),
                               back.get(subj.get("claim_b"), subj.get("claim_b"))],
                    "confidence": prop.get("confidence"), "explanation": prop.get("explanation")})
    return out


def verdict(fx: dict[str, Any], perms: list[dict[str, Any]], *, local: bool,
            baseline: dict[str, Any] | None) -> dict[str, Any]:
    th = fx["thresholds"]
    reasons: list[str] = []
    for p in perms:
        if p.get("refused"):
            reasons.append(f"permutation {p['index']} was refused: {p['refused']}")
        elif p["run"]["status"] != "succeeded" or not p["run"]["coverage"]["complete"]:
            reasons.append(f"permutation {p['index']} is not a clean, complete run "
                           f"(status {p['run']['status']}; {p['run'].get('stopped') or 'see coverage'})")
    scored = [p["score"] for p in perms if p.get("score")]
    worst_recall = min((s["recall"] for s in scored), default=None)
    worst_fp = {lab: max((s["false_positive_counts"][lab] for s in scored), default=None)
                for lab in FP_LABELS}
    if len(scored) == len(perms) and not reasons:
        if worst_recall < th["min_contradiction_recall"]:
            reasons.append(f"contradiction recall {worst_recall:.3f} is below the threshold "
                           f"{th['min_contradiction_recall']}")
        for lab in FP_LABELS:
            if worst_fp[lab] > th["max_false_positives"][lab]:
                reasons.append(f"{worst_fp[lab]} {lab} false positive(s), above the threshold "
                               f"{th['max_false_positives'][lab]}")
    relative: dict[str, Any] | None = None
    if local:
        relative = _relative(fx, worst_recall, worst_fp, baseline)
        reasons += relative["reasons"]
    if reasons:
        status = "fail"
    elif local and relative and relative["status"] == "missing":
        status = "incomplete"
    else:
        status = "pass"
    predicted = [{tuple(x["claims"]) for x in p.get("predictions") or []} for p in perms]
    union = set().union(*predicted) if predicted else set()
    sensitive = sorted("/".join(sorted(t)) for t in union if not all(t in s for s in predicted))
    return {"status": status, "reasons": reasons, "worst_recall": worst_recall,
            "worst_false_positives": worst_fp, "order_sensitive_pairs": sensitive,
            "relative": relative,
            "note": "a pass supports a recommendation for this task; it does not establish "
                    "real-corpus accuracy"}


def _relative(fx: dict[str, Any], recall: float | None, fps: dict[str, int | None],
              baseline: dict[str, Any] | None) -> dict[str, Any]:
    """A local model against a hosted judge on the same fixture (ADR-0069 decision 9)."""
    if baseline is None:
        return {"status": "missing", "reasons": [],
                "detail": "a local model also needs a hosted judge's result (--baseline)"}
    reasons = _baseline_problems(fx, baseline)
    agg = baseline.get("verdict") or {}
    b_recall = agg.get("worst_recall")
    b_fp = (agg.get("worst_false_positives") or {}).get("apparent_conflict")
    if not reasons:
        if recall is None or b_recall is None or recall < b_recall:
            reasons.append(f"contradiction recall {recall} is below the hosted judge's {b_recall}")
        if fps["apparent_conflict"] is None or b_fp is None or fps["apparent_conflict"] > b_fp:
            reasons.append(f"{fps['apparent_conflict']} apparent_conflict false positive(s), above "
                           f"the hosted judge's {b_fp}")
    return {"status": "fail" if reasons else "pass", "reasons": reasons,
            "baseline_model_ref": baseline.get("model_ref"), "baseline_recall": b_recall,
            "baseline_apparent_conflict_false_positives": b_fp}


def _baseline_problems(fx: dict[str, Any], baseline: dict[str, Any], *,
                       output_limit: int | None = None) -> list[str]:
    """Reject incomparable or incomplete hosted results before spending on a comparison."""
    if not isinstance(baseline, dict):
        return ["the baseline is not a result mapping"]
    reasons = []
    if baseline.get("result_version") != RESULT_VERSION:
        reasons.append("the baseline has an incompatible result version")
    if baseline.get("fixture_digest") != fixture_digest(fx):
        reasons.append("the baseline was measured on a different fixture")
    try:
        hosted = cc.requires_paid_authorization(baseline.get("model_ref") or "")
    except (ValueError, RuntimeError):
        hosted = False
    if baseline.get("paid") is not True or not hosted:
        reasons.append("the baseline is not a hosted judge's result")
    if baseline.get("prompt_version") != cc.art.CONTRADICTION_PROMPT_VERSION or \
            baseline.get("schema_version") != cc.art.CONTRADICTION_SCHEMA_VERSION:
        reasons.append("the baseline uses a different prompt or schema version")
    settings = baseline.get("settings") or {}
    expected_seeds = [None, *range(1, fx["settings"]["permutations"])]
    if (settings.get("window_chars") != fx["settings"]["window_chars"]
            or settings.get("strategy") != cc.STRATEGY
            or settings.get("chars_per_token") != cc.CHARS_PER_TOKEN
            or settings.get("permutations") != len(expected_seeds)
            or settings.get("order_seeds") != expected_seeds
            or (output_limit is not None and settings.get("max_output_tokens") != output_limit)):
        reasons.append("the baseline uses different evaluation settings")
    perms = baseline.get("permutations") or []
    if (baseline.get("dry_run") or len(perms) != len(expected_seeds)
            or any(not isinstance(p, dict) or p.get("index") != i
                   or p.get("order_seed") != expected_seeds[i]
                   or p.get("run", {}).get("status") != "succeeded"
                   or p.get("run", {}).get("coverage", {}).get("complete") is not True
                   or p.get("run", {}).get("stopped")
                   or p.get("run", {}).get("budget", {}).get("output_limit_violation")
                   or p.get("run", {}).get("maintenance", {}).get("error")
                   for i, p in enumerate(perms))):
        reasons.append("the baseline's runs are not all clean, complete runs")
    agg = baseline.get("verdict") or {}
    recall = agg.get("worst_recall")
    fp = (agg.get("worst_false_positives") or {}).get("apparent_conflict")
    if (not isinstance(recall, (int, float)) or isinstance(recall, bool) or not 0 <= recall <= 1
            or not isinstance(fp, int) or isinstance(fp, bool) or fp < 0):
        reasons.append("the baseline has invalid aggregate scores")
    return reasons


# --- the evaluation --------------------------------------------------------------------------------


def evaluate(fx: dict[str, Any], *, model_ref: str, real_settings: Any, allow_paid: bool = False,
             dry_run: bool = False, baseline: dict[str, Any] | None = None,
             work_dir: Path | None = None, keep_workdir: bool = False,
             client_factory: Callable[[Any], Any] | None = None,
             now: str | None = None) -> dict[str, Any]:
    """Run the evaluation; return the result record. Raises FixtureError / GateError / cc.Refused
    before any model call when a gate fails."""
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        problems = gate_problems(fx)
        if problems:
            raise GateError("not ready to evaluate:\n  - " + "\n  - ".join(problems))
    model_ref = cc.resolve_model(model_ref, real_settings)   # a chain is refused here
    paid = cc.requires_paid_authorization(model_ref)
    if not dry_run and not paid and baseline is not None:
        problems = _baseline_problems(fx, baseline,
                                      output_limit=real_settings.contradiction_max_output_tokens)
        if problems:
            raise GateError("invalid hosted baseline:\n  - " + "\n  - ".join(problems))
    if paid and not allow_paid and not dry_run:
        raise cc.Refused("paid_authorization_required",
                         f"{model_ref} requires paid authorization: pass --allow-paid")
    owned = work_dir is None
    work = Path(work_dir or tempfile.mkdtemp(prefix="ks-eval-contradictions-"))
    try:
        sids, ids = materialize(fx, work / "base")
        back = {v: k for k, v in ids.items()}
        doc_order = [d["id"] for d in fx["documents"]]
        perms: list[dict[str, Any]] = []
        for index in range(fx["settings"]["permutations"]):
            seed = None if index == 0 else index
            vault = work / f"perm{index}"
            shutil.copytree(work / "base", vault)
            st = eval_settings(real_settings, vault, fx)
            client = client_factory(st) if client_factory else None
            est = cc.estimate(st, [sids[d] for d in doc_order], model=model_ref, client=client,
                              order_seed=seed, now=now)
            if est["refused"]:
                raise FixtureError(f"permutation {index}: the estimate was refused "
                                   f"({est['refused']}: {est['budget'].get('over')})")
            if not est["available"] and not dry_run:
                raise cc.Refused("model_unavailable", f"{model_ref} is not configured (no "
                                                      "credential or base URL)")
            thin = [f"{p['a']}~{p['b']}" for p in est["plan"]["pairs"]
                    if p["eligible"] and min(p["windows_a"], p["windows_b"]) < 2]
            skipped = [f"{p['a']}~{p['b']}: {p['reason']}" for p in est["plan"]["pairs"]
                       if not p["eligible"]]
            noncorr = noncorresponding_contradictions(fx, est["plan"], ids)
            if skipped:
                raise FixtureError(f"permutation {index}: pairs were skipped ({'; '.join(skipped)})")
            if thin:
                raise FixtureError(f"permutation {index}: a document fits in one window for "
                                   f"{', '.join(thin)}; lower settings.window_chars")
            if not noncorr:
                raise FixtureError(f"permutation {index}: no contradiction sits only across "
                                   "non-corresponding windows; adjust the fixture")
            perms.append({"index": index, "order_seed": seed, "vault": vault, "settings": st,
                          "client": client, "token": est["token"], "plan": est["plan"],
                          "budget_estimate": est["budget"],
                          "noncorresponding_contradictions": noncorr})
        limits = cc.configured_limits(real_settings)
        planned_calls = sum(p["budget_estimate"]["calls"] for p in perms)
        planned_tokens = sum(p["budget_estimate"]["estimated_input_tokens"] for p in perms)
        if planned_calls > limits["max_calls"] or planned_tokens > limits["max_input_tokens"]:
            raise cc.Refused("over_budget", f"the evaluation plans {planned_calls} call(s) and "
                                            f"~{planned_tokens} input tokens, above the configured "
                                            f"{limits['max_calls']} / {limits['max_input_tokens']}")
        used_calls = used_tokens = used_chars = 0
        char_cap = limits["max_input_tokens"] * cc.CHARS_PER_TOKEN
        for p in perms:
            if dry_run:
                continue
            calls_left = limits["max_calls"] - used_calls
            tokens_left = min(limits["max_input_tokens"] - used_tokens,
                              (char_cap - used_chars) // cc.CHARS_PER_TOKEN)
            if calls_left <= 0 or tokens_left <= 0:
                p["refused"] = "over_budget: the whole-evaluation allowance is exhausted"
                continue
            try:
                summary = cc.run(p["settings"], p["token"], allow_paid=allow_paid,
                                 max_calls=calls_left, max_input_tokens=tokens_left,
                                 client=p["client"], now=now)
            except cc.Refused as exc:
                if used_calls == 0 and p["index"] == 0:
                    raise
                p["refused"] = f"{exc.code}: {exc}"
                continue
            b = summary["budget"]
            used_calls += b["calls"]
            used_chars += b["input_chars"]
            used_tokens += b["reported_input_tokens"] + b["estimated_input_tokens_not_reported"]
            p["run"] = {k: summary[k] for k in (
                "job_id", "status", "coverage", "stopped", "budget", "out_of_scope_pairs_dropped",
                "maintenance", "contradictions_proposed")}
            p["predictions"] = _proposals(p["settings"].reviews_dir, back)
            p["score"] = score(fx, [tuple(x["claims"]) for x in p["predictions"]])
        result = _record(fx, perms, model_ref=model_ref, paid=paid, dry_run=dry_run,
                         real_settings=real_settings, back=back, planned_calls=planned_calls,
                         planned_tokens=planned_tokens, now=now)
        if not dry_run:
            result["verdict"] = verdict(fx, perms, local=model_ref.startswith("local:"),
                                        baseline=baseline)
        if keep_workdir:
            result["workdir"] = str(work)
        return result
    finally:
        if owned and not keep_workdir:
            shutil.rmtree(work, ignore_errors=True)


def _record(fx: dict[str, Any], perms: list[dict[str, Any]], *, model_ref: str, paid: bool,
            dry_run: bool, real_settings: Any, back: dict[str, str], planned_calls: int,
            planned_tokens: int, now: str) -> dict[str, Any]:
    first = perms[0]["plan"]
    return {
        "result_version": RESULT_VERSION, "evaluated_at": now, "dry_run": dry_run,
        "fixture_digest": fixture_digest(fx), "review": fx.get("review"),
        "thresholds": fx.get("thresholds"),
        "model_ref": model_ref, "endpoint": first["endpoint"], "paid": paid,
        "reported_models": sorted({m for p in perms
                                   for m in (p.get("run", {}).get("budget", {})
                                             .get("reported_models") or [])}),
        "prompt_version": first["prompt_version"], "schema_version": first["schema_version"],
        "settings": {"window_chars": first["window_chars"],
                     "max_output_tokens": first["max_output_tokens"],
                     "limits": cc.configured_limits(real_settings),
                     "strategy": cc.STRATEGY, "chars_per_token": cc.CHARS_PER_TOKEN,
                     "permutations": len(perms),
                     "order_seeds": [p["order_seed"] for p in perms]},
        "planned": {"calls": planned_calls, "estimated_input_tokens": planned_tokens},
        "permutations": [{
            "index": p["index"], "order_seed": p["order_seed"],
            "window_plan": window_plan(p["plan"], back),
            "noncorresponding_contradictions": p["noncorresponding_contradictions"],
            "cache": {"expected_hits": p["budget_estimate"]["cache_hits"],
                      "from_cache": (p.get("run") or {}).get("coverage", {}).get("from_cache")},
            **({"refused": p["refused"]} if p.get("refused") else {}),
            **({"run": p["run"], "predictions": p["predictions"], "score": p["score"]}
               if p.get("run") else {}),
        } for p in perms],
    }


def _slug(model_ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model_ref).strip("-")


def write_result(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = result["evaluated_at"].replace(":", "").replace("-", "").replace("+0000", "Z")
    path = out_dir / f"contradiction_judge_{stamp}_{_slug(result['model_ref'])}.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _print(result: dict[str, Any], path: Path | None) -> None:
    print(f"Model: {result['model_ref']} ({result['endpoint']}); prompt {result['prompt_version']}")
    print(f"Planned: {result['planned']['calls']} call(s), "
          f"~{result['planned']['estimated_input_tokens']} input tokens over "
          f"{len(result['permutations'])} permutation(s)")
    for p in result["permutations"]:
        line = (f"  permutation {p['index']} (seed {p['order_seed']}): "
                f"{len(p['window_plan'])} windows, non-corresponding contradictions "
                f"{len(p['noncorresponding_contradictions'])}")
        if p.get("refused"):
            line += f"; refused ({p['refused']})"
        elif p.get("score"):
            s = p["score"]
            line += (f"; {p['run']['status']}; recall "
                     f"{len(s['found'])}/{s['contradictions']}; false positives "
                     + ", ".join(f"{k} {v}" for k, v in s["false_positive_counts"].items()))
        print(line)
    v = result.get("verdict")
    if v:
        print(f"Verdict: {v['status'].upper()}")
        for r in v["reasons"]:
            print(f"  - {r}")
        if v["relative"] and v["relative"]["status"] == "missing":
            print(f"  - {v['relative']['detail']}")
        if v["order_sensitive_pairs"]:
            print("  order-sensitive pairs: " + ", ".join(v["order_sensitive_pairs"]))
    else:
        print("Dry run: no model call was made.")
    if path:
        print(f"Result: {path}")
    if result.get("workdir"):
        print(f"Workdir kept: {result['workdir']}")


def main(argv: list[str] | None = None, *, settings: Any = None,
         client_factory: Callable[[Any], Any] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Contradiction-judge evaluation gate (ADR-0069).")
    ap.add_argument("--model", required=True, help="the judge, provider:model")
    ap.add_argument("--allow-paid", action="store_true", help="authorize a non-local model")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the vault and plan every permutation; no model call")
    ap.add_argument("--baseline", type=Path, help="a hosted judge's result (for a local model)")
    ap.add_argument("--fixture", type=Path, default=FIXTURE)
    ap.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    ap.add_argument("--keep-workdir", action="store_true")
    args = ap.parse_args(argv)
    try:
        fx = load_fixture(args.fixture)
        baseline = None
        if args.baseline:
            try:
                baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise FixtureError(f"cannot read the baseline {args.baseline}: {exc}") from exc
        result = evaluate(fx, model_ref=args.model, real_settings=settings or get_settings(),
                          allow_paid=args.allow_paid, dry_run=args.dry_run, baseline=baseline,
                          keep_workdir=args.keep_workdir, client_factory=client_factory)
    except (FixtureError, GateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except cc.Refused as exc:
        print(f"refused ({exc.code}): {exc}", file=sys.stderr)
        return 2
    path = None if args.dry_run else write_result(result, args.out_dir)
    _print(result, path)
    if args.dry_run:
        return 0
    return 0 if result["verdict"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
