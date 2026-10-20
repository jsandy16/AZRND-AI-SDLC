"""Startup checks: a red/green list with the fix for each. Required ones stop the app (except the Setup page)."""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass

from . import STRICT_MODES
from .github import GitHubError
from .project import feasibility


@dataclass
class Check:
    name: str
    ok: bool
    required: bool
    detail: str = ""
    fix: str = ""


def branch_rules_ok(rules: list[dict], check_name: str = "pkf-gate") -> list[str]:
    """What the base branch's ruleset is missing for SIT/PPT/PRD (GET /rules/branches/{b})."""
    by = {}
    for r in rules or []:
        by.setdefault(r.get("type"), []).append(r.get("parameters") or {})
    missing = []
    prs = by.get("pull_request") or []
    if not prs:
        missing.append("pull requests required")
    else:
        p = prs[0]
        if int(p.get("required_approving_review_count", 0)) < 1:
            missing.append("at least 1 required approval")
        for key, label in (("dismiss_stale_reviews_on_push", "dismiss stale approvals"),
                           ("require_code_owner_review", "require review from code owners"),
                           ("require_last_push_approval", "approval of the most recent push")):
            if not any(x.get(key) for x in prs):
                missing.append(label)
    checks = [c.get("context") for p in by.get("required_status_checks") or [] for c in p.get("required_status_checks") or []]
    if check_name not in checks:
        missing.append(f"required status check `{check_name}`")
    if "non_fast_forward" not in by:
        missing.append("block force pushes")
    if "deletion" not in by:
        missing.append("restrict deletions")
    return missing


def token_permissions(mode: str) -> list[str]:
    """The fine-grained token permissions the Studio needs on the repository, by mode (C15)."""
    perms = ["Contents: read and write", "Pull requests: read and write", "Metadata: read"]
    if mode in STRICT_MODES:
        perms += ["Checks: read (the pkf-gate check)", "Workflows: read and write (the team PR adds .github/workflows/pkf-gate.yml)",
                  "Administration: read (the base branch's rulesets)"]
    return perms


def token_fix(mode: str) -> str:
    return ("set GITHUB_TOKEN (fine-grained, for this repository: " + "; ".join(token_permissions(mode)) +
            ") or run `gh auth login`; in an SSO organisation also authorize the token for it")


# Errors that mean "there are no usable AWS credentials" (E13): the fix is the same for all of them.
_NO_CREDENTIALS = ("NoCredentialsError", "TokenRetrievalError", "SSOTokenLoadError", "UnauthorizedSSOTokenError",
                   "PartialCredentialsError", "ProfileNotFound", "SSOError", "CredentialRetrievalError")
_BAD_TOKEN_CODES = ("ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "RequestExpired",
                    "UnrecognizedClientException", "SignatureDoesNotMatch")


def no_credentials_text(kind: str) -> str:
    profile = os.environ.get("AWS_PROFILE") or "<your profile>"
    return (f"no valid AWS credentials ({kind}): run aws sso login --profile {profile} "
            f"(or source ~/pkf/env.sh), then try again")


