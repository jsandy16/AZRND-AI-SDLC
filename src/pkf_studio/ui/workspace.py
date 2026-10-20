"""The Workspace page's pieces (0.8.0, ST-3 v3 F): the stepper (click a stage to browse it), the stage header with Request
approval and ↩ Go back, the substage rail, *Before Request approval*, *Components*, *Files of this stage* and the console.

Layout and order live in workflow_dev.panel. Everything here is read-only or calls the same functions as 0.6.1/0.7.0 (the
run: flow.*; jobs: jobs.*; requests: workflow._request; the assistant: workflow_dev.* and assistant_ui). A card with fixed
widget keys (repair_card, scope_section) is rendered once per page; uncommitted changes and a diverged branch point to the
branch chip of the top bar, which renders those cards (shell.source_control)."""
from __future__ import annotations

import time

import streamlit as st

from .. import flow, jobs
from .. import ledger as L
from ..gates import GATES
from ..service import branch_for
from . import theme, wfd
from .common import (BUSY_HELP, Files, act, busy, crashed_text, elapsed, job_history, last_job_banner, last_line, local_time,
                     md_escape, since, start_job)

STAGE_NAMES = {"S1": "Requirements", "S2": "Design", "S3": "Plans", "S4": "Code & build", "S5": "Dev environment",
               "S6": "Run local", "S7": "Deploy to Airflow"}
STAGE_SUB = {"S1": "G1 · PO, BA, Architect", "S2": "G2 · DE", "S3": "G3 · DE", "S4": "G4 · DE", "S5": "G5 · Terraform, dry run",
             "S6": "record PR", "S7": "checklist"}
STAGE_LEAD = {
    "S1": "The AI analyst turns the requirement document into a finalized requirement, examples and pseudocode, then splits it "
          "into trigger, source, transformation and destination. The PO, BA and Architect approve it at G1.",
    "S2": "The split is reviewed: which components are new, which are reused, and their settings. A data engineer approves the "
          "design at G2.",
    "S3": "The environment values are filled in, then the AI plans every component: steps, pseudocode and test cases, simulated "
          "on the examples. A data engineer approves the plans at G3.",
    "S4": "The AI writes code for the custom steps one at a time, tests each in the hardened runner, then runs the end-to-end "
          "examples and a draft build. Nothing lands in dags/ until G4 approves.",
    "S5": "The approved code is built into dags/, Terraform renders and plans the dev resources, the policy checks run and the "
          "first run is dry-run. A data engineer approves it at G5.",
    "S6": "The approved Terraform plan is applied and the pipeline runs for real on this computer (Preview, then Move); the run "
          "record is merged as a PR.",
    "S7": "The build is checked, the artifacts downloaded, and the DAG, the runtime and the IAM policy deployed to Airflow by hand.",
}
GATE_NEXT = {"G1": "S2", "G2": "S3", "G3": "S4", "G4": "S5", "G5": "S6"}
KIND_WORD = {"gate": "GATE", "review": "REVIEW", "input": "INPUT", "checklist": "CHECKLIST", "action": "ACTION", "job": "JOB", "auto": "AUTO"}
STATUS_WORD = {"running": "RUNNING", "waiting": "WAITING", "failed": "FAILED", "done": "DONE", "skipped": "SKIPPED"}
CONSOLE_VIEWS = ["Console", "Workflow log", "Assistant transcript", "Approval history", "Jobs"]


def code(x) -> str:
    """Inline code in markdown: shown literally (no escaping inside backticks)."""
    return "`" + str(x).replace("`", "'") + "`"


# ------------------------------------------------------------------------------------------------ job-end refresh
def watch_job(p: str, jid: str | None, lane: str = "") -> None:
    """When the job this page was showing has ended: fresh stage data and PR list, and a rerun of the whole app — what the
    job panel's live log does on the other pages (common._live_log). Without it the page and the run's tick would keep
    using the gate states from before the job. `lane`: the assistant lane is watched the same way (its answer appears)."""
    key = f"_ws-job-{lane}{p}"
    prev = st.session_state.get(key)
    st.session_state[key] = jid
    if prev and prev != jid:
        st.session_state["_poll"] = st.session_state.get("_poll", 0) + 1
        st.session_state["_invalidate"] = True
        st.rerun(scope="app")


# ------------------------------------------------------------------------------------------------ small helpers
def _component_rows(f: Files, p: str) -> list[dict]:
    from .workflow import s4_status

    return [r for r in s4_status(f, p).get("components") or [] if isinstance(r, dict)]


def _regenerate(s, p: str, states: dict, st_: dict | None, args: dict, label: str, key: str, running: bool, primary: bool = True) -> None:
    """Regenerate (AI). While the run waits at 4.3 it goes through the run (flow.answer, as the 4.3 card's button), so the
    run notices the job's end; otherwise — a stopped run included, which must not be resumed by this — it starts the same
    job as the Workflow tab's S4 panel."""
    from .workflow_dev import _do

    if not st.button(label, key=key, type="primary" if primary else "tertiary", disabled=running, help=BUSY_HELP if running else
                     "the AI coder writes the step again in a background job, then the tests and the draft build run"):
        return
    title = f"Regenerate {args['kind']} {args['name']}" + (f" step {args['step']}" if args.get("step") else "")
    cur = (st_ or {}).get("cursor")
    if st_ and cur == "4.3" and st_.get("status") in flow.WAITING and ((st_.get("steps") or {}).get("4.3") or {}).get("status") == "waiting":
        _do(flow.answer, s, p, states, "4.3", {"job": "code_component", "args": args, "title": title}, ok=f"regenerating {args['kind']} {args['name']}")
        return
    start_job(s, p, "code_component", args, title)
    st.rerun()


