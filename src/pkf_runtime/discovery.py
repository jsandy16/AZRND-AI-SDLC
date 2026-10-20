"""Source catalogue: which files make up each package.

The source's steps run once per event (see events.py) and turn it into one or more **groups**; every group becomes a
package. Steps see the source only through `SourceView` (list, stat, read a small control file), never through the
raw client, so generated custom steps stay inside the configured source.

Group: {"id", "event_id", "message_ids": [...], "vars": {...}, "objects": [event objects], "files": [File],
        "rejected": [{"path", "key": key|None, "error"}] (only when a file's path is refused; the run reports it)}
File:  {"root": str, "path": str (relative to root, becomes the path inside the package), "size": int|None,
        "mtime": float|None}
"""
from __future__ import annotations

import csv
import io
import json
import posixpath
import re
import tempfile
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .ops import Op, P, _rx
from .package import PackageError, matches_any, render, safe_relpath
from .sources import RemoteFile, depth_ok

MAX_CONTROL_BYTES = 1 << 20


class SourceView:
    """What discovery steps may do with a source."""

    def __init__(self, session):
        self._s = session

    def _f(self, root: str, f) -> dict:
        d = {"root": root, "path": f.path, "size": f.size, "mtime": f.mtime}
        key_of = getattr(self._s, "key_of", None)
        if key_of is not None:  # S3: the exact listed key and version, so a move never touches another object
            d.update(key=key_of(root, f.path), etag=getattr(f, "etag", None))
        return d

    def list(self, root: str, recursive: bool = True, max_depth: int = 20) -> list[dict]:
        return [self._f(root, f) for f in self._s.list(root, recursive, max_depth)]

    def stat(self, root: str, path: str) -> dict | None:
        """The file, or None if it does not exist."""
        stat = getattr(self._s, "stat", None)
        if stat is not None:
            f = stat(root, path)
            return None if f is None else {**self._f(root, f), "path": path}
        parent = posixpath.dirname(path)
        for f in self._s.list(posixpath.join(root, parent) if parent else root, False, 0):
            if f.path == posixpath.basename(path):
                return {"root": root, "path": path, "size": f.size, "mtime": f.mtime}
        return None

    def read_text(self, root: str, path: str, max_bytes: int = MAX_CONTROL_BYTES) -> str:
        """A small control/manifest file's text (metadata about the package, never the data itself)."""
        info = self.stat(root, path)
        if info is None:
            raise PackageError(f"control file '{path}' not found")
        if (info.get("size") or 0) > max_bytes:
            raise PackageError(f"control file '{path}' is larger than {max_bytes} bytes")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            dest = Path(d) / "control"
            self._s.download(root, path, str(dest))
            if dest.stat().st_size > max_bytes:
                raise PackageError(f"control file '{path}' is larger than {max_bytes} bytes")
            return dest.read_bytes().decode("utf-8", errors="replace")


