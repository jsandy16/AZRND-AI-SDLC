"""Long actions (AI calls, tests, Terraform, runs) as detached processes whose whole state is in files, so a browser
reload, a second tab, an app restart or a closed console never loses or duplicates a job.

.pkf-studio/jobs/<id>/cmd.json      what to run (action, pipeline, args)
                     /pid           "<pid> <create_time>" of the runner
                     /log           the job's log (scrubbed of secrets), written by the runner only
                     /crash.log     the runner's own stdout/stderr (stray prints, a Python crash): `crash_tail`
                     /result.json   the action's result
                     /exit          the exit code (0 ok, 1 failed)
.pkf-studio/locks/<pipeline>.lock   one job per pipeline at a time (stale when the pid is dead or was reused); taken
                                    before the job folder exists, so two clicks can never start two jobs
.pkf-studio/locks/assistant/<pipeline>.lock
                                    0.8.0, the assistant lane: one read-only assistant job (ask, diagnose, brief) per
                                    pipeline, beside the pipeline's own job. Its cmd.json says "lane": "assistant". Lane
                                    jobs never count as the pipeline's job: current(), running_all(), recent() leave them
                                    out unless asked (lane=…), so busy(), the run's tick, the pilot, Cancel and "Last job"
                                    behave as before; the job's process has PKF_STUDIO_LANE set and the Studio refuses
                                    every change from it (service.Studio._refuse_lane).

The job's process (and every `pkf`/`git` it runs) has PKF_STUDIO_JOB=<job id>: the Studio service lets that job
merge its own record PR while refusing every other git change during a job (K11).
PKF_STUDIO_INLINE_JOBS=1 runs jobs inside the calling process (tests).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import STUDIO_DIR


class JobError(Exception):
    pass


def jobs_dir(root: Path) -> Path:
    return Path(root) / STUDIO_DIR / "jobs"


LANES = ("assistant",)
LANE_ACTIONS = {"assistant": ("assist",)}
LANE_ENV = "PKF_STUDIO_LANE"


def _lock(root: Path, pipeline: str, lane: str | None = None) -> Path:
    base = Path(root) / STUDIO_DIR / "locks"
    if lane:
        if lane not in LANES:
            raise JobError(f"unknown job lane '{lane}' (known: {', '.join(LANES)})")
        return base / lane / f"{pipeline}.lock"     # a subfolder: running_all's `locks/*.lock` never sees it
    return base / f"{pipeline}.lock"


def lock_path(root: Path, pipeline: str, lane: str | None = None) -> Path:
    return _lock(root, pipeline, lane)


def release(root: Path, pipeline: str, job_id: str, lane: str | None = None) -> None:
    """The runner's end-of-job release. The pipeline lock goes as before (unconditionally); a lane lock only while it
    still names this job, so a finishing lane job never releases another one."""
    lk = _lock(root, pipeline, lane)
    if lane:
        try:
            if json.loads(lk.read_text()).get("job") != job_id:
                return
        except (OSError, ValueError, AttributeError):
            pass
    lk.unlink(missing_ok=True)


def _alive(pid: int, created: float | None) -> bool:
    try:
        import psutil

        p = psutil.Process(pid)
        if created is not None and abs(p.create_time() - created) > 1.0:
            return False  # the pid was reused by another process
        return p.status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001 - NoSuchProcess, AccessDenied, psutil missing
        return False


def _read_pid(d: Path) -> tuple[int, float | None] | None:
    try:
        parts = (d / "pid").read_text().split()
        return int(parts[0]), (float(parts[1]) if len(parts) > 1 else None)
    except (OSError, ValueError, IndexError):
        return None


def status(root: Path, job_id: str) -> dict:
    d = jobs_dir(root) / job_id
    if not d.exists():
        return {"id": job_id, "state": "unknown"}
    try:
        cmd = json.loads((d / "cmd.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"id": job_id, "state": "starting" if _young(d) else "crashed"}   # jobs.start is writing it
    out = {"id": job_id, **{k: cmd.get(k) for k in ("action", "pipeline", "started_at", "title")}}
    if cmd.get("lane"):
        out["lane"] = cmd["lane"]
    if (d / "exit").exists():
        code = int((d / "exit").read_text().strip() or 1)
        res = {}
        try:
            res = json.loads((d / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        return {**out, "state": "succeeded" if code == 0 else "failed", "exit": code, "result": res}
    pid = _read_pid(d)
    if pid and _alive(*pid):
        return {**out, "state": "running"}
    if not pid and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(cmd["started_at"])).total_seconds() < 30:
        return {**out, "state": "starting"}
    return {**out, "state": "crashed"}


def log(root: Path, job_id: str, tail: int = 400) -> str:
    p = jobs_dir(root) / job_id / "log"
    if not p.exists():
        return ""
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])


def current(root: Path, pipeline: str, lane: str | None = None) -> str | None:
    """The job holding the pipeline's lock, releasing a stale lock (dead runner). lane=None: the pipeline's own job only
    (a lane job never counts); lane="assistant": the pipeline's assistant-lane job."""
    lk = _lock(root, pipeline, lane)
    if not lk.exists():
        return None
    try:
        job_id = json.loads(lk.read_text())["job"]
    except (OSError, ValueError, KeyError):
        lk.unlink(missing_ok=True)
        return None
    st = status(root, job_id)
    if st["state"] in ("running", "starting") or (st["state"] == "unknown" and _young(lk)):
        return job_id   # (unknown + a young lock: jobs.start took the lock and is creating the job folder)
    lk.unlink(missing_ok=True)
    return None


