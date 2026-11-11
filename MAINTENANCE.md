# 유지보수 안내

이 저장소를 고치거나 배포할 때 볼 문서다. 쓰는 법은 [README.md](README.md), 배포는 [deploy/README.md](deploy/README.md)에 있다.

## 범위

이 저장소가 하는 일은 **프롬프트를 만드는 것까지**다. 아래는 넣지 않는다. 필요하면 문서에 한두 줄로 언급만 한다.

- 이미지·영상 생성(ComfyUI 워크플로, 생성 스크립트, 생성물, 생성 결과 비교)
- LLM 서버를 띄우고 운영하는 것. 주소(`H3_BASE_URL`)만 받는다
- 웹 서버 앞의 HTTPS·인증(리버스 프록시, 터널)

프롬프트가 어디에 들어가는지(어느 노드, `<Picture N>`의 연결 순서, `length`)는 프롬프트의 쓰임이라 안내한다. 그 너머(체크포인트, 샘플러, LoRA 등 생성 설정)는 적지 않는다.

## 저장소 구성

| 경로 | 무엇 | 고칠 때 |
|---|---|---|
| `h3_prompt.py` | 파이프라인과 CLI. 표준 라이브러리만 쓴다 | `tests/test_offline.py`, 실제 LLM으로 모드별 한 번 |
| `prompts/` | LLM 시스템 프롬프트. 실행할 때마다 읽는다 | 아래 "프롬프트를 고칠 때" |
| `server.py` | 웹 서버(FastAPI). API와 정적 파일 | `tests/test_server.py` |
| `web/index.html`, `web/css/app.css`, `web/js/app.js` | 화면. 빌드 단계 없음 | 아래 "화면을 고칠 때" |
| `web/design-tokens/` | 화면이 쓰는 디자인 토큰과 기본 부품 | 아래 "디자인 토큰" |
| `deploy/` | 웹 서버의 Dockerfile과 Compose 파일 | 아래 "배포 파일을 고칠 때" |
| `tests/test_offline.py`, `tests/test_server.py` | LLM 없이 도는 테스트 | |
| `examples/*.prompt.txt` | 모드별 출력 예 (T2V, I2V, Ref2VA, Ref2VA + 첫 프레임) | 출력 형식이 바뀌면 새로 뽑아 바꾼다 |
| `pyproject.toml`, `uv.lock`, `.python-version` | 웹 서버 의존성 | 아래 "의존성" |

## 개발 환경

```bash
uv sync
```

```bash
uv run tests/test_offline.py
```

```bash
uv run tests/test_server.py
```

둘 다 LLM 서버 없이 돈다. `test_server.py`는 LLM을 가짜로 바꾸고, 캐시를 임시 폴더로 돌려서 프로젝트의 `.cache`를 건드리지 않는다. 끝에 각각 `offline tests: OK`, `server tests: OK`가 나오면 통과다.

실제 LLM으로 확인할 때는 LLM 서버를 띄우고 CLI나 웹 서버를 그대로 쓴다. 단계별 출력은 `-v`, 계획·브리프·초안은 `--debug-json out.json`으로 본다.

## 고칠 때 지킬 것

### 공통

- **가짜 LLM 테스트만으로 끝내지 않는다.** 프롬프트나 계획·검사 로직을 바꾸면 실제 LLM으로 해당 모드를 돌려 본다. 모델 출력은 매번 달라지므로 한 번이 아니라 몇 번 돌리고 횟수를 적는다(예: "3회 중 3회").
- **CLI 동작을 바꾸지 않는 변경은 전후를 비교한다.** 인자 처리처럼 결과가 정해지는 부분은 같은 입력의 결과를 전후로 떠서 비교한다.
- **확인하지 않은 것은 확인하지 않았다고 적는다.** 아래 "검증 기록"의 형식을 따른다.

### 코어 (`h3_prompt.py`)

