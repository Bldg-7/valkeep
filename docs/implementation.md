# 구현 상태와 실행 안내

## 1. 현재 범위

Valkeep은 여러 standalone Redis를 대상으로 하는 **로컬 stdio 읽기 전용 진단 MCP**다. 공식 Redis MCP 0.5.1을 재사용하고, 안전한 조회가 필요한 기능은 그 자식 프로세스에 Valkeep 읽기 어댑터를 등록해 처리한다.

| 기능 | 상태 |
|---|---|
| 다중 target, 명시적 target_id, 대상별 연결·예산·감사·장애 격리 | 구현, 최대 16개 |
| production-safe 프리셋과 기능별 명시적 활성화 | 구현 |
| 타입·밀리초 TTL·타입별 길이/개수 통합 조회 | 구현 |
| List 길이 조회 | 구현 |
| 제한된 String 미리보기 | 구현, 기본 비활성화 |
| 허용 prefix별 키 탐색·일회용 토큰·누적 예산 | 구현, 기본 비활성화 |
| PING 연결·인증 점검 | 구현 |
| ACL 권한안 생성 | 구현, 적용하지 않음 |
| 관리자 ACL GETUSER JSON의 정책 일치 검사 | 구현, 오프라인 검사 |
| 실제 Redis의 ACL·평문·TLS·큰 값·탐색 연동 테스트 | 격리된 로컬 Redis로 검증 |
| 실제 운영 데이터 분포에서의 부하·지연 허용치 검증 | 운영 도입 전 별도 필요 |
| Hash·List·Set 등 컬렉션 값 미리보기 | 지원하지 않음: 개수만으로 바이트 크기를 제한할 수 없음 |
| Cluster·Valkey·managed service별 호환성 | 지원 검증 범위 밖 |
| HTTP 중앙 서비스·사용자별 권한·분산 limiter | 현재 제품 범위 밖 |

## 2. 설치 및 연결 없는 검사

Python 3.12 이상, POSIX 환경(macOS/Linux)을 대상으로 한다. 개발 검증 환경은 Python 3.13이다. 실행 파일과 패키지 해시는 `uv.lock`으로 고정한다.

```bash
uv sync --locked
uv run --locked valkeep --config config/policy.example.toml --check-config
uv run --locked valkeep --config config/policy.example.toml --acl-plan
```

두 명령 모두 Redis에 접속하지 않으며 비밀번호 환경변수가 없어도 실행할 수 있다. `--acl-plan`도 계정이나 권한을 변경하지 않는다.

## 3. 초보자를 위한 설정 순서

1. [설정 예시](../config/policy.example.toml)를 `config/policy.local.toml`로 복사한다.
2. 각 target의 ID·환경·주소·계정·TLS를 명시한다.
3. 서비스 담당자에게 확인한 키 prefix 또는 정확한 키 이름을 지정한다. MCP가 민감정보가 없는 범위를 추측하지 않는다.
4. `preset = "production-safe"`를 사용한다. 기본값은 메타데이터와 연결 점검만 허용한다.
5. 필요한 경우에만 `features.value_preview` 또는 `features.key_discovery`를 켠다.
6. ACL 권한안을 생성해 관리자에게 검토·적용을 요청한다. 실제 비밀번호는 별도의 보안 경로로 설정한다.
7. `password_env`에 지정한 환경변수로 각 계정 비밀번호를 주입한다.
8. 승인된 대상에만 연결 점검을 수행한 뒤 MCP 클라이언트에 Valkeep을 등록한다.

설정 예시의 host·port는 로컬 설명용이며 실제 사내/운영 주소가 아니다. 개발·운영 환경에서는 TLS를 요구하고 인증서와 hostname 검증을 유지한다. 평문은 명시적인 local loopback 대상에만 허용한다. 환경변수·dotenv 파일·접속 URL을 임의로 상속하지 않는다.

### 프리셋과 제한값

`production-safe`의 기본 도구는 `inspect_key`, `list_length`, `connection_status`다. String 미리보기와 키 탐색은 각각 독립적으로 켠다.

- 기본 호출률: target당 초당 1회, 동시 실행 1개.
- 기본 String 미리보기: 앞부분 1,024바이트, 설정 가능한 최대 4,096바이트.
- 기본 탐색: SCAN COUNT 힌트 100, 탐색 세션당 최대 10회·60초, 관측 예산 500개, 동시 세션 최대 4개. 관측 수는 한 페이지를 받은 뒤 집계하므로 마지막 페이지에서 예산을 초과할 수 있다.
- 프리셋에서는 호출률·동시 실행·탐색 예산을 일정 수준 이상 높이지 못하게 한다.
- 명시적 `tools` 설정은 고급 사용자용 custom 경로다. `features`와 함께 사용하지 않는다. 기존 v2처럼 preset 없이 tools를 명시하면 custom으로 해석한다. production-safe와 tools를 함께 명시하면 거절한다.
- 어떤 설정에서도 임의 Redis 명령 실행 도구는 제공하지 않는다.

