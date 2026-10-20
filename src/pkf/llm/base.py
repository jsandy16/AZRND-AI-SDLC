"""Provider-agnostic structured LLM calls with strict pydantic validation.

Providers (PKF_LLM_PROVIDER):
  agent-sdk  Claude Agent SDK; runs the named plugin skill; structured output (default)
  replay     recorded outputs from fixtures (tests, CI) — no network, fully deterministic
  record     agent-sdk, then saves the output as a replay fixture
  script     hand-written answers from a JSON file (PKF_LLM_SCRIPT), selected by task and a prompt substring (tests)

Test switches: PKF_LLM_FIXTURE_TRACE=<file> appends every replay fixture that is hit; PKF_LLM_REKEY=1 maps a replay miss
caused by a listed prompt change (PROMPT_CHANGES_0_5) to the fixture recorded for the old prompt (tests/rekey_fixtures.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from ..envelope import PfError

T = TypeVar("T", bound=BaseModel)
PLUGIN_NAME = "package-factory-steps"  # headless-only plugin: interactive sessions never see these skills
PROMPT_VERSION = "1"

PROMPT_CHANGES_0_5: dict[str, list[str]] = {
    # The exact text 0.5.0 inserted into first-attempt prompts, each with its leading newline: removing every entry from a
    # 0.5.0 prompt gives the 0.4.0 prompt byte for byte, so a recording keyed on the old prompt can be re-keyed (never
    # re-recorded). Used by PKF_LLM_REKEY; design._code_prompt inserts these strings verbatim.
    "write_custom_step": [
        "\nAnswer with exactly one function. Put helper functions and imports INSIDE it: the file holds only the step "
        "functions, and module-level code is refused.",
        "\nS3 keys and package paths are POSIX strings: split and join on '/' (str methods or posixpath); never os.path, "
        "os.sep or pathlib (they differ on Windows).",
        "\nctx['delivery'] is always present: the destination's delivery settings (if_exists, server_side_encryption, "
        "kms_key_id; values may be None). Values earlier layout steps put in layout['vars'] arrive in params, e.g. "
        "params.get('reason:' + t['path']).",
    ],
}
LEGACY_SKILL_SHA: dict[str, str] = {}  # skill -> SKILL.md sha256 of 0.4.0, when a SKILL.md changed (none in 0.5.0)


class LLMMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    task: str
    skill: str
    skill_sha256: str
    prompt_sha256: str
    schema_sha256: str
    model: str
    prompt_version: str = PROMPT_VERSION
    cost_usd: float | None = None
    turns: int | None = None
    duration_s: float


def plugin_root() -> Path:
    env = os.environ.get("PKF_PLUGIN_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "steps-plugin" / ".claude-plugin" / "plugin.json").exists():
            return p / "steps-plugin"
    raise PfError("package-factory steps plugin not found; set PKF_PLUGIN_ROOT", "no_plugin")


def skill_sha(skill: str) -> str:
    f = plugin_root() / "skills" / skill / "SKILL.md"
    if not f.exists():
        raise PfError(f"skill '{skill}' not found at {f}", "no_skill")
    return hashlib.sha256(f.read_bytes()).hexdigest()


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON Schema for structured output: draft-07 compatible, additionalProperties false everywhere,
    no numeric/length constraints (enforced by pydantic after the call instead)."""
    schema = model.model_json_schema()

    constraint_keys = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern",
                       "multipleOf", "title", "default", "maxItems")

    def clean(node):
        """Walks schema nodes only: keys inside a `properties` map are field names, never keywords to strip."""
        if isinstance(node, list):
            for v in node:
                clean(v)
            return
        if not isinstance(node, dict):
            return
        for k in constraint_keys:
            node.pop(k, None)
        if node.get("type") == "array" and node.get("minItems", 0) > 1:
            node["minItems"] = 1
        if "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"])
            for sub in node["properties"].values():
                clean(sub)
        for k in ("items", "anyOf", "oneOf", "allOf", "not", "additionalItems", "prefixItems"):
            if k in node:
                clean(node[k])

    defs = schema.pop("$defs", {})

    def inline(node):  # $ref/$defs are not reliably honoured by every structured-output path: inline them
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(dict(defs[node["$ref"].rsplit("/", 1)[-1]]))
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    schema = inline(schema)
    clean(schema)
    return schema


