# MedSupply Radar

의약품 수급위험 관제 프로토타입이다. 선경병원 약제부의 재고·사용량·입고·공급중단 공고를
결합해 품목별 위험 등급(위험/경고/주의/정상)을 산정하고, 소진 예상·대체 후보·조치 이력을
한 화면에서 다룬다. **판정(등급·점수)은 전부 `medsupply.analytics`의 결정적 순수 함수가
산출**하며, LLM은 그 판정을 재현·수정하지 않고 공고 구조화 추출과 "왜 위험한가" 원인 설명·
대응방안 생성에만 관여한다(판정과 생성의 분리 — `docs/llm-pipeline.md` §1). LLM 없이도
①~⑥ 전 과정을 실행할 수 있다.

## 요구 사항

- **Python 3.14**(Homebrew 배포판 기준 — 이 저장소의 `.venv`는
  `/opt/homebrew/bin/python3.14`(3.14.6)로 만들어졌다. 미설치 시 `brew install python@3.14`).
- 가상환경(venv) — 저장소 전용으로 격리해 쓴다.
- 의존성 설치 — `requirements.txt`(streamlit·pandas·plotly·pyyaml·anthropic·openai·
  pydantic·python-dotenv). 테스트 실행에는 `requirements-dev.txt`(pytest)가 추가로
  필요하다(§테스트).

## 빠른 시작(30분 경로)

아래 ①~⑥은 처음 clone한 상태에서 화면 기동까지의 최소 경로다. 전 단계를 tmp 디렉터리에서
실제로 실행해 검증했다 — 표준 스냅샷은 `data/medsupply.db`가 아닌 별도 경로에 생성해
원본을 건드리지 않았고, streamlit 기동도 그 tmp 스냅샷을 가리키는 격리된 디렉터리에서
헤드리스로 짧게 확인한 뒤 즉시 종료했다. 실측 소요는 각 단계 뒤에 괄호로 적는다(이 개발
머신 기준 — pip 캐시가 이미 있고 데이터셋이 124품목으로 작아 실제로는 30분보다 훨씬
짧게 끝난다. 냉장 네트워크 다운로드·수기 입력 시간은 포함하지 않은 순수 명령 실행 시간이다).

① **가상환경 구성**
```bash
python3.14 -m venv .venv
source .venv/bin/activate
```
(venv 생성 실측 2초)

② **의존성 설치**
```bash
pip install -r requirements.txt
```
(pip 캐시 존재 시 실측 12초 — 콜드 네트워크에서는 더 걸릴 수 있다)

③ **표준 스냅샷 2단계 생성** — 명령은 `data/scenarios/standard_snapshot.sha256` 주석에 적힌
그대로다(생성 → 공고 적재):
```bash
python scripts/generate_dataset.py --config data/scenarios/scenario_config.yaml \
    --out data/medsupply.db --seed 20260801 --base-date 2026-08-01
python scripts/load_notices.py --db data/medsupply.db \
    --raw data/notices/raw --index data/notices/notices_index.csv
```
(2단계 합계 실측 1초, 124품목·45,260행. 두 번째 명령 완료 후 `meta.content_hash`가
`c34bf4cb9215e1ba4b7a21c9d0f1d7236e2164ae08b630bf54fdf82d4c2f6483`로 봉인 앵커와 일치함을
tmp 재생성으로 재확인했다)

④ **정합성 검증**
```bash
python scripts/validate_dataset.py --db data/medsupply.db \
    --expect-hash @data/scenarios/standard_snapshot.sha256
```
(실측 1초 미만, `VALIDATION PASSED (11/11)`)

