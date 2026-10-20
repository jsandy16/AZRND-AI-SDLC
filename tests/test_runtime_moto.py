"""The runtime of a custom-delivery (move) pipeline against moto, through `pkf run` (0.5.0): per-file run report
(K7, D4, D19), layout vars in the delivery params (K13, A12/D12), one bad key fails only that file (B4), a network
error is retried once and does not stop the other packages (B10), expired credentials stop the run (D2), and the
S6 "preview, then move" flags --plan-out / --only / --max-packages (K6, D1, R6). The parity test checks that the
step-test harness and the runtime call a delivery function with the same ctx (R13).

The component plan and code are written by hand, as in test_delivery.py (what the agents would produce)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from pkf.repo import load
from pkf_runtime import delivery as rt_delivery
from pkf_runtime import simulate, tasks
from test_delivery import BUCKET, CASES, MOVE, REGION, ROUTE, STEPS, design_by_hand, write
from test_run_cli import run

PIPELINE = "sas_restructure"

ROUTE_REASON = '''def route_keys(layout, params, ctx):
    for path in layout["files"]:
        study, rest = path.split("/", 1)
        if "/cdar/" in "/" + rest:
            layout["keys"][path] = study + "/primary/" + rest.rsplit("/", 1)[-1]
            layout["vars"]["reason:" + path] = "rule 1: cdar to primary"
        else:
            layout["keys"][path] = "error/" + path
            layout["vars"]["reason:" + path] = "no rule matched: error/"
    return layout'''

MOVE_REASON = '''def move_files(transfers, params, ctx, aws):
    import json, os
    out = os.environ.get("PKF_PARITY_OUT")
    if out:
        with open(out, "a", encoding="utf-8", newline="\\n") as fh:
            fh.write(json.dumps({"params": params, "ctx": ctx}, sort_keys=True, default=str) + "\\n")
    s3 = aws.s3_destination
    results = []
    for t in transfers:
        s3.copy({"Bucket": t["source_bucket"], "Key": t["source_key"]}, t["target_bucket"], t["target_key"])
        s3.delete_object(Bucket=t["source_bucket"], Key=t["source_key"])
        status = "moved_to_error" if t["target_key"].startswith("error/") else "moved"
        results.append({"path": t["path"], "key": t["target_key"], "status": status, "detail": params.get("reason:" + t["path"], "")})
    return results'''

MOVE_CASE = {"name": "move", "purpose": "a plain move", "params": {}, "expect_error": False,
             "input": {"objects": {f"{BUCKET}/incoming/S1/r/cdar/a.txt": "x"},
                       "transfers": [{"path": "S1/r/cdar/a.txt", "source_bucket": BUCKET, "source_key": "incoming/S1/r/cdar/a.txt",
                                      "size": 1, "target_bucket": BUCKET, "target_key": "S1/primary/a.txt"}]},
             "expected": {"objects": [f"{BUCKET}/S1/primary/a.txt"], "statuses": {"S1/r/cdar/a.txt": "moved"}}}
REASON_CASES = {"route_keys": CASES["route_keys"], "move_files": [MOVE_CASE]}

ROUTE_DS = ROUTE.replace('layout["keys"][path] = study + "/primary/"', 'layout["keys"][path] = params["ds"] + "/" + study + "/primary/"') \
                .replace('layout["keys"][path] = "error/" + path', 'layout["keys"][path] = "error/" + params["ds"] + "/" + path')
DS_CASES = {"route_keys": [{**CASES["route_keys"][0], "expected": ["2024-03-01/S1/primary/a.txt", "error/2024-03-01/S1/x/b.txt"]}],
            "move_files": CASES["move_files"]}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A builder for the move pipeline (one package per file under incoming/), inside moto."""
    for k, v in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    for k in ("PKF_STAGING_URL", "PKF_CODE_POLICY", "CLAUDECODE", "PKF_AGENT_SESSION", "PKF_KEEP_STAGING"):
        monkeypatch.delenv(k, raising=False)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})

        def make(route=ROUTE, move=MOVE, cases=CASES, package_name="{file}", delivery=None, examples=None):
            repo = tmp_path / "specs"
            repo.mkdir(exist_ok=True)
            make_repo(repo, tmp_path / "staging", package_name=package_name, delivery=delivery, examples=examples)
            design_by_hand(repo, "destination", "sas_layout", STEPS, cases, {"route_keys": route, "move_files": move})
            return repo

        yield {"make": make, "s3": s3, "tmp": tmp_path}


