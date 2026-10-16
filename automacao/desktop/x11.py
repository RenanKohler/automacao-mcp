"""Backend Linux/X11: Xlib para a tela e o cursor, extensão XTest para mouse e teclado.

Tudo via ctypes sobre libX11 e libXtst. Caracteres que não existem no mapa de teclado atual são
digitados remapeando temporariamente um keycode livre (a mesma técnica do xdotool).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
import time

from anthropic.tools import ToolError

from .base import Button, Desktop, Direction

Display_p = ctypes.c_void_p
Window = ctypes.c_ulong
KeySym = ctypes.c_ulong
KeyCode = ctypes.c_ubyte
ZPixmap = 2
AllPlanes = 0xFFFFFFFFFFFFFFFF


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
        ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong),
    ]


def _load(name: str, fallback: str) -> ctypes.CDLL:
    path = ctypes.util.find_library(name) or fallback
    try:
        return ctypes.CDLL(path)
    except OSError as exc:
        raise RuntimeError(f"Biblioteca {fallback} não encontrada (instale libx11-6 e libxtst6).") from exc


xlib = _load("X11", "libX11.so.6")
xtst = _load("Xtst", "libXtst.so.6")

xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
xlib.XOpenDisplay.restype = Display_p
xlib.XCloseDisplay.argtypes = [Display_p]
xlib.XDefaultScreen.argtypes = [Display_p]
xlib.XDefaultScreen.restype = ctypes.c_int
xlib.XDefaultRootWindow.argtypes = [Display_p]
xlib.XDefaultRootWindow.restype = Window
xlib.XDisplayWidth.argtypes = [Display_p, ctypes.c_int]
xlib.XDisplayWidth.restype = ctypes.c_int
xlib.XDisplayHeight.argtypes = [Display_p, ctypes.c_int]
xlib.XDisplayHeight.restype = ctypes.c_int
xlib.XGetImage.argtypes = [Display_p, Window, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_ulong, ctypes.c_int]
xlib.XGetImage.restype = ctypes.POINTER(XImage)
xlib.XDestroyImage.argtypes = [ctypes.POINTER(XImage)]
xlib.XQueryPointer.argtypes = [Display_p, Window, ctypes.POINTER(Window), ctypes.POINTER(Window),
                               ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                               ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                               ctypes.POINTER(ctypes.c_uint)]
xlib.XQueryPointer.restype = ctypes.c_int
xlib.XStringToKeysym.argtypes = [ctypes.c_char_p]
xlib.XStringToKeysym.restype = KeySym
xlib.XKeysymToKeycode.argtypes = [Display_p, KeySym]
xlib.XKeysymToKeycode.restype = KeyCode
xlib.XKeycodeToKeysym.argtypes = [Display_p, KeyCode, ctypes.c_int]
xlib.XKeycodeToKeysym.restype = KeySym
xlib.XDisplayKeycodes.argtypes = [Display_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
xlib.XGetKeyboardMapping.argtypes = [Display_p, KeyCode, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
xlib.XGetKeyboardMapping.restype = ctypes.POINTER(KeySym)
xlib.XChangeKeyboardMapping.argtypes = [Display_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(KeySym), ctypes.c_int]
xlib.XFree.argtypes = [ctypes.c_void_p]
xlib.XSync.argtypes = [Display_p, ctypes.c_int]
xlib.XFlush.argtypes = [Display_p]
xtst.XTestFakeMotionEvent.argtypes = [Display_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
xtst.XTestFakeButtonEvent.argtypes = [Display_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
xtst.XTestFakeKeyEvent.argtypes = [Display_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]

# nome canônico -> nome do keysym no X11
_KEYSYM_NAMES = {
    "Enter": "Return", "Tab": "Tab", "Escape": "Escape", "Backspace": "BackSpace", "Delete": "Delete",
    "Insert": "Insert", " ": "space", "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left",
    "ArrowRight": "Right", "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next",
    "CapsLock": "Caps_Lock", "ContextMenu": "Menu", "PrintScreen": "Print",
    "Control": "Control_L", "Shift": "Shift_L", "Alt": "Alt_L", "Meta": "Super_L",
    **{f"F{i}": f"F{i}" for i in range(1, 25)},
}
_BUTTONS = {"left": 1, "middle": 2, "right": 3}
_WHEEL = {"up": 4, "down": 5, "left": 6, "right": 7}


def _char_keysym(char: str) -> int:
    code = ord(char)
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code  # Latin-1: o keysym é o próprio código
    return 0x01000000 + code


class X11Desktop(Desktop):
    def __init__(self) -> None:
        self.display = xlib.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("Não foi possível conectar ao servidor X (verifique DISPLAY).")
        self.screen = xlib.XDefaultScreen(self.display)
        self.root = xlib.XDefaultRootWindow(self.display)
        self._lock = threading.Lock()
        self._spare: int | None = None

    def size(self) -> tuple[int, int]:
        return xlib.XDisplayWidth(self.display, self.screen), xlib.XDisplayHeight(self.display, self.screen)

    def capture(self) -> tuple[int, int, int, bytes]:
        width, height = self.size()
        with self._lock:
            image = xlib.XGetImage(self.display, self.root, 0, 0, width, height, AllPlanes, ZPixmap)
            if not image:
                raise ToolError("Falha ao capturar a tela.")
            try:
                img = image.contents
                if img.bits_per_pixel != 32 or img.blue_mask != 0xFF:
                    raise ToolError(f"Formato de tela não suportado ({img.bits_per_pixel} bits por pixel).")
                data = ctypes.string_at(img.data, img.bytes_per_line * img.height)
                return img.width, img.height, img.bytes_per_line, data
            finally:
                xlib.XDestroyImage(image)

    def cursor(self) -> tuple[int, int]:
        root, child = Window(), Window()
        rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        with self._lock:
            xlib.XQueryPointer(self.display, self.root, ctypes.byref(root), ctypes.byref(child),
                               ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
        return rx.value, ry.value

    def move(self, x: int, y: int) -> None:
        with self._lock:
            xtst.XTestFakeMotionEvent(self.display, self.screen, x, y, 0)
            xlib.XSync(self.display, 0)

    def button(self, button: Button, down: bool) -> None:
        with self._lock:
            xtst.XTestFakeButtonEvent(self.display, _BUTTONS[button], int(down), 0)
            xlib.XSync(self.display, 0)

    def wheel(self, direction: Direction, clicks: int) -> None:
        with self._lock:
            for _ in range(clicks):
                xtst.XTestFakeButtonEvent(self.display, _WHEEL[direction], 1, 0)
                xtst.XTestFakeButtonEvent(self.display, _WHEEL[direction], 0, 0)
            xlib.XSync(self.display, 0)

    def _keysym(self, name: str) -> int:
        if name in _KEYSYM_NAMES:
            keysym = xlib.XStringToKeysym(_KEYSYM_NAMES[name].encode())
        elif len(name) == 1:
            keysym = _char_keysym(name.lower() if name.isascii() and name.isalpha() else name)
        else:
            keysym = 0
        if not keysym:
            raise ToolError(f'Tecla não suportada: "{name}".')
        return keysym

    def _keycode_for(self, keysym: int) -> tuple[int, bool]:
        """(keycode, precisa de Shift); remapeia um keycode livre quando o keysym não está no mapa."""
        keycode = xlib.XKeysymToKeycode(self.display, keysym)
        if keycode:
            if xlib.XKeycodeToKeysym(self.display, keycode, 0) == keysym:
                return keycode, False
            if xlib.XKeycodeToKeysym(self.display, keycode, 1) == keysym:
                return keycode, True
        spare = self._spare_keycode()
        symbols = (KeySym * 2)(keysym, keysym)
        xlib.XChangeKeyboardMapping(self.display, spare, 2, symbols, 1)
        xlib.XSync(self.display, 0)
        time.sleep(0.02)  # o servidor precisa propagar o novo mapa antes da tecla
        return spare, False

    def _spare_keycode(self) -> int:
        if self._spare is not None:
            return self._spare
        low, high = ctypes.c_int(), ctypes.c_int()
        xlib.XDisplayKeycodes(self.display, ctypes.byref(low), ctypes.byref(high))
        per = ctypes.c_int()
        count = high.value - low.value + 1
        mapping = xlib.XGetKeyboardMapping(self.display, low.value, count, ctypes.byref(per))
        try:
            for index in range(count - 1, -1, -1):
                if all(mapping[index * per.value + j] == 0 for j in range(per.value)):
                    self._spare = low.value + index
                    return self._spare
        finally:
            xlib.XFree(mapping)
        raise ToolError("Nenhum keycode livre para digitar este caractere.")

    def _restore_spare(self) -> None:
        if self._spare is not None:
            symbols = (KeySym * 2)(0, 0)
            xlib.XChangeKeyboardMapping(self.display, self._spare, 2, symbols, 1)
            xlib.XSync(self.display, 0)

    def key(self, name: str, down: bool) -> None:
        with self._lock:
            keycode, needs_shift = self._keycode_for(self._keysym(name))
            shift = xlib.XKeysymToKeycode(self.display, xlib.XStringToKeysym(b"Shift_L"))
            if needs_shift and down:
                xtst.XTestFakeKeyEvent(self.display, shift, 1, 0)
            xtst.XTestFakeKeyEvent(self.display, keycode, int(down), 0)
            if needs_shift and not down:
                xtst.XTestFakeKeyEvent(self.display, shift, 0, 0)
            xlib.XSync(self.display, 0)

    def type_text(self, text: str) -> None:
        shift = xlib.XKeysymToKeycode(self.display, xlib.XStringToKeysym(b"Shift_L"))
        with self._lock:
            remapped = False
            try:
                for char in text:
                    if char == "\r":
                        continue
                    keysym = {"\n": 0xFF0D, "\t": 0xFF09}.get(char) or _char_keysym(char)
                    keycode, needs_shift = self._keycode_for(keysym)
                    remapped = remapped or keycode == self._spare
                    if needs_shift:
                        xtst.XTestFakeKeyEvent(self.display, shift, 1, 0)
                    xtst.XTestFakeKeyEvent(self.display, keycode, 1, 0)
                    xtst.XTestFakeKeyEvent(self.display, keycode, 0, 0)
                    if needs_shift:
                        xtst.XTestFakeKeyEvent(self.display, shift, 0, 0)
                    xlib.XSync(self.display, 0)
                    time.sleep(0.004)
            finally:
                if remapped:
                    time.sleep(0.02)
                    self._restore_spare()

    def close(self) -> None:
        if self.display:
            xlib.XCloseDisplay(self.display)
            self.display = None
