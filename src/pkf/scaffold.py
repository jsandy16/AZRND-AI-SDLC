"""`pkf new <kind> <name>` — a commented spec with every field present: defaults filled, decisions <REQUIRED>."""
from __future__ import annotations

from pathlib import Path

from .envelope import PfError
from .repo import FOLDERS
from .specs.source import SOURCE_TYPES
from .specs.destination import DESTINATION_TYPES

R = "<REQUIRED>"

CREDENTIALS = """credentials:                     # where secret values come from (never the values themselves)
  provider: {provider}               # aws_secrets_manager | vault | local_file | env | none
  ref: {ref}                         # secret id / Vault KV path / file path / env prefix; may use {{env}}
  region: null                       # aws_secrets_manager region (default: worker's region)
  vault_address: null                # default VAULT_ADDR; token from VAULT_TOKEN
  vault_mount: secret                # Vault KV v2 mount
  fields: {{}}                         # connection field -> key in the secret, if names differ"""

EXTRACTION = """extraction:                      # which files form the package (globs on paths relative to root)
  root: "{{input_path}}"               # start folder; templates: {{input_path}} {{ds}} {{ds_nodash}} …
  recursive: true                    # walk every sub-folder
  max_depth: 20                      # folder levels below root (0 = root only, max 100)
  include: ["**"]                    # e.g. ["**/*.csv", "**/*.xpt"]
  exclude: []                        # e.g. ["**/tmp/**"]
  regex: null                        # optional regex the relative path must fully match
  min_age_seconds: 0                 # skip files still being written
  min_files: 1                       # fail the run when fewer files are found
  max_files: null
  max_total_bytes: null
params:                              # default values for template parameters (overridable per pipeline and per run)
  input_path: "{R}"\n"""

SOURCE_CONN = {
    "https": """connection:
  base_url: "{R}"                    # e.g. https://files.cro.example
  listing: html_index                # html_index (autoindex pages) | webdav (PROPFIND) | json_index
  auth: basic                        # none | basic (username, password) | bearer (token)
  verify_tls: true
  ca_bundle: null                    # PEM file for a private CA
  timeout_seconds: 60
  headers: {{}}""",
    "sftp": """connection:
  host: "{R}"
  port: 22
  host_key: "{R}"                    # pinned key, e.g. 'ssh-ed25519 AAAA…' (ssh-keyscan); never skip in production
  insecure_skip_host_key: false
  timeout_seconds: 60""",
    "ftp": """connection:
  host: "{R}"
  port: 21
  tls: explicit                      # explicit (FTPS) | none
  passive: true
  ca_bundle: null
  timeout_seconds: 60""",
    "s3": """connection:
  bucket: "{R}"
  region: null
  endpoint_url: null                 # S3-compatible endpoint (tests, MinIO)""",
    "local": """connection:
  base_path: "{R}"                   # folder on the worker (development only)""",
}
SOURCE_FIELDS = {"https": "username/password (basic) or token (bearer)", "sftp": "username, and password or private_key",
                 "ftp": "username, password", "s3": "optional aws_access_key_id/aws_secret_access_key (default: worker role)",
                 "local": "none"}

DEST_CONN = {"s3": SOURCE_CONN["s3"], "local": SOURCE_CONN["local"]}
DELIVERY = """delivery:
  prefix: "{{source}}/{{ds}}/{{package}}/"   # where the package's files go (templates allowed)
  if_exists: skip_identical          # fail | skip_identical (safe retries) | overwrite
  server_side_encryption: null       # AES256 | aws:kms
  kms_key_id: null
  write_success_marker: false        # write _SUCCESS after all files (in their common folder)"""

DESIGN_NOTE = """# For anything the plain settings cannot express, replace them with a plain-English `requirement:` plus
# `examples:` (the AI plans it; pkf plan|show|approve-plan|code|approve-code {kind} {name}), or with explicit `steps:`
# from `pkf catalog` (no AI)."""


