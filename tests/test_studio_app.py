"""The Streamlit app (AppTest): startup checks, setup page, every tab, edits that survive tab switches, and requesting
G1 approval from the Workflow tab — against the fake GitHub, with the AI replayed."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from fakegithub import FakeGitHub, make_repo
from pkf_studio import jobs, teamsetup
from pkf_studio.github import GitHub
from pkf_studio.project import Person, ProjectConfig, Team, write_local
from pkf_studio.service import Studio
from pkf_studio.ui.requirements import import_text

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "src" / "pkf_studio" / "app.py")
P = "lab_drop_daily"


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    from test_studio_e2e import REQUIREMENT

    bare, clone = make_repo(tmp_path)
    fake = FakeGitHub(bare)
    fake.add_user("dev1", "dev@example.com", token="tok-dev1")
    for k, v in {"GITHUB_TOKEN": "tok-dev1", "PKF_STUDIO_INLINE_JOBS": "1", "PKF_LLM_PROVIDER": "replay",
                 "PKF_LLM_FIXTURES": str(ROOT / "tests" / "llm-fixtures"), "PKF_PLUGIN_ROOT": str(ROOT / "steps-plugin"),
                 "PKF_STUDIO_ROOT": str(clone), "ANTHROPIC_API_KEY": "x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(teamsetup, "build_wheel", lambda dest, log=print: None)
    s = Studio.open(clone, github=GitHub(fake.api, "acme", "specs", "tok-dev1"))
    me = Person(name="Dev One", email="dev@example.com", roles=["product_owner", "business_analyst", "architect", "data_engineer"],
                github="dev1", github_id=fake.users["dev1"]["id"])
    teamsetup.open_pr(s, ProjectConfig(mode="dev", github_api=fake.api, aws_region="eu-west-1"), Team(people=[me]), ["@dev1"])
    fake.merge(1)
    s = Studio.open(clone)
    write_local(s.root, {"email": "dev@example.com"})
    import_text(s, P, "lab-drop.md", REQUIREMENT.encode(), REQUIREMENT, [])
    jid = jobs.start(s.root, P, "analyze", {}, "analyze")
    assert jobs.status(s.root, jid)["state"] == "succeeded", jobs.log(s.root, jid)
    yield fake, clone
    fake.close()


def app():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(APP, default_timeout=120)
    return at


def no_errors(at):
    assert not at.exception, [e.value for e in at.exception]


def test_tabs_render_and_g1_request(repo):
    fake, clone = repo
    at = app()
    at.session_state["pipe-pick"] = P
    at.session_state["page"] = "Pipelines"        # 0.7.0: the app opens on the Workspace; this test reads the Pipelines page
    at.run()
    no_errors(at)
    assert at.title[0].value == P
    texts = " ".join(m.value for m in at.markdown)
    assert "Finalized requirement" in " ".join(h.value for h in at.subheader)
    req_areas = [t for t in at.text_area if t.key and t.key.startswith(f"req-{P}-")]
    assert req_areas and "TRIGGER" in req_areas[0].value
    for tab in ("Source", "Destination", "Trigger", "Transformation", "Pipeline", "Workflow"):
        at.session_state["tab"] = tab
        at.run()
        no_errors(at)
    texts = " ".join(m.value for m in at.markdown)
    assert "S1 Requirements" in texts
    btn = next(b for b in at.button if b.label.startswith("Request approval (G1)"))
    btn.click()
    at.run()
    no_errors(at)
    assert any(p["head"]["ref"] == f"pkf/{P}/g1" for p in fake.pulls.values())
    at.run()
    no_errors(at)
    labels = [b.label for b in at.button]
    assert "Post /approve as me" in labels and "Merge" in labels


def test_setup_page_and_checks(repo):
    at = app()
    at.session_state["page"] = "Setup"
    at.run()
    no_errors(at)
    md = " ".join(m.value for m in at.markdown)
    assert "Studio project and team on the base branch" in md and "GitHub token" in md
