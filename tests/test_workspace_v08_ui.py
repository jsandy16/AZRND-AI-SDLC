"""0.8.0 (ST-3 v3 F): the new pieces of the Workspace and the assistant — the stepper (click a stage to browse it, follow
the run again when it moves on), the substage rail, Components, the console (views, find, line numbers, download), the
sidebar (every pipeline, the assistant card, Acts on its own), the assistant pop-up and full screen, the assistant lane
and the likely cause. Each control calls the same function as before (spies)."""
from __future__ import annotations

import json

import pytest

from pkf_studio import flow, flowstore, jobs
from pkf_studio.ui import assistant_ui, shell, wfd
from pkf_studio.ui import workflow_dev as WD
from pkf_studio.ui import workspace as WS
from test_studio_github import write_g1
from test_studio_ui import P, _busy, app, gh, no_errors  # noqa: F401 - fixtures
from test_workspace_ui import _keys, _md, _on_workspace, _open, _s4, _starter, _state


def _html(at) -> str:
    return "\n".join(str(h.proto.body) for h in at.get("html"))


# ------------------------------------------------------------------------------------------------ stepper and stage
def test_the_stepper_browses_a_stage_and_follows_the_run_again(gh):
    fake, clone, s = gh
    _s4(s)
    _state(s.root, "4.2")
    at = _open(app())
    assert "STAGE 4 OF 7" in _html(at) and "ws-pick-4.1" in _keys(at)
    at.button(key="ws-stage-S2").click().run()
    no_errors(at)
    assert "STAGE 2 OF 7" in _html(at) and "ws-pick-2.1" in _keys(at) and "ws-pick-4.1" not in _keys(at)
    assert "ws-turn" not in str(at.main)                               # Your turn belongs to the run's stage
    st_ = flow.load(s.root, P)                                         # the run moves on to S5: the page follows it again
    st_["cursor"] = "5.1"
    st_["steps"]["5.1"] = {"status": "waiting", "detail": "x"}
    flowstore.save(s.root, P, st_)
    at.run()
    no_errors(at)
    assert "STAGE 5 OF 7" in _html(at)


@pytest.mark.parametrize("stage", ["S1", "S2", "S3", "S4", "S5", "S6", "S7"])
def test_every_stage_renders_its_own_panel(gh, stage):
    fake, clone, s = gh
    _s4(s)
    _state(s.root, "4.2")
    at = _open(app())
    at.button(key=f"ws-stage-{stage}").click().run()
    no_errors(at)
    assert f"STAGE {int(stage[1])} OF 7" in _html(at)
    assert not [x for x in at.error if str(x.value).startswith("This page could not be shown")]


def test_components_regenerate_and_test_build(gh, monkeypatch):
    fake, clone, s = gh
    _s4(s)
    _state(s.root, "4.2")
    started = []
    monkeypatch.setattr(jobs, "start", _starter(started))
    at = _open(app())
    at.button(key="ws-comp-regen-transformation-t_chk").click().run()
    assert started[-1] == ("code_component", {"kind": "transformation", "name": "t_chk"})
    at = _open(app())
    at.button(key="ws-testbuild").click().run()
    assert started[-1] == ("test_build", {})


def test_test_build_at_4_3_goes_through_the_run(gh, monkeypatch):
    fake, clone, s = gh
    _s4(s)
    _state(s.root, "4.3")
    calls = []
    monkeypatch.setattr(flow, "answer", lambda s_, p_, states, step, ans, **kw: calls.append((step, ans)))
    at = _open(app())
    at.button(key="ws-testbuild").click().run()
    assert calls == [("4.3", {"job": "test_build", "title": "Run tests & build again"})]


def test_before_request_approval_lists_passed_and_open_items(gh):
    fake, clone, s = gh
    _s4(s)
    _state(s.root, "4.2")
    at = _open(app())
    md = _md(at)
    assert "Draft build passes" in md and "End-to-end examples pass" in md and "1 of 2 failing" in md
    assert "Before Request approval G4" in _html(at)


