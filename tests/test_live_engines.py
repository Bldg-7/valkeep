"""실제 Valkey/Redis를 임시 디렉토리·랜덤 loopback 포트에서만 검증한다."""

import json
import os
import secrets
import selectors
import shutil
import socket
import subprocess
import time
from contextlib import asynccontextmanager

import pytest
import valkey
from valkey.backoff import NoBackoff
from valkey.retry import Retry

from valkeep.acl import permission_rules, verify_export
from valkeep.audit import Audit
from valkeep.config import Policy, upstream_environment
from valkeep.errors import Rejected
from valkeep.proxy import Proxy
from valkeep.reader import create_client
from valkeep.upstream import connect_upstream

pytestmark = pytest.mark.live


@pytest.fixture(scope="module", params=[False, True], ids=["plain-local", "tls-local"])
def local_engine(request, tmp_path_factory, server_spec):
    engine, image = server_spec
    binary = shutil.which("valkey-server" if engine == "valkey" else "redis-server")
    required = request.config.getoption("--require-valkey") and engine == "valkey"
    if image is None and binary is None:
        if required:
            pytest.fail("Valkey 테스트를 필수로 요청했지만 valkey-server가 없습니다")
        pytest.skip(f"{engine} 로컬 실행 파일이 없습니다")
    if image and shutil.which("docker") is None:
        pytest.fail("명시한 Valkey 이미지 테스트에는 Docker가 필요합니다")
    tls = request.param
    directory = tmp_path_factory.mktemp(f"valkeep-live-{engine}")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    password = secrets.token_urlsafe(32)
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    if tls:
        openssl = shutil.which("openssl")
        if openssl is None:
            if required or image:
                pytest.fail("필수 TLS 테스트에 openssl이 없습니다")
            pytest.skip("TLS live 테스트에는 openssl이 필요합니다")
        ssl_config = directory / "openssl.cnf"
        ssl_config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            "[dn]\nCN=localhost\n[ext]\nbasicConstraints=critical,CA:TRUE\n"
            "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        )
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-config",
                str(ssl_config),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    config = directory / "server.conf"
    server_dir = "/tmp" if image else str(directory)
    bind = "0.0.0.0" if image else "127.0.0.1"
    configuration = (
        f"bind {bind}\nport {0 if tls else port}\n"
        f'save ""\nappendonly no\ndir "{server_dir}"\n'
        f"user default off\nuser fixture_admin on >{password} ~* &* +@all\n"
    )
    if tls:
        server_cert = "/data/cert.pem" if image else str(cert)
        server_key = "/data/key.pem" if image else str(key)
        configuration += (
            f'tls-port {port}\ntls-cert-file "{server_cert}"\n'
            f'tls-key-file "{server_key}"\ntls-ca-cert-file "{server_cert}"\n'
            "tls-auth-clients no\n"
        )
    config.write_text(configuration)
    config.chmod(0o600)
    container = "valkeep-test-" + secrets.token_hex(8) if image else None
    command = (
        [
            "docker",
            "run",
            "--rm",
            "--name",
            container,
            "--user",
            "0:0",
            "--publish",
            f"127.0.0.1:{port}:{port}",
            "--volume",
            f"{directory}:/data:ro",
            "--entrypoint",
            "valkey-server",
            image,
            "/data/server.conf",
        ]
        if image
        else [binary, str(config)]
    )
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    client = None
    try:
        assert process.stdout
        deadline = time.monotonic() + 30
        output = b""
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while b"Ready to accept connections" not in output:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError(
                        "격리된 엔진 시작 실패: " + output.decode(errors="replace")
                    )
                for event, _ in selector.select(timeout=0.25):
                    output += os.read(event.fd, 4096)
                    if len(output) > 65536:
                        raise AssertionError("시작 로그 상한 초과")
        client = valkey.Valkey(
            host="127.0.0.1",
            port=port,
            username="fixture_admin",
            password=password,
            ssl=tls,
            ssl_ca_certs=str(cert) if tls else None,
            ssl_check_hostname=True,
            decode_responses=True,
            protocol=2,
            socket_timeout=2,
            socket_connect_timeout=2,
            retry=Retry(NoBackoff(), 0),
        )
        # 컨테이너 로그 준비와 호스트 포트 포워딩 준비는 서로 다르다.
        deadline = time.monotonic() + 10
        with selectors.DefaultSelector() as wait:
            while True:
                try:
                    assert client.ping()
                    break
                except valkey.exceptions.ConnectionError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise
                    wait.select(timeout=0.05)
        info = client.info("server")
        if engine == "valkey":
            assert info.get("valkey_version"), "Valkey로 지정한 테스트가 실제 Valkey가 아닙니다"
        else:
            assert info.get("redis_version")
            assert not info.get("valkey_version")
        yield client, port, tls, cert, engine
    finally:
        if client is not None:
            client.close()
        if container:
            subprocess.run(
                ["docker", "rm", "-f", container],
                capture_output=True,
                timeout=15,
                check=False,
            )
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()