def make_repo(repo: Path, staging: Path, package_name: str = "{file}", delivery: dict | None = None, examples: list | None = None) -> None:
    (repo / "pkf.toml").write_text(f'spec_api_version = "pkf/v1"\n[staging]\nurl = "file://{staging}"\n'
                                   '[airflow]\ndags_folder = "dags"\n[agent]\ncode_policy = "open"\n', encoding="utf-8", newline="\n")
    conn = {"bucket": BUCKET, "region": REGION}
    write(repo / "sources/sas_incoming.yaml", {"api_version": "pkf/v1", "kind": "source", "name": "sas_incoming", "type": "s3",
                                               "connection": conn, "steps": [
                                                   {"id": "all", "op": "list", "title": "Everything under incoming/", "params": {"root": "incoming/"}},
                                                   {"id": "one_each", "op": "split", "title": "One package per file",
                                                    "params": {"by": "regex", "regex": "(.+)", "var": "file"}}]})
    write(repo / "destinations/sas_layout.yaml", {"api_version": "pkf/v1", "kind": "destination", "name": "sas_layout", "type": "s3",
                                                  "connection": conn, "requirement": "Route and move every file.\n",
                                                  **({"delivery": delivery} if delivery else {}),
                                                  "examples": [{"name": "cdar", "files": ["S1/r/cdar/a.txt"], "expected_keys": None}]})
    write(repo / "connections/sas_move.yaml", {"api_version": "pkf/v1", "kind": "connection", "name": "sas_move",
                                               "source": "sas_incoming", "destination": "sas_layout"})
    write(repo / f"pipelines/{PIPELINE}.yaml", {"api_version": "pkf/v1", "kind": "pipeline", "name": PIPELINE, "owner": "t",
                                                 "connection": "sas_move", "schedule": "*/45 * * * *", "start_date": "2026-10-01",
                                                 "package_name": package_name, "examples": examples or []})


def keys(s3) -> list[str]:
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def put(s3, *ks: str, body: bytes = b"x") -> None:
    for k in ks:
        s3.put_object(Bucket=BUCKET, Key=k, Body=body)


def rows_by_path(doc: dict) -> dict[str, dict]:
    return {r["path"]: r for r in doc["data"]["files"]}


# ---------------------------------------------------------------------------------------------- K7 / D4 / D19
def test_run_returns_file_rows_on_success_and_failure(world, monkeypatch):
    repo, s3 = world["make"](), world["s3"]
    put(world["s3"], "incoming/S1/r/cdar/a.txt", "incoming/S1/x/b.txt")
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")
    assert code == 0 and doc["ok"], doc
    rows = rows_by_path(doc)
    assert set(rows) == {"S1/r/cdar/a.txt", "S1/x/b.txt"}
    a = rows["S1/r/cdar/a.txt"]
    assert a == {"package": "S1/r/cdar/a.txt", "index": a["index"], "path": "S1/r/cdar/a.txt", "source_key": "incoming/S1/r/cdar/a.txt",
                 "target_key": "S1/primary/a.txt", "status": "moved", "detail": "", "note": "", "size": 1}
    assert rows["S1/x/b.txt"]["status"] == "moved_to_error" and rows["S1/x/b.txt"]["target_key"] == "error/S1/x/b.txt"
    assert doc["data"]["summary"]["delivered"] == 2 and doc["data"]["tasks"][-1]["task"] == "report"

    put(s3, "incoming/S1/r/cdar/c.txt", "incoming/S2/r/cdar/d.txt")  # S2's copy is denied: that file fails, c moves
    real = rt_delivery.Aws.client

    def with_denied_copy(self, service, side="destination"):
        c = real(self, service, side)

        def deny(params, **kw):
            if params.get("Key", "").startswith("S2/"):
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "CopyObject")
        c.meta.events.register("before-parameter-build.s3.CopyObject", deny, unique_id="deny-s2")
        return c

    monkeypatch.setattr(rt_delivery.Aws, "client", with_denied_copy)
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")
    assert code == 1 and not doc["ok"] and doc["issues"][0]["code"] == "run_failed"
    rows = rows_by_path(doc)
    assert rows["S1/r/cdar/c.txt"]["status"] == "moved"
    d = rows["S2/r/cdar/d.txt"]
    assert d["status"] == "failed" and "AccessDenied" in d["detail"], d  # the function's own per-file result
    assert d["source_key"] == "incoming/S2/r/cdar/d.txt" and d["target_key"] == "S2/primary/d.txt"
    assert doc["data"]["summary"]["delivered"] == 1 and doc["data"]["tasks"]  # the trace and the report on the failure path too


