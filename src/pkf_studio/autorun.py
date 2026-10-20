"""Full auto — "Run without stopping" (0.8.2, dev only): the decisions a person would click while the run waits, made by
deterministic Studio code (no model), one per tick, each written to the workflow log as a `full_auto` event and to
st["auto"]["log"]. It calls only what the buttons call (flow.answer, flow.mark_done, flow.preview_again, the
stage_test_data job). Failures stay with the assistant acting on its own (pilot); gates with auto-approve.

It stops — leaves the run waiting with "Full auto stopped: <why>" — where a person is needed: open questions at 1.4,
environment values at 3.2, a component still failing after its tries, 6.1/6.2 without the intake's pre-answers, a
preview that would move files that are not its own test package (unless "Move whatever the preview finds"), a test
package it could not place (the commands are shown), a 6.5 that is not a no-op, and the Airflow deploy (7.3 / 7.4).

Safety: dev mode only (never SIT/PPT/PRD); a cap on decisions per run; a cap per kind of retry; the test package is
moved only when every planned source key is one it placed itself, in the same bucket."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import flow, flowstore, jobs

MAX_DECISIONS = 80          # per run: a loop between Full auto and the assistant's run-from / reopen ends here
REGEN_TRIES = 2             # 4.3: Regenerate per component
TEST_BUILD_TRIES = 2        # 4.3: Run tests & build again (the draft build)
STAGE_TRIES = 1             # 6.3: place a test package
PREVIEWS_AFTER_STAGE = 3    # 6.3: previews after placing it
MAX_WAIT_S = 30 * 60        # the source's minimum file age, capped
REVIEWS = ("2.2", "3.4", "4.2")


def enabled(s, st: dict | None) -> bool:
    return bool(st and st.get("full_auto") and not s.strict and s.cfg.mode == "dev")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _auto(st: dict) -> dict:
    a = st.setdefault("auto", {})
    for k, v in (("log", []), ("count", 0), ("tries", {}), ("staged", None), ("job", None), ("needs_you", None)):
        a.setdefault(k, v)
    return a


def _record(s, p: str, what: str, step: str) -> None:
    st = flow.load(s.root, p)
    if not st:
        return
    a = _auto(st)
    a["count"] = int(a["count"]) + 1
    a["log"] = (a["log"] + [f"{_now():%H:%M:%S} {step} {what}"])[-40:]
    a["needs_you"] = None
    a.pop("needs_you_key", None)
    flowstore.save(s.root, p, st)
    flowstore.event(s.root, p, "full_auto", step=step, summary=what)


def _stop(s, p: str, st: dict, step: str, why: str) -> str:
    """Leave the run waiting with the reason (once per situation)."""
    a = _auto(st)
    key = f"{step}|{st.get('status')}|{why}"
    if a.get("needs_you_key") == key:
        return ""
    a["needs_you"], a["needs_you_key"] = why, key
    st["message"] = f"Full auto stopped at {step}: {why}"
    flowstore.save(s.root, p, st)
    flowstore.event(s.root, p, "full_auto", step=step, summary=f"stopped: {why}")
    return f"stopped: {why}"


def _try(st: dict, key: str, limit: int) -> bool:
    t = _auto(st)["tries"]
    if int(t.get(key) or 0) >= limit:
        return False
    t[key] = int(t.get(key) or 0) + 1
    return True


def _planned_keys(pv: dict) -> list[str] | None:
    try:
        plan = json.loads(Path(str(pv.get("planned") or "")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return [str(f.get("source_key") or "") for f in plan.get("files") or []]


def _open_questions(s, p: str) -> int:
    from .ui.common import Files

    f = Files(s, p)
    qs = (f.json(f"requirements/{p}/analysis.json") or {}).get("open_questions") or []
    amb = ((f.yaml(f"pipelines/{p}.lock.yaml") or {}).get("split") or {}).get("ambiguities") or []
    return len(qs) + len(amb)


def tick(s, p: str, states: dict) -> str | None:
    """One decision, or None. Never raises into the page (the caller shows a warning)."""
    st = flow.load(s.root, p)
    if not enabled(s, st) or st.get("status") in ("done", "stopped", "idle", None):
        return None
    a = _auto(st)
    if a.get("job"):                                   # the test package job started by Full auto
        return _stage_job_ended(s, p, st, a)
    if jobs.current(s.root, p):
        return None
    step = flow.BY_ID.get(st.get("cursor") or "")
    if step is None:
        return None
    if int(a["count"]) >= MAX_DECISIONS:
        return _stop(s, p, st, step.id, f"{MAX_DECISIONS} automatic decisions in this run — have a look")
    row = st["steps"].get(step.id) or {}
    status = st.get("status")
    if status == "failed":
        if step.id == "6.5":
            return _stop(s, p, st, step.id, "the second run was not a no-op: pause the pipeline and check its source")
        if step.id == "6.3":
            # a move pipeline: Preview again (a new preview, then its own Move); any other pipeline would run for real again
            if flow.custom_delivery(s, p) and _try(st, "6.3-again", 1):
                flowstore.save(s.root, p, st)
                flow.preview_again(s, p, states, step.id)
                _record(s, p, "the run failed: Preview again (once)", step.id)
                return "preview again"
            return _stop(s, p, st, step.id, "the local run failed: see its report.json and run.log, or ask the assistant")
        return None                                    # the assistant acting on its own (pilot) handles failures
    if status not in flow.WAITING or row.get("status") != "waiting":
        return None
    return _decide(s, p, states, st, a, step)


def _answer(s, p: str, states: dict, step_id: str, values: dict, what: str) -> str:
    flow.answer(s, p, states, step_id, values)
    _record(s, p, what, step_id)
    return what


def _decide(s, p: str, states: dict, st: dict, a: dict, step) -> str | None:
    sid = step.id
    if sid == "1.4":
        n = _open_questions(s, p)
        if n:
            return _stop(s, p, st, sid, f"{n} open question(s) or ambiguities in the requirement: answer them in the text")
        return _answer(s, p, states, sid, {"detail": "continued by Full auto: no open questions"}, "Continue (no open questions)")
    if sid in REVIEWS:
        return _answer(s, p, states, sid, {"detail": "continued by Full auto"}, "Continue")
    if sid == "5.3":
        redone = (st["steps"].get("5.2") or {}).get("status") == "done"     # Render and plan again ended ok
        if (st["steps"].get("5.1") or {}).get("problems") and not redone:
            return _stop(s, p, st, sid, "5.1 reported problems: review the Terraform plan and the dry run")
        return _answer(s, p, states, sid, {"detail": "continued by Full auto"}, "Continue")
    if sid == "5.2":
        if _try(st, "5.2", 1):
            flowstore.save(s.root, p, st)
            return _answer(s, p, states, sid, {"job": "prepare_env"}, "Render and plan again")
        return _stop(s, p, st, sid, "Render and plan again did not clear 5.1's problems")
    if sid == "4.3":
        return _regenerate(s, p, states, st, sid)
    if sid == "3.2":
        return _stop(s, p, st, sid, "environment values are still <REQUIRED> (buckets, folders): fill them in")
    if sid == "6.1":
        if (st.get("answers") or {}).get("own_credentials"):
            return _answer(s, p, states, sid, {"own_credentials": True}, "Confirm own AWS credentials (intake)")
        return _stop(s, p, st, sid, "confirm running with your own AWS credentials (not pre-confirmed in the intake)")
    if sid == "6.2":
        if (st.get("answers") or {}).get("no_old_dag"):
            for item in step.items:
                flow.mark_done(s, p, states, sid, item, True)
            _record(s, p, "no old DAG to pause (intake)", sid)
            return "6.2 done"
        return _stop(s, p, st, sid, "tick that the old DAG is paused (not pre-confirmed in the intake)")
    if sid in ("6.3", "6.5"):
        return _s6(s, p, states, st, a, step)
    if sid == "7.2":
        return _answer(s, p, states, sid, {"detail": "Full auto: the artifacts zip is on the Workflow tab any time"}, "Done (artifacts any time)")
    if sid in ("7.3", "7.4"):
        return _stop(s, p, st, sid, "deploying to Airflow is done by hand (deploy.md has the commands)")
    return None


def _regenerate(s, p: str, states: dict, st: dict, sid: str) -> str:
    bad = flow.components_needing_work(s, p)
    for r in bad:
        kind, name = r.get("kind"), r.get("name")
        if kind in ("build", None) or not name:
            continue
        if _try(st, f"regen-{kind}-{name}", REGEN_TRIES):
            flowstore.save(s.root, p, st)
            return _answer(s, p, states, sid, {"job": "code_component", "args": {"kind": kind, "name": name},
                                               "title": f"Regenerate {kind} {name}"}, f"Regenerate {kind} {name}")
    if any(r.get("kind") == "build" for r in bad) and _try(st, "test_build", TEST_BUILD_TRIES):
        flowstore.save(s.root, p, st)
        return _answer(s, p, states, sid, {"job": "test_build", "title": "Run tests & build again"}, "Run tests & build again")
    flowstore.save(s.root, p, st)
    names = ", ".join(f"{r.get('kind')} {r.get('name')}" for r in bad[:4])
    return _stop(s, p, st, sid, f"still failing after the automatic tries: {names}")


def _s6(s, p: str, states: dict, st: dict, a: dict, step) -> str | None:
    sid = step.id
    pv = (st.get("sub") or {}).get("preview") or {}
    if sid == "6.5":
        if pv and int(pv.get("files") or 0) > 0:
            return _stop(s, p, st, sid, f"the second run would move {pv.get('files')} file(s): not a no-op — check the source")
        if not _try(st, "6.5-run", 1):
            return _stop(s, p, st, sid, "6.5 ran once already: decide yourself whether to run it again")
        flowstore.save(s.root, p, st)
        return _answer(s, p, states, sid, {}, "Run again (must be a no-op)")
    if not pv:
        return None
    n = int(pv.get("files") or 0)
    if n > 0:
        keys = _planned_keys(pv)
        staged = a.get("staged") or {}
        mine = set(staged.get("keys") or [])            # only keys the Studio placed itself (not ones placed by hand)
        bucket = staged.get("bucket")
        own = bool(keys) and bucket == pv.get("source_bucket") and set(keys) <= mine
        if own or (st.get("answers") or {}).get("move_any"):
            why = "its own test package" if own else "Move whatever the preview finds (intake)"
            return _answer(s, p, states, sid, {"planned": str(pv.get("planned"))}, f"Move {n} file(s) — {why}")
        return _stop(s, p, st, sid, f"the preview found {n} file(s) in {pv.get('source_bucket')} that are not its test package: "
                                    "check them and click Move yourself (or allow it in Run ▾)")
    # nothing to move: place a test package once, wait out the minimum age, preview again (bounded)
    if not a.get("staged"):
        if not _try(st, "stage", STAGE_TRIES):
            return _stop(s, p, st, sid, "the test package could not be placed: run the commands shown at 6.3, then Preview again")
        try:
            jid = jobs.start(s.root, p, "stage_test_data", {}, "6.3 Prepare a test package")
        except jobs.JobError as exc:
            flowstore.save(s.root, p, st)
            return _stop(s, p, st, sid, f"could not start the test package job: {exc}")
        a["job"] = jid
        st["message"] = "Full auto: preparing a test package for the source (6.3 found nothing to move)"
        flowstore.save(s.root, p, st)
        _record(s, p, "Prepare a test package (the preview found nothing)", sid)
        return "staging"
    nb = a["staged"].get("not_before")
    if nb and _now() < dt.datetime.fromisoformat(nb):
        msg = f"Full auto: waiting until {dt.datetime.fromisoformat(nb).astimezone():%H:%M:%S} for the test files' minimum age"
        if st.get("message") != msg:
            st["message"] = msg
            flowstore.save(s.root, p, st)
        return None
    if _try(st, "preview-after-stage", PREVIEWS_AFTER_STAGE):
        flowstore.save(s.root, p, st)
        flow.preview_again(s, p, states, sid)
        _record(s, p, "Preview again (the test package is in place)", sid)
        return "preview again"
    return _stop(s, p, st, sid, "the preview still finds nothing after placing the test package: check the example's keys "
                                "against the source's folder and filters")


def _stage_job_ended(s, p: str, st: dict, a: dict) -> str | None:
    stt = jobs.status(s.root, a["job"])
    if stt.get("state") in ("running", "starting"):
        return None
    res = stt.get("result") or {}
    a["job"] = None
    if stt.get("state") == "succeeded" and res.get("ok"):
        wait = min(int(res.get("min_age_s") or 0), MAX_WAIT_S)
        keys = list(res.get("placed") or []) + list(res.get("existing") or [])
        a["staged"] = {"bucket": res.get("bucket"), "keys": list(res.get("placed") or []), "all": keys,
                       "folder": res.get("folder"), "not_before": (_now() + dt.timedelta(seconds=wait + 5)).isoformat()}
        flowstore.save(s.root, p, st)
        _record(s, p, f"test package placed: {len(res.get('placed') or [])} new, {len(res.get('existing') or [])} kept", "6.3")
        return "staged"
    a["staged"] = None
    a["stage_failed"] = {"problem": res.get("problem") or (res.get("problems") or ["the job failed"])[0],
                         "upload_sh": res.get("upload_sh"), "commands": (res.get("commands") or [])[:20]}
    flowstore.save(s.root, p, st)
    hint = ("run the commands shown at 6.3, then Preview again" if a["stage_failed"]["commands"] else
            "place a file in the source yourself, then Preview again")
    return _stop(s, p, flow.load(s.root, p), "6.3", f"the test package could not be placed ({a['stage_failed']['problem']}): {hint}")


def forget_s6(st: dict) -> None:
    """Run from / Reopen redoes S6: the test package and its tries belong to the S6 that is being redone."""
    a = st.get("auto") or {}
    for k in ("staged", "stage_failed", "job", "needs_you", "needs_you_key"):
        a.pop(k, None)
    for k in [k for k in (a.get("tries") or {}) if k in ("stage", "preview-after-stage", "6.3-again", "6.5-run")]:
        del a["tries"][k]
