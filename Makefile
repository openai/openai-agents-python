.PHONY: sync
sync:
	uv sync --all-extras --all-packages --group dev

.PHONY: update-rclone-pin
update-rclone-pin:
	uv run python .github/scripts/update_rclone_pin.py --cooldown-days $(or $(RCLONE_COOLDOWN_DAYS),7) $(if $(RCLONE_VERSION),--version $(RCLONE_VERSION))

.PHONY: update-released-api-contract
update-released-api-contract:
	@test -n "$(VERSION)" || (echo "VERSION is required, for example VERSION=0.20.0" >&2; exit 2)
	uv run python .github/scripts/update_released_api_contract.py --version "$(VERSION)"

.PHONY: check-released-api-contract
check-released-api-contract:
	@test -n "$(VERSION)" || (echo "VERSION is required, for example VERSION=0.20.0" >&2; exit 2)
	uv run python .github/scripts/update_released_api_contract.py --version "$(VERSION)" --check

PROSPECTIVE_RELEASED_API_CONTRACT ?= .tmp/prospective_released_api_contract.json

.PHONY: prepare-prospective-released-api-contract
prepare-prospective-released-api-contract:
	@version="$$(uv run python -c 'from importlib.metadata import version; print(version("openai-agents"))')"; \
	uv run python .github/scripts/update_released_api_contract.py \
		--version "$$version" \
		--output "$(PROSPECTIVE_RELEASED_API_CONTRACT)"

.PHONY: check-prospective-released-api-contract
check-prospective-released-api-contract: prepare-prospective-released-api-contract
	OPENAI_AGENTS_PROSPECTIVE_RELEASE_CONTRACT="$(abspath $(PROSPECTIVE_RELEASED_API_CONTRACT))" \
		$(MAKE) integration-tests-prospective-contract

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
	uv run mypy $(if $(TYPECHECK_SRC_ONLY),src,.) --exclude site

.PHONY: pyright
pyright:
	uv run pyright --project pyrightconfig.json --threads "$${PYRIGHT_THREADS:-4}" $(if $(TYPECHECK_SRC_ONLY),src,)

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

.PHONY: typecheck-src
typecheck-src:
	@$(MAKE) typecheck TYPECHECK_SRC_ONLY=1

.PHONY: tests
tests: tests-parallel
	$(MAKE) tests-serial

.PHONY: tests-review
tests-review: tests-parallel-review
	$(MAKE) tests-serial-review

.PHONY: tests-asyncio-stability
tests-asyncio-stability:
	bash .github/scripts/run-asyncio-teardown-stability.sh

.PHONY: tests-parallel
tests-parallel:
	uv run pytest -n "$${PYTEST_XDIST_AUTO_NUM_WORKERS:-auto}" $(if $(PYTEST_XDIST_AUTO_NUM_WORKERS),,--maxprocesses=9) --dist worksteal -m "not serial"

.PHONY: tests-parallel-review
tests-parallel-review:
	uv run pytest -n "$${PYTEST_XDIST_AUTO_NUM_WORKERS:-auto}" $(if $(PYTEST_XDIST_AUTO_NUM_WORKERS),,--maxprocesses=9) --dist worksteal -m "not serial and not review_optional"

.PHONY: tests-serial
tests-serial:
	uv run python .github/scripts/run_serial_tests.py

.PHONY: tests-serial-review
tests-serial-review:
	uv run python .github/scripts/run_serial_tests.py --exclude-review-optional

.PHONY: integration-tests
integration-tests:
	uv run python .github/scripts/run_integration_tests.py --profile full $(filter --all,$(MAKECMDGOALS))

.PHONY: integration-tests-release
integration-tests-release:
	uv run python .github/scripts/run_integration_tests.py --profile release $(filter --all,$(MAKECMDGOALS))

.PHONY: integration-tests-nightly
integration-tests-nightly:
	uv run python .github/scripts/run_integration_tests.py --profile nightly $(filter --all,$(MAKECMDGOALS))

.PHONY: integration-tests-manual
integration-tests-manual:
	uv run python .github/scripts/run_integration_tests.py --profile manual $(filter --all,$(MAKECMDGOALS))

.PHONY: integration-tests-packaging
integration-tests-packaging:
	uv run python .github/scripts/run_integration_tests.py --profile packaging

.PHONY: integration-tests-prospective-contract
integration-tests-prospective-contract:
	uv run python .github/scripts/run_integration_tests.py --profile prospective-contract

.PHONY: integration-tests-security
integration-tests-security:
	uv run python .github/scripts/run_integration_tests.py --profile security

.PHONY: integration-tests-mcp-v1
integration-tests-mcp-v1:
	uv run python .github/scripts/run_integration_tests.py --profile mcp-v1

.PHONY: integration-tests-core
integration-tests-core:
	uv run python .github/scripts/run_integration_tests.py --profile core

.PHONY: integration-tests-providers
integration-tests-providers:
	uv run python .github/scripts/run_integration_tests.py --profile providers $(filter --all,$(MAKECMDGOALS))

.PHONY: integration-tests-providers-external
integration-tests-providers-external:
	OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS=1 uv run python .github/scripts/run_integration_tests.py --profile providers $(filter --all,$(MAKECMDGOALS))

.PHONY: integration-tests-providers-all
integration-tests-providers-all:
	uv run python .github/scripts/run_integration_tests.py --profile providers --all

.PHONY: --all
--all:
	@:

.PHONY: integration-tests-realtime
integration-tests-realtime:
	uv run python .github/scripts/run_integration_tests.py --profile realtime

.PHONY: integration-tests-voice
integration-tests-voice:
	uv run python .github/scripts/run_integration_tests.py --profile voice

.PHONY: integration-tests-hosted
integration-tests-hosted:
	uv run python .github/scripts/run_integration_tests.py --profile hosted

.PHONY: integration-tests-extras
integration-tests-extras:
	uv run python .github/scripts/run_integration_tests.py --profile extras

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
