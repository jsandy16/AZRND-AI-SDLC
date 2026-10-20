"""Destination catalogue: where each file of a finished package goes.

Steps compute an object key for every package file; the framework then delivers them with the destination's
policies (if_exists, encryption, success marker), which stay configuration, never generated code.

Layout: {"files": [package paths], "keys": {path: key or None (None = do not deliver)}, "vars": {...}}
Templates may use the run variables plus per-file {path} {dir} {name} {stem} {suffix} and regex groups.

A plan may end with one `custom_delivery` step: then a generated function delivers the files itself (with AWS
clients: moves, server-side copies, tags …) instead of the framework's upload; see pkf_runtime.delivery.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .ops import Op, P, _file_vars, _rx
from .package import PackageError, matches_any, render, safe_relpath


class PrefixParams(P):
    prefix: str = Field(default="{source}/{ds}/{package}/", description="key prefix template; each file keeps its package path below it")


def op_prefix(layout: dict, p: PrefixParams, ctx: dict) -> dict:
    pre = render(p.prefix, layout["vars"]).strip("/")
    for f in layout["files"]:
        if f not in layout["keys"]:
            layout["keys"][f] = f"{pre}/{f}" if pre else f
    return layout


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include: list[str] = Field(default_factory=lambda: ["**"], description="globs on the package path")
    regex: str | None = Field(default=None, description="optional full-match regex; named groups become variables")
    to: str | None = Field(description="key template, or null to not deliver matching files")
    _v = field_validator("regex")(classmethod(lambda cls, v: _rx(v)))


class RouteParams(P):
    rules: list[Rule] = Field(min_length=1, description="first matching rule wins")
    unmatched: Literal["leave", "fail", "skip"] = Field(default="leave", description="files no rule matches: leave for a later step, fail, or not deliver")


def op_route(layout: dict, p: RouteParams, ctx: dict) -> dict:
    for f in layout["files"]:
        if f in layout["keys"]:
            continue
        for r in p.rules:
            m = re.fullmatch(r.regex, f) if r.regex else None
            if matches_any(r.include, f) and (r.regex is None or m):
                extra = {**_file_vars(f), **({k: v for k, v in m.groupdict().items() if v is not None} if m else {})}
                layout["keys"][f] = None if r.to is None else render(r.to, {**layout["vars"], **extra})
                break
        else:
            if p.unmatched == "fail":
                raise PackageError(f"no delivery rule matches '{f}'")
            if p.unmatched == "skip":
                layout["keys"][f] = None
    return layout


class CustomParams(P):
    function: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")


DESTINATION_OPS: dict[str, Op] = {o.name: o for o in [
    Op("prefix", PrefixParams, op_prefix, "Every file not yet placed goes to <prefix template>/<package path>."),
    Op("route", RouteParams, op_route, "Rules (globs/regex → key template, or null to skip) place files; first match wins."),
    Op("custom", CustomParams, None, "Generated, reviewed function: (layout, params, ctx) -> layout; set layout['keys'][path] = key."),
    Op("custom_delivery", CustomParams, None, "Last step only. Generated, reviewed function that delivers the files itself instead "
       "of the framework's upload: (transfers, params, ctx, aws) -> results. Use for moves (delete the source after the copy), "
       "server-side copies, object tags or any other S3/AWS operation at delivery. Source must be S3; no transformation."),
]}


def run_layout_steps(files: list[str], variables: dict, steps: list[dict], ctx: dict, custom: dict | None = None) -> dict[str, str | None]:
    """Every file's key (None = not delivered). See run_layout for the layout variables as well."""
    return run_layout(files, variables, steps, ctx, custom)[0]


def run_layout(files: list[str], variables: dict, steps: list[dict], ctx: dict,
               custom: dict | None = None) -> tuple[dict[str, str | None], dict]:
    """(keys, layout vars after the steps). Values an earlier step put in layout["vars"] (e.g. "reason:<path>" for the
    delivery report) reach a custom_delivery function in its params (K13)."""
    from .ops import parse_params_for

    layout = {"files": sorted(files), "keys": {}, "vars": dict(variables)}
    for s in steps:
        model = parse_params_for(DESTINATION_OPS, s["op"], s["params"])
        if s["op"] == "custom_delivery":
            continue  # delivers the files; it does not place them
        if s["op"] == "custom":
            fn = (custom or {}).get(model.function)
            if fn is None:
                raise RuntimeError(f"custom step function '{model.function}' is not implemented")
            try:
                layout = fn(json.loads(json.dumps(layout)), dict(ctx.get("params", {})), dict(ctx))
            except Exception as exc:  # noqa: BLE001 - generated code: this package fails, no retry loop
                raise PackageError(f"custom step '{s['id']}': {type(exc).__name__}: {exc}") from None
            if not isinstance(layout, dict) or not isinstance(layout.get("keys"), dict):
                raise PackageError(f"custom step '{s['id']}' must return the layout with its 'keys'")
        else:
            layout = DESTINATION_OPS[s["op"]].run(layout, model, ctx)
    keys = layout["keys"]
    unplaced = [f for f in layout["files"] if f not in keys]
    if unplaced:
        raise PackageError(f"{len(unplaced)} file(s) have no destination key, e.g. '{unplaced[0]}'")
    seen: dict[str, str] = {}
    for f in layout["files"]:
        k = keys[f]
        if k is None:
            continue
        k = safe_relpath(k.strip("/"))
        if k in seen:
            raise PackageError(f"'{f}' and '{seen[k]}' would both be delivered to '{k}'")
        seen[k] = f
        keys[f] = k
    lvars = layout.get("vars")
    return {f: keys[f] for f in layout["files"]}, dict(lvars) if isinstance(lvars, dict) else {}


def default_steps(delivery: dict) -> list[dict]:
    return [{"id": "under_prefix", "op": "prefix", "title": "Deliver under the configured prefix",
             "params": {"prefix": (delivery or {}).get("prefix", "{source}/{ds}/{package}/")}}]
