"""The run brief (0.8.0): where one pipeline's run stands, built by code from what the Studio already has on disk — the
run's state.json, the gate states the caller passes in, review/<p>/tests.json, the job folders, the pilot and the
pipeline's memory notes. Deterministic, no LLM, no git, no GitHub, and it never raises: offline, without a worktree
or without a run it degrades to what is known.

Used (a) in every assistant user prompt, so each turn — a resumed one too — starts from the current state, (b) by the
workflow_state tool, mid-session, and (c) by the UI, which renders the dict and shows `text()`.

The *likely cause* lines are one tool-less model call (phase `brief`, actions.assist) cached per pipeline in
`.pkf-studio/flows/<p>/assistant/brief-cause.json` on a hash of the brief text without its elapsed times: reopening the
page costs nothing while nothing changed."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from .. import STUDIO_DIR

CAUSE_FILE = "brief-cause.json"
GATES = ("G1", "G2", "G3", "G4", "G5", "RUN")
GATE_DONE = ("approved", "skipped", "done")
GATE_TROUBLE = ("stale", "merged_unapproved", "unknown", "failed")
WHO_APPROVES = {"G1": "PO + BA + Architect"}
LABEL = 10


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def elapsed_text(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _since(ts: str | None) -> float | None:
    try:
        t = dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (_now() - t).total_seconds()


def gate_states(states) -> dict[str, dict]:
    """{gate: {"state", "note", "pr"}} from either stages.derive()'s dict (GateState rows under "gates" / "by_gate") or
    the assistant's own gates dict ({"G1": {"state": …}} — what actions.assist passes; {"offline": True, …} → {})."""
    out: dict[str, dict] = {}
    if not isinstance(states, dict):
        return out
    rows = states.get("gates") if isinstance(states.get("gates"), list) else list((states.get("by_gate") or {}).values())
    try:
        if rows:
            for g in rows:
                get = g.get if isinstance(g, dict) else (lambda k, _g=g: getattr(_g, k, None))
                name = get("gate")
                if name:
                    pr = get("pr")
                    out[str(name)] = {"state": str(get("state") or "unknown"), "note": str(get("note") or ""),
                                      "pr": pr.get("number") if isinstance(pr, dict) else pr}
            return out
        for k, v in states.items():
            if k in GATES:
                if isinstance(v, dict):
                    out[k] = {"state": str(v.get("state") or "unknown"), "note": str(v.get("note") or ""), "pr": v.get("pr")}
                elif v is not None:
                    out[k] = {"state": str(getattr(v, "state", v)), "note": str(getattr(v, "note", "") or ""), "pr": None}
    except Exception:  # noqa: BLE001 - a shape we do not know: no gates rather than no brief
        return {}
    return out


def _who(step) -> str:
    if step.kind in ("job", "auto"):
        return "run"
    if step.kind == "gate":
        if step.phases == ("approve",):
            return WHO_APPROVES.get("G1", "approvers")
        if step.phases in (("request",), ("merge",)):
            return "you"
        return "DE"
    return "you"


def _read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _last_detail(log_lines: list[str]) -> str:
    """The running job's latest framework step (`$ pkf …`) or heartbeat line: what it is doing now."""
    for ln in reversed(log_lines):
        t = ln.strip()
        if t.startswith(("$ pkf", "… still running")):
            return t[:200]
    return ""


def build(studio, pipeline: str, states: dict | None = None, flow_state: dict | None = None, n_log: int = 8, *,
          root: Path | None = None, worktree: Path | None = None, mode: str | None = None, readiness: bool = False) -> dict:
    """The run brief as a dict (see the module docstring). `studio` may be None when `root` (and optionally `worktree`,
    `mode`) are given — the assistant's job has no Studio open in the lane. `flow_state` None → read state.json.
    `readiness=True` adds the current gate's live readiness items (service.readiness: reads files and git — off by
    default so the brief stays git-free). Never raises."""
    try:
        return _build(studio, pipeline, states, flow_state, n_log, root, worktree, mode, readiness)
    except Exception as exc:  # noqa: BLE001 - the brief must never break the page or the prompt
        return {"pipeline": pipeline, "mode": mode or "dev", "run_status": "unknown", "message": f"(the run brief failed: {exc})",
                "done": [], "now": None, "next": [], "issue": None, "blocking": [], "log_tail": [], "gates": {}, "pilot": None,
                "memory": []}


