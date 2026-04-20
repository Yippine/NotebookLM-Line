import re


def format_for_line(text: str) -> str:
    """Convert NotebookLM markdown output to LINE-friendly plain text."""
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^\*\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"(?<!\*)\*(?!\*)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_sources_message(text: str, source_map: dict[int, str]) -> str | None:
    """Build a separate sources message. Returns None if no citations found."""
    if not source_map:
        return None
    cited = sorted(set(int(n) for n in re.findall(r"\[(\d+)\]", text)))
    if not cited:
        return None
    lines = ["📚 參考來源："]
    for n in cited:
        title = source_map.get(n)
        if title:
            lines.append(f"[{n}] {title}")
    return "\n".join(lines) if len(lines) > 1 else None
