"""The Pipelines page (0.7.0, P5): every pipeline at a glance, then the chosen pipeline's seven tabs exactly as in 0.6.1
(Requirements · Source · Destination · Trigger · Transformation · Pipeline · Workflow) under its job panel."""
from __future__ import annotations

import streamlit as st

from .. import flow, jobs
from ..stages import STAGES
from . import common, components, pipeline, requirements, workflow
from .common import md_escape

TABS = ["Requirements", "Source", "Destination", "Trigger", "Transformation", "Pipeline", "Workflow"]
RUN_WORD = {"idle": "no run", "running": "running", "waiting_input": "waiting for input", "waiting_gate": "waiting for approval",
            "waiting_user": "waiting for you", "failed": "failed", "done": "complete", "stopped": "stopped"}


def overview(s, current: str | None, states_of) -> None:
    """One row per pipeline: stage, run, running job, open PR; Open in Workspace / Open here."""
    from .shell import open_pipeline

    names = s.pipelines()
    with st.expander(f"All pipelines · {len(names)}", expanded=not current):
        if not names:
            st.caption("No pipelines yet: choose ➕ New pipeline in the selector above.")
            return
        head = st.columns([3, 3, 2, 3, 1.4, 1.6, 1.4])
        for c, h in zip(head, ["pipeline", "stage", "run", "running job", "PR", "", ""]):
            c.caption(h)
        for name in names:
            try:
                states, _ = states_of(s, name)
                stage = STAGES.get(states.get("current"), states.get("current"))
                waiting = [g for g in states["gates"] if g.state == "waiting" and g.pr]
                pr = f"#{waiting[0].pr['number']}" if waiting else "—"
            except Exception:  # noqa: BLE001 - one pipeline's trouble never hides the others
                stage, pr = "?", "?"
            st_ = flow.load(s.root, name) or {}
            jid = jobs.current(s.root, name)
            job = (jobs.status(s.root, jid) or {}).get("title") if jid else ""
            c = st.columns([3, 3, 2, 3, 1.4, 1.6, 1.4], vertical_alignment="center")
            c[0].markdown(("**" if name == current else "") + md_escape(name) + ("**" if name == current else ""))
            c[1].markdown(md_escape(str(stage)))
            c[2].markdown(RUN_WORD.get(st_.get("status") or "idle", st_.get("status") or ""))
            c[3].markdown(("▶ " + md_escape(job)) if job else "—")
            c[4].markdown(pr)
            c[5].button("Workspace", key=f"ov-ws-{name}", on_click=open_pipeline, args=(name, "Workspace"))
            c[6].button("Open", key=f"ov-open-{name}", on_click=open_pipeline, args=(name, "Pipelines"),
                        disabled=name == current)


def render(s, p: str, states: dict) -> None:
    """The chosen pipeline: title, job panel, the seven tabs (lazy: only the open one renders, as in 0.6.1)."""
    with st.container(key="pl-title"):
        st.title(p)
    common.job_panel(s, p)  # the running job's live log, whichever tab is open
    if st.session_state.get("tab") not in TABS:          # back from another page (or "Workflow Developer", now the Workspace)
        st.session_state["tab"] = st.session_state.get("_pl-tab") if st.session_state.get("_pl-tab") in TABS else TABS[0]
    tabs = st.tabs(TABS, key="tab", on_change="rerun")
    st.session_state["_pl-tab"] = st.session_state.get("tab")
    renderers = {
        "Requirements": lambda: requirements.render(s, p, states),
        "Source": lambda: components.render(s, p, "source", states),
        "Destination": lambda: components.render(s, p, "destination", states),
        "Trigger": lambda: components.render(s, p, "trigger", states),
        "Transformation": lambda: components.render(s, p, "transformation", states),
        "Pipeline": lambda: pipeline.render(s, p, states),
        "Workflow": lambda: workflow.render(s, p, states),
    }
    for label, tab in zip(TABS, tabs):
        if tab.open:
            with tab:
                try:
                    renderers[label]()
                except Exception as exc:  # noqa: BLE001 - one tab's trouble (e.g. GitHub gone mid-render) is never a traceback
                    st.error(f"This tab could not be shown completely: {common.err_text(exc)}")
                    if st.button("Retry", key=f"retry-tab-{label}"):
                        common.act(common.refresh, s, reset=True)
