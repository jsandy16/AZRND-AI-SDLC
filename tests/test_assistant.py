"""The assistant without the model: the path guard (reads inside the allowed roots, writes inside the worktree minus the
protected files), the tools, the diagnose → fix → apply protocol with a scripted session, and the `assist` job action."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from pkf_studio import flowstore
from pkf_studio.assistant import protocol, session, tools as T


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def ctx(tmp_path):
    root = tmp_path / "repo"
    wt = root / ".pkf-studio" / "worktrees" / "p1"
    wt.mkdir(parents=True)
    _git(wt, "init", "-q")
    _git(wt, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "base")
    (wt / "destinations").mkdir()
    (wt / "destinations" / "d.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (wt / "destinations" / "d.lock.yaml").write_text("plan: {}\n", encoding="utf-8")
    (wt / "dags").mkdir()
    (wt / "dags" / "x.py").write_text("# dag\n", encoding="utf-8")
    (wt / ".pkf-studio").mkdir()
    (wt / ".pkf-studio" / "project.yaml").write_text("mode: dev\n", encoding="utf-8")
    # 0.8.0: the pipeline's spec is committed, so its own files (destination d) are writable; before it is, only
    # requirements/p1/, pipelines/p1.yaml and review/p1/ are (tests/test_assistant_v08.py)
    (wt / "pipelines").mkdir()
    (wt / "pipelines" / "p1.yaml").write_text("name: p1\nconnection: c1\n", encoding="utf-8")
    (wt / "connections").mkdir()
    (wt / "connections" / "c1.yaml").write_text("destination: d\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "files")
    fw = tmp_path / "framework" / "src"
    (fw / "pkf").mkdir(parents=True)
    (fw / "pkf" / "cli.py").write_text("print('hi')\n", encoding="utf-8")
    jobs = root / ".pkf-studio" / "jobs" / "j1"
    jobs.mkdir(parents=True)
    (jobs / "log").write_text("== analyze\n  error: boom\n== failed: boom\n", encoding="utf-8")
    (jobs / "cmd.json").write_text(json.dumps({"action": "analyze", "pipeline": "p1", "args": {}, "title": "t", "started_at": "2026-01-01T00:00:00+00:00"}))
    (jobs / "exit").write_text("1")
    (jobs / "result.json").write_text(json.dumps({"ok": False, "error": "boom", "problems": ["pkf analyze: boom"]}))
    (tmp_path / "outside.txt").write_text("secret\n")
    return T.Context(root=root, pipeline="p1", worktree=wt, framework_dir=fw, job_id="j1", step_id="1.3", gates={"G1": {"state": "in_progress"}},
                     flow={"status": "failed", "cursor": "1.3"}, events=[{"type": "step_failed", "step": "1.3"}])


def test_read_guard(ctx, tmp_path):
    assert T.check_read(ctx, "destinations/d.py").name == "d.py"
    assert T.check_read(ctx, ctx.framework_dir / "pkf" / "cli.py")
    assert T.check_read(ctx, ctx.jobs_dir / "j1" / "log")
    with pytest.raises(T.PathDenied):
        T.check_read(ctx, tmp_path / "outside.txt")
    with pytest.raises(T.PathDenied):
        T.check_read(ctx, "../../../outside.txt")


@pytest.mark.parametrize("rel", ["dags/x.py", ".pkf-studio/project.yaml", "destinations/d.lock.yaml", "CODEOWNERS", ".github/w.yml", "tools/x.whl",
                                 "../../../outside.txt", "x.lock.json"])
def test_write_guard_denies(ctx, rel):
    with pytest.raises(T.PathDenied):
        T.check_write(ctx, rel)


def test_write_guard_allows_component_code(ctx):
    assert T.check_write(ctx, "destinations/d.py").name == "d.py"
    assert T.check_write(ctx, "destinations/d.yaml").name == "d.yaml"
    with pytest.raises(T.PathDenied):           # 0.8.0: a file that is not one of the pipeline's own (was allowed by a gap)
        T.check_write(ctx, "destinations/new.py")


def test_tools_read_grep_context(ctx):
    text = T.read_file(ctx, "destinations/d.py")
    assert text.splitlines()[0].strip().startswith("1") and "def f" in text
    assert any("d.py" in x for x in T.grep(ctx, "def f", "worktree", "**/*.py"))
    assert "error: boom" in T.read_job_log(ctx)
    c = T.pkf_context(ctx)
    assert c["pipeline"] == "p1" and c["failed_step"]["id"] == "1.3" and c["failed_step"]["job"]["problems"] == ["pkf analyze: boom"]
    assert c["gates"]["G1"]["state"] == "in_progress" and c["recent_events"][0]["type"] == "step_failed"
    long = T._clip("x" * 20000)
    assert len(long) < 13000 and "truncated" in long.lower()
    with pytest.raises(T.PathDenied):
        T.edit_file(ctx, "dags/x.py", "# dag", "# x")
    assert "ok" in T.edit_file(ctx, "destinations/d.py", "return 1", "return 2").lower() or (ctx.worktree / "destinations/d.py").read_text().count("return 2")
    assert "return 2" in T.git_diff(ctx)


def _hook_result(coro):
    import asyncio

    return asyncio.run(coro)


def test_pretooluse_hook_fences_reads_and_writes(ctx, tmp_path):
    tr = session.Transcript(tmp_path / "t.jsonl")
    hook = session.make_hook(ctx, {"Read", "Grep", "Glob", "Edit"}, write=False, transcript=tr)
    ok = _hook_result(hook({"tool_name": "Read", "tool_input": {"file_path": str(ctx.worktree / "destinations/d.py")}}, "1", None))
    assert ok == {}
    denied = _hook_result(hook({"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "outside.txt")}}, "1", None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    denied = _hook_result(hook({"tool_name": "Edit", "tool_input": {"file_path": str(ctx.worktree / "destinations/d.py")}}, "1", None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"           # diagnose: no writes
    denied = _hook_result(hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "1", None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    hook_w = session.make_hook(ctx, {"Read", "Edit", "Write"}, write=True, transcript=tr)
    assert _hook_result(hook_w({"tool_name": "Edit", "tool_input": {"file_path": str(ctx.worktree / "destinations/d.py")}}, "1", None)) == {}
    denied = _hook_result(hook_w({"tool_name": "Write", "tool_input": {"file_path": str(ctx.worktree / "dags/x.py")}}, "1", None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    tr.close()
    rows = [json.loads(x) for x in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert sum(r["kind"] == "tool_denied" for r in rows) == 4


def test_settings_env(monkeypatch):
    monkeypatch.setenv("PKF_LLM_MAX_TURNS_ASSISTANT_FIX", "7")
    monkeypatch.setenv("PKF_LLM_MODEL_ASSISTANT", "claude-x")
    assert session.settings("fix")["max_turns"] == 7 and session.settings("fix")["model"] == "claude-x"
    assert session.settings("diagnose")["max_turns"] == 30


class FakeStudio:
    def __init__(self, root: Path, wt: Path):
        self.root, self._wt = root, wt

    def worktree_path(self, p):
        return self._wt

    def commit(self, p, message):
        _git(self._wt, "add", "-A")
        _git(self._wt, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", message)
        return _git(self._wt, "rev-parse", "HEAD")


def test_protocol_diagnose_fix_apply(ctx, monkeypatch):
    calls = []

    def fake_run(phase, c, prompt, schema, transcript_path, resume=None):
        calls.append((phase, resume))
        session.Transcript(transcript_path).write("text", f"fake {phase}")
        if phase == "diagnose":
            assert "pkf analyze: boom" in prompt and schema is protocol.DIAGNOSIS_SCHEMA
            return ({"root_cause": "d.py returns 1", "evidence": ["destinations/d.py:2"], "fix_kind": "edit_files", "proposal": "return 2",
                     "files": ["destinations/d.py"], "studio_action": None, "user_action": None, "needs_reopen": None, "risk": "low",
                     "confidence": 0.9}, "sdk-1", {"cost_usd": 0.01, "turns": 3})
        if phase == "fix":
            assert resume == "sdk-1" and "return 2" in prompt
            T.edit_file(c, "destinations/d.py", "return 1", "return 2")
            return ({"summary": "return 2 instead of 1", "files_changed": ["destinations/d.py"], "tests": {"ran": True, "ok": True, "detail": "1 case"},
                     "notes": ""}, "sdk-1", {"cost_usd": 0.02, "turns": 5})
        return ("the answer", "sdk-2", {})

    monkeypatch.setattr(session, "run_session", fake_run)
    diag = protocol.diagnose(ctx)
    sid = diag["session_id"]
    d = flowstore.assistant_dir(ctx.root, "p1", sid)
    assert (d / "diagnosis.json").exists() and json.loads((d / "diagnosis.json").read_text())["fix_kind"] == "edit_files"
    fix = protocol.prepare_fix(ctx, sid)
    assert "return 2" in fix["diff"] and fix["files_changed"] == ["destinations/d.py"] and (d / "fix.json").exists()
    assert _git(ctx.worktree, "status", "--porcelain")              # not committed
    studio = FakeStudio(ctx.root, ctx.worktree)
    sha = protocol.apply_fix(studio, "p1", sid)
    assert sha and _git(ctx.worktree, "log", "-1", "--format=%s") == "assistant: return 2 instead of 1"
    assert json.loads((d / "summary.json").read_text())["applied"] is True
    ans = protocol.ask(ctx, "why?", session_id=sid)
    assert ans["answer"] == "the answer" and calls[-1] == ("ask", "sdk-1")
    types = [e["type"] for e in flowstore.events(ctx.root, "p1")]
    for t in ("assistant_started", "assistant_diagnosis", "assistant_fix_proposed", "assistant_fix_applied", "assistant_answer"):
        assert t in types, types


def test_protocol_discard_restores_the_worktree(ctx, monkeypatch):
    def fake_run(phase, c, prompt, schema, transcript_path, resume=None):
        if phase == "diagnose":
            return ({"root_cause": "x", "evidence": [], "fix_kind": "edit_files", "proposal": "p", "files": ["destinations/d.py"], "risk": "low",
                     "confidence": 0.5}, "s", {})
        T.edit_file(c, "destinations/d.py", "return 1", "return 3")
        T.write_file(c, "review/p1/extra.md", "x = 1\n")      # a new file of the pipeline's own (0.8.0: was destinations/extra.py)
        return ({"summary": "s", "files_changed": ["destinations/d.py", "review/p1/extra.md"], "tests": {"ran": False, "ok": False, "detail": "n/a"},
                 "notes": ""}, "s", {})
    monkeypatch.setattr(session, "run_session", fake_run)
    sid = protocol.diagnose(ctx)["session_id"]
    protocol.prepare_fix(ctx, sid)
    assert (ctx.worktree / "review/p1/extra.md").exists()
    protocol.discard_fix(FakeStudio(ctx.root, ctx.worktree), "p1", sid)
    assert "return 1" in (ctx.worktree / "destinations/d.py").read_text() and not (ctx.worktree / "review/p1/extra.md").exists()
    assert not _git(ctx.worktree, "status", "--porcelain")


def test_llm_failure_is_a_problem_not_a_crash(ctx, monkeypatch):
    def boom(phase, c, prompt, schema, transcript_path, resume=None):
        raise RuntimeError("the assistant timed out after 900s")
    monkeypatch.setattr(session, "run_session", boom)
    out = protocol.diagnose(ctx)
    assert out["error"].startswith("the assistant timed out") and out["session_id"]
    assert any(e["type"] == "assistant_failed" for e in flowstore.events(ctx.root, "p1"))


def test_assist_action_runs_inline(tmp_path, monkeypatch):
    """The job action, through jobs.start (inline), with the protocol scripted."""
    from fakegithub import FakeGitHub, make_repo
    from pkf_studio import jobs, teamsetup
    from pkf_studio.service import Studio
    from test_studio_github import setup_project, write_g1

    bare, clone = make_repo(tmp_path)
    fake = FakeGitHub(bare)
    fake.add_user("dev1", "dev@example.com", token="tok-dev1")
    for k, v in {"GITHUB_TOKEN": "tok-dev1", "PKF_STUDIO_INLINE_JOBS": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(teamsetup, "build_wheel", lambda dest, log=print: None)
    s = setup_project(fake, clone)
    write_g1(s, "p1")
    seen = {}

    def fake_diag(c, question=None, session_id=None, log=None):
        seen["ctx"] = c
        return {"session_id": "sess-1", "root_cause": "rc", "fix_kind": "rerun"}
    monkeypatch.setattr(protocol, "diagnose", fake_diag)
    jid = jobs.start(s.root, "p1", "assist", {"phase": "diagnose", "step_id": "1.3"}, "assist")
    stt = jobs.status(s.root, jid)
    assert stt["state"] == "succeeded", jobs.log(s.root, jid)
    assert stt["result"]["session_id"] == "sess-1" and stt["result"]["problems"] == []
    assert seen["ctx"].pipeline == "p1" and seen["ctx"].worktree == s.worktree_path("p1") and "G1" in seen["ctx"].gates
    assert (Path(seen["ctx"].framework_dir) / "pkf" / "cli.py").exists()
    fake.close()


# ------------------------------------------------------------------------------------------------ 0.6.1 guard
def _commit_all(wt):
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "specs")


def _two_pipelines(wt):
    (wt / "pipelines").mkdir(exist_ok=True)
    (wt / "connections").mkdir(exist_ok=True)
    (wt / "sources").mkdir(exist_ok=True)
    (wt / "pipelines" / "p1.yaml").write_text("name: p1\nconnection: c1\n")
    (wt / "connections" / "c1.yaml").write_text("source: s1\ndestination: d\n")
    (wt / "pipelines" / "p2.yaml").write_text("name: p2\nconnection: c2\n")
    (wt / "connections" / "c2.yaml").write_text("source: s2\ndestination: d2\n")
    (wt / "sources" / "s1.yaml").write_text("name: s1\n")
    (wt / "sources" / "s2.yaml").write_text("name: s2\n")
    _commit_all(wt)


def test_write_guard_keeps_to_this_pipeline(ctx):
    wt = ctx.worktree
    _two_pipelines(wt)
    assert T.check_write(ctx, "sources/s1.yaml")                       # its own source
    for rel in ("sources/s2.yaml", "pipelines/p2.yaml", "connections/c2.yaml", "review/p2/x.md"):
        with pytest.raises(T.PathDenied):
            T.check_write(ctx, rel)


def test_write_guard_cannot_be_widened_by_the_agents_own_edits(ctx):
    """Review finding 7: pointing the (uncommitted) connection at another pipeline's source does not make it writable."""
    wt = ctx.worktree
    _two_pipelines(wt)
    (wt / "connections" / "c1.yaml").write_text("source: s2\ndestination: d\n")
    with pytest.raises(T.PathDenied):
        T.check_write(ctx, "sources/s2.yaml")


