"""Tab 1 — Requirements: import (upload or paste), the AI's finalized requirement, pseudocode, examples, assumptions,
open questions and the split; Save, Analyze, Split again, Request approval (G1)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import tempfile
from pathlib import Path

import streamlit as st
import yaml

from .. import docimport, jobs
from ..scan import scan
from ..service import branch_for
from .common import BUSY_HELP, Files, act, busy, issues, job_button, ui_commit


def set_pipeline_fields(wt: Path, p: str, **fields) -> None:
    from pkf import yamlio

    path = wt / "pipelines" / f"{p}.yaml"
    doc = yamlio.load(path)
    for k, v in fields.items():
        doc[k] = v
    yamlio.dump(doc, path)


def import_text(s, p: str, original_name: str, data: bytes | None, text: str, flags: list[str]) -> None:
    wt = s.open_stage(p, "G1")
    rdir = wt / "requirements" / p
    rdir.mkdir(parents=True, exist_ok=True)
    for old in rdir.glob("original.*"):
        old.unlink()
    if data is not None:
        stored, _ = docimport.safe_name(original_name)
        (rdir / stored).write_bytes(data)
    (rdir / "extracted.txt").write_text(text.replace("\r\n", "\n").strip() + "\n", encoding="utf-8", newline="\n")
    meta = {"original_name": Path(original_name).name if data is not None else "pasted text",
            "sha256": hashlib.sha256(data if data is not None else text.encode()).hexdigest(),
            "imported_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "chars": len(text), "flags": flags,
            "pii_confirmed": False}
    (rdir / "meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8", newline="\n")


def _extract_upload(up) -> dict:
    data = up.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    cache = st.session_state.setdefault("_imports", {})
    if sha not in cache:
        try:
            _, ext = docimport.safe_name(up.name)
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
                f = Path(d) / f"doc{ext}"
                f.write_bytes(data)
                cache[sha] = {"ok": True, **docimport.extract_isolated(f)}
        except docimport.ImportFailed as exc:
            cache[sha] = {"ok": False, "error": str(exc)}
    return {**cache[sha], "data": data, "name": up.name, "sha256": sha}


def _findings(s, text: str):
    return scan(text, {x.email for x in s.team.people})


def _show_findings(findings) -> bool:
    """Shows them; True when a secret blocks."""
    sec = [f for f in findings if f.kind == "secret"]
    pii = [f for f in findings if f.kind == "pii"]
    if sec:
        st.error("Remove these before going on (they look like secrets): " + "; ".join(f"line {f.line}: {f.what} ({f.excerpt})" for f in sec[:8]))
    if pii:
        st.warning("Possible personal data (requirements and examples must not contain patient or personal data): " +
                   "; ".join(f"line {f.line}: {f.what}" for f in pii[:8]))
    return bool(sec)


def importer(s, p: str, f: Files, editable: bool) -> None:
    meta = f.yaml(f"requirements/{p}/meta.yaml")
    if meta:
        st.caption(f"Imported: **{meta.get('original_name')}** · {meta.get('chars')} characters · {str(meta.get('imported_at'))[:16]} · "
                   f"sha256 {str(meta.get('sha256'))[:12]}")
        for fl in meta.get("flags") or []:
            st.warning(fl)
    if not editable:
        return
    with st.expander("Import a requirement document" + (" (replace)" if meta else ""), expanded=not meta):
        up = st.file_uploader("Requirement document (.txt .md .docx .pdf, max 10 MB)", type=["txt", "md", "docx", "pdf"], key=f"up-{p}")
        if up is not None:
            res = _extract_upload(up)
            if not res["ok"]:
                st.error(res["error"])
            elif meta and meta.get("sha256") == res["sha256"]:
                st.info("This document is already imported.")
            else:
                for fl in res["flags"]:
                    st.warning(fl)
                blocked = _show_findings(_findings(s, res["text"]))
                st.text_area("Extracted text (this is what the AI sees)", res["text"], height=220, disabled=True, key=f"prev-{p}-{res['sha256'][:8]}")
                if st.button("Use this document", key=f"use-doc-{p}", disabled=blocked, type="primary"):
                    act(import_text, s, p, res["name"], res["data"], res["text"], res["flags"], ok="document imported")
        pasted = st.text_area("…or paste the requirement", key=f"paste-{p}", height=160, persist_state="session")
        if pasted.strip():
            blocked = _show_findings(_findings(s, pasted))
            if st.button("Use this text", key=f"use-text-{p}", disabled=blocked):
                act(import_text, s, p, "pasted text", None, pasted, [], ok="text imported")


def render(s, p: str, states: dict) -> None:
    f = Files(s, p)
    g1 = states["by_gate"]["G1"]
    on_g1 = s.worktree_branch(p) in (None, branch_for(p, "G1"))
    open_state = g1.state in ("not_started", "in_progress", "waiting")
    running = busy(s, p)
    editable = open_state and on_g1 and (f.live or g1.state == "not_started") and not running
    st.caption(f"Showing {f.where()} · G1 is **{g1.state.replace('_', ' ')}**" +
               ("" if open_state else " — to change the requirement, reopen G1 in Workflow"))
    if running:
        st.info("A job is running for this pipeline (log above); editing is paused until it ends.")
    if open_state and on_g1 and not f.live and g1.state != "not_started":
        if st.button("Open for editing", key=f"open-g1-{p}", disabled=running,
                     help=BUSY_HELP if running else "check out the G1 branch in this pipeline's worktree"):
            act(s.open_stage, p, "G1")
    importer(s, p, f, editable)
    extracted = f.read(f"requirements/{p}/extracted.txt")
    meta = f.yaml(f"requirements/{p}/meta.yaml")
    if extracted:
        with st.expander("The imported document's text (what the AI saw)"):
            st.code(extracted, language="text", wrap_lines=True, height=300)
    if extracted and editable:
        findings = _findings(s, extracted)
        blocked = _show_findings(findings)
        pii = any(x.kind == "pii" for x in findings)
        if pii:
            conf = st.checkbox("I confirm the document contains no patient or personal data (the matches above are not personal data)",
                               value=bool(meta.get("pii_confirmed")), key=f"pii-{p}")
            if conf != bool(meta.get("pii_confirmed")) and (f.wt / f"requirements/{p}").is_dir():
                meta["pii_confirmed"] = conf
                (f.wt / f"requirements/{p}/meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8", newline="\n")
        job_button(s, p, "Analyze with AI", "analyze", key=f"analyze-{p}", disabled=blocked or (pii and not meta.get("pii_confirmed")),
                   help="the requirement analyst writes the finalized requirement, examples and pseudocode, then the split runs",
                   primary=not f.yaml(f"pipelines/{p}.yaml").get("requirement"))
    spec = f.yaml(f"pipelines/{p}.yaml")
    if not spec.get("requirement"):
        if not extracted:
            st.info("Import or paste a requirement document to start.")
        return
    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("Finalized requirement")
        key = f"req-{p}-{hashlib.sha256(spec['requirement'].encode()).hexdigest()[:8]}"  # a new key when the file changes

        def save_req():
            act(set_pipeline_fields, f.wt, p, requirement=st.session_state[key].strip() + "\n", rerun=False)  # (a callback)

        st.text_area("Requirement (TRIGGER / SOURCE / TRANSFORMATION / DESTINATION / DELIVERY)", spec["requirement"], height=420, key=key,
                     on_change=save_req, disabled=not editable, persist_state="session")
        with st.expander("Preview"):
            st.markdown(st.session_state.get(key, spec["requirement"]))
    with right:
        st.subheader("Pseudocode")
        st.code(f.read(f"requirements/{p}/pseudocode.md") or "(none)", language="markdown", height=420, wrap_lines=True)
    st.subheader("End-to-end examples")
    ex_text = yaml.safe_dump(spec.get("examples") or [], sort_keys=False, allow_unicode=True)
    ex_key = f"ex-{p}-{hashlib.sha256(ex_text.encode()).hexdigest()[:8]}"

    def save_examples():
        from pkf.specs.design import PipelineExample

        try:
            data = yaml.safe_load(st.session_state[ex_key]) or []
            for e in data:
                PipelineExample.model_validate(e)
            set_pipeline_fields(f.wt, p, examples=data)
            st.session_state[f"ex-{p}-err"] = None
        except Exception as exc:  # noqa: BLE001
            st.session_state[f"ex-{p}-err"] = (ex_key, f"not saved — the examples are not valid: {exc}"[:800])

    st.text_area("Examples (YAML; synthetic data only)", ex_text, height=260, key=ex_key, on_change=save_examples, disabled=not editable,
                 persist_state="session")
    err = st.session_state.get(f"ex-{p}-err")
    if err and err[0] == ex_key:  # an error belongs to the text it was about
        st.error(err[1])
    analysis = f.json(f"requirements/{p}/analysis.json")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Assumptions**")
        for a in analysis.get("assumptions") or ["—"]:
            st.markdown(f"- {a}")
    with c2:
        st.markdown("**Open questions** (answer them in the requirement before approval)")
        qs = analysis.get("open_questions") or []
        for q in qs or ["none"]:
            st.markdown(f"- {q}")
    lock = f.yaml(f"pipelines/{p}.lock.yaml")
    st.subheader("The split")
    if not lock.get("split"):
        st.caption("Not split yet.")
    else:
        sp = lock["split"]
        if sp.get("summary"):
            st.markdown(sp["summary"])
        st.dataframe([{"kind": kind, "name": c.get("name"), "reuse": c.get("reuse"),
                       "requirement": ((c.get("spec") or {}).get("requirement") or "steps" if not c.get("reuse") else "existing")[:200]}
                      for kind, c in (sp.get("components") or {}).items()], hide_index=True)
        for title, k in (("Ambiguities (answer before approval)", "ambiguities"), ("To fill in later (environment values)", "to_fill_in"),
                         ("Errors", "errors")):
            if sp.get(k):
                st.markdown(f"**{title}**")
                for x in sp[k]:
                    st.markdown(f"- {x if isinstance(x, str) else json.dumps(x)}")
    last = jobs.recent(s.root, p, 1)
    if last and last[0].get("action") in ("analyze", "resplit"):
        issues((last[0].get("result") or {}).get("issues") or [])
    if editable:
        c1, c2, c3 = st.columns(3)
        if c1.button("Save (commit)", key=f"save-req-{p}"):
            act(ui_commit, s, p, f"S1 {p}: requirement edited",
                ok=lambda sha: f"Committed {sha[:7]}" if sha else "Nothing to commit")   # no toast on an error (E16)
        with c2:
            job_button(s, p, "Split again", "resplit", key=f"resplit-{p}", help="after editing the requirement")
        from .workflow import request_button

        with c3:
            request_button(s, p, "G1", states)
