# Build from the repository root: docker build -f docker/controlplane/Dockerfile .
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
COPY constraints/controlplane.txt ./constraints/controlplane.txt
RUN python -m pip install --prefix=/install --no-deps -r constraints/controlplane.txt
COPY pyproject.toml ./
COPY controlplane ./controlplane
RUN python -m pip install --prefix=/install --no-deps '.[controlplane]' \
    && PYTHONPATH=/install/lib/python3.12/site-packages python -m pip check

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
