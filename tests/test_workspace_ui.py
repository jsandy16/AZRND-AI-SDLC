"""0.8.0 (ST-3 v3 F): the four pages, the routing (and the 0.6.1 keys it still honours), the sidebar and the top bar, the
Workspace's stepper, stage header (Request approval: P1), substage rail, Before Request approval, Components, Files of
this stage, the console and its views, the assistant pop-up and full screen, the Pipelines page and the Artifacts page —
each control calling the same function as in 0.6.1 (spies), no duplicate keys. (Moved from the 0.7.0 layout: the same
assertions against the new places.)"""
from __future__ import annotations

import json

import pytest
import yaml

from pkf_studio import flow, flowstore, jobs
from pkf_studio.ui import artifacts as A
from pkf_studio.ui import workspace as WS
from test_studio_github import write_g1
from test_studio_ui import P, _busy, _job, app, gh, no_errors, texts  # noqa: F401 - fixtures


def _md(at) -> str:
    return "\n".join(str(e.value) for kind in ("markdown", "caption", "info", "warning", "error", "success")
                     for e in getattr(at, kind))


def _labels(at) -> list[str]:
    return [b.label for b in at.button]


def _keys(at) -> set[str]:
    return {b.key for b in at.button if b.key}


def _on_workspace(at) -> bool:
    """The Workspace is showing: its stepper (7 stage buttons) is on the page."""
    return {f"ws-stage-S{i}" for i in range(1, 8)} <= _keys(at)


def _starter(started: list):
    """A jobs.start spy (0.8.0: the assistant lane passes lane=)."""
    return lambda root, p, action, args, title, **kw: started.append((action, args) + ((kw["lane"],) if kw.get("lane") else ())) \
        or "20261019T120000-aaaaaa"


def _state(root, cursor: str, status: str = "waiting_user", row: dict | None = None, done_before: bool = True) -> dict:
    st = {"version": 1, "pipeline": P, "mode": "dev", "status": status, "cursor": cursor, "started_at": "2026-10-19T10:00:00+00:00",
          "updated_at": "2026-10-19T10:05:00+00:00", "answers": {}, "auto_approve": False,
          "steps": {s.id: {"status": "not_started"} for s in flow.STEPS}, "gate": {}, "sub": {}, "message": f"{cursor} message text"}
    if done_before:
        for s in flow.STEPS:
            if s.id == cursor:
                break
            st["steps"][s.id] = {"status": "done", "detail": "ok"}
    st["steps"][cursor] = {"status": "waiting" if status.startswith("waiting") else "failed" if status == "failed" else "running",
                           "detail": "detail text", **(row or {})}
    flowstore.save(root, P, st)
    flowstore.event(root, P, "run_started")
    return st


def _s4(s) -> None:
    """The pipeline on its G4 branch with a failing transformation (tests.json v2)."""
    write_g1(s, P)
    s.commit(P, "S1")
    wt = s.open_stage(P, "G4")
    files = {f"pipelines/{P}.yaml": {"api_version": "pkf/v1", "kind": "pipeline", "name": P, "owner": "dev1", "start_date": "2026-10-01",
                                     "connection": P, "transformation": "t_chk", "requirement": "TRIGGER\n- daily\n", "examples": []},
             f"connections/{P}.yaml": {"api_version": "pkf/v1", "kind": "connection", "name": P, "source": "s_in", "destination": "d_out"},
             "sources/s_in.yaml": {"type": "s3", "connection": {"bucket": "b"}, "requirement": "list"},
             "destinations/d_out.yaml": {"type": "s3", "connection": {"bucket": "c"}, "requirement": "move"},
             "transformations/t_chk.yaml": {"requirement": "check"}}
    for rel, doc in files.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(yaml.safe_dump(doc, sort_keys=False))
    (wt / "transformations/t_chk.py").write_text("def check_controls(rows, ctx):\n    return rows\n")
    (wt / f"review/{P}").mkdir(parents=True, exist_ok=True)
    (wt / f"review/{P}/tests.json").write_text(json.dumps({"version": 2, "build_ok": True, "examples_ok": False,
        "examples": [{"example": "e1", "status": "pass"}, {"example": "e2", "status": "fail"}],
        "components": [{"kind": "source", "name": "s_in", "stage": "approved", "custom_steps": []},
                       {"kind": "transformation", "name": "t_chk", "stage": "code_failing", "custom_steps": ["check_controls"],
                        "failing": [{"step": "check_controls", "case": "missing_control_row", "detail": "expected 1 error, got 0"}]}]}))
    s.commit(P, "S4 code")


