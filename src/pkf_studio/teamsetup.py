"""The setup / team pull request: project config, roster, .gitignore, the pkf-gate workflow, CODEOWNERS and the framework
wheel the workflow installs (tools/). These are the only files stage PRs may not touch; they change only here, with
the code owners' review (branch pkf-studio/team-<timestamp>)."""
from __future__ import annotations

import datetime as dt
import hashlib
import subprocess
import sys
from pathlib import Path

from . import STUDIO_DIR, VERSION
from .project import GITIGNORE, PROJECT_FILE, TEAM_FILE, ProjectConfig, Team, dump

WORKFLOW = """# pkf-gate: the required check for pkf Studio pull requests. It runs the BASE branch's framework (tools/) and
# reads the pull request's head only as data. Do not use pull_request_target here; never add secrets.
name: pkf-gate
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  pull_request_review:
    types: [submitted, dismissed, edited]
permissions:
  contents: read
  pull-requests: read
concurrency:
  group: pkf-gate-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  pkf-gate:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - name: Base branch (the code this check runs)
        uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.base.ref }}
          path: base
          persist-credentials: false
      - name: Pull request head (data)
        uses: actions/checkout@v4
        with:
          ref: refs/pull/${{ github.event.pull_request.number }}/head
          path: head
          fetch-depth: 0
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install the framework from the base branch
        run: |
          cd base/tools && sha256sum -c SHA256SUMS && cd ../..
          python -m pip install -r base/tools/requirements-gate.txt
          python -m pip install --no-deps base/tools/package_factory-*.whl
      - name: Verify the gate
        working-directory: head
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: pkf studio verify --pr ${{ github.event.pull_request.number }} --base-dir ../base --head-dir .
"""

GATE_PACKAGES = ("pydantic", "pydantic-core", "annotated-types", "typing-extensions", "typing-inspection", "ruamel.yaml",
                 "pyyaml", "requests", "urllib3", "certifi", "idna", "charset-normalizer", "boto3", "botocore", "s3transfer",
                 "jmespath", "python-dateutil", "six", "moto", "werkzeug", "markupsafe", "jinja2", "xmltodict", "responses",
                 "cryptography", "cffi", "pycparser")


def gate_requirements() -> str:
    """The gate's dependencies pinned to the versions this Studio runs with (so the check runs what you tested)."""
    import importlib.metadata as md

    lines = ["# Installed by the pkf-gate workflow, pinned to the Studio's versions. For hashes: pip-compile --generate-hashes"]
    for name in GATE_PACKAGES:
        try:
            lines.append(f"{name}=={md.version(name)}")
        except md.PackageNotFoundError:
            continue
    return "\n".join(lines) + "\n"


def codeowners(owners: list[str]) -> str:
    who = " ".join(owners)
    return ("# Changes to the Studio's roster and mode, the workflows and the gate's framework need these owners' review.\n"
            f"/{STUDIO_DIR}/project.yaml {who}\n/{STUDIO_DIR}/team.yaml {who}\n/.github/ {who}\n/tools/ {who}\n")


def gitignore(existing: str | None) -> str:
    lines = (existing or "").splitlines()
    add = [x for x in GITIGNORE if x not in lines]
    if not add:
        return existing or ""
    return ((existing or "").rstrip("\n") + "\n\n# pkf Studio (local state)\n" if existing else "# pkf Studio (local state)\n") + "\n".join(add) + "\n"


GITATTRIBUTES_LINE = "* -text"


def gitattributes(existing: str | None) -> str:
    """`* -text`: git never converts line endings in any clone, so approved bytes stay the bytes that were approved
    (B13). Never `text eol=lf`: that would make the CRLF files already committed show as modified."""
    lines = (existing or "").splitlines()
    if GITATTRIBUTES_LINE in (x.strip() for x in lines):
        return existing or ""
    head = "# pkf Studio: no line-ending conversion (approved files are fingerprinted byte for byte)\n"
    return ((existing or "").rstrip("\n") + "\n\n" if existing else "") + head + GITATTRIBUTES_LINE + "\n"


def framework_project() -> Path | None:
    """The package-factory source tree this Studio runs from (editable install), for building the gate's wheel."""
    import pkf

    for parent in Path(pkf.__file__).resolve().parents:
        pp = parent / "pyproject.toml"
        if pp.exists() and 'name = "package-factory"' in pp.read_text(encoding="utf-8"):
            return parent
    return None


