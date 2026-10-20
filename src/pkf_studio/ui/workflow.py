"""Tab 7 — Workflow: the stages with their gate cards, what to do next, the live job log, approval history and the
artifact download."""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

import streamlit as st
import yaml

from .. import STUDIO_DIR, bundle, jobs
from .. import ledger as L
from ..gates import GATES, ORDER
from .. import service as SV
from ..service import StudioError, branch_for, dags_problem, readiness, unfilled
from ..stages import STAGES, stage_started
from .common import (BADGES, BUSY_HELP, Files, act, busy, err_text, gate_card, issues, job_button, local_time, md_escape,
                     refresh, safe, since, ui_commit)

START = {"G2": "start_g2", "G3": "start_g3", "G4": "start_g4", "G5": "start_g5"}
DONE = ("approved", "skipped")


@st.cache_data(ttl=30, show_spinner="Reading the pull request…")
def _gate(_s, p: str, gate: str, pr_number: int, head: str, nonce: int):
    try:
        res, full = _s.gate_status(p, gate)
        return res, full, None
    except Exception as exc:  # noqa: BLE001
        return None, None, err_text(exc)


def gate_eval(s, p, gs):
    return _gate(s, p, gs.gate, gs.pr["number"], gs.pr["head"]["sha"], st.session_state.get("_poll", 0))


def _poll(s) -> None:
    st.session_state["_poll"] = st.session_state.get("_poll", 0) + 1
    s.invalidate()


# ------------------------------------------------------------------------------------------------ readiness (E11)
def ready_problems(s, p: str, gate: str) -> list[str]:
    f = Files(s, p)
    if not f.live or s.worktree_branch(p) != branch_for(p, gate):
        return [f"the stage's work is not on {branch_for(p, gate)} yet"]
    return readiness(f.wt, p, gate, base=s.base, dags_diff=s.dags_diff(p) if gate == "G4" else None)


@st.cache_data(ttl=300, show_spinner=False)
def _ready_cached(_s, p: str, gate: str, head: str | None, changed: int, nonce: int) -> list[str]:
    return ready_problems(_s, p, gate)


# the button that fixes a readiness item, when the item does not name it already
FIX_HINTS = [
    ("is not on pkf/", "Start the stage (or Open for editing, Requirements tab)"),
    ("analyse the requirement first", "Analyze with AI (Requirements tab)"),
    ("environment value(s) still <REQUIRED>", "Save values (Source / Destination / Trigger tabs)"),
    ("the plan fails its examples", "Re-plan it"),
    ("plan fails its examples", "Re-plan it"),
    ("end-to-end examples fail", "Re-plan it, or Reopen G1"),
    ("run the tests and the build first", "Run tests & build again"),
    ("render and plan the dev environment first", "Render & plan again"),
    ("the component files do not load", "fix the file in its tab, then Plan components (AI)"),
    ("outside pipeline", "Remove the files outside this pipeline"),
]


def with_fix(item: str, gate: str) -> str:
    for needle, label in FIX_HINTS:
        if needle in item and label not in item:
            return f"{item} → {label}"
    if gate == "G3" and re.match(r"^(source|destination|trigger|transformation) \S+: ", item) and "Re-plan" not in item:
        return f"{item} → Re-plan it (or Plan components (AI))"
    return item


def checklist(s, p: str, gate: str) -> list[str]:
    """What still stops Request approval for the gate (readiness), each item naming the button that fixes it. Read on
    every render, cached by (pipeline, gate, HEAD, number of changed files) for 5 minutes."""
    f = Files(s, p)
    try:
        head = s.git.head(f.wt) if f.live else None
        changed = len(s.changed_files(f.wt)) if f.live else 0
        items = _ready_cached(s, p, gate, head, changed, st.session_state.get("_poll", 0))
    except Exception as exc:  # noqa: BLE001 - the check itself failed: say so, and do not offer the request
        items = [f"the readiness check failed: {err_text(exc)}"]
    return [with_fix(x, gate) for x in items]


def _request(s, p: str, gate: str) -> dict:
    probs = ready_problems(s, p, gate)
    if probs:
        raise StudioError("not ready yet: " + "; ".join(probs))
    with st.spinner("Committing, pushing and opening the pull request…"):
        pr = s.request_approval(p, gate)
    refresh(s)
    return pr


def request_button(s, p: str, gate: str, states: dict) -> None:
    gs = states["by_gate"][gate]
    items = checklist(s, p, gate)
    if items:
        with st.container(border=True):
            st.markdown(f"**Before Request approval ({gate})** — still to do:")
            st.markdown("\n".join(f"- ☐ {md_escape(x)}" for x in items))
    running = busy(s, p)
    scope_section(s, p, running)
    label = "Update the PR" if gs.state == "waiting" else f"Request approval ({gate})"
    if st.button(label, key=f"req-{gate}-{p}", type="secondary" if gs.state == "waiting" else "primary",
                 disabled=bool(items) or running,
                 help=BUSY_HELP if running else ("finish the list above first" if items else None)):
        act(_request, s, p, gate, ok=lambda pr: f"PR #{pr['number']}: {pr['html_url']}")


def scope_section(s, p: str, running: bool) -> None:
    """Committed files outside the pipeline's own on its stage branch block the gate: one button takes them off the
    branch (Studio.remove_out_of_scope); uncommitted ones go with Discard them."""
    fn = getattr(s, "out_of_scope", None)
    if fn is None:
        return
    try:
        committed = fn(p).get("committed") or []
    except Exception:  # noqa: BLE001
        return
    if not committed:
        return
    if st.button("Remove the files outside this pipeline", key="remove-out-of-scope", disabled=running,
                 help=BUSY_HELP if running else "one commit: files the base branch has go back to its version, others are "
                 "deleted (their content stays in the branch history): " + ", ".join(committed[:5])):
        act(_remove_out_of_scope, s, p, ok=lambda files: f"{len(files or [])} file(s) outside {p} removed from the branch (one commit)")


def _remove_out_of_scope(s, p: str) -> list[str]:
    out = s.remove_out_of_scope(p)
    st.cache_data.clear()
    return out


# ------------------------------------------------------------------------------------------------ quick git actions
def _merge(s, p: str, gate: str) -> dict:
    with st.spinner("Merging…"):
        out = s.merge(p, gate)
    refresh(s)
    return out


