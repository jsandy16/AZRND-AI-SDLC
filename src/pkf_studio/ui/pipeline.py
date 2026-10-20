"""Tab 6 — Pipeline: summary, the data-flow diagram (DOT, downloadable), and the whole codebase in the file navigator."""
from __future__ import annotations

import streamlit as st

from .. import ledger as L
from ..diagram import dot
from ..service import StudioError, branch_for
from .common import BUSY_HELP, Files, act, available_after, busy, file_navigator, ui_commit

SETTINGS_GATES = ("G2", "G3", "G4")   # these fields are in no gate's input hash; they reach the DAG at S5 (A11)


def save_settings(s, p: str, values: dict) -> str | None:
    """Write the pipeline settings into pipelines/<p>.yaml (yamlio keeps comments) and commit "S<n> <p>: pipeline settings"."""
    import datetime as dt

    from pkf import yamlio

    f = Files(s, p)
    gate = next((g for g in SETTINGS_GATES if s.worktree_branch(p) == branch_for(p, g)), None)
    if not f.live or gate is None:
        raise StudioError("pipeline settings can be changed while the worktree is on the S2, S3 or S4 branch")
    owner = str(values.get("owner") or "").strip()
    if not owner:
        raise StudioError("not saved: the owner may not be empty")
    try:
        start = dt.date.fromisoformat(str(values.get("start_date")).strip())
    except ValueError:
        raise StudioError("not saved: the start date must look like 2026-10-01") from None
    path = f.wt / "pipelines" / f"{p}.yaml"
    doc = yamlio.load(path)
    doc["owner"] = owner
    doc["start_date"] = start.isoformat()
    doc["retries"] = int(values.get("retries") or 0)
    doc["retry_delay_minutes"] = int(values.get("retry_delay_minutes") or 5)
    doc["tags"] = [x.strip() for x in str(values.get("tags") or "").split(",") if x.strip()]
    doc["max_active_runs"] = int(values.get("max_active_runs") or 1)
    yamlio.dump(doc, path)
    return ui_commit(s, p, f"S{gate[1:]} {p}: pipeline settings")


def settings_form(s, p: str, f: Files, pspec: dict) -> None:
    """The DAG settings of the pipeline (owner, start date, retries, retry delay, tags, max active runs)."""
    on = f.live and any(s.worktree_branch(p) == branch_for(p, g) for g in SETTINGS_GATES)
    running = busy(s, p)
    with st.expander("Pipeline settings (owner, start date, retries, tags …)", expanded=False):
        if not on:
            st.caption("Editable while the pipeline is at S2, S3 or S4 (they reach the DAG at S5).")
        with st.form(f"settings-{p}"):
            c1, c2, c3 = st.columns(3)
            owner = c1.text_input("Owner", str(pspec.get("owner") or ""), key=f"set-owner-{p}")
            start = c2.text_input("Start date", str(pspec.get("start_date") or ""), key=f"set-start-{p}", help="YYYY-MM-DD")
            mar = c3.number_input("Max active runs", min_value=1, max_value=32, value=int(pspec.get("max_active_runs") or 1),
                                  key=f"set-mar-{p}")
            c4, c5, c6 = st.columns(3)
            retries = c4.number_input("Retries", min_value=0, max_value=10, value=int(pspec.get("retries", 2) or 0), key=f"set-retries-{p}")
            delay = c5.number_input("Retry delay (minutes)", min_value=1, max_value=1440,
                                    value=int(pspec.get("retry_delay_minutes") or 5), key=f"set-delay-{p}")
            tags = c6.text_input("Tags (comma-separated)", ", ".join(map(str, pspec.get("tags") or [])), key=f"set-tags-{p}")
            if st.form_submit_button("Save pipeline settings", disabled=not on or running, help=BUSY_HELP if running else None):
                act(save_settings, s, p, {"owner": owner, "start_date": start, "retries": retries, "retry_delay_minutes": delay,
                                          "tags": tags, "max_active_runs": mar},
                    ok=lambda sha: f"pipeline settings saved ({sha[:7]})" if sha else "pipeline settings unchanged")


def pipeline_files(f: Files, p: str) -> list[str]:
    comps = f.components()
    prefixes = [f"requirements/{p}/", f"pipelines/{p}.", f"infra/{p}/", f"review/{p}/", f"dags/pkf_{p}.py", f"dags/pkf/{p}/", L.ledger_path(p)]
    for kind, name in comps:
        prefixes.append(f"{L.FOLDERS[kind]}/{name}.")
    try:
        import json

        for b in json.loads(f.read(f"infra/{p}/manifest.json") or "{}").get("buckets", []):
            prefixes.append(f"infra/buckets/{b}/")
    except ValueError:
        pass
    files = [x for x in f.list() if x.startswith(tuple(prefixes))]
    return sorted(files, key=lambda x: (x.startswith((".pkf-studio", "requirements/.llm")), not x.startswith("pipelines/"), x))


def flow(f: Files, p: str) -> list[dict]:
    out = []
    pspec = f.yaml(f"pipelines/{p}.yaml")
    comps = dict(f.components())
    if "trigger" not in comps:
        out.append({"kind": "trigger", "name": "schedule", "steps": [{"id": "tick", "op": "schedule", "title": "the pipeline's schedule"}],
                    "schedule": pspec.get("schedule")})
    for kind in ("trigger", "source", "transformation", "destination"):
        if kind in comps:
            name = comps[kind]
            spec = f.yaml(f"{L.FOLDERS[kind]}/{name}.yaml")
            lock = f.yaml(f"{L.FOLDERS[kind]}/{name}.lock.yaml")
            mode = "requirement" if (spec.get("requirement") or "").strip() else "steps"
            steps = ((lock.get("plan") or {}).get("steps") if mode == "requirement" else spec.get("steps")) or []
            out.append({"kind": kind, "name": name, "steps": steps, "mode": mode, "schedule": spec.get("schedule")})
    return out


def render(s, p: str, states: dict) -> None:
    f = Files(s, p)
    pspec = f.yaml(f"pipelines/{p}.yaml")
    if not pspec:
        available_after("Available after S1.", key="aa-pipe")
        return
    st.caption(f"Showing {f.where()}")
    st.subheader(p)
    st.markdown(pspec.get("description") or "")
    comps = f.components()
    rows = []
    for g in states["gates"]:
        rows.append({"stage": g.gate, "title": g.title, "state": g.state.replace("_", " ")})
    c1, c2 = st.columns([3, 2])
    with c1:
        if comps:
            st.markdown("**Components:** " + " → ".join(f"{k} `{n}`" for k, n in comps))
        else:
            st.markdown("**Components:** not split yet")
        st.markdown(f"**Owner:** {pspec.get('owner', '—')} · **retries:** {pspec.get('retries', '—')} · "
                    f"**max active runs:** {pspec.get('max_active_runs', 1)}")
    with c2:
        st.dataframe(rows, hide_index=True)
    settings_form(s, p, f, pspec)
    st.markdown("**Data flow**")
    d = dot(p, flow(f, p))
    st.graphviz_chart(d, width="stretch")
    st.download_button("Download the diagram (.dot)", d.encode(), file_name=f"{p}.dot", key="dl-dot")
    st.markdown("**Codebase**")
    file_navigator(f, pipeline_files(f, p), key=f"nav-{p}")
