"""0.6.1 "Assistant can act": the pilot diagnoses a failed step and acts on the diagnosis — retry, run from, reopen,
fix (applied only when its tests pass) — one action per tick; people keep reviews, approvals, confirmations and S1."""
from __future__ import annotations

import json

import pytest

from pkf_studio import flow, flowstore, jobs, pilot
from pkf_studio.service import StudioError
from test_flow_gates import P, NavStudio, nav_states


@pytest.fixture()
def fake_jobs(monkeypatch):
    started = []

    def start(root, p, action, args=None, title=""):
        started.append((action, dict(args or {})))
        return f"job{len(started)}"
    monkeypatch.setattr(jobs, "start", start)
    monkeypatch.setattr(jobs, "current", lambda root, p: None)
    monkeypatch.setattr(jobs, "status", lambda root, jid: {"id": jid, "state": "running", "result": {}})
    return started


def failed_at(s, step_id: str, autopilot: bool = True, detail: str = "pkf code: the AI step failed") -> dict:
    st = flow._new_state(P, "dev")
    for x in flow.STEPS:
        if x.id == step_id:
            break
        st["steps"][x.id]["status"] = "done"
    st["steps"][step_id] = {"status": "failed", "detail": detail, "attempts": 1, "ended_at": "2026-10-19T10:00:00+00:00", "job_id": "j0"}
    st.update(status="failed", cursor=step_id, assistant_autopilot=autopilot)
    flow._save(s.root, st)
    return st


def write_session(s, sid: str, diagnosis: dict | None = None, fix: dict | None = None, summary: dict | None = None):
    d = flowstore.assistant_dir(s.root, P, sid)
    if diagnosis is not None:
        (d / "diagnosis.json").write_text(json.dumps(diagnosis))
    if fix is not None:
        (d / "fix.json").write_text(json.dumps(fix))
    (d / "summary.json").write_text(json.dumps(summary or {}))


DIAG = {"root_cause": "the copy step uses the wrong folder.", "evidence": ["x"], "fix_kind": "edit_files", "proposal": "p", "files": ["destinations/d.py"],
        "risk": "low", "confidence": 0.9, "summary": "The step that copies the files looks in the wrong folder."}


def test_policy_keeps_people_steps_with_people(tmp_path):
    s = NavStudio(tmp_path)
    st = failed_at(s, "4.2")
    assert "person's decision" in flow.assistant_may(s, P, nav_states(G5="in_progress"), st, {"do": "retry"})
    st = failed_at(s, "1.3")
    assert "S1" in flow.assistant_may(s, P, nav_states(G5="in_progress"), st, {"do": "retry"})
    assert "G1" in flow.assistant_may(s, P, nav_states(), st, {"do": "reopen", "gate": "G1"})
    assert "S1" in flow.assistant_may(s, P, nav_states(), st, {"do": "run_from", "step": "1.4"})
    st = failed_at(s, "5.1")
    assert flow.assistant_may(s, P, nav_states(G5="in_progress"), st, {"do": "retry"}) is None
    assert flow.assistant_may(s, P, nav_states(G5="in_progress"), st, {"do": "render_plan_again"}) is None
    assert "S4" in flow.assistant_may(s, P, nav_states(G5="in_progress"), st, {"do": "regenerate", "kind": "source", "name": "x"})
    assert flow.assistant_may(s, P, nav_states(G5="in_progress"), st, {"do": "reopen", "gate": "G3"}) is None
    assert "unknown" in flow.assistant_may(s, P, nav_states(), st, {"do": "approve"})
    with pytest.raises(StudioError, match="may not approve"):
        flow.assistant_act(s, P, nav_states(), {"do": "approve"})
    assert any(e["type"] == "assistant_action" and e.get("ok") is False for e in flowstore.events(s.root, P))


