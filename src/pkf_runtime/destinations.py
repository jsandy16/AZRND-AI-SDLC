"""Deliver a finished package: every file under <prefix>/<package-relative path>.

if_exists:
  fail            an existing object at a key fails the run
  skip_identical  an existing object with the same SHA-256 is skipped (safe task retries); a different one fails
  overwrite       replace
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .package import Package, PackageError, safe_relpath


def _key(prefix: str, rel: str) -> str:
    p = prefix.strip("/")
    return f"{p}/{rel}" if p else rel


class S3Destination:
    def __init__(self, conn: dict, creds: dict[str, str]):
        from .sources.s3 import client

        self.bucket = conn["bucket"]
        self.s3 = client(conn, creds)

    def check(self) -> str:
        self.s3.head_bucket(Bucket=self.bucket)
        return f"bucket {self.bucket} reachable"

    def _existing(self, key: str) -> tuple[str | None, str | None]:
        from botocore.exceptions import ClientError

        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404 or exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None, None
            raise
        meta = head.get("Metadata", {})
        return meta.get("pkf-sha256", "unknown"), meta.get("pkf-run-id")

    def put(self, key: str, local: Path, sha256: str, delivery: dict) -> str:
        mode = delivery.get("if_exists", "skip_identical")
        if mode != "overwrite":
            have, run = self._existing(key)
            if have is not None:
                if have == sha256 and (mode == "skip_identical" or (run and run == delivery.get("run_id"))):
                    return "skipped (identical)"  # with if_exists=fail: an earlier attempt of this same run
                raise PackageError(f"s3://{self.bucket}/{key} already exists" + (" with different content" if mode == "skip_identical" else ""))
        extra = {"Metadata": {"pkf-sha256": sha256, **({"pkf-run-id": delivery["run_id"]} if delivery.get("run_id") else {})}}
        if delivery.get("server_side_encryption"):
            extra["ServerSideEncryption"] = delivery["server_side_encryption"]
            if delivery.get("kms_key_id"):
                extra["SSEKMSKeyId"] = delivery["kms_key_id"]
        self.s3.upload_file(str(local), self.bucket, key, ExtraArgs=extra)
        return "uploaded"

    def url(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def close(self) -> None:
        pass


class LocalDestination:
    def __init__(self, conn: dict, creds: dict[str, str]):
        self.base = Path(conn["base_path"]).expanduser()

    def check(self) -> str:
        self.base.mkdir(parents=True, exist_ok=True)
        return f"folder {self.base} writable"

    def put(self, key: str, local: Path, sha256: str, delivery: dict) -> str:
        target = self.base / safe_relpath(key)
        mode = delivery.get("if_exists", "skip_identical")
        if target.exists() and mode != "overwrite":
            import hashlib

            h = hashlib.sha256()
            with open(target, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if mode == "skip_identical" and h.hexdigest() == sha256:
                return "skipped (identical)"
            raise PackageError(f"{target} already exists" + (" with different content" if mode == "skip_identical" else ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".pkf-tmp")
        shutil.copyfile(local, tmp)
        _replace(tmp, target)  # never a partial file at the final name
        return "uploaded"

    def url(self, key: str) -> str:
        return str(self.base / key)

    def close(self) -> None:
        pass


REPLACE_ATTEMPTS = 5
REPLACE_SLEEP_S = 0.5


def _replace(tmp: Path, target: Path) -> None:
    """os.replace, retried while another program holds the target open (Windows WinError 32: Excel, an indexer, a
    virus scanner); still locked after the retries → ConnectionError, which the task retries (B14)."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) != 32:
                raise
            if attempt == REPLACE_ATTEMPTS - 1:
                raise ConnectionError(f"{target} is in use by another program (WinError 32); close it and retry") from None
            time.sleep(REPLACE_SLEEP_S)


def open_destination(spec: dict, creds: dict[str, str]):
    return {"s3": S3Destination, "local": LocalDestination}[spec["type"]](spec["connection"], creds)


def deliver(pkg: Package, dest, keys: dict[str, str | None], delivery: dict) -> list[dict]:
    """Put every package file at its key (None = not delivered), then the success marker in the keys' common folder."""
    results = []
    for rel in pkg.files():
        key = keys.get(rel)
        if key is None:
            continue
        safe_relpath(key)
        sha = pkg.checksum(rel, "sha256")
        status = dest.put(key, pkg.path(rel), sha, delivery)
        results.append({"path": rel, "key": key, "size": pkg.size(rel), "sha256": sha, "status": status})
    if delivery.get("write_success_marker") and results:
        import hashlib
        import posixpath

        folder = posixpath.commonpath([posixpath.dirname(r["key"]) or "." for r in results])
        folder = "" if folder == "." else folder
        marker = pkg.new_temp_path()
        marker.write_bytes(b"")
        dest.put(_key(folder, "_SUCCESS"), marker, hashlib.sha256(b"").hexdigest(), {**delivery, "if_exists": "overwrite"})
    return results
