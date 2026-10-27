# 구현 상태와 실행 안내

## 1. 현재 범위

Valkeep 0.4는 **Valkey를 기본 대상으로 하는 로컬 stdio 읽기 전용 MCP**다. 공식 Valkey Python client(`valkey==6.2.0`)와 target별 Valkeep 전용 worker를 사용한다. Redis 공식 MCP와 redis-py 의존성은 제거했다.

| 기능 | 상태 |
|---|---|
| 다중 target·명시적인 target_id·연결·예산·감사·장애 격리 | 최대 16개 |
| production-safe 프리셋·기능별 활성화 | 구현 |
| 타입·밀리초 TTL·타입별 길이/개수 | 구현 |
| List 길이·PING 연결 점검 | 구현 |
| 제한된 String 미리보기·탐색 예산 및 일회용 토큰 | 구현, 각각 기본 비활성화 |
| ACL 권한안 생성·관리자 export 오프라인 검사 | 구현, 자동 적용하지 않음 |
| Valkey 엔진·버전·topology 확인 | 인증 후 HELLO 2로 매 연결 검사 |
| Valkey 8.1.6·9.1.2 평문·TLS 연동 테스트 | 고정 Docker 이미지로 검증 |
| Redis 호환 모드 회귀 테스트 | engine=redis, 로컬 Redis 8.10.1로 검증 |
| 운영 데이터 분포에서의 부하·지연 허용치 검증 | 운영 도입 전 별도 필요 |
| Hash·List·Set 등 컬렉션 값 미리보기 | 미지원, 개수만으로 값 바이트 크기를 제한할 수 없음 |
| Cluster·Sentinel·HTTP·다중 사용자·분산 limiter | 현재 범위 밖 |
| 관리형 서비스 인증·호환성 | 서비스별 추가 검증 필요 |

최소 버전 정책은 Valkey 8.1, Redis 호환 모드는 7.2다. 최소 버전 이상이라는 이유만으로 모든 배포와 미래 버전의 호환성을 보장하지 않는다.

## 2. 설치 및 연결 없는 검사

Python 3.12+, POSIX(macOS/Linux), uv를 사용한다. 실행 의존성과 배포 파일 해시는 uv.lock으로 고정한다.

```bash
uv sync --locked
uv run --locked valkeep --config config/policy.example.toml --check-config
uv run --locked valkeep --config config/policy.example.toml --acl-plan
```

두 명령은 서버에 연결하지 않고 자격증명 없이 실행할 수 있다. ACL 계획도 자동 적용하지 않는다.

## 3. 설정 및 0.3에서의 마이그레이션

1. [예시](../config/policy.example.toml)를 config/policy.local.toml로 복사한다.
2. target마다 고유 ID, engine, 환경, 주소, 전용 계정, TLS를 지정한다.
3. 서비스 담당자가 승인한 키 prefix 또는 정확한 키 이름을 지정한다.
4. production-safe 프리셋을 사용하고 필요한 경우에만 features를 켠다.
5. 관리자가 ACL 권한안을 검토·적용하고 비밀번호를 별도 보안 경로로 설정한다.
6. target.password_env가 지정한 환경변수로 비밀번호를 주입한다.
7. 승인된 target에만 연결 점검을 한 뒤 MCP 클라이언트에 Valkeep을 등록한다.

### 엔진 설정

```toml
# [[targets]]의 연결 설정 중 일부
engine = "valkey"
```

기본값은 valkey다. **기존 Redis target은 engine="redis"를 명시해야 한다.** 설정과 실제 서버의 엔진이 다르면 ENGINE_MISMATCH로 거절한다. 엔진은 자동 fallback 대상이 아니다.

각 연결은 공식 client의 TLS·인증·DB 선택 이후 HELLO 2를 실행한다. 응답의 server·version·proto·mode를 검증하고, 잘못된 엔진·최소 버전 미달·standalone이 아닌 연결을 데이터 조회 전에 거절한다. 이 검사는 HELLO 응답을 제공하는 서버를 신뢰하는 모델이며 TLS 인증서 검증을 대체하지 않는다.

소켓 제한 설정은 socket_timeout_seconds로 변경했다. 이전 입력 이름 redis_timeout_seconds도 마이그레이션용으로 허용하지만 두 이름을 동시에 지정하면 거절한다.

