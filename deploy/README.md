# 배포

웹 서버(`server.py`)를 컨테이너로 띄우는 방법입니다.

이 저장소가 맡는 것은 웹 서버 하나입니다. 아래 둘은 범위 밖이라 직접 준비합니다.

- **LLM 서버.** 이미지에 LLM은 들어 있지 않습니다. OpenAI 호환 서버(llama.cpp 등)를 따로 띄우고 그 주소를 `H3_BASE_URL`로 줍니다. 서버가 갖춰야 할 것은 [README](../README.md)의 "LLM 서버"에 있습니다.
- **HTTPS와 인증.** 앱에는 로그인이 없고 포트를 `127.0.0.1`에만 엽니다. 바깥에 내려면 리버스 프록시나 터널을 앞에 두고 거기서 HTTPS와 인증을 겁니다.

## 띄우기

명령은 저장소 루트에서 실행합니다. 설정 파일을 만들고 LLM 서버 주소를 적습니다.

```bash
cp deploy/.env.example deploy/.env
```

```
H3_BASE_URL=http://host.docker.internal:8080/v1
```

```bash
docker compose -f deploy/compose.yml up -d --build
```

`http://127.0.0.1:8000`을 엽니다. 화면 위쪽 배지가 "LLM 연결됨"이면 주소가 맞는 것입니다. `H3_BASE_URL`을 적지 않으면 Compose가 띄우지 않고 무엇이 빠졌는지 알려 줍니다.

## LLM 서버 주소

**컨테이너 안에서 닿는 주소**를 적습니다. 컨테이너에서 `127.0.0.1`은 컨테이너 자신입니다.

| LLM 서버 위치 | `H3_BASE_URL` |
|---|---|
| 컨테이너를 돌리는 PC | `http://host.docker.internal:8080/v1` |
| 다른 서버 | `http://<그 서버 주소>:<포트>/v1` |

- Docker Desktop(Windows에서 확인)은 PC의 `127.0.0.1`에만 열린 LLM 서버에도 `host.docker.internal`로 닿습니다.
- **리눅스 서버는 확인하지 않았습니다.** 리눅스에서는 컨테이너가 호스트의 `127.0.0.1`에 닿지 못하는 것이 보통이라, LLM 서버를 컨테이너에서 닿는 주소에 열어야 합니다.
- LLM 서버가 키를 요구하면 `H3_API_KEY`, 여러 모델을 내는 서버면 `H3_MODEL`도 적습니다.

## 그 밖의 설정

[.env.example](.env.example)에 설명과 함께 있습니다: 포트(`H3_PORT`), 동시에 돌릴 생성 수(`H3_CONCURRENCY`), 줄을 설 수 있는 요청 수(`H3_MAX_WAITING`), 이미지 크기 한도(`H3_MAX_UPLOAD_MB`), LLM에 보내는 이미지의 화소 수 상한(`H3_IMAGE_MAX_PIXELS`).

- 이미지 읽기 캐시는 볼륨 `cache`에 남습니다.
- 컨테이너는 루트가 아닌 계정으로 실행됩니다.
- Compose는 compose 파일이 있는 폴더(`deploy/`)의 `.env`를 자동으로 읽습니다. `deploy/.env`는 `.gitignore`에 들어 있습니다.

## 바깥에 낼 때 앞단이 맞춰야 하는 것

프록시나 터널의 설정은 이 저장소에 없습니다. 무엇을 쓰든 아래를 맞춥니다.

| 항목 | 값 | 이유 |
|---|---|---|
| 인증 | 앞단에서 겁니다 | 앱에는 없습니다. 그대로 열면 누구나 LLM을 쓰고 이미지 읽기 결과를 고칠 수 있습니다 |
| 응답 버퍼링 | 끕니다 | 생성 진행 상황이 줄 단위 스트림입니다. 모으면 화면의 단계 표시가 끝에 한꺼번에 나옵니다 |
| 유휴 시간 제한 | 15초 이상 | 서버가 10초마다 `ping` 줄을 보냅니다 |
| 요청 본문 크기 | `H3_MAX_UPLOAD_MB` × 9 + 여유 (기본 100MB) | 참조 이미지를 9장까지 올립니다 |

## 업데이트와 정리

코드를 받은 뒤 같은 명령을 다시 실행하면 이미지를 새로 만들어 바꿉니다.

```bash
docker compose -f deploy/compose.yml up -d --build
```

내릴 때는 `down`, 이미지 읽기 캐시까지 지우려면 `down -v`입니다.

```bash
docker compose -f deploy/compose.yml down
```

배포한 뒤 확인할 것은 [MAINTENANCE.md](../MAINTENANCE.md)의 "배포 전후 점검"에 있습니다.
