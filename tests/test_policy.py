from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import policy


def test_load_yaml_nested_maps_lists_scalars():
    text = (
        "version: 0.1\n"
        "router:\n"
        "  default_mode_set:\n"
        "    - keyword\n"
        "  rules:\n"
        "    discovery:          # inline comment after a key\n"
        "      - navigation\n"
        "      - graph\n"
        "  escalation_primary_below_k: 3\n"
        "caps:\n"
        "  max_graph_nodes: 50\n"
        "  ratio: 1.5\n"
        "  enabled: true\n"
        "fallbacks:\n"
        '  text: "No source found in vault."\n'
        "# a trailing full-line comment\n"
    )
    data = policy.load_yaml(text)
    assert data["version"] == 0.1
    assert data["router"]["default_mode_set"] == ["keyword"]
    assert data["router"]["rules"]["discovery"] == ["navigation", "graph"]
    assert data["router"]["escalation_primary_below_k"] == 3
    assert data["caps"]["max_graph_nodes"] == 50
    assert data["caps"]["ratio"] == 1.5
    assert data["caps"]["enabled"] is True
    assert data["fallbacks"]["text"] == "No source found in vault."


def test_quoted_value_keeps_hash():
    data = policy.load_yaml('key: "a # b"\n')
    assert data["key"] == "a # b"


def test_load_retrieval_policy_reads_repo_file():
    p = policy.load_retrieval_policy(ROOT / "policies" / "retrieval.yaml")
    assert p.modes_for_shape("discovery") == ["navigation", "graph"]
    assert p.modes_for_shape("exact") == ["keyword"]
    # Unknown shape falls back to the default mode set.
    assert p.modes_for_shape("nonsense") == p.default_mode_set
    assert p.cap("max_graph_nodes") == 50
    assert p.cap("max_evidence_hits") == 20


def test_load_retrieval_policy_missing_file_uses_defaults(tmp_path):
    p = policy.load_retrieval_policy(tmp_path / "absent.yaml")
    assert p.modes_for_shape("relationship") == ["graph"]
    assert p.cap("max_graph_edges") == 100
    assert p.default_mode_set == ["keyword"]


def test_invalid_policy_modes_are_filtered(tmp_path):
    # A typo in a rule must not yield a retrieval_path that runs no channel: a rule emptied by
    # filtering is dropped (falls back to default), and an all-bogus default falls back to keyword.
    f = tmp_path / "retrieval.yaml"
    f.write_text(
        "router:\n"
        "  default_mode_set:\n"
        "    - bogus\n"
        "  rules:\n"
        "    discovery:\n"
        "      - navigation\n"
        "      - bogus\n"
        "    exact:\n"
        "      - typo\n",
        encoding="utf-8",
    )
    p = policy.load_retrieval_policy(f)
    assert p.modes_for_shape("discovery") == ["navigation"]   # bogus dropped, valid kept
    assert p.modes_for_shape("exact") == p.default_mode_set    # all-bogus rule -> default
    assert p.default_mode_set == ["keyword"]                   # all-bogus default -> keyword fallback


def test_invalid_rrf_k_falls_back_to_default(tmp_path):
    f = tmp_path / "retrieval.yaml"
    f.write_text("caps:\n  rrf_k: -1\n", encoding="utf-8")
    assert policy.load_retrieval_policy(f).cap("rrf_k") == 60  # <1 -> default, never a divisor of 0


def test_negative_escalation_threshold_falls_back(tmp_path):
    f = tmp_path / "retrieval.yaml"
    f.write_text("caps:\n  escalation_primary_below_k: -2\n", encoding="utf-8")
    assert policy.load_retrieval_policy(f).cap("escalation_primary_below_k") == 3  # negative -> default


def test_malformed_policy_values_do_not_crash(tmp_path):
    f = tmp_path / "retrieval.yaml"
    f.write_text("router: not-a-mapping\ncaps:\n  max_graph_nodes: not-an-int\n", encoding="utf-8")
    p = policy.load_retrieval_policy(f)
    # Non-dict router and non-int cap are ignored; defaults stand.
    assert p.modes_for_shape("discovery") == ["navigation", "graph"]
    assert p.cap("max_graph_nodes") == 50
