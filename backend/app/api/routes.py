"""HTTP API (``/api/v1``).

* ``POST /extract-receipt`` - image -> pre-process -> OCR -> parse -> reconcile -> save.
* ``GET  /expenses``        - filter / paginate stored expenses.
* ``GET  /expenses/export.csv`` - the same filters, as a CSV download (Excel friendly).
* ``GET  /expenses/{id}``    - one expense with the reason for its reconciliation status.
* ``GET  /expenses/{id}/image`` - stored receipt image.
* ``PATCH /expenses/{id}``  - correct values, status is recomputed.
* ``DELETE /expenses/{id}`` - remove an expense and its image.
* ``GET  /stats``           - totals by currency / month / category / merchant.
* ``GET  /merchants``       - merchants (spelling variants merged) with their learned category.
* ``POST /merchants/merge`` - declare two merchant keys to be the same shop.
* ``GET  /health``          - liveness + database check.

CPU-bound work (image processing, OCR) runs in worker threads so the event loop
stays responsive. The warped receipt image is stored under ``UPLOAD_DIR``
(default ``./data/uploads``) with a random file name; the client-supplied file
name is never used.
"""

import asyncio
import csv
import io
import logging
import os
import time
import uuid
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Optional

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db.database import check_db_connection, get_db
from app.db.models import Expense, MerchantRule
from app.schemas.receipt import (
    Category,
    CategoryTotal,
    Currency,
    CurrencyTotal,
    ErrorResponse,
    ExpenseFilter,
    ExportQuery,
    HealthResponse,
    MerchantInfo,
    MerchantMergeRequest,
    MerchantMergeResponse,
    MerchantTotal,
    MonthlyTotal,
    ReceiptCreate,
    ReceiptListQuery,
    ReconciliationStatus,
    StatsResponse,
    OCRBox,
    ReceiptListResponse,
    ReceiptDetailResponse,
    ReceiptProcessResponse,
    ReceiptRead,
    ReconciliationDetail,
    ReceiptSummary,
    ReceiptUpdate,
)
from app.services.categorizer import categorize
from app.services.merchant import canonical_key, merchant_key
from app.services.orientation import needs_rotation_check, recover_orientation
from app.services.ocr_engine import OCRError, OCRInitError, OCRInputError, get_ocr_engine
from app.services.parser import extract_paid_amount, extract_tax_rates, parse_receipt
from app.services.preprocessor import (
    MAX_IMAGE_BYTES,
    ImageDecodeError,
    ImageProcessingError,
    preprocess_receipt,
)
from app.services.reconciler import METHOD_DERIVED, METHOD_MANUAL, METHOD_PRINTED, reconcile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./data/uploads"))
LOW_OCR_CONFIDENCE = 0.6  # mean line confidence below this adds a warning
JPEG_QUALITY = 90
TOP_MERCHANTS_PER_CURRENCY = 10
EXPORT_MAX_ROWS = 50_000
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _save_image(image: np.ndarray) -> Path:
    """Encode ``image`` as JPEG and write it under :data:`UPLOAD_DIR`.

    Returns:
        Path of the stored file (random ``<uuid>.jpg`` name).

    Raises:
        OSError: If encoding or writing fails.
    """
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise OSError("Could not encode the receipt image.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{uuid.uuid4().hex}.jpg"
    path.write_bytes(encoded.tobytes())
    return path


def _escape_like(value: str) -> str:
    """Escape ``%``, ``_`` and ``\\`` so user input is matched literally by LIKE."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _merchant_identity(
    db: AsyncSession, name: Optional[str], raw_text: Optional[str]
) -> tuple[Optional[str], Category]:
    """Merchant key (snapped to a known spelling) and category (learned rule first).

    A database problem here must not lose the receipt, so it falls back to the plain key
    and the keyword categoriser.
    """
    key = merchant_key(name)
    try:
        known = (await db.scalars(select(Expense.merchant_key).where(Expense.merchant_key.is_not(None)).distinct())).all()
        key = canonical_key(key, known)
        rule = await db.get(MerchantRule, key) if key else None
    except SQLAlchemyError:
        logger.exception("Could not look up the merchant; using the plain key")
        rule = None
    return key, rule.category if rule is not None else categorize(name, raw_text)


def _filter_conditions(query: ExpenseFilter) -> list[ColumnElement[bool]]:
    """Translate the shared list / export filters into SQL conditions."""
    conditions: list[ColumnElement[bool]] = []
    if query.merchant:
        pattern = f"%{_escape_like(query.merchant.strip())}%"
        conditions.append(Expense.merchant_name.ilike(pattern, escape="\\"))
    if query.merchant_key:
        conditions.append(Expense.merchant_key == query.merchant_key)
    if query.currency is not None:
        conditions.append(Expense.currency == query.currency)
    if query.status is not None:
        conditions.append(Expense.reconciliation_status == query.status)
    if query.category is not None:
        if query.category is Category.OTHER:  # rows from before categories existed count as "other"
            conditions.append(or_(Expense.category == Category.OTHER, Expense.category.is_(None)))
        else:
            conditions.append(Expense.category == query.category)
    if query.date_from is not None:
        conditions.append(Expense.date >= query.date_from)
    if query.date_to is not None:
        conditions.append(Expense.date <= query.date_to)
    return conditions


# ---------------------------------------------------------------------------
# POST /extract-receipt
# ---------------------------------------------------------------------------
@router.post(
    "/extract-receipt",
    response_model=ReceiptProcessResponse,
    status_code=201,
    summary="Read a receipt image and store the expense",
    responses={
        400: {"model": ErrorResponse, "description": "Empty upload."},
        413: {"model": ErrorResponse, "description": "Image larger than the upload limit."},
        422: {"model": ErrorResponse, "description": "Not an image, or no text could be read."},
        500: {"model": ErrorResponse, "description": "Processing or storage failure."},
        503: {"model": ErrorResponse, "description": "OCR engine unavailable."},
    },
)
async def extract_receipt(
    file: Annotated[UploadFile, File(description="Receipt photo (JPEG, PNG, WebP or BMP).")],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReceiptProcessResponse:
    """Run the full pipeline on an uploaded receipt and persist the result.

    Receipts that cannot be verified are still stored, with status
    ``flagged_for_review`` and the reason in ``reconciliation.message``. Images
    without any readable text are rejected and not stored.
    """
    started = time.perf_counter()

    data = await file.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Yüklenen dosya boş.")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Görsel {MAX_IMAGE_BYTES // 1_048_576} MB sınırından büyük; daha küçük bir fotoğraf yükleyin.",
        )

    # 1. Pre-process (perspective correction + enhancement)
    try:
        pre = await asyncio.to_thread(preprocess_receipt, data)
    except ImageDecodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ImageProcessingError as exc:
        logger.exception("Image pre-processing failed")
        raise HTTPException(status_code=500, detail="Görsel işlenemedi; farklı bir fotoğrafla tekrar deneyin.") from exc

    # 2. OCR
    try:
        engine = await asyncio.to_thread(get_ocr_engine)  # first call loads the model
        ocr = await engine.recognize_async(pre.enhanced)
    except OCRInitError as exc:
        logger.error("OCR engine unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Metin tanıma motoru henüz hazır değil; birkaç saniye sonra tekrar deneyin.") from exc
    except OCRInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OCRError as exc:
        logger.exception("OCR failed")
        raise HTTPException(status_code=500, detail="Metin tanıma başarısız oldu; fotoğrafı tekrar çekmeyi deneyin.") from exc
    if not ocr.lines:
        raise HTTPException(
            status_code=422,
            detail="Görselde okunabilir metin bulunamadı. Fişi iyi ışıkta, net ve tamamı görünecek şekilde tekrar çekin.",
        )

    # 3. Parse + 4. reconcile
    parsed = parse_receipt(ocr)
    warped = pre.warped
    rotation_note: Optional[str] = None
    if needs_rotation_check(parsed):  # sideways / upside-down photo? try the other orientations
        try:
            better = await recover_orientation(engine, pre.enhanced, pre.warped, ocr, parsed)
        except OCRError:
            logger.exception("Orientation recovery failed")
        else:
            ocr, parsed, warped = better.ocr, better.parsed, better.warped
            if better.rotation:
                rotation_note = "Fiş yan ya da ters çekilmişti; otomatik döndürülerek okundu."
    printed_or_derived = METHOD_DERIVED if parsed.subtotal_derived else METHOD_PRINTED
    reconciliation = reconcile(
        parsed.subtotal_amount,
        parsed.tax_amount,
        parsed.total_amount,
        subtotal_method=printed_or_derived if parsed.subtotal_amount is not None else None,
        tax_rates=parsed.tax_rates,
        paid_amount=parsed.paid_amount,
    )
    key, category = await _merchant_identity(db, parsed.merchant_name, parsed.raw_text)

    warnings = [*pre.warnings, *parsed.warnings]
    if rotation_note:
        warnings.append(rotation_note)
    currency = parsed.currency
    if currency is None:
        warnings.append("Para birimi anlaşılamadı; TL varsayıldı.")
        currency = Currency.TRY
    if ocr.mean_confidence < LOW_OCR_CONFIDENCE:
        warnings.append(
            f"Fotoğraf net okunamadı (güven {ocr.mean_confidence:.2f}). Değerleri kontrol edin; "
            "fişi düz bir zemine koyup ışığı artırarak tekrar çekmeniz önerilir."
        )

    # 5. Store the image, then the row (remove the image again if the row fails)
    try:
        image_path = await asyncio.to_thread(_save_image, warped)
    except OSError as exc:
        logger.exception("Could not store the receipt image")
        raise HTTPException(status_code=500, detail="Fiş görseli saklanamadı.") from exc

    payload = ReceiptCreate(
        merchant_name=parsed.merchant_name,
        date=parsed.date,
        subtotal_amount=reconciliation.subtotal_amount,  # printed, rebuilt from the rate, or implied
        tax_amount=parsed.tax_amount,
        total_amount=parsed.total_amount,
        currency=currency,
        merchant_key=key,
        category=category,
        reconciliation_status=reconciliation.status,
        reconciliation_method=reconciliation.method,
        raw_text=parsed.raw_text,
        image_path=str(image_path),
    )
    expense = Expense(**payload.model_dump())
    try:
        db.add(expense)
        await db.commit()
        await db.refresh(expense)
    except SQLAlchemyError as exc:
        logger.exception("Could not save the expense")
        await db.rollback()
        await asyncio.to_thread(image_path.unlink, True)
        raise HTTPException(status_code=500, detail="Fiş kaydedilemedi.") from exc

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    logger.info(
        "Receipt %s processed in %.0f ms (ocr=%.0f ms, lines=%d, status=%s)",
        expense.id, elapsed_ms, ocr.elapsed_ms, len(ocr.lines), reconciliation.status.value,
    )
    return ReceiptProcessResponse(
        receipt=ReceiptRead.model_validate(expense),
        reconciliation=reconciliation,
        boxes=[OCRBox(text=line.text, confidence=line.confidence, box=list(line.box)) for line in ocr.lines],
        image_width=ocr.image_width,
        image_height=ocr.image_height,
        ocr_confidence=ocr.mean_confidence,
        processing_time_ms=elapsed_ms,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# GET /expenses
# ---------------------------------------------------------------------------
@router.get(
    "/expenses",
    response_model=ReceiptListResponse,
    summary="List stored expenses",
    responses={500: {"model": ErrorResponse, "description": "Database error."}},
)
async def list_expenses(
    query: Annotated[ReceiptListQuery, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReceiptListResponse:
    """Return stored expenses, newest first, with optional filters and pagination.

    Filters: ``merchant`` (substring), ``currency``, ``status``, ``category``, ``date_from`` /
    ``date_to`` (inclusive). Rows without a date are listed last.
    """
    conditions = _filter_conditions(query)

    count_statement = select(func.count()).select_from(Expense)
    rows_statement = select(Expense)
    for condition in conditions:
        count_statement = count_statement.where(condition)
        rows_statement = rows_statement.where(condition)
    rows_statement = (
        rows_statement.order_by(Expense.date.desc().nulls_last(), Expense.id.desc())
        .offset(query.offset)
        .limit(query.page_size)
    )

    try:
        total = await db.scalar(count_statement) or 0
        rows = (await db.scalars(rows_statement)).all()
    except SQLAlchemyError as exc:
        logger.exception("Could not list expenses")
        raise HTTPException(status_code=500, detail="Fişler okunamadı.") from exc

    return ReceiptListResponse(
        items=[ReceiptSummary.model_validate(row) for row in rows],
        total=total,
        page=query.page,
        page_size=query.page_size,
    )


# ---------------------------------------------------------------------------
# GET /expenses/export.csv
# ---------------------------------------------------------------------------
def _csv_text(value: Optional[str]) -> str:
    """Neutralise spreadsheet formulas (``=``, ``+``, ``-``, ``@``) in user-controlled text."""
    text = value or ""
    return f"'{text}" if text.startswith(_CSV_FORMULA_PREFIXES) else text


def _csv_amount(value: Optional[Decimal], decimal_comma: bool) -> str:
    """Format an amount with a decimal comma (Turkish Excel) or point."""
    if value is None:
        return ""
    text = f"{value:.2f}"
    return text.replace(".", ",") if decimal_comma else text


@router.get(
    "/expenses/export.csv",
    response_class=Response,
    summary="Download the (filtered) expenses as CSV",
    responses={
        200: {"content": {"text/csv": {}}, "description": "UTF-8 CSV with a BOM so Excel shows Turkish letters."},
        500: {"model": ErrorResponse, "description": "Database error."},
    },
)
async def export_expenses(
    query: Annotated[ExportQuery, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Export up to ``EXPORT_MAX_ROWS`` expenses (same filters as the list, newest first).

    ``locale=tr`` (default) writes ``;``-separated values with decimal commas, which
    Turkish Excel opens correctly; ``locale=en`` writes ``,`` and decimal points.
    """
    statement = select(Expense)
    for condition in _filter_conditions(query):
        statement = statement.where(condition)
    statement = statement.order_by(Expense.date.desc().nulls_last(), Expense.id.desc()).limit(EXPORT_MAX_ROWS)
    try:
        rows = (await db.scalars(statement)).all()
    except SQLAlchemyError as exc:
        logger.exception("Could not export expenses")
        raise HTTPException(status_code=500, detail="Fişler okunamadı.") from exc

    decimal_comma = query.locale == "tr"
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";" if decimal_comma else ",", lineterminator="\r\n")
    writer.writerow(
        ["id", "date", "merchant", "category", "currency", "subtotal", "tax", "total", "status", "method", "created_at"]
    )
    for row in rows:
        writer.writerow(
            [
                row.id,
                row.date.isoformat() if row.date else "",
                _csv_text(row.merchant_name),
                (row.category or Category.OTHER).value,
                row.currency.value,
                _csv_amount(row.subtotal_amount, decimal_comma),
                _csv_amount(row.tax_amount, decimal_comma),
                _csv_amount(row.total_amount, decimal_comma),
                row.reconciliation_status.value,
                row.reconciliation_method or "",
                row.created_at.isoformat(timespec="seconds"),
            ]
        )
    return Response(
        content="﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="expenses.csv"'},
    )


