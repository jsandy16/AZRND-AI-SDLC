"""FTP / FTPS (explicit TLS, certificate verified, protected data channel)."""
from __future__ import annotations

import datetime as dt
import ftplib
import posixpath
import ssl

from . import RemoteFile, SourceError, depth_ok


class FtpSession:
    def __init__(self, conn: dict, creds: dict[str, str]):
        timeout = conn.get("timeout_seconds", 60)
        if conn.get("tls", "explicit") == "explicit":
            ctx = ssl.create_default_context(cafile=conn.get("ca_bundle"))
            self.ftp: ftplib.FTP = ftplib.FTP_TLS(context=ctx, timeout=timeout)
        else:
            self.ftp = ftplib.FTP(timeout=timeout)
        self.ftp.connect(conn["host"], conn.get("port", 21))
        self.ftp.login(creds.get("username", "anonymous"), creds.get("password", ""))
        if isinstance(self.ftp, ftplib.FTP_TLS):
            self.ftp.prot_p()
        self.ftp.set_pasv(conn.get("passive", True))

    def check(self) -> str:
        return f"FTP connected: {self.ftp.pwd()}"

    def list(self, root: str, recursive: bool, max_depth: int | None) -> list[RemoteFile]:
        out, stack, seen = [], [""], set()
        while stack:
            rel_dir = stack.pop()
            path = posixpath.join(root, rel_dir) if rel_dir else root
            try:
                entries = list(self.ftp.mlsd(path, facts=["type", "size", "modify", "unique"]))
            except ftplib.error_perm as exc:
                if str(exc)[:3] in ("500", "501", "502", "504"):
                    raise SourceError(f"the FTP server does not support MLSD listings (RFC 3659), which pkf needs ({str(exc)[:60]})") from None
                raise
            for name, facts in entries:
                if name in (".", ".."):
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                t = facts.get("type", "").lower()
                if t == "dir":
                    uid = facts.get("unique")
                    if uid and uid in seen:  # a linked folder seen before: no loops, no duplicates
                        continue
                    seen.add(uid)
                    if recursive and depth_ok(rel, max_depth):
                        stack.append(rel)
                elif t == "file":
                    mod = facts.get("modify")
                    mtime = dt.datetime.strptime(mod[:14], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp() if mod else None
                    out.append(RemoteFile(rel, int(facts["size"]) if facts.get("size") else None, mtime))
        return out

    def download(self, root: str, rel: str, dest: str) -> None:
        with open(dest, "wb") as fh:
            self.ftp.retrbinary(f"RETR {posixpath.join(root, rel)}", fh.write)

    def close(self) -> None:
        try:
            self.ftp.quit()
        except Exception:  # noqa: BLE001
            self.ftp.close()
