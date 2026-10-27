"""대상 선택·권한·예산·수명·감사·자격증명의 교차 누출을 검증한다."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from conftest import FakeUpstream
from pydantic import ValidationError

from valkeep.config import ServerConfig, load_config, upstream_environment
from valkeep.errors import Rejected
from valkeep.runtime import open_router


@pytest.fixture
def upstreams():
    return {"catalog": FakeUpstream(), "queue": FakeUpstream()}


def connector_for(upstreams):
    @asynccontextmanager
    async def connect(policy):
        upstream = upstreams[policy.target.id]
        try:
            yield upstream
        finally:
            await upstream.stop()

    return connect


@pytest.fixture
async def router(config, upstreams):
    async with open_router(config, connector=connector_for(upstreams)) as router:
        yield router


async def test_list_targets_is_local_and_hides_connections(router, upstreams):
    result = await router.execute("list_targets", {})
    targets = result.structuredContent["data"]["targets"]
    assert [target["id"] for target in targets] == ["catalog", "queue"]
    assert [target["engine"] for target in targets] == ["valkey", "redis"]
    assert targets[0]["tools"] == ["inspect_key"]
    encoded = result.model_dump_json()
    for secret in ["127.0.0.1", "6379", "reader", "CATALOG_PASSWORD", "catalog:"]:
        assert secret not in encoded
    assert all(not upstream.calls for upstream in upstreams.values())


def test_tool_schema_requires_explicit_target(router):
    tools = {tool.name: tool for tool in router.tools()}
    assert tools["inspect_key"].inputSchema["required"] == ["target_id", "key"]
    assert tools["list_length"].inputSchema["properties"]["target_id"]["enum"] == ["queue"]


@pytest.mark.parametrize(
    "arguments,code",
    [
        ({"key": "catalog:a"}, "INVALID_ARGUMENT"),
        ({"target_id": None, "key": "catalog:a"}, "INVALID_ARGUMENT"),
        ({"target_id": True, "key": "catalog:a"}, "INVALID_ARGUMENT"),
        ({"target_id": "*", "key": "catalog:a"}, "INVALID_ARGUMENT"),
        ({"target_id": "missing", "key": "catalog:a"}, "TARGET_UNKNOWN"),
        ({"target_id": "queue", "key": "catalog:a"}, "KEY_SCOPE_DENIED"),
        ({"target_id": "catalog", "key": "catalog:a", "host": "other"}, "INVALID_ARGUMENT"),
    ],
)
async def test_bad_routes_never_reach_either_target(router, upstreams, arguments, code):
    result = await router.execute("inspect_key", arguments)
    assert result.structuredContent["error"]["code"] == code
    assert all(not upstream.calls for upstream in upstreams.values())


async def test_target_tool_policy_is_enforced(router, upstreams):
    result = await router.execute("list_length", {"target_id": "catalog", "key": "catalog:a"})
    assert result.structuredContent["error"]["code"] == "TOOL_DENIED"
    assert all(not upstream.calls for upstream in upstreams.values())


async def test_route_rewrites_only_the_selected_target(router, upstreams):
    result = await router.execute("inspect_key", {"target_id": "catalog", "key": "catalog:a"})
    assert result.structuredContent["meta"]["target_id"] == "catalog"
    assert upstreams["catalog"].calls == [
        ("valkeep_read", {"request": json.dumps({"operation": "inspect_key", "key": "catalog:a"})})
    ]
    assert not upstreams["queue"].calls


async def test_busy_target_does_not_block_other_target(router, upstreams):
    upstreams["catalog"].gate.clear()
    task = asyncio.create_task(
        router.execute("inspect_key", {"target_id": "catalog", "key": "catalog:a"})
    )
    await upstreams["catalog"].started.wait()
    busy = await router.execute("inspect_key", {"target_id": "catalog", "key": "catalog:b"})
    other = await router.execute("list_length", {"target_id": "queue", "key": "queue:a"})
    assert busy.structuredContent["error"]["code"] == "BUSY"
    assert other.structuredContent["data"]["length"] == 3
    upstreams["catalog"].gate.set()
    await task


async def test_rate_limit_does_not_share_tokens(router):
    for proxy in router.proxies.values():
        proxy.limiter.clock = lambda limiter=proxy.limiter: limiter.last
    await router.execute("inspect_key", {"target_id": "catalog", "key": "catalog:a"})
    limited = await router.execute("inspect_key", {"target_id": "catalog", "key": "catalog:a"})
    other = await router.execute("list_length", {"target_id": "queue", "key": "queue:a"})
    assert limited.structuredContent["error"]["code"] == "RATE_LIMITED"
    assert not other.isError


async def test_failed_target_never_falls_back(router, upstreams):
    upstreams["catalog"].failed = True
    result = await router.execute("inspect_key", {"target_id": "catalog", "key": "catalog:a"})
    assert result.structuredContent["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert all(not upstream.calls for upstream in upstreams.values())
    result = await router.execute("list_length", {"target_id": "queue", "key": "queue:a"})
    assert not result.isError
    listed = await router.execute("list_targets", {})
    assert listed.structuredContent["data"]["targets"][0]["status"] == "unavailable"


async def test_hard_timeout_is_isolated(router, upstreams):
    upstreams["catalog"].gate.clear()
    result = await router.execute("inspect_key", {"target_id": "catalog", "key": "catalog:a"})
    assert result.structuredContent["error"]["code"] == "TIMEOUT"
    await asyncio.wait_for(upstreams["catalog"].stopped.wait(), 1)
    await asyncio.gather(*router.proxies["catalog"].tasks)
    other = await router.execute("list_length", {"target_id": "queue", "key": "queue:a"})
    assert not other.isError
    assert not upstreams["queue"].stopped.is_set()


async def test_target_audit_failure_is_isolated(router, upstreams):
    router.proxies["catalog"].audit.close()
    denied = await router.execute("inspect_key", {"target_id": "catalog", "key": "catalog:a"})
    other = await router.execute("list_length", {"target_id": "queue", "key": "queue:a"})
    assert denied.structuredContent["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert not other.isError
    assert not upstreams["catalog"].calls


async def test_router_audit_failure_blocks_all_targets(router, upstreams):
    router.audit.close()
    result = await router.execute("list_length", {"target_id": "queue", "key": "queue:a"})
    assert result.structuredContent["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert all(not upstream.calls for upstream in upstreams.values())


async def test_audit_target_identity_cannot_be_spoofed(router, config):
    await router.execute("inspect_key", {"target_id": "catalog", "key": "catalog:secret"})
    await router.execute("list_length", {"target_id": "queue", "key": "queue:secret"})
    for target in config.targets:
        records = [json.loads(line) for line in Path(target.audit_path).read_text().splitlines()]
        assert all(record["target"] == target.id for record in records)
        assert "secret" not in Path(target.audit_path).read_text()
    await router.execute("inspect_key", {"target_id": "sensitive-unknown", "key": "catalog:a"})
    assert "sensitive-unknown" not in Path(config.audit_path).read_text()


def test_credentials_are_not_cross_inherited(config, monkeypatch):
    monkeypatch.setenv("CATALOG_PASSWORD", "synthetic-catalog")
    monkeypatch.setenv("QUEUE_PASSWORD", "synthetic-queue")
    a, b = [upstream_environment(target.as_policy()) for target in config.targets]
    assert a["VALKEEP_PASSWORD"] == "synthetic-catalog"
    assert b["VALKEEP_PASSWORD"] == "synthetic-queue"
    assert "synthetic-queue" not in a.values()
    assert "synthetic-catalog" not in b.values()


@pytest.mark.parametrize("case", ["ids", "endpoints", "audit", "empty", "too_many"])
def test_invalid_target_registry(config, case):
    data = config.model_dump()
    if case == "ids":
        data["targets"][1]["id"] = data["targets"][0]["id"]
    elif case == "endpoints":
        data["targets"][1]["port"] = data["targets"][0]["port"]
    elif case == "audit":
        data["targets"][1]["audit_path"] = data["targets"][0]["audit_path"]
    elif case == "empty":
        data["targets"] = []
    else:
        data["targets"] = list(data["targets"]) * 9
    with pytest.raises(ValidationError):
        ServerConfig.model_validate(data)


async def test_startup_failure_closes_all_started_targets(config, upstreams):
    @asynccontextmanager
    async def connector(policy):
        if policy.target.id == "queue":
            raise RuntimeError("private-startup-error")
        try:
            yield upstreams["catalog"]
        finally:
            await upstreams["catalog"].stop()

    with pytest.raises(Rejected, match="UPSTREAM_UNAVAILABLE"):
        async with open_router(config, connector=connector):
            pytest.fail("일부 target만 시작한 서버를 공개하면 안 됩니다")
    assert upstreams["catalog"].stopped.is_set()


async def test_shutdown_closes_each_target(config, upstreams):
    async with open_router(config, connector=connector_for(upstreams)):
        pass
    assert all(upstream.stopped.is_set() for upstream in upstreams.values())


def test_legacy_single_target_config_rejected(tmp_path):
    path = tmp_path / "old.toml"
    path.write_text("policy_version = 1")
    with pytest.raises(Rejected, match="CONFIG_INVALID"):
        load_config(path)
