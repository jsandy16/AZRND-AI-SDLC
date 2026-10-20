"""The `pkf` CLI flags added in 0.5.0: `build --draft` (K3, A1) and `code --step/--all` (A8).

The custom component is planned and coded by hand (what the agents would produce), as in test_delivery.py; its code
passes its own tests (stage `code_generated`) but nobody has approved it yet — the S4 state the draft build is for."""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from pkf import build, design
from pkf.cli import main
from pkf.repo import load
from test_delivery import BUCKET, CASES, MOVE, REGION, ROUTE, STEPS, write

PIPELINE = "sas_restructure"
DEST = ("destination", "sas_layout")


def run(repo: Path, *args: str) -> tuple[int, dict]:
    """(exit code, envelope) of `pkf --json <args>` in repo."""
    buf, cwd = io.StringIO(), os.getcwd()
    os.chdir(repo)
    try:
        with redirect_stdout(buf):
            try:
                code = main(["--json", *args])
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
    finally:
        os.chdir(cwd)
    return code, json.loads(buf.getvalue())


def plan_and_code(root: Path, functions: dict[str, str]) -> dict:
    """Hand-written plan (approved) and code (tested, NOT approved). Returns the component status."""
    kind, name = DEST
    repo = load(root)
    spec = repo.get(kind, name)
    lock = {"lock_version": 2, "kind": kind, "name": name, "requirement_sha256": design.requirement_sha(kind, spec),
            "plan": {"summary": "hand-written", "steps": STEPS, "assumptions": [], "ambiguities": [], "step_tests": CASES},
            "simulation": design.run_examples_inprocess(kind, spec, STEPS)}
    lock["plan_digest"] = design.plan_digest(lock)
    design.save_lock(repo, kind, name, lock)
    design.approve_plan(repo, kind, name, "@tester")
    lock = design.load_lock(repo, kind, name)
    design.code_path(repo, kind, name).write_text(design.render_module(kind, name, lock, functions), encoding="utf-8", newline="\n")
    lock["code"] = {"file": f"{name}.py", "plan_digest": lock["plan_digest"], "generated_by": {}, "generated_at": "now"}
    design.save_lock(repo, kind, name, lock)
    design.test_code(repo, kind, name)
    return design.status(load(root), kind, name)


