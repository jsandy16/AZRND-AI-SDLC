"""The Artifacts page (0.7.0, the ST-2 v2 Artifacts template): the pipeline's files grouped by stage on the left; the
chosen file full height on the right — Code · Plan cards · Tests · Diff vs main (P6) · Capabilities · Data-flow diagram ·
Requirement — with Regenerate (AI) for a component's code and the artifacts zip. Read-only except Regenerate, which starts
the same job as the Workflow tab's S4 panel."""
from __future__ import annotations

import difflib

import streamlit as st
import yaml

from .. import ledger as L
from ..diagram import dot
from .common import Files, code_view, plan_cards
from .pipeline import flow as data_flow
from .pipeline import pipeline_files

GROUPS = ["01 Requirements", "02 Design · 03 Plans", "04 Code", "05 Infra · 06 Deploy", "07 Runs", "Other"]
PLACEHOLDERS = {"05 Infra · 06 Deploy": "infra/ and dags/ are built at S5", "07 Runs": "review/…/runs/ after S6"}


def group_of(rel: str, p: str) -> str:
    """The stage folder a file is listed under."""
    review = f"review/{p}/"
    if rel.startswith(f"requirements/{p}/") or rel == f"pipelines/{p}.yaml":
        return GROUPS[0]
    if rel.startswith(review + "runs/"):
        return GROUPS[4]
    if rel.startswith(("infra/", "dags/")) or rel in (review + "dry-run.json", review + "deploy.md"):
        return GROUPS[3]
    if rel.endswith(".py") or rel in (review + "tests.json", review + "capabilities.md"):
        return GROUPS[2]
    if rel.endswith((".yaml", ".yml")) or rel.startswith("connections/") or rel == review + "examples.json":
        return GROUPS[1]
    return GROUPS[5]


def _component_of(f: Files, rel: str) -> tuple[str, str] | None:
    for kind, name in f.components():
        if rel.startswith(f"{L.FOLDERS[kind]}/{name}."):
            return kind, name
    return None


def diff_vs_base(s, f: Files, rel: str) -> str:
    """The file as it stands (worktree, uncommitted edits included) against the base branch."""
    base = s.read_base(rel)
    now = f.read(rel)
    if now is None:
        return ""
    lines = difflib.unified_diff((base or "").splitlines(keepends=True), now.splitlines(keepends=True),
                                 fromfile=f"{s.base}:{rel}" if base is not None else "/dev/null", tofile=rel)
    return "".join(lines)


