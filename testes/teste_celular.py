"""Teste da ponte com o app Android: um celular simulado conecta por WebSocket e responde aos comandos.

Sobe servidor_http.py com token, conecta o "celular" em /celular/<token> e chama as ferramentas
android_* pelo MCP, como o claude.ai faria. Não precisa de navegador nem de aparelho.
Uso: python -m testes.teste_celular
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import threading
import time

from automacao.websocket import WebSocket, WebSocketClosed, WebSocketError
from testes.teste_http import free_port, http, start, wait_ready

PNG_1X1 = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")).decode()


class CelularFalso:
    """Faz o papel do app: diz olá e responde a cada comando como o app responderia."""

    def __init__(self, url: str) -> None:
        self.ws = WebSocket(url)
        self.recebidos: list[dict] = []
        self.ws.send_text(json.dumps({"tipo": "ola", "modelo": "Pixel Teste", "android": "16",
                                      "tela": [576, 1280], "versao_app": "1"}))
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        try:
            while True:
                msg = json.loads(self.ws.recv_text())
                self.recebidos.append(msg)
                acao, args = msg["acao"], msg["args"]
                if acao == "screenshot":
                    reply = {"ok": True, "imagem": {"data": PNG_1X1, "mimeType": "image/png"}, "texto": "Captura 1x1." + "." * 300_000}
                elif acao == "tap" and "texto" in args:
                    reply = {"ok": True, "texto": f"Tocou em \"{args['texto']}\"."}
                elif acao == "tap":
                    reply = {"ok": True, "texto": f"Tocou em ({args['x']}, {args['y']})."}
                elif acao == "type":
                    reply = {"ok": True, "texto": f"Escreveu {len(args['text'])} caracteres."}
                elif acao == "lento":
                    continue
                else:
                    reply = {"ok": False, "erro": f"Ação não suportada: {acao}"}
                self.ws.send_text(json.dumps({"id": msg["id"], **reply}, ensure_ascii=False))
        except (WebSocketClosed, WebSocketError, OSError):
            pass

    def close(self) -> None:
        self.ws.close()


def reconexao(check) -> None:
    """Servidor recém-acordado: a chamada espera o celular reconectar em vez de falhar na hora."""
    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "MCP_TOKEN": token, "AUTOMACAO_JANELA_RECONEXAO": "120", "AUTOMACAO_ESPERA_RECONEXAO": "4"}
    proc = start("--porta", str(port), "--headless", env=env)
    auth = {"Authorization": f"Bearer {token}"}

    def call(name: str, **arguments) -> dict:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        return json.loads(http("POST", base + "/mcp", msg, auth)[2])["result"]

    celular = None
    try:
        wait_ready(base, proc)
        t0 = time.monotonic()
        r = call("android_tap", x=1, y=2)
        texto = r["content"][0]["text"]
        check("acordando sem celular: espera e explica", r["isError"] and "ainda não reconectou" in texto
              and 3.5 < time.monotonic() - t0 < 10, texto)
        resultado: dict = {}
        worker = threading.Thread(target=lambda: resultado.update(call("android_tap", x=7, y=8)))
        worker.start()
        time.sleep(1)
        celular = CelularFalso(f"ws://127.0.0.1:{port}/celular/{token}")
        worker.join(15)
        check("celular que reconecta durante a espera recebe o comando",
              resultado and not resultado["isError"] and "(7, 8)" in resultado["content"][0]["text"], resultado)
    finally:
        if celular:
            celular.close()
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:  # noqa: BLE001
            proc.kill()


def main() -> int:
    failures: list[str] = []

    def check(label, ok, detail=""):
        print(("OK   " if ok else "FALHA") + f" {label} {str(detail)[:200]!r}")
        if not ok:
            failures.append(label)

    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "MCP_TOKEN": token, "AUTOMACAO_JANELA_RECONEXAO": "0"}
    proc = start("--porta", str(port), "--headless", env=env)
    auth = {"Authorization": f"Bearer {token}"}

    def rpc(method: str, params: dict | None = None) -> dict:
        msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return json.loads(http("POST", base + "/mcp", msg, auth)[2])["result"]

    def call(name: str, **arguments) -> dict:
        return rpc("tools/call", {"name": name, "arguments": arguments})

    def text_of(result: dict) -> str:
        return "\n".join(b.get("text", "<imagem>") for b in result["content"])

    celular = None
    try:
        wait_ready(base, proc)
        init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}})
        check("instruções citam o celular", "android_" in init["instructions"])
        tools = {t["name"] for t in rpc("tools/list")["tools"]}
        check("ferramentas android_* listadas", {"android_tap", "android_ui", "android_screenshot"} <= tools, len(tools))
        check("navegador continua", "browser_navigate" in tools)
        schemas = {t["name"]: t["inputSchema"] for t in rpc("tools/list")["tools"]}
        check("tap aceita texto, id e app sem x/y obrigatórios",
              {"texto", "id", "app", "x", "y"} <= set(schemas["android_tap"]["properties"])
              and not schemas["android_tap"].get("required"), schemas["android_tap"])
        check("type aceita app e ponto do campo", {"app", "x", "y", "id"} <= set(schemas["android_type"]["properties"]))

        r = call("android_status")
        check("primeira chamada avisa que o servidor acabou de iniciar", "acabou de iniciar" in text_of(r), text_of(r))
        r = call("android_status")
        check("só a primeira chamada avisa", "acabou de iniciar" not in text_of(r), text_of(r))
        r = call("android_tap", x=1, y=2)
        check("sem celular: erro claro", r["isError"] and "não está conectado" in text_of(r), text_of(r))
        r = call("android_status")
        check("status sem celular", "não está conectado" in text_of(r))

        try:
            WebSocket(f"ws://127.0.0.1:{port}/celular/{'x' * 43}")
            check("token errado no /celular recusado", False)
        except WebSocketError as exc:
            check("token errado no /celular recusado", "401" in str(exc), exc)
        check("POST /celular: 405", http("POST", f"{base}/celular/{token}", {}, auth)[0] == 405)
        check("GET /celular sem upgrade: 426", http("GET", f"{base}/celular/{token}")[0] == 426)

        celular = CelularFalso(f"ws://127.0.0.1:{port}/celular/{token}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "Pixel" not in text_of(call("android_status")):
            time.sleep(0.1)
        r = call("android_status")
        check("status com celular", "Pixel Teste" in text_of(r) and "Android 16" in text_of(r), text_of(r))
        r = call("android_tap", x=10, y=20)
        check("tap repassado", not r["isError"] and "(10, 20)" in text_of(r), text_of(r))
        check("argumentos chegam ao celular", celular.recebidos[-1] == {"id": celular.recebidos[-1]["id"],
                                                                         "acao": "tap", "args": {"x": 10, "y": 20}})
        r = call("android_tap", texto="Enviar", app="com.whatsapp")
        check("tap por texto", not r["isError"] and '"Enviar"' in text_of(r), text_of(r))
        check("texto e app chegam ao celular", celular.recebidos[-1]["args"] == {"texto": "Enviar", "app": "com.whatsapp"},
              celular.recebidos[-1])
        r = call("android_type", text="ação ç ✓")
        check("texto com acento", not r["isError"] and "8 caracteres" in text_of(r), text_of(r))
        r = call("android_screenshot")
        check("captura vira imagem MCP", r["content"][0]["type"] == "image"
              and r["content"][0]["mimeType"] == "image/png" and len(text_of(r)) > 300_000, r["content"][0].get("type"))
        r = call("android_key", key="home")
        check("erro do celular vira isError", r["isError"] and "não suportada" in text_of(r), text_of(r))
        r = call("android_wait", seconds=0.2)
        check("wait no servidor", not r["isError"])

        # Chamadas em paralelo casam cada resposta com o seu pedido.
        results: dict[int, str] = {}

        def worker(i: int) -> None:
            results[i] = text_of(call("android_tap", x=i, y=i))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("paralelo", all(f"({i}, {i})" in results[i] for i in range(8)), results)

        # Um segundo celular substitui o primeiro.
        segundo = CelularFalso(f"ws://127.0.0.1:{port}/celular/{token}")
        time.sleep(0.5)
        r = call("android_tap", x=3, y=4)
        check("segundo celular assume", not r["isError"] and segundo.recebidos, text_of(r))
        segundo.close()
        time.sleep(0.5)
        r = call("android_tap", x=5, y=6)
        check("desconectado: erro claro", r["isError"] and "não está conectado" in text_of(r), text_of(r))
    finally:
        if celular:
            celular.close()
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except Exception:  # noqa: BLE001
            proc.kill()
        log = proc.stderr.read()
    check("token fora do log", token.encode() not in log)
    reconexao(check)

    print(f"\n{len(failures)} falha(s): {failures}" if failures else "\nTodos os testes passaram.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
