"""실제 Redis에 연결하지 않는 테스트 구성."""

import asyncio
import json

import pytest
from mcp import types

from valkeep.config import Policy
from valkeep.errors import Code, Rejected


@pytest.fixture
def policy(tmp_path):
    return Policy.model_validate(
        {
            "policy_version": 1,
            "target": {
                "id": "fixture",
                "environment": "local",
                "host": "127.0.0.1",
                "username": "reader",
                "password_env": "VALKEEP_TEST_PASSWORD",
                "tls": False,
                "readonly_acl_confirmed": True,
            },
            "allowed_prefixes": ["safe:"],
            "allowed_keys": ["one"],
            "tools": ["inspect_key", "list_length"],
            "audit_path": str(tmp_path / "audit.jsonl"),
            "limits": {
                "requests_per_second": 100.0,
                "burst": 100,
                "response_timeout_seconds": 0.1,
                "cleanup_timeout_seconds": 0.1,
            },
        }
    )


class FakeUpstream:
    def __init__(self):
        self.failed = False
        self.calls = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.result = None

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        self.started.set()
        await self.gate.wait()
        if self.failed:
            raise Rejected(Code.UPSTREAM_UNAVAILABLE)
        if self.result is not None:
            return self.result
        value = {"key": arguments["key"], "type": "list", "ttl": -1} if name == "type" else 3
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(value))],
            structuredContent={"result": value},
        )

    async def stop(self):
        self.failed = True
        self.gate.set()
        self.stopped.set()


@pytest.fixture
def upstream():
    return FakeUpstream()
