# pkf Studio — user guide

Version 0.6.1 · Streamlit 1.66 · GitHub / GitHub Enterprise · Windows (Git Bash), macOS, Linux

pkf Studio is the front end of package-factory. It takes a requirement document and turns it into a pipeline that has been
approved, built, deployed and run locally. Every stage of that work is a branch and a pull request, and every approval
is given on GitHub. The app never stores state that git does not have. It works out where each pipeline stands from the
pull requests and the content of the branches.

## 1. Install and start

```bash
source ~/pkf/env.sh                       # proxy, CA bundle, ANTHROPIC_API_KEY
cd ~/pkf/package-factory
pip install -e '.[runtime,agent,studio]'
export GITHUB_TOKEN=github_pat_...         # or: gh auth login
pkf studio ~/pkf/specs                     # a clone of your specs repository; opens http://127.0.0.1:8501
```

- The app listens on 127.0.0.1 only.
- **Check the install.** Setup shows `pkf Studio <version> · pkf <version> · pkf_runtime <version> from <folder>`. The
  folder must be the one you just installed: the AI code tests run with that runtime. If it points to an older
  folder, you installed into another venv (S4 then reports "the test runner loaded package-factory … from …").
- Ctrl+C stops the app. Jobs that are running keep going, and the app picks them up again when it restarts.
- **GitHub token.** A fine-grained token for the specs repository needs, by mode:
  - **dev:** Contents: read and write; Pull requests: read and write; Metadata: read;
  - **sit, ppt, prd:** the same, plus Checks: read (the `pkf-gate` result), Workflows: read and write (the team PR adds
    the workflow) and Administration: read (the startup checks read the rulesets).

  If your organisation uses SSO, authorise the token for the organisation. The startup checks list what is missing.
- **Corporate proxy and the AI.** `REQUESTS_CA_BUNDLE` covers Python; the AI agent runs on Node.js, which needs
  `export NODE_EXTRA_CA_CERTS="$REQUESTS_CA_BUNDLE"` in `~/pkf/env.sh` (the startup check "Node CA for the AI" warns
  when it is missing).
- **Claude through Amazon Bedrock.** Set `CLAUDE_CODE_USE_BEDROCK=1` (or `PKF_LLM_AWS_PASSTHROUGH=1`): the AI steps then
  keep your `AWS_*` variables (other steps never get them; the code-test child has no credentials at all).
- **Terraform** is needed only for the dev-environment stage (S5/S6), and only for pipelines that need AWS resources
  (an SQS queue). Download the Windows zip from HashiCorp, check it against SHA256SUMS, and put `terraform.exe` on your
  PATH. Version 1.10 or later is required.

## 2. Setup (once per repository)

Open **Setup** in the top navigation (it always opens, even when a startup check fails). It has two parts.

1. **Startup checks.** Each check shows green or red, with the fix for anything red.
2. **Project and team.** Fill in:
   - the mode (`dev`, `sit`, `ppt` or `prd`);
   - the base branch;
   - the AWS account ID and region for dev;
   - optionally a run role and a Terraform state bucket;
   - the code owners;
   - the team: name, e-mail, roles and GitHub login for each person.

   Then click **Open the team PR**. That PR adds:
   - `.pkf-studio/project.yaml` and `.pkf-studio/team.yaml`;
   - `pkf.toml`;
   - `.gitignore` entries;
   - the `pkf-gate` workflow, `CODEOWNERS`, and the framework wheel in `tools/` that the workflow installs.

   Merge it on GitHub.

Then choose **Who am I** (it is kept on this computer only). The app checks your choice against `git config user.email`
and against the GitHub login the token belongs to.

| role | approves |
|---|---|
| Product Owner, Business Analyst, Architect | G1 Requirements (all three) |
| Data Engineer | G2 Design, G3 Plans, G4 Code & build, G5 Dev environment |

- **Dev mode:** one person may hold every role.
- **SIT, PPT and PRD:** each role must be a different person, and nobody can approve a pull request they opened or
  committed to.
- **Changing the team or the mode:** open a new team PR. Lowering the mode also needs an approval from an Architect who
  is not the author.

## 3. How approval works (in git)

1. **Request approval.** When a stage is ready, **Request approval** commits the work, pushes `pkf/<pipeline>/g<N>` and
   opens a pull request. The PR description lists:
   - what to review;
   - who must approve;
   - how to approve.
2. **Approve on GitHub.**
   - **Review → Approve** on the latest commit.
   - If you hold several roles, write `Approve as: architect` (or `po`, `ba`, `de`) in the review.
   - **Dev mode only:** you may instead comment `/approve <first 7 characters of the head commit>`, because GitHub does
     not let you approve your own PR. The **Post /approve as me** button posts that comment with your own token.
   - `/reject <reason>` or **Request changes** blocks the gate.
   - A plain `/approve` approves the head commit at the time of the comment. A line that starts with `/approve` but is
     neither `/approve` nor `/approve <7–40 hex>` is not counted; the gate card says why.
