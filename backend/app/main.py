"""Application entry point.

Run locally::

    uvicorn app.main:app --reload            # from the backend/ directory
    python -m app.main                       # same, via uvicorn.run

Environment variables: ``CORS_ORIGINS`` (comma separated, default the Streamlit
origins), ``LOG_LEVEL``, ``OCR_WARMUP`` (load the OCR model at start-up, default
on), ``HOST`` and ``PORT`` (only for ``python -m app.main``).
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import router
from app.db.database import dispose_engine, init_db
from app.services.ocr_engine import get_ocr_engine

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_CORS_ORIGINS = "http://localhost:8501,http://127.0.0.1:8501"


def _cors_origins() -> list[str]:
    """Allowed browser origins from ``CORS_ORIGINS`` (comma separated; ``*`` = any)."""
    raw = os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


async def _warm_up_ocr() -> None:
    """Load the OCR model in the background so the first request is fast.

    A failure (e.g. models cannot be downloaded) is logged but does not stop the
    API: ``/extract-receipt`` answers 503 and retries the load on the next call.
    """
    try:
        engine = await asyncio.to_thread(get_ocr_engine)
        await asyncio.to_thread(engine.warmup)
    except Exception:  # background task boundary: log everything, never crash the app
        logger.exception("OCR warm-up failed; the model will be loaded on demand")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create tables and start the OCR warm-up on start-up; release resources on exit."""
    await init_db()
    warmup_task = None
    if os.getenv("OCR_WARMUP", "1").strip().lower() in {"1", "true", "yes", "on"}:
        warmup_task = asyncio.create_task(_warm_up_ocr())
    try:
        yield
    finally:
        if warmup_task is not None and not warmup_task.done():
            warmup_task.cancel()
        await dispose_engine()


app = FastAPI(
    title="Akıllı Harcama & Fiş Mutabakat Sistemi",
    description=(
        "Reads Turkish and English receipts (OpenCV + PaddleOCR), extracts merchant, date, "
        "tax and total, and reconciles subtotal + tax against the total."
    ),
    version=__version__,
    lifespan=lifespan,
)

_origins = _cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials="*" not in _origins,  # browsers reject credentials with a wildcard origin
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(router)
# Mobile PWA (plain HTML/JS, same origin as the API). Mounted last so it never shadows /api.
app.mount("/m", StaticFiles(directory=Path(__file__).parent / "static" / "m", html=True), name="mobile")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )
