"""Building blocks shared by every spec kind."""
from __future__ import annotations

import re
import string
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

REQUIRED = "<REQUIRED>"
API_VERSION = "pkf/v1"


class Strict(BaseModel):
    """Unknown keys are errors: a typo never becomes a silently ignored setting."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=63)]


def _regex(v: str) -> str:
    try:
        re.compile(v)
    except re.error as exc:
        raise ValueError(f"not a valid regular expression: {exc}") from None
    return v


Regex = Annotated[str, AfterValidator(_regex)]

# Variables every template can use. {name} placeholders are resolved at run time; unknown names are errors.
BUILTIN_VARS = {
    "pipeline": "pipeline name",
    "source": "source name",
    "destination": "destination name",
    "connection": "connection name",
    "trigger": "trigger name",
    "package": "package name (after any rename step)",
    "run_id": "Airflow run id, made safe for paths",
    "ds": "logical date YYYY-MM-DD",
    "ds_nodash": "logical date YYYYMMDD",
    "ts_nodash": "logical timestamp YYYYMMDDTHHMMSS",
    "env": "environment name (PKF_ENV, default dev)",
}


def template_fields(template: str) -> set[str]:
    """Names used in a {placeholder} template (format-spec and conversions are not allowed)."""
    names = set()
    for _, field, spec, conv in string.Formatter().parse(template):
        if field is None:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field) or spec or conv:
            raise ValueError(f"template field '{{{field}}}' must be a plain name like {{ds}}")
        names.add(field)
    return names


def _template(v: str) -> str:
    template_fields(v)
    return v


Template = Annotated[str, AfterValidator(_template)]


def _params(d: dict[str, str]) -> dict[str, str]:
    for k in d:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", k):
            raise ValueError(f"parameter name '{k}' must be lower_snake_case (letters, digits, _)")
        if k in BUILTIN_VARS:
            raise ValueError(f"parameter '{k}' would shadow the built-in variable {{{k}}}; choose another name")
        if k.startswith("event_"):
            raise ValueError(f"parameter '{k}': names starting with event_ are reserved for event variables")
    return d


Params = Annotated[dict[str, str], AfterValidator(_params)]
"""Template parameter defaults: lower_snake_case names that never shadow a built-in variable."""


class Credentials(Strict):
    """Where the secret values for a connection come from. The spec only says where to look, never the values.

    provider:
      aws_secrets_manager  ref = secret id/ARN; SecretString must be a JSON object
      vault                ref = path in a KV v2 mount; token from VAULT_TOKEN (address from `vault_address` or VAULT_ADDR)
      local_file           ref = path to a JSON or YAML file (development only)
      env                  ref = prefix; field `password` is read from <PREFIX>PASSWORD
      none                 no credentials (anonymous, or the default AWS credential chain for S3)
    `ref` may use {env}, so one spec serves dev and prod: `rd/{env}/cro_https`.
    """

    provider: Literal["aws_secrets_manager", "vault", "local_file", "env", "none"] = "none"
    ref: Template | None = Field(default=None, description="Secret id, Vault path, file path or env prefix")
    region: str | None = Field(default=None, description="AWS region (aws_secrets_manager)")
    vault_address: str | None = Field(default=None, description="Vault URL; default VAULT_ADDR")
    vault_mount: str = Field(default="secret", description="KV v2 mount point")
    fields: dict[str, str] = Field(default_factory=dict, description="Connection field -> key in the secret, when names differ")

    @model_validator(mode="after")
    def _ref_needed(self) -> "Credentials":
        if self.provider != "none" and not self.ref:
            raise ValueError(f"credentials.provider {self.provider} needs `ref`")
        return self


class ApprovalRecord(Strict):
    by: str = Field(pattern=r"^@[\w.-]+(/[\w.-]+)?$")
    identity: str | None = None
    at: str
    digest: str


Kind = Literal["trigger", "source", "destination", "transformation", "connection", "pipeline"]


CRON_FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7)]
CRON_NAMES = {"month": "jan feb mar apr may jun jul aug sep oct nov dec".split(), "day of week": "sun mon tue wed thu fri sat".split()}


def _cron_field(field: str, name: str, lo: int, hi: int) -> None:
    def num(x: str) -> int:
        names = CRON_NAMES.get(name, [])
        if x.lower() in names:
            return names.index(x.lower()) + (1 if name == "month" else 0)
        if not x.isdigit() or not lo <= int(x) <= hi:
            raise ValueError(f"cron {name} value '{x}' must be {lo}-{hi}")
        return int(x)

    for part in field.split(","):
        base, _, step = part.partition("/")
        if step and (not step.isdigit() or int(step) == 0):
            raise ValueError(f"cron {name} step '{step}' must be a positive number")
        if base == "*":
            continue
        a, _, b = base.partition("-")
        if b and num(a) > num(b) or not b and num(a) is None:
            raise ValueError(f"cron {name} range '{base}' is reversed")


def check_schedule(v: str | None) -> str | None:
    """None (manual runs), a preset (@daily …) or a valid 5-field cron expression."""
    if v is None or v.startswith("@"):
        if v not in (None, "@once", "@hourly", "@daily", "@weekly", "@monthly", "@yearly"):
            raise ValueError("preset must be one of @once @hourly @daily @weekly @monthly @yearly")
        return v
    fields = v.split()
    if len(fields) != 5:
        raise ValueError("schedule must be a 5-field cron expression or a preset like @daily")
    for f, (name, lo, hi) in zip(fields, CRON_FIELDS):
        _cron_field(f, name, lo, hi)
    return v


NEW_FIELD_DEFAULTS = {  # settings added after the AI fixtures were recorded, with their defaults (Entimice, stream delivery)
    "delivery": {"mode": "staged", "part_size_mb": 16, "max_concurrency": 8, "content_types": {},
                 "default_content_type": "application/octet-stream", "verify_size": True},
    "connection": {"bucket_by": None, "bucket_map": {}},
}


def without_new_defaults(d: dict) -> dict:
    """A component's settings with the newer settings left out while they are at their default: what the AI roles see
    and what pipeline locks record, so prompts and locks of components that do not use them stay exactly as they were."""
    out = dict(d)
    for section, defaults in NEW_FIELD_DEFAULTS.items():
        if isinstance(out.get(section), dict):
            out[section] = {k: v for k, v in out[section].items() if not (k in defaults and v == defaults[k])}
    return out