이 값들은 보수적인 정책 기본값이지 운영 서비스에 무영향을 보장하는 성능 기준은 아니다.

### 이전 버전에서 변경된 점

v2 다중 target 설정은 유지한다. 기존처럼 `tools`를 명시하고 preset을 생략하면 custom으로 해석한다. `production-safe`를 명시할 때는 tools 대신 features를 사용한다.

0.2의 `inspect_key.ttl_seconds`는 0.3에서 **밀리초 단위 `ttl_ms`**로 변경되었다. 응답을 프로그램에서 처리하던 사용자는 필드명과 단위를 함께 변경해야 한다. 타입별 `length`, `length_unit`, `atomic`도 추가되었다.

## 4. 공개 도구 사용

모든 Redis 조회에는 `target_id`가 필요하다. 기본 target이나 다른 대상으로의 자동 재시도는 없다.

```text
list_targets({})
inspect_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
list_length({"target_id": "job-queue", "key": "example:jobs:pending"})
connection_status({"target_id": "catalog-cache"})
preview_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
find_keys({"target_id": "catalog-cache", "scope_id": "prefix-0"})
find_keys({"target_id": "catalog-cache", "scope_id": "prefix-0", "token": "직전_응답의_next_token"})
```

미리보기와 탐색은 활성화한 target에서만 사용할 수 있다. `list_targets`의 `discovery_scopes`에서 `prefix-0` 등의 사용 가능한 scope ID를 확인한다. scope는 설정의 허용 prefix 순서에 대응한다. 목록에 원본 prefix·host·계정·자격증명은 노출하지 않는다.

### inspect_key

타입, `ttl_ms`, 길이와 단위를 반환한다.

| 타입 | 길이 명령 | 단위 |
|---|---|---|
| String | STRLEN | bytes |
| List | LLEN | items |
| Hash | HLEN | fields |
| Set | SCARD | items |
| Sorted Set | ZCARD | items |
| Stream | XLEN | entries |

없는 키는 `type=none`, `ttl_ms=-2`다. 지원하지 않는 모듈 타입은 타입·TTL만 반환하고 길이는 null이다. TYPE·PTTL·길이 조회는 서로 다른 시점의 명령이므로 `atomic=false`를 명시한다. 조회 중 타입 변경으로 명령이 실패하면 `KEY_CHANGED`로 보고한다.

### preview_key

String에만 `GETRANGE 0 N-1`을 사용한다. 전체 GET 후 결과를 자르는 방식이 아니다. 반환 바이트 수, 관측된 전체 길이, 일부 결과 여부를 제공한다.

UTF-8로 해석할 수 없는 데이터나 문자 중간에서 끊긴 데이터는 base64로 반환한다. 바이트 한도에 도달하면 보수적으로 truncated=true로 표시한다. 메타데이터와 내용은 원자적 스냅샷이 아니다. 반환 값은 신뢰할 수 없는 사용자 데이터이며 명령이나 정책으로 해석하지 않는다.

### find_keys

- 호출당 SCAN은 한 번만 수행하며 자동으로 전체 순회하지 않는다.
- 사용자는 pattern·cursor·COUNT를 직접 지정할 수 없다. 서버가 literal prefix를 escape해 패턴을 생성한다.
- 다음 페이지는 추측 불가능한 일회용 토큰으로만 조회한다. 토큰은 target·scope·현재 프로세스에 묶이며 재사용·교차 대상 사용을 거절한다.
- 빈 페이지와 중복 키도 호출·관측 예산을 소비한다.
- 결과 크기 초과, 큰 키·바이너리 키 생략, 시간·호출·관측 예산 종료 시 부분 결과임을 명시한다.
- 이미 얻은 키를 응답 제한 때문에 버리면 탐색을 종료하고 next_token을 반환하지 않는다.
- Redis SCAN COUNT는 정확한 개수·바이트 상한이 아니다. 한 페이지가 Redis/클라이언트에 주는 비용까지 엄격하게 제한해야 하면 키 탐색을 끈다.
- SCAN은 키 prefix에 관계없이 넓은 키 공간을 순회할 수 있다. ACL 키 패턴만으로 키 이름 열거를 격리할 수는 없다.

## 5. ACL 생성과 검증

```bash
uv run --locked valkeep --config config/policy.local.toml --acl-plan --target catalog-cache
uv run --locked valkeep --config config/policy.local.toml \
  --verify-acl /secure/path/acl-export.json --target catalog-cache
```

`--acl-plan`은 관리자 검토용 JSON 인자 목록을 출력한다. CLIENT SETINFO를 지원하는 Redis 7.2 이상을 기준으로 `-@all`, 필요한 개별 명령, `%R~` 읽기 키 패턴을 사용한다. 큰 카테고리 권한을 통째로 열지 않는다. 연결 준비용 `CLIENT SETINFO`, DB가 0이 아닌 경우 `SELECT`를 포함한다.

