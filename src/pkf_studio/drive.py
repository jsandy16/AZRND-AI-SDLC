"""Who advances a pipeline's run (0.8.2): one browser tab at a time holds a short lease and ticks the run, the
assistant's own actions and Full auto; the other tabs only read. A tab in the background (its 2-s timer slowed by the
browser) lets its lease expire, and a visible tab takes over. Buttons a person clicks are not leased.

The lease is `.pkf-studio/locks/drive/<pipeline>.json` {owner, at}; it expires after TTL seconds without renewal."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import STUDIO_DIR

TTL = 8.0


def _path(root: Path, p: str) -> Path:
    return Path(root) / STUDIO_DIR / "locks" / "drive" / f"{p}.json"


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def acquire(root: Path, p: str, owner: str, ttl: float = TTL, now: float | None = None) -> bool:
    """True when `owner` holds (or just took) the lease; renews it. Never raises."""
    now = time.time() if now is None else now
    path = _path(root, p)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        cur = _read(path)
        if cur and cur.get("owner") != owner and now - float(cur.get("at") or 0) < ttl:
            return False
        tmp = path.with_suffix(f".{os.getpid()}.{owner[:8]}.tmp")
        tmp.write_text(json.dumps({"owner": owner, "at": now}), encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        back = _read(path)                     # two tabs taking over at once: the last writer wins, the other backs off
        return bool(back and back.get("owner") == owner)
    except OSError:
        return True                            # a read-only or odd file system: behave as before 0.8.2 (every tab ticks)


def release(root: Path, p: str, owner: str) -> None:
    path = _path(root, p)
    cur = _read(path)
    if cur and cur.get("owner") == owner:
        try:
            path.unlink()
        except OSError:
            pass
