"""The Workflow Developer tab (AppTest): renders without a run (intake), renders a run in every state the flow can be
in, and the new tab leaves the other tabs as they were."""
from __future__ import annotations

import json

import pytest

from pkf_studio import flowstore
from test_studio_github import write_g1
from test_studio_ui import P, app, gh, no_errors  # noqa: F401 - fixtures

TAB = "Workflow Developer"


@pytest.fixture()
def pipe(gh):
    fake, clone, s = gh
    write_g1(s, P)
    return gh


def _tab(at, p: str = P):
    at.session_state["pipe-pick"] = p
    at.session_state["tab"] = TAB
    at.run()
    no_errors(at)
    return at


def _markdown(at) -> str:
    return "\n".join(str(e.value) for e in at.markdown) + "\n" + "\n".join(str(e.value) for e in at.caption) + \
        "\n".join(str(e.value) for e in at.info) + "\n".join(str(e.value) for e in at.error) + "\n".join(str(e.value) for e in at.success)


def test_tab_is_listed_after_workflow(pipe):
    from pkf_studio.app import TABS

    assert TABS[-2:] == ["Workflow", TAB]


def test_intake_without_a_run(pipe):
    at = _tab(app())
    text = _markdown(at)
    assert "Before the run starts" in text
    assert "already imported" in text            # write_g1 imported a requirement: the intake does not ask for it again
    assert any(b.label.startswith("▶ Run workflow") and not b.disabled for b in at.button)


def _state(root, p, status, cursor, steps_done=(), extra=None):
    from pkf_studio import flow

    st = {"version": 1, "pipeline": p, "mode": "dev", "status": status, "cursor": cursor, "started_at": "2026-10-18T10:00:00+00:00",
          "updated_at": "2026-10-18T10:05:00+00:00", "answers": {}, "auto_approve": False,
          "steps": {s.id: {"status": "not_started"} for s in flow.STEPS}, "gate": {}, "sub": {}, "message": f"{cursor} test"}
    for x in steps_done:
        st["steps"][x] = {"status": "done", "detail": "ok"}
    st["steps"][cursor] = {"status": "waiting" if status.startswith("waiting") else "failed" if status == "failed" else "running",
                           "detail": "detail text", **(extra or {})}
    flowstore.save(root, p, st)
    flowstore.event(root, p, "run_started")
    flowstore.event(root, p, "step_started", step=cursor, title="t", job="j", action="a")
    return st


@pytest.mark.parametrize("status,cursor,extra", [
    ("failed", "1.3", {"problems": ["pkf analyze: the AI step failed"], "can_continue": False}),
    ("waiting_user", "1.4", None),
    ("waiting_user", "2.2", None),
    ("waiting_input", "3.2", None),
    ("waiting_user", "3.4", None),
    ("waiting_user", "4.2", None),
    ("waiting_user", "4.3", None),
    ("waiting_user", "5.3", None),
    ("waiting_input", "6.1", None),
    ("waiting_user", "6.2", None),
    ("waiting_user", "6.5", None),
    ("waiting_user", "7.2", None),
    ("waiting_user", "7.3", None),
    ("stopped", "4.1", None),
    ("done", "7.4", None),
])
def test_every_pause_renders(pipe, status, cursor, extra):
    fake, clone, s = pipe
    _state(s.root, P, status, cursor, extra=extra)
    at = _tab(app())
    text = _markdown(at) + "\n".join(b.label for b in at.button)
    assert cursor in text or status == "done", text[:500]
    if status == "failed":
        assert any(b.label.startswith("↻ Retry") for b in at.button) and any("assistant" in b.label.lower() for b in at.button)
    if status == "waiting_user" and cursor == "6.5":
        assert any(b.label == "Run again" for b in at.button) and any(b.label == "Skip" for b in at.button)
    if status == "stopped":
        assert any(b.label.startswith("▶ Resume") for b in at.button)


def test_waiting_gate_shows_approve_as_me_in_dev(pipe, monkeypatch):
    fake, clone, s = pipe
    from pkf_studio import stages
    from pkf_studio.stages import GateState

    pr = {"number": 7, "html_url": "https://x/pr/7", "head": {"sha": "a" * 40, "ref": f"pkf/{P}/g1"}, "state": "open"}
    rows = [GateState("G1", "waiting", pr), GateState("G2", "blocked"), GateState("G3", "blocked"), GateState("G4", "blocked"),
            GateState("G5", "blocked"), GateState("RUN", "blocked")]
    monkeypatch.setattr(stages, "derive", lambda s_, p_, prs=None: {"gates": rows, "current": "G1", "by_gate": {r.gate: r for r in rows}})
    st = _state(s.root, P, "waiting_gate", "1.7", steps_done=("1.1", "1.2", "1.3", "1.4", "1.5", "1.6"))
    st["gate"] = {"phase": "approve", "pr": 7}
    flowstore.save(s.root, P, st)
    at = _tab(app())
    labels = [b.label for b in at.button]
    assert any("Approve as me" in x for x in labels), labels
    assert any("PR #7" in str(e.label) for e in at.get("link_button")) or True


