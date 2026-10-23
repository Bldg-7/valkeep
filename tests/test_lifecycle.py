"""응답 취소와 실제 업스트림 실행 종료를 구분한다."""

import asyncio
from pathlib import Path

import pytest
from mcp import types

from valkeep.audit import Audit
from valkeep.proxy import Proxy


async def test_timeout_retains_slot(policy, upstream):
    audit = Audit(policy)
    proxy = Proxy(policy, upstream, audit)
    upstream.gate.clear()
    result = await proxy.execute("inspect_key", {"key": "safe:a"})
    assert result.structuredContent["error"]["code"] == "TIMEOUT"
    assert proxy.limiter.inflight == 1
    second = await proxy.execute("list_length", {"key": "safe:a"})
    assert second.structuredContent["error"]["code"] == "BUSY"
    assert len(upstream.calls) == 1
    upstream.gate.set()
    await asyncio.gather(*proxy.tasks)
    assert proxy.limiter.inflight == 0
    audit.close()


async def test_cancel_retains_slot(policy, upstream):
    audit = Audit(policy)
    proxy = Proxy(policy, upstream, audit)
    upstream.gate.clear()
    waiter = asyncio.create_task(proxy.execute("inspect_key", {"key": "safe:a"}))
    await upstream.started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert proxy.limiter.inflight == 1
    upstream.gate.set()
    await asyncio.gather(*proxy.tasks)
    assert proxy.limiter.inflight == 0
    audit.close()


async def test_hard_timeout_stops_without_restart(policy, upstream):
    audit = Audit(policy)
    proxy = Proxy(policy, upstream, audit)
    upstream.gate.clear()
    await proxy.execute("inspect_key", {"key": "safe:a"})
    await asyncio.wait_for(upstream.stopped.wait(), 1)
    await asyncio.gather(*proxy.tasks)
    assert proxy.closed
    assert proxy.limiter.inflight == 0
    result = await proxy.execute("inspect_key", {"key": "safe:a"})
    assert result.structuredContent["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert len(upstream.calls) == 1
    audit.close()


async def test_close_terminates_active_call(policy, upstream):
    audit = Audit(policy)
    proxy = Proxy(policy, upstream, audit)
    upstream.gate.clear()
    waiter = asyncio.create_task(proxy.execute("inspect_key", {"key": "safe:a"}))
    await upstream.started.wait()
    await proxy.close()
    await waiter
    assert upstream.stopped.is_set()
    assert proxy.limiter.inflight == 0
    audit.close()


@pytest.mark.parametrize(
    "payload",
    [
        types.CallToolResult(
            content=[types.TextContent(type="text", text="password=secret")], isError=True
        ),
        types.CallToolResult(content=[types.TextContent(type="text", text="secret-error")]),
        types.CallToolResult(content=[]),
        types.CallToolResult(content=[types.TextContent(type="text", text="true")]),
    ],
)
async def test_upstream_errors_are_not_exposed(policy, upstream, payload):
    audit = Audit(policy)
    proxy = Proxy(policy, upstream, audit)
    upstream.result = payload
    result = await proxy.execute("list_length", {"key": "safe:a"})
    assert result.isError
    assert "secret" not in result.model_dump_json()
    audit.close()
    assert "secret" not in Path(policy.audit_path).read_text()