def test_report_names_failed_packages_with_index_and_path(world, monkeypatch):
    repo, s3 = world["make"](package_name="{source}_{ds_nodash}"), world["s3"]  # every package has the same name (D19)
    put(s3, "incoming/S1/r/cdar/a.txt", "incoming/S2/r/cdar/c.txt", "incoming/S3/r/cdar/e.txt")
    real = tasks.upload

    def upload(lock, previous, ctx):
        if previous["index"] == 1:
            raise tasks.PackageError("refused for the test")
        return real(lock, previous, ctx)

    monkeypatch.setattr(tasks, "upload", upload)
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")
    assert code == 1
    assert doc["data"]["summary"]["failed_packages"] == ["sas_incoming_20261017#1 (S2/r/cdar/c.txt)"]
    assert "sas_incoming_20261017#1 (S2/r/cdar/c.txt)" in doc["issues"][0]["msg"]


def test_trace_attached_on_unexpected_error(world, monkeypatch):
    repo, s3 = world["make"](), world["s3"]
    put(s3, "incoming/S1/r/cdar/a.txt")

    def broken_report(lock, ctx, keep_staging=False):
        raise OSError("staging unreachable")

    monkeypatch.setattr(tasks, "report", broken_report)
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")
    assert code == 1 and doc["issues"][0]["code"] == "run_error" and "OSError: staging unreachable" in doc["issues"][0]["msg"]
    names = [t["task"] for t in doc["data"]["tasks"]]
    assert "plan_packages" in names and "package[0].upload" in names and "ack" in names  # the trace survives
    assert [r["status"] for r in doc["data"]["files"]] == ["moved"]  # and so do the per-file rows
    assert keys(s3) == ["S1/primary/a.txt"]


# ---------------------------------------------------------------------------------------------- K13 / A12 / D12
def test_layout_vars_reach_delivery_params(world):
    repo, s3 = world["make"](route=ROUTE_REASON, move=MOVE_REASON, cases=REASON_CASES), world["s3"]
    put(s3, "incoming/S1/r/cdar/a.txt", "incoming/S1/x/b.txt")
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")
    assert code == 0, doc
    rows = rows_by_path(doc)
    assert rows["S1/r/cdar/a.txt"]["detail"] == "rule 1: cdar to primary" == rows["S1/r/cdar/a.txt"]["note"]
    assert rows["S1/x/b.txt"]["detail"] == "no rule matched: error/" == rows["S1/x/b.txt"]["note"]


