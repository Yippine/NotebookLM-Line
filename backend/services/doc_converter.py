import io
from pathlib import Path

from markitdown import MarkItDown

_converter = MarkItDown()


def convert_to_markdown(file_bytes: bytes, file_name: str) -> tuple[bytes, str]:
    """Convert an uploaded document to Markdown text.

    Returns (markdown_bytes, markdown_file_name). Raises
    ``markitdown.MarkItDownException`` if the format isn't supported or the
    content can't be parsed.
    """
    extension = Path(file_name).suffix or None
    result = _converter.convert_stream(io.BytesIO(file_bytes), file_extension=extension)
    markdown_name = Path(file_name).stem + ".md"
    return result.markdown.encode("utf-8"), markdown_name
