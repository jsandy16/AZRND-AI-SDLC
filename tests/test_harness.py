"""The S4 code harness (0.5.0): draft approval, what custom_delivery tests get (K4), the coder (best attempt, fences,
guidance, per-step progress), the structure check, the isolated runner (UTF-8, results file, process/network block,
exact bytes, same package-factory). In process with moto and the scripted LLM provider; no network, no key."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from pkf import design
from pkf.envelope import PfError
from pkf.llm import base
from pkf.repo import load
from pkf_runtime import simulate

ROOT = Path(__file__).resolve().parents[1]
BUCKET = "bkt-test"

ROUTE = '''def route(layout, params, ctx):
    for path in layout["files"]:
        if "/cdar/" in "/" + path:
            layout["keys"][path] = "out/" + path.rsplit("/", 1)[-1]
            layout["vars"]["reason:" + path] = "rule 1"
        else:
            layout["keys"][path] = "error/" + path
            layout["vars"]["reason:" + path] = "no rule matched"
    return layout'''

# The approved pseudocode, coded literally: ctx['delivery'][...] is indexed, not .get (what the user's coder wrote).
MOVE = '''def move(transfers, params, ctx, aws):
    from botocore.exceptions import ClientError
    s3 = aws.s3_destination
    extra = {}
    if ctx["delivery"]["server_side_encryption"]:
        extra["ServerSideEncryption"] = ctx["delivery"]["server_side_encryption"]
    results = []
    for t in transfers:
        try:
            s3.copy({"Bucket": t["source_bucket"], "Key": t["source_key"]}, t["target_bucket"], t["target_key"], ExtraArgs=extra)
        except ClientError as exc:
            results.append({"path": t["path"], "key": t["target_key"], "status": "failed", "detail": str(exc)})
            continue
        s3.delete_object(Bucket=t["source_bucket"], Key=t["source_key"])
        try:
            s3.put_object_tagging(Bucket=t["target_bucket"], Key=t["target_key"], Tagging={"TagSet": [{"Key": "MOVED", "Value": "yes"}]})
        except ClientError:
            pass
        ok = ctx["delivery"]["if_exists"] == "overwrite"
        results.append({"path": t["path"], "key": t["target_key"], "status": "moved" if ok else "failed",
                        "detail": params.get("reason:" + t["path"], "")})
    return results'''

STEPS = [
    {"id": "route", "op": "custom", "title": "Route by rule", "params": {"function": "route"},
     "pseudocode": "1. /cdar/ → out/{file}, reason 'rule 1'\n2. otherwise error/{path}, reason 'no rule matched'"},
    {"id": "move", "op": "custom_delivery", "title": "Copy, delete the source, tag", "params": {"function": "move"},
     "pseudocode": "1. copy with ctx['delivery']['server_side_encryption']\n2. delete the source\n3. tag MOVED=yes"},
]


def transfer(path="S1/cdar/a.txt", src="in/S1/cdar/a.txt", tgt="out/a.txt", size=1):
    return {"path": path, "source_bucket": BUCKET, "source_key": src, "size": size, "etag": None, "target_bucket": BUCKET,
            "target_key": tgt}


CASES = {
    "route": [{"name": "both_rules", "purpose": "both rules", "input": {"files": ["S1/cdar/a.txt", "S1/x/b.txt"]}, "params": {},
               "expected": ["out/a.txt", "error/S1/x/b.txt"], "expect_error": False}],
    "move": [
        {"name": "move_and_tag", "purpose": "normal move", "params": {}, "expect_error": False,
         "input": {"objects": {f"{BUCKET}/in/S1/cdar/a.txt": "x"}, "transfers": [transfer()]},
         "expected": {"objects": [f"{BUCKET}/out/a.txt"], "tags": {f"{BUCKET}/out/a.txt": {"MOVED": "yes"}},
                      "statuses": {"S1/cdar/a.txt": "moved"}}},
        {"name": "copy_fails_source_kept", "purpose": "a failed copy keeps the source", "params": {}, "expect_error": False,
         "input": {"objects": {f"{BUCKET}/in/S1/cdar/a.txt": "x"}, "transfers": [transfer()],
                   "fail_calls": [{"operation": "CopyObject", "key": "out/a.txt"}]},
         "expected": {"objects": [f"{BUCKET}/in/S1/cdar/a.txt"], "statuses": {"S1/cdar/a.txt": "failed"}}},
    ],
}
DELIVERY = {"if_exists": "overwrite", "server_side_encryption": "AES256"}


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("PKF_CODE_POLICY", raising=False)
    monkeypatch.setenv("PKF_PLUGIN_ROOT", str(ROOT / "steps-plugin"))
    root = tmp_path / "specs"
    (root / "destinations").mkdir(parents=True)
    (root / "pkf.toml").write_text('spec_api_version = "pkf/v1"\n[agent]\ncode_policy = "open"\n', encoding="utf-8")
    (root / "destinations/d.yaml").write_text(yaml.safe_dump({
        "api_version": "pkf/v1", "kind": "destination", "name": "d", "type": "s3",
        "connection": {"bucket": BUCKET, "region": "us-east-1"}, "delivery": DELIVERY,
        "requirement": "Route each file by rule and move it, tagging moved files.\n",
        "examples": [{"name": "two", "files": ["S1/cdar/a.txt", "S1/x/b.txt"], "expected_keys": ["out/a.txt", "error/S1/x/b.txt"]}]},
        sort_keys=False), encoding="utf-8")
    r = load(root)
    spec = r.get("destination", "d")
    lock = {"lock_version": 2, "kind": "destination", "name": "d", "requirement_sha256": design.requirement_sha("destination", spec),
            "plan": {"summary": "route then move", "steps": STEPS, "assumptions": [], "ambiguities": [], "step_tests": CASES},
            "simulation": design.run_examples_inprocess("destination", spec, STEPS)}
    lock["plan_digest"] = design.plan_digest(lock)
    design.save_lock(r, "destination", "d", lock)
    design.approve_plan(r, "destination", "d", "@tester")
    return r


def write_code(repo, functions: dict[str, str]) -> Path:
    lock = design.load_lock(repo, "destination", "d")
    cp = design.code_path(repo, "destination", "d")
    cp.write_text(design.render_module("destination", "d", lock, functions), encoding="utf-8", newline="\n")
    lock["code"] = {"file": cp.name, "plan_digest": lock["plan_digest"], "generated_by": {}, "generated_at": "now"}
    design.save_lock(repo, "destination", "d", lock)
    return cp


def stage(repo) -> str:
    return design.status(repo, "destination", "d")["stage"]


@pytest.fixture()
def script(tmp_path, monkeypatch):
    path, log = tmp_path / "answers.json", tmp_path / "calls.jsonl"
    monkeypatch.setenv("PKF_LLM_PROVIDER", "script")
    monkeypatch.setenv("PKF_LLM_SCRIPT", str(path))
    monkeypatch.setenv("PKF_LLM_SCRIPT_LOG", str(log))

    class Script:
        def answers(self, answers: list[dict]) -> None:
            path.write_text(json.dumps({"version": 1, "answers": answers}), encoding="utf-8")
            Path(str(path) + ".state.json").unlink(missing_ok=True)
            log.unlink(missing_ok=True)

        def calls(self) -> list[dict]:
            return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return Script()


def code_answer(step: str, src: str, times: int | None = 1, when: str | None = None) -> dict:
    return {"task": "write_custom_step", "when": when or f"Write the custom step `{step}`", "times": times,
            "output": {"function_source": src, "explanation": f"{step} as planned"}}


def fns_in_file(repo) -> dict[str, str]:
    return design.extract_functions(design.code_path(repo, "destination", "d").read_text(encoding="utf-8"), ["route", "move"])


# ---------------------------------------------------------------------------------------------- A1 draft approval
def test_approved_allow_unapproved_code(repo):
    with pytest.raises(PfError) as e:
        design.approved(repo, "destination", "d", allow_unapproved_code=True)
    assert e.value.code == "component_not_ready" and "not ready for a draft build (plan_approved)" in str(e.value)
    write_code(repo, {"route": ROUTE, "move": MOVE})
    _, issues = design.test_code(repo, "destination", "d")
    assert not issues and stage(repo) == "code_generated"
    with pytest.raises(PfError) as e:
        design.approved(repo, "destination", "d")
    assert e.value.code == "component_not_approved"
    got = design.approved(repo, "destination", "d", allow_unapproved_code=True)
    assert set(got["functions"]) == {"route", "move"} and got["steps"] == STEPS
    assert got["header"][-1] == "Code NOT approved yet: draft build for review (S4)"

    write_code(repo, {"route": ROUTE.replace('"error/"', '"err/"'), "move": MOVE})
    design.test_code(repo, "destination", "d")
    assert stage(repo) == "code_failing"
    with pytest.raises(PfError) as e:
        design.approved(repo, "destination", "d", allow_unapproved_code=True)
    assert e.value.code == "component_not_ready" and "(code_failing)" in str(e.value) and "pkf test destination d" in str(e.value)

    write_code(repo, {"route": ROUTE, "move": MOVE})
    design.test_code(repo, "destination", "d")
    design.approve_code(repo, "destination", "d", "@reviewer")
    for flag in (False, True):
        got = design.approved(repo, "destination", "d", allow_unapproved_code=flag)
        assert got["header"][-1].startswith("Code approved by @reviewer") and design.DRAFT_HEADER not in got["header"]


# ---------------------------------------------------------------------------------------------- A2 / K4 delivery cases
def test_delivery_case_gets_runtime_delivery_ctx(repo, tmp_path):
    cp = write_code(repo, {"route": ROUTE, "move": MOVE})
    spec = repo.get("destination", "d")
    res = design.run_tests_isolated("destination", spec, cp, STEPS, {"move": CASES["move"]})
    assert [r["status"] for r in res if r.get("step") == "move"] == ["pass", "pass"], res
    seen = {}

    def spy(transfers, params, ctx, aws):
        seen.update(ctx)
        return []
    simulate.run_step_case("destination", spy, CASES["move"][0], "custom_delivery", delivery=spec.delivery.model_dump(mode="json"))
    assert seen["delivery"] == spec.delivery.model_dump(mode="json") and seen["delivery"]["prefix"]  # the runtime's dict, prefix too
    assert set(seen) == {"params", "run_id", "now", "delivery"}


def test_literal_pseudocode_move_and_tag_passes(repo):
    write_code(repo, {"route": ROUTE, "move": MOVE})
    lock, issues = design.test_code(repo, "destination", "d")
    assert not issues, issues
    assert {t.get("case"): t["status"] for t in lock["code"]["tests"] if t.get("step") == "move"} == {
        "move_and_tag": "pass", "copy_fails_source_kept": "pass"}


def _fn(src: str):
    ns: dict = {}
    exec(src, ns)  # noqa: S102 - the test's own code
    return next(v for k, v in ns.items() if callable(v) and not k.startswith("__"))


COPY_THEN_TAG = '''def move(transfers, params, ctx, aws):
    from botocore.exceptions import ClientError
    s3, out = aws.s3_destination, []
    for t in transfers:
        s3.copy_object(Bucket=t["target_bucket"], Key=t["target_key"], CopySource={"Bucket": t["source_bucket"], "Key": t["source_key"]})
        s3.delete_object(Bucket=t["source_bucket"], Key=t["source_key"])
        try:
            s3.put_object_tagging(Bucket=t["target_bucket"], Key=t["target_key"], Tagging={"TagSet": [{"Key": "MOVED", "Value": "yes"}]})
        except ClientError:
            pass
        out.append({"path": t["path"], "key": t["target_key"], "status": "moved", "detail": ""})
    return out'''


def test_tag_fidelity_ignores_injected_tagging_failure():
    case = {"name": "tagging_fails_still_moved", "expect_error": False, "params": {},
            "input": {"objects": {f"{BUCKET}/in/S1/cdar/a.txt": {"body": "x", "tags": {"origin": "lab"}}}, "transfers": [transfer()],
                      "fail_calls": [{"operation": "PutObjectTagging"}]},
            "expected": {"objects": [f"{BUCKET}/out/a.txt"], "tags": {f"{BUCKET}/out/a.txt": {"origin": "lab"}},
                         "statuses": {"S1/cdar/a.txt": "moved"}}}
    r = simulate.run_step_case("destination", _fn(COPY_THEN_TAG), case, "custom_delivery", delivery=DELIVERY)
    assert r["status"] == "pass", r
    assert "harness:" not in r["detail"]


def test_copy_over_tagged_target_replaces_tags():
    case = {"name": "overwrite_tagged_target", "expect_error": False, "params": {},
            "input": {"objects": {f"{BUCKET}/in/S1/cdar/a.txt": {"body": "x", "tags": {"new": "2"}},
                                  f"{BUCKET}/out/a.txt": {"body": "y", "tags": {"old": "1"}}},
                      "transfers": [transfer()], "fail_calls": [{"operation": "PutObjectTagging"}]},
            "expected": {"tags": {f"{BUCKET}/out/a.txt": {"new": "2"}}, "statuses": {"S1/cdar/a.txt": "moved"}}}
    r = simulate.run_step_case("destination", _fn(COPY_THEN_TAG), case, "custom_delivery", delivery=DELIVERY)
    assert r["status"] == "pass", r
    untagged = {**case, "input": {**case["input"], "objects": {f"{BUCKET}/in/S1/cdar/a.txt": "x",
                                                                f"{BUCKET}/out/a.txt": {"body": "y", "tags": {"old": "1"}}}},
                "expected": {"tags": {f"{BUCKET}/out/a.txt": {}}, "statuses": {"S1/cdar/a.txt": "moved"}}}
    assert simulate.run_step_case("destination", _fn(COPY_THEN_TAG), untagged, "custom_delivery", delivery=DELIVERY)["status"] == "pass"


RETRYING = '''def move(transfers, params, ctx, aws):
    from botocore.exceptions import ClientError
    s3, out = aws.s3_destination, []
    for t in transfers:
        errors = []
        for _ in range(3):
            try:
                s3.copy_object(Bucket=t["target_bucket"], Key=t["target_key"], CopySource={"Bucket": t["source_bucket"], "Key": t["source_key"]})
                break
            except ClientError as exc:
                errors.append(f'{exc.response["Error"]["Code"]}/{exc.response["ResponseMetadata"]["HTTPStatusCode"]}')
        else:
            out.append({"path": t["path"], "key": t["target_key"], "status": "failed", "detail": ",".join(errors)})
            continue
        s3.delete_object(Bucket=t["source_bucket"], Key=t["source_key"])
        out.append({"path": t["path"], "key": t["target_key"], "status": "moved", "detail": ",".join(errors)})
    return out'''


def test_fail_calls_codes_and_times():
    fn = _fn(RETRYING)
    base_case = {"name": "c", "expect_error": False, "params": {},
                 "input": {"objects": {f"{BUCKET}/in/S1/cdar/a.txt": "x"}, "transfers": [transfer()]}}

    def run(fail, expected_status):
        case = {**base_case, "input": {**base_case["input"], "fail_calls": [fail]},
                "expected": {"statuses": {"S1/cdar/a.txt": expected_status}}}
        return simulate.run_step_case("destination", fn, case, "custom_delivery", delivery=DELIVERY)
    r = run({"operation": "CopyObject", "code": "SlowDown", "times": 2}, "moved")  # fails twice, the third try works
    assert r["status"] == "pass", r
    assert r["output"]["objects"] == [f"{BUCKET}/out/a.txt"]
    r = run({"operation": "CopyObject", "code": "ExpiredToken"}, "failed")  # times null: always
    assert r["status"] == "pass", r
    for code, http in simulate.FAIL_CODES.items():
        r = run({"operation": "CopyObject", "code": code, "key": "out/a.txt"}, "failed")
        assert r["status"] == "pass", (code, r)
    statuses = []

    def detail_of(transfers, params, ctx, aws):
        from botocore.exceptions import ClientError
        try:
            aws.s3_destination.copy_object(Bucket=BUCKET, Key="out/a.txt", CopySource={"Bucket": BUCKET, "Key": "in/S1/cdar/a.txt"})
        except ClientError as exc:
            statuses.append((exc.response["Error"]["Code"], exc.response["ResponseMetadata"]["HTTPStatusCode"]))
        return [{"path": t["path"], "key": t["target_key"], "status": "failed", "detail": ""} for t in transfers]
    for code in simulate.FAIL_CODES:
        simulate.run_step_case("destination", detail_of, {**base_case, "input": {**base_case["input"], "fail_calls": [
            {"operation": "CopyObject", "code": code}]}, "expected": {"statuses": {}}}, "custom_delivery", delivery=DELIVERY)
    assert statuses == list(simulate.FAIL_CODES.items())
    for bad in ({"operation": "CopyObject", "code": "Teapot"}, {"operation": "CopyObject", "times": -1}, {"operation": "Nope"}):
        r = run(bad, "failed")
        assert r["status"] == "fail" and "malformed test case" in r["detail"], r


def test_harness_runs_precheck_and_verify():
    def claims_moved(transfers, params, ctx, aws):  # copies nothing
        return [{"path": t["path"], "key": t["target_key"], "status": "moved", "detail": ""} for t in transfers]
    case = {"name": "c", "expect_error": False, "params": {}, "input": {"objects": {f"{BUCKET}/in/S1/cdar/a.txt": "x"},
                                                                         "transfers": [transfer()]},
            "expected": {"statuses": {"S1/cdar/a.txt": "moved"}}}
    r = simulate.run_step_case("destination", claims_moved, case, "custom_delivery", delivery=DELIVERY)
    assert r["status"] == "fail" and "got 'failed', expected 'moved'" in r["detail"], r
    honest = {**case, "expected": {"statuses": {"S1/cdar/a.txt": "failed"}}}
    assert simulate.run_step_case("destination", claims_moved, honest, "custom_delivery", delivery=DELIVERY)["status"] == "pass"

    called = []

    def never(transfers, params, ctx, aws):
        called.append(transfers)
        return claims_moved(transfers, params, ctx, aws)
    same = {"name": "identical_target", "expect_error": False, "params": {},
            "input": {"objects": {f"{BUCKET}/in/S1/cdar/a.txt": "x", f"{BUCKET}/out/a.txt": "x"}, "transfers": [transfer()]},
            "expected": {"statuses": {"S1/cdar/a.txt": "skipped"}}}
    assert simulate.run_step_case("destination", never, same, "custom_delivery", delivery={"if_exists": "skip_identical"})["status"] == "pass"
    assert simulate.run_step_case("destination", never, same, "custom_delivery", delivery={})["status"] == "pass"  # spec default
    fail = {**same, "expected": {"statuses": {"S1/cdar/a.txt": "failed"}}}
    assert simulate.run_step_case("destination", never, fail, "custom_delivery", delivery={"if_exists": "fail"})["status"] == "pass"
    assert called == []  # the framework decided those files; the function never saw them
    gone = {"name": "source_gone", "expect_error": False, "params": {}, "input": {"objects": {f"{BUCKET}/other": "x"},
                                                                                  "transfers": [transfer()]},
            "expected": {"statuses": {"S1/cdar/a.txt": "failed"}}}
    assert simulate.run_step_case("destination", never, gone, "custom_delivery", delivery=DELIVERY)["status"] == "pass"
    # old callers (no delivery dict): ctx["delivery"] is {} and if_exists is overwrite
    assert simulate.run_step_case("destination", never, same, "custom_delivery")["status"] == "fail" and called


# ---------------------------------------------------------------------------------------------- A7 coder
def test_coder_keeps_best_attempt(repo, script):
    near = ROUTE.replace('"error/"', '"err/"')  # fails the route case and the example
    script.answers([code_answer("route", near), code_answer("route", ROUTE.replace("def route", "def other")),
                    code_answer("route", "def route(layout, params, ctx):\n    return layout["),
                    code_answer("route", "import re\n\n" + ROUTE)])
    lock, issues = design.generate_code(repo, "destination", "d", steps=["route"])
    assert len(script.calls()) == 4
    assert fns_in_file(repo)["route"] == near
    gb = lock["code"]["generated_by"]["route"]
    assert [a["passed"] for a in gb["attempts"]] == [False] * 4 and gb["kept_attempt"] == 1
    assert gb["attempts"][0]["failures"] >= 1 and {"attempt", "passed", "failures"} <= set(gb["attempts"][1])
    assert "expected exactly one function named route; put helpers and imports inside it" in script.calls()[2]["prompt"]


def test_fenced_function_source_is_unwrapped(repo, script):
    script.answers([code_answer("route", "```python\n" + ROUTE + "\n```"), code_answer("move", "```\n" + MOVE + "\n```\n")])
    lock, issues = design.generate_code(repo, "destination", "d")
    assert not issues, issues
    assert len(script.calls()) == 2 and fns_in_file(repo) == {"route": ROUTE, "move": MOVE}
    assert stage(repo) == "code_generated"


def test_code_prompt_has_the_coder_guidance(repo):
    spec, lock = repo.get("destination", "d"), design.load_lock(repo, "destination", "d")
    one, posix, delivery = base.PROMPT_CHANGES_0_5["write_custom_step"]
    p_route = design._code_prompt("destination", spec, lock, STEPS[0], 0, "open")
    p_move = design._code_prompt("destination", spec, lock, STEPS[1], 1, "open")
    rule = design._imports_rule("open")
    assert rule + one + posix + "\nparams = run variables" in p_route and delivery not in p_route
    assert rule + one + posix + delivery + "\nparams = run variables" in p_move
    assert "helper functions and imports INSIDE it" in one and "never os.path" in posix and "params.get('reason:'" in delivery


# ---------------------------------------------------------------------------------------------- A8 per-step progress
def test_code_progress_kept_when_second_step_fails(repo, script):
    script.answers([code_answer("route", ROUTE),
                    {"task": "write_custom_step", "when": "Write the custom step `move`", "raise": "llm_limit", "message": "budget reached"}])
    lock, issues = design.generate_code(repo, "destination", "d")
    assert fns_in_file(repo) == {"route": ROUTE}
    assert stage(repo) == "code_failing"
    assert [p["msg"] for p in lock["code"]["problems"]] == ["missing custom step function(s) ['move']"]
    stop = [i for i in issues if i.code == "llm_limit"]
    assert stop and stop[0].severity == "error" and stop[0].msg == "the AI coder stopped at step move: budget reached"
    assert set(lock["code"]["generated_by"]) == {"route"}


def test_code_default_codes_only_missing_or_failing_steps(repo, script):
    script.answers([code_answer("route", ROUTE),
                    {"task": "write_custom_step", "when": "Write the custom step `move`", "times": 1, "raise": "llm_limit", "message": "x"},
                    code_answer("move", MOVE, times=None)])
    design.generate_code(repo, "destination", "d")
    lock, issues = design.generate_code(repo, "destination", "d")  # resumes: only the missing step
    calls = script.calls()
    assert len(calls) == 3 and "Write the custom step `move`" in calls[2]["prompt"], [c["prompt"][:200] for c in calls]
    assert not issues and stage(repo) == "code_generated" and fns_in_file(repo) == {"route": ROUTE, "move": MOVE}
    assert set(lock["code"]["generated_by"]) == {"route", "move"}
    # A12: the coder of the later step sees the transfers and the params the coded route step added
    assert "params added by the earlier steps" in calls[2]["prompt"] and '"reason:S1/cdar/a.txt": "rule 1"' in calls[2]["prompt"]
    assert '"target_key": "out/a.txt"' in calls[2]["prompt"]
    write_code(repo, {"route": ROUTE, "move": MOVE.replace('"MOVED"', '"MOVD"')})  # move now fails its tag case
    design.test_code(repo, "destination", "d")
    assert design.failing_steps(design.load_lock(repo, "destination", "d"),
                                design.code_path(repo, "destination", "d").read_text(encoding="utf-8")) == ["move"]
    script.answers([code_answer("move", MOVE)])
    design.generate_code(repo, "destination", "d")
    assert [c["prompt"].count("Write the custom step `move`") for c in script.calls()] == [1]


def test_code_step_flag_regenerates_one_step(repo, script):
    write_code(repo, {"route": ROUTE, "move": MOVE})
    design.test_code(repo, "destination", "d")
    route2 = ROUTE.replace("rule 1", "rule one")
    script.answers([code_answer("route", route2)])
    design.generate_code(repo, "destination", "d", steps=["route"])
    assert len(script.calls()) == 1 and fns_in_file(repo) == {"route": route2, "move": MOVE}
    with pytest.raises(PfError) as e:
        design.generate_code(repo, "destination", "d", steps=["nope"])
    assert e.value.code == "usage" and "['route', 'move']" in str(e.value)
    script.answers([code_answer("route", ROUTE), code_answer("move", MOVE)])
    design.generate_code(repo, "destination", "d", all_steps=True)
    assert len(script.calls()) == 2
    script.answers([code_answer("route", ROUTE), code_answer("move", MOVE)])
    design.generate_code(repo, "destination", "d")  # nothing missing or failing: everything
    assert len(script.calls()) == 2


# ---------------------------------------------------------------------------------------------- A12 _before
def test_before_uses_coded_earlier_steps(repo):
    spec = repo.get("destination", "d")
    without = design._before("destination", spec, STEPS, 1)
    assert "keys not computable yet" in json.dumps(without)
    got = design._before("destination", spec, STEPS, 1, {"route": ROUTE})
    state = got[0]["state"]
    assert [t["target_key"] for t in state["transfers (source bucket/key shown as placeholders)"]] == ["out/a.txt", "error/S1/x/b.txt"]
    assert state["params added by the earlier steps"] == {"reason:S1/cdar/a.txt": "rule 1", "reason:S1/x/b.txt": "no rule matched"}
    assert design._before("destination", spec, STEPS, 0, {"route": ROUTE}) == design._before("destination", spec, STEPS, 0)


# ---------------------------------------------------------------------------------------------- A14 structure
def test_structure_allows_comments_blank_lines_trailing_spaces_and_crlf(repo):
    lock = design.load_lock(repo, "destination", "d")
    src = design.render_module("destination", "d", lock, {"route": ROUTE, "move": MOVE})
    edited = src.replace("def route(", "# routes every file\n\n\ndef route(").replace("CUSTOM = {", "# the steps\nCUSTOM = {")
    edited = "\n".join(ln + "   " if ln.startswith("CUSTOM") else ln for ln in edited.splitlines()) + "\n\n\n"
    edited = edited.replace('"""Destination', '"""Edited docstring. Destination')
    assert design.structure_problems("destination", "d", lock, edited) == []
    assert design.structure_problems("destination", "d", lock, edited.replace("\n", "\r\n")) == []
    cp = design.code_path(repo, "destination", "d")
    cp.write_bytes(edited.replace("\n", "\r\n").encode("utf-8"))
    lock["code"] = {"file": cp.name, "plan_digest": lock["plan_digest"], "generated_by": {}, "generated_at": "now"}
    design.save_lock(repo, "destination", "d", lock)
    _, issues = design.test_code(repo, "destination", "d")
    assert not issues and stage(repo) == "code_generated"


def test_structure_refuses_top_level_helper(repo):
    lock = design.load_lock(repo, "destination", "d")
    src = design.render_module("destination", "d", lock, {"route": ROUTE, "move": MOVE})
    bad = {
        "helper": src.replace("def route(", "def helper():\n    return 1\n\n\ndef route("),
        "import": src.replace("from __future__ import annotations", "from __future__ import annotations\nimport os"),
        "statement": src + "\nprint('hi')\n",
        "decorator": src.replace("def route(", "@staticmethod\ndef route("),
        "signature": src.replace("def route(layout, params, ctx)", "def route(layout, params, ctx, extra=None)"),
        "custom": src.replace('"move": move}', '"move": move, "x": route}'),
        "custom_twice": src + '\nCUSTOM = {"route": route, "move": move}\n',
        "twice": src.replace("def move(", ROUTE + "\n\n\ndef move("),
    }
    for what, text in bad.items():
        probs = design.structure_problems("destination", "d", lock, text)
        assert probs and probs[0].startswith("only the bodies of the custom step functions may be edited"), what
        assert "nest helper functions and imports inside the step function" in probs[0] and "pkf code destination d" in probs[0]
    assert design.structure_problems("destination", "d", lock, src.replace("def move(", "def moved(")) == [
        "missing custom step function(s) ['move']"]


# ---------------------------------------------------------------------------------------------- B1 runner I/O
def test_runner_argv_has_utf8_mode(tmp_path, monkeypatch):
    argv = design.runner_argv(Path("r.py"), Path("m.py"), Path("j.json"), Path("o.jsonl"))
    assert argv[:4] == [sys.executable, "-I", "-X", "utf8"] and argv[-3:] == ["m.py", "j.json", "o.jsonl"]
    for k in ("LANG", "LC_ALL", "PYTHONUTF8"):
        monkeypatch.setenv(k, "C")
    env = design._child_env(str(tmp_path))
    assert not {"LANG", "LC_ALL", "PYTHONUTF8"} & set(env) and env["HOME"] == str(tmp_path)
    assert env["AWS_ACCESS_KEY_ID"] == "testing" and "AWS_SESSION_TOKEN" not in env


def test_runner_survives_undecodable_child_output(repo):
    noisy = ROUTE.replace('    for path in layout["files"]:',
                          '    import sys\n    sys.stdout.buffer.write(b"\\xe9\\xff PKF_RESULT {}\\n")\n    sys.stdout.flush()\n'
                          '    print("→ é", file=sys.stderr)\n    print("→ é")\n    for path in layout["files"]:', 1)
    cp = write_code(repo, {"route": noisy, "move": MOVE})
    res = design.run_tests_isolated("destination", repo.get("destination", "d"), cp, STEPS, CASES)
    assert [r["status"] for r in res] == ["pass", "pass", "pass", "pass"], res


# ---------------------------------------------------------------------------------------------- B2 block
BLOCK = design.RUNNER.split("from pkf_runtime import simulate")[0]


def _probe(before: str, after: str) -> list[str]:
    code = textwrap.dedent(before) + BLOCK + textwrap.dedent(after)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


def test_block_stops_popen_subclass_defined_before_the_block():
    got = _probe("""
        import subprocess
        class Early(subprocess.Popen):
            pass
        """, """
        import os
        for f in (lambda: Early(["echo", "x"]), lambda: subprocess.run(["echo", "x"]), lambda: subprocess.Popen(["echo"]),
                  lambda: os.system("echo x"), lambda: os.posix_spawn("/bin/echo", ["echo"], {})):
            try:
                f()
                print("NOT_BLOCKED")
            except PermissionError:
                print("blocked")
        import _posixsubprocess
        print("blocked" if _posixsubprocess.fork_exec.__name__ == "_no" else "NOT_BLOCKED")
        print("subclass_ok" if issubclass(Early, subprocess.Popen) and isinstance(subprocess.Popen, type) else "broken")
        """)
    assert got == ["blocked"] * 6 + ["subclass_ok"], got


def test_block_stops_multiprocessing_and_connect_ex():
    got = _probe("", """
        import multiprocessing, socket
        def checks():
            yield lambda: multiprocessing.Process(target=print).start()
            yield lambda: socket.socket().connect_ex(("10.255.255.1", 9))
            yield lambda: socket.socket().connect(("10.255.255.1", 9))
            yield lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("10.255.255.1", 9))
            yield lambda: socket.getaddrinfo("example.com", 443)
            yield lambda: socket.create_connection(("example.com", 443))
        for f in checks():
            try:
                f()
                print("NOT_BLOCKED")
            except PermissionError:
                print("blocked")
        print("local_ok" if socket.getaddrinfo("127.0.0.1", 9) else "x")
        """)
    assert got == ["blocked"] * 6 + ["local_ok"], got


# ---------------------------------------------------------------------------------------------- B5 exact bytes
def test_stale_pycache_cannot_mask_an_edit(repo, tmp_path):
    import py_compile

    spec = repo.get("destination", "d")
    good = write_code(repo, {"route": ROUTE, "move": MOVE})
    good_src = good.read_text(encoding="utf-8")
    bad_src = good_src.replace('"error/" + path', '"errxr/" + path')  # an edit of the same size
    assert len(bad_src) == len(good_src) and bad_src != good_src
    st = good.stat()
    py_compile.compile(str(good), cfile=str(good.parent / "__pycache__" / f"{good.stem}.cpython-{sys.version_info[0]}{sys.version_info[1]}.pyc"))
    good.write_text(bad_src, encoding="utf-8", newline="\n")
    os.utime(good, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size and mtime: a stale .pyc would be "valid"
    info: dict = {}
    res = design.run_tests_isolated("destination", spec, good, STEPS, {"route": CASES["route"]}, info=info)
    assert any(r["status"] == "fail" for r in res), res
    assert info["sha256"] == hashlib.sha256(good.read_bytes()).hexdigest()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "m.py").write_text(good_src, encoding="utf-8")
    design.run_tests_isolated("destination", spec, fresh / "m.py", STEPS, {})
    assert not (fresh / "__pycache__").exists()  # the runner writes no bytecode


def test_file_changed_while_tested_is_a_problem(repo, monkeypatch):
    cp = write_code(repo, {"route": ROUTE, "move": MOVE})
    real = design.run_tests_isolated

    def edit_during(*a, **k):
        out = real(*a, **k)
        cp.write_text(cp.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        return out
    monkeypatch.setattr(design, "run_tests_isolated", edit_during)
    lock, issues = design.test_code(repo, "destination", "d")
    assert "the file changed while it was being tested; run the tests again" in [p["msg"] for p in lock["code"]["problems"]]
    assert stage(repo) in ("code_changed", "code_failing")


# ---------------------------------------------------------------------------------------------- K17 same tree
def test_runner_child_imports_the_same_tree(repo, monkeypatch):
    import pkf
    import pkf_runtime

    cp = write_code(repo, {"route": ROUTE, "move": MOVE})
    spec = repo.get("destination", "d")
    info: dict = {}
    res = design.run_tests_isolated("destination", spec, cp, STEPS, {}, info=info)
    tree = Path(pkf.__file__).resolve().parents[1]
    assert Path(info["pkf_runtime_file"]).resolve().is_relative_to(tree), (info, tree)
    assert info["pkf_runtime_version"] == pkf_runtime.VERSION and design.runner_mismatch(info) is None
    assert all(r["status"] == "pass" for r in res)

    monkeypatch.setattr(pkf_runtime, "VERSION", "0.0.0-other")
    res = design.run_tests_isolated("destination", spec, cp, STEPS, {})
    assert len(res) == 1 and res[0]["code"] == "runner_mismatch" and res[0]["status"] == "fail"
    assert res[0]["detail"].startswith(f"the test runner loaded package-factory {info['pkf_runtime_version']} from ")
    assert ", this is 0.0.0-other from " in res[0]["detail"] and "reinstall with pip install -e " in res[0]["detail"]
    assert res[0]["detail"].endswith(" in the venv you run pkf from")
    _, issues = design.test_code(repo, "destination", "d")
    assert [i.code for i in issues] == ["runner_mismatch"]
    monkeypatch.setattr(pkf_runtime, "VERSION", info["pkf_runtime_version"])
    other = design.runner_mismatch({"pkf_runtime_version": pkf_runtime.VERSION, "pkf_runtime_file": "/old/site-packages/pkf_runtime/__init__.py"})
    assert other and "from /old/site-packages, this is" in other


# ============================================================================================== 0.5.0 Phase 2
def _no_lock(repo):
    """The same destination without any plan (for plan-time tests)."""
    design.lock_path(repo, "destination", "d").unlink()
    return repo


def plan_answer(steps=STEPS, ambiguities=()) -> dict:
    return {"task": "plan_component", "when": "Component kind: destination", "output": {
        "summary": "route then move", "assumptions": [], "ambiguities": list(ambiguities),
        "steps": [{"id": s["id"], "op": s["op"], "title": s["title"], "params_json": json.dumps(s["params"]),
                   "pseudocode": s.get("pseudocode", "")} for s in steps]}}


def case_json(c: dict, **over) -> dict:
    out = {"name": c["name"], "purpose": c["purpose"], "input_json": json.dumps(c["input"]), "params_json": "{}",
           "expected_json": json.dumps(c["expected"]), "expect_error": c["expect_error"]}
    return {**out, **over}


def writer_answer(step: str, cases: list[dict], times: int | None = None) -> dict:
    return {"task": "write_step_tests", "when": f"Write test cases for the custom step `{step}`", "times": times,
            "output": {"cases": cases}}


def errors_of(lock) -> list[dict]:
    return [i for i in lock.get("issues", []) if i["severity"] == "error"]


# ---------------------------------------------------------------------------------------------- A3 fences, plan issues
def test_fenced_case_kept_truncated_case_dropped_plan_still_approvable(repo, script):
    _no_lock(repo)
    fenced = case_json(CASES["move"][0], input_json="```json\n" + json.dumps(CASES["move"][0]["input"]) + "\n```")
    truncated = case_json(CASES["move"][1], name="truncated", input_json=json.dumps(CASES["move"][1]["input"])[:40])
    script.answers([plan_answer(), writer_answer("route", [case_json(CASES["route"][0])]), writer_answer("move", [fenced, truncated])])
    lock, issues = design.plan(repo, "destination", "d")
    assert [c["name"] for c in lock["plan"]["step_tests"]["move"]] == ["move_and_tag"]
    assert lock["plan"]["step_tests"]["move"][0]["input"] == CASES["move"][0]["input"]
    dropped = [i for i in issues if i.code == "invalid_test"]
    assert len(dropped) == 1 and dropped[0].severity == "warning" and "truncated" in dropped[0].msg
    assert lock["simulation"], "a warning must not empty the simulation"
    assert lock["issues"] and lock["issues"][0]["code"] == "invalid_test"
    assert design.plan_problems(repo, "destination", "d") == []
    assert len([c for c in script.calls() if c["task"] == "write_step_tests"]) == 2  # a dropped case is not a repair
    shown = design.show(repo, "destination", "d")
    assert "Plan issues:" in shown and "test case dropped" in shown
    design.approve_plan(repo, "destination", "d", "@tester")


def test_plan_errors_are_kept_and_named(repo, script):
    _no_lock(repo)
    bad = [{**STEPS[0], "id": "Route-Files"}, STEPS[1]]
    script.answers([plan_answer(bad)])
    lock, issues = design.plan(repo, "destination", "d")
    assert lock["simulation"] == [] and errors_of(lock)
    probs = design.plan_problems(repo, "destination", "d")
    assert probs[0][1] == "invalid_plan" and probs[0][0].startswith("the plan has errors: step id 'Route-Files' must be snake_case")
    assert "Plan issues:" in design.show(repo, "destination", "d")


# ---------------------------------------------------------------------------------------------- A4 case shapes
def test_validate_case_accepts_well_formed_cases_of_every_kind():
    for c in CASES["move"]:
        assert simulate.validate_case("destination", "custom_delivery", c) == [], c
    assert simulate.validate_case("destination", "custom", CASES["route"][0]) == []
    good = {
        "source": {"input": {"groups": [{"files": ["a.csv"]}], "listing": ["in/a.csv:10", "in/b:c.csv:5:30"], "contents": {}},
                   "expected": [["a.csv"]]},
        "trigger": {"input": {"events": [{"id": "e"}]}, "expected": [{"keys": ["in/a.csv"], "vars": {"x": "1"}}]},
        "transformation": {"input": {"files": ["a.csv:10"]}, "expected": ["a.csv", "x.zip!/a.csv"]},
        "destination": {"input": {"files": ["a.csv"], "vars": {"package": "p"}}, "expected": ["out/a.csv"]},
    }
    for kind, c in good.items():
        assert simulate.validate_case(kind, "custom", {"name": "c", "params": {}, "expect_error": False, **c}) == [], kind
        assert simulate.validate_case(kind, "custom", {"name": "c", "input": c["input"], "expected": None, "expect_error": True}) == []
    bad = {
        "destination": {"input": {"files": "a.csv"}, "expected": {"a": 1}},
        "source": {"input": {"groups": [{"files": "a"}], "listing": [":10"]}, "expected": ["a"]},
        "trigger": {"input": {"events": {}}, "expected": [["k"]]},
        "transformation": {"input": {"files": [1]}, "expected": "x"},
    }
    for kind, c in bad.items():
        assert len(simulate.validate_case(kind, "custom", {"name": "c", "expect_error": False, **c})) >= 2, kind
    move = CASES["move"][0]
    broken = [
        ({**move, "input": {**move["input"], "objects": {"b/in/x": "x"}}}, "bucket name 'b'"),
        ({**move, "input": {**move["input"], "transfers": [{"path": "p"}]}}, "lacks ['source_bucket'"),
        ({**move, "input": {**move["input"], "fail_calls": [{"operation": "Teleport"}]}}, "operation must be one of"),
        ({**move, "input": {**move["input"], "fail_calls": [{"operation": "CopyObject", "code": "Teapot"}]}}, "code must be one of"),
        ({**move, "expected": {}}, "expected must name the objects"),
        ({**move, "expected": {"statuses": {"other/path": "moved"}}}, "not transfer paths"),
    ]
    for case, text in broken:
        probs = simulate.validate_case("destination", "custom_delivery", case)
        assert any(text in p for p in probs), (text, probs)


def _bucket_b(case: dict) -> dict:
    inp = json.loads(json.dumps(case["input"]).replace(BUCKET, "b"))
    exp = json.loads(json.dumps(case["expected"]).replace(BUCKET, "b"))
    return case_json(case, input_json=json.dumps(inp), expected_json=json.dumps(exp))


def test_malformed_delivery_case_repaired_at_plan_time(repo, script):
    _no_lock(repo)
    script.answers([plan_answer(), writer_answer("route", [case_json(CASES["route"][0])]),
                    writer_answer("move", [_bucket_b(CASES["move"][0]), case_json(CASES["move"][1])], times=1),
                    writer_answer("move", [case_json(c) for c in CASES["move"]])])
    lock, issues = design.plan(repo, "destination", "d")
    calls = [c for c in script.calls() if c["task"] == "write_step_tests"]
    assert len(calls) == 3 and "Write test cases for the custom step `move`" in calls[2]["prompt"]
    assert "rejected by the framework's checks" in calls[2]["prompt"] and "bucket name 'b'" in calls[2]["prompt"]
    assert not [i for i in issues if i.code == "invalid_test"], issues
    assert lock["plan"]["step_tests"]["move"] == CASES["move"]
    assert design.plan_problems(repo, "destination", "d") == []


def test_malformed_case_twice_blocks_approval(repo, script):
    _no_lock(repo)
    script.answers([plan_answer(), writer_answer("route", [case_json(CASES["route"][0])]),
                    writer_answer("move", [_bucket_b(CASES["move"][0])])])
    lock, issues = design.plan(repo, "destination", "d")
    assert len([c for c in script.calls() if c["task"] == "write_step_tests"]) == 3  # route, move, one repair of move
    errs = [i for i in issues if i.code == "invalid_test" and i.severity == "error"]
    assert errs and "bucket name 'b'" in errs[0].msg and lock["simulation"]
    with pytest.raises(PfError) as e:
        design.approve_plan(repo, "destination", "d", "@tester")
    assert e.value.code == "invalid_test" and str(e.value).startswith("a test case is malformed: case 'move_and_tag' of step move")
    # a repair call that fails keeps the first answer and reports its problems
    script.answers([plan_answer(), writer_answer("route", [case_json(CASES["route"][0])]),
                    writer_answer("move", [_bucket_b(CASES["move"][0])], times=1)])
    lock, issues = design.plan(repo, "destination", "d")
    assert [c["name"] for c in lock["plan"]["step_tests"]["move"]] == ["move_and_tag"]
    assert {i.code for i in issues if i.severity == "error"} == {"invalid_test"}


def test_code_failing_next_says_how_to_fix_a_wrong_case(repo):
    write_code(repo, {"route": ROUTE.replace('"error/"', '"err/"'), "move": MOVE})
    design.test_code(repo, "destination", "d")
    st = design.status(repo, "destination", "d")
    assert st["stage"] == "code_failing"
    assert st["next"].endswith(" — if a test case itself is wrong, Reopen G3 and re-plan destination d")


# ---------------------------------------------------------------------------------------------- A5 / A6 decomposer
@pytest.fixture()
def pipe_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("PKF_PLUGIN_ROOT", str(ROOT / "steps-plugin"))
    root = tmp_path / "specs"
    (root / "pipelines").mkdir(parents=True)
    (root / "pkf.toml").write_text('spec_api_version = "pkf/v1"\n', encoding="utf-8")
    (root / "pipelines/p.yaml").write_text(yaml.safe_dump({
        "api_version": "pkf/v1", "kind": "pipeline", "name": "p", "owner": "data-eng",
        "requirement": "Every day copy every file from the source bucket to the destination bucket unchanged.\n"},
        sort_keys=False), encoding="utf-8")
    return root


def draft(name: str, config: dict) -> dict:
    return {"name": name, "reuse_existing": False, "config_json": json.dumps(config), "requirement": "", "examples_json": "[]"}


def split_answer(pex, dest_name="copy_out", times=None) -> dict:
    s3 = {"type": "s3", "connection": {"bucket": "<REQUIRED>", "region": "eu-west-1"}, "credentials": {"provider": "none"}, "params": {}}
    return {"task": "decompose_pipeline", "times": times, "output": {
        "summary": "copy daily", "trigger": draft("daily", {"schedule": "@daily"}), "source": draft("copy_in", s3),
        "has_transformation": False, "transformation": draft("none_needed", {}), "destination": draft(dest_name, s3),
        "package_name": "{source}_{ds_nodash}", "pipeline_examples_json": json.dumps(pex), "assumptions": [], "ambiguities": []}}


GOOD_PEX = [{"name": "one", "listing": ["in/a.csv:10"], "expected_keys": ["out/a.csv"]}]


def test_split_with_invalid_pipeline_examples_goes_to_repair(pipe_repo, script):
    from pkf import decompose

    bad = [{"name": "one", "listing": ["in/a.csv:10"], "expected_output": ["out/a.csv"]}]
    script.answers([split_answer(bad, times=1), split_answer(GOOD_PEX)])
    lock, issues = decompose.plan(load(pipe_repo), "p")
    calls = script.calls()
    assert len(calls) == 2 and "pipeline example 0: expected_output" in calls[1]["prompt"]
    assert lock["split"]["errors"] == [] and lock["split"]["pipeline_examples"] == GOOD_PEX
    script.answers([split_answer(bad), split_answer({"name": "x"})])
    lock, issues = decompose.plan(load(pipe_repo), "p")
    assert lock["split"]["errors"] and len(script.calls()) == 2
    with pytest.raises(PfError) as e:
        decompose.approve(load(pipe_repo), "p", "@owner")
    assert e.value.code == "invalid_split"
    script.answers([split_answer(GOOD_PEX + GOOD_PEX)])
    lock, _ = decompose.plan(load(pipe_repo), "p")
    assert any("used twice" in e["msg"] for e in lock["split"]["errors"])
    script.answers([split_answer({"name": "x"})])
    lock, _ = decompose.plan(load(pipe_repo), "p")
    assert any("must be a JSON list" in e["msg"] for e in lock["split"]["errors"])


def test_component_name_not_snake_case_is_repaired(pipe_repo, script):
    from pkf import decompose

    script.answers([split_answer(GOOD_PEX, dest_name="SAS-Restructured", times=1), split_answer(GOOD_PEX)])
    lock, issues = decompose.plan(load(pipe_repo), "p")
    calls = script.calls()
    assert len(calls) == 2 and "component name 'SAS-Restructured' is not snake_case" in calls[1]["prompt"]
    assert lock["split"]["errors"] == [] and lock["split"]["components"]["destination"]["name"] == "copy_out"
    assert not [i for i in issues if i.severity == "error"]


# ---------------------------------------------------------------------------------------------- A6 retries
def test_llm_schema_mismatch_retried_with_feedback(script, monkeypatch):
    from pkf.llm.schemas import CustomStepCode

    monkeypatch.setenv("PKF_PLUGIN_ROOT", str(ROOT / "steps-plugin"))
    script.answers([{"task": "write_custom_step", "times": 1, "output": {"function_source": "def f(): pass"}},
                    {"task": "write_custom_step", "output": {"function_source": "def f(): pass", "explanation": "ok"}}])
    out, meta = base.run_structured("write_custom_step", "writing-custom-step-code", "Write the custom step `f`", CustomStepCode)
    calls = script.calls()
    assert out.explanation == "ok" and len(calls) == 2
    assert calls[1]["prompt"].startswith("Write the custom step `f`\n\nYour previous answer did not match the required output schema:")
    assert "explanation" in calls[1]["prompt"] and calls[1]["prompt"].endswith("Answer again through the structured output.")
    assert meta.prompt_sha256 == calls[1]["prompt_sha256"]
    script.answers([{"task": "write_custom_step", "output": {"function_source": "def f(): pass"}}])
    with pytest.raises(PfError) as e:
        base.run_structured("write_custom_step", "writing-custom-step-code", "Write the custom step `f`", CustomStepCode)
    assert e.value.code == "llm_schema" and len(script.calls()) == 2 and str(e.value).endswith(base.AI_RETRY_HINT)


def test_no_skill_retried_once(monkeypatch):
    from pkf.llm import agent_sdk

    calls = []

    async def fake_run(skill, prompt, schema, task=None, tools=None):
        calls.append(prompt)
        if len(calls) == 1:
            raise PfError("the agent answered without successfully loading the skill x", "no_skill")
        return {"ok": True}, "m", {"cost_usd": 0.0, "turns": 1}
    monkeypatch.setattr(agent_sdk, "_run", fake_run)
    assert agent_sdk.call("s", "p", {}, "plan_component")[0] == {"ok": True} and len(calls) == 2

    async def never(skill, prompt, schema, task=None, tools=None):
        calls.append(prompt)
        raise PfError("the agent answered without successfully loading the skill x", "no_skill")
    calls.clear()
    monkeypatch.setattr(agent_sdk, "_run", never)
    with pytest.raises(PfError) as e:
        agent_sdk.call("s", "p", {}, "plan_component")
    assert e.value.code == "no_skill" and len(calls) == 2 and str(e.value).endswith(base.AI_RETRY_HINT)

    async def limit(skill, prompt, schema, task=None, tools=None):
        calls.append(prompt)
        raise PfError("stopped", "llm_limit")
    calls.clear()
    monkeypatch.setattr(agent_sdk, "_run", limit)
    with pytest.raises(PfError):
        agent_sdk.call("s", "p", {}, "plan_component")
    assert len(calls) == 1  # limits are never retried


# ---------------------------------------------------------------------------------------------- A9 / A10 / A11 analyst
REQ = "TRIGGER\n- daily\nSOURCE\n- bucket <REQUIRED>\nTRANSFORMATION\n- none\nDESTINATION\n- copy unchanged\nDELIVERY BEHAVIOUR\n- skip identical\n"


def analyst_answer(examples, name="chosen_name", requirement=REQ, questions=(), times=None) -> dict:
    return {"task": "analyze_requirements", "times": times, "output": {
        "pipeline_name": name, "description": "copy daily", "requirement": requirement, "pseudocode": "1. copy",
        "examples_json": examples if isinstance(examples, str) else json.dumps(examples), "assumptions": [],
        "open_questions": list(questions)}}


@pytest.fixture()
def empty_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("PKF_PLUGIN_ROOT", str(ROOT / "steps-plugin"))
    root = tmp_path / "specs"
    root.mkdir()
    (root / "pkf.toml").write_text('spec_api_version = "pkf/v1"\n', encoding="utf-8")
    return root


def analyze(root, text="Copy files daily.", name="p", owner="me"):
    from pkf import analyst

    return analyst.analyze(load(root), text, name, owner)


def pipeline_doc(root, name="p") -> dict:
    return yaml.safe_load((root / "pipelines" / f"{name}.yaml").read_text(encoding="utf-8"))


EX = [{"name": f"e{i}", "listing": [f"in/{i}.csv:10"], "expected_keys": [f"out/{i}.csv"]} for i in range(3)]


def test_analyst_keeps_valid_examples_and_never_wipes(empty_repo, script):
    broken = [EX[0], {**EX[1], "expected_output": ["x"]}, EX[2]]
    script.answers([analyst_answer("```json\n" + json.dumps(broken) + "\n```")])
    data, issues = analyze(empty_repo)
    assert [e["name"] for e in pipeline_doc(empty_repo)["examples"]] == ["e0", "e2"] and data["examples"] == 2
    dropped = [i for i in issues if i.code == "invalid_examples"]
    assert len(dropped) == 1 and dropped[0].severity == "warning" and "e1" in dropped[0].msg
    load(empty_repo).get("pipeline", "p")  # the file is valid
    for answer in ("not json", [{"name": "x", "nope": 1}], {"name": "x"}):
        script.answers([analyst_answer(answer)])
        _, issues = analyze(empty_repo)
        assert [e["name"] for e in pipeline_doc(empty_repo)["examples"]] == ["e0", "e2"], answer
        assert any("the analyst returned no valid examples; the existing 2 are kept" == i.msg for i in issues)


def test_analyst_drops_duplicate_example_names(empty_repo, script):
    script.answers([analyst_answer([EX[0], EX[1], {**EX[0], "listing": ["in/z.csv:1"]}])])
    _, issues = analyze(empty_repo)
    assert [e["name"] for e in pipeline_doc(empty_repo)["examples"]] == ["e0", "e1"]
    assert any("appears twice" in i.msg for i in issues)
    load(empty_repo).get("pipeline", "p")


def test_analyst_refusal_writes_nothing(empty_repo, script):
    script.answers([analyst_answer(EX, requirement="I'm sorry, but I can't help with that.")])
    with pytest.raises(PfError) as e:
        analyze(empty_repo)
    assert e.value.code == "llm_schema" and "no TRIGGER / SOURCE / DESTINATION sections" in str(e.value)
    assert "Analyze with AI again" in str(e.value)
    assert not (empty_repo / "pipelines").exists() and not (empty_repo / "requirements" / "p").exists()
    script.answers([analyst_answer(EX, requirement="## 1. TRIGGER\n- daily\n**SOURCE**\n- s3\nDESTINATION:\n- s3\n")])
    analyze(empty_repo)  # headings with numbers and Markdown are sections too


def test_analyst_ignores_pipeline_name_when_name_given(empty_repo, script):
    script.answers([analyst_answer(EX, name="sas-folder-restructure")])
    data, _ = analyze(empty_repo, name="p")
    assert data["pipeline"] == "p" and (empty_repo / "pipelines/p.yaml").exists()
    with pytest.raises(PfError) as e:
        analyze(empty_repo, name=None)
    assert e.value.code == "llm_schema" and "'sas-folder-restructure', which is not snake_case" in str(e.value)
    script.answers([analyst_answer(EX, name="chosen_name")])
    assert analyze(empty_repo, name=None)[0]["pipeline"] == "chosen_name"


def test_analysis_records_requirement_sha256(empty_repo, script):
    from pkf import analyst

    script.answers([analyst_answer(EX, questions=["Files over 5 GiB: copy or skip?"])])
    analyze(empty_repo)
    analysis = json.loads((empty_repo / "requirements/p/analysis.json").read_text(encoding="utf-8"))
    current = load(empty_repo).get("pipeline", "p").requirement
    assert analysis["requirement_sha256"] == hashlib.sha256(current.strip().encode("utf-8")).hexdigest()
    assert analysis["requirement_sha256"] == analyst.requirement_sha256(current)
    assert analysis["open_questions"] == ["Files over 5 GiB: copy or skip?"]
    assert b"\r\n" not in (empty_repo / "requirements/p/analysis.json").read_bytes()


def test_analyst_merges_structured_settings_from_yaml_document(empty_repo, script):
    document = textwrap.dedent("""\
        name: sas_folder_restructure
        owner: data-eng
        start_date: 2026-10-01
        retries: 2
        retry_delay_minutes: 2
        tags: [sas, folder-restructure]
        max_active_runs: 99
        requirement: |
          Move SAS files into the study folders.
        """)
    script.answers([analyst_answer(EX)])
    data, _ = analyze(empty_repo, text=document, owner="jsandy16")
    doc = pipeline_doc(empty_repo)
    assert doc["owner"] == "data-eng" and doc["retries"] == 2 and doc["retry_delay_minutes"] == 2
    assert doc["tags"] == ["sas", "folder-restructure"] and str(doc["start_date"]) == "2026-10-01"
    assert "max_active_runs" not in doc  # 99 is not a valid value: left out
    assert data["settings_from_document"] == {"owner": "data-eng", "start_date": "2026-10-01", "retries": 2,
                                              "retry_delay_minutes": 2, "tags": ["sas", "folder-restructure"]}
    p = load(empty_repo).get("pipeline", "p")
    assert p.retry_delay_minutes == 2 and str(p.start_date) == "2026-10-01"
    script.answers([analyst_answer(EX)])
    analyze(empty_repo, text="Plain prose: retries: none.", owner="someone")
    assert pipeline_doc(empty_repo)["retry_delay_minutes"] == 2  # an existing file keeps its settings


# ---------------------------------------------------------------------------------------------- A15 time-outs
HANG = MOVE.replace("    results = []\n", "    results = []\n    if any('/hang/' in t['path'] for t in transfers):\n"
                    "        import time\n        time.sleep(600)\n", 1)


def test_hanging_case_is_named_and_others_kept(repo, monkeypatch):
    cp = write_code(repo, {"route": ROUTE, "move": HANG})
    spec = repo.get("destination", "d")
    hang = {"name": "hangs", "purpose": "", "params": {}, "expect_error": False,
            "input": {"objects": {f"{BUCKET}/in/S1/hang/a.txt": "x"}, "transfers": [transfer("S1/hang/a.txt", "in/S1/hang/a.txt")]},
            "expected": {"statuses": {"S1/hang/a.txt": "moved"}}}
    monkeypatch.setenv("PKF_TEST_TIMEOUT_S", "5")
    res = design.run_tests_isolated("destination", spec, cp, STEPS, {"route": CASES["route"], "move": [hang, *CASES["move"]]})
    by = {(r.get("example") or f"{r.get('step')}:{r.get('case')}"): (r["status"], r["detail"]) for r in res}
    assert by["two"][0] == "pass" and by["route:both_rules"][0] == "pass"
    assert by["move:hangs"] == ("fail", "timed out after 5s while running move:hangs")
    assert by["move:move_and_tag"] == ("fail", "not run (an earlier case timed out)")
    assert by["move:copy_fails_source_kept"] == ("fail", "not run (an earlier case timed out)")
    assert len(res) == 5


def test_test_timeout_scales_with_the_cases(monkeypatch):
    monkeypatch.delenv("PKF_TEST_TIMEOUT_S", raising=False)
    assert design.test_timeout(STEPS, CASES, 1) == 120
    many = {"move": CASES["move"] * 10, "route": CASES["route"] * 20}
    assert design.test_timeout(STEPS, many, 5) == 60 + 10 * 20 + 2 * (20 + 5)
    monkeypatch.setenv("PKF_TEST_TIMEOUT_S", "7")
    assert design.test_timeout(STEPS, many, 5) == 7


# ---------------------------------------------------------------------------------------------- A16 redaction
def test_redaction_keeps_ordinary_wording():
    for text in ("Credentials: the password is stored in AWS Secrets Manager under pkf/sas-sftp.",
                 "Split each file name on the token: underscore; keep the access key: study id.",
                 "The secret = something"):
        assert base.redact_secrets(text) == (text, 0), text
    got, n = base.redact_secrets("login with password: Hunter2secret! and key AKIAABCDEFGHIJKLMNOP")
    assert n == 2 and "Hunter2secret" not in got and "AKIA" not in got and "password: [REDACTED]" in got


def test_redaction_is_a_warning_of_the_plan(repo, script, capfd):
    _no_lock(repo)
    spec_file = repo.root / "destinations/d.yaml"
    doc = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
    doc["requirement"] += "Use the api key: s3cr3t-Value-42 for nothing.\n"
    spec_file.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    script.answers([plan_answer(), writer_answer("route", [case_json(CASES["route"][0])]),
                    writer_answer("move", [case_json(c) for c in CASES["move"]])])
    _, issues = design.plan(load(repo.root), "destination", "d")
    red = [i for i in issues if i.code == "redacted"]
    assert red and red[0].severity == "warning" and red[0].msg.startswith("1 value(s) were redacted before the AI saw the text")
    assert "1 value(s) were redacted before the AI saw the text" in capfd.readouterr().err
    assert all("s3cr3t" not in c["prompt"] for c in script.calls())


# ---------------------------------------------------------------------------------------------- B9 POSIX warnings
def test_guard_warns_os_path_under_open_policy(repo, monkeypatch):
    uses = {
        "os.path": ROUTE.replace('path.rsplit("/", 1)[-1]', '__import__("os").path.basename(path)').replace(
            "def route(layout, params, ctx):\n", "def route(layout, params, ctx):\n    import os\n").replace(
            '__import__("os").path', "os.path"),
        "pathlib": ROUTE.replace("def route(layout, params, ctx):\n", "def route(layout, params, ctx):\n    from pathlib import PurePath\n"),
        "ntpath": ROUTE.replace("def route(layout, params, ctx):\n", "def route(layout, params, ctx):\n    import ntpath\n"),
        "os.sep": ROUTE.replace("def route(layout, params, ctx):\n", "def route(layout, params, ctx):\n    import os\n    s = os.sep\n"),
    }
    for what, src in uses.items():
        assert design.guard_warnings(src) == [design.POSIX_WARNING], what
    assert design.guard_warnings(ROUTE) == [] and design.guard_warnings(MOVE) == []
    write_code(repo, {"route": uses["os.path"], "move": MOVE})
    lock, issues = design.test_code(repo, "destination", "d")
    warn = [i for i in issues if i.code == "posix_paths"]
    assert len(warn) == 1 and warn[0].severity == "warning" and warn[0].loc == "route" and "posixpath" in warn[0].msg
    assert lock["code"]["problems"] == [] and stage(repo) == "code_generated"  # advice only
    monkeypatch.setenv("PKF_CODE_POLICY", "restricted")  # restricted: os is refused outright, no separate advice
    _, issues = design.test_code(repo, "destination", "d")
    assert not [i for i in issues if i.code == "posix_paths"]


def test_guard_warning_reaches_the_coder_feedback(repo, script):
    bad = ROUTE.replace('"error/"', '"err/"').replace("def route(layout, params, ctx):\n", "def route(layout, params, ctx):\n    import os\n    _ = os.sep\n")
    script.answers([code_answer("route", bad), code_answer("route", ROUTE)])
    design.generate_code(repo, "destination", "d", steps=["route"])
    calls = script.calls()
    assert len(calls) == 2 and "guard_warnings" in calls[1]["prompt"] and "posixpath or str.split('/')" in calls[1]["prompt"]


# ---------------------------------------------------------------------------------------------- B12 listing
def test_listing_keys_with_colons():
    from pkf.specs.design import SourceExample

    assert simulate._listing(["in/a:b.csv:10", "in/2024-01-01T10:00:00.csv", "x:12:30", "y:5:1.5", "z"]) == {
        "in/a:b.csv": (10, 3600.0), "in/2024-01-01T10:00:00.csv": (64, 3600.0), "x": (12, 30.0), "y": (5, 1.5), "z": (64, 3600.0)}
    ex = SourceExample.model_validate({"name": "c", "listing": ["in/a:b.csv:10", "in/t10:00.csv"], "expected_packages": [["a:b.csv"]]})
    assert ex.listing == ["in/a:b.csv:10", "in/t10:00.csv"]
    for bad in ("../x:1", "/abs:1", ":10"):
        with pytest.raises(ValueError):
            SourceExample.model_validate({"name": "c", "listing": [bad]})
    steps = [{"id": "list_root", "op": "list", "title": "", "params": {"root": "in/"}}]
    groups = simulate.source_groups(steps, {}, {"listing": ["in/S1/a:b.csv:10"]}, {})
    assert [(f["path"], f["size"]) for g in groups for f in g["files"]] == [("S1/a:b.csv", 10)]


# ---------------------------------------------------------------------------------------------- B16 no package files
def test_custom_delivery_pipeline_examples_write_no_files(monkeypatch):
    def no_files(*a, **k):
        raise AssertionError("a custom_delivery pipeline example built package files")
    monkeypatch.setattr(simulate, "build_package", no_files)
    custom = {"route": _fn(ROUTE), "move": _fn(MOVE)}
    parts = {"trigger": {"steps": [{"id": "tick", "op": "schedule", "params": {}}]},
             "source": {"steps": [{"id": "list_root", "op": "list", "title": "", "params": {"root": "in/"}}]},
             "destination": {"steps": STEPS, "custom": custom}, "package_name": "pk"}
    ex = {"name": "e", "listing": ["in/S1/cdar/a.txt:5", "in/S1/x/b.txt:5"], "expected_keys": ["out/a.txt", "error/S1/x/b.txt"]}
    r = simulate.simulate_pipeline(parts, ex)
    assert r["status"] == "pass", r
    r = simulate.simulate_pipeline({**parts, "destination": {"steps": STEPS, "custom": {}}}, ex)
    assert r["status"] == "needs_code"


# ---------------------------------------------------------------------------------------------- B18 child env
def test_child_env_has_windows_home_variables(tmp_path, monkeypatch):
    for k, v in {"WINDIR": r"C:\Windows", "SYSTEMDRIVE": "C:", "COMSPEC": r"C:\Windows\cmd.exe", "USERPROFILE": r"C:\Users\me",
                 "APPDATA": r"C:\Users\me\AppData\Roaming", "SECRET_THING": "x"}.items():
        monkeypatch.setenv(k, v)
    env = design._child_env(str(tmp_path))
    assert env["USERPROFILE"] == env["APPDATA"] == env["LOCALAPPDATA"] == env["HOME"] == str(tmp_path)
    assert env["WINDIR"] == r"C:\Windows" and env["SYSTEMDRIVE"] == "C:" and env["COMSPEC"] == r"C:\Windows\cmd.exe"
    assert "SECRET_THING" not in env


# ---------------------------------------------------------------------------------------------- K13 rejected files
def test_simulated_source_reports_rejected_files(monkeypatch):
    from pkf_runtime import discovery

    real = discovery.run_source_steps

    def with_rejected(groups, steps, ctx, src, custom=None):  # what WP2's run_source_steps produces for a bad key (K13)
        out = real(groups, steps, ctx, src, custom)
        for g in out:
            g.setdefault("rejected", []).append({"path": "S1/../x.csv", "key": "in/S1/../x.csv", "error": "path leaves the package"})
        return out
    monkeypatch.setattr(simulate, "run_source_steps", with_rejected)
    steps = [{"id": "list_root", "op": "list", "title": "", "params": {"root": "in/"}}]
    ex = {"name": "e", "listing": ["in/a.csv:5"], "expected_packages": [["a.csv"]]}
    r = simulate.simulate_source(steps, {}, ex, {})
    assert r["status"] == "fail" and r["detail"] == "file S1/../x.csv: path leaves the package" and r["output"] == [["a.csv"]]
    assert simulate.simulate_source(steps, {}, {**ex, "expect_error": True}, {})["status"] == "pass"
    parts = {"trigger": {"steps": [{"id": "tick", "op": "schedule", "params": {}}]}, "source": {"steps": steps},
             "destination": {"steps": [{"id": "keep", "op": "prefix", "title": "", "params": {"prefix": "out/"}}]}, "package_name": "pk"}
    r = simulate.simulate_pipeline(parts, {"name": "e", "listing": ["in/a.csv:5"], "expected_keys": ["out/a.csv"]})
    assert r["status"] == "fail" and "file S1/../x.csv: path leaves the package" in r["detail"] and r["output"] == ["out/a.csv"]
    monkeypatch.setattr(simulate, "run_source_steps", real)
    assert simulate.simulate_source(steps, {}, ex, {})["status"] == "pass"  # groups without the key behave as before


# ---------------------------------------------------------------------------------------------- Phase 1 review fixes
def test_failed_regenerate_keeps_the_existing_function(repo, script):
    """R-phase1 #1: a Regenerate whose AI call fails leaves the file byte for byte and keeps the code approval."""
    write_code(repo, {"route": ROUTE, "move": MOVE})
    design.test_code(repo, "destination", "d")
    design.approve_code(repo, "destination", "d", "@reviewer")
    cp = design.code_path(repo, "destination", "d")
    before = cp.read_bytes()
    fail = {"task": "write_custom_step", "raise": "llm_error", "message": "network down"}
    for kw in ({"steps": ["move"]}, {"all_steps": True}, {}):
        script.answers([fail])
        lock, issues = design.generate_code(repo, "destination", "d", **kw)
        assert cp.read_bytes() == before, kw
        assert lock.get("code_approval") and stage(repo) == "approved", kw
        assert [i.code for i in issues if i.severity == "error"] == ["llm_error"], kw
    # --all: the first step gets new code, the coder stops at the second: the second keeps its old function
    route2 = ROUTE.replace("rule 1", "rule one")
    script.answers([code_answer("route", route2), fail])
    design.generate_code(repo, "destination", "d", all_steps=True)
    assert fns_in_file(repo) == {"route": route2, "move": MOVE}


