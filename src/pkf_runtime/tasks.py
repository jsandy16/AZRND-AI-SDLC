"""What each Airflow task does. The generated DAG is a thin wrapper around these functions; they import nothing
from Airflow, so the local runner (`pkf run`) and the tests execute exactly the same code.

Run shape (every pipeline, whatever its trigger):

  check_source → check_destination → read_events → plan_packages
      → package[i] (mapped task group, one per package): extract → t01…tNN → upload
      → ack → report

With a `custom_delivery` destination step the files are not staged: extract only records the package and upload
calls the generated delivery function with AWS clients (server-side copies, moves, tags).

Every function takes the pipeline lock (resolved specs) and a run context
  {"run_id", "logical_date", "params": {...run params}, "conf": {...dag_run.conf}}
and returns a small JSON-able dict (XCom). Bulk state (events, package plans, results, packages) lives in the
run's staging folder, never in XCom.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from . import VERSION, components, credentials, delivery, staging
from .destinations import deliver, open_destination
from .discovery import SourceView, initial_groups, run_source_steps
from .events import messages_to_events, receive_step, run_event_steps
from .layout import run_layout, run_layout_steps
from .ops import run_step
from .package import BUILTIN_NAMES, Package, PackageError, render
from .sources import SourceError, access_errors, open_source

log = logging.getLogger("pkf")
MAX_PACKAGES = 1000  # below Airflow's default [core] max_map_length (1024)


class RunFailed(Exception):
    """Some packages or events failed; the others were delivered and acknowledged."""


# ---------------------------------------------------------------------------------------------- lock and context
def lock_digest(lock: dict) -> str:
    import hashlib

    body = {k: v for k, v in lock.items() if k not in ("lock_sha256", "_dir")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def load_lock(dag_file: str, pipeline: str) -> dict:
    base = Path(dag_file).resolve().parent / "pkf" / pipeline
    lock = json.loads((base / "pipeline.lock.json").read_text(encoding="utf-8"))
    if lock.get("lock_sha256") != lock_digest(lock):
        raise PermissionError(f"pkf/{pipeline}/pipeline.lock.json was edited after `pkf build`; rebuild from the specs")
    lock["_dir"] = str(base)
    return lock


def builtins(lock: dict, ctx: dict) -> dict[str, Any]:
    """Built-in variables of this run. Nothing (spec params, run configuration, events) can override them."""
    when = ctx.get("logical_date") or dt.datetime.now(dt.timezone.utc)
    if isinstance(when, str):
        when = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
    return {
        "pipeline": lock["pipeline"]["name"], "source": lock["source"]["name"], "destination": lock["destination"]["name"],
        "connection": lock["connection"]["name"], "trigger": (lock.get("trigger") or {}).get("name", "schedule"),
        "run_id": staging.safe_run_id(ctx.get("run_id", "manual")),
        "ds": when.strftime("%Y-%m-%d"), "ds_nodash": when.strftime("%Y%m%d"), "ts_nodash": when.strftime("%Y%m%dT%H%M%S"),
        "env": os.environ.get("PKF_ENV", "dev"),
    }


def variables(lock: dict, ctx: dict, extra: dict | None = None) -> dict[str, Any]:
    """Template variables: parameter defaults (source < trigger < transformation < destination < pipeline), the run's
    own values for declared parameters, then event/package variables, then the built-ins, which always win."""
    trigger = (lock.get("trigger") or {}).get("params") or {}
    layers = [lock["source"].get("params") or {}, trigger, (lock.get("transformation") or {}).get("params") or {},
              lock["destination"].get("params") or {}, lock["pipeline"].get("params") or {}]
    declared = {k for layer in layers for k in layer} - set(trigger)  # a run cannot repoint the trigger (e.g. its queue)
    v: dict[str, Any] = {}
    for layer in (*layers, {k: val for k, val in (ctx.get("params") or {}).items() if k in declared}):
        for k, val in layer.items():
            if val is not None and str(val) != "":
                v[k] = str(val)
    v.update({k: str(val) for k, val in (extra or {}).items() if k not in BUILTIN_NAMES})
    return {**v, **builtins(lock, ctx)}


def _creds(spec: dict | None, lock: dict, ctx: dict) -> dict[str, str]:
    return credentials.resolve(spec, builtins(lock, ctx))


def _workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="pkf_"))


def _url(lock: dict, ctx: dict, name: str) -> str:
    return staging.stage_url(lock["staging_url"], lock["pipeline"]["name"], ctx.get("run_id", "manual"), name)


def _pkg_stage(lock: dict, ctx: dict, index: int, name: str) -> str:
    return _url(lock, ctx, f"p{index:04d}/{name}")


# ---------------------------------------------------------------------------------------------- checks
def check_source(lock: dict, ctx: dict) -> dict:
    src = lock["source"]
    creds = _creds(src.get("credentials"), lock, ctx)
    with access_errors(f"source {src['name']}"):
        s = open_source(src, creds)
        try:
            return {"source": s.check()}
        finally:
            s.close()


def check_destination(lock: dict, ctx: dict) -> dict:
    dst = lock["destination"]
    creds = _creds(dst.get("credentials"), lock, ctx)
    with access_errors(f"destination {dst['name']}"):
        d = open_destination(dst, creds)
        try:
            return {"destination": d.check()}
        finally:
            d.close()


# ---------------------------------------------------------------------------------------------- trigger
def _sqs(lock: dict, ctx: dict):
    import boto3

    t = lock.get("trigger") or {}
    conn = t.get("connection") or {}
    creds = _creds(t.get("credentials"), lock, ctx)
    kw = {k: creds[k] for k in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token") if creds.get(k)}
    endpoint = conn.get("endpoint_url") or os.environ.get("PKF_SQS_ENDPOINT_URL") or None
    return boto3.client("sqs", region_name=conn.get("region"), endpoint_url=endpoint, **kw)


def _receive(lock: dict, step: dict, ctx: dict, v: dict) -> tuple[list[dict], dict[str, str]]:
    """(messages, receipt handles by message id)."""
    op, p = step["op"], step["params"]
    if op == "schedule":
        return [{"id": v["run_id"], "body": "", "attributes": {}}], {}
    if op == "run_conf":
        items = (ctx.get("conf") or {}).get(p.get("key", "events")) or []
        if not isinstance(items, list):
            raise PackageError(f"run configuration '{p.get('key', 'events')}' must be a list")
        return [{"id": f"conf-{i}", "body": it if isinstance(it, str) else json.dumps(it), "attributes": {}} for i, it in enumerate(items)], {}
    if op != "sqs":
        raise ValueError(f"unknown receive step '{op}'")
    sqs, url = _sqs(lock, ctx), render(p["queue_url"], v)
    msgs, handles, wait = [], {}, p.get("wait_seconds", 2)
    with access_errors(f"trigger {(lock.get('trigger') or {}).get('name')}"):
        while len(msgs) < p.get("max_messages", 100):
            resp = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=min(10, p.get("max_messages", 100) - len(msgs)),
                                       WaitTimeSeconds=wait, VisibilityTimeout=p.get("visibility_timeout_seconds", 1800),
                                       MessageSystemAttributeNames=["ApproximateReceiveCount", "SentTimestamp"])
            batch = resp.get("Messages", [])
            if not batch:
                break
            for m in batch:
                if m["MessageId"] in handles:
                    continue
                handles[m["MessageId"]] = m["ReceiptHandle"]
                msgs.append({"id": m["MessageId"], "body": m.get("Body", ""), "attributes": m.get("Attributes", {})})
            wait = 0
    return msgs, handles


def _release(lock: dict, ctx: dict, url: str, handles: list[str]) -> None:
    """Make kept messages visible again now (retried by the next run, then the dead-letter queue), not after the full
    visibility timeout."""
    if not handles:
        return
    sqs = _sqs(lock, ctx)
    with access_errors(f"trigger {(lock.get('trigger') or {}).get('name')}"):
        for i in range(0, len(handles), 10):
            sqs.change_message_visibility_batch(QueueUrl=url, Entries=[{"Id": str(j), "ReceiptHandle": h, "VisibilityTimeout": 0}
                                                                       for j, h in enumerate(handles[i:i + 10])])


def _delete(lock: dict, ctx: dict, url: str, handles: list[str]) -> int:
    if not handles:
        return 0
    sqs, n = _sqs(lock, ctx), 0
    with access_errors(f"trigger {(lock.get('trigger') or {}).get('name')}"):
        for i in range(0, len(handles), 10):
            entries = [{"Id": str(j), "ReceiptHandle": h} for j, h in enumerate(handles[i:i + 10])]
            resp = sqs.delete_message_batch(QueueUrl=url, Entries=entries)
            n += len(resp.get("Successful", []))
            for f in resp.get("Failed", []):
                log.warning("could not delete a message (%s): it will be read again after its visibility timeout", f.get("Code"))
    return n


def _has_dlq(lock: dict, ctx: dict, url: str) -> bool:
    try:
        attrs = _sqs(lock, ctx).get_queue_attributes(QueueUrl=url, AttributeNames=["RedrivePolicy"]).get("Attributes", {})
    except Exception:  # noqa: BLE001 - only used to pick the poison-message policy
        return True
    if "RedrivePolicy" not in attrs:
        log.warning("queue %s has no dead-letter queue: a message that always fails is retried forever; configure a "
                    "RedrivePolicy or the trigger's max_receives", url)
        return False
    return True


def read_events(lock: dict, ctx: dict) -> dict:
    """Receive messages and turn them into events (one package each, unless a step groups or splits them)."""
    steps, custom = components.load(lock, "trigger")
    recv = receive_step(steps)
    v = variables(lock, ctx)
    staging.clear(_url(lock, ctx, "_run"))  # a re-run with the same run id starts from a clean slate
    messages, handles = _receive(lock, recv, ctx, v)
    if recv["op"] == "sqs":
        queue = render(recv["params"]["queue_url"], v)
        has_dlq = _has_dlq(lock, ctx, queue)
        limit = recv["params"].get("max_receives")
        if limit and not has_dlq:
            over = [m for m in messages if int(m["attributes"].get("ApproximateReceiveCount", "1")) > limit]
            for m in over:
                log.error("message %s was received %s times (max_receives %s) and keeps failing: deleted; body starts %r",
                          m["id"], m["attributes"].get("ApproximateReceiveCount"), limit, m["body"][:200])
            _delete(lock, ctx, queue, [handles[m["id"]] for m in over])
            messages = [m for m in messages if m not in over]
    events = run_event_steps(messages_to_events(messages), steps[1:], {"params": v, "run_id": v["run_id"]}, custom)
    used = {m for e in events for m in e.get("message_ids", [])}
    unmatched = [m["id"] for m in messages if m["id"] not in used]
    queue = render(recv["params"]["queue_url"], v) if recv["op"] == "sqs" else None
    deleted = _delete(lock, ctx, queue, [handles[m] for m in unmatched]) if queue and recv["params"].get("delete_unmatched", True) else 0
    staging.put_json(_url(lock, ctx, "_run/events.json"), {"receive": recv["op"], "queue_url": queue, "events": events,
                                                          "handles": {m: handles[m] for m in used if m in handles}})
    errors = [e for e in events if e.get("error")]
    for e in errors:
        log.warning("event %s: %s (its message stays in the queue)", e["id"], e["error"])
    out = {"messages": len(messages), "events": len(events) - len(errors), "event_errors": len(errors),
           "unmatched_acknowledged": deleted}
    log.info("trigger: %s", out)
    return out


# ---------------------------------------------------------------------------------------------- source
def plan_packages(lock: dict, ctx: dict) -> list[dict]:
    """Work out each package's files (source steps per event). Returns the mapping list for the package group.

    ctx may carry (local runner only, never Airflow): "max_packages" (at most N packages now; the rest wait for the
    next run, their messages are released) and "only" (the planned.json of a preview, K6: keep only the files that
    were previewed, unchanged and with the same target)."""
    doc = staging.get_json(_url(lock, ctx, "_run/events.json"))
    if doc is None:
        raise RunFailed("no events recorded for this run (read_events did not finish)")
    good = [e for e in doc["events"] if not e.get("error")]
    steps, custom = components.load(lock, "source")
    staging.clear(_url(lock, ctx, "_run/results"))  # a re-planned run never trusts an earlier attempt's results
    staging.clear(_url(lock, ctx, "_run/failed"))
    packages: list[dict] = []
    failed: list[dict] = []
    src = lock["source"]
    session = None
    if good:
        with access_errors(f"source {src['name']}"):
            session = open_source(src, _creds(src.get("credentials"), lock, ctx))
    try:
        for e in good:
            v = variables(lock, ctx, e.get("vars"))
            sctx = {"params": v, "run_id": v["run_id"], "now": time.time(),
                    "source_bucket": (src.get("connection") or {}).get("bucket") if src["type"] == "s3" else None}
            try:
                with access_errors(f"source {src['name']}"):
                    groups = run_source_steps(initial_groups([e]), steps, sctx, SourceView(session), custom)
            except PackageError as exc:
                failed.append({"event_id": e["id"], "message_ids": e.get("message_ids", []), "error": str(exc)})
                log.warning("event %s: %s", e["id"], exc)
                continue
            for g in groups:
                for r in g.get("rejected") or []:  # B4: this file fails the run at the end; the others are delivered
                    shown = r.get("key") or r["path"]
                    failed.append({"event_id": e["id"], "message_ids": e.get("message_ids", []), "error": f"file {shown}: {r['error']}",
                                   "files": [{"path": r["path"], "source_key": r.get("key") or r["path"], "target_key": None, "size": None}]})
                    log.error("file %s refused: %s (the other files go on)", shown, r["error"])
                if not g["files"]:
                    log.info("event %s: no files to deliver", e["id"])
                    continue
                gv = variables(lock, ctx, g.get("vars"))
                packages.append({"index": len(packages), "event_id": e["id"], "message_ids": e.get("message_ids", []),
                                 "vars": {k: x for k, x in gv.items() if k not in BUILTIN_NAMES},
                                 "package": render(lock["pipeline"]["package_name"], gv), "files": g["files"]})
    finally:
        if session is not None:
            session.close()
    dropped: set[str] = set()
    only_counts = None
    if _direct(lock):  # custom delivery: every target known before anything is copied or deleted
        dsteps, dcustom = components.load(lock, "destination")
        for pk in packages:
            v = {**variables(lock, ctx, pk["vars"]), "package": pk["package"]}
            try:
                keys, after = run_layout([f["path"] for f in pk["files"]], v, dsteps, {"params": v, "run_id": v["run_id"]}, dcustom)
            except PackageError as exc:
                pk["error"] = str(exc)
                continue
            pk["keys"] = keys
            pk["layout_vars"] = {k: str(x) for k, x in after.items() if k not in v or str(v[k]) != str(x)}  # K13
        listed = [f for pk in packages for f in pk["files"]]
        if ctx.get("only") is not None:
            packages, dropped, only_counts = _keep_planned(packages, ctx["only"])
        bad = delivery.plan_conflicts([pk for pk in packages if "error" not in pk], src["connection"]["bucket"],
                                      lock["destination"]["connection"]["bucket"], listed=listed)
        for pk in packages:
            reason = pk.get("error") or bad.get(pk["index"])
            if reason:
                failed.append({"event_id": pk["event_id"], "message_ids": pk["message_ids"], "error": f"package {pk['package']}: {reason}",
                               "package": pk["package"], "files": _file_refs(pk)})
                log.error("package %s refused before delivery: %s", pk["package"], reason)
        packages = [pk for pk in packages if not (pk.get("error") or pk["index"] in bad)]
        for i, pk in enumerate(packages):
            pk["index"] = i
    elif ctx.get("only") is not None:
        raise RunFailed("--only applies to custom-delivery pipelines")
    limit = MAX_PACKAGES
    if ctx.get("max_packages"):
        limit = max(1, min(int(ctx["max_packages"]), MAX_PACKAGES))
    deferred = set(dropped)  # messages of packages left out are released, never acknowledged
    deferred_packages = max(0, len(packages) - limit)
    if len(packages) > limit:  # the rest waits for the next run (its messages are released, not failed)
        kept_msgs = {m for p in packages[:limit] for m in p["message_ids"]}
        later = {m for p in packages[limit:] for m in p["message_ids"]} - kept_msgs
        deferred |= later
        log.warning("%s packages planned; %s run now%s, the rest (%s message(s)) in the next run", len(packages), limit,
                    f" (max_packages {ctx['max_packages']})" if limit < MAX_PACKAGES else "", len(later))
        packages = packages[:limit]
    staging.put_json(_url(lock, ctx, "_run/plan.json"), {"packages": packages, "failed_events": failed, "deferred": sorted(deferred),
                                                        "deferred_packages": deferred_packages, "only": only_counts,
                                                        "max_packages": ctx.get("max_packages")})
    log.info("planned %s package(s); %s event(s) or file(s) failed", len(packages), len(failed))
    return [{"index": p["index"], "package": p["package"]} for p in packages]


def _file_refs(pk: dict) -> list[dict]:
    """The files of a package refused at planning, for the per-file report."""
    keys, lv = pk.get("keys") or {}, pk.get("layout_vars") or {}
    return [{"path": f["path"], "source_key": delivery.source_key(f), "target_key": keys.get(f["path"]), "size": f.get("size"),
             "note": _note(lv, f["path"])} for f in pk["files"]]


def _keep_planned(packages: list[dict], only: dict) -> tuple[list[dict], set[str], dict]:
    """--only (K6): keep the files of the preview whose source key is planned, whose ETag is the planned one (when
    one was recorded) and whose target key is the planned target. Returns (packages, message ids of packages left
    empty, counts)."""
    want = {f["source_key"]: f for f in only.get("files") or [] if f.get("source_key")}
    seen: set[str] = set()
    kept: list[dict] = []
    dropped: set[str] = set()
    excluded = 0
    for pk in packages:
        here = [delivery.source_key(f) for f in pk["files"]]
        seen |= {k for k in here if k in want}
        if "error" in pk:  # its layout fails now: the package fails (reported) if it holds a planned file
            if any(k in want for k in here):
                kept.append(pk)
            else:
                excluded += len(here)
                dropped |= set(pk["message_ids"])
            continue
        keys, keep = pk.get("keys") or {}, []
        for f, sk in zip(pk["files"], here):
            w = want.get(sk)
            if w is None or (w.get("etag") is not None and f.get("etag") != w["etag"]) or keys.get(f["path"]) != w.get("target_key"):
                excluded += 1
                continue
            keep.append(f)
        if keep:
            kept.append({**pk, "files": keep, "keys": {f["path"]: keys.get(f["path"]) for f in keep}})
        else:
            dropped |= set(pk["message_ids"])
    counts = {"planned_files": len(want), "kept": sum(len(pk["files"]) for pk in kept if "error" not in pk),
              "excluded_since_preview": excluded, "missing_since_preview": len(set(want) - seen)}
    log.warning("only the previewed files: %s kept; %s listed file(s) excluded (new or changed since the preview); "
                "%s previewed file(s) no longer listed", counts["kept"], excluded, counts["missing_since_preview"])
    return kept, dropped, counts


def _note(layout_vars: dict, path: str) -> str:
    """The per-file rule or error reason an earlier layout step recorded (layout["vars"]["reason:<path>"] or
    "note:<path>"), for the run report's note column (K7)."""
    return str(layout_vars.get(f"reason:{path}") or layout_vars.get(f"note:{path}") or "")