def _post_approve(s, p: str, gate: str) -> dict:
    out = s.post_approve(p, gate)
    _poll(s)
    refresh(s)
    return out


def _recheck(s, p: str) -> bool | None:
    r = s.recheck_merged(p)
    refresh(s)
    if r is False:
        m = s.merged_unapproved(p) or {}
        raise StudioError(f"still without the required approval: {m.get('summary') or 'see the PR'}")
    return r


def _reopen(s, p: str, gate: str):
    with st.spinner(f"Reopening {gate}…"):
        out = s.reopen(p, gate)
    refresh(s)
    return out


def _resolve(s, p: str, gate: str, how: str) -> dict:
    out = s.resolve_diverged(p, gate, how)
    refresh(s)
    return out


def local_work(s, p: str, gate: str, head: str) -> bool:
    """This computer has work on the gate's branch that the PR does not show (commits ahead of the PR head, or
    uncommitted changes). Only behind is not local work (C3)."""
    f = Files(s, p)
    if not (f.live and s.worktree_branch(p) == branch_for(p, gate)):
        return False
    try:
        ahead = s.local_ahead(p, gate, head) > 0
    except Exception:  # noqa: BLE001
        ahead = s.git.head(f.wt) != head
    return ahead or not s.git.is_clean(f.wt)


def waiting_actions(s, p: str, gs) -> None:
    res, full, err = gate_eval(s, p, gs)
    if err:
        gate_card(s, p, gs)
        st.error(f"The pull request could not be read: {err}")
        if st.button("Refresh", key=f"poll-{gs.gate}"):
            _poll(s)
            st.rerun()
        return
    gate_card(s, p, gs, res)
    head = full["head"]["sha"]
    running = busy(s, p)
    c = st.columns(4)
    if c[0].button("Refresh", key=f"poll-{gs.gate}"):
        _poll(s)
        st.rerun()
    if not s.strict and c[1].button("Post /approve as me", key=f"approve-{gs.gate}", disabled=running,
                                    help=BUSY_HELP if running else
                                    "dev mode: posts `/approve <head>` on the PR with your GitHub token — the approval is in GitHub"):
        act(_post_approve, s, p, gs.gate, ok=f"/approve posted on PR #{gs.pr['number']}")
    if s.strict:
        chk = safe(s.gate_check, head) or "missing"
        c[2].markdown(f"`pkf-gate`: **{chk}**")
    ahead = local_work(s, p, gs.gate, head)
    if ahead:
        st.warning("This branch has local work the PR does not show yet (new commits or unsaved changes): *Update the PR* first, "
                   "so the approval covers it.")
    if c[3].button("Merge", key=f"merge-{gs.gate}", type="primary", disabled=not res.passed or ahead or running,
                   help=BUSY_HELP if running else None):
        act(_merge, s, p, gs.gate, ok=f"{gs.gate} merged. Start the next stage when you are ready.")
    st.caption(("On GitHub: review → Approve on the latest commit" if s.strict else
                "On GitHub: comment `/approve` on the PR (approves the latest commit)")
               + ". Several roles? add `Approve as: architect` (po, ba, de). You can merge on GitHub or here.")


# ------------------------------------------------------------------------------------------------ Reopen (C5, K8)
def reopen_text(p: str, gate: str, preview: list[dict] | None) -> str:
    """What Reopen <gate> sets aside, from Studio.reopen_preview."""
    if not preview:
        return f"Reopen {gate} restarts it from the base branch; no other branch has work to set aside."
    total = sum(int(x.get("unpushed") or 0) for x in preview)
    on = [x["branch"].rsplit("/", 1)[-1] for x in preview if x.get("unpushed")]
    head = (f"Reopen {gate} sets aside {total} unpushed commit{'s' if total != 1 else ''} on {', '.join(on)} "
            f"(kept as refs/pkf-archive/{p}/…)" if total else
            f"Reopen {gate} sets aside {', '.join(x['branch'].rsplit('/', 1)[-1] for x in preview)} (nothing unpushed; "
            f"the tips are kept as refs/pkf-archive/{p}/…)")
    lines = [head + ":"]
    for x in preview:
        bits = [f"{x.get('unpushed') or 0} unpushed commit(s)"]
        if x.get("open_pr"):
            bits.append(f"PR #{x['open_pr']} is closed")
        lines.append(f"- `{x['branch']}` ({x.get('tip', '')[:7]}): " + ", ".join(bits))
    return "\n".join(lines)


def reopen_control(s, p: str, gate: str, *, key: str, label: str | None = None) -> None:
    """Reopen with a preview of what it sets aside and a second Confirm reopen click (C5)."""
    ck = f"_confirm-{key}"
    running = busy(s, p)
    if not st.session_state.get(ck):
        if st.button(label or f"Reopen {gate}", key=key, disabled=running, help=BUSY_HELP if running else
                     "shows what it sets aside first, then asks you to confirm"):
            st.session_state[ck] = True
            st.rerun()
        return
    try:
        preview = s.reopen_preview(p, gate)
        st.warning(reopen_text(p, gate, preview))
    except Exception as exc:  # noqa: BLE001
        st.warning(f"What Reopen {gate} sets aside could not be listed ({err_text(exc)}); every later stage branch is kept "
                   f"as refs/pkf-archive/{p}/… before it moves.")
    c1, c2 = st.columns(2)
    if c1.button("Confirm reopen", key=f"{key}-confirm", type="primary", disabled=running):
        st.session_state.pop(ck, None)
        act(_reopen, s, p, gate, ok=f"{gate} reopened: work on it again, then Request approval ({gate}). "
                                    "Set-aside work is listed under Set-aside work (after Reopen).")
    if c2.button("Cancel", key=f"{key}-cancel"):
        st.session_state.pop(ck, None)
        st.rerun()


# ------------------------------------------------------------------------------------------------ GitHub's copy changed (C3)
CARD = getattr(SV, "DIVERGED_CARD", "GitHub's copy changed")
TAKE = getattr(SV, "TAKE_LABEL", "Take GitHub's copy")
MERGE = getattr(SV, "MERGE_LABEL", "Merge GitHub's copy into mine")
OVERWRITE = getattr(SV, "OVERWRITE_LABEL", "Overwrite GitHub's copy (dev)")

