"""Aprovação humana pelo terminal antes de ações com consequência (o callable `confirm` do SDK)."""

from __future__ import annotations

import json
import sys
import threading
from urllib.parse import urlsplit

# Ferramentas sem efeito no desktop: aprovadas sem perguntar.
COMPUTER_READ_ONLY = {"screenshot", "zoom", "cursor_position", "wait"}
COMPUTER_KEYBOARD = {"type", "key", "hold_key"}
BROWSER_GATED = {"javascript_exec", "file_upload"}
BROWSER_READ_ONLY = {"screenshot", "zoom", "read_page", "find", "get_page_text", "read_console", "read_network",
                     "list_tabs", "wait"}

_prompt_lock = threading.Lock()


def shown(value: object) -> str:
    """JSON com todo caractere fora do ASCII imprimível escapado: o texto pode vir da página."""
    return json.dumps(value, ensure_ascii=True, indent=2)


def ask_user(question: str) -> bool:
    with _prompt_lock:
        if not sys.stdin.isatty():
            print(f"\n[confirmação necessária, mas não há terminal interativo: recusado]\n{question}", file=sys.stderr)
            return False
        print(f"\n\033[33m{question}\033[0m", file=sys.stderr)
        try:
            answer = input("Permitir? [s/N] ").strip().lower()
        except EOFError:
            return False
        return answer in ("s", "sim", "y", "yes")


def computer_confirm(mode: str):
    """mode: 'teclado' (pergunta antes de digitar), 'tudo' (antes de toda ação com efeito) ou 'nada'."""

    def confirm(context) -> bool:
        member = context.member
        if mode == "nada" or member in COMPUTER_READ_ONLY:
            return True
        if mode == "teclado" and member not in COMPUTER_KEYBOARD:
            return True
        return ask_user(f'PC: permitir "{member}" com esta entrada?\n{shown(context.input.to_dict())}')

    return confirm


def browser_confirm(mode: str):
    """mode: 'controlados' (só javascript_exec e file_upload) ou 'tudo' (toda ação com efeito)."""
    granted: set[tuple[str, str, str]] = set()

    def confirm(context) -> bool:
        member = context.member
        if member in BROWSER_READ_ONLY:
            return True
        if mode != "tudo" and member not in BROWSER_GATED:
            return True
        detail = shown(context.input.to_dict())
        page = context.tab_url
        where = shown(page) if page else "uma aba sem URL"
        question = f'Navegador: permitir "{member}" em {where}?\n{detail}'
        if member not in BROWSER_GATED or page is None or urlsplit(page).scheme not in ("http", "https"):
            return ask_user(question)
        # Uma aprovação vale só para esta entrada exata nesta página.
        key = (member, page, detail)
        if key not in granted and ask_user(question):
            granted.add(key)
        return key in granted

    return confirm