def _test_build(s, p: str, states: dict, st_: dict | None, running: bool) -> None:
    """Run tests & build again: through the run at 4.3 (as its card), else the Workflow tab's job."""
    from .workflow_dev import _do

    if not st.button("Run tests & build again", key="ws-testbuild", disabled=running, help=BUSY_HELP if running else None):
        return
    if st_ and st_.get("cursor") == "4.3" and st_.get("status") in flow.WAITING:
        _do(flow.answer, s, p, states, "4.3", {"job": "test_build", "title": "Run tests & build again"}, ok="tests and build started")
        return
    start_job(s, p, "test_build", {}, "Run tests & build again")
    st.rerun()


def _cached(key: str, ttl: float, fn):
    """A value computed at most every `ttl` seconds per session (the Workspace refreshes every 2 s; git status and the
    branch comparison need not run that often). The key carries the poll and action counters, so a finished job, Refresh
    or any button (Commit, Discard, Take …) reads afresh."""
    base = f"_ws-cache-{key}-"
    full = f"{base}{st.session_state.get('_poll', 0)}-{st.session_state.get('_acts', 0)}"
    hit = st.session_state.get(full)
    now = time.monotonic()
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    for k in [k for k in st.session_state if isinstance(k, str) and k.startswith(base) and k != full]:
        del st.session_state[k]                                  # older counters: never read again
    st.session_state[full] = (now, value)
    return value


def _branch(s, p: str) -> str | None:
    """The worktree's branch (git), read at most every 2 s."""
    return _cached(f"wtb-{p}", 2, lambda: s.worktree_branch(p))


def _checklist(s, p: str, gate: str) -> list[str]:
    """The gate's unmet readiness items, only while the stage's work is on its branch (before that every item would read
    'not on the branch yet'). Cached 10 s per (pipeline, gate, poll)."""
    from .workflow import checklist

    if _branch(s, p) != branch_for(p, gate):
        return []
    return _cached(f"chk-{p}-{gate}", 10, lambda: checklist(s, p, gate))


def _needs_repair(s, p: str) -> bool:
    wt = s.worktree_path(p)
    if not wt.is_dir():
        return False
    try:
        return not (wt / ".git").exists() or s.worktree_branch(p) is None
    except Exception:  # noqa: BLE001
        return True


def cursor_stage(st_: dict | None, states: dict) -> str:
    cur = flow.BY_ID.get((st_ or {}).get("cursor") or "")
    if cur:
        return cur.stage
    return {v: k for k, v in flow.STAGE_GATE.items() if v}.get(states.get("current")) or "S1"


def shown_stage(p: str, st_: dict | None, states: dict) -> str:
    """The stage the page shows: the one clicked in the stepper (Q4), else the run's. A click is forgotten when the run
    moves to another stage, so the page follows the run again."""
    run_stage = cursor_stage(st_, states)
    pick = st.session_state.get(f"ws-stage-{p}")
    if pick and pick[1] == run_stage and pick[0] in flow.STAGE_IDS:
        return pick[0]
    return run_stage


def _pick_stage(p: str, stage: str, run_stage: str) -> None:
    st.session_state[f"ws-stage-{p}"] = (stage, run_stage)


def _pick_step(p: str, step_id: str) -> None:
    """A rail card clicked: the console shows its log and ↩ Go back is pre-filled with it (as the 0.7.0 tree click)."""
    st.session_state[f"wfd-sel-{p}"] = step_id
    st.session_state[f"wfd-nav-step-{p}"] = f"{step_id} {flow.BY_ID[step_id].title}"


def snapshot_rows(snap: dict) -> dict:
    return {x["id"]: x for stg in snap["stages"] for x in stg["steps"]}


# ------------------------------------------------------------------------------------------------ stepper
def stepper(s, p: str, st_: dict | None, states: dict, snap: dict) -> str:
    """The 7 stages: progress bar, name (click to browse), gate and state. Returns the stage shown."""
    run_stage = cursor_stage(st_, states)
    shown = shown_stage(p, st_, states)
    with st.container(key="ws-stepper"):
        cols = st.columns(len(snap["stages"]), gap="small")
        for col, stg in zip(cols, snap["stages"]):
            sid = stg["id"]
            done, total = stg["progress"]
            status = stg["status"]
            gate = flow.STAGE_GATE.get(sid)
            gs = states["by_gate"].get(gate) if gate else None
            gate_ok = bool(gs is not None and gs.state in ("approved", "skipped"))
            complete = status == "done" or (gate_ok and not st_)
            running = [x for x in stg["steps"] if x["status"] in ("running", "waiting", "failed")]
            pct = 100 if complete else int(100 * done / total) if total else 0
            color = "#21A353" if complete else "#E5484D" if status == "failed" else theme.BLUE if running else "#C9CED6"
            with col:
                st.html(f'<div class="pkf-bar"><i style="width:{max(pct, 0)}%;background:{color}"></i></div>')
                mark = "✓ " if complete else "● " if running else ""
                st.button(f"{mark}{sid if sid != 'S7' else '7'} {STAGE_NAMES[sid]}", key=f"ws-stage-{sid}", type="tertiary",
                          on_click=_pick_stage, args=(p, sid, run_stage), help=f"show the substages of {sid}")
                if running:
                    r = running[0]
                    t = f" · {elapsed(since(r['started_at']))}" if r.get("started_at") and r["status"] in ("running", "waiting") else ""
                    sub = f"{r['id']} {r['status']}{t}"
                    st.html(f'<div class="pkf-sub" style="color:{"#B42318" if r["status"] == "failed" else theme.BLUE}">{theme.esc(sub)}</div>')
                else:
                    sub = STAGE_SUB[sid] + (" ✓" if gate_ok else "")
                    st.html(f'<div class="pkf-sub"{" style=\"font-weight:700;color:" + theme.BLUE + "\"" if sid == shown else ""}>'
                            f'{theme.esc(sub)}{" · shown" if sid == shown and sid != run_stage else ""}</div>')
    return shown


