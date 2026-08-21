# 인터페이스 색인

계층별 공개 함수/클래스 시그니처 색인이다. 소스에서 추출해 수기로 정리했다 — 각 항목은
"시그니처 + 한 줄 설명"이며, 언더스코어(`_`)로 시작하는 비공개 헬퍼는 원칙적으로 싣지
않는다(계층 진입점 이해에 필요한 경우에만 본문에서 산문으로 언급). 상세 규칙·예외 조건은
각 함수의 docstring이 원천이며, 이 문서는 색인일 뿐이다 — 불일치 시 소스가 우선한다.

목차: [data](#data) · [analytics](#analytics) · [llm](#llm) · [services](#services) ·
[eval](#eval) · [scripts](#scripts-cli)

---

## data

### `medsupply/data/queries.py` — 읽기 전용 조회

INSERT/UPDATE/DELETE 없음. 모든 함수가 결정적 정렬(ORDER BY 명시)로 반환한다.

```python
def list_items(conn, *, ingredient_code=None, form=None, supplier=None, grade=None,
                essential_only=False, search=None, run_id=None) -> pd.DataFrame
```
items ⨝ ingredients LEFT JOIN risk_results(최신/지정 run) 목록. run_id 생략 시 최신 run.

```python
def get_item(conn, item_id: str) -> dict
```
items ⨝ ingredients 1행. 없으면 `KeyError`.

```python
def get_daily_series(conn, item_id: str, start: date | None = None,
                      end: date | None = None) -> pd.DataFrame
```
`stock_usage_daily` 시계열(date, usage_qty, incoming_qty, closing_stock).

```python
def get_current_stock_map(conn) -> pd.DataFrame
```
전 품목 최신 closing_stock 일괄 조회(item_id, current_stock) — 단일 SQL, 품목별 반복 없음.

```python
def get_substitutes(conn, item_id: str, *, same_condition_only: bool = True,
                     as_of: date | None = None) -> pd.DataFrame
```
같은 대체군(+ 옵션 시 동일 성분 타 대체군) 품목 목록 + current_stock. `as_of` 지정 시
그 시점 이하 재고만 본다(룩어헤드 차단).

```python
def get_incoming_shipments(conn, item_id: str | None = None, *,
                            pending_only: bool = True) -> pd.DataFrame
```
입고 예정/실적 목록. `pending_only=True`(기본)면 `actual_date IS NULL` 건만.

```python
def get_notices(conn, *, item_id: str | None = None, status: str | None = None) -> pd.DataFrame
```
공고 목록 + 추출 상태(status)·신뢰도(confidence)·매핑 품목 수(mapped_count).

```python
def get_notice_detail(conn, notice_id: str) -> dict | None
```
공고 1건 상세(payload 파싱 + 매핑 리스트). 없으면 `None`.

```python
def get_active_notice_map(conn, as_of: date) -> pd.DataFrame
```
활성 공고 매핑(공급중단/공급부족 · published_date<=as_of · 재개일 미도래). 룩어헤드 차단.

```python
def get_latest_runs(conn, n: int = 2) -> list[str]
```
최신 run과 **같은 params_hash 패밀리**의 run_id를 as_of 내림차순 최대 n개.

```python
def get_risk_results(conn, run_id: str) -> pd.DataFrame
```
지정 run의 위험 판정 결과 전체.

```python
def get_explanation(conn, item_id: str) -> dict | None
```
품목의 저장된 AI 근거 설명(`llm_explanations`, item_id PK). 없으면 `None`.

```python
def get_forecast(conn, run_id: str, item_id: str) -> dict | None
```
지정 run·품목의 예측 1행(daily_json 파싱 포함). 없으면 `None`.

```python
def list_action_history(conn, *, item_id=None, ingredient_code=None, risk_type=None,
                         limit: int | None = None) -> pd.DataFrame
```
조치 이력 최신순(품목명 포함).

```python
def fetch_alerts(conn, *, unread_only: bool = False, limit: int = 50) -> pd.DataFrame
```
알림 최신순 목록.

```python
def get_meta(conn) -> dict
```
`meta` 테이블 전체.

### `medsupply/data/writer.py` — 쓰기 단일 경로

이 모듈 밖에서 INSERT/UPDATE/DELETE 하지 않는다. **모든 공개 함수는 성공 시
`meta.data_version`을 정확히 1 증가**시키며(Streamlit 캐시 무효화 신호), 각자 자체
트랜잭션(`with conn:`)으로 원자적이다.

```python
def save_risk_results(conn, results: pd.DataFrame, run_id: str, as_of: date) -> None
```
`risk_results` 저장. 같은 run_id 재저장 시 DELETE 후 INSERT(멱등). grade/base_grade가
`{위험,경고,주의,정상}` 밖이면 `ValueError`.

```python
def save_forecasts(conn, forecasts: pd.DataFrame, run_id: str, as_of: date) -> None
```
`forecasts` 저장(멱등, save_risk_results와 동일 규칙).

```python
def save_notice_extraction(conn, notice_id: str, payload: dict, confidence: float,
                            status: str, prompt_version: str, provider: str, model: str,
                            mapped: list[dict]) -> None
```
`notice_extractions` INSERT OR REPLACE + `notice_item_map` 전량 교체(멱등). status가
`{자동확정,확인 필요,확인 완료}` 밖이면 `ValueError`.

```python
def save_explanation(conn, item_id: str, payload: dict, prompt_version: str, provider: str,
                      model: str, run_id: str) -> None
```
`llm_explanations` INSERT OR REPLACE(item_id PK). generated_at은 호출 시각.

```python
def save_action_history(conn, item_id: str, action_type: str, owner: str, note: str,
                         status: str = "진행 중", order_id: int | None = None,
                         risk_type: str | None = None) -> int
```
`action_history` INSERT, rowid 반환. status가 `{진행 중,완료}` 밖이면 `ValueError`.

```python
def save_order_request(conn, item_id: str, supplier: str, quantity: int, desired_date: str,
                        owner: str, reason: str) -> int
```
`order_requests` INSERT, rowid 반환.

```python
def create_alert(conn, alert_type: str, item_id: str | None, title: str, body: str | None,
                  severity: str, dedupe_key: str) -> int | None
```
`alerts` INSERT, rowid 반환. `dedupe_key` UNIQUE 충돌 시 예외를 삼키고 `None` 반환.

```python
def mark_alert_read(conn, alert_id: int) -> None
```
`alerts.is_read = 1`.

```python
def set_notice_status(conn, notice_id: str, status: str) -> None
```
`notice_extractions.status` 갱신. 존재하지 않는 notice_id면 `ValueError`.

### `medsupply/data/db.py` — 연결·스키마

```python
def get_connection(db_path: str | Path = settings.DB_PATH) -> sqlite3.Connection
```
WAL + `PRAGMA foreign_keys=ON` + Row factory가 설정된 커넥션.

```python
def init_db(conn, *, drop: bool = False) -> None
```
`schema.sql`(16개 테이블) 적용. `drop=True`면 자식→부모 순 DROP 후 재적용.

---

## analytics

### `medsupply/analytics/pipeline.py` — 평가 파이프라인 결선(이 모듈만 DB 어댑터를 가짐)

```python
def build_item_inputs(items_df, usage_df, receipts_df, notice_map_df, as_of: date) -> list[ItemInputs]
```
품목·시계열·입고·공고 매핑 DataFrame → `ItemInputs` 목록(item_id 오름차순, 룩어헤드 차단).

```python
def assess_item(inputs: ItemInputs, params: AnalyticsParams) -> RiskAssessment
```
단일 품목 순수 함수 결선: forecast → anomalies → depletion → grade → risk_type → score.

```python
def assess_all(items: Sequence[ItemInputs], params: AnalyticsParams) -> list[RiskAssessment]
```
item_id 오름차순으로 `assess_item` 반복(입력 순서 무관, 출력 결정성 보장).

```python
def assess_snapshot(conn, as_of: date, params: AnalyticsParams | None = None) -> pd.DataFrame
```
스냅샷 전 품목 평가 진입점(배치·화면·측정·재현성 검증이 공유). `params=None`이면
`load_params()` 기본값. 저장(run_id 채번·writer 호출)은 호출부(`scripts/run_risk_batch.py`)
책임.

### `medsupply/analytics/asof.py` — as_of 시점 재구성 술어(순수 함수, 상호배타)

```python
def is_on_or_before(value, as_of: date) -> bool
def is_strictly_after(value, as_of: date) -> bool
def arrived_by(actual_date, as_of: date) -> bool          # actual_date <= as_of
def is_overdue_at(expected_date, actual_date, as_of: date) -> bool   # 연체: 예정 경과 + 미도착
def is_pending_at(expected_date, actual_date, as_of: date) -> bool   # 예정: 미래 예정 + 미도착
```
백테스트에서 `actual_date`(도착 스탬프)는 as_of 시점에 존재하지 않는 미래 정보라는 원칙을
한 곳에 모은 모듈 — depletion·anomaly 양쪽이 이 술어를 공유한다.

### `medsupply/analytics/params.py` — 파라미터 로더 + 불변 dataclass 6종

```python
def load_params(path: str | Path = Path("config/analytics_params.toml")) -> AnalyticsParams
```
TOML 로드 + 검증(범위·미지 키) + `params_hash`(정규화 JSON sha256 앞 8자) 산출. 검증 실패 시
`ValueError`.

```python
@dataclass(frozen=True) class GradeParams:
    danger_days: int; warning_days: int; watch_days: int
    escalate_on_notice: bool; escalate_needs_review: bool

@dataclass(frozen=True) class ForecastParams:
    method: str; sma_window: int; ses_alpha: float; horizon_days: int

@dataclass(frozen=True) class AnomalyParams:
    surge_ratio: float; drop_ratio: float; recent_window: int
    baseline_window: int; receipt_delay_days: int

@dataclass(frozen=True) class DepletionParams:
    reflect_receipts: bool; overdue_cutoff: bool = False

@dataclass(frozen=True) class ScoreParams:
    base_danger: int; base_warning: int; base_watch: int; base_normal: int
    per_anomaly: int; notice_bonus: int

@dataclass(frozen=True) class AnalyticsParams:
    grade: GradeParams; forecast: ForecastParams; anomaly: AnomalyParams
    depletion: DepletionParams; score: ScoreParams; params_hash: str
```

### 나머지 순수 계산 모듈 (forecast · anomaly · depletion · risk · types)

`pipeline.assess_item`이 아래를 이 순서로 호출·결선한다. LLM 미관여, I/O 없음, 동일 입력 →
동일 출력.

```python
# medsupply/analytics/forecast.py
def sma_forecast(usage: pd.Series, window: int, horizon: int) -> ForecastResult
def ses_forecast(usage: pd.Series, alpha: float, horizon: int) -> ForecastResult

# medsupply/analytics/anomaly.py
def detect_usage_anomalies(usage: pd.Series, as_of: date, params: AnomalyParams) -> list[AnomalyFlag]
def detect_receipt_delay(receipts: pd.DataFrame, as_of: date, params: AnomalyParams) -> list[AnomalyFlag]

# medsupply/analytics/depletion.py
def estimate_depletion(stock_on_hand: float, daily_forecast: Sequence[float],
                        receipts: pd.DataFrame, as_of: date,
                        params: DepletionParams) -> DepletionEstimate

# medsupply/analytics/risk.py
def grade_risk(days_to_stockout: int | None, has_active_notice: bool,
                params: GradeParams) -> GradeDecision
def derive_risk_type(anomalies: Sequence[AnomalyFlag], has_active_notice: bool) -> str
def compute_score(decision: GradeDecision, anomalies: Sequence[AnomalyFlag],
                   has_active_notice: bool, params: ScoreParams) -> int
```

`medsupply/analytics/types.py`의 불변 dataclass: `RiskGrade`(Enum: 위험/경고/주의/정상) ·
`ForecastResult` · `AnomalyFlag` · `DepletionEstimate` · `GradeDecision` · `ItemInputs` ·
`RiskAssessment`(+ `.to_evidence() -> dict`, factors_json 직렬화용).

---

## llm

### `medsupply/llm/extraction.py` — 공고 구조화 추출(M-13)

```python
def extract_notice(raw_text: str, *, notice_id: str | None = None,
                    prompt_version: str | None = None,
                    force_refresh: bool = False) -> ExtractionResult
```
LLM(complete_json) 호출 + LLM 밖 결정적 `_verify`(발췌-원문 대조·4조건 게이트)로
`confidence`/`status`(자동확정|확인 필요만, 확인 완료는 사람 액션 전용) 산정.
`@observed("notice_extract")`.

```python
@dataclass(frozen=True) class ExtractionResult:
    extraction: NoticeExtraction; confidence: float; status: str
    verification: dict; provider: str; model: str; prompt_version: str; cache_hit: bool
```
`CONFIDENCE_THRESHOLD = 0.8`.

### `medsupply/llm/mapping.py` — 결정적 매핑 + 추출→매핑→영속화 파이프라인(M-14)

```python
def map_extraction_to_items(conn, extraction: NoticeExtraction, *,
                             extraction_status: str = "자동확정") -> MappingResult
```
LLM 미관여. ingredient_names 정확→부분 매칭 우선, 실패 시에만 product_names 정확 일치 보조.

```python
def process_notice(conn, notice_id: str, *, force_refresh: bool = False) -> NoticeProcessingResult
```
공고 1건: raw_text 로드 → `extract_notice`(LLM) → `map_extraction_to_items`(결정적) →
`writer.save_notice_extraction`. 존재하지 않는 notice_id면 `ValueError`. 멱등.

```python
@dataclass(frozen=True) class MappingResult:
    matched_ingredient_codes: tuple[str, ...]; mapped: tuple[dict, ...]
    unmatched_ingredients: tuple[str, ...]; unmatched_products: tuple[str, ...]

@dataclass(frozen=True) class NoticeProcessingResult:
    notice_id: str; status: str; confidence: float
    mapped_count: int; matched_ingredients: int; cache_hit: bool
```

### `medsupply/llm/grounding.py` — 위험 근거 패키징 + 환각 사후 대조(M-20), LLM 미관여

```python
def collect_risk_evidence(conn, item_id: str, run_id: str | None = None) -> RiskEvidence
```
risk_results **재산출 없이** 최신/지정 run 1건을 조회해 closed-world 근거(RiskEvidence)로
결선. 해당 run에 item_id 행이 없으면 `ValueError`.

```python
def verify_explanation_grounding(evidence: RiskEvidence, explanation: RiskExplanation) -> list[str]
```
생성물이 근거 밖 사실을 말했는지 5종 고정 순서로 대조(§docs/llm-pipeline.md). 위반이어도
예외 없음 — `hallucination_flags` 리스트만 반환.

### `medsupply/llm/explanation.py` — 원인 설명·대응방안 생성 + 영속화(M-21)

```python
def generate_risk_explanation(evidence: RiskEvidence, *, history: Sequence[dict] = (),
                               prompt_version: str | None = None,
                               force_refresh: bool = False) -> ExplanationResult
```
LLM(complete_json) 호출 + `verify_explanation_grounding`로 flags 부착. `@observed("risk_explain")`.

```python
def explain_item(conn, item_id: str, *, force_refresh: bool = False) -> ExplanationResult
```
`collect_risk_evidence` → `generate_risk_explanation` → `writer.save_explanation` 원콜(앱 소비
진입점). 근거 없음(run 없음)이면 `ValueError`(LLM 미호출).

```python
@dataclass(frozen=True) class ExplanationResult:
    explanation: RiskExplanation; hallucination_flags: tuple[str, ...]
    provider: str; model: str; prompt_version: str; cache_hit: bool
```

### `medsupply/llm/warm.py` — LLM 캐시 선워밍(M-27)

```python
def warm_cache(conn, *, scope: str = "all", force_refresh: bool = False,
                progress: Callable[[str], None] | None = None) -> WarmReport
```
`scope`: `all`(공고 먼저→설명, 기본) | `notices` | `explanations`. 설명 대상은 최신 run에서
grade∈{위험,경고,주의}인 품목만. 건별 실패는 격리(전체 중단 없음).

```python
@dataclass(frozen=True) class WarmReport:
    notices_total: int; notices_ok: int; notices_failed: tuple[str, ...]
    explanations_total: int; explanations_ok: int; explanations_failed: tuple[str, ...]
    cache_hits: int
```

### `medsupply/llm/cache.py` — 결과 캐시(SQLite, `data/llm_cache.db`)

```python
def build_cache_key(task: str, prompt_version: str, model: str, schema: type, payload: dict) -> str
def init_cache(path: str | Path = settings.LLM_CACHE_PATH) -> None
def cache_get(key: str, schema: type[T], *, path=settings.LLM_CACHE_PATH) -> LLMResult[T] | None
def cache_put(key: str, task: str, prompt_version: str, result: LLMResult, *,
              path=settings.LLM_CACHE_PATH) -> None
def cache_stats(path=settings.LLM_CACHE_PATH) -> dict   # {"entries": int, "by_task": dict}
```
키 = `sha256(task|prompt_version|model|schema명|canonical(payload))`, 휘발 필드
(run_id/generated_at/trace_id) 제거 후 정렬 직렬화.

### `medsupply/llm/client.py` + `config.py` — Anthropic 우선·OpenAI 폴백 JSON 구조화 호출

```python
def complete_json(task: str, prompt: RenderedPrompt, schema: type[T], *,
                   provider: Literal["anthropic", "openai"] | None = None,
                   temperature: float | None = None, max_tokens: int = 8192,
                   cache_key: str | None = None, force_refresh: bool = False) -> LLMResult[T]
```
`cache_key` 있으면 캐시 우선 조회(오프라인도 히트 우선). `LLM_MODE=offline`이고 미스면
`LLMOfflineError`. 두 공급자 모두 실패/폴백 불가면 `LLMUnavailableError`.

```python
@dataclass(frozen=True) class RenderedPrompt: system: str; user: str; version: str
@dataclass(frozen=True) class LLMResult(Generic[T]):
    data: T; provider: str; model: str; cache_hit: bool
    latency_ms: int; trace_id: str | None; usage: dict

def load_llm_config() -> LLMConfig   # 환경변수 LLM_PROVIDER/LLM_MODE/*_MODEL/*_API_KEY 스냅샷
@dataclass(frozen=True) class LLMConfig:
    provider: str; mode: str; anthropic_model: str; openai_model: str
    anthropic_key_set: bool; openai_key_set: bool
```

### `medsupply/llm/tracing.py` — Langfuse 관측 훅(결정 35)

```python
def init_tracing() -> bool                 # 3개 env 완전 설정 + SDK import 성공해야 True
def record_metadata(result: Any) -> dict   # 계약 5종 duck-typing 추출
def observed(task: str) -> Callable[[F], F]  # extract_notice·generate_risk_explanation 전용
```

### `medsupply/llm/schemas.py` — pydantic I/O 스키마

```python
class NoticeExtraction(BaseModel):
    product_names: list[str]; ingredient_names: list[str]; reason: str
    halt_start_date: str | None; expected_restart_date: str | None
    notice_type: str; evidence_quotes: list[str]

class RiskEvidence(BaseModel):
    item_id: str; item_name: str; ingredient_name_kr: str | None; as_of: str; run_id: str
    grade: str; score: int; risk_type: str; days_to_stockout: int | None
    depletion_date: str | None; current_stock: float | None; avg_daily_usage: float | None
    usage_change_pct: float | None; anomalies: list[dict]; escalated_by_notice: bool
    active_notices: list[dict]; next_shipment: dict | None
    substitutes_same_condition: list[dict]; evidence_refs: list[str]

class RiskAction(BaseModel):
    title: str; description: str; evidence_refs: list[str]

class RiskExplanation(BaseModel):   # 등급·점수 필드 없음(결정 38)
    cause_summary: str; actions: list[RiskAction]
    evidence_refs: list[str]; history_note: str | None = None
```

---

## services

Streamlit `st.cache_data`/`st.cache_resource` 계층 — 새 SQL을 직접 작성하지 않고
`medsupply.data.queries`/`writer`를 조합만 한다. `data_version` 인자는 전부 캐시 무효화
신호일 뿐 조회 조건이 아니다.

| 모듈 | 역할 | 주요 함수 |
| --- | --- | --- |
| `services/inventory.py` | 수급 상황실 통합조회 | `get_conn() -> Connection`(cache_resource) · `current_data_version(conn=None) -> int` · `load_overview(search="", ingredient=None, form=None, supplier=None, grade=None, status=None, data_version=0) -> DataFrame`(supply_status 4분기 파생) |
| `services/workbench.py` | 약사 검토 워크벤치 상세 | `open_write_conn() -> Connection`(단발성 쓰기) · `load_item_detail(item_id, data_version=0) -> dict`(risk/prev_risk/series/forecast/substitutes/explanation 일괄) |
| `services/notices.py` | 공급 공고 조회·확인 | `load_notice_list(status=None, data_version=0) -> DataFrame` · `load_notice_detail(notice_id, data_version=0) -> dict|None` · `confirm_notice(notice_id) -> None`(→ '확인 완료') |
| `services/orders.py` | 발주 요청안 결정적 산출 | `compute_order_proposal(item_id, data_version=0) -> dict`(expected_demand/shortage/suggested_qty 등, LLM 미관여) |
| `services/history.py` | 대응 이력 조회 | `load_history(risk_type=None, search="", data_version=0) -> DataFrame` |
| `services/alerts.py` | 알림 결정적 파생·조회 | `sync_alerts(conn) -> dict`(created/skipped, 등급 상승·입고 지연·신규 공고 매핑 3규칙) · `load_alerts(unread_only=False, data_version=0) -> DataFrame` |
| `services/evaluation.py` | AI 평가 리포트 파일 로딩(S-31, DB 아님) | `current_report_mtimes() -> tuple` · `load_eval_reports(mtimes=()) -> dict`(리포트 8종 + eval config + 최신 실험 요약, 부재·손상 격리) |

---

## eval

### `eval/schemas.py`

```python
class JudgeOutput(BaseModel):   # judge LLM이 직접 출력
    groundedness: float; cause_relevance: float; actionability: float  # 0~1
    hallucination: bool; rationale: str

class JudgeScore(JudgeOutput):  # + 실행기가 사후에 채우는 메타 2필드
    judge_model: str; rubric_version: str
```

### `eval/judge.py`

```python
def judge_generation(case: dict, generation: dict, *,
                      config_path: str | Path = "eval/config.yaml") -> JudgeScore
```
케이스 1건을 **교차 provider**로 채점(생성이 anthropic이면 judge는 openai, 그 역도 동일 —
같은 공급자가 자기 생성물을 채점하지 않음). `@observed("judge")`.

```python
def check_completeness(risk_row: dict, explanation: dict) -> list[str]
```
LLM 미관여 결정적 4종 검사(등급 존재·원인 비어있지 않음·대응 ≥1·근거 ≥1). 빈 리스트=완결.

```python
def run_experiment(name: str, *, prompt_version: str,
                    dataset_path: str | Path = "eval/cases/eval_cases_v1.json",
                    limit: int | None = None, pilot_only: bool = False,
                    force_refresh: bool = False) -> dict
```
케이스셋 순회 → explain → judge → completeness → `eval/results/{name}.jsonl` 기록 + 요약
dict 반환. 케이스 1건 실패는 격리(전체 중단 없음, 요약 분모에서만 제외).

### `eval/build_cases.py` — 평가셋 40건(파일럿 4건) 결정적 구성(S-26)

```python
def select_case_rows(risk_df: pd.DataFrame) -> pd.DataFrame
```
위험·경고 전건 + 주의 등급 item_id 오름차순 보충(최대 40건, 미달 시 있는 만큼).

```python
def select_pilot_ids(selected_rows: pd.DataFrame) -> list[str]
```
risk_type별 최소 item_id 대표 최대 4건(부족 시 목록 선두로 보충).

```python
def build_dataset(conn) -> dict   # {"meta": {...}, "cases": [...]}
def write_cases_file(path, dataset: dict) -> None
def sha256_file(path) -> str
def update_config_yaml(config_path, *, case_count: int, pilot_count: int, content_hash: str) -> None
```

CLI: `python -m eval.build_cases --db <db경로> --out <케이스 json 경로> [--config eval/config.yaml]`

---

## scripts (CLI)

`sys.path.insert(0, 리포 루트)`로 어디서 실행해도 `medsupply`/`scripts.datagen`을 import한다
(단, 상대 경로 인자 기본값은 CWD 기준이므로 저장소 루트에서 실행을 전제한다). 격리 원칙:
`scripts/datagen/`은 `medsupply`를 import하지 않고(역방향 차단), `medsupply`+`app.py`는
`scripts.datagen`·`scripts.measure_detection`을 import하지 않는다(순방향 차단,
`tests/test_isolation.py`가 정적으로 강제). **신규 scripts/ 최상위 파일**을 추가하면
`tests/test_isolation.py`의 `SCRIPTS_PATH_TARGETS` 딕셔너리에 수동 등록해야 저 정적 검사
대상에 포함된다(등록하지 않으면 검사에서 그냥 빠질 뿐 실패하지 않는다 — 조용히 누락되기
쉬우므로 신규 스크립트 작성 시 반드시 확인).

| 스크립트 | 사용법 |
| --- | --- |
| `generate_dataset.py` | `python scripts/generate_dataset.py --config data/scenarios/scenario_config.yaml --out <db> --seed <int> --base-date YYYY-MM-DD [--labels-out <json>] [--skip-history-seed]`(주입 경로, 표준 스냅샷) 또는 `--baseline-only`(정상 패턴만) |
| `load_notices.py` | `python scripts/load_notices.py --db <db> --raw data/notices/raw --index data/notices/notices_index.csv` |
| `validate_dataset.py` | `python scripts/validate_dataset.py --db <db> [--expect-hash <sha256>|@<파일경로>]` — 11개 검사 PASS/WARN/FAIL |
| `run_risk_batch.py` | `python scripts/run_risk_batch.py --db <db> --as-of YYYY-MM-DD [--as-of YYYY-MM-DD ...] [--params config/analytics_params.toml]` |
| `process_notices.py` | `python scripts/process_notices.py --db <db> (--notice-id <ID> \| --all) [--force-refresh]` |
| `warm_cache.py` | `python scripts/warm_cache.py --db <db> [--scope all\|notices\|explanations] [--force-refresh]` |
| `measure_detection.py` | `python scripts/measure_detection.py --db <db> --labels <라벨 json> --start YYYY-MM-DD --end YYYY-MM-DD --out <결과 json> [--params ...]`(일괄) 또는 `--predict-only <경로>` / `--score <예측경로>`(블라인드 2단계) |
| `measure_extraction.py` | `python scripts/measure_extraction.py --db <db> --gold data/notices/gold/gold_labels_v1.json --out <결과 json>` |
| `measure_mape.py` | `python scripts/measure_mape.py --db <db> --as-of YYYY-MM-DD [--as-of ...] --out <결과 json> [--params ...]` |
| `measure_perf.py` | `python scripts/measure_perf.py --db <db> [--repeats 30] --out <결과 json>` |
| `run_e2e.py` | `python scripts/run_e2e.py --db <db> [--runs 10] --out <결과 json>` — 원본 --db는 회차별 tmp 사본으로만 열림(쓰기 없음) |
| `verify_reproducibility.py` | `python scripts/verify_reproducibility.py [--runs 5] --out <결과 json> --labels <라벨 json> --detection-start YYYY-MM-DD --detection-end YYYY-MM-DD [--params ...]` — 생성·배치는 자체 tmp, 측정 재현은 `data/medsupply.db`를 읽기 전용으로 사용 |
| `generate_blind.py` | `python scripts/generate_blind.py --ranges data/scenarios/blind_ranges.yaml --seed <int> --base-date YYYY-MM-DD --out <db> [--sealed-dir data/blind/sealed] [--manifest data/blind/manifest.json]` |