- 표준 라이브러리만 쓴다. CLI는 `uv run --no-project h3_prompt.py`로 설치 없이 돌아야 한다.
- LLM 호출은 어떤 경우에도 4회를 넘지 않는다(`--max-calls`의 상한).
- 숫자와 글자 그대로 옮기는 일은 코드가 한다: 컷 시각, 화자 ID, 대사 삽입(`{{D1}}` 토큰), 참조 라벨, `subject_definitions`·`retention_analysis`. LLM에게 다시 맡기지 않는다.
- 인자 정의는 `build_parser`, 검사와 파생 값은 `finish_options`에 있다. 웹 서버가 같은 함수를 쓰므로 옵션을 더하면 `server.py`의 `build_options`와 화면도 같이 본다.
- 이미지 읽기 캐시의 키는 이미지 내용, 모드, 참조 이름, 이미지 읽기 시스템 프롬프트다(`_cache_file`). 캐시 파일의 모양을 바꾸면 웹의 고치기 기능(`editable_look`, `checked_look`)도 맞춘다.

### 프롬프트를 고칠 때 (`prompts/`)

- 고치면 다음 실행부터 바로 쓰인다. 이미지 읽기 프롬프트(`look_*.system.md`)를 고치면 캐시 키가 바뀌어 모든 이미지를 다시 읽는다.
- 예시가 규칙보다 세다. 예시의 특정 모양(예: "Subject 2는 장소")을 모델이 그대로 따라 하는지 본다.
- 프롬프트로 안 고쳐지는 버릇은 코드로 막는다(예: 음악 설명의 분위기 단어는 `strip_mood`가 지운다). 검사(`lint_draft`)에 항목을 더하면 `tests/test_offline.py`에도 더한다.
- 고친 뒤: 오프라인 테스트, 그리고 실제 LLM으로 관련 모드를 여러 번. 수정 호출(`repair`)이 전보다 자주 붙는지 호출 수를 본다.

### 서버를 고칠 때 (`server.py`)

지켜야 하는 것(README "서버가 지키는 것")과 그것을 확인하는 테스트:

| 지킬 것 | 어디서 |
|---|---|
| LLM을 동시에 `H3_CONCURRENCY`개만 쓴다 | `_gate`. 테스트 없음. 요청 둘을 동시에 보내 `queued` 이벤트를 본다 |
| 올린 이미지는 요청이 끝나면 지운다 | `run_job`의 `finally`. `test_server.py` 끝의 임시 폴더 검사 |
| 응답에 서버 경로·LLM 주소가 없다 | `public_look`, `public_message`. `test_server.py` |
| 입력은 스트림을 열기 전에 검사한다 | `build_options`, `save_images`. `test_server.py`의 `bad` 목록 |
| 이미지 형식은 파일 머리로 본다 | `image_ext` |
| 읽기 결과 고치기는 이미 있는 캐시 항목만, 검사를 통과한 값만 | `save_look`, `checked_look`. `test_server.py`의 `bad_looks` |
| 보안 헤더(CSP 등) | `SecurityHeaders`. `test_server.py` 첫 부분 |

새 API를 더하면 README의 "API"와 `test_server.py`에 같이 더한다.

### 화면을 고칠 때 (`web/`)

- **인라인 스크립트와 `style` 속성을 쓰지 않는다.** CSP(`default-src 'self'`)가 막는다. 스크립트는 파일로, 스타일은 클래스로 둔다.
- **사용자·모델이 쓴 글은 `textContent`로 넣는다.** 프롬프트에 `<Picture 1>` 같은 꺾쇠가 그대로 들어 있다. `innerHTML`을 쓰지 않는다.
- **주소는 상대 경로로 쓴다**(`api/generate`). 프록시 뒤 하위 경로에서도 돌아야 한다.
- **색·간격·글자는 토큰만 쓴다.** `app.css`에는 `web/design-tokens/tokens.css`의 2층(역할)·3층(컴포넌트) 변수만 쓰고 날 색(`#hex`, `rgb()`)과 날 시간 값을 쓰지 않는다. 버튼·입력창·패널·칩·배지는 `ui.css`의 클래스를 쓴다. 이 화면에만 있는 결정은 `app.css` 맨 위 `:root` 블록에 둔다.
- **컴포넌트에 바깥 여백(margin)을 주지 않는다.** 간격은 부모의 `gap`으로 정한다.
- 고친 뒤: 다크·라이트 테마, 좁은 화면(375px), 그리고 실제 서버에 붙여 세 모드를 한 번씩 본다.

