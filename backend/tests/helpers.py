"""Builders for synthetic OCR results (no model needed)."""

from app.services.ocr_engine import OCRLine, OCRResult

ROW_HEIGHT = 24.0
ROW_GAP = 40.0


def make_ocr(rows: list[list[str]], image_height: int = 1000, image_width: int = 600) -> OCRResult:
    """Lay ``rows`` out top to bottom; the boxes of one row sit side by side."""
    lines: list[OCRLine] = []
    for row_index, texts in enumerate(rows):
        top = 30.0 + row_index * ROW_GAP
        for col_index, text in enumerate(texts):
            left = 30.0 + col_index * 300.0
            box = ((left, top), (left + 200, top), (left + 200, top + ROW_HEIGHT), (left, top + ROW_HEIGHT))
            lines.append(OCRLine(text=text, confidence=0.95, box=box))
    return OCRResult(lines=tuple(lines), image_width=image_width, image_height=image_height, elapsed_ms=1.0)
