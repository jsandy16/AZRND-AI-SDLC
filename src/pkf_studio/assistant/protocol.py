"""The Assistant's protocol: diagnose → (the user approves the plan) → prepare_fix → (the user approves the diff) →
apply_fix. Each session lives in .pkf-studio/flows/<p>/assistant/<session>/ (transcript.jsonl, diagnosis.json, fix.json,
summary.json) and is written to the run's journal as assistant_* events. The agent never commits: apply_fix is the
Studio committing `assistant: <summary>` after the user's click; discard_fix restores the worktree.

0.8.0: every user prompt starts with the phase rules and the run brief (session.user_prompt), resumed ones too; and
`likely_cause` (phase `brief`) explains the brief's issue in two lines, cached in assistant/brief-cause.json."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import uuid
from pathlib import Path

from .. import flowstore
from . import session as S
from . import tools as T

FIX_KINDS = ["edit_files", "studio_action", "user_action", "rerun", "settings", "none"]
ACTION_KINDS = ["retry", "run_from", "reopen", "fix", "render_plan_again", "tests_build_again", "regenerate", "restore_dags"]
DIAGNOSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "root_cause": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "fix_kind": {"type": "string", "enum": FIX_KINDS},
        "proposal": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "studio_action": {"type": ["string", "null"]},
        "user_action": {"type": ["string", "null"]},
        "needs_reopen": {"type": ["string", "null"], "enum": ["G1", "G2", "G3", "G4", "G5", None]},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "confidence": {"type": "number"},
        # 0.6.1: plain words for a business analyst (2-3 sentences, no paths or jargon); the rest stays the engineer's detail
        "summary": {"type": "string"},
        # 0.6.1: what the Studio should do, in order (knowledge.md §13); reviews, approvals and S1 stay with people
        "actions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"do": {"type": "string", "enum": ACTION_KINDS},
                           "step": {"type": ["string", "null"]}, "gate": {"type": ["string", "null"]},
                           "kind": {"type": ["string", "null"]}, "name": {"type": ["string", "null"]},
                           "why": {"type": "string"}},
            "required": ["do", "why"]}},
    },
    "required": ["root_cause", "evidence", "fix_kind", "proposal", "files", "risk", "confidence", "summary"],
}
FIX_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "object", "additionalProperties": False,
                  "properties": {"ran": {"type": "boolean"}, "ok": {"type": "boolean"}, "detail": {"type": "string"}},
                  "required": ["ran", "ok", "detail"]},
        "notes": {"type": "string"},
        "plain_summary": {"type": "string"},   # 0.6.1: what the fix does, in 1-2 plain sentences
    },
    "required": ["summary", "files_changed", "tests", "notes", "plain_summary"],
}


def new_session_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def session_dir(ctx: T.Context, session_id: str) -> Path:
    return flowstore.assistant_dir(ctx.root, ctx.pipeline, session_id)


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8", newline="\n")


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _summary(d: Path, **fields) -> dict:
    doc = {**_read(d / "summary.json"), **fields, "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()}
    _write(d / "summary.json", doc)
    return doc


CAUSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"cause": {"type": "string"}, "next_step": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["cause", "next_step", "confidence"],
}
CAUSE_TRANSCRIPT = "brief-cause.transcript.jsonl"   # a file, not a session folder: the UI lists folders as sessions


def _context_text(ctx: T.Context) -> str:
    return json.dumps(T.pkf_context(ctx), indent=1, default=str)[:30000]


def _brief_text(ctx: T.Context) -> str:
    from . import brief

    try:
        return brief.text(brief.from_context(ctx))
    except Exception as exc:  # noqa: BLE001 - brief.build never raises; text() on a broken dict might
        return f"(the run brief is unavailable: {type(exc).__name__}: {exc})"


def _prompt(ctx: T.Context, phase: str, body: str) -> str:
    return S.user_prompt(phase, body, _brief_text(ctx), lane=ctx.lane)


def split_answer(text: str) -> tuple[str, str]:
    """(plain summary, details) of a free-text answer written as `In short: … Details: …` (0.6.1); without the markers,
    the first two sentences are the summary and the whole text the details."""
    import re

    t = (text or "").strip()
    m = re.match(r"(?is)^\W*in short\W*:?\s*(.+?)(?:\n\s*\W*details\W*:?\s*(.*))?$", t)
    if m:
        return m.group(1).strip(), (m.group(2) or "").strip()
    return plain_of(t), t


def plain_of(text: str | None, n: int = 2) -> str:
    """The first `n` sentences of `text` (a fallback summary for answers written before 0.6.1)."""
    import re

    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(parts[:n]).strip()


# ------------------------------------------------------------------------------------------------------- phases
def diagnose(ctx: T.Context, question: str | None = None, session_id: str | None = None, log=None) -> dict:
    """Phase 1. Writes diagnosis.json; returns it (with "session_id"). On an LLM failure returns {"error": …}."""
    sid = session_id or new_session_id()
    d = session_dir(ctx, sid)
    ctx.phase = "diagnose"
    flowstore.event(ctx.root, ctx.pipeline, "assistant_started", session=sid, phase="diagnose", step=ctx.step_id, job=ctx.job_id)
    _summary(d, phase="diagnose", step=ctx.step_id, job=ctx.job_id, question=question)
    prompt = _prompt(ctx, "diagnose",
                     f"A substage of the Workflow Developer run failed (step {ctx.step_id or '?'}, job {ctx.job_id or '?'}). "
                     + (f"The user adds: {question}\n\n" if question else "")
                     + "Find the root cause and propose the fix. Start from this briefing (also available as the pkf_context tool), "
                       "then read the job log and the files the evidence points to:\n\n" + _context_text(ctx))
    try:
        out, sdk_sid, usage = S.run_session("diagnose", ctx, prompt, DIAGNOSIS_SCHEMA, d / "transcript.jsonl")
    except (RuntimeError, S.LimitReached) as exc:
        err = str(exc)
        _summary(d, error=err)
        flowstore.event(ctx.root, ctx.pipeline, "assistant_failed", session=sid, phase="diagnose", error=err[:400])
        return {"session_id": sid, "error": err}
    doc = {**out, "session_id": sid, "sdk_session": sdk_sid, "usage": usage, "step": ctx.step_id, "job": ctx.job_id}
    _write(d / "diagnosis.json", doc)
    _summary(d, root_cause=doc.get("root_cause"), fix_kind=doc.get("fix_kind"), sdk_session=sdk_sid, usage=usage,
             plain=doc.get("summary") or plain_of(doc.get("root_cause")), actions=doc.get("actions") or [])
    flowstore.event(ctx.root, ctx.pipeline, "assistant_diagnosis", session=sid, step=ctx.step_id, root_cause=str(doc.get("root_cause"))[:400],
                    fix_kind=doc.get("fix_kind"), files=doc.get("files") or [], risk=doc.get("risk"), needs_reopen=doc.get("needs_reopen"))
    if log:
        log(f"  diagnosis: {doc.get('fix_kind')} — {str(doc.get('root_cause'))[:200]}")
    return doc


def prepare_fix(ctx: T.Context, session_id: str, log=None) -> dict:
    """Phase 2 (after the user approved the plan): edits in the worktree, tests, diff → fix.json. Nothing is committed."""
    d = session_dir(ctx, session_id)
    diag = _read(d / "diagnosis.json")
    if not diag:
        return {"session_id": session_id, "error": "no diagnosis to fix: run Diagnose first"}
    ctx.phase = "fix"
    flowstore.event(ctx.root, ctx.pipeline, "assistant_started", session=session_id, phase="fix", step=diag.get("step"))
    _summary(d, phase="fix")
    prompt = _prompt(ctx, "fix",
                     "The user approved this fix plan — carry it out.\n\nROOT CAUSE: " + str(diag.get("root_cause")) +
                     "\nPLAN: " + str(diag.get("proposal")) + "\nFILES: " + ", ".join(diag.get("files") or []) +
                     "\nEVIDENCE: " + " | ".join(str(x) for x in (diag.get("evidence") or [])[:10]) +
                     "\n\nUse edit_file / write_file (or Edit) inside the worktree only, then run the tests (run_tests, or the "
                     "verifier subagent) and answer with the structured output. Briefing:\n\n" + _context_text(ctx))
    try:
        out, sdk_sid, usage = S.run_session("fix", ctx, prompt, FIX_SCHEMA, d / "transcript.jsonl", resume=diag.get("sdk_session") or None)
    except (RuntimeError, S.LimitReached) as exc:
        err = str(exc)
        _summary(d, error=err)
        flowstore.event(ctx.root, ctx.pipeline, "assistant_failed", session=session_id, phase="fix", error=err[:400])
        return {"session_id": session_id, "error": err, "diff": T.git_diff(ctx)}
    diff = T.git_diff(ctx)
    changed = T.changed_files(ctx)
    # 0.6.1: the Studio runs the tests itself after the agent — `tests` above is what the agent reports; `verified` is
    # what the hardened runner says (the assistant applies a fix on its own only when this is ok)
    try:
        v = T.run_tests(ctx)
        verified = {"ok": bool(v.get("ok")), "stopped_at": v.get("stopped_at")}
    except Exception as exc:  # noqa: BLE001
        verified = {"ok": False, "stopped_at": None, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    doc = {**out, "files_changed": sorted(set(out.get("files_changed") or []) | set(changed)), "diff": diff, "session_id": session_id,
           "usage": usage, "verified": verified}
    _write(d / "fix.json", doc)
    (d / "fix.diff").write_text(diff, encoding="utf-8", newline="\n")
    _summary(d, summary=doc.get("summary"), files_changed=doc["files_changed"], tests=doc.get("tests"), usage=usage)
    flowstore.event(ctx.root, ctx.pipeline, "assistant_fix_proposed", session=session_id, summary=str(doc.get("summary"))[:400],
                    files=doc["files_changed"], tests_ok=(doc.get("tests") or {}).get("ok"))
    if log:
        log(f"  fix proposed: {len(doc['files_changed'])} file(s); tests ok={(doc.get('tests') or {}).get('ok')}")
    return doc


def ask(ctx: T.Context, question: str, session_id: str | None = None, log=None) -> dict:
    """A question about the pipeline or the stage; read-only. Resumes the session when one is given."""
    sid = new_session_id()
    d = session_dir(ctx, sid)
    ctx.phase = "ask"
    prev = _read(session_dir(ctx, session_id) / "summary.json") if session_id else {}
    flowstore.event(ctx.root, ctx.pipeline, "assistant_started", session=sid, phase="ask", question=question[:300])
    _summary(d, phase="ask", question=question)
    prompt = _prompt(ctx, "ask", f"QUESTION: {question}\n\nBriefing:\n\n{_context_text(ctx)}")
    try:
        out, sdk_sid, usage = S.run_session("ask", ctx, prompt, None, d / "transcript.jsonl", resume=prev.get("sdk_session") or None)
    except (RuntimeError, S.LimitReached) as exc:
        err = str(exc)
        _summary(d, error=err)
        flowstore.event(ctx.root, ctx.pipeline, "assistant_failed", session=sid, phase="ask", error=err[:400])
        return {"session_id": sid, "error": err}
    short, detail = split_answer(str(out))
    doc = _summary(d, answer=str(out), plain=short, detail=detail, sdk_session=sdk_sid, usage=usage)
    flowstore.event(ctx.root, ctx.pipeline, "assistant_answer", session=sid, question=question[:300], summary=str(out)[:400])
    if log:
        log(f"  answer: {str(out)[:200]}")
    return {**doc, "session_id": sid}


def likely_cause(ctx: T.Context, log=None, force: bool = False) -> dict:
    """Phase `brief`: the run brief's likely cause and next step in two lines — one model call with no tools, cached on
    the brief's hash (brief.cached_cause), and only when the brief shows an issue or a blocker. Returns
    {"cause", "next_step", "confidence", "hash", "at", "usage", "cached"} or {"skipped": why} or {"error": …}."""
    from . import brief as B

    ctx.phase = "brief"
    b = B.from_context(ctx)
    if not B.needs_cause(b):
        return {"skipped": "nothing to explain: no issue and nothing blocking", "brief": b}
    cached = None if force else B.cached_cause(ctx.root, ctx.pipeline, b)
    if cached:
        if log:
            log("  likely cause: unchanged run state, the stored answer is still valid")
        return {**cached, "cached": True}
    path = B.cause_path(ctx.root, ctx.pipeline)
    prompt = S.user_prompt("brief", "Explain the issue or what blocks the run, and the next step.", B.text(b))
    try:
        out, _sdk_sid, usage = S.run_session("brief", ctx, prompt, CAUSE_SCHEMA, path.with_name(CAUSE_TRANSCRIPT))
    except (RuntimeError, S.LimitReached) as exc:
        return {"error": str(exc)}
    doc = B.store_cause(ctx.root, ctx.pipeline, b, out or {}, usage)
    if log:
        log(f"  likely cause: {doc['cause'][:200]}")
    return {**doc, "cached": False}


# ------------------------------------------------------------------------------------------------------- apply
def apply_fix(studio, pipeline: str, session_id: str) -> str | None:
    """The user approved the diff: the Studio commits it as `assistant: <summary>`; records the sha. Returns the sha."""
    d = flowstore.assistant_dir(studio.root, pipeline, session_id)
    fix = _read(d / "fix.json")
    if not fix:
        raise RuntimeError("no fix to apply in this session")
    summary = str(fix.get("summary") or "fix").splitlines()[0][:100]
    sha = studio.commit(pipeline, f"assistant: {summary}")
    _summary(d, applied=True, sha=sha)
    flowstore.event(studio.root, pipeline, "assistant_fix_applied", session=session_id, sha=sha, files=fix.get("files_changed") or [],
                    summary=summary)
    return sha


def discard_fix(studio, pipeline: str, session_id: str) -> None:
    """Restore the worktree (tracked files back to HEAD, new files removed); records it."""
    d = flowstore.assistant_dir(studio.root, pipeline, session_id)
    wt = studio.worktree_path(pipeline)
    files = [f for f in (_read(d / "fix.json").get("files_changed") or []) if f and ".." not in f]
    if files:
        tracked = set(subprocess.run(["git", "ls-files", "--", *files], cwd=str(wt), capture_output=True, text=True, encoding="utf-8", check=False).stdout.split())
        if tracked:
            subprocess.run(["git", "checkout", "--", *sorted(tracked)], cwd=str(wt), capture_output=True, check=False)
        for f in files:
            if f not in tracked and (wt / f).is_file():
                (wt / f).unlink()
    else:   # the fix did not say which files: everything uncommitted goes
        subprocess.run(["git", "checkout", "--", "."], cwd=str(wt), capture_output=True, check=False)
        subprocess.run(["git", "clean", "-fdq"], cwd=str(wt), capture_output=True, check=False)
    _summary(d, discarded=True)
    flowstore.event(studio.root, pipeline, "assistant_fix_discarded", session=session_id)
