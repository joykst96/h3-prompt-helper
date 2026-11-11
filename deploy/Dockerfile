# h3-prompt 웹 서버 이미지. 저장소 루트를 빌드 컨텍스트로 쓴다:
#   docker build -f deploy/Dockerfile -t h3-prompt .
# LLM은 이미지에 들어 있지 않다. 실행할 때 H3_BASE_URL로 OpenAI 호환 서버를 가리킨다.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app

# 의존성 층을 먼저 만든다: 코드만 바뀌면 이 층은 다시 받지 않는다
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY h3_prompt.py server.py ./
COPY prompts ./prompts
COPY web ./web

# 이미지 읽기 캐시는 볼륨(/data/cache)에 둔다. 루트가 아닌 계정으로 돈다
RUN useradd --system --uid 10001 app && mkdir -p /data/cache && chown app /data/cache
ENV PATH="/app/.venv/bin:$PATH" H3_CACHE_DIR=/data/cache
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
