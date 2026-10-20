"""End to end on S3 (a moto server), in the shape of the user's pipeline: a source that lists incoming/ (one package per
file), a destination whose custom layout step routes files by two rules or to error/ (recording the rule in the layout
variables), and a custom_delivery step that moves each file inside the same bucket and tags it. Dev mode, one person,
fake GitHub, scripted LLM (tests/fakellm/study_move.json), real git and jobs (inline).

The scripted `move_and_tag` reads ctx["delivery"]["if_exists"] and params["reason:" + path] literally (R13): the e2e
exercises the runtime contract (K4, K13), not a defensive `.get`.

S6 is "Preview this run, then Move" (D1, K6): a custom-delivery pipeline never runs without a preview; the Move delivers
exactly the previewed files with the preview's run variables; started.json is committed before anything moves, so a Move
that dies is kept, archived and published as failed (R7); a record that could not be pushed is published later (D11);
files.csv has one row per planned file with the rule in `detail` and `note` (K7); report.json names the caller (D15).

One module-wide world goes through S1…S5 once; each test seeds the bucket afresh and runs S6 its own way."""
from __future__ import annotations

import csv
import io
import json
import shutil
import socket

import pytest
import yaml

from conftest import use_script
from fakegithub import FakeGitHub, make_repo
from pkf_studio import actions, jobs, teamsetup
from pkf_studio.github import GitHub
from pkf_studio.project import Person, ProjectConfig, Team, write_local
from pkf_studio.service import Studio, unfilled
from pkf_studio.stages import derive
from pkf_studio.ui.components import required_paths, write_values
from pkf_studio.ui.requirements import import_text

moto_server = pytest.importorskip("moto.server")

P = "study_move"
SRC, DEST = "study_incoming", "study_moved"
BUCKET = "study-bucket"
REGION = "eu-north-1"
ACCOUNT = "123456789012"   # moto's account
GATES = ["G1", "G2", "G3", "G4", "G5"]
REQUIREMENT = """Study files moved to their folders (S3)

TRIGGER
- Runs once a day on the schedule @daily. No events. No retries.

SOURCE
- The study S3 bucket of the environment (bucket name set per environment), region eu-north-1.
- List every object under the prefix incoming/, at any depth. Paths are relative to incoming/.
- Skip silently: folder markers (keys ending in "/") and files directly under incoming/ with no study folder.
- Each remaining file is its own package.

TRANSFORMATION
- None: files are never opened or changed; only their location changes.

DESTINATION (the same bucket)
- Rule 1: the folder path contains /adam/ -> {study}/adam/{file}. Rule 2: it contains /sdtm/ -> {study}/sdtm/{file}.
- No rule or both rules -> error/{path}. Never drop a file. Record the rule for the report.

DELIVERY BEHAVIOUR
- Move, not copy: copy server-side, check the target size, then delete the source. A failed copy keeps the source.
- After a move set exactly one tag ADDITIONAL_TAG = the path; files moved to error/ get no tag.
- Report one line per file with the rule or the error reason.
"""
OBJECTS = {"incoming/S1/raw/adam/a.csv": "a-data", "incoming/S1/raw/sdtm/b.csv": "bb-data", "incoming/S2/other/c.csv": "ccc",
           "incoming/S3/adam/sdtm/d.csv": "dddd", "incoming/S1/": "", "incoming/readme.txt": "skip me"}
MOVED = {"S1/adam/a.csv": ("S1/raw/adam/a.csv", "rule 1"), "S1/sdtm/b.csv": ("S1/raw/sdtm/b.csv", "rule 2"),
         "error/S2/other/c.csv": ("S2/other/c.csv", "no rule"), "error/S3/adam/sdtm/d.csv": ("S3/adam/sdtm/d.csv", "multiple rules")}


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


