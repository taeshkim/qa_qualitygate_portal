# Improvement: API 엔드포인트별 담당자 매핑

> **목표** — `api_endpoints_owners.csv` (540행, 12컬럼) 을 Django 모델로 옮긴 뒤,
> Admin 화면에서 CSV 일괄 임포트 / 행 단위 수동 생성/수정 을 모두 지원하고,
> Quality Gate 와 Test Suite 의 **Error Log** 화면에 등장하는 URL 을 자동으로
> 매칭해 "이 URL 의 담당자: X (팀: Y)" 를 같이 보여준다.
>
> 본 문서는 **계획 문서**다. 구현은 5개 Phase 로 나눠 진행하며, 각 Phase 가 끝날 때마다
> README/maintenance.md 동기화 + 단일 커밋 + push 까지 끝낸다.
>
> 작성일: 2026-05-20

---

## 0. 의사결정 요약 (선결정 필요)

아래 5건은 구현 전에 사용자 의사 확인이 필요한 분기점이다. 모두 본 문서에서는
**추천안** 을 굵게 표기하고 대안은 trade-off 와 함께 정리.

| # | 결정 | 추천 | 대안 |
|---|---|---|---|
| D1 | 새 모델이 살 앱 | **`apps/integrations/`** (외부 시스템 매핑 책임이 자연스러움) | `apps/accounts/` (담당자=사람) / `apps/owners/` (신규 앱) |
| D2 | 담당자/팀 정규화 | **평면화 (CharField 그대로)** — 초기 도입 비용 최소 | `Team` 모델 + `Owner` FK 분리 (장기적으로 그룹화 쿼리 유리하나 콤마 다중값 풀어내야) |
| D3 | 호스트 변수→실제 도메인 매핑 입력 방식 | **별도 모델 `ApiHostMapping`** (admin 화면에서 환경(dev/stg/prd) 별로 등록) | `.env` 환경변수 / 하드코드 dict |
| D4 | URL 매칭 알고리즘 | **path-only 매칭 + Method strict + 리터럴 segment 수 정렬 + 사전 컴파일 regex 캐시** (호스트는 매칭에 미사용, 동률 시에만 `ApiHostMapping` 으로 tie-break) | weighted segment score / fuzzy |
| D5 | Error Log 통합 UX | **옵션 A — view 단에서 URL 추출 → 상단에 요약 표 노출** + `<pre>` 본문은 그대로 | in-line 링크화 (옵션 B) / 별도 페이지 (옵션 C) |

위 5개 중 D1, D2, D3 은 구현 비용/장기 운영에 영향이 크므로 **Phase 1 진입 전 확정** 이 좋다.

---

## 1. 데이터 형상 (CSV 분석 요약)

| 항목 | 값 |
|---|---|
| 총 행 수 | 539 (헤더 1줄 별도) |
| 컬럼 수 | 12 |
| NULL/빈값 비율 | 0% (전 컬럼 완전) |
| 인코딩 / 줄바꿈 | UTF-8 (BOM 없음) / CRLF |
| 큰따옴표 escape | 정상 (`csv.DictReader` 그대로 OK) |
| Endpoint 최대 길이 | 430자 |
| Method distinct | GET(457), POST(63), PATCH(11), DELETE(5), PUT(3) |
| Endpoint prefix | 절대경로 83% / `${...}` 16% / 기타 1% |
| 호스트 변수 (6종) | `${BFF_URL}`(38), `${BFF_COMPANY_URL}`(24), `${BFF_PERSONAL_URL}`(20), `${PAY_URL}`(2), `${AM_BFF_DISPLAY_URL}`(1), `${ALBAMON_MEMBER_URL}`(1) |
| Path param 변수 (5종+) | `${RECRUIT_NO}`(9), `${APPLY_NO}`(3), `${created_gno}`(2), `${RESUME_NO}`(2), `${BRAND_BAKERY_ID}`(2), `${target_gno}`(1) |
| Query string 포함 행 | 147 (27%) |
| 500에러 담당자 콤마 다중값 비율 | 90.5% — `"이동훈, 윤복영"` 패턴이 488행 |
| **유니크 키 후보** | **(레포, Method, Endpoint)** — 중복 1.48% (8행), (Method, Endpoint) 단독은 38.59% 중복으로 부적합 |

### 1.1 같은 URL 이 GET/POST 등 다른 Method 로 중복 등록되는 사례

- `POST /member/login` ↔ `GET /member/login` (로그인 액션 vs 로그인 페이지)
- `POST .../online-apply` ↔ `GET .../online-apply` (지원 vs 정보조회)
- `PATCH .../apply-cancel` ↔ `GET .../apply-cancel` (취소 vs 조회)

→ **Method 가 매칭 키의 일부여야 함**.

---

## 2. 데이터 모델 설계

### 2.1 `ApiEndpointOwner` (메인 모델 — `apps/integrations/models.py`)

