# LLM 파이프라인 요약

MedSupply Radar에서 LLM이 관여하는 두 경로 — 공고 구조화 추출(M-13/M-14)과 위험 원인 설명
생성(M-20/M-21) — 을 한 페이지로 요약한다. 함수별 상세 계약은 각 모듈 docstring과
`docs/interfaces.md`(llm 절)를 참조한다.

## 1. 원칙 — 판정과 생성의 분리

위험 등급·점수는 `medsupply.analytics`의 순수 함수(결정적, LLM 미관여)가 이미 확정해
`risk_results`에 저장한 값이다. LLM은 그 판정을 재현하거나 수정하지 않는다 — LLM 출력
스키마 `RiskExplanation`(`medsupply/llm/schemas.py`)에는 등급·점수 필드가 아예 없다
(마스터 플랜 결정 38). LLM의 역할은 "왜 그런가"(원인 설명)와 "그래서 무엇을 확인할지"
(대응방안)를 사람이 읽을 문장으로 채우는 것뿐이다.

## 2. 흐름도

```
공고 원문(notices.raw_text)
  │
  ▼
extract_notice()                    medsupply/llm/extraction.py
  · LLM 호출(complete_json) → NoticeExtraction
  · 사후(LLM 미관여): _verify()가 발췌(evidence_quotes)를 원문과 문자열 대조하고
    4조건 게이트를 전부 만족해야 '자동확정':
      ① confidence >= 0.8(CONFIDENCE_THRESHOLD)
      ② 미발견 evidence_quotes 0건
      ③ 필수 필드 결손 0건(product_names/reason/notice_type)
      ④ 날짜(halt_start_date/expected_restart_date) 파싱 성공
    하나라도 어긋나면 '확인 필요'다 — '확인 완료'는 사람이 검토를 마쳤을 때만 붙는
    상태라 이 함수는 절대 반환하지 않는다.
  ▼
map_extraction_to_items()           medsupply/llm/mapping.py — LLM 미관여(결정적 문자열 매칭)
  · ingredient_names를 성분명·별칭에 매칭(정확 우선→부분 보조) → 실패 시에만 product_names를
    품목명 정확 일치로 보조 매칭. needs_review는 매칭 근거·추출 status로 행 단위 결정.
  ▼
save_notice_extraction()            medsupply/data/writer.py
  · notice_extractions INSERT OR REPLACE + notice_item_map 전량 교체(멱등)
  (위 세 단계는 process_notice()가 한 번에 오케스트레이션 — scripts/process_notices.py 진입점)

[별도 경로 — 위험 평가(risk_results)는 analytics 배치가 이미 결정적으로 완료했다고 전제]
risk_results(as_of 확정 완료)
  ▼
collect_risk_evidence()             medsupply/llm/grounding.py — LLM 미관여, 재산출 없이 조회·조립
  · risk_results 1건(재산출 없음) + 최근 28일 사용량·재고·활성 공고·다음 예정 입고·같은
    대체군 후보를 RiskEvidence(closed-world 근거 패키지)로 결선한다. evidence_refs가
    "본문이 인용할 수 있는 근거 ID 전집합"이다.
  ▼
generate_risk_explanation()         medsupply/llm/explanation.py
  · LLM 호출(complete_json) → RiskExplanation(cause_summary·actions·evidence_refs·history_note)
  · 사후(LLM 미관여): verify_explanation_grounding()이 근거 밖 인용을 5종 결정적으로
    대조해 hallucination_flags를 만든다(§6). 위반이어도 예외를 던지지 않고 그대로 반환·
    영속화한다(추출의 '확인 필요' 강등과 달리 결과를 낮추지 않는다).
  ▼
save_explanation()                  medsupply/data/writer.py — llm_explanations INSERT OR REPLACE
  ▼
앱 표시                              medsupply/views/review.py (_explanation_panel_html)
  · hallucination_flags가 비어 있지 않으면 "사후 대조 경고 N건 — 근거 밖 인용이 있을 수
    있습니다: …" 부분 신호 배지를 본문 위에 노출한다(_explanation_badge_html, 앞 2건만
    요약). flags가 비면 배지 없이 설명만 표시한다. 저장분이 아예 없으면(키 미설정 등)
    "AI 원인 설명이 아직 생성되지 않았습니다" + warm_cache 안내로 대체한다.
```

