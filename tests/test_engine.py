"""엔진 선택·연결 협상·ACL 프로필 및 버전 호환 계약 검증."""

import json

import pytest
from pydantic import ValidationError

from valkeep.acl import permission_rules, plan
from valkeep.config import Policy, Target
from valkeep.engine import ConnectionPolicyError, initialize_connection
from valkeep.errors import Code
from valkeep.reader import Reader, create_client
from valkeep.worker import build_worker


class Connection:
    def __init__(self, server=b"valkey", version=b"8.1.6", mode=b"standalone", protocol=2):
        self.response = [
            b"server",
            server,
            b"version",
            version,
            b"proto",
            protocol,
            b"mode",
            mode,
        ]
        self.initialized = False
        self.commands = []

    def on_connect(self):
        self.initialized = True

    def send_command(self, *args, **kwargs):
        assert self.initialized
        self.commands.append((args, kwargs))

    def read_response(self):
        return self.response


@pytest.mark.parametrize(
    "engine,version",
    [
        ("valkey", b"8.1.6"),
        ("valkey", b"9.1.2"),
        ("redis", b"7.2.0"),
        ("redis", b"8.10.1"),
    ],
)
def test_supported_engine_hello(engine, version):
    connection = Connection(server=engine.encode(), version=version)
    initialize_connection(connection, engine)
    assert connection.commands == [(("HELLO", 2), {"check_health": False})]


@pytest.mark.parametrize("expected,actual", [("valkey", b"redis"), ("redis", b"valkey")])
def test_wrong_engine_is_rejected_before_data_commands(expected, actual):
    with pytest.raises(ConnectionPolicyError) as error:
        initialize_connection(Connection(server=actual), expected)
    assert error.value.code == Code.ENGINE_MISMATCH


@pytest.mark.parametrize("engine,version", [("valkey", b"8.0.9"), ("redis", b"7.0.15")])
def test_unsupported_version(engine, version):
    with pytest.raises(ConnectionPolicyError) as error:
        initialize_connection(Connection(server=engine.encode(), version=version), engine)
    assert error.value.code == Code.UNSUPPORTED_SERVER_VERSION


@pytest.mark.parametrize("mode,protocol", [(b"cluster", 2), (b"sentinel", 2), (b"standalone", 3)])
def test_unsupported_topology(mode, protocol):
    with pytest.raises(ConnectionPolicyError) as error:
        initialize_connection(Connection(mode=mode, protocol=protocol), "valkey")
    assert error.value.code == Code.UNSUPPORTED_TOPOLOGY


@pytest.mark.parametrize("response", [None, [], [b"server"], [b"server", b"valkey"] * 2])
def test_malformed_hello_is_sanitized(response):
    connection = Connection()
    connection.response = response
    with pytest.raises(ConnectionPolicyError):
        initialize_connection(connection, "valkey")


def test_valkey_is_default_and_unknown_engine_rejected(policy):
    assert policy.target.engine == "valkey"
    values = policy.target.model_dump()
    values["engine"] = "other-engine"
    with pytest.raises(ValidationError):
        Target.model_validate(values)


def test_client_is_official_valkey_with_retries_disabled(policy, monkeypatch):
    import valkey

    monkeypatch.setenv(policy.target.password_env, "synthetic-only")
    client = create_client(policy)
    try:
        assert isinstance(client, valkey.Valkey)
        params = client.connection_pool.connection_kwargs
        assert params["socket_timeout"] == policy.limits.socket_timeout_seconds
        assert params["socket_connect_timeout"] == policy.limits.socket_timeout_seconds
        if policy.target.tls:
            assert params["ssl_check_hostname"] is True
        assert params["protocol"] == 2
        assert params["retry"]._retries == 0
        assert callable(params["valkey_connect_func"])
    finally:
        client.close()


@pytest.mark.parametrize("engine,minimum", [("valkey", "8.1"), ("redis", "7.2")])
def test_acl_plan_tracks_engine_profile(policy, engine, minimum):
    values = policy.model_dump()
    values["target"]["engine"] = engine
    configured = Policy.model_validate(values)
    document = plan(configured)
    assert document["engine"] == engine
    assert document["minimum_version"] == minimum
    assert "+hello" not in document["acl_setuser_arguments"]
    assert "+@connection" not in document["acl_setuser_arguments"]
    assert "+@read" not in document["acl_setuser_arguments"]
    assert "+@all" not in document["acl_setuser_arguments"]
    assert "+info" not in permission_rules(configured)


async def test_worker_registers_only_reviewed_tools(policy):
    data = policy.model_dump()
    data["tools"] = ["inspect_key", "list_length"]
    configured = Policy.model_validate(data)
    worker = build_worker(Reader(object(), configured), configured)
    assert {tool.name for tool in await worker.list_tools()} == {"valkeep_read", "llen"}
    data["tools"] = ["list_length"]
    configured = Policy.model_validate(data)
    worker = build_worker(Reader(object(), configured), configured)
    assert {tool.name for tool in await worker.list_tools()} == {"llen"}


def test_legacy_and_new_timeout_names_cannot_conflict(policy):
    values = policy.model_dump()
    values["limits"]["redis_timeout_seconds"] = 20.0
    with pytest.raises(ValidationError):
        Policy.model_validate(values)


def test_old_socket_timeout_name_is_accepted_for_migration(policy):
    values = policy.model_dump()
    values["limits"].pop("socket_timeout_seconds")
    values["limits"]["redis_timeout_seconds"] = 1.5
    configured = Policy.model_validate(values)
    assert configured.limits.socket_timeout_seconds == 1.5
    assert "redis_timeout_seconds" not in json.dumps(configured.model_dump())
