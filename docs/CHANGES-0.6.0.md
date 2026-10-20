# package-factory 0.6.0 — what changed from 0.5.0

Additive release: no existing behaviour changed; every 0.5.0 test still passes unchanged.

## New: Workflow Developer tab (docs/STUDIO.md §10)

- The run as a tree of 7 stages / 34 substages (S1 Requirements → G1 … S6 Run local + record PR, 7 Deploy to Airflow),
  exactly the workflow diagram, with grey / pulsing light-green / dark-green / red dots (plus amber = waiting for you,
  grey dash = skipped), collapsible stages, a progress bar per stage, a "now running" strip, the one action card that
  matters, and a console with three tabs (Console · Workflow log · Assistant).
- **Run workflow**: intake (document + scan, what Setup still lacks) → the run drives every substage: Studio jobs,
  gate request → approval → merge (dev: *Approve as me* or the *Auto-approve gates* toggle; SIT+: polls GitHub),
  pauses for open questions (1.4), environment values (3.2), reviews, S6 confirmations and the stage-7 checklist.
  Rejected PRs send the run back to the review substage. Retry / Continue anyway / Skip / Stop / Resume / Forget.
- The run is a cursor + journal in `.pkf-studio/flows/<pipeline>/` (git-ignored, self-ignoring); the gates still come
  from GitHub and git. The journal and the assistant sessions are in the artifacts zip under `workflow/`.

## New: Assistant (docs/STUDIO.md §11)

- A Claude agent (Agent SDK, same key/proxy/Bedrock as the AI steps) briefed with `assistant/knowledge.md`, with
  read access to the framework source, the worktree, job logs and the journal through in-process tools, fenced by a
  PreToolUse hook (reads: allowed roots only; writes: worktree only, never protected files, locks, dags/, .pkf-studio/).
- Protocol: **Diagnose** (root cause, evidence, fix kind: edit_files / studio_action / user_action / rerun / settings)
  → you approve the plan → **Fix** (edits + tests in the hardened runner + diff; nothing committed) → you apply
  (`assistant: <summary>` commit) or discard. **Ask** answers questions read-only. Every step is an `assistant_*`
  event in the workflow log; transcripts stream live to the Assistant console.
- One main agent + two narrow subagents on demand (`log-digger`, `verifier`), per Anthropic's current guidance on
  when multi-agent pays off (context isolation and blackbox verification; not role splitting).

## Files

```
src/pkf_studio/flow.py, flowstore.py                 the run (state machine, journal)
src/pkf_studio/assistant/{knowledge.md,tools.py,session.py,protocol.py}
src/pkf_studio/ui/workflow_dev.py, ui/wfd.py, ui/static/wfd/{index.html,wfd.js,wfd.css}
src/pkf_studio/actions.py                            + assist, build_check (appended)
src/pkf_studio/app.py                                + the tab
src/pkf_studio/bundle.py                             + workflow/flow-events.jsonl, workflow/assistant/
src/pkf_studio/project.py                            + .pkf-studio/flows/ in GITIGNORE
pyproject.toml                                       0.6.0; package-data for the static component
tests/test_workflow_run.py, test_workflow_dev_ui.py, test_assistant.py, test_wfd_component.py
docs/STUDIO.md §10–11, docs/WORKFLOW_DEVELOPER_PLAN.md, README.md
```

## Install / upgrade

```bash
cd ~/pkf/package-factory && pip install -e '.[runtime,agent,studio]'     # same as before
pkf studio ~/pkf/specs                                                   # the new tab is the last one
```

Repositories set up with 0.5.0 need nothing: the flows folder ignores itself. A new team PR (Setup) also adds
`.pkf-studio/flows/` to `.gitignore`.

## Known limits

- Stage 7 is a checklist with the commands from `deploy.md`: the Studio has no Airflow connection (yet).
- The run advances while the tab is open (it is driven by the page, like the live job log); jobs themselves keep
  running regardless, and the run resumes from them when the tab is reopened.
- The assistant's live behaviour against the model was not exercised in this build (no key here): the session, the
  tool fence and the protocol are unit-tested with a scripted session; the first real runs should be watched in the
  Assistant console.
