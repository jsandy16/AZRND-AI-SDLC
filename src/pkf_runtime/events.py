"""Trigger catalogue: how a pipeline run learns what to ingest.

A trigger is an ordered list of steps. The first step *receives* messages (a schedule tick, SQS messages, or the run
configuration sent by Lambda or the REST API); the following steps turn messages into
**events**. Each event becomes one package (one branch of the DAG's mapped task group).

Message: {"id", "body": str, "attributes": {...}}            (receipt handles stay in the runtime, never in events)
Event:   {"id", "message_ids": [...], "data": parsed body or None, "objects": [{"bucket","key","size","etag","time",
          "sequencer"}], "vars": {name: str}, "error": None | str}

Messages whose events are all filtered out are acknowledged at once (nothing to do); messages with an error event
stay in the queue (retried after the visibility timeout, then moved to the dead-letter queue by SQS).
"""
from __future__ import annotations

import datetime as dt
import json
import posixpath
import re
from typing import Literal
from urllib.parse import unquote_plus

from pydantic import Field, field_validator

from .ops import Op, P, _rx
from .package import BUILTIN_NAMES, PackageError, matches_any, render

# ---------------------------------------------------------------------------------------------- receive steps
class ScheduleParams(P):
    """One event per run (classic scheduled pipeline)."""


class SqsParams(P):
    queue_url: str = Field(description="queue URL (template, e.g. '{queue_url}')")
    max_messages: int = Field(default=100, ge=1, le=1000, description="most messages read per run (one package each, at most)")
    wait_seconds: int = Field(default=2, ge=0, le=20, description="long-poll wait for the first receive call")
    visibility_timeout_seconds: int = Field(default=1800, ge=30, le=43200,
                                            description="hidden from other readers while this run works; longer than a run takes")
    delete_unmatched: bool = Field(default=True, description="delete messages that produce no event (test events, filtered keys)")
    max_receives: int | None = Field(default=None, ge=1, description="a message received more often than this is logged and "
                                     "deleted (only when the queue has no dead-letter queue to take it)")


class RunConfParams(P):
    key: str = Field(default="events", description="dag_run.conf key holding a list of messages (objects or strings)")


RECEIVE_OPS = ("schedule", "sqs", "run_conf")


def _receive_placeholder(events, p, ctx):  # receive steps run in tasks.read_events (they need clients and context)
    return events


# ---------------------------------------------------------------------------------------------- event steps
def _ok(events: list[dict]) -> list[dict]:
    return [e for e in events if not e.get("error")]


class ParseJsonParams(P):
    on_error: Literal["error", "drop"] = Field(default="error", description="error: keep the message for retry/DLQ; drop: ignore it")


def op_parse_json(events: list[dict], p: ParseJsonParams, ctx: dict) -> list[dict]:
    for e in _ok(events):
        try:
            e["data"] = json.loads(e.get("body") or "")
        except json.JSONDecodeError:
            if p.on_error == "drop":
                e["drop"] = True
            else:
                e["error"] = "message body is not JSON"
    return [e for e in events if not e.get("drop")]


class UnwrapSnsParams(P):
    """If the message is an SNS notification, continue with its inner Message (parsed as JSON)."""


def op_unwrap_sns(events: list[dict], p: UnwrapSnsParams, ctx: dict) -> list[dict]:
    for e in _ok(events):
        d = e.get("data")
        if isinstance(d, dict) and d.get("Type") == "Notification" and "Message" in d:
            try:
                e["data"] = json.loads(d["Message"])
            except (TypeError, json.JSONDecodeError):
                e["error"] = "SNS Message is not JSON"
    return events


class S3RecordsParams(P):
    event_names: list[str] = Field(default_factory=lambda: ["ObjectCreated:*"], description="globs on eventName")


def _object_vars(o: dict) -> dict[str, str]:
    key = o["key"]
    d, name = posixpath.split(key)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    t = o.get("time") or ""
    try:
        when = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        date, ts = when.strftime("%Y-%m-%d"), when.strftime("%Y%m%dT%H%M%S")
    except ValueError:
        date, ts = "", ""
    return {"event_bucket": o.get("bucket") or "", "event_key": key, "event_key_dir": d, "event_key_name": name,
            "event_key_stem": stem, "event_date": date, "event_date_nodash": date.replace("-", ""), "event_ts_nodash": ts}


S3_EVENT_VARS = sorted(_object_vars({"key": "a/b.c"}))


