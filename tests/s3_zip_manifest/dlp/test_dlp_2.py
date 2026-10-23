import pytest
from moto import mock_aws
from pkf_runtime.kit import PackageContext
from pkf_runtime.kit.testing import covers, make_runtime
from pkf_pipelines.s3_zip_manifest import dlp as lane

B = "dev-dpl-dstore-sas-eu-north-1-992382551516"
SETTINGS = {
    "region": "eu-north-1", "aws_account": "992382551516",
    "source_bucket": B, "target_bucket": B,
    "incoming_prefix": "incoming/", "processing_prefix": "processing/",
    "staging_prefix": "staging_test/", "outgoing_prefix": "outgoing/",
    "index_prefix": "index/s3_zip_manifest/", "report_prefix": "reports/s3_zip_manifest/",
    "queue_url": "create", "dlq_url": "create", "aws_conn_id": "aws_default",
    "kms_key_arn": "arn:aws:kms:eu-north-1:992382551516:key/65f013e6-0620-4f49-a814-ce30d321f3dc",
    "alert_email": "",
}
TS = "20261022T100000Z"
PROC = f"processing/study_a_{TS}.zip"
STAGING = f"staging_test/study_a_{TS}.zip"
OUT = f"outgoing/{TS}_study_a.zip"


def _setup(rt, with_outgoing):
    s3 = rt.s3()
    s3.create_bucket(Bucket=B, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    s3.put_object(Bucket=B, Key=PROC, Body=b"z")
    s3.put_object(Bucket=B, Key=STAGING, Body=b"z")
    if with_outgoing:
        s3.put_object(Bucket=B, Key=OUT, Body=b"z")
    return s3


def _ctx():
    return PackageContext(
        package_id="p1", name="study_a.zip",
        source={"bucket": B, "key": "incoming/study_a.zip"},
        vars={"package_ts": TS, "stem": "study_a"},
        artefacts={"renamed_key": PROC, "staging_key": STAGING, "outgoing_key": OUT},
    )


def _exists(s3, key):
    return s3.list_objects_v2(Bucket=B, Prefix=key).get("KeyCount", 0) > 0


@covers("DLP-2")
@mock_aws
def test_cleanup_deletes_processing_and_staging():
    """critical: with the outgoing object present, processing and staging copies are deleted, outgoing kept."""
    rt = make_runtime(SETTINGS)
    s3 = _setup(rt, True)
    ctx = lane.dlp_2_clean_up(_ctx(), rt)
    assert ctx.status == "ok"
    assert not _exists(s3, PROC)
    assert not _exists(s3, STAGING)
    assert _exists(s3, OUT)


@covers("DLP-2", "R-DLP-2-a")
@mock_aws
def test_cleanup_keeps_copies_without_outgoing():
    """important: when the outgoing object is missing nothing is deleted."""
    rt = make_runtime(SETTINGS)
    s3 = _setup(rt, False)
    lane.dlp_2_clean_up(_ctx(), rt)
    assert _exists(s3, PROC)
    assert _exists(s3, STAGING)
