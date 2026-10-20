# package-factory 0.7.0 — what changed from 0.6.1

A new UI for pkf Studio (the ST-2 v3 design), nothing else. The run engine, GitHub, git, jobs, the assistant's logic and
the framework are unchanged: every moved button calls the same function with the same arguments as in 0.6.1; the new
buttons call existing functions (Regenerate in *Needs your attention* starts the same per-step job as the Workflow tab's
S4 panel, or goes through the run at 4.3 as the 4.3 card does). Every 0.6.1 test passes; four places in three test files
changed only *where* they look (see Tests), and `tests/test_workspace_ui.py` is new.

## Pages and the context bar

- **Top navigation** with four pages: **Workspace** (the 0.6.1 *Workflow Developer* tab, re-laid out — the app opens
  here), **Pipelines** (every pipeline, then the seven 0.6.1 tabs unchanged), **Artifacts** (new) and **Setup**
  (unchanged; it always opens, even when a startup check fails, as in 0.6.1). The sidebar is gone; everything it held
  moved:

  | 0.6.1 sidebar | 0.7.0 |
  |---|---|
  | pipeline selector, New pipeline | context bar (same widget key) |
  | Refresh from GitHub | context bar **Refresh** |
  | running jobs of every pipeline | **▶ n running elsewhere** (the other pipelines; click opens one in the Workspace) |
  | version, repository, mode, You, warnings, local-state note | your chip → ⋮ |
  | Pipelines / Setup | top navigation |

- **Context bar**: the mode, the run chip (`● S4 · 4.1 running · 2m10s`, the run's message on hover), the
  **source-control chip** (`repo · branch · ↑ahead · clean / n changed · PR #n open`; inside: Commit / Discard, GitHub's
  copy changed → Take / Merge / Overwrite, the PR, the worktree in VS Code), **Stop run** / **▶ Resume**, **Run ▾**
  (Auto-approve gates in dev, Forget this run).

## The Workspace

Three bands sized to the window below the context bar — **65 % work area · 15 % assistant · 20 % console** (measured:
64.5 / 14.8 / 20.7 % at 1920×1080, 63.6 / 15.8 / 20.6 % at 1440×900; minimum heights keep small screens usable, the
page then scrolls). Each band scrolls inside.

- **Left — Workflow** `17 / 34` and a progress bar: the stage tree (the 0.6.1 component, filling its band) and **↩ Go back** (the 0.6.1
  navigator: run from a step, stop after a stage, reopen a stage — a step clicked in the tree is pre-selected).
- **Centre**:
  - the **Now card**: the running job, whichever started it (a step, a button on another page, the assistant), with its
    last log line, elapsed time and **Cancel** (no Cancel for a local run that may be applying Terraform, as before).
    When the job ends the page reloads the stage data, as the job panel does on the other pages.
  - the **step's own card** exactly as in 0.6.1 (failed card, gate wait with Approve as me, reviews, inputs,
    checklists, download, preview / Move) — or the intake before the run.
  - **Needs your attention · n** (P2): a component failing its tests (**Regenerate \<step\>**; at 4.3 through the run, as
    the 4.3 button — only while the run waits there; a stopped run stays stopped), the gate's unmet checklist items with their fix, files outside the pipeline (Remove), a running job
    (wait), a crashed or problem job (the 0.6.1 banner), the run or the assistant not able to advance, the assistant
    needing you, uncommitted changes and a diverged branch (pointing to the source-control chip), GitHub unreachable, a
    broken worktree (Repair).
  - **After this** (P1): the next gate in words and **Request approval** — hidden while the run is on the gate step (the
    run opened the PR; its link is shown), disabled while a job runs or an item is open; "Update the PR" when the PR is
    already open.
  - the **stage summary** expander (S4: components ok, draft build, examples; S5: Terraform, policy, dry run; S3: plans).
- **Right — Artifacts of this stage**: the stage's files with a status line ("tests fail · 112 lines") and **Open →**
  (the Artifacts page with that file).
- **Assistant band** (full width): Ask (Enter submits), **Diagnose \<step\>**, **Expand** (P4: assistant 45 %, work 35 %;
  shows the full diagnosis and the diff in place), **Assistant can act** (P10). Left: the question and the answer — *In short*,
  root cause, proposal, evidence; right: the decision — Approve the fix plan / Dismiss, Apply the fix / Discard, the
  suggested actions with Do it, what the assistant does on its own. Same keys and functions as 0.6.1.
