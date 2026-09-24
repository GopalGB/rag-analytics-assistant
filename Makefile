.PHONY: setup run demo test lint evaluate sample-data clean reset

setup:            ## create venv + install everything (runtime + dev/test)
	python3 -m venv .venv
	. .venv/bin/activate && pip install --upgrade pip && pip install -r requirements-dev.txt

run:              ## start the assistant on http://127.0.0.1:8000 (local only)
	. .venv/bin/activate && uvicorn app.main:app --host 127.0.0.1 --port 8000

demo: reset run   ## fresh demo: clear local state (reviews, approvals, log, uploads) and start

test:
	. .venv/bin/activate && pytest

lint:
	. .venv/bin/activate && ruff check .

evaluate:         ## run acceptance checks and write docs/TEST-RESULTS.md
	. .venv/bin/activate && python scripts/evaluate.py

sample-data:      ## regenerate the synthetic demo data set
	. .venv/bin/activate && python scripts/generate_sample_data.py

reset:            ## delete local state + uploaded files (keeps the bundled sample data)
	rm -rf storage data/sample/uploads

clean: reset
	rm -rf .venv .pytest_cache .ruff_cache
