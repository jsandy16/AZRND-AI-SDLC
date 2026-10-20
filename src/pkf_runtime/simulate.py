"""Run component steps on examples without any real system: in-memory messages, a fake source listing, placeholder
file contents. Used to check AI plans, generated code and whole pipelines before anyone approves them (and by the
isolated test runner, so it takes plain dicts only).

Every function returns {"status": "pass"|"fail"|"needs_code"|"shown", "detail": str, "output": ...}.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .discovery import FakeSource, SourceView, initial_groups, normalize as norm_groups, run_source_steps
from .events import RECEIVE_OPS, messages_to_events, normalize as norm_events, run_event_steps
from .layout import run_layout_steps
from .ops import run_step
from .package import Package, PackageError, StepPackage, render

SAMPLE_VARS = {"pipeline": "example_pipeline", "source": "example_source", "destination": "example_destination",
               "connection": "example_connection", "trigger": "example_trigger", "run_id": "example_run",
               "ds": "2024-03-01", "ds_nodash": "20240301", "ts_nodash": "20240301T020000", "env": "dev"}
NOW = 1_700_000_000.0


class NeedsCode(Exception):
    pass


def _custom_or_stop(steps: list[dict], custom: dict | None) -> None:
    for s in steps:
        if s["op"] in ("custom", "custom_delivery") and s["params"].get("function") not in (custom or {}):
            raise NeedsCode(s["id"])


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _verdict(expect_error: bool, failure: str | None, got: Any, want: Any, compare) -> dict:
    if expect_error:
        return {"status": "pass" if failure else "fail", "detail": failure or "expected a refusal, but it passed", "output": got}
    if failure:
        return {"status": "fail", "detail": failure, "output": got}
    if want is None:
        return {"status": "shown", "detail": "no expected output to compare", "output": got}
    ok, why = compare(got, want)
    return {"status": "pass" if ok else "fail", "detail": "matches the expected output" if ok else why, "output": got}


LISTING_ITEM = re.compile(r"(.*?)(?::(\d+))?(?::(\d+(?:\.\d+)?))?$", re.S)
"""A listing entry 'path[:size[:age_seconds]]', parsed from the right: S3 keys may contain ':' themselves."""


def parse_listing_item(item: str) -> tuple[str, int | None, float | None]:
    m = LISTING_ITEM.match(item)
    return m.group(1), (int(m.group(2)) if m.group(2) else None), (float(m.group(3)) if m.group(3) else None)


def _listing(items: list[str]) -> dict[str, tuple[int, float]]:
    out = {}
    for it in items:
        path, size, age = parse_listing_item(it)
        out[path] = (64 if size is None else size, 3600.0 if age is None else age)
    return out


def _messages(example: dict) -> list[dict]:
    return [{"id": f"m{i}", "body": m if isinstance(m, str) else json.dumps(m), "attributes": {}} for i, m in enumerate(example.get("messages") or [])]


def example_vars(params: dict, example: dict, names: dict | None = None) -> dict:
    return {**SAMPLE_VARS, **(names or {}), **(params or {}), **(example.get("params") or {})}


# ---------------------------------------------------------------------------------------------- trigger
def trigger_events(steps: list[dict], custom: dict | None, example: dict, params: dict, names: dict | None = None) -> list[dict]:
    _custom_or_stop(steps, custom)
    recv = steps[0]["op"] if steps and steps[0]["op"] in RECEIVE_OPS else "sqs"
    v = example_vars(params, example, names)
    if recv == "schedule":
        msgs = [{"id": v["run_id"], "body": "", "attributes": {}}]
    elif recv == "run_conf":
        items = (example.get("conf") or {}).get(steps[0]["params"].get("key", "events")) or []
        msgs = [{"id": f"conf-{i}", "body": it if isinstance(it, str) else json.dumps(it), "attributes": {}} for i, it in enumerate(items)]
    else:
        msgs = _messages(example)
    return run_event_steps(messages_to_events(msgs), steps[1:] if steps and steps[0]["op"] in RECEIVE_OPS else steps,
                           {"params": v, "run_id": v["run_id"]}, custom)


def _cmp_events(got, want):
    if len(got) != len(want):
        return False, f"{len(got)} event(s), expected {len(want)}"
    for i, (g, w) in enumerate(zip(got, want)):
        if sorted(w.get("keys", [])) != g["keys"]:
            return False, f"event {i}: keys {g['keys']}, expected {sorted(w.get('keys', []))}"
        bad = {k: (g["vars"].get(k), x) for k, x in (w.get("vars") or {}).items() if g["vars"].get(k) != x}
        if bad:
            return False, f"event {i}: variables differ (got, expected): {bad}"
    return True, ""


def simulate_trigger(steps: list[dict], custom: dict | None, example: dict, params: dict) -> dict:
    try:
        events = trigger_events(steps, custom, example, params)
    except NeedsCode as exc:
        return {"status": "needs_code", "detail": f"stops at custom step '{exc}' until its code exists", "output": None}
    except Exception as exc:  # noqa: BLE001 - shown to the reviewer
        return _verdict(example.get("expect_error", False), _error(exc), None, example.get("expected_events"), _cmp_events)
    errs = [e["error"] for e in events if e.get("error")]
    return _verdict(example.get("expect_error", False), "; ".join(errs) or None, norm_events(events), example.get("expected_events"), _cmp_events)


# ---------------------------------------------------------------------------------------------- source
def _event(example: dict) -> dict:
    ev = example.get("event")
    if not ev:
        return {"id": "example_event", "message_ids": [], "objects": [], "vars": {}}
    objs = [{"bucket": o.get("bucket"), "key": o["key"], "size": o.get("size"), "time": None, "sequencer": None} for o in ev.get("objects", [])]
    return {"id": "example_event", "message_ids": ["m0"], "objects": objs, "vars": dict(ev.get("vars") or {})}


def source_groups(steps: list[dict], custom: dict | None, example: dict, params: dict, event: dict | None = None,
                  source_bucket: str | None = None, names: dict | None = None) -> list[dict]:
    _custom_or_stop(steps, custom)
    e = event or _event(example)
    v = {**example_vars(params, example, names), **e.get("vars", {})}
    src = SourceView(FakeSource(_listing(example.get("listing") or []), now=NOW, contents=example.get("contents") or {}))
    ctx = {"params": v, "run_id": v["run_id"], "now": NOW, "source_bucket": source_bucket}
    return run_source_steps(initial_groups([e]), steps, ctx, src, custom)


def _cmp_lists(got, want):
    w = sorted(sorted(x) for x in want)
    return (got == w), f"got {got}, expected {w}"


def rejected_failures(groups: list[dict]) -> list[str]:
    """Files the source steps refused (K13 `group["rejected"]`: a bad path, a duplicate package path), one failure each:
    "file <path>: <error>". The runtime delivers the other files and fails the run; examples fail the same way."""
    out = []
    for g in groups:
        for r in g.get("rejected") or []:
            out.append(f"file {r.get('path') or r.get('key')}: {r.get('error')}")
    return out


def simulate_source(steps: list[dict], custom: dict | None, example: dict, params: dict) -> dict:
    try:
        groups = source_groups(steps, custom, example, params)
    except NeedsCode as exc:
        return {"status": "needs_code", "detail": f"stops at custom step '{exc}' until its code exists", "output": None}
    except Exception as exc:  # noqa: BLE001
        return _verdict(example.get("expect_error", False), _error(exc), None, example.get("expected_packages"), _cmp_lists)
    out = [g for g in norm_groups(groups) if g]
    return _verdict(example.get("expect_error", False), "; ".join(rejected_failures(groups)) or None, out,
                    example.get("expected_packages"), _cmp_lists)


# ---------------------------------------------------------------------------------------------- destination
def _cmp_keys(got, want):
    g, w = sorted(got), sorted(want)
    missing, extra = sorted(set(w) - set(g)), sorted(set(g) - set(w))
    return (not missing and not extra), f"missing {missing}, unexpected {extra}"


def destination_keys(steps: list[dict], custom: dict | None, files: list[str], variables: dict) -> list[str]:
    _custom_or_stop(steps, custom)
    keys = run_layout_steps(files, variables, steps, {"params": variables, "run_id": variables.get("run_id")}, custom)
    return sorted(k for k in keys.values() if k is not None)


def simulate_destination(steps: list[dict], custom: dict | None, example: dict, params: dict) -> dict:
    v = {**example_vars(params, example), "package": "example_package", **(example.get("vars") or {})}
    try:
        keys = destination_keys(steps, custom, example["files"], v)
    except NeedsCode as exc:
        return {"status": "needs_code", "detail": f"stops at custom step '{exc}' until its code exists", "output": None}
    except Exception as exc:  # noqa: BLE001
        return _verdict(example.get("expect_error", False), _error(exc), None, example.get("expected_keys"), _cmp_keys)
    return _verdict(example.get("expect_error", False), None, keys, example.get("expected_keys"), _cmp_keys)


# ---------------------------------------------------------------------------------------------- transformation
def build_package(workdir: Path, inputs: list[str], variables: dict) -> Package:
    pkg = Package(workdir, variables.get("package", "example_package"), variables)
    for item in inputs:
        m = re.fullmatch(r"(.*):(\d+)", item)
        path, size = (m.group(1), int(m.group(2))) if m else (item, 64)
        seed = (path + "\n").encode()
        pkg.write_bytes(path, (seed * (size // len(seed) + 1))[:size])
    return pkg


def transform_package(pkg: Package, steps: list[dict], custom: dict | None) -> str | None:
    """Run the steps; the refusal message, or None."""
    _custom_or_stop(steps, custom)
    ctx = {"params": dict(pkg.vars), "run_id": "example_run"}
    for s in steps:
        try:
            run_step(pkg, s["op"], s["params"], ctx, custom)
        except PackageError as exc:
            return f"{s['id']}: {exc}"
    return None


def _cmp_paths(got, want):
    missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
    return (not missing and not extra), f"missing {missing}, unexpected {extra}"


def simulate_transformation(steps: list[dict], custom: dict | None, example: dict, params: dict) -> dict:
    work = Path(tempfile.mkdtemp(prefix="pkf_sim_"))
    try:
        pkg = build_package(work, example["input"], example_vars(params, example))
        try:
            refused = transform_package(pkg, steps, custom)
        except NeedsCode as exc:
            return {"status": "needs_code", "detail": f"stops at custom step '{exc}' until its code exists", "output": pkg.describe()}
        except Exception as exc:  # noqa: BLE001
            refused = _error(exc)
        res = _verdict(example.get("expect_error", False), refused, pkg.describe(), example.get("expected_output"), _cmp_paths)
        res["package_name"] = pkg.name
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


SIMULATORS = {"trigger": simulate_trigger, "source": simulate_source, "destination": simulate_destination,
              "transformation": simulate_transformation}


# ---------------------------------------------------------------------------------------------- whole pipeline
def _delivers_itself(steps: list[dict]) -> bool:
    return bool(steps) and steps[-1].get("op") == "custom_delivery"


def simulate_pipeline(parts: dict, example: dict) -> dict:
    """parts: {kind: {"steps", "custom", "params"}} plus "package_name", "source_bucket". Trigger → source →
    transformation → destination, exactly in the order the DAG runs them."""
    params = {}
    for k in ("source", "trigger", "transformation", "destination"):
        params.update((parts.get(k) or {}).get("params") or {})
    params.update(parts.get("pipeline_params") or {})
    names = parts.get("names") or {}
    failures, keys = [], []
    try:
        events = trigger_events(parts["trigger"]["steps"], parts["trigger"].get("custom"), example, params, names)
    except NeedsCode as exc:
        return {"status": "needs_code", "detail": f"trigger step '{exc}' has no code yet", "output": None}
    except Exception as exc:  # noqa: BLE001
        return _verdict(example.get("expect_error", False), f"trigger: {_error(exc)}", None, example.get("expected_keys"), _cmp_keys)
    failures += [f"event {e['id']}: {e['error']}" for e in events if e.get("error")]
    for e in [e for e in events if not e.get("error")]:
        try:
            groups = source_groups(parts["source"]["steps"], parts["source"].get("custom"), example, params, event=e,
                                   source_bucket=parts.get("source_bucket"), names=names)
        except NeedsCode as exc:
            return {"status": "needs_code", "detail": f"source step '{exc}' has no code yet", "output": None}
        except Exception as exc:  # noqa: BLE001
            failures.append(f"event {e['id']}: source: {_error(exc)}")
            continue
        failures += rejected_failures(groups)
        t = parts.get("transformation") or {"steps": [], "custom": {}}
        d = parts["destination"]
        paths_only = _delivers_itself(d["steps"]) and not t.get("steps")
        for g in groups:
            if not g["files"]:
                continue
            v = {**example_vars(params, example, names), **e.get("vars", {}), **g.get("vars", {})}
            try:
                v["package"] = render(parts.get("package_name", "{source}_{ds_nodash}"), v)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"package name: {_error(exc)}")
                continue
            if paths_only:  # custom_delivery copies server-side: only the paths matter, no package files are built (B16)
                try:
                    keys += destination_keys(d["steps"], d.get("custom"), sorted(f["path"] for f in g["files"]), v)
                except NeedsCode as exc:
                    return {"status": "needs_code", "detail": f"destination step '{exc}' has no code yet", "output": None}
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"package: {_error(exc)}")
                continue
            work = Path(tempfile.mkdtemp(prefix="pkf_sim_"))
            try:
                pkg = build_package(work, [f"{f['path']}:{f.get('size') or 64}" for f in g["files"]], v)
                try:
                    refused = transform_package(pkg, t["steps"], t.get("custom"))
                except NeedsCode as exc:
                    return {"status": "needs_code", "detail": f"transformation step '{exc}' has no code yet", "output": None}
                if refused:
                    failures.append(f"package {pkg.name}: {refused}")
                    continue
                keys += destination_keys(d["steps"], d.get("custom"), pkg.files(), {**pkg.vars, "package": pkg.name})
            except NeedsCode as exc:
                return {"status": "needs_code", "detail": f"destination step '{exc}' has no code yet", "output": None}
            except Exception as exc:  # noqa: BLE001
                failures.append(f"package: {_error(exc)}")
            finally:
                shutil.rmtree(work, ignore_errors=True)
    return _verdict(example.get("expect_error", False), "; ".join(failures) or None, sorted(keys), example.get("expected_keys"), _cmp_keys)


# ---------------------------------------------------------------------------------------------- custom delivery
FAILABLE = ("CopyObject", "UploadPartCopy", "CreateMultipartUpload", "HeadObject", "DeleteObject", "PutObjectTagging",
            "GetObjectTagging", "PutObject", "GetObject")
FAIL_CODES = {"AccessDenied": 403, "SlowDown": 503, "InternalError": 500, "PreconditionFailed": 412, "NoSuchKey": 404,
              "ExpiredToken": 400}
"""Error codes a case's `fail_calls` entry may inject (default AccessDenied), with the HTTP status S3 answers with."""


