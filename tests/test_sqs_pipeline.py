"""Event-driven pipeline end to end, in-process with moto: a zip lands under a prefix → S3 event notification → SQS →
the run reads the queue, copies each zip to the destination prefix, deletes only delivered messages."""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from pkf.cli import main

REGION = "eu-west-1"


def pkf(repo: Path, *args: str, ok: bool | None = True) -> dict:
    buf = io.StringIO()
    import os

    cwd = os.getcwd()
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


TRIGGER_STEPS = [
    {"id": "poll", "op": "sqs", "title": "Read the drop queue", "params": {"queue_url": "{queue_url}", "max_messages": 50, "wait_seconds": 0}},
    {"id": "parse", "op": "parse_json", "title": "Parse", "params": {}},
    {"id": "unwrap", "op": "unwrap_sns", "title": "Unwrap SNS", "params": {}},
    {"id": "records", "op": "s3_records", "title": "S3 records", "params": {}},
    {"id": "zips", "op": "filter", "title": "Zips under incoming/", "params": {"bucket": "partner-drop", "include": ["incoming/**/*.zip"]}},
    {"id": "latest", "op": "dedupe", "title": "One event per object", "params": {}},
]


def write(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PKF_STAGING_URL", raising=False)
    with mock_aws():
        s3, sqs = boto3.client("s3", region_name=REGION), boto3.client("sqs", region_name=REGION)
        for b in ("partner-drop", "lake"):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
        url = sqs.create_queue(QueueName="drop-events")["QueueUrl"]
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        s3.put_bucket_notification_configuration(Bucket="partner-drop", NotificationConfiguration={"QueueConfigurations": [
            {"QueueArn": arn, "Events": ["s3:ObjectCreated:*"], "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "incoming/"}]}}}]})
        repo = tmp_path / "specs"
        (repo).mkdir()
        (repo / "pkf.toml").write_text(f'spec_api_version = "pkf/v1"\n[staging]\nurl = "file://{tmp_path / "staging"}"\n[airflow]\ndags_folder = "dags"\n')
        write(repo / "triggers/zip_arrival.yaml", {"api_version": "pkf/v1", "kind": "trigger", "name": "zip_arrival",
                                                   "schedule": "*/5 * * * *", "connection": {"region": REGION},
                                                   "params": {"queue_url": url}, "steps": TRIGGER_STEPS})
        write(repo / "sources/partner_drop.yaml", {"api_version": "pkf/v1", "kind": "source", "name": "partner_drop", "type": "s3",
                                                   "connection": {"bucket": "partner-drop", "region": REGION},
                                                   "steps": [{"id": "named_object", "op": "from_event", "title": "The object the event names",
                                                              "params": {"root": "incoming/"}}]})
        write(repo / "destinations/lake_landing.yaml", {"api_version": "pkf/v1", "kind": "destination", "name": "lake_landing", "type": "s3",
                                                        "connection": {"bucket": "lake", "region": REGION},
                                                        "delivery": {"prefix": "landing/{source}/{event_date}/", "if_exists": "skip_identical"}})
        write(repo / "connections/drop_to_lake.yaml", {"api_version": "pkf/v1", "kind": "connection", "name": "drop_to_lake",
                                                       "source": "partner_drop", "destination": "lake_landing"})
        msg = {"Records": [{"eventName": "ObjectCreated:Put", "eventTime": "2026-10-16T10:00:00.000Z",
                            "s3": {"bucket": {"name": "partner-drop"}, "object": {"key": "incoming/b1/a+b.zip", "size": 5}}}]}
        write(repo / "pipelines/zip_copy.yaml", {"api_version": "pkf/v1", "kind": "pipeline", "name": "zip_copy", "owner": "t",
                                                 "connection": "drop_to_lake", "trigger": "zip_arrival", "package_name": "{event_key_stem}",
                                                 "start_date": "2026-10-01",
                                                 "examples": [{"name": "one_zip", "messages": [msg, {"Event": "s3:TestEvent"}],
                                                               "listing": ["incoming/b1/a b.zip:5"],
                                                               "expected_keys": ["landing/partner_drop/2026-10-16/b1/a b.zip"]}]})
        yield {"repo": repo, "s3": s3, "sqs": sqs, "url": url}


def _visible(sqs, url) -> int:
    a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    return int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])


def test_zip_copy_end_to_end(env):
    repo, s3, sqs, url = env["repo"], env["s3"], env["sqs"], env["url"]
    pkf(repo, "validate")
    pkf(repo, "test", "pipeline", "zip_copy")
    doc = pkf(repo, "build", "zip_copy")
    dag = (repo / "dags/pkf_zip_copy.py").read_text()
    assert "task_group" in dag and "package.expand(spec=planned)" in dag and "'*/5 * * * *'" in dag
    compile(dag, "dag", "exec")
    for kind in ("trigger", "source", "destination"):
        assert (repo / f"dags/pkf/zip_copy/components/{kind}.py").exists()

    sqs.purge_queue(QueueUrl=url)  # moto's test event on configuration
    s3.put_object(Bucket="partner-drop", Key="incoming/b1/a b.zip", Body=b"PK..1")
    s3.put_object(Bucket="partner-drop", Key="incoming/b2/c.zip", Body=b"PK..22")
    s3.put_object(Bucket="partner-drop", Key="incoming/notes.txt", Body=b"x")       # filtered out, acknowledged
    s3.put_object(Bucket="partner-drop", Key="elsewhere/x.zip", Body=b"x")          # no notification (prefix filter)
    sqs.send_message(QueueUrl=url, MessageBody="not json")                          # poison: stays for the DLQ
    doc = pkf(repo, "run", "zip_copy", "--date", "2026-10-16", ok=False)
    assert "1 event(s) failed" in doc["issues"][0]["msg"]
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="lake").get("Contents", []))
    assert [k.split("/", 3)[-1] for k in keys] == ["b1/a b.zip", "b2/c.zip"]
    assert all(k.startswith("landing/partner_drop/") for k in keys)
    assert _visible(sqs, url) == 1  # only the poison message is left, released for the next run (then the DLQ)

    doc = pkf(repo, "run", "zip_copy", "--date", "2026-10-16", ok=False)  # the poison message again; nothing re-delivered
    assert "0 package(s) and 1 event(s) failed" in doc["issues"][0]["msg"]
    for m in sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages", []):  # an operator removes it
        sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    doc = pkf(repo, "run", "zip_copy", "--date", "2026-10-16", ok=True)  # nothing new: green no-op
    assert doc["data"]["tasks"][-1]["result"]["packages"] == 0


def test_runtime_refuses_edited_component(env):
    repo = env["repo"]
    pkf(repo, "build", "zip_copy")
    from pkf_runtime import components, tasks

    lock = tasks.load_lock(str(repo / "dags/pkf_zip_copy.py"), "zip_copy")
    mod = repo / "dags/pkf/zip_copy/components/source.py"
    mod.write_text(mod.read_text().replace("incoming/", "elsewhere/"))
    components._cache.clear()
    with pytest.raises(PermissionError, match="not the approved"):
        components.load(lock, "source")


def test_pipeline_example_failure_blocks_build(env):
    repo = env["repo"]
    p = repo / "pipelines/zip_copy.yaml"
    d = yaml.safe_load(p.read_text())
    d["examples"][0]["expected_keys"] = ["landing/wrong.zip"]
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    doc = pkf(repo, "build", "zip_copy", ok=False)
    assert any(i["code"] == "example_failed" for i in doc["issues"])


_ = sys
