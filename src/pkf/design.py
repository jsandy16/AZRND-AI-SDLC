"""Designing a component (trigger, source, transformation or destination) from plain English.

    requirement + examples
      → PLAN      (planner agent: catalogue steps, custom steps with pseudocode; strict schema)
      → TESTS     (test-writer agent: cases for every custom step, from the pseudocode only, never from code)
      → SIMULATE  (deterministic: the plan on every example)            → a person approves plan + tests
      → CODE      (coder agent per custom step, with a test runner it calls until its tests pass)
      → VERIFY    (deterministic: exact file structure, static guard, every example and test case in an isolated
                   subprocess)                                          → a person approves the code
                                                                          (only when there are custom steps)

Components without a requirement need no AI and no approval: `steps:` written by hand, or plain settings
(`extraction`, `delivery.prefix`, pipeline `schedule`) turned into catalogue steps.

State: <folder>/<name>.lock.yaml (plan, tests, simulation, approvals) and <folder>/<name>.py (custom functions).
Any change to the requirement, params, examples or settings makes the plan stale; any edit to the code file makes
the code approval stale. `pkf build` refuses a pipeline whose components are not approved and current.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import importlib
import inspect
import json
import os
import re
import string
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pkf_runtime import discovery, events, layout, simulate
from pkf_runtime.ops import OPS, catalogue, parse_params_for

from . import VERSION, yamlio
from .envelope import Issue, PfError
from .repo import FOLDERS, Repo
from .specs.common import BUILTIN_VARS

ALLOWED_IMPORTS = {"re", "fnmatch", "json", "datetime", "hashlib", "zipfile", "gzip", "io", "posixpath", "math",
                   "itertools", "collections", "csv", "urllib.parse"}
FORMAT_ATTRS = {"format", "format_map", "get_field", "get_value", "vformat"}  # attribute walks through format fields
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "input", "globals", "locals", "vars", "setattr", "delattr",
                   "getattr", "breakpoint", "exit", "quit", "os", "sys", "subprocess", "socket", "shutil", "builtins"}

PKG_API = """Package API (pkg):
  pkg.files() -> list[str]                 package-relative paths, '/'-separated, sorted
  pkg.size(path) -> int ; pkg.exists(path) -> bool ; pkg.checksum(path, "md5"|"sha1"|"sha256"|"sha512") -> str
  pkg.move(src, dst, overwrite=False) ; pkg.copy(src, dst, overwrite=False) ; pkg.remove(path)
  pkg.write_text(path, text, overwrite=False) / pkg.write_bytes(path, data, overwrite=False)   new files (e.g. manifests)
  pkg.open_read(path) / pkg.open_write(path) -> binary streams, only for zipfile/gzip/hashlib (with-statement)
  pkg.name (get/set) ; pkg.render(template, extra_vars={}) -> str ; pkg.vars (dict of run variables)
  pkg.fail(message)                        refuse the package (the run fails for it, no retry)
Custom code works on structure (paths, sizes, checksums, archives). It never parses file contents."""

EVENT_API = """Events (list of dicts), each:
  {"id": str, "message_ids": [str], "data": parsed message body (after parse_json/unwrap_sns) or None,
   "objects": [{"bucket", "key", "size", "etag", "time", "sequencer"}], "vars": {name: str}, "error": None | str}
