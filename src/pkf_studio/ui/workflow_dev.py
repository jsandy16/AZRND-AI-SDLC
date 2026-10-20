"""Tab 8 — Workflow Developer: the run of a pipeline through every stage and substage (flow.py), with the stage tree,
the "now running" strip, the one action card that matters, the console and the assistant (docs/WORKFLOW_DEVELOPER_PLAN.md).

Everything live sits in one fragment that reruns every 2 s while the run is active: it ticks the run, redraws the tree
(the component updates in place) and tails the console. Buttons inside it rerun the whole app (act), like the other tabs."""
from __future__ import annotations

import json
import time
from pathlib import Path

import streamlit as st
import yaml

from .. import flow, flowstore, jobs, pilot, stages
from .. import ledger as L
from ..gates import GATES
from .common import BUSY_HELP, Files, act, busy, err_text, md_escape, plan_cards, refresh

PILL = {"idle": ("⚪", "not started"), "running": ("🟢", "running"), "waiting_input": ("🟡", "waiting for your input"),
        "waiting_gate": ("🟡", "waiting for approval"), "waiting_user": ("🟡", "waiting for you"), "failed": ("🔴", "failed"),
        "done": ("✅", "complete"), "stopped": ("⏸", "stopped")}
ACTIVE = ("running", "waiting_input", "waiting_gate", "waiting_user")
GATE_POLL_S = 15


# ------------------------------------------------------------------------------------------------ helpers
def _states(s, p: str, states: dict, state: dict | None) -> dict:
    """Fresh gate states while the run waits for GitHub (at most every GATE_POLL_S seconds), else the app's."""
    if not state or state.get("status") != "waiting_gate":
        return states
    key, now = f"_wfd-poll-{p}", time.monotonic()
    last = st.session_state.get(key, 0.0)
    if now - last < GATE_POLL_S:
        return st.session_state.get(f"_wfd-states-{p}") or states
    st.session_state[key] = now
    try:
        s.invalidate()
        fresh = stages.derive(s, p)
        st.session_state[f"_wfd-states-{p}"] = fresh
        return fresh
    except Exception:  # noqa: BLE001 - GitHub hiccup: use what we have
        return states


def _do(fn, *a, ok=None, **kw):
    """A flow action: run it, refresh the app's view of git/GitHub when it may have changed, rerun."""
    def wrapped():
        out = fn(*a, **kw)
        st.cache_data.clear()
        return out
    act(wrapped, ok=ok)


def _refresh_do(fn, *a, ok=None, **kw):
    def wrapped():
        out = fn(*a, **kw)
        refresh(a[0])
        return out
    act(wrapped, ok=ok)


def _step_row(st: dict | None, step_id: str) -> dict:
    return ((st or {}).get("steps") or {}).get(step_id) or {}


# ------------------------------------------------------------------------------------------------ intake
def intake(s, p: str, states: dict) -> None:
    from .requirements import _extract_upload, _findings, _show_findings

    fields = flow.intake_fields(s, p)
    st.markdown("#### Before the run starts")
    st.caption("Everything the run can ask now, in one go. It pauses later only for what the AI discovers: the open questions "
               "(after 1.3), the environment values (3.2) and the S6 confirmations.")
    blocked = []
    if fields["whoami"]["ask"]:
        blocked.append("choose **Who am I** in Setup (the approvals are recorded in your name)")
    if fields["aws"]["ask"]:
        st.info("No AWS account id in Setup: fine for pipelines that need no AWS resources; S5/S6 need it otherwise.")
    if fields["run_role"]["ask"]:
        st.info("No run role in Setup: a single-person dev team may run with its own credentials (asked at 6.1).")
    doc_text = doc_bytes = doc_name = None
    flags: list[str] = []
    if fields["document"].get("have"):
        meta = Files(s, p).yaml(f"requirements/{p}/meta.yaml")
        st.success(f"Requirement already imported: **{meta.get('original_name', '?')}** · {meta.get('chars', '?')} characters")
    else:
        up = st.file_uploader("Requirement document (.txt .md .docx .pdf, max 10 MB)", type=["txt", "md", "docx", "pdf"], key=f"wfd-up-{p}")
        pasted = st.text_area("…or paste the requirement", key=f"wfd-paste-{p}", height=180, persist_state="session")
        if up is not None:
            res = _extract_upload(up)
            if not res["ok"]:
                st.error(res["error"])
                blocked.append("the document could not be read")
            else:
                doc_text, doc_bytes, doc_name, flags = res["text"], res["data"], res["name"], res["flags"]
                for fl in flags:
                    st.warning(fl)
                if _show_findings(_findings(s, doc_text)):
                    blocked.append("remove the secrets from the document")
                with st.expander("Extracted text (this is what the AI sees)"):
                    st.code(doc_text[:20000], language="text", wrap_lines=True, height=220)
        elif pasted.strip():
            doc_text, doc_name = pasted, "pasted text"
            if _show_findings(_findings(s, doc_text)):
                blocked.append("remove the secrets from the text")
        else:
            blocked.append("upload or paste the requirement")
    auto = False
    if not s.strict:
        auto = st.toggle("Auto-approve the gates (dev only): the run posts /approve and merges every gate itself", key=f"wfd-auto-{p}",
                         help="Off: each gate shows Approve as me and merges after your click. On: no click per gate; inputs and failures still pause the run.")
    pilot_on = st.toggle("Assistant can act on its own: when a step fails it diagnoses, fixes (applied only when the tests pass), "
                         "re-runs, goes back or reopens a stage — reviews, approvals and S1 stay with you", value=not s.strict, key=f"wfd-pilot-{p}",
                         help="on by default in dev mode, off in SIT/PPT/PRD (switch it on there deliberately)")
    full = {}
    if not s.strict and s.cfg.mode == "dev":
        full = full_auto_form(s, p, key=f"wfd-full-{p}")
    if blocked:
        st.markdown("**Still needed:** " + "; ".join(blocked))
    running = busy(s, p)
    if st.button("▶ Run workflow", key=f"wfd-run-{p}", type="primary", disabled=bool(blocked) or running, help=BUSY_HELP if running else None):
        answers = {"document_name": doc_name, "document_bytes": doc_bytes, "document_text": doc_text, "flags": flags, "auto_approve": auto,
                   "assistant_autopilot": pilot_on, **full}
        if fields["document"].get("have"):
            answers = {"auto_approve": auto, "assistant_autopilot": pilot_on, **full}
        _refresh_do(flow.start, s, p, states, answers, ok="workflow started")


def full_auto_form(s, p: str, key: str, st_: dict | None = None) -> dict:
    """Run without stopping (0.8.2, dev only) and its pre-answers. Returns the answers to merge ({} when off)."""
    cur = (st_ or {}).get("answers") or {}
    on = st.toggle("Run without stopping (dev): the run approves, continues the reviews, fixes and re-runs by itself, runs the "
                   "pipeline locally and places a test package when the source is empty", value=bool((st_ or {}).get("full_auto")),
                   key=key, help="Stops only where a person is needed: open questions, environment values, a failure the assistant "
                                 "cannot fix, files it did not place itself, and the Airflow deploy (7.3 / 7.4).")
    if not on:
        return {}
    acct = s.cfg.aws_account_id
    if not acct:
        st.warning("Setup → AWS account id is empty: the S6 local run will stop to ask for it.")
    own = st.checkbox("6.1: run the pipeline with my own AWS credentials", value=cur.get("own_credentials", True), key=f"{key}-own",
                      disabled=bool(s.cfg.run_role_arn), help="not needed when a run role is set in Setup")
    nodag = st.checkbox("6.2: there is no old DAG to pause (or it is paused)", value=cur.get("no_old_dag", True), key=f"{key}-dag")
    anyf = st.checkbox("6.3: also Move files the run did not place itself (the whole dev source)", value=cur.get("move_any", False),
                       key=f"{key}-any", help="off: the run moves only its own test package and stops for anything else")
    return {"full_auto": True, "auto_approve": True, "assistant_autopilot": True, "own_credentials": bool(own), "no_old_dag": bool(nodag),
            "move_any": bool(anyf)}