3. **How the app reads approvals.** It reads the reviews and comments back from GitHub; **Refresh** polls again.
   - Only the latest review from each person counts, and only when it was given on the current head commit.
   - New commits reset the approvals. Every push that moves the head of an open PR (a stage job or **Update the PR**)
     first adds an empty commit "<gate> <title>: work pushed (approvals reset)", so a plain `/approve` written before the
     push stays on the older commit and never approves work it did not see.
4. **Merge.** Once the gate has passed, **Merge** merges the PR (merge commit, or squash or rebase when the repository
   allows only those). The app passes the head commit it evaluated to GitHub, so GitHub refuses the merge if anything
   was pushed after the approval. In SIT, PPT and PRD the required `pkf-gate` check must also be green. Merge stays
   disabled while this computer has commits or changes the PR does not show (only being behind GitHub is fine) and
   while a job runs.
5. **Ledger.** The next stage starts by writing a row to `.pkf-studio/approvals/<pipeline>.jsonl`. The row records who
   approved, in which role, the review or comment ID, the approved commit, and a hash of exactly what was approved.

   The framework's own approvals in the lock files are recorded at the same time, with the GitHub identity and a link
   to the evidence (`pkf approve-plan|approve-code --evidence`).

**Stale gates.** If something a gate approved changes on the base branch later, that gate becomes *stale*. For example,
someone edits the requirement after G1. When a gate is stale:
- every later stage is blocked;
- **Reopen** restarts that gate from the base branch and closes the later stage PRs. It first shows what it sets aside
  ("Reopen G3 sets aside 2 unpushed commits on g4 (kept as refs/pkf-archive/…)") and needs a second click,
  **Confirm reopen**. Every branch tip it moves is kept as a local ref under `refs/pkf-archive/<pipeline>/`; Workflow
  lists them under **Set-aside work (after Reopen)** with the command that restores one (`git branch <name> <ref>`).

**Merged without approval.** When a stage PR was merged on GitHub before it had its approval, the next Start finds out
and the gate shows "🟠 merged without approval"; the later stages are blocked. In dev mode **Post /approve on PR #n as
me** approves the merged PR and checks again (**Check again** asks GitHub once more). In SIT/PPT/PRD, and whenever the
gate was *blocked* (files outside the pipeline, protected files), approve it again with **Reopen**.

### SIT / PPT / PRD: what GitHub must enforce

Create a ruleset on the base branch with:
- pull requests required, with at least 1 approval;
- dismiss stale approvals;
- require approval of the most recent push;
- require review from code owners;
- the required status check `pkf-gate`;
- block force pushes;
- restrict deletions.

The startup checks read these rules and list anything missing. With these rules in place, nobody can merge without the
right approvals, even in the GitHub UI.

`pkf-gate` runs `pkf studio verify` using the base branch's code. It checks that `tools/SHA256SUMS` matches the
framework wheel before installing it. It fails when:
- the gate has not passed under the base branch's roster;
- a stage PR changes protected files (`.pkf-studio/project.yaml`, `team.yaml`, `.github/`, `tools/`, `CODEOWNERS`),
  including moving or renaming them;
- a stage PR changes anything outside its own pipeline: its requirement, pipeline, connection and component files,
  its `infra/`, `review/` and `dags/` folders, and its ledger. Bucket stacks are allowed only at G5. A record PR may add
  only a run report and the G5 ledger row;
- any approvals ledger row that the PR adds or changes differs in any field from the row rebuilt from GitHub and git,
  or a PR changes another pipeline's ledger;
- a PR lowers the mode, or deletes or breaks the project config, without approval from an Architect who is not an
  author;
- for G4 and G5, the tests or the build fail.

The ledger is not covered by CODEOWNERS, because every stage PR writes to it. `verify` checks every row instead.

## 4. Stages

| stage | the app does (as a background job, with a live log) | gate |
|---|---|---|
| S1 Requirements | **Analyze**: the AI requirement analyst writes the finalized requirement, the examples and the pseudocode, then the split runs. You edit, answer the open questions, and run **Split again** if needed. | G1 |
| S2 Design | Records G1 in the ledger. Splits again only if the requirement changed while G1 was under review. | G2 |
| S3 Plans | Approves the split in the framework and writes the component files. You fill in the environment values (buckets, folders, queue URLs) in the Source, Destination and Trigger tabs. **Plan components**: the AI plans each component, and each plan is simulated on its examples. If no component needs an AI plan, **Continue to code & build** skips G3. | G3 |
| S4 Code & build | Approves the plans. The AI writes code for custom steps, one step at a time (tested in a hardened runner; what it wrote is kept if it stops part-way). Then the end-to-end examples, a **draft build** (every check of the real build with the tested but not yet approved code; it writes nothing, so `dags/` only ever holds approved code and is built at S5) and a capability summary (deletes, network, processes). The **S4 status panel** in Workflow shows the draft build, the examples and every component with its next action: **Run tests & build again**, **Regenerate (AI)** for a component or one of its steps, and **Restore dags/ from main** when the branch holds `dags/` from an earlier build. The job pushes the branch when it ends, even when a step failed. | G4 |
| S5 Dev environment | Approves the code. Renders Terraform (queue, DLQ, alarm, bucket notification) and the worker IAM policy. Runs `terraform plan` (guarded) and the policy checks, then a dry run of the first run (checking your AWS credentials first; a failed dry run blocks G5). For a pipeline that moves files it records whether the source bucket has versioning. **Render & plan again** repeats it. | G5 |
| S6 Run local | Checks your AWS credentials, applies the approved saved plan (only if its hash matches and the account is the pinned one; skipped when that plan is already applied), runs the pipeline under the run role, writes `review/<p>/runs/<run>/` (`report.json`, `files.csv` with one row per file, `run.log`) and opens the record PR. A pipeline that moves or deletes files runs in two clicks: **Preview this run**, then **Move <n> files** (see *S6 preview and move*). Others: **Run locally**, with **Packages in this run (max)**. | — |