```python
import uuid
from django.db import models

class ApiEndpointOwner(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # 식별 (unique_together)
    repo     = models.CharField(max_length=32,  verbose_name="레포")          # 'JK PC' / 'AM Mobile' 등
    method   = models.CharField(max_length=10,  verbose_name="Method")        # 'GET' 등
    endpoint = models.CharField(max_length=500, verbose_name="Endpoint")      # 자체 길이 최대 430자, 여유 500

    # 분류
    category    = models.CharField(max_length=100, blank=True, verbose_name="카테고리")  # '회원가입 / 인증'
    area        = models.CharField(max_length=100, blank=True, verbose_name="영역")       # '로그인 / 회원가입 / 인증'
    mapped_app  = models.CharField(max_length=100, blank=True, verbose_name="매핑 앱")    # 'auth-api'

    # 담당자/팀 (평면화 — 콤마 다중값을 원문 그대로 보존)
    error_4xx_owner = models.CharField(max_length=200, blank=True, verbose_name="400에러 담당자")
    error_4xx_team  = models.CharField(max_length=200, blank=True, verbose_name="400에러 담당팀")
    error_5xx_owner = models.CharField(max_length=200, blank=True, verbose_name="500에러 담당자")
    error_5xx_team  = models.CharField(max_length=200, blank=True, verbose_name="500에러 담당팀")

    # 메타
    usage_count = models.PositiveIntegerField(default=0, verbose_name="사용 횟수")
    step_ids    = models.TextField(blank=True, verbose_name="Step IDs (콤마)")

    # 운영
    is_imported = models.BooleanField(default=False, verbose_name="CSV 임포트로 들어온 row 인지")
    # ↑ True 면 import 가 덮어쓸 수 있음. False (수동 row) 는 import 가 건드리지 않음.

    notes = models.TextField(blank=True, verbose_name="비고 (수동 메모)")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table  = "integrations_api_endpoint_owner"
        ordering  = ["repo", "method", "endpoint"]
        constraints = [
            models.UniqueConstraint(
                fields=["repo", "method", "endpoint"],
                name="uq_api_endpoint_owner_repo_method_endpoint",
            ),
        ]
        indexes = [
            models.Index(fields=["mapped_app"]),
            models.Index(fields=["error_4xx_team"]),
            models.Index(fields=["error_5xx_team"]),
        ]

    def __str__(self):
        return f"{self.method} {self.endpoint} ({self.repo})"
```

### 2.2 `ApiHostMapping` (환경변수 → 실제 호스트 매핑 — `apps/integrations/models.py`)

CSV 의 `${BFF_URL}` 을 실제 `bff-general-01.dev.albamon.com` 같은 호스트로 풀려면
외부 매핑이 필요한데, 코드/CSV 어디에도 단서가 없으므로 Admin 화면에서 직접
관리한다.

```python
class ApiHostMapping(models.Model):
    """`${BFF_URL}` → `bff-general-*.dev.albamon.com` 같은 환경변수→호스트 매핑.

    같은 변수가 환경(dev/stg/prd) 별로 다른 호스트일 수 있으므로 env 도 키에 포함.
    """
    class Env(models.TextChoices):
        DEV = "dev", "Dev"
        STG = "stg", "Staging"
        PRD = "prd", "Production"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    variable_name = models.CharField(max_length=64,  verbose_name="환경변수명")  # 'BFF_URL' (${} 제외 저장)
    env           = models.CharField(max_length=8,  choices=Env.choices, default=Env.DEV)
    host_pattern  = models.CharField(max_length=255, verbose_name="호스트 패턴")
    # ↑ 'bff-general-*.dev.albamon.com' 처럼 * 와일드카드 허용 (정확 매칭 / 와일드카드 둘 다)
    notes         = models.TextField(blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["variable_name", "env", "host_pattern"],
                name="uq_api_host_mapping_var_env_host",
            ),
        ]
        ordering = ["variable_name", "env"]
```

**왜 env 별로 분리?** — Dev/Stg/Prd 호스트가 다르고, 한 변수가 여러 도메인을
shard 형태로 가질 수도 있다(`bff-general-01`, `bff-general-02` ...). 와일드카드
하나면 충분하지만 명시적인 row 도 허용해 운영자가 직접 확인 가능.

### 2.3 평면화 결정의 근거 (D2)

- CSV 의 `"이동훈, 윤복영"` 패턴이 90.5% — 콤마를 풀어 `Owner` FK 로 만들면 한
  Endpoint 가 N 명의 담당자를 가지므로 M2M 이 필요. 표 1개에서 → 3개 표
  (Owner / OwnerEndpoint / EndpointOwner) 가 되고 CSV 임포트 로직도 그만큼 복잡.
- "팀별 그룹화" 쿼리는 `error_4xx_team` / `error_5xx_team` 컬럼에 인덱스만 걸어두면
  현재 540행 규모에선 충분히 빠르다 (`GROUP BY` 인덱스 스캔).
- 향후 팀/담당자 관리가 진짜 복잡해지면 그때 마이그레이션으로 정규화. **YAGNI**.

### 2.4 `is_imported` 플래그

