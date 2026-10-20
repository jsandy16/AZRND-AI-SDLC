"""A test package for S6 (0.8.2): when the 6.3 preview finds nothing to move, the Studio builds the objects of one of
the pipeline's own end-to-end examples and places them in the pipeline's dev source — or, when it cannot, writes the
exact commands for you to do it.

Only Studio code writes here (never the assistant): dev mode only; only the bucket of the pipeline's own source spec;
only the keys of an example (no '..', no leading '/', under the source's root when it can be resolved); never an
overwrite (S3 conditional write `IfNoneMatch="*"`, an existing key is kept and counted); never a delete; at most
MAX_OBJECTS objects and MAX_BYTES bytes; every object tagged `pkf-test-data=1`. The files and `upload.sh` stay under
`.pkf-studio/flows/<pipeline>/test-data/<stamp>/` (git-ignored)."""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import shlex
import zipfile
from pathlib import Path

from . import flowstore
from . import ledger as L

MAX_OBJECTS = 100
MAX_BYTES = 20 * 1024 * 1024
MAX_PLACEHOLDER = 1024 * 1024            # a listed size above this is capped (the content is never read by a move)
DEFAULT_SIZE = 64
LISTING_ITEM = re.compile(r"(.*?)(?::(\d+))?(?::(\d+(?:\.\d+)?))?$", re.S)
TAG = "pkf-test-data=1"


class TestDataError(Exception):
    pass


def _yaml(read, rel: str) -> dict:
    import yaml

    try:
        doc = yaml.safe_load(read(rel) or "") or {}
    except Exception:  # noqa: BLE001 - a broken file is "no data" here; validation reports it elsewhere
        return {}
    return doc if isinstance(doc, dict) else {}


def _source(read, p: str) -> tuple[str | None, dict]:
    for kind, name in L.components(read, p):
        if kind == "source":
            return name, _yaml(read, f"{L.FOLDERS[kind]}/{name}.yaml")
    return None, {}


def _resolve(template: str, params: dict) -> str | None:
    """`{name}` templates filled from params; None when one is left (then the root is not checked)."""
    out = str(template or "")
    for k, v in params.items():
        out = out.replace("{" + str(k) + "}", str(v))
    return None if "{" in out else out


