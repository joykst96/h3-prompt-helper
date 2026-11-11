/* h3-prompt 화면. 의존성 없음. 서버 API: GET api/health, POST api/generate (줄 단위 JSON 스트림).
   주소는 모두 상대 경로라서 프록시 뒤 하위 경로에 올려도 된다.
   사용자·모델이 쓴 글은 전부 textContent로 넣는다 (프롬프트에 <Picture 1> 같은 꺾쇠가 그대로 들어 있다). */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const MODES = {
    t2v: { name: "T2V", max: 0, hint: "글만으로 만듭니다. LLM을 2번 부릅니다." },
    i2v: {
      name: "I2V", max: 1, label: "첫 프레임 이미지",
      hint: "영상이 이 이미지에서 시작합니다. 요청문에는 그 뒤에 일어날 일을 적습니다. LLM을 3번 부르고, 같은 이미지는 다음부터 2번입니다.",
    },
    ref: {
      name: "Ref2VA", max: 9, label: "참조 이미지",
      hint: "인물·장소·화풍을 이미지로 정합니다. 이름을 붙이면 요청문에서 그 이름으로 부를 수 있습니다. " +
        "이름에 '배경'이 들어가면 장소, '화풍'이 들어가면 화풍 참조로 봅니다.",
    },
  };
  const STAGES = { look: "이미지 읽는 중", plan: "장면 계획 세우는 중", write: "프롬프트 쓰는 중", single: "프롬프트 쓰는 중", repair: "형식 고치는 중" };
  const KINDS = { character: "인물", creature: "생물", object: "사물", environment: "장소", style: "화풍" };
  const FRAME_NAME = "첫프레임"; // 서버가 이 이름의 참조를 영상의 0번 프레임으로 다룬다
  const STORE_KEY = "h3-prompt:options";

  const state = {
    mode: "t2v",
    images: { i2v: [], ref: [] }, // {file, url, name, first}
    limits: { max_refs: 9, max_upload_mb: 10, max_request_chars: 2000 },
    vision: null, // LLM 서버가 이미지를 받는지: true | false | null(모름)
    running: false,
    last: null, // 마지막 결과 {prompt, ctx}
    editors: [], // 이미지 읽기 결과 고치기: {id, picture, read(), original}
  };

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, "");
      else if (value !== false && value != null) node.setAttribute(key, value);
    }
    node.append(...children.filter((c) => c != null));
    return node;
  }

  // ---------------------------------------------------------------- 옵션 기억

  const OPTION_FIELDS = { duration: "duration", maxShots: "max-shots", style: "style", styleCustom: "style-custom", speech: "speech", dialogueLang: "dialogue-lang" };
  const OPTION_CHECKS = { noMusic: "no-music", single: "single" };

  function saveOptions() {
    const saved = { mode: state.mode };
    for (const [key, id] of Object.entries(OPTION_FIELDS)) saved[key] = $(id).value;
    for (const [key, id] of Object.entries(OPTION_CHECKS)) saved[key] = $(id).checked;
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(saved));
    } catch (e) {
      // 저장이 막혀 있어도 화면은 그대로 쓴다
    }
  }

  function loadOptions() {
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem(STORE_KEY));
    } catch (e) {
      saved = null;
    }
    if (!saved || typeof saved !== "object") return;
    for (const [key, id] of Object.entries(OPTION_FIELDS)) {
      if (typeof saved[key] !== "string") continue;
      $(id).value = saved[key];
      if ($(id).tagName === "SELECT" && $(id).selectedIndex < 0) $(id).selectedIndex = 0; // 없어진 선택지
    }
    for (const [key, id] of Object.entries(OPTION_CHECKS)) $(id).checked = saved[key] === true;
    if (MODES[saved.mode]) state.mode = saved.mode;
  }

  // ---------------------------------------------------------------- 모드와 이미지

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    $("mode-hint").textContent = MODES[mode].hint;
    $("images-field").hidden = mode === "t2v";
    $("single").disabled = mode === "ref"; // 참조 모드는 정의·보존 분석을 코드가 만들어야 해서 1회 모드가 없다
    showImagesNote("");
    renderImages();
    saveOptions();
  }

  function updateImagesMeta() {
    const mode = state.mode;
    if (mode === "t2v") return;
    const count = state.images[mode].length;
    $("images-label").textContent = MODES[mode].label;
    $("images-count").textContent = `${count} / ${MODES[mode].max}`;
    $("dropzone").hidden = mode === "ref" && count >= MODES[mode].max;
    $("dropzone").firstElementChild.textContent =
      mode === "i2v" && count ? "다른 이미지로 바꾸기" : "이미지를 끌어다 놓거나 눌러서 고르기";
    $("dropzone-note").textContent = `PNG · JPEG, 한 장에 ${state.limits.max_upload_mb}MB까지. 붙여넣기(Ctrl+V)도 됩니다`;
    $("file-input").multiple = mode === "ref";
    if (state.vision === false) showImagesNote("지금 LLM 서버는 이미지를 읽지 못합니다 (mmproj 없이 떠 있습니다).");
  }

  function renderImages() {
    updateImagesMeta();
    const mode = state.mode;
    if (mode === "t2v") return;
    $("image-list").replaceChildren(...state.images[mode].map((img, i) => imageItem(img, i, mode)));
  }

  function imageItem(img, i, mode) {
    const picture = `<Picture ${i + 1}>`;
    const head = el("div", { class: "image-head" },
      el("span", { class: "badge badge-accent", text: picture }),
      el("span", { class: "image-file", title: img.file.name, text: img.file.name }));
    const meta = el("div", { class: "image-meta" }, head);
    if (mode === "ref") {
      const box = el("input", { type: "checkbox", "data-first": String(i) });
      box.checked = img.first;
      box.addEventListener("change", () => {
        state.images.ref.forEach((other) => (other.first = false)); // 첫 프레임은 하나만
        img.first = box.checked;
        renderImages();
        const again = document.querySelector(`[data-first="${i}"]`);
        if (again) again.focus();
      });
      head.append(el("label", { class: "check" }, box, "첫 프레임"));
      const name = el("input", {
        class: "input", type: "text", maxlength: "40", "aria-label": `${picture} 이름`, disabled: img.first,
        placeholder: img.first ? "영상이 이 그림에서 시작합니다" : "이름 (예: 유나, 배경, 화풍)",
      });
      name.value = img.first ? "" : img.name;
      name.addEventListener("input", () => (img.name = name.value));
      meta.append(name);
    }
    head.append(el("button", {
      type: "button", class: "icon-btn", title: "빼기", "aria-label": `${img.file.name} 빼기`, text: "×",
      onclick: () => {
        URL.revokeObjectURL(img.url);
        state.images[mode].splice(i, 1);
        renderImages();
      },
    }));
    return el("li", { class: "image-item" }, el("img", { class: "image-thumb", src: img.url, alt: "" }), meta);
  }

  function showImagesNote(text) {
    $("images-note").textContent = text;
    $("images-note").hidden = !text;
  }

  function addFiles(files) {
    const mode = state.mode;
    if (mode === "t2v") return;
    const list = state.images[mode];
    const limit = state.limits.max_upload_mb * 1024 * 1024;
    const notes = [];
    for (const file of files) {
      if (!/^image\/(png|jpeg)$/.test(file.type)) {
        notes.push(`${file.name}: PNG 또는 JPEG만 쓸 수 있습니다.`);
      } else if (file.size > limit) {
        notes.push(`${file.name}: ${state.limits.max_upload_mb}MB를 넘습니다.`);
      } else if (mode === "i2v") {
        list.splice(0).forEach((old) => URL.revokeObjectURL(old.url)); // 한 장뿐이라 새 것으로 바꾼다
        list.push({ file, url: URL.createObjectURL(file), name: "", first: false });
        break;
      } else if (list.length >= MODES.ref.max) {
        notes.push(`참조 이미지는 ${MODES.ref.max}장까지입니다.`);
        break;
      } else {
        list.push({ file, url: URL.createObjectURL(file), name: "", first: false });
      }
    }
    showImagesNote(notes.join(" "));
    renderImages();
  }

  // ---------------------------------------------------------------- 서버 상태

  async function checkHealth() {
    const badge = $("llm-status");
    try {
      const res = await fetch("api/health", { cache: "no-store" });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      Object.assign(state.limits, data.limits);
      state.vision = data.llm.vision;
      badge.className = data.llm.reachable ? "badge badge-success" : "badge badge-danger";
      badge.textContent = !data.llm.reachable ? "LLM 연결 안 됨" : data.waiting ? `LLM 연결됨 · 대기 ${data.waiting}` : "LLM 연결됨";
    } catch (e) {
      badge.className = "badge badge-danger";
      badge.textContent = "서버 연결 안 됨";
    }
    MODES.ref.max = state.limits.max_refs;
    $("request").maxLength = state.limits.max_request_chars;
    updateRequestCount();
    updateImagesMeta();
  }

  function updateRequestCount() {
    $("request-count").textContent = `${$("request").value.length} / ${state.limits.max_request_chars}`;
  }

  // ---------------------------------------------------------------- 생성

  function show(name) {
    for (const id of ["empty", "running", "error", "done"]) $("state-" + id).hidden = id !== name;
  }

  function showFormError(text) {
    $("form-error").textContent = text;
    $("form-error").hidden = !text;
  }

  function setStage(text, fraction) {
    $("stage-text").textContent = text;
    const percent = Math.round(fraction * 100);
    $("progress-fill").style.width = percent + "%";
    $("progress").setAttribute("aria-valuenow", String(percent));
  }

  function onSubmit(event) {
    event.preventDefault();
    if (state.running) return;
    const mode = state.mode;
    const text = $("request").value.trim();
    const images = mode === "t2v" ? [] : state.images[mode];
    const duration = Number($("duration").value);
    const custom = $("style").value === "custom";
    const style = custom ? $("style-custom").value.trim() : $("style").value;
    const problem = !text ? "요청문을 적어 주세요."
      : !(duration >= 4 && duration <= 15) ? "길이는 4~15초로 적어 주세요."
      : mode === "i2v" && !images.length ? "첫 프레임 이미지를 넣어 주세요."
      : mode === "ref" && !images.length ? "참조 이미지를 한 장 이상 넣어 주세요."
      : custom && !style ? "화풍을 영어로 적거나 다른 화풍을 골라 주세요."
      : "";
    showFormError(problem);
    if (problem) return;

    const single = $("single").checked && mode !== "ref";
    const body = new FormData();
    body.append("request", text);
    body.append("mode", mode);
    body.append("duration", String(duration));
    body.append("max_shots", $("max-shots").value);
    body.append("style", style);
    body.append("speech", $("speech").value);
    body.append("dialogue_lang", $("dialogue-lang").value.trim());
    body.append("no_music", $("no-music").checked ? "1" : "");
    body.append("single", single ? "1" : "");
    body.append("no_cache", $("no-cache").checked ? "1" : "");
    for (const img of images) body.append("images", img.file, img.file.name);
    if (mode === "ref") body.append("names", JSON.stringify(images.map((img) => (img.first ? FRAME_NAME : img.name.trim()))));
    // 결과가 나왔을 때 화면이 바뀌어 있어도 보낸 내용 기준으로 안내한다
    const ctx = { mode, duration, single, files: images.map((img) => img.file.name), first: images.findIndex((img) => img.first) };
    saveOptions();
    run(body, ctx);
  }

  async function errorText(res) {
    try {
      const data = await res.json();
      return data.error || data.detail || `서버 오류 (${res.status})`;
    } catch (e) {
      return `서버 오류 (${res.status})`;
    }
  }

  async function readLines(res, onEvent) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let at;
      while ((at = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, at).trim();
        buffer = buffer.slice(at + 1);
        if (line) onEvent(JSON.parse(line));
      }
    }
  }

  async function run(body, ctx) {
    state.running = true;
    $("submit").disabled = true;
    $("submit").textContent = "생성 중";
    show("running");
    setStage("요청을 보내는 중", 0);
    const started = performance.now();
    const expected = ctx.single ? 1 : ctx.mode === "t2v" ? 2 : 3; // 수정 호출이 붙으면 더 늘어난다
    let calls = 0;
    let finished = false;
    const tick = () => {
      $("stage-note").textContent = `경과 ${Math.round((performance.now() - started) / 1000)}초 · LLM 호출 ${calls}회`;
    };
    tick();
    const timer = setInterval(tick, 1000);
    try {
      const res = await fetch("api/generate", { method: "POST", body });
      if (!res.ok) throw new Error(await errorText(res));
      await readLines(res, (ev) => {
        if (ev.event === "queued") {
          setStage("앞선 요청이 끝나기를 기다리는 중", 0);
        } else if (ev.event === "stage") {
          calls = ev.call;
          setStage(STAGES[ev.stage] || ev.stage, Math.min(0.95, (ev.call - 0.5) / Math.max(expected, ev.call)));
          tick();
        } else if (ev.event === "done") {
          finished = true;
          showResult(ev, ctx);
        } else if (ev.event === "error") {
          finished = true;
          showError(ev.message);
        }
      });
      if (!finished) throw new Error("서버와의 연결이 끊겼습니다. 다시 시도해 주세요.");
    } catch (err) {
      showError(err instanceof TypeError ? "서버에 연결할 수 없습니다. 잠시 뒤에 다시 시도해 주세요." : err.message || String(err));
    } finally {
      clearInterval(timer);
      state.running = false;
      $("submit").disabled = false;
      $("submit").textContent = "프롬프트 생성";
      checkHealth();
    }
  }

  function showError(message) {
    $("state-error").textContent = message;
    show("error");
  }

  // ---------------------------------------------------------------- 결과

  // ComfyUI 템플릿의 길이 식과 같다: 24fps 프레임 수를 17k+5 격자로 올린다 (5초 -> 124, 8초 -> 192)
  function lengthFrames(seconds) {
    const n = Math.max(5, Math.round(seconds * 24));
    return n + ((((5 - (n % 17)) % 17) + 17) % 17);
  }

  function wiring(ctx) {
    const lines = [];
    if (ctx.mode === "ref") {
      lines.push("노드: MiniMax H3 Reference to Video");
      lines.push("ref_image 연결 순서: " + ctx.files.map((name, i) => `<Picture ${i + 1}> ${name}`).join(" · "));
      if (ctx.first >= 0) {
        lines.push(`0번 프레임에 고정 (Add Guide, frame_idx 0): ${ctx.files[ctx.first]}. 프롬프트가 이 그림에서 시작한다고 전제합니다`);
      }
    } else {
      lines.push("노드: MiniMax H3 Image to Video");
      lines.push("first_frame: " + (ctx.mode === "i2v" ? ctx.files[0] : "연결하지 않음"));
    }
    lines.push("prompt: 위 프롬프트를 그대로 붙여 넣기");
    const frames = lengthFrames(ctx.duration);
    lines.push(`length: ${frames} (요청 ${ctx.duration}초, 24fps 격자에 맞춰 ${(frames / 24).toFixed(1)}초)`);
    return lines;
  }

  function frameBlock(frame) {
    const block = el("div", { class: "look-item" },
      el("div", { class: "look-head" },
        el("span", { class: "badge badge-accent", text: frame.picture || "<Picture 1>" }),
        el("span", { text: "첫 프레임" }),
        frame.cached ? el("span", { class: "badge", text: "캐시" }) : null));
    for (const [label, value] of [["화풍", frame.style], ["구도", frame.framing], ["장소", frame.setting], ["요약", frame.caption]]) {
      if (value) block.append(el("p", { text: `${label}: ${value}` }));
    }
    for (const s of frame.subjects || []) {
      block.append(el("p", { text: `${s.label} (${KINDS[s.kind] || s.kind}): ${[s.look, s.place].filter(Boolean).join(" / ")}` }));
    }
    return block;
  }

  function refBlock(ref) {
    const what = [ref.picture, KINDS[ref.kind] || ref.kind, ref.name, ref.from_frame ? "첫 프레임 안" : ""].filter(Boolean).join(" · ");
    const block = el("div", { class: "look-item" },
      el("div", { class: "look-head" },
        el("span", { class: "badge badge-accent", text: ref.label }),
        el("span", { text: what }),
        ref.cached ? el("span", { class: "badge", text: "캐시" }) : null));
    if ((ref.features || []).length) block.append(el("p", { text: ref.features.join(", ") }));
    if (ref.kind === "style" && ref.style) block.append(el("p", { text: `화풍: ${ref.style}` }));
    return block;
  }

  function showResult(ev, ctx) {
    state.last = { prompt: ev.prompt, ctx };
    $("result-meta").replaceChildren(
      el("span", { class: "badge badge-accent", text: MODES[ctx.mode].name }),
      el("span", { class: "badge", text: `${ctx.duration}초 영상` }),
      el("span", { class: "badge", text: `LLM ${ev.calls}회` }),
      el("span", { class: "badge", text: `${ev.seconds}초 걸림` }));
    $("prompt").textContent = ev.prompt;
    $("issues").replaceChildren(...(ev.issues || []).map((issue) => el("li", null,
      el("span", { class: issue.sev === "error" ? "badge badge-danger" : "badge badge-warning", text: issue.sev === "error" ? "남은 문제" : "참고" }),
      el("span", { text: issue.text }))));
    $("issues").hidden = !(ev.issues || []).length;
    $("wiring").replaceChildren(...wiring(ctx).map((line) => el("li", { text: line })));

    renderLook(ev.look);
    $("copy").textContent = "복사";
    show("done");
  }

  // ---------------------------------------------------------------- 이미지 읽기 결과 고치기

  let lookFieldId = 0;

  function control(tag, value, props) {
    const node = el(tag, { class: "input", id: `look-field-${++lookFieldId}`, ...props });
    node.value = value || "";
    return node;
  }

  function kindSelect(value, kinds) {
    const node = el("select", { class: "input", id: `look-field-${++lookFieldId}` },
      ...kinds.map((kind) => el("option", { value: kind, text: KINDS[kind] })));
    node.value = value;
    if (node.selectedIndex < 0) node.selectedIndex = 0;
    return node;
  }

  function labeled(label, node) {
    return el("div", { class: "field" }, el("label", { class: "label", for: node.id, text: label }), node);
  }

  // 이미지 한 장의 읽기 결과를 고치는 카드. 값은 서버 캐시에 든 원본이다 (화면 표시용으로 다듬은 값이 아니다)
  function lookCard(entry, look) {
    const data = entry.look;
    const frame = look.frame || look.first_frame;
    const ref = (look.refs || []).find((r) => r.picture === entry.picture && !r.from_frame);
    const cached = entry.type === "frame" ? frame && frame.cached : ref && ref.cached;
    const card = el("div", { class: "look-card" },
      el("div", { class: "look-head" },
        el("span", { class: "badge badge-accent", text: entry.picture }),
        el("span", { text: entry.type === "frame" ? "첫 프레임" : ref ? ref.label : "참조" }),
        cached ? el("span", { class: "badge", text: "캐시" }) : null));
    let read;
    if (entry.type === "frame") {
      const style = control("input", data.style, { type: "text", maxlength: "160" });
      const framing = control("input", data.framing, { type: "text", maxlength: "160" });
      const setting = control("textarea", data.setting, { maxlength: "600" });
      const caption = control("input", data.caption, { type: "text", maxlength: "300" });
      card.append(el("div", { class: "field-row" }, labeled("화풍", style), labeled("구도", framing)),
        labeled("장소", setting), labeled("요약 (첫 프레임 참조에서는 프롬프트에 그대로 들어갑니다)", caption));
      const subjects = (data.subjects || []).map((subject, i) => {
        const fields = {
          label: control("input", subject.label, { type: "text", maxlength: "80" }),
          kind: kindSelect(subject.kind, ["character", "creature", "object"]),
          look: control("textarea", subject.look, { maxlength: "400" }),
          place: control("input", subject.place, { type: "text", maxlength: "200" }),
        };
        card.append(el("div", { class: "look-group" },
          el("span", { class: "look-group-title", text: `피사체 ${i + 1}` }),
          el("div", { class: "field-row" }, labeled("이름", fields.label), labeled("종류", fields.kind)),
          labeled("외형", fields.look), labeled("위치와 자세", fields.place)));
        return fields;
      });
      read = () => ({
        style: style.value.trim(), framing: framing.value.trim(), setting: setting.value.trim(), caption: caption.value.trim(),
        subjects: subjects.map((f) => ({ label: f.label.value.trim(), kind: f.kind.value, look: f.look.value.trim(), place: f.place.value.trim() })),
      });
    } else {
      const kind = kindSelect(data.kind, ["character", "creature", "object", "environment", "style"]);
      const name = control("input", data.name, { type: "text", maxlength: "80" });
      const features = control("textarea", (data.features || []).join("\n"), { rows: String(Math.max(3, (data.features || []).length)) });
      const style = control("input", data.style, { type: "text", maxlength: "160" });
      card.append(el("div", { class: "field-row" }, labeled("종류", kind), labeled("이름", name)),
        labeled("특징 (한 줄에 하나, 1~8개. 프롬프트에 그대로 들어갑니다)", features), labeled("화풍", style));
      read = () => ({
        kind: kind.value, name: name.value.trim(), style: style.value.trim(),
        features: features.value.split("\n").map((line) => line.trim()).filter(Boolean),
      });
    }
    state.editors.push({ id: entry.id, picture: entry.picture, read, original: JSON.stringify(read()) });
    return card;
  }

  function showLookError(text) {
    $("look-error").textContent = text;
    $("look-error").hidden = !text;
  }

  function renderLook(look) {
    const body = $("look-body");
    const entries = (look && look.edit) || [];
    state.editors = [];
    body.replaceChildren();
    showLookError("");
    $("look-note").textContent = "";
    $("look").hidden = !look;
    $("look-edit").hidden = !entries.length;
    if (!look) return;
    const caution = "LLM이 이미지에서 읽은 내용입니다. 옆모습의 긴 머리를 짧다고 읽는 것처럼 틀릴 수 있습니다.";
    if (!entries.length) {
      // 서버가 읽기 결과를 저장하지 못한 경우: 고칠 대상이 없으니 보여 주기만 한다
      body.append(el("p", { class: "hint", text: caution }));
      const frame = look.frame || look.first_frame;
      if (frame) body.append(frameBlock(frame));
      for (const ref of look.refs || []) body.append(refBlock(ref));
      return;
    }
    body.append(el("p", {
      class: "hint",
      text: caution + " 틀린 곳을 영어로 고치면 같은 이미지는 다음 생성부터 고친 내용을 씁니다. 이 서버를 쓰는 모든 사람에게 적용됩니다.",
    }));
    for (const entry of entries) body.append(lookCard(entry, look));
  }

  async function applyLooks() {
    if (state.running) return;
    const changed = state.editors.filter((editor) => JSON.stringify(editor.read()) !== editor.original);
    showLookError("");
    if (!changed.length) {
      $("look-note").textContent = "고친 곳이 없습니다.";
      return;
    }
    $("look-apply").disabled = true;
    try {
      for (const editor of changed) {
        const res = await fetch("api/look/" + encodeURIComponent(editor.id), {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ look: editor.read() }),
        });
        if (!res.ok) throw new Error(`${editor.picture} ${await errorText(res)}`);
        editor.original = JSON.stringify(editor.read());
      }
    } catch (err) {
      showLookError(err instanceof TypeError ? "서버에 연결할 수 없습니다. 잠시 뒤에 다시 시도해 주세요." : err.message || String(err));
      return;
    } finally {
      $("look-apply").disabled = false;
    }
    $("no-cache").checked = false; // 다시 읽기를 켠 채로 생성하면 고친 내용이 새로 읽은 결과로 덮인다
    $("form").requestSubmit();
    if (!state.running) $("look-note").textContent = "저장했습니다. 왼쪽 입력을 확인한 뒤 다시 생성해 주세요.";
  }

  async function copyPrompt() {
    if (!state.last) return;
    let ok = false;
    try {
      await navigator.clipboard.writeText(state.last.prompt);
      ok = true;
    } catch (e) {
      // http로 연 LAN 주소처럼 클립보드 API가 막힌 곳: 글을 선택해서 옛 방식으로 복사한다
      const range = document.createRange();
      range.selectNodeContents($("prompt"));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      try {
        ok = document.execCommand("copy");
      } catch (e2) {
        ok = false;
      }
    }
    $("copy").textContent = ok ? "복사됨" : "Ctrl+C로 복사하세요";
    setTimeout(() => ($("copy").textContent = "복사"), 1500);
  }

  function savePrompt() {
    if (!state.last) return;
    const url = URL.createObjectURL(new Blob([state.last.prompt + "\n"], { type: "text/plain;charset=utf-8" }));
    const link = el("a", { href: url, download: `h3-prompt-${state.last.ctx.mode}.txt` });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---------------------------------------------------------------- 연결

  loadOptions();
  document.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  $("form").addEventListener("submit", onSubmit);
  $("form").addEventListener("change", saveOptions);
  $("request").addEventListener("input", updateRequestCount);
  $("request").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("form").requestSubmit();
  });
  const syncStyle = () => ($("style-custom-field").hidden = $("style").value !== "custom");
  $("style").addEventListener("change", syncStyle);
  syncStyle();

  const zone = $("dropzone");
  zone.addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (e) => {
    addFiles([...e.target.files]);
    e.target.value = ""; // 같은 파일을 다시 골라도 change가 나게
  });
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("is-over");
  });
  zone.addEventListener("dragleave", (e) => {
    if (!zone.contains(e.relatedTarget)) zone.classList.remove("is-over");
  });
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("is-over");
    addFiles([...e.dataTransfer.files]);
  });
  // 드롭존을 빗나간 파일을 브라우저가 열어 버려서 입력이 날아가는 것을 막는다
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());
  document.addEventListener("paste", (e) => {
    const files = [...((e.clipboardData && e.clipboardData.files) || [])].filter((f) => f.type.startsWith("image/"));
    if (state.mode === "t2v" || !files.length) return;
    e.preventDefault();
    addFiles(files);
  });

  $("copy").addEventListener("click", copyPrompt);
  $("save").addEventListener("click", savePrompt);
  $("look-apply").addEventListener("click", applyLooks);
  window.addEventListener("focus", checkHealth);

  setMode(state.mode);
  updateRequestCount();
  checkHealth();
})();
