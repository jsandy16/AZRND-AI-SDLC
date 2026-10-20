""".pkf-studio/flows/<p>/: the Workflow Developer run's cursor (state.json) and its journal (events.jsonl). Git-ignored and
disposable: pipeline truth is in git and on GitHub (stages.derive); this only remembers where the run is, what the user
answered and when things happened. Writes are atomic (temp file + replace); the journal is append-only.

assistant/<session>/ under the same folder holds one Assistant session (transcript.jsonl, diagnosis.json, fix.json,
summary.json)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from . import STUDIO_DIR


def flow_dir(root: Path, pipeline: str) -> Path:
    return Path(root) / STUDIO_DIR / "flows" / pipeline


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _ignored(path: Path) -> None:
    """The flows folder ignores itself (repositories set up before 0.6.0 have no .pkf-studio/flows/ in .gitignore)."""
    parts = path.parts
    if STUDIO_DIR in parts and "flows" in parts:
        gi = Path(*parts[:parts.index("flows") + 1]) / ".gitignore"
        if not gi.exists():
            gi.parent.mkdir(parents=True, exist_ok=True)
            gi.write_text("*\n", encoding="utf-8", newline="\n")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ignored(path)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load(root: Path, pipeline: str) -> dict | None:
    """state.json, or None when the run has no state (never started, or the folder was deleted)."""
    try:
        doc = json.loads((flow_dir(root, pipeline) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def save(root: Path, pipeline: str, state: dict) -> None:
    _write_atomic(flow_dir(root, pipeline) / "state.json", json.dumps(state, indent=2, default=str, sort_keys=False) + "\n")


def event(root: Path, pipeline: str, type: str, **data) -> dict:  # noqa: A002 - the contract's name
    """Append {"ts", "type", **data} to events.jsonl (created with its folder when missing); returns the row."""
    row = {"ts": _now(), "type": str(type), **data}
    path = flow_dir(root, pipeline) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ignored(path)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    return row


def events(root: Path, pipeline: str, n: int = 500, types: set[str] | None = None) -> list[dict]:
    """The last `n` rows of the journal (oldest first), optionally only the given types; a damaged line is skipped."""
    path = flow_dir(root, pipeline) / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or (types is not None and row.get("type") not in types):
            continue
        out.append(row)
        if len(out) >= n:
            break
    out.reverse()
    return out


def assistant_dir(root: Path, pipeline: str, session_id: str) -> Path:
    """.pkf-studio/flows/<p>/assistant/<session>/ (created)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(session_id)) or "session"
    d = flow_dir(root, pipeline) / "assistant" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d