# ------------------------------------------------------------------------------------------------ stage header
def stage_header(s, p: str, states: dict, st_: dict | None, snap: dict, stage: str, open_items: int) -> None:
    from .workflow_dev import navigator_body

    total = sum(x["progress"][1] for x in snap["stages"])
    done = sum(x["progress"][0] for x in snap["stages"])
    n = flow.STAGE_IDS.index(stage) + 1
    left, right = st.columns([7, 3], vertical_alignment="top")
    with left:
        st.html(f'<div class="pkf-eyebrow">STAGE {n} OF {len(flow.STAGE_IDS)} · {done} / {total} SUBSTAGES DONE</div>'
                f'<div class="pkf-h1">{theme.esc(STAGE_NAMES[stage])}</div><div class="pkf-lead">{theme.esc(STAGE_LEAD[stage])}</div>')
    with right:
        request_button(s, p, states, st_, stage, open_items)
        with st.popover("↩ Go back · run a stage · reopen", key=f"ws-goback-{p}", width="stretch",
                        help="run from any step, run a single stage (stop after it), or reopen a stage"):
            try:
                navigator_body(s, p, states, st_, st.session_state.get(f"wfd-sel-{p}"), inline_reopen=True)
            except Exception as exc:  # noqa: BLE001 - navigation trouble never takes the page down
                st.caption(f"navigation unavailable: {exc}")


def request_button(s, p: str, states: dict, st_: dict | None, stage: str, open_items: int) -> None:
    """Request approval G<n> with 0.7.0's P1 rules: hidden while the run sits on the gate step (it opened the PR; the PR
    link shows instead), disabled while a job runs or an item is open, "Update the PR" when the PR is open."""
    from .workflow import _request

    gate = flow.STAGE_GATE.get(stage)
    if gate not in GATES:
        return
    gs = states["by_gate"].get(gate)
    state = gs.state if gs is not None else "not_started"
    pr = (gs.pr or {}) if gs is not None else {}
    if state in ("approved", "skipped"):
        st.html(f'<div style="text-align:right">{theme.chip(f"{gate} {state}", "green")}</div>')
        if pr.get("html_url"):
            st.link_button(f"PR #{pr['number']} on GitHub", pr["html_url"], width="stretch")
        return
    cur = flow.BY_ID.get((st_ or {}).get("cursor") or "")
    on_gate_step = bool(cur and cur.kind == "gate" and cur.stage == stage)
    if on_gate_step and (pr.get("html_url") or (st_ or {}).get("status") != "stopped"):
        if pr.get("html_url"):
            st.link_button(f"PR #{pr['number']} on GitHub · {gate} waits for approval", pr["html_url"], type="primary", width="stretch")
        else:
            st.caption(f"the run is requesting {gate}")
        return
    if _branch(s, p) != branch_for(p, gate):
        st.button(f"Request approval {gate}", key=f"ws-req-{gate}-{p}", type="primary", disabled=True, width="stretch",
                  help=f"the run requests {gate} when the stage's work is ready")
        return
    running = busy(s, p)
    items = _checklist(s, p, gate)
    label = "Update the PR" if state == "waiting" else f"Request approval {gate}"
    active = bool(st_ and st_.get("status") in ("running", "waiting_input", "waiting_gate", "waiting_user"))
    tip = BUSY_HELP if running else ("finish the open items in Before Request approval first" if items else
                                     (f"optional: the run requests {gate} by itself at its gate step" if active else None))
    if st.button(label, key=f"ws-req-{gate}-{p}", type="primary", disabled=running or bool(items), help=tip, width="stretch"):
        act(_request, s, p, gate, ok=lambda pr_: f"PR #{pr_['number']}: {pr_['html_url']}")
    if open_items:
        st.html(f'<div class="pkf-muted" style="text-align:right">{open_items} thing{"s" if open_items != 1 else ""} still needed ↓</div>')


