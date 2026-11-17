#!/usr/bin/env python3
"""Synthetic benchmark vault generator for the Apply preview benchmark (ADR-0074 change 7c).

Usage:
    uv run python scripts/bench_fixture.py build DEST [--scale N] [--seed S] [--preset baseline|smoke]

Builds a deterministic, key-free vault through the **real** pipeline — intake, extraction,
enrichment, claims, items (tag suggestions), keeps, synthesis and the contradiction check — with fake
in-process model adapters (no network, no API key, no billable call). Two live setup Applies (real
preview tokens) seed the active / hidden targets the unhide and hide types need. It then files the two
declared review queues (ADR-0074 7c Q5) into two copies of the same corpus:

    DEST/corpus/   the shared corpus, no benchmark queue
    DEST/heavy/    + the heavy 50-item queue (judged; a backlog stress baseline)
    DEST/light/    + the light 5-item queue (a small interactive batch; not judged)
    DEST/fixture.json   the declaration: distribution, counts, fan-out, targets, fingerprints

Every profile vault passes `scripts/validate_all.py`, and one real preview of it must return `ok`
with an `applied` outcome for every approval, before the fixture is declared built.

Safety (7c Q3): DEST must not be the live vault (the repository, or the configured vault root), must
not contain it or lie inside it, and must be absent or an empty directory. Nothing existing is ever
deleted or overwritten. The corpus content is synthetic: only aggregate UAT ratios calibrate it.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GENERATOR_VERSION = "1"
DECLARATION_SCHEMA = 2      # 2: link-safe fingerprint walk + the declared code fingerprint
CODE_DIRS = ("app", "scripts", "templates", "policies")
MODEL = "anthropic:bench-fake"

# The declared corpus parameters. `baseline` is calibrated on UAT aggregate ratios (12-document clone,
# 2026-11: raw median ≈ 565 KB, mean ≈ 1.4 MB; ≈ 64 KB normalized Markdown, ≈ 83 claims and ≈ 15 tag
# suggestions per document) and ADR-0024's ≈ 600 files up to 50 MB; `smoke` shrinks the bytes only.
PRESETS: dict[str, dict[str, Any]] = {
    "baseline": {"raw_median_bytes": 565_000, "raw_sigma": 1.35, "raw_min_bytes": 16_000,
                 "raw_max_bytes": 48 * 1024 * 1024, "tail_every": 100,
                 "tail_bytes": [44 * 1024 * 1024, 48 * 1024 * 1024], "text_bytes": 64_000,
                 "vocabulary": 2000},
    "smoke": {"raw_median_bytes": 12_000, "raw_sigma": 0.5, "raw_min_bytes": 4_000,
              "raw_max_bytes": 64_000, "tail_every": 0, "tail_bytes": [0, 0], "text_bytes": 3_000,
              "vocabulary": 160},
}
CLAIM_EVERY = 9          # one claim per 9 sentences ≈ 83 claims per 64 KB document (UAT ratio)
TAGS_PER_DOC = 15        # items' DEFAULT_SUGGESTION_CAP
HEAD_TAGS = 40           # the high-degree set: each document suggests 3 of them, round-robin
HEAD_PER_DOC = 3
TAIL_KEEP_PROBABILITY = 0.06
SYNTH_TOPICS = 4         # dedicated synthesis topics, each kept on its own pair of documents
CONTRA_PAIRS = 4         # contradiction document pairs
MIN_SCALE = 48           # 16 provenance documents + enough plain ones for 2 kept docs per head tag

_SYLL = ("ka", "lor", "ven", "tri", "sol", "mar", "quin", "dal", "fen", "ros", "tal", "vir", "nox",
         "bel", "cor", "dra", "el", "fa", "gor", "hal", "ix", "jun", "kel", "lum", "mor", "nel",
         "ori", "pel", "qua", "ru", "sen", "tor", "ul", "vas", "wen", "xan", "yor", "zel")
_VERBS = ("improves", "reduces", "stabilizes", "extends", "constrains", "accelerates", "documents",
          "anticipates", "measures", "supports")
_NOUNS = ("throughput", "latency", "coverage", "accuracy", "resilience", "adoption", "variance",
          "budget", "workload", "retention")
_QUALS = ("under load", "across regions", "in pilot studies", "for small teams", "over two quarters",
          "in controlled trials", "at scale", "for new users", "in audits", "during migration")


class FixtureRefused(RuntimeError):
    """The destination or scale is not allowed; nothing was written."""


# --- the deterministic plan --------------------------------------------------------------------


def _word(rng: random.Random) -> str:
    return "".join(rng.choice(_SYLL) for _ in range(rng.choice((2, 3)))).capitalize()


def plan(scale: int, seed: int, preset: str) -> dict[str, Any]:
    """The whole fixture as data, from (scale, seed, preset) alone: documents, raw sizes, tag
    assignments, roles and every queue target. Pure and deterministic."""
    if preset not in PRESETS:
        raise FixtureRefused(f"unknown preset {preset!r}")
    if scale < MIN_SCALE:
        raise FixtureRefused(f"scale {scale} is below the minimum {MIN_SCALE}")
    from app.backend import taxonomy
    p = PRESETS[preset]
    rng = random.Random(f"bench-fixture:{seed}")
    types = sorted(taxonomy.ITEM_TYPES)
    names: list[str] = []
    seen: set[str] = set()
    vocab = max(p["vocabulary"], HEAD_TAGS + 2 * TAGS_PER_DOC + SYNTH_TOPICS + 20)
    while len(names) < vocab:
        name = f"{_word(rng)} {_word(rng)}"
        key = name.lower()
        if key not in seen and not any(key in s or s in key for s in seen):
            seen.add(key)
            names.append(name)
    tags = [{"name": n, "item_type": rng.choice(types)} for n in names]
    head = list(range(HEAD_TAGS))
    synth_tags = list(range(HEAD_TAGS, HEAD_TAGS + SYNTH_TOPICS))
    tail = list(range(HEAD_TAGS + SYNTH_TOPICS, len(tags)))

    # Roles: the first 2*SYNTH_TOPICS documents are synthesis pairs, the next 2*CONTRA_PAIRS are
    # contradiction pairs (all with distinct provenance authors); the rest are plain documents.
    docs: list[dict[str, Any]] = []
    for d in range(scale):
        if d < 2 * SYNTH_TOPICS:
            role = "synth"
        elif d < 2 * SYNTH_TOPICS + 2 * CONTRA_PAIRS:
            role = "contra"
        else:
            role = "plain"
        heads = [head[(HEAD_PER_DOC * d + k) % HEAD_TAGS] for k in range(HEAD_PER_DOC)]
        own = [synth_tags[d // 2]] if role == "synth" else []
        rest = rng.sample(tail, TAGS_PER_DOC - len(heads) - len(own))
        if p["tail_every"] and d % p["tail_every"] == p["tail_every"] - 1:
            raw = rng.randint(*p["tail_bytes"])
        else:
            raw = int(min(p["raw_max_bytes"], max(p["raw_min_bytes"], rng.lognormvariate(
                math.log(p["raw_median_bytes"]), p["raw_sigma"]))))
        docs.append({"index": d, "file": f"report_{d:04d}.pdf", "role": role, "raw_bytes": raw,
                     "tags": own + heads + rest, "author": f"Author {d:04d}" if role != "plain" else None})
    # Keeps: plain documents keep their head tags and some tail tags; a synthesis document keeps only
    # its own topic; a contradiction document keeps nothing (else provenance would make extra topics).
    for doc in docs:
        if doc["role"] == "synth":
            doc["keep"] = doc["tags"][:1]
        elif doc["role"] == "contra":
            doc["keep"] = []
        else:
            doc["keep"] = [t for t in doc["tags"] if t in head
                           or random.Random(f"{seed}:{doc['index']}:{t}").random()
                           < TAIL_KEEP_PROBABILITY]
    kept_anywhere = {t for doc in docs for t in doc["keep"]}
    suggested = {t for doc in docs for t in doc["tags"]}
    plain = [doc for doc in docs if doc["role"] == "plain"]
    active_tail = sorted(t for t in tail if t in kept_anywhere)
    candidates = sorted(t for t in tail if t in suggested and t not in kept_anywhere)
    if len(active_tail) < 6 or len(candidates) < 7:
        raise FixtureRefused("scale too small for the declared targets")
    # Plain documents reserved for source-level operations (never also a claim target).
    src_ops = {"archive": [plain[0]["index"], plain[1]["index"]], "hide": [plain[2]["index"]],
               "unhide": [plain[3]["index"]]}
    reserved_docs = {i for v in src_ops.values() for i in v}
    claim_docs = [doc["index"] for doc in plain if doc["index"] not in reserved_docs][:7]
    # Contention keeps (7c Q4): a never-kept candidate suggested by a plain, unreserved document,
    # so every trial's keep is a real keep (it activates the candidate) on an ordinary document.
    free = [doc for doc in plain if doc["index"] not in reserved_docs
            and doc["index"] not in claim_docs]
    contention = []
    for cand in candidates[3:]:
        if cand in candidates[5:7]:
            continue
        doc = next((x for x in free if cand in x["tags"]), None)
        if doc is not None:
            contention.append({"tag": cand, "doc": doc["index"]})
        if len(contention) == 2:
            break
    if len(contention) < 2:
        raise FixtureRefused("scale too small for the contention targets")
    targets = {
        "merge_pairs": [[head[2 * i], head[2 * i + 1]] for i in range(8)],
        # Item deprecations target never-kept candidates: deprecating a kept tag would leave kept
        # links to a deprecated tag, which validate_tag_links rejects (ADR-0066).
        "retype": head[16:24], "split": head[24:30], "deprecate_items": candidates[5:7],
        "hide_items": head[32:34], "duplicate_pairs": [[head[34], head[35]], [head[36], head[37]]],
        "unhide_items": active_tail[:2], "promote": candidates[:3],
        "contention": contention,
        "synth_topics": synth_tags,       # [A, B] proposed in the queue; C active, D hidden at setup
        "sources": src_ops,
        "claim_docs": claim_docs,         # hide x2, unhide x2 (hidden at setup), deprecate x1
    }
    return {"generator_version": GENERATOR_VERSION, "scale": scale, "seed": seed, "preset": preset,
            "parameters": dict(p, claim_every=CLAIM_EVERY, tags_per_doc=TAGS_PER_DOC,
                               head_tags=HEAD_TAGS, head_per_doc=HEAD_PER_DOC,
                               tail_keep_probability=TAIL_KEEP_PROBABILITY,
                               synth_topics=SYNTH_TOPICS, contra_pairs=CONTRA_PAIRS),
            "tags": tags, "docs": docs, "targets": targets}


def doc_sentences(plan_: dict[str, Any], d: int) -> list[str]:
    """Document `d`'s sentences, unique in the corpus, each naming one of its tags."""
    doc = plan_["docs"][d]
    rng = random.Random(f"bench-text:{plan_['seed']}:{d}")
    names = [plan_["tags"][t]["name"] for t in doc["tags"]]
    out: list[str] = []
    size = 0
    n = 0
    while size < plan_["parameters"]["text_bytes"]:
        s = (f"Record D{d:04d} N{n:04d} notes that {names[n % len(names)]} "
             f"{rng.choice(_VERBS)} the {rng.choice(_NOUNS)} {rng.choice(_QUALS)}.")
        out.append(s)
        size += len(s) + 1
        n += 1
    return out