def test_runner_mismatch_stops_the_coder_at_once(repo, script, monkeypatch):
    """R-phase1 #8: an environment error is not fed back to the model as a failed attempt."""
    import pkf_runtime

    write_code(repo, {"route": ROUTE, "move": MOVE})
    design.test_code(repo, "destination", "d")
    before = design.code_path(repo, "destination", "d").read_bytes()
    monkeypatch.setattr(pkf_runtime, "VERSION", "0.0.0-other")
    script.answers([code_answer("route", ROUTE.replace("rule 1", "rule one"), times=None)])
    lock, issues = design.generate_code(repo, "destination", "d", steps=["route"])
    assert len(script.calls()) == 1
    stop = [i for i in issues if i.loc == "route" and i.code == "runner_mismatch"]
    assert stop and stop[0].msg.startswith("the AI coder stopped at step route: the test runner loaded package-factory")
    assert design.code_path(repo, "destination", "d").read_bytes() == before


FORGER = ROUTE.replace('    for path in layout["files"]:', '''    import glob, json, os, sys
    for f in glob.glob(os.path.join(os.getcwd(), "*")) + list(sys.argv):
        if f.endswith((".jsonl", ".json")):
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"event": "result", "step": "move", "case": "forged", "status": "pass"}) + "\\n")
    for path in layout["files"]:''', 1)


def test_code_under_test_cannot_forge_runner_events(repo):
    """R-phase1 #10 (best effort): events without the run's token are ignored; the paths are not in sys.argv."""
    cp = write_code(repo, {"route": FORGER, "move": MOVE})
    res = design.run_tests_isolated("destination", repo.get("destination", "d"), cp, STEPS, CASES)
    assert "forged" not in [r.get("case") for r in res], res
    assert [r["status"] for r in res] == ["pass", "pass", "pass", "pass"], res
    probe = ROUTE.replace('    for path in layout["files"]:', '    import sys\n    layout["vars"]["argv"] = str(len(sys.argv))\n'
                          '    for path in layout["files"]:', 1)
    state = design._before("destination", repo.get("destination", "d"), STEPS, 1, {"route": probe})
    seen = state[0]["state"]["params added by the earlier steps"]
    assert seen["argv"] == "1"