- policy_version=2와 [[targets]] 구조는 유지한다.
- 기존처럼 preset 없이 tools를 명시하면 custom으로 처리한다.
- production-safe와 tools를 동시에 지정하지 않는다. 프리셋에서는 features를 사용한다.
- inspect_key의 TTL 필드는 0.3부터 밀리초 단위 ttl_ms다.
- 프로젝트가 지정하는 환경변수 이름은 자유롭게 유지할 수 있다. 내부 worker에는 선택한 비밀번호만 VALKEEP_PASSWORD로 전달한다.

### 보안 설정

개발·운영 대상은 TLS와 인증서·hostname 검증이 필수다. 평문은 environment=local이며 loopback host인 경우만 허용한다. default 계정은 거절한다.

target ID·endpoint/DB·감사 파일 경로의 중복을 검사한다. 다른 DNS 별칭이나 다른 logical DB가 같은 물리 서버를 공유하는지는 자동 감지하지 않는다.

production-safe 기본값은 target당 초당 요청 1회·동시 실행 1개다. 미리보기는 기본 1,024바이트, 최대 4,096바이트다. 탐색 기본값은 COUNT 힌트 100·10회·관측 예산 500개·60초·활성 세션 최대 4개다. 이 수치는 운영 무영향 보장이 아니라 보수적인 정책 기본값이다.

## 4. 공개 도구

모든 조회는 target_id를 필수로 받는다. 기본 target·사용자 임의 endpoint·다른 target으로 재시도는 없다.

```text
list_targets({})
inspect_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
list_length({"target_id": "job-queue", "key": "example:jobs:pending"})
connection_status({"target_id": "catalog-cache"})
preview_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
find_keys({"target_id": "catalog-cache", "scope_id": "prefix-0"})
find_keys({"target_id": "catalog-cache", "scope_id": "prefix-0", "token": "직전_next_token"})
```

list_targets는 ID·engine·환경·허용 도구·탐색 scope ID·프록시 상태만 제공한다. 주소·계정·자격증명·키 prefix는 반환하지 않는다. contract_verified는 worker 계약 검증 상태이지 실제 서버 연결 정상 상태가 아니다.

### 메타데이터 및 미리보기

- inspect_key: TYPE, PTTL, 타입별 STRLEN/LLEN/HLEN/SCARD/ZCARD/XLEN을 실행한다. 지원하지 않는 모듈 타입은 길이 null이다.
- list_length: LLEN만 실행한다. 별도 Redis MCP 도구를 사용하지 않는다.
- connection_status: 실제 연결·인증·엔진 협상 후 PING을 실행한다. ACL 전체 검증이 아니다.
- preview_key: String에만 GETRANGE 0 N-1을 실행한다. 전체 GET 후 절단하지 않는다.
- 바이너리나 잘린 UTF-8은 base64로 반환하며, 여러 명령의 관측은 atomic=false다.
- 타입 변경·권한 거절·업스트림 오류를 빈 정상 결과로 바꾸지 않는다.

### 키 탐색

features.key_discovery=true일 때만 find_keys를 제공한다. list_targets의 discovery_scopes에서 사용할 scope ID를 확인한다.

- 서버가 literal prefix를 glob 문법에 맞게 escape한다. 사용자는 pattern·cursor·COUNT를 전달할 수 없다.
- 호출당 SCAN 한 번만 실행한다. 자동 전체 순회하지 않는다.
- 일회용 토큰은 target·scope·현재 프로세스에 묶인다. 동시 사용·재사용·교차 사용을 거절한다.
- 빈 페이지·중복 키도 예산에 포함하며, 시간·호출·관측·상태 개수를 제한한다.
- 결과를 일부 버리거나 예산이 종료되면 partial·truncated·stop_reason을 제공하고 안전하게 이어받을 수 없으면 토큰을 발급하지 않는다.
- SCAN COUNT는 정확한 개수·바이트 상한이 아니다. 마지막 페이지가 관측 예산을 초과할 수 있으며 이미 사용한 서버 자원을 되돌릴 수 없다.
- prefix MATCH와 ACL 키 패턴만으로 서버 내부 탐색량·키 이름 열거를 완전히 격리하지는 못한다. 엄격한 자원 상한이 필요하면 탐색을 끈다.

## 5. ACL 생성 및 검증

```bash
uv run --locked valkeep --config config/policy.local.toml --acl-plan --target catalog-cache
uv run --locked valkeep --config config/policy.local.toml \
  --verify-acl /secure/path/acl-export.json --target catalog-cache
```

