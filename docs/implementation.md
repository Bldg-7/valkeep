# 초기 구현 상태와 실행 안내

## 1. 구현 범위

현재 단계는 설계의 **단계 B: 최소 프록시**다. 운영 배포 승인이나 운영 부하 검증이 완료된 상태는 아니다.

| 기능 | 상태 |
|---|---|
| Python stdio MCP 프록시 | 구현 |
| 공식 Redis MCP 자식 프로세스 실행 | 구현 |
| 공개 도구 `list_targets`, `inspect_key`, `list_length` | 구현 |
| 다중 target 등록·명시적 라우팅·자격증명 및 장애 격리 | 구현, 최대 16개 |
| 도구 허용 목록·직접 호출 재검증 | 구현 |
| strict TOML 설정·키 prefix/exact key 제한 | 구현 |
| 단일 프로세스의 대상별 rate limit·동시 실행 제한 | 구현 |
| 응답 timeout 이후 실행 슬롯 유지 | 구현 |
| 정리 시간 초과 시 자식 종료·새 호출 차단 | 구현 |
| 요청·업스트림 프레임 크기 검사 | 구현 |
| 결과 계약 검사·민감정보 없는 오류 | 구현 |
| 값·원본 키 없는 JSONL 감사 기록 | 구현 |
| 공식 서버 스키마·출력 계약 통합 테스트 | 구현, Redis 연결부는 테스트 대역 |
| 실제 Redis ACL 적용·검증 | 미수행, 운영자 책임 |
| 실제 Redis 부하·TLS·managed service 검증 | 미수행 |
| SCAN·cursor·탐색 예산 | 미구현, 도구 미노출 및 직접 호출 거절 |
| 값 미리보기·컬렉션 범위 조회 | 미구현, 도구 미노출 및 직접 호출 거절 |
| Redis Cluster·Valkey 호환성 | 미검증 |
| 원격 HTTP·다중 사용자·분산 limiter | 미구현 |

## 2. 구현 선택

- Python 3.12 이상, 현재 개발 테스트는 Python 3.13에서 수행했다.
- POSIX 환경(macOS/Linux)을 대상으로 한다. process group 종료와 파일 접근 권한 검사 때문에 Windows는 지원하지 않는다.
- `mcp==1.30.0`, `redis-mcp-server==0.5.1`을 고정했다. 공식 Redis MCP의 SDK 의존성이 `mcp<2`이므로 SDK 2.x를 사용하지 않는다.
- 전이 의존성 및 패키지 해시는 `uv.lock`에 고정한다. 실행·테스트 시 `--locked`를 사용한다.
- 공식 서버의 main을 격리된 Python 자식에서 실행한다. CLI 기본값이 환경변수의 연결 설정을 덮어쓰는 경로를 피하기 위해 공식 모듈의 `main()`을 호출한다.
- target마다 별도 수명 태스크와 공식 MCP 자식 프로세스를 만든다. 자식 프로세스는 자신의 대상 설정과 비밀번호만 명시적으로 전달받는다. 부모의 임의 환경변수와 Python 경로를 상속하지 않고 dotenv 자동 로드를 비활성화한다.
- 업스트림 stderr는 민감정보 유출 방지를 위해 폐기한다. 이는 상세 장애 분석 정보가 줄어드는 선택이며, 프록시 감사 로그로 종료 상태를 확인한다.
- resources, prompts, sampling, elicitation 등은 클라이언트와 업스트림 사이에서 중계하지 않는다.

## 3. 설치 및 연결 없는 검사

저장소 또는 구현 worktree 루트에서 실행한다.

```bash
uv sync --locked
uv run --locked valkeep --config config/policy.example.toml --check-config
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
```

`--check-config`는 TOML 정책만 검증한다. 자격증명 존재, Redis 연결, ACL 권한은 검사하지 않는다.

데이터 조회 테스트는 합성 데이터와 테스트 대역을 사용한다. 실제 공식 Redis MCP의 데이터 도구를 실행하는 테스트는 Redis 연결부를 대체하고 네트워크 연결을 금지한다. 별도로 실제 CLI와 공식 업스트림의 초기화·도구 목록만 확인하는 테스트가 있으며, 이 테스트도 Redis 데이터 도구는 호출하지 않는다. 테스트 통과가 실제 Redis 운영 안전성을 증명하지는 않는다.

