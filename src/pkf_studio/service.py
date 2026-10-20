"""The opened repository as the app and the job runner see it: git, GitHub, project config and roster (from the base
branch), who is using it, the stage branches and pull requests, and the approve → merge → ledger cycle.

Branches: pkf/<pipeline>/g1 … g5 (one per gate, opened into the base branch) and pkf/<pipeline>/run-<ts> (the record
PR of a local run). Each pipeline works in its own worktree, .pkf-studio/worktrees/<pipeline>.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import PROTECTED_PATHS, ROLE_LABELS, STRICT_MODES, STUDIO_DIR
from . import ledger as L
from .gates import GATES, ORDER, GateResult, evaluate
from .gitops import Git, GitError, parse_github_remote, toplevel
from .github import GitHub, GitHubError, api_url_for, find_token
from .project import PROJECT_FILE, TEAM_FILE, Person, ProjectConfig, Team, parse_config, parse_team, read_local
from .scan import decode, scan, secrets_only

GATE_CHECK = "pkf-gate"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")  # short: Windows paths stay under 260 characters


class StudioError(Exception):
    pass


class StudioDiverged(StudioError):
    """GitHub's copy of a stage branch and this computer's copy both have commits the other lacks (C3)."""

    def __init__(self, msg: str, branch: str = "", ahead: int = 0, behind: int = 0):
        super().__init__(msg)
        self.branch, self.ahead, self.behind = branch, ahead, behind


class MergedUnapproved(StudioError):
    """A gate's latest PR was merged (on GitHub) without the approval the gate needs (K14, C9)."""

    def __init__(self, msg: str, gate: str = "", pr: int | None = None, merge_sha: str | None = None, summary: str = ""):
        super().__init__(msg)
        self.gate, self.pr, self.merge_sha, self.summary = gate, pr, merge_sha, summary
        self.blocked = False  # True: the gate is blocked (an approval cannot fix it; Reopen)


MERGED_UNAPPROVED_DIR = f"{STUDIO_DIR}/cache/merged-unapproved"

# The Workflow card for a stage branch whose GitHub copy changed (C3) and its buttons: the messages below name them, so
# the UI uses these exact labels.
DIVERGED_CARD = "GitHub's copy changed"
TAKE_LABEL = "Take GitHub's copy"
MERGE_LABEL = "Merge GitHub's copy into mine"
OVERWRITE_LABEL = "Overwrite GitHub's copy (dev)"
DIVERGED_FIX = f"Workflow → {DIVERGED_CARD}: {TAKE_LABEL}, or {MERGE_LABEL}"


def scope_message(pipeline: str, files: list[str]) -> str:
    """Files outside the pipeline's own on a stage branch (R-phase1 #2): what is wrong and how to get rid of them."""
    shown = ", ".join(files[:5]) + (" …" if len(files) > 5 else "")
    return (f"{shown} {'is' if len(files) == 1 else 'are'} outside pipeline {pipeline}'s own files, which are all a stage "
            "branch may change: move them out of the repository folder (the app never commits them; Discard would delete "
            "them), then try again")


def _porcelain(wt: Path) -> list[str]:
    """Every changed path in the worktree (untracked included; both names of a rename)."""
    out = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=str(wt), capture_output=True,
                         timeout=120).stdout.split(b"\0")
    paths, i = [], 0
    while i < len(out):
        e = out[i]
        if len(e) > 3:
            paths.append(e[3:].decode("utf-8", errors="replace"))
            if e[:1] in (b"R", b"C"):  # the next entry is the old name
                i += 1
                if i < len(out) and out[i]:
                    paths.append(out[i].decode("utf-8", errors="replace"))
        i += 1
    return paths


def _wt_reader(wt: Path):
    return lambda q: (Path(wt) / q).read_text(encoding="utf-8") if (Path(wt) / q).is_file() else None


def _stage_of(wt: Path) -> tuple[str, str] | None:
    p = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(wt), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    return gate_of_branch(p.stdout.strip()) if p.returncode == 0 else None


def out_of_scope(wt: Path, pipeline: str, gate: str, base_ref: str | None = None) -> dict[str, list[str]]:
    """Files outside the pipeline's own on its stage branch: {"committed": [...] (vs the base branch), "uncommitted":
    [...] (changed, staged or untracked in the worktree)}."""
    wt = Path(wt)
    if not (wt / ".git").exists():
        return {"committed": [], "uncommitted": []}
    read = _wt_reader(wt)
    if base_ref is None:
        base_ref = "origin/main"
    committed = [x for x in subprocess.run(["git", "diff", "--name-only", f"{base_ref}...HEAD"], cwd=str(wt), capture_output=True,
                                           text=True, encoding="utf-8", errors="replace", timeout=120).stdout.splitlines() if x]
    return {"committed": L.out_of_scope(pipeline, gate, committed, read),
            "uncommitted": L.out_of_scope(pipeline, gate, _porcelain(wt), read)}


def branch_for(pipeline: str, gate: str) -> str:
    return f"pkf/{pipeline}/{gate.lower()}"


def gate_of_branch(branch: str) -> tuple[str, str] | None:
    """('pipeline', 'G3') | ('pipeline', 'RUN') for a Studio branch."""
    m = re.fullmatch(r"pkf/([a-z][a-z0-9_]*)/(g[1-5]|run-[0-9tTzZ-]+)", branch or "")
    if not m:
        return None
    return m.group(1), (m.group(2).upper() if m.group(2).startswith("g") else "RUN")


pipeline_paths = L.pipeline_paths


def unfilled(wt: Path, pipeline: str) -> list[str]:
    """Every '<REQUIRED>' left in the pipeline's component files."""
    out = []
    for kind, name in L.components(lambda p: (Path(wt) / p).read_text(encoding="utf-8") if (Path(wt) / p).exists() else None, pipeline):
        f = Path(wt) / L.FOLDERS[kind] / f"{name}.yaml"
        for i, ln in enumerate(f.read_text(encoding="utf-8").splitlines() if f.exists() else [], 1):
            if "<REQUIRED>" in ln and not ln.lstrip().startswith("#"):
                out.append(f"{f.relative_to(Path(wt)).as_posix()}:{i}: {ln.strip()}")
    return out



def _yaml_file(path: Path) -> dict:
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _plan_problems(wt: Path, pipeline: str) -> list[str]:
    """What the framework's approve-plan would refuse at the start of S4 (open questions, missing tests, stale plans …)."""
    from pkf import design
    from pkf.envelope import PfError
    from pkf.repo import load

    try:
        repo = load(Path(wt))
    except PfError as exc:
        return [f"the component files do not load: {exc}"]
    out = []
    for kind, name in L.components(lambda q: (Path(wt) / q).read_text(encoding="utf-8") if (Path(wt) / q).exists() else None, pipeline):
        try:
            out += [f"{kind} {name}: {m}" for m, _ in design.plan_problems(repo, kind, name)]
        except Exception as exc:  # noqa: BLE001 - a broken component must block the gate, not crash the page
            out.append(f"{kind} {name}: {exc}")
    return out


def dags_paths(pipeline: str) -> list[str]:
    """What `pkf build` writes for one pipeline (the DAG file and its folder)."""
    return [f"dags/pkf_{pipeline}.py", f"dags/pkf/{pipeline}/"]


AIRFLOWIGNORE = "dags/.airflowignore"


def dags_problem(base: str = "main") -> str:
    return (f"dags/ on this branch comes from an earlier build and differs from the one on {base}; it is rebuilt at S5 — "
            f"remove it with Restore dags/ from {base}")


def _issue_msgs(issues, n: int = 2) -> list[str]:
    """The first `n` messages of envelope issues, errors first."""
    rows = [i for i in issues or [] if isinstance(i, dict) and i.get("msg")]
    rows = [i for i in rows if i.get("severity", "error") == "error"] or rows
    return [str(i["msg"])[:300] for i in rows[:n]]