def _open(at, page: str | None = None, p: str = P):
    at.session_state["pipe-pick"] = p
    if page:
        at.session_state["_goto"] = page
    at.run()
    no_errors(at)
    return at


# ------------------------------------------------------------------------------------------------ pages and routing
def test_workspace_is_the_default_page_with_its_bands(gh):
    fake, clone, s = gh
    write_g1(s, P)
    at = _open(app())
    keys = _keys(at)
    assert "refresh" in keys and _on_workspace(at)                      # top bar, stepper
    assert {"sb-open", f"sb-pipe-{P}"} <= keys                          # the sidebar: assistant card, every pipeline
    assert any(b.proto.url == f"./assistant?p={P}" for b in at.get("link_button"))   # ⤢: the assistant in a new tab
    assert "Before the run starts" in _md(at)                          # the idle Workspace: the intake form (Your turn)
    views = at.get("button_group")                                      # the console's views (segmented control)
    assert views and all(v in views[0].options for v in WS.CONSOLE_VIEWS)
    assert set(WS.CONSOLE_VIEWS) >= {"Console", "Workflow log", "Approval history", "Jobs"}


def test_pages_open_through_goto_and_the_old_keys(gh):
    fake, clone, s = gh
    write_g1(s, P)
    at = _open(app(), "Pipelines")
    assert at.title[0].value == P and any(t.label == "Requirements" for t in at.tabs)
    at.session_state["_goto"] = "Artifacts"
    at.run()
    no_errors(at)
    assert any(b.label.startswith("⬇ Download artifacts") for b in at.get("download_button")), "artifacts page"
    at.session_state["tab"] = "Workflow Developer"                    # 0.6.1's tab name of the Workspace
    at.run()
    no_errors(at)
    assert _on_workspace(at)
    at.session_state["tab"] = "Workflow"                              # a Pipelines-page tab
    at.run()
    no_errors(at)
    assert at.title and at.title[0].value == P
    at.session_state["_goto"] = "Workspace"                           # the page wrote `tab` itself: no bounce back
    at.run()
    at.run()
    no_errors(at)
    assert _on_workspace(at) and not at.title


def test_setup_stays_reachable_when_a_startup_check_fails(gh, monkeypatch):
    from types import SimpleNamespace

    from pkf_studio import checks as C

    fake, clone, s = gh
    monkeypatch.setattr(C, "blocking", lambda results: [SimpleNamespace(name="GitHub token", detail="missing", fix="set it")])
    at = _open(app())
    assert any("Fix the startup checks first" in x for x in texts(at.error))
    at.session_state["page"] = "Setup"
    at.run()
    no_errors(at)
    assert any(t.label == "Who am I" for t in at.tabs) or "Studio project and team" in _md(at)


# ------------------------------------------------------------------------------------------------ the work area
def test_attention_lists_a_failing_component_and_regenerates_it(gh, monkeypatch):
    fake, clone, s = gh
    _s4(s)
    _state(s.root, "4.2")
    started = []
    monkeypatch.setattr(jobs, "start", _starter(started))
    at = _open(app())
    md = _md(at)
    assert "fails its tests" in md and "missing_control_row" in md     # Before Request approval G4
    btn = at.button(key=f"ws-regen-transformation-t_chk-check_controls")
    assert btn.label == "Regenerate check_controls" and not btn.disabled
    btn.click().run()
    assert started == [("code_component", {"kind": "transformation", "name": "t_chk", "step": "check_controls"})]


def test_regenerate_at_4_3_goes_through_the_run(gh, monkeypatch):
    fake, clone, s = gh
    _s4(s)
    _state(s.root, "4.3")
    calls = []
    monkeypatch.setattr(flow, "answer", lambda s_, p_, states, step, ans, **kw: calls.append((step, ans)))
    at = _open(app())
    at.button(key="ws-regen-transformation-t_chk-check_controls").click().run()
    assert calls and calls[0][0] == "4.3" and calls[0][1]["job"] == "code_component"
    assert calls[0][1]["args"] == {"kind": "transformation", "name": "t_chk", "step": "check_controls"}


