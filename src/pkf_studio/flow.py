"""Workflow Developer — one pipeline's run through S1…S6 and 7 Deploy as 34 substages (docs/WORKFLOW_DEVELOPER_PLAN.md).

The run is a cursor over STEPS, advanced by `tick()` every couple of seconds from the UI. Long work is the Studio's own
detached jobs (jobs.start); approvals are the Studio's own request_approval / post_approve / merge; the run only decides
*what comes next*. It never owns pipeline truth: `stages.derive()` says which gate is current, and `state.json`
(.pkf-studio/flows/<p>/, git-ignored) only remembers the cursor, the answers, the timings and what each step produced.
Delete it and Run workflow starts again from the current gate.

Step kinds
  input      answered in the intake form before the run starts (1.1, 1.2) or in a pause (3.2 env values, 6.1 credentials)
  job        a Studio job; done when it succeeded without problems, failed (red) otherwise — Retry / Ask the assistant /
             Continue anyway (only when the job itself succeeded, i.e. it ended with warnings)
  review     a pause: the UI shows the artefact and the user clicks Continue (or a stage-specific alternative)
  gate       request approval → wait for the approvers → merge (phases); dev mode: Approve as me inline
  auto       decided by the run itself (1.5 split again only if the requirement changed, 3.5 skip G3, 6.4 record PR)
  checklist  manual items the app cannot do (6.2 pause the old DAG, 7.3 deploy, 7.4 unpause): Mark done per item
  action     one click (7.2 download artifacts)

Run status: idle (no run) · running (a job runs) · waiting_input · waiting_gate · waiting_user (review, checklist, a
choice after a failure) · failed · done · stopped.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import flowstore, jobs
from . import ledger as L
from .gates import ORDER
from .service import StudioError, branch_for, readiness, unfilled
from .stages import STAGES, needs_ai_plan, stage_started

STAGE_TITLES = {"S1": "S1 Requirements → G1", "S2": "S2 Design → G2", "S3": "S3 Plans → G3", "S4": "S4 Code and build → G4",
                "S5": "S5 Dev environment → G5", "S6": "S6 Run local + record PR", "S7": "7 Deploy to Airflow"}
STAGE_GATE = {"S1": "G1", "S2": "G2", "S3": "G3", "S4": "G4", "S5": "G5", "S6": "RUN", "S7": None}
GATE_STAGE = {v: k for k, v in STAGE_GATE.items() if v}
REVIEW_OF = {"G1": "1.4", "G2": "2.2", "G3": "3.4", "G4": "4.2", "G5": "5.3"}   # where a rejected gate sends the run back
DONE = ("done", "skipped")
WAITING = ("waiting_input", "waiting_gate", "waiting_user")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


@dataclass(frozen=True)
class Step:
    id: str
    stage: str
    title: str
    kind: str
    action: str | None = None
    optional: bool = False
    phases: tuple[str, ...] = ()        # gate steps: which of request / approve / merge this step covers
    items: tuple[str, ...] = ()         # checklist items
    help: str = ""


STEPS: list[Step] = [
    Step("1.1", "S1", "New pipeline name", "input", help="snake_case, validated"),
    Step("1.2", "S1", "Import doc and scan", "input", help="upload or paste; secret and personal-data scan"),
    Step("1.3", "S1", "Analyze with AI and split", "job", "analyze"),
    Step("1.4", "S1", "Edit and save requirement", "review", help="answer the open questions and ambiguities in the text"),
    Step("1.5", "S1", "Split again", "job", "resplit", help="only when the requirement changed in 1.4"),
    Step("1.6", "S1", "Request approval G1", "gate", phases=("request",)),
    Step("1.7", "S1", "PO + BA + Architect approve", "gate", phases=("approve",)),
    Step("1.8", "S1", "Refresh and Merge", "gate", phases=("merge",)),
    Step("2.1", "S2", "Start S2: G1 ledger row", "job", "start_g2"),
    Step("2.2", "S2", "Review split", "review", help="the components the AI proposed; Reopen G1 if the split is wrong"),
    Step("2.3", "S2", "G2: DE approves, Merge", "gate", phases=("request", "approve", "merge")),
    Step("3.1", "S3", "Start S3: approve split", "job", "start_g3"),
    Step("3.2", "S3", "Fill env values", "input", help="buckets, folders, queue URLs for dev"),
    Step("3.3", "S3", "Plan components (AI)", "job", "plan_components"),
    Step("3.4", "S3", "Review plan cards", "review"),
    Step("3.5", "S3", "Skip G3: no AI plan", "auto"),
    Step("3.6", "S3", "G3: DE approves, Merge", "gate", phases=("request", "approve", "merge")),
    Step("4.1", "S4", "Start S4: code, test, build", "job", "start_g4"),
    Step("4.2", "S4", "Review code and capabilities", "review"),
    Step("4.3", "S4", "Regenerate / tests and build", "review", optional=True, help="only when a component fails"),
    Step("4.4", "S4", "G4: DE approves, Merge", "gate", phases=("request", "approve", "merge")),
    Step("5.1", "S5", "Start S5: Terraform plan, dry run", "job", "start_g5"),
    Step("5.2", "S5", "Render and plan again", "review", optional=True, help="only when 5.1 reported problems"),
    Step("5.3", "S5", "Review plan, IAM, dry run", "review"),
    Step("5.4", "S5", "G5: DE approves, Merge", "gate", phases=("request", "approve", "merge")),
    Step("6.1", "S6", "Run role or own creds", "input"),
    Step("6.2", "S6", "Pause old DAG", "checklist", items=("The DAG this pipeline replaces is paused in Airflow (or there is none)",)),
    Step("6.3", "S6", "Run locally: apply + pkf run", "job", "run_local"),
    Step("6.4", "S6", "Record PR merged", "auto"),
    Step("6.5", "S6", "Run again: no-op", "job", "run_local", optional=True, help="a second run must deliver nothing"),
    Step("7.1", "S7", "pkf build --all --check", "job", "build_check"),
    Step("7.2", "S7", "Download artifacts", "action"),
    Step("7.3", "S7", "Deploy DAG, runtime, IAM", "checklist",
         items=("dags/ copied to the Airflow DAGs folder", "pkf_runtime installed on every worker", "The worker IAM policy is attached")),
    Step("7.4", "S7", "Unpause, match dry run", "checklist", items=("The DAG is unpaused in Airflow", "The first run matches the dry run")),
]
BY_ID = {s.id: s for s in STEPS}
STAGE_IDS = list(dict.fromkeys(s.stage for s in STEPS))
FIRST_OF = {st: next(s.id for s in STEPS if s.stage == st) for st in STAGE_IDS}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _next_id(step_id: str) -> str | None:
    ids = [s.id for s in STEPS]
    i = ids.index(step_id)
    return ids[i + 1] if i + 1 < len(ids) else None


# ------------------------------------------------------------------------------------------------------------ state
def _new_state(p: str, mode: str) -> dict:
    return {"version": 1, "pipeline": p, "mode": mode, "status": "idle", "cursor": None, "started_at": None, "updated_at": _now(),
            "answers": {}, "auto_approve": False, "steps": {s.id: {"status": "not_started"} for s in STEPS}, "gate": {}, "sub": {},
            "message": ""}


def load(root: Path, p: str) -> dict | None:
    st = flowstore.load(root, p)
    if st and isinstance(st.get("steps"), dict):
        for s in STEPS:                                   # a state written by an older step table
            st["steps"].setdefault(s.id, {"status": "not_started"})
    return st


def _save(root: Path, st: dict) -> dict:
    st["updated_at"] = _now()
    flowstore.save(root, st["pipeline"], st)
    return st


def _set(st: dict, step_id: str, status: str, detail: str | None = None, **more) -> None:
    row = st["steps"][step_id]
    was = row.get("status")
    row["status"] = status
    if detail is not None:
        row["detail"] = detail[:400]
    row.update(more)
    if status in ("running", "waiting") and was not in ("running", "waiting"):
        row.setdefault("started_at", _now())
    if status in ("done", "failed", "skipped"):
        row["ended_at"] = _now()


def _event(root: Path, st: dict, type_: str, **data) -> None:
    try:
        flowstore.event(root, st["pipeline"], type_, **data)
    except OSError:
        pass


def _cur(st: dict) -> Step | None:
    return BY_ID.get(st.get("cursor") or "")


# ------------------------------------------------------------------------------------------------------------ files
def _wt_read(s, p: str):
    wt = s.worktree_path(p)

    def read(rel: str) -> str | None:
        f = wt / rel
        try:
            return f.read_text(encoding="utf-8") if f.is_file() else None
        except OSError:
            return None
    return read


def _yaml(read, rel: str) -> dict:
    try:
        return yaml.safe_load(read(rel) or "") or {}
    except yaml.YAMLError:
        return {}


def _json(read, rel: str) -> dict:
    try:
        return json.loads(read(rel) or "{}") or {}
    except ValueError:
        return {}


def components_needing_work(s, p: str) -> list[dict]:
    """S4: components that fail or are not coded yet, from review/<p>/tests.json (the S4 status)."""
    t = _json(_wt_read(s, p), f"review/{p}/tests.json")
    out = []
    for r in t.get("components") or []:
        bad = r.get("failing") or r.get("ai_issues") or (r.get("state") not in (None, "ok", "ready", "approved", "code_generated"))
        if bad:
            out.append(r)
    if t and not t.get("build_ok"):
        out.append({"kind": "build", "name": "draft build", "state": "failed"})
    return out


def custom_delivery(s, p: str) -> bool:
    try:
        from . import actions as A

        return bool(A._custom_delivery(s.read_base, p))
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------------------------------------------------ start
def intake_fields(s, p: str | None) -> dict:
    """What the intake form must still ask (the deterministic inputs): the UI renders exactly these."""
    me = s.me()
    fields = {
        "name": {"ask": not p, "label": "Pipeline name (snake_case)"},
        "document": {"ask": True, "label": "Requirement document or text"},
        "whoami": {"ask": me is None, "label": "Who am I (Setup → Who am I)"},
        "aws": {"ask": not s.cfg.aws_account_id, "label": "AWS account id and region (Setup)", "optional": True},
        "run_role": {"ask": not s.cfg.run_role_arn, "label": "Run role ARN (Setup); without one only a single-person dev team can run",
                     "optional": True},
    }
    if p:
        read = _wt_read(s, p)
        if read(f"requirements/{p}/extracted.txt") is not None or s.read_base(f"requirements/{p}/extracted.txt") is not None:
            fields["document"]["ask"] = False
            fields["document"]["have"] = True
    return fields


def start(s, p: str, states: dict, answers: dict | None = None) -> dict:
    """Create the run. `answers` (intake): document_name / document_bytes / document_text / flags (1.2), auto_approve.
    A pipeline that already has approved gates starts at its current stage; the earlier steps are shown as done."""
    answers = dict(answers or {})
    st = _new_state(p, s.cfg.mode)
    st["auto_approve"] = bool(answers.pop("auto_approve", False)) and not s.strict
    st["assistant_autopilot"] = bool(answers.pop("assistant_autopilot", not s.strict))   # 0.6.1: "Assistant can act"
    # 0.8.2 "Run without stopping" (dev only): auto-approve + the assistant acting + autorun's decisions (autorun.py)
    st["full_auto"] = bool(answers.pop("full_auto", False)) and not s.strict and s.cfg.mode == "dev"
    if st["full_auto"]:
        st["auto_approve"] = st["assistant_autopilot"] = True
    doc_text = answers.pop("document_text", None)
    doc_bytes = answers.pop("document_bytes", None)
    doc_name = answers.pop("document_name", None) or "pasted text"
    flags = answers.pop("flags", None) or []
    st["answers"] = {k: v for k, v in answers.items() if not str(k).startswith("_")}
    st["status"] = "running"
    st["started_at"] = _now()
    _event(s.root, st, "run_started", mode=st["mode"], auto_approve=st["auto_approve"])
    # 1.1 / 1.2 from the intake
    _set(st, "1.1", "done", p)
    if doc_text is not None:
        from .ui.requirements import import_text

        import_text(s, p, doc_name, doc_bytes, doc_text, list(flags))
        _set(st, "1.2", "done", f"{doc_name} · {len(doc_text)} characters")
        _event(s.root, st, "input_collected", step="1.2", keys=["document"], name=doc_name, chars=len(doc_text))
    elif intake_fields(s, p)["document"].get("have"):
        _set(st, "1.2", "done", "already imported")
    else:
        _set(st, "1.2", "failed", "import or paste the requirement first")
        st["status"], st["cursor"] = "failed", "1.2"
        return _save(s.root, st)
    st["cursor"] = _initial_cursor(s, p, states, st)
    return _save(s.root, _advance(s, p, states, st))


def _initial_cursor(s, p: str, states: dict, st: dict) -> str:
    """Where an existing pipeline is: every stage whose gate is approved is done; the current gate's stage starts at
    its first step not yet done (its Start job already ran → its review step; its PR is open → its gate step)."""
    by = states["by_gate"]
    cur_gate = states["current"]
    for g in ORDER:
        if by[g].done and g != cur_gate:
            for s_ in STEPS:
                if STAGE_GATE[s_.stage] == g:
                    _set(st, s_.id, "skipped" if by[g].state == "skipped" else "done", "before this run")
        else:
            break
    if cur_gate == "RUN" and by["RUN"].state in ("done", "failed"):
        return "6.5" if by["RUN"].state == "done" else "6.3"
    stage = GATE_STAGE.get(cur_gate, "S1")
    gs = by.get(cur_gate)
    read = _wt_read(s, p)
    if stage == "S1":
        if _yaml(read, f"pipelines/{p}.yaml").get("requirement") or s.read_base(f"pipelines/{p}.yaml"):
            _set(st, "1.3", "done", "already analysed")
            if gs is not None and gs.state == "waiting":
                for x in ("1.4", "1.5", "1.6"):
                    _set(st, x, "done", "before this run")
                return "1.7"
            return "1.4"
        return "1.3"
    first = FIRST_OF[stage]
    if gs is not None and gs.state == "waiting":
        for s_ in STEPS:
            if s_.stage == stage and s_.kind != "gate":
                _set(st, s_.id, "done", "before this run")
        gate_step = next(x for x in STEPS if x.stage == stage and x.kind == "gate")
        st["gate"] = {"phase": "approve"}
        return gate_step.id
    try:
        started = stage_started(s, p, cur_gate, states) if cur_gate in ORDER else False
    except Exception:  # noqa: BLE001
        started = False
    if started:
        _set(st, first, "done", "before this run")
        return _next_id(first) or first
    return first


# ------------------------------------------------------------------------------------------------------------ tick
def tick(s, p: str, states: dict) -> dict | None:
    """Advance the run by what happened since the last call. Cheap and idempotent: called every 2 s by the UI."""
    st = load(s.root, p)
    if not st or st["status"] in ("done", "stopped", "idle"):
        return st
    step = _cur(st)
    if step is None:
        return st
    row = st["steps"][step.id]
    if step.kind == "job" and row.get("status") == "running":
        jid = row.get("job_id")
        stt = jobs.status(s.root, jid) if jid else {"state": "unknown"}
        if stt.get("state") in ("running", "starting"):
            return st
        _job_ended(s, st, step, stt)
        if st["steps"][step.id]["status"] == "done":
            _next(st)
            st = _advance(s, p, states, st)
        return _save(s.root, st)
    if step.kind == "gate" and st["status"] == "waiting_gate":
        _gate_poll(s, p, states, st, step)
        if st["steps"][step.id]["status"] == "done":
            _next(st)
            st = _advance(s, p, states, st)
        return _save(s.root, st)
    if step.kind == "auto" and step.id == "6.4" and st["status"] == "waiting_gate":
        _record_pr(s, p, states, st)
        if st["steps"]["6.4"]["status"] == "done":
            _next(st)
            st = _advance(s, p, states, st)
        return _save(s.root, st)
    if step.kind == "review" and row.get("status") == "running" and row.get("job_id"):
        # 0.6.1: a job started from a review step (4.3 Regenerate / Run tests & build again, 5.2 Render & plan again).
        # 0.6.0 only finished job-kind steps, so these stayed "running" for ever.
        stt = jobs.status(s.root, row["job_id"])
        if stt.get("state") in ("running", "starting"):
            return st
        _review_job_ended(s, p, states, st, step, stt)
        if st["steps"][step.id]["status"] in DONE:
            _next(st)
            st = _advance(s, p, states, st)
        return _save(s.root, st)
    if st["status"] == "running" and step.kind in ("job", "gate", "auto"):
        return _save(s.root, _advance(s, p, states, st))   # e.g. a job that could not start last time
    return st


def _next(st: dict) -> None:
    prev = _cur(st)
    st["cursor"] = _next_id(st["cursor"])
    nxt = _cur(st)
    keep = {}
    if prev is not None and nxt is not None and prev.kind == nxt.kind == "gate" and prev.stage == nxt.stage:
        keep = {k: st["gate"][k] for k in ("pr", "head") if st["gate"].get(k)}   # 1.6 → 1.7 → 1.8: the same PR (0.6.1)
    st["gate"], st["sub"] = keep, {}


def _advance(s, p: str, states: dict, st: dict) -> dict:
    """Run every step that needs no one until the run has to wait (a job, an approver, the user) or ends."""
    for _ in range(len(STEPS) + 1):
        step = _cur(st)
        if step is None:
            st["status"], st["message"] = "done", "Live in Airflow — the workflow is complete"
            _event(s.root, st, "run_done")
            return st
        if st["steps"][step.id]["status"] in DONE:
            _next(st)
            continue
        sa = st.get("stop_after")
        if sa in STAGE_IDS and STAGE_IDS.index(step.stage) > STAGE_IDS.index(sa):   # 0.6.1: "run only S3"
            st["status"], st["stop_after"] = "stopped", None
            st["message"] = (f"stopped after {STAGE_TITLES[sa]} as you asked — Resume continues at {step.id} {step.title}, "
                             "or Run from any step")
            _event(s.root, st, "run_stopped", why=f"stop after {sa}", at=step.id)
            return st
        if jobs.current(s.root, p):                     # another job owns the pipeline: wait for it
            st["status"], st["message"] = "running", f"waiting for the running job of {p} to finish"
            return st
        handler = {"job": _start_job, "review": _pause_review, "input": _pause_input, "gate": _gate_step, "auto": _auto_step,
                   "checklist": _pause_checklist, "action": _pause_action}[step.kind]
        if not handler(s, p, states, st, step):          # False: the run must wait here
            return st
        if st["steps"][step.id]["status"] in DONE:
            _next(st)
    return st


def _msg(st: dict, step: Step, text: str) -> None:
    st["message"] = f"{STAGE_TITLES[step.stage]} · {step.id} {step.title} — {text}"


# ------------------------------------------------------------------------------------------------------------ jobs
def _job_args(s, p: str, st: dict, step: Step) -> dict | None:
    """The job's arguments, or None when the step is not applicable (it is skipped)."""
    read = _wt_read(s, p)
    if step.id == "1.5":
        return {} if st["sub"].get("requirement_changed") else None
    if step.id == "3.3":
        need = needs_ai_plan(read, p)
        return {} if need is not False else None
    if step.id in ("6.3", "6.5"):
        a = {"own_credentials": bool(st["answers"].get("own_credentials")), "params": st["answers"].get("params") or {},
             "max_packages": st["answers"].get("max_packages") or None}
        if st["sub"].get("planned"):
            a = {"own_credentials": a["own_credentials"], "planned": st["sub"]["planned"]}
        return a
    return {}


