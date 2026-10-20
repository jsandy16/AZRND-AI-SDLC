"""A folder on the worker (development and tests)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import RemoteFile, depth_ok


class LocalSession:
    def __init__(self, conn: dict, creds: dict[str, str]):
        self.base = Path(conn["base_path"]).expanduser()

    def _root(self, root: str) -> Path:
        p = (self.base / root.strip("/")).resolve()
        if self.base.resolve() not in (p, *p.parents):
            raise PermissionError("extraction root escapes base_path")
        return p

    def check(self) -> str:
        if not self.base.is_dir():
            raise FileNotFoundError(f"{self.base} is not a folder")
        return f"folder {self.base} readable"

    def list(self, root: str, recursive: bool, max_depth: int | None) -> list[RemoteFile]:
        r = self._root(root)
        out = []
        for dirpath, dirs, names in os.walk(r):
            rel_dir = Path(dirpath).relative_to(r).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            dirs[:] = sorted(d for d in dirs if recursive and depth_ok(f"{rel_dir}/{d}" if rel_dir else d, max_depth))
            for n in names:
                st = Path(dirpath, n).stat()
                out.append(RemoteFile(f"{rel_dir}/{n}" if rel_dir else n, st.st_size, st.st_mtime))
        return out

    def stat(self, root: str, rel: str) -> RemoteFile | None:
        p = self._root(root) / rel
        if not p.is_file() or (self.base.resolve() not in p.resolve().parents):
            return None
        st = p.stat()
        return RemoteFile(rel, st.st_size, st.st_mtime)

    def download(self, root: str, rel: str, dest: str) -> None:
        shutil.copyfile(self._root(root) / rel, dest)

    def close(self) -> None:
        pass
