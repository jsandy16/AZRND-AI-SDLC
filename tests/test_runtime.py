"""Runtime pieces that need no servers: file selection, error classification, credentials, staging, variables."""
from __future__ import annotations

import datetime as dt
import json
import ssl
import urllib.error

import pytest

from pkf_runtime import credentials, staging, tasks
from pkf_runtime.package import PackageError
from pkf_runtime.sources import RemoteFile, access_errors, depth_ok, select_files

NOW = 1_000_000.0


def rf(path, size=10, age=3600):
    return RemoteFile(path, size, NOW - age)


def test_select_files_rules():
    files = [rf("a/x.csv"), rf("a/x.tmp"), rf("b/y.csv", age=5), rf("z.csv", size=None)]
    ex = {"include": ["**/*.csv"], "exclude": ["**/*.tmp"], "min_age_seconds": 60, "min_files": 1}
    assert [f.path for f in select_files(files, ex, now=NOW)] == ["a/x.csv", "z.csv"]
    assert [f.path for f in select_files(files, {**ex, "regex": r"a/.*"}, now=NOW)] == ["a/x.csv"]
    with pytest.raises(PackageError, match="min_files"):
        select_files(files, {**ex, "min_files": 3}, now=NOW)
    with pytest.raises(PackageError, match="max_files"):
        select_files(files, {**ex, "max_files": 1}, now=NOW)
    with pytest.raises(PackageError, match="max_total_bytes"):
        select_files(files, {**ex, "max_total_bytes": 5}, now=NOW)


def test_depth():
    assert depth_ok("a", 1) and not depth_ok("a/b", 1) and depth_ok("a/b/c", None)


@pytest.mark.parametrize("exc,classified", [
    (urllib.error.HTTPError("u", 401, "Unauthorized", {}, None), True),
    (urllib.error.HTTPError("u", 503, "Unavailable", {}, None), False),
    (urllib.error.URLError(ssl.SSLCertVerificationError(1, "bad cert")), True),
    (ConnectionResetError(), False),
])
def test_access_errors(exc, classified):
    with pytest.raises(PermissionError if classified else type(exc)):
        with access_errors("source s"):
            raise exc


def test_credentials_local_and_env(tmp_path, monkeypatch):
    f = tmp_path / "dev.yaml"
    f.write_text("user: u1\npass: p1\n")
    got = credentials.resolve({"provider": "local_file", "ref": str(f), "fields": {"username": "user", "password": "pass"}}, {})
    assert got["username"] == "u1" and got["password"] == "p1"
    with pytest.raises(credentials.CredentialError):
        credentials.resolve({"provider": "local_file", "ref": str(f), "fields": {"token": "missing"}}, {})
    monkeypatch.setenv("SRC_DEV_USERNAME", "u2")
    assert credentials.resolve({"provider": "env", "ref": "SRC_{env}_"}, {"env": "DEV"}) == {"username": "u2"}
    assert credentials.resolve({"provider": "none"}, {}) == {}


def test_staging_file_round_trip(tmp_path):
    work = tmp_path / "w"
    (work / "files/a").mkdir(parents=True)
    (work / "files/a/b.txt").write_text("x")
    (work / "package.json").write_text("{}")
    url = staging.stage_url(f"file://{tmp_path / 'stage'}", "p", "manual__2026-10-15T00:00:00+00:00", "00_extract")
    assert ":" not in url.split("/p/")[1]
    staging.put(work, url)
    back = tmp_path / "back"
    staging.get(url, back)
    assert (back / "files/a/b.txt").read_text() == "x"
    staging.clear_run(f"file://{tmp_path / 'stage'}", "p", "manual__2026-10-15T00:00:00+00:00")
    assert not any((tmp_path / "stage").rglob("*.txt"))


def test_variables_layering(monkeypatch):
    monkeypatch.setenv("PKF_ENV", "prod")
    lock = {"pipeline": {"name": "p", "params": {"a": "pipe", "b": "pipe"}}, "source": {"name": "s", "params": {"a": "src", "c": "src"}},
            "destination": {"name": "d"}, "connection": {"name": "c"}, "transformation": {"params": {"b": "tr", "d": "tr"}}}
    v = tasks.variables(lock, {"run_id": "r", "logical_date": dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc), "params": {"d": "run", "c": ""}})
    assert (v["a"], v["b"], v["c"], v["d"]) == ("pipe", "pipe", "src", "run")
    assert (v["ds"], v["ds_nodash"], v["env"], v["source"]) == ("2026-10-15", "20261015", "prod", "s")


@pytest.mark.parametrize("body", [
    "    import zipfile\n    zipfile.os.system('id')",
    "    import io\n    io.open('/etc/passwd')",
    "    import gzip\n    gzip.open('/etc/passwd')",
    "    x = '{0.__class__}'.format(pkg)",
    "    getattr(pkg, 'x')",
    "    import os",
])
def test_guard_blocks_known_escapes(body):
    from pkf.design import guard

    assert guard(f"def step(pkg, params, ctx):\n{body}\n", "step")


