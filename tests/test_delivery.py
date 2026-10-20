"""Custom delivery (0.3.0): a generated function delivers the files itself with AWS clients — server-side copy,
check, delete the source (a move), tags — and the open code policy. In-process against moto; the component plans and
code are written by hand here (what the agents would produce), approved through the same functions as the CLI."""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from pkf import design
from pkf.cli import main
from pkf.repo import load
from pkf_runtime import simulate

REGION = "eu-north-1"
BUCKET = "sas-dstore-test"

ROUTE = '''def route_keys(layout, params, ctx):
    for path in layout["files"]:
        study, rest = path.split("/", 1)
        if "/cdar/" in "/" + rest:
            layout["keys"][path] = study + "/primary/" + rest.rsplit("/", 1)[-1]
        else:
            layout["keys"][path] = "error/" + path
    return layout'''

MOVE = '''def move_files(transfers, params, ctx, aws):
    from botocore.exceptions import ClientError
    s3 = aws.s3_destination
    results = []
    for t in transfers:
        src = {"Bucket": t["source_bucket"], "Key": t["source_key"]}
        try:
            s3.copy(src, t["target_bucket"], t["target_key"])
            s3.head_object(Bucket=t["target_bucket"], Key=t["target_key"])
        except ClientError as exc:
            results.append({"path": t["path"], "key": t["target_key"], "status": "failed", "detail": str(exc)})
            continue
        s3.delete_object(**src)
        status = "moved_to_error" if t["target_key"].startswith("error/") else "moved"
        if status == "moved":
            try:
                s3.put_object_tagging(Bucket=t["target_bucket"], Key=t["target_key"],
                                      Tagging={"TagSet": [{"Key": "ADDITIONAL_TAG", "Value": t["path"]}]})
            except ClientError as exc:
                aws.log.warning("tagging failed for %s: %s", t["target_key"], exc)
        results.append({"path": t["path"], "key": t["target_key"], "status": status, "detail": ""})
    return results'''

STEPS = [
    {"id": "route_keys", "op": "custom", "title": "Route by path rules", "params": {"function": "route_keys"},
     "pseudocode": "1. study = first segment\n2. /cdar/ → {study}/primary/{file}\n3. otherwise error/{path}"},
    {"id": "move_files", "op": "custom_delivery", "title": "Copy, check, delete source, tag", "params": {"function": "move_files"},
     "pseudocode": "1. copy server-side\n2. head the target\n3. delete the source\n4. tag ADDITIONAL_TAG = path"},
]

CASES = {
    "route_keys": [{"name": "cdar_and_error", "purpose": "both rules", "input": {"files": ["S1/r/cdar/a.txt", "S1/x/b.txt"]}, "params": {},
                    "expected": ["S1/primary/a.txt", "error/S1/x/b.txt"], "expect_error": False}],
    "move_files": [
        {"name": "move_and_tag", "purpose": "normal move", "params": {}, "expect_error": False,
         "input": {"objects": {f"{BUCKET}/incoming/S1/r/cdar/a.txt": "x"},
                   "transfers": [{"path": "S1/r/cdar/a.txt", "source_bucket": BUCKET, "source_key": "incoming/S1/r/cdar/a.txt", "size": 1,
                                  "target_bucket": BUCKET, "target_key": "S1/primary/a.txt"}]},
         "expected": {"objects": [f"{BUCKET}/S1/primary/a.txt"], "tags": {f"{BUCKET}/S1/primary/a.txt": {"ADDITIONAL_TAG": "S1/r/cdar/a.txt"}},
                      "statuses": {"S1/r/cdar/a.txt": "moved"}}},
        {"name": "copy_fails_source_kept", "purpose": "failure leaves the source", "params": {}, "expect_error": False,
         "input": {"objects": {f"{BUCKET}/incoming/S1/r/cdar/a.txt": "x"},
                   "transfers": [{"path": "S1/r/cdar/a.txt", "source_bucket": BUCKET, "source_key": "incoming/S1/r/cdar/a.txt", "size": 1,
                                  "target_bucket": BUCKET, "target_key": "S1/primary/a.txt"}],
                   "fail_calls": [{"operation": "CopyObject", "key": "S1/primary/a.txt"}]},
         "expected": {"objects": [f"{BUCKET}/incoming/S1/r/cdar/a.txt"], "statuses": {"S1/r/cdar/a.txt": "failed"}}},
    ],
}


