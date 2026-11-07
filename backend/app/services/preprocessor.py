"""Receipt image pre-processing (OpenCV + NumPy only, CPU friendly).

Pipeline run by :func:`preprocess_receipt`:

1. **Decode** raw bytes (JPEG / PNG / WebP / BMP) into a BGR array. EXIF
   orientation is applied by OpenCV, so phone photos arrive upright.
2. **Find the paper** on a down-scaled copy (brightness + edge based) and take
   the four corner points.
3. **Warp perspective** so the receipt becomes a flat, upright rectangle.
4. **Enhance faded thermal print**: flatten uneven lighting / shadows, stretch
   the contrast of weak strokes, denoise lightly and sharpen.
5. Optionally **binarize** with adaptive thresholding (A/B option for the OCR).

All heavy work is CPU bound; call it from a worker thread
(``asyncio.to_thread``) when used inside an async endpoint.
"""

import logging
from dataclasses import dataclass
from typing import Final, Optional

import cv2
import numpy as np

__all__ = [
    "ImageProcessingError",
    "ImageDecodeError",
    "PreprocessResult",
    "decode_image",
    "order_corners",
    "find_document_corners",
    "four_point_transform",
    "enhance_for_ocr",
    "binarize",
    "preprocess_receipt",
]

logger = logging.getLogger(__name__)

# --- Limits (keep memory / CPU use low on weak machines) --------------------
MAX_IMAGE_BYTES: Final[int] = 20 * 1024 * 1024  # reject uploads above 20 MiB
MAX_INPUT_SIDE: Final[int] = 3200  # decoded image is shrunk to this longest side
MAX_OUTPUT_SIDE: Final[int] = 2400  # warped / enhanced image longest side
MIN_OUTPUT_WIDTH: Final[int] = 640  # narrower results are up-scaled

# --- Paper detection ---------------------------------------------------------
DETECT_MAX_SIDE: Final[int] = 800  # detection runs on a copy of this size
MIN_AREA_RATIO: Final[float] = 0.05  # paper must cover >= 5 % of the frame
MAX_AREA_RATIO: Final[float] = 0.97  # >= 97 % means "no distinct paper edge"
MIN_SIDE_RATIO: Final[float] = 0.05  # shortest quad edge vs. longest frame side
MIN_ANGLE_DEG: Final[float] = 45.0
MAX_ANGLE_DEG: Final[float] = 135.0
CORNER_INSET_RATIO: Final[float] = 0.01  # shrink quad to drop table edge pixels
MIN_EDGE_SUPPORT: Final[float] = 0.5  # >= 50 % of the outline must sit on an edge

# --- Enhancement -------------------------------------------------------------
NOISE_FLOOR: Final[int] = 6  # ink darkness below this is treated as paper noise
MIN_INK_LEVEL: Final[int] = 24  # avoids amplifying noise on near-blank pages
INK_PERCENTILE: Final[float] = 0.997
# Laplacian variance of the *enhanced* page at 800 px width. Calibrated on
# synthetic receipts only (sharp >= ~1500, clearly blurred <= ~200): tune it
# with real photos from the target cameras.
BLUR_SHARPNESS_THRESHOLD: Final[float] = 250.0


class ImageProcessingError(Exception):
    """Raised when an image cannot be processed."""


class ImageDecodeError(ImageProcessingError):
    """Raised when the uploaded bytes are not a valid / acceptable image."""


@dataclass(frozen=True, eq=False)
class PreprocessResult:
    """Output of :func:`preprocess_receipt`.

    Attributes:
        original: Decoded BGR image (shrunk to ``MAX_INPUT_SIDE``).
        warped: Perspective-corrected BGR image (or the full frame when no
            paper edges were found).
        enhanced: Contrast-enhanced 3-channel BGR image, ready for the OCR.
        binary: Adaptive-threshold version (single channel, 0 / 255).
        corners: Paper corners in ``original`` coordinates ordered
            top-left, top-right, bottom-right, bottom-left; ``None`` if not found.
        document_found: ``True`` when the paper edges were detected and warped.
        sharpness: Laplacian variance of ``enhanced`` at 800 px width (higher =
            sharper). Measured after contrast stretching so faint but sharp
            thermal print is not mistaken for blur.
        warnings: Non-fatal quality remarks for the caller / UI.
    """

    original: np.ndarray
    warped: np.ndarray
    enhanced: np.ndarray
    binary: np.ndarray
    corners: Optional[np.ndarray]
    document_found: bool
    sharpness: float
    warnings: tuple[str, ...]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _odd(value: int) -> int:
    """Return ``value`` if odd, otherwise ``value + 1``."""
    return value if value % 2 == 1 else value + 1