@pytest.fixture
def live_policy(local_engine, policy, monkeypatch):
    admin, port, tls, cert, engine = local_engine
    values = policy.model_dump()
    values["target"].update(
        port=port,
        engine=engine,
        tls=tls,
        ca_file=str(cert) if tls else None,
        username="reader_" + secrets.token_hex(6),
        password_env="VALKEEP_LIVE_PASSWORD",
    )
    values["tools"] = [
        "inspect_key",
        "list_length",
        "preview_key",
        "find_keys",
        "connection_status",
    ]
    values["limits"].update(response_timeout_seconds=3.0, cleanup_timeout_seconds=1.0)
    configured = Policy.model_validate(values)
    password = secrets.token_urlsafe(32)
    monkeypatch.setenv("VALKEEP_LIVE_PASSWORD", password)
    # 이 쓰기는 위 fixture가 새로 시작한 격리 서버에만 실행한다.
    admin.execute_command(
        "ACL",
        "SETUSER",
        configured.target.username,
        "reset",
        "on",
        *permission_rules(configured),
        ">" + password,
    )
    return configured


@asynccontextmanager
async def live_proxy(policy):
    audit = Audit(policy)
    try:
        async with connect_upstream(policy) as upstream:
            proxy = Proxy(policy, upstream, audit)
            # 데이터 기능 검증에서는 테스트용 burst를 채운다. rate limit은 별도 검증한다.
            proxy.limiter.tokens = policy.limits.burst
            try:
                yield proxy
            finally:
                await proxy.close()
    finally:
        audit.close()


@pytest.mark.parametrize("kind", ["string", "list", "hash", "set", "zset", "stream", "none"])
async def test_real_type_metadata(local_engine, live_policy, kind):
    admin = local_engine[0]
    key = "safe:" + secrets.token_hex(8)
    if kind == "string":
        admin.set(key, "hello")
        expected, unit = 5, "bytes"
    elif kind == "list":
        admin.rpush(key, "a", "b")
        expected, unit = 2, "items"
    elif kind == "hash":
        admin.hset(key, mapping={"a": "b"})
        expected, unit = 1, "fields"
    elif kind == "set":
        admin.sadd(key, "a")
        expected, unit = 1, "items"
    elif kind == "zset":
        admin.zadd(key, {"a": 1})
        expected, unit = 1, "items"
    elif kind == "stream":
        admin.xadd(key, {"a": "b"})
        expected, unit = 1, "entries"
    else:
        expected, unit = None, None
    async with live_proxy(live_policy) as proxy:
        result = await proxy.execute("inspect_key", {"key": key})
    assert not result.isError, result
    assert result.structuredContent["data"] == {
        "type": kind,
        "ttl_ms": -2 if kind == "none" else -1,
        "length": expected,
        "length_unit": unit,
        "atomic": False,
    }


async def test_positive_ttl_is_milliseconds(local_engine, live_policy):
    local_engine[0].psetex("safe:expiring", 60000, "data")
    async with live_proxy(live_policy) as proxy:
        result = await proxy.execute("inspect_key", {"key": "safe:expiring"})
    data = result.structuredContent["data"]
    assert 0 < data["ttl_ms"] <= 60000
    assert data["length"] == 4


async def test_large_string_preview_is_bounded(local_engine, live_policy):
    admin = local_engine[0]
    admin.set("safe:huge", "a" * 2_000_000)
    async with live_proxy(live_policy) as proxy:
        result = await proxy.execute("preview_key", {"key": "safe:huge"})
    data = result.structuredContent["data"]
    assert data["returned_bytes"] == live_policy.limits.preview_bytes
    assert data["total_bytes_observed"] == 2_000_000
    assert data["value"] == "a" * live_policy.limits.preview_bytes
    assert data["truncated"]


