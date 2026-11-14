"""ADR-0074 decision 1, structural guard (static half; the runtime half is `tests/write_guard.py`).

1. **Deny by default.** Every raw file-write (or removal — review round 2 of slice 3b) primitive in `app/` and `scripts/` — called *or* merely
   referenced, under any import alias — must be in `ALLOWED` with a reason, or the test fails. So a
   new authority writer cannot bypass `app.backend.durable_write` through a wrapper or an alias: the
   wrapper itself contains a primitive. Stale entries fail too, so the list stays exact.
2. **Known writer entry points** of each authority family must call `durable_write`.
3. The runtime guard really catches an unguarded write from project code (a meta-test).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests import write_guard

ROOT = Path(__file__).resolve().parents[1]

_ATTR_PRIMITIVES = {"write_text", "write_bytes", "touch", "symlink_to", "hardlink_to", "link_to",
                    "mkdir", "unlink", "rmdir"}
_MODULE_PRIMITIVES = {
    "os": {"replace", "rename", "renames", "link", "symlink", "open", "write", "truncate",
           "ftruncate", "fdopen", "mkdir", "makedirs", "remove", "unlink", "rmdir", "removedirs"},
    "shutil": {"copy", "copy2", "copyfile", "copytree", "move", "copyfileobj", "rmtree"},
    "json": {"dump"},
    "tempfile": {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile", "mkstemp",
                 "mkdtemp", "TemporaryDirectory"},
}

# (repo-relative file, enclosing function, primitive) -> why a raw primitive is legitimate there.
_HELPER = "the durable-write helper itself"
_LOCK = "a lock file (never authority)"
_SANDBOX = "the throwaway Apply-preview sandbox (ADR-0040), not live authority"
_PROJECTION = "a projection, rebuildable by definition (ADR-0068 §1)"
_NORMALIZED = "normalized/ derived state; extraction install is ADR-0074 slice 5"
_EVAL = "an evaluation report / scratch vault outside the live vault"
_INDEX = "an index / model-cache directory, rebuildable (not an authority directory)"
ALLOWED: dict[tuple[str, str, str], str] = {
    ("app/backend/durable_write.py", "_fsync_path", "os.open"): _HELPER,
    ("app/backend/durable_write.py", "make_dirs", "os.mkdir"): _HELPER,
    ("app/backend/durable_write.py", "_write_temp", "os.open"): _HELPER,
    ("app/backend/durable_write.py", "_write_temp", "os.fdopen"): _HELPER,
    ("app/backend/durable_write.py", "_commit", "os.link"): _HELPER,
    ("app/backend/durable_write.py", "_commit", "os.replace"): _HELPER,
    ("app/backend/durable_write.py", "make_dirs", "os.rmdir"): _HELPER,
    ("app/backend/durable_write.py", "_commit", ".unlink"): _HELPER,
    ("app/backend/durable_write.py", "_commit", "os.unlink"): _HELPER,
    ("app/backend/durable_write.py", "_write", ".unlink"): _HELPER,
    ("app/backend/durable_write.py", "remove", "os.unlink"): _HELPER,
    ("app/backend/durable_write.py", "remove_dir", "os.rmdir"): _HELPER,
    ("app/backend/apply_sandbox.py", "cleanup_sandbox", "shutil.rmtree"): _SANDBOX,
    ("app/backend/vector_index.py", "_swap_dir", "shutil.rmtree"): _PROJECTION + " (vector index)",
    ("app/backend/vector_index.py", "_build_full", "shutil.rmtree"): _PROJECTION + " (vector index)",
    ("app/workers/authors.py", "publish", ".unlink"):
        _PROJECTION + " (wiki/Authors pages, generated from manifests)",
    ("app/workers/extract.py", "_stage_outputs", "shutil.rmtree"): _NORMALIZED,
    ("app/workers/extract.py", "_install_outputs", "shutil.rmtree"): _NORMALIZED,
    ("app/workers/extract.py", "_extract_one", "shutil.rmtree"): _NORMALIZED,
    ("app/workers/extract.py", "extract_sources", "shutil.rmtree"): _NORMALIZED,
    ("scripts/eval_contradictions.py", "evaluate", "shutil.rmtree"): _EVAL,
    ("app/backend/authority_read.py", "read_file", "os.open"):
        "a read-only open (O_RDONLY | O_NONBLOCK) of the shared typed authority reader, ADR-0074 "
        "decisions 2 and 4",
    ("app/backend/run_guard.py", "guard", "open(a)"): _LOCK,
    ("app/backend/vault_lock.py", "acquire_shared", "open(a)"): _LOCK,
    ("app/backend/vault_lock.py", "exclusive", "open(a)"): _LOCK,
    ("app/backend/write_lock.py", "hold", "open(a)"): _LOCK,
    ("app/backend/apply_gate.py", "carry_env", ".write_text"): _SANDBOX,
    ("app/backend/apply_sandbox.py", "copy_code_and_raw", "shutil.copy2"): _SANDBOX,
    ("app/backend/apply_sandbox.py", "copy_code_and_raw", "shutil.copytree"): _SANDBOX,
    ("app/backend/apply_sandbox.py", "copy_state", "shutil.copy2"): _SANDBOX,
    ("app/backend/apply_sandbox.py", "copy_state", "shutil.copytree"): _SANDBOX,
    ("app/backend/apply_sandbox.py", "copy_state", "tempfile.mkdtemp"): _SANDBOX,
    ("app/backend/apply_sandbox.py", "copy_state", ".mkdir"): _SANDBOX,
    ("app/backend/apply_sandbox.py", "copy_code_and_raw", ".mkdir"): _SANDBOX,
    ("app/backend/embeddings.py", "_load_model", ".mkdir"): _INDEX,
    ("app/backend/keyword_index.py", "connect", ".mkdir"): _INDEX,
    ("app/backend/vector_index.py", "_build_full", ".mkdir"): _PROJECTION + " (vector index)",
    ("app/backend/main.py", "run_eval_endpoint", ".mkdir"): _EVAL,
    ("app/workers/claims.py", "_write_source_artifact", ".mkdir"): _NORMALIZED,
    ("app/workers/enrich.py", "enrich_sources", ".mkdir"): _NORMALIZED,
    ("app/workers/extract.py", "_stage_outputs", ".mkdir"): _NORMALIZED,
    ("app/workers/extract.py", "extract_sources", ".mkdir"): _NORMALIZED,
    ("app/workers/items.py", "_write_artifact", ".mkdir"): _NORMALIZED,
    ("app/workers/synthesis.py", "generate_syntheses", ".mkdir"): _NORMALIZED,
    ("scripts/backup.py", "_create_backup_report", ".mkdir"):
        "the backup's own output directory (outside the vault's authority directories)",
    ("scripts/eval_contradictions.py", "materialize", ".mkdir"): _EVAL,
    ("scripts/eval_contradictions.py", "write_result", ".mkdir"): _EVAL,
    ("scripts/eval_retrieval.py", "_build_corpus_vault", ".mkdir"): _EVAL,
    ("scripts/eval_retrieval.py", "main", ".mkdir"): _EVAL,
    ("app/backend/main.py", "_append_wiki_log", "open(a)"): _PROJECTION + " (wiki/log.md)",
    ("app/backend/main.py", "run_eval_endpoint", ".write_text"): _EVAL,
    ("app/backend/vector_index.py", "_save_meta", ".write_text"): _PROJECTION + " (vector index)",
    ("app/backend/vector_index.py", "_build_full", ".touch"): _PROJECTION + " (vector index)",
    ("app/backend/vector_index.py", "_incremental", ".touch"): _PROJECTION + " (vector index)",
    ("app/backend/vector_index.py", "_swap_dir", "os.replace"): _PROJECTION + " (vector index)",
    ("app/workers/claims.py", "_write_source_artifact", ".write_text"): _NORMALIZED,
    ("app/workers/enrich.py", "enrich_sources", ".write_text"): _NORMALIZED,
    ("app/workers/items.py", "_write_artifact", ".write_text"): _NORMALIZED,
    ("app/workers/synthesis.py", "generate_syntheses", ".write_text"): _NORMALIZED,
    ("app/workers/extract.py", "_stage_outputs", ".write_text"): _NORMALIZED,
    ("app/workers/extract.py", "_install_outputs", "os.replace"): _NORMALIZED,
    ("app/workers/extract.py", "_write_log", ".write_text"): _NORMALIZED,
    ("app/workers/lint.py", "_append_log", "open(a)"): _PROJECTION + " (wiki/log.md)",
    ("app/workers/retention.py", "_append_log", "open(a)"): _PROJECTION + " (wiki/log.md)",
    ("app/workers/wiki.py", "_append_log", ".write_text"): _PROJECTION + " (wiki/log.md)",
    ("app/workers/wiki.py", "_append_log", "open(a)"): _PROJECTION + " (wiki/log.md)",
    ("app/workers/wiki.py", "_render_one", ".write_text"): _PROJECTION + " (Source page)",
    ("scripts/rebuild_index.py", "_build", ".write_text"): _PROJECTION + " (wiki/index.md)",
    ("scripts/backup.py", "_create_backup_report", "tempfile.TemporaryDirectory"):
        "scratch space for the SQLite snapshots of a backup",
    ("scripts/eval_retrieval.py", "_build_corpus_vault", "shutil.copy"): _EVAL,
    ("scripts/eval_retrieval.py", "_open_graph", "tempfile.TemporaryDirectory"): _EVAL,
    ("scripts/eval_retrieval.py", "main", "tempfile.TemporaryDirectory"): _EVAL,
    ("scripts/eval_retrieval.py", "main", ".write_text"): _EVAL,
    ("scripts/eval_contradictions.py", "materialize", "shutil.copytree"): _EVAL,
    ("scripts/eval_contradictions.py", "materialize", ".write_text"): _EVAL,
    ("scripts/eval_contradictions.py", "evaluate", "tempfile.mkdtemp"): _EVAL,
    ("scripts/eval_contradictions.py", "evaluate", "shutil.copytree"): _EVAL,
    ("scripts/eval_contradictions.py", "write_result", ".write_text"): _EVAL,
}


def _sources():
    for base in ("app", "scripts"):
        yield from sorted((ROOT / base).rglob("*.py"))


def _write_mode(call: ast.Call, *, builtin: bool) -> str | None:
    """The mode of an `open` call when it can write, '?' when unknown, None for a read."""
    mode: ast.AST | None = None
    if builtin and len(call.args) >= 2:
        mode = call.args[1]
    elif not builtin and call.args and isinstance(call.args[0], ast.Constant):
        # `Path.open(mode)`; a non-constant first argument is a name (`ZipFile.open(name)`,
        # `opener.open(request)`) — reads in this codebase. The runtime hook covers what this misses.
        mode = call.args[0]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return None                                   # default "r"
    if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
        return "?"
    return mode.value if any(c in mode.value for c in "wax+") else None


class _Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found: list[tuple[str, int, str]] = []    # (function, line, primitive)
        self.stack: list[str] = []
        self.modules: dict[str, str] = {}             # local alias -> os/shutil/json/tempfile
        self.names: dict[str, str] = {}               # local name -> "os.replace" etc.
        self.helper: set[str] = set()                 # local names of app.backend.durable_write

    def _here(self) -> str:
        return self.stack[-1] if self.stack else "<module>"

    def _add(self, node: ast.AST, prim: str) -> None:
        self.found.append((self._here(), node.lineno, prim))

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node):
        for a in node.names:
            if a.name in _MODULE_PRIMITIVES:
                self.modules[a.asname or a.name] = a.name
            elif a.name == "app.backend.durable_write" and a.asname:
                self.helper.add(a.asname)

    def visit_ImportFrom(self, node):
        if node.module == "app.backend":
            self.helper.update(a.asname or a.name for a in node.names if a.name == "durable_write")
        if node.module in _MODULE_PRIMITIVES:
            for a in node.names:
                if a.name in _MODULE_PRIMITIVES[node.module] or a.name == "*":
                    self._add(node, f"from {node.module} import {a.name}")
                    self.names[a.asname or a.name] = f"{node.module}.{a.name}"

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name) and f.id == "open":
            mode = _write_mode(node, builtin=True)
            if mode:
                self._add(node, f"open({mode})")
            self._visit_args(node)
            return
        if isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2 \
                and isinstance(node.args[1], ast.Constant) \
                and any(node.args[1].value in s for s in _MODULE_PRIMITIVES.values()):
            self._add(node, f"getattr(..., {node.args[1].value!r})")
        if isinstance(f, ast.Attribute):
            base = f.value
            if isinstance(base, ast.Name) and base.id in self.helper:
                self._visit_args(node)                        # the helper: never a primitive
                return
            if isinstance(base, ast.Name) and base.id in self.modules:
                mod = self.modules[base.id]
                if f.attr in _MODULE_PRIMITIVES[mod]:
                    if mod == "os" and f.attr == "open":
                        self._add(node, "os.open")
                    else:
                        self._add(node, f"{mod}.{f.attr}")
                    self._visit_args(node)
                    return
            elif f.attr == "open":
                mode = _write_mode(node, builtin=False)
                if mode:
                    self._add(node, f".open({mode})")
            elif f.attr in ("replace", "rename") and len(node.args) == 1 and not node.keywords:
                self._add(node, f".{f.attr}(target)")         # Path.replace / Path.rename
            elif f.attr in _ATTR_PRIMITIVES:
                self._add(node, f".{f.attr}")
                self._visit_args(node)
                return
        self.generic_visit(node)

    def _visit_args(self, node: ast.Call) -> None:
        for child in (*node.args, *node.keywords):
            self.visit(child)
        if isinstance(node.func, ast.Attribute):
            self.visit(node.func.value)

    def visit_Attribute(self, node):
        # A primitive referenced without being called (`w = path.write_text`, `fn = os.replace`).
        base = node.value
        if isinstance(base, ast.Name) and base.id in self.helper:
            return
        if isinstance(base, ast.Name) and base.id in self.modules \
                and node.attr in _MODULE_PRIMITIVES[self.modules[base.id]]:
            self._add(node, f"{self.modules[base.id]}.{node.attr} (reference)")
        elif node.attr in _ATTR_PRIMITIVES:
            self._add(node, f".{node.attr} (reference)")
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in self.names and isinstance(node.ctx, ast.Load):
            self._add(node, f"{self.names[node.id]} (imported name)")


def _scan() -> dict[tuple[str, str, str], list[int]]:
    out: dict[tuple[str, str, str], list[int]] = {}
    for py in _sources():
        finder = _Finder()
        finder.visit(ast.parse(py.read_text(encoding="utf-8"), filename=str(py)))
        rel = str(py.relative_to(ROOT))
        for func, line, prim in finder.found:
            out.setdefault((rel, func, prim), []).append(line)
    return out


def _normalize(prim: str) -> str:
    """Fold an open mode / Path-method spelling into the allowlist's key."""
    if prim.startswith(".open(") or prim.startswith("open("):
        return prim.lstrip(".")
    if prim == ".write_text" or prim.startswith(".write_text"):
        return ".write_text"
    return prim


