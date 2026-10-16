"""Interface comum dos backends de desktop.

Coordenadas aqui são sempre pixels físicos da tela principal. A conversão para o espaço das
capturas que o modelo vê fica no toolset (`automacao.computador`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

Button = Literal["left", "right", "middle"]
Direction = Literal["up", "down", "left", "right"]


class Desktop(ABC):
    @abstractmethod
    def size(self) -> tuple[int, int]:
        """Largura e altura da tela principal, em pixels físicos."""

    @abstractmethod
    def capture(self) -> tuple[int, int, int, bytes]:
        """(largura, altura, bytes por linha, pixels BGRA/BGRX de cima para baixo)."""

    @abstractmethod
    def cursor(self) -> tuple[int, int]: ...

    @abstractmethod
    def move(self, x: int, y: int) -> None: ...

    @abstractmethod
    def button(self, button: Button, down: bool) -> None: ...

    @abstractmethod
    def wheel(self, direction: Direction, clicks: int) -> None: ...

    @abstractmethod
    def key(self, name: str, down: bool) -> None:
        """Pressiona ou solta uma tecla pelo nome canônico de `automacao.teclas`."""

    @abstractmethod
    def type_text(self, text: str) -> None:
        """Digita texto Unicode independentemente do layout do teclado."""

    def close(self) -> None:
        pass
