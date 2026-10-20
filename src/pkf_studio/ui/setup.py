"""Setup: startup checks, project settings and team (a team PR), and who you are."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from .. import MODES, ROLE_LABELS, ROLES, STRICT_MODES, checks
from ..project import Person, ProjectConfig, Team, feasibility, write_local
from .. import teamsetup
from .common import act, refresh, reopen, safe


def render_checks(s, error: str | None) -> list:
    cs = checks.run(s, error=error)
    st.subheader("Startup checks")
    for c in cs:
        icon = "✅" if c.ok else ("❌" if c.required else "⚠️")
        line = f"{icon} **{c.name}**" + (f" — {c.detail}" if c.detail else "")
        st.markdown(line)
        if not c.ok and c.fix:
            st.caption(f"    Fix: {c.fix}")
    if st.button("Check again", key="recheck"):
        reopen()
        st.rerun()
    return cs


def versions() -> str:
    """Which package-factory this app runs, and where its runtime is imported from (the AI code tests use the same one)."""
    import pkf
    import pkf_runtime
    import pkf_studio

    build = getattr(pkf_studio, "BUILD", None) or pkf_studio.VERSION
    return (f"pkf Studio {build} · pkf {pkf.VERSION} · pkf_runtime {getattr(pkf_runtime, 'VERSION', '?')} from "
            f"`{Path(pkf_runtime.__file__).resolve().parent.as_posix()}`")


def render(s) -> None:
    st.header("Setup")
    st.caption(versions())
    tab_team, tab_me = st.tabs(["Project and team", "Who am I"])
    with tab_me:
        who(s)
    with tab_team:
        team_form(s)


def who(s) -> None:
    if not s.team.people:
        st.info("The team roster is not on the base branch yet: write it in *Project and team* and merge the setup PR.")
        return
    names = [f"{p.name} <{p.email}>" for p in s.team.people]
    me = s.me()
    idx = next((i for i, p in enumerate(s.team.people) if me and p.email == me.email), 0)
    choice = st.selectbox("I am", names, index=idx, key="whoami")
    if st.button("Save (this computer only)", key="save-me"):
        def _save():
            write_local(s.root, {"email": s.team.people[names.index(choice)].email})
            refresh(s)

        act(_save, ok=f"saved: you are {choice} on this computer")
    for prob in s.identity_problems():
        st.warning(prob)
    st.caption(f"GitHub token: @{s.login or '—'} · git user.email: {s.git.config('user.email') or '—'}")


def team_form(s) -> None:
    cfg = s.cfg
    st.caption("The project settings and the roster are read from the base branch only. Changing them opens a *team PR* "
               "that the code owners review (lowering the mode also needs an Architect who is not the author).")
    with st.form("project"):
        c1, c2, c3 = st.columns(3)
        mode = c1.selectbox("Mode", MODES, index=MODES.index(cfg.mode),
                            help="dev: one person may hold every role. sit/ppt/prd: different people, enforced on GitHub")
        base = c2.text_input("Base branch", cfg.base_branch)
        api = c3.text_input("GitHub API", cfg.github_api, help="GitHub Enterprise: https://HOST/api/v3")
        c4, c5, c6 = st.columns(3)
        account = c4.text_input("AWS account id (dev)", cfg.aws_account_id or "")
        region = c5.text_input("AWS region", cfg.aws_region)
        run_role = c6.text_input("Run role ARN (optional in a one-person dev team)", cfg.run_role_arn or "")
        c7, c8 = st.columns(2)
        tf_bucket = c7.text_input("Terraform state bucket (S3 backend)", cfg.terraform_backend.get("bucket", ""))
        owners = c8.text_input("Code owners (CODEOWNERS)", " ".join(cfg.code_owners) or (f"@{s.login}" if s.login else ""),
                               help="an admin-managed team such as @org/pkf-admins")
        tags = st.text_area("Mandatory tags (key=value per line)", "\n".join(f"{k}={v}" for k, v in cfg.tags.items()), height=80)
        st.markdown("**Team** — name, e-mail, roles, GitHub login (needed to approve)")
        rows = [{"name": p.name, "email": p.email, "roles": list(p.roles), "github": p.github or ""} for p in s.team.people] or \
               [{"name": "", "email": s.git.config("user.email") or "", "roles": list(ROLES), "github": s.login or ""}]
        edited = st.data_editor(rows, num_rows="dynamic", key="team-editor", column_config={
            "name": st.column_config.TextColumn("Name", required=True),
            "email": st.column_config.TextColumn("E-mail", required=True),
            "roles": st.column_config.MultiselectColumn("Roles", options=list(ROLES), format_func=lambda r: ROLE_LABELS[r]),
            "github": st.column_config.TextColumn("GitHub login")})
        with_gate = st.checkbox("Add the pkf-gate workflow, CODEOWNERS and the framework wheel (required for sit/ppt/prd)",
                                value=True)
        submitted = st.form_submit_button("Open the team PR", type="primary")
    if not submitted:
        return
    try:
        people = []
        for r in edited:
            if not (r.get("name") or "").strip():
                continue
            login = (r.get("github") or "").strip().lstrip("@") or None
            gid = None
            if login and s.gh is not None:
                gid = s.gh.user(login).get("id")
            people.append(Person(name=r["name"].strip(), email=r["email"].strip(), roles=r.get("roles") or [], github=login, github_id=gid))
        team = Team(people=people)
        new = ProjectConfig(mode=mode, base_branch=base.strip() or "main", github_api=api.strip(), aws_account_id=account.strip() or None,
                            aws_region=region.strip(), run_role_arn=run_role.strip() or None,
                            terraform_backend={"bucket": tf_bucket.strip(), "region": region.strip()} if tf_bucket.strip() else {},
                            code_owners=owners.split(), protection_confirmed_by=cfg.protection_confirmed_by,
                            tags=dict(ln.split("=", 1) for ln in tags.splitlines() if "=" in ln))
    except Exception as exc:  # noqa: BLE001 - validation errors from the models
        st.error(f"Not valid: {exc}")
        return
    problems = feasibility(team, mode)
    if problems:
        st.error("This team cannot approve every gate in this mode: " + "; ".join(problems))
        return
    if mode in STRICT_MODES and not with_gate:
        st.error("sit/ppt/prd need the pkf-gate workflow and CODEOWNERS")
        return
    with st.spinner("Writing the files and opening the team PR…"):
        pr = safe(teamsetup.open_pr, s, new, team, new.code_owners, with_gate, st.caption)
    if pr:
        st.success(f"Team PR opened: {pr['html_url']} — merge it on GitHub (code owners review it), then *Check again*.")
        if mode == "dev" and not s.configured:
            st.caption("First setup in dev mode: as a repository admin you can merge it yourself on GitHub.")