def aws_identity(region: str, account: str | None) -> tuple[bool, str, dict | None]:
    """Who the current AWS credentials are (STS get_caller_identity), with the fix when they are missing or expired.

    Returns (ok, message, identity): ok with "<arn> (account <a>)"; no or expired credentials → (False, "no valid AWS
    credentials (<type>): run aws sso login …", None); credentials for another account than the pinned one →
    (False, "your AWS credentials are for account <a>; the project is pinned to <b>", identity). Never raises."""
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover - boto3 ships with the runtime extra
        return False, f"cannot check AWS credentials: {exc} — install the extras: pip install -e '.[runtime,agent,studio]'", None
    try:
        # a fresh session: credentials from the environment as it is NOW (after `aws sso login`), never a cached default
        sts = boto3.session.Session().client("sts", region_name=region or None,
                           config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2}))
        ident = sts.get_caller_identity()
    except ClientError as exc:
        code = (exc.response.get("Error") or {}).get("Code") or type(exc).__name__
        if code in _BAD_TOKEN_CODES:
            return False, no_credentials_text(code), None
        return False, f"cannot check AWS credentials ({code}): {str(exc)[:300]}", None
    except BotoCoreError as exc:
        kind = type(exc).__name__
        if kind in _NO_CREDENTIALS or "credential" in kind.lower() or "token" in kind.lower():
            return False, no_credentials_text(kind), None
        return False, f"cannot reach AWS STS ({kind}): {str(exc)[:300]} — check the proxy (HTTPS_PROXY) and AWS_CA_BUNDLE", None
    except Exception as exc:  # noqa: BLE001 - a check never crashes the caller
        kind = type(exc).__name__
        if kind in _NO_CREDENTIALS:
            return False, no_credentials_text(kind), None
        return False, f"cannot check AWS credentials ({kind}): {str(exc)[:300]}", None
    ident = {k: ident.get(k) for k in ("Account", "Arn", "UserId")}
    if account and str(ident.get("Account")) != str(account):
        return False, f"your AWS credentials are for account {ident.get('Account')}; the project is pinned to {account}", ident
    return True, f"{ident.get('Arn')} (account {ident.get('Account')})", ident


_AWS_CACHE: dict[tuple, tuple[float, tuple[bool, str, dict | None]]] = {}
AWS_CACHE_S = 300.0


def aws_identity_cached(region: str, account: str | None, max_age: float = AWS_CACHE_S) -> tuple[bool, str, dict | None]:
    """aws_identity for the startup checks, cached 5 minutes per (region, account, credentials in the environment)."""
    env = tuple(os.environ.get(k) for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN",
                                             "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_ENDPOINT_URL"))
    key = (region, account, env)
    hit = _AWS_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    res = aws_identity(region, account)
    _AWS_CACHE[key] = (time.monotonic(), res)
    return res