def op_s3_records(events: list[dict], p: S3RecordsParams, ctx: dict) -> list[dict]:
    """S3 event notifications: one event per record; keys URL-decoded ('+' is a space); s3:TestEvent dropped."""
    out = []
    for e in events:
        if e.get("error"):
            out.append(e)
            continue
        d = e.get("data")
        if isinstance(d, dict) and d.get("Event") == "s3:TestEvent":
            continue
        if not isinstance(d, dict) or not isinstance(d.get("Records"), list):
            out.append({**e, "error": "not an S3 event notification (no Records)"})
            continue
        for i, r in enumerate(d["Records"]):
            name = r.get("eventName", "")
            if not any(re.fullmatch(re.escape(g).replace(r"\*", ".*"), name) for g in p.event_names):
                continue
            s3 = r.get("s3") or {}
            obj = s3.get("object") or {}
            if "key" not in obj:
                out.append({**e, "id": f"{e['id']}:{i}", "error": "S3 record without object key"})
                continue
            o = {"bucket": (s3.get("bucket") or {}).get("name"), "key": unquote_plus(obj["key"]), "size": obj.get("size"),
                 "etag": obj.get("eTag"), "time": r.get("eventTime"), "sequencer": obj.get("sequencer")}
            out.append({**e, "id": f"{e['id']}:{i}", "objects": [o], "vars": {**e.get("vars", {}), **_object_vars(o)}})
    return out


class FilterParams(P):
    bucket: str | None = Field(default=None, description="keep only objects in this bucket (template over parameters)")
    include: list[str] = Field(default_factory=lambda: ["**"], description="globs on the object key")
    exclude: list[str] = Field(default_factory=list)
    regex: str | None = Field(default=None, description="full-match regex on the object key")
    min_size: int | None = Field(default=None, ge=0)
    max_size: int | None = Field(default=None, ge=0)
    _v = field_validator("regex")(classmethod(lambda cls, v: _rx(v)))


def op_filter(events: list[dict], p: FilterParams, ctx: dict) -> list[dict]:
    """Keep only objects that match; events left without objects are dropped (their messages are acknowledged)."""
    out = []
    bucket = render(p.bucket, ctx.get("params", {})) if p.bucket else None
    for e in events:
        if e.get("error"):
            out.append(e)
            continue
        keep = []
        for o in e.get("objects", []):
            k = o["key"]
            if bucket and o.get("bucket") != bucket:
                continue
            if not matches_any(p.include, k) or matches_any(p.exclude, k) or (p.regex and not re.fullmatch(p.regex, k)):
                continue
            if (p.min_size is not None and (o.get("size") or 0) < p.min_size) or (p.max_size is not None and (o.get("size") or 0) > p.max_size):
                continue
            keep.append(o)
        if keep:
            out.append({**e, "objects": keep})
    return out


class SetVarsParams(P):
    vars: dict[str, str] = Field(description="new variable → template over the event's variables (e.g. {event_key_stem})")