# ---------------------------------------------------------------------------
# GET /stats
# ---------------------------------------------------------------------------
@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="Aggregated spending for the dashboard",
    responses={500: {"model": ErrorResponse, "description": "Database error."}},
)
async def get_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> StatsResponse:
    """Totals per currency, month, category and merchant (all done in SQL).

    Amounts of different currencies are never added together. Receipts without a
    total add to the counts but not to the sums; receipts without a date are left
    out of the monthly series. Rows from before categories existed count as ``other``.
    """
    total_sum = func.coalesce(func.sum(Expense.total_amount), 0)
    category_expr = func.coalesce(Expense.category, Category.OTHER.value)
    month_expr = func.strftime("%Y-%m", Expense.date)  # SQLite; use to_char() on PostgreSQL
    try:
        status_rows = (
            await db.execute(select(Expense.reconciliation_status, func.count()).group_by(Expense.reconciliation_status))
        ).all()
        currency_rows = (
            await db.execute(
                select(Expense.currency, total_sum, func.coalesce(func.sum(Expense.tax_amount), 0), func.count())
                .group_by(Expense.currency)
                .order_by(Expense.currency)
            )
        ).all()
        month_rows = (
            await db.execute(
                select(month_expr, Expense.currency, total_sum, func.count())
                .where(Expense.date.is_not(None))
                .group_by(month_expr, Expense.currency)
                .order_by(month_expr, Expense.currency)
            )
        ).all()
        category_rows = (
            await db.execute(
                select(category_expr, Expense.currency, total_sum, func.count())
                .group_by(category_expr, Expense.currency)
                .order_by(total_sum.desc())
            )
        ).all()
        merchant_rows = (
            await db.execute(
                select(Expense.merchant_key, func.min(Expense.merchant_name), Expense.currency, total_sum, func.count())
                .group_by(Expense.merchant_key, Expense.currency)
                .order_by(total_sum.desc())
            )
        ).all()
    except SQLAlchemyError as exc:
        logger.exception("Could not compute statistics")
        raise HTTPException(status_code=500, detail="İstatistikler okunamadı.") from exc

    by_status = {status: count for status, count in status_rows}
    verified = by_status.get(ReconciliationStatus.VERIFIED, 0)
    flagged = sum(count for status, count in by_status.items() if status is not ReconciliationStatus.VERIFIED)

    top_merchants: list[MerchantTotal] = []
    taken: defaultdict[Currency, int] = defaultdict(int)
    for key, name, currency, amount, count in merchant_rows:
        if taken[currency] < TOP_MERCHANTS_PER_CURRENCY:
            taken[currency] += 1
            top_merchants.append(
                MerchantTotal(
                    merchant_key=key,
                    merchant_name=name or "Bilinmeyen",
                    currency=currency,
                    total_amount=amount,
                    receipt_count=count,
                )
            )

    return StatsResponse(
        total_receipts=verified + flagged,
        verified_count=verified,
        flagged_count=flagged,
        totals_by_currency=[
            CurrencyTotal(currency=currency, total_amount=amount, tax_amount=tax, receipt_count=count)
            for currency, amount, tax, count in currency_rows
        ],
        monthly_totals=[
            MonthlyTotal(month=month, currency=currency, total_amount=amount, receipt_count=count)
            for month, currency, amount, count in month_rows
        ],
        category_totals=[
            CategoryTotal(category=category, currency=currency, total_amount=amount, receipt_count=count)
            for category, currency, amount, count in category_rows
        ],
        top_merchants=top_merchants,
    )


