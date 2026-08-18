# MedSupply Radar 데이터 모델 (schema v1, 2026-08-19)

이 문서는 `medsupply/data/schema.sql`(schema v1)의 계약 설명서다. 테이블 16종, 상태값 집합,
`run_id` 규약, 라벨(ground truth) 포맷, `meta` 키, LLM 추출 payload 초안을 정의한다.

**원칙**

- 이 스키마는 데이터 생성·조회·분석·LLM·화면 전 계층이 공유하는 **단일 계약**이다. 테이블명·컬럼명·
  상태값 문자열은 임의로 바꾸지 않는다.
- **마이그레이션은 없다.** 스키마 변경은 `init_db(conn, drop=True)` 재생성으로만 반영한다. 그래서
  `schema.sql`은 `CREATE TABLE IF NOT EXISTS`를 쓰지 않고 순수 `CREATE`문만 담는다.
- FK는 선언만으로 강제되지 않는다. 커넥션에서 **`PRAGMA foreign_keys = ON`**을 반드시 켠다
  (`db.get_connection`의 책임). `schema.sql`에는 PRAGMA·INSERT를 두지 않는다.
- 식별자는 `item_id` 하나로 단일화한다(`drug_id`/`item_code` 병립 금지). `notice_id`는 TEXT다.

**타입 규약**

| 구분 | 타입 | 예 |
| --- | --- | --- |
| 문자열·식별자 | `TEXT` | `item_id`, `grade` |
| 날짜·시각 | `TEXT` (ISO 8601) | `'2026-08-01'`, `'2026-08-01T09:30:00'` |
| 수량·개수·불리언(0/1)·rowid PK | `INTEGER` | `usage_qty`, `is_essential`, `alert_id` |
| 신뢰도·예측 수치 | `REAL` | `confidence`, `avg_daily_forecast` |

`alert_id`·`history_id`·`order_id`·`shipment_id`는 모두 `INTEGER PRIMARY KEY AUTOINCREMENT`다
(writer가 rowid를 반환하는 계약). 이 때문에 SQLite 내부 테이블 `sqlite_sequence`가 함께 생성되므로,
테이블 목록을 검사할 때는 `name NOT LIKE 'sqlite_%'`로 걸러야 한다.

---

## 1. 테이블 16종

### 1.1 마스터

#### `ingredients` — 성분 마스터

품목·대체군·공고 매핑이 공유하는 성분 정규화 기준점이다.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `ingredient_code` | TEXT | 성분 코드(내부 표준) | PK |
| `ingredient_name_kr` | TEXT | 한글 성분명 | NOT NULL |
| `ingredient_name_en` | TEXT | 영문 성분명 | |
| `atc_code` | TEXT | ATC 분류 코드 | |

#### `ingredient_aliases` — 성분 표기 변형 사전

공고 문면의 한글/영문/염 표기 변형을 성분코드로 정규화한다(결정적 매핑의 정확도 근거).

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `alias` | TEXT | 표기 변형(예: `Ceftriaxone`, `세프트리악손나트륨수화물`) | PK(alias, ingredient_code) |
| `ingredient_code` | TEXT | 정규화 대상 성분 | PK, FK → `ingredients` |
| `alias_type` | TEXT | `kr`/`en`/`salt`/`brand`/`abbr` 등 구분(자유 문자열) | |

#### `substitute_groups` — 대체군

동일 성분·함량·제형·투여경로 묶음. 대체 후보 탐색(`get_substitutes`)의 단위다.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `substitute_group_id` | TEXT | 대체군 ID | PK |
| `ingredient_code` | TEXT | 성분 | NOT NULL, FK → `ingredients` |
| `strength` | TEXT | 함량(`1g`, `500mg`) | |
| `form` | TEXT | 제형(`주사제`, `정제`) | |
| `route` | TEXT | 투여경로(`정맥`, `경구`) | |
| `group_label` | TEXT | 화면 표기용 라벨 | |

#### `items` — 병원 채택 의약품 마스터