Every stage job pushes its branch when it ends (also when it failed part-way), so a second computer continues from the
pushed branch.

Above **Request approval** the current gate shows a checklist "Before Request approval" of what is still missing; each
item names the button that fixes it, and the button stays disabled until the list is empty. It is refused while the
stage is not ready, for example when:
- the split has open questions or errors;
- environment values are still `<REQUIRED>`;
- a plan fails its examples, or the end-to-end examples fail;
- the build failed (at S4: the draft build), or `dags/` on the S4 branch differs from the base branch;
- the Terraform plan is missing, or the dry run failed;
- the branch has files outside the pipeline's own (**Remove the files outside this pipeline** takes committed ones off
  the branch in one commit; uncommitted ones: **Discard them**).

A job can succeed and still report **problems** (an AI call that failed, a push that did not go through): the job
panel then shows "⚠️ Last job: … — finished with problems" with the first problems and what to do. The full text is in
**Job log and history**. While a job runs, the panel shows when it started (local time), how long it has run and the
log's last line; a long framework step logs "… still running pkf <command> (2m30s)" every 15 seconds.

### S4 troubleshooting

Read the **S4 status panel** (Workflow, stage S4) first: every component has one line with its state and its button.

| symptom (in the S4 status panel or the job panel) | what to do |
|---|---|
| Last job "finished with problems": "… the AI step failed; this is usually temporary: run it again" | **Regenerate (AI)** for that component (in its row, or in the picker below the panel) |
| "… the AI step stopped at its turn or budget limit" | **Regenerate (AI)** again; it continues with the steps that are still missing. Raise `PKF_LLM_MAX_BUDGET_USD_<TASK>` only on purpose |
| a component says "code not written yet" (also when the AI coder stopped part-way: the steps it did not write are named) | **Regenerate (AI)** in its row: it writes the missing steps and keeps the others |
| a component says "code fails its tests" (the failing step, case and detail are listed) | **Regenerate (AI)**: `<step>` for the failing step only (the other steps are kept), or edit only the function body in the code file, then **Run tests & build again**. If a test case itself is wrong, Reopen G3 and re-plan the component |
| a failing case says "code failed to load: …" | the file does not import (a syntax error or a module-level line): **Regenerate (AI)**, or fix the file, then **Run tests & build again** |
| "the code changed since its last test" (you edited the file) | **Run tests & build again** |
| "Draft build: failed" | the issue under it names the component or the file; fix it, then **Run tests & build again** |
| "dags/ on this branch comes from an earlier build and differs from the one on main" | **Restore dags/ from main** (one commit; the DAG is built again at S5 from approved code) |
| "the plan changed: Reopen G3" | the plan is approved at G3: Reopen G3 (in the G3 card), plan again, approve again |
| "This status was written by an earlier version of the Studio" | **Run tests & build again** (it writes the full status) |
| "the test runner loaded package-factory … from …, this is … from …" | reinstall in the venv you start `pkf studio` from: `pip install -e '.[runtime,agent,studio]'` in the new folder (Setup shows which folder is used) |
| the Workflow lists uncommitted files (e.g. after a manual `pkf approve-code`) | **Commit them**, then **Run tests & build again** |
| "could not push …: your work is committed on this computer" | nothing is lost: the next job or Request approval pushes it. Check the proxy and `source ~/pkf/env.sh` |
| "GitHub's copy of pkf/<pipeline>/g4 changed" | someone pushed to the branch or used *Update branch* on GitHub. When this computer has no other commits the app takes GitHub's copy itself; otherwise the Workflow shows the card **GitHub's copy changed**: **Take GitHub's copy** (your commits are kept as set-aside work), **Merge GitHub's copy into mine**, or (dev only) **Overwrite GitHub's copy (dev)**. Then **Run tests & build again** |
| a step seems stuck | the running-job box shows the time and the last log line ("… still running pkf code …"); an AI coder call can take minutes. **Cancel** stops the job; what it finished is committed |

### S6 preview and move

