"""Local stand-ins for the Entimice pipeline: Entimice (REST document repository), the Self-Service API (SSAPI) and
Vault KV v2. Used by tests/test_entimice_pipeline.py and by the local end-to-end run (dryrun/entimice_run.py).

  Entimice  POST /sessions (username/password headers) → session id; DELETE /sessions/<id>;
            GET /system/children?parent=<path>&filter=webservice::<id>; GET /system?type=file&path=<path>
  SSAPI     GET /api/v1/forms/<form>/submissions; PUT /api/v1/forms/<form>/submissions/<id>   (X-API-KEY)
  Vault     GET /v1/<mount>/data/<path>  (X-Vault-Token)

State (a dict, also readable over GET /_state on each server):
  entimice: {"users": {user: password}, "files": {"/abs/path": "text" | {"size": n} | {"gzip": "text"} | {"empty": true}},
             "fail_list": {"/folder": 500 | 403 | "timeout"}, "fail_file": {"/path": 404 | 500}, "junk_nodes": bool,
             "sessions_created": [...], "sessions_deleted": [...]}
  ssapi:    {"api_key": str, "forms": {form_id: [records]}, "puts": [{"id", "body"}], "fail_put": [ids], "fail_get": status | null}
  vault:    {"token": str, "secrets": {"<mount>/<path>": {...}}}
"""
from __future__ import annotations

import gzip
import json
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Handler(BaseHTTPRequestHandler):
    state: dict = {}
    lock = threading.Lock()

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if "Content-Length" not in (headers or {}):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode())

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""


# ---------------------------------------------------------------------------------------------- Entimice
class EntimiceHandler(_Handler):
    def _sid_ok(self) -> str | None:
        sid = self.headers.get("Session-ID")
        return sid if sid and sid in self.state["live"] else None

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        st = self.state
        if u.path == "/_state":
            return self._json(200, {k: v for k, v in st.items() if k != "live"})
        sid = self._sid_ok()
        if sid is None:
            return self._send(401, b"no session")
        if u.path == "/system/children":
            if q.get("filter") != f"webservice::{sid}":
                return self._send(400, b"bad filter")
            parent = "/" + q.get("parent", "").strip("/")
            fail = st.get("fail_list", {}).get(parent)
            if fail == "timeout":
                time.sleep(st.get("timeout_sleep", 3))
                return self._json(200, [])
            if fail:
                return self._send(int(fail), b"listing refused")
            base = parent.rstrip("/") + "/"
            kids: dict[str, bool] = {}
            for p in st["files"]:
                if p.startswith(base):
                    rest = p[len(base):]
                    head, _, tail = rest.partition("/")
                    kids[base + head] = bool(tail) or kids.get(base + head, False)
            nodes = [{"contentHolders": [{"contentProperties": {"path": f"  {p}  " if i % 2 else p,
                                                                "isDirectory": "TRUE" if d else "false"}}]}
                     for i, (p, d) in enumerate(sorted(kids.items()))]
            if st.get("junk_nodes"):
                nodes += [{"contentHolders": []}, {"name": "x"}, {"contentHolders": [{"contentProperties": {"path": "  "}}]}]
            return self._json(200, nodes)
        if u.path == "/system" and q.get("type") == "file":
            path = q.get("path", "")
            if path in st.get("fail_file", {}):
                return self._send(int(st["fail_file"][path]), b"refused")
            f = st["files"].get(path)
            if f is None:
                return self._send(404, b"not found")
            if isinstance(f, dict) and f.get("empty"):
                return self._send(200, b"", "application/octet-stream")
            if isinstance(f, dict) and "gzip" in f:
                data = gzip.compress(f["gzip"].encode())
                return self._send(200, data, "application/octet-stream", {"Content-Encoding": "gzip"})
            data = f.encode() if isinstance(f, str) else (b"x" * int(f["size"]))
            if isinstance(f, dict) and f.get("short"):  # declares more bytes than it sends (a truncated download)
                self.send_response(200)
                self.send_header("Content-Length", str(len(data) + 10))
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True
                return None
            return self._send(200, data, "application/octet-stream")
        return self._send(404, b"no such endpoint")

    def do_POST(self):  # noqa: N802
        self._body()
        if self.path.rstrip("/") != "/sessions":
            return self._send(404)
        user, pw = self.headers.get("username"), self.headers.get("password")
        if self.headers.get("accept") != "text/plain" or self.state["users"].get(user) != pw or user is None:
            return self._send(401, b"login refused", "text/plain")
        sid = secrets.token_hex(8)
        with self.lock:
            self.state["live"].add(sid)
            self.state["sessions_created"].append(user)
        return self._send(200, f'"{sid}"\n'.encode(), "text/plain")

    def do_DELETE(self):  # noqa: N802
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "sessions" and parts[1] in self.state["live"]:
            with self.lock:
                self.state["live"].discard(parts[1])
                self.state["sessions_deleted"].append(parts[1])
            return self._send(204)
        return self._send(404)