### 디자인 토큰 (`web/design-tokens/`)

이 화면이 쓰는 3층 디자인 토큰과 기본 부품이다.

| 파일 | 무엇 |
|---|---|
| `tokens.css` | 토큰 전부. 1층(원시값: 색·간격·글자 스케일) → 2층(역할: `--color-bg-surface`, `--gap-md`, `--font-body`) → 3층(컴포넌트: `--button-padding-y`, `--control-height`) 순서로 들어 있다 |
| `base.css` | `body`·링크·포커스 기본값 |
| `ui.css` | 3층 토큰을 쓰는 클래스: `.btn`, `.field`, `.input`, `.panel`, `.chip`, `.badge`, `.progress`, `.code-block` |
| `theme.js` | 테마를 기억하고 첫 화면 전에 적용한다. `<head>`에서 CSS보다 먼저 일반 스크립트로 부른다 |

- 화면 하나만의 결정은 이 폴더가 아니라 `app.css`의 `:root` 블록에 둔다. 토큰 값을 바꿔야 하면 같은 블록에서 덮어쓰는 쪽이 먼저다. 이 폴더의 값을 고치면 버튼·입력창 등 전체가 같이 바뀐다.
- 날 값(`#hex`, `rgb()`, `ms`)은 `tokens.css`의 1층에만 둔다. 2·3층과 `ui.css`, `app.css`는 이름만 참조한다.
- 테마는 `data-theme="light|dark"`, 밀도는 `data-density="compact|comfortable"` 속성으로 바꾼다. 색은 `light-dark()` 한 줄에 두 테마가 같이 정의되어 있다.
- 토큰을 고치면 다크·라이트 두 테마에서 화면을 다시 본다. 글자는 배경과 4.5:1, 컨트롤 테두리와 포커스 표시는 3:1 이상이어야 한다.

### 배포 파일을 고칠 때 (`deploy/`)

- **범위는 웹 서버 하나다.** LLM 서버를 띄우는 구성과 HTTPS·인증을 거는 앞단(리버스 프록시, 터널)은 넣지 않는다. LLM은 주소(`H3_BASE_URL`)로만 받고, 앞단은 맞춰야 하는 값만 `deploy/README.md`에 적는다.
- `compose.yml`은 `H3_BASE_URL`이 없으면 띄우지 않는다(`${H3_BASE_URL:?...}`). 기본 주소를 다시 넣지 않는다. 컨테이너에서 닿는 주소는 환경마다 달라서, 틀린 기본값보다 빠진 값을 알려 주는 쪽이 낫다.
- 이미지에 들어가는 것은 `h3_prompt.py`, `server.py`, `prompts/`, `web/`, 의존성뿐이다. 최상위에 파일을 더하면 `deploy/Dockerfile`의 `COPY`에도 더한다.
- `Dockerfile`은 uv 버전(`ghcr.io/astral-sh/uv:<버전>`)과 파이썬 버전을 고정해 둔다. 올릴 때는 이미지를 다시 만들어 띄워 본다.
- compose 파일을 고치면 `docker compose -f deploy/compose.yml config`로 문법을 보고 실제로 띄워 본다.
- 환경 변수를 더하면 `server.py` 맨 위 설명, `.env.example`, `deploy/.env.example`, compose 파일의 `environment`, README의 표를 같이 고친다.

### 의존성

```bash
uv lock --upgrade
```

```bash
uv sync
```

올린 뒤 두 테스트를 돌리고 `uv.lock`을 같이 커밋한다. 웹 서버 의존성은 `fastapi`, `uvicorn`, `python-multipart` 셋이고, 테스트용으로 `httpx2`(FastAPI `TestClient`가 쓴다)가 있다.

## 저장소에 넣지 않는 것

`.gitignore`가 막는다. 새 파일을 더할 때 같은 기준으로 본다.

