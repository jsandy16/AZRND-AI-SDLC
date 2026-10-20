"""The person-facing flow through the `pkf` CLI on a copy of the examples: validate, plan (replayed LLM output),
approvals, code, build, local run. No network: the LLM is replayed from tests/llm-fixtures."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PKF = [sys.executable, "-m", "pkf.cli"]


@pytest.fixture()
def repo(tmp_path):
    dst = tmp_path / "specs"
    shutil.copytree(ROOT / "examples", dst, ignore=shutil.ignore_patterns("dags", "__pycache__", ".git", ".llm"))
    return dst


def pkf(repo: Path, *args: str, agent: bool = False, ok: bool | None = True, extra_env: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "PKF_AGENT_SESSION")}
    env.update(extra_env or {})
    env.update(PKF_LLM_PROVIDER=os.environ.get("PKF_TEST_LLM_PROVIDER", "replay"), PKF_LLM_FIXTURES=str(ROOT / "tests" / "llm-fixtures"),
               PKF_PLUGIN_ROOT=str(ROOT / "steps-plugin"), PKF_STAGING_URL=f"file://{repo.parent / 'staging'}")
    if agent:
        env["PKF_AGENT_SESSION"] = "1"
    r = subprocess.run([*PKF, "--json", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=300)
    doc = json.loads(r.stdout)
    if ok is not None:
        assert doc["ok"] is ok, json.dumps(doc, indent=1)[:2000] + r.stderr[-2000:]
    return doc


def test_examples_validate_and_build(repo):
    doc = pkf(repo, "validate")
    assert sum(doc["data"]["components"].values()) >= 12
    for t in ("csv_zip_bundle", "lab_control_check"):
        assert pkf(repo, "transform", "status", t)["data"]["stage"] == "approved"
    assert pkf(repo, "show", "trigger", "partner_drop_sqs")["data"]["status"]["stage"] == "approved"
    pkf(repo, "build", "--all")
    pkf(repo, "build", "--all", "--check")
    dags = sorted((repo / "dags").glob("pkf_*.py"))
    assert [d.name for d in dags] == ["pkf_archive_mirror.py", "pkf_cro_monitoring_daily.py", "pkf_lab_deliveries.py",
                                      "pkf_partner_zip_copy.py", "pkf_sas_folder_restructure.py"]
    for d in dags:
        compile(d.read_text(), str(d), "exec")
    assert (repo / "dags/.airflowignore").read_text().count("pkf/**") == 1
    lock = json.loads((repo / "dags/pkf/lab_deliveries/pipeline.lock.json").read_text())
    assert set(lock["components"]) == {"trigger", "source", "transformation", "destination"}
    assert (repo / "dags/pkf/lab_deliveries/components/transformation.py").exists()
    pkf(repo, "test", "pipeline", "partner_zip_copy")
    pkf(repo, "test", "pipeline", "sas_folder_restructure")
    sas = json.loads((repo / "dags/pkf/sas_folder_restructure/pipeline.lock.json").read_text())
    dest = (repo / "dags/pkf/sas_folder_restructure/components/destination.py").read_text()
    assert "custom_delivery" in json.dumps(sas["components"]["destination"]) and "(transfers, params, ctx, aws)" in dest
    assert "password" not in json.dumps(lock).lower() or "credentials" in json.dumps(lock)


def test_plan_approve_code_flow(repo):
    t = "csv_zip_bundle"
    (repo / "transformations" / f"{t}.lock.yaml").unlink()
    (repo / "transformations" / f"{t}.py").unlink(missing_ok=True)
    assert pkf(repo, "transform", "status", t)["data"]["stage"] == "needs_plan"
    pkf(repo, "build", "cro_monitoring_daily", ok=False)          # not approved → nothing to build

    doc = pkf(repo, "transform", "plan", t)
    assert [s["op"] for s in doc["data"]["plan"]["steps"]][:2] == ["select", "require"]
    assert all(r["status"] == "pass" for r in doc["data"]["simulation"]), doc["data"]["simulation"]
    assert pkf(repo, "transform", "status", t)["data"]["stage"] == "plan_proposed"

    pkf(repo, "transform", "approve-plan", t, "--by", "@tester", agent=True, ok=False)   # agents cannot approve
    pkf(repo, "transform", "approve-plan", t, "--by", "@tester")
    assert pkf(repo, "transform", "status", t)["data"]["stage"] == "approved"   # no custom steps: no code to review
    pkf(repo, "build", "cro_monitoring_daily")

    t = "lab_control_check"                                         # custom step: test writer, coder, code review
    (repo / "transformations" / f"{t}.lock.yaml").unlink()
    (repo / "transformations" / f"{t}.py").unlink()
    doc = pkf(repo, "plan", "transformation", t)
    assert len(doc["data"]["plan"]["step_tests"]["check_control_files"]) >= 3
    pkf(repo, "approve-plan", "transformation", t, "--by", "@tester")
    doc = pkf(repo, "code", "transformation", t)
    assert all(r["status"] == "pass" for r in doc["data"]["tests"])
    pkf(repo, "approve-code", "transformation", t, "--by", "@reviewer", agent=True, ok=False)
    pkf(repo, "approve-code", "transformation", t, "--by", "@reviewer")
    assert pkf(repo, "transform", "status", t)["data"]["stage"] == "approved"
    pkf(repo, "build", "lab_deliveries")


def test_changed_requirement_or_code_needs_new_approval(repo):
    code = repo / "transformations/lab_control_check.py"
    code.write_text(code.read_text().replace("    import re\n", "    import re\n    return None\n", 1))  # checks nothing
    st = pkf(repo, "transform", "status", "lab_control_check")["data"]["stage"]
    assert st == "code_changed"
    pkf(repo, "build", "lab_deliveries", ok=False)
    doc = pkf(repo, "transform", "test", "lab_control_check", ok=False)
    assert any(r["status"] != "pass" for r in doc["data"]["tests"]), doc["data"]

    spec = repo / "transformations/csv_zip_bundle.yaml"
    spec.write_text(spec.read_text().replace("Keep only CSV files.", "Keep only CSV and TXT files."))
    assert pkf(repo, "transform", "status", "csv_zip_bundle")["data"]["stage"] == "plan_stale"


def test_guard_rejects_unsafe_custom_code(repo):
    code = repo / "transformations/lab_control_check.py"
    code.write_text(code.read_text().replace("    import re\n", "    import re\n    import os\n    os.system('id')\n"))
    doc = pkf(repo, "transform", "test", "lab_control_check", ok=False, extra_env={"PKF_CODE_POLICY": "restricted"})
    assert "'os'" in json.dumps(doc["issues"]) or "['os']" in json.dumps(doc["issues"])
    doc = pkf(repo, "transform", "test", "lab_control_check", ok=None, extra_env={"PKF_CODE_POLICY": "open"})
    assert not [i for i in doc["issues"] if i["code"] == "guard"]  # open policy: imports are the reviewer's call


def test_local_run_end_to_end(repo, tmp_path):
    drop = tmp_path / "drop" / "2026-10-15"
    for rel in ("SITE_0001/a.csv", "SITE_0002/b.csv", "SITE_0002/readme.txt"):
        (drop / rel).parent.mkdir(parents=True, exist_ok=True)
        (drop / rel).write_text("id\n1\n")
    lake = tmp_path / "lake"
    docs = {
        "sources/drop.yaml": {"api_version": "pkf/v1", "kind": "source", "name": "drop", "type": "local",
                              "connection": {"base_path": str(tmp_path / "drop")}, "extraction": {"root": "{ds}"}},
        "destinations/lake.yaml": {"api_version": "pkf/v1", "kind": "destination", "name": "lake", "type": "local",
                                   "connection": {"base_path": str(lake)}, "delivery": {"write_success_marker": True}},
        "connections/drop_to_lake.yaml": {"api_version": "pkf/v1", "kind": "connection", "name": "drop_to_lake",
                                          "source": "drop", "destination": "lake"},
        "pipelines/drop_daily.yaml": {"api_version": "pkf/v1", "kind": "pipeline", "name": "drop_daily", "owner": "t",
                                      "connection": "drop_to_lake", "transformation": "csv_zip_bundle",
                                      "schedule": "@daily", "start_date": "2026-10-01"},
    }
    for rel, d in docs.items():
        (repo / rel).write_text(yaml.safe_dump(d, sort_keys=False))
    pkf(repo, "validate")
    doc = pkf(repo, "run", "drop_daily", "--date", "2026-10-15")
    tasks = [t["task"] for t in doc["data"]["tasks"]]
    assert tasks[:4] == ["check_source", "check_destination", "read_events", "plan_packages"] and tasks[-2:] == ["ack", "report"]
    assert "package[0].extract" in tasks and "package[0].upload" in tasks
    out = lake / "drop/2026-10-15/D1234C00001_20261015"
    assert sorted(p.name for p in out.iterdir()) == sorted( ["D1234C00001_20261015.zip", "_SUCCESS", "manifest.json"])
    assert not any((repo.parent / "staging").rglob("package.json"))       # staging cleared

    doc = pkf(repo, "run", "drop_daily", "--date", "2026-10-15")           # rerun: identical files skipped
    assert doc["data"]["tasks"][-1]["result"]["skipped_identical"] == 2

    (drop / "stray.csv").write_text("x\n")                                 # bad layout → refused, nothing new
    doc = pkf(repo, "run", "drop_daily", "--date", "2026-10-15", ok=False)
    assert "1 package(s)" in doc["issues"][0]["msg"]


def test_build_all_removes_dags_of_deleted_pipelines(repo):
    pkf(repo, "build", "--all")
    (repo / "pipelines/archive_mirror.yaml").unlink()
    doc = pkf(repo, "build", "--all", "--check", ok=False)
    assert any("archive_mirror" in i["msg"] for i in doc["issues"])
    doc = pkf(repo, "build", "--all")
    assert sorted(doc["data"]["removed"]) == ["dags/pkf/archive_mirror", "dags/pkf_archive_mirror.py"]
    pkf(repo, "build", "--all", "--check")


def test_code_outside_step_functions_is_refused(repo, tmp_path):
    code = repo / "transformations/lab_control_check.py"
    marker = tmp_path / "ran"
    code.write_text(code.read_text() + f"\nimport os as _o\n_o.system('touch {marker}')\n")
    doc = pkf(repo, "transform", "test", "lab_control_check", ok=False)
    assert not marker.exists(), "module-level code must never run"
    assert "only the bodies" in json.dumps(doc["issues"])
    pkf(repo, "transform", "approve-code", "lab_control_check", "--by", "@reviewer", ok=False)
    pkf(repo, "build", "lab_deliveries", ok=False)


def test_runtime_refuses_unapproved_code_in_dags_folder(repo):
    pkf(repo, "build", "lab_deliveries")
    sys.path.insert(0, str(ROOT / "src"))
    from pkf_runtime import tasks

    from pkf_runtime import components

    deployed = repo / "dags/pkf/lab_deliveries/components/transformation.py"
    deployed.write_text(deployed.read_text() + "\n# edited\n")
    lock = tasks.load_lock(str(repo / "dags/pkf_lab_deliveries.py"), "lab_deliveries")
    components._cache.clear()
    with pytest.raises(PermissionError, match="not the approved"):
        components.load(lock, "transformation")


def test_generated_dag_survives_awkward_spec_text(repo):
    p = repo / "pipelines/archive_mirror.yaml"
    d = yaml.safe_load(p.read_text())
    d.update(schedule=None, description='copies \\\\share\\Users\\archive """ and \'quotes\'', owner='a"""b')
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    pkf(repo, "build", "archive_mirror")
    src = (repo / "dags/pkf_archive_mirror.py").read_text()
    compile(src, "dag", "exec")
    assert "schedule=None" in src


@pytest.mark.parametrize("cron,ok", [("0 2 * * *", True), ("*/15 0-6 1,15 jan-mar mon-fri", True),
                                     ("61 25 * * *", False), ("0 2 * * 9", False), ("0 */0 * * *", False)])
def test_cron_validation(cron, ok):
    from pkf.specs.pipeline import PipelineFile

    base = {"api_version": "pkf/v1", "kind": "pipeline", "name": "p1", "owner": "o", "connection": "c1", "schedule": cron}
    if ok:
        PipelineFile.model_validate(base)
    else:
        with pytest.raises(Exception):
            PipelineFile.model_validate(base)


def test_params_cannot_shadow_builtins(repo):
    p = repo / "pipelines/cro_monitoring_daily.yaml"
    p.write_text(p.read_text().replace("  input_path: /exports/monitoring", "  input_path: /exports/monitoring\n  env: prod"))
    doc = pkf(repo, "validate", ok=False)
    assert "shadow" in json.dumps(doc["issues"])