- **Console band**: Console · \<step\>, Workflow log, Assistant transcript, **Approval history** and **Jobs** (the job
  history of the old job panel). Only the open tab is read.

## The Artifacts page (new)

The pipeline's files grouped by stage (01 Requirements … 07 Runs), filterable; the chosen file full height with Code
(line numbers, VS Code, download), Plan cards, Tests, **Diff vs \<base\>** (P6: the file as it stands — uncommitted edits
included — against the base branch, e.g. *Diff vs main*), Capabilities, Data-flow diagram (.dot download) and Requirement; Regenerate (AI)
for a component's code (the Workflow tab's job; through the run while it waits at 4.3); Download artifacts (zip); the
job panel on top, as on the Pipelines page.

## Look

Dark theme with a teal accent by default (P8); ⋮ → Settings switches to light or the system theme (the launch flag is
now `--client.toolbarMode viewer`, which keeps that menu and still hides Deploy). The theme lives in
`src/pkf_studio/ui/theme.py`; if a future Streamlit renames its internal CSS classes the app still works, the bands
then size to their content.

## Not like the picture, and why

- Your name and role sit at the right of the context bar: Streamlit's top bar takes no widgets.
- The Ask button is disabled while a job runs for the pipeline (as in 0.6.1): the assistant runs as a job, and a
  pipeline runs one job at a time.

## Review

The finished code was reviewed by an agent that had not seen it being written. It found: the context bar could keep
showing *Stop run* after the run failed or moved on (the bar is drawn before the run ticks — the page now reruns once
when the status changed), Regenerate could resume a stopped run, the tree click did not pre-fill ↩ Go back, the
Artifacts page lacked the job panel and bypassed the run at 4.3, about 20 git calls per click in the context bar (now
read once and only shown cards read git), the Pipelines tab not remembered across pages, a double-escaped detail line,
"Diff vs main" with another base branch, and test gaps. All are fixed, and each has a test.

## Tests

- New: `tests/test_workspace_ui.py` — the pages and routing (and the 0.6.1 `page` / `tab` keys), Setup reachable when a
  check fails, the attention list and Regenerate (both routes), the Now card's Cancel, P1, artifacts of the stage, the
  assistant band (Ask, Approve the fix plan), the console tabs, the source-control chip with no key clash against the
  Workflow tab, Stop / Resume, the Artifacts page (grouping, Diff vs main, Open →), the end-of-job reload.
- Adjusted (where they look, not what they check):
  - `test_studio_ui.py::_open_pipelines` and `test_studio_app.py::test_tabs_render_and_g1_request` open the Pipelines
    page explicitly (the app now opens on the Workspace);
  - `test_studio_ui.py::test_no_cancel_for_run_local` finds the running job in **▶ running elsewhere** (was the sidebar);
  - `test_studio_ui.py::test_tabs_render_when_github_unreachable` clicks **Refresh** in the context bar (was the sidebar).

## Files

- `src/pkf_studio/app.py` — the four pages, the shared page top.
- `src/pkf_studio/ui/shell.py` (new) — routing, the startup-check guard, the context bar.
- `src/pkf_studio/ui/workspace.py` (new) — Now card, attention, After this, stage summary, artifacts of the stage,
  assistant band, console band.
- `src/pkf_studio/ui/pipelines_page.py`, `ui/artifacts.py`, `ui/theme.py`, `ui/static/pkf-logo.svg` (new).
- `src/pkf_studio/ui/workflow_dev.py` — the Workspace layout; the assistant panel split into conversation and decisions;
  the navigator body (Reopen in place inside Go back). The 0.6.1 strip, navigator expander and one-column assistant
  panel are replaced by the context bar, ↩ Go back and the band.
- `src/pkf_studio/ui/workflow.py` — `approval_history` extracted; `key_prefix` on the dirty bar and the diverged card.
- `src/pkf_studio/ui/common.py` — `job_history` extracted.
- `src/pkf_studio/ui/wfd.py`, `ui/static/wfd/*` — an opt-in `fill` mode (the iframe sized by the page).
- `src/pkf_studio/cli.py` — dark theme, toolbar mode `viewer`.