class World:
    def __init__(self, fake, clone, root, log):
        self.fake, self.clone, self.root, self.log = fake, clone, root, log
        self.s = None

    def s3(self):
        import boto3

        return boto3.client("s3", region_name=REGION)

    def seed(self, objects=OBJECTS):
        c = self.s3()
        for page in c.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
            for o in page.get("Contents", []):
                c.delete_object(Bucket=BUCKET, Key=o["Key"])
        for k, v in objects.items():
            c.put_object(Bucket=BUCKET, Key=k, Body=v.encode())

    def keys(self) -> list[str]:
        c = self.s3()
        return sorted(o["Key"] for page in c.get_paginator("list_objects_v2").paginate(Bucket=BUCKET) for o in page.get("Contents", []))

    def tags(self, key) -> dict:
        return {t["Key"]: t["Value"] for t in self.s3().get_object_tagging(Bucket=BUCKET, Key=key)["TagSet"]}

    def job(self, action, args=None, ok=True):
        jid = jobs.start(self.s.root, P, action, args or {}, action)
        st = jobs.status(self.s.root, jid)
        if ok:
            assert st["state"] == "succeeded", jobs.log(self.s.root, jid)[-6000:]
        return st, jobs.log(self.s.root, jid)

    def run_state(self):
        self.s.invalidate()
        self.s.fetch(force=True)
        return derive(self.s, P, self.s.prs(P, 0))["by_gate"]["RUN"]


def approve_and_merge(fake, s, gate):
    pr = s.request_approval(P, gate)
    s.post_approve(P, gate)
    res, _ = s.gate_status(P, gate)
    assert res.passed, res.summary()
    s.merge(P, gate)
    assert fake.pulls[pr["number"]]["merged"]


def _clean(st, log=""):
    assert st["result"]["problems"] == [] and st["result"]["severity"] is None, (st["result"], log[-4000:])


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("pkf-studio-e2e-s3")
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=_free_port(), verbose=False)
    server.start()
    try:
        host, port = server.get_host_and_port()
        bare, clone = make_repo(root)
        fake = FakeGitHub(bare)
        fake.add_user("dev1", "dev@example.com", token="tok-dev1")
        for k, v in {"GITHUB_TOKEN": "tok-dev1", "PKF_STUDIO_INLINE_JOBS": "1", "PKF_PLUGIN_ROOT": str(teamsetup_root() / "steps-plugin"),
                     "PKF_STAGING_URL": (root / "staging").as_uri(), "AWS_EC2_METADATA_DISABLED": "true",
                     "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": REGION,
                     "AWS_ENDPOINT_URL": f"http://{host}:{port}"}.items():
            mp.setenv(k, v)
        for k in ("CLAUDECODE", "AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_DEFAULT_PROFILE"):
            mp.delenv(k, raising=False)
        mp.setattr(teamsetup, "build_wheel", lambda dest, log=print: None)
        log = use_script(mp, root, P)
        w = World(fake, clone, root, log)
        w.s3().create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        w.seed()
        _through_g5(w)
        yield w
        fake.close()
    finally:
        server.stop()
        mp.undo()


