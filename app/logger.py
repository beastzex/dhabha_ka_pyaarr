"""PII-safe structured JSON logger for Dhaba Triage Service.

Automatically redacts Indian GSTINs, emails, UPI IDs, credit card/phone numbers,
and corporate entity names from all log records and structured fields.
Strictly forbids passing raw ticket body or subject to event loggers.
"""

from datetime import datetime, timezone
import json
import logging
import re
import sys
from typing import Any, Dict, List, Tuple

# Regex Patterns for PII redaction
RE_ENTITY_PRECEDING_GSTIN = re.compile(
    r"([^\n,:]+?)(?=\s*,\s*\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}\b)"
)
RE_KNOWN_ENTITIES = re.compile(
    r"\b(?:Acme\s+Interiors)\b|"
    r"\b[A-Za-z0-9&.\'-]+(?:\s+[A-Za-z0-9&.\'-]+)*\s+(?:Pvt\.?\s*Ltd\.?|Private\s+Limited|LLP|Inc\.?|Corp\.?|Corporation|Enterprises|Solutions|Technologies|Services|Interiors|Industries|Trading)\b",
    re.IGNORECASE,
)
RE_GSTIN = re.compile(
    r"\b\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}\b"
)
RE_EMAIL_UPI = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\b"
)
RE_NUMBER = re.compile(
    r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}\b|\b(?:\d{4}[\s-]?){3}\d{4}\b|\b\d{10,16}\b"
)

# Ordered redaction rules: entity names preceding GSTIN must run before GSTIN itself
REDACTION_RULES: List[Tuple[re.Pattern, str]] = [
    (RE_ENTITY_PRECEDING_GSTIN, "[REDACTED_ENTITY]"),
    (RE_KNOWN_ENTITIES, "[REDACTED_ENTITY]"),
    (RE_GSTIN, "[REDACTED_GSTIN]"),
    (RE_EMAIL_UPI, "[REDACTED_EMAIL_UPI]"),
    (RE_NUMBER, "[REDACTED_NUMBER]"),
]

STANDARD_LOGRECORD_ATTRIBUTES = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}

FORBIDDEN_FIELDS = {"body", "subject"}


def scrub_pii(data: Any) -> Any:
    """Recursively scrub PII from strings, dictionaries, lists, and tuples."""
    if isinstance(data, str):
        text = data
        for pattern, replacement in REDACTION_RULES:
            text = pattern.sub(replacement, text)
        return text
    elif isinstance(data, dict):
        return {k: scrub_pii(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [scrub_pii(item) for item in data]
    elif isinstance(data, tuple):
        return tuple(scrub_pii(item) for item in data)
    return data


class PIIScrubbingFilter(logging.Filter):
    """Logging filter that redacts sensitive PII before records are handled or formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub_pii(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = scrub_pii(record.args)
            elif isinstance(record.args, (list, tuple)):
                record.args = tuple(scrub_pii(x) for x in record.args)

        # Scrub any structured extra attributes
        for key, val in list(record.__dict__.items()):
            if key not in STANDARD_LOGRECORD_ATTRIBUTES:
                record.__dict__[key] = scrub_pii(val)
        return True


class JSONFormatter(logging.Formatter):
    """Emits log records as structured single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        record_msg = record.getMessage()
        log_payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record_msg,
        }

        # Include structured extra fields
        for key, val in record.__dict__.items():
            if key not in STANDARD_LOGRECORD_ATTRIBUTES and key not in log_payload:
                log_payload[key] = val

        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_payload, default=str)


def setup_logger(name: str = "dhaba_triage") -> logging.Logger:
    """Configure and return the service logger with PII filter and JSON formatting."""
    app_logger = logging.getLogger(name)
    app_logger.setLevel(logging.INFO)

    # Attach filter directly to logger to sanitize records early (including caplog in pytest)
    pii_filter = PIIScrubbingFilter()
    app_logger.addFilter(pii_filter)

    # Avoid duplicate handlers if setup is called multiple times
    if not app_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        handler.addFilter(pii_filter)
        app_logger.addHandler(handler)

    return app_logger


logger = setup_logger()


def log_event(event_type: str, ticket_id: str, **kwargs: Any) -> None:
    """
    Log a structured event for a ticket.

    Safety guard:
    Strictly forbids passing raw 'body' or 'subject' to prevent PII exposure in production logs.
    """
    for forbidden in FORBIDDEN_FIELDS:
        if forbidden in kwargs:
            raise ValueError(
                f"Direct logging of raw ticket '{forbidden}' is strictly forbidden for PII safety."
            )

    extra_fields = {
        "event_type": event_type,
        "ticket_id": ticket_id,
        **kwargs,
    }
    logger.info(f"[{event_type}] ticket={ticket_id}", extra=extra_fields)
