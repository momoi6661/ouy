# Ouy：后端（Python）+ 欧易官方 Trade CLI / MCP（Node）。前端用仓库里已构建好的 web/dist。
FROM node:20-bookworm-slim AS okx
WORKDIR /okx
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.12-slim-bookworm
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=okx /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements.lock.txt pyproject.toml README.md ./
RUN pip install --timeout 120 --retries 8 --index-url "$PIP_INDEX_URL" --extra-index-url https://pypi.org/simple -r requirements.lock.txt
COPY ouy ./ouy
RUN pip install --timeout 120 --retries 8 --index-url "$PIP_INDEX_URL" --extra-index-url https://pypi.org/simple --no-deps .
COPY --from=okx /okx/node_modules ./node_modules
COPY package.json ./
COPY knowledge ./knowledge
COPY web/dist ./web/dist

# 容器内监听 0.0.0.0；compose 只把端口发布到宿主机的 127.0.0.1
ENV OUY_BIND=0.0.0.0 OUY_PORT=8899 OUY_DATA_DIR=/app/data
EXPOSE 8899
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8899/api/health', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "ouy"]
