import re
from pathlib import Path


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


_CITATION_GROUP_RE = re.compile(r"\[([\d,\s]+)\]")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def _vendor_from_title(title: str) -> str:
    """Extract the vendor name from a source's filename.

    Uploaders are expected to name knowledge-base files with a
    `{廠商}_...` prefix (e.g. "McLaren_型錄.md"); files without an
    underscore can't be attributed to a vendor.
    """
    stem = Path(title).stem
    vendor, sep, _ = stem.partition("_")
    return vendor if sep else "未分類"


def _citation_numbers_in(text: str) -> set[int]:
    numbers = set()
    for match in _CITATION_GROUP_RE.finditer(text):
        for piece in match.group(1).split(","):
            piece = piece.strip()
            if piece.isdigit():
                numbers.add(int(piece))
    return numbers


def _split_by_vendor(text: str, citation_vendor: dict[int, str]) -> list[str]:
    """Group an answer's paragraphs by which single vendor they cite.

    A paragraph that cites no source, or cites sources from more than one
    vendor, can't be cleanly attributed — it's merged into the preceding
    vendor's group instead of becoming its own unlabeled message.
    """
    paragraphs = [p for p in _PARAGRAPH_SPLIT_RE.split(text.strip()) if p.strip()]

    groups: list[list[str]] = []
    vendors: list[str | None] = []
    for para in paragraphs:
        cited_vendors = {
            citation_vendor[n] for n in _citation_numbers_in(para) if n in citation_vendor
        }
        vendor = next(iter(cited_vendors)) if len(cited_vendors) == 1 else None

        if groups and (vendor is None or vendors[-1] == vendor):
            groups[-1].append(para)
        else:
            groups.append([para])
            vendors.append(vendor)

    # A leading paragraph with no attributable vendor (e.g. a general
    # intro sentence) has nothing to merge into yet — fold it into the
    # first real vendor group rather than sending it unlabeled.
    if len(groups) > 1 and vendors[0] is None:
        groups[1] = groups[0] + groups[1]
        groups.pop(0)
        vendors.pop(0)

    return [
        (f"【{vendor}】\n\n" if vendor else "") + "\n\n".join(paras)
        for vendor, paras in zip(vendors, groups)
    ]


def build_answer_messages(text: str, source_map: dict[int, str]) -> list[str]:
    """Build the LINE message(s) for an answer.

    If the citations span more than one vendor (see `_vendor_from_title`),
    the answer is split into one message per vendor, each with its own
    "參考來源" footer. Otherwise this is just the combined answer plus one
    shared sources footer, same as before.
    """
    if not text.strip():
        return []

    cited_numbers = _citation_numbers_in(text) & source_map.keys()
    citation_vendor = {n: _vendor_from_title(source_map[n]) for n in cited_numbers}

    if len(set(citation_vendor.values())) <= 1:
        sources_msg = build_sources_message(text, source_map)
        return [text, sources_msg] if sources_msg else [text]

    messages = []
    for part in _split_by_vendor(text, citation_vendor):
        sources_msg = build_sources_message(part, source_map)
        messages.append(f"{part}\n\n{sources_msg}" if sources_msg else part)
    return messages
