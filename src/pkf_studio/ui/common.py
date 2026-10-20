"""Shared UI pieces: the opened repository, readers, job buttons and the live job log, gate cards, issues, the code
viewer, the file navigator and plan cards."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import streamlit as st
import yaml

from .. import ROLE_LABELS, jobs
from .. import ledger as L
from ..gates import GATES
from ..service import Studio, StudioError

LANG = {".py": "python", ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".tf": "hcl", ".hcl": "hcl", ".md": "markdown",
        ".txt": "text", ".toml": "toml", ".jsonl": "json", ".dot": "dot", ".sh": "bash"}
MAX_VIEW = 200_000


# ------------------------------------------------------------------------------------------------ the repository
def root() -> Path:
    return Path(os.environ.get("PKF_STUDIO_ROOT", ".")).resolve()


@st.cache_resource(show_spinner="Opening the repository…")
def _open(path: str, nonce: int):
    """The UI's Studio: its GitHub client is the interactive one (short timeouts, no retries, fail-fast offline; K9).
    Opening never fails because GitHub is unreachable: the Studio then carries `gh_error` and the app renders read-only."""
    try:
        return Studio.open(Path(path), interactive=True), None
    except Exception as exc:  # noqa: BLE001 - shown on the start page, never a traceback
        return None, err_text(exc)


def studio() -> tuple[Studio | None, str | None]:
    return _open(str(root()), st.session_state.get("_studio_nonce", 0))


def reopen() -> None:
    """Open the repository again (a new Studio): Check again on the startup checks."""
    st.session_state["_studio_nonce"] = st.session_state.get("_studio_nonce", 0) + 1
    st.cache_data.clear()


def refresh(s: Studio, *, reset: bool = False) -> None:
    """Read GitHub and git again with the same Studio (E9): after Request approval, Merge, Reopen and Save (who am I).
    reset=True (Retry, Refresh from GitHub) also leaves the GitHub client's offline state (K9)."""
    if reset and hasattr(s, "reset_offline"):
        s.reset_offline()
    s.fetch(force=True)
    s.reload()
    s.invalidate()
    st.cache_data.clear()


def offline_error(s: Studio) -> str | None:
    """Why GitHub (or the fetch of the base branch) cannot be read right now, or None (K9)."""
    gh = getattr(s, "gh", None)
    msgs = []
    off = getattr(gh, "offline", None) if gh is not None else None
    if off:
        msgs.append(str(off))
    elif getattr(s, "gh_error", None) and getattr(s, "gh_network", False):
        msgs.append(str(s.gh_error))
    if getattr(s, "fetch_error", None):
        msgs.append(f"git fetch failed: {s.fetch_error}")
    return "; ".join(msgs) or None


def offline_banner(s: Studio, error: str | None = None) -> bool:
    """The top banner while GitHub cannot be reached (C14/E4/E10): what failed, that the tabs show this computer's copy,
    and Retry. Returns True when it is shown."""
    msg = error or offline_error(s)
    if not msg:
        return False
    c1, c2 = st.columns([6, 1])
    c1.warning(f"GitHub unreachable: {md_escape(msg[:500])}  \nShowing this computer's copy of the work (read-only until "
               "GitHub is reachable). Buttons that need GitHub say so when clicked. Check the proxy (HTTPS_PROXY), "
               "REQUESTS_CA_BUNDLE and `source ~/pkf/env.sh`, then Retry.", icon="📴")
    if c2.button("Retry", key="retry-offline"):
        act(refresh, s, reset=True)
    return True


def pipeline() -> str | None:
    return st.session_state.get("pipeline")


