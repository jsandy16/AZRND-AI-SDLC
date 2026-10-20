"""Project configuration and team roster — always read from the BASE branch tip, never from a pull request.

.pkf-studio/project.yaml   mode, base branch, GitHub API, AWS account/region, tags, run role
.pkf-studio/team.yaml      people: name, email, roles, GitHub login (+ numeric id)
.pkf-studio/local.yaml     (git-ignored) who is using this checkout
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import MODES, ROLES, STUDIO_DIR

PROJECT_FILE = f"{STUDIO_DIR}/project.yaml"
TEAM_FILE = f"{STUDIO_DIR}/team.yaml"
LOCAL_FILE = f"{STUDIO_DIR}/local.yaml"
GITIGNORE = [f"{STUDIO_DIR}/local.yaml", f"{STUDIO_DIR}/jobs/", f"{STUDIO_DIR}/worktrees/", f"{STUDIO_DIR}/drafts/",
             f"{STUDIO_DIR}/locks/", f"{STUDIO_DIR}/terraform/", f"{STUDIO_DIR}/cache/", f"{STUDIO_DIR}/flows/", "*.tfplan", ".terraform/",
             "__pycache__/", "*.pyc"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Person(_M):
    name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    roles: list[Literal["product_owner", "business_analyst", "architect", "data_engineer"]] = Field(min_length=1)
    github: str | None = Field(default=None, description="GitHub login (needed to approve)")
    github_id: int | None = Field(default=None, description="numeric GitHub user id (logins can be renamed)")

    @field_validator("github")
    @classmethod
    def _login(cls, v):
        if v in (None, ""):
            return None
        v = v.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:_[A-Za-z0-9]+)?", v):
            raise ValueError(f"'{v}' is not a GitHub login")
        return v


class Team(_M):
    people: list[Person] = Field(default_factory=list)

    @field_validator("people")
    @classmethod
    def _unique(cls, v):
        for attr in ("email", "github", "github_id"):
            seen = [getattr(p, attr) for p in v if getattr(p, attr) is not None]
            if len(seen) != len({str(x).lower() for x in seen}):
                raise ValueError(f"two people share the same {attr}")
        return v

    def by_id(self, gid: int | None) -> Person | None:
        return next((p for p in self.people if gid is not None and p.github_id == gid), None)

    def by_login(self, login: str | None) -> Person | None:
        return next((p for p in self.people if login and p.github and p.github.lower() == login.lower()), None)

    def by_email(self, email: str | None) -> Person | None:
        return next((p for p in self.people if email and p.email.lower() == email.lower()), None)

    def with_role(self, role: str) -> list[Person]:
        return [p for p in self.people if role in p.roles]


class ProjectConfig(_M):
    mode: Literal["dev", "sit", "ppt", "prd"] = "dev"
    base_branch: str = "main"
    github_api: str = Field(default="https://api.github.com", description="Enterprise: https://HOST/api/v3")
    aws_account_id: str | None = Field(default=None, pattern=r"^\d{12}$")
    aws_region: str = "eu-north-1"
    tags: dict[str, str] = Field(default_factory=dict, description="mandatory resource tags")
    run_role_arn: str | None = Field(default=None, description="role pkf run assumes (session policy = the generated worker policy)")
    terraform_backend: dict[str, str] = Field(default_factory=dict, description="S3 backend: bucket, region (key is per pipeline)")
    protection_confirmed_by: str | None = Field(default=None, description="repo admin who confirmed classic branch protection")
    code_owners: list[str] = Field(default_factory=list, description="CODEOWNERS for the roster, config, .github/ and tools/ "
                                   "(an admin-managed team such as @org/pkf-admins)")

    @field_validator("mode")
    @classmethod
    def _mode(cls, v):
        assert v in MODES
        return v


def dump(model: BaseModel) -> str:
    return yaml.safe_dump(model.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True)


def parse_config(text: str | None) -> ProjectConfig | None:
    return None if not text else ProjectConfig.model_validate(yaml.safe_load(text) or {})


def parse_team(text: str | None) -> Team | None:
    return None if not text else Team.model_validate(yaml.safe_load(text) or {})


def read_local(root: Path) -> dict:
    p = root / LOCAL_FILE
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.exists() else {}
    except yaml.YAMLError:
        return {}


def write_local(root: Path, data: dict) -> None:
    p = root / LOCAL_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")


def feasibility(team: Team, mode: str, author_login: str | None = None) -> list[str]:
    """Which gates cannot be satisfied with this roster (strict modes: distinct people, author excluded)."""
    from .gates import GATES, assign

    problems = []
    for g in GATES.values():
        people = [p for p in team.people if p.github and (mode == "dev" or (p.github_id is not None and
                                                                            (author_login or "").lower() != (p.github or "").lower()))]
        cands = [(p, set(p.roles) & set(g.roles)) for p in people]
        if assign(list(g.roles), cands, distinct=mode != "dev") is None:
            need = ", ".join(g.roles)
            problems.append(f"{g.id} {g.title} needs {need}" + (" (different people, not the author)" if mode != "dev" else "")
                            + " with GitHub logins" + ("" if mode == "dev" else " and numeric GitHub ids"))
    return problems


ROLES_ALL = ROLES