def test_every_raw_write_primitive_is_the_helper_or_allowlisted_with_a_reason():
    found = _scan()
    keys = {(f, fn, _normalize(p)): lines for (f, fn, p), lines in found.items()}
    unexpected = sorted(f"{f}:{lines[0]} in {fn}: {p}" for (f, fn, p), lines in keys.items()
                        if (f, fn, p) not in ALLOWED)
    assert not unexpected, (
        "raw file-write primitive(s) outside app.backend.durable_write — route authority writes "
        "through the helper, or (for a non-authority file) add an ALLOWED entry with its reason:\n"
        + "\n".join(unexpected))
    stale = sorted(f"{k}" for k in ALLOWED if k not in keys)
    assert not stale, "ALLOWED entries that no longer match any code (remove them):\n" + "\n".join(stale)
    assert all(reason.strip() for reason in ALLOWED.values())


def test_the_scanner_sees_aliases_wrappers_and_references(tmp_path):
    src = (
        "import os as _os\nimport shutil as sh\nfrom json import dump as jd\n"
        "from pathlib import Path\n"
        "def a(p):\n    _os.replace('x', p)\n"
        "def b(p, f):\n    jd({}, f)\n"
        "def c(p):\n    w = Path(p).write_text\n    w('x')\n"
        "def d(p):\n    sh.copyfile('x', p)\n"
        "def e(p, m):\n    open(p, m)\n"
        "def f(p):\n    Path('t').replace(p)\n"
        "def g(p):\n    return getattr(_os, 'rename')\n"
        "def h(p):\n    with Path(p).open('a') as fh: pass\n"
        "def ok(p):\n    'a b'.replace('a', 'b'); open(p); Path(p).open()\n"
    )
    finder = _Finder()
    finder.visit(ast.parse(src))
    by_func = {}
    for func, _line, prim in finder.found:
        by_func.setdefault(func, set()).add(prim)
    assert by_func["a"] == {"os.replace"}
    assert "json.dump (imported name)" in by_func["b"]
    assert ".write_text (reference)" in by_func["c"]
    assert by_func["d"] == {"shutil.copyfile"}
    assert by_func["e"] == {"open(?)"}
    assert by_func["f"] == {".replace(target)"}
    assert by_func["g"] == {"getattr(..., 'rename')"}
    assert by_func["h"] == {".open(a)"}
    assert "ok" not in by_func
    assert any(p.startswith("from json import dump") for _f, _l, p in finder.found)