def _cmp_delivery(got, want):
    if not any(want.get(k) is not None for k in ("objects", "tags", "statuses")):
        return False, "the case expects nothing: give the objects after delivery, tags or statuses"
    if "objects" in want and sorted(want["objects"]) != got["objects"]:
        missing, extra = sorted(set(want["objects"]) - set(got["objects"])), sorted(set(got["objects"]) - set(want["objects"]))
        return False, f"objects after delivery: missing {missing}, unexpected {extra}"
    for k, tags in (want.get("tags") or {}).items():
        if got["tags"].get(k) != tags:
            return False, f"tags of {k}: got {got['tags'].get(k)}, expected {tags}"
    for path, st in (want.get("statuses") or {}).items():
        if got["statuses"].get(path) != st:
            return False, f"status of {path}: got {got['statuses'].get(path)!r}, expected {st!r}"
    return True, ""


def _copy_source(src) -> tuple[str, str]:
    """CopyObject's CopySource as (bucket, key): a dict, or the 'bucket/key[?versionId=…]' string botocore builds."""
    from urllib.parse import unquote

    if isinstance(src, dict):
        return src["Bucket"], src["Key"]
    text = unquote(str(src).split("?versionId=", 1)[0]).lstrip("/")
    bucket, key = text.split("/", 1)
    return bucket, key