def plan(read, p: str, mode: str = "dev") -> dict:
    """What the test package would be, from the pipeline's examples (pure; `read(rel) -> text|None`):
    {ok, problem, example, bucket, region, endpoint_url, root, min_age_s, objects: [{key, size, text?}], bytes}."""
    out: dict = {"ok": False, "problem": "", "example": None, "bucket": None, "region": None, "endpoint_url": None,
                 "root": None, "min_age_s": 0, "objects": [], "bytes": 0}
    if mode != "dev":
        out["problem"] = f"a test package is placed only in dev (this repository is in {mode})"
        return out
    pipe = _yaml(read, f"pipelines/{p}.yaml")
    name, src = _source(read, p)
    if not name:
        out["problem"] = "the pipeline has no source component"
        return out
    if src.get("type") != "s3":
        out["problem"] = f"the source {name} is {src.get('type') or 'not s3'}: only an S3 source can get a test package"
        return out
    conn = src.get("connection") or {}
    bucket = str(conn.get("bucket") or "").strip()
    if not bucket or "<REQUIRED>" in bucket or "{" in bucket:
        out["problem"] = f"the source {name} has no concrete bucket (sources/{name}.yaml → connection.bucket)"
        return out
    out.update(bucket=bucket, region=conn.get("region"), endpoint_url=conn.get("endpoint_url"))
    ext = src.get("extraction") or {}
    params = {**(src.get("params") or {}), **(pipe.get("params") or {})}
    root = _resolve(ext.get("root") or "", params) if ext.get("root") else None
    out["root"] = root.lstrip("/") if root else None
    out["min_age_s"] = min_age(read, name, src)
    trig = next((nm for k, nm in L.components(read, p) if k == "trigger"), None)
    if trig:
        tq = str(((_yaml(read, f"triggers/{trig}.yaml").get("params") or {}).get("queue_url")) or "")
        if tq and "<REQUIRED>" not in tq:
            out["problem"] = (f"the trigger {trig} reads an SQS queue: the preview does not list the bucket, so a test package "
                              "would not be seen — send a test message instead")
            return out
    defaults = {str(k): str(v) for k, v in params.items()}
    usable = [e for e in (pipe.get("examples") or []) if isinstance(e, dict) and not e.get("expect_error")
              and e.get("listing") and (e.get("expected_keys") is None or e.get("expected_keys"))]
    # the live preview runs with the spec's default parameters and today's date: only an example that needs nothing else
    plain = [e for e in usable if all(defaults.get(str(k)) == str(v) for k, v in (e.get("params") or {}).items())
             and not any("{" in str(x) for x in e.get("listing") or [])]
    if not plain:
        out["problem"] = (f"pipelines/{p}.yaml has no end-to-end example that a run with the default parameters would find "
                          "(an example with a listing, no expect_error, no special params) — add one, or place a file yourself"
                          if usable else
                          f"pipelines/{p}.yaml has no end-to-end example with a listing that should deliver files — add one "
                          "(listing: the source keys), or place a file yourself")
        return out
    ex = plain[0]
    out["example"] = ex.get("name")
    contents = {str(k): str(v) for k, v in (ex.get("contents") or {}).items()}
    objects, total = [], 0
    for item in ex.get("listing") or []:
        m = LISTING_ITEM.match(str(item))
        key = (m.group(1) if m else str(item)).strip()
        if not key or key.endswith("/"):
            continue                                     # a folder marker: S3 has no folders
        if key.startswith("/") or ".." in key.split("/"):
            out["problem"] = f"example {ex.get('name')}: {key!r} is not a plain key"
            return out
        if out["root"] and not key.startswith(out["root"].rstrip("/") + "/") and key != out["root"]:
            out["problem"] = f"example {ex.get('name')}: {key!r} is not under the source root {out['root']!r}"
            return out
        age = float(m.group(3)) if m and m.group(3) else None
        if age is not None and out["min_age_s"] and age < out["min_age_s"]:
            continue                                     # the example shows a file still being written: the run skips it
        size = int(m.group(2)) if m and m.group(2) else DEFAULT_SIZE
        obj = {"key": key, "size": min(size, MAX_PLACEHOLDER)}
        if key in contents:
            obj["text"] = contents[key]
            obj["size"] = len(contents[key].encode("utf-8"))
        objects.append(obj)
        total += obj["size"]
    if not objects:
        out["problem"] = f"example {ex.get('name')} lists no file"
        return out
    if len(objects) > MAX_OBJECTS or total > MAX_BYTES:
        out["problem"] = f"example {ex.get('name')} is too big for a test package ({len(objects)} objects, {total} bytes)"
        return out
    out.update(ok=True, objects=objects, bytes=total)
    return out


def min_age(read, name: str, src: dict) -> int:
    """The source's minimum file age: extraction.min_age_seconds, or a planned `min_age` step (its params.seconds)."""
    best = 0
    try:
        best = int(float((src.get("extraction") or {}).get("min_age_seconds") or 0))
    except (TypeError, ValueError):
        pass
    lock = _yaml(read, f"sources/{name}.lock.yaml")
    for step in ((lock.get("plan") or {}).get("steps") or []):
        if isinstance(step, dict) and step.get("op") == "min_age":
            try:
                best = max(best, int(float((step.get("params") or {}).get("seconds") or 0)))
            except (TypeError, ValueError):
                pass
    return best


