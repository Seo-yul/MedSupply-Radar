# MedSupply Radar 검증 리포트

무엇을 어떻게 측정했고, 수치가 얼마이며, 무엇이 아직 검증되지 않았는지를 한 문서에 모은다.

**이 문서의 모든 수치는 기계 대조된다.** 각 수치가 있는 줄 끝에는
`<!-- check: {파일}:{경로} = {표기값} -->` 주석이 붙어 있고, `scripts/check_report_numbers.py`가
표기값과 `reports/`의 측정 JSON 실값을 대조한다. 결과는 `reports/platform/report_check.json`에
커밋돼 있다.

```bash
.venv/bin/python scripts/check_report_numbers.py
```

값이 하나라도 어긋나거나, 마킹되지 않은 수치가 남아 있거나, 핵심 수치의 마킹이 빠지면 이
명령은 `exit 1`로 실패한다. 즉 "문서가 JSON과 다르다"는 상태로는 저장소가 그린일 수 없다.

JSON으로 대조할 수 없는 수치(동결 이전의 캘리브레이션 이력 등)는 `<!-- check-skip: 사유 -->`로만
면제되며, **면제된 토큰과 사유는 결과 JSON에 그대로 기록된다** — 면제 목록 자체가 감사 대상이다.

**아직 측정하지 않은 것은 "실측 전"으로 적는다**(§5). LLM API 키가 없는 환경이라 추출 정확도·
judge 채점·인간 교차검토는 장치만 준비돼 있고 수치가 없다. 그 자리에 추정치나 목표치를 적지
않았다.

**모든 감지·오탐 수치의 측정 조건**: 공고(notices)는 DB에 적재만 돼 있고 `process_notices`로
추출·매핑되지 않은 상태에서 측정했다. 즉 활성 공고에 의한 등급 상향(`escalate_on_notice`)이
없는 조건이다. 키 확보 후 추출을 실행하면 이 수치들은 재측정 대상이다(§6).

---

## 1. 검증 설계 원칙

### 1.1 판정과 정답을 코드 수준에서 분리한다

위험 판정 로직이 시나리오 설정이나 정답 라벨을 볼 수 있으면 어떤 측정도 의미가 없다. 그래서
분리를 문서 규칙이 아니라 **기계 강제**로 둔다 — `tests/test_isolation.py`가 저장소 전체를
ast로 파싱해 양방향으로 검사한다.

- 순방향(로직 → 정답 차단): `medsupply/` 전체와 `app.py`, 그리고 등록된 `scripts/` CLI들은
  `scripts.datagen`·`scripts.measure_detection`을 임포트할 수 없고, `data/scenarios`·
  `ground_truth`·`data/notices/gold` 경로를 코드 값으로 쓸 수 없다.
- 역방향(생성기 → 로직 차단): `scripts/datagen/` 전체가 `medsupply`를 임포트할 수 없다.
- DB 뒷문 차단: `schema.sql`의 어떤 컬럼명도 `scenario`를 포함하지 않는다.

문자열 grep이 아니라 ast 검사라 docstring의 서술은 허용하고 코드 값만 잡는다. 검사 함수는
tmp에 만든 위반 샘플로 자가 검증한다 — "검사기가 실제로 위반을 잡는가"를 확인하는 테스트가
함께 있다.

### 1.2 측정 기반을 동결한다

| 앵커 | 값 | 근거 |
| --- | --- | --- |
| 원천 데이터 콘텐츠 해시 | `c34bf4cb9215e1ba4b7a21c9d0f1d7236e2164ae08b630bf54fdf82d4c2f6483` | `data/scenarios/standard_snapshot.sha256` |
| 분석 파라미터 해시 | `6ec9bf05` | `config/analytics_params.toml` |
| git 태그 | `dataset-freeze-v1` (커밋 `f7fd573`) | 로컬 태그 |

`content_hash`는 DB 파일 해시가 아니라 **부트스트랩 원천 테이블**(items·ingredients·
ingredient_aliases·substitute_groups·stock_usage_daily·incoming_shipments·notices)만 해싱한
값이다. 배치 실행이나 앱 사용으로 파생 테이블이 바뀌어도 흔들리지 않아야 앵커 구실을 하기
때문이다. 이 정의는 리뷰에서 "파일 해시는 배치만 돌려도 바뀌어 앵커가 될 수 없다"는 지적이
나와 재정의됐고, 구 해시와의 대응은 측정 JSON에 함께 기록돼 있다.

### 1.3 캘리브레이션은 사전 등록하고, 기준을 뒤집었으면 그 사실을 적는다

파라미터 캘리브레이션은 스윕을 돌리기 **전에** 후보 TOML 넷과 채택 기준 넷을 사전 등록한 뒤
진행했다. 결과는 다음과 같다.

- 사전 등록한 채택 기준 ①은 "'주의 이상' 감지율 100% 유지"였다. <!-- check-skip: 사전 등록된 채택 기준값(동결 전 캘리브레이션 기록 — 측정 JSON에 없다) -->
- 후보 넷 전부 그 기준에 미달했고, 규칙대로 **BLOCKED(채택 없음)**으로 보고됐다.
- 이후 조사에서 기준 ① 자체가 잘못된 전제 위에 있었음이 실측으로 드러났다. 기준선의 감지 100%는 좋은 탐지력이 아니라 **모든 입고를 무시하는 추정기**의 부산물이었고, 같은 결함이 오탐률 100%를 만들고 있었다. <!-- check-skip: 동결 전 기준선 구성의 값(§2.3 여정표와 동일 출처 — 캘리브레이션 기록) -->
  감지와 오탐은 독립 조정이 가능하지 않았다.
- 그래서 컨트롤러가 기준 ①을 **명시적으로 오버라이드**하고 `cand-F`를 채택·동결했다. 사전
  등록 기준은 유효 후보 사이의 선별 규칙이며, 기준 자체가 퇴행적 구성을 고정한다는 것이
  실증되면 재룰링한다는 판단이다.

이 문서의 헤드라인 감지율이 90%인 것은 그 오버라이드의 직접적 결과다. <!-- check: reports/analytics/detection_metrics.json:results.detection_rate = 90% -->
기준을 사후에 조용히 바꾼 것이 아니라, 바꿨다는 사실과 이유를 여기 남긴다.

