"""The assistant acting on its own (0.6.1, "Assistant can act"): when the Workflow Developer run fails at a step the
assistant may handle, it diagnoses the failure, then does what the diagnosis lists — retry, go back to a step, reopen a
stage, render & plan again, regenerate code — and, for a file fix, prepares it, and the Studio applies it only when its
tests pass. One action per UI tick, the next one only when no job runs; at most PILOT_MAX cycles per step and run.

People keep every review, approval and merge decision, the environment values, credential and Move confirmations, the
checklists and S1 Requirements (flow.PEOPLE_STEPS / flow.assistant_may); a fix that touches the requirement waits for a
person. The pilot's state is `pilot` in the run's state.json; every decision is in the journal (assistant_pilot,
assistant_action)."""
from __future__ import annotations

import json
from pathlib import Path

from . import flow, flowstore, jobs

PILOT_MAX = 3      # diagnose → act cycles per step and run
ENDED = ("done", "needs_you", "gave_up")


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save_pilot(s, p: str, pilot: dict) -> None:
    st = flow.load(s.root, p)
    if st is None:
        return
    st["pilot"] = pilot
    flowstore.save(s.root, p, st)


def _note(s, p: str, pilot: dict, phase: str, text: str) -> dict:
    pilot = {**pilot, "phase": phase, "note": text[:600]}
    pilot.setdefault("log", []).append(f"{phase}: {text}"[:300])
    pilot["log"] = pilot["log"][-12:]
    _save_pilot(s, p, pilot)
    flowstore.event(s.root, p, "assistant_pilot", phase=phase, summary=text[:300], step=pilot.get("step"))
    return pilot


def _fail_key(st: dict) -> str | None:
    cur = st.get("cursor")
    row = (st.get("steps") or {}).get(cur or "") or {}
    if st.get("status") != "failed" or row.get("status") != "failed":
        return None
    return f"{cur}:{row.get('attempts') or 0}:{row.get('ended_at') or ''}:{(row.get('detail') or '')[:80]}"


def eligible(st: dict | None) -> bool:
    """The run failed at a step the assistant may handle on its own."""
    if not st or not st.get("assistant_autopilot") or st.get("status") != "failed":
        return False
    step = flow.BY_ID.get(st.get("cursor") or "")
    return step is not None and step.stage != "S1" and step.id not in flow.PEOPLE_STEPS


def pending(st: dict | None) -> bool:
    """The pilot has something to do (the UI keeps refreshing while it does)."""
    if not st or not st.get("assistant_autopilot"):
        return False
    pilot = st.get("pilot") or {}
    if pilot.get("phase") in ("diagnosing", "acting", "fixing"):
        return True
    key = _fail_key(st)
    return eligible(st) and key is not None and pilot.get("fail_key") != key


def tick(s, p: str, states: dict, derive=None) -> dict | None:
    """Advance the pilot by at most one thing. Returns the pilot state (for the UI), or None when it is off."""
    st = flow.load(s.root, p)
    if not st or not st.get("assistant_autopilot"):
        return None
    pilot = dict(st.get("pilot") or {})
    if jobs.current(s.root, p):
        return pilot                                   # a job runs (the run's, or the assistant's own): wait for it
    phase = pilot.get("phase")
    if phase == "diagnosing":
        return _after_diagnosis(s, p, states, pilot)
    if phase == "fixing":
        return _after_fix(s, p, states, pilot)
    if phase == "acting":
        return _act_next(s, p, states, pilot, derive)
    key = _fail_key(st)
    if not eligible(st) or key is None or pilot.get("fail_key") == key:
        return pilot
    cur = st["cursor"]
    cycles = dict(pilot.get("cycles") or {})
    if int(cycles.get(cur) or 0) >= PILOT_MAX:
        return _note(s, p, {**pilot, "fail_key": key, "step": cur}, "gave_up",
                     f"{cur} failed again after {PILOT_MAX} tries by the assistant: it is your turn (Retry, Ask the assistant, or Run from)")
    from .assistant.protocol import new_session_id

    sid = new_session_id()
    row = st["steps"][cur]
    try:
        jobs.start(s.root, p, "assist", {"phase": "diagnose", "step_id": cur, "job_id": row.get("job_id"), "session_id": sid},
                   f"🤖 Assistant (on its own): diagnose {cur}")
    except jobs.JobError as exc:
        return pilot | {"note": f"could not start the diagnosis yet: {exc}"}
    cycles[cur] = int(cycles.get(cur) or 0) + 1
    pilot = {"fail_key": key, "step": cur, "session": sid, "cycles": cycles, "queue": [], "log": pilot.get("log") or []}
    return _note(s, p, pilot, "diagnosing", f"{cur} failed: the assistant is looking for the cause (try {cycles[cur]} of {PILOT_MAX})")


