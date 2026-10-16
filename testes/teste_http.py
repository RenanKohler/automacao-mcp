"""Teste do servidor MCP por HTTP (servidor_http.py): autenticação, transporte e uma navegação real.

Sobe o servidor num processo separado, com token, e fala com ele por HTTP como um conector remoto.
Navegador: Chrome headless contra uma página servida localmente (com --permitir-rede-local).
Uso: python -m testes.teste_http
"""

from __future__ import annotations

import base64
import http.server as http_server
import json
import os
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = b"""<!doctype html><title>Teste MCP</title><h1>Ola MCP</h1><p>nada</p>"""
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # endereço local: sem proxy


class QuietServer(socketserver.ThreadingTCPServer):
    """Ignora conexões encerradas pelo navegador (comum no Windows ao fechar o Chrome)."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class Page(http_server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE)


def text_of(result: dict) -> str:
    return "\n".join(b.get("text", "<imagem>") for b in result["content"])


def png_size(result: dict) -> tuple[int, int] | None:
    from automacao.imagem import png_size as size

    for block in result["content"]:
        if block["type"] == "image":
            return size(base64.b64decode(block["data"]))
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body: object = None, headers: dict | None = None,
         content_type: str = "application/json") -> tuple[int, dict, bytes]:
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    req.add_header("Accept", "application/json, text/event-stream")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with OPENER.open(req, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def start(*args: str, env: dict | None = None) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(ROOT / "servidor_http.py"), *args], cwd=str(ROOT),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)


def wait_ready(base: str, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("o servidor encerrou: " + proc.stderr.read().decode("utf-8", "replace"))
        try:
            if http("GET", base + "/saude")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise TimeoutError("o servidor HTTP não respondeu")


def main() -> int:
    failures: list[str] = []

    def check(label, ok, detail=""):
        print(("OK   " if ok else "FALHA") + f" {label} {str(detail)[:200]!r}")
        if not ok:
            failures.append(label)

    page = QuietServer(("127.0.0.1", 0), Page)
    threading.Thread(target=page.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{page.server_address[1]}/"

    # Sem token, o servidor não sobe exposto.
    env = {k: v for k, v in os.environ.items() if k != "MCP_TOKEN"}
    proc = start("--host", "0.0.0.0", "--porta", str(free_port()), env=env)
    check("sem token em 0.0.0.0 recusado", proc.wait(timeout=30) != 0)
    proc = start("--porta", str(free_port()), env={**env, "MCP_TOKEN": "curto"})
    check("token curto recusado", proc.wait(timeout=30) != 0)

    token = secrets.token_urlsafe(32)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = start("--porta", str(port), "--headless", "--permitir-rede-local",
                 "--origens-permitidas", "https://claude.ai", env={**env, "MCP_TOKEN": token})
    try:
        wait_ready(base, proc)
        auth = {"Authorization": f"Bearer {token}"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}

        check("saúde sem token", http("GET", base + "/saude")[0] == 200)
        check("sem token: 401", http("POST", base + "/mcp", init)[0] == 401)
        check("token errado: 401", http("POST", base + "/mcp", init, {"Authorization": "Bearer " + "x" * 43})[0] == 401)
        check("token errado no caminho: 401", http("POST", base + "/mcp/" + "x" * 43, init)[0] == 401)
        check("caminho desconhecido: 404", http("GET", base + "/outro")[0] == 404)

        status, headers, body = http("POST", base + "/mcp", init, auth)
        result = json.loads(body)["result"]
        check("initialize com Bearer", status == 200 and result["protocolVersion"] == "2025-06-18", result.get("serverInfo"))
        check("instruções do modo navegador", "remoto" in result["instructions"] and "pc_" not in result["instructions"])
        status, _, body = http("POST", f"{base}/mcp/{token}", init)
        check("initialize com token no caminho", status == 200 and "result" in json.loads(body))

        status, _, body = http("POST", base + "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, auth)
        check("notificação: 202 sem corpo", status == 202 and body == b"", status)
        status, headers, _ = http("GET", base + "/mcp", headers=auth)
        check("GET /mcp: 405", status == 405 and headers.get("Allow") == "POST", status)
        check("Content-Type errado: 415", http("POST", base + "/mcp", b"{}", auth, "text/plain")[0] == 415)
        check("JSON inválido: 400", http("POST", base + "/mcp", b"{", auth)[0] == 400)
        check("versão desconhecida no cabeçalho aceita",
              http("POST", base + "/mcp", init, {**auth, "MCP-Protocol-Version": "2099-01-01"})[0] == 200)
        init_new = {**init, "params": {**init["params"], "protocolVersion": "2025-11-25"}}
        result = json.loads(http("POST", base + "/mcp", init_new, auth)[2])["result"]
        check("negocia 2025-11-25", result["protocolVersion"] == "2025-11-25", result["protocolVersion"])
        with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
            payload = json.dumps(init).encode()
            sock.sendall(b"POST /mcp/" + token.encode() + b" HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                         b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                         + f"{len(payload) - 5:x}\r\n".encode() + payload[:-5] + b"\r\n"
                         + b"5\r\n" + payload[-5:] + b"\r\n0\r\n\r\n")
            raw = b""
            while chunk := sock.recv(65536):
                raw += chunk
        check("corpo chunked", raw.startswith(b"HTTP/1.1 200") and b'"result"' in raw, raw[:40])
        check("HEAD /saude", http("HEAD", base + "/saude")[0] == 200)
        check("Origin não permitida: 403",
              http("POST", base + "/mcp", init, {**auth, "Origin": "https://evil.example"})[0] == 403)
        check("Origin permitida", http("POST", base + "/mcp", init, {**auth, "Origin": "https://claude.ai"})[0] == 200)

        status, _, body = http("POST", base + "/mcp", [
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/x"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        ], {**auth, "MCP-Protocol-Version": "2025-06-18"})
        replies = json.loads(body)
        check("lote: só respostas de pedidos", status == 200 and [r["id"] for r in replies] == [2, 3], status)
        tools = {t["name"] for t in replies[1]["result"]["tools"]}
        check("só ferramentas de navegador", tools and all(n.startswith("browser_") for n in tools), len(tools))

        def call(name: str, **arguments) -> dict:
            msg = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            return json.loads(http("POST", base + "/mcp", msg, auth)[2])["result"]

        r = call("browser_navigate", url=page_url)
        check("navigate", not r["isError"] and "Teste MCP" in text_of(r), text_of(r))
        r = call("browser_get_page_text")
        check("get_page_text", "Ola MCP" in text_of(r), text_of(r))
        r = call("browser_screenshot")
        check("screenshot", png_size(r) == (1280, 720), png_size(r))
        r = call("browser_navigate", url="file:///etc/hosts")
        check("política de URL", r["isError"] is True, text_of(r))
        log = b""
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log = proc.stderr.read()
        page.shutdown()
    check("token nunca aparece no log", token.encode() not in log)
    check("encerra com SIGTERM", proc.returncode is not None)

    print(f"\n{len(failures)} falha(s): {failures}" if failures else "\nTodos os testes passaram.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
