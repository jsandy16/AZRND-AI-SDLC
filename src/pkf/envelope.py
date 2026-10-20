"""The one result shape every `pf ... --json` command returns (agents, hooks and CI parse this)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import VERSION


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning"] = "error"
    file: str = ""
    loc: str = Field(default="", description="Dotted path inside the file, e.g. entities.0.attributes.2.type")
    msg: str
    code: str = Field(default="invalid", description="Stable machine code, e.g. required, invalid, cross_ref, stale")


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: Literal["pkf/v1"] = "pkf/v1"
    command: str
    ok: bool
    pf_version: str = VERSION
    issues: list[Issue] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]


class PfError(Exception):
    """A user-facing failure with a stable code; the CLI turns it into a Result."""

    def __init__(self, msg: str, code: str = "error", issues: list[Issue] | None = None):
        super().__init__(msg)
        self.code = code
        self.issues = issues or []