def _key(task: str, skill: str, prompt: str, schema: dict, tools: list[str] | None = None, skill_sha256: str | None = None) -> str:
    """Everything that can change the answer: task, skill name AND content, prompt, schema, prompt version, model.
    Editing a SKILL.md or switching model therefore needs a new recording, never a stale replay."""
    from .agent_sdk import settings

    parts = [task, skill, skill_sha256 or skill_sha(skill), prompt, schema, PROMPT_VERSION, settings(task)["model"]]
    if tools:
        parts.append(sorted(tools))
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


_FENCE = None


def strip_fence(text):
    """Remove one Markdown code fence around the whole text (any language tag, surrounding whitespace); anything else
    is returned unchanged. Models sometimes wrap code or JSON fields in ```python … ``` although the schema says not to."""
    import re

    global _FENCE
    if not isinstance(text, str):
        return text
    if _FENCE is None:
        _FENCE = re.compile(r"\A\s*```[^\n`]*\n(.*?)\n?[ \t]*```\s*\Z", re.S)
    m = _FENCE.match(text)
    return m.group(1) if m else text


def fence(tag: str, text: str) -> str:
    """Wrap untrusted text in <tag> delimiters it cannot close or reopen."""
    import re

    safe = re.sub(rf"<\s*/?\s*{re.escape(tag)}\b[^>]*>", f"[{tag}]", text, flags=re.IGNORECASE)
    return f"<{tag}>\n{safe}\n</{tag}>"


GENERIC_SECRET = r"(?i)\b(password|passwd|pwd|secret|api[_ -]?key|token|access[_ -]?key)\b([\"']?\s*(is|:|=)\s*[\"']?)([^\s,;\"']{4,})"
SECRET_PATTERNS = [
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"\bAKIA[0-9A-Z]{16}\b", r"\bASIA[0-9A-Z]{16}\b",
    GENERIC_SECRET,
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", r"\bgithub_pat_[A-Za-z0-9_]{30,}\b", r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b",
    r"\bsk-ant-[A-Za-z0-9_-]{20,}\b",
    r"(?i)\baws_secret_access_key\b(\s*[:=]\s*)([A-Za-z0-9/+=]{40})",
    r"(?i)[\"']?(SecretAccessKey|SessionToken|secret_access_key|session_token)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{16,}",
]


def looks_secret(value: str) -> bool:
    """A value after 'password:' / 'token is' … that looks like a secret: 8+ characters with a digit or a symbol.
    Ordinary words ("the password is stored in …", "split on the token: underscore") are not secrets."""
    import re

    return len(value) >= 8 and re.search(r"[^A-Za-z]", value) is not None


def redact_secrets(text: str) -> tuple[str, int]:
    """Deterministic scrub before any text leaves for the model: keys, AWS key ids, 'password: x' style values that
    look like secrets. Returns the text and how many values were replaced."""
    import re

    n = 0
    for pat in SECRET_PATTERNS:
        generic = pat == GENERIC_SECRET

        def repl(m):
            nonlocal n
            if generic and not looks_secret(m.group(4)):
                return m.group(0)
            n += 1
            return (m.group(1) + m.group(2) + "[REDACTED]") if m.lastindex and m.lastindex >= 4 else "[REDACTED]"
        text = re.sub(pat, repl, text)
    return text, n


def redaction_warning(n: int) -> str:
    return (f"{n} value(s) were redacted before the AI saw the text (they looked like secrets); if they are not secrets, "
            "reword them in the requirement")


