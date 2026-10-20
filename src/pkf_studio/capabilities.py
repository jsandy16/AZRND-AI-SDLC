"""What generated code can do, at a glance, for the G4 reviewer: imports, AWS calls (deletes first), file, network and
process use. A static reading of the source (ast) — it does not run the code."""
from __future__ import annotations

import ast
from pathlib import Path

DELETES = {"delete_object", "delete_objects", "delete_bucket", "delete_message", "delete_message_batch", "purge_queue", "remove",
           "unlink", "rmtree", "rmdir"}
WRITES = {"put_object", "copy_object", "copy", "upload_file", "upload_fileobj", "put_object_tagging", "create_multipart_upload",
          "complete_multipart_upload", "send_message", "write_text", "write_bytes"}
NETWORK = {"socket", "requests", "urllib", "http", "httpx", "ftplib", "paramiko", "smtplib", "aiohttp"}
PROCESS = {"subprocess", "multiprocessing", "os.system", "os.popen", "pty"}
COPY_OBJECT_WARNING = ("copy_object is limited to 5 GiB per object; the managed copy (multipart) is needed for larger files")


def summarize(source: str) -> dict:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {"error": f"does not parse: {exc}"}
    imports, calls, opens = set(), {}, 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name:
                calls[name] = calls.get(name, 0) + 1
                if name == "open":
                    opens += 1
    top = {i.split(".")[0] for i in imports}
    return {"imports": sorted(imports),
            "deletes": sorted(c for c in calls if c in DELETES),
            "writes": sorted(c for c in calls if c in WRITES),
            "network": sorted(top & NETWORK),
            "processes": sorted((top & {p.split(".")[0] for p in PROCESS if "." not in p}) |
                                ({"os.system"} if calls.get("system") else set()) | ({"os.popen"} if calls.get("popen") else set())),
            "file_opens": opens,
            "aws_calls": sorted(c for c in calls if c in DELETES | WRITES or c.startswith(("get_", "list_", "head_"))),
            # B3: a single CopyObject call fails above 5 GiB (the managed `copy` does multipart)
            "warnings": [COPY_OBJECT_WARNING] if calls.get("copy_object") else []}


def report(wt: Path, files: list[str], missing: list[str] = ()) -> str:
    """`missing`: components whose custom steps have no code yet, each "<kind> <name>: <step id>, <step id>"."""
    lines = ["# Capability summary (generated code)", "",
             "A static reading of the code the AI wrote — what it imports and calls. Review deletes, network and process use first.", ""]
    for m in missing:
        comp, _, ids = str(m).partition(":")
        lines += [f"- **Warning:** No generated code yet: {comp.strip()} has custom steps {ids.strip() or '(unknown)'} that are not coded "
                  "(Workflow → S4 → Regenerate (AI))", ""]
    if not files:
        if missing:
            return "\n".join(lines)
        return "\n".join(lines + ["No generated code: every component uses built-in operations only.", ""])
    for rel in files:
        p = Path(wt) / rel
        s = summarize(p.read_text(encoding="utf-8")) if p.exists() else {"error": "missing"}
        lines.append(f"## `{rel}`")
        if "error" in s:
            lines += [f"- **{s['error']}**", ""]
            continue
        flag = lambda xs: ", ".join(f"`{x}`" for x in xs) or "none"  # noqa: E731
        lines += [f"- **Warning:** {w}" for w in s["warnings"]]
        lines += [f"- **Deletes:** {flag(s['deletes'])}", f"- **Writes:** {flag(s['writes'])}", f"- **Network modules:** {flag(s['network'])}",
                  f"- **Processes:** {flag(s['processes'])}", f"- **open() calls:** {s['file_opens']}", f"- **Imports:** {flag(s['imports'])}",
                  f"- **AWS-style calls:** {flag(s['aws_calls'])}", ""]
    return "\n".join(lines)
