"""Custom delivery: a generated, reviewed function delivers a package's files itself, with AWS clients.

Used when the destination plan ends with a `custom_delivery` step (moves, server-side copies, object tags, anything
the framework's own upload does not do). The files are not downloaded to staging: the function receives one
*transfer* per file and talks to S3 directly.

    def deliver(transfers, params, ctx, aws) -> results

transfers  [{"path", "source_bucket", "source_key", "size", "target_bucket", "target_key"}]   (one per delivered file)
aws        aws.s3_source / aws.s3_destination (boto3 S3 clients with each side's region, endpoint and credentials),
           aws.client(service, side="destination"), aws.log (a logger)
results    [{"path", "key", "status", "detail"}], exactly one per transfer. A status starting with moved / copied /
           delivered / skipped counts as delivered; anything else fails the file, and so the package and the run.

What the framework checks around the function (data-loss guards, not limits on what the code may do):
  at planning   two files with the same target key, a target that is a listed source object, a same-bucket target
                under the listed root → the package is refused before anything is copied
  before        source already gone (a retried task): delivered if the target has the listed size, else failed;
                if_exists fail / skip_identical against an existing target
  after         every file reported moved/copied/delivered is HEADed at its key and must have the listed size
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .package import PackageError

log = logging.getLogger("pkf.delivery")


def has_custom_delivery(steps: list[dict]) -> bool:
    return any(s["op"] == "custom_delivery" for s in steps)


def delivery_step(steps: list[dict]) -> dict | None:
    return next((s for s in steps if s["op"] == "custom_delivery"), None)


class Aws:
    """AWS access for a delivery function: clients built from the source's and the destination's connection."""

    def __init__(self, source_conn: dict, source_creds: dict, dest_conn: dict, dest_creds: dict, clients: dict | None = None):
        self._conn = {"source": (source_conn or {}, source_creds or {}), "destination": (dest_conn or {}, dest_creds or {})}
        self._clients: dict[tuple[str, str], Any] = dict(clients or {})
        self.log = log

    def client(self, service: str, side: str = "destination"):
        if side not in self._conn:
            raise ValueError("side must be 'source' or 'destination'")
        key = (service, side)
        if key not in self._clients:
            import boto3

            conn, creds = self._conn[side]
            kw = {k: creds[k] for k in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token") if creds.get(k)}
            endpoint = conn.get("endpoint_url") if service == "s3" else None
            self._clients[key] = boto3.client(service, region_name=conn.get("region"), endpoint_url=endpoint, **kw)
        return self._clients[key]

    @property
    def s3_source(self):
        return self.client("s3", "source")

    @property
    def s3_destination(self):
        return self.client("s3", "destination")


def _join(root: str, path: str) -> str:
    r = (root or "").strip("/")
    return f"{r}/{path}" if r else path


SUCCESS = ("moved", "copied", "delivered", "skipped")
"""A result status must start with one of these to count as delivered; anything else ("failed", "error", …) fails."""


def source_key(f: dict) -> str:
    """The exact key that was listed (S3 sources record it); otherwise root + path."""
    return f.get("key") or _join(f.get("root", ""), f["path"])


def transfers_for(files: list[dict], keys: dict[str, str | None], source_bucket: str | None, target_bucket: str | None) -> list[dict]:
    """One transfer per file that has a key (files routed to None are not delivered)."""
    out = []
    for f in sorted(files, key=lambda x: x["path"]):
        key = keys.get(f["path"])
        if key is None:
            continue
        out.append({"path": f["path"], "source_bucket": source_bucket, "source_key": source_key(f), "size": f.get("size"),
                    "etag": f.get("etag"), "target_bucket": target_bucket, "target_key": key})
    return out


def plan_conflicts(packages: list[dict], source_bucket: str, target_bucket: str, listed: list[dict] | None = None) -> dict[int, str]:
    """Packages whose delivery would lose data, by package index → reason: two files with the same target, a target
    that is a listed source object (copy onto itself, or over a file still waiting to be moved), or — in the same
    bucket — a target under a listed root (the next run would pick it up and move it again).
    listed: every file the source listed (default: the packages' files), e.g. also files a preview left out (K6)."""
    bad: dict[int, str] = {}
    every = listed if listed is not None else [f for p in packages for f in p["files"]]
    sources = {source_key(f) for f in every}
    roots = {((f.get("root") or "").strip("/") + "/") for f in every if (f.get("root") or "").strip("/")}
    by_target: dict[str, list[tuple[int, str]]] = {}
    for p in packages:
        for f in p["files"]:
            k = (p.get("keys") or {}).get(f["path"])
            if k is None:
                continue
            by_target.setdefault(k, []).append((p["index"], f["path"]))
            if source_bucket == target_bucket and k in sources:
                bad.setdefault(p["index"], f"'{f['path']}' would be delivered onto the source object s3://{source_bucket}/{k}")
            elif source_bucket == target_bucket and any(k.startswith(r) for r in roots):
                bad.setdefault(p["index"], f"'{f['path']}' -> '{k}' is under the source root, so the next run would move it again")
    for k, owners in by_target.items():
        if len(owners) > 1:
            names = ", ".join(path for _, path in owners[:3])
            for idx, _ in owners:
                bad.setdefault(idx, f"{len(owners)} files would be delivered to the same key '{k}' ({names}); one would be lost")
    return bad


def _head(s3, bucket: str, key: str) -> dict | None:
    from botocore.exceptions import ClientError

    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def precheck(transfers: list[dict], aws: "Aws", if_exists: str) -> tuple[list[dict], list[dict]]:
    """Before the function runs: (transfers to pass on, results decided by the framework).
    - source gone but the target is there with the listed size → delivered by an earlier attempt (task retry)
    - source gone otherwise → failed (nothing to deliver)
    - if_exists fail / skip_identical: an existing target with other content fails; the same ETag is skipped"""
    todo, done = [], []
    for t in transfers:
        src = _head(aws.s3_source, t["source_bucket"], t["source_key"])
        if src is None:
            tgt = _head(aws.s3_destination, t["target_bucket"], t["target_key"])
            if tgt is not None and (t.get("size") is None or tgt.get("ContentLength") == t["size"]):
                done.append(_res(t, "skipped", "already delivered by an earlier attempt"))
            else:
                done.append(_res(t, "failed", "the source object no longer exists and the target is not there"))
            continue
        if if_exists != "overwrite":
            tgt = _head(aws.s3_destination, t["target_bucket"], t["target_key"])
            if tgt is not None:
                if if_exists == "skip_identical" and tgt.get("ETag") and tgt.get("ETag") == src.get("ETag"):
                    done.append(_res(t, "skipped", "identical object already at the target"))
                else:
                    done.append(_res(t, "failed", f"the target already exists (if_exists: {if_exists})"))
                continue
        todo.append(t)
    return todo, done


def _res(t: dict, status: str, detail: str) -> dict:
    return {"path": t["path"], "key": t["target_key"], "status": status, "detail": detail, "size": t.get("size") or 0}


def run_custom_delivery(fn, step_id: str, transfers: list[dict], params: dict, ctx: dict, aws: Aws) -> list[dict]:
    """Call the generated function; check and normalise its results (exactly one per transfer, known statuses)."""
    if not transfers:
        return []
    try:
        results = fn(json.loads(json.dumps(transfers)), dict(params), dict(ctx), aws)
    except Exception as exc:  # noqa: BLE001 - generated code: this package fails
        raise PackageError(f"custom delivery '{step_id}': {type(exc).__name__}: {exc}") from None
    if not isinstance(results, list) or not all(isinstance(r, dict) and isinstance(r.get("status"), str) for r in results):
        raise PackageError(f"custom delivery '{step_id}' must return a list of {{'path', 'key', 'status', 'detail'}}")
    by_path: dict = {}
    for r in results:
        if r.get("path") in by_path:
            raise PackageError(f"custom delivery '{step_id}' returned two results for '{r.get('path')}'")
        by_path[r.get("path")] = r
    missing = [t["path"] for t in transfers if t["path"] not in by_path]
    if missing:
        raise PackageError(f"custom delivery '{step_id}' returned no result for {len(missing)} file(s), e.g. '{missing[0]}'")
    out = []
    for t in transfers:
        r = by_path[t["path"]]
        status, detail = r["status"], str(r.get("detail") or "")
        if not status.lower().startswith(SUCCESS):
            status, detail = ("failed" if not status.lower().startswith("fail") else status), (detail or r["status"])
        out.append({"path": t["path"], "key": str(r.get("key") or t["target_key"]), "status": status, "detail": detail,
                    "size": t.get("size") or 0})
    return out


def verify(results: list[dict], transfers: list[dict], aws: Aws) -> list[dict]:
    """After the function: every file it reports moved/copied/delivered must be at its key with the listed size."""
    by_path = {t["path"]: t for t in transfers}
    for r in results:
        if r["status"].lower().startswith(("failed", "skipped")):
            continue
        t = by_path[r["path"]]
        head = _head(aws.s3_destination, t["target_bucket"], r["key"])
        if head is None:
            r.update(status="failed", detail=f"framework check: s3://{t['target_bucket']}/{r['key']} does not exist ({r['detail']})")
        elif t.get("size") is not None and head.get("ContentLength") != t["size"]:
            r.update(status="failed", detail=f"framework check: {r['key']} has {head.get('ContentLength')} bytes, the source had {t['size']}")
    return results


def failed(results: list[dict]) -> list[dict]:
    return [r for r in results if not r["status"].lower().startswith(SUCCESS)]


def status_counts(results: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return dict(sorted(out.items()))
