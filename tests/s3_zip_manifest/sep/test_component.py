import hashlib

import boto3
import pytest
from moto import mock_aws

from pkf_runtime.kit import PackageContext
from pkf_runtime.kit.ops import run_op
from pkf_runtime.kit.testing import covers, make_runtime, zip_bytes
from pkf_pipelines.s3_zip_manifest import sep as lane

B = "dev-dpl-dstore-sas-eu-north-1-992382551516"
SETTINGS = {
    "region": "eu-north-1", "aws_account": "992382551516",
    "source_bucket": B, "target_bucket": B,
    "incoming_prefix": "incoming/", "processing_prefix": "processing/",
    "staging_prefix": "staging_test/", "outgoing_prefix": "outgoing/",
    "index_prefix": "index/s3_zip_manifest/",
    "report_prefix": "reports/s3_zip_manifest/",
    "queue_url": "create", "dlq_url": "create",
    "kms_key_arn": "arn:aws:kms:eu-north-1:992382551516:key/x",
    "aws_conn_id": "aws_default", "alert_email": "",
}


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    with mock_aws():
        c = boto3.client("s3", region_name="eu-north-1")
        c.create_bucket(Bucket=B, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        yield c


def _ctx(stem):
    return PackageContext(
        package_id="m-" + stem, name=f"{stem}.zip",
        source={"bucket": B, "key": f"incoming/{stem}.zip",
                "event_time": "2026-10-22T10:00:00Z"},
        vars={"package_ts": "20261022T100000Z", "stem": stem, "ext": ".zip"},
    )


def _keys(s3):
    return [o["Key"] for o in s3.list_objects_v2(Bucket=B).get("Contents", [])]


@covers("SEP-1", "SEP-2")
def test_happy_path_study_a_lane(s3):
    """functional: SEP-1 then SEP-2 leave only the verified processing copy and the expected vars."""
    data = zip_bytes({"a.pdf": "0123456789", "b/c.pdf": "01234567890123456789"})
    s3.put_object(Bucket=B, Key="incoming/study_a.zip", Body=data)
    rt = make_runtime(SETTINGS)
    ctx = lane.sep_1_copy_and_rename_the_package(_ctx("study_a"), rt)
    assert ctx.status == "ok"
    assert ctx.artefacts["renamed_key"] == "processing/study_a_20261022T100000Z.zip"
    assert ctx.vars["source_sha256"] == hashlib.sha256(data).hexdigest()
    assert ctx.vars["source_key"] == "incoming/study_a.zip"
    assert sorted(_keys(s3)) == ["incoming/study_a.zip",
                                 "processing/study_a_20261022T100000Z.zip"]
    ctx = run_op("s3.delete", ctx, rt, {"missing_ok": True})
    assert _keys(s3) == ["processing/study_a_20261022T100000Z.zip"]
    body = s3.get_object(Bucket=B, Key="processing/study_a_20261022T100000Z.zip")["Body"].read()
    assert body == data


@covers("SEP-1", "SEP-2")
def test_replayed_message_source_gone_lane(s3):
    """functional: a replayed event with no source and no processing copy is skipped and creates nothing."""
    rt = make_runtime(SETTINGS)
    ctx = lane.sep_1_copy_and_rename_the_package(_ctx("study_b"), rt)
    assert ctx.status == "skipped"
    assert _keys(s3) == []