def _to_gray(image: np.ndarray) -> np.ndarray:
    """Convert a 2-D / BGR / BGRA array to a single-channel 8-bit image."""
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    raise ImageProcessingError(f"Unsupported image shape: {image.shape}")


def _resize_to_max_side(image: np.ndarray, max_side: int) -> np.ndarray:
    """Shrink ``image`` (keeping aspect ratio) so its longest side is ``max_side``."""
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_side:
        return image
    scale = max_side / longest
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)


def _fit_output_size(image: np.ndarray) -> np.ndarray:
    """Cap the longest side and up-scale very narrow results.

    Up-scaling stops as soon as the longest side reaches ``MAX_OUTPUT_SIDE``.
    """
    image = _resize_to_max_side(image, MAX_OUTPUT_SIDE)
    height, width = image.shape[:2]
    factor = min(MIN_OUTPUT_WIDTH / width, MAX_OUTPUT_SIDE / max(height, width))
    if factor > 1.05:
        new_size = (round(width * factor), round(height * factor))
        image = cv2.resize(image, new_size, interpolation=cv2.INTER_CUBIC)
    return image


# ---------------------------------------------------------------------------
# 1. Decoding
# ---------------------------------------------------------------------------
def decode_image(image_bytes: bytes) -> np.ndarray:
    """Decode raw image bytes into a BGR ``uint8`` array.

    EXIF orientation is honoured. The result is shrunk to ``MAX_INPUT_SIDE`` on
    its longest side to bound memory and CPU use.

    Args:
        image_bytes: Encoded image (JPEG, PNG, WebP, BMP, ...).

    Returns:
        BGR image of shape ``(H, W, 3)``.

    Raises:
        ImageDecodeError: If the payload is empty, too large or not an image.
    """
    if not image_bytes:
        raise ImageDecodeError("Görsel boş.")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ImageDecodeError(
            f"Görsel çok büyük ({len(image_bytes) / 1_048_576:.1f} MB); sınır {MAX_IMAGE_BYTES // 1_048_576} MB."
        )
    try:
        buffer = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise ImageDecodeError(f"Görsel açılamadı: {exc}") from exc
    if image is None or image.size == 0:
        raise ImageDecodeError("Dosya bir görsel değil (JPEG, PNG, WebP veya BMP bekleniyor).")
    return _resize_to_max_side(image, MAX_INPUT_SIDE)


# ---------------------------------------------------------------------------
# 2. Paper detection
# ---------------------------------------------------------------------------
def order_corners(points: np.ndarray) -> np.ndarray:
    """Order four points as top-left, top-right, bottom-right, bottom-left.

    Args:
        points: Array convertible to shape ``(4, 2)``.

    Returns:
        ``float32`` array of shape ``(4, 2)``.
    """
    pts = np.asarray(points, dtype=np.float32).reshape(4, 2)
    by_y = pts[np.argsort(pts[:, 1])]
    top = by_y[:2][np.argsort(by_y[:2, 0])]
    bottom = by_y[2:][np.argsort(by_y[2:, 0])]
    return np.array([top[0], top[1], bottom[1], bottom[0]], dtype=np.float32)


def _contour_to_quad(contour: np.ndarray) -> Optional[np.ndarray]:
    """Reduce a contour to four corner points.

    Tries a polygon approximation of the convex hull first (keeps perspective);
    falls back to the minimum-area rectangle if the shape is rectangle-like.
    """
    hull = cv2.convexHull(contour)
    perimeter = cv2.arcLength(hull, True)
    if perimeter <= 0:
        return None
    for epsilon in (0.02, 0.03, 0.04, 0.05, 0.07):
        approx = cv2.approxPolyDP(hull, epsilon * perimeter, True)
        if len(approx) == 4:
            return approx.reshape(4, 2).astype(np.float32)

    rect = cv2.minAreaRect(hull)
    rect_area = float(rect[1][0] * rect[1][1])
    if rect_area > 0 and cv2.contourArea(hull) / rect_area >= 0.85:
        return cv2.boxPoints(rect).astype(np.float32)
    return None


