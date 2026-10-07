ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
FROM ${PYTHON_IMAGE} AS ui
RUN python -m pip download --no-deps --only-binary=:all: --dest=/tmp mlflow==3.15.0
COPY docker/mlflow/extract_ui.py /tmp/extract_ui.py
RUN python /tmp/extract_ui.py

FROM ${PYTHON_IMAGE}
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

ARG MLFLOW_PROFILE=mlflow-server
COPY constraints/${MLFLOW_PROFILE}.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    env -u PIP_NO_CACHE_DIR python -m pip install --no-deps -r /tmp/requirements.txt \
    && python -m pip check
COPY --from=ui /ui /usr/local/lib/python3.12/site-packages/mlflow/server/js/build

RUN useradd --uid 1000 --create-home mlflow
USER mlflow

EXPOSE 5000
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
