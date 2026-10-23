import hashlib
import io
import json
import zipfile
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
SRC_SHA = "b" * 64
KH = "abcdef012345"


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def _prepare(stem, files, outgoing_existing=None):
    rt = make_runtime(SETTINGS)
    s3 = rt.s3()
    s3.create_bucket(Bucket=B, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
    data = _zip(files)
    proc = f"processing/{stem}_{TS}.zip"
    stg = f"staging_test/{stem}_{TS}.zip"
    s3.put_object(Bucket=B, Key=proc, Body=b"orig")
    s3.put_object(Bucket=B, Key=stg, Body=data, Metadata={
        "pkf-source-key": f"incoming/{stem}.zip", "pkf-source-sha256": SRC_SHA,
        "pkf-sha256": hashlib.sha256(data).hexdigest()})
    if outgoing_existing is not None:
        s3.put_object(Bucket=B, Key=f"outgoing/{TS}_{stem}.zip", Body=outgoing_existing)
    ctx = PackageContext(
        package_id=stem, name=f"{stem}.zip",
        source={"bucket": B, "key": f"incoming/{stem}.zip"},
        vars={"package_ts": TS, "stem": stem, "source_key": f"incoming/{stem}.zip",
              "source_sha256": SRC_SHA, "source_key_hash": KH, "manifest_rows": len(files)},
        artefacts={"renamed_key": proc, "staging_key": stg},
    )
    return rt, s3, ctx, proc, stg


def _exists(s3, key):
    return s3.list_objects_v2(Bucket=B, Prefix=key).get("KeyCount", 0) > 0


@covers("DLP-1", "DLP-2")
@mock_aws
def test_happy_path_study_a():
    """functional: happy_path_study_a delivers outgoing zip and index, and removes processing and staging."""
    files = {"a.pdf": "0123456789", "b/c.pdf": "01234567890123456789", "manifest.csv": "file_name,size_bytes\n"}
    rt, s3, ctx, proc, stg = _prepare("study_a", files)
    ctx = lane.dlp_1_deliver_to_outgoing(ctx, rt)
    out = f"outgoing/{TS}_study_a.zip"
    idx = f"index/s3_zip_manifest/study_a/{SRC_SHA}_{KH}.json"
    assert ctx.artefacts["outgoing_key"] == out
    assert ctx.artefacts["index_key"] == idx
    assert _exists(s3, proc) and _exists(s3, stg) and _exists(s3, idx)
    ctx = lane.dlp_2_clean_up(ctx, rt)
    assert ctx.status == "ok"
    assert ctx.artefacts["outgoing_key"] == out
    assert not _exists(s3, proc)
    assert not _exists(s3, stg)
    body = s3.get_object(Bucket=B, Key=out)["Body"].read()
    names = zipfile.ZipFile(io.BytesIO(body)).namelist()
    assert sorted(names) == ["a.pdf", "b/c.pdf", "manifest.csv"]
    assert json.loads(s3.get_object(Bucket=B, Key=idx)["Body"].read())["outgoing_key"] == out


@covers("DLP-1", "R-DLP-1-a")
@mock_aws
def test_outgoing_collision_not_overwritten():
    """functional: outgoing_collision_not_overwritten fails the package and keeps staging and processing."""
    rt, s3, ctx, proc, stg = _prepare("study_d", {"a.pdf": "x"}, outgoing_existing=b"other")
    with pytest.raises(DataError):
        lane.dlp_1_deliver_to_outgoing(ctx, rt)
    out = f"outgoing/{TS}_study_d.zip"
    assert s3.get_object(Bucket=B, Key=out)["Body"].read() == b"other"
    assert _exists(s3, stg) and _exists(s3, proc)
