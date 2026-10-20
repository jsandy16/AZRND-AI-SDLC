"""A small GitHub / GitHub Enterprise REST client: pull requests, reviews, comments, merges, branch rules.

requests honours HTTPS_PROXY / NO_PROXY and REQUESTS_CA_BUNDLE (the Zscaler bundle). GETs are cached with ETags (a 304
does not count against the rate limit); lists are paginated with per_page=100. The token comes from the environment or
`gh auth token` and is never written anywhere.

Network errors (K9): every `requests` exception becomes a GitHubError with `network = True`. Two profiles:
  job / CLI client (interactive=False)  timeout 30 s; GETs retried 3 times on 500/502/503/504 and network errors
      (1 s, 2 s, 4 s); a failed PATCH or merge re-reads the PR and returns it when the change is already there, else
      retries once; a failed create_pull looks up the open PR of the branch, else retries once; comments are never
      retried; rate limits are waited out up to 60 s.
  UI client (interactive=True)  timeout (5, 20); no retries; never sleeps for a rate limit; the first network error
      switches the client offline: every call fails at once with that message until reset_offline() (Retry / Refresh
      from GitHub) or 60 s later.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any

from .gates import Evidence, PRView, parse_comment


class GitHubError(Exception):
    def __init__(self, msg: str, status: int | None = None, network: bool = False):
        super().__init__(msg)
        self.status = status
        self.network = network


RETRY_STATUS = (500, 502, 503, 504)
BACKOFF = (1.0, 2.0, 4.0)
OFFLINE_RESET_S = 60.0
NETWORK_HINT = " — check the proxy (HTTPS_PROXY), REQUESTS_CA_BUNDLE and `source ~/pkf/env.sh`"


def _transient(exc: GitHubError) -> bool:
    return exc.network or exc.status in RETRY_STATUS


def _when(s: str | None):
    import datetime as dt

    try:
        return dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def find_token() -> str | None:
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def api_url_for(host: str) -> str:
    return "https://api.github.com" if host in ("github.com", "www.github.com") else f"https://{host}/api/v3"


class GitHub:
    def __init__(self, api: str, owner: str, repo: str, token: str | None, session=None, interactive: bool = False):
        import requests

        self.api = api.rstrip("/")
        self.owner, self.repo = owner, repo
        self.s = session or requests.Session()
        try:
            self.s.headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                                   "User-Agent": "pkf-studio"})
            if token:
                self.s.headers["Authorization"] = f"Bearer {token}"
        except AttributeError:  # a test double without headers
            pass
        self.interactive = interactive
        self.timeout: float | tuple[float, float] = (5, 20) if interactive else 30
        self.offline: str | None = None   # UI client: the network error that switched it offline
        self._offline_at = 0.0
        self.sleep = time.sleep            # monkeypatchable in tests
        self.clock = time.time
        self._etags: dict[str, tuple[str, Any, dict]] = {}

    # ------------------------------------------------------------------ transport
    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.api}{path}"

    def reset_offline(self) -> None:
        """Leave the offline state (Retry / Refresh from GitHub)."""
        self.offline = None
        self._offline_at = 0.0

    def _check_offline(self) -> None:
        if self.offline and self.clock() - self._offline_at > OFFLINE_RESET_S:
            self.reset_offline()  # try the network again after a minute
        if self.offline:
            raise GitHubError(self.offline, None, network=True)

    def _network_error(self, exc: Exception) -> GitHubError:
        msg = f"cannot reach {self.api}: {type(exc).__name__}: {exc}"[:400] + NETWORK_HINT
        if self.interactive:
            self.offline, self._offline_at = msg, self.clock()
        return GitHubError(msg, None, network=True)

    def _rate_limit_error(self, r) -> GitHubError:
        import datetime as dt

        if r.headers.get("Retry-After"):
            at = self.clock() + float(r.headers["Retry-After"])
        else:
            at = float(r.headers.get("X-RateLimit-Reset") or self.clock() + 60)
        when = dt.datetime.fromtimestamp(at).strftime("%H:%M")
        return GitHubError(f"GitHub rate limit reached; retry after {when} (local time)", 429)

    def _req(self, method: str, path: str, **kw) -> tuple[Any, dict]:
        import requests

        if self.interactive:
            self._check_offline()
        url = self._url(path)
        headers = kw.pop("headers", {})
        if method == "GET" and url in self._etags:
            headers["If-None-Match"] = self._etags[url][0]
        retry = method == "GET" and not self.interactive
        r = None
        for attempt in range(4):
            try:
                r = self.s.request(method, url, headers=headers, timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                err = self._network_error(exc)
                if retry and attempt < 3:
                    self.sleep(BACKOFF[attempt])
                    continue
                raise err from None
            if r.status_code == 304 and url in self._etags:
                return self._etags[url][1], self._etags[url][2]
            if r.status_code in (403, 429) and (r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Remaining") == "0"):
                if self.interactive:
                    raise self._rate_limit_error(r)
                wait = float(r.headers.get("Retry-After") or max(1, int(r.headers.get("X-RateLimit-Reset", self.clock() + 5)) - self.clock()))
                if attempt < 3 and wait <= 60:
                    self.sleep(wait)
                    continue
            if retry and r.status_code in RETRY_STATUS and attempt < 3:
                self.sleep(BACKOFF[attempt])
                continue
            break
        if r.status_code >= 400:
            msg = ""
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = r.text[:200]
            hint = {401: " (token missing or expired)", 403: " (token lacks a permission, or is not SSO-authorized for the org)",
                    404: " (not found, or the token cannot see it)"}.get(r.status_code, "")
            raise GitHubError(f"GitHub {method} {path}: {r.status_code} {msg}{hint}", r.status_code)
        data = r.json() if r.content and r.headers.get("Content-Type", "").startswith("application/json") else None
        if method == "GET" and r.headers.get("ETag"):
            self._etags[url] = (r.headers["ETag"], data, dict(r.headers))
        return data, dict(r.headers)

    def get(self, path: str, **params) -> Any:
        return self._req("GET", path, params=params or None)[0]

    def pages(self, path: str, **params) -> list:
        out, url, first = [], path, True
        while url:
            data, headers = self._req("GET", url, params={"per_page": 100, **params} if first else None)
            out += data or []
            first = False
            m = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", "") or headers.get("link", ""))
            url = m.group(1) if m else None
        return out

    def _r(self, suffix: str) -> str:
        return f"/repos/{self.owner}/{self.repo}{suffix}"

    # ------------------------------------------------------------------ calls
    def me(self) -> dict:
        return self.get("/user")

    def user(self, login: str) -> dict:
        return self.get(f"/users/{login}")

    def repo_info(self) -> dict:
        return self.get(self._r(""))

    def branch(self, name: str) -> dict:
        return self.get(self._r(f"/branches/{name}"))

    def branch_rules(self, name: str) -> list:
        try:
            return self.get(self._r(f"/rules/branches/{name}")) or []
        except GitHubError as exc:
            if exc.status == 404:
                return []
            raise

    def permission(self, login: str) -> str:
        try:
            return (self.get(self._r(f"/collaborators/{login}/permission")) or {}).get("permission", "none")
        except GitHubError as exc:
            if exc.status in (403, 404):
                return "unknown"
            raise

    def pulls(self, state: str = "all") -> list:
        return self.pages(self._r("/pulls"), state=state)

    def pull(self, n: int) -> dict:
        return self.get(self._r(f"/pulls/{n}"))

    def open_pull_for(self, branch: str) -> dict | None:
        """The open PR whose head is this repository's `branch` (exact match), or None."""
        rows = self.get(self._r("/pulls"), state="open", head=f"{self.owner}:{branch}") or []
        return next((p for p in rows if (p.get("head") or {}).get("ref") == branch), None)

    def create_pull(self, title: str, head: str, base: str, body: str) -> dict:
        payload = {"title": title, "head": head, "base": base, "body": body}
        try:
            return self._req("POST", self._r("/pulls"), json=payload)[0]
        except GitHubError as exc:
            if self.interactive or not _transient(exc):
                raise
            try:  # the PR may have been created before the connection dropped
                found = self.open_pull_for(head)
            except GitHubError:
                found = None
            if found:
                return found
            return self._req("POST", self._r("/pulls"), json=payload)[0]

    def update_pull(self, n: int, **fields) -> dict:
        try:
            return self._req("PATCH", self._r(f"/pulls/{n}"), json=fields)[0]
        except GitHubError as exc:
            if self.interactive or not _transient(exc):
                raise
            pr = self.pull(n)
            if all(pr.get(k) == v for k, v in fields.items()) or (fields.get("state") == "closed" and pr.get("state") == "closed"):
                return pr  # the change is already there
            return self._req("PATCH", self._r(f"/pulls/{n}"), json=fields)[0]

    def merge_pull(self, n: int, sha: str, title: str, method: str = "merge") -> dict:
        payload = {"sha": sha, "commit_title": title, "merge_method": method}
        try:
            return self._req("PUT", self._r(f"/pulls/{n}/merge"), json=payload)[0]
        except GitHubError as exc:
            if self.interactive or not _transient(exc):
                raise
            pr = self.pull(n)
            if pr.get("merged") or pr.get("merged_at"):
                return {"sha": pr.get("merge_commit_sha"), "merged": True, "message": "Pull Request successfully merged"}
            return self._req("PUT", self._r(f"/pulls/{n}/merge"), json=payload)[0]

    def comment(self, n: int, body: str) -> dict:
        return self._req("POST", self._r(f"/issues/{n}/comments"), json={"body": body})[0]

    def reviews(self, n: int) -> list:
        return self.pages(self._r(f"/pulls/{n}/reviews"))

    def comments(self, n: int) -> list:
        return self.pages(self._r(f"/issues/{n}/comments"))

    def pr_commits(self, n: int) -> list:
        return self.pages(self._r(f"/pulls/{n}/commits"))

    def pr_files(self, n: int) -> list:
        return self.pages(self._r(f"/pulls/{n}/files"))

    def check_runs(self, sha: str) -> list:
        """The check runs on a commit. Raises on any error (so "missing" means missing); 401/403 → "the token cannot
        read checks (Checks: read)"."""
        try:
            return (self.get(self._r(f"/commits/{sha}/check-runs")) or {}).get("check_runs", [])
        except GitHubError as exc:
            if exc.status in (401, 403):
                raise GitHubError(f"the token cannot read checks (Checks: read): {exc}", exc.status) from None
            raise

    def issue(self, n: int) -> dict:
        return self.get(self._r(f"/issues/{n}"))

    # ------------------------------------------------------------------ gate inputs
    def pr_view(self, n: int) -> tuple[PRView, dict]:
        from . import PROTECTED_PATHS

        pr = self.pull(n)
        commits = self.pr_commits(n)
        ids: set[int] = set()
        unlinked = False
        for c in commits:
            for who in ("author", "committer"):
                u = c.get(who)
                if u and u.get("id") is not None:
                    if u.get("login") != "web-flow":  # GitHub's own committer for web merges
                        ids.add(int(u["id"]))
                elif who == "author":
                    unlinked = True
        files = sorted({x for f in self.pr_files(n) for x in (f.get("filename"), f.get("previous_filename")) if x})
        touched = [f for f in files if f.startswith(PROTECTED_PATHS)]
        view = PRView(number=n, head_sha=pr["head"]["sha"], author_id=(pr.get("user") or {}).get("id"),
                      author_login=(pr.get("user") or {}).get("login", ""), commit_user_ids=ids, unlinked_commit=unlinked,
                      state=pr["state"], merged=bool(pr.get("merged") or pr.get("merged_at")), html_url=pr.get("html_url"),
                      touched_protected=touched, files=files,
                      too_many_commits=int(pr.get("commits") or len(commits)) > 250 or len(commits) >= 250)
        return view, pr

    def evidence(self, n: int, check_write: bool = True) -> list[Evidence]:
        out: list[Evidence] = []
        for r in self.reviews(n):
            u = r.get("user") or {}
            out.append(Evidence("review", r["id"], u.get("id"), u.get("login", ""), r.get("state", ""), r.get("commit_id"),
                                r.get("body") or "", r.get("submitted_at"), False, r.get("html_url")))
        perms: dict[str, bool] = {}
        commits = None
        for c in self.comments(n):
            kind, arg = parse_comment(c.get("body") or "")
            if kind == "OTHER":
                continue
            u = c.get("user") or {}
            login = u.get("login", "")
            if check_write and login not in perms and kind != "UNRECOGNISED":
                perm = self.permission(login)  # None when GitHub will not say (403/404): comments only count in dev mode
                perms[login] = True if perm in ("admin", "maintain", "write") else None if perm == "unknown" else False
            if kind == "APPROVE" and not arg:  # plain /approve: the head at the time of the comment
                if commits is None:
                    commits = self.pr_commits(n)
                at = _when(c.get("created_at"))
                before = [x["sha"] for x in commits if _when(((x.get("commit") or {}).get("committer") or {}).get("date")) <= at]
                arg = before[-1] if before else None
            out.append(Evidence("comment", c["id"], u.get("id"), login, kind, arg if kind == "APPROVE" else None,
                                c.get("body") or "", c.get("created_at"),
                                bool(c.get("updated_at") and c.get("created_at") and c["updated_at"] != c["created_at"]),
                                c.get("html_url"), perms.get(login, True)))
        return out
