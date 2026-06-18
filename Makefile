PORT   ?= 8080
HOST   ?= 0.0.0.0
CONFIG ?= ./data/config.toml
DB     ?= ./data/audit.sqlite3
KEY    ?= CHANGE_ME_1
URL    ?=

.DEFAULT_GOAL := help

.PHONY: help sync run run-json health metrics audit logs devices check best curl clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync: ## Install/lock dependencies with uv
	uv sync

run: ## Run the API (HOST/PORT/CONFIG overridable)
	uv run python -m app.main --config $(CONFIG) --host $(HOST) --port $(PORT)

run-json: ## Run the API with structured JSON logs
	uv run python -m app.main --config $(CONFIG) --host $(HOST) --port $(PORT) --json-logs

health: ## Curl the liveness endpoint
	curl -s localhost:$(PORT)/healthz; echo

metrics: ## Show Prometheus counters
	curl -s localhost:$(PORT)/metrics | grep -E '^media_gateway_' | grep -v '^#'

audit: ## Dump the audit log
	sqlite3 -header -column $(DB) "select id, ts, api_key_name, site, endpoint, item_count, status, device_name, device_os from downloads;"

logs: ## Show audit log via CLI (DAYS=10 API_KEY=name optional)
	uv run python -m app.main --config $(CONFIG) logs --days $(or $(DAYS),10) $(if $(API_KEY),--api-key $(API_KEY),)

devices: ## List registered devices (API_KEY=name optional to filter)
	uv run python -m app.main --config $(CONFIG) devices list $(if $(API_KEY),--api-key $(API_KEY),)

check: ## POST VERSION=... to /v1/check (default 1.0.0)
	curl -s -X POST localhost:$(PORT)/v1/check \
		-H "X-API-Key: $(KEY)" -H 'Content-Type: application/json' \
		-d '{"version":"$(or $(VERSION),1.0.0)"}'; echo

best: ## POST URL=... to /v1/best (JSON). Example: make best URL='https://...'
	@test -n "$(URL)" || { echo "set URL=..."; exit 1; }
	curl -s -X POST localhost:$(PORT)/v1/best \
		-H "X-API-Key: $(KEY)" -H 'Content-Type: application/json' \
		-d '{"url":"$(URL)"}'; echo

curl: ## POST URL=... to /v1/best?curl=true (printable curl commands)
	@test -n "$(URL)" || { echo "set URL=..."; exit 1; }
	curl -s -X POST 'localhost:$(PORT)/v1/best?curl=true' \
		-H "X-API-Key: $(KEY)" -H 'Content-Type: application/json' \
		-d '{"url":"$(URL)"}'

clean: ## Remove the SQLite audit DB and Python caches
	rm -f $(DB) $(DB)-wal $(DB)-shm
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
