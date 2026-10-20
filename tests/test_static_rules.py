"""Static rules for the source tree (G-4, K15, B13, B14): text written with LF and UTF-8 on every platform,
temporary folders whose cleanup never fails a command (Windows keeps files open), and subprocess text decoded
with an explicit encoding (the Windows console code page is not UTF-8).

Every file under src/ is checked except those in NOT_YET: files whose owner fixes them in a later step of the 0.5.0
work (each entry names its owner). Remove an entry once its file complies; `python tests/test_static_rules.py` lists
the entries that already comply. A new file is checked from the start."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

NOT_YET: set[str] = set()
SUBPROCESS_SCOPE = ("pkf_studio/", "pkf/design.py")  # K15: where subprocess text must name its encoding


def _kw(call: ast.Call) -> dict[str, ast.expr]:
    return {k.arg: k.value for k in call.keywords if k.arg}


def _name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _open_mode(call: ast.Call) -> str | None:
    """The mode of `open(path, mode)` / `<path>.open(mode)` when it is a literal, else None."""
    f, kw = call.func, _kw(call)
    if isinstance(f, ast.Name) and f.id == "open":
        m = call.args[1] if len(call.args) > 1 else kw.get("mode")
    elif isinstance(f, ast.Attribute) and f.attr == "open":
        m = call.args[0] if call.args else kw.get("mode")
    else:
        return None
    if m is None:
        return "r"
    return m.value if isinstance(m, ast.Constant) and isinstance(m.value, str) else None


def text_write_problems(source: str) -> list[str]:
    out = []
    for c in ast.walk(ast.parse(source)):
        if not isinstance(c, ast.Call):
            continue
        kw = _kw(c)
        nl = kw.get("newline")
        lf = isinstance(nl, ast.Constant) and nl.value == "\n" and "encoding" in kw
        if _name(c) == "write_text" and not lf:
            out.append(f"line {c.lineno}: write_text without newline=\"\\n\" and encoding=")
        mode = _open_mode(c)
        if mode and "b" not in mode and any(x in mode for x in "wax") and not lf:
            out.append(f"line {c.lineno}: open(..., {mode!r}) without newline=\"\\n\" and encoding=")
    return out


def tempdir_problems(source: str) -> list[str]:
    return [f"line {c.lineno}: TemporaryDirectory without ignore_cleanup_errors=True" for c in ast.walk(ast.parse(source))
            if isinstance(c, ast.Call) and _name(c) == "TemporaryDirectory" and "ignore_cleanup_errors" not in _kw(c)]


def subprocess_problems(source: str) -> list[str]:
    out = []
    for c in ast.walk(ast.parse(source)):
        if isinstance(c, ast.Call) and _name(c) == "run" and isinstance(c.func, ast.Attribute) \
                and getattr(c.func.value, "id", "") == "subprocess":
            kw = _kw(c)
            if ("text" in kw or "universal_newlines" in kw) and "encoding" not in kw:
                out.append(f"line {c.lineno}: subprocess.run(text=True) without encoding=")
    return out


def _files(scope: tuple[str, ...] = ("",)) -> list[tuple[str, str]]:
    out = []
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC).as_posix()
        if rel not in NOT_YET and rel.startswith(scope):
            out.append((rel, p.read_text(encoding="utf-8")))
    return out


def _report(rule) -> dict[str, list[str]]:
    return {rel: probs for rel, src in _files() if (probs := rule(src))}


def test_every_text_write_is_lf():
    assert _report(text_write_problems) == {}


def test_temporary_directories_ignore_cleanup_errors():
    assert _report(tempdir_problems) == {}


def test_subprocess_text_names_its_encoding():
    bad = {rel: probs for rel, src in _files(SUBPROCESS_SCOPE) if (probs := subprocess_problems(src))}
    assert bad == {}


def test_not_yet_names_existing_files():
    assert sorted(rel for rel in NOT_YET if not (SRC / rel).is_file()) == []


@pytest.mark.parametrize(("rule", "bad", "good"), [
    (text_write_problems, 'p.write_text(s, encoding="utf-8")', 'p.write_text(s, encoding="utf-8", newline="\\n")'),
    (text_write_problems, 'open(f, "a", encoding="utf-8")', 'open(f, "ab")'),
    (text_write_problems, 'p.open("w")', 'p.open("w", encoding="utf-8", newline="\\n")'),
    (text_write_problems, 'p.write_text(s, encoding="utf-8", newline="")', 'p.write_text(s, newline="\\n", encoding="utf-8")'),
    (tempdir_problems, "tempfile.TemporaryDirectory()", "tempfile.TemporaryDirectory(ignore_cleanup_errors=True)"),
    (subprocess_problems, "subprocess.run(a, text=True)", 'subprocess.run(a, text=True, encoding="utf-8")'),
])
def test_rules_catch_what_they_describe(rule, bad, good):
    assert rule(bad) and not rule(good)


if __name__ == "__main__":  # which NOT_YET entries comply already (remove them)
    for rel in sorted(NOT_YET):
        src = (SRC / rel).read_text(encoding="utf-8")
        probs = text_write_problems(src) + tempdir_problems(src) + (subprocess_problems(src) if rel.startswith(SUBPROCESS_SCOPE) else [])
        print(f"{'still' if probs else 'COMPLIES'}  {rel}  {len(probs)}")
