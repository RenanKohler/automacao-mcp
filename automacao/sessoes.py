"""Navegador isolado por sessão MCP (Mcp-Session-Id) no servidor HTTP.

Um único Chrome atende todas as sessões; cada sessão ganha um contexto próprio (como uma janela
anônima: abas, cookies e armazenamento separados), criado na primeira ferramenta browser_* que ela usa.
Sessões paradas por SESSAO_OCIOSA segundos têm o contexto descartado (as abas fecham). No máximo
LIMITE_SESSOES sessões ficam com navegador aberto ao mesmo tempo, por causa da memória do plano gratuito;
a seguinte recebe erro de capacidade até uma delas expirar.
Pedidos sem Mcp-Session-Id usam a sessão "padrao".
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .mcp_servidor import Ferramentas

log = logging.getLogger("automacao.sessoes")

LIMITE_SESSOES = 2
SESSAO_OCIOSA = float(os.environ.get("AUTOMACAO_SESSAO_OCIOSA", "600"))
SESSAO_PADRAO = "padrao"

# Sessão do pedido HTTP em andamento (cada pedido roda na sua thread).
atual = threading.local()


class GerenteChrome:
    """Abre o Chrome compartilhado e cria ou descarta os contextos das sessões."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._lock = threading.Lock()
        self._chrome: Any = None
        self._conn: Any = None
        self._endpoint: str | None = None

    def _vivo(self) -> bool:
        if self._conn is None or self._conn.closed:
            return False
        return self._chrome is None or self._chrome.process.poll() is None

    def endpoint(self) -> str:
        from .cdp import CDPConnection
        from .chrome import ChromeProcess, ws_url_from_http

        with self._lock:
            if not self._vivo():
                self._fechar()
                a = self.args
                if a.cdp:
                    self._endpoint = a.cdp if a.cdp.startswith("ws://") else ws_url_from_http(a.cdp)
                else:
                    width, height = (int(v) for v in a.viewport.lower().split("x"))
                    self._chrome = ChromeProcess(executable=a.chrome_path, headless=a.headless,
                                                 window_size=(width, height + 120))
                    self._endpoint = self._chrome.ws_url
                self._conn = CDPConnection(self._endpoint)
            return self._endpoint  # type: ignore[return-value]

    def novo_contexto(self) -> tuple[str, str]:
        endpoint = self.endpoint()
        contexto = self._conn.send("Target.createBrowserContext", {"disposeOnDetach": False})["browserContextId"]
        return endpoint, contexto

    def descartar(self, contexto: str) -> None:
        try:
            if self._vivo():
                self._conn.send("Target.disposeBrowserContext", {"browserContextId": contexto}, timeout=10)
        except Exception:  # noqa: BLE001 - o Chrome pode já ter saído
            log.debug("falha ao descartar o contexto", exc_info=True)

    def _fechar(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
        if self._chrome is not None:
            self._chrome.close()
            self._chrome = None

    def close(self) -> None:
        with self._lock:
            self._fechar()


class FerramentasDeSessao(Ferramentas):
    """Ferramentas de uma sessão: o navegador é um contexto próprio no Chrome compartilhado."""

    def __init__(self, args: argparse.Namespace, gerente: GerenteChrome) -> None:
        super().__init__(args)
        self.gerente = gerente
        self.contexto: str | None = None

    @property
    def navegador_aberto(self) -> bool:
        return self._browser is not None

    def _criar_navegador(self, **kwargs: Any) -> Any:
        from .navegador import NavegadorCDP

        endpoint, self.contexto = self.gerente.novo_contexto()
        kwargs.update(cdp_endpoint=endpoint, browser_context_id=self.contexto)
        try:
            return NavegadorCDP(**kwargs)
        except Exception:
            self.gerente.descartar(self.contexto)
            self.contexto = None
            raise

    def _close_browser(self) -> None:
        super()._close_browser()
        contexto, self.contexto = self.contexto, None
        if contexto:
            self.gerente.descartar(contexto)


@dataclass
class _Sessao:
    ferramentas: FerramentasDeSessao
    ultimo_uso: float = field(default_factory=time.monotonic)
    em_uso: int = 0


class Sessoes:
    """Encaminha cada chamada para as ferramentas da sessão do pedido em andamento."""

    def __init__(self, args: argparse.Namespace, limite: int = LIMITE_SESSOES, ociosa: float = SESSAO_OCIOSA) -> None:
        self.args = args
        self.limite = limite
        self.ociosa = ociosa
        self.gerente = GerenteChrome(args)
        self.tools = Ferramentas(args).tools  # só a lista; não abre navegador
        self._lock = threading.Lock()
        self._sessoes: dict[str, _Sessao] = {}
        self._parar = threading.Event()
        threading.Thread(target=self._limpar_periodicamente, daemon=True).start()

    def _ativas(self, exceto: str) -> int:
        return sum(1 for sid, s in self._sessoes.items()
                   if sid != exceto and (s.ferramentas.navegador_aberto or s.em_uso))

    def call(self, name: str, arguments: dict) -> tuple[list[dict], bool]:
        sid = getattr(atual, "sessao", None) or SESSAO_PADRAO
        with self._lock:
            sessao = self._sessoes.get(sid)
            if sessao is None:
                sessao = self._sessoes[sid] = _Sessao(FerramentasDeSessao(self.args, self.gerente))
            if (name.startswith("browser_") and not sessao.ferramentas.navegador_aberto
                    and self._ativas(sid) >= self.limite):
                return [{"type": "text", "text": (
                    f"Capacidade esgotada: já há {self.limite} conversas usando o navegador neste servidor, "
                    f"o limite do plano gratuito. O navegador de uma conversa fecha sozinho depois de "
                    f"{max(1, round(self.ociosa / 60))} minuto(s) sem uso; tente de novo depois disso.")}], True
            sessao.em_uso += 1
            sessao.ultimo_uso = time.monotonic()
        try:
            return sessao.ferramentas.call(name, arguments)
        finally:
            with self._lock:
                sessao.em_uso -= 1
                sessao.ultimo_uso = time.monotonic()

    def encerrar(self, sid: str) -> bool:
        with self._lock:
            sessao = self._sessoes.pop(sid, None)
        if sessao is None:
            return False
        sessao.ferramentas.close()
        log.info("sessão encerrada pelo cliente")
        return True

    def limpar(self) -> int:
        """Fecha as sessões paradas há mais de `ociosa` segundos. Devolve quantas fechou."""
        agora = time.monotonic()
        with self._lock:
            vencidas = [sid for sid, s in self._sessoes.items() if not s.em_uso and agora - s.ultimo_uso > self.ociosa]
            fechar = [self._sessoes.pop(sid) for sid in vencidas]
        for sessao in fechar:
            sessao.ferramentas.close()
        if fechar:
            log.info("%d sessão(ões) ociosa(s) encerrada(s)", len(fechar))
        return len(fechar)

    def _limpar_periodicamente(self) -> None:
        while not self._parar.wait(min(30.0, max(self.ociosa / 4, 0.5))):
            try:
                self.limpar()
            except Exception:  # noqa: BLE001
                log.exception("falha ao limpar sessões")

    def close(self) -> None:
        self._parar.set()
        with self._lock:
            todas, self._sessoes = list(self._sessoes.values()), {}
        for sessao in todas:
            sessao.ferramentas.close()
        self.gerente.close()
