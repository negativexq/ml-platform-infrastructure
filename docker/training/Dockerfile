ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
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