CSV import 가 사용자 수동 row 를 덮어쓰지 않도록 분리. 첫 import 는 모든 row 가
`is_imported=True`. 사용자가 admin 화면에서 추가/편집한 row 는 `is_imported=False`.
재임포트는 `is_imported=True` 인 row 만 update/delete 대상으로 본다.

---

## 3. CSV 임포트 설계

### 3.1 함수 시그니처 (`apps/integrations/services.py` 신규)

```python
@dataclass
class ImportResult:
    created: int
    updated: int
    skipped_manual: int      # is_imported=False 라서 건드리지 않은 행
    deleted_stale:  int      # import 에 없는 imported row 삭제 수 (옵션 활성 시)
    errors: list[dict]       # [{"row": 12, "error": "..."}]

def import_api_endpoint_owners(
    csv_file,                # file-like (uploaded file) or path
    *,
    user,                    # request.user
    dry_run: bool = False,
    remove_stale: bool = True,
) -> ImportResult: ...
```

### 3.2 알고리즘

1. `csv.DictReader(io.TextIOWrapper(uploaded_file, encoding='utf-8-sig'))` 로 헤더 자동 매핑.
2. 헤더 검증 — 12개 한국어 헤더 모두 존재 확인. 누락 시 즉시 fail 하고 `errors` 채워 리턴.
3. `transaction.atomic()` 안에서 row 별로:
   - 키 = `(레포, Method, Endpoint)` 로 `update_or_create(defaults={..., is_imported=True})`.
   - 단, **기존 row 가 `is_imported=False` 면 skip** + `skipped_manual` 증가.
   - 빈 행 (모든 컬럼 공백) 은 건너뛴다.
   - 예외는 `errors` 에 누적하고 다음 행 계속. 5개 이상 누적되면 즉시 rollback (안전망).
4. `remove_stale=True` 면 import 에 등장하지 않은 `is_imported=True` row 들을 삭제.
   수동 row(`is_imported=False`)는 보존. UI 에서 체크박스로 선택 가능.
5. `dry_run=True` 면 마지막에 `transaction.set_rollback(True)` 로 롤백, 결과만 리턴.
6. 성공 시 ActivityLog 기록 (§7).

### 3.3 헤더 매핑 (CSV → 모델)

```python
HEADER_MAP = {
    "레포":             "repo",
    "카테고리":         "category",
    "영역":             "area",
    "Method":           "method",
    "Endpoint":         "endpoint",
    "400에러 담당자":   "error_4xx_owner",
    "400에러 담당팀":   "error_4xx_team",
    "500에러 담당자":   "error_5xx_owner",
    "500에러 담당팀":   "error_5xx_team",
    "매핑 앱":          "mapped_app",
    "사용 횟수":        "usage_count",  # int 변환
    "step_ids":         "step_ids",
}
```

### 3.4 에러 케이스 (전부 테스트 필요)

| 케이스 | 기대 동작 |
|---|---|
| `사용 횟수` 가 비정수 (`'abc'`) | `errors` 에 row 번호 + reason, 다음 행 계속 |
| 헤더 누락 | 즉시 fail, 어느 헤더가 빠졌는지 명시 |
| BOM 포함 (UTF-8-BOM) | `utf-8-sig` 디코더로 자동 처리 |
| CRLF 줄바꿈 | csv 모듈이 자동 처리 |
| 콤마/큰따옴표 escape (`"이동훈, 윤복영"`) | `csv.DictReader` 가 자동 unquote |
| 같은 (repo, method, endpoint) 가 CSV 안에서 중복 | 마지막 값으로 덮어쓰기 (보고만) |
| `is_imported=False` 인 기존 row 와 키 충돌 | skip + `skipped_manual` |

---

## 4. URL 매칭 알고리즘 설계

### 4.0 핵심 원칙 — **path-based 매칭**

매칭의 1차 기준은 **`/` 로 시작하는 path** 다. 사용자가 보는 실제 로그가
```
URL: https://bff-general-01.dev.albamon.com/apply/recruits/95053137/online-apply
```
이고 CSV row 가
```
${BFF_URL}/apply/recruits/${RECRUIT_NO}/online-apply
```
일 때, 매칭은 **호스트(`bff-general-01.dev.albamon.com` / `${BFF_URL}`)를 양쪽 다 떼어낸 뒤**
`/apply/recruits/{wild}/online-apply` 형태로 path 만 비교한다.

즉:
- CSV 의 `${BFF_URL}` 같은 host 변수 prefix 는 컴파일 시점에 **그냥 떼어낸다**. 매칭에 안 쓴다.
- 입력 URL 의 netloc(`bff-general-01.dev.albamon.com`) 도 매칭에 안 쓴다.
- 양쪽 다 path 만 본다.
- `ApiHostMapping` 은 **같은 path 가 여러 row 와 매칭될 때만 동작하는 tie-breaker** 이지,
  매칭 자체의 필수 조건이 아니다 — 등록을 안 해도 path 만으로 잘 동작한다.

이렇게 짜야 운영자가 `ApiHostMapping` 을 깜빡하거나 새 도메인이 추가됐을 때도
매칭이 silent fail 하지 않는다.

### 4.1 입력 / 출력

