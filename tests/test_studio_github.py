"""pkf Studio against a fake GitHub (tests/fakegithub.py) and real git: setup PR, gate PRs, approvals by review and by
/approve comment, merging with the evaluated head, the ledger and its verification, staleness and reopening, strict
mode with the pkf-gate check."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fakegithub import FakeGitHub, make_repo
from pkf_studio import teamsetup
from pkf_studio import ledger as L
from pkf_studio.github import GitHub
from pkf_studio.project import Person, ProjectConfig, Team, write_local
from pkf_studio.service import Studio, StudioError
from pkf_studio.stages import derive
from pkf_studio.verify import rebuild_row, verify

ALL = ["product_owner", "business_analyst", "architect", "data_engineer"]


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


def setup_project(fake, clone, mode="dev", people=None) -> Studio:
    s = Studio.open(clone, github=GitHub(fake.api, "acme", "specs", "tok-dev1"))
    assert not s.configured
    people = people or [Person(name="Dev One", email="dev@example.com", roles=ALL, github="dev1", github_id=fake.users["dev1"]["id"])]
    cfg = ProjectConfig(mode=mode, github_api=fake.api, aws_account_id="123456789012", aws_region="eu-west-1")
    pr = teamsetup.open_pr(s, cfg, Team(people=people), ["@dev1"], with_gate=True)
    assert pr["number"] == 1
    fake.merge(1)
    s = Studio.open(clone)
    assert s.configured and s.cfg.mode == mode and s.read_base("pkf.toml")
    assert s.read_base(".github/workflows/pkf-gate.yml") and "/.pkf-studio/team.yaml @dev1" in s.read_base(".github/CODEOWNERS")
    write_local(s.root, {"email": "dev@example.com"})
    return s


def write_g1(s: Studio, p: str, requirement: str = "TRIGGER\n- daily\n") -> Path:
    wt = s.open_stage(p, "G1")
    (wt / "pipelines").mkdir(exist_ok=True)
    (wt / "pipelines" / f"{p}.yaml").write_text(yaml.safe_dump({"api_version": "pkf/v1", "kind": "pipeline", "name": p, "owner": "dev1",
                                                                "start_date": "2026-10-01", "requirement": requirement, "examples": []}))
    (wt / "pipelines" / f"{p}.lock.yaml").write_text(yaml.safe_dump({"split": {"summary": "one source, one destination", "components": []}}))
    (wt / "requirements" / p).mkdir(parents=True, exist_ok=True)
    (wt / "requirements" / p / "extracted.txt").write_text(requirement)
    return wt


def test_github_client_paginates_and_uses_etags(gh):
    fake, clone = gh
    c = GitHub(fake.api, "acme", "specs", "tok-dev1")
    assert c.me()["login"] == "dev1"
    fake.pulls[7] = {"number": 7, "state": "open", "head": {"ref": "x", "sha": "0" * 40, "repo": {}}, "base": {"ref": "main"},
                     "html_url": "u", "user": fake.users["dev1"], "_base_sha": "0" * 40}
    for i in range(130):
        fake.comment_as(7, "dev1", f"c{i}")
    assert len(c.comments(7)) == 130
    n = len(fake.calls)
    assert len(c.comments(7)) == 130 and len(fake.calls) == n + 2      # 2 pages again, answered 304
    with pytest.raises(Exception, match="401"):
        GitHub(fake.api, "acme", "specs", "wrong").me()


def test_dev_g1_cycle_ledger_verify_stale_reopen(gh):
    fake, clone = gh
    s = setup_project(fake, clone)
    p = "demo_pipe"
    write_g1(s, p)
    st = derive(s, p)
    assert st["current"] == "G1" and st["by_gate"]["G1"].state == "in_progress"
    with pytest.raises(StudioError, match="looks like a password"):
        (s.worktree_path(p) / "requirements" / p / "notes.txt").write_text("password: hunter22\n")
        s.request_approval(p, "G1")
    (s.worktree_path(p) / "requirements" / p / "notes.txt").unlink()
    pr = s.request_approval(p, "G1")
    assert "G1 Requirements" in pr["title"] and "/approve" in pr["body"]
    res, full = s.gate_status(p, "G1")
    assert not res.passed and "Product Owner" in res.missing
    with pytest.raises(StudioError, match="not passed"):
        s.merge(p, "G1")
    s.post_approve(p, "G1")
    res, full = s.gate_status(p, "G1")
    assert res.passed, res
    s.merge(p, "G1")
    st = derive(s, p)
    assert st["by_gate"]["G1"].state == "approved" and st["current"] == "G2"
    # S2 opens with the ledger row of G1
    s.open_stage(p, "G2")
    row = s.ledger_row(p, "G1")
    assert row["self_asserted"] and row["approvers"][0]["login"] == "dev1" and len(row["approvers"]) == 3
    s.write_ledger(p, row)
    assert rebuild_row(s.gh, s.root, s.team, "dev", p, row) == []
    bad = {**row, "approvers": [{**row["approvers"][0], "login": "mallory"}] + row["approvers"][1:]}
    assert any("approvers differ" in x for x in rebuild_row(s.gh, s.root, s.team, "dev", p, bad))
    bad = {**row, "input_hash": "0" * 64}
    assert any("input hash" in x for x in rebuild_row(s.gh, s.root, s.team, "dev", p, bad))
    s.commit(p, "ledger")
    pr2 = s.request_approval(p, "G2")
    ok, lines = verify(s.gh, pr2["number"], s.root, s.worktree_path(p), base_ref="origin/main", run_tests=False)
    assert "ledger G1" in "\n".join(lines) and not ok          # G2 itself is not approved yet
    # someone changes the requirement on main: G1 becomes stale, later stages blocked
    other = s.git.worktree("_other", "hotfix", "origin/main")
    f = other / "pipelines" / f"{p}.yaml"
    f.write_text(f.read_text().replace("daily", "hourly"))
    s.git.commit(other, "edit requirement", "Dev One", "dev@example.com")
    s.git.push(other, "hotfix")
    n = s.gh.create_pull("hotfix", "hotfix", "main", "")["number"]
    fake.merge(n)
    s.fetch(force=True)
    s.invalidate()
    st = derive(s, p)
    assert st["by_gate"]["G1"].state == "stale" and st["by_gate"]["G2"].state == "blocked" and st["current"] == "G1"
    s.reopen(p, "G1")
    assert fake.pulls[pr2["number"]]["state"] == "closed"
    assert s.worktree_branch(p) == f"pkf/{p}/g1"


def test_strict_mode_reviews_distinct_people_and_required_check(gh):
    fake, clone = gh
    for login in ("po1", "ba1", "ar1", "de1"):
        fake.add_user(login, f"{login}@example.com")
    ids = {u: fake.users[u]["id"] for u in fake.users}
    people = [Person(name="Dev One", email="dev@example.com", roles=["data_engineer"], github="dev1", github_id=ids["dev1"]),
              Person(name="Po", email="po1@example.com", roles=["product_owner"], github="po1", github_id=ids["po1"]),
              Person(name="Ba", email="ba1@example.com", roles=["business_analyst"], github="ba1", github_id=ids["ba1"]),
              Person(name="Ar", email="ar1@example.com", roles=["architect"], github="ar1", github_id=ids["ar1"]),
              Person(name="De", email="de1@example.com", roles=["data_engineer"], github="de1", github_id=ids["de1"])]
    s = setup_project(fake, clone, mode="sit", people=people)
    p = "strict_pipe"
    write_g1(s, p)
    pr = s.request_approval(p, "G1")
    with pytest.raises(StudioError):
        s.post_approve(p, "G1")                     # no comment approvals in sit
    fake.comment_as(pr["number"], "po1", f"/approve {pr['head']['sha'][:7]}")
    fake.review(pr["number"], "ba1")
    fake.review(pr["number"], "ar1")
    res, _ = s.gate_status(p, "G1")
    assert not res.passed and res.missing == ["Product Owner"]
    fake.review(pr["number"], "po1", body="Approve as: po")
    res, full = s.gate_status(p, "G1")
    assert res.passed
    with pytest.raises(StudioError, match="pkf-gate"):
        s.merge(p, "G1")
    fake.set_check(full["head"]["sha"], "success")
    s.merge(p, "G1")
    row = s.ledger_row(p, "G1")
    assert not row["self_asserted"] and {a["login"] for a in row["approvers"]} == {"po1", "ba1", "ar1"}
    # a stage PR may not touch the roster
    wt = s.open_stage(p, "G2")
    (wt / ".pkf-studio" / "team.yaml").write_text("people: []\n")
    with pytest.raises(StudioError, match="may not change"):
        s.commit(p, "sneaky")


def test_verify_rejects_workflow_edits_and_unapproved_mode_lowering(gh):
    fake, clone = gh
    fake.add_user("ar1", "ar1@example.com")
    ids = {u: fake.users[u]["id"] for u in fake.users}
    people = [Person(name="Dev One", email="dev@example.com", roles=ALL, github="dev1", github_id=ids["dev1"]),
              Person(name="Ar", email="ar1@example.com", roles=["architect"], github="ar1", github_id=ids["ar1"])]
    s = setup_project(fake, clone, mode="dev", people=people)
    # a stage branch that edits the gate workflow
    p = "wf_pipe"
    wt = write_g1(s, p)
    (wt / ".github" / "workflows" / "pkf-gate.yml").write_text("name: pkf-gate\njobs: {}\n")
    s.git.commit(wt, "edit the gate", "Dev One", "dev@example.com")
    s.push(p)
    n = s.gh.create_pull("x", f"pkf/{p}/g1", "main", "")["number"]
    fake.comment_as(n, "dev1", f"/approve {fake.pulls[n]['head']['sha'][:7]}")
    ok, lines = verify(s.gh, n, s.root, wt, base_ref="origin/main", run_tests=False)
    assert not ok and any("may not change .github/workflows/pkf-gate.yml" in x for x in lines)
    # a team PR lowering the mode needs an Architect who is not the author
    s.fetch(force=True)
    team_wt = s.git.worktree("_team2", "pkf-studio/team-lower", "origin/main")
    cfg = yaml.safe_load((team_wt / ".pkf-studio/project.yaml").read_text())
    # make the base strict first (merged team change), then try to lower it
    up = s.git.worktree("_team3", "pkf-studio/team-raise", "origin/main")
    (up / ".pkf-studio/project.yaml").write_text(yaml.safe_dump({**cfg, "mode": "sit"}))
    s.git.commit(up, "raise", "Dev One", "dev@example.com")
    s.git.push(up, "pkf-studio/team-raise")
    fake.merge(s.gh.create_pull("raise", "pkf-studio/team-raise", "main", "")["number"])
    s.fetch(force=True)
    s.git.run("checkout", "-q", "-B", "pkf-studio/team-lower", "origin/main", cwd=team_wt)
    (team_wt / ".pkf-studio/project.yaml").write_text(yaml.safe_dump({**cfg, "mode": "dev"}))
    s.git.commit(team_wt, "lower", "Dev One", "dev@example.com")
    s.git.push(team_wt, "pkf-studio/team-lower")
    n = s.gh.create_pull("lower", "pkf-studio/team-lower", "main", "")["number"]
    s.git.run("checkout", "-q", "--detach", "origin/main", cwd=up)
    ok, lines = verify(s.gh, n, up, team_wt, base_ref="HEAD", run_tests=False)
    assert not ok and any("an Architect who is not an author" in x for x in lines), lines
    fake.review(n, "ar1")
    ok, lines = verify(s.gh, n, up, team_wt, base_ref="HEAD", run_tests=False)
    assert ok, lines
