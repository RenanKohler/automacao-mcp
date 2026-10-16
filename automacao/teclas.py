"""Interpretação dos nomes de teclas que o modelo envia ("ctrl+s", "Return", "Page_Down", "a").

`parse_sequence` separa a sequência por espaços e cada combinação por "+", devolvendo nomes
canônicos. Cada driver traduz os nomes canônicos para o seu sistema (CDP, Win32, X11).
"""

from __future__ import annotations

from anthropic.tools import ToolError

MODIFIERS = {"Control", "Shift", "Alt", "Meta"}

_ALIASES = {
    "ctrl": "Control", "control": "Control", "ctl": "Control",
    "shift": "Shift",
    "alt": "Alt", "option": "Alt", "opt": "Alt",
    "cmd": "Meta", "command": "Meta", "meta": "Meta", "super": "Meta", "win": "Meta", "windows": "Meta",
    "return": "Enter", "enter": "Enter", "kp_enter": "Enter",
    "tab": "Tab", "iso_left_tab": "Tab",
    "escape": "Escape", "esc": "Escape",
    "backspace": "Backspace", "back_space": "Backspace",
    "delete": "Delete", "del": "Delete",
    "insert": "Insert", "ins": "Insert",
    "space": " ", "spacebar": " ",
    "up": "ArrowUp", "arrowup": "ArrowUp", "down": "ArrowDown", "arrowdown": "ArrowDown",
    "left": "ArrowLeft", "arrowleft": "ArrowLeft", "right": "ArrowRight", "arrowright": "ArrowRight",
    "home": "Home", "end": "End",
    "page_up": "PageUp", "pageup": "PageUp", "prior": "PageUp", "pgup": "PageUp",
    "page_down": "PageDown", "pagedown": "PageDown", "next": "PageDown", "pgdn": "PageDown",
    "caps_lock": "CapsLock", "capslock": "CapsLock",
    "menu": "ContextMenu", "print": "PrintScreen", "printscreen": "PrintScreen",
    "minus": "-", "plus": "+", "equal": "=", "comma": ",", "period": ".", "slash": "/",
    "backslash": "\\", "semicolon": ";", "apostrophe": "'", "grave": "`",
    "bracketleft": "[", "bracketright": "]",
    # nomes de keysym do X11 que o modelo também usa
    "question": "?", "exclam": "!", "at": "@", "numbersign": "#", "dollar": "$", "percent": "%",
    "asciicircum": "^", "ampersand": "&", "asterisk": "*", "parenleft": "(", "parenright": ")",
    "underscore": "_", "colon": ":", "quotedbl": '"', "less": "<", "greater": ">", "bar": "|",
    "braceleft": "{", "braceright": "}", "asciitilde": "~",
}
NAMED_KEYS = {
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Insert", " ", "ArrowUp", "ArrowDown", "ArrowLeft",
    "ArrowRight", "Home", "End", "PageUp", "PageDown", "CapsLock", "ContextMenu", "PrintScreen",
} | {f"F{i}" for i in range(1, 25)} | MODIFIERS


def canonical(name: str) -> str:
    if len(name) == 1:
        return name
    lowered = name.lower()
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    if lowered.startswith("f") and lowered[1:].isdigit() and 1 <= int(lowered[1:]) <= 24:
        return "F" + lowered[1:]
    if lowered.startswith("kp_") and len(lowered) == 4:
        return lowered[3]
    for key in NAMED_KEYS:
        if key.lower() == lowered:
            return key
    raise ToolError(f'Tecla desconhecida: "{name}".')


def parse_combo(combo: str) -> tuple[list[str], str | None]:
    """"ctrl+shift+t" -> (["Control", "Shift"], "t"). Uma combinação só de modificadores tem tecla None."""
    # "+" sozinho, ou no fim ("ctrl++"), é a própria tecla "+".
    parts = combo.split("+")
    if combo.endswith("+"):
        parts = [p for p in parts[:-2] if p] + ["+"]
    names = [canonical(p) for p in parts if p != ""]
    if not names:
        raise ToolError(f'Combinação de teclas vazia: "{combo}".')
    modifiers = [n for n in names[:-1] if n in MODIFIERS]
    if len(modifiers) != len(names) - 1:
        raise ToolError(f'Em "{combo}", apenas a última tecla pode não ser modificadora.')
    last = names[-1]
    if last in MODIFIERS:
        return modifiers + [last], None
    return modifiers, last


def parse_sequence(text: str) -> list[tuple[list[str], str | None]]:
    combos = [c for c in text.strip().split(" ") if c]
    if not combos:
        raise ToolError("Nenhuma tecla informada.")
    return [parse_combo(c) for c in combos]


def parse_modifiers(text: str | None) -> list[str]:
    if not text:
        return []
    names = [canonical(p) for p in text.split("+") if p]
    for name in names:
        if name not in MODIFIERS:
            raise ToolError(f'"{name}" não é uma tecla modificadora.')
    return names
