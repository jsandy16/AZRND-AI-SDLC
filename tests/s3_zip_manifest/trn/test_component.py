import io
import zipfile

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


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")


def _start(stem, files):
    rt = make_runtime(SETTINGS)
    s3 = rt.s3()
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    renamed = f"processing/{stem}_20261022T100000Z.zip"
    s3.put_object(Bucket=BUCKET, Key=renamed, Body=zip_bytes(files))
    ctx = PackageContext(
        package_id="x", name=f"{stem}.zip",
        source={"bucket": BUCKET, "key": f"incoming/{stem}.zip"},
        vars={"package_ts": "20261022T100000Z", "stem": stem, "source_sha256": "c" * 64,
              "source_key": f"incoming/{stem}.zip", "source_key_hash": "0" * 12},
        artefacts={"renamed_key": renamed},
    )
    return rt, s3, ctx, renamed


@mock_aws
@covers("TRN-1", "TRN-2")
def test_happy_path_study_a():
    """functional: study_a goes from the processing copy to a staging zip with manifest.csv."""
    rt, s3, ctx, renamed = _start("study_a", {"a.pdf": "0123456789", "b/c.pdf": "01234567890123456789"})
    ctx = lane.trn_1_build_the_manifest(ctx, rt)
    assert ctx.vars["manifest_rows"] == 2
    assert "new_zip_path" in ctx.artefacts
    ctx = lane.trn_2_upload_to_staging(ctx, rt)
    assert ctx.status == "ok"
    key = "staging_test/study_a_20261022T100000Z.zip"
    assert ctx.artefacts["staging_key"] == key
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        assert set(z.namelist()) == {"a.pdf", "b/c.pdf", "manifest.csv"}
        assert z.read("manifest.csv") == b"file_name,size_bytes\na.pdf,10\nb/c.pdf,20\n"
    s3.head_object(Bucket=BUCKET, Key=renamed)


@mock_aws
@covers("TRN-1", "R-TRN-1-b")
def test_zip_with_root_manifest_fails():
    """functional: study_c with a root manifest.csv fails and nothing is staged."""
    rt, s3, ctx, renamed = _start("study_c", {"a.pdf": "x", "manifest.csv": "old"})
    with pytest.raises(DataError):
        lane.trn_1_build_the_manifest(ctx, rt)
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET, Prefix="staging_test/")
    s3.head_object(Bucket=BUCKET, Key=renamed)