For a pipeline whose destination moves or deletes files (a custom delivery step), S6 never moves anything in one click:

1. **Packages in this run (max)**: 10 for the first run of the pipeline (no successful run record on the base branch
   yet), else 0 = all. The others wait for the next run (their SQS messages are released, not failed). When every
   package has one file, the caption says "one file each here".
2. **Preview this run** (a job): a dry run on a new run branch that lists **every** planned file into the git-ignored
   `.pkf-studio/cache/runs/<pipeline>/<run>/planned.json`. Nothing is committed or pushed. The card shows the counts per
   top-level target prefix, the files routed to `error/`, how many source objects would be deleted, and the first 500
   files; **Download the full list (planned.json)** has all of them.
3. Confirm: **Run with my own AWS credentials** (only without a run role, in a one-person dev team), and "I understand
   <n> source objects in <bucket> will be deleted" when the move deletes sources. When S5 found that the source bucket
   has no versioning, the G5 and S6 cards say "deletes and overwrites in <bucket> are irreversible (versioning is off)".
4. **Move <n> files** (a job): exactly the previewed files, with the preview's run id, date and parameters. A file added
   or changed since the preview waits for the next run. A preview older than 24 hours, or one already moved, is refused:
   **Preview this run again**. Before anything moves, the job commits `started.json` on the run branch.

