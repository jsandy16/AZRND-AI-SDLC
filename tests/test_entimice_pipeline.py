"""The Entimice pipeline (examples/pipelines/entimice_api.yaml, requirement in examples/requirements/): the Self-Service
control list and write-back (pkf_runtime.selfservice, trigger ops selfservice / selfservice_due / data_vars), the
Entimice source adapter, stream delivery with a bucket per study, and whole runs against local stand-ins
(dryrun/entimice_servers.py) and moto."""
from __future__ import annotations

import copy
import datetime as dt
import gzip
import shutil
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from pkf import build
from pkf.repo import load
from pkf_runtime import selfservice as ss
from pkf_runtime import streaming, tasks
from pkf_runtime.package import PackageError
from pkf_runtime.sources.entimice import EntimiceSession

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dryrun"))
from entimice_servers import start  # noqa: E402

API_KEY = "dpls_test_key_0123456789abc"
TOKEN = "vault-token"
SAS, CLIN = "dev-dpl-dstore-sas-eu-north-1", "dev-dpl-clinicalstudies-eu-north-1"
TODAY = dt.date(2026, 10, 20)


# ---------------------------------------------------------------------------------------------- due rules
@pytest.mark.parametrize("data,force,want", [
    ({"refreshSchedule": "Once", "lastTransferStatus": ""}, False, (True, "")),
    ({"refreshSchedule": " once ", "lastTransferStatus": " done "}, False, (False, "completed_once")),
    ({"refreshSchedule": "Once", "lastTransferStatus": "COMPLETED"}, True, (True, "")),
    ({"refreshSchedule": "Daily", "lastTransferStatus": "FAILED_INGESTION_STOPPED"}, True, (False, "ingestion_stopped")),
    ({"refreshSchedule": "daily", "lastTransferStatus": "failed_ingestion_stopped"}, False, (False, "ingestion_stopped")),
    ({"refreshSchedule": "Daily", "lastTransferStatus": "DONE", "lastTransferAt": "2026-10-19 11:59 PM"}, False, (True, "")),
    ({"refreshSchedule": "Daily", "lastTransferStatus": "DONE", "lastTransferAt": "2026-10-20 00:01"}, False, (False, "refreshed_recently")),
    ({"refreshSchedule": "Daily", "lastTransferStatus": "DONE", "lastTransferAt": "2026-10-20 00:01"}, True, (True, "")),
    ({"refreshSchedule": "Weekly", "lastTransferStatus": "DONE", "lastTransferAt": "10/13/2026 11:36PM"}, False, (True, "")),
    ({"refreshSchedule": "Weekly", "lastTransferStatus": "DONE", "lastTransferAt": "10/14/2026 00:31"}, False, (False, "refreshed_recently")),
    ({"refreshSchedule": "Monthly", "lastTransferStatus": "DONE", "lastTransferAt": "2026-09-20"}, False, (True, "")),
    ({"refreshSchedule": "Monthly", "lastTransferStatus": "DONE", "lastTransferAt": "9/21/2026 11:36 PM"}, False, (False, "refreshed_recently")),
    ({"refreshSchedule": "Monthly", "lastTransferStatus": "DONE", "lastTransferAt": ""}, False, (True, "")),
    ({"refreshSchedule": "Weekly", "lastTransferStatus": "DONE", "lastTransferAt": "garbage"}, False, (True, "")),
    ({"refreshSchedule": "Weekly", "lastTransferStatus": "FAILED", "lastTransferAt": "2026-10-20"}, False, (True, "")),
    ({"refreshSchedule": "Hourly", "lastTransferStatus": ""}, True, (False, "no_or_unknown_refresh_schedule")),
    ({"refreshSchedule": "", "lastTransferStatus": ""}, False, (False, "no_or_unknown_refresh_schedule")),
    ({}, False, (False, "no_or_unknown_refresh_schedule")),
])
def test_due(data, force, want):
    assert ss.due(data, TODAY, force) == want