FULL_KEYS = ("own_credentials", "no_old_dag", "move_any")


def full_auto_live(s, p: str, st_: dict) -> None:
    """Run ▾ → Run without stopping during a run: the widgets show the run's state, a click changes it (callbacks)."""
    key = f"wfd-full-live-{p}"
    ans = st_.get("answers") or {}
    st.session_state[key] = bool(st_.get("full_auto"))
    st.toggle("Run without stopping (dev)", key=key, on_change=_full_changed, args=(s, p, key),
              help="approves, continues the reviews, fixes, runs locally and places a test package by itself; stops where a "
                   "person is needed (open questions, environment values, files it did not place, the Airflow deploy)")
    if not st_.get("full_auto"):
        return
    labels = {"own_credentials": "6.1: run with my own AWS credentials", "no_old_dag": "6.2: there is no old DAG to pause",
              "move_any": "6.3: also Move files the run did not place itself"}
    for k in FULL_KEYS:
        st.session_state[f"{key}-{k}"] = bool(ans.get(k))
        st.checkbox(labels[k], key=f"{key}-{k}", on_change=_full_changed, args=(s, p, key))


def _full_changed(s, p: str, key: str) -> None:
    on = bool(st.session_state.get(key))
    cur = (flow.load(s.root, p) or {}).get("answers") or {}
    vals = {k: bool(st.session_state.get(f"{key}-{k}", cur.get(k, k != "move_any"))) for k in FULL_KEYS}
    set_full_auto(s, p, {"full_auto": on, **vals})


def set_full_auto(s, p: str, answers: dict) -> dict:
    """Run ▾ → Run without stopping, during a run (dev only)."""
    st_ = flow.load(s.root, p) or {}
    if s.strict or s.cfg.mode != "dev":
        return st_
    on = bool(answers.get("full_auto"))
    st_["full_auto"] = on
    if on:
        st_["auto_approve"] = st_["assistant_autopilot"] = True
        st_.setdefault("answers", {}).update({k: bool(answers.get(k)) for k in ("own_credentials", "no_old_dag", "move_any")})
        st_.setdefault("auto", {})["needs_you"] = None
        st_["auto"].pop("needs_you_key", None)       # a stop that still holds is shown again
    flowstore.save(s.root, p, st_)
    flowstore.event(s.root, p, "full_auto", summary=f"Run without stopping: {'on' if on else 'off'}")
    return st_


def _fresh(s, p: str) -> dict:
    try:
        s.invalidate()
        return stages.derive(s, p)
    except Exception:  # noqa: BLE001
        return stages.derive_offline(s, p, "GitHub unreachable")


# ------------------------------------------------------------------------------------------------ action cards
def action_card(s, p: str, states: dict, st_: dict) -> None:
    cur = st_.get("cursor")
    step = flow.BY_ID.get(cur or "")
    if step is None:
        return
    row = _step_row(st_, step.id)
    status = row.get("status")
    with st.container(border=True):
        st.markdown(f"**{step.id} {step.title}** · {flow.STAGE_TITLES[step.stage]}")
        if status == "failed":
            failed_card(s, p, states, st_, step, row)
            return
        if st_["status"] == "stopped":
            st.caption("The run is stopped. Resume continues here.")
            return
        if status == "running" or st_["status"] == "running":
            st.caption("Running… the console below shows the job's log" + (" (Cancel is in the card above)." if jobs.current(s.root, p) else "."))
            return
        if step.kind == "gate" or step.id == "6.4":
            gate_wait(s, p, states, st_, step)
        elif step.kind == "review":
            review_card(s, p, states, st_, step, row)
        elif step.kind == "input":
            input_card(s, p, states, st_, step, row)
        elif step.kind == "checklist":
            checklist_card(s, p, states, st_, step, row)
        elif step.kind == "action":
            download_card(s, p, states, st_, step)
        elif step.kind == "job" and status == "waiting":
            job_wait_card(s, p, states, st_, step, row)


def failed_card(s, p: str, states: dict, st_: dict, step, row: dict) -> None:
    st.error(md_escape(row.get("detail") or "failed"))
    probs = row.get("problems") or []
    if len(probs) > 1:
        st.markdown("\n".join(f"- {md_escape(x)}" for x in probs[:8]))
    running = busy(s, p)
    c = st.columns(4)
    if c[0].button("↻ Retry", key=f"wfd-retry-{p}", type="primary", disabled=running, help=BUSY_HELP if running else None):
        _refresh_do(flow.retry, s, p, _fresh(s, p), ok=f"{step.id} started again")
    if c[1].button("🤖 Ask the assistant", key=f"wfd-assist-{p}", disabled=running, help=BUSY_HELP if running else
                   "the assistant reads the logs, the artefacts and the code, finds the root cause and proposes a fix"):
        st.session_state[f"wfd-assist-open-{p}"] = True
        start_assist(s, p, "diagnose", step_id=step.id, job_id=row.get("job_id"))
    if row.get("can_continue") or step.optional:
        if c[2].button("Continue anyway" if row.get("can_continue") else "Skip", key=f"wfd-skip-{p}", disabled=running):
            _refresh_do(flow.skip, s, p, _fresh(s, p), ok=f"{step.id} skipped")
    if c[3].button("Stop", key=f"wfd-stop2-{p}"):
        _do(flow.stop, s, p, ok="workflow stopped")
    # 0.6.1: the way out of a failure that Retry cannot fix (a gate that is not ready, a merged but stale gate)
    fix, g = row.get("fix_step"), row.get("reopen")
    if fix in flow.BY_ID or g:
        c2 = st.columns(2)
        if fix in flow.BY_ID and c2[0].button(f"↩ Run from {fix} {flow.BY_ID[fix].title}", key=f"wfd-fixstep-{p}", disabled=running,
                                              help=BUSY_HELP if running else "go back to the step that fixes this; the steps after it run again"):
            _run_from(s, p, fix)
        if g:
            with c2[1]:
                reopen_button(s, p, g, key=f"wfd-reopen-failed-{p}")
    if step.kind == "gate":
        st.caption("A gate fails when the stage is not ready (the detail says what is missing) or a merge is refused. Fix it in the tabs, then Retry.")


# ------------------------------------------------------------------------------------------------ navigation (0.6.1)
def _me(s) -> str:
    try:
        me = s.me()
    except Exception:  # noqa: BLE001
        me = None
    return me.name if me else "you"


def _run_from(s, p: str, step_id: str, stop: str | None = None, reopen: bool = False) -> None:
    _refresh_do(flow.run_from, s, p, _fresh(s, p), step_id, stop_after=stop, reopen=reopen, by=_me(s), derive=lambda: _fresh(s, p),
                ok=f"the run continues from {step_id}" + (f" and stops after {stop}" if stop else ""))


PREVIEW_TTL_S = 20


def _reopen_preview(s, p: str, gate: str) -> list[dict]:
    """Studio.reopen_preview, cached 20 s per gate: popovers and expanders render their content on every rerun (every 2 s
    while the run is active) and the preview asks GitHub which pull requests are open."""
    key = f"_wfd-preview-{p}-{gate}"
    hit = st.session_state.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < PREVIEW_TTL_S:
        return hit[1]
    rows = s.reopen_preview(p, gate)
    st.session_state[key] = (now, rows)
    return rows


