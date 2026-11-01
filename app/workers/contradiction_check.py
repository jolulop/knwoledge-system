#!/usr/bin/env python3
"""Scoped contradiction checks: estimate, then run the saved estimate (ADR-0069 slice 2(b)).

Occasional and deliberate (decision 1): the user selects at least two documents, and nothing else is
ever compared (decision 2). Each unordered pair of **independent** documents is compared once, over
cross-document claims only; a pair that is not independent is skipped with its actual reason.

**Windows** (decision 3): a document's claims are packed in order into windows of at most
`CONTRADICTION_WINDOW_CHARS`; one judge call compares a window of A with a window of B and returns
the contradicting pairs. Exhaustive coverage needs **every window of A against every window of B**,
so the plan is the cross-product and its cost grows with the product, not linearly. A returned pair
must name one supplied claim from each list (that proves the model stayed in scope, not that the
claims contradict); anything else is dropped and counted. A window's cache identity is its exact
contents (claim wording and quoted evidence, offsets excluded), the selected documents, the model,
the prompt and schema versions and the output limit, so an interrupted run resumes completed windows
from the response cache.

**Estimate → token → run** (decision 5): `estimate` makes no model call and writes only its saved
estimate (`db/contradiction_estimates.json`, never backed up). The record binds a digest of the plan
— the selected documents, their independence inputs, the windows and their contents, the claims'
evidence fingerprints, the model and its endpoint, the prompt and schema versions, the generation
settings and the limits — so `run --token` executes exactly that plan: a relevant change refuses it,
an unrelated vault edit does not. A plan over the limits gets no token. Under the write lock the run
checks the token, the paid authorization, the model's configuration, the digest and the budget, then
consumes the token (single use) before anything changes; a refusal leaves the token usable. Targeted
maintenance of the selected documents then runs in the same commit phase (it never changes claims,
so it can't invalidate the accepted plan).

**The judge** (decision 4): one `provider:model` reference — `--model`, else `CONTRADICTION_MODEL`,
else refused; chains are refused; nothing is ever substituted. Every non-`local:` provider requires
`--allow-paid` on the run, which authorizes that model only and never lifts the budget.

**Budget** (decision 7): before **every** attempt, retries included, the run reserves one call, the
attempt's exact input characters, its estimated input tokens (characters / 2 — an estimate, not a
bound) and the full output limit; if that would exceed a limit, nothing is sent. The hard caps are
calls and input characters (`CONTRADICTION_MAX_INPUT_TOKENS` × 2); the token budget is approximate.
After the attempt, provider-reported usage replaces the estimate or the reservation where reported.
A truncated response (the output limit was reached) is a **failed window**, never "no
contradictions", and is not retried. A reported output above the limit (the endpoint didn't enforce
it) stops further calls. A window whose every attempt failed at the adapter stops the run: the
model's own error is reported and no other model is tried. Whatever stops a run, its coverage
record lists exactly which windows completed, failed and remain; partial coverage is never presented
as a complete comparison.

Verdicts are filed as ADR-0031 decision 2 left them — one sorted-pair `contradicts` assertion, born
`proposed`, and a `resolve_contradiction` review item showing both claims, their citations and the
model's explanation — under the write lock, after re-checking that both claims still read as judged
and that no human decided the pair. The review item records `context.judged_evidence` (the
fingerprints maintenance checks) and `judged_by` (job, window, model, prompt version). Filing a
proposal changes no page (pages render only active contradictions), so there is no publication.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.backend import db, graph, write_lock
from app.backend.manifests import (
    confirmed_details,
    get_provenance,
    independence_digest,
    independence_inputs,
    independent_sources,
    iso_now,
    name_identity,
    valid_manifests,
)
from app.backend.publication_queue import _sync_dir, _write_durably
from app.llm import prompts
from app.llm.adapters import AdapterError
from app.llm.cache import ResponseCache, cache_key
from app.llm.client import ConfigError, ParseError, build_client, parse_model_ref
from app.llm.usage import input_chars
from app.workers import contradictions, reviews, titles
from app.workers import enrichment_artifact as art

CHARS_PER_TOKEN = 2                  # pessimistic: estimated tokens, never a bound (decision 7)
PROVIDERS = ("anthropic", "openai", "local")
STRATEGY = "window-cross-v1"
STORE_NAME = "contradiction_estimates.json"
SCHEMA_VERSION = 1
TOKEN = re.compile(r"cce_[0-9a-f]{32}")
_KEEP = 20                           # saved estimates kept; older ones are dropped on each save
RUN_COMMAND = "uv run python scripts/detect_contradictions.py run --token"


class Refused(RuntimeError):
    """The estimate or run was refused before any model call; `code` names why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BudgetExhausted(RuntimeError):
    """The next attempt's reservation would exceed a limit, so it was not sent."""

    def __init__(self, what: str, retry: int) -> None:
        super().__init__(f"the next call would exceed {what}")
        self.retry = retry


class WindowTruncated(RuntimeError):
    """The response stopped at the output limit: a failed window (not retried)."""


