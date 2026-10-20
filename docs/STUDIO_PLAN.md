# pkf Studio — plan (draft 3, after two review rounds; updated for 0.5.0)

A Streamlit front end for package-factory: from a requirement document to a pipeline that runs locally, with every
stage committed to git and every approval given in git (GitHub pull requests).

Reviewed twice by five specialists (Git governance, Streamlit UX, security, AWS/Terraform, architecture). §16 lists
what the reviews changed.

## 1. Goals and scope

| goal | how |
|---|---|
| Build a pipeline from a requirement in one app | 7 tabs: Requirements, Source, Destination, Trigger, Transformation, Pipeline, Workflow |
| Git is the source of truth | the app refuses to work outside a git repository with a GitHub remote; every stage is a branch, commits and a pull request |
| Approvals happen in git, by role | a gate passes only when people holding the required roles approve the PR on GitHub |
| Dev mode | one person may hold every role and approve every gate |
| SIT / PPT / PRD modes | each role is a different person, authors cannot approve their own changes, and GitHub enforces it (ruleset + code owners + a required check) |
| Infra | deterministic Terraform (no AI) per pipeline; applied from the app in dev; plans for higher environments |
| Readable AI output | plans as structured cards, code with colouring and line numbers, a searchable file navigator, "Open in VS Code" |
| Everything downloadable | one zip with a fixed folder structure (§11) |

Not in v1: hosting the app for many users, remote Airflow deployment (the app produces the bundle and the commands),
promotion of one pipeline through dev→sit→prd inside one repo (each repo has one policy mode, §4.6), run_conf Lambda
triggers (dev uses `pkf run --conf`), EventBridge triggers for shared buckets (§10).

## 2. Architecture

```
pkf_studio/                       new package, extra: pip install 'package-factory[studio]'
  app.py                          Streamlit entry; `pkf studio [repo]` → `sys.executable -m streamlit run` (localhost only)
  checks.py                       startup checks (§3)
  project.py                      the opened repo: config + roster read from the BASE branch tip; per-pipeline worktrees
  team.py                         roster model, role coverage and gate feasibility
  gates.py                        pure functions: required roles per gate and mode → passed / missing / blocked, with reasons
  ledger.py                       append-only approvals ledger .pkf-studio/approvals/<pipeline>.jsonl
  stages.py                       stage derivation: f(pkf status --json, ledger input hashes, PRs) — no separate state file
  github.py                       GitHub / GitHub Enterprise REST client (requests; ETags; pagination; proxy + CA bundle)
  gitops.py                       git subprocess wrapper; one repo lock for every git write
  runner.py                       job process: python -m pkf_studio.runner <job> → .pkf-studio/jobs/<id>/{cmd.json,pid,log,result.json,exit}
  jobs.py                         start / inspect / cancel jobs from those files; per-pipeline lock with stale-lock detection
  docimport.py                    .txt/.md/.docx/.pdf → text in a subprocess with limits; secret/PII scan
  infra.py                        Terraform rendering from the specs (templates, no AI); plan / apply wrappers with guards
  diagram.py                      DOT string for st.graphviz_chart (escaped labels)
  bundle.py                       artifact zip
  verify.py                       `pkf studio verify` — gate evaluation for the GitHub Action (§4.4)
  ui/                             one module per tab + widgets (gate card, job log, code viewer, file navigator, plan cards)
```

- **The framework stays the single implementation.** Every action is a `pkf --json …` subprocess run by the job
  runner; the full JSON result is kept with the job. The environment is passed through (so the framework still
  refuses approvals inside agent sessions), minus what the step does not need.
- **No second source of truth.** A pipeline's stage is derived each time from the framework's own status (it detects
  stale plans and changed code from content hashes), the ledger's input hashes (for G1 and G5, which the framework
  does not know) and the PRs. Job folders and UI caches are git-ignored and disposable.