def _injector(fail: dict):
    """A before-parameter-build handler that makes one S3 operation fail (optionally for one key, `times` times)."""
    from botocore.exceptions import ClientError

    op, key, code, times = fail.get("operation"), fail.get("key"), fail.get("code") or "AccessDenied", fail.get("times")
    if op not in FAILABLE:
        raise ValueError(f"malformed test case: fail_calls operation must be one of {FAILABLE}")
    if code not in FAIL_CODES:
        raise ValueError(f"malformed test case: fail_calls code must be one of {sorted(FAIL_CODES)}")
    if times is not None and (isinstance(times, bool) or not isinstance(times, int) or times < 0):
        raise ValueError("malformed test case: fail_calls times must be a whole number or null (always)")
    left = {"n": times}

    def boom(params, **kw):
        if key not in (None, params.get("Key")):
            return
        if left["n"] is not None:
            if left["n"] <= 0:
                return
            left["n"] -= 1
        raise ClientError({"Error": {"Code": code, "Message": f"injected failure of {op}"},
                           "ResponseMetadata": {"HTTPStatusCode": FAIL_CODES[code]}}, op)
    return op, boom


def _tag_fidelity(s3, clean, notes: list[str]) -> None:
    """Real S3 replaces an object on copy, tags included (TaggingDirective COPY: the source's tags); moto keeps the
    target's old tags. After a successful CopyObject on the function's client, set the target's tags like S3 would,
    through the clean client only, so an injected PutObjectTagging failure never fires inside the copy."""
    stash: list[dict] = []

    def before(params, context=None, **kw):
        p = {k: params.get(k) for k in ("Bucket", "Key", "CopySource", "TaggingDirective")}
        if isinstance(context, dict):
            context["pkf_copy"] = p
        else:
            stash.append(p)

    def after(http_response=None, parsed=None, model=None, context=None, **kw):
        p = context.pop("pkf_copy", None) if isinstance(context, dict) else None
        if p is None and stash:
            p = stash.pop()
        try:
            if p is None or getattr(http_response, "status_code", 200) >= 300 or p.get("TaggingDirective") == "REPLACE":
                return
            sb, sk = _copy_source(p["CopySource"])
            ts = clean.get_object_tagging(Bucket=sb, Key=sk).get("TagSet", [])
            if ts:
                clean.put_object_tagging(Bucket=p["Bucket"], Key=p["Key"], Tagging={"TagSet": ts})
            else:
                clean.delete_object_tagging(Bucket=p["Bucket"], Key=p["Key"])
        except Exception as exc:  # noqa: BLE001 - the harness must never change the case's outcome
            notes.append(f"harness: {_error(exc)}")

    s3.meta.events.register("before-parameter-build.s3.CopyObject", before)
    s3.meta.events.register("after-call.s3.CopyObject", after)


