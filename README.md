# h3-prompt-helper

한국어로 쓴 영상 아이디어를 [MiniMax H3](https://huggingface.co/MiniMaxAI/MiniMax-H3)용 영어 프롬프트로 바꾸는 로컬 LLM 파이프라인입니다. CLI와 웹 화면 두 가지로 씁니다.

H3 호스팅 서비스는 느슨한 요청을 H3-Context-IR이 구조화된 프롬프트로 다시 써 줍니다. 로컬에서 H3-Base만 돌리면 그 단계가 없어서 직접 써야 하는데, 이 도구가 그 자리를 대신합니다. LLM은 이미지를 읽고 내용을 정하고 문장을 쓰는 일만 하고, 컷 시각·화자 ID·대사 삽입·참조 라벨·형식 검사는 코드가 합니다.

**이 도구가 하는 일은 프롬프트를 만드는 것까지입니다.** 이미지와 영상 생성, LLM 서버 운영, 웹 서버 앞의 HTTPS·인증은 범위 밖입니다. 나온 프롬프트는 ComfyUI의 MiniMax H3 노드 같은 곳에 직접 붙여 넣습니다.

주 용도가 anime 영상이라 화풍 기본값은 `2D-animated, Japanese anime style`입니다. 다른 화풍은 `--style`로 바꿉니다.

## 필요한 것

- **Python 3.10 이상과 [uv](https://docs.astral.sh/uv/).** CLI(`h3_prompt.py`)는 표준 라이브러리만 씁니다. 웹 서버는 FastAPI·uvicorn·python-multipart를 쓰고 `uv run`이 알아서 설치합니다.
- **이미 떠 있는 OpenAI 호환 LLM 서버의 주소** (`/v1/chat/completions`). 이 도구는 LLM 서버를 띄우지 않고 주소만 받습니다. 이미지를 쓰는 모드는 이미지 입력을 받는 모델이어야 합니다.
- (선택) Docker. 웹 서버를 컨테이너로 띄울 때.

시험한 구성은 [llama.cpp](https://github.com/ggml-org/llama.cpp) 서버 + Gemma 4 26B A4B (Q4_K_M GGUF, 컨텍스트 8192, mmproj로 이미지 입력) 하나뿐입니다. 다른 서버와 모델에서는 프롬프트(`prompts/`)를 손봐야 할 수 있습니다.

## 빨리 써 보기

LLM 서버 주소를 환경 변수 `H3_BASE_URL`이나 옵션 `--base-url`로 줍니다. `http://127.0.0.1:8080/v1`이면 생략해도 됩니다(서버가 갖춰야 할 것은 아래 "LLM 서버").

```bash
export H3_BASE_URL=http://127.0.0.1:8080/v1
```

CLI로 한 번 만듭니다.

```bash
uv run h3_prompt.py "비 오는 밤 골목에서 길고양이가 편의점 앞 우산 밑으로 들어와 몸을 턴다" -d 6
```

웹 화면으로 쓰려면 서버를 띄우고 `http://127.0.0.1:8000`을 엽니다.

```bash
uv run server.py
```

## 모드

| 모드 | 입력 | 출력 형식 | 프롬프트를 넣는 곳 (ComfyUI) |
|---|---|---|---|
| T2V | 요청문 | 3필드 | MiniMax H3 Image to Video (이미지 없이) |
| I2V | 요청문 + 첫 프레임 이미지 (`--image`) | 정렬 문장 + 3필드 | MiniMax H3 Image to Video (그 이미지를 `first_frame`에) |
| Ref2VA | 요청문 + 참조 이미지 1~9장 (`--ref`) | 6섹션 | MiniMax H3 Reference to Video (참조를 `ref_image_N`에 순서대로) |
| Ref2VA를 I2V처럼 | 요청문 + 첫 프레임 이미지 (`--ref 첫프레임=파일`), 다른 참조와 섞을 수 있음 | 6섹션 (`keyframe completion`) | 위와 같음 |

이미지는 LLM이 읽기만 합니다. 프롬프트 안의 `<Picture N>`은 영상을 만들 때 같은 이미지를 같은 순서로 넣는다는 뜻이고, 그 연결은 쓰는 사람이 합니다.

```bash
uv run h3_prompt.py "소녀가 일어나서 빨간 우산을 펴고 빗속으로 걸어 나간다" --image first.png -d 6
```

```bash
uv run h3_prompt.py "유나가 신사 계단을 뛰어 올라가고 삐삐가 뒤따라 날아온다" --ref 유나=girl.png --ref 배경=shrine.png --ref 삐삐=robot.png -d 8
```

프롬프트는 표준출력으로, 진행 정보와 경고는 표준오류로 나옵니다. `-o out.txt`로 파일에 저장할 수 있습니다.

대사는 요청문에 따옴표로 쓰면 그대로 들어갑니다: `여자가 "나 다음 달에 이사 가." 라고 말한다`.
화면에 보일 글자도 따옴표로 씁니다: `표지판에 "정상 2km" 라고 적혀 있다`.

### Ref2VA에서 참조를 주는 법

- `--ref 이름=파일`을 여러 번 씁니다. **쓴 순서가 `<Picture N>` 번호**이고, ComfyUI 노드의 `ref_image_1`, `ref_image_2`…에 같은 순서로 연결해야 합니다.
- 이름은 요청문에서 그 대상을 부르는 말입니다 (`유나`, `삐삐`). 요청문에 그 이름을 쓰면 계획 단계가 어느 참조인지 압니다. 이름 없이 `--ref 파일`만 줘도 되고, 그때는 "은발 소녀", "1번"처럼 불러도 됩니다.
- 이름에 `배경`·`장소`가 들어가면 장소 참조, `화풍`·`그림체`·`스타일`이 들어가면 화풍 참조로 다룹니다. 그 외에는 이미지를 보고 인물·생물·사물·장소 중 하나로 정합니다.
- 참조 영상과 참조 오디오(`<Video N>`, `<Audio N>`), 첫 프레임이 아닌 프레임에 고정하는 방식(중간 키프레임, 끝 프레임)은 지원하지 않습니다.

### Ref2VA를 I2V처럼 쓰는 법 (첫 프레임 참조)

```bash
uv run h3_prompt.py "소녀가 일어나서 빨간 우산을 펴고 빗속으로 걸어 나간다" --ref 첫프레임=first.png -d 6
```

```bash
uv run h3_prompt.py "삐삐가 왼쪽에서 떠 와서 소녀 앞에 멈추고, 소녀가 \"왔구나.\" 라고 말한다" --ref 첫프레임=first.png --ref 삐삐=robot.png -d 8
```

- 이름에 `첫프레임`(또는 `first_frame`)이 들어간 `--ref`는 영상이 시작하는 그림으로 다룹니다. 하나만 줄 수 있습니다.
- 공식 multiframe 템플릿과 같은 형식으로 나옵니다: `<Picture 1> is the first frame of [Shot 1], showing ...`, 과제 유형 `[keyframe completion + reference generation]`, 보존 분석 `<Picture 1> ([Shot 1] first frame): fully_preserved`, 본문 `[Shot 1] The shot begins from <Picture 1>.` 이 문장들은 코드가 넣습니다.
- 그림 안의 인물·생물(최대 3)은 `<Subject N>` 라벨을 받아서 컷이 바뀐 뒤에도 같은 인물로 이어집니다. 우산·표지판 같은 소품은 라벨 없이 서술합니다.
- 그림 안 인물의 머리·얼굴·옷은 본문에 적지 않습니다(`<Subject 1>, the girl,`까지만). LLM이 이미지를 잘못 읽었을 때(예: 긴 머리를 짧다고) 그 글이 영상에서 이미지를 이기기 때문입니다.
- 프롬프트는 그 이미지가 영상의 0번 프레임이라고 전제하고 쓰입니다. 영상을 만들 때 첫 프레임 이미지도 `--ref` 순서대로 참조로 넣고, 같은 이미지를 0번 프레임에 고정해야 그 그림에서 시작합니다(ComfyUI에서는 Add Guide for MiniMax H3의 `frame_idx` 0).
- `--image`(I2V)와의 차이: 이쪽은 인물이 참조 라벨을 받아 컷이 바뀌어도 이어지고, 다른 참조(인물·장소·화풍)를 같이 쓸 수 있습니다. 이미지 한 장에서 시작만 하면 될 때는 `--image`가 간단합니다.

## LLM 서버

LLM 서버를 띄우고 운영하는 일은 이 도구의 범위 밖입니다. 떠 있는 서버의 주소와 모델 이름을 환경 변수나 옵션으로 줍니다.

| 환경 변수 | 옵션 | 기본값 | 뜻 |
|---|---|---|---|
| `H3_BASE_URL` | `--base-url` | `http://127.0.0.1:8080/v1` | OpenAI 호환 서버의 `/v1` 주소 |
| `H3_MODEL` | `--model` | `gemma4` | 요청에 넣는 모델 이름. llama.cpp는 무시하고, 여러 모델을 내는 서버는 이 이름으로 고릅니다 |
| `H3_API_KEY` | `--api-key` | (없음) | 서버가 키를 요구할 때 |

파이프라인이 서버에 기대하는 것:

- **thinking(추론 출력)이 꺼져 있을 것.** 요청마다 `chat_template_kwargs: {"enable_thinking": false}`를 보냅니다. llama.cpp는 `--jinja`로 띄워야 이 값이 적용됩니다. 켜진 채로는 추론에만 1500토큰 넘게 써서 본문이 잘렸습니다(컨텍스트 8192 실측). 이 필드를 모르는 서버에서는 서버 쪽에서 꺼 둡니다.
- **JSON 스키마 응답**(`response_format: json_schema`). 계획과 이미지 읽기 단계가 씁니다. 서버가 400으로 거절하면 표준 필드만으로 한 번 더 보내고(이미지가 든 요청은 제외), 그때는 프롬프트의 지시만으로 JSON을 받습니다.
- **컨텍스트 8192 이상.** 참조 이미지 9장과 계획·브리프가 들어가는 크기입니다(이미지 한 장이 약 460~500토큰).

### 이미지를 쓰는 모드 (I2V, Ref2VA)

서버의 모델이 이미지 입력을 받아야 합니다(llama.cpp라면 mmproj를 같이 올린 서버). 이미지 입력이 없는 서버에서는 T2V와 `--i2v`(첫 프레임을 글로 설명)만 쓸 수 있고, 이미지를 쓰는 모드는 서버 오류로 끝납니다.

참고 실측 (2026-11-11. llama.cpp, Gemma 4 26B A4B Q4_K_M, GPU 16GB. 다른 환경에서는 다릅니다): 이미지 읽기 1장 8초, 4장 12초, 9장 24초. 글 생성 61~65 tok/s. 이미지 읽기 결과는 캐시되므로 같은 이미지는 다시 읽지 않습니다.

## 옵션

| 옵션 | 뜻 |
|---|---|
| `-d`, `--duration` | 영상 길이(초). 4~15, 기본 5. ComfyUI의 `length`와 맞춥니다 (24fps) |
| `--max-shots` | 샷 수 상한. 기본은 5초 이하 1, 8초 이하 2, 12초 이하 3, 그 위 4 |
| `--style` | 화풍. `anime`(기본) · `live` · `3d` · `auto`(모델이 정함) · 직접 쓴 영어 문구. 이미지를 쓰는 모드에서 생략하면 이미지에서 읽은 화풍을 따릅니다 |
| `--image 파일` | I2V 첫 프레임 이미지 |
| `--ref [이름=]파일` | Ref2VA 참조 이미지. 최대 9개 |
| `--i2v` | I2V인데 이미지를 LLM에 보여 주지 않을 때. 요청문에 첫 프레임을 글로 설명합니다 (이미지 입력 불필요) |
| `--look-only` | 이미지를 읽은 결과(JSON)만 출력합니다. 모델이 이미지를 어떻게 봤는지 확인할 때 |
| `--no-cache` | 이미지 읽기 캐시를 쓰지 않고 다시 읽습니다 |
| `--no-look` | I2V에서 이미지 읽기 단계를 건너뛰고 계획 단계에 이미지를 직접 붙입니다. LLM 1회가 줄지만 캐시가 없고, 잘못 읽은 외형이 본문에 그대로 들어갑니다 |
| `--speech auto\|on\|off` | 대사 허용. `auto`(기본)는 요청문에 따옴표나 "말하다·노래·내레이션·인사" 같은 표현이 있을 때만 허용 |
| `--dialogue-lang` | 대사 내용 없이 "말한다"고만 했을 때 모델이 지어낼 대사의 언어. 기본 Korean |
| `--no-music` | 배경음악 없이 (`non_diegetic_music: N/A`) |
| `--single` | LLM 1회로 바로 생성. 빠르지만 코드 보정이 적습니다. `--ref`와는 못 씁니다 |
| `--max-calls` | LLM 호출 상한. 기본 4, 그 이상으로는 못 올립니다 |
| `-v` | 단계별 LLM 출력과 이미지 읽기 결과를 보여 줍니다 |
| `--debug-json 파일` | 이미지 읽기 결과·계획·브리프·초안·검사 결과를 저장합니다 (프롬프트를 고칠 때 봅니다) |

종료 코드: 0 정상, 1 실행 실패, 3 호출 상한 안에 못 고친 형식 문제가 남음(프롬프트는 출력됨).

CLI만 쓸 때 웹 서버 의존성을 설치하지 않으려면 `uv run --no-project h3_prompt.py ...`로 실행합니다.

## 웹으로 쓰기

같은 파이프라인을 브라우저에서 씁니다. 서버 하나가 API와 화면을 같이 냅니다.

```bash
uv run server.py
```

`http://127.0.0.1:8000`을 엽니다. LLM 서버가 닿는지, 이미지를 받는지는 화면 위쪽 배지와 이미지 칸의 안내로 알 수 있습니다.

- 화면의 옵션은 CLI 옵션과 같습니다(모드, 길이, 샷 수, 화풍, 대사, 대사 언어, 배경음악 없이, 빠른 모드, 이미지 다시 읽기). 화면에 없는 것은 `--i2v`, `--no-look`, `--look-only`, `--max-calls`, `--debug-json`입니다.
- 참조 모드에서 "첫 프레임"을 체크한 이미지는 CLI의 `--ref 첫프레임=파일`과 같게 다룹니다.
- 결과 아래에 ComfyUI 노드에 연결하는 순서와 `length` 값을 같이 보여 줍니다.
- 화면 글은 한국어입니다. 빌드 단계가 없고 주소가 모두 상대 경로라 프록시 뒤 하위 경로에 올려도 됩니다.

**이미지 읽기 결과 고치기.** 결과 맨 아래 "이미지를 이렇게 읽었습니다"를 펴면 LLM이 이미지마다 읽은 내용(화풍·구도·장소·요약·피사체, 참조는 종류·이름·특징)이 입력칸으로 나옵니다. 틀린 곳을 영어로 고치고 "고친 내용으로 다시 생성"을 누르면 서버의 캐시에 저장하고 바로 다시 생성합니다(이미지를 다시 읽지 않으므로 LLM 호출이 한 번 줍니다). CLI에서 캐시의 JSON을 직접 고치는 것과 같은 일이고 같은 파일을 씁니다.

- 고친 내용은 같은 이미지(와 같은 이름)를 올리는 다음 생성부터 계속 쓰입니다. 이 서버를 쓰는 모든 사람에게 적용됩니다.
- "이미지를 다시 읽기"를 켜고 생성하면 새로 읽은 결과가 고친 내용을 덮습니다. "고친 내용으로 다시 생성"은 이 옵션을 끄고 생성합니다.
- 영어 한 줄만 받습니다. 한글과 꺾쇠·중괄호·대괄호는 거절합니다(참조의 특징과 첫 프레임 요약은 프롬프트에 그대로 들어갑니다).
- 피사체를 더하거나 빼는 기능은 없습니다. 있는 항목의 글만 고칩니다.

### 서버 설정

환경 변수로 줍니다. 예시는 [.env.example](.env.example)에 있습니다(`server.py`는 이 파일을 읽지 않습니다).

| 환경 변수 | 기본값 | 뜻 |
|---|---|---|
| `H3_HOST`, `H3_PORT` | `127.0.0.1`, `8000` | `uv run server.py`로 띄울 때의 주소 |
| `H3_CONCURRENCY` | `1` | 동시에 돌릴 생성 수. 로컬 LLM 하나를 나눠 쓰면 1로 둡니다 |
| `H3_MAX_WAITING` | `8` | 줄을 설 수 있는 요청 수. 넘으면 503 |
| `H3_MAX_UPLOAD_MB` | `10` | 이미지 한 장의 크기 한도 |
| `H3_MAX_REQUEST_CHARS` | `2000` | 요청문 길이 한도 |
| `H3_LLM_TIMEOUT` | `300` | LLM 호출 한 번의 제한 시간(초) |
| `H3_CACHE_DIR` | `.cache/look` | 이미지 읽기 캐시 폴더 |

### 서버가 지키는 것

- **LLM은 정한 수만큼만 동시에 씁니다.** 나머지 요청은 줄을 서고, 화면에 기다리는 중이라고 나옵니다.
- **올린 이미지는 요청이 끝나면 지웁니다.** 요청마다 임시 폴더에 저장하고, 성공·실패와 상관없이 지웁니다. 남는 것은 이미지 읽기 결과(JSON)뿐입니다.
- **응답에 서버 경로와 LLM 주소를 넣지 않습니다.** 원문은 서버 로그에 남습니다. 요청문은 로그에 남기지 않습니다.
- **보안 헤더.** `Content-Security-Policy: default-src 'self'`(이미지 미리보기용 `blob:`만 추가)를 붙입니다.
- **로그인은 없습니다.** 기본은 `127.0.0.1`에만 엽니다. 바깥에 낼 때는 HTTPS와 인증을 거는 프록시나 터널을 직접 앞에 둡니다. 그 구성은 이 저장소의 범위 밖이고, 앞단이 맞춰야 하는 값만 [deploy/README.md](deploy/README.md)에 적어 두었습니다. 이미지 읽기 캐시는 모든 사용자가 같이 쓰고, 누구든 자기가 올린 이미지의 읽기 결과를 고칠 수 있습니다(같은 이미지 파일을 가진 사람끼리만 서로 영향을 줍니다).

### API

`POST /api/generate`는 `multipart/form-data`를 받습니다.

| 필드 | 값 |
|---|---|
| `request` | 한국어 요청문 (필수) |
| `mode` | `t2v`(기본) · `i2v` · `ref` |
| `duration`, `max_shots`, `style`, `speech`, `dialogue_lang` | CLI와 같습니다. 비우면 기본값 |
| `no_music`, `single`, `no_cache` | `1`이면 켭니다 |
| `images` | 이미지 파일. `i2v`는 1장, `ref`는 1~9장(보낸 순서가 `<Picture N>`). PNG·JPEG만 받고, 형식은 파일 머리로 확인합니다 |
| `names` | `ref`에서만. 이미지와 같은 길이의 JSON 문자열 목록(`["유나","배경"]`). 이름이 없으면 빈 문자열 |

입력이 잘못되면 스트림을 열기 전에 400(크기 초과는 413, 줄이 꽉 차면 503)과 `{"error": "..."}`로 답합니다. 정상이면 한 줄에 JSON 하나씩 보내는 스트림(`application/x-ndjson`)입니다.

```
{"event":"queued"}                               앞선 요청을 기다리는 중 (없을 수도 있습니다)
{"event":"stage","stage":"look","call":1}        LLM을 부르기 직전마다. stage는 look · plan · write · repair · single
{"event":"ping"}                                 10초 동안 보낼 것이 없을 때 (프록시가 연결을 끊지 않게)
{"event":"done","prompt":"...","calls":3,"seconds":20.7,"issues":[{"sev":"warn","text":"..."}],"look":{...}}
{"event":"error","message":"..."}
```

`done`의 `look`에는 화면에 보여 줄 읽기 결과(`frame` 또는 `refs`·`first_frame`)와, 고칠 수 있는 원본 목록 `edit`이 들어 있습니다. `edit`의 항목은 `{"id", "type": "frame"|"ref", "picture": "<Picture N>", "look": {...}}`입니다.

`PUT /api/look/{id}`는 그 `id`의 읽기 결과를 본문 `{"look": {...}}`로 바꿉니다. `look`의 모양은 `edit`에서 받은 것과 같습니다(`frame`: `style`, `framing`, `setting`, `caption`, `subjects[{label, kind, look, place}]` / `ref`: `kind`, `name`, `features[]`, `style`). 이미 있는 캐시 항목만 바꿀 수 있고(없으면 404), 값이 잘못되면 400과 `{"error": "..."}`로 답하고 저장하지 않습니다.

`GET /api/health`는 LLM 서버에 닿는지(`llm.reachable`), 이미지를 받는지(`llm.vision`. llama.cpp의 `/props`가 알려 줄 때만 참·거짓이고 다른 서버에서는 `null`), 줄을 선 요청 수, 입력 한도를 돌려줍니다.

## 배포

웹 서버를 컨테이너로 띄웁니다. LLM 서버 주소(`H3_BASE_URL`)를 `deploy/.env`에 적는 것이 필수입니다. 자세한 것은 [deploy/README.md](deploy/README.md)에 있습니다.

```bash
cp deploy/.env.example deploy/.env
```

```bash
docker compose -f deploy/compose.yml up -d --build
```

컨테이너에는 웹 서버만 들어 있습니다. LLM 서버를 띄우는 일과 HTTPS·인증을 앞에 두는 일은 범위 밖입니다.

## 구조

| 단계 | 누가 | 하는 일 |
|---|---|---|
| 0. 이미지 읽기 | LLM (`look_frame` / `look_refs`) | I2V·Ref2VA만. 이미지에서 화풍·구도·피사체(I2V) 또는 참조마다 종류·이름·고정 특징(Ref2VA)을 JSON으로 읽습니다. 참조가 여러 장이어도 한 번에 읽습니다. 결과는 `.cache/look/`에 저장되어 같은 이미지는 다시 읽지 않습니다 |
| 1. 계획 | LLM (`plan` / `plan_ref`) | 한국어 요청을 읽고 스타일·장소·인물·샷·카메라·대사·소리를 JSON으로 정합니다. Ref2VA에서는 샷마다 어떤 참조가 나오는지도 정합니다. `json_schema`로 형식을 강제합니다 |
| 코드 | `normalize_plan` | 컷 시각 계산, 화자 ID `(S1)` 부여, 카메라 값을 표준 문장으로 변환, 대사를 `{{D1}}` 토큰으로 치환, 참조 id `R1`을 라벨 `<Subject 1>`로 치환, 음악 설명의 분위기 단어 제거 |
| 2. 작성 | LLM (`write` / `write_ref`) | 계획 브리프를 받아 영어 본문을 씁니다. 대사는 토큰만 씁니다 |
| 코드 | `lint_draft` | 샷 머리말을 계산값으로 다시 씀, 빈 소리 필드 채움, 흐트러진 라벨 표기 정리, 토큰·화자 ID·라벨·분량 검사 |
| 3. 수정 | LLM (`repair`) | 검사에 걸렸을 때만. 문제 목록을 주고 그 부분만 고치게 합니다 |
| 코드 | `substitute`, `assemble` / `assemble_ref` | 토큰을 `<d>[Korean] 원문.</d>`으로 바꾸고 조립. Ref2VA의 `subject_definitions`, `retention_analysis`, 과제 유형 `[reference generation]`, 본문 첫 화풍 문장은 이미지 읽기 결과에서 코드가 만듭니다 |

LLM 호출 수:

| 모드 | 평소 | 같은 이미지를 다시 쓸 때 | 수정이 붙으면 |
|---|---|---|---|
| T2V | 2 | - | 3~4 |
| I2V | 3 | 2 | +1 |
| Ref2VA | 3 | 2 | +1 |
| Ref2VA + 첫 프레임 | 3 (다른 참조도 처음 읽으면 이미지 읽기가 2회라 4) | 2 | +1 |

어떤 경우에도 4회를 넘지 않습니다. 참조 이미지는 여러 영상에 되풀이해 쓰는 것이 보통이라, 이미지 읽기를 따로 떼어 캐시하는 쪽이 계획 단계에 이미지를 직접 붙이는 쪽보다 평균 호출 수와 시간이 적습니다.

### 모델이 이미지를 잘못 읽었을 때

`--look-only`로 읽은 결과를 봅니다. 출력의 `cache` 경로에 있는 JSON 파일을 직접 고치면 다음 실행부터 고친 내용이 쓰입니다. 웹 화면에서는 결과 아래 "이미지를 이렇게 읽었습니다"에서 같은 파일을 입력칸으로 고칩니다.

```bash
uv run h3_prompt.py --look-only --ref 유나=girl.png
```

I2V에서는 첫 프레임에 이미 있는 인물의 외형을 본문에 다시 쓰지 않습니다. 공식 I2VA 예시가 `the young woman shown in <Picture 1> ... preserving her appearance, clothing`처럼 이미지를 가리키기만 하는 방식이고, 모델이 잘못 읽은 세부(머리 길이 등)가 이미지와 어긋나는 것도 막습니다.

## 프롬프트 파일 (`prompts/`)

| 파일 | 용도 |
|---|---|
| `look_frame.system.md` | 0단계, I2V 첫 프레임 읽기 |
| `look_refs.system.md` | 0단계, Ref2VA 참조 이미지 읽기 |
| `plan.system.md` / `plan.user.md` | 1단계 (T2V, I2V)와 사용자 메시지 틀 (`<<이름>>` 자리에 값이 들어감) |
| `plan_ref.system.md` | 1단계 (Ref2VA) |
| `write.system.md` | 2단계 (T2V, I2V). 사용자 메시지는 코드가 만드는 브리프 (`--debug-json`의 `brief`) |
| `write_ref.system.md` | 2단계 (Ref2VA) |
| `repair.system.md` / `repair.user.md` | 3단계 |
| `single_call.system.md` | 1회 호출용 (T2V, I2V). 채팅 UI나 ComfyUI LLM 노드에 그대로 붙여 쓸 수 있습니다 |

스크립트는 실행할 때마다 이 파일들을 읽으므로, 고치면 바로 반영됩니다. 이미지 읽기 프롬프트를 고치면 캐시도 새로 만들어집니다.

### 스크립트 없이 채팅 UI에서 쓸 때

시스템 프롬프트에 `single_call.system.md`를 넣고, 사용자 메시지를 이 형식으로 보냅니다. thinking은 꺼야 합니다.

```text
Video length: 8 seconds
Maximum shots: 2
Language for lines you invent: Korean
Visual style: 2D-animated, Japanese anime style
Music: decide yourself
Speech: allowed where the request asks for it
Mode: text-to-video

Korean request:
여기에 한국어 요청
```

대사를 원하지 않으면 `Speech: none. Nobody speaks or sings.`, 음악을 원하지 않으면 `Music: none`으로 바꿉니다.
이 방식은 컷 시각과 `<d>` 형식을 모델이 직접 쓰므로 틀릴 수 있습니다. 스크립트의 `--single`은 같은 프롬프트를 쓰되 형식 검사와 수정 1회를 붙입니다. Ref2VA는 단일 호출용 프롬프트가 없습니다.

## 출력 예 (Ref2VA, 참조 3장, 8초)

```text
subject_definitions:
<Subject 1> is the girl with short silver hair in <Picture 1>, with short silver hair with a small gold star clip, blue eyes, a white t-shirt, a red zip-up jacket with a yellow star on the chest, ...
<Subject 2> is the environment in <Picture 2>, a shrine entrance at sunset, featuring a large red torii gate, stone lanterns lining a staircase, ...
<Subject 3> is the small round gold robot in <Picture 3>, with a spherical gold body, glowing blue circular eyes, ...

summary:
[reference generation] <Subject 1> runs energetically up the stone stairs of <Subject 2> with <Subject 3> floating closely behind her. ...

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - the girl with short silver hair keeps short silver hair with a small gold star clip, ...

detailed_description:
The target video is in a 2D-animated Japanese anime style with clean line art, soft cel shading, and warm golden hour lighting.
[Shot 1] ... <Subject 1>, the girl with short silver hair, a small gold star clip, blue eyes, ... sprints up the steps ...
[Shot 2] At 00:04.000, the shot cuts to a medium shot of <Subject 1> at the top of the stairs, ... <Subject 1> (S1) shouts playfully, <d>[Korean] 빨리 와!</d> She closes her lips into a grin ...

overall_soundscape:
Evening wind rustles through the red maple leaves, ...

non_diegetic_music:
An upbeat acoustic guitar plays a rhythmic pattern ...
```

모드별 전체 예는 [examples/](examples)에 있습니다(`t2v`, `i2v`, `ref2va`, `ref2va_first_frame`).

## 근거

공식 자료 (2026-11-11 확인):

- [프롬프트 가이드 base](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md): 세 필드, 샷·컷 표기, 카메라 어휘, 화자 ID, `<d>`, 보이스오버 문형, 화면 글자, 소리 두 필드의 범위. I2VA의 정렬 문장과 "첫 프레임 기준점 → 동작 시작 → 전개 → 결과" 구조, 화풍 어휘(`2D-animated` 등)와 "키프레임 과제에서는 화풍을 참조 이미지에서 따온다"
- [프롬프트 가이드 ref](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md): 6섹션, `<Subject N>` / `<Picture N>` 라벨, 과제 유형, 보존 표시(`fully_preserved` 등), 화풍 문장을 `[Shot 1]` 앞에 두는 것, `<Subject N> (Sx)` 화자 표기, 본문 350~500단어, 대사 구두점 정리
- [모델 카드](https://huggingface.co/MiniMaxAI/MiniMax-H3)의 H3-Context-IR 출력 예시: 분량의 기준 (10초에 약 250단어 → 초당 24단어로 잡음)
- [h3-prompt-writing](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing): 길이를 영상 길이에 맞출 것, 추상어보다 구체적 묘사

ComfyUI에서 확인한 것 (ComfyUI 0.43.0의 `comfy_extras/nodes_minimax_h3.py`, `comfy/text_encoders/minimax.py`, 내장 템플릿):

- 프롬프트는 채팅 템플릿 없이 그대로 토큰화되고, 이미지는 `<Picture 1>: [이미지]` 형태로 프롬프트 앞에 붙습니다. 정렬 문장은 자동으로 붙지 않으므로 프롬프트에 직접 넣어야 합니다.
- Ref2VA 노드는 참조 이미지 최대 9장을 연결 순서대로 `<Picture 1>`, `<Picture 2>`…로 넣습니다.
- 내장 `video_minimax_h3_multiframe_reference` 템플릿의 예시 프롬프트가 6섹션 형식을 그대로 씁니다.

커뮤니티에서 가져온 것:

- [ComfyUI 프롬프트 가이드](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-prompt-guide): cfg 1이라 부정 프롬프트가 없으니 있는 것을 쓸 것, 컷은 단일 시각, 말줄임표는 발음을 해침, 원치 않는 말소리가 나오면 소리 두 필드를 채울 것
- [HF 토론 #76](https://huggingface.co/MiniMaxAI/MiniMax-H3/discussions/76): `overall_soundscape`를 비우지 말 것, 대사를 마침표로 끝낼 것, 말하지 않는 인물은 조용하다고 쓸 것
- [HF 토론 #106](https://huggingface.co/MiniMaxAI/MiniMax-H3/discussions/106): 말이 끝나면 입을 다문다고 쓸 것

만들면서 정한 것과 이유:

- 이미지 읽기를 계획에서 분리했습니다. 계획 단계에 이미지를 직접 붙이는 방식(`--no-look`으로 남겨 둠)도 2회 호출로 동작하지만, 분리하면 결과를 캐시·확인·수정할 수 있고 낮은 temperature로 읽게 할 수 있습니다.
- Ref2VA의 정의·보존 분석은 LLM에게 쓰게 하지 않습니다. 이미지 읽기 결과를 문장 틀에 끼우면 라벨 번호와 특징이 어긋날 일이 없습니다.
- 작성 단계 예시가 "Subject 2 = 장소"였던 탓에, 장소 참조가 없을 때 새 장소에 `<Subject 2>`를 붙이는 일이 반복됐습니다(2회 중 2회). 브리프의 샷마다 라벨이 무엇인지 같이 적고 검사를 추가한 뒤 3회 중 3회 사라졌습니다.
- 계획·작성 프롬프트에 "분위기 말 대신 악기·템포"를 적어도 `melancholic`, `emotive` 같은 말이 계속 나왔습니다. 코드로 지웁니다(`strip_mood`).

## 알아 둘 점

- 요청문과 화면은 한국어를 전제로 합니다. 대사 언어는 `--dialogue-lang`으로 바꿀 수 있지만 요청문을 다른 언어로 쓰는 것은 시험하지 않았습니다.
- 모델이 요청에 없는 세부를 채웁니다(옷 색, 조명, 소품, 먼 천둥소리 같은 소리). 의도한 동작입니다. 정해 두고 싶은 것은 요청문에 씁니다.
- Ref2VA에서 요청문에 없는 참조도 계획 단계가 영상에 넣는 경우가 있습니다. 쓰지 않을 참조는 `--ref`에서 뺍니다.
- Ref2VA 본문은 참조가 처음 나올 때 특징을 전부 다시 적습니다(가이드 방식). 그래서 I2V와 달리 이미지 읽기가 틀리면 틀린 내용이 본문에 들어갑니다. `--look-only`나 웹 화면에서 확인하고 고칩니다.
- 요청에 글자 언급이 없어도 화면 글자를 넣는 경우가 있었습니다(T2V 뉴스 장면의 자막 1건).
- 대사가 영상 길이에 비해 길면 경고만 하고 그대로 넣습니다. 한국어 초당 5음절로 계산합니다.
- ComfyUI는 길이를 17프레임 격자로 올림합니다. 6초는 158프레임(6.58초)이 됩니다. 5초(124), 8초(192)처럼 격자에 가까운 길이가 컷 시각과 잘 맞습니다.
- FL2VA(첫·끝 프레임), L2VA, 참조 영상·오디오, 첫 프레임이 아닌 키프레임 참조는 지원하지 않습니다.

## 더 읽을 것

- [MAINTENANCE.md](MAINTENANCE.md): 저장소 구성, 테스트, 고칠 때 지킬 것, 검증 기록
- [deploy/README.md](deploy/README.md): 컨테이너 배포
- [examples/](examples): 모드별 출력 예

## 라이선스

[MIT](LICENSE). MiniMax H3와 그 프롬프트 가이드, 이 도구가 부르는 LLM은 각자의 라이선스를 따릅니다.