def _build(studio, p: str, states, st, n_log: int, root, worktree, mode, readiness: bool) -> dict:
    from .. import flow, jobs
    from . import memory

    root = Path(root if root is not None else studio.root)
    try:
        wt = Path(worktree) if worktree is not None else (studio.worktree_path(p) if studio is not None
                                                          else root / STUDIO_DIR / "worktrees" / p)
    except Exception:  # noqa: BLE001
        wt = root / STUDIO_DIR / "worktrees" / p
    if st is None:
        try:
            st = flow.load(root, p)
        except Exception:  # noqa: BLE001
            st = None
    st = st if isinstance(st, dict) else {}
    if mode is None:
        try:
            mode = studio.cfg.mode if studio is not None else None
        except Exception:  # noqa: BLE001
            mode = None
    mode = mode or st.get("mode") or "dev"
    gates = gate_states(states)
    steps = st.get("steps") if isinstance(st.get("steps"), dict) else {}
    run_status = str(st.get("status") or "idle")
    active = run_status != "idle" and bool(st.get("cursor"))
    cursor = st.get("cursor") if active else None
    step = flow.BY_ID.get(cursor or "")
    row = steps.get(cursor or "") or {}

    # --- the job whose log matters: the current step's, else the pipeline's running job, else its last one
    running = None
    try:
        running = jobs.current(root, p)
    except Exception:  # noqa: BLE001
        running = None
    log_job = row.get("job_id") or running
    recent: list[dict] | None = None

    def last_job() -> dict | None:
        nonlocal recent
        if recent is None:
            try:
                recent = jobs.recent(root, p, n=1)
            except Exception:  # noqa: BLE001
                recent = []
        return recent[0] if recent else None
    if not log_job:
        lj = last_job()
        log_job = lj.get("id") if lj else None
    log_tail: list[str] = []
    if log_job:
        try:
            log_tail = [ln[:400] for ln in jobs.log(root, log_job, tail=max(0, int(n_log))).splitlines()]
        except Exception:  # noqa: BLE001
            log_tail = []

    # --- done: stages whose gate is approved/skipped, or (no gate) whose steps are all done
    done = []
    for sid in flow.STAGE_IDS:
        g = flow.STAGE_GATE.get(sid)
        gs = (gates.get(g) or {}).get("state") if g else None
        rows = [steps.get(x.id) or {} for x in flow.STEPS if x.stage == sid]
        all_done = bool(rows) and all((r.get("status") in flow.DONE) for r in rows)
        if gs in GATE_DONE or (all_done and active):
            done.append({"stage": sid, "title": flow.STAGE_TITLES[sid], "gate": g, "gate_state": gs or "done"})

    # --- now
    now = None
    if step is not None:
        status = str(row.get("status") or "not_started")
        el = _since(row.get("started_at")) if status in ("running", "waiting") else None
        detail = str(row.get("detail") or "")
        if status == "running" and log_tail:
            detail = _last_detail(log_tail) or detail
        now = {"stage": step.stage, "step": step.id, "title": step.title, "status": status,
               "elapsed_s": round(el) if el is not None else None, "detail": detail[:300]}
    elif gates:
        known = [g for g in GATES if g in gates]           # a partial dict: the first open known gate, else the one after
        cur = next((g for g in known if gates[g]["state"] not in GATE_DONE), None)
        if cur is None and GATES.index(known[-1]) + 1 < len(GATES):
            cur = GATES[GATES.index(known[-1]) + 1]
        if cur:
            sid = flow.GATE_STAGE.get(cur)
            gs = gates.get(cur) or {"state": "not_started", "note": ""}
            now = {"stage": sid, "step": None, "title": flow.STAGE_TITLES.get(sid, cur), "status": gs["state"],
                   "elapsed_s": None, "detail": gs.get("note") or ""}

    # --- next
    nxt: list[dict] = []
    if step is not None:
        ids = [x.id for x in flow.STEPS]
        for x in flow.STEPS[ids.index(step.id) + 1:]:
            if x.optional or (steps.get(x.id) or {}).get("status") in flow.DONE:
                continue
            nxt.append({"step": x.id, "title": x.title, "who": _who(x)})
            if len(nxt) >= 3:
                break
    elif run_status in ("idle", "") and now is not None:
        nxt.append({"step": None, "title": "Run workflow (it starts at the current stage)", "who": "you"})

    # --- the S4 status (review/<p>/tests.json): only while G4 is not approved
    g4_done = (gates.get("G4") or {}).get("state") in GATE_DONE
    tests = {} if g4_done else _read_json(wt / "review" / p / "tests.json")

    # --- issue: a failed step, else the first failing component, else a crashed job
    issue = None
    if step is not None and row.get("status") == "failed":
        probs = [str(x) for x in (row.get("problems") or []) if str(x).strip()]
        detail = str(row.get("detail") or "")
        extra = [x for x in probs if x[:80] not in detail][:2]
        issue = {"title": f"{step.id} {step.title} failed", "detail": "; ".join([detail, *extra]).strip("; ")[:600], "step": step.id}
    if issue is None and tests:
        for c in tests.get("components") or []:
            failing = [f for f in (c.get("failing") or []) if isinstance(f, dict)]
            if failing:
                f = failing[0]
                what = f.get("case") or f.get("example") or ""
                issue = {"title": f"{c.get('kind')} {c.get('name')} — {len(failing)} test(s) fail",
                         "detail": (f"{what}: " if what else "") + str(f.get("detail") or "")[:400], "step": f.get("step")}
                break
            if c.get("ai_issues"):
                issue = {"title": f"{c.get('kind')} {c.get('name')} — the AI step failed", "detail": str(c["ai_issues"][0])[:400],
                         "step": None}
                break
    if issue is None and not running:
        lj = last_job()
        if lj and lj.get("state") == "crashed":
            try:
                tail = jobs.crash_tail(root, lj["id"], n=3)
            except Exception:  # noqa: BLE001
                tail = ""
            issue = {"title": f"job {lj.get('title') or lj.get('action') or lj['id']} crashed",
                     "detail": (tail or "the job's process ended without writing a result").replace("\n", " ⏎ ")[:400], "step": None}

    # --- blocking
    blocking: list[str] = []
    if running:
        try:
            title = jobs.status(root, running).get("title") or running
        except Exception:  # noqa: BLE001
            title = running
        blocking.append(f"a job is running: {title}")
    for g in GATES:
        gs = gates.get(g) or {}
        if gs.get("state") == "waiting":
            blocking.append(f"{g} waits for the approvers" + (f" (PR #{gs['pr']})" if gs.get("pr") else ""))
        elif gs.get("state") in GATE_TROUBLE:
            blocking.append(f"{g} {gs['state'].replace('_', ' ')}" + (f": {gs['note']}" if gs.get("note") else ""))
    if step is not None and step.kind == "gate" and row.get("status") == "failed":
        blocking += [f"not ready: {x}"[:300] for x in (row.get("problems") or [])[:5]]
    if tests:
        if tests.get("build_ok") is False:
            blocking.append("the draft build failed")
        ex = [e for e in (tests.get("examples") or []) if isinstance(e, dict)]
        bad = [e for e in ex if e.get("status") not in ("pass", "shown")]
        if bad:
            blocking.append(f"examples {len(bad)} of {len(ex)} failing")
        nf = sum(1 for c in tests.get("components") or [] if c.get("failing") or c.get("ai_issues"))
        if nf:
            blocking.append(f"{nf} component(s) fail their tests")
    if readiness and studio is not None:
        cur = next((g for g in GATES[:5] if (gates.get(g) or {}).get("state") not in GATE_DONE), None)
        if cur:
            try:
                blocking += [f"{READINESS_PREFIX} {cur}: {x}"[:300] for x in flow._readiness(studio, p, cur)]
            except Exception:  # noqa: BLE001
                pass
    seen: set[str] = set()
    blocking = [b for b in blocking if not (b in seen or seen.add(b))]

    pilot = st.get("pilot") if isinstance(st.get("pilot"), dict) else None
    try:
        notes = memory.notes(root, p)
    except Exception:  # noqa: BLE001
        notes = []
    return {"pipeline": p, "mode": mode, "run_status": run_status, "message": str(st.get("message") or ""),
            "done": done, "now": now, "next": nxt, "issue": issue, "blocking": blocking, "log_tail": log_tail,
            "gates": {g: v["state"] for g, v in gates.items()},
            "pilot": {"phase": pilot.get("phase"), "note": pilot.get("note") or ""} if pilot and pilot.get("phase") else None,
            "memory": notes}


