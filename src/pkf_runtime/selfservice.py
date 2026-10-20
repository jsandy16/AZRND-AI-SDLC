"""Self-Service Portal (SSAPI) control lists: which records are due, and the result written back to each record.

A pipeline whose trigger receives with `selfservice` reads the submissions of one portal form (one record per study),
keeps the records that are due for a refresh (`selfservice_due`), runs one package per due record, and in its `ack`
task writes each due record's result back to the portal (one PUT per record). The portal record is the only state:
the consecutive-failure count lives in the record's error text.

Nothing here logs the api key.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import ssl
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("pkf")

ACCEPT = "application/vnd.dpl-selfservice.v1+json"
TIMEOUT_S = 60
DATE_OUT = "%Y-%m-%d %I:%M %p"
DATE_IN = ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
           "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M%p", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y")
COMPLETED = ("DONE", "COMPLETED")
STOPPED = "FAILED_INGESTION_STOPPED"
INTERVAL_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}
FAILURE_LIMITS = {"once": 10, "daily": 15}  # weekly and monthly: no limit
DISABLED_TEXT = "so disabling the trigger for this study"
COUNT_RX = re.compile(r"Consecutive failed runs:\s*(\d+)")

EMPTY_LISTING = "Empty data from the Entimice API."
NONE_UPLOADED = "Files were found in Entimice but none was uploaded to S3."
MISSING_LOCATION = "Missing location in submission_data"

REASONS = [  # first match wins (case-insensitive)
    (("403", "forbidden", "denied", "unauthor"), "Access issue"),
    (("404", "does not exist", "not found", "could not resolve"), "File missing issue"),
    (("empty content", "corrupt", "unreadable"), "File corrupt issue"),
    (("empty data", "no data"), "No data available issue"),
    (("timed out", "timeout"), "Timeout issue"),
    (("destination", "studyid"), "Configuration issue"),
]


class SelfServiceError(Exception):
    """The control list could not be read (the run fails at once, before anything is read or written)."""


# ---------------------------------------------------------------------------------------------- texts
def reason(text: str) -> str:
    low = (text or "").lower()
    for needles, name in REASONS:
        if any(n in low for n in needles):
            return name
    return "Other issue"


def with_reason(text: str) -> str:
    return f"{text} | Reason: {reason(text)}"


def clean(value: Any) -> str:
    """A text field compared or used after trimming; stray tabs anywhere are removed."""
    return "" if value is None else str(value).replace("\t", "").strip()


def parse_when(value: Any) -> dt.date | None:
    """The calendar date (UTC) of a lastTransferAt value, or None when blank or unreadable."""
    text = re.sub(r"\s+", " ", clean(value))
    if not text:
        return None
    for fmt in DATE_IN:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when.astimezone(dt.timezone.utc) if when.tzinfo else when).date()


def fmt_now(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).strftime(DATE_OUT)


# ---------------------------------------------------------------------------------------------- due
def due(data: dict, today: dt.date, force: bool = False) -> tuple[bool, str]:
    """(due, reason when not due) for one submission's submission_data."""
    status = clean(data.get("lastTransferStatus")).upper()
    schedule = clean(data.get("refreshSchedule")).lower()
    if status == STOPPED:
        return False, "ingestion_stopped"
    completed = status in COMPLETED and not force
    if schedule == "once":
        return (True, "") if not completed else (False, "completed_once")
    if schedule not in INTERVAL_DAYS:
        return False, "no_or_unknown_refresh_schedule"
    if not completed:
        return True, ""
    last = parse_when(data.get("lastTransferAt"))
    if last is None or (today - last).days >= INTERVAL_DAYS[schedule]:
        return True, ""
    return False, "refreshed_recently"


# ---------------------------------------------------------------------------------------------- results
def failure_count(data: dict, error: str) -> tuple[str, str]:
    """Apply the consecutive-failure limit to a failed study: (status, error)."""
    limit = FAILURE_LIMITS.get(clean(data.get("refreshSchedule")).lower())
    if limit is None:
        return "FAILED", error
    previous = 0
    old = str(data.get("error") or "")
    if clean(data.get("lastTransferStatus")).upper() == "FAILED":
        m = COUNT_RX.search(old)
        previous = int(m.group(1)) if m else 0
    if DISABLED_TEXT in old or previous >= limit:  # a person re-enabled the study: count again from zero
        previous = 0
    count = previous + 1
    error = f"{error} | Consecutive failed runs: {count} of {limit}"
    if count >= limit:
        return STOPPED, f"{error} | Study is failing for last {limit} runs so disabling the trigger for this study"
    return "FAILED", error


