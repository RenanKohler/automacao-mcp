# Servidor MCP remoto só com o driver de navegador (Chromium headless), para conector do claude.ai.
# Build: docker build -t automacao-mcp .
# Run:   docker run -p 8000:8000 -e MCP_TOKEN=<token> automacao-mcp
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation fonts-noto-color-emoji ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Usuário sem privilégios; o sandbox do Chromium precisa de namespaces que containers costumam negar.
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY automacao ./automacao
COPY servidor_http.py .

ENV CHROME_PATH=/usr/bin/chromium \
    CHROME_ARGS="--no-sandbox --disable-dev-shm-usage --disable-gpu" \
    PORT=8000 \
    PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/saude', timeout=4)"
CMD ["python", "servidor_http.py", "--host", "0.0.0.0", "--modo", "browser", "--headless"]
