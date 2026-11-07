"""Orientation recovery with a fake engine that only reads upright images."""

import asyncio

import numpy as np

from app.services.ocr_engine import OCRResult
from app.services.orientation import needs_rotation_check, recover_orientation
from app.services.parser import parse_receipt
from tests.helpers import make_ocr

READABLE = [["MARKET A.S"], ["TARIH 04.11.2026"], ["KDV %10", "9,68"], ["ARA TOPLAM", "96,80"], ["TOPLAM", "106,48"]]
GIBBERISH = [["xq zv"], ["lorem"]]


class UprightOnlyEngine:
    """Reads the receipt only when the marker pixel sits in the top-left corner."""

    def __init__(self) -> None:
        self.calls = 0

    async def recognize_async(self, image: np.ndarray) -> OCRResult:
        self.calls += 1
        return make_ocr(READABLE if image[0, 0, 0] == 255 else GIBBERISH)


def _image(marker_row: int, marker_col: int, shape: tuple[int, int] = (200, 100)) -> np.ndarray:
    image = np.zeros((*shape, 3), dtype=np.uint8)
    image[marker_row, marker_col] = 255
    return image


def test_upside_down_receipt_is_rotated_back() -> None:
    upright = _image(0, 0)
    flipped = np.rot90(upright, 2).copy()  # marker now bottom-right
    ocr = make_ocr(GIBBERISH)
    parsed = parse_receipt(ocr)
    assert needs_rotation_check(parsed)

    engine = UprightOnlyEngine()
    result = asyncio.run(recover_orientation(engine, flipped, flipped, ocr, parsed))  # type: ignore[arg-type]
    assert result.rotation == "180 degrees"
    assert result.parsed.total_amount is not None
    assert result.warped[0, 0, 0] == 255  # the stored image was rotated too


def test_sideways_receipt_is_rotated_back() -> None:
    upright = _image(0, 0)
    sideways = np.rot90(upright, 1).copy()  # 90 deg counter-clockwise -> needs a clockwise turn... or the reverse
    ocr = make_ocr(GIBBERISH)
    engine = UprightOnlyEngine()
    result = asyncio.run(recover_orientation(engine, sideways, sideways, ocr, parse_receipt(ocr)))  # type: ignore[arg-type]
    assert result.rotation is not None and "90" in result.rotation
    assert result.parsed.total_amount is not None


def test_unreadable_receipt_keeps_the_original_reading() -> None:
    blank = _image(5, 5)
    ocr = make_ocr(GIBBERISH)
    result = asyncio.run(recover_orientation(UprightOnlyEngine(), blank, blank, ocr, parse_receipt(ocr)))  # type: ignore[arg-type]
    assert result.rotation is None and result.ocr is ocr
