"""Entimice, a REST document repository: session login, recursive JSON listing, streamed download.

  login     POST {base}/sessions  (accept: text/plain, username, password headers) → 200, body = session id
  logout    DELETE {base}/sessions/{id}  → 200 or 204 (anything else is only a warning)
  children  GET {base}/system/children?parent=<path>&filter=webservice::<id>  (Session-ID header) → JSON list of nodes;
            node.contentHolders[0].contentProperties has "path" and "isDirectory" ("true"/"false")
  file      GET {base}/system?type=file&path=<path>  (Session-ID header), streamed; gzip content-encoding is decoded

Several accounts: the connection's `accounts` maps a value of `account_by` (e.g. the study's destination, matched
case-insensitively after trimming) to a secret reference holding that account's username and password; any other
value uses the default account (the source's own credentials). `bind(vars)` picks the account for the package being
worked on; `prepare([vars, …])` logs in to every account needed before anything is listed. `close()` deletes every
session this object opened.

Listing failures (HTTP errors, timeouts, network errors) of a package raise PackageError: that package fails, the
others go on. Login failures raise PermissionError: the run fails. Passwords and session ids are never logged.
"""
from __future__ import annotations

import gzip
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from ..package import PackageError, render
from . import RemoteFile

log = logging.getLogger("pkf")

LOGIN_TIMEOUT_S = 60
LIST_TIMEOUT_S = 60
DOWNLOAD_TIMEOUT_S = 120
DEFAULT = "__default__"