⑤ **위험 평가 배치** — 전일·당일 2개 run을 만든다(등급 변동 비교의 기준선):
```bash
python scripts/run_risk_batch.py --db data/medsupply.db \
    --as-of 2026-07-31 --as-of 2026-08-01
```
(실측 11초. 이 시점에는 공고가 `notices` 테이블에 적재만 됐을 뿐 아직 `process_notices`로
추출·매핑되지 않았으므로 공고발 등급 상향은 0건이다 — 실행 출력에 `상향(공고) 건수: 0건`이
두 run 모두 그대로 찍힌다. §LLM 기능 참조)

⑥ **화면 기동**
```bash
streamlit run app.py
```
(헤드리스 `--server.port 8503`으로 기동해 `curl`로 HTTP 200 응답까지 실측 1초. 브라우저는
띄우지 않고 확인 직후 프로세스를 종료했다)

⑦ **(선택) 측정 재현** — reports/의 수치를 직접 재현하려면 아래 5개를 각 1줄로 실행한다
(전부 tmp 스냅샷·tmp 출력으로 검증 완료, 원본 `reports/*.json`은 건드리지 않았다):
```bash
python scripts/measure_detection.py --db data/medsupply.db \
    --labels data/scenarios/ground_truth/standard_v1.json \
    --start 2026-07-01 --end 2026-08-01 --out reports/analytics/detection_metrics.json
python scripts/measure_mape.py --db data/medsupply.db \
    --as-of 2026-07-01 --as-of 2026-07-15 --out reports/analytics/forecast_mape.json
python scripts/run_e2e.py --db data/medsupply.db --runs 10 --out reports/platform/e2e_results.json
python scripts/measure_perf.py --db data/medsupply.db --repeats 30 --out reports/platform/perf_results.json
python scripts/verify_reproducibility.py --runs 5 --out reports/platform/reproducibility.json \
    --labels data/scenarios/ground_truth/standard_v1.json \
    --detection-start 2026-07-01 --detection-end 2026-08-01
```
(실측: measure_detection 13초 · measure_mape 1초 미만 · run_e2e 5초 · measure_perf 13초 ·
verify_reproducibility 74초 — 5개 합계 약 106초. 재현된 수치는 §측정 결과 요약 표와
소수점까지 일치했다)

**①~⑥ 핵심 경로 실측 합계: 약 27초** · **①~⑦ 전부 포함 실측 합계: 약 133초(2분 13초)**.
예산으로 잡은 30분에 크게 못 미치므로, 새 환경(콜드 네트워크·수기 타이핑 포함)에서도
30분 안에 충분히 여유 있게 끝난다고 볼 수 있다.

## LLM 기능(선택)