```
입력:  ('POST', 'https://bff-general-01.dev.albamon.com/apply/recruits/95053137/online-apply')
출력:  ApiEndpointOwner 인스턴스 or None
```

### 4.2 단계

#### 단계 1 — URL 분해 + path 정규화
- `urllib.parse.urlsplit(url)` 으로 `(scheme, netloc, path, query)` 분해.
- **query string 제거** (매칭에 안 씀).
- path 끝의 `/` 정규화 (`rstrip("/")`).
- path 앞에 `/` 가 없으면 추가 (정규화).
- 결과: `target_path = '/apply/recruits/95053137/online-apply'` 같은 형태.

#### 단계 2 — CSV endpoint 를 path 정규식으로 컴파일 (앱 부팅 / 모델 save signal 시점, LocMem 캐시)
각 `ApiEndpointOwner` row 의 `endpoint` 를 다음 규칙으로 변환:

| 입력 | 처리 |
|---|---|
| `${HOST_VAR}/...` 로 시작 | host 변수 부분(`${HOST_VAR}`) 통째 제거 → `/...` 만 남김. host 변수명은 row 의 메타로 따로 보관(tie-breaker 용). |
| `/...` 로 시작 (JK 절대경로) | 그대로. host 변수는 `None`. |
| `?query=...` 부분 | 잘라낸다 (query 는 매칭에 미사용). |
| path 안의 `${PARAM}` (path param 자리표시자) | `[^/]+` 로 치환. |
| 그 외 리터럴 문자 | `re.escape`. |
| 끝의 `/` | 정규화 (`rstrip`). |

컴파일 결과 인덱스:

```python
{
  method: [
    {
      "pk":         <ApiEndpointOwner.pk>,
      "pattern":    re.compile(r'^/apply/recruits/[^/]+/online-apply$'),
      "host_var":   "BFF_URL" | None,
      "raw_path":   "/apply/recruits/${RECRUIT_NO}/online-apply",
      "specificity": <리터럴 segment 수 — 정렬 키>,
    },
    ...
  ]
}
```

#### 단계 3 — 후보 수집 (path-only)
- `target_path` 를 위 인덱스의 `method` 풀에서 모든 row 와 매칭 시도.
- 일치하는 row 들의 list = `candidates`.
- `candidates` 가 0 이면 즉시 None 리턴.
- 1 이면 그 row 리턴.

#### 단계 4 — 동률 처리 (여러 후보일 때만)

`candidates` 가 2 개 이상이면 다음 우선순위로 1 개를 선택:

1. **리터럴 segment 수** 가 가장 많은 (= 가장 구체적인) row 우선.
   예: `/api/users/${UID}/orders/${OID}` 가 `/api/users/${UID}/*` 보다 우선.
2. (동률 시) **`ApiHostMapping` 으로 netloc → host_var 추론이 가능하면**
   그 host_var 를 가진 candidate 우선. 추론 실패 / 매핑 없음이면 skip.
3. (여전히 동률) **host_var 가 채워진 row** 가 None(JK 절대경로) 보다 우선
   (CSV 가 환경변수까지 명시했다는 건 좀 더 정확한 후보임을 시사).
4. (여전히 동률) `repo` 가 입력 URL 의 netloc 과 더 닮은 row.
   힌트: `"AM"` ∈ repo 이고 netloc 에 `albamon` 포함이면 가점. `"JK"` ∈ repo 이고
   netloc 에 `jobkorea` 포함이면 가점. 부재 시 미적용.
5. (여전히 동률) `usage_count` 가 큰 row 우선 (실제로 더 자주 호출되는 endpoint).
6. (여전히 동률) row 의 `pk` 안정 정렬 — 결정적인(deterministic) 결과 보장.

#### 단계 5 — `ApiHostMapping` 의 역할 (요약)

- **필수가 아니다.** 매핑이 비어있어도 단계 3 의 path 매칭이 단독으로 동작.
- 같은 path 가 AM `${BFF_URL}/foo` 와 JK `/foo` 양쪽에 등록됐을 때 disambiguation 용.
- 와일드카드 매칭: `fnmatch.fnmatchcase(netloc, pattern)`.
- 매핑이 있어도 단계 3 의 결과를 **덮어쓰지 않는다** — 단계 4 의 tie-breaker 로만 가산.

### 4.3 와일드카드 / Method strict

- **Method 는 항상 strict 매칭**. 같은 path 가 GET/POST 로 다른 owner 일 수 있으므로 (§1.1).
- path param `${...}` 만 와일드카드 (`[^/]+`).
- host 변수의 뒤 path segment 는 리터럴 (`/apply/recruits/` 같은 부분은 그대로 매칭해야 함).

### 4.4 캐시 / 성능

- 540행 × distinct method 5 → 풀 1개당 평균 100개 정규식. 정규식 1번에 시도하는 비용은
  microsecond 단위이므로 문제 없음.
- LocMem 캐시 키: `qgp:api_owner_patterns:v1`. invalidate 시점:
  - `ApiEndpointOwner` save/delete signal.
  - `ApiHostMapping` save/delete signal.