계획에는 engine·minimum_version과 관리자 검토용 ACL SETUSER 인자 목록을 출력한다. -@all, 필요한 개별 명령, %R~ 읽기 키 범위만 허용한다. 연결 정보 등록은 CLIENT SETINFO 하위 명령만 허용하며 DB가 0이 아니면 SELECT를 추가한다. HELLO는 연결 협상 명령이며 이를 위해 +@connection을 허용하지 않는다.

**reset은 기존 권한·비밀번호를 제거한다.** 기존 계정에 무작정 적용하지 않는다. 목록에 비밀번호는 없으며 관리자가 별도 설정해야 한다. MCP가 계정 생성·권한 변경을 실행하지 않는다.

관리자는 신뢰할 수 있는 경로로 valkey-cli --json ACL GETUSER <계정> 결과를 준비한다. Redis 대상은 redis-cli로도 가능하다. 검사기는 활성 상태·비밀번호 존재·개별 명령·읽기 키 범위를 비교하고 초과 권한·selector·채널을 거절한다.

export 검사는 오프라인 파일 일치 검사다. 파일의 출처·최신성·현재 계정의 권한을 증명하지 않는다. readonly_acl_confirmed=true도 관리자 확인 선언이다. 운영 키에 쓰기 시험을 보내 권한을 검증하지 않는다. export는 비밀번호 해시를 포함하므로 안전하게 보관·폐기한다.

## 6. 실행·수명·감사

```bash
uv run --locked valkeep --config config/policy.local.toml \
  --check-connections --target catalog-cache
uv run --locked valkeep --config config/policy.local.toml
```

연결 점검은 실제 네트워크 요청이다. target을 생략하면 등록된 모든 대상을 점검한다. 일반 서버 실행은 stdout에 안내 문구 없이 stdio MCP 요청을 기다린다.

시작 시 모든 target의 설정·비밀번호·worker 계약을 확인한다. 하나라도 실패하면 시작한 worker를 정리하고 공개 MCP 시작을 거절한다. 실행 중에는 대상별 장애를 분리한다.

client는 socket/connect timeout과 retry=0을 사용한다. 응답 timeout·취소 이후에도 실제 호출 종료 전에는 슬롯을 반환하지 않는다. 정리 시간을 넘으면 해당 자식을 종료하고 target을 차단한다. 이미 서버에서 실행 중인 명령이 반드시 취소되는 것은 아니다.

감사 파일은 target·라우터별로 분리하고 owner-only·symlink 거절을 적용한다. 원본 키·값·비밀번호·토큰을 기록하지 않는다. 대상 감사 오류는 해당 target을, 공통 감사 오류는 전체 새 조회를 차단한다. 로그 회전·보존·변조 방지 저장은 별도 운영 구성이다.

## 7. 실제 Valkey 테스트

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q -m "not live"
uv run --locked pytest -q --require-valkey \
  --valkey-image valkey/valkey@sha256:1f84517eca8e0dd9585ed57c572796f32839e1b0e7ce07ad28e6bce098468371 \
  --valkey-image valkey/valkey@sha256:418652cfb58ef879d4978c33553735d7147016032d5aefaa14c828e611eb9dfd
uv build
```

고정 이미지는 각각 Valkey 8.1.6과 9.1.2다. Docker 엔진과 OpenSSL이 필요하며, 컨테이너에 읽기 전용 임시 설정·인증서 디렉토리만 마운트한다. 임시 계정·데이터를 사용하고 랜덤 host 포트를 loopback에만 공개한다. 테스트 종료 시 생성한 컨테이너를 제거한다. 외부 URL이나 기존 데이터베이스를 입력받지 않는다.

Docker 옵션을 생략하면 설치된 valkey-server·redis-server로 테스트한다. 실행 파일이 없으면 선택적인 native 테스트는 skip한다. --require-valkey는 Valkey·TLS 필수 조건 누락을 실패로 처리한다. CI는 Valkey 두 버전의 digest를 고정하고 필수 실행하도록 구성한다.

평문·TLS, 주요 데이터 타입·밀리초 TTL, 큰 값의 제한된 미리보기·바이너리, 탐색·literal glob, 잘못된 비밀번호·엔진, 다른 DB, 쓰기·관리·범위 밖 접근 거절 및 ACL export 검사를 다룬다. Redis 8.10.1 native 테스트는 호환 모드의 별도 회귀 검사다.

운영 부하 허용치·실제 인증 방식·관리형 서비스·데이터 분포·로그 보존·자격증명 회전은 실제 배포 전에 추가 검증해야 한다. 이번 구현에서는 운영 대상에 접속하지 않았다.