Afterwards `files.csv` has one row per file (`package, index, path, source_key, target_key, status, detail, note, size`;
`note` is the rule or error reason the earlier layout steps set) and the S6 card shows it with **Download files.csv**.
A failed run is recorded too: the record PR is merged in dev and the S6 card says "❌ last run failed" with the first
issue. If the record could not be pushed, or a Move did not finish (laptop asleep, killed), the card shows **Publish run
record**: it pushes every run record that is only on this computer; a crashed move is recorded as failed ("the run did
not finish (crashed); run.log shows how far it got"), and its branch is kept as set-aside work, never deleted.

## 5. The pages and the tabs

0.8.0 (ST-3 v3 F): a light theme, a sidebar and four pages under a top bar.

- **Sidebar**: pkf Studio with the version, repository, base branch and mode; the pages **Pipelines · Workspace ·
  Artifacts · Setup**; the **Pipeline** selector (➕ New pipeline); **ALL PIPELINES** — every pipeline with its stage chip
  (`S4`, `S4 failed` in red, `G4 waiting` in amber, `done`; click one to open it); **RUNNING JOBS** of every pipeline
  (click to open it, with the elapsed time); the **ASSISTANT** card — **Acts on its own** (the 0.6.1 *Assistant can
  act* switch), what the assistant is doing in one line (a red dot when it waits for you), **Open** (a pop-up) and **⤢**
  (full screen); and you at the foot (⋮: version, repository, warnings, Setup).
- **Top bar** (Workspace, Pipelines, Artifacts): where you are (`Workspace / <pipeline>`), the mode (`DEV · auto-approve
  on/off`), the **source-control chip** (repository · branch · commits not on GitHub · clean / n changed · open PR — click
  it for Commit / Discard, GitHub's copy changed (Take / Merge / Overwrite), the PR and Open in VS Code), **Refresh**,
  **Stop run** / **▶ Resume** and **Run ▾** (Auto-approve gates in dev, Forget this run).
- **Workspace** — the run of the pipeline (§10):
  - the **stepper**: the seven stages with their progress, gate and what is running. Click a stage to look at its
    substages; the page follows the run again when the run moves to another stage.
  - the **stage header**: `STAGE 4 OF 7 · 17 / 34 SUBSTAGES DONE`, what the stage does, **Request approval G\<n\>** (hidden
    while the run is on the gate step, which opens the PR itself; disabled while a job runs or an item is open; *Update
    the PR* when it is open), *n things still needed*, and **↩ Go back · run a stage · reopen** (§12).
  - **SUBSTAGES**: one card per substage, in a row that scrolls sideways (running / waiting / failed cards framed).
    The running card has its time and **Cancel**; click a card's title to show its log in the console and pre-select it
    in ↩ Go back.
  - **Your turn**: the current step's card exactly as before (gate wait with Approve as me, reviews, inputs, checklists,
    failures with Retry / Ask the assistant / Skip, the S6 preview and Move), or the intake before the run.
  - **Before Request approval G\<n\>**: ✓ what passed (G4: the draft build, the end-to-end examples, dags/, no job
    running) and ○ what is open, each with its button — a component failing its tests (**Regenerate \<step\>**), the
    gate's unmet items with their fix, files outside the pipeline (Remove), a running or crashed job, the run or the
    assistant stuck, uncommitted changes and a diverged branch (fixed in the branch chip), GitHub unreachable, a broken
    worktree (Repair).
  - **Components** (S4: every component with its state, **Regenerate (AI)** and **Run tests & build again**; the other
    stages: the open questions, the split, the plans, Terraform / policy / dry run, the local run, the deploy notes).
  - **Files of this stage** (a status line each; **Open →** shows the file on the Artifacts page).
  - the dark **console**: the step selector, **find in log**, level chips, Copy, Follow, line numbers and **⬇ Log**
    (the whole job log); under it the views **Console · Workflow log · Assistant transcript · Approval history · Jobs**.
- **Pipelines** — every pipeline (stage, run, running job, open PR; Workspace / Open), then the chosen pipeline's
  job panel and the seven tabs below, unchanged.
- **Artifacts** — the pipeline's files grouped by stage; the chosen file full height: Code, Plan cards, Tests, **Diff vs
  \<base\>** (the file as it stands, uncommitted edits included, against the base branch), Capabilities, Data-flow
  diagram, Requirement; Regenerate (AI) for a component's code; Download artifacts (zip).
- **Setup** — §2.

The app opens on the Workspace, in a light theme (⋮ → Settings switches to dark or your system's theme).

The seven tabs of the Pipelines page:

1. **Requirements:** upload (.txt, .md, .docx or .pdf, up to 10 MB) or paste, with a secret and personal-data scan.
   Shows the extracted text, the finalized requirement (editable, with a preview), the pseudocode, the examples (YAML,
   validated as you edit), the assumptions, the open questions and the split.
2. **Source** and 3. **Destination:** the settings, the environment-values form (at S3), the plan as step cards, the
   generated code, and a connectivity snippet. Destination also shows the delivery policy.
4. **Trigger:** how runs start, in words, the AWS pieces the trigger needs, and its Terraform.
5. **Transformation:** the requirement in plain English and the steps as task cards with their code. It says "No
   transformation" when files move unchanged.
6. **Pipeline:** a summary, the data-flow diagram (downloadable as a .dot file), **Pipeline settings** (owner, start
   date, retries, retry delay, tags, max active runs; editable at S2–S4 when no job runs, committed as "S<n> <p>:
   pipeline settings"; they are in no gate's hash and reach the DAG at S5), and the codebase with search, line numbers
   and an **Open in VS Code** button.
7. **Workflow:** the gate cards, the current stage's actions, the live job log, the approval history, set-aside work and
   **Download artifacts**. The running jobs of every pipeline are in the sidebar (RUNNING JOBS).

The 0.6.0 **Workflow Developer** tab is the **Workspace** page since 0.7.0 (§10, §11).

## 6. Artifacts

**Download artifacts** builds a zip from the files tracked in git:

```
01_requirements/  original document, extracted text, pipeline.yaml, pseudocode.md, analysis.json
02_design/        split.lock.yaml, components/<kind>/<name>.yaml
03_plans/         <kind>/<name>.lock.yaml and a readable <kind>/<name>.md
04_code/          component code, dags/, capabilities.md
05_infra/         terraform/ (module, envs, bucket stacks), iam-policy.json, manifest.json, policy-check.json
06_deploy/        deploy.md
07_runs/          dry-run.json, runs/<run>/report.json, apply.json
workflow/         approvals.jsonl, logs/ (job logs, secrets scrubbed), flow-events.jsonl (the Workflow Developer run),
                  assistant/<session>/ (every diagnosis, fix and transcript), README.md
```

In dev mode, approvals that one person gave in several roles, or on their own PR, are marked `self_asserted`. The
README in the zip says so.

## 7. Infrastructure rules

- **Queues.** The Studio creates the queue `pkf-dev-<pipeline>`, with `_` written as `-`. Point the trigger's
  `queue_url` at that name, e.g. `https://sqs.<region>.amazonaws.com/<account>/pkf-dev-<pipeline>`. Any other queue URL
  is treated as an existing queue: it is not created, and its bucket notification is left to the queue's owner.
- **Bucket notifications.** There is one stack per bucket, under `infra/buckets/<bucket>/`, and it needs the S3 state
  backend. If the bucket already has notifications this repository does not own, the Studio writes `handoff.json` for
  the bucket's owner instead of planning the stack.
- **Scheduled pipelines** need no AWS resources: "nothing to apply — policy only".
- **Guards.**
  - Destroy, replace, and any resource type outside the allowlist are refused.
  - Only the saved plan approved at G5 is applied.
  - The provider is pinned to the project's account.
- **Higher environments.** The `sit`, `ppt` and `prd` tfvars are templates with `<REQUIRED>` values, for the platform
  team.
- **Applied plans.** After a successful apply the stack's plan folder gets `applied.sha256` (the approved plan's hash);
  a second run with the same approved plan skips the apply ("already applied") instead of failing with "Saved plan is
  stale". **Residual risk:** the drift check is not in 0.5.0 — a second S6 run with Terraform skips the apply even if
  the stack was changed or deleted outside the Studio since. If you changed the dev resources by hand, **Render & plan
  again** at S5 (a new plan) and approve G5 again.
- **Removing dev resources.** The Studio never destroys. To remove them, in Git Bash:
  `terraform -chdir=.pkf-studio/worktrees/<pipeline>/infra/<pipeline>/envs/dev destroy` with the same AWS profile (bucket
  notification stacks under `infra/buckets/<bucket>/` the same way), then delete `applied.sha256` there.
- **Staging on Airflow workers.** Every worker must reach the same staging: set `PKF_STAGING_URL=s3://<bucket>/<prefix>`
  (with a lifecycle rule) unless Airflow runs on one machine; with `file://` staging the build warns. A worker without
  the run plan fails with "the run plan is missing in staging …".
- **Run role.** Set a run role in Setup: `pkf run` then assumes it, with the generated worker policy as the session
  policy. Without one, only a single-person dev team can run, with its own credentials, and only after confirming.

## 8. Troubleshooting

| symptom | fix |
|---|---|
| CERTIFICATE_VERIFY_FAILED | `source ~/pkf/env.sh` (REQUESTS_CA_BUNDLE, AWS_CA_BUNDLE) before `pkf studio` |
| "GitHub token" red | `export GITHUB_TOKEN=…` or `gh auth login`; SSO-authorise the token |
| a job shows *crashed* | "The job's process ended without finishing (laptop asleep, killed, or Python crashed). What it did so far is committed. Run '<title>' again." The panel also shows the log's last line, the process's own output (`crash.log` in the job folder) and the job folder |
| a Move did not finish (crashed move) | the run branch with `started.json` but no report is kept (and archived); **Publish run record** records it as failed; then **Preview this run again** — files already moved are recognised by the next run |
| "the run finished but its record could not be published" | **Publish run record** (Workflow, S6) |
| "GitHub unreachable: …" banner (offline) | the proxy or GitHub is down. The tabs show this computer's copy, read-only; gates show "❔ unknown (cannot reach GitHub)"; buttons that need GitHub say so when clicked. `source ~/pkf/env.sh`, check `HTTPS_PROXY` and `REQUESTS_CA_BUNDLE`, then **Retry** (the app also tries again by itself after a minute). Reopen waits until the base branch can be fetched |
| "GitHub rate limit reached; retry after HH:MM" | wait until then; the app never sleeps for it |
| GitHub's copy changed | card **GitHub's copy changed** in Workflow (see S4 troubleshooting) |
| "merged without the required approval" | **Post /approve on PR #n as me** (dev) or **Reopen** (see §3) |
| "<n> commits on g<k> were not in the merged PR" | the PR was merged on GitHub before your last push; the commits are kept as `refs/pkf-archive/…`: Reopen G<k> to include them, or ignore them |
| set-aside work | Workflow → **Set-aside work (after Reopen)**: restore one with `git branch <name> <ref>` |
| "the worktree folder … is broken" / the repository folder was moved | **Repair the worktree** (Workflow): `git worktree repair`, else the folder is moved aside (`<name>.broken-<time>`) and checked out again. If moving it fails, close programs using it (VS Code) |
| "no valid AWS credentials (…): run aws sso login …" | log in again (`aws sso login --profile …` or `source ~/pkf/env.sh`) and run the job again; "your AWS credentials are for account <a>; the project is pinned to <b>": switch profile |
| "AWS credentials expired after k of n packages" | log in again and run again; delivered files are recognised by the next run |
| uncommitted changes on the branch | Workflow shows them with **Commit them** / **Discard them** |
| a local run cannot be cancelled | by design: it may be applying Terraform; wait for it to end |
| "another git operation is still running" | wait. A lock whose process has ended is cleared; one older than 30 minutes is cleared (`.pkf-studio/locks/git.lock`). A leftover `index.lock` older than 60 s in a worktree is removed when no other job of that pipeline runs |
| "a job is running for <p>; wait for it to finish" | git buttons (Merge, Reopen, Post /approve, Repair …) are disabled while a job runs; wait for it |
| files show as changed right after cloning (line endings) | the team PR adds `.gitattributes` with `* -text`: git never converts line endings in any clone; all files are written with LF |
| "the worktree has uncommitted changes" | open the tab you were editing and Save, or discard the changes in `.pkf-studio/worktrees/<pipeline>` |
| G1 refused: open questions | answer them in the requirement text, **Split again**, then request approval |
| "this pipeline moves or deletes files: Preview this run first, then Move" | S6 → **Preview this run**, then **Move <n> files** |
| the saved plan is not on this machine | S6 must run on the laptop that planned S5 (the binary plan is never committed): **Render & plan again** and re-approve G5 |

## 9. What is sent to the AI, and residual risk

- **Credentials per step.** Each framework step gets only the credentials it needs. The AI roles get the Anthropic key.
  Only live runs and checks get AWS credentials. No step gets the GitHub token.
- **The repository's own files** can never stand in for the framework: child Python processes run with a safe
  `sys.path`.


- **What leaves the laptop.** The extracted document text, the specs, the plans and the code are sent to Anthropic
  under your organisation's agreement. Before anything leaves, text that looks like a secret is scrubbed.
- **What must never be in requirements or examples:** patient or personal data. The examples must be synthetic.
- **Generated code before G4.** It runs only in the hardened test runner:
  - no network except localhost;
  - no subprocesses;
  - no credentials;
  - an in-memory S3.

  This is not an operating-system sandbox, so the capability summary is part of the G4 review.

## 10. Workflow Developer (0.6.0) — the Workspace page since 0.7.0 (0.8.0 layout: §5)

The page shows the pipeline's run as seven stages with their substages (S1 Requirements → G1 … S6 Run local + record PR,
7 Deploy to Airflow; 34 substages in all), and drives it:

| colour | meaning |
|---|---|
| grey, muted | not started |
| light green, pulsing | running (a job) |
| amber, pulsing | waiting for you: an approval, an input form, a review, a checklist |
| dark green | done |
| red | failed |
| grey dash | skipped (not applicable, e.g. 3.6 G3 when no component needs an AI plan) |

The stepper shows the stage the run is in; click another stage to look at it. Clicking a substage card's title shows
its job log in the **Console**. The running substage card says what is running (with its time and **Cancel**); *Your
turn* says what the run is waiting for. The
**Workflow log** tab is the run's journal (`.pkf-studio/flows/<pipeline>/events.jsonl`, git-ignored, also in the
artifacts zip); the **Assistant transcript** tab is the assistant's live transcript.

