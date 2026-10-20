"""Split a whole-pipeline requirement into its four components (the decomposer agent), for a person to approve.

    pipelines/<p>.yaml requirement
      → SPLIT (decomposer agent, strict schema): trigger / source / transformation / destination, each with settings,
        its own self-contained requirement and examples, plus end-to-end pipeline examples and open questions
      → CHECK (deterministic): every drafted spec validates against its model; values the text does not give stay
        "<REQUIRED>" for the owner to fill in
      → a person approves the split → the component files are written and the pipeline is wired to them
    then each component goes through its own plan → approve → code → approve (pkf status shows what is next).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from pkf_runtime import discovery, events, layout
from pkf_runtime.ops import OPS

from . import design, yamlio
from .envelope import Issue, PfError
from .repo import FOLDERS, MODELS, Repo
from .specs.common import REQUIRED, without_new_defaults

KIND_ORDER = ("trigger", "source", "transformation", "destination")

SETTINGS_ADDITIONS = {  # inserted into SETTINGS_FORMAT after the fixtures were recorded (listed for re-keying, llm.base)
    "trigger": ("\n              (a control list read from the Self-Service Portal, one record per item, with results written back: give the "
                "trigger a requirement; its planner uses the selfservice, selfservice_due and data_vars steps; params "
                "{\"self_service_base_url\", \"form_id\"}, credentials with the field api_key)"),
    "source": ("\n              {\"type\": \"entimice\", \"connection\": {\"base_url\": \"https://…\", \"account_by\": \"{…}\" | null, "
               "\"accounts\": {\"<value>\": \"<secret ref>\"}}, \"credentials\": {\"provider\": \"vault\", \"ref\": \"…\"}, \"params\": {}}"),
    "destination": ("\n               a bucket per package: \"connection\": {\"bucket_by\": \"{…}\", \"bucket_map\": {\"<value>\": \"<bucket>\"}, "
                    "\"region\": \"…\"} with \"delivery\": {\"mode\": \"stream\", \"if_exists\": \"overwrite\" | \"fail\", "
                    "\"content_types\": {\".pdf\": \"application/pdf\"}} (each file streamed from the source; entimice sources)"),
}

SETTINGS_FORMAT = """Settings (config_json) by kind — use only these keys; put "<REQUIRED>" where the requirement does not say:
trigger:      {"schedule": "*/5 * * * *" | "@daily" | null, "connection": {"region": "eu-west-1"}, "params": {"queue_url": "https://sqs…"}}""" + SETTINGS_ADDITIONS["trigger"] + """
source:       {"type": "s3", "connection": {"bucket": "…", "region": "…"}, "credentials": {"provider": "none"}, "params": {}}
              {"type": "https", "connection": {"base_url": "https://…", "listing": "html_index"|"webdav"|"json_index", "auth": "none"|"basic"|"bearer"},
               "credentials": {"provider": "aws_secrets_manager"|"vault"|"local_file"|"env"|"none", "ref": "…"}, "params": {}}
              {"type": "sftp", "connection": {"host": "…", "port": 22, "host_key": "ssh-ed25519 …"}, "credentials": {…}, "params": {}}
              {"type": "ftp", "connection": {"host": "…", "tls": "explicit"}, "credentials": {…}, "params": {}}
              {"type": "local", "connection": {"base_path": "…"}, "params": {}}""" + SETTINGS_ADDITIONS["source"] + """
transformation: {"params": {}}
destination:  {"type": "s3", "connection": {"bucket": "…", "region": "…"}, "credentials": {"provider": "none"},
               "delivery": {"if_exists": "skip_identical", "server_side_encryption": "aws:kms" | "AES256" | null, "write_success_marker": false},
               "params": {}}     ({"type": "local", "connection": {"base_path": "…"}, …} for development)""" + SETTINGS_ADDITIONS["destination"]

EXAMPLE_FORMAT = """Examples (examples_json) by kind — structure only, never data:
trigger:        {"name", "messages": [message bodies as JSON objects or strings], "conf": null, "params": {},
                 "expected_events": [{"keys": [object keys], "vars": {a subset}}], "expect_error": false}
source:         {"name", "event": {"objects": [{"key": "…", "bucket": "…"}], "vars": {}} | null, "listing": ["path:size" from the TOP of the
                 source: for S3 the full object key including every prefix, e.g. "incoming/S1/a.csv:10"; a key ending in "/" is a folder marker],
                 "contents": {}, "params": {}, "expected_packages": [[paths inside each package]], "expect_error": false}