| 무엇 | 이유 |
|---|---|
| `.cache/` | 이미지 읽기 캐시. 올린 이미지의 로컬 경로가 들어 있다 |
| `.env`, `deploy/.env` | 환경별 설정. LLM 서버 주소와 API 키가 들어간다. 예시 파일(`.env.example`)만 넣는다 |
| `local/` | 범위 밖의 개인 작업물을 두는 곳. 이미지·영상 생성 실험(스크립트, 생성물, 기록)이 여기에 있다 |
| `.venv/`, `__pycache__/` | 만들어지는 것 |
| `.claude/`, `CLAUDE.md` 등 | AI 코딩 도구의 로컬 설정 |

커밋하기 전에 확인할 것:

- 내 PC의 경로(드라이브 문자, 사용자 폴더), 계정 이름, 내부 주소가 문서·주석·테스트 데이터에 없는가.
- API 키, 비밀번호, 실제 서버 주소가 없는가. 예시에는 자리 표시(`example.com`, `host.docker.internal`)만 쓴다.
- `git status`에 생성한 이미지·영상·캐시가 올라와 있지 않은가.

## 배포 전후 점검

배포 전:

- [ ] 두 테스트 통과
- [ ] LLM 서버가 떠 있고, `deploy/.env`의 `H3_BASE_URL`이 컨테이너에서 닿는 주소인가 (컨테이너의 `127.0.0.1`은 컨테이너 자신이다)
- [ ] 바깥에 내는 경우 앞단(프록시·터널)에 HTTPS와 인증이 걸려 있는가 (앱에는 로그인이 없다. 앞단 구성은 이 저장소 밖이다)
- [ ] 앞단의 요청 본문 한도가 `H3_MAX_UPLOAD_MB` × 9보다 큰가

배포 후:

- [ ] `/api/health`의 `llm.reachable`이 `true`인가. 이미지를 쓸 거면 `llm.vision`도 `true`인가
- [ ] 화면에서 T2V 한 번, 이미지를 쓰는 모드 한 번을 생성해 본다. 단계 표시가 끝에 한꺼번에가 아니라 하나씩 바뀌는가(앞단이 응답을 모으면 한꺼번에 나온다)
- [ ] 바깥에 낸 경우, 인증 없이 열었을 때 막히는가
- [ ] 응답 헤더에 `Content-Security-Policy`가 있는가, `/design-tokens/theme.js`가 200인가

## 검증 기록

모델 출력이 달라지는 부분이라 "무엇을 몇 번 돌려 무엇을 봤는지"를 남긴다. 측정 환경: llama.cpp 서버, Gemma 4 26B A4B (Q4_K_M), GPU 16GB.

### 파이프라인 (2026-11-11)

- 오프라인 테스트: 컷 시각 계산, 토큰 치환, 흠 있는 초안 보정, 검사 항목, I2V 첫 프레임 검사, Ref2VA 라벨 치환과 6섹션 조립.
- T2V: 화풍 기본값을 anime로 바꾼 뒤 6건 재실행(`--single` 1건 포함). 파이프라인 모드는 모두 2회 호출로 통과. `--style live`도 동작.
- I2V: 이미지 3장으로 요청 6종을 10여 회 실행. 처음 3회, 캐시된 뒤 2회 호출. 처음 28초(mmproj CPU), 캐시된 뒤 9~12초. `--i2v`(글 설명)와 `--single --image`도 동작.
- Ref2VA: 요청 8종을 약 20회 실행 (참조 1~4장, 2인 대화, 화풍 참조, 참조 없는 인물 추가, 요청문에 없는 참조, 15초 4샷, `--style live`). 처음 3회, 캐시된 뒤 2회 호출. 참조 3장 기준 처음 65초(mmproj CPU), 캐시된 뒤 13~15초. 참조 9장도 컨텍스트 8192에 들어갔다.
- 수정 경로: 작성 단계 출력을 일부러 망가뜨려 넣었더니 I2V(`<Picture 1>`과 화자 ID 삭제), Ref2VA(라벨 삭제, 요약에 한국어, 토큰 대신 영어 대사) 모두 수정 1회로 복구.
- 구 경로와 비교: 같은 요청·같은 이미지로 `--no-look`(계획에 이미지 직접 첨부)과 새 경로를 돌려 둘 다 형식 검사를 통과하는 것을 확인.
- 만든 프롬프트로 영상을 생성해 보는 일은 이 저장소의 범위 밖이라 기록을 여기 두지 않는다. 그 실험에서 프롬프트 쪽에 반영한 결론은 하나다: 첫 프레임 이미지 안의 인물은 외형을 글로 적지 않는다(잘못 읽은 외형이 영상에서 이미지를 이겼다).