**Run workflow.** Before the run starts, the intake asks for everything that can be known: the requirement document
or text (with the secret and personal-data scan), and it tells you what Setup still lacks (Who am I, AWS account, run
role). The run then pauses only for what the AI discovers: **1.4** the open questions and ambiguities (answer them in
the requirement text; a change runs *Split again*), **3.2** the environment values, **6.1/6.3** the S6 confirmations
(own credentials, *Move <n> files* after the preview), and the reviews (2.2, 3.4, 4.2, 5.3).

**Gates.** In dev mode each gate shows **Approve as me**; the run merges the PR itself once the gate has passed. The
toggle **Auto-approve gates (dev only)** lets a dev run go through every gate without a click. In SIT/PPT/PRD the run
waits for the approvers on GitHub (polled every 15 seconds) and merges by itself. A `/reject` or a closed PR sends the
run back to the stage's review substage, as the red edges of the workflow diagram do.

**Failures.** A job that fails, or ends with problems, turns its substage red and pauses the run: **Retry**, **Ask the
assistant** (§11), **Continue anyway** (only when the job itself succeeded with warnings), **Skip** (optional substages
only: 4.3, 5.2, 6.5), **Stop**. **Resume** continues from the same substage; **Forget this run** forgets the cursor
only — the pipeline, its branches and PRs are untouched, and Run workflow starts again from the current gate.