def run(studio=None, root=None, error: str | None = None, *, aws: bool = True) -> list[Check]:
    out: list[Check] = []
    if studio is None:
        out.append(Check("git repository with a GitHub remote", False, True, error or "",
                         "open a clone of your specs repository: pkf studio PATH (it needs `git remote -v` to show github)"))
        return out
    s = studio
    out.append(Check("git repository with a GitHub remote", True, True, f"{s.owner}/{s.repo} on {s.host}"))
    out.append(Check("the repository has a commit", s.git.has_commit(), True, "", "make a first commit and push it"))
    offline = bool(getattr(s, "gh_network", False)) and s.gh is not None and s.login is None
    if offline:  # the token may be fine: GitHub cannot be reached right now (K9); the app opens read-only
        out.append(Check("GitHub token", False, False, f"GitHub unreachable: {s.gh_error} — read-only until it is reachable",
                         "check the proxy (HTTPS_PROXY), REQUESTS_CA_BUNDLE and `source ~/pkf/env.sh`, then Retry"))
    else:
        out.append(Check("GitHub token", s.gh is not None and s.login is not None, True,
                         f"@{s.login}" if s.login else "; ".join(s.warnings[-2:]), token_fix(s.cfg.mode)))
    if s.gh is not None and s.login:
        try:
            info = s.gh.repo_info()
            push = (info.get("permissions") or {}).get("push")
            out.append(Check("the token can push and open pull requests", bool(push), True, "",
                             "give the token Contents: write and Pull requests: write on this repository (and SSO-authorize it)"))
        except GitHubError as exc:
            if getattr(exc, "network", False):
                out.append(Check("the token can read the repository", False, False, f"GitHub unreachable: {exc}",
                                 "check the proxy, then Retry"))
            else:
                out.append(Check("the token can read the repository", False, True, str(exc),
                                 "check the token's repository access: " + "; ".join(token_permissions(s.cfg.mode))))
    llm_replay = os.environ.get("PKF_LLM_PROVIDER") == "replay"
    out.append(Check("Anthropic API key", bool(os.environ.get("ANTHROPIC_API_KEY")) or llm_replay, False,
                     "replaying recorded AI answers" if llm_replay else "",
                     "export ANTHROPIC_API_KEY=… in ~/pkf/env.sh (never in a file in the repository)"))
    out.append(Check("CA bundle for the proxy", bool(os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")), False,
                     os.environ.get("REQUESTS_CA_BUNDLE", ""), "source ~/pkf/env.sh (REQUESTS_CA_BUNDLE and AWS_CA_BUNDLE)"))
    out.append(node_ca_check())
    tf = os.environ.get("PKF_TERRAFORM") or shutil.which("terraform")
    out.append(Check("Terraform ≥ 1.10 (dev environment stage)", bool(tf), False, tf or "",
                     "download the Terraform zip from HashiCorp, check SHA256SUMS, put terraform.exe on PATH"))
    if aws:
        ok_aws, aws_msg, _ = aws_identity_cached(s.cfg.aws_region, s.cfg.aws_account_id)
        out.append(Check("AWS credentials (S5/S6)", ok_aws, False, aws_msg if ok_aws else "",
                         "" if ok_aws else aws_msg))
    out.append(Check("Studio project and team on the base branch", s.configured, True,
                     f"mode {s.cfg.mode}, {len(s.team.people)} people" if s.configured else "",
                     "Setup → write the project and team, open the setup PR, merge it"))
    out.append(Check("specs repository (pkf.toml) on the base branch", s.read_base("pkf.toml") is not None, True, "",
                     "the setup PR adds pkf.toml"))
    if s.configured:
        problems = feasibility(s.team, s.cfg.mode)
        out.append(Check("every gate can be approved with this team", not problems, True, "; ".join(problems),
                         "add people with the missing roles and GitHub logins (Setup → Team)"))
        ident = s.identity_problems()
        out.append(Check("who you are", not ident, True, s.me().name if s.me() else "", "; ".join(ident)))
        if s.cfg.mode in STRICT_MODES and s.gh is not None:
            try:
                missing = branch_rules_ok(s.gh.branch_rules(s.base))
                if missing and s.cfg.protection_confirmed_by:
                    br = s.gh.branch(s.base)
                    ok = bool(br.get("protected"))
                    out.append(Check(f"base branch protection ({s.cfg.mode})", ok, True,
                                     f"classic protection, confirmed by {s.cfg.protection_confirmed_by}" if ok else "the branch is not protected",
                                     "protect the base branch"))
                else:
                    out.append(Check(f"base branch rules ({s.cfg.mode})", not missing, True, ", ".join(missing),
                                     "Settings → Rules → Rulesets on the base branch: add what is listed"))
            except GitHubError as exc:
                out.append(Check(f"base branch rules ({s.cfg.mode})", False, not getattr(exc, "network", False), str(exc),
                                 "the token needs Metadata: read and Administration: read"))
    return out


def node_ca_check() -> Check:
    """Env-1: the AI agent runs on Node.js, which ignores REQUESTS_CA_BUNDLE; behind a TLS-inspecting proxy it needs
    NODE_EXTRA_CA_CERTS too."""
    req, node = os.environ.get("REQUESTS_CA_BUNDLE"), os.environ.get("NODE_EXTRA_CA_CERTS")
    ok = not (req and not node)
    return Check("Node CA for the AI (NODE_EXTRA_CA_CERTS)", ok, False, node or ("" if ok else f"REQUESTS_CA_BUNDLE={req}"),
                 "" if ok else 'export NODE_EXTRA_CA_CERTS="$REQUESTS_CA_BUNDLE" in ~/pkf/env.sh')


def blocking(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.required and not c.ok]