# --- raw files -----------------------------------------------------------------------------------


def pdf_bytes(lines_per_page: list[list[str]], pad_to: int, pad_seed: str) -> bytes:
    """A minimal PDF with one Tj per line, padded to `pad_to` bytes by one unreferenced stream of
    incompressible bytes (so raw size follows the declared distribution without inflating text)."""
    objects: dict[int, bytes] = {}
    n = len(lines_per_page)
    kid_ids = [4 + 2 * i for i in range(n)]
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kid_ids)}] "
                  f"/Count {n} >>").encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for i, lines in enumerate(lines_per_page):
        pid, cid = 4 + 2 * i, 5 + 2 * i
        ops = []
        for j, line in enumerate(lines):
            esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"{'40 760 Td' if j == 0 else '0 -14 Td'} ({esc}) Tj")
        stream = ("BT /F1 9 Tf " + " ".join(ops) + " ET").encode()
        objects[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cid} 0 R "
                        f"/Resources << /Font << /F1 3 0 R >> >> >>").encode()
        objects[cid] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)

    def assemble(pad: bytes) -> bytes:
        objs = dict(objects)
        if pad:
            objs[max(objs) + 1] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(pad), pad)
        out = bytearray(b"%PDF-1.4\n")
        offsets: dict[int, int] = {}
        for num in sorted(objs):
            offsets[num] = len(out)
            out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
        xref = len(out)
        top = max(objs)
        out += f"xref\n0 {top + 1}\n".encode() + b"0000000000 65535 f \n"
        for num in range(1, top + 1):
            out += f"{offsets[num]:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {top + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
        return bytes(out)

    base = assemble(b"")
    need = pad_to - len(base) - 120
    if need <= 0:
        return base
    return assemble(random.Random(pad_seed).randbytes(need))