class Files:
    """The pipeline's files as they stand: its worktree when there is one (work in progress), else the base branch."""

    def __init__(self, s: Studio, p: str):
        self.s, self.p = s, p
        self.wt = s.worktree_path(p)
        self.live = (self.wt / ".git").exists()

    def read(self, rel: str) -> str | None:
        if self.live:
            f = self.wt / rel
            return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None
        return self.s.read_base(rel)

    def yaml(self, rel: str) -> dict:
        try:
            v = yaml.safe_load(self.read(rel) or "")
        except yaml.YAMLError:
            return {}
        return v if isinstance(v, dict) else {}

    def json(self, rel: str) -> dict:
        try:
            v = json.loads(self.read(rel) or "{}")
        except ValueError:
            return {}
        return v if isinstance(v, dict) else {}

    def list(self, prefix: str = "") -> list[str]:
        if self.live:
            out = self.s.git.run("ls-files", "--cached", "--others", "--exclude-standard", *([prefix] if prefix else []), cwd=self.wt, check=False)
            return sorted({ln for ln in out.splitlines() if ln and (self.wt / ln).is_file()})
        return self.s.list_at(self.s.base_ref, prefix)

    def components(self) -> list[tuple[str, str]]:
        return L.components(self.read, self.p)

    def where(self) -> str:
        return f"worktree · {self.s.worktree_branch(self.p)}" if self.live else f"base · {self.s.base_ref}"


# ------------------------------------------------------------------------------------------------ jobs
def start_job(s: Studio, p: str, action: str, args: dict | None = None, title: str = "", lane: str | None = None) -> None:
    """Start a background job. lane="assistant" (0.8.0): the assistant's own read-only lane, beside a pipeline job."""
    try:
        if lane:
            jid = jobs.start(s.root, p, action, args or {}, title or action, lane=lane)
        else:
            jid = jobs.start(s.root, p, action, args or {}, title or action)
        st.session_state["last_job"] = jid
        st.toast(f"Started: {title or action}")
    except jobs.JobError as exc:
        st.error(str(exc))


def job_button(s: Studio, p: str, label: str, action: str, args: dict | None = None, *, key: str, disabled: bool = False,
               help: str | None = None, primary: bool = False) -> None:
    busy = jobs.current(s.root, p) is not None
    if st.button(label, key=key, disabled=disabled or busy, help=help or ("a job is running for this pipeline" if busy else None),
                 type="primary" if primary else "secondary"):
        start_job(s, p, action, args, label)
        st.rerun()


def local_time(iso: str | None) -> str:
    """A job timestamp (UTC, ISO) as the laptop's local time."""
    try:
        t = dt.datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return str(iso or "")[:19].replace("T", " ")
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    t = t.astimezone()
    return t.strftime("%H:%M:%S") if t.date() == dt.datetime.now().astimezone().date() else t.strftime("%Y-%m-%d %H:%M:%S")


def elapsed(seconds: float) -> str:
    """2m30s, 45s, 1h02m."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def since(iso: str | None) -> float:
    try:
        t = dt.datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds()


def last_line(text: str) -> str:
    return next((ln.strip() for ln in reversed((text or "").splitlines()) if ln.strip()), "")


def md_escape(text: str) -> str:
    """Plain text (file names with _ and *, paths, <placeholders>) shown literally inside markdown."""
    return "".join("\\" + ch if ch in "\\`*_[]<>#|~$" else ch for ch in str(text))


def running_box(stt: dict, log_text: str) -> str:
    """The running job: title, start (local time), elapsed time and the log's last line (E3)."""
    msg = (f"Running: **{md_escape(stt.get('title') or stt.get('action') or '')}** · started {local_time(stt.get('started_at'))}"
           f" (local time) · {elapsed(since(stt.get('started_at')))} elapsed")
    tail = last_line(log_text)
    return msg + (f"  \nLast log line: `{tail[:300].replace('`', chr(39))}`" if tail else "")


@st.fragment(run_every=2)
def _live_log(root_: str, p: str, jid: str) -> None:
    stt = jobs.status(Path(root_), jid)
    text = jobs.log(Path(root_), jid, tail=200)
    st.info(running_box(stt, text))
    st.code(text or "(starting…)", language="text", height=320)
    if stt["state"] not in ("running", "starting"):
        st.session_state["_poll"] = st.session_state.get("_poll", 0) + 1  # fresh stage data after the job
        st.session_state["_invalidate"] = True                            # and a fresh PR list
        st.rerun(scope="app")


