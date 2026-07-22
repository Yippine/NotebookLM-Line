import re


_NUMERIC_CITATION = re.compile(
    r"\s*\[(?=\s*\d)(?:\s*\d+\s*(?:(?:,|，|[-–—])\s*\d+\s*)*)\]"
)


def format_for_line(text: str) -> str:
    """Convert NotebookLM markdown output to LINE-friendly plain text."""
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^\*\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"(?<!\*)\*(?!\*)", "", text)
    # NotebookLM citations such as [1] and [1, 2] aren't actionable in LINE.
    text = _NUMERIC_CITATION.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
