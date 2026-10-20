"""0.8.2: Run without stopping (Full auto, dev only) — each decision of autorun.py, its stops, its caps, the S6 test
package path, the drive lease, and the UI switches."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from pkf_studio import autorun, drive, flow, flowstore, jobs
from test_studio_github import write_g1
from test_studio_ui import P, app, gh, no_errors  # noqa: F401 - fixtures
from test_workspace_ui import _keys, _open, _state


@pytest.fixture()
def spies(monkeypatch):
    calls = {"answer": [], "mark": [], "again": [], "start": []}
    monkeypatch.setattr(flow, "answer", lambda s, p, states, step, values=None: calls["answer"].append((step, values or {})) or {})
    monkeypatch.setattr(flow, "mark_done", lambda s, p, states, step, item=None, done=True: calls["mark"].append((step, item)) or {})
    monkeypatch.setattr(flow, "preview_again", lambda s, p, states, step="6.3": calls["again"].append(step) or {})
    monkeypatch.setattr(jobs, "start", lambda root, p, action, args=None, title="", **kw: calls["start"].append(action) or "20261020T120000-aaaaaa")
    return calls


def _full(s, cursor, status="waiting_user", answers=None, sub=None, row=None):
    st = _state(s.root, cursor, status=status, row=row)
    st["full_auto"] = True
    st["answers"].update(answers or {})
    if sub is not None:
        st["sub"] = sub
    flowstore.save(s.root, P, st)
    return st


def _events(s):
    path = flowstore.flow_dir(s.root, P) / "events.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _auto_events(s):
    return [e for e in _events(s) if "full_auto" in json.dumps(e)]


# ------------------------------------------------------------------------------------------------ decisions
def test_off_unless_full_auto_and_dev(gh, spies, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    st = _state(s.root, "2.2")
    assert autorun.tick(s, P, {}) is None and not spies["answer"]
    st["full_auto"] = True
    flowstore.save(s.root, P, st)
    monkeypatch.setattr(type(s.cfg), "mode", property(lambda self: "sit"), raising=False)
    assert not autorun.enabled(s, flow.load(s.root, P))


@pytest.mark.parametrize("cursor", ["2.2", "3.4", "4.2"])
def test_reviews_are_continued_and_logged(gh, spies, cursor):
    fake, clone, s = gh
    write_g1(s, P)
    _full(s, cursor)
    assert autorun.tick(s, P, {}) == "Continue"
    assert spies["answer"] == [(cursor, {"detail": "continued by Full auto"})]
    st = flow.load(s.root, P)
    assert st["auto"]["count"] == 1 and "Continue" in st["auto"]["log"][-1]
    assert _auto_events(s)


def test_1_4_stops_on_open_questions_once(gh, spies):
    fake, clone, s = gh
    wt = write_g1(s, P)
    (wt / f"requirements/{P}").mkdir(parents=True, exist_ok=True)
    (wt / f"requirements/{P}/analysis.json").write_text(json.dumps({"open_questions": ["which bucket?"]}))
    _full(s, "1.4")
    assert autorun.tick(s, P, {}).startswith("stopped")
    assert not spies["answer"]
    n = len(_auto_events(s))
    assert autorun.tick(s, P, {}) == ""                                # the same stop is not written twice
    assert len(_auto_events(s)) == n
    st = flow.load(s.root, P)
    assert "open question" in st["auto"]["needs_you"] and st["message"].startswith("Full auto stopped at 1.4")


def test_4_3_regenerates_each_component_then_builds_then_stops(gh, spies, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    _full(s, "4.3")
    monkeypatch.setattr(flow, "components_needing_work", lambda s_, p_: [{"kind": "transformation", "name": "t1"},
                                                                         {"kind": "build", "name": "draft build"}])
    for _ in range(autorun.REGEN_TRIES):
        autorun.tick(s, P, {})
    assert [a[1]["job"] for a in spies["answer"]] == ["code_component"] * autorun.REGEN_TRIES
    for _ in range(autorun.TEST_BUILD_TRIES):
        autorun.tick(s, P, {})
    assert [a[1]["job"] for a in spies["answer"]][-autorun.TEST_BUILD_TRIES:] == ["test_build"] * autorun.TEST_BUILD_TRIES
    assert autorun.tick(s, P, {}).startswith("stopped")


def test_3_2_and_7_3_stop(gh, spies):
    fake, clone, s = gh
    write_g1(s, P)
    _full(s, "3.2", status="waiting_input")
    assert autorun.tick(s, P, {}).startswith("stopped") and "REQUIRED" in flow.load(s.root, P)["auto"]["needs_you"]
    _full(s, "7.3")
    assert autorun.tick(s, P, {}).startswith("stopped") and "by hand" in flow.load(s.root, P)["auto"]["needs_you"]
    assert not spies["answer"]


def test_6_1_and_6_2_use_the_intake_pre_answers(gh, spies):
    fake, clone, s = gh
    write_g1(s, P)
    _full(s, "6.1", status="waiting_input", answers={"own_credentials": True})
    autorun.tick(s, P, {})
    assert spies["answer"][-1] == ("6.1", {"own_credentials": True})
    _full(s, "6.1", status="waiting_input", answers={"own_credentials": False})
    assert autorun.tick(s, P, {}).startswith("stopped")
    _full(s, "6.2", answers={"no_old_dag": True})
    autorun.tick(s, P, {})
    assert spies["mark"] == [("6.2", flow.BY_ID["6.2"].items[0])]
    _full(s, "6.2", answers={"no_old_dag": False})
    assert autorun.tick(s, P, {}).startswith("stopped")


def _preview(s, tmp, files, bucket="dev-src"):
    planned = tmp / "planned.json"
    planned.write_text(json.dumps({"files": [{"source_key": k, "target_key": "t/" + k} for k in files]}))
    return {"preview": {"files": len(files), "planned": str(planned), "source_bucket": bucket, "deletes": len(files)}}


def test_6_3_moves_only_its_own_test_package(gh, spies, tmp_path):
    fake, clone, s = gh
    write_g1(s, P)
    st = _full(s, "6.3", sub=_preview(s, tmp_path, ["incoming/D1/a.csv"]))
    assert autorun.tick(s, P, {}).startswith("stopped")                # not its own: a person checks and moves
    assert not spies["answer"]
    st = flow.load(s.root, P)
    st["auto"]["staged"] = {"bucket": "dev-src", "keys": ["incoming/D1/a.csv"]}
    flowstore.save(s.root, P, st)
    autorun.tick(s, P, {})
    assert spies["answer"][-1][0] == "6.3" and spies["answer"][-1][1]["planned"].endswith("planned.json")
    st["auto"]["staged"] = {"bucket": "another-bucket", "keys": ["incoming/D1/a.csv"]}   # same keys, other bucket: not its own
    flowstore.save(s.root, P, st)
    n = len(spies["answer"])
    autorun.tick(s, P, {})
    assert len(spies["answer"]) == n


def test_6_3_move_any_when_allowed(gh, spies, tmp_path):
    fake, clone, s = gh
    write_g1(s, P)
    _full(s, "6.3", answers={"move_any": True}, sub=_preview(s, tmp_path, ["real/x.csv"]))
    autorun.tick(s, P, {})
    assert spies["answer"][-1][0] == "6.3"


def test_6_3_empty_places_a_test_package_waits_and_previews_again(gh, spies, tmp_path, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    _full(s, "6.3", sub=_preview(s, tmp_path, []))
    assert autorun.tick(s, P, {}) == "staging" and spies["start"] == ["stage_test_data"]
    monkeypatch.setattr(jobs, "status", lambda root, jid: {"state": "running"})
    assert autorun.tick(s, P, {}) is None                              # the job runs
    monkeypatch.setattr(jobs, "status", lambda root, jid: {"state": "succeeded", "result": {
        "ok": True, "bucket": "dev-src", "placed": ["incoming/D1/a.csv"], "existing": [], "min_age_s": 600, "folder": "f"}})
    assert autorun.tick(s, P, {}) == "staged"
    st = flow.load(s.root, P)
    assert st["auto"]["staged"]["keys"] == ["incoming/D1/a.csv"]
    assert autorun.tick(s, P, {}) is None and "minimum age" in flow.load(s.root, P)["message"]   # waits
    st = flow.load(s.root, P)
    st["auto"]["staged"]["not_before"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
    flowstore.save(s.root, P, st)
    for _ in range(autorun.PREVIEWS_AFTER_STAGE):
        assert autorun.tick(s, P, {}) == "preview again"
    assert spies["again"] == ["6.3"] * autorun.PREVIEWS_AFTER_STAGE
    assert autorun.tick(s, P, {}).startswith("stopped")
    assert spies["start"] == ["stage_test_data"]                       # placed once


def test_6_3_empty_and_the_package_cannot_be_placed(gh, spies, tmp_path, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    _full(s, "6.3", sub=_preview(s, tmp_path, []))
    autorun.tick(s, P, {})
    monkeypatch.setattr(jobs, "status", lambda root, jid: {"state": "succeeded", "result": {
        "ok": False, "problem": "AccessDenied", "commands": ["aws s3 cp a s3://b/a"], "upload_sh": "/x/upload.sh"}})
    assert autorun.tick(s, P, {}).startswith("stopped")
    st = flow.load(s.root, P)
    assert st["auto"]["stage_failed"]["commands"] == ["aws s3 cp a s3://b/a"] and "AccessDenied" in st["auto"]["needs_you"]


def test_6_5_must_be_a_no_op_and_7_2_is_done(gh, spies, tmp_path):
    fake, clone, s = gh
    write_g1(s, P)
    _full(s, "6.5")
    autorun.tick(s, P, {})
    assert spies["answer"][-1] == ("6.5", {})
    _full(s, "6.5", sub=_preview(s, tmp_path, ["incoming/x"]))
    assert autorun.tick(s, P, {}).startswith("stopped")
    _full(s, "7.2")
    autorun.tick(s, P, {})
    assert spies["answer"][-1][0] == "7.2"


def test_a_failed_local_run_previews_again_once(gh, spies, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    monkeypatch.setattr(flow, "custom_delivery", lambda s_, p_: True)      # a move pipeline: there is a preview
    _full(s, "6.3", status="failed", row={"problems": ["the run failed"]})
    assert autorun.tick(s, P, {}) == "preview again"
    assert autorun.tick(s, P, {}).startswith("stopped")               # once; then a person
    assert spies["again"] == ["6.3"]


def test_review_fixes_no_real_rerun_and_6_5_stops(gh, spies, monkeypatch):
    """0.8.2 review: a failed 6.3 of a pipeline without a preview, or a failed 6.5, never runs for real again."""
    fake, clone, s = gh
    write_g1(s, P)
    monkeypatch.setattr(flow, "custom_delivery", lambda s_, p_: False)
    _full(s, "6.3", status="failed", row={"problems": ["the run failed"]})
    assert autorun.tick(s, P, {}).startswith("stopped") and not spies["again"]
    _full(s, "6.5", status="failed", row={"problems": ["not a no-op"]})
    assert autorun.tick(s, P, {}).startswith("stopped") and not spies["again"]
    _full(s, "6.5")
    autorun.tick(s, P, {})
    st = flow.load(s.root, P)
    st["steps"]["6.5"]["status"] = "waiting"
    flowstore.save(s.root, P, st)
    assert autorun.tick(s, P, {}).startswith("stopped")               # Run again once only
    assert [a for a in spies["answer"] if a[0] == "6.5"] == [("6.5", {})]


def test_review_fixes_stop_shown_again_and_s6_memory_reset(gh, spies, tmp_path):
    from pkf_studio.ui.workflow_dev import set_full_auto

    fake, clone, s = gh
    write_g1(s, P)
    _full(s, "7.3")
    autorun.tick(s, P, {})
    set_full_auto(s, P, {"full_auto": False})
    set_full_auto(s, P, {"full_auto": True, "own_credentials": True})
    assert autorun.tick(s, P, {}).startswith("stopped") and flow.load(s.root, P)["auto"]["needs_you"]
    st = flow.load(s.root, P)
    st["auto"].update(staged={"bucket": "b", "keys": ["k"]}, tries={"stage": 1, "preview-after-stage": 3, "regen-x-y": 1})
    autorun.forget_s6(st)
    assert "staged" not in st["auto"] and st["auto"]["tries"] == {"regen-x-y": 1}


def test_review_fixes_5_3_after_a_good_5_2(gh, spies):
    fake, clone, s = gh
    write_g1(s, P)
    st = _full(s, "5.3")
    st["steps"]["5.1"]["problems"] = ["plan warning"]
    st["steps"]["5.2"] = {"status": "done", "detail": "Render & plan again: ok"}
    flowstore.save(s.root, P, st)
    assert autorun.tick(s, P, {}) == "Continue"


def test_the_decision_cap(gh, spies):
    fake, clone, s = gh
    write_g1(s, P)
    st = _full(s, "2.2")
    st["auto"] = {"count": autorun.MAX_DECISIONS}
    flowstore.save(s.root, P, st)
    assert autorun.tick(s, P, {}).startswith("stopped") and not spies["answer"]


def test_start_with_full_auto_turns_on_auto_approve_and_the_assistant(gh, monkeypatch):
    fake, clone, s = gh
    write_g1(s, P)
    monkeypatch.setattr(flow, "_advance", lambda s_, p_, states, st: st)
    from pkf_studio import stages

    st = flow.start(s, P, stages.derive(s, P), {"full_auto": True, "auto_approve": False, "assistant_autopilot": False,
                                                "no_old_dag": True})
    assert st["full_auto"] and st["auto_approve"] and st["assistant_autopilot"] and st["answers"]["no_old_dag"]


# ------------------------------------------------------------------------------------------------ drive lease
def test_one_tab_drives_the_run(tmp_path):
    assert drive.acquire(tmp_path, P, "tab-a", now=100.0)
    assert not drive.acquire(tmp_path, P, "tab-b", now=101.0)          # a fresh lease of another tab
    assert drive.acquire(tmp_path, P, "tab-a", now=105.0)              # renewed
    assert drive.acquire(tmp_path, P, "tab-b", now=105.0 + drive.TTL + 1)   # tab-a went quiet (background): tab-b takes over
    assert not drive.acquire(tmp_path, P, "tab-a", now=105.0 + drive.TTL + 2)
    drive.release(tmp_path, P, "tab-b")
    assert drive.acquire(tmp_path, P, "tab-a", now=200.0)


# ------------------------------------------------------------------------------------------------ UI
def test_the_intake_and_run_menu_switch(gh):
    fake, clone, s = gh
    write_g1(s, P)
    at = _open(app())
    tog = next(t for t in at.toggle if t.key == f"wfd-full-{P}")
    tog.set_value(True).run()
    no_errors(at)
    assert at.checkbox(key=f"wfd-full-{P}-own").value and at.checkbox(key=f"wfd-full-{P}-dag").value
    assert not at.checkbox(key=f"wfd-full-{P}-any").value
    _state(s.root, "2.1", status="running")
    at = _open(app())
    at.toggle(key=f"wfd-full-live-{P}").set_value(True).run()
    no_errors(at)
    st = flow.load(s.root, P)
    assert st["full_auto"] and st["auto_approve"] and st["answers"]["own_credentials"] is True


def test_the_6_3_box_prepares_a_test_package_or_shows_the_commands(gh, monkeypatch, tmp_path):
    from pkf_studio import testdata

    fake, clone, s = gh
    write_g1(s, P)
    st = _state(s.root, "6.3")
    st["sub"] = {"preview": {"files": 0, "planned": str(tmp_path / "planned.json"), "source_bucket": "dev-src", "deletes": 0}}
    flowstore.save(s.root, P, st)
    plan = {"ok": True, "example": "typical", "bucket": "dev-src", "region": "eu-north-1", "endpoint_url": None,
            "objects": [{"key": "incoming/D1/a.csv", "size": 10}], "bytes": 10, "min_age_s": 0, "root": "incoming"}
    monkeypatch.setattr(testdata, "plan", lambda read, p, mode="dev": dict(plan))
    started = []
    monkeypatch.setattr(jobs, "start", lambda root, p, action, args=None, title="", **kw: started.append(action) or "20261020T120000-bbbbbb")
    at = _open(app())
    at.button(key=f"wfd-testpkg-{P}").click().run()
    assert started == ["stage_test_data"]
    folder = testdata.write_local(s.root, P, plan, stamp="t1")
    st = flow.load(s.root, P)
    st["auto"] = {"stage_failed": {"problem": "AccessDenied", "commands": ["aws s3 cp x s3://dev-src/incoming/D1/a.csv"],
                                   "upload_sh": str(folder / "upload.sh")}}
    flowstore.save(s.root, P, st)
    at = _open(app())
    assert any("aws s3 cp x" in str(c.value) for c in at.code)
    assert any(b.key == f"wfd-testpkg-sh-{P}" for b in at.get("download_button"))
