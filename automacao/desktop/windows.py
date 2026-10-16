"""Backend Windows: SendInput para mouse e teclado, GDI (BitBlt + GetDIBits) para a tela.

Tudo via ctypes, sem dependências. O processo se declara "DPI aware" para que coordenadas e
capturas usem pixels físicos mesmo com escala de 125%/150% no Windows.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from anthropic.tools import ToolError

from .base import Button, Desktop, Direction

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x4, 0x8
WHEEL_DELTA = 120
SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
DIB_RGB_COLORS, BI_RGB = 0, 0
SM_CXSCREEN, SM_CYSCREEN = 0, 1


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(BITMAPINFO), wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

# nome canônico -> código de tecla virtual
_VK = {
    "Enter": 0x0D, "Tab": 0x09, "Escape": 0x1B, "Backspace": 0x08, "Delete": 0x2E, "Insert": 0x2D, " ": 0x20,
    "ArrowUp": 0x26, "ArrowDown": 0x28, "ArrowLeft": 0x25, "ArrowRight": 0x27, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22, "CapsLock": 0x14, "ContextMenu": 0x5D, "PrintScreen": 0x2C,
    "Control": 0x11, "Shift": 0x10, "Alt": 0x12, "Meta": 0x5B,
    **{f"F{i}": 0x6F + i for i in range(1, 25)},
}
_EXTENDED = {0x2E, 0x2D, 0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x5D, 0x5B, 0x2C}


def _set_dpi_aware() -> None:
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


class WindowsDesktop(Desktop):
    def __init__(self) -> None:
        _set_dpi_aware()

    def size(self) -> tuple[int, int]:
        return user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)

    def _send(self, *inputs: INPUT) -> None:
        array = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            # Bloqueado pelo UIPI: a janela em foco roda com privilégio maior (ex.: como administrador).
            raise ToolError("O Windows bloqueou a entrada simulada (a janela em foco pode exigir privilégio de administrador).")

    def capture(self) -> tuple[int, int, int, bytes]:
        width, height = self.size()
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        old = gdi32.SelectObject(mem_dc, bitmap)
        try:
            if not gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, 0, 0, SRCCOPY | CAPTUREBLT):
                raise ToolError("Falha ao capturar a tela.")
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # negativo: linhas de cima para baixo
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = BI_RGB
            buffer = ctypes.create_string_buffer(width * height * 4)
            if not gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS):
                raise ToolError("Falha ao ler os pixels da tela.")
            return width, height, width * 4, buffer.raw
        finally:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)

    def cursor(self) -> tuple[int, int]:
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def move(self, x: int, y: int) -> None:
        width, height = self.size()
        event = INPUT(type=INPUT_MOUSE)
        event.mi = MOUSEINPUT(dx=int(x * 65535 / max(1, width - 1)), dy=int(y * 65535 / max(1, height - 1)),
                              dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)
        self._send(event)

    def button(self, button: Button, down: bool) -> None:
        flags = {
            ("left", True): MOUSEEVENTF_LEFTDOWN, ("left", False): MOUSEEVENTF_LEFTUP,
            ("right", True): MOUSEEVENTF_RIGHTDOWN, ("right", False): MOUSEEVENTF_RIGHTUP,
            ("middle", True): MOUSEEVENTF_MIDDLEDOWN, ("middle", False): MOUSEEVENTF_MIDDLEUP,
        }[(button, down)]
        event = INPUT(type=INPUT_MOUSE)
        event.mi = MOUSEINPUT(dwFlags=flags)
        self._send(event)

    def wheel(self, direction: Direction, clicks: int) -> None:
        horizontal = direction in ("left", "right")
        sign = 1 if direction in ("up", "right") else -1
        event = INPUT(type=INPUT_MOUSE)
        # mouseData é DWORD: o valor negativo vai em complemento de dois.
        event.mi = MOUSEINPUT(mouseData=(sign * WHEEL_DELTA * clicks) & 0xFFFFFFFF,
                              dwFlags=MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL)
        self._send(event)

    def _vk(self, name: str) -> tuple[int, bool]:
        """(código virtual, precisa de Shift) para um nome canônico.

        Código 0 indica um caractere sem tecla própria no layout atual, ou que exige AltGr
        (Ctrl+Alt), como "?" e "/" no ABNT2: esses são enviados como caractere Unicode.
        """
        if name in _VK:
            return _VK[name], False
        if len(name) == 1:
            if name.isascii() and name.isalnum():
                return ord(name.upper()), False
            scan = user32.VkKeyScanW(name)
            if scan != -1 and not scan & 0x600:
                return scan & 0xFF, bool(scan & 0x100)
            return 0, False
        raise ToolError(f'Tecla não suportada neste teclado: "{name}".')

    def _unicode_events(self, char: str, down: bool) -> list[INPUT]:
        """Eventos KEYEVENTF_UNICODE de um caractere (dois se for par substituto UTF-16)."""
        units = char.encode("utf-16-le")
        events = []
        for i in range(0, len(units), 2):
            event = INPUT(type=INPUT_KEYBOARD)
            event.ki = KEYBDINPUT(wVk=0, wScan=int.from_bytes(units[i : i + 2], "little"),
                                  dwFlags=KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP))
            events.append(event)
        return events

    def _unicode_event(self, char: str, down: bool) -> INPUT:
        return self._unicode_events(char, down)[0]

    def _key_event(self, vk: int, down: bool) -> INPUT:
        flags = 0 if down else KEYEVENTF_KEYUP
        if vk in _EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        event = INPUT(type=INPUT_KEYBOARD)
        event.ki = KEYBDINPUT(wVk=vk, wScan=user32.MapVirtualKeyW(vk, 0), dwFlags=flags)
        return event

    def key(self, name: str, down: bool) -> None:
        vk, shift = self._vk(name)
        if vk == 0:
            self._send(self._unicode_event(name, down))
        elif shift and down:
            self._send(self._key_event(_VK["Shift"], True), self._key_event(vk, True))
        elif shift:
            self._send(self._key_event(vk, False), self._key_event(_VK["Shift"], False))
        else:
            self._send(self._key_event(vk, down))

    def type_text(self, text: str) -> None:
        for char in text:
            if char in "\r\n":
                if char == "\n":
                    self._send(self._key_event(0x0D, True), self._key_event(0x0D, False))
                continue
            if char == "\t":
                self._send(self._key_event(0x09, True), self._key_event(0x09, False))
                continue
            downs, ups = self._unicode_events(char, True), self._unicode_events(char, False)
            self._send(*(event for pair in zip(downs, ups) for event in pair))
            time.sleep(0.004)
