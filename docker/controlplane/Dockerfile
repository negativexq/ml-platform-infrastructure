# Build from the repository root: docker build -f docker/controlplane/Dockerfile .
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
COPY pyproject.toml ./
COPY constraints/controlplane.txt ./constraints/controlplane.txt
COPY controlplane ./controlplane
RUN python -m pip install --prefix=/install -c constraints/controlplane.txt '.[controlplane]'

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/install/bin:$PATH \
    PYTHONPATH=/install/lib/python3.12/site-packages
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
WORKDIR /app
RUN useradd --uid 10001 --create-home platform
COPY --from=builder /install /install
USER 10001:10001
EXPOSE 8080 8081
# API is the default. The chart overrides the command for gateway, reconciler and migrations.
CMD ["uvicorn", "controlplane.main:app_factory", "--factory", "--host=0.0.0.0", "--port=8080"]
