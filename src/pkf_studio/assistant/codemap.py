"""The framework's code map (0.8.0): one line of purpose per file and its top-level classes and functions, generated from
the source with `ast` (nothing is imported or executed), so the assistant knows where everything is without reading
470k tokens of code. It is part of the stable system prompt (cached by the API) and searchable with the framework_map
tool; the full code stays on demand through read_file / grep (root `framework`).

Cached at <repo>/.pkf-studio/cache/codemap-<pkf VERSION>-<hash>.md, the hash covering every source file's path, size
and mtime: a changed framework regenerates it, an unchanged one costs a stat per file. The cache folder is git-ignored,
readable by the assistant (read root `cache`) and never writable by it (the write guard keeps to the worktree)."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

from .. import STUDIO_DIR

PACKAGES = ("pkf", "pkf_runtime", "pkf_studio")
PLUGIN = "steps-plugin"
MAX_CHARS = 56_000          # ~14k tokens: the plan's 12–15k target, capped
SIG_MAX = 90


def _sources(framework_dir: Path) -> list[tuple[str, Path]]:
    """(display path, file) of every framework source file: the packages under the framework source root (falling
    back to the installed packages) and the steps plugin's skills when the plugin sits beside the source."""
    from .tools import _package_dir

    out: list[tuple[str, Path]] = []
    fw = Path(framework_dir)
    for name in PACKAGES:
        pkg = fw / name if (fw / name / "__init__.py").exists() else _package_dir(name)
        if pkg is None or not Path(pkg).is_dir():
            continue
        pkg = Path(pkg)
        for f in sorted(pkg.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            out.append((f"{name}/{f.relative_to(pkg).as_posix()}", f))
        for f in sorted(pkg.rglob("*.md")):         # the assistant's own briefing, framework docs shipped in packages
            if "__pycache__" not in f.parts:
                out.append((f"{name}/{f.relative_to(pkg).as_posix()}", f))
    for base in (fw.parent / PLUGIN, fw / PLUGIN):
        if base.is_dir():
            for f in sorted(base.rglob("SKILL.md")):
                out.append((f"{PLUGIN}/{f.relative_to(base).as_posix()}", f))
            break
    return out


def fingerprint(framework_dir: Path) -> str:
    h = hashlib.sha256()
    for rel, f in _sources(framework_dir):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


def _first_sentence(text: str | None, limit: int = 160) -> str:
    t = " ".join((text or "").split())
    if not t:
        return ""
    for sep in (". ", ": "):
        i = t.find(sep)
        if 0 < i < limit:
            return t[:i + 1].rstrip(":") if sep == ". " else t[:i]
    return t if len(t) <= limit else t[:limit - 1] + "…"


def _sig(node) -> str:
    a = node.args
    names = [x.arg for x in (*a.posonlyargs, *a.args)]
    if names and names[0] in ("self", "cls"):
        names = names[1:]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        names.append("*")
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    s = f"{node.name}({', '.join(names)})"
    return s if len(s) <= SIG_MAX else s[:SIG_MAX - 2] + "…)"


DOC_LEN = {0: 120, 1: 100, 2: 90, 3: 60}


def _py_entry(rel: str, path: Path, level: int) -> list[str]:
    """level 0: everything; 1: private functions without their docstring line; 2: no private functions/methods;
    3: shorter docstring lines and no method lists."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return [f"## {rel} — (could not be parsed)"]
    doc = _first_sentence(ast.get_docstring(tree))
    lines = [f"## {rel}" + (f" — {doc}" if doc else "")]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            private = node.name.startswith("_")
            if private and level >= 2:
                continue
            d = "" if (private and level >= 1) else _first_sentence(ast.get_docstring(node), DOC_LEN[level])
            lines.append(f"- def {_sig(node)}" + (f" — {d}" if d else ""))
        elif isinstance(node, ast.ClassDef):
            d = _first_sentence(ast.get_docstring(node), DOC_LEN[level])
            lines.append(f"- class {node.name}" + (f" — {d}" if d else ""))
            methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and (not m.name.startswith("_") or (level == 0 and m.name not in ("__init__", "__repr__", "__str__")))]
            if methods and level < 3:
                shown = [m.name for m in methods if level < 2 or not m.name.startswith("_")]
                if shown:
                    lines.append("  methods: " + ", ".join(shown))
    return lines


def _md_entry(rel: str, path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [f"## {rel}"]
    desc = ""
    for line in text.splitlines()[:30]:
        if line.lower().startswith("description:"):
            desc = line.split(":", 1)[1].strip().strip("'\"")
            break
    if not desc:
        desc = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
    return [f"## {rel}" + (f" — {_first_sentence(desc)}" if desc else "")]


def generate(framework_dir: Path, version: str = "") -> str:
    """The code map as markdown, at the most detailed level that fits MAX_CHARS (then cut, saying so)."""
    files = _sources(framework_dir)
    head = (f"# Framework code map (pkf {version or '?'}; {len(files)} files)\n"
            "One line per file (its purpose) and its top-level functions and classes. Read the code itself with read_file / "
            "grep in root `framework` (paths below are relative to it; steps-plugin sits beside it); search this map with "
            "framework_map.\n")
    text = head
    for level in (0, 1, 2, 3):
        body: list[str] = []
        for rel, f in files:
            body += _md_entry(rel, f) if f.suffix == ".md" else _py_entry(rel, f, level)
        text = head + "\n" + "\n".join(body) + "\n"
        if len(text) <= MAX_CHARS:
            return text
    cut = text[:MAX_CHARS].rsplit("\n", 1)[0]
    return cut + f"\n… [code map cut at {MAX_CHARS} characters; use grep / list_files in root `framework` for the rest]\n"


def cache_dir(root: Path) -> Path:
    return Path(root) / STUDIO_DIR / "cache"


def path_for(root: Path, framework_dir: Path) -> Path:
    import pkf

    return cache_dir(root) / f"codemap-{pkf.VERSION}-{fingerprint(framework_dir)}.md"


def text(root: Path, framework_dir: Path) -> str:
    """The cached code map, regenerated (and older maps removed) when the framework source changed."""
    import pkf

    target = path_for(root, framework_dir)
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        pass
    body = generate(framework_dir, pkf.VERSION)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".tmp{os.getpid()}")
        tmp.write_text(body, encoding="utf-8", newline="\n")
        os.replace(tmp, target)
        for old in target.parent.glob("codemap-*.md"):
            if old != target:
                old.unlink(missing_ok=True)
    except OSError:
        pass                          # a read-only cache folder: the map still works, it is just not cached
    return body


def search(root: Path, framework_dir: Path, query: str, limit: int = 80) -> list[str]:
    """The map's lines matching every word of `query` (case-insensitive), each under its file's heading."""
    words = [w.lower() for w in str(query or "").split() if w.strip()]
    if not words:
        return ["(give one or more words: a file, module, function or class name, or a word of its purpose)"]
    out: list[str] = []
    heading = ""
    shown_heading = None
    for line in text(root, framework_dir).splitlines():
        if line.startswith("## "):
            heading = line
        low = line.lower()
        if all(w in low for w in words) or (line.startswith(("- ", "  ")) and all(w in (heading + " " + line).lower() for w in words)):
            if line.startswith(("- ", "  ")) and shown_heading != heading:
                out.append(heading)
                shown_heading = heading
            elif line.startswith("## "):
                shown_heading = heading
            if line not in out:
                out.append(line)
            if len(out) >= limit:
                out.append(f"… [stopped at {limit} lines; narrow the query]")
                break
    return out or [f"(no line of the code map matches {query!r}; try grep in root `framework`)"]
