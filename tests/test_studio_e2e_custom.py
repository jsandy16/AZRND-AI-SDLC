"""End to end with AI-written custom code (the path the plain e2e never reaches): a destination whose layout needs two
custom steps → G3 plans approved → S4 code written and tested in the hardened runner, checked by the draft build →
G4 → S5 → G5 → a local run whose output is checked file by file. Also what a user meets at S4:
  * the code test crashed (e.g. the old Windows runner bug) → "Run tests & build again" re-tests and the build passes;
  * "Regenerate (AI)" rewrites the code (the whole component, or one step) and the build passes;
  * an AI step that fails is a problem of a succeeded job (with the next action), not a silent success;
and every approval style (Studio buttons, a /approve comment on GitHub, a merge on GitHub with each merge method, a merge
on GitHub without approval) at every gate.
The AI is the scripted provider (K12) answering from tests/fakellm/visit_restructure.json: hand-written answers with no
prompt hash, so prompt changes never invalidate them."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys

import pytest
import yaml

from conftest import use_script
from fakegithub import FakeGitHub, make_repo  # noqa: F401  (fixture helpers)
from pkf_studio import jobs, teamsetup
from pkf_studio.github import GitHub
from pkf_studio.project import Person, ProjectConfig, Team, write_local
from pkf_studio.service import Studio, unfilled
from pkf_studio.stages import derive
from pkf_studio.ui.components import required_paths, write_values
from pkf_studio.ui.requirements import import_text
from test_studio_e2e import env  # noqa: F401  (the shared fixture)

P = "visit_restructure"
DEST = "lake_folder"
STEPS = ["route_visits", "route_unmatched"]
REQUIREMENT = """Daily visit files restructured into the lake (local folders)

TRIGGER
- Run once a day (@daily). No events. No retries.

SOURCE
- A local folder: the drop folder (its path is set per environment).
- Each day's deliveries are in the subfolder named after the run date (YYYY-MM-DD). Only that folder is read.
- Everything under the day's folder is one package for the day (one package per run).
- Take every file whose name ends in ".csv" (lowercase), at any depth under the day's folder; ignore all other files.
- Paths in the package are relative to the day's folder, e.g. SITE_0001/AB12_V007_labs.csv.
- The run fails when the day's folder has no .csv file at all.

TRANSFORMATION
- None: files are copied unchanged.

DESTINATION
- A local folder: the lake folder (its path is set per environment).
- File names look like <STUDY>_V<visit>_<rest>.csv, e.g. AB12_V007_labs.csv (study code: letters and digits; visit: digits).
- Such a file goes to <lake>/<run date>/<study code in lowercase>/visit_<visit number without leading zeros>/<file name>,
  e.g. AB12_V007_labs.csv of 2026-10-15 → <lake>/2026-10-15/ab12/visit_7/AB12_V007_labs.csv. The site folder is dropped.