def diverged_card(s, p: str, gate: str, key_prefix: str = "") -> bool:
    """The card shown when this computer's copy of the gate's branch and GitHub's both have commits the other has not.
    True when it is shown (the stage's other actions wait until it is resolved)."""
    if gate not in GATES or not hasattr(s, "branch_sync"):
        return False
    try:
        sync = s.branch_sync(p, gate)
    except Exception:  # noqa: BLE001
        return False
    if sync.get("state") != "diverged":
        return False
    b = branch_for(p, gate)
    running = busy(s, p)
    with st.container(border=True):
        st.error(f"**{CARD}**: someone pushed to `{b}` (or used *Update branch* on GitHub), and this computer "
                 f"has other commits on it ({sync['ahead']} here, {sync['behind']} on GitHub). Choose one:")
        c = st.columns(3)
        if c[0].button(TAKE, key=f"{key_prefix}div-take-{gate}", disabled=running,
                       help=f"this computer's commits are kept as refs/pkf-archive/{p}/… (Set-aside work)"):
            act(_resolve, s, p, gate, "take", ok=lambda r: f"took GitHub's copy of {b}"
                + (f"; this computer's commits are kept as {r['archive']}" if (r or {}).get("archive") else ""))
        if c[1].button(MERGE, key=f"{key_prefix}div-merge-{gate}", disabled=running,
                       help="a merge commit on this computer; a conflict stops and names the files"):
            act(_resolve, s, p, gate, "merge", ok=f"merged GitHub's copy into {b}: the next job or Update the PR pushes it")
        if not s.strict:
            if c[2].button(OVERWRITE, key=f"{key_prefix}div-overwrite-{gate}", disabled=running,
                           help="dev mode only: pushes this computer's copy over GitHub's (force with lease); GitHub's "
                                "new commits are lost there"):
                act(_resolve, s, p, gate, "overwrite", ok=f"GitHub's copy of {b} now equals this computer's")
        else:
            c[2].caption("Overwrite is for dev mode only.")
    return True


# ------------------------------------------------------------------------------------------------ the current stage
def merged_unapproved_actions(s, p: str, gs) -> None:
    """A gate merged on GitHub without the required approval (C9, K14): later stages wait until it has one."""
    marker = (s.merged_unapproved(p) if hasattr(s, "merged_unapproved") else None) or {}
    n = (gs.pr or {}).get("number") or marker.get("pr")
    running = busy(s, p)
    c1, c2, c3 = st.columns(3)
    if marker.get("blocked"):  # an approval cannot fix a blocked gate (files outside the pipeline, protected files …)
        st.error(f"{gs.gate} PR #{n} was merged although its gate is blocked ({gs.note}); an approval cannot fix this: "
                 f"Reopen {gs.gate} and request approval again. The later stages are blocked until then.")
        with c1:
            reopen_control(s, p, gs.gate, key=f"mu-reopen-{gs.gate}")
        return
    st.warning(f"{gs.gate} PR #{n} was merged without the required approval ({gs.note}). The later stages are blocked "
               "until it has one.")
    if not s.strict:
        if c1.button(f"Post /approve on PR #{n} as me", key=f"mu-approve-{gs.gate}", disabled=running,
                     help=BUSY_HELP if running else "dev mode: comments /approve on the merged PR with your token, then checks again"):
            act(_post_approve, s, p, gs.gate, ok=f"/approve posted on PR #{n}: checked again")
    else:
        c1.markdown(f"approve it again: Reopen {gs.gate}")
        with c3:
            reopen_control(s, p, gs.gate, key=f"mu-reopen-{gs.gate}")
    if c2.button("Check again", key=f"mu-check-{gs.gate}", help="asks GitHub once more whether the merged PR has its approval"):
        act(_recheck, s, p, ok=f"{gs.gate} has its approval now: start the next stage")


def unmerged_warning(s, p: str, prev: str, states: dict) -> None:
    """Commits on the previous stage's branch that its merged PR did not include (C20)."""
    fn = getattr(s, "unmerged_local_commits", None)
    pr = states["by_gate"][prev].pr if prev in states["by_gate"] else None
    if fn is None or not pr:
        return
    try:
        n = fn(p, prev)
    except Exception:  # noqa: BLE001
        return
    if n:
        k = prev[1:]
        st.warning(f"{n} commits on g{k} were not in the merged PR #{pr['number']}: Reopen {prev} to include them (they are "
                   f"kept as refs/pkf-archive/{p}/g{k}-… when the stage starts)")


def stage_actions(s, p: str, gs, states: dict) -> None:
    g = gs.gate
    if diverged_card(s, p, g):
        gate_card(s, p, gs)
        return
    if g == "RUN":
        gate_card(s, p, gs)
        run_actions(s, p, gs)
        return
    if gs.state == "unknown":
        gate_card(s, p, gs)
        st.info("Where this stage stands cannot be read from GitHub right now: the tabs show this computer's copy. Retry "
                "(top of the page) when GitHub is reachable.")
        return
    if gs.state == "merged_unapproved":
        gate_card(s, p, gs)
        merged_unapproved_actions(s, p, gs)
        return
    if gs.state == "waiting":
        waiting_actions(s, p, gs)
        st.caption("Changes requested? Edit in the tabs (or run the stage's action again), then *Update the PR*: new commits reset approvals.")
        extra_actions(s, p, g, states, waiting=True)
        request_button(s, p, g, states)
        return
    gate_card(s, p, gs)
    if gs.state == "stale":
        st.warning("What this gate approved has changed on the base branch. Reopen it to approve it again "
                   "(later stage PRs are closed; their work is re-checked from the framework's hashes).")
        reopen_control(s, p, g, key=f"reopen-{g}")
        return
    if g == "G1":
        st.markdown("Work in the **Requirements** tab: import, analyse, edit, split. Then request approval.")
        request_button(s, p, "G1", states)
        return
    if gs.state == "not_started" or not stage_started(s, p, g, states):
        prev = ORDER[ORDER.index(g) - 1]
        prev_gs = states["by_gate"].get(prev)
        if prev_gs is not None and prev_gs.state not in DONE:  # merged without approval, unknown …: no Start (K14)
            st.warning(f"{prev} is {BADGES.get(prev_gs.state, prev_gs.state)}: Start {STAGES[g]} waits until it is approved.")
            return
        st.markdown(f"{prev} is approved. Starting **{STAGES[g]}** records {prev}'s approvals in the ledger"
                    + {"G2": " (and splits again only if the requirement changed).",
                       "G3": ", approves the split in the framework and writes the component files to fill in.",
                       "G4": ", approves the plans, then the AI writes the code; tests and the build run.",
                       "G5": ", approves the code, builds the DAG, renders and plans Terraform, and does a dry run."}[g])
        if g == "G4" and states["by_gate"]["G3"].state == "skipped":
            st.caption("G3 is skipped: no component needs an AI plan.")
        if prev_gs is not None and prev_gs.state == "approved":
            unmerged_warning(s, p, prev, states)
        last = next((j for j in jobs.recent(s.root, p, 10) if j.get("action") == START[g]), None)
        if last and last["state"] in ("failed", "crashed"):
            st.error(f"The last start of this stage {last['state']}: {(last.get('result') or {}).get('error', 'see the job log')}. "
                     "Fix the cause and start it again (it picks up where it stopped).")
        job_button(s, p, f"Start {STAGES[g]}", START[g], key=f"start-{g}", primary=True)
        return
    extra_actions(s, p, g, states, waiting=False)
    if g == "G3" and not any_ai_plan(s, p):
        st.caption("No component needs an AI plan, so G3 has nothing to approve: continue to code & build.")
        return
    request_button(s, p, g, states)