### 1.4 블라인드는 예측을 먼저 봉인한 뒤 정답을 연다

블라인드 스냅샷은 `data/scenarios/`를 전혀 참조하지 않고 생성되며, 라벨은 `data/blind/sealed/`에
봉인된다. 채점 절차는 회차마다 동일하다.

1. 라벨을 열지 않은 상태로 `run_risk_batch` → `measure_detection --predict-only`를 실행해
   예측 파일(일자 x 품목 등급 격자)을 만든다. `--labels`는 전달하지 않는다.
2. 예측 파일들의 sha256을 `data/blind/manifest.json`에 기록하고 **매니페스트 한 개 파일만**
   스테이징해 커밋한다. 이 커밋이 "예측 확정" 시점의 감사 증거다.
3. 커밋 이후, 디스크상 예측 파일 해시가 커밋된 매니페스트와 일치함을 재확인한 뒤에만 봉인
   라벨을 열어 `--score`를 실행한다.

두 회차 모두 예측 종료 → 봉인 커밋 → 라벨 개봉 순서가 타임스탬프로 남아 있고, 채점 결과를
이유로 파라미터·문턱·스윕 구간·시드를 바꾸지 않았다.

---

## 2. 감지 성능

### 2.1 대표 수치 선택

'주의 이상' 문턱의 감지율·오탐률 **쌍**을 주지표로 삼고, '경고 이상'·'위험 이상' 쌍을 함께
싣는다. **문턱을 감지와 오탐에 비대칭으로 적용하지 않는다** — "감지는 주의 이상, 오탐은 경고
이상"처럼 유리한 조합을 고르는 것은 지표 게이밍이다. 채점 코드는 문턱 하나를 감지·오탐 양쪽에
동시에 적용하는 단일 함수라, 비대칭 조합이 구조적으로 불가능하다.

`raw`는 라벨 전건이고, `지평 내`는 "이 스윕이 맞힐 기회가 있었던 라벨"만 추린 부분집합이다.
지평 기준은 채점 시점에만 계산하는 뷰이며 **라벨 파일은 수정하지 않는다**(라벨 세탁 방지).

### 2.2 표준 스냅샷 — 문턱 x 표본 전 조합

스윕 2026-07-01 ~ 2026-08-01(32일), 라벨 20건 / 정상 104건. 출처 `reports/analytics/detection_metrics.json`. <!-- check: reports/analytics/detection_metrics.json:results.sweep.days = 32 --><!-- check: reports/analytics/detection_metrics.json:results.counts.labeled = 20 --><!-- check: reports/analytics/detection_metrics.json:results.counts.normal = 104 -->

| 문턱 | 표본 | 감지율 | 오탐률 | 선행일수 중앙값 |
| --- | --- | --- | --- | --- |
| **주의 이상(주지표)** | raw(라벨 20) | **90.0%**(18/20) | **45.19%**(47/104) | **26.5일** | <!-- check: reports/analytics/detection_metrics.json:results.counts.labeled = 20 --><!-- check: reports/analytics/detection_metrics.json:results.detection_rate = 90.0% --><!-- check: reports/analytics/detection_metrics.json:results.counts.detected = 18 --><!-- check: reports/analytics/detection_metrics.json:results.counts.labeled = 20 --><!-- check: reports/analytics/detection_metrics.json:results.false_positive_rate = 45.19% --><!-- check: reports/analytics/detection_metrics.json:results.counts.false_positives = 47 --><!-- check: reports/analytics/detection_metrics.json:results.counts.normal = 104 --><!-- check: reports/analytics/detection_metrics.json:results.lead_days.median = 26.5 -->
| 주의 이상 | 지평 내(라벨 15) | 93.3%(14/15) | 45.19%(47/104) | 15.0일 | <!-- check: reports/analytics/detection_metrics.json:results.within_horizon.counts.labeled_in_horizon = 15 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_watch.detection_rate = 93.3% --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_watch.counts.detected = 14 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_watch.counts.labeled = 15 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_watch.false_positive_rate = 45.19% --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_watch.counts.false_positives = 47 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_watch.counts.normal = 104 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_watch.lead_days.median = 15.0 -->
| 경고 이상 | raw(라벨 20) | 65.0%(13/20) | 3.85%(4/104) | 10일 | <!-- check: reports/analytics/detection_metrics.json:results.threshold_warning.counts.labeled = 20 --><!-- check: reports/analytics/detection_metrics.json:results.threshold_warning.detection_rate = 65.0% --><!-- check: reports/analytics/detection_metrics.json:results.threshold_warning.counts.detected = 13 --><!-- check: reports/analytics/detection_metrics.json:results.threshold_warning.counts.labeled = 20 --><!-- check: reports/analytics/detection_metrics.json:results.threshold_warning.false_positive_rate = 3.85% --><!-- check: reports/analytics/detection_metrics.json:results.threshold_warning.counts.false_positives = 4 --><!-- check: reports/analytics/detection_metrics.json:results.threshold_warning.counts.normal = 104 --><!-- check: reports/analytics/detection_metrics.json:results.threshold_warning.lead_days.median = 10 -->
| 경고 이상 | 지평 내(라벨 15) | 66.7%(10/15) | 3.85%(4/104) | 9.0일 | <!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_warning.counts.labeled = 15 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_warning.detection_rate = 66.7% --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_warning.counts.detected = 10 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_warning.counts.labeled = 15 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_warning.false_positive_rate = 3.85% --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_warning.counts.false_positives = 4 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_warning.counts.normal = 104 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_warning.lead_days.median = 9.0 -->
| 위험 이상 | raw(라벨 20) | 45.0%(9/20) | 0.00%(0/104) | 7일 | <!-- check: reports/analytics/detection_metrics.json:results.threshold_danger.counts.labeled = 20 --><!-- check: reports/analytics/detection_metrics.json:results.threshold_danger.detection_rate = 45.0% --><!-- check: reports/analytics/detection_metrics.json:results.threshold_danger.counts.detected = 9 --><!-- check: reports/analytics/detection_metrics.json:results.threshold_danger.counts.labeled = 20 --><!-- check: reports/analytics/detection_metrics.json:results.threshold_danger.false_positive_rate = 0.00% --><!-- check: reports/analytics/detection_metrics.json:results.threshold_danger.counts.false_positives = 0 --><!-- check: reports/analytics/detection_metrics.json:results.threshold_danger.counts.normal = 104 --><!-- check: reports/analytics/detection_metrics.json:results.threshold_danger.lead_days.median = 7 -->
| 위험 이상 | 지평 내(라벨 15) | 53.3%(8/15) | 0.00%(0/104) | 7.0일 | <!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_danger.counts.labeled = 15 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_danger.detection_rate = 53.3% --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_danger.counts.detected = 8 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_danger.counts.labeled = 15 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_danger.false_positive_rate = 0.00% --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_danger.counts.false_positives = 0 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_danger.counts.normal = 104 --><!-- check: reports/analytics/detection_metrics.json:results.within_horizon.threshold_danger.lead_days.median = 7.0 -->