def test_pilot_is_off_unless_switched_on_and_never_in_s1(tmp_path, fake_jobs):
    s = NavStudio(tmp_path)
    failed_at(s, "4.1", autopilot=False)
    assert pilot.tick(s, P, nav_states(G5="in_progress")) is None and not fake_jobs
    failed_at(s, "1.3")
    assert not pilot.pending(flow.load(s.root, P))
    pilot.tick(s, P, nav_states(G5="in_progress"))
    assert not fake_jobs


def test_pilot_diagnoses_then_goes_back(tmp_path, fake_jobs, monkeypatch):
    s = NavStudio(tmp_path)
    monkeypatch.setattr(flow, "stage_started", lambda *a: True)
    failed_at(s, "5.4", detail="not ready: the dry run failed")
    st = flow.load(s.root, P)
    assert pilot.pending(st)
    pl = pilot.tick(s, P, nav_states(G5="in_progress"))
    assert pl["phase"] == "diagnosing" and fake_jobs[0][0] == "assist" and fake_jobs[0][1]["phase"] == "diagnose"
    sid = fake_jobs[0][1]["session_id"]
    assert pl["session"] == sid and pl["cycles"] == {"5.4": 1}
    write_session(s, sid, {**DIAG, "fix_kind": "studio_action", "actions": [{"do": "run_from", "step": "5.1", "why": "plan again"}]})
    pl = pilot.tick(s, P, nav_states(G5="in_progress"))
    assert pl["phase"] == "acting" and pl["queue"][0]["do"] == "run_from" and "looks in the wrong folder" in pl["note"]
    pl = pilot.tick(s, P, nav_states(G5="in_progress"))
    st = flow.load(s.root, P)
    assert pl["phase"] == "done" and st["cursor"] == "5.1" and ("prepare_env", {}) in fake_jobs
    ev = [e for e in flowstore.events(s.root, P) if e["type"] == "run_from"]
    assert ev and ev[-1]["by"] == "assistant"


def test_pilot_fix_is_applied_only_when_tests_pass(tmp_path, fake_jobs, monkeypatch):
    s = NavStudio(tmp_path)
    applied = []
    from pkf_studio.assistant import protocol
    monkeypatch.setattr(protocol, "apply_fix", lambda studio, p, sid: applied.append(sid) or "abc1234")
    failed_at(s, "4.1")
    pl = pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
    sid = pl["session"]
    write_session(s, sid, DIAG)                                   # no actions listed: default for edit_files is [fix]
    pl = pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
    assert pl["phase"] == "acting" and [a["do"] for a in pl["queue"]] == ["fix"]
    pl = pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
    assert pl["phase"] == "fixing" and fake_jobs[-1][1] == {"phase": "fix", "session_id": sid}
    write_session(s, sid, DIAG, fix={"summary": "s", "plain_summary": "Point the copy at the right folder.", "files_changed": ["destinations/d.py"],
                                     "tests": {"ran": True, "ok": True, "detail": "4 passed"}, "notes": "", "verified": {"ok": True}})
    pl = pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
    assert applied == [sid] and pl["phase"] == "acting" and pl["queue"] == [{"do": "retry", "why": "the fix is applied"}]
    assert "Point the copy" in pl["note"]
    pl = pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
    assert pl["phase"] == "done" and flow.load(s.root, P)["status"] != "failed"


@pytest.mark.parametrize("fix,why", [
    ({"tests": {"ran": True, "ok": False, "detail": "1 failed"}, "files_changed": ["destinations/d.py"]}, "does not pass its tests"),
    ({"tests": {"ran": False, "ok": False, "detail": "no credentials"}, "files_changed": ["destinations/d.py"]}, "does not pass its tests"),
    ({"tests": {"ran": True, "ok": True, "detail": ""}, "files_changed": ["pipelines/pipe_a.yaml"], "verified": {"ok": True}}, "changes the requirement"),
    ({"tests": {"ran": True, "ok": True, "detail": "the agent says ok"}, "files_changed": ["destinations/d.py"],
      "verified": {"ok": False, "stopped_at": "draft build"}}, "stopped at draft build"),
])
def test_pilot_leaves_risky_fixes_to_a_person(tmp_path, fake_jobs, monkeypatch, fix, why):
    s = NavStudio(tmp_path)
    from pkf_studio.assistant import protocol
    monkeypatch.setattr(protocol, "apply_fix", lambda *a: pytest.fail("must not apply"))
    failed_at(s, "4.1")
    sid = pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))["session"]
    write_session(s, sid, DIAG)
    pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
    pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
    write_session(s, sid, DIAG, fix={"summary": "s", "notes": "", **fix})
    pl = pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
    assert pl["phase"] == "needs_you" and why in pl["note"]