## 4. 연결 전 설정

1. 개발/운영 환경과 Redis 버전·topology를 확인한다.
2. `config/policy.example.toml`을 `config/policy.local.toml`로 복사한다.
3. `[[targets]]`마다 고유 ID, host·port·database·username·TLS, 허용 도구·키 범위·감사 파일을 명시한다.
4. 전용 계정의 Redis ACL을 운영자가 검토한다.
5. 감사 파일을 저장할 디렉토리를 준비한다.
6. 비밀번호는 각 `targets[*].password_env`가 가리키는 환경변수에 secret manager 등으로 주입한다.

`*.local.toml`, `.env*`, `*.jsonl`은 기본적으로 Git에서 제외한다. 설정 파일에는 비밀번호 값 자체를 넣지 않는다.

### ACL 요구

현재 도구가 실행하는 데이터 명령은 다음과 같다.

| 공개 도구 | 실제 데이터 명령 |
|---|---|
| `inspect_key` | `TYPE`, `TTL` |
| `list_length` | `LLEN` |

연결 준비에 필요한 권한은 Redis/client 버전에 따라 별도로 확인해야 한다. `+@read`를 광범위하게 허용하는 대신 실제 필요한 명령만 허용한다.

`readonly_acl_confirmed = true`는 운영자가 검토했다는 선언이다. 프록시가 ACL을 자동 검증하거나 생성하는 기능은 아니다. 잘못 구성된 ACL의 쓰기 차단까지 보증하지 않는다. 실제 운영 키에 쓰기 요청을 보내 검증하지 않는다.

### TLS 및 대상 제한

- `environment = "dev"` 또는 `"prod"`이면 TLS를 요구한다.
- 평문은 `environment = "local"`이고 host가 loopback인 경우만 허용한다.
- TLS 인증서 검증을 끄는 옵션은 없다. 필요한 경우 `ca_file`을 설정한다.
- `default` 계정은 거절한다.
- 키 prefix는 glob이 아닌 literal 문자열이다. 예를 들어 `safe:*`는 별표 자체를 포함하는 prefix다.
- 바이너리 키는 지원하지 않는다. 유효한 UTF-8 키와 설정된 바이트 상한을 검사한다.

### 다중 target 설정과 마이그레이션

- 외부 설정은 `policy_version = 2`, `[[targets]]` 목록을 사용한다. [두 target 설정 예시](../config/policy.example.toml)를 참고한다.
- 기존 v1 설정은 자동 변환하지 않는다. 기존 `[target]`의 연결 필드와 최상위 키 범위·도구·감사 경로를 하나의 `[[targets]]` 안으로 옮긴다.
- 기존 `[limits]`는 `[targets.limits]`로 옮긴다. 최상위에는 라우터 `audit_path`, 공개 입력 `max_request_bytes`, 전체 결과 `max_response_bytes`를 둔다.
- 감사 파일은 라우터와 각 target별로 달라야 한다. 중복 ID와 같은 host/port/database 조합도 거절한다.
- 동일 Redis를 가리키는 서로 다른 DNS 별칭이나 서로 다른 DB의 물리 자원 공유까지 감지하지는 못한다. 이런 경우 합산 부하는 운영자가 고려해야 한다.
- target이 하나여도 모든 데이터 조회에서 `target_id`가 필수다. 생략은 `INVALID_ARGUMENT`, 미등록 ID는 `TARGET_UNKNOWN`이다.
- 현재 MCP 연결 사용자는 등록된 모든 target을 볼 수 있다. 다중 target은 사용자별 권한 분리 기능이 아니다.

도구 호출 예:

```text
list_targets({})
inspect_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
list_length({"target_id": "job-queue", "key": "example:jobs:pending"})
```

`list_targets`는 ID·환경·허용 도구·프록시 상태만 반환하며 연결 주소·계정·자격증명·키 prefix는 노출하지 않는다. `contract_verified` 상태는 MCP 계약 검증이 끝났다는 뜻이지 Redis 연결이나 ACL이 정상이라는 뜻은 아니다.

## 5. 실행

검토한 설정과 비밀번호 환경변수가 준비된 후에만 실행한다.

```bash
uv run --locked valkeep --config config/policy.local.toml
```