- **Worktrees.** Each pipeline's stage work happens in its own `git worktree` (`.pkf-studio/worktrees/<p>`,
  git-ignored): the user's checkout is never switched, and several pipelines can progress at once.

## 3. Start-up, project, team

1. **Startup checks** (red/green list with the fix for each; the app stops until the required ones pass):
   - git work tree with a commit and a GitHub remote; clean work tree;
   - GitHub token (`GITHUB_TOKEN`/`GH_TOKEN`/`gh auth token`), SSO-authorized, with the permissions needed —
     fine-grained for this repo: Contents RW, Pull requests RW, Metadata R (Members R when roles map to teams); a
     classic `repo` token works with a warning;
   - `ANTHROPIC_API_KEY`; CA bundle for Zscaler (`REQUESTS_CA_BUNDLE`, `AWS_CA_BUNDLE`);
   - Terraform ≥ 1.10 (only needed for the dev-environment stage);
   - SIT/PPT/PRD: the base-branch rules of §4.4, read with `GET /repos/{o}/{r}/rules/branches/{b}` (Metadata read is
     enough; classic branch protection would need admin rights). When the repo uses classic protection instead, the
     app accepts `protected: true` plus the required check name, and a repo admin confirms the rest once (recorded).
2. **Project config** `.pkf-studio/project.yaml` (base branch): mode, base branch, GitHub API URL (Enterprise:
   `https://HOST/api/v3`), allowed AWS account id + region, mandatory tags, optional run role (§5, S6).
3. **Team** `.pkf-studio/team.yaml` (base branch): per person name, roles (`product_owner`, `business_analyst`,
   `architect`, `data_engineer`), email, GitHub handle (optional in the form; required for anyone who approves). The
   GitHub numeric user id is stored with the handle (logins can be renamed).
   - Project config and team are read **only from the base branch tip**, never from a PR.
   - They change through a "team" PR. In SIT+ that PR needs the admin code-owner team (CODEOWNERS on `.pkf-studio/**`)
     and, to lower the mode, also an Architect who is not the author. The first roster commit is approved by a repo
     admin outside the roster. Optionally roles map to GitHub teams (`pkf-po`, `pkf-ba`, `pkf-architect`, `pkf-de`) and
     the app reads membership from GitHub.
   - The app checks that every gate can be satisfied (e.g. SIT G1 needs a PO, a BA and an Architect who are not the
     author) and names the missing roles.
4. **Who am I**: chosen once from the roster, kept in git-ignored `.pkf-studio/local.yaml`, checked against
   `git config user.email` and the GitHub token's login.

## 4. Gates and approvals

### 4.1 Gates

| # | gate | what is reviewed | approvers in SIT/PPT/PRD | dev |
|---|---|---|---|---|
| G1 | Requirements | original document, `pipelines/<p>.yaml` (finalized requirement + end-to-end examples), pseudocode, the decomposer's split (made in S1, no open questions) | Product Owner, Business Analyst, Architect (three different people) | one person |
| G2 | Design | the split: trigger / source / transformation / destination, settings, end-to-end examples | Data Engineer (Architect optional) | one person |
| G3 | Plans | environment values, each component plan: steps, pseudocode, test cases, simulation | Data Engineer | one person |
| G4 | Code & build | generated code with its tests, a capability summary (imports, AWS calls such as deletes, file/network use), `pkf test pipeline`, the built DAG | Data Engineer | one person |
| G5 | Dev environment | Terraform + the saved plan's text, IAM policy (validated and simulated), deploy manifest, a dry-run listing of what the first run would move/delete | Data Engineer | one person |
| — | Record | ledger rows for G5, the run report: a final PR with no reviewer, merged when `pkf-gate` passes | — | — |

Gates follow the pipeline's lifecycle: G3 is skipped when no component needs a plan (its commits then go into the G4
PR); G4 still runs (the build is code) and shows "no generated code" when there is none; G5 shows "nothing to apply —
policy only" for pipelines that need no AWS resources (e.g. the scheduled S3 move).

### 4.2 Approving

