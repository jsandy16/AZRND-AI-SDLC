"""The assistant (0.8.0): the start-up brief, the likely cause, the conversation, the decisions and Ask — in a pop-up
dialog (sidebar → Open) or full screen (sidebar → ⤢). Both call the same 0.6.1 functions as before (workflow_dev's
assistant_conversation / assistant_decisions / ask_controls; the buttons keep their keys), so nothing about what the
assistant may do changed: it edits only this pipeline's files, never the framework (assistant.tools.check_write).

The brief is deterministic (assistant.brief.build: no model call). The *likely cause* is one short model call in the
assistant's read-only lane (phase "brief"), started on its own when something is wrong and cached per brief, so opening
the assistant twice does not pay twice."""
from __future__ import annotations

import time

import streamlit as st

from .. import flow, jobs
from . import theme
from .common import elapsed, md_escape, since


def _brief(s, p: str, states: dict | None, st_: dict | None) -> dict:
    from ..assistant import brief

    return brief.build(s, p, states, st_, n_log=6)


CAUSE_EVERY_S = 600          # at most one automatic likely-cause call per pipeline per 10 minutes (a running job's log moves)


def _stored_cause(s, p: str) -> dict | None:
    """The last likely cause written for the pipeline, whatever brief it was for."""
    import json

    from ..assistant import brief

    try:
        return json.loads(brief.cause_path(s.root, p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _start_cause(s, p: str, states: dict | None = None) -> bool:
    from .workflow_dev import lane_job, lanes, start_assist

    if not lanes() or lane_job(s, p):
        return False
    try:
        start_assist(s, p, "brief", rerun=False, states=states)
    except Exception:  # noqa: BLE001 - the likely cause is a nicety: never take the assistant down
        return False
    st.session_state[f"_as-cause-at-{p}"] = time.monotonic()
    return True


def _ensure_cause(s, p: str, b: dict, states: dict | None = None) -> tuple[dict | None, bool]:
    """(likely cause, fresh?): the cause cached for this very brief, else the last one written (stale) — and, when there
    is an issue, start the lane job that works it out again (throttled: once per brief, at most every 10 minutes)."""
    from ..assistant import brief

    if not brief.needs_cause(b):
        return None, False
    hit = brief.cached_cause(s.root, p, b)
    if hit:
        return hit, True
    key = f"_as-cause-{p}-{brief.cause_hash(b)}"
    last = st.session_state.get(f"_as-cause-at-{p}")
    if b.get("issue") and not st.session_state.get(key) and (last is None or time.monotonic() - last > CAUSE_EVERY_S):
        st.session_state[key] = True
        if _start_cause(s, p, states):
            st.rerun()                 # once (the key above): the next run sees the lane job and refreshes every 2 s
    return _stored_cause(s, p), False


def brief_card(s, p: str, states: dict | None, st_: dict | None) -> None:
    """Where things stand, in the structured form the user asked for: Done / Now / Next / Issue / Blocking / Log, then
    the likely cause (one model call, cached)."""
    from .workflow_dev import job_phase, lane_job, lanes

    b = _brief(s, p, states, st_)
    rows = []
    done = b.get("done") or []
    rows.append(("Done", " · ".join(f"{d['stage']} ✓" + (f" {d['gate']}" if d.get("gate") else "") for d in done) or "nothing yet"))
    now = b.get("now")
    if now:
        t = f" · {elapsed(now['elapsed_s'])}" if now.get("elapsed_s") is not None else ""
        rows.append(("Now", f"{now.get('stage') or ''} · {now.get('step') or ''} {now.get('title') or ''} — {now.get('status') or ''}{t}"
                     + (f" · {now['detail']}" if now.get("detail") else "")))
    else:
        rows.append(("Now", b.get("message") or "no run"))
    nxt = b.get("next") or []
    rows.append(("Next", " → ".join(f"{n.get('step') or ''} {n.get('title')} ({n.get('who')})".strip() for n in nxt[:3]) or "—"))
    issue = b.get("issue")
    rows.append(("Issue", (f"{issue.get('title')}" + (f": {issue['detail']}" if issue.get("detail") else "")
                           + (f" · step {issue['step']}" if issue.get("step") else "")) if issue else "none"))
    rows.append(("Blocking", " · ".join(b.get("blocking") or []) or "nothing"))
    if b.get("pilot"):
        rows.append(("On its own", f"{b['pilot'].get('phase')}: {b['pilot'].get('note') or ''}".rstrip(": ")))
    au = (st_ or {}).get("auto") or {}
    if (st_ or {}).get("full_auto"):
        rows.append(("Full auto", f"stopped: {au['needs_you']}" if au.get("needs_you") else (au.get("log") or ["on"])[-1]))
    body = "".join(f"<div><b>{theme.esc(k)}</b>{theme.esc(str(v)[:400])}</div>" for k, v in rows)
    with st.container(border=True, key="as-brief"):
        st.html('<div style="display:flex;justify-content:space-between;align-items:center"><b>Where things stand</b>'
                f'<span class="pkf-muted">{theme.esc(b.get("pipeline", p))} · {theme.esc(b.get("mode", ""))} · run {theme.esc(b.get("run_status", ""))}</span></div>'
                f'<div class="pkf-brief">{body}</div>')
        cause, fresh = _ensure_cause(s, p, b, states)
        working = job_phase(s, lane_job(s, p)) == "brief"
        if cause and (cause.get("cause") or cause.get("next_step")):
            conf = cause.get("confidence")
            st.markdown(f"**Likely cause{'' if fresh else ' (from earlier)'}:** {md_escape(cause.get('cause') or '')}  \n"
                        f"**Next:** {md_escape(cause.get('next_step') or '')}"
                        + (f"  \n<span class='pkf-muted'>confidence {float(conf):.0%}</span>" if isinstance(conf, (int, float)) else ""),
                        unsafe_allow_html=True)
        if working:
            st.html('<span class="pkf-spin"></span><span class="pkf-muted">working out the likely cause…</span>')
        elif (b.get("issue") or b.get("blocking")) and not fresh and lanes():
            if st.button("Work out the likely cause" if not cause else "Update the likely cause", key=f"as-cause-{p}", type="tertiary",
                         help="one short model call (Opus 5.5, low effort) in the assistant's read-only lane"):
                if _start_cause(s, p, states):
                    st.rerun()
        if b.get("log_tail"):
            with st.expander("Last log lines"):
                st.code("\n".join(b["log_tail"]), language="text")
        if b.get("memory"):
            with st.expander(f"Pipeline notes ({len(b['memory'])})"):
                for n in b["memory"]:
                    st.markdown(f"- {md_escape(n)}")


def _activity(s, p: str, a: dict) -> None:
    """What the assistant is doing now (its job in the lane or on the pipeline) with Cancel — or, when its last lane job
    failed or ended with problems, that job's banner (as the Last job banner shows a pipeline job's)."""
    from .common import act, last_job_banner

    for jid in (a.get("lane"), a.get("running")):
        stt = jobs.status(s.root, jid) if jid else None
        if stt and stt.get("action") == "assist":
            c = st.columns([8, 2], vertical_alignment="center")
            c[0].html(f'<span class="pkf-spin"></span><b>{theme.esc(stt.get("title") or "assistant")}</b> '
                      f'<span class="pkf-muted">· {elapsed(since(stt.get("started_at")))} · the console\'s Assistant transcript shows each step</span>')
            key = f"cancel-lane-{jid}" if jid == a.get("lane") else f"as-cancel-{jid}"
            if c[1].button("Cancel", key=key, help="stops the assistant's job"):
                act(jobs.cancel, s.root, jid)
            return
    if a.get("lanes"):
        recent = jobs.recent(s.root, p, 1, lane="assistant")
        last = recent[0] if recent else None
        res = (last or {}).get("result") or {}
        probs = [x for x in (res.get("problems") or []) if str(x).strip()] if isinstance(res, dict) else []
        if last and last.get("state") not in ("running", "starting") and (last.get("state") != "succeeded" or probs):
            seen = st.session_state.get(f"_as-seen-{p}")
            if seen != last.get("id"):
                last_job_banner(last)
                if st.button("Dismiss", key=f"as-lastjob-ok-{p}", type="tertiary"):
                    st.session_state[f"_as-seen-{p}"] = last.get("id")
                    st.rerun()


def body(s, p: str, states: dict | None, full: bool) -> None:
    """The assistant itself (dialog and full screen): brief, activity, conversation, decisions, Ask."""
    from .workflow_dev import ask_controls, assistant_conversation, assistant_decisions, assistant_state

    st_ = flow.load(s.root, p)
    a = assistant_state(s, p)
    if full:      # 0.8.2: the assistant on top (full width, ~75 % of the window, own scroll bar), Decisions below (~25 %)
        left = st.container(key="as-main")
        right = st.container(key="as-bottom")
    else:
        left = right = st.container()
    with left:
        brief_card(s, p, states, st_)
        _activity(s, p, a)
        with st.container(border=True, key="as-chat"):
            st.html("<b>Conversation</b>")
            assistant_conversation(s, p, states or {}, st_, a, expanded=full)
    with right:
        if full:
            ask_controls(s, p, st_, a, states=states)
        with st.container(border=True, key="as-decide"):
            st.html("<b>Decisions</b>")
            assistant_decisions(s, p, states or {}, st_, a)
            if a["latest"] is None and not (st_ or {}).get("assistant_autopilot"):
                st.caption("Nothing to decide yet.")
        if not full:
            ask_controls(s, p, st_, a, states=states)
        st.caption("The assistant may change this pipeline's spec and components; it can never change the framework's code.")


def _live(s, p: str, states, full: bool) -> None:
    """The body, refreshed every 2 s while an assistant job runs (its answer appears without a click). Full screen: the
    Workspace's own live fragment already refreshes it (render counts the lane job), so no second timer."""
    from .workflow_dev import lane_job

    if full:
        _body_watch(s, p, states, full)
        return
    working = bool(lane_job(s, p)) or bool(jobs.current(s.root, p))
    st.fragment(run_every=2 if working else None)(_body_watch)(s, p, states, full)


def _body_watch(s, p: str, states, full: bool) -> None:
    from .workspace import watch_job
    from .workflow_dev import lane_job

    body(s, p, states, full)
    watch_job(p, lane_job(s, p), lane="as-")       # the lane job ended: rerun so every part shows the answer


def _close(p: str) -> None:
    st.session_state[f"as-open-{p}"] = False


def maybe_dialog(s, p: str | None, states: dict | None) -> None:
    """The pop-up: reopened on every full run while as-open-<p> is set (a dialog closes on an app rerun); X clears it."""
    if not s or not p or not st.session_state.get(f"as-open-{p}") or st.session_state.get(f"ws-full-{p}"):
        return

    @st.dialog(f"✦ Assistant · {p}", width="large", on_dismiss=lambda: _close(p))
    def _dlg():
        c = st.columns([7, 2.2], vertical_alignment="center")
        c[0].caption("Opus 5.5 · knows the framework, reads the code, the artefacts and every log")
        c[1].link_button("⤢ Full screen", tab_url(p), key=f"as-dlg-full-{p}", width="stretch",
                         help="the assistant full screen, in a new browser tab")
        _live(s, p, states, full=False)

    _dlg()


def exit_full(p: str) -> None:
    st.session_state[f"ws-full-{p}"] = False


def tab_url(p: str) -> str:
    """The assistant's own page (0.8.2: ⤢ opens it in a new browser tab). Relative, so a base URL path keeps working."""
    from urllib.parse import quote

    return f"./assistant?p={quote(p)}"


def close_tab() -> None:
    """Close this browser tab (it was opened by a link with target=_blank, so the browser allows it)."""
    st.html("<script>window.close();</script>", unsafe_allow_javascript=True)


def tab_view(s, p: str, states: dict | None) -> None:
    """The assistant in its own browser tab (0.8.2): the header (Acts on its own, Open the Workspace, Close tab), the
    assistant on top and Decisions below. It reads the run and acts through the same functions; the run itself advances
    in the Workspace tab (one place ticks it)."""
    from .workspace import pilot_toggle

    st_ = flow.load(s.root, p)
    with st.container(horizontal=True, key="as-full-top", vertical_alignment="center"):
        st.html(f'<div class="pkf-crumb">✦ Assistant · <b>{theme.esc(p)}</b></div>')
        st.space("stretch")
        if st_ and st_.get("status") not in (None, "idle"):
            pilot_toggle(s, p, st_, label="Acts on its own", help_text="reviews, approvals and S1 stay with you")
        from .shell import PAGES

        if PAGES.get("Workspace") is not None:
            st.page_link(PAGES["Workspace"], label="Open the Workspace", icon=":material/space_dashboard:")
        if st.button("Close tab", key=f"as-close-tab-{p}", icon=":material/close:"):
            close_tab()
            st.caption("If the tab stays open, close it with Ctrl+W.")
    body(s, p, states, full=True)


def full_view(s, p: str, states: dict | None) -> None:
    """The assistant full screen (design H): its header (switch, Diagnose, Exit full screen), then the body in two
    columns. The sidebar is hidden (app.main)."""
    from .workspace import pilot_toggle

    st_ = flow.load(s.root, p)
    with st.container(horizontal=True, key="as-full-top", vertical_alignment="center"):
        st.html(f'<div class="pkf-crumb">✦ Assistant · <b>{theme.esc(p)}</b></div>')
        st.space("stretch")
        if st_ and st_.get("status") not in (None, "idle"):
            pilot_toggle(s, p, st_, label="Acts on its own", help_text="reviews, approvals and S1 stay with you")
        if st.button("Exit full screen", key=f"as-exit-{p}", icon=":material/close_fullscreen:"):
            exit_full(p)
            st.rerun(scope="app")          # inside the live fragment: the whole app, so the sidebar comes back
    _live(s, p, states, full=True)