# ---------------------------------------------------------------------------------------------- one package
def _direct(lock: dict) -> bool:
    return delivery.has_custom_delivery(components.load(lock, "destination")[0])


def _plan(lock: dict, ctx: dict) -> dict:
    """This run's plan.json; missing → RunFailed (D6: on multi-worker Airflow every worker must reach the staging
    that plan_packages wrote to)."""
    url = _url(lock, ctx, "_run/plan.json")
    plan = staging.get_json(url)
    if plan is None:
        raise RunFailed(f"the run plan is missing in staging ({url}): every Airflow worker must reach the same staging "
                        "(set PKF_STAGING_URL=s3://…)")
    return plan


def extract(lock: dict, spec: dict, ctx: dict) -> dict:
    plan = _plan(lock, ctx)
    p = plan["packages"][spec["index"]]
    src = lock["source"]
    if _direct(lock):  # the delivery function copies from the source itself: nothing to download
        size = sum(f.get("size") or 0 for f in p["files"])
        log.info("package %s: %s file(s), %s bytes, delivered directly (no staging)", p["package"], len(p["files"]), size)
        return {"stage": None, "direct": True, "index": spec["index"], "package": p["package"], "files": len(p["files"]), "bytes": size}
    v = variables(lock, ctx, p["vars"])
    work = _workdir()
    with access_errors(f"source {src['name']}"):
        session = open_source(src, _creds(src.get("credentials"), lock, ctx))
    try:
        pkg = Package(work, p["package"], v)
        for f in p["files"]:
            dest = pkg.root / f["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            with access_errors(f"source {src['name']}"):
                session.download(f["root"], f["path"], str(dest))
            if f.get("size") is not None and dest.stat().st_size != f["size"]:
                raise ConnectionError(f"download of '{f['path']}' incomplete ({dest.stat().st_size} of {f['size']} bytes)")
        pkg.save()
        url = _pkg_stage(lock, ctx, spec["index"], "00_extract")
        staging.put(work, url)
        log.info("package %s: extracted %s file(s), %s bytes", p["package"], len(p["files"]), pkg.total_bytes())
        return {"stage": url, "index": spec["index"], "package": pkg.name, "files": len(p["files"]), "bytes": pkg.total_bytes()}
    finally:
        session.close()
        shutil.rmtree(work, ignore_errors=True)


def transform(lock: dict, step_numbers: list[int], previous: dict, ctx: dict) -> dict:
    """Run transformation steps `step_numbers` (1-based) on the previous stage's package."""
    steps, custom = components.load(lock, "transformation")
    work = _workdir()
    try:
        staging.get(previous["stage"], work)
        pkg = Package.load(work)
        run_ctx = {"params": {k: val for k, val in pkg.vars.items()}, "run_id": ctx.get("run_id"), "step": None}
        for n in step_numbers:
            step = steps[n - 1]
            run_ctx["step"] = step["id"]
            t0 = time.time()
            run_step(pkg, step["op"], step["params"], run_ctx, custom)
            log.info("step %s %s (%s): %s file(s) after %.1fs", n, step["id"], step["op"], len(pkg.files()), time.time() - t0)
        pkg.save()
        last = steps[step_numbers[-1] - 1]["id"]
        url = _pkg_stage(lock, ctx, previous["index"], f"{step_numbers[-1]:02d}_{last}")
        staging.put(work, url)
        return {"stage": url, "index": previous["index"], "package": pkg.name, "files": len(pkg.files()), "bytes": pkg.total_bytes()}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _upload_direct(lock: dict, previous: dict, ctx: dict) -> dict:
    """Custom delivery: the generated function delivers every file with AWS clients."""
    dst, src = lock["destination"], lock["source"]
    plan = _plan(lock, ctx)
    steps, custom = components.load(lock, "destination")
    if src["type"] != "s3" or dst["type"] != "s3":
        raise PackageError("custom delivery needs an S3 source and an S3 destination")
    p = plan["packages"][previous["index"]]
    # K13: values earlier layout steps put in layout["vars"] (e.g. "reason:<path>") reach the function in params
    v = {**variables(lock, ctx, p["vars"]), "package": p["package"], **(p.get("layout_vars") or {})}
    keys = p["keys"]  # computed and checked against every other package when the run was planned
    transfers = delivery.transfers_for(p["files"], keys, src["connection"]["bucket"], dst["connection"]["bucket"])
    step = delivery.delivery_step(steps)
    fn = custom.get(step["params"]["function"])
    if fn is None:
        raise RuntimeError(f"custom delivery function '{step['params']['function']}' is not implemented")
    aws = delivery.Aws(src.get("connection"), _creds(src.get("credentials"), lock, ctx), dst.get("connection"),
                       _creds(dst.get("credentials"), lock, ctx))
    pol = dst.get("delivery") or {}
    with access_errors(f"destination {dst['name']}"):
        todo, decided = delivery.precheck(transfers, aws, pol.get("if_exists", "skip_identical"))
        # the same ctx keys as the step-test harness (simulate.run_step_case): params, run_id, now, delivery
        fctx = {"params": v, "run_id": v["run_id"], "now": time.time(), "delivery": pol}
        results = decided + delivery.verify(delivery.run_custom_delivery(fn, step["id"], todo, v, fctx, aws), todo, aws)
    results.sort(key=lambda r: r["path"])
    for r in results:
        (log.error if r["status"].lower().startswith("fail") else log.info)(
            "%s -> %s [%s]%s", r["path"], r["key"], r["status"], f" {r['detail']}" if r["detail"] else "")
    bad = delivery.failed(results)
    if bad:  # keep the per-file outcome (which files moved, which failed) for the run report, then fail the package
        staging.put_json(_url(lock, ctx, f"_run/failed/p{previous['index']:04d}.json"), {"index": previous["index"], "objects": results})
        raise PackageError(f"{len(bad)} of {len(results)} file(s) failed delivery, e.g. '{bad[0]['path']}': {bad[0]['detail'] or bad[0]['status']}")
    summary = {"index": previous["index"], "destination": f"s3://{dst['connection']['bucket']}", "package": p["package"],
               "files": len(results), "uploaded": sum(not r["status"].lower().startswith("skip") for r in results),
               "skipped_identical": sum(r["status"].lower().startswith("skip") for r in results),
               "bytes": sum(r["size"] for r in results), "statuses": delivery.status_counts(results)}
    staging.put_json(_url(lock, ctx, f"_run/results/p{previous['index']:04d}.json"), {**summary, "objects": results})
    log.info("package %s delivered: %s", p["package"], summary)
    return summary


def upload(lock: dict, previous: dict, ctx: dict) -> dict:
    if previous.get("direct"):
        return _upload_direct(lock, previous, ctx)
    dst = lock["destination"]
    steps, custom = components.load(lock, "destination")
    work = _workdir()
    try:
        staging.get(previous["stage"], work)
        pkg = Package.load(work)
        v = {**pkg.vars, **builtins(lock, ctx), "package": pkg.name}
        keys = run_layout_steps(pkg.files(), v, steps, {"params": v, "run_id": v["run_id"]}, custom)
        creds = _creds(dst.get("credentials"), lock, ctx)
        with access_errors(f"destination {dst['name']}"):
            d = open_destination(dst, creds)
            try:
                results = deliver(pkg, d, keys, {**(dst.get("delivery") or {}), "run_id": v["run_id"]})
                location = d.url(os.path.commonprefix([r["key"] for r in results]).rsplit("/", 1)[0]) if results else None
            finally:
                d.close()
        summary = {"index": previous["index"], "destination": location, "package": pkg.name, "files": len(results),
                   "uploaded": sum(r["status"] == "uploaded" for r in results),
                   "skipped_identical": sum(r["status"].startswith("skipped") for r in results),
                   "bytes": sum(r["size"] for r in results)}
        staging.put_json(_url(lock, ctx, f"_run/results/p{previous['index']:04d}.json"), {**summary, "objects": results})
        log.info("package %s delivered: %s", pkg.name, summary)
        return summary
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------------------------- end of run
def _outcome(lock: dict, ctx: dict) -> dict:
    events = staging.get_json(_url(lock, ctx, "_run/events.json"))
    plan = staging.get_json(_url(lock, ctx, "_run/plan.json"))
    results = {r["index"]: r for r in staging.list_json(_url(lock, ctx, "_run/results"))}
    return {"events": events, "plan": plan, "results": results}


def ack(lock: dict, ctx: dict) -> dict:
    """Delete the messages whose packages were all delivered (runs even when some packages failed)."""
    o = _outcome(lock, ctx)
    if o["events"] is None or o["plan"] is None or o["events"].get("receive") != "sqs":
        return {"deleted": 0, "kept": 0}
    bad = {m for e in o["events"]["events"] if e.get("error") for m in e.get("message_ids", [])}
    bad |= {m for f in o["plan"]["failed_events"] for m in f["message_ids"]}
    bad |= {m for p in o["plan"]["packages"] if p["index"] not in o["results"] for m in p["message_ids"]}
    bad |= set(o["plan"].get("deferred", []))
    handles = o["events"]["handles"]
    done = [h for m, h in handles.items() if m not in bad]
    deleted = _delete(lock, ctx, o["events"]["queue_url"], done)
    _release(lock, ctx, o["events"]["queue_url"], [h for m, h in handles.items() if m in bad])
    out = {"deleted": deleted, "kept": len(handles) - len(done)}
    staging.put_json(_url(lock, ctx, "_run/ack.json"), out)
    log.info("acknowledged: %s", out)
    return out


def _label(p: dict) -> str:
    """A package in the report: name, index and first file (D19: packages of one-file pipelines often share a name)."""
    first = p["files"][0]["path"] if p.get("files") else "-"
    return f"{p['package']}#{p['index']} ({first})"


def report(lock: dict, ctx: dict, keep_staging: bool = False) -> dict:
    """Run summary; fails the run when anything failed (after the successful packages were delivered and acked)."""
    o = _outcome(lock, ctx)
    if o["events"] is None or o["plan"] is None:
        raise RunFailed("the run stopped before its packages were planned (see the failed task)")
    if o["events"].get("receive") == "sqs" and staging.get_json(_url(lock, ctx, "_run/ack.json")) is None:
        raise RunFailed("the ack task did not finish: delivered messages were not deleted and will be read again")
    planned = o["plan"]["packages"]
    failed_pkgs = [_label(p) for p in planned if p["index"] not in o["results"]]
    failed_events = [e for e in o["events"]["events"] if e.get("error")] + o["plan"]["failed_events"]
    summary = {"pipeline": lock["pipeline"]["name"], "run_id": ctx.get("run_id"), "runtime_version": VERSION,
               "events": len([e for e in o["events"]["events"] if not e.get("error")]), "packages": len(planned),
               "delivered": len(o["results"]), "failed_packages": failed_pkgs,
               "failed_events": [{"event": e.get("id") or e.get("event_id"), "error": e["error"]} for e in failed_events],
               "files": sum(r["files"] for r in o["results"].values()),
               "uploaded": sum(r["uploaded"] for r in o["results"].values()),
               "skipped_identical": sum(r["skipped_identical"] for r in o["results"].values()),
               "bytes": sum(r["bytes"] for r in o["results"].values()),
               "destinations": sorted({r["destination"] for r in o["results"].values() if r.get("destination")}),
               "deferred_messages": len(o["plan"].get("deferred", [])), "deferred_packages": o["plan"].get("deferred_packages", 0)}
    statuses: dict[str, int] = {}
    for r in o["results"].values():
        for k, n in (r.get("statuses") or {}).items():
            statuses[k] = statuses.get(k, 0) + n
    if statuses:
        summary["statuses"] = dict(sorted(statuses.items()))
    if o["plan"].get("max_packages"):
        summary["max_packages"] = o["plan"]["max_packages"]
    if o["plan"].get("only"):  # --only (K6): what the preview planned and what changed since
        summary.update({k: o["plan"]["only"][k] for k in ("planned_files", "excluded_since_preview", "missing_since_preview")})
    log.info("run summary: %s", json.dumps(summary, sort_keys=True))
    if failed_pkgs or failed_events:
        err = RunFailed(f"{len(failed_pkgs)} package(s) and {len(failed_events)} event(s) failed; delivered {len(o['results'])} "
                        f"of {len(planned)} (staging kept for inspection): {json.dumps(summary['failed_events'])[:500]}"
                        + (f"; failed packages: {', '.join(failed_pkgs[:5])}" if failed_pkgs else ""))
        err.summary = summary  # type: ignore[attr-defined]
        raise err
    if not keep_staging and not os.environ.get("PKF_KEEP_STAGING"):
        staging.clear_run(lock["staging_url"], lock["pipeline"]["name"], ctx.get("run_id", "manual"))
    return summary


SAMPLE_CUSTOM_DELIVERY = 5000
"""dry_run rows shown for a custom delivery (the files it moves or deletes): enough for every planned file of a run
of up to MAX_PACKAGES one-file packages (D8). Framework uploads keep the `sample` argument."""


def dry_run(lock: dict, ctx: dict, sample: int = 50, *, full: bool = False) -> dict:
    """What a run would do, changing nothing: events and packages as planned, every file's target key, and what a
    custom delivery would touch. SQS messages read for the dry run are made visible again at once; staging is cleared.
    full=True adds "planned_files": every planned file as {"package", "index", "path", "source_key", "etag", "size",
    "target_key"} (what `pkf run --dry-run --plan-out` writes to planned.json, K6)."""
    out: dict[str, Any] = {"pipeline": lock["pipeline"]["name"], "run_id": ctx.get("run_id")}
    try:
        out["read_events"] = read_events(lock, ctx)
        plan_packages(lock, ctx)
        plan = staging.get_json(_url(lock, ctx, "_run/plan.json")) or {"packages": [], "failed_events": []}
        dsteps, dcustom = components.load(lock, "destination")
        direct = delivery.has_custom_delivery(dsteps)
        src, dst = lock["source"], lock["destination"]
        rows, prefixes, planned = [], {}, []
        for p in plan["packages"]:
            keys = p.get("keys")
            if keys is None and lock.get("transformation") is None:  # framework upload, files unchanged: keys are known
                v = {**variables(lock, ctx, p["vars"]), "package": p["package"]}
                keys = run_layout_steps([f["path"] for f in p["files"]], v, dsteps, {"params": v, "run_id": v["run_id"]}, dcustom)
            for f in p["files"]:
                k = (keys or {}).get(f["path"]) if keys is not None else "(after transformation)"
                skey = delivery.source_key(f) if src["type"] == "s3" else f["path"]
                rows.append({"package": p["package"], "index": p["index"], "path": f["path"], "source": skey,
                             "size": f.get("size"), "target": k})
                if full:
                    planned.append({"package": p["package"], "index": p["index"], "path": f["path"], "source_key": skey,
                                    "etag": f.get("etag"), "size": f.get("size"), "target_key": k if keys is not None else None})
                if isinstance(k, str):
                    top = k.split("/", 1)[0] + "/" if "/" in k else k
                    prefixes[top] = prefixes.get(top, 0) + 1
        same_bucket = direct and src["type"] == "s3" and (src.get("connection") or {}).get("bucket") == (dst.get("connection") or {}).get("bucket")
        cap = max(sample, SAMPLE_CUSTOM_DELIVERY) if direct else sample
        out.update(packages=len(plan["packages"]), files=len(rows), failed_events=plan["failed_events"],
                   deferred_packages=plan.get("deferred_packages", 0), delivery="custom_delivery" if direct else "framework upload",
                   targets_by_prefix=dict(sorted(prefixes.items())), sample=rows[:cap], sample_truncated=len(rows) > cap,
                   max_packages=plan.get("max_packages"),
                   note=(f"custom delivery: the approved code receives {len(rows)} transfer(s); if it moves files, up to "
                         f"{len(rows)} source object(s) are deleted" + (" in the same bucket" if same_bucket else "")) if direct else
                        "framework upload: source objects are never deleted")
        if full:
            out["planned_files"] = planned
    finally:
        doc = staging.get_json(_url(lock, ctx, "_run/events.json"))
        if doc and doc.get("receive") == "sqs":
            _release(lock, ctx, doc["queue_url"], list(doc.get("handles", {}).values()))
        staging.clear_run(lock["staging_url"], lock["pipeline"]["name"], ctx.get("run_id", "manual"))
    return out


def is_custom_delivery(lock: dict) -> bool:
    """The destination ends with a custom_delivery step (the generated code moves, copies or deletes objects)."""
    return _direct(lock)


FAIL_FAST = (PackageError, credentials.CredentialError, PermissionError, SourceError, RunFailed)
"""Data or configuration problems: retrying cannot help, so the DAG fails these tasks without retries."""

EXPIRED_CREDENTIALS = ("ExpiredToken", "RequestExpired", "TokenRetrievalError", "SSOTokenLoadError", "UnauthorizedSSOTokenError")
"""Error types or codes (ExpiredToken also covers ExpiredTokenException) that mean the run's AWS credentials ended:
no later package can succeed, so the local run stops attempting them (D2)."""


def credentials_expired(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(name in text for name in EXPIRED_CREDENTIALS)


def file_rows(lock: dict, ctx: dict, errors: dict[int, str] | None = None, not_attempted: tuple | list | set = ()) -> list[dict]:
    """One row per planned file (K7): {"package", "index", "path", "source_key", "target_key", "status", "detail",
    "note", "size"}. Delivered and failed files come from the packages' results; files of a package without results
    are "failed" with the package's error (errors: index → message), or "not_attempted" when the run stopped first;
    files refused at planning (bad paths, conflicts) are "failed" with the reason; a file the layout does not deliver
    is "not_delivered". note = the reason:<path> / note:<path> value an earlier layout step recorded. Read from
    staging, so call it before report() clears it."""
    o = _outcome(lock, ctx)
    plan = o["plan"]
    if plan is None:
        return []
    errors = errors or {}
    partial = {r["index"]: r for r in staging.list_json(_url(lock, ctx, "_run/failed"))}
    s3 = lock["source"]["type"] == "s3"
    transformed = lock.get("transformation") is not None
    rows: list[dict] = []

    def row(package, index, path, source_key, target_key, status, detail, size, note) -> dict:
        return {"package": package, "index": index, "path": path, "source_key": source_key, "target_key": target_key,
                "status": status, "detail": detail, "note": note, "size": size}

    for p in plan["packages"]:
        lv = p.get("layout_vars") or {}
        by_path = {f["path"]: f for f in p["files"]}
        skey = (lambda f: delivery.source_key(f) if s3 else f["path"])
        res = o["results"].get(p["index"]) or partial.get(p["index"])
        if res is not None and isinstance(res.get("objects"), list):
            done = set()
            for r in res["objects"]:
                path, f = r.get("path"), by_path.get(r.get("path"))
                rows.append(row(p["package"], p["index"], path, skey(f) if f else None, r.get("key"), r.get("status"),
                                str(r.get("detail") or ""), r.get("size") if r.get("size") is not None else (f or {}).get("size"),
                                _note(lv, path) if path else ""))
                done.add(path)
            if not transformed:
                rows += [row(p["package"], p["index"], f["path"], skey(f), None, "not_delivered",
                             "no destination key: the layout does not deliver this file", f.get("size"), _note(lv, f["path"]))
                         for f in p["files"] if f["path"] not in done]
            continue
        stopped = p["index"] in not_attempted
        detail = errors.get(p["index"]) or ("not attempted: the run stopped before this package" if stopped
                                            else "the package was not delivered (see the run log)")
        keys = p.get("keys") or {}
        rows += [row(p["package"], p["index"], f["path"], skey(f), keys.get(f["path"]), "not_attempted" if stopped else "failed",
                     detail, f.get("size"), _note(lv, f["path"])) for f in p["files"]]
    for e in plan.get("failed_events") or []:
        rows += [row(e.get("package"), None, f.get("path"), f.get("source_key"), f.get("target_key"), "failed", e["error"],
                     f.get("size"), f.get("note") or "") for f in e.get("files") or []]
    return rows


def run_all(lock: dict, ctx: dict, keep_staging: bool = False) -> dict:
    """The whole run in one process, task by task, like the DAG (local runner and tests). A failing package does not
    stop the others: an error that is not FAIL_FAST is retried once (the package again from extract, as Airflow's
    task retries would), then recorded; expired AWS credentials stop the run (the remaining packages are
    "not_attempted"). ack and report always run once the packages are planned.

    Returns {"tasks": trace, "files": file_rows, "summary": report}. Every exception that leaves run_all carries
    .trace, .files and .summary (None when the report did not run)."""
    trace: list[dict] = []
    state: dict[str, Any] = {"files": None, "summary": None}
    try:
        return _run_all(lock, ctx, keep_staging, trace, state)
    except Exception as exc:
        for name, value in (("trace", trace), ("files", state["files"] or []), ("summary", getattr(exc, "summary", None) or state["summary"])):
            try:
                setattr(exc, name, value)
            except (AttributeError, TypeError):  # an exception type without a __dict__
                pass
        raise


def _run_all(lock: dict, ctx: dict, keep_staging: bool, trace: list[dict], state: dict) -> dict:
    def rec(task, fn, *a):
        out = fn(lock, *a, ctx)
        trace.append({"task": task, "result": out})
        return out

    if lock["connection"].get("check_before_run", True):
        rec("check_source", check_source)
        rec("check_destination", check_destination)
    rec("read_events", read_events)
    specs = rec("plan_packages", plan_packages)
    n_steps = len(components.load(lock, "transformation")[0])
    errors: dict[int, str] = {}
    not_attempted: list[int] = []
    stopped: str | None = None
    for pos, spec in enumerate(specs):
        i = spec["index"]
        if stopped:
            not_attempted.append(i)
            continue
        for attempt in (1, 2):
            try:
                stage = rec(f"package[{i}].extract", extract, spec)
                for t in range(1, n_steps + 1):
                    stage = rec(f"package[{i}].t{t:02d}", transform, [t], stage)
                rec(f"package[{i}].upload", upload, stage)
                errors.pop(i, None)
                break
            except Exception as exc:  # noqa: BLE001 - recorded per package; the other packages go on
                msg = f"{type(exc).__name__}: {exc}"
                if credentials_expired(exc):
                    stopped = (f"AWS credentials expired after {pos} of {len(specs)} packages: log in again (aws sso login) "
                               "and run again; delivered files are recognised by the next run")
                elif not isinstance(exc, FAIL_FAST) and attempt == 1:
                    trace.append({"task": f"package[{i}]", "error": msg, "retrying": True})
                    log.warning("package %s: %s; retrying it once", spec["package"], msg)
                    continue
                errors[i] = msg
                trace.append({"task": f"package[{i}]", "error": msg})
                log.error("package %s failed: %s", spec["package"], msg)
                break
    if stopped:
        log.error("%s (%s package(s) not attempted)", stopped, len(not_attempted))
    try:
        rec("ack", ack)
    except Exception as exc:  # noqa: BLE001 - report() then says the ack did not finish
        trace.append({"task": "ack", "error": f"{type(exc).__name__}: {exc}"})
        log.error("ack failed: %s: %s", type(exc).__name__, exc)
    try:
        state["files"] = file_rows(lock, ctx, errors, not_attempted)
    except Exception as exc:  # noqa: BLE001 - the report must still run
        log.error("could not collect the per-file results: %s: %s", type(exc).__name__, exc)
        state["files"] = []
    try:
        summary = report(lock, ctx, keep_staging)
    except RunFailed as exc:
        trace.append({"task": "report", "error": str(exc)})
        state["summary"] = getattr(exc, "summary", None)
        if state["summary"] is not None and stopped:
            state["summary"].update(stopped=stopped, not_attempted_packages=len(not_attempted))
        err = RunFailed(f"{stopped}; {exc}" if stopped else str(exc))
        err.summary = state["summary"]  # type: ignore[attr-defined]
        raise err from None
    trace.append({"task": "report", "result": summary})
    state["summary"] = summary
    return {"tasks": trace, "files": state["files"], "summary": summary}