def _start_job(s, p: str, states: dict, st: dict, step: Step) -> bool:
    refresh = _refresh_start(s, p, states, st, step)      # 0.6.1: Run from / Reopen reached a stage that already started
    if refresh is True:
        _set(st, step.id, "done", "already started: the stage's work is on its branch")
        return True
    args = _job_args(s, p, st, step)
    if args is None:
        _set(st, step.id, "skipped", {"1.5": "the requirement did not change", "3.3": "no component needs an AI plan"}.get(step.id, "not needed"))
        return True
    action = step.action
    if refresh:
        action, args = refresh[0], {}
    if step.id in ("6.3", "6.5") and custom_delivery(s, p) and not st["sub"].get("planned"):
        action = "preview_run"                            # a move pipeline: preview first, then Move <n> files (6.3 confirm)
        args = {"max_packages": args.get("max_packages")}
    if step.id == "6.5" and not st["sub"].get("confirmed"):
        st["status"] = "waiting_user"
        _set(st, step.id, "waiting", "run the pipeline a second time: it must deliver nothing (Skip is fine)")
        _msg(st, step, "waiting for you: Run again or Skip")
        return False
    try:
        jid = jobs.start(s.root, p, action, args, f"{step.id} {step.title}")
    except jobs.JobError as exc:
        st["status"] = "running"
        _msg(st, step, f"could not start yet: {exc}")
        return False
    st["status"] = "running"
    _set(st, step.id, "running", "", job_id=jid, attempts=int(st["steps"][step.id].get("attempts") or 0) + 1)
    _msg(st, step, "running")
    _event(s.root, st, "step_started", step=step.id, title=step.title, job=jid, action=action)
    return False


