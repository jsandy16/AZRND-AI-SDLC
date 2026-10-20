"""Studio UI (AppTest, 0.5.0): the job panel's problems banner (K1) and the running job's elapsed time (E3), the S4 status
panel from tests.json v2 and v1 (K5, C2/E1) with Restore dags/ from main (R10) and the per-step Regenerate picker (A8),
the Code tab's uncoded steps, flash messages (K10), and the S4 labels in STUDIO.md (E14, S4 part).

Job folders and tests.json files are written by hand here (the shapes of K1 and K5), so the UI is tested on its own;
the stage states are monkeypatched where a test needs S4 to be the current stage."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from fakegithub import FakeGitHub, make_repo
from pkf_studio import jobs, stages, teamsetup
from pkf_studio.service import Studio, dags_problem
from pkf_studio.stages import GateState
from pkf_studio.ui import common, workflow
from test_studio_github import setup_project, write_g1

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "src" / "pkf_studio" / "app.py")
P = "ui_pipe"


@pytest.fixture()
def gh(tmp_path, monkeypatch):
    import streamlit as st

    st.cache_data.clear()        # the app's caches are per process: no stage data from another test
    st.cache_resource.clear()
    bare, clone = make_repo(tmp_path)
    fake = FakeGitHub(bare)
    fake.add_user("dev1", "dev@example.com", token="tok-dev1")
    for k, v in {"GITHUB_TOKEN": "tok-dev1", "PKF_STUDIO_ROOT": str(clone), "PKF_LLM_PROVIDER": "replay",
                 "PKF_LLM_FIXTURES": str(ROOT / "tests" / "llm-fixtures")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("PKF_STUDIO_JOB", raising=False)
    monkeypatch.setattr(teamsetup, "build_wheel", lambda dest, log=print: None)
    s = setup_project(fake, clone)
    yield fake, clone, s
    fake.close()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=True).stdout


def app():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(APP, default_timeout=120)


def no_errors(at):
    assert not at.exception, [e.value for e in at.exception]


def texts(elements) -> list[str]:
    return [str(e.value) for e in elements]


def plain(text: str) -> str:
    """Markdown as read: escapes removed."""
    return re.sub(r"\\(.)", r"\1", text)


def _iso(seconds_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).isoformat()


def _job(root: Path, jid: str, *, title: str, action: str = "test_build", result: dict | None = None, exit: int | None = 0,
         started: float = 60, log: str = "== start\n") -> Path:
    d = jobs.jobs_dir(root) / jid
    d.mkdir(parents=True)
    (d / "cmd.json").write_text(json.dumps({"action": action, "pipeline": P, "args": {}, "title": title,
                                            "started_at": _iso(started)}), encoding="utf-8")
    (d / "log").write_text(log, encoding="utf-8")
    if exit is not None:
        (d / "exit").write_text(str(exit), encoding="utf-8")
        if result is not None:
            (d / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return d


def _clear_jobs(root: Path) -> None:
    import shutil

    shutil.rmtree(jobs.jobs_dir(root), ignore_errors=True)


def _open_pipelines(at, p: str = P):
    at.session_state["pipe-pick"] = p
    at.session_state["page"] = "Pipelines"        # 0.7.0: the app opens on the Workspace; these tests read the Pipelines page
    at.run()
    no_errors(at)
    return at


# ---------------------------------------------------------------------------------------------- job panel (K1, E3)
def test_job_panel_shows_problems_banner(gh):
    fake, clone, s = gh
    write_g1(s, P)
    probs = [f"pkf code destination sas_restructured: step {i} failed — the AI step failed; this is usually temporary: "
             f"run it again (Regenerate (AI) / Run tests & build again)" for i in range(7)]
    _job(s.root, "20261018T090000-aaaaaa", title="Start S4 Code & build",
         result={"ok": True, "examples_ok": True, "problems": probs, "severity": "warning"})
    at = _open_pipelines(app())
    warn = [w for w in at.warning if "Last job" in w.value]
    assert len(warn) == 1, texts(at.warning)
    assert warn[0].icon == "⚠️"
    w = plain(warn[0].value)
    assert w.startswith("Last job: **Start S4 Code & build** — finished with problems")
    assert all(f"step {i} failed" in w for i in range(5)) and "step 5 failed" not in w and "… and 2 more" in w
    assert "All details: Job log and history (below)" in w
    assert not any("Last job" in x for x in texts(at.success))

    # work that ran but failed (K1 severity "error"): red, still "finished with problems"
    _clear_jobs(s.root)
    _job(s.root, "20261018T090100-bbbbbb", title="Run locally", action="run_local",
         result={"ok": True, "problems": ["pkf run ui_pipe: 1 file failed — see review/<p>/runs/<run>/files.csv and run.log"],
                 "severity": "error"})
    at.run()
    no_errors(at)
    err = [plain(x) for x in texts(at.error) if "Last job" in x]
    assert err and "finished with problems" in err[0] and "files.csv and run.log" in err[0]
    assert not any("Last job" in x for x in texts(at.warning))

    # a result written before 0.5.0 (no problems, no severity) shows as before
    _clear_jobs(s.root)
    _job(s.root, "20261018T090200-cccccc", title="Plan components (AI)", result={"ok": True, "components": []})
    at.run()
    no_errors(at)
    ok = [x for x in at.success if "Last job" in x.value]
    assert ok and ok[0].icon == "✅" and plain(ok[0].value) == "Last job: **Plan components (AI)** — succeeded"
    assert not any("Last job" in x for x in texts(at.warning) + texts(at.error))

    # a failed job keeps its error and also lists the problems it collected
    _clear_jobs(s.root)
    _job(s.root, "20261018T090300-dddddd", title="Regenerate (AI)", exit=1,
         result={"ok": False, "error": "the worktree is not on pkf/ui_pipe/g4", "problems": ["could not push pkf/ui_pipe/g4: x"]})
    at.run()
    no_errors(at)
    err = [plain(x) for x in texts(at.error) if "Last job" in x]
    assert err and err[0].startswith("Last job: **Regenerate (AI)** — failed: the worktree is not on pkf/ui_pipe/g4")
    assert "could not push pkf/ui_pipe/g4: x" in err[0]


def test_running_job_shows_elapsed_time_and_last_log_line(gh):
    fake, clone, s = gh
    write_g1(s, P)
    d = _job(s.root, "20261018T091000-eeeeee", title="Start S4 Code & build", exit=None, started=150,
             log="== start_g4\n$ pkf code destination d\n  … still running pkf code destination d (2m30s)\n\n")
    (d / "pid").write_text(str(os.getpid()), encoding="utf-8")
    lock = jobs._lock(s.root, P)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"job": d.name}), encoding="utf-8")
    assert jobs.current(s.root, P) == d.name
    at = _open_pipelines(app())
    box = [plain(x) for x in texts(at.info) if x.startswith("Running:")]
    assert box, texts(at.info)
    b = box[0]
    started = dt.datetime.fromisoformat(json.loads((d / "cmd.json").read_text())["started_at"]).astimezone().strftime("%H:%M:%S")
    assert "**Start S4 Code & build**" in b and f"started {started} (local time)" in b
    m = re.search(r"· (\d+)m(\d\d)s elapsed", b)
    assert m and 2 <= int(m.group(1)) <= 4, b
    assert "Last log line: `… still running pkf code destination d (2m30s)`" in b
    assert "Cancel" in [x.label for x in at.button]


def test_time_helpers():
    assert common.elapsed(5) == "5s" and common.elapsed(150) == "2m30s" and common.elapsed(3720) == "1h02m"
    assert common.elapsed(-3) == "0s"
    assert common.last_line("a\nb\n\n  \n") == "b" and common.last_line("") == ""
    assert common.local_time("not a time") == "not a time"
    t = dt.datetime(2020, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    assert common.local_time(t.isoformat()) == t.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert common.local_time("2020-01-02T03:04:05") == t.astimezone().strftime("%Y-%m-%d %H:%M:%S")  # naive = UTC
    assert common.md_escape("a_b*c <x>") == "a\\_b\\*c \\<x\\>"


# ---------------------------------------------------------------------------------------------- S4 status panel (K5)
CODE = '''def compute_target_keys(layout, params, ctx):
    return layout
'''


def _s4_worktree(s: Studio, p: str = P) -> Path:
    """A pipeline at S4: its worktree on g4 with a source and a destination whose plans have custom steps."""
    wt = s.open_stage(p, "G4")
    files = {
        f"pipelines/{p}.yaml": {"api_version": "pkf/v1", "kind": "pipeline", "name": p, "owner": "dev1", "start_date": "2026-10-01",
                                "connection": p, "requirement": "TRIGGER\n- daily\n", "examples": []},
        f"connections/{p}.yaml": {"api_version": "pkf/v1", "kind": "connection", "name": p, "source": "s_in", "destination": "d_out"},
        "sources/s_in.yaml": {"requirement": "list the files"},
        "sources/s_in.lock.yaml": {"plan": {"steps": [{"id": "pick_files", "op": "custom", "title": "pick"}]}},
        "destinations/d_out.yaml": {"requirement": "move and tag"},
        "destinations/d_out.lock.yaml": {"plan": {"steps": [{"id": "compute_target_keys", "op": "custom"},
                                                            {"id": "move_and_tag", "op": "custom_delivery"}]}},
    }
    for rel, doc in files.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (wt / "destinations" / "d_out.py").write_text(CODE, encoding="utf-8")
    s.commit(p, f"S4 {p}: components")
    return wt


def _tests_json(wt: Path, doc: dict, p: str = P) -> None:
    (wt / "review" / p).mkdir(parents=True, exist_ok=True)
    (wt / "review" / p / "tests.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _at_s4(monkeypatch, s: Studio) -> None:
    """Workflow shows S4 as the current stage, already started (G1–G3 approved)."""
    def pr(n):
        return {"number": n, "html_url": f"https://github.example/acme/specs/pull/{n}", "state": "closed",
                "merged_at": "2026-10-17T10:00:00Z", "head": {"sha": "0" * 40}}

    rows = [GateState("G1", "approved", pr(2)), GateState("G2", "approved", pr(3)), GateState("G3", "approved", pr(4)),
            GateState("G4", "in_progress"), GateState("G5", "blocked", None, "an earlier gate must be approved first"),
            GateState("RUN", "blocked")]
    states = {"gates": rows, "current": "G4", "by_gate": {r.gate: r for r in rows}}
    monkeypatch.setattr(stages, "derive", lambda *a, **k: states)
    monkeypatch.setattr(workflow, "stage_started", lambda *a, **k: True)


def _workflow(at):
    at.session_state["pipe-pick"] = P
    at.session_state["tab"] = "Workflow"
    at.run()
    no_errors(at)
    return at


V2 = {
    "version": 2, "build_mode": "draft", "build_ok": False,
    "build_issues": [{"severity": "error", "code": "component_not_ready", "file": "destinations/d_out.yaml",
                      "msg": "destination 'd_out' is not ready for a draft build (code_failing): fix destinations/d_out.py"}],
    "examples_ok": False,
    "examples": [{"example": "cdar_file", "status": "fail", "detail": "destination step 'move_and_tag' has no code yet"},
                 {"example": "readme", "status": "pass", "detail": ""}],
    "components": [
        {"kind": "source", "name": "s_in", "stage": "plan_approved", "mode": "requirement", "next": "pkf code source s_in",
         "custom_steps": ["pick_files"], "code_file": None, "failing": [],
         "ai_issues": ["pkf code source s_in: budget — the AI step stopped at its turn or budget limit: run it again"]},
        {"kind": "destination", "name": "d_out", "stage": "code_failing", "mode": "requirement", "next": "fix destinations/d_out.py",
         "custom_steps": ["compute_target_keys", "move_and_tag"], "code_file": "destinations/d_out.py",
         "failing": [{"step": "move_and_tag", "case": "normal_move_and_tag", "example": None, "detail": "KeyError: 'delivery'"},
                     {"step": None, "case": None, "example": "visit_zero", "detail": "expected 3 keys, got 2"}],
         "ai_issues": []},
        {"kind": "transformation", "name": "t_x", "stage": "code_changed", "mode": "requirement", "next": "pkf test transformation t_x",
         "custom_steps": [], "code_file": "transformations/t_x.py", "failing": [], "ai_issues": []},
        {"kind": "trigger", "name": "tr", "stage": "plan_proposed", "mode": "requirement", "next": "review", "custom_steps": [],
         "code_file": None, "failing": [], "ai_issues": []},
    ],
    "dags_differ": False,
    "problems": ["pkf code source s_in: budget — the AI step stopped at its turn or budget limit: run it again"],
}


def test_s4_panel_lists_components_with_actions(gh, monkeypatch):
    fake, clone, s = gh
    wt = _s4_worktree(s)
    _tests_json(wt, V2)
    s.commit(P, f"S4 {P}: tests, draft build and capability summary")
    _at_s4(monkeypatch, s)
    started = []
    monkeypatch.setattr(jobs, "start", lambda root, p, action, args=None, title="": started.append((action, args, title)) or "j1")
    at = _workflow(app())
    md = plain(" ".join(texts(at.markdown)))
    assert "S4 status" in md and f"review/{P}/tests.json" in md
    errors = [plain(x) for x in texts(at.error)]
    assert any(x.startswith("Draft build (checks everything, writes nothing") and x.endswith("failed") for x in errors)
    assert any("not ready for a draft build (code_failing)" in x for x in errors)
    assert "End-to-end examples**: some fail" in md
    # one row per component, each with its action
    assert "**source `s_in`** · `plan_approved`" in md and "**destination `d_out`** · `code_failing`" in md
    warnings = [plain(x) for x in texts(at.warning)]
    assert any(x.startswith("code not written yet") for x in warnings)
    assert "code fails its tests" in errors
    assert any(x.startswith("the code changed since its last test") for x in warnings)
    assert any(x.startswith("the plan changed: Reopen G3") for x in warnings)
    captions = [plain(x) for x in texts(at.caption)]
    assert any("the AI step stopped at its turn or budget limit" in x for x in captions)
    assert any("or edit only the function body in `destinations/d_out.py`, then Run tests & build again — if a test case "
               "itself is wrong, Reopen G3 and re-plan destination d_out" in x for x in captions)
    failing = [df.value for df in at.dataframe if "step" in df.value.columns]
    assert failing and list(failing[0]["case"]) == ["normal_move_and_tag", "visit_zero"] and "KeyError: 'delivery'" in list(failing[0]["detail"])
    labels = [b.label for b in at.button]
    assert "Regenerate (AI): move_and_tag" in labels and "Regenerate (AI): compute_target_keys" not in labels
    assert labels.count("Run tests & build again") == 2          # the panel's and the code_changed row's
    assert labels.count("Regenerate (AI)") == 2                  # the plan_approved row's and the picker's
    assert not any("Restore dags/" in x for x in labels)         # dags/ is the same as on main
    with_problems = [x for x in at.expander if x.label.startswith("Problems of the last S4 job")]
    assert with_problems
    # the per-step buttons start code_component with the step
    next(b for b in at.button if b.label == "Regenerate (AI): move_and_tag").click()
    at.run()
    no_errors(at)
    assert started[-1][:2] == ("code_component", {"kind": "destination", "name": "d_out", "step": "move_and_tag"})
    # the picker lists every component with custom steps, whole and per step
    pick = at.selectbox(key="recode-pick")
    assert pick.options == ["source:s_in (missing or failing steps)", "source:s_in · pick_files",
                            "destination:d_out (missing or failing steps)", "destination:d_out · compute_target_keys",
                            "destination:d_out · move_and_tag"]
    pick.set_value("destination:d_out · compute_target_keys")
    at.run()
    at.button(key="recode").click()
    at.run()
    no_errors(at)
    assert started[-1][:2] == ("code_component", {"kind": "destination", "name": "d_out", "step": "compute_target_keys"})
    at.selectbox(key="recode-pick").set_value("source:s_in (missing or failing steps)")
    at.run()
    at.button(key="recode").click()
    at.run()
    assert started[-1][:2] == ("code_component", {"kind": "source", "name": "s_in"})
    at.button(key="s4-code-source-s_in").click()                 # the plan_approved row's button
    at.run()
    assert started[-1][:2] == ("code_component", {"kind": "source", "name": "s_in"})


def test_s4_panel_ready_for_review_and_build_ok(gh, monkeypatch):
    fake, clone, s = gh
    wt = _s4_worktree(s)
    doc = {**V2, "build_ok": True, "build_issues": [], "examples_ok": True,
           "examples": [{"example": "cdar_file", "status": "pass", "detail": ""}],
           "components": [{**V2["components"][1], "stage": "code_generated", "failing": []}], "problems": []}
    _tests_json(wt, doc)
    s.commit(P, "tests")
    _at_s4(monkeypatch, s)
    at = _workflow(app())
    succ = [plain(x) for x in texts(at.success)]
    assert any(x.startswith("Draft build (checks everything") and x.endswith(": ok") for x in succ)
    assert any(x.startswith("ready for review: the code in `destinations/d_out.py` passes its tests") for x in succ)
    assert "End-to-end examples**: all pass" in plain(" ".join(texts(at.markdown)))
    assert not [x for x in at.expander if x.label.startswith("Problems of the last S4 job")]


def test_s4_panel_coder_stopped_part_way(gh, monkeypatch):
    """A coder that stopped (AI error) leaves a module without the step functions: stage code_failing with "missing custom
    step function(s)" — shown as code not written yet, with Regenerate (AI) for the component (A8 resumes it)."""
    fake, clone, s = gh
    wt = _s4_worktree(s)
    row = {**V2["components"][1], "failing": [
        {"step": None, "case": None, "example": None, "detail": "missing custom step function(s) ['compute_target_keys', 'move_and_tag']"},
        {"step": None, "case": None, "example": "cdar_file", "detail": "not run: fix the problems first"}],
        "ai_issues": ["pkf code destination d_out: the AI coder stopped at step compute_target_keys: no answer — the AI step failed"]}
    _tests_json(wt, {**V2, "components": [row]})
    s.commit(P, "tests")
    _at_s4(monkeypatch, s)
    started = []
    monkeypatch.setattr(jobs, "start", lambda root, p, action, args=None, title="": started.append((action, args)) or "j1")
    at = _workflow(app())
    w = [plain(x) for x in texts(at.warning) if x.startswith("code not written yet")]
    assert w and w[0].startswith("code not written yet for `compute_target_keys`, `move_and_tag`"), texts(at.warning)
    assert "code fails its tests" not in texts(at.error)
    assert any("the AI coder stopped at step compute_target_keys" in plain(x) for x in texts(at.caption))
    at.button(key="s4-code-destination-d_out").click()
    at.run()
    no_errors(at)
    assert started == [("code_component", {"kind": "destination", "name": "d_out"})]
    assert workflow.uncoded_steps({"failing": [{"detail": "x"}, "junk"]}) == []