def test_harness_and_runtime_pass_the_same_ctx(world, tmp_path, monkeypatch):
    """R13 parity: the step-test harness (simulate.run_step_case with the destination's delivery settings, as
    design.run_tests_isolated passes them) and the runtime (tasks._upload_direct) give a custom_delivery function the
    same ctx keys and the same ctx["delivery"]; the runtime's params are the harness's plus exactly the documented
    run variables: "package", the package's own variables (here the source's split variable `file`) and the layout
    vars of the earlier steps (K13). ctx["params"] is params in both."""
    out = tmp_path / "parity.jsonl"
    monkeypatch.setenv("PKF_PARITY_OUT", str(out))
    delivery = {"if_exists": "overwrite", "server_side_encryption": "AES256"}
    repo = world["make"](route=ROUTE_REASON, move=MOVE_REASON, cases=REASON_CASES, delivery=delivery)
    out.unlink(missing_ok=True)  # design_by_hand's own test run happens in the isolated child (no PKF_PARITY_OUT there)
    put(world["s3"], "incoming/S1/r/cdar/a.txt")
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")
    assert code == 0, doc
    runtime = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(runtime) == 1

    out.unlink()
    s3 = world["s3"]
    for k in keys(s3):  # the harness sets up its own bucket and objects (moto's state is shared with this test's)
        s3.delete_object(Bucket=BUCKET, Key=k)
    s3.delete_bucket(Bucket=BUCKET)
    from pkf.specs.common import without_new_defaults

    spec_delivery = without_new_defaults({"delivery": load(repo).get("destination", "sas_layout").delivery.model_dump(mode="json")})["delivery"]
    fns: dict = {}
    exec(MOVE_REASON, fns)  # noqa: S102 - the test's own code
    r = simulate.run_step_case("destination", fns["move_files"], MOVE_CASE, "custom_delivery", delivery=spec_delivery)
    assert r["status"] == "pass", r
    harness = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(harness) == 1

    rt, hs = runtime[0], harness[0]
    assert sorted(rt["ctx"]) == sorted(hs["ctx"]) == ["delivery", "now", "params", "run_id"]
    assert rt["ctx"]["delivery"] == hs["ctx"]["delivery"] == spec_delivery
    assert spec_delivery["if_exists"] == "overwrite" and spec_delivery["server_side_encryption"] == "AES256"
    assert rt["ctx"]["params"] == rt["params"] and hs["ctx"]["params"] == hs["params"]
    assert isinstance(rt["ctx"]["now"], float) and isinstance(hs["ctx"]["now"], float)
    assert set(hs["params"]) <= set(rt["params"])
    assert set(rt["params"]) - set(hs["params"]) == {"package", "file", "reason:S1/r/cdar/a.txt"}
    assert rt["params"]["file"] == rt["params"]["package"] == "S1/r/cdar/a.txt"
    assert rt["params"]["reason:S1/r/cdar/a.txt"] == "rule 1: cdar to primary"


# ---------------------------------------------------------------------------------------------- B4 / B10 / D2
def test_bad_key_fails_only_that_file(world):
    repo, s3 = world["make"](), world["s3"]
    put(s3, "incoming/S1/r/cdar/a.txt", "incoming/S1/x/b.txt", "incoming/S1/../x.csv")
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")
    assert code == 1 and not doc["ok"]
    assert "incoming/S1/../x.csv" in doc["issues"][0]["msg"] and "escapes the package" in doc["issues"][0]["msg"]
    assert keys(s3) == ["S1/primary/a.txt", "error/S1/x/b.txt", "incoming/S1/../x.csv"]  # the two good files moved
    bad = [r for r in doc["data"]["files"] if r["status"] == "failed"]
    assert len(bad) == 1 and bad[0]["source_key"] == "incoming/S1/../x.csv" and "escapes the package" in bad[0]["detail"]
    assert sorted(r["status"] for r in doc["data"]["files"]) == ["failed", "moved", "moved_to_error"]


def _failing_precheck(monkeypatch, fail: dict[str, int | None], exc_for):
    """precheck raises exc_for(key) for transfers of the given source keys: n times (None = always)."""
    real = tasks.delivery.precheck
    seen: dict[str, int] = {}

    def precheck(transfers, aws, if_exists):
        for t in transfers:
            k = t["source_key"]
            if k in fail:
                seen[k] = seen.get(k, 0) + 1
                if fail[k] is None or seen[k] <= fail[k]:
                    raise exc_for(k)
        return real(transfers, aws, if_exists)

    monkeypatch.setattr(tasks.delivery, "precheck", precheck)
    return seen


def test_run_continues_after_network_error_and_retries_once(world, monkeypatch):
    repo, s3 = world["make"](), world["s3"]
    files = [f"incoming/S{i}/r/cdar/f{i}.txt" for i in (1, 2, 3, 4)]
    put(s3, *files)
    seen = _failing_precheck(monkeypatch, {files[1]: 1, files[2]: None}, lambda k: ConnectionError(f"connection reset for {k}"))
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")
    assert code == 1 and doc["issues"][0]["code"] == "run_failed"
    assert seen == {files[1]: 2, files[2]: 2}  # each retried exactly once
    rows = {r["source_key"]: r for r in doc["data"]["files"]}
    assert [rows[f]["status"] for f in files] == ["moved", "moved", "failed", "moved"]
    assert "ConnectionError: connection reset" in rows[files[2]]["detail"]
    tasks_ = [t["task"] for t in doc["data"]["tasks"]]
    assert "ack" in tasks_ and tasks_[-1] == "report"
    retried = [t for t in doc["data"]["tasks"] if t.get("retrying")]
    assert [t["task"] for t in retried] == ["package[1]", "package[2]"]
    assert keys(s3) == ["S1/primary/f1.txt", "S2/primary/f2.txt", "S4/primary/f4.txt", files[2]]


