FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Full mlflow (not skinny) for the sklearn flavor and the model registry.
RUN pip install --upgrade pip && pip install \
      "mlflow==3.15.0" \
      "scikit-learn>=1.5" \
      "pandas>=2.2" \
      "numpy>=2.1" \
      "boto3>=1.35" && \
    pip install --upgrade \
      "cryptography>=50.0.0" \
      "setuptools>=78.1.1" \
      "msgpack>=1.2.1"

COPY scripts/train.py ./scripts/train.py

RUN useradd --uid 1000 --create-home trainer && chown -R trainer /app
USER trainer

ENTRYPOINT ["python", "scripts/train.py"]
