"""Workflow Developer gate steps (flow.py) with a stub Studio: the edge cases that left a run stuck at a gate step (the
S5 5.4 loop) — each test drives tick() the way the UI does every 2 s and checks that the run either advances or stops
with a clear, actionable failure, and never spins at the gate forever."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pkf_studio import flow
from pkf_studio.github import GitHubError
from pkf_studio.service import StudioError
from pkf_studio.stages import GateState

P = "pipe_a"


class StubResult:
    def __init__(self, passed: bool, blocked=None, summary="waiting for Data engineer"):
        self.passed, self.blocked, self._summary = passed, list(blocked or []), summary

    def summary(self) -> str:
        return self._summary


class StubStudio:
    """Just what flow.py's gate steps call. `gh_state` is GitHub's truth: per gate, the PR dict and whether merged."""

    def __init__(self, root: Path, strict: bool = False):
        self.root = Path(root)
        self.strict = strict
        self.cfg = SimpleNamespace(mode="sit" if strict else "dev", run_role_arn=None, aws_account_id=None)
        self.team = SimpleNamespace(people=[1])
        self.prs_by_gate: dict[str, dict] = {}
        self.passed = False
        self.merge_error: Exception | None = None
        self.merge_effect = True        # the merge happens on GitHub even when merge_error is raised afterwards
        self.merges = 0
        self.approvals = 0
        self.check = "success"
        self.head = "a" * 40

    # what flow calls
    def me(self):
        return None

    def invalidate(self):
        pass

    def worktree_path(self, p):
        return self.root / "wt"

    def worktree_branch(self, p):
        return f"pkf/{p}/g5"

    def dags_diff(self, p):
        return []

    def prs(self, p, max_age=20.0):
        return self.prs_by_gate

    def request_approval(self, p, g):
        pr = {"number": 7, "state": "open", "merged_at": None, "head": {"sha": self.head}, "html_url": "u"}
        self.prs_by_gate[g] = pr
        return pr

    def gate_status(self, p, g, pr=None):
        return StubResult(self.passed, summary="approved by Dev" if self.passed else "waiting for Data engineer"), \
            {"head": {"sha": self.head}}

    def post_approve(self, p, g, role=None):
        self.approvals += 1
        self.passed = True
        return {}

    def merge(self, p, g, pr_number=None):
        pr = self.prs_by_gate.get(g)
        if not pr or pr["state"] != "open":
            raise StudioError(f"{g} has no open pull request")
        if self.strict and self.check != "success":
            raise StudioError(f"the required `pkf-gate` check is {self.check} on {self.head[:7]}")
        if self.merge_error is not None:
            if self.merge_effect:
                self._merged(g)
            err, self.merge_error = self.merge_error, None
            raise err
        self._merged(g)
        return {"merged": True}

    def _merged(self, g):
        self.merges += 1
        self.prs_by_gate[g] = {**self.prs_by_gate[g], "state": "closed", "merged_at": "2026-10-19T10:00:00Z"}

    def gate_check(self, sha):
        return self.check


def states(s: StubStudio, g5: str | None = None, note: str = "") -> dict:
    """derive()'s shape: G1–G4 approved; G5 from GitHub's truth unless `g5` forces a state (e.g. "stale")."""
    pr = s.prs_by_gate.get("G5")
    if g5 is None:
        g5 = "approved" if pr and pr.get("merged_at") else "waiting" if pr else "in_progress"
    rows = [GateState(g, "approved", {"number": i}) for i, g in enumerate(["G1", "G2", "G3", "G4"], 1)]
    rows.append(GateState("G5", g5, pr, note))
    rows.append(GateState("RUN", "ready" if g5 == "approved" else "blocked", None))
    cur = next((r for r in rows if not r.done and r.state != "blocked"), rows[-1])
    return {"gates": rows, "current": cur.gate, "by_gate": {r.gate: r for r in rows}}


def at_54(tmp_path, monkeypatch, strict=False, auto=False) -> StubStudio:
    """A run whose cursor is at 5.4 with every earlier step done (as after 5.3 Continue)."""
    s = StubStudio(tmp_path, strict=strict)
    monkeypatch.setattr(flow, "_readiness", lambda s_, p, g: [])
    st = flow._new_state(P, s.cfg.mode)
    for x in flow.STEPS:
        if x.id == "5.4":
            break
        st["steps"][x.id]["status"] = "done"
    st.update(status="running", cursor="5.4", auto_approve=auto)
    flow._save(s.root, st)
    return s


def drive(s, n=6, g5=None, note=""):
    st = None
    for _ in range(n):
        st = flow.tick(s, P, states(s, g5, note))
    return st


def test_happy_path_still_merges_and_moves_on(tmp_path, monkeypatch):
    s = at_54(tmp_path, monkeypatch, auto=True)
    st = flow.answer(s, P, states(s), "5.3", {}) or flow.load(s.root, P)
    st = flow._advance(s, P, states(s), flow.load(s.root, P))
    flow._save(s.root, st)
    st = drive(s, 4)
    assert st["steps"]["5.4"]["status"] == "done" and st["cursor"] != "5.4", st["steps"]["5.4"]
    assert s.merges == 1


def test_merged_but_stale_does_not_wait_forever(tmp_path, monkeypatch):
    """The 5.4 loop: GitHub merged the G5 PR, but derive() calls G5 stale (e.g. another pipeline changed a shared bucket
    stack on main) — before: the step said "waiting for the approvers" forever and offered Approve as me."""
    s = at_54(tmp_path, monkeypatch, auto=True)
    st = drive(s, 1)                       # requests the PR
    s.passed = True
    s._merged("G5")                        # merged on GitHub (by the run, by a person, or in another session)
    st = drive(s, 6, g5="stale", note="what this gate approved has changed on the base branch; approve it again")
    row = st["steps"]["5.4"]
    assert st["status"] == "failed" and row["status"] == "failed", (st["status"], row)
    assert "stale" in row["detail"] or "changed" in row["detail"]
    assert "Reopen G5" in row["detail"]


@pytest.mark.parametrize("g5,note", [("in_progress", "reopened: approve it again"),
                                     ("merged_unapproved", "merged without the required approval"),
                                     ("unknown", "the base branch could not be fetched")])
def test_merged_in_any_non_approved_state_stops_with_a_reason(tmp_path, monkeypatch, g5, note):
    s = at_54(tmp_path, monkeypatch, auto=True)
    drive(s, 1)
    s._merged("G5")
    st = drive(s, 6, g5=g5, note=note)
    row = st["steps"]["5.4"]
    if g5 == "unknown":           # GitHub/fetch trouble is temporary: keep waiting, but say why
        assert st["status"] == "waiting_gate" and note in row["detail"], row
    else:
        assert st["status"] == "failed" and row["status"] == "failed", (st["status"], row)
        assert note.split(":")[0] in row["detail"], row


def test_merge_timeout_after_github_merged_is_not_a_retry_loop(tmp_path, monkeypatch):
    """The UI's GitHub client has short timeouts: the merge can go through on GitHub while the call raises. Before, the
    next tick retried merge() → "G5 has no open pull request" → failed; Retry kept phase=merge → failed again, forever."""
    s = at_54(tmp_path, monkeypatch, auto=True)
    drive(s, 1)
    s.merge_error = GitHubError("timed out")
    seen = []
    for _ in range(5):                     # the app's states are up to 15 s old: they keep saying "waiting"
        st = drive(s, 1, g5="waiting")
        seen.append(st["steps"]["5.4"]["status"])
    assert "failed" not in seen, seen
    assert st["steps"]["5.4"]["status"] == "done" and st["cursor"] != "5.4", st["steps"]["5.4"]
    assert s.merges == 1


def test_retry_after_a_merge_failure_starts_from_github_truth(tmp_path, monkeypatch):
    s = at_54(tmp_path, monkeypatch, auto=True)
    drive(s, 1)
    s.merge_error, s.merge_effect = StudioError("GitHub refused the merge: 405"), False
    st = drive(s, 2, g5="waiting")
    assert st["status"] == "failed", st
    st = flow.retry(s, P, states(s))       # nothing changed: the merge is tried again (not an endless request loop)
    assert st["steps"]["5.4"]["status"] == "done", st["steps"]["5.4"]
    assert s.merges == 1


def test_auto_approve_posts_again_after_the_head_moved(tmp_path, monkeypatch):
    """Auto-approve posted once (head A); a push moved the head (approvals reset). Before, auto_posted stayed True and
    the run waited forever for an approval nobody would give."""
    s = at_54(tmp_path, monkeypatch, auto=True)
    drive(s, 1)
    st = drive(s, 1, g5="waiting")
    assert s.approvals == 1
    # the merge did not happen yet (e.g. GitHub hiccup), and a new commit reset the approval
    st = flow.load(s.root, P)
    st["gate"]["phase"] = "approve"
    flow._save(s.root, st)
    s.prs_by_gate["G5"] = {**s.prs_by_gate["G5"], "state": "open", "merged_at": None, "head": {"sha": "b" * 40}}
    s.head, s.passed, s.merges = "b" * 40, False, 0
    st = drive(s, 3, g5="waiting")
    assert s.approvals == 2, st["steps"]["5.4"]
    assert st["steps"]["5.4"]["status"] == "done", st["steps"]["5.4"]


def test_strict_mode_waits_for_a_pending_pkf_gate_check(tmp_path, monkeypatch):
    """SIT/PPT/PRD: the required pkf-gate check runs tests and the build (minutes at G4/G5). Before, a pending check
    failed the step ("merge refused"); Retry failed again until the check finished."""
    s = at_54(tmp_path, monkeypatch, strict=True)
    drive(s, 1)
    s.passed, s.check = True, "pending"
    st = drive(s, 3, g5="waiting")
    assert st["status"] == "waiting_gate" and st["steps"]["5.4"]["status"] == "waiting", st["steps"]["5.4"]
    assert "pending" in st["steps"]["5.4"]["detail"]
    s.check = "success"
    st = drive(s, 2, g5="waiting")
    assert st["steps"]["5.4"]["status"] == "done", st["steps"]["5.4"]


def test_strict_mode_failed_check_stops_with_the_reason(tmp_path, monkeypatch):
    s = at_54(tmp_path, monkeypatch, strict=True)
    drive(s, 1)
    s.passed, s.check = True, "failure"
    st = drive(s, 2, g5="waiting")
    assert st["status"] == "failed" and "check failed" in st["steps"]["5.4"]["detail"], st["steps"]["5.4"]


def test_gate_step_never_requests_a_second_pr_for_a_merged_gate(tmp_path, monkeypatch):
    """Retry or Resume at a gate whose PR GitHub already merged (and derive calls approved) just moves on."""
    s = at_54(tmp_path, monkeypatch)
    drive(s, 1)
    s._merged("G5")
    st = flow.load(s.root, P)
    st["status"], st["steps"]["5.4"]["status"] = "failed", "failed"
    flow._save(s.root, st)
    st = flow.retry(s, P, states(s))
    assert st["steps"]["5.4"]["status"] == "done" and st["cursor"] != "5.4", st["steps"]["5.4"]


def _fake_jobs(monkeypatch, result=None, state="succeeded"):
    """jobs.start/status/current without processes: every job ends at once with `result`."""
    from pkf_studio import jobs
    started = []

    def start(root, p, action, args=None, title=""):
        started.append((action, args))
        return f"job{len(started)}"
    monkeypatch.setattr(jobs, "start", start)
    monkeypatch.setattr(jobs, "status", lambda root, jid: {"id": jid, "state": state,
                                                         "result": result or {"problems": [], "severity": None}})
    monkeypatch.setattr(jobs, "current", lambda root, p: None)
    return started


@pytest.mark.parametrize("step_id,values", [("5.2", {"job": "prepare_env"}),
                                            ("4.3", {"job": "test_build", "title": "Run tests & build again"})])
def test_a_job_started_from_a_review_step_finishes(tmp_path, monkeypatch, step_id, values):
    """4.3 Regenerate / Run tests & build again and 5.2 Render & plan again start a job from a *review* step. Before,
    tick() only finished job-kind steps: the step stayed "running" forever (the UI re-ran every 2 s)."""
    s = StubStudio(tmp_path)
    started = _fake_jobs(monkeypatch)
    monkeypatch.setattr(flow, "components_needing_work", lambda s_, p: [])
    st = flow._new_state(P, "dev")
    for x in flow.STEPS:
        if x.id == step_id:
            break
        st["steps"][x.id]["status"] = "done"
    st["steps"]["5.1"]["problems"] = ["the dry run failed: no credentials"] if step_id == "5.2" else []
    st.update(status="waiting_user", cursor=step_id)
    st["steps"][step_id]["status"] = "waiting"
    flow._save(s.root, st)
    st = flow.answer(s, P, states(s), step_id, values)
    assert started and st["steps"][step_id]["status"] == "running"
    st = flow.tick(s, P, states(s))
    assert st["steps"][step_id]["status"] in ("done", "skipped", "waiting"), st["steps"][step_id]
    assert st["cursor"] != step_id or st["status"] in ("waiting_user", "failed"), st


# ------------------------------------------------------------------------------------------------ navigation (0.6.1)
def nav_states(**g):
    """G1–G5 states by keyword (default approved); RUN ready when G5 is approved."""
    rows = []
    for i, gate in enumerate(["G1", "G2", "G3", "G4", "G5"], 1):
        stt = g.get(gate, "approved")
        rows.append(GateState(gate, stt, {"number": i, "merged_at": "x"} if stt in ("approved", "stale") else None,
                              "what this gate approved has changed" if stt == "stale" else ""))
    rows.append(GateState("RUN", "ready" if rows[-1].done else "blocked", None))
    cur = next((r for r in rows if not r.done and r.state != "blocked"), rows[-1])
    return {"gates": rows, "current": cur.gate, "by_gate": {r.gate: r for r in rows}}


class NavStudio(StubStudio):
    def __init__(self, root):
        super().__init__(root)
        self.reopened = []

    def reopen(self, p, gate):
        self.reopened.append(gate)
        return self.root / "wt"


def test_jump_check_follows_the_approval_chain():
    st = nav_states(G4="stale", G5="blocked")
    chk = flow.jump_check(None, P, st, "5.1")
    assert not chk["ok"] and chk["suggest"] == "4.1" and "G4" in chk["problem"] and "Run from 4.1" in chk["problem"]
    chk = flow.jump_check(None, P, nav_states(G5="in_progress"), "3.3")
    assert chk["ok"] and chk["needs_reopen"] == "G3"
    chk = flow.jump_check(None, P, nav_states(G5="in_progress"), "5.1")
    assert chk["ok"] and chk["needs_reopen"] is None
    assert flow.jump_check(None, P, nav_states(G5="in_progress"), "6.1")["blocked_by"] == "G5"


def test_run_from_inside_the_current_stage(tmp_path, monkeypatch):
    s = at_54(tmp_path, monkeypatch)
    started = _fake_jobs(monkeypatch, state="running")
    monkeypatch.setattr(flow, "stage_started", lambda *a: True)
    st = flow.load(s.root, P)
    st["status"], st["steps"]["5.4"]["status"] = "failed", "failed"      # e.g. "not ready: the dry run failed"
    flow._save(s.root, st)
    st = flow.run_from(s, P, nav_states(G5="in_progress"), "5.1")
    assert st["cursor"] == "5.1" and st["steps"]["5.1"]["status"] == "running" and st["steps"]["5.4"]["status"] == "not_started"
    assert st["steps"]["4.4"]["status"] == "done"                                           # earlier steps keep what they did
    assert started == [("prepare_env", {})]                       # the stage already started: render & plan again, not start_g5
    assert any(e["type"] == "run_from" for e in __import__("pkf_studio").flowstore.events(s.root, P))


def test_run_from_an_approved_stage_needs_reopen(tmp_path, monkeypatch):
    s = NavStudio(tmp_path)
    _fake_jobs(monkeypatch, state="running")
    monkeypatch.setattr(flow, "stage_started", lambda *a: True)
    with pytest.raises(StudioError, match="reopens G3"):
        flow.run_from(s, P, nav_states(G5="in_progress"), "3.3")
    assert s.reopened == []
    st = flow.run_from(s, P, nav_states(G5="in_progress"), "3.3", reopen=True, derive=lambda: nav_states(G3="in_progress", G4="blocked", G5="blocked"))
    assert s.reopened == ["G3"] and st["cursor"] == "3.3" and st["status"] == "running"


def test_run_from_refuses_a_stage_whose_earlier_gate_is_not_approved(tmp_path, monkeypatch):
    s = NavStudio(tmp_path)
    _fake_jobs(monkeypatch)
    with pytest.raises(StudioError, match="G4 .* must be approved first — Run from 4.1"):
        flow.run_from(s, P, nav_states(G4="stale", G5="blocked"), "5.1")


def test_run_from_refused_while_a_job_runs(tmp_path, monkeypatch):
    s = NavStudio(tmp_path)
    from pkf_studio import jobs
    monkeypatch.setattr(jobs, "current", lambda root, p: "job9")
    with pytest.raises(StudioError, match="a job is running"):
        flow.run_from(s, P, nav_states(G5="in_progress"), "5.1")


def test_stop_after_a_stage(tmp_path, monkeypatch):
    """Run only S5: the run stops when it reaches S6, and says where Resume continues."""
    s = at_54(tmp_path, monkeypatch)
    _fake_jobs(monkeypatch)
    monkeypatch.setattr(flow, "stage_started", lambda *a: True)
    st = flow.run_from(s, P, nav_states(G5="in_progress"), "5.3", stop_after="S5")
    assert st["cursor"] == "5.3" and st["status"] == "waiting_user"
    st = flow.answer(s, P, nav_states(G5="approved"), "5.3", {})     # G5 approved (as by the approver on GitHub)
    assert st["steps"]["5.4"]["status"] == "done" and st["status"] == "stopped" and st["cursor"] == "6.1", st
    assert "stopped after S5" in st["message"] and "Resume continues at 6.1" in st["message"]
    with pytest.raises(StudioError, match="before 5.1"):
        flow.run_from(s, P, nav_states(G5="in_progress"), "5.1", stop_after="S4")


def test_reopen_stage_restarts_the_stage(tmp_path, monkeypatch):
    s = NavStudio(tmp_path)
    started = _fake_jobs(monkeypatch, state="running")
    monkeypatch.setattr(flow, "stage_started", lambda *a: True)
    st = flow.reopen_stage(s, P, nav_states(), "G5", derive=lambda: nav_states(G5="in_progress"))
    assert s.reopened == ["G5"] and st["cursor"] == "5.1" and started == [("prepare_env", {})]
    s2 = NavStudio(tmp_path / "two")
    st = flow.reopen_stage(s2, P, nav_states(), "G2", derive=lambda: nav_states(G2="in_progress", G3="blocked", G4="blocked", G5="blocked"))
    assert st["steps"]["2.1"]["status"] == "done" and st["cursor"] == "2.2" and st["status"] == "waiting_user"
    st = flow.reopen_stage(NavStudio(tmp_path / "three"), P, nav_states(), "G1",
                           derive=lambda: nav_states(G1="in_progress", G2="blocked", G3="blocked", G4="blocked", G5="blocked"))
    assert st["cursor"] == "1.4"


def test_merged_but_stale_points_at_reopen(tmp_path, monkeypatch):
    s = at_54(tmp_path, monkeypatch)
    drive(s, 1)
    s._merged("G5")
    st = drive(s, 1, g5="stale", note="what this gate approved has changed on the base branch")
    assert st["steps"]["5.4"]["reopen"] == "G5" and st["steps"]["5.4"]["gate_state"] == "stale"


def test_not_ready_points_at_the_step_that_fixes_it(tmp_path, monkeypatch):
    s = at_54(tmp_path, monkeypatch)
    monkeypatch.setattr(flow, "_readiness", lambda s_, p, g: ["the dry run failed or is missing (AWS credentials?): fix it, then Render & plan again"])
    st = drive(s, 1)
    row = st["steps"]["5.4"]
    assert st["status"] == "failed" and row["fix_step"] == "5.2" and "Run from 5.2" in row["detail"], row


# ------------------------------------------------------------------------------------------------ review findings
def test_a_temporary_blocker_then_the_request_is_sent(tmp_path, monkeypatch):
    """Review finding 1: entering 5.4 while G4 is 'unknown' (a fetch failed) must not leave the step waiting for ever
    once G4 clears — the run requests the PR then."""
    s = at_54(tmp_path, monkeypatch)
    blocked = nav_states(G4="unknown", G5="blocked")
    blocked["by_gate"]["G4"].note = "the base branch could not be fetched"
    st = flow.tick(s, P, blocked)
    assert st["steps"]["5.4"]["status"] == "waiting" and "G4" in st["steps"]["5.4"]["detail"] and not s.prs_by_gate
    st = flow.tick(s, P, blocked)
    assert not s.prs_by_gate
    st = flow.tick(s, P, states(s))                  # G4 approved again: G5 in progress → the request goes out
    assert s.prs_by_gate.get("G5") and st["steps"]["5.4"]["status"] == "waiting" and st["status"] == "waiting_gate", st["steps"]["5.4"]


def test_merged_on_github_is_fetched_before_it_is_called_stale(tmp_path, monkeypatch):
    """Review finding 2: derive() reads the last fetch; a PR merged by hand on GitHub looks stale until the merge is
    fetched. The run fetches and derives once more — approved → done, not "Reopen G5"."""
    s = at_54(tmp_path, monkeypatch)
    drive(s, 1)
    s._merged("G5")
    fetched = []
    s.fetch = lambda force=False: fetched.append(force)
    monkeypatch.setattr(flow, "_derive", lambda s_, p: states(s, "approved"))
    st = drive(s, 1, g5="stale", note="what this gate approved has changed on the base branch")
    assert fetched == [True] and st["steps"]["5.4"]["status"] == "done" and st["cursor"] != "5.4", st["steps"]["5.4"]


def test_retry_after_a_failed_check_reads_the_new_head(tmp_path, monkeypatch):
    """Review finding 10: after a failed pkf-gate check the user pushes a fix and clicks Retry: the run approves and
    checks the NEW head (0.6.1 first draft re-checked the old head for ever)."""
    s = at_54(tmp_path, monkeypatch, strict=True)
    drive(s, 1)
    s.passed, s.check = True, "failure"
    st = drive(s, 2, g5="waiting")
    assert st["status"] == "failed"
    checked = []
    s.gate_check = lambda sha: checked.append(sha) or "success"
    s.check = "success"
    s.head = "c" * 40
    s.prs_by_gate["G5"]["head"] = {"sha": s.head}
    st = flow.retry(s, P, states(s))
    st = drive(s, 2, g5="waiting")
    assert st["steps"]["5.4"]["status"] == "done", st["steps"]["5.4"]
    assert checked and checked[-1] == "c" * 40


def test_a_missing_check_is_waited_for_then_reported(tmp_path, monkeypatch):
    s = at_54(tmp_path, monkeypatch, strict=True)
    drive(s, 1)
    s.passed, s.check = True, "missing"
    st = drive(s, 3, g5="waiting")
    assert st["status"] == "waiting_gate" and "missing" in st["steps"]["5.4"]["detail"]
    st = drive(s, flow.CHECK_MISSING_MAX + 2, g5="waiting")
    assert st["status"] == "failed" and "missing" in st["steps"]["5.4"]["detail"], st["steps"]["5.4"]