부가 지표: 최고등급('위험') 정밀도 100.0%, risk_type 일치율 25.0%(5/20). <!-- check: reports/analytics/detection_metrics.json:results.danger_precision = 100.0% --><!-- check: reports/analytics/detection_metrics.json:results.risk_type_match.overall = 25.0% --><!-- check: reports/analytics/detection_metrics.json:results.risk_type_match.counts.matched = 5 --><!-- check: reports/analytics/detection_metrics.json:results.risk_type_match.counts.labeled = 20 -->
선행일수 분포(주의 이상, raw)는 최소 3일 · 평균 27.56일 · 최대 75일이다. <!-- check: reports/analytics/detection_metrics.json:results.lead_days.min = 3 --><!-- check: reports/analytics/detection_metrics.json:results.lead_days.mean = 27.56 --><!-- check: reports/analytics/detection_metrics.json:results.lead_days.max = 75 -->

**지평 내 수치가 raw보다 높은 이유**는 지평 기준이 "품절일이 스윕 종료 + `watch_days` 안"인
라벨만 남기기 때문이다. 표준에서 제외된 라벨은 5건이며 전부 스윕이 끝난 뒤 한참 지나서 품절한다. <!-- check: reports/analytics/detection_metrics.json:results.within_horizon.counts.excluded = 5 -->
이 비대칭은 블라인드 1차에서 정반대로 나타났다(§2.4).

### 2.3 S-17 여정 — 전건 오탐에서 현재 수치까지

헤드라인만 보면 보이지 않는 경로가 있다. 초기 구성에서는 **정상 품목 전건**이 스윕 중 한 번
이상 '주의' 이상으로 떴다.

| 단계 | 무엇을 고쳤나 | 감지(주의+) | 오탐(주의+) |
| --- | --- | --- | --- |
| 기준선 | — | 20/20 | 104/104 | <!-- check-skip: 동결 전 캘리브레이션 기록의 중간 구성 — 현행 측정 JSON에 없는 값이다 -->
| as_of 재구성 | 입고 반영 술어를 as_of 시점 pending으로 재정의 | 20/20 | 91/104 | <!-- check-skip: 동결 전 캘리브레이션 기록의 중간 구성 — 현행 측정 JSON에 없는 값이다 -->
| **`cand-F`(채택·동결)** | + 연체 입고의 미래 예정분 반영 차단 | **18/20** | **47/104** | <!-- check: reports/analytics/detection_metrics.json:results.counts.detected = 18 --><!-- check: reports/analytics/detection_metrics.json:results.counts.labeled = 20 --><!-- check: reports/analytics/detection_metrics.json:results.counts.false_positives = 47 --><!-- check: reports/analytics/detection_metrics.json:results.counts.normal = 104 -->

원인은 파라미터가 아니라 **버그였다**. 소진 추정이 "아직 도착 스탬프가 없는 입고"만 반영
대상으로 삼는 바람에, 과거 시점으로 되감아 도는 백테스트에서는 임박한 정상 입고 대부분이
반영에서 빠졌다. 모든 품목이 "곧 재고가 바닥난다"로 계산되니 전건이 오탐이 된 것이다. as_of
시점 기준으로 술어를 재구성(`expected_date > as_of AND (actual_date IS NULL OR actual_date >
as_of)`)하자 오탐이 크게 떨어졌고, 연체 입고에 대한 보수 전환(`overdue_cutoff`)까지 넣은
`cand-F`에서 현재 수치가 됐다.

이 여정은 §1.3의 오버라이드와 같은 사건의 양면이다. **기준선의 높은 감지율은 전건 오탐과 같은
결함의 이면**이었고, 버그를 고치자 둘이 함께 움직였다.

### 2.4 블라인드 평가 1차·2차

블라인드는 `data/scenarios/`를 참조하지 않는 role-blind 스냅샷(회차마다 시드 다섯 개)에 대해,
표준과 **같은 파라미터·같은 스윕 구간**으로 예측을 봉인한 뒤 채점한 결과다.

#### (i) 두 회차 사이에 바뀐 것 — 전부 평가 설계다

- 시나리오 배치 구간을 측정 창과 **결합**했다. 이전에는 시나리오 개시 오프셋 범위와 측정
  구간 사이에 아무 제약이 없었다. 지금은 라벨 전건이 관측 가능 창(스윕 시작 ~ 스윕 종료 +
  `watch_days`) 안에 들어올 때까지 스냅샷을 재추첨한다.
- 미끼(정상 품목에 넣는 양성 교란) 적격성 기준을 "기준일 한 점의 재고 커버리지"에서 "스윕
  구간 최저 커버리지"로 바꿨다. 새 기준에서는 적격 품목이 없어 2차에는 미끼가 하나도 주입되지
  않았다.
- 채점기에 진단 두 가지를 신설했다: 채점 불가 라벨 카운터(`unscoreable_labels`)와 지평 기준의
  하한(`sweep_start <= stockout_date`).

#### (ii) 바뀌지 않은 것 — 전부 탐지기다

탐지기 코드, 판정 문턱, 채점 산식, 분석 파라미터(`config_hash = 6ec9bf05` — 두 회차 동일),
스윕 구간(2026-07-01 ~ 2026-08-01). 감지율·오탐률의 정의도 그대로다. **2차의 감지율은 탐지기가
좋아져서 오른 값이 아니다.**

#### (iii) 그 주장의 직접 증거 — 1차 봉인 예측 재채점

