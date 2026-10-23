import hashlib
import json

import boto3
import pytest
from moto import mock_aws

from pkf_runtime.kit import PackageContext, DataError
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
KEY = "incoming/study_a.zip"
PROC = "processing/study_a_20261022T100000Z.zip"


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    with mock_aws():
        c = boto3.client("s3", region_name="eu-north-1")
        c.create_bucket(Bucket=B, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        yield c


def _ctx(size=None):
    return PackageContext(
        package_id="m1", name="study_a.zip",
        source={"bucket": B, "key": KEY, "size": size,
                "event_time": "2026-10-22T10:00:00Z"},
        vars={"package_ts": "20261022T100000Z", "stem": "study_a", "ext": ".zip"},
    )


def _body(s3, key):
    return s3.get_object(Bucket=B, Key=key)["Body"].read()


@covers("SEP-1", "R-SEP-1-a", "R-SEP-1-b")
def test_copy_to_processing_keeps_original_and_content(s3):
    """critical: the package is copied to processing/ with identical content, original kept, vars set."""
    data = zip_bytes({"a.pdf": "0123456789"})
    s3.put_object(Bucket=B, Key=KEY, Body=data)
    ctx = lane.sep_1_copy_and_rename_the_package(_ctx(len(data)), make_runtime(SETTINGS))
    sha = hashlib.sha256(data).hexdigest()
    assert ctx.status == "ok"
    assert ctx.artefacts["renamed_key"] == PROC
    assert ctx.vars["source_sha256"] == sha
    assert ctx.vars["source_key"] == KEY
    assert ctx.vars["source_key_hash"] == hashlib.sha1(KEY.encode()).hexdigest()[:12]
    assert _body(s3, PROC) == data
    assert _body(s3, KEY) == data


@covers("SEP-1", "R-SEP-1-c")
def test_already_delivered_deletes_original_and_skips(s3):
    """critical: a verified earlier delivery deletes the incoming original and marks skipped, no processing copy."""
    data = zip_bytes({"a.pdf": "0123456789"})
    sha = hashlib.sha256(data).hexdigest()
    kh = hashlib.sha1(KEY.encode()).hexdigest()[:12]
    out = "outgoing/20261022T100000Z_study_a.zip"
    s3.put_object(Bucket=B, Key=KEY, Body=data)
    s3.put_object(Bucket=B, Key=out, Body=b"delivered",
                  Metadata={"pkf-source-sha256": sha})
    idx = f"index/s3_zip_manifest/study_a/{sha}_{kh}.json"
    s3.put_object(Bucket=B, Key=idx,
                  Body=json.dumps({"source_key": KEY, "outgoing_key": out}).encode())
    ctx = lane.sep_1_copy_and_rename_the_package(_ctx(len(data)), make_runtime(SETTINGS))
    assert ctx.status == "skipped"
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=B).get("Contents", [])]
    assert KEY not in keys
    assert PROC not in keys
    assert out in keys


@covers("SEP-1", "R-SEP-1-d")
def test_source_gone_resumes_or_skips(s3):
    """important: source gone with a processing copy resumes; without one the package is skipped."""
    rt = make_runtime(SETTINGS)
    ctx = lane.sep_1_copy_and_rename_the_package(_ctx(), rt)
    assert ctx.status == "skipped"
    data = zip_bytes({"a.pdf": "x"})
    s3.put_object(Bucket=B, Key=PROC, Body=data)
    ctx = lane.sep_1_copy_and_rename_the_package(_ctx(), rt)
    assert ctx.status == "ok"
    assert ctx.artefacts["renamed_key"] == PROC
    assert ctx.vars["source_sha256"] == hashlib.sha256(data).hexdigest()
