"""The app shell (0.8.0, ST-3 v3 F): page routing, the startup-check guard, the sidebar (pages, pipeline, all pipelines,
running jobs, the assistant card, you) and the top bar (breadcrumb, mode, branch, Refresh, Run ▾, Stop run / Resume).

Routing: pages change through a one-shot `_goto` key (consumed with st.switch_page before the page runs). The keys 0.6.1
used are still honoured when they change: `page` ("Pipelines" / "Setup", written by common.select_pipeline and the old
sidebar) and `tab` (a Pipelines-page tab, or "Workflow Developer" = the Workspace)."""
from __future__ import annotations

import streamlit as st

import pkf_studio
from .. import ROLE_LABELS, STUDIO_DIR, VERSION, flow, flowstore
from ..gates import GATES
from ..service import NAME_RE, branch_for
from . import theme
from .common import act, md_escape, refresh, running_elsewhere

NEW = "➕ New pipeline"
ROLE_SHORT = {"product_owner": "PO", "business_analyst": "BA", "architect": "Arch", "data_engineer": "DE"}
PAGES: dict = {}            # title -> st.Page, filled by app.py
WORKSPACE_TAB = "Workflow Developer"


# ------------------------------------------------------------------------------------------------ routing
def goto(page: str) -> None:
    """Open a page on the next run (a callback, or before st.rerun)."""
    st.session_state["_goto"] = page


def open_pipeline(name: str, page: str = "Workspace") -> None:
    """A callback: choose the pipeline and open it (running-elsewhere chip, Pipelines overview)."""
    st.session_state["pipe-pick"] = name
    st.session_state["_goto"] = page


def route(current) -> None:
    """Switch page when code asked for one (_goto) or a 0.6.1 key changed (page, tab). Call after st.navigation."""
    want = st.session_state.pop("_goto", None)
    page, tab = st.session_state.get("page"), st.session_state.get("tab")
    if want is None and page != st.session_state.get("_seen_page"):
        want = page if page in ("Pipelines", "Setup") else None
    if want is None and tab != st.session_state.get("_seen_tab") and tab:
        want = "Workspace" if tab == WORKSPACE_TAB else "Pipelines"
    st.session_state["_seen_page"], st.session_state["_seen_tab"] = page, tab
    target = PAGES.get(want) if want else None
    if target is not None and getattr(target, "title", None) != getattr(current, "title", None):
        st.switch_page(target)


def remember() -> None:
    """After the page ran: the `page` / `tab` values it left are the baseline for the next run's change check."""
    st.session_state["_seen_page"], st.session_state["_seen_tab"] = st.session_state.get("page"), st.session_state.get("tab")


# ------------------------------------------------------------------------------------------------ guard
def guard(s, err, blocking_fn) -> bool:
    """The startup checks for Workspace / Pipelines / Artifacts (Setup always opens, as in 0.6.1). False = stop."""
    from . import setup

    if s is None:
        st.title("pkf Studio")
        setup.render_checks(None, err)
        return False
    blocking = blocking_fn(s)
    if blocking:
        st.title("pkf Studio")
        st.error("Fix the startup checks first (Setup).")
        for c in blocking:
            st.markdown(f"❌ **{c.name}** — {c.detail}  \n  Fix: {c.fix}")
        return False
    return True


# ------------------------------------------------------------------------------------------------ context bar
def pipeline_select(s, names: list[str]) -> str | None:
    """The pipeline selector (0.6.1's sidebar selectbox, same key) and the new-pipeline name. Sets session 'pipeline'."""
    choice = st.selectbox("Pipeline", names + [NEW], key="pipe-pick", persist_state="session")
    if choice == NEW:
        name = st.text_input("Name (snake_case)", key="new-name", placeholder="new pipeline name (snake_case)",
                             label_visibility="collapsed")
        if name and not NAME_RE.match(name):
            st.error("lowercase letters, digits and _, starting with a letter")
            name = None
        st.session_state["pipeline"] = name or None
    else:
        st.session_state["pipeline"] = choice
    return st.session_state.get("pipeline")


