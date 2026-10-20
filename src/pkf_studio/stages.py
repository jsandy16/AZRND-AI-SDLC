"""Where a pipeline stands — derived every time, never stored.

Inputs: the gate PRs on GitHub (pkf/<p>/g1…g5), the base branch's content and each merged PR's head content. A merged
gate is *approved* while what it covered (its input hash, ledger.input_hash) is the same on the base branch as on the
approved head; otherwise it is *stale* and has to be approved again (reopened). The framework's own content hashes
(plan and code digests) are inside those input hashes, so a changed plan or code file makes G3/G4 stale too.

  not_started  no branch, no PR             in_progress  branch exists, no open PR
  waiting      PR open                      approved     PR merged, inputs unchanged on base
  stale        PR merged, inputs changed    skipped      G3 when no component needs an AI plan
  blocked      an earlier gate is not approved (or is stale)
  merged_unapproved  PR merged without the approval the gate needs (found when the next stage started; K14)
  unknown      cannot be decided: GitHub unreachable, or the merge is not in the local history (fetch failed)
RUN: blocked, ready, waiting (record PR open), done, failed (the latest merged run record says ok: false).
merged_unapproved and unknown block every later gate and RUN, like stale.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import yaml

from . import ledger as L
from .gates import GATES, ORDER
from .service import Studio, branch_for, gate_of_branch

STAGES = {"G1": "S1 Requirements", "G2": "S2 Design", "G3": "S3 Plans", "G4": "S4 Code & build", "G5": "S5 Dev environment",
          "RUN": "S6 Run local"}


@dataclass
class GateState:
    gate: str
    state: str
    pr: dict | None = None
    note: str = ""

    @property
    def title(self) -> str:
        return GATES[self.gate].title if self.gate in GATES else "Run local"

    @property
    def done(self) -> bool:
        return self.state in ("approved", "skipped")


def needs_ai_plan(read, pipeline: str) -> bool | None:
    """True when a component is in requirement mode (it needs an AI plan); None when the split is not written yet."""
    comps = L.components(read, pipeline)
    if not comps:
        return None
    for kind, name in comps:
        try:
            spec = yaml.safe_load(read(f"{L.FOLDERS[kind]}/{name}.yaml") or "") or {}
        except yaml.YAMLError:
            return True
        if (spec.get("requirement") or "").strip():
            return True
    return False


def _reopened(s: Studio, pipeline: str, gate: str, pr: dict) -> bool:
    """The gate's branch was restarted from the current base after its PR merged (Reopen): work in progress again."""
    tip = s.git.sha(f"refs/heads/{branch_for(pipeline, gate)}") or s.git.sha(f"origin/{branch_for(pipeline, gate)}")
    if not tip or tip == pr["head"]["sha"]:
        return False
    return subprocess.run(["git", "merge-base", "--is-ancestor", s.base_ref, tip], cwd=str(s.root)).returncode == 0


def _built_on(s: Studio, merge_sha: str | None, head: str) -> bool:
    """The PR's head contains the earlier gate's merge (it was started after that approval)."""
    if not merge_sha:
        return True
    if not s.git.sha(merge_sha) or not s.git.sha(head):
        s.git.run("fetch", "-q", "origin", check=False)
    return subprocess.run(["git", "merge-base", "--is-ancestor", merge_sha, head], cwd=str(s.root), capture_output=True).returncode == 0


def _in_history(s: Studio, sha: str | None) -> bool:
    if not sha:
        return True
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=str(s.root), capture_output=True).returncode == 0


def _closed_note(s: Studio, pr: dict) -> str:
    login = ((pr.get("closed_by") or {}).get("login")) or s._closed_by.get(pr["number"])
    if login is None and s.gh is not None and pr["number"] not in s._closed_by:
        try:
            login = ((s.gh.issue(pr["number"]) or {}).get("closed_by") or {}).get("login")
        except Exception:  # noqa: BLE001 - the note is only a hint
            login = None
        s._closed_by[pr["number"]] = login
    by = f" (by @{login})" if login else ""
    return f"PR #{pr['number']} was closed without merging{by}; Request approval opens a new one"