# ---------------------------------------------------------------------------------------------- SSAPI
class SsapiHandler(_Handler):
    def _auth(self) -> bool:
        return self.headers.get("X-API-KEY") == self.state["api_key"] and self.headers.get("accept") == "application/vnd.dpl-selfservice.v1+json"

    def do_GET(self):  # noqa: N802
        if self.path == "/_state":
            return self._json(200, self.state)
        parts = self.path.strip("/").split("/")
        if not self._auth():
            return self._send(401, b"bad key")
        if parts[:3] == ["api", "v1", "forms"] and len(parts) == 5 and parts[4] == "submissions":
            if self.state.get("fail_get"):
                return self._send(int(self.state["fail_get"]), b"down")
            if self.state.get("not_json"):
                return self._send(200, b"<html>maintenance</html>", "text/html")
            return self._json(200, self.state["forms"].get(parts[3], []))
        return self._send(404)

    def do_PUT(self):  # noqa: N802
        body = self._body()
        parts = self.path.strip("/").split("/")
        if not self._auth() or self.headers.get("Content-Type") != "application/json":
            return self._send(401, b"bad key or content type")
        if parts[:3] == ["api", "v1", "forms"] and len(parts) == 6 and parts[4] == "submissions":
            sid = parts[5]
            if sid in [str(x) for x in self.state.get("fail_put", [])]:
                return self._send(500, b"write failed")
            doc = json.loads(body)
            with self.lock:
                self.state["puts"].append({"id": sid, "body": doc})
                for r in self.state["forms"].get(parts[3], []):
                    if str(r.get("id")) == sid:
                        r["submission_data"] = doc["submission_data"]
            return self._json(200, {"id": sid})
        return self._send(404)


# ---------------------------------------------------------------------------------------------- Vault
class VaultHandler(_Handler):
    def do_GET(self):  # noqa: N802
        if self.headers.get("X-Vault-Token") != self.state["token"]:
            return self._send(403, b'{"errors":["permission denied"]}')
        p = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        if len(p) < 4 or p[0] != "v1" or p[2] != "data":
            return self._send(404)
        key = f"{p[1]}/{'/'.join(p[3:])}"
        if key not in self.state["secrets"]:
            return self._send(404, b'{"errors":[]}')
        return self._json(200, {"data": {"data": self.state["secrets"][key]}})


def _serve(handler: type, state: dict, port: int) -> ThreadingHTTPServer:
    cls = type(handler.__name__, (handler,), {"state": state, "lock": threading.Lock()})
    srv = ThreadingHTTPServer(("127.0.0.1", port), cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start(entimice: dict, ssapi: dict, vault: dict, ports: dict | None = None) -> dict:
    """Start the three servers in this process (daemon threads). Returns {"entimice", "ssapi", "vault": url, "servers"}."""
    ports = {k: (ports or {}).get(k) or free_port() for k in ("entimice", "ssapi", "vault")}
    entimice.setdefault("live", set())
    entimice.setdefault("sessions_created", [])
    entimice.setdefault("sessions_deleted", [])
    ssapi.setdefault("puts", [])
    servers = [_serve(EntimiceHandler, entimice, ports["entimice"]), _serve(SsapiHandler, ssapi, ports["ssapi"]),
               _serve(VaultHandler, vault, ports["vault"])]
    return {k: f"http://127.0.0.1:{ports[k]}" for k in ports} | {"servers": servers}


if __name__ == "__main__":  # python dryrun/entimice_servers.py STATE.json  (serves until Ctrl+C)
    cfg = json.loads(Path(sys.argv[1]).read_text())
    urls = start(cfg["entimice"], cfg["ssapi"], cfg["vault"], cfg.get("ports"))
    print(json.dumps({k: v for k, v in urls.items() if k != "servers"}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