**What the run does not do.** It never approves in your place (except with the dev-only toggle), never pushes beyond
what the stage jobs push, and stage 7 (deploy to Airflow) is a checklist with the commands from `deploy.md`: the app
has no connection to Airflow. Who ticked each item, and when, is in the workflow log.

The run's state is a cursor, not a source of truth: the gates still come from GitHub and git (§3). Closing the browser
or restarting the app loses nothing; the run continues from the pushed branches and the running job. The run only
advances while the tab is open (it is driven by the page).

## 11. The assistant (0.6.0)

The assistant is a Claude agent (the same Agent SDK, key, proxy and Bedrock settings as the AI steps) that knows the
framework (its briefing is `pkf_studio/assistant/knowledge.md`), and can read the framework's source, the pipeline's
worktree, every job's log and result, and the run's journal. It never sees AWS or GitHub credentials.

- **Ask**: a question about the pipeline or the stage ("why is G3 stale?", "what did 4.1 produce?"). Read-only.
- **Diagnose** (also *Ask the assistant* on a failed substage): finds the root cause, cites the evidence (files, log
  lines), and proposes a fix with its kind:
  - `edit_files` — it will change files in the worktree: **Approve the fix plan** starts the fix phase;
  - `studio_action` — an existing button does it (Reopen G3, Restore dags/ from main, Regenerate, Render & plan again);
  - `user_action` — something only you can do (`aws sso login`, the proxy, Terraform on the PATH);
  - `settings` / `rerun` — a limit to raise, or simply Retry.
- **Fix** (after your approval): it edits only the pipeline's own files (never the protected files, the ledger, lock
  files, `dags/` or `.pkf-studio/`), runs the tests in the hardened runner, and shows the diff. Nothing is committed
  until **Apply the fix**; **Discard** restores the files. If the fix changes something a merged gate approved, it says
  which gate to reopen. Then **Retry** the substage.
- Each session is a job (cancel in the job panel) with a live transcript in the **Assistant** console; its diagnosis,
  fix, diff and transcript are kept under `.pkf-studio/flows/<pipeline>/assistant/<session>/`, written to the workflow
  log as `assistant_*` events, and included in the artifacts zip. Fix commits are prefixed `assistant:`.