def teamsetup_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def _through_g5(w: World) -> None:
    fake, clone = w.fake, w.clone
    s = Studio.open(clone, github=GitHub(fake.api, "acme", "specs", "tok-dev1"))
    me = Person(name="Dev One", email="dev@example.com", roles=["product_owner", "business_analyst", "architect", "data_engineer"],
                github="dev1", github_id=fake.users["dev1"]["id"])
    teamsetup.open_pr(s, ProjectConfig(mode="dev", github_api=fake.api, aws_region=REGION, aws_account_id=ACCOUNT),
                      Team(people=[me]), ["@dev1"])
    fake.merge(1)
    s = w.s = Studio.open(clone)
    write_local(s.root, {"email": "dev@example.com"})
    import_text(s, P, "study-move.md", REQUIREMENT.encode(), REQUIREMENT, [])
    _clean(*w.job("analyze"))
    wt = s.worktree_path(P)
    approve_and_merge(fake, s, "G1")
    _clean(*w.job("start_g2"))
    approve_and_merge(fake, s, "G2")
    st, _ = w.job("start_g3")
    for line in st["result"]["unfilled"]:
        rel = line.split(":", 1)[0]
        doc = yaml.safe_load((wt / rel).read_text())
        write_values(wt / rel, {keys: BUCKET if "bucket" in ".".join(map(str, keys)) else "x" for keys, _ in required_paths(doc)})
    assert not unfilled(wt, P)
    st, log = w.job("plan_components")
    _clean(st, log)
    lock = yaml.safe_load((wt / "destinations" / f"{DEST}.lock.yaml").read_text())
    assert [(x["id"], x["op"]) for x in lock["plan"]["steps"]] == [("route_files", "custom"), ("move_and_tag", "custom_delivery")]
    approve_and_merge(fake, s, "G3")
    st, log = w.job("start_g4")
    _clean(st, log)
    tests = json.loads((wt / "review" / P / "tests.json").read_text())
    assert tests["build_ok"] and not tests["dags_differ"], tests
    approve_and_merge(fake, s, "G4")
    st, log = w.job("start_g5")      # real build, identity preflight, dry run against moto
    _clean(st, log)
    dry = json.loads((wt / "review" / P / "dry-run.json").read_text())
    assert dry["ok"] and dry["caller_arn"].startswith(f"arn:aws:") and ACCOUNT in dry["caller_arn"], dry
    assert dry["source_versioning"] == "Off", dry
    assert "AWS credentials: arn:aws:" in log
    assert (wt / "dags" / f"pkf_{P}.py").exists()
    approve_and_merge(fake, s, "G5")


def _preview(w: World, **args):
    st, log = w.job("preview_run", args)
    res = st["result"]
    assert res["ok"], (res, log[-4000:])
    return res, log


def _move(w: World, preview: dict, ok=True):
    st, log = w.job("run_local", {"own_credentials": True, "planned": preview["planned"]}, ok=ok)
    return st["result"], log


def _record(w: World, res: dict) -> dict:
    return json.loads(w.s.read_base(res["report"]))


def _files_csv(w: World, res: dict) -> list[dict]:
    return list(csv.DictReader(io.StringIO(w.s.read_base(res["files_csv"]))))


# ---------------------------------------------------------------------------------------------------------------- tests
def test_custom_delivery_run_needs_a_preview(world):
    """D1: a pipeline that moves files never runs in one click."""
    before = actions.run_branches(world.s, P)
    st, log = world.job("run_local", {"own_credentials": True, "params": {}, "date": None}, ok=False)
    assert st["state"] == "failed" and st["result"]["error"] == actions.NEEDS_PREVIEW, (st["result"], log[-3000:])
    assert actions.run_branches(world.s, P) == before, "no run branch is made"
    assert world.keys() == sorted(OBJECTS), "nothing moved"