class EntimiceDownload:
    """A streamed file: .read(n) gives the original bytes; .declared is the Content-Length the server declared for
    them (None when unknown, e.g. gzip-encoded responses)."""

    def __init__(self, response, path: str):
        self._r = response
        self.path = path
        encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
        length = response.headers.get("Content-Length")
        self.declared = int(length) if length and length.strip().isdigit() and encoding in ("", "identity") else None
        self._raw_declared = int(length) if length and length.strip().isdigit() else None
        self._body = gzip.GzipFile(fileobj=response, mode="rb") if encoding in ("gzip", "x-gzip") else response
        if self._raw_declared == 0:
            response.close()
            raise PackageError(f"Entimice returned empty content for file | path={path}")

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def close(self) -> None:
        try:
            self._body.close()
        finally:
            self._r.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class EntimiceSession:
    def __init__(self, conn: dict, creds: dict[str, str], resolve: Callable[[str], dict[str, str]] | None = None):
        self.base = conn["base_url"].rstrip("/")
        self.accounts = {str(k).strip().lower(): v for k, v in (conn.get("accounts") or {}).items()}
        self.account_by = conn.get("account_by")
        self.creds = {DEFAULT: creds}
        self._resolve = resolve
        ctx = ssl.create_default_context(cafile=conn.get("ca_bundle"))
        if not conn.get("verify_tls", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        self.sessions: dict[str, str] = {}
        self.current = DEFAULT

    # --- accounts and sessions
    def account_for(self, variables: dict) -> str:
        if not self.account_by or not self.accounts:
            return DEFAULT
        try:
            value = render(self.account_by, variables)
        except PackageError:
            return DEFAULT
        key = value.strip().lower()
        return key if key in self.accounts else DEFAULT

    def bind(self, variables: dict) -> None:
        """Work as the account of this package from now on."""
        self.current = self.account_for(variables)

    def prepare(self, many: list[dict]) -> None:
        """Log in to every account the packages need, before any of them is processed."""
        for name in sorted({self.account_for(v) for v in many}):
            self._session(name)

    def _credentials(self, name: str) -> dict[str, str]:
        if name not in self.creds:
            if self._resolve is None:
                raise PermissionError(f"Entimice account '{name}' needs a credential resolver")
            self.creds[name] = self._resolve(self.accounts[name])
        return self.creds[name]

    def _label(self, name: str) -> str:
        return "default account" if name == DEFAULT else f"account for '{name}'"

    def _session(self, name: str) -> str:
        if name in self.sessions:
            return self.sessions[name]
        c = self._credentials(name)
        if not c.get("username") or not c.get("password"):
            raise PermissionError(f"Entimice {self._label(name)}: credential fields username and password are needed")
        req = urllib.request.Request(f"{self.base}/sessions", data=b"", method="POST",
                                     headers={"accept": "text/plain", "username": c["username"], "password": c["password"]})
        try:
            with self.opener.open(req, timeout=LOGIN_TIMEOUT_S) as r:
                status, body = r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise PermissionError(f"Entimice login failed ({self._label(name)}): HTTP {exc.code} {exc.reason}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PermissionError(f"Entimice login failed ({self._label(name)}): {getattr(exc, 'reason', exc)}") from None
        sid = body.strip().strip('"').strip("'").strip()
        if status != 200 or not sid:
            raise PermissionError(f"Entimice login failed ({self._label(name)}): HTTP {status}" + ("" if sid else ", empty session id"))
        self.sessions[name] = sid
        log.info("Entimice: logged in (%s)", self._label(name))
        return sid

    def _logout(self, name: str, sid: str) -> None:
        req = urllib.request.Request(f"{self.base}/sessions/{urllib.parse.quote(sid, safe='')}", method="DELETE", headers={"accept": "*/*"})
        try:
            with self.opener.open(req, timeout=LOGIN_TIMEOUT_S) as r:
                ok = r.status in (200, 204)
                status = r.status
        except urllib.error.HTTPError as exc:
            ok, status = False, exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            ok, status = False, getattr(exc, "reason", exc)
        if ok:
            log.info("Entimice: session deleted (%s)", self._label(name))
        else:
            log.warning("Entimice: could not delete the session (%s): %s", self._label(name), status)

    def check(self) -> str:
        self._session(self.current)
        return f"Entimice login ok at {self.base}"

    def close(self) -> None:
        for name, sid in list(self.sessions.items()):
            self._logout(name, sid)
            del self.sessions[name]

    # --- listing
    def _children(self, path: str) -> list:
        sid = self._session(self.current)
        q = urllib.parse.urlencode({"parent": path, "filter": f"webservice::{sid}"})
        req = urllib.request.Request(f"{self.base}/system/children?{q}", method="GET", headers={"accept": "application/json", "Session-ID": sid})
        try:
            with self.opener.open(req, timeout=LIST_TIMEOUT_S) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as exc:
            raise PackageError(f"Entimice listing failed | path={path} | HTTP {exc.code} {exc.reason}") from None
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            why = getattr(exc, "reason", exc)
            what = "timed out" if "timed out" in str(why).lower() or isinstance(why, TimeoutError) else "failed"
            raise PackageError(f"Entimice listing {what} | path={path} | {why}") from None
        if status != 200:
            raise PackageError(f"Entimice listing failed | path={path} | HTTP {status}")
        try:
            nodes = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise PackageError(f"Entimice listing failed | path={path} | the response is not JSON") from None
        if not isinstance(nodes, list):
            raise PackageError(f"Entimice listing failed | path={path} | expected a JSON list of nodes")
        return nodes

    def list(self, root: str, recursive: bool, max_depth: int | None) -> list[RemoteFile]:
        """Files under root; each path is the Entimice path relative to root (root itself has no trailing '/')."""
        top = "/" + root.strip().strip("/") if root.strip().strip("/") else "/"
        files, queue = [], [(top, 0)]
        while queue:
            folder, depth = queue.pop(0)
            for node in self._children(folder):
                holders = node.get("contentHolders") if isinstance(node, dict) else None
                if not holders or not isinstance(holders, list) or not isinstance(holders[0], dict):
                    continue
                props = holders[0].get("contentProperties") or {}
                path = str(props.get("path") or "").strip()
                if not path:
                    continue
                if str(props.get("isDirectory", "false")).strip().lower() == "true":
                    if not recursive:
                        continue
                    if max_depth is not None and depth + 1 > max_depth:
                        log.warning("Entimice: folder %s is more than %s levels below %s: skipped", path, max_depth, top)
                        continue
                    queue.append((path, depth + 1))
                    continue
                files.append(RemoteFile(_relative(top, path), None, None))
        return files

    # --- download
    def open(self, root: str, rel: str) -> EntimiceDownload:
        """Stream one file; raise PackageError when Entimice refuses it or declares it empty."""
        path = _join(root, rel)
        sid = self._session(self.current)
        q = urllib.parse.urlencode({"type": "file", "path": path})
        req = urllib.request.Request(f"{self.base}/system?{q}", method="GET",
                                     headers={"accept": "*/*", "Session-ID": sid})
        try:
            r = self.opener.open(req, timeout=DOWNLOAD_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            raise PackageError(f"Entimice download failed | path={path} | HTTP {exc.code} {exc.reason}") from None
        if r.status != 200:
            r.close()
            raise PackageError(f"Entimice download failed | path={path} | HTTP {r.status}")
        return EntimiceDownload(r, path)

    def download(self, root: str, rel: str, dest: str) -> None:
        with self.open(root, rel) as src, open(dest, "wb") as fh:
            n = 0
            for chunk in iter(lambda: src.read(1 << 20), b""):
                fh.write(chunk)
                n += len(chunk)
        if src.declared is not None and src.declared != n:
            raise ConnectionError(f"download of '{rel}' incomplete ({n} of {src.declared} bytes)")


def _join(root: str, rel: str) -> str:
    r = root.strip().strip("/")
    return "/" + "/".join(x for x in (r, rel.strip().strip("/")) if x)


def _relative(top: str, path: str) -> str:
    """The node's path below the listed root; a path outside it keeps its full form (without the leading '/')."""
    p = "/" + path.strip().lstrip("/")
    base = top.rstrip("/") + "/"
    return p[len(base):] if base != "/" and p.startswith(base) else p.lstrip("/")
