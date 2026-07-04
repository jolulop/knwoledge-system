"""Guard against operational-surface drift: every scripts/*.py referenced by a tracked hook,
skill, or the README must exist on disk.

This is the regression test for the Slice 4a miss where ``scripts/reindex_vector.py`` was deleted
but still invoked by the reindex hook and both vault skills (ADR-0032 §7 "change together").
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Same repo-root bootstrap the other app-importing test modules use, so the `_APPLY_TYPES` parity test
# can `import app.backend.main` even when this file runs in isolation (no conftest; CWD isn't on path
# under pytest).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tracked surfaces that instruct a human or the harness to run scripts.
OPERATIONAL_FILES = [
    ROOT / ".claude" / "hooks" / "reindex_changed_file.sh",
    ROOT / ".claude" / "skills" / "vault-ingest" / "SKILL.md",
    ROOT / ".claude" / "skills" / "vault-maintenance" / "SKILL.md",
    ROOT / "README.md",
    ROOT / "docs" / "UAT Guide.md",
]

SCRIPT_REF_RE = re.compile(r"scripts/([A-Za-z0-9_./-]+\.py)")


def test_operational_files_reference_only_existing_scripts():
    missing: list[str] = []
    checked = 0
    for surface in OPERATIONAL_FILES:
        if not surface.exists():
            continue
        for match in SCRIPT_REF_RE.findall(surface.read_text(encoding="utf-8")):
            checked += 1
            if not (ROOT / "scripts" / match).exists():
                missing.append(f"{surface.relative_to(ROOT)} references missing scripts/{match}")
    assert checked > 0, "expected to find script references to validate"
    assert not missing, "operational surfaces reference deleted scripts:\n" + "\n".join(missing)


def test_per_file_hook_does_not_run_vector_reindex():
    # ADR-0033 §5: vector re-embedding is GPU/latency-heavy and explicit-only — it must never be
    # wired into the per-file change hook (which would make editing depend on the embedding server).
    hook = ROOT / ".claude" / "hooks" / "reindex_changed_file.sh"
    assert "reindex_vector" not in hook.read_text(encoding="utf-8")


def test_env_example_documents_query_model():
    # The POST /query 503 tells operators to set QUERY_MODEL — it must be documented in .env.example
    # so that guidance isn't a dead end (ADR-0034 operational drift).
    assert "QUERY_MODEL=" in (ROOT / ".env.example").read_text(encoding="utf-8")


# Runbook/operational docs may WARN against a bare uvicorn launch, but must never RECOMMEND one: the
# blessed ``python -m app.backend`` entrypoint is the only launch routed through the assert_safe_bind
# loopback guard (ADR-0009). A direct ``uvicorn app.backend.main:app --host ...`` overrides the bind
# without re-checking the guard. This mirrors test_api.py's docker-compose guard for prose docs.
#
# Scope = README + every top-level operator doc under docs/ (ADRs under docs/adr/ are historical records,
# excluded and confirmed to carry no uvicorn text).
RUNBOOK_DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

_UVICORN_DIRECT_RE = re.compile(r"uvicorn\s+app\.backend\.main:app")
# Only explicit negations exempt a line as *warning* prose ("do not run ..."). Kept deliberately tight so
# the guard fails CLOSED: an ambiguous word like "bypass" can appear in a RECOMMENDATION ("bypass the
# guard with uvicorn ...") — the no-auth bind-bypass risk (ADR-0009) means we'd rather flag than exempt.
_UVICORN_WARNING_CUES = ("do not", "don't", "never")


def _recommends_direct_uvicorn(line: str) -> bool:
    # Flag ANY line presenting a runnable ``uvicorn app.backend.main:app`` bind unless it is warning
    # prose. Deliberately wrapper-agnostic: an earlier version only matched lines *starting* with
    # ``uvicorn``/``uv run uvicorn``, so ``env ... uvicorn``, ``APP_HOST=... uvicorn``, ``timeout 5
    # uvicorn``, ``cd ... && uvicorn``, and ``python -m uvicorn`` all slipped through. The blessed launch
    # is ``uv run python -m app.backend`` — never a direct uvicorn bind — so any non-warning app-path
    # invocation is an offender regardless of wrapper.
    if not _UVICORN_DIRECT_RE.search(line):
        return False
    return not any(cue in line.lower() for cue in _UVICORN_WARNING_CUES)


def test_runbook_docs_do_not_recommend_bare_uvicorn():
    offenders: list[str] = []
    checked = 0
    for doc in RUNBOOK_DOCS:
        if not doc.exists():
            continue
        checked += 1
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
            if _recommends_direct_uvicorn(line):
                offenders.append(f"{doc.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert checked > 0, "expected runbook docs to scan"
    assert not offenders, (
        "runbook docs recommend a bare uvicorn launch (use `uv run python -m app.backend`; ADR-0009):\n"
        + "\n".join(offenders))


def test_operations_doc_lists_every_apply_executor_type():
    # Parity guard: every review type with a deterministic executor (``_APPLY_TYPES``) must be documented
    # by its literal name in docs/Operations.md, so a newly executor-backed type can't silently stay
    # documented as record-only (the ADR-0043..0052 drift this test was added for). Scoped to
    # Operations.md only — it is the authoritative operator surface; other docs are not forced to mirror
    # a private implementation constant.
    from app.backend.main import _APPLY_TYPES

    text = (ROOT / "docs" / "Operations.md").read_text(encoding="utf-8")
    missing = sorted(t for t in _APPLY_TYPES if t not in text)
    assert not missing, (
        "docs/Operations.md omits executor-backed review types (add them to the 'Executor-backed "
        "review types' table):\n" + "\n".join(missing))


# Any of these existing on disk means "an in-repo CI runner exists". Kept in sync with the provider set
# checked when this guard was written; extend if a new CI provider is adopted.
CI_CONFIG_PATHS = [
    ROOT / ".github" / "workflows",
    ROOT / ".gitlab-ci.yml",
    ROOT / ".circleci",
    ROOT / "azure-pipelines.yml",
    ROOT / "Jenkinsfile",
    ROOT / ".drone.yml",
    ROOT / "bitbucket-pipelines.yml",
    ROOT / ".travis.yml",
]

# Operator-facing docs that must not claim an automated CI regression gate while no CI runner ships.
# ADRs/phase-design docs are historical records and are intentionally excluded — except Phase 7 Plan,
# which the operator reads for the maintenance story.
_NO_CI_CLAIM_DOCS = [
    ROOT / "docs" / "Operations.md",
    ROOT / "docs" / "Phase 7 Plan.md",
    ROOT / "README.md",
]

# Phrases that assert an automated CI gate. ``CI ... regression gate`` is a regex (bounded to one
# sentence via ``[^.\n]*``) so it catches "CI fake-adapter evals remain the regression gate" while still
# allowing benign phrasing like "fake-adapter CI fixture" or "not a CI gate" (no "regression gate"
# follows within the sentence).
_CI_GATE_CLAIM_RES = [
    re.compile(r"the CI suites", re.IGNORECASE),
    re.compile(r"gated by the CI", re.IGNORECASE),
    re.compile(r"\bCI\b[^.\n]*regression gate", re.IGNORECASE),
]


# --- UAT Guide drift guards (added after the ADR-0053 review round) -------------------------------
#
# The UAT Guide drifted twice in one slice: its curl targets referenced the pre-implementation API
# shape, and its "clean embedding environment" ritual enumerated the pre-ADR-0053 EMBEDDING_* vars
# (missing the six new keys). These guards make both drifts structural test failures.

_UAT_GUIDE = ROOT / "docs" / "UAT Guide.md"
# curl targets appear as "$APP/<path>" (after `export APP=http://127.0.0.1:18000`) or as literal
# loopback URLs. Query strings stop the capture ('?' is outside the class); mermaid labels and prose
# never carry either prefix, so only runnable targets are collected.
_UAT_ROUTE_RES = [
    re.compile(r"\$APP(/[A-Za-z0-9_\-/.{}<>]*)"),
    re.compile(r"127\.0\.0\.1:18000(/[A-Za-z0-9_\-/.{}<>]*)"),
]


def _doc_path_matches_route(doc_path: str, route_path: str) -> bool:
    doc_segs = doc_path.strip("/").split("/")
    route_segs = route_path.strip("/").split("/")
    if len(doc_segs) != len(route_segs):
        return False
    for doc_seg, route_seg in zip(doc_segs, route_segs):
        if route_seg.startswith("{") and route_seg.endswith("}"):
            continue  # route param matches any doc segment, incl. `<review_id>` placeholders
        if doc_seg != route_seg:
            return False
    return True


_CURL_METHOD_RE = re.compile(r"-X\s+([A-Z]+)")


def _uat_http_targets(text: str) -> set[tuple[str, str]]:
    # (method, path) per target. Backslash-continued curl commands are joined first so `-X POST`
    # and the URL always land on one logical line. Method: explicit `-X <M>` wins, `--get` and
    # plain curls (and bare browser URLs) are GET.
    joined = re.sub(r"\\\n\s*", " ", text)
    targets: set[tuple[str, str]] = set()
    for line in joined.splitlines():
        paths = {m for rx in _UAT_ROUTE_RES for m in rx.findall(line) if m != "/"}
        if not paths:
            continue
        method_match = _CURL_METHOD_RE.search(line) if "curl" in line else None
        method = method_match.group(1) if method_match else "GET"
        targets.update((method, p) for p in paths)
    return targets


def test_uat_guide_curl_targets_are_real_routes():
    # Every runnable HTTP target in the UAT Guide must resolve to a registered FastAPI route WITH a
    # matching method, so a renamed/removed endpoint or a GET/POST drift can't silently strand the
    # operator checklist.
    from app.backend.main import app as fastapi_app

    routes = [
        (getattr(r, "path", ""), getattr(r, "methods", None) or set())
        for r in fastapi_app.routes
    ]
    targets = _uat_http_targets(_UAT_GUIDE.read_text(encoding="utf-8"))
    assert targets, "expected the UAT Guide to reference API routes"
    missing = [
        f"{method} {path}"
        for method, path in sorted(targets)
        if not any(
            _doc_path_matches_route(path, route_path) and method in route_methods
            for route_path, route_methods in routes
        )
    ]
    assert not missing, (
        "docs/UAT Guide.md curls method+route pairs the app does not serve:\n" + "\n".join(missing))


# Operator docs must never instruct printing environment VALUES: the operator's env can hold secrets
# (ANTHROPIC_API_KEY, EMBEDDING_API_KEY — config.py reads them) and AGENTS.md "Do not expose secrets" /
# policies/security.yaml forbid exposure. This is the regression guard for the UAT-Guide
# `env | sort | grep '^EMBEDDING_'` leak (printed EMBEDDING_API_KEY's value). Fails CLOSED: any
# `env`/`printenv` dump or `echo $..KEY/TOKEN/SECRET` is an offender unless the line carries a
# value-stripping idiom from the tight allowlist below.
_SECURITY_DOCS = [
    ROOT / "README.md",
    *sorted((ROOT / "docs").glob("*.md")),  # top-level operator docs; docs/adr/ = historical records
    *sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md")),
]
_ENV_DUMP_RE = re.compile(r"(?:^|[\s|&$(])(?:env|printenv)\s*\|")
_PRINTENV_ARG_RE = re.compile(r"printenv\s+[A-Za-z_]")
_ECHO_SECRET_RE = re.compile(r'echo\s+"?\$\{?[A-Za-z_]*(?:KEY|TOKEN|SECRET)', re.IGNORECASE)
# Idioms that provably strip values before printing: name-only extraction (`grep -o '^NAME'`,
# `cut -d= -f1`, `awk -F=` on $1) and the pytest preflight's sed loop that emits `-u NAME` flags.
_VALUE_STRIP_CUES = ("grep -o '^", "cut -d= -f1", "awk -F=", "/-u \\1/")


def test_operator_docs_never_print_env_values():
    offenders: list[str] = []
    checked = 0
    for doc in _SECURITY_DOCS:
        if not doc.exists():
            continue
        checked += 1
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
            dump = _ENV_DUMP_RE.search(line) or _PRINTENV_ARG_RE.search(line)
            if dump and not any(cue in line for cue in _VALUE_STRIP_CUES):
                offenders.append(f"{doc.relative_to(ROOT)}:{lineno}: {line.strip()}")
            elif _ECHO_SECRET_RE.search(line):
                offenders.append(f"{doc.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert checked > 0, "expected operator docs to scan"
    assert not offenders, (
        "operator docs print environment values (secrets like *_KEY may be set; list names only — "
        "AGENTS.md / policies/security.yaml):\n" + "\n".join(offenders))


_CFG_KEY_RE = re.compile(r'cfg\(\s*"([A-Z0-9_]+)"')


def test_embedding_env_keys_all_carry_the_embedding_prefix():
    # The UAT Guide's preflight strips embedding config by PREFIX (`env -u` over every EMBEDDING_*
    # shell var) instead of enumerating keys, so it cannot go stale the way the pre-ADR-0053 list
    # did. That only holds while every embedding-related env key config.py reads actually carries
    # the EMBEDDING_ prefix — this guard pins that invariant.
    src = (ROOT / "app" / "backend" / "config.py").read_text(encoding="utf-8")
    keys = set(_CFG_KEY_RE.findall(src))
    assert "EMBEDDING_PROVIDER" in keys, "config.py key scan failed to find the embedding block"
    offenders = sorted(k for k in keys if "EMBEDDING" in k and not k.startswith("EMBEDDING_"))
    assert not offenders, (
        "embedding env keys outside the EMBEDDING_ prefix break the UAT Guide's prefix-strip "
        "preflight:\n" + "\n".join(offenders))


def test_stripping_embedding_prefix_yields_unconfigured_embedding(monkeypatch, tmp_path):
    # Functional half of the same contract: with every EMBEDDING_* shell var stripped and no .env
    # at the root (the fresh-clone posture the UAT Guide prescribes), the embedding layer must
    # resolve to cleanly-unconfigured — no client, no staleness identity.
    import os

    for key in list(os.environ):
        if key.startswith("EMBEDDING_"):
            monkeypatch.delenv(key)
    from app.backend.config import get_settings
    from app.backend.embeddings import client_from_settings, resolve_model_ref

    settings = get_settings(tmp_path)
    assert client_from_settings(settings) is None
    assert resolve_model_ref(settings) is None


def test_operator_docs_dont_claim_ci_gate_when_no_ci_runner():
    # "No in-repo CI runner yet" is the accepted posture (Build Spec §16): the structural pytest suites
    # are the *local* gate run by the working rhythm. While that stays true (no CI config on disk), no
    # operator-facing doc may describe an automated CI regression gate. If a CI runner is later added,
    # the claim becomes accurate and this guard steps aside.
    if any(p.exists() for p in CI_CONFIG_PATHS):
        return
    offenders: list[str] = []
    for doc in _NO_CI_CLAIM_DOCS:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        for rx in _CI_GATE_CLAIM_RES:
            offenders.extend(
                f"{doc.relative_to(ROOT)}: {m.group(0)!r}" for m in rx.finditer(text))
    assert not offenders, (
        "operator docs claim an automated CI gate but no in-repo CI runner exists "
        "(reword to the local pytest gate; see Build Spec §16):\n" + "\n".join(offenders))
