"""Conexão com o Chrome DevTools Protocol (CDP).

Uma única conexão WebSocket com o endpoint do navegador, no modo "flatten": comandos e eventos
de cada aba carregam o `sessionId` da sessão anexada a ela. Uma thread lê o socket e resolve as
respostas; outra entrega os eventos aos ouvintes, em ordem, para que um ouvinte possa enviar
comandos e esperar a resposta sem travar a leitura.
"""

from __future__ import annotations

import itertools
import json
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from .websocket import WebSocket, WebSocketClosed

log = logging.getLogger(__name__)

EventListener = Callable[[str, dict, "str | None"], None]


class CDPError(Exception):
    def __init__(self, method: str, error: dict) -> None:
        self.method = method
        self.code = error.get("code")
        self.message = error.get("message", "")
        super().__init__(f"{method}: {self.message} ({self.code})")


class CDPTimeout(Exception):
    pass


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None
        self.error: dict | None = None


class CDPConnection:
    def __init__(self, ws_url: str) -> None:
        self._ws = WebSocket(ws_url)
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._listeners: list[EventListener] = []
        self._events: queue.Queue = queue.Queue()
        self.closed = False
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._dispatcher = threading.Thread(target=self._dispatch_loop, name="cdp-events", daemon=True)
        self._reader.start()
        self._dispatcher.start()

    def add_listener(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    def send(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Envia um comando e espera a resposta. Lança CDPError, CDPTimeout ou WebSocketClosed."""
        if self.closed:
            raise WebSocketClosed("conexão CDP encerrada")
        msg_id = next(self._ids)
        pending = _Pending()
        with self._pending_lock:
            self._pending[msg_id] = pending
        self._write(msg_id, method, params, session_id)
        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            raise CDPTimeout(f"{method}: sem resposta em {timeout:.0f}s")
        if pending.error is not None:
            raise CDPError(method, pending.error)
        return pending.result or {}

    def send_nowait(self, method: str, params: dict | None = None, session_id: str | None = None) -> None:
        """Envia um comando sem esperar a resposta (usado dentro de ouvintes de eventos)."""
        if self.closed:
            return
        try:
            self._write(next(self._ids), method, params, session_id)
        except (OSError, WebSocketClosed):
            pass

    def _write(self, msg_id: int, method: str, params: dict | None, session_id: str | None) -> None:
        message: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self._ws.send_text(json.dumps(message))

    def _read_loop(self) -> None:
        try:
            while True:
                raw = self._ws.recv_text()
                message = json.loads(raw)
                if "id" in message:
                    with self._pending_lock:
                        pending = self._pending.pop(message["id"], None)
                    if pending is not None:
                        pending.result = message.get("result")
                        pending.error = message.get("error")
                        pending.event.set()
                elif "method" in message:
                    self._events.put((message["method"], message.get("params", {}), message.get("sessionId")))
        except (WebSocketClosed, OSError, ValueError) as exc:
            log.debug("leitura CDP encerrada: %s", exc)
        finally:
            self.closed = True
            with self._pending_lock:
                for pending in self._pending.values():
                    pending.error = {"message": "conexão CDP encerrada", "code": -1}
                    pending.event.set()
                self._pending.clear()
            self._events.put(None)

    def _dispatch_loop(self) -> None:
        while True:
            item = self._events.get()
            if item is None:
                return
            method, params, session_id = item
            for listener in list(self._listeners):
                try:
                    listener(method, params, session_id)
                except Exception:  # um ouvinte com erro não pode parar os demais
                    log.exception("erro ao tratar o evento %s", method)

    def close(self) -> None:
        self.closed = True
        self._ws.close()