# ------------------------------------------------------------------------------------------------ console
def test_console_find_line_numbers_and_views(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    d = _busy(s)
    _state(s.root, "4.1", status="running", row={"job_id": d.name, "started_at": "2026-10-19T10:00:00+00:00"})
    seen = []
    real = wfd.console
    monkeypatch.setattr(wfd, "console", lambda lines, **kw: seen.append(kw) or real(lines, **kw))
    at = _open(app())
    assert seen[-1]["numbers"] is True and seen[-1]["query"] == "" and seen[-1]["key"] == f"wfd-console-{P}"
    assert any(b.key == f"ws-cdl-{P}" for b in at.get("download_button"))          # the whole job log
    at.text_input(key=f"ws-cfind-{P}").input("working").run()
    no_errors(at)
    assert seen[-1]["query"] == "working"
    for view, key in (("Workflow log", f"wfd-flowlog-{P}"), ("Assistant transcript", f"wfd-assistlog-{P}")):
        at.session_state[f"ws-cview-{P}"] = view
        at.run()
        no_errors(at)
        assert seen[-1]["key"] == key


def test_wfd_console_passes_find_and_numbers_only_when_set(monkeypatch):
    got = []
    monkeypatch.setattr(wfd, "_component", lambda: (lambda **kw: got.append(kw)))
    wfd.console([], key="k")
    wfd.console([], key="k", query="err", numbers=True)
    assert "query" not in got[0] and "numbers" not in got[0]
    assert got[1]["query"] == "err" and got[1]["numbers"] is True


# ------------------------------------------------------------------------------------------------ sidebar
def test_the_sidebar_switches_pipeline_and_shows_its_chip(gh):
    fake, clone, s = gh
    write_g1(s, P)
    at = _open(app(), p="➕ New pipeline")
    at.button(key=f"sb-pipe-{P}").click().run()
    no_errors(at)
    assert at.session_state["pipe-pick"] == P and _on_workspace(at)


def test_pipeline_chip_texts(gh):
    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "4.1", status="failed", row={"problems": ["x"], "can_continue": False})
    text, color = shell.pipeline_chip(s, P, lambda s_, p_: ({"by_gate": {}, "current": "G1"}, None))
    assert (text, color) == ("S4 failed", "red")


def test_acts_on_its_own_in_the_sidebar_is_the_pilot_switch(gh):
    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "1.4")
    at = _open(app())
    tog = at.toggle(key=f"wfd-pilot-live-{P}")
    assert tog.label == "Acts on its own" and not tog.value
    tog.set_value(True).run()
    no_errors(at)
    assert flow.load(s.root, P)["assistant_autopilot"] is True
    assert "assistant_pilot" in (flowstore.flow_dir(s.root, P) / "events.jsonl").read_text()     # the same event as 0.6.1


# ------------------------------------------------------------------------------------------------ assistant
def _diagnosis(s):
    d = flowstore.assistant_dir(s.root, P, "20261019T100000-abcdef")
    (d / "diagnosis.json").write_text(json.dumps({"root_cause": "rc", "evidence": ["e1"], "fix_kind": "rerun", "proposal": "retry",
                                                  "files": [], "risk": "low", "confidence": 0.9, "summary": "Try again."}))
    (d / "summary.json").write_text(json.dumps({"phase": "diagnose"}))
    return d


def test_open_shows_the_brief_and_x_closes_it(gh):
    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "1.3", status="failed", row={"problems": ["boom"], "can_continue": False})
    _diagnosis(s)
    at = _open(app())
    assert "Where things stand" not in _html(at)
    at.button(key="sb-open").click().run()
    no_errors(at)
    html = _html(at)
    assert "Where things stand" in html and "Done" in html and "Issue" in html and "Try again." in _md(at)
    assert _on_workspace(at)                                             # the pop-up sits over the Workspace
    assistant_ui._close(P)                                               # what X (on_dismiss) does
    at.session_state[f"as-open-{P}"] = False
    at.run()
    assert "Where things stand" not in _html(at)


def test_the_pop_up_opens_on_the_other_pages_too(gh):
    fake, clone, s = gh
    write_g1(s, P)
    at = app()
    at.session_state[f"as-open-{P}"] = True
    at = _open(at, "Artifacts")
    assert "Where things stand" in _html(at)