def body(obj: dict) -> bytes:
    """The bytes of one test object: the example's text, a small valid zip for .zip, else placeholder bytes."""
    if "text" in obj:
        return obj["text"].encode("utf-8")
    key = obj["key"]
    if key.lower().endswith(".zip"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("pkf-test-data.txt", f"pkf test package: {key}\n")
        return buf.getvalue()
    head = f"pkf test package: {key}\n".encode("utf-8")
    n = max(int(obj.get("size") or DEFAULT_SIZE), len(head))
    return head + b"." * (n - len(head))


def commands(pl: dict, folder: Path) -> list[str]:
    """`aws s3 cp` commands placing the written files (what you run when the Studio cannot)."""
    region = f" --region {shlex.quote(str(pl['region']))}" if pl.get("region") else ""
    endpoint = f" --endpoint-url {shlex.quote(str(pl['endpoint_url']))}" if pl.get("endpoint_url") else ""
    out = []
    for o in pl["objects"]:
        src = (folder / "files" / o["key"]).as_posix()
        b, k = shlex.quote(pl["bucket"]), shlex.quote(o["key"])
        out.append(f"aws s3api head-object --bucket {b} --key {k}{region}{endpoint} >/dev/null 2>&1 && echo \"kept (exists): {o['key']}\" "
                   f"|| aws s3api put-object --bucket {b} --key {k} --body {shlex.quote(src)} --tagging {TAG}{region}{endpoint}")
    return out


def write_local(root: Path, p: str, pl: dict, stamp: str | None = None) -> Path:
    """The package's files, upload.sh and manifest.json under .pkf-studio/flows/<p>/test-data/<stamp>/."""
    stamp = stamp or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    folder = flowstore.flow_dir(root, p) / "test-data" / stamp
    for o in pl["objects"]:
        f = folder / "files" / o["key"]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body(o))
    lines = ["#!/usr/bin/env bash", f"# pkf test package for {p}: example {pl.get('example')} → s3://{pl['bucket']}/",
             "# Run in Git Bash with your AWS credentials (aws sso login first). A key that already exists is kept.",
             "set -e", *commands(pl, folder), 'echo "done: now Preview again at 6.3"']
    (folder / "upload.sh").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    (folder / "manifest.json").write_text(json.dumps({k: v for k, v in pl.items() if k != "objects"} | {
        "objects": [{"key": o["key"], "size": o["size"]} for o in pl["objects"]], "stamp": stamp}, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    return folder


def upload(pl: dict, folder: Path, client=None) -> dict:
    """Put each object that is not there yet (never overwrite). {placed: [...], existing: [...]}."""
    import botocore.exceptions

    if client is None:
        import boto3

        client = boto3.client("s3", region_name=pl.get("region") or None, endpoint_url=pl.get("endpoint_url") or None)
    placed, existing = [], []
    tagging = {"Tagging": TAG}
    for o in pl["objects"]:
        data = (folder / "files" / o["key"]).read_bytes()
        for _ in range(2):
            try:
                client.put_object(Bucket=pl["bucket"], Key=o["key"], Body=data, IfNoneMatch="*", **tagging)
                placed.append(o["key"])
                break
            except botocore.exceptions.ClientError as exc:
                code = str((exc.response.get("Error") or {}).get("Code") or "")
                if code in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                    existing.append(o["key"])
                    break
                if code == "AccessDenied" and tagging:     # no s3:PutObjectTagging: place it untagged
                    tagging = {}
                    continue
                raise TestDataError(f"S3 refused {o['key']}: {code or exc}") from None
    return {"placed": placed, "existing": existing, "tagged": bool(tagging)}


def latest(root: Path, p: str) -> dict | None:
    """The last test package written for the pipeline (its manifest + folder), or None."""
    d = flowstore.flow_dir(root, p) / "test-data"
    if not d.is_dir():
        return None
    for f in sorted(d.iterdir(), reverse=True):
        m = f / "manifest.json"
        if m.is_file():
            try:
                doc = json.loads(m.read_text(encoding="utf-8"))
            except ValueError:
                continue
            doc["folder"] = str(f)
            doc["upload_sh"] = str(f / "upload.sh")
            return doc
    return None