def test_pilot_stops_after_three_tries_and_never_rediagnoses_the_same_failure(tmp_path, fake_jobs):
    s = NavStudio(tmp_path)
    for i in range(3):
        st = failed_at(s, "4.1", detail=f"failure {i}")
        st["pilot"] = {**(flow.load(s.root, P).get("pilot") or {}), "cycles": {"4.1": i}, "phase": "needs_you"}
        flow._save(s.root, st)
        pl = pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
        assert pl["phase"] == "diagnosing"
        write_session(s, pl["session"], summary={"error": "budget"})
        pl = pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
        assert pl["phase"] == "needs_you"
        n = len(fake_jobs)
        assert pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))["phase"] == "needs_you" and len(fake_jobs) == n
    st = failed_at(s, "4.1", detail="failure 4")
    st["pilot"] = {"cycles": {"4.1": 3}, "phase": "needs_you"}
    flow._save(s.root, st)
    pl = pilot.tick(s, P, nav_states(G4="in_progress", G5="blocked"))
    assert pl["phase"] == "gave_up" and "your turn" in pl["note"]


def test_default_actions_from_older_diagnoses():
    st = nav_states(G5="in_progress")
    assert flow.default_actions({"fix_kind": "edit_files", "needs_reopen": "G3"}, st) == [
        {"do": "reopen", "gate": "G3", "why": "the fix changes what G3 approved"}, {"do": "fix", "why": "carry out the plan"}]
    assert flow.default_actions({"fix_kind": "studio_action", "studio_action": "Restore dags/ from main"})[0]["do"] == "restore_dags"
    assert flow.default_actions({"fix_kind": "studio_action", "studio_action": "Reopen G4"}) == [{"do": "reopen", "gate": "G4", "why": "Reopen G4"}]
    assert flow.default_actions({"fix_kind": "user_action"}) == []


def test_tests_and_build_again_from_a_failed_start_reruns_it(tmp_path, fake_jobs, monkeypatch):
    """At a failed 4.1, "run tests & build again" re-runs 4.1's tests and draft build (not a jump to 4.3, which may skip
    to the gate when the last tests.json looked fine)."""
    s = NavStudio(tmp_path)
    monkeypatch.setattr(flow, "stage_started", lambda *a: True)
    failed_at(s, "4.1")
    msg = flow.assistant_act(s, P, nav_states(G4="in_progress", G5="blocked"), {"do": "tests_build_again", "why": "w"})
    st = flow.load(s.root, P)
    assert st["cursor"] == "4.1" and ("test_build", {}) in fake_jobs and "tests" in msg.lower()