class OutputLimitViolated(RuntimeError):
    """A response reported more output than the limit (the endpoint didn't enforce it): its window
    fails, the response is not cached (raised before the client stores it), and no further call is
    made."""


# --- the judge model -------------------------------------------------------------------------------


def resolve_model(model: str | None, settings: Any) -> str:
    """`--model` → `CONTRADICTION_MODEL` → refused. Exactly one `provider:model`; no chain."""
    ref = (model or settings.contradiction_model or "").strip()
    if not ref:
        raise Refused("no_model", "choose a judge model: pass --model provider:model or set "
                                  "CONTRADICTION_MODEL (it has no default until the evaluation "
                                  "gate passes)")
    if "," in ref:
        raise Refused("chain", f"the judge is one provider:model reference; chains are not "
                               f"accepted, so nothing is ever substituted ({ref})")
    try:
        provider, _ = parse_model_ref(ref)
    except ConfigError as exc:
        raise Refused("bad_model", str(exc)) from exc
    if provider not in PROVIDERS:
        raise Refused("bad_model", f"unknown provider {provider!r}; known: {', '.join(PROVIDERS)}")
    return ref


def requires_paid_authorization(model_ref: str) -> bool:
    """Every non-`local:` provider (actual billing can vary)."""
    return parse_model_ref(model_ref)[0] != "local"


def endpoint(model_ref: str, settings: Any) -> str:
    provider = parse_model_ref(model_ref)[0]
    if provider == "anthropic":
        return "https://api.anthropic.com"
    if provider == "openai":
        return settings.openai_base_url or "https://api.openai.com/v1"
    return settings.enrich_local_base_url or "(not configured)"


# --- the plan --------------------------------------------------------------------------------------


def skip_reason(prov_a: dict[str, Any], prov_b: dict[str, Any]) -> str | None:
    """None for independent documents (ADR-0018, `manifests.independent_sources`), else the actual
    reason they are not."""
    if independent_sources(prov_a, prov_b):
        return None
    a, b = independence_inputs(prov_a), independence_inputs(prov_b)
    if not [k for k in a if k in b]:
        return "provenance unknown (details not confirmed)"
    shared = set(a.get("authors", ())) & set(b.get("authors", ()))
    if shared:
        names = sorted({x["name"] for x in confirmed_details(prov_a)["authors"]
                        if name_identity(x["name"]) in shared})
        return "shared author " + ", ".join(names)
    if a.get("publisher") and a.get("publisher") == b.get("publisher"):
        return "same publisher " + confirmed_details(prov_a)["publisher"]["name"]
    if a.get("report_family") and a.get("report_family") == b.get("report_family"):
        return "same report family " + str(prov_a.get("report_family"))
    if a.get("canonical_url") and a.get("canonical_url") == b.get("canonical_url"):
        return "same canonical URL"
    return "not independent"


def _documents(manifests_dir: Path, source_ids: list[str]) -> dict[str, dict[str, Any]]:
    distinct = sorted(dict.fromkeys(source_ids))
    if len(distinct) < 2:
        raise Refused("too_few_documents", "select at least two distinct documents")
    valid = {m["source_id"]: m for m in valid_manifests(manifests_dir)[0]}
    unknown = [s for s in distinct if s not in valid]
    if unknown:
        raise Refused("unknown_document", "not a valid source id: " + ", ".join(unknown))
    return {s: valid[s] for s in distinct}


def _judged_claims(gconn, source_id: str, *, claims_dir: Path,
                   markdown_dir: Path) -> dict[str, dict[str, Any]]:
    """`{claim_id: context}` of a document's active claims that have wording and a quote **from that
    document**; each context carries only that document's citations (a claim it shares with an
    unselected document never brings that document's evidence along)."""
    active = set(graph.active_node_ids_of_type(gconn, "claim"))
    out: dict[str, dict[str, Any]] = {}
    for cid in graph.claims_for_source(gconn, source_id):
        if cid not in active:
            continue
        ctx = contradictions.scoped_claim_context(gconn, cid, source_id, claims_dir=claims_dir,
                                                  markdown_dir=markdown_dir)
        if ctx is not None and any(c["quote"] for c in ctx["citations"]):
            out[cid] = ctx
    return out


def _prompt_claim(ctx: dict[str, Any]) -> dict[str, Any]:
    cites = sorted({(c["source_id"], c["quote"]) for c in ctx["citations"] if c["quote"]})
    return {"claim_id": ctx["claim_id"], "claim_text": ctx["claim_text"],
            "citations": [{"source_id": s, "quote": q} for s, q in cites]}


def _windows(contexts: list[dict[str, Any]], window_chars: int) -> list[list[dict[str, Any]]]:
    """Pack claims, in claim-id order, into windows of at most `window_chars` (a claim larger than
    that gets a window of its own)."""
    out: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for ctx in contexts:
        n = len(prompts.judge_claims_block([_prompt_claim(ctx)])) + 1
        if current and size + n > window_chars:
            out.append(current)
            current, size = [], 0
        current.append(ctx)
        size += n
    if current:
        out.append(current)
    return out


