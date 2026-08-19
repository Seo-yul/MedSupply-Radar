# Metrics Specification

## 결과 JSON 공통 헤더

모든 측정 스크립트는 다음 구조의 JSON을 생성해야 한다:

```json
{
  "meta": {
    "dataset_content_hash": "<meta.content_hash>",
    "config_hash": "<analytics params_hash>",
    "labels_version": "<ground truth 파일명 또는 'n/a'>",
    "params_ref": "config/analytics_params.toml",
    "generated_at": "<ISO8601>",
    "measured_by": "<스크립트 경로>"
  },
  "results": { }
}
```

## 지표 산식 정의

### 감지 (Detection)
- **정의**: 스윕 기간 중 '주의' 이상 최초 판정일이 라벨의 stockout_date 이전이면 감지 성공
- **감지율**: 감지 성공 시나리오 수 / 전체 시나리오 수

### 선행일수 (Lead Time)
- **정의**: stockout_date − 최초 감지일 (일 단위, 감지 성공 건만)

### 오탐 (False Positive)
- **정의**: 라벨에 없는(정상) 품목이 스윕 기간 중 1회라도 '주의' 이상 판정되면 오탐 1건
- **오탐률**: 오탐 품목 수 / 정상 품목 수

### 최고등급 정밀도 (High-Risk Precision)
- **정의**: '위험' 판정 품목 중 실제 시나리오 품목 비율
- **분모**: '위험' 판정 품목 수

### MAPE (Mean Absolute Percentage Error)
- **정의**: 품목별 mean(|forecast−actual| / actual), actual=0 구간 제외
- **대상**: 사용량 상위 20개 + 시나리오 품목 전수
- **비교**: SES(채택)와 SMA(베이스라인)를 병기
- **개선판정**: 품목별 `baseline_improved`(SES MAPE < SMA MAPE) 포함

### 추출 정확도 (Extraction Accuracy)
- **정의**: 공고 20건 × 필드(품목명·성분·사유·기간)별 골드라벨 일치율 + needs_review 적중률

### 재현성 (Reproducibility)
- **정의**: 동일 스냅샷 5회 독립 실행(subprocess)의 전 품목 등급·수치 100% 일치 여부

### E2E (End-to-End)
- **정의**: "조회→품목 상세→위험 확인→대응방안 생성→이력 기록" 5단계 10회 중 무오류 횟수
- **합격기준**: ≥9회

### 응답 성능 (Response Performance)
- **정의**: 대시보드 서버사이드 렌더 10회 p95 ≤ 2초

### LLM 생성 품질 (LLM Quality)
- **평가지표**:
  - 근거충실성 (Faithfulness): 0~1
  - 원인관련성 (Relevance): 0~1
  - 대응실행가능성 (Actionability): 0~1
  - 환각 (Hallucination): Boolean
- **평가방식**: LLM-as-a-Judge 고정 루브릭
- **Judge 모델**: 생성 모델과 다른 계열

## 파일 배치 규약

### 분석 결과 (Analytics Results)
- `reports/analytics/detection_metrics.json`
- `reports/analytics/forecast_metrics.json`
- `reports/analytics/reproducibility_metrics.json`

### LLM 평가 결과 (LLM Evaluation Results)
- `reports/llm/extraction_metrics.json`

### 종합 평가 결과 (System Evaluation Results)
- `eval/results/experiments.json`
- `eval/results/system_quality.json`