def test_guard_allows_normal_code():
    from pkf.design import guard

    src = "def step(pkg, params, ctx):\n    import re, zipfile\n    for f in sorted(pkg.files()):\n        if re.fullmatch(r'x', f):\n            pkg.fail('bad')\n"
    assert guard(src, "step") == []


def test_variables_ignore_undeclared_and_builtin_overrides():
    lock = {"pipeline": {"name": "p", "params": {"input_path": "/a"}}, "source": {"name": "s", "params": {}},
            "destination": {"name": "d"}, "connection": {"name": "c"}, "transformation": None}
    v = tasks.variables(lock, {"run_id": "r", "logical_date": dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc),
                               "params": {"env": "prod", "source": "x", "ds": "../../x", "input_path": "/b", "other": "1"}})
    assert (v["env"], v["source"], v["ds"], v["input_path"]) != ("prod", "x", "../../x", None)
    assert v["source"] == "s" and v["ds"] == "2026-10-15" and v["input_path"] == "/b" and "other" not in v


def test_prefix_cannot_escape(tmp_path):
    from pkf_runtime.destinations import LocalDestination, deliver
    from pkf_runtime.package import Package

    pkg = Package(tmp_path / "w", "p", {})
    pkg.write_text("a.txt", "x")
    with pytest.raises(PackageError):
        deliver(pkg, LocalDestination({"base_path": str(tmp_path / "out")}, {}), {"a.txt": "raw/../../etc/a.txt"}, {})


def test_https_redirect_to_other_host_refused():
    import urllib.request

    from pkf_runtime.sources.https import _SameOriginRedirects

    req = urllib.request.Request("https://a.example/x", headers={"Authorization": "Bearer s"})
    with pytest.raises(PermissionError):
        _SameOriginRedirects().redirect_request(req, None, 302, "Found", {}, "http://b.example/y")


def test_custom_steps_see_no_local_paths(tmp_path):
    from pkf_runtime.package import Package, StepPackage

    pkg = Package(tmp_path / "w", "p", {})
    pkg.write_text("a.txt", "x")
    view = StepPackage(pkg)
    for attr in ("workdir", "root", "path", "new_temp_path", "add_local_file", "checksums"):
        assert not hasattr(view, attr)
    with view.open_read("a.txt") as fh:
        assert fh.read() == b"x"
    with view.open_write("b.bin") as fh:
        fh.write(b"y")
    assert view.files() == ["a.txt", "b.bin"]
    with pytest.raises(PackageError):
        view.open_write("../escape")


@pytest.mark.parametrize("body", [
    "    import string\n    string.Formatter()",
    "    x = ('{0.' + '_s}').format(src)",
    "    s = 'a'\n    s.format_map({})",
])
def test_guard_blocks_format_walks(body):
    from pkf.design import guard

    assert guard(f"def step(groups, params, ctx, src):\n{body}\n", "step", ("groups", "params", "ctx", "src"))


def test_guard_allows_literal_format():
    from pkf.design import guard

    assert guard("def step(pkg, params, ctx):\n    x = 'a{}'.format(1)\n    y = f'{x}'\n", "step") == []


def test_lock_edit_detected(tmp_path):
    lock = {"pipeline": {"name": "p"}, "a": 1}
    lock["lock_sha256"] = tasks.lock_digest(lock)
    (tmp_path / "pkf/p").mkdir(parents=True)
    (tmp_path / "pkf/p/pipeline.lock.json").write_text(json.dumps({**lock, "a": 2}))
    with pytest.raises(PermissionError, match="edited"):
        tasks.load_lock(str(tmp_path / "pkf_p.py"), "p")


def test_dedupe_keeps_events_without_objects():
    from pkf_runtime.events import messages_to_events, run_event_steps

    ev = run_event_steps(messages_to_events([{"id": f"m{i}", "body": "{}"} for i in range(3)]),
                         [{"id": "p", "op": "parse_json", "params": {}}, {"id": "d", "op": "dedupe", "params": {}}], {"params": {}})
    assert [e["message_ids"] for e in ev] == [["m0"], ["m1"], ["m2"]]


def test_custom_steps_cannot_drop_message_ids_or_crash_the_batch():
    from pkf_runtime.discovery import SourceView, run_source_steps
    from pkf_runtime.events import messages_to_events, run_event_steps

    with pytest.raises(PackageError, match="message_ids"):
        run_event_steps(messages_to_events([{"id": "m0", "body": "{}"}]), [{"id": "c", "op": "custom", "params": {"function": "c"}}],
                        {"params": {}}, {"c": lambda ev, p, c: [{"id": "x"}]})
    src = SourceView(None)
    with pytest.raises(PackageError, match="ValueError"):
        run_source_steps([{"id": "g", "files": [], "vars": {}}], [{"id": "c", "op": "custom", "params": {"function": "c"}}],
                         {"params": {}}, src, {"c": lambda g, p, c, s: (_ for _ in ()).throw(ValueError("incomplete batch"))})