def test_expired_token_stops_and_defers(world, monkeypatch):
    repo, s3 = world["make"](), world["s3"]
    files = [f"incoming/S{i}/r/cdar/f{i}.txt" for i in (1, 2, 3, 4)]
    put(s3, *files)
    expired = lambda k: ClientError({"Error": {"Code": "ExpiredToken", "Message": "The security token included in the request is expired"}},  # noqa: E731
                                    "HeadObject")
    seen = _failing_precheck(monkeypatch, {files[1]: None}, expired)
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")
    assert code == 1 and seen == {files[1]: 1}  # never retried: no later package can succeed
    msg = doc["issues"][0]["msg"]
    assert "AWS credentials expired after 1 of 4 packages: log in again (aws sso login) and run again" in msg, msg
    rows = {r["source_key"]: r for r in doc["data"]["files"]}
    assert [rows[f]["status"] for f in files] == ["moved", "failed", "not_attempted", "not_attempted"]
    assert "ExpiredToken" in rows[files[1]]["detail"]
    assert doc["data"]["summary"]["not_attempted_packages"] == 2
    assert keys(s3) == ["S1/primary/f1.txt", *files[1:]]  # nothing else was touched


# ---------------------------------------------------------------------------------------------- K6 / D1 / R6
def _preview(repo: Path, out: Path, *extra: str) -> dict:
    code, doc = run(repo, "run", PIPELINE, "--dry-run", "--plan-out", str(out), *extra)
    assert code == 0, doc
    return json.loads(out.read_text(encoding="utf-8"))


def test_plan_out_writes_every_row(world, monkeypatch):
    repo, s3 = world["make"](), world["s3"]
    put(s3, *[f"incoming/S{i:02d}/r/cdar/a.txt" for i in range(60)])
    monkeypatch.setattr(tasks, "SAMPLE_CUSTOM_DELIVERY", 1)  # the sample is the CLI default (50); planned.json has all
    out = world["tmp"] / "cache/runs/p/r1/planned.json"
    code, doc = run(repo, "run", PIPELINE, "--dry-run", "--plan-out", str(out), "--date", "2026-10-17", "--param", "x=1")
    assert code == 0, doc
    dr = doc["data"]["dry_run"]
    assert dr["files"] == 60 and len(dr["sample"]) == 50 and dr["sample_truncated"] is True
    assert "planned_files" not in dr and doc["data"]["planned"] == {"file": str(out), "files": 60}
    planned = json.loads(out.read_text(encoding="utf-8"))
    assert out.read_bytes().count(b"\r") == 0
    assert {k: planned[k] for k in ("version", "pipeline", "params", "date", "max_packages", "delivery")} == {
        "version": 1, "pipeline": PIPELINE, "params": {"x": "1"}, "date": "2026-10-17", "max_packages": None, "delivery": "custom_delivery"}
    assert planned["run_id"].startswith("local__") and planned["created_at"].endswith("Z")
    assert planned["logical_date"].startswith("2026-10-17T00:00:00")
    assert len(planned["files"]) == 60
    f = planned["files"][0]
    assert sorted(f) == ["etag", "index", "package", "path", "size", "source_key", "target_key"]
    assert f["source_key"] == "incoming/S00/r/cdar/a.txt" and f["target_key"] == "S00/primary/a.txt" and f["etag"] and f["size"] == 1
    assert len(keys(s3)) == 60 and all(k.startswith("incoming/") for k in keys(s3))  # a preview changes nothing


