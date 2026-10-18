"""Teste de trocar_token.py contra uma API do Render simulada (sem rede).

O servidor falso responde às rotas da API do Render que o script usa e também a /mcp/<token>,
aceitando só o MCP_TOKEN que está "no ar" (o do último deploy concluído).
Uso: python -m testes.teste_trocar_token
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ANTIGO = "token-antigo-" + "a" * 32


class RenderFalso(BaseHTTPRequestHandler):
    estado: dict = {}

    def log_message(self, *args) -> None:
        pass

    def _json(self, status: int, dados) -> None:
        corpo = json.dumps(dados).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self) -> None:  # noqa: N802
        e = self.estado
        if self.headers.get("Authorization") != "Bearer chave-render":
            return self._json(401, {"message": "unauthorized"})
        if self.path.startswith("/v1/services?"):
            return self._json(200, [{"service": {"id": "srv-1", "name": "automacao-mcp",
                                                 "serviceDetails": {"url": e["base"]}}}])
        if self.path == "/v1/services/srv-1/env-vars/MCP_TOKEN":
            return self._json(200, {"key": "MCP_TOKEN", "value": e["env"]})
        if self.path.startswith("/v1/services/srv-1/deploys/"):
            return self._json(200, {"id": "dep-1", "status": e["deploy_status"]})
        self._json(404, {})

    def do_PUT(self) -> None:  # noqa: N802
        e = self.estado
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        e["puts"].append(corpo["value"])
        if e.get("put_falha"):
            return self._json(500, {"message": "erro interno"})
        e["env"] = corpo["value"]
        self._json(200, {"key": "MCP_TOKEN", "value": corpo["value"]})

    def do_POST(self) -> None:  # noqa: N802
        e = self.estado
        if self.path == "/v1/services/srv-1/deploys":
            if e["deploy_status"] == "live":
                e["no_ar"] = e["env"]
            return self._json(201, {"id": "dep-1", "status": "created"})
        if self.path.startswith("/mcp/"):
            self.rfile.read(int(self.headers["Content-Length"]))
            return self._json(200 if self.path[5:] == e["no_ar"] else 401, {})
        self._json(404, {})


def main() -> int:
    failures: list[str] = []

    def check(label, ok, detail=""):
        print(("OK   " if ok else "FALHA") + f" {label} {str(detail)[:200]!r}")
        if not ok:
            failures.append(label)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RenderFalso)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    os.environ["RENDER_API_URL"] = base + "/v1"
    import trocar_token

    def rodar(**estado) -> tuple[int, str, str, Path]:
        RenderFalso.estado = {"base": base, "env": ANTIGO, "no_ar": ANTIGO, "deploy_status": "live", "puts": [],
                              **estado}
        pasta = Path(tempfile.mkdtemp())
        arquivo = pasta / ".mcp_token"
        arquivo.write_text(ANTIGO)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = trocar_token.main(["--arquivo-token", str(arquivo), "--espera", "5"])
        return code, out.getvalue(), err.getvalue(), arquivo

    os.environ.pop("RENDER_API_KEY", None)
    code, _, err, arquivo = rodar()
    check("sem chave: nada muda", code == 2 and "RENDER_API_KEY" in err and arquivo.read_text() == ANTIGO, err)

    os.environ["RENDER_API_KEY"] = "chave-render"
    code, out, err, arquivo = rodar()
    novo = arquivo.read_text().strip()
    e = RenderFalso.estado
    check("troca concluída", code == 0, err)
    check("token novo tem 32+ caracteres e foi gravado no Render", len(novo) >= 32 and e["env"] == novo and novo != ANTIGO)
    check("token novo entra e antigo recebe 401", "token novo -> HTTP 200, token antigo -> HTTP 401" in out, out)
    check("imprime a nova URL e os lembretes", f"{base}/mcp/{novo}" in out and "celular" in out and "claude.ai" in out)
    check("chave da API não aparece na saída", "chave-render" not in out + err)

    code, out, err, arquivo = rodar(put_falha=True)
    e = RenderFalso.estado
    check("falha na API: token antigo continua", code == 1 and e["env"] == ANTIGO and arquivo.read_text() == ANTIGO
          and "continua valendo" in err, err)

    code, out, err, arquivo = rodar(deploy_status="build_failed")
    e = RenderFalso.estado
    check("deploy falhou: valor antigo volta ao Render", code == 1 and e["env"] == ANTIGO and len(e["puts"]) == 2
          and arquivo.read_text() == ANTIGO, err)

    httpd.shutdown()
    print(f"\n{len(failures)} falha(s): {failures}" if failures else "\nTodos os testes passaram.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