def _job_ended(s, st: dict, step: Step, stt: dict) -> None:
    res = stt.get("result") or {}
    probs = [x for x in (res.get("problems") or []) if str(x).strip()]
    ok = stt.get("state") == "succeeded"
    if ok and not probs and res.get("severity") != "error":
        if step.id in ("6.3", "6.5") and "run_ok" not in res and res.get("planned"):
            return _preview_ready(s, st, step, res)
        _set(st, step.id, "done", "ok")
        _event(s.root, st, "step_done", step=step.id, job=stt.get("id"))
        _after_job(s, st, step, res)
        return
    if ok:
        why = probs[0] if probs else "finished with problems"
    else:
        why = res.get("error") or stt.get("state") or "failed"
    _set(st, step.id, "failed", str(why), can_continue=bool(ok), problems=probs[:10])
    st["status"] = "failed"
    _msg(st, step, f"failed: {str(why)[:160]}")
    _event(s.root, st, "step_failed", step=step.id, job=stt.get("id"), error=str(why)[:600], problems=probs[:10])


def _preview_ready(s, st: dict, step: Step, res: dict) -> None:
    """preview_run ended (a pipeline that moves or deletes files): the user confirms Move <n> files; the step waits.
    A preview with nothing planned at 6.5 is the no-op itself."""
    n = int(res.get("files") or 0)
    if step.id == "6.5" and n == 0:
        _set(st, step.id, "done", "no-op confirmed: the preview plans no file")
        _event(s.root, st, "step_done", step=step.id)
        return
    st["sub"]["preview"] = {k: res.get(k) for k in ("branch", "run", "planned", "files", "deletes", "by_prefix", "error_files",
                                                      "packages", "source_bucket", "run_id")}
    st["sub"]["planned"] = None
    _set(st, step.id, "waiting", f"preview ready: {n} file(s) planned, {res.get('deletes') or 0} source object(s) deleted — confirm Move")
    st["status"] = "waiting_user"
    _msg(st, step, f"waiting for you: Move {n} files, or preview again")


def _after_job(s, st: dict, step: Step, res: dict) -> None:
    """What a finished job's result means for the step."""
    if step.id in ("6.3", "6.5"):
        st["sub"] = {}                                   # the planned preview was used
    if step.id == "6.3" and res.get("run_ok") is False:
        _set(st, step.id, "failed", "the run failed: see its report.json and run.log", can_continue=False)
        st["status"] = "failed"
        _msg(st, step, "failed: the local run failed")
    if step.id == "6.5":
        n = res.get("rows")
        if not n:
            _set(st, step.id, "done", "no-op confirmed: nothing delivered")
        else:
            _set(st, step.id, "failed", f"not a no-op: {n} file(s) delivered — pause the pipeline and check its source", can_continue=True)
            st["status"] = "failed"
            _msg(st, step, "failed: the second run was not a no-op")


def _review_job_ended(s, p: str, states: dict, st: dict, step: Step, stt: dict) -> None:
    """The job a review step started (4.3, 5.2) ended. Success: 5.2 is done (the plan and the dry run were made again);
    4.3 looks at the components again (skipped when every one passes, else it waits with the new list). Failure: the
    step is red with the job's reason — Retry shows the step's buttons again, Continue anyway moves on."""
    res = stt.get("result") or {}
    probs = [x for x in (res.get("problems") or []) if str(x).strip()]
    ok = stt.get("state") == "succeeded" and not probs and res.get("severity") != "error"
    title = st["steps"][step.id].get("detail") or "the job"
    if ok:
        _event(s.root, st, "step_done", step=step.id, job=stt.get("id"), title=title)
        if step.id == "4.3":
            _set(st, step.id, "not_started", "")
            st["status"] = "running"
            _pause_review(s, p, states, st, step)
            return
        _set(st, step.id, "done", f"{title}: ok")
        st["status"] = "running"
        return
    why = (probs[0] if probs else None) or res.get("error") or stt.get("state") or "failed"
    _set(st, step.id, "failed", f"{title}: {why}", can_continue=True, problems=probs[:10])
    st["status"] = "failed"
    _msg(st, step, f"failed: {str(why)[:160]}")
    _event(s.root, st, "step_failed", step=step.id, job=stt.get("id"), error=str(why)[:600], problems=probs[:10])


# ------------------------------------------------------------------------------------------------------------ pauses
def _pause_review(s, p: str, states: dict, st: dict, step: Step) -> bool:
    if step.id == "3.4" and st["steps"]["3.3"]["status"] == "skipped":
        _set(st, step.id, "skipped", "no AI plans to review")
        return True
    if step.id == "4.3":
        bad = components_needing_work(s, p)
        if not bad:
            _set(st, step.id, "skipped", "every component passes its tests and the draft build is ok")
            return True
        _set(st, step.id, "waiting", f"{len(bad)} component(s) need work: " + ", ".join(f"{b.get('kind')} {b.get('name')}" for b in bad[:4]))
        st["status"] = "waiting_user"
        _msg(st, step, "waiting for you: Regenerate (AI) or Run tests & build again")
        return False
    if step.id == "5.2":
        last = st["steps"]["5.1"]
        if last.get("status") == "done" and not last.get("problems"):
            _set(st, step.id, "skipped", "the Terraform plan and the dry run are fine")
            return True
    _set(st, step.id, "waiting", step.help or "review, then Continue")
    st["status"] = "waiting_user"
    _msg(st, step, "waiting for your review")
    return False


def _pause_input(s, p: str, states: dict, st: dict, step: Step) -> bool:
    if step.id == "3.2":
        wt = s.worktree_path(p)
        left = unfilled(wt, p) if (wt / "pipelines").exists() else []
        if not left:
            _set(st, step.id, "done", "no environment values to fill in")
            return True
        _set(st, step.id, "waiting", f"{len(left)} value(s) still <REQUIRED>")
        st["status"] = "waiting_input"
        _msg(st, step, f"waiting for {len(left)} environment value(s)")
        return False
    if step.id == "6.1":
        if s.cfg.run_role_arn:
            _set(st, step.id, "done", f"run role {s.cfg.run_role_arn}")
            return True
        if s.strict or len(s.team.people) > 1:
            _set(st, step.id, "failed", "a run role is required (Setup → Run role ARN): approved code runs with at most the generated worker policy")
            st["status"] = "failed"
            _msg(st, step, "failed: a run role is required")
            return False
        if st["answers"].get("own_credentials"):
            _set(st, step.id, "done", "own AWS credentials (single-person dev team)")
            return True
        _set(st, step.id, "waiting", "no run role: confirm running with your own AWS credentials")
        st["status"] = "waiting_input"
        _msg(st, step, "waiting for you: confirm your own AWS credentials")
        return False
    _set(st, step.id, "waiting", step.help)
    st["status"] = "waiting_input"
    _msg(st, step, "waiting for your input")
    return False


def _pause_checklist(s, p: str, states: dict, st: dict, step: Step) -> bool:
    done = set(st["steps"][step.id].get("items_done") or [])
    if all(i in done for i in step.items):
        _set(st, step.id, "done", "all items done")
        return True
    _set(st, step.id, "waiting", f"{len(done)}/{len(step.items)} items done")
    st["status"] = "waiting_user"
    _msg(st, step, "waiting for you: tick the checklist")
    return False


def _pause_action(s, p: str, states: dict, st: dict, step: Step) -> bool:
    _set(st, step.id, "waiting", "click Download artifacts")
    st["status"] = "waiting_user"
    _msg(st, step, "waiting for you")
    return False


# ------------------------------------------------------------------------------------------------------------ gates
def _gate_of(step: Step) -> str:
    return STAGE_GATE[step.stage]


def _gate_step(s, p: str, states: dict, st: dict, step: Step) -> bool:
    g = _gate_of(step)
    if step.id == "3.6" and st["steps"]["3.5"].get("status") == "done":
        _set(st, step.id, "skipped", "G3 not needed")
        return True
    gs = states["by_gate"].get(g)
    if gs is not None and gs.done:
        _set(st, step.id, "done", "approved" if gs.state == "approved" else gs.state)
        return True
    if _gate_cannot_continue(s, p, states, st, step, g, gs):   # 0.6.1: merged but not approved, or blocked by an earlier gate
        return st["steps"][step.id]["status"] in DONE
    phase = st["gate"].get("phase") or step.phases[0]
    if phase not in step.phases:      # this step only covers some phases (1.6 / 1.7 / 1.8)
        _set(st, step.id, "done", phase)
        return True
    st["gate"]["phase"] = phase
    if phase == "request":
        if gs is not None and gs.state == "waiting":
            st["gate"]["phase"] = "approve"
        else:
            probs = _readiness(s, p, g)
            if probs:
                fix = fix_step_for(g, probs)
                hint = f" — go back to {fix} {BY_ID[fix].title} (Run from {fix})" if fix else ""
                _set(st, step.id, "failed", "not ready: " + "; ".join(probs[:3]) + hint, problems=probs[:10], fix_step=fix)
                st["status"] = "failed"
                _msg(st, step, "failed: not ready for approval")
                return False
            try:
                pr = s.request_approval(p, g)
            except StudioError as exc:
                _set(st, step.id, "failed", str(exc))
                st["status"] = "failed"
                _msg(st, step, f"failed: {exc}")
                return False
            s.invalidate()
            _event(s.root, st, "gate_requested", gate=g, pr=pr.get("number"), url=pr.get("html_url"))
            st["gate"]["phase"] = "approve"
            st["gate"]["pr"] = pr.get("number")
        if "approve" not in step.phases:
            _set(st, step.id, "done", f"PR #{st['gate'].get('pr') or ''} opened".replace("# ", "#"))
            return True
    if st["gate"]["phase"] == "approve":
        _set(st, step.id, "waiting", "waiting for the approvers")
        st["status"] = "waiting_gate"
        _msg(st, step, "waiting for approval on GitHub" + (" (Approve as me below)" if not s.strict else ""))
        if phase == "approve":                           # fresh states; right after the request they predate the PR
            _gate_poll(s, p, states, st, step)
        return st["steps"][step.id]["status"] in DONE
    if st["gate"]["phase"] == "merge":
        return _gate_merge(s, p, st, step, g)
    return False


