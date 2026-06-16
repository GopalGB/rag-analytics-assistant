.PHONY: setup sample-data test lint run clean

setup:
	python3 -m venv .venv
	. .venv/bin/activate && pip install --upgrade pip && pip install -r requirements-dev.txt

sample-data:
	. .venv/bin/activate && python scripts/generate_sample_data.py

test:
	. .venv/bin/activate && pytest

lint:
	. .venv/bin/activate && ruff check .

run:
	. .venv/bin/activate && uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

clean:
	rm -rf .venv .pytest_cache .ruff_cache storage/*.duckdb storage/*.wal
