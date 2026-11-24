# CardVault: run the stack locally and in CI.
SHELL := /bin/bash
COMPOSE := docker compose
CI_COMPOSE := docker compose -f docker-compose.yml -f docker-compose.ci.yml

.DEFAULT_GOAL := help
.PHONY: help up down clean seed reset smoke logs ci-up bug-up lint format events

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

.env:
	cp .env.example .env

up: .env ## Build and start the stack, wait until healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack (keeps data volumes)
	$(COMPOSE) down --remove-orphans

clean: ## Stop the stack and delete its volumes
	$(COMPOSE) down -v --remove-orphans

seed: ## Load the deterministic seed data
	./scripts/seed.sh

reset: ## Reset every service, clear the clock offset, reseed
	./scripts/reset.sh

smoke: ## Run the smoke test against the running stack
	./scripts/smoke.sh

logs: ## Follow logs
	$(COMPOSE) logs -f --tail=200

ci-up: .env ## Start with the CI override, wait for readiness, seed
	$(CI_COMPOSE) up -d --build --wait
	./scripts/seed.sh

bug-up: .env ## Start with one seeded bug on: make bug-up BUG=BUG_PAN_IN_LOGS
	@./scripts/check-bug.sh "$(BUG)"
	$(BUG)=true $(COMPOSE) up -d --build --wait
	./scripts/seed.sh

lint: ## ruff check + format check on app/
	cd app && uv run ruff check . && uv run ruff format --check .

format: ## ruff fix + format on app/
	cd app && uv run ruff check --fix . && uv run ruff format .

events: ## Regenerate docs/events JSON Schemas
	cd app && uv run python -m shared.events.export ../docs/events
