from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from app.backend import graph
from app.backend.paths import safe_child, safe_under


def test_safe_under_contains_and_rejects(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    assert safe_under(base, base, "ok.md") == (base / "ok.md").resolve()
    assert safe_under(base, base, "sub/ok.md") == (base / "sub" / "ok.md").resolve()  # contained subdir
    assert safe_under(base, base, "../evil.md") is None
    assert safe_under(base, base, "../../etc/passwd") is None
    assert safe_under(base, base, "/abs/evil.md") is None


def test_safe_child_basename_only(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    assert safe_child(base, "clm_0123456789abcdef.md") == (base / "clm_0123456789abcdef.md").resolve()
    # stricter than safe_under: even a *contained* nested segment is rejected (not a basename)
    assert safe_child(base, "sub/ok.md") is None
    assert safe_child(base, "../evil.md") is None
    assert safe_child(base, "/abs/evil.md") is None
    assert safe_child(base, "..") is None
    assert safe_child(base, "a\\b.md") is None


def test_validate_graph_rejects_noncanonical_node_id(tmp_path, capsys):
    import validate_graph as vg
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    # claim ids have a documented grammar (clm_<16 hex>) -> a path-like one must hard-fail
    graph.upsert_node(conn, node_id="../../etc/x", node_type="claim", slug="x", status="active")
    conn.commit()
    conn.close()
    assert vg.main([str(tmp_path)]) == 1
    assert "etc" not in capsys.readouterr().out          # never echoes the malformed id


def test_validate_graph_accepts_canonical_node_id(tmp_path):
    import validate_graph as vg
    gdb = tmp_path / "db" / "graph.sqlite"
    gdb.parent.mkdir(parents=True)
    graph.init_db(gdb)
    conn = graph.connect(gdb)
    graph.upsert_node(conn, node_id="clm_0123456789abcdef", node_type="claim", slug="x", status="active")
    conn.commit()
    conn.close()
    assert vg.main([str(tmp_path)]) == 0