def _canon_sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fixtures_dir() -> Path:
    return Path(os.environ.get("PKF_LLM_FIXTURES", "tests/llm-fixtures"))


def _append_line(env: str, line: str) -> None:
    path = os.environ.get(env)
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")


def _rekey(task: str, skill: str, prompt: str, schema: dict, tools: list[str] | None, new: Path) -> Path | None:
    """PKF_LLM_REKEY: the fixture recorded for the same call before the 0.5.0 prompt changes, copied under the new key."""
    old_prompt = prompt
    for sentence in PROMPT_CHANGES_0_5.get(task, []):
        old_prompt = old_prompt.replace(redact_secrets(sentence)[0], "", 1)
    old_key = _key(task, skill, old_prompt, schema, tools, skill_sha256=LEGACY_SKILL_SHA.get(skill))
    old = fixtures_dir() / task / f"{old_key}.json"
    if old_key == new.stem or not old.exists():
        return None
    rec = json.loads(old.read_text(encoding="utf-8"))
    rec["provenance"] = {**rec.get("provenance", {}), "rekeyed_from": old_key}
    new.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    import sys

    print(f"pkf rekey: {task}/{old_key} -> {task}/{new.stem}", file=sys.stderr)
    _append_line("PKF_LLM_REKEY_LOG", json.dumps({"task": task, "old": old_key, "new": new.stem}))
    return new


def _script_answer(task: str, prompt: str) -> tuple[Any, int]:
    """PKF_LLM_PROVIDER=script: the first unused entry of PKF_LLM_SCRIPT for this task whose `when` occurs in the prompt."""
    path = os.environ.get("PKF_LLM_SCRIPT")
    if not path or not Path(path).exists():
        raise PfError(f"PKF_LLM_PROVIDER=script needs PKF_LLM_SCRIPT=<answers.json> (got {path!r})", "invalid")
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    state_file = Path(str(path) + ".state.json")
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    used = {int(k): v for k, v in (state.get("used") or {}).items()}
    for i, entry in enumerate(doc.get("answers") or []):
        if entry.get("task") != task or (entry.get("when") and entry["when"] not in prompt):
            continue
        times = entry.get("times")
        if times is not None and used.get(i, 0) >= int(times):
            continue
        used[i] = used.get(i, 0) + 1
        state_file.write_text(json.dumps({"used": {str(k): v for k, v in sorted(used.items())}}, indent=1), encoding="utf-8",
                              newline="\n")
        return entry, i
    return None, -1


AI_RETRY_HINT = " — the AI step failed; this is usually temporary: run it again (Regenerate (AI) / Run tests & build again)"
SCHEMA_FEEDBACK = ("\n\nYour previous answer did not match the required output schema: {errors}. "
                   "Answer again through the structured output.")


