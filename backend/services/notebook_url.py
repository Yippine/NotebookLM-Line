"""NotebookLM URL validation and normalization.

Only the notebook identifier is returned.  The original URL is deliberately
not persisted or logged because it is an access capability in practice.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit


class NotebookUrlError(ValueError):
    """Raised when a submitted URL is not an allowed NotebookLM URL."""


_NOTEBOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _normalize_hosts(
    host_allowlist: list[str] | tuple[str, ...] | set[str],
) -> set[str]:
    hosts: set[str] = set()
    for value in host_allowlist:
        candidate = value.strip().lower().rstrip(".")
        if not candidate:
            continue
        # Accept either a bare host or a configured origin, while still
        # comparing exact hostnames (never suffix matching).
        if "://" in candidate:
            candidate = (urlsplit(candidate).hostname or "").rstrip(".")
        if candidate:
            hosts.add(candidate)
    return hosts


def parse_notebook_url(
    value: str,
    host_allowlist: list[str] | tuple[str, ...] | set[str],
) -> str:
    """Return a normalized notebook ID from an official HTTPS URL.

    Supported paths intentionally stay narrow: the current and rebranded
    official hosts use ``/notebook/<id>``.  Optional ``/edit`` or ``/view``
    suffixes, query parameters, and fragments are ignored.
    """

    if not isinstance(value, str) or not value.strip():
        raise NotebookUrlError("請貼上完整的 NotebookLM HTTPS 網址")

    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise NotebookUrlError("NotebookLM 網址格式不正確") from exc

    if parsed.scheme.lower() != "https":
        raise NotebookUrlError("NotebookLM 網址必須使用 HTTPS")
    try:
        has_custom_port = parsed.port is not None
    except ValueError as exc:
        raise NotebookUrlError("NotebookLM 網址連接埠格式不正確") from exc
    if parsed.username or parsed.password or has_custom_port:
        raise NotebookUrlError("NotebookLM 網址不可包含帳號、密碼或自訂連接埠")

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname not in _normalize_hosts(host_allowlist):
        raise NotebookUrlError("請使用官方 NotebookLM 網址")

    try:
        segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    except UnicodeDecodeError as exc:
        raise NotebookUrlError("NotebookLM 網址格式不正確") from exc

    if len(segments) not in (2, 3) or segments[0].lower() != "notebook":
        raise NotebookUrlError("網址中找不到 Notebook ID")
    if len(segments) == 3 and segments[2].lower() not in {"edit", "view"}:
        raise NotebookUrlError("NotebookLM 網址路徑不受支援")

    notebook_id = segments[1]
    if not _NOTEBOOK_ID_RE.fullmatch(notebook_id):
        raise NotebookUrlError("Notebook ID 格式不正確")
    return notebook_id
