from agents.sandbox.entries.mounts.patterns import _join_mountpoint_command


def test_join_mountpoint_command_redacts_aws_credentials_for_display() -> None:
    env_vars = [
        ("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE"),
        ("AWS_SECRET_ACCESS_KEY", "super-secret"),
        ("AWS_SESSION_TOKEN", "session-token"),
    ]

    real_command = _join_mountpoint_command(
        ["mount-s3", "bucket-name", "/workspace/mnt"], env_vars
    )
    display_command = _join_mountpoint_command(
        ["mount-s3", "bucket-name", "/workspace/mnt"], env_vars, redact=True
    )

    assert "super-secret" in real_command
    assert "session-token" in real_command
    assert "AKIAEXAMPLE" in real_command
    assert "super-secret" not in display_command
    assert "session-token" not in display_command
    assert "AKIAEXAMPLE" not in display_command
    assert display_command.count("REDACTED") == 3
    assert "mount-s3 bucket-name /workspace/mnt" in display_command