def _is_plausible_quad(quad: np.ndarray, width: int, height: int) -> bool:
    """Reject slivers and extreme shapes (edge length and corner-angle checks)."""
    ordered = order_corners(quad)
    edges = ordered[(np.arange(4) + 1) % 4] - ordered
    lengths = np.linalg.norm(edges, axis=1)
    if lengths.min() < MIN_SIDE_RATIO * max(width, height):
        return False
    for i in range(4):
        a, b = -edges[i - 1], edges[i]  # vectors leaving corner i
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        if denom == 0:
            return False
        cosine = float(np.clip(np.dot(a, b) / denom, -1.0, 1.0))
        if not MIN_ANGLE_DEG <= np.degrees(np.arccos(cosine)) <= MAX_ANGLE_DEG:
            return False
    return True


def _edge_support(quad: np.ndarray, edge_map: np.ndarray) -> float:
    """Fraction of the quad outline that lies on a real image edge.

    A brightness split caused by shadows / lighting gradients forms a clean
    rectangle but has no edge under its outline, unlike a true paper border.
    Outline points on the frame border are ignored (the frame is not an edge).

    Args:
        quad: Four corner points (any order).
        edge_map: Binary (dilated) edge image of the same scale as ``quad``.

    Returns:
        Support ratio in ``[0, 1]``; ``1.0`` if no outline point is testable.
    """
    ordered = order_corners(quad)
    height, width = edge_map.shape
    hits = 0
    testable = 0
    for i in range(4):
        start, end = ordered[i], ordered[(i + 1) % 4]
        for t in np.linspace(0.0, 1.0, 80):
            x, y = start + (end - start) * t
            col, row = int(round(float(x))), int(round(float(y)))
            if col < 3 or row < 3 or col >= width - 3 or row >= height - 3:
                continue
            testable += 1
            hits += int(edge_map[row, col] > 0)
    return hits / testable if testable else 1.0


def _candidate_masks(blurred_gray: np.ndarray) -> list[np.ndarray]:
    """Build binary masks in which the paper should be one connected blob."""
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    masks: list[np.ndarray] = []

    # Brightness: paper is usually brighter than the table; the inverse covers
    # the opposite case (dark paper). Invalid blobs are filtered out later.
    _, bright = cv2.threshold(blurred_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    for mask in (bright, cv2.bitwise_not(bright)):
        masks.append(cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel))

    # Edges: closed paper outline even when brightness contrast is weak.
    edges = cv2.Canny(blurred_gray, 40, 120)
    edges = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)), iterations=2)
    masks.append(cv2.morphologyEx(edges, cv2.MORPH_CLOSE, close_kernel))
    return masks


def find_document_corners(image: np.ndarray) -> Optional[np.ndarray]:
    """Locate the receipt / paper quadrilateral in a BGR image.

    Detection runs on a copy limited to ``DETECT_MAX_SIDE`` pixels and the
    corners are scaled back to the coordinates of ``image``.

    Args:
        image: BGR (or grayscale) image.

    Returns:
        ``float32`` array ``(4, 2)`` ordered TL, TR, BR, BL, or ``None`` when no
        distinct paper outline was found (e.g. the frame already is the paper,
        or the paper has no contrast against its background).

    Raises:
        ImageProcessingError: If the image has an unsupported shape.
    """
    height, width = image.shape[:2]
    scale = min(1.0, DETECT_MAX_SIDE / max(height, width))
    small = (
        cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else image
    )
    gray = cv2.GaussianBlur(_to_gray(small), (7, 7), 0)
    small_h, small_w = gray.shape
    frame_area = float(small_h * small_w)
    # Sensitive edge map (low thresholds) used only to validate candidates.
    support_edges = cv2.dilate(
        cv2.Canny(gray, 20, 50), cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    )

    # Masks are tried in priority order (tight brightness mask first, the
    # edge mask - which is inflated by dilation - only as a fallback); the first
    # mask that yields a valid quad wins, so a looser mask cannot override it.
    best: Optional[np.ndarray] = None
    for mask in _candidate_masks(gray):
        best_area = 0.0
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            if cv2.contourArea(contour) / frame_area < MIN_AREA_RATIO:
                break  # sorted by area: the rest are smaller
            quad = _contour_to_quad(contour)
            if quad is None or not _is_plausible_quad(quad, small_w, small_h):
                continue
            quad_area = float(cv2.contourArea(quad))
            if quad_area / frame_area > MAX_AREA_RATIO or quad_area / frame_area < MIN_AREA_RATIO:
                continue
            if _edge_support(quad, support_edges) < MIN_EDGE_SUPPORT:
                continue
            if quad_area > best_area:
                best, best_area = quad, quad_area
        if best is not None:
            break

    if best is None:
        return None
    corners = order_corners(best / scale)
    corners[:, 0] = np.clip(corners[:, 0], 0, width - 1)
    corners[:, 1] = np.clip(corners[:, 1], 0, height - 1)
    return corners


