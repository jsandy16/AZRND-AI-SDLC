"""The catalogue of deterministic package operations transformations are built from.

Each operation = a pydantic parameter model + an implementation working on a `Package`. The same table drives the
AI's planning prompt (names, parameters, semantics), the plan simulator, the generated transformation code and the
runtime. Anything the catalogue cannot express becomes a `custom` step with reviewed generated code.

Templates in parameters may use the run variables ({ds_nodash}, {source}, {package}, pipeline params …) and, where
noted, per-file variables: {path} {dir} {name} {stem} {suffix} and the named groups of a `match` regex.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .package import Package, PackageError, StepPackage, matches_any, posix_parts

HASHES = ("md5", "sha1", "sha256", "sha512")


class P(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _rx(v: str | None) -> str | None:
    if v is not None:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from None
    return v


def _file_vars(rel: str) -> dict[str, str]:
    d, name, stem, suffix = posix_parts(rel)
    return {"path": rel, "dir": d, "name": name, "stem": stem, "suffix": suffix}


def _join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


# ---------------------------------------------------------------------------------------------- select / require
class SelectParams(P):
    include: list[str] = Field(default_factory=lambda: ["**"], description="globs to keep")
    exclude: list[str] = Field(default_factory=list, description="globs to drop (applied after include)")
    regex: str | None = Field(default=None, description="optional regex the path must also fully match")
    _v = field_validator("regex")(classmethod(lambda cls, v: _rx(v)))


def op_select(pkg: Package, p: SelectParams, ctx: dict) -> None:
    for f in pkg.files():
        keep = matches_any(p.include, f) and not matches_any(p.exclude, f) and (p.regex is None or re.fullmatch(p.regex, f))
        if not keep:
            pkg.remove(f)


class RequireParams(P):
    patterns: list[str] = Field(default_factory=list, description="globs that must each match at least one file")
    forbid: list[str] = Field(default_factory=list, description="globs no file may match")
    folder_regex: str | None = Field(default=None, description="every file's folder ('' at the root) must fully match")
    name_regex: str | None = Field(default=None, description="every file name must fully match")
    min_files: int = Field(default=1, ge=0)
    max_files: int | None = Field(default=None, ge=1)
    max_total_bytes: int | None = Field(default=None, ge=1)
    _v = field_validator("folder_regex", "name_regex")(classmethod(lambda cls, v: _rx(v)))


def op_require(pkg: Package, p: RequireParams, ctx: dict) -> None:
    files = pkg.files()
    problems = []
    if len(files) < p.min_files or (p.max_files is not None and len(files) > p.max_files):
        problems.append(f"{len(files)} files (expected {p.min_files}..{p.max_files or 'any'})")
    for g in p.patterns:
        if not any(matches_any([g], f) for f in files):
            problems.append(f"no file matches '{g}'")
    bad = [f for f in files if matches_any(p.forbid, f)]
    if bad:
        problems.append(f"{len(bad)} forbidden file(s), e.g. '{bad[0]}'")
    if p.folder_regex is not None:
        bad = [f for f in files if not re.fullmatch(p.folder_regex, posix_parts(f)[0])]
        if bad:
            problems.append(f"{len(bad)} file(s) in an unexpected folder, e.g. '{bad[0]}'")
    if p.name_regex is not None:
        bad = [f for f in files if not re.fullmatch(p.name_regex, posix_parts(f)[1])]
        if bad:
            problems.append(f"{len(bad)} file name(s) not matching, e.g. '{bad[0]}'")
    if p.max_total_bytes is not None and pkg.total_bytes() > p.max_total_bytes:
        problems.append(f"package is {pkg.total_bytes()} bytes (limit {p.max_total_bytes})")
    if problems:
        raise PackageError("structure check failed: " + "; ".join(problems))


# ---------------------------------------------------------------------------------------------- restructure
class MoveParams(P):
    match: str = Field(description="regex fully matched against each path; named groups become template variables")
    to: str = Field(description="new path template, e.g. '{site}/{name}' (file vars: path dir name stem suffix)")
    on_conflict: Literal["fail", "overwrite"] = "fail"
    _v = field_validator("match")(classmethod(lambda cls, v: _rx(v)))


def op_move(pkg: Package, p: MoveParams, ctx: dict) -> None:
    plan = []
    for f in pkg.files():
        m = re.fullmatch(p.match, f)
        if m:
            plan.append((f, pkg.render(p.to, {**_file_vars(f), **{k: v for k, v in m.groupdict().items() if v is not None}})))
    targets = [t for _, t in plan]
    if len(targets) != len(set(targets)):
        raise PackageError("move would put two files at the same path")
    tmp = [(src, f".pkf_move_{i}/{posix_parts(src)[1]}") for i, (src, _) in enumerate(plan)]
    for (src, t) in tmp:  # two-phase so moves never collide with each other
        pkg.move(src, t)
    for (_, t), (_, dst) in zip(tmp, plan):
        pkg.move(t, dst, overwrite=p.on_conflict == "overwrite")


class FlattenParams(P):
    include: list[str] = Field(default_factory=lambda: ["**"])


def op_flatten(pkg: Package, p: FlattenParams, ctx: dict) -> None:
    chosen = [f for f in pkg.files() if matches_any(p.include, f) and "/" in f]
    names = [posix_parts(f)[1] for f in chosen]
    clash = sorted({n for n in names if names.count(n) > 1 or (pkg.exists(n))})
    if clash:
        raise PackageError(f"flatten would put two files at the same path, e.g. '{clash[0]}'")
    for f, n in zip(chosen, names):
        pkg.move(f, n)


class PrefixParams(P):
    folder: str = Field(description="folder template to put every file under, e.g. '{package}', 'data/{ds}' or "
                                    "'{input_path}'; leading and trailing '/' are removed ('/a/b' -> 'a/b/<file>')")


def op_prefix(pkg: Package, p: PrefixParams, ctx: dict) -> None:
    folder = pkg.render(p.folder).strip("/")
    if folder:
        files = pkg.files()
        for f in files:  # temporary names first, so an existing folder of the same name never collides
            pkg.move(f, f".pkf_prefix/{f}")
        for f in files:
            pkg.move(f".pkf_prefix/{f}", f"{folder}/{f}")


class StripFoldersParams(P):
    levels: int = Field(default=1, ge=1, description="leading folder levels to remove from every path")


def op_strip_folders(pkg: Package, p: StripFoldersParams, ctx: dict) -> None:
    for f in pkg.files():
        parts = f.split("/")
        if len(parts) <= p.levels:
            raise PackageError(f"'{f}' has fewer than {p.levels} folder level(s) to strip")
    op_move(pkg, MoveParams(match=r"(?:[^/]+/){%d}(?P<__rest>.+)" % p.levels, to="{__rest}"), ctx)


class RenamePackageParams(P):
    name: str = Field(description="new package name template, e.g. 'CRO_{ds_nodash}' (used as {package})")


def op_rename_package(pkg: Package, p: RenamePackageParams, ctx: dict) -> None:
    pkg.name = pkg.render(p.name)


# ---------------------------------------------------------------------------------------------- integrity
class ChecksumParams(P):
    algorithms: list[Literal["md5", "sha1", "sha256", "sha512"]] = Field(default_factory=lambda: ["sha256"], min_length=1)
    include: list[str] = Field(default_factory=lambda: ["**"])
    sidecar: bool = Field(default=False, description="write a sidecar file per file and algorithm ('<digest>  <name>')")
    sidecar_name: str = Field(default="{name}.{algorithm}", description="sidecar path template, next to the file")


def op_checksum(pkg: Package, p: ChecksumParams, ctx: dict) -> None:
    for f in pkg.files():
        if not matches_any(p.include, f):
            continue
        for alg in p.algorithms:
            digest = pkg.checksum(f, alg)
            if p.sidecar:
                d, name, *_ = posix_parts(f)
                side = _join(d, pkg.render(p.sidecar_name, {**_file_vars(f), "algorithm": alg}))
                pkg.write_bytes(side, f"{digest}  {name}\n".encode("utf-8"))


class ManifestParams(P):
    path: str = Field(default="manifest.json", description="manifest path template")
    format: Literal["json", "csv"] = "json"
    fields: list[Literal["path", "size", "md5", "sha1", "sha256", "sha512"]] = Field(
        default_factory=lambda: ["path", "size", "sha256"], min_length=1)
    include: list[str] = Field(default_factory=lambda: ["**"])
    package_fields: bool = Field(default=True, description="json only: add package name, file count and total bytes")


def op_manifest(pkg: Package, p: ManifestParams, ctx: dict) -> None:
    target = pkg.render(p.path)
    rows, listed = [], []
    for f in pkg.files():
        if f == target or not matches_any(p.include, f):
            continue
        listed.append(f)
        row: dict[str, Any] = {}
        for fld in p.fields:
            row[fld] = f if fld == "path" else pkg.size(f) if fld == "size" else pkg.checksum(f, fld)
        rows.append(row)
    if p.format == "json":
        doc: Any = {"package": pkg.name, "file_count": len(rows), "total_bytes": sum(pkg.size(f) for f in listed),
                    "files": rows} if p.package_fields else rows
        pkg.write_bytes(target, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    else:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(p.fields), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        pkg.write_bytes(target, buf.getvalue().encode("utf-8"))


# ---------------------------------------------------------------------------------------------- archives
class ZipParams(P):
    name: str = Field(description="archive path template, e.g. '{package}.zip'")
    include: list[str] = Field(default_factory=lambda: ["**"])
    exclude: list[str] = Field(default_factory=list)
    folder_in_archive: str = Field(default="", description="template for a top folder inside the archive ('' = none)")
    flatten: bool = Field(default=False, description="store files by name only (duplicates fail)")
    remove_originals: bool = True
    compression: Literal["deflated", "stored"] = "deflated"
    allow_empty: bool = False


def op_zip(pkg: Package, p: ZipParams, ctx: dict) -> None:
    target = pkg.render(p.name)
    members = [f for f in pkg.files() if matches_any(p.include, f) and not matches_any(p.exclude, f) and f != target]
    if not members and not p.allow_empty:
        raise PackageError(f"nothing to zip into '{target}'")
    top = pkg.render(p.folder_in_archive).strip("/") if p.folder_in_archive else ""
    tmp = pkg.new_temp_path(".zip")
    names = set()
    comp = zipfile.ZIP_DEFLATED if p.compression == "deflated" else zipfile.ZIP_STORED
    with zipfile.ZipFile(tmp, "w", compression=comp) as zf:
        for f in members:
            arc = _join(top, posix_parts(f)[1] if p.flatten else f)
            if arc in names:
                raise PackageError(f"two files would be stored as '{arc}' in the archive")
            names.add(arc)
            info = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))  # reproducible archives
            info.compress_type = comp
            info.external_attr = 0o644 << 16
            with open(pkg.path(f), "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    if p.remove_originals:
        for f in members:
            pkg.remove(f)
    pkg.add_local_file(target, tmp)


class UnzipParams(P):
    include: list[str] = Field(default_factory=lambda: ["**/*.zip"])
    into: str = Field(default="{dir}/{stem}", description="folder template for the members (file vars available); '{dir}' = beside the archive")
    remove_archive: bool = True
    max_members: int = Field(default=10000, ge=1)
    max_total_bytes: int = Field(default=20 * 1024**3, ge=1)
    max_ratio: int = Field(default=200, ge=1, description="largest uncompressed/compressed ratio (zip-bomb guard)")


def op_unzip(pkg: Package, p: UnzipParams, ctx: dict) -> None:
    for f in [x for x in pkg.files() if matches_any(p.include, x)]:
        dest = pkg.render(p.into, _file_vars(f)).strip("/")
        try:
            zf = zipfile.ZipFile(pkg.path(f))
        except zipfile.BadZipFile:
            raise PackageError(f"'{f}' is not a valid zip archive") from None
        with zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > p.max_members:
                raise PackageError(f"'{f}' has {len(infos)} members (limit {p.max_members})")
            total = 0
            for i in infos:
                if i.flag_bits & 0x1:
                    raise PackageError(f"'{f}' is encrypted")
                if (i.external_attr >> 16) & 0o170000 == 0o120000:
                    raise PackageError(f"'{f}' contains a symbolic link")
                rel = _join(dest, i.filename)
                target = pkg._target(rel, overwrite=False)  # safe_relpath refuses '..' and absolute names
                written, limit = 0, max(i.compress_size, 1) * p.max_ratio
                try:
                    with zf.open(i) as src, open(target, "wb") as dst:
                        for chunk in iter(lambda: src.read(1 << 20), b""):
                            written += len(chunk)
                            total += len(chunk)
                            if total > p.max_total_bytes or (written > limit and written > (1 << 20)):
                                raise PackageError(f"'{f}' expands beyond the allowed size or ratio (zip-bomb guard)")
                            dst.write(chunk)
                except (zipfile.BadZipFile, EOFError, OSError, NotImplementedError) as exc:
                    raise PackageError(f"'{f}' could not be extracted ({type(exc).__name__})") from None
        if p.remove_archive:
            pkg.remove(f)


class GzipParams(P):
    include: list[str] = Field(default_factory=lambda: ["**"])
    exclude: list[str] = Field(default_factory=lambda: ["**/*.gz"])
    remove_originals: bool = True


def op_gzip(pkg: Package, p: GzipParams, ctx: dict) -> None:
    for f in [x for x in pkg.files() if matches_any(p.include, x) and not matches_any(p.exclude, x)]:
        tmp = pkg.new_temp_path(".gz")
        with open(pkg.path(f), "rb") as src, open(tmp, "wb") as raw, \
                gzip.GzipFile(filename=posix_parts(f)[1], mode="wb", fileobj=raw, mtime=0) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        if p.remove_originals:
            pkg.remove(f)
        pkg.add_local_file(f + ".gz", tmp)


class GunzipParams(P):
    include: list[str] = Field(default_factory=lambda: ["**/*.gz"])
    remove_archive: bool = True
    max_total_bytes: int = Field(default=20 * 1024**3, ge=1)


def op_gunzip(pkg: Package, p: GunzipParams, ctx: dict) -> None:
    for f in [x for x in pkg.files() if matches_any(p.include, x)]:
        out = f[:-3] if f.lower().endswith(".gz") else f + ".out"
        tmp = pkg.new_temp_path()
        total = 0
        try:
            with gzip.open(pkg.path(f), "rb") as src, open(tmp, "wb") as dst:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    total += len(chunk)
                    if total > p.max_total_bytes:
                        raise PackageError(f"'{f}' expands beyond max_total_bytes")
                    dst.write(chunk)
        except (OSError, EOFError) as exc:
            raise PackageError(f"'{f}' is not a valid gzip file ({type(exc).__name__})") from None
        if p.remove_archive:
            pkg.remove(f)
        pkg.add_local_file(out, tmp)


class CustomParams(P):
    function: str = Field(pattern=r"^[a-z_][a-z0-9_]*$", description="name of the generated function implementing this step")


# ---------------------------------------------------------------------------------------------- registry
@dataclass(frozen=True)
class Op:
    name: str
    params: type[P]
    run: Callable[[Package, Any, dict], None] | None
    summary: str


OPS: dict[str, Op] = {o.name: o for o in [
    Op("select", SelectParams, op_select, "Keep only files matching include globs (and regex), minus exclude globs; drop the rest."),
    Op("require", RequireParams, op_require, "Check structure: required patterns, forbidden patterns, folder/name regex, file count and size limits. Fails the run when violated."),
    Op("move", MoveParams, op_move, "Restructure/rename: every path fully matching `match` moves to the `to` template (regex groups and file vars available)."),
    Op("flatten", FlattenParams, op_flatten, "Move files to the package root by name (name clashes fail)."),
    Op("prefix", PrefixParams, op_prefix, "Put every file under a folder (template), keeping its sub-folders; leading/trailing '/' of the rendered folder are removed."),
    Op("strip_folders", StripFoldersParams, op_strip_folders, "Remove the first N folder levels from every path."),
    Op("rename_package", RenamePackageParams, op_rename_package, "Set the package name (template); later templates see it as {package}."),
    Op("checksum", ChecksumParams, op_checksum, "Compute md5/sha1/sha256/sha512 per file (remembered for manifests); optionally write sidecar files."),
    Op("manifest", ManifestParams, op_manifest, "Write a JSON or CSV manifest listing files with size and checksums."),
    Op("zip", ZipParams, op_zip, "Bundle matching files into one zip (template name), keeping paths (or flattened, or under a top folder); removes originals by default."),
    Op("unzip", UnzipParams, op_unzip, "Extract matching zip archives into a folder (template), with zip-bomb and path-traversal guards."),
    Op("gzip", GzipParams, op_gzip, "Compress matching files individually to .gz."),
    Op("gunzip", GunzipParams, op_gunzip, "Decompress matching .gz files."),
    Op("custom", CustomParams, None, "Anything the catalogue cannot express: a generated, reviewed Python function working through the package API."),
]}


def parse_params_for(ops: dict[str, Op], op: str, params: dict) -> P:
    if op not in ops:
        raise ValueError(f"unknown operation '{op}' (catalogue: {sorted(ops)})")
    return ops[op].params.model_validate(params)


def parse_params(op: str, params: dict) -> P:
    return parse_params_for(OPS, op, params)


def run_step(pkg: Package, op: str, params: dict, ctx: dict, custom: dict[str, Callable] | None = None) -> None:
    model = parse_params(op, params)
    if op == "custom":
        fn = (custom or {}).get(model.function)
        if fn is None:
            raise RuntimeError(f"custom step function '{model.function}' is not implemented")
        try:
            fn(StepPackage(pkg), dict(ctx.get("params", {})), dict(ctx))
        except PackageError:
            raise
        except Exception as exc:  # noqa: BLE001 - generated code: the package is refused, not retried
            raise PackageError(f"custom step '{model.function}': {type(exc).__name__}: {exc}") from None
        return
    OPS[op].run(pkg, model, ctx)


def catalogue(ops: dict[str, Op] | None = None) -> list[dict]:
    """Machine-readable catalogue for prompts, docs and `pkf catalog`."""
    out = []
    for o in (ops or OPS).values():
        props = {}
        for n, f in o.params.model_fields.items():
            if n.startswith("_"):
                continue
            props[n] = {"type": str(f.annotation).replace("typing.", ""), "default": None if f.is_required() else f.get_default(call_default_factory=True),
                        "required": f.is_required(), "description": f.description or ""}
        out.append({"op": o.name, "summary": o.summary, "params": props})
    return out
