# Valkeep

[English](README.md) | **한국어**

**조회 대상·접근 범위·실행 예산을 명시적으로 통제하는 AI 어시스턴트용 읽기 전용 Redis 조회 도구.**

Valkeep은 [공식 Redis MCP 서버](https://github.com/redis/mcp-redis) 앞에서 조회 정책을 적용하는 로컬 [Model Context Protocol(MCP)](https://modelcontextprotocol.io/) 서버다. 여러 Redis target을 지원하며, 임의 Redis 명령을 실행하는 콘솔은 제공하지 않는다.

기본 프리셋은 메타데이터 조회와 연결 점검만 제공한다. String 미리보기와 키 탐색은 필요한 경우 각각 따로 활성화한다.

```text
AI 클라이언트
  → Valkeep: 대상 선택, 도구 권한, 키 범위, 호출 예산 검사
    ├─ catalog-cache → 전용 MCP 프로세스 + 읽기 어댑터 → ACL 계정 → Redis A
    └─ job-queue     → 전용 MCP 프로세스 + 읽기 어댑터 → ACL 계정 → Redis B
```

## 주요 기능

- **다중 target:** 최대 16개의 대상에 프로세스·자격증명·정책·호출 제한·감사 로그를 각각 분리한다.
- **명시적인 대상 선택:** 모든 Redis 조회에 `target_id`가 필요하다. 기본 대상 선택이나 다른 대상으로의 자동 재시도는 없다.
- **메타데이터 중심 기본값:** 값을 가져오지 않고 키 타입·TTL·길이를 확인한다.
- **제한된 String 미리보기:** 전체 값을 가져온 뒤 자르는 대신 `GETRANGE`로 앞부분만 읽는다.
- **예산이 적용된 키 탐색:** 요청당 SCAN 한 번, 일회용 이어받기 토큰, 탐색 세션별 호출·시간·관측 예산을 적용한다.
- **ACL 설정 지원:** Redis 권한을 변경하지 않고 최소 권한안을 생성하고 관리자가 제공한 ACL export를 검사한다.
- **민감정보를 최소화한 감사 로그:** 원본 키·값·비밀번호·탐색 토큰을 감사 로그에 기록하지 않는다.

## 제공 도구

| 도구 | 기능 | 기본 상태 |
|---|---|---|
| `list_targets` | 등록된 target ID·환경·허용 도구·탐색 scope ID 확인 | 제공 |
| `inspect_key` | 타입·밀리초 TTL·타입별 길이 또는 개수 조회 | 활성화 |
| `list_length` | List의 원소 수 조회 | 활성화 |
| `connection_status` | PING으로 연결·인증 점검 | 활성화 |
| `preview_key` | String 값의 앞부분을 바이트 한도 내에서 조회 | 명시적으로 활성화 |
| `find_keys` | 허용 prefix에서 키를 한 페이지씩 탐색 | 명시적으로 활성화 |

`inspect_key`는 String, List, Hash, Set, Sorted Set, Stream의 길이·개수 정보를 지원한다. 길이 조회를 지원하지 않는 모듈 타입은 타입·TTL과 null 길이를 반환한다. 메타데이터는 원자적인 스냅샷이 아니다.

String 미리보기는 기본 1,024바이트이며 최대 4,096바이트까지 설정할 수 있다. UTF-8이 아닌 데이터나 문자 중간에서 끊긴 미리보기는 base64로 반환한다. 원소 개수만으로 바이트 크기를 제한할 수 없으므로 컬렉션 값 미리보기는 지원하지 않는다.

## 요구 환경

- Python 3.12 이상
- macOS 또는 Linux
- [uv](https://docs.astral.sh/uv/)
- Standalone Redis. 생성하는 ACL 권한안은 Redis **7.2 이상** 기준
- 관리자가 권한을 검토한 전용 Redis ACL 계정

Redis Cluster, Valkey 호환성, HTTP 서버 방식, 사용자별 권한 분리는 현재 지원 범위에 포함하지 않는다.

## 빠른 시작

### 1. 설치 및 예시 정책 확인

```bash
git clone https://github.com/Bldg-7/valkeep.git
cd valkeep
uv sync --locked

uv run --locked valkeep --config config/policy.example.toml --check-config
uv run --locked valkeep --config config/policy.example.toml --acl-plan
```

위 검사 명령은 **Redis에 연결하거나 권한을 적용하지 않는다.** 예시에는 설명용 로컬 대상이 들어 있으며, 두 명령을 실행할 때는 자격증명이 필요하지 않다.

### 2. 대상 설정

```bash
cp config/policy.example.toml config/policy.local.toml
```

[설정 예시](config/policy.example.toml)를 승인된 대상에 맞게 수정한다.

1. 각 target의 ID·환경·host·port·username·TLS 설정을 지정한다.
2. AI 클라이언트가 조회해도 되는 키 prefix 또는 정확한 키 이름을 지정한다.
3. 별도의 custom 정책이 필요한 경우가 아니라면 `preset = "production-safe"`를 유지한다.
4. 관리자가 생성된 ACL 권한안을 검토하고 적용하도록 한다.
5. 각 target의 `password_env`가 지정하는 환경변수에 비밀번호를 주입한다. 사용 중인 비밀정보 관리 방식을 이용한다.

비밀번호나 로컬 설정을 커밋하지 않는다. `config/*.local.toml`, `.env*`, `*.jsonl`은 기본적으로 Git에서 제외한다.

`production-safe`의 기본 제한은 target당 초당 MCP 요청 1회, 동시 실행 1개다. 추가 기능이 필요하면 해당 target의 **기존** `[targets.features]` 테이블을 수정한다.

```toml
[targets.features]
value_preview = true
key_discovery = true
```

키 탐색의 기본값은 COUNT 힌트 100, 세션당 최대 10회·60초, 관측 예산 500개, target당 활성 세션 최대 4개다. 관측 수는 한 페이지를 받은 뒤 집계하므로 마지막 페이지에서 예산을 초과할 수 있다. Redis의 `SCAN COUNT`는 힌트이며 정확한 응답 크기 제한이 아니다.

개발·운영 target에는 인증서와 hostname 검증을 유지하는 TLS가 필요하다. 평문 연결은 명시적으로 설정한 local loopback 대상에만 허용한다.

### 3. 연결 점검 및 실행

정책을 검토하고 자격증명을 준비한 뒤 실행한다.

```bash
uv run --locked valkeep --config config/policy.local.toml \
  --check-connections --target catalog-cache

uv run --locked valkeep --config config/policy.local.toml
```

연결 점검은 **실제로 Redis에 접근한다.** `--target`을 생략하면 등록된 모든 target을 점검한다. PING 성공은 연결·인증 확인이며, 계정이 의도한 읽기 전용 권한만 가졌다는 증명은 아니다.

서버 모드에서는 stdio로 MCP 요청을 기다린다. MCP 클라이언트에는 내부 Redis MCP 프로세스가 아니라 Valkeep을 등록한다.

일반적인 `mcpServers` 형식의 클라이언트 설정 예시는 다음과 같다. 두 절대 경로를 실제 경로로 바꾼다.

```json
{
  "mcpServers": {
    "valkeep": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/valkeep",
        "run", "--locked", "valkeep",
        "--config", "/absolute/path/to/valkeep/config/policy.local.toml"
      ]
    }
  }
}
```

비밀번호 환경변수는 MCP 클라이언트의 안전한 환경변수·비밀정보 설정 기능으로 주입한다. 실제 설정 형식은 클라이언트마다 다를 수 있다.

## 도구 호출 예시

아래는 MCP 도구 이름과 인자의 예시이며 셸 명령이 아니다.

```text
list_targets({})
inspect_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
list_length({"target_id": "job-queue", "key": "example:jobs:pending"})
connection_status({"target_id": "catalog-cache"})
preview_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
find_keys({"target_id": "catalog-cache", "scope_id": "prefix-0"})
find_keys({"target_id": "catalog-cache", "scope_id": "prefix-0", "token": "previous_next_token"})
```

미리보기·탐색은 해당 기능이 활성화된 target에서만 사용할 수 있다. `list_targets`에서 사용 가능한 탐색 scope ID를 확인한다. 이어받기 토큰은 일회용이며 다른 target이나 scope에서 재사용할 수 없다. 부분 결과나 잘린 탐색 결과는 명시적으로 표시하며, Valkeep이 자동으로 전체 DB를 순회하지 않는다.

## ACL 권한안 생성 및 검사

```bash
uv run --locked valkeep --config config/policy.local.toml \
  --acl-plan --target catalog-cache

uv run --locked valkeep --config config/policy.local.toml \
  --verify-acl /secure/path/acl-export.json --target catalog-cache
```

- **권한안 생성:** 관리자가 검토할 JSON 인자 목록을 출력한다. `ACL SETUSER`를 실행하거나 비밀번호를 포함하지 않는다. **제안하는 `reset` 규칙은 기존 권한과 비밀번호를 제거하므로** 기존 계정에 무작정 적용하지 않는다.
- **검사:** 관리자가 제공한 `redis-cli --json ACL GETUSER <계정>` export를 설정된 정책과 비교한다. 초과 권한·예상하지 않은 selector·채널·불일치하는 키 범위를 거절한다.
- **한계:** export 검사는 오프라인 검사다. 파일의 출처·최신성이나 현재 실제 계정의 권한을 증명할 수 없다. `readonly_acl_confirmed = true` 역시 자동 검사 결과가 아니라 운영자의 확인 선언이다.

ACL export에는 비밀번호 해시 등 민감정보가 포함된다. 안전하게 취급하고 커밋하지 않는다. Valkeep은 export 원문이나 비밀번호 해시를 출력하지 않는다.

## 안전 모델과 한계

- **쓰기 차단의 최종 경계는 Redis ACL이다.** 프록시도 허용되지 않은 도구·키 접근을 거절하지만, Redis 계정 권한을 직접 생성하거나 강제하지는 않는다.
- **읽기 전용은 무영향을 뜻하지 않는다.** 읽기도 Redis의 CPU·메모리·네트워크를 사용한다. 운영 적용 전 실제 환경을 대표하는 부하에서 제한값을 검증한다.
- **키 탐색은 엄격한 자원 격리가 아니다.** SCAN은 넓은 키 공간을 순회할 수 있고, ACL 키 패턴만으로 키 이름 열거를 격리할 수 없다. 명령별 응답 크기에 엄격한 상한이 필요하면 탐색을 끈다.
- **timeout은 서버 측 취소가 아니다.** 실행 종료 전까지 슬롯을 유지하고 정리 시간이 지나면 멈춘 자식을 종료하지만, Redis에서 이미 실행 중인 명령의 취소를 보장할 수는 없다.
- **예산은 로컬에서 적용한다.** 제한은 target·Valkeep 프로세스별이며, 여러 인스턴스나 같은 물리 Redis의 다른 별칭 사이에서 공유하지 않는다.
- **로컬 MCP 사용자가 접근 권한을 공유한다.** 등록된 모든 target이 해당 사용자에게 제공된다. 다중 사용자 권한 분리 서비스가 아니다.
- **AI 클라이언트에 전달되는 데이터도 보호해야 한다.** 읽기 전용 조회로도 민감정보가 노출될 수 있다. 승인된 키 범위에서만 미리보기·탐색을 활성화하고, 반환 값은 신뢰할 수 없는 데이터로 취급한다.

## 개발 및 테스트

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
uv run --locked pytest -q -m live
uv build
```

테스트에는 단위 테스트, 공식 MCP 프로세스 통합 테스트, 실제 Redis 연동 테스트가 포함된다. live 테스트는 설치된 `redis-server`·`openssl`로 임시 로컬 서버를 랜덤 loopback 포트에 시작한다. 외부 Redis URL은 입력받지 않으며, 생성한 프로세스는 종료 시 정리한다.

필요한 실행 파일이 없으면 live 테스트는 skip된다. 릴리스 검증 환경에서는 실제로 실행됐는지 확인한다. 특정 로컬 Redis 버전에서의 검증이 모든 Redis 배포나 managed service의 호환성을 인증하는 것은 아니다.

공식 Redis MCP 서버 0.5.1, MCP Python SDK 1.30.0, redis-py 8.1.0과 전이 의존성은 `pyproject.toml`과 `uv.lock`으로 고정한다.

## 문서

아래 상세 문서는 현재 한국어로 제공한다.

| 문서 | 내용 |
|---|---|
| [영문 README](README.md) | 영어 소개 및 빠른 시작 |
| [구현·실행 안내](docs/implementation.md) | 설정·프리셋·ACL 작업·도구·한계 |
| [요구사항](docs/requirements.md) | 기능·비기능 요구사항과 수용 기준 |
| [설계](docs/design.md) | 구조·정책·프로세스 수명·탐색 안전성 |
