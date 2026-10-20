# pkf Studio 0.6.0 — Workflow Developer tab and Assistant agent (plan and contracts)

Approved 2026-10-18. Additive to 0.5.0: nothing existing changes behaviour; the existing Workflow tab stays.

## 1. Modules

```
src/pkf_studio/
  flow.py                 the run: STEPS table (S1…S6 + 7, 34 substages), tick(), pauses, gates, failures, events
  flowstore.py            .pkf-studio/flows/<p>/state.json + events.jsonl (git-ignored; a cursor + journal, disposable)
  assistant/
    __init__.py
    knowledge.md          what the agent knows before it reads anything (architecture, stages, failure→fix tables)
    tools.py              in-process MCP tools: pkf_context, read_job_log, read_file, list_files, grep, run_tests, …
    session.py            Agent SDK session: tools, hooks (path guard), subagents (log-digger, verifier), transcript
    protocol.py           diagnose → (user approves) → prepare_fix → (user approves) → apply; structured outputs
  actions.py              + assist(phase, …), build_check  (appended to ACTIONS)
  ui/workflow_dev.py      the tab
  ui/wfd.py               python wrapper of the static component
  ui/static/wfd/          index.html, wfd.js, wfd.css  (plain JS, no build step; shipped in the wheel)
```

## 2. Contract: snapshot (Python → component)

```json
{
  "run": {"status": "idle|running|waiting_input|waiting_gate|waiting_user|failed|done|stopped",
          "current": "4.1", "message": "S4 Code & build · 4.1 Start S4: code, test, build — running (2m10s)",
          "started_at": "…iso…", "updated_at": "…iso…", "mode": "dev"},
  "stages": [
    {"id": "S1", "title": "S1 Requirements → G1", "status": "not_started|running|waiting|done|failed|skipped",
     "progress": [3, 8],
     "steps": [
       {"id": "1.1", "title": "New pipeline name", "kind": "input|job|gate|review|auto|checklist|action",
        "status": "not_started|running|waiting|done|failed|skipped", "detail": "one line (optional)",
        "started_at": "…", "ended_at": "…", "job_id": "20261018T…-abc123 (optional)", "optional": false}
     ]}
  ],
  "selected": "4.1"
}
```

Status colours (CSS classes = status names): `not_started` grey (disabled look), `running` light-green pulsing dot,
`waiting` amber pulsing dot (waits for the user: approval, input, confirmation), `done` dark green, `failed` red,
`skipped` grey with a dash. Stage status = running if any step running; waiting if any waiting; failed if any failed;
done if all done/skipped; else not_started.

## 3. Contract: component API (ui/wfd.py)

```python
tree(snapshot: dict, *, key: str, height: int | None = None) -> str | None   # the step id the user clicked, else None
console(lines: list[dict], *, key: str, title: str = "", height: int = 360, follow: bool = True) -> None
# line = {"ts": "HH:MM:SS", "level": "info|warn|error|ok|agent|tool|user", "source": "job|flow|assistant", "text": "…"}
```

- The component keeps its own expand/collapse state across reruns (args update in place; no remount).
- Stages open by default when running/waiting/failed; done and not_started collapsed; the user's toggles win.
- Theme: read `theme` from the Streamlit render event (base, primaryColor, backgroundColor, secondaryBackgroundColor,
  textColor, font); work in light and dark.
- `tree` returns the clicked step id (via setComponentValue) and clears nothing; the Python side decides.
- `console`: monospace, auto-scroll when `follow` and the user is at the bottom; a level filter and a copy button
  inside the component; render incrementally (append only new lines when the prefix is unchanged).

## 4. Contract: flowstore

```python
load(root, p) -> dict | None            # state.json
save(root, p, state) -> None            # atomic write
event(root, p, type: str, **data) -> dict     # appends {"ts", "type", **data} to events.jsonl; returns the row
events(root, p, n: int = 500, types: set[str] | None = None) -> list[dict]
assistant_dir(root, p, session_id) -> Path    # .pkf-studio/flows/<p>/assistant/<session>/
```

