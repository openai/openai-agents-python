# Repository skill tests

Run `make tests-repo-skills` from the repository root. This command uses `uv run --no-project --python 3.11` and needs Git and make for disposable repository fixtures. It installs no SDK dependencies and does not collect `tests/` or pytest configuration. With an existing Python 3.11+ interpreter, use `python .github/scripts/run_repo_skill_tests.py` directly.

Use `uv run --no-project --python 3.11 python .github/scripts/run_repo_skill_tests.py --list` to print the discovered inventory without executing tests. The runner discovers `.agents/skills/*/scripts/test_*.py` and its own `.github/scripts/test_run_repo_skill_tests.py` regression suite. Add skill helper tests under that pattern using standard-library `unittest` and sibling imports.

The current skill inventory contains 159 tests across five modules:

| Skill | Module | Tests |
| --- | --- | --- |
| implementation-final-review | test_review_protocol.py | 77 |
| implementation-final-review | test_review_state.py | 43 |
| implementation-kickoff | test_validate_handoff.py | 12 |
| release-candidate-prep | test_prepare.py | 17 |
| sensitive-logging-audit | test_inventory.py | 10 |

Each module runs in a separate interpreter from its own scripts directory so sibling imports cannot collide across suites. Child processes receive only process essentials and fixed test settings; inherited API keys, tokens, Python import overrides, and user Git configuration are excluded. Git transport is limited to local files. Tests must use disposable local Git repositories and local bare origins, with no live service calls.

The runner executes every discovered module and reports all failed modules before returning a nonzero exit code. An empty skill inventory also fails. The dedicated `repo-skills.yml` workflow runs the same Make target when skills or the command's owning files change. Existing review tiers, budgets, and validator behavior are unchanged.
