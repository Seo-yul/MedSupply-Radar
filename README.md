# MedSupply Radar UI 초안

기획서의 핵심 사용자 여정을 검증하기 위한 Streamlit 프로토타입입니다. 시연 데이터가 내장되어 있어 별도 DB나 API 없이 실행할 수 있습니다.

## 실행

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## 포함 화면

- 관제 대시보드와 위험 품목 우선순위
- 품목별 Risk Timeline, 위험점수 기여요인, AI 설명과 대응방안
- 공급중단 공고 구조화 및 기관 품목 매핑
- 알림센터와 대응 이력
- Langfuse LLM-as-a-Judge 평가 지표 화면

현재 버전은 UI 검증용이며 입력값은 실제 저장되지 않습니다. 다음 단계에서 FastAPI·SQLite 및 Langfuse trace/score API를 연결할 수 있습니다.