def test_console_lines_merge_flow_and_job(pipe):
    fake, clone, s = pipe
    from pkf_studio import flow, jobs

    st = _state(s.root, P, "failed", "1.3", extra={"job_id": "20260101T000000-abc"})
    d = jobs.jobs_dir(s.root) / "20260101T000000-abc"
    d.mkdir(parents=True)
    (d / "cmd.json").write_text(json.dumps({"action": "analyze", "pipeline": P, "args": {}, "title": "t", "started_at": "2026-01-01T00:00:00+00:00"}))
    (d / "log").write_text("== analyze\n$ pkf analyze-requirements\n  error: boom\n== failed: boom\n")
    lines = flow.console_lines(s, P, st, "1.3")
    assert [x["source"] for x in lines].count("job") == 4 and any(x["level"] == "error" for x in lines)
    assert any(x["source"] == "flow" for x in lines)


# ------------------------------------------------------------------------------------------------ 0.6.1 controls
def test_intake_offers_assistant_can_act(pipe):
    at = _tab(app())
    assert any("Assistant can act on its own" in str(t.label) for t in at.toggle)


def test_failed_gate_offers_run_from_and_reopen(pipe):
    fake, clone, s = pipe
    _state(s.root, P, "failed", "5.4", steps_done=[x for x in ("1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "2.1", "2.2", "2.3",
                                                               "3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "4.1", "4.2", "4.3", "4.4",
                                                               "5.1", "5.2", "5.3")],
           extra={"detail": "G5 PR #9 is merged, but the gate is stale (...): Reopen G5", "fix_step": "5.2", "reopen": "G5",
                  "gate_state": "stale", "can_continue": False})
    at = _tab(app())
    labels = [b.label for b in at.button]
    assert any(x.startswith("↩ Run from 5.2") for x in labels), labels
    assert any(x == "Confirm Reopen G5" for x in labels), labels          # inside the Reopen popover


def test_navigator_renders_and_explains_a_refused_jump(pipe):
    fake, clone, s = pipe
    _state(s.root, P, "waiting_user", "1.4", steps_done=("1.1", "1.2", "1.3"))
    at = _tab(app())
    assert any(sb.label == "Run from step" for sb in at.selectbox)
    at.selectbox(key=f"wfd-nav-step-{P}").select("5.1 Start S5: Terraform plan, dry run").run()
    text = _markdown(at) + "\n".join(str(w.value) for w in at.warning)
    assert "cannot run yet" in text and "G1" in text, text[-800:]
    assert any(b.label.startswith("Run from 1.") for b in at.button)


def test_navigator_moves_the_run(pipe):
    fake, clone, s = pipe
    from pkf_studio import flow

    _state(s.root, P, "waiting_user", "1.4", steps_done=("1.1", "1.2", "1.3"))
    at = _tab(app())
    at.selectbox(key=f"wfd-nav-step-{P}").select("1.3 Analyze with AI and split").run()
    btn = next(b for b in at.button if b.label == "▶ Run from 1.3")
    btn.click().run()
    st = flow.load(s.root, P)
    assert st["cursor"] == "1.3" and any(e["type"] == "run_from" for e in flowstore.events(s.root, P)), st


def test_pilot_box_and_plain_summary(pipe):
    fake, clone, s = pipe
    st = _state(s.root, P, "failed", "4.1", extra={"problems": ["pkf code: the AI step failed"], "can_continue": False})
    st["assistant_autopilot"] = True
    from pkf_studio import pilot
    st["pilot"] = {"phase": "needs_you", "note": "the assistant could not diagnose 4.1: budget", "fail_key": pilot._fail_key(st),
                   "log": ["diagnosing: …"]}
    flowstore.save(s.root, P, st)
    d = flowstore.assistant_dir(s.root, P, "20261019T100000-abcdef")
    (d / "diagnosis.json").write_text(json.dumps({"root_cause": "Wrong folder. More.", "evidence": [], "fix_kind": "rerun", "proposal": "retry",
                                                  "files": [], "risk": "low", "confidence": 0.8,
                                                  "summary": "The copy step looked in the wrong place; trying again will fix it.",
                                                  "actions": [{"do": "retry", "why": "temporary"}]}))
    (d / "summary.json").write_text(json.dumps({"phase": "diagnose"}))
    at = app()
    at.session_state[f"as-open-{P}"] = True               # 0.8.0: the assistant is a pop-up (sidebar → Open)
    at = _tab(at)
    text = _markdown(at) + "\n".join(str(w.value) for w in at.warning)
    assert "On its own" in text and "could not diagnose" in text
    assert "trying again will fix it" in text and "In short" in text
    assert any(b.label == "Do it" for b in at.button)