def _source_label(s, p: str, states: dict | None) -> tuple[str, str | None, dict]:
    branch = s.worktree_branch(p)
    gate = next((g for g in GATES if branch == branch_for(p, g)), None)
    sync: dict = {}
    if gate:
        try:
            sync = s.branch_sync(p, gate)
        except Exception:  # noqa: BLE001
            sync = {}
    try:
        changed = len(s.changed_files(s.worktree_path(p))) if branch else 0
    except Exception:  # noqa: BLE001
        changed = 0
    parts = [f"{s.owner}/{s.repo}", branch or f"base {s.base}"]
    if sync.get("ahead") or sync.get("behind"):
        parts.append(f"↑{sync.get('ahead', 0)}" + (f" ↓{sync['behind']}" if sync.get("behind") else ""))
    parts.append(f"{changed} changed" if changed else "clean")
    gs = (states or {}).get("by_gate", {}).get(gate) if gate else None
    if gs is not None and gs.pr and gs.state == "waiting":
        parts.append(f"PR #{gs.pr['number']} open")
    return " · ".join(parts), gate, {"sync": sync, "changed": changed, "branch": branch, "gs": gs}


def source_control(s, p: str, states: dict | None) -> None:
    """The source-control chip: repo · branch · ahead · clean/changed · PR; inside, the cards that act on git (0.6.1's
    dirty bar and diverged card, keys prefixed "sc-" so the Workflow tab's own copies on the Pipelines page do not clash)."""
    from .workflow import diverged_card, dirty_bar

    try:
        label, gate, info = _source_label(s, p, states)
    except Exception as exc:  # noqa: BLE001 - git trouble never takes the bar down
        st.caption(f"git: {exc}")
        return
    with st.popover(label, key=f"sc-pop-{p}", help="this pipeline's branch: changes, GitHub's copy, the pull request"):
        st.markdown(f"**{md_escape(s.owner)}/{md_escape(s.repo)}** · base `{s.base}` · mode **{s.cfg.mode}**")
        if info["branch"]:
            sync = info["sync"]
            st.markdown(f"Branch `{info['branch']}`" + (f" — {sync.get('ahead', 0)} commit(s) not on GitHub, "
                        f"{sync.get('behind', 0)} on GitHub not here" if sync else ""))
            wt = s.worktree_path(p)
            st.link_button("Open the worktree in VS Code", f"vscode://file/{wt.as_posix()}")
        else:
            st.caption("No worktree yet: the files shown are the base branch's.")
        gs = info["gs"]
        if gs is not None and gs.pr:
            st.link_button(f"PR #{gs.pr['number']} on GitHub", gs.pr["html_url"])
        if info["changed"]:                         # the cards read git again: only when they have something to show
            dirty_bar(s, p, key_prefix="sc-")
        if gate and (info["sync"] or {}).get("state") == "diverged":
            diverged_card(s, p, gate, key_prefix="sc-")
        if not info["changed"] and (info["sync"] or {}).get("state") != "diverged":
            st.caption("Nothing to commit; this computer and GitHub agree.")


def run_controls(s, p: str, st_: dict | None) -> None:
    """Stop run / ▶ Resume (0.6.1's strip buttons, same keys) and Run ▾ (P10: Auto-approve, Forget this run)."""
    from .workflow_dev import ACTIVE, _do, _fresh, _refresh_do

    status = (st_ or {}).get("status")
    if status in ACTIVE:
        if st.button("Stop run", key=f"wfd-stop-{p}", help="pauses the run after the current job; Resume continues from the same step"):
            _do(flow.stop, s, p, ok="workflow stopped")
    if status == "stopped":
        if st.button("▶ Resume", key=f"wfd-resume-{p}", type="primary"):
            _refresh_do(flow.resume, s, p, _fresh(s, p), ok="workflow resumed")
    if status in ACTIVE or status in ("stopped", "done", "failed"):
        with st.popover("Run ▾", key=f"run-menu-{p}"):
            if not s.strict and status in ACTIVE:
                key = f"wfd-auto-live-{p}"
                st.session_state[key] = bool(st_.get("auto_approve"))     # 0.8.2: shows the run's state; a click changes it
                st.toggle("Auto-approve gates (dev)", key=key, on_change=_auto_changed, args=(s, p, key),
                          help="On: the run posts /approve and merges every gate itself; inputs and failures still pause it.")
            if not s.strict and s.cfg.mode == "dev" and status in ACTIVE + ("failed",):
                from .workflow_dev import full_auto_live

                full_auto_live(s, p, st_)
            if status in ("stopped", "done", "failed"):
                if st.button("Forget this run", key=f"wfd-reset-{p}", help="the pipeline is untouched; Run workflow starts again from its current gate"):
                    _do(flow.reset, s, p, ok="run forgotten")
            st.caption(md_escape((st_ or {}).get("message") or ""))