def run_delivery_case(fn, case: dict, params: dict, ctx: dict, if_exists: str = "overwrite") -> tuple[Any, str | None]:
    """A custom_delivery case against an in-memory S3 (moto): objects before → the framework's precheck → the function
    → the framework's verify → objects, tags and statuses after, exactly like the runtime's delivery task.
    `fail_calls` makes chosen S3 calls of the function fail (code, key, how often); the framework's own calls use a
    clean client without injected failures."""
    import os
    from urllib.parse import urlencode

    from .delivery import Aws, precheck, run_custom_delivery, verify

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
        from moto import mock_aws
    except ImportError:
        return None, "custom delivery tests need boto3 and moto (pip install 'package-factory[agent]')"
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        os.environ.setdefault(k, v)
    inp = case.get("input") or {}
    objects = inp.get("objects") or {}
    transfers = inp.get("transfers") or []
    notes: list[str] = []
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        clean = boto3.client("s3", region_name="us-east-1")  # the framework's client, without the injected failures
        buckets = {k.split("/", 1)[0] for k in objects} | {t.get(b) for t in transfers for b in ("source_bucket", "target_bucket")}
        try:
            for b in sorted(x for x in buckets if x):
                clean.create_bucket(Bucket=b)
            for full, body in objects.items():
                b, k = full.split("/", 1)
                tags = body.get("tags") if isinstance(body, dict) else None
                data = (body.get("body", "x") if isinstance(body, dict) else str(body)).encode()
                clean.put_object(Bucket=b, Key=k, Body=data, **({"Tagging": urlencode(tags)} if tags else {}))
        except Exception as exc:  # noqa: BLE001 - a malformed case (e.g. a bucket name shorter than 3 characters)
            raise ValueError(f"malformed test case (objects must be 'bucket/key' with valid bucket names): {_error(exc)}") from None
        _tag_fidelity(s3, clean, notes)  # registered first: an injected CopyObject failure then leaves nothing behind
        for f in inp.get("fail_calls") or []:
            op, boom = _injector(f)
            s3.meta.events.register(f"before-parameter-build.s3.{op}", boom)
        aws = Aws({}, {}, {}, {}, clients={("s3", "source"): s3, ("s3", "destination"): s3})
        framework = Aws({}, {}, {}, {}, clients={("s3", "source"): clean, ("s3", "destination"): clean})
        try:
            todo, decided = precheck(transfers, framework, if_exists)
            results = decided + verify(run_custom_delivery(fn, case.get("name", "case"), todo, params, ctx, aws), todo, framework)
        except PackageError as exc:
            return None, "; ".join([str(exc), *notes])
        except (ClientError, BotoCoreError) as exc:
            return None, "; ".join([f"framework check: {_error(exc)}", *notes])
        after, tags = [], {}
        for b in sorted(x for x in buckets if x):
            for page in clean.get_paginator("list_objects_v2").paginate(Bucket=b):
                for o in page.get("Contents", []):
                    full = f"{b}/{o['Key']}"
                    after.append(full)
                    ts = clean.get_object_tagging(Bucket=b, Key=o["Key"]).get("TagSet", [])
                    tags[full] = {t["Key"]: t["Value"] for t in ts}
    got = {"objects": sorted(after), "tags": tags, "statuses": {r["path"]: r["status"] for r in results}}
    if notes:
        got["harness"] = notes
    return got, None