Event types: `run_started`, `run_stopped`, `run_done`, `step_started`, `step_done`, `step_failed`, `step_skipped`,
`input_collected` (step, keys — values only when not secret), `gate_requested`, `gate_approved`, `gate_merged`,
`gate_rejected`, `checklist_done`, `assistant_started`, `assistant_diagnosis`, `assistant_fix_proposed`,
`assistant_fix_applied`, `assistant_fix_discarded`, `assistant_failed`, `assistant_answer`.

## 5. Contract: flow.py

```python
STEPS: list[Step]      # Step(id, stage, title, kind, action=None, optional=False, help="")
start(s, p, answers: dict) -> dict        # intake answers; creates the run; returns state
tick(s, p, states) -> dict                # advance: finished job → record; next job → start; gate → wait; returns state
answer(s, p, step_id, values: dict) -> dict   # a pause's form was submitted
retry(s, p) -> dict; skip(s, p) -> dict; stop(s, p) -> dict; resume(s, p) -> dict
mark_done(s, p, step_id, item: str | None = None) -> dict    # checklists (6.2, 7.3, 7.4)
snapshot(s, p, states, state) -> dict     # §2
```

The run never owns pipeline truth: `states = stages.derive()` decides which gate is current; `state.json` only remembers
the cursor, answers and timings. If state.json is missing, `snapshot()` shows derived progress read-only and Run workflow
starts from the current gate.

## 6. Contract: assistant

Job action `assist(ctx, phase, session_id=None, question=None, step_id=None)`; phases: `diagnose`, `fix`, `ask`.
Per session: `.pkf-studio/flows/<p>/assistant/<session>/{transcript.jsonl, diagnosis.json, fix.json, summary.json}`.
`transcript.jsonl` rows: `{"ts", "kind": "text|thinking|tool_use|tool_result|result|error|subagent", "name", "text"}`
(scrubbed with `pkf.llm.base.redact_secrets`), written as the agent works — the console shows it live.

Structured outputs:
```json
diagnosis.json: {"root_cause": "…", "evidence": ["file:line or log excerpt", …], "fix_kind": "edit_files|studio_action|user_action|rerun|settings|none",
                 "proposal": "what the fix will do", "files": ["relative paths it will touch"], "studio_action": "Reopen G3 | Restore dags/ from main | Regenerate | Render & plan again | …",
                 "user_action": "exact command or step", "needs_reopen": "G3|null", "risk": "low|medium|high", "confidence": 0.0}
fix.json:       {"summary": "…", "files_changed": ["…"], "diff": "unified diff (truncated 20k)", "tests": {"ran": true, "ok": true, "detail": "…"}, "notes": "…"}
```

Tool/permission rules (enforced by a PreToolUse hook, not only by prompt):
- diagnose/ask: read-only — Read, Grep, Glob + MCP tools; paths limited to the framework source (installed package
  folder), the pipeline worktree, `.pkf-studio/jobs/`, `.pkf-studio/flows/<p>/`, and the base branch content via tools.
- fix: additionally Edit/Write/MultiEdit limited to the pipeline worktree, excluding PROTECTED_PATHS, the ledger,
  `dags/`, `.pkf-studio/`; `run_tests` tool runs `pkf test`/`pkf validate` in the hardened runner; no Bash.
- No AWS or GitHub credentials in the job (ANTHROPIC_API_KEY only, like the other AI roles; Bedrock passthrough honoured).
- Nothing is committed by the agent. Apply = the Studio commits (`assistant: <summary>`) after the user clicks Apply.
- Subagents: `log-digger` (read-only; long logs → relevant excerpt) and `verifier` (fix phase; runs tests, reports).
- Limits: PKF_LLM_MAX_TURNS_ASSISTANT_DIAGNOSE (30), _FIX (50), _ASK (15); budgets 2.0 / 4.0 / 1.0 USD; model
  PKF_LLM_MODEL_ASSISTANT (default claude-sonnet-5-5). Session resume for follow-ups.
