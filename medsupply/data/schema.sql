-- schema v1 (2026-08-19) — MedSupply Radar 통합 SQLite 스키마
--
-- 원칙
--   1. 이 파일은 이후 모든 태스크(데이터 생성·조회·분석·LLM·화면)가 소비하는 데이터 계약이다.
--      테이블명·컬럼명·상태값 문자열은 계약이므로 임의로 바꾸지 않는다.
--   2. 스키마 변경은 마이그레이션이 아니라 init_db 재생성으로만 반영한다(마이그레이션 없음).
--      따라서 이 파일에는 CREATE TABLE IF NOT EXISTS를 쓰지 않고 순수한 CREATE문만 둔다.
--      재생성(DROP 후 재적용)은 medsupply/data/db.py의 init_db(conn, drop=True)가 담당한다.
--   3. FK는 선언만으로는 강제되지 않는다. 커넥션에서 반드시 `PRAGMA foreign_keys = ON`을 켠다
--      (db.get_connection의 책임). 이 파일은 PRAGMA를 포함하지 않는다.
--
-- 타입 규약
--   TEXT    : 문자열, 그리고 모든 날짜·시각(ISO 8601. 날짜 '2026-08-01', 시각 '2026-08-01T09:30:00')
--   INTEGER : 수량·개수·불리언(0/1)·rowid 계열 PK
--   REAL    : 신뢰도·예측값 등 소수 수치
--
-- 감사 시각 기본값
--   writer 계약에 created_at/generated_at 인자가 없는 테이블은 DEFAULT로 로컬시각을 채운다.
--   Python의 datetime.now().isoformat(timespec='seconds')와 동일한 표기를 맞추기 위해 'localtime'을 쓴다.

-- ===========================================================================
-- 1. 마스터 — 성분 / 표기 변형 / 대체군 / 품목
-- ===========================================================================

-- 성분 마스터. 품목·대체군·공고 매핑이 공유하는 정규화 기준점.
CREATE TABLE ingredients (
    ingredient_code    TEXT PRIMARY KEY,           -- 성분 코드(내부 표준)
    ingredient_name_kr TEXT NOT NULL,              -- 한글 성분명
    ingredient_name_en TEXT,                       -- 영문 성분명
    atc_code           TEXT                        -- ATC 분류 코드
);

-- 공고 문면의 한/영/염 표기 변형을 성분코드로 정규화하기 위한 별칭 사전.
-- 매핑(map_extraction_to_items)은 LLM 미관여 결정적 조인이므로 이 표가 정확도의 근거가 된다.
CREATE TABLE ingredient_aliases (
    alias           TEXT NOT NULL,                 -- 표기 변형(예: 'Ceftriaxone', '세프트리악손나트륨수화물')
    ingredient_code TEXT NOT NULL,
    alias_type      TEXT,                          -- 'kr' | 'en' | 'salt' | 'brand' | 'abbr' (자유 문자열)
    PRIMARY KEY (alias, ingredient_code),
    FOREIGN KEY (ingredient_code) REFERENCES ingredients(ingredient_code)
);

-- 동일 성분·함량·제형·투여경로 대체군. 대체 후보 탐색(get_substitutes)의 단위.
CREATE TABLE substitute_groups (
    substitute_group_id TEXT PRIMARY KEY,
    ingredient_code     TEXT NOT NULL,
    strength            TEXT,                      -- 함량(예: '1g', '500mg')
    form                TEXT,                      -- 제형(예: '주사제', '정제')
    route               TEXT,                      -- 투여경로(예: '정맥', '경구')
    group_label         TEXT,                      -- 화면 표기용 라벨
    FOREIGN KEY (ingredient_code) REFERENCES ingredients(ingredient_code)
);

-- 병원 채택 의약품 마스터. 모든 시계열·판정·액션의 키는 item_id 하나로 단일화한다.
CREATE TABLE items (
    item_id             TEXT PRIMARY KEY,
    item_name           TEXT NOT NULL,             -- 품목명(제품명)
    standard_code       TEXT,                      -- 표준코드(바코드/KD코드)
    ingredient_code     TEXT,
    strength            TEXT,
    form                TEXT,
    route               TEXT,
    pack_size           INTEGER,                   -- 포장 단위 수량
    supplier            TEXT,                      -- 공급사
    is_essential        INTEGER NOT NULL DEFAULT 0 CHECK (is_essential IN (0, 1)),  -- 필수의약품 여부
    substitute_group_id TEXT,
    atc_code            TEXT,
    FOREIGN KEY (ingredient_code) REFERENCES ingredients(ingredient_code),
    FOREIGN KEY (substitute_group_id) REFERENCES substitute_groups(substitute_group_id)
);

-- ===========================================================================
-- 2. 재고·사용량·입고
-- ===========================================================================