def reopen_button(s, p: str, gate: str, key: str, inline: bool = False) -> None:
    """Reopen <gate> with its two clicks: the popover lists what it sets aside, then Confirm. inline=True (0.7.0, inside
    the ↩ Go back popover, where a popover in a popover is not reliable): the same list and Confirm, in place."""
    running = busy(s, p)
    if inline:
        with st.container(border=True):
            st.markdown(f"**Reopen {gate}** — restart its stage from the current base branch; the later stages restart after it")
            _reopen_body(s, p, gate, key, running)
        return
    with st.popover(f"Reopen {gate}", disabled=running, help=BUSY_HELP if running else
                    f"restart {gate}'s stage from the current base branch; the later stages restart after it"):
        _reopen_body(s, p, gate, key, running)


def _reopen_body(s, p: str, gate: str, key: str, running: bool) -> None:
    try:
        rows = _reopen_preview(s, p, gate)
    except Exception as exc:  # noqa: BLE001
        rows = []
        st.caption(f"could not list what Reopen sets aside: {err_text(exc)}")
    if rows:
        st.markdown("Sets aside (kept as local archive refs; open pull requests are closed):")
        for r in rows:
            st.markdown(f"- `{r['branch']}`" + (f" · {r['unpushed']} unpushed commit(s)" if r.get("unpushed") else "")
                        + (f" · PR #{r['open_pr']}" if r.get("open_pr") else ""))
    else:
        st.caption("Nothing to set aside.")
    if st.button(f"Confirm Reopen {gate}", key=key, type="primary", disabled=running):
        for g_ in ("G1", "G2", "G3", "G4", "G5"):
            st.session_state.pop(f"_wfd-preview-{p}-{g_}", None)
        _refresh_do(flow.reopen_stage, s, p, _fresh(s, p), gate, by=_me(s), derive=lambda: _fresh(s, p),
                    ok=f"{gate} reopened; the run continues from its stage")


def navigator_body(s, p: str, states: dict, st_: dict | None, sel: str | None, inline_reopen: bool = False) -> None:
    """Go back to any step, run a single stage (stop after it), or reopen a stage — the ↩ Go back popover of the
    Workspace (0.7.0; 0.6.1 had it in an expander). Reviews and approvals still wait for a person; a jump the approval
    chain does not allow says which gate comes first. inline_reopen: the Reopen confirm in place (no nested popover)."""
    running = busy(s, p)
    ids = [x.id for x in flow.STEPS]
    labels = [f"{x.id} {x.title}" for x in flow.STEPS]
    default = sel if sel in ids else ((st_ or {}).get("cursor") if (st_ or {}).get("cursor") in ids else ids[0])
    c = st.columns([5, 4, 3])
    pick = c[0].selectbox("Run from step", labels, index=ids.index(default), key=f"wfd-nav-step-{p}")
    stops = ["the end (no stop)"] + [flow.STAGE_TITLES[x] for x in flow.STAGE_IDS]
    stop_label = c[1].selectbox("…and stop after", stops, key=f"wfd-nav-stop-{p}")
    step_id = pick.split(" ", 1)[0]
    stop = None if stop_label == stops[0] else flow.STAGE_IDS[stops.index(stop_label) - 1]
    chk = flow.jump_check(s, p, states, step_id)
    with c[2]:
        st.write("")
        if not chk["ok"]:
            if chk.get("suggest") and st.button(f"Run from {chk['suggest']} instead", key=f"wfd-nav-suggest-{p}", disabled=running,
                                                help=BUSY_HELP if running else None):
                _run_from(s, p, chk["suggest"])
        elif chk["needs_reopen"]:
            if st.button(f"Reopen {chk['needs_reopen']} and run from {step_id}", key=f"wfd-nav-reopen-run-{p}", type="primary",
                         disabled=running, help=BUSY_HELP if running else None):
                _run_from(s, p, step_id, stop, reopen=True)
        elif st.button(f"▶ Run from {step_id}", key=f"wfd-nav-run-{p}", type="primary", disabled=running,
                       help=BUSY_HELP if running else None):
            _run_from(s, p, step_id, stop)
    over = flow.people_skipped(st_, step_id) if st_ and st_.get("status") not in (None, "idle") else []
    if chk["ok"] and over:
        st.warning("This jumps over " + ", ".join(f"{x} {flow.BY_ID[x].title}" for x in over)
                   + " — a person's step that is not done (the assistant never does this).")
    if not chk["ok"]:
        st.warning(md_escape(chk["problem"]))
    elif chk["needs_reopen"]:
        st.info(md_escape(chk["problem"]))
        try:
            rows = _reopen_preview(s, p, chk["needs_reopen"])
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            st.caption("Sets aside: " + ", ".join(f"`{r['branch']}`" + (f" (PR #{r['open_pr']})" if r.get("open_pr") else "") for r in rows))
    st.caption("Reviews (2.2, 3.4, 4.2, 5.3), approvals and checklists still wait for a person. A stage builds on the "
               "approved stage before it, so a jump past a gate that is not approved is refused with the step to run first.")
    approved = [g for g in ("G1", "G2", "G3", "G4", "G5") if (states["by_gate"].get(g) is not None
                                                               and states["by_gate"][g].state in ("approved", "stale", "merged_unapproved"))]
    if approved:
        c3 = st.columns([3, 9])
        g = c3[0].selectbox("Reopen a stage", approved, index=len(approved) - 1, key=f"wfd-nav-reopen-pick-{p}")
        with c3[1]:
            st.write("")
            reopen_button(s, p, g, key=f"wfd-nav-reopen-{p}-{g}", inline=inline_reopen)


def gate_wait(s, p: str, states: dict, st_: dict, step) -> None:
    g = flow.STAGE_GATE[step.stage]
    gs = states["by_gate"].get(g)
    running = busy(s, p)
    if g == "RUN":
        st.markdown("Waiting for the run's record PR." + (" It merges when the `pkf-gate` check is green." if s.strict else ""))
        if gs is not None and gs.pr:
            st.link_button(f"PR #{gs.pr['number']}", gs.pr["html_url"])
            if s.strict and st.button("Merge the record PR", key=f"wfd-merge-run-{p}", disabled=running):
                _refresh_do(s.merge, p, "RUN", ok="the run record is merged")
        return
    gate = GATES[g]
    roles = ", ".join(r.replace("_", " ") for r in gate.roles)
    st.markdown(f"**{g} {gate.title}** — approvers: {roles}" + (" (different people)" if s.strict else ""))
    if gs is not None and gs.pr:
        c = st.columns([2, 2, 2, 3])
        c[0].link_button(f"PR #{gs.pr['number']} on GitHub", gs.pr["html_url"])
        if c[1].button("Refresh", key=f"wfd-poll-{p}-{g}"):
            st.session_state[f"_wfd-poll-{p}"] = 0.0
            _refresh_do(flow.tick, s, p, _fresh(s, p))
        if not s.strict and c[2].button("✓ Approve as me", key=f"wfd-approve-{p}-{g}", type="primary", disabled=running,
                                        help="dev mode: posts /approve <head> on the PR with your token; the run merges it on the next tick"):
            def approve_and_tick():
                s.post_approve(p, g)
                refresh(s)
                return flow.tick(s, p, _fresh(s, p))
            act(approve_and_tick, ok=f"{g} approved; merging")
        st.caption(md_escape(_step_row(st_, step.id).get("detail") or ""))
        if s.strict:
            st.caption("Approve on GitHub: Review → Approve on the latest commit (several roles? add `Approve as: architect`). "
                       "The run notices the approval and merges by itself.")
    else:
        st.caption("Opening the pull request…")