def op_set_vars(events: list[dict], p: SetVarsParams, ctx: dict) -> list[dict]:
    bad = [k for k in p.vars if k in BUILTIN_NAMES or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", k)]
    if bad:
        raise ValueError(f"variable names {bad} must be lower_snake_case and not built-ins")
    for e in _ok(events):
        v = {**ctx.get("params", {}), **e.get("vars", {})}
        e["vars"] = {**e.get("vars", {}), **{k: render(t, v) for k, t in p.vars.items()}}
    return events


class GroupParams(P):
    by: str = Field(default="", description="template over event variables; events with the same value become one package ('' = all)")
    var: str = Field(default="group", description="variable that receives the group value")


def op_group(events: list[dict], p: GroupParams, ctx: dict) -> list[dict]:
    groups: dict[str, dict] = {}
    for e in _ok(events):
        key = render(p.by, {**ctx.get("params", {}), **e.get("vars", {})}) if p.by else "all"
        g = groups.setdefault(key, {"id": f"group:{key}", "message_ids": [], "data": None, "objects": [],
                                    "vars": {**e.get("vars", {}), p.var: key}, "error": None})
        g["message_ids"] += [m for m in e.get("message_ids", []) if m not in g["message_ids"]]
        g["objects"] += e.get("objects", [])
    return [e for e in events if e.get("error")] + [groups[k] for k in sorted(groups)]


class DedupeParams(P):
    """Several events for the same objects: keep the latest (S3 sequencer, else last seen). Messages of dropped
    duplicates are acknowledged with the kept one. Events without objects pass through unchanged."""


def op_dedupe(events: list[dict], p: DedupeParams, ctx: dict) -> list[dict]:
    best: dict[tuple, dict] = {}
    order, plain = [], []
    for e in _ok(events):
        if not e.get("objects"):
            plain.append(e)
            continue
        o = e["objects"][0]
        k = tuple(sorted((x.get("bucket"), x.get("key")) for x in e["objects"]))
        cur = best.get(k)
        if cur is None:
            best[k] = e
            order.append(k)
            continue
        new_seq, old_seq = (o.get("sequencer") or ""), ((cur.get("objects") or [{}])[0].get("sequencer") or "")
        winner, loser = (e, cur) if new_seq.rjust(32, "0") >= old_seq.rjust(32, "0") else (cur, e)
        winner["message_ids"] = sorted(set(winner.get("message_ids", [])) | set(loser.get("message_ids", [])))
        best[k] = winner
    return [e for e in events if e.get("error")] + plain + [best[k] for k in order]


class RequireParams(P):
    max_events: int | None = Field(default=None, ge=1, description="more events than this fails the run")


def op_require(events: list[dict], p: RequireParams, ctx: dict) -> list[dict]:
    if p.max_events and len(_ok(events)) > p.max_events:
        raise PackageError(f"{len(_ok(events))} events (max_events {p.max_events})")
    return events


class CustomParams(P):
    function: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")


TRIGGER_OPS: dict[str, Op] = {o.name: o for o in [
    Op("schedule", ScheduleParams, _receive_placeholder, "RECEIVE (first step): one event per scheduled run."),
    Op("sqs", SqsParams, _receive_placeholder, "RECEIVE (first step): read up to max_messages SQS messages; a message is deleted only after all its packages are delivered."),
    Op("run_conf", RunConfParams, _receive_placeholder, "RECEIVE (first step): messages passed in the run configuration (Lambda, REST API, manual trigger)."),
    Op("parse_json", ParseJsonParams, op_parse_json, "Parse each message body as JSON (into the event's data)."),
    Op("unwrap_sns", UnwrapSnsParams, op_unwrap_sns, "If the data is an SNS notification, continue with its inner Message."),
    Op("s3_records", S3RecordsParams, op_s3_records, "S3 event notification → one event per record with the object (key URL-decoded) and variables "
       + ", ".join("{" + v + "}" for v in S3_EVENT_VARS) + "; drops s3:TestEvent."),
    Op("filter", FilterParams, op_filter, "Keep objects by bucket, key globs/regex and size; events left without objects are dropped."),
    Op("set_vars", SetVarsParams, op_set_vars, "Add variables computed from templates over the event's variables (used by package names, paths, prefixes)."),
    Op("group", GroupParams, op_group, "Merge events with the same template value into one package (e.g. all files of one batch)."),
    Op("dedupe", DedupeParams, op_dedupe, "Keep one event per object (latest by S3 sequencer)."),
    Op("require", RequireParams, op_require, "Fail the run when there are more events than max_events."),
    Op("custom", CustomParams, None, "Generated, reviewed function: (events, params, ctx) -> events."),
]}


def provides(steps: list[dict]) -> set[str]:
    """Variables the trigger steps add to every event."""
    out: set[str] = set()
    for s in steps:
        if s["op"] == "s3_records":
            out |= set(S3_EVENT_VARS)
        elif s["op"] == "set_vars":
            out |= set(s["params"].get("vars", {}))
        elif s["op"] == "group":
            out.add(s["params"].get("var", "group"))
    return out


def messages_to_events(messages: list[dict]) -> list[dict]:
    return [{"id": m["id"], "message_ids": [m["id"]], "body": m.get("body", ""), "data": None, "objects": [], "vars": {},
             "error": None} for m in messages]


def run_event_steps(events: list[dict], steps: list[dict], ctx: dict, custom: dict | None = None) -> list[dict]:
    """All steps after the receive step."""
    from .ops import parse_params_for

    for s in steps:
        if s["op"] in RECEIVE_OPS:
            continue
        model = parse_params_for(TRIGGER_OPS, s["op"], s["params"])
        if s["op"] == "custom":
            fn = (custom or {}).get(model.function)
            if fn is None:
                raise RuntimeError(f"custom step function '{model.function}' is not implemented")
            known = {m for e in events for m in e.get("message_ids", [])}
            try:
                events = fn(json.loads(json.dumps(events)), dict(ctx.get("params", {})), dict(ctx))
            except Exception as exc:  # noqa: BLE001 - generated code: a refusal, never a retry loop
                raise PackageError(f"custom step '{s['id']}': {type(exc).__name__}: {exc}") from None
            if not isinstance(events, list) or not all(isinstance(e, dict) and "id" in e for e in events):
                raise PackageError(f"custom step '{s['id']}' must return a list of events")
            for e in events:  # the framework owns which messages an event acknowledges
                ids = e.get("message_ids")
                if not ids or not set(ids) <= known:
                    raise PackageError(f"custom step '{s['id']}' returned event '{e['id']}' without the message_ids it came from")
        else:
            events = TRIGGER_OPS[s["op"]].run(events, model, ctx)
    for e in events:
        e.pop("body", None)
        e.setdefault("objects", [])
        e.setdefault("vars", {})
        e["vars"] = {k: str(v) for k, v in e["vars"].items()}
    return events


def receive_step(steps: list[dict]) -> dict:
    if not steps or steps[0]["op"] not in RECEIVE_OPS:
        raise ValueError(f"a trigger's first step must be one of {list(RECEIVE_OPS)}")
    return steps[0]


def normalize(events: list[dict]) -> list[dict]:
    """What examples compare: per good event, its object keys and variables."""
    return [{"keys": sorted(o["key"] for o in e.get("objects", [])), "vars": e.get("vars", {})} for e in events if not e.get("error")]