stdio 서버이므로 정상 실행 시 일반 안내 문구를 stdout으로 출력하지 않고 MCP 클라이언트의 초기화 요청을 기다린다. MCP 클라이언트에는 Valkeep만 등록하고 공식 Redis MCP를 별도로 등록하지 않는다. 실제 클라이언트 설정은 이 구현에서 변경하지 않았다.

시작 시 모든 target의 설정·자격증명·업스트림 계약을 확인하며 하나라도 실패하면 시작한 대상을 정리하고 전체 시작을 거절한다. 실행 중에는 실패한 target만 차단하고 다른 정상 target은 유지한다. 공식 서버의 도구 계약을 확인하지만 Redis 연결은 지연 생성된다. **MCP 초기화 성공은 Redis 연결·ACL 검사 성공이 아니다.**

## 6. 제한의 의미

- `requests_per_second`: MCP 업스트림 호출 기준이다. Redis 명령 수나 CPU 사용률의 정확한 상한이 아니다.
- `max_inflight`: 응답 대기 요청 수가 아니라 아직 종료되지 않은 업스트림 호출 수다.
- `response_timeout_seconds`: 클라이언트 응답 대기의 상한이다.
- `cleanup_timeout_seconds`: 응답 제한 시간 이후에도 호출이 남아 있으면 추가로 허용하는 정리 시간이다. 초과 시 해당 target의 자식을 종료하고 그 target만 재시작 전까지 사용 불가 상태로 만든다.
- 최상위 `max_request_bytes`: 공개 stdio 입력의 줄 단위 바이트 상한이다. 잘못된/초과 프레임은 세션을 닫을 수 있다. target의 같은 이름 설정은 해당 조회 인자를 직렬화한 크기 및 업스트림 전송 제한에 사용한다.
- `max_response_bytes`: 도구 결과 직렬화 크기 상한이다. 초기화·도구 목록 등 전체 MCP 프레임 상한과는 다르다.
- `max_upstream_frame_bytes`: 업스트림 프레임을 JSON으로 파싱하기 전 적용하는 제한이다. 이미 Redis에서 전송된 데이터의 비용은 되돌리지 못한다.
- 각 target은 독립적인 limiter를 갖고 서로의 토큰·실행 슬롯을 소비하지 않는다. limiter는 프로세스 메모리에 있다. 동일 대상을 여러 인스턴스로 연결하면 합산 제한이 되지 않는다.
- Valkeep 자체 재시도·자동 재시작은 없다. 공식 서버와 Redis client 내부의 연결·재시도 동작까지 명령별로 제어하는 기능은 아직 없다.

Redis 실행 중인 명령을 timeout이나 자식 종료로 반드시 취소할 수 있는 것은 아니다.

## 7. 감사 로그

- 감사 파일은 owner-only 권한으로 생성한다. symlink, 일반 파일이 아닌 대상, 다른 소유자, group/other 접근 권한이 있는 기존 파일은 거절한다.
- 요청 ID, 대상 식별자, 정책 fingerprint, 공개 도구명, 상태 코드, 경과 시간, 결과 크기를 기록한다.
- 허용 목록 밖의 도구 이름도 그대로 기록하지 않는다.
- 원본 키·값·비밀번호·업스트림 오류 원문을 기록하지 않는다.
- target 감사 쓰기 실패 시 해당 target의 추가 조회를 차단한다. 공통 라우터 감사 실패는 모든 target의 새 조회를 차단한다. 결과 완료 감사가 실패하면 조회가 이미 수행되었더라도 성공으로 보고하지 않는다.
- 현재 로그는 로컬 파일 append다. 매 레코드 fsync, 변조 방지 저장소, 로그 회전·중앙 수집은 구현하지 않았다.

## 8. 다음 단계

1. 승인된 테스트 Redis에서 실제 ACL·연결 준비 권한·TLS 검증.
2. 목표 데이터 분포에서 latency·CPU·네트워크 영향 측정.
3. 키 탐색이 필요한지 결정한 후 SCAN 토큰과 누적 예산 구현.
4. 값 조회 필요 시 데이터 타입별 안전한 대체 조회 설계.
5. 운영 도입 전 프로세스 감시·감사 로그 보관·자격증명 회전 절차 확정.