def test_s1_to_s6_move_and_tag(world):
    world.seed()
    res, log = _preview(world, max_packages=10)
    assert res["files"] == 4 and res["packages"] == 4 and res["one_file_each"] and res["max_packages"] == 10, res
    assert res["deletes"] == 4 and res["error_files"] == 2 and res["by_prefix"] == {"S1/": 2, "error/": 2}, res
    assert res["source_bucket"] == BUCKET and res["delivery"] == "custom_delivery"
    assert world.keys() == sorted(OBJECTS), "the preview moves nothing"
    branch = res["branch"]
    assert not world.s.git.sha(f"origin/{branch}"), "the preview never pushes"
    move, mlog = _move(world, res)
    assert move["run_ok"] and move["merged"] and move["severity"] is None and move["problems"] == [], (move, mlog[-5000:])
    left = world.keys()
    assert left == sorted(["incoming/S1/", "incoming/readme.txt", *MOVED]), left
    assert world.tags("S1/adam/a.csv") == {"ADDITIONAL_TAG": "S1/raw/adam/a.csv"}
    assert world.tags("S1/sdtm/b.csv") == {"ADDITIONAL_TAG": "S1/raw/sdtm/b.csv"}
    assert world.tags("error/S2/other/c.csv") == {} and world.tags("error/S3/adam/sdtm/d.csv") == {}
    rows = _files_csv(world, move)
    assert sorted(r["target_key"] for r in rows) == sorted(MOVED) and len(rows) == res["files"]
    for r in rows:
        path, rule = MOVED[r["target_key"]]
        assert r["path"] == path and r["source_key"] == "incoming/" + path and r["detail"] == rule and r["note"] == rule, r
        assert r["status"] == ("moved_to_error" if r["target_key"].startswith("error/") else "moved"), r
    report = _record(world, move)
    assert report["ok"] and report["rows"] == 4 and report["files_csv"] == "files.csv" and report["log"] == "run.log", report
    assert report["planned_files"] == 4 and report["planned_sha256"] == res["planned_sha256"]
    assert world.s.read_base(f"review/{P}/runs/{res['run']}/run.log"), "run.log is in the record"
    started = json.loads(world.s.read_base(f"review/{P}/runs/{res['run']}/started.json"))
    assert started["planned_files"] == 4 and started["max_packages"] == 10 and started["run_id"] == res["run_id"]
    assert world.s.read_base(f".pkf-studio/cache/runs/{P}/{res['run']}/planned.json") is None, "planned.json stays out of git"
    assert world.run_state().state == "done"
    st = derive(world.s, P, world.s.prs(P, 0))
    assert [g.state for g in st["gates"]][:5] == ["approved"] * 5
    # the coder saw the runtime contract (R13, K16, A12)
    calls = [json.loads(x) for x in world.log.read_text(encoding="utf-8").splitlines()]
    (prompt,) = [c["prompt"] for c in calls if c["task"] == "write_custom_step" and "Write the custom step `move_and_tag`" in c["prompt"]]
    assert "params added by the earlier steps" in prompt and "reason:" in prompt, prompt[-3000:]
    assert "ctx['delivery'] is always present" in prompt


def test_preview_then_move(world):
    """K6: a file added after the preview stays where it is; a planned file that changed is excluded (not moved)."""
    world.seed()
    res, _ = _preview(world)
    c = world.s3()
    c.put_object(Bucket=BUCKET, Key="incoming/S4/adam/new.csv", Body=b"new")              # not in the preview
    c.put_object(Bucket=BUCKET, Key="incoming/S1/raw/sdtm/b.csv", Body=b"changed-data")   # its ETag changed
    move, log = _move(world, res)
    assert move["run_ok"], (move, log[-4000:])
    left = world.keys()
    assert "incoming/S4/adam/new.csv" in left and "S4/adam/new.csv" not in left
    assert "incoming/S1/raw/sdtm/b.csv" in left and "S1/sdtm/b.csv" not in left
    assert "S1/adam/a.csv" in left and "error/S2/other/c.csv" in left
    summary = json.dumps(_record(world, move)["data"])
    assert "excluded_since_preview" in summary, summary


def test_failed_run_is_recorded_as_failed(world):
    """A key the runtime refuses (`..`) fails only that file (B4); the run fails → severity error, RUN failed (D10)."""
    world.seed({**OBJECTS, "incoming/S1/../x.csv": "bad"})
    res, _ = _preview(world)
    move, log = _move(world, res)
    assert move["run_ok"] is False and move["severity"] == "error" and move["merged"], (move, log[-4000:])
    assert any("files.csv" in p for p in move["problems"]), move["problems"]
    assert "S1/adam/a.csv" in world.keys() and "incoming/S1/../x.csv" in world.keys()
    report = _record(world, move)
    assert report["ok"] is False and "x.csv" in json.dumps(report["issues"])
    rs = world.run_state()
    assert rs.state == "failed" and "x.csv" in rs.note, (rs.state, rs.note)