# ---------------------------------------------------------------------------------------------- one custom step
def run_step_case(kind: str, fn, case: dict, op: str = "custom", *, delivery: dict | None = None) -> dict:
    """A test-writer case for one custom function: {"name", "input", "expected" | "expect_error"}.
    For a custom_delivery step `delivery` is the destination's delivery settings, passed as ctx["delivery"] exactly as
    the runtime passes them (None for old callers: ctx["delivery"] = {} and if_exists overwrite)."""
    inp, params = case.get("input") or {}, {**SAMPLE_VARS, **(case.get("params") or {})}
    ctx = {"params": params, "run_id": "example_run", "now": NOW}
    if op == "custom_delivery":
        ctx["delivery"] = delivery if delivery is not None else {}
        if_exists = "overwrite" if delivery is None else delivery.get("if_exists", "skip_identical")
        try:
            got, failure = run_delivery_case(fn, case, dict(params), ctx, if_exists=if_exists)
        except ValueError as exc:  # a broken case never passes, not even one that expects an error
            return {"case": case.get("name"), "status": "fail", "detail": str(exc), "output": None}
        res = {"case": case.get("name"), **_verdict(case.get("expect_error", False), failure, got, case.get("expected"), _cmp_delivery)}
        if got and got.get("harness"):
            res["detail"] = "; ".join([res["detail"], *got["harness"]])
        return res
    try:
        if kind == "trigger":
            events = fn(json.loads(json.dumps(inp.get("events", []))), dict(params), ctx)
            got = norm_events(events)
            cmp = _cmp_events
        elif kind == "source":
            src = SourceView(FakeSource(_listing(inp.get("listing") or []), now=NOW, contents=inp.get("contents") or {}))
            groups = [{"id": f"g{i}", "event_id": "e", "message_ids": [], "vars": g.get("vars", {}), "objects": g.get("objects", []),
                       "files": [{"root": g.get("root", ""), "path": p, "size": 64, "mtime": NOW - 3600} for p in g.get("files", [])]}
                      for i, g in enumerate(inp.get("groups", []))]
            got = [sorted(f["path"] for f in g["files"]) for g in fn(groups, dict(params), ctx, src)]
            got = sorted(got)
            cmp = _cmp_lists
        elif kind == "destination":
            layout = {"files": sorted(inp.get("files", [])), "keys": dict(inp.get("keys") or {}), "vars": {**params, **(inp.get("vars") or {})}}
            out = fn(layout, dict(params), ctx)
            got = sorted(k for k in out["keys"].values() if k is not None)
            cmp = _cmp_keys
        else:
            work = Path(tempfile.mkdtemp(prefix="pkf_case_"))
            try:
                pkg = build_package(work, inp.get("files", []), params)
                fn(StepPackage(pkg), dict(params), ctx)
                got = pkg.describe()
            finally:
                shutil.rmtree(work, ignore_errors=True)
            cmp = _cmp_paths
        failure = None
    except Exception as exc:  # noqa: BLE001
        got, failure, cmp = None, _error(exc), (lambda g, w: (False, ""))
    return {"case": case.get("name"), **_verdict(case.get("expect_error", False), failure, got, case.get("expected"), cmp)}