- The app pushes `pkf/<p>/<gate>` and opens a PR into the base branch with what to review, the required roles and how
  to approve.
- **SIT/PPT/PRD: GitHub reviews only.** The latest non-comment review per user (by user id) counts. It must be
  `APPROVED` and on the head commit (`commit_id == head.sha`). A `CHANGES_REQUESTED` on the head from a roster member
  blocks the gate. A person with several roles writes `Approve as: architect` in the review body (this is also the
  meaning of the signature an auditor expects); without it the app assigns roles deterministically.
- **Dev: `/approve <sha7>` comments as well**, because GitHub does not let a PR author approve their own PR. The sha
  must match the head, edited comments are ignored, and the commenter must have write access.
  `/reject <reason>` means changes requested.
- **Authors are excluded in SIT+**: the PR opener and the author and committer of every commit in the PR (the API
  does not name who pushed; the ruleset's "approval of the most recent push" covers that). A commit whose author is
  not linked to a GitHub account blocks the gate.
- An approval stays valid while only base-branch merges are added and this pipeline's files are unchanged, so another
  pipeline's merge does not reset every waiting gate. Branches do not have to be up to date; `pkf-gate` runs on the
  PR's merge result.

### 4.3 Reading back and merging

- Reads: `GET /pulls/{n}`, `/pulls/{n}/reviews`, `/pulls/{n}/commits`, `/issues/{n}/comments` (paginated,
  `per_page=100`), with ETags (304s cost nothing) and back-off on rate limits. They are polled every 30–60 s only
  while a gate is waiting, through a shared cache so several browser tabs make one call.
- When the gate passes and `pkf-gate` is green, the app merges with `PUT /pulls/{n}/merge`, `sha=<evaluated head>`
  (GitHub refuses if the head moved), as a **merge commit**, so the approved commit stays in history. Only same-repo
  branches are merged.
- "Start next stage" is an explicit button. It claims the stage by pushing its branch, and the first push wins, so
  two laptops cannot both advance a pipeline.
- **Each stage job pushes its branch when it ends (0.5.0)**, also when it failed part-way, with a plain push (never a
  force push). A second machine continues the pushed branch: opening a stage fast-forwards this computer's copy when it
  is only behind GitHub's; when both have commits, Workflow shows **GitHub's copy changed** (take GitHub's copy, merge
  it, or — dev only — overwrite it). A push that moves an open PR's head first adds an empty commit, so approvals given
  before the push never cover the new work.
- The UI's GitHub client fails fast (short timeouts, no retries; one network error switches it offline until Retry or
  a minute later) and the app then renders from the local branches; jobs and `pkf studio verify` keep retrying.

### 4.4 Server-side enforcement (SIT/PPT/PRD, required)

The base branch must have:
- a ruleset (or classic protection, §3): PRs required, required approvals, dismiss stale approvals, require approval
  of the most recent push, **require review from code owners**, no force pushes or deletion;
- CODEOWNERS for `.pkf-studio/**`, `.github/**` and `tools/**`, owned by an admin-managed team;
- the required status check **`pkf-gate`**. This is a GitHub Action the app adds through a team PR
  (`.github/workflows/pkf-gate.yml`):
  - it triggers on `pull_request` and `pull_request_review: [submitted, dismissed]`, with the same job name, so the
    check re-evaluates when someone approves;
  - it checks out the **base branch** for the code it runs (the workflow and the framework wheel in `tools/`, whose
    hash is in the ledger). The PR head is checked out only as data;
  - it runs `pkf studio verify --pr <n>`, the same `gates.py` evaluation with the roster from base. That command
    rebuilds every ledger row from the GitHub API and fails on any mismatch, rejects stage PRs that touch
    `.pkf-studio/**`, `.github/**` or `tools/**`, and for G4/G5 runs `pkf test pipeline` and `pkf build --check`
    (generated code runs in the hardened runner of §12);
  - it has `permissions: contents: read, pull-requests: read`, no repository secrets and no AWS credentials, and
    never uses `pull_request_target`.

