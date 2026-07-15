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


_CITATION_GROUP_RE = re.compile(r"\[([\d,\s]+)\]")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_BULLET_LINE_RE = re.compile(r"^(?:[•\-*]|\d+[.\)])\s+")


_TRAILING_DATE_RE = re.compile(r"\d{8}$")


def _vendor_from_title(title: str) -> str:
    """Extract the vendor name from a source's filename.

    Current convention is `{廠商}{西元年月日}` with no separator (e.g.
    "McLaren20260715.md" for 2026-07-15) — the vendor name is whatever
    precedes the trailing 8-digit date. Older files named with a
    `{廠商}_...` prefix (e.g. "McLaren_型錄.md") are still supported.
    Filenames matching neither pattern can't be attributed to a vendor.
    """
    stem = Path(title).stem

    vendor, sep, _ = stem.partition("_")
    if sep:
        return vendor

    match = _TRAILING_DATE_RE.search(stem)
    if match:
        vendor = stem[: match.start()]
        if vendor:
            return vendor

    return "未分類"


def _citation_numbers_in(text: str) -> set[int]:
    numbers = set()
    for match in _CITATION_GROUP_RE.finditer(text):
        for piece in match.group(1).split(","):
            piece = piece.strip()
            if piece.isdigit():
                numbers.add(int(piece))
    return numbers


def _strip_citations(text: str) -> str:
    """Remove citation markers like [1] or [1, 2] — nothing shown to the
    user explains what they'd refer to, so they'd just be noise."""
    text = _CITATION_GROUP_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _atoms_in(text: str) -> list[tuple[str, str]]:
    """Split text into ordered (kind, content) atoms.

    Each bullet list item becomes its own 'bullet' atom instead of being
    stuck to the rest of its paragraph — the model often answers with a
    single bulleted list whose items cite different vendors line by line
    (e.g. sorted by year rather than grouped by dealer), so paragraph-level
    splitting can't separate them. A paragraph with no bullets stays a
    single 'block' atom.
    """
    atoms: list[tuple[str, str]] = []
    for para in (p for p in _PARAGRAPH_SPLIT_RE.split(text.strip()) if p.strip()):
        lines = [line for line in para.split("\n") if line.strip()]
        lead: list[str] = []
        i = 0
        while i < len(lines) and not _BULLET_LINE_RE.match(lines[i].strip()):
            lead.append(lines[i])
            i += 1
        if lead:
            atoms.append(("block", "\n".join(lead)))
        while i < len(lines):
            atoms.append(("bullet", lines[i]))
            i += 1
    return atoms


def _render_atoms(atoms: list[tuple[str, str]]) -> str:
    """Rejoin atoms, keeping consecutive bullets tight and everything else
    separated by a blank line."""
    parts: list[str] = []
    prev_kind = None
    for kind, content in atoms:
        if parts:
            parts.append("\n" if kind == "bullet" and prev_kind == "bullet" else "\n\n")
        parts.append(content)
        prev_kind = kind
    return "".join(parts)


def _split_by_vendor(text: str, citation_vendor: dict[int, str]) -> list[str]:
    """Group an answer's content by which single vendor each atom cites,
    bucketing atoms by vendor rather than only merging consecutive runs —
    so a bulleted list that interleaves several vendors' items still ends
    up as one clean message per vendor.

    An atom that cites no source, or cites more than one vendor, can't be
    attributed on its own: it's folded into whichever vendor is currently
    "active" (the most recent one before it), or if none has appeared yet,
    into the first vendor found afterward.
    """
    atoms = _atoms_in(text)

    buckets: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    pending: list[tuple[str, str]] = []
    last_vendor: str | None = None

    for atom in atoms:
        _, content = atom
        cited_vendors = {
            citation_vendor[n] for n in _citation_numbers_in(content) if n in citation_vendor
        }
        vendor = next(iter(cited_vendors)) if len(cited_vendors) == 1 else None

        if vendor is None:
            if last_vendor is not None:
                buckets[last_vendor].append(atom)
            else:
                pending.append(atom)
            continue

        if vendor not in buckets:
            buckets[vendor] = []
            order.append(vendor)
        buckets[vendor].extend(pending)
        pending = []
        buckets[vendor].append(atom)
        last_vendor = vendor

    if not order:
        return [_render_atoms(pending)] if pending else []

    return [f"【{vendor}】\n\n{_render_atoms(buckets[vendor])}" for vendor in order]


_UNCLASSIFIED_HEADER = "【未分類】"
_NO_VENDOR_MATCH_REPLY = "很抱歉，目前的資料無法明確對應到特定廠商，請提供更明確的條件（如廠牌、車型）以便查詢。"


def build_answer_messages(text: str, source_map: dict[int, str]) -> list[str]:
    """Build the LINE message(s) for an answer, one message per vendor.

    Each paragraph is attributed to the single vendor it cites (see
    `_vendor_from_title`) and labeled with a "【廠商】" header, so the user
    can see at a glance which manufacturers have matching info — no
    separate sources footer is sent. If multiple vendors have matching
    info, each gets its own message. Paragraphs that cite no source, or
    cite more than one vendor, merge into a neighboring vendor's message
    instead of becoming their own unlabeled one.

    Content whose source filename couldn't be attributed to any vendor
    (labeled "未分類") is dropped rather than sent — it can't be tied to a
    specific dealer, so it's not useful to the user.
    """
    if not text.strip():
        return []

    cited_numbers = _citation_numbers_in(text) & source_map.keys()
    citation_vendor = {n: _vendor_from_title(source_map[n]) for n in cited_numbers}

    parts = [_strip_citations(part) for part in _split_by_vendor(text, citation_vendor)]
    messages = [part for part in parts if not part.startswith(_UNCLASSIFIED_HEADER)]

    if not messages and parts:
        return [_NO_VENDOR_MATCH_REPLY]
    return messages
