"""웹 서버(server.py) 테스트. LLM은 가짜로 바꿔서 서버 없이 돈다: 입력 검사, 스트림 이벤트, 응답에서 경로가 빠지는지, 임시 파일 정리.

    uv run tests/test_server.py
"""
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SCRATCH = Path(tempfile.mkdtemp(prefix="h3-test-"))
os.environ["H3_CACHE_DIR"] = str(SCRATCH / "cache")   # 프로젝트의 .cache를 건드리지 않는다 (코어를 불러오기 전에 정해야 한다)

from fastapi.testclient import TestClient  # noqa: E402

import h3_prompt as core  # noqa: E402
import server  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64          # 서버는 파일 머리만 본다. 가짜 LLM은 이미지를 풀지 않는다
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 64
STYLE = "2D-animated, Japanese anime style"
LOOK_FRAME = {"style": STYLE, "framing": "wide shot", "setting": "A bus stop at night in the rain.",
              "caption": "A girl sits on a bench at a bus stop",
              "subjects": [{"label": "the girl", "kind": "character", "look": "a girl with long black hair in a sailor uniform",
                            "place": "on the bench at the left"}]}
PLAN = {"style": STYLE, "setting": "A bus stop at night in the rain.",
        "characters": [{"id": "C1", "label": "the girl", "look": "a girl with long black hair in a sailor uniform", "voice": ""}],
        "shots": [{"seconds": 5, "framing": "wide shot", "camera_move": "static", "camera_amplitude": "small",
                   "camera_speed": "slow", "action": "She stands up and opens the umbrella.", "sounds": "rain on the roof",
                   "lines": [], "screen_text": [], "refs": ["R1"]}],
        "ambience": "steady rain", "music": "none"}
BODY = ("a wide shot shows the girl shown in <Picture 1> sitting on the bench under the bus stop roof while rain falls on the street. "
        "She stands up, lifts the red umbrella and opens it above her head. The camera holds a static shot. " * 3)
DRAFT = (f"integrated_multimodal_description: [Shot 1] {STYLE}, {BODY}\n\n"
         "overall_soundscape: Steady rain on the roof.\n\nnon_diegetic_music: N/A\n")
DRAFT_REF = ("summary: <Subject 1> stands up at the bus stop and opens an umbrella.\n\n"
             f"detailed_description: [Shot 1] The shot begins from <Picture 1>. <Subject 1> {BODY}\n\n"
             "overall_soundscape: Steady rain on the roof.\n\nnon_diegetic_music: N/A\n")
down = False   # True면 LLM 서버가 꺼진 것처럼 군다


def prompt_file(name: str) -> str:
    return (core.PROMPTS / name).read_text(encoding="utf-8")


def fake_post(self, payload: dict) -> dict:
    """시스템 프롬프트로 어느 단계인지 가려서 정해 둔 답을 돌려준다."""
    if down:
        raise urllib.error.URLError("connection refused")
    system = payload["messages"][0]["content"]
    if system == prompt_file("look_frame.system.md"):
        text = json.dumps(LOOK_FRAME)
    elif system == prompt_file("look_refs.system.md"):
        n = payload["response_format"]["json_schema"]["schema"]["properties"]["pictures"]["minItems"]
        text = json.dumps({"pictures": [{"picture": i + 1, "kind": "character", "name": "girl with silver hair",
                                         "features": ["a short silver bob", "a red jacket"], "style": STYLE} for i in range(n)]})
    elif "response_format" in payload:
        text = json.dumps(PLAN)
    elif system == prompt_file("write_ref.system.md"):
        text = DRAFT_REF
    else:
        text = DRAFT
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


core.LLMClient._post = fake_post
client = TestClient(server.app)


def generate(data: dict, files: list | None = None):
    """POST /api/generate. 반환: (상태 코드, 이벤트 목록 또는 오류 JSON, 응답 본문 글자)."""
    # files가 없어도 multipart로 보내야 브라우저의 FormData와 같다
    res = client.post("/api/generate", data=data, files=files or [("_", (None, ""))])
    if res.status_code != 200:
        return res.status_code, res.json(), res.text
    return 200, [json.loads(line) for line in res.text.splitlines() if line], res.text