def _ordered(contexts: list[dict[str, Any]], order_seed: int | None,
             source_id: str) -> list[dict[str, Any]]:
    """Claim-id order, or — for the judge evaluation's claim-order permutations (ADR-0069 decision
    9) — a seeded shuffle of it, the same for a document in every pair it is part of."""
    if order_seed is None:
        return contexts
    out = list(contexts)
    random.Random(f"{order_seed}:{source_id}").shuffle(out)
    return out


def build_plan(settings: Any, gconn, source_ids: list[str], model_ref: str, *,
               order_seed: int | None = None) -> dict[str, Any]:
    """The plan for the selected documents: `{"plan": <bound, JSON-serializable>, "work":
    {window_id: {"messages", "contexts"}}, "titles": {sid: title}}`. Deterministic. `order_seed`
    (the evaluator only; no CLI flag) packs each document's claims in a seeded order instead of
    claim-id order; it is part of the plan, so its token binds it."""
    docs = _documents(settings.manifests_dir, source_ids)
    prov = {s: get_provenance(m) for s, m in docs.items()}
    claims_dir, markdown_dir = settings.wiki_dir / "Claims", settings.markdown_dir
    judged = {s: _judged_claims(gconn, s, claims_dir=claims_dir, markdown_dir=markdown_dir)
              for s in docs}
    limit = settings.contradiction_max_output_tokens
    # The endpoint is part of the cache identity: another server under the same model reference
    # must not replay this one's verdicts.
    strategy = json.dumps({"strategy": STRATEGY, "documents": sorted(docs),
                           "endpoint": endpoint(model_ref, settings),
                           "max_output_tokens": limit}, sort_keys=True)
    pairs: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    work: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, dict[str, str]] = {}   # {source_id: {claim_id: scoped fingerprint}}
    order = sorted(docs)
    for i, sa in enumerate(order):
        for sb in order[i + 1:]:
            reason = skip_reason(prov[sa], prov[sb])
            if reason:
                pairs.append({"a": sa, "b": sb, "eligible": False, "reason": reason})
                continue
            # Cross-document claims only: a claim drawn from both documents is in neither list.
            ca = _ordered([judged[sa][c] for c in sorted(judged[sa]) if c not in judged[sb]],
                          order_seed, sa)
            cb = _ordered([judged[sb][c] for c in sorted(judged[sb]) if c not in judged[sa]],
                          order_seed, sb)
            wa = _windows(ca, settings.contradiction_window_chars)
            wb = _windows(cb, settings.contradiction_window_chars)
            pairs.append({"a": sa, "b": sb, "eligible": True, "reason": None,
                          "claims_a": len(ca), "claims_b": len(cb),
                          "windows_a": len(wa), "windows_b": len(wb)})
            for sid, side in ((sa, ca), (sb, cb)):
                for ctx in side:
                    fingerprints.setdefault(sid, {})[ctx["claim_id"]] = \
                        contradictions.evidence_fingerprint(ctx)
            for x, win_a in enumerate(wa, 1):
                for y, win_b in enumerate(wb, 1):
                    messages = prompts.build_contradiction_messages(
                        sa, [_prompt_claim(c) for c in win_a],
                        sb, [_prompt_claim(c) for c in win_b])
                    wid = f"{sa}~{sb}#a{x}/{len(wa)}xb{y}/{len(wb)}"
                    windows.append({
                        "id": wid, "a": sa, "b": sb,
                        "claims_a": [c["claim_id"] for c in win_a],
                        "claims_b": [c["claim_id"] for c in win_b],
                        "input_chars": input_chars(messages, prompts.CONTRADICTION_SCHEMA),
                        "cache_key": cache_key(
                            messages, model_ref, prompts.CONTRADICTION_SCHEMA,
                            schema_version=art.CONTRADICTION_SCHEMA_VERSION,
                            prompt_version=art.CONTRADICTION_PROMPT_VERSION,
                            strategy_ref=strategy),
                    })
                    work[wid] = {"messages": messages,
                                 "contexts": {c["claim_id"]: c for c in win_a + win_b}}
    plan = {
        "documents": order,
        "independence": {s: independence_digest(prov[s]) for s in order},
        "pairs": pairs, "windows": windows, "fingerprints": fingerprints,
        "model_ref": model_ref, "endpoint": endpoint(model_ref, settings),
        "paid": requires_paid_authorization(model_ref),
        "prompt_version": art.CONTRADICTION_PROMPT_VERSION,
        "schema_version": art.CONTRADICTION_SCHEMA_VERSION,
        "window_chars": settings.contradiction_window_chars,
        "max_output_tokens": limit, "strategy": strategy,
    }
    if order_seed is not None:                    # absent otherwise: production digests unchanged
        plan["order_seed"] = order_seed
    names = {s: titles.confirmed_title(m) or m.get("original_filename") or s
             for s, m in docs.items()}
    return {"plan": plan, "work": work, "titles": names}


