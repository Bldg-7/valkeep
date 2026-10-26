"""다중 target 초기화 취소와 감사 경로 검증의 회귀 테스트."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from test_integration import write_policy

from valkeep.audit import Audit
from valkeep.config import load_config
from valkeep.errors import Rejected
from valkeep.runtime import open_router


async def test_cancelled_startup_cleans_up_each_runtime(config):
    entered = {target.id: asyncio.Event() for target in config.targets}
    cleaned = {target.id: asyncio.Event() for target in config.targets}

    @asynccontextmanager
    async def connector(policy):
        try:
            entered[policy.target.id].set()
            await asyncio.Event().wait()
            yield
        finally:
            cleaned[policy.target.id].set()

    async def start():
        async with open_router(config, connector=connector):
            pytest.fail("초기화가 완료되어서는 안 됩니다")

    task = asyncio.create_task(start())
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert all(event.is_set() for event in cleaned.values())


def test_config_normalization_keeps_symlink_rejection(tmp_path):
    path = tmp_path / "config.toml"
    write_policy(path)
    destination = tmp_path / "other.jsonl"
    destination.touch(mode=0o600)
    (tmp_path / "audit.jsonl").symlink_to(destination)
    config = load_config(path)
    assert config.targets[0].audit_path == str(tmp_path / "audit.jsonl")
    with pytest.raises(Rejected, match="AUDIT_UNAVAILABLE"):
        Audit(config.targets[0].as_policy())


def test_equivalent_audit_paths_rejected(tmp_path):
    path = tmp_path / "config.toml"
    write_policy(path)
    path.write_text(
        path.read_text().replace('audit_path = "router.jsonl"', 'audit_path = "./audit.jsonl"')
    )
    with pytest.raises(Rejected, match="CONFIG_INVALID"):
        load_config(path)