키가 없어도 ①~⑥은 전부 동작한다(공고는 원문 그대로 열람 가능, AI 근거 설명 탭은 "아직
생성되지 않았습니다" 안내로 대체). 키를 확보하면:

1. `.env.example`을 복사해 키를 채운다.
   ```bash
   cp .env.example .env
   ```
   `.env.example` 실제 내용(9줄): `ANTHROPIC_API_KEY`·`OPENAI_API_KEY`·
   `ANTHROPIC_MODEL=claude-opus-5`·`OPENAI_MODEL=gpt-5`·`LLM_PROVIDER=auto`·
   `LLM_MODE=online`·`LANGFUSE_HOST`·`LANGFUSE_PUBLIC_KEY`·`LANGFUSE_SECRET_KEY`.
   `LLM_PROVIDER`: `auto`(기본, Anthropic 우선·자격 있는 오류만 OpenAI로 폴백) |
   `anthropic` | `openai`(단일 공급자 강제, 폴백 없음). `LLM_MODE`: `online`(기본, 실제
   호출) | `offline`(캐시 히트만 서빙, 미스면 즉시 실패 — 아래 3 참조).

2. **런북 순서(키 확보 후, 이 순서 그대로)**:
   ```bash
   python scripts/process_notices.py --db data/medsupply.db --all
   python scripts/run_risk_batch.py --db data/medsupply.db \
       --as-of 2026-07-31 --as-of 2026-08-01
   python scripts/warm_cache.py --db data/medsupply.db
   python scripts/measure_extraction.py --db data/medsupply.db \
       --gold data/notices/gold/gold_labels_v1.json --out reports/llm/extraction_accuracy.json
   ```
   순서가 고정인 이유: `process_notices --all`이 공고를 추출·매핑해 `notice_item_map`을
   채워야 `run_risk_batch` 재실행에서 활성 공고발 등급 상향(`escalate_on_notice`)이
   비로소 반영된다 — **§측정 결과 요약 표의 첫 감지·오탐·선행 표(공고 반영 후 재측정
   행 제외)는 이 재실행 이전, 즉 공고가 매핑되지 않아 상향이 0건인 조건에서 측정된
   값**이다(위 ⑤에서 실측으로 확인한 그대로). 이 순서를 실제로 실행한 뒤 같은 조건으로
   재측정한 값은 같은 절 아래 "공고 반영 후 재측정" 소표에 병기돼 있다(Task X-3).
   `warm_cache`는 그다음(공고 매핑이 먼저 반영돼야 설명 근거의 활성 공고 목록이
   정확하다), `measure_extraction`은 추출 결과가 쌓인 뒤에야 골드 대조가 의미 있다.

3. **오프라인 시연 모드**: `LLM_MODE=offline`으로 두면 `warm_cache`가 미리 채운 캐시
   히트만 재생하고, 캐시에 없는 입력은 호출 대신 즉시 실패한다(`LLMOfflineError`) —
   키 없이도 이미 웜업된 입력에 한해 결정적으로 재생할 수 있는 시연 모드다. 캐시 키·
   흐름·tracing 계약은 `docs/llm-pipeline.md` 전체를 참조.

4. **다른 머신에서 무과금 재현(키 불요)**: LLM 응답 캐시(`data/llm_cache.db`, 91건)가
   저장소에 커밋돼 있으므로, 새로 clone한 머신에서 아래를 실행하면 API 키·과금 없이
   현재와 동일한 상태(추출 20건·매핑 47행·상향 28건·설명 71건)가 재구성된다 —
   tmp 재현으로 전 단계 검증했다(캐시 적중 91건, API 호출 0, 등급 분포 비트 단위 일치):
   ```bash
   # 빠른 시작 ①~⑤(스냅샷 생성·검증·배치)를 먼저 실행한 뒤:
   LLM_MODE=offline python scripts/process_notices.py --db data/medsupply.db --all
   python scripts/run_risk_batch.py --db data/medsupply.db --as-of 2026-07-31 --as-of 2026-08-01
   LLM_MODE=offline python scripts/warm_cache.py --db data/medsupply.db
   streamlit run app.py
   ```
   주의: 공고 원문·프롬프트가 바뀌면 캐시 키가 달라져 offline 재생이 실패한다(그 경우
   키를 넣고 online으로 재워밍해야 하며 그때만 과금된다).

## 테스트

```bash
pip install -r requirements-dev.txt   # pytest만 추가로 필요
.venv/bin/python -m pytest tests/ -q
```
현재 **1317 passed, 4 skipped**(4건은 아래 실 API 스모크 — RUN_LLM_SMOKE 미설정 시
항상 skip).

**실 API 스모크**: judge·추출·설명 생성 각 1~2건은 실제 Anthropic/OpenAI API를 호출한다
— `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`가 설정돼 있어도 기본은 skip이고,
`RUN_LLM_SMOKE=1`을 추가로 지정해야만 실행된다(소액 과금 발생, `tests/llm_smoke.py`
공용 게이트).

**격리 가드**: `tests/test_isolation.py`가 저장소 전역을 ast로 정적 검사해 "로직이 정답을
볼 수 없다"를 기계적으로 강제한다 — 순방향(`medsupply/`+`app.py`+지정된 `scripts/` 일부는
`scripts.datagen`·`scripts.measure_detection`을 import하지 않고 `data/scenarios`·
`ground_truth`·`data/notices/gold` 경로를 코드 값으로 쓰지 않음)과 역방향
(`scripts/datagen/`은 `medsupply`를 import하지 않음) 양쪽을 막는다. **`scripts/` 아래
새 최상위 파일을 추가하면 `tests/test_isolation.py`의 `SCRIPTS_PATH_TARGETS` 딕셔너리에
수동으로 등록해야** 이 정적 검사 대상에 포함된다 — 등록하지 않아도 테스트가 실패하지는
않고 그냥 검사 범위에서 조용히 빠지므로, 신규 스크립트를 추가할 때 빠뜨리지 않도록
주의해야 한다.

