"""Ponte entre o servidor MCP e o app Android que roda no celular (driver nativo, sem PC).

O celular não aceita conexões de fora, então é o app que abre um WebSocket para o servidor
(GET /celular/<token>) e fica esperando comandos. Cada ferramenta android_* vira uma mensagem:

    servidor -> celular  {"id": 7, "acao": "tap", "args": {"x": 100, "y": 200}}
    celular -> servidor  {"id": 7, "ok": true, "texto": "...", "imagem": {"data": "...", "mimeType": "image/jpeg"}}
                         {"id": 7, "ok": false, "erro": "..."}
    celular -> servidor  {"tipo": "ola", "modelo": "...", "android": "14", "tela": [576, 1280]}  (ao conectar)

O lado servidor do WebSocket (RFC 6455) é implementado aqui sobre o socket da requisição HTTP.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
from typing import Any, BinaryIO

log = logging.getLogger("automacao.celular")

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 16 * 1024 * 1024
PING_INTERVAL = 20.0
READ_TIMEOUT = 75.0  # sem nenhum frame (nem pong) nesse tempo, a conexão é dada como perdida
CALL_TIMEOUT = 45.0
# Logo depois de o servidor subir (acordando da hibernação), espera o celular reconectar antes de desistir.
JANELA_RECONEXAO = float(os.environ.get("AUTOMACAO_JANELA_RECONEXAO", "120"))
ESPERA_RECONEXAO = float(os.environ.get("AUTOMACAO_ESPERA_RECONEXAO", "35"))

NAO_CONECTADO = ("O celular não está conectado. Abra o app Automação no celular, confira se o serviço de "
                 "acessibilidade está ligado e se o endereço do servidor está correto.")

_XY = {"type": "integer", "description": "Pixels da última android_screenshot ou android_ui."}
_APP = {"type": "string", "description":
        "Pacote do app que deve estar na frente (ex.: com.whatsapp). Se outro app, como o Claude, tiver tomado "
        "a frente, este é trazido de volta antes da ação. Depois de android_open_app não precisa repetir."}

TOOLS: list[dict[str, Any]] = [
    {"name": "android_status", "read_only": True, "schema": {"type": "object", "properties": {}},
     "description": "Mostra se o celular Android está conectado ao servidor, com modelo, versão e tamanho das capturas."},
    {"name": "android_screenshot", "read_only": True, "schema": {"type": "object", "properties": {"app": _APP}},
     "description": "Captura a tela do celular. As coordenadas das outras ferramentas android_* são pixels desta captura."},
    {"name": "android_ui", "read_only": True, "schema": {"type": "object", "properties": {"app": _APP}},
     "description": "Lê os elementos da tela atual do celular (textos, botões, campos, ids) com o centro de cada um em "
                    "pixels da captura. Prefira esta ferramenta a capturas."},
    {"name": "android_tap", "read_only": False,
     "schema": {"type": "object", "properties": {
         "texto": {"type": "string", "description": "Texto ou descrição do elemento (ex.: \"Enviar\", \"Maria\"). "
                                                    "Igual vence parcial; se houver mais de um, nada é tocado."},
         "id": {"type": "string", "description": "id do elemento, como aparece em android_ui."},
         "x": _XY, "y": _XY, "app": _APP}},
     "description": "Toca num elemento do celular pelo texto ou id (preferível: funciona mesmo se a tela se mexer) "
                    "ou no ponto (x, y)."},
    {"name": "android_long_press", "read_only": False,
     "schema": {"type": "object", "properties": {"x": _XY, "y": _XY, "app": _APP,
                                                 "duration_ms": {"type": "integer", "description": "Duração em ms (padrão 800)."}},
                "required": ["x", "y"]},
     "description": "Toque longo no ponto (x, y) da tela do celular."},
    {"name": "android_swipe", "read_only": False,
     "schema": {"type": "object", "properties": {
         "x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"}, "y2": {"type": "integer"},
         "duration_ms": {"type": "integer", "description": "Duração em ms (padrão 300). Maior = rolagem mais lenta."},
         "app": _APP},
         "required": ["x1", "y1", "x2", "y2"]},
     "description": "Desliza o dedo de (x1, y1) até (x2, y2), em pixels da captura. Para rolar a lista para baixo, "
                    "deslize de baixo para cima."},
    {"name": "android_type", "read_only": False,
     "schema": {"type": "object", "properties": {
         "text": {"type": "string"},
         "x": {"type": "integer", "description": "Opcional: ponto do campo, quando a tela tem mais de um."},
         "y": {"type": "integer", "description": "Opcional: ponto do campo, quando a tela tem mais de um."},
         "id": {"type": "string", "description": "Opcional: id do campo, como aparece em android_ui."},
         "app": _APP},
         "required": ["text"]},
     "description": "Escreve texto num campo do celular, sem usar o teclado: o campo em foco ou, se nenhum, o único "
                    "campo de texto da tela (ex.: \"Mensagem\" no WhatsApp). Não precisa tocar no campo antes. "
                    "Acrescenta ao que já estiver escrito. Aceita acentos e emojis. Não envia: em apps de mensagem, "
                    "toque depois no botão de enviar (android_tap com texto \"Enviar\")."},
    {"name": "android_key", "read_only": False,
     "schema": {"type": "object", "properties": {"key": {"type": "string", "description":
         "back, home, recents, notifications, quick_settings, enter, power_dialog ou lock_screen."}, "app": _APP},
         "required": ["key"]},
     "description": "Aciona um botão do sistema do celular (voltar, início, recentes, notificações...)."},
    {"name": "android_open_app", "read_only": False,
     "schema": {"type": "object", "properties": {"package": {"type": "string", "description": "Ex.: com.android.chrome"}},
                "required": ["package"]},
     "description": "Abre um app do celular pelo nome do pacote (veja android_list_apps). Ele passa a ser o app operado: "
                    "as ações seguintes o trazem de volta se outro app tomar a frente."},
    {"name": "android_list_apps", "read_only": True,
     "schema": {"type": "object", "properties": {"filter": {"type": "string", "description": "Trecho do nome ou do pacote."}}},
     "description": "Lista os apps do celular que podem ser abertos, com nome e pacote."},
    {"name": "android_wait", "read_only": True,
     "schema": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]},
     "description": "Aguarda alguns segundos (até 30) para a tela do celular carregar."},
]


class CelularError(Exception):
    pass


class ConexaoFechada(Exception):
    pass


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")


class ConexaoWS:
    """Lado servidor de um WebSocket já aceito (frames do cliente chegam mascarados)."""

    def __init__(self, sock: socket.socket, rfile: BinaryIO, wfile: BinaryIO) -> None:
        self.sock = sock
        self.rfile = rfile
        self.wfile = wfile
        self._write_lock = threading.Lock()
        self.closed = False
        sock.settimeout(READ_TIMEOUT)

    def _read_exact(self, n: int) -> bytes:
        data = self.rfile.read(n)
        if data is None or len(data) < n:
            raise ConexaoFechada("conexão encerrada")
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header += bytes([size])
        elif size < 65536:
            header += bytes([126]) + struct.pack("!H", size)
        else:
            header += bytes([127]) + struct.pack("!Q", size)
        with self._write_lock:
            if self.closed:
                raise ConexaoFechada("conexão encerrada")
            try:
                self.wfile.write(header + payload)
                self.wfile.flush()
            except OSError as exc:
                self.closed = True
                raise ConexaoFechada(str(exc)) from None

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self) -> None:
        self._send_frame(OP_PING, b"automacao")

    def recv_text(self) -> str:
        parts: list[bytes] = []
        total = 0
        while True:
            try:
                b0, b1 = self._read_exact(2)
            except (socket.timeout, OSError) as exc:
                raise ConexaoFechada(f"sem resposta do celular: {exc}") from None
            fin, opcode = b0 & 0x80, b0 & 0x0F
            size = b1 & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._read_exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read_exact(8))[0]
            if not b1 & 0x80:
                raise ConexaoFechada("frame do cliente sem máscara")
            if size > MAX_MESSAGE or total + size > MAX_MESSAGE:
                raise ConexaoFechada("mensagem grande demais")
            mask = self._read_exact(4)
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(self._read_exact(size))) if size < 4096 \
                else _unmask(self._read_exact(size), mask)
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except ConexaoFechada:
                    pass
                raise ConexaoFechada("fechada pelo celular")
            if opcode in (OP_TEXT, OP_BINARY, OP_CONT):
                parts.append(payload)
                total += size
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        if not self.closed:
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
            except ConexaoFechada:
                pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _unmask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ key).to_bytes(n, "big")


class Ponte:
    """Guarda a conexão do celular (uma por vez) e casa cada pedido com a resposta."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: ConexaoWS | None = None
        self._pending: dict[int, list] = {}
        self._next_id = 0
        self.info: dict[str, Any] = {}
        self.connected_at = 0.0
        self.iniciado = time.monotonic()
        self._conectou = threading.Event()

    @property
    def conectado(self) -> bool:
        conn = self._conn
        return conn is not None and not conn.closed

    def atender(self, conn: ConexaoWS) -> None:
        """Roda na thread da requisição HTTP até a conexão do celular cair."""
        with self._lock:
            old, self._conn = self._conn, conn
            self.info = {}
            self.connected_at = time.time()
        if old is not None:
            log.info("nova conexão do celular substitui a anterior")
            old.close()
        log.info("celular conectado")
        self._conectou.set()
        stop = threading.Event()
        threading.Thread(target=self._pinger, args=(conn, stop), daemon=True).start()
        try:
            while True:
                try:
                    message = json.loads(conn.recv_text())
                except json.JSONDecodeError:
                    log.warning("mensagem inválida do celular")
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("tipo") == "ola":
                    self.info = {k: v for k, v in message.items() if k != "tipo"}
                    log.info("celular: %s", json.dumps(self.info, ensure_ascii=False)[:200])
                    continue
                with self._lock:
                    waiter = self._pending.get(message.get("id"))
                if waiter is not None:
                    waiter[1] = message
                    waiter[0].set()
        except ConexaoFechada as exc:
            log.info("celular desconectado: %s", exc)
        finally:
            stop.set()
            conn.close()
            with self._lock:
                if self._conn is conn:
                    self._conn = None
                    self.info = {}
                    self._conectou.clear()
                for waiter in self._pending.values():
                    if waiter[1] is None:
                        waiter[0].set()

    @staticmethod
    def _pinger(conn: ConexaoWS, stop: threading.Event) -> None:
        while not stop.wait(PING_INTERVAL):
            try:
                conn.ping()
            except ConexaoFechada:
                return

    def pedir(self, acao: str, args: dict, timeout: float = CALL_TIMEOUT) -> dict:
        with self._lock:
            conn = self._conn
            if conn is None or conn.closed:
                raise CelularError(NAO_CONECTADO)
            self._next_id += 1
            msg_id = self._next_id
            waiter: list = [threading.Event(), None]
            self._pending[msg_id] = waiter
        try:
            try:
                conn.send_text(json.dumps({"id": msg_id, "acao": acao, "args": args}, ensure_ascii=False))
            except ConexaoFechada:
                raise CelularError(NAO_CONECTADO) from None
            if not waiter[0].wait(timeout):
                raise CelularError(f"O celular não respondeu em {timeout:.0f} s.")
            if waiter[1] is None:
                raise CelularError("A conexão com o celular caiu durante o comando. " + NAO_CONECTADO)
            return waiter[1]
        finally:
            with self._lock:
                self._pending.pop(msg_id, None)

    # ------------------------------------------------------------------ ferramentas MCP

    def call(self, name: str, arguments: dict) -> tuple[list[dict], bool]:
        try:
            if name == "android_status":
                return [{"type": "text", "text": self._status()}], False
            if name == "android_wait":
                seconds = max(0.0, min(float(arguments.get("seconds", 1)), 30.0))
                time.sleep(seconds)
                return [{"type": "text", "text": f"Aguardou {seconds:g} s."}], False
            self._esperar_reconexao()
            reply = self.pedir(name.removeprefix("android_"), arguments)
        except CelularError as exc:
            return [{"type": "text", "text": str(exc)}], True
        except (TypeError, ValueError) as exc:
            return [{"type": "text", "text": f"Argumentos inválidos: {exc}"}], True
        if not reply.get("ok"):
            return [{"type": "text", "text": str(reply.get("erro") or "Falha no celular.")}], True
        content: list[dict] = []
        image = reply.get("imagem")
        if isinstance(image, dict) and image.get("data"):
            content.append({"type": "image", "data": image["data"], "mimeType": image.get("mimeType", "image/jpeg")})
        if reply.get("texto"):
            content.append({"type": "text", "text": str(reply["texto"])})
        return content or [{"type": "text", "text": "Feito."}], False

    def _esperar_reconexao(self) -> None:
        """Servidor recém-acordado: dá tempo ao app de reconectar (ele tenta de novo a cada 30 s no máximo)."""
        if self.conectado or time.monotonic() - self.iniciado > JANELA_RECONEXAO:
            return
        if not self._conectou.wait(ESPERA_RECONEXAO) or not self.conectado:
            raise CelularError("O servidor acabou de iniciar e o celular ainda não reconectou. O app tenta de novo "
                               "sozinho a cada 30 s no máximo; tente de novo em instantes. " + NAO_CONECTADO)
        time.sleep(0.5)  # o "olá" do app chega logo depois da conexão

    def _status(self) -> str:
        if not self.conectado:
            return NAO_CONECTADO
        info = self.info
        parts = [f"Celular conectado há {int(time.time() - self.connected_at)} s"]
        if info.get("modelo"):
            parts.append(f"modelo {info['modelo']}")
        if info.get("android"):
            parts.append(f"Android {info['android']}")
        if info.get("tela"):
            parts.append(f"capturas de {info['tela'][0]}x{info['tela'][1]}")
        if info.get("versao_app"):
            parts.append(f"app {info['versao_app']}")
        return ", ".join(parts) + "."


def mcp_tools() -> list[dict]:
    return [{
        "name": t["name"],
        "description": "[Celular Android, app Automação] " + t["description"],
        "inputSchema": t["schema"],
        "annotations": {"title": "Android: " + t["name"].removeprefix("android_"),
                        "readOnlyHint": t["read_only"], "destructiveHint": not t["read_only"],
                        "openWorldHint": False},
    } for t in TOOLS]