def _run_report(s: Studio, pipeline: str, pr: dict) -> dict | None:
    g = gate_of_branch((pr.get("head") or {}).get("ref", ""))
    if not g:
        return None
    run = pr["head"]["ref"].rsplit("/", 1)[-1]
    text = s.read_base(f"review/{pipeline}/runs/{run}/report.json")
    try:
        doc = json.loads(text) if text else None
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _first_issue(report: dict) -> str:
    for i in report.get("issues") or []:
        msg = i.get("msg") if isinstance(i, dict) else str(i)
        if msg:
            return str(msg)[:400]
    return "the last run failed: see its report.json and run.log"


_HASH_MEMO: dict[tuple, str] = {}
_HASH_MEMO_MAX = 4096


def _scoped_hash(s: Studio, gate: str, pipeline: str, ref: str) -> str:
    """L.input_hash_scoped at `ref`, memoised by (repository, gate, pipeline, commit sha)."""
    sha = s.git.sha(ref) if hasattr(s, "git") else None
    key = (str(getattr(s, "root", "")), gate, pipeline, sha) if sha else None
    if key is not None and key in _HASH_MEMO:
        return _HASH_MEMO[key]
    at = sha or ref
    value = L.input_hash_scoped(gate, lambda p: s.read_at(at, p), pipeline, lambda pre: s.list_at(at, pre))
    if key is not None:
        if len(_HASH_MEMO) >= _HASH_MEMO_MAX:
            _HASH_MEMO.clear()
        _HASH_MEMO[key] = value
    return value


def derive(s: Studio, pipeline: str, prs: dict | None = None) -> dict:
    prs = s.prs(pipeline) if prs is None else prs
    closed = {g: pr for g, pr in prs.items() if pr.get("state") == "closed" and not pr.get("merged_at")}
    prs = {g: pr for g, pr in prs.items() if g not in closed}  # closed unmerged: no decision
    marker = s.merged_unapproved(pipeline) if hasattr(s, "merged_unapproved") else None
    if marker:  # it counts only while it names the gate's latest merged PR
        cur = prs.get(marker.get("gate"))
        if not cur or cur.get("number") != marker.get("pr") or not cur.get("merged_at") or (
                marker.get("merge_sha") and cur.get("merge_commit_sha") and cur["merge_commit_sha"] != marker["merge_sha"]):
            s.clear_merged_unapproved(pipeline)
            marker = None
    fetch_error = getattr(s, "fetch_error", None)
    base = s.base_ref
    rows: list[GateState] = []
    earlier_ok = True
    prev_merge = None         # a gate counts only if its work was built on the latest approval of the gate before it
    for g in ORDER:
        pr = prs.get(g)
        if g == "G3" and pr is None:
            g4 = prs.get("G4")
            if g4:
                ref = base if g4.get("merged_at") else g4["head"]["sha"]
            else:  # S4 already started from the plans branch (no plan needed an approval)
                ref = s.git.sha(f"refs/heads/{branch_for(pipeline, 'G4')}")
            if ref and needs_ai_plan(lambda p: s.read_at(ref, p), pipeline) is False:
                rows.append(GateState(g, "skipped" if earlier_ok else "blocked", None, "no component needs an AI plan"))
                continue
        if not earlier_ok:
            rows.append(GateState(g, "blocked", pr, "an earlier gate must be approved first"))
            continue
        if pr and pr.get("merged_at"):
            if fetch_error and not _in_history(s, pr.get("merge_commit_sha")):
                rows.append(GateState(g, "unknown", pr, f"the base branch could not be fetched: {fetch_error} — Retry"))
                earlier_ok = False
                continue
            if _reopened(s, pipeline, g, pr):
                rows.append(GateState(g, "in_progress", None, "reopened: approve it again"))
                earlier_ok = False
                continue
            if marker and marker.get("gate") == g:
                rows.append(GateState(g, "merged_unapproved", pr, marker.get("summary") or "merged without the required approval"))
                earlier_ok = False
                continue
            head = pr["head"]["sha"]
            if not s.git.sha(head):
                s.git.run("fetch", "-q", "origin", f"pull/{pr['number']}/head", check=False)
            # 0.6.1: G5 is judged on this pipeline's own part of shared bucket stacks (L.input_hash_scoped), so another
            # pipeline's notification on the same bucket no longer makes it stale; G1–G4 are unchanged. Memoised per
            # commit (content at a commit never changes): derive() runs every few seconds while the UI polls.
            then = _scoped_hash(s, g, pipeline, head)
            now = _scoped_hash(s, g, pipeline, base)
            if then != now:
                rows.append(GateState(g, "stale", pr, "what this gate approved has changed on the base branch; approve it again"))
                earlier_ok = False
            elif not _built_on(s, prev_merge, head):
                rows.append(GateState(g, "stale", pr, "an earlier gate was approved again after this one; approve it again"))
                earlier_ok = False
            else:
                rows.append(GateState(g, "approved", pr))
                prev_merge = pr.get("merge_commit_sha") or prev_merge
            continue
        earlier_ok = False
        note = _closed_note(s, closed[g]) if g in closed else ""
        if pr:
            rows.append(GateState(g, "waiting", pr))
        elif s.git.sha(f"refs/heads/{branch_for(pipeline, g)}") or s.git.sha(f"origin/{branch_for(pipeline, g)}"):
            rows.append(GateState(g, "in_progress", None, note))
        else:
            rows.append(GateState(g, "not_started", None, note))
    run_pr = prs.get("RUN")
    if run_pr and run_pr.get("merged_at") and not _built_on(s, prev_merge, run_pr["head"]["sha"]):
        run_pr = None  # a run before the latest approvals is history
    run_note = ""
    if not earlier_ok:
        run_state = "blocked"
    elif run_pr and run_pr["state"] == "open":
        run_state = "waiting"
    elif run_pr and run_pr.get("merged_at"):
        report = _run_report(s, pipeline, run_pr)
        if report is not None and report.get("ok") is False:
            run_state, run_note = "failed", _first_issue(report)
        else:
            run_state = "done"
    else:
        run_state = "ready"
    rows.append(GateState("RUN", run_state, run_pr, run_note))
    current = next((r for r in rows if not r.done and r.state != "blocked"), rows[-1])
    return {"gates": rows, "current": current.gate, "by_gate": {r.gate: r for r in rows}}


