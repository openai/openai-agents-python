.PHONY: sync
sync:
	uv sync --all-extras --all-packages --group dev

.PHONY: format
format: 
	uv run ruff format
	uv run ruff check --fix

.PHONY: format-check
format-check:
	uv run ruff format --check

.PHONY: lint
lint: 
	uv run ruff check

.PHONY: mypy
mypy: 
	uv run mypy . --exclude site

.PHONY: tests
tests: 
	uv run pytest 

.PHONY: coverage
coverage:
	
	uv run coverage run -m pytest
	uv run coverage xml -o coverage.xml
	uv run coverage report -m --fail-under=95

.PHONY: snapshots-fix
snapshots-fix: 
	uv run pytest --inline-snapshot=fix 

.PHONY: snapshots-create 
snapshots-create: 
	uv run pytest --inline-snapshot=create 

.PHONY: old_version_tests
old_version_tests:
	UV_PROJECT_ENVIRONMENT=.venv_39 uv sync --python 3.9 --all-extras --all-packages --group dev
	UV_PROJECT_ENVIRONMENT=.venv_39 uv run --python 3.9 -m pytest

.PHONY: build-docs
build-docs:
	uv run docs/scripts/generate_ref_files.py
	uv run mkdocs build

.PHONY: build-full-docs
build-full-docs:
	uv run docs/scripts/translate_docs.py
	uv run mkdocs build

.PHONY: serve-docs
serve-docs:
	uv run mkdocs serve

.PHONY: deploy-docs
deploy-docs:
	uv run mkdocs gh-deploy --force --verbose

.PHONY: check
check: format-check lint mypy tests
