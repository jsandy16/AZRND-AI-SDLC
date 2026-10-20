"""An in-process fake of the GitHub REST endpoints pkf Studio uses, backed by a real bare git repository (pushes from
the Studio land there; merges make real merge commits). ETags, pagination and refs/pull/<n>/head like GitHub.

Fidelity (C22): an open PR's files and commits are diffed against the CURRENT merge-base with its base branch (as on
GitHub, so "Update branch" does not make main's files look like the PR's); merge / squash / rebase merges with optional
head-branch deletion; the repository's allowed merge methods (`merge_methods`); fault injection with `fail_next`."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FakeGitHub:
    def __init__(self, bare: Path, owner: str = "acme", repo: str = "specs"):
        self.bare, self.owner, self.repo = Path(bare), owner, repo
        self.users: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}
        self.emails: dict[str, str] = {}          # commit e-mail → login
        self.pulls: dict[int, dict] = {}
        self.reviews: dict[int, list] = {}
        self.comments: dict[int, list] = {}
        self.check_runs: dict[str, list] = {}
        self.rules: list = []
        self.permissions: dict[str, str] = {}
        self._id = 1000
        self.calls: list[tuple[str, str]] = []
        self.merge_methods = {"merge": True, "squash": True, "rebase": True}
        self.faults: list[dict] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = f"http://127.0.0.1:{self.server.server_port}/api/v3"
        self.lock = threading.Lock()

    def close(self):
        self.server.shutdown()

    # ------------------------------------------------------------------ test helpers
    def nid(self) -> int:
        self._id += 1
        return self._id

    def add_user(self, login: str, email: str, token: str | None = None) -> dict:
        u = {"login": login, "id": self.nid(), "type": "User"}
        self.users[login] = u
        self.emails[email.lower()] = login
        if token:
            self.tokens[token] = login
        self.permissions.setdefault(login, "write")
        return u

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=str(self.bare), capture_output=True, text=True, check=True).stdout

    def sha(self, ref: str) -> str | None:
        p = subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=str(self.bare), capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None

    def review(self, n: int, login: str, state: str = "APPROVED", body: str = "", commit: str | None = None) -> dict:
        pr = self._refresh(n)
        r = {"id": self.nid(), "user": self.users[login], "state": state, "body": body, "commit_id": commit or pr["head"]["sha"],
             "submitted_at": _now(), "html_url": f"{pr['html_url']}#pullrequestreview-{self._id}"}
        self.reviews.setdefault(n, []).append(r)
        return r

    def comment_as(self, n: int, login: str, body: str) -> dict:
        pr = self.pulls[n]
        c = {"id": self.nid(), "user": self.users[login], "body": body, "created_at": _now(), "updated_at": None,
             "html_url": f"{pr['html_url']}#issuecomment-{self._id}"}
        c["updated_at"] = c["created_at"]
        self.comments.setdefault(n, []).append(c)
        return c

    def fail_next(self, path_regex: str, status: int | None = None, reset: bool = False, times: int | None = 1,
                  method: str | None = None, message: str = "fake failure") -> None:
        """The next `times` requests (None = every one) whose path matches `path_regex` (re.search on the path without
        /api/v3; optionally only `method`) fail: with HTTP `status`, or — reset=True — by closing the connection without
        an answer (the client sees a ConnectionError)."""
        self.faults.append({"re": re.compile(path_regex), "status": status, "reset": reset, "times": times,
                            "method": method, "message": message})

    def _fault(self, method: str, path: str) -> dict | None:
        for f in self.faults:
            if (f["method"] is None or f["method"] == method) and f["re"].search(path):
                if f["times"] is not None:
                    f["times"] -= 1
                    if f["times"] <= 0:
                        self.faults.remove(f)
                return f
        return None

    def set_check(self, sha: str, conclusion: str = "success", name: str = "pkf-gate"):
        self.check_runs.setdefault(sha, []).append({"name": name, "status": "completed", "conclusion": conclusion, "started_at": _now()})

    def _commit_tree(self, tree: str, parents: list[str], msg: str, author: tuple[str, str] = ("GitHub", "noreply@github.com"),
                     date: str | None = None) -> str:
        env = {"GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1], "GIT_COMMITTER_NAME": "GitHub",
               "GIT_COMMITTER_EMAIL": "noreply@github.com", "PATH": "/usr/bin:/bin", **({"GIT_AUTHOR_DATE": date} if date else {})}
        args = ["git", "commit-tree", tree, *[x for p in parents for x in ("-p", p)], "-m", msg]
        return subprocess.run(args, cwd=str(self.bare), capture_output=True, text=True, check=True, env=env).stdout.strip()

    def merge(self, n: int, title: str = "merge", method: str = "merge", delete_branch: bool = False) -> str:
        """Merge PR n like GitHub's buttons: "merge" (a merge commit), "squash" (one commit with the PR's changes on top
        of the base) or "rebase" (each PR commit replayed on top of the base, merge commits dropped). GitHub is the
        committer of the new commits. delete_branch: "Automatically delete head branches"."""
        pr = self._refresh(n)
        base, head = pr["base"]["ref"], pr["head"]["sha"]
        tip = self.sha(f"refs/heads/{base}")
        pr["_base_sha"] = self.git("merge-base", tip, head).strip()  # what the PR showed when it was merged
        if method == "merge":
            tree = self.git("merge-tree", "--write-tree", tip, head).split()[0]
            sha = self._commit_tree(tree, [tip, head], title)
        elif method == "squash":
            tree = self.git("merge-tree", "--write-tree", tip, head).split()[0]
            who = self.git("log", "-1", "--format=%an%x00%ae", head).strip().split("\x00")
            sha = self._commit_tree(tree, [tip], f"{title} (#{n})", (who[0], who[1]))
        elif method == "rebase":
            sha = tip
            for c in self.git("rev-list", "--reverse", "--no-merges", f"{tip}..{head}").split():
                tree = self.git("merge-tree", "--write-tree", f"--merge-base={c}^", sha, c).split()[0]
                an, ae, ad, msg = self.git("log", "-1", "--format=%an%x00%ae%x00%aI%x00%B", c).split("\x00", 3)
                sha = self._commit_tree(tree, [sha], msg.strip() or f"rebase {c[:7]}", (an, ae), ad)
        else:
            raise ValueError(f"unknown merge method {method}")
        self.git("update-ref", f"refs/heads/{base}", sha)
        pr.update(state="closed", merged=True, merged_at=_now(), merge_commit_sha=sha)
        if delete_branch:
            self.git("update-ref", "-d", f"refs/heads/{pr['head']['ref']}")
        return sha

    # ------------------------------------------------------------------ state
    def _refresh(self, n: int) -> dict:
        pr = self.pulls[n]
        if pr["state"] == "open":
            sha = self.sha(f"refs/heads/{pr['head']['ref']}")
            if sha:
                pr["head"]["sha"] = sha
                self.git("update-ref", f"refs/pull/{n}/head", sha)
        return pr

    def _merge_base(self, pr: dict) -> str:
        """Open PR: the current merge-base with the base branch (GitHub's three-dot diff); merged or closed: as it was."""
        if pr["state"] == "open":
            tip = self.sha(f"refs/heads/{pr['base']['ref']}")
            if tip:
                return self.git("merge-base", tip, pr["head"]["sha"]).strip()
        return pr["_base_sha"]

    def _commits(self, n: int) -> list:
        pr = self._refresh(n)
        base = self._merge_base(pr)
        out = []
        for ln in self.git("log", "--format=%H%x00%ae%x00%ce%x00%cI%x00%s", f"{base}..{pr['head']['sha']}").splitlines():
            sha, ae, ce, cdate, msg = ln.split("\x00")
            au, cu = self.emails.get(ae.lower()), self.emails.get(ce.lower())
            out.append({"sha": sha, "commit": {"message": msg, "committer": {"date": cdate}}, "author": self.users.get(au) if au else None,
                        "committer": self.users.get(cu) if cu else None})
        return list(reversed(out))

    def _files(self, n: int) -> list:
        pr = self._refresh(n)
        names = self.git("diff", "--name-only", self._merge_base(pr), pr["head"]["sha"]).splitlines()
        return [{"filename": f} for f in names if f]

    def _public(self, pr: dict) -> dict:
        return {k: v for k, v in pr.items() if not k.startswith("_")}

    # ------------------------------------------------------------------ HTTP
    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _user(self):
                auth = self.headers.get("Authorization", "")
                tok = auth.split(" ", 1)[1] if " " in auth else ""
                login = fake.tokens.get(tok)
                return fake.users.get(login) if login else None

            def _send(self, code: int, data=None, headers: dict | None = None):
                body = json.dumps(data).encode() if data is not None else b""
                etag = '"' + hashlib.sha1(body).hexdigest() + '"'
                if self.command == "GET" and code == 200 and self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                if self.command == "GET":
                    self.send_header("ETag", etag)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _page(self, items: list, url):
                q = parse_qs(url.query)
                per = int(q.get("per_page", ["30"])[0])
                page = int(q.get("page", ["1"])[0])
                chunk = items[(page - 1) * per: page * per]
                headers = {}
                if page * per < len(items):
                    nxt = f"http://127.0.0.1:{fake.server.server_port}{url.path}?per_page={per}&page={page + 1}"
                    headers["Link"] = f'<{nxt}>; rel="next"'
                self._send(200, chunk, headers)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PATCH(self):
                self._route("PATCH")

            def do_PUT(self):
                self._route("PUT")

            def _route(self, method):
                url = urlparse(self.path)
                path = url.path.removeprefix("/api/v3")
                fake.calls.append((method, path))
                with fake.lock:
                    fault = fake._fault(method, path)
                if fault:
                    if fault["reset"]:
                        self.close_connection = True
                        try:
                            import socket

                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return None
                    return self._send(fault["status"] or 500, {"message": fault["message"]})
                me = self._user()
                if me is None:
                    return self._send(401, {"message": "Bad credentials"})
                rp = f"/repos/{fake.owner}/{fake.repo}"
                with fake.lock:
                    try:
                        return self._dispatch(method, path, url, me, rp)
                    except KeyError as exc:
                        return self._send(404, {"message": f"Not Found {exc}"})

            def _dispatch(self, method, path, url, me, rp):
                if path == "/user":
                    return self._send(200, me)
                m = re.fullmatch(r"/users/([^/]+)", path)
                if m:
                    return self._send(200, fake.users[m.group(1)])
                if path == rp:
                    return self._send(200, {"full_name": f"{fake.owner}/{fake.repo}", "permissions": {"push": True, "admin": True},
                                            "allow_merge_commit": fake.merge_methods["merge"],
                                            "allow_squash_merge": fake.merge_methods["squash"],
                                            "allow_rebase_merge": fake.merge_methods["rebase"]})
                m = re.fullmatch(rp + r"/branches/(.+)", path)
                if m:
                    return self._send(200, {"name": m.group(1), "protected": bool(fake.rules)})
                m = re.fullmatch(rp + r"/rules/branches/(.+)", path)
                if m:
                    return self._send(200, fake.rules)
                m = re.fullmatch(rp + r"/collaborators/([^/]+)/permission", path)
                if m:
                    return self._send(200, {"permission": fake.permissions.get(m.group(1), "none")})
                if path == rp + "/pulls" and method == "GET":
                    q = parse_qs(url.query)
                    state = q.get("state", ["open"])[0]
                    items = [fake._public(fake._refresh(n)) for n in sorted(fake.pulls)]
                    items = [p for p in items if state == "all" or p["state"] == state]
                    if q.get("head"):  # exact owner:branch, like GitHub
                        owner, _, ref = q["head"][0].partition(":")
                        items = [p for p in items if owner == fake.owner and p["head"]["ref"] == ref]
                    return self._page(items, url)
                if path == rp + "/pulls" and method == "POST":
                    b = self._body()
                    head_sha = fake.sha(f"refs/heads/{b['head']}")
                    if not head_sha:
                        return self._send(422, {"message": f"head {b['head']} does not exist"})
                    if any(p["head"]["ref"] == b["head"] and p["state"] == "open" for p in fake.pulls.values()):
                        return self._send(422, {"message": "A pull request already exists"})
                    n = max(fake.pulls or [0]) + 1
                    full = {"full_name": f"{fake.owner}/{fake.repo}"}
                    pr = {"number": n, "title": b["title"], "body": b.get("body"), "state": "open", "user": me, "merged": False,
                          "merged_at": None, "merge_commit_sha": None, "html_url": f"https://github.com/{fake.owner}/{fake.repo}/pull/{n}",
                          "head": {"ref": b["head"], "sha": head_sha, "repo": full}, "base": {"ref": b["base"], "repo": full},
                          "_base_sha": fake.git("merge-base", f"refs/heads/{b['base']}", head_sha).strip()}
                    fake.pulls[n] = pr
                    fake.git("update-ref", f"refs/pull/{n}/head", head_sha)
                    return self._send(201, fake._public(pr))
                m = re.fullmatch(rp + r"/pulls/(\d+)", path)
                if m:
                    n = int(m.group(1))
                    pr = fake._refresh(n)
                    if method == "PATCH":
                        b = self._body()
                        if b.get("state") == "closed" and pr["state"] == "open":
                            pr["_closed_by"] = me
                        pr.update({k: v for k, v in b.items() if k in ("title", "body", "state")})
                    return self._send(200, fake._public(pr))
                m = re.fullmatch(rp + r"/pulls/(\d+)/merge", path)
                if m and method == "PUT":
                    n = int(m.group(1))
                    pr = fake._refresh(n)
                    b = self._body()
                    if pr["state"] != "open":
                        return self._send(405, {"message": "Pull Request is not mergeable"})
                    if b.get("sha") and b["sha"] != pr["head"]["sha"]:
                        return self._send(409, {"message": "Head branch was modified. Review and try the merge again."})
                    method = b.get("merge_method") or "merge"
                    if method not in ("merge", "squash", "rebase"):
                        return self._send(422, {"message": f"Invalid merge_method {method}"})
                    if not fake.merge_methods.get(method):
                        what = {"merge": "Merge commits", "squash": "Squash merges", "rebase": "Rebase merges"}[method]
                        return self._send(405, {"message": f"{what} are not allowed on this repository."})
                    sha = fake.merge(n, b.get("commit_title") or "merge", method=method)
                    return self._send(200, {"sha": sha, "merged": True, "message": "Pull Request successfully merged"})
                m = re.fullmatch(rp + r"/pulls/(\d+)/reviews", path)
                if m:
                    return self._page(fake.reviews.get(int(m.group(1)), []), url)
                m = re.fullmatch(rp + r"/pulls/(\d+)/commits", path)
                if m:
                    return self._page(fake._commits(int(m.group(1))), url)
                m = re.fullmatch(rp + r"/pulls/(\d+)/files", path)
                if m:
                    return self._page(fake._files(int(m.group(1))), url)
                m = re.fullmatch(rp + r"/issues/(\d+)", path)
                if m and method == "GET":
                    pr = fake._refresh(int(m.group(1)))
                    return self._send(200, {"number": pr["number"], "state": pr["state"], "closed_by": pr.get("_closed_by")})
                m = re.fullmatch(rp + r"/issues/(\d+)/comments", path)
                if m:
                    n = int(m.group(1))
                    if method == "POST":
                        c = fake.comment_as(n, me["login"], self._body()["body"])
                        return self._send(201, c)
                    return self._page(fake.comments.get(n, []), url)
                m = re.fullmatch(rp + r"/commits/([0-9a-f]+)/check-runs", path)
                if m:
                    return self._send(200, {"check_runs": fake.check_runs.get(m.group(1), [])})
                return self._send(404, {"message": f"no fake for {method} {path}"})

        return H


def make_repo(tmp: Path, owner: str = "acme", repo: str = "specs") -> tuple[Path, Path]:
    """(bare origin, clone). The clone's origin URL is a GitHub URL rewritten (insteadOf) to the bare repository."""
    bare = tmp / "origin.git"
    clone = tmp / "clone"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    url = f"https://github.com/{owner}/{repo}.git"
    for args in (["remote", "add", "origin", url], ["config", f"url.{bare.as_uri()}.insteadOf", url],
                 ["config", "user.email", "dev@example.com"], ["config", "user.name", "Dev One"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", *args], cwd=str(clone), check=True)
    (clone / "README.md").write_text("# specs\n")
    subprocess.run(["git", "add", "-A"], cwd=str(clone), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(clone), check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=str(clone), check=True)
    return bare, clone