## 데이터 주의

- `data/medsupply.db`·`data/blind/`는 **미추적**(`.gitignore`로 명시 제외 — 재생성이
  결정적이라 파일을 나를 필요가 없다). 지워지거나 손상되면 위 ③(+ §LLM 기능 4의
  오프라인 재현)으로 다시 만든다. 반면 `data/llm_cache.db`는 **커밋 대상**이다 —
  LLM 응답의 원장이라 이것만 있으면 어느 머신에서든 무과금·무키로 동일 상태를
  재구성할 수 있다(§LLM 기능 4).
- **`validate_dataset` 검사 9(action_history 시드 8건)**는 앱(검토 워크벤치)에서 조치를
  저장하면 `action_history` 행수가 8건을 넘어가 **자연히 FAIL로 바뀐다 — 이것은 정상
  동작**이다(앱을 정상적으로 썼다는 증거). 표준 스냅샷으로 되돌리려면 ③을 다시 실행한다.

## 측정 결과 요약 표

수치는 전부 `reports/`의 실측 JSON에서 그대로 옮긴 값이며, 옆에 원본 파일 경로를 병기한다.
합격·불합격을 판단하는 문장은 이 문서에 없다 — 각 파일의 원 수치를 그대로 확인하라.

**감지·오탐·선행일수**(표준 스냅샷, `config_hash=6ec9bf05`, 스윕 2026-07-01~2026-08-01,
`reports/analytics/detection_metrics.json`):

| 문턱 | 표본 범위 | 감지율 | 오탐률 | 선행일수(중앙값) |
| --- | --- | --- | --- | --- |
| 주의 이상 | raw(라벨 20 / 정상 104) | 90.0%(18/20) | 45.2%(47/104) | 26.5일 |
| 주의 이상 | 지평 내(라벨 15, 2026-08-31까지) | 93.3%(14/15) | 45.2%(47/104) | 15.0일 |
| 경고 이상 | raw(라벨 20 / 정상 104) | 65.0%(13/20) | 3.8%(4/104) | 10일 |
| 경고 이상 | 지평 내(라벨 15) | 66.7%(10/15) | 3.8%(4/104) | 9.0일 |

참고로 최고등급('위험') 정밀도는 100.0%(같은 파일 `results.danger_precision`)다.

**공고 반영 후 재측정**(Task X-3, 같은 스냅샷·같은 `config_hash=6ec9bf05`·같은 스윕 —
`process_notices.py --all`로 공고 20건을 추출·매핑하고 위험 배치를 재실행한 뒤 다시 측정,
`reports/analytics/detection_metrics_with_notices.json`):

| 문턱 | 표본 범위 | 감지율 | 오탐률 | 선행일수(중앙값) |
| --- | --- | --- | --- | --- |
| 주의 이상 | raw(라벨 20 / 정상 104) | 90.0%(18/20) | 57.7%(60/104) | 31.5일 |
| 주의 이상 | 지평 내(라벨 15, 2026-08-31까지) | 93.3%(14/15) | 57.7%(60/104) | 22.5일 |
| 경고 이상 | raw(라벨 20 / 정상 104) | 80.0%(16/20) | 14.4%(15/104) | 14.0일 |
| 경고 이상 | 지평 내(라벨 15) | 86.7%(13/15) | 14.4%(15/104) | 10일 |

