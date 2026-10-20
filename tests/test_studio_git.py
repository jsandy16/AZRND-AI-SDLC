"""Studio git and GitHub workflow (0.5.0): pushing stage work after every job (K2) and the empty commit that resets
approvals when an open PR's head moves (C4 rule 1), G4 readiness from tests.json v2 and dags/ left by an earlier build
(R10, Restore dags/ from main), `pkf studio verify` for G4 (re-test of custom components, draft build; K3/R2) and G5
(`build --check`), and GitHub's merge / squash / rebase merges (fake GitHub, C22)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from fakegithub import FakeGitHub, make_repo
from pkf_studio import service, teamsetup
from pkf_studio import verify as V
from pkf_studio.gitops import Git, GitError
from pkf_studio.service import Studio, StudioError, dags_problem, readiness
from pkf_studio.stages import derive
from test_studio_github import setup_project, write_g1


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


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=True).stdout


def _iso(seconds_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()


def _other_clone(fake: FakeGitHub, tmp: Path, name: str = "other") -> Path:
    """A second machine (or GitHub's web editor): a separate clone of the origin."""
    d = tmp / name
    subprocess.run(["git", "clone", "-q", str(fake.bare), str(d)], check=True)
    for args in (["config", "user.email", "dev@example.com"], ["config", "user.name", "Dev One"], ["config", "commit.gpgsign", "false"]):
        _git(d, *args)
    return d


# ---------------------------------------------------------------------------------------------- §4.1, small units
def test_plan_problems_reports_components_that_do_not_load(tmp_path):
    p = "rd_pipe"
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / f"{p}.yaml").write_text(yaml.safe_dump({"connection": "c", "requirement": "x"}))
    (tmp_path / "connections").mkdir()
    (tmp_path / "connections" / "c.yaml").write_text(yaml.safe_dump({"source": "s", "destination": "d"}))
    (tmp_path / "destinations").mkdir()
    (tmp_path / "destinations" / "d.yaml").write_text("type: s3\n")
    probs = service._plan_problems(tmp_path, p)
    assert any(x.startswith("source s: ") and "source 's' not found" in x for x in probs), probs
    assert any(x.startswith("destination d: ") and "destination 'd' not found" in x for x in probs), probs
    assert all(x in readiness(tmp_path, p, "G3") for x in probs)