말이 아니라 실험으로 보인다. **1차의 봉인 예측 파일을 한 글자도 고치지 않고** 현행 채점기로
다시 채점했다. 산출물은 `reports/analytics/blind_round1_rescored.json`이며, 1차 원본
(`blind_summary.json`·`blind_2026090*_metrics.json`)은 읽기만 했다.

- **재채점 전 게이트**: 예측 파일과 봉인 라벨의 sha256이 커밋된 매니페스트의 `predictions`·
  `runs` 절과 전건 일치함을 확인했다(`meta.hash_verification`). <!-- check: reports/analytics/blind_round1_rescored.json:meta.hash_verification.status = "PASS" -->
- **결과**: 현행 지평 기준으로 1차 라벨 20건 중 측정 창 안에 있던 것은 5건이었고, <!-- check: reports/analytics/blind_round1_rescored.json:aggregate.labels_total = 20 --><!-- check: reports/analytics/blind_round1_rescored.json:aggregate.within_horizon_current_criterion.labeled_in_horizon = 5 -->
  1차 예측은 그 5건 가운데 5건을 감지했다(100.0%). <!-- check: reports/analytics/blind_round1_rescored.json:aggregate.within_horizon_current_criterion.labeled_in_horizon = 5 --><!-- check: reports/analytics/blind_round1_rescored.json:aggregate.within_horizon_current_criterion.detected = 5 --><!-- check: reports/analytics/blind_round1_rescored.json:aggregate.within_horizon_current_criterion.detection_rate = 100.0% -->
- **그리고 재채점된 raw 지표는 1차 공표값과 완전히 같다.** 1차 원본 결과와 재귀 전수 비교한
  결과, 지평 뷰와 신규 진단 밖에서 달라진 경로는 0건이다. <!-- check: reports/analytics/blind_round1_rescored.json:raw_metrics_identity_check.differing_paths_outside_horizon_view_total = 0 -->

즉 **같은 예측·같은 산식** 위에서 "지평 안 라벨만 보면 전건 감지"와 "전체 라벨로 보면 35.0%"가 동시에 성립한다. <!-- check: reports/analytics/blind_round1_rescored.json:aggregate.raw_detection_rate_mean = 35.0% -->
차이는 전적으로 분모에 남은 채점 불가 라벨에서 온다. 2차의 감지율 100%는 이 결과와 같은 성질의 수치이지 탐지기 개선의 증거가 아니다. <!-- check: reports/analytics/blind_round2_summary.json:aggregate.detection_rate.mean = 100% -->

#### 회차 비교('주의 이상' 문턱, raw)

지평 기준은 1차 채점 시점(상한만)과 2차 시점(상·하한)이 서로 다르다. **서로 다른 기준의 지평
내 수치를 한 표에 나란히 놓지 않는다.** 아래 표는 전부 raw 기준이며, 기준이 통일된 지평 내
대조는 바로 위 (iii)의 재채점 결과로만 제시한다.

| 지표(주의 이상, raw) | 표준 스냅샷 | 1차 블라인드 | 2차 블라인드 |
| --- | --- | --- | --- |
| 감지율 | 90.0%(18/20) | 35.0% | **100.0%** | <!-- check: reports/analytics/detection_metrics.json:results.detection_rate = 90.0% --><!-- check: reports/analytics/detection_metrics.json:results.counts.detected = 18 --><!-- check: reports/analytics/detection_metrics.json:results.counts.labeled = 20 --><!-- check: reports/analytics/blind_summary.json:aggregate.detection_rate.mean = 35.0% --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.detection_rate.mean = 100.0% -->
| 오탐률 | 45.19%(47/104) | 55.50% | 44.67% | <!-- check: reports/analytics/detection_metrics.json:results.false_positive_rate = 45.19% --><!-- check: reports/analytics/detection_metrics.json:results.counts.false_positives = 47 --><!-- check: reports/analytics/detection_metrics.json:results.counts.normal = 104 --><!-- check: reports/analytics/blind_summary.json:aggregate.false_positive_rate.mean = 55.50% --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.false_positive_rate.mean = 44.67% -->
| 최고등급 정밀도 | 100.0% | 76.67% | 100.0% | <!-- check: reports/analytics/detection_metrics.json:results.danger_precision = 100.0% --><!-- check: reports/analytics/blind_summary.json:aggregate.danger_precision.mean = 76.67% --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.danger_precision.mean = 100.0% -->
| 선행일수 중앙값 | 26.5일 | 45.5일 | 14.4일 | <!-- check: reports/analytics/detection_metrics.json:results.lead_days.median = 26.5 --><!-- check: reports/analytics/blind_summary.json:aggregate.lead_days_median.mean = 45.5 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.lead_days_median.mean = 14.4 -->
| **risk_type 일치율** | 25.0% | 20.0% | **30.0%** | <!-- check: reports/analytics/detection_metrics.json:results.risk_type_match.overall = 25.0% --><!-- check: reports/analytics/blind_summary.json:aggregate.risk_type_match_overall.mean = 20.0% --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.risk_type_match_overall.mean = 30.0% -->
| 채점 불가 라벨 | — | 12/20 | 0/20 | <!-- check: reports/analytics/blind_round1_rescored.json:aggregate.unscoreable_labels_total = 12 --><!-- check: reports/analytics/blind_round1_rescored.json:aggregate.labels_total = 20 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.unscoreable_labels_total = 0 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.labels_total = 20 -->

표준 스냅샷의 채점 불가 라벨은 없다. 이 진단은 2차 회차에서 신설돼 `detection_metrics.json`에는
기록돼 있지 않고(동결 파일 무수정 원칙), 표준 전수 재측정에서 해당 라벨이 없음이 확인됐다.

**risk_type 일치율은 감지율과 함께 읽어야 한다.** 2차에서 네 유형 모두 감지 5/5였지만, <!-- check: reports/analytics/blind_round2_summary.json:aggregate.by_type_detected.supply_halt.detected = 5 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.by_type_detected.supply_halt.labeled = 5 -->
원인 유형을 맞힌 비율은 30.0%에 그친다. <!-- check: reports/analytics/blind_round2_summary.json:aggregate.risk_type_match_overall.mean = 30.0% -->
특히 `supply_halt` 0/5, `composite` 0/5로 **체계적 실패**다. <!-- check: reports/analytics/blind_round2_summary.json:aggregate.risk_type_match_by_type.supply_halt.matched = 0 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.risk_type_match_by_type.supply_halt.labeled = 5 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.risk_type_match_by_type.composite.matched = 0 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.risk_type_match_by_type.composite.labeled = 5 -->
등급은 올바로 올리면서 원인은 못 짚는다는 뜻이며, 감지율이 이 실패를 가리지 않도록 같은 표에
싣는다.

