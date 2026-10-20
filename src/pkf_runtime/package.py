"""A package on local disk: a tree of files plus metadata (name, per-file checksums, template variables).

Every operation and every custom transformation step works only through this API, so packages are handled by
structure (paths, sizes, checksums, archives) — file contents are never parsed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import string
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

META_FILE = "package.json"
FILES_DIR = "files"


BUILTIN_NAMES = frozenset({"pipeline", "source", "destination", "connection", "trigger", "package", "run_id", "ds",
                           "ds_nodash", "ts_nodash", "env"})
"""Run variables the framework sets; nothing else may define them."""


class PackageError(Exception):
    """The package does not meet the transformation's rules (a data problem, not a bug): the run fails, no retry."""


class TemplateError(PackageError, ValueError):
    pass


# ---------------------------------------------------------------------------------------------- templates and globs
def render(template: str, variables: dict[str, Any]) -> str:
    """Resolve {name} placeholders; an unknown name is an error (never an empty string)."""
    out = []
    for literal, field, spec, conv in string.Formatter().parse(template):
        out.append(literal)
        if field is None:
            continue
        if spec or conv or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
            raise TemplateError(f"template field '{{{field}}}' must be a plain name")
        if field not in variables or variables[field] is None:
            raise TemplateError(f"unknown template variable '{{{field}}}'; available: {sorted(k for k, v in variables.items() if v is not None)}")
        out.append(str(variables[field]))
    return "".join(out)


_GLOB_CACHE: dict[str, re.Pattern] = {}


def glob_regex(pattern: str) -> re.Pattern:
    """Glob on '/'-separated relative paths: ** = any number of folders, * = within one name, ? = one char,
    [abc] = character class. 'a/**' matches everything under a; '**/*.csv' matches csv files at any depth."""
    if pattern in _GLOB_CACHE:
        return _GLOB_CACHE[pattern]
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1:j].replace("\\", "\\\\")
                out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    rx = re.compile("".join(out) + r"\Z")
    _GLOB_CACHE[pattern] = rx
    return rx


def glob_match(pattern: str, path: str) -> bool:
    return glob_regex(pattern).match(path) is not None


def matches_any(patterns: Iterable[str], path: str) -> bool:
    return any(glob_match(p, path) for p in patterns)


def safe_relpath(path: str) -> str:
    """Normalise a package-relative path and refuse anything that could escape the package."""
    p = path.replace("\\", "/").strip()
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise PackageError(f"'{path}' is not a relative path")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts):
        raise PackageError(f"'{path}' escapes the package")
    return "/".join(parts)


