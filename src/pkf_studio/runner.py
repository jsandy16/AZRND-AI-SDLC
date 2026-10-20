"""Job runner process: `python -m pkf_studio.runner <job dir>`. Runs one Studio action, logs everything (scrubbed),
writes result.json and exit, and releases the pipeline lock (a lane job: its lane lock, 0.8.0).

result.json = {"ok": <the action returned>, **<the action's result>, "problems": [...], "severity": None|"warning"|"error"}.
A job can succeed (the action returned) and still have problems: a framework step that reported `ok: false` (an AI
step that failed, code that fails its tests, a build error). They are collected with `Ctx.problem` and shown as a
warning by the UI; `severity: "error"` marks work that ran but failed (a local run that failed)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path


SECRET_ENV = {"GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
              "AWS_PROFILE", "AWS_DEFAULT_PROFILE"}
NEEDS_AWS = {"run", "check"}                                # live runs and connectivity checks
NEEDS_AI = {"analyze-requirements", "plan", "code"}  # roles that call the model (their test runner strips it again)

MAX_PROBLEMS, MAX_PROBLEM_LEN = 30, 600
AI_HINT = " — the AI step failed; this is usually temporary: run it again (Regenerate (AI) / Run tests & build again)"
NOT_READY_HINT = " — a component is not ready: see the S4 status panel (Workflow)"
HINTS = {  # K1: chosen by the envelope's data.code or the issues' codes (first in this order wins)
    "llm_error": AI_HINT, "llm_timeout": AI_HINT, "no_skill": AI_HINT, "llm_schema": AI_HINT,
    "llm_limit": " — the AI step stopped at its turn or budget limit: run it again, or raise PKF_LLM_MAX_BUDGET_USD_<TASK> deliberately",
    "no_fixture": " — no recorded AI answer (replay mode)",
    "missing_dependency": " — install the extras: pip install -e '.[runtime,agent,studio]'",
    "runner_mismatch": " — reinstall: pip install -e <this folder> in the venv you run pkf from",
    "test_failed": " — the generated code fails its tests: see the S4 status panel (Workflow)",
    "guard": " — the generated code fails its tests: see the S4 status panel (Workflow)",
    "component_not_ready": NOT_READY_HINT, "component_not_approved": NOT_READY_HINT,
    "run_failed": " — see review/<p>/runs/<run>/files.csv and run.log",
    "run_error": " — see review/<p>/runs/<run>/files.csv and run.log",
}
_RANK = {c: i for i, c in enumerate(HINTS)}
NOT_RUN = "not run: fix the problems first"


def heartbeat_s() -> float:
    try:
        return max(0.05, float(os.environ.get("PKF_STUDIO_HEARTBEAT_S", "15")))
    except ValueError:
        return 15.0


def _elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def problem_text(args, doc: dict) -> str:
    """One line for a framework step that reported `ok: false`: the command, its first error messages and a hint."""
    issues = [i for i in (doc.get("issues") or []) if isinstance(i, dict)]
    errors = [i for i in issues if i.get("severity", "error") == "error"] or issues
    # cases that did not run because of an earlier problem add nothing (R-phase1 #9)
    errors = [i for i in errors if NOT_RUN not in str(i.get("msg", ""))] or errors
    # an AI failure is the actionable cause even when the partial code's test failures are listed first: then it is
    # the only message (one concise line for a stopped coder)
    ai = [i for i in errors if _RANK.get(i.get("code"), 99) < _RANK["test_failed"]]
    if ai:
        errors = ai[:1]
    msgs = "; ".join(str(i.get("msg", "")).strip() for i in errors[:3]) or "failed"
    codes = [(doc.get("data") or {}).get("code") if isinstance(doc.get("data"), dict) else None, *(i.get("code") for i in errors)]
    known = sorted({c for c in codes if c in HINTS}, key=_RANK.get)
    hint = HINTS[known[0]] if known else ""
    if hint and (hint.strip(" —") in msgs or "run it again" in msgs and hint == AI_HINT):
        hint = ""   # the message already says it (e.g. WP1's llm_schema / no_skill texts)
    return "pkf " + " ".join(map(str, list(args)[:3])) + ": " + msgs + hint


AWS_KEEP = ("AWS_CA_BUNDLE", "AWS_DEFAULT_REGION", "AWS_REGION")


def _on(environ, name: str) -> bool:
    return str(environ.get(name, "")).strip().lower() in ("1", "true", "yes")


def child_env(args, environ) -> dict:
    """The environment of one `pkf` step: a step gets only the credentials it needs (AI-written code may run in it).
    AWS_* only for `run`/`check`; ANTHROPIC_API_KEY only for the AI commands; with Bedrock as the AI's provider
    (CLAUDE_CODE_USE_BEDROCK=1, or PKF_LLM_AWS_PASSTHROUGH=1) the AI commands keep AWS_* too (Env-2: the code-test
    child of `pkf code` scrubs its own environment anyway)."""
    cmd = str(args[0]) if args else ""
    bedrock = _on(environ, "CLAUDE_CODE_USE_BEDROCK") or _on(environ, "PKF_LLM_AWS_PASSTHROUGH")
    out = {**environ, "PYTHONUTF8": "1", "PYTHONSAFEPATH": "1"}
    for k in [k for k in out if k in SECRET_ENV or (k.startswith("AWS_") and k not in AWS_KEEP)]:
        keep = (cmd in NEEDS_AWS or (k == "ANTHROPIC_API_KEY" and cmd in NEEDS_AI)
                or (bedrock and cmd in NEEDS_AI and k.startswith("AWS_")))
        if not keep:
            out.pop(k)
    return out


class Ctx:
    """What an action gets: logging, the framework CLI, the job's folder and the problems found on the way."""

    def __init__(self, job_dir: Path, cmd: dict):
        self.dir = job_dir
        self.cmd = cmd
        self.root = Path(cmd["root"])
        self.pipeline = cmd["pipeline"]
        self.problems: list[str] = []
        self.studio = None  # the Studio the action opened (actions._open), for the push at the end of a stage job
        self._log = open(job_dir / "log", "a", encoding="utf-8", newline="\n")  # noqa: SIM115

    def log(self, msg: str) -> None:
        from pkf.llm.base import redact_secrets

        self._log.write(redact_secrets(str(msg))[0].rstrip("\n") + "\n")
        self._log.flush()

    def problem(self, msg: str) -> None:
        """Record something the user must look at; the job still succeeds (K1)."""
        msg = str(msg).strip()[:MAX_PROBLEM_LEN]
        if not msg or msg in self.problems:
            return
        self.log(f"  ⚠ {msg}")
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(msg)

    @staticmethod
    def _argv(args) -> list[str]:
        return [sys.executable, "-P", "-m", "pkf.cli", "--json", *map(str, args)]

    @staticmethod
    def _env(args, env: dict | None) -> dict:
        full_env = child_env(args, os.environ)
        full_env.update(env or {})
        return {k: v for k, v in full_env.items() if v is not None}  # None removes a variable

    def pkf(self, *args: str, cwd: Path, check: bool = False, env: dict | None = None, timeout: int = 3600,
            quiet: bool = False) -> dict:
        """Run `pkf --json …` in `cwd` (env: extra variables; None removes one); returns the Result envelope (ok, issues,
        data). Logs the command, a heartbeat while it runs, and its issues. `ok: false` raises with `check`, else it
        becomes a problem of the job unless `quiet` (calls whose failure is expected or handled by the caller)."""
        cmd = " ".join(map(str, args))
        self.log(f"$ pkf {cmd}")
        started = time.monotonic()
        try:
            p = subprocess.Popen(self._argv(args), cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                 env=self._env(args, env), text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            doc = {"ok": False, "issues": [{"msg": f"could not start pkf: {exc}", "code": "crash"}], "data": {}}
            return self._done(args, doc, check, quiet)
        beat = heartbeat_s()
        out = err = ""
        while True:
            left = timeout - (time.monotonic() - started)
            try:
                out, err = p.communicate(timeout=max(0.01, min(beat, left)))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - started >= timeout:
                    p.kill()
                    try:
                        p.communicate(timeout=30)
                    except (subprocess.TimeoutExpired, OSError, ValueError):
                        pass
                    self.log(f"  timed out after {timeout}s")
                    if check:
                        raise ActionError(f"pkf {args[0]} timed out") from None
                    doc = {"ok": False, "issues": [{"msg": f"timed out after {timeout}s", "code": "timeout"}], "data": {}}
                    return self._done(args, doc, False, quiet, logged=True)
                self.log(f"  … still running pkf {' '.join(map(str, args[:3]))} ({_elapsed(time.monotonic() - started)})")
        out, err = out or "", err or ""
        try:
            doc = json.loads(out)
            if not isinstance(doc, dict):
                raise ValueError("not an envelope")
        except (json.JSONDecodeError, ValueError):
            doc = {"ok": False, "issues": [{"msg": (err or out)[-1500:], "code": "crash"}], "data": {}}
        if err.strip():
            doc.setdefault("_stderr", err.strip()[-800:])
        return self._done(args, doc, check, quiet)

    def _done(self, args, doc: dict, check: bool, quiet: bool, logged: bool = False) -> dict:
        stderr = doc.pop("_stderr", "")
        if not logged:
            for i in doc.get("issues", []):
                self.log(f"  {i.get('severity', 'error')}: {i.get('file', '')} {i.get('msg', '')}"[:1500])
            self.log(f"  → {'OK' if doc.get('ok') else 'PROBLEMS'}")
            if stderr:
                self.log("  stderr: " + stderr)
        if not doc.get("ok"):
            if check:
                msgs = "; ".join(i.get("msg", "") for i in doc.get("issues", []))[:800]
                raise ActionError(f"pkf {' '.join(map(str, args[:2]))} failed: {msgs}")
            if not quiet:
                self.problem(problem_text(args, doc))
        return doc


class ActionError(Exception):
    pass


def _result(code: int, result: dict, ctx: Ctx) -> dict:
    problems = []
    for p in [*ctx.problems, *(result.get("problems") or [])]:
        if p not in problems:
            problems.append(p)
    return {"ok": code == 0, **result, "problems": problems[:MAX_PROBLEMS],
            "severity": result.get("severity") or ("warning" if problems else None)}


def run(job_dir: Path) -> int:
    job_dir = Path(job_dir)
    cmd = json.loads((job_dir / "cmd.json").read_text(encoding="utf-8"))
    ctx = Ctx(job_dir, cmd)
    code, result = 1, {}
    try:
        from .actions import ACTIONS

        fn = ACTIONS[cmd["action"]]
        ctx.log(f"== {cmd.get('title') or cmd['action']} ({cmd['pipeline']})")
        result = fn(ctx, **(cmd.get("args") or {})) or {}
        code = 0
        ctx.log("== done" + (f" with {len(ctx.problems)} problem(s)" if ctx.problems else ""))
    except ActionError as exc:
        result = {"ok": False, "error": str(exc)}
        ctx.log(f"== failed: {exc}")
    except Exception as exc:  # noqa: BLE001 - every failure lands in the log
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ctx.log("== crashed:\n" + traceback.format_exc())
    (job_dir / "result.json").write_text(json.dumps(_result(code, result, ctx), indent=2, default=str), encoding="utf-8", newline="\n")
    (job_dir / "exit").write_text(str(code), encoding="utf-8", newline="\n")
    try:
        from .jobs import release  # 0.8.0: a lane job releases its lane lock, never the pipeline's

        release(Path(cmd["root"]), cmd["pipeline"], cmd.get("id") or job_dir.name, cmd.get("lane") or None)
    except Exception:  # noqa: BLE001 - OSError, an unknown lane: the lock goes stale and is released by jobs.current
        pass
    return code


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1])))