**주의: 출력되는 reset은 기존 권한과 비밀번호를 제거한다. 기존 계정에 무작정 적용하지 않는다.** 생성된 목록에는 비밀번호가 없으므로 관리자가 별도로 설정해야 인증할 수 있다. Valkeep은 관리자 자격증명을 받거나 ACL SETUSER를 실행하지 않는다.

`--verify-acl`은 관리자가 신뢰할 수 있는 경로로 확보한 `redis-cli --json ACL GETUSER <계정>` 결과를 검사한다. 계정 활성화·비밀번호 존재·명령 목록·읽기 키 패턴을 비교하고, 추가 selector·채널·광범위 카테고리·추가 쓰기 권한을 거절한다. 결과만 출력하며 export의 비밀번호 해시나 원문은 출력하지 않는다. export 파일은 민감한 자료로 취급하고 안전하게 보관·폐기한다.

검증 결과는 **입력한 export가 설정과 일치한다는 의미**다. 파일의 출처·최신성이나 현재 실제 계정의 권한을 증명하지 않는다. `readonly_acl_confirmed=true`도 여전히 관리자 검토 선언이다. 운영 키에 쓰기 시도를 보내 권한을 검사하지 않는다.

공백·제어문자가 포함된 키 범위는 ACL export 문자열 해석이 모호하므로 자동 권한안/검증을 거절한다. 메타데이터 조회의 키 인자 자체와는 별개 제한이다.

## 6. 연결 점검 및 실행

```bash
uv run --locked valkeep --config config/policy.local.toml \
  --check-connections --target catalog-cache
uv run --locked valkeep --config config/policy.local.toml
```

연결 점검은 지정한 target에만 PING을 보내며 실제 네트워크 접근이다. target을 지정하지 않으면 등록된 모든 target을 점검한다. 연결·인증 실패 시 비밀번호나 원본 오류 없이 안전한 상태 코드와 nonzero 종료 코드를 반환한다. PING 성공은 읽기 전용 ACL 전체 검증을 의미하지 않는다.

정상 stdio 실행은 stdout에 안내 문구를 출력하지 않고 MCP 요청을 기다린다. 시작 시 모든 target의 설정·자격증명·업스트림 계약을 확인하며, 하나라도 실패하면 시작한 자식을 정리한다. Redis 연결은 요청 시 생성되므로 `contract_verified`는 Redis 연결 정상 상태가 아니다.

실행 중 대상별 지연·장애·감사 실패를 분리한다. 공통 라우터 감사 실패는 모든 target을 차단한다. 초기화 이후 임의 대상 추가, 연결 설정 변경, 자동 권한 확대·재시작은 제공하지 않는다.

## 7. 실행 제한과 감사

- 호출률은 MCP 호출 기준이다. 메타데이터 조회 한 번이 여러 Redis 명령을 사용할 수 있으므로 Redis CPU나 명령 수의 정확한 상한은 아니다.
- 각 target의 Redis client는 socket timeout을 설정하고 자동 재시도를 비활성화한다.
- 응답 timeout 이후에도 실제 실행 종료 전까지 슬롯을 유지한다. 정리 제한 시간을 넘으면 해당 자식을 종료하고 그 target을 차단한다.
- timeout이나 프로세스 종료가 Redis의 실행 중인 명령을 반드시 취소하는 것은 아니다.
- target별 limiter는 프로세스 메모리에 있다. 여러 Valkeep 인스턴스, 다른 DNS 별칭, 같은 물리 Redis의 다른 DB까지 합산 제한하지는 않는다.
- 감사 파일은 owner-only이며 symlink·공유 권한 파일을 거절한다. 원본 키·값·비밀번호·탐색 토큰을 기록하지 않는다.
- 감사 기록은 로컬 파일 append다. 매 레코드 fsync·변조 방지 저장·로그 회전·중앙 수집은 별도 운영 구성이다.

## 8. 테스트와 운영 도입 기준

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
uv run --locked pytest -q -m live
uv build
```

live 테스트는 설치된 `redis-server`와 `openssl`을 사용해 임시 디렉토리·랜덤 loopback 포트에 서버와 계정을 새로 만든다. 외부 Redis URL을 입력받지 않으며 종료 시 생성한 프로세스를 정리한다. 도구가 없으면 해당 테스트는 skip되므로, 배포 검증 환경에서는 Redis·OpenSSL을 설치하고 live 테스트가 실행됐는지 확인해야 한다.

로컬 Redis 8.10.1에서 평문·TLS, 주요 데이터 타입, 2MB String의 제한된 미리보기, 바이너리 값, 키 탐색, ACL 기반 쓰기·관리·범위 밖 접근 거절, 과도한 ACL export 거절을 검증했다. 이것은 모든 Redis 7.2+ 배포와 managed service를 인증한 결과가 아니다.

운영 도입 전에는 실제 버전·인증 방식·데이터 분포와 유사한 환경에서 지연시간·CPU·네트워크 영향을 측정하고, 허용 기준·감사 보존·자격증명 회전 절차를 확정해야 한다. 실제 운영 Redis에는 이번 구현 과정에서 접속하지 않았다.