def test_commit_allow_empty(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    g = Git(repo)
    (repo / ".gitignore").write_text(".pkf-studio/\n")                                   # the lock file lives there
    (repo / "a.txt").write_text("a\n")
    first = g.commit(repo, "first", "Dev One", "dev@example.com")
    assert first and g.commit(repo, "nothing", "Dev One", "dev@example.com") is None
    (repo / "dirty.txt").write_text("not mine to commit\n")
    empty = g.commit(repo, "empty", "Dev One", "dev@example.com", paths=[], allow_empty=True)
    assert empty and empty != first
    assert _git(repo, "show", "--name-only", "--format=", empty).strip() == ""       # changes no file
    assert "?? dirty.txt" in _git(repo, "status", "--porcelain")                       # uncommitted work stays as it is
    with_all = g.commit(repo, "everything", "Dev One", "dev@example.com", allow_empty=True)
    assert "dirty.txt" in _git(repo, "show", "--name-only", "--format=", with_all)
    assert g.commit(repo, "again", "Dev One", "dev@example.com", allow_empty=True)     # nothing changed: still a commit


# ---------------------------------------------------------------------------------------------- K2 push after every job
def test_push_quiet_reports_rejection_without_raising(gh, tmp_path, monkeypatch):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "push_pipe"
    write_g1(s, p)
    s.commit(p, "S1 work")
    branch = f"pkf/{p}/g1"
    assert not fake.sha(f"refs/heads/{branch}")                         # stage work is laptop-only so far (C1)
    assert s.push_quiet(p) is None
    assert fake.sha(f"refs/heads/{branch}") == s.git.head(s.worktree_path(p))
    before = s.git.head(s.worktree_path(p))
    assert s.push_quiet(p) is None and s.git.head(s.worktree_path(p)) == before    # no PR open: no extra commit
    # GitHub's copy moves (a second machine, or 'Update branch'); this computer commits too
    other = _other_clone(fake, tmp_path)
    _git(other, "checkout", "-q", branch)
    (other / "requirements" / p / "from-elsewhere.md").write_text("x\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "elsewhere")
    _git(other, "push", "-q", "origin", branch)
    (s.worktree_path(p) / "requirements" / p / "here.md").write_text("y\n")
    local = s.commit(p, "here")
    msg = s.push_quiet(p)
    assert msg == (f"GitHub's copy of {branch} changed (someone pushed or used 'Update branch'): "
                   "Workflow → GitHub's copy changed: Take GitHub's copy, or Merge GitHub's copy into mine")
    assert service.DIVERGED_FIX.startswith(f"Workflow → {service.DIVERGED_CARD}: {service.TAKE_LABEL}")
    assert s.git.head(s.worktree_path(p)) == local                      # nothing lost, nothing forced
    assert fake.sha(f"refs/heads/{branch}") != local
    # any other failure: a message, never an exception
    monkeypatch.setattr(s.git, "push", lambda cwd, b: (_ for _ in ()).throw(
        GitError("git push -q -u…: fatal: unable to access 'https://github.com/acme/specs.git/': Could not resolve host")))
    msg = s.push_quiet(p)
    assert msg.startswith(f"could not push {branch}: git push") and "Could not resolve host" in msg
    assert msg.endswith("your work is committed on this computer; it is pushed by the next job or by Request approval")


def test_request_approval_reports_a_moved_remote_as_github_copy_changed(gh, tmp_path):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "req_pipe"
    write_g1(s, p)
    s.request_approval(p, "G1")
    branch = f"pkf/{p}/g1"
    other = _other_clone(fake, tmp_path)
    _git(other, "checkout", "-q", branch)
    (other / "requirements" / p / "web.md").write_text("x\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "web edit")
    _git(other, "push", "-q", "origin", branch)
    (s.worktree_path(p) / "requirements" / p / "local.md").write_text("y\n")
    with pytest.raises(StudioError, match="GitHub's copy of pkf/req_pipe/g1 changed"):
        s.request_approval(p, "G1")


def _plain_approve(fake: FakeGitHub, n: int, seconds_ago: float) -> None:
    c = fake.comment_as(n, "dev1", "/approve")                           # typed on GitHub, no commit id
    c["created_at"] = c["updated_at"] = _iso(seconds_ago)


@pytest.mark.parametrize("how", ["request_approval", "push_quiet"])
def test_plain_approve_before_push_does_not_approve_the_new_head(gh, monkeypatch, how):
    """r4: the reviewer approves what GitHub shows; a commit made 20 s before the /approve and pushed 5 s after it
    must not be covered by it."""
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "r4_pipe"
    monkeypatch.setenv("GIT_COMMITTER_DATE", _iso(120))                # the reviewed head is two minutes old
    write_g1(s, p)
    pr = s.request_approval(p, "G1")
    reviewed = fake._refresh(pr["number"])["head"]["sha"]
    wt = s.worktree_path(p)
    monkeypatch.setenv("GIT_COMMITTER_DATE", _iso(25))                 # a late edit, committed but not pushed yet
    (wt / "requirements" / p / "late-change.md").write_text("changed after the reviewer looked\n")
    late = s.commit(p, "late edit")
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    _plain_approve(fake, pr["number"], seconds_ago=5)                  # 20 s after the late commit, 5 s before the push
    res, _ = s.gate_status(p, "G1")
    assert res.passed and fake._refresh(pr["number"])["head"]["sha"] == reviewed      # it approves what it saw
    if how == "request_approval":                                      # "Update the PR"
        s.request_approval(p, "G1")
    else:                                                              # the end of a stage job
        assert s.push_quiet(p) is None
    s.invalidate()
    new_head = fake._refresh(pr["number"])["head"]["sha"]
    assert new_head != late and _git(wt, "rev-parse", f"{new_head}^").strip() == late
    assert _git(wt, "show", "--name-only", "--format=%s", new_head).strip() == "G1 Requirements: work pushed (approvals reset)"
    res, _ = s.gate_status(p, "G1")
    assert not res.passed and any("older commit" in n for n in res.notes), (res.summary(), res.notes)
    _plain_approve(fake, pr["number"], seconds_ago=-2)                 # written after the push: approves the new head
    res, _ = s.gate_status(p, "G1")
    assert res.passed, res.summary()
    # the head did not move this time: no second empty commit
    assert s.push_quiet(p) is None and fake._refresh(pr["number"])["head"]["sha"] == new_head


# ---------------------------------------------------------------------------------------------- G4 readiness, dags/
def _tests_json(wt: Path, p: str, doc: dict) -> None:
    f = wt / "review" / p / "tests.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc))


def test_g4_readiness_messages(tmp_path):
    p = "g4_pipe"
    assert readiness(tmp_path, p, "G4") == ["run the tests and the build first"]
    issues = [{"severity": "warning", "msg": "a warning"}, {"severity": "error", "msg": "destination 'd' is not ready"},
              {"severity": "error", "msg": "second"}, {"severity": "error", "msg": "third"}]
    _tests_json(tmp_path, p, {"build_ok": False, "build_issues": issues})                      # 0.4.0 (version 1)
    assert readiness(tmp_path, p, "G4") == [
        "the build failed: destination 'd' is not ready; second — Run tests & build again, then see the S4 status panel"]
    _tests_json(tmp_path, p, {"version": 2, "build_mode": "draft", "build_ok": False, "build_issues": issues, "dags_differ": True})
    assert readiness(tmp_path, p, "G4") == ["the draft build failed: destination 'd' is not ready; second — see the S4 status panel",
                                            dags_problem("main")]
    _tests_json(tmp_path, p, {"version": 2, "build_mode": "draft", "build_ok": True, "build_issues": [], "dags_differ": True})
    assert readiness(tmp_path, p, "G4", base="trunk") == [
        "dags/ on this branch comes from an earlier build and differs from the one on trunk; it is rebuilt at S5 — "
        "remove it with Restore dags/ from trunk"]
    assert readiness(tmp_path, p, "G4", dags_diff=[]) == []                                    # live state wins
    _tests_json(tmp_path, p, {"version": 2, "build_ok": True, "dags_differ": False})
    assert readiness(tmp_path, p, "G4") == [] and readiness(tmp_path, p, "G4", dags_diff=["dags/pkf_g4_pipe.py"]) == [dags_problem()]


def test_restore_dags_from_base(gh, tmp_path, monkeypatch):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "dg_pipe"
    # main has a DAG for this pipeline (an earlier S5); g4 carries a different one, a module main does not have,
    # and an untracked leftover — a 0.4.0 real build committed at S4
    other = _other_clone(fake, tmp_path)
    (other / "dags").mkdir()
    (other / "dags" / f"pkf_{p}.py").write_text("# built from approved code\n")
    _git(other, "checkout", "-q", "-b", "add-dag")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "dag")
    _git(other, "push", "-q", "origin", "add-dag")
    fake.merge(s.gh.create_pull("dag", "add-dag", "main", "")["number"])
    s.fetch(force=True)
    wt = s.open_stage(p, "G4")
    (wt / "dags" / f"pkf_{p}.py").write_text("# built by 0.4.0 from unapproved code\n")
    (wt / "dags" / "pkf" / p).mkdir(parents=True)
    (wt / "dags" / "pkf" / p / "destination_d.py").write_text("def f(): pass\n")
    (wt / "review" / p).mkdir(parents=True)
    (wt / "review" / p / "keep.md").write_text("S4 notes\n")
    _tests_json(wt, p, {"version": 2, "build_mode": "draft", "build_ok": True, "build_issues": [], "dags_differ": True,
                        "problems": [dags_problem(), "something else"]})
    s.commit(p, "S4 dg_pipe: tests, build and capability summary")
    (wt / "dags" / "pkf" / p / "leftover.py").write_text("x = 1\n")                       # untracked
    (wt / "review" / p / "unsaved.md").write_text("the user's unsaved edit\n")            # not dags/: left alone
    assert s.dags_diff(p) == [f"dags/pkf/{p}/destination_d.py", f"dags/pkf/{p}/leftover.py", f"dags/pkf_{p}.py"]
    assert dags_problem() in readiness(wt, p, "G4", dags_diff=s.dags_diff(p))
    with pytest.raises(StudioError, match="not ready for review: .*Restore dags/ from main"):
        s.request_approval(p, "G4")
    # a job running for the pipeline: refused (the job itself may restore)
    from pkf_studio import jobs

    monkeypatch.setattr(jobs, "current", lambda root, pipeline: "job-1")
    with pytest.raises(StudioError, match="a job is running for dg_pipe"):
        s.restore_dags_from_base(p)
    monkeypatch.setenv("PKF_STUDIO_JOB", "job-1")
    changed = s.restore_dags_from_base(p)
    monkeypatch.setattr(jobs, "current", lambda root, pipeline: None)
    monkeypatch.delenv("PKF_STUDIO_JOB")
    assert changed == [f"dags/pkf/{p}/destination_d.py", f"dags/pkf/{p}/leftover.py", f"dags/pkf_{p}.py"]
    assert _git(wt, "log", "-1", "--format=%s").strip() == f"S4 {p}: dags/ restored from main"
    assert (wt / "dags" / f"pkf_{p}.py").read_text() == "# built from approved code\n"
    assert not (wt / "dags" / "pkf" / p).exists()
    assert s.dags_diff(p) == []
    t = json.loads((wt / "review" / p / "tests.json").read_text())
    assert t["dags_differ"] is False and t["problems"] == ["something else"]
    assert readiness(wt, p, "G4", dags_diff=s.dags_diff(p)) == []
    assert _git(wt, "status", "--porcelain").strip() == f"?? review/{p}/unsaved.md"
    assert s.restore_dags_from_base(p) == []                                                # nothing left to restore


# ---------------------------------------------------------------------------------------------- verify for G4 / G5
ROUTE = '''def route_keys(layout, params, ctx):
    for path in layout["files"]:
        study, rest = path.split("/", 1)
        if "/cdar/" in "/" + rest:
            layout["keys"][path] = study + "/primary/" + rest.rsplit("/", 1)[-1]
        else:
            layout["keys"][path] = "error/" + path
    return layout'''
WRONG = ROUTE.replace('study + "/primary/"', 'study + "/secondary/"')
STEPS = [{"id": "route_keys", "op": "custom", "title": "Route by path rules", "params": {"function": "route_keys"},
          "pseudocode": "1. study = first segment\n2. /cdar/ → {study}/primary/{file}\n3. otherwise error/{path}"}]
CASES = {"route_keys": [{"name": "cdar_and_error", "purpose": "both rules", "input": {"files": ["S1/r/cdar/a.txt", "S1/x/b.txt"]},
                         "params": {}, "expected": ["S1/primary/a.txt", "error/S1/x/b.txt"], "expect_error": False}]}


def _write(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(doc, sort_keys=False))


def _pipeline_files(wt: Path, p: str) -> None:
    conn = {"bucket": "lake-bucket", "region": "eu-west-1"}
    _write(wt / "sources" / "vr_src.yaml", {"api_version": "pkf/v1", "kind": "source", "name": "vr_src", "type": "s3", "connection": conn,
                                            "steps": [{"id": "all", "op": "list", "title": "Everything under incoming/",
                                                       "params": {"root": "incoming/"}},
                                                      {"id": "one_each", "op": "split", "title": "One package per file",
                                                       "params": {"by": "regex", "regex": "(.+)", "var": "file"}}]})
    _write(wt / "destinations" / "vr_dst.yaml", {"api_version": "pkf/v1", "kind": "destination", "name": "vr_dst", "type": "s3",
                                                 "connection": conn, "requirement": "Route every file.\n",
                                                 "examples": [{"name": "cdar", "files": ["S1/r/cdar/a.txt"], "expected_keys": ["S1/primary/a.txt"]}]})
    _write(wt / "connections" / "vr_conn.yaml", {"api_version": "pkf/v1", "kind": "connection", "name": "vr_conn",
                                                 "source": "vr_src", "destination": "vr_dst"})
    _write(wt / "pipelines" / f"{p}.yaml", {"api_version": "pkf/v1", "kind": "pipeline", "name": p, "owner": "t", "connection": "vr_conn",
                                            "schedule": "@daily", "start_date": "2026-10-01", "package_name": "{file}",
                                            "examples": [{"name": "two", "listing": ["incoming/S1/r/cdar/a.txt:1", "incoming/S1/x/b.txt:1"],
                                                          "expected_keys": ["S1/primary/a.txt", "error/S1/x/b.txt"]}]})


def _code_by_hand(wt: Path, code: str) -> None:
    """What `pkf plan` + approve-plan + `pkf code` produce at S4, with a hand-written plan and code (stage code_generated)."""
    from pkf import design
    from pkf.repo import load

    repo = load(wt)
    spec = repo.get("destination", "vr_dst")
    lock = {"lock_version": 2, "kind": "destination", "name": "vr_dst", "requirement_sha256": design.requirement_sha("destination", spec),
            "plan": {"summary": "hand-written", "steps": STEPS, "assumptions": [], "ambiguities": [], "step_tests": CASES},
            "simulation": design.run_examples_inprocess("destination", spec, STEPS)}
    lock["plan_digest"] = design.plan_digest(lock)
    design.save_lock(repo, "destination", "vr_dst", lock)
    design.approve_plan(repo, "destination", "vr_dst", "@tester")
    lock = design.load_lock(repo, "destination", "vr_dst")
    design.code_path(repo, "destination", "vr_dst").write_text(design.render_module("destination", "vr_dst", lock, {"route_keys": code}),
                                                               encoding="utf-8", newline="\n")
    lock["code"] = {"file": "vr_dst.py", "plan_digest": lock["plan_digest"], "generated_by": {}, "generated_at": "now"}
    design.save_lock(repo, "destination", "vr_dst", lock)
    design.test_code(repo, "destination", "vr_dst")
    assert design.status(repo, "destination", "vr_dst")["stage"] in ("code_generated", "code_failing")


def _lie_about_tests(wt: Path, code: str) -> None:
    """Swap in code that fails its tests but keep the lock's recorded results ('pass') and fix up the file hash."""
    cp = wt / "destinations" / "vr_dst.py"
    cp.write_text(cp.read_text(encoding="utf-8").replace(ROUTE, code), encoding="utf-8", newline="\n")
    lf = wt / "destinations" / "vr_dst.lock.yaml"
    lock = yaml.safe_load(lf.read_text(encoding="utf-8"))
    lock["code"]["sha256"] = hashlib.sha256(cp.read_bytes()).hexdigest()
    lf.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8", newline="\n")


def _g4_pr(s: Studio, fake: FakeGitHub, p: str, gate: str, prepare) -> tuple[int, Path]:
    """A stage PR for `gate` with the pipeline's files, approved with /approve <sha>; returns (number, head checkout)."""
    wt = s.open_stage(p, gate)
    _pipeline_files(wt, p)
    prepare(wt)
    s.commit(p, f"{gate} work")
    s.push(p)
    n = s.gh.create_pull(f"{gate}", f"pkf/{p}/{gate.lower()}", "main", "")["number"]
    fake.comment_as(n, "dev1", f"/approve {s.git.head(wt)[:12]}")
    head = s.root.parent / f"head-{p}-{gate.lower()}"
    s.git.run("worktree", "add", "--detach", str(head), s.git.head(wt))
    return n, head


def _draft_supported() -> bool:
    """`pkf build --draft` is WP2's (0.5.0 Phase 1); until it is in this tree a draft build is a usage error."""
    from pkf.cli import parser

    try:
        parser().parse_args(["build", "x", "--draft"])
        return True
    except SystemExit:
        return False


def test_verify_g4_uses_draft_and_g5_uses_check(gh, monkeypatch):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "vf_pipe"
    calls: list[list[str]] = []
    real = subprocess.run

    def run(argv, *a, **kw):
        if list(argv[:4]) == [sys.executable, "-P", "-m", "pkf.cli"]:
            calls.append(list(argv))
            data = {"status": {"stage": "code_generated"}} if argv[5] == "test" and argv[6] != "pipeline" else {}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "issues": [], "data": data}), "")
        return real(argv, *a, **kw)

    def prepare(wt):  # a destination whose plan has a custom step (contents do not matter: pkf is faked here)
        _write(wt / "destinations" / "vr_dst.lock.yaml", {"plan": {"steps": STEPS}})

    n4, head4 = _g4_pr(s, fake, p, "G4", prepare)
    n5, head5 = _g4_pr(s, fake, "vg_pipe", "G5", prepare)
    monkeypatch.setattr(V.subprocess, "run", run)
    ok4, lines4 = V.verify(s.gh, n4, s.root, head4, base_ref="origin/main")
    pre = [sys.executable, "-P", "-m", "pkf.cli", "--json"]
    assert calls == [pre + ["test", "destination", "vr_dst"], pre + ["test", "pipeline", p], pre + ["build", p, "--draft"]]
    assert ok4, lines4
    assert "pkf test destination vr_dst: OK (code_generated)" in lines4 and f"pkf build {p} --draft: OK" in lines4
    calls.clear()
    ok5, lines5 = V.verify(s.gh, n5, s.root, head5, base_ref="origin/main")
    assert calls == [pre + ["test", "pipeline", "vg_pipe"], pre + ["build", "vg_pipe", "--check"]]
    assert ok5, lines5


