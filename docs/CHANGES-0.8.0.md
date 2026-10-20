# package-factory 0.8.0 — what changed from 0.7.0

Two things: the **ST-3 v3 F** UI for pkf Studio, and a stronger **assistant** (Claude Opus 5.5, a structured brief of the
run, a map of the framework's code, its own read-only lane, pipeline notes). The run engine, GitHub, git, the stage jobs
and the framework (`pkf`, `pkf_runtime`) are unchanged. Every moved button calls the same function with the same
arguments as in 0.7.0; the new buttons call existing functions. The assistant still can never change the framework's
code — and its write guard is stricter than before (see *The assistant*).

## The UI (ST-3 v3 F)

- **Light theme** (blue accent; `pkf studio` starts with `--theme.base light`). The console stays dark.
- **Sidebar** (new): brand with version · repository · base · mode; the pages **Pipelines · Workspace · Artifacts ·
  Setup**; the **Pipeline** selector (same widget key, ➕ New pipeline); **ALL PIPELINES** with a stage chip each
  (`S4`, `S4 failed`, `G4 waiting`, `done`; click to open); **RUNNING JOBS** of every pipeline (click to open; elapsed);
  the **ASSISTANT** card (**Acts on its own** = 0.6.1's *Assistant can act*, same state and event; a status line with a
  red dot when the assistant waits for you; **Open** and **⤢**); you at the foot.

  | 0.7.0 | 0.8.0 |
  |---|---|
  | top navigation | sidebar page links |
  | pipeline selector in the context bar | sidebar (same key `pipe-pick`) |
  | ▶ n running elsewhere | sidebar RUNNING JOBS (same `goto-job-<p>` buttons) |
  | your chip → ⋮ | sidebar foot |
  | Assistant can act (assistant band) | sidebar **Acts on its own** / full screen header |
  | run chip | the stepper and the running substage card |

- **Top bar**: `Workspace / <pipeline>`, the mode chip (`DEV · auto-approve on/off`), the source-control chip (unchanged:
  Commit / Discard, Take / Merge / Overwrite, PR, VS Code), **Refresh**, **Stop run** / **▶ Resume**, **Run ▾**.

### The Workspace

| 0.7.0 | 0.8.0 |
|---|---|
| stage tree (left) | **stepper** — 7 stages, progress, gate, what runs; **click a stage to browse it** (the page follows the run again when the run moves to another stage) |
| *Workflow 17 / 34* | stage header eyebrow `STAGE 4 OF 7 · 17 / 34 SUBSTAGES DONE`, the stage's title and description |
| ↩ Go back (above the tree) | ↩ Go back · run a stage · reopen (stage header; same navigator) |
| click a step in the tree | click a substage card's title (console + Go back pre-selection) |
| Now card + Cancel | the running substage card (time, last detail, **Cancel**; none for a local run) |
| the step's card | **Your turn** (same cards; only while the run's stage is shown) |
| Needs your attention | **Before Request approval G\<n\>** — ✓ what passed (G4: draft build, examples, dags/, no job running) and ○ what is open, same rows and buttons |
| After this · Request approval | Request approval in the stage header (same P1 rules) + *n things still needed* |
| stage summary | **Components** (S4: every component, Regenerate (AI), Run tests & build again; S1/S2/S3/S5/S6/S7: their own panel) |
| Artifacts of this stage | **Files of this stage** (a row that scrolls sideways; Open →) |
| assistant band | sidebar **Open** (pop-up) / **⤢** (full screen) |
| console band (tabs) | the dark **console**: step selector, **find in log**, level chips, Copy, Follow, **line numbers**, **⬇ Log**; views Console · Workflow log · Assistant transcript · Approval history · Jobs |

The substage row has a visible horizontal scroll bar (stages have up to 9 substages). The page refreshes every 2 s while
the run is active, as before.

### The assistant pop-up and full screen