def test_reopen_then_fix_holds_the_run_until_the_fix_is_applied(tmp_path, fake_jobs, monkeypatch):
    """reopen → fix: the reopened stage must not run its jobs before the fix is in (they would plan/render the old
    files). The run is held after the reopen and resumes once the fix is applied."""
    s = NavStudio(tmp_path)
    monkeypatch.setattr(flow, "stage_started", lambda *a: True)
    from pkf_studio.assistant import protocol
    monkeypatch.setattr(protocol, "apply_fix", lambda studio, p, sid: "abc1234")
    failed_at(s, "5.1")                                  # the run fails in S5; the cause is in the code G4 approved
    states_now = nav_states(G5="in_progress")
    after = nav_states(G4="in_progress", G5="blocked")
    sid = pilot.tick(s, P, states_now, derive=lambda: after)["session"]
    write_session(s, sid, {**DIAG, "needs_reopen": "G4"})
    pl = pilot.tick(s, P, states_now, derive=lambda: after)
    assert [a["do"] for a in pl["queue"]] == ["reopen", "fix"]
    pl = pilot.tick(s, P, states_now, derive=lambda: after)      # reopen G4: held
    st = flow.load(s.root, P)
    assert s.reopened == ["G4"] and st["status"] == "stopped" and st["cursor"] == "4.1"
    assert not [j for j in fake_jobs if j[0] == "test_build"], fake_jobs
    pl = pilot.tick(s, P, after, derive=lambda: after)           # the fix
    assert pl["phase"] == "fixing"
    write_session(s, sid, {**DIAG, "needs_reopen": "G4"}, fix={"summary": "s", "plain_summary": "p", "files_changed": ["destinations/d.py"],
                                                               "tests": {"ran": True, "ok": True, "detail": "ok"}, "notes": "", "verified": {"ok": True}})
    pl = pilot.tick(s, P, after, derive=lambda: after)           # applied → resume queued
    pl = pilot.tick(s, P, after, derive=lambda: after)           # resumed: S4 tests and builds again with the fix in
    st = flow.load(s.root, P)
    assert st["status"] == "running" and ("test_build", {}) in fake_jobs, (st["status"], fake_jobs)
    assert pl["phase"] == "done"


def test_assistant_never_jumps_over_a_persons_step(tmp_path, fake_jobs):
    """Review finding: run_from past a review that is not done would skip it (and with auto-approve, request and merge
    the gate without the review). The assistant may not; a person may, after a warning."""
    s = NavStudio(tmp_path)
    failed_at(s, "5.1")
    why = flow.assistant_may(s, P, nav_states(G5="in_progress"), flow.load(s.root, P), {"do": "run_from", "step": "5.4"})
    assert why and "5.3" in why
    assert flow.people_skipped(flow.load(s.root, P), "5.4") == ["5.3"]
    assert flow.assistant_may(s, P, nav_states(G5="in_progress"), flow.load(s.root, P), {"do": "run_from", "step": "5.1"}) is None


def test_assistant_never_retries_a_real_run(tmp_path, fake_jobs):
    s = NavStudio(tmp_path)
    st = failed_at(s, "6.3")
    assert "person" in flow.assistant_may(s, P, nav_states(), st, {"do": "retry"})
    assert not pilot.eligible(st)


def test_reopen_needs_something_to_reopen_and_somewhere_to_go(tmp_path, fake_jobs):
    s = NavStudio(tmp_path)
    st = failed_at(s, "3.3")
    assert "nothing to reopen" in flow.assistant_may(s, P, nav_states(G3="in_progress", G4="blocked", G5="blocked"), st,
                                                     {"do": "reopen", "gate": "G5"})
    with pytest.raises(StudioError, match="nowhere to go"):          # checked before anything is set aside
        flow.reopen_stage(s, P, nav_states(G2="stale", G3="blocked", G4="blocked", G5="blocked"), "G4")
    assert s.reopened == []


def test_run_from_forgets_ticked_checklists(tmp_path, fake_jobs):
    """Review finding: Run from 7.3 on a finished run went straight to "live in Airflow" (the old ticks were kept)."""
    s = NavStudio(tmp_path)
    st = flow._new_state(P, "dev")
    for x in flow.STEPS:
        st["steps"][x.id] = {"status": "done", "items_done": list(x.items)} if x.kind == "checklist" else {"status": "done"}
    st.update(status="done", cursor=None)
    flow._save(s.root, st)
    st = flow.run_from(s, P, nav_states(), "7.3")
    assert st["cursor"] == "7.3" and st["status"] == "waiting_user" and st["steps"]["7.3"]["status"] == "waiting", st["steps"]["7.3"]