transformation: {"name", "input": ["path[:size]"], "params": {}, "expected_output": [paths, archive members as "x.zip!/inner"], "expect_error": false}
destination:    {"name", "files": [package paths], "vars": {"package": "…", …}, "params": {}, "expected_keys": [object keys], "expect_error": false}
pipeline (pipeline_examples_json): {"name", "messages": [...], "conf": null, "listing": ["path:size"], "params": {},
                 "expected_keys": [every delivered object key, without _SUCCESS], "expect_error": false}
Trigger messages must look exactly like the real ones. An S3 event notification body:
  {"Records": [{"eventVersion": "2.1", "eventSource": "aws:s3", "eventName": "ObjectCreated:Put", "eventTime": "2024-03-01T10:00:00.000Z",
                "s3": {"bucket": {"name": "<bucket>"}, "object": {"key": "<URL-encoded key, '+' for space>", "size": 123}}}]}
  the test event S3 sends when notifications are configured: {"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": "<bucket>"}
  through SNS: {"Type": "Notification", "Message": "<the S3 body above as a JSON string>"}
Variables in examples: built-ins use sample values ds=2024-03-01, ds_nodash=20240301, source/pipeline names are the real
component names (source = the source component's name). S3 event variables: """ + ", ".join("{" + v + "}" for v in events.S3_EVENT_VARS)


def _catalogues() -> str:
    lines = []
    for kind, ops in (("trigger", events.TRIGGER_OPS), ("source", discovery.SOURCE_OPS), ("transformation", OPS),
                      ("destination", layout.DESTINATION_OPS)):
        lines.append(f"{kind}: " + "; ".join(f"{n} — {o.summary}" for n, o in ops.items()))
    return "\n".join(lines)


def _existing(repo: Repo, pipeline: str) -> list[dict]:
    """Components other pipelines own (this pipeline's own earlier drafts are rewritten, never reused)."""
    lock = load_lock(repo, pipeline)
    own = {tuple(x) for x in lock.get("owned", [])}
    own |= {(k, d["name"]) for k, d in ((lock.get("split") or {}).get("components") or {}).items() if not d.get("reuse")}
    out = []
    for kind in KIND_ORDER:
        for name, spec in repo.specs[kind].items():
            if (kind, name) in own:
                continue
            d = without_new_defaults(spec.model_dump(mode="json", exclude={"requirement", "examples", "steps", "credentials", "api_version"}))
            out.append({"kind": kind, "name": name, "settings": d, "has_requirement": bool(getattr(spec, "requirement", None))})
    return out


def _ex_sha(examples: list) -> str:
    """Digest of pipeline examples in their validated form (defaults filled), so YAML round trips compare equal."""
    from .specs.design import PipelineExample

    try:
        norm = [PipelineExample.model_validate(e).model_dump(mode="json") for e in examples]
    except ValidationError:
        norm = examples
    return design._sha(norm)


def _owner_examples(repo: Repo, name: str) -> list[dict]:
    """The pipeline's examples unless an earlier split approval wrote them."""
    ex = [e.model_dump(mode="json") for e in repo.get("pipeline", name).examples]
    return [] if ex and _ex_sha(ex) == load_lock(repo, name).get("written_examples_sha256") else ex


def _prompt(repo: Repo, name: str) -> str:
    from .llm.base import fence

    p = repo.get("pipeline", name)
    return (f"Pipeline: {name}\nOwner: {p.owner}\n\nSplit the requirement into trigger, source, transformation and destination.\n\n"
            f"Operation catalogues (what each component's planner can use later):\n{_catalogues()}\n\n"
            f"{SETTINGS_FORMAT}\n\n{EXAMPLE_FORMAT}\n\n"
            f"Existing components you may reuse (reuse_existing=true, same name):\n{json.dumps(_existing(repo, name), indent=1)}\n\n"
            + (f"End-to-end examples written by the owner (they are kept as the pipeline's examples; your parts and their "
               f"examples must be consistent with them):\n{json.dumps(_owner_examples(repo, name), indent=1)}\n\n"
               if _owner_examples(repo, name) else "")
            + f"{fence('requirement', (p.requirement or '').strip())}\n")


def lock_path(repo: Repo, name: str) -> Path:
    return repo.root / FOLDERS["pipeline"] / f"{name}.lock.yaml"


def load_lock(repo: Repo, name: str) -> dict:
    p = lock_path(repo, name)
    return yamlio.to_plain(yamlio.load(p)) if p.exists() else {}


def _save(repo: Repo, name: str, lock: dict) -> None:
    header = (f"# pipelines/{name}.lock.yaml — the AI's split of the pipeline requirement and its approval.\n"
              "# Written by `pkf plan pipeline` / `pkf approve-plan pipeline`. Do not edit by hand.\n")
    lock_path(repo, name).write_text(header + yamlio.dumps(json.loads(json.dumps(lock))), encoding="utf-8", newline="\n")


def _req_sha(p) -> str:
    return design._sha({"requirement": (p.requirement or "").strip()})


def _draft_spec(kind: str, d, issues: list[Issue], loc: str) -> dict | None:
    from .llm.base import strip_fence

    try:
        cfg = json.loads(strip_fence(d.config_json or "{}") or "{}")
        exs = json.loads(strip_fence(d.examples_json or "[]") or "[]")
    except json.JSONDecodeError as exc:
        issues.append(Issue(file=loc, msg=f"{kind}: settings/examples are not JSON ({exc})", code="invalid_split"))
        return None
    if not isinstance(cfg, dict) or not isinstance(exs, list):
        issues.append(Issue(file=loc, msg=f"{kind}: settings must be an object and examples a list", code="invalid_split"))
        return None
    cfg = {k: v for k, v in cfg.items() if k not in ("api_version", "kind", "name", "requirement", "examples", "steps")}
    spec = {"api_version": "pkf/v1", "kind": kind, "name": d.name, "description": f"from the {kind} part of the pipeline requirement", **cfg}
    if d.requirement.strip():
        spec["requirement"] = d.requirement.strip() + "\n"
        spec["examples"] = exs
    if kind == "transformation" and "requirement" not in spec:
        issues.append(Issue(file=loc, msg="a transformation needs a requirement", code="invalid_split"))
    return spec


def _validate(kind: str, spec: dict) -> list[str]:
    probe = json.loads(json.dumps(spec).replace(REQUIRED, "required-value"))
    try:
        MODELS[kind].validate_python(probe)
    except ValidationError as exc:
        return [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]]
    return []


