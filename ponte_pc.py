"""Ponte do PC: deixa o claude.ai operar este PC (ferramentas pc_*) pelo conector da nuvem.

Roda no PC, na pasta automacao-pc-browser, e só funciona enquanto esta janela estiver aberta:

    .venv\\Scripts\\python ponte_pc.py

A ponte abre uma conexão de saída para o servidor (wss://<servidor>/pc/<token>), sem abrir portas no
roteador, e executa no PC os comandos que chegam por ela: captura de tela, mouse e teclado. Feche a
janela (ou Ctrl+C) para desligar o acesso remoto. O token vem de --url (a URL completa do conector), da
variável AUTOMACAO_CONECTOR_URL ou do arquivo nuvem\\.mcp_token; ele nunca é impresso.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from automacao.websocket import WebSocket, WebSocketClosed, WebSocketError

SERVIDOR_PADRAO = "https://automacao-mcp.onrender.com"
LEITURA_S = 90  # o servidor manda ping a cada 20 s; sem nada nesse tempo, a conexão caiu


def endereco_ponte(url_conector: str) -> str:
    """https://host/mcp/<token> -> wss://host/pc/<token> (http vira ws, para testes locais)."""
    partes = urlsplit(url_conector.strip())
    caminho = partes.path.rstrip("/")
    if partes.scheme not in ("https", "http") or not caminho.startswith("/mcp/") or len(caminho) <= 5:
        raise ValueError("Use a URL completa do conector, no formato https://<servidor>/mcp/<token>.")
    esquema = "wss" if partes.scheme == "https" else "ws"
    return f"{esquema}://{partes.netloc}/pc/{caminho[5:]}"


def url_do_conector(args: argparse.Namespace) -> str:
    if args.url:
        return args.url
    if os.environ.get("AUTOMACAO_CONECTOR_URL"):
        return os.environ["AUTOMACAO_CONECTOR_URL"]
    for arquivo in (args.token_arquivo, "nuvem/.mcp_token", ".mcp_token"):
        if arquivo and Path(arquivo).is_file():
            token = Path(arquivo).read_text(encoding="utf-8").strip()
            return f"{args.servidor.rstrip('/')}/mcp/{token}"
    raise SystemExit("Não achei o token: passe --url com a URL do conector ou rode na pasta que tem nuvem\\.mcp_token.")


def ferramentas_do_pc(tela_max: str) -> Any:
    from automacao.mcp_servidor import Ferramentas, build_parser

    return Ferramentas(build_parser().parse_args(["--modo", "pc", "--tela-max", tela_max]))


def atender(ws: WebSocket, ferramentas: Any, mostrar=print) -> None:
    """Responde aos comandos até a conexão cair."""
    ws.send_text(json.dumps({"tipo": "ola", "modelo": platform.node(),
                             "sistema": f"{platform.system()} {platform.release()}"}, ensure_ascii=False))
    while True:
        msg = json.loads(ws.recv_text())
        if not isinstance(msg, dict) or "id" not in msg:
            continue
        nome = "pc_" + str(msg.get("acao", ""))
        mostrar(f"{time.strftime('%H:%M:%S')} {nome}")
        try:
            conteudo, erro = ferramentas.call(nome, msg.get("args") or {})
        except Exception as exc:  # noqa: BLE001 - o erro volta para o Claude, a ponte continua
            conteudo, erro = [{"type": "text", "text": f"Falha no PC: {exc}"}], True
        ws.send_text(json.dumps({"id": msg["id"], "ok": not erro, "conteudo": conteudo}, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", help="URL completa do conector (https://<servidor>/mcp/<token>)")
    p.add_argument("--servidor", default=SERVIDOR_PADRAO, help=f"servidor, quando o token vem de arquivo (padrão: {SERVIDOR_PADRAO})")
    p.add_argument("--token-arquivo", help="arquivo com o token (padrão: nuvem\\.mcp_token)")
    p.add_argument("--tela-max", default="1280x720", help="tamanho máximo das capturas (padrão: 1280x720)")
    args = p.parse_args(argv)

    try:
        endereco = endereco_ponte(url_do_conector(args))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    host = urlsplit(endereco).hostname
    ferramentas = ferramentas_do_pc(args.tela_max)
    print(f"Ponte do PC ligada para {host}. Enquanto esta janela estiver aberta, o claude.ai pode ver a tela e "
          "usar o mouse e o teclado deste PC. Ctrl+C desliga.")
    espera = 2.0
    try:
        while True:
            try:
                ws = WebSocket(endereco, timeout=30)
                ws._sock.settimeout(LEITURA_S)
                print("Conectado ao servidor.")
                espera = 2.0
                atender(ws, ferramentas)
            except (WebSocketClosed, WebSocketError, OSError, json.JSONDecodeError) as exc:
                motivo = "token recusado (401)" if "401" in str(exc) else type(exc).__name__
                print(f"Sem conexão ({motivo}). Tentando de novo em {espera:.0f} s.")
            time.sleep(espera)
            espera = min(espera * 2, 30.0)
    except KeyboardInterrupt:
        print("\nPonte desligada: o PC não está mais acessível pelo claude.ai.")
    finally:
        ferramentas.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
