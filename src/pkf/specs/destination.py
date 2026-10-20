"""Destination: where the finished package is delivered and under which prefix."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, field_validator

from .common import API_VERSION, Credentials, Name, Params, Strict, Template
from .design import Designed, DestinationExample, unique_names
from .source import LocalConnection, S3Connection


class Delivery(Strict):
    prefix: Template = Field(default="{source}/{ds}/{package}/", description="Where the package's files go; templates allowed")
    if_exists: Literal["fail", "skip_identical", "overwrite"] = Field(
        default="skip_identical", description="An object already at the key: fail; skip if identical content (safe retries), "
        "otherwise fail; overwrite")
    server_side_encryption: Literal["AES256", "aws:kms"] | None = None
    kms_key_id: str | None = None
    write_success_marker: bool = Field(default=False, description="Write _SUCCESS after all files (in their common folder)")


class _Base(Designed):
    """Where each file goes: `delivery.prefix`, or a `requirement` (AI-planned) or `steps` for routing rules.
    Delivery policies (if_exists, encryption, marker) always come from `delivery`."""

    api_version: Literal["pkf/v1"] = API_VERSION
    kind: Literal["destination"] = "destination"
    name: Name
    description: str = ""
    credentials: Credentials = Field(default_factory=Credentials)
    delivery: Delivery = Field(default_factory=Delivery)
    params: Params = Field(default_factory=dict)
    examples: list[DestinationExample] = Field(default_factory=list)
    _u = field_validator("examples")(classmethod(lambda cls, v: unique_names(v)))


class S3Destination(_Base):
    type: Literal["s3"]
    connection: S3Connection


class LocalDestination(_Base):
    type: Literal["local"]
    connection: LocalConnection


Destination = Annotated[Union[S3Destination, LocalDestination], Field(discriminator="type")]
DESTINATION_TYPES = ("s3", "local")