## 3. 캐시 키 구성

`build_cache_key(task, prompt_version, model, schema, payload)`(`medsupply/llm/cache.py`):

```
key = sha256(f"{task}|{prompt_version}|{model}|{schema.__name__}|{canonical(payload)}")
```

`canonical(payload)`는 payload를 재귀 순회해 휘발 필드(`run_id`·`generated_at`·`trace_id`)를
어느 깊이에서든 제거한 뒤 `sort_keys=True`로 직렬화한 문자열이다 — 같은 논리적 입력이면
필드 순서나 실행마다 달라지는 run_id와 무관하게 항상 같은 키가 나온다. 캐시는 메인
DB와 분리된 `data/llm_cache.db`(단일 테이블 `llm_cache`, PK=`key`, 잠금 분리 목적)에
저장한다.

## 4. 오프라인 모드(`LLM_MODE=offline`)

`complete_json()`은 `cache_key`가 주어지면 **모드와 무관하게 캐시 조회를 먼저** 시도한다 —
히트하면 online/offline 어느 쪽이든 그 값을 즉시 반환한다(`cache_hit=True`). 미스 상태에서만
모드를 본다: `online`이면 실제 공급자(Anthropic 우선·OpenAI 폴백, `LLM_PROVIDER`)를 호출하고,
`offline`이면 호출하지 않고 `LLMOfflineError`를 던진다(`cache_key`가 아예 없는 호출은 offline에서
즉시 같은 예외). 즉 오프라인 시연은 **"워밍된 캐시가 있는 입력만"** 재생할 수 있다 —
`scripts/warm_cache.py`(`medsupply.llm.warm.warm_cache`)가 공고 추출·위험 설명 생성물을
미리 캐시·DB에 채우는 선워밍을 맡는다.

## 5. tracing 계약(Langfuse, 마스터 플랜 결정 35)

`medsupply/llm/tracing.py`가 유일한 구현이다. `init_tracing()`은 `LANGFUSE_PUBLIC_KEY`·
`LANGFUSE_SECRET_KEY`·`LANGFUSE_HOST` **세 변수가 전부 설정되고** langfuse SDK import까지
성공해야 활성이다 — 그 외(미설정·부분 설정·SDK 미설치)는 전부 no-op이며 임포트 실패로
저장소가 깨지지 않는다(langfuse는 선택 의존성, requirements에 없음). `record_metadata(result)`가
duck-typing으로 뽑는 관측 메타데이터 계약 5종: `prompt_version`·`provider`·`cache_hit`·
`usage`·`hallucination_flags`(결과 객체에 없는 필드는 생략). `@observed(task)` 데코레이터는
`extract_notice`("notice_extract")·`generate_risk_explanation`("risk_explain") **두 곳에만**
걸려 있다 — `complete_json`에는 걸지 않는다(상위 함수 1곳만 계측해 이중 계측을 막는다).

## 6. role-blind 한계 — verify_explanation_grounding

`verify_explanation_grounding()`(`medsupply/llm/grounding.py`)은 본문(cause_summary + 각
action.description)을 근거(`RiskEvidence`) 전체와 **고정 순서** 5종으로 대조한다:
`unknown_ref`(근거 밖 ID 인용) → `empty_refs`(인용 누락) → `unsupported_date`(근거 밖 날짜) →
`unsupported_number`(근거 밖 수치) → `phantom_notice`(활성 공고가 없는데 '공고' 언급). 위반이
있어도 예외를 던지지 않고 `hallucination_flags` 리스트로 결과에 부착될 뿐이다.

**구조적 한계**: 이 대조기는 "그 값이 evidence 어딘가에 존재하는가"만 본다 — 본문이 그
값에 부여한 **역할**이 evidence 안에서의 역할과 같은지는 확인하지 않는다(role-blind). 예:
evidence의 다음 입고 예정 수량이 200일 때 본문이 "현재 재고 200개"라고 썼다면, 200이라는
숫자 자체는 evidence 안에 있으므로 `unsupported_number`로 잡히지 않는다 — 이런 교차 인용
(cross-citation) 오류는 5종 플래그 중 어떤 것도 방어하지 않는다. 그래서 앱의 배지는
"탐지된 경고"일 뿐이며, 배지가 없다고 해서 본문이 전부 정확하다는 뜻은 아니다.
