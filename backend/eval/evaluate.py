"""Field-level accuracy of the whole pipeline (pre-process -> OCR -> parse -> reconcile).

Usage (from ``backend/``)::

    python -m eval.evaluate                       # bundled samples + synthetic variants
    python -m eval.evaluate --dir my_receipts     # your own photos + labels.json
    python -m eval.evaluate --out eval/RESULTS.md # also write the Markdown table

``labels.json`` maps a file name to the true values (see the bundled one). Only
the fields present in a label are scored.

The bundled data are *fictional, synthetic* receipts plus synthetic degradations
(rotation, blur, noise, dim light, skew). They show how robust the pipeline is,
not how it performs on the real world: add real, anonymised receipts via ``--dir``
and report that number.
"""

import argparse
import asyncio
import json
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import numpy as np

from app.services.categorizer import categorize
from eval.metrics import CRITICAL_FIELDS, Record, condition_of, is_verified, render_receipt_metrics
from app.services.ocr_engine import get_ocr_engine
from app.services.orientation import needs_rotation_check, recover_orientation
from app.services.parser import parse_receipt
from app.services.preprocessor import preprocess_receipt
from app.services.reconciler import reconcile

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parents[1] / "frontend" / "samples"
FIELDS = ("merchant_name", "date", "subtotal_amount", "tax_amount", "total_amount", "currency", "category", "status")

Transform = Callable[[np.ndarray], np.ndarray]


# ---------------------------------------------------------------------------
# Synthetic degradations
# ---------------------------------------------------------------------------
def _rot(code: int) -> Transform:
    """Rotate by an OpenCV rotation code."""
    return lambda image: cv2.rotate(image, code)


def _blur(image: np.ndarray) -> np.ndarray:
    """Out-of-focus camera."""
    return cv2.GaussianBlur(image, (0, 0), 1.6)


def _noise(image: np.ndarray) -> np.ndarray:
    """Sensor noise (fixed seed, so runs are repeatable)."""
    rng = np.random.default_rng(7)
    noisy = image.astype(np.int16) + rng.normal(0, 14, image.shape).astype(np.int16)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def _dim(image: np.ndarray) -> np.ndarray:
    """Poor lighting."""
    return cv2.convertScaleAbs(image, alpha=0.55, beta=-10)


def _skew(image: np.ndarray) -> np.ndarray:
    """Slightly tilted photo."""
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), 6, 1.0)
    return cv2.warpAffine(image, matrix, (width, height), borderValue=(70, 60, 50))


VARIANTS: dict[str, Transform] = {
    "original": lambda image: image,
    "rotated 90": _rot(cv2.ROTATE_90_CLOCKWISE),
    "rotated 180": _rot(cv2.ROTATE_180),
    "rotated 270": _rot(cv2.ROTATE_90_COUNTERCLOCKWISE),
    "blur": _blur,
    "noise": _noise,
    "dim light": _dim,
    "skew 6 deg": _skew,
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _fold(text: str) -> str:
    """Accent-free upper-case alphanumerics (``Örnek Market A.Ş.`` -> ``ORNEKMARKETAS``)."""
    stripped = "".join(c for c in unicodedata.normalize("NFKD", text) if unicodedata.category(c) != "Mn")
    return "".join(c for c in stripped.upper() if c.isalnum())


def _equal(field_name: str, expected: str, actual: Any) -> bool:
    """Compare one extracted value with its label."""
    if actual is None:
        return False
    if field_name == "merchant_name":
        return _fold(str(actual)) == _fold(expected)
    if field_name.endswith("_amount"):
        return abs(float(actual) - float(expected)) < 0.005
    return str(getattr(actual, "value", actual)) == expected


@dataclass
class Tally:
    """Correct / total counters per field and per variant."""

    field_hits: dict[str, list[int]] = field(default_factory=lambda: {name: [0, 0] for name in FIELDS})
    variant_hits: dict[str, list[int]] = field(default_factory=dict)
    seconds: list[float] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)

    def add(self, variant: str, name: str, ok: bool) -> None:
        """Record one scored field."""
        self.field_hits[name][0] += int(ok)
        self.field_hits[name][1] += 1
        bucket = self.variant_hits.setdefault(variant, [0, 0])
        bucket[0] += int(ok)
        bucket[1] += 1


