"""The approvals ledger and the input hashes that tie each gate to exactly what was approved.

.pkf-studio/approvals/<pipeline>.jsonl — one JSON row per passed gate:
  gate, pr, pr_url, head_sha, merge_sha, mode, input_hash, roster_sha, approvers[{name, login, github_id, role,
  kind, id, url, submitted_at, body}], recorded_at, self_asserted

Input hashes (what a gate covers; a later change to it makes the gate stale):
  G1  the pipeline's requirement + examples          G2  the split digest (pipelines/<p>.lock.yaml)
  G3  every component's plan digest + spec text      G4  plan digests + code file hashes
  G5  the built DAG and the infra files (incl. the saved plan's text and hash, and the bucket stacks it uses)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Callable

import yaml

from . import STUDIO_DIR

Reader = Callable[[str], "str | None"]


def ledger_path(pipeline: str) -> str:
    return f"{STUDIO_DIR}/approvals/{pipeline}.jsonl"


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _yaml(read: Reader, path: str) -> dict:
    text = read(path)
    if not text:
        return {}
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}


FOLDERS = {"trigger": "triggers", "source": "sources", "destination": "destinations", "transformation": "transformations"}


def components(read: Reader, pipeline: str) -> list[tuple[str, str]]:
    p = _yaml(read, f"pipelines/{pipeline}.yaml")
    out = []
    if p.get("trigger"):
        out.append(("trigger", p["trigger"]))
    c = _yaml(read, f"connections/{p['connection']}.yaml") if p.get("connection") else {}
    for kind in ("source", "destination"):
        if c.get(kind):
            out.append((kind, c[kind]))
    if p.get("transformation"):
        out.append(("transformation", p["transformation"]))
    return out


def input_hash(gate: str, read: Reader, pipeline: str, list_files: Callable[[str], list[str]] | None = None) -> str:
    if gate == "G1":
        p = _yaml(read, f"pipelines/{pipeline}.yaml")
        return _sha({"requirement": (p.get("requirement") or "").strip(), "examples": p.get("examples") or []})
    if gate == "G2":
        return _sha({"split_digest": _yaml(read, f"pipelines/{pipeline}.lock.yaml").get("split_digest")})
    if gate in ("G3", "G4"):
        rows = []
        for kind, name in components(read, pipeline):
            folder = FOLDERS[kind]
            lock = _yaml(read, f"{folder}/{name}.lock.yaml")
            row = {"kind": kind, "name": name, "plan": lock.get("plan_digest"),
                   "spec": hashlib.sha256((read(f"{folder}/{name}.yaml") or "").encode()).hexdigest()}
            if gate == "G4":
                code = read(f"{folder}/{name}.py")
                row["code"] = hashlib.sha256(code.encode()).hexdigest() if code is not None else None
            rows.append(row)
        return _sha(rows)
    if gate == "G5":
        lf = list_files or (lambda prefix: [])
        files = list(lf(f"infra/{pipeline}/")) + list(lf(f"dags/pkf/{pipeline}/")) + [f"dags/pkf_{pipeline}.py"]  # what is deployed
        try:
            buckets = json.loads(read(f"infra/{pipeline}/manifest.json") or "{}").get("buckets", [])
        except ValueError:
            buckets = []
        for b in buckets:  # the shared bucket stacks this pipeline's notifications live in
            files += lf(f"infra/buckets/{b}/")
        files = sorted(set(files))
        return _sha([(f, hashlib.sha256((read(f) or "").encode()).hexdigest()) for f in files])
    raise ValueError(gate)


# ---------------------------------------------------------------------------------------------- pipeline isolation (0.6.1)
# A bucket stack (infra/buckets/<b>/) is shared by every pipeline whose notifications live on that bucket: one
# `queue { id = "pkf-<pipeline>" … }` block per pipeline (infra.render_bucket). input_hash (above) hashes the whole stack,
# so before 0.6.1 another pipeline's G5 on the same bucket made this pipeline's G5 stale. stages.derive now decides
# staleness with input_hash_scoped: this pipeline's own blocks plus everything that is not another pipeline's block. The
# ledger rows (and `pkf studio verify`) keep input_hash exactly as before.

def queue_blocks(text: str | None) -> dict[str, str]:
    """{pipeline: block text} of a rendered bucket stack's queue blocks (blocks whose id is not pkf-<name> are kept
    under their raw id)."""
    out: dict[str, str] = {}
    lines = (text or "").splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if lines[i].rstrip() == "  queue {":
            j = i + 1
            while j < len(lines) and lines[j].rstrip() != "  }":
                j += 1
            block = "".join(lines[i:j + 1])
            m = re.search(r'^\s*id\s*=\s*"(?:pkf-)?([^"]+)"', block, re.M)
            out[m.group(1) if m else f"#{len(out)}"] = block
            i = j + 1
            continue
        i += 1
    return out


def foreign_blocks(text: str | None, pipeline: str) -> dict[str, str]:
    """The queue blocks of every pipeline except `pipeline`."""
    return {k: v for k, v in queue_blocks(text).items() if k != pipeline}


def own_view(rel: str, text: str | None, pipeline: str) -> str:
    """What of a shared bucket-stack file belongs to `pipeline`'s approval: main.tf without the other pipelines' queue
    blocks and without the "Rendered by" comment; the tfvars without the stack's tags (who rendered it last);
    handoff.json with only this pipeline's entries. Other files unchanged."""
    text = text or ""
    name = rel.rsplit("/", 1)[-1]
    if name == "main.tf":
        for block in foreign_blocks(text, pipeline).values():
            text = text.replace(block, "", 1)
        return "".join(ln for ln in text.splitlines(keepends=True) if not ln.startswith("# Rendered by pkf Studio"))
    if name in ("terraform.tfvars.json", "handoff.json"):
        try:
            doc = json.loads(text)
        except ValueError:
            return text
        if isinstance(doc, dict):
            doc.pop("tags", None)
            if isinstance(doc.get("add"), list):
                doc["add"] = [n for n in doc["add"] if isinstance(n, dict) and n.get("pipeline") == pipeline]
            return json.dumps(doc, sort_keys=True)
    return text


