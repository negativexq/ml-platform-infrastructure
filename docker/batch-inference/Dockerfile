ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
FROM ${PYTHON_IMAGE} AS dependencies
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
COPY constraints/batch.txt ./constraints/batch.txt
RUN python -m pip install --prefix=/install --no-deps -r constraints/batch.txt
FROM dependencies AS builder
COPY pyproject.toml ./
COPY batch_inference ./batch_inference
COPY controlplane/domain/model_artifacts.py ./controlplane/domain/model_artifacts.py
RUN python -m pip install --prefix=/application --no-deps '.[inference,batch]' \
    && PYTHONPATH=/application/lib/python3.12/site-packages:/install/lib/python3.12/site-packages python -m pip check
FROM ${PYTHON_IMAGE}
ENV PATH=/application/bin:/install/bin:$PATH \
    PYTHONPATH=/application/lib/python3.12/site-packages:/install/lib/python3.12/site-packages \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GIT_PYTHON_REFRESH=quiet
RUN useradd --uid 10001 --create-home platform
COPY --from=dependencies /install /install
COPY --from=builder /application /application
USER 10001:10001
WORKDIR /tmp
CMD ["python", "-m", "batch_inference.worker"]
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
