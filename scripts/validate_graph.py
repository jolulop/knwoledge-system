#!/usr/bin/env python3
"""Validate the Phase 3.5b semantic graph (`db/graph.sqlite`, ADR-0030).

Checks the graph's internal integrity and governed vocabulary:
- every `node_type` is within Build Spec §6.1 and every `edge_type` within §6.2 **minus
  `needs_review`** (a literal `needs_review` edge is rejected — review is a `status`);
- every assertion's `status` and `asserted_by` are within their allowed sets;
- every edge references existing node ids in both `src_id` and `dst_id` (no slug-keyed or
  dangling edges).

If there is no graph database yet (no producers have run), there is nothing to validate —
a pass. The page-level projection round-trip (page links match `active` assertions) is
enforced once the producers wire the projector into pages (Phase 3.5b slices 3/4).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph


def _check(db_path: Path) -> list[str]:
    errors: list[str] = []
    conn = graph.connect(db_path)
    try:
        node_types = {r["node_id"]: r["node_type"] for r in conn.execute("SELECT node_id, node_type FROM nodes")}
        for node_id, node_type in node_types.items():
            if node_type not in graph.NODE_TYPES:
                errors.append(f"node {node_id}: invalid node_type {node_type!r}")

        known = set(node_types)
        for e in conn.execute("SELECT * FROM edges"):
            ref = e["edge_id"]
            if e["edge_type"] not in graph.EDGE_TYPES:
                errors.append(f"edge {ref}: invalid edge_type {e['edge_type']!r} (needs_review is a status)")
            if e["status"] not in graph.EDGE_STATUSES:
                errors.append(f"edge {ref}: invalid status {e['status']!r}")
            if e["asserted_by"] not in graph.ASSERTED_BY:
                errors.append(f"edge {ref}: invalid asserted_by {e['asserted_by']!r}")
            if e["src_id"] not in known:
                errors.append(f"edge {ref}: src_id {e['src_id']!r} is not an indexed node")
            if e["dst_id"] not in known:
                errors.append(f"edge {ref}: dst_id {e['dst_id']!r} is not an indexed node")
    finally:
        conn.close()
    return errors


def main(argv: list[str]) -> int:
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    db_path = root / "db" / "graph.sqlite"
    if not db_path.exists():
        print("Graph validation passed (no graph database yet).")
        return 0

    errors = _check(db_path)
    if errors:
        print("Graph validation failed:")
        for err in errors:
            print(f"- {err}")
        return 1
    print("Graph validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
