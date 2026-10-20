"""Tabs 2–5 — Source, Destination, Trigger, Transformation: settings and the environment-values form, the plan as step
cards, the generated module, a connectivity snippet; plus the delivery policy, the trigger's AWS pieces and the
transformation's task cards."""
from __future__ import annotations

import ast
import hashlib
import re

import streamlit as st
import yaml

from .. import ledger as L
from ..service import branch_for
from ..service import StudioError
from .common import BUSY_HELP, Files, act, available_after, busy, code_view, go_to, plan_cards, ui_commit

REQ = "<REQUIRED>"


# --------------------------------------------------------------------------------------- environment values
def required_paths(node, path=()) -> list[tuple[tuple, str]]:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += required_paths(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += required_paths(v, path + (i,))
    elif isinstance(node, str) and REQ in node:
        out.append((path, node))
    return out


def _typed(text: str):
    try:
        v = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    return v if isinstance(v, (int, float, bool)) else text


def write_values(path, values: dict[tuple, str]) -> None:
    from pkf import yamlio

    doc = yamlio.load(path)
    for keys, val in values.items():
        node = doc
        for k in keys[:-1]:
            node = node[k]
        old = node[keys[-1]]
        node[keys[-1]] = old.replace(REQ, val) if isinstance(old, str) and old.strip() != REQ else _typed(val)
    yamlio.dump(doc, path)


def _commit_or_restore(s, p: str, path, old: str | None, message: str) -> str | None:
    """Commit the edited file; when the commit is refused (a secret in a value, a job running …) the file is put back as
    it was, so nothing half-saved stays in the worktree (E7)."""
    try:
        return ui_commit(s, p, message)
    except Exception as exc:  # noqa: BLE001
        if old is not None:
            path.write_text(old, encoding="utf-8", newline="\n")
        raise StudioError(f"not saved: {exc}") from None


def save_values(s, p: str, path, values: dict, message: str) -> str | None:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    write_values(path, values)
    return _commit_or_restore(s, p, path, old, message)


def save_raw(s, p: str, path, new: str, message: str) -> str | None:
    try:
        ok = isinstance(yaml.safe_load(new), dict)
    except yaml.YAMLError as exc:
        raise StudioError(f"not valid YAML: {exc}") from None
    if not ok:
        raise StudioError("not valid: the file must be a mapping (key: value)")
    old = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(new, encoding="utf-8", newline="\n")
    return _commit_or_restore(s, p, path, old, message)


def env_form(s, p: str, f: Files, kind: str, name: str, spec: dict) -> None:
    on_g3 = s.worktree_branch(p) == branch_for(p, "G3")
    holes = required_paths(spec)
    path = f.wt / L.FOLDERS[kind] / f"{name}.yaml"
    if holes:
        st.markdown("**Environment values to fill in** (bucket names, queue URLs, endpoints… for dev)")
        if not on_g3:
            st.caption("They are filled in at stage S3 (Plans).")
            st.dataframe([{"field": ".".join(map(str, k)), "value": v} for k, v in holes], hide_index=True)
            return
        with st.form(f"env-{kind}-{name}"):
            vals = {}
            for keys, v in holes:
                label = ".".join(map(str, keys))
                vals[keys] = st.text_input(label, "", placeholder=v, key=f"env-{kind}-{name}-{label}")
            if st.form_submit_button("Save values", type="primary", disabled=busy(s, p)):
                filled = {k: x.strip() for k, x in vals.items() if x.strip()}
                if filled:
                    act(save_values, s, p, path, filled, f"S3 {p}: environment values for {kind} {name}",
                        ok=lambda sha: f"Saved and committed {sha[:7]}" if sha else "Saved (nothing changed)")
    elif on_g3:
        st.caption("All environment values are filled in.")
    if on_g3:
        with st.expander("Edit the settings file (advanced)"):
            text = f.read(f"{L.FOLDERS[kind]}/{name}.yaml") or ""
            new = st.text_area("YAML", text, height=300, key=f"raw-{kind}-{name}-{hashlib.sha256(text.encode()).hexdigest()[:8]}")
            if st.button("Save file", key=f"rawsave-{kind}-{name}", disabled=busy(s, p),
                         help=BUSY_HELP if busy(s, p) else None) and new != text:
                act(save_raw, s, p, path, new, f"S3 {p}: {kind} {name} settings edited",
                    ok=lambda sha: f"Saved and committed {sha[:7]}" if sha else "Saved (nothing changed)")


# --------------------------------------------------------------------------------------- snippets
def snippet(kind: str, spec: dict) -> str | None:
    t, c, params = spec.get("type"), spec.get("connection") or {}, spec.get("params") or {}
    region = c.get("region") or "eu-north-1"
    if kind == "trigger":
        q = (params.get("queue_url") or "{queue_url}")
        return ("import boto3\n\nsqs = boto3.client('sqs', region_name=%r)\n# how many messages wait, and the DLQ redrive policy\n"
                "print(sqs.get_queue_attributes(QueueUrl=%r, AttributeNames=['ApproximateNumberOfMessages', 'RedrivePolicy']))\n" % (region, q))
    if t == "s3":
        root = (spec.get("extraction") or {}).get("root", "")
        pre = re.split(r"[{*]", root)[0] if kind == "source" else ""
        return ("import boto3\n\ns3 = boto3.client('s3', region_name=%r)\n"
                "resp = s3.list_objects_v2(Bucket=%r, Prefix=%r, MaxKeys=20)\n"
                "for o in resp.get('Contents', []):\n    print(o['Key'], o['Size'])\n" % (region, c.get("bucket"), pre))
    if t == "https":
        return ("import requests\n\nr = requests.get(%r, timeout=60)  # add auth=(user, password) from the secret\n"
                "print(r.status_code, r.text[:500])\n" % (c.get("base_url"),))
    if t == "sftp":
        return ("import paramiko\n\nt = paramiko.Transport((%r, %r))\nt.connect(username='USER', password='…')  # or a key\n"
                "sftp = paramiko.SFTPClient.from_transport(t)\nprint(sftp.listdir('.'))\n" % (c.get("host"), c.get("port", 22)))
    if t == "ftp":
        return "from ftplib import FTP_TLS\n\nftp = FTP_TLS(%r)\nftp.login('USER', '…')\nftp.prot_p()\nprint(ftp.nlst())\n" % (c.get("host"),)
    if t == "local":
        return "import os\n\nfor root, dirs, files in os.walk(%r):\n    print(root, files[:10])\n" % (c.get("path") or ".",)
    return None


def cron_words(cron: str | None) -> str:
    if not cron:
        return "No schedule: runs start by hand, from the Airflow UI or the API."
    s = cron.strip()
    presets = {"@hourly": "every hour", "@daily": "every day at midnight (UTC)", "@weekly": "every week", "@once": "once"}
    if s in presets:
        return f"Runs {presets[s]}."
    m = re.fullmatch(r"\*/(\d+) \* \* \* \*", s)
    if m:
        return f"Runs every {m.group(1)} minutes (at minutes divisible by {m.group(1)}, so the gap at the top of the hour can be shorter)."
    m = re.fullmatch(r"(\d+) \* \* \* \*", s)
    if m:
        return f"Runs every hour at minute {m.group(1)}."
    m = re.fullmatch(r"(\d+) (\d+) \* \* \*", s)
    if m:
        return f"Runs every day at {int(m.group(2)):02d}:{int(m.group(1)):02d} UTC."
    return f"Cron `{s}` (minute hour day-of-month month day-of-week, UTC)."


def functions_of(code: str) -> dict[str, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {}
    lines = code.splitlines()
    return {n.name: "\n".join(lines[n.lineno - 1 - len(n.decorator_list): n.end_lineno]) for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def missing_steps(lock: dict, code: str | None) -> list[str]:
    """Custom steps of the plan that have no function in the code file (not coded yet, or the coder stopped part-way)."""
    ids = [x["id"] for x in ((lock.get("plan") or {}).get("steps") or [])
           if isinstance(x, dict) and x.get("op") in ("custom", "custom_delivery") and x.get("id")]
    if not ids:
        return []
    fns = functions_of(code) if code else {}
    if code and not fns:   # unreadable (syntax error): the S4 status says why
        return []
    return [i for i in ids if i not in fns and f"step_{i}" not in fns]


# --------------------------------------------------------------------------------------- the tab
def render(s, p: str, kind: str, states: dict) -> None:
    f = Files(s, p)
    comps = dict(f.components())
    if kind not in comps:
        if not f.read(f"pipelines/{p}.yaml"):
            available_after("Available after S1 (the requirement is analysed and split).", key=f"aa-{kind}")
        elif not f.yaml(f"pipelines/{p}.lock.yaml").get("split"):
            available_after("Available after the split (S1).", key=f"aa-{kind}")
        elif kind == "transformation":
            st.info("No transformation: files move unchanged.")
        elif kind == "trigger":
            pspec = f.yaml(f"pipelines/{p}.yaml")
            st.info(f"No separate trigger component. {cron_words(pspec.get('schedule'))}")
        else:
            available_after("The component files are written at S3, once the design (G2) is approved.", key=f"aa-{kind}")
        return
    name = comps[kind]
    folder = L.FOLDERS[kind]
    spec = f.yaml(f"{folder}/{name}.yaml")
    lock = f.yaml(f"{folder}/{name}.lock.yaml")
    mode = "requirement" if (spec.get("requirement") or "").strip() else "steps"
    st.caption(f"`{folder}/{name}.yaml` · {mode} mode · showing {f.where()}")
    st.markdown(f"### {kind.capitalize()} `{name}`" + (f" — {spec.get('description')}" if spec.get("description") else ""))
    if kind == "transformation":
        st.markdown("**In plain English**")
        st.markdown(spec.get("requirement") or "(steps written by hand)")
    if kind == "trigger":
        st.markdown(f"**How runs start:** {cron_words(spec.get('schedule'))}")
    t_set, t_plan, t_code = st.tabs(["Settings", "Plan" if mode == "requirement" else "Steps", "Code"])
    with t_set:
        shown = {k: v for k, v in spec.items() if k not in ("requirement", "examples", "steps", "api_version", "kind", "name")}
        st.code(yaml.safe_dump(shown, sort_keys=False, allow_unicode=True), language="yaml")
        env_form(s, p, f, kind, name, spec)
        if kind == "destination":
            st.markdown("**Delivery policy**")
            st.code(yaml.safe_dump(spec.get("delivery") or {}, sort_keys=False), language="yaml")
            steps = (lock.get("plan") or {}).get("steps") or spec.get("steps") or []
            if any(x.get("op") == "custom_delivery" for x in steps):
                st.warning("Custom delivery: the AI-written code copies server-side, may delete the source (a move) and sets tags. "
                           "See the Code tab and the capability summary at G4.")
        if kind == "trigger":
            st.markdown("**AWS pieces it needs**")
            man = f.json(f"infra/{p}/manifest.json")
            steps = (lock.get("plan") or {}).get("steps") or spec.get("steps") or []
            if man:
                facts = man.get("facts") or {}
                q = facts.get("queue")
                if q:
                    st.markdown(f"- SQS queue `{q['name']}` ({'created by the Studio' if q['managed'] else 'existing'}), visibility "
                                f"{q['visibility_timeout_seconds']} s, dead-letter queue after {q['max_receive_count']} receives (about 24 h)")
                for n in facts.get("notifications") or []:
                    st.markdown(f"- S3 event notification on `{n['bucket']}` (prefix `{n['prefix']}`, suffix `{n['suffix']}`) → the queue")
                if not q:
                    st.markdown(f"- {man.get('note', 'none')}")
                mt = f.read(f"infra/{p}/module/main.tf")
                if mt:
                    with st.expander("Terraform module"):
                        code_view(mt, f"infra/{p}/module/main.tf", key=f"tf-{p}")
            elif steps and steps[0].get("op") == "sqs":
                st.markdown("- an SQS queue with a dead-letter queue, a queue policy for S3, an S3 event notification (rendered at S5)")
            else:
                st.markdown("- none: a schedule needs no AWS resources")
    with t_plan:
        if mode == "requirement":
            if spec.get("requirement"):
                with st.expander("Requirement for this component", expanded=False):
                    st.markdown(spec["requirement"])
            plan_cards(lock)
        else:
            plan_cards({"plan": {"steps": spec.get("steps") or []}})
    with t_code:
        code = f.read(f"{folder}/{name}.py")
        uncoded = missing_steps(lock, code)
        if uncoded:
            c1, c2 = st.columns([4, 1])
            c1.warning("Custom steps without code yet: " + ", ".join(f"`{x}`" for x in uncoded)
                       + ". Workflow → S4 status → Regenerate (AI) writes them.")
            c2.button("Open Workflow", key=f"uncoded-{kind}", on_click=go_to, args=("Workflow",))
        if code:
            if kind == "transformation":
                fns = functions_of(code)
                for step in (lock.get("plan") or {}).get("steps") or []:
                    body = fns.get(step["id"]) or fns.get(f"step_{step['id']}")
                    if body:
                        with st.container(border=True):
                            st.markdown(f"**{step.get('title') or step['id']}** · `{step['op']}`")
                            st.code(body, language="python", line_numbers=True)
            code_view(code, f"{folder}/{name}.py", key=f"code-{kind}", abs_path=(f.wt / folder / f"{name}.py") if f.live else None)
            tests = (lock.get("code") or {}).get("tests") or []
            if tests:
                st.dataframe([{"test": t.get("example") or t.get("case"), "status": t.get("status"), "detail": (t.get("detail") or "")[:200]}
                              for t in tests], hide_index=True)
        else:
            st.caption("No generated code (built-in operations only, or not coded yet — S4).")
        snip = snippet(kind, spec)
        if snip:
            st.markdown("**Connectivity snippet** (to try the connection yourself)")
            st.code(snip, language="python")
