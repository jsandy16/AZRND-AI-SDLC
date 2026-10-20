"""Deterministic Terraform for a pipeline (templates, no AI), the worker IAM policy, and guarded plan / apply.

infra/<p>/module/            the pipeline's resources: SQS queue + DLQ (redrive), queue policy, DLQ alarm, worker policy
infra/<p>/envs/<env>/        main.tf (backend, provider pinned to the account, default tags), terraform.tfvars.json
                             dev only: plan.txt and plan.sha256 of the saved plan (the binary plan stays git-ignored)
infra/<p>/iam-policy.json    what the Airflow worker needs for this pipeline (from the calls the runtime makes)
infra/<p>/manifest.json      the facts the files were rendered from, warnings, and what needs applying
infra/buckets/<bucket>/      one S3 → SQS notification stack per bucket, merging every pipeline of this repo

Scheduled pipelines need no AWS resources: only the policy and the manifest are written ("nothing to apply").
A queue_url that does not name the Studio's queue (pkf-<env>-<pipeline>) is an existing queue: not created here, and
its bucket notification is left to the queue's owner.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import STUDIO_DIR, VERSION

ENVS = ("dev", "sit", "ppt", "prd")
ALLOWED_TYPES = {"aws_sqs_queue", "aws_sqs_queue_policy", "aws_cloudwatch_metric_alarm", "aws_iam_policy",
                 "aws_s3_bucket_notification"}
TF_REQUIRED = (1, 10)


class InfraError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------- facts
def resource_name(env: str, pipeline: str) -> str:
    return f"pkf-{env}-{pipeline.replace('_', '-')}"[:76]


def _static_prefix(template: str) -> str:
    """The literal part of a key template or glob before its first variable or wildcard ('incoming/{ds}' → 'incoming/')."""
    m = re.search(r"[{*?\[]", template or "")
    return (template or "")[: m.start()] if m else (template or "")


def schedule_minutes(schedule: str | None) -> int:
    s = (schedule or "").strip()
    presets = {"@hourly": 60, "@daily": 1440, "@midnight": 1440, "@weekly": 10080}
    if s in presets:
        return presets[s]
    m = re.fullmatch(r"\*/(\d+) \* \* \* \*", s)
    if m:
        return max(1, int(m.group(1)))
    m = re.fullmatch(r"\d+ \*/(\d+) \* \* \*", s)
    if m:
        return 60 * max(1, int(m.group(1)))
    if re.fullmatch(r"\d+ \* \* \* \*", s):
        return 60
    return 60


def max_receive_count(schedule: str | None) -> int:
    """About 24 hours of retries at the pipeline's cadence (each run receives a message at most once)."""
    return max(3, min(1000, 1440 // schedule_minutes(schedule)))


def _fmt(template: str, params: dict) -> str:
    try:
        return re.sub(r"\{(\w+)\}", lambda m: str(params.get(m.group(1), m.group(0))), template or "")
    except Exception:  # noqa: BLE001
        return template or ""


def _has_key(node, key: str) -> list:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and v:
                out.append(v)
            out += _has_key(v, key)
    elif isinstance(node, list):
        for v in node:
            out += _has_key(v, key)
    return out


def facts(wt: Path, pipeline: str, env: str, region: str, account: str | None) -> dict:
    """What the Terraform and the policy are rendered from — read from the approved specs and plans."""
    from pkf.build import parts_for
    from pkf.repo import load, settings

    repo = load(Path(wt))
    p = repo.get("pipeline", pipeline)
    parts = parts_for(repo, pipeline)
    trig = parts["trigger"]
    tspec, tsteps = trig["spec"], trig["steps"]
    recv = tsteps[0]
    tparams = {**(tspec.get("params") or {})}
    warnings: list[str] = []
    out: dict = {"pipeline": pipeline, "env": env, "region": region, "account_id": account, "schedule": tspec.get("schedule") or p.schedule,
                 "receive": recv["op"], "queue": None, "notifications": [], "warnings": warnings}
    src, dst = parts["source"]["spec"], parts["destination"]["spec"]
    custom = any(s["op"] == "custom_delivery" for s in parts["destination"]["steps"])
    out["source"] = {"type": src.get("type"), "bucket": (src.get("connection") or {}).get("bucket"),
                     "prefix": _static_prefix(_fmt((src.get("extraction") or {}).get("root", ""), src.get("params") or {})).lstrip("/")}
    out["destination"] = {"type": dst.get("type"), "bucket": (dst.get("connection") or {}).get("bucket"), "custom_delivery": custom}
    out["kms_keys"] = sorted({str(k) for k in _has_key({"s": src, "d": dst}, "kms_key_id")})
    staging = settings(repo.root)["staging"]["url"]
    out["staging"] = staging if staging.startswith("s3://") and not custom else None
    if recv["op"] == "sqs":
        rp = recv.get("params") or {}
        url = _fmt(rp.get("queue_url", "{queue_url}"), tparams)
        m = re.match(r"^https://sqs\.([a-z0-9-]+)\.amazonaws\.com/(\d{12})/([A-Za-z0-9_-]{1,80})$", url)
        if not m:
            raise InfraError(f"the trigger's queue_url is not an SQS queue URL: {url!r}")
        q_region, q_account, q_name = m.groups()
        managed = q_name == resource_name(env, pipeline)
        if q_region != region:
            warnings.append(f"the queue is in {q_region}, the project's region is {region}")
        if account and q_account != account:
            raise InfraError(f"the queue is in account {q_account}, the project is pinned to {account}")
        out["queue"] = {"url": url, "name": q_name, "arn": f"arn:aws:sqs:{q_region}:{q_account}:{q_name}", "managed": managed,
                        "visibility_timeout_seconds": int(rp.get("visibility_timeout_seconds", 1800)),
                        "max_receive_count": max_receive_count(out["schedule"])}
        if not managed:
            warnings.append(f"queue {q_name} is not {resource_name(env, pipeline)}: it is treated as an existing queue (not created; "
                            "its bucket notification is up to its owner)")
        else:
            flt = next((s for s in tsteps if s["op"] == "filter"), None)
            fp = (flt or {}).get("params") or {}
            bucket = fp.get("bucket") or out["source"]["bucket"]
            inc = fp.get("include") or []
            prefix = _static_prefix(inc[0]) if len(inc) == 1 else ""
            m2 = re.search(r"\*(\.[A-Za-z0-9]+)$", inc[0]) if len(inc) == 1 else None
            if bucket:
                out["notifications"].append({"bucket": bucket, "queue_arn": out["queue"]["arn"], "prefix": prefix,
                                             "suffix": m2.group(1) if m2 else "", "pipeline": pipeline})
            else:
                warnings.append("no bucket found for the S3 event notification (the trigger's filter names none)")
    return out


# ---------------------------------------------------------------------------------------------------------- policy
def worker_policy(f: dict) -> tuple[dict, list[tuple[str, str]]]:
    """(IAM policy document, [(action, resource)] the runtime calls — what simulate-custom-policy checks)."""
    st: list[dict] = []
    calls: list[tuple[str, str]] = []

    def add(sid, actions, resources, cond=None):
        s = {"Sid": sid, "Effect": "Allow", "Action": sorted(actions), "Resource": sorted(set(resources))}
        if cond:
            s["Condition"] = cond
        st.append(s)
        calls.extend((a, r) for a in actions for r in resources[:1])

    src, dst = f["source"], f["destination"]
    buckets = {b for b in (src.get("bucket") if src.get("type") == "s3" else None, dst.get("bucket") if dst.get("type") == "s3" else None) if b}
    if buckets:
        add("ListBuckets", ["s3:ListBucket"], [f"arn:aws:s3:::{b}" for b in sorted(buckets)])
    if src.get("type") == "s3" and src.get("bucket"):
        acts = ["s3:GetObject", "s3:GetObjectTagging"] + (["s3:DeleteObject"] if dst.get("custom_delivery") else [])
        add("ReadSource", acts, [f"arn:aws:s3:::{src['bucket']}/{src.get('prefix') or ''}*"])
    if dst.get("type") == "s3" and dst.get("bucket"):
        add("WriteDestination", ["s3:PutObject", "s3:PutObjectTagging", "s3:GetObject", "s3:GetObjectTagging", "s3:AbortMultipartUpload"],
            [f"arn:aws:s3:::{dst['bucket']}/*"])
        f["warnings"].append(f"destination keys are computed, so the policy allows writing anywhere in {dst['bucket']}")
    if f.get("staging"):
        b, _, pre = f["staging"][5:].partition("/")
        add("StagingList", ["s3:ListBucket"], [f"arn:aws:s3:::{b}"])
        add("Staging", ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"], [f"arn:aws:s3:::{b}/{pre}*"])
    if f.get("queue"):
        add("Queue", ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], [f["queue"]["arn"]])
    if f.get("kms_keys"):
        add("Kms", ["kms:Decrypt", "kms:GenerateDataKey"],
            [k if k.startswith("arn:") else f"arn:aws:kms:{f['region']}:{f['account_id'] or '*'}:key/{k}" for k in f["kms_keys"]],
            {"StringEquals": {"kms:ViaService": [f"s3.{f['region']}.amazonaws.com"]}})
    return {"Version": "2012-10-17", "Statement": st}, calls


# ---------------------------------------------------------------------------------------------------------- HCL
MODULE_MAIN = """# Rendered by pkf Studio {version} — do not edit; change the specs and render again.
terraform {{
  required_version = ">= 1.10"
  required_providers {{
    aws = {{ source = "hashicorp/aws", version = "~> 6.0" }}
  }}
}}

variable "name" {{ type = string }}
variable "create_queue" {{ type = bool }}
variable "visibility_timeout_seconds" {{ type = number }}
variable "max_receive_count" {{ type = number }}
variable "sender_bucket_arns" {{ type = list(string) }}
variable "account_id" {{ type = string }}
variable "worker_policy_json" {{ type = string }}
variable "create_worker_policy" {{
  type    = bool
  default = false
}}
variable "alarm_actions" {{
  type    = list(string)
  default = []
}}

resource "aws_sqs_queue" "dlq" {{
  count                     = var.create_queue ? 1 : 0
  name                      = "${{var.name}}-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}}

resource "aws_sqs_queue" "queue" {{
  count                      = var.create_queue ? 1 : 0
  name                       = var.name
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({{
    deadLetterTargetArn = aws_sqs_queue.dlq[0].arn
    maxReceiveCount     = var.max_receive_count
  }})
}}

resource "aws_sqs_queue_policy" "queue" {{
  count     = var.create_queue && length(var.sender_bucket_arns) > 0 ? 1 : 0
  queue_url = aws_sqs_queue.queue[0].id
  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Sid       = "S3Notifications"
      Effect    = "Allow"
      Principal = {{ Service = "s3.amazonaws.com" }}
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.queue[0].arn
      Condition = {{
        ArnLike      = {{ "aws:SourceArn" = var.sender_bucket_arns }}
        StringEquals = {{ "aws:SourceAccount" = var.account_id }}
      }}
    }}]
  }})
}}

resource "aws_cloudwatch_metric_alarm" "dlq" {{
  count               = var.create_queue ? 1 : 0
  alarm_name          = "${{var.name}}-dlq-not-empty"
  alarm_description   = "Messages the pipeline could not process (see the DAG's failed runs)"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = {{ QueueName = aws_sqs_queue.dlq[0].name }}
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
}}

resource "aws_iam_policy" "worker" {{
  count       = var.create_worker_policy ? 1 : 0
  name        = "${{var.name}}-worker"
  description = "What the Airflow worker needs for this pipeline (pkf Studio)"
  policy      = var.worker_policy_json
}}

output "queue_url" {{ value = var.create_queue ? aws_sqs_queue.queue[0].url : null }}
output "queue_arn" {{ value = var.create_queue ? aws_sqs_queue.queue[0].arn : null }}
output "dlq_arn" {{ value = var.create_queue ? aws_sqs_queue.dlq[0].arn : null }}
output "worker_policy_arn" {{ value = var.create_worker_policy ? aws_iam_policy.worker[0].arn : null }}
"""

ENV_MAIN = """# Rendered by pkf Studio {version} — environment `{env}` of pipeline `{pipeline}`.
terraform {{
  required_version = ">= 1.10"
  required_providers {{
    aws = {{ source = "hashicorp/aws", version = "~> 6.0" }}
  }}
  backend "{backend}" {{}}
}}

variable "region" {{ type = string }}
variable "account_id" {{ type = string }}
variable "tags" {{ type = map(string) }}
variable "name" {{ type = string }}
variable "create_queue" {{ type = bool }}
variable "visibility_timeout_seconds" {{ type = number }}
variable "max_receive_count" {{ type = number }}
variable "sender_bucket_arns" {{ type = list(string) }}
variable "worker_policy_json" {{ type = string }}
variable "create_worker_policy" {{ type = bool }}

provider "aws" {{
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {{ tags = var.tags }}
}}

module "pipeline" {{
  source                     = "../../module"
  name                       = var.name
  create_queue               = var.create_queue
  visibility_timeout_seconds = var.visibility_timeout_seconds
  max_receive_count          = var.max_receive_count
  sender_bucket_arns         = var.sender_bucket_arns
  account_id                 = var.account_id
  worker_policy_json         = var.worker_policy_json
  create_worker_policy       = var.create_worker_policy
}}

output "queue_url" {{ value = module.pipeline.queue_url }}
output "queue_arn" {{ value = module.pipeline.queue_arn }}
output "worker_policy_arn" {{ value = module.pipeline.worker_policy_arn }}
"""

BUCKET_MAIN = """# Rendered by pkf Studio {version} — S3 event notifications of bucket `{bucket}` for every pipeline in this repository.
# Terraform owns the bucket's WHOLE notification configuration: a bucket with entries this repository does not own is
# never planned (the Studio writes handoff.json for the bucket's owner instead).
terraform {{
  required_version = ">= 1.10"
  required_providers {{
    aws = {{ source = "hashicorp/aws", version = "~> 6.0" }}
  }}
  backend "{backend}" {{}}
}}

variable "region" {{ type = string }}
variable "account_id" {{ type = string }}
variable "tags" {{ type = map(string) }}

provider "aws" {{
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {{ tags = var.tags }}
}}

resource "aws_s3_bucket_notification" "this" {{
  bucket = {bucket_json}
{queues}}}
"""

QUEUE_BLOCK = """  queue {{
    id            = {id}
    queue_arn     = {arn}
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = {prefix}
    filter_suffix = {suffix}
  }}
"""


def _j(v) -> str:
    return json.dumps(v)  # a JSON string literal is a valid HCL string literal


def tags_for(env: str, pipeline: str, owner: str, repo_slug: str, extra: dict) -> dict:
    return {**{k: str(v) for k, v in (extra or {}).items()}, "pipeline": pipeline, "env": env, "owner": owner, "repo": repo_slug,
            "managed_by": "pkf-studio"}


def backend_for(env: str, backend: dict) -> str:
    if backend.get("bucket"):
        return "s3"
    if env == "dev":
        return "local"
    return "s3"


def render(wt: Path, pipeline: str, f: dict, owner: str, repo_slug: str, extra_tags: dict, backend: dict,
           all_notifications: list[dict] | None = None) -> dict:
    """Write infra/<p>/ (and the bucket stacks it needs). Returns the manifest."""
    wt = Path(wt)
    base = wt / "infra" / pipeline
    policy, calls = worker_policy(f)
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    (base / "iam-policy.json").write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8", newline="\n")
    q = f.get("queue")
    needs_resources = bool(q and q["managed"])
    manifest = {"rendered_by": f"pkf-studio {VERSION}", "facts": f, "needs_apply": needs_resources, "runtime_calls": calls,
                "buckets": sorted({n["bucket"] for n in f["notifications"]}), "warnings": f["warnings"]}
    if needs_resources:
        (base / "module").mkdir()
        (base / "module" / "main.tf").write_text(MODULE_MAIN.format(version=VERSION), encoding="utf-8", newline="\n")
        senders = sorted({f"arn:aws:s3:::{n['bucket']}" for n in f["notifications"]})
        for env in ENVS:
            d = base / "envs" / env
            d.mkdir(parents=True)
            be = backend_for(env, backend)
            (d / "main.tf").write_text(ENV_MAIN.format(version=VERSION, env=env, pipeline=pipeline, backend=be), encoding="utf-8", newline="\n")
            if be == "s3":
                (d / "backend.hcl").write_text(
                    f"bucket       = {_j(backend.get('bucket', '<REQUIRED: state bucket>'))}\n"
                    f"key          = {_j(f'pkf/{env}/{pipeline}.tfstate')}\nregion       = {_j(backend.get('region', f['region']))}\n"
                    "use_lockfile = true\n", encoding="utf-8", newline="\n")
            dev = env == "dev"
            tfvars = {"region": f["region"], "account_id": f["account_id"] if dev else f"<REQUIRED: {env} account id>",
                      "tags": tags_for(env, pipeline, owner, repo_slug, extra_tags), "name": resource_name(env, pipeline),
                      "create_queue": True, "visibility_timeout_seconds": q["visibility_timeout_seconds"],
                      "max_receive_count": q["max_receive_count"],
                      "sender_bucket_arns": senders if dev else [f"<REQUIRED: {env} source bucket ARN>"],
                      "worker_policy_json": json.dumps(policy, sort_keys=True) if dev else f"<REQUIRED: the {env} worker policy>",
                      "create_worker_policy": False}
            (d / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    else:
        manifest["note"] = "nothing to apply — policy only" if not q else "the queue is an existing one: nothing to apply"
    for b in manifest["buckets"]:
        render_bucket(wt, b, f, owner, repo_slug, extra_tags, backend, all_notifications or f["notifications"])
    (base / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8", newline="\n")
    return manifest


def render_bucket(wt: Path, bucket: str, f: dict, owner: str, repo_slug: str, extra_tags: dict, backend: dict,
                  notifications: list[dict]) -> Path:
    d = Path(wt) / "infra" / "buckets" / bucket
    d.mkdir(parents=True, exist_ok=True)
    mine = sorted([n for n in notifications if n["bucket"] == bucket], key=lambda n: n["pipeline"])
    blocks = "".join(QUEUE_BLOCK.format(id=_j(f"pkf-{n['pipeline']}"), arn=_j(n["queue_arn"]), prefix=_j(n["prefix"]), suffix=_j(n["suffix"]))
                     for n in mine)
    be = backend_for("dev", backend)
    if be == "local":
        be = "s3"  # never local state for a shared resource
    (d / "main.tf").write_text(BUCKET_MAIN.format(version=VERSION, bucket=bucket, bucket_json=_j(bucket), queues=blocks, backend=be),
                               encoding="utf-8", newline="\n")
    (d / "backend.hcl").write_text(f"bucket       = {_j(backend.get('bucket', '<REQUIRED: state bucket>'))}\n"
                                   f"key          = {_j(f'pkf/dev/buckets/{bucket}.tfstate')}\n"
                                   f"region       = {_j(backend.get('region', f['region']))}\nuse_lockfile = true\n", encoding="utf-8", newline="\n")
    (d / "terraform.tfvars.json").write_text(json.dumps({"region": f["region"], "account_id": f["account_id"],
                                                         "tags": tags_for("dev", f"bucket-{bucket}", owner, repo_slug, extra_tags)},
                                                        indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return d


def repo_notifications(wt: Path) -> list[dict]:
    """Every pipeline's notifications rendered in this repository (so a bucket stack merges all of them)."""
    out = []
    for m in sorted((Path(wt) / "infra").glob("*/manifest.json")):
        try:
            out += json.loads(m.read_text(encoding="utf-8"))["facts"]["notifications"]
        except (OSError, ValueError, KeyError):
            continue
    return out


# ---------------------------------------------------------------------------------------------------------- terraform
def terraform_bin() -> str | None:
    return os.environ.get("PKF_TERRAFORM") or shutil.which("terraform")


def terraform_version(tf: str) -> tuple[int, int]:
    out = subprocess.run([tf, "version", "-json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    try:
        v = json.loads(out.stdout)["terraform_version"]
    except (ValueError, KeyError):
        m = re.search(r"v(\d+)\.(\d+)", out.stdout)
        if not m:
            raise InfraError("could not read the Terraform version") from None
        return int(m.group(1)), int(m.group(2))
    a, b, *_ = v.split(".")
    return int(a), int(b)


def _tf_env(cwd: Path) -> dict:
    """Terraform's working data (.terraform, providers) in a short per-user folder: Windows paths stay under 260 characters."""
    data = Path(os.environ.get("PKF_TF_DATA", Path.home() / ".pkf-tf")) / hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:12]
    data.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1", "TF_DATA_DIR": str(data)}


def _tf(tf: str, args: list[str], cwd: Path, log, timeout: int = 1800) -> subprocess.CompletedProcess:
    log(f"$ terraform {' '.join(args)}   (in {cwd.name})")
    env = _tf_env(cwd)
    p = subprocess.run([tf, *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env, encoding="utf-8", errors="replace")
    for ln in (p.stdout + p.stderr).splitlines()[-60:]:
        log("  " + ln)
    return p


def plan_guard(plan_json: dict) -> list[str]:
    """What a saved plan may not do: destroy, replace, or touch resource types outside the allowlist."""
    problems = []
    for rc in plan_json.get("resource_changes", []):
        acts = rc.get("change", {}).get("actions", [])
        if rc.get("type") not in ALLOWED_TYPES:
            problems.append(f"{rc.get('address')}: resource type {rc.get('type')} is not allowed")
        if "delete" in acts:
            problems.append(f"{rc.get('address')}: the plan would {'replace' if 'create' in acts else 'destroy'} it (refused)")
    return problems


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def plan_dir(root: Path, rel: str) -> Path:
    d = Path(root) / STUDIO_DIR / "terraform" / rel
    d.mkdir(parents=True, exist_ok=True)
    return d


def plan_stack(root: Path, stack: Path, rel: str, log, local_state: bool) -> dict:
    """terraform init + plan -out; writes plan.txt and plan.sha256 next to main.tf; returns the summary."""
    tf = terraform_bin()
    if not tf:
        raise InfraError("Terraform is not installed (download it from the HashiCorp zip, check SHA256SUMS, put it on PATH)")
    if terraform_version(tf) < TF_REQUIRED:
        raise InfraError("Terraform 1.10 or newer is needed (S3 native state locking)")
    pdir = plan_dir(root, rel)
    init = ["init", "-input=false", "-no-color"]
    if local_state:
        init.append(f"-backend-config=path={(pdir / 'terraform.tfstate').as_posix()}")
    elif (stack / "backend.hcl").exists():
        init.append("-backend-config=backend.hcl")
    if _tf(tf, init, stack, log).returncode != 0:
        raise InfraError(f"terraform init failed in {stack}")
    plan_file = pdir / "dev.tfplan"
    p = _tf(tf, ["plan", "-input=false", "-no-color", "-var-file=terraform.tfvars.json", f"-out={plan_file.as_posix()}"], stack, log)
    if p.returncode != 0:
        raise InfraError(f"terraform plan failed in {stack}")
    show = _tf(tf, ["show", "-no-color", plan_file.as_posix()], stack, lambda m: None)
    (stack / "plan.sha256").unlink(missing_ok=True)
    (stack / "plan.txt").write_text(show.stdout, encoding="utf-8", newline="\n")
    plan_json = _plan_json(tf, stack, plan_file)
    problems = plan_guard(plan_json)
    if problems:
        plan_file.unlink(missing_ok=True)
        raise InfraError("the plan is refused: " + "; ".join(problems))
    digest = _sha_file(plan_file)
    (stack / "plan.sha256").write_text(digest + "\n", encoding="utf-8", newline="\n")  # only a plan that passed the guard can be approved
    changes = [(rc["address"], rc["change"]["actions"]) for rc in plan_json.get("resource_changes", [])
               if rc.get("change", {}).get("actions") not in (["no-op"], ["read"])]
    return {"stack": str(stack), "plan_sha256": digest, "changes": changes}


def _plan_json(tf: str, stack: Path, plan_file: Path) -> dict:
    """`terraform show -json` of a saved plan; fails closed (an unreadable plan is never applied)."""
    js = subprocess.run([tf, "show", "-json", plan_file.as_posix()], cwd=str(stack), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                        env=_tf_env(stack))
    try:
        doc = json.loads(js.stdout) if js.returncode == 0 else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or "resource_changes" not in doc and "planned_values" not in doc:
        raise InfraError("could not read the saved plan (terraform show -json failed); nothing is applied")
    return doc


def foreign_notifications(s3, bucket: str, ours: set[str]) -> list[str]:
    """Entries of the bucket's live notification configuration this repository does not own."""
    cfg = s3.get_bucket_notification_configuration(Bucket=bucket)
    out = []
    for key, arn_key in (("QueueConfigurations", "QueueArn"), ("TopicConfigurations", "TopicArn"),
                         ("LambdaFunctionConfigurations", "LambdaFunctionArn")):
        for e in cfg.get(key, []):
            if e.get(arn_key) not in ours:
                out.append(e.get(arn_key))
    if "EventBridgeConfiguration" in cfg:
        out.append("EventBridge")
    return out


def aws_identity(region: str, account: str | None) -> tuple[bool, str, dict | None]:
    """`checks.aws_identity` (WP3, E13): (ok, message, identity); never raises."""
    from . import checks

    return checks.aws_identity(region, account)


def check_identity(account: str | None, region: str) -> dict:
    """The caller's identity, or InfraError with the fix (no or expired credentials, another account)."""
    ok, msg, ident = aws_identity(region, account)
    if not ok:
        raise InfraError(msg)
    return ident


NO_CREDENTIALS = ("NoCredentialsError", "TokenRetrievalError", "SSOTokenLoadError", "UnauthorizedSSOTokenError",
                  "PartialCredentialsError", "ProfileNotFound", "SSOError", "CredentialRetrievalError",
                  "ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "RequestExpired")


def credentials_error(exc: Exception) -> str | None:
    """The E13 fix text when `exc` means "no usable AWS credentials", else None."""
    kind = type(exc).__name__
    code = ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code")
    if kind in NO_CREDENTIALS or code in NO_CREDENTIALS:
        from . import checks

        return checks.no_credentials_text(code or kind)
    return None


def apply_stack(root: Path, stack: Path, rel: str, log) -> dict:
    tf = terraform_bin()
    if not tf:
        raise InfraError("Terraform is not installed")
    pdir = plan_dir(root, rel)
    plan_file = pdir / "dev.tfplan"
    approved = (stack / "plan.sha256").read_text(encoding="utf-8").strip()
    marker = pdir / "applied.sha256"
    try:
        applied = marker.read_text(encoding="utf-8").strip()
    except OSError:
        applied = None
    if applied and applied == approved:
        # D3: the approved plan was applied by an earlier run; applying a saved plan twice fails ("Saved plan is stale")
        log(f"  {stack.name}: the approved plan ({approved[:12]}) is already applied; skipped")
        return {"stack": str(stack), "skipped": "already applied", "applied_sha256": approved}
    if not plan_file.exists():
        raise InfraError(f"the saved plan for {stack.name} is not on this machine; render and plan the dev environment again (S5)")
    if _sha_file(plan_file) != approved:
        raise InfraError("the saved plan is not the one approved at G5 (hash differs); plan again and get G5 approved")
    problems = plan_guard(_plan_json(tf, stack, plan_file))  # again, on the exact file about to be applied
    if problems:
        raise InfraError("the plan is refused: " + "; ".join(problems))
    p = _tf(tf, ["apply", "-input=false", "-no-color", plan_file.as_posix()], stack, log)
    if p.returncode != 0:
        raise InfraError(f"terraform apply failed in {stack} (a stale plan means the state changed: plan again)")
    out = subprocess.run([tf, "output", "-json"], cwd=str(stack), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, env=_tf_env(stack))
    try:
        outputs = {k: v.get("value") for k, v in json.loads(out.stdout or "{}").items()}
    except ValueError:
        outputs = {}
    marker.write_text(approved + "\n", encoding="utf-8", newline="\n")
    return {"stack": str(stack), "outputs": outputs, "log_tail": (p.stdout or "")[-3000:], "applied_sha256": approved}


# ---------------------------------------------------------------------------------------------------------- policy checks
def policy_check(policy: dict, calls: list[tuple[str, str]], region: str) -> dict:
    """IAM Access Analyzer validate-policy (well-formed) and simulate-custom-policy (sufficient). Needs AWS credentials;
    recorded as 'not checked' when they are not available."""
    out: dict = {"validate": None, "simulate": None}
    try:
        import boto3

        aa = boto3.client("accessanalyzer", region_name=region)
        findings = aa.validate_policy(policyDocument=json.dumps(policy), policyType="IDENTITY_POLICY").get("findings", [])
        out["validate"] = [{"type": x.get("findingType"), "issue": x.get("issueCode"), "detail": x.get("findingDetails")} for x in findings]
    except Exception as exc:  # noqa: BLE001
        out["validate"] = f"not checked: {type(exc).__name__}: {exc}"[:300]
    try:
        import boto3

        iam = boto3.client("iam", region_name=region)
        denied = []
        for action, res in calls:
            r = iam.simulate_custom_policy(PolicyInputList=[json.dumps(policy)], ActionNames=[action],
                                           ResourceArns=[res.replace("*", "pkf-simulated-key")])
            for e in r.get("EvaluationResults", []):
                if e.get("EvalDecision") != "allowed":
                    denied.append(f"{action} on {res}: {e.get('EvalDecision')}")
        out["simulate"] = {"checked": len(calls), "denied": denied}
    except Exception as exc:  # noqa: BLE001
        out["simulate"] = f"not checked: {type(exc).__name__}: {exc}"[:300]
    return out


def assume_run_role(role_arn: str, policy: dict, region: str, pipeline: str) -> dict:
    """Temporary credentials for the run role, capped by the generated worker policy (session policy)."""
    import boto3

    try:
        c = boto3.client("sts", region_name=region).assume_role(RoleArn=role_arn, RoleSessionName=f"pkf-{pipeline}"[:64],
                                                                Policy=json.dumps(policy), DurationSeconds=3600)["Credentials"]
    except Exception as exc:  # noqa: BLE001 - E13: the same fix text as the identity check
        msg = credentials_error(exc)
        if msg:
            raise InfraError(msg) from None
        raise InfraError(f"could not assume the run role {role_arn}: {type(exc).__name__}: {str(exc)[:300]}") from None
    return {"AWS_ACCESS_KEY_ID": c["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": c["SecretAccessKey"], "AWS_SESSION_TOKEN": c["SessionToken"]}
