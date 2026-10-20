"""Source: where packages come from, how to connect, and which files to extract."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, field_validator, model_validator

from .common import API_VERSION, Credentials, Name, Params, Regex, Strict, Template
from .design import Designed, SourceExample, unique_names


class Extraction(Strict):
    """Which files form the package. Paths are relative to `root`; globs use / and ** (recursive)."""

    root: Template = Field(default="/", description="Folder to start from; templates allowed, e.g. '{input_path}'")
    recursive: bool = Field(default=True, description="Walk all sub-folders")
    max_depth: int = Field(default=20, ge=0, le=100, description="Folder levels below root to visit (0 = root only); bounds link loops")
    include: list[str] = Field(default_factory=lambda: ["**"], description="Glob patterns to keep, e.g. ['**/*.csv']")
    exclude: list[str] = Field(default_factory=list, description="Glob patterns to drop")
    regex: Regex | None = Field(default=None, description="Optional regex the relative path must fully match")
    min_age_seconds: int = Field(default=0, ge=0, description="Skip files modified more recently (still uploading)")
    min_files: int = Field(default=1, ge=0, description="Fail the run if fewer files are found")
    max_files: int | None = Field(default=None, ge=1)
    max_total_bytes: int | None = Field(default=None, ge=1)


class _Base(Designed):
    """Which files form each package: plain `extraction` settings, or a `requirement` (AI-planned) or `steps` for
    anything more complex (event-driven keys, control files, one package per folder …)."""

    api_version: Literal["pkf/v1"] = API_VERSION
    kind: Literal["source"] = "source"
    name: Name
    description: str = ""
    credentials: Credentials = Field(default_factory=Credentials)
    extraction: Extraction = Field(default_factory=Extraction)
    params: Params = Field(default_factory=dict, description="Default values for template parameters, e.g. input_path")
    examples: list[SourceExample] = Field(default_factory=list)
    _u = field_validator("examples")(classmethod(lambda cls, v: unique_names(v)))


class HttpsConnection(Strict):
    base_url: str = Field(pattern=r"^https?://", description="Server root, e.g. https://files.cro.example")
    listing: Literal["html_index", "webdav", "json_index"] = Field(
        default="html_index", description="How a folder is listed: html_index (Apache/nginx autoindex links), webdav "
        "(PROPFIND Depth 1), json_index (GET <folder>/ returns [{name, type: file|dir, size?}])")
    auth: Literal["none", "basic", "bearer"] = Field(default="none", description="basic: fields username/password; bearer: field token")
    verify_tls: bool = True
    ca_bundle: str | None = Field(default=None, description="Path to a PEM CA bundle (private CA)")
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    headers: dict[str, str] = Field(default_factory=dict, description="Extra request headers (no secrets)")

    @model_validator(mode="after")
    def _https(self) -> "HttpsConnection":
        if self.base_url.startswith("http://") and not self.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise ValueError("base_url must be https (http only for localhost test servers)")
        return self


class HttpsSource(_Base):
    type: Literal["https"]
    connection: HttpsConnection


class SftpConnection(Strict):
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    host_key: str | None = Field(default=None, description="Pinned server key 'ssh-ed25519 AAAA…'; required unless insecure_skip_host_key")
    insecure_skip_host_key: bool = Field(default=False, description="Development only")
    timeout_seconds: int = Field(default=60, ge=1, le=3600)

    @model_validator(mode="after")
    def _pinned(self) -> "SftpConnection":
        if not self.host_key and not self.insecure_skip_host_key:
            raise ValueError("set host_key (pinned) or insecure_skip_host_key: true (development only)")
        return self


class SftpSource(_Base):
    """Credential fields: username, and password or private_key (PEM)."""

    type: Literal["sftp"]
    connection: SftpConnection


class FtpConnection(Strict):
    host: str
    port: int = Field(default=21, ge=1, le=65535)
    tls: Literal["explicit", "none"] = "explicit"
    passive: bool = True
    ca_bundle: str | None = None
    timeout_seconds: int = Field(default=60, ge=1, le=3600)


class FtpSource(_Base):
    """Credential fields: username, password."""

    type: Literal["ftp"]
    connection: FtpConnection


class S3Connection(Strict):
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    region: str | None = None
    endpoint_url: str | None = Field(default=None, description="S3-compatible endpoint (tests, MinIO)")


class S3Source(_Base):
    """Credential fields (optional): aws_access_key_id, aws_secret_access_key, aws_session_token. Omit to use the
    worker's role (default AWS credential chain)."""

    type: Literal["s3"]
    connection: S3Connection


class EntimiceConnection(Strict):
    base_url: str = Field(pattern=r"^https?://", description="Entimice REST root, e.g. https://entimice.example/api")
    verify_tls: bool = Field(default=True, description="VERIFY_SSL")
    ca_bundle: str | None = Field(default=None, description="Path to a PEM CA bundle (private CA)")
    account_by: Template | None = Field(default=None, description="template whose value picks the account, e.g. '{study_destination}'")
    accounts: dict[str, Template] = Field(default_factory=dict, description="value of account_by (case-insensitive, trimmed) → "
                                          "secret ref of that account (same provider as `credentials`); other values use the default account")

    @model_validator(mode="after")
    def _https(self) -> "EntimiceConnection":
        if self.base_url.startswith("http://") and not self.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise ValueError("base_url must be https (http only for localhost test servers)")
        if self.accounts and not self.account_by:
            raise ValueError("accounts needs account_by (the template that picks the account)")
        return self


class EntimiceSource(_Base):
    """Credential fields: username, password (the default account; `connection.accounts` names the others)."""

    type: Literal["entimice"]
    connection: EntimiceConnection


class LocalConnection(Strict):
    base_path: str = Field(description="Folder on the worker (development and tests)")


class LocalSource(_Base):
    type: Literal["local"]
    connection: LocalConnection


Source = Annotated[Union[HttpsSource, SftpSource, FtpSource, S3Source, LocalSource, EntimiceSource], Field(discriminator="type")]
SOURCE_TYPES = ("https", "sftp", "ftp", "s3", "local", "entimice")
