"""End to end, dev mode, one person: a requirement document → G1…G5 approved on (fake) GitHub → a local run → the record
PR → the artifact zip. Real git, real jobs (inline), the framework CLI, and the AI replayed from tests/llm-fixtures
(record them once with PKF_TEST_LLM_PROVIDER=record). The drop and lake folders live in this session's own temporary
root (conftest.e2e_root): they are environment values the AI never sees, so the replay keys do not depend on them."""
from __future__ import annotations

import io
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

from fakegithub import FakeGitHub, make_repo
from pkf_studio import bundle, jobs, teamsetup
from pkf_studio.service import unfilled
from pkf_studio.github import GitHub
from pkf_studio.project import Person, ProjectConfig, Team, write_local
from pkf_studio.service import Studio
from pkf_studio.stages import derive
from pkf_studio.ui.components import required_paths, write_values
from pkf_studio.ui.requirements import import_text

ROOT = Path(__file__).resolve().parents[1]
P = "lab_drop_daily"
REQUIREMENT = """Daily lab drop to the lake (local folders)

TRIGGER
- Run once a day (@daily). No events. If a run fails, Airflow retries it twice, 5 minutes apart.

SOURCE
- A local folder: the drop folder (its path is set per environment).
- Each day's deliveries are in the subfolder named after the run date (YYYY-MM-DD). Only that folder is read.
- Everything under the day's folder is one package for the day (one package per run).
- Take every file whose name ends in ".csv" (lowercase), at any depth under the day's folder; ignore all other files.
- Paths in the package are relative to the day's folder, e.g. SITE_0001/a.csv.
- The run fails when the day's folder has no .csv file at all.

TRANSFORMATION
- None: files are copied unchanged.

DESTINATION
- A local folder: the lake folder (its path is set per environment).
- The package goes to <run date>/ under the lake folder, keeping the paths: <lake>/<run date>/SITE_0001/a.csv.
- Overwrite files that already exist. Write an empty _SUCCESS marker in <lake>/<run date>/ after all files.
- Copy, never move: the drop folder is left as it is.
"""


def run(s: Studio, action: str, args: dict | None = None) -> dict:
    jid = jobs.start(s.root, P, action, args or {}, action)
    stt = jobs.status(s.root, jid)
    assert stt["state"] == "succeeded", jobs.log(s.root, jid)[-6000:]
    assert stt["result"]["problems"] == [] and stt["result"]["severity"] is None, (stt["result"], jobs.log(s.root, jid)[-6000:])
    return stt["result"]


def approve_and_merge(fake: FakeGitHub, s: Studio, gate: str) -> None:
    pr = s.request_approval(P, gate)
    res, _ = s.gate_status(P, gate)
    assert not res.passed
    s.post_approve(P, gate)
    res, _ = s.gate_status(P, gate)
    assert res.passed, res.summary()
    s.merge(P, gate)
    assert fake.pulls[pr["number"]]["merged"]