def _auto_changed(s, p: str, key: str) -> None:
    cur = flow.load(s.root, p)
    if cur and bool(cur.get("auto_approve")) != bool(st.session_state.get(key)):
        cur["auto_approve"] = bool(st.session_state.get(key))
        flowstore.save(s.root, p, cur)


# ------------------------------------------------------------------------------------------------ sidebar
NAV = [("Pipelines", ":material/menu:"), ("Workspace", ":material/monitor_heart:"), ("Artifacts", ":material/description:"),
       ("Setup", ":material/settings:")]
BRAND_SVG = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" '
             'aria-hidden="true"><path d="M4 7h16M4 12h10M4 17h16"/></svg>')


def pipeline_chip(s, name: str, states_of) -> tuple[str, str]:
    """The chip of a pipeline in the sidebar list: 'S4' (where it is), 'G1' (waiting for that approval), 'S5 failed',
    'done'. (text, colour)."""
    from .. import jobs

    st_ = flow.load(s.root, name) or {}
    cur = flow.BY_ID.get(st_.get("cursor") or "")
    if st_.get("status") == "failed" and cur:
        return f"{cur.stage} failed", "red"
    try:
        states, _ = states_of(s, name)
    except Exception:  # noqa: BLE001 - one pipeline's trouble never hides the list
        states = None
    if states:
        waiting = [g.gate for g in states["gates"] if g.state == "waiting" and g.gate in GATES]
        if waiting:
            return waiting[0], "amber"
        if st_.get("status") == "done" or states.get("current") is None:
            return "done", "grey"
    if cur:
        return cur.stage, "green" if (jobs.current(s.root, name) or st_.get("status") == "running") else "grey"
    gate = (states or {}).get("current")
    return ({v: k for k, v in flow.STAGE_GATE.items() if v}.get(gate) or "S1"), "grey"


def is_full(p: str | None) -> bool:
    return bool(p and st.session_state.get(f"ws-full-{p}"))


def sidebar(s, err, states_of, *, workspace: bool = True) -> str | None:
    """The whole sidebar. Returns the chosen pipeline (or None). The assistant full screen (ws-full-<p>, only on the
    Workspace) hides the sidebar, and its widgets with keys used on the full-screen view are not drawn twice; on another
    page the flag is dropped, so Open works there."""
    full = False
    with st.sidebar:
        st.html(f'<div class="pkf-brand"><div class="logo">{BRAND_SVG}</div><div><b>pkf Studio</b><small>'
                + (theme.esc(f"{getattr(pkf_studio, 'BUILD', None) or VERSION} · {s.owner}/{s.repo} · {s.base} · {s.cfg.mode}") if s else
                   theme.esc(VERSION)) + "</small></div></div>")
        for title, icon in NAV:
            page = PAGES.get(title)
            if page is not None:
                st.page_link(page, label=title, icon=icon)
        if s is None:
            st.caption(err or "")
            return None
        names = s.pipelines()
        p = pipeline_select(s, names)
        if is_full(p) and not workspace:
            st.session_state[f"ws-full-{p}"] = False
        full = workspace and is_full(p)
        if full:
            theme.full_screen()
        if names:
            st.html('<div class="pkf-side-h">ALL PIPELINES</div>')
            for name in names:
                text, color = pipeline_chip(s, name, states_of)
                dot = {"red": ":red[●]", "amber": ":orange[●]", "green": ":green[●]"}.get(color, ":gray[●]")
                c = st.columns([8, 2.4], vertical_alignment="center", gap="small")
                c[0].button(f"{dot} {name}", key=f"sb-pipe-{name}", type="tertiary", width="stretch", on_click=_choose, args=(name,),
                            help=f"open {name}")
                c[1].html(f'<div style="text-align:right">{theme.chip(text, color)}</div>')
        live_sidebar(s, p, full)
        if p and p in names and not full:
            assistant_card_actions(p)
        st.divider()
        user_menu(s)
    return p