# ------------------------------------------------------------------------------------------------ substage rail
def rail(s, p: str, st_: dict | None, snap: dict, stage: str) -> None:
    """One card per substage of the stage shown, in one row that scrolls sideways. The running card carries the job's time
    and Cancel; a click on a card's title selects it (console + Go back)."""
    stg = next(x for x in snap["stages"] if x["id"] == stage)
    steps = stg["steps"]
    todo = sum(1 for x in steps if x["status"] not in ("done", "skipped"))
    running = sum(1 for x in steps if x["status"] in ("running", "waiting"))
    st.html(f'<div style="display:flex;gap:12px;align-items:center;margin-top:.3rem"><span class="pkf-eyebrow" style="font-size:.78rem">SUBSTAGES</span>'
            f'{theme.chip(str(len(steps)), "grey")}' + (f'<span style="font-size:.8rem;color:{theme.BLUE};font-weight:600">{running} active</span>' if running else "")
            + f'<span class="pkf-muted">· {todo} to go</span></div>')
    jid = jobs.current(s.root, p)
    sel = st.session_state.get(f"wfd-sel-{p}") or (st_ or {}).get("cursor")
    first_todo = next((x["id"] for x in steps if x["status"] == "not_started"), None)
    hl = [x["id"].replace(".", "-") for x in steps if x["status"] in ("running", "waiting", "failed") or x["id"] == sel]
    if hl:                                     # the active / selected cards get the blue (failed: red) frame
        css = "".join(f".st-key-ws-card-{h}{{border:2px solid {'#E5484D' if x['status'] == 'failed' else theme.BLUE} !important;}}"
                      for h, x in ((x["id"].replace(".", "-"), x) for x in steps) if h in hl)
        st.html(f"<style>{css}</style>")
    with st.container(horizontal=True, key="ws-rail", gap="small"):
        for x in steps:
            on = x["status"] in ("running", "waiting", "failed") or x["id"] == sel
            with st.container(border=True, width=320, key=f"ws-card-{x['id']}"):
                word = STATUS_WORD.get(x["status"]) or ("NEXT" if x["id"] == first_todo else
                                                       "OPTIONAL" if x.get("optional") else KIND_WORD.get(x["kind"], ""))
                cls = {"running": "on", "waiting": "wait", "failed": "fail", "done": "ok"}.get(x["status"], "on" if on else "")
                dot = {"running": "blue", "waiting": "orange", "failed": "red", "done": "green"}.get(x["status"], "grey")
                st.html(f'<div class="pkf-kind {cls}"><span class="pkf-dot {dot}"></span>{x["id"]} · {word}</div>')
                st.button(x["title"], key=f"ws-pick-{x['id']}", type="tertiary", on_click=_pick_step, args=(p, x["id"]),
                          help="show this substage's log in the console and use it in ↩ Go back")
                detail = x.get("detail") or ""
                if x["status"] in ("running", "waiting") and x.get("started_at"):
                    detail = (detail + " · " if detail else "") + elapsed(since(x["started_at"]))
                st.html(f'<div class="pkf-sub">{theme.esc(detail[:120]) if detail else "&nbsp;"}</div>')
                if x["status"] == "running" and jid and x.get("job_id") == jid:
                    stt = jobs.status(s.root, jid) or {}
                    if stt.get("action") in getattr(jobs, "UNCANCELLABLE", ()):  # E17: it may be applying Terraform
                        st.caption("may be applying Terraform: wait for it")
                    elif st.button("Cancel", key=f"cancel-{jid}", help="stops the job; what it did so far is committed"):
                        act(jobs.cancel, s.root, jid)
    if jid and not any(x.get("job_id") == jid and x["status"] == "running" for x in steps):
        stt = jobs.status(s.root, jid) or {}      # a job no step of this stage owns (another page's button, another stage)
        c = st.columns([8, 2], vertical_alignment="center")
        c[0].html(f'<span class="pkf-spin"></span><b>{theme.esc(stt.get("title") or stt.get("action") or "job")}</b> '
                  f'<span class="pkf-muted">· {elapsed(since(stt.get("started_at")))} · {theme.esc(last_line(jobs.log(s.root, jid, tail=20))[:140])}</span>')
        if stt.get("action") in getattr(jobs, "UNCANCELLABLE", ()):
            c[1].caption("may be applying Terraform: wait for it")
        elif c[1].button("Cancel", key=f"cancel-{jid}", help="stops the job; what it did so far is committed"):
            act(jobs.cancel, s.root, jid)


# ------------------------------------------------------------------------------------------------ Before Request approval
def gate_checks(s, p: str, gate: str | None, stage: str) -> list[tuple[bool, str, str]]:
    """What the Studio knows to be passed for the gate (✓ rows): G4's draft build, examples and dags/ from
    review/<p>/tests.json, and "no job running". (passed, title, sub). Read-only; open items come from readiness."""
    out: list[tuple[bool, str, str]] = []
    if gate == "G4":
        t = Files(s, p).json(f"review/{p}/tests.json")
        if t:
            out.append((bool(t.get("build_ok")), "Draft build passes", "every check of the real build · writes nothing"))
            ex = [e for e in t.get("examples") or [] if isinstance(e, dict)]
            bad = [e for e in ex if e.get("status") not in ("pass", "shown")]
            out.append((not bad and bool(t.get("examples_ok", True)), "End-to-end examples pass",
                        f"{len(bad)} of {len(ex)} failing" if bad else f"{len(ex)} example(s)"))
            dags_bad = any("dags/" in str(x) for x in t.get("problems") or [])
            out.append((not dags_bad, "dags/ on the branch matches the base", "built again at S5 from approved code"))
    return out