# ---------------------------------------------------------------------------
# 3. Perspective correction
# ---------------------------------------------------------------------------
def four_point_transform(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Warp the quadrilateral ``corners`` of ``image`` into an upright rectangle.

    The output size follows the real edge lengths, so the pixel density of the
    camera is preserved (then capped / floored by :func:`_fit_output_size`).

    Args:
        image: Source BGR image.
        corners: Four points ordered TL, TR, BR, BL (see :func:`order_corners`).

    Returns:
        The rectified BGR image.

    Raises:
        ImageProcessingError: If the quadrilateral is degenerate or OpenCV fails.
    """
    quad = order_corners(corners)
    centre = quad.mean(axis=0)
    quad = centre + (quad - centre) * (1.0 - CORNER_INSET_RATIO)
    top_left, top_right, bottom_right, bottom_left = quad

    width = int(round(max(np.linalg.norm(top_right - top_left), np.linalg.norm(bottom_right - bottom_left))))
    height = int(round(max(np.linalg.norm(bottom_left - top_left), np.linalg.norm(bottom_right - top_right))))
    if width < 8 or height < 8:
        raise ImageProcessingError("Detected paper region is degenerate.")

    destination = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    try:
        matrix = cv2.getPerspectiveTransform(quad.astype(np.float32), destination)
        warped = cv2.warpPerspective(
            image,
            matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
    except cv2.error as exc:
        raise ImageProcessingError(f"Perspective transform failed: {exc}") from exc
    return _fit_output_size(warped)


# ---------------------------------------------------------------------------
# 4 + 5. Enhancement and binarisation
# ---------------------------------------------------------------------------
def _flatten_background(gray: np.ndarray) -> np.ndarray:
    """Return "ink darkness": how much darker each pixel is than the local paper.

    The paper background is estimated on a reduced copy by a grayscale closing
    (which erases thin dark text strokes) followed by smoothing, then upscaled.
    Subtracting the gray image from it removes shadows and uneven lighting.
    """
    height, width = gray.shape
    side = max(height, width)
    downscale = max(1, side // 600)
    small = gray if downscale == 1 else cv2.resize(
        gray, (max(1, width // downscale), max(1, height // downscale)), interpolation=cv2.INTER_AREA
    )
    kernel_size = _odd(max(5, round(side / 150 / downscale)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    background = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel)
    background = cv2.GaussianBlur(background, (0, 0), sigmaX=3)
    if downscale > 1:
        background = cv2.resize(background, (width, height), interpolation=cv2.INTER_LINEAR)
    return cv2.subtract(background, gray)  # saturating: brighter-than-paper -> 0


def _enhance_gray(gray: np.ndarray) -> np.ndarray:
    """Core enhancement on a single channel; returns dark text on white paper."""
    denoised = cv2.medianBlur(gray, 3)  # cheap, keeps stroke edges
    ink = _flatten_background(denoised)

    histogram = np.bincount(ink.ravel(), minlength=256)
    cumulative = np.cumsum(histogram) / ink.size
    strong_ink = int(np.searchsorted(cumulative, INK_PERCENTILE))
    strong_ink = max(strong_ink, MIN_INK_LEVEL)

    # Map [NOISE_FLOOR, strong_ink] -> [0, 255]: faint thermal strokes become dark.
    stretched = (ink.astype(np.float32) - NOISE_FLOOR) * (255.0 / (strong_ink - NOISE_FLOOR))
    page = 255.0 - np.clip(stretched, 0.0, 255.0)
    page_u8 = page.astype(np.uint8)

    softened = cv2.GaussianBlur(page_u8, (0, 0), sigmaX=1.2)
    return cv2.addWeighted(page_u8, 1.4, softened, -0.4, 0)  # unsharp mask


def enhance_for_ocr(image: np.ndarray) -> np.ndarray:
    """Sharpen faded / low-contrast (thermal) print and even out the lighting.

    Args:
        image: BGR, BGRA or grayscale ``uint8`` image.

    Returns:
        3-channel BGR image (dark text on a clean white page) that can be fed
        directly to PaddleOCR.

    Raises:
        ImageProcessingError: If the image shape / dtype is unsupported or
            OpenCV fails.
    """
    if image.dtype != np.uint8:
        raise ImageProcessingError(f"Expected a uint8 image, got {image.dtype}.")
    try:
        return cv2.cvtColor(_enhance_gray(_to_gray(image)), cv2.COLOR_GRAY2BGR)
    except cv2.error as exc:
        raise ImageProcessingError(f"Enhancement failed: {exc}") from exc


def binarize(image: np.ndarray) -> np.ndarray:
    """Adaptive (Gaussian) thresholding for hard black / white output.

    Args:
        image: Grayscale or BGR ``uint8`` image (ideally the output of
            :func:`enhance_for_ocr`).

    Returns:
        Single-channel image containing only 0 and 255.

    Raises:
        ImageProcessingError: If OpenCV fails or the image shape is unsupported.
    """
    gray = _to_gray(image)
    block_size = _odd(max(15, gray.shape[1] // 30))
    try:
        return cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, 10
        )
    except cv2.error as exc:
        raise ImageProcessingError(f"Adaptive thresholding failed: {exc}") from exc


def _sharpness(image: np.ndarray) -> float:
    """Laplacian variance measured at a normalised 800 px width."""
    gray = _to_gray(image)
    height, width = gray.shape
    if width > 800:
        gray = cv2.resize(gray, (800, max(1, round(height * 800 / width))), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def preprocess_receipt(image_bytes: bytes) -> PreprocessResult:
    """Run the full pre-processing pipeline on an uploaded / captured image.

    If the paper outline cannot be found (or warping fails) the whole frame is
    used, so the OCR still gets an image; ``document_found`` and ``warnings``
    tell the caller what happened.

    Args:
        image_bytes: Encoded image as received from the API.

    Returns:
        A :class:`PreprocessResult`.

    Raises:
        ImageDecodeError: If the bytes are not a usable image.
        ImageProcessingError: If enhancement fails unexpectedly.
    """
    original = decode_image(image_bytes)
    warnings: list[str] = []

    corners: Optional[np.ndarray] = None
    try:
        corners = find_document_corners(original)
    except (cv2.error, ImageProcessingError) as exc:
        logger.warning("Paper detection failed, using the full frame: %s", exc)

    warped: Optional[np.ndarray] = None
    if corners is not None:
        try:
            warped = four_point_transform(original, corners)
        except ImageProcessingError as exc:
            logger.warning("Perspective correction failed, using the full frame: %s", exc)
            corners = None
    if warped is None:
        warped = _fit_output_size(original)
        warnings.append("Fişin kenarları bulunamadı, tüm görüntü kullanıldı. Fişi koyu ve düz bir zemine koyup tamamı görünecek şekilde çekmeniz önerilir.")

    enhanced = enhance_for_ocr(warped)
    binary = binarize(enhanced)
    sharpness = _sharpness(enhanced)
    if sharpness < BLUR_SHARPNESS_THRESHOLD:
        warnings.append("Fotoğraf bulanık görünüyor. Telefonu sabit tutup ışığı artırarak tekrar çekmeniz önerilir.")

    logger.debug(
        "Pre-processed %sx%s -> %sx%s (document_found=%s, sharpness=%.1f)",
        original.shape[1], original.shape[0], warped.shape[1], warped.shape[0],
        corners is not None, sharpness,
    )
    return PreprocessResult(
        original=original,
        warped=warped,
        enhanced=enhanced,
        binary=binary,
        corners=corners,
        document_found=corners is not None,
        sharpness=sharpness,
        warnings=tuple(warnings),
    )