def write(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(doc, sort_keys=False))


def pkf(repo: Path, *args: str, ok: bool | None = True) -> dict:
    buf, cwd = io.StringIO(), os.getcwd()
    os.chdir(repo)
    try:
        with redirect_stdout(buf):
            main(["--json", *args])
    finally:
        os.chdir(cwd)
    doc = json.loads(buf.getvalue())
    if ok is not None:
        assert doc["ok"] is ok, json.dumps(doc, indent=1)[:3000]
    return doc


def design_by_hand(root: Path, kind: str, name: str, steps: list[dict], cases: dict, functions: dict[str, str]) -> None:
    """What `pkf plan` + `pkf code` + both approvals produce, with a hand-written plan and code."""
    repo = load(root)
    spec = repo.get(kind, name)
    lock = {"lock_version": 2, "kind": kind, "name": name, "requirement_sha256": design.requirement_sha(kind, spec),
            "plan": {"summary": "hand-written", "steps": steps, "assumptions": [], "ambiguities": [], "step_tests": cases},
            "simulation": design.run_examples_inprocess(kind, spec, steps)}
    lock["plan_digest"] = design.plan_digest(lock)
    design.save_lock(repo, kind, name, lock)
    design.approve_plan(repo, kind, name, "@tester")
    if not functions:
        return
    lock = design.load_lock(repo, kind, name)
    design.code_path(repo, kind, name).write_text(design.render_module(kind, name, lock, functions), encoding="utf-8", newline="\n")
    lock["code"] = {"file": f"{name}.py", "plan_digest": lock["plan_digest"], "generated_by": {}, "generated_at": "now"}
    design.save_lock(repo, kind, name, lock)
    _, issues = design.test_code(repo, kind, name)
    assert not [i for i in issues if i.severity == "error"], issues
    design.approve_code(repo, kind, name, "@reviewer")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PKF_STAGING_URL", raising=False)
    monkeypatch.delenv("PKF_CODE_POLICY", raising=False)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        repo = tmp_path / "specs"
        repo.mkdir()
        (repo / "pkf.toml").write_text(f'spec_api_version = "pkf/v1"\n[staging]\nurl = "file://{tmp_path / "staging"}"\n'
                                       '[airflow]\ndags_folder = "dags"\n[agent]\ncode_policy = "open"\n')
        conn = {"bucket": BUCKET, "region": REGION}
        write(repo / "sources/sas_incoming.yaml", {"api_version": "pkf/v1", "kind": "source", "name": "sas_incoming", "type": "s3",
                                                   "connection": conn, "steps": [
                                                       {"id": "all", "op": "list", "title": "Everything under incoming/", "params": {"root": "incoming/"}},
                                                       {"id": "one_each", "op": "split", "title": "One package per file",
                                                        "params": {"by": "regex", "regex": "(.+)", "var": "file"}}]})
        write(repo / "destinations/sas_layout.yaml", {"api_version": "pkf/v1", "kind": "destination", "name": "sas_layout", "type": "s3",
                                                      "connection": conn, "requirement": "Route and move every file.\n",
                                                      "examples": [{"name": "cdar", "files": ["S1/r/cdar/a.txt"], "expected_keys": ["S1/primary/a.txt"]}]})
        write(repo / "connections/sas_move.yaml", {"api_version": "pkf/v1", "kind": "connection", "name": "sas_move",
                                                   "source": "sas_incoming", "destination": "sas_layout"})
        write(repo / "pipelines/sas_restructure.yaml", {"api_version": "pkf/v1", "kind": "pipeline", "name": "sas_restructure", "owner": "t",
                                                        "connection": "sas_move", "schedule": "*/45 * * * *", "start_date": "2026-10-01",
                                                        "package_name": "{file}",
                                                        "examples": [{"name": "two", "listing": ["incoming/S1/r/cdar/a.txt:1", "incoming/S1/x/b.txt:1"],
                                                                      "expected_keys": ["S1/primary/a.txt", "error/S1/x/b.txt"]}]})
        design_by_hand(repo, "destination", "sas_layout", STEPS, CASES, {"route_keys": ROUTE, "move_files": MOVE})
        yield {"repo": repo, "s3": s3}