def test_the_assistant_tab_has_no_sidebar_and_its_own_controls(gh, monkeypatch):
    from pkf_studio.ui import theme

    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "1.4")
    at = _open(app())
    at.session_state["_goto"] = "Assistant"                              # ⤢ opens /assistant?p=<p> in a new tab (0.8.2)
    at.run()
    no_errors(at)
    assert 'data-testid="stSidebar"' in _html(at) and "display:none" in _html(at)
    assert "Where things stand" in _html(at) and "sb-open" not in _keys(at) and not _on_workspace(at)
    assert at.toggle(key=f"wfd-pilot-live-{P}").label == "Acts on its own"          # in the tab's header
    assert f"as-close-tab-{P}" in _keys(at) and any(b.label == "Ask" for b in at.button)
    assert ".st-key-as-main" in theme.BASE_CSS and ".st-key-as-bottom" in theme.BASE_CSS    # ~75 % / ~25 % of the window
    at.button(key=f"as-close-tab-{P}").click().run()
    no_errors(at)
    assert any("window.close" in str(h.proto.body) for h in at.get("html"))


def test_the_full_screen_links_open_the_assistant_tab(gh):
    fake, clone, s = gh
    write_g1(s, P)
    at = app()
    at.session_state[f"as-open-{P}"] = True
    at = _open(at)
    urls = {b.proto.url for b in at.get("link_button")}
    assert f"./assistant?p={P}" in urls
    assert assistant_ui.tab_url("a b") == "./assistant?p=a%20b"


def test_ask_and_diagnose_go_to_the_assistant_lane_fix_does_not(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    started = []
    monkeypatch.setattr(jobs, "start", _starter(started))
    monkeypatch.setattr(WD, "lanes", lambda: True)
    monkeypatch.setattr(WD, "lane_job", lambda s_, p_: None)
    import streamlit as st

    monkeypatch.setattr(st, "rerun", lambda *a, **k: None)
    for phase in ("ask", "diagnose", "brief"):
        WD.start_assist(s, P, phase, question="q" if phase == "ask" else None)
        assert started[-1][-1] == "assistant", (phase, started[-1])
    WD.start_assist(s, P, "fix", session_id="x")
    assert started[-1] == ("assist", {"phase": "fix", "session_id": "x"})      # no lane: the pipeline's lock
    monkeypatch.setattr(WD, "lanes", lambda: False)
    WD.start_assist(s, P, "ask", question="q")
    assert started[-1] == ("assist", {"phase": "ask", "question": "q"})        # an older jobs module: as 0.7.0


def test_asking_is_open_while_a_pipeline_job_runs_when_the_lane_exists(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    d = _busy(s)
    _state(s.root, "4.1", status="running", row={"job_id": d.name})
    monkeypatch.setattr(WD, "lanes", lambda: True)
    monkeypatch.setattr(WD, "lane_job", lambda s_, p_: None)
    at = app()
    at.session_state[f"as-open-{P}"] = True
    at = _open(at)
    assert not next(b for b in at.button if b.label == "Ask").disabled
    monkeypatch.setattr(WD, "lanes", lambda: False)
    at.run()
    assert next(b for b in at.button if b.label == "Ask").disabled       # no lane: as 0.7.0, wait for the job


def test_the_likely_cause_is_shown_from_its_cache_and_started_once(gh, monkeypatch):
    from pkf_studio.assistant import brief

    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "1.3", status="failed", row={"problems": ["boom"], "can_continue": False})
    started = []
    monkeypatch.setattr(jobs, "start", _starter(started))
    monkeypatch.setattr(WD, "lanes", lambda: True)
    monkeypatch.setattr(WD, "lane_job", lambda s_, p_: None)
    at = app()
    at.session_state[f"as-open-{P}"] = True
    at = _open(at)
    briefs = [x for x in started if x[1].get("phase") == "brief"]
    assert len(briefs) == 1 and briefs[0][-1] == "assistant" and "G1" in (briefs[0][1].get("gates") or {})   # lane, with the gates
    at.run()
    assert len([x for x in started if x[1].get("phase") == "brief"]) == 1          # once per brief (and per 10 minutes)
    b = brief.build(s, P, None, flow.load(s.root, P), n_log=6)
    brief.store_cause(s.root, P, b, {"cause": "the requirement is empty", "next_step": "fill it in", "confidence": 0.7})
    at.run()
    no_errors(at)
    md = _md(at)
    assert "the requirement is empty" in md and "fill it in" in md


def test_no_likely_cause_call_without_an_issue(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    started = []
    monkeypatch.setattr(jobs, "start", _starter(started))
    monkeypatch.setattr(WD, "lanes", lambda: True)
    monkeypatch.setattr(WD, "lane_job", lambda s_, p_: None)
    at = app()
    at.session_state[f"as-open-{P}"] = True
    _open(at)
    assert not [x for x in started if x[1].get("phase") == "brief"]


# ------------------------------------------------------------------------------------------------ after the review
def _lane_busy(s, phase: str = "diagnose"):
    """A running assistant-lane job (its own lock under locks/assistant/)."""
    import os

    from test_studio_ui import _job

    d = _job(s.root, "20261019T110000-aaaaaa", title="🤖 Assistant: diagnose", action="assist", exit=None, started=20)
    cmd = json.loads((d / "cmd.json").read_text())
    cmd.update(args={"phase": phase}, lane="assistant")
    (d / "cmd.json").write_text(json.dumps(cmd))
    (d / "pid").write_text(str(os.getpid()))
    lock = jobs._lock(s.root, P, "assistant")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"job": d.name}))
    assert jobs.current(s.root, P, lane="assistant") == d.name and jobs.current(s.root, P) is None
    return d


