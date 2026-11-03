.PHONY: sync native-prepare build
sync:
	uv sync --group dev

native-prepare:
	python3 packages/codex/scripts/prepare_codex.py $(if $(CODEX_SOURCE),--source "$(CODEX_SOURCE)")

build:
	uv build --wheel

.PHONY: update-released-api-contract check-released-api-contract
update-released-api-contract:
	uv run python .github/scripts/update_released_api_contract.py --version "$(VERSION)"

check-released-api-contract:
	uv run python .github/scripts/update_released_api_contract.py --version "$(VERSION)" --check

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
	uv run python .github/scripts/check_optional_truthiness.py src/agents

.PHONY: mypy
mypy: 
	uv run mypy src
	uv run mypy --strict --follow-imports=silent tests/test_native_agent_typing.py tests/test_native_agent_checkpoint_typing.py tests/test_native_mcp_mypy.py tests/test_native_manual_tools_typing.py

.PHONY: pyright
pyright:
	uv run pyright --project pyrightconfig.json --threads "$${PYRIGHT_THREADS:-4}"
	uv run pyright --project tests/pyright-negative.json

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
tests: tests-parallel
	$(MAKE) tests-serial

.PHONY: tests-repo-skills
tests-repo-skills:
	uv run --no-project --python 3.11 python .github/scripts/run_repo_skill_tests.py

.PHONY: tests-review
tests-review: tests-parallel-review
	$(MAKE) tests-serial-review

.PHONY: tests-parallel
SDK_TEST := uv run python packages/codex/scripts/test_native.py --sdk

tests-parallel:
	$(SDK_TEST) -m pytest -n "$${PYTEST_XDIST_AUTO_NUM_WORKERS:-auto}" $(if $(PYTEST_XDIST_AUTO_NUM_WORKERS),,--maxprocesses=9) --dist worksteal -m "not serial"

.PHONY: tests-parallel-review
tests-parallel-review:
	$(SDK_TEST) -m pytest -n "$${PYTEST_XDIST_AUTO_NUM_WORKERS:-auto}" $(if $(PYTEST_XDIST_AUTO_NUM_WORKERS),,--maxprocesses=9) --dist worksteal -m "not serial and not review_optional"

.PHONY: tests-serial
tests-serial:
	uv run python .github/scripts/run_serial_tests.py

.PHONY: tests-serial-review
tests-serial-review:
	uv run python .github/scripts/run_serial_tests.py --exclude-review-optional

.PHONY: coverage
coverage:
	$(SDK_TEST) -m coverage run -m pytest
	uv run coverage xml -o coverage.xml
	uv run coverage report -m --fail-under=85

.PHONY: build-docs serve-docs
build-docs:
	uv run mkdocs build

serve-docs:
	uv run mkdocs serve

.PHONY: check
check: format-check lint typecheck tests

.PHONY: check-docs
check-docs:
	uv run python .github/scripts/check_docs.py