### 웹 서버와 화면 (2026-11-12)

- 코어 변경이 CLI를 바꾸지 않았는지: 인자 처리를 `build_parser`·`finish_options`로 나누기 전후에 인자 조합 19개(정상 11, 오류 8)의 결과를 떠서 비교했고 모두 같았다. 실제 LLM으로 CLI 1회 실행 확인.
- 서버 테스트(가짜 LLM): 세 모드의 단계 순서, 캐시 사용과 다시 읽기, 빠른 모드, 잘못된 입력 19가지의 상태 코드와 문구, LLM이 꺼졌을 때의 오류 이벤트, 응답에 경로·주소가 없는 것, 임시 폴더가 남지 않는 것. 읽기 결과 고치기: 고친 값이 다음 생성에 쓰이는 것, 잘못된 값 16가지 거절, 거절된 값이 남지 않는 것, 다시 읽기가 고친 내용을 덮는 것.
- 브라우저에서 실제 LLM으로: T2V 6초 LLM 2회 19.3초, I2V 6초 3회 20.7초(이미지 다시 읽기), 참조 2장(첫 프레임 + 인물) 8초 4회 33.3초. 단계 표시가 순서대로 바뀌는 것, 다크·라이트 테마, 폭 375px(가로 넘침 0), 입력 오류와 LLM이 꺼졌을 때의 표시.
- 읽기 결과 고치기(브라우저, 실제 LLM): 첫 프레임 + 인물 참조에서 `short black hair`를 `long black hair`로, 요약과 `green eyes`를 고친 뒤 버튼을 눌러 LLM 2회 18.3초로 다시 생성됐고, 고친 요약과 `emerald green eyes`가 `subject_definitions`에 그대로 들어갔다. 한글을 넣으면 저장하지 않고 이유를 보여 준다.
- 대기열: 요청 둘을 1초 간격으로 보내면 둘째가 `queued` → `ping` → `stage` → `done` 순서로 받는다.

### 배포 (2026-11-12, Docker Desktop, Windows)

- `compose.yml`: 이미지 빌드(293MB), 실행, `host.docker.internal`로 PC의 LLM 서버(`127.0.0.1:8080`)에 닿는 것, 실제 이미지로 I2V(3회 23.4초)와 참조 2장(3회 27.5초) 생성, 캐시가 볼륨에 쌓이는 것, 임시 폴더가 지워지는 것, 루트가 아닌 계정으로 도는 것. `deploy/.env`를 자동으로 읽는 것.
- LLM 주소를 받는 방식: `H3_BASE_URL` 없이는 Compose가 띄우지 않고 빠진 값을 알려 준다. 다른 포트의 가짜 서버 주소를 주면 `/api/health`가 `reachable: true`로, 닿지 않는 주소를 주면 `false`로 답한다.
- 리버스 프록시 뒤(Caddy 2를 앞에 두고 Basic 인증을 걸어 한 번 시험했다. 그 구성 파일은 범위 밖이라 저장소에 두지 않는다): 프록시를 거쳐도 보안 헤더가 붙는 것, T2V 스트림의 이벤트가 0초·9초·13초에 따로 도착하는 것(모아서 오지 않는다), 8MB 이미지 업로드가 통과하고 12MB는 413인 것.

### 확인하지 않은 것

- 리눅스 서버의 Docker, 다른 서버에 있는 LLM을 주소로 가리키는 것(같은 PC의 LLM만 써 봤다).
- Caddy가 아닌 프록시나 터널 뒤에서의 스트림 동작, 도메인과 HTTPS.
- llama.cpp + Gemma 4가 아닌 LLM 서버·모델.
- 여러 사람이 동시에 쓰는 상황, Safari·Firefox, 실제 파일 끌어다 놓기와 붙여넣기(화면 테스트는 파일 입력에 스크립트로 넣었다).
- 만든 프롬프트가 영상에서 어떻게 나오는지는 이 저장소에서 다루지 않는다.
