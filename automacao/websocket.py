"""Cliente WebSocket mínimo (RFC 6455) sobre a biblioteca padrão.

Suficiente para falar com o Chrome DevTools Protocol em ws://127.0.0.1 e, com TLS (wss://), com o
servidor da nuvem (ponte do PC): apenas cliente, sem extensões. Envia frames de texto mascarados e
remonta mensagens fragmentadas.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlsplit

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


class WebSocketClosed(WebSocketError):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 10.0) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise WebSocketError(f"esquema não suportado: {parts.scheme}")
        tls = parts.scheme == "wss"
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if tls else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if tls:
            self._sock = ssl.create_default_context().wrap_socket(self._sock, server_hostname=host)
        self._send_lock = threading.Lock()
        self._buffer = b""
        self.closed = False
        self._handshake(host if parts.port is None and (tls or port == 80) else f"{host}:{port}", path)
        self._sock.settimeout(None)

    def _handshake(self, host: str, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        while b"\r\n\r\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("conexão encerrada durante o handshake")
            self._buffer += chunk
        head, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise WebSocketError(f"handshake recusado: {lines[0]}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketError("Sec-WebSocket-Accept inválido")

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self._sock.recv(max(65536, n - len(self._buffer)))
            if not chunk:
                raise WebSocketClosed("conexão encerrada")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        # Mascaramento obrigatório para frames do cliente (RFC 6455, 5.3).
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload)) if length < 512 else _mask(payload, mask)
        with self._send_lock:
            if self.closed:
                raise WebSocketClosed("conexão encerrada")
            self._sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def recv_text(self) -> str:
        """Bloqueia até a próxima mensagem de texto completa. Responde a pings."""
        fragments: list[bytes] = []
        message_opcode = None
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            if b1 & 0x80:
                mask = self._recv_exact(4)
                payload = _mask(self._recv_exact(length), mask)
            else:
                payload = self._recv_exact(length)

            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.closed = True
                raise WebSocketClosed("o servidor fechou a conexão")
            if opcode in (OP_TEXT, OP_BINARY):
                message_opcode = opcode
                fragments = [payload]
            elif opcode == OP_CONT:
                fragments.append(payload)
            if fin and message_opcode is not None:
                return b"".join(fragments).decode("utf-8", errors="replace")

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
        except OSError:
            pass
        self.closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


def _mask(payload: bytes, mask: bytes) -> bytes:
    # XOR em blocos via inteiros grandes: rápido o bastante para capturas de tela em base64.
    n = len(payload)
    repeated = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(n, "big")