#### 1차 결과를 읽는 법 — 수치는 그대로 두고 조건을 공시한다

1차의 raw 감지율과 오탐률은 **바꾸지 않고 그대로 싣는다**. 사후에 유리한 부분집합을 새
헤드라인으로 삼는 것은 지표 게이밍이기 때문이다. 대신 그 수치가 놓인 조건을 사실 그대로 적는다.

- 1차 라벨 20건 중 12건은 품절일이 스윕 시작일보다 **앞선다**. <!-- check: reports/analytics/blind_round1_rescored.json:aggregate.labels_total = 20 --><!-- check: reports/analytics/blind_round1_rescored.json:aggregate.unscoreable_labels_total = 12 -->
  채점 성공 규칙이 `first_alert <= stockout_date`이므로 이 라벨들은 어떤 예측으로도 감지
  성공이 될 수 없다. 새 지표를 정의한 것이 아니라, 기존 채점 규칙의 적용 가능성에 대한 사실
  진술이다.
- 1차의 오탐률은 표준과 **직접 비교할 수 없었다**. 표준의 정상 모집단에는 주입된 입고 이상이
  하나도 없지만, 1차 블라인드의 정상 모집단에는 미끼가 주입돼 있었다. 대조군 난이도가 다르다.

#### 조건부 표집 — 2차 수치의 가장 큰 한계

2차 스냅샷은 "라벨 전건이 **동시에** 측정 창 안에 들어올 때까지" 전량 재추첨해 만들었다.
실제 재추첨 횟수는 시드별로 1·6·2·5·8회이며 평균 4.4회, 최대 8회다. <!-- check: reports/analytics/blind_round2_summary.json:aggregate.attempts_used.values[0] = 1 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.attempts_used.values[1] = 6 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.attempts_used.values[2] = 2 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.attempts_used.values[3] = 5 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.attempts_used.values[4] = 8 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.attempts_used.mean = 4.4 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.attempts_used.max = 8 -->

재추첨 조건이 참조하는 것은 **라벨의 품절일뿐**이며 탐지기의 예측이나 채점 결과가 아니다.
그럼에도 이것은 명백한 조건부 표집이다. **2차 감지율은 균등 무작위 시나리오의 기대 성능이
아니라 "측정 가능하게 배치된 시나리오에 한한 조건부 성능"이다.** 완화하지 않았다 — 배치를
측정 창에 결합하라는 요구 자체가 조건부 표집이기 때문이다.

#### 선행일수의 질 — 중앙값만 보면 안 된다

2차의 시드별 선행일수 중앙값을 평균하면 14.4일이지만, 라벨 20건의 개별 선행일수는 아래와 같다 <!-- check: reports/analytics/blind_round2_summary.json:aggregate.lead_days_median.mean = 14.4 --><!-- check: reports/analytics/blind_round2_summary.json:aggregate.labels_total = 20 -->
(시드마다 유형별로 라벨 하나씩).

| 시드 | demand_surge | supply_halt | delivery_delay | composite |
| --- | --- | --- | --- | --- |
| 20260911 | 31 | 8 | 44 | **0** | <!-- check: reports/analytics/blind_round2_summary.json:blind_results[0].by_type.demand_surge.lead_days.min = 31 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[0].by_type.supply_halt.lead_days.min = 8 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[0].by_type.delivery_delay.lead_days.min = 44 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[0].by_type.composite.lead_days.min = 0 -->
| 20260912 | 28 | 6 | 12 | **3** | <!-- check: reports/analytics/blind_round2_summary.json:blind_results[1].by_type.demand_surge.lead_days.min = 28 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[1].by_type.supply_halt.lead_days.min = 6 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[1].by_type.delivery_delay.lead_days.min = 12 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[1].by_type.composite.lead_days.min = 3 -->
| 20260913 | 25 | 7 | 59 | 5 | <!-- check: reports/analytics/blind_round2_summary.json:blind_results[2].by_type.demand_surge.lead_days.min = 25 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[2].by_type.supply_halt.lead_days.min = 7 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[2].by_type.delivery_delay.lead_days.min = 59 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[2].by_type.composite.lead_days.min = 5 -->
| 20260914 | 29 | 9 | 32 | 4 | <!-- check: reports/analytics/blind_round2_summary.json:blind_results[3].by_type.demand_surge.lead_days.min = 29 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[3].by_type.supply_halt.lead_days.min = 9 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[3].by_type.delivery_delay.lead_days.min = 32 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[3].by_type.composite.lead_days.min = 4 -->
| 20260915 | 26 | 8 | 9 | 5 | <!-- check: reports/analytics/blind_round2_summary.json:blind_results[4].by_type.demand_surge.lead_days.min = 26 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[4].by_type.supply_halt.lead_days.min = 8 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[4].by_type.delivery_delay.lead_days.min = 9 --><!-- check: reports/analytics/blind_round2_summary.json:blind_results[4].by_type.composite.lead_days.min = 5 -->

위 20개 값을 모으면 최소 0일, 중앙 9.0일이고 3일 이하가 2건이다. <!-- check: reports/analytics/blind_round2_summary.json:blind_results[0].by_type.composite.lead_days.min = 0 --><!-- check-skip: 위 표의 마킹된 20개 값에서 계산한 분포(중앙값·3일 이하 건수) — 합산값 자체는 JSON에 없다 -->
**선행 0일은 "품절 당일에야 처음 경보했다"는 뜻이다.** <!-- check: reports/analytics/blind_round2_summary.json:blind_results[0].by_type.composite.lead_days.min = 0 -->
감지 성공 판정은 받았지만 실무적으로 쓸모 있는 예고는 아니다.
감지율 100%를 "라벨 전건을 여유 있게 예고했다"로 읽으면 안 된다. <!-- check: reports/analytics/blind_round2_summary.json:aggregate.detection_rate.mean = 100% -->