def test_now_card_cancels_the_running_job_and_hides_cancel_for_run_local(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    d = _busy(s, action="test_build", title="Run tests & build again")
    _state(s.root, "4.1", status="running", row={"job_id": d.name, "started_at": "2026-10-19T10:00:00+00:00"})
    cancelled = []
    monkeypatch.setattr(jobs, "cancel", lambda root, jid: cancelled.append(jid))
    at = _open(app())
    assert "Run tests & build again is still running" in _md(at)       # Before Request approval: No job running (open)
    at.button(key=f"cancel-{d.name}").click().run()                     # the running substage card's Cancel
    assert cancelled == [d.name]


def test_no_cancel_for_a_local_run_on_the_workspace(gh):
    fake, clone, s = gh
    write_g1(s, P)
    d = _busy(s, action="run_local", title="Run locally")
    _state(s.root, "6.3", status="running", row={"job_id": d.name})
    at = _open(app())
    assert f"cancel-{d.name}" not in _keys(at)
    assert any("Terraform" in x for x in texts(at.caption))


def test_request_approval_is_hidden_on_the_gate_step_and_offered_before_it(gh):
    fake, clone, s = gh
    _s4(s)
    _state(s.root, "4.4", status="waiting_gate")
    at = _open(app())
    assert f"ws-req-G4-{P}" not in _keys(at)                          # P1: the run opened the PR on its gate step
    _state(s.root, "4.2")
    at = _open(app())
    btn = at.button(key=f"ws-req-G4-{P}")
    assert btn.label in ("Request approval G4", "Update the PR")
    assert "Before Request approval G4" in "".join(str(h.proto.body) for h in at.get("html"))


def test_artifacts_of_the_stage_link_to_the_artifacts_page(gh):
    fake, clone, s = gh
    _s4(s)
    _state(s.root, "4.2")
    at = _open(app())
    links = at.get("page_link")                                         # Files of this stage: Open → the Artifacts page
    assert any("transformations%2Ft_chk.py" in x.proto.query_string or "transformations/t_chk.py" in x.proto.query_string
               for x in links), [x.proto.query_string for x in links]
    rows = dict(WS.stage_files(__import__("pkf_studio.ui.common", fromlist=["Files"]).Files(s, P), P, "S4"))
    assert rows["transformations/t_chk.py"].startswith("tests fail")
    assert rows[f"review/{P}/tests.json"].startswith("draft build ok")


# ------------------------------------------------------------------------------------------------ assistant and console
def test_assistant_band_ask_and_fix_plan(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "4.1", status="failed", row={"problems": ["boom"], "can_continue": False})
    d = flowstore.assistant_dir(s.root, P, "20261019T100000-abcdef")
    (d / "diagnosis.json").write_text(json.dumps({"root_cause": "an empty control id", "evidence": ["x.py:41"], "fix_kind": "edit_files",
                                                  "proposal": "treat a missing column as an error", "files": ["transformations/t.py"],
                                                  "risk": "low", "confidence": 0.86, "summary": "A missing column slips through."}))
    (d / "summary.json").write_text(json.dumps({"phase": "diagnose", "question": "Why is it failing?"}))
    started = []
    monkeypatch.setattr(jobs, "start", _starter(started))
    at = app()
    at.session_state[f"as-open-{P}"] = True                            # sidebar → Open: the pop-up
    at = _open(at)
    md = _md(at)
    assert "Why is it failing?" in md and "A missing column slips through." in md and "Root cause" in md
    assert at.button(key=f"wfd-fix-go-{P}").label == "✓ Approve the fix plan"
    at.button(key=f"wfd-fix-go-{P}").click().run()
    assert started and started[-1][0] == "assist" and started[-1][1]["phase"] == "fix"
    at = app()
    at.session_state[f"as-open-{P}"] = True
    at = _open(at)
    at.text_input(key=f"wfd-ask-{P}").input("what did 3.3 produce?")
    next(b for b in at.button if b.label == "Ask").click().run()
    args = dict(started[-1][1])
    gates = args.pop("gates", None)                                     # 0.8.0: the lane gets the page's gates
    assert args == {"phase": "ask", "question": "what did 3.3 produce?", "session_id": "20261019T100000-abcdef"}
    assert gates is None or gates.get("G1", {}).get("state")


def test_console_tabs_approval_history_and_jobs(gh):
    fake, clone, s = gh
    write_g1(s, P)
    _job(s.root, "20261019T090000-cccccc", title="Analyze with AI", action="analyze", result={"ok": True})
    _state(s.root, "1.4")
    at = _open(app())
    at.session_state[f"ws-cview-{P}"] = "Approval history"
    at.run()
    no_errors(at)
    assert "Approval history" in _md(at)
    at.session_state[f"ws-cview-{P}"] = "Jobs"
    at.run()
    no_errors(at)
    assert "Analyze with AI" in _md(at)


# ------------------------------------------------------------------------------------------------ context bar
def test_source_control_chip_and_no_duplicate_keys_with_the_workflow_tab(gh):
    fake, clone, s = gh
    wt = write_g1(s, P)
    (wt / "notes.txt").write_text("uncommitted\n")
    at = _open(app())
    assert "sc-commit-local" in _keys(at)                               # the dirty bar, in the source-control chip
    assert any("uncommitted change" in x for x in _md(at).splitlines())
    at.session_state["_goto"] = "Pipelines"
    at.session_state["tab"] = "Workflow"
    at.run()
    no_errors(at)                                                       # the Workflow tab's own dirty bar: no key clash
    assert {"commit-local", "sc-commit-local"} <= _keys(at)


def test_stop_and_resume_are_in_the_context_bar(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "1.4", status="waiting_user")
    at = _open(app())
    at.button(key=f"wfd-stop-{P}").click().run()
    assert flow.load(s.root, P)["status"] == "stopped"
    at.run()
    assert any(b.label == "▶ Resume" for b in at.button)


# ------------------------------------------------------------------------------------------------ Artifacts page
def test_artifacts_page_groups_files_and_diffs_against_the_base(gh):
    fake, clone, s = gh
    _s4(s)
    assert A.group_of(f"requirements/{P}/extracted.txt", P) == "01 Requirements"
    assert A.group_of("transformations/t_chk.py", P) == "04 Code"
    assert A.group_of(f"dags/pkf_{P}.py", P) == "05 Infra · 06 Deploy"
    assert A.group_of("sources/s_in.yaml", P) == "02 Design · 03 Plans"
    from pkf_studio.ui.common import Files

    f = Files(s, P)
    (f.wt / "transformations/t_chk.py").write_text("def check_controls(rows, ctx):\n    return []\n")   # uncommitted
    d = A.diff_vs_base(s, f, "transformations/t_chk.py")
    assert "/dev/null" in d and "+    return []" in d                  # new on the branch, uncommitted edit included
    at = _open(app(), "Artifacts")
    at.query_params["file"] = "transformations/t_chk.py"            # what "Open →" (st.page_link query_params) sends
    at.run()
    no_errors(at)
    md = _md(at)
    assert "transformations/t_chk.py" in md and any(t.label == "Diff vs main" for t in at.tabs)
    assert at.button(key=f"art-regen-{P}").label == "Regenerate (AI)"


def test_watch_job_reruns_the_app_when_the_job_ends(monkeypatch):
    import streamlit as st

    calls = []
    monkeypatch.setattr(st, "rerun", lambda scope="app": calls.append(scope))
    st.session_state.clear()
    WS.watch_job("p", "job-1")
    WS.watch_job("p", "job-1")
    assert calls == []
    WS.watch_job("p", None)                                           # the job ended
    assert calls == ["app"] and st.session_state["_invalidate"] is True and st.session_state["_poll"] == 1


# ------------------------------------------------------------------------------------------------ after the review
def test_the_context_bar_follows_a_status_change_made_by_the_tick(gh, monkeypatch):
    """The bar is drawn before the run ticks: when the tick changes the status, the page reruns once and the bar follows
    (no Stop run next to a failed run)."""
    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "2.1", status="running")
    real = flow.tick

    def failing_tick(s_, p_, states):
        st_ = flow.load(s_.root, p_)
        st_["status"] = "failed"
        st_["steps"]["2.1"] = {"status": "failed", "detail": "boom"}
        flowstore.save(s_.root, p_, st_)
        return st_

    monkeypatch.setattr(flow, "tick", failing_tick)
    at = _open(app())
    keys = _keys(at)
    assert f"wfd-stop-{P}" not in keys, keys
    monkeypatch.setattr(flow, "tick", real)


