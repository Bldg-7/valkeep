"""검증 실패는 업스트림에 전달하지 않아야 한다."""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from valkeep.audit import Audit
from valkeep.config import Policy, load_config, upstream_environment
from valkeep.errors import Code, Rejected
from valkeep.limiter import Limiter
from valkeep.proxy import Proxy


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("delete", {"key": "safe:a"}, "TOOL_DENIED"),
        ("scan_all_keys", {}, "TOOL_DENIED"),
        ("scan_keys", {"pattern": "*"}, "TOOL_DENIED"),
        ("get", {"key": "safe:a"}, "TOOL_DENIED"),
        ("inspect_key", {"key": "secret:a"}, "KEY_SCOPE_DENIED"),
        ("inspect_key", {"key": "safe:a", "host": "other"}, "INVALID_ARGUMENT"),
        ("inspect_key", {"key": True}, "INVALID_ARGUMENT"),
        ("inspect_key", {"key": ""}, "INVALID_ARGUMENT"),
        ("inspect_key", {"key": "safe:" + "한" * 512}, "INVALID_ARGUMENT"),
        ("inspect_key", {"key": "safe:\ud800"}, "INVALID_ARGUMENT"),
        ("inspect_key", None, "INVALID_ARGUMENT"),
        ("list_length", {"name": "safe:a"}, "INVALID_ARGUMENT"),
    ],
)
async def test_denied_before_upstream(policy, upstream, name, arguments, code):
    audit = Audit(policy)
    proxy = Proxy(policy, upstream, audit)
    try:
        result = await proxy.execute(name, arguments)
        assert result.isError
        assert result.structuredContent == {"error": {"code": code}}
        assert upstream.calls == []
    finally:
        audit.close()


async def test_mapping_and_safe_audit(policy, upstream):
    audit = Audit(policy)
    proxy = Proxy(policy, upstream, audit)
    result = await proxy.execute("inspect_key", {"key": "safe:private"})
    audit.close()
    assert result.structuredContent["data"] == {
        "type": "list",
        "ttl_ms": -1,
        "length": 3,
        "length_unit": "items",
        "atomic": False,
    }
    assert upstream.calls == [
        (
            "valkeep_read",
            {"request": json.dumps({"operation": "inspect_key", "key": "safe:private"})},
        )
    ]
    records = Path(policy.audit_path).read_text()
    assert "safe:private" not in records
    assert [json.loads(line)["event"] for line in records.splitlines()] == ["started", "finished"]
    assert os.stat(policy.audit_path).st_mode & 0o777 == 0o600


async def test_unknown_tool_name_not_logged(policy, upstream):
    audit = Audit(policy)
    proxy = Proxy(policy, upstream, audit)
    await proxy.execute("password-is-secret", {})
    audit.close()
    assert "password-is-secret" not in Path(policy.audit_path).read_text()


async def test_audit_failure_prevents_call(policy, upstream):
    audit = Audit(policy)
    audit.close()
    proxy = Proxy(policy, upstream, audit)
    result = await proxy.execute("inspect_key", {"key": "safe:a"})
    assert result.structuredContent["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert not upstream.calls
    assert proxy.limiter.inflight == 0


def test_scope_must_not_be_empty(policy):
    data = policy.model_dump()
    data["allowed_prefixes"] = [""]
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        {"tools": ["scan_keys"]},
        {"tools": ["inspect_key", "inspect_key"]},
        {"unexpected": True},
        {"allowed_prefixes": [], "allowed_keys": []},
    ],
)
def test_bad_policy(policy, mutation):
    data = policy.model_dump()
    data.update(mutation)
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_remote_plaintext_and_default_account_rejected(policy):
    for changes in [{"host": "redis.example.com"}, {"username": "default"}]:
        data = policy.model_dump()
        data["target"].update(changes)
        with pytest.raises(ValidationError):
            Policy.model_validate(data)


def test_environment_does_not_inherit_credentials(policy, monkeypatch):
    monkeypatch.setenv("VALKEEP_TEST_PASSWORD", "test-only-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-for-child")
    monkeypatch.setenv("REDIS_HOST", "unapproved-host")
    monkeypatch.setenv("PYTHONPATH", "/unapproved")
    env = upstream_environment(policy)
    assert env["VALKEEP_PASSWORD"] == "test-only-secret"
    assert json.loads(env["VALKEEP_POLICY"])["target"]["host"] == "127.0.0.1"
    assert "REDIS_HOST" not in env
    assert not {"AWS_SECRET_ACCESS_KEY", "PYTHONPATH", "VALKEEP_TEST_PASSWORD"} & env.keys()


def test_missing_password_is_sanitized(policy, monkeypatch):
    monkeypatch.delenv("VALKEEP_TEST_PASSWORD", raising=False)
    with pytest.raises(Rejected, match="CONFIG_INVALID"):
        upstream_environment(policy)


def test_audit_refuses_symlinks_and_world_readable(policy, tmp_path):
    target = tmp_path / "other"
    target.write_text("")
    os.symlink(target, policy.audit_path)
    with pytest.raises(Rejected, match="AUDIT_UNAVAILABLE"):
        Audit(policy)
    os.unlink(policy.audit_path)
    Path(policy.audit_path).touch()
    os.chmod(policy.audit_path, 0o644)
    with pytest.raises(Rejected, match="AUDIT_UNAVAILABLE"):
        Audit(policy)


def test_limiter_with_test_clock(policy):
    clock = [0.0]
    limiter = Limiter(policy.limits, clock=lambda: clock[0])
    limiter.acquire()
    with pytest.raises(Rejected) as exc:
        limiter.acquire()
    assert exc.value.code == Code.BUSY
    limiter.release()
    with pytest.raises(Rejected) as exc:
        limiter.acquire()
    assert exc.value.code == Code.RATE_LIMITED
    clock[0] = 1.0
    limiter.acquire()
    assert limiter.inflight == 1


def test_settings_errors_do_not_expose_values(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('password = "never-show-this"')
    with pytest.raises(Rejected) as exc:
        load_config(path)
    assert str(exc.value) == "CONFIG_INVALID"