# --- known writer entry points of every authority family call the helper -------------------------

ENTRY_POINTS = {
    "manifest": [("app/backend/manifests.py", "save_manifest")],
    "page": [("app/workers/items.py", "_recompose_node"),
             ("app/workers/items.py", "recompose_semantic_node_page"),
             ("app/workers/item_pages.py", "write_page"),
             ("app/workers/promote.py", "activate_node"),
             ("app/workers/claims.py", "recompose_claim"),
             ("app/workers/synthesis.py", "_render_page")],
    "reviews": [("app/workers/reviews.py", "create_review_item"),
                ("app/workers/reviews.py", "resolve_review_item"),
                ("app/workers/reviews.py", "reopen_review_item"),
                ("app/workers/reviews.py", "defer_review_item"),
                ("app/workers/reviews.py", "withdraw_review_item"),
                ("app/workers/tag_links.py", "_save_ledger"),
                ("app/workers/tag_links.py", "_audit"),
                ("app/workers/tag_links.py", "_write_resolving"),
                ("app/workers/tag_links.py", "delete_tag"),
                ("app/workers/tag_history.py", "_write_tracking"),
                ("app/workers/identity_journal.py", "_write"),
                ("app/workers/merges.py", "_write_audit"),
                ("app/workers/splits.py", "_write_audit"),
                ("app/workers/retypes.py", "_write_audit"),
                ("app/workers/retypes.py", "_write_verification"),
                ("app/workers/promote.py", "_write_executor_record"),
                ("app/workers/human_add.py", "add_candidate"),
                ("app/workers/human_merge.py", "file_merge")],
    "policies": [("app/workers/tag_links.py", "_write_blocklist")],
    "db_json": [("app/backend/publication_queue.py", "save"),
                ("app/backend/publication_queue.py", "preserve_damaged"),
                ("app/backend/producer_journal.py", "save"),
                ("app/backend/producer_journal.py", "preserve_damaged"),
                ("app/backend/revision.py", "_save"),
                ("app/backend/revision.py", "current"),
                ("app/backend/apply_authorization.py", "_save"),
                ("app/backend/apply_authorization.py", "load"),
                ("app/workers/contradiction_check.py", "_save"),
                ("app/workers/tag_projection.py", "save_published")],
}


