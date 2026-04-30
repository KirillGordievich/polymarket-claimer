.PHONY: dry run test lint help install

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Create .venv and install all dependencies (run once after clone)
	uv sync --group dev
	@echo "Virtual env ready. To activate: source .venv/bin/activate"

dry: ## Run claimer in dry-run mode (no real transactions)
	uv run python -m src.apps.claimer --dry-run

run: ## Run claimer
	uv run python -m src.apps.claimer

test: ## Run tests
	uv run pytest tests/ -v

lint: ## Lint source code with ruff
	uv run ruff check src/ tests/
