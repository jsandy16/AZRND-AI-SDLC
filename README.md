# package-factory

A spec-driven, agent-assisted framework that builds many Airflow 3 ingestion pipelines for **data packages**. A data
package is a set of files that is moved by its structure (paths, sizes, checksums, archives, object keys) and never
opened. Packages come from HTTPS, SFTP, FTP/FTPS, S3 or local folders, and are delivered into the data lake on S3.

A pipeline is made of six YAML components:

| component | answers |
|---|---|
| **trigger** | When does a run start, and what does it ingest? A schedule, S3 events in SQS, a Lambda or REST call. |
| **source** | Where do packages come from, and which files form each package? |
| **transformation** | How does the structure change? Select, check, restructure, zip, checksum, manifest. |
| **destination** | Where does each file go, and under which delivery policy? |
| **connection** | Which source feeds which destination? |
| **pipeline** | How are the parts joined into one Airflow DAG? |

Each of trigger, source, transformation and destination can be given in one of three ways:

- plain **settings**
- hand-written catalogue **steps**
- a plain-English **requirement** with examples

A requirement goes through four agent roles. The **planner** writes catalogue steps, plus custom steps with
pseudocode. The **test writer** writes cases from the pseudocode only. The **coder** writes each custom function; the
framework runs the tests on every attempt and feeds failures back. Before anything runs, a person approves the plan
and its tests, and approves the custom code.

You can also write the **whole pipeline in English**. The **decomposer** agent splits it into the four parts, and a
person approves the split.

Since 0.3.0 generated code may use any library (`[agent] code_policy = "open"`, the default; `"restricted"` brings
back the static guard), and a destination can end with a **`custom_delivery`** step: generated code that delivers the
files itself with boto3 — moves (copy, check, delete the source), server-side copies of objects of any size, object
tags. Its tests run against an in-memory S3 with injected failures.

**pkf Studio (0.6.0)** is a Streamlit front end: a requirement document goes in; a pipeline comes out that has been
approved stage by stage on GitHub, built, deployed to dev and run locally. Each stage is a pull request. The roles
(Product Owner, Business Analyst, Architect, Data Engineer) approve with GitHub reviews. The app merges with the head
commit it evaluated and records every approval in a ledger, which a required check (`pkf-gate`) verifies. Start it
with `pkf studio <repo>`; see [docs/STUDIO.md](docs/STUDIO.md).

0.8.0 is the ST-3 v3 F UI: a light theme with a sidebar (pages, every pipeline, running jobs, the assistant card); the
Workspace shows the stepper, the stage with its substages in a sideways-scrolling row, *Before Request approval*,
*Components* and a dark console. The assistant (Claude Opus 5.5) opens as a pop-up or full screen with a structured
brief of where the run stands and the likely cause, answers while a pipeline job runs, and can change only the
pipeline's own files, never the framework — docs/STUDIO.md §5 and §11, docs/CHANGES-0.8.0.md.

0.6.0 adds the **Workflow Developer** tab (the whole run as an animated tree of stages and substages, driven from one button with inline approvals) and the **assistant** (diagnoses a failed step, proposes a fix, applies it after your approval) — docs/STUDIO.md §10–11.

0.5.0 adds: a draft build and an S4 status panel (every component with its next action), per-step AI coding that
keeps partial work, a push at the end of every stage job, **Preview this run → Move <n> files** for pipelines that move
or delete files (with a per-file `files.csv` report), a read-only offline mode when GitHub cannot be reached, safe
Reopen (set-aside work is kept as `refs/pkf-archive/…`), and recovery cards for a diverged branch, a merge without
approval, a broken worktree and a crashed job. New CLI flags: `pkf build --draft`, `pkf code --step/--all`,
`pkf run --dry-run --plan-out / --only / --max-packages / --log-file` (see [docs/MANUAL.md](docs/MANUAL.md)).

```
check_source → check_destination → read_events → plan_packages
   → package[i] (mapped: extract → t01…tNN → upload) → ack → report
```

## Quick start

```bash
pip install -e '.[runtime,agent]'
export ANTHROPIC_API_KEY=…                   # org-level key: also ANTHROPIC_CUSTOM_HEADERS="anthropic-workspace-id: wrkspc_…"
pkf init specs && cd specs
pkf new pipeline partner_zip_copy           # write `requirement:` (the whole pipeline in English)
pkf plan pipeline partner_zip_copy          # decomposer → review with: pkf show pipeline partner_zip_copy
pkf approve-plan pipeline partner_zip_copy --by @you
pkf status partner_zip_copy                 # the next command for every part, e.g.:
pkf plan trigger partner_drop_sqs        # planner + test writer, simulated on the examples
pkf approve-plan trigger partner_drop_sqs --by @you
pkf code source …  /  pkf approve-code source … --by @reviewer   # only when a plan has custom steps
pkf test pipeline partner_zip_copy          # end-to-end examples
pkf run partner_zip_copy                    # local run with the Airflow runtime code
pkf build --all                             # dags/pkf_<p>.py + dags/pkf/<p>/components/*.py
```

Airflow workers need `pip install 'package-factory[runtime]'` (or the built wheel). Deploy the `dags/` folder.

## Documentation

- [docs/MANUAL.md](docs/MANUAL.md) is the full manual: components, variables, agent roles and lifecycle, the
  decomposer, catalogues, generated files, runtime and SQS semantics, the CLI, configuration and extension.
- `examples/` is a specs repository with four pipelines and their generated DAGs:
  - HTTPS → zip and manifest → S3
  - SFTP → custom control-file check → S3
  - S3 → S3 mirror
  - the event-driven S3 → SQS → S3 zip copy, created from one English requirement
  - `sas_folder_restructure`: every 45 minutes, route each file under `incoming/` by seven path rules and move it
    within the bucket (server-side copy, check, delete, tag), created from one English requirement
- `dryrun/` holds the end-to-end dry run on real Airflow 3 against local stand-ins for HTTPS, WebDAV, SFTP, FTPS,
  S3, SQS, Secrets Manager and Vault.

Out of scope for this version (agreed): UI, security hardening, governance, audit trail.
