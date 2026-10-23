import pytest
from moto import mock_aws
from pkf_runtime.kit import PackageContext
from pkf_runtime.kit.testing import covers, make_runtime, zip_bytes
from pkf_pipelines.s3_zip_manifest import trn as lane

BUCKET = "dev-dpl-dstore-sas-eu-north-1-992382551516"
SETTINGS = {
    "region": "eu-north-1", "source_bucket": BUCKET, "target_bucket": BUCKET,
    "incoming_prefix": "incoming/", "processing_prefix": "processing/",
    "staging_prefix": "staging_test/", "outgoing_prefix": "outgoing/",
    "index_prefix": "index/s3_zip_manifest/",
}
STAGING = "staging_test/study_a_20261022T100000Z.zip"
SHA = "b" * 64


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")


def _ctx(**extra):
    vars_ = {"package_ts": "20261022T100000Z", "stem": "study_a",
             "source_sha256": SHA, "source_key": "incoming/study_a.zip"}
    vars_.update(extra)
    return PackageContext(package_id="x", name="study_a.zip",
                          source={"bucket": BUCKET, "key": "incoming/study_a.zip"},
                          vars=vars_, artefacts={})


@mock_aws
@covers("TRN-2", "R-TRN-2-a", "R-TRN-2-b")
def test_upload_to_staging_with_metadata(tmp_path):
    """critical: the rebuilt zip lands in staging with source metadata and staging_key is set."""
    rt = make_runtime(SETTINGS)
    s3 = rt.s3()
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    data = zip_bytes({"a.pdf": "x", "manifest.csv": "file_name,size_bytes\na.pdf,1\n"})
    p = tmp_path / "out.zip"
    p.write_bytes(data)
    ctx = _ctx()
    ctx.artefacts["new_zip_path"] = str(p)
    out = lane.trn_2_upload_to_staging(ctx, rt)
    assert out.artefacts["staging_key"] == STAGING
    obj = s3.get_object(Bucket=BUCKET, Key=STAGING)
    assert obj["Body"].read() == data
    assert obj["Metadata"]["pkf-source-key"] == "incoming/study_a.zip"
    assert obj["Metadata"]["pkf-source-sha256"] == SHA


@mock_aws
@covers("TRN-2")
def test_staged_does_nothing():
    """important: when TRN-1 found the staging zip done, nothing is uploaded and the key is kept."""
    rt = make_runtime(SETTINGS)
    s3 = rt.s3()
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    ctx = _ctx(staged=True)
    ctx.artefacts["staging_key"] = STAGING
    out = lane.trn_2_upload_to_staging(ctx, rt)
    assert out.artefacts["staging_key"] == STAGING
    assert out.status == "ok"
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET, Prefix="staging_test/")