def _keys(s3) -> list[str]:
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def test_move_tag_and_error_prefix_end_to_end(env):
    repo, s3 = env["repo"], env["s3"]
    pkf(repo, "test", "pipeline", "sas_restructure")
    pkf(repo, "build", "sas_restructure")
    module = (repo / "dags/pkf/sas_restructure/components/destination.py").read_text()
    assert "def move_files(transfers, params, ctx, aws):" in module
    s3.put_object(Bucket=BUCKET, Key="incoming/S1/r/cdar/a.txt", Body=b"PK1", Tagging="OLD=1")
    s3.put_object(Bucket=BUCKET, Key="incoming/S1/x/b.txt", Body=b"B")
    doc = pkf(repo, "run", "sas_restructure", "--date", "2026-10-17")
    report = doc["data"]["tasks"][-1]["result"]
    assert report["delivered"] == 2 and report["statuses"] == {"moved": 1, "moved_to_error": 1}
    assert _keys(s3) == ["S1/primary/a.txt", "error/S1/x/b.txt"]  # sources deleted: a move
    tags = s3.get_object_tagging(Bucket=BUCKET, Key="S1/primary/a.txt")["TagSet"]
    assert tags == [{"Key": "ADDITIONAL_TAG", "Value": "S1/r/cdar/a.txt"}]
    assert s3.get_object(Bucket=BUCKET, Key="S1/primary/a.txt")["Body"].read() == b"PK1"
    extract = next(t for t in doc["data"]["tasks"] if t["task"].endswith(".extract"))
    assert extract["result"]["direct"] is True and extract["result"]["stage"] is None  # nothing staged
    doc = pkf(repo, "run", "sas_restructure", "--date", "2026-10-17")  # incoming/ is empty now: a no-op
    assert doc["data"]["tasks"][-1]["result"]["packages"] == 0


def test_failed_file_fails_the_run_and_keeps_its_source(env, monkeypatch):
    repo, s3 = env["repo"], env["s3"]
    pkf(repo, "build", "sas_restructure")
    s3.put_object(Bucket=BUCKET, Key="incoming/S1/r/cdar/a.txt", Body=b"A")
    s3.put_object(Bucket=BUCKET, Key="incoming/S2/r/cdar/c.txt", Body=b"C")
    from botocore.exceptions import ClientError

    from pkf_runtime.delivery import Aws

    real = Aws.client

    def with_denied_copy(self, service, side="destination"):
        c = real(self, service, side)

        def deny(params, **kw):
            if params.get("Key", "").startswith("S2/"):
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "CopyObject")
        c.meta.events.register("before-parameter-build.s3.CopyObject", deny, unique_id="deny-s2")
        return c

    monkeypatch.setattr(Aws, "client", with_denied_copy)
    doc = pkf(repo, "run", "sas_restructure", "--date", "2026-10-17", ok=False)
    assert "1 package(s)" in doc["issues"][0]["msg"]
    assert _keys(s3) == ["S1/primary/a.txt", "incoming/S2/r/cdar/c.txt"]  # S1 moved; S2's source kept for the next run


def test_build_refuses_custom_delivery_with_a_transformation(env):
    repo = env["repo"]
    p = repo / "pipelines/sas_restructure.yaml"
    d = yaml.safe_load(p.read_text())
    write(repo / "transformations/noop.yaml", {"api_version": "pkf/v1", "kind": "transformation", "name": "noop",
                                               "requirement": "Keep every file.\n",
                                               "examples": [{"name": "one", "input": ["a.txt"], "expected_output": ["a.txt"]}]})
    design_by_hand(repo, "transformation", "noop", [{"id": "all", "op": "select", "title": "all", "params": {"include": ["**"]}}], {}, {})
    d["transformation"] = "noop"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    doc = pkf(repo, "build", "sas_restructure", ok=False)
    assert "cannot have a transformation" in json.dumps(doc)


def test_delivery_case_runner_injects_failures():
    fns: dict = {}
    exec(MOVE, fns)  # noqa: S102 - the test's own code
    for case in CASES["move_files"]:
        r = simulate.run_step_case("destination", fns["move_files"], case, "custom_delivery")
        assert r["status"] == "pass", r


