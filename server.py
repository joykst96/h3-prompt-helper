"""h3-prompt 웹 서버: 파이프라인(h3_prompt.py)을 HTTP API로 내고, web/ 폴더의 정적 프론트를 같이 서빙한다.

    uv run server.py                                   # 127.0.0.1:8000
    uv run uvicorn server:app --host 0.0.0.0 --port 8000

API
  GET  /api/health     LLM 서버 연결 상태와 입력 한도
  POST /api/generate   multipart 폼(요청문·옵션·이미지) -> 줄 단위 JSON(NDJSON) 스트림
                       {"event":"queued"} · {"event":"stage","stage":"look|plan|write|repair|single","call":n}
                       · {"event":"ping"} · 마지막에 {"event":"done",...} 또는 {"event":"error","message":...}
  PUT  /api/look/{id}  이미지 읽기 결과를 고친 값으로 바꾼다 (id는 done 이벤트의 look.edit[].id). 본문 {"look": {...}}

환경 변수 (모두 선택)
  H3_BASE_URL  H3_MODEL  H3_API_KEY   LLM 서버 (CLI와 같다. 기본 http://127.0.0.1:8080/v1, gemma4)
  H3_CACHE_DIR          이미지 읽기 캐시 폴더 (기본 .cache/look)
  H3_HOST  H3_PORT      `uv run server.py`로 띄울 때의 주소 (기본 127.0.0.1:8000)
  H3_CONCURRENCY        동시에 돌릴 생성 수 (기본 1. 로컬 LLM 하나를 나눠 쓰므로 나머지는 줄을 선다)
  H3_MAX_WAITING        줄을 설 수 있는 요청 수 (기본 8. 넘으면 503)
  H3_MAX_UPLOAD_MB      이미지 한 장의 크기 한도 (기본 10)
  H3_MAX_REQUEST_CHARS  요청문 길이 한도 (기본 2000)
  H3_LLM_TIMEOUT        LLM 호출 한 번의 제한 시간(초) (기본 300)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

import h3_prompt as core

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
log = logging.getLogger("uvicorn.error").getChild("h3")


def _env_int(name: str, default: int, low: int = 1) -> int:
    try:
        return max(low, int(os.environ.get(name, "")))
    except ValueError:
        return default


CONCURRENCY = _env_int("H3_CONCURRENCY", 1)
MAX_WAITING = _env_int("H3_MAX_WAITING", 8, low=0)
MAX_UPLOAD_MB = _env_int("H3_MAX_UPLOAD_MB", 10)
MAX_UPLOAD = MAX_UPLOAD_MB * 1024 * 1024
MAX_REQUEST_CHARS = _env_int("H3_MAX_REQUEST_CHARS", 2000)
LLM_TIMEOUT = _env_int("H3_LLM_TIMEOUT", 300)
PING_SECONDS = 10       # 이 시간 동안 보낼 것이 없으면 빈 줄 대신 ping을 보낸다 (프록시의 유휴 연결 끊기 방지)
MODES = ("t2v", "i2v", "ref")

# CSP: 스크립트·스타일은 같은 출처의 파일만. 이미지 미리보기는 blob: URL을 쓴다.
SECURITY_HEADERS = [
    (b"content-security-policy", b"default-src 'self'; img-src 'self' blob:; object-src 'none'; base-uri 'none'; "
                                 b"frame-ancestors 'none'; form-action 'self'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


class SecurityHeaders:
    """모든 응답에 보안 헤더를 붙인다. 스트리밍 응답을 버퍼링하지 않도록 순수 ASGI 미들웨어로 둔다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", [])) + SECURITY_HEADERS}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app = FastAPI(title="h3-prompt", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityHeaders)


@app.exception_handler(BadRequest)
async def _bad_request(_: Request, exc: BadRequest):
    return JSONResponse({"error": exc.message}, status_code=exc.status)


@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


# --------------------------------------------------------------------------- 입력 검사

