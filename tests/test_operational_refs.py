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
