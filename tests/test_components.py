"""Trigger, source and destination catalogues, explicit steps, and the decomposer (replayed) — no network."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pkf_runtime import simulate
from pkf_runtime.discovery import SOURCE_OPS
from pkf_runtime.events import TRIGGER_OPS, messages_to_events, run_event_steps
from pkf_runtime.layout import run_layout_steps
from pkf_runtime.package import PackageError

ROOT = Path(__file__).resolve().parents[1]


def s3_msg(*keys, bucket="drop", name="ObjectCreated:Put", t="2026-10-16T10:00:00.000Z", seq=None):
    return {"Records": [{"eventName": name, "eventTime": t, "s3": {"bucket": {"name": bucket},
                                                                   "object": {"key": k, "size": 1, **({"sequencer": seq} if seq else {})}}}
                        for k in keys]}


def events(msgs, steps):
    m = [{"id": f"m{i}", "body": x if isinstance(x, str) else json.dumps(x)} for i, x in enumerate(msgs)]
    return run_event_steps(messages_to_events(m), steps, {"params": {"bucket": "drop"}})


S3 = [{"id": "p", "op": "parse_json", "params": {}}, {"id": "u", "op": "unwrap_sns", "params": {}}, {"id": "r", "op": "s3_records", "params": {}}]


def test_s3_records_decode_test_event_sns_and_errors():
    ev = events([s3_msg("in/a+b%2B.zip"), {"Event": "s3:TestEvent"}, {"Type": "Notification", "Message": json.dumps(s3_msg("in/c.zip"))},
                 "garbage", s3_msg("in/x.zip", name="ObjectRemoved:Delete")], S3)
    good = [e for e in ev if not e.get("error")]
    assert [e["objects"][0]["key"] for e in good] == ["in/a b+.zip", "in/c.zip"]
    assert good[0]["vars"]["event_key_stem"] == "a b+" and good[0]["vars"]["event_date"] == "2026-10-16"
    assert [e["error"] for e in ev if e.get("error")] == ["message body is not JSON"]


def test_filter_group_dedupe_set_vars():
    steps = S3 + [{"id": "f", "op": "filter", "params": {"bucket": "{bucket}", "include": ["in/**/*.zip"]}},
                  {"id": "d", "op": "dedupe", "params": {}},
                  {"id": "v", "op": "set_vars", "params": {"vars": {"batch": "{event_key_dir}"}}},
                  {"id": "g", "op": "group", "params": {"by": "{batch}"}}]
    ev = events([s3_msg("in/b1/a.zip", seq="01"), s3_msg("in/b1/a.zip", seq="0A"), s3_msg("in/b1/c.zip"), s3_msg("in/b2/d.zip"),
                 s3_msg("in/b2/e.txt"), s3_msg("in/b9/z.zip", bucket="other")], steps)
    assert [(e["vars"]["group"], sorted(o["key"] for o in e["objects"])) for e in ev] == [
        ("in/b1", ["in/b1/a.zip", "in/b1/c.zip"]), ("in/b2", ["in/b2/d.zip"])]
    assert sorted(ev[0]["message_ids"]) == ["m0", "m1", "m2"]  # the duplicate's message is acknowledged with it
    with pytest.raises(ValueError):
        events([s3_msg("in/a.zip")], S3 + [{"id": "v", "op": "set_vars", "params": {"vars": {"ds": "x"}}}])


def test_source_steps_control_file_split_latest():
    listing = ["d/D001/CONTROL.txt:10", "d/D001/a.csv", "d/D001/b.csv", "d/D001/junk.tmp", "d/D002/CONTROL.txt:5", "d/D002/c.csv",
               "d/old.csv:3:99999", "d/new.csv:3:10"]
    ex = {"listing": listing, "contents": {"d/D001/CONTROL.txt": "a.csv\nb.csv\n", "d/D002/CONTROL.txt": "c.csv\n"}}
    steps = [{"id": "l", "op": "list", "params": {"root": "d"}}, {"id": "s", "op": "split", "params": {"by": "folder", "var": "delivery"}}]
    with pytest.raises(PackageError):  # d/old.csv has no folder to split on
        simulate.source_groups(steps, {}, ex, {})
    steps = [{"id": "l", "op": "list", "params": {"root": "d"}}, {"id": "x", "op": "select", "params": {"include": ["*/**"]}},
             {"id": "s", "op": "split", "params": {"by": "folder", "var": "delivery"}},
             {"id": "c", "op": "control_file", "params": {"match": "*/CONTROL.txt", "keep_control": False}}]
    groups = simulate.source_groups(steps, {}, ex, {})
    assert [(g["vars"]["delivery"], [f["path"] for f in g["files"]]) for g in groups] == [("D001", ["D001/a.csv", "D001/b.csv"]), ("D002", ["D002/c.csv"])]
    latest = simulate.source_groups([{"id": "l", "op": "list", "params": {"root": "d", "recursive": False}},
                                     {"id": "n", "op": "latest", "params": {"n": 1}}], {}, ex, {})
    assert [f["path"] for f in latest[0]["files"]] == ["new.csv"]
    ex["contents"]["d/D001/CONTROL.txt"] = "a.csv\nmissing.csv\n"
    with pytest.raises(PackageError, match="missing"):
        simulate.source_groups(steps, {}, ex, {})


def test_destination_route_and_collisions():
    steps = [{"id": "r", "op": "route", "params": {"rules": [{"include": ["**/*.zip"], "to": "zips/{event_date}/{name}"},
                                                              {"include": ["**/*.tmp"], "to": None}], "unmatched": "leave"}},
             {"id": "p", "op": "prefix", "params": {"prefix": "other/{package}/"}}]
    keys = run_layout_steps(["a/x.zip", "a/y.tmp", "m.json"], {"event_date": "2026-10-16", "package": "pk"}, steps, {"params": {}})
    assert keys == {"a/x.zip": "zips/2026-10-16/x.zip", "a/y.tmp": None, "m.json": "other/pk/m.json"}
    with pytest.raises(PackageError, match="both be delivered"):
        run_layout_steps(["a/x.zip", "b/x.zip"], {"event_date": "d"}, steps[:1], {"params": {}})


def test_catalogues_have_custom_and_receive_first():
    assert "custom" in TRIGGER_OPS and "custom" in SOURCE_OPS
    from pkf.design import validate_steps

    assert validate_steps("trigger", [{"id": "p", "op": "parse_json", "params": {}}])  # no receive step
    assert not validate_steps("trigger", [{"id": "q", "op": "schedule", "params": {}}])


# ---------------------------------------------------------------------------------------------- decomposer (replay)
def test_pipeline_split_replayed(tmp_path):
    from test_flow import pkf

    repo = tmp_path / "specs"
    shutil.copytree(ROOT / "examples", repo, ignore=shutil.ignore_patterns("dags", "__pycache__", ".git", ".llm"))
    doc = pkf(repo, "plan", "pipeline", "partner_zip_copy")
    split = doc["data"]["split"]
    assert set(split["components"]) >= {"trigger", "source", "destination"} and not split["ambiguities"]
    pkf(repo, "approve-plan", "pipeline", "partner_zip_copy", "--by", "@x", agent=True, ok=False)
    out = pkf(repo, "approve-plan", "pipeline", "partner_zip_copy", "--by", "@owner", "--force")
    assert any(w.startswith("triggers/") for w in out["data"]["written"])
    st = pkf(repo, "status", "partner_zip_copy")["data"]["pipelines"][0]
    assert st["stage"] in ("components", "build", "ready")


def test_run_conf_trigger_lambda_style():
    steps = [{"id": "c", "op": "run_conf", "params": {"key": "events"}}] + S3
    ex = {"conf": {"events": [s3_msg("in/a.zip"), s3_msg("in/b.zip")]}}
    ev = simulate.trigger_events(steps, {}, ex, {})
    assert [e["objects"][0]["key"] for e in ev] == ["in/a.zip", "in/b.zip"]
    assert simulate.trigger_events(steps, {}, {"conf": {}}, {}) == []


def test_split_with_errors_or_foreign_files_is_not_applied(tmp_path):
    from test_flow import pkf

    from pkf import decompose, design
    from pkf.repo import load

    repo = tmp_path / "specs"
    shutil.copytree(ROOT / "examples", repo, ignore=shutil.ignore_patterns("dags", "__pycache__", ".git", ".llm"))
    pkf(repo, "plan", "pipeline", "partner_zip_copy")
    r = load(repo)
    lock = decompose.load_lock(r, "partner_zip_copy")
    lock["split"]["components"]["source"]["name"] = "lab_sftp"          # a file another pipeline owns
    lock["split_digest"] = design._sha(lock["split"])
    decompose._save(r, "partner_zip_copy", lock)
    doc = pkf(repo, "approve-plan", "pipeline", "partner_zip_copy", "--by", "@o", "--force", ok=False)
    assert "belongs to another pipeline" in json.dumps(doc["issues"])
    lock["split"]["errors"] = [{"severity": "error", "file": "x", "loc": "", "msg": "bad name", "code": "invalid_split"}]
    lock["split_digest"] = design._sha(lock["split"])
    decompose._save(r, "partner_zip_copy", lock)
    doc = pkf(repo, "approve-plan", "pipeline", "partner_zip_copy", "--by", "@o", "--force", ok=False)
    assert "bad name" in json.dumps(doc["issues"])
