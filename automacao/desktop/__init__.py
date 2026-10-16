"""Backends nativos de entrada (mouse e teclado) e captura de tela."""

from __future__ import annotations

import os
import sys

from .base import Desktop


def open_desktop() -> Desktop:
    """O backend do sistema atual: Windows (Win32 via ctypes) ou Linux com X11 (Xlib/XTest via ctypes)."""
    if sys.platform == "win32":
        from .windows import WindowsDesktop

        return WindowsDesktop()
    if sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
        if not os.environ.get("DISPLAY"):
            raise RuntimeError("DISPLAY não está definido: o backend Linux precisa de uma sessão X11 (ou XWayland).")
        from .x11 import X11Desktop

        return X11Desktop()
    raise RuntimeError(f"Sistema não suportado pelo driver de PC: {sys.platform}")


__all__ = ["Desktop", "open_desktop"]
