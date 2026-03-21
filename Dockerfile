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

COPY requirements.txt package.json package-lock.json railway-start.sh ./
RUN pip install -r requirements.txt \
    && npm ci

COPY app ./app
COPY benchmarks ./benchmarks
COPY docs ./docs
COPY kapso-bridge ./kapso-bridge
COPY scripts ./scripts
COPY main.py nixpacks.toml README.md ./

RUN chmod +x railway-start.sh

ENV PYTHON_SERVICE_PORT=8000 \
    INTERNAL_AGENT_API_URL=http://127.0.0.1:8000/api/v1/kapso/inbound

CMD ["bash", "railway-start.sh"]
