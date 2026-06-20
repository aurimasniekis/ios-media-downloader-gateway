# Docker Compose helpers for the published `gateway` image.
# Run from this folder: `make <target>` (e.g. `make up`). `make help` lists all.

COMPOSE ?= docker compose
SERVICE ?= gateway
PORT    ?= 8080
APP     := python -m app.main

.DEFAULT_GOAL := help

.PHONY: help config up update pull start stop restart down recreate \
        ps status logs shell exec health metrics devices devices-remove stats audit clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

config: ## Create data/config.toml from the example (if missing)
	@if [ -f data/config.toml ]; then echo "data/config.toml already exists"; \
	else mkdir -p data && cp data/config.example.toml data/config.toml \
		&& echo "created data/config.toml — edit the API keys before starting"; fi

# --- lifecycle ---------------------------------------------------------------

up: ## Start the gateway (detached, pulls the image if needed)
	$(COMPOSE) up -d

update: ## Pull the latest image and recreate the container
	$(COMPOSE) pull
	$(COMPOSE) up -d

pull: ## Pull the latest published image
	$(COMPOSE) pull

recreate: ## Force-recreate the container from the current image
	$(COMPOSE) up -d --force-recreate

start: ## Start a stopped container
	$(COMPOSE) start $(SERVICE)

stop: ## Stop the container (keep it)
	$(COMPOSE) stop $(SERVICE)

restart: ## Restart the gateway
	$(COMPOSE) restart $(SERVICE)

down: ## Stop and remove the container
	$(COMPOSE) down

# --- inspection --------------------------------------------------------------

ps status: ## Show service status
	$(COMPOSE) ps

logs: ## Follow gateway logs (LINES=200 to limit history)
	$(COMPOSE) logs -f $(if $(LINES),--tail $(LINES),) $(SERVICE)

# --- shell / exec ------------------------------------------------------------

shell: ## Open a shell inside the gateway container
	$(COMPOSE) exec $(SERVICE) bash

exec: ## Run CMD=... inside the container. e.g. make exec CMD='ls /app/data'
	@test -n "$(CMD)" || { echo "set CMD=..."; exit 1; }
	$(COMPOSE) exec $(SERVICE) $(CMD)

# --- health / metrics (queried from the host) --------------------------------

health: ## Curl the liveness endpoint (host PORT)
	curl -s localhost:$(PORT)/healthz; echo

metrics: ## Show Prometheus counters (host PORT)
	curl -s localhost:$(PORT)/metrics | grep -E '^media_gateway_' | grep -v '^#'

# --- gateway CLI (runs inside the container) ---------------------------------

devices: ## List registered devices (API_KEY=name optional to filter)
	$(COMPOSE) exec $(SERVICE) $(APP) devices list $(if $(API_KEY),--api-key $(API_KEY),)

devices-remove: ## Remove device(s): API_KEY=name + DEVICE_ID=.. | LABEL=.. | ALL=1
	@test -n "$(API_KEY)" || { echo "set API_KEY=..."; exit 1; }
	$(COMPOSE) exec $(SERVICE) $(APP) devices remove --api-key $(API_KEY) \
		$(if $(DEVICE_ID),--device-id $(DEVICE_ID),) \
		$(if $(LABEL),--label "$(LABEL)",) \
		$(if $(ALL),--all,)

audit: ## Show the audit log via the CLI (DAYS=10 API_KEY=name optional)
	$(COMPOSE) exec $(SERVICE) $(APP) logs --days $(or $(DAYS),10) $(if $(API_KEY),--api-key $(API_KEY),)

stats: ## Show per-API-key usage stats (API_KEY=name optional to filter)
	$(COMPOSE) exec $(SERVICE) $(APP) stats $(if $(API_KEY),--api-key $(API_KEY),)

clean: ## Stop and remove containers, networks, and anonymous volumes
	$(COMPOSE) down --remove-orphans
