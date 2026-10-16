"""Publica o servidor MCP de navegador num Hugging Face Space (Docker) e mostra a URL do conector.

Antes, uma vez: pip install huggingface_hub  e  hf auth login  (token de escrita do Hugging Face).
Uso: python publicar_space.py [--nome automacao-mcp] [--novo-token]

O token de acesso ao MCP é gerado aqui, guardado como segredo MCP_TOKEN do Space e em .mcp_token
nesta pasta (para republicar sem trocar a URL). O código do Space fica público; o token, não.
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / ".mcp_token"
FILES = ["Dockerfile", ".dockerignore", "requirements.txt", "servidor_http.py"]

SPACE_README = """---
title: Automacao MCP
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 8000
pinned: false
---

Servidor MCP remoto (Streamable HTTP) com um navegador Chromium headless.
Acesso só com token; o endpoint é /mcp.
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--nome", default="automacao-mcp", help="nome do Space (padrão: automacao-mcp)")
    p.add_argument("--novo-token", action="store_true", help="gera um token novo (a URL do conector muda)")
    args = p.parse_args()

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("Instale antes: python -m pip install huggingface_hub", file=sys.stderr)
        return 1
    api = HfApi()
    try:
        owner = api.whoami()["name"]
    except Exception:  # noqa: BLE001 - sem login ou token inválido
        print("Faça login antes: hf auth login (com um token de escrita).", file=sys.stderr)
        return 1
    repo_id = f"{owner}/{args.nome}"

    if TOKEN_FILE.is_file() and not args.novo_token:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    else:
        token = secrets.token_urlsafe(32)
        TOKEN_FILE.write_text(token, encoding="utf-8")

    print(f"Criando ou atualizando o Space {repo_id}...")
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", private=False, exist_ok=True)
    api.add_space_secret(repo_id, "MCP_TOKEN", token)

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        for name in FILES:
            shutil.copy2(ROOT / name, stage / name)
        shutil.copytree(ROOT / "automacao", stage / "automacao",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (stage / "README.md").write_text(SPACE_README, encoding="utf-8")
        api.upload_folder(folder_path=str(stage), repo_id=repo_id, repo_type="space",
                          commit_message="Publica o servidor MCP de navegador")

    host = getattr(api.space_info(repo_id), "host", None) or \
        f"https://{owner.lower()}-{args.nome.lower()}.hf.space".replace("_", "-")
    print(f"Space: https://huggingface.co/spaces/{repo_id}")
    print("O build leva alguns minutos. Acompanhando...")
    for _ in range(90):
        runtime = getattr(api.get_space_runtime(repo_id), "stage", "")
        stage_name = str(getattr(runtime, "value", runtime))
        if stage_name in ("RUNNING", "BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR"):
            break
        time.sleep(10)
    print(f"Estado do Space: {stage_name}")
    print("\nURL do conector (trate como senha):")
    print(f"{host.rstrip('/')}/mcp/{token}")
    return 0 if stage_name == "RUNNING" else 1


if __name__ == "__main__":
    sys.exit(main())