def test_write_guard_never_writes_a_shared_component(ctx):
    """A component two pipelines use: a change would reach the other pipeline too."""
    wt = ctx.worktree
    _two_pipelines(wt)
    (wt / "connections" / "c2.yaml").write_text("source: s1\ndestination: d2\n")
    _commit_all(wt)
    with pytest.raises(T.PathDenied, match="another pipeline"):
        T.check_write(ctx, "sources/s1.yaml")


@pytest.mark.parametrize("rel", ["src/pkf/cli.py", "SRC/pkf/cli.py", "pkf_studio/flow.py", "steps-plugin/skills/x/SKILL.md", "pyproject.toml",
                                 "PyProject.toml"])
def test_write_guard_never_writes_framework_code(ctx, rel):
    with pytest.raises(T.PathDenied, match="framework"):
        T.check_write(ctx, rel)


def test_write_guard_never_writes_into_the_framework_source(ctx, tmp_path):
    """Even when the framework source sits inside the worktree (a copy checked into the repository)."""
    inner = ctx.worktree / "vendor" / "fw"
    (inner / "pkf").mkdir(parents=True)
    (inner / "pkf" / "cli.py").write_text("x\n")
    c2 = T.Context(root=ctx.root, pipeline="p1", worktree=ctx.worktree, framework_dir=inner)
    with pytest.raises(T.PathDenied, match="framework"):
        T.check_write(c2, "vendor/fw/pkf/cli.py")
