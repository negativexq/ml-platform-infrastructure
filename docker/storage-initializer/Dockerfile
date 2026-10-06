ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
FROM ${PYTHON_IMAGE} AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY constraints/storage-initializer.txt /tmp/requirements.txt
# KServe 0.15 pins psutil 5.x, which needs a source build on ARM64.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && python -m pip install --prefix=/install --no-deps -r /tmp/requirements.txt
ENV PYTHONPATH=/install/lib/python3.12/site-packages
RUN python -m pip check

FROM ${PYTHON_IMAGE}
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/install/bin:$PATH PYTHONPATH=/install/lib/python3.12/site-packages
COPY --from=builder /install /install
RUN useradd --uid 1000 --create-home kserve
COPY docker/storage-initializer/initialize.py /app/initialize.py
WORKDIR /app
USER 1000:1000
ENTRYPOINT ["python", "/app/initialize.py"]