What stops a stage PR from editing `pkf-gate.yml` itself is the **required code-owner review on `.github/**`**:
GitHub runs `pull_request` workflows from the PR's merge ref, so the base-branch checkout protects the framework code
the check runs, not the workflow file. CODEOWNERS on `.github/**` must therefore never be relaxed.

Merging in the GitHub UI without the right approvals is then impossible, and "tests green" is not self-reported.
Dev mode works without these rules: the app evaluates the gate itself.

### 4.5 The approvals ledger and framework approvals

- After a merge, the next stage branch starts with a ledger commit. `.pkf-studio/approvals/<p>.jsonl` gets one row per
  gate:
  - gate, PR URL, approved head sha, merge sha, mode;
  - the **input hash**: for G1, `pipelines/<p>.yaml`; for G5, the Terraform files and saved plan; for G2–G4, the
    framework digests;
  - the roster blob sha;
  - per approver: GitHub login and id, role, review or comment id, `submitted_at` from GitHub, and the body.

  The next gate reviews this commit, and the final record PR carries G5's row. `verify` re-derives every row from
  GitHub, so deleted comments do not erase evidence and edited rows fail.
- Framework approvals are recorded from the same evidence. New option:
  `pkf approve-plan|approve-code … --by @login --evidence <PR URL>#<review id>`. It records identity
  `github:<login>#<id>`, not the laptop's git email. SIT+ builds run `verify` on that evidence.
- In dev mode, when one person approved everything, approvals are marked `self_asserted: true`, and the artifact
  bundle says so.

### 4.6 Modes

The mode is per repository (project config on the base branch). Lowering it needs the admin code-owner team plus an
Architect who is not the author. A repo in `prd` mode only builds pipelines whose every gate was approved under PRD
rules. Promoting one pipeline across environments (per-environment values, re-review under the target environment's
rules) is the first item after v1.

### 4.7 Rejecting and reopening

- **Changes requested on the open gate.** The user edits the requirement or environment values, or asks the AI again.
  A G3 rejection names the component (`source: …`), so only that component is re-planned. New commits on the same PR
  reset the gate.
- **Reopening an earlier, merged gate** (for example, the plan is wrong, found at G4):
  - the app opens a new PR for that stage from the base branch and closes the later stage PRs;
  - later stages re-derive their state from staleness: framework digests for G2–G4, ledger input hashes for G1 and G5;
  - S6 refuses to run while any gate is reopened or stale, so the old DAG on base is never run as if it were current.

## 5. Stages

```
S1 requirements ─G1─▶ S2 design ─G2─▶ S3 plans ─G3─▶ S4 code & build ─G4─▶ S5 dev environment ─G5─▶ S6 run local ─▶ record PR
```

All work happens in the pipeline's worktree, on the stage branch.

| stage | the app does |
|---|---|
| S1 | Imports the document (§7). The AI requirement analyst writes `pipelines/<p>.yaml`, validated by the pipeline model: `requirement` in the TRIGGER/SOURCE/TRANSFORMATION/DESTINATION/DELIVERY layout, plus end-to-end `examples`. Pseudocode and open questions are shown. `pkf plan pipeline` runs, and its split is committed, so the decomposer's open questions surface before G1. |
| S2 | Writes the ledger row for G1. Reuses the S1 split (it re-runs the split only if the requirement changed during G1). The split is reviewed at G2. |
| S3 | Writes the ledger row. Runs `pkf approve-plan pipeline --evidence …`, which writes the component files. The user fills the `<REQUIRED>` environment values in a form; environment values are not part of what makes a plan stale. Runs `pkf plan` for each component that needs one, with a checkpoint commit after each. |
| S4 | Writes the ledger row and the approvals. Runs `pkf code` where plans have custom steps, then `pkf test pipeline` and `pkf build`. Adds the capability summary. |
| S5 | Writes the ledger row and the approvals. Renders the Terraform. Runs `terraform plan -out`; the binary plan stays local (git-ignored) and its hash and text are committed. Validates the policy. Writes the deploy manifest. Runs `pkf run --dry-run`: the listing and target keys, with the moves and deletes counted, and nothing changed. |
| S6 | After G5. Runs `terraform apply` on the saved plan, only if its hash matches and the account and region are the pinned ones. Runs `pkf run` under the run role (below). Opens the record PR with the G5 ledger row and the run report. |

