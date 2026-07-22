from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|setup_token|admin_token|admin_password|password|"
    r"channel_secret|channel_access_token|access_token|invite_code|code)=)[^&\s]*"
)
_KEY_VALUE_RE = re.compile(
    r"(?i)([\"']?(?:token|password|secret|cookie|cookies|authorization|master_token|"
    r"storage_state_json|auth_encrypted|auth_json|invite_code|channel_access_token|"
    r"channel_secret|value)[\"']?\s*[:=]\s*)"
    r"(?:[\"'][^\"']*[\"']|[^,}\s]+)"
)
_NOTEBOOK_URL_RE = re.compile(
    r"https://(?:notebooklm\.google(?:\.com)?)/[^\s\"']+", re.IGNORECASE
)
_RESOURCE_PATH_RE = re.compile(
    r"(?P<prefix>/(?:api/)?channels/|/webhook/)(?P<identifier>[^/?\s\"']+)"
)
_INVITE_VERIFY_PATH_RE = re.compile(
    r"(?:^|https?://[^/\s]+)?/api/verify-invite(?:[/?#\s]|$)", re.IGNORECASE
)

_REQUEST_PATH_FIELD_NAMES = frozenset(
    {"path", "request_path", "request_url", "route", "url"}
)
_REQUEST_BODY_FIELD_NAMES = frozenset(
    {"body", "json", "payload", "request_body", "request_json"}
)
_RESPONSE_FIELD_NAMES = frozenset({"response", "response_body", "response_json"})

_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "token",
        "password",
        "secret",
        "cookie",
        "cookies",
        "authorization",
        "master_token",
        "storage_state_json",
        "auth_encrypted",
        "auth_json",
        "invite_code",
        "channel_access_token",
        "channel_secret",
    }
)


def _invite_request_path(value: Mapping[Any, Any]) -> str | None:
    for key, item in value.items():
        if str(key).lower() in _REQUEST_PATH_FIELD_NAMES and isinstance(item, str):
            if _INVITE_VERIFY_PATH_RE.search(item):
                return item
    return None


def _json_container(value: str) -> Mapping[Any, Any] | list[Any] | None:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, (Mapping, list)) else None


def _redact_structure(
    value: Any,
    *,
    request_path: str | None = None,
    is_request_body: bool = False,
    is_response: bool = False,
) -> Any:
    if isinstance(value, Mapping):
        local_request_path = _invite_request_path(value) or request_path
        redact_invite_code = bool(
            is_request_body
            and local_request_path
            and _INVITE_VERIFY_PATH_RE.search(local_request_path)
        )
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in _SENSITIVE_FIELD_NAMES or (
                normalized_key == "code" and redact_invite_code
            ):
                redacted[key] = "<redacted>"
                continue

            child_is_response = is_response or normalized_key in _RESPONSE_FIELD_NAMES
            child_is_request_body = is_request_body or (
                normalized_key in _REQUEST_BODY_FIELD_NAMES and not child_is_response
            )
            redacted[key] = _redact_structure(
                item,
                request_path=local_request_path,
                is_request_body=child_is_request_body,
                is_response=child_is_response,
            )
        return redacted
    if isinstance(value, tuple):
        return tuple(
            _redact_structure(
                item,
                request_path=request_path,
                is_request_body=is_request_body,
                is_response=is_response,
            )
            for item in value
        )
    if isinstance(value, list):
        return [
            _redact_structure(
                item,
                request_path=request_path,
                is_request_body=is_request_body,
                is_response=is_response,
            )
            for item in value
        ]
    if isinstance(value, str) and is_request_body:
        parsed = _json_container(value)
        if parsed is not None:
            return json.dumps(
                _redact_structure(
                    parsed,
                    request_path=request_path,
                    is_request_body=True,
                    is_response=is_response,
                ),
                ensure_ascii=False,
            )
    return value


def sanitize_for_log(value: Any, *, explicit_secrets: tuple[str, ...] = ()) -> str:
    if isinstance(value, str) and (parsed := _json_container(value)) is not None:
        text = json.dumps(_redact_structure(parsed), ensure_ascii=False)
    else:
        text = str(_redact_structure(value))
    for secret in explicit_secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}<redacted>", text)
    text = _KEY_VALUE_RE.sub(lambda match: f"{match.group(1)}<redacted>", text)
    text = _NOTEBOOK_URL_RE.sub("<notebook-url:redacted>", text)
    text = _RESOURCE_PATH_RE.sub(
        lambda match: (
            f"{match.group('prefix')}<{mask_identifier(match.group('identifier'))}>"
        ),
        text,
    )
    return text


def mask_identifier(value: str | None) -> str:
    if not value:
        return "-"
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]}"


class SensitiveDataFilter(logging.Filter):
    """Redact known secret shapes from values intentionally sent to logging."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_for_log(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                redacted_args = _redact_structure(record.args)
                record.args = {
                    key: (
                        sanitize_for_log(value)
                        if isinstance(value, (str, Mapping, list, tuple))
                        else value
                    )
                    for key, value in redacted_args.items()
                }
            else:
                record.args = tuple(
                    sanitize_for_log(value)
                    if isinstance(value, (str, Mapping, list, tuple))
                    else value
                    for value in record.args
                )
        return True


def configure_sensitive_logging() -> None:
    filter_instance = SensitiveDataFilter()
    for logger_name in ("", "uvicorn", "uvicorn.access", "uvicorn.error"):
        target = logging.getLogger(logger_name)
        if not any(isinstance(item, SensitiveDataFilter) for item in target.filters):
            target.addFilter(filter_instance)
        for handler in target.handlers:
            if not any(
                isinstance(item, SensitiveDataFilter) for item in handler.filters
            ):
                handler.addFilter(filter_instance)


def log_security_event(
    logger: logging.Logger,
    *,
    action: str,
    outcome: str,
    resource_id: str | None = None,
    duration_ms: float | None = None,
) -> None:
    logger.info(
        "security_event action=%s outcome=%s resource=%s request_id=%s duration_ms=%s",
        action,
        outcome,
        mask_identifier(resource_id),
        request_id_var.get(),
        "-" if duration_ms is None else f"{duration_ms:.1f}",
    )