def test_regenerate_on_a_stopped_run_does_not_resume_it(gh, monkeypatch):
    fake, clone, s = gh
    _s4(s)
    st_ = _state(s.root, "4.3")
    st_["status"] = "stopped"
    flowstore.save(s.root, P, st_)
    started, answered = [], []
    monkeypatch.setattr(jobs, "start", lambda root, p, action, args, title, **kw: started.append(action) or "20261019T120000-dddddd")
    monkeypatch.setattr(flow, "answer", lambda *a, **k: answered.append(a))
    at = _open(app())
    at.button(key="ws-regen-transformation-t_chk-check_controls").click().run()
    assert started == ["code_component"] and not answered
    assert flow.load(s.root, P)["status"] == "stopped"


def test_a_substage_picked_in_the_rail_prefills_go_back(gh):
    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "1.4")
    at = _open(app())
    at.button(key="ws-pick-1.2").click().run()
    no_errors(at)
    assert at.selectbox(key=f"wfd-nav-step-{P}").value.startswith("1.2 ")
    assert at.session_state[f"wfd-sel-{P}"] == "1.2"


def test_run_menu_forgets_a_failed_run(gh):
    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "1.3", status="failed", row={"problems": ["x"], "can_continue": False})
    at = _open(app())
    at.button(key=f"wfd-reset-{P}").click().run()
    no_errors(at)
    st_ = flow.load(s.root, P)
    assert st_ is None or st_.get("status") in (None, "idle")


