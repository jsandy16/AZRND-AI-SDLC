"""`pkf studio verify --pr <n>` — the pkf-gate check. It runs in a GitHub Action from the BASE branch's code, with the
pull request's head checked out as data, and fails the check when:

- a stage PR (pkf/<p>/g1…g5) changes protected files (roster, config, .github/, tools/, CODEOWNERS — renames included)
  or anything outside its own pipeline (a record PR pkf/<p>/run-…: anything but its run report and the G5 ledger row);
- the gate is not passed under the base branch's mode and roster (same evaluation as the app, gates.evaluate);
- any approvals-ledger row the PR adds or changes differs in any field from the row rebuilt from GitHub and git, or the
  PR changes another pipeline's ledger (non-stage PRs may not change any ledger);
- G4: a component with custom steps fails its own tests when re-run (`pkf test <kind> <name>`: the lock's recorded
  results are not trusted), or `pkf test pipeline` or the draft build `pkf build --draft` fails on the head;
- G5: `pkf test pipeline` or `pkf build --check` fails on the head;
- any PR lowers the mode — or deletes, moves or breaks the project config — without an Architect (not an author).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import MODES, PROTECTED_PATHS
from . import ledger as L
from .gates import GATES, PRView, evaluate
from .github import GitHub, GitHubError, api_url_for
from .gitops import parse_github_remote
from .project import PROJECT_FILE, TEAM_FILE, parse_config, parse_team
from .service import gate_of_branch


def _show(repo_dir: Path, ref: str, path: str) -> str | None:
    p = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=str(repo_dir), capture_output=True, timeout=60)
    return p.stdout.decode("utf-8", errors="replace") if p.returncode == 0 else None


def _ls(repo_dir: Path, ref: str, prefix: str) -> list[str]:
    p = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref, prefix], cwd=str(repo_dir), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    return [x for x in p.stdout.splitlines() if x]


def _roster_sha(base_dir: Path, base_ref: str) -> str | None:
    p = subprocess.run(["git", "rev-parse", f"{base_ref}:{TEAM_FILE}"], cwd=str(base_dir), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.stdout.strip() or None if p.returncode == 0 else None


def rebuild_row(gh: GitHub, repo_dir: Path, team, mode: str, pipeline: str, row: dict, roster_sha: str | None = None,
                base_branch: str | None = None) -> list[str]:
    """Differences between a ledger row and the row rebuilt from GitHub and git (every field but recorded_at)."""
    gate = row.get("gate")
    if gate not in GATES:
        return [f"unknown gate {gate!r}"]
    try:
        pr = gh.pull(int(row["pr"]))
    except (GitHubError, KeyError, ValueError) as exc:
        return [f"{gate}: PR {row.get('pr')} cannot be read: {exc}"]
    problems = []
    if gate_of_branch(pr["head"]["ref"]) != (pipeline, gate):
        problems.append(f"{gate}: PR #{pr['number']} is not this pipeline's {gate} branch")
    if not pr.get("merged_at"):
        problems.append(f"{gate}: PR #{pr['number']} is not merged")
    if base_branch and pr["base"]["ref"] != base_branch:
        problems.append(f"{gate}: PR #{pr['number']} was merged into {pr['base']['ref']}, not {base_branch}")
    view, _ = gh.pr_view(pr["number"])
    res = evaluate(gate, mode, team, view, gh.evidence(pr["number"]))
    if not res.passed:
        problems.append(f"{gate}: GitHub shows the gate as not passed ({res.summary()})")
        return problems
    head = pr["head"]["sha"]
    if subprocess.run(["git", "cat-file", "-e", f"{head}^{{commit}}"], cwd=str(repo_dir), capture_output=True).returncode != 0:
        return problems + [f"{gate}: commit {head[:7]} is not in the repository history"]
    ih = L.input_hash(gate, lambda p: _show(repo_dir, head, p), pipeline, lambda pre: _ls(repo_dir, head, pre))
    want = L.make_row(gate, pr, res, mode, ih, roster_sha if roster_sha is not None else row.get("roster_sha"),
                      L.self_asserted(res, pr, GATES[gate].roles))
    for k in sorted(set(want) | set(row)):
        if k == "recorded_at":
            continue
        if want.get(k) != row.get(k):
            label = {"approvers": "approvers differ from GitHub", "input_hash": "input hash differs from the approved head's content"}.get(k, f"{k} differs")
            problems.append(f"{gate}: {label} (recorded {str(row.get(k))[:120]}, expected {str(want.get(k))[:120]})")
    return problems


def _mode_rank(m: str | None) -> int:
    return MODES.index(m) if m in MODES else 0


def _changed_ledgers(base_dir: Path, base_ref: str, head_dir: Path) -> dict[str, tuple[list[dict], list[dict]]]:
    """{pipeline: (rows on base, rows on the head)} for every approvals file that differs."""
    out = {}
    names = set(_ls(base_dir, base_ref, ".pkf-studio/approvals/")) | set(_ls(head_dir, "HEAD", ".pkf-studio/approvals/"))
    for path in sorted(names):
        if not path.endswith(".jsonl"):
            continue
        b, h = _show(base_dir, base_ref, path), _show(head_dir, "HEAD", path)
        if b != h:
            out[Path(path).stem] = (L.parse(b), L.parse(h))
    return out


def verify(gh: GitHub, n: int, base_dir: Path, head_dir: Path, base_ref: str = "HEAD", run_tests: bool = True) -> tuple[bool, list[str]]:
    lines: list[str] = []
    pr = gh.pull(n)
    cfg = parse_config(_show(base_dir, base_ref, PROJECT_FILE))
    team = parse_team(_show(base_dir, base_ref, TEAM_FILE))
    branch = pr["head"]["ref"]
    g = gate_of_branch(branch)
    if ((pr["head"].get("repo") or {}).get("full_name") or "") != ((pr["base"].get("repo") or {}).get("full_name") or ""):
        return (False, ["PRs from forks are not accepted for pkf Studio branches"]) if g else (True, ["not a Studio PR"])
    if cfg is None or team is None:
        if g:
            return False, ["the base branch has no Studio project/team: stage PRs are refused until the setup PR is merged"]
        return True, ["the base branch has no Studio project/team yet: only the setup PR is expected"]
    if pr["base"]["ref"] != cfg.base_branch:
        return False, [f"the PR targets {pr['base']['ref']}, the project's base branch is {cfg.base_branch}"]
    view, _ = gh.pr_view(n)
    ok = True
    if PROJECT_FILE in view.files:  # any PR that touches (or moves) the project config: a lower mode needs an Architect
        ok_mode, mode_lines = check_mode_change(gh, n, cfg, team, view, head_dir)
        ok, lines = ok and ok_mode, lines + mode_lines
    ledgers = _changed_ledgers(base_dir, base_ref, head_dir)
    if g is None:
        if ledgers:
            return False, lines + [f"FAIL: only a pipeline's own stage or record PR may change its approvals ledger ({', '.join(ledgers)})"]
        lines.append(f"{branch} is not a Studio stage branch: code owners review protected files")
        return ok, lines
    pipeline, gate = g
    if view.touched_protected:
        ok = False
        lines.append(f"FAIL: stage PRs may not change {', '.join(view.touched_protected[:5])}")
    scope = L.scope_problems(pipeline, gate, view.files, lambda q: _show(head_dir, "HEAD", q))
    if scope:
        ok = False
        lines += [f"FAIL: {x}" for x in scope]
    if gate in GATES:
        res = evaluate(gate, cfg.mode, team, view, gh.evidence(n))
        lines.append(f"{gate} {GATES[gate].title} ({cfg.mode}): {res.summary()}")
        lines += [f"  note: {x}" for x in res.notes]
        if not res.passed:
            ok = False
    roster = _roster_sha(base_dir, base_ref)
    for other, (before_rows, after_rows) in ledgers.items():
        if other != pipeline:
            ok = False
            lines.append(f"FAIL ledger: this PR changes pipeline {other}'s approvals")
            continue
        before = {(r.get("gate"), r.get("pr")): r for r in before_rows}
        keys_after = {(r.get("gate"), r.get("pr")) for r in after_rows}
        if [k for k in before if k not in keys_after]:
            ok = False
            lines.append("FAIL ledger: rows removed")
        for row in after_rows:
            if before.get((row.get("gate"), row.get("pr"))) == row:
                continue
            probs = rebuild_row(gh, head_dir, team, cfg.mode, pipeline, row, roster, cfg.base_branch)
            if probs:
                ok = False
                lines += [f"FAIL ledger: {p}" for p in probs]
            else:
                lines.append(f"ledger {row['gate']} (PR #{row['pr']}): matches GitHub")
    if run_tests and gate in ("G4", "G5"):
        if gate == "G4":  # CI trust (K3): the lock's own test results are not trusted; re-test every custom component
            ok_re, re_lines = retest_custom_components(head_dir, pipeline)
            ok, lines = ok and ok_re, lines + re_lines
        # G4 holds code that is generated and tested but not approved yet (the draft build); G5 the real, committed build
        last = ["build", pipeline, "--draft"] if gate == "G4" else ["build", pipeline, "--check"]
        for args in (["test", "pipeline", pipeline], last):
            doc = run_pkf(head_dir, args)
            lines.append(f"pkf {' '.join(args)}: {'OK' if doc.get('ok') else 'FAILED'}")
            if not doc.get("ok"):
                ok = False
                lines += [f"  {i.get('msg', '')}"[:300] for i in doc.get("issues", [])[:10]]
    return ok, lines


def run_pkf(head_dir: Path, args: list[str], timeout: int = 1800) -> dict:
    """`pkf --json <args>` in the head checkout (the framework of the base branch's gate wheel, never the head's own
    code: -P keeps the checkout off sys.path)."""
    try:
        p = subprocess.run([sys.executable, "-P", "-m", "pkf.cli", "--json", *args], cwd=str(head_dir), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env={**os.environ, "PKF_AGENT_SESSION": "1", "GITHUB_TOKEN": "", "GH_TOKEN": ""})
    except subprocess.TimeoutExpired:
        return {"ok": False, "issues": [{"msg": f"pkf {' '.join(args)} did not finish within {timeout} s", "code": "timeout"}]}
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        doc = {"ok": False, "issues": [{"msg": (p.stderr or p.stdout or "no output")[-500:]}]}
    return doc


def _custom_components(head_dir: Path, pipeline: str) -> list[tuple[str, str, dict]]:
    """(kind, name, committed lock) of every component of the pipeline whose plan has custom steps."""
    import yaml

    try:
        from pkf.design import CUSTOM_OPS
    except ImportError:  # pragma: no cover - the gate wheel always has pkf
        CUSTOM_OPS = ("custom", "custom_delivery")
    out = []
    for kind, name in L.components(lambda q: _show(head_dir, "HEAD", q), pipeline):
        try:
            lock = yaml.safe_load(_show(head_dir, "HEAD", f"{L.FOLDERS[kind]}/{name}.lock.yaml") or "") or {}
        except yaml.YAMLError:
            lock = {}
        steps = (lock.get("plan") or {}).get("steps") or [] if isinstance(lock, dict) else []
        if any(isinstance(s, dict) and s.get("op") in CUSTOM_OPS for s in steps):
            out.append((kind, name, lock))
    return out


def _retested_digest(head_dir: Path, kind: str, name: str) -> str | None:
    """code_digest of the lock `pkf test` just rewrote, over the code file as it is in the checkout."""
    import hashlib

    import yaml

    try:
        from pkf.design import code_digest
    except ImportError:  # pragma: no cover
        return None
    folder = L.FOLDERS[kind]
    lp, cp = head_dir / folder / f"{name}.lock.yaml", head_dir / folder / f"{name}.py"
    if not lp.exists() or not cp.exists():
        return None
    try:
        lock = yaml.safe_load(lp.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    return code_digest(lock, hashlib.sha256(cp.read_bytes()).hexdigest())


def retest_custom_components(head_dir: Path, pipeline: str) -> tuple[bool, list[str]]:
    """G4 in CI (K3, R2): `pkf test <kind> <name>` for every component with custom steps, in the throw-away head
    checkout. FAIL when the re-tested stage is not code_generated/approved, or when the committed lock has a code
    approval whose digest differs from the re-tested code digest."""
    ok, lines = True, []
    for kind, name, lock in _custom_components(head_dir, pipeline):
        doc = run_pkf(head_dir, ["test", kind, name])
        st = (doc.get("data") or {}).get("status") or {}
        stage = st.get("stage")
        if stage not in ("code_generated", "approved"):
            detail = (_first_msg(doc.get("issues")) or st.get("next") or stage or "no result")[:300]
            ok = False
            lines.append(f"FAIL: {kind} {name}: the code fails its own tests when re-run ({detail})")
            continue
        ca = lock.get("code_approval") if isinstance(lock, dict) else None
        if isinstance(ca, dict):
            now = _retested_digest(head_dir, kind, name)
            if ca.get("digest") != now:
                ok = False
                lines.append(f"FAIL: {kind} {name}: the code approval does not match the re-tested code "
                             f"(approved {str(ca.get('digest'))[:12]}, re-tested {str(now)[:12]})")
                continue
        lines.append(f"pkf test {kind} {name}: OK ({stage})")
    return ok, lines


def _first_msg(issues) -> str | None:
    rows = [i for i in issues or [] if isinstance(i, dict) and i.get("msg")]
    rows = [i for i in rows if i.get("severity", "error") == "error"] or rows
    return str(rows[0]["msg"]) if rows else None


def check_mode_change(gh: GitHub, n: int, cfg, team, view: PRView, head_dir: Path) -> tuple[bool, list[str]]:
    """Lowering the mode — including deleting, moving or breaking the config — needs an Architect who is not an author
    (the code owners' review is enforced by GitHub's rules)."""
    try:
        new = parse_config((head_dir / PROJECT_FILE).read_text(encoding="utf-8")) if (head_dir / PROJECT_FILE).exists() else None
    except Exception:  # noqa: BLE001 - an invalid config counts as lowering the mode
        new = None
    new_mode = new.mode if new else None
    if new is not None and _mode_rank(new_mode) >= _mode_rank(cfg.mode):
        return True, [f"project config changed; mode stays {cfg.mode}" + (f" → {new_mode}" if new_mode != cfg.mode else "")]
    if cfg.mode == "dev" and new is not None:
        return True, ["project config changed (dev)"]
    arch = [e for e in gh.evidence(n, check_write=False) if e.kind == "review" and e.state == "APPROVED" and e.sha == view.head_sha
            and (p := team.by_id(e.user_id)) and "architect" in p.roles and e.user_id not in ({view.author_id} | view.commit_user_ids)]
    what = f"lowered from {cfg.mode} to {new_mode}" if new is not None else "removed or made invalid"
    if not arch:
        return False, [f"FAIL: the mode is {what}: an Architect who is not an author must approve this PR"]
    return True, [f"mode {what}, approved by Architect @{arch[0].login}"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pkf studio verify")
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--base-dir", default="../base", help="checkout of the base branch (the roster and mode come from here)")
    ap.add_argument("--head-dir", default=".", help="checkout of the PR head (data)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="OWNER/REPO")
    ap.add_argument("--api", default=os.environ.get("GITHUB_API_URL"))
    ap.add_argument("--no-tests", action="store_true")
    a = ap.parse_args(argv)
    head_dir, base_dir = Path(a.head_dir).resolve(), Path(a.base_dir).resolve()
    repo = a.repo
    if not repo:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(head_dir), capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout.strip()
        parsed = parse_github_remote(url)
        if not parsed:
            print("cannot tell the repository (pass --repo OWNER/REPO)")
            return 2
        repo = f"{parsed[1]}/{parsed[2]}"
        a.api = a.api or api_url_for(parsed[0])
    owner, name = repo.split("/", 1)
    gh = GitHub(a.api or "https://api.github.com", owner, name, os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    ok, lines = verify(gh, a.pr, base_dir, head_dir, run_tests=not a.no_tests)
    text = "\n".join([f"pkf-gate for PR #{a.pr}: {'PASSED' if ok else 'FAILED'}", *lines])
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8", newline="\n") as fh:
            fh.write("```\n" + text + "\n```\n")
    return 0 if ok else 1
