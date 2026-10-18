"""Teste do navegador isolado por sessão (US-30): abas separadas, limite de 2 sessões e expiração.

Sobe servidor_http.py com Chrome headless e conversa como dois clientes MCP com Mcp-Session-Id diferentes.
Uso: python -m testes.teste_sessoes
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time

from testes.teste_http import Page, QuietServer, free_port, http, start, wait_ready


def main() -> int:
    failures: list[str] = []

    def check(label, ok, detail=""):
        print(("OK   " if ok else "FALHA") + f" {label} {str(detail)[:200]!r}")
        if not ok:
            failures.append(label)

    page = QuietServer(("127.0.0.1", 0), Page)
    threading.Thread(target=page.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{page.server_address[1]}/"

    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "MCP_TOKEN": token, "AUTOMACAO_SESSAO_OCIOSA": "8"}
    proc = start("--porta", str(port), "--headless", "--permitir-rede-local", "--sem-celular", env=env)
    auth = {"Authorization": f"Bearer {token}"}

    def post(msg: dict, sid: str | None = None) -> tuple[int, dict, dict]:
        headers = {**auth, **({"Mcp-Session-Id": sid} if sid else {})}
        status, resp_headers, body = http("POST", base + "/mcp", msg, headers)
        return status, {k.lower(): v for k, v in resp_headers.items()}, json.loads(body) if body else {}

    def initialize() -> str:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
        return post(msg)[1].get("mcp-session-id", "")

    def call(sid: str, name: str, **arguments) -> dict:
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        return post(msg, sid)[2]["result"]

    def text(result: dict) -> str:
        return "\n".join(b.get("text", "") for b in result["content"])

    try:
        wait_ready(base, proc)
        a, b = initialize(), initialize()
        check("initialize devolve Mcp-Session-Id", len(a) >= 16 and len(b) >= 16 and a != b, (a, b))

        call(a, "browser_navigate", url=page_url + "?sessao=a")
        call(b, "browser_navigate", url=page_url + "?sessao=b")
        abas_a, abas_b = text(call(a, "browser_list_tabs")), text(call(b, "browser_list_tabs"))
        check("sessão A vê só as próprias abas", "sessao=a" in abas_a and "sessao=b" not in abas_a, abas_a)
        check("sessão B vê só as próprias abas", "sessao=b" in abas_b and "sessao=a" not in abas_b, abas_b)
        call(a, "browser_new_tab")
        check("aba nova de A não aparece em B", text(call(b, "browser_list_tabs")).count("tab_id") ==
              abas_b.count("tab_id"), text(call(b, "browser_list_tabs")))

        c = initialize()
        r = call(c, "browser_list_tabs")
        check("terceira sessão recebe erro de capacidade", r["isError"] and "Capacidade" in text(r), text(r))

        status = http("DELETE", base + "/mcp", None, {**auth, "Mcp-Session-Id": a})[0]
        check("DELETE encerra a sessão", status == 204, status)
        r = call(c, "browser_navigate", url=page_url + "?sessao=c")
        check("com uma vaga livre, a terceira usa o navegador", not r["isError"], text(r))

        time.sleep(14)  # B e C ficam paradas além de 8 s
        abas_b = text(call(b, "browser_list_tabs"))
        check("sessão parada tem as abas fechadas", "sessao=b" not in abas_b, abas_b)
        r = call("id-de-antes-de-reiniciar", "browser_list_tabs")
        check("id desconhecido vira sessão nova, sem erro", not r["isError"], text(r))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:  # noqa: BLE001
            proc.kill()
        log = proc.stderr.read()
    check("token fora do log", token.encode() not in log)

    print(f"\n{len(failures)} falha(s): {failures}" if failures else "\nTodos os testes passaram.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
