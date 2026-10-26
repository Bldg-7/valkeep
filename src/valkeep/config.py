"""엄격한 읽기 전용 TOML 설정과 최소 자식 환경."""

import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from valkeep.errors import Code, Rejected

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
PublicTool = Literal["inspect_key", "list_length"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Target(FrozenModel):
    id: Identifier
    environment: Literal["local", "dev", "prod"]
    host: Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9.:-]+$")]
    port: Annotated[int, Field(ge=1, le=65535)] = 6379
    database: Annotated[int, Field(ge=0, le=15)] = 0
    username: Annotated[str, Field(min_length=1, max_length=128)]
    password_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]
    tls: bool = True
    ca_file: str | None = None
    readonly_acl_confirmed: bool

    @model_validator(mode="after")
    def check_security(self):
        if not self.readonly_acl_confirmed:
            raise ValueError("전용 ACL 검토 확인이 필요합니다")
        if self.username == "default":
            raise ValueError("전용 ACL 계정이 필요합니다")
        if not self.tls and (
            self.environment != "local" or self.host not in {"127.0.0.1", "::1", "localhost"}
        ):
            raise ValueError("평문 연결은 명시적인 로컬 대상에만 허용합니다")
        return self


class Limits(FrozenModel):
    max_inflight: Annotated[int, Field(ge=1, le=8)] = 1
    requests_per_second: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 1.0
    burst: Annotated[int, Field(ge=1, le=100)] = 1
    max_request_bytes: Annotated[int, Field(ge=1024, le=65536)] = 8192
    max_response_bytes: Annotated[int, Field(ge=512, le=65536)] = 8192
    max_upstream_frame_bytes: Annotated[int, Field(ge=65536, le=4194304)] = 1048576
    max_key_bytes: Annotated[int, Field(ge=1, le=4096)] = 512
    response_timeout_seconds: Annotated[float, Field(gt=0, le=60, allow_inf_nan=False)] = 3.0
    cleanup_timeout_seconds: Annotated[float, Field(gt=0, le=60, allow_inf_nan=False)] = 5.0
    startup_timeout_seconds: Annotated[float, Field(gt=0, le=120, allow_inf_nan=False)] = 15.0


class Policy(FrozenModel):
    policy_version: Literal[1, 2]
    target: Target
    allowed_prefixes: Annotated[tuple[str, ...], Field(strict=False, max_length=100)] = ()
    allowed_keys: Annotated[tuple[str, ...], Field(strict=False, max_length=100)] = ()
    tools: Annotated[tuple[PublicTool, ...], Field(strict=False, min_length=1, max_length=2)]
    limits: Limits = Field(default_factory=Limits)
    audit_path: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def check_scope(self):
        if not self.allowed_prefixes and not self.allowed_keys:
            raise ValueError("최소 하나의 키 범위가 필요합니다")
        for key in (*self.allowed_prefixes, *self.allowed_keys):
            if not key or len(key.encode("utf-8")) > self.limits.max_key_bytes:
                raise ValueError("키 범위가 올바르지 않습니다")
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("도구가 중복되었습니다")
        return self

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class TargetEntry(Target):
    """외부 설정의 한 target을 기존 단일 대상 실행 정책으로 변환한다."""

    allowed_prefixes: Annotated[tuple[str, ...], Field(strict=False, max_length=100)] = ()
    allowed_keys: Annotated[tuple[str, ...], Field(strict=False, max_length=100)] = ()
    tools: Annotated[tuple[PublicTool, ...], Field(strict=False, min_length=1, max_length=2)]
    limits: Limits = Field(default_factory=Limits)
    audit_path: Annotated[str, Field(min_length=1)]

    def as_policy(self) -> Policy:
        values = self.model_dump()
        connection = {name: values.pop(name) for name in Target.model_fields}
        return Policy.model_validate({"policy_version": 2, "target": connection, **values})

    @model_validator(mode="after")
    def check_policy(self):
        self.as_policy()
        return self


class ServerConfig(FrozenModel):
    policy_version: Literal[2]
    targets: Annotated[tuple[TargetEntry, ...], Field(strict=False, min_length=1, max_length=16)]
    audit_path: Annotated[str, Field(min_length=1)]
    max_request_bytes: Annotated[int, Field(ge=1024, le=65536)] = 8192
    max_response_bytes: Annotated[int, Field(ge=16384, le=65536)] = 32768

    @model_validator(mode="after")
    def check_targets(self):
        ids = [target.id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("target ID가 중복되었습니다")
        endpoints = [
            (target.host.lower().rstrip("."), target.port, target.database)
            for target in self.targets
        ]
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("동일 endpoint와 DB를 중복 등록할 수 없습니다")
        paths = [
            os.path.realpath(value)
            for value in [self.audit_path, *(target.audit_path for target in self.targets)]
        ]
        if len(paths) != len(set(paths)):
            raise ValueError("감사 파일은 target 및 라우터별로 분리해야 합니다")
        if any(
            target.limits.max_response_bytes > self.max_response_bytes for target in self.targets
        ):
            raise ValueError("target 응답 상한은 서버 응답 상한 이하여야 합니다")
        return self

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_config(path: Path) -> ServerConfig:
    try:
        if path.stat().st_size > 65536:
            raise ValueError("설정이 너무 큽니다")
        config = ServerConfig.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
        values = config.model_dump()
        base = path.resolve().parent
        for item in [values, *values["targets"]]:
            item["audit_path"] = os.path.abspath(base / item["audit_path"])
        # 정규화 뒤 경로 별칭의 중복도 다시 검사한다.
        return ServerConfig.model_validate(values)
    except (OSError, ValueError, UnicodeError, RuntimeError):
        raise Rejected(Code.CONFIG_INVALID) from None


def upstream_environment(policy: Policy) -> dict[str, str]:
    target = policy.target
    password = os.environ.get(target.password_env)
    if not password or "\x00" in password:
        raise Rejected(Code.CONFIG_INVALID)
    env = {
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONIOENCODING": "utf-8",
        "REDIS_HOST": target.host,
        "REDIS_PORT": str(target.port),
        "REDIS_DB": str(target.database),
        "REDIS_USERNAME": target.username,
        "REDIS_PWD": password,
        "REDIS_SSL": "true" if target.tls else "false",
        "REDIS_SSL_CERT_REQS": "required",
        "REDIS_CLUSTER_MODE": "false",
        "MCP_REDIS_LOG_LEVEL": "CRITICAL",
        "MCP_DOCS_SEARCH_URL": "",
    }
    if target.ca_file:
        env["REDIS_SSL_CA_CERTS"] = target.ca_file
    return env
