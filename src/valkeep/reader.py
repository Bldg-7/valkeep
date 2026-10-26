"""공식 MCP 프로세스 안에서 실행하는 제한된 읽기 어댑터."""

import base64
import json
import os
from typing import Any

import redis
from redis.backoff import NoBackoff
from redis.exceptions import NoPermissionError, RedisError, ResponseError
from redis.retry import Retry

from valkeep.config import Policy
from valkeep.errors import Code, Rejected

LENGTH_COMMANDS = {
    "string": ("strlen", "bytes"),
    "list": ("llen", "items"),
    "hash": ("hlen", "fields"),
    "set": ("scard", "items"),
    "zset": ("zcard", "items"),
    "stream": ("xlen", "entries"),
}


def escape_glob(value: str) -> str:
    return "".join("\\" + char if char in "*?[]\\" else char for char in value)


def create_client(policy: Policy) -> redis.Redis:
    target = policy.target
    return redis.Redis(
        host=target.host,
        port=target.port,
        db=target.database,
        username=target.username,
        password=os.environ.get(target.password_env) or os.environ["REDIS_PWD"],
        ssl=target.tls,
        ssl_ca_certs=target.ca_file,
        ssl_cert_reqs="required",
        ssl_check_hostname=True,
        decode_responses=False,
        protocol=2,
        max_connections=policy.limits.max_inflight,
        socket_timeout=policy.limits.redis_timeout_seconds,
        socket_connect_timeout=policy.limits.redis_timeout_seconds,
        retry=Retry(NoBackoff(), 0),
        retry_on_error=[],
        health_check_interval=0,
    )


class Reader:
    def __init__(self, client, policy: Policy | None = None):
        self.client = client
        self.policy = policy

    def _check_key(self, key):
        if type(key) is not str or not key or len(key.encode()) > 4096:
            raise Rejected(Code.INVALID_ARGUMENT)
        if self.policy is not None:
            if len(key.encode()) > self.policy.limits.max_key_bytes:
                raise Rejected(Code.INVALID_ARGUMENT)
            if key not in self.policy.allowed_keys and not any(
                key.startswith(prefix) for prefix in self.policy.allowed_prefixes
            ):
                raise Rejected(Code.KEY_SCOPE_DENIED)

    def execute(self, request: str) -> dict[str, Any]:
        try:
            if len(request.encode()) > 32768:
                raise Rejected(Code.INVALID_ARGUMENT)
            args = json.loads(request)
            if type(args) is not dict:
                raise Rejected(Code.INVALID_ARGUMENT)
            operation = args.pop("operation", None)
            if self.policy is not None and operation not in self.policy.tools:
                raise Rejected(Code.TOOL_DENIED)
            if operation == "connection_status" and not args:
                data = {"connected": self.client.ping() is True}
            elif operation == "inspect_key" and set(args) == {"key"}:
                self._check_key(args["key"])
                data = self.inspect(args["key"])
            elif operation == "preview_key" and set(args) == {"key", "limit"}:
                self._check_key(args["key"])
                limit = args["limit"]
                cap = self.policy.limits.preview_bytes if self.policy else 4096
                if type(limit) is not int or not 1 <= limit <= cap:
                    raise Rejected(Code.INVALID_ARGUMENT)
                data = self.preview(args["key"], limit)
            elif operation == "find_keys" and set(args) == {"prefix", "cursor", "count"}:
                prefix, cursor, count = args["prefix"], args["cursor"], args["count"]
                if (
                    type(prefix) is not str
                    or not prefix
                    or type(cursor) is not int
                    or not 0 <= cursor <= 2**64 - 1
                    or type(count) is not int
                    or not 1 <= count <= 100
                ):
                    raise Rejected(Code.INVALID_ARGUMENT)
                if self.policy and (
                    prefix not in self.policy.allowed_prefixes
                    or count > self.policy.limits.scan_count_hint
                ):
                    raise Rejected(Code.KEY_SCOPE_DENIED)
                data = self.scan(prefix, cursor, count)
            else:
                raise Rejected(Code.INVALID_ARGUMENT)
            return {"ok": True, "data": data}
        except Rejected as exc:
            return {"ok": False, "code": exc.code.value}
        except NoPermissionError:
            return {"ok": False, "code": Code.PERMISSION_DENIED.value}
        except ResponseError:
            return {"ok": False, "code": Code.KEY_CHANGED.value}
        except RedisError:
            return {"ok": False, "code": Code.UPSTREAM_UNAVAILABLE.value}
        except (ValueError, TypeError, UnicodeError):
            return {"ok": False, "code": Code.INVALID_ARGUMENT.value}

    def inspect(self, key: str) -> dict:
        kind = self.client.type(key)
        kind = kind.decode("ascii") if isinstance(kind, bytes) else kind
        ttl = self.client.pttl(key)
        command, unit = LENGTH_COMMANDS.get(kind, (None, None))
        length = getattr(self.client, command)(key) if command else None
        return {
            "type": kind,
            "ttl_ms": ttl,
            "length": length,
            "length_unit": unit,
            "atomic": False,
        }

    def preview(self, key: str, limit: int) -> dict:
        kind = self.client.type(key)
        if kind not in ("string", b"string"):
            raise Rejected(Code.UNSUPPORTED_TYPE)
        total = self.client.strlen(key)
        value = self.client.getrange(key, 0, limit - 1)
        try:
            content, encoding = value.decode("utf-8"), "utf-8"
        except UnicodeError:
            content, encoding = base64.b64encode(value).decode("ascii"), "base64"
        return {
            "value": content,
            "encoding": encoding,
            "returned_bytes": len(value),
            "total_bytes_observed": total,
            # 조회 사이 변경을 고려해 한도에 도달하면 보수적으로 일부 결과로 표시한다.
            "truncated": total > len(value) or len(value) == limit,
            "atomic": False,
        }

    def scan(self, prefix: str, cursor: int, count: int) -> dict:
        cursor, values = self.client.scan(
            cursor=cursor, match=escape_glob(prefix) + "*", count=count
        )
        maximum = self.policy.limits.max_key_bytes if self.policy else 512
        keys = []
        dropped = len(values) > 100
        for value in values[:100]:
            if len(keys) >= 100 or len(value) > maximum:
                dropped = True
                continue
            try:
                key = value.decode("utf-8") if isinstance(value, bytes) else value
            except UnicodeError:
                dropped = True
                continue
            if not key.startswith(prefix):
                dropped = True
                continue
            keys.append(key)
        return {
            "cursor": cursor,
            "keys": keys,
            "observed": len(values),
            "truncated": dropped,
        }


def register(client=None):
    from src.common.connection import RedisConnectionManager
    from src.common.server import mcp

    policy = Policy.model_validate_json(os.environ["VALKEEP_POLICY"]) if client is None else None
    client = create_client(policy) if client is None else client
    # 공식 LLEN도 같은 제한된 연결 설정을 사용하게 한다.
    RedisConnectionManager._instance = client
    RedisConnectionManager.get_connection = classmethod(lambda cls, **kwargs: client)
    reader = Reader(client, policy)

    @mcp.tool()
    async def valkeep_read(request: str) -> dict[str, Any]:
        """Valkeep 정책을 통과한 내부 읽기 요청만 처리한다."""
        return reader.execute(request)