def test_a_lane_job_can_be_cancelled_from_the_assistant(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    d = _lane_busy(s)
    cancelled = []
    monkeypatch.setattr(jobs, "cancel", lambda root, jid: cancelled.append(jid))
    at = app()
    at.session_state[f"as-open-{P}"] = True
    at = _open(at)
    assert next(b for b in at.button if b.label == "Ask").disabled          # one lane job at a time
    at.button(key=f"cancel-lane-{d.name}").click().run()
    assert cancelled == [d.name]


def test_a_failed_lane_job_is_shown_and_can_be_dismissed(gh):
    from test_studio_ui import _job

    fake, clone, s = gh
    write_g1(s, P)
    d = _job(s.root, "20261019T110000-bbbbbb", title="🤖 Assistant: answer", action="assist", exit=1,
             result={"ok": False, "problems": ["no API key"]})
    cmd = json.loads((d / "cmd.json").read_text())
    cmd["lane"] = "assistant"
    (d / "cmd.json").write_text(json.dumps(cmd))
    at = app()
    at.session_state[f"as-open-{P}"] = True
    at = _open(at)
    assert "no API key" in _md(at)
    at.button(key=f"as-lastjob-ok-{P}").click().run()
    no_errors(at)
    assert "no API key" not in _md(at)


def test_a_likely_cause_job_does_not_hold_up_the_fix_plan(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    _state(s.root, "1.3", status="failed", row={"problems": ["boom"], "can_continue": False})
    d = flowstore.assistant_dir(s.root, P, "20261019T100000-abcdef")
    (d / "diagnosis.json").write_text(json.dumps({"root_cause": "rc", "evidence": [], "fix_kind": "edit_files", "proposal": "p",
                                                  "files": ["review/x.md"], "risk": "low", "confidence": 0.8, "summary": "s"}))
    (d / "summary.json").write_text(json.dumps({"phase": "diagnose"}))
    _lane_busy(s, phase="brief")
    at = app()
    at.session_state[f"as-open-{P}"] = True
    at = _open(at)
    assert not at.button(key=f"wfd-fix-go-{P}").disabled


def test_full_screen_is_dropped_on_another_page_so_open_works_there(gh):
    fake, clone, s = gh
    write_g1(s, P)
    at = _open(app())
    at.session_state[f"ws-full-{P}"] = True                             # a 0.8.0 session's in-page full screen flag
    at.session_state["_goto"] = "Artifacts"
    at.run()
    no_errors(at)
    assert not at.session_state[f"ws-full-{P}"] and "sb-open" in _keys(at)
    at.button(key="sb-open").click().run()
    no_errors(at)
    assert "Where things stand" in _html(at)


def test_a_failing_component_is_listed_on_the_runs_stage_too(gh):
    fake, clone, s = gh
    _s4(s)
    st_ = _state(s.root, "5.1", status="failed", row={"problems": ["boom"], "can_continue": False})
    assert st_["cursor"] == "5.1"
    at = _open(app())
    assert "STAGE 5 OF 7" in _html(at) and "ws-regen-transformation-t_chk-check_controls" in _keys(at)