@pytest.mark.parametrize("text,want", [
    ("2025-02-18 07:48 AM", dt.date(2025, 2, 18)), ("2026-09-30 17:02", dt.date(2026, 9, 30)), ("2026-10-01", dt.date(2026, 10, 1)),
    ("4/21/2022 11:36 PM", dt.date(2022, 4, 21)), ("03/03/2026 00:31", dt.date(2026, 3, 3)), ("4/21/2022 11:36PM", dt.date(2022, 4, 21)),
    ("  2026-10-01\t", dt.date(2026, 10, 1)), ("", None), ("tomorrow", None), (None, None),
])
def test_parse_when(text, want):
    assert ss.parse_when(text) == want


@pytest.mark.parametrize("text,want", [
    ("HTTP 403 Forbidden", "Access issue"), ("access DENIED", "Access issue"), ("HTTP 401 Unauthorized", "Access issue"),
    ("HTTP 404 Not Found", "File missing issue"), ("could not resolve host", "File missing issue"),
    ("Entimice returned empty content for file | path=/a", "File corrupt issue"), ("Empty data from x", "No data available issue"),
    ("listing timed out", "Timeout issue"), ("Unknown destination 'x'", "Configuration issue"),
    ("Missing studyId in submission_data", "Configuration issue"), ("boom", "Other issue"),
    ("403 and 404", "Access issue"),
])
def test_reason(text, want):
    assert ss.reason(text) == want


def test_failure_count():
    once = {"refreshSchedule": "Once", "lastTransferStatus": "FAILED", "error": "x | Consecutive failed runs: 3 of 10"}
    assert ss.failure_count(once, "e") == ("FAILED", "e | Consecutive failed runs: 4 of 10")
    assert ss.failure_count({**once, "lastTransferStatus": "DONE"}, "e") == ("FAILED", "e | Consecutive failed runs: 1 of 10")
    nine = {**once, "error": "x | Consecutive failed runs: 9 of 10"}
    assert ss.failure_count(nine, "e") == ("FAILED_INGESTION_STOPPED", "e | Consecutive failed runs: 10 of 10 | Study is failing "
                                           "for last 10 runs so disabling the trigger for this study")
    reenabled = {**once, "error": "x | Consecutive failed runs: 10 of 10 | Study is failing for last 10 runs so disabling the trigger for this study"}
    assert ss.failure_count(reenabled, "e")[1] == "e | Consecutive failed runs: 1 of 10"
    assert ss.failure_count({**once, "error": "x | Consecutive failed runs: 12 of 10"}, "e")[1].endswith("1 of 10")
    assert ss.failure_count({"refreshSchedule": "Daily", "lastTransferStatus": "failed", "error": "Consecutive failed runs: 14"}, "e") == \
        ("FAILED_INGESTION_STOPPED", "e | Consecutive failed runs: 15 of 15 | Study is failing for last 15 runs so disabling the trigger for this study")
    assert ss.failure_count({"refreshSchedule": "Weekly", "lastTransferStatus": "FAILED"}, "e") == ("FAILED", "e")


def test_study_result_order():
    r = ss.study_result
    assert r(listing_error="HTTP 404 x", files_found=3, config_error="Unknown destination 'a'", file_errors=[], copied=0) == \
        ("FAILED", "HTTP 404 x | Reason: File missing issue")
    assert r(listing_error="Missing location in submission_data", files_found=0, config_error=None, file_errors=[], copied=0) == \
        ("FAILED", "Missing location in submission_data | Reason: Other issue")
    assert r(listing_error=None, files_found=0, config_error="x", file_errors=[], copied=0)[1] == \
        "Empty data from the Entimice API. | Reason: No data available issue"
    assert r(listing_error=None, files_found=2, config_error="Destination is blank — enter it.", file_errors=[("/a", "x")], copied=0)[1] == \
        "Destination is blank — enter it. | Reason: Configuration issue"
    assert r(listing_error=None, files_found=2, config_error=None, file_errors=[("/a", "HTTP 404"), ("/b", "timed out")], copied=0)[1] == \
        "S3 upload failed for 2 file(s): /a | Reason: File missing issue, /b | Reason: Timeout issue"
    assert r(listing_error=None, files_found=2, config_error=None, file_errors=[], copied=0)[1] == \
        "Files were found in Entimice but none was uploaded to S3. | Reason: Other issue"
    assert r(listing_error=None, files_found=2, config_error=None, file_errors=[], copied=2) == ("DONE", "")