Return the new list; keep each event's "message_ids" (the framework acknowledges messages through them; a new event
built from several takes the union). Drop an event to ignore it (its message is acknowledged); set "error" to keep its message for
retry (dead-letter queue after the queue's max receives). Never read or write anything else."""

SOURCE_API = """Groups (list of dicts), one package each:
  {"id", "event_id", "message_ids", "vars": {name: str}, "objects": [event objects], "files": [{"root", "path", "size", "mtime"}]}
'path' is relative to 'root' and becomes the path inside the package.
src (the source, read-only): src.list(root, recursive=True, max_depth=20) -> [file], src.stat(root, path) -> file | None,
src.read_text(root, path) -> str (a small control/manifest file only, never data files).
Return the new list of groups. Refuse with: raise ValueError("reason")."""

LAYOUT_API = """Layout (dict): {"files": [package paths], "keys": {path: object key or None (= not delivered)}, "vars": {name: str}}
Set layout["keys"][path] for files not placed yet (earlier steps may have placed some). Return the layout.
Delivery policies (if_exists, encryption, success marker) apply to the framework's own upload.
A plan may END with one `custom_delivery` step instead of the framework's upload (see DELIVERY API)."""

DELIVERY_API = """DELIVERY API — the `custom_delivery` step (last step of a destination plan; S3 source and destination; no transformation):
  def <step_id>(transfers, params, ctx, aws) -> results
  transfers: [{"path": package path, "source_bucket", "source_key": the exact listed key, "size", "etag": the listed ETag
             or None, "target_bucket", "target_key"}], one per file to deliver; target_key computed by the earlier steps
             (files whose key is None are not passed)
  aws: aws.s3_source / aws.s3_destination are boto3 S3 clients (each side's region, endpoint and credentials);
       aws.client(service, side="destination") any other boto3 client; aws.log a logger
  ctx["delivery"]: the destination's delivery settings (if_exists, server_side_encryption, kms_key_id) — apply them
  results: exactly one dict per transfer: {"path", "key": the key written, "status", "detail": reason or ""}
       status starting with "moved", "copied", "delivered" or "skipped" counts as delivered (e.g. "moved_to_error");
       anything else — use "failed" — fails the file, its package and the run; the source must then be left as it was
  The files are NOT downloaded: copy server-side (aws.s3_destination.copy(CopySource, Bucket, Key, ExtraArgs=…,
  SourceClient=aws.s3_source) is multipart above 8 MiB; pass CopySourceIfMatch=etag in ExtraArgs when etag is given so
  only the listed version is copied). Delete a source only after its copy is confirmed.
  The framework already: refuses packages whose targets collide or land on a listed source (before your code runs);
  marks a file delivered when an earlier attempt moved it (source gone, target present with the size); applies
  if_exists fail/skip_identical; and after your code HEADs every moved/copied/delivered target and checks its size."""

CASE_FORMATS = {
    "trigger": 'input_json: {"events": [event, ...]}  expected_json: [{"keys": [object keys], "vars": {subset}} ...] (one per remaining good event)',
    "source": 'input_json: {"groups": [{"files": [paths], "vars": {}, "objects": [{"key": ...}]}], "listing": ["path[:size]" from the top of the source (S3: the full object key, with every prefix)], '
              '"contents": {path: text}}  expected_json: [[package paths of a group], ...]',
    "destination": 'input_json: {"files": [package paths], "vars": {name: value}, "keys": {already placed}}  expected_json: [delivered keys]',
    "custom_delivery": ('input_json: {"objects": {"<bucket>/<key>": "<text>" or {"body": "<text>", "tags": {k: v}}} (the S3 state before; '
                        'bucket names are 3+ lowercase characters), "transfers": [transfer, ...], "fail_calls": [{"operation": "CopyObject"|'
                        '"HeadObject"|"DeleteObject"|"PutObjectTagging"|"UploadPartCopy"|"PutObject"|"GetObject", "key": "<key or null = any>"}] '
                        '(optional: make that S3 call fail)}  expected_json: {"objects": [every "<bucket>/<key>" after delivery], '
                        '"tags": {"<bucket>/<key>": {exact tag set}} (only objects whose tags matter), "statuses": {path: status}} '
                        '(the S3 calls run against an in-memory S3)'),
    "transformation": 'input_json: {"files": ["path[:size]"]}  expected_json: [every output path; archive members as "x.zip!/inner"]',
}


@dataclass(frozen=True)
class KindDef:
    kind: str
    ops: dict
    args: tuple[str, ...]
    api: str
    about: str

    @property
    def folder(self) -> str:
        return FOLDERS[self.kind]

    def signature(self, fn: str) -> str:
        return f"def {fn}({', '.join(self.args)}):"


KINDS = {
    "trigger": KindDef("trigger", events.TRIGGER_OPS, ("events", "params", "ctx"), EVENT_API,
                       "How a run starts and what it ingests: the first step receives messages (schedule, sqs, run_conf); "
                       "later steps turn them into events, one package each."),
    "source": KindDef("source", discovery.SOURCE_OPS, ("groups", "params", "ctx", "src"), SOURCE_API,
                      "Which files form each package, per event: list folders, take the objects an event names, select, "
                      "control files, split into several packages."),
    "transformation": KindDef("transformation", OPS, ("pkg", "params", "ctx"), PKG_API,
                              "How a package's structure changes (never its contents): select, check, restructure, archive, checksum, manifest."),
    "destination": KindDef("destination", layout.DESTINATION_OPS, ("layout", "params", "ctx"), LAYOUT_API + "\n\n" + DELIVERY_API,
                           "Where each file of a finished package is delivered (object keys) and, with a final custom_delivery step, "
                           "how (moves, server-side copies, tags, any S3 operation)."),
}

CUSTOM_OPS = ("custom", "custom_delivery")
DELIVERY_ARGS = ("transfers", "params", "ctx", "aws")
CODE_POLICIES = ("open", "restricted")


def step_args(kind: str, step: dict) -> tuple[str, ...]:
    return DELIVERY_ARGS if step.get("op") == "custom_delivery" else KINDS[kind].args


def step_signature(kind: str, step: dict) -> str:
    return f"def {step['id']}({', '.join(step_args(kind, step))}):"


def code_policy(root: Path | None = None) -> str:
    """open (default): generated code may import and call anything (boto3, os, …); people review and approve it.
    restricted: the static guard allows only a small set of pure-Python modules. Set in pkf.toml [agent] code_policy,
    or PKF_CODE_POLICY for one command."""
    pol = os.environ.get("PKF_CODE_POLICY")
    if not pol and root is not None:
        from .repo import settings

        pol = (settings(root).get("agent") or {}).get("code_policy")
    pol = (pol or "open").strip().lower()
    if pol not in CODE_POLICIES:
        raise PfError(f"code_policy must be one of {CODE_POLICIES}, found '{pol}'", "invalid")
    return pol


def kind_def(kind: str) -> KindDef:
    if kind not in KINDS:
        raise PfError(f"unknown component kind '{kind}' (one of {list(KINDS)})", "usage")
    return KINDS[kind]


# ---------------------------------------------------------------------------------------------- files and hashes
def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def lock_path(repo: Repo, kind: str, name: str) -> Path:
    return repo.root / FOLDERS[kind] / f"{name}.lock.yaml"


def code_path(repo: Repo, kind: str, name: str) -> Path:
    return repo.root / FOLDERS[kind] / f"{name}.py"


def load_lock(repo: Repo, kind: str, name: str) -> dict:
    p = lock_path(repo, kind, name)
    return yamlio.to_plain(yamlio.load(p)) if p.exists() else {}


def save_lock(repo: Repo, kind: str, name: str, lock: dict) -> None:
    header = (f"# {FOLDERS[kind]}/{name}.lock.yaml — plan, step tests, simulation, code tests and approvals.\n"
              "# Written by `pkf plan/code/test/approve-…`; approvals are a person's decision. Do not edit by hand.\n")
    lock_path(repo, kind, name).write_text(header + yamlio.dumps(json.loads(json.dumps(lock, default=str))), encoding="utf-8",
                                           newline="\n")


def spec_of(repo: Repo, kind: str, name: str):
    return repo.get(kind, name)


def mode(spec) -> str:
    """requirement (AI-planned), steps (hand-written) or config (plain settings)."""
    if getattr(spec, "requirement", None):
        return "requirement"
    if getattr(spec, "steps", None) is not None:
        return "steps"
    return "config"


def settings_view(kind: str, spec) -> dict:
    """What the planner may rely on: the component type and the NAMES of its parameters. Environment values (buckets,
    queue URLs, hosts, parameter values) are hidden, so plans refer to them through templates and stay valid in every
    environment."""
    d: dict = {"type": getattr(spec, "type", kind), "parameters": {k: "{" + k + "}" for k in spec.params}}
    if kind == "destination":
        d["delivery_policies"] = {k: v for k, v in spec.delivery.model_dump(mode="json").items() if k != "prefix"}
    return d


def requirement_sha(kind: str, spec) -> str:
    """What the plan depends on: the requirement, examples, parameter names and the component type. Environment values
    (bucket names, queue URLs, endpoints, credentials, parameter values) can change per environment without re-planning,
    because plans refer to them through templates such as {queue_url}."""
    return _sha({"requirement": (spec.requirement or "").strip(), "params": sorted(spec.params),
                 "examples": [e.model_dump(mode="json") for e in spec.examples], "type": getattr(spec, "type", kind)})


def plan_digest(lock: dict) -> str:
    p = lock.get("plan") or {}
    return _sha({"requirement_sha256": lock.get("requirement_sha256"), "steps": p.get("steps"), "step_tests": p.get("step_tests")})


def code_digest(lock: dict, code_sha: str) -> str:
    c = lock.get("code") or {}
    return _sha({"plan_digest": lock.get("plan_digest"), "code_sha256": code_sha,
                 "tests": [(t.get("example") or t.get("case"), t["status"]) for t in c.get("tests", [])], "problems": c.get("problems", [])})


def _identity(root: Path) -> str | None:
    if os.environ.get("GITHUB_ACTOR"):
        return "github:" + os.environ["GITHUB_ACTOR"]
    try:
        return subprocess.run(["git", "-C", str(root), "config", "user.email"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def approval_record(repo_root: Path, by: str, digest: str, evidence: dict | None = None) -> dict:
    """Who approved, as recorded in a lock. With evidence (a GitHub review or comment the Studio verified) the identity
    is the approver's GitHub account, not the git email of the machine that ran the command."""
    rec = {"by": by, "identity": _identity(repo_root), "at": _now(), "digest": digest}
    if evidence:
        login = by.lstrip("@")
        rec["identity"] = f"github:{login}" + (f"#{evidence['github_id']}" if evidence.get("github_id") else "")
        rec["evidence"] = {k: v for k, v in evidence.items() if v is not None}
    return rec


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _check_handle(by: str) -> None:
    if not re.fullmatch(r"@[\w.-]+(/[\w.-]+)?", by or ""):
        raise PfError("--by must be a handle like @jane", "invalid")


def custom_ids(steps: list[dict]) -> list[str]:
    return [s["id"] for s in steps if s["op"] in CUSTOM_OPS]


def examples_of(spec) -> list[dict]:
    return [e.model_dump(mode="json") for e in spec.examples]


def run_examples_inprocess(kind: str, spec, steps: list[dict], custom: dict | None = None) -> list[dict]:
    sim = simulate.SIMULATORS[kind]
    return [{"example": e["name"], **sim(steps, custom, e, spec.params)} for e in examples_of(spec)]


# ---------------------------------------------------------------------------------------------- steps without AI
def config_steps(kind: str, spec) -> list[dict]:
    if kind == "source":
        return discovery.default_steps(spec.extraction.model_dump(mode="json"))
    if kind == "destination":
        return layout.default_steps(spec.delivery.model_dump(mode="json"))
    if kind == "trigger":  # settings only: one run per schedule tick
        return [{"id": "tick", "op": "schedule", "title": "One run per scheduled tick", "params": {}}]
    raise PfError(f"{kind} '{spec.name}' needs a requirement or steps", "invalid")


def validate_steps(kind: str, steps: list[dict]) -> list[str]:
    problems, ids = [], set()
    for s in steps:
        if s["id"] in ids:
            problems.append(f"step id '{s['id']}' is used twice")
        ids.add(s["id"])
        try:
            parse_params_for(KINDS[kind].ops, s["op"], s["params"])
        except (ValidationError, ValueError) as exc:
            problems.append(f"step '{s['id']}' ({s['op']}): {str(exc)[:300]}")
        if s["op"] in CUSTOM_OPS and s["params"].get("function") != s["id"]:
            problems.append(f"custom step '{s['id']}' must have params {{\"function\": \"{s['id']}\"}}")
    deliveries = [i for i, s in enumerate(steps) if s["op"] == "custom_delivery"]
    if deliveries and deliveries != [len(steps) - 1]:
        problems.append("a destination has at most one custom_delivery step, and it is the last step")
    if kind == "trigger":
        receives = [i for i, s in enumerate(steps) if s["op"] in events.RECEIVE_OPS]
        if receives != [0]:
            problems.append(f"a trigger has exactly one receive step ({', '.join(events.RECEIVE_OPS)}), and it comes first")
    return problems


def provided_vars(repo: Repo, kind: str, name: str) -> set[str]:
    """Variables a trigger or source adds (for template validation); best effort from its current steps."""
    try:
        steps = peek_steps(repo, kind, name)
    except Exception:  # noqa: BLE001 - validation must not fail on a half-designed component
        return set()
    return (events.provides if kind == "trigger" else discovery.provides)(steps or [])


def peek_steps(repo: Repo, kind: str, name: str) -> list[dict] | None:
    spec = repo.specs[kind].get(name)
    if spec is None:
        return None
    m = mode(spec)
    if m == "steps":
        return [s.model_dump(mode="json") for s in spec.steps]
    if m == "config":
        return config_steps(kind, spec)
    return (load_lock(repo, kind, name).get("plan") or {}).get("steps")


# ---------------------------------------------------------------------------------------------- status
def status(repo: Repo, kind: str, name: str) -> dict:
    spec = spec_of(repo, kind, name)
    m = mode(spec)
    if m != "requirement":
        steps = peek_steps(repo, kind, name) or []
        problems = validate_steps(kind, steps)
        if problems:
            return {"stage": "steps_invalid", "mode": m, "next": f"fix {repo.file_of(kind, name)}: " + "; ".join(problems[:3])}
        if custom_ids(steps):
            return {"stage": "steps_invalid", "mode": m, "next": "custom steps need a requirement (the AI plans and codes them)"}
        bad = [r for r in run_examples_inprocess(kind, spec, steps) if r["status"] == "fail"]
        if bad:
            return {"stage": "examples_failing", "mode": m, "next": f"example '{bad[0]['example']}': {bad[0]['detail']}"}
        return {"stage": "approved", "mode": m, "next": "ready (no AI design needed)"}
    lock = load_lock(repo, kind, name)
    if not lock.get("plan"):
        return {"stage": "needs_plan", "mode": m, "next": f"pkf plan {kind} {name}"}
    if lock.get("requirement_sha256") != requirement_sha(kind, spec):
        return {"stage": "plan_stale", "mode": m, "next": f"the requirement, examples or settings changed: pkf plan {kind} {name}"}
    if lock.get("plan_digest") != plan_digest(lock):
        return {"stage": "plan_tampered", "mode": m, "next": f"the lock was edited: pkf plan {kind} {name}"}
    appr = lock.get("plan_approval")
    if not appr or appr.get("digest") != lock["plan_digest"]:
        return {"stage": "plan_proposed", "mode": m,
                "next": f"review `pkf show {kind} {name}`, then a person runs `pkf approve-plan {kind} {name} --by @handle`"}
    steps = lock["plan"]["steps"]
    if not custom_ids(steps):
        return {"stage": "approved", "mode": m, "next": "ready: no custom code to review"}
    code = lock.get("code")
    cp = code_path(repo, kind, name)
    if not code or not cp.exists() or code.get("plan_digest") != lock["plan_digest"]:
        return {"stage": "plan_approved", "mode": m, "next": f"pkf code {kind} {name}"}
    sha = hashlib.sha256(cp.read_bytes()).hexdigest()
    if code_policy(repo.root) == "restricted" and code.get("code_policy") == "open":
        return {"stage": "code_changed", "mode": m,
                "next": f"the code was tested under the open code policy, the repository is now restricted: pkf test {kind} {name}, then approve again"}
    if code.get("sha256") != sha or "tests" not in code:
        return {"stage": "code_changed", "mode": m, "next": f"the code file changed: pkf test {kind} {name}, then approve again"}
    if code.get("problems") or any(x["status"] not in ("pass", "shown") for x in code.get("tests", [])):
        return {"stage": "code_failing", "mode": m, "next": f"fix {FOLDERS[kind]}/{name}.py (or re-plan) and run pkf test {kind} {name}"
                f" — if a test case itself is wrong, Reopen G3 and re-plan {kind} {name}"}
    ca = lock.get("code_approval")
    if not ca or ca.get("digest") != code_digest(lock, sha):
        return {"stage": "code_generated", "mode": m,
                "next": f"review {FOLDERS[kind]}/{name}.py, then a person runs `pkf approve-code {kind} {name} --by @handle`"}
    return {"stage": "approved", "mode": m, "next": "ready"}


def current(repo: Repo, kind: str, name: str) -> dict:
    """Steps and custom code as they stand now, approved or not (for end-to-end checks while reviewing)."""
    spec = spec_of(repo, kind, name)
    steps = peek_steps(repo, kind, name)
    if steps is None:
        raise PfError(f"{kind} '{name}' has no plan yet: pkf plan {kind} {name}", "not_ready")
    fns = {}
    if mode(spec) == "requirement" and custom_ids(steps) and code_path(repo, kind, name).exists():
        fns = extract_functions(code_path(repo, kind, name).read_text(encoding="utf-8"), custom_ids(steps)) or {}
    return {"steps": steps, "functions": fns, "header": []}


DRAFT_HEADER = "Code NOT approved yet: draft build for review (S4)"


def approved(repo: Repo, kind: str, name: str, *, allow_unapproved_code: bool = False) -> dict:
    """Steps and custom function sources of an approved component (what `pkf build` puts in the DAG).
    allow_unapproved_code (`pkf build --draft`, S4): code that passes its tests but awaits a person's approval
    (code_generated) is accepted too, and the header says so; any other stage is refused."""
    st = status(repo, kind, name)
    draft = False
    if st["stage"] != "approved":
        if not allow_unapproved_code:
            raise PfError(f"{kind} '{name}' is not ready ({st['stage']}): {st['next']}", "component_not_approved")
        if st["stage"] != "code_generated":
            raise PfError(f"{kind} '{name}' is not ready for a draft build ({st['stage']}): {st['next']}", "component_not_ready")
        draft = True
    spec = spec_of(repo, kind, name)
    if st["mode"] != "requirement":
        return {"steps": peek_steps(repo, kind, name), "functions": {}, "header": [f"Mode: {st['mode']} (no AI design)"]}
    lock = load_lock(repo, kind, name)
    steps = lock["plan"]["steps"]
    fns: dict[str, str] = {}
    if custom_ids(steps):
        fns = extract_functions(code_path(repo, kind, name).read_text(encoding="utf-8"), custom_ids(steps)) or {}
    header = ["Requirement (approved):", *("  " + ln for ln in (spec.requirement or "").strip().splitlines()),
              f"Plan approved by {lock['plan_approval']['by']} at {lock['plan_approval']['at']}"]
    if draft:
        header.append(DRAFT_HEADER)
    elif lock.get("code_approval"):
        header.append(f"Code approved by {lock['code_approval']['by']} at {lock['code_approval']['at']}")
    return {"steps": steps, "functions": fns, "header": header}


# ---------------------------------------------------------------------------------------------- plan
def _plan_prompt(kind: str, spec) -> str:
    from .llm.base import fence

    k = KINDS[kind]
    var_names = sorted(set(BUILTIN_VARS) | set(spec.params) | {p for e in spec.examples for p in (e.params or {})})
    extra = ""
    if kind in ("source", "destination", "transformation"):
        extra = ("Event variables an event-driven trigger can provide (when the examples use them): "
                 + ", ".join("{" + v + "}" for v in events.S3_EVENT_VARS) + ", plus any set_vars/split variables shown in examples.\n")
    return (f"Component kind: {kind}. Name: {spec.name}.\n{k.about}\n\n"
            f"Operation catalogue (JSON):\n{json.dumps(catalogue(k.ops), indent=1, default=str)}\n\n"
            f"Template variables: {var_names}\n{extra}\n"
            f"Custom step signature: {k.signature('<step_id>')}\n{k.api}\n\n"
            f"Component settings (JSON, fixed by the owner):\n{json.dumps(settings_view(kind, spec), indent=1)}\n\n"
            f"Examples (JSON):\n{json.dumps(examples_of(spec), indent=1)}\n\n"
            f"{fence('requirement', (spec.requirement or '').strip())}\n")


def _tests_prompt(kind: str, spec, plan: dict, step: dict) -> str:
    from .llm.base import fence

    k = KINDS[kind]
    return (f"Component kind: {kind}. Name: {spec.name}.\nPlan summary: {plan['summary']}\n\n"
            f"Write test cases for the custom step `{step['id']}` ({step['title']}).\nSignature: {step_signature(kind, step)}\n{k.api}\n\n"
            f"Pseudocode (the specification you test against):\n{step['pseudocode']}\n\n"
            f"Case format: {CASE_FORMATS['custom_delivery' if step['op'] == 'custom_delivery' else kind]}\n\n"
            f"Full plan (for context):\n{json.dumps(plan['steps'], indent=1)}\n\n"
            f"Component examples (end to end, for context):\n{json.dumps(examples_of(spec), indent=1)}\n\n"
            f"{fence('requirement', (spec.requirement or '').strip())}\n")


def _parse_plan(kind: str, k, out, f: str) -> tuple[list[dict], list[Issue]]:
    """The model's plan → validated steps, with every problem as an issue."""
    from .llm.base import strip_fence

    issues: list[Issue] = []
    steps = []
    for i, s in enumerate(out.steps):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", s.id):
            issues.append(Issue(file=f, loc=f"plan.steps.{i}", msg=f"step id '{s.id}' must be snake_case", code="invalid_plan"))
        try:
            params = {"function": s.id} if s.op in CUSTOM_OPS else json.loads(strip_fence(s.params_json or "{}") or "{}")
            params = parse_params_for(k.ops, s.op, params).model_dump(mode="json", exclude_defaults=True)
            if s.op in CUSTOM_OPS:
                params = {"function": s.id}
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            issues.append(Issue(file=f, loc=f"plan.steps.{i}", code="invalid_plan", msg=f"step '{s.id}' ({s.op}): {str(exc)[:300]}"))
            params = {}
        if s.op in CUSTOM_OPS and not s.pseudocode.strip():
            issues.append(Issue(file=f, loc=f"plan.steps.{i}", msg=f"custom step '{s.id}' has no pseudocode", code="invalid_plan"))
        steps.append({"id": s.id, "op": s.op, "title": s.title, "params": params, **({"pseudocode": s.pseudocode} if s.op in CUSTOM_OPS else {})})
    if not issues:
        issues += [Issue(file=f, loc="plan", msg=m, code="invalid_plan") for m in validate_steps(kind, steps)]
    return steps, issues


def plan(repo: Repo, kind: str, name: str) -> tuple[dict, list[Issue]]:
    from .llm import run_structured
    from .llm.base import redact_secrets, redaction_warning
    from .llm.schemas import ComponentPlan

    k = kind_def(kind)
    spec = spec_of(repo, kind, name)
    if mode(spec) != "requirement":
        raise PfError(f"{kind} '{name}' has no requirement: nothing to plan ({mode(spec)} mode)", "not_ready")
    f = f"{k.folder}/{name}.lock.yaml"
    log_dir = repo.root / k.folder / ".llm"
    prompt = _plan_prompt(kind, spec)
    for attempt in range(2):  # one repair round: the model sees its validation problems and failing examples
        out, meta = run_structured("plan_component", "planning-pipeline-components", prompt, ComponentPlan, log_dir=log_dir)
        steps, issues = _parse_plan(kind, k, out, f)
        sim = [] if issues else run_examples_inprocess(kind, spec, steps)
        failing = [r for r in sim if r["status"] == "fail"]
        if (not issues and not failing) or attempt == 1:
            break
        prompt = (_plan_prompt(kind, spec) + "\n\nYour previous plan was rejected. Plan again, fixing exactly these problems:\n"
                  + "\n".join([f"- {i.loc}: {i.msg}" for i in issues] + [f"- example '{r['example']}': {r['detail']}" for r in failing])
                  + "\nYour previous steps were:\n" + json.dumps(steps, indent=1)[:6000] + "\n")
    p = {"summary": out.summary, "steps": steps, "assumptions": out.assumptions, "ambiguities": out.ambiguities, "step_tests": {}}
    tested_by = {}
    plan_errors = [i for i in issues if i.severity == "error"]
    if not plan_errors:
        for s in steps:  # the test writer sees the pseudocode, never code
            if s["op"] not in CUSTOM_OPS:
                continue
            cases, tmeta, case_issues = _write_tests(kind, spec, p, s, f, log_dir)
            issues += case_issues
            p["step_tests"][s["id"]] = cases
            tested_by[s["id"]] = tmeta.model_dump(exclude={"duration_s", "cost_usd", "turns", "provider"})
    lock = {"lock_version": 2, "kind": kind, "name": name, "requirement_sha256": requirement_sha(kind, spec), "plan": p,
            "planned_by": meta.model_dump(exclude={"duration_s", "cost_usd", "turns", "provider"}), "tests_written_by": tested_by,
            "simulation": [] if plan_errors else (sim or run_examples_inprocess(kind, spec, steps))}  # only plan errors stop it
    lock["plan_digest"] = plan_digest(lock)
    for r in lock["simulation"]:
        if r["status"] == "fail":
            issues.append(Issue(severity="warning", file=f, loc=f"simulation.{r['example']}", code="simulation_mismatch",
                                msg=f"example '{r['example']}': {r['detail']}"))
    if out.ambiguities:
        issues.append(Issue(severity="warning", file=repo.file_of(kind, name), loc="requirement", code="ambiguous",
                            msg="the owner must clarify: " + " | ".join(out.ambiguities)))
    n_redacted = redact_secrets(_plan_prompt(kind, spec))[1]
    if n_redacted:
        issues.append(Issue(severity="warning", file=repo.file_of(kind, name), loc="requirement", code="redacted",
                            msg=redaction_warning(n_redacted)))
    lock["issues"] = [i.model_dump() for i in issues]  # shown by `pkf show` and plan_problems; not part of plan_digest
    code_path(repo, kind, name).unlink(missing_ok=True)  # a new plan invalidates any previous code
    save_lock(repo, kind, name, lock)
    return lock, issues


def _parse_cases(tests, f: str, sid: str) -> tuple[list[dict], list[Issue]]:
    """The test writer's cases as dicts; a case whose JSON does not parse (after removing a Markdown fence) is dropped
    with a warning."""
    from .llm.base import strip_fence

    cases, issues = [], []
    for c in tests.cases:
        try:
            cases.append({"name": c.name, "purpose": c.purpose, "input": json.loads(strip_fence(c.input_json)),
                          "params": json.loads(strip_fence(c.params_json or "{}") or "{}"),
                          "expected": json.loads(strip_fence(c.expected_json)) if not c.expect_error else None,
                          "expect_error": c.expect_error})
        except json.JSONDecodeError as exc:
            issues.append(Issue(severity="warning", file=f, loc=f"step_tests.{sid}.{c.name}", code="invalid_test",
                                msg=f"test case dropped (not JSON): {c.name}: {exc}"))
    return cases, issues


def case_problems(kind: str, step: dict, cases: list[dict]) -> list[tuple[str, str]]:
    """(case name, problem) for every malformed case of a custom step (simulate.validate_case)."""
    out = []
    for c in cases:
        out += [(str(c.get("name")), m) for m in simulate.validate_case(kind, step["op"], c)]
    names = [c.get("name") for c in cases]
    out += [(str(n), "two cases have this name") for n in sorted({n for n in names if names.count(n) > 1}, key=str)]
    return out


def _write_tests(kind: str, spec, plan: dict, step: dict, f: str, log_dir: Path):
    """The test writer's cases for one custom step, checked for shape; malformed cases are sent back once with the
    problems (a failure of that call keeps the first answer). Problems left are `invalid_test` errors."""
    from .llm import run_structured
    from .llm.schemas import StepTests

    prompt = _tests_prompt(kind, spec, plan, step)
    tests, tmeta = run_structured("write_step_tests", "writing-step-tests", prompt, StepTests, log_dir=log_dir)
    cases, issues = _parse_cases(tests, f, step["id"])
    probs = case_problems(kind, step, cases)
    if probs:
        repair = (prompt + "\n\nYour previous test cases were rejected by the framework's checks. Write the cases again, fixing "
                  "exactly these problems and keeping the good cases:\n" + "\n".join(f"- case '{n}': {m}" for n, m in probs)
                  + "\nYour previous cases were:\n" + json.dumps(cases, indent=1, default=str)[:6000] + "\n")
        try:
            tests2, tmeta2 = run_structured("write_step_tests", "writing-step-tests", repair, StepTests, log_dir=log_dir)
        except PfError:
            pass  # keep the first answer; its problems are reported below
        else:
            cases2, issues2 = _parse_cases(tests2, f, step["id"])
            if cases2:
                cases, issues, tmeta, probs = cases2, issues2, tmeta2, case_problems(kind, step, cases2)
    issues += [Issue(file=f, loc=f"step_tests.{step['id']}.{n}", code="invalid_test", msg=f"case '{n}' of step {step['id']}: {m}")
               for n, m in probs]
    return cases, tmeta, issues


def plan_problems(repo: Repo, kind: str, name: str) -> list[tuple[str, str]]:
    """Why a proposed plan cannot be approved (message, code); empty when it can. Shared by approve-plan and by the
    Studio's G3 readiness, so a G3 PR is never approved for a plan the framework would then refuse."""
    if mode(spec_of(repo, kind, name)) != "requirement":
        return []
    st = status(repo, kind, name)
    if st["stage"] not in ("plan_proposed", "plan_approved", "code_generated", "code_changed", "code_failing", "approved"):
        return [(f"nothing to approve: {st['next']}", "not_ready")]
    lock = load_lock(repo, kind, name)
    p = lock["plan"]
    out = []
    if p.get("ambiguities"):
        out.append(("the plan lists open questions; answer them in the requirement text and plan again: "
                    + " | ".join(map(str, p["ambiguities"]))[:600], "ambiguous"))
    errors = [i for i in lock.get("issues") or [] if isinstance(i, dict) and i.get("severity") == "error"]
    plan_errs = [i for i in errors if i.get("code") != "invalid_test"]
    test_errs = [i for i in errors if i.get("code") == "invalid_test"]
    if plan_errs:
        out.append(("the plan has errors: " + "; ".join(str(i.get("msg")) for i in plan_errs[:3]) + "; plan again", "invalid_plan"))
    if test_errs:
        out.append(("a test case is malformed: " + "; ".join(str(i.get("msg")) for i in test_errs[:3]) + "; plan again", "invalid_test"))
    bad = [r for r in lock.get("simulation", []) if r["status"] == "fail"]
    if bad:
        out.append((f"the plan does not produce the expected output for {[r['example'] for r in bad]}; reword the requirement "
                    "or fix the examples, then plan again", "simulation_mismatch"))
    if not lock.get("simulation") and examples_of(spec_of(repo, kind, name)) and not plan_errs:
        out.append((f"the plan could not be simulated: the plan is invalid (pkf show {kind} {name}); plan again", "invalid_plan"))
    for sid in custom_ids(p["steps"]):
        if not p["step_tests"].get(sid):
            out.append((f"custom step '{sid}' has no test cases; plan again", "invalid_plan"))
    return out


def approve_plan(repo: Repo, kind: str, name: str, by: str, evidence: dict | None = None) -> dict:
    st = status(repo, kind, name)
    if st["stage"] not in ("plan_proposed", "plan_approved", "code_generated", "code_changed", "code_failing", "approved") or st["mode"] != "requirement":
        raise PfError(f"nothing to approve: {st['next']}", "not_ready")
    probs = plan_problems(repo, kind, name)
    if probs:
        raise PfError(probs[0][0], probs[0][1])
    lock = load_lock(repo, kind, name)
    _check_handle(by)
    lock["plan_approval"] = approval_record(repo.root, by, lock["plan_digest"], evidence)
    save_lock(repo, kind, name, lock)
    return lock["plan_approval"]


# ---------------------------------------------------------------------------------------------- code: guard and file
def guard(source: str, fn_name: str, args: tuple[str, ...] = ("pkg", "params", "ctx"), policy: str = "restricted") -> list[str]:
    """Checks on a generated custom function. Always: valid Python, exactly one function with the step's name and
    signature. With policy "restricted" also static guard rails (allowed imports only, no file system, no
    attribute walks). With "open" anything goes: the code is tested, reviewed and approved by people."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    problems = []
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef) or tree.body[0].name != fn_name:
        return [f"expected exactly one function named {fn_name}; put helpers and imports inside it"]
    got = [a.arg for a in tree.body[0].args.args]
    if got != list(args):
        problems.append(f"signature must be {fn_name}({', '.join(args)}), found {got}")
    if policy == "open":
        return problems
    allowed = lambda m: m in ALLOWED_IMPORTS  # noqa: E731
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if allowed(a.name):
                    imported[a.asname or a.name.split(".")[0]] = importlib.import_module(a.name.split(".")[0])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad = [a.name for a in node.names if not allowed(a.name)]
            if bad:
                problems.append(f"import of {bad} is not allowed (allowed: {sorted(ALLOWED_IMPORTS)})")
        elif isinstance(node, ast.ImportFrom):
            if not allowed(node.module or "") or node.level:
                problems.append(f"import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            problems.append(f"access to private attribute '.{node.attr}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append("global/nonlocal is not allowed")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in imported:
            member = getattr(imported[node.value.id], node.attr, None)
            if node.attr in ("open", "system", "popen") or (inspect.ismodule(member) and f"{node.value.id}.{node.attr}" not in ALLOWED_IMPORTS):
                problems.append(f"'{node.value.id}.{node.attr}' is not allowed (file system or another module)")
        if isinstance(node, ast.Attribute) and node.attr in FORMAT_ATTRS and not (
                node.attr == "format" and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            problems.append(f"'.{node.attr}' is only allowed as \"literal\".format(...); use f-strings")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "{" in node.value:
            try:
                fields = [f for _, f, _, _ in string.Formatter().parse(node.value) if f]
            except ValueError:
                fields = []
            if any("." in f or "[" in f for f in fields):
                problems.append("format fields with '.' or '[' are not allowed")
    return sorted(set(problems))


POSIX_WARNING = ("S3 keys and package paths are POSIX: use posixpath or str.split('/') instead of os.path/pathlib (they differ "
                 "on Windows)")


def guard_warnings(source: str) -> list[str]:
    """Advice on a step function under the open code policy (never a problem: status is unaffected): os.path, os.sep,
    ntpath or pathlib split keys and package paths differently on Windows than in Airflow."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" \
                and node.attr in ("path", "sep", "altsep"):
            return [POSIX_WARNING]
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] in ("ntpath", "pathlib") or a.name == "os.path" for a in node.names):
            return [POSIX_WARNING]
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in ("ntpath", "pathlib", "os") and (
                node.module != "os" or any(a.name in ("path", "sep", "altsep") for a in node.names)):
            return [POSIX_WARNING]
    return []


def render_module(kind: str, name: str, lock: dict, functions: dict[str, str]) -> str:
    """The code file: a reviewer's docstring, the custom step functions and CUSTOM. Catalogue steps run from the
    approved plan, so the file holds nothing but the generated functions."""
    k = KINDS[kind]
    lines = [f'"""{kind.capitalize()} `{name}`: custom steps generated by package-factory; review before approving.',
             "", "Plan (approved by " + lock["plan_approval"]["by"] + "):",
             *[f"  {i}. {s['id']} [{s['op']}] {s['title']}" for i, s in enumerate(lock['plan']['steps'], 1)],
             "", f"Each custom function: {k.signature('<step_id>')} — only function bodies may be edited",
             *([f"The custom_delivery function: def <step_id>({', '.join(DELIVERY_ARGS)}) -> results"]
               if any(s["op"] == "custom_delivery" for s in lock["plan"]["steps"]) else []),
             f"(then `pkf test {kind} {name}` and approve again).", f"Plan digest: {lock['plan_digest']}", '"""',
             "from __future__ import annotations", ""]
    for src in functions.values():
        lines += ["", src.strip("\n"), ""]
    lines += ["", "CUSTOM = {" + ", ".join(f'"{fn}": {fn}' for fn in functions) + "}", ""]
    return "\n".join(lines)


def extract_functions(source: str, names: list[str]) -> dict[str, str] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    found = {n.name: ast.get_source_segment(source, n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names}
    return {n: found[n] for n in names if n in found}


STRUCTURE_MSG = ("only the bodies of the custom step functions may be edited; other code was added or changed (nest helper "
                 "functions and imports inside the step function, or regenerate with pkf code {kind} {name})")


def structure_problems(kind: str, name: str, lock: dict, source: str) -> list[str]:
    """The code file may hold only: a module docstring, `from __future__ import annotations`, one function per custom
    step (its name and arguments, no decorators) and CUSTOM naming exactly those functions. Compared as syntax trees,
    so comments, blank lines, trailing spaces and CRLF line endings do not matter."""
    steps = lock["plan"]["steps"]
    ids = custom_ids(steps)
    fns = extract_functions(source, ids)
    if fns is None:
        return ["the file is not valid Python"]
    missing = [i for i in ids if i not in fns]
    if missing:
        return [f"missing custom step function(s) {missing}"]
    tree = ast.parse(source)
    by_id = {s["id"]: s for s in steps}
    seen_fns: list[str] = []
    custom_ok = False
    other = False
    for n, node in enumerate(tree.body):
        if n == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__" and [a.name for a in node.names] == ["annotations"] \
                and not node.level:
            continue
        if isinstance(node, ast.FunctionDef) and node.name in by_id and node.name not in seen_fns and not node.decorator_list:
            a = node.args
            if ([x.arg for x in a.args] == list(step_args(kind, by_id[node.name])) and not a.posonlyargs and not a.vararg
                    and not a.kwonlyargs and not a.kwarg and not a.defaults):
                seen_fns.append(node.name)
                continue
        if (isinstance(node, ast.Assign) and not custom_ok and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "CUSTOM" and isinstance(node.value, ast.Dict)):
            pairs = list(zip(node.value.keys, node.value.values))
            if (all(isinstance(k, ast.Constant) and isinstance(v, ast.Name) and k.value == v.id for k, v in pairs)
                    and sorted(k.value for k, _ in pairs) == sorted(ids)):
                custom_ok = True
                continue
        other = True
    if other or not custom_ok or sorted(seen_fns) != sorted(ids):
        return [STRUCTURE_MSG.format(kind=kind, name=name)]
    return []


# ---------------------------------------------------------------------------------------------- code: tests
RUNNER = r'''
import json, sys, importlib.util, os, socket, subprocess, hashlib, types
sys.dont_write_bytecode = True  # never read or write a __pycache__: the bytes tested are the bytes on disk
# Best effort, not a sandbox: generated code may start no child processes and reach no network host but this one.
def _no(*a, **k):
    raise PermissionError("not allowed while testing generated code (no child processes)")
_LOCAL = ("127.0.0.1", "::1", "localhost")
def _remote(address):
    if isinstance(address, (str, bytes)):  # AF_UNIX paths
        return None
    host = address[0] if isinstance(address, tuple) and address else address
    return None if host in _LOCAL else host
def _deny_net(host):
    raise PermissionError(f"not allowed while testing generated code (network to {host})")
_real_connect = socket.socket.connect
def _connect(self, address, *a):
    host = _remote(address)
    if host is not None:
        _deny_net(host)
    return _real_connect(self, address, *a)
socket.socket.connect = _connect
_real_connect_ex = socket.socket.connect_ex
def _connect_ex(self, address, *a):
    host = _remote(address)
    if host is not None:
        _deny_net(host)
    return _real_connect_ex(self, address, *a)
socket.socket.connect_ex = _connect_ex
_real_sendto = socket.socket.sendto
def _sendto(self, data, *a):
    host = _remote(a[-1]) if a else None
    if host is not None:
        _deny_net(host)
    return _real_sendto(self, data, *a)
socket.socket.sendto = _sendto
_real_gai = socket.getaddrinfo
def _getaddrinfo(host, *a, **k):
    h = host.decode() if isinstance(host, bytes) else host
    if h not in (None, "", *_LOCAL):
        _deny_net(h)
    return _real_gai(host, *a, **k)
socket.getaddrinfo = _getaddrinfo
_real_cc = socket.create_connection
def _create_connection(address, *a, **k):
    if address[0] not in _LOCAL:
        _deny_net(address[0])
    return _real_cc(address, *a, **k)
socket.create_connection = _create_connection
import asyncio, unittest.mock, multiprocessing.process  # imported before the block: on Windows asyncio subclasses subprocess.Popen at import
subprocess.Popen._execute_child = _no  # Popen stays the real class (subclasses keep loading), but no class can start a process
os.system = os.popen = _no
for _n in ("execv", "execve", "execvp", "execvpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp",
           "fork", "forkpty", "startfile"):
    if hasattr(os, _n):
        setattr(os, _n, _no)
try:
    import _posixsubprocess
    _posixsubprocess.fork_exec = _no
except ImportError:
    pass
try:
    import _winapi
    _winapi.CreateProcess = _no
except ImportError:
    pass
multiprocessing.process.BaseProcess.start = _no
from pkf_runtime import simulate
import pkf_runtime
_module, _job_file, _out_file = sys.argv[1:4]
del sys.argv[1:]  # best effort: the code under test does not see where the events go
job = json.loads(open(_job_file, encoding="utf-8").read())
def _emitter(path, token):
    out = open(path, "w", encoding="utf-8", newline="\n")
    def emit(ev):
        out.write(json.dumps({**ev, "t": token}, default=str) + "\n")
        out.flush()
    return emit
_emit = _emitter(_out_file, job.pop("token", None))  # the parent ignores events without this run's token
os.remove(_job_file)
del _job_file, _out_file, _emitter
src = open(_module, "rb").read()
_emit({"event": "loaded", "sha256": hashlib.sha256(src).hexdigest(), "pkf_runtime_version": getattr(pkf_runtime, "VERSION", None),
       "pkf_runtime_file": os.path.abspath(pkf_runtime.__file__)})
mod = types.ModuleType("pkf_under_test")
mod.__file__ = _module
sys.modules["pkf_under_test"] = mod
exec(compile(src, _module, "exec"), mod.__dict__)
custom = dict(mod.CUSTOM)
_emit({"event": "ready"})
if "before" in job:
    for e in job["examples"]:
        _emit({"event": "start", "example": e["name"]})
        _emit({"event": "before", "example": e["name"],
               "state": simulate.state_before(job["kind"], job["steps"], job["before"], custom, e, job["params"])})
else:
    sim = simulate.SIMULATORS[job["kind"]]
    for e in job["examples"]:
        _emit({"event": "start", "example": e["name"]})
        try:
            r = sim(job["steps"], custom, e, job["params"])
        except Exception as exc:
            r = {"status": "fail", "detail": f"crash: {type(exc).__name__}: {exc}", "output": None}
        _emit({"event": "result", "example": e["name"], **r})
    for fn, cases in job["step_tests"].items():
        if fn not in custom:
            continue
        for c in cases:
            _emit({"event": "start", "step": fn, "case": c.get("name")})
            _emit({"event": "result", "step": fn, **simulate.run_step_case(job["kind"], custom[fn], c, job["ops"].get(fn, "custom"),
                                                                          delivery=job.get("delivery"))})
_emit({"event": "done"})
'''

CHILD_ENV_KEEP = ("PATH", "SYSTEMROOT", "TZ", "WINDIR", "SYSTEMDRIVE", "COMSPEC")  # no LANG/LC_ALL/PYTHONUTF8: -X utf8


def _child_env(d: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k in CHILD_ENV_KEEP}
    env.update(HOME=d, USERPROFILE=d, APPDATA=d, LOCALAPPDATA=d, TMPDIR=d, TEMP=d, TMP=d, NO_PROXY="", HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
               AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_DEFAULT_REGION="us-east-1",
               AWS_CONFIG_FILE=str(Path(d) / "none"), AWS_SHARED_CREDENTIALS_FILE=str(Path(d) / "none"))  # never real AWS
    return env


def runner_argv(runner: Path, module_file: Path, job_file: Path, out_file: Path) -> list[str]:
    return [sys.executable, "-I", "-X", "utf8", str(runner), str(module_file), str(job_file), str(out_file)]


def runner_mismatch(loaded: dict) -> str | None:
    """The child must run the package-factory this process runs (K17): a venv with another install would test the code
    with another harness."""
    import pkf
    import pkf_runtime

    tree = Path(pkf.__file__).resolve().parents[1]
    v_child, f_child = loaded.get("pkf_runtime_version"), loaded.get("pkf_runtime_file")
    child_tree = Path(f_child).resolve().parents[1] if f_child else None
    if v_child == pkf_runtime.VERSION and child_tree == tree:
        return None
    here = tree.parent if (tree.parent / "pyproject.toml").exists() else tree
    return (f"the test runner loaded package-factory {v_child} from {child_tree}, this is {pkf_runtime.VERSION} from {tree}: "
            f"reinstall with pip install -e {here} in the venv you run pkf from")


def _run_runner(module_file: Path, job: dict, timeout: int) -> tuple[list[dict], subprocess.CompletedProcess | None]:
    """Run RUNNER on a module in an isolated child; the events it wrote (complete lines) and the process (None on
    timeout). Results come from the events file, never from stdout, which generated code may print anything to."""
    import secrets

    token = secrets.token_hex(16)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        runner, job_file = Path(d) / "runner.py", Path(d) / f"job-{secrets.token_hex(8)}.json"
        out = Path(d) / f"events-{secrets.token_hex(8)}.jsonl"
        runner.write_text(RUNNER, encoding="utf-8", newline="\n")
        job_file.write_text(json.dumps({**job, "token": token}), encoding="utf-8", newline="\n")
        proc = None
        try:
            proc = subprocess.run(runner_argv(runner, module_file, job_file, out), capture_output=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, env=_child_env(d), cwd=d)
        except subprocess.TimeoutExpired:
            pass
        events = []
        if out.exists():
            for ln in out.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    ev = json.loads(ln)
                except json.JSONDecodeError:
                    break  # a line cut off by a crash or the time limit
                if isinstance(ev, dict) and ev.pop("t", None) == token:  # lines the code under test wrote are ignored
                    events.append(ev)
    return events, proc


def test_timeout(steps: list[dict], step_tests: dict, n_examples: int) -> int:
    """The time limit of one test run: PKF_TEST_TIMEOUT_S, else 60 s plus 10 s per delivery case (in-memory S3) and 2 s
    per other case or example, at least 120 s."""
    env = os.environ.get("PKF_TEST_TIMEOUT_S")
    if env:
        try:
            return max(1, int(float(env)))
        except ValueError:
            raise PfError(f"PKF_TEST_TIMEOUT_S must be a number of seconds, found {env!r}", "invalid") from None
    ops = {s["id"]: s["op"] for s in steps}
    delivery = sum(len(c or []) for fn, c in step_tests.items() if ops.get(fn) == "custom_delivery")
    other = sum(len(c or []) for fn, c in step_tests.items() if ops.get(fn) != "custom_delivery")
    return max(120, 60 + 10 * delivery + 2 * (other + n_examples))


def _timed_out(job: dict, events: list[dict], results: list[dict], timeout: int) -> list[dict]:
    """After the time limit: the case or example in progress fails and names itself; the ones not started fail as not run."""
    def ident(e: dict) -> tuple:
        return ("example", e.get("example")) if e.get("example") is not None else ("case", e.get("step"), e.get("case"))
    finished = {ident(r) for r in results}
    started = [e for e in events if e.get("event") == "start"]
    out = []
    current = next((e for e in reversed(started) if ident(e) not in finished), None)
    if current is None:
        what = "loading the code" if not any(e.get("event") == "ready" for e in events) else "running the tests"
        out.append({"example": "all", "status": "fail", "detail": f"timed out after {timeout}s while {what}"})
    elif current.get("example") is not None:
        out.append({"example": current["example"], "status": "fail",
                    "detail": f"timed out after {timeout}s while running example {current['example']}"})
    else:
        out.append({"step": current["step"], "case": current["case"], "status": "fail",
                    "detail": f"timed out after {timeout}s while running {current['step']}:{current['case']}"})
    seen = {ident(e) for e in started}
    rest = [{"example": e["name"]} for e in job["examples"]] + [
        {"step": fn, "case": c.get("name")} for fn, cases in job["step_tests"].items() for c in cases or []]
    for r in rest:
        if ident(r) not in seen:
            out.append({**r, "status": "fail", "detail": "not run (an earlier case timed out)"})
    return out


def run_tests_isolated(kind: str, spec, module_file: Path, steps: list[dict], step_tests: dict, timeout: int | None = None,
                       info: dict | None = None) -> list[dict]:
    """Every example and every step test case, against the code file, in a separate Python process (isolated mode,
    UTF-8 mode, scrubbed environment, time limit `test_timeout`). `info` (optional) receives the runner's `loaded`
    event: the sha256 of the bytes that were tested. Results are written as they come, so a time-out keeps them."""
    job = {"kind": kind, "steps": steps, "params": spec.params, "examples": examples_of(spec), "step_tests": step_tests,
           "ops": {s["id"]: s["op"] for s in steps},
           "delivery": spec.delivery.model_dump(mode="json") if kind == "destination" else None}
    if timeout is None:
        timeout = test_timeout(steps, step_tests, len(job["examples"]))
    events, proc = _run_runner(module_file, job, timeout)
    loaded = next((e for e in events if e.get("event") == "loaded"), None)
    if info is not None and loaded:
        info.update(loaded)
    if loaded is not None:
        bad = runner_mismatch(loaded)
        if bad:
            return [{"example": "all", "status": "fail", "detail": bad, "code": "runner_mismatch"}]
    results = [{k: v for k, v in e.items() if k != "event"} for e in events if e.get("event") == "result"]
    if proc is None:
        return results + _timed_out(job, events, results, timeout)
    stderr = (proc.stderr or "")[-600:]
    if loaded is None:
        return [{"example": "all", "status": "fail", "detail": f"the test runner did not start: {stderr}"}]
    if not any(e.get("event") == "ready" for e in events):
        return [{"example": "all", "status": "fail", "detail": f"code failed to load: {stderr}"}]
    if not any(e.get("event") == "done" for e in events):
        return results + [{"example": "all", "status": "fail", "detail": f"the test runner stopped: {stderr}"}]
    return results


def _summ(results: list[dict]) -> list[dict]:
    return [{k: r.get(k) for k in ("example", "case", "step", "status", "detail") if r.get(k) is not None} for r in results]


def _before(kind: str, spec, steps: list[dict], i: int, done: dict[str, str] | None = None) -> list[dict]:
    """What each example looks like when it reaches step i (for the coder). The steps before i run with the custom
    functions already written (`done`, sources), in the isolated test runner; without any, in process as before.
    For a custom_delivery step this shows the computed transfers and the params the earlier steps added."""
    examples = examples_of(spec)
    earlier = {s["id"] for s in steps[:i] if s["op"] in CUSTOM_OPS}
    fns = {k: v for k, v in (done or {}).items() if k in earlier}
    if fns:
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
                mod = Path(d) / "earlier.py"
                mod.write_text("from __future__ import annotations\n\n" + "\n\n".join(f.strip("\n") for f in fns.values())
                               + "\n\nCUSTOM = {" + ", ".join(f'"{fn}": {fn}' for fn in fns) + "}\n", encoding="utf-8", newline="\n")
                events, _ = _run_runner(mod, {"kind": kind, "steps": steps, "params": spec.params, "examples": examples,
                                              "before": i}, timeout=120)
            states = {e["example"]: e["state"] for e in events if e.get("event") == "before"}
            if len(states) == len(examples):
                return [{"example": e["name"], "state": states[e["name"]]} for e in examples]
        except Exception:  # noqa: BLE001 - fall back to the view without the earlier code
            pass
    return [{"example": e["name"], "state": simulate.state_before(kind, steps, i, {}, e, spec.params)} for e in examples]


def _imports_rule(policy: str) -> str:
    if policy == "open":
        return ("Code policy: OPEN — any import is allowed (boto3, botocore, os, urllib.parse, …), placed INSIDE the function "
                "(the file holds nothing but the step functions). Prefer the clients and data the signature gives you.")
    return (f"Code policy: RESTRICTED — allowed imports inside the function: {sorted(ALLOWED_IMPORTS)}. Not allowed: open(), os, sys, "
            "subprocess, network, getattr/setattr, private attributes, module members like gzip.open.")


def coder_guidance(step: dict) -> str:
    """The 0.5.0 coder guidance, inserted right after the imports rule (exact text: PROMPT_CHANGES_0_5)."""
    from .llm.base import PROMPT_CHANGES_0_5

    one, posix, delivery = PROMPT_CHANGES_0_5["write_custom_step"]
    return one + posix + (delivery if step.get("op") == "custom_delivery" else "")


def _code_prompt(kind: str, spec, lock: dict, step: dict, i: int, policy: str = "restricted",
                 done: dict[str, str] | None = None) -> str:
    from .llm.base import fence

    k = KINDS[kind]
    return (f"Component kind: {kind}. Name: {spec.name}.\nPlan summary: {lock['plan']['summary']}\n\n"
            f"Write the custom step `{step['id']}`: {step['title']}\nSignature: {step_signature(kind, step)}\n{k.api}\n\n"
            f"Pseudocode (approved):\n{step['pseudocode']}\n\n"
            f"Approved test cases for this step (your code must pass them):\n{json.dumps(lock['plan']['step_tests'].get(step['id'], []), indent=1)}\n\n"
            f"{_imports_rule(policy)}{coder_guidance(step)}\nparams = run variables (strings): "
            f"{sorted(set(BUILTIN_VARS) | set(spec.params))}.\n\n"
            f"State reaching this step per example (from the steps before it):\n{json.dumps(_before(kind, spec, lock['plan']['steps'], i, done), indent=1, default=str)}\n\n"
            f"Full plan:\n{json.dumps(lock['plan']['steps'], indent=1)}\n\nExamples:\n{json.dumps(examples_of(spec), indent=1)}\n\n"
            f"{fence('requirement', (spec.requirement or '').strip())}\n"
            "The framework runs these test cases and the examples on your function after you answer; failures come back "
            "to you with your attempt (examples may show needs_code for other custom steps not written yet).\n")


def _try(kind: str, name: str, spec, lock: dict, done: dict[str, str], step_id: str, src: str,
         policy: str = "restricted") -> tuple[bool, dict]:
    """Guard + this step's approved cases + every example, for a candidate function (with the other steps' functions)."""
    step = next(s for s in lock["plan"]["steps"] if s["id"] == step_id)
    problems = guard(src, step_id, step_args(kind, step), policy)
    if problems:
        return False, {"guard_problems": problems}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        mod = Path(d) / "candidate.py"
        mod.write_text(render_module(kind, name, lock, _in_plan_order(lock, {**done, step_id: src})), encoding="utf-8", newline="\n")
        res = run_tests_isolated(kind, spec, mod, lock["plan"]["steps"], {step_id: lock["plan"]["step_tests"].get(step_id, [])})
    ok = all(r["status"] in ("pass", "shown", "needs_code") for r in res)
    report = {"results": _summ(res)}
    mismatch = next((r["detail"] for r in res if r.get("code") == "runner_mismatch"), None)
    if mismatch:
        report["runner_mismatch"] = mismatch
    warnings = guard_warnings(src) if policy == "open" else []
    if warnings and not ok:
        report["guard_warnings"] = warnings
    return ok, report


def _score(report: dict) -> tuple[int, int]:
    """Lower is better: guard-clean first, then the fewest failing results."""
    if report.get("guard_problems"):
        return 1, len(report["guard_problems"])
    return 0, sum(r.get("status") not in ("pass", "shown", "needs_code") for r in report.get("results", []))


def _in_plan_order(lock: dict, fns: dict[str, str]) -> dict[str, str]:
    return {i: fns[i] for i in custom_ids(lock["plan"]["steps"]) if i in fns}


def failing_steps(lock: dict, source: str) -> list[str]:
    """Custom steps whose function is missing from the file or that have a failing case or guard problem."""
    ids = custom_ids(lock["plan"]["steps"])
    fns = extract_functions(source, ids) or {}
    code = lock.get("code") or {}
    bad = {i for i in ids if i not in fns}
    bad |= {p.get("loc") for p in code.get("problems", []) if p.get("loc") in ids}
    bad |= {t.get("step") for t in code.get("tests", []) if t.get("step") in ids and t.get("status") not in ("pass", "shown")}
    return [i for i in ids if i in bad]


def generate_code(repo: Repo, kind: str, name: str, steps: list[str] | None = None, all_steps: bool = False) -> tuple[dict, list[Issue]]:
    """The coder writes custom steps; the framework runs the approved tests on every attempt and hands the failures
    back, up to PKF_CODER_ATTEMPTS times (default 4), and keeps the best attempt. Every attempt is a recorded,
    replayable call. Which steps: `steps` (exactly those), `all_steps`, or by default the steps whose function is missing
    or failing in the current file (all when none is, or when there is no current file). The module is written after
    each step, so a coder that stops (budget, turns, network) keeps the steps already done."""
    from .llm import run_structured
    from .llm.base import strip_fence
    from .llm.schemas import CustomStepCode

    st = status(repo, kind, name)
    if st["stage"] in ("needs_plan", "plan_stale", "plan_tampered", "plan_proposed") or st["mode"] != "requirement":
        raise PfError(f"the plan must be approved first: {st['next']}", "not_ready")
    spec = spec_of(repo, kind, name)
    lock = load_lock(repo, kind, name)
    plan_steps = lock["plan"]["steps"]
    ids = custom_ids(plan_steps)
    if not ids:
        raise PfError(f"{kind} '{name}' has no custom steps: nothing to code", "not_ready")
    cp = code_path(repo, kind, name)
    prev = lock.get("code") or {}
    current = cp.exists() and prev.get("plan_digest") == lock["plan_digest"]
    existing = (extract_functions(cp.read_text(encoding="utf-8"), ids) or {}) if current else {}
    if steps:
        unknown = [x for x in steps if x not in ids]
        if unknown:
            raise PfError(f"{unknown} {'is' if len(unknown) == 1 else 'are'} not custom steps of {kind} '{name}' (its custom steps: {ids})",
                          "usage")
        todo = [i for i in ids if i in steps]
    elif all_steps or not current:
        todo = list(ids)
    else:
        todo = failing_steps(lock, cp.read_text(encoding="utf-8")) or list(ids)
    attempts = max(1, int(os.environ.get("PKF_CODER_ATTEMPTS", "4")))
    policy = code_policy(repo.root)
    done: dict[str, str] = dict(existing)  # a step's function is replaced only when the coder produced a new one
    generated_by = {k: v for k, v in (prev.get("generated_by") or {}).items() if k in done} if current else {}
    stopped: tuple[str, PfError] | None = None
    best = None
    changed = False

    def write_module() -> None:
        module = render_module(kind, name, lock, _in_plan_order(lock, done))
        fns = extract_functions(module, list(done))
        if fns is not None and len(fns) == len(done):
            module = render_module(kind, name, lock, _in_plan_order(lock, fns))
        cp.write_text(module, encoding="utf-8", newline="\n")
        lock["code"] = {"file": cp.name, "plan_digest": lock["plan_digest"], "generated_by": dict(generated_by),
                        "generated_at": _now(), "code_policy": policy}
        lock.pop("code_approval", None)
        save_lock(repo, kind, name, lock)

    for i, s in enumerate(plan_steps):
        if s["id"] not in todo:
            continue
        base = _code_prompt(kind, spec, lock, s, i, policy, done)
        feedback, history, best = "", [], None  # best: (score, attempt, source, meta, explanation)
        for attempt in range(1, attempts + 1):
            try:
                code, meta = run_structured("write_custom_step", "writing-custom-step-code", base + feedback, CustomStepCode,
                                            log_dir=repo.root / KINDS[kind].folder / ".llm")
            except PfError as exc:
                stopped = (s["id"], exc)
                break
            src = strip_fence(code.function_source)
            ok, report = _try(kind, name, spec, lock, done, s["id"], src, policy)
            if report.get("runner_mismatch"):  # an environment error: no attempt can pass, stop at once (K17)
                stopped = (s["id"], PfError(report["runner_mismatch"], "runner_mismatch"))
                break
            score = _score(report)
            history.append({"attempt": attempt, "passed": ok, "failures": score[1]})
            if best is None or score < best[0]:
                best = (score, attempt, src, meta, code.explanation)
            if ok:
                break
            feedback = ("\n\nYOUR PREVIOUS ATTEMPT (attempt " + str(attempt) + ") FAILED. Fix it and answer again.\n```python\n"
                        + src + "\n```\nTest results:\n" + json.dumps(report, indent=1, default=str)[:12000] + "\n")
        if best is not None and not (stopped and stopped[1].code == "runner_mismatch"):
            done[s["id"]] = best[2]
            generated_by[s["id"]] = {**best[3].model_dump(exclude={"duration_s", "cost_usd", "turns", "provider"}),
                                     "explanation": best[4], "attempts": history, "kept_attempt": best[1]}
            write_module()
            changed = True
        if stopped:
            break
    if not changed and not current:
        write_module()  # no usable file yet: it holds what exists (missing steps are reported by the test)
    # otherwise an unchanged file (and its code approval) is left exactly as it was
    lock, issues = test_code(repo, kind, name)
    if stopped:
        sid, exc = stopped
        issues.append(Issue(file=f"{KINDS[kind].folder}/{name}.py", loc=sid, code=exc.code,
                            msg=f"the AI coder stopped at step {sid}: {exc}"))
    return lock, issues


def test_code(repo: Repo, kind: str, name: str) -> tuple[dict, list[Issue]]:
    spec = spec_of(repo, kind, name)
    lock = load_lock(repo, kind, name)
    cp = code_path(repo, kind, name)
    if not lock.get("code") or not cp.exists():
        raise PfError(f"no code yet: pkf code {kind} {name}", "not_ready")
    sha_before = hashlib.sha256(cp.read_bytes()).hexdigest()
    src = cp.read_text(encoding="utf-8")
    policy = code_policy(repo.root)
    by_id = {s["id"]: s for s in lock["plan"]["steps"]}
    problems = [{"loc": "file", "msg": m} for m in structure_problems(kind, name, lock, src)]
    if not problems:
        for fn, fsrc in (extract_functions(src, custom_ids(lock["plan"]["steps"])) or {}).items():
            problems += [{"loc": fn, "msg": m} for m in guard(fsrc, fn, step_args(kind, by_id[fn]), policy)]
    loaded: dict = {}
    tests = (run_tests_isolated(kind, spec, cp, lock["plan"]["steps"], lock["plan"]["step_tests"], info=loaded) if not problems else
             [{"example": e.name, "status": "fail", "detail": "not run: fix the problems first"} for e in spec.examples])
    sha_now = hashlib.sha256(cp.read_bytes()).hexdigest()
    sha = loaded.get("sha256") or sha_before  # the bytes that were checked and tested, never a later edit
    if sha != sha_before or sha != sha_now:
        problems.append({"loc": "file", "msg": "the file changed while it was being tested; run the tests again"})
    issues = [Issue(file=f"{KINDS[kind].folder}/{name}.py", loc=p["loc"], code="guard", msg=p["msg"]) for p in problems]
    if policy == "open":
        for fn, fsrc in (extract_functions(src, custom_ids(lock["plan"]["steps"])) or {}).items():
            issues += [Issue(severity="warning", file=f"{KINDS[kind].folder}/{name}.py", loc=fn, code="posix_paths", msg=m)
                       for m in guard_warnings(fsrc)]
    for r in tests:
        if r["status"] not in ("pass", "shown"):
            issues.append(Issue(file=f"{KINDS[kind].folder}/{name}.py", loc=r.get("example") or f"{r.get('step')}:{r.get('case')}",
                                code=r.get("code") or "test_failed", msg=f"{r.get('example') or r.get('case')}: {r.get('detail')}"))
    lock["code"].update(sha256=sha, problems=problems, tests=_summ(tests), tested_at=_now(), code_policy=policy)
    save_lock(repo, kind, name, lock)
    return lock, issues


def approve_code(repo: Repo, kind: str, name: str, by: str, evidence: dict | None = None) -> dict:
    st = status(repo, kind, name)
    if st["stage"] not in ("code_generated", "approved") or not (load_lock(repo, kind, name).get("code")):
        raise PfError(f"code cannot be approved now ({st['stage']}): {st['next']}", "not_ready")
    _check_handle(by)
    lock = load_lock(repo, kind, name)
    sha = hashlib.sha256(code_path(repo, kind, name).read_bytes()).hexdigest()
    lock["code_approval"] = approval_record(repo.root, by, code_digest(lock, sha), evidence)
    save_lock(repo, kind, name, lock)
    return lock["code_approval"]


# ---------------------------------------------------------------------------------------------- show
def show(repo: Repo, kind: str, name: str) -> str:
    spec = spec_of(repo, kind, name)
    st = status(repo, kind, name)
    out = [f"{kind.capitalize()} {name} — {st['stage']} ({st['mode']})", ""]
    if st["mode"] != "requirement":
        steps = peek_steps(repo, kind, name) or []
        out += ["Steps (no AI design):", *[f"  {i}. [{s['op']}] {s.get('title') or s['id']}  {json.dumps(s['params'], sort_keys=True)}"
                                           for i, s in enumerate(steps, 1)]]
        res = run_examples_inprocess(kind, spec, steps)
    else:
        lock = load_lock(repo, kind, name)
        out += ["Requirement:", *("  " + ln for ln in (spec.requirement or "").strip().splitlines()), ""]
        if not lock.get("plan"):
            return "\n".join(out + [f"No plan yet. Run: pkf plan {kind} {name}"])
        p = lock["plan"]
        out += ["Plan:", "  " + p["summary"], ""]
        for i, s in enumerate(p["steps"], 1):
            out.append(f"  {i}. [{s['op']}] {s['title']}   (id: {s['id']})")
            if s["op"] in CUSTOM_OPS:
                out += ["       " + ln for ln in s.get("pseudocode", "").splitlines()]
                for c in p["step_tests"].get(s["id"], []):
                    out.append(f"       test {c['name']}: {c['purpose']}  →  {'refused' if c['expect_error'] else json.dumps(c['expected'])[:160]}")
            elif s["params"]:
                out.append("       " + json.dumps(s["params"], sort_keys=True))
        if p.get("assumptions"):
            out += ["", "Assumptions:", *("  - " + a for a in p["assumptions"])]
        if p.get("ambiguities"):
            out += ["", "OPEN QUESTIONS (answer in the requirement, then plan again):", *("  ? " + a for a in p["ambiguities"])]
        if lock.get("issues"):
            out += ["", "Plan issues:", *(f"  [{str(i.get('severity', 'error')).upper()}] {i.get('loc') or ''}: {i.get('msg')}"
                                          for i in lock["issues"] if isinstance(i, dict))]
        res = (lock.get("code") or {}).get("tests") or lock.get("simulation") or []
    out += ["", "Examples and tests:"]
    for r in res:
        out.append(f"  [{r['status'].upper()}] {r.get('example') or (str(r.get('step')) + ':' + str(r.get('case')))} — {r.get('detail', '')}")
        if r.get("output") is not None:
            out.append("     output: " + json.dumps(r["output"], default=str)[:400])
    return "\n".join(out)