def render(kind: str, name: str, type_: str | None) -> str:
    if kind == "source":
        if type_ not in SOURCE_TYPES:
            raise PfError(f"--type must be one of {list(SOURCE_TYPES)}", "invalid")
        prov = "none" if type_ in ("s3", "local") else "aws_secrets_manager"
        ref = "null" if prov == "none" else f'"rd/{{env}}/{name}"'
        return (f"# Source `{name}` ({type_}). Credential fields: {SOURCE_FIELDS[type_]}.\napi_version: pkf/v1\nkind: source\n"
                f"name: {name}\ntype: {type_}\ndescription: \"{R}\"\n" + SOURCE_CONN[type_].format(R=R) + "\n"
                + CREDENTIALS.format(provider=prov, ref=ref) + "\n" + EXTRACTION.format(R=R)
                + DESIGN_NOTE.format(kind=kind, name=name) + "\n")
    if kind == "destination":
        if type_ not in DESTINATION_TYPES:
            raise PfError(f"--type must be one of {list(DESTINATION_TYPES)}", "invalid")
        return (f"# Destination `{name}` ({type_}).\napi_version: pkf/v1\nkind: destination\nname: {name}\ntype: {type_}\n"
                f"description: \"{R}\"\n" + DEST_CONN[type_].format(R=R) + "\n" + CREDENTIALS.format(provider="none", ref="null") + "\n" + DELIVERY.format() + "\nparams: {}\n"
                + DESIGN_NOTE.format(kind=kind, name=name) + "\n")
    if kind == "transformation":
        return f"""# Transformation `{name}`: describe it in plain English; the AI proposes a plan, the framework shows its effect
# on the examples, a person approves the plan, then the code (pkf plan|show|approve-plan|code|approve-code transformation {name}).
api_version: pkf/v1
kind: transformation
name: {name}
description: "{R}"
requirement: |
  {R}
params: {{}}                         # defaults for parameters the requirement uses, e.g. study: D1234C00001
examples:                            # package structure before/after (paths, optional ':size'); archive members 'x.zip!/a.csv'
  - name: typical
    description: ""
    input: ["{R}"]
    params: {{}}
    expected_output: ["{R}"]
    expect_error: false
"""
    if kind == "trigger":
        return f"""# Trigger `{name}`: how runs start and what each run ingests. Describe it in plain English with examples; the AI
# plans it from the trigger catalogue (schedule, sqs, run_conf, s3_records, filter, set_vars, group …).
# (pkf plan|show|approve-plan|code|approve-code trigger {name}); or write `steps:` instead of `requirement:`.
api_version: pkf/v1
kind: trigger
name: {name}
description: "{R}"
schedule: "*/5 * * * *"              # when the DAG runs (polls); null = only when started by an event, the API or by hand
connection:
  region: null                       # SQS region
  endpoint_url: null
credentials: {{provider: none}}       # optional AWS keys for SQS; default: the worker's role
params:
  queue_url: "{R}"                   # e.g. https://sqs.eu-west-1.amazonaws.com/123456789012/partner-drop-events
requirement: |
  {R}
examples:                            # messages as received → events (object keys + variables)
  - name: typical
    messages: []
    expected_events: []
    expect_error: false
"""
    if kind == "connection":
        return f"""# Connection `{name}`: which source feeds which destination.
api_version: pkf/v1
kind: connection
name: {name}
description: ""
source: "{R}"
destination: "{R}"
check_before_run: true               # first DAG task verifies both ends are reachable
"""
    if kind == "pipeline":
        return f"""# Pipeline `{name}`: a trigger (or a schedule), a connection, an optional transformation. `pkf build {name}` makes the
# Airflow DAG. Alternatively write the whole pipeline as `requirement:` and run `pkf plan pipeline {name}` (the AI splits it).
api_version: pkf/v1
kind: pipeline
name: {name}
description: "{R}"
owner: "{R}"
connection: "{R}"
trigger: null                        # name in triggers/, or null to run on the schedule below
transformation: null                 # name in transformations/, or null to deliver files unchanged
package_name: "{{source}}_{{ds_nodash}}"   # initial package name ({{package}} in templates)
params: {{}}                         # defaults for template parameters, e.g. input_path: /exports/monitoring
schedule: null                       # without a trigger: cron ("0 2 * * *") or preset (@daily); null = manual runs only
start_date: 2026-01-01
task_split: per_step                 # per_step (one Airflow task per transformation step) | single_transform
retries: 2
retry_delay_minutes: 5
max_active_runs: 1
tags: []
examples: []                         # end-to-end: messages/listing → delivered keys (pkf test pipeline {name})
"""
    raise PfError(f"unknown kind {kind}", "invalid")


def new(root: Path, kind: str, name: str, type_: str | None, force: bool = False) -> Path:
    import re

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise PfError("name must be snake_case", "invalid")
    target = root / FOLDERS[kind] / f"{name}.yaml"
    if target.exists() and not force:
        raise PfError(f"{target.relative_to(root)} exists (use --force to overwrite)", "exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(kind, name, type_), encoding="utf-8", newline="\n")
    return target