def gate_card(s, p: str, states: dict, st_: dict | None, stage: str, warnings: list[str]) -> int:
    """Before Request approval: ✓ what passed, ○ what is open — the gate's readiness items with their fix, a failing
    component (Regenerate), a running or crashed job, the run or the assistant stuck, uncommitted changes, a diverged
    branch, GitHub offline, a broken worktree (Repair). Returns the number of open items."""
    from .common import offline_error
    from .workflow import repair_card, scope_section

    gate = flow.STAGE_GATE.get(stage)
    gate = gate if gate in GATES else None
    gs = states["by_gate"].get(gate) if gate else None
    f = Files(s, p)
    running = busy(s, p)
    jid = jobs.current(s.root, p)
    rows: list[tuple] = []        # (kind "ok"/"open"/"bad"/"card", title md, sub md, action or None)
    for ok, title, sub in gate_checks(s, p, gate, stage):
        rows.append(("ok" if ok else "open", title, sub, None))
    if f.live and (stage == "S4" or stage == cursor_stage(st_, states)):      # 0.7.0: listed whatever the run's stage
        for r in _component_rows(f, p):
            kind, name, cstage = r.get("kind"), r.get("name"), r.get("stage")
            failing = [x for x in r.get("failing") or [] if isinstance(x, dict)]
            steps = list(r.get("custom_steps") or [])
            if cstage == "code_failing":
                bad = [i for i in steps if any(x.get("step") == i for x in failing)] or steps[:1]
                first = failing[0] if failing else {}
                case = first.get("case") or first.get("example") or ""
                sub = (f"case {code(case)}: " if case else "") + md_escape(str(first.get("detail") or "")[:160])
                if bad:
                    a = {"kind": kind, "name": name, "step": bad[0]}
                    rows.append(("bad", f"{kind} {code(name)} fails its tests", sub,
                                 lambda a=a, k=f"ws-regen-{kind}-{name}-{bad[0]}", lb=f"Regenerate {bad[0]}": _regenerate(s, p, states, st_, a, lb, k, running)))
                else:
                    a = {"kind": kind, "name": name}
                    rows.append(("bad", f"{kind} {code(name)} fails its tests", sub,
                                 lambda a=a, k=f"ws-regen-{kind}-{name}": _regenerate(s, p, states, st_, a, "Regenerate (AI)", k, running)))
            elif cstage == "plan_approved":
                a = {"kind": kind, "name": name}
                rows.append(("open", f"{kind} {code(name)}: code not written yet", "the AI coder writes it in a background job",
                             lambda a=a, k=f"ws-regen-{kind}-{name}": _regenerate(s, p, states, st_, a, "Regenerate (AI)", k, running)))
    if gate and gs is not None and gs.state not in ("approved", "skipped", "waiting", "merged_unapproved") and f.live:
        scope_shown = False
        for item in _checklist(s, p, gate):
            if "outside pipeline" in item and not scope_shown:          # its button (fixed key) once
                scope_shown = True
                rows.append(("open", "Files outside this pipeline are on the branch", md_escape(item), lambda: scope_section(s, p, running)))
            else:
                head, _, fix = item.partition(" → ")
                rows.append(("open", md_escape(head), ("fix: " + md_escape(fix)) if fix else "", None))
    if jid:
        stt = jobs.status(s.root, jid) or {}
        rows.append(("open", "No job running", md_escape(f"{stt.get('title') or stt.get('action') or 'a job'} is still running · wait or Cancel"), None))
    elif gate == "G4":
        rows.append(("ok", "No job running", "", None))
    if not jid:
        recent = jobs.recent(s.root, p, 1)
        last = recent[0] if recent else None
        res = (last or {}).get("result") or {}
        probs = [x for x in (res.get("problems") or []) if str(x).strip()] if isinstance(res, dict) else []
        if last and last.get("state") == "crashed":
            rows.append(("card", "", "", lambda: st.error(f"Last job: **{md_escape(last.get('title') or last.get('action') or '')}** — "
                                                         "crashed\n\n" + crashed_text(s, last), icon="💥")))
        elif last and last.get("state") not in ("running", "starting") and (last.get("state") != "succeeded" or probs):   # ending now
            rows.append(("card", "", "", lambda: last_job_banner(last)))
    for w in warnings:
        rows.append(("bad", md_escape(w), "", None))
    pl = (st_ or {}).get("pilot") or {}
    if (st_ or {}).get("assistant_autopilot") and pl.get("phase") in ("needs_you", "gave_up"):
        rows.append(("open", "The assistant needs you", md_escape(str(pl.get("note") or "")) + " — open the assistant (sidebar)", None))
    if f.live and not running:
        try:
            changed = _cached(f"changed-{p}", 10, lambda: s.changed_files(f.wt))
        except Exception:  # noqa: BLE001
            changed = []
        if changed:
            rows.append(("open", f"{len(changed)} uncommitted change(s) on {code(_branch(s, p) or '')}",
                         "Commit or Discard them in the branch chip at the top", None))
        if gate:
            try:
                sync = _cached(f"sync-{p}-{gate}", 10, lambda: s.branch_sync(p, gate))
            except Exception:  # noqa: BLE001
                sync = {}
            if sync.get("state") == "diverged":
                rows.append(("bad", "GitHub's copy of the branch changed",
                             f"{sync.get('ahead')} commit(s) here, {sync.get('behind')} on GitHub: Take / Merge / Overwrite in the branch chip at the top", None))
    if offline_error(s):
        rows.append(("open", "GitHub unreachable", "the banner above says why; the page shows this computer's copy", None))
    if _cached(f"repair-{p}", 10, lambda: _needs_repair(s, p)):
        rows.append(("card", "", "", lambda: repair_card(s, p)))
    au = (st_ or {}).get("auto") or {}
    if (st_ or {}).get("full_auto") and au.get("needs_you"):
        rows.insert(0, ("bad", "Run without stopping has stopped", md_escape(str(au["needs_you"])), None))
    n_open = sum(1 for r in rows if r[0] in ("open", "bad", "card"))
    with st.container(border=True, key="ws-gatecard"):
        title = f"Before Request approval {gate}" if gate else "What needs attention"
        st.html(f'<div style="display:flex;justify-content:space-between;align-items:center"><b style="font-size:1rem">{theme.esc(title)}</b>'
                + (theme.chip(f"{n_open} open", "amber") if n_open else theme.chip("all clear", "green")) + "</div>")
        if not rows:
            st.caption("Nothing blocks this stage right now." if gate else "Nothing needs you right now.")
        for kind, title_md, sub, action in rows:
            if kind == "card":
                action()
                continue
            icon = '<span class="pkf-check">✓</span>' if kind == "ok" else f'<span class="pkf-ring{" red" if kind == "bad" else ""}"></span>'
            c = st.columns([6.5, 3.5], vertical_alignment="center")
            c[0].markdown(f"{icon}{title_md}" + (f'  \n<span class="pkf-muted" style="margin-left:1.6rem">{sub}</span>' if sub else ""),
                          unsafe_allow_html=True)
            if action is not None:
                with c[1]:
                    action()
    return n_open


