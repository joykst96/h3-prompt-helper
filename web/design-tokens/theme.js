/* ==========================================================================
   theme.js — 선택 사항. 테마를 기억하고 첫 화면 전에 적용한다. 의존성 없음.

   <head>에서 CSS보다 먼저, 일반 스크립트로(모듈·defer·async 아님) 불러온다.
     <script src="design-tokens/theme.js" data-key="my-app:theme"></script>
   data-key는 localStorage 키다. 앱마다 다르게 준다(없으면 "theme").

   규칙
   - 기억한 값("light"·"dark")이 있으면 <html data-theme>에 붙인다. 토큰은 data-theme을 OS보다 우선한다.
   - 없으면 아무것도 붙이지 않는다 → 토큰 기본 동작(다크, OS가 라이트면 라이트)이고 OS 변경도 따라간다.
   - localStorage가 막혀도(사생활 모드 등) 화면은 바뀐다. 기억만 안 된다.

   API: window.colorTheme
     get()       지금 보이는 테마 "light" | "dark" (data-theme, 없으면 OS)
     saved()     기억한 값 "light" | "dark" | null(OS를 따르는 중)
     set(t)      t로 바꾸고 기억한다
     toggle()    반대로 바꾸고 기억한다. 바뀐 테마를 돌려준다
     reset()     기억을 지우고 data-theme을 떼어 OS 따르기로 돌아간다
     key         쓰는 localStorage 키
   이벤트: window에 "themechange" (detail: { theme, saved }).
     set·toggle·reset 때, OS를 따르는 중에 OS 설정이 바뀔 때, 다른 탭에서 바꿨을 때 보낸다.

   버튼 연결(선택): data-theme-toggle 속성이 있는 요소를 누르면 toggle()한다.
     aria-label·title은 할 일로 맞춘다("라이트 모드로 바꾸기" / "다크 모드로 바꾸기").
     문구는 data-label-to-light / data-label-to-dark 속성으로 바꿀 수 있다.
     해/달 아이콘을 어느 쪽 보일지는 ui.css의 .theme-toggle 규칙이 CSS만으로 정한다.
   ========================================================================== */
(function () {
  "use strict";
  if (window.colorTheme) return; // 두 번 불러도 한 번만

  var script = document.currentScript;
  var KEY = (script && script.dataset.key) || "theme";
  var root = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

  function valid(v) {
    return v === "light" || v === "dark" ? v : null;
  }
  function read() {
    try {
      return valid(localStorage.getItem(KEY));
    } catch (e) {
      return null;
    }
  }
  function write(v) {
    try {
      if (v) localStorage.setItem(KEY, v);
      else localStorage.removeItem(KEY);
    } catch (e) {
      // 저장이 막혀도 지금 화면은 바뀐다
    }
  }

  var saved = read();
  // 렌더 전 적용. 기억한 값이 없으면 손대지 않는다(앱이 data-theme을 고정해 둔 경우도 그대로 둔다).
  if (saved) root.setAttribute("data-theme", saved);

  function os() {
    return mq && mq.matches ? "light" : "dark"; // 토큰 기본은 다크
  }
  function get() {
    return valid(root.getAttribute("data-theme")) || os();
  }

  function labels() {
    var t = get();
    var els = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var label = t === "dark" ? el.getAttribute("data-label-to-light") || "라이트 모드로 바꾸기" : el.getAttribute("data-label-to-dark") || "다크 모드로 바꾸기";
      el.setAttribute("aria-label", label);
      el.setAttribute("title", label);
    }
  }
  function changed() {
    labels();
    window.dispatchEvent(new CustomEvent("themechange", { detail: { theme: get(), saved: saved } }));
  }

  function set(t) {
    if (!valid(t)) throw new TypeError('colorTheme.set: "light" 또는 "dark"만 받는다: ' + t);
    saved = t;
    root.setAttribute("data-theme", t);
    write(t);
    changed();
  }
  function toggle() {
    set(get() === "dark" ? "light" : "dark");
    return saved;
  }
  function reset() {
    saved = null;
    root.removeAttribute("data-theme");
    write(null);
    changed();
  }

  // OS를 따르는 중이면 OS 변경이 곧 테마 변경이다(CSS는 저절로 바뀌고, 문구·이벤트만 맞춘다)
  if (mq) {
    var onOs = function () {
      if (!saved) changed();
    };
    if (mq.addEventListener) mq.addEventListener("change", onOs);
    else if (mq.addListener) mq.addListener(onOs);
  }
  // 다른 탭에서 바꾸면 따라간다
  window.addEventListener("storage", function (e) {
    if (e.key !== KEY && e.key !== null) return;
    saved = read();
    if (saved) root.setAttribute("data-theme", saved);
    else root.removeAttribute("data-theme");
    changed();
  });

  document.addEventListener("click", function (e) {
    var el = e.target && e.target.closest ? e.target.closest("[data-theme-toggle]") : null;
    if (el) toggle();
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", labels);
  else labels();

  window.colorTheme = { get: get, saved: function () { return saved; }, set: set, toggle: toggle, reset: reset, key: KEY };
})();
