# package-factory manual

Version 0.5.0 · spec API `pkf/v1` · Airflow 3.x (verified on 3.3.2, task SDK 1.3.2) · Python ≥ 3.11

## Contents

1. [What the framework does](#1-what-the-framework-does)
2. [How it works end to end](#2-how-it-works-end-to-end)
3. [The specs repository](#3-the-specs-repository)
4. [Component reference](#4-component-reference)
5. [Templates and variables](#5-templates-and-variables)
6. [Designing components with agents](#6-designing-components-with-agents)
7. [Splitting a whole-pipeline requirement](#7-splitting-a-whole-pipeline-requirement)
8. [Catalogues](#8-catalogues)
9. [Building DAGs and what is generated](#9-building-dags-and-what-is-generated)
10. [Runtime behaviour](#10-runtime-behaviour)
11. [Worked example: S3 → SQS → Airflow → S3](#11-worked-example-s3--sqs--airflow--s3)
12. [CLI reference](#12-cli-reference)
13. [Code map](#13-code-map)
14. [Installation and configuration](#14-installation-and-configuration)
15. [Testing and the dry run](#15-testing-and-the-dry-run)
16. [Extending the framework](#16-extending-the-framework)
17. [Troubleshooting](#17-troubleshooting)

---

> **pkf Studio** (the Streamlit front end with GitHub approvals) is described in [STUDIO.md](STUDIO.md). Since 0.4.0 the
> decomposer and the component planner get one repair round: a split that fails validation, or a plan whose examples
> fail in simulation, goes back to the model once with the problems listed. `pkf analyze-requirements` (the requirement
> analyst), `pkf run --dry-run` and `--evidence` on approvals are in the CLI reference.

## 1. What the framework does

Teams need hundreds of ingestion pipelines. Each one collects **data packages** from a file server (HTTPS, SFTP, FTP/FTPS,
S3, local) and delivers them into the data lake (S3). A data package is moved by its structure: paths, sizes,
checksums, archives and object keys. It is never opened.

A pipeline is assembled from six YAML components:

| component | answers |
|---|---|
| **trigger** | When does a run start, and what does it ingest? A schedule, S3 event notifications in SQS, a Lambda or REST call. |
| **source** | Where do packages come from, and which files form each package? |
| **transformation** | How does a package's structure change? Select, check, restructure, zip, checksum, manifest. |
| **destination** | Where does each file go (object key), and under which delivery policy? |
| **connection** | Which source feeds which destination? |
| **pipeline** | How are the parts joined? Package name, parameters, retries. |

Each of trigger, source, transformation and destination can be given in one of three ways:

1. **Settings.** Plain configuration such as `extraction`, `delivery.prefix` or a pipeline `schedule`. No AI is
   involved.
2. **Steps.** Catalogue steps written by hand. No AI is involved.
3. **Requirement.** Plain English plus examples. Agents plan it, test it and code it, and a person approves the plan
   and the custom code.

You can also describe a **whole pipeline in plain English**. The decomposer agent splits it into the four parts, a
person approves the split, and each part is then designed as above.

`pkf build` turns a pipeline into an Airflow 3 DAG. Its tasks are listed in the section on generated files. Every
component becomes a Python module of approved steps and code, which the runtime executes.

Only the decomposition, the planning, the test cases and the custom code use AI. Everything else is deterministic:
validation, simulation, testing, the build and the runtime. The AI's output must match strict schemas and is checked
against your examples. A person approves it before anything runs.

## 2. How it works end to end

```
person                        agents (Claude Agent SDK)          framework (deterministic)              Airflow 3
──────                        ─────────────────────────          ─────────────────────────              ─────────
pipeline requirement ───────▶ DECOMPOSER: trigger/source/        validate every drafted spec
(optional)                    transformation/destination +        
                              examples + end-to-end examples
approve the split ─────────────────────────────────────────────▶ write component files, wire pipeline
                              per component with a requirement:
                              PLANNER: catalogue steps,          validate params, simulate on examples
                              custom steps with pseudocode
                              TEST WRITER: cases per custom      (writes from pseudocode, never sees code)
                              step
approve plan + tests ──────────────────────────────────────────▶ (no custom steps → approved)
                              CODER: function per custom step ◀─ run cases + examples; failures fed back
                                                                 (up to PKF_CODER_ATTEMPTS)
                                                                 file structure, guard, isolated test run
approve code ──────────────────────────────────────────────────▶ approved
                                                                 pkf test pipeline: end-to-end examples
                                                                 pkf build: DAG + component modules ──▶ runs
```

## 3. The specs repository

`pkf init <folder>` creates the folder below. One component goes in each file, and the file name must equal `name:`.

```
pkf.toml                                  marker + settings (staging URL, dags folder)
triggers/<name>.yaml                      sources/<name>.yaml           transformations/<name>.yaml
destinations/<name>.yaml                  connections/<name>.yaml       pipelines/<name>.yaml
<folder>/<name>.lock.yaml                 plan, step tests, simulation, test results, approvals (framework writes)
<folder>/<name>.py                        reviewed custom functions (framework writes; you may edit function bodies)
pipelines/<name>.lock.yaml                the decomposer's split and its approval
dags/                                     generated by pkf build (commit it, deploy it)
```

`pkf.toml`:

```toml
spec_api_version = "pkf/v1"
[staging]
url = "s3://my-airflow-staging/pkf"     # file:///tmp/pkf-staging for development
[airflow]
dags_folder = "dags"
```

## 4. Component reference

`pkf schema --write schemas/` writes JSON Schemas for editor completion. `pkf new <kind> <name> [--type T]` writes a
commented template.

### 4.1 Credentials (sources, destinations, triggers)

```yaml
credentials:
  provider: aws_secrets_manager | vault | local_file | env | none
  ref: rd/{env}/cro_https         # secret id / KV path / file path / env prefix; built-in variables only
  region: eu-west-1               # aws_secrets_manager
  vault_address: https://vault:8200   # vault (or VAULT_ADDR); token from VAULT_TOKEN
  vault_mount: secret
  fields: {username: user}        # field the adapter needs → key in the secret
```

| provider | where values come from |
|---|---|
| `aws_secrets_manager` | `GetSecretValue(ref)`; the SecretString is a JSON object |
| `vault` | KV v2 `GET {address}/v1/{mount}/data/{ref}` with `VAULT_TOKEN` (`VAULT_NAMESPACE` if set) |
| `local_file` | a JSON or YAML file (development only) |
| `env` | environment variables `<ref><FIELD>` |
| `none` | nothing: anonymous access, or the worker's IAM role for AWS |

Credentials are resolved inside the task that needs them. They never appear in specs, locks, XComs or logs.

The fields each adapter reads:

- **https:** `username`/`password` (basic auth) or `token` (bearer)
- **sftp:** `username`, plus `private_key` (with an optional `passphrase`) or `password`
- **ftp:** `username` and `password`
- **s3 and sqs:** optional AWS keys; without them the default AWS chain is used
- **entimice:** `username`, `password`
- **trigger `selfservice`:** `api_key` (the Self-Service Portal api key)

### 4.2 Trigger

```yaml
api_version: pkf/v1
kind: trigger
name: partner_drop_sqs
schedule: "*/5 * * * *"           # when the DAG runs (polls); null = only by hand or the API (run_conf)
connection: {region: eu-west-1, endpoint_url: null}
credentials: {provider: none}
params:
  queue_url: https://sqs.eu-west-1.amazonaws.com/123456789012/partner-drop-events
requirement: |                    # or `steps:` (catalogue steps, no AI)
  Read S3 event notifications from {queue_url}; ignore test events; keep only .zip objects under incoming/ …
examples:
  - name: single_zip
    messages: [{"Records": [{"eventName": "ObjectCreated:Put", "eventTime": "2024-03-01T10:00:00.000Z",
                "s3": {"bucket": {"name": "partner-drop-bucket"}, "object": {"key": "incoming/a.zip", "size": 1}}}]}]
    expected_events: [{"keys": ["incoming/a.zip"], "vars": {"event_date": "2024-03-01"}}]
  - name: test_event_ignored
    messages: [{"Service": "Amazon S3", "Event": "s3:TestEvent"}]
    expected_events: []
```

A pipeline without a trigger uses its own `schedule`, which gives one package per run, as in version 0.1. Trigger
parameters cannot be overridden by a run, so a run can never be pointed at another queue.

### 4.3 Source

```yaml
api_version: pkf/v1
kind: source
name: cro_https
type: https                       # https | sftp | ftp | s3 | local | entimice
connection: {...}                 # by type, below
credentials: {...}
extraction:                       # settings mode: list root, select, settle, limits
  root: "{input_path}"
  recursive: true
  max_depth: 20                   # 0 = root only, max 100; bounds link loops
  include: ["**/*.csv"]
  exclude: []
  regex: null
  min_age_seconds: 600            # skip files still being written
  min_files: 1
  max_files: 5000
  max_total_bytes: null
params: {input_path: /exports/monitoring}
# requirement: | …  + examples   (event-driven keys, control files, one package per folder …)  or  steps: [...]
```

| type | connection fields |
|---|---|
| `https` | `base_url`, `listing: html_index \| webdav \| json_index`, `auth: none \| basic \| bearer`, `verify_tls`, `ca_bundle`, `timeout_seconds`, `headers` |
| `sftp` | `host`, `port`, `host_key` (pinned, required), `insecure_skip_host_key` (development only), `timeout_seconds` |
| `ftp` | `host`, `port`, `tls: explicit \| none`, `ca_bundle`, `passive`, `timeout_seconds` (MLSD required) |
| `s3` | `bucket`, `region`, `endpoint_url` |
| `local` | `base_path` (development and tests) |
| `entimice` | `base_url`, `verify_tls`, `ca_bundle`, `account_by` (template), `accounts` (value → secret ref of that account) |

**Entimice** (a REST document repository): session login (`POST /sessions` with username/password headers),
recursive JSON listing (`GET /system/children`), streamed download (`GET /system?type=file`, gzip decoded). Credential
fields `username`, `password` (the default account). `accounts` maps a value of `account_by` (e.g. the study's
destination, case-insensitive after trimming) to the secret of another account, resolved with the same provider; any
other value uses the default account. Every account the run's packages need is logged in to before any package is
listed (a login failure fails the run); every session is deleted at the end of each task, also on failure. A listing
failure fails only its package. Use it with `delivery.mode: stream`.

HTTPS servers offer three listing modes:

- `html_index`: Apache/nginx autoindex pages. Size and age come from a `HEAD` request per file.
- `webdav`: `PROPFIND` with `Depth: 1`.
- `json_index`: `GET folder/` returns a JSON list.

Redirects are followed only to the same scheme and host.

A file's path relative to its root becomes its path inside the package.

Source examples give the event, a source listing (`"path:size[:age_seconds]"` from the source root), contents for
small control files, and `expected_packages` (the package paths of each package).

### 4.4 Transformation

```yaml
api_version: pkf/v1
kind: transformation
name: csv_zip_bundle
requirement: |
  Keep only CSV files. Every CSV must be directly inside a folder named SITE_ + four digits; refuse otherwise …
params: {study: D1234C00001}
examples:
  - name: typical
    input: ["SITE_0001/visits.csv:1200", "SITE_0002/notes.txt"]   # path[:size]
    params: {ds_nodash: "20240301"}
    expected_output: [D1234C00001_20240301.zip, "D1234C00001_20240301.zip!/SITE_0001/visits.csv", manifest.json]
  - name: file_outside_site_folder
    input: ["summary.csv"]
    expect_error: true
```

### 4.5 Destination

```yaml
api_version: pkf/v1
kind: destination
name: partner_landing
type: s3                          # s3 | local
connection: {bucket: az-rd-lake-raw, region: eu-west-1, endpoint_url: null}
credentials: {provider: none}
delivery:
  prefix: "landing/partner/{event_date}/"   # settings mode: every file at <prefix><package path>
  if_exists: skip_identical       # fail | skip_identical | overwrite
  server_side_encryption: aws:kms # AES256 | aws:kms | null
  kms_key_id: null
  write_success_marker: false     # _SUCCESS in the common folder of the delivered keys
params: {}
# requirement: | …  + examples (files + vars → expected_keys)   or  steps: [route rules …]
```

Delivery policies always come from `delivery`, even when a requirement or steps decide the keys.

- `skip_identical` compares SHA-256 values. Objects carry `pkf-sha256` and `pkf-run-id` metadata.
- **`mode: stream`** (default `staged`): each file is streamed from the source straight into a multipart upload — no
  local copy, no staging, files of any size (`part_size_mb`, default 16, is raised automatically when a file would need
  more than ~9 500 parts; `max_concurrency` parts in parallel, default 8). `content_types` maps extensions to
  Content-Type (`default_content_type` for others); `verify_size` HEADs each object and fails the file when its size
  differs from the Content-Length the source declared. One failing file never stops the others. Needs a source that can
  stream (`entimice`), no transformation, and `if_exists` `overwrite` or `fail`. No metadata or tags are written.
- **A bucket per package:** `connection: {bucket_by: "{study_destination}", bucket_map: {SAS: …, clinicalstudies: …}}`
  instead of `bucket` (exact match after trimming; needs `mode: stream`).
- With `fail`, an object that an earlier attempt of the *same* run already uploaded is accepted.
- The local destination writes each file under a temporary name and then renames it.

**Custom delivery (0.3.0).** When delivery itself must do something the framework's upload does not — a *move*
(delete the source after the copy), a server-side copy of very large objects, object tags, overwrite rules, any other
S3/AWS call — the destination plan ends with one `custom_delivery` step. Its generated, reviewed function is

```python
def move_and_tag(transfers, params, ctx, aws):   # -> results
    # transfers: [{"path", "source_bucket", "source_key", "size", "target_bucket", "target_key"}], keys from the earlier steps
    # aws.s3_source / aws.s3_destination: boto3 S3 clients with each side's region, endpoint and credentials
    # aws.client(service, side="destination"), aws.log
    # return [{"path", "key", "status": "moved" | "copied" | "skipped" | "moved_to_error" | "failed", "detail"}]
```

- Files are **not** downloaded: `extract` only records the package and `upload` calls the function, which copies
  server-side (`aws.s3_destination.copy(...)` is multipart above 8 MiB, so there is no 5 GiB limit).
- A result with status `failed…` fails the package (the run reports it, an SQS message is kept). Any other status counts
  as delivered and appears in the report's `statuses` counts.
- Needs an S3 source and an S3 destination (the same bucket is fine) and no transformation; the build checks this.
- `delivery` policies (`if_exists`, encryption, marker) apply to the framework's upload only; with custom delivery the
  function does what the requirement says.
- Its test cases run against an in-memory S3 (moto), with the S3 state before and after, tags, statuses, and
  `fail_calls` that make a chosen S3 call fail. An entry is `{"operation": "CopyObject", "key": "<key>" | null,
  "code": "AccessDenied", "times": n | null}`: `operation` one of `CopyObject`, `UploadPartCopy`,
  `CreateMultipartUpload`, `HeadObject`, `DeleteObject`, `PutObjectTagging`, `GetObjectTagging`, `PutObject`,
  `GetObject`; `code` one of `AccessDenied` (403, the default), `SlowDown` (503), `InternalError` (500),
  `PreconditionFailed` (412), `NoSuchKey` (404), `ExpiredToken` (400); `times` = how many calls fail (null = every
  call); `key` null = any key.
- **The tests run the function exactly like the runtime (0.5.0):** between the framework's own `precheck` and `verify`
  (with a client without injected failures), with `if_exists` from the destination's `delivery` (default
  `skip_identical`). S3 copies replace the target's tags unless `TaggingDirective` is `REPLACE`; the in-memory S3 is
  corrected to behave the same.
- **The `ctx` contract** is the same in tests and at run time: `ctx = {"params", "run_id", "now", "delivery"}`.
  `ctx["delivery"]` is always present: the destination's `delivery` settings (`prefix`, `if_exists`,
  `server_side_encryption`, `kms_key_id`, …; values may be None). `now` is a fixed time in tests and the wall clock at
  run time. At run time `params` holds the harness's sample variables plus `package`, the package's own variables (e.g.
  a split variable) and **the values earlier layout steps put in `layout["vars"]`** — so `params.get("reason:" +
  t["path"])` gives the rule or error reason an earlier step recorded for a file, and it lands in `files.csv`'s `note`
  column (`reason:<path>` or `note:<path>`).
- S3 keys and package paths are POSIX strings: split and join on `/` (str methods or `posixpath`), never `os.path`,
  `os.sep` or `pathlib` (they differ on Windows).
- Transfers carry the exact listed key and ETag (`CopySourceIfMatch` makes the copy fail if the object was replaced
  since it was listed).

The framework guards against data loss around the generated code (it does not limit what the code may do):

| when | check |
|---|---|
| planning the run | all target keys are computed first; a package is refused (nothing copied or deleted) when two files would get the same key, a target is a listed source object, or a same-bucket target lies under the listed root |
| before the function | a source that is already gone (a retried task) counts as delivered when the target has the listed size, otherwise it fails; `if_exists: fail / skip_identical` against an existing target |
| after the function | exactly one result per file; a status that does not start with `moved`, `copied`, `delivered` or `skipped` is a failure; every file reported delivered is HEADed and must have the listed size |
| build | S3 source and destination, no transformation, `max_active_runs: 1` |

### 4.6 Connection

```yaml
api_version: pkf/v1
kind: connection
name: drop_to_lake
source: partner_drop_event_objects
destination: partner_landing
check_before_run: true            # check_source and check_destination tasks
```

### 4.7 Pipeline

```yaml
api_version: pkf/v1
kind: pipeline
name: partner_zip_copy
owner: clinical-data-eng
requirement: | …                  # optional: the whole pipeline in English (pkf plan pipeline)
connection: partner_zip_copy_connection
trigger: partner_drop_sqs      # or null + `schedule:` below
transformation: null              # or a transformation name
package_name: "{event_key_stem}"
params: {}
schedule: null                    # only without a trigger
start_date: 2026-10-01
task_split: per_step              # per_step | single_transform
retries: 2
retry_delay_minutes: 5
max_active_runs: 1
tags: [partner]
examples:                         # end to end: messages/conf + source listing → every delivered key
  - name: zip_in_subfolder
    messages: [ … S3 event … ]
    listing: ["incoming/studyA/batch1.zip:123"]
    expected_keys: ["landing/partner/2024-03-01/studyA/batch1.zip"]
```

## 5. Templates and variables

Templates use `{name}` and are strict: an unknown variable is an error. These variables are available:

| variables | from |
|---|---|
| `pipeline source destination connection trigger package run_id ds ds_nodash ts_nodash env` | built-ins; nothing can override them |
| parameter defaults | source < trigger < transformation < destination < pipeline `params` |
| run configuration | Airflow "Trigger DAG w/ config" or `pkf run --param`. Declared parameters only; trigger parameters excluded. |
| `event_bucket event_key event_key_dir event_key_name event_key_stem event_date event_date_nodash event_ts_nodash` | trigger step `s3_records`, per event |
| `set_vars` names, `group` var, `split` var | trigger and source steps |
| `path dir name stem suffix` and regex groups | per-file, in operations that say so (`move`, `route`, `checksum` sidecars) |

The guard rails are:

- Parameter names cannot shadow built-ins.
- `credentials.ref` may use built-ins only.
- `{package}` exists only once the package exists.
- Object keys and prefixes may not contain `..`.

`ds` is the run's logical date; manual runs use `run_after`.

## 6. Designing components with agents

### 6.1 Roles

| role | sees | returns (strict schema) | skill |
|---|---|---|---|
| decomposer | whole-pipeline requirement, catalogues, settings/example formats, existing components | four component drafts + end-to-end examples, assumptions, open questions | `decomposing-pipeline-requirements` |
| planner | one component: requirement, catalogue, settings, examples, API | ordered steps (catalogue or custom with pseudocode), assumptions, open questions | `planning-pipeline-components` |
| test writer | a custom step's pseudocode, signature, case format — **never code** | 3-8 cases (input → expected output or refusal) | `writing-step-tests` |
| coder | approved pseudocode, approved cases, each example's state at that step | one function; fixed from its own failing results, up to `PKF_CODER_ATTEMPTS` (default 4) | `writing-custom-step-code` |

Each role runs in an isolated Agent SDK session with these properties:

- no settings and no tools other than its own skill
- the requirement is passed as delimited data
- budget, turn and time limits per role (`PKF_LLM_MODEL_<ROLE>`, `PKF_LLM_MAX_TURNS_<ROLE>` …)
- record and replay, so tests and CI are reproducible

### 6.2 Lifecycle and stages

```
pkf plan <kind> <name> → pkf show → pkf approve-plan <kind> <name> --by @you
                       → pkf code <kind> <name> → pkf test → pkf approve-code <kind> <name> --by @reviewer
```

| stage | meaning → next |
|---|---|
| `needs_plan` | `pkf plan` |
| `plan_stale` | requirement, examples, parameter names or type changed → plan again |
| `plan_tampered` | the lock was edited → plan again |
| `plan_proposed` | review `pkf show`, then a person approves the plan *and its test cases* |
| `plan_approved` | `pkf code` (only when the plan has custom steps) |
| `code_generated` | review the `.py` file, then a person approves the code |
| `code_changed` | the code file changed → `pkf test`, approve again |
| `code_failing` | fix the function bodies or plan again |
| `approved` | ready to build |

What a plan depends on: the requirement, the examples, the parameter *names* and the component type. The planner
sees parameter names, never their values, so plans refer to environment values (bucket names, queue URLs, endpoints,
credentials) through templates such as `{queue_url}`. Those values can then differ between dev and prod without
planning again. A value written into the requirement text itself is part of the plan: changing that text means
planning again. Put per-environment values in `params`, not in the text.

### 6.3 What protects the code

- **Approvals.** Only a person approves (`--by @handle`, plus the git identity). Approval is refused inside agent
  sessions (`CLAUDECODE` or `PKF_AGENT_SESSION`).
- **Exact structure.** The code file may hold only: the module docstring, `from __future__ import annotations`, one
  function per custom step (its name and positional arguments as the plan says, no decorators) and `CUSTOM = {...}`
  naming exactly the custom steps. Comments, blank lines, trailing spaces and CRLF do not matter (the check compares
  syntax trees). Anything else is refused with "only the bodies of the custom step functions may be edited; other code
  was added or changed (nest helper functions and imports inside the step function, or regenerate with pkf code <kind>
  <name>)". The coder is told the same: "Answer with exactly one function. Put helper functions and imports INSIDE it";
  a second top-level function gives "expected exactly one function named <fn>; put helpers and imports inside it".
- **Code policy** (`pkf.toml` `[agent] code_policy`, or `PKF_CODE_POLICY` for one command):
  - `open` (the default since 0.3.0): generated code may import and call anything (`boto3`, `os`, …). The checks
    below are skipped except the exact structure and the signature; the code is tested, reviewed and approved by people,
    and promoted to higher environments only after that.
  - `restricted`: the static guard below.
  The policy used is recorded in the component lock (`code.code_policy`).
- **Static guard** (restricted policy). It checks:
  - allowed imports (`string` is not one of them)
  - forbidden names and private attributes
  - module members such as `gzip.open`
  - format-field attribute walks: `.format` is allowed only on a literal, and `format_map`, `get_field` and
    `vformat` are banned

  Guard results are stored in the lock and block approval. Code that raises is treated as refusing its package or
  event: the refusal never crashes the whole run or loops on retries.
- **Restricted inputs.** Custom functions receive data (events, groups, layouts) or a restricted package view. The
  only exception is the source's `SourceView`, which can list, stat and read small control files. No custom
  function receives local paths or clients.
- **Isolated tests.** Examples and cases run in a separate `python -I -X utf8` process with a minimal environment, a
  temporary HOME, proxies pointing nowhere and a timeout. Results are written to a file as each case finishes (what the
  code prints never mixes with them). The time limit is `PKF_TEST_TIMEOUT_S`, else 60 s + 10 s per delivery case + 2 s
  per other case or example (at least 120 s); on a timeout the finished results are kept, the case in progress fails
  with "timed out after <T>s while running <step>:<case>" and the rest with "not run (an earlier case timed out)". The
  child must load the same package-factory as `pkf` itself; otherwise every result is "the test runner loaded
  package-factory <v> from <path>, this is <v'> from <path'>: reinstall with pip install -e <this folder> in the venv
  you run pkf from". This is not an operating-system sandbox. Candidate code has passed the
  guard, but it runs before anyone reviews it: run `pkf code` on a machine that holds no secrets beyond the
  Anthropic key.
- **Fingerprinted modules and lock.** The deployed component modules are fingerprinted in the lock (SHA-256). The
  worker executes exactly the bytes it checked. The lock carries its own digest, which is verified when the DAG
  loads, so an edited lock or module never runs.

### 6.4 Writing good requirements and examples

- Say what to keep, what to refuse and when, the target layout, and the names.
- Give one example per rule, including refusals and edge cases (URL-encoded keys, test events, duplicates).
- Answer open questions by editing the requirement, then plan again. Do not argue with the plan.

## 7. Splitting a whole-pipeline requirement

```bash
pkf new pipeline partner_zip_copy        # write `requirement:` instead of connection/trigger
pkf plan pipeline partner_zip_copy       # decomposer → pipelines/partner_zip_copy.lock.yaml
pkf show pipeline partner_zip_copy       # settings, requirement and examples per part; to-fill-in list
pkf approve-plan pipeline partner_zip_copy --by @you [--force]
pkf status partner_zip_copy              # the next command for every part
```

When you approve the split, the framework:

1. writes the component files. Files this pipeline's earlier splits created are rewritten, keeping every
   `<REQUIRED>` value you have since filled in (bucket names …).
2. removes drafts an earlier split of this pipeline created that the new split no longer uses (renamed components),
   unless another pipeline uses them
3. writes `connections/<pipeline>_connection.yaml`. An existing connection keeps its other settings; only its two ends
   follow the split.
4. sets the pipeline's `connection`, `trigger`, `transformation` and `package_name`. Your own end-to-end `examples`
   are kept (the decomposer sees them and must be consistent with them); `--force` replaces them with the split's.

The framework refuses when:

- a split has errors or open questions
- a component name is not snake_case
- a file belongs to another pipeline or was written by hand
- `package_name` uses event variables while the trigger is a plain schedule

Any value the text did not give stays `<REQUIRED>` until you fill it in, and `pkf validate` lists it. A missing value
is never an open question. Components owned by other pipelines can be reused unchanged.

## 8. Catalogues

`pkf catalog` lists every operation and parameter for all four kinds.

**Trigger**

| op | does |
|---|---|
| `schedule` | receive: one event per scheduled run |
| `sqs` | receive: up to `max_messages` (≤ 1000); `visibility_timeout_seconds` must exceed a run's duration; `delete_unmatched`; `max_receives` (only for queues without a dead-letter queue) |
| `run_conf` | receive: messages in `dag_run.conf[key]` (Lambda, REST API, manual) |
| `selfservice` | receive: one message per submission of a Self-Service Portal form (`base_url`, `form_id`, credential field `api_key`, which must start with `dpls_`, be ≥ 20 characters, no spaces); after the run, `ack` writes each due record's result back to it (see §11.3) |
| `selfservice_due` | keep the submissions due for a refresh (refreshSchedule Once/Daily/Weekly/Monthly, lastTransferStatus, lastTransferAt; `force`); the others are counted per reason in the report |
| `data_vars` | variables from the event's data by dotted path, trimmed (e.g. `study_id: submission_data.studyId`) |
| `parse_json`, `unwrap_sns` | message body → data; SNS envelope → inner message |
| `s3_records` | one event per record; key URL-decoded (`+` is a space); `s3:TestEvent` dropped; `event_*` variables |
| `filter` | bucket (template), key globs/regex, size |
| `set_vars`, `group`, `dedupe`, `require` | variables from templates; several events → one package; one event per object (by sequencer); max events |
| `custom` | `(events, params, ctx) -> events` |

**Source**

| op | does |
|---|---|
| `list` | every file under a root (template), recursive, depth-limited; `keep_root` keeps the root in the package paths (full source paths); `blank_root` refuses the package with a message when the root is blank |
| `from_event` | exactly the objects the event names; keys must be under `root`, which is removed from paths |
| `select`, `min_age`, `latest`, `limits` | globs/regex; settled files only; newest N; counts and bytes |
| `control_file` | keep only what a small control file lists (lines, CSV column, JSON list) |
| `split` | one package per folder or regex key; sets a variable |
| `custom` | `(groups, params, ctx, src) -> groups` |

**Transformation:** `select require move flatten prefix strip_folders rename_package checksum manifest zip unzip gzip gunzip custom` (unchanged from 0.1; see `pkf catalog`).

**Destination**

| op | does |
|---|---|
| `prefix` | `<prefix>/<package path>` for files not yet placed |
| `route` | rules: globs/regex → key template, or `null` to not deliver; first match wins |
| `check` | ordered checks of variables (`not_blank`, `one_of`); the first that fails refuses the package with its `error` message before any file is delivered |
| `prefix_by` | `<prefix>/<package path>`, the prefix chosen by a variable's value (`prefixes`, `default`) |
| `custom` | `(layout, params, ctx) -> layout` |
| `custom_delivery` | last step only: `(transfers, params, ctx, aws) -> results` — the function delivers the files itself (moves, server-side copies, tags) |

## 9. Building DAGs and what is generated

`pkf build <pipeline>` (or `--all`) refuses unless every component is approved and every pipeline example passes end
to end. Warnings never fail a build (exit 0 unless there are errors): trigger params named `retries`/`retry_delay*`
(`inert_trigger_param`: they do nothing, set `retries` / `retry_delay_minutes` in `pipelines/<p>.yaml`), a `*/N`
minute cron with `60 % N != 0` (`uneven_cron`), and `file://` staging (`file_staging`: every Airflow worker must reach
the same staging, set `PKF_STAGING_URL=s3://…`).

`pkf build <p> --draft` (0.5.0) runs every check of the real build — spec errors, every pipeline example end to end with
the current code, the custom-delivery invariants, the DAG compiles — and also accepts components whose code is
generated and tested but **not approved yet** (`code_generated`). It writes nothing: `data.files` is `[]`,
`data.would_write` lists what a build would write and `data.unapproved_code` the components that still need code
approval. The Studio uses it at S4; `dags/` only ever holds approved code. `--draft --check` is a usage error.

A real build then writes:

| artefact | content |
|---|---|
| `dags/pkf_<p>.py` | the DAG; thin, with no logic. Schedule from the trigger, a `Param` per run parameter (trigger parameters excluded), retries, owner, tags. |
| `dags/pkf/<p>/components/{trigger,source,transformation,destination}.py` | the approved steps (`STEPS`), the approved custom functions and `CUSTOM` |
| `dags/pkf/<p>/pipeline.lock.json` | resolved specs (credential *references* only), each component module's SHA-256, staging URL, versions |
| `dags/.airflowignore` | makes sure `pkf/**` is present |

The DAG's tasks are:

```
check_source → check_destination → read_events → plan_packages
   → package[i] (mapped task group: extract → t01…tNN | transform → upload)
   → ack (trigger_rule all_done) → report (all_done)
```

Generated files are deterministic and compiled before they are written. `pkf build --all --check` is the CI gate,
and `--all` also removes the DAGs of deleted pipelines.

## 10. Runtime behaviour

| task | does |
|---|---|
| `check_source`, `check_destination` | resolve credentials and connect |
| `read_events` | receive messages (schedule tick, SQS, run configuration), run the trigger steps, record events in staging. Messages that produce no event (test events, filtered keys) are deleted at once. |
| `plan_packages` | run the source steps per event; each resulting group is one package. An event whose source steps refuse is recorded as failed; the others go on. |
| `package[i].extract` | download the package's files, verify sizes, stage `p<i>/00_extract` (custom delivery: nothing is downloaded) |
| `package[i].tNN_<step>` | the transformation step(s) |
| `package[i].upload` | the destination steps give each file's key; deliver with the policies; record the result. With `custom_delivery`: build the transfers and call the function; one log line per file (`source -> target [status] detail`) |
| `ack` | SQS: delete the messages whose packages were all delivered. Messages with a failed event or package are released at once (visibility 0): the next run reads them again, and after the queue's `maxReceiveCount` SQS moves them to the dead-letter queue. Without a dead-letter queue, set the trigger's `max_receives`; a warning is logged. |
| `report` | summary. It fails the run when any event or package failed (after the others were delivered and acknowledged) or when `ack` did not finish. On success it clears staging. |

**Local runs (`pkf run`, 0.5.0).**

- `--dry-run` lists every file's target and changes nothing (for custom delivery up to 5000 rows in `dry_run.sample`,
  `sample_truncated` when there are more). With `--plan-out FILE` it also writes `planned.json` with **every** planned
  file:
  ```json
  {"version": 1, "pipeline": "p", "created_at": "2026-10-18T09:00:00Z", "run_id": "local__20261018T090000",
   "params": {}, "date": null, "logical_date": "2026-10-18T09:00:00+00:00", "max_packages": 10,
   "delivery": "custom_delivery",
   "files": [{"package": "…", "index": 0, "path": "…", "source_key": "…", "etag": "…", "size": 10, "target_key": "…"}]}
  ```
- `--only FILE` (custom-delivery pipelines only) moves exactly the files of a `planned.json`, with its `run_id`,
  `params` and date (`date`, else `logical_date`), so the targets are the previewed ones even on another day. A file
  whose ETag or target changed since the preview, or that is new, is left for the next run (`excluded_since_preview`);
  planned files that are gone are counted (`missing_since_preview`); empty packages are dropped and their SQS messages
  released. `--only` with `--param`, `--date`, `--run-id` or `--dry-run`, or on a framework-upload pipeline, is a
  usage error (exit 2).
- `--max-packages N` caps the packages of this run (`0` = no cap beyond the framework's 1000); the others wait for the
  next run, their messages released, not failed.
- `--log-file FILE` writes INFO logging of the `pkf` loggers to FILE (UTF-8, LF) as it goes; without it INFO goes to
  stderr (WARNING with `--json`).
- The result's `data` has `tasks`, `files` and `summary` on success **and** on failure. `files` has one row per planned
  file: `{"package", "index", "path", "source_key", "target_key", "status", "detail", "note", "size"}`; `status` is what
  the delivery reported (`moved`, `moved_to_error`, `uploaded`, `skipped …`, `failed`), `not_delivered` (the layout
  routed the file nowhere), `failed` (package error, or refused at planning) or `not_attempted` (the run stopped);
  `note` is the `reason:<path>` / `note:<path>` value an earlier layout step set. The Studio writes these rows to
  `review/<p>/runs/<run>/files.csv` (header `package,index,path,source_key,target_key,status,detail,note,size`).
- A package that fails with a network error is tried once more from extract; other packages go on. One bad or duplicate
  key fails only that file. When AWS credentials expire mid-run the run stops attempting packages ("AWS credentials
  expired after <k> of <n> packages: log in again (aws sso login) and run again; delivered files are recognised by the
  next run"). `report.failed_packages` names `<package>#<index> (<first file path>)`.
- A worker that cannot find the run plan in staging fails with "the run plan is missing in staging (<url>): every
  Airflow worker must reach the same staging (set PKF_STAGING_URL=s3://…)". `file://` staging URLs need a drive on
  Windows: `file:///C:/path` (not `/c/path`).

These failures fail a task at once, without retries:

- `PackageError`: layout, limits, refused package, different content at a key
- `CredentialError`
- `PermissionError`: 401/403, untrusted TLS, cross-host redirect, host key, login, S3 access, code not the approved code
- `SourceError`
- `RunFailed`: the report

Network errors, timeouts and unavailable secret stores use the task's retries. Retries are safe: stages are
rewritten, uploads skip identical objects, and a re-run with the same run id starts clean.

SQS delivery is at least once. Duplicates are harmless because identical objects are skipped and `dedupe` handles
duplicate events. With `if_exists: fail`, a duplicate notification fails its package, so use `skip_identical` for
event-driven pipelines.

The guard rails on SQS runs are:

- An SQS-triggered pipeline must have `max_active_runs: 1`; the build refuses otherwise.
- Set a visibility timeout longer than a run takes.
- A run plans at most 1000 packages, below Airflow's `max_map_length`. Messages beyond that are released for the next
  run, not failed.
- Each event's `message_ids` belong to the framework: custom trigger steps must keep them, otherwise their event is
  refused. Source and destination steps cannot change which messages are acknowledged.
- A re-planned run clears earlier results, so stale results are never trusted.

## 11. Worked example: S3 → SQS → Airflow → S3

The requirement in `examples/pipelines/partner_zip_copy.yaml`:

> When a file is placed in the source bucket under incoming/, an S3 event notification sends it to SQS. A DAG every 5
> minutes reads the events and copies each zip, unchanged, to the destination bucket under
> landing/partner/<event date>/, keeping its path below incoming/. Ignore non-zips and test events; delete a message
> only after its zip is delivered; skip identical objects.

The decomposer split it into four parts:

- **trigger `partner_drop_sqs`:** `sqs` → `parse_json` → `unwrap_sns` → `s3_records` →
  `filter(bucket, incoming/**/*.zip)` → `set_vars`
- **source `partner_drop_event_objects`:** `from_event(root incoming/)`
- **no transformation**
- **destination `partner_landing`:** settings only, `prefix: landing/partner/{event_date}/`

The dry run on Airflow 3.3.2 checks these behaviours:

- two zips are delivered in two mapped branches
- the test event and the `.txt` notification are acknowledged
- a poison message is released and read again (the run is red), then moved to the dead-letter queue by SQS
- the next run is a green no-op
- a re-uploaded identical zip is skipped

### 11.2 Worked example: scheduled move inside one bucket (SAS folder restructure)

`examples/pipelines/sas_folder_restructure.yaml` reproduces an existing hand-written pipeline from its requirement:
every 45 minutes, every file under `incoming/` is routed by seven path rules (exactly one must match, otherwise
`error/`) and **moved** within the same bucket: server-side copy, HEAD the target, delete the source, replace the tags
with `ADDITIONAL_TAG = <original path>`. The decomposer split it into:

- **trigger `every_45_minutes`:** settings only, `schedule: "*/45 * * * *"`
- **source `sas_incoming`:** `list(incoming/)` → `select` (a study folder and a file extension) → `split` (one package per file)
- **no transformation**
- **destination `sas_restructure_move`:** custom `compute_target_keys` (the rules) → `custom_delivery` `move_and_tag`

Checked against the original implementation: the generated routing agrees with it on more than 800 000 generated
paths except two cases the requirement decides differently (an upper-case `IA` suffix in anonymised folders unless
the requirement says case-sensitive, and an `ar` folder directly holding the file, which the original turns into a
broken key and the generated code sends to `error/`). The dry run moves real objects (one of 20 MiB, multipart) in a
moto S3 server and checks tags, deletes, `error/` and skipped files.

Worker permissions for a move: `s3:ListBucket`, `s3:GetObject`, `s3:GetObjectTagging`, `s3:PutObject`,
`s3:PutObjectTagging`, `s3:DeleteObject` on the bucket.

### 11.3 Worked example: Self-Service control list → Entimice → S3, with write-back (`entimice_api`)

`examples/pipelines/entimice_api.yaml` implements `examples/requirements/entimice_api_pipeline_requirement.txt` with
catalogue steps only (no generated code):

- **trigger `entimice_selfservice`:** `schedule: "0 6,18 * * *"`; `selfservice` (the form's submissions) →
  `parse_json` → `selfservice_due` (`force: "{force_refresh}"`, a pipeline run parameter) → `data_vars`
  (`submission_id`, `study_id`, `study_location`, `study_destination`)
- **source `entimice_studies`:** `type: entimice`, the clinicalstudies account by destination; `list` the study's
  location (`keep_root`, `max_depth: 20`, `blank_root: Missing location in submission_data`)
- **no transformation**
- **destination `entimice_datalake`:** a bucket per destination (`bucket_map`), `mode: stream`, `if_exists: overwrite`,
  Content-Types by extension; `check` (destination blank, unknown, studyId blank) → `prefix_by` (SAS →
  `incoming/<studyId>/`, clinicalstudies → `<studyId>/ongoing/`, else `<studyId>/`)
- **connection:** `check_before_run: false` (a run with no study due touches neither Entimice nor S3); pipeline
  `max_active_runs: 1`

With a `selfservice` trigger the run's outcome is recorded in the portal: `ack` decides each due study's result by the
requirement's rules (listing failed → no files → configuration → failed files → nothing copied → DONE), applies the
consecutive-failure limit (Once 10, Daily 15, kept in the record's error text) and PUTs the record's `submission_data`
with only the result fields changed. A failed write-back is logged and never fails the run; failed studies do not fail
the run either. The run fails when the control list cannot be read, the api key is invalid, an Entimice login fails, or
something fails outside a single study. The report has one line per due study and the not-due counts per reason.

`python dryrun/entimice_run.py` runs it end to end against local stand-ins for Entimice, the Self-Service API and Vault
(`dryrun/entimice_servers.py`) and a moto S3 (`AWS_ENDPOINT_URL`); `tests/test_entimice_pipeline.py` covers the rules.

## 12. CLI reference

`--json` (before the command) prints one result envelope. Exit codes: 0 ok, 1 problems, 2 usage or environment
error.

| command | purpose |
|---|---|
| `pkf init [path]` | create a specs repository |
| `pkf new <trigger\|source\|destination\|transformation\|connection\|pipeline> <name> [--type T]` | commented template |
| `pkf validate [--allow-unfilled]` | models, references, templates, design status |
| `pkf list`, `pkf catalog`, `pkf schema [--write DIR]` | components, catalogues, JSON Schemas |
| `pkf plan <kind\|pipeline> <name>` | planner (or decomposer) |
| `pkf show <kind\|pipeline> <name>` | plan, test cases and results (or the split) for review |
| `pkf approve-plan <kind\|pipeline> <name> --by @h [--force] [--evidence URL --github-id N --head-sha S]` | a person approves (Studio: with the GitHub review or comment as evidence) |
| `pkf code <kind> <name> [--step ID]... [--all]` | coder with the framework's test loop. Default: only the custom steps whose function is missing or failing (all when none is); `--step` exactly those steps; `--all` every step. The file is written after each step, so what the coder wrote is kept when it stops part-way ("the AI coder stopped at step <id>: …") |
| `pkf test <kind> <name>` / `pkf test pipeline <name>` | all examples and cases (a component without generated code: its examples) / end-to-end examples |
| `pkf approve-code <kind> <name> --by @h [--evidence …]` | a person approves custom code |
| `pkf analyze-requirements TEXT_FILE --name P [--owner O] [--source-file F]` | requirement analyst: document text → `pipelines/P.yaml` + pseudocode |
| `pkf transform <action> <name>` | compatibility alias for transformations |
| `pkf status [<pipeline>]` | the next command for every part not ready |
| `pkf build [<p> \| --all] [--check \| --draft]` | generate DAGs and component modules; `--draft` checks everything with tested but unapproved code and writes nothing |
| `pkf check <p> [--log-file F]` | live connectivity |
| `pkf run <p> [--date D] [--param k=v] [--conf JSON] [--run-id R] [--keep-staging] [--dry-run [--plan-out F]] [--only F] [--max-packages N] [--log-file F]` | local run of the whole task flow with the Airflow runtime (see *Local runs* in §10) |
| `pkf studio [REPO] [--port N]` | the Studio app on 127.0.0.1 (extra `studio`) |
| `pkf studio verify --pr N [--base-dir D --head-dir D]` | the `pkf-gate` check (GitHub Action) |

## 13. Code map

```
pkf (authoring)                                          pkf_runtime (workers; no dependency on pkf)
cli.py ─┬─ repo.py, specs/ (trigger, source, …, design)   tasks.py   check_*, read_events, plan_packages, extract,
        ├─ decompose.py  decomposer + split approval                  transform, upload, ack, report, run_all
        ├─ design.py     plan/tests/code/approve, guard,  events.py      trigger catalogue (receive + event steps)
        │                isolated runner                  discovery.py   source catalogue, SourceView, FakeSource
        ├─ llm/          Agent SDK roles, record/replay   ops.py         transformation catalogue
        │   └─ steps-plugin/skills (4 roles)              layout.py      destination catalogue
        ├─ build.py      lock, component modules, DAG     simulate.py    examples, step cases, whole pipelines
        └─ scaffold.py   templates                        components.py  module render + SHA-checked load
                                                          sources/, destinations.py, credentials.py, staging.py, package.py
```

## 14. Installation and configuration

| where | install |
|---|---|
| authoring | `pip install 'package-factory[runtime,agent]'`, then set `ANTHROPIC_API_KEY`. The Agent SDK bundles Claude Code. For an org-level key, also set `ANTHROPIC_CUSTOM_HEADERS="anthropic-workspace-id: wrkspc_…"`. |
| Airflow workers | `pip install 'package-factory[runtime]'` (or the wheel) |
| CI | `pip install 'package-factory[dev]'`, then `pytest`, `pkf validate`, `pkf build --all --check` |

| variable | meaning |
|---|---|
| `PKF_ENV` | `{env}` |
| `PKF_STAGING_URL`, `PKF_STAGING_ENDPOINT_URL` | staging override; S3-compatible endpoint (tests) |
| `PKF_AWS_ENDPOINT_URL`, `PKF_SQS_ENDPOINT_URL` | Secrets Manager / SQS endpoints (tests) |
| `PKF_KEEP_STAGING` | keep stages after success |
| `VAULT_TOKEN`, `VAULT_ADDR`, `VAULT_NAMESPACE` | Vault |
| `PKF_LLM_PROVIDER` | `agent-sdk` (default) / `record` / `replay` |
| `PKF_LLM_MODEL[_<ROLE>]`, `PKF_LLM_MAX_TURNS[_<ROLE>]`, `PKF_LLM_MAX_BUDGET_USD[_<ROLE>]`, `PKF_LLM_TIMEOUT_S[_<ROLE>]` | per role: `DECOMPOSE_PIPELINE`, `PLAN_COMPONENT`, `WRITE_STEP_TESTS`, `WRITE_CUSTOM_STEP` |
| `PKF_CODER_ATTEMPTS` | coder test/fix rounds per custom step (default 4) |
| `PKF_CODE_POLICY` | `open` / `restricted` for one command (default: `pkf.toml` `[agent] code_policy`, else `open`) |
| `PKF_LLM_FIXTURES`, `PKF_PLUGIN_ROOT` | fixture folder; plugin location |
| `PKF_TEST_TIMEOUT_S` | time limit of one isolated test run (default: 60 s + 10 s per delivery case + 2 s per other case or example, at least 120 s) |
| `PKF_LLM_SCRIPT`, `PKF_LLM_SCRIPT_LOG` | `PKF_LLM_PROVIDER=script` (tests): the answers file, and a JSONL log of every call with its full redacted prompt |
| `PKF_LLM_FIXTURE_TRACE`, `PKF_LLM_REKEY` | tests: log every replay fixture hit; map a replay miss to the fixture recorded before a listed prompt change |
| `CLAUDE_CODE_USE_BEDROCK`, `PKF_LLM_AWS_PASSTHROUGH` | `1`: the AI steps keep the `AWS_*` variables (Claude through Amazon Bedrock) |
| `NODE_EXTRA_CA_CERTS` | the corporate CA for the AI agent (Node.js); set it to `$REQUESTS_CA_BUNDLE` behind a TLS proxy |

## 15. Testing and the dry run

`pytest` runs:

- unit tests of every catalogue
- the guard and the package view
- the variables layering
- the moto S3 → SQS → S3 pipeline in-process
- custom delivery in-process against moto: move, tags, error prefix, refused collisions, failed files, retries, the code policy
- the CLI flows with replayed agents: split, plan with test writer, coder, approvals refused for agents, tamper
  detection, builds

**Scripted AI answers (tests).** `PKF_LLM_PROVIDER=script` with `PKF_LLM_SCRIPT=<file>` answers every AI call from a
hand-written JSON file — no recording, no key, and prompt changes never invalidate it:

```json
{"version": 1, "answers": [
   {"task": "write_custom_step", "when": "Write the custom step `move_and_tag`", "times": 1, "output": {...}},
   {"task": "write_custom_step", "when": "`move_and_tag`", "raise": "llm_limit", "message": "budget"},
   {"task": "plan_component", "when": "Component kind: destination", "output": {...}}]}
```

The first entry with the same `task` whose `when` (a plain substring, optional) occurs in the redacted prompt and that
is not used up (`times`, default unlimited) answers; use counts persist in `<file>.state.json`. No match →
`script: no answer for <task> …` (`llm_error`); `raise` entries raise that error code. The output is validated like a
model's answer.

**Re-keying replay fixtures.** When a listed prompt change (`pkf.llm.base.PROMPT_CHANGES_0_5`) changes the key of a
recorded fixture, never re-record: `python tests/rekey_fixtures.py` runs the replay tests with `PKF_LLM_REKEY=1`, copies
each fixture recorded before the change under its new key (`provenance.rekeyed_from`) and prints old → new; commit the
new files. CI never sets `PKF_LLM_REKEY`, so a missing fixture is still an error there.

`dryrun/` holds the end-to-end check:

- it starts stand-ins: HTTPS (autoindex, WebDAV, JSON) with TLS, SFTP, FTPS, Vault, and a moto server for S3,
  Secrets Manager and SQS, with S3 notifications to SQS
- it builds 9 DAGs and runs them with `airflow dags test` on Airflow 3.3.2
- it checks deliveries, queue state, refusals, fail-fast errors and the SAS move (copy, delete, tags, multipart, error/, skipped files) — 47 checks

## 16. Extending the framework

- **New operation for any kind.** Add a parameter model and a function to that kind's catalogue: `events.py`,
  `discovery.py`, `ops.py` or `layout.py`. Planning, simulation, tests, code generation and the runtime pick it up.
  When the same custom step keeps appearing, promote it into the catalogue.
- **New receive type for triggers.** Add a receive op, plus its branch in `tasks._receive` and in
  `simulate.trigger_events`. An Airflow AssetWatcher on SQS was tried and left out. It deletes messages on reception
  (at most once), and it needs the `common.messaging` provider.
- **New source or destination type.** Add an adapter. The `check`, `list`, `download`, `close` methods are required;
  `stat` is optional and makes `from_event` faster. Then add the model, the scaffold and access-error mapping.
- **New credential provider.** Add it in `credentials.resolve` and to the `Credentials.provider` literal.

## 17. Troubleshooting

| symptom | cause / fix |
|---|---|
| validate: template uses unknown variables | add a parameter, or plan the trigger/source that provides it (it is a warning until they are planned) |
| build refused: component not ready | `pkf status <pipeline>` gives the next command |
| build refused: pipeline examples fail | `pkf test pipeline <p>` shows expected vs produced keys |
| `plan_stale` after editing a value | only requirement, examples, parameter names or type make a plan stale; check what changed |
| run failed: `n event(s) failed` | poison or malformed messages are released and go to the DLQ after `maxReceiveCount`; the others were delivered |
| `PermissionError … pipeline.lock.json was edited` | rebuild from the specs; never edit files under `dags/pkf/` |
| messages processed twice | visibility timeout shorter than the run; raise `visibility_timeout_seconds` |
| `PermissionError … not the approved … code` | a file in `dags/pkf/` was edited; rebuild from the specs |
| TLS, host key, credentials | see `PermissionError` / `CredentialError` messages; nothing is retried |
| `n of m file(s) failed delivery` | a custom delivery returned `failed` for those files (the log has one line per file with the reason); their sources stay in place for the next run |
| `custom delivery tests need boto3 and moto` | `pip install 'package-factory[agent]'` on the authoring machine |
| `only the bodies of the custom step functions may be edited` | code outside the step functions was added or changed (a top-level helper or import); nest it inside the step function, or regenerate with `pkf code <kind> <name> --step <id>` |
| `the AI coder stopped at step <id>` | the steps before it are kept; `pkf code <kind> <name>` continues with the missing ones |
| `timed out after <T>s while running <step>:<case>` | the case hangs (a loop, a network call); fix the code, or raise `PKF_TEST_TIMEOUT_S` deliberately |
| `the test runner loaded package-factory … from …` | the venv's installed package-factory is another tree: `pip install -e <this folder>` in the venv you run `pkf` from |
| `--only applies to custom-delivery pipelines` | use a plain `pkf run`; preview/move is for pipelines that move or delete files |
| `the run plan is missing in staging` | set `PKF_STAGING_URL=s3://…` on every worker |
| build warning `inert_trigger_param` / `uneven_cron` / `file_staging` | see §9 |
