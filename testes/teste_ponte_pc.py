"""Teste da ponte do PC (US-25): servidor na nuvem + ponte com um PC simulado, sem mexer no mouse real.

Uso: python -m testes.teste_ponte_pc
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time

import ponte_pc
from automacao.websocket import WebSocket, WebSocketError
from testes.teste_celular import PNG_1X1
from testes.teste_http import free_port, http, start, wait_ready


class PCFalso:
    def __init__(self) -> None:
        self.chamadas: list[tuple[str, dict]] = []

    def call(self, name: str, arguments: dict):
        self.chamadas.append((name, arguments))
        if name == "pc_screenshot":
            return [{"type": "image", "data": PNG_1X1, "mimeType": "image/png"}], False
        if name == "pc_type":
            return [{"type": "text", "text": f"Digitou {len(arguments['text'])} caracteres."}], False
        return [{"type": "text", "text": f"Ação inválida: {name}"}], True

    def close(self) -> None:
        pass


def main() -> int:
    failures: list[str] = []

    def check(label, ok, detail=""):
        print(("OK   " if ok else "FALHA") + f" {label} {str(detail)[:200]!r}")
        if not ok:
            failures.append(label)

    check("URL do conector vira wss /pc", ponte_pc.endereco_ponte("https://x.onrender.com/mcp/abc") ==
          "wss://x.onrender.com/pc/abc")
    try:
        ponte_pc.endereco_ponte("https://x.onrender.com/")
        check("URL sem token recusada", False)
    except ValueError:
        check("URL sem token recusada", True)

    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = start("--porta", str(port), "--headless", env={**os.environ, "MCP_TOKEN": token})
    auth = {"Authorization": f"Bearer {token}"}

    def rpc(method: str, params: dict | None = None) -> dict:
        msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return json.loads(http("POST", base + "/mcp", msg, auth)[2])["result"]

    def call(name: str, **arguments) -> dict:
        return rpc("tools/call", {"name": name, "arguments": arguments})

    def text(r: dict) -> str:
        return "\n".join(b.get("text", "") for b in r["content"])

    ws = None
    try:
        wait_ready(base, proc)
        init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}})
        check("instruções citam a ponte do PC", "pc_" in init["instructions"])
        tools = {t["name"]: t for t in rpc("tools/list")["tools"]}
        check("ferramentas pc_* listadas", {"pc_screenshot", "pc_type", "pc_key", "pc_hold_key"} <= set(tools))
        check("digitação e teclas marcadas como destrutivas (pedem aprovação)",
              all(tools[n]["annotations"]["destructiveHint"] for n in ("pc_type", "pc_key", "pc_hold_key"))
              and tools["pc_screenshot"]["annotations"]["readOnlyHint"])

        t0 = time.monotonic()
        r = call("pc_screenshot")
        check("PC desligado: aviso em até 5 s", r["isError"] and "PC não está conectado" in text(r)
              and time.monotonic() - t0 < 5, (round(time.monotonic() - t0, 1), text(r)))

        try:
            WebSocket(f"ws://127.0.0.1:{port}/pc/{'x' * 43}")
            check("token inválido na ponte: 401", False)
        except WebSocketError as exc:
            check("token inválido na ponte: 401", "401" in str(exc), exc)

        pc = PCFalso()
        ws = WebSocket(ponte_pc.endereco_ponte(f"{base}/mcp/{token}"))
        def ponte() -> None:
            try:
                ponte_pc.atender(ws, pc, mostrar=lambda _t: None)
            except (WebSocketError, OSError):
                pass  # a conexão fecha no fim do teste

        threading.Thread(target=ponte, daemon=True).start()
        time.sleep(0.5)
        r = call("pc_screenshot")
        check("captura do PC chega como imagem", not r["isError"] and r["content"][0]["type"] == "image", r["content"][0].get("type"))
        r = call("pc_type", text="olá ✓")
        check("digitação repassada ao PC", not r["isError"] and pc.chamadas[-1] == ("pc_type", {"text": "olá ✓"}), text(r))
        r = call("pc_zoom", region=[0, 0, 10, 10])
        check("erro do PC vira isError", r["isError"] and "inválida" in text(r), text(r))
        r = call("android_status")
        check("celular continua separado", "celular não está conectado" in text(r), text(r))
    finally:
        if ws:
            ws.close()
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