Re-running a stage skips work that is already current (the framework's statuses say so), reuses the existing branch
and PR, and refuses to start while another job holds the pipeline's lock.

**Run role.** When project config names one, `pkf run` assumes that role with the G5 worker policy as its session
policy, so approved code can do at most what that policy allows. A run role is required in SIT/PPT/PRD and whenever
the team has more than one person; only a single-person dev team may run with the developer's own credentials (after
a warning and confirmation). Two people running the same pipeline locally against the
same bucket or queue bypass `max_active_runs`, so the app warns about it.

## 6. The tabs

Tabs render lazily: `st.tabs(..., key=, on_change="rerun")`, and only the open tab draws. Each tab starts with a
status line saying what it shows, or "available after G1 — open Workflow" (a button that switches tabs from a
callback). Edit widgets have keys and `persist_state="session"`. Drafts are also saved as uncommitted files in the
pipeline's worktree, so nothing is lost when switching tabs, reloading or restarting.

1. **Requirements**
   - Upload (.txt .md .docx .pdf, max 10 MB, deduplicated by SHA-256) or paste. The original is kept as
     `requirements/original/original.<ext>`, with the real name in metadata.
   - The extracted text is shown; it is what the AI sees.
   - AI result: the finalized requirement (`st.text_area` with a markdown preview, refreshed when the field loses
     focus), pseudocode (display only), examples (a YAML text area validated against the example model, because the
     examples are nested), assumptions and open questions.
   - Buttons: Analyze (only on click), Save (commit), Request approval (G1).
2. **Source**
   - Type and connection settings, and the environment-values form.
   - The plan as step cards: operation, parameters, pseudocode, test-case table, simulation.
   - The generated component module first, then a connectivity snippet: a deterministic template per source type,
     e.g. a boto3 S3 client listing the prefix.
3. **Destination** — the same as Source, plus the delivery policy and, with custom delivery, the routing and move/tag
   code.
4. **Trigger**
   - How runs start, in words (the cron explained).
   - The AWS pieces it needs (queue, DLQ, notification), with their Terraform.
   - The trigger module.
5. **Transformation** — the requirement in plain English, and the steps as task cards with the code of each custom
   step. It says "no transformation" when files move unchanged.
6. **Pipeline**
   - A plain-English summary.
   - The diagram: trigger → events → source → package ×N → steps → destination, with operations on the edges. It uses
     the built-in Graphviz rendering (no binary), and the `.dot` file is downloadable.
   - The whole codebase in the file navigator: a searchable path list with `st.code` and line numbers, an
     "Open in VS Code" link (`vscode://file/…`), and large files truncated with a download.
7. **Workflow**
   - The stage list with gate cards (approved by, missing, blocked; PR link) and the live job log.
   - Start, Retry, Cancel and Refresh, the approval history, and Download artifacts.
   - When a job ends, the log fragment calls `st.rerun()` so the other tabs refresh.

**Errors.** Each framework issue is shown as its own message, with the log tail and the raw JSON in an expander.
Exit code 2 means "fix setup" and links to the startup checks.

**Jobs.** Jobs run detached from the console (Windows: `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`), so closing the
Git Bash window does not kill them. Cancel uses `taskkill /T /F`. A pipeline lock is stale when its pid is dead or the
process start time differs, checked with `psutil`, and the app then releases it.

## 7. Documents, secrets and data

- Import runs in a subprocess with a timeout and limits on size and pages. A .docx is checked for uncompressed size
  (zip bombs). Hidden .docx text, comments and tracked changes are flagged. A PDF with no text (scanned) is refused.
