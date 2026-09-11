FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install --upgrade pip && \
    pip install \
      "mlflow==3.15.0" \
      "psycopg2-binary>=2.9.10" \
      "boto3>=1.35" && \
    pip install --upgrade \
      "cryptography>=50.0.0" \
      "setuptools>=78.1.1" \
      "wheel>=0.46.2" \
      "msgpack>=1.2.1" \
      "jaraco.context>=6.1.0"

RUN useradd --uid 1000 --create-home mlflow
USER mlflow

EXPOSE 5000
