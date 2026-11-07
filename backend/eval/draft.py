"""Label assistant: writes a draft ``labels.json`` from the system's own reading.

Usage (from ``backend/``)::

    python -m eval.draft D:\\fis_seti

Labelling 50 receipts by hand is slow; correcting a draft is fast. For every image in the folder
the pipeline runs once and the result is written as the starting label. Open the file, **check
every value against the photo and fix the wrong ones**; only then run
``python -m eval.evaluate --dir D:\\fis_seti``.

Each entry also carries ``"_oku"`` notes (not scored): what the system thought of its own reading,
so you can start with the receipts it was unsure about. Never trust a draft you did not check:
an unchecked draft would score the system against itself.

Existing ``labels.json`` is never overwritten; the draft goes to ``labels.draft.json`` then.
File names may carry a capture condition after ``__`` (``migros__kotu.jpg``) which the report
groups by.
"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from eval.evaluate import read_image
from eval.metrics import condition_of

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
LABEL_FIELDS = ("merchant_name", "date", "subtotal_amount", "tax_amount", "total_amount", "currency", "category")


def _plain(value: Any) -> Any:
    """JSON-friendly value: enums to their value, Decimals to strings."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def draft_entry(got: dict[str, Any], filename: str) -> dict[str, Any]:
    """Draft label for one receipt from a pipeline result (empty ``got`` = the read failed)."""
    entry: dict[str, Any] = {name: _plain(got.get(name)) for name in LABEL_FIELDS}
    status = _plain(got.get("status"))
    notes = [f"koşul: {condition_of(filename)}", f"sistem durumu: {status or 'okunamadı'}"]
    missing = [name for name in ("total_amount", "tax_amount", "date") if entry.get(name) is None]
    if missing:
        notes.append("bulunamadı: " + ", ".join(missing))
    entry["_oku"] = notes
    return entry


async def build_draft(directory: Path) -> dict[str, dict[str, Any]]:
    """Run the pipeline on every image of ``directory`` and return the draft labels."""
    draft: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            print(f"! cannot read {path.name}")
            continue
        try:
            got = await read_image(image)
        except Exception as exc:  # one bad image must not stop the run
            print(f"! {path.name} failed: {exc}")
            got = {}
        draft[path.name] = draft_entry(got, path.name)
        print(f"  {path.name}: {draft[path.name]['_oku'][1]}")
    return draft


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path, help="folder with the receipt photos")
    args = parser.parse_args()
    if not args.directory.is_dir():
        raise SystemExit(f"Klasör bulunamadı: {args.directory}")
    draft = asyncio.run(build_draft(args.directory))
    target = args.directory / "labels.json"
    if target.exists():
        target = args.directory / "labels.draft.json"
    target.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(draft)} fiş için taslak yazıldı: {target}\nHer değeri fotoğrafla karşılaştırıp düzeltin, sonra:")
    print(f"  python -m eval.evaluate --dir {args.directory}")


if __name__ == "__main__":
    main()
