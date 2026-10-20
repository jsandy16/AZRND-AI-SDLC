"""Shared fixtures. Every e2e test works in this session's own temporary root, so two pytest runs (or xdist workers)
never share the drop/lake folders, the staging folder or a scripted-LLM state file."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKELLM = ROOT / "tests" / "fakellm"


@pytest.fixture(scope="session")
def e2e_root(tmp_path_factory) -> Path:
    """The e2e tests' drop/lake folders (environment values the AI never sees, so replay keys do not depend on them)."""
    return tmp_path_factory.mktemp("pkf-studio-e2e")


def use_script(monkeypatch, tmp_path: Path, name: str, edit=None) -> Path:
    """Answer the AI from tests/fakellm/<name>.json (provider `script`, K12) through a per-test copy, so the usage counts
    (<script>.state.json) are never shared. `edit(doc)` may change the copy. Returns the prompt log (jsonl)."""
    doc = json.loads((FAKELLM / f"{name}.json").read_text(encoding="utf-8"))
    if edit is not None:
        edit(doc)
    script = tmp_path / "fakellm" / f"{name}.json"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(json.dumps(doc, indent=1), encoding="utf-8", newline="\n")
    log = tmp_path / "fakellm" / f"{name}.log.jsonl"
    monkeypatch.setenv("PKF_LLM_PROVIDER", "script")
    monkeypatch.setenv("PKF_LLM_SCRIPT", str(script))
    monkeypatch.setenv("PKF_LLM_SCRIPT_LOG", str(log))
    return log


def fresh(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path