#### 오탐률 — '개선'이 아니라 비교가 처음 성립한 것이다

2차 오탐률 44.67%는 표준 45.19%보다 낮지만, 이것을 **탐지기 개선으로 읽으면 틀린다**. <!-- check: reports/analytics/blind_round2_summary.json:aggregate.false_positive_rate.mean = 44.67% --><!-- check: reports/analytics/detection_metrics.json:results.false_positive_rate = 45.19% -->
두 회차 사이에 탐지기는 아무것도 바뀌지 않았다((ii) 참조). 달라진 것은 **비교 가능성**이다.

- 2차 정상 모집단에는 주입된 입고 이상이 없다(미끼 적격 품목이 없어 미끼가 주입되지 않았다).
  표준의 정상 모집단도 입고 이상이 없다. **같은 조건이 됐다.**
- 그래서 2차에서 처음으로 표준과 like-for-like 비교가 **성립**한다. 차이는 -0.52%p다. <!-- check: reports/analytics/blind_round2_summary.json:comparison.false_positive_rate.delta_round2_minus_standard = -0.52%p -->
  이는 "탐지기가 표준 스냅샷 밖의 데이터에서도 대체로 같은 오탐 수준을 낸다"는 뜻으로 읽는
  것이 정확하다. 개선폭이 아니다.
- 오탐률 자체는 여전히 높다. 정상 품목의 절반 가까이가 스윕 32일 중 한 번은 '주의' 이상으로 뜬다. <!-- check: reports/analytics/detection_metrics.json:results.sweep.days = 32 -->
  '경고 이상'으로 문턱을 올리면 표준 기준 3.85%까지 내려간다(§2.2). <!-- check: reports/analytics/detection_metrics.json:results.threshold_warning.false_positive_rate = 3.85% -->

#### 이 블라인드 결과를 재사용하지 말 것

- **역산 누출**: 2차에 미끼가 없어, `actual_date > expected_date OR (actual_date IS NULL AND
  expected_date < base_date)` 같은 SQL 한 줄로 시나리오 품목의 상당수를 정답 없이 골라낼 수
  있다. 이번 회차의 예측은 그 SQL을 쓰지 않는 고정 파이프라인(`run_risk_batch` →
  `measure_detection --predict-only`)으로만 만들었지만, **설계상의 누출은 실재한다.**
- **2차 스냅샷은 이미 개봉됐다.** 라벨이 열렸고 결과가 공표됐으므로 이 스냅샷을 이후의 인간
  블라인드 평가나 새 회차의 블라인드 세트로 **재사용해서는 안 된다**. 재사용하면 그것은
  블라인드가 아니다. 새 회차가 필요하면 시드를 새로 사전 등록해 생성해야 한다.

---

## 3. 수요예측

출처 `reports/analytics/forecast_mape.json`의 `overall`. 예측 지평 14일, 기준일 둘 <!-- check: reports/analytics/forecast_mape.json:horizon_days = 14 -->
(2026-07-01·2026-07-15), 평가 대상 품목 x 기준일 쌍 248건. <!-- check: reports/analytics/forecast_mape.json:overall.items_evaluated = 248 -->

| 지표 | SES(채택 모델) | SMA(베이스라인) |
| --- | --- | --- |
| MAPE 평균 | 32.05% | 31.25% | <!-- check: reports/analytics/forecast_mape.json:overall.ses_mape_mean = 32.05% --><!-- check: reports/analytics/forecast_mape.json:overall.sma_mape_mean = 31.25% -->
| MAPE 중앙값 | 31.25% | 30.61% | <!-- check: reports/analytics/forecast_mape.json:overall.ses_mape_median = 31.25% --><!-- check: reports/analytics/forecast_mape.json:overall.sma_mape_median = 30.61% -->

- `baseline_improved` = **-2.56%p** — 채택 모델(SES)이 베이스라인(SMA)보다 **나쁘다**. <!-- check: reports/analytics/forecast_mape.json:overall.baseline_improved = -2.56%p -->
- SES 승률 33.06% — 품목 x 기준일 쌍 중 SES가 SMA보다 낮은 MAPE를 낸 비율이다. <!-- check: reports/analytics/forecast_mape.json:overall.ses_win_rate = 33.06% -->

**해석**: 이 데이터에서 지수평활은 단순이동평균을 이기지 못했다. 부호를 뒤집거나 유리한
부분집합을 고르지 않고 그대로 싣는다. 원인은 두 가지로 본다. 첫째, 이 스냅샷의 사용량은
대체로 완만한 수준 변동에 잡음이 얹힌 형태라, 최근값에 가중치를 더 주는 SES가 잡음을 따라가는
쪽으로 손해를 본다. 둘째, 시나리오 품목의 급증 구간은 전체 평가 쌍 중 소수라 평균 MAPE를
끌어올리지 못한다. 실무적 함의는 명확하다 — **수요예측은 현재 이 시스템의 강점이 아니며,
발주 제안의 근거로 쓸 때 그 불확실성을 함께 표시해야 한다.** 모델 교체(계절성·주간 패턴 반영)는
동결 범위 밖의 후속 과제다.

---

## 4. 플랫폼 품질

### 4.1 E2E

핵심 사용자 여정(상황실 → 워크벤치 → 공고 → 조치·발주 → 이력·알림)을 10회 반복 실행해 10/10 전 회차 무예외 통과했다. <!-- check: reports/platform/e2e_results.json:runs = 10 --><!-- check: reports/platform/e2e_results.json:passed_runs = 10 --><!-- check: reports/platform/e2e_results.json:runs = 10 --><!-- check: reports/platform/e2e_results.json:verdict = true -->
출처 `reports/platform/e2e_results.json`. LLM 키가 없는 환경에서 실행했으므로
(`environment.llm_keys = false`) 공고 단계는 "추출 미실행" 안내 경로를 지난다 — 키 확보 후
재실행 대상이다.

### 4.2 응답 성능(p95)

캐시를 우회한 원 함수를 30회 반복 호출해 측정했다. 판정 기준은 `p95 <= 2000ms`다. <!-- check: reports/platform/perf_results.json:repeats = 30 -->
출처 `reports/platform/perf_results.json`.