def study_result(*, listing_error: str | None, files_found: int, config_error: str | None,
                 file_errors: list[tuple[str, str]], copied: int, upload_error: str | None = None) -> tuple[str, str]:
    """(DONE | FAILED, error) by the requirement's rules, in order (the first that applies wins)."""
    if listing_error is not None:
        if listing_error.startswith(MISSING_LOCATION):
            return "FAILED", f"{MISSING_LOCATION} | Reason: Other issue"
        return "FAILED", with_reason(listing_error)
    if files_found == 0:
        return "FAILED", f"{EMPTY_LISTING} | Reason: No data available issue"
    if config_error:
        return "FAILED", with_reason(config_error)
    if file_errors:
        return "FAILED", f"S3 upload failed for {len(file_errors)} file(s): " + ", ".join(f"{p} | Reason: {reason(e)}" for p, e in file_errors)
    if upload_error:
        return "FAILED", with_reason(upload_error)
    if copied == 0:
        return "FAILED", f"{NONE_UPLOADED} | Reason: Other issue"
    return "DONE", ""


def updated(data: dict, status: str, error: str, now: dt.datetime, prid: str, name: str) -> dict:
    """The record's submission_data with only the result fields changed."""
    out = dict(data)
    stamp = fmt_now(now)
    out["modifiedAt"] = stamp
    if status == "DONE":
        out["lastTransferAt"] = stamp
    out["lastTransferStatus"] = status
    out["error"] = error
    out["modifiedByPrid"] = prid
    out["modifiedByFullName"] = name
    return out


# ---------------------------------------------------------------------------------------------- HTTP
def check_api_key(key: str | None, prefix: str = "dpls_", min_length: int = 20) -> str:
    if not key or not key.startswith(prefix) or len(key) < min_length or re.search(r"\s", key):
        raise PermissionError(f"the Self-Service api key is not valid (it must start with '{prefix}', be at least {min_length} "
                              "characters and contain no spaces)")
    return key


def _opener(verify_tls: bool = True, ca_bundle: str | None = None):
    ctx = ssl.create_default_context(cafile=ca_bundle)
    if not verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def submissions_url(base_url: str, form_id: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/forms/{form_id}/submissions"


def fetch_submissions(base_url: str, form_id: str, api_key: str, *, verify_tls: bool = True, ca_bundle: str | None = None,
                      timeout: float = TIMEOUT_S) -> list[dict]:
    """GET the form's submissions; anything but a JSON list raises SelfServiceError."""
    req = urllib.request.Request(submissions_url(base_url, form_id), method="GET", headers={"accept": ACCEPT, "X-API-KEY": api_key})
    try:
        with _opener(verify_tls, ca_bundle).open(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as exc:
        raise SelfServiceError(f"Self-Service control list: HTTP {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SelfServiceError(f"Self-Service control list: {getattr(exc, 'reason', exc)}") from None
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise SelfServiceError("Self-Service control list: the response is not JSON") from None
    if not isinstance(data, list):
        raise SelfServiceError(f"Self-Service control list: expected a JSON list of submissions, got {type(data).__name__}")
    return data


def put_submission(base_url: str, form_id: str, sub_id: Any, api_key: str, submission_data: dict, *, verify_tls: bool = True,
                   ca_bundle: str | None = None, timeout: float = TIMEOUT_S) -> int:
    url = f"{submissions_url(base_url, form_id)}/{sub_id}"
    body = json.dumps({"submission_data": submission_data}).encode()
    req = urllib.request.Request(url, data=body, method="PUT",
                                 headers={"accept": ACCEPT, "X-API-KEY": api_key, "Content-Type": "application/json"})
    with _opener(verify_tls, ca_bundle).open(req, timeout=timeout) as r:
        return r.status