def requirement_sha(requirement: str | None) -> str:
    """sha256 of the requirement as written to the pipeline file, stripped (analysis.json["requirement_sha256"], A10)."""
    import hashlib

    return hashlib.sha256((requirement or "").strip().encode("utf-8")).hexdigest()


def _json_file(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _base_ref_of(wt: Path, base: str) -> str:
    p = subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/remotes/origin/{base}"], cwd=str(wt), capture_output=True,
                       timeout=60)
    return f"origin/{base}" if p.returncode == 0 else base


def readiness(wt: Path, pipeline: str, gate: str, *, base: str = "main", dags_diff: list[str] | None = None) -> list[str]:
    """What still stops a gate's PR from being requested (the reviewers would have to send it back).

    G4: tests.json (version 2 from the S4 draft build; a file without `version` is the 0.4.0 format). `dags_diff` (the
    live `Studio.dags_diff`) overrides the recorded `dags_differ` when given; `base` names the base branch in messages."""
    wt = Path(wt)
    out: list[str] = []
    if gate == "G1":
        requirement = _yaml_file(wt / "pipelines" / f"{pipeline}.yaml").get("requirement")
        if not requirement:
            out.append("analyse the requirement first")
        analysis = _json_file(wt / "requirements" / pipeline / "analysis.json")
        qs = [str(q) for q in analysis.get("open_questions") or [] if str(q).strip()]
        if qs and analysis.get("requirement_sha256") and analysis["requirement_sha256"] == requirement_sha(requirement):
            out.append("the analyst's open questions are not answered: " + " | ".join(qs)[:600] +
                       " — answer them in the requirement text (the questions are cleared when the requirement changes), "
                       "then Split again")
        sp = _yaml_file(wt / "pipelines" / f"{pipeline}.lock.yaml").get("split") or {}
        if not sp:
            out.append("split the requirement first (Split again)")
        else:
            if sp.get("errors"):
                out.append("the split has errors: " + "; ".join(map(str, sp["errors"]))[:400])
            if sp.get("ambiguities"):
                out.append("the split has open questions — answer them in the requirement, then Split again: "
                           + " | ".join(map(str, sp["ambiguities"]))[:600])
    if gate == "G3":
        left = unfilled(wt, pipeline)
        if left:
            out.append(f"{len(left)} environment value(s) still <REQUIRED>")
        for kind, name in L.components(lambda q: (wt / q).read_text(encoding="utf-8") if (wt / q).exists() else None, pipeline):
            lock = _yaml_file(wt / L.FOLDERS[kind] / f"{name}.lock.yaml")
            bad = [x.get("example") for x in lock.get("simulation") or [] if x.get("status") == "fail"]  # needs_code: coded at S4
            if bad:
                out.append(f"the {kind} plan fails its examples {bad}: reword the component's requirement or examples and re-plan it")
        out += _plan_problems(wt, pipeline)
        ex = _yaml_file(wt / "review" / pipeline / "examples.json")
        if ex:
            names = [e.get("example") for e in ex.get("examples") or [] if e.get("status") == "fail"]
            if names:
                out.append(f"the pipeline's end-to-end examples fail with these plans ({names}): fix the plans, or reopen G1 to fix the examples")
    if gate == "G4":
        t = _yaml_file(wt / "review" / pipeline / "tests.json")
        if not t:
            out.append("run the tests and the build first")
        elif not t.get("build_ok"):
            msgs = _issue_msgs(t.get("build_issues"))
            what = ": " + "; ".join(msgs) if msgs else ""
            if t.get("version"):  # v2 (0.5.0): the S4 draft build
                out.append(f"the draft build failed{what} — see the S4 status panel")
            else:                 # v1, written by 0.4.0 (a real build)
                out.append(f"the build failed{what} — Run tests & build again, then see the S4 status panel")
        differ = bool(dags_diff) if dags_diff is not None else bool(t.get("dags_differ"))
        if t and differ:
            out.append(dags_problem(base))
    if gate == "G5":
        man = _yaml_file(wt / "infra" / pipeline / "manifest.json")
        if not man:
            out.append("render and plan the dev environment first")
        elif man.get("needs_apply") and not (wt / "infra" / pipeline / "envs" / "dev" / "plan.sha256").exists():
            out.append("the Terraform plan is missing (install Terraform, then Render & plan)")
        if man:
            dry = _json_file(wt / "review" / pipeline / "dry-run.json")
            if not dry or not dry.get("ok"):
                out.append("the dry run failed or is missing (AWS credentials?): fix it, then Render & plan again")
    if (wt / ".git").exists() and _stage_of(wt) == (pipeline, gate):  # the PR's gate would block on these (R-phase1 #2)
        bad = out_of_scope(wt, pipeline, gate, _base_ref_of(wt, base))
        files = sorted(set(bad["committed"]) | set(bad["uncommitted"]))
        if files:
            out.append(scope_message(pipeline, files))
    return out


