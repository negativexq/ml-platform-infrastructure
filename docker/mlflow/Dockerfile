ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
FROM ${PYTHON_IMAGE}
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY constraints/mlflow.txt /tmp/requirements.txt
RUN python -m pip install --no-deps -r /tmp/requirements.txt

RUN useradd --uid 1000 --create-home mlflow
USER mlflow

EXPOSE 5000