def test_record_push_failure_then_publish(world, monkeypatch):
    """D11: the files moved but the record could not be pushed: the job says so; Publish run record publishes it."""
    world.seed()
    res, _ = _preview(world)
    calls = []
    real = Studio.push

    def flaky(self, p):
        calls.append(p)
        if len(calls) == 1:
            raise RuntimeError("proxy said no")
        return real(self, p)

    monkeypatch.setattr(Studio, "push", flaky)
    move, log = _move(world, res)
    assert move["run_ok"] and not move["merged"] and move["severity"] == "warning", move
    assert move["problems"] == ["the run finished but its record could not be published: proxy said no — Workflow → Publish run record"]
    assert actions.unpublished_runs(world.s, P) == [res["branch"]]
    st, log = world.job("publish_run_record")
    out = st["result"]
    assert [x["branch"] for x in out["published"]] == [res["branch"]] and out["published"][0]["merged"], (out, log[-3000:])
    assert actions.unpublished_runs(world.s, P) == []
    assert world.run_state().state == "done"


def test_crashed_move_is_kept_and_publishable(world, monkeypatch):
    """R7: a Move that dies after started.json is never deleted by the next Preview; it is archived and published as
    failed; then a new Preview and Move work."""
    from pkf_studio.runner import Ctx

    world.seed()
    res, _ = _preview(world)
    real = Ctx.pkf

    def dies(self, *args, **kw):
        if args[:1] == ("run",) and "--only" in args:
            (kw["cwd"] / f"review/{P}/runs/{res['run']}").mkdir(parents=True, exist_ok=True)
            (kw["cwd"] / f"review/{P}/runs/{res['run']}/run.log").write_text("moved 1 of 4\n", encoding="utf-8")
            raise RuntimeError("the laptop went to sleep")
        return real(self, *args, **kw)

    monkeypatch.setattr(Ctx, "pkf", dies)
    st, _ = _move(world, res, ok=False)
    assert st["ok"] is False
    monkeypatch.setattr(Ctx, "pkf", real)
    crashed = res["branch"]
    assert world.s.read_at(crashed, f"review/{P}/runs/{res['run']}/started.json"), "started.json was committed before the run"
    res2, log = _preview(world)                          # the next Preview keeps it
    assert any("did not finish" in p for p in res2["problems"]), res2["problems"]
    assert world.s.git.sha(f"refs/heads/{crashed}"), "the crashed move's branch is kept"
    archived = world.s.git.run("for-each-ref", "--format=%(refname)", f"refs/pkf-archive/{P}/").split()
    assert any(r.startswith(f"refs/pkf-archive/{P}/{res['run']}-") for r in archived), archived
    assert crashed in actions.unpublished_runs(world.s, P)
    st, log = world.job("publish_run_record")
    pub = {x["branch"]: x for x in st["result"]["published"]}
    assert pub[crashed]["run_ok"] is False and pub[crashed]["merged"], (st["result"], log[-3000:])
    report = json.loads(world.s.read_base(f"review/{P}/runs/{res['run']}/report.json"))
    assert report["issues"][0]["code"] == "run_crashed" and report["log"] == "run.log", report
    assert world.s.read_base(f"review/{P}/runs/{res['run']}/run.log") == "moved 1 of 4\n"
    rs = world.run_state()
    assert rs.state == "failed" and "did not finish" in rs.note, (rs.state, rs.note)
    res3, _ = _preview(world)                            # (the publish moved the worktree: preview again)
    move, log = _move(world, res3)
    assert move["run_ok"] and move["merged"], (move, log[-4000:])
    assert world.run_state().state == "done"


def test_caller_arn_recorded(world):
    """D15: the identity that ran the move is in started.json and report.json."""
    world.seed()
    res, log = _preview(world)
    assert "AWS credentials: arn:aws:" in log
    move, _ = _move(world, res)
    report = _record(world, move)
    started = json.loads(world.s.read_base(f"review/{P}/runs/{res['run']}/started.json"))
    assert report["caller_arn"] and ACCOUNT in report["caller_arn"] and started["caller_arn"] == report["caller_arn"], report
    preview = json.loads((world.s.root / ".pkf-studio" / "cache" / "runs" / P / res["run"] / "preview.json").read_text())
    assert preview["caller_arn"] == report["caller_arn"]
    shutil.rmtree(world.s.root / ".pkf-studio" / "cache" / "runs" / P, ignore_errors=True)