- A visit of all zeros (V000) is visit_0.
- A .csv whose name does not match that pattern goes to <lake>/<run date>/unmatched/<file name>.
- Only .csv files reach the destination (the source already ignores every other file), so no other file needs a rule.
- Overwrite files that already exist. Write an empty _SUCCESS marker in <lake>/<run date>/ after all files.
- Copy, never move: the drop folder is left as it is.
"""
FILES = ("SITE_0001/AB12_V007_labs.csv", "SITE_0001/AB12_V000_vitals.csv", "SITE_0002/XY9_V120_ecg.csv",
         "SITE_0002/notes.csv", "SITE_0002/readme.txt")
EXPECTED = ["2026-10-15/_SUCCESS", "2026-10-15/ab12/visit_0/AB12_V000_vitals.csv", "2026-10-15/ab12/visit_7/AB12_V007_labs.csv",
            "2026-10-15/unmatched/notes.csv", "2026-10-15/xy9/visit_120/XY9_V120_ecg.csv"]
GATES = ["G1", "G2", "G3", "G4", "G5"]


@pytest.fixture()
def drops(env, e2e_root):  # noqa: F811
    import shutil

    shutil.rmtree(e2e_root / "drop", ignore_errors=True)   # not the plain e2e's files
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    for day in ("2026-10-15", today):
        for rel in FILES:
            f = e2e_root / "drop" / day / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("id\n1\n")
    fake, clone = env
    return fake, clone, e2e_root


# ---------------------------------------------------------------------------------------------- approvals (§5.2)
def approve_and_merge(fake, s, gate, style="app"):
    """Approve and merge one gate's PR the way `style` says. Returns True when the gate was merged without approval,
    so the next Start must fail until /approve is posted on the merged PR."""
    pr = s.request_approval(P, gate)
    res, _ = s.gate_status(P, gate)
    assert not res.passed
    n = pr["number"]
    if style == "app":                               # Post /approve as me + Merge in the app
        s.post_approve(P, gate)
        res, _ = s.gate_status(P, gate)
        assert res.passed, res.summary()
        s.merge(P, gate)
    elif style == "gh-comment":                      # a plain /approve comment on GitHub + Merge in the app
        fake.comment_as(n, "dev1", "/approve")
        s.invalidate()
        res, _ = s.gate_status(P, gate)
        assert res.passed, res.summary()
        s.merge(P, gate)
    elif style.startswith("gh-merge:"):              # comment and merge on GitHub (merge commit, squash or rebase), branch deleted
        fake.comment_as(n, "dev1", "/approve")
        method = style.split(":", 1)[1]
        if method == "merge":
            fake.merge(n)
            fake.git("update-ref", "-d", f"refs/heads/{pr['head']['ref']}")
        else:
            fake.merge(n, f"[pkf] {P}: {gate} ({method})", method=method, delete_branch=True)  # tests/fakegithub.py C22 (WP3)
    elif style == "gh-merge-unapproved":             # merged on GitHub without any approval
        fake.merge(n)
    else:
        raise AssertionError(style)
    s.invalidate()
    s.fetch(force=True)
    assert fake.pulls[n]["merged"]
    return style == "gh-merge-unapproved"


def _job(s, action, args=None, ok=True):
    jid = jobs.start(s.root, P, action, args or {}, action)
    stt = jobs.status(s.root, jid)
    if ok:
        assert stt["state"] == "succeeded", jobs.log(s.root, jid)[-6000:]
    return stt, jobs.log(s.root, jid)


def _start(s, action, unapproved_gate=None, args=None):
    """Start the next stage. After a merge without approval the Start fails with the documented message; Post
    /approve on the merged PR, and the Start succeeds."""
    if unapproved_gate:
        stt, log = _job(s, action, args, ok=False)
        assert stt["state"] == "failed", log[-3000:]
        err = stt["result"]["error"]
        assert "was merged without the required approval" in err and "comment `/approve` on it, then start this stage again" in err, err
        s.post_approve(P, unapproved_gate)
        s.invalidate()
    return _job(s, action, args)


def _clean_problems(stt, log=""):
    assert stt["result"]["problems"] == [] and stt["result"]["severity"] is None, (stt["result"], log[-4000:])


def _through_g3(fake, clone, root, styles=None):
    styles = styles or {g: "app" for g in GATES}
    s = Studio.open(clone, github=GitHub(fake.api, "acme", "specs", "tok-dev1"))
    me = Person(name="Dev One", email="dev@example.com", roles=["product_owner", "business_analyst", "architect", "data_engineer"],
                github="dev1", github_id=fake.users["dev1"]["id"])
    teamsetup.open_pr(s, ProjectConfig(mode="dev", github_api=fake.api, aws_region="eu-west-1"), Team(people=[me]), ["@dev1"])
    fake.merge(1)
    s = Studio.open(clone)
    write_local(s.root, {"email": "dev@example.com"})
    import_text(s, P, "visits.md", REQUIREMENT.encode(), REQUIREMENT, [])
    _clean_problems(*_job(s, "analyze"))
    wt = s.worktree_path(P)
    split = yaml.safe_load((wt / "pipelines" / f"{P}.lock.yaml").read_text())["split"]
    assert split and not split.get("ambiguities"), split.get("ambiguities")
    unapproved = approve_and_merge(fake, s, "G1", styles["G1"])
    _clean_problems(*_start(s, "start_g2", "G1" if unapproved else None))
    unapproved = approve_and_merge(fake, s, "G2", styles["G2"])
    st, _ = _start(s, "start_g3", "G2" if unapproved else None)
    for line in st["result"]["unfilled"]:
        rel = line.split(":", 1)[0]
        doc = yaml.safe_load((wt / rel).read_text())
        values = {}
        for keys, _ in required_paths(doc):
            k = ".".join(map(str, keys))
            values[keys] = str(root / ("drop" if rel.startswith("sources/") else "lake")) if "path" in k else "x"
        write_values(wt / rel, values)
    assert not unfilled(wt, P)
    st, log = _job(s, "plan_components")
    assert not st["result"]["no_plans"], "the destination needs an AI plan in this test"
    _clean_problems(st, log)   # R3: plans whose custom steps are coded at S4 are not a problem at S3
    lock = yaml.safe_load((wt / "destinations" / f"{DEST}.lock.yaml").read_text())
    assert [x["id"] for x in lock["plan"]["steps"] if x["op"] == "custom"] == STEPS, (lock["plan"]["steps"], log[-3000:])
    return s, wt, approve_and_merge(fake, s, "G3", styles["G3"])


def test_s3_plans_with_custom_steps_have_no_problems(drops, tmp_path, monkeypatch):
    """K1 R3: at S3 the pipeline examples cannot run the uncoded custom steps (`needs_code`): no problem is reported."""
    use_script(monkeypatch, tmp_path, P)
    fake, clone, root = drops
    s, wt, _ = _through_g3(fake, clone, root)   # asserts plan_components finished with no problems
    ex = json.loads((wt / "review" / P / "examples.json").read_text())
    assert ex["examples"] and {e["status"] for e in ex["examples"]} <= {"pass", "shown", "needs_code"}, ex


def _dest_lock(wt):
    return yaml.safe_load((wt / "destinations" / f"{DEST}.lock.yaml").read_text())


def _tests(wt):
    return json.loads((wt / "review" / P / "tests.json").read_text())


def _row(tests, kind="destination", name=DEST):
    (row,) = [c for c in tests["components"] if c["kind"] == kind and c["name"] == name]
    return row


def _finish(fake, s, wt, root, styles=None, g3_unapproved=False):
    styles = styles or {g: "app" for g in GATES}
    tests = _tests(wt)
    assert tests["version"] == 2 and tests["build_mode"] == "draft" and tests["build_ok"], tests
    assert not tests["dags_differ"] and not [f for f in (wt / "dags").rglob("*") if f.is_file()], "S4 writes no dags/ (A1)"
    unapproved = approve_and_merge(fake, s, "G4", styles["G4"])
    res = _start(s, "start_g5", "G4" if unapproved else None)[0]["result"]
    assert not res["needs_apply"]
    assert json.loads((wt / "review" / P / "dry-run.json").read_text())["ok"]
    assert (wt / "dags" / f"pkf_{P}.py").exists(), "S5 builds the DAG from the approved code"
    lock = _dest_lock(wt)
    assert lock["code_approval"].get("evidence"), "approve-code at S5 carries the G4 evidence"
    unapproved = approve_and_merge(fake, s, "G5", styles["G5"])
    import shutil

    shutil.rmtree(root / "lake", ignore_errors=True)
    res = _start(s, "run_local", "G5" if unapproved else None, {"own_credentials": True, "params": {}, "date": "2026-10-15"})[0]["result"]
    assert res["merged"], res
    report = json.loads(s.read_base(res["report"]))
    assert report["ok"], report
    got = sorted(x.relative_to(root / "lake").as_posix() for x in (root / "lake").rglob("*") if x.is_file())
    assert got == EXPECTED, got
    assert sorted(x.relative_to(root / "drop" / "2026-10-15").as_posix() for x in (root / "drop" / "2026-10-15").rglob("*")
                  if x.is_file()) == sorted(FILES), "copy, never move"
    s.invalidate()
    st = derive(s, P, s.prs(P, 0))
    assert [g.state for g in st["gates"]][:5] == ["approved"] * 5 and st["by_gate"]["RUN"].state == "done", \
        [(g.gate, g.state, g.note) for g in st["gates"]]
    rows = [json.loads(x) for x in s.read_base(f".pkf-studio/approvals/{P}.jsonl").splitlines()]
    assert [r["gate"] for r in rows] == GATES


def test_custom_code_to_local_run(drops, tmp_path, monkeypatch):
    log = use_script(monkeypatch, tmp_path, P)
    fake, clone, root = drops
    s, wt, _ = _through_g3(fake, clone, root)
    stt, jlog = _job(s, "start_g4")
    _clean_problems(stt, jlog)
    lock = _dest_lock(wt)
    assert lock["code"]["tests"] and all(t["status"] in ("pass", "shown") for t in lock["code"]["tests"]), lock["code"]["tests"]
    row = _row(_tests(wt))
    assert row["stage"] == "code_generated" and row["custom_steps"] == STEPS and row["failing"] == [] and row["ai_issues"] == [], row
    assert row["code_file"] == f"destinations/{DEST}.py"
    assert s.git.sha(f"origin/pkf/{P}/g4") == s.git.head(wt), "the S4 job pushed its branch (K2)"
    calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    coder = [c for c in calls if c["task"] == "write_custom_step"]
    assert len(coder) == 2 and "Write the custom step `route_visits`" in coder[0]["prompt"], [c["prompt"][:200] for c in coder]
    _finish(fake, s, wt, root)


def test_s4_recovers_from_a_crashed_code_test(drops, tmp_path, monkeypatch):
    """The code was written but its test run crashed (what the Windows runner bug left behind): the lock records a
    failed load. "Run tests & build again" must re-test it, and the draft build must then pass; Regenerate must work too,
    for the whole component and for one step."""
    use_script(monkeypatch, tmp_path, P)
    fake, clone, root = drops
    s, wt, _ = _through_g3(fake, clone, root)
    _job(s, "start_g4")
    lock_file = wt / "destinations" / f"{DEST}.lock.yaml"
    lock = yaml.safe_load(lock_file.read_text())
    lock["code"]["tests"] = [{"example": "all", "status": "fail", "detail": "code failed to load: TypeError (simulated)"}]
    lock_file.write_text(yaml.safe_dump(lock, sort_keys=False))
    s.commit(P, "simulate the crashed test run")
    assert _row(_tests(wt))["stage"] == "code_generated"   # tests.json still from before the simulated crash
    stt, jlog = _job(s, "test_build")
    _clean_problems(stt, jlog)
    tests = _tests(wt)
    assert tests["build_ok"] and _row(tests)["stage"] == "code_generated", tests
    # the UI offers Regenerate for this component (same rule as ui/workflow.py)
    from pkf_studio.ui.common import Files
    from pkf_studio import ledger as L

    f = Files(s, P)
    coded = [f"{k}:{n}" for k, n in f.components()
             if any(x.get("op") in ("custom", "custom_delivery") for x in ((f.yaml(f"{L.FOLDERS[k]}/{n}.lock.yaml").get("plan") or {})
                                                                         .get("steps") or []))]
    assert coded == [f"destination:{DEST}"], coded
    _clean_problems(*_job(s, "code_component", {"kind": "destination", "name": DEST}))
    assert _tests(wt)["build_ok"]
    _clean_problems(*_job(s, "code_component", {"kind": "destination", "name": DEST, "step": "route_visits"}))
    assert _tests(wt)["build_ok"]
    # uncommitted local edits in the worktree (e.g. a `pkf test` run by hand) do not block the next action; a stray file
    # outside the pipeline (R-phase1 #2, R1e) is left uncommitted and reported, so it never reaches the G4 PR
    subprocess.run([sys.executable, "-P", "-m", "pkf.cli", "test", "destination", DEST], cwd=wt, capture_output=True, timeout=600)
    (wt / "notes-from-sandeep.txt").write_text("my notes\n")
    stt, _ = _job(s, "test_build")
    assert _tests(wt)["build_ok"]
    left = [p for p in stt["result"]["problems"] if "notes-from-sandeep.txt" in p]
    assert len(left) == 1 and left[0].startswith("left uncommitted: notes-from-sandeep.txt is outside pipeline") \
        and "Discard" in left[0], stt["result"]["problems"]
    assert s.read_at(s.git.head(wt), "notes-from-sandeep.txt") is None
    assert s.read_at(f"origin/pkf/{P}/g4", "notes-from-sandeep.txt") is None
    assert (wt / "notes-from-sandeep.txt").exists(), "left where it was"
    (wt / "notes-from-sandeep.txt").unlink()
    _finish(fake, s, wt, root)


def test_failed_ai_step_is_a_problem_not_a_failure(drops, tmp_path, monkeypatch):
    """C2: the coder's AI call fails → the S4 job succeeds *with a problem* that names the step and the next action;
    tests.json shows the component's uncoded steps; the branch is pushed anyway; Run tests & build again recovers."""
    def edit(doc):
        doc["answers"].insert(0, {"task": "write_custom_step", "raise": "llm_error", "times": 1,
                                  "message": "the AI service did not answer (scripted outage)"})
    use_script(monkeypatch, tmp_path, P, edit)
    fake, clone, root = drops
    s, wt, _ = _through_g3(fake, clone, root)
    stt, jlog = _job(s, "start_g4")
    res = stt["result"]
    assert res["ok"] and res["severity"] == "warning" and res["problems"], res
    first = res["problems"][0]
    assert first.startswith(f"pkf code destination {DEST}") and "run it again" in first and "scripted outage" in first, res["problems"]
    assert not any("push" in p for p in res["problems"]), res["problems"]
    assert s.git.sha(f"origin/pkf/{P}/g4") == s.git.head(wt), "pushed although the AI step failed (C1)"
    tests = _tests(wt)
    row = _row(tests)
    # the coder stopped before the first step: WP1 (A8) writes a module without functions, so the component is
    # code_failing "missing custom step function(s)" and S4 codes it again (build_code's resume rule)
    assert row["stage"] == "code_failing" and row["ai_issues"] and row["ai_issues"][0] == first, row
    assert any("missing custom step function" in (f["detail"] or "") for f in row["failing"]), row["failing"]
    assert not tests["build_ok"] and tests["problems"][0] == first, tests
    assert "No generated code yet: destination lake_folder has custom steps route_visits, route_unmatched" in \
        (wt / "review" / P / "capabilities.md").read_text()
    with pytest.raises(Exception, match="draft build failed|build failed|not ready"):
        s.request_approval(P, "G4")
    stt, jlog = _job(s, "test_build")       # Run tests & build again: codes the component that has no code yet
    _clean_problems(stt, jlog)
    assert _tests(wt)["build_ok"] and _row(_tests(wt))["stage"] == "code_generated"
    _finish(fake, s, wt, root)


