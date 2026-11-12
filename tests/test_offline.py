# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=12.3.0"]
# ///
"""LLM 없이 도는 검사: 코드가 맡은 부분(시각 계산, 토큰 치환, 형식 보정, 검사)만 확인한다.

    uv run tests/test_offline.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import h3_prompt as h  # noqa: E402

PLAN = {
    "style": "Live-action, cinematic", "setting": "A cafe by the window in the afternoon.",
    "characters": [
        {"id": "C1", "label": "the woman", "look": "a woman in her twenties in a cream sweater", "voice": "soft, calm voice"},
        {"id": "C2", "label": "the man", "look": "a man in a navy shirt", "voice": "quick, clear voice"},
        {"id": "C3", "label": "the barista", "look": "a barista in a green apron", "voice": ""},
    ],
    "shots": [
        {"seconds": 3, "framing": "medium two-shot", "camera_move": "dolly in", "camera_amplitude": "small",
         "camera_speed": "slow", "action": "She speaks.", "sounds": "cup clink",
         "lines": [{"speaker": "C1", "mode": "speech", "language": "Korean", "text": "\"나 다음 달에 이사 가...\"", "delivery": "calm"}],
         "screen_text": [{"text": "영업중", "where": "a neon sign"}]},
        {"seconds": 3, "framing": "close-up of the man", "camera_move": "static", "camera_amplitude": "medium",
         "camera_speed": "normal", "action": "He asks.", "sounds": "chair creak",
         "lines": [{"speaker": "c2", "mode": "speech", "language": "Korean", "text": "갑자기?? 어디로~", "delivery": "surprised"}],
         "screen_text": []},
    ],
    "ambience": "low cafe murmur", "music": "none",
}

# 약한 모델이 흔히 내는 흠을 일부러 넣은 초안: 마크다운, 틀린 시각, 따옴표로 감싼 토큰, <d>로 감싼 토큰, 빈 소리 필드
MESSY = """Here is the prompt:

**integrated_multimodal_description:** [Shot 1] Live-action, cinematic, a medium two-shot frames a woman in a cream sweater and a man in a navy shirt at a window table while a barista in a green apron wipes the counter behind them. A neon sign reading "{{T1}}" glows in the window. The camera pushes in with small amplitude at slow speed. The woman with a soft, calm voice (S1) says: "{D1}". She closes her lips.
[Shot 2] At 0:07, A close-up of the man shows his eyes widening as he sets his cup down. The camera holds a static shot. The man with a quick, clear voice (S2) asks: <d>[Korean] {{ D2 }}</d> He closes his mouth.

**overall_soundscape:** N/A

