"""pkf Studio — the Streamlit app. Start it with `pkf studio [REPO]` (127.0.0.1 only).

0.8.0 (ST-3 v3 F): a light sidebar (the four pages, the pipeline, every pipeline with its stage, running jobs, the
assistant card, you) and four pages — Workspace (the stepper, the stage with its substages, Before Request approval,
Components, the console), Pipelines (every pipeline, then the seven tabs of 0.6.1), Artifacts (the files) and Setup —
under a top bar (where you are, mode, source control, Refresh, Stop run / Resume). The assistant opens as a pop-up or
full screen (assistant_ui)."""
from __future__ import annotations

import streamlit as st

from pkf_studio import checks as C
from pkf_studio import stages
from pkf_studio.ui import artifacts, assistant_ui, common, pipelines_page, setup, shell, theme, workflow_dev

# 0.6.1's tab names, kept for callers and tests: the first seven are the Pipelines page's tabs, the last is the Workspace.
TABS = pipelines_page.TABS + ["Workflow Developer"]

st.set_page_config(page_title="pkf Studio", page_icon="🏭", layout="wide")


@st.cache_data(ttl=60, show_spinner="Running the startup checks…")
def _blocking(_s, nonce: int) -> list:
    return C.blocking(C.run(_s))


@st.cache_data(ttl=20, show_spinner="Reading pull requests…")
def _states(_s, p: str, nonce: int, poll: int):
    return stages.derive(_s, p)


def states_of(s, p: str) -> tuple[dict, str | None]:
    """Where the pipeline stands, and why GitHub could not be asked (then the stages come from this computer's branches:
    `derive_offline`, K9). Never raises."""
    try:
        return _states(s, p, st.session_state.get("_studio_nonce", 0), st.session_state.get("_poll", 0)), None
    except Exception as exc:  # noqa: BLE001 - GitHub, proxy or git trouble must not take the whole app down
        err = getattr(s, "gh_error", None) if getattr(s, "gh_network", False) else None
        err = common.err_text(exc) if not err or getattr(getattr(s, "gh", None), "offline", None) else err
        try:
            return stages.derive_offline(s, p, err), err
        except Exception as exc2:  # noqa: BLE001 - even the local branches cannot be read
            last = st.session_state.get(f"_last_states-{p}")
            if last is None:
                raise RuntimeError(f"{err}; and the local branches cannot be read: {common.err_text(exc2)}") from None
            return last, err


@st.cache_data(ttl=60, show_spinner=False)
def _overview_states(_s, p: str, nonce: int, poll: int):
    return stages.derive(_s, p)


def overview_states(s, p: str) -> tuple[dict, str | None]:
    """The Pipelines overview's stage per pipeline: cached 60 s (one row per pipeline, read on every visit)."""
    try:
        return _overview_states(s, p, st.session_state.get("_studio_nonce", 0), st.session_state.get("_poll", 0)), None
    except Exception:  # noqa: BLE001
        return states_of(s, p)


def _blocking_now(s) -> list:
    return _blocking(s, st.session_state.get("_studio_nonce", 0))


def _open_page(title: str, top: bool = True):
    """Shared top of Workspace / Pipelines / Artifacts: the startup checks, the top bar, the stage states, the offline
    banner (the pipeline was chosen in the sidebar). Returns (studio, pipeline, states) or None when the page must stop."""
    s, err = common.studio()
    if not shell.guard(s, err, _blocking_now):
        return None
    if st.session_state.pop("_invalidate", False):
        s.invalidate()
    p = st.session_state.get("pipeline")
    states = offline = None
    if p and p in s.pipelines():
        try:
            states, offline = states_of(s, p)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not read where {p} stands: {common.err_text(exc)}")
            if st.button("Retry", key="retry-states"):
                common.act(common.refresh, s, reset=True)
            return None
        st.session_state[f"_last_states-{p}"] = states
    elif p:
        try:
            states, offline = states_of(s, p)
        except Exception:  # noqa: BLE001 - a new pipeline name: nothing to read yet
            states = None
    if top:
        shell.topbar(s, p, title, states)
    common.offline_banner(s, offline)
    if not p:
        return s, None, None
    if states is None:
        st.error(f"Could not read where {p} stands.")
        return None
    try:
        assistant_ui.maybe_dialog(s, p, states)       # sidebar → Open: the pop-up, on every page that shows a pipeline
    except Exception as exc:  # noqa: BLE001 - the assistant's trouble never takes the page down
        st.warning(f"The assistant could not be opened: {common.err_text(exc)}")
    return s, p, states