- Limits (per phase): `PKF_LLM_MAX_TURNS_ASSISTANT_DIAGNOSE|FIX|ASK`, `PKF_LLM_MAX_BUDGET_USD_ASSISTANT_*`,
  `PKF_LLM_MODEL_ASSISTANT`. One agent does the work; it may call two narrow helpers — `log-digger` (reads a long log
  and returns the relevant lines) and `verifier` (runs the tests after a fix, without having written it).

### The assistant in 0.8.0

- **Where**: sidebar → **Open** (a pop-up over any page; X closes it) or **⤢** (full screen; **Exit full screen** brings
  the sidebar back). Both show the same thing:
  - **Where things stand** — a short structured brief, built from the run's files without a model call: *Done* (the
    stages and gates passed), *Now* (stage · substage · status · time · what the job is doing), *Next*, *Issue* (a failed
    step, a failing component, a crashed job), *Blocking*, the last log lines and the pipeline notes;
  - the **Likely cause** and the next step: one short model call (Opus 5.5, low effort) made by itself when there is an
    issue, at most once per situation and every 10 minutes, and kept until the situation changes (*Update the likely
    cause* asks again);
  - the conversation, the decisions (Approve the fix plan / Dismiss, Apply / Discard, Do it) and **Ask** / **Diagnose**.
- **The assistant lane**: Ask, Diagnose and the likely cause run in the assistant's own read-only lane, so you can ask
  while a pipeline job (e.g. 4.1) runs. A fix still waits for the pipeline to be idle (it edits files and runs tests).
- **Model**: Claude Opus 5.5 for every phase (effort: brief low, ask medium, diagnose high, fix high), falling back to
  Sonnet 5.5 when Opus is unavailable; the log-reading helper runs on Sonnet 5.5. Limits: diagnose 30 turns / $4 /
  15 min, fix 50 / $8 / 25 min, ask 15 / $2 / 10 min, brief 4 / $0.50 / 2 min (the `PKF_LLM_*_ASSISTANT_*` variables
  override them).
- **What it knows**: the framework's architecture (its briefing), a map of the framework's code (every module, class
  and function with its first docstring line; built from the source and cached under `.pkf-studio/cache/`), and the run
  brief — in every prompt; it reads any framework file, the pipeline's worktree, every job log and the run's journal when
  it needs more, and can refresh the brief mid-session.
- **Pipeline notes**: the assistant keeps short notes per pipeline (root causes found, your decisions, what did not
  work) in `.pkf-studio/flows/<pipeline>/assistant/memory.md`; they are part of every brief.
- **What it may change**: only this pipeline's own files (its spec, its components, its requirement and review files);
  never the framework's code, another pipeline's files, a shared component, lock files, `dags/` or `.pkf-studio/`.

### Run without stopping and the test package (0.8.2)

- **Run without stopping (dev)** — intake switch or **Run ▾**: the run approves its gates, continues the reviews,
  regenerates failing components, confirms 6.1 / 6.2 from the intake's pre-answers, runs the pipeline locally (6.3) and
  moves its own test package, runs the 6.5 no-op check and passes 7.2. It stops for what needs a person: open questions,
  environment values, a component still failing, files it did not place itself (unless you allow it), and the Airflow
  deploy (7.3 / 7.4). Every decision is in the workflow log (`full_auto`).
- **Test package** — when the 6.3 preview finds nothing, **Prepare a test package** (automatic under Run without
  stopping) places one of the pipeline's own examples in its dev source with your AWS credentials, never overwriting.
  When it cannot, 6.3 shows the `aws s3 cp` commands (and **Download upload.sh**): run them in Git Bash, then
  **Preview again**.
- Keep the Workspace or the assistant tab open while the run goes on: one tab at a time advances it.
- **⤢** opens the assistant in its own browser tab.

## 12. Going back, single stages, and the assistant acting (0.6.1)

- **↩ Go back · run a single stage · reopen a stage** (Workspace: the button in the stage header; clicking a substage
  card pre-selects it): *Run from* any step,
  optionally *stop after* a stage; *Reopen* an approved stage. A jump past a gate that is not approved is refused with
  the step to run first; running a stage whose gate is approved reopens it (two clicks).
- A gate whose PR is merged but not approved (stale, merged without approval, reopened, or blocked by an earlier gate)
  now stops red with the reason and a **Reopen** button, instead of waiting. A gate that is not ready names the step that
  fixes it (**Run from …**).
- **Assistant can act** (toggle; the intake, then **Acts on its own** in the sidebar's assistant card): on a failure the assistant diagnoses, then retries, goes back, reopens G2–G5, renders
  again, regenerates, or prepares a file fix that is applied only when its tests pass (3 tries per step). Reviews,
  approvals, confirmations, checklists and S1 stay with people. The assistant never writes framework code or another
  pipeline's files. Every answer starts with a plain-language *In short*.
- Pipelines are isolated: other pipelines' specs on main no longer fail this pipeline's validation (S3/S4) or deploy
  check (7.1); a shared bucket stack changed by another pipeline no longer makes this pipeline's G5 stale; Render & plan
  merges main when a shared stack changed. Details: docs/CHANGES-0.6.1.md.
