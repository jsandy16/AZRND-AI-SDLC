"""git through subprocess. Every write goes through one repository lock; stage work happens in per-pipeline worktrees
(.pkf-studio/worktrees/<pipeline>), so the user's own checkout is never switched."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import STUDIO_DIR


log = logging.getLogger("pkf_studio.git")

LOCK_MAX_AGE_S = 1800        # a git.lock older than 30 minutes is stale whoever holds it
INDEX_LOCK_MAX_AGE_S = 60    # a worktree index.lock older than this, with no other job running, is a leftover


def lock_timeout() -> float:
    """How long a git writer waits for the repository-wide lock: 600 s in a job process (0.6.1, was 120 s), 120 s in the
    app (a button never hangs the tab for 10 minutes). The lock is shared by every pipeline's jobs (their worktrees share
    one .git), so a slow fetch or push of one pipeline behind a proxy no longer fails another pipeline's job.
    PKF_GIT_LOCK_TIMEOUT_S overrides it."""
    default = 600.0 if os.environ.get("PKF_STUDIO_JOB") else 120.0   # jobs wait longer; the UI keeps 0.6.0's 120 s
    try:
        return max(5.0, float(os.environ.get("PKF_GIT_LOCK_TIMEOUT_S") or default))
    except ValueError:
        return default


class GitError(Exception):
    pass


def _create_time(pid: int) -> float | None:
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001 - NoSuchProcess, AccessDenied, psutil missing
        return None


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")


def _git(args: list[str], cwd: Path, check: bool = True, input: str | None = None, env: dict | None = None) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, input=input, timeout=300,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}…: {(p.stderr or p.stdout).strip()[:800]}")
    return p.stdout


def toplevel(path: Path) -> Path | None:
    try:
        return Path(_git(["rev-parse", "--show-toplevel"], path).strip())
    except GitError:
        return None


def parse_github_remote(url: str) -> tuple[str, str, str] | None:
    """(host, owner, repo) from an https or ssh GitHub remote URL."""
    m = re.match(r"^(?:https?://(?:[^@/]+@)?([^/]+)/|git@([^:]+):|ssh://git@([^/]+)/)([^/]+)/(.+?)(?:\.git)?/?$", url.strip())
    if not m:
        return None
    host = m.group(1) or m.group(2) or m.group(3)
    return host, m.group(4), m.group(5)


class Git:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lockfile = self.root / STUDIO_DIR / "locks" / "git.lock"

    def _holder_dead(self) -> bool:
        """The lock's holder process is gone: its pid does not exist, the pid now belongs to another process (its
        create time differs by more than 1 s; the lock is "<pid> <token> <create_time>"; the 0.4.0 two-field form has
        no create time), or the lock is older than 30 minutes."""
        try:
            parts = self._lockfile.read_text(encoding="utf-8").split()
            pid = int(parts[0])
            created = float(parts[2]) if len(parts) > 2 and parts[2] not in ("", "-") else None
            if time.time() - self._lockfile.stat().st_mtime > LOCK_MAX_AGE_S:
                return True
        except (OSError, ValueError, IndexError):
            return False  # being written right now
        try:
            import psutil
        except ImportError:
            return False
        if not psutil.pid_exists(pid):
            return True
        if created is not None:
            now = _create_time(pid)
            if now is not None and abs(now - created) > 1.0:
                return True  # the pid was reused (Windows reuses pids quickly)
        return False

    @contextmanager
    def lock(self, timeout: float | None = None):
        """One git writer at a time across browser tabs and job processes. A lock is stale only when its holder process
        has died; only the holder (by token) removes it."""
        if timeout is None:
            timeout = lock_timeout()
        self._lockfile.parent.mkdir(parents=True, exist_ok=True)
        ct = _create_time(os.getpid())
        token = f"{os.getpid()} {uuid.uuid4().hex} {ct if ct is not None else '-'}"
        start = time.time()
        while True:
            try:
                fd = os.open(self._lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, token.encode())
                os.close(fd)
                break
            except FileExistsError:
                if self._holder_dead():
                    self._lockfile.unlink(missing_ok=True)
                    continue
                if time.time() - start > timeout:
                    raise GitError("another git operation is still running (.pkf-studio/locks/git.lock)") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            try:
                if self._lockfile.read_text(encoding="utf-8") == token:
                    self._lockfile.unlink(missing_ok=True)
            except OSError:
                pass

    def run(self, *args: str, cwd: Path | None = None, check: bool = True, env: dict | None = None) -> str:
        return _git(list(args), cwd or self.root, check, env=env)

    # ------------------------------------------------------------------ reading
    def has_commit(self) -> bool:
        return self.run("rev-parse", "--verify", "HEAD", check=False).strip() != ""

    def remote_url(self, name: str = "origin") -> str | None:
        out = self.run("config", "--get", f"remote.{name}.url", check=False).strip()  # as written (not insteadOf-rewritten)
        return out or None

    def is_clean(self, cwd: Path | None = None) -> bool:
        """No uncommitted changes, counting untracked files that are not ignored (C11: they would be carried along
        through a branch switch and committed to the wrong gate)."""
        return self.run("status", "--porcelain", "--untracked-files=normal", cwd=cwd).strip() == ""

    def base_ref(self, base: str) -> str:
        """origin/<base> when the remote has it (after fetch), else the local branch."""
        if self.run("rev-parse", "--verify", f"refs/remotes/origin/{base}", check=False).strip():
            return f"origin/{base}"
        return base

    def show(self, ref: str, path: str) -> str | None:
        out = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=str(self.root), capture_output=True, timeout=60)
        return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else None

    def sha(self, ref: str) -> str | None:
        out = self.run("rev-parse", "--verify", f"{ref}^{{commit}}", check=False).strip()
        return out or None

    def ls_files(self, ref: str, prefix: str = "") -> list[str]:
        out = self.run("ls-tree", "-r", "--name-only", ref, *([prefix] if prefix else []), check=False)
        return [ln for ln in out.splitlines() if ln]

    def changed_paths(self, a: str, b: str) -> list[str]:
        return [ln for ln in self.run("diff", "--name-only", f"{a}...{b}", check=False).splitlines() if ln]

    def config(self, key: str, cwd: Path | None = None) -> str | None:
        out = self.run("config", "--get", key, cwd=cwd, check=False).strip()
        return out or None

    def count(self, *revs: str, cwd: Path | None = None) -> int | None:
        """`git rev-list --count <revs>`; None when a rev is unknown."""
        out = self.run("rev-list", "--count", *revs, "--", cwd=cwd, check=False).strip()
        return int(out) if out.isdigit() else None

    def archive(self, pipeline: str, label: str, sha: str, cwd: Path | None = None) -> str:
        """Keep `sha` reachable as the local ref refs/pkf-archive/<pipeline>/<label>-<UTC timestamp> (never pushed;
        not a branch, so the Studio's branch listings ignore it). Returns the ref."""
        base = f"refs/pkf-archive/{pipeline}/{label}-{_stamp()}"
        ref, i = base, 1
        while self.sha(ref):
            if self.sha(ref) == self.sha(sha):
                return ref
            i += 1
            ref = f"{base}-{i}"
        self.run("update-ref", ref, sha, cwd=cwd)
        return ref

    def archives(self, pipeline: str) -> list[dict]:
        out = self.run("for-each-ref", "--sort=-creatordate", "--format=%(refname)%00%(objectname)%00%(contents:subject)%00%(creatordate:iso-strict)",
                       f"refs/pkf-archive/{pipeline}/", check=False)
        rows = []
        for ln in out.splitlines():
            parts = ln.split("\0")
            if len(parts) == 4:
                rows.append({"ref": parts[0], "sha": parts[1], "subject": parts[2], "date": parts[3]})
        return rows

    # ------------------------------------------------------------------ leftovers of killed processes (K11)
    def index_lock(self, cwd: Path) -> Path | None:
        out = self.run("rev-parse", "--git-path", "index.lock", cwd=cwd, check=False).strip()
        if not out:
            return None
        p = Path(out)
        return p if p.is_absolute() else Path(cwd) / p

    def _other_job_running(self, cwd: Path) -> bool:
        """A live job other than this process's own (PKF_STUDIO_JOB) holds the lock of the pipeline whose worktree
        `cwd` is."""
        cwd = Path(cwd)
        wts = self.root / STUDIO_DIR / "worktrees"
        try:
            name = cwd.resolve().relative_to(wts.resolve()).parts[0]
        except (ValueError, IndexError, OSError):
            return True  # not a Studio worktree: never touch it
        try:
            from . import jobs

            cur = jobs.current(self.root, name)
        except Exception:  # noqa: BLE001
            return True
        return bool(cur) and cur != os.environ.get("PKF_STUDIO_JOB")

    def clear_stale_index_lock(self, cwd: Path) -> bool:
        """Under the git lock, before commit / checkout / merge / reset: every Studio git write holds the git lock, so a
        worktree index.lock older than 60 s is the leftover of a killed process — removed (and logged) when no other job
        of that pipeline is running. True when one was removed."""
        try:
            lk = self.index_lock(cwd)
            if lk is None or not lk.exists() or time.time() - lk.stat().st_mtime <= INDEX_LOCK_MAX_AGE_S:
                return False
            if self._other_job_running(cwd):
                return False
            lk.unlink()
        except OSError:
            return False
        log.warning("removed a stale %s (left by a killed git process)", lk)
        return True

    # ------------------------------------------------------------------ writing
    def fetch(self) -> None:
        if self.remote_url():
            with self.lock():
                self.run("fetch", "--prune", "origin")

    SETTINGS = (("core.autocrlf", "false"), ("core.longpaths", "true"))

    def ensure_settings(self, cwd: Path | None = None) -> None:
        """Approved code is fingerprinted: never let git rewrite line endings. Long paths for Windows. Read first; write
        only what differs, under the git lock (concurrent writers of .git/config fail; C17)."""
        todo = [(k, v) for k, v in self.SETTINGS
                if self.run("config", "--local", "--get", k, cwd=cwd, check=False).strip() != v]
        if not todo:
            return
        with self.lock():
            for k, v in todo:
                if self.run("config", "--local", "--get", k, cwd=cwd, check=False).strip() != v:
                    self.run("config", k, v, cwd=cwd)

    def worktree_path(self, name: str) -> Path:
        return self.root / STUDIO_DIR / "worktrees" / name

    def _healthy(self, path: Path) -> bool:
        if not (path / ".git").exists():
            return False
        p = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(path), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        if p.returncode != 0:
            return False
        out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"], cwd=str(path),
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout.split("\n")
        try:
            top, common = Path(out[0].strip()), Path(out[1].strip())
            main = Path(self.run("rev-parse", "--path-format=absolute", "--git-common-dir", check=False).strip())
            return top.resolve() == path.resolve() and common.resolve() == main.resolve()
        except (OSError, IndexError):
            return False

    def _repair(self, name: str) -> dict:
        """Caller holds the lock. A worktree folder whose links broke (the repository folder was moved or renamed, e.g.
        by OneDrive) is repaired with `git worktree repair`; one that is still broken, or has no .git, is moved aside to
        worktrees/<name>.broken-<ts> (its files are kept) and the worktree entry is pruned, so it can be added again."""
        path = self.worktree_path(name)
        out = {"path": str(path), "existed": path.exists(), "repaired": False, "moved_aside": None}
        if not path.exists():
            return out
        if self._healthy(path):
            return out
        if (path / ".git").exists():
            self.run("worktree", "repair", str(path), check=False)
            if self._healthy(path):
                out["repaired"] = True
                return out
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
        else:
            aside = path.with_name(f"{name}.broken-{_stamp()}")
            try:
                shutil.move(str(path), str(aside))
            except OSError as exc:
                raise GitError(f"the worktree folder {path} is broken and cannot be moved aside (close programs using it, "
                               f"e.g. VS Code): {exc}") from None
            out["moved_aside"] = str(aside)
            log.warning("moved the broken worktree %s aside to %s", path, aside)
        self.run("worktree", "prune", check=False)
        return out

    def repair_worktree(self, name: str) -> dict:
        """Run the worktree self-repair explicitly: {"path", "existed", "repaired", "moved_aside"}."""
        with self.lock():
            return self._repair(name)

    def worktree(self, name: str, branch: str, start: str) -> Path:
        """The pipeline's worktree on `branch` (created from `start` when the branch does not exist yet). A broken
        worktree folder repairs itself first (C10)."""
        path = self.worktree_path(name)
        with self.lock():
            self._repair(name)
            if path.exists() and (path / ".git").exists():
                cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], path).strip()
                if cur != branch:
                    if not self.is_clean(path):
                        raise GitError(f"the worktree for {name} has uncommitted changes on {cur}; save or discard them first")
                    self.clear_stale_index_lock(path)
                    exists = self.run("rev-parse", "--verify", f"refs/heads/{branch}", check=False).strip()
                    _git(["checkout", branch] if exists else ["checkout", "-b", branch, start], path)
                return path
            self.run("worktree", "prune")
            exists = self.run("rev-parse", "--verify", f"refs/heads/{branch}", check=False).strip()
            if exists:
                self.run("worktree", "add", str(path), branch)
            else:
                self.run("worktree", "add", "-b", branch, str(path), start)
        self.ensure_settings(cwd=path)
        return path

    def staged(self, cwd: Path) -> list[str]:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"], cwd=str(cwd), capture_output=True,
                             timeout=120).stdout
        return [x.decode("utf-8", errors="replace") for x in out.split(b"\0") if x]

    def staged_blob(self, cwd: Path, path: str) -> bytes:
        return subprocess.run(["git", "show", f":{path}"], cwd=str(cwd), capture_output=True, timeout=120).stdout

    def commit(self, cwd: Path, message: str, name: str | None = None, email: str | None = None,
               paths: list[str] | None = None, check=None, allow_empty: bool = False) -> str | None:
        """Commit everything (or `paths`) in the worktree; None when there is nothing to commit. `check(staged files)` runs
        under the lock on exactly what is staged and returns problems; any problem unstages everything and raises.

        allow_empty=True commits even when nothing changed. With `paths=[]` (an explicitly empty list) nothing is staged
        and the commit is always empty (`git commit --only --allow-empty`): uncommitted or staged work stays as it is."""
        env = {}
        if name and email:
            env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
        with self.lock():
            self.clear_stale_index_lock(cwd)
            if allow_empty and paths is not None and not paths:
                _git(["commit", "-q", "--only", "--allow-empty", "-m", message], cwd, env=env)
                return _git(["rev-parse", "HEAD"], cwd).strip()
            _git(["add", "-A", *(paths or [])], cwd)
            if not _git(["status", "--porcelain"], cwd).strip() and not allow_empty:
                return None
            if check is not None:
                problems = check(self.staged(cwd))
                if problems:
                    _git(["reset", "-q"], cwd)
                    raise GitError("not committed — " + "; ".join(problems[:6]))
            _git(["commit", "-q", *(["--allow-empty"] if allow_empty else []), "-m", message], cwd, env=env)
            return _git(["rev-parse", "HEAD"], cwd).strip()

    def is_ancestor(self, a: str, b: str, cwd: Path | None = None) -> bool:
        """`a` is an ancestor of (or equal to) `b`; False when either is unknown."""
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=str(cwd or self.root), capture_output=True,
                              timeout=120).returncode == 0

    def push(self, cwd: Path, branch: str) -> None:
        with self.lock():
            _git(["push", "-q", "-u", "origin", f"{branch}:{branch}"], cwd)

    def head(self, cwd: Path) -> str:
        return _git(["rev-parse", "HEAD"], cwd).strip()

    def remove_worktree(self, name: str) -> None:
        path = self.root / STUDIO_DIR / "worktrees" / name
        with self.lock():
            if path.exists():
                self.run("worktree", "remove", "--force", str(path), check=False)
            self.run("worktree", "prune", check=False)
