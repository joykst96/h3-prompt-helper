# design-tokens 사본

이 폴더의 파일은 별도 저장소(`design-tokens`)의 3층 디자인 토큰을 복사한 것이다. **직접 고치지 않는다.** 이 화면에만 필요한 결정은 `../css/app.css` 맨 위의 `:root` 블록(앱 3층)에 둔다.

- 원본 커밋: `0ca47b13b72eb3b03d737b9690f27b4aa4d55f60` (2026-11-12에 가져옴)
- `tokens.css`: 3층(원시값·역할·컴포넌트)을 하나로 합친 번들. 원본의 `scripts/bundle.mjs`로 만든다
- `base.css`, `ui.css`, `theme.js`: 원본 파일 그대로 복사

원본이 있을 때의 갱신과 검사(`<design-tokens>`는 원본 저장소의 경로):

```bash
node <design-tokens>/scripts/bundle.mjs web/design-tokens/tokens.css
```

```bash
node <design-tokens>/scripts/check.mjs --tokens web/design-tokens/tokens.css web/css/app.css web/index.html web/js/app.js
```

원본이 바뀌면 색과 컨트롤 높이가 달라질 수 있으니, 갱신한 뒤에는 화면을 다크·라이트로 다시 확인하고 위의 커밋 해시를 고친다.