def test_updated_keeps_other_fields():
    data = {"studyId": "S", "lastTransferAt": "old", "custom": {"x": 1}, "error": "e"}
    now = dt.datetime(2026, 10, 20, 6, 2, tzinfo=dt.timezone.utc)
    failed = ss.updated(data, "FAILED", "bad", now, "p1", "Svc")
    assert failed == {"studyId": "S", "lastTransferAt": "old", "custom": {"x": 1}, "error": "bad", "modifiedAt": "2026-10-20 06:02 AM",
                      "lastTransferStatus": "FAILED", "modifiedByPrid": "p1", "modifiedByFullName": "Svc"}
    assert ss.updated(data, "DONE", "", now, "p1", "Svc")["lastTransferAt"] == "2026-10-20 06:02 AM"
    assert data["error"] == "e"  # the original is untouched


@pytest.mark.parametrize("key,ok", [(API_KEY, True), ("dpls_short", False), ("xpls_0123456789abcdefghij", False),
                                    ("dpls_0123456789 abcdefgh", False), (None, False), ("", False)])
def test_api_key(key, ok):
    if ok:
        assert ss.check_api_key(key) == key
    else:
        with pytest.raises(PermissionError) as e:
            ss.check_api_key(key)
        assert (key or "@@") not in str(e.value) or not key


# ---------------------------------------------------------------------------------------------- stand-ins
def _files() -> dict:
    deep = "/root/deep" + "".join(f"/l{n}" for n in range(1, 25))
    return {
        "/root/s1/data/a.sas7bdat": "aaa", "/root/s1/define.xml": "<x/>", "/root/s1/t.csv": {"gzip": "a,b\n" * 500},
        "/root/s1/big.bin": {"size": 17 * 1024 * 1024 + 3},
        "/root/s2/r.pdf": "%PDF",
        "/root/s3/ok.txt": "ok", "/root/s3/gone.txt": "x", "/root/s3/zero.pdf": {"empty": True},
        f"{deep}/too_deep.txt": "d", "/root/deep/l1/near.txt": "n",
        "/root/cfg/a.txt": "a",
    }


@pytest.fixture
def stand_ins():
    ent = {"users": {"u1": "p1", "u2": "p2"}, "files": _files(), "fail_list": {"/root/locked": 403},
           "fail_file": {"/root/s3/gone.txt": 404}, "junk_nodes": True}
    api = {"api_key": API_KEY, "forms": {"f1": []}, "fail_put": []}
    vault = {"token": TOKEN, "secrets": {"secret/entimice": {"entimice_username": "u1", "entimice_password": "p1", "self_service_api_key": API_KEY},
                                         "secret/entimice_clinicalstudies": {"entimice_username": "u2", "entimice_password": "p2"}}}
    urls = start(ent, api, vault)
    yield {"urls": urls, "entimice": ent, "ssapi": api, "vault": vault}
    for s in urls["servers"]:
        s.shutdown()