def review_card(s, p: str, states: dict, st_: dict, step, row: dict) -> None:
    f = Files(s, p)
    detail = row.get("detail") or ""
    if detail.startswith(("G1 rejected", "G2 rejected", "G3 rejected", "G4 rejected", "G5 rejected")):
        st.warning(md_escape(detail))
    if step.id == "1.4":
        spec = f.yaml(f"pipelines/{p}.yaml")
        analysis = f.json(f"requirements/{p}/analysis.json")
        lock = f.yaml(f"pipelines/{p}.lock.yaml")
        qs = analysis.get("open_questions") or []
        amb = (lock.get("split") or {}).get("ambiguities") or []
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Open questions** — answer them in the requirement text")
            for q in qs or ["none"]:
                st.markdown(f"- {md_escape(str(q))}")
        with c2:
            st.markdown("**Ambiguities from the split**")
            for q in amb or ["none"]:
                st.markdown(f"- {md_escape(q if isinstance(q, str) else json.dumps(q))}")
        if analysis.get("assumptions"):
            with st.expander("Assumptions the AI made"):
                for a in analysis["assumptions"]:
                    st.markdown(f"- {md_escape(str(a))}")
        text = st.text_area("Finalized requirement (edit here; a change runs Split again)", spec.get("requirement") or "", height=360,
                            key=f"wfd-req-{p}-{st_.get('updated_at')}")
        if st.button("Save and continue →", key=f"wfd-cont-{p}-{step.id}", type="primary", disabled=busy(s, p)):
            _refresh_do(flow.answer, s, p, _fresh(s, p), step.id, {"requirement": text}, ok="requirement saved")
        return
    if step.id == "2.2":
        sp = f.yaml(f"pipelines/{p}.lock.yaml").get("split") or {}
        if sp.get("summary"):
            st.markdown(sp["summary"])
        st.dataframe([{"kind": k, "name": c.get("name"), "reuse": c.get("reuse"),
                       "requirement": (((c.get("spec") or {}).get("requirement") or "steps") if not c.get("reuse") else "existing")[:200]}
                      for k, c in (sp.get("components") or {}).items()], hide_index=True)
        c = st.columns([2, 3])
        if c[0].button("Looks good, continue →", key=f"wfd-cont-{p}-{step.id}", type="primary", disabled=busy(s, p)):
            _refresh_do(flow.answer, s, p, _fresh(s, p), step.id, {}, ok="design reviewed")
        with c[1].popover("Split wrong: Reopen G1"):
            st.caption("Restarts G1 from the base branch (the later stage PRs are closed; branch tips are kept as set-aside work). "
                       "The run goes back to 1.4.")
            if st.button("Confirm Reopen G1", key=f"wfd-reopen-g1-{p}", disabled=busy(s, p)):
                def reopen():
                    s.reopen(p, "G1")
                    refresh(s)
                    return flow.back_to(s, p, _fresh(s, p), "1.4", "Reopen G1 from 2.2")
                act(reopen, ok="G1 reopened; back to 1.4")
        return
    if step.id == "3.4":
        for kind, name in f.components():
            lock = f.yaml(f"{L.FOLDERS[kind]}/{name}.lock.yaml")
            if lock.get("plan"):
                with st.expander(f"{kind} · {name}", expanded=True):
                    plan_cards(lock)
        ex = f.json(f"review/{p}/examples.json")
        if ex:
            (st.success if ex.get("ok") else st.warning)("End-to-end examples: " + ("pass" if ex.get("ok") else "not passing yet (custom steps are coded at S4)"))
        st.caption("To re-plan one component, use its tab (Source / Destination / Trigger / Transformation → Re-plan), then Continue.")
    if step.id == "4.2":
        s4_summary(f, p)
        cap = f.read(f"review/{p}/capabilities.md")
        if cap:
            with st.expander("Capability summary (deletes, network, processes) — part of the G4 review"):
                st.markdown(cap)
    if step.id == "4.3":
        bad = flow.components_needing_work(s, p)
        s4_summary(f, p)
        running = busy(s, p)
        c = st.columns(3)
        if c[0].button("Run tests & build again", key=f"wfd-tb-{p}", type="primary", disabled=running):
            _do(flow.answer, s, p, states, step.id, {"job": "test_build", "title": "Run tests & build again"}, ok="tests and build started")
        comps = [b for b in bad if b.get("kind") in ("source", "destination", "transformation", "trigger")]
        if comps:
            pick = c[1].selectbox("Component", [f"{b['kind']} {b['name']}" for b in comps], key=f"wfd-regen-pick-{p}", label_visibility="collapsed")
            if c[2].button("Regenerate (AI)", key=f"wfd-regen-{p}", disabled=running):
                kind, name = pick.split(" ", 1)
                _do(flow.answer, s, p, states, step.id, {"job": "code_component", "args": {"kind": kind, "name": name}, "title": f"Regenerate {kind} {name}"},
                    ok=f"regenerating {kind} {name}")
        if st.button("🤖 Ask the assistant to fix it", key=f"wfd-assist43-{p}", disabled=running):
            st.session_state[f"wfd-assist-open-{p}"] = True
            start_assist(s, p, "diagnose", step_id=step.id, job_id=_step_row(st_, "4.1").get("job_id"))
    if step.id == "5.2":
        st.warning("5.1 reported problems (see the console). Render & plan again repeats the Terraform plan and the dry run.")
        if st.button("Render & plan again", key=f"wfd-rpa-{p}", type="primary", disabled=busy(s, p)):
            _do(flow.answer, s, p, states, step.id, {"job": "prepare_env"}, ok="render & plan started")
    if step.id == "5.3":
        man = f.json(f"infra/{p}/manifest.json")
        dry = f.json(f"review/{p}/dry-run.json")
        pol = f.json(f"infra/{p}/policy-check.json")
        c = st.columns(3)
        c[0].metric("Terraform", "nothing to apply" if str(man.get("note", "")).startswith("nothing") else f"{len(man.get('resources') or [])} resource(s)")
        c[1].metric("Policy check", "ok" if pol.get("ok", True) else "issues")
        c[2].metric("Dry run", "ok" if dry.get("ok") else "failed")
        if man.get("note"):
            st.caption(md_escape(str(man["note"])))
        for rel in (f"infra/{p}/iam-policy.json", f"review/{p}/dry-run.json"):
            txt = f.read(rel)
            if txt:
                with st.expander(rel):
                    st.code(txt[:20000], language="json", height=240)
    label = {"3.4": "Plans reviewed, continue →", "4.2": "Code reviewed, continue →", "4.3": "Continue to G4 →", "5.2": "Continue →",
             "5.3": "Reviewed, continue →"}.get(step.id, "Continue →")
    if st.button(label, key=f"wfd-cont-{p}-{step.id}", type="primary" if step.id not in ("4.3", "5.2") else "secondary", disabled=busy(s, p)):
        _refresh_do(flow.answer, s, p, _fresh(s, p), step.id, {}, ok=f"{step.id} done")


def s4_summary(f: Files, p: str) -> None:
    t = f.json(f"review/{p}/tests.json")
    if not t:
        st.caption("No S4 status yet.")
        return
    c = st.columns(3)
    c[0].metric("Draft build", "ok" if t.get("build_ok") else "failed")
    c[1].metric("End-to-end examples", "pass" if t.get("examples_ok") else "fail")
    c[2].metric("Components", len(t.get("components") or []))
    rows = []
    for r in t.get("components") or []:
        rows.append({"component": f"{r.get('kind')} {r.get('name')}", "state": r.get("state"), "custom steps": ", ".join(r.get("custom_steps") or []),
                     "failing": "; ".join(str(x.get("msg") or x) for x in (r.get("failing") or []))[:160],
                     "AI": "; ".join(r.get("ai_issues") or [])[:120]})
    if rows:
        st.dataframe(rows, hide_index=True)
    for i in (t.get("build_issues") or [])[:5]:
        st.warning(md_escape(str(i.get("msg") if isinstance(i, dict) else i)))


