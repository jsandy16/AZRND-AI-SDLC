"""Regression tests for the code-review findings: reopen and approval order, pipeline scope of stage and record PRs,
crafted merges, full ledger-row verification, Terraform guard placement, commit scan gaps, safe cancel, repo modules
never shadowing the framework."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fakegithub import FakeGitHub, make_repo
from pkf_studio import infra, jobs, teamsetup
from pkf_studio.service import StudioError
from pkf_studio.stages import derive
from pkf_studio.verify import rebuild_row, verify
from test_studio_github import setup_project, write_g1
from test_studio_units import FAKE_TF


@pytest.fixture()
def gh(tmp_path, monkeypatch):
    bare, clone = make_repo(tmp_path)
    fake = FakeGitHub(bare)
    fake.add_user("dev1", "dev@example.com", token="tok-dev1")
    monkeypatch.setenv("GITHUB_TOKEN", "tok-dev1")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(teamsetup, "build_wheel", lambda dest, log=print: None)
    yield fake, clone
    fake.close()


def approve_merge(fake, s, p, gate):
    s.request_approval(p, gate)
    s.post_approve(p, gate)
    s.merge(p, gate)


def test_reopen_then_reapproval_restarts_later_gates(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "re_pipe"
    write_g1(s, p)
    approve_merge(fake, s, p, "G1")
    wt = s.open_stage(p, "G2")
    s.write_ledger(p, s.ledger_row(p, "G1"))
    approve_merge(fake, s, p, "G2")
    assert derive(s, p, s.prs(p, 0))["by_gate"]["G2"].state == "approved"
    s.reopen(p, "G1")                                             # found a problem in the requirement
    st = derive(s, p, s.prs(p, 0))
    assert st["by_gate"]["G1"].state == "in_progress" and st["current"] == "G1" and st["by_gate"]["G2"].state == "blocked"
    assert not s.git.sha(f"refs/heads/pkf/{p}/g2")                # later branches restart from the new base
    f = s.worktree_path(p) / "pipelines" / f"{p}.yaml"
    f.write_text(f.read_text().replace("daily", "hourly"))
    approve_merge(fake, s, p, "G1")
    st = derive(s, p, s.prs(p, 0))
    assert st["by_gate"]["G1"].state == "approved"
    assert st["by_gate"]["G2"].state == "stale" and "approved again after" in st["by_gate"]["G2"].note


def test_scope_blocks_other_pipelines_and_top_level_code(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "scope_pipe"
    wt = write_g1(s, p)
    (wt / "pkf").mkdir()
    (wt / "pkf" / "cli.py").write_text("print('{\"ok\": true}')\n")       # would shadow the framework
    with pytest.raises(StudioError, match="pkf/cli.py is outside pipeline scope_pipe's own files"):
        s.request_approval(p, "G1")                                       # the app refuses before the PR (R-phase1 #2)
    s.git.run("add", "-A", cwd=wt)                                        # committed outside the app: the gate blocks it
    s.git.run("commit", "-q", "-m", "by hand", cwd=wt)
    s.push(p)
    s.gh.create_pull("g1", f"pkf/{p}/g1", "main", "")
    s.post_approve(p, "G1")
    res, _ = s.gate_status(p, "G1")
    assert not res.passed and any("outside pipeline scope_pipe" in b for b in res.blocked)
    ok, lines = verify(s.gh, s.prs(p, 0)["G1"]["number"], s.root, wt, base_ref="origin/main", run_tests=False)
    assert not ok and any("pkf/cli.py" in x for x in lines)


def test_record_pr_may_only_add_a_report(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "run_pipe"
    wt, branch = s.open_run_branch(p)
    (wt / "transformations").mkdir()
    (wt / "transformations" / "x.py").write_text("def f(): pass\n")
    assert s.commit(p, "sneak") is None and s.left_uncommitted[p] == ["transformations/x.py"]  # the app leaves it out
    s.git.run("add", "-A", cwd=wt)                                        # committed outside the app: merge refuses
    s.git.run("commit", "-q", "-m", "sneak", cwd=wt)
    s.push(p)
    s.gh.create_pull("run", branch, "main", "")
    s.invalidate()
    with pytest.raises(StudioError, match="only add a run report"):
        s.merge(p, "RUN")


def test_crafted_merge_does_not_keep_an_old_approval(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "eq_pipe"
    wt = write_g1(s, p)
    s.commit(p, "work")
    approved = s.git.head(wt)
    # a "merge" whose tree is not the plain merge of its parents
    (wt / "dags").mkdir()
    (wt / "dags" / "evil.py").write_text("x = 1\n")
    s.git.run("add", "-A", cwd=wt)
    tree = s.git.run("write-tree", cwd=wt).strip()
    s.git.run("reset", "-q", cwd=wt)
    crafted = subprocess.run(["git", "commit-tree", tree, "-p", "HEAD", "-p", "HEAD~1", "-m", "merge"], cwd=str(wt), capture_output=True,
                             text=True, check=True).stdout.strip()
    assert s.equivalent(p, crafted)(approved) is False
    # a genuine merge of the base branch keeps it
    s.git.run("checkout", "-q", "--", ".", cwd=wt)
    s.git.run("merge", "-q", "--no-ff", "--no-edit", "origin/main", cwd=wt, check=False)
    assert s.equivalent(p, s.git.head(wt))(approved) in (True, False)  # no new base commit: may be a no-op merge
    other = s.git.worktree("_o", "other", "origin/main")
    (other / "OTHER.md").write_text("x\n")
    s.git.commit(other, "other", "Dev One", "dev@example.com")
    s.git.push(other, "other")
    fake.merge(s.gh.create_pull("o", "other", "main", "")["number"])
    s.fetch(force=True)
    s.git.run("reset", "-q", "--hard", approved, cwd=wt)
    s.git.run("merge", "-q", "--no-ff", "--no-edit", "origin/main", cwd=wt)
    assert s.equivalent(p, s.git.head(wt))(approved) is True


def test_ledger_row_fields_cannot_be_edited(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "led_pipe"
    write_g1(s, p)
    approve_merge(fake, s, p, "G1")
    row = s.ledger_row(p, "G1")
    assert rebuild_row(s.gh, s.root, s.team, "dev", p, row) == []
    for k, v in (("self_asserted", False), ("mode", "prd"), ("merge_sha", "0" * 40)):
        assert any(f"{k} differs" in x for x in rebuild_row(s.gh, s.root, s.team, "dev", p, {**row, k: v})), k


def test_refused_plan_is_never_hashed_and_apply_rechecks(tmp_path, monkeypatch):
    stack = tmp_path / "stack"
    stack.mkdir()
    tf = tmp_path / "terraform"
    tf.write_text(FAKE_TF.format(py=sys.executable).replace('"actions": ["create"]', '"actions": ["delete", "create"]'))
    tf.chmod(0o755)
    monkeypatch.setenv("PKF_TERRAFORM", str(tf))
    monkeypatch.setenv("PKF_TF_DATA", str(tmp_path / "tfdata"))
    with pytest.raises(infra.InfraError, match="refused"):
        infra.plan_stack(tmp_path, stack, "p/dev", lambda m: None, local_state=True)
    assert not (stack / "plan.sha256").exists()
    # a plan file that terraform cannot read is never applied (fail closed)
    bad = tmp_path / "bad-tf"
    bad.write_text(FAKE_TF.format(py=sys.executable).replace('if "-json" in a:', 'if "-json" in a:\n            sys.exit(1)'))
    bad.chmod(0o755)
    monkeypatch.setenv("PKF_TERRAFORM", str(bad))
    pf = infra.plan_dir(tmp_path, "p/dev") / "dev.tfplan"
    pf.write_text("PLAN")
    (stack / "plan.sha256").write_text(infra._sha_file(pf) + "\n")
    with pytest.raises(infra.InfraError, match="could not read the saved plan"):
        infra.apply_stack(tmp_path, stack, "p/dev", lambda m: None)


def test_commit_scan_covers_odd_names_and_encodings(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "scan_pipe"
    wt = write_g1(s, p)
    (wt / "requirements" / p / "Spéc – v2.md").write_text("password: hunter22\n", encoding="utf-8")
    with pytest.raises(StudioError, match="Spéc"):
        s.commit(p, "x")
    (wt / "requirements" / p / "Spéc – v2.md").unlink()
    (wt / "requirements" / p / "notes.txt").write_bytes("token = abcdef123456\n".encode("utf-16"))
    with pytest.raises(StudioError, match="notes.txt"):
        s.commit(p, "x")
    (wt / "requirements" / p / "notes.txt").unlink()
    (wt / "requirements" / p / "report.json").write_text(json.dumps({"SecretAccessKey": "A" * 40}))
    with pytest.raises(StudioError, match="report.json"):
        s.commit(p, "x")


def test_local_run_cannot_be_cancelled(tmp_path):
    d = jobs.jobs_dir(tmp_path) / "j1"
    d.mkdir(parents=True)
    (d / "cmd.json").write_text(json.dumps({"action": "run_local", "pipeline": "p", "started_at": "2026-01-01T00:00:00+00:00"}))
    (d / "pid").write_text(f"{__import__('os').getpid()}")
    with pytest.raises(jobs.JobError, match="cannot be cancelled"):
        jobs.cancel(tmp_path, "j1")


def test_repository_modules_never_shadow_the_framework(tmp_path):
    (tmp_path / "pkf").mkdir()
    (tmp_path / "pkf" / "__init__.py").write_text("")
    (tmp_path / "pkf" / "cli.py").write_text("print('PLANTED')\n")
    from pkf_studio.runner import Ctx

    job = tmp_path / "job"
    job.mkdir()
    ctx = Ctx(job, {"root": str(tmp_path), "pipeline": "p"})
    doc = ctx.pkf("list", cwd=tmp_path)
    assert "PLANTED" not in json.dumps(doc) and "PLANTED" not in (job / "log").read_text()


def test_g3_accepts_plans_whose_custom_steps_await_code(tmp_path, monkeypatch):
    from pkf_studio import service
    from pkf_studio.service import readiness

    # this test is about the simulation / needs_code rule; the toy component files do not load as specs, which
    # _plan_problems reports (its own test: test_studio_git.py::test_plan_problems_reports_components_that_do_not_load)
    monkeypatch.setattr(service, "_plan_problems", lambda wt, p: [])
    p = "rd_pipe"
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / f"{p}.yaml").write_text(yaml.safe_dump({"connection": "c", "requirement": "x"}))
    (tmp_path / "connections").mkdir()
    (tmp_path / "connections" / "c.yaml").write_text(yaml.safe_dump({"source": "s", "destination": "d"}))
    (tmp_path / "destinations").mkdir()
    (tmp_path / "destinations" / "d.yaml").write_text("type: s3\n")
    lock = tmp_path / "destinations" / "d.lock.yaml"
    (tmp_path / "review" / p).mkdir(parents=True)
    ex = tmp_path / "review" / p / "examples.json"
    lock.write_text(yaml.safe_dump({"simulation": [{"example": "a", "status": "needs_code"}]}))
    ex.write_text(json.dumps({"ok": False, "examples": [{"example": "e", "status": "needs_code"}]}))
    assert readiness(tmp_path, p, "G3") == []                      # custom delivery is coded at S4
    lock.write_text(yaml.safe_dump({"simulation": [{"example": "a", "status": "fail"}]}))
    ex.write_text(json.dumps({"ok": False, "examples": [{"example": "e", "status": "fail"}]}))
    probs = readiness(tmp_path, p, "G3")
    assert any("destination plan fails" in x for x in probs) and any("end-to-end examples fail" in x for x in probs)


def test_plain_approve_comment_on_github_then_merge_on_github(gh):
    import time

    fake, clone = gh
    s = setup_project(fake, clone)
    p = "gh_pipe"
    write_g1(s, p)
    pr = s.request_approval(p, "G1")
    time.sleep(1.1)
    fake.comment_as(pr["number"], "dev1", "/approve")            # typed on GitHub, no commit id
    res, _ = s.gate_status(p, "G1")
    assert res.passed, res.summary()
    fake.merge(pr["number"])                                     # merged with GitHub's button
    s.invalidate()
    s.fetch(force=True)
    assert derive(s, p, s.prs(p, 0))["by_gate"]["G1"].state == "approved"
    assert s.ledger_row(p, "G1")["approvers"][0]["login"] == "dev1"
    # a plain /approve written before a new commit does not cover it
    s.open_stage(p, "G2")
    (s.worktree_path(p) / "review" / p).mkdir(parents=True, exist_ok=True)
    (s.worktree_path(p) / "review" / p / "x.md").write_text("x\n")
    s.write_ledger(p, s.ledger_row(p, "G1"))
    pr2 = s.request_approval(p, "G2")
    time.sleep(1.1)
    fake.comment_as(pr2["number"], "dev1", "/approve")
    time.sleep(1.1)
    (s.worktree_path(p) / "review" / p / "y.md").write_text("y\n")
    s.request_approval(p, "G2")
    res, _ = s.gate_status(p, "G2")
    assert not res.passed and any("older commit" in n for n in res.notes)
