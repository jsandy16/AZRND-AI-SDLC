import hashlib
import zipfile

import boto3
import pytest
from moto import mock_aws
from pkf_runtime.kit import PackageContext, DataError
from pkf_runtime.kit.testing import covers, make_runtime, zip_bytes
from pkf_pipelines.s3_zip_manifest import trn as lane

BUCKET = "dev-dpl-dstore-sas-eu-north-1-992382551516"
SETTINGS = {
    "region": "eu-north-1", "source_bucket": BUCKET, "target_bucket": BUCKET,
    "incoming_prefix": "incoming/", "processing_prefix": "processing/",
    "staging_prefix": "staging_test/", "outgoing_prefix": "outgoing/",
    "index_prefix": "index/s3_zip_manifest/",
}
RENAMED = "processing/study_a_20261022T100000Z.zip"
STAGING = "staging_test/study_a_20261022T100000Z.zip"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")


def _setup(files, sha="a" * 64):
    rt = make_runtime(SETTINGS)
    s3 = rt.s3()
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    s3.put_object(Bucket=BUCKET, Key=RENAMED, Body=zip_bytes(files))
    ctx = PackageContext(
        package_id="x", name="study_a.zip",
        source={"bucket": BUCKET, "key": "incoming/study_a.zip"},
        vars={"package_ts": "20261022T100000Z", "stem": "study_a",
              "source_sha256": sha, "source_key": "incoming/study_a.zip"},
        artefacts={"renamed_key": RENAMED},
    )
    return rt, s3, ctx


@mock_aws
@covers("TRN-1", "R-TRN-1-a")
def test_manifest_built_and_zip_rebuilt():
    """critical: the rebuilt zip holds the original files plus a correct manifest.csv."""
    rt, s3, ctx = _setup({"a.pdf": "0123456789", "b/c.pdf": "01234567890123456789"})
    out = lane.trn_1_build_the_manifest(ctx, rt)
    assert out.status == "ok"
    assert out.vars["manifest_rows"] == 2
    with zipfile.ZipFile(out.artefacts["new_zip_path"]) as z:
        names = z.namelist()
        assert set(names) == {"a.pdf", "b/c.pdf", "manifest.csv"}
        assert z.read("a.pdf") == b"0123456789"
        manifest = z.read("manifest.csv")
    assert manifest == b"file_name,size_bytes\na.pdf,10\nb/c.pdf,20\n"


@mock_aws
@covers("TRN-1", "R-TRN-1-b")
def test_root_manifest_fails():
    """important: a zip that already has a root manifest.csv raises DataError."""
    rt, s3, ctx = _setup({"a.pdf": "x", "manifest.csv": "old"})
    with pytest.raises(DataError):
        lane.trn_1_build_the_manifest(ctx, rt)


@mock_aws
@covers("TRN-1")
def test_existing_staging_same_sha_is_reused():
    """important: an existing staging zip with the same source sha is reused, nothing rebuilt."""
    sha = hashlib.sha256(b"s").hexdigest()
    rt, s3, ctx = _setup({"a.pdf": "x"}, sha=sha)
    s3.put_object(Bucket=BUCKET, Key=STAGING, Body=b"staged",
                  Metadata={"pkf-source-sha256": sha})
    out = lane.trn_1_build_the_manifest(ctx, rt)
    assert out.artefacts["staging_key"] == STAGING
    assert out.vars["staged"] is True
    assert "new_zip_path" not in out.artefacts
