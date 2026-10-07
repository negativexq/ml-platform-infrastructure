# Build from the repository root: docker build -f docker/controlplane/Dockerfile .
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
FROM ${PYTHON_IMAGE} AS dependencies
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
COPY constraints/controlplane.txt ./constraints/controlplane.txt
RUN python -m pip install --prefix=/install --no-deps -r constraints/controlplane.txt

FROM dependencies AS builder
COPY pyproject.toml ./
COPY controlplane ./controlplane
COPY docker/controlplane/check_dependencies.py ./check_dependencies.py
RUN python -m pip install --prefix=/application --no-deps '.[controlplane]' \
    && PYTHONPATH=/application/lib/python3.12/site-packages:/install/lib/python3.12/site-packages python -m pip check \
    && PYTHONPATH=/application/lib/python3.12/site-packages:/install/lib/python3.12/site-packages python check_dependencies.py

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/application/bin:/install/bin:$PATH \
    PYTHONPATH=/application/lib/python3.12/site-packages:/install/lib/python3.12/site-packages
WORKDIR /app
RUN useradd --uid 10001 --create-home platform
COPY --from=dependencies /install /install
COPY --from=builder /application /application
COPY --from=builder /build/check_dependencies.py /usr/local/bin/check-controlplane-dependencies.py
USER 10001:10001
EXPOSE 8080 8081
# API is the default. The chart overrides the command for gateway, reconciler and migrations.
CMD ["uvicorn", "controlplane.main:app_factory", "--factory", "--host=0.0.0.0", "--port=8080"]
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
