"""단일 MCP 연결에서 서로 다른 공식 서버 프로세스로 분기되는지 확인한다."""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_integration import FIXTURES, write_policy


def write_multi_policy(path):
    write_policy(path)
    first = path.read_text()
    first = first.replace(
        "burst = 100",
        ("burst = 100\nresponse_timeout_seconds = 0.1\ncleanup_timeout_seconds = 0.1"),
    )
    path.write_text(
        first
        + """
[[targets]]
id = "queue"
environment = "local"
host = "127.0.0.1"
port = 6380
username = "queue_reader"
password_env = "IGNORED_QUEUE"
tls = false
readonly_acl_confirmed = true
tools = ["inspect_key", "list_length"]
allowed_prefixes = ["queue:"]
audit_path = "queue.jsonl"

[targets.limits]
requests_per_second = 100.0
burst = 100
response_timeout_seconds = 0.1
cleanup_timeout_seconds = 0.1
"""
    )


async def test_two_official_processes_route_independently(tmp_path):
    path = tmp_path / "policy.toml"
    write_multi_policy(path)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(FIXTURES / "proxy_stub.py"), str(path)],
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            listed = await session.call_tool("list_targets", {})
            assert [t["id"] for t in listed.structuredContent["data"]["targets"]] == [
                "fixture",
                "queue",
            ]
            missing = await session.call_tool("list_length", {"key": "safe:a"})
            assert missing.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
            unknown = await session.call_tool(
                "list_length", {"target_id": "unknown", "key": "safe:a"}
            )
            assert unknown.structuredContent["error"]["code"] == "TARGET_UNKNOWN"
            first, second = await asyncio.gather(
                session.call_tool("list_length", {"target_id": "fixture", "key": "safe:a"}),
                session.call_tool("list_length", {"target_id": "queue", "key": "queue:a"}),
            )
            # 각 자식 서버가 다른 합성 값을 반환하여 실제 분기를 검증한다.
            assert first.structuredContent["data"]["length"] == 6379
            assert second.structuredContent["data"]["length"] == 6380
            assert first.structuredContent["meta"]["target_id"] == "fixture"
            assert second.structuredContent["meta"]["target_id"] == "queue"
    for name, target in [("audit.jsonl", "fixture"), ("queue.jsonl", "queue")]:
        records = [json.loads(line) for line in (tmp_path / name).read_text().splitlines()]
        assert {record["target"] for record in records} == {target}


async def test_slow_official_target_does_not_block_other_target(tmp_path):
    path = tmp_path / "policy.toml"
    write_multi_policy(path)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(FIXTURES / "proxy_stub.py"), str(path)],
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            slow, healthy = await asyncio.gather(
                session.call_tool("inspect_key", {"target_id": "fixture", "key": "safe:slow"}),
                session.call_tool("list_length", {"target_id": "queue", "key": "queue:a"}),
            )
            assert slow.structuredContent["error"]["code"] == "TIMEOUT"
            assert healthy.structuredContent["data"]["length"] == 6380
            # 첫 대상의 응답 timeout 뒤에도 다른 대상의 호출은 계속 가능하다.
            result = await session.call_tool(
                "inspect_key", {"target_id": "queue", "key": "queue:a"}
            )
            assert result.structuredContent["data"]["type"] == "list"


async def test_abrupt_child_exit_does_not_close_public_session(tmp_path):
    path = tmp_path / "policy.toml"
    write_multi_policy(path)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(FIXTURES / "proxy_stub.py"), str(path)],
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            failed = await session.call_tool(
                "inspect_key", {"target_id": "fixture", "key": "safe:crash"}
            )
            assert failed.structuredContent["error"]["code"] == "UPSTREAM_UNAVAILABLE"
            healthy = await session.call_tool(
                "list_length", {"target_id": "queue", "key": "queue:a"}
            )
            assert healthy.structuredContent["data"]["length"] == 6380
            listed = await session.call_tool("list_targets", {})
            states = {t["id"]: t["status"] for t in listed.structuredContent["data"]["targets"]}
            assert states == {"fixture": "unavailable", "queue": "contract_verified"}