def _calls_helper(rel: str, func: str) -> bool:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            return any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id == "durable_write" for n in ast.walk(node))
    raise AssertionError(f"{rel}: no function {func} (update ENTRY_POINTS)")


@pytest.mark.parametrize("fam", sorted(ENTRY_POINTS))
def test_known_authority_writers_call_the_helper(fam):
    missing = [f"{rel}:{func}" for rel, func in ENTRY_POINTS[fam] if not _calls_helper(rel, func)]
    assert not missing, f"{fam} writers not using app.backend.durable_write: {missing}"


# --- the runtime guard catches an unguarded write from project code -------------------------------


def test_the_runtime_guard_flags_an_unguarded_authority_write_from_project_code(tmp_path):
    target = tmp_path / "raw" / "manifests" / "src_0123456789abcdef.json"
    target.parent.mkdir(parents=True)
    fake = compile("def rogue(p):\n    open(p, 'w').write('{}')\n",
                   str(ROOT / "app" / "workers" / "rogue_writer.py"), "exec")
    ns: dict = {}
    exec(fake, ns)
    write_guard.take_violations()
    ns["rogue"](target)
    found = write_guard.take_violations()
    assert len(found) == 1 and "manifest" in found[0] and "rogue_writer.py:rogue" in found[0]
    # Through the helper, and from test code, the same write is fine.
    from app.backend import durable_write
    durable_write.write_text(target, "{}")
    target.write_text("{}")
    assert write_guard.take_violations() == []