def input_card(s, p: str, states: dict, st_: dict, step, row: dict) -> None:
    from ..service import unfilled
    from .components import required_paths

    if step.id == "3.2":
        wt = s.worktree_path(p)
        st.markdown("**Environment values** (bucket names, folders, queue URLs… for dev). The run continues when none is `<REQUIRED>`.")
        with st.form(f"wfd-env-{p}"):
            files: dict[str, dict] = {}
            for line in unfilled(wt, p):
                rel = line.split(":", 1)[0]
                if rel in files:
                    continue
                try:
                    doc = yaml.safe_load((wt / rel).read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError):
                    continue
                holes = required_paths(doc)
                if not holes:
                    continue
                st.markdown(f"`{rel}`")
                vals = {}
                for keys, v in holes:
                    k = ".".join(map(str, keys))
                    hint = v.replace("<REQUIRED", "").strip("<> :") or ""
                    vals[json.dumps(list(keys))] = st.text_input(k, key=f"wfd-env-{p}-{rel}-{k}", placeholder=hint or "value")
                files[rel] = vals
            if st.form_submit_button("Save values and continue →", type="primary", disabled=busy(s, p)):
                clean = {rel: {k: v for k, v in vals.items() if str(v).strip()} for rel, vals in files.items()}
                _do(flow.answer, s, p, states, step.id, {"files": clean}, ok="environment values saved")
        return
    if step.id == "6.1":
        st.warning("No run role is configured (Setup → Run role ARN). As a single-person dev team you may run with your own AWS credentials.")
        own = st.checkbox("Run with my own AWS credentials", key=f"wfd-own-{p}")
        if st.button("Continue →", key=f"wfd-cont-{p}-61", type="primary", disabled=not own or busy(s, p)):
            _do(flow.answer, s, p, states, step.id, {"own_credentials": True}, ok="credentials confirmed")
        return
    st.caption(step.help)
    if st.button("Continue →", key=f"wfd-cont-{p}-{step.id}", type="primary"):
        _do(flow.answer, s, p, states, step.id, {}, ok=f"{step.id} done")


def checklist_card(s, p: str, states: dict, st_: dict, step, row: dict) -> None:
    done = set(row.get("items_done") or [])
    if step.id == "7.3":
        notes = Files(s, p).read(f"review/{p}/deploy.md") or s.read_base(f"review/{p}/deploy.md")
        if notes:
            with st.expander("deploy.md — the exact commands for this pipeline", expanded=True):
                st.markdown(notes)
    st.caption("The app cannot do these itself. Tick each item when done; who ticked it and when goes into the workflow log.")
    for item in step.items:
        val = st.checkbox(item, value=item in done, key=f"wfd-chk-{p}-{step.id}-{abs(hash(item))}")
        if val != (item in done):
            _do(flow.mark_done, s, p, states, step.id, item, val, ok=None)
    if step.id == "6.2" and not done:
        if st.button("Not applicable (no DAG to replace) →", key=f"wfd-na-{p}"):
            _do(flow.mark_done, s, p, states, step.id, None, True, ok="noted")


def download_card(s, p: str, states: dict, st_: dict, step) -> None:
    from .workflow import artifacts

    st.caption("The zip has every artefact of the pipeline, the approvals ledger, the job logs and the workflow log of this run.")
    st.download_button("⬇ Download artifacts (zip)", data=lambda: artifacts(s, p), file_name=f"{p}-artifacts.zip", mime="application/zip",
                       key=f"wfd-dl-{p}", on_click=lambda: st.session_state.__setitem__(f"wfd-dl-done-{p}", True))
    if st.button("Downloaded, continue →", key=f"wfd-cont-{p}-72", type="primary"):
        _do(flow.answer, s, p, states, step.id, {"detail": "artifacts downloaded"}, ok="continuing")


def job_wait_card(s, p: str, states: dict, st_: dict, step, row: dict) -> None:
    sub = st_.get("sub") or {}
    running = busy(s, p)
    if step.id == "6.5" and not sub.get("preview"):
        st.markdown("A second run must deliver nothing (everything was delivered by the first run). Optional.")
        c = st.columns(2)
        if c[0].button("Run again", key=f"wfd-run-again-{p}", type="primary", disabled=running):
            _do(flow.answer, s, p, states, step.id, {}, ok="second run started")
        if c[1].button("Skip", key=f"wfd-skip65-{p}", disabled=running):
            _refresh_do(flow.skip, s, p, _fresh(s, p), "second run not needed", ok="6.5 skipped")
        return
    pv = sub.get("preview") or {}
    if pv:
        st.markdown(f"**Preview:** {pv.get('files', 0)} file(s) in {pv.get('packages', 0)} package(s); "
                    f"{pv.get('deletes', 0)} source object(s) would be deleted" + (f" in `{pv['source_bucket']}`" if pv.get("source_bucket") else "") +
                    (f"; {pv['error_files']} routed to error/" if pv.get("error_files") else ""))
        if pv.get("by_prefix"):
            st.dataframe([{"target prefix": k, "files": v} for k, v in pv["by_prefix"].items()], hide_index=True)
        planned = Path(str(pv.get("planned") or ""))
        if planned.is_file():
            st.download_button("Download the full list (planned.json)", planned.read_bytes(), file_name=f"{p}-{pv.get('run')}-planned.json",
                               mime="application/json", key=f"wfd-dl-planned-{p}")
        ok = True
        if pv.get("deletes"):
            ok = st.checkbox(f"I understand {pv['deletes']} source object(s) will be deleted", key=f"wfd-del-ok-{p}")
        c = st.columns(2)
        if c[0].button(f"Move {pv.get('files', 0)} files", key=f"wfd-move-{p}", type="primary", disabled=running or not ok or not pv.get("files")):
            _do(flow.answer, s, p, states, step.id, {"planned": str(pv.get("planned"))}, ok="move started")
        if c[1].button("Preview again", key=f"wfd-preview-again-{p}", disabled=running):
            _do(flow.preview_again, s, p, states, step.id, ok="previewing again")
        if not pv.get("files") and step.id == "6.3":
            test_package_box(s, p, running)


def test_package_box(s, p: str, running: bool) -> None:
    """6.3 found nothing to move (0.8.2): Prepare a test package (the Studio places one of the pipeline's examples in its
    dev source), or — when it could not — the commands to do it yourself, then Preview again."""
    from .. import testdata

    st_ = flow.load(s.root, p) or {}
    au = st_.get("auto") or {}
    with st.container(border=True, key="ws-testpkg"):
        st.markdown("**Nothing to move.** The source has no file the pipeline would pick up.")
        try:
            pl = testdata.plan(s.read_base, p, s.cfg.mode)
        except Exception as exc:  # noqa: BLE001
            pl = {"ok": False, "problem": err_text(exc)}
        if pl.get("ok"):
            st.caption(f"Test package from example **{md_escape(str(pl['example']))}**: {len(pl['objects'])} object(s) → "
                       f"`s3://{pl['bucket']}/` (never overwrites; tagged pkf-test-data=1)")
            if st.button("Prepare a test package", key=f"wfd-testpkg-{p}", disabled=running or s.strict,
                         help="places the example's objects in the pipeline's dev source with your AWS credentials, then Preview again"):
                from .common import start_job

                start_job(s, p, "stage_test_data", {}, "6.3 Prepare a test package")
                st.rerun()
        else:
            st.caption(md_escape(str(pl.get("problem") or "")))
        failed = au.get("stage_failed")
        recent = jobs.recent(s.root, p, 1)
        lastjob = recent[0] if recent else {}
        res = lastjob.get("result") or {}
        if lastjob.get("action") == "stage_test_data" and isinstance(res, dict) and res.get("ok") is False and res.get("commands"):
            failed = {"problem": res.get("problem"), "commands": res.get("commands"), "upload_sh": res.get("upload_sh")}
        if failed and not failed.get("commands"):
            st.warning("The Studio could not prepare a test package: " + md_escape(str(failed.get("problem") or "")) +
                       ". Place a file in the source yourself, then **Preview again**.")
        elif failed:
            st.warning("The Studio could not place the test package: " + md_escape(str(failed.get("problem") or "")) +
                       ". Run these in Git Bash with your AWS credentials, then **Preview again**:")
            st.code("\n".join(failed.get("commands") or []), language="bash", wrap_lines=True)
            sh = Path(str(failed.get("upload_sh") or ""))
            if sh.is_file():
                st.download_button("Download upload.sh", sh.read_bytes(), file_name=f"{p}-upload.sh", mime="text/x-shellscript",
                                   key=f"wfd-testpkg-sh-{p}")


