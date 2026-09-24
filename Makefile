.PHONY: setup run demo test lint evaluate sample-data backup verify-backup restore docker docker-up reset clean

setup:            ## create venv + install everything (runtime + dev/test)
	python3 -m venv .venv
	. .venv/bin/activate && pip install --upgrade pip && pip install -r requirements-dev.txt

run:              ## start the assistant on http://127.0.0.1:8000 (local only)
	. .venv/bin/activate && uvicorn app.main:app --host 127.0.0.1 --port 8000

demo: reset       ## fresh demo: clear local state, fix the "as of" date to match the sample data, start
	. .venv/bin/activate && REPORT_AS_OF=2026-07-15 LOG_FORMAT=text uvicorn app.main:app --host 127.0.0.1 --port 8000

test:
	. .venv/bin/activate && pytest

lint:
	. .venv/bin/activate && ruff check .

evaluate:         ## run acceptance checks and write docs/TEST-RESULTS.md
	. .venv/bin/activate && python scripts/evaluate.py

sample-data:      ## regenerate the synthetic demo data set
	. .venv/bin/activate && python scripts/generate_sample_data.py

backup:           ## checksummed backup of documents + state into backups/ (add ARGS=--include-env to keep .env)
	. .venv/bin/activate && python scripts/backup.py create $(ARGS)

verify-backup:    ## make verify-backup FILE=backups/assistant-backup-....tar.gz
	. .venv/bin/activate && python scripts/backup.py verify $(FILE)

restore:          ## make restore FILE=backups/assistant-backup-....tar.gz (stop the app first)
	. .venv/bin/activate && python scripts/backup.py restore $(FILE) $(ARGS)

docker:           ## build the container image
	docker build -t private-ai-assistant:latest .

docker-up:        ## run with docker compose (bound to 127.0.0.1:8000)
	docker compose up -d --build

reset:            ## delete local state + uploaded files (keeps the bundled sample data)
	rm -rf storage data/sample/uploads

clean: reset
	rm -rf .venv .pytest_cache .ruff_cache
