#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=12.3.0"]
# ///
"""한국어 영상 아이디어 -> MiniMax H3 영어 프롬프트 (로컬 LLM 파이프라인).

세 가지 모드:
  T2V    글만으로            계획 -> 작성                    LLM 2회
  I2V    첫 프레임 이미지    이미지 읽기 -> 계획 -> 작성     LLM 3회 (같은 이미지는 캐시되어 2회)
  Ref2VA 참조 이미지 1~9장   이미지 읽기 -> 계획 -> 작성     LLM 3회 (같은 이미지는 캐시되어 2회)
검사에서 걸리면 수정 호출이 붙고, 어떤 경우에도 4회를 넘지 않는다.
타임스탬프·화자 ID·대사 삽입·참조 라벨 정의·형식 검사는 전부 코드가 한다.

    uv run h3_prompt.py "비 오는 밤 골목에서 고양이가 우산 밑으로 들어온다" -d 6
    uv run h3_prompt.py "소녀가 일어나 우산을 펴고 걸어 나간다" --image first.png -d 6
    uv run h3_prompt.py "유나가 계단을 뛰어 올라간다" --ref 유나=girl.png --ref 배경=shrine.png -d 8
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
# 이미지 읽기 결과. 파일을 직접 고쳐서 모델이 잘못 본 것을 바로잡을 수 있다. 배포할 때는 H3_CACHE_DIR로 볼륨 경로를 준다
CACHE = Path(os.environ.get("H3_CACHE_DIR") or HERE / ".cache" / "look")
# LLM에 보내는 이미지의 화소 수 상한. 넘는 이미지만 비율을 지켜 이 화소 수 이하로 줄여 보낸다. 0이면 제한하지 않는다.
# 이미지 토큰은 화소 수에 비례한다 (Gemma 4 26B A4B + mmproj 실측: 100만 화소 약 470토큰, 180만 약 800, 250만 이상 약 1,120).
# 큰 참조 이미지 9장은 컨텍스트 8192를 넘기므로, 읽기 정확도를 확인한 크기(1344x768, 832x1216, 1024x1024)까지만 그대로 보낸다.
# 이보다 낮추면 토큰은 더 줄지만 읽기가 나빠진다: 60만 화소(1024x585)에서 이미 anime 그림의 화풍을 3D CG로 읽는 일이 생긴다
# (MAINTENANCE.md 검증 기록).
DEFAULT_IMAGE_MAX_PIXELS = 1024 * 1024
try:
    IMAGE_MAX_PIXELS = max(0, int(os.environ.get("H3_IMAGE_MAX_PIXELS") or DEFAULT_IMAGE_MAX_PIXELS))
except ValueError:
    IMAGE_MAX_PIXELS = DEFAULT_IMAGE_MAX_PIXELS

# 화풍 기본값. 이 도구는 주로 anime 영상에 쓴다. 매체 이름은 공식 가이드의 어휘(2D-animated, Live-action ...)를 따른다.
STYLE_PRESETS = {
    "anime": "2D-animated, Japanese anime style",
    "live": "Live-action, cinematic",
    "3d": "3D CG animation",
    "auto": "",
}
DEFAULT_STYLE = STYLE_PRESETS["anime"]
MAX_REFS = 9                        # ComfyUI MiniMaxH3ReferenceToVideo 노드의 ref_images 상한
REF_KINDS = ("character", "creature", "object", "environment", "style")
STYLE_NOTE = re.compile(r"화풍|그림체|스타일|작화|style", re.I)
PLACE_NOTE = re.compile(r"배경|장소|공간|무대|환경|background|scene|place|\bbg\b", re.I)
FRAME_NOTE = re.compile(r"첫\s*프레임|첫\s*장면|시작\s*프레임|first[\s_-]*frame|^(first|frame|start)$", re.I)  # Ref2VA를 I2V처럼

# 단계별 temperature. Gemma 4 권장값은 1.0 (top_p 0.95, top_k 64).
TEMP_PLAN = 1.0     # 계획: 내용 결정이라 권장값 그대로
TEMP_WRITE = 0.7    # 작성: 형식을 지켜야 해서 조금 낮춤
TEMP_REPAIR = 0.3   # 수정: 지적된 곳만 고치게 낮게
TEMP_LOOK = 0.2     # 이미지 읽기: 보이는 것만 적게 낮게

I2V_LINE = ("For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced.")

CAMERA = {  # 값: (동사구, 진폭·속도를 붙일 수 있는지)
    "static": ("holds a static shot", False),
    "push_in": ("pushes in", True),
    "pull_out": ("pulls out", True),
    "zoom_in": ("zooms in", True),
    "zoom_out": ("zooms out", True),
    "pan_left": ("pans left", True),
    "pan_right": ("pans right", True),
    "truck_left": ("trucks left", True),
    "truck_right": ("trucks right", True),
    "tilt_up": ("tilts up", True),
    "tilt_down": ("tilts down", True),
    "pedestal_up": ("pedestals up", True),
    "pedestal_down": ("pedestals down", True),
    "arc": ("circles the scene in an arc shot", True),
    "tracking": ("follows the action in a tracking shot", True),
    "shake_slightly": ("shakes slightly", False),
    "shake_strongly": ("shakes strongly", False),
    "pov": ("shows a POV shot", False),
    "roll_clockwise": ("rolls clockwise", True),
    "roll_counterclockwise": ("rolls counterclockwise", True),
}
CAMERA_ALIASES = {
    "": "static", "none": "static", "fixed": "static", "still": "static", "static_shot": "static",
    "dolly_in": "push_in", "dolly_out": "pull_out", "push": "push_in", "pull": "pull_out",
    "zoom": "zoom_in", "pan": "pan_right", "tilt": "tilt_up", "truck": "truck_right",
    "orbit": "arc", "arc_shot": "arc", "follow": "tracking", "tracking_shot": "tracking",
    "track": "tracking", "handheld": "shake_slightly", "shake": "shake_slightly",
    "roll": "roll_clockwise", "pedestal": "pedestal_up",
}

NON_ENGLISH = re.compile(r"[ᄀ-ᇿ぀-ヿ㄰-㆏一-鿿가-힣Ѐ-ӿ؀-ۿ]")
TOKEN = re.compile(r"\{\{([DT])(\d+)\}\}")
SHOT_MARK = re.compile(r"\[\s*Shot\s*(\d+)\s*\]", re.I)
CUT_PHRASE = re.compile(r"(the\s+)?(camera|shot|scene|view)\s+(cuts|transitions|changes|switches|dissolves|fades|wipes|jumps)\b", re.I)
FRAMING_NOUN = re.compile(r"\b(shot|close-?up|view|angle|frame|profile|insert|overhead|wide|medium)\b", re.I)


class PipelineError(RuntimeError):
    pass


class Issue:
    def __init__(self, sev: str, en: str, ko: str):
        self.sev, self.en, self.ko = sev, en, ko  # sev: "error"는 수정 호출을 부르고 "warn"은 알리기만 한다


# --------------------------------------------------------------------------- LLM

def image_payload(path: Path, max_pixels: int | None = None) -> tuple[str, bytes]:
    """LLM에 보낼 이미지의 (MIME, 바이트). 화소 수가 max_pixels(기본 IMAGE_MAX_PIXELS)를 넘으면 비율을 지켜 줄이고 PNG로 다시 만든다.

    상한 안의 이미지, max_pixels 0, 줄이지 못한 이미지는 파일을 그대로 돌려준다. 캐시 키는 원본 파일로 정하므로
    여기서 무엇을 보내든 캐시는 그대로 맞는다."""
    max_pixels = IMAGE_MAX_PIXELS if max_pixels is None else max_pixels
    original = (mimetypes.guess_type(path.name)[0] or "image/png", path.read_bytes())
    if max_pixels <= 0:
        return original
    try:
        with Image.open(io.BytesIO(original[1])) as im:
            if im.width * im.height <= max_pixels:
                return original
            im = ImageOps.exif_transpose(im)  # 사진의 회전 정보는 다시 저장하면 사라지므로 픽셀에 먼저 적용한다
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.mode or "transparency" in im.info else "RGB")
            scale = (max_pixels / (im.width * im.height)) ** 0.5
            size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))  # 내림: 상한을 넘지 않게
            out = io.BytesIO()
            im.resize(size, Image.Resampling.LANCZOS).save(out, "PNG")
            return "image/png", out.getvalue()
    except Exception:  # 깨진 파일, 모르는 형식, 지나치게 큰 이미지: 원본을 보내고 판단은 LLM 서버에 맡긴다
        return original


class LLMClient:
    """OpenAI 호환 /v1/chat/completions 클라이언트. 호출 횟수를 세고 상한을 넘기지 않는다."""

    def __init__(self, base_url: str, model: str, api_key: str = "", max_calls: int = 4,
                 max_tokens: int = 2048, timeout: int = 600, verbose: bool = False, on_call=None):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.api_key = model, api_key
        self.max_calls, self.max_tokens = max_calls, max_tokens
        self.timeout, self.verbose = timeout, verbose
        self.on_call = on_call  # on_call(label, 몇 번째 호출): 호출 직전에 부른다. 웹 서버가 진행 단계를 알리는 데 쓴다
        self.calls = 0

    @property
    def remaining(self) -> int:
        return self.max_calls - self.calls

    def _post(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, json.dumps(payload).encode("utf-8"), headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def chat(self, system: str, user: str | list, *, temperature: float, schema: dict | None = None,
             image: Path | None = None, label: str = "", max_tokens: int | None = None) -> str:
        """user는 문자열이거나, 문자열과 이미지 경로(Path)를 섞은 목록이다 (이미지마다 앞에 번호를 붙일 때)."""
        if self.remaining <= 0:
            raise PipelineError("LLM 호출 상한에 도달했습니다.")
        parts = (user if isinstance(user, list) else [user]) + ([image] if image else [])
        content: object = user
        if any(isinstance(p, Path) for p in parts):
            content = []
            for p in parts:
                if isinstance(p, Path):
                    mime, data = image_payload(p)
                    b64 = base64.b64encode(data).decode("ascii")
                    content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                else:
                    content.append({"type": "text", "text": p})
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "temperature": temperature, "top_p": 0.95, "top_k": 64,
            "max_tokens": max_tokens or self.max_tokens,
            # thinking은 끈다: 켜면 추론에만 1500토큰 넘게 쓰고 본문이 잘린다 (ctx 8192에서 실측)
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if schema:
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "shot_plan", "strict": True, "schema": schema}}
        self.calls += 1
        if self.on_call:
            self.on_call(label, self.calls)
        try:
            try:
                data = self._post(payload)
            except urllib.error.HTTPError as e:
                if e.code != 400 or isinstance(content, list):  # 이미지를 못 받는 서버는 아래에서 그대로 오류로 알린다
                    raise
                # 확장 필드를 모르는 서버: 표준 필드만으로 한 번 더 (생성이 안 됐으므로 호출 수에 넣지 않음)
                for key in ("response_format", "chat_template_kwargs", "top_k"):
                    payload.pop(key, None)
                data = self._post(payload)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            hint = (" 이미지를 읽으려면 서버를 --mmproj로 띄워야 합니다 (README의 '이미지를 쓰는 모드' 참고)."
                    if isinstance(content, list) else "")
            raise PipelineError(f"LLM 서버 오류 {e.code}: {body}{hint}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise PipelineError(f"LLM 서버에 연결할 수 없습니다 ({self.url}): {e}") from e
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        if self.verbose:
            t = data.get("timings") or {}
            speed = f", {t['predicted_per_second']:.0f} tok/s" if t.get("predicted_per_second") else ""
            print(f"\n----- LLM 호출 {self.calls} [{label}] finish={choice.get('finish_reason')}{speed} -----\n{text}",
                  file=sys.stderr)
        if choice.get("finish_reason") == "length":
            print("[경고] 출력이 max_tokens에서 잘렸습니다. --max-tokens를 늘려 보세요.", file=sys.stderr)
        return text


# --------------------------------------------------------------------------- 0단계: 이미지 읽기 (I2V, Ref2VA)

def strip_article(text: str) -> str:
    return re.sub(r"^(a|an|the)\s+", "", clean(text).rstrip(".,;"), flags=re.I)


def article(word: str) -> str:
    return "an" if re.match(r"[aeiou]", word, re.I) else "a"


def and_join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def parse_ref(value: str) -> dict:
    """--ref 값: '경로' 또는 '이름=경로'. 이름은 요청문에서 그 참조를 부르는 말(유나, 배경, 화풍 ...)."""
    note, path = "", value
    if "=" in value and not Path(value).is_file():
        note, path = value.split("=", 1)
    return {"path": Path(path.strip().strip('"')), "note": note.strip()}


def _cache_file(image: Path, mode: str, note: str, system: str) -> Path:
    h = hashlib.sha256()
    for chunk in (image.read_bytes(), mode.encode(), note.encode("utf-8"), system.encode("utf-8")):
        h.update(chunk + b"\0")
    return CACHE / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', image.stem)[:40]}-{mode}-{h.hexdigest()[:12]}.json"


def _cache_read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["look"]
    except (OSError, ValueError, KeyError):
        return None


def _cache_write(path: Path, image: Path, note: str, look: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"image": str(image), "note": note, "look": look}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        pass  # 캐시는 편의 기능이다. 못 써도 결과는 같다


def look_frame(image: Path, llm: LLMClient, use_cache: bool = True) -> dict:
    """I2V 첫 프레임을 읽어 화풍·구도·장소·피사체를 돌려준다. 같은 이미지는 캐시에서 읽는다(LLM 호출 없음)."""
    system = (PROMPTS / "look_frame.system.md").read_text(encoding="utf-8")
    cache = _cache_file(image, "frame", "", system)
    raw = _cache_read(cache) if use_cache else None
    cached = raw is not None
    if raw is None:
        s = {"type": "string"}
        subject = {"type": "object", "additionalProperties": False, "required": ["label", "kind", "look", "place"],
                   "properties": {"label": s, "kind": {"type": "string", "enum": ["character", "creature", "object"]},
                                  "look": s, "place": s}}
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["style", "framing", "setting", "caption", "subjects"],
                  "properties": {"style": s, "framing": s, "setting": s, "caption": s,
                                 "subjects": {"type": "array", "maxItems": 4, "items": subject}}}
        raw = extract_json(llm.chat(system, ["First frame of the video:", image], temperature=TEMP_LOOK, schema=schema,
                                    label="look", max_tokens=900))
        _cache_write(cache, image, "", raw)
    subjects = []
    for sub in raw.get("subjects") or []:
        label = clean(sub.get("label")) if isinstance(sub, dict) else ""
        if label:
            label = label if re.match(r"(the|a|an)\s", label, re.I) else f"the {label}"
            kind = clean(sub.get("kind")).lower()
            subjects.append({"label": label, "kind": kind if kind in ("character", "creature", "object") else "character",
                             "look": clean(sub.get("look")).rstrip("."), "place": clean(sub.get("place")).rstrip(".")})
    caption = clean(raw.get("caption")).rstrip(".")
    caption = caption[:1].lower() + caption[1:] if re.match(r"(A|An|The)\s", caption) else caption
    return {"style": clean(raw.get("style")).rstrip(",.") or DEFAULT_STYLE, "framing": clean(raw.get("framing")).rstrip("."),
            "setting": clean(raw.get("setting")), "caption": caption, "subjects": subjects, "cache": str(cache), "cached": cached}


def look_refs(refs: list[dict], llm: LLMClient, use_cache: bool = True) -> tuple[list[dict], dict | None]:
    """참조 이미지를 읽는다. 반환: (참조 대상 목록, 첫 프레임 정보 또는 None).

    N번째 --ref가 <Picture N>이다(ComfyUI 노드에 연결하는 순서). 참조 대상에는 나온 순서대로 <Subject 1>, <Subject 2>...를 붙인다.
    보통 참조는 이미지 하나가 대상 하나다. 이름이 '첫프레임'인 참조는 영상의 0번 프레임으로 쓰는 이미지라서 look_frame으로
    읽고, 그 안의 피사체들이 각각 참조 대상이 된다(공식 multiframe 템플릿과 같은 방식: 같은 이미지가 정체성 참조이자 첫 프레임).
    캐시에 없는 보통 참조는 모아서 LLM을 한 번 부른다."""
    system = (PROMPTS / "look_refs.system.md").read_text(encoding="utf-8")
    frame_at = [i for i, r in enumerate(refs) if FRAME_NOTE.search(r["note"])]
    if len(frame_at) > 1:
        raise PipelineError("첫 프레임 참조는 하나만 줄 수 있습니다.")
    looks: list[dict | None] = []
    for i, r in enumerate(refs):
        if i in frame_at:
            looks.append({})
            continue
        r["cache"] = _cache_file(r["path"], "ref", r["note"], system)
        looks.append(_cache_read(r["cache"]) if use_cache else None)
    first = None
    if frame_at:
        i = frame_at[0]
        first = look_frame(refs[i]["path"], llm, use_cache)
        first.update(n=i + 1, picture=f"<Picture {i + 1}>", path=str(refs[i]["path"]))
    pending = [i for i, look in enumerate(looks) if look is None]
    if pending:
        s = {"type": "string"}
        item = {"type": "object", "additionalProperties": False, "required": ["picture", "kind", "name", "features", "style"],
                "properties": {"picture": {"type": "integer"}, "kind": {"type": "string", "enum": list(REF_KINDS)}, "name": s,
                               "features": {"type": "array", "minItems": 2, "maxItems": 8, "items": s}, "style": s}}
        schema = {"type": "object", "additionalProperties": False, "required": ["pictures"],
                  "properties": {"pictures": {"type": "array", "minItems": len(pending), "maxItems": len(pending), "items": item}}}
        parts: list = []
        for k, i in enumerate(pending, 1):
            note = f" (user's note: {refs[i]['note']})" if refs[i]["note"] else ""
            parts += [f"Picture {k}{note}:", refs[i]["path"]]
        got = extract_json(llm.chat(system, parts, temperature=TEMP_LOOK, schema=schema, label="look",
                                    max_tokens=300 + 220 * len(pending))).get("pictures") or []
        got = [g for g in got if isinstance(g, dict)]
        if len(got) != len(pending):
            raise PipelineError(f"이미지 읽기 단계가 {len(pending)}장 중 {len(got)}장만 돌려줬습니다. 다시 실행해 보세요.")
        for i, g in zip(pending, got):  # 순서로 짝짓는다. 모델이 쓴 picture 번호는 믿지 않는다
            looks[i] = g
            _cache_write(refs[i]["cache"], refs[i]["path"], refs[i]["note"], g)
    out: list[dict] = []

    def add(pic: int, **fields) -> None:
        n = len(out) + 1
        out.append({"n": n, "rid": f"R{n}", "label": f"<Subject {n}>", "picture": f"<Picture {pic}>", "from_frame": False, **fields})

    for pic, (r, look) in enumerate(zip(refs, looks), 1):
        if first and pic == first["n"]:
            # 첫 프레임 안에서 라벨을 받는 것은 정체성을 지켜야 하는 인물·생물이다(최대 3). 우산·표지판 같은 소품까지 라벨을 주면
            # 문장이 라벨투성이가 된다. 인물·생물이 하나도 없으면(제품 컷 등) 첫 피사체 하나가 라벨을 받는다.
            living = [s for s in first["subjects"] if s["kind"] in ("character", "creature")][:3] or first["subjects"][:1]
            first["props"] = [s for s in first["subjects"] if s not in living]
            for sub in living:  # 외형은 이미지 읽기의 한 줄 설명을 그대로 특징으로 쓴다
                add(pic, kind=sub["kind"], name=strip_article(sub["label"]), features=[sub["look"]], place=sub["place"],
                    style=first["style"], note="", path=str(r["path"]), cache=first["cache"], cached=first["cached"], from_frame=True)
            continue
        kind = clean(look.get("kind")).lower()
        kind = kind if kind in REF_KINDS else "object"
        if STYLE_NOTE.search(r["note"]):       # 용도는 사용자의 이름이 정한다. 모델의 판단보다 우선
            kind = "style"
        elif kind == "style":
            kind = "environment"
        elif PLACE_NOTE.search(r["note"]):
            kind = "environment"
        features = [f for f in (clean(x).rstrip(".,;") for x in (look.get("features") or []) if isinstance(x, str)) if f][:8]
        add(pic, kind=kind, name=strip_article(look.get("name")) or f"subject of picture {pic}", features=features,
            style=clean(look.get("style")).rstrip(",."), note=r["note"], path=str(r["path"]),
            cache=str(r["cache"]), cached=pic - 1 not in pending)
    return out, first


# --------------------------------------------------------------------------- 1단계: 계획

def plan_schema(max_shots: int, allow_speech: bool = True, ref_ids: list[str] | None = None) -> dict:
    s = {"type": "string"}
    line = {"type": "object", "additionalProperties": False,
            "required": ["speaker", "mode", "language", "text", "delivery"],
            "properties": {"speaker": s, "mode": {"type": "string", "enum": ["speech", "voiceover", "singing"]},
                           "language": s, "text": s, "delivery": s}}
    screen = {"type": "object", "additionalProperties": False, "required": ["text", "where"],
              "properties": {"text": s, "where": s}}
    shot = {"type": "object", "additionalProperties": False,
            "required": ["seconds", "framing", "camera_move", "camera_amplitude", "camera_speed",
                         "action", "sounds", "lines", "screen_text"],
            "properties": {"seconds": {"type": "number"}, "framing": s,
                           "camera_move": {"type": "string", "enum": list(CAMERA)},
                           "camera_amplitude": {"type": "string", "enum": ["small", "medium", "large"]},
                           "camera_speed": {"type": "string", "enum": ["slow", "normal", "fast"]},
                           "action": s, "sounds": s,
                           "lines": {"type": "array", "items": line, **({} if allow_speech else {"maxItems": 0})},
                           "screen_text": {"type": "array", "items": screen}}}
    if ref_ids:  # 참조 모드: 샷마다 화면에 나오는 참조 대상을 적게 한다
        shot["required"].insert(1, "refs")
        shot["properties"] = {"seconds": shot["properties"]["seconds"],
                              "refs": {"type": "array", "items": {"type": "string", "enum": ref_ids}},
                              **{k: v for k, v in shot["properties"].items() if k != "seconds"}}
    char = {"type": "object", "additionalProperties": False, "required": ["id", "label", "look", "voice"],
            "properties": {"id": s, "label": s, "look": s, "voice": s}}
    return {"type": "object", "additionalProperties": False,
            "required": ["style", "setting", "characters", "shots", "ambience", "music"],
            "properties": {"style": s, "setting": s,
                           "characters": {"type": "array", "items": char},
                           "shots": {"type": "array", "minItems": 1, "maxItems": max_shots, "items": shot},
                           "ambience": s, "music": s}}


def fill(template: str, **values: object) -> str:
    for key, value in values.items():
        template = template.replace(f"<<{key}>>", str(value))
    return template


def style_choice(opts, frame: dict | None = None, refs: list[dict] | None = None) -> tuple[str, str]:
    """(화풍, 계획 단계에 보여 줄 한 줄). --style이 있으면 그것, 없으면 이미지에서 읽은 화풍, 그것도 없으면 anime."""
    chosen = (opts.style or "").strip()
    if chosen:
        style = STYLE_PRESETS.get(chosen.lower(), chosen)
        if not style:
            return "", "decide from the request (Live-action, 2D-animated, 3D CG, claymation, watercolor or vintage film)"
        return style, style
    if frame:
        return frame["style"], f"{frame['style']} (taken from the first frame; keep it)"
    for r in refs or []:
        if r["kind"] == "style" and r["style"]:
            return r["style"], f"{r['style']} (the style of reference {r['rid']}; keep it)"
    for r in refs or []:
        if r["style"]:
            return r["style"], f"{r['style']} (taken from the reference pictures)"
    return DEFAULT_STYLE, DEFAULT_STYLE


def request_header(request: str, opts, frame: dict | None = None, refs: list[dict] | None = None,
                   opening: dict | None = None) -> str:
    """1단계와 단일 호출 모드가 같이 쓰는 사용자 메시지. refs가 None이 아니면 참조 모드, opening은 그 안의 첫 프레임 정보."""
    if refs is not None:
        lines = []
        for r in refs:
            name = f" User's name for it: {r['note']}." if r["note"] else ""
            if r["kind"] == "style":
                lines.append(f"- {r['rid']} [style] the drawing style of the whole video.{name} It is not a character or a place; "
                             f"leave it out of `refs`.")
            elif r.get("from_frame"):
                lines.append(f"- {r['rid']} [{r['kind']}] {r['name']}, already in the first frame. Features: "
                             f"{', '.join(r['features'])}. In the first frame: {r['place']}")
            else:
                lines.append(f"- {r['rid']} [{r['kind']}] {r['name']}.{name} Features: {', '.join(r['features'])}")
        mode = "reference-to-video"
        note = ("\nReference subjects:\n" + "\n".join(lines) + "\n") if lines else ""
        if opening:
            note += ("\nFirst frame: the video starts from a given image, exactly as it is. An earlier step read these facts "
                     "from it; they are fixed:\n"
                     f"- framing: {opening['framing']}\n- setting: {opening['setting']}\n"
                     + "".join(f"- {p['label']} (a prop without a reference id): {p['look']}. In the first frame: {p['place']}\n"
                               for p in opening.get("props", [])) +
                     "Rules for the first frame: `setting` restates the setting above. Shot 1 keeps the framing above, lists "
                     "every reference subject that is already in the first frame in `refs`, and its `action` begins from "
                     "their positions above and develops forward. Change nothing that the image already shows. A reference "
                     "subject that is not in the first frame enters during a shot or appears after a cut.\n")
    elif frame:
        facts = [f"- framing: {frame['framing']}", f"- setting: {frame['setting']}"]
        facts += [f"- {s['label']}: {s['look']}. In the frame: {s['place']}" for s in frame["subjects"]]
        mode, note = "image-to-video", (
            "The video starts from a given first-frame image. An earlier step read these facts from the image; they are fixed:\n"
            + "\n".join(facts) + "\n"
            "Rules for this mode: copy these subjects into `characters` with the same labels and looks. `setting` restates the "
            "setting above. Shot 1 keeps the framing above, and its `action` begins from the poses above and develops forward. "
            "Change nothing that the image already shows. Prefer a single shot.\n")
    elif opts.image:
        mode, note = "image-to-video", ("The attached image is the first frame of the video. `style`, `setting`, every "
                                        "character's `look` and the framing of shot 1 describe that image as it is. "
                                        "The action starts from that exact frame. Prefer a single shot.\n")
    elif opts.i2v:
        mode, note = "image-to-video", ("The request describes an image that is the first frame of the video. Shot 1 "
                                        "starts from that exact frame and the action develops forward from it. "
                                        "Prefer a single shot.\n")
    else:
        mode, note = "text-to-video", ""
    return fill((PROMPTS / "plan.user.md").read_text(encoding="utf-8"),
                duration=fmt_num(opts.duration), max_shots=opts.max_shots,
                dialogue_language=opts.dialogue_lang, style=style_choice(opts, frame or opening, refs)[1],
                music_policy="none (the user turned music off)" if opts.no_music else "decide yourself",
                speech_policy=("allowed where the request asks for it" if opts.allow_speech
                               else "none. Nobody speaks or sings; every `lines` list is empty."),
                mode=mode, mode_note=note, request=request.strip())


SPEECH_HINT = re.compile(
    r"[\"“”‘’「」『』]|말하|말한|말했|말을|대사|대화|내레이션|나레이션|독백|노래|외치|외친|속삭|묻는|물어|묻자|대답|인사|중얼|소리치|소리친"
    r"|부른다|부르는|부르며|연설|발표|앵커|라고|얘기|이야기|수다|통화|전화|\b(say|says|speak|talk|dialogue|narrat|sing)", re.I)


def speech_wanted(request: str, mode: str) -> bool:
    """요청문에 말·노래를 뜻하는 표현이 없으면 대사를 아예 막는다 (약한 모델이 대사를 지어내는 것 방지)."""
    return mode == "on" or (mode == "auto" and bool(SPEECH_HINT.search(request)))


def extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise PipelineError("계획 단계 출력에서 JSON을 찾지 못했습니다.")
    raw = text[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", raw))  # 끝에 붙은 쉼표 정도는 봐준다
        except json.JSONDecodeError as e:
            raise PipelineError(f"계획 단계 JSON을 읽지 못했습니다: {e}") from e


def fmt_num(x: float) -> str:
    return f"{x:g}"


def fmt_time(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:06.3f}"


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def detect_language(text: str, fallback: str) -> str:
    if re.search(r"[가-힣]", text):
        return "Korean"
    if re.search(r"[぀-ヿ]", text):
        return "Japanese"
    if re.search(r"[一-鿿]", text):
        return "Chinese"
    if re.search(r"[Ѐ-ӿ]", text):
        return "Russian"
    if re.search(r"[؀-ۿ]", text):
        return "Arabic"
    fallback = re.sub(r"[^A-Za-z]", "", fallback or "")
    return fallback.capitalize() if fallback and fallback.lower() != "korean" else "English"


def sanitize_line(text: str) -> str:
    """대사는 그대로 두되, 가이드가 발음을 망친다고 한 장식만 정리한다."""
    text = clean(text).strip("\"'“”‘’「」『』")
    text = re.sub(r"(\.{2,}|…+)\s*$", ".", text)           # 끝의 말줄임표 -> 마침표
    text = re.sub(r"\s*(\.{2,}|…+)\s*", ", ", text)         # 중간 말줄임표 -> 쉼표
    text = re.sub(r"[~〜♡♥★☆♪]|[\U0001F000-\U0001FAFF]", "", text)
    text = re.sub(r"([!?.,])\1+", r"\1", text).strip()
    if text and text[-1] not in ".?!。？！":
        text += "."
    return text


def speech_seconds(text: str) -> float:
    hangul = len(re.findall(r"[가-힣]", text))
    cjk = len(re.findall(r"[぀-ヿ一-鿿]", text))
    words = len(re.findall(r"[A-Za-zÀ-ÿ']+", text))
    pauses = len(re.findall(r"[,.!?。、？！]", text))
    return hangul / 5.0 + cjk / 4.5 + words / 2.6 + pauses * 0.25


MOOD_WORD = re.compile(r"\b(melanchol\w*|emotional|emotive|sad|happy|hopeful|nostalgic|dramatic|romantic|uplifting|"
                       r"heartwarming|heartfelt|tense|mysterious|peaceful|cheerful|bittersweet|sentimental|wistful|"
                       r"lighthearted|somber|sombre)\b", re.I)


def strip_mood(music: str) -> str:
    """음악 설명에서 분위기 형용사를 지운다. 가이드는 악기·템포·음량만 쓰라고 하는데 모델이 자주 어긴다."""
    # "soft and nostalgic melody", "a flute and playful strings"처럼 and로 이어진 것은 지우면 문장이 깨질 수 있어 그대로 둔다
    out = re.sub(r",?\s*(?<!\band\s)" + MOOD_WORD.pattern + r"(?=[\s,.])(?!\s+and\b)(\s*,)?",
                 lambda m: " " if not m.group(0).startswith(",") else "", music, flags=re.I)
    out = re.sub(r"\s+([,.])", r"\1", re.sub(r"\s{2,}", " ", out)).strip(" ,")
    out = re.sub(r"\b(a|an) (?=(\w))", lambda m: ("an " if m.group(2).lower() in "aeiou" else "a ") if m.group(1).islower()
                 else ("An " if m.group(2).lower() in "aeiou" else "A "), out)
    return out[:1].upper() + out[1:] if music[:1].isupper() else out


def camera_sentence(move: str, amplitude: str, speed: str) -> str:
    key = re.sub(r"[\s\-]+", "_", clean(move).lower())
    key = CAMERA_ALIASES.get(key, key)
    phrase, scalable = CAMERA.get(key, CAMERA["static"])
    out = f"The camera {phrase}"
    if scalable:
        amp, spd = clean(amplitude).lower(), clean(speed).lower()
        if amp in ("small", "large"):
            out += f" with {amp} amplitude"
        if spd in ("slow", "fast"):
            out += f" at {spd} speed"
    return out


def allocate_cuts(planned: list[float], minimum: list[float], total: float) -> list[float]:
    """샷 길이를 영상 길이에 맞추고 컷 시각(0.5초 단위)을 돌려준다."""
    n = len(planned)
    if n == 1:
        return []
    if sum(minimum) > total:
        minimum = [m * total / sum(minimum) for m in minimum]
    weights = [max(p, 0.1) for p in planned]
    alloc = [w * total / sum(weights) for w in weights]
    for _ in range(20):
        deficit = sum(max(0.0, m - a) for a, m in zip(alloc, minimum))
        slack = sum(max(0.0, a - m) for a, m in zip(alloc, minimum))
        if deficit < 1e-6 or slack < 1e-6:
            break
        alloc = [m if a < m else a - deficit * (a - m) / slack for a, m in zip(alloc, minimum)]
    cuts, t = [], 0.0
    for a in alloc[:-1]:
        t += a
        cuts.append(max(round(t * 2) / 2, (cuts[-1] if cuts else 0.0) + 1.0))
    for i in range(n - 2, -1, -1):  # 마지막 샷에도 1초 이상 남긴다
        cuts[i] = min(cuts[i], total - 1.0 - (n - 2 - i))
    return cuts


def normalize_plan(raw: dict, opts, frame: dict | None = None, refs: list[dict] | None = None,
                   opening: dict | None = None) -> tuple[dict, list[Issue]]:
    """LLM이 낸 계획을 검증하고, 계산이 필요한 것(컷 시각·화자 ID·토큰·참조 라벨)을 코드로 채운다.

    frame은 I2V(fl2va)의 첫 프레임 정보. refs가 None이 아니면 참조 모드이고, opening은 참조 모드에서 첫 프레임으로 쓰는 이미지의 정보다."""
    notes: list[Issue] = []
    ref_mode = refs is not None
    refs = refs or []
    shots_in = [s for s in (raw.get("shots") or []) if isinstance(s, dict)]
    if not shots_in:
        raise PipelineError("계획에 샷이 없습니다.")
    if len(shots_in) > opts.max_shots:  # 문법 제약이 없는 서버 대비: 넘치는 샷은 마지막 샷에 합친다
        keep, extra = shots_in[:opts.max_shots], shots_in[opts.max_shots:]
        for s in extra:
            keep[-1]["action"] = clean(keep[-1].get("action")) + " " + clean(s.get("action"))
            keep[-1].setdefault("lines", []).extend(s.get("lines") or [])
            keep[-1].setdefault("screen_text", []).extend(s.get("screen_text") or [])
            keep[-1].setdefault("refs", []).extend(s.get("refs") or [])
        shots_in = keep

    ref_by_id = {r["rid"].lower(): r for r in refs}

    def labeled(text: object) -> str:
        """계획이 R1처럼 적은 참조 id를 최종 프롬프트의 라벨 <Subject 1>로 바꾼다."""
        return re.sub(r"\bR(\d+)\b", lambda m: ref_by_id[m.group(0).lower()]["label"] if m.group(0).lower() in ref_by_id
                      else m.group(0), clean(text))

    def ref_char(r: dict, voice: str = "") -> dict:
        return {"id": r["rid"], "label": r["label"], "look": ", ".join(r["features"]), "voice": voice, "sid": None, "ref": r}

    chars = []
    for i, c in enumerate(c for c in (raw.get("characters") or []) if isinstance(c, dict)):
        cid = clean(c.get("id")) or f"C{i + 1}"
        if cid.lower() in ref_by_id:   # 참조 대상의 이름과 외형은 이미지에서 읽은 값으로 고정한다
            if not any(x["id"].lower() == cid.lower() for x in chars):
                chars.append(ref_char(ref_by_id[cid.lower()], clean(c.get("voice"))))
            continue
        chars.append({"id": cid, "label": clean(c.get("label")) or f"character {i + 1}",
                      "look": clean(c.get("look")), "voice": clean(c.get("voice")), "sid": None, "ref": None})
    by_id = {c["id"].lower(): c for c in chars}
    for r in refs:  # 계획이 빠뜨린 참조 대상도 화자로 쓸 수 있게 넣어 둔다
        if r["kind"] != "style" and r["rid"].lower() not in by_id:
            by_id[r["rid"].lower()] = ref_char(r)
            chars.append(by_id[r["rid"].lower()])
    if frame:  # I2V: 첫 프레임에 있는 인물의 외형은 이미지에서 읽은 값을 쓴다
        seen = {s["label"].lower(): s for s in frame["subjects"]}
        for c in chars:
            if c["label"].lower() in seen and seen[c["label"].lower()]["look"]:
                c["look"] = seen[c["label"].lower()]["look"]

    shots, d_count, t_count, speakers = [], 0, 0, 0
    for s in shots_in:
        lines = []
        for ln in (s.get("lines") or []) if opts.allow_speech else []:
            text = sanitize_line(ln.get("text")) if isinstance(ln, dict) else ""
            if not text:
                continue
            mode = clean(ln.get("mode")).lower()
            mode = "voiceover" if "voice" in mode or "narrat" in mode else "singing" if "sing" in mode else "speech"
            who = by_id.get(clean(ln.get("speaker")).lower())
            if who is None:
                if mode != "voiceover" and len(chars) == 1:
                    who = chars[0]
                else:
                    who = {"id": f"X{len(chars) + 1}", "label": "an off-screen narrator" if mode == "voiceover" else "an off-screen voice",
                           "look": "never visible", "voice": "", "sid": None, "ref": None}
                    chars.append(who)
                    by_id[who["id"].lower()] = who
            first = who["sid"] is None
            if first:
                speakers += 1
                who["sid"] = f"S{speakers}"
            d_count += 1
            lines.append({"token": f"{{{{D{d_count}}}}}", "who": who, "first": first, "mode": mode,
                          "language": detect_language(text, ln.get("language")), "text": text,
                          "delivery": clean(ln.get("delivery")), "seconds": speech_seconds(text)})
        texts = []
        for st in s.get("screen_text") or []:
            text = clean(st.get("text")).strip("\"“”") if isinstance(st, dict) else ""
            if text:
                t_count += 1
                texts.append({"token": f"{{{{T{t_count}}}}}", "text": text, "where": clean(st.get("where"))})
        try:
            seconds = float(s.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        # 이 샷에 보이는 참조 대상: 계획의 refs + 본문에 적힌 R id + 이 샷에서 말하는 참조 인물
        mentioned = [str(x).lower() for x in (s.get("refs") or [])]
        mentioned += [m.lower() for m in re.findall(r"\bR\d+\b", f"{s.get('framing')} {s.get('action')}")]
        mentioned += [ln["who"]["id"].lower() for ln in lines if ln["who"].get("ref") and ln["mode"] != "voiceover"]
        shot_refs = [r for r in refs if r["rid"].lower() in mentioned and r["kind"] != "style"]
        shots.append({"planned": seconds, "framing": labeled(s.get("framing")),
                      "camera": camera_sentence(s.get("camera_move"), s.get("camera_amplitude"), s.get("camera_speed")),
                      "action": labeled(s.get("action")), "sounds": labeled(s.get("sounds")),
                      "lines": lines, "screen_text": texts, "refs": shot_refs})

    for r in refs:  # 첫 프레임에 있는 대상은 Shot 1에 반드시 보인다
        if r.get("from_frame") and r not in shots[0]["refs"]:
            shots[0]["refs"] = sorted(shots[0]["refs"] + [r], key=lambda x: x["n"])
    for r in refs:  # 어느 샷에도 안 쓰인 참조: 장소는 모든 샷의 배경으로, 나머지는 첫 샷에 넣고 알린다
        if r["kind"] == "style" or any(r in s["refs"] for s in shots):
            continue
        for s in (shots if r["kind"] == "environment" else shots[:1]):
            s["refs"] = sorted(s["refs"] + [r], key=lambda x: x["n"])
        if r["kind"] != "environment":
            notes.append(Issue("warn", "", f"참조 {r['n']}번({r['note'] or r['name']})이 계획의 어느 샷에도 없어 첫 샷에 넣었습니다. "
                                           f"요청문에서 그 대상을 이름으로 불러 주세요."))

    minimum = [max(2.0, sum(ln["seconds"] for ln in s["lines"]) + 1.0) for s in shots]
    speech = sum(ln["seconds"] for s in shots for ln in s["lines"])
    if sum(minimum) > opts.duration + 0.01:
        notes.append(Issue("warn", "", f"대사가 길어 {fmt_num(opts.duration)}초 안에 다 들어가지 않을 수 있습니다 "
                                       f"(예상 발화 {speech:.1f}초). 영상 길이를 늘리거나 대사를 줄이세요."))
    cuts = allocate_cuts([s["planned"] for s in shots], minimum, opts.duration)
    starts = [0.0] + cuts
    for i, s in enumerate(shots):
        s["start"], s["end"] = starts[i], (starts[i + 1] if i + 1 < len(shots) else opts.duration)

    music = clean(raw.get("music"))
    if opts.no_music or re.fullmatch(r"(none|n/?a|no music|null|no)?\.?", music.lower()):
        music = ""
    music = strip_mood(music)
    wanted = style_choice(opts, frame or opening, refs)[0]
    style = clean(raw.get("style")).rstrip(",.") or wanted or DEFAULT_STYLE
    if (frame or opening or refs) and wanted and style.split(",")[0].strip().lower() != wanted.split(",")[0].strip().lower():
        style = wanted  # 이미지가 정한 매체(2D-animated 등)를 계획이 바꿨으면 되돌린다
    mode = "ref" if ref_mode else "i2v" if (frame or opts.image or opts.i2v) else "t2v"
    if mode == "ref":   # 공식 가이드: 참조 모드의 본문은 보통 350~500단어. 짧은 영상은 조금 줄인다
        words = int(min(450, max(300, opts.duration * 35)) // 10 * 10)
    else:
        words = int(min(380, max(110, opts.duration * 24)) // 10 * 10)
    return ({"mode": mode, "style": style, "setting": labeled(raw.get("setting")), "characters": chars, "shots": shots,
             "ambience": labeled(raw.get("ambience")), "music": music, "words": words, "frame": frame, "refs": refs, "opening": opening,
             "desc_name": "detailed_description" if mode == "ref" else "integrated_multimodal_description"}, notes)


# --------------------------------------------------------------------------- 2단계: 작성

def line_pattern(ln: dict) -> str:
    who, sid, token = ln["who"], ln["who"]["sid"], ln["token"]
    if who.get("ref"):  # 참조 모드 가이드의 문형: <Subject N> (Sx) ... , <d>
        voice = " + voice" if ln["first"] and who["voice"] else ""
        if ln["mode"] == "voiceover":
            return f"{who['label']} ({sid}){voice} says in an off-screen voiceover, {token} while his/her lips remain completely closed."
        return f"{who['label']} ({sid}){voice} + {'sings' if ln['mode'] == 'singing' else 'verb'}, {token}"
    subject = f"<{who['label']} + voice>" if ln["first"] and who["voice"] else who["label"]
    if ln["mode"] == "voiceover":
        return f"{subject} ({sid}) says in an off-screen voiceover: {token} while his/her lips remain completely closed."
    verb = "sings" if ln["mode"] == "singing" else "says"
    return f"{subject} ({sid}) {verb}: {token}"


def render_brief(plan: dict, opts) -> str:
    n = len(plan["shots"])
    ref_mode = plan["mode"] == "ref"
    mode = {"ref": "reference-to-video", "i2v": "image-to-video", "t2v": "text-to-video"}[plan["mode"]]
    out = [f"VIDEO: {fmt_num(opts.duration)} seconds, {n} shot{'s' if n > 1 else ''}, {mode}",
           f"LENGTH TARGET: about {plan['words']} words for {plan['desc_name']}"]
    if plan["mode"] == "i2v":
        out.append("FIRST FRAME: the video starts from a given image, <Picture 1>. Shot 1 opens on that exact frame: name the "
                   "main subject as \"shown in <Picture 1>\" with its starting pose and place, then let the action begin.")
        if plan["frame"]:
            out.append(f"  framing of the image: {plan['frame']['framing']}")
            out += [f"  {s['label']} in the image: {s['place']}" for s in plan["frame"]["subjects"]]
    first = plan.get("opening") if ref_mode else None   # 참조 모드에서 첫 프레임으로 쓰는 이미지
    if first:
        out.append(f"FIRST FRAME: the video starts from the image {first['picture']}, exactly as it is. Shot 1 begins with its "
                   f"opening words, then shows every subject at its starting place below before anything moves.")
        out.append(f"  framing of the image: {first['framing']}")
        out += [f"  {r['label']} in the image: {r['place']}" for r in plan["refs"] if r.get("from_frame")]
        out += [f"  {p['label']} in the image (no label): {p['place']}" for p in first.get("props", [])]
    out += [f"STYLE: {plan['style']}", f"SETTING: {plan['setting']}"]

    def role(c: dict) -> str:
        return f"speaks as ({c['sid']}), voice: {c['voice'] or 'natural voice'}" if c["sid"] else "silent, never speaks"

    if ref_mode:
        speakers = {c["id"]: c for c in plan["characters"] if c.get("ref")}
        out.append("REFERENCE SUBJECTS:")
        for r in plan["refs"]:
            if r["kind"] == "style":
                continue  # 화풍 참조는 코드가 본문 첫 문장에 넣는다
            tail = " (place)" if r["kind"] == "environment" else " (already in the first frame)" if r.get("from_frame") else ""
            who = f" | {role(speakers[r['rid']])}" if r["kind"] in ("character", "creature") and r["rid"] in speakers else ""
            look = ("the image fixes the look. Write only the label and this name; never describe hair, face or clothing"
                    if r.get("from_frame") else ", ".join(r["features"]))
            out.append(f"- {r['label']} = the {r['name']}{tail}: {look}{who}")
        if first:
            out.append(f"PLACE: the setting of the first frame. It has no label. Describe it in ordinary words from SETTING.")
        elif not any(r["kind"] == "environment" for r in plan["refs"]):
            out.append("PLACE: has no reference picture and no label. Describe it in ordinary words from SETTING.")
    others = [c for c in plan["characters"] if not c.get("ref")]
    if others:
        out.append("OTHER CHARACTERS (no label):" if ref_mode else "CHARACTERS:")
        in_frame = {s["label"].lower() for s in plan["frame"]["subjects"]} if plan["frame"] else set()
        for c in others:
            # 첫 프레임에 이미 있는 인물은 외형을 넘기지 않는다: 이미지가 정하고, 모델이 잘못 읽은 세부가 프롬프트에 섞이는 것도 막는다
            look = ("already in <Picture 1>; the image fixes the look, so refer to it by this name only"
                    if c["label"].lower() in in_frame else c["look"])
            out.append(f"- {c['label']}: {look} | {role(c)}")
    for i, s in enumerate(plan["shots"], 1):
        span = s["end"] - s["start"]
        out.append(f"SHOT {i} ({'first' if i == 1 else 'next'} {fmt_num(span)} seconds)")
        if ref_mode:
            opening = (f"[Shot 1] The shot begins from {first['picture']}." if first else "[Shot 1]") if i == 1 \
                else f"[Shot {i}] At {fmt_time(s['start'])}, the shot cuts to"
        else:
            opening = f"[Shot 1] {plan['style']}," if i == 1 else f"[Shot {i}] At {fmt_time(s['start'])}, the camera cuts to"
        out.append(f"  opening words: {opening}")
        if ref_mode:  # 라벨마다 무엇인지 같이 적는다: 예시를 따라 엉뚱한 대상(라벨 없는 장소 등)에 라벨을 붙이는 것을 막는다
            out.append("  labels in this shot: " + (", ".join(f"{r['label']} ({'place: ' if r['kind'] == 'environment' else ''}"
                                                              f"the {r['name']})" for r in s["refs"]) or "none"))
        out.append(f"  framing: {s['framing']}")
        out.append(f"  camera sentence: {s['camera']}")
        out.append(f"  action: {s['action']}")
        out.append(f"  sounds: {s['sounds']}")
        for ln in s["lines"]:
            who = ln["who"]
            out.append(f"  line {ln['token']}: {who['label']} ({who['sid']}), delivery: {ln['delivery'] or 'natural'}, "
                       f"lasts about {max(1, round(ln['seconds']))} s. Pattern: {line_pattern(ln)}")
        for st in s["screen_text"]:
            out.append(f"  visible text {st['token']}: on {st['where'] or 'a sign'}")
    out.append(f"AMBIENCE: {plan['ambience']}")
    out.append(f"MUSIC: {plan['music'] or 'none'}")
    return "\n".join(out)


def split_fields(text: str, ref: bool = False) -> dict | None:
    """LLM 출력에서 필드를 떼어 낸다 (기본 3개, 참조 모드는 summary까지 4개). 마크다운 장식과 앞뒤 군말은 버린다."""
    text = re.sub(r"```[a-z]*", "", text).replace("**", "").replace("`", "")
    if ref:
        m = re.search(r"summary\s*:\s*(.*?)\s*detailed_description\s*:\s*(.*?)\s*overall_soundscape\s*:\s*(.*?)\s*"
                      r"non_diegetic_music\s*:\s*(.*)$", text, flags=re.S | re.I)
        if not m:
            return None
        summary, desc, sound, music = (clean(g) for g in m.groups())
        return {"summary": summary, "desc": desc, "sound": sound, "music": music}
    m = re.search(r"integrated_multimodal_description\s*:\s*(.*?)\s*overall_soundscape\s*:\s*(.*?)\s*"
                  r"non_diegetic_music\s*:\s*(.*)$", text, flags=re.S | re.I)
    if not m:
        return None
    desc, sound, music = (clean(g) for g in m.groups())
    return {"desc": desc, "sound": sound, "music": music}


def normalize_tokens(text: str) -> str:
    text = re.sub(r"\{+\s*([DT])\s*(\d+)\s*\}+", r"{{\1\2}}", text)
    text = re.sub(r"<d>\s*(?:\[[A-Za-z ]+\]\s*)?(\{\{D\d+\}\})\s*</d>", r"\1", text)   # 토큰을 <d>로 감싼 경우
    return re.sub(r"[\"“”']\s*(\{\{[DT]\d+\}\})\s*[\"“”']", r"\1", text)                # 토큰을 따옴표로 감싼 경우


def normalize_labels(text: str) -> str:
    """<subject 1>, <Subject1>, Subject 1, <picture 1>처럼 흐트러진 참조 라벨을 표준 표기로 고친다."""
    for word in ("Subject", "Picture"):
        text = re.sub(rf"<\s*{word}\s*(\d+)\s*>", rf"<{word} \1>", text, flags=re.I)
        text = re.sub(rf"(?<![<\w]){word} (\d+)\b(?!>)", rf"<{word} \1>", text)
    return text


def fix_shot_headers(desc: str, starts: list[float], cut: str = "the camera cuts to") -> tuple[str, bool]:
    """샷 머리말을 코드가 계산한 시각으로 다시 쓴다. 샷 수가 다르면 (원문, False)."""
    parts = SHOT_MARK.split(desc)
    bodies = [b.strip() for b in parts[2::2]]
    if len(bodies) != len(starts) or parts[0].strip():
        return desc, False
    out = []
    for i, body in enumerate(bodies):
        body = re.sub(r"^[\s,:\-–—]*(At\s+[\d:.]+\s*(seconds?|s)?\s*,?\s*)?", "", body, flags=re.I)
        if i == 0:
            out.append(f"[Shot 1] {body}")
            continue
        if CUT_PHRASE.match(body):
            body = body[0].lower() + body[1:]
        elif re.match(r"(a|an|the)\s", body, re.I) and FRAMING_NOUN.search(" ".join(body.split()[:7])):
            body = f"{cut} " + body[0].lower() + body[1:]
        else:
            body = f"{cut} a new angle. " + body[0].upper() + body[1:]
        out.append(f"[Shot {i + 1}] At {fmt_time(starts[i])}, {body}")
    return " ".join(out), True


def plan_sound_sentence(plan: dict) -> str:
    bits = [plan["ambience"]] + [s["sounds"] for s in plan["shots"]]
    bits = [b.rstrip(". ") for b in bits if b]
    return ". ".join(b[0].upper() + b[1:] for b in bits) + "." if bits else "Soft room tone continues throughout."


def lint_draft(fields: dict, plan: dict) -> list[Issue]:
    """토큰을 실제 대사로 바꾸기 전 초안을 검사한다. 코드로 고칠 수 있는 건 여기서 바로 고친다."""
    issues: list[Issue] = []
    ref_mode, field = plan["mode"] == "ref", plan["desc_name"]
    desc = normalize_tokens(fields["desc"])
    if plan["mode"] != "t2v":
        desc = normalize_labels(desc)
    if ref_mode:  # 화풍 문장은 코드가 붙이므로 [Shot 1] 앞에 모델이 쓴 것은 버린다
        m = SHOT_MARK.search(desc)
        desc = desc[m.start():] if m else desc
    desc, ok = fix_shot_headers(desc, [s["start"] for s in plan["shots"]],
                                cut="the shot cuts to" if ref_mode else "the camera cuts to")
    fields["desc"] = desc
    n = len(plan["shots"])
    if not ok:
        issues.append(Issue("error", f"{field} must contain exactly {n} shot(s), each starting "
                                     f"with the opening words from the plan ([Shot 1] ... [Shot {n}]), and nothing before [Shot 1].",
                            "샷 수 또는 샷 머리말이 계획과 다릅니다."))
    segments = [b for b in SHOT_MARK.split(desc)[2::2]] if ok else [desc] * n

    if plan["mode"] == "i2v" and "<Picture 1>" not in segments[0]:
        issues.append(Issue("error", "Shot 1 must open on the first frame: right after the opening words, name the main subject as "
                                     "\"shown in <Picture 1>\" with its starting pose and place, for example: the girl shown in "
                                     "<Picture 1> sits on the bench.", "Shot 1에 첫 프레임 <Picture 1> 언급이 없습니다."))
    first = plan.get("opening") if ref_mode else None
    if first and ok and not re.match(r"\s*The shot (begins|starts|opens) (from|on|with) " + re.escape(first["picture"]),
                                     segments[0], re.I):
        # 첫 프레임 문장은 정해진 문형이라 모델이 빠뜨리면 코드가 넣는다 (LLM을 다시 부르지 않는다)
        desc = desc.replace("[Shot 1] ", f"[Shot 1] The shot begins from {first['picture']}. ", 1)
        fields["desc"] = desc
        segments = [b for b in SHOT_MARK.split(desc)[2::2]]
    if ref_mode:
        for key in ("sound", "music"):  # 소리 필드에는 라벨을 쓰지 않는다 (공식 예시). 이름으로 바꿔 둔다
            for r in plan["refs"]:
                fields[key] = normalize_labels(fields[key]).replace(r["label"], f"the {r['name']}")
        known_labels = {r["label"] for r in plan["refs"]}
        for i, shot in enumerate(plan["shots"]):
            for r in shot["refs"]:
                # 장소 라벨은 한 번 소개하면 된다 (공식 예시도 뒤 샷에서는 생략한다). 인물·사물은 나오는 샷마다 라벨이 있어야 한다
                if r["label"] not in (desc if r["kind"] == "environment" else segments[i]):
                    issues.append(Issue("error", f"Shot {i + 1} must name {r['label']} (the {r['name']}) by its label, written "
                                                 f"exactly as {r['label']}.", f"Shot {i + 1}에 {r['label']} 라벨이 없습니다."))
        for r in plan["refs"]:  # 라벨 바로 뒤의 설명이 그 참조와 한 단어도 안 겹치면 다른 대상에 라벨을 붙인 것이다
            own = set(re.findall(r"[a-z]{4,}", f"{r['name']} {' '.join(r['features'])}".lower()))
            for m in re.finditer(re.escape(r["label"]) + r",\s+(?:an?|the)\s+([^,.;]{3,90})", desc):
                said = set(re.findall(r"[a-z]{4,}", m.group(1).lower())) - {"from", "shot", "same", "with"}
                if said and own and not (said & own):
                    issues.append(Issue("error", f"{r['label']} is the {r['name']}, but the text uses it for \"{m.group(1).strip()}\". "
                                                 f"Describe that without a label, and keep {r['label']} only for the {r['name']}.",
                                        f"{r['label']} 라벨을 다른 대상({m.group(1).strip()[:40]})에 붙였습니다."))
                    break
        text = desc + " " + fields.get("summary", "")
        for lab in sorted(set(re.findall(r"<Subject \d+>", text)) - known_labels):
            issues.append(Issue("error", f"Remove {lab}; the plan defines only {', '.join(sorted(known_labels))}.",
                                f"계획에 없는 라벨 {lab}."))
        if "<Picture" in (text.replace(first["picture"], "") if first else text):
            allowed = f" The only exception is {first['picture']} in the opening words of Shot 1." if first else ""
            issues.append(Issue("error", "Do not write <Picture ...>. Refer to reference pictures only through their "
                                         f"<Subject N> labels.{allowed}", "본문에 <Picture N>이 들어갔습니다."))
        summary = re.sub(r"^\s*\[[a-z +]+\]\s*", "", normalize_labels(fields.get("summary", "")))  # 과제 유형은 코드가 붙인다
        summary = re.sub(r"\s*\(S\d+\)", "", summary)
        if len(summary.split()) < 8 or TOKEN.search(summary) or "<d>" in summary.lower() or NON_ENGLISH.search(summary):
            issues.append(Issue("error", "summary must be 2 or 3 English sentences that say what happens in the video and name "
                                         "every reference subject by its label. It contains no tokens and no spoken words.",
                                "summary가 비었거나 대사·토큰이 들어갔습니다."))
        else:  # 요약에서 빠진 라벨은 코드가 한 문장으로 보탠다 (LLM을 다시 부르지 않는다)
            missing = [r["label"] for r in plan["refs"] if r["kind"] != "style" and r["label"] not in summary]
            if missing:
                summary = summary.rstrip() + f" The video also features {and_join(missing)}."
            if first and first["picture"] not in summary:
                summary = f"The video begins from {first['picture']}. " + summary
        fields["summary"] = summary

    # 소리 필드: 비었으면 계획에서 채운다 (LLM을 다시 부르지 않는다)
    if not fields["sound"] or re.fullmatch(r"(n/?a|none)\.?", fields["sound"], re.I):
        fields["sound"] = plan_sound_sentence(plan)
    if not plan["music"]:
        fields["music"] = "N/A"
    elif not fields["music"] or re.fullmatch(r"(n/?a|none)\.?", fields["music"], re.I):
        fields["music"] = plan["music"][0].upper() + plan["music"][1:].rstrip(".") + "."
    else:
        fields["music"] = strip_mood(fields["music"])  # 가이드: 음악은 악기·템포로 쓰고 분위기 말은 쓰지 않는다

    for name, key in ((field, "desc"), ("overall_soundscape", "sound"), ("non_diegetic_music", "music")):
        found = NON_ENGLISH.findall(fields[key])
        if found:
            issues.append(Issue("error", f"{name} contains non-English characters ({''.join(found[:12])}). Write this field in "
                                         f"English only; spoken words appear only as tokens such as {{{{D1}}}}.",
                                f"{name}에 영어가 아닌 글자가 섞였습니다."))
    if "<d>" in desc.lower():
        issues.append(Issue("error", "Remove every <d>...</d> block you wrote yourself. A spoken line is written only as its "
                                     "token, for example {{D1}}.", "모델이 <d> 블록을 직접 썼습니다."))
    for key, name in (("sound", "overall_soundscape"), ("music", "non_diegetic_music")):
        if TOKEN.search(fields[key]) or "<d>" in fields[key].lower():
            issues.append(Issue("error", f"{name} must not contain tokens or spoken words. Describe only sounds there.",
                                f"{name}에 대사 토큰이 들어갔습니다."))

    any_line = False
    for i, shot in enumerate(plan["shots"]):
        for ln in shot["lines"]:
            any_line = True
            tok, sid = ln["token"], ln["who"]["sid"]
            count = desc.count(tok)
            if count != 1:
                issues.append(Issue("error", f"The token {tok} must appear exactly once, in Shot {i + 1} (found {count} times). "
                                             f"Write it as: {line_pattern(ln)}", f"대사 토큰 {tok}이(가) {count}번 나옵니다."))
                continue
            if tok not in segments[i]:
                issues.append(Issue("error", f"Move the line {tok} into Shot {i + 1}, where the plan lists it.",
                                    f"대사 토큰 {tok}이(가) 다른 샷에 들어갔습니다."))
            pos = desc.index(tok)
            before, after = desc[max(0, pos - 220):pos], desc[pos + len(tok):pos + len(tok) + 140]
            if re.search(r'["“][^"“”]{2,90}["”]\s*[,.:]?\s*$', before) or re.match(r'\s*[,.]?\s*["“][^"“”]{2,90}["”]', after):
                issues.append(Issue("error", f"Delete the quoted words next to {tok}. The token already stands for the spoken "
                                             f"words; write them nowhere else, not even translated.",
                                    f"{tok} 옆에 대사를 따옴표로 한 번 더 적었습니다."))
            if f"({sid})" not in before:
                issues.append(Issue("error", f"Put the speaker ID ({sid}) right after the phrase that names the speaker, just "
                                             f"before {tok}. Write it as: {line_pattern(ln)}", f"{tok} 앞에 화자 ID ({sid})가 없습니다."))
            elif ln["who"].get("ref") and not re.search(re.escape(ln["who"]["label"]) + r"\s*\(" + sid + r"\)", before):
                issues.append(Issue("error", f"The speaker of {tok} is a reference subject: write its label directly before the "
                                             f"speaker ID, as {ln['who']['label']} ({sid}). Pattern: {line_pattern(ln)}",
                                    f"{tok} 앞에 {ln['who']['label']} ({sid}) 형태가 없습니다."))
            if ln["mode"] == "voiceover" and ("off-screen voiceover" not in before or "lips remain" not in after):
                issues.append(Issue("error", f"{tok} is narration. Write it exactly as: {line_pattern(ln)}",
                                    f"{tok}의 보이스오버 문형이 맞지 않습니다."))
        for st in shot["screen_text"]:
            if desc.count(st["token"]) != 1:
                issues.append(Issue("error", f"The token {st['token']} (visible text on {st['where'] or 'a sign'}) must appear "
                                             f"exactly once, in Shot {i + 1}.", f"화면 글자 토큰 {st['token']} 개수가 맞지 않습니다."))
    known = {m.group(0) for s in plan["shots"] for it in s["lines"] + s["screen_text"] for m in [TOKEN.match(it["token"])]}
    for m in TOKEN.finditer(desc):
        if m.group(0) not in known:
            issues.append(Issue("error", f"Remove the token {m.group(0)}; the plan has no such line.", f"계획에 없는 토큰 {m.group(0)}."))
    if not any_line and re.search(r"\(S\d+\)", desc):
        issues.append(Issue("error", "Nobody speaks in this video. Remove speaker IDs such as (S1) and any spoken words; "
                                     "everyone keeps their lips closed.", "대사가 없는데 화자 ID가 나왔습니다."))

    words = len(desc.split())
    if words < plan["words"] * 0.55:
        issues.append(Issue("error", f"{field} is too short ({words} words). Reach about {plan['words']} "
                                     f"words by adding concrete visible detail: positions in the frame, colors, materials, light, "
                                     f"small movements.", f"본문이 너무 짧습니다 ({words}단어, 목표 {plan['words']})."))
    elif words > plan["words"] * 1.8:
        issues.append(Issue("warn", f"Shorten {field} to about {plan['words']} words.",
                            f"본문이 깁니다 ({words}단어, 목표 {plan['words']})."))
    body = re.sub(r"\[Shot \d+\] At [\d:.]+,", "", desc)
    if re.search(r"\b\d{1,2}:\d{2}(\.\d+)?\b|\b\d+(\.\d+)?\s*-?\s*(seconds?|secs?)\b", body):
        issues.append(Issue("warn", "Remove timestamps and second counts from inside the shots; keep only the cut time in "
                                    "each shot's opening words.", "샷 본문에 시간 표기가 들어 있습니다."))
    m = re.search(r"\b(no|without)\s+(text|subtitles?|captions?|watermarks?|music|dialogue|speech|blur|distortion|people)\b", desc, re.I)
    if m:
        issues.append(Issue("warn", f"Rephrase \"{m.group(0)}\" positively: describe what is present instead.",
                            f"부정형 표현이 있습니다: {m.group(0)}"))
    m = re.search(r"\b(beautiful|stunning|breathtaking|masterpiece|award-winning|4k|8k|ultra[- ]detailed|highly detailed|high quality)\b", desc, re.I)
    if m:
        issues.append(Issue("warn", f"Replace \"{m.group(0)}\" with a concrete visible detail.", f"추상적인 수식어: {m.group(0)}"))
    return issues


def substitute(desc: str, plan: dict) -> str:
    for shot in plan["shots"]:
        for ln in shot["lines"]:
            desc = desc.replace(ln["token"], f"<d>[{ln['language']}] {ln['text']}</d>")
        for st in shot["screen_text"]:
            desc = desc.replace(st["token"], f"\"{st['text']}\"")
    desc = re.sub(r"</d>\s*[.。]", "</d>", desc)          # 대사 안에서 이미 문장이 끝났다
    desc = re.sub(r"</d>(?=[A-Za-z\[])", "</d> ", desc)
    return re.sub(r"\s+", " ", desc).strip()


# --------------------------------------------------------------------------- 마지막 검사와 조립

def assemble(fields: dict, i2v: bool) -> str:
    body = (f"integrated_multimodal_description: {fields['desc']}\n\n"
            f"overall_soundscape: {fields['sound']}\n\n"
            f"non_diegetic_music: {fields['music']}")
    return f"{I2V_LINE}\n\n{body}" if i2v else body


def style_sentence(style: str) -> str:
    """'2D-animated, Japanese anime style, clean line art' -> 참조 모드 본문 맨 앞의 화풍 문장."""
    parts = [p.strip() for p in style.split(",") if p.strip()] or [DEFAULT_STYLE]
    head, rest = parts[0], parts[1:]
    if rest and re.search(r"\bstyle$", rest[0], re.I):
        head = f"{head} {rest.pop(0)}"
    elif not re.search(r"\bstyle$", head, re.I):
        head += " style"
    if re.match(r"[A-Z][a-z]", head):  # Live-action -> live-action. 2D, 3D CG는 그대로
        head = head[0].lower() + head[1:]
    adjectives = [r for r in rest if " " not in r]  # cinematic처럼 한 단어짜리는 앞에 붙인다: a cinematic live-action style
    rest = [r for r in rest if " " in r]
    head = " ".join(adjectives + [head])
    return f"The target video is in {article(head)} {head}" + (f" with {and_join(rest)}" if rest else "") + "."


def assemble_ref(fields: dict, plan: dict) -> str:
    """참조 모드의 6개 섹션. 정의·보존 분석·화풍 문장은 이미지 읽기 결과에서 코드가 만든다."""
    refs, desc = plan["refs"], fields["desc"]
    segments = SHOT_MARK.split(desc)[2::2]
    defs, keeps, style_ref = [], [], None
    for r in refs:
        feats = and_join(r["features"])
        if r["kind"] == "style":
            style_ref = r
            defs.append(f"{r['label']} is the visual style of {r['picture']}: {r['style'] or plan['style']}, with {feats}.")
            keeps.append(f"{r['label']} (applies to every shot): fully_preserved - every shot keeps {feats}.")
            continue
        where = [f"[Shot {i}]" for i, (seg, shot) in enumerate(zip(segments, plan["shots"]), 1)
                 if r["label"] in seg or r in shot["refs"]] or ["[Shot 1]"]
        if r.get("from_frame"):
            # 첫 프레임 안의 피사체는 외형을 글로 적지 않는다. 잘못 읽은 "short black hair"를 적었더니 H3가 이미지의 긴 머리를
            # 단발로 바꿨다(2026-11-11 실측). 이미지가 이미 0번 프레임에 있으므로 글은 이미지를 가리키기만 한다.
            what = "the face, hair and clothing" if r["kind"] == "character" else "the shape and colors"
            defs.append(f"{r['label']} is the {r['name']} shown in {r['picture']}, with {what} exactly as in that picture.")
            keeps.append(f"{r['label']} (appears in {', '.join(where)}): fully_preserved - the {r['name']} keeps the "
                         f"appearance shown in {r['picture']}.")
            continue
        if r["kind"] == "environment":
            defs.append(f"{r['label']} is the environment in {r['picture']}, {article(r['name'])} {r['name']}, featuring {feats}.")
        else:
            defs.append(f"{r['label']} is the {r['name']} in {r['picture']}, with {feats}.")
        keeps.append(f"{r['label']} (appears in {', '.join(where)}): fully_preserved - the {r['name']} keeps {feats}.")
    first, task = plan.get("opening"), "reference generation"
    if first:  # 공식 가이드 2.2 / 3 / 4.1: 프레임으로 쓰는 이미지는 <Picture N>으로 따로 정의하고 과제 유형에 keyframe completion을 넣는다
        showing = first["caption"] or "the starting composition of the video"
        defs.append(f"{first['picture']} is the first frame of [Shot 1], showing {showing}.")
        keeps.append(f"{first['picture']} ([Shot 1] first frame): fully_preserved - the video starts exactly from "
                     f"{first['picture']}'s framing, subject positions, and setting.")
        task = "keyframe completion + reference generation" if any(r["kind"] != "style" for r in refs) else "keyframe completion"
    opening = (f"The target video follows the visual style of {style_ref['label']}: {plan['style']}." if style_ref
               else style_sentence(plan["style"]))
    summary = fields["summary"]
    if style_ref and style_ref["label"] not in summary:
        summary += f" The whole video follows the visual style of {style_ref['label']}."
    shots = re.sub(r"\s*(\[Shot \d+\])", r"\n\1", desc).strip()
    return ("subject_definitions:\n" + "\n".join(defs) + "\n\n"
            f"summary:\n[{task}] {summary}\n\n"
            "retention_analysis:\n" + "\n".join(keeps) + "\n\n"
            f"detailed_description:\n{opening}\n{shots}\n\n"
            f"overall_soundscape:\n{fields['sound']}\n\n"
            f"non_diegetic_music:\n{fields['music']}")


def lint_final(fields: dict, duration: float, max_shots: int) -> list[Issue]:
    """완성된 프롬프트의 형식 검사. 단일 호출 모드에서는 이것만 쓴다."""
    issues: list[Issue] = []
    desc = fields["desc"]
    numbers = [int(n) for n in SHOT_MARK.findall(desc)]
    if not numbers or numbers != list(range(1, len(numbers) + 1)) or not desc.lstrip().lower().startswith("[shot 1]"):
        issues.append(Issue("error", "integrated_multimodal_description must start with [Shot 1] and number its shots "
                                     "[Shot 1], [Shot 2], ... in order.", "샷 번호가 순서대로가 아닙니다."))
    elif len(numbers) > max_shots:
        issues.append(Issue("error", f"Use at most {max_shots} shot(s); merge the extra shots.", f"샷이 {len(numbers)}개로 상한을 넘습니다."))
    else:
        bodies, last = SHOT_MARK.split(desc)[2::2], 0.0
        for i, body in enumerate(bodies[1:], 2):
            m = re.match(r"\s*At (\d{2}):(\d{2}\.\d{3}),", body)
            t = int(m.group(1)) * 60 + float(m.group(2)) if m else None
            if t is None or t <= last or t >= duration:
                issues.append(Issue("error", f"[Shot {i}] must begin with its cut time as \"At MM:SS.mmm,\" (for example "
                                             f"\"At 00:05.000,\"), later than the previous cut and below {fmt_num(duration)} seconds.",
                                    f"[Shot {i}]의 컷 시각이 없거나 범위를 벗어납니다."))
                break
            last = t
    if desc.lower().count("<d>") != desc.lower().count("</d>"):
        issues.append(Issue("error", "Every <d> needs a closing </d>.", "<d> 태그 짝이 맞지 않습니다."))
    for m in re.finditer(r"<d>(.*?)</d>", desc, flags=re.S):
        inner = m.group(1).strip()
        if not re.fullmatch(r"\[[A-Z][A-Za-z]+\] .*[.?!。？！]", inner, flags=re.S):
            issues.append(Issue("error", f"Write the line as <d>[Language] words.</d> with a language tag and final "
                                         f"punctuation: {m.group(0)[:80]}", "<d> 안의 형식이 맞지 않습니다."))
        before = desc[max(0, m.start() - 220):m.start()]
        if not re.search(r"\(S\d+(,\s*S\d+)*\)", before):
            issues.append(Issue("error", f"Put a speaker ID such as (S1) after the speaker phrase before {m.group(0)[:60]}",
                                "<d> 앞에 화자 ID가 없습니다."))
        if "voiceover" in before[-60:] and "lips remain" not in desc[m.end():m.end() + 140]:
            issues.append(Issue("error", f"Right after the voiceover line {m.group(0)[:60]} add: while his/her lips remain "
                                         f"completely closed.", "보이스오버 뒤에 입을 다물고 있다는 문장이 없습니다."))
    outside = re.sub(r"<d>.*?</d>|\"[^\"]*\"", "", desc, flags=re.S) + " " + fields["sound"] + " " + fields["music"]
    found = NON_ENGLISH.findall(outside)
    if found:
        issues.append(Issue("error", f"Outside <d>...</d> and quoted on-screen text, write English only. Found: {''.join(found[:12])}",
                            "<d>와 따옴표 밖에 영어가 아닌 글자가 있습니다."))
    if not fields["sound"] or re.fullmatch(r"(n/?a|none)\.?", fields["sound"], re.I):
        issues.append(Issue("error", "overall_soundscape must describe real ambient and action sounds in 1-4 sentences.",
                            "overall_soundscape가 비었습니다."))
    if not fields["music"]:
        issues.append(Issue("error", "non_diegetic_music must describe the score or be exactly N/A.", "non_diegetic_music이 비었습니다."))
    return issues


def issue_list(issues: list[Issue]) -> str:
    return "\n".join(f"{i}. {it.en}" for i, it in enumerate([x for x in issues if x.en], 1))


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.sev == "error"]


# --------------------------------------------------------------------------- 실행 흐름

def run_pipeline(request: str, opts, llm) -> dict:
    """계획 -> 작성 -> (검사에 걸리면) 수정. 반환: prompt, issues, calls, debug."""
    debug: dict = {}
    opts.allow_speech = speech_wanted(request, opts.speech)
    frame, refs, opening = look_images(opts, llm)   # refs가 None이 아니면 참조 모드. opening은 참조 모드의 첫 프레임 정보
    if frame or refs is not None:
        debug["look"] = frame or {"refs": refs, "first_frame": opening}
    header = request_header(request, opts, frame, refs, opening)
    system = (PROMPTS / ("plan_ref.system.md" if refs is not None else "plan.system.md")).read_text(encoding="utf-8")
    schema = plan_schema(opts.max_shots, opts.allow_speech, [r["rid"] for r in refs if r["kind"] != "style"] if refs else None)
    direct = opts.image if (opts.image and not frame) else None  # --no-look: 계획 단계에 이미지를 직접 붙이는 옛 경로
    raw = None
    for attempt in range(2):  # JSON이 깨졌을 때만 한 번 더
        text = llm.chat(system, header, temperature=TEMP_PLAN if attempt == 0 else 0.5, schema=schema, image=direct, label="plan")
        try:
            raw = extract_json(text)
            break
        except PipelineError:
            if attempt:
                raise
    plan, notes = normalize_plan(raw, opts, frame, refs, opening)
    brief = render_brief(plan, opts)
    debug.update(plan_raw=raw, brief=brief)
    ref_mode = plan["mode"] == "ref"
    names = ("summary:, detailed_description:" if ref_mode else "integrated_multimodal_description:") + \
        ", overall_soundscape:, non_diegetic_music:"

    def show(fields: dict) -> str:
        head = (f"summary: {fields['summary']}\n\ndetailed_description: {fields['desc']}" if ref_mode
                else f"integrated_multimodal_description: {fields['desc']}")
        return f"{head}\n\noverall_soundscape: {fields['sound']}\n\nnon_diegetic_music: {fields['music']}"

    def check(text: str) -> tuple[dict | None, list[Issue]]:
        fields = split_fields(text, ref_mode)
        if fields is None:
            return None, [Issue("error", f"Output exactly these fields in this order: {names}", "출력에서 필드를 찾지 못했습니다.")]
        return fields, lint_draft(fields, plan)

    writer = (PROMPTS / ("write_ref.system.md" if ref_mode else "write.system.md")).read_text(encoding="utf-8")
    draft = llm.chat(writer, brief, temperature=TEMP_WRITE, label="write")
    fields, issues = check(draft)
    debug["drafts"] = [draft]
    repairs = 0
    while errors(issues) and llm.remaining > 0 and repairs < 2:
        repairs += 1
        shown = show(fields) if fields else draft
        user = fill((PROMPTS / "repair.user.md").read_text(encoding="utf-8"), brief=brief, draft=shown, issues=issue_list(issues))
        draft = llm.chat((PROMPTS / "repair.system.md").read_text(encoding="utf-8"), user, temperature=TEMP_REPAIR, label="repair")
        new_fields, new_issues = check(draft)
        debug["drafts"].append(draft)
        if new_fields is None and fields is not None:
            break  # 수정본이 더 망가졌으면 이전 초안을 쓴다
        fields, issues = new_fields, new_issues
    if fields is None:
        raise PipelineError("작성 단계 출력에서 필드를 찾지 못했습니다. --verbose로 출력을 확인하세요.")
    fields["desc"] = substitute(fields["desc"], plan)
    issues = issues + lint_final(fields, opts.duration, opts.max_shots)
    prompt = assemble_ref(fields, plan) if ref_mode else assemble(fields, plan["mode"] == "i2v")
    return {"prompt": prompt, "issues": notes + issues, "calls": llm.calls, "debug": debug}


def look_images(opts, llm) -> tuple[dict | None, list[dict] | None, dict | None]:
    """모드에 따라 이미지를 읽는다. 반환: (I2V 첫 프레임 정보, Ref2VA 참조 목록, Ref2VA의 첫 프레임 정보).

    참조 모드가 아니면 참조 목록은 None이다. 참조 모드에서 첫 프레임 이미지에 피사체가 없으면 빈 목록일 수 있다."""
    if opts.refs:
        refs, opening = look_refs(opts.refs, llm, not opts.no_cache)
        if opts.verbose:
            print(json.dumps({"refs": refs, "first_frame": opening}, ensure_ascii=False, indent=2), file=sys.stderr)
        return None, refs, opening
    if opts.image and not opts.no_look:
        frame = look_frame(opts.image, llm, not opts.no_cache)
        if opts.verbose:
            print(json.dumps(frame, ensure_ascii=False, indent=2), file=sys.stderr)
        return frame, None, None
    return None, None, None


def run_single(request: str, opts, llm) -> dict:
    """LLM 한 번으로 바로 쓰는 모드. 형식 검사에 걸리면 수정 호출을 한 번 한다."""
    if opts.refs:
        raise PipelineError("--single은 참조 모드(--ref)에서 쓸 수 없습니다. 참조 모드는 정의·보존 분석을 코드가 만들어야 합니다.")
    opts.allow_speech = speech_wanted(request, opts.speech)
    header = request_header(request, opts)
    system = (PROMPTS / "single_call.system.md").read_text(encoding="utf-8")

    def check(text: str) -> tuple[dict | None, list[Issue]]:
        fields = split_fields(text)
        if fields is None:
            return None, [Issue("error", "Output exactly three fields in this order: integrated_multimodal_description:, "
                                         "overall_soundscape:, non_diegetic_music:", "세 필드를 찾지 못했습니다.")]
        fields["desc"] = re.sub(r"</d>\s*\.", "</d>", fields["desc"])
        fields["music"] = strip_mood(fields["music"])
        found = lint_final(fields, opts.duration, opts.max_shots)
        if opts.image or opts.i2v:
            fields["desc"] = normalize_labels(fields["desc"])
            first = (SHOT_MARK.split(fields["desc"])[2:3] or [fields["desc"]])[0]
            if "<Picture 1>" not in first:
                found.append(Issue("error", "Shot 1 must open on the first frame: right after the style words, name the main "
                                            "subject as \"shown in <Picture 1>\" with its starting pose and place.",
                                   "Shot 1에 첫 프레임 <Picture 1> 언급이 없습니다."))
        if not opts.allow_speech and ("<d>" in fields["desc"] or re.search(r"\(S\d+\)", fields["desc"])):
            found.append(Issue("error", "Nobody speaks in this video. Remove every <d>...</d> line and every speaker ID; "
                                        "everyone keeps their lips closed.", "대사를 요청하지 않았는데 대사가 들어갔습니다."))
        return fields, found

    draft = llm.chat(system, header, temperature=TEMP_WRITE, image=opts.image, label="single")
    fields, issues = check(draft)
    if errors(issues) and llm.remaining > 0:
        shown = assemble(fields, False) if fields else draft
        user = fill((PROMPTS / "repair.user.md").read_text(encoding="utf-8"), brief=header, draft=shown, issues=issue_list(issues))
        new_fields, new_issues = check(llm.chat((PROMPTS / "repair.system.md").read_text(encoding="utf-8"), user,
                                                temperature=TEMP_REPAIR, label="repair"))
        if new_fields is not None:
            fields, issues = new_fields, new_issues
    if fields is None:
        raise PipelineError("출력에서 세 필드를 찾지 못했습니다. --verbose로 출력을 확인하세요.")
    return {"prompt": assemble(fields, bool(opts.image or opts.i2v)), "issues": issues, "calls": llm.calls, "debug": {}}


def default_max_shots(duration: float) -> int:
    return 1 if duration <= 5 else 2 if duration <= 8 else 3 if duration <= 12 else 4


def build_parser() -> argparse.ArgumentParser:
    """CLI 인자 정의. 웹 서버도 parse_args([])로 같은 기본값을 받아 쓴다."""
    p = argparse.ArgumentParser(description="한국어 영상 아이디어를 MiniMax H3용 영어 프롬프트로 바꿉니다.")
    p.add_argument("request", nargs="?", help="한국어 요청문 (생략하면 -f 파일 또는 표준입력)")
    p.add_argument("-f", "--file", type=Path, help="요청문이 든 텍스트 파일")
    p.add_argument("-d", "--duration", type=float, default=5, help="영상 길이(초), 4~15. 기본 5")
    p.add_argument("--max-shots", type=int, help="샷 수 상한. 기본은 길이에 따라 1~4")
    p.add_argument("--dialogue-lang", default="Korean", help="대사 내용을 주지 않았을 때 모델이 지어낼 대사의 언어. 기본 Korean")
    p.add_argument("--speech", choices=["auto", "on", "off"], default="auto",
                   help="대사 허용. auto(기본)는 요청문에 따옴표나 말하다·노래 같은 표현이 있을 때만 허용")
    p.add_argument("--no-music", action="store_true", help="배경음악 없이 (non_diegetic_music: N/A)")
    p.add_argument("--style", help="화풍. anime(기본) | live | 3d | auto | 직접 쓴 영어 문구. "
                                   "이미지를 쓰는 모드에서 생략하면 이미지의 화풍을 따른다")
    p.add_argument("--image", type=Path, help="I2V: 첫 프레임 이미지. 서버를 --mmproj로 띄워야 한다")
    p.add_argument("--i2v", action="store_true", help="I2V인데 이미지를 LLM에 보여 주지 않고, 요청문에 첫 프레임을 글로 설명할 때")
    p.add_argument("--ref", action="append", default=[], metavar="[이름=]파일",
                   help=f"Ref2VA: 참조 이미지. 여러 번 쓸 수 있고(최대 {MAX_REFS}) 쓴 순서가 <Picture N> 번호다. "
                        "이름에 '첫프레임'이 들어가면 영상이 시작하는 그림으로 다룬다(Ref2VA를 I2V처럼). "
                        "이름은 요청문에서 그 대상을 부르는 말. 이름에 '배경'이 들어가면 장소, '화풍'이 들어가면 화풍 참조로 본다")
    p.add_argument("--no-look", action="store_true", help="I2V에서 이미지 읽기 단계를 건너뛰고 계획 단계에 이미지를 직접 붙인다 (LLM 1회 절약, 캐시 없음)")
    p.add_argument("--no-cache", action="store_true", help="이미지 읽기 캐시를 쓰지 않고 다시 읽는다")
    p.add_argument("--look-only", action="store_true", help="이미지를 읽은 결과(JSON)만 출력하고 끝낸다")
    p.add_argument("--single", action="store_true", help="LLM 1회로 바로 생성 (빠르지만 형식 보정이 약함. --ref와는 못 씀)")
    p.add_argument("--base-url", default=os.environ.get("H3_BASE_URL", "http://127.0.0.1:8080/v1"))
    p.add_argument("--model", default=os.environ.get("H3_MODEL", "gemma4"))
    p.add_argument("--api-key", default=os.environ.get("H3_API_KEY", ""))
    p.add_argument("--max-calls", type=int, default=4, help="LLM 호출 상한. 기본 4")
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("-o", "--output", type=Path, help="프롬프트를 저장할 파일")
    p.add_argument("--debug-json", type=Path, help="계획·브리프·초안·검사 결과를 저장할 JSON 파일")
    p.add_argument("-v", "--verbose", action="store_true", help="단계별 LLM 출력을 stderr로 보여 줌")
    return p


def finish_options(opts):
    """인자를 검사하고 파생 값(max_shots, refs ...)을 채운다. 문제가 있으면 PipelineError. CLI와 웹 서버가 같이 쓴다.

    opts.refs가 이미 있으면(웹 서버가 업로드한 파일로 만든 목록) 그것을 쓰고, 없으면 --ref 값에서 만든다."""
    if not 4 <= opts.duration <= 15:
        raise PipelineError("--duration은 4~15초여야 합니다 (H3 출력 범위).")
    opts.max_shots = max(1, min(opts.max_shots or default_max_shots(opts.duration), int(opts.duration // 2)))
    opts.max_calls = max(1, min(opts.max_calls, 4))
    opts.allow_speech = opts.speech != "off"
    if opts.image and not opts.image.is_file():
        raise PipelineError(f"이미지 파일이 없습니다: {opts.image}")
    opts.refs = getattr(opts, "refs", None) or [parse_ref(v) for v in opts.ref]
    for r in opts.refs:
        if not r["path"].is_file():
            raise PipelineError(f"참조 이미지 파일이 없습니다: {r['path']}")
    if len(opts.refs) > MAX_REFS:
        raise PipelineError(f"--ref는 최대 {MAX_REFS}개입니다.")
    if opts.refs and (opts.image or opts.i2v):
        raise PipelineError("--ref(참조 모드)와 --image/--i2v(첫 프레임 모드)는 같이 쓸 수 없습니다.")
    if opts.look_only and not (opts.refs or opts.image):
        raise PipelineError("--look-only는 --image 또는 --ref와 함께 씁니다.")
    return opts


def parse_args(argv: list[str] | None = None):
    p = build_parser()
    opts = p.parse_args(argv)
    try:
        return finish_options(opts)
    except PipelineError as e:
        p.error(str(e))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    opts = parse_args(argv)
    llm = LLMClient(opts.base_url, opts.model, opts.api_key, opts.max_calls, opts.max_tokens, verbose=opts.verbose)
    if opts.look_only:  # 모델이 이미지를 어떻게 읽는지만 본다. 틀린 곳은 출력된 cache 파일을 고치면 다음 실행에 반영된다
        opts.no_look = False
        try:
            frame, refs, opening = look_images(opts, llm)
        except PipelineError as e:
            print(f"[오류] {e}", file=sys.stderr)
            return 1
        print(json.dumps(frame or ({"refs": refs, "first_frame": opening} if opening else refs), ensure_ascii=False, indent=2))
        print(f"[LLM 호출 {llm.calls}회]", file=sys.stderr)
        return 0
    request = opts.request or (opts.file.read_text(encoding="utf-8") if opts.file else "" if sys.stdin.isatty() else sys.stdin.read())
    if not request.strip():
        print("요청문이 비었습니다. 인자, -f 파일, 표준입력 중 하나로 주세요.", file=sys.stderr)
        return 2
    try:
        result = (run_single if opts.single else run_pipeline)(request, opts, llm)
    except PipelineError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1
    for it in result["issues"]:
        print(f"[{'남은 문제' if it.sev == 'error' else '참고'}] {it.ko}", file=sys.stderr)
    print(f"[LLM 호출 {result['calls']}회]", file=sys.stderr)
    print(result["prompt"])
    if opts.output:
        opts.output.write_text(result["prompt"] + "\n", encoding="utf-8")
    if opts.debug_json:
        dump = {"request": request, "prompt": result["prompt"], "calls": result["calls"],
                "issues": [{"sev": i.sev, "ko": i.ko, "en": i.en} for i in result["issues"]], **result["debug"]}
        opts.debug_json.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    return 3 if errors(result["issues"]) else 0


if __name__ == "__main__":
    sys.exit(main())