| 대상 | p95 |
| --- | --- |
| `list_items` | 0.7ms | <!-- check: reports/platform/perf_results.json:targets.list_items.p95_ms = 0.7 -->
| `load_overview` | 4.8ms | <!-- check: reports/platform/perf_results.json:targets.load_overview.p95_ms = 4.8 -->
| `load_item_detail` | 2.7ms | <!-- check: reports/platform/perf_results.json:targets.load_item_detail.p95_ms = 2.7 -->
| `assess_snapshot` | 416.5ms | <!-- check: reports/platform/perf_results.json:targets.assess_snapshot.p95_ms = 416.5 -->
| `notice_detail_sweep` | 0.3ms | <!-- check: reports/platform/perf_results.json:targets.notice_detail_sweep.p95_ms = 0.3 -->

전 대상이 기준을 만족한다(`verdict`). 최댓값인 `assess_snapshot`은 전 품목 위험 판정을 한 번에
수행하는 배치성 호출이다. <!-- check: reports/platform/perf_results.json:verdict = true -->

### 4.3 재현성

같은 스냅샷·같은 파라미터로 **subprocess를 5회씩 독립 실행**해 결과가 서로 같은지 본다. <!-- check: reports/platform/reproducibility.json:runs = 5 -->
프로세스 안에서 함수를 다시 호출하는 방식이 아니라 CLI를 다시 띄우는 방식이라, 전역 상태나
캐시가 결과를 붙들고 있는 경우도 드러난다. 출처 `reports/platform/reproducibility.json`.

| 계열 | 무엇을 비교하나 | 결과 |
| --- | --- | --- |
| 생성 | 스냅샷 `content_hash` | 5회 동일 + 봉인 앵커와 일치 | <!-- check: reports/platform/reproducibility.json:generation.runs = 5 --><!-- check: reports/platform/reproducibility.json:generation.identical = true --><!-- check: reports/platform/reproducibility.json:generation.anchor_match = true -->
| 배치 | `risk_results` 전 행 다이제스트 | 5회 동일(품목 124건) | <!-- check: reports/platform/reproducibility.json:batch.runs = 5 --><!-- check: reports/platform/reproducibility.json:batch.row_count = 124 --><!-- check: reports/platform/reproducibility.json:batch.identical = true -->
| 측정 | 감지 지표 JSON 다이제스트 | 5회 동일(원본 DB 불변) | <!-- check: reports/platform/reproducibility.json:detection.runs = 5 --><!-- check: reports/platform/reproducibility.json:detection.identical = true --><!-- check: reports/platform/reproducibility.json:detection.db_unchanged = true -->

**앵커 일치가 핵심이다.** "5회가 서로 같다"만으로는 결정성만 보이지만, <!-- check: reports/platform/reproducibility.json:generation.runs = 5 -->
생성 계열은 그 해시가 동결 시점에 봉인한 앵커 값과도 같다 — 지금 재생성해도 측정 기반이
그때와 동일하다는 뜻이다. <!-- check: reports/platform/reproducibility.json:generation.anchor_match = true -->

---

## 5. LLM 품질 — **실측 전**

API 키가 없는 환경이라 이 절의 수치는 **하나도 측정되지 않았다**. 준비된 장치와, 키를 넣으면
무엇이 어떤 순서로 실행되는지를 적는다. 수치 자리에 추정치를 넣지 않는다.

| 항목 | 준비된 장치 | 수치 |
| --- | --- | --- |
| 공고 추출 정확도 | 골드 라벨(`data/notices/gold/gold_labels_v1.json` — 공고 전건, 추출 로직과 독립 작성) + 필드별 일치율·`needs_review` 재현/정밀을 산출하는 `scripts/measure_extraction.py` | **실측 전** |
| 생성 품질 judge | 평가셋 40건(`eval/cases/eval_cases_v1.json` — 자기완결 evidence 동봉·해시 고정) + 고정 루브릭(`eval/rubric.md`) + 교차 judge 실행기(`eval/judge.py`) | **실측 전** | <!-- check: eval/cases/eval_cases_v1.json:meta.case_count = 40 -->
| 인간 교차검토 | judge 결과와 대조할 표본(파일럿 케이스가 평가셋에 사전 지정돼 있다) | **실측 전** |

설계상 고정된 것들:

- **교차 judge**: 생성 공급자와 채점 공급자를 다르게 강제한다. 생성이 Anthropic이면 judge는
  OpenAI, 생성이 OpenAI면 judge는 Anthropic이다. 같은 공급자가 자기 생성물을 채점하지 않는다.
- **`temperature = 0` · 스냅샷 고정**: 재현 가능한 채점을 위해 온도를 구조적으로 배제했다. 다만
  현재 `eval/config.yaml`의 모델 값은 **별칭**(`gpt-5`·`claude-opus-5`)이며, 키 확보 후 날짜
  스냅샷 ID로 교체해야 한다는 TODO가 남아 있다. 지금 상태로 채점하면 모델 버전이 고정되지
  않는다는 뜻이므로, 이 교체는 judge 본실행의 선결 조건이다.
- **평가 지표**: 근거충실성 · 원인관련성 · 대응실행가능성(각각 연속값)과 환각 여부(불리언).
- **평가셋 해시 고정**: 케이스 구성이 바뀌면 해시가 바뀌므로 결과와 평가셋 버전의 결속이
  깨지지 않는다.

키 확보 후 실행 순서는 README의 런북에 고정돼 있다(`process_notices --all` → 배치 재실행 →
`warm_cache` → `measure_extraction`). **순서가 중요한 이유**는 공고 매핑이 먼저 반영돼야
§2의 감지 수치가 "공고 반영 조건"으로 재측정되기 때문이다.

---

## 6. 한계와 알려진 문제

1. **`overdue_cutoff`에 일수 임계가 없다.** 입고가 하루 늦든 여덟 달째 안 오든 똑같이 그
   품목의 미래 예정 입고를 전부 미반영으로 돌린다. 이 시뮬레이터에서 재발주 저점의 재고
   커버리지가 대체로 열흘 남짓이라, 그 스위치가 켜지면 사실상 자동으로 '경고'가 된다. 실제
   병원 데이터에서도 사소한 입고 슬립은 흔하므로 **오탐률의 실재하는 원인**이다. 동결 대상이라
   이번 범위에서는 고치지 않고 문서화만 했다 — 가장 먼저 손볼 개선 후보다.
