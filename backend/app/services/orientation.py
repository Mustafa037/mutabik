"""Recover receipts photographed sideways or upside down.

PaddleOCR's angle classifier only handles 0/180 degrees of *text lines*, and the
perspective step keeps whatever orientation the photo had. When the normal pass
finds no total, the other rotations are tried and the best reading wins. This
costs extra OCR passes only for receipts that already failed.
"""

import logging
from dataclasses import dataclass
from typing import Final

import cv2
import numpy as np

from app.services.ocr_engine import OCREngine, OCRError, OCRResult
from app.services.parser import ParsedReceipt, parse_receipt

__all__ = ["OrientationResult", "needs_rotation_check", "recover_orientation"]

logger = logging.getLogger(__name__)

LANDSCAPE_RATIO: Final[float] = 1.2  # receipts are tall; wider than this means "probably sideways"
_ROTATIONS: Final[dict[str, int]] = {
    "90 degrees clockwise": cv2.ROTATE_90_CLOCKWISE,
    "90 degrees counter-clockwise": cv2.ROTATE_90_COUNTERCLOCKWISE,
    "180 degrees": cv2.ROTATE_180,
}


@dataclass(frozen=True, eq=False)
class OrientationResult:
    """The best reading found and the rotation that produced it."""

    ocr: OCRResult
    parsed: ParsedReceipt
    warped: np.ndarray
    rotation: str | None  # ``None`` when the original orientation was kept


def needs_rotation_check(parsed: ParsedReceipt) -> bool:
    """``True`` when the reading is poor enough to try other orientations."""
    return parsed.total_amount is None


def _score(parsed: ParsedReceipt, ocr: OCRResult) -> tuple[int, int, float]:
    """Rank a reading: found total, then other found fields, then OCR confidence."""
    fields = (parsed.merchant_name, parsed.date, parsed.subtotal_amount, parsed.tax_amount)
    return (int(parsed.total_amount is not None), sum(value is not None for value in fields), ocr.mean_confidence)


async def recover_orientation(
    engine: OCREngine,
    enhanced: np.ndarray,
    warped: np.ndarray,
    ocr: OCRResult,
    parsed: ParsedReceipt,
) -> OrientationResult:
    """Try the other rotations and keep the best reading.

    Args:
        engine: OCR engine.
        enhanced: Image the first pass ran on.
        warped: Colour image that is stored; rotated together with ``enhanced``.
        ocr: First-pass OCR result.
        parsed: First-pass parse of ``ocr``.

    Returns:
        The best :class:`OrientationResult`; the first pass itself when no rotation
        improves it. OCR failures on a rotated copy are ignored.
    """
    best = OrientationResult(ocr=ocr, parsed=parsed, warped=warped, rotation=None)
    best_score = _score(parsed, ocr)
    height, width = enhanced.shape[:2]
    names = list(_ROTATIONS)
    if width <= LANDSCAPE_RATIO * height:  # already portrait: the 180 degree flip is the likely fix
        names = names[::-1]

    for name in names:
        code = _ROTATIONS[name]
        rotated = cv2.rotate(enhanced, code)
        try:
            candidate_ocr = await engine.recognize_async(rotated)
        except OCRError as exc:
            logger.debug("OCR failed for rotation %s: %s", name, exc)
            continue
        candidate = parse_receipt(candidate_ocr)
        score = _score(candidate, candidate_ocr)
        if score > best_score:
            best = OrientationResult(candidate_ocr, candidate, cv2.rotate(warped, code), name)
            best_score = score
        if candidate.total_amount is not None:  # good enough: stop spending CPU
            break
    if best.rotation:
        logger.info("Receipt re-read after rotating %s", best.rotation)
    return best