def _choose(name: str) -> None:
    st.session_state["pipe-pick"] = name


def live_sidebar(s, p: str | None, full: bool) -> None:
    """The live parts of the sidebar — running jobs and what the assistant is doing — in their own fragment: a fragment in
    the main area may not write to the sidebar, so these refresh themselves every 2 s while something runs."""
    from .. import jobs

    st_ = flow.load(s.root, p) if p and p in s.pipelines() else None
    lane_job = jobs.current(s.root, p, lane="assistant") if (p and _has_lanes()) else None
    active = bool(running_elsewhere(s)) or bool(lane_job) or bool(st_ and st_.get("status") in ("running", "waiting_input", "waiting_gate",
                                                                                              "waiting_user"))

    def body():
        from .common import elapsed, local_time, since

        running = running_elsewhere(s)
        if running:
            with st.container(border=True, key="sb-jobs"):
                st.html('<div class="pkf-side-h" style="margin-top:0">RUNNING JOBS</div>')
                for j in running:
                    jp = j.get("pipeline") or "?"
                    st.button(f"▶ {jp}: {j.get('title') or j.get('action') or ''}", key=f"goto-job-{jp}", type="tertiary",
                              on_click=open_pipeline, args=(jp,),
                              help=f"started {local_time(j.get('started_at'))} (local time): open this pipeline in the Workspace")
                    st.caption(f"{elapsed(since(j.get('started_at')))} elapsed")
        if p and p in s.pipelines():
            assistant_card_status(s, p, full)

    st.fragment(run_every=2 if active else None)(body)()


def _has_lanes() -> bool:
    """jobs.current takes a `lane` (0.8.0 assistant lane); False on an older jobs module."""
    import inspect

    from .. import jobs

    try:
        return "lane" in inspect.signature(jobs.current).parameters
    except (TypeError, ValueError):
        return False


def assistant_status(s, p: str) -> tuple[str, bool]:
    """What the assistant is doing, in one line, and whether it waits for the user (the red dot)."""
    from .. import jobs
    from .workflow_dev import _assist_sessions, _read_json

    st_ = flow.load(s.root, p) or {}
    pl = st_.get("pilot") or {}
    lane_job = jobs.current(s.root, p, lane="assistant") if _has_lanes() else None
    main_job = jobs.current(s.root, p)
    for jid in (lane_job, main_job):
        stt = jobs.status(s.root, jid) if jid else None
        if stt and stt.get("action") == "assist":
            return f"working: {stt.get('title') or 'assistant'}", False
    if st_.get("assistant_autopilot") and pl.get("phase"):
        waits = pl["phase"] in ("needs_you", "gave_up")
        return str(pl.get("note") or pl["phase"])[:140], waits
    sessions = _assist_sessions(s, p)
    if sessions:
        d = sessions[0]
        sm = _read_json(d / "summary.json") or {}
        if (d / "fix.json").is_file() and not sm.get("applied") and not sm.get("discarded"):
            return "a fix waits for you to apply or discard", True
        diag = _read_json(d / "diagnosis.json")
        if diag and not (d / "diagnosis.dismissed").exists():
            step = diag.get("step") or sm.get("step") or ""
            waiting = diag.get("fix_kind") == "edit_files"
            return f"Diagnosed {step} · " + ("a fix plan waits for you" if waiting else str(diag.get("fix_kind") or "")), waiting
        if sm.get("answer"):
            return "answered your last question", False
    return "ready · ask about this pipeline or a step", False