**non_diegetic_music:** Gentle piano.
"""


def opts(**kw):
    o = h.parse_args(["x", "-d", str(kw.pop("duration", 8))])
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def main() -> None:
    o = opts()
    plan, notes = h.normalize_plan(PLAN, o)
    s1, s2 = plan["shots"]
    assert (s1["start"], s1["end"], s2["start"], s2["end"]) == (0.0, 4.0, 4.0, 8.0), "3:3 비율이 8초로 늘어나야 한다"
    assert s1["camera"] == "The camera pushes in with small amplitude at slow speed", s1["camera"]
    assert s1["lines"][0]["text"] == "나 다음 달에 이사 가.", s1["lines"][0]["text"]
    assert s2["lines"][0]["text"] == "갑자기? 어디로.", s2["lines"][0]["text"]
    assert [c["sid"] for c in plan["characters"]] == ["S1", "S2", None]
    assert plan["music"] == ""
    brief = h.render_brief(plan, o)
    assert "[Shot 2] At 00:04.000, the camera cuts to" in brief and "silent, never speaks" in brief

    fields = h.split_fields(MESSY)
    assert fields is not None
    issues = h.lint_draft(fields, plan)
    assert not h.errors(issues), [i.en for i in h.errors(issues)]
    fields["desc"] = h.substitute(fields["desc"], plan)
    final = h.assemble(fields, False)
    assert "(S1) says: <d>[Korean] 나 다음 달에 이사 가.</d> She closes" in final, final
    assert "[Shot 2] At 00:04.000, the camera cuts to a close-up of the man" in final, final
    assert "(S2) asks: <d>[Korean] 갑자기? 어디로.</d> He closes" in final, final
    assert 'reading "영업중" glows' in final, final
    assert "overall_soundscape: Low cafe murmur. Cup clink. Chair creak." in final, final
    assert final.endswith("non_diegetic_music: N/A"), final
    assert not h.errors(h.lint_final(fields, o.duration, o.max_shots))

    # 검사가 잡아야 하는 것들
    def problems(desc: str) -> str:
        f = {"desc": desc, "sound": "Rain.", "music": "N/A"}
        return " | ".join(i.en for i in h.errors(h.lint_draft(f, plan)))

    base = h.split_fields(MESSY)["desc"]
    assert "exactly once" in problems(base.replace('"{D1}"', "나 다음 달에 이사 가")), "빠진 토큰 + 한글"
    assert "non-English" in problems(base.replace('"{D1}"', "나 다음 달에 이사 가"))
    assert "speaker ID (S2)" in problems(base.replace("(S2) ", ""))
    assert "exactly 2 shot" in problems(base.replace("[Shot 2] At 0:07,", ""))
    assert "Move the line {{D2}}" in problems(base.replace("(S1) says: \"{D1}\".", "(S1) says: {{D1}} (S2) {{D2}}").replace("<d>[Korean] {{ D2 }}</d>", ""))

    # 대사가 영상보다 길면 알려 준다
    long_plan = {**PLAN, "shots": [{**PLAN["shots"][0], "lines": [{"speaker": "C1", "mode": "voiceover", "language": "Korean",
                 "text": "가" * 60, "delivery": ""}], "screen_text": []}]}
    plan2, notes2 = h.normalize_plan(long_plan, opts(duration=5))
    assert notes2 and "대사가 길어" in notes2[0].ko
    assert "says in an off-screen voiceover: {{D1}} while" in h.render_brief(plan2, opts(duration=5))

    # 컷 시각: 항상 증가하고 마지막 샷에 1초 이상 남는다
    for planned, total in (([1, 1, 1, 1], 8), ([10, 1], 4), ([0, 0, 0], 6), ([2, 9, 2], 15)):
        cuts = h.allocate_cuts(planned, [2.0] * len(planned), total)
        assert all(b > a for a, b in zip([0] + cuts, cuts + [total])), (planned, total, cuts)
        assert total - cuts[-1] >= 1 and cuts[0] >= 1, (planned, total, cuts)

    # 단일 호출 모드의 형식 검사
    bad = {"desc": "[Shot 1] A. [Shot 2] At 5s, the camera cuts to B. The man (S1) says: <d>안녕</d>", "sound": "Rain.", "music": "N/A"}
    msgs = " | ".join(i.en for i in h.lint_final(bad, 8, 2))
    assert "cut time" in msgs and "language tag" in msgs, msgs

    test_helpers()
    test_image_payload()
    test_i2v()
    test_ref()
    test_ref_first_frame()
    print("offline tests: OK")


OPENING = {"style": "2D-animated, Japanese anime style", "framing": "wide shot from the side", "setting": "A bus stop at night in the rain.",
           "caption": "a girl in a sailor uniform sitting on a bus stop bench at night in the rain", "picture": "<Picture 1>", "n": 1,
           "subjects": []}
FRAME_REFS = [
    {"n": 1, "rid": "R1", "label": "<Subject 1>", "picture": "<Picture 1>", "kind": "character", "name": "girl",
     "features": ["a teenage girl, long black hair, a sailor uniform"], "place": "right of the frame, sitting on the bench, facing left",
     "style": "2D-animated, Japanese anime style", "note": "", "from_frame": True},
    {"n": 2, "rid": "R2", "label": "<Subject 2>", "picture": "<Picture 2>", "kind": "creature", "name": "small round yellow robot",
     "features": ["a spherical gold body", "glowing blue eyes"], "style": "2D-animated, Japanese anime style", "note": "삐삐",
     "from_frame": False},
]
FRAME_PLAN = {
    "style": "2D-animated, Japanese anime style, cool night palette", "setting": "A bus stop at night in the rain.", "characters": [],
    "shots": [{"seconds": 5, "refs": [], "framing": "wide shot from the side", "camera_move": "static", "camera_amplitude": "medium",
               "camera_speed": "normal", "action": "The girl stands up. R2 floats in from the left.", "sounds": "rain", "lines": [],
               "screen_text": []}],
    "ambience": "steady rain", "music": "none",
}
FRAME_DRAFT = """summary: <Subject 1> stands up from the bench while <Subject 2> floats in from the left side of the street.