def test_s4_panel_reads_tests_json_v1(gh, monkeypatch):
    """A tests.json written by 0.4.0 (no version: build_ok, build_issues, examples) still renders; the Regenerate picker
    then comes from the component locks."""
    fake, clone, s = gh
    wt = _s4_worktree(s)
    _tests_json(wt, {"examples": [{"example": "cdar_file", "status": "fail", "detail": "boom"}], "build_ok": False,
                     "build_issues": [{"severity": "error", "msg": "destination 'd_out' is not approved (code_generated)"}]})
    s.commit(P, "S4: tests, build and capability summary")
    _at_s4(monkeypatch, s)
    at = _workflow(app())
    errors = [plain(x) for x in texts(at.error)]
    assert "Build: failed" in errors and any("is not approved (code_generated)" in x for x in errors)
    assert "End-to-end examples**: some fail" in plain(" ".join(texts(at.markdown)))
    assert any("written by an earlier version of the Studio: Run tests & build again" in x for x in texts(at.caption))
    assert at.selectbox(key="recode-pick").options == [
        "source:s_in (missing or failing steps)", "source:s_in · pick_files", "destination:d_out (missing or failing steps)",
        "destination:d_out · compute_target_keys", "destination:d_out · move_and_tag"]
    # no tests.json at all
    (wt / "review" / P / "tests.json").unlink()
    s.commit(P, "drop")
    at.run()
    no_errors(at)
    assert any("No tests or build yet: Run tests & build again." in x for x in texts(at.caption))