def any_ai_plan(s, p: str) -> bool:
    f = Files(s, p)
    return any((f.yaml(f"{L.FOLDERS[k]}/{n}.yaml").get("requirement") or "").strip() for k, n in f.components())


def source_bucket(read, p: str) -> str | None:
    for kind, name in L.components(read, p):
        if kind == "source":
            try:
                spec = yaml.safe_load(read(f"{L.FOLDERS[kind]}/{name}.yaml") or "") or {}
            except yaml.YAMLError:
                return None
            return (spec.get("connection") or {}).get("bucket") if isinstance(spec, dict) else None
    return None


def versioning_off(read, p: str) -> str | None:
    """The S5 record of the source bucket's versioning (D1): the warning when it is not Enabled, else None."""
    def _json(rel):
        try:
            v = json.loads(read(rel) or "{}")
        except ValueError:
            return {}
        return v if isinstance(v, dict) else {}

    v = _json(f"review/{p}/dry-run.json").get("source_versioning")
    if v is None:
        v = (_json(f"infra/{p}/manifest.json").get("facts") or {}).get("source_versioning")
    if not v or v == "Enabled":
        return None
    return f"deletes and overwrites in {source_bucket(read, p) or 'the source bucket'} are irreversible (versioning is off)"


def extra_actions(s, p: str, g: str, states: dict, waiting: bool) -> None:
    f = Files(s, p)
    if g == "G1" or not f.live:
        return
    if g == "G3":
        left = unfilled(f.wt, p)
        if left:
            st.warning(f"{len(left)} environment value(s) to fill in — in the Source / Destination / Trigger tabs:")
            st.code("\n".join(left[:30]), language="text")
        comps = []
        for kind, name in f.components():
            spec = f.yaml(f"{L.FOLDERS[kind]}/{name}.yaml")
            lock = f.yaml(f"{L.FOLDERS[kind]}/{name}.lock.yaml")
            comps.append({"kind": kind, "name": name, "mode": "requirement" if (spec.get("requirement") or "").strip() else "steps",
                          "plan": "yes" if lock.get("plan") else "—"})
        st.dataframe(comps, hide_index=True)
        c1, c2 = st.columns(2)
        with c1:
            job_button(s, p, "Plan components (AI)", "plan_components", key="plan-comps", disabled=bool(left), primary=not waiting)
        needs = [c for c in comps if c["mode"] == "requirement"]
        if not needs and not waiting:
            with c2:
                job_button(s, p, "Continue to code & build (G3 skipped)", "start_g4", key="skip-g3", disabled=bool(left),
                           help="fill in every environment value first" if left else
                           "no component needs an AI plan, so there is nothing to approve at G3")
        elif needs:
            pick = st.selectbox("Plan one component again", [f"{c['kind']}:{c['name']}" for c in needs], key="replan-pick")
            with c2:
                k, n = pick.split(":")
                job_button(s, p, "Re-plan it", "plan_component", {"kind": k, "name": n}, key="replan")
    if g == "G4":
        s4_panel(s, p, f)
    if g == "G5":
        job_button(s, p, "Render & plan again", "prepare_env", key="prepenv")
        off = versioning_off(f.read, p)
        if off:
            st.warning(off)
        man = f.json(f"infra/{p}/manifest.json")
        if man:
            st.markdown(f"**Infrastructure:** {'resources to create (Terraform plan below)' if man.get('needs_apply') else man.get('note', '')}")
            for w in man.get("warnings") or []:
                st.warning(w)
            pt = f.read(f"infra/{p}/envs/dev/plan.txt")
            if pt:
                with st.expander("Terraform plan (dev)"):
                    st.code(pt, language="text")
            with st.expander("Worker IAM policy"):
                st.code(f.read(f"infra/{p}/iam-policy.json") or "", language="json")
                pc = f.json(f"infra/{p}/policy-check.json")
                if pc:
                    st.json(pc, expanded=False)
        dry = f.json(f"review/{p}/dry-run.json")
        if dry:
            st.markdown("**Dry run of the first run** (nothing was changed)")
            dr = dry.get("dry_run") or {}
            if dr.get("note"):
                st.caption(dr["note"])
            rows = dr.get("sample") or dr.get("rows") or []   # D8: the dry run's rows are in `sample`
            if rows:
                st.dataframe(rows[:500], hide_index=True)
                if dr.get("sample_truncated") or len(rows) > 500:
                    st.caption(f"Showing the first {min(len(rows), 500)} planned files; Preview this run (S6) lists every file.")
            for i in dry.get("issues") or []:
                st.warning(i.get("msg"))
            if dry.get("ok") is False and not dry.get("issues"):
                st.warning("the dry run failed: fix it, then Render & plan again")


# ------------------------------------------------------------------------------------------------ S4 status panel
CUSTOM_OPS = ("custom", "custom_delivery")
PLAN_STAGES = ("needs_plan", "plan_stale", "plan_tampered", "plan_proposed")
REPLAN = " — if a test case itself is wrong, Reopen G3 and re-plan {kind} {name}"


def custom_steps(lock: dict) -> list[str]:
    return [x["id"] for x in ((lock.get("plan") or {}).get("steps") or []) if isinstance(x, dict) and x.get("op") in CUSTOM_OPS and x.get("id")]