def test_entimice_adapter(stand_ins, caplog):
    ent = stand_ins["entimice"]
    s = EntimiceSession({"base_url": stand_ins["urls"]["entimice"], "account_by": "{dest}", "accounts": {" ClinicalStudies ": "ref2"}},
                        {"username": "u1", "password": "p1"}, lambda ref: {"username": "u2", "password": "p2"} if ref == "ref2" else {})
    s.prepare([{"dest": "SAS"}, {"dest": "clinicalstudies "}])
    assert sorted(ent["sessions_created"]) == ["u1", "u2"] and len(s.sessions) == 2
    s.bind({"dest": "clinicalstudies"})
    assert s.current == "clinicalstudies"
    s.bind({"dest": "SAS"})
    paths = sorted(f.path for f in s.list("/root/s1", True, 20))
    assert paths == ["big.bin", "data/a.sas7bdat", "define.xml", "t.csv"]  # junk nodes skipped, paths trimmed
    with caplog.at_level("WARNING"):
        deep = [f.path for f in s.list("/root/deep", True, 20)]
    assert deep == ["l1/near.txt"] and "more than 20 levels" in caplog.text
    with pytest.raises(PackageError, match=r"Entimice listing failed \| path=/root/locked \| HTTP 403"):
        s.list("/root/locked", True, 20)
    with s.open("/root/s1", "t.csv") as f:
        assert f.read() == b"a,b\n" * 500 and f.declared is None  # gzip decoded; its encoded length is not the file's size
    with s.open("/root/s1", "define.xml") as f:
        assert f.declared == 4
    with pytest.raises(PackageError, match=r"^Entimice returned empty content for file \| path=/root/s3/zero.pdf$"):
        s.open("/root/s3", "zero.pdf")
    with pytest.raises(PackageError, match="HTTP 404"):
        s.open("/root/s3", "gone.txt")
    s.close()
    assert not ent["live"] and len(ent["sessions_deleted"]) == 2
    bad = EntimiceSession({"base_url": stand_ins["urls"]["entimice"]}, {"username": "u1", "password": "wrong"})
    with pytest.raises(PermissionError, match="Entimice login failed") as e:
        bad.check()
    assert "wrong" not in str(e.value)


def test_part_size_and_content_type():
    assert streaming.part_size(None, 16) == 16 << 20
    assert streaming.part_size(160 << 30, 16) > 16 << 20  # 160 GiB would need 10 240 parts of 16 MiB
    assert (160 << 30) / streaming.part_size(160 << 30, 16) <= 10_000
    assert streaming.content_type("a/B.PDF", {".pdf": "application/pdf"}, "x") == "application/pdf"
    assert streaming.content_type("a/b", {".pdf": "application/pdf"}, "x") == "x"


# ---------------------------------------------------------------------------------------------- whole runs
def _records() -> list[dict]:
    def rec(i, **sd):
        return {"id": i, "submission_data": {"studyId": f"S{i}", "location": "", "destination": "SAS", "refreshSchedule": "Once",
                                             "lastTransferStatus": "", "lastTransferAt": "", "error": "", "keepMe": i, **sd}}
    return [rec(1, location="/root/s1"), rec(2, location="/root/s2", destination="clinicalstudies", refreshSchedule="Daily",
                                                lastTransferStatus="DONE", lastTransferAt="2020-01-01 10:00"),
            rec(3, location="/root/s3"), rec(4, location="/root/locked"), rec(5, location="/root/nothing"),
            rec(6, location="/root/cfg", destination="Other"), rec(7, location="/root/cfg", studyId="\t "),
            rec(8, location=""), rec(9, location="/root/s1", lastTransferStatus="Done"),
            rec(10, location="/root/s1", refreshSchedule="Daily", lastTransferStatus="FAILED_INGESTION_STOPPED")]


def _repo(tmp_path: Path, urls: dict) -> Path:
    repo = tmp_path / "specs"
    shutil.copytree(ROOT / "examples", repo, ignore=shutil.ignore_patterns("dags", "__pycache__"))
    (repo / "pkf.toml").write_text((repo / "pkf.toml").read_text().replace("file:///tmp/pkf-staging", (tmp_path / "staging").as_uri()))
    for rel, old, new in [("sources/entimice_studies.yaml", "http://127.0.0.1:18080", urls["entimice"]),
                          ("triggers/entimice_selfservice.yaml", "http://127.0.0.1:18081", urls["ssapi"]),
                          ("triggers/entimice_selfservice.yaml", "form_id: entimice-studies", "form_id: f1")]:
        p = repo / rel
        assert old in p.read_text()
        p.write_text(p.read_text().replace(old, new))
    return repo


def _lock(repo: Path) -> dict:
    files = build.render(load(repo), "entimice_api")
    build.write(load(repo), files, "entimice_api")
    return tasks.load_lock(str(repo / "dags" / "pkf_entimice_api.py"), "entimice_api")