def full_auto_line(st_: dict | None) -> None:
    """What Full auto did last, or why it stopped (0.8.2)."""
    au = (st_ or {}).get("auto") or {}
    if not (st_ or {}).get("full_auto"):
        return
    if au.get("needs_you"):
        st.warning(f"**Full auto stopped:** {md_escape(str(au['needs_you']))}")
    elif au.get("log"):
        st.caption("Full auto: " + md_escape(str(au["log"][-1])))


# ------------------------------------------------------------------------------------------------ assistant
LANE_PHASES = ("ask", "diagnose", "brief")


def lanes() -> bool:
    """The assistant lane exists (0.8.0 jobs: ask, diagnose and the likely cause run beside a pipeline job)."""
    from .shell import _has_lanes

    return _has_lanes()


def lane_job(s, p: str) -> str | None:
    return jobs.current(s.root, p, lane="assistant") if lanes() else None


def job_phase(s, jid: str | None) -> str | None:
    """The assistant phase a job runs (its cmd.json args; jobs.status does not carry the args)."""
    if not jid:
        return None
    try:
        return (json.loads((jobs.jobs_dir(s.root) / jid / "cmd.json").read_text(encoding="utf-8")).get("args") or {}).get("phase")
    except (OSError, ValueError, AttributeError):
        return None


def start_assist(s, p: str, phase: str, rerun: bool = True, states: dict | None = None, **args) -> None:
    """Start an assistant job. ask / diagnose / brief go to the assistant's read-only lane when it exists (they never
    wait for a pipeline job); fix stays on the pipeline's lock as in 0.6.1 (it edits files and runs tests)."""
    from .common import start_job

    title = {"diagnose": "🤖 Assistant: diagnose", "fix": "🤖 Assistant: prepare the fix", "ask": "🤖 Assistant: answer",
             "brief": "🤖 Assistant: likely cause"}[phase]
    lane = "assistant" if phase in LANE_PHASES and lanes() else None
    if lane and states:                 # the lane opens no Studio (no derive): it gets the gates the page already has
        from ..assistant.brief import gate_states

        args["gates"] = gate_states(states) or None
    start_job(s, p, "assist", {"phase": phase, **{k: v for k, v in args.items() if v is not None}}, title, lane=lane)
    if rerun:
        st.rerun()


def _assist_sessions(s, p: str) -> list[Path]:
    d = flowstore.flow_dir(s.root, p) / "assistant"
    return sorted([x for x in d.iterdir() if x.is_dir()], key=lambda x: x.name, reverse=True) if d.is_dir() else []


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def assistant_state(s, p: str) -> dict:
    """What the assistant band shows: the running job, the latest session and its diagnosis / fix / summary."""
    running = jobs.current(s.root, p)
    job = jobs.status(s.root, running) if running else None
    lane = lane_job(s, p)
    lane_phase = job_phase(s, lane)
    sessions = _assist_sessions(s, p)
    latest = sessions[0] if sessions else None
    # a likely-cause job (phase brief, seconds, read-only) does not hold up approving or applying a fix
    a = {"running": running, "assist_running": bool(job and job.get("action") == "assist") or bool(lane and lane_phase != "brief"),
         "lane": lane, "lane_phase": lane_phase,
         "lanes": lanes(), "sessions": sessions, "latest": latest, "diag": None, "fix": None, "summary": {}, "sid": None}
    if latest is not None:
        a.update(diag=_read_json(latest / "diagnosis.json"), fix=_read_json(latest / "fix.json"),
                 summary=_read_json(latest / "summary.json") or {}, sid=latest.name)
        st.session_state[f"wfd-assist-sid-{p}"] = latest.name
    return a


def assistant_conversation(s, p: str, states: dict, st_: dict | None, a: dict, expanded: bool = False) -> None:
    """The conversation half of the assistant: the question, the plain-language answer (*In short*), the diagnosis with
    its evidence and proposal, the proposed fix (diff) and the earlier sessions. The buttons are in assistant_decisions."""
    diag, fix, summary, latest = a["diag"], a["fix"], a["summary"], a["latest"]
    if a["assist_running"]:
        st.caption("working… (console: Assistant transcript)")
    if latest is None:
        st.caption("Ask anything about this pipeline or the step it is in, or Diagnose the current step: the assistant knows the "
                   "framework, reads the codebase, the artefacts and every job's log.")
        return
    if summary.get("question"):
        st.markdown(f"**You:** {md_escape(str(summary['question']))}")
    if summary.get("answer") and not diag:
        from ..assistant.protocol import split_answer

        short, detail = (summary.get("plain"), summary.get("detail")) if summary.get("plain") else split_answer(summary["answer"])
        if expanded:
            st.markdown("**In short**")
            st.info(md_escape(short or summary["answer"][:400]))
        else:
            st.markdown(f"**In short:** {md_escape(short or summary['answer'][:400])}")
        with st.expander("The details", expanded=expanded):
            st.markdown(detail or summary["answer"])
    if diag and not expanded:            # the 15 % band: the gist first, the rest one click away
        from ..assistant.protocol import plain_of

        st.markdown(f"**In short:** {md_escape(str(diag.get('summary') or plain_of(diag.get('root_cause'))))}")
        st.markdown(f"**Root cause:** {md_escape(str(diag.get('root_cause', '')))} **Proposal:** {md_escape(str(diag.get('proposal', '')))}"
                    + (f" Touches {', '.join(f'`{x}`' for x in (diag.get('files') or [])[:4])}." if diag.get("files") else ""))
        if diag.get("evidence"):
            st.caption("Evidence: " + " · ".join(md_escape(str(e)) for e in diag["evidence"][:6]))
    if diag and expanded:
        from ..assistant.protocol import plain_of

        st.markdown("**In short**")
        st.info(md_escape(str(diag.get("summary") or plain_of(diag.get("root_cause")))))
        st.markdown("**Diagnosis (details)**")
        risk = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(str(diag.get("risk")), "")
        st.markdown(f"**Root cause:** {md_escape(str(diag.get('root_cause', '')))}  \n"
                    f"**Fix kind:** `{diag.get('fix_kind')}` · risk {risk} {diag.get('risk', '')} · confidence {diag.get('confidence', '')}")
        if diag.get("evidence"):
            with st.expander("Evidence", expanded=expanded):
                for e in diag["evidence"][:12]:
                    st.markdown("- `" + str(e).replace("`", "'") + "`")
        st.markdown(f"**Proposal:** {md_escape(str(diag.get('proposal', '')))}")
    if diag:
        if diag.get("needs_reopen"):
            st.warning(f"This fix changes something {diag['needs_reopen']} approved: after applying it, Reopen {diag['needs_reopen']} and approve again.")
        kind = diag.get("fix_kind")
        if kind == "user_action":
            st.info("**Do this yourself:** " + md_escape(str(diag.get("user_action", ""))))
        elif kind == "studio_action":
            st.info(f"**Use the Studio button:** {md_escape(str(diag.get('studio_action', '')))} (Workflow tab or this card), then Retry.")
        elif kind == "settings":
            st.info("**Setting to change:** " + md_escape(str(diag.get("user_action") or diag.get("proposal", ""))))
        elif kind == "rerun":
            st.info("Nothing to change: Retry the step.")
        elif kind == "edit_files" and not fix and expanded:
            st.markdown("Files it will touch: " + ", ".join(f"`{x}`" for x in (diag.get("files") or [])[:10]))
    if fix:
        st.markdown("**Proposed fix** — nothing is committed until it is applied")
        if fix.get("plain_summary"):
            st.info(md_escape(str(fix["plain_summary"])))
        st.markdown(md_escape(str(fix.get("summary", ""))))
        t = fix.get("tests") or {}
        (st.success if t.get("ok") else st.warning)(f"Tests: {'pass' if t.get('ok') else 'not passing / not run'} — {md_escape(str(t.get('detail', '')))[:400]}")
        if fix.get("diff"):
            if expanded:
                st.code(fix["diff"][:20000], language="diff", height=320)
            else:
                with st.expander("The diff (⛶ shows it larger)"):
                    st.code(fix["diff"][:20000], language="diff", height=320)
    with st.expander("Assistant sessions", expanded=False):
        for d in a["sessions"][:10]:
            sm = _read_json(d / "summary.json") or {}
            st.markdown(f"- `{d.name}` · {sm.get('phase', '')} · {md_escape(str(sm.get('root_cause') or sm.get('question') or sm.get('summary') or ''))[:120]}")