최고등급('위험') 정밀도는 90.0%(공고 미반영 조건은 100.0%). 감지된 라벨 집합은 두 조건에서
동일하다(미감지 2건 — ITM-0011·ITM-0087 — 반영 전후 그대로) — 같은 탐지기·같은 문턱이고
달라진 것은 공고 매핑 데이터의 존재뿐이다. 두 조건의 전체 비교와 메커니즘 설명은
`docs/verification-report.md` §2.5 참조.

**MAPE**(SES 채택 모델 vs SMA 베이스라인 병기, horizon 14일, as_of 2개,
`reports/analytics/forecast_mape.json` `overall`):

| 지표 | SES(채택) | SMA(베이스라인) |
| --- | --- | --- |
| MAPE 평균 | 32.05% | 31.25% |
| MAPE 중앙값 | 31.25% | 30.61% |

`baseline_improved = -2.56%p`(SES가 SMA보다 낮지 않다 — 음수), `ses_win_rate = 33.06%`
(248건의 품목×as_of 쌍 중 SES가 SMA보다 낮은 MAPE를 낸 비율).

**E2E**(핵심 사용자 여정 5단계 × 10회, `reports/platform/e2e_results.json`): 10/10 회차
모두 5단계(상황실→워크벤치→공고→조치·발주→이력·알림) 전부 무예외 통과.

**p95 지연**(핵심 조회 5종 × 30회 반복, `reports/platform/perf_results.json`):

| 대상 | p95 |
| --- | --- |
| list_items | 0.7ms |
| load_overview | 4.8ms |
| load_item_detail | 2.7ms |
| assess_snapshot | 416.5ms |
| notice_detail_sweep | 0.3ms |

**재현성**(subprocess 수준 5회, `reports/platform/reproducibility.json`): 생성(콘텐츠
해시)·배치(risk_results 튜플 집합)·측정(감지 지표) 3계열 전부 5회 결과가 서로
동일(`identical: true`)했고, 생성 계열은 봉인 앵커(`standard_snapshot.sha256`)와도
일치했다.

**블라인드 평가**(`data/scenarios/`를 전혀 참조하지 않는 role-blind 스냅샷, 표준과 같은
`config_hash=6ec9bf05`·같은 스윕 구간):

| 회차 | seed 수 | 감지율(평균) | 오탐률(평균) | 선행일수 중앙값(평균) |
| --- | --- | --- | --- | --- |
| 1차(S-30) | 5(20260901~05) | 35.0%(25~50%) | 55.5%(47.5~60%) | 45.5일(32~61.5일) |
| 2차(S-30c) | 5(20260911~15) | 100.0%(전 seed) | 44.7%(41.7~49.2%) | 14.4일(8.5~19.5일) |

1차: `reports/analytics/blind_summary.json`. 2차: `reports/analytics/blind_round2_summary.json`.

> **2차 감지율 100%를 읽는 법**: 탐지기가 개선된 결과가 아니다. **탐지기 코드·판정
> 문턱·산식은 1차와 완전히 동일**하다(`config_hash=6ec9bf05` 그대로, 파라미터
> 무변경). 바뀐 것은 **블라인드 라벨의 배치 설계**뿐이다 — 1차는 라벨 20건 중 12건이
> 스윕 시작 이전에 이미 품절되어 있어(`stockout_date < sweep_start`) 어떤 예측으로도
> 감지 성공 판정을 받을 수 없는 "구조적으로 채점 불가"한 라벨이었다(2차 사후 진단
> `unscoreable_labels`). 2차는 시나리오 배치 조건을 측정 창(스윕 구간 + watch_days)
> 안에 들어오도록 결합해 이 채점 불가 라벨을 0건으로 없앴을 뿐이다. 즉 35.0%→100.0%는
> "같은 시험을 다시 봐서 더 잘 봤다"가 아니라 "1차 시험 문항의 60%가 애초에 풀 수 없는
> 문제였고, 2차에서 그 문항들을 들어냈다"는 뜻에 가깝다. 반대로 오탐률(2차 44.7% vs
> 표준 45.2%)은 2차에서 미끼 주입이 0건이라 표준과 like-for-like 비교가 처음으로
> 성립한다.