# ---------------------------------------------------------------------------------------------- case shape (plan time)
BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
TRANSFER_KEYS = ("path", "source_bucket", "source_key", "target_bucket", "target_key")


def _str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _bucket_problem(name, where: str) -> str | None:
    if not isinstance(name, str) or not BUCKET_NAME.fullmatch(name) or ".." in name:
        return f"{where}: bucket name {name!r} is not a valid S3 bucket name (3-63 lowercase letters, digits, '.' or '-')"
    return None


def _delivery_case_problems(case: dict) -> list[str]:
    out: list[str] = []
    inp = case.get("input")
    if not isinstance(inp, dict):
        return ['input must be an object {"objects": …, "transfers": […]}']
    objects, transfers = inp.get("objects") or {}, inp.get("transfers")
    if not isinstance(objects, dict):
        out.append('input.objects must be an object {"<bucket>/<key>": "<text>" or {"body", "tags"}}')
        objects = {}
    for full, body in objects.items():
        if not isinstance(full, str) or "/" not in full or not full.split("/", 1)[1]:
            out.append(f"input.objects: {full!r} must be '<bucket>/<key>'")
            continue
        out += [p for p in [_bucket_problem(full.split("/", 1)[0], f"input.objects {full!r}")] if p]
        if isinstance(body, dict):
            if set(body) - {"body", "tags"} or not isinstance(body.get("tags") or {}, dict):
                out.append(f"input.objects {full!r}: an object is \"<text>\" or {{\"body\": \"<text>\", \"tags\": {{k: v}}}}")
        elif not isinstance(body, str):
            out.append(f"input.objects {full!r}: the value must be text or {{\"body\", \"tags\"}}")
    if not isinstance(transfers, list):
        out.append("input.transfers must be a list of transfers")
        transfers = []
    paths = []
    for i, t in enumerate(transfers):
        if not isinstance(t, dict):
            out.append(f"input.transfers[{i}] must be an object")
            continue
        missing = [k for k in TRANSFER_KEYS if not isinstance(t.get(k), str) or not t.get(k)]
        if missing:
            out.append(f"input.transfers[{i}] lacks {missing} (every transfer has {list(TRANSFER_KEYS)})")
        for b in ("source_bucket", "target_bucket"):
            if isinstance(t.get(b), str) and t.get(b):
                out += [p for p in [_bucket_problem(t[b], f"input.transfers[{i}].{b}")] if p]
        paths.append(t.get("path"))
    fails = inp.get("fail_calls") or []
    if not isinstance(fails, list):
        out.append("input.fail_calls must be a list")
        fails = []
    for i, f in enumerate(fails):
        if not isinstance(f, dict) or f.get("operation") not in FAILABLE:
            out.append(f"input.fail_calls[{i}]: operation must be one of {list(FAILABLE)}")
            continue
        if (f.get("code") or "AccessDenied") not in FAIL_CODES:
            out.append(f"input.fail_calls[{i}]: code must be one of {sorted(FAIL_CODES)}")
        times = f.get("times")
        if times is not None and (isinstance(times, bool) or not isinstance(times, int) or times < 0):
            out.append(f"input.fail_calls[{i}]: times must be a whole number or null (always)")
    if not case.get("expect_error"):
        want = case.get("expected")
        if not isinstance(want, dict) or not any(want.get(k) is not None for k in ("objects", "tags", "statuses")):
            out.append("expected must name the objects after delivery, their tags or the statuses")
        else:
            if want.get("objects") is not None and not _str_list(want["objects"]):
                out.append('expected.objects must be a list of "<bucket>/<key>"')
            if want.get("tags") is not None and not (isinstance(want["tags"], dict) and all(isinstance(v, dict) for v in want["tags"].values())):
                out.append('expected.tags must be {"<bucket>/<key>": {tag: value}}')
            st = want.get("statuses")
            if st is not None:
                if not isinstance(st, dict):
                    out.append("expected.statuses must be {transfer path: status}")
                else:
                    unknown = [k for k in st if k not in paths]
                    if unknown:
                        out.append(f"expected.statuses names {unknown}, which are not transfer paths {paths}")
    if not out:
        out += _moto_setup_problems(objects, transfers)
    return out