def assistant_decisions(s, p: str, states: dict, st_: dict | None, a: dict) -> None:
    """The decision half of the assistant: what it does on its own (pilot), the diagnosis' actions with Do it, Approve the
    fix plan / Dismiss, Apply the fix / Discard. Same buttons, keys and functions as in 0.6.1."""
    diag, fix, summary, latest, sid = a["diag"], a["fix"], a["summary"], a["latest"], a["sid"]
    running, assist_running = a["running"], a["assist_running"]
    pilot_box(st_)
    if latest is None:
        return
    if diag:
        if diag.get("fix_kind") == "edit_files" and not fix:
            risk = {"low": "green", "medium": "orange", "high": "red"}.get(str(diag.get("risk")), "grey")
            c = st.columns([2.3, 2.6, 1.3], vertical_alignment="center")
            c[0].markdown(" ".join([chip_html(str(diag.get("fix_kind")), "grey"), chip_html(f"risk {diag.get('risk', '?')}", risk),
                                    chip_html(str(diag.get("confidence", "")), "grey")]), unsafe_allow_html=True)
            if c[1].button("✓ Approve the fix plan", key=f"wfd-fix-go-{p}", type="primary", disabled=assist_running or bool(running)):
                start_assist(s, p, "fix", session_id=sid)
            if c[2].button("Dismiss", key=f"wfd-fix-no-{p}"):
                flowstore.event(s.root, p, "assistant_fix_discarded", session=sid, why="plan dismissed")
                (latest / "diagnosis.dismissed").write_text("1", encoding="utf-8", newline="\n")
                st.rerun()
        actions_box(s, p, states, st_, diag, sid, fix, summary, assist_running or bool(running))
    if fix:
        if not summary.get("applied") and not summary.get("discarded"):
            c = st.columns(2)
            if c[0].button("✓ Apply the fix (commit)", key=f"wfd-apply-{p}", type="primary", disabled=bool(running)):
                def apply_():
                    from ..assistant import protocol

                    sha = protocol.apply_fix(s, p, sid)
                    refresh(s)
                    return sha
                act(apply_, ok=lambda sha: f"fix committed {str(sha)[:7]} — Retry the step")
            if c[1].button("Discard the fix", key=f"wfd-discard-{p}", disabled=bool(running)):
                from ..assistant import protocol

                _refresh_do(protocol.discard_fix, s, p, sid, ok="fix discarded")
        elif summary.get("applied"):
            st.success(f"Applied as commit {str(summary.get('sha', ''))[:7]}. Retry the failed step.")


def ask_controls(s, p: str, st_: dict | None, a: dict, *, ask_col=None, diag_col=None, states: dict | None = None) -> None:
    """Ask (a form: Enter submits, leaving the field does not start a job) and Diagnose the current step. Keys as 0.6.1."""
    running, assist_running = a["running"], a["assist_running"]
    # the lane: a pipeline job does not block asking; one lane job at a time (a likely cause takes seconds)
    blocked = assist_running or bool(a.get("lane")) or (bool(running) and not a.get("lanes"))
    cur = flow.BY_ID.get((st_ or {}).get("cursor") or "")
    row = _step_row(st_, cur.id) if cur else {}
    if ask_col is None or diag_col is None:
        ask_col, diag_col = st.columns([7, 3], vertical_alignment="bottom")
    with ask_col:
        with st.form(f"wfd-ask-form-{p}", clear_on_submit=False, border=False):
            fc = st.columns([8, 1.3], vertical_alignment="bottom")
            q = fc[0].text_input("Ask anything about this pipeline or the stage it is in", key=f"wfd-ask-{p}", label_visibility="collapsed",
                                 placeholder="Ask anything about this pipeline or step — e.g. why is 4.1 slow? what did 3.3 produce? "
                                             "how do I fix the failing example?")
            sent = fc[1].form_submit_button("Ask", type="primary", disabled=blocked, width="stretch")
        if sent and (q or "").strip():
            start_assist(s, p, "ask", question=q.strip(), session_id=st.session_state.get(f"wfd-assist-sid-{p}"), states=states)
    with diag_col:
        if st.button(f"Diagnose {cur.id}" if cur else "Diagnose the current step", key=f"wfd-diag-btn-{p}",
                     disabled=blocked or not cur,
                     help="finds the root cause of the failed or stuck step and proposes a fix"):
            start_assist(s, p, "diagnose", step_id=cur.id if cur else None, job_id=row.get("job_id"), states=states)


def chip_html(text: str, color: str = "grey") -> str:
    from .theme import chip

    return chip(text, color)


PILOT_ICON = {"diagnosing": "🔎", "acting": "⚙️", "fixing": "🛠️", "done": "✅", "needs_you": "🙋", "gave_up": "🙋"}


def pilot_box(st_: dict | None) -> None:
    """What the assistant is doing on its own (0.6.1)."""
    if not st_ or not st_.get("assistant_autopilot"):
        return
    pl = st_.get("pilot") or {}
    if not pl.get("phase"):
        st.caption("Assistant can act: on. When a step fails it diagnoses and acts on its own; reviews and approvals stay with you.")
        return
    icon = PILOT_ICON.get(pl["phase"], "🤖")
    text = md_escape(str(pl.get("note") or pl["phase"]))
    (st.warning if pl["phase"] in ("needs_you", "gave_up") else st.success if pl["phase"] == "done" else st.info)(f"{icon} **On its own:** {text}")
    if pl.get("log"):
        with st.expander("What the assistant did", expanded=False):
            for x in pl["log"][-12:]:
                st.markdown(f"- {md_escape(str(x))}")


def actions_box(s, p: str, states: dict, st_: dict | None, diag: dict, sid: str, fix: dict | None, summary: dict, busy_now: bool) -> None:
    """The diagnosis' actions with a Do it button each (when the assistant does not act on its own, or to do one now)."""
    acts = [a for a in (diag.get("actions") or flow.default_actions(diag, states)) if isinstance(a, dict) and a.get("do")]
    if not acts or summary.get("applied") or summary.get("discarded"):
        return
    st.markdown("**What the assistant suggests doing**")
    for i, a in enumerate(acts):
        label = pilot._label(a)
        c = st.columns([7, 2])
        why = None if a.get("do") == "fix" else flow.assistant_may(s, p, states, st_, a)
        c[0].markdown(f"{i + 1}. {md_escape(label)}" + (f" — {md_escape(str(a.get('why')))}" if a.get("why") else "")
                      + (f"  \n   *a person does this: {md_escape(why)}*" if why else ""))
        if a.get("do") == "fix":
            if not fix and c[1].button("Do it", key=f"wfd-act-{p}-{sid}-{i}", disabled=busy_now):
                start_assist(s, p, "fix", session_id=sid)
            continue
        if c[1].button("Do it", key=f"wfd-act-{p}-{sid}-{i}", disabled=busy_now or bool(why), help=why):
            _refresh_do(flow.assistant_act, s, p, _fresh(s, p), a, derive=lambda: _fresh(s, p), ok=lambda m: str(m))