def assistant_card_status(s, p: str, full: bool) -> None:
    """The assistant card's live part: the switch and the status line."""
    from .workspace import pilot_toggle

    st_ = flow.load(s.root, p)
    with st.container(border=True, key="sb-assistant"):
        text, waits = assistant_status(s, p)
        st.html('<div class="pkf-side-h" style="margin-top:0;color:#5B2DD6">✦ ASSISTANT'
                + (' <span style="float:right;color:#E5484D">●</span>' if waits else "") + "</div>")
        if st_ and st_.get("status") not in (None, "idle") and not full:
            pilot_toggle(s, p, st_, label="Acts on its own", help_text="reviews, approvals and S1 stay with you")
        elif not st_ or st_.get("status") in (None, "idle"):
            st.caption("Acts on its own: chosen when the run starts")
        st.caption(md_escape(text))


def assistant_card_actions(p: str) -> None:
    """Open (the dialog) and ⤢ (full screen): outside the live fragment, so a click reruns the page and the dialog opens."""
    from .assistant_ui import tab_url

    c = st.columns([4, 1.2], gap="small")
    with c[0]:
        st.button("Open", key="sb-open", width="stretch", on_click=open_assistant, args=(p,), help="open the assistant")
    c[1].link_button("⤢", tab_url(p), key="sb-full", help="the assistant full screen, in a new browser tab")


def open_assistant(p: str) -> None:
    st.session_state[f"as-open-{p}"] = True


def full_assistant(p: str) -> None:
    st.session_state[f"as-open-{p}"] = False
    st.session_state[f"ws-full-{p}"] = True
    st.session_state["_goto"] = "Workspace"


def user_menu(s) -> None:
    """You (initials · name · role) at the foot of the sidebar; the popover: build, repository, mode, warnings, where
    local state lives, Setup."""
    try:
        me = s.me()
    except Exception:  # noqa: BLE001
        me = None
    who = f"{theme.initials(me.name)} · {me.name} · {'/'.join(ROLE_SHORT.get(r, r) for r in me.roles)}" if me else "Who am I? (Setup)"
    with st.popover(who, key="user-menu", type="tertiary"):
        st.markdown(f"**pkf Studio {getattr(pkf_studio, 'BUILD', None) or VERSION}**")
        st.caption(f"{s.owner}/{s.repo} · base `{s.base}` · mode **{s.cfg.mode}**")
        st.caption(f"You: **{me.name}** ({', '.join(ROLE_LABELS[r] for r in me.roles)})" if me else
                   "You: not chosen yet (Setup → Who am I)")
        for w in s.warnings[-3:]:
            st.caption(f"⚠️ {w}")
        st.caption(f"Local state lives in {STUDIO_DIR}/ (git-ignored): worktrees, jobs, locks, Terraform plans.")
        st.button("Open Setup", key="user-menu-setup", on_click=goto, args=("Setup",))


# ------------------------------------------------------------------------------------------------ top bar
def topbar(s, p: str | None, page_title: str, states: dict | None) -> None:
    """Breadcrumb · mode (and auto-approve) · branch chip · Refresh · Run ▾ · Stop run / ▶ Resume."""
    exists = bool(p and p in s.pipelines())
    st_ = flow.load(s.root, p) if exists else None
    with st.container(horizontal=True, vertical_alignment="center", key="pkf-top"):
        st.html(f'<span class="pkf-crumb">{theme.esc(page_title)}' + (f' &nbsp;/&nbsp; <b>{theme.esc(p)}</b>' if p else "") + "</span>")
        if exists:
            auto = (" · run without stopping" if (st_ or {}).get("full_auto") else " · auto-approve on" if (st_ or {}).get("auto_approve") else " · auto-approve off")
            st.html(theme.chip(f"{s.cfg.mode.upper()}{auto if not s.strict else ''}", "amber" if s.cfg.mode == "dev" else "red"))
        st.space("stretch")
        if exists:
            source_control(s, p, states)
        if st.button("Refresh", key="refresh", help="read GitHub and git again"):
            act(refresh, s, reset=True)
        if exists:
            run_controls(s, p, st_)
    if exists:
        st.session_state[bar_key(p)] = (st_ or {}).get("status") or "idle"


def bar_key(p: str) -> str:
    """Where the top bar records the run status it drew (the Workspace reruns the app when the run moved on)."""
    return f"_ws-bar-status-{p}"
