FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1

RUN pip install --upgrade pip && \
    pip install \
      "mlflow==3.16.0" \
      "psycopg2-binary==2.9.9" \
      "boto3==1.35.24"

EXPOSE 5000
