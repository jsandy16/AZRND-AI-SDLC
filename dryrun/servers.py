"""Local stand-ins for the dry run (127.0.0.1 only, fresh keys and certificates each start):

  HTTPS   TLS (own CA), basic auth; one file tree served three ways: /html (autoindex pages), /dav (WebDAV PROPFIND),
          /json (JSON folder index)
  SFTP    asyncssh, key auth, pinned host key
  FTPS    pyftpdlib, explicit TLS
  AWS     moto server: S3 (sources, staging, destination) and Secrets Manager
  Vault   KV v2 read endpoint (GET /v1/<mount>/data/<path>, X-Vault-Token)

    python dryrun/servers.py <state_dir>     # writes <state_dir>/env.json and blocks
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from email.utils import formatdate
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HTTPS_USER, HTTPS_PASSWORD = "cro_reader", "https-dry-run-password"
VAULT_TOKEN = "dry-run-vault-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"port {port} did not open")


def make_ca(state: Path) -> tuple[Path, Path, Path]:
    """A private CA and a server certificate for 127.0.0.1 signed by it (clients trust only the CA)."""
    d = state / "tls"
    d.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(list(a), check=True, capture_output=True)  # noqa: E731
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=pkf dry-run CA",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout", str(d / "ca.key"), "-out", str(d / "ca.pem"))
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1", "-keyout", str(d / "server.key"), "-out", str(d / "server.csr"))
    (d / "ext.cnf").write_text("subjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n"
                               "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
    run("openssl", "x509", "-req", "-in", str(d / "server.csr"), "-CA", str(d / "ca.pem"), "-CAkey", str(d / "ca.key"), "-CAcreateserial",
        "-days", "2", "-extfile", str(d / "ext.cnf"), "-out", str(d / "server.pem"))
    return d / "ca.pem", d / "server.pem", d / "server.key"


def https_server(state: Path, port: int, cert: Path, key: Path, ready: threading.Event) -> None:
    root = state / "https"
    root.mkdir(exist_ok=True)
    expected = "Basic " + base64.b64encode(f"{HTTPS_USER}:{HTTPS_PASSWORD}".encode()).decode()

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

        def _auth(self) -> bool:
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="dry-run"')
                self.end_headers()
                return False
            return True

        def _mode_and_path(self):
            path = urllib.parse.urlparse(self.path).path
            for prefix in ("/html", "/dav", "/json"):
                if path == prefix or path.startswith(prefix + "/"):
                    return prefix[1:], path[len(prefix):] or "/"
            return "html", path

        def translate_path(self, path):
            _, p = self._mode_and_path()
            return super().translate_path(p)

        def do_GET(self):
            if not self._auth():
                return
            mode, p = self._mode_and_path()
            local = Path(self.translate_path(self.path))
            if mode == "json" and local.is_dir():
                entries = [{"name": c.name, "type": "dir" if c.is_dir() else "file", **({} if c.is_dir() else {"size": c.stat().st_size, "mtime": c.stat().st_mtime})}
                           for c in sorted(local.iterdir())]
                body = json.dumps(entries).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if local.is_dir() and not self.path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", self.path + "/")
                self.end_headers()
                return
            super().do_GET()

        def do_HEAD(self):
            if self._auth():
                super().do_HEAD()

        def do_PROPFIND(self):
            if not self._auth():
                return
            mode, p = self._mode_and_path()
            local = Path(self.translate_path(self.path))
            if not local.exists():
                self.send_error(404)
                return
            depth = self.headers.get("Depth", "1")
            items = [local] + (sorted(local.iterdir()) if local.is_dir() and depth != "0" else [])
            base = urllib.parse.urlparse(self.path).path.rstrip("/")
            parts = []
            for it in items:
                href = base if it == local else f"{base}/{urllib.parse.quote(it.name)}"
                if it.is_dir():
                    prop = "<d:resourcetype><d:collection/></d:resourcetype>"
                    href += "/"
                else:
                    prop = f"<d:resourcetype/><d:getcontentlength>{it.stat().st_size}</d:getcontentlength>"
                prop += f"<d:getlastmodified>{formatdate(it.stat().st_mtime, usegmt=True)}</d:getlastmodified>"
                parts.append(f"<d:response><d:href>{html.escape(href)}</d:href><d:propstat><d:prop>{prop}</d:prop>"
                             f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
            body = ('<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:">' + "".join(parts) + "</d:multistatus>").encode()
            self.send_response(207)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    ready.set()
    srv.serve_forever()


def vault_server(state: Path, port: int, ready: threading.Event) -> None:
    store = state / "vault.json"  # {"<mount>/<path>": {...}}

    class V(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get("X-Vault-Token") != VAULT_TOKEN:
                self.send_error(403)
                return
            path = urllib.parse.urlparse(self.path).path
            if not path.startswith("/v1/") or "/data/" not in path:
                self.send_error(404)
                return
            mount, _, secret = path[4:].partition("/data/")
            data = json.loads(store.read_text()) if store.exists() else {}
            value = data.get(f"{mount}/{secret}")
            if value is None:
                self.send_error(404)
                return
            body = json.dumps({"data": {"data": value, "metadata": {"version": 1}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), V)
    ready.set()
    srv.serve_forever()


def sftp_server(state: Path, port: int, ready: threading.Event) -> None:
    import asyncssh

    keys = state / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    host, client = asyncssh.generate_private_key("ssh-ed25519"), asyncssh.generate_private_key("ssh-ed25519")
    host.write_private_key(str(keys / "host_ed25519"))
    client.write_private_key(str(keys / "client_ed25519"))
    (keys / "host_ed25519.pub").write_bytes(host.export_public_key())
    (keys / "authorized_keys").write_bytes(client.export_public_key())
    root = state / "sftp"
    root.mkdir(exist_ok=True)

    class Chroot(asyncssh.SFTPServer):
        def __init__(self, chan):
            super().__init__(chan, chroot=str(root).encode())

    async def main():
        await asyncssh.listen("127.0.0.1", port, server_host_keys=[str(keys / "host_ed25519")],
                              authorized_client_keys=str(keys / "authorized_keys"), sftp_factory=Chroot, allow_scp=False)
        ready.set()
        await asyncio.Event().wait()

    asyncio.run(main())


def ftps_server(state: Path, port: int, cert: Path, key: Path, ready: threading.Event) -> None:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import TLS_FTPHandler
    from pyftpdlib.servers import FTPServer

    root = state / "ftp"
    root.mkdir(exist_ok=True)
    auth = DummyAuthorizer()
    auth.add_user("pkf_ftp", "ftp-dry-run-password", str(root), perm="elr")
    TLS_FTPHandler.certfile, TLS_FTPHandler.keyfile = str(cert), str(key)
    TLS_FTPHandler.authorizer = auth
    TLS_FTPHandler.tls_control_required = TLS_FTPHandler.tls_data_required = True
    TLS_FTPHandler.passive_ports = range(61000, 61100)
    srv = FTPServer(("127.0.0.1", port), TLS_FTPHandler)
    ready.set()
    srv.serve_forever()


def main(state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    ports = {k: free_port() for k in ("https", "sftp", "ftp", "aws", "vault")}
    ca, cert, key = make_ca(state)
    evs = [threading.Event() for _ in range(4)]
    threading.Thread(target=https_server, args=(state, ports["https"], cert, key, evs[0]), daemon=True).start()
    threading.Thread(target=sftp_server, args=(state, ports["sftp"], evs[1]), daemon=True).start()
    threading.Thread(target=ftps_server, args=(state, ports["ftp"], cert, key, evs[2]), daemon=True).start()
    threading.Thread(target=vault_server, args=(state, ports["vault"], evs[3]), daemon=True).start()
    aws = subprocess.Popen([sys.executable, "-m", "moto.server", "-H", "127.0.0.1", "-p", str(ports["aws"])],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for e in evs:
        e.wait(30)
    for p in ports.values():
        wait_port(p)
    pub = (state / "keys" / "host_ed25519.pub").read_text().split()
    env = {"ports": ports, "ca_bundle": str(ca), "https_root": str(state / "https"), "sftp_root": str(state / "sftp"),
           "ftp_root": str(state / "ftp"), "host_key": f"{pub[0]} {pub[1]}", "client_key": str(state / "keys" / "client_ed25519"),
           "https_user": HTTPS_USER, "https_password": HTTPS_PASSWORD, "ftp_user": "pkf_ftp", "ftp_password": "ftp-dry-run-password",
           "aws_endpoint": f"http://127.0.0.1:{ports['aws']}", "vault_address": f"http://127.0.0.1:{ports['vault']}",
           "vault_token": VAULT_TOKEN, "vault_store": str(state / "vault.json"), "pid": os.getpid()}
    (state / "env.json").write_text(json.dumps(env, indent=2))

    def stop(*_):
        aws.terminate()
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
