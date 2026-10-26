# Valkeep

여러 Redis를 AI 클라이언트에서 조사할 때 **읽기 전용 권한·조회 범위·호출 예산을 통제하는 로컬 MCP**.

기본 프리셋은 메타데이터만 조회한다. 값 미리보기와 키 탐색은 필요할 때만 따로 켠다. 임의 Redis 명령 실행 기능은 제공하지 않는다.

```text
AI 클라이언트
  → Valkeep: target_id·도구·키 범위·호출 예산 검사
    ├─ catalog-cache → 전용 공식 MCP + 안전 조회 어댑터 → ACL 계정 → Redis A
    └─ job-queue     → 전용 공식 MCP + 안전 조회 어댑터 → ACL 계정 → Redis B
```

## 제공 기능

| 도구 | 기능 | 기본 상태 |
|---|---|---|
| `list_targets` | 등록 대상·환경·허용 도구·탐색 scope 확인 | 제공 |
| `inspect_key` | 타입·밀리초 TTL·타입별 길이/개수 | 제공 |
| `list_length` | List 원소 수 | 제공 |
| `connection_status` | PING 연결·인증 점검 | 제공 |
| `preview_key` | String 앞부분의 제한된 바이트 조회 | 명시적으로 활성화 |
| `find_keys` | 허용 prefix별 한 페이지 탐색·일회용 토큰·누적 예산 | 명시적으로 활성화 |

모든 Redis 조회는 `target_id`를 명시해야 한다. 대상 생략·미등록 ID·허용 범위 밖 접근을 거절하며 다른 대상으로 자동 재시도하지 않는다.

## 시작

Python 3.12+, macOS/Linux, uv가 필요하다.

```bash
uv sync --locked
uv run --locked valkeep --config config/policy.example.toml --check-config
uv run --locked valkeep --config config/policy.example.toml --acl-plan
```

위 명령은 Redis에 접속하거나 권한을 변경하지 않는다. [설정 예시](config/policy.example.toml)를 복사한 뒤 대상·계정·허용 키 범위를 지정하고, 관리자가 ACL을 적용한 다음 사용한다.

```text
list_targets({})
inspect_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
preview_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
find_keys({"target_id": "catalog-cache", "scope_id": "prefix-0"})
```

미리보기와 탐색은 target의 `features.value_preview`, `features.key_discovery`를 각각 true로 설정해야 사용할 수 있다. 프리셋은 `production-safe`이며, 고급 사용자는 명시적인 `tools`와 제한값을 지정할 수 있다.

## 안전 경계

- **변경 방지:** 프록시는 쓰기 도구를 거절한다. 실제 계정 권한은 Redis ACL로 제한해야 한다.
- **부하 완화:** target별 호출률·동시 실행·메시지 크기·탐색 호출/시간/관측 예산을 적용한다. 큰 값을 GET으로 읽은 뒤 잘라내지 않는다.
- **대상 격리:** 최대 16개의 target에 각각 프로세스·자격증명·정책·감사 로그를 분리한다.
- **비밀정보 최소화:** 로그에 원본 키·값·비밀번호·탐색 토큰을 남기지 않는다.
- **명시적 한계:** 읽기도 Redis 자원을 소비한다. SCAN COUNT는 정확한 바이트 상한이 아니며 운영 무영향을 보장하지 않는다.
- **권한 검증 한계:** ACL 계획은 자동 적용하지 않는다. 관리자 export 검증은 입력 파일의 정책 일치 검사이며 현재 계정의 권한을 실시간 증명하지 않는다.

현재 범위는 standalone Redis용 로컬 stdio다. HTTP 중앙 서비스·사용자별 권한·Cluster·Valkey 인증 지원은 포함하지 않는다.

## 검증

```bash
uv run --locked pytest -q
uv run --locked pytest -q -m live
uv run --locked ruff check .
uv build
```

단위·공식 MCP 통합 테스트와 함께, 설치된 `redis-server`·`openssl`로 임시 로컬 서버를 시작해 실제 ACL·TLS·큰 값·탐색을 검증한다. 테스트는 외부 Redis URL을 받지 않는다. 관련 실행 파일이 없으면 live 테스트는 skip되므로 배포 검증에서는 실행 여부를 확인한다.

실제 운영 환경의 부하 허용치·서비스별 민감정보 범위는 별도 확인이 필요하다. 운영 Redis에는 개발 과정에서 접속하지 않았다.

## 문서

| 문서 | 내용 |
|---|---|
| [구현·실행 안내](docs/implementation.md) | 프리셋·설정·ACL 생성/검증·연결 점검·도구·한계 |
| [요구사항](docs/requirements.md) | 목적, 기능·비기능 요구사항, 수용 기준 |
| [설계](docs/design.md) | 구조, 정책, 수명 관리, 조회 및 탐색 안전성 |

공식 Redis MCP 0.5.1, MCP SDK 1.30.0, redis-py 8.1.0과 전이 의존성은 `uv.lock`으로 고정한다. 조회 어댑터를 추가해도 공식 서버의 다른 도구가 자동 노출되지는 않는다.
