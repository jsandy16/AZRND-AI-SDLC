"""What every designed component shares: a plain-English `requirement` (planned by the AI, approved by a person) or
explicit catalogue `steps` (no AI), and examples that every plan and every piece of code is checked against.

Example formats by kind (structure only, never data):
  trigger      messages (SQS bodies, objects or strings) or conf → expected events (object keys + variables)
  source       an event + a source listing ("path:size[:age_seconds]") → expected packages (lists of package paths)
  destination  package files + variables → expected object keys
  pipeline     messages/conf + source listing → expected delivered keys (the whole pipeline, end to end)
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .common import Name, Strict

ID = r"^[a-z][a-z0-9_]{0,40}$"


class StepSpec(Strict):
    id: str = Field(pattern=ID)
    op: str = Field(min_length=1)
    title: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


LISTING_ITEM = re.compile(r"(.*?)(?::(\d+))?(?::(\d+(?:\.\d+)?))?$", re.S)  # size and age from the right: keys may hold ':'


def _listing(v: list[str]) -> list[str]:
    for item in v:
        path = LISTING_ITEM.match(item).group(1)
        if not path.strip("/") or path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"'{item}' must be 'relative/path[:size[:age_seconds]]' (a key ending in '/' is a folder marker)")
    return v


class _Example(Strict):
    name: Name
    description: str = ""
    params: dict[str, str] = Field(default_factory=dict, description="template values for this example (e.g. queue_url, ds)")
    expect_error: bool = Field(default=False, description="this case must be refused (the event/package fails)")


class ExpectedEvent(Strict):
    keys: list[str] = Field(default_factory=list, description="object keys of the event")
    vars: dict[str, str] = Field(default_factory=dict, description="variables the event must have (a subset is enough)")


class TriggerExample(_Example):
    messages: list[Any] = Field(default_factory=list, description="message bodies (JSON objects or strings) as received")
    conf: dict[str, Any] | None = Field(default=None, description="run configuration (run_conf triggers)")
    expected_events: list[ExpectedEvent] | None = Field(default=None, description="events in order; [] = all ignored")


class ExampleObject(Strict):
    key: str
    bucket: str | None = None
    size: int | None = None


class ExampleEvent(Strict):
    objects: list[ExampleObject] = Field(default_factory=list)
    vars: dict[str, str] = Field(default_factory=dict)


class SourceExample(_Example):
    event: ExampleEvent | None = Field(default=None, description="the event (null for scheduled runs)")
    listing: list[str] = Field(default_factory=list, description="files in the source: 'path[:size[:age_seconds]]' from the top of the source (S3: the full object key)")
    contents: dict[str, str] = Field(default_factory=dict, description="text of small control files, by path")
    expected_packages: list[list[str]] | None = Field(default=None, description="package paths of each package")
    _l = field_validator("listing")(classmethod(lambda cls, v: _listing(v)))


class DestinationExample(_Example):
    files: list[str] = Field(min_length=1, description="package paths")
    vars: dict[str, str] = Field(default_factory=dict, description="package variables (e.g. package, event_date)")
    expected_keys: list[str] | None = Field(default=None, description="every delivered object key (files not delivered are absent)")


class PipelineExample(_Example):
    messages: list[Any] = Field(default_factory=list)
    conf: dict[str, Any] | None = None
    listing: list[str] = Field(default_factory=list)
    contents: dict[str, str] = Field(default_factory=dict)
    expected_keys: list[str] | None = Field(default=None, description="every object key delivered by the run (without _SUCCESS)")
    _l = field_validator("listing")(classmethod(lambda cls, v: _listing(v)))


def unique_names(v: list) -> list:
    names = [e.name for e in v]
    if len(names) != len(set(names)):
        raise ValueError("example names must be unique")
    return v


class Designed(Strict):
    """Mixin: requirement (AI-planned) or steps (hand-written), plus examples."""

    requirement: str | None = Field(default=None, description="What this component must do, in plain English (planned by the AI)")
    steps: list[StepSpec] | None = Field(default=None, description="Catalogue steps written by hand instead of a requirement")

    @model_validator(mode="after")
    def _design(self):
        if self.requirement is not None and self.steps is not None:
            raise ValueError("give either a requirement (planned by the AI) or steps, not both")
        if self.requirement is not None:
            if len(self.requirement.strip()) < 10:
                raise ValueError("requirement is too short")
            if not getattr(self, "examples", None):
                raise ValueError("a requirement needs at least one example")
        if self.steps is not None:
            ids = [s.id for s in self.steps]
            if len(ids) != len(set(ids)) or not ids:
                raise ValueError("steps need unique ids")
        return self


Kinds = Literal["trigger", "source", "transformation", "destination"]