def input_hash_scoped(gate: str, read: Reader, pipeline: str, list_files: Callable[[str], list[str]] | None = None) -> str:
    """input_hash, except that G5 sees only this pipeline's part of the shared bucket stacks (see own_view)."""
    if gate != "G5":
        return input_hash(gate, read, pipeline, list_files)
    lf = list_files or (lambda prefix: [])
    files = list(lf(f"infra/{pipeline}/")) + list(lf(f"dags/pkf/{pipeline}/")) + [f"dags/pkf_{pipeline}.py"]
    try:
        buckets = json.loads(read(f"infra/{pipeline}/manifest.json") or "{}").get("buckets", [])
    except ValueError:
        buckets = []
    shared = set()
    for b in buckets:
        for f in lf(f"infra/buckets/{b}/"):
            shared.add(f)
    rows = []
    for f in sorted(set(files) | shared):
        text = read(f) or ""
        if f in shared:
            text = own_view(f, text, pipeline)
        rows.append((f, hashlib.sha256(text.encode()).hexdigest()))
    return _sha(rows)


def parse(text: str | None) -> list[dict]:
    rows = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return rows


def latest(rows: list[dict], gate: str) -> dict | None:
    return next((r for r in reversed(rows) if r.get("gate") == gate), None)


def make_row(gate: str, pr: dict, result, mode: str, input_hash_value: str, roster_sha: str | None, single_person: bool) -> dict:
    approvers = [{"name": a.person, "login": a.login, "github_id": a.user_id, "role": a.role, "kind": a.evidence.kind,
                  "id": a.evidence.id, "url": a.evidence.html_url, "submitted_at": a.evidence.submitted_at,
                  "body": (a.evidence.body or "")[:2000]} for a in result.approvals]
    return {"gate": gate, "pr": pr["number"], "pr_url": pr.get("html_url"), "head_sha": pr["head"]["sha"],
            "merge_sha": pr.get("merge_commit_sha"), "mode": mode, "input_hash": input_hash_value, "roster_sha": roster_sha,
            "approvers": approvers, "recorded_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "self_asserted": bool(single_person)}


def append(text: str | None, row: dict) -> str:
    rows = [r for r in parse(text) if not (r.get("gate") == row["gate"] and r.get("pr") == row["pr"])]
    rows.append(row)
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)


def pipeline_paths(pipeline: str, comps: list[tuple[str, str]], connection: str | None = None) -> list[str]:
    """Every path that belongs to one pipeline."""
    out = [f"requirements/{pipeline}/", f"pipelines/{pipeline}.yaml", f"pipelines/{pipeline}.lock.yaml", f"infra/{pipeline}/",
           f"review/{pipeline}/", f"dags/pkf_{pipeline}.py", f"dags/pkf/{pipeline}/", ledger_path(pipeline)]
    if connection:
        out.append(f"connections/{connection}.yaml")
    for kind, name in comps:
        folder = FOLDERS[kind]
        out += [f"{folder}/{name}.yaml", f"{folder}/{name}.lock.yaml", f"{folder}/{name}.py"]
    return out


SHARED_OK = ("requirements/.llm/", "pipelines/.llm/", "triggers/.llm/", "sources/.llm/", "destinations/.llm/", "transformations/.llm/",
             "dags/.airflowignore")


def allowed_paths(pipeline: str, gate: str, read_head: Reader) -> tuple[str, ...]:
    """The path prefixes a stage PR (or a run-record PR) of this pipeline may change."""
    if gate == "RUN":
        return (f"review/{pipeline}/runs/", ledger_path(pipeline))
    conn = (_yaml(read_head, f"pipelines/{pipeline}.yaml") or {}).get("connection")
    allowed = tuple(pipeline_paths(pipeline, components(read_head, pipeline), conn)) + SHARED_OK
    if gate == "G5":
        allowed += ("infra/buckets/",)
    return allowed


def out_of_scope(pipeline: str, gate: str, files: list[str], read_head: Reader) -> list[str]:
    """The files of `files` that a stage PR of this pipeline may not change (sorted)."""
    allowed = allowed_paths(pipeline, gate, read_head)
    return sorted(f for f in files if not f.startswith(allowed))


def scope_problems(pipeline: str, gate: str, files: list[str], read_head: Reader) -> list[str]:
    """Files a stage PR (or a run-record PR) may not change: anything outside its own pipeline."""
    bad = out_of_scope(pipeline, gate, files, read_head)
    if not bad:
        return []
    return [f"the PR changes files outside pipeline {pipeline}'s own ({', '.join(bad[:5])}{' …' if len(bad) > 5 else ''})"]


def self_asserted(result, pr: dict, gate_roles: tuple) -> bool:
    """One person filled several roles, or someone approved their own PR."""
    author = ((pr.get("user") or {}).get("login") or "").lower()
    logins = {(a.login or "").lower() for a in result.approvals}
    return len(logins) < len(gate_roles) or author in logins
