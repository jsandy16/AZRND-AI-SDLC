"""The requirement analyst: a requirement document → the pipeline file (finalized requirement + end-to-end examples),
pseudocode, assumptions and open questions. Used by pkf Studio (stage S1) through `pkf analyze-requirements`.

The finalized requirement is written straight into pipelines/<name>.yaml (`requirement`, `examples`): there is no
second copy. Pseudocode and the analysis are written next to the original document under requirements/<name>/.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from pydantic import ValidationError

from . import yamlio
from .envelope import Issue, PfError
from .repo import Repo
from .specs.common import REQUIRED

MAX_CHARS = 200_000


def _prompt(text: str, name: str | None) -> str:
    from .decompose import EXAMPLE_FORMAT, _catalogues
    from .llm.base import fence

    pex = next((ln for ln in EXAMPLE_FORMAT.splitlines() if ln.startswith("pipeline")), "")
    return ((f"Pipeline name: {name}\n" if name else "Pipeline name: choose a snake_case name\n")
            + "What pipelines built with this framework can do (catalogues of each component, for orientation):\n"
            + _catalogues() + "\n\n"
            + "End-to-end example format (examples_json is a JSON list of these):\n" + pex + "\n"
            + "  listing: full object keys / paths from the top of the source with ':size', e.g. \"incoming/S1/a.csv:10\"\n\n"
            + fence("document", text) + "\n")


SETTINGS_FROM_DOCUMENT = ("owner", "start_date", "retries", "retry_delay_minutes", "tags", "max_active_runs")
SECTIONS = ("TRIGGER", "SOURCE", "DESTINATION")


def requirement_sha256(requirement: str) -> str:
    """The hash `analysis.json` records for the requirement it wrote (stripped, UTF-8); G1 compares it with the
    pipeline file's current requirement, so the analyst's open questions count only until the requirement changes."""
    return hashlib.sha256((requirement or "").strip().encode("utf-8")).hexdigest()


def document_settings(text: str) -> dict:
    """When the imported document is itself a YAML mapping (e.g. a pipeline file), its structured pipeline settings
    that are valid PipelineFile values, in the pipeline file's form."""
    import yaml

    from .specs.pipeline import PipelineFile

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict):
        return {}
    out = {}
    for k in SETTINGS_FROM_DOCUMENT:
        if k not in doc or doc[k] is None:
            continue
        probe = {"api_version": "pkf/v1", "kind": "pipeline", "name": "probe", "requirement": "probe requirement", k: doc[k]}
        try:
            out[k] = PipelineFile.model_validate(probe).model_dump(mode="json")[k]
        except ValidationError:
            continue
    return out


def _examples(raw: str, loc: str, issues: list[Issue]) -> list[dict]:
    """The analyst's examples, each validated on its own: invalid ones and repeated names are dropped with a warning."""
    from .llm.base import strip_fence
    from .specs.design import PipelineExample

    try:
        items = json.loads(strip_fence(raw or "[]") or "[]")
    except json.JSONDecodeError as exc:
        issues.append(Issue(severity="warning", file=loc, loc="examples", code="invalid_examples",
                            msg=f"the analyst's examples are not JSON and were left out: {str(exc)[:300]}"))
        return []
    if not isinstance(items, list):
        issues.append(Issue(severity="warning", file=loc, loc="examples", code="invalid_examples",
                            msg="the analyst's examples are not a list and were left out"))
        return []
    out, names = [], set()
    for i, e in enumerate(items):
        try:
            PipelineExample.model_validate(e)
        except ValidationError as exc:
            name = e.get("name") if isinstance(e, dict) else None
            issues.append(Issue(severity="warning", file=loc, loc=f"examples.{i}", code="invalid_examples",
                                msg=f"example {name or i} was not valid and was left out: {str(exc)[:300]}"))
            continue
        if e["name"] in names:
            issues.append(Issue(severity="warning", file=loc, loc=f"examples.{i}", code="invalid_examples",
                                msg=f"example {e['name']} appears twice; the second one was left out"))
            continue
        names.add(e["name"])
        out.append(e)
    return out


