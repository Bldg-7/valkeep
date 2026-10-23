"""CLI는 검증 모드에서 자격증명·Redis 연결을 요구하지 않는다."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_check_config_without_credentials():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "valkeep.cli",
            "--config",
            str(ROOT / "config/policy.example.toml"),
            "--check-config",
        ],
        env={},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert "Redis 연결·ACL 검증은 수행하지 않음" in result.stderr.decode()


def test_bad_configuration_is_sanitized(tmp_path):
    path = tmp_path / "invalid.toml"
    path.write_text('private_secret = "never-echo-this"')
    result = subprocess.run(
        [sys.executable, "-m", "valkeep.cli", "--config", str(path)],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"valkeep: CONFIG_INVALID\n"


def test_missing_password_fails_before_upstream(tmp_path):
    text = (ROOT / "config/policy.example.toml").read_text()
    text = text.replace("../valkeep.audit.jsonl", "audit.jsonl")
    path = tmp_path / "policy.toml"
    path.write_text(text)
    env = dict(os.environ)
    env.pop("VALKEEP_REDIS_PASSWORD", None)
    result = subprocess.run(
        [sys.executable, "-m", "valkeep.cli", "--config", str(path)],
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"valkeep: CONFIG_INVALID\n"