def _young(path: Path, seconds: float = 30.0) -> bool:
    try:
        return time.time() - path.stat().st_mtime < seconds
    except OSError:
        return False


def crash_tail(root: Path, job_id: str, n: int = 40) -> str:
    """The last `n` lines the job's process wrote outside its log (stray prints, a Python traceback): E12."""
    p = jobs_dir(root) / job_id / "crash.log"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def running_all(root: Path, lanes: bool = False) -> list[dict]:
    """The running jobs of every pipeline (the sidebar shows them; E18). `locks/*.lock` does not descend into the lane
    subfolders, so lane jobs are left out unless `lanes=True` (then they come after, with "lane" set)."""
    d = Path(root) / STUDIO_DIR / "locks"
    out = []
    for lk in sorted(d.glob("*.lock")) if d.exists() else []:
        if not lk.is_file():
            continue
        jid = current(root, lk.stem)
        if jid:
            out.append({**status(root, jid), "pipeline": lk.stem})
    if lanes:
        for lane in LANES:
            for lk in sorted((d / lane).glob("*.lock")) if (d / lane).is_dir() else []:
                jid = current(root, lk.stem, lane)
                if jid:
                    out.append({**status(root, jid), "pipeline": lk.stem, "lane": lane})
    return out


def recent(root: Path, pipeline: str, n: int = 20, lane: str | None = None) -> list[dict]:
    """The pipeline's latest jobs, newest first. lane=None: its own jobs only (the "Last job" banners and the Jobs tab
    stay about pipeline jobs); lane="assistant": only that lane's jobs; lane="*": all."""
    d = jobs_dir(root)
    if not d.exists():
        return []
    rows = []
    for j in d.iterdir():
        try:
            cmd = json.loads((j / "cmd.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if cmd.get("pipeline") != pipeline:
            continue
        if lane == "*" or (cmd.get("lane") or None) == lane:
            rows.append((cmd.get("started_at", ""), j.name))
    return [status(root, jid) for _, jid in sorted(rows, reverse=True)[:n]]


def start(root: Path, pipeline: str, action: str, args: dict | None = None, title: str = "", lane: str | None = None) -> str:
    """Take the pipeline's lock first (O_EXCL), then create the job folder (K11): of two clicks, exactly one gets the
    lock; a folder that cannot be created releases it again. lane="assistant" (0.8.0): the lane's own lock instead,
    so a read-only assistant job runs beside the pipeline's job; only the lane's actions (assist) may run there."""
    root = Path(root)
    if lane and action not in LANE_ACTIONS.get(lane, ()):
        raise JobError(f"only {', '.join(LANE_ACTIONS.get(lane, ())) or 'nothing'} runs in the {lane} lane, not {action}")
    busy = "the assistant is already working on" if lane else "a job is already running for"
    if current(root, pipeline, lane):
        raise JobError(f"{busy} {pipeline}; wait for it or cancel it")
    job_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    lk = _lock(root, pipeline, lane)
    lk.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lk, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise JobError(f"{busy} {pipeline}") from None
    try:
        os.write(fd, json.dumps({"job": job_id}).encode())
    finally:
        os.close(fd)
    d = jobs_dir(root) / job_id
    try:
        d.mkdir(parents=True)
        cmd = {"id": job_id, "action": action, "pipeline": pipeline, "args": args or {}, "root": str(root), "title": title or action,
               "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        if lane:
            cmd["lane"] = lane
        (d / "cmd.json").write_text(json.dumps(cmd, indent=2), encoding="utf-8", newline="\n")
    except BaseException:
        lk.unlink(missing_ok=True)
        raise
    if os.environ.get("PKF_STUDIO_INLINE_JOBS") == "1":
        from .runner import run

        saved = {k: os.environ.get(k) for k in (JOB_ENV, LANE_ENV)}
        os.environ[JOB_ENV] = job_id
        if lane:
            os.environ[LANE_ENV] = lane
        else:
            os.environ.pop(LANE_ENV, None)
        try:
            run(d)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return job_id
    try:
        p = _spawn(root, d, job_id, lane)
    except BaseException:
        lk.unlink(missing_ok=True)
        raise
    created = None
    try:
        import psutil

        created = psutil.Process(p.pid).create_time()
    except Exception:  # noqa: BLE001
        pass
    (d / "pid").write_text(f"{p.pid} {created or ''}".strip(), encoding="utf-8", newline="\n")
    return job_id


JOB_ENV = "PKF_STUDIO_JOB"


def _spawn(root: Path, d: Path, job_id: str, lane: str | None = None) -> subprocess.Popen:
    """The detached runner. Its stdout/stderr go to crash.log, never into `log` (which only the runner writes, B8)."""
    crash = open(d / "crash.log", "a", encoding="utf-8", newline="\n")  # noqa: SIM115 - handed to the child
    kw: dict = {"stdout": crash, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": str(root)}
    if os.name != "nt":
        kw["start_new_session"] = True
    # never import modules from the repository's own folders; the job's id lets the service tell its own job apart
    kw["env"] = {**os.environ, "PYTHONSAFEPATH": "1", JOB_ENV: job_id}
    kw["env"].pop(LANE_ENV, None)
    if lane:
        kw["env"][LANE_ENV] = lane          # the Studio refuses every change from a lane job's process
    argv = [sys.executable, "-P", "-m", "pkf_studio.runner", str(d)]
    try:
        if os.name == "nt":
            with open(d / "log", "a", encoding="utf-8", newline="\n") as note:
                return _popen_windows(argv, kw, note)
        return subprocess.Popen(argv, **kw)
    finally:
        crash.close()   # the child has its own handle


CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
WINDOWS_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB


def _popen_windows(argv: list[str], kw: dict, logf) -> subprocess.Popen:
    """Windows: a hidden console that every `pkf`/`git` child inherits (no window flashes per step; DETACHED_PROCESS gave
    each console child its own new window), its own process group (Ctrl+C in Git Bash does not reach it), and out of the
    console's job object so closing the Git Bash window does not kill the job. Some console hosts forbid the breakaway:
    then start it once more without it (the job then ends with the console window)."""
    try:
        return subprocess.Popen(argv, **kw, creationflags=WINDOWS_FLAGS)
    except OSError as exc:
        logf.write(f"(could not leave the console's job object: {exc}; closing the console window will stop this job)\n")
        logf.flush()
        return subprocess.Popen(argv, **kw, creationflags=WINDOWS_FLAGS & ~CREATE_BREAKAWAY_FROM_JOB)


UNCANCELLABLE = {"run_local"}  # it may be applying Terraform: killing it can leave state half-written


def cancel(root: Path, job_id: str) -> None:
    d = jobs_dir(root) / job_id
    try:
        if json.loads((d / "cmd.json").read_text())["action"] in UNCANCELLABLE:
            raise JobError("a local run applies Terraform and cannot be cancelled safely; wait for it to finish")
    except (OSError, ValueError, KeyError):
        pass
    pid = _read_pid(d)
    if not pid or not _alive(*pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid[0])], capture_output=True)
    else:
        try:
            import psutil

            proc = psutil.Process(pid[0])
            for c in proc.children(recursive=True):
                c.kill()
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    for _ in range(50):
        if not _alive(*pid):
            break
        time.sleep(0.1)
    else:
        raise JobError("the job's process did not stop; its lock is kept")
    (d / "exit").write_text("1", encoding="utf-8", newline="\n")
    (d / "result.json").write_text(json.dumps({"ok": False, "error": "cancelled"}), encoding="utf-8", newline="\n")
    try:
        cmd = json.loads((d / "cmd.json").read_text())
        # a lane job releases the LANE lock (only while it names this job), never the pipeline's: that would orphan
        # the pipeline job running beside it (0.8.0)
        release(Path(root), cmd["pipeline"], job_id, cmd.get("lane") or None)
    except (OSError, ValueError, KeyError, JobError):
        pass