def test_running_elsewhere_opens_that_pipeline_in_the_workspace(gh):
    fake, clone, s = gh
    write_g1(s, P)
    _busy(s, action="test_build", title="Run tests & build again")
    at = _open(app(), "Pipelines", p="➕ New pipeline")
    at.button(key=f"goto-job-{P}").click().run()
    no_errors(at)
    assert at.session_state["pipe-pick"] == P and _on_workspace(at) and not at.title


def test_full_screen_shows_the_full_diagnosis(gh):
    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "1.3", status="failed", row={"problems": ["x"], "can_continue": False})
    d = flowstore.assistant_dir(s.root, P, "20261019T100000-eeeeee")
    (d / "diagnosis.json").write_text(json.dumps({"root_cause": "rc", "evidence": ["e1"], "fix_kind": "rerun", "proposal": "retry",
                                                  "files": [], "risk": "low", "confidence": 0.9, "summary": "Try again."}))
    (d / "summary.json").write_text(json.dumps({"phase": "diagnose"}))
    at = app()
    at.session_state[f"as-open-{P}"] = True
    at = _open(at)
    assert "Diagnosis (details)" not in _md(at) and "Try again." in _md(at)    # the pop-up: the gist
    at.session_state["_goto"] = "Assistant"                              # ⤢ opens this page in a new tab (0.8.2)
    at.run()
    no_errors(at)
    assert "Diagnosis (details)" in _md(at) and not _on_workspace(at)


def test_the_pipelines_tab_is_remembered_across_pages(gh):
    fake, clone, s = gh
    write_g1(s, P)
    at = _open(app(), "Pipelines")
    at.session_state["tab"] = "Pipeline"
    at.run()
    at.session_state["_goto"] = "Workspace"
    at.run()
    at.session_state["_goto"] = "Pipelines"
    at.run()
    no_errors(at)
    assert at.session_state["tab"] == "Pipeline"


@pytest.mark.parametrize("cursor,status", [("4.4", "waiting_gate"), ("5.4", "failed"), ("6.4", "waiting_gate"), ("7.1", "running"),
                                           ("4.3", "stopped"), ("2.2", "done"), ("1.1", "running"), ("6.5", "waiting_user")])
def test_every_page_renders_in_every_state_without_key_clashes(gh, cursor, status):
    fake, clone, s = gh
    _s4(s)
    _state(s.root, cursor, status=status)
    at = _open(app())
    for page, tab in (("Pipelines", "Workflow"), ("Artifacts", None), ("Workspace", None)):
        at.session_state["_goto"] = page
        if tab:
            at.session_state["tab"] = tab
        at.run()
        no_errors(at)
        assert not [x for x in texts(at.error) if x.startswith("This page could not be shown")], (page, texts(at.error))


def test_a_new_pipeline_name_on_every_page(gh):
    fake, clone, s = gh
    at = app()
    at.session_state["pipe-pick"] = "➕ New pipeline"
    at.session_state["new-name"] = "brand_new"
    at.run()
    no_errors(at)
    assert "Before the run starts" in _md(at)
    for page in ("Pipelines", "Artifacts"):
        at.session_state["_goto"] = page
        at.run()
        no_errors(at)


def test_the_workspace_renders_offline(gh):
    fake, clone, s = gh
    write_g1(s, P)
    s.commit(P, "S1")
    _state(s.root, "1.4")
    at = _open(app())
    fake.close()
    at.button(key="refresh").click().run()
    no_errors(at)
    assert any(x.startswith("GitHub unreachable:") for x in texts(at.warning))
    assert _on_workspace(at)
