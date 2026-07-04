#!/usr/bin/env python3
"""Validate the in-process FlagEmbedding backend (ADR-0053): torch/CUDA probe + BGE-M3 dense smoke.

Run this before ``scripts/reindex_vector.py`` to confirm the box can load and run BAAI/bge-m3. It reads
the ``EMBEDDING_*`` config (``EMBEDDING_MODEL_ID`` / ``EMBEDDING_DEVICE`` / ``EMBEDDING_USE_FP16`` /
``EMBEDDING_BATCH_SIZE`` / ``EMBEDDING_MAX_LENGTH`` / ``EMBEDDING_DIMENSION`` / ``EMBEDDING_CACHE_DIR``)
and:

  1. prints ``torch.__version__``, ``torch.version.cuda``, ``torch.cuda.get_device_name(0)``;
  2. loads the model once — **fail-fast** if ``EMBEDDING_DEVICE=cuda`` and CUDA is unavailable;
  3. embeds the smoke inputs and asserts 3 vectors of dimension 1024, no ``None`` dense result.

Exit 0 on success, non-zero on any failure. ``--json`` emits the health block as JSON. This inspects the
*in-process* backend regardless of the currently-selected ``EMBEDDING_PROVIDER`` (so an operator can
validate BGE-M3 before flipping the provider).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backend import embeddings
from app.backend.config import get_settings

SMOKE_TEXTS = ["hello world", "hola mundo", "semantic search over enterprise documents"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the in-process FlagEmbedding backend (ADR-0053)."
    )
    parser.add_argument("root", nargs="?", default=None, help="project root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit the health block as JSON")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path.cwd()
    settings = get_settings(root)
    config = embeddings.FlagEmbeddingConfig.from_settings(settings)
    provider = embeddings.BgeM3FlagEmbeddingProvider(config)

    try:
        info = provider.validate_startup()  # probe + fail-fast (device=cuda) + load once
        vecs = provider.embed_texts(SMOKE_TEXTS)
    except embeddings.EmbeddingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    n = len(vecs)
    dim = len(vecs[0]) if vecs else 0
    ok = n == len(SMOKE_TEXTS) and all(len(v) == config.dimension for v in vecs)
    info.update({"smoke_count": n, "smoke_dim": dim, "dense_vecs_shape": [n, dim], "smoke_ok": ok})

    if args.json:
        print(json.dumps(info, indent=2, sort_keys=True))
    else:
        print(f"torch: {info.get('torch_version')}")
        print(f"cuda available: {info.get('cuda_available')}")
        print(f"cuda version: {info.get('torch_cuda_version')}")
        print(f"device: {info.get('cuda_device_name')}")
        print(f"model: {config.model_id}  device: {config.device}  fp16: {config.use_fp16}")
        print(f"model_ref: {config.model_ref}")
        print(f"dense_vecs shape: ({n}, {dim})")

    if not ok:
        print(
            f"error: expected {len(SMOKE_TEXTS)} vectors of dim {config.dimension}, got {n} of dim {dim}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