def image_ext(head: bytes) -> str | None:
    """파일 머리로 형식을 본다. 확장자와 Content-Type은 보내는 쪽이 정하므로 믿지 않는다."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return None


def flag(value: object) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def text_field(form, name: str, limit: int, default: str = "") -> str:
    value = form.get(name)
    value = value.strip() if isinstance(value, str) else default
    if len(value) > limit:
        raise BadRequest(f"{name} 값이 너무 깁니다 (최대 {limit}자).")
    return value


async def save_images(files: list[UploadFile], workdir: Path) -> list[Path]:
    """업로드를 검사하고 작업 폴더에 저장한다. 캐시 키가 이미지 내용과 파일 이름으로 정해지므로 원래 이름(영숫자만)을 살린다."""
    paths = []
    for i, f in enumerate(files, 1):
        data = await f.read(MAX_UPLOAD + 1)
        shown = (f.filename or f"{i}번째 이미지")[:80]
        if len(data) > MAX_UPLOAD:
            raise BadRequest(f"이미지는 한 장에 {MAX_UPLOAD_MB}MB까지입니다: {shown}", 413)
        ext = image_ext(data[:16])
        if not ext:
            raise BadRequest(f"PNG 또는 JPEG 이미지만 쓸 수 있습니다: {shown}")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(f.filename or "").stem).strip("_")[:40] or "image"
        path = workdir / f"{i:02d}" / (stem + ext)   # 번호 폴더: 이름이 같은 파일끼리 덮어쓰지 않게
        path.parent.mkdir()
        path.write_bytes(data)
        paths.append(path)
    return paths


def build_options(form, images: list[Path]):
    """폼 값을 CLI와 같은 옵션 객체로 바꾼다. 기본값은 CLI 인자 정의에서 그대로 가져온다."""
    opts = core.build_parser().parse_args([])
    mode = text_field(form, "mode", 10, "t2v")
    if mode not in MODES:
        raise BadRequest("mode는 t2v, i2v, ref 중 하나여야 합니다.")
    try:
        opts.duration = float(text_field(form, "duration", 10, "5"))
        shots = text_field(form, "max_shots", 4)
        opts.max_shots = int(shots) if shots else None
    except ValueError:
        raise BadRequest("길이와 샷 수는 숫자여야 합니다.") from None
    if not 4 <= opts.duration <= 15:
        raise BadRequest("길이는 4~15초여야 합니다.")
    if opts.max_shots is not None and not 1 <= opts.max_shots <= 4:
        raise BadRequest("샷 수는 1~4여야 합니다.")
    opts.style = text_field(form, "style", 120) or None
    opts.speech = text_field(form, "speech", 10, "auto")
    if opts.speech not in ("auto", "on", "off"):
        raise BadRequest("대사 설정은 auto, on, off 중 하나여야 합니다.")
    opts.dialogue_lang = text_field(form, "dialogue_lang", 40) or opts.dialogue_lang
    opts.no_music, opts.no_cache, opts.single = flag(form.get("no_music")), flag(form.get("no_cache")), flag(form.get("single"))

    if mode == "t2v":
        if images:
            raise BadRequest("글만 쓰는 모드에는 이미지를 넣을 수 없습니다.")
    elif mode == "i2v":
        if len(images) != 1:
            raise BadRequest("첫 프레임 모드에는 이미지가 한 장 필요합니다.")
        opts.image = images[0]
    else:
        if not images:
            raise BadRequest("참조 모드에는 이미지가 한 장 이상 필요합니다.")
        if opts.single:
            raise BadRequest("빠른 모드는 참조 모드에서 쓸 수 없습니다.")
        try:
            names = json.loads(form.get("names") or "[]")
        except (TypeError, ValueError):
            names = None
        if not isinstance(names, list) or len(names) != len(images) or not all(isinstance(n, str) for n in names):
            raise BadRequest("참조 이름 목록이 이미지 수와 맞지 않습니다.")
        names = [re.sub(r"\s+", " ", n).strip() for n in names]
        if any(len(n) > 40 for n in names):
            raise BadRequest("참조 이름은 40자까지입니다.")
        if sum(bool(core.FRAME_NOTE.search(n)) for n in names) > 1:
            raise BadRequest("첫 프레임 참조는 하나만 줄 수 있습니다.")
        opts.refs = [{"path": p, "note": n} for p, n in zip(images, names)]
    try:
        return core.finish_options(opts)
    except core.PipelineError as e:
        raise BadRequest(str(e)) from None


# --------------------------------------------------------------------------- 생성 작업

_gate = threading.BoundedSemaphore(CONCURRENCY)
_lock = threading.Lock()
_active = 0   # 돌고 있거나 줄을 선 작업 수


def public_message(message: str) -> str:
    """파이프라인 오류에서 서버 내부 주소와 CLI 안내를 뺀다. 원문은 서버 로그에 남긴다."""
    message = re.sub(r"\s*\(https?://[^)]*\).*", ".", message, flags=re.S)
    return message.replace(" --verbose로 출력을 확인하세요.", " 다시 시도해 보세요.")


def public_look(look: dict | None) -> dict | None:
    """이미지 읽기 결과에서 화면에 보여 줄 것만 고른다 (서버의 파일 경로는 내보내지 않는다).

    edit에는 고칠 수 있는 원본(캐시에 든 값)을 이미지마다 하나씩 넣는다. 화면이 이것을 고쳐 PUT /api/look/{id}로 보낸다."""
    if not look:
        return None

    def frame(f: dict) -> dict:
        out = {k: f.get(k) for k in ("style", "framing", "setting", "caption", "cached", "picture")}
        out["subjects"] = [{k: s.get(k) for k in ("label", "kind", "look", "place")} for s in f.get("subjects") or []]
        return out

    if "refs" in look:
        keys = ("label", "picture", "kind", "name", "features", "style", "note", "from_frame", "cached")
        first = look.get("first_frame")
        out = {"refs": [{k: r.get(k) for k in keys} for r in look["refs"] or []], "first_frame": frame(first) if first else None}
        sources = [("frame", first["picture"], first.get("cache"))] if first else []
        sources += [("ref", r["picture"], r.get("cache")) for r in look["refs"] or [] if not r.get("from_frame")]
    else:
        out = {"frame": frame(look)}
        sources = [("frame", "<Picture 1>", look.get("cache"))]
    out["edit"] = []
    for kind, picture, cache in sorted(sources, key=lambda s: int(re.search(r"\d+", s[1])[0])):
        raw = core._cache_read(Path(cache)) if cache else None   # 캐시를 못 썼으면 고칠 대상이 없다
        if isinstance(raw, dict) and LOOK_ID.fullmatch(Path(cache).stem):
            out["edit"].append({"id": Path(cache).stem, "type": kind, "picture": picture, "look": editable_look(kind, raw)})
    return out


# --------------------------------------------------------------------------- 이미지 읽기 결과 고치기

# 캐시 파일 이름과 같은 모양만 받는다 (h3_prompt._cache_file). 경로 구분자가 들어갈 수 없다
LOOK_ID = re.compile(r"[A-Za-z0-9_-]{1,40}-(frame|ref)-[0-9a-f]{12}")
LOOK_BODY_LIMIT = 32 * 1024
FRAME_KINDS = ("character", "creature", "object")
FORBIDDEN = re.compile(r"[<>{}\[\]]")   # <Subject 1>, {{D1}}, [Shot 1] 같은 표기를 흉내 내지 못하게


def editable_look(kind: str, raw: dict) -> dict:
    """캐시에 든 값에서 화면이 고칠 필드만 고른다."""
    def text(value: object) -> str:
        return core.clean(value) if isinstance(value, str) else ""

    if kind == "frame":
        subjects = [s for s in raw.get("subjects") or [] if isinstance(s, dict)][:4]
        return {**{k: text(raw.get(k)) for k in ("style", "framing", "setting", "caption")},
                "subjects": [{k: text(s.get(k)) for k in ("label", "kind", "look", "place")} for s in subjects]}
    return {"kind": text(raw.get("kind")), "name": text(raw.get("name")), "style": text(raw.get("style")),
            "features": [text(f) for f in raw.get("features") or [] if isinstance(f, str)][:8]}


def look_text(value: object, limit: int, what: str, required: bool = False) -> str:
    """고친 값 하나를 검사한다. 이 글은 계획 단계에 들어가고 일부는 프롬프트에 그대로 들어가므로 영어 한 줄이어야 한다."""
    if not isinstance(value, str):
        raise BadRequest(f"{what}: 글자로 적어 주세요.")
    value = core.clean(value)
    if required and not value:
        raise BadRequest(f"{what}: 비워 둘 수 없습니다.")
    if len(value) > limit:
        raise BadRequest(f"{what}: {limit}자까지 적을 수 있습니다.")
    if core.NON_ENGLISH.search(value):
        raise BadRequest(f"{what}: 영어로 적어 주세요. 프롬프트에 그대로 들어갑니다.")
    if FORBIDDEN.search(value):
        raise BadRequest(f"{what}: 꺾쇠·중괄호·대괄호는 쓸 수 없습니다.")
    return value


def checked_look(kind: str, data: object) -> dict:
    """화면이 보낸 고친 값을 검사해서 캐시에 넣을 모양으로 돌려준다."""
    if not isinstance(data, dict):
        raise BadRequest("고친 내용이 없습니다.")
    if kind == "frame":
        subjects = data.get("subjects")
        if not isinstance(subjects, list) or len(subjects) > 4 or not all(isinstance(s, dict) for s in subjects):
            raise BadRequest("피사체 목록이 잘못됐습니다.")
        out = {"style": look_text(data.get("style"), 160, "화풍"), "framing": look_text(data.get("framing"), 160, "구도"),
               "setting": look_text(data.get("setting"), 600, "장소"), "caption": look_text(data.get("caption"), 300, "요약", True),
               "subjects": []}
        for i, s in enumerate(subjects, 1):
            if s.get("kind") not in FRAME_KINDS:
                raise BadRequest(f"피사체 {i}: 종류가 잘못됐습니다.")
            out["subjects"].append({"label": look_text(s.get("label"), 80, f"피사체 {i} 이름", True), "kind": s["kind"],
                                    "look": look_text(s.get("look"), 400, f"피사체 {i} 외형"),
                                    "place": look_text(s.get("place"), 200, f"피사체 {i} 위치와 자세")})
        return out
    if data.get("kind") not in core.REF_KINDS:
        raise BadRequest("종류가 잘못됐습니다.")
    features = data.get("features")
    if not isinstance(features, list):
        raise BadRequest("특징 목록이 잘못됐습니다.")
    features = [f for f in (look_text(f, 160, "특징") for f in features) if f]
    if not 1 <= len(features) <= 8:
        raise BadRequest("특징은 1~8개로 적어 주세요.")
    return {"kind": data["kind"], "name": look_text(data.get("name"), 80, "이름", True), "features": features,
            "style": look_text(data.get("style"), 160, "화풍")}


@app.put("/api/look/{look_id}")
async def save_look(look_id: str, request: Request):
    """이미지 읽기 결과를 고친 값으로 바꾼다. 이미 있는 캐시 항목만 바꿀 수 있고, 같은 이미지의 다음 생성부터 쓰인다."""
    match = LOOK_ID.fullmatch(look_id)
    path = core.CACHE / f"{look_id}.json"
    if not match or not path.is_file():
        raise BadRequest("그 이미지 읽기 결과가 서버에 없습니다. 다시 생성한 뒤에 고쳐 주세요.", 404)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > LOOK_BODY_LIMIT:
            raise BadRequest("요청이 너무 큽니다.", 413)
    try:
        data = json.loads(body)
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BadRequest("요청이나 저장된 값을 읽을 수 없습니다.") from None
    if not isinstance(data, dict) or not isinstance(stored, dict) or not isinstance(stored.get("look"), dict):
        raise BadRequest("요청이나 저장된 값을 읽을 수 없습니다.")
    stored["look"] = {**stored["look"], **checked_look(match[1], data.get("look"))}
    stored["edited"] = True
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:   # 다 쓴 뒤에 바꿔치기한다: 돌고 있는 생성이 반쯤 쓰인 파일을 읽지 않게
        tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.exception("look save failed")
        tmp.unlink(missing_ok=True)
        raise BadRequest("저장하지 못했습니다. 잠시 뒤에 다시 시도해 주세요.", 500) from None
    log.info("look edited: %s", look_id)
    return {"ok": True}


def run_job(request_text: str, opts, events: queue.Queue, workdir: Path) -> None:
    """작업 스레드. 차례를 기다렸다가 파이프라인을 돌리고, 진행 상황과 결과를 events에 넣는다. 끝에 None을 넣는다."""
    global _active
    started = time.monotonic()
    try:
        if not _gate.acquire(blocking=False):
            events.put({"event": "queued"})
            _gate.acquire()
        try:
            started = time.monotonic()
            llm = core.LLMClient(opts.base_url, opts.model, opts.api_key, opts.max_calls, opts.max_tokens, timeout=LLM_TIMEOUT,
                                 on_call=lambda label, n: events.put({"event": "stage", "stage": label, "call": n}))
            result = (core.run_single if opts.single else core.run_pipeline)(request_text, opts, llm)
        finally:
            _gate.release()
        seconds = round(time.monotonic() - started, 1)
        events.put({"event": "done", "prompt": result["prompt"], "calls": result["calls"], "seconds": seconds,
                    "issues": [{"sev": i.sev, "text": i.ko} for i in result["issues"]],
                    "look": public_look(result["debug"].get("look"))})
        log.info("generate ok: %d calls, %.1fs, %d issues", result["calls"], seconds, len(result["issues"]))
    except core.PipelineError as e:
        log.warning("generate failed: %s", e)
        events.put({"event": "error", "message": public_message(str(e))})
    except Exception:
        log.exception("generate crashed")
        events.put({"event": "error", "message": "서버에서 오류가 났습니다. 잠시 뒤에 다시 시도해 주세요."})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        with _lock:
            _active -= 1
        events.put(None)


def stream(events: queue.Queue):
    while True:
        try:
            event = events.get(timeout=PING_SECONDS)
        except queue.Empty:
            event = {"event": "ping"}
        if event is None:
            return
        yield json.dumps(event, ensure_ascii=False) + "\n"


@app.post("/api/generate")
async def generate(request: Request):
    global _active
    limit = core.MAX_REFS * MAX_UPLOAD + 1024 * 1024
    if int(request.headers.get("content-length") or 0) > limit:
        raise BadRequest("요청이 너무 큽니다.", 413)
    workdir = Path(tempfile.mkdtemp(prefix="h3-web-"))
    try:
        async with request.form(max_files=core.MAX_REFS, max_fields=40) as form:
            request_text = text_field(form, "request", MAX_REQUEST_CHARS)
            if not request_text:
                raise BadRequest("요청문을 적어 주세요.")
            files = [f for f in form.getlist("images") if isinstance(f, UploadFile)]
            opts = build_options(form, await save_images(files, workdir))
        with _lock:
            if _active >= CONCURRENCY + MAX_WAITING:
                raise BadRequest("요청이 밀려 있습니다. 잠시 뒤에 다시 시도해 주세요.", 503)
            _active += 1
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    events: queue.Queue = queue.Queue()
    threading.Thread(target=run_job, args=(request_text, opts, events, workdir), daemon=True).start()
    return StreamingResponse(stream(events), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- 상태

_probe: dict = {"at": 0.0, "value": None}


def _get_json(url: str, api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


def probe_llm() -> dict:
    """LLM 서버가 살아 있는지, 이미지를 받을 수 있는지 본다. 5초 동안은 같은 답을 돌려준다.

    vision은 llama.cpp 서버의 /props가 알려 줄 때만 True/False이고, 그 밖의 서버에서는 None(모름)이다."""
    now = time.monotonic()
    if _probe["value"] is not None and now - _probe["at"] < 5:
        return _probe["value"]
    opts = core.build_parser().parse_args([])
    base = opts.base_url.rstrip("/")
    value: dict = {"reachable": False, "vision": None}
    try:
        _get_json(base + "/models", opts.api_key)
        value["reachable"] = True
        props = _get_json(re.sub(r"/v1$", "", base) + "/props", opts.api_key)
        vision = (props.get("modalities") or {}).get("vision")
        value["vision"] = vision if isinstance(vision, bool) else None
    except (urllib.error.URLError, OSError, ValueError):
        pass
    _probe.update(at=now, value=value)
    return value


@app.get("/api/health")
def health():
    with _lock:
        waiting = max(0, _active - CONCURRENCY)
    return {"ok": True, "llm": probe_llm(), "waiting": waiting,
            "limits": {"max_refs": core.MAX_REFS, "max_upload_mb": MAX_UPLOAD_MB, "max_request_chars": MAX_REQUEST_CHARS,
                       "duration": [4, 15]}}


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("H3_HOST", "127.0.0.1"), port=_env_int("H3_PORT", 8000))
