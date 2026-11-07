# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 - build the virtualenv (compiler only lives here, not in the final image)
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt


# ---------------------------------------------------------------------------
# Stage 2 - runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# Shared libraries needed at run time:
#   libgl1, libglib2.0-0  - OpenCV (paddleocr also installs the non-headless wheel)
#   libgomp1              - OpenMP, used by PaddlePaddle / numpy on CPU
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

# Non-root user; /app/data holds the SQLite file and the stored receipt images.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
WORKDIR /app
USER app

COPY --chown=app:app app ./app

# Bake the PaddleOCR models into the image so the first request does not need
# internet access. Uses the application's own OCR configuration. Skip with
# `--build-arg PRELOAD_MODELS=0` (models are then downloaded on first start).
ARG PRELOAD_MODELS=1
RUN if [ "$PRELOAD_MODELS" = "1" ]; then \
        python -c "from app.services.ocr_engine import get_ocr_engine; get_ocr_engine().warmup()"; \
    fi

EXPOSE 8000
VOLUME ["/app/data"]

# urlopen raises on HTTP 503 (database down) -> non-zero exit -> unhealthy.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)"]

# One worker on purpose: the OCR model is large and SQLite allows a single writer.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