def from_context(ctx, n_log: int = 8) -> dict:
    """The brief for an assistant session (tools.Context): its gates, a fresh state.json (the session may run for
    minutes; ctx.flow is what the job saw when it started), its worktree and mode."""
    return build(None, ctx.pipeline, ctx.gates, None, n_log, root=ctx.root, worktree=ctx.worktree, mode=ctx.mode)


def _pad(label: str) -> str:
    return label.ljust(LABEL)


def text(brief: dict, elapsed: bool = True) -> str:
    """The compact block: a header, then Done / Now / Next / Issue / Blocking (one line each), the pilot and memory
    notes when there are any, and the log tail. `elapsed=False` leaves the running time out (the cause cache's key)."""
    b = brief or {}
    lines = [f"{b.get('pipeline', '?')} · {b.get('mode', '?')} · run: {b.get('run_status', '?')}"]
    done = b.get("done") or []
    lines.append(_pad("Done") + (" · ".join(f"{d['stage']} ✓ {d['gate']}" if d.get("gate") else f"{d['stage']} ✓" for d in done)
                                 or "nothing yet"))
    now = b.get("now")
    if now:
        head = f"{now.get('stage') or '?'} · " + (f"{now['step']} " if now.get("step") else "") + str(now.get("title") or "")
        state = str(now.get("status") or "")
        if elapsed and now.get("elapsed_s") is not None:
            state += f" {elapsed_text(now['elapsed_s'])}"
        lines.append(_pad("Now") + f"{head} — {state}" + (f" · {now['detail']}" if now.get("detail") else ""))
    else:
        lines.append(_pad("Now") + (b.get("message") or "no run"))
    nxt = b.get("next") or []
    lines.append(_pad("Next") + (" → ".join((f"{n['step']} " if n.get("step") else "") + f"{n.get('title')} ({n.get('who')})" for n in nxt)
                                 or "—"))
    issue = b.get("issue")
    lines.append(_pad("Issue") + (f"{issue.get('title')}" + (f": {issue['detail']}" if issue.get("detail") else "")
                                  + (f" · step {issue['step']}" if issue.get("step") else "") if issue else "none"))
    lines.append(_pad("Blocking") + (" · ".join(b.get("blocking") or []) or "nothing"))
    if b.get("pilot"):
        lines.append(_pad("Pilot") + f"{b['pilot'].get('phase')}: {b['pilot'].get('note') or ''}".rstrip(": "))
    for i, n in enumerate(b.get("memory") or []):
        lines.append((_pad("Memory") if i == 0 else " " * LABEL) + n)
    log = b.get("log_tail") or []
    if log:
        for i, ln in enumerate(log):
            lines.append((_pad("Log") if i == 0 else " " * LABEL) + ln)
    else:
        lines.append(_pad("Log") + "—")
    return "\n".join(lines)


_ELAPSED = re.compile(r"\b\d+h\d{2}m\b|\b\d+m\d{2}s\b|\b\d+(?:\.\d+)?s\b")


READINESS_PREFIX = "before Request approval"


def cause_hash(brief: dict) -> str:
    """sha256 of the brief text without elapsed times (a running step's clock and the log's heartbeat times). The mode
    and the opt-in live readiness items are left out too, so the UI's brief (Studio open, readiness=True) and the job's
    (no Studio, git-free) hash alike for the same run state."""
    b = dict(brief or {})
    b["mode"] = ""
    b["blocking"] = [x for x in (b.get("blocking") or []) if not str(x).startswith(READINESS_PREFIX)]
    return hashlib.sha256(_ELAPSED.sub("", text(b, elapsed=False)).encode("utf-8")).hexdigest()


def cause_path(root: Path, pipeline: str) -> Path:
    from .. import flowstore

    return flowstore.flow_dir(root, pipeline) / "assistant" / CAUSE_FILE


def needs_cause(brief: dict) -> bool:
    """A likely cause is only worth a model call when something is wrong or in the way."""
    return bool((brief or {}).get("issue") or (brief or {}).get("blocking"))


def cached_cause(root: Path, pipeline: str, brief_dict: dict) -> dict | None:
    """The stored likely cause when it was computed for this very brief (same hash), else None."""
    doc = _read_json(cause_path(root, pipeline))
    if not doc or doc.get("hash") != cause_hash(brief_dict):
        return None
    return doc


def store_cause(root: Path, pipeline: str, brief_dict: dict, out: dict, usage: dict | None = None) -> dict:
    from .. import flowstore

    try:
        conf = float(out.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    doc = {"hash": cause_hash(brief_dict), "cause": str(out.get("cause") or "").strip(), "next_step": str(out.get("next_step") or "").strip(),
           "confidence": max(0.0, min(1.0, conf)), "at": _now().replace(microsecond=0).isoformat(), "usage": usage or {}}
    flowstore._write_atomic(cause_path(root, pipeline), json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return doc
