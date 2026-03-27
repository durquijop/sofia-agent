FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv bash ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/pip" install --upgrade pip

WORKDIR /app

COPY requirements.txt package.json package-lock.json docker-entrypoint.sh ./
RUN pip install -r requirements.txt \
    && npm ci

COPY app ./app
COPY benchmarks ./benchmarks
COPY docs ./docs
COPY kapso-bridge ./kapso-bridge
COPY scripts ./scripts
COPY main.py nixpacks.toml README.md ./

RUN chmod +x docker-entrypoint.sh

ENV PYTHON_SERVICE_PORT=8000 \
    NODE_BRIDGE_PORT=3001 \
    INTERNAL_AGENT_API_URL=http://127.0.0.1:8000/api/v1/kapso/inbound \
    WAIT_FOR_PYTHON=true

EXPOSE 8000 3001

CMD ["bash", "docker-entrypoint.sh"]
