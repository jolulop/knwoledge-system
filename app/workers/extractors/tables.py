#!/usr/bin/env python3
"""Tabular extractor (Phase 2): XLSX/CSV via pandas → structured CSV + table chunks.

Each sheet (CSV is treated as a single sheet) is written to
``normalized/tables/<source_id>/<n>.csv`` and emitted as a ``kind:"table"`` Element
whose ``table_reference`` points at that file and whose ``sheet_reference`` is the
sheet name. Cells are read as strings with NA disabled so output is byte-stable for
byte-stable input. A section heading per sheet keeps the normalized Markdown navigable.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.workers.chunking import Element
from app.workers.extractors import Extraction, gfm_table, pkg_version


def _sheets(path: Path) -> list[tuple[str, pd.DataFrame]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        return [("Sheet1", df)]
    frames = pd.read_excel(path, sheet_name=None, dtype=str)
    out: list[tuple[str, pd.DataFrame]] = []
    for name, df in frames.items():
        out.append((str(name), df.fillna("")))
    return out


def extract(path: Path, source_id: str) -> Extraction:
    path = Path(path)
    elements: list[Element] = []
    tables: list[tuple[str, str]] = []

    for index, (sheet_name, df) in enumerate(_sheets(path)):
        filename = f"{index}.csv"
        table_reference = f"normalized/tables/{source_id}/{filename}"
        tables.append((filename, df.to_csv(index=False)))

        header = list(df.columns)
        rows = df.astype(str).values.tolist()
        elements.append(Element(kind="heading", text=sheet_name, level=1))
        elements.append(
            Element(
                kind="table",
                text=gfm_table(header, rows),
                table_reference=table_reference,
                sheet_reference=sheet_name,
            )
        )

    return Extraction(
        elements=elements,
        tool="pandas",
        tool_version=pkg_version("pandas"),
        tables=tables,
    )