def test_regenerate_one_step_keeps_the_other(drops, tmp_path, monkeypatch):
    """A8: Regenerate (AI) for one step asks the coder once, for that step, and leaves the other step's function as it is."""
    second = ("def route_unmatched(layout, params, ctx):\n    date = layout[\"vars\"][\"ds\"]\n    for path in layout[\"files\"]:\n"
              "        if path not in layout[\"keys\"]:\n            name = path.split(\"/\")[-1]\n"
              "            layout[\"keys\"][path] = date + \"/unmatched/\" + name if name.endswith(\".csv\") else None\n    return layout")

    def edit(doc):
        i = next(i for i, a in enumerate(doc["answers"]) if a.get("when") == "Write the custom step `route_unmatched`")
        doc["answers"][i]["times"] = 1
        doc["answers"].insert(i + 1, {"task": "write_custom_step", "when": "Write the custom step `route_unmatched`",
                                      "output": {"function_source": second, "explanation": "second version"}})
    log = use_script(monkeypatch, tmp_path, P, edit)
    fake, clone, root = drops
    s, wt, _ = _through_g3(fake, clone, root)
    _job(s, "start_g4")
    code = wt / "destinations" / f"{DEST}.py"
    from pkf.design import extract_functions

    before = extract_functions(code.read_text(), STEPS)
    n_before = len(log.read_text(encoding="utf-8").splitlines())
    stt, jlog = _job(s, "code_component", {"kind": "destination", "name": DEST, "step": "route_unmatched"})
    _clean_problems(stt, jlog)
    calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()[n_before:]]
    assert [c["task"] for c in calls] == ["write_custom_step"], [(c["task"], c["prompt"][:120]) for c in calls]
    assert "Write the custom step `route_unmatched`" in calls[0]["prompt"]
    after = extract_functions(code.read_text(), STEPS)
    assert after["route_visits"] == before["route_visits"] and after["route_unmatched"] == second, after
    tests = _tests(wt)
    assert tests["build_ok"] and _row(tests)["stage"] == "code_generated", tests
    _finish(fake, s, wt, root)


STYLES = ["app", "gh-comment", "gh-merge:merge", "gh-merge:squash", "gh-merge:rebase", "gh-merge-unapproved"]


@pytest.mark.parametrize("offset", range(len(STYLES)), ids=[f"G1-{s}" for s in STYLES])
def test_approvals_from_github_and_from_the_app(drops, tmp_path, monkeypatch, offset):
    """§5.2: every approval style at every gate (rotated across the parametrization), and the pipeline still runs."""
    styles = {g: STYLES[(i + offset) % len(STYLES)] for i, g in enumerate(GATES)}
    use_script(monkeypatch, tmp_path, P)
    fake, clone, root = drops
    s, wt, g3_unapproved = _through_g3(fake, clone, root, styles)
    _start(s, "start_g4", "G3" if g3_unapproved else None)
    _finish(fake, s, wt, root, styles)