def _safe(label: str, s, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - one page's trouble (e.g. GitHub gone mid-render) is never a traceback
        st.error(f"This page could not be shown completely: {common.err_text(exc)}")
        if st.button("Retry", key=f"retry-page-{label}"):
            common.act(common.refresh, s, reset=True)


def page_workspace() -> None:
    full = bool(st.session_state.get(f"ws-full-{st.session_state.get('pipeline')}"))
    got = _open_page("Workspace", top=not full)
    if got is None:
        return
    s, p, states = got
    if not p:
        st.info("Choose a pipeline in the sidebar, or ➕ New pipeline.")
        return
    _safe("workspace", s, lambda: workflow_dev.render(s, p, states, artifacts_page=shell.PAGES.get("Artifacts")))


def page_pipelines() -> None:
    got = _open_page("Pipelines")
    if got is None:
        return
    s, p, states = got
    pipelines_page.overview(s, p, overview_states)
    if not p:
        st.info("Choose a pipeline in the sidebar, or create a new one.")
        return
    pipelines_page.render(s, p, states)


def page_artifacts() -> None:
    got = _open_page("Artifacts")
    if got is None:
        return
    s, p, states = got
    if not p:
        st.info("Choose a pipeline in the sidebar.")
        return
    _safe("artifacts", s, lambda: artifacts.render(s, p, states))


def _tab_pipeline(s) -> str | None:
    """The assistant tab's pipeline: ?p=<name> (a new browser tab is a new session), else the session's own."""
    want = st.query_params.get("p") or st.session_state.get("pipeline")
    if want and s is not None and want in s.pipelines():
        st.session_state["pipeline"] = st.session_state["pipe-pick"] = want
        return want
    return None


def page_assistant() -> None:
    """0.8.2: the assistant in its own browser tab (⤢), without the sidebar."""
    s, err = common.studio()
    if not shell.guard(s, err, _blocking_now):
        return
    p = _tab_pipeline(s)
    if not p:
        st.info("Open the assistant from a pipeline (sidebar → ⤢).")
        return
    try:
        states, offline = states_of(s, p)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read where {p} stands: {common.err_text(exc)}")
        return
    common.offline_banner(s, offline)
    _safe("assistant", s, lambda: workflow_dev.render_assistant(s, p, states))


def page_setup() -> None:
    s, err = common.studio()
    if s is None:
        st.title("pkf Studio")
        setup.render_checks(None, err)
        return
    setup.render_checks(s, None)
    st.divider()
    setup.render(s)


def main() -> None:
    theme.inject()
    pages = {"Workspace": st.Page(page_workspace, title="Workspace", url_path="workspace", default=True),
             "Pipelines": st.Page(page_pipelines, title="Pipelines", url_path="pipelines"),
             "Artifacts": st.Page(page_artifacts, title="Artifacts", url_path="artifacts"),
             "Setup": st.Page(page_setup, title="Setup", url_path="setup"),
             "Assistant": st.Page(page_assistant, title="Assistant", url_path="assistant")}   # its own tab (⤢), not in the nav
    shell.PAGES.clear()
    shell.PAGES.update(pages)
    pg = st.navigation(list(pages.values()), position="hidden")   # the sidebar draws its own page links (design order)
    shell.route(pg)
    s, err = common.studio()
    if getattr(pg, "title", "") == "Assistant":         # the assistant's own tab: no sidebar
        theme.full_screen()
        common.flash()                                  # what Apply / Discard / Do it left before their rerun
        try:
            pg.run()
        finally:
            shell.remember()
        return
    try:
        shell.sidebar(s, err, overview_states, workspace=getattr(pg, "title", "") == "Workspace")
    except Exception as exc:  # noqa: BLE001 - the sidebar's trouble never takes the page down
        st.sidebar.error(f"The sidebar could not be shown completely: {common.err_text(exc)}")
    common.flash()  # messages left by a quick action before its rerun (K10)
    try:
        pg.run()
    finally:
        shell.remember()   # what the page itself wrote to `page` / `tab` is not a request to switch page


main()
