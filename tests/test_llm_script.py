"""The scripted LLM provider (K12), the replay fixture trace and the fixture re-key switch (K16). No network, no key."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pkf.envelope import PfError
from pkf.llm import base
from pkf.llm.schemas import CustomStepCode

ROOT = Path(__file__).resolve().parents[1]
FN = "def step(pkg, params, ctx):\n    return None"


def answer(src: str = FN, why: str = "x") -> dict:
    return {"function_source": src, "explanation": why}


@pytest.fixture()
def script(tmp_path, monkeypatch):
    monkeypatch.setenv("PKF_PLUGIN_ROOT", str(ROOT / "steps-plugin"))
    monkeypatch.setenv("PKF_LLM_PROVIDER", "script")
    path = tmp_path / "answers.json"
    monkeypatch.setenv("PKF_LLM_SCRIPT", str(path))
    monkeypatch.setenv("PKF_LLM_SCRIPT_LOG", str(tmp_path / "calls.jsonl"))

    def write(answers: list[dict]) -> Path:
        path.write_text(json.dumps({"version": 1, "answers": answers}), encoding="utf-8")
        return path
    return write


def call(prompt: str, task: str = "write_custom_step"):
    return base.run_structured(task, "writing-custom-step-code", prompt, CustomStepCode)


def test_script_selects_by_task_and_when(script):
    script([{"task": "plan_component", "output": {"bad": 1}},
            {"task": "write_custom_step", "when": "Write the custom step `b`", "output": answer(why="b")},
            {"task": "write_custom_step", "output": answer(why="any")}])
    out, meta = call("Component kind: x.\nWrite the custom step `b`: second")
    assert out.explanation == "b" and meta.provider == "script" and meta.model == "script"
    assert call("Write the custom step `a`: first")[0].explanation == "any"  # `when` is optional
    with pytest.raises(PfError) as e:
        call("anything", task="write_step_tests")
    assert e.value.code == "llm_error" and "script: no answer for write_step_tests" in str(e.value)


def test_script_output_is_validated_like_a_model_answer(script):
    script([{"task": "write_custom_step", "output": {"function_source": FN}}])
    with pytest.raises(PfError) as e:
        call("p")
    assert e.value.code == "llm_schema"


def test_script_times_and_state_persist_across_processes(script, tmp_path):
    path = script([{"task": "write_custom_step", "when": "step `a`", "times": 1, "output": answer(why="first")},
                   {"task": "write_custom_step", "when": "step `a`", "output": answer(why="later")}])
    probe = ("import sys; from pkf.llm import base; from pkf.llm.schemas import CustomStepCode\n"
             "print(base.run_structured('write_custom_step', 'writing-custom-step-code', 'step `a`', CustomStepCode)[0].explanation)")
    import os
    env = {**os.environ}
    got = [subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=60).stdout.strip()
           for _ in range(3)]
    assert got == ["first", "later", "later"]
    state = json.loads(Path(str(path) + ".state.json").read_text(encoding="utf-8"))
    assert state["used"] == {"0": 1, "1": 2}


def test_script_raise_entries(script):
    script([{"task": "write_custom_step", "when": "`b`", "times": 1, "raise": "llm_limit", "message": "budget"},
            {"task": "write_custom_step", "when": "`b`", "output": answer(why="retry")}])
    with pytest.raises(PfError) as e:
        call("step `b`")
    assert (e.value.code, str(e.value)) == ("llm_limit", "budget")
    assert call("step `b`")[0].explanation == "retry"


def test_script_log_has_full_prompt(script, tmp_path):
    script([{"task": "write_custom_step", "output": answer()}])
    long_prompt = "start\n" + "x" * 50_000 + "\nthe end; password: Hunter2secret!"
    call(long_prompt)
    with pytest.raises(PfError):
        call("p", task="plan_component")
    rows = [json.loads(ln) for ln in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["entry"] for r in rows] == [0, None]
    assert rows[0]["prompt"].startswith("start\n") and rows[0]["prompt"].endswith("[REDACTED]")  # full, redacted
    assert rows[0]["len"] == len(rows[0]["prompt"]) > 50_000 and len(rows[0]["prompt_sha256"]) == 64


def _replay_env(monkeypatch, fixtures: Path, tmp_path: Path) -> None:
    monkeypatch.setenv("PKF_PLUGIN_ROOT", str(ROOT / "steps-plugin"))
    monkeypatch.setenv("PKF_LLM_PROVIDER", "replay")
    monkeypatch.setenv("PKF_LLM_FIXTURES", str(fixtures))
    monkeypatch.delenv("PKF_LLM_REKEY", raising=False)


def _record(fixtures: Path, task: str, key: str, output: dict, **prov) -> Path:
    f = fixtures / task / f"{key}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"task": task, "skill": "writing-custom-step-code", "output": output,
                             "output_sha256": base._canon_sha(output), "provenance": {"model": "m", **prov}}), encoding="utf-8")
    return f


def test_fixture_trace_records_hits(tmp_path, monkeypatch):
    fixtures = tmp_path / "fx"
    _replay_env(monkeypatch, fixtures, tmp_path)
    schema = base.strict_schema(CustomStepCode)
    f = _record(fixtures, "write_custom_step", base._key("write_custom_step", "writing-custom-step-code", "p1", schema), answer())
    trace = tmp_path / "trace.txt"
    monkeypatch.setenv("PKF_LLM_FIXTURE_TRACE", str(trace))
    call("p1")
    call("p1")
    with pytest.raises(PfError):
        call("p2")  # a miss is not a hit
    assert trace.read_text(encoding="utf-8").splitlines() == [str(f.resolve())] * 2


def _lab_control_prompt(tmp_path: Path, monkeypatch) -> str:
    """The 0.5.0 first-attempt coder prompt of the scenario test_flow replays (lab_control_check: re-planned from the
    recorded plan and tests, approved, then coded)."""
    from pkf import design
    from pkf.repo import load

    repo_dir = tmp_path / "specs"
    shutil.copytree(ROOT / "examples", repo_dir, ignore=shutil.ignore_patterns("dags", "__pycache__", ".git", ".llm"))
    (repo_dir / "transformations/lab_control_check.lock.yaml").unlink()
    (repo_dir / "transformations/lab_control_check.py").unlink()
    monkeypatch.setenv("PKF_LLM_FIXTURES", str(ROOT / "tests" / "llm-fixtures"))
    repo = load(repo_dir)
    design.plan(repo, "transformation", "lab_control_check")
    design.approve_plan(repo, "transformation", "lab_control_check", "@tester")
    spec = design.spec_of(repo, "transformation", "lab_control_check")
    lock = design.load_lock(repo, "transformation", "lab_control_check")
    i, step = next((i, s) for i, s in enumerate(lock["plan"]["steps"]) if s["op"] in design.CUSTOM_OPS)
    return design._code_prompt("transformation", spec, lock, step, i, design.code_policy(repo.root))


def test_rekey_maps_old_key_to_new_for_the_same_scenario(tmp_path, monkeypatch):
    fixtures = tmp_path / "fx"
    _replay_env(monkeypatch, fixtures, tmp_path)
    prompt, _ = base.redact_secrets(_lab_control_prompt(tmp_path, monkeypatch))
    monkeypatch.setenv("PKF_LLM_FIXTURES", str(fixtures))
    for sentence in base.PROMPT_CHANGES_0_5["write_custom_step"][:2]:
        assert sentence.startswith("\n") and sentence in prompt  # inserted verbatim, leading newline included
    assert base.PROMPT_CHANGES_0_5["write_custom_step"][2] not in prompt  # ctx['delivery']: custom_delivery steps only
    schema = base.strict_schema(CustomStepCode)
    old_prompt = prompt
    for sentence in base.PROMPT_CHANGES_0_5["write_custom_step"]:
        old_prompt = old_prompt.replace(sentence, "", 1)
    old_key = base._key("write_custom_step", "writing-custom-step-code", old_prompt, schema)
    new_key = base._key("write_custom_step", "writing-custom-step-code", prompt, schema)
    assert old_key == "26a9abe9c388da74315ecd0438840900e09c8715b805048540ff867ababd47ed"  # the 0.4.0 recording, byte for byte
    assert (ROOT / "tests/llm-fixtures/write_custom_step" / f"{new_key}.json").exists()  # committed after re-keying
    out = answer(why="recorded for 0.4.0")
    _record(fixtures, "write_custom_step", old_key, out, recorded_at="2026-10-17T10:46:30Z")

    with pytest.raises(PfError) as e:  # CI: no re-key switch, a miss stays an error
        call(prompt)
    assert e.value.code == "no_fixture"
    monkeypatch.setenv("PKF_LLM_REKEY", "1")
    monkeypatch.setenv("PKF_LLM_REKEY_LOG", str(tmp_path / "rekey.jsonl"))
    got, meta = call(prompt)
    assert got.explanation == "recorded for 0.4.0" and meta.provider == "replay:2026-10-17T10:46:30Z"
    rec = json.loads((fixtures / "write_custom_step" / f"{new_key}.json").read_text(encoding="utf-8"))
    assert rec["output"] == out and rec["output_sha256"] == base._canon_sha(out)
    assert rec["provenance"]["rekeyed_from"] == old_key
    assert json.loads((tmp_path / "rekey.jsonl").read_text(encoding="utf-8")) == {"task": "write_custom_step", "old": old_key,
                                                                                  "new": new_key}
    monkeypatch.delenv("PKF_LLM_REKEY")
    assert call(prompt)[0].explanation == "recorded for 0.4.0"  # served without the switch from now on


def test_strip_fence():
    assert base.strip_fence("```python\ndef f():\n    pass\n```") == "def f():\n    pass"
    assert base.strip_fence("  \n```json\n[1]\n```  \n") == "[1]"
    assert base.strip_fence("```\nx\n```") == "x"
    assert base.strip_fence("def f():\n    return '```'") == "def f():\n    return '```'"
    assert base.strip_fence("not fenced") == "not fenced" and base.strip_fence(None) is None


def test_replay_serves_a_recorded_schema_retry(tmp_path, monkeypatch):
    """A6: a recording made with agent-sdk/record keeps both answers (the invalid one and the retry); replay serves the
    retry from its fixture and never calls a model. Without the retry fixture, replay fails as before (llm_schema)."""
    monkeypatch.setenv("PKF_PLUGIN_ROOT", str(ROOT / "steps-plugin"))
    monkeypatch.setenv("PKF_LLM_PROVIDER", "replay")
    monkeypatch.setenv("PKF_LLM_FIXTURES", str(tmp_path))
    task, skill, prompt = "write_custom_step", "writing-custom-step-code", "Write the custom step `s`"
    schema = base.strict_schema(CustomStepCode)

    def store(p: str, out: dict) -> None:
        f = tmp_path / task / f"{base._key(task, skill, p, schema)}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"task": task, "skill": skill, "provenance": {"model": "m"}, "output": out,
                                 "output_sha256": base._canon_sha(out)}), encoding="utf-8")
    store(prompt, {"function_source": FN})
    with pytest.raises(PfError) as e:
        call(prompt)
    assert e.value.code == "llm_schema" and "explanation" in str(e.value)
    errors = [{"loc": ["explanation"], "msg": "Field required", "type": "missing"}]
    store(prompt + base.SCHEMA_FEEDBACK.format(errors=errors), answer(why="retried"))
    assert call(prompt)[0].explanation == "retried"