def test_s4_panel_restores_dags_from_main(gh, monkeypatch):
    fake, clone, s = gh
    wt = _s4_worktree(s)
    (wt / "dags" / "pkf" / P).mkdir(parents=True)
    (wt / "dags" / f"pkf_{P}.py").write_text("# built by 0.4.0 at S4\n", encoding="utf-8")
    (wt / "dags" / "pkf" / P / "destination_d_out.py").write_text("x = 1\n", encoding="utf-8")
    _tests_json(wt, {**V2, "dags_differ": True, "problems": [dags_problem("main")]})
    s.commit(P, f"S4 {P}: tests, build and capability summary")
    _at_s4(monkeypatch, s)
    at = _workflow(app())
    assert dags_problem("main") in texts(at.warning)
    assert any(f"dags/pkf_{P}.py" in x for x in texts(at.caption))
    # the dags/ problem is shown once (as the warning), not again in the problems list
    assert not [x for x in at.expander if x.label.startswith("Problems of the last S4 job")]
    at.button(key="restore-dags").click()
    at.run()
    no_errors(at)
    assert _git(wt, "log", "-1", "--format=%s").strip() == f"S4 {P}: dags/ restored from main"
    assert not (wt / "dags" / f"pkf_{P}.py").exists() and s.dags_diff(P) == []
    assert any(x.startswith("dags/ restored from main") for x in texts(at.success))   # the flash survived the rerun
    assert "Restore dags/ from main" not in [b.label for b in at.button]
    assert dags_problem("main") not in texts(at.warning)
    at.run()
    no_errors(at)
    assert not any(x.startswith("dags/ restored from main") for x in texts(at.success))  # shown once


