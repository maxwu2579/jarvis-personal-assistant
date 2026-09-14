"""文档文本提取：Parser 抽象 + 三种实现 + 上传校验（扩展名/类型/文件头/ZIP bomb）。

Parser 契约：输入原始字节，输出提取的纯文本（换行分隔）。提取失败抛
ParsingError（sanitized 简述），由 service 层捕获转为 FAILED 状态。

上传校验在解析之前执行（service 层调用），任何失败都不落库、不落盘。

诚实边界（README 同步说明）：
- .pdf 仅支持文本型 PDF（可提取文本）。加密 PDF 明确失败；扫描版 PDF
  不在支持范围——若提取为空文本则产生 0 个 chunks，不做 OCR、不声称支持；
- ZIP bomb 检查基于 zipfile.infolist() 的中央目录声明值（不解压内容），
  超过任一阈值即拒绝：条目数 >1000、声明解压总量 >200MB、压缩比 >200:1。
"""

import io
import zipfile
from typing import Protocol

from app.services.document_errors import (
    DocxZipBombError,
    InvalidFileContentError,
    UnsupportedFileTypeError,
)

# ---- 上传校验常量 ----

# 扩展名白名单（.docm 因含宏风险被明确排除在白名单之外 → UNSUPPORTED_FILE_TYPE）
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".txt", ".md", ".pdf", ".docx"})

# 扩展名 → 允许的 Content-Type 集合。浏览器/curl 常发 application/octet-stream
# 或省略类型，这些「中性类型」不参与类型一致性校验（由文件头兜底）。
_EXTENSION_TYPES: dict[str, frozenset[str]] = {
    ".txt": frozenset({"text/plain", "text/markdown"}),
    ".md": frozenset({"text/markdown", "text/plain"}),
    ".pdf": frozenset({"application/pdf"}),
    ".docx": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    ),
}
_NEUTRAL_CONTENT_TYPES: frozenset[str] = frozenset(
    {"", "application/octet-stream", "application/x-www-form-urlencoded"}
)

# 文件头（magic bytes）：扩展名与真实内容必须一致，杜绝伪装
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"

# ZIP bomb 阈值
_ZIP_MAX_ENTRIES = 1000
_ZIP_MAX_TOTAL_UNCOMPRESSED = 200 * 1024 * 1024  # 200 MB
_ZIP_MAX_RATIO = 200  # 解压总量 / 压缩后大小


class ParsingError(Exception):
    """解析阶段失败（sanitized 简述，不含路径/堆栈/内容）。"""


class DocumentParser(Protocol):
    extension: str

    def extract_text(self, content: bytes) -> str: ...


# ---- 上传阶段校验（任何失败不落库、不落盘） ----


def check_extension(filename: str) -> str:
    """校验扩展名白名单，返回小写扩展名。.docm 一并拒绝（含宏）。"""
    lower = filename.lower()
    for ext in (".docm",):
        if lower.endswith(ext):
            raise UnsupportedFileTypeError(
                f"file type .docm is not supported (macro-enabled documents are rejected)"
            )
    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UnsupportedFileTypeError(
            f"file type not supported (allowed: {allowed})"
        )
    return ext


def check_content_type(content_type: str, ext: str) -> None:
    """Content-Type 一致性校验：中性类型跳过，明确类型必须与扩展名匹配。"""
    normalized = (content_type or "").split(";")[0].strip().lower()
    if normalized in _NEUTRAL_CONTENT_TYPES:
        return
    if normalized not in _EXTENSION_TYPES[ext]:
        raise InvalidFileContentError(
            f"content type {normalized or '(none)'} does not match {ext}"
        )


def check_file_header(content: bytes, ext: str) -> None:
    """文件头校验：扩展名与真实文件内容必须一致（扩展名伪装检测）。"""
    if ext == ".pdf" and not content.startswith(_PDF_MAGIC):
        raise InvalidFileContentError("file content does not match .pdf")
    if ext == ".docx" and not content.startswith(_ZIP_MAGIC):
        raise InvalidFileContentError("file content does not match .docx")
    # .txt/.md 无强制文件头（任意文本字节均可）


def check_docx_zip_bomb(content: bytes) -> None:
    """ZIP bomb 防御：基于中央目录声明值（zipfile.infolist()，不解压内容）。

    阈值：条目数 > 1000，或声明解压总量 > 200MB，或压缩比 > 200:1。
    非法 zip（损坏的 .docx）按文件内容错误拒绝。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        raise InvalidFileContentError("file is not a valid .docx archive")
    if len(infos) > _ZIP_MAX_ENTRIES:
        raise DocxZipBombError(
            f"docx archive has too many entries ({len(infos)} > {_ZIP_MAX_ENTRIES})"
        )
    total_uncompressed = sum(info.file_size for info in infos)
    if total_uncompressed > _ZIP_MAX_TOTAL_UNCOMPRESSED:
        raise DocxZipBombError("docx archive uncompressed size is too large")
    if content and total_uncompressed > len(content) * _ZIP_MAX_RATIO:
        raise DocxZipBombError("docx archive compression ratio is too high")


# ---- 解析器 ----


class TextParser:
    """.txt / .md：UTF-8（容忍 BOM），非法字节替换为 U+FFFD——确定性、不抛异常。"""

    def __init__(self, extension: str):
        self.extension = extension

    def extract_text(self, content: bytes) -> str:
        return content.decode("utf-8-sig", errors="replace")


class PdfParser:
    """.pdf（仅文本）：加密 PDF 明确失败；逐页提取，页间以空行分隔。"""

    extension = ".pdf"

    def extract_text(self, content: bytes) -> str:
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(content))
        except Exception as exc:
            # 损坏/不支持的 PDF：不向用户暴露底层异常细节
            raise ParsingError("pdf parsing failed") from exc
        if reader.is_encrypted:
            raise ParsingError("encrypted pdf is not supported")
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())
        return "\n\n".join(pages)


class DocxParser:
    """.docx：按文档流顺序提取段落与表格（表格行以 | 连接单元格）。"""

    extension = ".docx"

    def extract_text(self, content: bytes) -> str:
        from docx import Document as DocxDocument
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        try:
            document = DocxDocument(io.BytesIO(content))
        except Exception as exc:
            raise ParsingError("docx parsing failed") from exc

        lines: list[str] = []
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                text = Paragraph(child, document).text.strip()
                if text:
                    lines.append(text)
            elif child.tag == qn("w:tbl"):
                for row in Table(child, document).rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    lines.append(" | ".join(cells))
        return "\n".join(lines)


_PARSERS_BY_EXTENSION: dict[str, DocumentParser] = {
    ".txt": TextParser(".txt"),
    ".md": TextParser(".md"),
    ".pdf": PdfParser(),
    ".docx": DocxParser(),
}


def get_parser(ext: str) -> DocumentParser:
    return _PARSERS_BY_EXTENSION[ext]
