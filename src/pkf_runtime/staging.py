"""Run-scoped staging: how a package travels between Airflow tasks (which may run on different workers).

Layout: <staging url>/<pipeline>/<run id>/<NN_stage>/{package.json, files/...}
Each task reads the previous stage and writes its own; writing first clears the stage, so a retried task
starts clean. URLs: s3://bucket/prefix (production) or file:///path (development, single machine).
The environment variable PKF_STAGING_URL overrides the built-in URL (e.g. on a developer laptop).
On Windows a file URL needs its drive: file:///C:/Users/me/pkf-staging (the Git Bash spelling /c/Users/... is refused).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname


def safe_run_id(run_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", run_id).strip("_")[:120] or "run"


def stage_url(base: str, pipeline: str, run_id: str, stage: str) -> str:
    base = os.environ.get("PKF_STAGING_URL") or base
    return f"{base.rstrip('/')}/{pipeline}/{safe_run_id(run_id)}/{stage}"


def _windows() -> bool:
    return os.name == "nt"


def _path(url: str) -> str:
    """The local path of a file:// URL (B15): percent-escapes decoded, file:///C:/x and file://C:/x → C:\\x on
    Windows; the MSYS spelling file:///c/x is refused there (it would silently mean C:\\c\\x)."""
    u = urlparse(url)
    path = u.path
    if re.fullmatch(r"[A-Za-z]:", u.netloc or ""):  # file://C:/x: the drive was parsed as the host
        path = f"/{u.netloc}{path}"
    if _windows() and re.match(r"^/[A-Za-z](/|$)", path):
        raise ValueError(f"file URLs need a drive: file:///C:/path (not /c/path); got {url}")
    return url2pathname(path)


def _s3(url: str):
    import boto3

    u = urlparse(url)
    s3 = boto3.client("s3", endpoint_url=os.environ.get("PKF_STAGING_ENDPOINT_URL") or None)
    return s3, u.netloc, u.path.strip("/")


def put(local_dir: Path, url: str) -> None:
    u = urlparse(url)
    if u.scheme == "file":
        target = Path(_path(url))
        shutil.rmtree(target, ignore_errors=True)  # a file held open (Windows) may survive: copy over what is left
        shutil.copytree(local_dir, target, dirs_exist_ok=True)
        return
    if u.scheme != "s3":
        raise ValueError(f"unsupported staging url {url}")
    s3, bucket, prefix = _s3(url)
    clear(url)
    for p in sorted(Path(local_dir).rglob("*")):
        if p.is_file():
            s3.upload_file(str(p), bucket, f"{prefix}/{p.relative_to(local_dir).as_posix()}")


def get(url: str, local_dir: Path) -> None:
    u = urlparse(url)
    local_dir = Path(local_dir)
    if u.scheme == "file":
        shutil.rmtree(local_dir, ignore_errors=True)
        shutil.copytree(Path(_path(url)), local_dir, dirs_exist_ok=True)
        return
    s3, bucket, prefix = _s3(url)
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/"):
        for o in page.get("Contents", []):
            rel = o["Key"][len(prefix) + 1:]
            dest = local_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, o["Key"], str(dest))
            n += 1
    if n == 0:
        raise FileNotFoundError(f"staging {url} is empty (did the previous task run?)")


def clear(url: str) -> None:
    u = urlparse(url)
    if u.scheme == "file":
        shutil.rmtree(Path(_path(url)), ignore_errors=True)
        return
    s3, bucket, prefix = _s3(url)
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/")
            for o in page.get("Contents", [])]
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})


def clear_run(base: str, pipeline: str, run_id: str) -> None:
    clear(stage_url(base, pipeline, run_id, "").rstrip("/"))


def put_json(url: str, obj) -> None:
    """A small JSON document at <url> (run bookkeeping: events, package plans, results)."""
    import json

    data = json.dumps(obj, sort_keys=True, default=str).encode()
    u = urlparse(url)
    if u.scheme == "file":
        p = Path(_path(url))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return
    s3, bucket, key = _s3(url)
    s3.put_object(Bucket=bucket, Key=key, Body=data)


def get_json(url: str, default=None):
    import json

    u = urlparse(url)
    if u.scheme == "file":
        p = Path(_path(url))
        return json.loads(p.read_bytes()) if p.exists() else default
    s3, bucket, key = _s3(url)
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return default


def list_json(url: str) -> list:
    """Every JSON document directly under the folder <url> (sorted by name)."""
    import json

    u = urlparse(url)
    if u.scheme == "file":
        d = Path(_path(url))
        return [json.loads(p.read_bytes()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []
    s3, bucket, prefix = _s3(url)
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/"):
        for o in sorted(page.get("Contents", []), key=lambda o: o["Key"]):
            if o["Key"].endswith(".json") and "/" not in o["Key"][len(prefix) + 1:]:
                out.append(json.loads(s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read()))
    return out