class FakeSource:
    """In-memory source for examples and tests: {path from the source root: (size, age_seconds)}."""

    def __init__(self, listing: dict[str, tuple[int, float]], now: float | None = None, contents: dict[str, str] | None = None):
        self.now = now or time.time()
        self.files = {p.lstrip("/"): v for p, v in listing.items()}  # a key ending in "/" is a folder marker, as in S3
        self.contents = contents or {}

    def _under(self, root: str) -> str:
        r = root.strip("/")
        return f"{r}/" if r else ""

    def list(self, root: str, recursive: bool, max_depth: int | None) -> list[RemoteFile]:
        pre, out = self._under(root), []
        for p, (size, age) in sorted(self.files.items()):
            if not p.startswith(pre) or p.endswith("/"):
                continue  # folder placeholder objects are never files (like the S3 source)
            rel = p[len(pre):]
            d = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if (recursive or not d) and depth_ok(d, max_depth):
                out.append(RemoteFile(rel, size, self.now - age))
        return out

    def stat(self, root: str, rel: str) -> RemoteFile | None:
        p = self._under(root) + rel
        if p not in self.files:
            return None
        size, age = self.files[p]
        return RemoteFile(rel, size, self.now - age)

    def download(self, root: str, rel: str, dest: str) -> None:
        p = self._under(root) + rel
        text = self.contents.get(p)
        data = text.encode() if text is not None else ((p + "\n").encode() * (self.files[p][0] // (len(p) + 1) + 1))[:self.files[p][0]]
        Path(dest).write_bytes(data)


# ---------------------------------------------------------------------------------------------- steps
def _vars(g: dict, ctx: dict) -> dict:
    return {**ctx.get("params", {}), **g.get("vars", {})}


class ListParams(P):
    root: str = Field(default="", description="folder or prefix to list (template); file paths are relative to it")
    recursive: bool = True
    max_depth: int = Field(default=20, ge=0, le=100)
    keep_root: bool = Field(default=False, description="package paths keep the root folder (the file's full path without the "
                            "leading '/') instead of being relative to it")
    blank_root: str | None = Field(default=None, description="when the root renders blank, refuse the package with this message "
                                   "instead of listing the top of the source")


def op_list(groups: list[dict], p: ListParams, ctx: dict, src: SourceView) -> list[dict]:
    for g in groups:
        root = render(p.root, _vars(g, ctx)).strip()
        if p.blank_root is not None and not root.strip("/"):
            raise PackageError(p.blank_root)
        found = src.list(root, p.recursive, p.max_depth)
        if p.keep_root:
            top = root.strip("/")
            found = [{**f, "root": "", "path": f"{top}/{f['path']}" if top else f["path"]} for f in found]
        g["files"] += found
    return groups


class FromEventParams(P):
    root: str = Field(default="", description="prefix the event's keys must start with (template); removed from the package path")
    missing: Literal["fail", "skip"] = Field(default="skip", description="object named by the event no longer exists")


def op_from_event(groups: list[dict], p: FromEventParams, ctx: dict, src: SourceView) -> list[dict]:
    for g in groups:
        root = render(p.root, _vars(g, ctx)).strip("/")
        pre = f"{root}/" if root else ""
        for o in g.get("objects", []):
            key = o["key"]
            if not key.startswith(pre) or key.endswith("/"):
                raise PackageError(f"event object '{key}' is not under '{pre or '(source root)'}'")
            if o.get("bucket") and ctx.get("source_bucket") and o["bucket"] != ctx["source_bucket"]:
                raise PackageError(f"event object is in bucket '{o['bucket']}', not the source bucket '{ctx['source_bucket']}'")
            f = src.stat(root, key[len(pre):])
            if f is None:
                if p.missing == "fail":
                    raise PackageError(f"'{key}' named by the event does not exist")
                continue
            g["files"].append(f)
    return groups


class SelectParams(P):
    include: list[str] = Field(default_factory=lambda: ["**"])
    exclude: list[str] = Field(default_factory=list)
    regex: str | None = None
    _v = field_validator("regex")(classmethod(lambda cls, v: _rx(v)))


def op_select(groups: list[dict], p: SelectParams, ctx: dict, src: SourceView) -> list[dict]:
    for g in groups:
        g["files"] = [f for f in g["files"] if matches_any(p.include, f["path"]) and not matches_any(p.exclude, f["path"])
                      and (p.regex is None or re.fullmatch(p.regex, f["path"]))]
    return groups


class MinAgeParams(P):
    seconds: int = Field(ge=1, description="skip files modified more recently (still being written)")


def op_min_age(groups: list[dict], p: MinAgeParams, ctx: dict, src: SourceView) -> list[dict]:
    now = ctx.get("now") or time.time()
    for g in groups:
        g["files"] = [f for f in g["files"] if f.get("mtime") is None or now - f["mtime"] >= p.seconds]
    return groups


class LatestParams(P):
    n: int = Field(default=1, ge=1)
    by: Literal["mtime", "path"] = "mtime"


def op_latest(groups: list[dict], p: LatestParams, ctx: dict, src: SourceView) -> list[dict]:
    for g in groups:
        key = (lambda f: (f.get("mtime") or 0, f["path"])) if p.by == "mtime" else (lambda f: f["path"])
        g["files"] = sorted(g["files"], key=key)[-p.n:]
    return groups


class LimitsParams(P):
    min_files: int = Field(default=1, ge=0)
    max_files: int | None = Field(default=None, ge=1)
    max_total_bytes: int | None = Field(default=None, ge=1)


def op_limits(groups: list[dict], p: LimitsParams, ctx: dict, src: SourceView) -> list[dict]:
    for g in groups:
        n, total = len(g["files"]), sum(f.get("size") or 0 for f in g["files"])
        if n < p.min_files:
            raise PackageError(f"found {n} matching file(s) (min_files {p.min_files})")
        if p.max_files and n > p.max_files:
            raise PackageError(f"found {n} matching files (max_files {p.max_files})")
        if p.max_total_bytes and total > p.max_total_bytes:
            raise PackageError(f"matching files total {total} bytes (max_total_bytes {p.max_total_bytes})")
    return groups


class ControlFileParams(P):
    match: str = Field(description="glob of the control file within the group's files, e.g. '*/CONTROL.csv'")
    format: Literal["lines", "csv", "json"] = Field(default="lines", description="lines: one path per line; csv: a column; json: a list of paths")
    column: str | None = Field(default=None, description="csv column holding the paths")
    relative_to: Literal["control_folder", "root"] = "control_folder"
    keep_control: bool = True
    require_all: bool = Field(default=True, description="every listed file must exist")


def op_control_file(groups: list[dict], p: ControlFileParams, ctx: dict, src: SourceView) -> list[dict]:
    """Restrict a group to the files its control/manifest file lists (reads that small file only)."""
    for g in groups:
        controls = [f for f in g["files"] if matches_any([p.match], f["path"])]
        if len(controls) != 1:
            raise PackageError(f"expected exactly one control file matching '{p.match}', found {len(controls)}")
        c = controls[0]
        text = src.read_text(c["root"], c["path"])
        if p.format == "lines":
            names = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        elif p.format == "csv":
            rows = list(csv.DictReader(io.StringIO(text)))
            if not p.column or (rows and p.column not in rows[0]):
                raise PackageError(f"control file has no column '{p.column}'")
            names = [r[p.column].strip() for r in rows if r.get(p.column, "").strip()]
        else:
            try:
                names = json.loads(text)
            except json.JSONDecodeError:
                raise PackageError("control file is not JSON") from None
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise PackageError("control file must be a JSON list of paths")
        base = posixpath.dirname(c["path"]) if p.relative_to == "control_folder" else ""
        wanted = {safe_relpath(posixpath.join(base, n)) for n in names}
        have = {f["path"]: f for f in g["files"]}
        missing = sorted(wanted - set(have))
        if missing and p.require_all:
            raise PackageError(f"{len(missing)} file(s) listed in the control file are missing, e.g. '{missing[0]}'")
        g["files"] = [have[w] for w in sorted(wanted) if w in have] + ([c] if p.keep_control else [])
    return groups


class SplitParams(P):
    by: Literal["folder", "regex"] = "folder"
    depth: int = Field(default=1, ge=1, description="folder: split on the first N folder levels")
    regex: str | None = Field(default=None, description="regex: the first group (or named group 'key') is the package key")
    var: str = Field(default="part", description="variable holding the split key (for package names and prefixes)")
    _v = field_validator("regex")(classmethod(lambda cls, v: _rx(v)))


def op_split(groups: list[dict], p: SplitParams, ctx: dict, src: SourceView) -> list[dict]:
    """One package per folder (or regex key) instead of one per event."""
    out = []
    for g in groups:
        parts: dict[str, list] = {}
        for f in g["files"]:
            if p.by == "folder":
                segs = f["path"].split("/")
                if len(segs) <= p.depth:
                    raise PackageError(f"'{f['path']}' has fewer than {p.depth} folder level(s) to split on")
                key = "/".join(segs[:p.depth])
            else:
                m = re.search(p.regex or "", f["path"])
                if not m:
                    raise PackageError(f"'{f['path']}' does not match the split regex")
                key = m.groupdict().get("key") or (m.group(1) if m.groups() else m.group(0))
            parts.setdefault(key, []).append(f)
        for key in sorted(parts):
            out.append({**g, "id": f"{g['id']}/{key}", "vars": {**g.get("vars", {}), p.var: key}, "files": parts[key]})
    return out


class CustomParams(P):
    function: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")


SOURCE_OPS: dict[str, Op] = {o.name: o for o in [
    Op("list", ListParams, op_list, "Add every file under a folder/prefix (template), recursively up to max_depth; paths relative to it."),
    Op("from_event", FromEventParams, op_from_event, "Add exactly the objects named by the event (keys must be under root; root is removed from paths)."),
    Op("select", SelectParams, op_select, "Keep files matching include globs (and regex), minus exclude globs."),
    Op("min_age", MinAgeParams, op_min_age, "Skip files modified less than N seconds ago (still being written)."),
    Op("latest", LatestParams, op_latest, "Keep only the newest N files (by modification time or path)."),
    Op("limits", LimitsParams, op_limits, "Fail the package when it has too few/many files or bytes."),
    Op("control_file", ControlFileParams, op_control_file, "Keep only the files a control/manifest file lists (reads that small file only)."),
    Op("split", SplitParams, op_split, "One package per folder or per regex key instead of one per event; sets a variable."),
    Op("custom", CustomParams, None, "Generated, reviewed function: (groups, params, ctx, src) -> groups; src has list/stat/read_text."),
]}


def provides(steps: list[dict]) -> set[str]:
    return {s["params"].get("var", "part") for s in steps if s["op"] == "split"}


def initial_groups(events: list[dict]) -> list[dict]:
    return [{"id": e["id"], "event_id": e["id"], "message_ids": e.get("message_ids", []), "vars": dict(e.get("vars", {})),
             "objects": e.get("objects", []), "files": []} for e in events if not e.get("error")]


def run_source_steps(groups: list[dict], steps: list[dict], ctx: dict, src: SourceView, custom: dict | None = None) -> list[dict]:
    from .ops import parse_params_for

    for s in steps:
        model = parse_params_for(SOURCE_OPS, s["op"], s["params"])
        if s["op"] == "custom":
            fn = (custom or {}).get(model.function)
            if fn is None:
                raise RuntimeError(f"custom step function '{model.function}' is not implemented")
            try:
                groups = fn(json.loads(json.dumps(groups)), dict(ctx.get("params", {})), dict(ctx), src)
            except (PermissionError, PackageError):
                raise
            except Exception as exc:  # noqa: BLE001 - generated code refusing (as its API says) or failing: this event only
                raise PackageError(f"custom step '{s['id']}': {type(exc).__name__}: {exc}") from None
            if not isinstance(groups, list) or not all(isinstance(g, dict) and isinstance(g.get("files"), list) for g in groups):
                raise PackageError(f"custom step '{s['id']}' must return a list of groups")
        else:
            groups = SOURCE_OPS[s["op"]].run(groups, model, ctx, src)
    for g in groups:
        _normalise(g)
    return groups


def _normalise(g: dict) -> None:
    """Per file (B4): a path that would escape the package, and both files of a duplicate package path, move to
    g["rejected"] ([{"path", "key", "error"}], K13) instead of refusing the whole event; the other files go on."""
    rejected = list(g.get("rejected") or [])
    by_path: dict[str, list[dict]] = {}
    for f in g["files"]:
        try:
            p = safe_relpath(f["path"])
        except PackageError as exc:
            rejected.append({"path": f["path"], "key": f.get("key"), "error": str(exc)})
            continue
        by_path.setdefault(p, []).append({**f, "path": p})
    files = []
    for p, fs in by_path.items():
        if len(fs) == 1:
            files.append(fs[0])
            continue
        for f in fs:
            others = [x.get("key") or x["path"] for x in fs if x is not f]
            rejected.append({"path": p, "key": f.get("key"),
                             "error": f"two files would have the package path '{p}' (also {', '.join(others[:3])})"})
    g["files"] = sorted(files, key=lambda f: f["path"])
    if rejected:
        g["rejected"] = rejected


def default_steps(extraction: dict) -> list[dict]:
    """Steps equivalent to a source's plain `extraction` settings (no requirement, no AI)."""
    ex = extraction or {}
    steps = [{"id": "list_root", "op": "list", "title": "List the extraction root",
              "params": {"root": ex.get("root", "/"), "recursive": ex.get("recursive", True), "max_depth": ex.get("max_depth", 20)}},
             {"id": "select_files", "op": "select", "title": "Keep matching files",
              "params": {"include": ex.get("include") or ["**"], "exclude": ex.get("exclude") or [],
                         **({"regex": ex["regex"]} if ex.get("regex") else {})}}]
    if ex.get("min_age_seconds"):
        steps.append({"id": "settled_only", "op": "min_age", "title": "Skip files still being written", "params": {"seconds": ex["min_age_seconds"]}})
    steps.append({"id": "check_limits", "op": "limits", "title": "Check file count and size",
                  "params": {"min_files": ex.get("min_files", 1), **{k: ex[k] for k in ("max_files", "max_total_bytes") if ex.get(k)}}})
    return steps


def normalize(groups: list[dict]) -> list[list[str]]:
    return sorted(sorted(f["path"] for f in g["files"]) for g in groups)
