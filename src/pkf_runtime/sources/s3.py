"""Amazon S3 (or S3-compatible) as a source."""
from __future__ import annotations

from . import RemoteFile, depth_ok


def client(conn: dict, creds: dict[str, str]):
    import boto3

    kw = {k: creds[k] for k in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token") if creds.get(k)}
    return boto3.client("s3", region_name=conn.get("region"), endpoint_url=conn.get("endpoint_url"), **kw)


class S3Session:
    def __init__(self, conn: dict, creds: dict[str, str]):
        self.bucket = conn["bucket"]
        self.s3 = client(conn, creds)

    def check(self) -> str:
        self.s3.head_bucket(Bucket=self.bucket)
        return f"bucket {self.bucket} reachable"

    def _prefix(self, root: str) -> str:
        r = root.strip("/")
        return f"{r}/" if r else ""

    def list(self, root: str, recursive: bool, max_depth: int | None) -> list[RemoteFile]:
        prefix = self._prefix(root)
        kw = {"Bucket": self.bucket, "Prefix": prefix}
        if not recursive:
            kw["Delimiter"] = "/"
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(**kw):
            for o in page.get("Contents", []):
                rel = o["Key"][len(prefix):]
                if not rel or rel.endswith("/"):
                    continue  # folder placeholder objects
                d = rel.rsplit("/", 1)[0] if "/" in rel else ""
                if depth_ok(d, max_depth):
                    out.append(RemoteFile(rel, o["Size"], o["LastModified"].timestamp(), o.get("ETag")))
        return out

    def stat(self, root: str, rel: str) -> RemoteFile | None:
        """One object's size and age (HEAD), or None when it does not exist."""
        from botocore.exceptions import ClientError

        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=self._prefix(root) + rel)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return RemoteFile(rel, head["ContentLength"], head["LastModified"].timestamp(), head.get("ETag"))

    def key_of(self, root: str, rel: str) -> str:
        """The exact object key of a listed file (custom delivery copies and deletes this key)."""
        return self._prefix(root) + rel

    def download(self, root: str, rel: str, dest: str) -> None:
        self.s3.download_file(self.bucket, self._prefix(root) + rel, dest)

    def close(self) -> None:
        pass
