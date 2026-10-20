"""End-to-end dry run: the example specs, repointed at the local stand-ins (dryrun/servers.py), built into DAGs and
executed by a real Airflow 3 (`airflow dags test`) and by `pkf run`. Exits non-zero on the first failed check.

    python dryrun/run.py <servers_state_dir> <work_dir>      # run with the Airflow environment's python
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import boto3
import yaml

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent / "examples"
BIN = Path(sys.executable).parent
DATE = "2026-10-15"
OLD = time.time() - 3600
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  — {detail}" if detail else ""), flush=True)


def sh(*args: str, env: dict, cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def last_json(out: str) -> str:
    return next((l for l in reversed(out.splitlines()) if l.startswith(("[", "{"))), "[]")


def put_tree(root: Path, files: dict[str, bytes], mtime: float = OLD) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (mtime, mtime))


def edit(path: Path, fn) -> None:
    doc = yaml.safe_load(path.read_text())
    fn(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def write(path: Path, doc: dict) -> None:
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def main(servers: Path, work: Path) -> int:
    e = json.loads((servers / "env.json").read_text())
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    repo = work / "specs"
    shutil.copytree(EXAMPLES, repo, ignore=shutil.ignore_patterns("dags", "__pycache__", ".git", ".llm"))
    port = e["ports"]

    # ------------------------------------------------------------------ seed the servers
    csv = lambda n: (b"id,value\n" + b"1,x\n" * n)  # noqa: E731
    put_tree(Path(e["https_root"]), {
        "exports/monitoring/SITE_0001/visits.csv": csv(40), "exports/monitoring/SITE_0001/issues.csv": csv(10),
        "exports/monitoring/SITE_0002/visits.csv": csv(30), "exports/monitoring/SITE_0002/notes.txt": b"not a csv\n",
        "exports/monitoring_bad/SITE_0001/visits.csv": csv(5), "exports/monitoring_bad/summary.csv": csv(2)})
    put_tree(Path(e["https_root"]), {"exports/monitoring/SITE_0002/partial.csv": csv(1)}, mtime=time.time())  # too young
    day = DATE.replace("-", "")
    put_tree(Path(e["sftp_root"]), {
        f"outbound/{day}/D001/CONTROL_2.txt": b"2\n", f"outbound/{day}/D001/lb_a.csv": csv(3),
        f"outbound/{day}/D001/sub/lb_b.csv": csv(4), f"outbound/{day}/D001/upload.tmp": b"x",
        f"outbound/{day}/D002/CONTROL_1.txt": b"1\n", f"outbound/{day}/D002/lb_c.csv": csv(5),
        "outbound/20261016/D001/CONTROL_3.txt": b"3\n", "outbound/20261016/D001/lb_a.csv": csv(1),
        "outbound/20261016/D001/lb_b.csv": csv(1)})
    put_tree(Path(e["ftp_root"]), {"vendor/SITE_0007/ae.csv": csv(7), "vendor/SITE_0007/readme.pdf": b"%PDF"})
    local_drop = work / "local_drop"
    put_tree(local_drop, {f"{DATE}/batch1/img_001.dcm": b"\x00" * 2048, f"{DATE}/batch1/img_002.dcm": b"\x01" * 4096})

    os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_DEFAULT_REGION="eu-west-1")
    aws = dict(endpoint_url=e["aws_endpoint"], region_name="eu-west-1")
    s3 = boto3.client("s3", **aws)
    for b in ("partner-drop-bucket", "az-rd-lake-raw", "pkf-staging"):
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    for k, body in {f"incoming/{DATE}/a/part-0001.parquet": b"P" * 5000, f"incoming/{DATE}/a/part-0002.parquet": b"Q" * 3000,
                    f"incoming/{DATE}/_SUCCESS": b""}.items():
        s3.put_object(Bucket="partner-drop-bucket", Key=k, Body=body)
    sqs = boto3.client("sqs", **aws)
    dlq = sqs.create_queue(QueueName="partner-drop-events-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    queue = sqs.create_queue(QueueName="partner-drop-events", Attributes={
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "2"})})["QueueUrl"]
    q_arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    s3.put_bucket_notification_configuration(Bucket="partner-drop-bucket", NotificationConfiguration={"QueueConfigurations": [
        {"QueueArn": q_arn, "Events": ["s3:ObjectCreated:*"], "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "incoming/"}]}}}]})
    sm = boto3.client("secretsmanager", **aws)
    sm.create_secret(Name="rd/dev/cro_https", SecretString=json.dumps({"username": e["https_user"], "password": e["https_password"]}))
    sm.create_secret(Name="rd/dev/cro_https_wrong", SecretString=json.dumps({"username": e["https_user"], "password": "wrong"}))
    Path(e["vault_store"]).write_text(json.dumps({"secret/rd/dev/lab_sftp": {
        "username": "lab", "private_key": Path(e["client_key"]).read_text()}}))
    (work / "ftp_creds.yaml").write_text(yaml.safe_dump({"username": e["ftp_user"], "password": e["ftp_password"]}))

    # ------------------------------------------------------------------ repoint the specs at the stand-ins
    (repo / "pkf.toml").write_text((repo / "pkf.toml").read_text().replace("file:///tmp/pkf-staging", "s3://pkf-staging/runs"))
    https = f"https://127.0.0.1:{port['https']}"

    def cro(doc):
        doc["connection"].update(base_url=https + "/html", ca_bundle=e["ca_bundle"])

    def lab(doc):
        doc["connection"].update(host="127.0.0.1", port=port["sftp"], host_key=e["host_key"])
        doc["credentials"]["vault_address"] = e["vault_address"]

    def s3_endpoint(doc):
        doc["connection"]["endpoint_url"] = e["aws_endpoint"]

    edit(repo / "sources/cro_https.yaml", cro)
    edit(repo / "sources/lab_sftp.yaml", lab)
    edit(repo / "sources/archive_bucket.yaml", s3_endpoint)
    edit(repo / "destinations/lake_raw.yaml", s3_endpoint)
    edit(repo / "sources/partner_drop_event_objects.yaml", s3_endpoint)
    edit(repo / "destinations/partner_landing.yaml", s3_endpoint)

    def sqs_trigger(doc):
        doc["connection"].update(endpoint_url=e["aws_endpoint"], region="eu-west-1")
        doc["params"]["queue_url"] = queue

    edit(repo / "triggers/partner_drop_sqs.yaml", sqs_trigger)

    # SAS folder restructure: a scheduled move inside one bucket (custom_delivery); component names from the split
    sas = yaml.safe_load((repo / "pipelines/sas_folder_restructure.yaml").read_text())
    sas_conn = yaml.safe_load((repo / f"connections/{sas['connection']}.yaml").read_text())
    for kind, name in (("sources", sas_conn["source"]), ("destinations", sas_conn["destination"])):
        edit(repo / f"{kind}/{name}.yaml", s3_endpoint)
    sas_bucket = yaml.safe_load((repo / f"sources/{sas_conn['source']}.yaml").read_text())["connection"]["bucket"]
    boto3.client("s3", endpoint_url=e["aws_endpoint"], region_name="eu-north-1").create_bucket(
        Bucket=sas_bucket, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    sas_keys = sorted({it.split(":")[0] for ex in sas["examples"] for it in ex["listing"]})
    for k in sas_keys:
        s3.put_object(Bucket=sas_bucket, Key=k, Body=b"SAS " + k.encode(), Tagging="SOURCE_TAG=1")
    s3.put_object(Bucket=sas_bucket, Key="incoming/d0000c00004/", Body=b"")  # a folder marker
    big = "incoming/BIG-0001/root/cdar/d1/BIG-0001/ar/csr/adam/prod/data/big.sas7bdat"
    s3.put_object(Bucket=sas_bucket, Key=big, Body=b"S" * (20 * 1024 * 1024))  # multipart server-side copy

    # extra components: the other listing modes and protocols, and a single_transform pipeline
    base_src = yaml.safe_load((repo / "sources/cro_https.yaml").read_text())
    for name, listing in (("cro_dav", "webdav"), ("cro_json", "json_index")):
        d = json.loads(json.dumps(base_src))
        d.update(name=name, description=f"CRO export over {listing}")
        d["connection"].update(base_url=f"{https}/{'dav' if listing == 'webdav' else 'json'}", listing=listing)
        write(repo / f"sources/{name}.yaml", d)
    write(repo / "sources/vendor_ftps.yaml", {
        "api_version": "pkf/v1", "kind": "source", "name": "vendor_ftps", "type": "ftp", "description": "Vendor FTPS",
        "connection": {"host": "127.0.0.1", "port": port["ftp"], "tls": "explicit", "ca_bundle": e["ca_bundle"]},
        "credentials": {"provider": "local_file", "ref": str(work / "ftp_creds.yaml")},
        "extraction": {"root": "/vendor", "include": ["**/*.csv"]}})
    write(repo / "sources/imaging_local.yaml", {
        "api_version": "pkf/v1", "kind": "source", "name": "imaging_local", "type": "local", "description": "Imaging drop",
        "connection": {"base_path": str(local_drop)}, "extraction": {"root": "{ds}", "include": ["**/*.dcm"]}})
    for name, src in (("dav_to_lake", "cro_dav"), ("json_to_lake", "cro_json"), ("ftps_to_lake", "vendor_ftps"),
                      ("imaging_to_lake", "imaging_local")):
        write(repo / f"connections/{name}.yaml", {"api_version": "pkf/v1", "kind": "connection", "name": name,
                                                  "source": src, "destination": "lake_raw"})
    for name, conn, tr, split in (("cro_dav_single", "dav_to_lake", "csv_zip_bundle", "single_transform"),
                                  ("cro_json_daily", "json_to_lake", "csv_zip_bundle", "per_step"),
                                  ("vendor_ftps_daily", "ftps_to_lake", "csv_zip_bundle", "per_step"),
                                  ("imaging_mirror", "imaging_to_lake", None, "per_step")):
        write(repo / f"pipelines/{name}.yaml", {
            "api_version": "pkf/v1", "kind": "pipeline", "name": name, "description": f"dry run: {name}", "owner": "dry-run",
            "connection": conn, "transformation": tr, "package_name": "{source}_{ds_nodash}",
            "params": {"input_path": "/exports/monitoring"} if "cro" in name else {}, "schedule": "@daily",
            "start_date": "2026-10-01", "task_split": split})

    env = {**os.environ, "PKF_AWS_ENDPOINT_URL": e["aws_endpoint"], "PKF_STAGING_ENDPOINT_URL": e["aws_endpoint"],
           "VAULT_TOKEN": e["vault_token"], "PKF_ENV": "dev", "PATH": f"{BIN}:{os.environ['PATH']}"}
    env.pop("PKF_STAGING_URL", None)
    pkf = str(BIN / "pkf")

    r = sh(pkf, "validate", env=env, cwd=repo)
    check("validate repointed specs", r.returncode == 0, r.stdout[-400:] + r.stderr[-400:] if r.returncode else "")
    r = sh(pkf, "build", "--all", env=env, cwd=repo)
    check("build all pipelines", r.returncode == 0, r.stdout[-600:] + r.stderr[-400:] if r.returncode else "")
    r = sh(pkf, "build", "--all", "--check", env=env, cwd=repo)
    check("build --check is clean", r.returncode == 0)
    if any(not ok for _, ok, _ in RESULTS):
        return summary()

    # ------------------------------------------------------------------ Airflow 3
    ahome = work / "airflow"
    aenv = {**env, "AIRFLOW_HOME": str(ahome), "AIRFLOW__CORE__DAGS_FOLDER": str(repo / "dags"),
            "AIRFLOW__CORE__LOAD_EXAMPLES": "False", "AIRFLOW__CORE__UNIT_TEST_MODE": "False",
            "AIRFLOW__LOGGING__LOGGING_LEVEL": "INFO", "PKF_KEEP_STAGING": ""}
    r = sh(str(BIN / "airflow"), "db", "migrate", env=aenv, cwd=work)
    check("airflow db migrate", r.returncode == 0, r.stderr[-500:] if r.returncode else "")
    r = sh(str(BIN / "airflow"), "dags", "reserialize", env=aenv, cwd=work)
    check("airflow dags reserialize", r.returncode == 0, r.stderr[-500:] if r.returncode else "")
    r = sh(str(BIN / "airflow"), "dags", "list", "-o", "json", env=aenv, cwd=work)
    ids = sorted(d["dag_id"] for d in json.loads(last_json(r.stdout))) if r.returncode == 0 else []
    check("airflow parses 9 DAGs without import errors", len([i for i in ids if i.startswith("pkf_")]) == 9, f"{ids} {r.stderr[-300:]}")
    r = sh(str(BIN / "airflow"), "dags", "list-import-errors", "-o", "json", env=aenv, cwd=work)
    check("no import errors", r.returncode == 0 and json.loads(last_json(r.stdout)) == [], r.stdout[-400:])

    def dag_test(pipeline: str, conf: dict | None = None, date: str = DATE) -> tuple[bool, str]:
        args = [str(BIN / "airflow"), "dags", "test", f"pkf_{pipeline}", f"{date}T02:00:00+00:00"]
        if conf:
            args += ["-c", json.dumps(conf)]
        try:
            r = sh(*args, env=aenv, cwd=work, timeout=420)
        except subprocess.TimeoutExpired as exc:
            return False, f"TIMEOUT (a task is probably waiting for a retry)\n{exc.stdout or ''}"[-3000:]
        out = r.stdout + r.stderr
        (work / f"airflow_{pipeline}_{date}_{len(RESULTS)}.log").write_text(out)
        failed = r.returncode != 0 or "state=failed" in out.replace(" ", "") or "DagRunState.FAILED" in out
        return not failed, out

    def keys(prefix: str) -> dict[str, int]:
        resp = s3.list_objects_v2(Bucket="az-rd-lake-raw", Prefix=prefix)
        return {o["Key"][len(prefix):]: o["Size"] for o in resp.get("Contents", [])}

    # 1. HTTPS autoindex + basic auth from Secrets Manager → csv_zip_bundle (per_step) → S3
    ok, out = dag_test("cro_monitoring_daily")
    pre = f"raw/cro_https/{DATE}/D1234C00001_{day}/"
    got = keys(pre)
    check("airflow: cro_monitoring_daily succeeds", ok, out[-800:] if not ok else "")
    check("cro: zip + manifest + _SUCCESS delivered", sorted(got) == ["D1234C00001_20261015.zip", "_SUCCESS", "manifest.json"], str(got))
    if f"D1234C00001_{day}.zip" in got:
        z = zipfile.ZipFile(io.BytesIO(s3.get_object(Bucket="az-rd-lake-raw", Key=pre + f"D1234C00001_{day}.zip")["Body"].read()))
        check("cro: zip holds the three settled CSVs (young partial.csv and notes.txt left out)",
              sorted(z.namelist()) == ["SITE_0001/issues.csv", "SITE_0001/visits.csv", "SITE_0002/visits.csv"], str(z.namelist()))
        man = json.loads(s3.get_object(Bucket="az-rd-lake-raw", Key=pre + "manifest.json")["Body"].read())
        check("cro: manifest has size/md5/sha256 of the zip", "md5" in json.dumps(man) and "sha256" in json.dumps(man), json.dumps(man)[:300])
        head = s3.head_object(Bucket="az-rd-lake-raw", Key=pre + f"D1234C00001_{day}.zip")
        check("cro: object metadata carries pkf-sha256 and SSE", "pkf-sha256" in head.get("Metadata", {}) and head.get("ServerSideEncryption") == "aws:kms",
              str({k: head.get(k) for k in ("Metadata", "ServerSideEncryption")}))
    left = s3.list_objects_v2(Bucket="pkf-staging", Prefix="runs/cro_monitoring_daily/").get("KeyCount", 0)
    check("cro: staging cleared by report", left == 0, f"{left} objects left")

    # 2. same run again → identical objects skipped
    ok, out = dag_test("cro_monitoring_daily")
    check("airflow: rerun succeeds (idempotent)", ok, out[-600:] if not ok else "")
    check("rerun: report shows skipped_identical", '"skipped_identical": 2' in out or "'skipped_identical': 2" in out,
          next((l for l in out.splitlines() if "skipped_identical" in l), "")[:300])

    # 3. run param override: a bad layout must be refused, nothing delivered
    ok, out = dag_test("cro_monitoring_daily", {"input_path": "/exports/monitoring_bad"}, date="2026-10-14")
    check("airflow: bad layout refused (run fails)", not ok)
    check("bad layout: fail-fast PackageError, no retry", "PackageError" in out and "up_for_retry" not in out.lower(),
          next((l for l in out.splitlines() if "PackageError" in l), "")[:300])
    check("bad layout: nothing delivered", keys("raw/cro_https/2026-10-14/") == {})

    # 4. SFTP (pinned host key, key from Vault KV v2) → custom-step transformation
    ok, out = dag_test("lab_deliveries")
    got = keys(f"raw/lab_sftp/{DATE}/lab_{day}/")
    check("airflow: lab_deliveries succeeds", ok, out[-800:] if not ok else "")
    check("lab: restructured files + manifest", sorted(got) == ["_SUCCESS", "data/D001/lb_a.csv", "data/D001/lb_b.csv", "data/D002/lb_c.csv", "manifest.csv"], str(got))
    if "manifest.csv" in got:
        m = s3.get_object(Bucket="az-rd-lake-raw", Key=f"raw/lab_sftp/{DATE}/lab_{day}/manifest.csv")["Body"].read().decode()
        check("lab: manifest.csv has md5 and sha256 columns", m.splitlines()[0].startswith("path,size,md5,sha256"), m.splitlines()[0])
    ok, out = dag_test("lab_deliveries", date="2026-10-16")
    check("airflow: lab control count mismatch refused", not ok and "PackageError" in out)

    # 5. S3 → S3 without transformation
    ok, out = dag_test("archive_mirror")
    got = keys(f"raw/archive_bucket/{DATE}/partner_{day}/")
    check("airflow: archive_mirror succeeds", ok, out[-800:] if not ok else "")
    check("archive: files copied unchanged", got.get("a/part-0001.parquet") == 5000 and got.get("a/part-0002.parquet") == 3000, str(got))

    # 7. event-driven: zips land under incoming/ → S3 notifications → SQS → mapped package group → landing/partner/<date>/
    import datetime as _dt

    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    s3.put_object(Bucket="partner-drop-bucket", Key="incoming/studyA/batch1.zip", Body=b"PK\x03\x04 batch one")
    s3.put_object(Bucket="partner-drop-bucket", Key="incoming/my file.zip", Body=b"PK\x03\x04 spaced name")
    s3.put_object(Bucket="partner-drop-bucket", Key="incoming/readme.txt", Body=b"not a zip")
    s3.put_object(Bucket="partner-drop-bucket", Key="outgoing/other.zip", Body=b"PK no notification")
    sqs.send_message(QueueUrl=queue, MessageBody="not json at all")
    ok, out = dag_test("partner_zip_copy", date=DATE)
    got = keys(f"landing/partner/{today}/")
    check("airflow: partner_zip_copy delivers both zips (each in its own mapped branch)",
          sorted(got) == ["my file.zip", "studyA/batch1.zip"], str(got))
    check("partner: run reports the poison message (run failed, zips still delivered)", not ok and "1 event(s) failed" in out,
          next((l for l in out.splitlines() if "event(s) failed" in l), "")[:300])
    check("partner: mapped task group ran per package", out.count("package.extract") >= 2 or "map_index=1" in out.replace(" ", ""),
          "")
    attrs = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    left = int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])
    check("partner: delivered, filtered and test-event messages deleted; only the poison message left", left == 1, str(attrs))
    ok, out = dag_test("partner_zip_copy", date=DATE)
    check("partner: poison message released and read again (2nd receive): run red, nothing re-delivered",
          not ok and "0 package(s) and 1 event(s) failed" in out, next((l for l in out.splitlines() if "event(s) failed" in l), "")[:300])
    ok, out = dag_test("partner_zip_copy", date=DATE)
    dlq_n = int(sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]["ApproximateNumberOfMessages"])
    check("partner: after maxReceiveCount the poison message is in the dead-letter queue; the run is a green no-op",
          ok and '"packages": 0' in out and dlq_n == 1, f"dlq={dlq_n}")
    s3.put_object(Bucket="partner-drop-bucket", Key="incoming/studyA/batch1.zip", Body=b"PK\x03\x04 batch one")  # same content again
    ok, out = dag_test("partner_zip_copy", date=DATE)
    check("partner: duplicate delivery of identical zip is skipped", ok and '"skipped_identical": 1' in out,
          next((l for l in out.splitlines() if "run summary" in l), "")[:300])

    # 8. scheduled move inside one bucket: route by path rules, server-side copy, HEAD, delete the source, replace tags
    ok, out = dag_test("sas_folder_restructure")
    objs = {o["Key"]: o["Size"] for o in s3.list_objects_v2(Bucket=sas_bucket).get("Contents", [])}
    expected = sorted({k for ex in sas["examples"] for k in ex["expected_keys"]} | {"BIG-0001/primary/complete/adam/big.sas7bdat"})
    left = sorted(k for k in objs if k.startswith("incoming/"))
    check("airflow: sas_folder_restructure succeeds", ok, out[-1500:] if not ok else "")
    check("sas: every routable file moved to its target (incl. error/)", sorted(k for k in objs if not k.startswith("incoming/")) == expected,
          str(sorted(set(expected) ^ {k for k in objs if not k.startswith("incoming/")})))
    check("sas: sources deleted; skipped files and the folder marker stay in incoming/",
          left == ["incoming/04-3077/root/data_reuse/d525/04-3077/shared/adam/prod/data/no_extension", "incoming/d0000c00004/",
                   "incoming/readme.txt"], str(left))
    check("sas: 20 MiB object copied server-side (multipart) intact", objs.get("BIG-0001/primary/complete/adam/big.sas7bdat") == 20 * 1024 * 1024,
          str(objs.get("BIG-0001/primary/complete/adam/big.sas7bdat")))
    tag = lambda k: {t["Key"]: t["Value"] for t in s3.get_object_tagging(Bucket=sas_bucket, Key=k)["TagSet"]}  # noqa: E731
    good = "d0000c00004/primary/complete/adam/a.sas7bdat"
    if good in objs:
        check("sas: moved file's tags replaced by exactly ADDITIONAL_TAG = original path",
              tag(good) == {"ADDITIONAL_TAG": "d0000c00004/root/cdar/d000/d0000c00004/ar/csr/adam/dev/data/data_area/a.sas7bdat"}, str(tag(good)))
    err = next((k for k in objs if k.startswith("error/")), None)
    check("sas: files moved to error/ keep their tags (no ADDITIONAL_TAG)", err is not None and "ADDITIONAL_TAG" not in tag(err), str(err and tag(err)))
    check("sas: no package staged (direct delivery)",
          s3.list_objects_v2(Bucket="pkf-staging", Prefix="runs/sas_folder_restructure/").get("KeyCount", 0) == 0)
    ok, out = dag_test("sas_folder_restructure")
    check("sas: next run is a green no-op (only skipped files left)", ok and ('"packages": 0' in out or "'packages': 0" in out),
          next((l for l in out.splitlines() if "run summary" in l), out[-400:])[:300])

    # 6. WebDAV listing, single_transform task split
    ok, out = dag_test("cro_dav_single")
    got = keys(f"raw/cro_dav/{DATE}/D1234C00001_{day}/")
    check("airflow: cro_dav_single (webdav, single_transform) succeeds", ok and "D1234C00001_20261015.zip" in got, out[-800:] if not ok else str(got))

    # ------------------------------------------------------------------ pkf run (local runner)
    def pkf_run(pipeline: str, *extra: str) -> tuple[bool, dict]:
        r = sh(pkf, "--json", "run", pipeline, "--date", DATE, *extra, env=env, cwd=repo)
        try:
            doc = json.loads(r.stdout)
        except json.JSONDecodeError:
            doc = {"raw": r.stdout[-600:] + r.stderr[-600:]}
        return r.returncode == 0, doc

    ok, doc = pkf_run("cro_json_daily")
    check("pkf run: json_index listing", ok and "D1234C00001_20261015.zip" in keys(f"raw/cro_json/{DATE}/D1234C00001_{day}/"), json.dumps(doc)[-500:])
    ok, doc = pkf_run("vendor_ftps_daily")
    check("pkf run: FTPS (explicit TLS, local_file credentials)", ok and "D1234C00001_20261015.zip" in keys(f"raw/vendor_ftps/{DATE}/D1234C00001_{day}/"), json.dumps(doc)[-500:])
    ok, doc = pkf_run("imaging_mirror")
    check("pkf run: local source, no transformation", ok and len(keys(f"raw/imaging_local/{DATE}/imaging_local_{day}/")) == 3, json.dumps(doc)[-500:])

    # wrong secret → CredentialError-style fail fast, not a retry loop
    edit(repo / "sources/cro_json.yaml", lambda d: d["credentials"].update(ref="rd/{env}/cro_https_wrong"))
    ok, doc = pkf_run("cro_json_daily")
    check("pkf run: wrong password fails fast (PermissionError, HTTP 401)", not ok and "PermissionError" in json.dumps(doc) and "401" in json.dumps(doc), json.dumps(doc)[-400:])
    edit(repo / "sources/cro_json.yaml", lambda d: d["credentials"].update(ref="rd/{env}/missing"))
    ok, doc = pkf_run("cro_json_daily")
    check("pkf run: missing secret is a CredentialError", not ok and "CredentialError" in json.dumps(doc), json.dumps(doc)[-400:])
    edit(repo / "sources/cro_json.yaml", lambda d: d["credentials"].update(ref="rd/{env}/cro_https"))
    edit(repo / "sources/cro_json.yaml", lambda d: d["connection"].update(ca_bundle=None))
    ok, doc = pkf_run("cro_json_daily")
    check("pkf run: untrusted TLS certificate fails fast (PermissionError)", not ok and "PermissionError" in json.dumps(doc), json.dumps(doc)[-400:])
    edit(repo / "sources/lab_sftp.yaml", lambda d: d["connection"].update(host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM+q46zWGkXM1yoev4CEou+F50Sc0fxiUJbCZpG6sHPp"))
    ok, doc = pkf_run("lab_deliveries")
    check("pkf run: SFTP host key mismatch fails fast", not ok and "host key" in json.dumps(doc), json.dumps(doc)[-400:])
    return summary()


def summary() -> int:
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for n in failed:
        print("  failed:", n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