모든 시계열·판정·액션이 참조하는 품목 원장이다.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `item_id` | TEXT | 품목 ID(전 계층 단일 키) | PK |
| `item_name` | TEXT | 품목명(제품명) | NOT NULL |
| `standard_code` | TEXT | 표준코드(바코드/KD코드) | |
| `ingredient_code` | TEXT | 성분 | FK → `ingredients` |
| `strength` | TEXT | 함량 | |
| `form` | TEXT | 제형 | |
| `route` | TEXT | 투여경로 | |
| `pack_size` | INTEGER | 포장 단위 수량 | |
| `supplier` | TEXT | 공급사 | |
| `is_essential` | INTEGER | 필수의약품 여부 | NOT NULL, DEFAULT 0, CHECK(0/1) |
| `substitute_group_id` | TEXT | 대체군 | FK → `substitute_groups` |
| `atc_code` | TEXT | ATC 분류 코드 | |

### 1.2 재고·입고

#### `stock_usage_daily` — 일자별 사용량·재고

예측과 소진일 추정의 **유일한 입력 시계열**이다.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `item_id` | TEXT | 품목 | PK(item_id, date), FK → `items` |
| `date` | TEXT | 일자 `YYYY-MM-DD` | PK |
| `usage_qty` | INTEGER | 당일 사용량 | NOT NULL, DEFAULT 0 |
| `incoming_qty` | INTEGER | 당일 입고량 | NOT NULL, DEFAULT 0 |
| `closing_stock` | INTEGER | 당일 마감 재고 | NOT NULL, DEFAULT 0 |

인덱스: `(date)` — 기준일 기준 스냅샷 조회.

#### `incoming_shipments` — 입고 예정·실적

입고 지연(`delivery_delay`) 이상신호와 소진일 보정의 근거다.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `shipment_id` | INTEGER | 입고 건 ID | PK AUTOINCREMENT |
| `item_id` | TEXT | 품목 | NOT NULL, FK → `items` |
| `order_date` | TEXT | 발주일 | |
| `expected_date` | TEXT | 입고 예정일 | |
| `expected_qty` | INTEGER | 입고 예정 수량 | |
| `actual_date` | TEXT | 실제 입고일(미입고 NULL) | |
| `actual_qty` | INTEGER | 실제 입고 수량(미입고 NULL) | |
| `status` | TEXT | 입고 상태 | CHECK 없음(값 집합은 데이터 생성 태스크가 확정) |

인덱스: `(item_id, expected_date)`.

### 1.3 공급 공고

#### `notices` — 공고 원문

수집된 공급 관련 공고 1건이다.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `notice_id` | TEXT | 공고 ID(외부 식별자 기반) | PK |
| `published_date` | TEXT | 공고일 | NOT NULL |
| `title` | TEXT | 제목 | NOT NULL |
| `source` | TEXT | 출처 기관·시스템 | |
| `source_url` | TEXT | 원문 URL | |
| `raw_text` | TEXT | 공고 원문(발췌-원문 대조 기준) | |
| `notice_type` | TEXT | 공고 유형 | NOT NULL, DEFAULT `'기타'`, CHECK(§2.4) |
| `collected_at` | TEXT | 수집 시각 | |

인덱스: `(published_date DESC)`.

#### `notice_extractions` — LLM 구조화 추출 결과

공고 1건당 1행(멱등 갱신). 추출 payload 필드는 부록 §6 참조.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `notice_id` | TEXT | 공고 | PK, FK → `notices` |
| `payload_json` | TEXT | 추출 결과 JSON | NOT NULL |
| `confidence` | REAL | 추출 신뢰도 0.0~1.0 | |
| `status` | TEXT | 확인 상태 | NOT NULL, CHECK(§2.3) |
| `prompt_version` | TEXT | 프롬프트 레지스트리 버전 | |
| `provider` | TEXT | `anthropic` / `openai` | |
| `model` | TEXT | 모델 ID | |
| `created_at` | TEXT | 생성 시각 | NOT NULL, DEFAULT 현재 로컬시각 |

#### `notice_item_map` — 공고 ↔ 품목 매핑

추출 결과를 병원 품목에 잇는 **결정적 조인**(LLM 미관여) 산출물이다.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `notice_id` | TEXT | 공고 | PK(notice_id, item_id), FK → `notices` |
| `item_id` | TEXT | 품목 | PK, FK → `items` |
| `substitute_group_id` | TEXT | 매핑 시점 대체군 스냅샷 | FK 없음(의도적, §3) |
| `match_basis` | TEXT | 매칭 근거(`standard_code`, `ingredient+strength+form` 등) | |
| `needs_review` | INTEGER | 사람 확인 필요 여부 | NOT NULL, DEFAULT 0, CHECK(0/1) |

