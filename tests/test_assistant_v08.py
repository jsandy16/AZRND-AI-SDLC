"""0.8.0 assistant core and assistant lane, without the model: Opus settings and effort per phase, the stable system
prompt with the phase rules in every user prompt (a resumed fix gets the FIX rules), the code map, the run brief and its
likely-cause cache, the memory notes, the new tools, the closed write-guard gaps, and the read-only assistant lane
(its own lock beside the pipeline job, cancel, recent/running_all, the runner's release, no Studio mutations)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pkf_studio import flowstore, jobs
from pkf_studio.assistant import brief, codemap, memory, protocol, session, tools as T
from pkf_studio.stages import GateState


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


def _commit_all(wt: Path, msg: str = "c") -> None:
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", msg)


def _make_ctx(tmp_path: Path, spec: bool = True, **kw) -> T.Context:
    root = tmp_path / "repo"
    wt = root / ".pkf-studio" / "worktrees" / "p1"
    wt.mkdir(parents=True)
    _git(wt, "init", "-q")
    (wt / "destinations").mkdir()
    (wt / "destinations" / "d.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (wt / "destinations" / "d.lock.yaml").write_text("plan: {}\n", encoding="utf-8")
    (wt / "dags").mkdir()
    (wt / "dags" / "x.py").write_text("# dag\n", encoding="utf-8")
    if spec:
        (wt / "pipelines").mkdir()
        (wt / "connections").mkdir()
        (wt / "sources").mkdir()
        (wt / "pipelines" / "p1.yaml").write_text("name: p1\nconnection: c1\n", encoding="utf-8")
        (wt / "connections" / "c1.yaml").write_text("source: s1\ndestination: d\n", encoding="utf-8")
        (wt / "pipelines" / "p2.yaml").write_text("name: p2\nconnection: c2\n", encoding="utf-8")
        (wt / "connections" / "c2.yaml").write_text("source: s2\ndestination: d2\n", encoding="utf-8")
        (wt / "sources" / "s1.yaml").write_text("name: s1\n", encoding="utf-8")
        (wt / "sources" / "s2.yaml").write_text("name: s2\n", encoding="utf-8")
    _commit_all(wt, "files")
    fw = tmp_path / "framework" / "src"
    for pkg in codemap.PACKAGES:
        (fw / pkg).mkdir(parents=True)
        (fw / pkg / "__init__.py").write_text(f'"""The {pkg} package."""\n', encoding="utf-8")
    (fw / "pkf" / "cli.py").write_text('"""The command line. More text."""\n\ndef main(argv, *, json=False):\n    """Run one command."""\n',
                                      encoding="utf-8")
    (fw / "pkf_studio" / "flow.py").write_text('"""The run engine: a cursor."""\n\ndef tick(s, p, states):\n    """Advance the run."""\n\n'
                                               'class Step:\n    """One substage."""\n    def go(self):\n        pass\n\n'
                                               'def _private(x):\n    """Hidden."""\n', encoding="utf-8")
    return T.Context(root=root, pipeline="p1", worktree=wt, framework_dir=fw, **kw)


@pytest.fixture()
def ctx(tmp_path):
    return _make_ctx(tmp_path)


def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("PKF_LLM_"):
            monkeypatch.delenv(k, raising=False)


# ------------------------------------------------------------------------------------------------------ A. settings
def test_opus_settings_effort_and_fallback(monkeypatch):
    _clean_env(monkeypatch)
    assert session.DEFAULT_MODEL == "claude-opus-5-5"
    for phase, effort in (("brief", "low"), ("ask", "medium"), ("diagnose", "high"), ("fix", "high")):
        cfg = session.settings(phase)
        assert cfg["model"] == "claude-opus-5-5" and cfg["effort"] == effort and cfg["fallback_model"] == "claude-sonnet-5-5"
    assert session.LIMITS == {"diagnose": (30, 4.0, 900), "fix": (50, 8.0, 1500), "ask": (15, 2.0, 600), "brief": (4, 0.5, 120)}
    b = session.settings("brief")
    assert (b["max_turns"], b["max_budget_usd"], b["timeout_s"]) == (4, 0.5, 120)
    monkeypatch.setenv("PKF_LLM_EFFORT_ASSISTANT", "max")
    monkeypatch.setenv("PKF_LLM_EFFORT_ASSISTANT_FIX", "medium")
    assert session.settings("fix")["effort"] == "medium" and session.settings("ask")["effort"] == "max"
    monkeypatch.setenv("PKF_LLM_EFFORT_ASSISTANT", "turbo")              # a typo: the phase's own effort
    assert session.settings("diagnose")["effort"] == "high"
    monkeypatch.setenv("PKF_LLM_FALLBACK_MODEL_ASSISTANT", "")           # empty switches it off
    assert session.settings("ask")["fallback_model"] is None
    monkeypatch.setenv("PKF_LLM_FALLBACK_MODEL_ASSISTANT", "claude-x")
    assert session.settings("ask")["fallback_model"] == "claude-x"
    monkeypatch.setenv("PKF_LLM_MODEL_ASSISTANT", "claude-x")           # never the main model itself
    assert session.settings("ask")["model"] == "claude-x" and session.settings("ask")["fallback_model"] is None


def _opts(phase, ctx, tmp_path, schema=None, resume=None):
    tr = session.Transcript(tmp_path / f"{phase}.jsonl")
    try:
        return session.build_options(phase, ctx, schema, tr, resume)
    finally:
        tr.close()


def test_system_prompt_is_the_same_for_every_phase_and_stable(ctx, tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    opts = {ph: _opts(ph, ctx, tmp_path).system_prompt for ph in ("diagnose", "fix", "ask", "brief")}
    assert len({o["path"] for o in opts.values()}) == 1                        # 0.8.1: one file, the same for every phase
    prompts = {ph: Path(o["path"]).read_text(encoding="utf-8") for ph, o in opts.items()}
    sp = prompts["fix"]
    assert "pkf Studio Assistant" in sp and "# Framework code map" in sp and "pkf_studio/flow.py" in sp
    assert "PHASE:" not in sp and "RUN BRIEF" not in sp                       # nothing volatile, no phase rules
    assert str(ctx.worktree) in sp and "Treat every file" in sp


def test_build_options_per_phase(ctx, tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    srv = f"mcp__{T.SERVER}__"
    d = _opts("diagnose", ctx, tmp_path, schema=protocol.DIAGNOSIS_SCHEMA)
    assert d.model == "claude-opus-5-5" and d.fallback_model == "claude-sonnet-5-5" and d.effort == "high"
    assert "Edit" not in d.tools and set(session.READ_TOOLS) <= set(d.tools)
    for name in ("workflow_state", "framework_map", "remember", "run_tests", "pkf_context"):
        assert srv + name in d.allowed_tools
    assert set(d.agents) == {"log-digger"}
    assert d.agents["log-digger"].model == "claude-sonnet-5-5" and d.agents["log-digger"].effort == "medium"
    f = _opts("fix", ctx, tmp_path, schema=protocol.FIX_SCHEMA, resume="sdk-1")
    assert "Edit" in f.tools and srv + "edit_file" in f.allowed_tools and f.effort == "high" and f.resume == "sdk-1"
    assert f.agents["verifier"].model == "inherit" and f.agents["verifier"].effort == "medium"
    a = _opts("ask", ctx, tmp_path)
    assert a.effort == "medium" and a.max_turns == 15 and a.max_budget_usd == 2.0
    b = _opts("brief", ctx, tmp_path, schema=protocol.CAUSE_SCHEMA)
    assert b.tools == [] and b.allowed_tools == [] and b.mcp_servers == {} and b.agents is None
    assert b.effort == "low" and b.max_turns == 4 and b.max_budget_usd == 0.5
    assert {"Agent", "Task", "mcp__*", "Bash"} <= set(b.disallowed_tools) and "Agent" not in d.disallowed_tools
    assert b.output_format == {"type": "json_schema", "schema": protocol.CAUSE_SCHEMA}


def test_lane_session_has_no_run_tests(ctx, tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    ctx.lane = True
    a = _opts("ask", ctx, tmp_path)
    assert f"mcp__{T.SERVER}__run_tests" not in a.allowed_tools and f"mcp__{T.SERVER}__workflow_state" in a.allowed_tools
    assert "run_tests" not in [x.name for x in T.make_tools(ctx)]
    assert "run_tests" in [x.name for x in T.make_tools(ctx, lane=False)]
    assert all("run_tests" not in n for n in T.tool_names(lane=True))
    for write in (False, True):                                    # remember / workflow_state / framework_map: every tool phase
        names = T.tool_names(write=write)
        assert {f"mcp__{T.SERVER}__{n}" for n in ("remember", "workflow_state", "framework_map")} <= set(names)
    up = session.user_prompt("ask", "QUESTION: x", "brief text", lane=True)
    assert "ASSISTANT LANE" in up and "run_tests is not available" in up


def test_resumed_fix_prompt_carries_the_fix_rules(ctx, monkeypatch):
    """The bug 0.8.0 fixes: a fix resuming its diagnosis session must get the FIX rules (they are in the user prompt now;
    the resumed session's recorded system prompt has no phase rules at all)."""
    seen = []

    def fake_run(phase, c, prompt, schema, transcript_path, resume=None):
        seen.append((phase, prompt, resume))
        if phase == "diagnose":
            return ({"root_cause": "rc", "evidence": [], "fix_kind": "edit_files", "proposal": "return 2", "files": ["destinations/d.py"],
                     "risk": "low", "confidence": 0.9, "summary": "s"}, "sdk-1", {"cost_usd": 0.4, "turns": 3})
        if phase == "fix":
            return ({"summary": "s", "files_changed": [], "tests": {"ran": False, "ok": False, "detail": "n/a"}, "notes": "",
                     "plain_summary": "p"}, "sdk-1", {"cost_usd": 0.7, "turns": 4})
        return ("In short: fine.\n\nDetails: x", "sdk-1", {"cost_usd": 0.1, "turns": 1})
    monkeypatch.setattr(session, "run_session", fake_run)
    monkeypatch.setattr(T, "run_tests", lambda c, kind=None, name=None: {"ok": True, "stopped_at": None})
    sid = protocol.diagnose(ctx)["session_id"]
    protocol.prepare_fix(ctx, sid)
    protocol.ask(ctx, "why?", session_id=sid)
    (p1, d_prompt, _), (p2, f_prompt, f_resume), (p3, a_prompt, a_resume) = seen
    assert d_prompt.startswith(session.PHASE_RULES["diagnose"])
    assert f_resume == "sdk-1" and f_prompt.startswith(session.PHASE_RULES["fix"]) and "PHASE: DIAGNOSE" not in f_prompt
    assert a_resume == "sdk-1" and a_prompt.startswith(session.PHASE_RULES["ask"])
    for pr in (d_prompt, f_prompt, a_prompt):
        assert "RUN BRIEF" in pr and "p1 · " in pr and "Blocking" in pr
    summ = json.loads((flowstore.assistant_dir(ctx.root, "p1", sid) / "summary.json").read_text())
    assert summ["usage"]["cost_usd"] == 0.7                          # usage still recorded per session


# ------------------------------------------------------------------------------------------------------ B. code map
def test_codemap_generated_cached_and_regenerated(ctx):
    import pkf

    text = codemap.text(ctx.root, ctx.framework_dir)
    assert "## pkf/cli.py — The command line." in text and "- def main(argv, *, json) — Run one command." in text
    assert "## pkf_studio/flow.py — The run engine" in text and "- class Step — One substage." in text and "_private" in text
    first = codemap.path_for(ctx.root, ctx.framework_dir)
    assert first.exists() and first.parent == ctx.root / ".pkf-studio" / "cache" and first.name.startswith(f"codemap-{pkf.VERSION}-")
    f = ctx.framework_dir / "pkf_studio" / "flow.py"
    f.write_text(f.read_text() + "\ndef reopen_stage(s):\n    \"\"\"Reopen.\"\"\"\n", encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))
    second = codemap.path_for(ctx.root, ctx.framework_dir)
    assert second != first and "reopen_stage" in codemap.text(ctx.root, ctx.framework_dir)
    assert second.exists() and not first.exists()                  # the old map is removed