# ---------------------------------------------------------------------------
# GET /expenses/{id}/image  +  PATCH /expenses/{id}  +  DELETE /expenses/{id}
# ---------------------------------------------------------------------------
@router.get(
    "/expenses/{expense_id}/image",
    response_class=FileResponse,
    summary="Stored (perspective-corrected) receipt image",
    responses={404: {"model": ErrorResponse, "description": "Unknown expense or missing image."}},
)
async def get_expense_image(
    expense_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FileResponse:
    """Serve the image stored for an expense (only files inside ``UPLOAD_DIR``)."""
    try:
        expense = await db.get(Expense, expense_id)
    except SQLAlchemyError as exc:
        logger.exception("Could not read expense %s", expense_id)
        raise HTTPException(status_code=500, detail="Fiş okunamadı.") from exc
    if expense is None or not expense.image_path:
        raise HTTPException(status_code=404, detail="Görsel bulunamadı.")

    path = Path(expense.image_path).resolve()
    if UPLOAD_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Görsel bulunamadı.")
    return FileResponse(path, media_type="image/jpeg")


def _reconcile_stored(expense: Expense, method: Optional[str]) -> ReconciliationDetail:
    """Run the reconciliation on a stored expense (tax rates and payment come from its raw text)."""
    raw_rows = (expense.raw_text or "").splitlines()
    rates = extract_tax_rates(raw_rows, dollar_is_percent=expense.currency is Currency.TRY)
    return reconcile(
        expense.subtotal_amount,
        expense.tax_amount,
        expense.total_amount,
        subtotal_method=method,
        tax_rates=rates,
        paid_amount=extract_paid_amount(raw_rows),
    )


@router.get(
    "/expenses/{expense_id}",
    response_model=ReceiptDetailResponse,
    summary="One stored expense with the reason for its reconciliation status",
    responses={
        404: {"model": ErrorResponse, "description": "Unknown expense."},
        500: {"model": ErrorResponse, "description": "Database error."},
    },
)
async def get_expense(
    expense_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReceiptDetailResponse:
    """Return the receipt and the reconciliation explanation, recomputed without saving."""
    try:
        expense = await db.get(Expense, expense_id)
    except SQLAlchemyError as exc:
        logger.exception("Could not read expense %s", expense_id)
        raise HTTPException(status_code=500, detail="Fiş okunamadı.") from exc
    if expense is None:
        raise HTTPException(status_code=404, detail="Fiş bulunamadı.")
    return ReceiptDetailResponse(
        receipt=ReceiptRead.model_validate(expense),
        reconciliation=_reconcile_stored(expense, expense.reconciliation_method),
    )


async def _learn_category(db: AsyncSession, expense: Expense) -> None:
    """Remember the user's category for this merchant and apply it to its undecided receipts.

    Receipts of the same merchant that still have no category (or ``other``) follow the
    rule; categories the user chose explicitly for other receipts are left alone.
    """
    assert expense.category is not None and expense.merchant_key is not None
    rule = await db.get(MerchantRule, expense.merchant_key)
    if rule is None:
        db.add(MerchantRule(merchant_key=expense.merchant_key, category=expense.category))
    else:
        rule.category = expense.category
    await db.execute(
        update(Expense)
        .where(
            Expense.merchant_key == expense.merchant_key,
            Expense.id != expense.id,
            or_(Expense.category.is_(None), Expense.category == Category.OTHER),
        )
        .values(category=expense.category)
    )


@router.patch(
    "/expenses/{expense_id}",
    response_model=ReceiptRead,
    summary="Correct extracted values and re-run the reconciliation",
    responses={
        404: {"model": ErrorResponse, "description": "Unknown expense."},
        500: {"model": ErrorResponse, "description": "Database error."},
    },
)
async def update_expense(
    expense_id: int,
    changes: ReceiptUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReceiptRead:
    """Apply the supplied fields, then recompute ``reconciliation_status``."""
    try:
        expense = await db.get(Expense, expense_id)
        if expense is None:
            raise HTTPException(status_code=404, detail="Fiş bulunamadı.")
        fields = changes.model_dump(exclude_unset=True)
        for field, value in fields.items():
            setattr(expense, field, value)

        method = expense.reconciliation_method
        if "subtotal_amount" in fields:  # the user's own value is kept as "manual"; clearing it re-derives
            method = METHOD_MANUAL if expense.subtotal_amount is not None else None
        detail = _reconcile_stored(expense, method)
        expense.reconciliation_status = detail.status
        expense.reconciliation_method = detail.method
        expense.subtotal_amount = detail.subtotal_amount

        if "merchant_name" in fields:
            known = (
                await db.scalars(select(Expense.merchant_key).where(Expense.merchant_key.is_not(None)).distinct())
            ).all()
            expense.merchant_key = canonical_key(merchant_key(expense.merchant_name), known)
        if fields.get("category") is not None and expense.merchant_key:
            await _learn_category(db, expense)
        await db.commit()
        await db.refresh(expense)
    except SQLAlchemyError as exc:
        logger.exception("Could not update expense %s", expense_id)
        await db.rollback()
        raise HTTPException(status_code=500, detail="Fiş güncellenemedi.") from exc
    return ReceiptRead.model_validate(expense)


@router.delete(
    "/expenses/{expense_id}",
    status_code=204,
    summary="Delete an expense and its stored image",
    responses={
        404: {"model": ErrorResponse, "description": "Unknown expense."},
        500: {"model": ErrorResponse, "description": "Database error."},
    },
)
async def delete_expense(
    expense_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Remove the row first, then the image file (only files inside ``UPLOAD_DIR``)."""
    try:
        expense = await db.get(Expense, expense_id)
        if expense is None:
            raise HTTPException(status_code=404, detail="Fiş bulunamadı.")
        image_path = Path(expense.image_path).resolve() if expense.image_path else None
        await db.delete(expense)
        await db.commit()
    except SQLAlchemyError as exc:
        logger.exception("Could not delete expense %s", expense_id)
        await db.rollback()
        raise HTTPException(status_code=500, detail="Fiş silinemedi.") from exc
    if image_path is not None and UPLOAD_DIR.resolve() in image_path.parents:
        await asyncio.to_thread(image_path.unlink, True)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# GET /merchants  +  POST /merchants/merge
# ---------------------------------------------------------------------------
@router.get(
    "/merchants",
    response_model=list[MerchantInfo],
    summary="Merchants (spelling variants merged) with their learned category",
    responses={500: {"model": ErrorResponse, "description": "Database error."}},
)
async def list_merchants(db: Annotated[AsyncSession, Depends(get_db)]) -> list[MerchantInfo]:
    """One row per merchant key, most receipts first."""
    count = func.count()
    try:
        rows = (
            await db.execute(
                select(Expense.merchant_key, func.min(Expense.merchant_name), count)
                .where(Expense.merchant_key.is_not(None))
                .group_by(Expense.merchant_key)
                .order_by(count.desc(), Expense.merchant_key)
            )
        ).all()
        rules = {rule.merchant_key: rule.category for rule in (await db.scalars(select(MerchantRule))).all()}
    except SQLAlchemyError as exc:
        logger.exception("Could not list merchants")
        raise HTTPException(status_code=500, detail="İşletmeler okunamadı.") from exc
    return [
        MerchantInfo(merchant_key=key, merchant_name=name or key, receipt_count=total, category=rules.get(key))
        for key, name, total in rows
    ]


@router.post(
    "/merchants/merge",
    response_model=MerchantMergeResponse,
    summary="Treat one merchant key as another spelling of a second one",
    responses={
        404: {"model": ErrorResponse, "description": "Unknown merchant key."},
        422: {"model": ErrorResponse, "description": "Both keys are the same."},
        500: {"model": ErrorResponse, "description": "Database error."},
    },
)
async def merge_merchants(
    body: MerchantMergeRequest, db: Annotated[AsyncSession, Depends(get_db)]
) -> MerchantMergeResponse:
    """Move all receipts of ``from_key`` to ``into_key``; a learned category of ``from_key``
    is kept when ``into_key`` has none. Future receipts snap to ``into_key`` automatically."""
    if body.from_key == body.into_key:
        raise HTTPException(status_code=422, detail="İki farklı işletme seçin.")
    try:
        exists = lambda key: select(func.count()).select_from(Expense).where(Expense.merchant_key == key)  # noqa: E731
        if not await db.scalar(exists(body.from_key)) or not await db.scalar(exists(body.into_key)):
            raise HTTPException(status_code=404, detail="İşletme bulunamadı.")
        result = await db.execute(
            update(Expense).where(Expense.merchant_key == body.from_key).values(merchant_key=body.into_key)
        )
        old_rule = await db.get(MerchantRule, body.from_key)
        if old_rule is not None:
            if await db.get(MerchantRule, body.into_key) is None:
                db.add(MerchantRule(merchant_key=body.into_key, category=old_rule.category))
            await db.delete(old_rule)
        await db.commit()
    except SQLAlchemyError as exc:
        logger.exception("Could not merge merchants")
        await db.rollback()
        raise HTTPException(status_code=500, detail="İşletmeler birleştirilemedi.") from exc
    return MerchantMergeResponse(moved=result.rowcount or 0)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    responses={503: {"model": HealthResponse, "description": "Database unreachable."}},
)
async def health(response: Response) -> HealthResponse:
    """Report liveness and database reachability (HTTP 503 when degraded)."""
    database_ok = await check_db_connection()
    if not database_ok:
        response.status_code = 503
    return HealthResponse(
        status="ok" if database_ok else "degraded",
        database=database_ok,
        version=__version__,
    )