def put_look(look_id: str, look) -> tuple:
    res = client.put(f"/api/look/{look_id}", json={"look": look})
    return res.status_code, res.json()


def stages(events: list) -> list:
    return [e["stage"] for e in events if e["event"] == "stage"]


def workdirs() -> list:
    return list(Path(tempfile.gettempdir()).glob("h3-web-*"))


def image(name: str, data: bytes = PNG, kind: str = "image/png"):
    return ("images", (name, data, kind))


def main() -> None:
    global down
    before = workdirs()

    # 정적 파일과 보안 헤더
    res = client.get("/")
    assert res.status_code == 200 and "H3 프롬프트" in res.text
    assert "default-src 'self'" in res.headers["content-security-policy"] and res.headers["x-content-type-options"] == "nosniff"
    for path in ("/css/app.css", "/js/app.js", "/design-tokens/tokens.css", "/design-tokens/ui.css", "/design-tokens/theme.js"):
        assert client.get(path).status_code == 200, path
    assert "<script>" not in res.text and " style=" not in res.text   # CSP가 인라인 스크립트·스타일을 막는다

    # 상태: LLM이 없으면 reachable false, 한도는 코어와 같다
    health = client.get("/api/health").json()
    assert health["ok"] and health["llm"]["reachable"] in (True, False)
    assert health["limits"]["max_refs"] == core.MAX_REFS and health["limits"]["duration"] == [4, 15]

    # T2V: 계획 -> 작성
    code, events, _ = generate({"request": "비 오는 밤 버스 정류장에서 소녀가 우산을 편다", "mode": "t2v", "duration": "5"})
    assert code == 200 and stages(events) == ["plan", "write"], events
    done = events[-1]
    assert done["event"] == "done" and done["calls"] == 2 and done["look"] is None
    assert done["prompt"].startswith("integrated_multimodal_description:") and "non_diegetic_music:" in done["prompt"]

    # I2V: 이미지 읽기 -> 계획 -> 작성. 응답에 서버 경로가 없어야 한다
    form = {"request": "소녀가 일어나 우산을 편다", "mode": "i2v", "duration": "5"}
    code, events, text = generate(form, [image("버스 정류장 bus-stop.png")])
    assert code == 200 and stages(events) == ["look", "plan", "write"], events
    done = events[-1]
    assert done["event"] == "done" and "<Picture 1>" in done["prompt"] and "is fully referenced" in done["prompt"]
    assert done["look"]["frame"]["subjects"][0]["label"] == "the girl" and done["look"]["frame"]["cached"] is False
    assert "h3-web-" not in text and str(SCRATCH) not in text and "cache" not in done["look"]["frame"]
    # 같은 이미지를 다시 올리면 캐시에서 읽는다 (이미지 읽기 호출 없음)
    code, events, _ = generate(form, [image("버스 정류장 bus-stop.png")])
    assert stages(events) == ["plan", "write"] and events[-1]["look"]["frame"]["cached"] is True

    # 이미지 읽기 결과 고치기: done 이벤트의 look.edit에 원본이 오고, 고쳐서 PUT하면 다음 생성부터 그 값을 쓴다
    entry = done["look"]["edit"][0]
    assert len(done["look"]["edit"]) == 1 and entry["type"] == "frame" and entry["picture"] == "<Picture 1>"
    assert server.LOOK_ID.fullmatch(entry["id"]) and entry["look"]["subjects"][0]["look"] == LOOK_FRAME["subjects"][0]["look"]
    fixed = json.loads(json.dumps(entry["look"]))
    fixed["subjects"][0]["look"] = "a girl with  long black hair tied back, in a sailor uniform"   # 겹친 공백은 서버가 줄인다
    fixed["caption"] = "A girl waits on a bench at a rainy bus stop"
    assert put_look(entry["id"], fixed) == (200, {"ok": True})
    code, events, _ = generate(form, [image("버스 정류장 bus-stop.png")])
    frame = events[-1]["look"]["frame"]
    assert stages(events) == ["plan", "write"] and frame["cached"] is True
    assert frame["subjects"][0]["look"] == "a girl with long black hair tied back, in a sailor uniform", frame
    assert events[-1]["look"]["edit"][0]["look"]["caption"] == "A girl waits on a bench at a rainy bus stop"
    # 잘못된 값은 저장하지 않는다
    bad_looks = [
        ({**fixed, "caption": ""}, 400, "비워"),
        ({**fixed, "setting": "비 오는 정류장"}, 400, "영어로"),
        ({**fixed, "style": "anime <Subject 9>"}, 400, "꺾쇠"),
        ({**fixed, "framing": "x" * 161}, 400, "160자"),
        ({**fixed, "subjects": [{**fixed["subjects"][0], "kind": "robot"}]}, 400, "종류"),
        ({**fixed, "subjects": [fixed["subjects"][0]] * 5}, 400, "피사체 목록"),
        ({"kind": "character", "name": "girl", "features": ["a red coat"], "style": ""}, 400, "피사체 목록"),   # 참조 모양을 첫 프레임에
        (None, 400, "고친 내용"),
    ]
    for look, status, expect in bad_looks:
        code, body = put_look(entry["id"], look)
        assert code == status and expect in body["error"], (look, code, body)
    assert put_look("nope-frame-000000000000", fixed)[0] == 404 and put_look("bad id", fixed)[0] == 404
    assert client.put(f"/api/look/{entry['id']}", content=b"not json").status_code == 400
    assert client.put(f"/api/look/{entry['id']}", content=b"x" * (server.LOOK_BODY_LIMIT + 1)).status_code == 413
    code, events, _ = generate(form, [image("버스 정류장 bus-stop.png")])
    assert events[-1]["look"]["frame"]["subjects"][0]["look"].startswith("a girl with long black hair tied back")   # 거절된 값이 안 남았다

    # "다시 읽기"를 켜면 캐시를 쓰지 않는다 (고친 내용도 새로 읽은 결과로 바뀐다)
    code, events, _ = generate({**form, "no_cache": "1"}, [image("버스 정류장 bus-stop.png")])
    assert stages(events)[0] == "look"
    assert events[-1]["look"]["frame"]["subjects"][0]["look"] == LOOK_FRAME["subjects"][0]["look"]
    # 빠른 모드: 한 번
    code, events, _ = generate({**form, "single": "1"}, [image("a.jpg", JPG, "image/jpeg")])
    assert stages(events) == ["single"] and events[-1]["event"] == "done", events

    # 참조 모드: 첫 프레임 + 보통 참조. 이름은 JSON 목록으로 이미지와 짝을 맞춘다
    ref = {"request": "유나가 일어나 우산을 편다", "mode": "ref", "duration": "5"}
    code, events, text = generate({**ref, "names": json.dumps(["첫프레임", ""])}, [image("first.png"), image("yuna.jpg", JPG, "image/jpeg")])
    assert code == 200 and events[-1]["event"] == "done", events
    done = events[-1]
    assert done["prompt"].startswith("subject_definitions:") and "<Picture 1> is the first frame of [Shot 1]" in done["prompt"]
    look = done["look"]
    assert look["first_frame"]["picture"] == "<Picture 1>" and [r["from_frame"] for r in look["refs"]] == [True, False]
    assert all("path" not in r and "cache" not in r for r in look["refs"]) and "h3-web-" not in text
    # 참조의 특징을 고치면 다음 생성의 subject_definitions에 그대로 들어간다
    assert [(e["type"], e["picture"]) for e in look["edit"]] == [("frame", "<Picture 1>"), ("ref", "<Picture 2>")]
    ref_entry = look["edit"][1]
    assert ref_entry["look"] == {"kind": "character", "name": "girl with silver hair", "style": STYLE,
                                 "features": ["a short silver bob", "a red jacket"]}
    new_ref = {**ref_entry["look"], "features": ["long silver hair", "a blue coat"]}
    assert put_look(ref_entry["id"], new_ref)[0] == 200
    for look_value, expect in (({**new_ref, "features": []}, "1~8개"), ({**new_ref, "features": ["a"] * 9}, "1~8개"),
                               ({**new_ref, "kind": "person"}, "종류"), ({**new_ref, "name": " "}, "비워")):
        code, body = put_look(ref_entry["id"], look_value)
        assert code == 400 and expect in body["error"], (look_value, body)
    code, events, _ = generate({**ref, "names": json.dumps(["첫프레임", ""])}, [image("first.png"), image("yuna.jpg", JPG, "image/jpeg")])
    done = events[-1]
    assert "look" not in stages(events) and "long silver hair and a blue coat" in done["prompt"], (stages(events), done["prompt"][:400])
    assert "a short silver bob" not in done["prompt"]

    # 입력 검사: 스트림을 열기 전에 400으로 답한다
    bad = [
        ({"request": " ", "mode": "t2v"}, None, "요청문"),
        ({"request": "x", "mode": "video"}, None, "mode"),
        ({"request": "x", "mode": "t2v", "duration": "3"}, None, "4~15"),
        ({"request": "x", "mode": "t2v", "duration": "abc"}, None, "숫자"),
        ({"request": "x", "mode": "t2v", "max_shots": "7"}, None, "샷 수"),
        ({"request": "x", "mode": "t2v", "speech": "maybe"}, None, "대사"),
        ({"request": "x" * (server.MAX_REQUEST_CHARS + 1), "mode": "t2v"}, None, "너무 깁니다"),
        ({"request": "x", "mode": "t2v"}, [image("a.png")], "넣을 수 없습니다"),
        ({"request": "x", "mode": "i2v"}, None, "한 장"),
        ({"request": "x", "mode": "i2v"}, [image("a.png"), image("b.png")], "한 장"),
        ({"request": "x", "mode": "i2v"}, [image("a.png", b"GIF89a" + b"\0" * 32)], "PNG 또는 JPEG"),
        ({"request": "x", "mode": "i2v"}, [image("a.png", b"<html>not an image</html>")], "PNG 또는 JPEG"),
        ({"request": "x", "mode": "ref"}, None, "한 장 이상"),
        ({"request": "x", "mode": "ref", "names": "[]"}, [image("a.png")], "이름 목록"),
        ({"request": "x", "mode": "ref", "names": '["a"]', "single": "1"}, [image("a.png")], "빠른 모드"),
        ({"request": "x", "mode": "ref", "names": '["첫프레임", "first frame"]'}, [image("a.png"), image("b.png")], "하나만"),
        ({"request": "x", "mode": "ref", "names": json.dumps(["n" * 41])}, [image("a.png")], "40자"),
    ]
    for data, files, expect in bad:
        code, body, _ = generate(data, files)
        assert code == 400 and expect in body["error"], (data, code, body)
    big = [image("big.png", PNG + b"\0" * server.MAX_UPLOAD)]
    code, body, _ = generate({"request": "x", "mode": "i2v"}, big)
    assert code == 413 and "MB까지" in body["error"], body
    code, body, _ = generate({"request": "x", "mode": "ref", "names": json.dumps([""] * 10)}, [image(f"{i}.png") for i in range(10)])
    assert code == 400, (code, body)   # 참조 10장: 파일 수 한도

    # LLM 서버가 꺼져 있으면 오류 이벤트로 알리고, 내부 주소는 내보내지 않는다
    down = True
    code, events, text = generate({"request": "x", "mode": "t2v"})
    down = False
    assert code == 200 and events[-1]["event"] == "error" and "연결할 수 없습니다" in events[-1]["message"], events
    assert "http" not in events[-1]["message"] and "127.0.0.1" not in text

    # 작업 폴더가 남지 않는다 (성공·검사 실패·오류 모두). 작업 스레드가 마지막 이벤트를 넣은 뒤에 지우므로 잠깐 기다린다
    import time
    for _ in range(50):
        left = [p for p in workdirs() if p not in before]
        if not left:
            break
        time.sleep(0.1)
    assert not left, left
    assert server._active == 0
    print("server tests: OK")


if __name__ == "__main__":
    main()