def test_run_only_planned_files(world):
    repo, s3 = world["make"](), world["s3"]
    put(s3, "incoming/S1/r/cdar/a.txt", "incoming/S2/r/cdar/b.txt", "incoming/S3/r/cdar/c.txt")
    out = world["tmp"] / "planned.json"
    planned = _preview(repo, out, "--date", "2026-10-17")
    assert len(planned["files"]) == 3
    put(s3, "incoming/S4/r/cdar/new.txt")  # added after the preview: stays
    put(s3, "incoming/S2/r/cdar/b.txt", body=b"changed")  # changed after the preview: excluded
    code, doc = run(repo, "run", PIPELINE, "--only", str(out), "--log-file", str(world["tmp"] / "run/run.log"))
    assert code == 0, doc
    assert keys(s3) == ["S1/primary/a.txt", "S3/primary/c.txt", "incoming/S2/r/cdar/b.txt", "incoming/S4/r/cdar/new.txt"]
    summary = doc["data"]["summary"]
    assert (summary["planned_files"], summary["excluded_since_preview"], summary["missing_since_preview"]) == (3, 2, 0)
    assert summary["run_id"] == planned["run_id"]  # the preview's run id
    assert sorted(r["source_key"] for r in doc["data"]["files"]) == ["incoming/S1/r/cdar/a.txt", "incoming/S3/r/cdar/c.txt"]
    log = (world["tmp"] / "run/run.log").read_bytes()
    assert b"\r" not in log and b"S1/r/cdar/a.txt -> S1/primary/a.txt [moved]" in log
    assert b"only the previewed files: 2 kept; 2 listed file(s) excluded" in log

    s3.delete_object(Bucket=BUCKET, Key="incoming/S2/r/cdar/b.txt")  # a previewed file that is gone now
    code, doc = run(repo, "run", PIPELINE, "--only", str(out))
    assert code == 0 and doc["data"]["summary"]["missing_since_preview"] == 3 and doc["data"]["summary"]["packages"] == 0
    assert keys(s3) == ["S1/primary/a.txt", "S3/primary/c.txt", "incoming/S4/r/cdar/new.txt"]


def test_run_only_reuses_planned_variables(world):
    repo, s3 = world["make"](route=ROUTE_DS, cases=DS_CASES), world["s3"]
    put(s3, "incoming/S1/r/cdar/a.txt")
    out = world["tmp"] / "planned.json"
    planned = _preview(repo, out)  # no --date: the preview records the date it used
    assert planned["date"] is None and planned["logical_date"]
    planned["logical_date"] = "2026-10-01T09:00:00+00:00"  # as if the preview was taken on an earlier day
    planned["files"][0]["target_key"] = "2026-10-01/S1/primary/a.txt"
    out.write_text(json.dumps(planned), encoding="utf-8", newline="\n")
    code, doc = run(repo, "run", PIPELINE, "--only", str(out))
    assert code == 0, doc
    assert keys(s3) == ["2026-10-01/S1/primary/a.txt"]  # the preview's date, not today's

    put(s3, "incoming/S2/r/cdar/b.txt")
    planned = _preview(repo, out, "--date", "2026-10-17")
    assert planned["files"][0]["target_key"] == "2026-10-17/S2/primary/b.txt"
    code, doc = run(repo, "run", PIPELINE, "--only", str(out))  # moved "a day later": same targets
    assert code == 0 and "2026-10-17/S2/primary/b.txt" in keys(s3)

    for extra in (["--param", "x=1"], ["--date", "2026-10-18"], ["--run-id", "other"]):
        code, doc = run(repo, "run", PIPELINE, "--only", str(out), *extra)
        assert code == 2 and doc["issues"][0]["code"] == "usage" and "from the preview" in doc["issues"][0]["msg"], extra
    code, doc = run(repo, "run", PIPELINE, "--only", str(out), "--dry-run")
    assert code == 2 and "--only cannot be combined with --dry-run" in doc["issues"][0]["msg"]
    code, doc = run(repo, "run", PIPELINE, "--plan-out", str(out))
    assert code == 2 and "--plan-out needs --dry-run" in doc["issues"][0]["msg"]
    code, doc = run(repo, "run", PIPELINE, "--only", str(world["tmp"] / "missing.json"))
    assert code == 2 and "does not exist" in doc["issues"][0]["msg"]
    other = {**json.loads(out.read_text(encoding="utf-8")), "pipeline": "another"}
    (world["tmp"] / "other.json").write_text(json.dumps(other), encoding="utf-8")
    code, doc = run(repo, "run", PIPELINE, "--only", str(world["tmp"] / "other.json"))
    assert code == 2 and "preview of pipeline 'another'" in doc["issues"][0]["msg"]

    # a framework-upload pipeline (no custom delivery, never deletes): --only is refused
    write(repo / "destinations/plain.yaml", {"api_version": "pkf/v1", "kind": "destination", "name": "plain", "type": "s3",
                                             "connection": {"bucket": BUCKET, "region": REGION}, "delivery": {"prefix": "out/{ds}/"}})
    write(repo / "connections/plain.yaml", {"api_version": "pkf/v1", "kind": "connection", "name": "plain",
                                            "source": "sas_incoming", "destination": "plain"})
    write(repo / "pipelines/plain_copy.yaml", {"api_version": "pkf/v1", "kind": "pipeline", "name": "plain_copy", "owner": "t",
                                               "connection": "plain", "schedule": "@daily", "start_date": "2026-10-01",
                                               "package_name": "{file}"})
    (world["tmp"] / "plain.json").write_text(json.dumps({**other, "pipeline": "plain_copy"}), encoding="utf-8")
    code, doc = run(repo, "run", "plain_copy", "--only", str(world["tmp"] / "plain.json"))
    assert code == 2 and "`--only` applies to custom-delivery pipelines" in doc["issues"][0]["msg"], doc