def assistant_lines(s, p: str, n: int = 300) -> list[dict]:
    sessions = _assist_sessions(s, p)
    if not sessions:
        return []
    out = []
    for d in sessions[:2][::-1]:
        tr = d / "transcript.jsonl"
        try:
            rows = tr.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            continue
        for ln in rows:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            kind = e.get("kind", "")
            lvl = "error" if kind == "error" else "tool" if kind in ("tool_use", "tool_result") else "agent"
            name = f"[{e.get('name')}] " if e.get("name") else ""
            out.append({"ts": flow._hms(e.get("ts")), "level": lvl, "source": "assistant", "text": f"{kind}: {name}{e.get('text', '')}"[:2000]})
    return out[-n:]


# ------------------------------------------------------------------------------------------------ render
def drive_id() -> str:
    """This browser session's id for the drive lease (drive.py)."""
    import uuid

    return st.session_state.setdefault("_drive_id", uuid.uuid4().hex)


def _advance(s, p: str, states: dict, st_: dict | None, warnings: list[str]) -> dict | None:
    """The run's tick, the assistant's own actions (pilot) and Full auto (0.8.2) — by the one tab that holds the drive
    lease (the others only read). Never raises."""
    from .. import autorun, drive

    if not drive.acquire(s.root, p, drive_id()):
        return st_
    st_ = _advance_run(s, p, states, st_, warnings)
    if autorun.enabled(s, st_):
        try:
            if autorun.tick(s, p, _states(s, p, states, st_)):
                st_ = flow.load(s.root, p) or st_
                st.cache_data.clear()
        except Exception as exc:  # noqa: BLE001 - Full auto's trouble never takes the page down
            warnings.append(f"Full auto could not decide just now: {err_text(exc)}")
    return st_


def _advance_run(s, p: str, states: dict, st_: dict | None, warnings: list[str]) -> dict | None:
    """The run's tick and the assistant's own actions (pilot), as in 0.6.1/0.7.0. Never raises."""
    if st_ and st_.get("status") in ACTIVE:
        try:
            st_ = flow.tick(s, p, _states(s, p, states, st_)) or st_
        except Exception as exc:  # noqa: BLE001 - a tick must never take the page down
            warnings.append(f"The run could not advance just now: {err_text(exc)}")
    if st_ and st_.get("assistant_autopilot") and (pilot.pending(st_) or (st_.get("pilot") or {}).get("phase") in ("diagnosing", "acting", "fixing")):
        try:
            before = json.dumps(st_.get("pilot") or {}, sort_keys=True, default=str)
            pilot.tick(s, p, _states(s, p, states, st_), derive=lambda: _fresh(s, p))
            st_ = flow.load(s.root, p) or st_
            if json.dumps(st_.get("pilot") or {}, sort_keys=True, default=str) != before:
                st.cache_data.clear()
        except Exception as exc:  # noqa: BLE001 - the assistant's trouble never takes the page down
            warnings.append(f"The assistant could not act just now: {err_text(exc)}")
    return st_


def panel(s, p: str, states: dict, artifacts_page=None) -> None:
    """The Workspace (0.8.0, ST-3 v3 F): the stepper, the stage header (Request approval, ↩ Go back), the substage rail,
    Your turn, Before Request approval, Components, Files of this stage, and the console. Runs inside the 2-second live
    fragment while the run is active."""
    from . import workspace

    warnings: list[str] = []
    st_ = _advance(s, p, states, flow.load(s.root, p), warnings)
    workspace.watch_job(p, jobs.current(s.root, p))
    _bar_watch(p, st_)
    sel_key = f"wfd-sel-{p}"
    snap = flow.snapshot(s, p, states, st_, st.session_state.get(sel_key))
    stage = workspace.stepper(s, p, st_, states, snap)
    head = st.container()                      # the header is drawn after the checklist (it shows the open count)
    workspace.rail(s, p, st_, snap, stage)
    full_auto_line(st_)
    run_stage = workspace.cursor_stage(st_, states)
    if stage == run_stage:
        if st_ is None or st_.get("status") == "idle":
            with st.container(border=True, key="ws-turn"):
                intake(s, p, states)
        elif not _running_now(s, p, st_):          # while the step's job runs, its rail card shows it
            with st.container(border=True, key="ws-turn"):
                action_card(s, p, states, st_)
    left, right = st.columns([6, 4], gap="medium")
    with left:
        n_open = workspace.gate_card(s, p, states, st_, stage, warnings)
    with right:
        workspace.components_card(s, p, states, st_, stage)
    with head:
        workspace.stage_header(s, p, states, st_, snap, stage, n_open)
    workspace.files_strip(s, p, stage, artifacts_page)
    sel = st.session_state.get(sel_key) or (st_ or {}).get("cursor")
    workspace.console(s, p, st_, sel)


def full_panel(s, p: str, states: dict, artifacts_page=None) -> None:
    """The assistant full screen; the run keeps advancing underneath (the same tick as the Workspace)."""
    from . import assistant_ui, workspace

    warnings: list[str] = []
    _advance(s, p, states, flow.load(s.root, p), warnings)
    workspace.watch_job(p, jobs.current(s.root, p))
    for w in warnings:
        st.warning(w)
    assistant_ui.full_view(s, p, states)


def _running_now(s, p: str, st_: dict) -> bool:
    row = _step_row(st_, st_.get("cursor") or "")
    return st_.get("status") == "running" and row.get("status") == "running" and jobs.current(s.root, p) is not None


def _bar_watch(p: str, st_: dict | None) -> None:
    """The context bar (Stop run / Resume, Run ▾, the run chip) is drawn before the run ticks: when the run's status is not
    what the bar drew — the tick of this run or a later 2-s refresh moved it on — rerun the whole app once so the bar
    follows. Bounded: the next run's bar draws the status it finds."""
    from .shell import bar_key

    drew = st.session_state.get(bar_key(p))
    now = (st_ or {}).get("status") or "idle"
    if drew is not None and drew != now:
        st.session_state[bar_key(p)] = now          # at most one rerun per change, even if the bar is not drawn again
        st.rerun(scope="app")


def render_assistant(s, p: str, states: dict) -> None:
    """The assistant's own tab (0.8.2): refreshed every 2 s while the run or an assistant job is active. It advances the
    run only while it holds the drive lease (drive.py: one tab at a time — e.g. when the Workspace tab is in the
    background and the browser slows its timer)."""
    st_ = flow.load(s.root, p)
    active = bool(st_ and st_.get("status") in ACTIVE) or jobs.current(s.root, p) is not None or lane_job(s, p) is not None \
        or pilot.pending(st_)
    st.fragment(run_every=2 if active else None)(_assistant_tab)(s, p, states)


def _assistant_tab(s, p: str, states: dict) -> None:
    from . import assistant_ui

    warnings: list[str] = []
    _advance(s, p, states, flow.load(s.root, p), warnings)
    for w in warnings:
        st.warning(w)
    assistant_ui.tab_view(s, p, states)


def render(s, p: str, states: dict, artifacts_page=None) -> None:
    st_ = flow.load(s.root, p)
    active = bool(st_ and st_.get("status") in ACTIVE) or jobs.current(s.root, p) is not None or pilot.pending(st_)
    if st.session_state.get(f"ws-full-{p}"):
        active = active or lane_job(s, p) is not None
        st.fragment(run_every=2 if active else None)(full_panel)(s, p, states, artifacts_page)
        return
    st.fragment(run_every=2 if active else None)(panel)(s, p, states, artifacts_page)