@pytest.fixture
def run_env(stand_ins, tmp_path, monkeypatch):
    for k in ("AWS_ENDPOINT_URL", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("VAULT_ADDR", stand_ins["urls"]["vault"])
    monkeypatch.setenv("VAULT_TOKEN", TOKEN)
    stand_ins["ssapi"]["forms"]["f1"] = _records()
    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-north-1")
        for b in (SAS, CLIN):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        yield {**stand_ins, "lock": _lock(_repo(tmp_path, stand_ins["urls"])), "s3": s3}


def _ctx(n: int = 1, **params) -> dict:
    return {"run_id": f"manual__test{n}", "logical_date": "2026-10-20T06:00:00+00:00", "params": params, "conf": {}}


def _keys(s3, bucket):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", []))


def test_run_end_to_end(run_env):
    s3, ent, api = run_env["s3"], run_env["entimice"], run_env["ssapi"]
    out = tasks.run_all(run_env["lock"], _ctx())
    summary = out["summary"]
    by_id = {line["id"]: line for line in summary["studies"]}
    assert sorted(by_id) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert summary["not_due"] == {"completed_once": 1, "ingestion_stopped": 1}
    assert summary["study_totals"] == {"DONE": 2, "FAILED": 6}
    assert _keys(s3, SAS) == ["incoming/S1/root/s1/big.bin", "incoming/S1/root/s1/data/a.sas7bdat", "incoming/S1/root/s1/define.xml",
                              "incoming/S1/root/s1/t.csv", "incoming/S3/root/s3/ok.txt"]
    assert _keys(s3, CLIN) == ["S2/ongoing/root/s2/r.pdf"]
    assert s3.get_object(Bucket=SAS, Key="incoming/S1/root/s1/t.csv")["Body"].read() == b"a,b\n" * 500
    head = s3.head_object(Bucket=SAS, Key="incoming/S1/root/s1/big.bin")
    assert head["ContentLength"] == 17 * 1024 * 1024 + 3 and head["ContentType"] == "application/octet-stream"
    assert "-" in head["ETag"]  # a multipart upload
    assert s3.head_object(Bucket=CLIN, Key="S2/ongoing/root/s2/r.pdf")["ContentType"] == "application/pdf"
    assert s3.get_object_tagging(Bucket=SAS, Key="incoming/S1/root/s1/define.xml")["TagSet"] == []
    puts = {int(p["id"]): p["body"]["submission_data"] for p in api["puts"]}
    assert sorted(puts) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert puts[1]["lastTransferStatus"] == "DONE" and puts[1]["error"] == "" and puts[1]["keepMe"] == 1
    assert puts[1]["lastTransferAt"] == puts[1]["modifiedAt"] and puts[1]["modifiedByPrid"] == "svc-entimice"
    assert dt.datetime.strptime(puts[1]["modifiedAt"], "%Y-%m-%d %I:%M %p")
    assert puts[2]["lastTransferStatus"] == "DONE"
    assert puts[3]["error"] == ("S3 upload failed for 2 file(s): /root/s3/gone.txt | Reason: File missing issue, /root/s3/zero.pdf | "
                                "Reason: File corrupt issue | Consecutive failed runs: 1 of 10")
    assert puts[3]["lastTransferAt"] == ""
    assert puts[4]["error"] == "Entimice listing failed | path=/root/locked | HTTP 403 Forbidden | Reason: Access issue | Consecutive failed runs: 1 of 10"
    assert puts[5]["error"] == "Empty data from the Entimice API. | Reason: No data available issue | Consecutive failed runs: 1 of 10"
    assert puts[6]["error"] == ("Unknown destination 'Other' — cannot resolve S3 bucket. | Reason: Configuration issue | "
                                "Consecutive failed runs: 1 of 10")
    assert puts[7]["error"].startswith("Missing studyId in submission_data — cannot build S3 key. | Reason: Configuration issue")
    assert puts[8]["error"].startswith("Missing location in submission_data | Reason: Other issue")
    assert len(ent["sessions_created"]) == len(ent["sessions_deleted"]) and not ent["live"]
    assert set(ent["sessions_created"]) == {"u1", "u2"}  # clinicalstudies used its own account
    # the second run: done studies are not due, the failed ones count on
    tasks.run_all(run_env["lock"], _ctx(2))
    puts2 = {int(p["id"]): p["body"]["submission_data"] for p in api["puts"][8:]}
    assert sorted(puts2) == [3, 4, 5, 6, 7, 8]
    assert puts2[4]["error"].endswith("Consecutive failed runs: 2 of 10")


def test_force_refresh_and_failed_write_back(run_env):
    api = run_env["ssapi"]
    api["fail_put"] = [1]
    out = tasks.run_all(run_env["lock"], _ctx(1, force_refresh="true"))
    assert sorted(line["id"] for line in out["summary"]["studies"]) == [1, 2, 3, 4, 5, 6, 7, 8, 9]  # 9 was completed
    assert out["summary"]["write_back_failed"] == 1 and out["summary"]["written_back"] == 8
    assert 1 not in {int(p["id"]) for p in api["puts"]}


def test_nothing_due_touches_nothing(run_env):
    run_env["ssapi"]["forms"]["f1"] = [r for r in _records() if r["id"] in (9, 10)]
    out = tasks.run_all(run_env["lock"], _ctx())
    assert out["summary"]["studies"] == [] and out["summary"]["not_due"] == {"completed_once": 1, "ingestion_stopped": 1}
    assert run_env["entimice"]["sessions_created"] == [] and run_env["ssapi"]["puts"] == []


@pytest.mark.parametrize("break_it,match", [
    (lambda e: e["ssapi"].update(fail_get=503), "HTTP 503"),
    (lambda e: e["ssapi"].update(not_json=True), "not JSON"),
    (lambda e: e["vault"]["secrets"]["secret/entimice"].update(self_service_api_key="dpls_short"), "api key is not valid"),
])
def test_control_list_failure_fails_the_run(run_env, break_it, match):
    break_it(run_env)
    with pytest.raises(Exception, match=match):
        tasks.run_all(run_env["lock"], _ctx())
    assert run_env["entimice"]["sessions_created"] == [] and run_env["ssapi"]["puts"] == []
    assert _keys(run_env["s3"], SAS) == []


def test_login_failure_fails_the_run_before_any_study(run_env):
    run_env["entimice"]["users"]["u2"] = "changed"  # the clinicalstudies account cannot log in
    with pytest.raises(PermissionError, match="Entimice login failed"):
        tasks.run_all(run_env["lock"], _ctx())
    assert run_env["ssapi"]["puts"] == [] and _keys(run_env["s3"], SAS) == [] and _keys(run_env["s3"], CLIN) == []
    assert not run_env["entimice"]["live"]  # the session that did open was deleted


def test_size_mismatch_fails_the_file(run_env, monkeypatch):
    real = streaming._head_size
    monkeypatch.setattr(streaming, "_head_size", lambda s3, b, k: real(s3, b, k) + (1 if k.endswith("define.xml") else 0))
    run_env["ssapi"]["forms"]["f1"] = _records()[:1]
    out = tasks.run_all(run_env["lock"], _ctx())
    err = out["summary"]["studies"][0]["error"]
    assert err.startswith("S3 upload failed for 1 file(s): /root/s1/define.xml | Reason: Other issue")
    rows = {r["path"]: r for r in out["files"]}
    assert rows["root/s1/define.xml"]["detail"] == "S3 upload size mismatch | key=incoming/S1/root/s1/define.xml | source=4 | target=5"
    assert rows["root/s1/data/a.sas7bdat"]["status"] == "uploaded"  # the other files were delivered


def test_dag_settings(run_env):
    dag = (Path(run_env["lock"]["_dir"]).parents[1] / "pkf_entimice_api.py").read_text()
    assert "schedule='0 6,18 * * *'" in dag and "max_active_runs=1" in dag and "catchup=False" in dag
    assert "check_source" not in dag  # no study due: neither Entimice nor S3 is touched
    assert "'force_refresh': Param('false'" in dag
