ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
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
