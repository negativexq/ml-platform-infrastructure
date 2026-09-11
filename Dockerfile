# Multi-stage: the builder carries pip and the toolchain; the runtime image
# gets only the installed packages and the app. Shrinks the attack surface and
# keeps build-only CVEs out of what ships.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY scripts ./scripts
RUN pip install --upgrade pip && pip install --prefix=/install .

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/install/bin:$PATH \
    PYTHONPATH=/install/lib/python3.12/site-packages

WORKDIR /app

# Drop the two setuid binaries a slim image ships but this workload never uses.
RUN rm -f /usr/bin/chsh /usr/bin/chfn /usr/bin/newgrp /usr/bin/gpasswd \
    /usr/bin/passwd /usr/bin/su /usr/bin/mount /usr/bin/umount || true

COPY --from=builder /install /install
COPY app ./app

# No model is baked into the image: from M1 the artifact is pulled at startup
# from MLflow (ML_MODEL_URI). An image with no reachable artifact stays alive
# but reports /ready 503 — that is the contract M2 relies on.

EXPOSE 8000

RUN useradd --uid 1000 --create-home appuser && chown -R appuser /app
USER appuser

HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
