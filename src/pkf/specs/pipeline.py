"""Connection (which source feeds which destination) and pipeline (connection + transformation + schedule)."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .common import API_VERSION, Name, Params, Strict, Template, check_schedule
from .design import PipelineExample


class ConnectionFile(Strict):
    api_version: Literal["pkf/v1"] = API_VERSION
    kind: Literal["connection"] = "connection"
    name: Name
    description: str = ""
    source: Name
    destination: Name
    check_before_run: bool = Field(default=True, description="Generate a first task that verifies both ends are reachable")


class PipelineFile(Strict):
    api_version: Literal["pkf/v1"] = API_VERSION
    kind: Literal["pipeline"] = "pipeline"
    name: Name
    description: str = ""
    owner: str = Field(default="data-engineering", min_length=1)
    requirement: str | None = Field(default=None, description="The whole pipeline in plain English: the AI splits it into "
                                    "trigger, source, transformation and destination requirements (pkf plan pipeline)")
    connection: Name | None = Field(default=None, description="Which source feeds which destination (set by the split when null)")
    trigger: Name | None = Field(default=None, description="How runs start; null = the `schedule` below")
    transformation: Name | None = Field(default=None, description="null = deliver the extracted files unchanged")
    package_name: Template = Field(default="{source}_{ds_nodash}", description="Initial package name ({package} in templates)")
    params: Params = Field(default_factory=dict, description="Defaults for template parameters; overridable per run (Airflow params)")
    schedule: str | None = Field(default=None, description="Shorthand when there is no trigger: cron or preset (@daily); null = manual")
    start_date: dt.date = Field(default=dt.date(2026, 1, 1))
    task_split: Literal["per_step", "single_transform"] = Field(
        default="per_step", description="per_step: one Airflow task per transformation step; single_transform: one task for all steps")
    retries: int = Field(default=2, ge=0, le=10)
    retry_delay_minutes: int = Field(default=5, ge=1, le=1440)
    max_active_runs: int = Field(default=1, ge=1, le=32)
    tags: list[str] = Field(default_factory=list)
    examples: list[PipelineExample] = Field(default_factory=list, description="End-to-end cases: input → delivered keys")

    _cron = field_validator("schedule")(classmethod(lambda cls, v: check_schedule(v)))

    @model_validator(mode="after")
    def _shape(self) -> "PipelineFile":
        if self.connection is None and self.requirement is None:
            raise ValueError("a pipeline needs a connection, or a requirement to split (pkf plan pipeline)")
        if self.trigger is not None and self.schedule is not None:
            raise ValueError("with a trigger, the schedule belongs in the trigger spec")
        names = [e.name for e in self.examples]
        if len(names) != len(set(names)):
            raise ValueError("example names must be unique")
        return self