def test_restore_dags_failure_is_shown_after_the_rerun(gh, monkeypatch):
    fake, clone, s = gh
    wt = _s4_worktree(s)
    (wt / "dags").mkdir(exist_ok=True)
    (wt / "dags" / f"pkf_{P}.py").write_text("# old\n", encoding="utf-8")
    _tests_json(wt, {**V2, "dags_differ": True})
    s.commit(P, "S4: build")
    _at_s4(monkeypatch, s)
    from pkf_studio.service import StudioError

    def refuse(self, p):
        raise StudioError("a job is running for ui_pipe; wait for it to finish")

    monkeypatch.setattr(Studio, "restore_dags_from_base", refuse)
    at = _workflow(app())
    at.button(key="restore-dags").click()
    at.run()
    no_errors(at)
    assert "a job is running for ui_pipe; wait for it to finish" in texts(at.error)


def test_request_approval_checks_dags_like_the_service(gh, monkeypatch):
    """The Workflow's readiness check passes the live dags/ difference (as Request approval does)."""
    fake, clone, s = gh
    wt = _s4_worktree(s)
    (wt / "dags").mkdir(exist_ok=True)
    (wt / "dags" / f"pkf_{P}.py").write_text("# old\n", encoding="utf-8")
    _tests_json(wt, {**V2, "build_ok": True, "build_issues": [], "examples_ok": True, "examples": [], "components": [],
                     "dags_differ": False, "problems": []})
    s.commit(P, "S4: build")
    assert dags_problem(s.base) in workflow.ready_problems(s, P, "G4")


# ---------------------------------------------------------------------------------------------- Code tab, Setup
def test_code_tab_names_steps_without_code(gh, monkeypatch):
    fake, clone, s = gh
    _s4_worktree(s)
    _at_s4(monkeypatch, s)
    at = app()
    at.session_state["pipe-pick"] = P
    at.session_state["tab"] = "Destination"
    at.run()
    no_errors(at)
    w = [plain(x) for x in texts(at.warning) if x.startswith("Custom steps without code yet")]
    assert w and "`move_and_tag`" in w[0] and "compute_target_keys" not in w[0] and "Regenerate (AI)" in w[0]
    assert at.button(key="uncoded-destination").label == "Open Workflow"


def test_setup_shows_build_and_runtime_path(gh):
    import pkf_runtime
    import pkf_studio

    at = app()
    at.session_state["page"] = "Setup"
    at.run()
    no_errors(at)
    cap = " ".join(texts(at.caption))
    assert f"pkf Studio {getattr(pkf_studio, 'BUILD', pkf_studio.VERSION)}" in cap
    assert Path(pkf_runtime.__file__).resolve().parent.as_posix() in cap