def test_code_policy_open_and_restricted(tmp_path, monkeypatch):
    src = "def step(pkg, params, ctx):\n    import os, boto3\n    os.environ.get('X')\n"
    assert design.guard(src, "step", policy="open") == []
    assert design.guard(src, "step", policy="restricted")
    assert design.guard("def other(pkg, params, ctx):\n    pass\n", "step", policy="open")  # structure is always checked
    (tmp_path / "pkf.toml").write_text('[agent]\ncode_policy = "restricted"\n')
    assert design.code_policy(tmp_path) == "restricted"
    monkeypatch.setenv("PKF_CODE_POLICY", "open")
    assert design.code_policy(tmp_path) == "open"
    monkeypatch.delenv("PKF_CODE_POLICY")
    (tmp_path / "pkf.toml").write_text("")
    assert design.code_policy(tmp_path) == "open"  # the default


def test_custom_delivery_must_be_last():
    steps = [dict(STEPS[1]), dict(STEPS[0])]
    assert any("last step" in p for p in design.validate_steps("destination", steps))
    assert not design.validate_steps("destination", STEPS)


# ---------------------------------------------------------------------------------------------- data-loss guards
COLLIDE = ROUTE.replace('study + "/primary/" + rest.rsplit("/", 1)[-1]', '"S1/primary/" + rest.rsplit("/", 1)[-1]')


def test_two_files_to_one_key_are_refused_before_anything_moves(env):
    repo, s3 = env["repo"], env["s3"]
    design_by_hand(repo, "destination", "sas_layout", STEPS, {**CASES, "route_keys": []} | {"route_keys": CASES["route_keys"]},
                   {"route_keys": COLLIDE, "move_files": MOVE})
    p = repo / "pipelines/sas_restructure.yaml"
    d = yaml.safe_load(p.read_text())
    d["examples"] = []
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    pkf(repo, "build", "sas_restructure")
    s3.put_object(Bucket=BUCKET, Key="incoming/S1/r/cdar/a.txt", Body=b"FIRST")
    s3.put_object(Bucket=BUCKET, Key="incoming/S2/q/cdar/a.txt", Body=b"SECOND")
    s3.put_object(Bucket=BUCKET, Key="incoming/S3/q/cdar/b.txt", Body=b"OTHER")
    doc = pkf(repo, "run", "sas_restructure", "--date", "2026-10-17", ok=False)
    assert "same key 'S1/primary/a.txt'" in json.dumps(doc)
    assert _keys(s3) == ["S1/primary/b.txt", "incoming/S1/r/cdar/a.txt", "incoming/S2/q/cdar/a.txt"]  # both kept, the other moved


def _transfer(path="S1/a.txt", src="incoming/S1/a.txt", tgt="S1/x/a.txt", size=1):
    return {"path": path, "source_bucket": BUCKET, "source_key": src, "size": size, "etag": None, "target_bucket": BUCKET, "target_key": tgt}


def test_plan_conflicts_target_on_source_and_under_root():
    from pkf_runtime.delivery import plan_conflicts

    f = lambda path: {"root": "incoming/", "path": path, "key": f"incoming/{path}"}  # noqa: E731
    pk = [{"index": 0, "files": [f("S1/a.txt")], "keys": {"S1/a.txt": "incoming/S1/a.txt"}},
          {"index": 1, "files": [f("S2/b.txt")], "keys": {"S2/b.txt": "incoming/other/b.txt"}},
          {"index": 2, "files": [f("S3/c.txt")], "keys": {"S3/c.txt": "S3/c.txt"}}]
    bad = plan_conflicts(pk, BUCKET, BUCKET)
    assert "onto the source object" in bad[0] and "under the source root" in bad[1] and 2 not in bad
    assert plan_conflicts(pk, BUCKET, "another-bucket") == {}