@pytest.mark.parametrize("path,fam", [
    ("v/raw/manifests/src_x.json", "manifest"), ("v/raw/manifests/quarantine/x.json", None),
    ("v/raw/inbox/a.json", None), ("v/wiki/Items/a.md", "page"), ("v/wiki/Sources/a.md", None),
    ("v/wiki/Claims/clm_x.md", "page"), ("v/wiki/Synthesis/syn_x.md", "page"),
    ("v/reviews/pending/rev.json", "reviews"), ("v/reviews/tag_links/.lock", None),
    ("v/reviews/audit_log/x.json", "reviews"), ("v/policies/blocked_tags.txt", "policies"),
    ("v/db/revision.json", "db_json"), ("v/db/.vault.lock", None), ("v/db/graph.sqlite", None),
    ("v/normalized/markdown/src_x.md", None), ("v/wiki/index.md", None),
])
def test_family_classification(path, fam):
    assert write_guard.family(path) == fam


def test_the_runtime_guard_flags_an_unguarded_authority_directory_creation(tmp_path):
    """Review round 1 (P1): an authority directory created outside `make_dirs` is a violation, so a
    caller cannot leave a new ancestor entry unsynced; through the helper (or a non-authority
    directory) is fine."""
    fake = compile("from pathlib import Path\n"
                   "def rogue(p):\n    Path(p).mkdir(parents=True, exist_ok=True)\n",
                   str(ROOT / "app" / "workers" / "rogue_writer.py"), "exec")
    ns: dict = {}
    exec(fake, ns)
    write_guard.take_violations()
    ns["rogue"](tmp_path / "reviews" / "audit_log")
    found = write_guard.take_violations()
    assert all("reviews directory" in f for f in found)
    assert {f.split(" by ")[0].rsplit("/", 1)[-1] for f in found} == {"reviews", "audit_log"}
    ns["rogue"](tmp_path / "normalized" / "markdown")                         # not authority
    assert write_guard.take_violations() == []
    from app.backend import durable_write
    write_guard.helper_dirs.clear()
    durable_write.make_dirs(tmp_path / "wiki" / "Items")
    assert write_guard.take_violations() == []
    assert [f for f, _p in write_guard.helper_dirs] == ["page", "page"]