# ------------------------------------------------------------------------------------------------ Components (and the stage's own panel)
def components_card(s, p: str, states: dict, st_: dict | None, stage: str) -> None:
    """S4: every component with its state and action; the other stages: their own summary (S1 open questions, S2 split,
    S3 plans, S5 Terraform / policy / dry run, S6 the last run, S7 the deploy notes)."""
    from .common import plan_cards

    f = Files(s, p)
    running = busy(s, p)
    with st.container(border=True, key="ws-components"):
        if stage == "S4":
            c = st.columns([6, 4], vertical_alignment="center")
            c[0].html('<b style="font-size:1rem">Components</b>')
            with c[1]:
                _test_build(s, p, states, st_, running)
            rows = _component_rows(f, p)
            comps = rows or [{"kind": k, "name": n, "stage": ""} for k, n in f.components()]
            if not comps:
                st.caption("No components yet (S3 writes them).")
            for r in comps:
                kind, name, cstage = r.get("kind"), r.get("name"), r.get("stage") or r.get("state") or ""
                dot, word, color = {"code_failing": ("red", "tests fail", "#B42318"), "code_generated": ("green", "tests pass", "#1E7A43"),
                                    "approved": ("green", "ok", "#1E7A43"), "plan_approved": ("orange", "not coded yet", "#9A6700"),
                                    "code_changed": ("orange", "changed since its test", "#9A6700")}.get(cstage, ("grey", cstage.replace("_", " ") or "—", theme.MUTED))
                cc = st.columns([7, 3], vertical_alignment="center")
                cc[0].html(f'<span class="pkf-dot {dot}"></span><span class="pkf-muted">{kind}</span> <span class="pkf-mono">{theme.esc(name)}</span>')
                with cc[1]:
                    if cstage in ("code_failing", "plan_approved"):
                        _regenerate(s, p, states, st_, {"kind": kind, "name": name}, "Regenerate (AI)", f"ws-comp-regen-{kind}-{name}", running,
                                    primary=False)
                    else:
                        st.html(f'<div style="text-align:right;color:{color};font-weight:600;font-size:.82rem">{theme.esc(word)}</div>')
            t = f.json(f"review/{p}/tests.json")
            if t:
                from .workflow_dev import s4_summary

                with st.expander("Tests, examples and the draft build"):
                    s4_summary(f, p)
        elif stage == "S1":
            st.html('<b style="font-size:1rem">Requirement</b>')
            analysis = f.json(f"requirements/{p}/analysis.json")
            qs = analysis.get("open_questions") or []
            amb = (f.yaml(f"pipelines/{p}.lock.yaml").get("split") or {}).get("ambiguities") or []
            st.markdown(f"**Open questions:** {len(qs)} · **ambiguities:** {len(amb)}")
            for q in (qs + amb)[:6]:
                st.markdown(f"- {md_escape(q if isinstance(q, str) else str(q))}")
            if not (qs or amb):
                st.caption("None: the requirement is ready for G1." if f.yaml(f"pipelines/{p}.yaml") else "Not analysed yet.")
        elif stage == "S2":
            st.html('<b style="font-size:1rem">The split</b>')
            sp = f.yaml(f"pipelines/{p}.lock.yaml").get("split") or {}
            comps = sp.get("components") or {}
            for k, c_ in comps.items():
                st.html(f'<span class="pkf-dot {"grey" if c_.get("reuse") else "blue"}"></span><span class="pkf-muted">{k}</span> '
                        f'<span class="pkf-mono">{theme.esc(c_.get("name"))}</span> <span class="pkf-muted">{"reused" if c_.get("reuse") else "new"}</span>')
            if not comps:
                st.caption("Not split yet.")
        elif stage == "S3":
            st.html('<b style="font-size:1rem">Plans</b>')
            comps = f.components()
            for kind, name in comps:
                lock = f.yaml(f"{L.FOLDERS[kind]}/{name}.lock.yaml")
                n = len((lock.get("plan") or {}).get("steps") or [])
                st.html(f'<span class="pkf-dot {"green" if n else "grey"}"></span><span class="pkf-muted">{kind}</span> '
                        f'<span class="pkf-mono">{theme.esc(name)}</span> <span class="pkf-muted">{f"{n} step(s) planned" if n else "no plan yet"}</span>')
            planned = [(k, n) for k, n in comps if f.yaml(f"{L.FOLDERS[k]}/{n}.lock.yaml").get("plan")]
            if planned:
                with st.expander("Plan cards"):
                    for kind, name in planned:
                        st.markdown(f"**{kind} · {name}**")
                        plan_cards(f.yaml(f"{L.FOLDERS[kind]}/{name}.lock.yaml"))
            if not comps:
                st.caption("The component files are written when S3 starts.")
        elif stage == "S5":
            st.html('<b style="font-size:1rem">Dev environment</b>')
            man, dry, pol = f.json(f"infra/{p}/manifest.json"), f.json(f"review/{p}/dry-run.json"), f.json(f"infra/{p}/policy-check.json")
            if man or dry:
                tf = "nothing to apply" if str(man.get("note", "")).startswith("nothing") else f"{len(man.get('resources') or [])} resource(s)"
                c = st.columns(3)
                c[0].metric("Terraform", tf)
                c[1].metric("Policy check", "ok" if pol.get("ok", True) else "issues")
                c[2].metric("Dry run", "ok" if dry.get("ok") else "failed")
                if man.get("note"):
                    st.caption(md_escape(str(man["note"])))
            else:
                st.caption("Rendered and planned by 5.1.")
        elif stage == "S6":
            st.html('<b style="font-size:1rem">Local run</b>')
            pv = (st_ or {}).get("sub", {}).get("preview") if st_ else None
            if pv:
                st.markdown(f"Preview: **{pv.get('files', 0)}** file(s) in {pv.get('packages', 0)} package(s); {pv.get('deletes', 0)} source object(s) to delete")
            runs = [x for x in f.list(f"review/{p}/runs/") if x.endswith("report.json")]
            st.caption(f"{len(runs)} run report(s)" if runs else "No run yet.")
        else:
            st.html('<b style="font-size:1rem">Deploy</b>')
            st.caption("deploy.md has the exact commands; the checklist steps 7.3 and 7.4 record what was done.")


