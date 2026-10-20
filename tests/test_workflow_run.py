"""Workflow Developer run (flow.py): a dev-mode pipeline driven from intake to "live in Airflow" by tick()/answer()/
mark_done() only — the same fake GitHub, inline jobs and recorded AI answers as test_studio_e2e."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from fakegithub import FakeGitHub  # noqa: F401 - the fixture's type
from pkf_studio import flow, flowstore, jobs
from pkf_studio.github import GitHub
from pkf_studio.project import Person, ProjectConfig, Team, write_local
from pkf_studio import teamsetup
from pkf_studio.service import Studio
from pkf_studio.stages import derive
from pkf_studio.ui.components import required_paths
from test_studio_e2e import P, REQUIREMENT, env  # noqa: F401 - the fixture

ROOT = Path(__file__).resolve().parents[1]


def _studio(fake, clone) -> Studio:
    s = Studio.open(clone, github=GitHub(fake.api, "acme", "specs", "tok-dev1"))
    me = Person(name="Dev One", email="dev@example.com", roles=["product_owner", "business_analyst", "architect", "data_engineer"],
                github="dev1", github_id=fake.users["dev1"]["id"])
    teamsetup.open_pr(s, ProjectConfig(mode="dev", github_api=fake.api, aws_region="eu-west-1"), Team(people=[me]), ["@dev1"])
    fake.merge(1)
    s = Studio.open(clone)
    write_local(s.root, {"email": "dev@example.com"})
    return s


def states_of(s):
    s.invalidate()
    return derive(s, P, s.prs(P, 0))


def tick(s, n: int = 1) -> dict:
    st = None
    for _ in range(n):
        st = flow.tick(s, P, states_of(s))
    return st


def test_steps_table():
    ids = [x.id for x in flow.STEPS]
    assert len(ids) == 34 and ids[0] == "1.1" and ids[-1] == "7.4" and len(set(ids)) == 34
    assert [x.id for x in flow.STEPS if x.kind == "gate"] == ["1.6", "1.7", "1.8", "2.3", "3.6", "4.4", "5.4"]
    assert flow.STAGE_IDS == ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]


def test_snapshot_without_a_run_shows_derived_progress(env):
    fake, clone = env
    s = _studio(fake, clone)
    snap = flow.snapshot(s, P, states_of(s), None)
    assert snap["run"]["status"] == "idle" and [x["id"] for x in snap["stages"]] == flow.STAGE_IDS
    assert all(st["status"] == "not_started" for st in snap["stages"])
    assert snap["stages"][0]["progress"] == [0, 8]


def test_run_from_intake_to_live(env, e2e_root):
    fake, clone = env
    s = _studio(fake, clone)
    E2E = e2e_root

    st = flow.start(s, P, states_of(s), {"document_name": "lab-drop.md", "document_bytes": REQUIREMENT.encode(),
                                          "document_text": REQUIREMENT, "flags": [], "auto_approve": True})
    # 1.1, 1.2 done; 1.3 ran inline and is picked up by the next tick
    assert st["steps"]["1.1"]["status"] == "done" and st["steps"]["1.2"]["status"] == "done"
    assert st["steps"]["1.3"]["status"] == "running" and st["cursor"] == "1.3"
    st = tick(s)
    assert st["steps"]["1.3"]["status"] == "done", st["steps"]["1.3"]
    assert st["cursor"] == "1.4" and st["status"] == "waiting_user"
    wt = s.worktree_path(P)
    spec = yaml.safe_load((wt / "pipelines" / f"{P}.yaml").read_text())
    # 1.4: keep the requirement as it is → 1.5 skipped → G1 requested, auto-approved, merged (1.6 1.7 1.8)
    st = flow.answer(s, P, states_of(s), "1.4", {"requirement": spec["requirement"]})
    assert st["steps"]["1.5"]["status"] == "skipped"
    assert st["steps"]["1.6"]["status"] == "done", st["steps"]["1.6"]
    st = tick(s, 3)
    assert st["steps"]["1.7"]["status"] == "done" and st["steps"]["1.8"]["status"] == "done", (st["steps"]["1.7"], st["steps"]["1.8"])
    assert states_of(s)["by_gate"]["G1"].state == "approved"
    # S2: 2.1 ran, 2.2 waits for the review
    st = tick(s)
    assert st["steps"]["2.1"]["status"] == "done" and st["cursor"] == "2.2", st["steps"]["2.1"]
    st = flow.answer(s, P, states_of(s), "2.2", {})
    st = tick(s, 3)
    assert st["steps"]["2.3"]["status"] == "done", st["steps"]["2.3"]
    # S3: 3.1 ran, 3.2 asks for the environment values
    st = tick(s)
    assert st["steps"]["3.1"]["status"] == "done" and st["cursor"] == "3.2" and st["status"] == "waiting_input", st["steps"]["3.1"]
    files = {}
    from pkf_studio.service import unfilled

    for line in unfilled(wt, P):
        rel = line.split(":", 1)[0]
        doc = yaml.safe_load((wt / rel).read_text())
        vals = {}
        for keys, _ in required_paths(doc):
            k = ".".join(map(str, keys))
            vals[json.dumps(list(keys))] = str(E2E / ("drop" if rel.startswith("sources/") else "lake")) if "path" in k else "x"
        files[rel] = vals
    st = flow.answer(s, P, states_of(s), "3.2", {"files": files})
    assert st["steps"]["3.2"]["status"] == "done"
    st = tick(s, 2)
    assert st["steps"]["3.3"]["status"] in ("done", "skipped"), st["steps"]["3.3"]
    if st["cursor"] == "3.4":
        st = flow.answer(s, P, states_of(s), "3.4", {})
        st = tick(s, 3)
    assert st["steps"]["3.5"]["status"] in ("done", "skipped") and st["steps"]["3.6"]["status"] in ("done", "skipped"), \
        (st["steps"]["3.5"], st["steps"]["3.6"])
    # S4
    st = tick(s, 2)
    assert st["steps"]["4.1"]["status"] == "done" and st["cursor"] == "4.2", st["steps"]["4.1"]
    st = flow.answer(s, P, states_of(s), "4.2", {})
    assert st["steps"]["4.3"]["status"] == "skipped", st["steps"]["4.3"]
    st = tick(s, 3)
    assert st["steps"]["4.4"]["status"] == "done", st["steps"]["4.4"]
    # S5
    st = tick(s, 2)
    assert st["steps"]["5.1"]["status"] == "done" and st["steps"]["5.2"]["status"] == "skipped" and st["cursor"] == "5.3", st["steps"]["5.1"]
    st = flow.answer(s, P, states_of(s), "5.3", {})
    st = tick(s, 3)
    assert st["steps"]["5.4"]["status"] == "done", st["steps"]["5.4"]
    # S6: own credentials (no run role, one person), checklist, run, record PR, second run skipped
    st = tick(s)
    assert st["cursor"] == "6.1" and st["status"] == "waiting_input"
    st = flow.answer(s, P, states_of(s), "6.1", {"own_credentials": True})
    assert st["cursor"] == "6.2" and st["status"] == "waiting_user"
    st = flow.mark_done(s, P, states_of(s), "6.2")
    assert st["steps"]["6.2"]["status"] == "done" and st["steps"]["6.3"]["status"] == "running"
    st = tick(s, 2)
    assert st["steps"]["6.3"]["status"] == "done", st["steps"]["6.3"]
    assert st["steps"]["6.4"]["status"] == "done", st["steps"]["6.4"]
    assert st["cursor"] == "6.5" and st["status"] == "waiting_user"
    st = flow.skip(s, P, states_of(s), "second run not needed")
    assert st["steps"]["6.5"]["status"] == "skipped"
    # 7 Deploy
    st = tick(s, 2)
    assert st["steps"]["7.1"]["status"] == "done", st["steps"]["7.1"]
    assert st["cursor"] == "7.2"
    st = flow.answer(s, P, states_of(s), "7.2", {})
    st = flow.mark_done(s, P, states_of(s), "7.3")
    assert st["cursor"] == "7.4"
    st = flow.mark_done(s, P, states_of(s), "7.4")
    assert st["status"] == "done", st
    done = {k for k, v in st["steps"].items() if v["status"] in ("done", "skipped")}
    assert len(done) == 34
    # the journal
    types = [e["type"] for e in flowstore.events(s.root, P)]
    assert types[0] == "run_started" and types[-1] == "run_done"
    assert "gate_requested" in types and "gate_merged" in types and "input_collected" in types and "checklist_done" in types
    snap = flow.snapshot(s, P, states_of(s), st)
    assert snap["run"]["status"] == "done" and all(x["status"] in ("done", "skipped") for x in snap["stages"])
    lines = flow.console_lines(s, P, st, "6.3")
    assert any(x["source"] == "job" for x in lines) and any(x["source"] == "flow" for x in lines)


def test_failed_job_pauses_and_retry_continues(env, monkeypatch):
    fake, clone = env
    s = _studio(fake, clone)
    flow.start(s, P, states_of(s), {"document_name": "lab-drop.md", "document_text": "", "flags": []})
    st = tick(s)   # analyze refuses an empty requirement? no: the import happened; the AI step fails on an empty text
    # whatever the cause, an empty document cannot be analysed: the step is red and the run waits
    assert st["status"] == "failed" and st["steps"][st["cursor"]]["status"] == "failed", st
    assert any(e["type"] == "step_failed" for e in flowstore.events(s.root, P))
    snap = flow.snapshot(s, P, states_of(s), st)
    assert snap["stages"][0]["status"] == "failed"
    st = flow.stop(s, P)
    assert st["status"] == "stopped"
    st = flow.resume(s, P, states_of(s))
    assert st["status"] == "failed"      # a failed step stays failed until Retry
    flow.reset(s, P)
    assert flow.load(s.root, P)["status"] == "idle"