def test_verify_g4_retests_custom_components(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    draft = _draft_supported()

    def build_line_ok(lines, p):
        return f"pkf build {p} --draft: OK" in lines

    # an honest head: the code passes its tests (stage code_generated, not approved yet: the draft build accepts it)
    n, head = _g4_pr(s, fake, "hon_pipe", "G4", lambda wt: _code_by_hand(wt, ROUTE))
    ok, lines = V.verify(s.gh, n, s.root, head, base_ref="origin/main")
    assert "pkf test destination vr_dst: OK (code_generated)" in lines, lines
    assert not any("the code fails its own tests" in x for x in lines)
    assert "pkf test pipeline hon_pipe: OK" in lines, lines
    if draft:
        assert ok and build_line_ok(lines, "hon_pipe"), lines
    else:  # documented: until WP2's `build --draft` lands only the draft build line fails
        assert not ok and [x for x in lines if x.startswith("pkf build")] == ["pkf build hon_pipe --draft: FAILED"]
    # a head whose lock claims passing tests for code that fails them → FAILED (CI does not trust the lock)
    n, head = _g4_pr(s, fake, "lie_pipe", "G4", lambda wt: (_code_by_hand(wt, ROUTE), _lie_about_tests(wt, WRONG)))
    ok, lines = V.verify(s.gh, n, s.root, head, base_ref="origin/main")
    assert not ok
    bad = [x for x in lines if x.startswith("FAIL: destination vr_dst: the code fails its own tests when re-run (")]
    assert bad and "cdar" in bad[0], lines

    # the same lie with a code approval recorded over it
    def approved_lie(wt):
        from pkf import design
        from pkf.repo import load

        _code_by_hand(wt, ROUTE)
        _lie_about_tests(wt, WRONG)
        design.approve_code(load(wt), "destination", "vr_dst", "@reviewer")

    n, head = _g4_pr(s, fake, "apl_pipe", "G4", approved_lie)
    ok, lines = V.verify(s.gh, n, s.root, head, base_ref="origin/main")
    assert not ok and any(x.startswith("FAIL: destination vr_dst: the code fails its own tests when re-run") for x in lines), lines

    # passing code whose recorded code approval does not match the re-tested code digest
    def stale_approval(wt):
        _code_by_hand(wt, ROUTE)
        lf = wt / "destinations" / "vr_dst.lock.yaml"
        lock = yaml.safe_load(lf.read_text(encoding="utf-8"))
        lock["code_approval"] = {"by": "@reviewer", "at": "2026-10-01T00:00:00Z", "digest": "0" * 64}
        lf.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8", newline="\n")

    n, head = _g4_pr(s, fake, "sta_pipe", "G4", stale_approval)
    ok, lines = V.verify(s.gh, n, s.root, head, base_ref="origin/main")
    assert not ok and any(x.startswith("FAIL: destination vr_dst: the code approval does not match the re-tested code") for x in lines), lines


# ---------------------------------------------------------------------------------------------- GitHub merges (C22)
@pytest.mark.parametrize("method", ["squash", "rebase"])
@pytest.mark.parametrize("via", ["github_button", "api"])
def test_github_merge_methods_lead_to_the_next_stage(gh, method, via):
    """Merged on GitHub (or through the API) with squash or rebase and the head branch deleted: the gate is approved, the
    next stage starts with the ledger row, and its PR builds on the approval."""
    fake, clone = gh
    s = setup_project(fake, clone)
    p = f"m_{method}_{via}"
    write_g1(s, p)
    pr = s.request_approval(p, "G1")
    (s.worktree_path(p) / "requirements" / p / "second.md").write_text("a second commit\n")
    s.request_approval(p, "G1")
    s.post_approve(p, "G1")
    head = fake._refresh(pr["number"])["head"]["sha"]
    if via == "api":
        s.gh.merge_pull(pr["number"], head, "merge it", method=method)
    else:
        fake.merge(pr["number"], "merge it", method=method, delete_branch=True)
        assert not fake.sha(f"refs/heads/pkf/{p}/g1")
    merged = fake.pulls[pr["number"]]
    tip = fake.sha("refs/heads/main")
    assert merged["merge_commit_sha"] == tip and merged["merged_at"]
    parents = _git(fake.bare, "rev-list", "--parents", "-n", "1", tip).split()[1:]
    assert len(parents) == 1                                                  # no merge commit
    assert _git(fake.bare, "diff", "--name-only", f"{head}", tip).strip() == ""  # same content as the approved head
    if method == "rebase":
        assert len(_git(fake.bare, "rev-list", f"{parents[0]}..{tip}").split()) >= 1
        assert _git(fake.bare, "log", "-1", "--format=%cn", tip).strip() == "GitHub"
    s.invalidate()
    s.fetch(force=True)
    st = derive(s, p, s.prs(p, 0))
    assert st["by_gate"]["G1"].state == "approved" and st["current"] == "G2", [(g.gate, g.state, g.note) for g in st["gates"]]
    wt = s.open_stage(p, "G2")
    s.write_ledger(p, s.ledger_row(p, "G1"))
    (wt / "review" / p).mkdir(parents=True, exist_ok=True)
    (wt / "review" / p / "design.md").write_text("S2\n")
    pr2 = s.request_approval(p, "G2")
    s.post_approve(p, "G2")
    res, _ = s.gate_status(p, "G2")
    assert res.passed, res.summary()
    st = derive(s, p, s.prs(p, 0))
    assert st["by_gate"]["G2"].state == "waiting" and st["by_gate"]["G2"].pr["number"] == pr2["number"]
    assert s.git.is_ancestor(merged["merge_commit_sha"], fake._refresh(pr2["number"])["head"]["sha"])


# ---------------------------------------------------------------------------------------------- E13 AWS identity
def test_aws_identity_messages(tmp_path, monkeypatch):
    from moto import mock_aws

    from pkf_studio import checks

    for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(k, raising=False)
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy", "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_WEB_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(k, raising=False)  # the result must not depend on a proxy or container credentials
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    ok, msg, ident = checks.aws_identity("eu-north-1", None)
    assert (ok, ident) == (False, None)
    assert msg == ("no valid AWS credentials (NoCredentialsError): run aws sso login --profile <your profile> "
                   "(or source ~/pkf/env.sh), then try again")
    monkeypatch.setenv("AWS_PROFILE", "rd-dev")
    (tmp_path / "no-config").write_text("[profile rd-dev]\nregion = eu-north-1\n", encoding="utf-8")
    ok, msg, _ = checks.aws_identity("eu-north-1", None)
    assert not ok and "--profile rd-dev " in msg and msg.startswith("no valid AWS credentials (")
    monkeypatch.delenv("AWS_PROFILE")
    # expired session token: STS answers ExpiredToken
    from botocore.exceptions import ClientError

    class _Expired:
        def get_caller_identity(self):
            raise ClientError({"Error": {"Code": "ExpiredToken", "Message": "The security token included in the request is expired"}},
                              "GetCallerIdentity")
    import boto3
    with monkeypatch.context() as m:
        m.setattr(boto3.session.Session, "client", lambda self, *a, **k: _Expired())
        ok, msg, _ = checks.aws_identity("eu-north-1", None)
    assert not ok and msg.startswith("no valid AWS credentials (ExpiredToken): run aws sso login")
    # moto STS: a valid identity, then a mismatch with the pinned account
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ok, msg, ident = checks.aws_identity("eu-north-1", None)
        assert ok and ident["Account"] == "123456789012" and msg.endswith("(account 123456789012)")
        assert ident["Arn"] in msg
        ok, msg, ident = checks.aws_identity("eu-north-1", "123456789012")
        assert ok
        ok, msg, ident = checks.aws_identity("eu-north-1", "999999999999")
        assert not ok and ident["Account"] == "123456789012"
        assert msg == "your AWS credentials are for account 123456789012; the project is pinned to 999999999999"
        # the startup check is cached and never required
        checks._AWS_CACHE.clear()
        first = checks.aws_identity_cached("eu-north-1", None)
        monkeypatch.setattr(checks, "aws_identity", lambda *a: pytest.fail("not cached"))
        assert checks.aws_identity_cached("eu-north-1", None) == first


# ---------------------------------------------------------------------------------------------- Phase 2 helpers
def _stage(s: Studio, fake: FakeGitHub, p: str, gate: str, *, approve: bool = True, merge: bool = True,
           github_merge: bool = False) -> dict:
    """Do one stage the way the app does: start it (the previous gate's ledger row), add a file, request approval, then
    (optionally) /approve and merge — in the app, or on GitHub (`github_merge`)."""
    from pkf_studio.gates import ORDER

    if gate == "G1":
        write_g1(s, p)
    else:
        wt = s.open_stage(p, gate)
        s.write_ledger(p, s.ledger_row(p, ORDER[ORDER.index(gate) - 1]))
        (wt / "review" / p).mkdir(parents=True, exist_ok=True)
        (wt / "review" / p / f"{gate.lower()}.md").write_text(f"{gate} work\n")
    pr = s.request_approval(p, gate)
    if approve:
        s.post_approve(p, gate)
    if merge:
        if github_merge or not approve:
            fake.merge(pr["number"], f"merge {gate}")
        else:
            s.merge(p, gate)
    s.invalidate()
    s.fetch(force=True)
    return pr


def _states(s: Studio, p: str) -> dict[str, str]:
    s.invalidate()
    return {g.gate: g.state for g in derive(s, p, s.prs(p, 0))["gates"]}


def _push_from_elsewhere(fake: FakeGitHub, tmp: Path, branch: str, name: str, p: str, clone_name: str = "other") -> str:
    other = tmp / clone_name
    if not other.exists():
        other = _other_clone(fake, tmp, clone_name)
    _git(other, "fetch", "-q", "origin")
    _git(other, "checkout", "-q", "-B", branch, f"origin/{branch}")
    f = other / "review" / p / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{name} {time.time_ns()}\n")                          # unique content: always a new commit
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", f"elsewhere: {name}")
    _git(other, "push", "-q", "origin", branch)
    return _git(other, "rev-parse", "HEAD").strip()


# ---------------------------------------------------------------------------------------------- C3 GitHub's copy changed
def test_open_stage_fast_forwards_when_behind(gh, tmp_path):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "ff_pipe"
    _stage(s, fake, p, "G1")
    _stage(s, fake, p, "G2", approve=False, merge=False)              # waiting
    branch = f"pkf/{p}/g2"
    remote = _push_from_elsewhere(fake, tmp_path, branch, "from-github.md", p)   # e.g. "Update branch" / web edit
    s.fetch(force=True)
    assert s.branch_sync(p, "G2") == {"state": "behind", "ahead": 0, "behind": 1}
    wt = s.open_stage(p, "G2")
    assert s.git.head(wt) == remote and (wt / "review" / p / "from-github.md").exists()
    assert s.branch_sync(p, "G2")["state"] == "same"
    # behind with uncommitted changes: refused, nothing moved
    remote2 = _push_from_elsewhere(fake, tmp_path, branch, "again.md", p)
    s.fetch(force=True)
    (wt / "review" / p / "unsaved.md").write_text("mine\n")
    with pytest.raises(StudioError, match="has uncommitted changes; commit or discard them, then try again"):
        s.open_stage(p, "G2")
    assert s.git.head(wt) == remote and (wt / "review" / p / "unsaved.md").exists()
    (wt / "review" / p / "unsaved.md").unlink()
    # the worktree is on another branch: the local branch is moved with branch -f
    with s.git.lock():
        s.git.run("checkout", "-q", "--detach", cwd=wt)
    s.git.run("checkout", "-q", branch, cwd=wt)
    assert s.git.head(s.open_stage(p, "G2")) == remote2
    # ahead: nothing to do (pushed by the next job)
    (wt / "review" / p / "local.md").write_text("x\n")
    s.commit(p, "local")
    assert s.branch_sync(p, "G2") == {"state": "ahead", "ahead": 1, "behind": 0}
    assert s.branch_sync(p, "G3")["state"] == "no_remote"


def test_open_stage_refuses_when_diverged(gh, tmp_path):
    from pkf_studio.service import StudioDiverged

    fake, clone = gh
    s = setup_project(fake, clone)
    p = "div_pipe"
    _stage(s, fake, p, "G1")
    _stage(s, fake, p, "G2", approve=False, merge=False)
    branch = f"pkf/{p}/g2"
    _push_from_elsewhere(fake, tmp_path, branch, "a.md", p)
    _push_from_elsewhere(fake, tmp_path, branch, "b.md", p)
    wt = s.worktree_path(p)
    (wt / "review" / p / "mine.md").write_text("local\n")
    local = s.commit(p, "local work")
    s.fetch(force=True)
    with pytest.raises(StudioDiverged) as ei:
        s.open_stage(p, "G2")
    assert str(ei.value) == (f"GitHub's copy of {branch} changed and this computer has other commits on it (1 here, 2 on "
                             "GitHub): Workflow → GitHub's copy changed: Take GitHub's copy, or Merge GitHub's copy into mine")
    assert isinstance(ei.value, StudioError) and (ei.value.ahead, ei.value.behind) == (1, 2)
    assert s.git.head(wt) == local                                       # nothing moved


def _diverge(s, fake, tmp_path, p, clone_name="other", conflict=False):
    branch = f"pkf/{p}/g2"
    remote = _push_from_elsewhere(fake, tmp_path, branch, "same.md" if conflict else "theirs.md", p, clone_name)
    wt = s.worktree_path(p)
    (wt / "review" / p / ("same.md" if conflict else "ours.md")).write_text("ours, different\n")
    local = s.commit(p, "ours")
    s.fetch(force=True)
    assert s.branch_sync(p, "G2")["state"] == "diverged"
    return branch, local, remote


def test_resolve_diverged_take_merge_overwrite(gh, tmp_path):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "res_pipe"
    _stage(s, fake, p, "G1")
    _stage(s, fake, p, "G2", approve=False, merge=False)
    wt = s.worktree_path(p)
    # take GitHub's copy: this computer's commits are kept as an archive ref
    branch, local, remote = _diverge(s, fake, tmp_path, p)
    out = s.resolve_diverged(p, "G2", "take")
    assert s.git.head(wt) == remote and out["archive"].startswith(f"refs/pkf-archive/{p}/g2-")
    assert s.git.sha(out["archive"]) == local
    assert [a["ref"] for a in s.archives(p)] == [out["archive"]] and s.archives(p)[0]["subject"] == "ours"
    # merge GitHub's copy into mine
    branch, local, remote = _diverge(s, fake, tmp_path, p)
    s.resolve_diverged(p, "G2", "merge")
    head = s.git.head(wt)
    assert s.git.is_ancestor(local, head, cwd=wt) and s.git.is_ancestor(remote, head, cwd=wt)
    assert s.branch_sync(p, "G2")["state"] == "ahead" and s.push_quiet(p) is None
    # a conflict aborts and says what to do
    branch, local, remote = _diverge(s, fake, tmp_path, p, conflict=True)
    with pytest.raises(StudioError, match=r"the two copies conflict in review/res_pipe/same.md: take GitHub's copy, or resolve it on GitHub"):
        s.resolve_diverged(p, "G2", "merge")
    assert s.git.head(wt) == local and s.git.is_clean(wt)
    # overwrite GitHub (dev only), with a lease
    s.resolve_diverged(p, "G2", "overwrite")
    assert fake.sha(f"refs/heads/{branch}") == local
    with pytest.raises(StudioError, match="unknown choice"):
        s.resolve_diverged(p, "G2", "force")


def test_merge_enabled_when_local_is_only_behind(gh, tmp_path):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "behind_pipe"
    _stage(s, fake, p, "G1")
    pr = _stage(s, fake, p, "G2", approve=False, merge=False)
    branch = f"pkf/{p}/g2"
    remote = _push_from_elsewhere(fake, tmp_path, branch, "web.md", p)   # GitHub's copy moved on
    s.post_approve(p, "G2")
    s.invalidate()
    pr_head = fake._refresh(pr["number"])["head"]["sha"]
    assert pr_head == remote and s.git.head(s.worktree_path(p)) != pr_head
    assert s.local_ahead(p, "G2", pr_head) == 0                        # only behind: Merge stays enabled
    (s.worktree_path(p) / "review" / p / "more.md").write_text("x\n")
    s.commit(p, "more local work")
    assert s.local_ahead(p, "G2", pr_head) == 1                        # real local work: Update the PR first


# ---------------------------------------------------------------------------------------------- C5 / C7 / C6 Reopen
def test_reopen_archives_later_branches(gh):
    """r2: Reopen G3 used to delete the unpushed S4 commits; now their tip stays reachable from an archive ref."""
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "arch_pipe"
    for g in ("G1", "G2", "G3"):
        _stage(s, fake, p, g)
    wt = s.open_stage(p, "G4")
    s.write_ledger(p, s.ledger_row(p, "G3"))
    (wt / "review" / p / "s4-code.md").write_text("paid-for AI code\n")
    g4_tip = s.commit(p, "S4 code")
    assert not fake.sha(f"refs/heads/pkf/{p}/g4")                      # never pushed
    s.reopen(p, "G3")
    assert not s.git.sha(f"refs/heads/pkf/{p}/g4")
    refs = [a for a in s.archives(p) if "/g4-" in a["ref"]]
    assert len(refs) == 1 and refs[0]["sha"] == g4_tip and refs[0]["subject"] == "S4 code"
    assert s.git.is_ancestor(g4_tip, refs[0]["ref"])
    assert s.worktree_branch(p) == f"pkf/{p}/g3"
    assert s.pipelines() == [p]                                         # archive refs are not branches
    assert service.gate_of_branch(refs[0]["ref"].removeprefix("refs/")) is None
    st = derive(s, p, s.prs(p, 0))
    assert st["by_gate"]["G3"].state == "in_progress"


def test_reopen_preview_counts_unpushed(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "prev_pipe"
    for g in ("G1", "G2", "G3"):
        _stage(s, fake, p, g)
    wt = s.open_stage(p, "G4")
    s.write_ledger(p, s.ledger_row(p, "G3"))
    (wt / "review" / p / "a.md").write_text("a\n")
    s.commit(p, "pushed")
    assert s.push_quiet(p) is None
    (wt / "review" / p / "b.md").write_text("b\n")
    s.commit(p, "not pushed 1")
    (wt / "review" / p / "c.md").write_text("c\n")
    tip = s.commit(p, "not pushed 2")
    rows = s.reopen_preview(p, "G3")
    assert rows == [{"branch": f"pkf/{p}/g4", "tip": tip, "unpushed": 2, "open_pr": None}], rows
    # with an open PR on g4
    s.write_ledger(p, s.ledger_row(p, "G3"))
    (wt / "review" / p / "tests.json").write_text(json.dumps({"version": 2, "build_ok": True, "build_issues": []}))
    pr = s.request_approval(p, "G4")
    rows = s.reopen_preview(p, "G3")
    assert rows[0]["open_pr"] == pr["number"] and rows[0]["unpushed"] == 0
    s.reopen(p, "G3")
    assert fake.pulls[pr["number"]]["state"] == "closed"
    assert any("Closed by pkf Studio: G3 was reopened." in c["body"] for c in fake.comments[pr["number"]])


def test_reopen_dirty_refuses_before_closing_prs(gh):
    """r9: an unsaved edit used to abort Reopen G2 only after the approved G3 PR had been closed."""
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "dirty_pipe"
    _stage(s, fake, p, "G1")
    _stage(s, fake, p, "G2")
    pr3 = _stage(s, fake, p, "G3", merge=False)                         # approved, waiting
    wt = s.worktree_path(p)
    (wt / "review" / p / "unsaved.md").write_text("an unsaved edit (untracked)\n")
    with pytest.raises(StudioError, match="uncommitted changes"):
        s.reopen(p, "G2")
    assert fake.pulls[pr3["number"]]["state"] == "open"
    assert s.git.sha(f"refs/heads/pkf/{p}/g3") and s.archives(p) == []
    (wt / "review" / p / "unsaved.md").unlink()
    s.reopen(p, "G2")
    assert fake.pulls[pr3["number"]]["state"] == "closed"


def test_fetch_failure_gives_unknown_not_stale(gh, tmp_path):
    """r11: the API works, git fetch does not: a gate merged on GitHub is unknown (no Reopen), not stale."""
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "fetch_pipe"
    _stage(s, fake, p, "G1")
    _stage(s, fake, p, "G2")
    pr3 = _stage(s, fake, p, "G3", merge=False)
    good_url = s.git.run("config", "--get-regexp", r"^url\..*\.insteadof$").split()[0]
    bare_uri = good_url[len("url."):-len(".insteadof")]
    s.git.run("config", "--unset", good_url)
    s.git.run("config", f"url.{(tmp_path / 'missing.git').as_uri()}.insteadOf", "https://github.com/acme/specs.git")
    fake.merge(pr3["number"], "merged on GitHub")
    s.invalidate()
    s.fetch(force=True)
    assert s.fetch_error
    st = derive(s, p, s.prs(p, 0))
    g3 = st["by_gate"]["G3"]
    assert g3.state == "unknown" and g3.note.startswith("the base branch could not be fetched: ") and g3.note.endswith(" — Retry")
    assert st["by_gate"]["G4"].state == "blocked" and st["by_gate"]["RUN"].state == "blocked" and st["current"] == "G3"
    assert st["by_gate"]["G1"].state == "approved"                       # merges this computer has are fine
    with pytest.raises(StudioError, match="the base branch could not be fetched"):
        s.reopen(p, "G3")
    s.git.run("config", "--unset", f"url.{(tmp_path / 'missing.git').as_uri()}.insteadOf")
    s.git.run("config", f"url.{bare_uri}.insteadOf", "https://github.com/acme/specs.git")
    s.fetch(force=True)
    assert s.fetch_error is None and _states(s, p)["G3"] == "approved"


def test_untracked_file_blocks_branch_switch(gh):
    """r12: a stray file is not carried to the next branch and committed to the wrong gate."""
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "untr_pipe"
    _stage(s, fake, p, "G1")
    wt = s.worktree_path(p)
    stray = wt / "destinations" / "x.py"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("left by a crashed pkf code\n")
    assert not s.git.is_clean(wt)
    with pytest.raises(GitError, match="uncommitted changes"):
        s.open_stage(p, "G2")
    assert s.worktree_branch(p) == f"pkf/{p}/g1" and stray.exists()
    s.discard(p)
    assert not stray.exists() and s.git.is_clean(wt)
    s.open_stage(p, "G2")
    assert s.worktree_branch(p) == f"pkf/{p}/g2"


# ---------------------------------------------------------------------------------------------- K14 merged without approval
def test_merged_unapproved_blocks_later_gates(gh, monkeypatch):
    """r10: G3 merged on GitHub without /approve. Start S4 fails with MergedUnapproved; derive shows G3
    merged_unapproved and everything after it blocked; Post /approve + Check again approves it."""
    from pkf_studio.service import MergedUnapproved

    fake, clone = gh
    s = setup_project(fake, clone)
    p = "mu_pipe"
    _stage(s, fake, p, "G1")
    _stage(s, fake, p, "G2")
    pr3 = _stage(s, fake, p, "G3", approve=False)                       # merged on GitHub, nobody approved
    calls = len(fake.calls)
    assert _states(s, p)["G3"] == "approved"                            # lazy: unknown until a start finds out
    assert not any("/reviews" in c[1] or "/comments" in c[1] for c in fake.calls[calls:])  # no approval calls per render
    s.open_stage(p, "G4")
    with pytest.raises(MergedUnapproved) as ei:
        s.ledger_row(p, "G3")
    e = ei.value
    assert (e.gate, e.pr, e.merge_sha) == ("G3", pr3["number"], fake.pulls[pr3["number"]]["merge_commit_sha"])
    assert str(e).startswith(f"G3 PR #{pr3['number']} was merged without the required approval (")
    marker = json.loads((s.root / ".pkf-studio/cache/merged-unapproved" / f"{p}.json").read_text(encoding="utf-8"))
    assert marker["gate"] == "G3" and marker["pr"] == pr3["number"] and marker["summary"]
    st = derive(s, p, s.prs(p, 0))
    by = st["by_gate"]
    assert (by["G3"].state, by["G4"].state, by["G5"].state, by["RUN"].state) == ("merged_unapproved", "blocked", "blocked", "blocked")
    assert st["current"] == "G3" and by["G3"].note == marker["summary"] and not by["G3"].done
    assert s.recheck_merged(p) is False                                  # Check again: still unapproved
    mfile = s.root / ".pkf-studio/cache/merged-unapproved" / f"{p}.json"
    saved = mfile.read_text(encoding="utf-8")
    head = fake.pulls[pr3["number"]]["head"]["sha"]
    c = fake.comment_as(pr3["number"], "dev1", f"/approve {head[:12]}")  # approved on GitHub, then Check again
    assert s.recheck_merged(p) is True and not mfile.exists()
    fake.comments[pr3["number"]].remove(c)
    mfile.write_text(saved, encoding="utf-8")
    assert _states(s, p)["G3"] == "merged_unapproved"
    s.post_approve(p, "G3")                                              # in the app: works on a merged PR and checks again
    assert not mfile.exists()
    assert _states(s, p)["G3"] == "approved"
    row = s.ledger_row(p, "G3")                                          # Start S4 now succeeds
    assert row["gate"] == "G3" and row["pr"] == pr3["number"]
    assert s.recheck_merged(p) is None


# ---------------------------------------------------------------------------------------------- C10 worktree repair
def test_worktree_self_repairs_after_repo_move(gh, tmp_path):
    """r5a: the clone folder was moved (OneDrive, a rename): the worktree's absolute links broke."""
    import shutil

    fake, clone = gh
    s = setup_project(fake, clone)
    p = "mv_pipe"
    write_g1(s, p)
    s.commit(p, "S1")
    moved = tmp_path / "moved-clone"
    shutil.move(str(clone), str(moved))
    s2 = Studio.open(moved)
    wt = s2.worktree_path(p)
    assert wt.exists() and not s2.git._healthy(wt)
    out = s2.git.repair_worktree(p)
    assert out["repaired"] and not out["moved_aside"]
    assert s2.worktree_branch(p) == f"pkf/{p}/g1"
    shutil.move(str(moved), str(clone))                                 # and back: the next open repairs on its own
    s3 = Studio.open(clone)
    assert s3.open_stage(p, "G1") == s3.worktree_path(p) and s3.git._healthy(s3.worktree_path(p))
    assert (s3.worktree_path(p) / "pipelines" / f"{p}.yaml").exists()


def test_worktree_without_dotgit_is_set_aside(gh):
    """r5b: the worktree's .git file was removed (antivirus, a cleanup tool): moved aside, added again."""
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "nogit_pipe"
    write_g1(s, p)
    s.commit(p, "S1")
    wt = s.worktree_path(p)
    (wt / "notes-only-here.txt").write_text("keep me\n")
    (wt / ".git").unlink()
    res = s.repair_worktree(p)
    aside = Path(res["moved_aside"])
    assert aside.name.startswith(f"{p}.broken-") and (aside / "notes-only-here.txt").read_text() == "keep me\n"
    assert res["branch"] == f"pkf/{p}/g1" and (wt / ".git").exists()
    assert (wt / "pipelines" / f"{p}.yaml").exists()
    # the same through any stage call
    (wt / ".git").unlink()
    assert s.open_stage(p, "G1") == wt and s.worktree_branch(p) == f"pkf/{p}/g1"
    # a folder that cannot be moved aside: a clear message
    (wt / ".git").unlink()
    import shutil as _sh
    real = _sh.move

    def refuse(a, b):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")
    try:
        _sh.move = refuse
        with pytest.raises(GitError, match=r"is broken and cannot be moved aside \(close programs using it, e.g. VS Code\)"):
            s.git.repair_worktree(p)
    finally:
        _sh.move = real


# ---------------------------------------------------------------------------------------------- K11 locks
def test_git_lock_with_reused_pid_is_stale(tmp_path):
    import os

    import psutil

    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    g = Git(repo)
    lk = repo / ".pkf-studio" / "locks" / "git.lock"
    lk.parent.mkdir(parents=True)
    me = psutil.Process(os.getpid()).create_time()
    lk.write_text(f"{os.getpid()} tok {me}")                       # alive: this very process
    assert not g._holder_dead()
    lk.write_text(f"{os.getpid()} tok {me - 3600}")                # same pid, another process's create time: reused
    assert g._holder_dead()
    lk.write_text(f"{os.getpid()} tok")                            # 0.4.0 two-field form: pid only
    assert not g._holder_dead()
    lk.write_text("999999999 tok 1.0")                             # gone
    assert g._holder_dead()
    lk.write_text(f"{os.getpid()} tok {me}")
    old = time.time() - 31 * 60
    os.utime(lk, (old, old))                                        # older than 30 minutes
    assert g._holder_dead()
    lk.write_text(f"{os.getpid()} tok {me - 3600}")
    with g.lock(timeout=2):                                         # taken over at once
        assert lk.read_text().split()[0] == str(os.getpid()) and len(lk.read_text().split()) == 3
    assert not lk.exists()


def test_stale_index_lock_removed(gh, monkeypatch):
    import os

    from pkf_studio import jobs

    fake, clone = gh
    s = setup_project(fake, clone)
    p = "lock_pipe"
    wt = write_g1(s, p)
    il = s.git.index_lock(wt)
    il.write_text("")                                               # left by a killed `git add`
    with pytest.raises(StudioError, match="index.lock"):          # fresh: it may belong to a running git
        s.commit(p, "S1")
    old = time.time() - 120
    os.utime(il, (old, old))
    monkeypatch.setattr(jobs, "current", lambda root, pipeline: "job-other")
    with pytest.raises(StudioError, match="index.lock"):          # another job of this pipeline runs: left alone
        s.commit(p, "S1")
    monkeypatch.setenv("PKF_STUDIO_JOB", "job-other")              # the job running now cleans up after a killed one
    assert s.commit(p, "S1") and not il.exists()
    monkeypatch.delenv("PKF_STUDIO_JOB")
    monkeypatch.setattr(jobs, "current", lambda root, pipeline: None)
    il.write_text("")
    os.utime(il, (old, old))
    (wt / "review" / p).mkdir(parents=True, exist_ok=True)
    (wt / "review" / p / "x.md").write_text("x\n")
    s.discard(p)                                                     # reset / clean: the leftover is removed first
    assert not il.exists()


def test_service_refuses_merge_and_reopen_while_a_job_runs(gh, monkeypatch):
    from pkf_studio import jobs

    fake, clone = gh
    s = setup_project(fake, clone)
    p = "busy_pipe"
    _stage(s, fake, p, "G1")
    _stage(s, fake, p, "G2", merge=False)
    monkeypatch.setattr(jobs, "current", lambda root, pipeline: "job-1" if pipeline == p else None)
    msg = f"a job is running for {p}; wait for it to finish"
    for call in (lambda: s.merge(p, "G2"), lambda: s.reopen(p, "G1"), lambda: s.request_approval(p, "G2"),
                 lambda: s.discard(p), lambda: s.post_approve(p, "G2"), lambda: s.resolve_diverged(p, "G2", "take"),
                 lambda: s.repair_worktree(p), lambda: s.restore_dags_from_base(p)):
        with pytest.raises(StudioError, match=msg):
            call()
    assert fake.pulls[max(fake.pulls)]["state"] == "open"
    monkeypatch.setenv("PKF_STUDIO_JOB", "job-1")                    # the job itself (run_local merges its record PR)
    assert s.merge(p, "G2")
    monkeypatch.delenv("PKF_STUDIO_JOB")
    # without PKF_STUDIO_JOB: an inline job (same process) is the job itself, another process's job is not
    d = s.root / ".pkf-studio" / "jobs" / "job-1"
    d.mkdir(parents=True)
    (d / "cmd.json").write_text("{}")
    monkeypatch.setenv("PKF_STUDIO_INLINE_JOBS", "1")
    s._idle(p)
    (d / "pid").write_text("999999999 1.0")
    with pytest.raises(StudioError, match=msg):
        s._idle(p)
    import os
    (d / "pid").write_text(f"{os.getpid()} 1.0")
    s._idle(p)


# ---------------------------------------------------------------------------------------------- C15 C16 C17 C20 C21 E15
def test_token_hints_per_mode():
    from pkf_studio import checks

    dev = checks.token_permissions("dev")
    assert dev == ["Contents: read and write", "Pull requests: read and write", "Metadata: read"]
    for mode in ("sit", "ppt", "prd"):
        strict = checks.token_permissions(mode)
        assert strict[:3] == dev and any(x.startswith("Checks: read") for x in strict)
        assert any(x.startswith("Workflows: read and write") for x in strict)
        assert any(x.startswith("Administration: read") for x in strict)
    assert "Checks" not in checks.token_fix("dev") and "Workflows" in checks.token_fix("sit")


def test_merge_uses_an_allowed_method(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "sq_pipe"
    fake.merge_methods = {"merge": False, "squash": True, "rebase": False}
    write_g1(s, p)
    pr = s.request_approval(p, "G1")
    s.post_approve(p, "G1")
    assert s.merge_methods() == ["squash"]
    s.merge(p, "G1")
    tip = fake.sha("refs/heads/main")
    assert len(_git(fake.bare, "rev-list", "--parents", "-n", "1", tip).split()) == 2     # one parent: squashed
    assert fake.pulls[pr["number"]]["merge_commit_sha"] == tip
    assert _states(s, p)["G1"] == "approved"
    # GitHub refuses (settings changed since the app looked): a clear message with what is allowed
    write_g1(s, "sq_two")
    s.request_approval("sq_two", "G1")
    s.post_approve("sq_two", "G1")
    fake.merge_methods = {"merge": False, "squash": False, "rebase": True}
    with pytest.raises(StudioError, match=r"^GitHub refused the merge: .*Squash merges are not allowed.*; this repository "
                                          r"allows rebase: merge on GitHub$"):
        s.merge("sq_two", "G1")


def test_config_written_only_when_different(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    g = Git(repo)
    writes = []
    real = g.run

    def spy(*args, **kw):
        if args and args[0] == "config" and "--get" not in args and "--local" not in args[:2]:
            writes.append(args)
        return real(*args, **kw)
    monkeypatch.setattr(g, "run", spy)
    g.ensure_settings()
    assert sorted(writes) == [("config", "core.autocrlf", "false"), ("config", "core.longpaths", "true")]
    writes.clear()
    g.ensure_settings()
    g.ensure_settings()
    assert writes == []                                             # already right: read only, no lock, no write
    real("config", "core.autocrlf", "true")
    g.ensure_settings()
    assert writes == [("config", "core.autocrlf", "false")]
    lk = repo / ".pkf-studio" / "locks" / "git.lock"
    assert not lk.exists()


def test_unmerged_local_commits_after_github_merge(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "um_pipe"
    _stage(s, fake, p, "G1")
    pr2 = _stage(s, fake, p, "G2", merge=False)
    wt = s.worktree_path(p)
    (wt / "review" / p / "replan.md").write_text("a re-plan after the PR was approved\n")
    s.commit(p, "re-plan")                                          # not pushed
    assert s.unmerged_local_commits(p, "G2") == 0                  # not merged yet
    fake.merge(pr2["number"], "merged on GitHub at the old head", delete_branch=True)
    s.invalidate()
    s.fetch(force=True)
    assert s.unmerged_local_commits(p, "G2") == 1
    ref = s.archive(p, "G2")
    assert ref.startswith(f"refs/pkf-archive/{p}/g2-") and s.archives(p)[0]["subject"] == "re-plan"
    assert s.unmerged_local_commits(p, "G1") == 0


def test_unrecognised_approve_gives_note(gh):
    from pkf_studio.gates import parse_comment

    assert parse_comment("/approve lgtm") == ("UNRECOGNISED", "lgtm")
    assert parse_comment("/approve 9fc07f6 looks good") == ("UNRECOGNISED", "9fc07f6 looks good")
    assert parse_comment("/approve 9fc07f") == ("UNRECOGNISED", "9fc07f")          # 6 hex: too short
    assert parse_comment("/approve") == ("APPROVE", "")
    assert parse_comment("/approve 9FC07F6") == ("APPROVE", "9fc07f6")
    assert parse_comment("looks good\n/approve abcdef0\nApprove as: po") == ("APPROVE", "abcdef0")
    assert parse_comment("I will /approve later") == ("OTHER", None)
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "note_pipe"
    write_g1(s, p)
    pr = s.request_approval(p, "G1")
    fake.comment_as(pr["number"], "dev1", "/approve lgtm")
    res, _ = s.gate_status(p, "G1")
    assert not res.passed
    assert "@dev1 wrote '/approve lgtm', which is not recognised: write /approve or /approve <sha>" in res.notes


def test_closed_pr_note(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "closed_pipe"
    write_g1(s, p)
    pr = s.request_approval(p, "G1")
    s.gh.update_pull(pr["number"], state="closed")
    st = derive(s, p, s.prs(p, 0))
    g1 = st["by_gate"]["G1"]
    assert g1.state == "in_progress"
    assert g1.note == f"PR #{pr['number']} was closed without merging (by @dev1); Request approval opens a new one"
    pr2 = s.request_approval(p, "G1")
    assert pr2["number"] != pr["number"] and _states(s, p)["G1"] == "waiting"


def test_checks_warn_missing_node_ca(monkeypatch):
    from pkf_studio import checks

    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/zscaler.pem")
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    c = checks.node_ca_check()
    assert c.name == "Node CA for the AI (NODE_EXTRA_CA_CERTS)" and not c.ok and not c.required
    assert c.fix == 'export NODE_EXTRA_CA_CERTS="$REQUESTS_CA_BUNDLE" in ~/pkf/env.sh'
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/zscaler.pem")
    assert checks.node_ca_check().ok
    monkeypatch.delenv("REQUESTS_CA_BUNDLE")
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS")
    assert checks.node_ca_check().ok                                 # no proxy bundle: nothing to warn about


def test_team_pr_writes_gitattributes_without_conversion(gh, tmp_path):
    fake, clone = gh
    (clone / "crlf.yaml").write_bytes(b"a: 1\r\nb: 2\r\n")         # a CRLF file already committed (Windows writer)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "crlf file")
    _git(clone, "push", "-q", "origin", "main")
    s = setup_project(fake, clone)
    ga = s.read_base(".gitattributes")
    assert ga is not None and "* -text" in ga.splitlines() and "eol=lf" not in ga and "text=auto" not in ga
    assert any(f["filename"] == ".gitattributes" for f in fake._files(1))
    assert teamsetup.gitattributes(ga) == ga                         # idempotent
    assert teamsetup.gitattributes("*.png binary\n") == ("*.png binary\n\n# pkf Studio: no line-ending conversion (approved "
                                                         "files are fingerprinted byte for byte)\n* -text\n")
    # a clone with autocrlf=true does not convert, and the CRLF file does not show as modified
    other = tmp_path / "win"
    subprocess.run(["git", "-c", "core.autocrlf=true", "clone", "-q", str(fake.bare), str(other)], check=True)
    _git(other, "config", "core.autocrlf", "true")
    assert (other / "crlf.yaml").read_bytes() == b"a: 1\r\nb: 2\r\n"
    assert (other / ".pkf-studio" / "team.yaml").read_bytes().count(b"\r\n") == 0
    assert _git(other, "status", "--porcelain").strip() == ""


# ---------------------------------------------------------------------------------------------- readiness, RUN, runs
def test_g1_blocks_on_open_analysis_questions_until_requirement_changes(tmp_path):
    p = "aq_pipe"
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "requirements" / p).mkdir(parents=True)
    req = "Move files daily.\n"
    (tmp_path / "pipelines" / f"{p}.yaml").write_text(yaml.safe_dump({"requirement": req}))
    (tmp_path / "pipelines" / f"{p}.lock.yaml").write_text(yaml.safe_dump({"split": {"components": []}}))
    analysis = tmp_path / "requirements" / p / "analysis.json"
    analysis.write_text(json.dumps({"open_questions": ["Which bucket?", "Keep the source?"],
                                    "requirement_sha256": service.requirement_sha(req)}))
    probs = readiness(tmp_path, p, "G1")
    assert probs == ["the analyst's open questions are not answered: Which bucket? | Keep the source? — answer them in the "
                     "requirement text (the questions are cleared when the requirement changes), then Split again"]
    assert service.requirement_sha("  " + req + "\n\n") == service.requirement_sha(req)       # stripped
    (tmp_path / "pipelines" / f"{p}.yaml").write_text(yaml.safe_dump({"requirement": req + "Bucket: b1; keep it.\n"}))
    assert readiness(tmp_path, p, "G1") == []                       # the requirement changed: answered
    analysis.write_text(json.dumps({"open_questions": ["Which bucket?"]}))                     # 0.4.0 analysis: no hash
    assert readiness(tmp_path, p, "G1") == []


def test_g5_readiness_requires_an_ok_dry_run(tmp_path):
    p = "dry_pipe"
    infra = tmp_path / "infra" / p
    infra.mkdir(parents=True)
    (infra / "manifest.json").write_text(json.dumps({"needs_apply": False}))
    msg = "the dry run failed or is missing (AWS credentials?): fix it, then Render & plan again"
    assert readiness(tmp_path, p, "G5") == [msg]
    rv = tmp_path / "review" / p
    rv.mkdir(parents=True)
    (rv / "dry-run.json").write_text(json.dumps({"ok": False, "issues": [{"msg": "NoCredentialsError"}]}))
    assert readiness(tmp_path, p, "G5") == [msg]
    (rv / "dry-run.json").write_text(json.dumps({"ok": True, "issues": []}))
    assert readiness(tmp_path, p, "G5") == []
    (infra / "manifest.json").unlink()
    assert readiness(tmp_path, p, "G5") == ["render and plan the dev environment first"]


def _run_record(s: Studio, fake: FakeGitHub, p: str, report: dict, merge: bool = True) -> dict:
    wt, branch = s.open_run_branch(p)
    run = branch.rsplit("/", 1)[-1]
    d = wt / "review" / p / "runs" / run
    d.mkdir(parents=True)
    (d / "report.json").write_text(json.dumps(report))
    s.commit(p, f"S6 {p}: run")
    s.push(p)
    pr = s.gh.create_pull(f"[pkf] {p}: run record", branch, s.base, "record")
    if merge:
        s.merge(p, "RUN", pr_number=pr["number"])
    s.invalidate()
    return pr


def test_run_state_failed_from_report_on_base(gh, monkeypatch):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "runf_pipe"
    for g in ("G1", "G2", "G3", "G4", "G5"):
        if g in ("G4", "G5"):
            monkeypatch.setattr(service, "readiness", lambda *a, **k: [])
        _stage(s, fake, p, g)
    assert _states(s, p)["RUN"] == "ready"
    _run_record(s, fake, p, {"ok": False, "issues": [{"msg": "file incoming/../x: invalid key", "code": "run_failed"}]})
    st = derive(s, p, s.prs(p, 0))
    run = st["by_gate"]["RUN"]
    assert run.state == "failed" and run.note == "file incoming/../x: invalid key" and st["current"] == "RUN"
    _run_record(s, fake, p, {"ok": True, "issues": []})
    assert _states(s, p)["RUN"] == "done"
    pr = _run_record(s, fake, p, {"ok": False, "issues": []}, merge=False)
    assert _states(s, p)["RUN"] == "waiting" and pr["number"]


def test_unpublished_runs_and_open_run_branch(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "pub_pipe"
    _stage(s, fake, p, "G1")
    wt, branch = s.open_run_branch(p)
    assert s.unpublished_runs(p) == []                                # nothing recorded yet: a preview
    run = branch.rsplit("/", 1)[-1]
    d = wt / "review" / p / "runs" / run
    d.mkdir(parents=True)
    (d / "started.json").write_text("{}")
    s.commit(p, "move started")
    assert s.unpublished_runs(p) == [branch]                         # a crashed move
    s.git.worktree(p, f"pkf/{p}/g1", s.base_ref)
    wt2, b2 = s.open_run_branch(p, branch=branch)                    # back on the preview's branch
    assert b2 == branch and s.worktree_branch(p) == branch and (wt2 / "review" / p / "runs" / run / "started.json").exists()
    s.push(p)
    assert s.unpublished_runs(p) == []                               # on GitHub now
    with pytest.raises(StudioError, match="is not a run branch"):
        s.open_run_branch(p, branch=f"pkf/{p}/g1")
    with pytest.raises(StudioError, match="does not exist"):
        s.open_run_branch(p, branch=f"pkf/{p}/run-20990101t000000")


# ---------------------------------------------------------------------------------------------- R-phase1 #2 #3
def test_airflowignore_from_an_earlier_build_is_restored_away(gh, tmp_path):
    """R1f: 0.4.0's real build at S4 also wrote dags/.airflowignore; main has none, so Restore removes it too. When main
    has one (shared by every pipeline) it is never touched."""
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "ai_pipe"
    wt = s.open_stage(p, "G4")
    (wt / "dags" / "pkf" / p).mkdir(parents=True)
    (wt / "dags" / ".airflowignore").write_text("pkf/**\n")
    (wt / "dags" / f"pkf_{p}.py").write_text("# 0.4.0 build\n")
    (wt / "dags" / "pkf" / p / "m.py").write_text("x = 1\n")
    s.commit(p, "S4 ai_pipe: 0.4.0 build")
    assert s.dags_diff(p) == ["dags/.airflowignore", f"dags/pkf/{p}/m.py", f"dags/pkf_{p}.py"]
    assert s.restore_dags_from_base(p) == ["dags/.airflowignore", f"dags/pkf/{p}/m.py", f"dags/pkf_{p}.py"]
    assert not (wt / "dags").exists() and s.dags_diff(p) == []
    assert _git(wt, "diff", "--name-only", f"{s.base_ref}...HEAD", "--", "dags/").strip() == ""
    # main has its own .airflowignore: a different one on the branch is left alone
    other = _other_clone(fake, tmp_path)
    (other / "dags").mkdir()
    (other / "dags" / ".airflowignore").write_text("pkf/**\n")
    _git(other, "checkout", "-q", "-b", "ign")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "ignore")
    _git(other, "push", "-q", "origin", "ign")
    fake.merge(s.gh.create_pull("ign", "ign", "main", "")["number"])
    s.fetch(force=True)
    (wt / "dags").mkdir(exist_ok=True)
    (wt / "dags" / ".airflowignore").write_text("pkf/**\nother/**\n")
    assert s.dags_diff(p) == []


def _job_on_g1(s: Studio, p: str) -> Path:
    wt = write_g1(s, p)
    s.commit(p, "S1")
    return wt


def test_out_of_scope_files_are_never_committed(gh):
    """R1e: a stray file in the worktree (notes-from-sandeep.txt) used to be committed by the next job, pushed into the
    PR, and block its gate. Now the commit leaves it out and says so, a staged one refuses the commit, readiness and
    Request approval name it, and a committed one can be taken off the branch."""
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "scope_pipe"
    wt = _job_on_g1(s, p)
    (wt / "notes-from-sandeep.txt").write_text("my notes\n")
    (wt / "requirements" / p / "more.md").write_text("in scope\n")
    sha = s.commit(p, "S1 edits")
    assert sha and _git(wt, "show", "--name-only", "--format=", sha).split() == [f"requirements/{p}/more.md"]
    assert s.left_uncommitted[p] == ["notes-from-sandeep.txt"]
    msg = s.left_uncommitted_message(p)
    assert msg.startswith("left uncommitted: notes-from-sandeep.txt is outside pipeline scope_pipe's own files")
    assert "move them out of the repository folder" in msg and "Discard would delete them" in msg
    assert s.commit(p, "nothing in scope") is None                     # only the stray file is left: nothing to commit
    assert (wt / "notes-from-sandeep.txt").exists()                    # never deleted
    # readiness and Request approval name it
    probs = readiness(wt, p, "G1")
    assert any(x.startswith("notes-from-sandeep.txt is outside pipeline scope_pipe's own files") for x in probs), probs
    with pytest.raises(StudioError, match="not ready for review: .*notes-from-sandeep.txt is outside pipeline"):
        s.request_approval(p, "G1")
    # staged already (e.g. by hand): the commit is refused with the same text, nothing committed
    _git(wt, "add", "notes-from-sandeep.txt")
    head = s.git.head(wt)
    with pytest.raises(StudioError, match="not committed — notes-from-sandeep.txt is outside pipeline"):
        s.commit(p, "with the stray file")
    assert s.git.head(wt) == head
    s.discard(p)                                                        # the Discard path
    assert not (wt / "notes-from-sandeep.txt").exists() and readiness(wt, p, "G1") == []
    # committed by 0.4.0 (or by hand): readiness names it; remove_out_of_scope takes it off the branch
    (wt / "notes-from-sandeep.txt").write_text("my notes\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "0.4.0 committed everything")
    assert s.out_of_scope(p) == {"committed": ["notes-from-sandeep.txt"], "uncommitted": []}
    assert any("notes-from-sandeep.txt" in x for x in readiness(wt, p, "G1"))
    assert s.remove_out_of_scope(p) == ["notes-from-sandeep.txt"]
    assert s.out_of_scope(p) == {"committed": [], "uncommitted": []} and readiness(wt, p, "G1") == []
    pr = s.request_approval(p, "G1")
    assert "notes-from-sandeep.txt" not in [f["filename"] for f in fake._files(pr["number"])]


def test_blocked_merged_gate_does_not_suggest_approve(gh):
    """A PR merged on GitHub although its gate is blocked (a file outside the pipeline): the Start message says Reopen,
    not /approve."""
    from pkf_studio.service import MergedUnapproved

    fake, clone = gh
    s = setup_project(fake, clone)
    p = "blk_pipe"
    wt = _job_on_g1(s, p)
    (wt / "stray.txt").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "stray committed by hand")
    s.push(p)
    pr = s.gh.create_pull("g1", f"pkf/{p}/g1", "main", "")
    s.post_approve(p, "G1")
    fake.merge(pr["number"])
    s.invalidate()
    s.fetch(force=True)
    s.open_stage(p, "G2")
    with pytest.raises(MergedUnapproved) as ei:
        s.ledger_row(p, "G1")
    msg = str(ei.value)
    assert ei.value.blocked and "/approve" not in msg and "Reopen G1 and request approval again" in msg
    assert "outside pipeline blk_pipe" in msg
    assert s.merged_unapproved(p)["blocked"] is True
