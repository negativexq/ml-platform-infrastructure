ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
FROM ${PYTHON_IMAGE}
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Full mlflow (not skinny) for the sklearn flavor and the model registry.
COPY constraints/training.txt /tmp/requirements.txt
RUN python -m pip install --no-deps -r /tmp/requirements.txt

COPY scripts/train.py ./scripts/train.py

RUN useradd --uid 1000 --create-home trainer && chown -R trainer /app
USER 1000:1000

ENTRYPOINT ["python", "scripts/train.py"]