@pytest.fixture()
def specs(tmp_path, monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    for k in ("PKF_STAGING_URL", "PKF_CODE_POLICY", "CLAUDECODE", "PKF_AGENT_SESSION"):
        monkeypatch.delenv(k, raising=False)
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        repo = tmp_path / "specs"
        repo.mkdir()
        (repo / "pkf.toml").write_text(f'spec_api_version = "pkf/v1"\n[staging]\nurl = "file://{tmp_path / "staging"}"\n'
                                       '[airflow]\ndags_folder = "dags"\n[agent]\ncode_policy = "open"\n', encoding="utf-8")
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
        write(repo / f"pipelines/{PIPELINE}.yaml", {"api_version": "pkf/v1", "kind": "pipeline", "name": PIPELINE, "owner": "t",
                                                     "connection": "sas_move", "schedule": "*/45 * * * *", "start_date": "2026-10-01",
                                                     "package_name": "{file}",
                                                     "examples": [{"name": "two", "listing": ["incoming/S1/r/cdar/a.txt:1", "incoming/S1/x/b.txt:1"],
                                                                   "expected_keys": ["S1/primary/a.txt", "error/S1/x/b.txt"]}]})
        yield repo


def test_build_draft_accepts_code_generated_writes_nothing(specs):
    assert plan_and_code(specs, {"route_keys": ROUTE, "move_files": MOVE})["stage"] == "code_generated"

    code, doc = run(specs, "build", PIPELINE)  # the real build still needs the code approval
    assert code == 1 and doc["ok"] is False and doc["command"] == "build"
    assert [i["code"] for i in doc["issues"]] == ["component_not_approved"], doc["issues"]
    assert not (specs / "dags").exists()

    code, doc = run(specs, "build", PIPELINE, "--draft")
    assert code == 0 and doc["ok"] is True, json.dumps(doc, indent=1)[:3000]
    assert doc["command"] == "build --draft"
    assert doc["data"]["files"] == [] and doc["data"]["draft"] is True
    assert doc["data"]["unapproved_code"] == ["destination/sas_layout"]
    assert f"dags/pkf_{PIPELINE}.py" in doc["data"]["would_write"]
    assert f"dags/pkf/{PIPELINE}/components/destination.py" in doc["data"]["would_write"]
    assert not (specs / "dags").exists()  # a draft build writes nothing

    files = build.render(load(specs), PIPELINE, allow_unapproved_code=True)  # what a reviewer would see
    module = files[f"dags/pkf/{PIPELINE}/components/destination.py"].decode()
    assert "Code NOT approved yet: draft build for review (S4)" in module
    assert "def move_files(transfers, params, ctx, aws):" in module

    code, doc = run(specs, "build", PIPELINE, "--draft", "--check")
    assert code == 2 and doc["ok"] is False
    assert doc["issues"][0]["code"] == "usage" and "--draft and --check cannot be combined" in doc["issues"][0]["msg"]

    code, doc = run(specs, "build", "--all", "--draft")  # --all: same, and orphans are left alone
    assert code == 0 and doc["data"]["unapproved_code"] == ["destination/sas_layout"] and not (specs / "dags").exists()


def test_build_draft_of_approved_code_matches_the_real_build(specs):
    plan_and_code(specs, {"route_keys": ROUTE, "move_files": MOVE})
    design.approve_code(load(specs), *DEST, "@reviewer")
    code, doc = run(specs, "build", PIPELINE, "--draft")
    assert code == 0 and doc["data"]["unapproved_code"] == []
    would = doc["data"]["would_write"]
    draft = build.render(load(specs), PIPELINE, allow_unapproved_code=True)
    assert draft == build.render(load(specs), PIPELINE)  # approved code: the draft is exactly the real build
    code, doc = run(specs, "build", PIPELINE)
    assert code == 0 and doc["data"]["files"] == would and (specs / f"dags/pkf_{PIPELINE}.py").exists()


def test_build_draft_refuses_failing_code(specs):
    wrong = ROUTE.replace('"/primary/"', '"/secondary/"')  # every routing case and example now fails
    st = plan_and_code(specs, {"route_keys": wrong, "move_files": MOVE})
    assert st["stage"] == "code_failing", st
    code, doc = run(specs, "build", PIPELINE, "--draft")
    assert code == 1 and doc["ok"] is False
    issue = doc["issues"][0]
    assert issue["code"] == "component_not_ready", doc["issues"]
    assert "destination 'sas_layout' is not ready for a draft build (code_failing)" in issue["msg"]
    assert st["next"] in issue["msg"]  # names the component's next action
    assert not (specs / "dags").exists()


def test_cli_code_step_and_all_flags(specs, monkeypatch):
    plan_and_code(specs, {"route_keys": ROUTE, "move_files": MOVE})
    calls = []

    def fake_generate_code(repo, kind, name, steps=None, all_steps=False, **kw):
        calls.append({"kind": kind, "name": name, "steps": steps, "all_steps": all_steps, "kw": kw})
        return design.load_lock(repo, kind, name), []

    monkeypatch.setattr(design, "generate_code", fake_generate_code)

    code, doc = run(specs, "code", *DEST, "--step", "move_files", "--step", "route_keys")
    assert code == 0 and doc["ok"] is True, doc
    assert calls[-1]["steps"] == ["move_files", "route_keys"] and calls[-1]["all_steps"] is False
    assert doc["data"]["file"] == "destinations/sas_layout.py" and doc["data"]["status"]["stage"] == "code_generated"

    code, doc = run(specs, "code", *DEST, "--all")
    assert code == 0 and calls[-1]["steps"] is None and calls[-1]["all_steps"] is True

    n = len(calls)
    code, doc = run(specs, "code", *DEST)  # no flag: the design module's default (missing or failing steps first)
    assert code == 0 and len(calls) == n + 1 and calls[-1]["steps"] is None and calls[-1]["all_steps"] is False

    n = len(calls)
    code, doc = run(specs, "code", *DEST, "--step", "move_files", "--all")
    assert code == 2 and doc["issues"][0]["code"] == "usage" and "cannot be combined" in doc["issues"][0]["msg"]
    code, doc = run(specs, "test", *DEST, "--step", "move_files")  # the flags belong to `pkf code` only
    assert code == 2 and doc["issues"][0]["code"] == "usage"
    assert len(calls) == n  # usage errors never reach the coder

    def old_generate_code(repo, kind, name):  # a design module without the 0.5.0 keywords
        calls.append("old")
        return design.load_lock(repo, kind, name), []

    monkeypatch.setattr(design, "generate_code", old_generate_code)
    code, doc = run(specs, "code", *DEST, "--step", "move_files")
    assert code == 2 and "--step/--all need package-factory 0.5.0" in doc["issues"][0]["msg"] and calls[-1] != "old"
    code, doc = run(specs, "code", *DEST)
    assert code == 0 and calls[-1] == "old"


def test_draft_build_uses_the_design_modules_flag_when_it_has_one(specs, monkeypatch):
    """K3: build delegates to `design.approved(..., allow_unapproved_code=True)`; its own rule is only the fallback."""
    plan_and_code(specs, {"route_keys": ROUTE, "move_files": MOVE})
    real, seen = design.approved, []

    def approved(repo, kind, name, *, allow_unapproved_code=False):
        seen.append((kind, name, allow_unapproved_code))
        if allow_unapproved_code and design.status(repo, kind, name)["stage"] == "code_generated":
            lock = design.load_lock(repo, kind, name)
            fns = design.extract_functions(design.code_path(repo, kind, name).read_text(encoding="utf-8"),
                                           design.custom_ids(lock["plan"]["steps"]))
            return {"steps": lock["plan"]["steps"], "functions": fns, "header": ["from the design module"]}
        return real(repo, kind, name)

    monkeypatch.setattr(design, "approved", approved)
    files = build.render(load(specs), PIPELINE, allow_unapproved_code=True)
    assert ("destination", "sas_layout", True) in seen
    assert "from the design module" in files[f"dags/pkf/{PIPELINE}/components/destination.py"].decode()
    seen.clear()
    code, doc = run(specs, "build", PIPELINE)  # the real build never passes the flag
    assert code == 1 and all(flag is False for *_, flag in seen)


# ---------------------------------------------------------------------------------------------- 0.5.0 Phase 2
def test_build_warns_on_inert_trigger_params_and_uneven_cron(specs):
    """D18/A11: settings meant for the DAG that it does not use, and a */N cron that does not divide the hour, are
    build warnings; a warning never fails a build (K3)."""
    plan_and_code(specs, {"route_keys": ROUTE, "move_files": MOVE})
    write(specs / "triggers/every_45_minutes.yaml", {"api_version": "pkf/v1", "kind": "trigger", "name": "every_45_minutes",
                                                     "schedule": "*/45 * * * *",
                                                     "params": {"retries": "2", "retry_delay_seconds": "120"}})
    p = specs / f"pipelines/{PIPELINE}.yaml"
    import yaml

    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    doc.pop("schedule")
    doc["trigger"] = "every_45_minutes"
    write(p, doc)
    code, doc = run(specs, "build", PIPELINE, "--draft")
    assert code == 0 and doc["ok"] is True, doc
    by = {i["code"]: i for i in doc["issues"]}
    assert all(i["severity"] == "warning" for i in doc["issues"])
    assert by["inert_trigger_param"]["file"] == "triggers/every_45_minutes.yaml"
    assert "retries, retry_delay_seconds not used by the DAG: set retries / retry_delay_minutes in pipelines/sas_restructure.yaml" \
        in by["inert_trigger_param"]["msg"] and "Pipeline settings" in by["inert_trigger_param"]["msg"]
    assert "runs at minutes divisible by 45" in by["uneven_cron"]["msg"] and "gap at the top of the hour is shorter" in by["uneven_cron"]["msg"]
    lock = {"trigger": {"name": "schedule", "schedule": "*/15 * * * *", "params": {}}, "staging_url": "s3://b/p"}
    assert build.warnings(load(specs), PIPELINE, lock) == []  # 15 divides 60; s3 staging


def test_build_warns_file_staging(specs):
    plan_and_code(specs, {"route_keys": ROUTE, "move_files": MOVE})
    design.approve_code(load(specs), *DEST, "@reviewer")
    code, doc = run(specs, "build", PIPELINE)
    assert code == 0 and doc["ok"] is True and doc["data"]["files"]
    w = [i for i in doc["issues"] if i["code"] == "file_staging"]
    assert len(w) == 1 and w[0]["severity"] == "warning" and "PKF_STAGING_URL=s3://" in w[0]["msg"]
    code, doc = run(specs, "build", PIPELINE, "--check")
    assert code == 0 and doc["ok"] is True and [i["code"] for i in doc["issues"]] == ["uneven_cron", "file_staging"]


def test_missing_plan_json_is_runfailed(tmp_path):
    from pkf_runtime import tasks

    lock = {"staging_url": f"file://{tmp_path / 'staging'}", "pipeline": {"name": "p"}}
    ctx = {"run_id": "r1"}
    for call in (lambda: tasks.extract(lock, {"index": 0}, ctx), lambda: tasks._upload_direct(
            {**lock, "source": {"type": "s3"}, "destination": {"type": "s3"}, "_dir": str(tmp_path),
             "components": {"destination": {"module": "x.py", "sha256": "", "steps": []}}}, {"index": 0}, ctx)):
        with pytest.raises(tasks.RunFailed, match=r"the run plan is missing in staging \(file://.*/p/r1/_run/plan.json\): "
                                                  r"every Airflow worker must reach the same staging \(set PKF_STAGING_URL=s3://…\)"):
            call()


def test_dry_run_without_credentials_returns_envelope(specs, monkeypatch):
    from botocore.exceptions import NoCredentialsError

    from pkf_runtime.sources import s3 as s3src

    plan_and_code(specs, {"route_keys": ROUTE, "move_files": MOVE})
    design.approve_code(load(specs), *DEST, "@reviewer")

    def no_credentials(self, *a, **kw):
        raise NoCredentialsError()

    monkeypatch.setattr(s3src.S3Session, "list", no_credentials)
    code, doc = run(specs, "run", PIPELINE, "--dry-run")
    assert code == 1 and doc["ok"] is False and doc["command"] == "run"
    assert doc["issues"][0]["code"] == "run_error" and "NoCredentialsError: Unable to locate credentials" in doc["issues"][0]["msg"]


def test_dry_run_custom_delivery_sample_not_capped_at_50(specs):
    plan_and_code(specs, {"route_keys": ROUTE, "move_files": MOVE})
    design.approve_code(load(specs), *DEST, "@reviewer")
    s3 = boto3.client("s3", region_name=REGION)
    for i in range(60):
        s3.put_object(Bucket=BUCKET, Key=f"incoming/S{i:02d}/r/cdar/a.txt", Body=b"x")
    code, doc = run(specs, "run", PIPELINE, "--dry-run", "--date", "2026-10-17")
    assert code == 0, doc
    dr = doc["data"]["dry_run"]
    assert dr["files"] == 60 and len(dr["sample"]) == 60 and dr["sample_truncated"] is False
    row = dr["sample"][0]
    assert {"package", "source", "size", "target"} <= set(row)  # what the S5 card renders (D8)
    assert row["source"] == "incoming/S00/r/cdar/a.txt" and row["target"] == "S00/primary/a.txt"


def test_cli_output_survives_cp1252_stdout(specs):
    """B6: a cp1252 console (Git Bash/mintty pipes) prints '?' instead of crashing `pkf show`."""
    import subprocess
    import sys

    plan_and_code(specs, {"route_keys": ROUTE, "move_files": MOVE})
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8",)}
    env["PYTHONIOENCODING"] = "cp1252"
    r = subprocess.run([sys.executable, "-m", "pkf.cli", "show", *DEST], cwd=specs, env=env, capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode("cp1252", errors="replace")[-2000:]
    out = r.stdout.decode("cp1252")
    assert "show destination: OK" in out
    assert "\u2192" in design.show(load(specs), *DEST) and "?" in out  # the arrow could not be shown: replaced


def test_staging_put_over_partial_target(tmp_path, monkeypatch):
    """B14: a folder left half-deleted (a file held open on Windows) does not make the next put fail."""
    from pkf_runtime import staging

    src = tmp_path / "work"
    (src / "files/a").mkdir(parents=True)
    (src / "files/a/x.txt").write_bytes(b"new")
    (src / "package.json").write_bytes(b"{}")
    target = tmp_path / "stage/p0000/00_extract"
    (target / "files/a").mkdir(parents=True)
    (target / "files/a/x.txt").write_bytes(b"old, locked")
    (target / "files/a/left.txt").write_bytes(b"leftover")
    monkeypatch.setattr(staging.shutil, "rmtree", lambda *a, **kw: None)  # nothing could be removed
    staging.put(src, f"file://{target}")
    assert (target / "files/a/x.txt").read_bytes() == b"new" and (target / "package.json").exists()
    back = tmp_path / "back"
    back.mkdir()
    (back / "stale.txt").write_bytes(b"s")
    staging.get(f"file://{target}", back)
    assert (back / "files/a/x.txt").read_bytes() == b"new"


def test_staging_file_url_to_windows_path(monkeypatch):
    """B15: file URLs become Windows paths with their drive; the Git Bash spelling /c/... is refused there."""
    import nturl2path

    from pkf_runtime import staging

    assert staging._path("file:///tmp/pkf%20staging/p") == "/tmp/pkf staging/p"  # this OS
    monkeypatch.setattr(staging, "url2pathname", nturl2path.url2pathname)
    monkeypatch.setattr(staging, "_windows", lambda: True)
    assert staging._path("file:///C:/Users/kmlh360/pkf-staging") == "C:\\Users\\kmlh360\\pkf-staging"
    assert staging._path("file://C:/Users/kmlh360/pkf-staging") == "C:\\Users\\kmlh360\\pkf-staging"
    assert staging._path("file://localhost/D:/s") == "D:\\s"
    with pytest.raises(ValueError, match=r"file URLs need a drive: file:///C:/path \(not /c/path\)"):
        staging._path("file:///c/Users/kmlh360/pkf-staging")


def test_local_destination_retries_a_locked_target(tmp_path, monkeypatch):
    """B14: a target held open (WinError 32) is retried, then a retryable ConnectionError, never a FAIL_FAST error."""
    from pkf_runtime import destinations, tasks

    monkeypatch.setattr(destinations.time, "sleep", lambda s: None)
    real, calls = destinations.os.replace, []

    def locked(n):
        def replace(a, b):
            calls.append(b)
            if len(calls) <= n:
                err = PermissionError(13, "The process cannot access the file because it is being used by another process")
                err.winerror = 32
                raise err
            return real(a, b)
        return replace

    local = tmp_path / "f.txt"
    local.write_bytes(b"data")
    d = destinations.LocalDestination({"base_path": str(tmp_path / "out")}, {})
    monkeypatch.setattr(destinations.os, "replace", locked(2))
    assert d.put("a/f.txt", local, "x", {"if_exists": "overwrite"}) == "uploaded" and len(calls) == 3
    assert (tmp_path / "out/a/f.txt").read_bytes() == b"data"
    calls.clear()
    monkeypatch.setattr(destinations.os, "replace", locked(99))
    with pytest.raises(ConnectionError, match="in use by another program") as exc:
        d.put("a/g.txt", local, "x", {"if_exists": "overwrite"})
    assert len(calls) == destinations.REPLACE_ATTEMPTS and not isinstance(exc.value, tasks.FAIL_FAST)
    monkeypatch.setattr(destinations.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(PermissionError):  # an ordinary permission error is still not retried
        d.put("a/h.txt", local, "x", {"if_exists": "overwrite"})