def write_raw(root: Path, plan_: dict[str, Any]) -> None:
    inbox = root / "raw" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for doc in plan_["docs"]:
        d = doc["index"]
        lines = [f"Synthetic Report D{d:04d}"] + doc_sentences(plan_, d)
        pages = [lines[i:i + 50] for i in range(0, len(lines), 50)]
        (inbox / doc["file"]).write_bytes(
            pdf_bytes(pages, doc["raw_bytes"], f"bench-pad:{plan_['seed']}:{d}"))


# --- fake model adapters (key-free; the real LLMClient validates their output) -------------------

_DOC = re.compile(r"Synthetic\s+Report\s+D(\d{4})")
_SENT = re.compile(r"Record\s+D(\d{4})\s+N(\d{4})\s+notes\s+that\s+([^.]*)\.")
_CLM = re.compile(r"^\[(clm_[0-9a-f]{16})\] ", re.M)


class _Adapter:
    name = "anthropic"
    supports_batch = False

    def __init__(self, plan_: dict[str, Any]) -> None:
        self.plan = plan_

    def available(self) -> bool:
        return True

    def _doc(self, body: str) -> dict[str, Any] | None:
        m = _DOC.search(body)
        return self.plan["docs"][int(m.group(1))] if m else None


class EnrichFake(_Adapter):
    def parse(self, messages, schema, model_id, *, max_tokens):
        doc = self._doc(messages[-1]["content"])
        tags = [self.plan["tags"][t] for t in (doc["tags"][:5] if doc else [])]
        d = doc["index"] if doc else 0
        return {"title": f"Synthetic Report D{d:04d}", "authors": [], "publisher": "",
                "summary": f"A synthetic benchmark report, number {d}, covering several topics.",
                "tags": [{"name": t["name"], "item_type": t["item_type"]} for t in tags]}


class ClaimsFake(_Adapter):
    def parse(self, messages, schema, model_id, *, max_tokens):
        out = []
        for m in _SENT.finditer(messages[-1]["content"]):
            if int(m.group(2)) % CLAIM_EVERY:
                continue
            quote = " ".join(m.group(0).split())
            out.append({"claim": f"In D{m.group(1)} N{m.group(2)}, {' '.join(m.group(3).split())}",
                        "quote": quote})
        return {"claims": out}


class ItemsFake(_Adapter):
    def parse(self, messages, schema, model_id, *, max_tokens):
        doc = self._doc(messages[-1]["content"])
        tags = [self.plan["tags"][t] for t in (doc["tags"] if doc else [])]
        return {"items": [{"name": t["name"], "item_type": t["item_type"], "aliases": []}
                          for t in tags]}


class SynthFake(_Adapter):
    def parse(self, messages, schema, model_id, *, max_tokens):
        return {"summary": "Two independent synthetic reports discuss this topic.",
                "synthesis": "Across independent synthetic reports the topic is described "
                             "consistently, with complementary observations.",
                "confidence": 0.8}


class JudgeFake(_Adapter):
    """Contradicts the first claim of each side on its first call with claims on both sides."""

    def __init__(self, plan_: dict[str, Any]) -> None:
        super().__init__(plan_)
        self.done = False

    def parse_with_usage(self, messages, schema, model_id, *, max_tokens):
        user = messages[-1]["content"]

        def ids(block: str) -> list[str]:
            return _CLM.findall(user.split(f"<{block}>", 1)[1].split(f"</{block}>", 1)[0])
        a, b = ids("claims_a"), ids("claims_b")
        if self.done or not a or not b:
            return {"contradictions": []}, None
        self.done = True
        return {"contradictions": [{"claim_a": a[0], "claim_b": b[0], "confidence": 0.8,
                                    "explanation": "opposite synthetic outcomes"}]}, None