def _flat(node, loc=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _flat(v, f"{loc}.{k}" if loc else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _flat(v, f"{loc}.{i}")
    else:
        yield loc, node


def _pipeline_examples(raw: str, loc: str, issues: list[Issue]) -> list:
    """The split's end-to-end examples, checked as the pipeline file will check them (A5): a list of valid pipeline
    examples with unique names. Problems are invalid_split errors, so the repair round sees them."""
    from .llm.base import strip_fence
    from .specs.design import PipelineExample

    try:
        pex = json.loads(strip_fence(raw or "[]") or "[]")
    except json.JSONDecodeError:
        issues.append(Issue(file=loc, loc="split.pipeline_examples", msg="pipeline examples are not JSON", code="invalid_split"))
        return []
    if not isinstance(pex, list):
        issues.append(Issue(file=loc, loc="split.pipeline_examples", code="invalid_split",
                            msg="pipeline examples must be a JSON list of examples"))
        return []
    names = []
    for i, e in enumerate(pex):
        try:
            PipelineExample.model_validate(e)
        except ValidationError as exc:
            for err in exc.errors()[:3]:
                issues.append(Issue(file=loc, loc=f"split.pipeline_examples.{i}", code="invalid_split",
                                    msg=f"pipeline example {i}: {'.'.join(str(x) for x in err['loc'])}: {err['msg']}"))
            continue
        names.append(e["name"])
    for n in sorted({n for n in names if names.count(n) > 1}):
        issues.append(Issue(file=loc, loc="split.pipeline_examples", code="invalid_split",
                            msg=f"pipeline example name '{n}' is used twice (names must be unique)"))
    return pex


def _assemble(repo: Repo, out, loc: str) -> tuple[dict, list[Issue], list]:
    """The split's draft specs and examples from the model's answer, with every validation problem as an issue."""
    issues: list[Issue] = []
    drafts = {}
    for kind in KIND_ORDER:
        d = getattr(out, kind)
        if kind == "transformation" and not out.has_transformation:
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", d.name):
            issues.append(Issue(file=loc, loc=f"split.{kind}", msg=f"component name '{d.name}' is not snake_case", code="invalid_split"))
            continue
        if d.reuse_existing:
            if d.name not in repo.specs[kind]:
                issues.append(Issue(file=loc, msg=f"{kind} '{d.name}' marked for reuse does not exist", code="invalid_split"))
            drafts[kind] = {"name": d.name, "reuse": True}
            continue
        spec = _draft_spec(kind, d, issues, loc)
        if spec is None:
            continue
        for m in _validate(kind, spec):
            issues.append(Issue(file=loc, loc=f"split.{kind}", msg=m, code="invalid_split"))
        drafts[kind] = {"name": d.name, "reuse": False, "spec": spec}
    pex = _pipeline_examples(out.pipeline_examples_json, loc, issues)
    trig = drafts.get("trigger") or {}
    if not trig.get("reuse") and trig.get("spec") is not None and not trig["spec"].get("requirement"):  # a plain schedule
        used = set(re.findall(r"\{(\w+)", out.package_name or ""))
        if any(v.startswith("event_") for v in used):
            issues.append(Issue(file=loc, loc="split.package_name", code="invalid_split",
                                msg=f"package_name '{out.package_name}' uses event variables, but the trigger is a plain schedule "
                                    "(no events): use built-ins such as {source}_{ds_nodash} or a variable the source's split sets"))
    return drafts, issues, pex


def plan(repo: Repo, name: str) -> tuple[dict, list[Issue]]:
    from .llm import run_structured
    from .llm.base import redact_secrets, redaction_warning
    from .llm.schemas import PipelineSplit

    p = repo.get("pipeline", name)
    if not p.requirement:
        raise PfError(f"pipeline '{name}' has no requirement to split", "not_ready")
    prompt = _prompt(repo, name)
    loc = f"pipelines/{name}.lock.yaml"
    for attempt in range(2):  # one repair round: the model sees what was wrong with its first answer
        out, meta = run_structured("decompose_pipeline", "decomposing-pipeline-requirements", prompt, PipelineSplit,
                                   log_dir=repo.root / "pipelines" / ".llm")
        drafts, issues, pex = _assemble(repo, out, loc)
        bad = [i for i in issues if i.severity == "error"]
        if not bad or attempt == 1:
            break
        prompt = (_prompt(repo, name) + "\n\nYour previous answer was rejected by the framework's validation. Answer again, "
                  "fixing exactly these problems and keeping everything else:\n" + "\n".join(f"- {i.loc or ''}: {i.msg}" for i in bad) + "\n")
    holes = sorted({f"{k}.{h}" for k, d in drafts.items() if not d["reuse"] for h, v in _flat(d["spec"]) if isinstance(v, str) and REQUIRED in v})
    errors = [i.model_dump() for i in issues if i.severity == "error"]
    split = {"summary": out.summary, "components": drafts, "errors": errors, "package_name": out.package_name, "pipeline_examples": pex,
             "assumptions": out.assumptions, "ambiguities": out.ambiguities, "to_fill_in": holes}
    owned = load_lock(repo, name).get("owned", [])
    lock = {"lock_version": 1, "pipeline": name, "requirement_sha256": _req_sha(p), "split": split, "owned": owned,
            "split_by": meta.model_dump(exclude={"duration_s", "cost_usd", "turns", "provider"})}
    lock["split_digest"] = design._sha(split)
    if out.ambiguities:
        issues.append(Issue(severity="warning", file=repo.file_of("pipeline", name), loc="requirement", code="ambiguous",
                            msg="the owner must clarify: " + " | ".join(out.ambiguities)))
    n_redacted = redact_secrets(_prompt(repo, name))[1]
    if n_redacted:
        issues.append(Issue(severity="warning", file=repo.file_of("pipeline", name), loc="requirement", code="redacted",
                            msg=redaction_warning(n_redacted)))
    _save(repo, name, lock)
    return lock, issues


def status(repo: Repo, name: str) -> dict:
    p = repo.get("pipeline", name)
    if not p.requirement:
        return {"stage": "no_requirement", "next": "components are wired by hand"}
    lock = load_lock(repo, name)
    if not lock.get("split"):
        return {"stage": "needs_split", "next": f"pkf plan pipeline {name}"}
    if lock["requirement_sha256"] != _req_sha(p):
        return {"stage": "split_stale", "next": f"the requirement changed: pkf plan pipeline {name}"}
    if lock.get("split_digest") != design._sha(lock["split"]):
        return {"stage": "split_tampered", "next": f"the lock was edited: pkf plan pipeline {name}"}
    a = lock.get("split_approval")
    if not a or a.get("digest") != lock["split_digest"]:
        return {"stage": "split_proposed", "next": f"review `pkf show pipeline {name}`, then a person runs `pkf approve-plan pipeline {name} --by @handle`"}
    return {"stage": "split_approved", "next": "plan each component (pkf status)"}


def approve(repo: Repo, name: str, by: str, force: bool = False, evidence: dict | None = None) -> dict:
    st = status(repo, name)
    if st["stage"] not in ("split_proposed", "split_approved"):
        raise PfError(f"nothing to approve: {st['next']}", "not_ready")
    lock = load_lock(repo, name)
    s = lock["split"]
    if s["ambiguities"]:
        raise PfError("the split lists open questions; answer them in the pipeline requirement and plan again", "ambiguous")
    if s.get("errors"):
        raise PfError("the split has errors (see `pkf show pipeline`); plan again", "invalid_split",
                      [Issue(**e) for e in s["errors"]])
    design._check_handle(by)
    owned = {tuple(x) for x in lock.get("owned", [])}
    writes: list[tuple[Path, str]] = []

    def plan_write(path: Path, text: str, kind: str, cname: str) -> None:
        root = (repo.root / FOLDERS[kind]).resolve()
        if path.resolve().parent != root:
            raise PfError(f"refusing to write outside {FOLDERS[kind]}/: {path}", "invalid_split")
        if path.exists() and path.read_text(encoding="utf-8") != text and (kind, cname) not in owned:
            raise PfError(f"{path.relative_to(repo.root)} belongs to another pipeline or was written by hand; rename the component "
                          "in the requirement (files this pipeline's earlier splits created are rewritten)", "conflict")
        writes.append((path, text))

    for kind in KIND_ORDER:
        d = s["components"].get(kind)
        if not d or d["reuse"]:
            continue
        spec = d["spec"]
        target = repo.root / FOLDERS[kind] / f"{d['name']}.yaml"
        if target.exists() and (kind, d["name"]) in owned:  # keep what the owner filled in for <REQUIRED> (buckets …)
            spec = _keep_filled(spec, yamlio.to_plain(yamlio.load(target)) or {})
        text = f"# {kind} `{d['name']}`: written from the approved split of pipeline `{name}` ({by}). Fill in every <REQUIRED>.\n" + yamlio.dumps(spec)
        plan_write(repo.root / FOLDERS[kind] / f"{d['name']}.yaml", text, kind, d["name"])
    src, dst = s["components"]["source"]["name"], s["components"]["destination"]["name"]
    conn_name = f"{name}_connection"
    conn_doc = {"api_version": "pkf/v1", "kind": "connection", "name": conn_name, "source": src, "destination": dst}
    conn = repo.root / FOLDERS["connection"] / f"{conn_name}.yaml"
    if conn.exists():  # keep the owner's settings (check_before_run …); only the two ends follow the split
        old = yamlio.to_plain(yamlio.load(conn))
        conn_doc = {**old, "source": src, "destination": dst}
    plan_write(conn, yamlio.dumps(conn_doc), "connection", conn_name)
    pf = repo.root / FOLDERS["pipeline"] / f"{name}.yaml"
    doc = yamlio.load(pf)
    have = yamlio.to_plain(doc.get("examples")) or []
    # the owner's end-to-end examples win (--force: the split's); examples an earlier split wrote are not the owner's
    keep_examples = bool(have) and not force and _ex_sha(have) != lock.get("written_examples_sha256")
    written = []
    for path, text in writes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(str(path.relative_to(repo.root)))
    # drafts an earlier split of this pipeline created and the new split no longer uses (renamed components)
    now = {(k, d["name"]) for k, d in s["components"].items() if not d["reuse"]} | {("connection", conn_name)}
    used = {(k, n) for k, n in _references(repo, exclude_pipeline=name)}
    removed = []
    for kind, cname in sorted(owned - now - used):
        if kind not in FOLDERS or kind in ("pipeline", "connection"):
            continue
        for suffix in (".yaml", ".lock.yaml", ".py"):
            f = repo.root / FOLDERS[kind] / f"{cname}{suffix}"
            if f.exists():
                f.unlink()
                removed.append(str(f.relative_to(repo.root)))
    owned = {o for o in owned if o in now or o in used}
    doc["connection"] = conn_name
    doc["trigger"] = s["components"]["trigger"]["name"]
    doc.pop("schedule", None)
    doc["transformation"] = (s["components"].get("transformation") or {}).get("name")
    doc["package_name"] = s["package_name"]
    if not keep_examples:
        doc["examples"] = s["pipeline_examples"]
        lock["written_examples_sha256"] = _ex_sha(s["pipeline_examples"])
    yamlio.dump(doc, pf)
    lock["owned"] = sorted(owned | {(k, d["name"]) for k, d in s["components"].items() if not d["reuse"]} | {("connection", conn_name)})
    lock["split_approval"] = design.approval_record(repo.root, by, lock["split_digest"], evidence)
    _save(repo, name, lock)
    return {"approval": lock["split_approval"], "written": written, "removed": removed, "to_fill_in": s["to_fill_in"]}


def _keep_filled(new, old):
    """The split's spec, with every "<REQUIRED>" the owner has since filled in taken from the existing file."""
    if new == REQUIRED and isinstance(old, (str, int, float)) and old != REQUIRED:
        return old
    if isinstance(new, dict) and isinstance(old, dict):
        return {k: _keep_filled(v, old.get(k)) for k, v in new.items()}
    if isinstance(new, list) and isinstance(old, list) and len(new) == len(old):
        return [_keep_filled(a, b) for a, b in zip(new, old)]
    return new


def _references(repo: Repo, exclude_pipeline: str) -> set[tuple[str, str]]:
    """Components that other pipelines (and their connections) use."""
    out: set[tuple[str, str]] = set()
    for pname, p in repo.specs["pipeline"].items():
        if pname == exclude_pipeline:
            continue
        if p.trigger:
            out.add(("trigger", p.trigger))
        if p.transformation:
            out.add(("transformation", p.transformation))
        c = repo.specs["connection"].get(p.connection) if p.connection else None
        if c is not None:
            out |= {("connection", c.name), ("source", c.source), ("destination", c.destination)}
    return out


def show(repo: Repo, name: str) -> str:
    lock = load_lock(repo, name)
    st = status(repo, name)
    out = [f"Pipeline {name} — {st['stage']}", ""]
    if not lock.get("split"):
        return "\n".join(out + [st["next"]])
    s = lock["split"]
    out += ["Split:", "  " + s["summary"], ""]
    for kind in KIND_ORDER:
        d = s["components"].get(kind)
        if not d:
            out.append(f"  {kind}: none")
            continue
        if d["reuse"]:
            out.append(f"  {kind}: reuse existing `{d['name']}`")
            continue
        spec = d["spec"]
        out.append(f"  {kind} `{d['name']}`:")
        settings = {k: v for k, v in spec.items() if k not in ("api_version", "kind", "name", "description", "requirement", "examples")}
        out.append("     settings: " + json.dumps(settings, sort_keys=True)[:500])
        if spec.get("requirement"):
            out += ["     requirement: " + ln for ln in spec["requirement"].strip().splitlines()]
            out.append(f"     examples: {len(spec.get('examples', []))} — " + ", ".join(e.get("name", "?") for e in spec.get("examples", [])))
        else:
            out.append("     (plain settings, no AI plan needed)")
    owner_examples = _owner_examples(repo, name)
    out += ["", f"Package name: {s['package_name']}",
            (f"End-to-end examples: the owner's {len(owner_examples)} are kept (approve with --force to use the split's "
             f"{len(s['pipeline_examples'])} instead)") if owner_examples else
            f"End-to-end examples: {len(s['pipeline_examples'])} — " + ", ".join(e.get("name", "?") for e in s["pipeline_examples"])]
    if s["assumptions"]:
        out += ["", "Assumptions:", *("  - " + a for a in s["assumptions"])]
    if s["ambiguities"]:
        out += ["", "OPEN QUESTIONS (answer in the requirement, then plan again):", *("  ? " + a for a in s["ambiguities"])]
    if s["to_fill_in"]:
        out += ["", "To fill in after approval (not in the requirement):", *("  - " + h for h in s["to_fill_in"])]
    return "\n".join(out)
