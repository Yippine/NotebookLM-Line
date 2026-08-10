import io
from pathlib import Path

from markitdown import MarkItDown

_converter = MarkItDown()


def convert_to_markdown(file_bytes: bytes, file_name: str) -> tuple[bytes, str]:
    """將上傳的文件轉換為 Markdown 文字。

    回傳 (markdown_bytes, markdown_file_name)。若格式不受支援或
    內容無法解析，會拋出 ``markitdown.MarkItDownException``。
    """
    extension = Path(file_name).suffix or None
    result = _converter.convert_stream(io.BytesIO(file_bytes), file_extension=extension)
    markdown_name = Path(file_name).stem + ".md"
    return result.markdown.encode("utf-8"), markdown_name