MAX_SHOWN_PROBLEMS = 5


def problems_text(problems: list[str], n: int = MAX_SHOWN_PROBLEMS) -> str:
    """The first problems of a job result as markdown bullets (K1)."""
    probs = [str(x) for x in problems or [] if str(x).strip()]
    lines = [f"- {md_escape(x)}" for x in probs[:n]]
    if len(probs) > n:
        lines.append(f"- … and {len(probs) - n} more")
    return "\n".join(lines)


def last_job_banner(last: dict) -> None:
    """The last finished job: ✅ succeeded, ⚠️ succeeded with problems (K1: warning, or error when the work ran but
    failed), ❌ failed, 💥 crashed. Results written before 0.5.0 have no `problems` and show as before."""
    res = last.get("result") or {}
    probs = [x for x in (res.get("problems") or []) if str(x).strip()] if isinstance(res, dict) else []
    title = md_escape(last.get("title") or last.get("action") or "")
    state = last.get("state")
    more = "\n\nAll details: Job log and history (below)"
    if state == "succeeded" and probs:
        box = st.error if res.get("severity") == "error" else st.warning
        box(f"Last job: **{title}** — finished with problems\n\n" + problems_text(probs) + more, icon="⚠️")
        return
    if state == "succeeded":
        st.success(f"Last job: **{title}** — succeeded", icon="✅")
        return
    msg = f"Last job: **{title}** — {state}" + (f": {md_escape(res.get('error'))}" if res.get("error") else "")
    st.error(msg + (("\n\n" + problems_text(probs) + more) if probs else ""), icon={"crashed": "💥"}.get(state, "❌"))


CRASHED = ("The job's process ended without finishing (laptop asleep, killed, or Python crashed). What it did so far is "
           "committed. Run '{title}' again.")
FENCE = "`" * 3


def crashed_text(s: Studio, job: dict) -> str:
    """Why a crashed job stopped and what to do (E12): the cause, the next step, the log's last line, the process's own
    output (crash.log) and the job folder."""
    title = job.get("title") or job.get("action") or "the job"
    out = [CRASHED.format(title=md_escape(title))]
    tail = last_line(jobs.log(s.root, job["id"], tail=50))
    if tail:
        out.append(f"Last log line: `{tail[:300].replace('`', chr(39))}`")
    crash = jobs.crash_tail(s.root, job["id"]) if hasattr(jobs, "crash_tail") else ""
    if crash.strip():
        out.append(f"The process's own output (crash.log):\n{FENCE}\n{crash[-3000:].replace(FENCE, chr(39) * 3)}\n{FENCE}")
    out.append(f"Job folder: `{(jobs.jobs_dir(s.root) / job['id']).as_posix()}`")
    return "\n\n".join(out)


def job_panel(s: Studio, p: str) -> None:
    jid = jobs.current(s.root, p)
    if jid:
        c1, c2 = st.columns([5, 1])
        with c1:
            _live_log(str(s.root), p, jid)
        action = (jobs.status(s.root, jid) or {}).get("action")
        if action in getattr(jobs, "UNCANCELLABLE", ()):  # E17: it may be applying Terraform
            c2.caption("a local run may be applying Terraform: wait for it")
        elif c2.button("Cancel", key=f"cancel-{jid}"):
            act(jobs.cancel, s.root, jid)
        return
    recent = jobs.recent(s.root, p, 8)
    if recent:
        if recent[0].get("state") == "crashed":
            st.error(f"Last job: **{md_escape(recent[0].get('title') or recent[0].get('action') or '')}** — crashed\n\n"
                     + crashed_text(s, recent[0]), icon="💥")
        else:
            last_job_banner(recent[0])
        with st.expander("Job log and history"):
            job_history(s, recent)


def job_history(s: Studio, recent: list[dict]) -> None:
    """The last jobs with their logs and results (the job panel's history; 0.7.0 also the Workspace console's Jobs tab)."""
    for j in recent:
        st.markdown(f"**{md_escape(j.get('title') or '')}** · {j['state']} · {local_time(j.get('started_at'))}")
        st.code(jobs.log(s.root, j["id"], tail=120) or "", language="text", height=200)
        if j.get("result"):
            st.json(j["result"], expanded=False)