detailed_description: [Shot 1] <Subject 1>, the teenage girl with long black hair and a sailor uniform, sits on the bench at the right of the frame, facing left, under the roof of the bus stop while rain falls on the dark street. The camera holds a static shot. She stands up and smooths her skirt as <Subject 2>, the small round yellow robot with a spherical gold body and glowing blue eyes, floats in from the left through the rain, its eyes lighting the wet pavement.

overall_soundscape: Steady rain falls on the shelter roof.

non_diegetic_music: N/A
"""


def test_ref_first_frame() -> None:
    for name, hit in (("첫프레임", True), ("첫 프레임", True), ("first_frame", True), ("유나", False), ("배경", False)):
        assert bool(h.FRAME_NOTE.search(name)) == hit, name
    o = opts(duration=5)
    plan, _ = h.normalize_plan(FRAME_PLAN, o, refs=FRAME_REFS, opening=OPENING)
    assert plan["mode"] == "ref" and [r["rid"] for r in plan["shots"][0]["refs"]] == ["R1", "R2"], "첫 프레임의 대상은 Shot 1에 들어간다"
    plan["words"] = 120
    brief = h.render_brief(plan, o)
    assert "opening words: [Shot 1] The shot begins from <Picture 1>." in brief, brief
    assert "<Subject 1> in the image: right of the frame, sitting on the bench" in brief and "(already in the first frame)" in brief
    assert "PLACE: the setting of the first frame" in brief
    assert "the image fixes the look" in brief and "long black hair" not in brief, "첫 프레임 인물의 외형은 작성 단계에 넘기지 않는다"
    header = h.request_header("소녀가 일어난다", o, None, FRAME_REFS, OPENING)
    assert "already in the first frame" in header and "First frame: the video starts from a given image" in header, header

    fields = h.split_fields(FRAME_DRAFT, ref=True)
    issues = h.lint_draft(fields, plan)
    assert not h.errors(issues), [i.en for i in h.errors(issues)]
    assert fields["desc"].startswith("[Shot 1] The shot begins from <Picture 1>. <Subject 1>,"), "빠진 첫 프레임 문장은 코드가 넣는다"
    assert fields["summary"].startswith("The video begins from <Picture 1>. <Subject 1> stands up")
    out = h.assemble_ref(fields, plan)
    for piece in (
        "<Subject 1> is the girl shown in <Picture 1>, with the face, hair and clothing exactly as in that picture.",
        "<Subject 2> is the small round yellow robot in <Picture 2>, with a spherical gold body and glowing blue eyes.",
        "<Picture 1> is the first frame of [Shot 1], showing a girl in a sailor uniform sitting on a bus stop bench at night in the rain.",
        "summary:\n[keyframe completion + reference generation] The video begins from <Picture 1>.",
        "<Subject 1> (appears in [Shot 1]): fully_preserved - the girl keeps the appearance shown in <Picture 1>.",
        "<Picture 1> ([Shot 1] first frame): fully_preserved - the video starts exactly from <Picture 1>'s framing",
        "\n[Shot 1] The shot begins from <Picture 1>. <Subject 1>, the teenage girl",
    ):
        assert piece in out, f"빠진 부분: {piece}\n---\n{out}"
    bad = h.split_fields(FRAME_DRAFT.replace("floats in from the left through", "floats out of <Picture 2> through"), ref=True)
    assert "Do not write <Picture" in " | ".join(i.en for i in h.errors(h.lint_draft(bad, plan)))

    # 첫 프레임에 피사체가 없으면(풍경) 참조 대상 없이도 참조 모드다
    plan2, _ = h.normalize_plan({**FRAME_PLAN, "shots": [{**FRAME_PLAN["shots"][0], "action": "Rain keeps falling."}]}, o,
                                refs=[], opening=OPENING)
    assert plan2["mode"] == "ref"
    f2 = {"summary": "Rain keeps falling on the empty bus stop while the street lamp flickers.", "sound": "Rain.", "music": "N/A",
          "desc": "[Shot 1] The shot begins from <Picture 1>. Rain falls on the empty bus stop."}
    assert "summary:\n[keyframe completion] " in h.assemble_ref(f2, plan2)


def test_helpers() -> None:
    assert h.parse_ref("유나=girl.png") == {"path": Path("girl.png"), "note": "유나"}
    assert h.parse_ref(r"C:\a\girl.png")["note"] == ""
    assert h.style_sentence("2D-animated, Japanese anime style, clean line art, soft cel shading") == \
        "The target video is in a 2D-animated Japanese anime style with clean line art and soft cel shading."
    assert h.style_sentence("Live-action, cinematic, warm film grain") == \
        "The target video is in a cinematic live-action style with warm film grain."
    assert h.strip_mood("Gentle, emotive solo piano at a slow tempo.") == "Gentle solo piano at a slow tempo."
    assert h.strip_mood("An emotional orchestral swell.") == "An orchestral swell."
    assert h.strip_mood("A soft and nostalgic music box melody.") == "A soft and nostalgic music box melody.", "and로 이어진 것은 그대로"
    assert h.normalize_labels("<subject 1> and Subject 2 meet <Subject3> in < Picture 1 >") == \
        "<Subject 1> and <Subject 2> meet <Subject 3> in <Picture 1>"
    o = opts()
    assert h.style_choice(o)[0] == "2D-animated, Japanese anime style", "화풍 기본값은 anime"
    assert h.style_choice(opts(style="live"))[0] == "Live-action, cinematic"
    assert h.style_choice(o, frame={"style": "watercolor, soft edges"})[0] == "watercolor, soft edges", "이미지가 있으면 이미지의 화풍"


def test_image_payload() -> None:
    """LLM에 보낼 이미지: 화소 수 상한을 넘으면 비율을 지켜 줄여 PNG로, 상한 안·0·못 여는 파일은 원본 그대로."""
    import io
    import tempfile

    from PIL import Image

    limit = h.DEFAULT_IMAGE_MAX_PIXELS

    def sent(path: Path, max_pixels: int = limit):
        mime, data = h.image_payload(path, max_pixels)
        with Image.open(io.BytesIO(data)) as im:
            return mime, im.size, im.mode

    def fits(size, ratio: float) -> bool:   # 상한을 넘지 않고, 상한에 가깝고, 비율이 그대로인가
        w, h_ = size
        return 0.99 * limit < w * h_ <= limit and abs(w / h_ - ratio) < 0.005

    with tempfile.TemporaryDirectory() as d:
        for size in ((1344, 768), (832, 1216), (1024, 1024)):   # 읽기 정확도를 확인한 크기는 손대지 않는다
            ok = Path(d) / f"ok{size[0]}.png"
            Image.new("RGB", size, (200, 80, 40)).save(ok)
            assert h.image_payload(ok, limit) == ("image/png", ok.read_bytes()), size
        wide = Path(d) / "wide.jpg"
        Image.new("RGB", (2688, 1536), (200, 80, 40)).save(wide, "JPEG")
        mime, size, mode = sent(wide)
        assert (mime, mode) == ("image/png", "RGB") and fits(size, 2688 / 1536), size
        tall = Path(d) / "tall.png"
        Image.new("RGBA", (1664, 2432), (0, 0, 0, 0)).save(tall)
        mime, size, mode = sent(tall)
        assert mode == "RGBA" and fits(size, 1664 / 2432), (size, mode)
        turned = Path(d) / "turned.jpg"   # 옆으로 누운 사진: 회전 정보(Orientation 6)를 적용한 뒤에 줄인다
        exif = Image.Exif()
        exif[0x0112] = 6
        Image.new("RGB", (2688, 1536)).save(turned, "JPEG", exif=exif)
        assert fits(sent(turned)[1], 1536 / 2688), sent(turned)
        assert sent(wide, 250_000)[1] == (661, 377), "상한을 낮추면 그만큼 줄인다"
        assert h.image_payload(wide, 0) == ("image/jpeg", wide.read_bytes()), "0이면 제한 없이 원본 그대로"
        broken = Path(d) / "broken.png"
        broken.write_bytes(b"\x89PNG\r\n\x1a\n not an image")
        assert h.image_payload(broken, limit) == ("image/png", broken.read_bytes()), "못 여는 파일은 원본 그대로"


FRAME = {"style": "2D-animated, Japanese anime style, clean line art", "framing": "wide shot from the side",
         "setting": "A bus stop at night in the rain.",
         "subjects": [{"label": "the girl", "look": "a teenage girl, long black hair, a sailor uniform",
                       "place": "right of the frame, sitting on a bench, facing left, mouth closed"}]}
I2V_PLAN = {
    "style": "2D-animated, Japanese anime style, clean line art, cool night palette", "setting": "A bus stop at night in the rain.",
    "characters": [{"id": "C1", "label": "the girl", "look": "a girl with short hair", "voice": ""}],
    "shots": [{"seconds": 5, "framing": "wide shot from the side", "camera_move": "static", "camera_amplitude": "medium",
               "camera_speed": "normal", "action": "The girl stands up and opens her umbrella.", "sounds": "rain", "lines": [],
               "screen_text": []}],
    "ambience": "steady rain", "music": "none",
}


def test_i2v() -> None:
    o = opts(duration=5, image=Path("first.png"))
    plan, _ = h.normalize_plan(I2V_PLAN, o, frame=FRAME)
    assert plan["mode"] == "i2v" and plan["characters"][0]["look"].startswith("a teenage girl, long black hair"), "외형은 이미지 값으로"
    brief = h.render_brief(plan, o)
    assert "the girl in the image: right of the frame" in brief and "already in <Picture 1>" in brief, brief
    assert "long black hair" not in brief, "첫 프레임에 있는 인물의 외형은 작성 단계에 넘기지 않는다"
    body = ("a wide shot shows the girl shown in < picture 1 > sitting on the bench under the shelter roof, keeping her appearance "
            "and the rainy street as in the image. The camera holds a static shot as she stands, lifts the red umbrella that "
            "leans beside her and opens it with a snap, the fabric spreading into a red circle against the dark blue street. "
            "She steps out from under the roof toward the wet pavement while rain keeps falling around her and ripples spread "
            "across the puddles under the street lamp.")
    good = {"desc": f"[Shot 1] {plan['style']}, {body}", "sound": "Rain.", "music": "N/A"}
    assert not h.errors(h.lint_draft(good, plan)) and "shown in <Picture 1> sitting" in good["desc"]
    bad = {"desc": good["desc"].replace(" shown in <Picture 1>", ""), "sound": "Rain.", "music": "N/A"}
    assert "<Picture 1>" in " ".join(i.en for i in h.errors(h.lint_draft(bad, plan)))
    assert h.assemble(good, True).startswith(h.I2V_LINE + "\n\nintegrated_multimodal_description: [Shot 1]")


REF_LOOKS = [
    {"n": 1, "rid": "R1", "label": "<Subject 1>", "picture": "<Picture 1>", "kind": "character", "name": "girl with silver hair",
     "features": ["short silver hair", "blue eyes", "a red jacket"], "style": "2D-animated, Japanese anime style", "note": "유나"},
    {"n": 2, "rid": "R2", "label": "<Subject 2>", "picture": "<Picture 2>", "kind": "environment", "name": "shrine stairway at sunset",
     "features": ["a red torii gate", "stone stairs"], "style": "2D-animated, Japanese anime style", "note": "배경"},
    {"n": 3, "rid": "R3", "label": "<Subject 3>", "picture": "<Picture 3>", "kind": "style", "name": "rainy night illustration",
     "features": ["thin ink lines", "a muted blue palette"], "style": "2D-animated, painterly", "note": "화풍"},
]
REF_PLAN = {
    "style": "2D-animated, painterly, thin ink lines", "setting": "The shrine stairway of R2 at sunset.",
    "characters": [{"id": "R1", "label": "the girl", "look": "", "voice": "bright young voice"},
                   {"id": "C1", "label": "the old priest", "look": "an old man in white robes", "voice": ""}],
    "shots": [
        {"seconds": 4, "refs": ["R1"], "framing": "wide shot of the stairs", "camera_move": "tracking", "camera_amplitude": "small",
         "camera_speed": "slow", "action": "R1 runs up the stairs past the old priest.", "sounds": "footsteps", "lines": [],
         "screen_text": []},
        {"seconds": 4, "refs": ["R1"], "framing": "close-up of R1", "camera_move": "static", "camera_amplitude": "medium",
         "camera_speed": "normal", "action": "R1 turns and shouts.", "sounds": "wind",
         "lines": [{"speaker": "R1", "mode": "speech", "language": "Korean", "text": "빨리 와", "delivery": "shouting"}],
         "screen_text": []},
    ],
    "ambience": "evening wind", "music": "none",
}
REF_DRAFT = """summary: [reference generation] <Subject 1> (S1) runs up the stairs and calls back from the top.

