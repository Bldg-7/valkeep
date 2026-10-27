"""실제 Valkey/Redis에 연결하지 않는 테스트 구성."""

import asyncio
import json

import pytest
from mcp import types

from valkeep.config import Policy, ServerConfig
from valkeep.errors import Code, Rejected


def pytest_addoption(parser):
    parser.addoption(
        "--valkey-image",
        action="append",
        default=[],
        help="live 테스트에 사용할 Valkey Docker 이미지. 반복 지정 가능",
    )
    parser.addoption(
        "--require-valkey",
        action="store_true",
        default=False,
        help="Valkey/TLS 테스트 실행 파일이 없을 때 skip 대신 실패",
    )


def pytest_generate_tests(metafunc):
    if "server_spec" in metafunc.fixturenames:
        images = metafunc.config.getoption("--valkey-image")
        specs = [("redis", None)]
        ids = ["redis-native"]
        if images:
            specs.extend(("valkey", image) for image in images)
            ids.extend(f"valkey-image-{index}" for index in range(len(images)))
        else:
            specs.append(("valkey", None))
            ids.append("valkey-native")
        metafunc.parametrize("server_spec", specs, ids=ids, scope="module")


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
        self.extra_tools = 0
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
        value = 3
        if name == "valkeep_read":
            request = json.loads(arguments["request"])
            if request["operation"] == "inspect_key":
                value = {
                    "ok": True,
                    "data": {
                        "type": "list",
                        "ttl_ms": -1,
                        "length": 3,
                        "length_unit": "items",
                        "atomic": False,
                    },
                }
            elif request["operation"] == "connection_status":
                value = {"ok": True, "data": {"connected": True}}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(value))],
            structuredContent={"result": value} if name == "llen" else value,
        )

    async def stop(self):
        self.failed = True
        self.gate.set()
        self.stopped.set()


@pytest.fixture
def upstream():
    return FakeUpstream()


@pytest.fixture
def config(policy, tmp_path):
    base = policy.model_dump(exclude={"target", "policy_version"})
    a = {
        **base,
        **policy.target.model_dump(),
        "id": "catalog",
        "port": 6379,
        "password_env": "CATALOG_PASSWORD",
        "audit_path": str(tmp_path / "catalog.jsonl"),
        "allowed_prefixes": ["catalog:"],
        "allowed_keys": [],
        "tools": ["inspect_key"],
    }
    b = {
        **base,
        **policy.target.model_dump(),
        "id": "queue",
        "engine": "redis",
        "port": 6380,
        "password_env": "QUEUE_PASSWORD",
        "audit_path": str(tmp_path / "queue.jsonl"),
        "allowed_prefixes": ["queue:"],
        "allowed_keys": [],
    }
    return ServerConfig.model_validate(
        {
            "policy_version": 2,
            "targets": [a, b],
            "audit_path": str(tmp_path / "router.jsonl"),
        }
    )