- A secret and PII scan runs on the extracted text before anything is committed or sent to the AI. Its findings block
  the step until the user removes them.
- The framework's secret scrubber is defined but called nowhere today. It is now wired into every prompt and every
  log, and every commit the app makes is scanned the same way.
- The app and the docs state that requirement documents and examples must not contain patient or personal data
  (examples are synthetic), and what is sent to Anthropic (extracted text, plans, code) under the organisation's
  agreement.

## 8. The requirement analyst (new AI role)

The new role is `analyze_requirements`, with the skill `finalizing-pipeline-requirements`. It follows the same rules
as the other roles: strict schema, record/replay, a locked-down session, and document text fenced as data.

- **Output:** the `requirement` text in the layout the decomposer handles best, end-to-end examples, pseudocode,
  assumptions and open questions.
- It never invents infrastructure values.
- It writes the pipeline file directly, so there is no second copy of the requirement.

## 9. Framework additions (small)

- `pkf status --json`: per component, its kind, name, stage and mode (today `next` is free text).
- `--evidence` on `approve-plan` and `approve-code`, recording identity `github:<login>#<id>`.
- `pkf run --dry-run`: the planned packages, target keys and counts of moves and deletes, with no delivery.
- The secret scrubber wired into prompts and logs.
- Hardened test runner (§12).

## 10. Infra

Deterministic Terraform from the specs (templates, with golden-file tests):

- **Module per pipeline plus tfvars per environment** (`dev`, `sit`, `ppt`, `prd`). The app applies dev only; for higher
  environments it produces the plan for the platform team.
- **SQS triggers**:
  - queue and DLQ with a redrive policy; `maxReceiveCount` derived from the schedule to cover about 24 h of retries;
    DLQ retention 14 days;
  - queue policy, SQS-managed SSE, and a CloudWatch alarm on DLQ depth;
  - visibility timeout set on the queue and in the trigger's `visibility_timeout_seconds`, longer than the worst-case
    run including task retries.
- **Bucket notifications**:
  - **one stack per bucket** (`infra/buckets/<bucket>`) that merges every pipeline in this repo, with `depends_on` the
    queue policy;
  - a drift check on every plan;
  - if the live configuration has entries this repo does not own (a shared bucket), the app does not touch it. G5
    then produces the merged configuration for the bucket owner or platform team. EventBridge for shared buckets comes
    after v1;
  - scheduled pipelines create no resources.
- **IAM policy document** for the Airflow worker, built from the calls the runtime really makes:
  - `s3:ListBucket` (no prefix condition), `GetObject`, `GetObjectTagging`, `PutObject`, `PutObjectTagging`,
    `DeleteObject` and `AbortMultipartUpload` on the buckets and prefixes the specs use, widened to `bucket/*` with a
    warning when keys are computed;
  - List/Get/Put/Delete on the staging bucket, and SQS Receive/Delete/ChangeMessageVisibility/GetQueueAttributes;
  - KMS Decrypt/GenerateDataKey with `kms:ViaService` when keys are given;
  - well-formedness checked with IAM Access Analyzer `validate-policy`, and sufficiency checked with
    `simulate-custom-policy` against the runtime's action list;
  - attached to a named role only when the user opts in, never to a role matching `*Admin*`.
- **Staging bucket**: an existing input, not created here. The app checks it has lifecycle rules (expiry about 7 days,
  incomplete multipart uploads aborted after 1 day) and the lake's encryption, because it holds copies of clinical
  data.
- **Guards**:
  - account id and region pinned in project config, checked with `sts get-caller-identity`;
  - apply only the saved plan whose hash was approved;
  - destroy, replace and resource types outside an allowlist are refused (so in v1, removing a pipeline leaves its
    resources behind; that is documented);
  - `default_tags`: pipeline, env, owner, repo, managed_by, plus mandatory corporate tags;
  - names `pkf-<env>-<pipeline>`, at most 80 characters.