Both show: **Where things stand** (Done / Now / Next / Issue / Blocking / On its own, the last log lines, the pipeline
notes), the **Likely cause** with the next step, the conversation (*In short*, root cause, proposal, evidence, the fix
and its diff), the decisions (Approve the fix plan / Dismiss, Apply / Discard, Do it) and Ask / Diagnose — the same
keys and functions as 0.7.0. The pop-up opens over any page and stays open across refreshes until X; ⤢ hides the
sidebar, **Exit full screen** brings it back.

## The assistant

- **Model**: Claude Opus 5.5 (`claude-opus-5-5`) for every phase, with Sonnet 5.5 as the fallback model; effort per
  phase: brief low, ask medium, diagnose high, fix high (`PKF_LLM_EFFORT_ASSISTANT[_<PHASE>]`,
  `PKF_LLM_FALLBACK_MODEL_ASSISTANT`, empty disables the fallback). The `log-digger` helper runs on Sonnet 5.5
  (`PKF_LLM_MODEL_ASSISTANT_LOG_DIGGER`); the `verifier` on the main model.
- **Limits**: diagnose 30 turns / $4 / 900 s, fix 50 / $8 / 1500 s, ask 15 / $2 / 600 s, brief 4 / $0.50 / 120 s.
- **What it has in its prompt**: the expanded architecture briefing (`assistant/knowledge.md`), a **code map** of the
  framework (every module, class and function with its first docstring line, generated from the source, cached at
  `.pkf-studio/cache/codemap-<version>-<hash>.md`, regenerated when the source changes) and the **run brief**. The whole
  codebase (~470k tokens) is not put in the prompt: the map plus on-demand reads cost about $0.25 per 20-turn session
  against about $4.40. New read-only tools: **framework_map(query)** (search the map) and **workflow_state** (a fresh
  brief mid-session).
- **Prompt layout (bug fix)**: the system prompt is the same for every phase (stable, cacheable); the phase rules and
  the brief are in the user message. Before, a *fix* that resumed a *diagnose* session kept the read-only diagnose rules
  (the CLI resends a resumed session's system prompt).
- **Run brief** (`assistant/brief.py`, no model call): Done / Now / Next / Issue / Blocking / Log, the gates, what the
  assistant does on its own, the notes. It never raises (offline, no worktree, no run).
- **Likely cause**: phase `brief` — no tools, a JSON answer {cause, next_step, confidence}, cached per brief at
  `.pkf-studio/flows/<p>/assistant/brief-cause.json`. The UI starts it by itself only when there is an issue, at most
  once per situation and every 10 minutes.
- **Pipeline notes**: tool **remember(note)** writes `.pkf-studio/flows/<p>/assistant/memory.md` (one timestamped line
  per note, ≤ 300 characters, about 4 KB kept); the notes are part of every brief. Never secrets. The notes and the
  cached likely cause are in the artifacts zip.
- **The assistant lane**: Ask, Diagnose and the likely cause run in their own lane (`.pkf-studio/locks/assistant/<p>.lock`,
  one lane job per pipeline), so you can ask while a pipeline job runs. Lane jobs have no test runner and no git-changing
  tool, are refused every Studio mutation, and are not counted as the pipeline's job (busy, the run's tick, the pilot,
  Cancel of the pipeline job and the Jobs view are as before). *Fix* stays on the pipeline's lock.
- **Write guard, stricter**: before the spec is committed only `requirements/<p>/`, `pipelines/<p>.yaml` and `review/<p>/`
  are writable; file entries match the whole path (`pipelines/p.yaml` no longer allows `pipelines/p.yaml.bak`); pipeline
  names and protected paths compare case-insensitively; `requirements/<p>/extracted.txt` (the uploaded document's text)
  is refused. Framework files, other pipelines' files, shared components, lock files, `dags/` and
  `.pkf-studio/` stay refused in every phase.

## Tests

- `tests/test_workspace_ui.py`: the 0.7.0 assertions, moved to the new places (Workspace = the stepper is on the page;
  the assistant tests open the pop-up; the console views key `ws-cview-<p>`; the rail card pre-fills Go back; full screen
  replaces Expand).
- `tests/test_workflow_dev_ui.py`: one test opens the pop-up (the pilot box moved there).
- New `tests/test_workspace_v08_ui.py` (the stepper, every stage's panel, Components, Before Request approval, the
  console's find / line numbers / views / download, the sidebar, Acts on its own, the pop-up on every page, full screen,
  the lane routing, the likely cause) and `tests/test_assistant_v08.py` (50 tests: the assistant core and the lane).
- `tests/test_assistant.py`: the fixture commits the pipeline's spec (before a spec exists only the requirement and
  review files are writable now); a new component file is expected to be refused; the discard test writes a review
  file.