2. **블라인드 라벨의 잔존 식별 가능성.** 미끼 하나(양성 지연)는 `receipt_delay_days` 임계
   미만이라 이상신호를 내지 않는다는 전제로 설계됐지만, 위 1번 때문에 등급은 실제로 움직였다.
   2차에서는 미끼가 없어 SQL 한 줄 역산이 다시 가능해졌다(§2.4). 근본 해법은 미끼 상수 조정이
   아니라 1번의 수정이다.
3. **재시도 낙관 편향.** 스냅샷 생성기는 시나리오 효과가 조건을 만족할 때까지 재추첨한다.
   경계선에서 막 무효과로 떨어지는 조합이 결과 분포에서 사실상 배제되므로, 감지율이 균등
   무작위 표본보다 낙관적으로 나올 수 있다. 2차에서 이 편향은 1차보다 **커졌다**(§2.4).
4. **라벨 표본이 작다.** 블라인드는 시드마다 유형별로 라벨 하나씩뿐이라, 품목 하나의
   감지/미감지가 유형별 비율을 통째로 흔든다. 유형별 전건 감지를 "이 유형은 항상 잡힌다"로
   일반화할 수 없다.
5. **ITM-0011 — 공고 경로로 보완 예정.** 표준 스냅샷에서 감지되지 않은 라벨 하나는 봉쇄된
   발주의 예정일이 스윕 종료일 밖이라, 스윕 내내 "아직 예정일이 안 온 정상 pending"으로 보인
   사례다. 파라미터로는 닿지 않으며, 공고 추출·매핑이 반영되면 활성 공고에 의한 등급 상향으로
   잡힐 여지가 있다(키 확보 후 재측정 항목).
6. **품절 품목의 발주 제안이 비어 있다.** 발주 산식은
   `shortage = max(0, 예측수요 - 재고 - 미래입고)`인데, 이미 품절된 품목은 사용량 실적이
   없어 예측 수요도 바닥으로 내려간다.
   결과적으로 "가장 급한 품목에 발주 제안이 뜨지 않는" 표시가 나온다. 앱 로직 층위 문제이며
   (동결 무관), 품절 전 구간 평균을 쓰는 등의 보정이 후속 과제다.
7. **SES가 베이스라인에 뒤진다**(§3). 채택 모델이 단순이동평균보다 나쁜 상태로 동결돼 있다.
8. **모든 감지·오탐 수치는 공고 미반영 조건이다.** 공고는 적재만 됐고 추출·매핑되지 않아 활성
   공고에 의한 등급 상향이 없는 상태에서 측정했다. 추출을 실행하면 재측정 대상이다.
9. **AI 근거 설명의 role-blind 한계.** 설명 생성 프롬프트는 역할 간 교차 인용을 완전히 막지
   못한다. 이 한계는 코드 docstring에 명시돼 있고 화면 표시에서도 "부분 신호"로만 취급한다 —
   이 시스템의 판정 근거는 결정적 파이프라인이며 LLM 설명은 그 근거를 서술할 뿐이다.
10. **지평 기준이 회차별로 다르다.** 1차 채점 시점의 지평 기준은 상한만 있었고 현재 기준은
    상·하한을 모두 본다. 이 문서는 서로 다른 기준의 수치를 나란히 놓지 않으며, 기준이 통일된
    대조는 §2.4 (iii)의 재채점 결과 하나뿐이다. `detection_metrics.json`에 기록된
    `within_horizon.criterion` 문자열은 옛 기준 그대로다(동결 파일 무수정).

---

## 7. 재현 절차

### 7.1 환경과 스냅샷

README의 "빠른 시작" 절 ①~⑥을 그대로 따르면 측정 기반이 갖춰진다(가상환경 → 의존성 →
스냅샷 2단계 생성 → 정합성 검증 → 위험 평가 배치 → 화면 기동). 정합성 검증 단계에서
`content_hash`가 §1.2의 앵커와 일치하는지 확인하는 것이 가장 중요하다.

### 7.2 측정 명령

```bash
# 감지·오탐·선행일수 (§2)
python scripts/measure_detection.py --db data/medsupply.db \
    --labels data/scenarios/ground_truth/standard_v1.json \
    --start 2026-07-01 --end 2026-08-01 --out reports/analytics/detection_metrics.json

# 수요예측 MAPE (§3)
python scripts/measure_mape.py --db data/medsupply.db \
    --as-of 2026-07-01 --as-of 2026-07-15 --out reports/analytics/forecast_mape.json

# 플랫폼 (§4)
python scripts/run_e2e.py --db data/medsupply.db --runs 10 --out reports/platform/e2e_results.json
python scripts/measure_perf.py --db data/medsupply.db --repeats 30 --out reports/platform/perf_results.json
python scripts/verify_reproducibility.py --runs 5 --out reports/platform/reproducibility.json \
    --labels data/scenarios/ground_truth/standard_v1.json \
    --detection-start 2026-07-01 --detection-end 2026-08-01
```

### 7.3 블라인드 1차 봉인 예측 재채점 (§2.4 (iii))

```bash
# 시드 20260901~20260905 각각에 대해
python scripts/measure_detection.py \
    --score data/blind/tmp/predictions/blind_<seed>.pred.json \
    --labels data/blind/sealed/blind_<seed>.labels.json \
    --out <tmp>/blind_<seed>_rescored.json
```

예측 파일과 봉인 라벨은 `.gitignore` 대상이라 저장소에 없다(sha256만 `data/blind/manifest.json`에
커밋돼 있다). 재현하려면 봉인 산출물이 남아 있는 작업 환경이 필요하며, 재채점 결과 요약은
`reports/analytics/blind_round1_rescored.json`에 커밋돼 있다.

### 7.4 이 문서의 수치 대조

```bash
.venv/bin/python scripts/check_report_numbers.py
.venv/bin/python -m pytest tests/ -q
```

첫 명령은 이 문서의 모든 마킹을 `reports/`의 JSON과 대조하고 결과를
`reports/platform/report_check.json`에 쓴다. 테스트 스위트에도 같은 대조가 회귀 테스트로 들어
있어(`tests/platform/test_check_report_numbers.py`), 측정을 다시 돌려 수치가 바뀌면 이 문서를
고치기 전까지 스위트가 그린이 되지 않는다.
