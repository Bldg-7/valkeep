"""SCAN 호출·시간·관측·메모리 예산 및 토큰 재사용 방지 검증."""

import asyncio
import json

import pytest
from mcp import types

from valkeep.audit import Audit
from valkeep.config import Policy
from valkeep.proxy import Proxy
from valkeep.scans import Scans


def page(keys=(), cursor=1, observed=None, truncated=False):
    value = {
        "ok": True,
        "data": {
            "keys": list(keys),
            "cursor": cursor,
            "observed": len(keys) if observed is None else observed,
            "truncated": truncated,
        },
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(value))],
        structuredContent=value,
    )


@pytest.fixture
def discovery(policy, upstream):
    data = policy.model_dump()
    data.update(tools=["find_keys"], allowed_prefixes=["safe:", "other:"])
    data["limits"].update(scan_calls=3, scan_sessions=2, scan_observations=5)
    configured = Policy.model_validate(data)
    audit = Audit(configured)
    proxy = Proxy(configured, upstream, audit)
    proxy.limiter.tokens = 100
    upstream.result = page()
    yield proxy
    audit.close()


async def test_empty_pages_still_consume_call_budget(discovery, upstream):
    args = {"scope_id": "prefix-0"}
    for index in range(3):
        result = await discovery.execute("find_keys", args)
        data = result.structuredContent["data"]
        assert data["calls"] == index + 1
        assert data["keys"] == []
        args["token"] = data["next_token"]
    assert data["next_token"] is None
    assert data["stop_reason"] == "call_budget"
    assert data["partial"]
    assert len(upstream.calls) == 3
    assert not discovery.scans.states


async def test_cursor_never_leaves_proxy_and_token_rotates(discovery, upstream):
    first = await discovery.execute("find_keys", {"scope_id": "prefix-0"})
    token = first.structuredContent["data"]["next_token"]
    assert "cursor" not in first.model_dump_json()
    second = await discovery.execute("find_keys", {"scope_id": "prefix-0", "token": token})
    assert second.structuredContent["data"]["next_token"] != token
    reused = await discovery.execute("find_keys", {"scope_id": "prefix-0", "token": token})
    assert reused.structuredContent["error"]["code"] == "CURSOR_INVALID"
    assert len(upstream.calls) == 2


async def test_tokens_cannot_cross_scope_or_target(discovery, upstream):
    first = await discovery.execute("find_keys", {"scope_id": "prefix-0"})
    token = first.structuredContent["data"]["next_token"]
    wrong_scope = await discovery.execute("find_keys", {"scope_id": "prefix-1", "token": token})
    assert wrong_scope.structuredContent["error"]["code"] == "CURSOR_INVALID"
    other = Scans(discovery.policy)
    from valkeep.errors import Rejected

    with pytest.raises(Rejected, match="CURSOR_INVALID"):
        other.validate("prefix-0", token)
    assert len(upstream.calls) == 1


async def test_same_token_concurrent_use_is_rejected(discovery, upstream):
    first = await discovery.execute("find_keys", {"scope_id": "prefix-0"})
    args = {"scope_id": "prefix-0", "token": first.structuredContent["data"]["next_token"]}
    upstream.started.clear()
    upstream.gate.clear()
    worker = asyncio.create_task(discovery.execute("find_keys", args))
    await upstream.started.wait()
    second = await discovery.execute("find_keys", args)
    assert second.structuredContent["error"]["code"] == "CURSOR_INVALID"
    upstream.gate.set()
    await worker


async def test_expired_token_does_not_call_redis(discovery, upstream):
    clock = [0.0]
    discovery.scans.clock = lambda: clock[0]
    first = await discovery.execute("find_keys", {"scope_id": "prefix-0"})
    clock[0] = 1000
    denied = await discovery.execute(
        "find_keys",
        {
            "scope_id": "prefix-0",
            "token": first.structuredContent["data"]["next_token"],
        },
    )
    assert denied.structuredContent["error"]["code"] == "CURSOR_INVALID"
    assert len(upstream.calls) == 1


async def test_observation_budget_counts_duplicates(discovery, upstream):
    upstream.result = page(["safe:a", "safe:a", "safe:b"], observed=3)
    first = await discovery.execute("find_keys", {"scope_id": "prefix-0"})
    assert first.structuredContent["data"]["keys"] == ["safe:a", "safe:b"]
    second = await discovery.execute(
        "find_keys",
        {
            "scope_id": "prefix-0",
            "token": first.structuredContent["data"]["next_token"],
        },
    )
    data = second.structuredContent["data"]
    assert data["observed"] == 6
    assert data["keys"] == []
    assert data["stop_reason"] == "observation_budget"
    assert data["next_token"] is None


async def test_outside_keys_or_truncation_end_session(discovery, upstream):
    upstream.result = page(["safe:a", "outside:secret"])
    result = await discovery.execute("find_keys", {"scope_id": "prefix-0"})
    data = result.structuredContent["data"]
    assert data["keys"] == ["safe:a"]
    assert data["truncated"]
    assert data["next_token"] is None
    assert "outside:secret" not in result.model_dump_json()


async def test_active_session_cap(discovery, upstream):
    await discovery.execute("find_keys", {"scope_id": "prefix-0"})
    await discovery.execute("find_keys", {"scope_id": "prefix-1"})
    third = await discovery.execute("find_keys", {"scope_id": "prefix-0"})
    assert third.structuredContent["error"]["code"] == "SCAN_BUDGET_EXCEEDED"
    assert len(upstream.calls) == 2


@pytest.mark.parametrize(
    "args",
    [
        {"scope_id": "prefix-0", "cursor": 1},
        {"scope_id": "prefix-0", "pattern": "*"},
        {"scope_id": "prefix-0", "token": None},
        {"scope_id": "prefix-0", "token": ""},
        {"scope_id": ["prefix-0"]},
    ],
)
async def test_raw_scan_controls_are_denied(discovery, upstream, args):
    result = await discovery.execute("find_keys", args)
    assert result.isError
    assert not upstream.calls


async def test_response_limit_invalidates_continuation(policy, upstream):
    data = policy.model_dump()
    data["tools"] = ["find_keys"]
    data["limits"].update(max_response_bytes=1024, scan_observations=100)
    configured = Policy.model_validate(data)
    audit = Audit(configured)
    proxy = Proxy(configured, upstream, audit)
    upstream.result = page(["safe:" + str(i) + "x" * 100 for i in range(30)])
    try:
        result = await proxy.execute("find_keys", {"scope_id": "prefix-0"})
        assert len(result.model_dump_json(by_alias=True).encode()) <= 1024
        assert result.structuredContent["data"]["next_token"] is None
        assert result.structuredContent["data"]["stop_reason"] == "response_size"
        assert not proxy.scans.states
    finally:
        audit.close()
