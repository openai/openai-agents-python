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

.PHONY: pyright
pyright:
	uv run pyright --project pyrightconfig.json

.PHONY: typecheck
typecheck:
	@set -eu; \
	mypy_pid=''; \
	pyright_pid=''; \
	trap 'test -n "$$mypy_pid" && kill $$mypy_pid 2>/dev/null || true; test -n "$$pyright_pid" && kill $$pyright_pid 2>/dev/null || true' EXIT INT TERM; \
	echo "Running make mypy and make pyright in parallel..."; \
	$(MAKE) mypy & mypy_pid=$$!; \
	$(MAKE) pyright & pyright_pid=$$!; \
	wait $$mypy_pid; \
	wait $$pyright_pid; \
	trap - EXIT

.PHONY: tests
tests: tests-parallel tests-serial

.PHONY: tests-asyncio-stability
tests-asyncio-stability:
	bash .github/scripts/run-asyncio-teardown-stability.sh

.PHONY: tests-parallel
tests-parallel:
	uv run pytest -n auto --dist loadfile -m "not serial"

.PHONY: tests-serial
tests-serial:
	uv run pytest -m serial

.PHONY: coverage
coverage:
	
	uv run coverage run -m pytest
	uv run coverage xml -o coverage.xml
	uv run coverage report -m --fail-under=85

.PHONY: snapshots-fix
snapshots-fix: 
	uv run pytest --inline-snapshot=fix 

.PHONY: snapshots-create 
snapshots-create: 
	uv run pytest --inline-snapshot=create 

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
check: format-check lint typecheck tests
