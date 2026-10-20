# package-factory 0.6.1 — what changed from 0.6.0

Additive release: nothing that worked in 0.6.0 behaves differently, except the bugs below. Every 0.6.0 test still
passes unchanged; 0.6.1 adds tests for every fix and feature (`tests/test_flow_gates.py`, `tests/test_isolation.py`,
`tests/test_pilot.py`, and new cases in `tests/test_workflow_dev_ui.py`).

## Fixed: the Workflow Developer stuck at S5 5.4 (and every gate like it)

Three separate causes, each reproduced by a test that failed on 0.6.0:

| cause | what you saw | 0.6.1 |
|---|---|---|
| The G5 PR is **merged**, but the gate is not *approved*: **stale** (most often another pipeline changed a shared bucket stack on main), merged without approval, reopened elsewhere, or blocked by an earlier gate | 5.4 said "waiting for the approvers" for ever; *Approve as me* commented on the merged PR; Retry tried to merge a merged PR | first the base branch is fetched and the gate derived again (a PR merged by hand on GitHub is *approved* once the merge is fetched → the run moves on); only then, if it is really not approved, the step turns red with the reason and the fix — **Reopen G5** (button), the earlier gate to reopen, or *Run from …*. GitHub trouble ("unknown") keeps waiting and says why; a gate waiting on an earlier gate requests its PR as soon as that one clears |
| The **merge call timed out after GitHub merged** (the UI's GitHub client has short timeouts) | the next tick merged again → "has no open pull request" → red; Retry repeated it | a failed merge call first asks GitHub whether the PR is merged; if it is, the step is done. A merge that keeps failing for network reasons turns red after 10 tries instead of retrying silently |
| **5.2 Render & plan again** and **4.3 Regenerate / Run tests & build again** start a job from a *review* step, and the run only finished *job* steps | the step stayed "running" for ever (the tab re-ran every 2 s) | the run finishes these jobs: 5.2 → done and on to 5.3; 4.3 looks at the components again; a failed job is red with Retry / Continue anyway |

Also fixed:
- **Auto-approve** posted once per gate; a later commit (approvals reset) left the run waiting for ever. It now posts
  again for each new head (at most 5 per gate).
- **SIT/PPT/PRD**: a *pending* `pkf-gate` check (it runs tests and the build — minutes at G4/G5) failed the step
  ("merge refused"). The step now waits for the check and merges when it is green (a *missing* check is waited for
  about a minute, then reported as before); a *failed* check is red with the reason, and **Retry** reads the new head
  after you push a fix.
- A gate that is **not ready** (e.g. "the dry run failed") at 5.4 had no way back but Retry, which failed again. The
  step now names the step that fixes it (*Run from 5.2*, *Run from 3.2*, …) with a button.
- G1's three gate steps (1.6 → 1.7 → 1.8) and every gate right after *Request approval* no longer wait up to 15 s for
  the next GitHub refresh before polling the PR the run just opened.

## Fixed: one pipeline's build affecting another

Each pipeline already had its own worktree and branches; these shared paths still coupled them:

- **`pkf validate` checked every spec on main** at S3 (3.3) and S4 (4.1): another pipeline half-way through its gates
  in another session failed yours. Now only this pipeline's own files (its specs, its components — reused ones
  included — its connection) decide; other pipelines' errors are logged.
- **7.1 `pkf build --all --check`** failed when any other pipeline on main could not build yet. Now: this pipeline's
  DAG decides; pipelines still on their way through the gates are logged; a *deployed* pipeline with an out-of-date
  DAG is a warning (*Continue anyway*), since `dags/` is copied as a whole.
- **Shared S3 notification stacks** (`infra/buckets/<bucket>/`):
  - G5 staleness is now judged on **this pipeline's own part** of the stack (its queue block + everything that is not
    another pipeline's block). Another pipeline's G5 on the same bucket no longer makes yours stale. Your own entry,
    your infra files and your DAG are covered exactly as before. The ledger rows and `pkf studio verify` keep the
    0.6.0 input hash unchanged (existing approvals stay valid, the pkf-gate check is unaffected).
  - **Render & plan** (5.1 / 5.2) first merges the base branch into the stage branch when another pipeline changed a
    stack you use, so your stack is rendered with everyone's current entries and your G5 PR neither conflicts with nor
    reverts theirs. A conflict inside `infra/buckets/` takes main's version (the stack is rendered again right after);
    any other conflict aborts cleanly with the file names.
  - Before *Request approval G5*, the run checks whether main's stack changed since you rendered it, and if so asks you
    to *Run from 5.2*.
- The repository-wide **git lock** (all pipelines' worktrees share one `.git`) waits up to **10 minutes in a job** (was
  2), so a slow fetch or push of one pipeline behind a proxy no longer fails another pipeline's job; buttons in the app
  keep the 2-minute wait (`PKF_GIT_LOCK_TIMEOUT_S` overrides both).

Unchanged on purpose: specs still live in the shared folders (`pipelines/`, `sources/`, …) — that is the framework's
repository layout. Another pipeline's work only becomes visible to yours once it is merged to main.