async def test_binary_preview_and_wrong_type(local_engine, live_policy):
    admin = local_engine[0]
    admin.set("safe:binary", b"\xff\x00")
    admin.hset("safe:hash", mapping={"a": "b"})
    async with live_proxy(live_policy) as proxy:
        result = await proxy.execute("preview_key", {"key": "safe:binary"})
        other = await proxy.execute("preview_key", {"key": "safe:hash"})
    assert result.structuredContent["data"]["encoding"] == "base64"
    assert result.structuredContent["data"]["returned_bytes"] == 2
    assert other.structuredContent["error"]["code"] == "UNSUPPORTED_TYPE"


async def test_real_scan_stays_in_scope(local_engine, live_policy):
    admin = local_engine[0]
    admin.set("outside:private", "not-returned")
    for index in range(12):
        admin.set(f"safe:search:{index}", "v")
    async with live_proxy(live_policy) as proxy:
        args = {"scope_id": "prefix-0"}
        keys = []
        for _ in range(live_policy.limits.scan_calls):
            result = await proxy.execute("find_keys", args)
            assert not result.isError, result
            data = result.structuredContent["data"]
            keys.extend(data["keys"])
            if not data["next_token"]:
                break
            args["token"] = data["next_token"]
    assert keys
    assert all(key.startswith("safe:") for key in keys)
    assert "outside:private" not in keys


def test_acl_rejects_write_admin_and_outside_scope(local_engine, live_policy):
    # 실제 쓰기 거절 검증도 격리된 로컬 서버에만 수행한다.
    client = create_client(live_policy)
    try:
        for args in [
            ("SET", "safe:attempt", "never-written"),
            ("DEL", "safe:attempt"),
            ("CONFIG", "GET", "*"),
            ("EVAL", "return 1", "0"),
            ("GET", "outside:private"),
            ("TYPE", "outside:private"),
            ("HGETALL", "safe:hash"),
        ]:
            with pytest.raises(valkey.exceptions.NoPermissionError):
                client.execute_command(*args)
    finally:
        client.close()


def test_export_verification_rejects_excess_permissions(local_engine, live_policy, tmp_path):
    admin = local_engine[0]
    path = tmp_path / "acl-export.json"
    path.write_text(
        json.dumps(admin.execute_command("ACL", "GETUSER", live_policy.target.username))
    )
    assert verify_export(live_policy, path)["matches_policy"]
    admin.execute_command("ACL", "SETUSER", live_policy.target.username, "+set")
    path.write_text(
        json.dumps(admin.execute_command("ACL", "GETUSER", live_policy.target.username))
    )
    with pytest.raises(Rejected, match="ACL_MISMATCH"):
        verify_export(live_policy, path)


async def test_connection_status_and_wrong_password(live_policy, monkeypatch):
    async with live_proxy(live_policy) as proxy:
        result = await proxy.execute("connection_status", {})
    assert result.structuredContent["data"]["connected"]
    monkeypatch.setenv(live_policy.target.password_env, "deliberately-wrong")
    async with live_proxy(live_policy) as proxy:
        denied = await proxy.execute("connection_status", {})
    assert denied.isError
    assert "deliberately-wrong" not in denied.model_dump_json()


def test_tls_hostname_and_untrusted_ca_rejected(local_engine, live_policy):
    if not local_engine[2]:
        return
    data = live_policy.model_dump()
    data["target"]["ca_file"] = None
    client = create_client(Policy.model_validate(data))
    try:
        with pytest.raises(valkey.exceptions.ConnectionError):
            client.ping()
    finally:
        client.close()


async def test_wrong_engine_rejected_with_real_server(local_engine, live_policy):
    values = live_policy.model_dump()
    values["target"]["engine"] = "redis" if local_engine[4] == "valkey" else "valkey"
    async with live_proxy(Policy.model_validate(values)) as proxy:
        result = await proxy.execute("inspect_key", {"key": "safe:a"})
    assert result.structuredContent["error"]["code"] == "ENGINE_MISMATCH"


async def test_hello_handshake_needs_no_extra_acl_privileges(local_engine, live_policy):
    local_engine[0].execute_command("ACL", "SETUSER", live_policy.target.username, "-hello")
    async with live_proxy(live_policy) as proxy:
        result = await proxy.execute("connection_status", {})
    assert result.structuredContent["data"]["connected"] is True