def files_strip(s, p: str, stage: str, artifacts_page=None) -> None:
    """Files of this stage: the files a reviewer looks at, with a status line and Open → (the Artifacts page)."""
    f = Files(s, p)
    files = _cached(f"files-{p}-{stage}", 5, lambda: stage_files(f, p, stage))
    if not files:
        return
    st.html('<div class="pkf-eyebrow" style="margin-top:.4rem">FILES OF THIS STAGE</div>')
    with st.container(horizontal=True, key="ws-files", gap="small"):
        for rel, status in files:
            with st.container(border=True, width=300):
                bad = status.startswith(("tests fail", "draft build failed"))
                short = rel.replace(f"review/{p}/", "review/").replace(f"requirements/{p}/", "requirements/").replace(f"infra/{p}/", "infra/")
                st.html(f'<div class="pkf-mono" title="{theme.esc(rel)}">{theme.esc(short)}</div>'
                        f'<div class="pkf-muted" style="color:{"#B42318" if bad else "#1E7A43"}">{theme.esc(status)}</div>')
                if artifacts_page is not None:
                    st.page_link(artifacts_page, label="Open →", query_params={"file": rel})


def stage_files(f: Files, p: str, stage: str) -> list[tuple[str, str]]:
    """The files a reviewer of the stage looks at, with a status line: [(path, status)]."""
    comps = f.components()
    tests = f.json(f"review/{p}/tests.json")
    rows = {(r.get("kind"), r.get("name")): r for r in tests.get("components") or [] if isinstance(r, dict)}
    out: list[str] = []
    if stage == "S1":
        out = [f"pipelines/{p}.yaml", f"requirements/{p}/pseudocode.md", f"requirements/{p}/analysis.json", f"requirements/{p}/extracted.txt"]
    elif stage == "S2":
        out = [f"pipelines/{p}.lock.yaml", f"pipelines/{p}.yaml"] + [f"{L.FOLDERS[k]}/{n}.yaml" for k, n in comps]
    elif stage == "S3":
        out = [x for k, n in comps for x in (f"{L.FOLDERS[k]}/{n}.yaml", f"{L.FOLDERS[k]}/{n}.lock.yaml")] + [f"review/{p}/examples.json"]
    elif stage == "S4":
        out = [f"{L.FOLDERS[k]}/{n}.py" for k, n in comps] + [f"review/{p}/capabilities.md", f"review/{p}/tests.json"]
    elif stage == "S5":
        out = [f"infra/{p}/manifest.json", f"infra/{p}/iam-policy.json", f"infra/{p}/module/main.tf", f"dags/pkf_{p}.py",
               f"review/{p}/dry-run.json", f"infra/{p}/policy-check.json"]
    elif stage == "S6":
        out = [x for x in f.list(f"review/{p}/runs/") if x.endswith((".json", ".md"))][-6:] + [f"review/{p}/dry-run.json"]
    elif stage == "S7":
        out = [f"review/{p}/deploy.md", f"dags/pkf_{p}.py", f"infra/{p}/iam-policy.json"]
    res = []
    for rel in out:
        text = f.read(rel)
        if text is None:
            continue
        n = len(text.splitlines())
        status = f"{n} lines"
        for (k, nm), r in rows.items():
            if rel == f"{L.FOLDERS.get(k, k)}/{nm}.py":
                st_ = r.get("stage") or r.get("state") or ""
                status = {"code_failing": "tests fail", "code_generated": "tests pass", "approved": "approved",
                          "code_changed": "changed since its test", "plan_approved": "not coded yet"}.get(st_, st_.replace("_", " ")) + f" · {n} lines"
        if rel.endswith("tests.json") and tests:
            status = f"draft build {'ok' if tests.get('build_ok') else 'failed'} · examples {'pass' if tests.get('examples_ok') else 'fail'} · per component"
        res.append((rel, status))
    return res


# ------------------------------------------------------------------------------------------------ assistant switch
def pilot_toggle(s, p: str, st_: dict, label: str = "Assistant can act", help_text: str | None = None) -> None:
    """Acts on its own / Assistant can act — the same state and event as 0.6.1's strip toggle (one place per page)."""
    key = f"wfd-pilot-live-{p}"
    # 0.8.2: the switch shows the run's state (Run without stopping, another tab may change it) and only a click changes it
    st.session_state[key] = bool((flow.load(s.root, p) or st_).get("assistant_autopilot"))
    st.toggle(label, key=key, on_change=_pilot_changed, args=(s, p, key),
              help=help_text or "on: when a step fails, the assistant diagnoses it and fixes, re-runs, goes back or reopens on its "
              "own (a file fix is applied only when its tests pass). Reviews, approvals, confirmations and S1 stay with you.")