- **State**:
  - S3 backend with native locking (`use_lockfile = true`);
  - keys `pkf/<env>/<pipeline>.tfstate` and `pkf/<env>/buckets/<bucket>.tfstate`;
  - local state only for a single-person dev team, and never for bucket stacks.
- **Versions**: `required_version >= 1.10`, AWS provider `~> 6.0`. `.terraform.lock.hcl` is committed, generated with
  `terraform providers lock -platform=windows_amd64 -platform=linux_amd64`.
- **Laptop**: Terraform from the zip, checked against SHA256SUMS. `AWS_CA_BUNDLE` is the full bundle; proxy settings
  come from `HTTPS_PROXY` and `NO_PROXY`. A provider `filesystem_mirror` is used when the registry is blocked. There is
  no AWS CLI fallback.

## 11. Artifact bundle

The bundle is built on click (a callable `st.download_button`), from tracked files only (`git ls-files`), with no
symlinks followed and relative names:

```
<pipeline>-artifacts-<timestamp>.zip
  01_requirements/  original/original.<ext>, extracted.txt, pipeline.yaml (finalized requirement + examples), pseudocode.md
  02_design/        split.lock.yaml, components/<kind>/<name>.yaml
  03_plans/         <kind>/<name>.lock.yaml + <kind>/<name>.md (readable plan)
  04_code/          <kind>/<name>.py, dags/pkf_<p>.py, dags/pkf/<p>/** (lock + component modules), test results
  05_infra/         terraform/** (module, tfvars, bucket stacks), plan.txt, iam-policy.json, apply.log
  06_deploy/        deploy manifest, deploy commands
  07_runs/          <run_id>/report.json, dry-run.json, run.log
  workflow/         approvals.jsonl (ledger), logs/<stage>/*.log (scrubbed), llm-calls.jsonl, README.md (dev/self-asserted label when it applies)
```

## 12. AI-written code, test runs, residual risk

The repository policy stays **open**, the default you chose in 0.3.0: approved generated code may import and call
anything. Tests during code generation run candidate code before G4, so the test runner is hardened:
- outbound sockets are blocked except localhost;
- `subprocess` and `os.system` are blocked;
- no credentials are in the environment, HOME is temporary, and there is a time limit;
- AWS calls go to an in-memory S3.

Residual risk: this is not an operating-system sandbox. Candidate code can still read files by absolute path (for
example `%USERPROFILE%\.aws\credentials` or `~/pkf/env.sh`). G4 shows a capability summary, so reviewers see deletes,
network and file access at a glance.

| risk | where | mitigation | signed off by |
|---|---|---|---|
| Data sent to Anthropic | S1–S4 prompts | scan + scrub; no PHI/PII; organisation agreement | security / data owner |
| AI code runs before approval | code-generation tests | hardened runner; no credentials | security |
| Approved code runs with AWS access | S6 local run, Airflow | run role capped by the generated policy; dry run first; reviewed code | Data Engineer + security |
| Dev-mode approvals are by one person | dev repos | labelled self-asserted; SIT+ refuses them | Architect |

Supply chain: the studio dependencies are pinned with hashes, and the framework wheel in `tools/` is built
reproducibly with its hash recorded in the ledger.

## 13. Windows laptop

- Install with `pip install -e '.[runtime,agent,studio]'` (no winget, no Docker).
- `pkf studio` starts Streamlit through the current Python, with `server.address 127.0.0.1`, headless, file watcher
  off, telemetry off and XSRF on.
- The app sets `core.autocrlf false` in the repos it opens, because approved code is fingerprinted.

## 14. Testing

- **Pure functions:**
  - gate evaluation: roles, modes, head sha, authors and committers, multi-role, changes requested, base-only merges;
  - roster feasibility and the mode-lowering rule;
  - stage derivation from framework statuses and ledger input hashes; ledger rebuild from GitHub;
  - Terraform rendering (golden files; `terraform validate` when present);
  - document import limits, the secret scan, the bundle layout, stale locks.