### 4.6 동작 예시 (사용자가 보낸 실제 케이스)

**입력**
```
Method: POST
URL:    https://bff-general-01.dev.albamon.com/apply/recruits/95053137/online-apply
```

**처리**
1. `urlsplit` → `netloc='bff-general-01.dev.albamon.com'`, `path='/apply/recruits/95053137/online-apply'`.
2. 컴파일된 인덱스의 POST 풀에서 path 매칭 시도.
3. `${BFF_URL}/apply/recruits/${RECRUIT_NO}/online-apply` 컴파일 결과
   `^/apply/recruits/[^/]+/online-apply$` 와 일치 → candidate 1개.
4. candidate 1개이므로 그대로 반환.

**결과**
```
ApiEndpointOwner(
    repo='AM Mobile',
    method='POST',
    endpoint='${BFF_URL}/apply/recruits/${RECRUIT_NO}/online-apply',
    error_4xx_owner='장호철',
    error_5xx_owner='이동훈, 윤복영',
    ...
)
```

`ApiHostMapping` 에 `BFF_URL → bff-general-*.dev.albamon.com` 매핑을 **등록하지 않아도**
이 매칭은 그대로 성공한다.

### 4.7 매칭 함수 시그니처 (`apps/integrations/url_matcher.py` 신규)