# ---------------------------------------------------------------------------------------------- docs (E14)
def _section(doc: str, heading: str) -> str:
    return doc.split(heading, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]


def _missing_labels(text: str) -> list[str]:
    labels = {re.sub(r"\s+", " ", x) for x in re.findall(r"\*\*([^*]+)\*\*", text)}
    assert labels
    ui = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "pkf_studio").rglob("*.py")
                   if p.parent.name == "ui" or p.name in ("app.py", "service.py"))
    out = []
    for x in sorted(labels):
        if x in ui or x.replace(" from main", " from {s.base}") in ui:
            continue
        if "<" in x:   # a label with a value in it: "Move <n> files", "Post /approve on PR #n as me"
            parts = [re.escape(y) for y in re.split(r"<[^>]+>", x)]
            if re.search(r"\{[^}]+\}".join(parts), ui):
                continue
        out.append(x)
    return out


def test_s4_labels_in_docs_exist():
    """Every bold label in STUDIO.md's S4 row and "S4 troubleshooting" table exists in the UI code."""
    doc = (ROOT / "docs" / "STUDIO.md").read_text(encoding="utf-8")
    row = next(ln for ln in doc.splitlines() if ln.startswith("| S4 "))
    assert _missing_labels(row + _section(doc, "### S4 troubleshooting")) == []


def test_button_labels_in_docs_exist():
    """E14: every bold label in STUDIO.md's S4 and S6 rows and sections exists in the UI code."""
    doc = (ROOT / "docs" / "STUDIO.md").read_text(encoding="utf-8")
    rows = "\n".join(ln for ln in doc.splitlines() if ln.startswith(("| S4 ", "| S6 ")))
    text = rows + _section(doc, "### S4 troubleshooting") + _section(doc, "### S6 preview and move")
    assert "Preview this run" in text and "Packages in this run (max)" in text
    assert _missing_labels(text) == []


# ================================================================================================ Phase 2
def ok_render(at):
    """No exception, and no tab that could not be shown (app.main catches a tab's error to keep the page up)."""
    no_errors(at)
    bad = [x for x in texts(at.error) if x.startswith("This tab could not be shown completely")]
    assert not bad, bad


def _g1_pr(s: Studio, fake: FakeGitHub, p: str = P) -> dict:
    write_g1(s, p)
    s.commit(p, f"S1 {p}: requirement")
    return s.request_approval(p, "G1")


def _states_patch(monkeypatch, rows: list[GateState], current: str, started: bool = True) -> None:
    states = {"gates": rows, "current": current, "by_gate": {r.gate: r for r in rows}}
    monkeypatch.setattr(stages, "derive", lambda *a, **k: states)
    monkeypatch.setattr(workflow, "stage_started", lambda *a, **k: started)


def _merged(n: int) -> dict:
    return {"number": n, "html_url": f"https://github.example/acme/specs/pull/{n}", "state": "closed",
            "merged_at": "2026-10-17T10:00:00Z", "head": {"sha": "0" * 40}}


def _approved_until(gate: str) -> list[GateState]:
    from pkf_studio.gates import ORDER

    rows = []
    for i, g in enumerate(ORDER):
        rows.append(GateState(g, "approved", _merged(i + 2)) if ORDER.index(g) < ORDER.index(gate) else
                    GateState(g, "in_progress" if g == gate else "blocked"))
    return rows


def _busy(s: Studio, action: str = "test_build", title: str = "Run tests & build again") -> Path:
    d = _job(s.root, "20261018T095900-ffffff", title=title, action=action, exit=None, started=30, log="== x\nworking\n")
    (d / "pid").write_text(str(os.getpid()), encoding="utf-8")
    lock = jobs._lock(s.root, P)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"job": d.name}), encoding="utf-8")
    assert jobs.current(s.root, P) == d.name
    return d


def _g3_worktree(s: Studio, p: str = P) -> Path:
    wt = s.open_stage(p, "G3")
    files = {
        f"pipelines/{p}.yaml": {"api_version": "pkf/v1", "kind": "pipeline", "name": p, "owner": "dev1", "start_date": "2026-10-01",
                                "connection": p, "requirement": "TRIGGER\n- daily\n", "examples": []},
        f"connections/{p}.yaml": {"api_version": "pkf/v1", "kind": "connection", "name": p, "source": "s_in", "destination": "d_out"},
        "sources/s_in.yaml": {"type": "s3", "connection": {"bucket": "src-bucket"}, "requirement": "list the files"},
        "destinations/d_out.yaml": {"type": "s3", "connection": {"bucket": "<REQUIRED>"}, "requirement": "move"},
    }
    for rel, doc in files.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    s.commit(p, f"S3 {p}: components")
    return wt


# ---------------------------------------------------------------------------------------------- K10 flash (E7, E16)
def test_flash_survives_rerun_after_failed_save(gh, monkeypatch):
    """s7: a value that looks like a secret is refused by the commit; the error is shown after the rerun, and the file
    is put back as it was (nothing half-saved stays in the worktree)."""
    fake, clone, s = gh
    wt = _g3_worktree(s)
    _states_patch(monkeypatch, _approved_until("G3"), "G3")
    at = app()
    at.session_state["pipe-pick"] = P
    at.session_state["tab"] = "Destination"
    at.run()
    ok_render(at)
    at.text_input(key="env-destination-d_out-connection.bucket").set_value("AKIAIOSFODNN7EXAMPLE")
    next(b for b in at.button if b.label == "Save values").click()
    at.run()
    ok_render(at)
    errs = [plain(x) for x in texts(at.error)]
    assert any(x.startswith("not saved:") and "AKIA" in x for x in errs), errs
    assert "<REQUIRED>" in (wt / "destinations" / "d_out.yaml").read_text(encoding="utf-8")
    assert s.git.is_clean(wt)
    at.run()
    assert not any(x.startswith("not saved:") for x in texts(at.error))   # shown once