# ---------------------------------------------------------------------------------------------- package
class Package:
    """Files under <workdir>/files/, metadata in <workdir>/package.json."""

    def __init__(self, workdir: Path, name: str = "package", variables: dict[str, Any] | None = None,
                 checksums: dict[str, dict[str, str]] | None = None):
        self.workdir = Path(workdir)
        self.root = self.workdir / FILES_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self._name = name
        self.vars: dict[str, Any] = dict(variables or {})
        self.vars["package"] = name
        self.checksums: dict[str, dict[str, str]] = dict(checksums or {})

    # --- identity
    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        value = value.strip()
        if not value or "/" in value:
            raise PackageError(f"package name '{value}' must be non-empty and contain no '/'")
        self._name = value
        self.vars["package"] = value

    # --- reading the structure
    def files(self) -> list[str]:
        out = []
        for dirpath, _, names in os.walk(self.root):
            for n in names:
                out.append(Path(dirpath, n).relative_to(self.root).as_posix())
        return sorted(out)

    def exists(self, rel: str) -> bool:
        return (self.root / safe_relpath(rel)).is_file()

    def path(self, rel: str) -> Path:
        p = self.root / safe_relpath(rel)
        if not p.is_file():
            raise PackageError(f"no file '{rel}' in the package")
        return p

    def size(self, rel: str) -> int:
        return self.path(rel).stat().st_size

    def total_bytes(self) -> int:
        return sum(self.size(f) for f in self.files())

    def checksum(self, rel: str, algorithm: str = "sha256") -> str:
        """Computed once per file and remembered (moves keep it)."""
        rel = safe_relpath(rel)
        known = self.checksums.get(rel, {})
        if algorithm in known:
            return known[algorithm]
        h = hashlib.new(algorithm)
        with open(self.path(rel), "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        self.checksums.setdefault(rel, {})[algorithm] = h.hexdigest()
        return h.hexdigest()

    # --- changing the structure
    def _target(self, rel: str, overwrite: bool) -> Path:
        dst = self.root / safe_relpath(rel)
        if dst.exists() and not overwrite:
            raise PackageError(f"'{rel}' already exists in the package")
        dst.parent.mkdir(parents=True, exist_ok=True)
        return dst

    def move(self, src: str, dst: str, overwrite: bool = False) -> None:
        src, dst = safe_relpath(src), safe_relpath(dst)
        if src == dst:
            return
        target = self._target(dst, overwrite)
        os.replace(self.path(src), target)
        if src in self.checksums:
            self.checksums[dst] = self.checksums.pop(src)
        else:
            self.checksums.pop(dst, None)
        self._prune(Path(src).parent)

    def copy(self, src: str, dst: str, overwrite: bool = False) -> None:
        dst = safe_relpath(dst)
        shutil.copyfile(self.path(src), self._target(dst, overwrite))
        if safe_relpath(src) in self.checksums:
            self.checksums[dst] = dict(self.checksums[safe_relpath(src)])

    def remove(self, rel: str) -> None:
        rel = safe_relpath(rel)
        self.path(rel).unlink()
        self.checksums.pop(rel, None)
        self._prune(Path(rel).parent)

    def write_bytes(self, rel: str, data: bytes, overwrite: bool = False) -> None:
        rel = safe_relpath(rel)
        self._target(rel, overwrite).write_bytes(data)
        self.checksums.pop(rel, None)

    def write_text(self, rel: str, text: str, overwrite: bool = False) -> None:
        self.write_bytes(rel, text.encode("utf-8"), overwrite)

    def add_local_file(self, rel: str, local: Path, overwrite: bool = False) -> None:
        """Move a file produced on local disk (e.g. a new archive) into the package."""
        rel = safe_relpath(rel)
        os.replace(local, self._target(rel, overwrite))
        self.checksums.pop(rel, None)

    def new_temp_path(self, suffix: str = "") -> Path:
        tmp = self.workdir / "tmp"
        tmp.mkdir(exist_ok=True)
        import tempfile

        fd, name = tempfile.mkstemp(dir=tmp, suffix=suffix)
        os.close(fd)
        return Path(name)

    def render(self, template: str, extra: dict[str, Any] | None = None) -> str:
        return render(template, {**self.vars, **(extra or {})})

    def fail(self, message: str) -> None:
        raise PackageError(message)

    def _prune(self, folder: Path) -> None:
        d = self.root / folder
        while d != self.root and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent

    # --- persistence between tasks
    def save(self) -> None:
        shutil.rmtree(self.workdir / "tmp", ignore_errors=True)
        live = set(self.files())
        meta = {"name": self._name, "vars": {k: v for k, v in self.vars.items() if isinstance(v, (str, int, float, bool)) or v is None},
                "checksums": {k: v for k, v in self.checksums.items() if k in live}}
        (self.workdir / META_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8", newline="\n")

    @classmethod
    def load(cls, workdir: Path) -> "Package":
        meta = json.loads((Path(workdir) / META_FILE).read_text(encoding="utf-8"))
        pkg = cls(workdir, meta["name"], meta.get("vars"), meta.get("checksums"))
        return pkg

    def describe(self, expand_archives: bool = True) -> list[str]:
        """The package's structure: every path, plus archive members as 'bundle.zip!/inner/path'."""
        out = []
        for f in self.files():
            out.append(f)
            if expand_archives and f.lower().endswith(".zip"):
                import zipfile

                try:
                    with zipfile.ZipFile(self.path(f)) as zf:
                        out += [f"{f}!/{i.filename}" for i in zf.infolist() if not i.is_dir()]
                except zipfile.BadZipFile:
                    pass
        return sorted(out)


class StepPackage:
    """What a generated custom step sees: the package API without local paths or internals, so reviewed code can
    only work on this package's files. Files are read and written as streams (for zipfile, gzip, hashlib)."""

    def __init__(self, pkg: Package):
        self._pkg = pkg

    name = property(lambda self: self._pkg.name, lambda self, v: setattr(self._pkg, "name", v))
    vars = property(lambda self: dict(self._pkg.vars))

    def files(self) -> list[str]:
        return self._pkg.files()

    def exists(self, rel: str) -> bool:
        return self._pkg.exists(rel)

    def size(self, rel: str) -> int:
        return self._pkg.size(rel)

    def total_bytes(self) -> int:
        return self._pkg.total_bytes()

    def checksum(self, rel: str, algorithm: str = "sha256") -> str:
        return self._pkg.checksum(rel, algorithm)

    def move(self, src: str, dst: str, overwrite: bool = False) -> None:
        self._pkg.move(src, dst, overwrite)

    def copy(self, src: str, dst: str, overwrite: bool = False) -> None:
        self._pkg.copy(src, dst, overwrite)

    def remove(self, rel: str) -> None:
        self._pkg.remove(rel)

    def write_text(self, rel: str, text: str, overwrite: bool = False) -> None:
        self._pkg.write_bytes(rel, text.encode("utf-8"), overwrite)  # UTF-8, newlines exactly as given

    def write_bytes(self, rel: str, data: bytes, overwrite: bool = False) -> None:
        self._pkg.write_bytes(rel, data, overwrite)

    def open_read(self, rel: str):
        """Binary read stream of a package file (e.g. for zipfile.ZipFile or hashlib)."""
        return open(self._pkg.path(rel), "rb")

    def open_write(self, rel: str, overwrite: bool = False):
        """Binary write stream for a new package file (e.g. an archive built with zipfile)."""
        rel = safe_relpath(rel)
        self._pkg.checksums.pop(rel, None)
        return open(self._pkg._target(rel, overwrite), "wb")

    def render(self, template: str, extra: dict[str, Any] | None = None) -> str:
        return self._pkg.render(template, extra)

    def fail(self, message: str) -> None:
        raise PackageError(message)


def posix_parts(rel: str) -> tuple[str, str, str, str]:
    """(dir, name, stem, suffix) of a package-relative path; dir is '' at the root."""
    p = PurePosixPath(rel)
    d = "" if str(p.parent) == "." else str(p.parent)
    suffix = "".join(p.suffixes[-2:]) if p.name.lower().endswith((".tar.gz",)) else p.suffix
    stem = p.name[: len(p.name) - len(suffix)] if suffix else p.name
    return d, p.name, stem, suffix
