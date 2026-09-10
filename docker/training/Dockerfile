FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Full mlflow (not skinny) for the sklearn flavor and the model registry.
RUN pip install --upgrade pip && pip install \
      "mlflow==2.19.0" \
      "scikit-learn>=1.5" \
      "pandas>=2.2" \
      "numpy>=2.1" \
      "boto3>=1.35"

COPY scripts/train.py ./scripts/train.py

RUN useradd --uid 1000 --create-home trainer && chown -R trainer /app
USER trainer

ENTRYPOINT ["python", "scripts/train.py"]