def s4_status(f: Files, p: str) -> dict:
    """review/<p>/tests.json, read as version 2 (K5); a file without `version` is version 1 (build_ok, build_issues,
    examples only) and has no component rows."""
    t = f.json(f"review/{p}/tests.json")
    if not t:
        return {}
    v = t.get("version") or 1
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 1
    return {**t, "version": v, "components": t.get("components") if v >= 2 and isinstance(t.get("components"), list) else []}


def regenerate_choices(f: Files, rows: list[dict]) -> list[tuple[str, dict]]:
    """The Regenerate (AI) picker (A8): `kind:name (missing or failing steps)` and `kind:name · <step>` for every component
    with custom steps; from tests.json v2, else from the component locks."""
    comps = [(r.get("kind"), r.get("name"), list(r.get("custom_steps") or [])) for r in rows if r.get("custom_steps")]
    if not rows:
        comps = [(k, n, custom_steps(f.yaml(f"{L.FOLDERS[k]}/{n}.lock.yaml"))) for k, n in f.components()]
    out = []
    for k, n, ids in comps:
        if not ids:
            continue
        out.append((f"{k}:{n} (missing or failing steps)", {"kind": k, "name": n}))
        out += [(f"{k}:{n} · {i}", {"kind": k, "name": n, "step": i}) for i in ids]
    return out


def _example_rows(examples) -> list[dict]:
    return [{"example": e.get("example"), "status": e.get("status"), "detail": str(e.get("detail") or "")[:300]}
            for e in examples or [] if isinstance(e, dict)]


MISSING_RE = re.compile(r"missing custom step function\(s\) \[([^\]]*)\]")


def uncoded_steps(r: dict) -> list[str]:
    """Custom steps the code file has no function for, as the component's test reported them (a coder that stopped)."""
    out: list[str] = []
    for x in r.get("failing") or []:
        m = MISSING_RE.search(str((x or {}).get("detail") or "")) if isinstance(x, dict) else None
        for name in re.findall(r"'([^']+)'", m.group(1)) if m else []:
            if name not in out:
                out.append(name)
    return out


def component_row(s, p: str, r: dict) -> None:
    """One component of the S4 status with what to do about it (C2/E1)."""
    kind, name, stage = r.get("kind"), r.get("name"), r.get("stage")
    code_file = r.get("code_file") or f"{L.FOLDERS.get(kind, kind)}/{name}.py"
    steps = list(r.get("custom_steps") or [])
    key = f"{kind}-{name}"
    with st.container(border=True):
        head = f"**{kind} `{name}`** · `{stage}`" + (f" · custom steps: {', '.join(f'`{x}`' for x in steps)}" if steps else "")
        st.markdown(head)
        if stage == "plan_approved":
            st.warning("code not written yet: Regenerate (AI) writes it (the AI coder runs in a background job)")
            for x in r.get("ai_issues") or []:
                st.caption("· " + md_escape(x))
            job_button(s, p, "Regenerate (AI)", "code_component", {"kind": kind, "name": name}, key=f"s4-code-{key}")
        elif stage == "code_failing" and uncoded_steps(r):
            todo = uncoded_steps(r)
            st.warning("code not written yet for " + ", ".join(f"`{x}`" for x in todo)
                       + " (the AI coder stopped before them): Regenerate (AI) writes the missing steps and keeps the others")
            for x in r.get("ai_issues") or []:
                st.caption("· " + md_escape(x))
            job_button(s, p, "Regenerate (AI)", "code_component", {"kind": kind, "name": name}, key=f"s4-code-{key}")
        elif stage == "code_failing":
            st.error("code fails its tests")
            for x in r.get("ai_issues") or []:
                st.caption("· " + md_escape(x))
            failing = [x for x in r.get("failing") or [] if isinstance(x, dict)]
            if failing:
                st.dataframe([{"step": x.get("step") or "", "case": x.get("case") or x.get("example") or "",
                               "detail": str(x.get("detail") or "")[:300]} for x in failing[:50]], hide_index=True)
            bad = [i for i in steps if any(x.get("step") == i for x in failing)] or steps
            cols = st.columns(max(1, min(len(bad), 4)))
            for i, step in enumerate(bad):
                with cols[i % len(cols)]:
                    job_button(s, p, f"Regenerate (AI): {step}", "code_component", {"kind": kind, "name": name, "step": step},
                               key=f"s4-code-{key}-{step}")
            st.caption(f"or edit only the function body in `{code_file}`, then Run tests & build again"
                       + REPLAN.format(kind=kind, name=name))
        elif stage == "code_changed":
            st.warning("the code changed since its last test: Run tests & build again")
            job_button(s, p, "Run tests & build again", "test_build", key=f"s4-test-{key}")
        elif stage == "code_generated":
            st.success(f"ready for review: the code in `{code_file}` passes its tests (approved with G4)")
        elif stage == "approved":
            st.success("approved" + (" (built-in steps only: no code to write)" if not steps else ""))
        elif stage in PLAN_STAGES:
            st.warning("the plan changed: Reopen G3 (in the G3 card below) to plan and approve it again")
        elif stage:
            st.warning(md_escape(r.get("next") or stage) + " — the component's settings were approved at G3: Reopen G3 to change them")


def s4_panel(s, p: str, f: Files) -> None:
    """The S4 status panel: draft build, end-to-end examples, every component with its next action, dags/ left by an
    earlier build (Restore dags/ from main), and the Regenerate (AI) picker."""
    t = s4_status(f, p)
    rows = [r for r in t.get("components") or [] if isinstance(r, dict)]
    with st.container(border=True):
        st.markdown("**S4 status**" + (f" · from `review/{p}/tests.json`" if t else ""))
        c1, c2 = st.columns(2)
        with c1:
            job_button(s, p, "Run tests & build again", "test_build", key="testbuild", primary=True)
        if not t:
            st.caption("No tests or build yet: Run tests & build again.")
        else:
            draft = t.get("build_mode") == "draft"
            what = "Draft build (checks everything, writes nothing; dags/ is built at S5 from approved code)" if draft else "Build"
            if t.get("build_ok"):
                st.success(f"{what}: ok")
            else:
                st.error(f"{what}: failed")
                issues(t.get("build_issues") or [])
            ex = _example_rows(t.get("examples"))
            ex_ok = t.get("examples_ok") if "examples_ok" in t else all(e["status"] in ("pass", "shown") for e in ex)
            if ex:
                st.markdown("**End-to-end examples**: " + ("all pass" if ex_ok else "some fail"))
                st.dataframe(ex, hide_index=True)
            if t["version"] < 2:
                st.caption("This status was written by an earlier version of the Studio: Run tests & build again for the "
                           "status of every component.")
            for r in rows:
                component_row(s, p, r)
            shown = {dags_problem(s.base)}
            other = [x for x in t.get("problems") or [] if str(x).strip() and x not in shown and not str(x).startswith("dags/ on this branch")]
            if other:
                with st.expander(f"Problems of the last S4 job ({len(other)})"):
                    for x in other[:30]:
                        st.markdown("- " + md_escape(x))
        dags_section(s, p, f, t)
        choices = regenerate_choices(f, rows)
        if choices:
            labels = [c[0] for c in choices]
            pick = st.selectbox("Regenerate the code of", labels, key="recode-pick")
            args = dict(choices[labels.index(pick)][1]) if pick in labels else dict(choices[0][1])
            with c2:
                job_button(s, p, "Regenerate (AI)", "code_component", args, key="recode")
        cap = f.read(f"review/{p}/capabilities.md")
        if cap:
            with st.expander("Capability summary (what the generated code can do)"):
                st.markdown(cap)


