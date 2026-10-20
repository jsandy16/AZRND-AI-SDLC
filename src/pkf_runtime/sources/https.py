"""HTTPS file servers. Plain HTTP has no standard folder listing, so the spec says how folders are listed:

html_index  GET <folder>/ returns an HTML page whose links are the entries (Apache/nginx autoindex); names ending
            in '/' are folders. Links leaving the folder (absolute URLs to other places, '../', '?sort') are ignored.
webdav      PROPFIND <folder>/ with Depth: 1 (RFC 4918) returns a multistatus XML document.
json_index  GET <folder>/ returns a JSON list of {"name", "type": "file"|"dir", "size"?, "mtime"?}.
"""
from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from . import RemoteFile, depth_ok


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within the same scheme and host, so credentials never leave the configured server."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(urllib.parse.urljoin(req.full_url, newurl))
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise PermissionError(f"redirect to another server or scheme refused ({new.scheme}://{new.netloc})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpsSession:
    def __init__(self, conn: dict, creds: dict[str, str]):
        self.base = conn["base_url"].rstrip("/")
        self.listing = conn.get("listing", "html_index")
        self.timeout = conn.get("timeout_seconds", 60)
        self.headers = {"User-Agent": "package-factory", **(conn.get("headers") or {})}
        auth = conn.get("auth", "none")
        if auth == "basic":
            if not creds.get("username") or "password" not in creds:
                raise PermissionError("https basic auth needs credential fields username and password")
            self.headers["Authorization"] = "Basic " + base64.b64encode(f"{creds['username']}:{creds['password']}".encode()).decode()
        elif auth == "bearer":
            if not creds.get("token"):
                raise PermissionError("https bearer auth needs credential field token")
            self.headers["Authorization"] = f"Bearer {creds['token']}"
        ctx = ssl.create_default_context(cafile=conn.get("ca_bundle"))
        if not conn.get("verify_tls", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _SameOriginRedirects())

    # --- helpers
    def _url(self, path: str) -> str:
        return self.base + urllib.parse.quote("/" + path.strip("/"), safe="/")

    def _open(self, url: str, method: str = "GET", body: bytes | None = None, extra: dict | None = None):
        req = urllib.request.Request(url, data=body, method=method, headers={**self.headers, **(extra or {})})
        return self.opener.open(req, timeout=self.timeout)

    def check(self) -> str:
        with self._open(self.base + "/", method="GET" if self.listing != "webdav" else "PROPFIND",
                        extra={"Depth": "0"} if self.listing == "webdav" else None) as r:
            return f"HTTP {r.status} from {self.base}"

    # --- listing
    def _entries(self, folder: str) -> list[tuple[str, bool, int | None, float | None]]:
        """(name, is_dir, size, mtime) of the direct children of folder (relative to the server root)."""
        url = self._url(folder) + "/"
        if self.listing == "webdav":
            body = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/>'
                    b'<d:getlastmodified/></d:prop></d:propfind>')
            with self._open(url, "PROPFIND", body, {"Depth": "1", "Content-Type": "application/xml"}) as r:
                doc = ET.fromstring(r.read())
            out, me = [], urllib.parse.urlparse(url).path.rstrip("/")
            for resp in doc.findall("{DAV:}response"):
                href = urllib.parse.unquote(urllib.parse.urlparse(resp.findtext("{DAV:}href") or "").path).rstrip("/")
                if href == urllib.parse.unquote(me) or not href:
                    continue
                prop = resp.find(".//{DAV:}prop")
                is_dir = prop is not None and prop.find("{DAV:}resourcetype/{DAV:}collection") is not None
                size = prop.findtext("{DAV:}getcontentlength") if prop is not None else None
                lm = prop.findtext("{DAV:}getlastmodified") if prop is not None else None
                out.append((href.rsplit("/", 1)[-1], is_dir, int(size) if size and size.isdigit() else None,
                            parsedate_to_datetime(lm).timestamp() if lm else None))
            return out
        with self._open(url) as r:
            raw = r.read()
        if self.listing == "json_index":
            return [(e["name"].strip("/"), e.get("type") == "dir", e.get("size"), e.get("mtime")) for e in json.loads(raw)]
        p = _Links()
        p.feed(raw.decode("utf-8", errors="replace"))
        out, here = [], urllib.parse.urlparse(url)
        for href in p.links:
            target = urllib.parse.urlparse(urllib.parse.urljoin(url, href))
            if target.netloc != here.netloc or target.query or target.fragment and not target.path:
                continue
            path = urllib.parse.unquote(target.path)
            parent = urllib.parse.unquote(here.path)
            if not path.startswith(parent) or path == parent:
                continue  # parent links, absolute links elsewhere on the server
            name = path[len(parent):]
            is_dir = name.endswith("/")
            name = name.strip("/")
            if not name or "/" in name:
                continue
            out.append((name, is_dir, None, None))
        return out

    def list(self, root: str, recursive: bool, max_depth: int | None) -> list[RemoteFile]:
        root = root.strip("/")
        files, queue, seen = [], [""], set()
        while queue:
            rel_dir = queue.pop()
            if rel_dir in seen:
                continue
            seen.add(rel_dir)
            for name, is_dir, size, mtime in self._entries("/".join(x for x in (root, rel_dir) if x)):
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if is_dir:
                    if recursive and depth_ok(rel, max_depth):
                        queue.append(rel)
                else:
                    if size is None and self.listing == "html_index":
                        size, mtime = self._head("/".join(x for x in (root, rel) if x))
                    files.append(RemoteFile(rel, size, mtime))
        return files

    def _head(self, path: str) -> tuple[int | None, float | None]:
        """Size and age of one file (autoindex pages do not reliably carry them)."""
        with self._open(self._url(path), method="HEAD") as r:
            length, lm = r.headers.get("Content-Length"), r.headers.get("Last-Modified")
        return (int(length) if length and length.isdigit() else None,
                parsedate_to_datetime(lm).timestamp() if lm else None)

    def download(self, root: str, rel: str, dest: str) -> None:
        url = self._url("/".join(x for x in (root.strip("/"), rel) if x))
        with self._open(url) as r, open(dest, "wb") as fh:
            expected = r.headers.get("Content-Length")
            n = 0
            for chunk in iter(lambda: r.read(1 << 20), b""):
                fh.write(chunk)
                n += len(chunk)
        if expected is not None and expected.isdigit() and int(expected) != n:
            raise ConnectionError(f"download of '{rel}' incomplete ({n} of {expected} bytes)")

    def close(self) -> None:
        pass
