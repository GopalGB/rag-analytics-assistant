.PHONY: setup run demo test test-js lint secrets-scan preflight evaluate evaluate-live sample-data backup verify-backup restore docker docker-up docker-up-ollama reset clean

setup:            ## create venv + install everything (runtime + dev/test)
	python3 -m venv .venv
	. .venv/bin/activate && pip install --upgrade pip && pip install -r requirements-dev.txt

run:              ## start the assistant on http://127.0.0.1:8000 (local only)
	. .venv/bin/activate && uvicorn app.main:app --host 127.0.0.1 --port 8000

demo: reset       ## fresh demo: clear local state, fix the "as of" date to match the sample data, start
	. .venv/bin/activate && REPORT_AS_OF=2026-07-15 LOG_FORMAT=text uvicorn app.main:app --host 127.0.0.1 --port 8000

test:
	. .venv/bin/activate && pytest
	@if command -v node >/dev/null; then node --test tests/*.mjs; else echo "(node not installed: skipped the proxy tests)"; fi

test-js:          ## Cloudflare Worker proxy tests (needs Node 18+)
	node --test tests/*.mjs

lint:
	. .venv/bin/activate && ruff check .

preflight:        ## check the configuration before deploying: make preflight TARGET=local|docker|public-demo
	. .venv/bin/activate && python scripts/preflight.py --target $${TARGET:-local}

secrets-scan:     ## scan the repo and its history for committed credentials (brew install gitleaks)
	gitleaks git --no-banner --redact .

evaluate:         ## run acceptance checks and write docs/TEST-RESULTS.md
	. .venv/bin/activate && python scripts/evaluate.py

evaluate-live:    ## end-to-end check of the RUNNING app over HTTP (make evaluate-live URL=http://127.0.0.1:8000)
	. .venv/bin/activate && python scripts/evaluate_live.py --base-url $${URL:-http://127.0.0.1:8000} --runs 20

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

docker-up-ollama: ## same, plus Ollama in a container (Linux servers without a native Ollama)
	docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d --build

reset:            ## delete local state + uploaded files (keeps the bundled sample data)
	rm -rf storage data/sample/uploads

clean: reset
	rm -rf .venv .pytest_cache .ruff_cache