def test_the_runtime_guard_flags_an_unguarded_authority_removal(tmp_path):
    """Slice 3b review round 2: an authority file removed outside `durable_write.remove` (whose
    directory sync makes the removal durable) is a violation; through the helper it is fine."""
    target = tmp_path / "reviews" / "approved" / "rev_x.json"
    target.parent.mkdir(parents=True)
    fake = compile("import os, shutil\nfrom pathlib import Path\n"
                   "def rogue(p):\n    Path(p).unlink()\n"
                   "def rogue_tree(p):\n    shutil.rmtree(p)\n",
                   str(ROOT / "app" / "workers" / "rogue_writer.py"), "exec")
    ns: dict = {}
    exec(fake, ns)
    write_guard.take_violations()
    target.write_text("{}")
    ns["rogue"](target)
    found = write_guard.take_violations()
    assert len(found) == 1 and found[0].startswith("os.remove of reviews") and "rogue" in found[0]
    (tmp_path / "policies").mkdir()
    ns["rogue_tree"](tmp_path / "policies")
    # The tree itself, and its top-level rmdir (by absolute path), are both flagged.
    assert [f.split(" ")[0] for f in write_guard.take_violations()] == ["shutil.rmtree", "os.rmdir"]
    scratch = tmp_path / "normalized" / "x.md"                  # not authority
    scratch.parent.mkdir()
    scratch.write_text("x")
    ns["rogue"](scratch)
    assert write_guard.take_violations() == []
    from app.backend import durable_write
    target.write_text("{}")
    durable_write.remove(target)
    assert write_guard.take_violations() == []


@pytest.mark.parametrize("path,fam", [
    ("v/raw", "manifest"), ("v/raw/manifests", "manifest"), ("v/raw/inbox", None),
    ("v/wiki", "page"), ("v/wiki/Items", "page"), ("v/wiki/Claims", "page"),
    ("v/wiki/Synthesis", "page"), ("v/wiki/Sources", None), ("v/reviews", "reviews"),
    ("v/reviews/audit_log", "reviews"), ("v/reviews/tag_links/x", "reviews"),
    ("v/policies", "policies"), ("v/db", "db_json"), ("v/normalized/markdown", None),
    ("v/indexes/keyword", None),
])
def test_directory_family_classification(path, fam):
    assert write_guard.directory_family(path) == fam
