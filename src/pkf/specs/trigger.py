"""Trigger: how a pipeline run starts and what it ingests (a schedule, SQS event notifications, a Lambda or REST
call …), as a plain-English requirement or explicit catalogue steps."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .common import API_VERSION, Credentials, Name, Params, Strict, check_schedule
from .design import Designed, TriggerExample, unique_names


class TriggerConnection(Strict):
    region: str | None = Field(default=None, description="AWS region of the queue (sqs)")
    endpoint_url: str | None = Field(default=None, description="SQS-compatible endpoint (tests)")


class TriggerFile(Designed):
    api_version: Literal["pkf/v1"] = API_VERSION
    kind: Literal["trigger"] = "trigger"
    name: Name
    description: str = ""
    schedule: str | None = Field(default=None, description="When the DAG runs (polls): cron or preset; null = only when "
                                 "started by hand or the API (run_conf: Lambda, REST)")
    connection: TriggerConnection = Field(default_factory=TriggerConnection)
    credentials: Credentials = Field(default_factory=Credentials, description="AWS keys for SQS (optional; default chain)")
    params: Params = Field(default_factory=dict, description="Defaults for variables the trigger uses (e.g. queue_url)")
    examples: list[TriggerExample] = Field(default_factory=list)

    _cron = field_validator("schedule")(classmethod(lambda cls, v: check_schedule(v)))
    _u = field_validator("examples")(classmethod(lambda cls, v: unique_names(v)))

    @model_validator(mode="after")
    def _how(self) -> "TriggerFile":
        if self.requirement is None and self.steps is None and self.schedule is None:
            raise ValueError("a trigger needs a schedule (plain settings), a requirement (planned by the AI) or explicit steps")
        return self
