from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_base_api_boots_without_extraction_extras():
    """The core API must import and serve /health even if the extraction extras are
    absent. We block the heavy modules with a meta-path finder in a fresh subprocess,
    proving nothing in the import chain pulls them in eagerly (lazy-import contract)."""
    code = textwrap.dedent(
        """
        import sys, importlib.abc
        BLOCKED = {"pypdf", "docx", "bs4", "pandas", "openpyxl"}

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name.split(".")[0] in BLOCKED:
                    raise ModuleNotFoundError(f"blocked for test: {name}")
                return None

        sys.meta_path.insert(0, Blocker())

        from fastapi.testclient import TestClient
        from app.backend.main import app  # must not import any blocked module

        client = TestClient(app)
        assert client.get("/health").status_code == 200
        print("BOOT_OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "BOOT_OK" in result.stdout
