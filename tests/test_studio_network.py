"""GitHub transport (K9): network errors are GitHubErrors, the job client retries what is safe to retry, the UI client
fails fast (no retries, no rate-limit sleeps, an offline switch that resets itself after 60 s), and the Studio opens
and renders from the local worktree when GitHub cannot be reached."""
from __future__ import annotations

import json
import time

import pytest
import requests

from fakegithub import FakeGitHub, make_repo
from pkf_studio import checks, teamsetup
from pkf_studio import github as github_mod
from pkf_studio.github import GitHub, GitHubError
from pkf_studio.service import Studio
from pkf_studio.stages import derive_offline
from test_studio_github import setup_project, write_g1


class _Resp:
    def __init__(self, status: int, data=None, headers: dict | None = None):
        self.status_code = status
        self._data = data
        self.content = json.dumps(data).encode() if data is not None else b""
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.text = self.content.decode()

    def json(self):
        if self._data is None:
            raise ValueError("no body")
        return self._data


class _Session:
    """A requests.Session stand-in answering from a script: each entry is an exception to raise or a _Resp."""

    def __init__(self, script):
        self.headers: dict = {}
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append((method, url))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item


def _client(script, interactive=False):
    sess = _Session(script)
    c = GitHub("https://api.example.test", "acme", "specs", "tok", session=sess, interactive=interactive)
    sleeps: list[float] = []
    c.sleep = sleeps.append
    return c, sess, sleeps


@pytest.fixture()
def gh(tmp_path, monkeypatch):
    bare, clone = make_repo(tmp_path)
    fake = FakeGitHub(bare)
    fake.add_user("dev1", "dev@example.com", token="tok-dev1")
    monkeypatch.setenv("GITHUB_TOKEN", "tok-dev1")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("PKF_STUDIO_JOB", raising=False)
    monkeypatch.setattr(teamsetup, "build_wheel", lambda dest, log=print: None)
    yield fake, clone
    fake.close()


# ---------------------------------------------------------------------------------------------- job / CLI client
def test_connection_error_is_github_error():
    c, sess, sleeps = _client([requests.exceptions.ProxyError("Unable to connect to proxy")])
    with pytest.raises(GitHubError) as ei:
        c.me()
    e = ei.value
    assert e.network and e.status is None
    assert str(e).startswith("cannot reach https://api.example.test: ProxyError: Unable to connect to proxy")
    assert str(e).endswith("— check the proxy (HTTPS_PROXY), REQUESTS_CA_BUNDLE and `source ~/pkf/env.sh`")
    assert len(sess.calls) == 4 and sleeps == [1.0, 2.0, 4.0]          # a GET: 3 more attempts with back-off
    assert c.timeout == 30


def test_get_retried_on_502_then_ok(gh):
    fake, _ = gh
    c = GitHub(fake.api, "acme", "specs", "tok-dev1")
    sleeps: list[float] = []
    c.sleep = sleeps.append
    fake.fail_next(r"^/user$", status=502, times=2)
    assert c.me()["login"] == "dev1" and sleeps == [1.0, 2.0]
    fake.fail_next(r"^/user$", reset=True)                               # a dropped connection, then fine
    assert c.me()["login"] == "dev1"
    fake.fail_next(r"^/user$", status=503, times=None)                   # never recovers: the 503 is reported
    with pytest.raises(GitHubError) as ei:
        c.me()
    assert ei.value.status == 503 and not ei.value.network
    fake.faults.clear()
    fake.fail_next(r"^/user$", status=404)                               # 4xx: no retry
    n = len(fake.calls)
    with pytest.raises(GitHubError, match="404"):
        c.me()
    assert len(fake.calls) == n + 1


