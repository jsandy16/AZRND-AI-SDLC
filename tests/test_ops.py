"""Package API and catalogue operations (deterministic, no network)."""
from __future__ import annotations

import gzip
import io
import json
import zipfile

import pytest

from pkf_runtime.ops import OPS, catalogue, parse_params, run_step
from pkf_runtime.package import Package, PackageError, TemplateError, glob_match, render, safe_relpath


def make(tmp_path, files, name="pkg", **vars):
    pkg = Package(tmp_path / "w", name, {"ds_nodash": "20261015", "source": "src", **vars})
    for f in files:
        pkg.write_bytes(f, f"data:{f}".encode())
    return pkg


def run(pkg, op, **params):
    run_step(pkg, op, params, {"params": dict(pkg.vars)})


# ---------------------------------------------------------------- package basics
@pytest.mark.parametrize("pattern,path,ok", [
    ("**/*.csv", "a.csv", True), ("**/*.csv", "x/y/a.csv", True), ("*.csv", "x/a.csv", False),
    ("a/**", "a/b/c", True), ("a/**", "ab/c", False), ("SITE_?/*", "SITE_1/f", True), ("[ab].txt", "c.txt", False),
])
def test_glob(pattern, path, ok):
    assert glob_match(pattern, path) is ok


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../b", "", "a/./b/.."])
def test_safe_relpath_refuses_escapes(bad):
    with pytest.raises(PackageError):
        safe_relpath(bad)


def test_render_is_strict():
    assert render("{a}_{b}", {"a": 1, "b": "x"}) == "1_x"
    with pytest.raises(TemplateError):
        render("{missing}", {})


def test_save_load_round_trip_keeps_checksums(tmp_path):
    pkg = make(tmp_path, ["a/b.csv"])
    digest = pkg.checksum("a/b.csv", "md5")
    pkg.move("a/b.csv", "c.csv")
    pkg.save()
    again = Package.load(tmp_path / "w")
    assert again.files() == ["c.csv"] and again.checksums["c.csv"]["md5"] == digest and again.name == "pkg"


def test_move_prunes_empty_folders(tmp_path):
    pkg = make(tmp_path, ["a/b/c.txt"])
    pkg.move("a/b/c.txt", "c.txt")
    assert not (pkg.root / "a").exists()


# ---------------------------------------------------------------- operations
def test_select_and_require(tmp_path):
    pkg = make(tmp_path, ["SITE_0001/a.csv", "SITE_0001/n.txt", "x.csv"])
    run(pkg, "select", include=["**/*.csv"])
    assert pkg.files() == ["SITE_0001/a.csv", "x.csv"]
    with pytest.raises(PackageError, match="unexpected folder"):
        run(pkg, "require", folder_regex=r"SITE_\d{4}")
    run(pkg, "select", exclude=["*.csv"])
    run(pkg, "require", folder_regex=r"SITE_\d{4}", patterns=["**/*.csv"], min_files=1)


def test_move_with_groups_and_conflict(tmp_path):
    pkg = make(tmp_path, ["D1/sub/a.csv", "D1/b.csv", "D2/a.csv"])
    run(pkg, "move", match=r"(?P<d>[^/]+)/(?:.*/)?(?P<f>[^/]+)", to="data/{d}/{f}")
    assert pkg.files() == ["data/D1/a.csv", "data/D1/b.csv", "data/D2/a.csv"]
    with pytest.raises(PackageError, match="same path"):
        run(pkg, "flatten")


def test_prefix_strip_rename(tmp_path):
    pkg = make(tmp_path, ["a/b/c.txt"])
    run(pkg, "strip_folders", levels=1)
    run(pkg, "rename_package", name="{source}_{ds_nodash}")
    run(pkg, "prefix", folder="{package}")
    assert pkg.name == "src_20261015" and pkg.files() == ["src_20261015/b/c.txt"]
    with pytest.raises(PackageError):
        run(pkg, "strip_folders", levels=5)


def test_zip_is_reproducible_and_manifest_describes_it(tmp_path):
    digests = []
    for i in range(2):
        pkg = make(tmp_path / str(i), ["S/a.csv", "S/b.csv"])
        run(pkg, "zip", name="{package}.zip")
        run(pkg, "checksum", algorithms=["md5", "sha256"])
        run(pkg, "manifest", fields=["path", "size", "md5", "sha256"])
        digests.append(pkg.checksum("pkg.zip"))
        assert pkg.describe() == ["manifest.json", "pkg.zip", "pkg.zip!/S/a.csv", "pkg.zip!/S/b.csv"]
        man = json.loads(pkg.path("manifest.json").read_text())
        assert man["files"][0]["path"] == "pkg.zip" and man["files"][0]["md5"] == pkg.checksum("pkg.zip", "md5")
    assert digests[0] == digests[1]