def _moto_setup_problems(objects: dict, transfers: list) -> list[str]:
    """Set the case's objects up in an in-memory S3, without calling any function: what would stop the case at S4."""
    import os
    from urllib.parse import urlencode

    try:
        import boto3
        from moto import mock_aws
    except ImportError:
        return []
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        os.environ.setdefault(k, v)
    try:
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            buckets = {k.split("/", 1)[0] for k in objects} | {t.get(b) for t in transfers for b in ("source_bucket", "target_bucket")}
            for b in sorted(x for x in buckets if x):
                s3.create_bucket(Bucket=b)
            for full, body in objects.items():
                b, k = full.split("/", 1)
                tags = body.get("tags") if isinstance(body, dict) else None
                data = (body.get("body", "x") if isinstance(body, dict) else str(body)).encode()
                s3.put_object(Bucket=b, Key=k, Body=data, **({"Tagging": urlencode(tags)} if tags else {}))
    except Exception as exc:  # noqa: BLE001
        return [f"the objects cannot be set up in S3: {_error(exc)}"]
    return []


def validate_case(kind: str, op: str, case: dict) -> list[str]:
    """Problems with the SHAPE of one test-writer case (empty = well formed), checked at plan time so a case that can
    never pass is repaired before G3 instead of failing every coder attempt at S4."""
    if not isinstance(case, dict):
        return ["a case must be an object"]
    out: list[str] = []
    if not isinstance(case.get("params") or {}, dict):
        out.append("params must be an object of run variables")
    if op == "custom_delivery":
        return out + _delivery_case_problems(case)
    inp, want, err = case.get("input"), case.get("expected"), bool(case.get("expect_error"))
    if not isinstance(inp, dict):
        return out + ["input must be an object in the case format"]
    if kind == "destination":
        if not _str_list(inp.get("files")):
            out.append("input.files must be a list of package paths")
        if not isinstance(inp.get("keys") or {}, dict) or not isinstance(inp.get("vars") or {}, dict):
            out.append("input.keys and input.vars must be objects")
        if not err and not _str_list(want):
            out.append("expected must be the list of delivered keys")
    elif kind == "source":
        groups = inp.get("groups")
        if not isinstance(groups, list) or not all(isinstance(g, dict) and _str_list(g.get("files", [])) for g in groups):
            out.append('input.groups must be a list of {"files": [paths], …}')
        listing = inp.get("listing") or []
        if not _str_list(listing):
            out.append('input.listing must be a list of "path[:size[:age_seconds]]"')
        else:
            bad = [x for x in listing if not parse_listing_item(x)[0].strip("/")]
            if bad:
                out.append(f'input.listing entries {bad[:3]} are not "path[:size[:age_seconds]]"')
        if not isinstance(inp.get("contents") or {}, dict):
            out.append("input.contents must be an object {path: text}")
        if not err and not (isinstance(want, list) and all(_str_list(x) for x in want)):
            out.append("expected must be a list of package path lists")
    elif kind == "trigger":
        if not isinstance(inp.get("events"), list):
            out.append("input.events must be a list of events")
        if not err and not (isinstance(want, list) and all(
                isinstance(x, dict) and _str_list(x.get("keys", [])) and isinstance(x.get("vars") or {}, dict) for x in want)):
            out.append('expected must be a list of {"keys": [object keys], "vars": {…}}')
    elif kind == "transformation":
        if not _str_list(inp.get("files")):
            out.append('input.files must be a list of "path[:size]"')
        if not err and not _str_list(want):
            out.append("expected must be the list of output paths")
    return out


