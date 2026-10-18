"""Troca o token de acesso do servidor MCP no Render e mostra a nova URL do conector.

Use se a URL do conector vazar. Roda no PC:

    $env:RENDER_API_KEY = "<chave da API do Render>"   # PowerShell; nunca cole a chave em chats
    python trocar_token.py

A chave da API do Render é criada em https://dashboard.render.com/u/settings#api-keys.

O que faz, em ordem: gera um token novo, grava em MCP_TOKEN no Render, dispara um deploy e espera
ficar no ar, confere que o token novo entra e o antigo recebe 401, e só então grava o token em
.mcp_token. Se a API do Render falhar antes do deploy, nada muda e o token antigo continua valendo;
se o deploy falhar, o valor antigo volta para MCP_TOKEN.
Depois, atualize a URL no conector do claude.ai e no app do celular.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("RENDER_API_URL", "https://api.render.com/v1").rstrip("/")
DEPLOY_OK = {"live"}
DEPLOY_FALHOU = {"build_failed", "update_failed", "canceled", "pre_deploy_failed", "deactivated"}


class ErroRender(Exception):
    pass


def render(metodo: str, caminho: str, chave: str, corpo: dict | None = None) -> dict | list:
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(API + caminho, data=dados, method=metodo, headers={
        "Authorization": f"Bearer {chave}", "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            texto = resp.read().decode() or "{}"
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode(errors="replace")[:300]
        raise ErroRender(f"API do Render: {metodo} {caminho} respondeu {exc.code}. {detalhe}") from None
    except urllib.error.URLError as exc:
        raise ErroRender(f"API do Render inacessível: {exc.reason}") from None
    return json.loads(texto)


def achar_servico(chave: str, nome: str) -> dict:
    lista = render("GET", f"/services?name={urllib.request.quote(nome)}&limit=20", chave)
    servicos = [item.get("service", item) for item in lista if isinstance(item, dict)]
    servicos = [s for s in servicos if s.get("name") == nome]
    if not servicos:
        raise ErroRender(f"Nenhum serviço chamado {nome!r} nesta conta do Render.")
    return servicos[0]


def esperar_deploy(chave: str, servico: str, deploy: str, limite: float) -> str:
    fim = time.monotonic() + limite
    status = ""
    while time.monotonic() < fim:
        status = render("GET", f"/services/{servico}/deploys/{deploy}", chave).get("status", "")
        if status in DEPLOY_OK or status in DEPLOY_FALHOU:
            return status
        time.sleep(5)
    return status or "sem resposta"


def status_mcp(url_base: str, token: str) -> int:
    corpo = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "trocar_token", "version": "1"}}})
    req = urllib.request.Request(f"{url_base}/mcp/{token}", data=corpo.encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:  # o plano gratuito pode estar acordando
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError:
        return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--servico", default="automacao-mcp", help="nome do serviço no Render (padrão: automacao-mcp)")
    p.add_argument("--arquivo-token", default=".mcp_token", help="onde guardar o token novo (padrão: .mcp_token)")
    p.add_argument("--espera", type=float, default=900, help="segundos máximos esperando o deploy (padrão: 900)")
    args = p.parse_args(argv)

    chave = os.environ.get("RENDER_API_KEY", "").strip()
    if not chave:
        print("Defina RENDER_API_KEY com uma chave criada em https://dashboard.render.com/u/settings#api-keys "
              "(PowerShell: $env:RENDER_API_KEY = \"...\"). Nada foi alterado.", file=sys.stderr)
        return 2

    try:
        servico = achar_servico(chave, args.servico)
        sid = servico["id"]
        url_base = (servico.get("serviceDetails") or {}).get("url", "").rstrip("/")
        antigo = render("GET", f"/services/{sid}/env-vars/MCP_TOKEN", chave).get("value", "")
        novo = secrets.token_urlsafe(32)
        render("PUT", f"/services/{sid}/env-vars/MCP_TOKEN", chave, {"value": novo})
    except ErroRender as exc:
        print(f"{exc}\nNada foi alterado: o token antigo continua valendo.", file=sys.stderr)
        return 1
    print("Token novo gravado no Render. Publicando (pode levar alguns minutos)...")

    try:
        deploy = render("POST", f"/services/{sid}/deploys", chave, {})
        status = esperar_deploy(chave, sid, deploy["id"], args.espera)
    except ErroRender as exc:
        status = f"erro ({exc})"
    if status not in DEPLOY_OK:
        print(f"O deploy não ficou no ar: {status}.", file=sys.stderr)
        try:
            if antigo:
                render("PUT", f"/services/{sid}/env-vars/MCP_TOKEN", chave, {"value": antigo})
                print("MCP_TOKEN voltou ao valor antigo; o token antigo continua valendo.", file=sys.stderr)
        except ErroRender as exc:
            print(f"Não foi possível devolver o valor antigo: {exc}. Confira MCP_TOKEN no painel do Render.",
                  file=sys.stderr)
        return 1

    if url_base:
        entra, recusa = status_mcp(url_base, novo), status_mcp(url_base, antigo) if antigo else 401
        print(f"Conferência: token novo -> HTTP {entra}, token antigo -> HTTP {recusa}.")
        if entra != 200 or recusa != 401:
            print("Atenção: o resultado não é o esperado (200 e 401). Confira o serviço no painel do Render.",
                  file=sys.stderr)

    destino = Path(args.arquivo_token)
    destino.write_text(novo + "\n", encoding="utf-8")
    print(f"\nToken novo guardado em {destino.resolve()}.")
    if url_base:
        print(f"Nova URL do conector: {url_base}/mcp/{novo}")
    print("Atualize agora:\n"
          "- claude.ai: Configurações > Conectores, remova o conector antigo e adicione um com a nova URL;\n"
          "- app Automação no celular: cole a nova URL e toque em Salvar URL;\n"
          "- Claude Code, se usar: claude mcp remove automacao-nuvem e adicione de novo com a nova URL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
