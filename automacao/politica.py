"""Política de URL: decide quais endereços o navegador pode abrir.

A mesma função `is_allowed` é usada em dois pontos: na política de `navigate` que o SDK chama
antes de cada navegação, e na interceptação de requisições do driver (Fetch.requestPaused),
que vê cliques, redirecionamentos, formulários e sub-recursos.

Regras padrão:
- apenas http e https (além de about:blank); `javascript:`, `file:`, `data:` etc. são recusados;
- loopback, endereços privados, link-local (incluindo 169.254.169.254) e afins são recusados,
  tanto como IP literal quanto pelo endereço para o qual o nome resolve;
- opcionalmente, uma lista de hosts permitidos (com subdomínios).

Isto não substitui regras de egress na rede: veja o README.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from anthropic.tools import ToolError

SCHEME_PREFIX = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_BLOCKED_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def with_scheme(url: str) -> str:
    """Acrescenta https:// a uma URL sem esquema, como o modelo pode escrever (`example.com`)."""
    url = url.strip()
    return url if SCHEME_PREFIX.match(url) else f"https://{url}"


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


class URLPolicy:
    def __init__(
        self,
        allowed_hosts: list[str] | None = None,
        allow_private: bool = False,
        resolve_dns: bool = True,
    ) -> None:
        self.allowed_hosts = [h.strip().lower().lstrip(".") for h in (allowed_hosts or []) if h.strip()]
        self.allow_private = allow_private
        self.resolve_dns = resolve_dns
        self._dns_cache: dict[str, tuple[float, bool]] = {}
        self._lock = threading.Lock()

    def reason_blocked(self, url: str) -> str | None:
        """None quando a URL é permitida; caso contrário, o motivo da recusa."""
        if url.strip().lower() == "about:blank":
            return None
        try:
            # O navegador lê "\" como "/" em URLs da web.
            parts = urlsplit(with_scheme(url).replace("\\", "/"))
            host = (parts.hostname or "").rstrip(".").lower()
        except ValueError:
            return "URL inválida"
        if parts.scheme not in ("http", "https"):
            return f"esquema não permitido: {parts.scheme}"
        if not host:
            return "URL sem host"
        if self.allowed_hosts and not any(host == h or host.endswith("." + h) for h in self.allowed_hosts):
            return f"{host} não está na lista de hosts permitidos"
        if self.allow_private:
            return None
        if host in _BLOCKED_NAMES or host.endswith(".localhost") or host.endswith(".internal"):
            return f"{host} é um endereço local"
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None:
            return f"{host} é um endereço privado ou reservado" if _ip_blocked(literal) else None
        if self.resolve_dns and self._resolves_private(host):
            return f"{host} resolve para um endereço privado ou reservado"
        return None

    def is_allowed(self, url: str) -> bool:
        return self.reason_blocked(url) is None

    def _resolves_private(self, host: str) -> bool:
        now = time.monotonic()
        with self._lock:
            cached = self._dns_cache.get(host)
            if cached and now - cached[0] < 60:
                return cached[1]
        try:
            infos = socket.getaddrinfo(host, None)
            blocked = any(_ip_blocked(ipaddress.ip_address(info[4][0].split("%")[0])) for info in infos)
        except (socket.gaierror, UnicodeError, ValueError):
            blocked = False  # nome que não resolve: o navegador também não chegará a ele
        with self._lock:
            self._dns_cache[host] = (now, blocked)
        return blocked

    def __call__(self, context: object, url: str) -> None:
        """Assinatura de `url_policy` do SDK: lança ToolError para recusar."""
        reason = self.reason_blocked(url)
        if reason is not None:
            raise ToolError(f"Navegação recusada pela política de URL: {reason}.")
