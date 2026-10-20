"""`pkf` command line. With --json (before the command) every command prints exactly one Result envelope.
Exit codes: 0 ok · 1 the command ran but found problems · 2 usage or environment error."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from .envelope import Issue, PfError, Result


def _repo():
    from .repo import find_root, load

    return load(find_root())


def agent_session() -> bool:
    """True inside Claude Code / Agent SDK sessions: approvals are a person's decision."""
    return bool(os.environ.get("CLAUDECODE") or os.environ.get("PKF_AGENT_SESSION"))


def cmd_init(a) -> Result:
    root = Path(a.path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    from .repo import FOLDERS

    for folder in FOLDERS.values():
        (root / folder).mkdir(exist_ok=True)
    if not (root / "pkf.toml").exists():
        (root / "pkf.toml").write_text(
            '# package-factory specs repository\nspec_api_version = "pkf/v1"\n\n[staging]\n'
            '# where packages wait between Airflow tasks: s3://bucket/prefix in production (add a lifecycle rule),\n'
            '# file:///path for a single-machine development Airflow. PKF_STAGING_URL overrides it at run time.\n'
            'url = "file:///tmp/pkf-staging"\n\n[airflow]\ndags_folder = "dags"\n\n[agent]\n'
            '# open: generated code may import and call anything (boto3, os, …) — reviewed and approved by people.\n'
            '# restricted: only pure-Python modules (re, json, zipfile, …) pass the static guard.\n'
            'code_policy = "open"\n', encoding="utf-8", newline="\n")
    gi = root / ".gitignore"
    lines = [".venv/", "__pycache__/", "*.pyc", "*/.llm/", "local-secrets/"]
    have = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    gi.write_text("\n".join(have + [x for x in lines if x not in have]) + "\n", encoding="utf-8", newline="\n")
    return Result(command="init", ok=True, data={"root": str(root)})


def cmd_new(a) -> Result:
    from .repo import find_root
    from .scaffold import new

    root = find_root()
    path = new(root, a.kind, a.name, a.type, a.force)
    return Result(command="new", ok=True, data={"file": str(path.relative_to(root))})


DESIGNED = ("trigger", "source", "transformation", "destination")


def _designed_status(repo) -> list[Issue]:
    from . import design

    out = []
    for kind in DESIGNED:
        for name in repo.specs[kind]:
            try:
                st = design.status(repo, kind, name)
            except PfError as exc:
                st = {"stage": exc.code, "next": str(exc)}
            if st["stage"] != "approved":
                out.append(Issue(severity="warning", file=repo.file_of(kind, name), code=st["stage"], msg=st["next"]))
    return out


def cmd_validate(a) -> Result:
    repo = _repo()
    issues = list(repo.issues)
    errors = [i for i in issues if i.severity == "error" and not (a.allow_unfilled and i.code == "required")]
    issues += _designed_status(repo)
    counts = {k: len(v) for k, v in repo.specs.items()}
    return Result(command="validate", ok=not errors, issues=issues, data={"components": counts,
                                                                           "unfilled": sum(i.code == "required" for i in issues)})


def cmd_list(a) -> Result:
    from . import design

    repo = _repo()
    data = {k: sorted(v) for k, v in repo.specs.items()}
    data["design_status"] = {f"{k}/{n}": design.status(repo, k, n)["stage"] for k in DESIGNED for n in repo.specs[k]}
    return Result(command="list", ok=True, data=data)


def cmd_catalog(a) -> Result:
    from pkf_runtime.ops import catalogue

    from .design import KINDS
    from .specs.common import BUILTIN_VARS
    from .specs.destination import DESTINATION_TYPES
    from .specs.source import SOURCE_TYPES

    return Result(command="catalog", ok=True, data={
        "source_types": list(SOURCE_TYPES), "destination_types": list(DESTINATION_TYPES),
        "credential_providers": ["aws_secrets_manager", "vault", "local_file", "env", "none"], "template_variables": BUILTIN_VARS,
        "operations": {k: catalogue(d.ops) for k, d in KINDS.items()}})


def cmd_schema(a) -> Result:
    from .repo import MODELS

    out = {k: m.json_schema() for k, m in MODELS.items()}
    if a.write:
        d = Path(a.write)
        d.mkdir(parents=True, exist_ok=True)
        for k, s in out.items():
            (d / f"{k}.schema.json").write_text(json.dumps(s, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        return Result(command="schema", ok=True, data={"written": sorted(out)})
    return Result(command="schema", ok=True, data=out)


def _approval_guard(what: str) -> None:
    if agent_session():
        raise PfError(f"approving {what} is a person's decision; agents cannot run it", "forbidden")


def _evidence(a) -> dict | None:
    url = getattr(a, "evidence", None)
    if not url:
        return None
    if not url.startswith(("https://", "http://")):
        raise PfError("--evidence must be the URL of the pull request review or comment that approved", "usage")
    return {"url": url, "github_id": getattr(a, "github_id", None), "head_sha": getattr(a, "head_sha", None)}


def _generate_code(design, repo, kind: str, name: str, steps: list[str] | None, all_steps: bool):
    """`design.generate_code(..., steps=, all_steps=)` (A8). Without --step/--all the plain call is made, so the
    design module decides the default (missing or failing steps first)."""
    if not steps and not all_steps:
        return design.generate_code(repo, kind, name)
    import inspect

    params = inspect.signature(design.generate_code).parameters
    if "steps" not in params or "all_steps" not in params:  # a design module older than 0.5.0
        raise PfError("--step/--all need package-factory 0.5.0's design module (reinstall: pip install -e <this folder>)", "usage")
    return design.generate_code(repo, kind, name, steps=list(steps) if steps else None, all_steps=all_steps)


def design_action(action: str, kind: str, name: str, by: str | None = None, force: bool = False, evidence: dict | None = None,
                  steps: list[str] | None = None, all_steps: bool = False) -> Result:
    """plan | show | status | approve-plan | code | test | approve-code, for a component or (plan/show/status/
    approve-plan/test) a pipeline. `code` takes steps (--step, repeatable: exactly those custom steps) or all_steps
    (--all: every custom step); with neither, the missing or failing steps are coded."""
    from . import design

    repo = _repo()
    cmd = f"{action} {kind}"
    if kind == "pipeline":
        from . import decompose

        if action == "plan":
            lock, issues = decompose.plan(repo, name)
            return Result(command=cmd, ok=not any(i.severity == "error" for i in issues), issues=issues,
                          data={"status": decompose.status(repo, name), "split": lock["split"], "show": decompose.show(repo, name)})
        if action == "show":
            return Result(command=cmd, ok=True, data={"status": decompose.status(repo, name), "text": decompose.show(repo, name)})
        if action == "status":
            return Result(command=cmd, ok=True, data=decompose.status(repo, name))
        if action == "approve-plan":
            _approval_guard("a pipeline split")
            return Result(command=cmd, ok=True, data=decompose.approve(repo, name, by, force, evidence))
        if action == "test":
            from .build import pipeline_examples

            res = pipeline_examples(repo, name, draft=True)
            bad = [r for r in res if r["status"] not in ("pass", "shown")]
            return Result(command=cmd, ok=not bad, issues=[Issue(file=repo.file_of("pipeline", name), loc=f"examples.{r['example']}",
                                                                   code="example_failed", msg=r["detail"]) for r in bad], data={"examples": res})
        raise PfError(f"`pkf {action}` does not apply to a pipeline", "usage")
    if (steps or all_steps) and action != "code":
        raise PfError("--step/--all apply to `pkf code` only", "usage")
    if steps and all_steps:
        raise PfError("--step and --all cannot be combined", "usage")
    design.kind_def(kind)
    errs = repo.errors_for(kind, name)
    if errs:
        raise PfError(f"{repo.file_of(kind, name)} has errors", "invalid", errs)
    if action == "plan":
        lock, issues = design.plan(repo, kind, name)
        return Result(command=cmd, ok=not any(i.severity == "error" for i in issues), issues=issues,
                      data={"status": design.status(repo, kind, name), "plan": lock["plan"], "simulation": lock["simulation"],
                            "show": design.show(repo, kind, name)})
    if action == "show":
        return Result(command=cmd, ok=True, data={"status": design.status(repo, kind, name), "text": design.show(repo, kind, name)})
    if action == "status":
        return Result(command=cmd, ok=True, data=design.status(repo, kind, name))
    if action in ("approve-plan", "approve-code"):
        _approval_guard(f"a {kind}")
        rec = (design.approve_plan(repo, kind, name, by, evidence) if action == "approve-plan"
               else design.approve_code(repo, kind, name, by, evidence))
        return Result(command=cmd, ok=True, data={"approval": rec, "status": design.status(repo, kind, name)})
    if action in ("code", "test"):
        lock, issues = (_generate_code(design, repo, kind, name, steps, all_steps) if action == "code"
                        else design.test_code(repo, kind, name))
        return Result(command=cmd, ok=not any(i.severity == "error" for i in issues), issues=issues,
                      data={"file": f"{design.KINDS[kind].folder}/{name}.py", "tests": lock["code"].get("tests"),
                            "status": design.status(repo, kind, name)})
    raise PfError(f"unknown action {action}", "usage")


def cmd_design(a) -> Result:
    return design_action(a.action, a.kind, a.name, getattr(a, "by", None), getattr(a, "force", False), _evidence(a),
                         steps=getattr(a, "step", None), all_steps=getattr(a, "all", False))


def cmd_transform(a) -> Result:
    """Kept for compatibility: `pkf transform <action> <name>` = `pkf <action> transformation <name>`."""
    return design_action(a.action, "transformation", a.name, a.by)


def _pipelines(repo, a) -> list[str]:
    if a.all:
        return sorted(repo.specs["pipeline"])
    if not a.name:
        raise PfError("name a pipeline or use --all", "usage")
    return [a.name]


def cmd_analyze(a) -> Result:
    """A requirement document's text → pipelines/<name>.yaml (finalized requirement + examples) and its analysis."""
    from . import analyst

    repo = _repo()
    data, issues = analyst.analyze(repo, analyst.load_text(Path(a.text_file)), a.name, a.owner, a.source_file)
    return Result(command="analyze-requirements", ok=not any(i.severity == "error" for i in issues), issues=issues, data=data)


def cmd_build(a) -> Result:
    """Render and write the DAG(s); --check compares instead of writing; --draft (K3) renders with code that passes
    its tests but is not approved yet (S4 review) and writes nothing. ok = no error issue (warnings never fail)."""
    from .build import check, lock_of, render, unapproved_code, warnings, write

    draft = getattr(a, "draft", False)
    if draft and a.check:
        raise PfError("--draft and --check cannot be combined", "usage")
    repo = _repo()
    issues: list[Issue] = []
    built: list[str] = []
    would: list[str] = []
    unapproved: list[str] = []
    for name in _pipelines(repo, a):
        try:
            files = render(repo, name, allow_unapproved_code=draft)
        except PfError as exc:
            issues += exc.issues or [Issue(file=f"pipelines/{name}.yaml", msg=str(exc), code=exc.code)]
            continue
        issues += warnings(repo, name, lock_of(files, name))
        if draft:
            would += files
            unapproved += unapproved_code(repo, name)
        elif a.check:
            issues += [Issue(file=f"pipelines/{name}.yaml", msg=d, code="stale_build") for d in check(repo, files, name)]
        else:
            built += write(repo, files, name)
    ok = not any(i.severity == "error" for i in issues)
    if draft:
        return Result(command="build --draft", ok=ok, issues=issues,
                      data={"files": [], "would_write": sorted(set(would)), "draft": True, "unapproved_code": sorted(set(unapproved))})
    removed = []
    if getattr(a, "all", False):  # DAGs whose pipeline spec was deleted
        from .build import orphans

        for rel in orphans(repo):
            if a.check:
                issues.append(Issue(file=rel, msg=f"unexpected: {rel} (its pipeline spec no longer exists)", code="stale_build"))
            else:
                removed.append(rel)
                p = repo.root / rel
                shutil.rmtree(p) if p.is_dir() else p.unlink()
    ok = not any(i.severity == "error" for i in issues)
    return Result(command="build --check" if a.check else "build", ok=ok, issues=issues,
                  data={"files": sorted(set(built)), "removed": removed})


def _params(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise PfError(f"--param needs key=value, got '{p}'", "usage")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def _local_lock(repo, name: str, workdir: Path) -> dict:
    """The pipeline's lock with its component modules written to a scratch folder (what the DAG folder would hold)."""
    from .build import lock_for

    lock, modules = lock_for(repo, name)
    for rel, data in modules.items():
        (workdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (workdir / rel).write_bytes(data)
    lock["_dir"] = str(workdir)
    return lock


class _RunLogging:
    """`pkf run` / `pkf check` logging of the `pkf` logger tree (B10): INFO to --log-file (UTF-8, LF, written as it
    goes) when given, else INFO to stderr, or WARNING to stderr with --json (stdout holds the envelope)."""

    def __init__(self, a):
        self.file = getattr(a, "log_file", None)
        self.level = logging.INFO if (self.file or not getattr(a, "json", False)) else logging.WARNING
        self.handler: logging.Handler | None = None
        self.stream = None
        self.old_level = logging.NOTSET

    def __enter__(self):
        log = logging.getLogger("pkf")
        if self.file:
            path = Path(self.file)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = open(path, "a", encoding="utf-8", newline="\n")
            self.handler = logging.StreamHandler(self.stream)  # flushed after every record
        else:
            self.handler = logging.StreamHandler(sys.stderr)
        self.handler.setLevel(self.level)
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(self.handler)
        self.old_level = log.level
        if log.level == logging.NOTSET or log.level > self.level:
            log.setLevel(self.level)
        return self

    def __exit__(self, *exc):
        log = logging.getLogger("pkf")
        if self.handler is not None:
            log.removeHandler(self.handler)
            self.handler.close()
        if self.stream is not None:
            self.stream.close()
        log.setLevel(self.old_level)
        return False


def cmd_check(a) -> Result:
    import tempfile

    from pkf_runtime import tasks

    repo = _repo()
    with _RunLogging(a), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        lock = _local_lock(repo, a.name, Path(d))
        ctx = {"run_id": "check", "params": _params(a.param)}
        return Result(command="check", ok=True, data={**tasks.check_source(lock, ctx), **tasks.check_destination(lock, ctx)})


PLANNED_VERSION = 1


def _usage_run(a) -> None:
    """Flag combinations of `pkf run` that are refused before anything runs (K6, R6)."""
    only, plan_out = getattr(a, "only", None), getattr(a, "plan_out", None)
    if only and a.dry_run:
        raise PfError("--only cannot be combined with --dry-run: preview with --dry-run --plan-out FILE, then move with --only FILE", "usage")
    if only and (a.param or a.date or a.run_id):
        raise PfError("--only takes run_id, date and params from the preview (planned.json): do not pass --param, --date or --run-id", "usage")
    if plan_out and not a.dry_run:
        raise PfError("--plan-out needs --dry-run", "usage")
    if a.max_packages is not None and a.max_packages < 0:
        raise PfError("--max-packages needs a number >= 0 (0 = no limit beyond the framework's own)", "usage")


def read_planned(path: str, pipeline: str) -> dict:
    """A preview's planned.json (K6), checked: version, pipeline, run id and file list."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PfError(f"--only: {path} does not exist (Preview this run first)", "usage") from None
    except (OSError, ValueError) as exc:
        raise PfError(f"--only: {path} is not a readable planned.json ({exc})", "usage") from None
    if not isinstance(doc, dict) or doc.get("version") != PLANNED_VERSION or not isinstance(doc.get("files"), list):
        raise PfError(f"--only: {path} is not a planned.json of this package-factory (version {PLANNED_VERSION})", "usage")
    if doc.get("pipeline") != pipeline:
        raise PfError(f"--only: {path} is the preview of pipeline '{doc.get('pipeline')}', not '{pipeline}'", "usage")
    if not doc.get("run_id"):
        raise PfError(f"--only: {path} has no run_id", "usage")
    return doc


def _when(text: str | None):
    import datetime as dt

    if not text:
        return None
    when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def cmd_run(a) -> Result:
    """Run a pipeline locally, task by task, with the same runtime the DAG uses (development and debugging).
    --dry-run [--plan-out FILE] previews (and records every planned file); --only FILE moves exactly the previewed
    files with the preview's run id, date and params (custom delivery only); --max-packages N caps the packages."""
    import datetime as dt
    import tempfile

    from pkf_runtime import tasks

    _usage_run(a)
    planned = read_planned(a.only, a.name) if getattr(a, "only", None) else None
    repo = _repo()
    conf = json.loads(a.conf) if a.conf else {}
    with _RunLogging(a), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        lock = _local_lock(repo, a.name, Path(d))
        now = dt.datetime.now(dt.timezone.utc)
        if planned is not None:
            if not tasks.is_custom_delivery(lock):
                raise PfError("`--only` applies to custom-delivery pipelines (this one uploads with the framework and never "
                              "deletes sources: run it without --only)", "usage")
            when = _when(planned.get("date")) or _when(planned.get("logical_date"))
            if when is None:
                logging.getLogger("pkf").warning("%s records no date: the targets are computed for now", a.only)
            ctx = {"run_id": planned["run_id"], "logical_date": when or now, "params": dict(planned.get("params") or {}),
                   "conf": conf, "only": planned}
        else:
            ctx = {"run_id": a.run_id or f"local__{now:%Y%m%dT%H%M%S}", "logical_date": _when(a.date) or now,
                   "params": _params(a.param), "conf": conf}
        if a.max_packages:
            ctx["max_packages"] = a.max_packages
        if a.dry_run:
            try:
                dr = tasks.dry_run(lock, ctx, full=bool(a.plan_out))
            except Exception as exc:  # noqa: BLE001 - D9: e.g. no AWS credentials; always an envelope, never a traceback
                code = "run_failed" if isinstance(exc, tasks.FAIL_FAST) else "run_error"
                return Result(command="run", ok=False, issues=[Issue(msg=f"{type(exc).__name__}: {exc}", code=code)],
                              data={"code": code})
            data: dict = {"dry_run": dr}
            if a.plan_out:
                files = dr.pop("planned_files")
                doc = {"version": PLANNED_VERSION, "pipeline": a.name, "created_at": f"{now:%Y-%m-%dT%H:%M:%SZ}",
                       "run_id": ctx["run_id"], "params": ctx["params"], "date": a.date or None,
                       "logical_date": ctx["logical_date"].isoformat(), "max_packages": a.max_packages or None,
                       "delivery": dr.get("delivery"), "files": files}
                out = Path(a.plan_out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8", newline="\n")
                data["planned"] = {"file": str(out), "files": len(files)}
            return Result(command="run", ok=True, data=data)
        try:
            out = tasks.run_all(lock, ctx, keep_staging=a.keep_staging)
        except Exception as exc:  # noqa: BLE001 - a failed run, or network errors Airflow would retry: report, don't crash
            fail_fast = isinstance(exc, tasks.FAIL_FAST)
            msg = f"{type(exc).__name__}: {exc}" + ("" if fail_fast else " (Airflow would retry this)")
            return Result(command="run", ok=False, issues=[Issue(msg=msg, code="run_failed" if fail_fast else "run_error")],
                          data={"tasks": getattr(exc, "trace", []), "files": getattr(exc, "files", []),
                                "summary": getattr(exc, "summary", None)})
    return Result(command="run", ok=True, data=out)


def pipeline_status(repo, name: str) -> dict:
    """One pipeline: its stage, the next commands, and (structured) the split and every component's stage."""
    from . import decompose, design
    from .build import check, render

    p = repo.specs["pipeline"].get(name)
    if p is None:
        return {"pipeline": name, "stage": "invalid", "next": [f"fix pipelines/{name}.yaml (pkf validate)"], "components": []}
    split = decompose.status(repo, name) if p.requirement else None
    base = {"pipeline": name, "split": split, "components": []}
    if split is not None and split["stage"] != "split_approved":
        return {**base, "stage": split["stage"], "next": [split["next"]]}
    if p.connection is None or p.connection not in repo.specs["connection"]:
        return {**base, "stage": "invalid", "next": ["pkf validate"]}
    c = repo.specs["connection"][p.connection]
    todo = []
    for kind, cname in (("trigger", p.trigger), ("source", c.source), ("transformation", p.transformation), ("destination", c.destination)):
        if not cname:
            continue
        if cname not in repo.specs[kind]:
            base["components"].append({"kind": kind, "name": cname, "stage": "invalid", "mode": None,
                                       "next": f"{repo.file_of(kind, cname)}: missing or invalid (pkf validate)"})
            todo.append(base["components"][-1]["next"])
            continue
        st = design.status(repo, kind, cname)
        base["components"].append({"kind": kind, "name": cname, "stage": st["stage"], "mode": st["mode"], "next": st["next"]})
        if st["stage"] != "approved":
            todo.append(f"{kind} {cname} [{st['stage']}]: {st['next']}")
    if todo:
        return {**base, "stage": "components", "next": todo}
    try:
        stale = check(repo, render(repo, name), name)
    except PfError as exc:
        return {**base, "stage": "invalid", "next": [str(exc)]}
    return {**base, "stage": "build" if stale else "ready",
            "next": [f"pkf build {name}"] if stale else ["commit dags/ and deploy the dags folder to Airflow"]}


def cmd_status(a) -> Result:
    """Where each pipeline stands, and the next command for every part that is not ready."""
    repo = _repo()
    names = [a.name] if a.name else sorted(repo.specs["pipeline"])
    return Result(command="status", ok=True, data={"pipelines": [pipeline_status(repo, n) for n in names]})


_ARGV: list[str] | None = None  # the arguments main() parses (argv given by a caller, else sys.argv)


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # type: ignore[override]
        if "--json" in (sys.argv[1:] if _ARGV is None else _ARGV):
            print(Result(command="pkf", ok=False, issues=[Issue(msg=message, code="usage")], data={"code": "usage"}).model_dump_json(indent=2))
            raise SystemExit(2)
        super().error(message)


def parser() -> argparse.ArgumentParser:
    p = _Parser(prog="pkf", description="package-factory: spec-driven Airflow ingestion pipelines for data packages")
    p.add_argument("--json", action="store_true", help="print one JSON result envelope")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("studio", help="pkf Studio: the Streamlit app (pkf studio [REPO]) and its gate check (pkf studio verify --pr N)")
    s = sub.add_parser("init", help="make a folder a specs repository")
    s.add_argument("path", nargs="?", default=".")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("new", help="scaffold a component spec")
    s.add_argument("kind", choices=["trigger", "source", "destination", "transformation", "connection", "pipeline"])
    s.add_argument("name")
    s.add_argument("--type", help="source: https|sftp|ftp|s3|local; destination: s3|local")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_new)
    s = sub.add_parser("validate", help="validate every spec and reference")
    s.add_argument("--allow-unfilled", action="store_true")
    s.set_defaults(fn=cmd_validate)
    sub.add_parser("list", help="components and transformation status").set_defaults(fn=cmd_list)
    sub.add_parser("catalog", help="source/destination types, credential providers, template variables, operations").set_defaults(fn=cmd_catalog)
    s = sub.add_parser("schema", help="JSON Schema of every spec kind")
    s.add_argument("--write")
    s.set_defaults(fn=cmd_schema)
    kinds = ["trigger", "source", "transformation", "destination", "pipeline"]
    for action, h in (("plan", "AI plan of a component (or the split of a pipeline requirement)"),
                      ("show", "the plan, tests and example results, for review"),
                      ("approve-plan", "a person approves the plan (or the pipeline split)"),
                      ("code", "AI code for the custom steps, tested as it is written"),
                      ("test", "run every example and test case against the code (pipeline: end-to-end examples)"),
                      ("approve-code", "a person approves the custom code")):
        s = sub.add_parser(action, help=h)
        s.add_argument("kind", choices=kinds)
        s.add_argument("name")
        if action.startswith("approve"):
            s.add_argument("--by", required=True, help="handle of the person approving, e.g. @jane")
            s.add_argument("--evidence", help="URL of the GitHub review/comment that approved (pkf Studio)")
            s.add_argument("--github-id", type=int, help="the approver's numeric GitHub user id")
            s.add_argument("--head-sha", help="the commit the approval was given on")
        if action == "approve-plan":
            s.add_argument("--force", action="store_true", help="pipeline split: replace the pipeline's own examples with the split's")
        if action == "code":
            s.add_argument("--step", action="append", metavar="ID",
                           help="code exactly this custom step (repeatable); default: the missing or failing steps")
            s.add_argument("--all", action="store_true", help="code every custom step again")
        s.set_defaults(fn=cmd_design, action=action)
    s = sub.add_parser("transform", help="compatibility: same as `pkf <action> transformation <name>`")
    s.add_argument("action", choices=["plan", "show", "status", "approve-plan", "code", "test", "approve-code"])
    s.add_argument("name")
    s.add_argument("--by", help="handle of the person approving, e.g. @jane")
    s.set_defaults(fn=cmd_transform)
    s = sub.add_parser("analyze-requirements", help="AI: a requirement document's text → the pipeline file + pseudocode")
    s.add_argument("text_file", help="UTF-8 text extracted from the requirement document")
    s.add_argument("--name", help="pipeline name (snake_case); chosen by the analyst when omitted")
    s.add_argument("--owner", default="data-engineering")
    s.add_argument("--source-file", help="original document name, for the record")
    s.set_defaults(fn=cmd_analyze)
    for c, h in (("build", "generate Airflow DAG(s)"), ("status", "where each pipeline stands")):
        s = sub.add_parser(c, help=h)
        s.add_argument("name", nargs="?")
        if c == "build":
            s.add_argument("--all", action="store_true")
            s.add_argument("--check", action="store_true", help="fail if generated files differ from the specs")
            s.add_argument("--draft", action="store_true",
                           help="review build (S4): accept tested but not yet approved code, check everything, write nothing")
            s.set_defaults(fn=cmd_build)
        else:
            s.set_defaults(fn=cmd_status)
    s = sub.add_parser("check", help="verify the pipeline's source and destination are reachable (live)")
    s.add_argument("name")
    s.add_argument("--param", action="append", help="key=value template parameter")
    s.add_argument("--log-file", metavar="FILE", help="write the log (INFO) to FILE")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("run", help="run a pipeline locally, task by task (development)")
    s.add_argument("name")
    s.add_argument("--param", action="append", help="key=value template parameter (like Airflow 'Trigger DAG w/ config')")
    s.add_argument("--date", help="logical date YYYY-MM-DD (default: now)")
    s.add_argument("--run-id")
    s.add_argument("--keep-staging", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="plan the run and list every file's target; change nothing")
    s.add_argument("--conf", help="run configuration as JSON (e.g. events for run_conf triggers)")
    s.add_argument("--max-packages", type=int, metavar="N",
                   help="at most N packages in this run; the rest wait for the next run (0 = no limit)")
    s.add_argument("--plan-out", metavar="FILE", help="with --dry-run: write every planned file to FILE (planned.json)")
    s.add_argument("--only", metavar="FILE",
                   help="custom delivery: move only the files of this preview (planned.json), with its run id, date and params")
    s.add_argument("--log-file", metavar="FILE", help="write the run's log (INFO) to FILE as it goes")
    s.set_defaults(fn=cmd_run)
    return p


def _safe_console() -> None:
    """B6: a console or pipe that is not UTF-8 (cp1252 in Git Bash/mintty, redirects) prints '?' for characters it
    cannot show instead of crashing the command."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if enc not in ("utf8", "utf8sig") and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):  # a stream that cannot be reconfigured is left as it is
                pass


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["studio"]:  # pkf studio [repo] | pkf studio verify --pr N (the studio extra)
        try:
            from pkf_studio.cli import main as studio
        except ImportError as exc:
            print(f"pkf studio is not available: {exc}")
            return 2
        return studio(list(args[1:]))
    global _ARGV
    _ARGV = list(args)
    a = parser().parse_args(args)
    try:
        r = a.fn(a)
        code = 0 if r.ok else 1
    except PfError as exc:
        r = Result(command=a.cmd, ok=False, issues=exc.issues or [Issue(msg=str(exc), code=exc.code)], data={"error": str(exc), "code": exc.code})
        code = 2 if exc.code in ("usage", "no_repo", "missing_dependency") else 1
    if a.json:
        print(r.model_dump_json(indent=2))
    else:
        if r.data.get("text") or r.data.get("show"):
            print(r.data.get("text") or r.data.get("show"))
        print(f"{r.command}: {'OK' if r.ok else 'PROBLEMS'}")
        for i in r.issues:
            print(f"  {i.severity:7} {i.file}{(':' + i.loc) if i.loc else ''}: {i.msg}")
        rest = {k: v for k, v in r.data.items() if k not in ("text", "show")}
        if isinstance(rest.get("files"), list):  # a run: the per-file rows are in --json and the run log
            rows = rest.pop("files")
            counts: dict = {}
            for row in rows:
                counts[row.get("status")] = counts.get(row.get("status"), 0) + 1
            print(f"  files: {len(rows)} {json.dumps(counts, sort_keys=True, default=str)}")
            if any(t.get("task") == "report" and "result" in t for t in rest.get("tasks") or []):
                rest.pop("summary", None)  # printed below as "report"
        if isinstance(rest.get("tasks"), list):  # a run: one line per task, then the report in full
            for t in rest.pop("tasks"):
                if t.get("task") == "report" and "result" in t:
                    rest["report"] = t["result"]
                    continue
                body = t.get("result") if "result" in t else {"error": t.get("error")}
                print(f"  {t.get('task')}: {json.dumps(body, default=str)[:400]}")
        if rest:
            print(json.dumps(rest, indent=2, default=str)[:6000])
    return code


if __name__ == "__main__":
    sys.exit(main())