# ------------------------------------------------------------------------------------------------ display helpers
def issues(doc_issues: list[dict]) -> None:
    for i in doc_issues or []:
        sev = i.get("severity", "error")
        where = f"`{i.get('file', '')}{(':' + i['loc']) if i.get('loc') else ''}` " if i.get("file") else ""
        (st.error if sev == "error" else st.warning if sev == "warning" else st.info)(where + i.get("msg", ""))


def code_view(text: str | None, rel: str, *, key: str, abs_path: Path | None = None, height: int | None = 520) -> None:
    if text is None:
        st.caption(f"`{rel}` does not exist yet.")
        return
    lang = LANG.get(Path(rel).suffix.lower(), "text")
    big = len(text) > MAX_VIEW
    cols = st.columns([6, 1, 1])
    cols[0].caption(f"`{rel}` · {len(text.splitlines())} lines" + (" · truncated" if big else ""))
    if abs_path is not None:
        cols[1].link_button("VS Code", f"vscode://file/{abs_path.as_posix()}", help="Open in VS Code")
    cols[2].download_button("⬇", text.encode("utf-8"), file_name=Path(rel).name, key=f"dl-{key}", help="Download")
    st.code(text[:MAX_VIEW], language=lang, line_numbers=True, height=height if text.count("\n") > 25 else "content")


def file_navigator(f: Files, files: list[str], *, key: str) -> None:
    if not files:
        st.caption("No files yet.")
        return
    q = st.text_input("Search files", key=f"{key}-q", placeholder="path or part of it", persist_state="session")
    shown = [x for x in files if q.lower() in x.lower()] if q else files
    c1, c2 = st.columns([2, 5])
    with c1:
        sel = st.radio("Files", shown, key=f"{key}-sel", label_visibility="collapsed", persist_state="session") if shown else None
    with c2:
        if sel:
            code_view(f.read(sel), sel, key=f"{key}-{sel}", abs_path=(f.wt / sel) if f.live else None)


BADGES = {"approved": "🟢 approved", "skipped": "⚪ skipped", "waiting": "🟡 waiting for approval", "stale": "🟠 stale — approve again",
          "in_progress": "🔵 in progress", "not_started": "⚪ not started", "blocked": "⚫ blocked", "ready": "🔵 ready",
          "done": "🟢 recorded", "merged_unapproved": "🟠 merged without approval",
          "unknown": "❔ unknown (cannot reach GitHub)", "failed": "❌ last run failed"}   # K14


def gate_card(s: Studio, p: str, gs, result=None) -> None:
    g = GATES.get(gs.gate)
    title = f"{gs.gate} · {gs.title}" if g else "S6 · Run local"
    badge = BADGES.get(gs.state, gs.state)
    with st.container(border=True):
        c1, c2 = st.columns([4, 2])
        c1.markdown(f"**{title}** — {badge}")
        if gs.pr:
            c2.link_button(f"PR #{gs.pr['number']}", gs.pr["html_url"])
        if g:
            roles = ", ".join(ROLE_LABELS[r] for r in g.roles)
            st.caption(f"Approvers: {roles}{' (different people)' if s.strict and len(g.roles) > 1 else ''} · reviews: {g.reviews}")
        if gs.note:
            (st.error if gs.state == "failed" else st.caption)(gs.note)
        if result is not None:
            (st.success if result.passed else st.error if result.blocked else st.warning)(result.summary())
            for n in result.notes:
                st.caption("· " + n)


