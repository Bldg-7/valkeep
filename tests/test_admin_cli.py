"""ACL 진단 CLI는 권한을 적용하거나 자격증명을 출력하지 않는다."""

import json
import subprocess
import sys
from pathlib import Path

from test_presets_acl import export_for

from valkeep.config import load_config

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config/policy.example.toml"


def test_acl_plan_requires_no_credentials_or_redis():
    result = subprocess.run(
        [sys.executable, "-m", "valkeep.cli", "--config", str(CONFIG), "--acl-plan"],
        env={},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    plans = json.loads(result.stdout)
    assert len(plans) == 2
    assert all(plan["applied"] is False for plan in plans)
    assert all("+getrange" not in plan["acl_setuser_arguments"] for plan in plans)


def test_acl_verification_cli_is_offline(tmp_path):
    config = load_config(CONFIG)
    export = tmp_path / "acl.json"
    export.write_text(json.dumps(export_for(config.targets[0].as_policy())))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "valkeep.cli",
            "--config",
            str(CONFIG),
            "--verify-acl",
            str(export),
            "--target",
            config.targets[0].id,
        ],
        env={},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["matches_policy"]
    assert not json.loads(result.stdout)["live_account_verified"]
    assert b"synthetic-not-a-real-password-hash" not in result.stdout + result.stderr


def test_unknown_cli_target_never_connects():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "valkeep.cli",
            "--config",
            str(CONFIG),
            "--check-connections",
            "--target",
            "unregistered",
        ],
        env={},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stderr == b"valkeep: TARGET_UNKNOWN\n"
