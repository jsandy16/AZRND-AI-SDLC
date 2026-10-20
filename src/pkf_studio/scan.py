"""Secret and personal-data scan of requirement text and of every file the Studio commits.

Secrets (private keys, AWS keys, GitHub/Anthropic/Slack tokens, password assignments, connection strings with
credentials) block. Personal data (e-mail addresses other than the team's, phone numbers, dates of birth, subject or
patient identifiers) needs the user to confirm it is not personal data before it is committed or sent to the AI.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SECRETS = [
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("AWS access key id", r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("AWS secret key", r"(?i)[\"']?(aws_?)?secret_?access_?key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}"),
    ("AWS session token", r"(?i)[\"']?(aws_?)?session_?token[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{100,}"),
    ("GitHub token", r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    ("Anthropic key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("Slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("password", r"(?i)\b(password|passwd|pwd|secret|api[_ -]?key|token)\b[\"']?\s*[:=]\s*['\"]?(?!<REQUIRED>|\{)[^\s'\",;]{6,}"),
    ("credentials in a URL", r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{3,}@"),
]
PII = [
    ("e-mail address", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("phone number", r"(?<!\d)(\+\d{1,3}[\s-]?)?\(?\d{2,4}\)?[\s-]\d{3,4}[\s-]\d{3,4}(?!\d)"),
    ("date of birth", r"(?i)\b(dob|date of birth|birth ?date)\b"),
    ("subject/patient identifier", r"(?i)\b(subject|patient|usubjid|subjid|mrn)\s*(id|no|number)?\s*[:=#]\s*\w+"),
]


@dataclass
class Finding:
    kind: str           # secret | pii
    what: str
    line: int
    excerpt: str


def _mask(s: str) -> str:
    return s[:4] + "…" if len(s) > 6 else "…"


def scan(text: str, allowed_emails: set[str] | None = None) -> list[Finding]:
    allowed = {e.lower() for e in (allowed_emails or set())}
    out: list[Finding] = []
    for i, line in enumerate((text or "").splitlines(), 1):
        for what, pat in SECRETS:
            for m in re.finditer(pat, line):
                out.append(Finding("secret", what, i, _mask(m.group(0))))
        for what, pat in PII:
            for m in re.finditer(pat, line):
                if what == "e-mail address" and m.group(0).lower() in allowed:
                    continue
                out.append(Finding("pii", what, i, _mask(m.group(0))))
    return out


def secrets_only(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.kind == "secret"]


def decode(data: bytes) -> str:
    """Text of a file whatever its encoding (UTF-8, UTF-16 with a BOM, else Latin-1), so nothing is skipped."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")