-- 품목별 일자별 사용량·입고량·마감재고 스냅샷. 예측·소진일 추정의 유일한 입력 시계열.
CREATE TABLE stock_usage_daily (
    item_id       TEXT NOT NULL,
    date          TEXT NOT NULL,                   -- ISO 날짜 'YYYY-MM-DD'
    usage_qty     INTEGER NOT NULL DEFAULT 0,      -- 당일 사용량
    incoming_qty  INTEGER NOT NULL DEFAULT 0,      -- 당일 입고량
    closing_stock INTEGER NOT NULL DEFAULT 0,      -- 당일 마감 재고
    PRIMARY KEY (item_id, date),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- 발주 후 입고 예정/실적. 입고 지연(delivery_delay) 이상신호와 소진일 보정의 근거.
-- shipment_id는 계약상 INTEGER AUTOINCREMENT로 통일한다(writer가 rowid를 반환).
CREATE TABLE incoming_shipments (
    shipment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       TEXT NOT NULL,
    order_date    TEXT,                            -- 발주일
    expected_date TEXT,                            -- 입고 예정일
    expected_qty  INTEGER,                         -- 입고 예정 수량
    actual_date   TEXT,                            -- 실제 입고일(미입고면 NULL)
    actual_qty    INTEGER,                         -- 실제 입고 수량(미입고면 NULL)
    status        TEXT,                            -- 입고 상태(값 집합은 데이터 생성 태스크가 확정, CHECK 없음)
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- ===========================================================================
-- 3. 공급 공고 — 원문 / LLM 추출 / 품목 매핑
-- ===========================================================================

-- 수집된 공급 관련 공고 원문. notice_id는 TEXT(외부 공고 식별자 기반).
CREATE TABLE notices (
    notice_id     TEXT PRIMARY KEY,
    published_date TEXT NOT NULL,                  -- 공고일 'YYYY-MM-DD'
    title         TEXT NOT NULL,
    source        TEXT,                            -- 출처 기관·시스템
    source_url    TEXT,
    raw_text      TEXT,                            -- 공고 원문(발췌-원문 대조의 기준)
    notice_type   TEXT NOT NULL DEFAULT '기타'
        CHECK (notice_type IN ('공급중단', '공급부족', '정상화', '기타')),
    collected_at  TEXT                             -- 수집 시각
);

-- 공고 1건당 LLM 구조화 추출 결과 1행(멱등 갱신). payload_json 필드는 docs/data-model.md 부록 참조.
CREATE TABLE notice_extractions (
    notice_id      TEXT PRIMARY KEY,
    payload_json   TEXT NOT NULL,                  -- 추출 결과 JSON
    confidence     REAL,                           -- 0.0~1.0
    status         TEXT NOT NULL
        CHECK (status IN ('자동확정', '확인 필요', '확인 완료')),
    prompt_version TEXT,                           -- 프롬프트 레지스트리 버전
    provider       TEXT,                           -- 'anthropic' | 'openai'
    model          TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    FOREIGN KEY (notice_id) REFERENCES notices(notice_id)
);

-- 공고 → 병원 품목 매핑(LLM 미관여 결정적 조인 결과).
-- substitute_group_id는 매핑 시점의 대체군 스냅샷 값이라 FK를 걸지 않는다(계약 FK 목록에도 없음).
CREATE TABLE notice_item_map (
    notice_id           TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    substitute_group_id TEXT,
    match_basis         TEXT,                      -- 매칭 근거(예: 'standard_code', 'ingredient+strength+form')
    needs_review        INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
    PRIMARY KEY (notice_id, item_id),
    FOREIGN KEY (notice_id) REFERENCES notices(notice_id),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- ===========================================================================
-- 4. 분석 결과 — 위험 판정 / 수요 예측 / LLM 설명
-- ===========================================================================

-- run 단위 위험 판정 결과. run_id = f"{as_of.isoformat()}#{params_hash[:8]}" (결정적).
-- 위험 판정에는 LLM이 관여하지 않는다: 동일 입력 → 동일 판정.
CREATE TABLE risk_results (
    run_id              TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    as_of               TEXT NOT NULL,             -- 판정 기준일
    grade               TEXT NOT NULL
        CHECK (grade IN ('위험', '경고', '주의', '정상')),        -- 최종 등급(공고 상향 반영)
    base_grade          TEXT NOT NULL
        CHECK (base_grade IN ('위험', '경고', '주의', '정상')),   -- 공고 상향 전 등급
    escalated_by_notice INTEGER NOT NULL DEFAULT 0 CHECK (escalated_by_notice IN (0, 1)),
    -- 이상신호·활성 공고에서 결정적으로 유도되는 값이라 NULL이 존재할 이유가 없다.
    -- 미분류는 NULL이 아니라 'general' 하나로만 표현한다(이중 표현 금지).
    risk_type           TEXT NOT NULL DEFAULT 'general'
        CHECK (risk_type IN ('demand_surge', 'supply_halt', 'delivery_delay', 'composite', 'general')),
    score               INTEGER,                   -- 0~100 정렬용 점수
    days_to_stockout    INTEGER,                   -- 소진까지 남은 일수(추정 불가/소진 없음이면 NULL)
    depletion_date      TEXT,                      -- 예상 소진일(없으면 NULL)
    factors_json        TEXT NOT NULL DEFAULT '{}',-- RiskAssessment.to_evidence() 직렬화(근거 단일 정의)
    PRIMARY KEY (run_id, item_id),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- run 단위 수요 예측 결과. risk_results와 동일한 run_id를 공유한다.
CREATE TABLE forecasts (
    run_id             TEXT NOT NULL,
    item_id            TEXT NOT NULL,
    as_of              TEXT NOT NULL,
    horizon_days       INTEGER NOT NULL,           -- 예측 지평(일)
    avg_daily_forecast REAL,                       -- 일 평균 예측 사용량
    total_forecast     REAL,                       -- 지평 합계 예측 사용량
    daily_json         TEXT NOT NULL DEFAULT '[]', -- 일자별 예측값 배열 JSON
    PRIMARY KEY (run_id, item_id),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- 품목별 최신 LLM 설명 1행(멱등 갱신). 근거는 risk_results 최신 run 조회로 채운다.
CREATE TABLE llm_explanations (
    item_id        TEXT PRIMARY KEY,
    run_id         TEXT,                           -- 설명이 근거로 삼은 run
    payload_json   TEXT NOT NULL,
    prompt_version TEXT,
    provider       TEXT,
    model          TEXT,
    generated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- ===========================================================================
-- 5. 액션·운영 — 알림 / 조치 이력 / 발주 요청 / 메타
-- ===========================================================================

-- 알림. dedupe_key로 동일 사건의 중복 생성을 DB 레벨에서 차단한다(create_alert는 중복 시 None 반환).
-- item_id는 품목 무관 알림(배치 실패 등)을 위해 NULL을 허용한다.
CREATE TABLE alerts (
    alert_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    alert_type TEXT NOT NULL,                      -- 알림 유형 키
    item_id    TEXT,                               -- NULL 허용
    title      TEXT NOT NULL,
    body       TEXT,
    severity   TEXT,                               -- 표시 강도(값 집합은 UI 태스크가 확정, CHECK 없음)
    dedupe_key TEXT NOT NULL UNIQUE,
    is_read    INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- 약사 조치 이력. risk_grade_before/after는 조치 전후 등급 스냅샷(문자열, 미기록 시 NULL).
-- order_id는 order_requests.order_id를 참조하는 값이지만, 계약 FK 목록에 없어 제약은 걸지 않는다.
CREATE TABLE action_history (
    history_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    item_id           TEXT NOT NULL,
    action_type       TEXT NOT NULL,               -- 조치 유형(대체 검토·발주 요청 등)
    owner             TEXT,                        -- 담당자
    note              TEXT,
    status            TEXT NOT NULL DEFAULT '진행 중'
        CHECK (status IN ('진행 중', '완료')),
    order_id          INTEGER,                     -- 연계 발주 요청(없으면 NULL)
    risk_grade_before TEXT,
    risk_grade_after  TEXT,
    result_note       TEXT,
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- 발주 요청. writer가 rowid(order_id)를 반환해 action_history와 연결한다.
CREATE TABLE order_requests (
    order_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    item_id      TEXT NOT NULL,
    supplier     TEXT,
    quantity     INTEGER NOT NULL,
    desired_date TEXT,                             -- 희망 납기일
    owner        TEXT,
    reason       TEXT,
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- 데이터셋·앱 공용 단일 키-값 메타(dataset_meta와 앱 meta 통합).
-- 키: seed, config_hash, content_hash, base_date, item_count, data_version, generated_at
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ===========================================================================
-- 6. 인덱스
-- ===========================================================================

-- 계약에 명시된 인덱스
CREATE INDEX idx_stock_usage_daily_date ON stock_usage_daily(date);
CREATE INDEX idx_incoming_shipments_item_expected ON incoming_shipments(item_id, expected_date);
CREATE INDEX idx_notice_item_map_item ON notice_item_map(item_id);
CREATE INDEX idx_risk_results_item ON risk_results(item_id);
CREATE INDEX idx_alerts_created_at ON alerts(created_at DESC);
CREATE INDEX idx_action_history_item_created ON action_history(item_id, created_at DESC);

-- 추가 인덱스(조회 계약에서 자주 쓰이는 경로)
CREATE INDEX idx_items_ingredient ON items(ingredient_code);              -- list_items 성분 필터
CREATE INDEX idx_items_substitute_group ON items(substitute_group_id);    -- get_substitutes 대체군 조회
CREATE INDEX idx_ingredient_aliases_code ON ingredient_aliases(ingredient_code);  -- 별칭 → 성분 역조회
CREATE INDEX idx_substitute_groups_ingredient ON substitute_groups(ingredient_code);
CREATE INDEX idx_notices_published ON notices(published_date DESC);       -- 공고 목록 최신순
CREATE INDEX idx_risk_results_run_grade ON risk_results(run_id, grade);   -- 등급 필터·집계
CREATE INDEX idx_forecasts_item ON forecasts(item_id);                    -- 품목 상세 예측 조회
CREATE INDEX idx_order_requests_item_created ON order_requests(item_id, created_at DESC);