def dags_section(s, p: str, f: Files, t: dict) -> None:
    """dags/ on the stage branch that differs from the base branch (R10, e.g. a 0.4.0 real build committed at S4)."""
    on_g4 = f.live and s.worktree_branch(p) == branch_for(p, "G4")
    if on_g4:
        try:
            diff = s.dags_diff(p)
        except Exception:  # noqa: BLE001 - fall back to what the last S4 job recorded
            diff = ["dags/"] if t.get("dags_differ") else []
    else:
        diff = ["dags/"] if t.get("dags_differ") else []
    if not diff:
        return
    st.warning(dags_problem(s.base))
    st.caption("Differs: " + ", ".join(f"`{x}`" for x in diff[:8]) + (" …" if len(diff) > 8 else ""))
    if st.button(f"Restore dags/ from {s.base}", key="restore-dags", disabled=busy(s, p) or not on_g4,
                 help=f"puts dags/ for this pipeline back as it is on {s.base}, in one commit; the DAG is built at S5 from approved code"):
        act(s.restore_dags_from_base, p, ok=f"dags/ restored from {s.base} (one commit; it is pushed by the next job or by Request approval)")


def run_actions(s, p: str, gs) -> None:
    f = Files(s, p)
    if gs.state == "blocked":
        st.caption("Every gate must be approved first.")
        return
    running = busy(s, p)
    if gs.state == "waiting" and gs.pr:
        st.markdown(f"Record PR [#{gs.pr['number']}]({gs.pr['html_url']}) is open (no reviewer needed)"
                    + (": it merges when the `pkf-gate` check is green." if s.strict else "."))
        c = st.columns(3)
        if c[0].button("Refresh", key="poll-run"):
            _poll(s)
            st.rerun()
        if s.strict:
            c[1].markdown(f"`pkf-gate`: **{safe(s.gate_check, gs.pr['head']['sha']) or 'missing'}**")
        if c[2].button("Merge the record PR", key="merge-run", type="primary", disabled=running,
                       help=BUSY_HELP if running else None):
            act(_merge, s, p, "RUN", ok="the run record is merged")
        return
    publish_section(s, p)
    own = False
    if not s.cfg.run_role_arn:
        if s.strict or len(s.team.people) > 1:
            st.error("A run role is required (Setup → Run role ARN): approved code runs with at most the generated worker policy.")
            return
        own = st.checkbox("Run with my own AWS credentials (single-person dev team; no run role configured)", key="own-creds")
    off = versioning_off(s.read_base, p)
    if off:
        st.warning(off)
    from .. import actions as A

    try:
        custom = A._custom_delivery(s.read_base, p)
    except Exception:  # noqa: BLE001
        custom = False
    if custom:
        s6_card(s, p, gs, own)
    else:
        n = max_packages_input(s, p, None)
        params = st.text_input("Run parameters (key=value, comma-separated; optional)", key="run-params", persist_state="session")
        st.caption("Runs: apply the approved Terraform plan (if any) → pkf run → report → record PR.")
        job_button(s, p, "Run locally" if gs.state not in ("done", "failed") else "Run again", "run_local",
                   {"own_credentials": own, "params": parse_params(params), "max_packages": n or None},
                   key="run", primary=True, disabled=not (s.cfg.run_role_arn or own))
    last_report(s, p, f)


def parse_params(text: str) -> dict:
    return dict((k.strip(), v.strip()) for k, v in (x.split("=", 1) for x in (text or "").split(",") if "=" in x))


def has_good_run(s, p: str) -> bool:
    """The base branch has a successful run record of this pipeline."""
    try:
        reports = [x for x in s.list_at(s.base_ref, f"review/{p}/runs/") if x.endswith("/report.json")]
    except Exception:  # noqa: BLE001
        return False
    for rel in reports:
        try:
            if json.loads(s.read_base(rel) or "{}").get("ok") is True:
                return True
        except ValueError:
            continue
    return False


def max_packages_input(s, p: str, preview: dict | None) -> int:
    """'Packages in this run (max)': 10 for the first run of a pipeline, else 0 = all (K6, S5)."""
    default = 0 if has_good_run(s, p) else 10
    n = st.number_input("Packages in this run (max)", min_value=0, value=default, step=1, key="run-max",
                        help="0 = every planned package; the others wait for the next run (their messages are released)")
    cap = "0 = all. The first run of a pipeline starts with 10, so you can check the result before moving everything."
    if preview and preview.get("one_file_each"):
        cap += " Every planned package has one file: one file each here."
    st.caption(cap)
    return int(n or 0)


def latest_preview(s, p: str) -> dict | None:
    """The newest preview (preview.json in the git-ignored cache) whose run branch still exists, with "moved" set when
    its branch already has started.json (a Move ran from it)."""
    base = s.root / STUDIO_DIR / "cache" / "runs" / p
    found = []
    for pj in base.glob("*/preview.json") if base.is_dir() else []:
        try:
            doc = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or not doc.get("branch"):
            continue
        if not s.git.sha(f"refs/heads/{doc['branch']}"):
            continue
        run = doc.get("run") or doc["branch"].rsplit("/", 1)[-1]
        doc["moved"] = s.read_at(f"refs/heads/{doc['branch']}", f"review/{p}/runs/{run}/started.json") is not None
        doc.setdefault("planned", str(pj.parent / "planned.json"))
        found.append(doc)
    return max(found, key=lambda d: str(d.get("created_at") or "")) if found else None


