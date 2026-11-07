"""PaddleOCR wrapper: lightweight CPU configuration, process-wide singleton.

Design goals (weak hardware, low-quality cameras):

* **Mobile models only** - PP-OCRv4 for ``en``/``ch`` and PP-OCRv3 for the Latin
  group, which covers Turkish *and* English with a single recogniser, so only
  one detector + one recogniser (~15 MB) are held in memory.
* **CPU only**, MKL-DNN on x86, bounded thread count, angle classifier off by
  default (receipts are upright after the perspective correction).
* **Load once**: the model is created on first use (or at start-up through
  :meth:`OCREngine.warmup`) and shared by all requests. Inference is serialised
  with a lock because a Paddle predictor is not thread-safe.

Runtime settings are read from environment variables until ``core/config.py``
exists: ``OCR_LANG``, ``OCR_VERSION``, ``OCR_USE_ANGLE_CLS``, ``OCR_MAX_SIDE``,
``OCR_CPU_THREADS``, ``OCR_ENABLE_MKLDNN``, ``OCR_MIN_CONFIDENCE``.

The model files are downloaded by PaddleOCR on first start (``~/.paddleocr``);
pre-download them when building a container image.
"""

import asyncio
import logging
import os
import platform
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Optional

import cv2
import numpy as np

__all__ = [
    "OCRError",
    "OCRInitError",
    "OCRInputError",
    "OCRInferenceError",
    "OCREngineConfig",
    "OCRLine",
    "OCRResult",
    "OCREngine",
    "group_into_rows",
    "get_ocr_engine",
    "reset_ocr_engine",
]

logger = logging.getLogger(__name__)