## New: go back, run a single stage, reopen — in the Workflow Developer

- **↩ Go back · run a single stage · reopen a stage** (under the status strip): pick any step, optionally *stop after*
  a stage, *Run from*. The steps from there on run again (checklists are ticked again); earlier ones keep what they did.
  A jump forward over a person's step that is not done shows a warning.
- The approvals form a chain (each stage builds on the approved stage before it), so a jump past a gate that is not
  approved is refused **with the step to run first** (one click). Running a stage whose gate is already approved
  reopens that gate first, after showing what it sets aside.
- **Run only S3, then resume at S5** works when S3 changed nothing a later approval depends on. If S3 changed a plan,
  G4 becomes stale and the app says so and offers *Run from 4.1* — the code must be rebuilt and approved on the new plan.
- **Reopen a stage** from the panel or from a failed gate (two clicks: what is set aside, then Confirm). The run
  continues from the stage's first step; a stage whose work is already on the branch skips its Start job (S4 runs its
  tests and draft build again, S5 renders and plans again).

## New: the assistant can act — reviews stay with you

- **Assistant can act** (intake and status-strip toggle; **on by default in dev mode, off in SIT/PPT/PRD**; runs saved by
  0.6.0 keep it off until you switch it on): when a step fails, the assistant diagnoses
  it and then does what the diagnosis lists, one action at a time — retry, run from a step, reopen G2–G5, render &
  plan again, run tests & build again, regenerate a component, restore `dags/`, or a **file fix**: it edits, runs the
  tests, then the **Studio runs the tests itself** and applies the fix only when both pass and the worktree holds
  nothing but the fix's changes. When a fix needs a stage reopened first, the reopened stage waits for the fix before
  its jobs run. At most 3 tries per step and run, then it hands over. Every action is in the workflow log
  (`by: assistant`).
- **People keep**: every review (1.4, 2.2, 3.4, 4.2, 5.3), every approval and merge decision, environment values (3.2),
  credentials (6.1), the real runs and Moves (6.3, 6.5 — Terraform apply and real data), the checklists (6.2, 7.3, 7.4),
  all of **S1 Requirements**, and any fix that changes the requirement. The assistant never *jumps over* a person's step
  that is not done, and reopens a stage only when there is one to reopen and the run can continue from it. The Studio
  enforces this (`flow.assistant_may`), whatever the model asks.
- With the toggle off, the diagnosis shows its actions with a **Do it** button each.
- **The assistant never changes framework code**: writes under the framework source or any installed `pkf`,
  `pkf_runtime`, `pkf_studio` package (a framework copy inside the repository included), framework folders and
  packaging files are refused in every phase, on case-insensitive file systems too. It also only changes **this
  pipeline's own files**, as committed (its own uncommitted edits cannot widen that), and never a file of another
  pipeline — not even a component or connection the two share.
- **Plain language first**: every diagnosis, fix and answer starts with a 2–3 sentence summary for a business analyst
  (shown as *In short*), with the engineer's detail folded underneath.

## Faster

- `stages.derive()` memoises each gate's input hash per commit (content at a commit never changes): about half the
  time per call on a small repository, more at later stages and on Windows. derive runs on every refresh and every few
  seconds while a gate waits.
- Gates poll the PR right after opening it (no up-to-15-second pause per gate, ×5 gates, ×3 for G1's steps).
- Considered and **not** done: planning components in parallel (a destination's plan depends on the variables the
  source's plan provides — parallel plans would change plan quality); generating code for several components in
  parallel (helps only with 2+ AI-coded components; adds concurrency to the job log and coarser checkpoints).

## Review

The change was reviewed by an independent agent that had not seen the work, against the code. Its findings — two new
stuck/false-red gate states, the assistant being able to skip a review via *Run from*, re-run checklists keeping their
ticks, the assistant default in strict modes, trusting the agent's own test claim, ownership widened by uncommitted
edits, merge edge cases in the shared-stack sync, reopen before checking, Retry after a failed check, and five minor
ones — are all fixed above and each has a test.

## Files

- `src/pkf_studio/flow.py` — gate fixes, review-step jobs, `run_from` / `jump_check` / `reopen_stage`, stop-after,
  `assistant_may` / `assistant_act` / `default_actions`, `bucket_drift`.
- `src/pkf_studio/pilot.py` (new) — *Assistant can act*.
- `src/pkf_studio/stages.py` — scoped, memoised G5 staleness.
- `src/pkf_studio/ledger.py` — bucket-stack parsing, `input_hash_scoped` (the ledger's `input_hash` is unchanged).
- `src/pkf_studio/actions.py` — `validate_own`, scoped 7.1 check, `_sync_shared_stacks`.
- `src/pkf_studio/assistant/` — framework and own-files guard, `summary` / `actions` / `plain_summary`, briefing §3.
- `src/pkf_studio/ui/workflow_dev.py` — navigator, Reopen / Run from on failures, the toggle, *In short*, *Do it*.
- `src/pkf_studio/gitops.py` — lock wait.