def analyze(repo: Repo, text: str, name: str | None, owner: str, source_file: str | None = None) -> tuple[dict, list[Issue]]:
    from .llm import run_structured
    from .llm.base import AI_RETRY_HINT, redact_secrets, redaction_warning
    from .llm.schemas import FinalizedRequirement

    text = (text or "").strip()
    if not text:
        raise PfError("the document has no text (a scanned PDF?): nothing to analyze", "invalid")
    if len(text) > MAX_CHARS:
        raise PfError(f"the document text is longer than {MAX_CHARS} characters; split it", "invalid")
    prompt = _prompt(text, name)
    out, meta = run_structured("analyze_requirements", "finalizing-pipeline-requirements", prompt,
                               FinalizedRequirement, log_dir=repo.root / "requirements" / ".llm")
    if name:
        pname = name  # the name given wins; the analyst's suggestion is not used
    else:
        pname = (out.pipeline_name or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", pname):
            raise PfError(f"the analyst chose the pipeline name {pname!r}, which is not snake_case; nothing was written — "
                          "give a name or Analyze with AI again" + AI_RETRY_HINT, "llm_schema")
    missing = [w for w in SECTIONS if not re.search(rf"(?m)^[\W\d_]*{w}\b", out.requirement or "")]
    if missing:
        raise PfError("the analyst's answer has no TRIGGER / SOURCE / DESTINATION sections (a refusal?); nothing was written — "
                      "Analyze with AI again", "llm_schema")
    issues: list[Issue] = []
    loc = f"pipelines/{pname}.yaml"
    examples = _examples(out.examples_json, loc, issues)
    path = repo.root / "pipelines" / f"{pname}.yaml"
    doc = yamlio.to_plain(yamlio.load(path)) if path.exists() else {
        "api_version": "pkf/v1", "kind": "pipeline", "name": pname, "owner": owner, "start_date": dt.date.today().isoformat()}
    if not examples and doc.get("examples"):
        issues.append(Issue(severity="warning", file=loc, loc="examples", code="invalid_examples",
                            msg=f"the analyst returned no valid examples; the existing {len(doc['examples'])} are kept"))
        examples = doc["examples"]
    requirement = out.requirement.strip().replace(REQUIRED, "(set per environment)")  # values are filled in the components later
    settings = document_settings(text)  # the document's own structured settings win over --owner and the defaults
    doc.update(description=out.description, requirement=requirement + "\n", examples=examples, **settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    yamlio.dump(doc, path)
    rdir = repo.root / "requirements" / pname
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "pseudocode.md").write_text(f"# {pname} — pseudocode (display only)\n\n{out.pseudocode.strip()}\n", encoding="utf-8",
                                        newline="\n")
    analysis = {"pipeline": pname, "source_file": source_file, "assumptions": out.assumptions, "open_questions": out.open_questions,
                "requirement_sha256": requirement_sha256(requirement),
                "analyzed_by": meta.model_dump(exclude={"duration_s", "cost_usd", "turns", "provider"})}
    (rdir / "analysis.json").write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8", newline="\n")
    if out.open_questions:
        issues.append(Issue(severity="warning", file=loc, loc="requirement", code="ambiguous",
                            msg="open questions to answer before approval: " + " | ".join(out.open_questions)))
    n_redacted = redact_secrets(prompt)[1]
    if n_redacted:
        issues.append(Issue(severity="warning", file=loc, loc="requirement", code="redacted", msg=redaction_warning(n_redacted)))
    return {"pipeline": pname, "file": loc, "examples": len(examples), "settings_from_document": settings, **analysis}, issues


def load_text(path: Path) -> str:
    if not path.exists():
        raise PfError(f"{path} not found", "usage")
    return path.read_text(encoding="utf-8", errors="replace")
