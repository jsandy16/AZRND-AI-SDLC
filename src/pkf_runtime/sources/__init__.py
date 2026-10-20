"""Source adapters. Every adapter lists files under a root (recursively, as deep as allowed) and downloads them;
selection (globs, regex, age, limits) is done once, in `select_files`, the same way for every source type."""
from __future__ import annotations

import contextlib
import importlib
import re
import ssl
import time
import urllib.error
from dataclasses import dataclass
from typing import Protocol

from ..package import PackageError, matches_any


class SourceError(Exception):
    """The source cannot be used as configured (retrying cannot help)."""


@dataclass(frozen=True)
class RemoteFile:
    path: str     # relative to the extraction root, '/'-separated
    size: int | None
    mtime: float | None
    etag: str | None = None   # S3: the object's ETag (custom delivery copies only that version)


class SourceSession(Protocol):
    def check(self) -> str: ...
    def list(self, root: str, recursive: bool, max_depth: int | None) -> list[RemoteFile]: ...
    def download(self, root: str, rel: str, dest: str) -> None: ...
    def close(self) -> None: ...


ADAPTERS = {"https": "HttpsSession", "sftp": "SftpSession", "ftp": "FtpSession", "s3": "S3Session", "local": "LocalSession"}


def open_source(spec: dict, creds: dict[str, str]) -> SourceSession:
    if spec["type"] not in ADAPTERS:
        raise ValueError(f"unknown source type {spec['type']}")
    mod = importlib.import_module(f"{__name__}.{spec['type']}")
    return getattr(mod, ADAPTERS[spec["type"]])(spec["connection"], creds)


def select_files(files: list[RemoteFile], ex: dict, now: float | None = None) -> list[RemoteFile]:
    now = now or time.time()
    out = []
    for f in files:
        if not matches_any(ex.get("include") or ["**"], f.path) or matches_any(ex.get("exclude") or [], f.path):
            continue
        if ex.get("regex") and not re.fullmatch(ex["regex"], f.path):
            continue
        if ex.get("min_age_seconds") and f.mtime is not None and now - f.mtime < ex["min_age_seconds"]:
            continue
        out.append(f)
    out.sort(key=lambda f: f.path)
    if len(out) < ex.get("min_files", 1):
        raise PackageError(f"found {len(out)} matching file(s) under the extraction root (min_files {ex.get('min_files', 1)})")
    if ex.get("max_files") and len(out) > ex["max_files"]:
        raise PackageError(f"found {len(out)} matching files (max_files {ex['max_files']})")
    total = sum(f.size or 0 for f in out)
    if ex.get("max_total_bytes") and total > ex["max_total_bytes"]:
        raise PackageError(f"matching files total {total} bytes (max_total_bytes {ex['max_total_bytes']})")
    return out


def depth_ok(rel_dir: str, max_depth: int | None) -> bool:
    """rel_dir: folder relative to root ('' = root). max_depth 0 = root only."""
    return max_depth is None or (0 if not rel_dir else rel_dir.count("/") + 1) <= max_depth


@contextlib.contextmanager
def access_errors(what: str):
    """Turn 'retrying cannot help' failures (rejected credentials, untrusted certificate or host key, access
    denied) into PermissionError so the task fails at once instead of retrying; other errors pass unchanged."""
    try:
        yield
    except PermissionError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified below, re-raised unchanged otherwise
        reason = _access_reason(exc)
        if reason is None:
            raise
        raise PermissionError(f"{what}: {reason}") from None


def _access_reason(exc: BaseException) -> str | None:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}" if exc.code in (401, 403) else None
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else exc
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"TLS certificate not trusted ({getattr(exc, 'verify_message', None) or exc}); set ca_bundle or fix the server certificate"
    name = type(exc).__name__
    if name in ("AuthenticationException", "BadAuthenticationType"):
        return "SSH authentication rejected"
    if name == "BadHostKeyException" or (name == "SSHException" and "known_hosts" in str(exc)):
        return "SSH host key does not match the pinned host_key"
    if name == "error_perm" and str(exc)[:3] in ("530", "532"):
        return f"FTP login rejected ({str(exc)[:3]})"
    if name == "ClientError":
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "403", "NoSuchBucket"):
            return f"S3 {code}"
    return None
