from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]

app = FastAPI(title="Knowledge System", version="0.1.0")


class QueryRequest(BaseModel):
    question: str
    mode: str = "auto"
    save_answer: bool = False
    filters: dict[str, Any] = {}


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "project_root": str(PROJECT_ROOT),
        "wiki_exists": (PROJECT_ROOT / "wiki").exists(),
    }


@app.get("/wiki/index")
def read_index() -> dict[str, str]:
    path = PROJECT_ROOT / "wiki" / "index.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="index.md not found")
    return {"path": str(path.relative_to(PROJECT_ROOT)), "content": path.read_text(encoding="utf-8")}


@app.post("/query")
def query(req: QueryRequest) -> dict[str, Any]:
    # Placeholder: implement retrieval router in later phase.
    return {
        "answer": "Query routing is not implemented yet. Use scripts and wiki/index.md for the scaffold phase.",
        "question": req.question,
        "mode": req.mode,
        "citations": [],
        "retrieval_path": [],
        "confidence": "low",
        "unsourced_claims": ["No source found in vault."],
    }