def test_save_commit_shows_no_toast_on_error(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    from pkf_studio.service import StudioError

    def refuse(self, p, message):
        raise StudioError("the commit was refused: boom")

    monkeypatch.setattr(Studio, "commit", refuse)
    at = _open_pipelines(app())
    at.button(key=f"save-req-{P}").click()
    at.run()
    ok_render(at)
    assert "the commit was refused: boom" in texts(at.error)
    assert not at.toast and not any("Nothing to commit" in x for x in texts(at.success))


# ---------------------------------------------------------------------------------------------- E9 refresh, E11 checklist
def test_merge_refreshes_without_reopening_studio(gh, monkeypatch):
    fake, clone, s = gh
    pr = _g1_pr(s, fake)
    s.post_approve(P, "G1")
    opened = []
    real = Studio.open.__func__

    def counting(cls, *a, **k):
        opened.append(k.get("interactive"))
        return real(cls, *a, **k)

    monkeypatch.setattr(Studio, "open", classmethod(counting))
    at = app()
    at.session_state["pipe-pick"] = P
    at.session_state["tab"] = "Workflow"
    at.run()
    ok_render(at)
    assert opened == [True]                          # the UI's interactive client (K9)
    merge = at.button(key="merge-G1")
    assert not merge.disabled
    merge.click()
    at.run()
    ok_render(at)
    assert fake.pulls[pr["number"]].get("merged_at")
    assert any(x.startswith("G1 merged") for x in texts(at.success))
    assert opened == [True] and "_studio_nonce" not in at.session_state      # the same Studio, read again (E9)
    assert "🟢 approved" in " ".join(texts(at.markdown))


def test_request_approval_disabled_with_checklist(gh, monkeypatch):
    fake, clone, s = gh
    wt = _s4_worktree(s)
    _tests_json(wt, V2)
    s.commit(P, "tests")
    _at_s4(monkeypatch, s)
    at = _workflow(app())
    btn = at.button(key=f"req-G4-{P}")
    assert btn.disabled
    md = plain(" ".join(texts(at.markdown)))
    assert "Before Request approval (G4)" in md and "the draft build failed" in md and "see the S4 status panel" in md
    # ready: the list goes away and the button is enabled
    _tests_json(wt, {**V2, "build_ok": True, "build_issues": [], "examples_ok": True, "examples": [], "components": [], "problems": []})
    s.commit(P, "ok")
    at.run()
    ok_render(at)
    assert "Before Request approval (G4)" not in plain(" ".join(texts(at.markdown)))
    assert not at.button(key=f"req-G4-{P}").disabled
    assert workflow.with_fix("run the tests and the build first", "G4") == "run the tests and the build first → Run tests & build again"


# ---------------------------------------------------------------------------------------------- jobs (E12, E17, E18)
def test_crashed_job_shows_cause_and_next_step(gh):
    fake, clone, s = gh
    write_g1(s, P)
    d = _job(s.root, "20261018T092000-cccccc", title="Start S4 Code & build", action="start_g4", exit=None, started=300,
             log="== start_g4\n$ pkf code destination d\n")
    (d / "crash.log").write_text("Traceback (most recent call last):\nMemoryError\n", encoding="utf-8")
    assert jobs.status(s.root, d.name)["state"] == "crashed"
    at = _open_pipelines(app())
    err = [plain(x) for x in texts(at.error) if "crashed" in x]
    assert err, texts(at.error)
    e = err[0]
    assert "The job's process ended without finishing (laptop asleep, killed, or Python crashed). What it did so far is committed." in e
    assert "Run 'Start S4 Code & build' again." in e
    assert "Last log line: `$ pkf code destination d`" in e and "MemoryError" in e
    assert (jobs.jobs_dir(s.root) / d.name).as_posix() in e


def test_no_cancel_for_run_local(gh):
    fake, clone, s = gh
    write_g1(s, P)
    _busy(s, action="run_local", title="Run locally")
    at = _open_pipelines(app())
    assert "Cancel" not in [b.label for b in at.button]
    assert "a local run may be applying Terraform: wait for it" in texts(at.caption)
    assert any(plain(x).startswith("Running:") and "elapsed" in x for x in texts(at.info))
    # the running jobs of the other pipelines are listed (E18): 0.7.0 the "▶ n running elsewhere" chip of the context bar
    # (it was the sidebar), which lists every pipeline but the one shown — so look from another selection
    at.session_state["pipe-pick"] = "➕ New pipeline"
    at.run()
    side = [b for b in at.button if b.label.startswith(f"▶ {P}")]
    assert side and "Run locally" in side[0].label


def test_git_buttons_disabled_while_busy(gh):
    fake, clone, s = gh
    _g1_pr(s, fake)
    s.post_approve(P, "G1")
    _busy(s)
    at = app()
    at.session_state["pipe-pick"] = P
    at.session_state["tab"] = "Workflow"
    at.run()
    ok_render(at)
    for key in ("merge-G1", "approve-G1", f"req-G1-{P}"):
        assert at.button(key=key).disabled, key


# ---------------------------------------------------------------------------------------------- C3, C5, C9, C10, C20
def test_diverged_card_offers_three_choices(gh, tmp_path):
    from pkf_studio import service as SV

    fake, clone, s = gh
    wt = write_g1(s, P)
    s.commit(P, "S1: requirement")
    s.push(P)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(fake.bare), str(other))
    _git(other, "checkout", "-q", f"pkf/{P}/g1")
    (other / "requirements" / P / "theirs.md").write_text("theirs\n", encoding="utf-8")
    _git(other, "add", ".")
    _git(other, "-c", "user.email=o@example.com", "-c", "user.name=O", "commit", "-qm", "theirs")
    _git(other, "push", "-q", "origin", f"pkf/{P}/g1")
    (wt / "requirements" / P / "mine.md").write_text("mine\n", encoding="utf-8")
    assert s.commit(P, "mine")
    s.fetch(force=True)
    assert s.branch_sync(P, "G1")["state"] == "diverged"
    at = app()
    at.session_state["pipe-pick"] = P
    at.session_state["tab"] = "Workflow"
    at.run()
    ok_render(at)
    assert any(SV.DIVERGED_CARD in plain(x) for x in texts(at.error))
    labels = [b.label for b in at.button]
    assert SV.TAKE_LABEL in labels and SV.MERGE_LABEL in labels and SV.OVERWRITE_LABEL in labels
    at.button(key="div-take-G1").click()
    at.run()
    ok_render(at)
    assert any(x.startswith("took GitHub's copy") and "refs/pkf-archive/" in x for x in texts(at.success))
    assert (wt / "requirements" / P / "theirs.md").exists() and not (wt / "requirements" / P / "mine.md").exists()
    assert SV.TAKE_LABEL not in [b.label for b in at.button]
    assert [x for x in at.expander if x.label.startswith("Set-aside work (after Reopen)")]


