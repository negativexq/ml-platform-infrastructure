.PHONY: install train test lint type check docker-build docker-run run clean

IMAGE ?= ml-platform-inference:dev

install:
	python -m pip install --upgrade pip
	pip install -e ".[dev]"

train:
	python scripts/train_baseline.py

test:
	pytest

lint:
	ruff check .

type:
	mypy app

check: lint type test

run: train
	uvicorn app.main:app --reload --port 8000

docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -p 8000:8000 $(IMAGE)

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache artifacts
