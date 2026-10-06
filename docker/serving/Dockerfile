# Build from the repository root; Python's base provides ARM64 and AMD64 variants.
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
FROM ${PYTHON_IMAGE}
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    MLSERVER_MODELS_DIR=/mnt/models MLSERVER_HTTP_PORT=8080 MLSERVER_GRPC_PORT=9000 \
    MLSERVER_PARALLEL_WORKERS=0 GIT_PYTHON_REFRESH=quiet
COPY constraints/serving.txt /tmp/requirements.txt
COPY docker/serving/patch_mlserver.py /tmp/patch_mlserver.py
RUN python /tmp/patch_mlserver.py --out /tmp/mlp-wheels \
    && python -m pip install --find-links=/tmp/mlp-wheels --only-binary=:all: --no-deps -r /tmp/requirements.txt \
    && python -m pip check \
    && useradd --uid 1000 --create-home server
WORKDIR /mnt/models
USER 1000:1000
EXPOSE 8080 9000
CMD ["mlserver", "start", "/mnt/models"]
