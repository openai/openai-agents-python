from __future__ import annotations

import examples.run_examples as run_examples


def test_default_auto_skip_excludes_prerequisite_bound_examples() -> None:
    expected = {
        "examples/sandbox/docker/mounts/azure_mount_read_write.py",
        "examples/sandbox/docker/mounts/gcs_mount_read_write.py",
        "examples/sandbox/docker/mounts/s3_files_mount_read_write.py",
        "examples/sandbox/docker/mounts/s3_mount_read_write.py",
        "examples/sandbox/extensions/daytona/usaspending_text2sql/setup_db.py",
        "examples/sandbox/extensions/temporal/temporal_sandbox_agent.py",
        "examples/sandbox/extensions/vercel_runner.py",
        "examples/sandbox/memory_s3.py",
        "examples/sandbox/sandbox_agent_with_remote_snapshot.py",
        "examples/sandbox/tax_prep.py",
        "examples/sandbox/tutorials/dataroom_metric_extract/evals.py",
        "examples/sandbox/tutorials/dataroom_metric_extract/main.py",
        "examples/sandbox/tutorials/dataroom_qa/main.py",
        "examples/sandbox/tutorials/repo_code_review/evals.py",
        "examples/sandbox/tutorials/repo_code_review/main.py",
        "examples/sandbox/tutorials/vision_website_clone/main.py",
        "examples/tools/codex_same_thread.py",
    }

    assert expected <= run_examples.DEFAULT_AUTO_SKIP


def test_default_auto_skip_keeps_computer_use_example_enabled() -> None:
    assert "examples/tools/computer_use.py" not in run_examples.DEFAULT_AUTO_SKIP