**공고 추출 정확도**(Task X-2 실측, `reports/llm/extraction_accuracy.json`): 관용(lenient)
대조 macro_accuracy 100.0%. 이 100%는 N-001·N-014·N-017 세 건에서 사전 등록된 대안값
(재개일자/정상화 예상일자 이중 표기, S-24 리뷰 근거)을 정답으로 인정한 결과다 — 그
인정을 걷어낸 strict 대조로는 expected_restart_date 85.0%(17/20)·macro_accuracy 97.0%로
내려가고, needs_review recall이 0.0%(tp=0/fn=3)다(게이트가 이 규약 차이 세 건을 전부
자동확정으로 통과시켰다는 뜻). 두 대조의 전체 비교는 `docs/verification-report.md` §5 참조.

**AI 설명 사후 대조 플래그**(hallucination_flags, `warm_cache.py`가 생성한 원인 설명 71건
중 직접 집계 — `data/llm_cache.db`는 미추적이라 committed JSON 출처가 없다): 44건
(62.0%)에 플래그가 1개 이상 있다. role-blind 부분 신호일 뿐 무결 보증이 아니며
(`medsupply/llm/grounding.py`), 워크벤치 화면에 "사후 대조 경고 N건" 배지로 그대로
노출된다 — 유리한 수치와 같은 자리에서, 불리한 수치도 감추지 않는다.

## 디렉터리 구조 개요

```
MedSupply-Radar/
├── app.py                     Streamlit 진입점(st.Page/st.navigation, 7개 페이지)
├── medsupply/                  애플리케이션 패키지
│   ├── data/                     DB 연결(db.py)·조회(queries.py)·쓰기 단일 경로(writer.py)·schema.sql
│   ├── analytics/                 결정적 위험 판정 파이프라인(순수 함수 + pipeline.py 결선)
│   ├── llm/                       LLM 호출·캐시·근거 조립·설명 생성·워밍·tracing(docs/llm-pipeline.md)
│   ├── services/                   화면용 캐시 조회 계층(st.cache_data/resource)
│   ├── views/                      페이지별 렌더 함수(situation·review·orders·history·notices·alerts·evaluation)
│   └── ui/                         공용 컴포넌트·차트·등급 배지
├── scripts/                    CLI 진입점(생성·적재·배치·측정·검증 — docs/interfaces.md §scripts)
│   └── datagen/                    시나리오 생성기(medsupply 미참조 — 격리 원칙)
├── eval/                       LLM 평가(judge 채점·40건 평가셋 구성·루브릭)
├── config/analytics_params.toml  판정 파라미터 단일 소스
├── data/
│   ├── reference/                  성분·품목·대체군·이력 시드 마스터 CSV(커밋)
│   ├── notices/                    공고 원문·색인·골드 라벨(커밋)
│   ├── scenarios/                   시나리오 config·ground truth·동결 앵커(커밋, 격리 대상)
│   ├── blind/                       블라인드 평가 스냅샷(라벨 봉인, manifest.json만 커밋)
│   ├── medsupply.db                 표준 스냅샷(미추적 — §데이터 주의)
│   └── llm_cache.db                 LLM 결과 캐시(미추적)
├── reports/                    측정 결과 JSON(analytics/llm/platform — §측정 결과 요약 표)
├── tests/                      pytest 스위트 + 저장소 전역 격리 가드(test_isolation.py)
└── docs/                       data-model.md·metrics-spec.md·interfaces.md·llm-pipeline.md
```
