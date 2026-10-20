"""The Assistant's in-process MCP tools and the path guard both the tools and the session's PreToolUse hook enforce.

Read roots: the framework source, the pipeline worktree, `.pkf-studio/jobs/`, `.pkf-studio/flows/<p>/`, and (0.8.0)
`.pkf-studio/cache/` where the code map lives. Write root (fix phase only): the worktree minus the protected paths,
`dags/`, `.pkf-studio/`, every `*.lock.yaml` / `*.lock.json`, and anything reached through a symlink — and of that only
this pipeline's own files (0.8.0: before its spec is committed only `requirements/<p>/`, `pipelines/<p>.yaml` and
`review/<p>/`; a file entry matches the whole path, a folder entry its prefix; case-insensitive). Every tool result is
truncated (TOOL_MAX chars) and says so. `run_tests` runs the framework CLI the way the Studio's jobs do (same argv,
`runner.child_env`): AI-written code never sees credentials. In the assistant lane (0.8.0: a read-only session beside a
running pipeline job) there is no `run_tests`.

0.8.0 tools: `workflow_state` (the run brief, refreshed mid-session), `framework_map` (search the code map) and
`remember` (one line into the pipeline's memory notes — the only file a read-only phase writes, through
memory.remember, never through Edit/Write)."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from pkf.llm.base import redact_secrets

from .. import PROTECTED_PATHS, STUDIO_DIR

SERVER = "pkf_assistant"
TOOL_MAX = 12_000
DIFF_MAX = 20_000
TESTS_TIMEOUT_S = 600
WRITE_DENIED_PREFIXES = (*PROTECTED_PATHS, "dags/", f"{STUDIO_DIR}/", ".git/")
WRITE_DENIED_GLOBS = ("*.lock.yaml", "*.lock.json")
KINDS = ("trigger", "source", "destination", "transformation")


class PathDenied(Exception):
    pass


@dataclass
class Context:
    """What one Assistant session may see: paths, the failed step and what the action pre-computed (gate states, flow
    state, recent events) so the agent never calls GitHub itself."""

    root: Path
    pipeline: str
    worktree: Path
    framework_dir: Path
    mode: str = "dev"
    job_id: str | None = None
    step_id: str | None = None
    gates: dict = field(default_factory=dict)
    flow: dict | None = None
    events: list[dict] = field(default_factory=list)
    phase: str = "diagnose"
    lane: bool = False              # 0.8.0: the assistant lane (read-only, beside a running pipeline job: no run_tests)

    @property
    def jobs_dir(self) -> Path:
        return Path(self.root) / STUDIO_DIR / "jobs"

    @property
    def flows_dir(self) -> Path:
        return Path(self.root) / STUDIO_DIR / "flows" / self.pipeline

    @property
    def cache_dir(self) -> Path:
        return Path(self.root) / STUDIO_DIR / "cache"

    @property
    def read_roots(self) -> dict[str, Path]:
        return {"framework": Path(self.framework_dir), "worktree": Path(self.worktree), "jobs": self.jobs_dir, "flows": self.flows_dir,
                "cache": self.cache_dir}

    @property
    def write_root(self) -> Path:
        return Path(self.worktree)


# ------------------------------------------------------------------------------------------------------ path guard
def _resolved(path: str | Path, base: Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(base) / p
    return Path(os.path.realpath(p))


def check_read(ctx: Context, path: str | Path) -> Path:
    """The real path when it is under an allowed read root, else PathDenied (symlinks are followed first)."""
    real = _resolved(path, ctx.worktree)
    for alias, root in ctx.read_roots.items():
        try:
            real.relative_to(Path(os.path.realpath(root)))
            return real
        except ValueError:
            continue
    raise PathDenied(f"{path} is outside the allowed folders ({', '.join(ctx.read_roots)})")


FRAMEWORK_PACKAGES = ("pkf", "pkf_runtime", "pkf_studio")
FRAMEWORK_TOP = ("src/", "pkf/", "pkf_runtime/", "pkf_studio/", "steps-plugin/", "package-factory/", "dryrun/")
FRAMEWORK_FILES = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pkf.toml")


def _framework_roots(ctx: Context) -> list[Path]:
    roots = []
    for r in [ctx.framework_dir, *(_package_dir(n) for n in FRAMEWORK_PACKAGES)]:
        if r:
            try:
                roots.append(Path(os.path.realpath(r)))
            except OSError:
                continue
    return roots


def _package_dir(name: str) -> Path | None:
    try:
        import importlib.util

        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent


def framework_path(ctx: Context, real: Path, rel_posix: str) -> str | None:
    """Why `rel_posix` (real path `real`) is framework code, or None: under the framework source or an installed
    framework package (a framework copy inside the repository included), a framework folder at the top of the
    repository, or a project/packaging file. The assistant never writes these (0.6.1)."""
    try:
        r = Path(os.path.realpath(real))
    except OSError:
        r = Path(real)
    for root in _framework_roots(ctx):
        try:
            r.relative_to(root)
            return f"under {root}"
        except ValueError:
            continue
    low = rel_posix.lower()                        # case-insensitive file systems (Windows, macOS)
    if low.startswith(FRAMEWORK_TOP) or low in FRAMEWORK_FILES:
        return "a framework or packaging path"
    return None


def _committed(ctx: Context):
    """A reader of the worktree's COMMITTED files (HEAD): what decides ownership cannot be widened by the agent's own
    uncommitted edits (e.g. pointing the connection at another pipeline's source)."""
    wt = Path(ctx.worktree)

    def read(rel: str) -> str | None:
        p = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=str(wt), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        return p.stdout if p.returncode == 0 else None
    return read


def _pipeline_paths_at_head(ctx: Context, read, pipeline: str) -> tuple[str, ...] | None:
    from .. import ledger as L

    text = read(f"pipelines/{pipeline}.yaml")
    if text is None:
        return None
    conn = (L._yaml(read, f"pipelines/{pipeline}.yaml") or {}).get("connection")
    return tuple(L.pipeline_paths(pipeline, L.components(read, pipeline), conn))


def path_matches(rel_posix: str, entries) -> bool:
    """`rel_posix` is one of `entries` (ledger.pipeline_paths): an entry ending in "/" is a folder and matches by prefix,
    any other entry names one file and matches the whole path only (0.8.0: `pipelines/p.yaml` no longer lets
    `pipelines/p.yaml.bak` through, nor `sources/x.py` `sources/x.pyc`). Case-insensitive, like framework_path."""
    low = rel_posix.lower()
    for e in entries or ():
        e = str(e).lower()
        if (low.startswith(e) if e.endswith("/") else low == e):
            return True
    return False


def pre_spec_paths(pipeline: str) -> tuple[str, ...]:
    """What the assistant may write before the pipeline's spec is committed (S1): its requirement folder, its pipeline
    file and its review folder — nothing else in the worktree (0.8.0; 0.6.1 let almost any file through then)."""
    return (f"requirements/{pipeline}/", f"pipelines/{pipeline}.yaml", f"review/{pipeline}/")


def own_prefixes(ctx: Context) -> tuple[str, ...] | None:
    """The path prefixes of the pipeline's own files as committed (ledger.pipeline_paths, reused components included),
    or None before its spec is committed (S1)."""
    try:
        return _pipeline_paths_at_head(ctx, _committed(ctx), ctx.pipeline)
    except Exception:  # noqa: BLE001
        return None


def others_prefixes(ctx: Context) -> tuple[str, ...]:
    """The committed files of every OTHER pipeline (their specs, components, connections, review/ …): the assistant
    never writes them — not even a component or connection this pipeline shares with them, because the change would
    reach the other pipeline too (0.6.1)."""
    read = _committed(ctx)
    try:
        p = subprocess.run(["git", "ls-tree", "--name-only", "HEAD", "pipelines/"], cwd=str(ctx.worktree), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        names = [Path(x).stem for x in p.stdout.splitlines() if x.endswith(".yaml") and not x.endswith(".lock.yaml")]
    except Exception:  # noqa: BLE001
        return ()
    out: list[str] = []
    for n in names:
        if n.lower() == ctx.pipeline.lower():
            continue
        try:
            out += list(_pipeline_paths_at_head(ctx, read, n) or ())
        except Exception:  # noqa: BLE001
            continue
    return tuple(out)


def check_write(ctx: Context, path: str | Path) -> Path:
    """The real path when it may be written: under the worktree, not protected, not a lock, not through a symlink."""
    root = Path(os.path.realpath(ctx.write_root))
    p = Path(path)
    if not p.is_absolute():
        p = Path(ctx.worktree) / p
    lexical = Path(os.path.normpath(os.path.abspath(p)))
    for cand in (lexical, Path(os.path.realpath(lexical))):
        try:
            rel = cand.relative_to(root)
            break
        except ValueError:
            continue
    else:
        raise PathDenied(f"{path} is outside the pipeline worktree")
    rel_posix = rel.as_posix()
    low = rel_posix.lower()                                   # 0.8.0: case-insensitive file systems (like framework_path)
    denied = tuple(x.lower() for x in WRITE_DENIED_PREFIXES)
    if rel_posix in ("", ".") or low.startswith(denied) or low in {x.rstrip("/") for x in denied}:
        raise PathDenied(f"{rel_posix} is protected (ledger, dags/, .pkf-studio/, team files): the Studio owns it")
    framework = framework_path(ctx, cand, rel_posix)
    if framework:                                             # 0.6.1: never, in any phase, whatever the plan says
        raise PathDenied(f"{rel_posix} is framework code ({framework}): the assistant never changes the framework — fix the "
                         "pipeline's own files, or report the framework problem to the platform team")
    own = own_prefixes(ctx)
    if own is None:                                           # 0.8.0: before the spec is committed, only these
        if not path_matches(rel_posix, pre_spec_paths(ctx.pipeline)):
            raise PathDenied(f"{rel_posix} is not writable yet: before pipeline {ctx.pipeline}'s spec is committed the assistant "
                             f"only changes {', '.join(pre_spec_paths(ctx.pipeline))}")
    elif not path_matches(rel_posix, own):                    # 0.6.1: one pipeline's assistant never touches another's files
        raise PathDenied(f"{rel_posix} is not one of pipeline {ctx.pipeline}'s own files: the assistant only changes this "
                         "pipeline (its requirement, specs, component code and review files)")
    theirs = others_prefixes(ctx)
    if theirs and path_matches(rel_posix, theirs):
        raise PathDenied(f"{rel_posix} belongs to (or is shared with) another pipeline: a change here would reach that "
                         "pipeline too — a person decides")
    if low.startswith("requirements/") and rel.name.lower() == "extracted.txt":   # 0.8.0: the user's document (knowledge.md §6)
        raise PathDenied(f"{rel_posix} is the text of the user's requirement document: change the requirement in "
                         f"pipelines/{ctx.pipeline}.yaml instead")
    if any(fnmatch.fnmatch(rel.name.lower(), g) for g in WRITE_DENIED_GLOBS):
        raise PathDenied(f"{rel_posix} is a lock file written by the framework: re-plan or regenerate instead of editing it")
    # no symlink anywhere between the root and the file (an existing symlinked parent or leaf)
    cur = root
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise PathDenied(f"{rel_posix} goes through a symlink")
    return root / rel


def _clip(text: str, limit: int = TOOL_MAX) -> str:
    text = redact_secrets(str(text))[0]
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated: {len(text) - limit} more characters; narrow the request (start/end, glob, tail)]"


def _text(text: str, limit: int = TOOL_MAX, error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": _clip(text, limit)}]}
    if error:
        out["is_error"] = True
    return out


def _json(doc, limit: int = TOOL_MAX) -> dict:
    return _text(json.dumps(doc, indent=1, default=str), limit)


def _root(ctx: Context, alias: str) -> Path:
    roots = ctx.read_roots
    if alias not in roots:
        raise PathDenied(f"unknown root '{alias}'; use one of {', '.join(roots)}")
    return roots[alias]


# ------------------------------------------------------------------------------------------------ tool functions
# Plain functions first (unit-testable without the SDK); `make_tools` wraps them with the SDK's @tool decorator.
def _artifact_files(ctx: Context, limit: int = 300) -> list[str]:
    wt = Path(ctx.worktree)
    if not wt.exists():
        return []
    out: list[str] = []
    for top in ("pipelines", "requirements", "sources", "destinations", "transformations", "triggers", "review", "infra", "dags"):
        d = wt / top
        if not d.exists():
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file() and ".terraform" not in f.parts:
                out.append(f.relative_to(wt).as_posix())
            if len(out) >= limit:
                return out
    return out


def _job_result(ctx: Context, job_id: str | None) -> dict | None:
    if not job_id:
        return None
    d = ctx.jobs_dir / job_id
    out: dict = {"id": job_id}
    for name in ("cmd.json", "result.json"):
        try:
            out[name[:-5]] = json.loads((d / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[name[:-5]] = None
    res = out.get("result") or {}
    out["problems"] = res.get("problems") or []
    out["severity"] = res.get("severity")
    out["error"] = res.get("error")
    out["ok"] = res.get("ok")
    return out


def pkf_context(ctx: Context) -> dict:
    """The one-call briefing: pipeline, mode, gate states, the current flow step, the failed step and its job result,
    the last 40 events and the artifact files in the worktree."""
    flow = ctx.flow or {}
    return {
        "pipeline": ctx.pipeline, "mode": ctx.mode, "phase": ctx.phase,
        "worktree": str(ctx.worktree), "framework_dir": str(ctx.framework_dir),
        "gates": ctx.gates,
        "flow": {k: flow.get(k) for k in ("status", "current", "message", "mode", "started_at", "updated_at") if k in flow},
        "failed_step": {"id": ctx.step_id, "job": _job_result(ctx, ctx.job_id)},
        "recent_events": ctx.events[-40:],
        "files": _artifact_files(ctx),
    }


def read_job_log(ctx: Context, job_id: str | None = None, tail: int = 400) -> str:
    jid = job_id or ctx.job_id
    if not jid:
        raise PathDenied("no job id: pass job_id (see list_jobs)")
    path = check_read(ctx, ctx.jobs_dir / jid / "log")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"(job {jid} has no log)"
    tail = max(1, int(tail or 400))
    skipped = max(0, len(lines) - tail)
    head = f"[{skipped} earlier line(s) skipped; {len(lines)} in total]\n" if skipped else ""
    return head + "\n".join(lines[-tail:])


def list_jobs(ctx: Context, n: int = 10) -> list[dict]:
    from .. import jobs

    return [{k: j.get(k) for k in ("id", "state", "action", "title", "started_at")} | {"problems": ((j.get("result") or {}).get("problems") or [])[:3],
                                                                                      "error": (j.get("result") or {}).get("error")}
            for j in jobs.recent(ctx.root, ctx.pipeline, n=max(1, int(n or 10)))]


def read_file(ctx: Context, path: str, start: int = 1, end: int = 400) -> str:
    real = check_read(ctx, path)
    try:
        lines = real.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise PathDenied(f"cannot read {path}: {exc.strerror or exc}") from None
    start = max(1, int(start or 1))
    end = max(start, int(end or 400))
    body = "\n".join(f"{i:>5}\t{line}" for i, line in enumerate(lines[start - 1:end], start))
    note = f"\n[lines {start}-{min(end, len(lines))} of {len(lines)}]" if len(lines) > end or start > 1 else ""
    return body + note


def list_files(ctx: Context, root_alias: str, glob: str = "**/*", limit: int = 400) -> list[str]:
    base = _root(ctx, root_alias)
    out = []
    if not base.exists():
        return out
    for f in sorted(base.glob(glob or "**/*")):
        if f.is_file() and ".git" not in f.parts and "__pycache__" not in f.parts:
            out.append(f.relative_to(base).as_posix())
            if len(out) >= limit:
                out.append(f"… [more than {limit} files; narrow the glob]")
                break
    return out


def grep(ctx: Context, pattern: str, root_alias: str, glob: str = "**/*", max: int = 50) -> list[str]:  # noqa: A002 - the contract's name
    base = _root(ctx, root_alias)
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise PathDenied(f"bad pattern: {exc}") from None
    limit = builtins_max(1, int(max or 50))
    out: list[str] = []
    if not base.exists():
        return out
    for f in sorted(base.glob(glob or "**/*")):
        if not f.is_file() or ".git" in f.parts or "__pycache__" in f.parts or f.suffix in (".pyc", ".zip", ".png", ".whl"):
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        out.append(f"{f.relative_to(base).as_posix()}:{i}: {line.rstrip()[:300]}")
                        if len(out) >= limit:
                            out.append(f"… [stopped at {limit} matches]")
                            return out
        except OSError:
            continue
    return out


builtins_max = max


def git_diff(ctx: Context) -> str:
    wt = Path(ctx.worktree)
    if not (wt / ".git").exists():
        return "(no git worktree)"
    try:
        out = subprocess.run(["git", "-c", "core.quotepath=off", "diff", "--no-color"], cwd=str(wt), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
        unt = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=str(wt), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120).stdout.split()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(git diff failed: {exc})"
    text = out.stdout
    if unt:
        text += "\n# untracked files:\n" + "\n".join(f"+ {u}" for u in unt)
    return text.strip() or "(no changes)"


def changed_files(ctx: Context) -> list[str]:
    wt = Path(ctx.worktree)
    if not (wt / ".git").exists():
        return []
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=str(wt), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line[3:].strip() for line in out.splitlines() if len(line) > 3]


def _pkf(ctx: Context, args: list[str], timeout: int) -> dict:
    """`pkf --json …` in the worktree exactly as runner.Ctx runs it (same argv, same scrubbed environment)."""
    from ..runner import child_env

    argv = [sys.executable, "-P", "-m", "pkf.cli", "--json", *map(str, args)]
    started = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=str(ctx.worktree), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, env=child_env(args, os.environ), timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "issues": [{"code": "timeout", "msg": f"pkf {' '.join(args[:3])} timed out after {timeout}s"}], "data": {}}
    except OSError as exc:
        return {"ok": False, "issues": [{"code": "crash", "msg": f"could not start pkf: {exc}"}], "data": {}}
    try:
        doc = json.loads(p.stdout)
        if not isinstance(doc, dict):
            raise ValueError
    except ValueError:
        doc = {"ok": False, "issues": [{"code": "crash", "msg": (p.stderr or p.stdout)[-1500:]}], "data": {}}
    doc["command"] = "pkf " + " ".join(map(str, args))
    doc["seconds"] = round(time.monotonic() - started, 1)
    if p.stderr.strip():
        doc["stderr"] = p.stderr.strip()[-800:]
    return doc


def run_tests(ctx: Context, kind: str | None = None, name: str | None = None) -> dict:
    """validate → the component's tests (or every changed/failing component's) → the end-to-end examples → the draft
    build, like S4's Run tests & build again; stops after the first step that fails. 600 s in total."""
    deadline = time.monotonic() + TESTS_TIMEOUT_S
    steps: list[dict] = []

    def left() -> int:
        return builtins_max(5, int(deadline - time.monotonic()))

    def slim(doc: dict) -> dict:
        data = doc.get("data") or {}
        keep = {k: data[k] for k in ("examples", "tests", "results", "would_write", "unapproved", "code") if k in data}
        return {"command": doc.get("command"), "ok": doc.get("ok"), "seconds": doc.get("seconds"), "issues": (doc.get("issues") or [])[:30],
                "data": keep, **({"stderr": doc["stderr"]} if doc.get("stderr") else {})}

    v = _pkf(ctx, ["validate"], left())
    steps.append(slim(v))
    if not v.get("ok"):
        return {"ok": False, "stopped_at": "validate", "steps": steps}
    if kind and name:
        if kind not in KINDS:
            return {"ok": False, "stopped_at": "arguments", "steps": steps, "error": f"kind must be one of {', '.join(KINDS)}"}
        targets = [(kind, name)]
    else:
        st = _pkf(ctx, ["status", ctx.pipeline], left())
        rows = ((st.get("data") or {}).get("pipelines") or [{}])[0].get("components") or []
        targets = [(c["kind"], c["name"]) for c in rows if c.get("stage") in ("code_changed", "code_failing", "code_generated")]
    ok = True
    for k, n in targets:
        t = _pkf(ctx, ["test", k, n], left())
        steps.append(slim(t))
        ok = ok and bool(t.get("ok"))
    if not ok:
        return {"ok": False, "stopped_at": "component tests", "steps": steps}
    ex = _pkf(ctx, ["test", "pipeline", ctx.pipeline], left())
    steps.append(slim(ex))
    if not ex.get("ok"):
        return {"ok": False, "stopped_at": "pipeline examples", "steps": steps}
    b = _pkf(ctx, ["build", ctx.pipeline, "--draft"], left())
    steps.append(slim(b))
    return {"ok": bool(b.get("ok")), "stopped_at": None if b.get("ok") else "draft build", "steps": steps}


def write_file(ctx: Context, path: str, content: str) -> str:
    real = check_write(ctx, path)
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(str(content), encoding="utf-8", newline="\n")
    return f"wrote {real.relative_to(Path(os.path.realpath(ctx.write_root))).as_posix()} ({len(str(content))} chars)"


def edit_file(ctx: Context, path: str, old: str, new: str) -> str:
    real = check_write(ctx, path)
    try:
        text = real.read_text(encoding="utf-8")
    except OSError as exc:
        raise PathDenied(f"cannot read {path}: {exc.strerror or exc}") from None
    n = text.count(old)
    if n != 1:
        raise PathDenied(f"`old` occurs {n} times in {path}; it must occur exactly once (include more context)")
    real.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
    return f"edited {real.relative_to(Path(os.path.realpath(ctx.write_root))).as_posix()}"


def workflow_state(ctx: Context, n_log: int = 8) -> dict:
    """The run brief (brief.build) as it stands now: done, now, next, issue, blocking, gates, pilot, memory, log tail."""
    from . import brief

    return brief.from_context(ctx, n_log=builtins_max(0, min(int(n_log if n_log is not None else 8), 200)))


def framework_map(ctx: Context, query: str, max: int = 80) -> list[str]:  # noqa: A002 - like grep's
    """The lines of the framework code map that match every word of `query`, under their file's heading."""
    from . import codemap

    return codemap.search(ctx.root, ctx.framework_dir, query, limit=builtins_max(1, min(int(max or 80), 400)))


def remember(ctx: Context, note: str) -> str:
    """One line into the pipeline's memory notes (memory.md): the only write a read-only phase makes, at a fixed path."""
    from . import memory

    return memory.remember(ctx.root, ctx.pipeline, note)


# ------------------------------------------------------------------------------------------------------ SDK wiring
def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def make_tools(ctx: Context, write: bool = False, lane: bool | None = None) -> list:
    """The SDK tool objects bound to `ctx`; `write` adds write_file / edit_file (fix phase only); `lane` (default:
    ctx.lane) drops run_tests — a lane session runs beside a pipeline job and never runs the framework (0.8.0)."""
    lane = ctx.lane if lane is None else lane
    from claude_agent_sdk import tool

    def wrap(fn, *, as_json: bool, limit: int = TOOL_MAX):
        async def handler(args: dict) -> dict:
            try:
                out = fn(ctx, **(args or {}))
            except PathDenied as exc:
                return _text(f"refused: {exc}", error=True)
            except Exception as exc:  # noqa: BLE001 - the model gets the error as a tool error, the session goes on
                return _text(f"{type(exc).__name__}: {exc}", error=True)
            return _json(out, limit) if as_json else _text(out, limit)
        return handler

    tools = [
        tool("pkf_context", "The briefing: pipeline, mode, gate states, flow step, the failed step with its job result and problems, "
             "recent events, artifact files. Call it first.", _schema({}, []))(wrap(pkf_context, as_json=True)),
        tool("read_job_log", "The last `tail` lines of a Studio job's log (default: the failed step's job).",
             _schema({"job_id": {"type": ["string", "null"]}, "tail": {"type": "integer"}}, []))(wrap(read_job_log, as_json=False)),
        tool("list_jobs", "The pipeline's recent jobs: id, state, action, first problems.",
             _schema({"n": {"type": "integer"}}, []))(wrap(list_jobs, as_json=True)),
        tool("read_file", "Numbered lines start..end of a file under an allowed root (framework source, worktree, jobs, flows). "
             "Relative paths are under the worktree.",
             _schema({"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
                     ["path"]))(wrap(read_file, as_json=False)),
        tool("list_files", "Files under a root alias (worktree | framework | jobs | flows) matching a glob.",
             _schema({"root_alias": {"type": "string"}, "glob": {"type": "string"}}, ["root_alias"]))(wrap(list_files, as_json=True)),
        tool("grep", "Regex search (path:line: text) under a root alias, limited to `max` matches.",
             _schema({"pattern": {"type": "string"}, "root_alias": {"type": "string"}, "glob": {"type": "string"}, "max": {"type": "integer"}},
                     ["pattern", "root_alias"]))(wrap(grep, as_json=True)),
        tool("git_diff", "The worktree's uncommitted changes as a unified diff (plus untracked files).",
             _schema({}, []))(wrap(git_diff, as_json=False, limit=DIFF_MAX)),
        tool("workflow_state", "The run brief as it stands NOW (the one in the prompt may be minutes old): done stages, the "
             "current step with its status and time, the next steps and who does them, the issue, what blocks the run, the "
             "gates, the pilot, the memory notes and the last `n_log` log lines.",
             _schema({"n_log": {"type": "integer"}}, []))(wrap(workflow_state, as_json=True)),
        tool("framework_map", "Search the framework code map (also in your system prompt): every line whose file, function or "
             "class name or purpose contains all the words of `query`, under its file. Then read the code with read_file / grep "
             "in root `framework`.",
             _schema({"query": {"type": "string"}, "max": {"type": "integer"}}, ["query"]))(wrap(framework_map, as_json=True)),
        tool("remember", "Keep one short note (≤300 characters) in this pipeline's memory, shown to every later assistant "
             "session: a root cause you found, a decision the user made, something that did not work. Never a secret, a "
             "credential or personal data.",
             _schema({"note": {"type": "string"}}, ["note"]))(wrap(remember, as_json=False)),
    ]
    if not lane:
        tools.append(
            tool("run_tests", "pkf validate, then the component's tests (kind + name) or every changed component's, the end-to-end "
                 "examples and the draft build, in the hardened runner (no credentials). Up to 600 s.",
                 _schema({"kind": {"type": ["string", "null"]}, "name": {"type": ["string", "null"]}}, []))(wrap(run_tests, as_json=True)))
    if write:
        tools += [
            tool("write_file", "Write a whole file under the worktree (never locks, dags/, .pkf-studio/, protected files).",
                 _schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]))(wrap(write_file, as_json=False)),
            tool("edit_file", "Replace one exact occurrence of `old` with `new` in a worktree file.",
                 _schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, ["path", "old", "new"]))(
                wrap(edit_file, as_json=False)),
        ]
    return tools


def tool_names(write: bool = False, lane: bool = False) -> list[str]:
    names = ["pkf_context", "read_job_log", "list_jobs", "read_file", "list_files", "grep", "git_diff",
             "workflow_state", "framework_map", "remember"]
    if not lane:
        names.append("run_tests")
    if write:
        names += ["write_file", "edit_file"]
    return [f"mcp__{SERVER}__{n}" for n in names]


def make_server(ctx: Context, write: bool = False, lane: bool | None = None):
    from claude_agent_sdk import create_sdk_mcp_server

    return create_sdk_mcp_server(SERVER, tools=make_tools(ctx, write=write, lane=lane))