async def read_image(image: np.ndarray) -> dict[str, Any]:
    """Run the production pipeline on one decoded image and return the extracted fields."""
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("could not encode image")
    engine = get_ocr_engine()
    pre = await asyncio.to_thread(preprocess_receipt, encoded.tobytes())
    ocr = await engine.recognize_async(pre.enhanced)
    parsed = parse_receipt(ocr)
    if needs_rotation_check(parsed):
        better = await recover_orientation(engine, pre.enhanced, pre.warped, ocr, parsed)
        parsed = better.parsed
    verdict = reconcile(parsed.subtotal_amount, parsed.tax_amount, parsed.total_amount)
    return {
        "merchant_name": parsed.merchant_name,
        "date": parsed.date.isoformat() if parsed.date else None,
        "subtotal_amount": parsed.subtotal_amount,
        "tax_amount": parsed.tax_amount,
        "total_amount": parsed.total_amount,
        "currency": parsed.currency,
        "category": categorize(parsed.merchant_name, parsed.raw_text),
        "status": verdict.status,
    }


async def evaluate(directory: Path, labels: dict[str, dict[str, str]], variants: dict[str, Transform]) -> Tally:
    """Score every labelled image under every variant."""
    tally = Tally()
    for filename, truth in labels.items():
        truth = {name: value for name, value in truth.items() if value is not None}  # unlabelled = not scored
        path = directory / filename
        image: Optional[np.ndarray] = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            print(f"! cannot read {path}")
            continue
        for variant, transform in variants.items():
            started = time.perf_counter()
            try:
                got = await read_image(transform(image))
            except Exception as exc:  # one bad image must not stop the run
                print(f"! {filename} [{variant}] failed: {exc}")
                got = {}
            tally.seconds.append(time.perf_counter() - started)
            for name, expected in truth.items():
                if name in FIELDS:
                    tally.add(variant, name, _equal(name, expected, got.get(name)))
            wrong = tuple(
                name for name in CRITICAL_FIELDS if name in truth and not _equal(name, truth[name], got.get(name))
            )
            condition = variant if len(variants) > 1 else condition_of(filename)
            verified = is_verified(got.get("status"))
            expect_flag = truth.get("status") == "flagged_for_review"
            if expect_flag and verified:
                wrong += ("status",)  # a receipt that does not add up was approved
            tally.records.append(Record(filename, condition, wrong, verified, expect_flag))
    return tally


def _pct(hit: int, total: int) -> str:
    """Percentage text (``-`` when nothing was scored)."""
    return f"{100 * hit / total:.0f}%" if total else "-"


def render(tally: Tally, images: int, variants: int, synthetic: bool) -> str:
    """Markdown report."""
    note = (
        "> Synthetic fictional receipts + synthetic degradations: a robustness check, not real-world accuracy."
        if synthetic
        else "> Evaluated on user-supplied receipts."
    )
    lines = [
        f"# Pipeline accuracy ({images} receipts x {variants} variants = {images * variants} runs)",
        "",
        note,
        "",
        "| Field | Accuracy |",
        "|---|---|",
    ]
    for name, (hit, total) in tally.field_hits.items():
        lines.append(f"| {name} | {_pct(hit, total)} ({hit}/{total}) |")
    lines += ["", "| Variant | Field accuracy |", "|---|---|"]
    for variant, (hit, total) in tally.variant_hits.items():
        lines.append(f"| {variant} | {_pct(hit, total)} |")
    mean = sum(tally.seconds) / len(tally.seconds) if tally.seconds else 0.0
    lines += ["", f"Mean time per receipt (CPU): {mean:.1f} s", ""]
    return render_receipt_metrics(tally.records) + "\n" + "\n".join(lines) + "\n"


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, help="folder with images and labels.json (default: bundled samples)")
    parser.add_argument("--no-variants", action="store_true", help="evaluate the images as they are")
    parser.add_argument("--out", type=Path, help="write the Markdown report here")
    args = parser.parse_args()

    directory = args.dir or SAMPLES
    labels_path = (args.dir / "labels.json") if args.dir else HERE / "labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    variants = {"original": VARIANTS["original"]} if (args.no_variants or args.dir) else VARIANTS

    tally = asyncio.run(evaluate(directory, labels, variants))
    report = render(tally, len(labels), len(variants), synthetic=args.dir is None)
    print(report)
    if args.out:
        args.out.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