def test_create_pull_retry_returns_existing_pr(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "net_pipe"
    write_g1(s, p)
    s.commit(p, "S1")
    s.push(p)
    branch = f"pkf/{p}/g1"
    c = GitHub(fake.api, "acme", "specs", "tok-dev1")
    c.sleep = lambda x: None
    pr = c.create_pull("t", branch, "main", "b")
    assert pr["head"]["ref"] == branch
    fake.pulls[pr["number"]]["state"] = "closed"                         # start over with no open PR
    fake.fail_next(r"/pulls$", status=502, method="POST")                # fails before creating: retried once
    pr2 = c.create_pull("t", branch, "main", "b")
    assert pr2["number"] == pr["number"] + 1 and fake.pulls[pr2["number"]]["state"] == "open"
    # the request reached GitHub but the connection dropped: the open PR of the branch is returned, not a second one
    fake.pulls[pr2["number"]]["state"] = "closed"
    n = max(fake.pulls)

    class _DropAfterCreate:
        def __init__(self, inner):
            self.inner, self.headers = inner, inner.headers

        def request(self, method, url, **kw):
            r = self.inner.request(method, url, **kw)
            if method == "POST" and url.endswith("/pulls"):
                raise requests.exceptions.ConnectionError("Connection aborted.")
            return r

    c.s = _DropAfterCreate(c.s)
    pr3 = c.create_pull("t", branch, "main", "b")
    assert pr3["number"] == n + 1 and max(fake.pulls) == n + 1          # exactly one new PR


def test_merge_retry_rereads_merged(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "merge_net"
    write_g1(s, p)
    pr = s.request_approval(p, "G1")
    s.post_approve(p, "G1")
    head = fake._refresh(pr["number"])["head"]["sha"]
    c = GitHub(fake.api, "acme", "specs", "tok-dev1")
    c.sleep = lambda x: None

    class _DropAfterMerge:
        def __init__(self, inner):
            self.inner, self.headers = inner, inner.headers

        def request(self, method, url, **kw):
            r = self.inner.request(method, url, **kw)
            if method == "PUT":
                raise requests.exceptions.ReadTimeout("read timed out")
            return r

    c.s = _DropAfterMerge(c.s)
    out = c.merge_pull(pr["number"], head, "merge it")
    assert out["merged"] and out["sha"] == fake.pulls[pr["number"]]["merge_commit_sha"]
    # a PATCH whose answer was lost: re-read, the PR is closed already
    write_g1(s, "other_net")
    s.commit("other_net", "S1")
    pr2 = s.request_approval("other_net", "G1")

    class _DropAfterPatch(_DropAfterMerge):
        def request(self, method, url, **kw):
            r = self.inner.request(method, url, **kw)
            if method == "PATCH":
                raise requests.exceptions.ConnectionError("reset by peer")
            return r

    c.s = _DropAfterPatch(c.s.inner)
    assert c.update_pull(pr2["number"], state="closed")["state"] == "closed"
    # a refused merge (405) is not retried
    fake.fail_next(r"/merge$", status=405, method="PUT")
    n = len(fake.calls)
    c.s = c.s.inner
    with pytest.raises(GitHubError, match="405"):
        c.merge_pull(pr2["number"], "0" * 40, "x")
    assert len(fake.calls) == n + 1


def test_comment_not_retried(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    write_g1(s, "c_pipe")
    pr = s.request_approval("c_pipe", "G1")
    c = GitHub(fake.api, "acme", "specs", "tok-dev1")
    c.sleep = lambda x: pytest.fail("a comment is never retried")
    before = len(fake.comments.get(pr["number"], []))
    fake.fail_next(r"/comments$", status=502, method="POST")
    with pytest.raises(GitHubError, match="502"):
        c.comment(pr["number"], "/approve")
    fake.fail_next(r"/comments$", reset=True, method="POST")
    with pytest.raises(GitHubError) as ei:
        c.comment(pr["number"], "/approve")
    assert ei.value.network
    assert len(fake.comments.get(pr["number"], [])) == before


def test_check_runs_raises_on_403(gh):
    fake, _ = gh
    c = GitHub(fake.api, "acme", "specs", "tok-dev1")
    c.sleep = lambda x: None
    assert c.check_runs("a" * 40) == []                                  # really none
    fake.fail_next(r"/check-runs$", status=403, message="Resource not accessible by personal access token")
    with pytest.raises(GitHubError, match=r"^the token cannot read checks \(Checks: read\)"):
        c.check_runs("a" * 40)
    fake.fail_next(r"/check-runs$", reset=True, times=None)
    with pytest.raises(GitHubError) as ei:
        c.check_runs("a" * 40)
    assert ei.value.network
    fake.faults.clear()


# ---------------------------------------------------------------------------------------------- the Studio without GitHub
def test_studio_open_survives_network_error(gh, monkeypatch):
    fake, clone = gh
    setup_project(fake, clone)
    sess = _Session([requests.exceptions.ConnectionError("Max retries exceeded (Caused by ProxyError)")])
    s = Studio.open(clone, session=sess, interactive=True)
    assert s.configured and s.login is None
    assert s.gh_network and s.gh_error.startswith("cannot reach ") and "ProxyError" in s.gh_error
    assert any("cannot reach" in w for w in s.warnings)
    cs = checks.run(s, aws=False)
    tok = next(c for c in cs if c.name == "GitHub token")
    assert not tok.ok and not tok.required and "read-only until it is reachable" in tok.detail
    assert not any(c.name == "GitHub token" for c in checks.blocking(cs))
    # a token problem still stops the app
    bad = Studio.open(clone, token="wrong-token", interactive=True)
    assert not bad.gh_network and "401" in bad.gh_error
    tok = next(c for c in checks.run(bad, aws=False) if c.name == "GitHub token")
    assert tok.required and not tok.ok


def test_derive_offline_marks_unknown(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "off_pipe"
    write_g1(s, p)
    s.commit(p, "S1")
    st = derive_offline(s, p, "cannot reach x")
    by = st["by_gate"]
    assert by["G1"].state == "unknown" and by["G1"].note == "GitHub unreachable: cannot reach x"
    assert [by[g].state for g in ("G2", "G3", "G4", "G5")] == ["not_started"] * 4
    assert by["RUN"].state == "blocked" and st["current"] == "G1" and st["offline"]
    assert not by["G1"].done


# ---------------------------------------------------------------------------------------------- UI client (R8)
def test_ui_client_fails_fast_when_unreachable(gh, monkeypatch):
    fake, clone = gh
    setup_project(fake, clone)
    clock = [1000.0]

    class _Time:  # the GitHub module's clock only (git and the fake server keep the real one)
        time = staticmethod(lambda: clock[0])
        sleep = staticmethod(lambda x: clock.__setitem__(0, clock[0] + x))

    monkeypatch.setattr(github_mod, "time", _Time)
    sess = _Session([requests.exceptions.ConnectTimeout("connect timeout=5")])
    t0 = clock[0]
    s = Studio.open(clone, session=sess, interactive=True)
    assert s.gh.timeout == (5, 20) and s.gh.offline and len(sess.calls) == 1
    p = "ui_pipe"
    with pytest.raises(GitHubError) as ei:                                 # the PR list: no second network attempt
        s.prs(p, 0)
    assert ei.value.network and len(sess.calls) == 1
    st = derive_offline(s, p, s.gh_error)                                  # what app.main renders instead
    assert st["by_gate"]["RUN"].state == "blocked"
    for _ in range(5):                                                     # every button that needs GitHub: at once
        with pytest.raises(GitHubError):
            s.gh.repo_info()
    assert len(sess.calls) == 1 and clock[0] - t0 < 1.0                   # no sleeps, no waiting
    # Retry: one new attempt
    s.reset_offline()
    assert len(sess.calls) == 2 and s.gh.offline
    # after 60 s the switch resets itself
    clock[0] += 61
    with pytest.raises(GitHubError):
        s.gh.me()
    assert len(sess.calls) == 3
    # the job client of the same situation retries
    jc, jsess, sleeps = _client([requests.exceptions.ConnectTimeout("connect timeout")])
    with pytest.raises(GitHubError):
        jc.me()
    assert len(jsess.calls) == 4 and sleeps == [1.0, 2.0, 4.0]


def test_ui_client_never_sleeps_for_rate_limit(monkeypatch):
    reset = int(time.time()) + 600
    limited = _Resp(403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)})
    c, sess, sleeps = _client([limited], interactive=True)
    with pytest.raises(GitHubError) as ei:
        c.pulls()
    hhmm = time.strftime("%H:%M", time.localtime(reset))
    assert str(ei.value) == f"GitHub rate limit reached; retry after {hhmm} (local time)" and ei.value.status == 429
    assert sleeps == [] and len(sess.calls) == 1
    assert not c.offline                                                    # a rate limit is not "offline"
    # the job client waits (up to 60 s) and retries
    soon = _Resp(429, {"message": "slow down"}, {"Retry-After": "3"})
    jc, jsess, jsleeps = _client([soon, _Resp(200, {"login": "dev1"})])
    assert jc.me()["login"] == "dev1" and jsleeps == [3.0]
    # 5xx in the UI: one attempt, the error shown, not offline
    c, sess, sleeps = _client([_Resp(502, {"message": "Bad gateway"})], interactive=True)
    with pytest.raises(GitHubError, match="502"):
        c.me()
    assert len(sess.calls) == 1 and sleeps == [] and not c.offline