인덱스: `(item_id)`.

### 1.4 분석 결과

#### `risk_results` — run 단위 위험 판정

`factors_json`은 `RiskAssessment.to_evidence()` 직렬화이며, **근거 패키지의 단일 정의**다.
LLM 설명 생성은 이 테이블의 최신 run을 조회해 근거를 채운다(근거 이중 정의 금지).

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `run_id` | TEXT | 판정 run(§3) | PK(run_id, item_id) |
| `item_id` | TEXT | 품목 | PK, FK → `items` |
| `as_of` | TEXT | 판정 기준일 | NOT NULL |
| `grade` | TEXT | 최종 등급(공고 상향 반영) | NOT NULL, CHECK(§2.1) |
| `base_grade` | TEXT | 공고 상향 전 등급 | NOT NULL, CHECK(§2.1) |
| `escalated_by_notice` | INTEGER | 활성 공고로 상향되었는지 | NOT NULL, DEFAULT 0, CHECK(0/1) |
| `risk_type` | TEXT | 위험 유형 | NOT NULL, DEFAULT `'general'`, CHECK(`demand_surge`, `supply_halt`, `delivery_delay`, `composite`, `general`) |
| `score` | INTEGER | 정렬용 점수(0~100) | |
| `days_to_stockout` | INTEGER | 소진까지 남은 일수 | 추정 불가·소진 없음이면 NULL |
| `depletion_date` | TEXT | 예상 소진일 | 없으면 NULL |
| `factors_json` | TEXT | `to_evidence()` 직렬화 | NOT NULL, DEFAULT `'{}'` |

인덱스: `(item_id)`, `(run_id, grade)`.

#### `forecasts` — run 단위 수요 예측

`risk_results`와 동일한 `run_id`를 공유해 같은 판정 회차의 예측을 잇는다.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `run_id` | TEXT | 예측 run | PK(run_id, item_id) |
| `item_id` | TEXT | 품목 | PK, FK → `items` |
| `as_of` | TEXT | 기준일 | NOT NULL |
| `horizon_days` | INTEGER | 예측 지평(일) | NOT NULL |
| `avg_daily_forecast` | REAL | 일 평균 예측 사용량 | |
| `total_forecast` | REAL | 지평 합계 예측 사용량 | |
| `daily_json` | TEXT | 일자별 예측값 배열 JSON | NOT NULL, DEFAULT `'[]'` |

#### `llm_explanations` — 품목별 LLM 설명

품목당 최신 1행(멱등 갱신). 위험 판정 자체에는 관여하지 않는 **설명 전용** 산출물이다.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `item_id` | TEXT | 품목 | PK, FK → `items` |
| `run_id` | TEXT | 근거로 삼은 run | |
| `payload_json` | TEXT | 설명 JSON | NOT NULL |
| `prompt_version` | TEXT | 프롬프트 버전 | |
| `provider` | TEXT | `anthropic` / `openai` | |
| `model` | TEXT | 모델 ID | |
| `generated_at` | TEXT | 생성 시각 | NOT NULL, DEFAULT 현재 로컬시각 |

### 1.5 액션·운영

#### `alerts` — 알림

`dedupe_key` UNIQUE로 동일 사건의 중복 생성을 DB 레벨에서 차단한다(`create_alert`는 중복 시 `None`).

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `alert_id` | INTEGER | 알림 ID | PK AUTOINCREMENT |
| `created_at` | TEXT | 생성 시각 | NOT NULL, DEFAULT 현재 로컬시각 |
| `alert_type` | TEXT | 알림 유형 키 | NOT NULL |
| `item_id` | TEXT | 품목 | **NULL 허용**(품목 무관 알림), FK → `items` |
| `title` | TEXT | 제목 | NOT NULL |
| `body` | TEXT | 본문 | |
| `severity` | TEXT | 표시 강도 | CHECK 없음(값 집합은 UI 태스크 확정) |
| `dedupe_key` | TEXT | 중복 차단 키 | NOT NULL, UNIQUE |
| `is_read` | INTEGER | 읽음 여부 | NOT NULL, DEFAULT 0, CHECK(0/1) |