def build_wheel(dest: Path, log=print) -> Path | None:
    src = framework_project()
    if src is None:
        log("  the framework source is not available here (not an editable install): copy the wheel into tools/ by hand")
        return None
    if (src / ".git").exists():
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(src), capture_output=True, text=True,
                               encoding="utf-8", errors="replace").stdout.strip()
        if dirty:
            log("  the framework's source has uncommitted changes: the gate's wheel is built from them (commit them first)")
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("package_factory-*.whl"):
        old.unlink()
    p = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(dest), str(src)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if p.returncode != 0:
        log("  building the wheel failed: " + (p.stderr or p.stdout)[-800:])
        return None
    whl = next(dest.glob("package_factory-*.whl"), None)
    if whl:
        (dest / "SHA256SUMS").write_text(f"{hashlib.sha256(whl.read_bytes()).hexdigest()}  {whl.name}\n", encoding="utf-8", newline="\n")
    return whl


def write(wt: Path, cfg: ProjectConfig, team: Team, owners: list[str], with_gate: bool = True, log=print) -> list[str]:
    wt = Path(wt)
    (wt / STUDIO_DIR).mkdir(exist_ok=True)
    (wt / PROJECT_FILE).write_text(f"# pkf Studio project (read from the base branch only). Studio {VERSION}\n" + dump(cfg), encoding="utf-8", newline="\n")
    (wt / TEAM_FILE).write_text("# pkf Studio team roster (read from the base branch only)\n" + dump(team), encoding="utf-8", newline="\n")
    gi = wt / ".gitignore"
    gi.write_text(gitignore(gi.read_text(encoding="utf-8") if gi.exists() else None), encoding="utf-8", newline="\n")
    ga = wt / ".gitattributes"
    ga.write_text(gitattributes(ga.read_text(encoding="utf-8") if ga.exists() else None), encoding="utf-8", newline="\n")
    written = [PROJECT_FILE, TEAM_FILE, ".gitignore", ".gitattributes"]
    if not (wt / "pkf.toml").exists():
        subprocess.run([sys.executable, "-P", "-m", "pkf.cli", "init", "."], cwd=str(wt), capture_output=True, timeout=120)
        written.append("pkf.toml")
    if with_gate:
        (wt / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (wt / ".github" / "workflows" / "pkf-gate.yml").write_text(WORKFLOW, encoding="utf-8", newline="\n")
        (wt / ".github" / "CODEOWNERS").write_text(codeowners(owners), encoding="utf-8", newline="\n")
        (wt / "tools").mkdir(exist_ok=True)
        (wt / "tools" / "requirements-gate.txt").write_text(gate_requirements(), encoding="utf-8", newline="\n")
        written += [".github/workflows/pkf-gate.yml", ".github/CODEOWNERS", "tools/requirements-gate.txt"]
        if build_wheel(wt / "tools", log):
            written.append("tools/package_factory-*.whl")
    return written


def branch_name() -> str:
    return f"pkf-studio/team-{dt.datetime.now(dt.timezone.utc):%Y%m%dt%H%M%S}"


def open_pr(studio, cfg: ProjectConfig, team: Team, owners: list[str], with_gate: bool = True, log=print) -> dict:
    """Write the files on a new team branch in the '_team' worktree, commit, push and open the PR."""
    branch = branch_name()
    studio.fetch(force=True)
    wt = studio.git.worktree("_team", branch, studio.base_ref)
    written = write(wt, cfg, team, owners, with_gate, log)
    me = team.by_login(studio.login) if studio.login else None
    try:
        studio.git.commit(wt, "pkf Studio: project and team", me.name if me else None, me.email if me else None,
                          check=studio.staged_check(wt, protected=False))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from None
    studio.git.push(wt, branch)
    roles = "\n".join(f"- {p.name} <{p.email}> @{p.github or '—'}: {', '.join(p.roles)}" for p in team.people)
    body = (f"## pkf Studio — project and team\n\nMode **{cfg.mode}**, base `{cfg.base_branch}`.\n\n### Team\n{roles}\n\n"
            f"### Files\n" + "\n".join(f"- `{w}`" for w in written) +
            "\n\nThe code owners review this PR. Lowering the mode also needs an Architect who is not the author."
            "\nThe first roster is approved by a repository admin.\n")
    pr = studio.gh.create_pull("[pkf] Studio project and team", branch, cfg.base_branch, body)
    studio.invalidate()
    return pr