detailed_description: The video is anime. [Shot 1] A wide shot establishes <subject 2>, the shrine stairway with a red torii gate and stone stairs glowing in the low orange sun, where an old man in white robes sweeps fallen leaves beside a stone lantern. Subject 1, the girl with short silver hair, blue eyes and a red jacket, runs up the steps past him, her sneakers tapping on the stone. The camera follows the action in a tracking shot with small amplitude at slow speed while maple leaves drift across the frame.
[Shot 2] At 0:04, a close-up of <Subject 1>, the girl from Shot 1, shows her stopping at the top step and turning around with her hair swinging. The camera holds a static shot. <Subject 1> (S1) shouts in a bright young voice, {{D1}} She closes her lips into a grin and waves one arm.

overall_soundscape: Evening wind moves through the maple trees while sneakers tap on the stone steps.

non_diegetic_music: N/A
"""


def test_ref() -> None:
    o = opts(duration=8)
    plan, notes = h.normalize_plan(REF_PLAN, o, refs=REF_LOOKS)
    assert plan["mode"] == "ref" and not notes and plan["words"] == 300
    plan["words"] = 220  # 아래 시험용 초안은 짧다. 분량 검사가 아니라 라벨·조립을 본다
    s1, s2 = plan["shots"]
    assert [r["rid"] for r in s1["refs"]] == ["R1", "R2"], "장소 참조는 계획이 빠뜨려도 모든 샷에 들어간다"
    assert s1["action"].startswith("<Subject 1> runs up") and plan["setting"] == "The shrine stairway of <Subject 2> at sunset."
    assert plan["style"].startswith("2D-animated, painterly"), plan["style"]
    brief = h.render_brief(plan, o)
    assert "- <Subject 1> = the girl with silver hair: short silver hair, blue eyes, a red jacket | speaks as (S1)" in brief, brief
    assert "(place)" in brief and "OTHER CHARACTERS (no label):" in brief and "<Subject 3>" not in brief, brief
    assert "opening words: [Shot 2] At 00:04.000, the shot cuts to" in brief
    assert "Pattern: <Subject 1> (S1) + voice + verb, {{D1}}" in brief, brief

    fields = h.split_fields(REF_DRAFT.replace("while sneakers tap", "while the sneakers of <Subject 1> tap"), ref=True)
    issues = h.lint_draft(fields, plan)
    assert not h.errors(issues), [i.en for i in h.errors(issues)]
    assert "the sneakers of the girl with silver hair tap" in fields["sound"], "소리 필드의 라벨은 이름으로 바꾼다"
    assert fields["summary"].startswith("<Subject 1> runs up") and "<Subject 2>" in fields["summary"], fields["summary"]
    fields["desc"] = h.substitute(fields["desc"], plan)
    assert not h.errors(h.lint_final(fields, o.duration, o.max_shots))
    out = h.assemble_ref(fields, plan)
    for piece in (
        "subject_definitions:\n<Subject 1> is the girl with silver hair in <Picture 1>, with short silver hair, blue eyes, and a red jacket.\n"
        "<Subject 2> is the environment in <Picture 2>, a shrine stairway at sunset, featuring a red torii gate and stone stairs.\n"
        "<Subject 3> is the visual style of <Picture 3>: 2D-animated, painterly, with thin ink lines and a muted blue palette.",
        "summary:\n[reference generation] <Subject 1> runs up",
        "The whole video follows the visual style of <Subject 3>.",
        "<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - the girl with silver hair keeps short silver hair, blue eyes, and a red jacket.",
        "<Subject 3> (applies to every shot): fully_preserved",
        "detailed_description:\nThe target video follows the visual style of <Subject 3>: 2D-animated, painterly, thin ink lines.\n[Shot 1] A wide shot establishes <Subject 2>,",
        "\n[Shot 2] At 00:04.000, the shot cuts to a close-up of <Subject 1>",
        "<Subject 1> (S1) shouts in a bright young voice, <d>[Korean] 빨리 와.</d> She closes",
        "overall_soundscape:\nEvening wind", "non_diegetic_music:\nN/A",
    ):
        assert piece in out, f"빠진 부분: {piece}\n---\n{out}"

    def problems(text: str) -> str:
        return " | ".join(i.en for i in h.errors(h.lint_draft(h.split_fields(text, ref=True), plan)))

    assert "Shot 2 must name <Subject 1>" in problems(REF_DRAFT.replace("<Subject 1>, the girl from Shot 1,", "the girl").replace(
        "<Subject 1> (S1) shouts", "The girl (S1) shouts"))
    assert "write its label directly before" in problems(REF_DRAFT.replace("<Subject 1> (S1) shouts", "The girl (S1) shouts"))
    assert "Remove <Subject 7>" in problems(REF_DRAFT.replace("maple leaves drift", "<Subject 7> drifts"))
    assert "Do not write <Picture" in problems(REF_DRAFT.replace("maple leaves drift", "leaves from <Picture 2> drift"))
    assert "summary must be" in problems(REF_DRAFT.replace("runs up the stairs and calls back from the top.", "유나가 달린다."))
    assert "Delete the quoted words" in problems(REF_DRAFT.replace("{{D1}}", "\"Hurry up!\" {{D1}}"))
    assert "but the text uses it for" in problems(REF_DRAFT.replace("while maple leaves drift", "beside <Subject 1>, a quiet park, while leaves drift"))


if __name__ == "__main__":
    main()
