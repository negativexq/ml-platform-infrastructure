# Build from the repository root; intentionally copies only migration source.
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
COPY constraints/controlplane-migrate.txt /tmp/requirements.txt
RUN python -m pip install --prefix=/install --no-deps -r /tmp/requirements.txt \
    && PYTHONPATH=/install/lib/python3.12/site-packages python -m pip check

FROM ${PYTHON_IMAGE}
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/install/bin:$PATH PYTHONPATH=/app:/install/lib/python3.12/site-packages
WORKDIR /app
RUN useradd --uid 10001 --create-home platform
COPY --from=builder /install /install
COPY controlplane/__init__.py controlplane/settings.py ./controlplane/
COPY controlplane/persistence/__init__.py controlplane/persistence/engine.py controlplane/persistence/pool.py controlplane/persistence/models.py controlplane/persistence/migrate.py ./controlplane/persistence/
COPY controlplane/persistence/migrations ./controlplane/persistence/migrations
USER 10001:10001
CMD ["python", "-m", "controlplane.persistence.migrate", "upgrade"]