def plan_cards(lock: dict) -> None:
    plan = lock.get("plan") or {}
    if not plan:
        st.caption("No plan yet.")
        return
    if plan.get("summary"):
        st.markdown(plan["summary"])
    for i, step in enumerate(plan.get("steps", []), 1):
        with st.container(border=True):
            st.markdown(f"**{i}. {step.get('title') or step['id']}** · `{step['op']}` · id `{step['id']}`")
            if step.get("params"):
                st.code(yaml.safe_dump(step["params"], sort_keys=False, allow_unicode=True).strip(), language="yaml")
            if step.get("pseudocode"):
                st.code(step["pseudocode"], language="text")
            tests = (plan.get("step_tests") or {}).get(step["id"])
            if tests:
                st.dataframe([{"case": t.get("name"), **{k: json.dumps(v, default=str)[:120] for k, v in t.items() if k != "name"}}
                              for t in tests], hide_index=True)
    for title, key in (("Assumptions", "assumptions"), ("Ambiguities", "ambiguities")):
        if plan.get(key):
            st.markdown(f"**{title}**")
            for x in plan[key]:
                st.markdown(f"- {x}")
    sim = lock.get("simulation") or []
    if sim:
        st.markdown("**Simulation (the plan run on the component's examples)**")
        st.dataframe([{"example": x.get("example"), "status": x.get("status"), "detail": x.get("detail")} for x in sim], hide_index=True)
    ap = lock.get("plan_approval")
    if ap:
        st.caption(f"Plan approved by {ap.get('by')} ({ap.get('identity')}) at {ap.get('at')}")


def go_to(tab: str) -> None:
    st.session_state["tab"] = tab


def available_after(msg: str, key: str) -> None:
    c1, c2 = st.columns([4, 1])
    c1.info(msg)
    c2.button("Open Workflow", key=key, on_click=go_to, args=("Workflow",))


def safe(fn, *a, **kw):
    """Run a quick synchronous action and show its error instead of crashing the page."""
    try:
        return fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001
        st.error(f"{type(exc).__name__}: {exc}")
        return None


def err_text(exc: BaseException) -> str:
    """An error as the user reads it: the message of the Studio's own errors, else its type and message (K10)."""
    from ..github import GitHubError
    from ..gitops import GitError
    from ..runner import ActionError

    if isinstance(exc, (StudioError, GitHubError, jobs.JobError, GitError, ActionError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def act(fn, *a, ok=None, rerun: bool = True, **kw):
    """A quick action whose outcome must survive the rerun that follows it (K10): an error or the `ok` message is
    stored as a flash message and shown by `flash()` at the top of the next render. `ok` may also be a function of the
    action's return value (e.g. the commit it made)."""
    st.session_state["_acts"] = st.session_state.get("_acts", 0) + 1   # short-lived caches (workspace._cached) read afresh
    try:
        value = fn(*a, **kw)
        done = True
        msg = ok(value) if callable(ok) else ok
        if msg:
            st.session_state.setdefault("_flash", []).append(("success", msg))
    except Exception as exc:  # noqa: BLE001 - shown to the user, never a traceback
        value, done = None, False
        st.session_state.setdefault("_flash", []).append(("error", err_text(exc)))
    if rerun:
        st.rerun()
    return done, value


def flash() -> None:
    """Show (once) the messages that quick actions left before their rerun."""
    for kind, msg in st.session_state.pop("_flash", None) or []:
        (st.error if kind == "error" else st.success)(msg)


def busy(s: Studio, p: str) -> bool:
    """A job owns the pipeline's worktree: edits and git buttons wait until it ends (C19/E8)."""
    return jobs.current(s.root, p) is not None


BUSY_HELP = "a job is running for this pipeline: wait for it to finish"


def running_elsewhere(s: Studio) -> list[dict]:
    """Running jobs of every pipeline (E18), [] when they cannot be listed."""
    fn = getattr(jobs, "running_all", None)
    try:
        return list(fn(s.root)) if fn else []
    except Exception:  # noqa: BLE001 - a sidebar extra never breaks the page
        return []


def select_pipeline(name: str) -> None:
    st.session_state["pipe-pick"] = name
    st.session_state["page"] = "Pipelines"


def ui_commit(s: Studio, p: str, message: str) -> str | None:
    if busy(s, p):
        raise StudioError("a job is running for this pipeline; save again when it has finished")
    return s.commit(p, message)