def derive_offline(s: Studio, pipeline: str, error: str) -> dict:
    """derive() without GitHub (K9): each gate is "unknown" when this computer or its last fetch has the gate's
    branch, else "not_started"; RUN is blocked. Same shape as derive()."""
    note = f"GitHub unreachable: {error}"
    rows = []
    for g in ORDER:
        b = branch_for(pipeline, g)
        known = s.git.sha(f"refs/heads/{b}") or s.git.sha(f"origin/{b}")
        rows.append(GateState(g, "unknown" if known else "not_started", None, note))
    rows.append(GateState("RUN", "blocked", None, note))
    current = next((r for r in rows if r.state == "unknown"), None) or next(r for r in rows if r.state == "not_started")
    if any(r.state == "unknown" for r in rows):  # the latest known stage is where the work is
        current = [r for r in rows if r.state == "unknown"][-1]
    return {"gates": rows, "current": current.gate, "by_gate": {r.gate: r for r in rows}, "offline": True}


def stage_started(s: Studio, pipeline: str, gate: str, states: dict) -> bool:
    """Has the work that opens this stage been committed on its branch: the ledger row of the previous gate's CURRENT
    approval (its latest merged PR) is in the stage branch's last commit."""
    if gate == "G1":
        return (s.worktree_path(pipeline) / f"pipelines/{pipeline}.yaml").exists()
    if s.worktree_branch(pipeline) != branch_for(pipeline, gate):
        return False
    prev = [x for x in states["gates"][:ORDER.index(gate)] if x.state != "skipped"][-1]
    if not prev.pr:
        return False
    rows = L.parse(s.git.run("show", f"HEAD:{L.ledger_path(pipeline)}", cwd=s.worktree_path(pipeline), check=False))
    return any(r.get("gate") == prev.gate and r.get("pr") == prev.pr["number"] for r in rows)
