ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
FROM ${PYTHON_IMAGE}
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY constraints/storage-initializer.txt /tmp/requirements.txt
RUN python -m pip install --only-binary=:all: --no-deps -r /tmp/requirements.txt \
    && python -m pip check \
    && useradd --uid 1000 --create-home kserve
COPY docker/storage-initializer/initialize.py /app/initialize.py
WORKDIR /app
USER 1000:1000
ENTRYPOINT ["python", "/app/initialize.py"]