def test_results_must_be_known_unique_and_verified(env):
    from pkf_runtime.delivery import Aws, failed, precheck, run_custom_delivery, verify
    from pkf_runtime.package import PackageError

    s3 = env["s3"]
    aws = Aws({}, {}, {}, {}, clients={("s3", "source"): s3, ("s3", "destination"): s3})
    t = _transfer()
    res = run_custom_delivery(lambda tr, p, c, a: [{"path": "S1/a.txt", "status": "error", "detail": "denied"}], "x", [t], {}, {}, aws)
    assert failed(res) and res[0]["status"] == "failed"  # an unknown status is a failure, never "delivered"
    with pytest.raises(PackageError, match="two results"):
        run_custom_delivery(lambda tr, p, c, a: [{"path": "S1/a.txt", "status": "failed"}, {"path": "S1/a.txt", "status": "moved"}],
                            "x", [t], {}, {}, aws)
    res = verify(run_custom_delivery(lambda tr, p, c, a: [{"path": "S1/a.txt", "status": "moved"}], "x", [t], {}, {}, aws), [t], aws)
    assert res[0]["status"] == "failed" and "does not exist" in res[0]["detail"]  # claimed moved, but nothing is there
    s3.put_object(Bucket=BUCKET, Key="S1/x/a.txt", Body=b"12")
    res = verify([{"path": "S1/a.txt", "key": "S1/x/a.txt", "status": "moved", "detail": "", "size": 1}], [t], aws)
    assert "2 bytes" in res[0]["detail"]
    # a retried task: the source is gone, the target has the listed size → delivered by the earlier attempt
    s3.put_object(Bucket=BUCKET, Key="S1/x/a.txt", Body=b"1")
    todo, done = precheck([t], aws, "overwrite")
    assert todo == [] and done[0]["status"] == "skipped"
    s3.put_object(Bucket=BUCKET, Key="incoming/S1/a.txt", Body=b"2")
    todo, done = precheck([t], aws, "fail")
    assert todo == [] and done[0]["status"] == "failed" and "already exists" in done[0]["detail"]
    todo, done = precheck([t], aws, "overwrite")
    assert todo == [t] and done == []


def test_custom_delivery_needs_one_active_run(env):
    repo = env["repo"]
    p = repo / "pipelines/sas_restructure.yaml"
    d = yaml.safe_load(p.read_text())
    d["max_active_runs"] = 2
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    assert "max_active_runs: 1" in json.dumps(pkf(repo, "build", "sas_restructure", ok=False))


def test_broken_delivery_case_never_passes():
    fns: dict = {}
    exec(MOVE, fns)  # noqa: S102
    case = {**CASES["move_files"][0], "expect_error": True, "expected": None,
            "input": {**CASES["move_files"][0]["input"], "fail_calls": [{"operation": "Nope"}]}}
    assert simulate.run_step_case("destination", fns["move_files"], case, "custom_delivery")["status"] == "fail"
    empty = {**CASES["move_files"][0], "expected": {}}
    assert simulate.run_step_case("destination", fns["move_files"], empty, "custom_delivery")["status"] == "fail"


def test_dry_run_lists_targets_and_changes_nothing(env):
    repo, s3 = env["repo"], env["s3"]
    pkf(repo, "build", "sas_restructure")
    s3.put_object(Bucket=BUCKET, Key="incoming/S1/r/cdar/a.txt", Body=b"A")
    s3.put_object(Bucket=BUCKET, Key="incoming/S1/x/b.txt", Body=b"B")
    doc = pkf(repo, "run", "sas_restructure", "--dry-run", "--date", "2026-10-17")
    dr = doc["data"]["dry_run"]
    assert dr["files"] == 2 and dr["delivery"] == "custom_delivery" and "up to 2 source object(s) are deleted" in dr["note"]
    assert {(r["source"], r["target"]) for r in dr["sample"]} == {("incoming/S1/r/cdar/a.txt", "S1/primary/a.txt"),
                                                                  ("incoming/S1/x/b.txt", "error/S1/x/b.txt")}
    assert _keys(s3) == ["incoming/S1/r/cdar/a.txt", "incoming/S1/x/b.txt"]  # nothing moved


def test_secrets_never_reach_the_model(monkeypatch, tmp_path):
    from pkf.llm import base

    seen = {}

    def fake_call(skill, prompt, schema, task, tools):
        seen["prompt"] = prompt
        return {"x": 1}, "m", {}
    import pkf.llm.agent_sdk as sdk
    monkeypatch.setattr(sdk, "call", fake_call)
    monkeypatch.setenv("PKF_LLM_PROVIDER", "agent-sdk")
    monkeypatch.setenv("PKF_PLUGIN_ROOT", str(Path(__file__).resolve().parents[1] / "steps-plugin"))
    from pydantic import BaseModel

    class Out(BaseModel):
        x: int
    base.run_structured("plan_component", "planning-pipeline-components",
                        "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX and password: hunter22 and AKIAABCDEFGHIJKLMNOP", Out)
    assert "sk-ant" not in seen["prompt"] and "hunter22" not in seen["prompt"] and "AKIA" not in seen["prompt"]