## 0.8.1 (fix)

- **The assistant failed on Windows with "CLINotFoundError: Claude Code not found at …\claude.exe".** 0.8.0's system
  prompt (knowledge.md + the code map, ~88k characters) went to `claude.exe` on the command line, over Windows' 32,767
  character limit, so the process could not start. The system prompt now goes as a file (`--system-prompt-file`,
  `.pkf-studio/cache/assistant-system-<hash>.md`); a test keeps the command line under 30,000 characters. When
  `claude.exe` exists but cannot be started, the error now says so (antivirus / application control / command line).

## 0.8.2 — Run without stopping, the 6.3 test package, the assistant in its own tab

- **Run without stopping (dev only).** An intake switch and a Run ▾ toggle. On = auto-approve gates + the assistant
  acting on its own + `autorun.py`, deterministic Studio code that makes the clicks a person would make while the run
  waits, one per tick, each a `full_auto` event in the workflow log:
  - Continue at 1.4 (only with no open questions / ambiguities), 2.2, 3.4, 4.2, 5.3 (only when 5.1 had no problems);
    5.2 Render & plan again once;
  - 4.3: Regenerate each failing component (2 tries), then Run tests & build again (2 tries);
  - 6.1 own AWS credentials and 6.2 "no old DAG" from the intake's pre-answers (ticked by default, editable);
  - 6.3 **Move** when every planned source key is one it placed itself in the same bucket — or anything, when *Move
    files the run did not place itself* is ticked (off by default); a failed local run → Preview again once;
  - 6.5 Run again (a preview with files there stops: not a no-op); 7.2 done.

  It stops — "Run without stopping has stopped: …" at the top of *Before Request approval*, in the run message and in
  the assistant's brief — for 3.2 values still `<REQUIRED>`, open questions, a component still failing, files it did not
  place, a test package it could not place, and 7.3 / 7.4 (the Airflow deploy is by hand). At most 80 decisions per run.
- **Test package at 6.3** (`testdata.py`, job `stage_test_data`): when the preview finds nothing, the Studio takes the
  first end-to-end example of `pipelines/<p>.yaml` a default run would find (a listing, no `expect_error`, no special
  params, no templates), writes it under `.pkf-studio/flows/<p>/test-data/<stamp>/` with `upload.sh`, and puts it in
  the source's bucket with your AWS credentials: never overwriting (`IfNoneMatch`), tagged `pkf-test-data=1`, refused
  outside dev, for an SQS trigger, or when the bucket notifies targets this pipeline does not own. Then it waits out the
  source's minimum file age (at most 30 min) and previews again (at most 3 times). When it cannot place the package,
  6.3 shows the commands and **Download upload.sh** (each command checks the key first and keeps an existing object).
  6.3 with nothing to move also has a **Prepare a test package** button.
- **One tab drives the run** (`drive.py`, an 8-second lease in `.pkf-studio/locks/drive/<p>.json`): two open tabs no
  longer advance the same run at once; a tab in the background (slowed timers) hands over to a visible one.
- **The assistant in its own browser tab**: ⤢ (sidebar) and ⤢ Full screen (pop-up) open `/assistant?p=<pipeline>`:
  no sidebar; Acts on its own, **Open the Workspace**, **Close tab**; the assistant full width at ~75 % of the window
  with its own scroll bar, Ask and Decisions below at ~25 %.
- **Switches follow the run**: *Acts on its own*, *Auto-approve gates* and *Run without stopping* always show the run's
  state and change it only on a click (before, a switch drawn earlier could undo a change made by another tab).
- The mode chip shows `DEV · run without stopping`. The assistant's briefing covers Full auto and the test package.