def _pilot_changed(s, p: str, key: str) -> None:
    from .. import flowstore

    val = bool(st.session_state.get(key))
    cur = flow.load(s.root, p)
    if not cur or bool(cur.get("assistant_autopilot")) == val:
        return
    cur["assistant_autopilot"] = val
    if val:
        cur["pilot"] = {k: v for k, v in (cur.get("pilot") or {}).items() if k in ("cycles", "log")}
    flowstore.save(s.root, p, cur)
    flowstore.event(s.root, p, "assistant_pilot", phase="on" if val else "off", summary=f"Assistant can act: {'on' if val else 'off'}")


# ------------------------------------------------------------------------------------------------ console
def _job_choices(st_: dict | None) -> list[str]:
    rows = (st_ or {}).get("steps") or {}
    return [f"{x.id} {x.title}" for x in flow.STEPS if (rows.get(x.id) or {}).get("job_id")]


def console(s, p: str, st_: dict | None, sel: str | None) -> None:
    """The console: header (step selector, find in log, Download), the log (level chips, Copy, Follow and line numbers in
    the component), the views under it (Console · Workflow log · Assistant transcript · Approval history · Jobs)."""
    from .workflow import approval_history
    from .workflow_dev import assistant_lines

    view_key = f"ws-cview-{p}"
    view = st.session_state.get(view_key) or CONSOLE_VIEWS[0]
    with st.container(border=True, key="ws-console"):
        lines: list[dict] = []
        title = ""
        h = st.columns([1.1, 3.6, 3.2, 1.3], vertical_alignment="center", gap="small")
        h[0].html('<span style="color:#E6EAF2;font-weight:600"><span class="pkf-dot green"></span>Console</span>')
        if view == "Console":
            choices = _job_choices(st_)
            cur_label = f"{sel} {flow.BY_ID[sel].title}" if sel in flow.BY_ID else None
            if cur_label and cur_label not in choices:
                choices = [cur_label] + choices
            if choices:
                idx = choices.index(cur_label) if cur_label in choices else len(choices) - 1
                pick = h[1].selectbox("Step", choices, index=idx, key=f"ws-cstep-{p}-{sel}", label_visibility="collapsed")
                sel_step = pick.split(" ", 1)[0]
            else:
                sel_step = sel
                h[1].caption("no job has run yet")
            lines = flow.console_lines(s, p, st_, sel_step) if st_ else []
            row = ((st_ or {}).get("steps") or {}).get(sel_step or "", {})
            jid = jobs.current(s.root, p)
            if jid and row.get("job_id") != jid:      # a job no step owns (a button on another page)
                stt = jobs.status(s.root, jid) or {}
                lines = lines + [{"ts": "", "level": "info", "source": "job", "text": ln}
                                 for ln in [f"== {stt.get('title') or stt.get('action') or 'job'} (running)"] + jobs.log(s.root, jid, tail=200).splitlines()]
            title = f"{sel_step} {flow.BY_ID[sel_step].title}" if sel_step in flow.BY_ID else "console"
            full_id = row.get("job_id") or jid
        elif view == "Workflow log":
            lines = [x for x in flow.console_lines(s, p, st_, None) if x["source"] == "flow"] if st_ else []
            title, full_id = "workflow log (events.jsonl)", None
        elif view == "Assistant transcript":
            lines = assistant_lines(s, p)
            title, full_id = "assistant transcript", None
        else:
            full_id = None
        query = h[2].text_input("Find in log", key=f"ws-cfind-{p}", placeholder="find in log…", label_visibility="collapsed") \
            if view in ("Console", "Workflow log", "Assistant transcript") else ""
        with h[3]:
            if view == "Console" and full_id:
                st.download_button("⬇ Log", data=lambda jid_=full_id: jobs.log(s.root, jid_, tail=10**9) or "",
                                   file_name=f"{p}-{full_id}.log", mime="text/plain", key=f"ws-cdl-{p}", help="download the whole job log")
            elif view in ("Workflow log", "Assistant transcript") and lines:
                st.download_button("⬇ Log", data="\n".join(f"{x.get('ts', '')} [{x.get('source', '')}] {x.get('text', '')}" for x in lines),
                                   file_name=f"{p}-{view.replace(' ', '-').lower()}.log", mime="text/plain", key=f"ws-cdl-{p}")
        if view in ("Console", "Workflow log", "Assistant transcript"):
            key = {"Console": f"wfd-console-{p}", "Workflow log": f"wfd-flowlog-{p}", "Assistant transcript": f"wfd-assistlog-{p}"}[view]
            wfd.console(lines, key=key, title=title, height=380, fill=True, query=query or "", numbers=True)
        elif view == "Approval history":
            with st.container(key="ws-con-scroll"):
                approval_history(s, p)
        else:
            with st.container(key="ws-con-scroll"):
                recent = jobs.recent(s.root, p, 8)
                if recent:
                    job_history(s, recent)
                else:
                    st.caption("No jobs yet.")
        f = st.columns([7, 3], vertical_alignment="center")
        with f[0]:
            st.segmented_control("View", CONSOLE_VIEWS, key=view_key, default=CONSOLE_VIEWS[0], label_visibility="collapsed")
        q = (query or "").strip().lower()
        hits = [x for x in lines if q in f"{x.get('text', '')} {x.get('source', '')}".lower()] if q else lines
        n_err = sum(1 for x in hits if x.get("level") == "error")
        count = f"{len(hits)} of {len(lines)} lines" if q else f"{len(lines)} lines"
        f[1].html(f'<div style="text-align:right;color:#7E879A;font-family:{theme.MONO};font-size:.78rem">{count} · error {n_err}</div>')


def local_time_of(iso) -> str:
    return local_time(iso)
