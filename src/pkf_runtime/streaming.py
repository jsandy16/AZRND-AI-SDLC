"""Stream delivery (`delivery.mode: stream`): every file of a package is read from the source as a stream and written
with a multipart upload, never staged or held in memory as a whole. One failing file never stops the others; each
file gets a result row {"path", "source_path", "key", "size", "status": "uploaded" | "failed", "detail"}.

The source adapter must offer `open(root, rel)` returning a readable object with `.declared` (the byte count the
source announced, or None) and `.close()`.
"""
from __future__ import annotations

import logging
import math
import posixpath

log = logging.getLogger("pkf")

MIB = 1 << 20
MAX_PARTS = 10_000
PART_HEADROOM = 9_500  # keep below S3's 10 000 parts, so a declared size a little off still fits


def part_size(declared: int | None, configured_mb: int) -> int:
    """The configured part size, raised (whole MiB) when the file would need more than ~9 500 parts."""
    size = configured_mb * MIB
    if declared:
        size = max(size, math.ceil(declared / PART_HEADROOM / MIB) * MIB)
    return size


def content_type(path: str, types: dict[str, str], default: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    lowered = {k.lower() if k.startswith(".") else "." + k.lower(): v for k, v in types.items()}
    return lowered.get(ext, default)


def _head_size(s3, bucket: str, key: str) -> int:
    return int(s3.head_object(Bucket=bucket, Key=key)["ContentLength"])


def _exists(s3, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def deliver(session, files: list[dict], keys: dict[str, str | None], s3, bucket: str, delivery: dict) -> list[dict]:
    from boto3.s3.transfer import TransferConfig

    results = []
    for f in files:
        key = keys.get(f["path"])
        source_path = f.get("source_path") or "/" + "/".join(x for x in (f.get("root", "").strip("/"), f["path"]) if x)
        row = {"path": f["path"], "source_path": source_path, "key": key, "size": None, "status": "failed", "detail": ""}
        if key is None:
            row.update(status="not_delivered", detail="no destination key: the layout does not deliver this file")
            results.append(row)
            continue
        try:
            if delivery.get("if_exists") == "fail" and _exists(s3, bucket, key):
                raise FileExistsError(f"s3://{bucket}/{key} already exists")
            with session.open(f.get("root", ""), f["path"]) as body:
                cfg = TransferConfig(multipart_threshold=part_size(body.declared, delivery.get("part_size_mb", 16)),
                                     multipart_chunksize=part_size(body.declared, delivery.get("part_size_mb", 16)),
                                     max_concurrency=delivery.get("max_concurrency", 8), use_threads=True)
                extra = {"ContentType": content_type(f["path"], delivery.get("content_types") or {},
                                                     delivery.get("default_content_type") or "application/octet-stream")}
                s3.upload_fileobj(body, bucket, key, ExtraArgs=extra, Config=cfg)
                declared = body.declared
            size = _head_size(s3, bucket, key)
            row["size"] = size
            if delivery.get("verify_size", True) and declared is not None and declared != size:
                row["detail"] = f"S3 upload size mismatch | key={key} | source={declared} | target={size}"
            else:
                row["status"] = "uploaded"
        except Exception as exc:  # noqa: BLE001 - this file fails; the other files of the package go on
            row["detail"] = str(exc) if type(exc).__name__ in ("PackageError", "FileExistsError") else f"{type(exc).__name__}: {exc}"
        (log.info if row["status"] == "uploaded" else log.error)(
            "%s -> s3://%s/%s [%s]%s", source_path, bucket, key, row["status"], f" {row['detail']}" if row["detail"] else "")
        results.append(row)
    return results