- **GitHub client** against a fake GitHub API: an in-process HTTP server with the endpoints used, ETags and
  pagination.
- **Streamlit `AppTest`** for setup and every tab, including that edits survive tab switches, with an injected fake
  job runner and fake GitHub.
- **End to end:** dev mode, fake GitHub, replayed LLM, moto S3 — from a requirement document to the run report and the
  artifact zip.
- **`pkf studio verify`** in a GitHub Actions-like run, including a PR that edits the workflow (it must be rejected).

## 15. Milestones

1. **GitHub core:** startup checks, project and team from base, gates and ledger, G1 end to end against the fake
   GitHub, and once against a real repository through the proxy.
2. **Server side and lifecycle:** `pkf studio verify` and the `pkf-gate` Action; the job runner; stage derivation;
   framework additions (§9).
3. **Tabs 2–6:** plan cards, code viewer, diagram, environment-values form.
4. **Dev environment:** infra rendering, guards, plan and apply; deploy manifest; dry run; run local; record PR;
   artifact bundle.
5. **Release:** tests, docs, 0.4.0.

## 16. What the reviews changed

| from | change |
|---|---|
| governance, security | <ul><li>GitHub enforces the gates in SIT+: ruleset, code-owner review, required `pkf-gate` check running base-branch code on review events.</li><li>Roster, mode, `.github/` and `tools/` are protected.</li><li>Comments with a sha are allowed in dev only.</li><li>Latest review per user id on the head commit.</li><li>Commit authors and committers are excluded.</li><li>Merge commit with the head sha.</li><li>Ledger with GitHub evidence, rebuilt by `verify`.</li><li>Framework approvals carry `--evidence`.</li><li>Lowering the mode needs an admin plus an Architect.</li></ul> |
| architecture | <ul><li>No state file; stages derived from framework statuses and ledger input hashes.</li><li>Environment values move to S3; the split is made once in S1.</li><li>Record PR for the last commits.</li><li>Reopening rules.</li><li>Worktrees, checkpoints and locks.</li><li>Local backend dropped.</li><li>`verify` moved to milestone 2.</li></ul> |
| UX | <ul><li>Lazy tabs with persisted state and drafts on disk.</li><li>File-based jobs that survive reloads, restarts and a closed console; stale-lock handling.</li><li>Explicit "Start next stage".</li><li>No third-party editor; examples as validated YAML.</li><li>Upload deduplication and Analyze on click; download built on click.</li><li>Plan cards; error display.</li></ul> |
| security | <ul><li>Document scan for secrets and PII before commit or AI; scrubber wired in.</li><li>Hardened test runs and a residual-risk table.</li><li>Run role capped by the generated policy; dry run before the first real run.</li><li>Upload limits; bundle built from tracked files only.</li><li>Token checks; dependencies pinned with hashes.</li></ul> |
| infra | <ul><li>Complete IAM action list including KMS and staging, validated and simulated.</li><li>S3 backend with locking, including bucket stacks.</li><li>One notification stack per bucket with drift checks; shared buckets handed off.</li><li>DLQ and visibility settings derived from the framework.</li><li>tfvars per environment; version pins and a multi-platform lock file.</li><li>Account and region pinned; apply only the saved plan; no destroy.</li><li>No Lambda in v1.</li></ul> |

## 17. Decisions taken (defaults)

| question | decision |
|---|---|
| Git host | GitHub / GitHub Enterprise only in v1 |
| Pipelines per repo | many; one workflow per pipeline, each in its own worktree |
| Who merges | the app, after the gate and `pkf-gate` pass, with a merge commit |
| G2 approver | Data Engineer (Architect optional), as you listed |
| Generated-code policy | open, as you asked; hardened test runs; residual risk stated |
| After v1 | remote Airflow deploy, environment promotion, run_conf Lambda, EventBridge for shared buckets |
