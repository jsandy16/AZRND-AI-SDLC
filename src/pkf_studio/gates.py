"""Gate evaluation — pure functions, used by the app and by `pkf studio verify` (the pkf-gate check on GitHub).

Inputs are plain data read from GitHub (the PR, its commits, reviews and comments) and the roster from the base branch.

Rules (docs/STUDIO_PLAN.md §4.2):
  sit/ppt/prd  GitHub reviews only; the latest non-comment review per user (by user id) counts; it must be APPROVED on
               the head commit (or on an equivalent commit: only base merges since, this pipeline's files unchanged);
               CHANGES_REQUESTED on the head by a roster member blocks; the PR opener and every commit author/committer
               cannot approve; a commit whose author is not linked to a GitHub account blocks; each required role is
               filled by a different person.
  dev          reviews as above, plus `/approve <sha7+>` comments matching the head (edited comments ignored, writer
               access required); `/reject <reason>` blocks; one person may fill every role.
  multi-role   `Approve as: architect` in the review/comment body restricts that approval to the named role(s).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import ROLE_LABELS, STRICT_MODES


@dataclass(frozen=True)
class Gate:
    id: str
    title: str
    roles: tuple[str, ...]           # one approval slot per role
    reviews: str                     # what the reviewer looks at


GATES: dict[str, Gate] = {g.id: g for g in [
    Gate("G1", "Requirements", ("product_owner", "business_analyst", "architect"),
         "original document, finalized requirement and examples, pseudocode, the decomposer's split (no open questions)"),
    Gate("G2", "Design", ("data_engineer",), "the split into trigger / source / transformation / destination, settings, examples"),
    Gate("G3", "Plans", ("data_engineer",), "environment values; every component plan: steps, pseudocode, test cases, simulation"),
    Gate("G4", "Code & build", ("data_engineer",), "generated code and its tests, capability summary, end-to-end examples, built DAG"),
    Gate("G5", "Dev environment", ("data_engineer",), "Terraform and its saved plan, IAM policy, deploy manifest, dry run of the first run"),
]}
ORDER = list(GATES)


@dataclass
class Evidence:
    kind: str                        # review | comment
    id: int
    user_id: int | None
    login: str
    state: str                       # review: APPROVED / CHANGES_REQUESTED / COMMENTED / DISMISSED; comment: APPROVE / REJECT / OTHER
    sha: str | None                  # review: commit_id; comment: the sha written after /approve
    body: str = ""
    submitted_at: str | None = None
    edited: bool = False
    html_url: str | None = None
    can_write: bool | None = None    # comments: the commenter has write access


@dataclass
class PRView:
    number: int
    head_sha: str
    author_id: int | None
    author_login: str
    commit_user_ids: set[int] = field(default_factory=set)       # authors and committers linked to GitHub accounts
    unlinked_commit: bool = False                                # a commit whose author is no GitHub account
    state: str = "open"
    merged: bool = False
    html_url: str | None = None
    touched_protected: list[str] = field(default_factory=list)   # roster, config, .github/, tools/ (incl. renames away from them)
    files: list[str] = field(default_factory=list)               # every file the PR changes (old and new names)
    too_many_commits: bool = False                               # > 250 commits: GitHub cannot list them all


@dataclass
class Approval:
    person: str
    login: str
    user_id: int | None
    role: str
    evidence: Evidence


@dataclass
class GateResult:
    gate: str
    passed: bool
    approvals: list[Approval]
    missing: list[str]                 # role labels still needed
    blocked: list[str]                 # reasons that block regardless of approvals
    notes: list[str]

    def summary(self) -> str:
        if self.blocked:
            return "blocked: " + "; ".join(self.blocked)
        if self.passed:
            return "approved by " + ", ".join(f"{a.person} ({ROLE_LABELS[a.role]})" for a in self.approvals)
        return "waiting for " + ", ".join(self.missing)


_APPROVE = re.compile(r"^\s*/approve(?:\s+([0-9a-f]{7,40}))?\s*$", re.I | re.M)
_APPROVE_ANY = re.compile(r"^\s*/approve(.*)$", re.I | re.M)
_REJECT = re.compile(r"^\s*/reject\b(.*)$", re.I | re.M)
_AS = re.compile(r"approve\s+as\s*:\s*([a-z_ ,]+)", re.I)


def parse_comment(body: str) -> tuple[str, str | None]:
    """('APPROVE', sha) | ('REJECT', reason) | ('UNRECOGNISED', text) | ('OTHER', None)

    UNRECOGNISED: a line starting with /approve that is neither `/approve` nor `/approve <7–40 hex>` (e.g.
    `/approve lgtm`, `/approve 9fc07f6 looks good`, a 6-character sha): it approves nothing, and the gate says so (C21)."""
    m = _REJECT.search(body or "")
    if m:
        return "REJECT", (m.group(1) or "").strip()
    m = _APPROVE.search(body or "")
    if m:
        return "APPROVE", (m.group(1) or "").lower()  # "" = the commit that was the PR's head when the comment was written
    m = _APPROVE_ANY.search(body or "")
    if m:
        return "UNRECOGNISED", (m.group(1) or "").strip()[:200]
    return "OTHER", None


def roles_named(body: str) -> set[str] | None:
    m = _AS.search(body or "")
    if not m:
        return None
    names = {x.strip().lower().replace(" ", "_") for x in m.group(1).split(",") if x.strip()}
    aliases = {"po": "product_owner", "ba": "business_analyst", "de": "data_engineer", "product_owner": "product_owner",
               "business_analyst": "business_analyst", "architect": "architect", "data_engineer": "data_engineer"}
    return {aliases[n] for n in names if n in aliases}


def assign(slots: list[str], candidates: list[tuple[object, set[str]]], distinct: bool) -> list[tuple[str, object]] | None:
    """Fill every role slot with a candidate eligible for it (each candidate at most once when distinct). Backtracking;
    candidates are few. Returns [(role, candidate)] or None."""
    def go(i: int, used: set[int]) -> list | None:
        if i == len(slots):
            return []
        for ci, (cand, roles) in enumerate(candidates):
            if slots[i] in roles and (not distinct or ci not in used):
                rest = go(i + 1, used | {ci})
                if rest is not None:
                    return [(slots[i], cand)] + rest
        return None
    return go(0, set())


def evaluate(gate_id: str, mode: str, team, pr: PRView, evidence: list[Evidence],
             equivalent: Callable[[str], bool] = lambda sha: False) -> GateResult:
    gate = GATES[gate_id]
    strict = mode in STRICT_MODES
    notes: list[str] = []
    blocked: list[str] = []
    if pr.touched_protected:
        blocked.append(f"the PR changes protected files ({', '.join(pr.touched_protected[:3])}); those change only through a team PR")
    if pr.too_many_commits:
        blocked.append("the PR has more than 250 commits, so its authors cannot all be checked; squash or split it")
    if strict and pr.unlinked_commit:
        blocked.append("a commit's author is not linked to a GitHub account, so authorship cannot be checked")

    def on_head(sha: str | None) -> bool:
        if not sha:
            return False
        s = sha.lower()
        if len(s) >= 7 and pr.head_sha.lower().startswith(s):
            return True
        return len(s) == 40 and equivalent(s)  # only base merges since that commit; this pipeline's files unchanged

    excluded = set()
    if strict:
        excluded = {i for i in ({pr.author_id} | pr.commit_user_ids) if i is not None}

    # latest decisive record per user (reviews always; comments only in dev)
    latest: dict[object, Evidence] = {}
    for ev in sorted(evidence, key=lambda e: (e.submitted_at or "", e.id)):
        key = ev.user_id if ev.user_id is not None else ev.login.lower()
        if ev.kind == "review":
            if ev.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[key] = ev
        elif ev.kind == "comment" and not strict:
            if ev.state == "UNRECOGNISED":
                text = parse_comment(ev.body)[1] or ""
                notes.append(f"@{ev.login} wrote '/approve {text}', which is not recognised: write /approve or /approve <sha>")
                continue
            if ev.state in ("APPROVE", "REJECT"):
                if ev.edited:
                    notes.append(f"ignored an edited comment by @{ev.login}")
                    continue
                if ev.can_write is False:
                    notes.append(f"ignored /approve from @{ev.login} (no write access)")
                    continue
                latest[key] = ev

    candidates: list[tuple[Approval, set[str]]] = []
    for key, ev in latest.items():
        person = team.by_id(ev.user_id) if ev.user_id is not None else None
        if person is None:  # by login only for roster entries without a numeric id (logins can be renamed or recycled)
            cand = team.by_login(ev.login)
            person = cand if cand is not None and cand.github_id is None and not strict else None
        if person is None:
            notes.append(f"@{ev.login} is not in the team roster")
            continue
        if ev.state in ("CHANGES_REQUESTED", "REJECT") and (on_head(ev.sha) or ev.kind == "comment"):
            reason = ev.body.strip().splitlines()[0][:200] if ev.body.strip() else "changes requested"
            blocked.append(f"{person.name} requested changes: {reason}")
            continue
        if ev.state not in ("APPROVED", "APPROVE"):
            continue
        if not on_head(ev.sha):
            notes.append(f"{person.name}'s approval is on an older commit ({(ev.sha or '?')[:7]}); it must be given again")
            continue
        uid = ev.user_id if ev.user_id is not None else person.github_id
        if strict and uid in excluded:
            notes.append(f"{person.name} authored or committed to this PR, so cannot approve it")
            continue
        roles = set(person.roles) & set(gate.roles)
        named = roles_named(ev.body)
        if named is not None:
            roles &= named
        if not roles:
            notes.append(f"{person.name} approved, but does not hold a role this gate needs")
            continue
        candidates.append((Approval(person.name, ev.login, uid, "", ev), roles))

    match = assign(list(gate.roles), candidates, distinct=strict)
    approvals: list[Approval] = []
    missing: list[str] = []
    if match is not None:
        for role, a in match:
            approvals.append(Approval(a.person, a.login, a.user_id, role, a.evidence))
    else:  # the most slots we can fill, for a useful "waiting for" message
        filled: set[str] = set()
        used: set[int] = set()
        for role in gate.roles:
            for ci, (a, roles) in enumerate(candidates):
                if role in roles and (not strict or ci not in used):
                    filled.add(role)
                    used.add(ci)
                    approvals.append(Approval(a.person, a.login, a.user_id, role, a.evidence))
                    break
        missing = [ROLE_LABELS[r] for r in gate.roles if r not in filled]
    passed = match is not None and not blocked
    return GateResult(gate_id, passed, approvals, missing, blocked, notes)