# --- stage helpers (run inside a stage process bound to one vault root) -------------------------


def _client(root: Path, adapter: Any) -> Any:
    from app.llm.cache import ResponseCache
    from app.llm.client import LLMClient
    return LLMClient({"anthropic": adapter}, cache=ResponseCache(root / "db" / "llm_cache.sqlite"))


def _log(msg: str) -> None:
    print(f"[bench_fixture {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _sid_map(root: Path) -> dict[str, str]:
    from app.backend import manifests
    return {m["original_filename"]: m["source_id"]
            for m in manifests.scan_manifests(root / "raw" / "manifests").valid}


def _item_id(plan_: dict[str, Any], t: int) -> str:
    from app.workers import items
    return items.node_id(plan_["tags"][t]["name"])


def _check(resp: Any, what: str) -> dict[str, Any]:
    if resp.status_code != 200:
        raise RuntimeError(f"{what}: HTTP {resp.status_code} {resp.text[:400]}")
    return resp.json()


def _approve(api: Any, rid: str, body: dict[str, Any] | None = None) -> None:
    _check(api.post(f"/reviews/{rid}/approve", json=body or {}), f"approve {rid}")


def _live_apply(api: Any, expect: set[str], what: str) -> None:
    """A real preview, then Apply with its token; every expected review must be `applied`."""
    pv = _check(api.post("/reviews/apply/dry-run"), f"{what} preview")
    if pv["status"] != "ok" or not pv.get("preview_token"):
        raise RuntimeError(f"{what} preview not ok: {pv['status']} {pv.get('reason')} "
                           f"{pv.get('validators')} {pv.get('warnings')}")
    _require_applied(pv["outcomes"], expect, f"{what} preview")
    res = _check(api.post("/reviews/apply", json={"preview_token": pv["preview_token"]}), what)
    if res.get("status") != "applied":
        raise RuntimeError(f"{what} not applied: {res.get('status')} {res.get('warnings')}")


def _require_applied(outcomes: list[dict[str, Any]], expect: set[str], what: str) -> None:
    got = {o["review_id"]: o for o in outcomes if o["review_id"] in expect}
    bad = {rid: (o["outcome"], o.get("reason"), (o.get("details") or {}).get("normalized"))
           for rid, o in got.items()
           if o["outcome"] != "applied" or (o.get("details") or {}).get("normalized")}
    missing = expect - set(got)
    if bad or missing:
        raise RuntimeError(f"{what}: not applied {bad}, missing {sorted(missing)}")


def stage_corpus(root: Path, plan_: dict[str, Any]) -> None:
    """The real pipeline with fake adapters, then two live setup Applies."""
    import dataclasses

    from fastapi.testclient import TestClient

    from app.backend import graph, manifests, write_lock
    from app.backend import main as api_main
    from app.backend.config import get_settings
    from app.workers import claims, contradiction_check, enrich, extract, intake, items, reviews
    from app.workers import synthesis, tag_links
    jobs = root / "db" / "jobs.sqlite"
    write_raw(root, plan_)
    _log("raw files written; intake + extraction")
    intake.scan_inbox(root, jobs_db=jobs)
    ext = extract.extract_sources(root, jobs_db=jobs)
    sids = _sid_map(root)
    if len(sids) != plan_["scale"]:
        raise RuntimeError(f"intake catalogued {len(sids)} of {plan_['scale']} documents")
    if ext.get("errors") or ext.get("status") == "failed":
        raise RuntimeError(f"extraction: {ext.get('errors')} error(s), status {ext.get('status')}")
    sid = {doc["index"]: sids[doc["file"]] for doc in plan_["docs"]}
    _log("enrichment")
    enrich.enrich_sources(root, client=_client(root, EnrichFake(plan_)), model_ref=MODEL, jobs_db=jobs)
    with write_lock.hold(root / "reviews"):
        for doc in plan_["docs"]:
            if doc["author"]:
                manifests.set_provenance(root / "raw" / "manifests", sid[doc["index"]],
                                         author=doc["author"])
    _log("claims")
    claims.extract_claims(root, client=_client(root, ClaimsFake(plan_)), model_ref=MODEL,
                          jobs_db=jobs)
    _log("items (tag suggestions)")
    items.extract_items(root, client=_client(root, ItemsFake(plan_)), model_ref=MODEL, jobs_db=jobs)
    _log("keeps")
    affected: set[str] = set()
    gconn = graph.connect(root / "db" / "graph.sqlite")
    try:
        for doc in plan_["docs"]:
            want = {_item_id(plan_, t) for t in doc["keep"]}
            if not want:
                continue
            with write_lock.hold(root / "reviews"):
                for link in graph.source_tag_links(gconn, sid[doc["index"]]):
                    if link["dst_id"] in want and link["status"] == "active":
                        out = tag_links.keep(gconn, source_id=sid[doc["index"]],
                                             node_id=link["dst_id"], wiki_dir=root / "wiki",
                                             reviews_dir=root / "reviews")
                        affected.update(out.get("affected_sources") or [])
    finally:
        gconn.close()
    tag_links.finish(root, affected, wiki_dir=root / "wiki", templates_dir=root / "templates")
    _log("synthesis")
    synthesis.generate_syntheses(root, client=_client(root, SynthFake(plan_)), model_ref=MODEL,
                                 jobs_db=jobs)
    _log("contradiction check")
    st = dataclasses.replace(get_settings(root), contradiction_max_calls=50,
                             contradiction_max_input_tokens=2_000_000,
                             contradiction_max_output_tokens=512, enrich_local_base_url=None)
    first = 2 * SYNTH_TOPICS
    for k in range(CONTRA_PAIRS):
        pair = [sid[first + 2 * k], sid[first + 2 * k + 1]]
        client = _client(root, JudgeFake(plan_))
        est = contradiction_check.estimate(st, pair, model=MODEL, client=client)
        if not est.get("token"):
            raise RuntimeError(f"contradiction estimate refused: {est.get('refused')}")
        contradiction_check.run(st, est["token"], allow_paid=True, client=client)
    api = TestClient(api_main.app, base_url="http://127.0.0.1")
    syn = {t: _synth_review(root, plan_, t) for t in plan_["targets"]["synth_topics"]}
    if any(v is None for v in syn.values()):
        raise RuntimeError(f"expected {SYNTH_TOPICS} synthesis proposals, got {syn}")
    # Setup Apply 1: topics C and D become active syntheses.
    t_c, t_d = plan_["targets"]["synth_topics"][2:4]
    for t in (t_c, t_d):
        _approve_synthesis(api, syn[t])
    _live_apply(api, {syn[t_c], syn[t_d]}, "setup apply 1")
    # Setup Apply 2: hidden targets for the queue's unhide types.
    gconn = graph.connect(root / "db" / "graph.sqlite")
    try:
        filed = []
        t = plan_["targets"]
        filed.append(_file(reviews, root, "hide_synthesis", _page_subject(gconn, _syn_id(root, plan_, t_d), "Synthesis"),
                           {"to_status": "hidden"}, {"node_type": "synthesis"}))
        filed.append(_file(reviews, root, "hide_content", {"source_id": sid[t["sources"]["unhide"][0]]},
                           {"to_status": "hidden"}))
        for it in t["unhide_items"]:
            filed.append(_file(reviews, root, "hide_semantic_page",
                               _page_subject(gconn, _item_id(plan_, it), "Items"),
                               {"to_status": "hidden"}, {"node_type": "item"}))
        for cid in _claims_of(gconn, sid, t["claim_docs"][2:4]):
            filed.append(_file(reviews, root, "hide_claim", _page_subject(gconn, cid, "Claims"),
                               {"to_status": "hidden"}, {"node_type": "claim"}))
    finally:
        gconn.close()
    for rid in filed:
        _approve(api, rid)
    _live_apply(api, set(filed), "setup apply 2")
    _log("corpus built")


def _file(reviews: Any, root: Path, rtype: str, subject: dict[str, Any], proposal: dict[str, Any],
          context: dict[str, Any] | None = None) -> str:
    from app.backend import write_lock
    with write_lock.hold(root / "reviews"):
        return reviews.create_review_item(root / "reviews", review_type=rtype, subject=subject,
                                          proposal=proposal, context=context)


def _page_subject(gconn: Any, nid: str, family: str) -> dict[str, Any]:
    from app.backend import graph
    node = graph.get_node(gconn, nid)
    if node is None:
        raise RuntimeError(f"no graph node {nid}")
    return {"node_id": nid, "page": f"{family}/{node['slug']}.md"}


def _claims_of(gconn: Any, sid: dict[int, str], docs: list[int], status: str = "active") -> list[str]:
    """Each document's first claim by id (whatever its status), which must have `status`; one per
    document, so targets never share a document. Setup hides exactly these, the queue unhides them."""
    from app.backend import graph
    out = []
    for d in docs:
        rows = gconn.execute(
            "SELECT DISTINCT src_id FROM edges WHERE dst_id = ? AND edge_type = 'derived_from'",
            (sid[d],)).fetchall()
        cids = sorted(r[0] for r in rows if r[0].startswith("clm_"))
        if not cids:
            raise RuntimeError(f"document {d} has no claim")
        got = (graph.get_node(gconn, cids[0]) or {}).get("status")
        if got != status:
            raise RuntimeError(f"claim {cids[0]} of document {d} is {got}, expected {status}")
        out.append(cids[0])
    return out


def _syn_id(root: Path, plan_: dict[str, Any], topic: int) -> str:
    from app.workers import synthesis
    return synthesis.synthesis_id(_item_id(plan_, topic))


def _synth_review(root: Path, plan_: dict[str, Any], topic: int) -> str | None:
    nid = _item_id(plan_, topic)
    for p in sorted((root / "reviews" / "pending").glob("*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        if rec.get("type") == "propose_synthesis" and rec["subject"].get("topic_node_id") == nid:
            return rec["review_id"]
    return None


def _approve_synthesis(api: Any, rid: str) -> None:
    rec = _check(api.get(f"/reviews/{rid}"), f"read {rid}")
    digest = ((rec.get("preview") or {}).get("synthesis_version") or {}).get("version_digest")
    _approve(api, rid, {"version_digest": digest})


def stage_queue(root: Path, plan_: dict[str, Any], profile: str) -> dict[str, Any]:
    """File and approve one declared queue; return its declaration (ids by type, fan-out)."""
    from fastapi.testclient import TestClient

    from app.backend import graph
    from app.backend import main as api_main
    from app.workers import items, reviews
    api = TestClient(api_main.app, base_url="http://127.0.0.1")
    sid = {doc["index"]: _sid_map(root)[doc["file"]] for doc in plan_["docs"]}
    t = plan_["targets"]
    ids: dict[str, list[str]] = {}
    fanout: list[dict[str, Any]] = []

    def add(rtype: str, rid: str) -> None:
        ids.setdefault(rtype, []).append(rid)

    gconn = graph.connect(root / "db" / "graph.sqlite")
    try:
        def degree(nid: str) -> dict[str, Any]:
            links = [r for r in gconn.execute(
                "SELECT src_id FROM edges WHERE dst_id = ? AND edge_type = 'mentions' "
                "AND status = 'active'", (nid,))]
            edges = gconn.execute("SELECT COUNT(*) FROM edges WHERE src_id = ? OR dst_id = ?",
                                  (nid, nid)).fetchone()[0]
            return {"node_id": nid, "documents": len({r[0] for r in links}), "edges": edges}

        def merge(pair: list[int]) -> None:
            s, a = _item_id(plan_, pair[0]), _item_id(plan_, pair[1])
            out = _check(api.post("/reviews/merge", json={"survivor": s, "absorbed": a}), "merge")
            add("merge_items", out["review_id"])
            fanout.append({"type": "merge_items", "survivor": degree(s), "absorbed": degree(a)})

        def retype(ti: int) -> None:
            nid = _item_id(plan_, ti)
            types = sorted(__import__("app.backend.taxonomy", fromlist=["x"]).ITEM_TYPES)
            cur = plan_["tags"][ti]["item_type"]
            to = types[(types.index(cur) + 1) % len(types)]
            rid = _file(reviews, root, "change_item_type", {"node_id": nid, "to_item_type": to},
                        {"to_item_type": to})
            _approve(api, rid)
            add("change_item_type", rid)
            fanout.append({"type": "change_item_type", "target": degree(nid)})

        def promote(ti: int) -> None:
            rid = _file(reviews, root, "promote_candidate_node", {"node_id": _item_id(plan_, ti)},
                        {"to_status": "active"})
            _approve(api, rid)
            add("promote_candidate_node", rid)

        def source(rtype: str, d: int, to: str) -> None:
            rid = _file(reviews, root, rtype, {"source_id": sid[d]}, {"to_status": to})
            _approve(api, rid)
            add(rtype, rid)

        if profile == "light":
            merge(t["merge_pairs"][0])
            retype(t["retype"][0])
            promote(t["promote"][0])
            source("hide_content", t["sources"]["hide"][0], "hidden")
            source("archive_source", t["sources"]["archive"][0], "archive_candidate")
        elif profile == "heavy":
            for topic in t["synth_topics"][:2]:
                rid = _synth_review(root, plan_, topic)
                _approve_synthesis(api, rid)
                add("propose_synthesis", rid)
            contra = sorted(p.stem for p in (root / "reviews" / "pending").glob("*.json")
                            if json.loads(p.read_text())["type"] == "resolve_contradiction")
            if len(contra) != CONTRA_PAIRS:
                raise RuntimeError(f"expected {CONTRA_PAIRS} contradictions, got {len(contra)}")
            for k, rid in enumerate(contra):
                rec = json.loads((root / "reviews" / "pending" / f"{rid}.json").read_text())
                _approve(api, rid, {"winner": rec["subject"]["claim_a"]} if k % 2 else None)
                add("resolve_contradiction", rid)
            for ti in t["deprecate_items"]:
                rid = _file(reviews, root, "deprecate_wiki_page",
                            _page_subject(gconn, _item_id(plan_, ti), "Items"),
                            {"to_status": "deprecated_candidate"}, {"node_type": "item"})
                _approve(api, rid)
                add("deprecate_wiki_page", rid)
            (c_dep,) = _claims_of(gconn, sid, t["claim_docs"][4:5])
            rid = _file(reviews, root, "deprecate_wiki_page", _page_subject(gconn, c_dep, "Claims"),
                        {"to_status": "deprecated_candidate"}, {"node_type": "claim"})
            _approve(api, rid)
            add("deprecate_wiki_page", rid)
            for rtype, tis, to in (("hide_semantic_page", t["hide_items"], "hidden"),
                                   ("unhide_semantic_page", t["unhide_items"], "active")):
                for ti in tis:
                    rid = _file(reviews, root, rtype,
                                _page_subject(gconn, _item_id(plan_, ti), "Items"),
                                {"to_status": to}, {"node_type": "item"})
                    _approve(api, rid)
                    add(rtype, rid)
            for rtype, docs, to in (("hide_claim", t["claim_docs"][0:2], "hidden"),
                                    ("unhide_claim", t["claim_docs"][2:4], "active")):
                for cid in _claims_of(gconn, sid, docs, "active" if to == "hidden" else "hidden"):
                    rid = _file(reviews, root, rtype, _page_subject(gconn, cid, "Claims"),
                                {"to_status": to}, {"node_type": "claim"})
                    _approve(api, rid)
                    add(rtype, rid)
            t_c, t_d = t["synth_topics"][2:4]
            for rtype, topic, to in (("hide_synthesis", t_c, "hidden"),
                                     ("unhide_synthesis", t_d, "active")):
                rid = _file(reviews, root, rtype,
                            _page_subject(gconn, _syn_id(root, plan_, topic), "Synthesis"),
                            {"to_status": to}, {"node_type": "synthesis"})
                _approve(api, rid)
                add(rtype, rid)
            for pair in t["duplicate_pairs"]:
                rid = _file(reviews, root, "mark_semantic_duplicate",
                            {"node_ids": sorted(_item_id(plan_, x) for x in pair)}, {})
                _approve(api, rid)
                add("mark_semantic_duplicate", rid)
            for pair in t["merge_pairs"]:
                merge(pair)
            for ti in t["retype"]:
                retype(ti)
            for ti in t["split"]:
                nid = _item_id(plan_, ti)
                docs = sorted({r[0] for r in gconn.execute(
                    "SELECT src_id FROM edges WHERE dst_id = ? AND edge_type = 'mentions' "
                    "AND status = 'active'", (nid,))})
                name = f"{plan_['tags'][ti]['name']} Spinoff"
                rid = _file(reviews, root, "split_item",
                            {"node_id": nid, "spinoff_node_id": items.node_id(name)},
                            {"spinoff_name": name, "spinoff_sources": docs[: len(docs) // 2],
                             "spinoff_aliases": []})
                _approve(api, rid)
                add("split_item", rid)
                fanout.append({"type": "split_item", "target": degree(nid),
                               "moved_documents": len(docs) // 2})
            for ti in t["promote"]:
                promote(ti)
            for d in t["sources"]["archive"]:
                source("archive_source", d, "archive_candidate")
            source("hide_content", t["sources"]["hide"][0], "hidden")
            source("unhide_content", t["sources"]["unhide"][0], "active")
        else:
            raise FixtureRefused(f"unknown profile {profile!r}")
    finally:
        gconn.close()
    every = {rid for v in ids.values() for rid in v}
    pv = _check(api.post("/reviews/apply/dry-run"), f"{profile} verification preview")
    if pv["status"] != "ok" or not pv.get("preview_token"):
        raise RuntimeError(f"{profile} verification preview not ok: {pv['status']} "
                           f"{pv.get('reason')} {pv.get('validators')} {pv.get('warnings')}")
    _require_applied(pv["outcomes"], every, f"{profile} verification preview")
    contention = []
    for c in t["contention"]:
        nid = _item_id(plan_, c["tag"])
        if not _unkept_suggestion(root, sid[c["doc"]], nid):
            raise RuntimeError(f"contention target {nid} on document {c['doc']} is not an unkept "
                               "suggestion")
        contention.append({"source_id": sid[c["doc"]], "node_id": nid})
    return {"profile": profile, "review_ids": {k: sorted(v) for k, v in sorted(ids.items())},
            "counts_by_type": {k: len(v) for k, v in sorted(ids.items())},
            "total": len(every), "fanout": fanout, "contention_targets": contention}


def _unkept_suggestion(root: Path, source_id: str, nid: str) -> bool:
    """True if `source_id` suggests `nid` and the link is still undecided (an active llm link to a
    candidate tag)."""
    from app.backend import graph
    gconn = graph.connect(root / "db" / "graph.sqlite")
    try:
        node = graph.get_node(gconn, nid)
        links = [x for x in graph.source_tag_links(gconn, source_id) if x["dst_id"] == nid]
        return (node is not None and node["status"] == "candidate" and len(links) == 1
                and links[0]["status"] == "active" and links[0]["asserted_by"] == "llm")
    finally:
        gconn.close()


# --- declaration ---------------------------------------------------------------------------------


CODE_FINGERPRINT_DIRS = ("app", "scripts", "templates")    # what a preview's sandbox executes


def content_fingerprint(root: Path, subdirs: tuple[str, ...] | None = None) -> str:
    """sha256 over every entry's relative path and content (the fixture content, not its path).

    The walk never follows a link: a symbolic link is hashed as a link (its target text), and any
    other non-regular file by its type, so neither can borrow content from outside `root`. Python
    caches (`__pycache__/`, `*.pyc`) are excluded; empty directories carry no content."""
    root = Path(root)
    entries: list[tuple[str, str]] = []
    bases = [root / d for d in subdirs] if subdirs is not None else [root]
    for base in bases:
        if not os.path.lexists(base):
            continue
        if base != root and (os.path.islink(base) or not base.is_dir()):
            entries.append((base.relative_to(root).as_posix(), str(base)))   # hashed by type below
            continue
        for dirpath, dirnames, filenames in os.walk(base):      # followlinks=False
            links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
            dirnames[:] = [d for d in dirnames if d != "__pycache__" and d not in links]
            for name in filenames + links:
                if name.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, name)
                entries.append((Path(full).relative_to(root).as_posix(), full))
    h = hashlib.sha256()
    for rel, full in sorted(set(entries)):
        mode = os.lstat(full).st_mode
        if stat.S_ISLNK(mode):
            h.update(b"L\0" + rel.encode() + b"\0" + os.readlink(full).encode() + b"\0")
        elif stat.S_ISREG(mode):
            h.update(b"F\0" + rel.encode() + b"\0")
            with open(full, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            h.update(b"\0")
        else:
            h.update(b"X\0" + rel.encode() + b"\0" + str(stat.S_IFMT(mode)).encode() + b"\0")
    return "sha256:" + h.hexdigest()


def code_fingerprint(root: Path) -> str:
    """The fingerprint of the code a vault's preview sandbox copies and runs (`app/`, `scripts/`,
    `templates/`); the runner compares the fixture's copy with the code executing it."""
    return content_fingerprint(root, CODE_FINGERPRINT_DIRS)


def _dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def counts(root: Path) -> dict[str, Any]:
    import sqlite3
    out: dict[str, Any] = {}
    con = sqlite3.connect(f"file:{root / 'db' / 'graph.sqlite'}?mode=ro", uri=True)
    try:
        out["graph_nodes"] = {f"{t}/{s}": n for t, s, n in con.execute(
            "SELECT node_type, status, COUNT(*) FROM nodes GROUP BY 1, 2 ORDER BY 1, 2")}
        out["graph_edges"] = {f"{t}/{s}": n for t, s, n in con.execute(
            "SELECT edge_type, status, COUNT(*) FROM edges GROUP BY 1, 2 ORDER BY 1, 2")}
    finally:
        con.close()
    out["wiki_pages"] = {d.name: sum(1 for _ in d.glob("*.md"))
                         for d in sorted((root / "wiki").iterdir()) if d.is_dir()}
    reviews: dict[str, dict[str, int]] = {}
    for state in ("pending", "approved", "rejected", "deferred"):
        for p in (root / "reviews" / state).glob("*.json"):
            rtype = json.loads(p.read_text(encoding="utf-8")).get("type", "?")
            reviews.setdefault(state, {}).setdefault(rtype, 0)
            reviews[state][rtype] += 1
    out["reviews"] = reviews
    raw = sorted(p.stat().st_size for p in (root / "raw").rglob("*")
                 if p.is_file() and "manifests" not in p.parts)
    out["raw"] = {"files": len(raw), "total_bytes": sum(raw),
                  "median_bytes": raw[len(raw) // 2] if raw else 0, "max_bytes": max(raw, default=0)}
    out["bytes"] = {d: _dir_bytes(root / d) for d in ("raw", "normalized", "wiki", "reviews", "db",
                                                      "policies")}
    return out


# --- orchestration -------------------------------------------------------------------------------


def _live_roots() -> list[Path]:
    roots = [ROOT.resolve()]
    try:
        from app.backend.config import get_settings
        roots.append(Path(get_settings().root).resolve())
    except Exception:  # noqa: BLE001 - an unreadable config only means fewer roots to refuse
        pass
    return roots


def check_destination(dest: Path) -> Path:
    dest = Path(dest).resolve()
    for live in _live_roots():
        if dest == live or live in dest.parents or dest in live.parents:
            raise FixtureRefused("the destination is, contains or lies inside the live vault")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise FixtureRefused("the destination exists and is not an empty directory")
    return dest


def _copy_code(root: Path) -> None:
    for rel in CODE_DIRS:
        shutil.copytree(ROOT / rel, root / rel, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _run_stage(root: Path, stage: str, plan_path: Path, *extra: str) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items()
           if not re.search(r"API_KEY|SECRET|TOKEN|PASSWORD", k, re.IGNORECASE)}
    env["KNOWLEDGE_SYSTEM_HOME"] = str(root)
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_stage", stage, str(root),
                           str(plan_path), *extra], env=env, cwd=str(root), stdout=subprocess.PIPE,
                          text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"stage {stage} failed (exit {proc.returncode})")
    return json.loads(proc.stdout or "{}")


def _validate(root: Path) -> None:
    env = dict(os.environ, KNOWLEDGE_SYSTEM_HOME=str(root))
    proc = subprocess.run([sys.executable, str(root / "scripts" / "validate_all.py"), str(root)],
                          env=env, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, check=False)
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-15:])
        raise RuntimeError(f"validate_all failed for {root.name}:\n{tail}")


def build(dest: Path, *, scale: int, seed: int, preset: str) -> dict[str, Any]:
    dest = check_destination(dest)
    plan_ = plan(scale, seed, preset)
    dest.mkdir(parents=True, exist_ok=True)
    plan_path = dest / "plan.json"
    plan_path.write_text(json.dumps(plan_, sort_keys=True), encoding="utf-8")
    t0 = time.monotonic()
    corpus = dest / "corpus"
    corpus.mkdir()
    _copy_code(corpus)
    _run_stage(corpus, "corpus", plan_path)
    _validate(corpus)
    profiles: dict[str, Any] = {}
    for profile in ("heavy", "light"):
        target = dest / profile
        _log(f"profile {profile}: copy corpus, file queue")
        shutil.copytree(corpus, target, symlinks=True)
        decl = _run_stage(target, "queue", plan_path, profile)
        _validate(target)
        decl["fingerprint"] = content_fingerprint(target)
        decl["counts"] = counts(target)
        profiles[profile] = decl
    declaration = {
        "schema_version": DECLARATION_SCHEMA, "generator_version": GENERATOR_VERSION,
        "label": "synthetic baseline" if preset == "baseline" else f"synthetic {preset}",
        "scale": scale, "seed": seed, "preset": preset, "parameters": plan_["parameters"],
        "distribution": {"raw": "lognormal(median=raw_median_bytes, sigma=raw_sigma) clipped to "
                                "[raw_min_bytes, raw_max_bytes]; every tail_every-th document "
                                "uniform in tail_bytes", "text_bytes_per_doc": plan_["parameters"]["text_bytes"]},
        "corpus_fingerprint": content_fingerprint(corpus), "corpus_counts": counts(corpus),
        "code_fingerprint": code_fingerprint(corpus),
        "profiles": profiles, "build_seconds": round(time.monotonic() - t0, 1),
        "note": "A declared synthetic workload: not a claim about average or worst-case use.",
    }
    (dest / "fixture.json").write_text(json.dumps(declaration, indent=2, sort_keys=True) + "\n",
                                       encoding="utf-8")
    return declaration


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[0] == "_stage":
        stage, root, plan_path = argv[1], Path(argv[2]), Path(argv[3])
        plan_ = json.loads(plan_path.read_text(encoding="utf-8"))
        if stage == "corpus":
            stage_corpus(root, plan_)
            print("{}")
        elif stage == "queue":
            print(json.dumps(stage_queue(root, plan_, argv[4])))
        else:
            return 2
        return 0
    if not argv or argv[0] != "build" or len(argv) < 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    opts = {"--scale": "600", "--seed": "1", "--preset": "baseline"}
    rest = argv[2:]
    while rest:
        if rest[0] not in opts or len(rest) < 2:
            print(f"unknown or incomplete option {rest[0]!r}", file=sys.stderr)
            return 2
        opts[rest[0]] = rest[1]
        rest = rest[2:]
    try:
        decl = build(Path(argv[1]), scale=int(opts["--scale"]), seed=int(opts["--seed"]),
                     preset=opts["--preset"])
    except FixtureRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"built": True, "label": decl["label"], "scale": decl["scale"],
                      "profiles": {k: v["fingerprint"] for k, v in decl["profiles"].items()}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