def _session(s, p: str, sid: str) -> Path:
    return flowstore.flow_dir(s.root, p) / "assistant" / sid


def _after_diagnosis(s, p: str, states: dict, pilot: dict) -> dict:
    d = _session(s, p, pilot.get("session") or "")
    diag = _read(d / "diagnosis.json")
    if not diag:
        err = _read(d / "summary.json").get("error") or "the diagnosis did not finish"
        return _note(s, p, pilot, "needs_you", f"the assistant could not diagnose {pilot.get('step')}: {err}")
    queue = [a for a in (diag.get("actions") or flow.default_actions(diag, states)) if isinstance(a, dict) and a.get("do")]
    if diag.get("fix_kind") != "edit_files":
        queue = [a for a in queue if a.get("do") != "fix"]
    plain = diag.get("summary") or str(diag.get("root_cause") or "")[:300]
    if not queue:
        what = {"user_action": "something only you can do", "settings": "a setting to change"}.get(diag.get("fix_kind"), "a decision")
        return _note(s, p, {**pilot, "queue": []}, "needs_you", f"{plain} — this needs {what}: "
                     f"{diag.get('user_action') or diag.get('proposal') or ''}")
    pilot = {**pilot, "queue": queue, "plain": plain}
    return _note(s, p, pilot, "acting", f"{plain} — next: " + ", ".join(_label(a) for a in queue))


def _label(a: dict) -> str:
    do = a.get("do")
    return {"run_from": f"run from {a.get('step')}", "reopen": f"reopen {a.get('gate')}", "regenerate": f"regenerate {a.get('kind')} {a.get('name')}",
            "fix": "prepare and test the fix", "retry": "retry", "render_plan_again": "render & plan again",
            "tests_build_again": "run tests & build again", "restore_dags": "restore dags/", "resume": "resume the run"}.get(do, str(do))


def _act_next(s, p: str, states: dict, pilot: dict, derive=None) -> dict:
    queue = list(pilot.get("queue") or [])
    if not queue:
        return _note(s, p, pilot, "done", "the assistant did what it planned; the run continues (reviews and approvals wait for you)")
    a = queue.pop(0)
    pilot = {**pilot, "queue": queue}
    if a.get("do") == "fix":
        try:
            jobs.start(s.root, p, "assist", {"phase": "fix", "session_id": pilot.get("session")}, "🤖 Assistant (on its own): prepare the fix")
        except jobs.JobError as exc:
            return _note(s, p, {**pilot, "queue": [a, *queue]}, "acting", f"could not start the fix yet: {exc}")
        return _note(s, p, pilot, "fixing", "the assistant is changing the files and running the tests")
    if a.get("do") == "resume":                       # internal: the fix is in, the held run continues
        try:
            flow.resume(s, p, states)
        except Exception as exc:  # noqa: BLE001
            return _note(s, p, pilot, "needs_you", f"could not resume the run: {str(exc)[:300]}")
        return _note(s, p, pilot, "acting" if queue else "done", "the fix is in: the run continues")
    if a.get("do") in ("reopen", "run_from") and queue and queue[0].get("do") == "fix":
        a = {**a, "hold": True}                       # the reopened stage waits for the fix (it would use the old files)
        pilot["held"] = True
    st = flow.load(s.root, p)
    why = flow.assistant_may(s, p, states, st, a)
    if why:
        flowstore.event(s.root, p, "assistant_action", do=a.get("do"), ok=False, summary=f"refused {a.get('do')}: {why}"[:300], action=a)
        return _note(s, p, pilot, "acting" if queue else "needs_you", f"skipped {_label(a)}: {why}")
    try:
        msg = flow.assistant_act(s, p, states, a, derive=derive)
    except Exception as exc:  # noqa: BLE001 - a refused or failed action is reported, never a crash
        return _note(s, p, pilot, "needs_you", f"{_label(a)} did not work: {str(exc)[:300]}")
    return _note(s, p, pilot, "acting" if queue else "done", msg)


