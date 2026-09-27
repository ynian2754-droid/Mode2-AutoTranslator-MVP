"""Font loading and metrics for the portable ReportLab PDF path.

ReportLab is imported only when a PDF is requested so that the web app and
the other exporters can still start in an environment without the optional
PDF dependency.

Two registered fonts exist:

* the primary CJK/Latin font (``Mode2SansCJKCN-Regular.ttf``), and
* an optional DejaVu fallback used only for codepoints inside reviewed math
  and Latin Extended-A ranges that the primary font cannot draw.

The fallback is deliberately bounded by ranges rather than "whatever the second
font happens to contain": a broad font also covers things this project has
decided *not* to render (emoji, for example), and drawing those would silently
change export behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exceptions import PipelineError


PDF_FONT_NAME = "Mode2SansCJKCN"
PDF_FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "pdf" / "Mode2SansCJKCN-Regular.ttf"

# The fallback keeps the font's own PostScript name: it is used unmodified, and
# its license requires renaming only for modified copies.
PDF_MATH_FONT_NAME = "DejaVuSans"
PDF_MATH_FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "pdf" / "DejaVuSans.ttf"

# Reviewed ranges the fallback may serve.  A codepoint outside these ranges is
# never drawn with the fallback even when that font contains a glyph for it.
PDF_MATH_FALLBACK_RANGES: tuple[tuple[int, int], ...] = (
    (0x2070, 0x209F),  # Superscripts and Subscripts (₀₁₂ …)
    (0x27C0, 0x27EF),  # Miscellaneous Mathematical Symbols-A (⟨⟩ ⟪⟫ …)
)

# The primary CJK font intentionally has a compact Latin repertoire.  These
# letters are common in names and bibliographies (for example Turkish ı/ğ and
# Polish ł) and are present in the bundled DejaVu fallback.  Keep the fallback
# limited to the reviewed Latin Extended-A block instead of enabling every
# character that happens to exist in the broad fallback font.
PDF_LATIN_FALLBACK_RANGES: tuple[tuple[int, int], ...] = (
    (0x0100, 0x017F),  # Latin Extended-A
)


def in_pdf_fallback_ranges(codepoint: int) -> bool:
    return any(
        start <= codepoint <= end
        for start, end in (*PDF_MATH_FALLBACK_RANGES, *PDF_LATIN_FALLBACK_RANGES)
    )


def in_math_fallback_ranges(codepoint: int) -> bool:
    """Whether a codepoint is in the original math-only fallback ranges."""
    return any(start <= codepoint <= end for start, end in PDF_MATH_FALLBACK_RANGES)


class PdfFontError(PipelineError):
    """A portable PDF font or its ReportLab dependency is unavailable."""


@dataclass(frozen=True)
class PdfFont:
    """A registered ReportLab font with the same metrics used for drawing."""

    name: str
    path: Path
    pdfmetrics: Any
    face: Any

    def width(self, text: str, size: float) -> float:
        return float(self.pdfmetrics.stringWidth(text, self.name, size))

    def char_width(self, char: str, size: float) -> float:
        return self.width(char, size)

    def validate_text(self, text: str) -> None:
        missing = sorted({ord(char) for char in text if not self._has_glyph(ord(char))})
        if missing:
            formatted = ", ".join(f"U+{codepoint:04X}" for codepoint in missing[:12])
            if len(missing) > 12:
                formatted += f", …（共 {len(missing)} 个）"
            raise PdfFontError(f"PDF 字体缺少字符：{formatted}。请更换已登记的字体资源。")

    def has_glyph(self, codepoint: int) -> bool:
        """Public coverage probe used by the read-only pre-export scan."""

        return self._has_glyph(codepoint)

    def _has_glyph(self, codepoint: int) -> bool:
        glyph = getattr(self.face, "charToGlyph", {}).get(codepoint)
        return glyph not in (None, 0)


@dataclass(frozen=True)
class PdfFontChain:
    """Primary font plus an optional, range-bounded DejaVu fallback.

    The primary font answers for every character it covers, so ordinary text
    still renders as a single font run.  Only characters the primary lacks and
    that fall inside the reviewed ranges are drawn with the fallback.
    """

    primary: PdfFont
    fallback: PdfFont | None = None

    @property
    def fallback_active(self) -> bool:
        return self.fallback is not None

    def _fallback_serves(self, codepoint: int) -> bool:
        return (
            self.fallback is not None
            and in_pdf_fallback_ranges(codepoint)
            and self.fallback._has_glyph(codepoint)
        )

    def has_glyph(self, codepoint: int) -> bool:
        return self.primary._has_glyph(codepoint) or self._fallback_serves(codepoint)

    def font_for(self, char: str) -> PdfFont:
        if self.primary._has_glyph(ord(char)):
            return self.primary
        if self._fallback_serves(ord(char)) and self.fallback is not None:
            return self.fallback
        # Uncovered characters stay with the primary so validation reports them.
        return self.primary

    def char_width(self, char: str, size: float) -> float:
        return self.font_for(char).char_width(char, size)

    def width(self, text: str, size: float) -> float:
        """Sum of the per-character widths used for wrapping.

        Drawing advances by this same value, so a wrapped line can never
        overflow its measured width.
        """

        return sum(self.char_width(char, size) for char in text)

    def runs(self, text: str) -> list[tuple[str, str]]:
        """Split one line into ``(text, font name)`` runs.

        Consecutive characters served by the same font stay in one run, so a
        line fully covered by the primary font produces exactly one run.
        """

        if not text:
            return []
        runs: list[tuple[str, str]] = []
        current: list[str] = []
        current_name: str | None = None
        for char in text:
            name = self.font_for(char).name
            if current_name is None:
                current_name = name
            elif name != current_name:
                runs.append(("".join(current), current_name))
                current = []
                current_name = name
            current.append(char)
        if current and current_name is not None:
            runs.append(("".join(current), current_name))
        return runs

    def validate_text(self, text: str) -> None:
        missing = sorted({ord(char) for char in text if not self.has_glyph(ord(char))})
        if missing:
            formatted = ", ".join(f"U+{codepoint:04X}" for codepoint in missing[:12])
            if len(missing) > 12:
                formatted += f", …（共 {len(missing)} 个）"
            raise PdfFontError(f"PDF 字体缺少字符：{formatted}。请更换已登记的字体资源。")


def _load_registered_font(name: str, path: Path) -> PdfFont:
    """Load and register one font file, raising a useful error on failure."""

    path = Path(path)
    if not path.is_file():
        raise PdfFontError(f"PDF 字体文件缺失：{path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PdfFontError(f"PDF 字体文件无法读取：{path}（{exc}）") from exc
    if not size:
        raise PdfFontError(f"PDF 字体文件为空：{path}")

    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError as exc:
        raise PdfFontError("PDF 导出需要 ReportLab，请按 requirements.txt 安装 reportlab。") from exc

    try:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(path), validate=True))
        registered = pdfmetrics.getFont(name)
        face = registered.face
    except Exception as exc:  # ReportLab exposes several parser-specific error types.
        raise PdfFontError(f"PDF 字体无法由 ReportLab 加载：{path}（{exc}）") from exc

    if not getattr(face, "charToGlyph", None):
        raise PdfFontError(f"PDF 字体没有可用 Unicode 字形映射：{path}")
    return PdfFont(name, path, pdfmetrics, face)


def load_pdf_font() -> PdfFont:
    """Load and register the project font, raising a useful error on failure."""

    return _load_registered_font(PDF_FONT_NAME, PDF_FONT_PATH)


def load_pdf_math_font() -> PdfFont | None:
    """Load the optional math/Latin fallback, or ``None`` when absent.

    A missing asset (an older checkout) degrades to the reviewed equivalence
    substitutions.  An asset that is present but unloadable is a packaging bug
    and fails loudly instead of being ignored.
    """

    path = Path(PDF_MATH_FONT_PATH)
    if not path.is_file():
        return None
    return _load_registered_font(PDF_MATH_FONT_NAME, path)


def load_pdf_fonts() -> PdfFontChain:
    """Load the primary font and its optional bounded DejaVu fallback."""

    return PdfFontChain(primary=load_pdf_font(), fallback=load_pdf_math_font())


def char_width(char: str, size: float) -> float:
    """Measure one character through the registered portable PDF font."""

    return load_pdf_font().char_width(char, size)