def render(s, p: str, states: dict) -> None:
    from .workflow import artifacts as zip_artifacts

    from .. import flow
    from .common import busy, job_panel
    from .workspace import _regenerate

    job_panel(s, p)                 # the running job's live log, Cancel, the last job — as on the Pipelines page
    f = Files(s, p)
    files = pipeline_files(f, p)
    sel_key = f"art-sel-{p}"
    want = st.query_params.get("file")
    if want:
        if want in files:
            st.session_state[sel_key] = want
        try:
            del st.query_params["file"]          # consumed: a later click in the list wins
        except KeyError:
            pass
    sel = st.session_state.get(sel_key)
    if sel not in files:
        sel = files[0] if files else None
    head = st.columns([6, 2], vertical_alignment="center")
    head[0].caption(f"Showing {f.where()} · {len(files)} file(s)")
    head[1].download_button("⬇ Download artifacts (zip)", data=lambda: zip_artifacts(s, p), file_name=f"{p}-artifacts.zip",
                            mime="application/zip", key=f"art-zip-{p}", width="stretch")
    left, right = st.columns([3, 9], gap="medium")
    with left, st.container(border=True, key="art-left"):
        q = st.text_input("Filter files", key=f"art-q-{p}", placeholder="filter files…", label_visibility="collapsed")
        shown = [x for x in files if q.lower() in x.lower()] if q else files
        groups: dict[str, list[str]] = {}
        for rel in shown:
            groups.setdefault(group_of(rel, p), []).append(rel)
        for title in GROUPS:
            items = groups.get(title) or []
            if not items and title not in PLACEHOLDERS:
                continue
            st.caption(title.upper())
            if not items:
                st.caption(f"*{PLACEHOLDERS[title]}*")
            for rel in items:
                st.button(rel, key=f"art-f-{p}-{rel}", type="primary" if rel == sel else "tertiary", width="stretch",
                          on_click=st.session_state.__setitem__, args=(sel_key, rel))
    with right, st.container(border=True, key="art-right"):
        if not sel:
            st.info("No files yet: the pipeline's files appear here as the stages write them.")
            return
        comp = _component_of(f, sel)
        c = st.columns([7, 2], vertical_alignment="center")
        c[0].markdown(f"**`{sel}`**" + (f" · {comp[0]} `{comp[1]}`" if comp else ""))
        if comp and sel.endswith(".py"):
            with c[1]:
                _regenerate(s, p, states, flow.load(s.root, p), {"kind": comp[0], "name": comp[1]}, "Regenerate (AI)",
                            f"art-regen-{p}", busy(s, p))
        t = st.tabs(["Code", "Plan cards", "Tests", f"Diff vs {s.base}", "Capabilities", "Data-flow diagram", "Requirement"])
        with t[0]:
            code_view(f.read(sel), sel, key=f"art-{sel}", abs_path=(f.wt / sel) if f.live else None, height=600)
        with t[1]:
            if comp:
                lock = f.yaml(f"{L.FOLDERS[comp[0]]}/{comp[1]}.lock.yaml")
                spec = f.yaml(f"{L.FOLDERS[comp[0]]}/{comp[1]}.yaml")
                plan_cards(lock if (spec.get("requirement") or "").strip() else {"plan": {"steps": spec.get("steps") or []}})
            else:
                st.caption("Plan cards belong to a component's files (sources/, destinations/, transformations/, triggers/).")
        with t[2]:
            tests_view(f, p, comp)
        with t[3]:
            d = diff_vs_base(s, f, sel)
            if d:
                st.code(d[:200_000], language="diff", height=600)
            else:
                st.caption(f"No difference from {s.base}." if f.read(sel) is not None else "The file does not exist yet.")
        with t[4]:
            cap = f.read(f"review/{p}/capabilities.md")
            st.markdown(cap) if cap else st.caption("The capability summary is written at S4.")
        with t[5]:
            if f.yaml(f"pipelines/{p}.yaml"):
                d = dot(p, data_flow(f, p))
                st.graphviz_chart(d, width="stretch")
                st.download_button("Download the diagram (.dot)", d.encode(), file_name=f"{p}.dot", key=f"art-dot-{p}")
            else:
                st.caption("Available after S1.")
        with t[6]:
            spec = f.yaml(f"pipelines/{p}.yaml")
            if spec.get("requirement"):
                st.markdown(spec["requirement"])
                pc = f.read(f"requirements/{p}/pseudocode.md")
                if pc:
                    with st.expander("Pseudocode"):
                        st.code(pc, language="markdown", wrap_lines=True)
            else:
                st.caption("Available after S1 (the requirement is analysed).")


def tests_view(f: Files, p: str, comp: tuple[str, str] | None) -> None:
    """The component's tests (its lock), its row of review/<p>/tests.json; without a component, the S4 summary."""
    from .workflow_dev import s4_summary

    if comp:
        lock = f.yaml(f"{L.FOLDERS[comp[0]]}/{comp[1]}.lock.yaml")
        tests = (lock.get("code") or {}).get("tests") or []
        if tests:
            st.dataframe([{"test": x.get("example") or x.get("case"), "status": x.get("status"), "detail": (x.get("detail") or "")[:200]}
                          for x in tests], hide_index=True)
        rows = [r for r in (f.json(f"review/{p}/tests.json").get("components") or [])
                if isinstance(r, dict) and (r.get("kind"), r.get("name")) == comp]
        for r in rows:
            failing = [x for x in r.get("failing") or [] if isinstance(x, dict)]
            st.markdown(f"**S4 status:** `{r.get('stage') or r.get('state')}`")
            if failing:
                st.dataframe([{"step": x.get("step") or "", "case": x.get("case") or x.get("example") or "",
                               "detail": str(x.get("detail") or "")[:300]} for x in failing[:50]], hide_index=True)
        if not tests and not rows:
            st.caption("No tests for this component yet (S4).")
            sim = lock.get("simulation") or []
            if sim:
                st.code(yaml.safe_dump(sim, sort_keys=False)[:5000], language="yaml")
        return
    s4_summary(f, p)
