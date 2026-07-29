from __future__ import annotations

import pytest

from services.notebook_url import NotebookUrlError, parse_notebook_url


ALLOWED = [
    "notebooklm.google.com",
    "https://notebooklm.google",
    "notebook.google.com",
]
NOTEBOOK_ID = "AbcdEFGH_1234-xyz"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://notebooklm.google.com/notebook/{NOTEBOOK_ID}", NOTEBOOK_ID),
        (f"https://notebook.google.com/notebook/{NOTEBOOK_ID}", NOTEBOOK_ID),
        (
            f"https://notebooklm.google.com/notebook/{NOTEBOOK_ID}/edit?tab=sources#top",
            NOTEBOOK_ID,
        ),
        (f"https://NOTEBOOKLM.GOOGLE/notebook/{NOTEBOOK_ID}/view", NOTEBOOK_ID),
    ],
)
def test_parse_notebook_url_accepts_only_normalized_official_urls(
    url: str, expected: str
) -> None:
    assert parse_notebook_url(url, ALLOWED) == expected


@pytest.mark.parametrize(
    "url",
    [
        f"http://notebooklm.google.com/notebook/{NOTEBOOK_ID}",
        f"https://notebooklm.google.com.evil.example/notebook/{NOTEBOOK_ID}",
        f"https://notebook.google.com.evil.example/notebook/{NOTEBOOK_ID}",
        f"https://evil.example/notebook/{NOTEBOOK_ID}",
        f"https://user:password@notebooklm.google.com/notebook/{NOTEBOOK_ID}",
        f"https://notebooklm.google.com:8443/notebook/{NOTEBOOK_ID}",
        "https://notebooklm.google.com/notebook/short",
        f"https://notebooklm.google.com/notebook/{NOTEBOOK_ID}/share",
        f"https://notebooklm.google.com/notebook/{NOTEBOOK_ID}/edit/extra",
        f"https://notebooklm.google.com/notebook/{NOTEBOOK_ID}%2F..%2Fsecret",
        f"https://notebooklm.google.com/%2e%2e/notebook/{NOTEBOOK_ID}",
        f"javascript://notebooklm.google.com/notebook/{NOTEBOOK_ID}",
        f"//notebooklm.google.com/notebook/{NOTEBOOK_ID}",
        f"https://notebooklm.google.com@evil.example/notebook/{NOTEBOOK_ID}",
        "not a url",
        "",
    ],
)
def test_parse_notebook_url_rejects_malformed_or_malicious_urls(url: str) -> None:
    with pytest.raises(NotebookUrlError):
        parse_notebook_url(url, ALLOWED)


def test_parse_notebook_url_requires_exact_allowlist_match() -> None:
    with pytest.raises(NotebookUrlError):
        parse_notebook_url(
            f"https://sub.notebooklm.google.com/notebook/{NOTEBOOK_ID}",
            ["notebooklm.google.com"],
        )
