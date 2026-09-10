FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY scripts ./scripts
RUN pip install --upgrade pip && pip install .

# No model is baked into the image: from M1 the artifact is pulled at startup
# from MLflow (ML_MODEL_URI). An image with no reachable artifact stays alive
# but reports /ready 503 — that is the contract M2 relies on.

EXPOSE 8000

RUN useradd --uid 1000 --create-home appuser && chown -R appuser /app
USER appuser

HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