def test_reopen_asks_for_confirmation_and_lists_archives(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    rows = [GateState("G1", "approved", _merged(2)), GateState("G2", "in_progress"), GateState("G3", "blocked"),
            GateState("G4", "blocked"), GateState("G5", "blocked"), GateState("RUN", "blocked")]
    _states_patch(monkeypatch, rows, "G2")
    calls = []
    monkeypatch.setattr(Studio, "reopen_preview", lambda self, p, g: [
        {"branch": f"pkf/{P}/g2", "tip": "a" * 40, "unpushed": 2, "open_pr": 7}])
    monkeypatch.setattr(Studio, "reopen", lambda self, p, g: calls.append((p, g)))
    monkeypatch.setattr(Studio, "archives", lambda self, p: [
        {"ref": f"refs/pkf-archive/{P}/g2-20261018T090000", "sha": "b" * 40, "subject": "S2 work", "date": "2026-10-18"}])
    at = app()
    at.session_state["pipe-pick"] = P
    at.session_state["tab"] = "Workflow"
    at.run()
    ok_render(at)
    at.button(key="reopen-approved-G1").click()
    at.run()
    ok_render(at)
    assert not calls
    w = [plain(x) for x in texts(at.warning) if x.startswith("Reopen G1 sets aside")]
    assert w and w[0].startswith(f"Reopen G1 sets aside 2 unpushed commits on g2 (kept as refs/pkf-archive/{P}/…)")
    assert "PR #7 is closed" in w[0]
    at.button(key="reopen-approved-G1-confirm").click()
    at.run()
    ok_render(at)
    assert calls == [(P, "G1")]
    assert any(x.startswith("G1 reopened") for x in texts(at.success))
    df = [d.value for d in at.dataframe if "restore" in d.value.columns]
    assert df and list(df[0]["restore"]) == [f"git branch {P}-g2-20261018T090000 refs/pkf-archive/{P}/g2-20261018T090000"]


def test_merged_unapproved_offers_post_approve(gh):
    from pkf_studio.service import MergedUnapproved

    fake, clone, s = gh
    pr = _g1_pr(s, fake)
    fake.merge(pr["number"])
    s.invalidate()
    s.fetch(force=True)
    with pytest.raises(MergedUnapproved):
        s.ledger_row(P, "G1")
    at = app()
    at.session_state["pipe-pick"] = P
    at.session_state["tab"] = "Workflow"
    at.run()
    ok_render(at)
    md = " ".join(texts(at.markdown))
    assert "🟠 merged without approval" in md
    assert not any(b.label.startswith("Start ") for b in at.button)        # no Start for S2 (K14)
    btn = at.button(key="mu-approve-G1")
    assert btn.label == f"Post /approve on PR #{pr['number']} as me"
    assert "Check again" in [b.label for b in at.button]
    btn.click()
    at.run()
    ok_render(at)
    assert s.merged_unapproved(P) is None
    assert "🟢 approved" in " ".join(texts(at.markdown))
    assert any(b.label.startswith("Start ") for b in at.button)


def test_repair_worktree_button(gh):
    fake, clone, s = gh
    wt = write_g1(s, P)
    s.commit(P, "S1")
    (wt / ".git").unlink()
    at = _open_pipelines(app())
    at.session_state["tab"] = "Workflow"
    at.run()
    ok_render(at)
    at.button(key="repair-wt").click()
    at.run()
    ok_render(at)
    assert (s.worktree_path(P) / ".git").exists() and s.worktree_branch(P) == f"pkf/{P}/g1"
    assert any(x.startswith("the worktree is repaired") for x in texts(at.success))


def test_start_warns_about_commits_not_in_the_merged_pr(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    _states_patch(monkeypatch, _approved_until("G4"), "G4", started=False)
    monkeypatch.setattr(Studio, "unmerged_local_commits", lambda self, p, g: 2 if g == "G3" else 0)
    at = _workflow(app())
    assert any(plain(x).startswith("2 commits on g3 were not in the merged PR #4: Reopen G3 to include them") for x in texts(at.warning))
    assert at.button(key="start-G4")


# ---------------------------------------------------------------------------------------------- offline (C14/E4/E10)
def test_tabs_render_when_github_unreachable(gh):
    """s5: GitHub goes away while the app is open; s1: it is unreachable when the app opens. Every tab renders from this
    computer's copy, a banner says why, and nothing is a traceback."""
    import streamlit as st

    fake, clone, s = gh
    write_g1(s, P)
    s.commit(P, "S1")
    at = _open_pipelines(app())
    fake.close()
    at.button(key="refresh").click()                       # 0.7.0: Refresh is in the context bar (was the sidebar)
    at.run()
    for tab in ("Requirements", "Source", "Destination", "Trigger", "Transformation", "Pipeline", "Workflow"):
        at.session_state["tab"] = tab
        at.run()
        ok_render(at)
        assert any(x.startswith("GitHub unreachable:") for x in texts(at.warning)), (tab, texts(at.warning))
    assert "❔ unknown (cannot reach GitHub)" in " ".join(texts(at.markdown))
    at.button(key="retry-offline").click()                 # still down: the banner stays, no traceback
    at.run()
    ok_render(at)
    assert any(x.startswith("GitHub unreachable:") for x in texts(at.warning))
    st.cache_resource.clear()                              # s1: a new start of the app
    st.cache_data.clear()
    at2 = _open_pipelines(app())
    ok_render(at2)
    assert any(x.startswith("GitHub unreachable:") for x in texts(at2.warning))
    assert f"Showing worktree · pkf/{P}/g1" in " ".join(texts(at2.caption)) or at2.title[0].value == P


# ---------------------------------------------------------------------------------------------- S5 / S6 (D1, D8, D10/E2)
def test_g5_card_renders_dry_run_rows(gh, monkeypatch):
    fake, clone, s = gh
    wt = s.open_stage(P, "G5")
    (wt / "review" / P).mkdir(parents=True)
    (wt / "infra" / P).mkdir(parents=True)
    (wt / "review" / P / "dry-run.json").write_text(json.dumps({"ok": True, "source_versioning": "Off", "dry_run": {
        "note": "nothing was changed", "sample": [{"package": "a", "source": "in/a.csv", "size": 1, "target": "out/a.csv"},
                                                  {"package": "b", "source": "in/b.csv", "size": 2, "target": "out/b.csv"}]}}),
        encoding="utf-8")
    (wt / "infra" / P / "manifest.json").write_text(json.dumps({"needs_apply": False, "note": "nothing to apply"}), encoding="utf-8")
    s.commit(P, "S5")
    _states_patch(monkeypatch, _approved_until("G5"), "G5")
    at = _workflow(app())
    rows = [d.value for d in at.dataframe if "target" in d.value.columns]
    assert rows and list(rows[0]["target"]) == ["out/a.csv", "out/b.csv"]
    assert any("are irreversible (versioning is off)" in x for x in texts(at.warning))


def test_failed_run_is_red(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    rows = [GateState(g, "approved", _merged(i + 2)) for i, g in enumerate(("G1", "G2", "G3", "G4", "G5"))]
    rows.append(GateState("RUN", "failed", _merged(9), "x.csv: AccessDenied"))
    _states_patch(monkeypatch, rows, "RUN")
    at = _workflow(app())
    assert "❌ last run failed" in " ".join(texts(at.markdown))
    assert "x.csv: AccessDenied" in texts(at.error)
    assert "Run again" in [b.label for b in at.button]


def test_s6_preview_then_move_button(gh, monkeypatch):
    from pkf_studio import STUDIO_DIR
    from pkf_studio import actions as A

    fake, clone, s = gh
    write_g1(s, P)
    s.commit(P, "S1")
    rows = [GateState(g, "approved", _merged(i + 2)) for i, g in enumerate(("G1", "G2", "G3", "G4", "G5"))]
    rows.append(GateState("RUN", "ready"))
    _states_patch(monkeypatch, rows, "RUN")
    monkeypatch.setattr(A, "_custom_delivery", lambda read, p: True)
    started = []
    monkeypatch.setattr(jobs, "start", lambda root, p, action, args=None, title="": started.append((action, args)) or "j1")
    at = _workflow(app())
    assert at.number_input(key="run-max").value == 10                     # no successful run yet
    assert "Run locally" not in [b.label for b in at.button]              # custom delivery: preview, then move
    at.button(key="preview").click()
    at.run()
    ok_render(at)
    assert started[-1] == ("preview_run", {"params": {}, "max_packages": 10})
    # the preview's result (written by the job)
    run = "run-20261018t090000"
    _git(clone, "branch", f"pkf/{P}/{run}", "HEAD")
    cache = s.root / STUDIO_DIR / "cache" / "runs" / P / run
    cache.mkdir(parents=True)
    (cache / "planned.json").write_text(json.dumps({"version": 1, "files": []}), encoding="utf-8")
    files = [{"package": f"p{i}", "index": 0, "path": f"f{i}.csv", "source_key": f"in/f{i}.csv", "target_key": f"out/f{i}.csv",
              "size": 3} for i in range(3)]
    (cache / "preview.json").write_text(json.dumps({
        "branch": f"pkf/{P}/{run}", "run": run, "planned": str(cache / "planned.json"), "files": 3, "deletes": 3,
        "by_prefix": {"out/": 3}, "error_files": 0, "max_packages": 10, "packages": 3, "one_file_each": True,
        "source_bucket": "src-bucket", "created_at": _iso(60), "rows": files}), encoding="utf-8")
    at.run()
    ok_render(at)
    assert any("one file each here" in x for x in texts(at.caption))
    tbl = [d.value for d in at.dataframe if "target_key" in d.value.columns]
    assert tbl and list(tbl[0]["target_key"]) == ["out/f0.csv", "out/f1.csv", "out/f2.csv"]
    move = at.button(key="move")
    assert move.label == "Move 3 files" and move.disabled                # own credentials and the deletes not confirmed
    at.checkbox(key="own-creds").check()
    at.run()
    assert at.button(key="move").disabled
    ack = at.checkbox(key=f"ack-deletes-{run}")
    assert ack.label == "I understand 3 source objects in src-bucket will be deleted"
    ack.check()
    at.run()
    move = at.button(key="move")
    assert not move.disabled
    move.click()
    at.run()
    ok_render(at)
    assert started[-1] == ("run_local", {"own_credentials": True, "planned": str(cache / "planned.json")})


# ---------------------------------------------------------------------------------------------- A11 pipeline settings
def test_pipeline_settings_form_writes_and_commits(gh):
    fake, clone, s = gh
    wt = s.open_stage(P, "G2")
    (wt / "pipelines").mkdir(exist_ok=True)
    (wt / "pipelines" / f"{P}.yaml").write_text(yaml.safe_dump({"api_version": "pkf/v1", "kind": "pipeline", "name": P,
                                                                 "owner": "dev1", "start_date": "2026-10-01", "requirement": "x\n",
                                                                 "examples": []}, sort_keys=False), encoding="utf-8")
    s.commit(P, "S2")
    at = app()
    at.session_state["pipe-pick"] = P
    at.session_state["tab"] = "Pipeline"
    at.run()
    ok_render(at)
    at.text_input(key=f"set-owner-{P}").set_value("data-team")
    at.text_input(key=f"set-tags-{P}").set_value("lab, daily")
    at.number_input(key=f"set-retries-{P}").set_value(4)
    next(b for b in at.button if b.label == "Save pipeline settings").click()
    at.run()
    ok_render(at)
    doc = yaml.safe_load((wt / "pipelines" / f"{P}.yaml").read_text(encoding="utf-8"))
    assert doc["owner"] == "data-team" and doc["tags"] == ["lab", "daily"] and doc["retries"] == 4 and doc["requirement"] == "x\n"
    assert _git(wt, "log", "-1", "--format=%s").strip() == f"S2 {P}: pipeline settings"
    assert any(x.startswith("pipeline settings saved") for x in texts(at.success))
