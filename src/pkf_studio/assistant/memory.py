"""The pipeline's memory notes (0.8.0): `.pkf-studio/flows/<p>/assistant/memory.md`, one timestamped line per note.

The assistant writes it only through `remember` (the tool of the same name), never through Edit/Write: the path is
fixed here, the note is one line of at most NOTE_MAX characters with secrets redacted, and the file keeps the newest
notes within FILE_MAX bytes. The notes travel in the run brief, so every later session — diagnose, fix, ask — starts
from what an earlier one found (a root cause, a decision the user made, a fix that did not work). Git-ignored like the
rest of flows/; part of the artifacts zip's workflow/assistant/ folder."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from pkf.llm.base import redact_secrets

from .. import flowstore

NOTE_MAX = 300
FILE_MAX = 4096
FILE = "memory.md"


def path(root: Path, pipeline: str) -> Path:
    return flowstore.flow_dir(root, pipeline) / "assistant" / FILE


def notes(root: Path, pipeline: str) -> list[str]:
    """The notes, oldest first, each `<UTC timestamp> <text>`; [] when there are none (never raises)."""
    try:
        text = path(root, pipeline).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    return [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ") and ln[2:].strip()]


def _header(pipeline: str) -> str:
    return (f"# Assistant memory — {pipeline}\n\n"
            "Written by the assistant's `remember` tool (one line per note, newest last; the oldest go beyond 4 KB).\n\n")


def remember(root: Path, pipeline: str, note: str) -> str:
    """Append one note (whitespace collapsed, secrets redacted, cut to NOTE_MAX), dropping the oldest notes while the
    file would exceed FILE_MAX. Returns what was stored."""
    text = redact_secrets(" ".join(str(note or "").split()))[0].strip()
    if not text:
        raise ValueError("the note is empty")
    if len(text) > NOTE_MAX:
        text = text[:NOTE_MAX - 1] + "…"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [f"- {n}" for n in notes(root, pipeline)] + [f"- {stamp} {text}"]
    head = _header(pipeline)
    while len(lines) > 1 and len((head + "\n".join(lines) + "\n").encode("utf-8")) > FILE_MAX:
        lines.pop(0)
    flowstore._write_atomic(path(root, pipeline), head + "\n".join(lines) + "\n")
    return f"remembered: {stamp} {text}"