def test_run_max_packages_caps_and_defers(world):
    repo, s3 = world["make"](), world["s3"]
    put(s3, "incoming/S1/r/cdar/a.txt", "incoming/S2/r/cdar/b.txt", "incoming/S3/r/cdar/c.txt")
    planned = _preview(repo, world["tmp"] / "planned.json", "--max-packages", "2", "--date", "2026-10-17")
    assert len(planned["files"]) == 2 and planned["max_packages"] == 2
    code, doc = run(repo, "run", PIPELINE, "--max-packages", "2", "--date", "2026-10-17")
    assert code == 0, doc
    s = doc["data"]["summary"]
    assert (s["packages"], s["delivered"], s["deferred_packages"], s["max_packages"]) == (2, 2, 1, 2)
    assert keys(s3) == ["S1/primary/a.txt", "S2/primary/b.txt", "incoming/S3/r/cdar/c.txt"]  # the rest waits
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")
    assert code == 0 and keys(s3) == ["S1/primary/a.txt", "S2/primary/b.txt", "S3/primary/c.txt"]
    code, doc = run(repo, "run", PIPELINE, "--max-packages", "-1")
    assert code == 2 and doc["issues"][0]["code"] == "usage"


def test_keep_planned_drops_unplanned_packages_and_releases_their_messages():
    """Unit (K6): packages left without a planned file are dropped; their messages are released, not acknowledged."""
    pk = [{"index": i, "message_ids": [f"m{i}"], "files": [{"path": f"f{i}"}], "package": f"p{i}"} for i in range(3)]
    kept, dropped, counts = tasks._keep_planned(
        [{**p, "keys": {f"f{p['index']}": f"t{p['index']}"}} for p in pk],
        {"files": [{"source_key": "f0", "target_key": "t0", "etag": None}, {"source_key": "f9", "target_key": "x"}]})
    assert [p["index"] for p in kept] == [0] and dropped == {"m1", "m2"}
    assert counts == {"planned_files": 2, "kept": 1, "excluded_since_preview": 2, "missing_since_preview": 1}


def test_run_log_file_and_stderr_logging(world, capfd):
    repo, s3 = world["make"](), world["s3"]
    put(s3, "incoming/S1/r/cdar/a.txt")
    log = world["tmp"] / "logs/run.log"
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17", "--log-file", str(log))
    assert code == 0
    text = log.read_text(encoding="utf-8")
    assert "S1/r/cdar/a.txt -> S1/primary/a.txt [moved]" in text and "run summary:" in text
    assert os.path.getsize(log) == len(text.encode("utf-8"))  # LF only
    put(s3, "incoming/S2/r/cdar/b.txt")
    capfd.readouterr()
    code, doc = run(repo, "run", PIPELINE, "--date", "2026-10-17")  # --json: only warnings and errors on stderr
    err = capfd.readouterr().err
    assert code == 0 and "[moved]" not in err
