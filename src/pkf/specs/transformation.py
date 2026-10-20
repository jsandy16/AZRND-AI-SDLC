"""Transformation: what to do to a package, written in plain English, anchored by examples.

The AI turns `requirement` into a plan of catalogue operations (and custom steps), the framework runs that plan on
the examples so people see exactly what happens, a person approves the plan, code is generated and tested on the
examples again, and a person approves the code. Plan, results and approvals live in <name>.lock.yaml; the code in
<name>.py.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

from .common import API_VERSION, Name, Strict, Params

ARCHIVE_SEP = "!/"  # "bundle.zip!/SITE_01/a.csv" = a member inside an archive in expected outputs


class Example(Strict):
    """A package before and after, by structure only (paths and optional sizes; contents are never needed).

    input:            ["SITE_0001/a.csv", "SITE_0001/readme.txt:120"]   (path, optionally ":size" in bytes)
    expected_output:  ["cro_20240301.zip", "cro_20240301.zip!/SITE_0001/a.csv", "manifest.json"]
    """

    name: Name
    description: str = ""
    input: list[str] = Field(min_length=1)
    params: dict[str, str] = Field(default_factory=dict, description="Template values for this example, e.g. ds_nodash")
    expected_output: list[str] | None = Field(default=None, description="Every output path; null = show only, nothing to compare")
    expect_error: bool = Field(default=False, description="The transformation must refuse this package")

    @field_validator("input", "expected_output")
    @classmethod
    def _paths(cls, v):
        for p in v or []:
            path = p.rsplit(":", 1)[0] if re.search(r":\d+$", p) else p
            if path.startswith("/") or ".." in path.split("/") or not path or path.endswith("/"):
                raise ValueError(f"'{p}' must be a relative file path without '..'")
        return v


class TransformationFile(Strict):
    api_version: Literal["pkf/v1"] = API_VERSION
    kind: Literal["transformation"] = "transformation"
    name: Name
    description: str = ""
    requirement: str = Field(min_length=10, description="The transformation in plain English")
    params: Params = Field(default_factory=dict, description="Default values for parameters the requirement uses")
    examples: list[Example] = Field(min_length=1, description="At least one; include a refusal case if the requirement checks structure")

    @field_validator("examples")
    @classmethod
    def _unique(cls, v):
        names = [e.name for e in v]
        if len(names) != len(set(names)):
            raise ValueError("example names must be unique")
        return v