@dataclass
class Studio:
    root: Path
    git: Git
    host: str
    owner: str
    repo: str
    gh: GitHub | None
    cfg: ProjectConfig
    team: Team
    configured: bool
    roster_sha: str | None = None
    login: str | None = None          # the token's GitHub login
    login_id: int | None = None
    warnings: list[str] = field(default_factory=list)
    gh_error: str | None = None       # why GitHub could not be asked when the Studio was opened (K9)
    gh_network: bool = False          # ... and it was a network error (not a token problem)
    fetch_error: str | None = None    # the last `git fetch` failure, None after a good fetch
    interactive: bool = False
    _fetched_at: float = 0.0
    _pulls: tuple[float, list] | None = None
    _repo_info: dict | None = None
    _closed_by: dict = field(default_factory=dict)
    left_uncommitted: dict = field(default_factory=dict)  # pipeline → files the last commit left out (outside its scope)

    # ------------------------------------------------------------------ opening
    @classmethod
    def open(cls, path: Path, token: str | None = None, session=None, fetch: bool = True, github: GitHub | None = None,
             interactive: bool = False) -> "Studio":
        """interactive=True: the UI's GitHub client (short timeouts, no retries, fail-fast offline switch; K9)."""
        root = toplevel(Path(path))
        if root is None:
            raise StudioError(f"{path} is not inside a git repository")
        git = Git(root)
        url = git.remote_url()
        parsed = parse_github_remote(url or "")
        if not parsed:
            raise StudioError("the repository has no GitHub remote named origin (git remote add origin https://github.com/OWNER/REPO.git)")
        host, owner, repo = parsed
        s = cls(root=root, git=git, host=host, owner=owner, repo=repo, gh=None, cfg=ProjectConfig(), team=Team(), configured=False,
                interactive=interactive)
        try:
            git.ensure_settings()
        except GitError as exc:
            s.warnings.append(str(exc))
        if fetch:
            s.fetch(force=True)
        s.reload()
        if github is not None:
            s.gh = github
        else:
            tok = token or find_token()
            if tok:
                s.gh = GitHub(s.cfg.github_api if s.configured else api_url_for(host), owner, repo, tok, session=session,
                              interactive=interactive)
        if s.gh is not None:
            try:
                me = s.gh.me()
                s.login, s.login_id = me.get("login"), me.get("id")
            except GitHubError as exc:
                s.warnings.append(str(exc))
                s.gh_error, s.gh_network = str(exc), bool(getattr(exc, "network", False))
            except Exception as exc:  # noqa: BLE001 - opening never fails because GitHub is unreachable
                s.warnings.append(f"{type(exc).__name__}: {exc}")
                s.gh_error, s.gh_network = f"{type(exc).__name__}: {exc}", True
        return s

    def fetch(self, force: bool = False) -> None:
        if not force and time.time() - self._fetched_at < 30:
            return
        try:
            self.git.fetch()
            self.fetch_error = None
        except GitError as exc:
            self.warnings.append(f"git fetch failed: {exc}")
            self.fetch_error = str(exc)[:600]
        self._fetched_at = time.time()

    def reset_offline(self) -> None:
        """Retry / Refresh from GitHub: leave the UI client's offline state and ask GitHub again."""
        if self.gh is not None and hasattr(self.gh, "reset_offline"):
            self.gh.reset_offline()
        self.invalidate()
        if self.gh is not None and self.login is None:
            try:
                me = self.gh.me()
                self.login, self.login_id = me.get("login"), me.get("id")
                self.gh_error, self.gh_network = None, False
            except GitHubError as exc:
                self.gh_error, self.gh_network = str(exc), bool(getattr(exc, "network", False))

    def reload(self) -> None:
        ref = self.base_ref
        cfg_text, team_text = self.git.show(ref, PROJECT_FILE), self.git.show(ref, TEAM_FILE)
        self.configured = bool(cfg_text and team_text)
        self.cfg = parse_config(cfg_text) or ProjectConfig(base_branch=self.cfg.base_branch)
        self.team = parse_team(team_text) or Team()
        self.roster_sha = (self.git.run("rev-parse", f"{ref}:{TEAM_FILE}", check=False).strip() or None) if team_text else None

    # ------------------------------------------------------------------ reading
    @property
    def base(self) -> str:
        return self.cfg.base_branch

    @property
    def base_ref(self) -> str:
        return self.git.base_ref(self.cfg.base_branch if self.cfg else "main")

    @property
    def strict(self) -> bool:
        return self.cfg.mode in STRICT_MODES

    def read_at(self, ref: str, path: str) -> str | None:
        return self.git.show(ref, path)

    def read_base(self, path: str) -> str | None:
        return self.git.show(self.base_ref, path)

    def list_at(self, ref: str, prefix: str) -> list[str]:
        return self.git.ls_files(ref, prefix)

    def me(self) -> Person | None:
        local = read_local(self.root)
        p = self.team.by_email(local.get("email"))
        if p is None and self.login:
            p = self.team.by_login(self.login)
        return p

    def identity_problems(self) -> list[str]:
        me, out = self.me(), []
        if me is None:
            return ["choose who you are (Setup)"]
        git_email = self.git.config("user.email")
        if git_email and git_email.lower() != me.email.lower():
            out.append(f"git user.email is {git_email}, the roster says {me.email} for {me.name}")
        if self.login and me.github and self.login.lower() != me.github.lower():
            out.append(f"the GitHub token belongs to @{self.login}, the roster says @{me.github} for {me.name}")
        return out

    def pipelines(self) -> list[str]:
        names = {Path(f).stem for f in self.list_at(self.base_ref, "pipelines/") if f.endswith(".yaml") and not f.endswith(".lock.yaml")}
        for ln in self.git.run("for-each-ref", "--format=%(refname:short)", "refs/heads/pkf/", "refs/remotes/origin/pkf/", check=False).splitlines():
            g = gate_of_branch(ln.removeprefix("origin/"))
            if g:
                names.add(g[0])
        return sorted(names)

    def worktree_path(self, pipeline: str) -> Path:
        return self.root / STUDIO_DIR / "worktrees" / pipeline

    def worktree_branch(self, pipeline: str) -> str | None:
        wt = self.worktree_path(pipeline)
        if not (wt / ".git").exists():
            return None
        return self.git.run("rev-parse", "--abbrev-ref", "HEAD", cwd=wt, check=False).strip() or None

    # ------------------------------------------------------------------ branches
    def branch_sync(self, pipeline: str, gate: str) -> dict:
        """How this computer's copy of the gate's branch relates to GitHub's (after the last fetch):
        {"state": "no_remote"|"same"|"behind"|"ahead"|"diverged", "ahead": n, "behind": n}."""
        branch = branch_for(pipeline, gate)
        remote = self.git.sha(f"origin/{branch}")
        local = self.git.sha(f"refs/heads/{branch}")
        if not remote:
            return {"state": "no_remote", "ahead": 0, "behind": 0}
        if not local:
            return {"state": "behind", "ahead": 0, "behind": self.git.count(remote, f"^{self.base_ref}") or 0}
        if local == remote:
            return {"state": "same", "ahead": 0, "behind": 0}
        ahead = self.git.count(local, f"^{remote}") or 0
        behind = self.git.count(remote, f"^{local}") or 0
        state = "diverged" if ahead and behind else "ahead" if ahead else "behind" if behind else "same"
        return {"state": state, "ahead": ahead, "behind": behind}

    @staticmethod
    def diverged_message(branch: str, ahead: int, behind: int) -> str:
        return (f"GitHub's copy of {branch} changed and this computer has other commits on it ({ahead} here, {behind} on "
                f"GitHub): {DIVERGED_FIX}")

    def _git_env(self) -> dict:
        me = self.me()
        return ({"GIT_AUTHOR_NAME": me.name, "GIT_AUTHOR_EMAIL": me.email, "GIT_COMMITTER_NAME": me.name,
                 "GIT_COMMITTER_EMAIL": me.email} if me else {})

    def open_stage(self, pipeline: str, gate: str, start: str | None = None) -> Path:
        """The pipeline's worktree on the gate's branch. A new branch starts from `start` (default: the base tip).

        GitHub's copy of the branch wins when this computer's is only behind it (fast-forward; C3); a dirty worktree
        then refuses; diverged copies raise StudioDiverged (Workflow → GitHub's copy changed)."""
        self._refuse_lane("open a stage branch")
        if not NAME_RE.match(pipeline):
            raise StudioError(f"'{pipeline}' is not a pipeline name (lowercase letters, digits and _; starts with a letter)")
        branch = branch_for(pipeline, gate)
        self.fetch()
        remote = self.git.sha(f"origin/{branch}")
        local = self.git.sha(f"refs/heads/{branch}")
        if remote and not local:
            self.git.run("branch", "--track", branch, f"origin/{branch}")
        elif remote and local and remote != local:
            sync = self.branch_sync(pipeline, gate)
            if sync["state"] == "diverged":
                raise StudioDiverged(self.diverged_message(branch, sync["ahead"], sync["behind"]), branch, sync["ahead"], sync["behind"])
            if sync["state"] == "behind":
                self._fast_forward(pipeline, branch)
        return self.git.worktree(pipeline, branch, start or self.base_ref)

    def _fast_forward(self, pipeline: str, branch: str) -> None:
        wt = self.worktree_path(pipeline)
        on_branch = self.worktree_branch(pipeline) == branch
        if on_branch and not self.git.is_clean(wt):
            raise StudioError(f"GitHub's copy of {branch} has newer commits, and the worktree of {pipeline} has uncommitted "
                              "changes; commit or discard them, then try again")
        with self.git.lock():
            if on_branch:
                self.git.clear_stale_index_lock(wt)
                self.git.run("merge", "-q", "--ff-only", f"origin/{branch}", cwd=wt)
            else:
                self.git.run("branch", "-f", branch, f"origin/{branch}")

    def local_ahead(self, pipeline: str, gate: str, pr_head: str) -> int:
        """Commits on this computer's copy of the gate's branch that the PR's head does not have (0 when the local copy
        is only behind; C3). The UI also treats uncommitted changes as local work."""
        wt = self.worktree_path(pipeline)
        if self.worktree_branch(pipeline) != branch_for(pipeline, gate):
            return 0
        head = self.git.head(wt)
        if head == pr_head:
            return 0
        if not self.git.sha(pr_head):  # GitHub's head is newer than the last fetch
            self.fetch(force=True)
        n = self.git.count(head, f"^{pr_head}", cwd=wt)
        return 1 if n is None else n

    def resolve_diverged(self, pipeline: str, gate: str, how: str) -> dict:
        """GitHub's copy changed (C3). how = "take" (archive this computer's tip, then use GitHub's copy), "merge" (merge
        GitHub's copy into this one; a conflict aborts) or "overwrite" (dev mode only: push this copy over GitHub's,
        with a lease on what was fetched)."""
        self._idle(pipeline)
        if how not in ("take", "merge", "overwrite"):
            raise StudioError(f"unknown choice {how!r}: take, merge or overwrite")
        branch = branch_for(pipeline, gate)
        self.fetch(force=True)
        if self.fetch_error:
            raise StudioError(f"GitHub's copy of {branch} could not be fetched: {self.fetch_error} — Retry")
        remote = self.git.sha(f"origin/{branch}")
        if not remote:
            raise StudioError(f"GitHub has no copy of {branch}")
        if not self.git.sha(f"refs/heads/{branch}"):
            self.git.run("branch", "--track", branch, f"origin/{branch}")
        wt = self.git.worktree(pipeline, branch, self.base_ref)
        if not self.git.is_clean(wt):
            raise StudioError(f"the worktree of {pipeline} has uncommitted changes; commit or discard them, then try again")
        local = self.git.head(wt)
        out = {"how": how, "branch": branch, "archive": None}
        if how == "take":
            if local != remote and not self.git.is_ancestor(local, remote, cwd=wt):
                out["archive"] = self.git.archive(pipeline, gate.lower(), local, cwd=wt)
            with self.git.lock():
                self.git.clear_stale_index_lock(wt)
                self.git.run("reset", "-q", "--hard", f"origin/{branch}", cwd=wt)
        elif how == "merge":
            with self.git.lock():
                self.git.clear_stale_index_lock(wt)
                try:
                    self.git.run("merge", "-q", "--no-edit", f"origin/{branch}", cwd=wt, env=self._git_env())
                except GitError:
                    files = [x for x in self.git.run("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False).splitlines() if x]
                    self.git.run("merge", "--abort", cwd=wt, check=False)
                    raise StudioError(f"the two copies conflict in {', '.join(files[:8]) or 'some files'}: take GitHub's copy, "
                                      "or resolve it on GitHub") from None
        else:
            if self.strict:
                raise StudioError("overwriting GitHub's copy is for dev mode only: take GitHub's copy, or merge it")
            with self.git.lock():
                try:
                    self.git.run("push", "-q", f"--force-with-lease={branch}:{remote}", "origin", f"{branch}:{branch}", cwd=wt)
                except GitError as exc:
                    raise StudioError(f"GitHub's copy of {branch} changed again; fetch and choose again ({str(exc)[:300]})") from None
        self.invalidate()
        self.fetch(force=True)
        out["head"] = self.git.head(wt)
        return out

    def _later_branches(self, pipeline: str, gate: str) -> list[tuple[str, str]]:
        """[(gate, branch)] of the stages after `gate`."""
        return [(g, branch_for(pipeline, g)) for g in ORDER[ORDER.index(gate) + 1:]]

    def _open_prs_safe(self, pipeline: str) -> dict[str, dict]:
        try:
            return {g: pr for g, pr in self.prs(pipeline, 0).items() if pr.get("state") == "open"}
        except Exception:  # noqa: BLE001 - the preview works without GitHub
            return {}

    def reopen_preview(self, pipeline: str, gate: str) -> list[dict]:
        """What Reopen <gate> sets aside (K8): every later stage branch, and the gate's own branch when its tip is not on
        the base branch. [{"branch", "tip", "unpushed": n, "open_pr": number | None}]; unpushed = commits that are
        neither on GitHub's copy of the branch nor on the base branch (they survive only as the archive ref)."""
        base = self.base_ref
        open_prs = self._open_prs_safe(pipeline)
        out = []
        for g, branch in [(gate, branch_for(pipeline, gate))] + self._later_branches(pipeline, gate):
            local, remote = self.git.sha(f"refs/heads/{branch}"), self.git.sha(f"origin/{branch}")
            tip = local or remote
            if not tip:
                continue
            if g == gate and self.git.is_ancestor(tip, base):
                continue
            unpushed = self.git.count(tip, f"^{base}", *([f"^{remote}"] if remote else [])) or 0
            pr = open_prs.get(g)
            out.append({"branch": branch, "tip": tip, "unpushed": unpushed, "open_pr": pr["number"] if pr else None})
        return out

    def archives(self, pipeline: str) -> list[dict]:
        """Set-aside work (Reopen, Take GitHub's copy, unmerged commits): [{"ref", "sha", "subject", "date"}], newest
        first. Restore one with `git branch <name> <ref>`."""
        return self.git.archives(pipeline)

    def archive(self, pipeline: str, gate: str, sha: str | None = None) -> str | None:
        """Keep the tip of the gate's local branch (or `sha`) as refs/pkf-archive/<p>/<gate>-<ts>; None without one."""
        sha = sha or self.git.sha(f"refs/heads/{branch_for(pipeline, gate)}")
        return self.git.archive(pipeline, gate.lower(), sha) if sha else None

    def repair_worktree(self, pipeline: str) -> dict:
        """Repair the pipeline's worktree folder (C10): `git worktree repair`, else move it aside and add it again on its
        branch. Returns {"path", "existed", "repaired", "moved_aside", "branch"}."""
        self._idle(pipeline)
        res = self.git.repair_worktree(pipeline)
        if res.get("moved_aside") or not (self.worktree_path(pipeline) / ".git").exists():
            branch = None
            for g in reversed(ORDER):  # the latest stage branch this computer has
                b = branch_for(pipeline, g)
                if self.git.sha(f"refs/heads/{b}") or self.git.sha(f"origin/{b}"):
                    branch = b
                    break
            if branch:
                if not self.git.sha(f"refs/heads/{branch}"):
                    self.git.run("branch", "--track", branch, f"origin/{branch}")
                self.git.worktree(pipeline, branch, self.base_ref)
        res["branch"] = self.worktree_branch(pipeline)
        return res

    def reopen(self, pipeline: str, gate: str) -> Path:
        """Approve an earlier gate again: restart the gate's branch from the base tip, delete the later stage branches
        and close their PRs. Order (C7): no job running, a clean worktree (untracked files count), a good fetch; then
        every later branch tip (and the gate's own tip when it is not on the base) is kept as a local archive ref (K8);
        then the branches move; the PRs are closed last."""
        self._idle(pipeline)
        branch = branch_for(pipeline, gate)
        wt = self.git.worktree(pipeline, self.worktree_branch(pipeline) or branch, self.base_ref)
        if not self.git.is_clean(wt):
            raise StudioError("the worktree has uncommitted changes; commit or discard them first (Workflow)")
        self.invalidate()
        self.fetch(force=True)
        if self.fetch_error:
            raise StudioError(f"the base branch could not be fetched: {self.fetch_error} — Retry (Reopen needs the current "
                              "base branch)")
        try:
            open_prs = {g: pr for g, pr in self.prs(pipeline, 0).items() if pr.get("state") == "open"}
        except (GitHubError, StudioError) as exc:
            raise StudioError(f"GitHub cannot be asked which pull requests are open ({exc}): Retry, then Reopen") from None
        base = self.base_ref
        archived = []
        for g, b in [(gate, branch)] + self._later_branches(pipeline, gate):
            for tip in dict.fromkeys(x for x in (self.git.sha(f"refs/heads/{b}"), self.git.sha(f"origin/{b}")) if x):
                if g == gate and self.git.is_ancestor(tip, base):
                    continue
                archived.append(self.git.archive(pipeline, g.lower(), tip))
        with self.git.lock():
            self.git.clear_stale_index_lock(wt)
            self.git.run("checkout", "-q", "-B", branch, self.base_ref, cwd=wt)
            for _, later in self._later_branches(pipeline, gate):  # later stages restart from the re-approved base
                self.git.run("branch", "-D", later, check=False, cwd=wt)
                if self.git.sha(f"origin/{later}"):
                    self.git.run("push", "-q", "origin", "--delete", later, check=False, cwd=wt)
        marker = wt / "review" / pipeline / f"reopened-{gate.lower()}.md"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"# {gate} reopened\n\n{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC by "
                          f"{(self.me().name if self.me() else '?')}: what {gate} approved changed, or was found wrong; it is approved again.\n",
                          encoding="utf-8", newline="\n")
        self.commit(pipeline, f"{gate} reopened")
        for g in ORDER[ORDER.index(gate) + 1:] + ["RUN"]:  # last: closing PRs is the only step GitHub can refuse
            pr = open_prs.get(g)
            if not pr:
                continue
            try:
                self.gh.update_pull(pr["number"], state="closed")
            except GitHubError as exc:
                try:  # once more, unless it is closed already
                    if (self.gh.pull(pr["number"]) or {}).get("state") != "closed":
                        self.gh.update_pull(pr["number"], state="closed")
                except GitHubError:
                    self.warnings.append(f"{gate} was reopened, but PR #{pr['number']} could not be closed ({exc}): close it on GitHub")
                    continue
            try:
                self.gh.comment(pr["number"], f"Closed by pkf Studio: {gate} was reopened.")
            except GitHubError:
                pass
        self.invalidate()
        if archived:
            self.warnings.append(f"Reopen {gate}: earlier work kept as {', '.join(archived)}")
        return wt

    def discard(self, pipeline: str) -> None:
        """Throw away uncommitted changes in the pipeline's worktree."""
        self._idle(pipeline)
        wt = self.worktree_path(pipeline)
        with self.git.lock():
            self.git.clear_stale_index_lock(wt)
            self.git.run("reset", "-q", "--hard", "HEAD", cwd=wt)
            self.git.run("clean", "-q", "-fd", cwd=wt)

    def open_run_branch(self, pipeline: str, branch: str | None = None) -> tuple[Path, str]:
        """The worktree on a new run branch pkf/<p>/run-<ts> from the base tip, or on the existing run `branch` (a
        preview's branch, K6)."""
        self._refuse_lane("open a run branch")
        self.fetch(force=True)
        if branch is not None:
            g = gate_of_branch(branch)
            if not g or g != (pipeline, "RUN"):
                raise StudioError(f"{branch} is not a run branch of {pipeline}")
            if not self.git.sha(f"refs/heads/{branch}"):
                if not self.git.sha(f"origin/{branch}"):
                    raise StudioError(f"the run branch {branch} does not exist (preview this run again)")
                self.git.run("branch", "--track", branch, f"origin/{branch}")
            return self.git.worktree(pipeline, branch, self.base_ref), branch
        stamp = f"pkf/{pipeline}/run-{dt.datetime.now(dt.timezone.utc):%Y%m%dt%H%M%S}"
        branch, i = stamp, 1
        while self.git.sha(f"refs/heads/{branch}") or self.git.sha(f"origin/{branch}"):  # two runs in one second
            i += 1
            branch = f"{stamp}-{i}"
        return self.git.worktree(pipeline, branch, self.base_ref), branch

    def unpublished_runs(self, pipeline: str) -> list[str]:
        """Local run branches pkf/<p>/run-* that are not on GitHub and hold a run record to publish: a report.json, or a
        started.json without one (a crashed move, K6/R7). Oldest first."""
        out = []
        for ln in self.git.run("for-each-ref", "--sort=refname", "--format=%(refname:short)", f"refs/heads/pkf/{pipeline}/",
                               check=False).splitlines():
            g = gate_of_branch(ln.strip())
            if not g or g != (pipeline, "RUN") or self.git.sha(f"origin/{ln.strip()}"):
                continue
            run = ln.strip().rsplit("/", 1)[-1]
            files = self.git.ls_files(ln.strip(), f"review/{pipeline}/runs/{run}/")
            if any(f.endswith(("/report.json", "/started.json")) for f in files):
                out.append(ln.strip())
        return out

    def unmerged_local_commits(self, pipeline: str, gate: str) -> int:
        """Commits on this computer's copy of the gate's branch that are neither in its merged PR nor on the base branch
        (C20: e.g. a re-plan committed after the PR was merged on GitHub). 0 when the PR is not merged."""
        local = self.git.sha(f"refs/heads/{branch_for(pipeline, gate)}")
        if not local:
            return 0
        try:
            pr = self.prs(pipeline, 0).get(gate)
        except Exception:  # noqa: BLE001
            return 0
        if not pr or not pr.get("merged_at"):
            return 0
        head = pr["head"]["sha"]
        if not self.git.sha(head):
            self.git.run("fetch", "-q", "origin", f"pull/{pr['number']}/head", check=False)
        if not self.git.sha(head):
            return 0
        return self.git.count(local, f"^{head}", f"^{self.base_ref}") or 0

    def changed_files(self, wt: Path) -> list[str]:
        out = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=str(wt), capture_output=True,
                             timeout=120).stdout.split(b"\0")
        return [x[3:].decode("utf-8", errors="replace") for x in out if len(x) > 3]

    def staged_check(self, wt: Path, protected: bool = True):
        """The check run under the git lock on exactly what is staged: protected paths and secrets block a commit."""
        allowed = {p.email for p in self.team.people}

        def check(files: list[str]) -> list[str]:
            problems = []
            if protected:
                bad = [f for f in files if f.startswith(PROTECTED_PATHS)]
                if bad:
                    problems.append(f"stage branches may not change {', '.join(bad[:3])} (use a team PR in Setup)")
                stage = _stage_of(wt)
                if stage:
                    outside = [f for f in L.out_of_scope(stage[0], stage[1], files, _wt_reader(wt)) if f not in bad]
                    if outside:
                        problems.append(scope_message(stage[0], outside))
            for f in files:
                text = decode(self.git.staged_blob(wt, f))
                for x in secrets_only(scan(text, allowed)):
                    problems.append(f"{f}:{x.line}: looks like a {x.what} ({x.excerpt}) — remove it")
            return problems
        return check

    def commit(self, pipeline: str, message: str) -> str | None:
        """Commit the worktree's changes. On a stage branch, files outside the pipeline's own are left uncommitted (they
        would block the gate; R-phase1 #2) and listed in `left_uncommitted[pipeline]` / `left_uncommitted_message()`;
        anything outside that is already staged refuses the commit (staged_check)."""
        self._refuse_lane("commit")
        wt = self.worktree_path(pipeline)
        me = self.me()
        paths = None
        self.left_uncommitted.pop(pipeline, None)
        stage = _stage_of(wt) if (wt / ".git").exists() else None
        if stage and stage[0] == pipeline:
            changed = _porcelain(wt)
            # protected files (roster, workflows …) are not left out quietly: staged_check refuses them by name
            outside = {f for f in L.out_of_scope(pipeline, stage[1], changed, _wt_reader(wt)) if not f.startswith(PROTECTED_PATHS)}
            if outside:
                self.left_uncommitted[pipeline] = sorted(outside)
                staged = set(self.git.run("diff", "--cached", "--name-only", cwd=wt, check=False).splitlines())
                if not (staged & outside):  # leave them out; already staged ones are refused by staged_check below
                    paths = sorted(set(changed) - outside)
                    if not paths:
                        return None
        try:
            return self.git.commit(wt, message, me.name if me else None, me.email if me else None, paths=paths,
                                   check=self.staged_check(wt))
        except GitError as exc:
            raise StudioError(str(exc)) from None

    def left_uncommitted_message(self, pipeline: str) -> str | None:
        """For the job's problems after a commit that left files out."""
        files = self.left_uncommitted.get(pipeline)
        if not files:
            return None
        return "left uncommitted: " + scope_message(pipeline, files)

    def out_of_scope(self, pipeline: str) -> dict[str, list[str]]:
        """Files outside the pipeline's own on its stage branch: {"committed": [...], "uncommitted": [...]}."""
        wt = self.worktree_path(pipeline)
        stage = _stage_of(wt) if (wt / ".git").exists() else None
        if not stage or stage[0] != pipeline:
            return {"committed": [], "uncommitted": []}
        return out_of_scope(wt, pipeline, stage[1], self.base_ref)

    def remove_out_of_scope(self, pipeline: str) -> list[str]:
        """Take the committed files outside the pipeline's own off the stage branch in one commit (a file the base branch
        has goes back to the base version; any other is deleted, its content stays in the branch history). Uncommitted
        ones are left to Discard. Returns the paths changed."""
        self._idle(pipeline)
        wt = self.worktree_path(pipeline)
        files = self.out_of_scope(pipeline)["committed"]
        if not files:
            return []
        on_base = set(self.git.ls_files(self.base_ref))
        with self.git.lock():
            if [x for x in self.git.run("diff", "--cached", "--name-only", cwd=wt).splitlines() if x]:
                raise StudioError("the worktree has staged changes: commit or discard them first")
            self.git.clear_stale_index_lock(wt)
            for f in files:
                if f in on_base:
                    self.git.run("checkout", self.base_ref, "--", f, cwd=wt)
                else:
                    self.git.run("rm", "-q", "-f", "--ignore-unmatch", "--", f, cwd=wt)
            if self.git.run("diff", "--cached", "--name-only", cwd=wt).strip():
                self.git.run("commit", "-q", "-m", f"{pipeline}: files outside this pipeline removed from the branch",
                             cwd=wt, env=self._git_env())
        return files

    def push(self, pipeline: str) -> str:
        self._refuse_lane("push")
        wt = self.worktree_path(pipeline)
        branch = self.worktree_branch(pipeline)
        self.git.push(wt, branch)
        return branch

    # ------------------------------------------------------------------ pushing stage work (K2)
    def _open_pr_head(self, pipeline: str, gate: str) -> tuple[bool, str | None]:
        """(known, head sha of the gate's OPEN pull request or None). known=False when GitHub cannot be asked."""
        if self.gh is None:
            return False, None
        try:
            self.invalidate()
            pr = self.prs(pipeline, 0).get(gate)
        except Exception:  # noqa: BLE001 - GitHub unreachable or refusing: decide from git alone
            return False, None
        return True, (pr["head"]["sha"] if pr and pr.get("state") == "open" else None)

    def _reset_approvals_before_push(self, pipeline: str, wt: Path, branch: str, fresh: str | None = None) -> str | None:
        """C4 rule 1: when this push moves the head of an OPEN pull request, first make an empty commit, so that its
        committer date is the push time: a plain `/approve` written before the push resolves to an older commit (which
        `equivalent()` does not carry forward), and the approvals reset as documented. `fresh` is a commit this call just
        made (its date is already the push time). Returns the empty commit's sha, or None when none was needed.

        When GitHub cannot be asked, GitHub's copy of the branch stands in for the PR head (a PR may be open on it)."""
        g = gate_of_branch(branch)
        if not g or g[0] != pipeline:
            return None
        gate = g[1]
        head = self.git.head(wt)
        known, pr_head = self._open_pr_head(pipeline, gate)
        if not known:
            pr_head = self.git.sha(f"origin/{branch}")
        if not pr_head or pr_head == head or head == fresh:
            return None
        if not self.git.is_ancestor(pr_head, head, cwd=wt):
            return None  # not a fast-forward: the push is refused anyway (GitHub's copy changed); no local commit added
        title = GATES[gate].title if gate in GATES else "run record"
        me = self.me()
        return self.git.commit(wt, f"{gate} {title}: work pushed (approvals reset)", me.name if me else None,
                               me.email if me else None, paths=[], allow_empty=True)

    @staticmethod
    def _push_message(branch: str, exc: Exception) -> str:
        text = str(exc)
        if any(x in text for x in ("non-fast-forward", "fetch first", "[rejected]", "stale info")):
            return (f"GitHub's copy of {branch} changed (someone pushed or used 'Update branch'): {DIVERGED_FIX}")
        return (f"could not push {branch}: {text[:300]} — your work is committed on this computer; it is pushed by the next "
                "job or by Request approval")

    def push_quiet(self, pipeline: str) -> str | None:
        """Push the worktree's current branch (plain push, never forced) at the end of a stage job. None on success,
        otherwise a message for the job's problems; never raises."""
        try:
            self._refuse_lane("push")          # 0.8.0 (caught below: a message, like every push problem)
            wt = self.worktree_path(pipeline)
            branch = self.worktree_branch(pipeline)
            if not branch or branch == "HEAD":
                return f"could not push: the worktree of {pipeline} is not on a branch"
            try:
                self._reset_approvals_before_push(pipeline, wt, branch)
            except Exception as exc:  # noqa: BLE001
                return f"could not push {branch}: {str(exc)[:300]} — your work is committed on this computer"
            try:
                self.git.push(wt, branch)
            except GitError as exc:
                return self._push_message(branch, exc)
            self.invalidate()
            return None
        except Exception as exc:  # noqa: BLE001 - a push problem never fails the job
            return f"could not push the work of {pipeline}: {type(exc).__name__}: {str(exc)[:300]}"

    # ------------------------------------------------------------------ jobs, dags/ (S4)
    def _refuse_lane(self, what: str = "change the repository") -> None:
        """0.8.0: a job of the assistant lane (PKF_STUDIO_LANE in its environment) only reads — it runs beside the
        pipeline's own job and holds no pipeline lock — so every Studio change from its process is refused: commit,
        push, stage branches, request / approve / merge, reopen, discard, repair …"""
        from .jobs import LANE_ENV

        lane = os.environ.get(LANE_ENV)
        if lane:
            raise StudioError(f"the {lane} lane only reads: a job there may not {what} (it runs beside the pipeline's own job)")

    def _idle(self, pipeline: str) -> None:
        """Refuse while a job runs for the pipeline (the job's own process, PKF_STUDIO_JOB, may go on). A lane job's
        process is refused always (_refuse_lane): it is never the pipeline's job, even when none runs."""
        from . import jobs

        self._refuse_lane()
        try:
            cur = jobs.current(self.root, pipeline)
        except Exception:  # noqa: BLE001
            cur = None
        if cur and cur != os.environ.get("PKF_STUDIO_JOB") and not self._is_this_job(cur):
            raise StudioError(f"a job is running for {pipeline}; wait for it to finish")

    def _is_this_job(self, job_id: str) -> bool:
        """Fallback when PKF_STUDIO_JOB is not set: the job's runner is this very process (its pid file), or an inline
        job (PKF_STUDIO_INLINE_JOBS=1, no pid file) — inline jobs run synchronously in the process that started them."""
        if os.environ.get("PKF_STUDIO_JOB"):
            return False
        d = self.root / STUDIO_DIR / "jobs" / job_id
        try:
            pid = int((d / "pid").read_text(encoding="utf-8").split()[0])
            return pid == os.getpid()
        except (OSError, ValueError, IndexError):
            return os.environ.get("PKF_STUDIO_INLINE_JOBS") == "1" and (d / "cmd.json").exists()

    def dags_diff(self, pipeline: str) -> list[str]:
        """Paths of `dags/pkf_<p>.py` and `dags/pkf/<p>/` in the worktree (committed or not, untracked included) that
        differ from the base branch: typically a real build of 0.4.0 committed on the stage branch (R10)."""
        wt = self.worktree_path(pipeline)
        if not (wt / ".git").exists():
            return []
        paths = dags_paths(pipeline)
        if not self.git.ls_files(self.base_ref, AIRFLOWIGNORE):  # shared by every pipeline: only when main has none
            paths.append(AIRFLOWIGNORE)                           # (0.4.0's S4 build wrote it; R-phase1 #3)
        tracked = self.git.run("diff", "--name-only", self.base_ref, "--", *paths, cwd=wt, check=False).splitlines()
        untracked = self.git.run("ls-files", "--others", "--exclude-standard", "--", *paths, cwd=wt, check=False).splitlines()
        return sorted({x for x in tracked + untracked if x})

    def restore_dags_from_base(self, pipeline: str) -> list[str]:
        """Make dags/ of this pipeline on the stage branch the same as on the base branch, in one commit
        "S4 <p>: dags/ restored from <base>". The DAG is rebuilt from approved code at S5. Returns the paths changed."""
        self._idle(pipeline)
        wt = self.worktree_path(pipeline)
        branch = self.worktree_branch(pipeline)
        g = gate_of_branch(branch or "")
        if not g or g[0] != pipeline or g[1] == "RUN":
            raise StudioError(f"the worktree of {pipeline} is not on a stage branch (it is on {branch})")
        changed = self.dags_diff(pipeline)
        tj = wt / "review" / pipeline / "tests.json"
        if not changed:
            return []
        base_ref = self.base_ref
        me = self.me()
        env = ({"GIT_AUTHOR_NAME": me.name, "GIT_AUTHOR_EMAIL": me.email, "GIT_COMMITTER_NAME": me.name,
                "GIT_COMMITTER_EMAIL": me.email} if me else {})
        on_base = set(self.git.ls_files(base_ref, f"dags/pkf/{pipeline}/")) | set(self.git.ls_files(base_ref, f"dags/pkf_{pipeline}.py"))
        with self.git.lock():
            staged = [x for x in self.git.run("diff", "--cached", "--name-only", cwd=wt).splitlines() if x]
            if staged:
                raise StudioError("the worktree has staged changes (" + ", ".join(staged[:3]) + "): commit or discard them first")
            for path in changed:
                if path in on_base:
                    self.git.run("checkout", base_ref, "--", path, cwd=wt)
                else:
                    self.git.run("rm", "-q", "-f", "--cached", "--ignore-unmatch", "--", path, cwd=wt)
                    f = wt / path
                    if f.is_file() or f.is_symlink():
                        f.unlink()
            for d in sorted({(wt / p).parent for p in changed}, key=lambda x: len(x.parts), reverse=True):
                while d != wt and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
            if tj.exists():  # the S4 summary said dags/ differed: it no longer does
                try:
                    t = json.loads(tj.read_text(encoding="utf-8"))
                except ValueError:
                    t = None
                if isinstance(t, dict) and t.get("dags_differ"):
                    t["dags_differ"] = False
                    t["problems"] = [x for x in t.get("problems") or [] if not str(x).startswith("dags/ on this branch")]
                    tj.write_text(json.dumps(t, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
                    self.git.run("add", "--", tj.relative_to(wt).as_posix(), cwd=wt)
            problems = self.staged_check(wt)(self.git.staged(wt))
            if problems:
                self.git.run("reset", "-q", cwd=wt)
                raise StudioError("not committed — " + "; ".join(problems[:6]))
            if self.git.run("diff", "--cached", "--name-only", cwd=wt).strip():
                self.git.run("commit", "-q", "-m", f"S4 {pipeline}: dags/ restored from {self.base}", cwd=wt, env=env)
        return changed

    # ------------------------------------------------------------------ pull requests
    def _all_pulls(self, max_age: float = 20.0) -> list:
        if self.gh is None:
            raise StudioError("no GitHub token (set GITHUB_TOKEN or run `gh auth login`)")
        if self._pulls and time.time() - self._pulls[0] < max_age:
            return self._pulls[1]
        rows = self.gh.pulls("all")
        self._pulls = (time.time(), rows)
        return rows

    def prs(self, pipeline: str, max_age: float = 20.0) -> dict[str, dict]:
        """The latest PR per gate (G1…G5) and RUN for one pipeline, from this repository's own branches."""
        out: dict[str, dict] = {}
        for pr in sorted(self._all_pulls(max_age), key=lambda p: p["number"]):
            head = pr.get("head") or {}
            if ((head.get("repo") or {}).get("full_name") or "").lower() != f"{self.owner}/{self.repo}".lower():
                continue  # only same-repo branches
            g = gate_of_branch(head.get("ref", ""))
            if g and g[0] == pipeline:
                out[g[1]] = pr
        return out

    def invalidate(self) -> None:
        self._pulls = None

    def pr_body(self, pipeline: str, gate: str, head: str, files: list[str]) -> str:
        g = GATES[gate]
        roles = ", ".join(ROLE_LABELS[r] for r in g.roles)
        who = (f"{roles} — different people, none of whom authored or committed to this PR" if self.strict and len(g.roles) > 1
               else f"{roles}" + (" (not an author or committer of this PR)" if self.strict else ""))
        how = ["- Submit a GitHub review → **Approve** on the latest commit. Holding several roles? Write `Approve as: architect` "
               "(or `po`, `ba`, `de`) in the review."]
        if not self.strict:
            how.append("- Dev mode (GitHub does not let you approve your own PR): comment `/approve` — it approves the latest commit at that "
                       "moment. `/reject <reason>` requests changes. You can then merge here on GitHub or in the app.")
        listing = "\n".join(f"- `{f}`" for f in files[:60]) + ("\n- …" if len(files) > 60 else "")
        marker = json.dumps({"pipeline": pipeline, "gate": gate})
        return (f"## {gate} {g.title} · pipeline `{pipeline}`\n\nMode **{self.cfg.mode}** · head `{head[:7]}` · opened by pkf Studio\n\n"
                f"### What to review\n{g.reviews}\n\n### Who approves\n{who}\n\n### How to approve\n" + "\n".join(how) +
                f"\n\nNew commits reset the approvals. The app merges this PR (merge commit) once the gate passes"
                f"{' and the `pkf-gate` check is green' if self.strict else ''}.\n\n### Files changed\n{listing}\n\n"
                f"<!-- pkf-studio: {marker} -->\n")

    def request_approval(self, pipeline: str, gate: str) -> dict:
        self._idle(pipeline)
        wt = self.worktree_path(pipeline)
        branch = self.worktree_branch(pipeline)
        if branch != branch_for(pipeline, gate):
            raise StudioError(f"the worktree is on {branch}, not {branch_for(pipeline, gate)}")
        problems = readiness(wt, pipeline, gate, base=self.base, dags_diff=self.dags_diff(pipeline) if gate == "G4" else None)
        if problems:
            raise StudioError("not ready for review: " + "; ".join(problems))
        fresh = self.commit(pipeline, f"{gate} {GATES[gate].title}: ready for review")
        self._reset_approvals_before_push(pipeline, wt, branch, fresh=fresh)  # K2 / C4 rule 1
        try:
            self.git.push(wt, branch)
        except GitError as exc:
            raise StudioError(self._push_message(branch, exc)) from None
        head = self.git.head(wt)
        files = [ln for ln in self.git.run("diff", "--name-only", f"{self.base_ref}...HEAD", cwd=wt).splitlines() if ln]
        if not files:
            raise StudioError("nothing to review: this branch has no changes against the base branch")
        body = self.pr_body(pipeline, gate, head, files)
        title = f"[pkf] {pipeline}: {gate} {GATES[gate].title}"
        self.invalidate()
        pr = self.prs(pipeline, 0).get(gate)
        if pr and pr["state"] == "open":
            pr = self.gh.update_pull(pr["number"], body=body, title=title)
        else:
            pr = self.gh.create_pull(title, branch, self.base, body)
        self.invalidate()
        return pr

    def equivalent(self, pipeline: str, head: str):
        """sha → True when only merge commits were added since `sha` and this pipeline's files are unchanged."""
        def fn(sha: str) -> bool:
            if not self.git.sha(sha):
                return False
            base = self.base_ref
            for c in self.git.run("rev-list", f"{sha}..{head}", f"^{base}", check=False).split():  # base's own commits are reviewed there
                parents = self.git.run("rev-list", "--parents", "-n", "1", c, check=False).split()[1:]
                if len(parents) != 2:
                    return False  # only merges of the base branch may follow an approval
                p1, p2 = parents
                if subprocess.run(["git", "merge-base", "--is-ancestor", p2, base], cwd=str(self.root)).returncode != 0:
                    return False
                tree = self.git.run("merge-tree", "--write-tree", p1, p2, check=False).split()
                if not tree or tree[0] != self.git.run("rev-parse", f"{c}^{{tree}}", check=False).strip():
                    return False  # a "merge" whose content is not the plain merge of the two parents
            comps = L.components(lambda p: self.read_at(head, p), pipeline)
            diff = self.git.run("diff", "--name-only", sha, head, "--", *pipeline_paths(pipeline, comps), check=False).strip()
            return diff == ""
        return fn

    def gate_status(self, pipeline: str, gate: str, pr: dict | None = None) -> tuple[GateResult, dict]:
        pr = pr or self.prs(pipeline).get(gate)
        if not pr:
            raise StudioError(f"{gate} has no pull request yet")
        view, full = self.gh.pr_view(pr["number"])
        evidence = self.gh.evidence(pr["number"])
        self.git.run("fetch", "-q", "origin", f"pull/{pr['number']}/head", check=False)
        res = evaluate(gate, self.cfg.mode, self.team, view, evidence, self.equivalent(pipeline, view.head_sha))
        scope = L.scope_problems(pipeline, gate, view.files, lambda q: self.read_at(view.head_sha, q))
        if scope:
            res.blocked += scope
            res.passed = False
        return res, full

    def gate_check(self, sha: str) -> str:
        """The pkf-gate check on a commit: success | failure | pending | missing | "unknown: <reason>" (the checks
        cannot be read: a token without Checks: read, or GitHub unreachable)."""
        try:
            runs = [r for r in self.gh.check_runs(sha) if r.get("name") == GATE_CHECK]
        except GitHubError as exc:
            return f"unknown: {exc}"
        if not runs:
            return "missing"
        r = sorted(runs, key=lambda r: r.get("started_at") or "")[-1]
        if r.get("status") != "completed":
            return "pending"
        return "success" if r.get("conclusion") == "success" else "failure"

    def merge_methods(self) -> list[str]:
        """The merge methods this repository allows, in the app's order of preference (merge, squash, rebase)."""
        if self._repo_info is None:
            try:
                self._repo_info = self.gh.repo_info() or {}
            except GitHubError:
                return ["merge"]
        info = self._repo_info
        allowed = [m for m, k in (("merge", "allow_merge_commit"), ("squash", "allow_squash_merge"),
                                  ("rebase", "allow_rebase_merge")) if info.get(k, True) is not False]
        return allowed or ["merge"]

    def merge(self, pipeline: str, gate: str, pr_number: int | None = None) -> dict:
        self._idle(pipeline)
        pr = self.gh.pull(pr_number) if pr_number else self.prs(pipeline, 0).get(gate)
        if not pr or pr["state"] != "open":
            raise StudioError(f"{gate} has no open pull request")
        if gate != "RUN":
            res, full = self.gate_status(pipeline, gate, pr)
            if not res.passed:
                raise StudioError(f"{gate} has not passed: {res.summary()}")
        else:
            view, full = self.gh.pr_view(pr["number"])
            scope = L.scope_problems(pipeline, "RUN", view.files, lambda q: None)
            if scope or view.touched_protected:
                raise StudioError("the record PR may only add a run report and the G5 ledger row: " + "; ".join(scope or view.touched_protected))
        head = full["head"]["sha"]
        if self.strict:
            chk = self.gate_check(head)
            if chk != "success":
                raise StudioError(f"the required `{GATE_CHECK}` check is {chk} on {head[:7]}")
        methods = self.merge_methods()
        try:
            out = self.gh.merge_pull(pr["number"], head, f"[pkf] {pipeline}: {gate} merged (#{pr['number']})", method=methods[0])
        except GitHubError as exc:
            if exc.status == 405:
                self._repo_info = None
                raise StudioError(f"GitHub refused the merge: {exc}; this repository allows {', '.join(self.merge_methods())}: "
                                  "merge on GitHub") from None
            raise
        self.invalidate()
        self.fetch(force=True)
        return out

    def post_approve(self, pipeline: str, gate: str, role: str | None = None) -> dict:
        """Dev mode only: post `/approve <head>` on the PR as the token's user (the approval is still in GitHub)."""
        self._idle(pipeline)
        if self.strict:
            raise StudioError("approve with a GitHub review in this mode")
        pr = self.prs(pipeline, 0).get(gate)
        if not pr:
            raise StudioError(f"{gate} has no pull request yet")
        full = self.gh.pull(pr["number"])
        body = f"/approve {full['head']['sha'][:12]}" + (f"\nApprove as: {role}" if role else "")
        out = self.gh.comment(pr["number"], body)
        m = self.merged_unapproved(pipeline)
        if m and m.get("gate") == gate and m.get("pr") == pr["number"]:
            try:  # merged without approval (K14): the comment may be all it lacked; Check again at once
                self.recheck_merged(pipeline)
            except (GitHubError, StudioError):
                pass
        return out

    # ------------------------------------------------------------------ ledger
    # ------------------------------------------------------------------ merged without approval (K14, lazy)
    def _marker_path(self, pipeline: str) -> Path:
        return self.root / MERGED_UNAPPROVED_DIR / f"{pipeline}.json"

    def merged_unapproved(self, pipeline: str) -> dict | None:
        """The marker written when a stage start found the previous gate merged without approval:
        {"gate", "pr", "merge_sha", "summary", "at"} or None."""
        doc = _json_file(self._marker_path(pipeline))
        return doc if doc.get("gate") and doc.get("pr") else None

    def clear_merged_unapproved(self, pipeline: str, pr: int | None = None) -> None:
        m = self.merged_unapproved(pipeline)
        if m and (pr is None or m.get("pr") == pr):
            self._marker_path(pipeline).unlink(missing_ok=True)

    def _write_marker(self, pipeline: str, doc: dict) -> None:
        f = self._marker_path(pipeline)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")

    def recheck_merged(self, pipeline: str) -> bool | None:
        """Check again (one gate_status call): True when the marked PR now has its approval (the marker is removed),
        False when it still has not (the summary is updated), None when there is no marker."""
        m = self.merged_unapproved(pipeline)
        if not m:
            return None
        self.invalidate()
        pr = self.prs(pipeline, 0).get(m["gate"])
        if not pr or pr.get("number") != m["pr"] or not pr.get("merged_at"):
            self.clear_merged_unapproved(pipeline)
            return True
        res, _ = self.gate_status(pipeline, m["gate"], pr)
        if res.passed:
            self.clear_merged_unapproved(pipeline)
            return True
        self._write_marker(pipeline, {**m, "summary": res.summary(), "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()})
        return False

    # ------------------------------------------------------------------ ledger
    def ledger_row(self, pipeline: str, gate: str) -> dict:
        pr = self.prs(pipeline, 0).get(gate)
        if not pr or not pr.get("merged_at"):
            raise StudioError(f"{gate} is not merged")
        res, full = self.gate_status(pipeline, gate, pr)
        if not res.passed:
            summary = res.summary()
            merge_sha = full.get("merge_commit_sha") or pr.get("merge_commit_sha")
            blocked = bool(res.blocked)
            self._write_marker(pipeline, {"gate": gate, "pr": pr["number"], "merge_sha": merge_sha, "summary": summary,
                                          "blocked": blocked,
                                          "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()})
            if blocked:  # an approval cannot fix a block (files outside the pipeline, protected files, ...)
                msg = (f"{gate} PR #{pr['number']} was merged although its gate is blocked ({summary}); an approval cannot fix "
                       f"this: Reopen {gate} and request approval again (the new PR does not carry what blocked it)")
            else:
                how = ("comment `/approve` on it" if not self.strict else "approve it with a GitHub review") + ", then start this stage again"
                msg = f"{gate} PR #{pr['number']} was merged without the required approval ({summary}): {how}"
            exc = MergedUnapproved(msg, gate, pr["number"], merge_sha, summary)
            exc.blocked = blocked
            raise exc
        self.clear_merged_unapproved(pipeline, pr["number"])
        head = full["head"]["sha"]
        ih = L.input_hash(gate, lambda p: self.read_at(head, p), pipeline, lambda pre: self.list_at(head, pre))
        return L.make_row(gate, full, res, self.cfg.mode, ih, self.roster_sha, L.self_asserted(res, full, GATES[gate].roles))

    def write_ledger(self, pipeline: str, row: dict) -> None:
        p = self.worktree_path(pipeline) / L.ledger_path(pipeline)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(L.append(p.read_text(encoding="utf-8") if p.exists() else None, row), encoding="utf-8", newline="\n")

    def evidence_args(self, row: dict, role: str = "data_engineer") -> list[str]:
        a = next((x for x in row["approvers"] if x["role"] == role), row["approvers"][0])
        return ["--by", f"@{a['login']}", "--evidence", a["url"] or row["pr_url"], *(["--github-id", str(a["github_id"])] if a.get("github_id") else []),
                "--head-sha", row["head_sha"]]