@pytest.fixture()
def env(tmp_path, monkeypatch, e2e_root):
    E2E = e2e_root
    for sub in ("drop", "lake"):
        shutil.rmtree(E2E / sub, ignore_errors=True)
    import datetime as dt

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()     # the dry run lists today's folder
    for day in ("2026-10-15", today):
        for rel in ("SITE_0001/a.csv", "SITE_0002/b.csv", "SITE_0002/readme.txt"):
            f = E2E / "drop" / day / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("id\n1\n")
    bare, clone = make_repo(tmp_path)
    fake = FakeGitHub(bare)
    fake.add_user("dev1", "dev@example.com", token="tok-dev1")
    for k, v in {"GITHUB_TOKEN": "tok-dev1", "PKF_STUDIO_INLINE_JOBS": "1", "PKF_LLM_PROVIDER": os.environ.get("PKF_TEST_LLM_PROVIDER", "replay"),
                 "PKF_LLM_FIXTURES": str(ROOT / "tests" / "llm-fixtures"), "PKF_PLUGIN_ROOT": str(ROOT / "steps-plugin"),
                 "PKF_STAGING_URL": f"file://{tmp_path / 'staging'}", "AWS_EC2_METADATA_DISABLED": "true",
                 "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(teamsetup, "build_wheel", lambda dest, log=print: None)
    yield fake, clone
    fake.close()


def test_requirement_to_local_run(env, e2e_root):
    fake, clone = env
    E2E = e2e_root
    s = Studio.open(clone, github=GitHub(fake.api, "acme", "specs", "tok-dev1"))
    me = Person(name="Dev One", email="dev@example.com", roles=["product_owner", "business_analyst", "architect", "data_engineer"],
                github="dev1", github_id=fake.users["dev1"]["id"])
    teamsetup.open_pr(s, ProjectConfig(mode="dev", github_api=fake.api, aws_region="eu-west-1"), Team(people=[me]), ["@dev1"])
    fake.merge(1)
    s = Studio.open(clone)
    write_local(s.root, {"email": "dev@example.com"})

    # S1 → G1
    import_text(s, P, "lab-drop.md", REQUIREMENT.encode(), REQUIREMENT, [])
    res = run(s, "analyze")
    wt = s.worktree_path(P)
    spec = yaml.safe_load((wt / "pipelines" / f"{P}.yaml").read_text())
    assert "TRIGGER" in spec["requirement"] and (wt / "requirements" / P / "pseudocode.md").exists()
    split = yaml.safe_load((wt / "pipelines" / f"{P}.lock.yaml").read_text())["split"]
    assert split and not split.get("ambiguities"), split.get("ambiguities")
    approve_and_merge(fake, s, "G1")

    # S2 → G2
    run(s, "start_g2")
    approve_and_merge(fake, s, "G2")

    # S3: component files, environment values, plans → G3 (or skipped)
    res = run(s, "start_g3")
    for line in res["unfilled"]:
        rel = line.split(":", 1)[0]
        doc = yaml.safe_load((wt / rel).read_text())
        values = {}
        for keys, _ in required_paths(doc):
            k = ".".join(map(str, keys))
            values[keys] = str(E2E / ("drop" if rel.startswith("sources/") else "lake")) if "path" in k else "x"
        write_values(wt / rel, values)
    assert not unfilled(wt, P)
    res = run(s, "plan_components")
    if res["no_plans"]:
        run(s, "start_g4")
    else:
        approve_and_merge(fake, s, "G3")
        run(s, "start_g4")
    tests = json.loads((wt / "review" / P / "tests.json").read_text())
    assert tests["build_ok"] and tests["version"] == 2 and tests["build_mode"] == "draft" and not tests["dags_differ"], tests
    assert (wt / "review" / P / "capabilities.md").exists()
    assert not [f for f in (wt / "dags").rglob("*") if f.is_file()] and not s.list_at(f"origin/pkf/{P}/g4", "dags/"), \
        "the draft build writes nothing at S4 (and the S4 job pushed g4)"
    assert s.git.sha(f"origin/pkf/{P}/g4") == s.git.head(wt), "every stage job pushes its branch (K2)"
    approve_and_merge(fake, s, "G4")

    # S5 → G5
    res = run(s, "start_g5")
    assert not res["needs_apply"]
    man = json.loads((wt / "infra" / P / "manifest.json").read_text())
    assert man["note"].startswith("nothing to apply")
    assert json.loads((wt / "review" / P / "dry-run.json").read_text())["ok"]
    approve_and_merge(fake, s, "G5")

    # S6: run locally, record PR merged (dev)
    res = run(s, "run_local", {"own_credentials": True, "params": {}, "date": "2026-10-15"})
    assert res["merged"], res
    st = derive(s, P, s.prs(P, 0))
    assert [g.state for g in st["gates"]][:5] == ["approved", "approved", st["by_gate"]["G3"].state, "approved", "approved"]
    assert st["by_gate"]["G3"].state in ("approved", "skipped") and st["by_gate"]["RUN"].state == "done"
    rows = [json.loads(x) for x in s.read_base(f".pkf-studio/approvals/{P}.jsonl").splitlines()]
    assert [r["gate"] for r in rows] == [g for g in ("G1", "G2", "G3", "G4", "G5") if g != "G3" or st["by_gate"]["G3"].state == "approved"]
    assert all(r["self_asserted"] for r in rows)
    report = json.loads(s.read_base(res["report"]))
    assert report["ok"], report
    got = sorted(x.relative_to(E2E / "lake").as_posix() for x in (E2E / "lake").rglob("*") if x.is_file())
    assert got == ["2026-10-15/SITE_0001/a.csv", "2026-10-15/SITE_0002/b.csv", "2026-10-15/_SUCCESS"], got

    name, data = bundle.build(s.root, s.base_ref, P, job_logs=[("x", "log")], self_asserted=True)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    for prefix in ("01_requirements/original.md", "01_requirements/pipeline.yaml", "02_design/split.lock.yaml", "04_code/dags/",
                   "05_infra/iam-policy.json", "06_deploy/deploy.md", "07_runs/dry-run.json", "workflow/approvals.jsonl", "workflow/README.md"):
        assert any(n.startswith(prefix) for n in names), (prefix, names)
