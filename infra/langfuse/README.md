# Langfuse 셀프호스트 (LLM 생성 품질 평가용)

MedSupply Radar의 LLM 생성(요약·코파일럿 응답 등) 품질을 관찰·평가하기 위한
[Langfuse](https://langfuse.com) 셀프호스트 스택이다. **완전히 선택 사항이며,
이 스택 없이도 앱과 평가 파이프라인은 정상 동작한다** — 아래 "비의존 원칙"
참고.

이미지·서비스 구성은 Langfuse 공식 셀프호스트 가이드
(https://langfuse.com/self-hosting/deployment/docker-compose)가 가리키는
[`langfuse/langfuse` 저장소의 `docker-compose.yml`](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml)
(2026-08-19 확인, Langfuse v4)을 따르되, 이 프로젝트에 필요 없는 항목을 덜어냈다.
전체 구성 근거는 `task-M08-report.md`에 기록되어 있다.

## 1. 기동

```bash
cd infra/langfuse
cp .env.example .env
# .env를 열어 changeme-* 값들을 실제 값으로 채운다(아래 "시크릿 생성" 참고).
docker compose up -d
```

첫 기동은 이미지 다운로드와 DB 마이그레이션 때문에 2~3분 정도 걸릴 수 있다.
`docker compose logs -f langfuse-web`에서 `Ready` 로그가 보이면 준비된 것이다.
이후 브라우저로 http://localhost:3000 에 접속한다.

### 포트 충돌 시

로컬에서 3000번 포트가 이미 사용 중이면 `.env`에 아래를 추가하고 다시
`docker compose up -d`를 실행한다.

```bash
LANGFUSE_WEB_PORT=3001
NEXTAUTH_URL=http://localhost:3001
```

### 시크릿 생성

`.env.example`의 `changeme-*` 값은 로컬 개발용 예시일 뿐이다. 최소한 아래
명령으로 생성한 값을 `.env`에 채워 넣는 것을 권장한다.

```bash
openssl rand -base64 32   # SALT, NEXTAUTH_SECRET
openssl rand -hex 32      # ENCRYPTION_KEY (정확히 64자 hex 필요)
```

`POSTGRES_PASSWORD`를 바꾸면 `DATABASE_URL` 안의 비밀번호도 동일하게 맞춰야
한다(두 변수가 값을 공유하지 않고 각자 설정되는 상류 compose 설계를 그대로
따른다).

## 2. 초기 설정 (Langfuse ↔ 앱 연결)

1. http://localhost:3000 (또는 3001) 접속 후 계정을 만든다.
2. 조직(Organization)과 프로젝트(Project)를 생성한다.
3. 프로젝트 설정(Project Settings) → API Keys에서 PUBLIC KEY / SECRET KEY를
   발급한다.
4. 저장소 루트의 `.env`(MedSupply Radar 앱 자체 설정 파일, 이 디렉터리의
   `.env`와는 다른 파일)에 아래 값을 채운다.

   ```bash
   LANGFUSE_HOST=http://localhost:3000
   LANGFUSE_PUBLIC_KEY=<발급받은 public key>
   LANGFUSE_SECRET_KEY=<발급받은 secret key>
   ```

   실제 tracing 연동(SDK 초기화, no-op 안전 훅)은 후속 태스크에서 구현된다.
   이 세 변수가 비어 있어도 앱은 오류 없이 동작해야 한다.

## 3. 중지 / 삭제

```bash
docker compose down       # 컨테이너 중지(데이터 볼륨 유지)
docker compose down -v    # 컨테이너 중지 + 데이터 볼륨까지 삭제
```

## 4. 비의존 원칙

`LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`가 설정되지
않은 경우 앱과 평가 파이프라인은 tracing 없이 정상 동작해야 한다. 품질 평가의
1차 지표 소스는 Langfuse가 아니라 로컬 `eval/results/*.json`이며, Langfuse는
어디까지나 보조적인 관찰·디버깅 도구다.

## 5. 자원 참고

이 스택은 가볍지 않다 — Postgres, ClickHouse, Redis, MinIO, langfuse-web,
langfuse-worker까지 컨테이너 6개가 함께 뜬다(Langfuse v4 표준 구성). 메모리·
디스크가 넉넉하지 않은 시연 장비에서는 기동하지 않아도 된다(1번의 비의존
원칙 참고). 완전히 정리하려면 `docker compose down -v`로 named volume까지
삭제한다.

## 이 구성에서 상류(upstream) compose 대비 생략한 항목

로컬 품질 평가 용도에 필요하지 않은 다음 항목은 뺐다(모두 상류에서도
기본값이 비어 있거나 꺼져 있는 선택 기능이라 동작에 영향 없음):
Azure Blob / OCI Object Storage 대체 스토리지 백엔드, SMTP/이메일 초대, 인앱
AI 에이전트(AWS Bedrock) 관련 변수, S3 배치 export, Redis TLS, 조직/프로젝트
자동 부트스트랩(`LANGFUSE_INIT_*`). 필요해지면 상류 `docker-compose.yml`에서
해당 블록을 그대로 가져와 추가하면 된다.