인덱스: `(created_at DESC)`.

#### `action_history` — 약사 조치 이력

위험 인지 → 조치 → 결과의 흐름을 남긴다.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `history_id` | INTEGER | 이력 ID | PK AUTOINCREMENT |
| `created_at` | TEXT | 생성 시각 | NOT NULL, DEFAULT 현재 로컬시각 |
| `item_id` | TEXT | 품목 | NOT NULL, FK → `items` |
| `action_type` | TEXT | 조치 유형(대체 검토·발주 요청 등) | NOT NULL |
| `owner` | TEXT | 담당자 | |
| `note` | TEXT | 메모 | |
| `status` | TEXT | 진행 상태 | NOT NULL, DEFAULT `'진행 중'`, CHECK(`진행 중`, `완료`) |
| `order_id` | INTEGER | 연계 발주 요청 | FK 없음(의도적, §3) |
| `risk_grade_before` | TEXT | 조치 전 등급 스냅샷 | CHECK 없음(미기록 NULL 허용) |
| `risk_grade_after` | TEXT | 조치 후 등급 스냅샷 | CHECK 없음 |
| `result_note` | TEXT | 결과 메모 | |

인덱스: `(item_id, created_at DESC)`.

#### `order_requests` — 발주 요청

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `order_id` | INTEGER | 발주 요청 ID | PK AUTOINCREMENT |
| `created_at` | TEXT | 생성 시각 | NOT NULL, DEFAULT 현재 로컬시각 |
| `item_id` | TEXT | 품목 | NOT NULL, FK → `items` |
| `supplier` | TEXT | 공급사 | |
| `quantity` | INTEGER | 요청 수량 | NOT NULL |
| `desired_date` | TEXT | 희망 납기일 | |
| `owner` | TEXT | 요청자 | |
| `reason` | TEXT | 요청 사유 | |

인덱스: `(item_id, created_at DESC)`.

#### `meta` — 데이터셋·앱 공용 키-값 메타

dataset_meta와 앱 meta를 단일 테이블로 통합해 드리프트를 막는다. 키 목록은 §5 참조.

| 컬럼 | 타입 | 의미 | 제약 |
| --- | --- | --- | --- |
| `key` | TEXT | 메타 키 | PK |
| `value` | TEXT | 값(문자열 직렬화) | NOT NULL |

---

## 2. 상태값 집합

### 2.1 위험등급 (`risk_results.grade`, `base_grade`)

판정 결과를 나타내는 **4단계 서열 축**이다. 코드 enum·DB CHECK·화면 표기가 모두 같은 문자열을 쓴다.

| 등급 | 의미 | UI CSS 클래스 |
| --- | --- | --- |
| `위험` | 소진 임박 — 즉시 조치 | `critical` |
| `경고` | 단기 내 소진 가능 | `high` |
| `주의` | 관찰 필요 | `watch` |
| `정상` | 특이사항 없음 | `safe` |

기본 판정은 소진 예상일수 기준(≤7 위험 / ≤14 경고 / ≤30 주의 / 그 외 정상)이며, 활성 공고가 있으면
1단계 상향(위험에서 캡)한다. 상향 전 값은 `base_grade`, 상향 여부는 `escalated_by_notice`에 남긴다.

### 2.2 공급상태 라벨 — **위험등급과 별개 축**

공급 측 사실(공고·품절 여부)을 나타내는 라벨로, 위험등급과 혼동하면 안 된다. 등급은 *우리 재고가
언제 바닥나는가*, 공급상태는 *시장/공급사가 어떤 상태인가*를 말한다. 같은 `정상` 등급 품목이
`공급중단` 라벨을 가질 수 있고(재고가 충분한 경우), 반대도 가능하다.

| 라벨 | 의미 |
| --- | --- |
| `현재 품절` | 지금 공급이 끊긴 상태 |
| `품절 예상` | 곧 품절이 예상되는 상태 |
| `공급중단` | 공급중단 공고가 활성인 상태 |
| `정상화` | 공급 재개·정상화가 확인된 상태 |

이 라벨은 별도 컬럼으로 저장하지 않고 공고(`notices.notice_type`)·매핑(`notice_item_map`)·재고
상태에서 파생해 표기한다(표기 문자열은 불변 계약).

