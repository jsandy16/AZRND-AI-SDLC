"""Destination: where the finished package is delivered and under which prefix."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, field_validator, model_validator

from .common import API_VERSION, Credentials, Name, Params, Strict, Template
from .design import Designed, DestinationExample, unique_names
from .source import LocalConnection


class Delivery(Strict):
    prefix: Template = Field(default="{source}/{ds}/{package}/", description="Where the package's files go; templates allowed")
    if_exists: Literal["fail", "skip_identical", "overwrite"] = Field(
        default="skip_identical", description="An object already at the key: fail; skip if identical content (safe retries), "
        "otherwise fail; overwrite")
    server_side_encryption: Literal["AES256", "aws:kms"] | None = None
    kms_key_id: str | None = None
    write_success_marker: bool = Field(default=False, description="Write _SUCCESS after all files (in their common folder)")
    mode: Literal["staged", "stream"] = Field(
        default="staged", description="staged: download, stage, transform, upload; stream: each file is streamed from the source "
        "straight into a multipart upload (no transformation, no staging copy; files of any size; one failing file does not stop the others)")
    part_size_mb: int = Field(default=16, ge=5, le=5120, description="stream: multipart part size (MiB); larger for files that "
                              "would need more than 10 000 parts")
    max_concurrency: int = Field(default=8, ge=1, le=64, description="stream: parts uploaded in parallel")
    content_types: dict[str, str] = Field(default_factory=dict, description="stream: file extension (e.g. '.pdf') → Content-Type; "
                                          "other files get default_content_type")
    default_content_type: str = Field(default="application/octet-stream")
    verify_size: bool = Field(default=True, description="stream: HEAD each uploaded object and fail the file when its size differs "
                              "from the Content-Length the source declared")

    @model_validator(mode="after")
    def _stream(self) -> "Delivery":
        if self.mode == "stream":
            if self.if_exists == "skip_identical":
                raise ValueError("delivery.mode stream needs if_exists overwrite or fail (skip_identical needs the content's "
                                 "SHA-256 before the upload starts)")
            if self.write_success_marker:
                raise ValueError("delivery.mode stream does not write a success marker")
        return self


class S3DestinationConnection(Strict):
    """One bucket, or a bucket per package: bucket_map[render(bucket_by)] (exact match after trimming)."""

    bucket: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    bucket_by: Template | None = Field(default=None, description="template whose value picks the bucket, e.g. '{study_destination}'")
    bucket_map: dict[str, str] = Field(default_factory=dict, description="value of bucket_by → bucket")
    region: str | None = None
    endpoint_url: str | None = Field(default=None, description="S3-compatible endpoint (tests, MinIO)")

    @model_validator(mode="after")
    def _one_way(self) -> "S3DestinationConnection":
        if (self.bucket is None) == (not self.bucket_map):
            raise ValueError("give either bucket, or bucket_by with bucket_map")
        if self.bucket_map and not self.bucket_by:
            raise ValueError("bucket_map needs bucket_by (the template that picks the bucket)")
        return self


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
    connection: S3DestinationConnection

    @model_validator(mode="after")
    def _map_needs_stream(self) -> "S3Destination":
        if self.connection.bucket_map and self.delivery.mode != "stream":
            raise ValueError("connection.bucket_map (a bucket per package) needs delivery.mode: stream")
        return self


class LocalDestination(_Base):
    type: Literal["local"]
    connection: LocalConnection


Destination = Annotated[Union[S3Destination, LocalDestination], Field(discriminator="type")]
DESTINATION_TYPES = ("s3", "local")