# ---------------------------------------------------------------------------------------------- state before a step
def state_before(kind: str, steps: list[dict], i: int, custom: dict | None, example: dict, params: dict) -> Any:
    """What one example looks like when it reaches step i, running the steps before it (with the custom functions
    written so far). Shown to the coder of step i."""
    custom = custom or {}
    try:
        if kind == "transformation":
            r = simulate_transformation(steps[:i], custom, {**example, "expected_output": None, "expect_error": False}, params)
            return r.get("output")
        if kind == "trigger":
            return trigger_events(steps[:i], custom, example, params)
        if kind == "source":
            return source_groups(steps[:i], custom, example, params)
        if steps[i]["op"] == "custom_delivery":
            try:
                v = {**example_vars(params, example), "package": "example_package", **(example.get("vars") or {})}
                keys, after = _layout_with_vars(example.get("files") or [], v, steps[:i], {"params": v, "run_id": v.get("run_id")}, custom)
                state = {"transfers (source bucket/key shown as placeholders)": [
                    {"path": f, "source_bucket": "<source bucket>", "source_key": f"<source root>/{f}",
                     "target_bucket": "<destination bucket>", "target_key": k} for f, k in keys.items() if k is not None]}
                state["params added by the earlier steps"] = {k: str(x) for k, x in after.items()
                                                              if k not in v or str(v[k]) != str(x)}
                return state
            except Exception as exc:  # noqa: BLE001 - earlier custom steps have no code yet
                return {"files": example.get("files"), "vars": example.get("vars"), "note": f"keys not computable yet: {exc}"}
        return {"files": example.get("files"), "vars": example.get("vars")}
    except Exception as exc:  # noqa: BLE001
        return f"(refused before this step: {exc})"


def _layout_with_vars(files: list[str], variables: dict, steps: list[dict], ctx: dict, custom: dict) -> tuple[dict, dict]:
    """(keys, layout vars after the steps): layout.run_layout when the runtime has it, else run_layout_steps with the
    custom functions wrapped so the layout they return (and its vars) is seen."""
    from . import layout as _layout

    run_layout = getattr(_layout, "run_layout", None)
    if run_layout is not None:
        keys, after = run_layout(files, variables, steps, ctx, custom)
        return keys, dict(after or {})
    seen = {"vars": dict(variables)}

    def wrap(fn):
        def call(lay, p, c):
            out = fn(lay, p, c)
            if isinstance(out, dict) and isinstance(out.get("vars"), dict):
                seen["vars"] = dict(out["vars"])
            return out
        return call
    keys = run_layout_steps(files, variables, steps, ctx, {k: wrap(f) for k, f in custom.items()})
    return keys, seen["vars"]