### 2.3 공고 확인상태 (`notice_extractions.status`)

| 값 | 의미 |
| --- | --- |
| `자동확정` | 신뢰도·대조 검증 통과로 사람 확인 없이 확정 |
| `확인 필요` | 사람 확인 대기(보수적 기본값으로 등급 상향에는 **포함**) |
| `확인 완료` | 사람이 검토해 확정 |

### 2.4 공고 유형 (`notices.notice_type`)

| 값 | 의미 | 활성 공고 여부 |
| --- | --- | --- |
| `공급중단` | 공급 중단 안내 | 활성 대상 |
| `공급부족` | 공급 부족·제한 공급 안내 | 활성 대상 |
| `정상화` | 공급 재개 안내 | 비활성(상향 없음) |
| `기타` | 위에 해당하지 않는 공고 | 비활성 |

활성 공고 = 유형이 `공급중단`/`공급부족`이고, 재개예정일이 없거나 `as_of` 이상인 매핑이 존재.

### 2.5 위험 유형 (`risk_results.risk_type`)

`demand_surge`(수요 급증) · `supply_halt`(공급 중단) · `delivery_delay`(입고 지연) ·
`composite`(복합) · `general`(일반). 이상신호와 활성 공고 유무에서 결정적으로 유도한다.

**미분류는 NULL이 아니라 `general` 하나로만 표현한다.** 컬럼이 `NOT NULL DEFAULT 'general'`이므로
"유형 없음"이 NULL과 `'general'`로 이중화될 여지가 없다. 조회 쪽에서 `IS NULL` 분기를 둘 필요도 없다.

### 2.6 조치 상태 (`action_history.status`)

`진행 중` · `완료`.

---

## 3. `run_id` 형식과 결정성 원칙

```
run_id = f"{as_of.isoformat()}#{params_hash[:8]}"     예: "2026-08-01#a1b2c3d4"
```

- `as_of`: 판정 기준일. `params_hash`: 분석 파라미터(`analytics_params.toml`)의 해시 앞 8자.
- **위험 판정에 LLM은 관여하지 않는다.** 동일 입력(데이터 + 파라미터 + 기준일)이면 언제 돌려도
  동일한 등급·점수·근거가 나온다. 같은 `run_id`가 곧 같은 판정을 뜻한다.
- 룩어헤드 금지: 판정 입력은 `as_of` 이하 데이터로만 구성한다.
- `risk_results`와 `forecasts`는 같은 `run_id`를 공유한다.
- LLM은 설명(`llm_explanations`)과 공고 추출(`notice_extractions`)에만 쓰이며, 판정 결과를 바꾸지
  않는다. 그래서 재실행 시 설명 문구는 달라질 수 있어도 등급은 달라지지 않는다.

### FK를 걸지 않은 컬럼(의도적)

계약에 명시된 FK만 선언한다. 아래 두 컬럼은 참조 성격이지만 제약을 걸지 않는다.

| 컬럼 | 이유 |
| --- | --- |
| `notice_item_map.substitute_group_id` | 매핑 시점의 대체군 스냅샷 값(마스터 재생성 시 값이 남아야 함) |
| `action_history.order_id` | 발주 없이 남는 조치 이력이 다수이고, 이력은 발주 삭제와 무관하게 보존 |

---

## 4. 라벨(ground truth) JSON 포맷

탐지 성능 측정용 정답 라벨이다. 위치: `data/scenarios/ground_truth/` (예: `standard_v1.json`).

```json
[
  {
    "item_id": "ITEM-0042",
    "scenario_type": "supply_halt",
    "onset_date": "2026-07-18",
    "stockout_date": "2026-08-05",
    "params_ref": "scenarios/supply_halt_v1"
  }
]
```

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `item_id` | str | 대상 품목 |
| `scenario_type` | str | `demand_surge` \| `supply_halt` \| `delivery_delay` \| `composite` |
| `onset_date` | str | 시나리오 발생 시작일(ISO 날짜) |
| `stockout_date` | str | 실제 소진 발생일(ISO 날짜) |
| `params_ref` | str | 주입에 쓴 시나리오 파라미터 참조 키 |