def digest(plan: dict[str, Any], limits: dict[str, int]) -> str:
    data = json.dumps({"plan": plan, "limits": limits}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def configured_limits(settings: Any) -> dict[str, int]:
    return {"max_calls": settings.contradiction_max_calls,
            "max_input_tokens": settings.contradiction_max_input_tokens}


def derived_limits(limits: dict[str, int], max_output_tokens: int) -> dict[str, int]:
    """The caps a run enforces: calls and input characters (hard), estimated input tokens
    (approximate) and the output allowance (every call's full output limit)."""
    return {"max_calls": limits["max_calls"],
            "max_input_chars": limits["max_input_tokens"] * CHARS_PER_TOKEN,
            "max_input_tokens": limits["max_input_tokens"],
            "max_output_tokens_per_call": max_output_tokens,
            "output_allowance": limits["max_calls"] * max_output_tokens}


def cached_keys(cache_path: Path, keys: list[str]) -> set[str]:
    """The keys the response cache holds, read-only (an estimate creates nothing)."""
    cache_path = Path(cache_path)
    if not keys or not cache_path.exists():
        return set()
    found: set[str] = set()
    try:
        conn = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
        try:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                found |= {r[0] for r in conn.execute(
                    "SELECT cache_key FROM response_cache WHERE cache_key IN "
                    f"({','.join('?' for _ in chunk)})", chunk)}
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    return found


def budget_check(plan: dict[str, Any], cached: set[str], limits: dict[str, int]) -> dict[str, Any]:
    """Whether the plan's calls (windows not expected from the cache), without retries, fit."""
    caps = derived_limits(limits, plan["max_output_tokens"])
    todo = [w for w in plan["windows"] if w["cache_key"] not in cached]
    calls = len(todo)
    chars = sum(w["input_chars"] for w in todo)
    est = sum(math.ceil(w["input_chars"] / CHARS_PER_TOKEN) for w in todo)
    over = [name for name, value, cap in (
        ("calls", calls, caps["max_calls"]),
        ("input characters", chars, caps["max_input_chars"]),
        ("estimated input tokens", est, caps["max_input_tokens"])) if value > cap]
    return {"windows": len(plan["windows"]), "cache_hits": len(plan["windows"]) - calls,
            "calls": calls, "input_chars": chars, "estimated_input_tokens": est,
            "max_output_tokens": calls * plan["max_output_tokens"], "limits": caps,
            "over": over, "fits": not over}


# --- saved estimates -------------------------------------------------------------------------------


def store_path(root: Path) -> Path:
    return Path(root) / "db" / STORE_NAME


def _load(root: Path) -> list[dict[str, Any]]:
    """Every saved estimate. Missing or malformed contents are empty: losing a record can only
    refuse a run."""
    try:
        body = json.loads(store_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(body, dict) or body.get("schema_version") != SCHEMA_VERSION \
            or not isinstance(body.get("records"), list):
        return []
    return [r for r in body["records"]
            if isinstance(r, dict) and isinstance(r.get("token"), str)
            and TOKEN.fullmatch(r["token"])]


def _save(root: Path, records: list[dict[str, Any]]) -> None:
    path = store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    _write_durably(tmp, (json.dumps({"schema_version": SCHEMA_VERSION, "records": records},
                                    indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    tmp.replace(path)
    _sync_dir(path.parent)


def find_estimate(root: Path, token: str) -> dict[str, Any] | None:
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return None
    return next((r for r in _load(root) if r["token"] == token), None)


# --- estimate --------------------------------------------------------------------------------------


def estimate(settings: Any, source_ids: list[str], *, model: str | None = None, client: Any = None,
             now: str | None = None, order_seed: int | None = None) -> dict[str, Any]:
    """Plan the check, without a model call, and save it under a token when it fits the limits.
    Returns `{plan, titles, budget, available, token, refused}` (`refused`: why no token)."""
    model_ref = resolve_model(model, settings)
    if not Path(settings.graph_db_path).exists():
        raise Refused("no_graph", "there is no graph yet: run the pipeline first")
    gconn = graph.connect(settings.graph_db_path)
    try:
        built = build_plan(settings, gconn, source_ids, model_ref, order_seed=order_seed)
    finally:
        gconn.close()
    plan = built["plan"]
    limits = configured_limits(settings)
    check = budget_check(plan, cached_keys(settings.response_cache_path,
                                           [w["cache_key"] for w in plan["windows"]]), limits)
    client = client or build_client(settings)
    out: dict[str, Any] = {"plan": plan, "titles": built["titles"], "budget": check,
                           "available": client.provider_available(model_ref), "token": None,
                           "refused": None}
    if not plan["windows"]:
        out["refused"] = "nothing_to_check"
        return out
    if not check["fits"]:
        out["refused"] = "over_budget"
        return out
    token = f"cce_{uuid.uuid4().hex}"
    record = {"token": token, "created_at": now or iso_now(), "documents": plan["documents"],
              "model_ref": model_ref, "limits": limits, "digest": digest(plan, limits),
              "windows": len(plan["windows"]), "calls": check["calls"]}
    if order_seed is not None:
        record["order_seed"] = order_seed
    with write_lock.hold(settings.reviews_dir, changes=False):
        _save(settings.root, _load(settings.root)[-(_KEEP - 1):] + [record])
    out["token"] = token
    return out


# --- budget ----------------------------------------------------------------------------------------


def _reported_input(usage: dict[str, Any]) -> int | None:
    """Reported input tokens, cache categories included once (ADR-0069 decision 6)."""
    value = usage.get("input_tokens")
    if not isinstance(value, int):
        return None
    if usage.get("family") == "anthropic":           # Anthropic input excludes cache tokens
        value += sum(v for v in (usage.get("cache_read_tokens"), usage.get("cache_write_tokens"))
                     if isinstance(v, int))
    return value


class Budget:
    """Reservations before every attempt; settlement after (the client's `guard`)."""

    def __init__(self, limits: dict[str, int], max_output_tokens: int) -> None:
        self.caps = derived_limits(limits, max_output_tokens)
        self.calls = self.input_chars = 0
        self.input_tokens = 0            # charged: reported where reported, else estimated
        self.output_tokens = 0           # charged: reported where reported, else reserved
        self.reported_input = self.estimated_input = 0
        self.reported_output = self.reserved_output = 0
        self.violation: str | None = None
        self.reported_models: set[str] = set()   # the model names the provider says answered
        self._open: tuple[int, int] | None = None

    def reserve(self, *, retry: int, input_chars: int, max_tokens: int) -> None:
        if self.violation:
            raise BudgetExhausted("the budget: an output-limit violation stopped further calls",
                                  retry)
        est = math.ceil(input_chars / CHARS_PER_TOKEN)
        over = [name for name, value, cap in (
            ("the call cap", self.calls + 1, self.caps["max_calls"]),
            ("the input-character cap", self.input_chars + input_chars,
             self.caps["max_input_chars"]),
            ("the estimated input-token budget", self.input_tokens + est,
             self.caps["max_input_tokens"]),
            ("the output allowance", self.output_tokens + max_tokens,
             self.caps["output_allowance"])) if value > cap]
        if over:
            raise BudgetExhausted(" and ".join(over), retry)
        self.calls += 1
        self.input_chars += input_chars
        self.input_tokens += est
        self.output_tokens += max_tokens
        self._open = (est, max_tokens)

    def settle(self, *, outcome: str, usage: dict[str, Any] | None) -> None:
        est, limit = self._open or (0, 0)
        self._open = None
        u = usage or {}
        if isinstance(u.get("model"), str) and u["model"]:
            self.reported_models.add(u["model"])
        reported_in = _reported_input(u)
        if reported_in is None:
            self.estimated_input += est
        else:
            self.input_tokens += reported_in - est
            self.reported_input += reported_in
        out = u.get("output_tokens")
        if isinstance(out, int):
            self.output_tokens += out - limit
            self.reported_output += out
        else:
            self.reserved_output += limit
        if isinstance(out, int) and out > limit:
            self.violation = (f"a response reported {out} output tokens, above the "
                              f"{limit}-token limit (the endpoint didn't enforce it)")
            raise OutputLimitViolated(self.violation)
        if outcome == "output_limit":
            raise WindowTruncated("the response stopped at the output limit (truncated)")

    def report(self) -> dict[str, Any]:
        return {"calls": self.calls, "input_chars": self.input_chars,
                "reported_input_tokens": self.reported_input,
                "estimated_input_tokens_not_reported": self.estimated_input,
                "reported_output_tokens": self.reported_output,
                "reserved_output_tokens_not_reported": self.reserved_output,
                "limits": self.caps, "output_limit_violation": self.violation,
                "reported_models": sorted(self.reported_models)}


# --- run -------------------------------------------------------------------------------------------


def _accept(settings: Any, token: str, *, allow_paid: bool, max_calls: int | None,
            max_input_tokens: int | None, client: Any) -> tuple[dict[str, Any], dict[str, Any],
                                                                dict[str, int]]:
    """Validate a token (the caller holds the write lock); return `(record, built plan, effective
    limits)`. Raises Refused — the token untouched — when anything doesn't hold. The effective
    limits are the tighter of the estimate's and the ones configured now, then the run's own."""
    record = find_estimate(settings.root, token)
    if record is None:
        raise Refused("unknown_token", "unknown, already used or expired estimate token; run "
                                       "`estimate` again")
    saved = {k: int(v) for k, v in record["limits"].items()}
    current = configured_limits(settings)
    limits = {k: min(saved[k], current[k]) for k in saved}
    for name, value in (("max_calls", max_calls), ("max_input_tokens", max_input_tokens)):
        if value is None:
            continue
        flag = "--" + name.replace("_", "-")
        if value <= 0:
            raise Refused("bad_limit", f"{flag} must be a positive number")
        if value > limits[name]:
            raise Refused("limit_raised", f"{flag} {value} is above the effective {limits[name]} "
                                          f"(this estimate's {saved[name]}, configured now "
                                          f"{current[name]}); raising a limit needs a new "
                                          "estimate")
        limits[name] = value
    model_ref = record["model_ref"]
    if requires_paid_authorization(model_ref) and not allow_paid:
        raise Refused("paid_authorization_required",
                      f"{model_ref} requires paid authorization: pass --allow-paid to authorize "
                      "this model for this run (it authorizes no other model and doesn't lift "
                      "the budget)")
    if not client.provider_available(model_ref):
        raise Refused("model_unavailable", f"{model_ref} is not configured (no credential or "
                                           "base URL); no other model is substituted")
    gconn = graph.connect(settings.graph_db_path)
    try:
        built = build_plan(settings, gconn, record["documents"], model_ref,
                           order_seed=record.get("order_seed"))
    except Refused as exc:
        raise Refused("estimate_stale", f"the selection is no longer valid ({exc}); run "
                                        "`estimate` again") from exc
    finally:
        gconn.close()
    if digest(built["plan"], saved) != record["digest"]:
        raise Refused("estimate_stale",
                      "the selected documents' claims, evidence or details, the windows, the "
                      "model, its endpoint or its settings changed since this estimate; run "
                      "`estimate` again")
    check = budget_check(built["plan"], cached_keys(
        settings.response_cache_path, [w["cache_key"] for w in built["plan"]["windows"]]), limits)
    if not check["fits"]:
        raise Refused("over_budget", "the plan doesn't fit the effective limits ("
                                     + ", ".join(check["over"]) + ")")
    return record, built, limits


def _consume(root: Path, token: str) -> None:
    _save(root, [r for r in _load(root) if r["token"] != token])


def _revalidate(gconn, settings: Any, plan: dict[str, Any], window: dict[str, Any]) -> str | None:
    """Why a window no longer stands as estimated, or None: its documents' independence inputs, and
    each of its claims still active with the same wording and evidence from its document."""
    prov = {m["source_id"]: get_provenance(m)
            for m in valid_manifests(settings.manifests_dir)[0]}
    for sid in (window["a"], window["b"]):
        if sid not in prov:
            return f"{sid} is no longer a valid document"
        if independence_digest(prov[sid]) != plan["independence"][sid]:
            return f"the Details of {sid} changed"
    active = set(graph.active_node_ids_of_type(gconn, "claim"))
    for sid, ids in ((window["a"], window["claims_a"]), (window["b"], window["claims_b"])):
        for cid in ids:
            if cid not in active:
                return f"claim {cid} is no longer active (hidden or retracted)"
            ctx = contradictions.scoped_claim_context(
                gconn, cid, sid, claims_dir=settings.wiki_dir / "Claims",
                markdown_dir=settings.markdown_dir)
            if ctx is None or contradictions.evidence_fingerprint(ctx) != \
                    plan["fingerprints"][sid][cid]:
                return f"claim {cid}'s wording or evidence changed"
    return None


def _verdicts(response: dict[str, Any], window: dict[str, Any]) -> tuple[dict[tuple[str, str],
                                                                            dict[str, Any]], int]:
    """The in-scope pairs (`{(lo, hi): {confidence, explanation}}`) and how many were dropped."""
    a_ids, b_ids = set(window["claims_a"]), set(window["claims_b"])
    out: dict[tuple[str, str], dict[str, Any]] = {}
    invalid = 0
    for v in response.get("contradictions") or []:
        x, y = v["claim_a"], v["claim_b"]
        if x in b_ids and y in a_ids:
            x, y = y, x
        if x not in a_ids or y not in b_ids:
            invalid += 1
            continue
        key = (x, y) if x < y else (y, x)
        out.setdefault(key, {"confidence": max(0.0, min(1.0, float(v["confidence"]))),
                             "explanation": str(v["explanation"])})
    return out, invalid


def _file(gconn, settings: Any, plan: dict[str, Any], window: dict[str, Any],
          verdicts: dict[tuple[str, str], dict[str, Any]], *, job_id: str,
          now: str) -> dict[str, int]:
    """File a revalidated window's verdicts (the caller holds the write lock). Each side shows and
    records only its own document's evidence (`judged_scope`)."""
    reviews_dir = settings.reviews_dir
    scope = {c: window["a"] for c in window["claims_a"]} | \
        {c: window["b"] for c in window["claims_b"]}
    counts = {"proposed": 0, "already_proposed": 0, "decided": 0}
    for (lo, hi), verdict in sorted(verdicts.items()):
        sides = [contradictions.scoped_claim_context(
            gconn, c, scope[c], claims_dir=settings.wiki_dir / "Claims",
            markdown_dir=settings.markdown_dir) for c in (lo, hi)]
        rid = reviews.review_id("resolve_contradiction", {"claim_a": lo, "claim_b": hi})
        existing = graph.contradiction_between(gconn, lo, hi)
        state, _item = contradictions._review_state(reviews_dir, rid)
        if {r["status"] for r in existing} & {"active", "rejected"} or state == "decided":
            counts["decided"] += 1                        # a human decided this pair
            continue
        if state in ("pending", "unreadable"):
            counts["already_proposed"] += 1               # left as it is (maintenance checks it)
            continue
        for r in existing:                                # a proposal that lost its review item
            if r["status"] == "proposed":
                graph.set_status(gconn, r["edge_id"], "superseded", now=now)
        anchor = sides[0]["citations"][0]
        graph.upsert_assertion(
            gconn, src_id=lo, dst_id=hi, edge_type="contradicts", asserted_by="llm",
            status="proposed", confidence=verdict["confidence"],
            evidence_source_id=anchor["source_id"], evidence_char_start=anchor["char_start"],
            evidence_char_end=anchor["char_end"], review_id=rid, job_id=job_id, now=now)
        reviews.create_review_item(
            reviews_dir, review_type="resolve_contradiction", subject={"claim_a": lo, "claim_b": hi},
            proposal={"outcomes": ["acknowledge", "supersede", "reject"],
                      "confidence": verdict["confidence"], "explanation": verdict["explanation"],
                      "sides": sides},
            context={"asserted_by": "llm",
                     "judged_evidence": {c: plan["fingerprints"][scope[c]][c] for c in (lo, hi)},
                     "judged_scope": {lo: scope[lo], hi: scope[hi]},
                     "judged_by": {"job_id": job_id, "window": window["id"],
                                   "model_ref": plan["model_ref"],
                                   "prompt_version": plan["prompt_version"], "judged_at": now}},
            priority="medium", now=now)
        counts["proposed"] += 1
    return counts


class _Progress:
    """A run's coverage record, checkpointed on its job as it goes."""

    def __init__(self, job_id: str, token: str, plan: dict[str, Any], limits: dict[str, int],
                 budget: Budget, now: str) -> None:
        self.job_id, self.token, self.plan, self.limits, self.budget, self.now = (
            job_id, token, plan, limits, budget, now)
        self.phase = "accepted"
        self.status_of = {w["id"]: "remaining" for w in plan["windows"]}
        self.failed: dict[str, str] = {}
        self.in_progress: str | None = None
        self.from_cache = 0
        self.counts = {"proposed": 0, "already_proposed": 0, "decided": 0, "invalid_pairs": 0}
        self.stopped: str | None = None
        self.maintenance: dict[str, Any] | None = None

    def complete(self) -> bool:
        return all(v == "completed" for v in self.status_of.values())

    def status(self) -> str:
        clean = self.complete() and not self.budget.violation and \
            not (self.maintenance or {}).get("error")
        return "succeeded" if clean else "partial"

    def summary(self, status: str) -> dict[str, Any]:
        plan = self.plan
        return {
            "job_id": self.job_id, "mode": "run", "phase": self.phase, "status": status,
            "estimate_token": self.token, "documents": plan["documents"], "pairs": plan["pairs"],
            "model_ref": plan["model_ref"], "endpoint": plan["endpoint"],
            "prompt_version": plan["prompt_version"], "schema_version": plan["schema_version"],
            "max_output_tokens": plan["max_output_tokens"], "window_chars": plan["window_chars"],
            "limits": self.limits,
            "coverage": {"complete": self.complete(), "windows": len(plan["windows"]),
                         "completed": [i for i, v in self.status_of.items() if v == "completed"],
                         "failed": dict(self.failed),
                         "remaining": [i for i, v in self.status_of.items() if v == "remaining"],
                         "in_progress": self.in_progress, "from_cache": self.from_cache},
            "stopped": self.stopped, "budget": self.budget.report(),
            "contradictions_proposed": self.counts["proposed"],
            "already_proposed": self.counts["already_proposed"],
            "skipped_human_decided": self.counts["decided"],
            "out_of_scope_pairs_dropped": self.counts["invalid_pairs"],
            "maintenance": self.maintenance, "checked_at": self.now,
        }


def run(settings: Any, token: str, *, allow_paid: bool = False, max_calls: int | None = None,
        max_input_tokens: int | None = None, client: Any = None, record_job: bool = True,
        now: str | None = None) -> dict[str, Any]:
    """Execute a saved estimate. Raises Refused (token untouched; the job, if recorded, `skipped`)
    before acceptance; after acceptance returns the run summary with its coverage record.

    The job is recorded before the write lock is taken, the accepted scope is written to it before
    the token is spent, and progress is checkpointed before and after every window, so an
    interrupted process leaves a `running` job naming exactly which windows completed."""
    now = now or iso_now()
    if not Path(settings.graph_db_path).exists():
        raise Refused("no_graph", "there is no graph yet: run the pipeline first")
    client = client or build_client(settings, cache=ResponseCache(settings.response_cache_path))
    job_id = f"job_{uuid.uuid4().hex[:16]}"
    conn = None
    if record_job:
        db.init_db(settings.jobs_db_path)
        conn = db.connect(settings.jobs_db_path)
        db.insert_job(conn, job_id=job_id, job_type="detect_contradictions", status="running",
                      created_at=now, started_at=now,
                      metadata={"mode": "run", "phase": "validating", "estimate_token": token})
    progress: _Progress | None = None

    def checkpoint() -> None:
        if conn is not None and progress is not None:
            db.update_job(conn, job_id, metadata=progress.summary("running"))

    try:
        with write_lock.hold(settings.reviews_dir):
            try:
                _record, built, limits = _accept(
                    settings, token, allow_paid=allow_paid, max_calls=max_calls,
                    max_input_tokens=max_input_tokens, client=client)
            except Refused as exc:
                if conn is not None:
                    db.update_job(conn, job_id, status="skipped", finished_at=iso_now(),
                                  error_message=f"refused ({exc.code}): {exc}",
                                  metadata={"mode": "run", "phase": "refused",
                                            "estimate_token": token, "refused": exc.code})
                raise
            plan, work = built["plan"], built["work"]
            budget = Budget(limits, plan["max_output_tokens"])
            progress = _Progress(job_id, token, plan, limits, budget, now)
            checkpoint()                                  # the accepted scope, before spending it
            _consume(settings.root, token)
            gconn = graph.connect(settings.graph_db_path)
            try:
                maint = contradictions.targeted_maintenance(
                    gconn, reviews_dir=settings.reviews_dir,
                    claims_dir=settings.wiki_dir / "Claims", markdown_dir=settings.markdown_dir,
                    manifests_dir=settings.manifests_dir, sources=set(plan["documents"]), now=now)
            finally:
                gconn.close()
            progress.maintenance = {"retired": len(maint["retired"]), "error": maint["error"]}
            progress.phase = "judging"
            checkpoint()
        _judge_windows(settings, client, plan, work, progress, checkpoint)
        progress.phase = "finished"
        status = progress.status()
        summary = progress.summary(status)
        if conn is not None:
            db.update_job(conn, job_id, status=status, finished_at=iso_now(), metadata=summary)
        return summary
    except Refused:
        raise
    except BaseException as exc:                          # a failure or an interruption
        if conn is not None:
            what = "interrupted" if not isinstance(exc, Exception) else "failed"
            db.update_job(conn, job_id, status="failed", finished_at=iso_now(),
                          error_message=f"{what}: {type(exc).__name__}: {exc}",
                          **({"metadata": progress.summary("failed")} if progress else {}))
        raise
    finally:
        if conn is not None:
            conn.close()


def _judge_windows(settings: Any, client: Any, plan: dict[str, Any], work: dict[str, Any],
                   progress: _Progress, checkpoint: Any) -> None:
    budget = progress.budget
    gconn = graph.connect(settings.graph_db_path)
    try:
        for w in plan["windows"]:
            reason = _revalidate(gconn, settings, plan, w)
            if reason:                                    # before sending: needs a new estimate
                progress.stopped = f"stopped: {reason} since the estimate; estimate again"
                break
            progress.in_progress = w["id"]
            checkpoint()
            calls_before = budget.calls
            try:
                response = client.parse(
                    work[w["id"]]["messages"], prompts.CONTRADICTION_SCHEMA, plan["model_ref"],
                    schema_version=plan["schema_version"], prompt_version=plan["prompt_version"],
                    strategy_ref=plan["strategy"], operation=w["id"],
                    max_tokens=plan["max_output_tokens"], guard=budget)
            except BudgetExhausted as exc:
                if exc.retry:                             # some attempts were sent: it failed
                    progress.status_of[w["id"]] = "failed"
                    progress.failed[w["id"]] = f"stopped by the budget after {exc.retry} attempt(s)"
                progress.stopped = f"stopped by the budget: {exc}"
                break
            except WindowTruncated as exc:
                progress.status_of[w["id"]] = "failed"
                progress.failed[w["id"]] = str(exc)
                continue
            except OutputLimitViolated as exc:            # not cached, not filed
                progress.status_of[w["id"]] = "failed"
                progress.failed[w["id"]] = f"output-limit violation: {exc}"
                progress.stopped = f"stopped: {exc}"
                break
            except ParseError as exc:
                progress.status_of[w["id"]] = "failed"
                cause = exc.__cause__
                if isinstance(cause, AdapterError) and cause.outcome == "adapter_error":
                    progress.failed[w["id"]] = f"{plan['model_ref']} failed: {cause}"
                    progress.stopped = (f"stopped: {plan['model_ref']} failed on every attempt; "
                                        "no other model is substituted")
                    break
                progress.failed[w["id"]] = f"no valid response: {exc}"
                continue
            finally:
                progress.in_progress = None
            if budget.calls == calls_before:
                progress.from_cache += 1
            verdicts, invalid = _verdicts(response, w)
            with write_lock.hold(settings.reviews_dir):
                reason = _revalidate(gconn, settings, plan, w)
                if reason is None:
                    filed = _file(gconn, settings, plan, w, verdicts, job_id=progress.job_id,
                                  now=progress.now)
            if reason:                                    # changed during the call: not filed
                progress.status_of[w["id"]] = "failed"
                progress.failed[w["id"]] = f"not filed: {reason} during the call"
                progress.stopped = f"stopped: {reason} during the run; estimate again"
                checkpoint()
                break
            progress.counts["invalid_pairs"] += invalid
            for k, v in filed.items():
                progress.counts[k] += v
            progress.status_of[w["id"]] = "completed"
            checkpoint()
        else:
            return
        checkpoint()
    finally:
        gconn.close()
