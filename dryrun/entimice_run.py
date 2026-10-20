"""End-to-end local run of examples/pipelines/entimice_api.yaml against local stand-ins.

  python dryrun/entimice_run.py [--runs N] [--out DIR]

Needs a moto server for S3 (AWS_ENDPOINT_URL, e.g. `moto_server -p 5000` and AWS_ENDPOINT_URL=http://127.0.0.1:5000).
Starts Entimice, the Self-Service API and Vault on the ports the example specs use (18080, 18081, 18200), loads a
control list that exercises every rule of the requirement, runs `pkf run entimice_api` N times (default 2) and writes
what the portal, S3 and Entimice saw to DIR (default ./entimice-run). Prints a summary.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent / "examples"
sys.path.insert(0, str(HERE))

from entimice_servers import start  # noqa: E402

API_KEY = "dpls_local_test_key_0123456789"
VAULT_TOKEN = "local-vault-token"
BUCKETS = {"SAS": "dev-dpl-dstore-sas-eu-north-1", "clinicalstudies": "dev-dpl-clinicalstudies-eu-north-1"}
D1 = "/root/cdar/d000/d0000c00001/ar"


def control_list() -> list[dict]:
    def rec(i, **sd):
        return {"id": i, "form_id": "entimice-studies", "submission_data": {
            "studyId": "", "location": "", "destination": "", "refreshSchedule": "Once", "lastTransferStatus": "",
            "lastTransferAt": "", "error": "", "studyTitle": f"untouched field {i}", **sd}}
    deep = "/root/deep" + "".join(f"/l{n}" for n in range(1, 23))
    return [
        rec(101, studyId="D0000C00001", location=f" {D1} ", destination="SAS"),
        rec(102, studyId="\tD0000C00002\t", location="/root/cdar/d000/d0000c00002", destination="clinicalstudies",
            refreshSchedule="Daily", lastTransferStatus="DONE", lastTransferAt="2025-02-18 07:48 AM"),
        rec(103, studyId="D3", location="/root/cdar/d3", destination="SAS", refreshSchedule="Weekly",
            lastTransferStatus="Completed", lastTransferAt="2099-01-01"),
        rec(104, studyId="D4", location="/root/cdar/d4", destination="SAS", refreshSchedule="Daily",
            lastTransferStatus="FAILED_INGESTION_STOPPED"),
        rec(105, studyId="D5", location="/root/forbidden", destination="SAS"),
        rec(106, studyId="D6", location="/root/empty", destination="SAS", refreshSchedule="Daily", lastTransferStatus="FAILED",
            error="Empty data from the Entimice API. | Reason: No data available issue | Consecutive failed runs: 3 of 15"),
        rec(107, studyId="D7", location="/root/cdar/d7", destination="sas"),
        rec(108, studyId="D8", location="/root/cdar/d7", destination="  "),
        rec(109, studyId=" ", location="/root/cdar/d7", destination="SAS"),
        rec(110, studyId="D10", location="/root/cdar/d10", destination="SAS"),
        rec(111, studyId="D11", location="", destination="SAS", lastTransferStatus="failed",
            error="Missing location in submission_data | Reason: Other issue | Consecutive failed runs: 9 of 10"),
        rec(112, studyId="D12", location="/root/cdar/d12", destination="clinicalstudies", refreshSchedule="monthly",
            lastTransferStatus="done", lastTransferAt="sometime"),
        {"submission_data": {"studyId": "NOID", "location": "/root/cdar/d12", "destination": "SAS", "refreshSchedule": "Once"}},
        rec(113, studyId="D13", location="/root/cdar/d3", destination="SAS", refreshSchedule="Hourly"),
        rec(114, studyId="D14", location="/root/cdar/d12", destination="SAS", refreshSchedule="Weekly", lastTransferStatus="DONE",
            lastTransferAt="4/21/2022 11:36PM"),
        rec(115, studyId="D15", location="/root/deep/l1", destination="SAS"),  # l2…l22 is 21 levels: too_deep.txt is skipped
    ], deep


def entimice_files(deep: str) -> dict:
    return {
        f"{D1}/csr/adam/dev/data/data_area/adadas.sas7bdat": {"size": 20 * 1024 * 1024 + 7},   # multipart (two 16 MiB parts)
        f"{D1}/csr/adam/dev/data/data_area/adsl.sas7bdat": "adsl bytes",
        f"{D1}/define.xml": "<define/>",
        f"{D1}/listing.csv": {"gzip": "a,b\n1,2\n" * 1000},
        f"{D1}/readme.txt": "hello",
        f"{D1}/report.pdf": "%PDF-1.4",
        f"{D1}/sheet.xlsx": "xlsx",
        "/root/cdar/d000/d0000c00002/csr/report.pdf": "%PDF clinical",
        "/root/cdar/d000/d0000c00002/data/old.xls": "xls",
        "/root/cdar/d3/x.csv": "1",
        "/root/cdar/d4/x.csv": "1",
        "/root/empty/.keep/": None,
        "/root/cdar/d7/a.txt": "7",
        "/root/cdar/d10/ok.txt": "ok",
        "/root/cdar/d10/missing.sas7bdat": "gone",
        "/root/cdar/d10/empty.pdf": {"empty": True},
        "/root/cdar/d10/short.bin": {"size": 100, "short": True},
        "/root/cdar/d12/a.dat": "twelve",
        f"{deep}/too_deep.txt": "deep",
        "/root/deep/l1/l2/shallow.txt": "shallow",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--out", default="entimice-run")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("AWS_ENDPOINT_URL"):
        print("set AWS_ENDPOINT_URL to a moto server (moto_server -p 5000)")
        return 2
    records, deep = control_list()
    files = {k: v for k, v in entimice_files(deep).items() if v is not None}
    entimice = {"users": {"svc_entimice": "pw-default", "svc_clinical": "pw-clinical"}, "files": files,
                "fail_list": {"/root/forbidden": 403}, "fail_file": {"/root/cdar/d10/missing.sas7bdat": 404}, "junk_nodes": True}
    ssapi = {"api_key": API_KEY, "forms": {"entimice-studies": copy.deepcopy(records)}, "fail_put": [114]}
    vault = {"token": VAULT_TOKEN, "secrets": {
        "secret/entimice": {"entimice_username": "svc_entimice", "entimice_password": "pw-default", "self_service_api_key": API_KEY},
        "secret/entimice_clinicalstudies": {"entimice_username": "svc_clinical", "entimice_password": "pw-clinical"}}}
    urls = start(entimice, ssapi, vault, {"entimice": 18080, "ssapi": 18081, "vault": 18200})
    import boto3

    s3 = boto3.client("s3", region_name="eu-north-1")
    for b in BUCKETS.values():
        try:
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass
    env = {**os.environ, "VAULT_ADDR": urls["vault"], "VAULT_TOKEN": VAULT_TOKEN, "PKF_ENV": "dev"}
    for n in range(1, a.runs + 1):
        before = len(ssapi["puts"])
        p = subprocess.run([sys.executable, "-m", "pkf.cli", "--json", "run", "entimice_api", "--log-file", str(out / f"run{n}.log")],
                           cwd=EXAMPLES, env=env, capture_output=True, text=True)
        (out / f"run{n}.json").write_text(p.stdout)
        res = json.loads(p.stdout)
        summary = (res.get("data") or {}).get("summary") or {}
        print(f"\n=== run {n}: exit {p.returncode}, ok={res.get('ok')}")
        for line in summary.get("studies", []):
            print(f"  {str(line['id']):>5} {line['study_id'] or '-':12} {line['destination'] or '-':16} found {line['files_found']:>2} "
                  f"copied {line['files_copied']:>2} {line['status']:25} {line['error'][:150]}")
        print("  totals:", summary.get("study_totals"), "| not due:", summary.get("not_due"),
              "| written back:", summary.get("written_back"), "failed:", summary.get("write_back_failed"))
        (out / f"portal_puts_run{n}.json").write_text(json.dumps(ssapi["puts"][before:], indent=2))
    objects = {b: sorted((o["Key"], o["Size"]) for o in s3.list_objects_v2(Bucket=b).get("Contents", [])) for b in BUCKETS.values()}
    types = {f"{b}/{k}": s3.head_object(Bucket=b, Key=k)["ContentType"] for b, objs in objects.items() for k, _ in objs}
    (out / "s3.json").write_text(json.dumps({"objects": objects, "content_types": types}, indent=2))
    sessions = {"created": len(entimice["sessions_created"]), "deleted": len(entimice["sessions_deleted"]), "still_open": len(entimice["live"]),
                "logins_by_user": {u: entimice["sessions_created"].count(u) for u in set(entimice["sessions_created"])}}
    (out / "entimice_sessions.json").write_text(json.dumps(sessions, indent=2))
    (out / "portal_records_after.json").write_text(json.dumps(ssapi["forms"]["entimice-studies"], indent=2))
    print("\nS3:")
    for b, objs in objects.items():
        for k, size in objs:
            print(f"  s3://{b}/{k}  {size} bytes  {types[f'{b}/{k}']}")
    print("Entimice sessions:", sessions)
    print(f"details in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