- **파일에 없는 품목은 정상**으로 규정한다. 오탐률 분모 = 전체 품목 수 − 라벨 품목 수.
- **격리 원칙(중요):** 이 파일은 앱·분석 코드가 **절대 참조하지 않는다.** `medsupply/` 전체와
  `app.py`는 `data/scenarios/`·`ground_truth` 경로·모듈을 어떤 형태로도 import하거나 읽지 않는다.
  참조가 허용되는 곳은 측정 스크립트(`scripts/measure_detection.py`)와 `eval/`뿐이다. 라벨을 보고
  판정하면 탐지 성능 측정이 무의미해지기 때문이다.
- **미구현 — 후속 태스크 요구사항:** 이 격리는 후속 태스크가 `tests/test_isolation.py` 하나로 통합해
  저장소 전역 정적 검사로 강제해야 한다(역방향 `scripts/datagen` → `medsupply.analytics` 참조도 함께
  검사, 허용 목록은 `scripts/measure_detection.py`와 `eval/`뿐). schema v1 시점에는 아직 그 테스트가
  없으므로 지금은 규약으로만 존재한다.

---

## 5. `meta` 키 목록

| 키 | 예시 값 | 의미 |
| --- | --- | --- |
| `seed` | `20260819` | 데이터 생성 난수 시드(재현성) |
| `config_hash` | `9f2c1ab4…` | 데이터 생성 설정 해시 |
| `content_hash` | `3d77ee01…` | 생성된 데이터 내용 해시(스냅샷 동일성 검증) |
| `base_date` | `2026-08-01` | 스냅샷 기준일(사이드바 기준시각 표기의 출처) |
| `item_count` | `120` | 품목 수(사이드바 표기의 출처) |
| `data_version` | `7` | 쓰기마다 증가하는 데이터 버전(캐시 무효화 신호) |
| `generated_at` | `2026-08-01T09:30:00` | 스냅샷 생성 시각 |

- 값은 모두 문자열로 저장한다(정수도 문자열 직렬화). 읽는 쪽에서 필요한 타입으로 변환한다.
- 모든 쓰기는 `writer.py`를 경유하며, writer의 모든 함수는 `data_version`을 증가시킨다. 화면 캐시는
  이 값을 키로 삼아 스테일 데이터를 피한다.
- 사이드바의 "시나리오"는 유형 4종 정적 문구만 표기하고, 개수·상세는 앱에서 참조하지 않는다.

---

## 6. 부록 — `notice_extractions.payload_json` 필드 초안 (v1 초안)

**v1 초안이다.** 실제 스키마는 LLM 추출 태스크에서 Pydantic 모델로 확정하며, 확정 시 이 절을 갱신한다.

```json
{
  "product_names": ["세프트리악손주 1g"],
  "ingredient_names": ["세프트리악손나트륨"],
  "reason": "제조소 설비 점검에 따른 생산 중단",
  "halt_start_date": "2026-07-20",
  "expected_restart_date": "2026-09-15",
  "notice_type": "공급중단",
  "evidence_quotes": ["당사 사정으로 2026년 7월 20일부터 공급이 중단됩니다."]
}
```

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `product_names` | list[str] | 공고에 등장한 제품명 |
| `ingredient_names` | list[str] | 공고에 등장한 성분명(별칭 사전으로 정규화) |
| `reason` | str | 공급 차질 사유 |
| `halt_start_date` | str \| null | 공급 중단 시작일(ISO 날짜) |
| `expected_restart_date` | str \| null | 공급 재개 예정일(ISO 날짜, 활성 공고 판정에 사용) |
| `notice_type` | str | §2.4 값 집합 |
| `evidence_quotes` | list[str] | 원문 발췌(발췌-원문 대조 검증용) |

발췌는 원문에 실제로 존재해야 하며, 대조에 실패하거나 신뢰도가 낮으면 `status = '확인 필요'`로
내려 사람 확인 대기 큐에 넣는다.

---

## 7. 검증

```bash
.venv/bin/python -m pytest tests/data/test_schema.py -v
```

`tests/data/test_schema.py`가 이 문서의 계약(테이블 16종, FK 강제, 등급·상태 CHECK, `dedupe_key`
UNIQUE, 복합 PK, AUTOINCREMENT rowid, 필수 인덱스)을 회귀 테스트로 고정한다.
