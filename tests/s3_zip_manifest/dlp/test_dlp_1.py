import hashlib
import json
import pytest
from moto import mock_aws
from pkf_runtime.kit import PackageContext, DataError
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
STAGING = f"staging_test/study_a_{TS}.zip"
OUT = f"outgoing/{TS}_study_a.zip"
CONTENT = b"zip-content-bytes"
SHA = hashlib.sha256(CONTENT).hexdigest()
SRC_SHA = "a" * 64
KH = "0123456789ab"
IDX = f"index/s3_zip_manifest/study_a/{SRC_SHA}_{KH}.json"


def _setup(rt, staging_meta=True):
    s3 = rt.s3()
    s3.create_bucket(Bucket=B, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    meta = {"pkf-source-key": "incoming/study_a.zip", "pkf-source-sha256": SRC_SHA, "pkf-sha256": SHA}
    s3.put_object(Bucket=B, Key=STAGING, Body=CONTENT, Metadata=meta if staging_meta else {})
    return s3


def _ctx():
    return PackageContext(
        package_id="p1", name="study_a.zip",
        source={"bucket": B, "key": "incoming/study_a.zip"},
        vars={"package_ts": TS, "stem": "study_a", "source_key": "incoming/study_a.zip",
              "source_sha256": SRC_SHA, "source_key_hash": KH},
        artefacts={"renamed_key": f"processing/study_a_{TS}.zip", "staging_key": STAGING},
    )


@covers("DLP-1", "R-DLP-1-a", "R-DLP-1-c")
@mock_aws
def test_delivers_copy_and_index():
    """critical: staging zip is copied to outgoing with same content and the index JSON is written."""
    rt = make_runtime(SETTINGS)
    s3 = _setup(rt)
    ctx = lane.dlp_1_deliver_to_outgoing(_ctx(), rt)
    assert ctx.artefacts["outgoing_key"] == OUT
    assert ctx.artefacts["index_key"] == IDX
    assert s3.get_object(Bucket=B, Key=OUT)["Body"].read() == CONTENT
    idx = json.loads(s3.get_object(Bucket=B, Key=IDX)["Body"].read())
    assert idx["source_key"] == "incoming/study_a.zip"
    assert idx["source_sha256"] == SRC_SHA
    assert idx["outgoing_key"] == OUT
    assert idx["package_ts"] == TS
    assert idx["outgoing_sha256"] == SHA


@covers("DLP-1", "R-DLP-1-a")
@mock_aws
def test_existing_different_outgoing_not_overwritten():
    """important: a different existing outgoing object raises DataError and is left untouched, no index."""
    rt = make_runtime(SETTINGS)
    s3 = _setup(rt)
    s3.put_object(Bucket=B, Key=OUT, Body=b"other")
    with pytest.raises(DataError):
        lane.dlp_1_deliver_to_outgoing(_ctx(), rt)
    assert s3.get_object(Bucket=B, Key=OUT)["Body"].read() == b"other"
    listed = s3.list_objects_v2(Bucket=B, Prefix="index/").get("KeyCount", 0)
    assert listed == 0


@covers("DLP-1", "R-DLP-1-b")
@mock_aws
def test_metadata_mismatch_raises():
    """important: outgoing without matching pkf-source metadata raises DataError and writes no index."""
    rt = make_runtime(SETTINGS)
    s3 = _setup(rt, staging_meta=False)
    with pytest.raises(DataError):
        lane.dlp_1_deliver_to_outgoing(_ctx(), rt)
    assert s3.list_objects_v2(Bucket=B, Prefix="index/").get("KeyCount", 0) == 0