async def test_non_default_database(local_engine, live_policy, monkeypatch):
    values = live_policy.model_dump()
    values["target"]["database"] = 3
    configured = Policy.model_validate(values)
    password = secrets.token_urlsafe(32)
    monkeypatch.setenv(configured.target.password_env, password)
    admin = local_engine[0]
    admin.execute_command(
        "ACL",
        "SETUSER",
        configured.target.username,
        "reset",
        "on",
        *permission_rules(configured),
        ">" + password,
    )
    admin.execute_command("SELECT", 3)
    try:
        admin.set("safe:db3", "third-db")
    finally:
        admin.execute_command("SELECT", 0)
    async with live_proxy(configured) as proxy:
        result = await proxy.execute("inspect_key", {"key": "safe:db3"})
    assert result.structuredContent["data"]["length"] == 8
    assert local_engine[0].type("safe:db3") == "none"


def test_tls_hostname_mismatch_is_rejected(local_engine, live_policy, monkeypatch):
    if not local_engine[2]:
        return
    values = live_policy.model_dump()
    values["target"]["host"] = "wrong-host.invalid"
    configured = Policy.model_validate(values)
    original = socket.getaddrinfo

    def resolve_local(host, *args, **kwargs):
        return original("127.0.0.1" if host == "wrong-host.invalid" else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve_local)
    client = create_client(configured)
    try:
        with pytest.raises(valkey.exceptions.ConnectionError):
            client.ping()
    finally:
        client.close()


def test_subprocess_environment_contains_only_selected_password(live_policy):
    env = upstream_environment(live_policy)
    assert env["VALKEEP_PASSWORD"]
    assert "fixture_admin" not in env.values()


async def test_live_rate_limit_prevents_second_command(live_policy):
    async with live_proxy(live_policy) as proxy:
        proxy.limiter.tokens = 1
        proxy.limiter.clock = lambda: proxy.limiter.last
        first = await proxy.execute("connection_status", {})
        second = await proxy.execute("connection_status", {})
    assert first.structuredContent["data"]["connected"]
    assert second.structuredContent["error"]["code"] == "RATE_LIMITED"


async def test_literal_glob_scope_with_real_acl(local_engine, live_policy, monkeypatch):
    admin = local_engine[0]
    data = live_policy.model_dump()
    data["allowed_prefixes"] = ["safe[*]:"]
    data["allowed_keys"] = []
    scoped = Policy.model_validate(data)
    password = secrets.token_urlsafe(32)
    monkeypatch.setenv(scoped.target.password_env, password)
    admin.execute_command(
        "ACL",
        "SETUSER",
        scoped.target.username,
        "reset",
        "on",
        *permission_rules(scoped),
        ">" + password,
    )
    admin.set("safe[*]:literal", "value")
    admin.set("safeX:outside", "not-returned")
    async with live_proxy(scoped) as proxy:
        inspected = await proxy.execute("inspect_key", {"key": "safe[*]:literal"})
        assert not inspected.isError
        keys = []
        args = {"scope_id": "prefix-0"}
        for _ in range(scoped.limits.scan_calls):
            result = await proxy.execute("find_keys", args)
            assert not result.isError
            page = result.structuredContent["data"]
            keys.extend(page["keys"])
            if not page["next_token"]:
                break
            args["token"] = page["next_token"]
    assert "safe[*]:literal" in keys
    assert all(key.startswith("safe[*]:") for key in keys)


async def test_untrusted_tls_target_is_rejected_by_mcp(local_engine, live_policy):
    if not local_engine[2]:
        return
    data = live_policy.model_dump()
    data["target"]["ca_file"] = None
    async with live_proxy(Policy.model_validate(data)) as proxy:
        result = await proxy.execute("connection_status", {})
    assert result.isError


def test_check_connections_cli(local_engine, live_policy, tmp_path):
    import sys

    config = tmp_path / "connection.toml"
    target = live_policy.target
    config.write_text(
        'policy_version = 2\naudit_path = "router.jsonl"\n[[targets]]\n'
        f'id = "fixture"\nengine = "{target.engine}"\nenvironment = "local"\n'
        f'host = "127.0.0.1"\nport = {target.port}\n'
        f'username = "{target.username}"\npassword_env = "{target.password_env}"\n'
        f"tls = {str(target.tls).lower()}\n"
        + (f"ca_file = {json.dumps(target.ca_file)}\n" if target.ca_file else "")
        + 'readonly_acl_confirmed = true\nallowed_prefixes = ["safe:"]\n'
        'tools = ["connection_status"]\naudit_path = "target.jsonl"\n'
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "valkeep.cli",
            "--config",
            str(config),
            "--check-connections",
            "--target",
            "fixture",
        ],
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["fixture"]["data"]["connected"]