def s6_card(s, p: str, gs, own: bool) -> None:
    """S6 for a pipeline that moves or deletes files (custom delivery; D1, K6): Preview this run → the planned files →
    Move <n> files, exactly those, with the preview's run variables."""
    st.markdown("**Preview, then move.** This pipeline moves or deletes files (custom delivery): **Preview this run** "
                "lists every file the run would move, and nothing moves until you click **Move**.")
    prev = latest_preview(s, p)
    n = max_packages_input(s, p, prev)
    params = st.text_input("Run parameters (key=value, comma-separated; optional; used by the preview and its move)",
                           key="run-params", persist_state="session")
    job_button(s, p, "Preview this run" if not prev else "Preview this run again", "preview_run",
               {"params": parse_params(params), "max_packages": n or None}, key="preview", primary=prev is None or prev.get("moved"))
    if not prev:
        st.caption("No preview yet.")
        return
    files, deletes = int(prev.get("files") or 0), int(prev.get("deletes") or 0)
    bucket = prev.get("source_bucket") or "the source bucket"
    with st.container(border=True):
        st.markdown(f"**Preview** `{prev.get('run')}` · {local_time(prev.get('created_at'))} (local time): **{files}** file(s) "
                    f"in {prev.get('packages', 0)} package(s) · source objects deleted: **{deletes}** · to `error/`: "
                    f"**{prev.get('error_files', 0)}**" + (f" · at most {prev['max_packages']} package(s)" if prev.get("max_packages") else ""))
        by = prev.get("by_prefix") or {}
        if by:
            st.dataframe([{"top-level prefix": k, "files": v} for k, v in by.items()], hide_index=True)
        rows = [r for r in prev.get("rows") or [] if isinstance(r, dict)]
        if rows:
            st.dataframe([{"package": r.get("package"), "path": r.get("path"), "source_key": r.get("source_key"),
                           "target_key": r.get("target_key"), "size": r.get("size")} for r in rows[:500]], hide_index=True)
            if files > len(rows[:500]):
                st.caption(f"Showing the first {len(rows[:500])} of {files} files: the full list is in the download.")
        planned = Path(str(prev.get("planned") or ""))
        if planned.is_file():
            st.download_button("Download the full list (planned.json)", planned.read_bytes(), file_name=f"{p}-{prev.get('run')}-planned.json",
                               mime="application/json", key="dl-planned")
    if prev.get("moved"):
        st.info("This preview was moved already: Preview this run again for the next run.")
        return
    if since(prev.get("created_at")) > 24 * 3600:
        st.warning("This preview is older than 24 hours: preview again (a Move refuses an older preview).")
        return
    ok = bool(s.cfg.run_role_arn or own)
    if not ok:
        st.caption("Confirm running with your own AWS credentials above (no run role is set).")
    ack = True
    if deletes:
        ack = st.checkbox(f"I understand {deletes} source objects in {bucket} will be deleted", key=f"ack-deletes-{prev.get('run')}")
    job_button(s, p, f"Move {files} files", "run_local", {"own_credentials": own, "planned": str(prev.get("planned"))},
               key="move", primary=True, disabled=not (ok and ack) or files == 0,
               help="moves exactly the previewed files (files added or changed since the preview wait for the next run)")


def publish_section(s, p: str) -> None:
    """Run records on this computer that are not on GitHub (D11): a record push that failed, or a crashed Move."""
    from .. import actions as A

    try:
        todo = A.unpublished_runs(s, p)
    except Exception:  # noqa: BLE001
        todo = []
    if not todo:
        return
    st.warning(f"{len(todo)} run record(s) on this computer are not on GitHub yet ({', '.join(f'`{b}`' for b in todo[:5])}): "
               "**Publish run record** pushes them and opens the record PR (a Move that did not finish is recorded as failed).")
    job_button(s, p, "Publish run record", "publish_run_record", key="publish-runs")


def last_report(s, p: str, f: Files) -> None:
    """The latest run report (report.json) with its per-file table (files.csv, K7)."""
    reports = sorted(x for x in f.list(f"review/{p}/runs/") if x.endswith("report.json"))
    read = f.read
    if not reports:
        reports = sorted(x for x in s.list_at(s.base_ref, f"review/{p}/runs/") if x.endswith("report.json"))
        read = s.read_base
    if not reports:
        return
    rel = reports[-1]
    try:
        rep = json.loads(read(rel) or "{}")
    except ValueError:
        rep = {}
    counts = ", ".join(f"{k}: {v}" for k, v in (rep.get("counts") or {}).items())
    (st.markdown if rep.get("ok") else st.error)(f"**Last run report** `{rel}`: {'succeeded' if rep.get('ok') else 'FAILED'}"
                                                 + (f" · {counts}" if counts else ""))
    for i in (rep.get("issues") or [])[:5]:
        if isinstance(i, dict) and i.get("msg"):
            st.caption("· " + md_escape(i["msg"]))
    csv_rel = rel.rsplit("/", 1)[0] + "/" + (rep.get("files_csv") or "files.csv")
    text = read(csv_rel)
    if text:
        rows = list(csv.DictReader(io.StringIO(text)))
        st.dataframe(rows[:500], hide_index=True)
        st.download_button("Download files.csv", text.encode("utf-8"), file_name=f"{p}-files.csv", key="dl-files-csv")
    with st.expander("report.json"):
        st.json(rep, expanded=False)


def plan_md(lock: dict, kind: str, name: str) -> str:
    plan = lock.get("plan") or {}
    lines = [f"# {kind} `{name}` — plan", "", plan.get("summary", ""), ""]
    for i, s in enumerate(plan.get("steps", []), 1):
        lines += [f"## {i}. {s.get('title') or s['id']} (`{s['op']}`)", "", "```yaml", yaml.safe_dump(s.get("params") or {}, sort_keys=False).strip(), "```", ""]
    for k in ("assumptions", "ambiguities"):
        if plan.get(k):
            lines += [f"## {k.capitalize()}", ""] + [f"- {x}" for x in plan[k]] + [""]
    ap = lock.get("plan_approval")
    if ap:
        lines.append(f"Approved by {ap.get('by')} ({ap.get('identity')}) at {ap.get('at')}.")
    return "\n".join(lines) + "\n"


