"""Resolve a component's credentials at run time. Specs only say where to look; values never enter specs, locks,
XComs or logs.

AWS Secrets Manager: GetSecretValue; SecretString must be a JSON object.
HashiCorp Vault:     KV version 2, GET {address}/v1/{mount}/data/{path}; token from VAULT_TOKEN.
local_file:          JSON or YAML file (development only).
env:                 field `x` read from environment variable <PREFIX>X.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .package import render


class CredentialError(Exception):
    """Credentials could not be resolved (configuration problem: no retry)."""


def resolve(spec: dict[str, Any] | None, variables: dict[str, Any]) -> dict[str, str]:
    spec = spec or {}
    provider = spec.get("provider", "none")
    if provider == "none":
        return {}
    ref = render(spec["ref"], variables)
    if provider == "aws_secrets_manager":
        data = _aws(ref, spec.get("region"))
    elif provider == "vault":
        data = _vault(ref, spec.get("vault_address") or os.environ.get("VAULT_ADDR"), spec.get("vault_mount", "secret"))
    elif provider == "local_file":
        data = _local(ref)
    elif provider == "env":
        data = {k[len(ref):].lower(): v for k, v in os.environ.items() if k.startswith(ref)}
    else:
        raise CredentialError(f"unknown credentials provider '{provider}'")
    mapping = spec.get("fields") or {}
    out = {k: str(v) for k, v in data.items()}
    for field, key in mapping.items():
        if key not in data:
            raise CredentialError(f"secret '{ref}' has no key '{key}' (mapped to '{field}')")
        out[field] = str(data[key])
    return out


def _aws(secret_id: str, region: str | None) -> dict:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        kw = {"region_name": region} if region else {}
        if os.environ.get("PKF_AWS_ENDPOINT_URL"):  # tests and local stand-ins only
            kw["endpoint_url"] = os.environ["PKF_AWS_ENDPOINT_URL"]
        resp = boto3.client("secretsmanager", **kw).get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ThrottlingException", "InternalServiceError", "InternalFailure", "ServiceUnavailable", "RequestTimeout"):
            raise ConnectionError(f"AWS Secrets Manager temporarily unavailable ({code})") from None
        raise CredentialError(f"AWS Secrets Manager: cannot read '{secret_id}' ({code or type(exc).__name__})") from None
    except BotoCoreError as exc:
        if type(exc).__name__ in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError", "ConnectionClosedError"):
            raise ConnectionError(f"AWS Secrets Manager unreachable ({type(exc).__name__})") from None
        raise CredentialError(f"AWS Secrets Manager: cannot read '{secret_id}' ({type(exc).__name__})") from None
    try:
        data = json.loads(resp.get("SecretString") or "")
    except json.JSONDecodeError:
        raise CredentialError(f"AWS Secrets Manager secret '{secret_id}' is not a JSON object") from None
    if not isinstance(data, dict):
        raise CredentialError(f"AWS Secrets Manager secret '{secret_id}' is not a JSON object")
    return data


def _vault(path: str, address: str | None, mount: str) -> dict:
    token = os.environ.get("VAULT_TOKEN")
    if not address or not token:
        raise CredentialError("Vault needs an address (vault_address or VAULT_ADDR) and VAULT_TOKEN")
    url = f"{address.rstrip('/')}/v1/{mount.strip('/')}/data/{path.strip('/')}"
    req = urllib.request.Request(url, headers={"X-Vault-Token": token, "Accept": "application/json"})
    if os.environ.get("VAULT_NAMESPACE"):
        req.add_header("X-Vault-Namespace", os.environ["VAULT_NAMESPACE"])
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            doc = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code >= 500 or exc.code == 429:  # Vault sealed / standby / overloaded: worth a retry
            raise ConnectionError(f"Vault temporarily unavailable (HTTP {exc.code})") from None
        raise CredentialError(f"Vault: cannot read '{mount}/{path}' (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ConnectionError(f"Vault unreachable ({getattr(exc, 'reason', exc)})") from None
    data = (doc.get("data") or {}).get("data")
    if not isinstance(data, dict):
        raise CredentialError(f"Vault: '{mount}/{path}' is not a KV v2 secret")
    return data


def _local(path: str) -> dict:
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        raise CredentialError(f"local credentials file '{path}' not found")
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise CredentialError(f"local credentials file '{path}' must hold an object")
    return data


def mask(text: str, secrets: dict[str, str]) -> str:
    for v in sorted(secrets.values(), key=len, reverse=True):
        if v and len(v) > 3:
            text = text.replace(v, "***")
    return text
