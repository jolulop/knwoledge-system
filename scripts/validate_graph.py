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

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import graph

# Hard-fail only for node-id grammars that are explicitly documented: `src_<sha256[:16]>` (ADR-0007),
# `clm_<sha256[:16]>` (claims), `syn_<sha256[:16]>` (synthesis). These flow into filesystem paths, so a
# non-canonical id is tampering — the validator twin of the runtime `safe_child` guards (ADR-0009/0037).
# concept/entity/person/organization/project canonicality is **deferred** (their id grammar is not yet
# explicitly fixed here); the runtime `safe_child` guard already protects their paths. tag/query carry no
# such id. The defense-in-depth validator stays within documented id rules.
_NODE_ID_PREFIX = {"source": "src", "claim": "clm", "synthesis": "syn"}


def _check(db_path: Path) -> list[str]:
    errors: list[str] = []
    conn = graph.connect(db_path)
    try:
        node_rows = list(conn.execute("SELECT node_id, node_type, status FROM nodes"))
        node_types = {r["node_id"]: r["node_type"] for r in node_rows}
        node_status = {r["node_id"]: r["status"] for r in node_rows}
        for node_id, node_type in node_types.items():
            if node_type not in graph.NODE_TYPES:
                errors.append(f"node {node_id}: invalid node_type {node_type!r}")
            prefix = _NODE_ID_PREFIX.get(node_type)
            if prefix and not re.fullmatch(rf"{prefix}_[0-9a-f]{{16}}", node_id):
                # Sanitize: never echo a path-like/oversized id verbatim.
                errors.append(f"node of type {node_type!r}: non-canonical id (expected {prefix}_<16 hex>)")

        known = set(node_types)
        for e in conn.execute("SELECT * FROM edges"):
            ref = e["edge_id"]
            edge_type = e["edge_type"]
            if edge_type not in graph.EDGE_TYPES:
                errors.append(f"edge {ref}: invalid edge_type {edge_type!r} (needs_review is a status)")
            if e["status"] not in graph.EDGE_STATUSES:
                errors.append(f"edge {ref}: invalid status {e['status']!r}")
            if e["asserted_by"] not in graph.ASSERTED_BY:
                errors.append(f"edge {ref}: invalid asserted_by {e['asserted_by']!r}")

            # Canonical ordering for the symmetric, canonically-stored relations (`contradicts`
            # ADR-0031, `duplicates` ADR-0041): the pair is stored once with src_id < dst_id, so
            # A-vs-B and B-vs-A cannot become two rows (nor a reversed row enter via tampered DB).
            if edge_type in ("contradicts", "duplicates") and e["src_id"] >= e["dst_id"]:
                errors.append(f"edge {ref}: {edge_type} must be canonically ordered "
                              f"(src_id < dst_id), got {e['src_id']} >= {e['dst_id']}")

            src_in = e["src_id"] in known
            dst_in = e["dst_id"] in known
            if not src_in:
                errors.append(f"edge {ref}: src_id {e['src_id']!r} is not an indexed node")
            if not dst_in:
                errors.append(f"edge {ref}: dst_id {e['dst_id']!r} is not an indexed node")

            # ADR-0050 merge invariant: no ACTIVE edge may have a `merged` (absorbed-identity) endpoint —
            # a merge re-points every active edge off the absorbed id, so a live reference to a tombstone
            # means the rewrite was incomplete.
            if e["status"] == "active":
                for endpoint in (e["src_id"], e["dst_id"]):
                    if node_status.get(endpoint) == "merged":
                        errors.append(f"edge {ref}: active edge has a merged endpoint {endpoint} "
                                      f"(ADR-0050: merge must re-point all active edges off the absorbed id)")

            # Endpoint-type contract (ADR-0030), checked only when both nodes resolve.
            if src_in and dst_in and edge_type in graph.EDGE_TYPES:
                src_t, dst_t = node_types[e["src_id"]], node_types[e["dst_id"]]
                if edge_type in graph.SAME_TYPE_EDGES:
                    if src_t != dst_t:
                        errors.append(f"edge {ref}: {edge_type} requires same node_type "
                                      f"(got {src_t} -> {dst_t})")
                else:
                    allowed_src, allowed_dst = graph.EDGE_ENDPOINTS[edge_type]
                    if allowed_src is not None and src_t not in allowed_src:
                        errors.append(f"edge {ref}: {edge_type} src must be one of "
                                      f"{sorted(allowed_src)}, got {src_t}")
                    if allowed_dst is not None and dst_t not in allowed_dst:
                        errors.append(f"edge {ref}: {edge_type} dst must be one of "
                                      f"{sorted(allowed_dst)}, got {dst_t}")

            # Evidence-anchor structural integrity (resolvability vs normalized text is the
            # citation gate's job once claims wire in; here we check the anchor is well-formed).
            start, end, ev_src = e["evidence_char_start"], e["evidence_char_end"], e["evidence_source_id"]
            if (start is None) != (end is None):
                errors.append(f"edge {ref}: evidence char range half-specified")
            elif start is not None:
                if start < 0 or end <= start:
                    errors.append(f"edge {ref}: evidence char range [{start}, {end}) is invalid")
                if not ev_src:
                    errors.append(f"edge {ref}: evidence char range without an evidence_source_id")
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
