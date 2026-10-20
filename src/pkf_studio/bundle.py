"""The artifact zip for one pipeline, built from tracked files only (git ls-files at a ref), with fixed folders:

  01_requirements/  original.<ext>, extracted.txt, pipeline.yaml, pseudocode.md, analysis.json
  02_design/        split.lock.yaml, components/<kind>/<name>.yaml
  03_plans/         <kind>/<name>.lock.yaml, <kind>/<name>.md (the readable plan)
  04_code/          <kind>/<name>.py, dags/…, capabilities.md
  05_infra/         terraform/** (module, envs, bucket stacks), iam-policy.json, manifest.json
  06_deploy/        deploy.md (the commands to deploy the DAG folder)
  07_runs/          dry-run.json, runs/<run>/report.json
  workflow/         approvals.jsonl, logs/<job>.log (already scrubbed), flow-events.jsonl, assistant/<session>/, README.md
"""
from __future__ import annotations

import datetime as dt
import io
import json
import subprocess
import zipfile
from pathlib import Path

from . import ledger as L
from .gitops import Git


def _route(path: str, pipeline: str, comps: dict[str, str]) -> str | None:
    rq = f"requirements/{pipeline}/"
    if path.startswith(rq):
        return "01_requirements/" + path[len(rq):]
    if path == f"pipelines/{pipeline}.yaml":
        return "01_requirements/pipeline.yaml"
    if path == f"pipelines/{pipeline}.lock.yaml":
        return "02_design/split.lock.yaml"
    for kind, name in comps.items():
        folder = L.FOLDERS[kind]
        if path == f"{folder}/{name}.yaml":
            return f"02_design/components/{kind}/{name}.yaml"
        if path == f"{folder}/{name}.lock.yaml":
            return f"03_plans/{kind}/{name}.lock.yaml"
        if path == f"{folder}/{name}.py":
            return f"04_code/{kind}/{name}.py"
    if path == f"dags/pkf_{pipeline}.py" or path.startswith(f"dags/pkf/{pipeline}/"):
        return "04_code/" + path
    if path.startswith(f"infra/{pipeline}/"):
        rest = path[len(f"infra/{pipeline}/"):]
        return ("05_infra/" + rest) if rest in ("iam-policy.json", "manifest.json", "policy-check.json") else "05_infra/terraform/" + rest
    rv = f"review/{pipeline}/"
    if path.startswith(rv):
        rest = path[len(rv):]
        if rest == "capabilities.md":
            return "04_code/capabilities.md"
        if rest.startswith("deploy"):
            return "06_deploy/" + rest
        return "07_runs/" + rest
    if path == L.ledger_path(pipeline):
        return "workflow/approvals.jsonl"
    return None


def _flow_files(root: Path, pipeline: str) -> list[tuple[str, bytes]]:
    """events.jsonl and the assistant sessions of .pkf-studio/flows/<p>/ (secrets were scrubbed when written)."""
    from .flowstore import flow_dir

    d = flow_dir(root, pipeline)
    out = []
    if (d / "events.jsonl").is_file():
        out.append(("flow-events.jsonl", (d / "events.jsonl").read_bytes()))
    for f in sorted((d / "assistant").rglob("*")) if (d / "assistant").is_dir() else []:
        if f.is_file() and f.suffix in (".json", ".jsonl", ".md", ".txt", ".diff"):
            out.append((f.relative_to(d).as_posix(), f.read_bytes()))
    return out


def build(root: Path, ref: str, pipeline: str, plans_md: dict[str, str] | None = None, job_logs: list[tuple[str, str]] | None = None,
          self_asserted: bool = False) -> tuple[str, bytes]:
    """(file name, zip bytes)."""
    git = Git(root)
    files = git.ls_files(ref)
    comps = dict(L.components(lambda p: git.show(ref, p), pipeline))
    buckets = []
    try:
        buckets = json.loads(git.show(ref, f"infra/{pipeline}/manifest.json") or "{}").get("buckets", [])
    except ValueError:
        pass
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            dest = _route(f, pipeline, comps)
            if dest is None and any(f.startswith(f"infra/buckets/{b}/") for b in buckets):
                dest = "05_infra/terraform/" + f[len("infra/"):]
            if dest is None:
                continue
            out = subprocess.run(["git", "show", f"{ref}:{f}"], cwd=str(root), capture_output=True, timeout=60)
            if out.returncode == 0:
                z.writestr(dest, out.stdout)
        for key, md in (plans_md or {}).items():
            z.writestr(f"03_plans/{key}.md", md)
        for name, text in job_logs or []:
            z.writestr(f"workflow/logs/{name}.log", text)
        for rel, data in _flow_files(root, pipeline):   # the Workflow Developer run: journal and assistant sessions (0.6.0)
            z.writestr(f"workflow/{rel}", data)
        readme = [f"# {pipeline} — pkf Studio artifacts", "", f"Built {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M UTC} from `{ref}` "
                  f"({git.sha(ref) or '?'}).", ""]
        if self_asserted:
            readme += ["**Dev mode, self-asserted:** one person approved gates in several roles or their own changes. "
                       "These approvals are not accepted in SIT/PPT/PRD.", ""]
        readme += ["Folders: 01 requirements · 02 design · 03 plans · 04 code · 05 infra · 06 deploy · 07 runs · workflow (approvals ledger, logs, "
                   "flow-events.jsonl = the Workflow Developer run's journal, assistant/<session>/ = every assistant diagnosis and fix)."]
        z.writestr("workflow/README.md", "\n".join(readme) + "\n")
    return f"{pipeline}-artifacts-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}.zip", buf.getvalue()
