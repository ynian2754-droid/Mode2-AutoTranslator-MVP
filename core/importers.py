"""Convert supported source files into normalized text for the segmenter."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from .epub_structure import epub_document_paths, extract_epub_structure
from core.text_reconstruction import PdfTextReconstruction, reconstruct_pdf_pages


class SourceImportError(ValueError):
    """The uploaded file cannot be converted into usable source text."""


@dataclass(frozen=True)
class PdfPageSpan:
    """One extracted PDF page and its ranges in ``ImportedSource.text``.

    The legacy text still contains a ``> [PDF Page N]`` marker for now, so
    these offsets let later reconstruction stages remove layout artifacts
    without losing the original page binding.
    """

    page_number: int
    text: str
    marker_start: int
    marker_end: int
    text_start: int
    text_end: int


@dataclass(frozen=True)
class ImportedSource:
    original_name: str
    format: str
    text: str
    size_bytes: int
    pdf_pages: tuple[PdfPageSpan, ...] = ()
    pdf_reconstruction: PdfTextReconstruction | None = None
    structure_blocks: tuple[dict[str, Any], ...] = ()

    def metadata(self) -> dict[str, str | int | None]:
        return {
            "name": self.original_name,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "stored_path": None,
        }


class SourceImporter:
    """Own file-format detection and extraction, not project state."""

    SUPPORTED_FORMATS = {
        ".md": "markdown",
        ".markdown": "markdown",
        ".txt": "text",
        ".pdf": "pdf",
        ".epub": "epub",
    }

    def import_bytes(self, filename: str, content: bytes) -> ImportedSource:
        suffix = Path(filename or "").suffix.casefold()
        file_format = self.SUPPORTED_FORMATS.get(suffix)
        if not file_format:
            supported = ", ".join(sorted(self.SUPPORTED_FORMATS))
            raise SourceImportError(f"暂不支持 {suffix or '无扩展名'} 文件。支持：{supported}")
        if not content:
            raise SourceImportError("上传文件为空。")
        pdf_pages: tuple[PdfPageSpan, ...] = ()
        pdf_reconstruction: PdfTextReconstruction | None = None
        structure_blocks: tuple[dict[str, Any], ...] = ()
        if file_format in {"markdown", "text"}:
            text = self._decode_text(content)
        elif file_format == "pdf":
            text, pdf_pages = self._extract_pdf_document(content)
            pdf_reconstruction = reconstruct_pdf_pages(pdf_pages)
        else:
            try:
                text, structure_blocks = extract_epub_structure(content)
            except ValueError as exc:
                raise SourceImportError(str(exc)) from exc
        text = self._normalize(text)
        if not text:
            raise SourceImportError("文件中没有提取到可翻译文字。扫描 PDF 需要先经过 OCR。")
        return ImportedSource(
            original_name=Path(filename).name or "source",
            format=file_format,
            text=text,
            size_bytes=len(content),
            pdf_pages=pdf_pages,
            pdf_reconstruction=pdf_reconstruction,
            structure_blocks=structure_blocks,
        )

    @staticmethod
    def _decode_text(content: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise SourceImportError("文本文件不是可识别的 UTF-8 或 GB18030 编码。")

    @staticmethod
    def _extract_pdf(content: bytes) -> str:
        """Return the legacy marker-bearing PDF text for existing callers."""
        text, _pages = SourceImporter._extract_pdf_document(content)
        return text

    @classmethod
    def _extract_pdf_document(cls, content: bytes) -> tuple[str, tuple[PdfPageSpan, ...]]:
        """Extract legacy text plus page-local provenance without reordering it."""
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise SourceImportError("PDF 导入需要安装 pypdf。") from exc
        try:
            reader = PdfReader(BytesIO(content))
            legacy_parts: list[str] = []
            pages: list[PdfPageSpan] = []
            cursor = 0
            for index, page in enumerate(reader.pages, 1):
                # Applying the existing normalization per page makes ranges
                # stable while preserving the old final text behavior.
                page_text = cls._normalize(page.extract_text() or "")
                if page_text:
                    if legacy_parts:
                        legacy_parts.append("\n\n")
                        cursor += 2
                    marker = f"> [PDF Page {index}]"
                    marker_start = cursor
                    marker_end = marker_start + len(marker)
                    text_start = marker_end + 1
                    text_end = text_start + len(page_text)
                    legacy_parts.append(f"{marker}\n{page_text}")
                    pages.append(
                        PdfPageSpan(
                            page_number=index,
                            text=page_text,
                            marker_start=marker_start,
                            marker_end=marker_end,
                            text_start=text_start,
                            text_end=text_end,
                        )
                    )
                    cursor = text_end
        except Exception as exc:  # pypdf exposes several format-specific errors
            raise SourceImportError(f"PDF 解析失败：{exc}") from exc
        return "".join(legacy_parts), tuple(pages)

    @classmethod
    def _extract_epub(cls, content: bytes) -> str:
        try:
            text, _structure_blocks = extract_epub_structure(content)
        except ValueError as exc:
            raise SourceImportError(str(exc)) from exc
        return text

    @staticmethod
    def _epub_document_paths(archive: zipfile.ZipFile) -> list[str]:
        return epub_document_paths(archive)

    @staticmethod
    def _normalize(value: str) -> str:
        value = value.replace("\x00", "")
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()