_MIN_IMAGE_SIDE: Final[int] = 8
_LATIN_MODEL_LANGS: Final[frozenset[str]] = frozenset({"en", "ch"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class OCRError(Exception):
    """Base class for OCR engine errors."""


class OCRInitError(OCRError):
    """The OCR model could not be created (missing package, download failure...)."""


class OCRInputError(OCRError):
    """The image handed to the engine is unusable."""


class OCRInferenceError(OCRError):
    """The model raised an error while processing an image."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int, minimum: int) -> int:
    """Read an integer environment variable, falling back on invalid input."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Ignoring invalid %s=%r (expected an integer); using %s", name, raw, default)
        return default


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read a float environment variable clamped to ``[minimum, maximum]``."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return min(maximum, max(minimum, float(raw)))
    except ValueError:
        logger.warning("Ignoring invalid %s=%r (expected a number); using %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable (``1/true/yes/on``)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OCREngineConfig:
    """Settings of the OCR engine.

    Attributes:
        lang: PaddleOCR language. ``latin`` (alias of ``tr``) recognises Turkish
            and English with one model; use ``en`` for English-only receipts.
        ocr_version: Force a model generation (``PP-OCRv3`` / ``PP-OCRv4``).
            ``None`` picks v4 for ``en``/``ch`` and v3 otherwise.
        use_angle_cls: Run the 0/180 degree text-direction classifier (slower).
        max_side: Longest image side fed to the detector, in pixels. Larger
            images are shrunk first; lower it to speed things up.
        cpu_threads: Inference threads.
        enable_mkldnn: Use Intel MKL-DNN acceleration (x86 only).
        min_confidence: Text lines scoring below this are dropped by PaddleOCR.
    """

    lang: str = "latin"
    ocr_version: Optional[str] = None
    use_angle_cls: bool = False
    max_side: int = 1920
    cpu_threads: int = 4
    enable_mkldnn: bool = False
    min_confidence: float = 0.3

    @property
    def resolved_version(self) -> str:
        """Model generation actually requested from PaddleOCR."""
        if self.ocr_version:
            return self.ocr_version
        return "PP-OCRv4" if self.lang in _LATIN_MODEL_LANGS else "PP-OCRv3"

    @classmethod
    def from_env(cls) -> "OCREngineConfig":
        """Build a configuration from ``OCR_*`` environment variables."""
        cpu_count = os.cpu_count() or 2
        is_x86 = platform.machine().lower() in {"x86_64", "amd64"}
        return cls(
            lang=(os.getenv("OCR_LANG") or "latin").strip() or "latin",
            ocr_version=(os.getenv("OCR_VERSION") or "").strip() or None,
            use_angle_cls=_env_bool("OCR_USE_ANGLE_CLS", False),
            max_side=_env_int("OCR_MAX_SIDE", 1920, minimum=320),
            cpu_threads=_env_int("OCR_CPU_THREADS", min(4, cpu_count), minimum=1),
            enable_mkldnn=_env_bool("OCR_ENABLE_MKLDNN", is_x86),
            min_confidence=_env_float("OCR_MIN_CONFIDENCE", 0.3, 0.0, 1.0),
        )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OCRLine:
    """One detected text line.

    Attributes:
        text: Recognised text (stripped, never empty).
        confidence: Recognition score in ``[0, 1]``.
        box: Four ``(x, y)`` corner points (TL, TR, BR, BL) in the pixel
            coordinates of the image passed to :meth:`OCREngine.recognize`.
    """

    text: str
    confidence: float
    box: tuple[tuple[float, float], ...]

    @property
    def x_min(self) -> float:
        """Left edge of the bounding rectangle."""
        return min(x for x, _ in self.box)

    @property
    def x_max(self) -> float:
        """Right edge of the bounding rectangle."""
        return max(x for x, _ in self.box)

    @property
    def y_min(self) -> float:
        """Top edge of the bounding rectangle."""
        return min(y for _, y in self.box)

    @property
    def y_max(self) -> float:
        """Bottom edge of the bounding rectangle."""
        return max(y for _, y in self.box)

    @property
    def width(self) -> float:
        """Width of the bounding rectangle."""
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        """Height of the bounding rectangle."""
        return self.y_max - self.y_min

    @property
    def x_center(self) -> float:
        """Horizontal centre of the bounding rectangle."""
        return (self.x_min + self.x_max) / 2.0

    @property
    def y_center(self) -> float:
        """Vertical centre of the bounding rectangle."""
        return (self.y_min + self.y_max) / 2.0


def group_into_rows(lines: Sequence[OCRLine], y_tolerance: float = 0.5) -> list[list[OCRLine]]:
    """Group detected boxes into visual rows, in reading order.

    Receipts print ``ITEM ........ 12,50`` as two separate boxes on one visual
    row; grouping them restores that pairing for the parser.

    Args:
        lines: Detected lines in any order.
        y_tolerance: Maximum vertical distance between a box centre and its
            row centre, as a fraction of the median box height.

    Returns:
        Rows from top to bottom; boxes inside each row from left to right.
    """
    if not lines:
        return []
    median_height = float(np.median([line.height for line in lines])) or 1.0
    threshold = y_tolerance * median_height

    rows: list[list[OCRLine]] = []
    row_center = 0.0
    for line in sorted(lines, key=lambda item: item.y_center):
        if rows and abs(line.y_center - row_center) <= threshold:
            rows[-1].append(line)
            row_center = sum(item.y_center for item in rows[-1]) / len(rows[-1])
        else:
            rows.append([line])
            row_center = line.y_center
    return [sorted(row, key=lambda item: item.x_min) for row in rows]


@dataclass(frozen=True)
class OCRResult:
    """Everything the OCR found in one image.

    Attributes:
        lines: Detected lines, in the detector's order (use :attr:`rows` for
            reading order).
        image_width: Width of the image given to the engine, in pixels.
        image_height: Height of the image given to the engine, in pixels.
        elapsed_ms: Inference time.
    """

    lines: tuple[OCRLine, ...]
    image_width: int
    image_height: int
    elapsed_ms: float

    @property
    def mean_confidence(self) -> float:
        """Average confidence over all lines (``0.0`` when nothing was found)."""
        if not self.lines:
            return 0.0
        return float(sum(line.confidence for line in self.lines) / len(self.lines))

    @property
    def rows(self) -> list[list[OCRLine]]:
        """Lines grouped into visual rows (see :func:`group_into_rows`)."""
        return group_into_rows(self.lines)

    @property
    def text(self) -> str:
        """Full text in reading order: one row per line, boxes joined by two spaces."""
        return "\n".join("  ".join(line.text for line in row) for row in self.rows)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _create_paddle_ocr(config: OCREngineConfig) -> Any:
    """Instantiate PaddleOCR (imported lazily: heavy import, optional in tests).

    Raises:
        OCRInitError: If the package is missing or the model cannot be created.
    """
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise OCRInitError(
            "paddleocr / paddlepaddle are not installed (pip install -r requirements.txt)."
        ) from exc

    try:
        return PaddleOCR(
            lang=config.lang,
            ocr_version=config.resolved_version,
            use_angle_cls=config.use_angle_cls,
            use_gpu=False,
            enable_mkldnn=config.enable_mkldnn,
            cpu_threads=config.cpu_threads,
            det_limit_type="max",
            det_limit_side_len=config.max_side,
            drop_score=config.min_confidence,
            show_log=False,
        )
    except Exception as exc:  # third-party constructor: download / model / runtime errors
        raise OCRInitError(f"Could not initialise PaddleOCR (lang={config.lang!r}): {exc}") from exc


def _prepare_image(image: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    """Validate ``image``, convert it to BGR and shrink it to ``max_side``.

    Returns:
        ``(prepared_image, scale)`` where ``scale >= 1`` converts prepared
        coordinates back to the coordinates of the input image.

    Raises:
        OCRInputError: For non-arrays, wrong dtype / shape or tiny images.
    """
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8:
        raise OCRInputError("Image must be a uint8 numpy array.")
    if image.ndim == 2:
        bgr = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    elif image.ndim == 3 and image.shape[2] == 3:
        bgr = image
    elif image.ndim == 3 and image.shape[2] == 4:
        bgr = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    else:
        raise OCRInputError(f"Unsupported image shape: {image.shape}.")

    height, width = bgr.shape[:2]
    if min(height, width) < _MIN_IMAGE_SIDE:
        raise OCRInputError(f"Image is too small ({width}x{height}).")

    longest = max(height, width)
    scale = 1.0
    if longest > max_side:
        scale = longest / max_side
        bgr = cv2.resize(
            bgr, (max(1, round(width / scale)), max(1, round(height / scale))), interpolation=cv2.INTER_AREA
        )
    return np.ascontiguousarray(bgr), scale


def _parse_paddle_output(raw: Any, scale: float) -> list[OCRLine]:
    """Convert PaddleOCR 2.x output into :class:`OCRLine` objects.

    PaddleOCR returns ``[page]`` where ``page`` is ``None`` (nothing found) or a
    list of ``[box, (text, score)]``. Malformed entries are skipped.
    """
    if not raw:
        return []
    page = raw[0]
    if not page:
        return []

    lines: list[OCRLine] = []
    for item in page:
        try:
            box, (text, score) = item[0], item[1]
            if not isinstance(text, str):
                raise TypeError(f"expected str text, got {type(text).__name__}")
            points = np.asarray(box, dtype=np.float64).reshape(4, 2) * scale
            clean_text = text.strip()
            if not clean_text:
                continue
            lines.append(
                OCRLine(
                    text=clean_text,
                    confidence=min(1.0, max(0.0, float(score))),
                    box=tuple((float(x), float(y)) for x, y in points),
                )
            )
        except (TypeError, ValueError, IndexError) as exc:
            logger.debug("Skipping malformed OCR entry %r: %s", item, exc)
    return lines


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class OCREngine:
    """Thread-safe facade over one PaddleOCR instance.

    Prefer :func:`get_ocr_engine` so the model is loaded only once per process.
    """

    def __init__(self, config: Optional[OCREngineConfig] = None) -> None:
        """Load the model.

        Args:
            config: Engine settings; defaults to :meth:`OCREngineConfig.from_env`.

        Raises:
            OCRInitError: If the model cannot be created.
        """
        self._config = config or OCREngineConfig.from_env()
        started = time.perf_counter()
        self._ocr = _create_paddle_ocr(self._config)
        self._lock = threading.Lock()
        logger.info(
            "OCR engine ready in %.1fs (lang=%s, version=%s, threads=%d, mkldnn=%s, angle_cls=%s)",
            time.perf_counter() - started,
            self._config.lang,
            self._config.resolved_version,
            self._config.cpu_threads,
            self._config.enable_mkldnn,
            self._config.use_angle_cls,
        )

    @property
    def config(self) -> OCREngineConfig:
        """Settings this engine was created with."""
        return self._config

    def recognize(self, image: np.ndarray) -> OCRResult:
        """Detect and read all text lines in ``image``.

        Blocking and CPU bound: from async code use :meth:`recognize_async`.

        Args:
            image: ``uint8`` BGR, BGRA or grayscale image (e.g. the ``enhanced``
                output of the pre-processor).

        Returns:
            An :class:`OCRResult`; box coordinates refer to ``image``.

        Raises:
            OCRInputError: If ``image`` is unusable.
            OCRInferenceError: If the model fails.
        """
        prepared, scale = _prepare_image(image, self._config.max_side)
        started = time.perf_counter()
        try:
            with self._lock:
                raw = self._ocr.ocr(prepared, cls=self._config.use_angle_cls)
        except Exception as exc:  # third-party inference: any runtime failure
            raise OCRInferenceError(f"OCR inference failed: {exc}") from exc

        lines = _parse_paddle_output(raw, scale)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.debug("OCR found %d lines in %.0f ms", len(lines), elapsed_ms)
        return OCRResult(
            lines=tuple(lines),
            image_width=int(image.shape[1]),
            image_height=int(image.shape[0]),
            elapsed_ms=elapsed_ms,
        )

    async def recognize_async(self, image: np.ndarray) -> OCRResult:
        """Run :meth:`recognize` in a worker thread so the event loop stays free."""
        return await asyncio.to_thread(self.recognize, image)

    def warmup(self) -> None:
        """Run one inference on a blank page so the first real request is fast."""
        self.recognize(np.full((96, 320, 3), 255, dtype=np.uint8))


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------
_engine: Optional[OCREngine] = None
_engine_lock = threading.Lock()


def get_ocr_engine(config: Optional[OCREngineConfig] = None) -> OCREngine:
    """Return the process-wide :class:`OCREngine`, creating it on first use.

    Creation is guarded by a lock, so concurrent first requests load the model
    exactly once. A failed creation is not cached: the next call retries.

    Args:
        config: Only honoured by the call that creates the engine.

    Raises:
        OCRInitError: If the model cannot be created.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = OCREngine(config)
    return _engine


def reset_ocr_engine() -> None:
    """Drop the singleton (tests, configuration reloads)."""
    global _engine
    with _engine_lock:
        _engine = None