def artifacts(s, p: str) -> bytes:
    f = Files(s, p)
    ref = s.git.head(f.wt) if f.live else s.base_ref
    plans = {f"{k}/{n}": plan_md(f.yaml(f"{L.FOLDERS[k]}/{n}.lock.yaml"), k, n) for k, n in f.components()
             if f.yaml(f"{L.FOLDERS[k]}/{n}.lock.yaml").get("plan")}
    logs = [(f"{j.get('started_at', '')[:19].replace(':', '')}-{j.get('action')}", jobs.log(s.root, j["id"], tail=100000))
            for j in jobs.recent(s.root, p, 200)]
    rows = L.parse(f.read(L.ledger_path(p)))
    _, data = bundle.build(s.root, ref, p, plans, logs, self_asserted=any(r.get("self_asserted") for r in rows))
    return data


def dirty_bar(s, p: str, key_prefix: str = "") -> None:
    f = Files(s, p)
    if not f.live or busy(s, p):
        return
    changed = s.changed_files(f.wt)
    if not changed:
        return
    with st.container(border=True):
        st.warning(f"{len(changed)} uncommitted change(s) on `{s.worktree_branch(p)}`: " + ", ".join(f"`{x}`" for x in changed[:6])
                   + (" …" if len(changed) > 6 else ""))
        c1, c2 = st.columns(2)
        if c1.button("Commit them", key=f"{key_prefix}commit-local"):
            act(ui_commit, s, p, "Studio: local edits", ok=lambda sha: f"Committed {sha[:7]}" if sha else "Nothing to commit")
        if c2.button("Discard them", key=f"{key_prefix}discard-local", help="throws the uncommitted changes away and DELETES new (untracked) files listed above — move your own "
                     "notes out of the repository folder first"):
            act(s.discard, p, ok="the uncommitted changes were discarded")


def repair_card(s, p: str) -> None:
    """The pipeline's worktree folder exists but git cannot use it (the repository was moved, `.git` is missing; C10)."""
    wt = s.worktree_path(p)
    if not wt.is_dir():
        return
    try:
        broken = not (wt / ".git").exists() or s.worktree_branch(p) is None
    except Exception:  # noqa: BLE001
        broken = True
    if not broken:
        return
    running = busy(s, p)
    st.warning(f"The worktree folder of {p} (`{wt.as_posix()}`) exists, but git cannot use it (the repository folder was "
               "moved, or its `.git` file is missing). **Repair the worktree** repairs it, or moves the folder aside and checks "
               "the branch out again (commits are safe in the repository; only uncommitted edits stay in the old folder).")
    if st.button("Repair the worktree", key="repair-wt", disabled=running, help=BUSY_HELP if running else None):
        act(_repair, s, p, ok=lambda r: "the worktree is repaired" + (f" (on {r['branch']})" if (r or {}).get("branch") else "")
            + (f"; the broken folder was moved to {r['moved_aside']}" if (r or {}).get("moved_aside") else ""))


def _repair(s, p: str) -> dict:
    out = s.repair_worktree(p)
    refresh(s)
    return out


def archives_section(s, p: str) -> None:
    """Set-aside work (K8): archive refs kept by Reopen, Take GitHub's copy, unmerged commits and crashed moves."""
    try:
        rows = s.archives(p) if hasattr(s, "archives") else []
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        return
    with st.expander(f"Set-aside work (after Reopen) · {len(rows)}"):
        st.caption("Kept on this computer only (never pushed). To get one back, run the command in Git Bash in the repository "
                   "folder: it makes a branch you can look at, merge or push.")
        st.dataframe([{"ref": r.get("ref"), "commit": str(r.get("sha") or "")[:7], "subject": r.get("subject"),
                       "date": r.get("date"), "restore": f"git branch {p}-{str(r.get('ref') or '').rsplit('/', 1)[-1]} {r.get('ref')}"}
                      for r in rows], hide_index=True)


def approval_history(s, p: str) -> None:
    """The approvals ledger on the base branch, plus rows only on the current branch (the Workflow tab; 0.7.0 also the
    Workspace console's Approval history tab)."""
    f = Files(s, p)
    rows = L.parse(s.read_base(L.ledger_path(p)))
    pending = [r for r in L.parse(f.read(L.ledger_path(p))) if r not in rows] if f.live else []
    st.markdown("**Approval history** (ledger on the base branch" + (", plus rows on the current branch" if pending else "") + ")")
    if rows or pending:
        st.dataframe([{"gate": r["gate"], "PR": r["pr"], "approved head": r["head_sha"][:7], "mode": r["mode"],
                       "approvers": ", ".join(f"@{a['login']} ({a['role']})" for a in r["approvers"]),
                       "self-asserted": r.get("self_asserted"), "recorded": r["recorded_at"],
                       "where": "base" if r in rows else "branch"} for r in rows + pending], hide_index=True)
    else:
        st.caption("No approvals recorded yet.")


def render(s, p: str, states: dict) -> None:
    cur = states["current"]
    st.markdown(f"**Mode {s.cfg.mode}** · current stage: **{STAGES.get(cur, cur)}**")
    repair_card(s, p)
    dirty_bar(s, p)
    for gs in states["gates"]:
        if gs.gate == cur:
            st.markdown(f"#### ▶ {STAGES[gs.gate]}")
            stage_actions(s, p, gs, states)
        else:
            gate_card(s, p, gs)
            if gs.state == "approved" and gs.gate in GATES:
                with st.expander(f"Reopen {gs.gate} (found a problem in what it approved?)"):
                    st.caption("Restarts this gate from the base branch and closes the later stage PRs; the later stages are "
                               "approved again after it. Their branch tips are kept as set-aside work.")
                    reopen_control(s, p, gs.gate, key=f"reopen-approved-{gs.gate}")
    archives_section(s, p)
    st.divider()
    approval_history(s, p)
    st.download_button("Download artifacts (zip)", data=lambda: artifacts(s, p), file_name=f"{p}-artifacts.zip", mime="application/zip",
                       key="dl-artifacts")
    with st.expander("Raw stage data"):
        st.json({g.gate: {"state": g.state, "pr": (g.pr or {}).get("html_url"), "note": g.note} for g in states["gates"]}, expanded=False)
        st.caption(json.dumps({"gates": list(GATES)}))