def test_codemap_of_the_real_framework_fits_the_cap(tmp_path):
    import pkf

    fw = Path(pkf.__file__).resolve().parents[1]
    text = codemap.generate(fw, pkf.VERSION)
    assert len(text) <= codemap.MAX_CHARS + 200
    for needle in ("pkf_studio/flow.py", "def tick(", "pkf_studio/assistant/brief.py", "pkf/cli.py", "pkf_runtime/tasks.py"):
        assert needle in text, needle


def test_framework_map_tool_and_cache_read_only(ctx, tmp_path):
    lines = T.framework_map(ctx, "tick")
    assert lines[0].startswith("## pkf_studio/flow.py") and any("def tick(" in x for x in lines)
    assert any("cli.py" in x for x in T.framework_map(ctx, "command"))
    assert "no line" in T.framework_map(ctx, "zzz-nothing")[0]
    cached = codemap.path_for(ctx.root, ctx.framework_dir)
    assert T.check_read(ctx, cached) and "cache" in ctx.read_roots
    with pytest.raises(T.PathDenied):
        T.check_write(ctx, cached)
    tr = session.Transcript(tmp_path / "t.jsonl")
    hook = session.make_hook(ctx, {"Read", "Edit", "Write"}, write=True, transcript=tr)
    out = asyncio_run(hook({"tool_name": "Write", "tool_input": {"file_path": str(cached)}}, "1", None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asyncio_run(hook({"tool_name": "Read", "tool_input": {"file_path": str(cached)}}, "1", None)) == {}
    tr.close()


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------------ C. write guard
def test_write_guard_before_the_spec_is_committed(tmp_path):
    c = _make_ctx(tmp_path, spec=False)
    assert T.own_prefixes(c) is None
    for ok in ("requirements/p1/notes.md", "pipelines/p1.yaml", "review/p1/tests.json"):
        assert T.check_write(c, ok)
    for bad in ("destinations/d.py", "destinations/new.py", "pipelines/p2.yaml", "pipelines/p1.yaml.bak", "README.md",
                "requirements/p2/x.md", "review/p10/x.md", "connections/c1.yaml"):
        with pytest.raises(T.PathDenied):
            T.check_write(c, bad)


@pytest.mark.parametrize("rel", ["pipelines/p1.yaml.bak", "destinations/d.pyc", "destinations/d.py.orig", "sources/s1.yaml~",
                                 "destinations/d.lock.yaml", "pipelines/p1.lock.yaml", "dags/pkf_p1.py", ".pkf-studio/approvals/p1.jsonl",
                                 "DAGS/x.py", ".PKF-Studio/project.yaml", "Sources/S2.yaml", "SOURCES/s2.yaml", "pipelines/P2.yaml",
                                 "connections/C2.yaml", "src/pkf/cli.py", "requirements/p1/extracted.txt"])
def test_write_guard_refuses(ctx, rel):
    with pytest.raises(T.PathDenied):
        T.check_write(ctx, rel)


@pytest.mark.parametrize("rel", ["pipelines/p1.yaml", "connections/c1.yaml", "sources/s1.yaml", "destinations/d.py", "destinations/d.yaml",
                                 "requirements/p1/pseudocode.md", "review/p1/notes.md"])
def test_write_guard_allows_the_own_spec_and_components(ctx, rel):
    assert T.check_write(ctx, rel)


def test_path_matches_whole_files_and_folder_prefixes():
    entries = ("review/p/", "pipelines/p.yaml", "sources/x.py")
    assert T.path_matches("review/p/a/b.md", entries) and T.path_matches("PIPELINES/P.yaml", entries)
    assert not T.path_matches("pipelines/p.yaml.bak", entries) and not T.path_matches("sources/x.pyc", entries)
    assert not T.path_matches("review/px/a.md", entries) and not T.path_matches("x", ())


def test_write_guard_shared_component_and_framework_in_every_phase(ctx, tmp_path):
    wt = ctx.worktree
    (wt / "connections" / "c2.yaml").write_text("source: s1\ndestination: d2\n")
    _commit_all(wt)
    with pytest.raises(T.PathDenied, match="another pipeline"):
        T.check_write(ctx, "sources/s1.yaml")
    tr = session.Transcript(tmp_path / "t.jsonl")
    fw_file = str(ctx.framework_dir / "pkf" / "cli.py")
    for phase in ("diagnose", "ask", "brief", "fix"):
        hook = session.make_hook(ctx, {"Read", "Edit", "Write"}, write=(phase == "fix"), transcript=tr)
        for target in (fw_file, str(wt / "src" / "pkf" / "x.py"), str(wt / "pyproject.toml")):
            out = asyncio_run(hook({"tool_name": "Edit", "tool_input": {"file_path": target}}, "1", None))
            assert out["hookSpecificOutput"]["permissionDecision"] == "deny", (phase, target)
    tr.close()
    rows = [json.loads(x) for x in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert any("framework" in r["text"] for r in rows)


# ------------------------------------------------------------------------------------------------------ D. memory
def test_memory_notes(ctx):
    assert memory.notes(ctx.root, "p1") == []
    out = T.remember(ctx, "  root cause:\n the copy step uses   the wrong prefix  ")
    assert out.startswith("remembered:") and memory.notes(ctx.root, "p1")[0].endswith("root cause: the copy step uses the wrong prefix")
    T.remember(ctx, "token ghp_" + "a" * 36 + " " + "x" * 400)
    last = memory.notes(ctx.root, "p1")[-1]
    assert "[REDACTED]" in last and "ghp_" not in last and len(last.split(" ", 2)[2]) <= memory.NOTE_MAX
    for i in range(60):
        T.remember(ctx, f"note {i} " + "y" * 100)
    path = memory.path(ctx.root, "p1")
    assert path == ctx.root / ".pkf-studio" / "flows" / "p1" / "assistant" / "memory.md"
    assert path.stat().st_size <= memory.FILE_MAX and memory.notes(ctx.root, "p1")[-1].split(" ", 2)[2].startswith("note 59 ")
    assert not any("root cause" in n for n in memory.notes(ctx.root, "p1"))   # the oldest went
    with pytest.raises(ValueError):
        T.remember(ctx, "   ")
    assert (path.parent.parent / ".gitignore").exists() or (ctx.root / ".pkf-studio" / "flows" / ".gitignore").exists()


# ------------------------------------------------------------------------------------------------------ E. run brief
def _job(root: Path, jid: str, *, pipeline="p1", action="start_g4", title="4.1 Start S4", lane=None, state="running", log="",
         started=None, result=None) -> Path:
    d = root / ".pkf-studio" / "jobs" / jid
    d.mkdir(parents=True)
    cmd = {"id": jid, "action": action, "pipeline": pipeline, "args": {}, "root": str(root), "title": title,
           "started_at": started or "2026-01-01T10:00:00+00:00"}
    if lane:
        cmd["lane"] = lane
    (d / "cmd.json").write_text(json.dumps(cmd))
    (d / "log").write_text(log)
    if state == "running":
        import psutil

        (d / "pid").write_text(f"{os.getpid()} {psutil.Process(os.getpid()).create_time()}")
    elif state in ("succeeded", "failed"):
        (d / "exit").write_text("0" if state == "succeeded" else "1")
        (d / "result.json").write_text(json.dumps(result or {"ok": state == "succeeded"}))
    return d


def _lock(root: Path, jid: str, pipeline="p1", lane=None) -> Path:
    lk = jobs.lock_path(root, pipeline, lane)
    lk.parent.mkdir(parents=True, exist_ok=True)
    lk.write_text(json.dumps({"job": jid}))
    return lk


def _states(**states) -> dict:
    rows = [GateState(g, states.get(g, "not_started"), {"number": 7} if states.get(g) == "waiting" else None,
                      "an earlier approval changed" if states.get(g) == "stale" else "") for g in ("G1", "G2", "G3", "G4", "G5", "RUN")]
    return {"gates": rows, "by_gate": {r.gate: r for r in rows}, "current": next((r.gate for r in rows if not r.done), "RUN")}


def _flow(cursor: str, status: str = "running", **row) -> dict:
    from pkf_studio import flow

    st = flow._new_state("p1", "dev")
    for x in flow.STEPS:
        if x.id == cursor:
            break
        st["steps"][x.id]["status"] = "done"
    st["steps"][cursor] = {"status": row.pop("step_status", "running"), **row}
    st.update(status=status, cursor=cursor, message=f"S? · {cursor} — {status}")
    return st


def test_brief_without_anything_never_raises(tmp_path):
    b = brief.build(None, "p1", None, None, root=tmp_path / "nowhere")
    assert b["run_status"] == "idle" and b["now"] is None and b["issue"] is None and b["blocking"] == [] and b["log_tail"] == []
    text = brief.text(b)
    for label in ("Done", "Now", "Next", "Issue", "Blocking", "Log"):
        assert any(line.startswith(label) for line in text.splitlines()), label
    weird = brief.build(None, "p1", "garbage", {"steps": "bad", "cursor": 5, "status": "running"}, root=tmp_path)
    assert isinstance(weird, dict) and "pipeline" in weird
    assert brief.build(object(), "p1")["message"].startswith("(the run brief failed")    # a broken studio: degrades


def test_brief_running_job(ctx):
    log = "== 4.1 Start S4 (p1)\n$ pkf code source s1\n  → OK\n$ pkf test pipeline p1\n  … still running pkf test pipeline p1 (2m30s)\n"
    _job(ctx.root, "j41", log=log)
    _lock(ctx.root, "j41")
    import datetime as dt

    started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=200)).isoformat()
    st = _flow("4.1", job_id="j41", started_at=started)
    b = brief.build(None, "p1", _states(G1="approved", G2="approved", G3="skipped"), st, n_log=3, root=ctx.root, worktree=ctx.worktree)
    assert [d["stage"] for d in b["done"]] == ["S1", "S2", "S3"] and b["done"][2]["gate_state"] == "skipped"
    assert b["now"]["step"] == "4.1" and b["now"]["status"] == "running" and b["now"]["elapsed_s"] > 60
    assert b["now"]["detail"].startswith("… still running pkf test pipeline")
    assert b["next"][0] == {"step": "4.2", "title": "Review code and capabilities", "who": "you"}
    assert b["next"][1]["step"] == "4.4" and b["next"][1]["who"] == "DE"           # 4.3 is optional
    assert b["blocking"] == ["a job is running: 4.1 Start S4"] and len(b["log_tail"]) == 3
    assert b["gates"]["G3"] == "skipped" and b["issue"] is None
    text = brief.text(b)
    assert "Done      S1 ✓ G1 · S2 ✓ G2 · S3 ✓ G3" in text and "Now       S4 · 4.1 Start S4: code, test, build — running " in text
    assert "Next      4.2 Review code and capabilities (you) → 4.4 G4: DE approves, Merge (DE)" in text


def test_brief_failed_step_and_gate_readiness(ctx):
    st = _flow("4.4", status="failed", step_status="failed", detail="not ready: the draft build failed",
               problems=["the draft build failed: x", "files outside the pipeline"])
    b = brief.build(None, "p1", {"G1": {"state": "approved"}, "G4": {"state": "in_progress"}}, st, root=ctx.root, worktree=ctx.worktree)
    assert b["issue"]["title"] == "4.4 G4: DE approves, Merge failed" and b["issue"]["step"] == "4.4"
    assert "files outside the pipeline" in b["issue"]["detail"]
    assert "not ready: files outside the pipeline" in b["blocking"]
    assert b["next"][0]["step"] == "5.1" and b["next"][0]["who"] == "run"


def test_brief_tests_json_issue_and_blockers(ctx):
    rv = ctx.worktree / "review" / "p1"
    rv.mkdir(parents=True)
    tests = {"version": 2, "build_ok": False, "examples": [{"example": "a", "status": "pass"}, {"example": "b", "status": "fail"},
                                                         {"example": "c", "status": "pass"}],
             "components": [{"kind": "source", "name": "s1", "failing": []},
                            {"kind": "transformation", "name": "lab_control_check",
                             "failing": [{"step": "check_controls", "case": "missing_control_row", "detail": "expected 1 error, got 0"}]}]}
    (rv / "tests.json").write_text(json.dumps(tests))
    b = brief.build(None, "p1", {"G3": {"state": "approved"}}, {}, root=ctx.root, worktree=ctx.worktree)
    assert b["issue"] == {"title": "transformation lab_control_check — 1 test(s) fail", "step": "check_controls",
                          "detail": "missing_control_row: expected 1 error, got 0"}
    assert "examples 1 of 3 failing" in b["blocking"] and "the draft build failed" in b["blocking"]
    assert "1 component(s) fail their tests" in b["blocking"]
    after = brief.build(None, "p1", {"G4": {"state": "approved"}}, {}, root=ctx.root, worktree=ctx.worktree)
    assert after["issue"] is None and after["blocking"] == []                     # G4 approved: the S4 status is history


def test_brief_crashed_job_waiting_and_stale_gates_pilot_memory(ctx):
    d = _job(ctx.root, "jc", state="crashed", title="5.1 Start S5")
    (d / "crash.log").write_text("Traceback ...\nMemoryError\n")
    memory.remember(ctx.root, "p1", "G3 reopen agreed with the user")
    st = {"status": "failed", "cursor": None, "pilot": {"phase": "needs_you", "note": "a person must review 4.2"}}
    b = brief.build(None, "p1", _states(G1="approved", G2="stale", G3="waiting"), st, root=ctx.root, worktree=ctx.worktree)
    assert b["issue"]["title"] == "job 5.1 Start S5 crashed" and "MemoryError" in b["issue"]["detail"]
    assert "G3 waits for the approvers (PR #7)" in b["blocking"] and "G2 stale: an earlier approval changed" in b["blocking"]
    assert b["pilot"] == {"phase": "needs_you", "note": "a person must review 4.2"}
    assert b["memory"][0].endswith("G3 reopen agreed with the user")
    text = brief.text(b)
    assert "Pilot     needs_you: a person must review 4.2" in text and "Memory" in text


def test_brief_ignores_lane_jobs_and_gate_formats(ctx):
    _job(ctx.root, "lane1", action="assist", title="assistant", lane="assistant")
    _lock(ctx.root, "lane1", lane="assistant")
    b = brief.build(None, "p1", None, {}, root=ctx.root, worktree=ctx.worktree)
    assert b["blocking"] == [] and b["log_tail"] == []                            # the lane job is not the pipeline's job
    assert brief.gate_states({"offline": True, "error": "x"}) == {}
    assert brief.gate_states(_states(G1="approved"))["G1"]["state"] == "approved"
    assert brief.gate_states({"G2": {"state": "waiting", "pr": 3}})["G2"] == {"state": "waiting", "note": "", "pr": 3}
    assert T.workflow_state(ctx)["pipeline"] == "p1"


def test_cause_hash_ignores_elapsed_mode_and_readiness():
    base = {"pipeline": "p1", "mode": "dev", "run_status": "running", "done": [], "next": [], "blocking": ["a job is running: 4.1"],
            "now": {"stage": "S4", "step": "4.1", "title": "t", "status": "running", "elapsed_s": 10, "detail": "… still running pkf x (0m10s)"},
            "issue": None, "log_tail": ["  … still running pkf x (0m10s)"], "memory": []}
    later = json.loads(json.dumps(base))
    later["now"].update(elapsed_s=190, detail="… still running pkf x (3m10s)")
    later["log_tail"] = ["  … still running pkf x (3m10s)"]
    later["mode"] = "sit"
    later["blocking"].append(f"{brief.READINESS_PREFIX} G4: tests")
    assert brief.cause_hash(base) == brief.cause_hash(later)
    other = json.loads(json.dumps(base))
    other["issue"] = {"title": "x failed", "detail": "d", "step": "4.1"}
    assert brief.cause_hash(other) != brief.cause_hash(base)


def test_likely_cause_is_cached_on_the_brief(ctx, monkeypatch):
    calls = []

    def fake_run(phase, c, prompt, schema, transcript_path, resume=None):
        calls.append(phase)
        assert phase == "brief" and schema is protocol.CAUSE_SCHEMA and resume is None
        assert prompt.startswith(session.PHASE_RULES["brief"]) and "RUN BRIEF" in prompt and "4.1 Start S4: code, test, build failed" in prompt
        assert transcript_path.name == protocol.CAUSE_TRANSCRIPT and transcript_path.parent.name == "assistant"
        return ({"cause": "The check step never writes the missing row.", "next_step": "Regenerate check_controls.", "confidence": 0.7},
                None, {"cost_usd": 0.05, "turns": 1})
    monkeypatch.setattr(session, "run_session", fake_run)
    assert protocol.likely_cause(ctx)["skipped"]                                   # nothing wrong: no call
    flow_st = _flow("4.1", status="failed", step_status="failed", detail="pkf test: 1 failing")
    flowstore.save(ctx.root, "p1", flow_st)
    out = protocol.likely_cause(ctx)
    assert out["cause"].startswith("The check step") and out["cached"] is False and out["usage"]["cost_usd"] == 0.05
    again = protocol.likely_cause(ctx)
    assert again["cached"] is True and calls == ["brief"]
    b = brief.from_context(ctx)
    assert brief.cached_cause(ctx.root, "p1", b)["next_step"] == "Regenerate check_controls."
    doc = json.loads(brief.cause_path(ctx.root, "p1").read_text())
    assert set(doc) == {"hash", "cause", "next_step", "confidence", "at", "usage"}
    flow_st["steps"]["4.1"]["detail"] = "pkf test: 2 failing"                     # the state changed: recomputed
    flowstore.save(ctx.root, "p1", flow_st)
    assert brief.cached_cause(ctx.root, "p1", brief.from_context(ctx)) is None
    protocol.likely_cause(ctx)
    assert calls == ["brief", "brief"]
    sessions = [x for x in (flowstore.flow_dir(ctx.root, "p1") / "assistant").iterdir() if x.is_dir()]
    assert sessions == []                                   # the cause and its transcript are files, not a "session"


# ------------------------------------------------------------------------------------------------------ F. the lane
def test_lane_locks_current_running_all_recent(tmp_path):
    root = tmp_path
    _job(root, "20261020T100000-pipe", title="4.1")
    _lock(root, "20261020T100000-pipe")
    _job(root, "20261020T100500-lane", action="assist", title="ask", lane="assistant", started="2026-01-01T10:05:00+00:00")
    lane_lock = _lock(root, "20261020T100500-lane", lane="assistant")
    assert lane_lock == root / ".pkf-studio" / "locks" / "assistant" / "p1.lock"
    assert jobs.current(root, "p1") == "20261020T100000-pipe"
    assert jobs.current(root, "p1", lane="assistant") == "20261020T100500-lane"
    assert [j["id"] for j in jobs.running_all(root)] == ["20261020T100000-pipe"]          # the glob never descends
    both = jobs.running_all(root, lanes=True)
    assert [(j["id"], j.get("lane")) for j in both] == [("20261020T100000-pipe", None), ("20261020T100500-lane", "assistant")]
    assert [j["id"] for j in jobs.recent(root, "p1")] == ["20261020T100000-pipe"]
    assert [j["id"] for j in jobs.recent(root, "p1", lane="assistant")] == ["20261020T100500-lane"]
    assert [j["id"] for j in jobs.recent(root, "p1", lane="*")] == ["20261020T100500-lane", "20261020T100000-pipe"]
    assert jobs.status(root, "20261020T100500-lane")["lane"] == "assistant" and "lane" not in jobs.status(root, "20261020T100000-pipe")
    with pytest.raises(jobs.JobError):
        jobs.lock_path(root, "p1", "nope")


def test_release_and_runner_release_only_the_lane_lock(tmp_path):
    pipe = _lock(tmp_path, "pj")
    lane = _lock(tmp_path, "lj", lane="assistant")
    jobs.release(tmp_path, "p1", "someone-else", "assistant")
    assert lane.exists() and pipe.exists()                     # a lane lock naming another job stays
    jobs.release(tmp_path, "p1", "lj", "assistant")
    assert not lane.exists() and pipe.exists()


def test_cancel_lane_job_keeps_the_pipeline_lock(tmp_path):
    import psutil

    root = tmp_path
    _job(root, "pipe", title="4.1")
    pipe_lock = _lock(root, "pipe")
    d = _job(root, "lanejob", action="assist", lane="assistant", state="none")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (d / "pid").write_text(f"{proc.pid} {psutil.Process(proc.pid).create_time()}")
        lane_lock = _lock(root, "lanejob", lane="assistant")
        jobs.cancel(root, "lanejob")
        assert not lane_lock.exists() and pipe_lock.exists() and jobs.current(root, "p1") == "pipe"
        assert jobs.status(root, "lanejob")["state"] == "failed"
    finally:
        proc.kill()
        proc.wait()


def _inline(monkeypatch):
    monkeypatch.setenv("PKF_STUDIO_INLINE_JOBS", "1")
    monkeypatch.delenv(jobs.LANE_ENV, raising=False)


def test_lane_job_runs_beside_a_pipeline_job(tmp_path, monkeypatch):
    """A lane assist job starts while the pipeline's job holds its lock, opens no Studio (the root is not even a git
    repository), takes the gates passed in, gets no run_tests, and releases only its own lock."""
    _inline(monkeypatch)
    root = tmp_path
    _job(root, "pipe", title="4.1 Start S4")
    pipe_lock = _lock(root, "pipe")
    seen = {}

    def fake_ask(c, question, session_id=None, log=None):
        seen.update(ctx=c, lane_env=os.environ.get(jobs.LANE_ENV), tools=[t.name for t in T.make_tools(c)])
        return {"session_id": "s1", "answer": "In short: 4.1 is coding.", "plain": "4.1 is coding."}
    monkeypatch.setattr(protocol, "ask", fake_ask)
    with pytest.raises(jobs.JobError):                                       # the pipeline lane is busy, as before
        jobs.start(root, "p1", "assist", {"phase": "ask", "question": "q"}, "ask")
    jid = jobs.start(root, "p1", "assist", {"phase": "ask", "question": "what runs?", "gates": {"G1": {"state": "approved"}}},
                     "🤖 ask", lane="assistant")
    stt = jobs.status(root, jid)
    assert stt["state"] == "succeeded", jobs.log(root, jid)
    assert stt["lane"] == "assistant" and stt["result"]["session_id"] == "s1"
    assert json.loads((root / ".pkf-studio" / "jobs" / jid / "cmd.json").read_text())["lane"] == "assistant"
    c = seen["ctx"]
    assert c.lane is True and c.gates == {"G1": {"state": "approved"}} and c.worktree == root / ".pkf-studio" / "worktrees" / "p1"
    assert seen["lane_env"] == "assistant" and "run_tests" not in seen["tools"] and "workflow_state" in seen["tools"]
    assert jobs.LANE_ENV not in os.environ                                     # restored after the inline job
    assert pipe_lock.exists() and jobs.current(root, "p1") == "pipe"           # never touched the pipeline's lock
    assert not jobs.lock_path(root, "p1", "assistant").exists()                # released its own
    assert [j["id"] for j in jobs.recent(root, "p1")] == ["pipe"]              # "Last job" stays the pipeline's
    with pytest.raises(jobs.JobError, match="only assist"):
        jobs.start(root, "p1", "test_build", {}, "x", lane="assistant")


def test_lane_refuses_fix_and_runs_the_brief_phase(tmp_path, monkeypatch):
    _inline(monkeypatch)
    root = tmp_path
    jid = jobs.start(root, "p1", "assist", {"phase": "fix", "session_id": "s"}, "fix", lane="assistant")
    stt = jobs.status(root, jid)
    assert stt["state"] == "failed" and "pipeline's own lane" in stt["result"]["error"]
    flowstore.save(root, "p1", _flow("3.3", status="failed", step_status="failed", detail="pkf plan: llm_timeout"))

    def fake_run(phase, c, prompt, schema, transcript_path, resume=None):
        assert phase == "brief" and c.lane
        return ({"cause": "The AI planner timed out.", "next_step": "Retry 3.3.", "confidence": 0.8}, None, {"cost_usd": 0.02, "turns": 1})
    monkeypatch.setattr(session, "run_session", fake_run)
    jid = jobs.start(root, "p1", "assist", {"phase": "brief", "gates": json.dumps({"G2": {"state": "approved"}})}, "brief",
                     lane="assistant")
    stt = jobs.status(root, jid)
    assert stt["state"] == "succeeded", jobs.log(root, jid)
    assert stt["result"]["phase"] == "brief" and stt["result"]["cause"] == "The AI planner timed out." and stt["result"]["cached"] is False
    assert "brief" not in stt["result"]
    assert brief.cached_cause(root, "p1", brief.build(None, "p1", {"G2": {"state": "approved"}}, None, root=root))["next_step"] == "Retry 3.3."


def test_lane_job_is_refused_every_studio_change(tmp_path, monkeypatch):
    from pkf_studio.project import ProjectConfig, Team
    from pkf_studio.service import Studio, StudioError

    s = Studio(root=tmp_path, git=None, host="github.com", owner="o", repo="r", gh=None, cfg=ProjectConfig(), team=Team(), configured=True)
    monkeypatch.delenv(jobs.LANE_ENV, raising=False)
    s._idle("p1")                                                              # no job, no lane: allowed as before
    monkeypatch.setenv(jobs.LANE_ENV, "assistant")
    for call in (lambda: s._idle("p1"), lambda: s.commit("p1", "m"), lambda: s.push("p1"), lambda: s.open_stage("p1", "G2"),
                 lambda: s.open_run_branch("p1"), lambda: s.request_approval("p1", "G1"), lambda: s.merge("p1", "G1"),
                 lambda: s.reopen("p1", "G2"), lambda: s.discard("p1"), lambda: s.post_approve("p1", "G1")):
        with pytest.raises(StudioError, match="lane only reads"):
            call()
    assert "lane only reads" in s.push_quiet("p1")


def test_spawned_lane_job_gets_the_lane_env(tmp_path, monkeypatch):
    seen = {}

    class P:
        pid = os.getpid()

    def fake_popen(argv, **kw):
        seen["env"] = kw["env"]
        return P()
    monkeypatch.delenv("PKF_STUDIO_INLINE_JOBS", raising=False)
    monkeypatch.setenv(jobs.LANE_ENV, "assistant")                             # a stray value never leaks into a pipeline job
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    jobs.start(tmp_path, "p1", "assist", {"phase": "ask", "question": "q"}, "ask", lane="assistant")
    assert seen["env"][jobs.LANE_ENV] == "assistant"
    jobs.start(tmp_path, "p2", "assist", {"phase": "ask", "question": "q"}, "ask")
    assert jobs.LANE_ENV not in seen["env"]


# ------------------------------------------------------------------------------------------------ Windows command line
def test_the_system_prompt_goes_to_the_cli_as_a_file_and_the_command_line_stays_short(tmp_path, monkeypatch):
    """Windows caps a command line at 32,767 characters; knowledge.md + the code map are ~90k. 0.8.0 first passed them
    with --system-prompt and CreateProcess failed ("Claude Code not found at …\\claude.exe")."""
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    from pkf_studio.assistant import session as S

    ctx = _ctx_for_cmdline(tmp_path)
    for phase in ("diagnose", "fix", "ask", "brief"):
        tr = S.Transcript(tmp_path / f"t-{phase}.jsonl")
        opts = S.build_options(phase, ctx, {"type": "object"} if phase != "ask" else None, tr)
        tr.close()
        assert isinstance(opts.system_prompt, dict) and opts.system_prompt["type"] == "file"
        path = Path(opts.system_prompt["path"])
        assert path.is_file() and path.read_text(encoding="utf-8") == S.system_prompt(ctx)
        assert str(path).startswith(str(ctx.cache_dir))
        opts.cli_path = "claude.exe"
        t = SubprocessCLITransport(prompt="x", options=opts)
        t._cli_path = "claude.exe"
        cmd = t._build_command()
        assert "--system-prompt-file" in cmd and "--system-prompt" not in cmd
        assert len(" ".join(cmd)) < 30000, len(" ".join(cmd))


def _ctx_for_cmdline(tmp_path):
    from types import SimpleNamespace

    import pkf

    root = tmp_path / "repo"
    (root / ".pkf-studio" / "cache").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    fw = Path(pkf.__file__).resolve().parent.parent
    return SimpleNamespace(root=root, worktree=wt, framework_dir=fw, pipeline="p1", mode="dev", jobs_dir=root / ".pkf-studio/jobs",
                           flows_dir=root / ".pkf-studio/flows", cache_dir=root / ".pkf-studio" / "cache", lane=False,
                           flow={}, gates={})