def _readiness(s, p: str, g: str) -> list[str]:
    try:
        wt = s.worktree_path(p)
        if s.worktree_branch(p) != branch_for(p, g):
            return [f"the stage's work is not on {branch_for(p, g)} yet"]
        out = readiness(wt, p, g, base=s.base, dags_diff=s.dags_diff(p) if g == "G4" else None)
        if g == "G5" and not out:
            out += bucket_drift(s, p)        # 0.6.1: another pipeline changed a shared bucket stack meanwhile
        return out
    except Exception as exc:  # noqa: BLE001
        return [f"the readiness check failed: {exc}"]


def bucket_drift(s, p: str) -> list[str]:
    """Shared S3 notification stacks (infra/buckets/<b>/) whose OTHER pipelines' entries on the base branch differ from
    what this branch rendered: merging would revert or conflict with another pipeline's notifications. Render & plan
    again (5.2) takes the base branch's entries (0.6.1)."""
    wt = s.worktree_path(p)
    try:
        man = json.loads((wt / "infra" / p / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for b in man.get("buckets") or []:
        rel = f"infra/buckets/{b}/main.tf"
        base_text = s.read_base(rel)
        if base_text is None:
            continue
        try:
            mine = (wt / rel).read_text(encoding="utf-8")
        except OSError:
            mine = ""
        live = lambda k: s.read_base(f"infra/{k}/manifest.json") is not None   # noqa: E731 - a removed pipeline's leftover block
        theirs = {k: v for k, v in L.foreign_blocks(base_text, p).items() if live(k)}   # is dropped by the next render anyway
        ours = {k: v for k, v in L.foreign_blocks(mine, p).items() if live(k)}
        if theirs != ours:
            names = sorted(k for k in set(theirs) | set(ours) if theirs.get(k) != ours.get(k))
            out.append(f"the shared notification stack of bucket {b} changed on {s.base} since this branch rendered it "
                       f"(pipeline{'s' if len(names) != 1 else ''} {', '.join(names)})")
    return out


def _gate_poll(s, p: str, states: dict, st: dict, step: Step) -> None:
    """While a gate waits: approved → merge (or done when this step stops at approve); rejected → back to review.

    0.6.1: a PR that GitHub already merged while the gate is not approved (stale, merged without approval, reopened,
    blocked) stops the step with the reason and the fix instead of waiting for ever (the S5 5.4 loop); the states the UI
    passes may be up to 15 s old, so a PR the run itself just opened is polled at once (no 15 s pause per gate)."""
    g = _gate_of(step)
    gs = states["by_gate"].get(g)
    if gs is not None and gs.done:
        _set(st, step.id, "done", "approved")
        _event(s.root, st, "gate_merged", gate=g)
        return
    pr = _gate_pr(s, p, g, gs, st)
    if pr and pr.get("merged_at") and not (gs is not None and gs.state == "waiting"):
        _merged_but_not_approved(s, p, states, st, step, g, gs, pr)
        return
    if gs is not None and gs.state in ("in_progress", "not_started") and pr and pr.get("state") == "open":
        from .stages import GateState
        gs = GateState(g, "waiting", pr)               # the states predate the request: poll the PR the run opened
    if gs is not None and gs.state == "unknown":
        _set(st, step.id, "waiting", f"waiting: {gs.note or 'GitHub or the base branch cannot be read just now'}")
        return
    if gs is None or gs.state != "waiting" or not gs.pr:
        if gs is not None and gs.state in ("in_progress", "not_started") and st["gate"].get("pr") and _pr_closed(s, p, g, st["gate"]["pr"]):
            _rejected(s, p, st, step, g, gs.note or "the pull request was closed without merging")
        elif gs is not None and gs.state == "blocked":
            _gate_cannot_continue(s, p, states, st, step, g, gs)
        return
    try:
        res, full = s.gate_status(p, g)
    except Exception as exc:  # noqa: BLE001 - GitHub hiccup: try again next tick
        _set(st, step.id, "waiting", f"waiting for the approvers (GitHub: {str(exc)[:80]})")
        return
    head = (full.get("head") or {}).get("sha") if isinstance(full, dict) else None
    if head:
        st["gate"]["head"] = head
    if getattr(res, "blocked", False) and "reject" in (res.summary() or "").lower():
        _rejected(s, p, st, step, g, res.summary())
        return
    if not res.passed:
        posted = st["gate"].get("auto_posted")
        # 0.6.1: once per head — a new commit resets the approvals, so the head it approved no longer counts (at most 5)
        already = bool(posted) and (posted == head or (posted is True and not head))
        if st["auto_approve"] and not s.strict and not already and int(st["gate"].get("auto_count") or 0) < AUTO_APPROVE_MAX:
            try:
                s.post_approve(p, g)
                st["gate"]["auto_posted"] = head or True
                st["gate"]["auto_count"] = int(st["gate"].get("auto_count") or 0) + 1
                _event(s.root, st, "gate_approved", gate=g, how="auto-approve (dev)", head=head)
                s.invalidate()
            except Exception as exc:  # noqa: BLE001
                _set(st, step.id, "waiting", f"auto-approve failed: {str(exc)[:120]} — approve by hand")
            return
        _set(st, step.id, "waiting", res.summary()[:200] if hasattr(res, "summary") else "waiting for the approvers")
        return
    if not st["gate"].get("approved_seen"):
        st["gate"]["approved_seen"] = True
        _event(s.root, st, "gate_approved", gate=g, head=head)
    if "merge" not in step.phases:
        _set(st, step.id, "done", "approved")
        return
    st["gate"]["phase"] = "merge"
    _gate_merge(s, p, st, step, g)


AUTO_APPROVE_MAX = 5          # auto-approve comments per gate and run (a new head after each one)
CHECK_MISSING_MAX = 30       # polls (about a minute) that a missing pkf-gate check is waited for before merge() says why
MERGE_TRIES_MAX = 10          # merge attempts that fail with a network or GitHub error before the step turns red
FIX_STEP = {"G1": "1.4", "G2": "2.2", "G3": "3.2", "G4": "4.3", "G5": "5.2"}


def fix_step_for(g: str, problems: list[str]) -> str | None:
    """The step that fixes a gate's readiness problems (shown as Go back to … / Run from …)."""
    text = " ".join(problems).lower()
    if g == "G3":
        return "3.2" if "<required>" in text else "3.3"
    if g == "G4":
        return "4.3"
    if g == "G5":
        return "5.2" if any(w in text for w in ("dry run", "plan", "render", "bucket")) else "5.1"
    return FIX_STEP.get(g)


def _gate_pr(s, p: str, g: str, gs, st: dict) -> dict | None:
    """The gate's latest PR: the derived states' copy when they have one, else (states up to 15 s old, or a reopened gate
    that dropped its PR) the PR this run opened, from GitHub's PR list (cached 20 s)."""
    if gs is not None and gs.pr:
        return gs.pr
    number = st["gate"].get("pr")
    if not number:
        return None
    try:
        pr = s.prs(p).get(g)
    except Exception:  # noqa: BLE001 - GitHub unreachable: decide next tick
        return None
    return pr if pr and pr.get("number") == number else None


def _blocker(states: dict, g: str):
    """The first earlier gate that is not approved (what blocks `g`), or None."""
    order = [x.gate for x in states.get("gates") or []]
    for x in states.get("gates") or []:
        if x.gate == g or (g in order and order.index(x.gate) >= order.index(g)):
            break
        if not x.done:
            return x
    return None


REOPEN_HELP = {
    "stale": "what it approved has changed on the base branch since. Reopen {g} (it restarts the stage from the current base "
             "branch; the run continues from its first step), then approve it again",
    "merged_unapproved": "it was merged without the approval it needs. Approve PR #{n} on GitHub{dev}, or Reopen {g}",
    "in_progress": "{g} was reopened after PR #{n} merged. Run from {first} to make and approve it again",
    "not_started": "GitHub no longer lists its branch. Reopen {g}",
}


def _merged_but_not_approved(s, p: str, states: dict, st: dict, step: Step, g: str, gs, pr: dict) -> None:
    """GitHub merged the gate's PR, but the gate is not approved: stop with the reason and the fix (never wait for ever)."""
    n = pr.get("number")
    if gs is not None and gs.state in ("stale", "in_progress", "not_started", "merged_unapproved") and st["gate"].get("rechecked") != n:
        # derive() reads the last fetch of the base branch: a PR merged on GitHub (by hand, or a moment ago) is "stale"
        # until the merge is fetched. Fetch and derive once more before concluding anything (0.6.1 review).
        st["gate"]["rechecked"] = n
        fresh = _fresh_gate(s, p, g)
        if fresh is not None:
            gs = fresh
            if gs.done:
                _set(st, step.id, "done", "approved (merged on GitHub)")
                _event(s.root, st, "gate_merged", gate=g, pr=n, note="merged on GitHub")
                return
    state = gs.state if gs is not None else "unknown"
    if state == "unknown":
        _set(st, step.id, "waiting", f"{g} PR #{n} is merged; waiting until the base branch can be read again: {gs.note if gs else ''}"[:400])
        st["status"] = "waiting_gate"
        return
    if state == "blocked":
        _gate_cannot_continue(s, p, states, st, step, g, gs)
        return
    first = FIRST_OF.get(step.stage, step.id)
    why = (gs.note if gs is not None else "") or state
    fix = REOPEN_HELP.get(state, "Reopen {g}").format(g=g, n=n, first=first, dev=" (Post /approve as me)" if not s.strict else "")
    _set(st, step.id, "failed", f"{g} PR #{n} is merged, but the gate is {state.replace('_', ' ')} ({why}): {fix}",
         can_continue=False, gate_state=state, merged_pr=n, reopen=g if state in ("stale", "merged_unapproved", "not_started") else None,
         fix_step=first if state == "in_progress" else None)
    st["status"] = "failed"
    _msg(st, step, f"failed: {g} is merged but {state.replace('_', ' ')}")
    _event(s.root, st, "gate_merged_not_approved", gate=g, pr=n, state=state, why=str(why)[:300])


def _fresh_gate(s, p: str, g: str):
    """The gate's state after a forced fetch of the base branch (None when it cannot be had)."""
    try:
        fetch = getattr(s, "fetch", None)
        if fetch is not None:
            fetch(force=True)
        return _derive(s, p)["by_gate"].get(g)
    except Exception:  # noqa: BLE001 - GitHub or git trouble: decide from what we have
        return None


def _gate_cannot_continue(s, p: str, states: dict, st: dict, step: Step, g: str, gs) -> bool:
    """Before requesting or polling: a gate that cannot pass as it is stops the step with the reason (0.6.1). True when it
    stopped (or waits for a temporary reason)."""
    if gs is None:
        return False
    if gs.state in ("stale", "merged_unapproved"):
        pr = gs.pr or _gate_pr(s, p, g, gs, st) or {"number": st["gate"].get("pr")}
        _merged_but_not_approved(s, p, states, st, step, g, gs, pr)
        return True
    if gs.state == "in_progress" and (gs.note or "").startswith("reopened") and st["gate"].get("pr"):
        pr = _gate_pr(s, p, g, gs, st)     # the PR this run opened was merged, then the gate was reopened elsewhere
        if pr and pr.get("merged_at"):
            _merged_but_not_approved(s, p, states, st, step, g, gs, pr)
            return True
        return False
    if gs.state != "blocked":
        return False
    b = _blocker(states, g)
    if b is None:
        return False
    if b.state in ("unknown", "waiting"):     # temporary (GitHub) or its PR is open: wait for it
        _set(st, step.id, "waiting", f"waiting for {b.gate} ({b.state.replace('_', ' ')}{': ' + b.note if b.note else ''}) before {g}"[:400])
        # before the request the run keeps re-checking this step (status running → tick → _advance → _gate_step), so it
        # requests the PR once the blocker clears; after the request the poll (waiting_gate) does the same
        st["status"] = "waiting_gate" if st["gate"].get("phase") in ("approve", "merge") else "running"
        _msg(st, step, f"waiting for {b.gate} first")
        return True
    bstage = GATE_STAGE.get(b.gate)
    _set(st, step.id, "failed", f"{g} cannot be approved while {b.gate} is {b.state.replace('_', ' ')}"
         f"{' (' + b.note + ')' if b.note else ''}: Reopen {b.gate} (or Run from {FIRST_OF.get(bstage, '?')}), then continue",
         can_continue=False, gate_state="blocked", reopen=b.gate if b.state in ("stale", "merged_unapproved") else None,
         fix_step=FIRST_OF.get(bstage))
    st["status"] = "failed"
    _msg(st, step, f"failed: {b.gate} must be approved first")
    return True


def _merged_now(s, p: str, g: str, st: dict) -> dict | None:
    """GitHub's fresh answer: the gate's PR (the one this run opened, when known) is merged → that PR, else None."""
    try:
        s.invalidate()
        pr = s.prs(p, 0).get(g)
    except Exception:  # noqa: BLE001
        return None
    if not pr or not pr.get("merged_at"):
        return None
    number = st["gate"].get("pr")
    return pr if not number or pr.get("number") == number else None


def _pr_closed(s, p: str, g: str, number: int) -> bool:
    """GitHub says the gate's PR #number is closed without a merge (not merely not yet in a cached PR list)."""
    try:
        pr = s.prs(p, 0).get(g) or {}
    except Exception:  # noqa: BLE001
        return False
    return pr.get("number") == number and pr.get("state") == "closed" and not pr.get("merged_at")


def _gate_merge(s, p: str, st: dict, step: Step, g: str) -> bool:
    """Merge an approved gate. 0.6.1: in SIT/PPT/PRD a pending pkf-gate check waits (it runs tests and the build, minutes
    at G4/G5) instead of failing the step; a merge call that failed after GitHub merged (a timed-out request, or someone
    merged it meanwhile) counts as merged instead of retrying an impossible merge for ever."""
    head = st["gate"].get("head")
    if getattr(s, "strict", False) and head:
        try:
            chk = s.gate_check(head)
        except Exception as exc:  # noqa: BLE001
            chk = f"unknown: {exc}"
        missing = int(st["gate"].get("check_missing") or 0) + (1 if chk == "missing" else 0)
        st["gate"]["check_missing"] = missing
        if chk == "pending" or (chk == "missing" and missing <= CHECK_MISSING_MAX):
            _set(st, step.id, "waiting", f"approved; the required pkf-gate check is {chk} on {head[:7]} — the run merges when it is green")
            st["status"] = "waiting_gate"
            _msg(st, step, "approved — waiting for the pkf-gate check")
            return False
        if chk == "failure":
            _set(st, step.id, "failed", f"approved, but the required pkf-gate check failed on {head[:7]}: open the check on GitHub "
                 f"(PR #{st['gate'].get('pr') or '?'}), fix what it reports, then Retry", can_continue=False)
            st["status"] = "failed"
            _msg(st, step, "failed: the pkf-gate check failed")
            return False
    try:
        s.merge(p, g)
    except Exception as exc:  # noqa: BLE001 - decided below
        merged = _merged_now(s, p, g, st)
        if merged is not None:                     # GitHub merged it after all
            st["gate"]["pr"] = merged.get("number")
            _set(st, step.id, "done", "merged")
            _event(s.root, st, "gate_merged", gate=g, pr=merged.get("number"), note="the merge call failed, but GitHub merged it")
            return True
        if isinstance(exc, StudioError):
            text = str(exc)
            hint = _merge_hint(s, p, g, text)
            _set(st, step.id, "failed", f"merge refused: {text}{hint}", fix_step=FIX_STEP.get(g) if hint else None)
            st["status"] = "failed"
            _msg(st, step, f"failed: {exc}")
            return False
        tries = int(st["gate"].get("merge_tries") or 0) + 1
        st["gate"]["merge_tries"] = tries
        if tries >= MERGE_TRIES_MAX:
            _set(st, step.id, "failed", f"merge did not go through after {tries} tries: {str(exc)[:200]} — check GitHub and the proxy, then Retry")
            st["status"] = "failed"
            _msg(st, step, "failed: the merge did not go through")
            return False
        _set(st, step.id, "waiting", f"merge did not go through: {str(exc)[:120]} — trying again ({tries}/{MERGE_TRIES_MAX})")
        st["status"] = "waiting_gate"
        return False
    s.invalidate()
    _set(st, step.id, "done", "merged")
    _event(s.root, st, "gate_merged", gate=g, pr=st["gate"].get("pr"))
    return True


def _merge_hint(s, p: str, g: str, text: str) -> str:
    """Why GitHub refused a G5 merge, when the shared bucket stack changed on the base branch meanwhile (0.6.1)."""
    if g != "G5":
        return ""
    try:
        drift = bucket_drift(s, p)
    except Exception:  # noqa: BLE001
        drift = []
    return (" — " + "; ".join(drift) + ": go back to 5.2 Render & plan again (Run from 5.2)") if drift else ""


def _rejected(s, p: str, st: dict, step: Step, g: str, why: str) -> None:
    """The diagram's red edge: a rejected or closed PR sends the run back to the stage's review step."""
    back = REVIEW_OF.get(g)
    _event(s.root, st, "gate_rejected", gate=g, why=why[:300])
    for x in STEPS:
        if x.stage == step.stage and x.kind == "gate":
            _set(st, x.id, "not_started", "")
    if back:
        _set(st, back, "waiting", f"{g} rejected: {why[:160]} — fix, then Continue")
        st["cursor"], st["gate"] = back, {}
        st["status"] = "waiting_user"
        _msg(st, BY_ID[back], f"{g} was rejected: review again")


# ------------------------------------------------------------------------------------------------------------ auto
def _auto_step(s, p: str, states: dict, st: dict, step: Step) -> bool:
    if step.id == "3.5":
        need = needs_ai_plan(_wt_read(s, p), p)
        if need is False:
            _set(st, step.id, "done", "G3 not needed: no component needs an AI plan")
        else:
            _set(st, step.id, "skipped", "not applicable: a component has an AI plan, G3 is needed")
        return True
    if step.id == "6.4":
        return _record_pr(s, p, states, st)
    _set(st, step.id, "done")
    return True


def _record_pr(s, p: str, states: dict, st: dict) -> bool:
    run = states["by_gate"].get("RUN")
    if run is not None and run.state in ("done", "failed"):
        _set(st, "6.4", "done", "the run record is on the base branch" + (" (the run failed)" if run.state == "failed" else ""))
        return True
    if run is not None and run.state == "waiting" and run.pr:
        if not s.strict:
            try:
                s.merge(p, "RUN")
                s.invalidate()
                _set(st, "6.4", "done", "record PR merged")
                _event(s.root, st, "gate_merged", gate="RUN", pr=run.pr.get("number"))
                return True
            except Exception as exc:  # noqa: BLE001
                _set(st, "6.4", "waiting", f"record PR #{run.pr.get('number')}: {str(exc)[:120]}")
        else:
            _set(st, "6.4", "waiting", f"record PR #{run.pr.get('number')} merges when pkf-gate is green")
        st["status"] = "waiting_gate"
        _msg(st, BY_ID["6.4"], "waiting for the record PR")
        return False
    _set(st, "6.4", "waiting", "waiting for the record PR to appear on GitHub")
    st["status"] = "waiting_gate"
    _msg(st, BY_ID["6.4"], "waiting for the record PR")
    return False


# ------------------------------------------------------------------------------------------------------------ user
def answer(s, p: str, states: dict, step_id: str, values: dict | None = None) -> dict:
    """A pause's form was submitted (review Continue, env values, own credentials, a Move confirmation, …)."""
    st = load(s.root, p)
    if not st or st["cursor"] != step_id:
        return st or {}
    values = dict(values or {})
    step = BY_ID[step_id]
    if step_id == "1.4":
        text = values.get("requirement")
        if text is not None:
            from .ui.requirements import set_pipeline_fields

            wt = s.worktree_path(p)
            cur = _yaml(_wt_read(s, p), f"pipelines/{p}.yaml").get("requirement") or ""
            if text.strip() != cur.strip():
                set_pipeline_fields(wt, p, requirement=text.strip() + "\n")
                st["sub"]["requirement_changed"] = True
        _event(s.root, st, "input_collected", step=step_id, keys=["requirement"], changed=bool(st["sub"].get("requirement_changed")))
    elif step_id == "3.2":
        from .ui.components import write_values

        wt = s.worktree_path(p)
        for rel, vals in (values.get("files") or {}).items():
            write_values(wt / rel, {tuple(json.loads(k)) if k.startswith("[") else tuple(k.split(".")): v for k, v in vals.items()})
        left = unfilled(wt, p)
        _event(s.root, st, "input_collected", step=step_id, keys=sorted(values.get("files") or {}), left=len(left))
        if left:
            _set(st, step_id, "waiting", f"{len(left)} value(s) still <REQUIRED>")
            return _save(s.root, st)
    elif step_id == "6.1":
        st["answers"]["own_credentials"] = bool(values.get("own_credentials"))
        _event(s.root, st, "input_collected", step=step_id, keys=["own_credentials"])
    elif step_id in ("6.3", "6.5") and values.get("planned"):
        st["sub"]["planned"] = values["planned"]
        st["sub"]["confirmed"] = True
        _set(st, step_id, "not_started", "")
        st["status"] = "running"
        _event(s.root, st, "input_collected", step=step_id, keys=["move_confirmed"])
        return _save(s.root, _advance(s, p, states, st))
    elif step_id == "6.5":
        st["sub"]["confirmed"] = True
        st["answers"]["max_packages"] = values.get("max_packages", st["answers"].get("max_packages"))
        _set(st, step_id, "not_started", "")
        st["status"] = "running"
        return _save(s.root, _advance(s, p, states, st))
    elif step_id == "4.3" and values.get("job"):
        try:
            jid = jobs.start(s.root, p, values["job"], values.get("args") or {}, f"{step_id} {values.get('title') or values['job']}")
        except jobs.JobError as exc:
            st["message"] = str(exc)
            return _save(s.root, st)
        _set(st, step_id, "running", values.get("title") or values["job"], job_id=jid)
        st["status"] = "running"
        _msg(st, step, f"running {values.get('title') or values['job']}")
        _event(s.root, st, "step_started", step=step_id, job=jid, action=values["job"])
        return _save(s.root, st)
    elif step_id == "5.2" and values.get("job"):
        try:
            jid = jobs.start(s.root, p, "prepare_env", {}, f"{step_id} Render & plan again")
        except jobs.JobError as exc:
            st["message"] = str(exc)
            return _save(s.root, st)
        _set(st, step_id, "running", "Render & plan again", job_id=jid)
        st["status"] = "running"
        _event(s.root, st, "step_started", step=step_id, job=jid, action="prepare_env")
        return _save(s.root, st)
    if step_id in ("1.4", "2.2", "3.4", "4.2", "4.3", "5.2", "5.3", "3.2", "6.1", "7.2"):
        _set(st, step_id, "done", values.get("detail") or "continued")
        _event(s.root, st, "step_done", step=step_id)
        _next(st)
        st["status"] = "running"
        st = _advance(s, p, states, st)
    return _save(s.root, st)


def preview_again(s, p: str, states: dict, step_id: str = "6.3") -> dict:
    """6.3 / 6.5 "Preview again" (0.8.2: one function for the button and Full auto): forget the preview and run it anew."""
    st = load(s.root, p)
    if not st or st.get("cursor") != step_id or step_id not in ("6.3", "6.5"):
        return st or {}
    st["sub"] = {}
    st["steps"][step_id]["status"] = "not_started"
    st["status"] = "running"
    _save(s.root, st)
    return tick(s, p, states) or st


def mark_done(s, p: str, states: dict, step_id: str, item: str | None = None, done: bool = True) -> dict:
    """A checklist item (6.2, 7.3, 7.4) was ticked (or unticked)."""
    st = load(s.root, p)
    if not st:
        return {}
    step = BY_ID[step_id]
    items = set(st["steps"][step_id].get("items_done") or [])
    for it in ([item] if item else list(step.items)):
        (items.add if done else items.discard)(it)
    st["steps"][step_id]["items_done"] = [i for i in step.items if i in items]
    _event(s.root, st, "checklist_done", step=step_id, item=item, done=done, by=(s.me().name if s.me() else None))
    if st["cursor"] == step_id:
        if all(i in items for i in step.items):
            _set(st, step_id, "done", "all items done")
            _next(st)
            st["status"] = "running"
            st = _advance(s, p, states, st)
        else:
            _set(st, step_id, "waiting", f"{len(items)}/{len(step.items)} items done")
    return _save(s.root, st)


def retry(s, p: str, states: dict) -> dict:
    st = load(s.root, p)
    if not st or st["status"] != "failed":
        return st or {}
    step = _cur(st)
    _set(st, step.id, "not_started", "")
    if step.kind == "gate":
        st["gate"].pop("auto_posted", None)
        for k in ("head", "merge_tries", "check_missing", "rechecked"):   # 0.6.1: the next poll reads them again
            st["gate"].pop(k, None)
        if st["gate"].get("phase") == "merge" and "approve" in step.phases:
            st["gate"]["phase"] = "approve"          # approved again on the current head (a fix may have been pushed)
    st["status"] = "running"
    _event(s.root, st, "step_retried", step=step.id)
    return _save(s.root, _advance(s, p, states, st))


def skip(s, p: str, states: dict, why: str = "") -> dict:
    """Skip an optional step, or continue after a job that succeeded with warnings (can_continue)."""
    st = load(s.root, p)
    if not st:
        return {}
    step = _cur(st)
    row = st["steps"][step.id]
    if not (step.optional or row.get("can_continue")):
        st["message"] = f"{step.id} cannot be skipped"
        return _save(s.root, st)
    _set(st, step.id, "skipped" if step.optional and row.get("status") != "failed" else "done",
         why or ("continued despite the problems" if row.get("status") == "failed" else "skipped"))
    _event(s.root, st, "step_skipped", step=step.id, why=why)
    _next(st)
    st["status"] = "running"
    return _save(s.root, _advance(s, p, states, st))


def stop(s, p: str) -> dict:
    st = load(s.root, p)
    if not st:
        return {}
    st["status"] = "stopped"
    st["message"] = "stopped by you — Resume continues from the same step"
    _event(s.root, st, "run_stopped")
    return _save(s.root, st)


def resume(s, p: str, states: dict) -> dict:
    st = load(s.root, p)
    if not st:
        return {}
    step = _cur(st)
    if step is None:
        st["status"] = "done"
        return _save(s.root, st)
    row = st["steps"][step.id]
    if row.get("status") == "failed":
        st["status"] = "failed"
    elif row.get("status") == "running":
        st["status"] = "running"
    else:
        _set(st, step.id, "not_started", "")
        st["status"] = "running"
    _event(s.root, st, "run_resumed")
    return _save(s.root, _advance(s, p, states, st) if st["status"] == "running" else st)


def reset(s, p: str) -> None:
    """Forget the run (the pipeline itself is untouched): Run workflow starts again from the current gate."""
    flowstore.event(s.root, p, "run_reset")
    st = flowstore.load(s.root, p)
    if st:
        st["status"] = "idle"
        flowstore.save(s.root, p, st)


# ------------------------------------------------------------------------------------------------------------ navigation (0.6.1)
START_STEPS = ("2.1", "3.1", "4.1", "5.1")
# a stage's Start job when the stage's work is already on its branch (a reopened stage, or Run from its first step):
# S2/S3 need nothing more; S4 runs its tests and draft build again; S5 renders and plans the dev environment again.
REFRESH_ACTION = {"4.1": ("test_build", "Run tests & build again"), "5.1": ("prepare_env", "Render & plan again")}


def jump_check(s, p: str, states: dict, step_id: str) -> dict:
    """Can the run (re)start at `step_id` now? {"ok", "needs_reopen": gate | None, "problem": text | None,
    "suggest": step id | None}. Every gate before the step's stage must be approved (the approvals form a chain: a stage
    builds on the approved work of the one before it); a stage whose gate is already approved is run again by
    reopening that gate (Studio.reopen: the later stages restart from it)."""
    step = BY_ID.get(step_id)
    if step is None:
        return {"ok": False, "needs_reopen": None, "problem": f"there is no step {step_id}", "suggest": None}
    g = STAGE_GATE[step.stage]
    need = ORDER[:ORDER.index(g)] if g in ORDER else list(ORDER)
    for x in need:
        gs = states["by_gate"].get(x)
        if gs is None or not gs.done:
            first = FIRST_OF[GATE_STAGE[x]]
            state = (gs.state if gs is not None else "unknown").replace("_", " ")
            return {"ok": False, "needs_reopen": None, "suggest": first, "blocked_by": x,
                    "problem": f"{step_id} cannot run yet: {x} {STAGE_TITLES[GATE_STAGE[x]].split(' → ')[0]} is {state}"
                               f"{' (' + gs.note + ')' if gs is not None and gs.note else ''} and must be approved first — Run from {first}"}
    if g in ORDER:
        gs = states["by_gate"].get(g)
        if gs is not None and gs.done:
            return {"ok": True, "needs_reopen": g, "suggest": None,
                    "problem": f"{g} is {gs.state}: running {step_id} again reopens {g} — its stage restarts from the current base "
                               "branch, and the later stages (and their open pull requests) are set aside"}
        if gs is not None and gs.state in ("stale", "merged_unapproved"):
            return {"ok": True, "needs_reopen": g, "suggest": None,
                    "problem": f"{g} is {gs.state.replace('_', ' ')}: running {step_id} again reopens {g}"}
    return {"ok": True, "needs_reopen": None, "problem": None, "suggest": None}


def run_from(s, p: str, states: dict, step_id: str, *, stop_after: str | None = None, reopen: bool = False, why: str = "",
             by: str = "you", derive=None, hold: bool = False) -> dict:
    """Start (or move) the run at `step_id`: the steps from there on start again, the earlier ones keep what they did;
    `stop_after` (a stage id, e.g. "S3") stops the run when it reaches the next stage ("run only S3"). A stage whose gate
    is approved needs `reopen=True` (it reopens the gate first). Raises StudioError with the reason when it cannot."""
    if step_id not in BY_ID:
        raise StudioError(f"there is no step {step_id}")
    if stop_after is not None and stop_after not in STAGE_IDS:
        raise StudioError(f"there is no stage {stop_after}")
    if stop_after is not None and STAGE_IDS.index(stop_after) < STAGE_IDS.index(BY_ID[step_id].stage):
        raise StudioError(f"stop after {stop_after} is before {step_id}: choose {BY_ID[step_id].stage} or a later stage")
    if jobs.current(s.root, p):
        raise StudioError(f"a job is running for {p}: wait for it (or cancel it) first")
    chk = jump_check(s, p, states, step_id)
    if not chk["ok"]:
        raise StudioError(chk["problem"])
    if chk["needs_reopen"]:
        if not reopen:
            raise StudioError(chk["problem"] + " — confirm Reopen to go on")
        s.reopen(p, chk["needs_reopen"])
        states = derive() if derive else _derive(s, p)
        _event(s.root, {"pipeline": p}, "stage_reopened", gate=chk["needs_reopen"], by=by, why=why or f"Run from {step_id}")
    st = load(s.root, p)
    fresh = not st or st.get("status") == "idle"
    if fresh:
        st = _new_state(p, s.cfg.mode)
        st["started_at"] = _now()
        st["assistant_autopilot"] = not getattr(s, "strict", False)
        rows = derived_rows(states)
    ids = [x.id for x in STEPS]
    i = ids.index(step_id)
    for x in ids[:i]:
        row = st["steps"][x]
        if fresh:
            r = rows.get(x) or {}
            _set(st, x, r.get("status") if r.get("status") in DONE else "skipped", "before this run")
        elif row.get("status") not in DONE:
            _set(st, x, "skipped", f"jumped over (run from {step_id})")
    for x in ids[i:]:
        _set(st, x, "not_started", "")
        for k in ("jumped", "items_done", "can_continue", "problems", "fix_step", "reopen", "gate_state", "merged_pr", "job_id"):
            st["steps"][x].pop(k, None)
    st["steps"][step_id]["jumped"] = True
    st["cursor"], st["gate"], st["sub"] = step_id, {}, {}
    if st.get("auto"):          # 0.8.2: Full auto's S6 memory belongs to the run of S6 that is being redone
        from .autorun import forget_s6

        forget_s6(st)
    st["status"] = "running"
    st["stop_after"] = stop_after
    _event(s.root, st, "run_from", step=step_id, stop_after=stop_after, reopened=chk["needs_reopen"], by=by, why=why)
    if hold:      # the assistant changes files first (reopen → fix): Resume (or the assistant, once the fix is in) continues
        st["status"] = "stopped"
        st["message"] = f"held at {step_id} {BY_ID[step_id].title} while the assistant prepares a fix — Resume continues from here"
        return _save(s.root, st)
    return _save(s.root, _advance(s, p, states, st))


def reopen_stage(s, p: str, states: dict, gate: str, *, why: str = "", by: str = "you", derive=None, hold: bool = False) -> dict:
    """Reopen <gate> from the Workflow Developer: Studio.reopen (the stage restarts from the current base branch; later
    stages and their PRs are set aside as archive refs), then the run continues from the stage's first step (G1: the
    requirement review, 1.4)."""
    if gate not in ORDER:
        raise StudioError(f"there is no gate {gate}")
    target = "1.4" if gate == "G1" else FIRST_OF[GATE_STAGE[gate]]
    if jobs.current(s.root, p):
        raise StudioError(f"a job is running for {p}: wait for it (or cancel it) first")
    chk = jump_check(s, p, states, target)
    if not chk["ok"]:                                   # checked before anything is set aside (0.6.1 review)
        raise StudioError(f"Reopen {gate} would leave the run nowhere to go: {chk['problem']}")
    s.reopen(p, gate)
    fresh = derive() if derive else _derive(s, p)
    _event(s.root, {"pipeline": p}, "stage_reopened", gate=gate, by=by, why=why)
    return run_from(s, p, fresh, target, why=why or f"Reopen {gate}", by=by, derive=derive, hold=hold)


def _derive(s, p: str) -> dict:
    from .stages import derive

    try:
        s.invalidate()
    except Exception:  # noqa: BLE001
        pass
    return derive(s, p)


def _refresh_start(s, p: str, states: dict, st: dict, step: Step) -> tuple[str, str] | bool | None:
    """A Start step reached by Run from / Reopen when its stage's work is already on the branch: (action, title) of the
    job to run instead, True when nothing needs to run, None to run the Start job as usual."""
    if step.id not in START_STEPS or not st["steps"][step.id].get("jumped"):
        return None
    try:
        started = stage_started(s, p, STAGE_GATE[step.stage], states)
    except Exception:  # noqa: BLE001
        started = False
    if not started:
        return None
    return REFRESH_ACTION.get(step.id) or True


# ------------------------------------------------------------------------------------------------------------ assistant (0.6.1)
# What the assistant may make the run do on its own (when "Assistant can act" is on) or with one click. A person keeps
# every review, approval and merge decision, the environment values, the credential and Move confirmations, the
# checklists, and everything in S1 Requirements.
PEOPLE_STEPS = ("1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "2.2", "3.2", "3.4", "4.2", "5.3", "6.1", "6.2", "6.3",
                "6.5", "7.2", "7.3", "7.4")   # 6.3/6.5 apply Terraform and run (or Move) for real: never re-run by the assistant
ASSISTANT_ACTIONS = ("retry", "run_from", "reopen", "fix", "render_plan_again", "tests_build_again", "regenerate", "restore_dags")


def assistant_may(s, p: str, states: dict, st: dict | None, action: dict) -> str | None:
    """Why the assistant may NOT do `action` ({"do", "step", "gate", "kind", "name", "why"}), or None when it may."""
    do = (action or {}).get("do")
    if do not in ASSISTANT_ACTIONS:
        return f"unknown action {do!r}"
    if do == "run_from":
        step = BY_ID.get(str(action.get("step") or ""))
        if step is None:
            return "run_from needs a step id"
        if step.stage == "S1":
            return "S1 Requirements stays with people"
        chk = jump_check(s, p, states, step.id)
        if not chk["ok"]:
            return chk["problem"]
        over = people_skipped(st, step.id)
        if over:
            return f"it would jump over {', '.join(over)}, which a person does"
        return None
    if do == "reopen":
        g = action.get("gate")
        if g not in ORDER:
            return "reopen needs a gate (G2–G5)"
        if g == "G1":
            return "reopening G1 (the requirement) stays with people"
        gs = states["by_gate"].get(g)
        if gs is None or gs.state in ("not_started", "blocked", "unknown"):
            return f"{g} is {(gs.state if gs else 'unknown').replace('_', ' ')}: there is nothing to reopen"
        chk = jump_check(s, p, states, FIRST_OF[GATE_STAGE[g]])
        if not chk["ok"]:
            return chk["problem"]
        return None
    cur = _cur(st) if st else None
    if cur is None:
        return "the run has no current step"
    if cur.stage == "S1":
        return "S1 Requirements stays with people"
    if do == "retry":
        if (st or {}).get("status") != "failed":
            return "nothing has failed"
        if cur.id in PEOPLE_STEPS:
            return f"{cur.id} {cur.title} is a person's decision"
        return None
    if do == "render_plan_again":
        return None if cur.stage == "S5" else "Render & plan again belongs to S5"
    if do in ("tests_build_again", "regenerate", "restore_dags"):
        if cur.stage != "S4":
            return "it belongs to S4"
        if do == "regenerate" and not (action.get("kind") in L.FOLDERS and action.get("name")):
            return "regenerate needs the component's kind and name"
        return None
    return None   # fix: carried out by the assistant's fix phase, applied by the Studio


def people_skipped(st: dict | None, step_id: str) -> list[str]:
    """The person's steps (reviews, inputs, confirmations, checklists) before `step_id` that are not done: a run from
    `step_id` would jump over them."""
    if not st or step_id not in BY_ID:
        return []
    ids = [x.id for x in STEPS]
    rows = st.get("steps") or {}
    return [x for x in ids[:ids.index(step_id)] if x in PEOPLE_STEPS and (rows.get(x) or {}).get("status") not in DONE]


def assistant_act(s, p: str, states: dict, action: dict, derive=None) -> str:
    """Do one assistant action through the same functions the buttons use; returns what happened (one line). Raises
    StudioError when the action is not allowed or cannot be done now. Every action is in the journal (by: assistant)."""
    st = load(s.root, p)
    why = assistant_may(s, p, states, st, action)
    do = action.get("do")
    if why:
        _event(s.root, {"pipeline": p}, "assistant_action", do=do, ok=False, summary=f"refused {do}: {why}"[:300], action=action)
        raise StudioError(f"the assistant may not {do}: {why}")
    reason = str(action.get("why") or "")[:200]
    cur = _cur(st) if st else None
    if do == "retry":
        retry(s, p, states)
        msg = f"retried {cur.id if cur else ''}"
    elif do == "run_from":
        hold = bool(action.get("hold"))
        run_from(s, p, states, action["step"], reopen=True, by="assistant", why=reason, derive=derive, hold=hold)
        msg = f"the run {'is held' if hold else 'continues'} at {action['step']}"
    elif do == "reopen":
        hold = bool(action.get("hold"))
        reopen_stage(s, p, states, action["gate"], by="assistant", why=reason, derive=derive, hold=hold)
        msg = f"{action['gate']} reopened" + (" (the run waits for the fix)" if hold else "")
    elif do == "render_plan_again":
        if cur is not None and cur.id == "5.2":
            answer(s, p, states, "5.2", {"job": "prepare_env"})
        else:
            run_from(s, p, states, "5.1", by="assistant", why=reason, derive=derive)
        msg = "rendering and planning the dev environment again"
    elif do == "tests_build_again" and (cur is None or cur.id != "4.3"):
        run_from(s, p, states, "4.1", by="assistant", why=reason, derive=derive)   # S4 started: 4.1 runs tests & build again
        msg = "Run tests & build again"
    elif do in ("tests_build_again", "regenerate"):
        if cur is None or cur.id != "4.3":
            run_from(s, p, states, "4.3", by="assistant", why=reason, derive=derive)
        values = {"job": "test_build", "title": "Run tests & build again"}
        if do == "regenerate":
            args = {"kind": action["kind"], "name": action["name"], **({"step": action["step"]} if action.get("step") else {})}
            values = {"job": "code_component", "args": args, "title": f"Regenerate {action['kind']} {action['name']}"}
        st2 = load(s.root, p) or {}
        if st2.get("cursor") == "4.3" and (st2.get("steps", {}).get("4.3") or {}).get("status") != "running":
            answer(s, p, states, "4.3", values)
        msg = values["title"]
    elif do == "restore_dags":
        changed = s.restore_dags_from_base(p)
        msg = f"dags/ restored from the base branch ({len(changed)} file(s))"
    else:  # fix: the caller starts the assistant's fix phase
        msg = "the assistant prepares the fix"
    _event(s.root, {"pipeline": p}, "assistant_action", do=do, ok=True, summary=msg[:300], why=reason, action=action)
    return msg


def default_actions(diag: dict, states: dict | None = None) -> list[dict]:
    """The actions a diagnosis implies when it lists none (written before 0.6.1, or the model left them out)."""
    kind = diag.get("fix_kind")
    out: list[dict] = []
    if kind == "edit_files":
        g = diag.get("needs_reopen")
        if g and g != "G1" and states is not None and (states["by_gate"].get(g) is not None and states["by_gate"][g].done):
            out.append({"do": "reopen", "gate": g, "why": f"the fix changes what {g} approved"})
        out.append({"do": "fix", "why": "carry out the plan"})
    elif kind == "rerun":
        out.append({"do": "retry", "why": "a temporary failure"})
    elif kind == "studio_action":
        text = str(diag.get("studio_action") or diag.get("proposal") or "").lower()
        m = re.search(r"reopen\s+(g[1-5])", text)
        if m:
            out.append({"do": "reopen", "gate": m.group(1).upper(), "why": diag.get("studio_action") or ""})
        elif "restore dags" in text:
            out += [{"do": "restore_dags", "why": "dags/ differs from the base"}, {"do": "tests_build_again", "why": "check again"}]
        elif "render" in text and "plan" in text:
            out.append({"do": "render_plan_again", "why": diag.get("studio_action") or ""})
        elif "tests" in text and "build" in text:
            out.append({"do": "tests_build_again", "why": diag.get("studio_action") or ""})
        elif "retry" in text:
            out.append({"do": "retry", "why": diag.get("studio_action") or ""})
    return out


def back_to(s, p: str, states: dict, step_id: str, why: str = "") -> dict:
    """After Reopen G<n> from a review card: the run continues from that gate's review step."""
    st = load(s.root, p)
    if not st:
        return {}
    ids = [x.id for x in STEPS]
    for x in ids[ids.index(step_id):]:
        _set(st, x, "not_started", "")
    st["cursor"], st["gate"], st["sub"] = step_id, {}, {}
    st["status"] = "running"
    _event(s.root, st, "run_back", step=step_id, why=why)
    return _save(s.root, _advance(s, p, states, st))


# ------------------------------------------------------------------------------------------------------------ view
def _stage_status(rows: list[dict]) -> str:
    sts = [r["status"] for r in rows]
    if "running" in sts:
        return "running"
    if "failed" in sts:
        return "failed"
    if "waiting" in sts:
        return "waiting"
    if all(x in DONE for x in sts):
        return "skipped" if all(x == "skipped" for x in sts) else "done"
    return "not_started"


def derived_rows(states: dict) -> dict[str, dict]:
    """Without a run: what the gates say, so the tree still shows where the pipeline is (read-only)."""
    out = {}
    by = states["by_gate"]
    for s_ in STEPS:
        g = STAGE_GATE[s_.stage]
        gs = by.get(g) if g else None
        if gs is None:
            out[s_.id] = {"status": "not_started"}
        elif gs.done:
            out[s_.id] = {"status": "skipped" if gs.state == "skipped" else "done", "detail": gs.state}
        elif gs.state == "waiting" and s_.kind == "gate":
            out[s_.id] = {"status": "waiting", "detail": f"PR #{gs.pr['number']} open" if gs.pr else "PR open"}
        elif gs.state in ("waiting", "in_progress") and s_.kind != "gate":
            out[s_.id] = {"status": "done" if gs.state == "waiting" else "not_started", "detail": ""}
        elif gs.state == "failed":
            out[s_.id] = {"status": "failed", "detail": gs.note}
        else:
            out[s_.id] = {"status": "not_started", "detail": gs.note if gs.state in ("stale", "blocked", "merged_unapproved") else ""}
    return out


def snapshot(s, p: str, states: dict, st: dict | None, selected: str | None = None) -> dict:
    rows = st["steps"] if st and st.get("status") != "idle" else derived_rows(states)
    stages = []
    for sid in STAGE_IDS:
        steps = []
        for x in STEPS:
            if x.stage != sid:
                continue
            r = rows.get(x.id) or {"status": "not_started"}
            steps.append({"id": x.id, "title": x.title, "kind": x.kind, "status": r.get("status", "not_started"),
                          "detail": r.get("detail") or "", "started_at": r.get("started_at"), "ended_at": r.get("ended_at"),
                          "job_id": r.get("job_id"), "optional": x.optional})
        n_done = sum(1 for r in steps if r["status"] in DONE)
        stages.append({"id": sid, "title": STAGE_TITLES[sid], "status": _stage_status(steps), "progress": [n_done, len(steps)], "steps": steps})
    run = {"status": st["status"] if st else "idle", "current": st.get("cursor") if st else states.get("current"),
           "message": (st or {}).get("message") or f"current stage: {STAGES.get(states.get('current'), states.get('current'))}",
           "started_at": (st or {}).get("started_at"), "updated_at": (st or {}).get("updated_at"), "mode": s.cfg.mode}
    return {"run": run, "stages": stages, "selected": selected or run["current"]}


def console_lines(s, p: str, st: dict | None, step_id: str | None = None, n: int = 400) -> list[dict]:
    """The console: the workflow events (source flow), the selected (or current) step's job log (source job), the
    assistant transcripts (source assistant), merged in time order."""
    lines: list[dict] = []
    for e in flowstore.events(s.root, p, n=n):
        t = e.get("type", "")
        lvl = "error" if t.endswith("failed") or t == "gate_rejected" else "ok" if t.endswith(("done", "merged", "approved", "applied")) else \
            "agent" if t.startswith("assistant") else "info"
        text = _event_text(e)
        lines.append({"ts": _hms(e.get("ts")), "level": lvl, "source": "flow", "text": text, "_ts": e.get("ts") or ""})
    row = (st or {}).get("steps", {}).get(step_id or (st or {}).get("cursor") or "", {})
    jid = row.get("job_id")
    if jid:
        for ln in jobs.log(s.root, jid, tail=n).splitlines():
            lvl = "error" if ln.lstrip().startswith(("error:", "== failed", "== crashed")) or " error: " in ln else \
                "warn" if "⚠" in ln or "warning:" in ln else "ok" if "→ OK" in ln or ln.startswith("== done") else "info"
            lines.append({"ts": "", "level": lvl, "source": "job", "text": ln, "_ts": row.get("started_at") or "~"})
    lines.sort(key=lambda x: x["_ts"])
    for x in lines:
        x.pop("_ts", None)
    return lines[-n:]


def _hms(ts: str | None) -> str:
    try:
        return dt.datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S") if ts else ""
    except ValueError:
        return (ts or "")[11:19]


def _event_text(e: dict) -> str:
    t = e.get("type", "")
    step = f"{e['step']} " if e.get("step") else ""
    if t == "step_started":
        return f"▶ {step}{e.get('title') or ''} started (job {e.get('job', '')[:15]}, {e.get('action', '')})"
    if t == "step_done":
        return f"✓ {step}done"
    if t == "step_failed":
        return f"✗ {step}failed: {e.get('error', '')}"
    if t == "step_skipped":
        return f"– {step}skipped {e.get('why') or ''}".rstrip()
    if t == "gate_requested":
        return f"PR #{e.get('pr')} opened for {e.get('gate')}: {e.get('url', '')}"
    if t == "gate_approved":
        return f"{e.get('gate')} approved" + (f" ({e['how']})" if e.get("how") else "")
    if t == "gate_merged":
        return f"{e.get('gate')} merged" + (f" (PR #{e['pr']})" if e.get("pr") else "")
    if t == "gate_rejected":
        return f"{e.get('gate')} rejected: {e.get('why', '')}"
    if t == "input_collected":
        return f"{step}input: {', '.join(e.get('keys') or [])}"
    if t == "checklist_done":
        return f"{step}{'☑' if e.get('done', True) else '☐'} {e.get('item') or 'all items'}" + (f" — {e['by']}" if e.get("by") else "")
    if t.startswith("assistant"):
        return f"🤖 {t.replace('assistant_', 'assistant ')}: {e.get('summary') or e.get('root_cause') or e.get('question') or e.get('error') or ''}"[:400]
    if t == "run_from":
        return (f"run from {e.get('step')}" + (f" (stop after {e['stop_after']})" if e.get("stop_after") else "")
                + (f" — reopened {e['reopened']}" if e.get("reopened") else "") + (f" by {e['by']}" if e.get("by") else ""))
    if t == "stage_reopened":
        return f"{e.get('gate')} reopened" + (f" by {e['by']}" if e.get("by") else "") + (f": {e['why']}" if e.get("why") else "")
    if t == "gate_merged_not_approved":
        return f"{e.get('gate')} PR #{e.get('pr')} is merged but {str(e.get('state', '')).replace('_', ' ')}: {e.get('why', '')}"
    if t == "run_stopped" and e.get("why"):
        return f"workflow stopped: {e['why']}"
    if t in ("run_started", "run_done", "run_stopped", "run_resumed", "run_reset", "run_back", "step_retried"):
        return {"run_started": "workflow started", "run_done": "workflow complete: live in Airflow", "run_stopped": "workflow stopped",
                "run_resumed": "workflow resumed", "run_reset": "run forgotten", "run_back": f"back to {e.get('step')}",
                "step_retried": f"{step}retried"}[t]
    return f"{t} {json.dumps({k: v for k, v in e.items() if k not in ('ts', 'type')}, default=str)[:300]}"
