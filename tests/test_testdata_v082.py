"""0.8.2: the 6.3 test package — built from the pipeline's own example, placed only in the pipeline's dev source
bucket, never overwriting, tagged; the commands when it cannot be placed."""
from __future__ import annotations

import json
import zipfile

import pytest
import yaml

from pkf_studio import testdata as TD

P = "p1"


def _reader(files: dict):
    def read(rel):
        doc = files.get(rel)
        return None if doc is None else (doc if isinstance(doc, str) else yaml.safe_dump(doc, sort_keys=False))
    return read


def _files(listing=None, examples=None, bucket="dev-src-bucket", root="incoming", src_type="s3", min_age=30):
    ex = examples if examples is not None else [
        {"name": "bad", "listing": ["incoming/x"], "expect_error": True},
        {"name": "typical", "listing": listing or ["incoming/D1/a.sas7bdat:120", "incoming/D1/sub/b.zip", "incoming/D1/",
                                                   "incoming/D1/control.txt"],
         "contents": {"incoming/D1/control.txt": "rows=2\n"}, "expected_keys": ["out/D1/a.sas7bdat"]}]
    return {
        f"pipelines/{P}.yaml": {"name": P, "connection": P, "transformation": None, "examples": ex, "params": {}},
        f"connections/{P}.yaml": {"name": P, "source": "s_in", "destination": "d_out"},
        "sources/s_in.yaml": {"type": src_type, "connection": {"bucket": bucket, "region": "eu-north-1"},
                              "extraction": {"root": "{input_path}", "min_age_seconds": min_age}, "params": {"input_path": root}},
        "destinations/d_out.yaml": {"type": "s3", "connection": {"bucket": "dest"}},
    }


def test_plan_takes_the_first_example_that_should_deliver():
    pl = TD.plan(_reader(_files()), P)
    assert pl["ok"], pl["problem"]
    assert pl["example"] == "typical" and pl["bucket"] == "dev-src-bucket" and pl["region"] == "eu-north-1"
    assert pl["root"] == "incoming" and pl["min_age_s"] == 30
    keys = [o["key"] for o in pl["objects"]]
    assert keys == ["incoming/D1/a.sas7bdat", "incoming/D1/sub/b.zip", "incoming/D1/control.txt"]   # the folder marker is skipped
    assert pl["objects"][0]["size"] == 120 and pl["objects"][2]["text"] == "rows=2\n"


@pytest.mark.parametrize("change,problem", [
    ({"mode": "sit"}, "only in dev"),
    ({"bucket": "<REQUIRED>"}, "no concrete bucket"),
    ({"src_type": "sftp"}, "only an S3 source"),
    ({"examples": []}, "no end-to-end example"),
    ({"listing": ["other/D1/a.csv"]}, "not under the source root"),
    ({"listing": ["incoming/../etc/x"]}, "not a plain key"),
])
def test_plan_refuses(change, problem):
    mode = change.pop("mode", "dev")
    pl = TD.plan(_reader(_files(**change)), P, mode)
    assert not pl["ok"] and problem in pl["problem"]


def test_bodies_text_zip_and_placeholder():
    assert TD.body({"key": "a/c.txt", "text": "hi"}) == b"hi"
    z = TD.body({"key": "a/b.zip", "size": 10})
    assert zipfile.ZipFile(__import__("io").BytesIO(z)).namelist() == ["pkf-test-data.txt"]
    ph = TD.body({"key": "a/x.sas7bdat", "size": 200})
    assert len(ph) == 200 and ph.startswith(b"pkf test package")


def test_write_local_and_commands(tmp_path):
    pl = TD.plan(_reader(_files()), P)
    folder = TD.write_local(tmp_path, P, pl, stamp="20261020T120000")
    assert (folder / "files/incoming/D1/control.txt").read_text() == "rows=2\n"
    sh = (folder / "upload.sh").read_text()
    assert "aws s3api head-object" in sh and "aws s3api put-object --bucket dev-src-bucket --key incoming/D1/a.sas7bdat" in sh
    assert "--tagging pkf-test-data=1" in sh and "--region eu-north-1" in sh
    man = json.loads((folder / "manifest.json").read_text())
    assert man["example"] == "typical" and len(man["objects"]) == 3
    assert TD.latest(tmp_path, P)["stamp"] == "20261020T120000"


def test_upload_never_overwrites_and_tags(tmp_path, monkeypatch):
    import boto3
    from moto import mock_aws

    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    pl = TD.plan(_reader(_files()), P)
    folder = TD.write_local(tmp_path, P, pl, stamp="s1")
    with mock_aws():
        c = boto3.client("s3", region_name="eu-north-1")
        c.create_bucket(Bucket="dev-src-bucket", CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        c.put_object(Bucket="dev-src-bucket", Key="incoming/D1/a.sas7bdat", Body=b"REAL DATA")
        res = TD.upload(pl, folder, client=c)
        assert res["existing"] == ["incoming/D1/a.sas7bdat"]
        assert set(res["placed"]) == {"incoming/D1/sub/b.zip", "incoming/D1/control.txt"}
        assert c.get_object(Bucket="dev-src-bucket", Key="incoming/D1/a.sas7bdat")["Body"].read() == b"REAL DATA"
        tags = c.get_object_tagging(Bucket="dev-src-bucket", Key="incoming/D1/control.txt")["TagSet"]
        assert {"Key": "pkf-test-data", "Value": "1"} in tags


def test_upload_failure_is_a_test_data_error(tmp_path, monkeypatch):
    import boto3
    from moto import mock_aws

    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    pl = TD.plan(_reader(_files()), P)
    folder = TD.write_local(tmp_path, P, pl, stamp="s2")
    with mock_aws():
        c = boto3.client("s3", region_name="eu-north-1")              # the bucket does not exist
        with pytest.raises(TD.TestDataError, match="NoSuchBucket"):
            TD.upload(pl, folder, client=c)