S1_PREFIXES = ("requirements/", "pipelines/")


def _other_changes(s, p: str, files: list[str]) -> list[str]:
    """Uncommitted changes in the worktree that are not the fix's (protocol.apply_fix commits everything)."""
    try:
        wt = s.worktree_path(p)
        if not (Path(wt) / ".git").exists():
            return []
        from .service import _porcelain

        return sorted(set(_porcelain(Path(wt))) - set(files))
    except Exception:  # noqa: BLE001
        return []


def _after_fix(s, p: str, states: dict, pilot: dict) -> dict:
    d = _session(s, p, pilot.get("session") or "")
    fix = _read(d / "fix.json")
    summ = _read(d / "summary.json")
    if not fix or summ.get("applied") or summ.get("discarded"):
        err = summ.get("error") or ("already applied" if summ.get("applied") else "the fix did not finish")
        return _note(s, p, pilot, "needs_you", f"the fix was not applied: {err}")
    tests = fix.get("tests") or {}
    files = [str(f) for f in fix.get("files_changed") or []]
    verified = fix.get("verified") or {}
    if not (tests.get("ran") and tests.get("ok") and verified.get("ok")):
        why = str(tests.get("detail") or "")[:200] if not (tests.get("ran") and tests.get("ok")) else \
            f"the Studio's own test run stopped at {verified.get('stopped_at') or verified.get('error') or 'an error'}"
        return _note(s, p, pilot, "needs_you", "the assistant's fix does not pass its tests, so it was not applied: review the diff "
                     f"(Apply or Discard) — {why}")
    extra = _other_changes(s, p, files)
    if extra:
        return _note(s, p, pilot, "needs_you", f"the worktree also has changes the fix did not make ({', '.join(extra[:5])}): "
                     "a person applies it, so nothing of theirs is committed by accident")
    s1 = [f for f in files if f.startswith(S1_PREFIXES) and (f.startswith(f"requirements/{p}/") or f == f"pipelines/{p}.yaml")]
    if s1:
        return _note(s, p, pilot, "needs_you", f"the fix changes the requirement ({', '.join(s1)}): a person reviews and applies it")
    if not files:
        return _note(s, p, pilot, "needs_you", "the fix changed no file")
    from .assistant import protocol

    try:
        sha = protocol.apply_fix(s, p, pilot.get("session"))
        try:
            s.invalidate()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        return _note(s, p, pilot, "needs_you", f"the fix could not be committed: {str(exc)[:300]}")
    queue = list(pilot.get("queue") or [])
    st = flow.load(s.root, p) or {}
    if pilot.get("held") and st.get("status") == "stopped":
        queue = [{"do": "resume", "why": "the fix is applied"}, *queue]
        pilot = {**pilot, "held": False}
    elif not queue and st.get("status") == "failed":
        queue = [{"do": "retry", "why": "the fix is applied"}]
    plain = fix.get("plain_summary") or fix.get("summary") or "the fix"
    return _note(s, p, {**pilot, "queue": queue}, "acting", f"applied the fix ({str(sha or '')[:7]}): {plain}")
