"""SFTP (paramiko). Host key pinned unless explicitly disabled for development."""
from __future__ import annotations

import base64
import io
import posixpath
import stat

from . import RemoteFile, depth_ok


def _load_key(paramiko, text: str, passphrase: str | None):
    """Private key from its text (OpenSSH or PEM): Ed25519, ECDSA or RSA."""
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase)
        except paramiko.PasswordRequiredException:
            raise PermissionError("the SFTP private key needs a passphrase (credential field passphrase)") from None
        except (paramiko.SSHException, ValueError):
            continue
    raise PermissionError("credential field private_key is not a readable Ed25519, ECDSA or RSA private key")


class SftpSession:
    def __init__(self, conn: dict, creds: dict[str, str]):
        import paramiko

        self.client = paramiko.SSHClient()
        if conn.get("host_key"):
            kind, b64 = conn["host_key"].split()[:2]
            key = paramiko.PKey.from_type_string(kind, base64.b64decode(b64))
            host = conn["host"] if conn.get("port", 22) == 22 else f"[{conn['host']}]:{conn['port']}"
            self.client.get_host_keys().add(host, kind, key)
            self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:  # insecure_skip_host_key (development only, validated in the spec)
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        pkey = None
        if creds.get("private_key"):
            pkey = _load_key(paramiko, creds["private_key"], creds.get("passphrase"))
        self.client.connect(conn["host"], port=conn.get("port", 22), username=creds.get("username"),
                            password=creds.get("password") if pkey is None else None, pkey=pkey,
                            timeout=conn.get("timeout_seconds", 60), allow_agent=False, look_for_keys=False)
        self.sftp = self.client.open_sftp()

    def check(self) -> str:
        return f"SFTP connected, cwd {self.sftp.normalize('.')}"

    def list(self, root: str, recursive: bool, max_depth: int | None) -> list[RemoteFile]:
        out, stack = [], [""]
        while stack:
            rel_dir = stack.pop()
            for a in self.sftp.listdir_attr(posixpath.join(root, rel_dir) if rel_dir else root):
                rel = f"{rel_dir}/{a.filename}" if rel_dir else a.filename
                mode = a.st_mode or 0
                if stat.S_ISLNK(mode):  # follow file links, never directory links (no loops)
                    try:
                        a = self.sftp.stat(posixpath.join(root, rel))
                        mode = a.st_mode or 0
                    except OSError:
                        continue
                    if stat.S_ISDIR(mode):
                        continue
                if stat.S_ISDIR(mode):
                    if recursive and depth_ok(rel, max_depth):
                        stack.append(rel)
                elif stat.S_ISREG(mode):
                    out.append(RemoteFile(rel, a.st_size, float(a.st_mtime) if a.st_mtime else None))
        return out

    def download(self, root: str, rel: str, dest: str) -> None:
        self.sftp.get(posixpath.join(root, rel), dest, max_concurrent_prefetch_requests=64)

    def close(self) -> None:
        self.client.close()