def test_zip_empty_fails(tmp_path):
    pkg = make(tmp_path, ["a.txt"])
    with pytest.raises(PackageError, match="nothing to zip"):
        run(pkg, "zip", name="x.zip", include=["*.csv"])


def test_checksum_sidecars_and_csv_manifest(tmp_path):
    pkg = make(tmp_path, ["d/a.bin"])
    run(pkg, "checksum", algorithms=["md5"], sidecar=True)
    run(pkg, "manifest", path="manifest.csv", format="csv", fields=["path", "md5"], include=["**/*.bin"])
    assert pkg.files() == ["d/a.bin", "d/a.bin.md5", "manifest.csv"]
    assert pkg.path("d/a.bin.md5").read_text().endswith("  a.bin\n")
    assert pkg.path("manifest.csv").read_text().splitlines()[0] == "path,md5"


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in members.items():
            z.writestr(n, d)
    return buf.getvalue()


def test_unzip_and_guards(tmp_path):
    pkg = make(tmp_path, [])
    pkg.write_bytes("in/bundle.zip", _zip_bytes({"x/a.txt": b"a", "b.txt": b"b"}))
    run(pkg, "unzip")
    assert pkg.files() == ["in/bundle/b.txt", "in/bundle/x/a.txt"]

    pkg.write_bytes("evil.zip", _zip_bytes({"../../etc/x": b"a"}))
    with pytest.raises(PackageError):
        run(pkg, "unzip", include=["evil.zip"])

    pkg2 = make(tmp_path / "bomb", [])
    pkg2.write_bytes("bomb.zip", _zip_bytes({"big": b"\0" * (8 << 20)}))
    with pytest.raises(PackageError, match="zip-bomb"):
        run(pkg2, "unzip", include=["bomb.zip"])

    pkg3 = make(tmp_path / "bad", [])
    pkg3.write_bytes("bad.zip", b"not a zip")
    with pytest.raises(PackageError, match="not a valid zip"):
        run(pkg3, "unzip", include=["bad.zip"])


def test_gzip_round_trip(tmp_path):
    pkg = make(tmp_path, ["a.txt"])
    run(pkg, "gzip")
    assert pkg.files() == ["a.txt.gz"]
    assert gzip.decompress(pkg.path("a.txt.gz").read_bytes()) == b"data:a.txt"
    run(pkg, "gunzip")
    assert pkg.files() == ["a.txt"] and pkg.path("a.txt").read_bytes() == b"data:a.txt"


def test_custom_step_and_unknown(tmp_path):
    pkg = make(tmp_path, ["a"])
    seen = {}
    run_step(pkg, "custom", {"function": "f"}, {"params": {"x": "1"}}, {"f": lambda p, params, ctx: seen.update(params)})
    assert seen == {"x": "1"}
    with pytest.raises(RuntimeError, match="not implemented"):
        run_step(pkg, "custom", {"function": "g"}, {"params": {}}, {})
    with pytest.raises(ValueError, match="unknown operation"):
        parse_params("explode", {})


def test_params_are_strict_and_catalogue_complete():
    with pytest.raises(Exception):
        parse_params("select", {"includes": ["*"]})
    with pytest.raises(Exception):
        parse_params("move", {"match": "(", "to": "x"})
    names = {c["op"] for c in catalogue()}
    assert names == set(OPS) and all(c["summary"] for c in catalogue())


def test_zip_large_member_uses_zip64(tmp_path, monkeypatch):
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 100)
    pkg = make(tmp_path, [])
    pkg.write_bytes("big.bin", b"x" * 1000)
    run(pkg, "zip", name="b.zip")
    assert pkg.describe() == ["b.zip", "b.zip!/big.bin"]


def test_manifest_total_without_path_and_prefix_renders_once(tmp_path):
    pkg = make(tmp_path, ["a.txt"], site="X{ds_nodash}")
    run(pkg, "manifest", fields=["size"])
    assert json.loads(pkg.path("manifest.json").read_text())["total_bytes"] == len(b"data:a.txt")
    run(pkg, "prefix", folder="{site}")
    assert pkg.files() == ["X{ds_nodash}/a.txt", "X{ds_nodash}/manifest.json"]