```python
def find_endpoint_owner(method: str, url: str) -> ApiEndpointOwner | None: ...

def find_endpoint_owners_from_text(text: str) -> list[MatchedUrl]:
    """`URL: https://...` / 단독 `https://...` 줄을 모두 추출 + 매칭 + dedupe + count.

    반환: [{"url": ..., "method": "POST"|None, "count": N, "owner": ApiEndpointOwner|None}, ...]
    method 는 `Method: POST` 라인이 같은 step 안에 있으면 그 값. 없으면 None
    (None 이면 매칭 시 GET 가정).
    """
```

---

## 5. Admin UI 설계

### 5.1 라우팅 (`apps/integrations/urls.py` 신규 또는 `apps/accounts/urls.py` 위임)

라우팅은 Admin 탭 일관성을 위해 **`/accounts/` prefix 아래** 배치:

| URL | view | 권한 |
|---|---|---|
| `/accounts/api-owners/` | `api_owner_list` | admin only |
| `/accounts/api-owners/new/` | `api_owner_create` | admin |
| `/accounts/api-owners/<uuid>/edit/` | `api_owner_edit` | admin |
| `/accounts/api-owners/<uuid>/delete/` | `api_owner_delete` (POST) | admin |
| `/accounts/api-owners/import/` | `api_owner_import` (POST, file upload) | admin |
| `/accounts/api-owners/export.csv` | `api_owner_export` (GET) | admin |
| `/accounts/api-hosts/` | `api_host_list` | admin |
| `/accounts/api-hosts/new/` | `api_host_create` | admin |
| `/accounts/api-hosts/<uuid>/edit/` | `api_host_edit` | admin |
| `/accounts/api-hosts/<uuid>/delete/` | `api_host_delete` (POST) | admin |

view 함수는 `apps/integrations/views.py` 에 두되, `apps/accounts/urls.py` 에서
import 해 등록 (Admin 탭 그룹핑 일관성).

### 5.2 사이드바 + 탭 헤더

#### `templates/accounts/_admin_tabs.html`
기존 3개 탭 옆에 추가:
```html
<a href="{% url 'accounts:api_owner_list' %}"
   class="{% if admin_active_tab == 'api_owners' %}active{% endif %}">
    <i class="fas fa-user-tie"></i> 담당자 관리
</a>
<a href="{% url 'accounts:api_host_list' %}"
   class="{% if admin_active_tab == 'api_hosts' %}active{% endif %}">
    <i class="fas fa-server"></i> 호스트 매핑
</a>
```

#### `templates/base.html:50-56`
Admin sidebar 의 url_name 매칭 조건에 `api_owner_*`, `api_host_*` 추가:
```html
class="{% if request.resolver_match.url_name|slice:":10" == "api_owner_"
           or request.resolver_match.url_name|slice:":9"  == "api_host_"
           or request.resolver_match.url_name == 'user_list'
           ... %}active{% endif %}"
```
(템플릿 필터 한계상 startswith 가 어색하면 view-side 에 boolean 컨텍스트로 넘기는 게 깔끔.)

### 5.3 담당자 목록 화면

- **필터 / 검색** (GET 파라미터):
  - `q` — repo / category / endpoint / owner / team 전체 LIKE
  - `repo` — distinct repo dropdown
  - `mapped_app` — distinct mapped_app dropdown
  - `team` — 400/500 team 둘 다 검색
  - `is_imported` — yes/no/all
- **페이지네이션**: `Paginator(queryset, 50)` — `apps/activitylog/views.py:23-24` 패턴.
- **표 컬럼**: 레포 / Method / Endpoint / 카테고리 / 매핑 앱 / 400담당 / 500담당 / 수정 / 삭제
- **상단 액션 버튼**:
  - "CSV 일괄 임포트" — 모달 (`<input type="file">` + dry-run 체크박스 + remove-stale 체크박스)
  - "수동 row 추가" — `api_owner_create` 폼
  - "CSV 다운로드" — 현재 필터 적용된 결과를 CSV 로

### 5.4 파일 업로드 폼

```python
class ApiOwnerImportForm(forms.Form):
    file        = forms.FileField(label="CSV 파일")
    dry_run     = forms.BooleanField(required=False, initial=False,
                                     label="Dry-run (실제 저장 안 함)")
    remove_stale = forms.BooleanField(required=False, initial=False,
                                      label="CSV 에 없는 imported row 삭제")
```

업로드 성공 시 결과 페이지(`api_owner_import_result.html`) 로 redirect:
- created / updated / skipped_manual / deleted_stale / errors 표시.
- errors 가 있으면 row 번호 + reason 표.

### 5.5 호스트 매핑 화면

- 단순 CRUD. 모달 또는 별도 페이지로 (`ApiHostMapping` 모델만 다룸).
- variable_name dropdown 은 CSV 안에서 자동 감지된 변수명 + 자유 입력.

### 5.6 권한

- 모든 view 첫 줄에 `@login_required` + `_require_admin(request.user)` (기존 패턴 — `apps/accounts/views.py:43-45`).
- `RolePermissionMiddleware` 의 `REGULAR_POST_ALLOW` 에는 추가하지 않는다 (admin 전용).

### 7.5 Sidebar / 메뉴 노출
- 사이드바 Admin 메뉴 자체는 기존 그대로 유지. 두 탭은 _admin_tabs.html 안에만 추가.

---

## 6. Error Log 화면 통합

### 6.1 변경 흐름

1. `apps/integrations/log_errors.py` 에 `extract_urls(text)` 추가.
   - `r'(?:URL[:\s]+)?(https?://[^\s\n]+)'` 정규식 + 같은 step 의 `Method:` 라인을 함께 추출.
   - 같은 url 은 dedupe 하면서 count 증가.
2. `apps/qualitygates/views.py:_render_error_log` 에서 `extract_urls(console_output)` 호출 →
   각 url 에 대해 `url_matcher.find_endpoint_owner(method, url)` 매칭 → context 에 `matched_urls` 변수.
3. `apps/suites/views.py:job_run_error_log` 도 동일.
4. 템플릿 `templates/qualitygates/error_log.html` / `templates/suites/error_log.html` 상단에
   `<table>` 추가:

   ```
   | URL                              | Method | 호출 횟수 | 400 담당 | 500 담당 |
   | https://bff-.../online-apply     | POST   | 5        | 장호철   | 이동훈, 윤복영 |
   ```

   매칭 실패한 URL 은 담당 컬럼에 "미등록" + "담당자 등록" 링크(`?endpoint=...&method=...` 쿼리로
   `api_owner_create` 폼 prefill).

### 6.2 본문 `<pre>` 는 그대로

- 옵션 B (in-line 링크화) 는 정규식이 본문 텍스트를 수정해야 하므로 가독성 손상.
- 옵션 C (별도 페이지) 는 페이지 hop 이 늘어 UX 손해.
- 옵션 A — **본문은 raw 그대로 + 상단 요약 표** 가 가장 깔끔. 채택.

### 6.3 매칭 못 한 URL 의 "담당자 등록" 동선

- 표 행에 "+ 등록" 버튼 → `/accounts/api-owners/new/?method=POST&endpoint=/apply/recruits/${RECRUIT_NO}/online-apply&host=bff-general-01.dev.albamon.com` 로 prefill.
- host 정보가 있으면 `ApiHostMapping` 도 신규 row 후보로 안내.
- admin 만 노출 (`{% if user.is_admin %}` 가드).

### 6.4 권한

- Error Log 자체는 `@login_required` 만 (Viewer 도 조회 가능).
- 담당 정보도 동일하게 모두에게 노출 (`RolePermissionMiddleware` 의 `can_write` 와 무관).
- "+ 등록" 동선만 admin 가드.

---

## 7. ActivityLog / 감사

### 7.1 신규 액션 (`apps/activitylog/models.py:Action`)

```python
API_OWNER_CREATED  = "api_owner_created",  "담당자 생성"
API_OWNER_UPDATED  = "api_owner_updated",  "담당자 수정"
API_OWNER_DELETED  = "api_owner_deleted",  "담당자 삭제"
API_OWNER_IMPORTED = "api_owner_imported", "담당자 CSV 임포트"
API_HOST_CREATED   = "api_host_created",   "호스트 매핑 생성"
API_HOST_UPDATED   = "api_host_updated",   "호스트 매핑 수정"
API_HOST_DELETED   = "api_host_deleted",   "호스트 매핑 삭제"
```

### 7.2 신규 TargetType

```python
API_ENDPOINT_OWNER = "api_endpoint_owner", "API Endpoint Owner"
API_HOST_MAPPING   = "api_host_mapping",   "API Host Mapping"
```

### 7.3 import 액션 로그 메시지

```python
ActivityLog.log(
    user=request.user,
    action=ActivityLog.Action.API_OWNER_IMPORTED,
    target_type=ActivityLog.TargetType.API_ENDPOINT_OWNER,
    target_name=f"CSV ({result.created}+{result.updated} rows)",
    detail=(f"created={result.created} updated={result.updated} "
            f"skipped_manual={result.skipped_manual} "
            f"deleted_stale={result.deleted_stale} errors={len(result.errors)}"),
)
```

---

## 8. 마이그레이션 / 배포

### 8.1 마이그레이션 순서

1. `apps/integrations/migrations/0002_apiendpointowner_apihostmapping.py` — 두 모델 CreateModel.
2. **자동 시드 안 한다** — 운영자가 admin 화면에서 첫 import.
   - 이유: CSV 버전 관리가 db 안에 들어오면 추적이 어렵고, 호스트 매핑은 어차피 사람이 직접 입력해야 함.
   - 단, 개발 환경 편의를 위해 `apps/integrations/management/commands/import_api_owners.py` 명령은 추가 — `manage.py import_api_owners api_endpoints_owners.csv` 한 줄로 import 가능.
3. `apps/activitylog/migrations/000X_add_api_owner_actions.py` — Action / TargetType 확장.
   - 두 모델이 TextChoices 라서 마이그레이션 X. 그냥 코드 추가만.

### 8.2 롤백

- 모델 삭제 마이그레이션이 자동 생성됨. 기존 데이터는 백업 안 해도 됨 (다시 csv 임포트 가능).
- 단, 수동 row (`is_imported=False`) 는 CSV 에 없으므로 사용자에게 백업 가이드 필요.

### 8.3 데이터 손실 방지

- `db.sqlite3` 충돌 시 항상 로컬본 최신 (기존 CLAUDE.md 룰).
- 첫 import 후 export CSV 받아 git LFS 가 아닌 별도 백업 위치에 저장 권장.

---

## 9. 테스트 전략

### 9.1 단위 테스트 — `apps/integrations/tests.py` 신규 (또는 확장)

| 테스트 | 함수 |
|---|---|
| CSV header missing | `test_import_missing_header_fails` |
| 정상 csv | `test_import_creates_rows` |
| 콤마 escape 큰따옴표 | `test_import_handles_quoted_commas` |
| 중복 키 갱신 | `test_import_updates_existing` |
| 수동 row 보호 | `test_import_skips_manual_rows` |
| 빈 행 무시 | `test_import_ignores_blank_rows` |
| dry_run rollback | `test_dry_run_does_not_persist` |
| stale 삭제 | `test_remove_stale_deletes_only_imported` |
| URL 매칭 — AM 환경변수 | `test_match_am_bff_url` |
| URL 매칭 — JK 절대경로 | `test_match_jk_absolute_path` |
| URL 매칭 — path param | `test_match_recruit_no_wildcard` |
| URL 매칭 — Method 다른 endpoint | `test_match_method_strict` |
| URL 매칭 — Longest prefix | `test_match_picks_longest` |
| URL 매칭 — 호스트 매핑 없으면 JK fallback | `test_match_falls_back_to_jk_mode` |

### 9.2 수동 E2E 시나리오

1. admin 으로 `/accounts/api-owners/import/` → CSV 업로드 → 결과 페이지에서 539 created 확인.
2. `/accounts/api-hosts/` → `BFF_URL` `dev` → `bff-general-*.dev.albamon.com` 등록.
3. 기존 실패 Suite Job 의 Error Log 열어 상단에 매칭 표 노출 확인.
4. "+ 등록" 동선으로 신규 row 생성 후 재진입 → 매칭 결과 갱신.
5. 두 번째 CSV 임포트 (1개 행 수정 후) → `updated=1, others=skipped/updated` 메시지 확인.
6. remove-stale 옵션 켜서 임의 row 삭제 후 재임포트 → 그 row 가 `deleted_stale` 으로 처리되는지 확인.

---

## 10. 단계별 작업 계획 (Phase 1 → 5)

각 Phase 가 끝나면: 테스트 → README/maintenance.md 갱신 → 단일 커밋 → push.

### Phase 1 — 모델 + 마이그레이션 + 관리 명령어
- `apps/integrations/models.py` 에 `ApiEndpointOwner`, `ApiHostMapping` 추가.
- 마이그레이션 생성/적용.
- `manage.py import_api_owners <csv>` 커맨드 (`apps/integrations/management/commands/`).
- `apps/integrations/services.py` 의 `import_api_endpoint_owners()` 구현 + 단위 테스트 (8개).
- 결과 검증: 명령어로 `api_endpoints_owners.csv` 539행 import 성공.
- 커밋: `feat(integrations): ApiEndpointOwner/ApiHostMapping 모델 + CSV 임포트 서비스`

### Phase 2 — Admin 탭 + CRUD (담당자/호스트 매핑)
- `apps/integrations/forms.py` (ImportForm + CRUD ModelForm 들).
- `apps/integrations/views.py` 추가 — list/create/edit/delete/import/export 9개 view.
- `apps/accounts/urls.py` 에 path 위임 등록.
- `templates/accounts/_admin_tabs.html` 에 2개 탭 추가.
- `templates/accounts/api_owner_*.html`, `api_host_*.html` 신규.
- 페이지네이션 + 검색/필터.
- ActivityLog 액션/타입 확장.
- 커밋: `feat(integrations): Admin 담당자/호스트 매핑 화면 + CSV import/export`

### Phase 3 — URL 매칭 엔진
- `apps/integrations/url_matcher.py` 구현 — `extract_urls`, `find_endpoint_owner`, `find_endpoint_owners_from_text`.
- 사전 컴파일 캐시 + signal 기반 invalidate.
- 단위 테스트 (6개 매칭 케이스).
- 커밋: `feat(integrations): URL→담당자 매칭 엔진 (환경변수 호스트 + path param 와일드카드)`

### Phase 4 — Error Log 화면 통합
- `apps/qualitygates/views.py:_render_error_log` 와 `apps/suites/views.py:job_run_error_log` 에서 매칭 결과 context 추가.
- `templates/qualitygates/error_log.html` / `templates/suites/error_log.html` 상단 표 추가.
- 매칭 실패 URL 의 "+ 등록" prefill 동선.
- 커밋: `feat(qualitygates,suites): Error Log 상단에 URL별 담당자 표 노출`

### Phase 5 — 폴리싱
- 호스트 매핑 화면에 "이 변수가 CSV 에서 X번 등장" 카운트.
- 담당자 목록 페이지에 "매칭 미커버 endpoint" 필터 (Error Log 에서 자주 매칭 실패한 URL 추적).
- README / maintenance.md 의 §4 (외부 연동), §10 (자주 하는 변경) 에 새 모듈 기록.
- 커밋: `docs: 담당자 매핑 + Error Log 통합 문서 정리`

---

## 11. 위험 / 미해결 의문점

| # | 항목 | 영향도 | 대응 |
|---|---|---|---|
| R1 | 호스트 매핑이 사람 손에 의존 — 운영자가 환경변수 추가를 잊으면 매칭 실패 | 중 | Phase 5 의 "매칭 미커버 endpoint" 리포트로 보강. Error Log 화면에 "+ 호스트 매핑 등록" 동선도 안내. |
| R2 | CSV 가 정기적으로 갱신될 때 수동 row(`is_imported=False`) 가 누락 추적 안 됨 | 중 | export CSV 에 `is_imported` 컬럼 포함해서 운영자가 백업 가능. |
| R3 | `${RECRUIT_NO}` 같은 path param 이 `[^/]+` 와일드카드라 너무 관대해서 의도치 않은 매칭 가능성 | 낮 | 리터럴 segment 수 정렬(§4.2 단계 4) 로 대부분 해결. 정 안되면 path param 의 정규식을 owner 가 직접 입력하는 필드 추가 (Phase 6 후보). |
| R4 | 같은 endpoint 가 4xx/5xx 다른 팀 — 에러 코드를 어떻게 알 것인가 | 중 | 콘솔 로그에서 status code (예: `Status: 503`) 를 파싱해서 5xx 면 `error_5xx_*`, 4xx 면 `error_4xx_*` 분기. extract_urls() 가 같은 step 의 Status 라인을 함께 수집. |
| R5 | LocMem 캐시는 single-process 한정. runserver 멀티스레드 OK, multi-worker 면 무효화 race. | 낮 | 현재 환경이 runserver 단일 프로세스라 무시 가능. 향후 gunicorn 전환 시 신호 처리만 재검토. |
| R6 | CSV 헤더가 한국어라 변경에 취약 (사용자가 "레포" → "Repository" 로 바꾸면 import 깨짐) | 낮 | 헤더 매핑 dict 가 단일 진실원. 변경 시 거기만 갱신. import 폼에 헤더 미스매치 시 친절한 에러 메시지. |
| R7 | step_ids 가 콤마로 여러 ID 를 담는데 검색/필터 어떻게 할 것인가 | 낮 | Phase 1 은 TextField 단순 저장. 필요시 Phase 5 에서 `step_id__icontains` 검색만 추가. 정규화는 보류. |

---

## 12. 본 문서에서 다루지 **않은** 것 (의도적 제외)

- 담당자(사람) 자체의 마스터 데이터 관리 — 본 모델은 Endpoint 측 메타이지 HR 시스템이 아니다.
- Slack 알림 통합 — 매칭된 담당자에게 자동 멘션 같은 기능은 별도 PR 로.
- API v2 endpoint 노출 — 외부 시스템에서 매칭 결과를 fetch 하는 API. 필요해지면 추후.
- 다국어 — 현재 한국어 컬럼/UI 유지.

---

## 13. 다음 액션

1. **사용자 확인** — §0 의 D1~D5 결정사항 검토. 특히 D2 (정규화 여부), D3 (호스트 매핑 입력 방식).
2. 확정되면 **Phase 1** 부터 단일 커밋 단위로 진행.
3. 각 Phase 끝마다 사용자에게 짧은 진행 보고 (어떤 화면이 추가됐고, 다음 Phase 가 무엇인지).
