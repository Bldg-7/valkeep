"""Redis 접속 없이 실제 공식 MCP와 stdio 계약을 검증한다."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from valkeep.audit import Audit
from valkeep.contracts import decode_result, verify_contracts
from valkeep.errors import Rejected
from valkeep.proxy import Proxy
from valkeep.upstream import connect_upstream

FIXTURES = Path(__file__).parent / "fixtures"
OFFICIAL = (sys.executable, "-I", str(FIXTURES / "official_stub.py"))


async def test_pinned_official_contract_and_results(policy):
    async with connect_upstream(policy, command=OFFICIAL, environment={}) as upstream:
        assert upstream.extra_tools > 0
        result = await upstream.call("type", {"key": "safe:a"})
        assert decode_result("inspect_key", "safe:a", result) == {
            "type": "list",
            "ttl_seconds": -1,
        }
        result = await upstream.call("llen", {"name": "safe:a"})
        assert decode_result("list_length", "safe:a", result) == {"length": 7}
    assert upstream.process.returncode is not None


async def test_real_sync_worker_timeout_is_killed(policy):
    audit = Audit(policy)
    async with connect_upstream(policy, command=OFFICIAL, environment={}) as upstream:
        proxy = Proxy(policy, upstream, audit)
        result = await proxy.execute("inspect_key", {"key": "safe:slow"})
        assert result.structuredContent["error"]["code"] == "TIMEOUT"
        assert proxy.limiter.inflight == 1
        await asyncio.wait_for(asyncio.gather(*proxy.tasks), 5)
        assert upstream.process.returncode is not None
        assert proxy.closed
        assert proxy.limiter.inflight == 0
    audit.close()


def test_changed_or_duplicate_schema_rejected():
    tool = types.Tool(
        name="type",
        inputSchema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    )
    assert verify_contracts([tool], ("inspect_key",)) == 0
    with pytest.raises(Rejected, match="UPSTREAM_CONTRACT_MISMATCH"):
        verify_contracts([tool, tool], ("inspect_key",))
    tool.inputSchema["additionalProperties"] = False
    with pytest.raises(Rejected, match="UPSTREAM_CONTRACT_MISMATCH"):
        verify_contracts([tool], ("inspect_key",))


def write_policy(path):
    path.write_text(
        "policy_version = 1\n"
        'tools = ["inspect_key", "list_length"]\n'
        'allowed_prefixes = ["safe:"]\n'
        'audit_path = "audit.jsonl"\n'
        "[target]\n"
        'id = "fixture"\n'
        'environment = "local"\n'
        'host = "127.0.0.1"\n'
        'username = "reader"\n'
        'password_env = "IGNORED"\n'
        "tls = false\n"
        "readonly_acl_confirmed = true\n"
        "[limits]\n"
        "requests_per_second = 100.0\n"
        "burst = 100\n"
    )


async def test_public_stdio_capabilities_and_denials(tmp_path):
    path = tmp_path / "policy.toml"
    write_policy(path)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(FIXTURES / "proxy_stub.py"), str(path)],
    )
    with (tmp_path / "stderr").open("w+") as stderr:
        async with stdio_client(params, errlog=stderr) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as session:
                initialized = await session.initialize()
                assert initialized.capabilities.tools is not None
                assert initialized.capabilities.resources is None
                assert initialized.capabilities.prompts is None
                assert initialized.capabilities.logging is None
                listed = await session.list_tools()
                assert {t.name for t in listed.tools} == {"inspect_key", "list_length"}
                denied = await session.call_tool("delete", {"key": "safe:a"})
                assert denied.structuredContent["error"]["code"] == "TOOL_DENIED"
                invalid = await session.call_tool(
                    "inspect_key", {"key": "safe:private", "password": "hidden"}
                )
                assert invalid.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
                allowed = await session.call_tool("inspect_key", {"key": "safe:a"})
                assert allowed.structuredContent["data"]["type"] == "list"
                with pytest.raises(McpError):
                    await session.list_resources()
        stderr.seek(0)
        assert "upstream-private-secret" not in stderr.read()
    assert "hidden" not in (tmp_path / "audit.jsonl").read_text()


async def test_real_cli_initializes_without_redis(tmp_path):
    path = tmp_path / "policy.toml"
    write_policy(path)
    # 연결 준비만 검사한다. 도구 호출 없이, Redis가 없는 포트를 지정한다.
    path.write_text(path.read_text().replace('host = "127.0.0.1"', 'host = "127.0.0.1"\nport = 1'))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "valkeep.cli", "--config", str(path)],
        env={"IGNORED": "synthetic-not-a-credential"},
    )
    async with stdio_client(params) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "valkeep"
            listed = await session.list_tools()
            assert len(listed.tools) == 2
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert events[0]["code"] == "CONTRACT_VERIFIED"
    assert events[0]["ignored_tools"] > 0
    assert len(events) == 1


async def test_oversized_public_frame_closes_process(tmp_path):
    path = tmp_path / "policy.toml"
    write_policy(path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(FIXTURES / "proxy_stub.py"),
        str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(b"x" * 9000 + b"\n"), 10)
        assert b"x" * 100 not in stdout + stderr
        assert b"upstream-private-secret" not in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_upstream_oversized_frame_is_sanitized(policy):
    command = (sys.executable, "-I", "-c", "print('sensitive-' * 200000)")
    with pytest.raises(Exception) as exc:
        async with connect_upstream(policy, command=command, environment={}):
            pytest.fail("계약 검증이 성공해서는 안 됩니다")
    assert "sensitive-" not in str(exc.value)


async def test_public_invalid_arguments_do_not_echo_secrets(tmp_path):
    path = tmp_path / "policy.toml"
    write_policy(path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(FIXTURES / "proxy_stub.py"),
        str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "inspect_key", "arguments": "private-secret"},
            }
        ).encode()
        + b"\n"
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(payload), 10)
    assert b"private-secret" not in stdout + stderr
