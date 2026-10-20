"""Workflow Developer component (ui/static/wfd + ui/wfd.py): the static files ship and speak the Streamlit protocol,
the wrapper imports without a Streamlit runtime, and both views render in an AppTest without raising."""
from __future__ import annotations

import datetime as dt
import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "pkf_studio" / "ui" / "static" / "wfd"

STAGES = [
    ("S1", "S1 Requirements → G1", ["New pipeline name", "Source and destination", "Trigger", "Delivery policy",
                                      "Transformation", "Environments", "Review requirements", "Refresh and Merge"]),
    ("S2", "S2 Design → G2", ["Draft design", "Review design", "Approve G2"]),
    ("S3", "S3 Plans → G3", ["Source plan", "Destination plan", "Trigger plan", "Transformation plan", "Review plans", "Approve G3"]),
    ("S4", "S4 Code and build → G4", ["Start S4: code, test, build", "Review tests", "Fix failures", "Approve G4"]),
    ("S5", "S5 Dev environment → G5", ["Render infra", "Plan", "Apply", "Approve G5"]),
    ("S6", "S6 Run local + record PR", ["Start Airflow", "Trigger a run", "Check the lake", "Record the PR", "Merge"]),
    ("S7", "7 Deploy to Airflow", ["Open deployment PR", "Deploy", "Smoke test", "Sign off"]),
]
KINDS = {"S1": "input", "S2": "review", "S3": "job", "S4": "job", "S5": "job", "S6": "checklist", "S7": "action"}


def sample_snapshot(running: str = "4.1", waiting: str = "3.6", failed: str = "2.3", selected: str = "4.1") -> dict:
    """7 stages, 34 steps: S1 done, S2 failed at 2.3, S3 waiting at 3.6, S4 running at 4.1, the rest not started."""
    t0 = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.timezone.utc)
    stages, n = [], 0
    for sid, title, names in STAGES:
        steps = []
        for i, name in enumerate(names, 1):
            step_id, n = f"{sid[1]}.{i}", n + 1
            if step_id == running:
                status, detail = "running", "pkf build · 3 tests passed, 1 running"
            elif step_id == waiting:
                status, detail = "waiting", "waiting for your approval"
            elif step_id == failed:
                status, detail = "failed", "review rejected: destination bucket missing"
            elif step_id == "1.4":
                status, detail = "skipped", "no delivery policy for this source"
            elif sid == "S1" or (sid == "S2" and i < 3) or (sid == "S3" and i < 6):
                status, detail = "done", "ok"
            else:
                status, detail = "not_started", ""
            started = (t0 + dt.timedelta(minutes=n * 3)).isoformat() if status not in ("not_started", "skipped") else None
            ended = (t0 + dt.timedelta(minutes=n * 3 + 2)).isoformat() if status in ("done", "failed") else None
            steps.append({"id": step_id, "title": name, "kind": KINDS[sid], "status": status, "detail": detail,
                          "started_at": started, "ended_at": ended, "optional": step_id == "1.4"})
        done = sum(s["status"] in ("done", "skipped") for s in steps)
        st = ("running" if any(s["status"] == "running" for s in steps) else "waiting" if any(s["status"] == "waiting" for s in steps)
              else "failed" if any(s["status"] == "failed" for s in steps) else "done" if done == len(steps) else "not_started")
        stages.append({"id": sid, "title": title, "status": st, "progress": [done, len(steps)], "steps": steps})
    return {"run": {"status": "running", "current": running, "mode": "dev",
                    "message": f"S4 Code & build · {running} Start S4: code, test, build — running (2m10s)",
                    "started_at": t0.isoformat(), "updated_at": t0.isoformat()},
            "stages": stages, "selected": selected}


SAMPLE_LINES = [
    {"ts": "09:00:01", "level": "info", "source": "flow", "text": "run started (mode dev)"},
    {"ts": "09:00:02", "level": "tool", "source": "job", "text": "$ pkf build --project demo"},
    {"ts": "09:00:05", "level": "ok", "source": "job", "text": "tests: 3 passed"},
    {"ts": "09:00:06", "level": "warn", "source": "job", "text": "module source/sftp.py: unused import  (ruff F401)"},
    {"ts": "09:00:07", "level": "agent", "source": "assistant", "text": "Reading tests.json …\n  → the failure is in test_delivery_policy"},
    {"ts": "09:00:09", "level": "error", "source": "job", "text": "FAILED tests/test_delivery.py::test_policy - KeyError: 'bucket'"},
    {"ts": "09:00:10", "level": "user", "source": "assistant", "text": "why did test_policy fail?"},
]


def test_static_files_ship_and_speak_the_protocol():
    for name in ("index.html", "wfd.js", "wfd.css"):
        assert (STATIC / name).is_file(), name
    html = (STATIC / "index.html").read_text()
    assert 'src="wfd.js"' in html and 'href="wfd.css"' in html
    js = (STATIC / "wfd.js").read_text()
    for token in ("streamlit:componentReady", "streamlit:render", "streamlit:setFrameHeight", "streamlit:setComponentValue"):
        assert token in js, token
    assert "http://" not in js and "https://" not in js and "http" not in (STATIC / "wfd.css").read_text()   # offline
    text = (ROOT / "pyproject.toml").read_text()
    assert "[tool.setuptools.package-data]" in text and '"ui/static/wfd/*"' in text   # the folder ships in the wheel


def test_wrapper_imports_without_streamlit_runtime():
    # a fresh interpreter: importing the module must not declare the component (that needs the runtime's registry)
    code = textwrap.dedent("""
        import sys
        from pkf_studio.ui import wfd
        assert callable(wfd.tree) and callable(wfd.console)
        assert "streamlit.components.v1" not in sys.modules or wfd._component.cache_info().currsize == 0
        print("ok")
        """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), check=True,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert out.stdout.strip() == "ok"
    mod = importlib.import_module("pkf_studio.ui.wfd")
    assert mod.STATIC.is_dir()


def test_both_views_render_in_apptest():
    from streamlit.testing.v1 import AppTest

    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT / "tests")!r})
        import streamlit as st
        from pkf_studio.ui import wfd
        from test_wfd_component import sample_snapshot, SAMPLE_LINES
        picked = wfd.tree(sample_snapshot(), key="t")
        st.session_state["picked"] = picked
        wfd.console(SAMPLE_LINES, key="c", title="Job log", height=240)
        wfd.console([], key="c2")
        st.write("rendered")
        """)
    at = AppTest.from_string(script, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["picked"] is None          # no click yet: the default comes back
    assert any("rendered" in str(m.value) for m in at.markdown)


def test_sample_snapshot_shape():
    snap = sample_snapshot()
    assert [s["id"] for s in snap["stages"]] == ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    assert sum(len(s["steps"]) for s in snap["stages"]) == 34
    assert {s["status"] for s in snap["stages"]} == {"done", "failed", "waiting", "running", "not_started"}
    assert snap["stages"][0]["progress"] == [8, 8]