def _schema_errors(exc: ValidationError) -> list[dict]:
    """The first three validation errors, without the (possibly long) input values."""
    return [{"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()[:3]]


def _answer(provider: str, task: str, skill: str, prompt: str, schema: dict, tools: list | None,
            tool_names: list[str] | None) -> tuple[Any, str, dict, dict]:
    """One answer of the configured provider: (raw output, model name, cost/turns, replay provenance)."""
    if provider == "script":
        entry, idx = _script_answer(task, prompt)
        _append_line("PKF_LLM_SCRIPT_LOG", json.dumps({"task": task, "entry": idx if entry is not None else None,
                                                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                                                        "len": len(prompt), "prompt": prompt}))
        if entry is None:
            raise PfError(f"script: no answer for {task} (prompt starts {prompt[:160]!r})", "llm_error")
        if entry.get("raise"):
            raise PfError(entry.get("message") or f"script: {entry['raise']}", entry["raise"])
        return entry.get("output"), "script", {}, {}
    if provider == "replay":
        key = _key(task, skill, prompt, schema, tool_names)
        f = fixtures_dir() / task / f"{key}.json"
        if not f.exists() and os.environ.get("PKF_LLM_REKEY") == "1":
            _rekey(task, skill, prompt, schema, tool_names, f)
        if not f.exists():
            raise PfError(f"no recorded LLM output for this request ({f}); record it with PKF_LLM_PROVIDER=record", "no_fixture")
        _append_line("PKF_LLM_FIXTURE_TRACE", str(f.resolve()))
        rec = json.loads(f.read_text(encoding="utf-8"))
        raw = rec["output"]
        if rec.get("output_sha256") != _canon_sha(raw):
            raise PfError(f"recorded LLM output {f} was edited after recording", "fixture_tampered")
        recorded = rec.get("provenance", {})  # replay reports how the answer was really produced
        return raw, recorded.get("model", "unknown"), {k: recorded.get(k) for k in ("cost_usd", "turns")}, recorded
    if provider in ("agent-sdk", "record"):
        from .agent_sdk import call

        raw, model_name, meta_extra = call(skill, prompt, schema, task, tools)
        if provider == "record":
            f = fixtures_dir() / task / f"{_key(task, skill, prompt, schema, tool_names)}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            prov = {"model": model_name, "skill_sha256": skill_sha(skill), "prompt_version": PROMPT_VERSION,
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **meta_extra}
            f.write_text(json.dumps({"task": task, "skill": skill, "provenance": prov, "output": raw,
                                     "output_sha256": _canon_sha(raw)}, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
        return raw, model_name, meta_extra, {}
    raise PfError(f"unknown PKF_LLM_PROVIDER '{provider}'", "invalid")


def run_structured(task: str, skill: str, prompt: str, output: type[T], *, log_dir: Path | None = None,
                   tools: list | None = None) -> tuple[T, LLMMeta]:
    """One agent role: a skill, a prompt, a strict output schema and (optionally) the framework's own tools, e.g. a
    test runner the coder can call while it works. Replay/record key on everything that can change the answer.
    An answer that does not match the schema is asked for once more, with the validation errors added to the prompt
    (a model call for agent-sdk/record/script; replay serves the recorded retry when one exists, never calls a model)."""
    import sys

    provider = os.environ.get("PKF_LLM_PROVIDER", "agent-sdk")
    prompt, n_redacted = redact_secrets(prompt)  # nothing that looks like a secret leaves for the model (or lands in a log)
    if n_redacted:
        print(f"{n_redacted} value(s) were redacted before the AI saw the text", file=sys.stderr)
    schema = strict_schema(output)
    tool_names = [t.name for t in tools] if tools else None
    started = time.time()
    raw, model_name, meta_extra, recorded = _answer(provider, task, skill, prompt, schema, tools, tool_names)
    try:
        obj = output.model_validate(raw)
    except ValidationError as exc:
        errors = _schema_errors(exc)
        retry = redact_secrets(prompt + SCHEMA_FEEDBACK.format(errors=errors))[0]
        obj = None
        if provider in ("agent-sdk", "record", "script") or (
                provider == "replay" and (fixtures_dir() / task / f"{_key(task, skill, retry, schema, tool_names)}.json").exists()):
            prompt = retry
            raw, model_name, meta_extra, recorded = _answer(provider, task, skill, prompt, schema, tools, tool_names)
            try:
                obj = output.model_validate(raw)
            except ValidationError as exc2:
                errors = _schema_errors(exc2)
        if obj is None:
            raise PfError(f"{task}: the model's output did not match the {output.__name__} schema: {errors}" + AI_RETRY_HINT,
                          "llm_schema") from None
    meta = LLMMeta(provider=provider if provider != "replay" else f"replay:{recorded.get('recorded_at', '?')}", task=task, skill=skill,
                   skill_sha256=recorded.get("skill_sha256", skill_sha(skill)),
                   prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), schema_sha256=hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                   model=model_name, duration_s=round(time.time() - started, 2), **meta_extra)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "llm-calls.jsonl", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"meta": meta.model_dump(), "output": obj.model_dump(mode="json")}, sort_keys=True) + "\n")
    return obj, meta
