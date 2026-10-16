"""Driver de navegador próprio: o toolset de navegador do SDK operando o Chrome via CDP.

`NavegadorCDP` é uma subclasse de `BetaAbstractBrowserToolset20260801` que implementa todos os
membros da ferramenta de uso do navegador sobre o Chrome DevTools Protocol, sem Selenium,
Playwright ou WebDriver. O SDK chama a política de URL, a política de arquivos e `confirm` antes de
cada membro; este driver verifica, além disso, cada requisição que o navegador faz (Fetch).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anthropic.tools import ToolError
from anthropic.tools.browser import (
    BetaAbstractBrowserToolset20260801,
    BetaBrowserNavigateResult,
    BetaBrowserState,
    BetaDialogDismissed,
    BetaNavigationRefused,
    BetaScreenshotResult,
    BetaToolsetCallContext,
)
from anthropic.types.beta import BetaBrowserStateTabEntryParam

from . import dom_js
from .cdp import CDPConnection, CDPError, CDPTimeout
from .chrome import ChromeProcess, ws_url_from_http
from .politica import URLPolicy, with_scheme
from .teclas import MODIFIERS, parse_modifiers, parse_sequence
from .websocket import WebSocketClosed

log = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 30
MAX_HOLD_SECONDS = 10
MAX_REPEAT = 100
MAX_SCROLL_AMOUNT = 50
MAX_TEXT_CHARS = 100_000
READ_PAGE_LIMIT = 50_000
BUFFER_LINES = 1000
LOAD_TIMEOUT = 30.0

_MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
_MODIFIER_KEYS = {
    "Alt": ("Alt", "AltLeft", 18),
    "Control": ("Control", "ControlLeft", 17),
    "Meta": ("Meta", "MetaLeft", 91),
    "Shift": ("Shift", "ShiftLeft", 16),
}
# tecla canônica -> (key, code, windowsVirtualKeyCode, texto inserido)
_NAMED = {
    "Enter": ("Enter", "Enter", 13, "\r"),
    "Tab": ("Tab", "Tab", 9, None),
    "Escape": ("Escape", "Escape", 27, None),
    "Backspace": ("Backspace", "Backspace", 8, None),
    "Delete": ("Delete", "Delete", 46, None),
    "Insert": ("Insert", "Insert", 45, None),
    " ": (" ", "Space", 32, " "),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, None),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, None),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, None),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, None),
    "Home": ("Home", "Home", 36, None),
    "End": ("End", "End", 35, None),
    "PageUp": ("PageUp", "PageUp", 33, None),
    "PageDown": ("PageDown", "PageDown", 34, None),
    "CapsLock": ("CapsLock", "CapsLock", 20, None),
    "ContextMenu": ("ContextMenu", "ContextMenu", 93, None),
    "PrintScreen": ("PrintScreen", "PrintScreen", 44, None),
    **{f"F{i}": (f"F{i}", f"F{i}", 111 + i, None) for i in range(1, 25)},
}
_PUNCT = {
    "-": ("Minus", 189), "=": ("Equal", 187), ",": ("Comma", 188), ".": ("Period", 190), "/": ("Slash", 191),
    ";": ("Semicolon", 186), "'": ("Quote", 222), "`": ("Backquote", 192), "[": ("BracketLeft", 219),
    "]": ("BracketRight", 221), "\\": ("Backslash", 220), "+": ("Equal", 187),
}
# Atalhos de edição que o Chrome só executa via "commands" quando o evento é sintético.
_EDIT_COMMANDS = {
    ("Control", "a"): "selectAll", ("Meta", "a"): "selectAll",
    ("Control", "c"): "copy", ("Meta", "c"): "copy",
    ("Control", "x"): "cut", ("Meta", "x"): "cut",
    ("Control", "v"): "paste", ("Meta", "v"): "paste",
    ("Control", "z"): "undo", ("Meta", "z"): "undo",
    ("Control", "y"): "redo",
}
_MOUSE_BUTTONS = {"left": 1, "right": 2, "middle": 4}


def _key_event_fields(key: str) -> dict[str, Any]:
    if key in _NAMED:
        k, code, vk, text = _NAMED[key]
        fields: dict[str, Any] = {"key": k, "code": code, "windowsVirtualKeyCode": vk}
        if text:
            fields["text"] = text
        return fields
    if key in _MODIFIER_KEYS:
        k, code, vk = _MODIFIER_KEYS[key]
        return {"key": k, "code": code, "windowsVirtualKeyCode": vk}
    if len(key) == 1:
        if key.isalpha() and key.isascii():
            return {"key": key, "code": f"Key{key.upper()}", "windowsVirtualKeyCode": ord(key.upper()), "text": key}
        if key.isdigit():
            return {"key": key, "code": f"Digit{key}", "windowsVirtualKeyCode": ord(key), "text": key}
        if key in _PUNCT:
            code, vk = _PUNCT[key]
            return {"key": key, "code": code, "windowsVirtualKeyCode": vk, "text": key}
        return {"key": key, "text": key}
    raise ToolError(f'Tecla não suportada no navegador: "{key}".')


@dataclass
class Tab:
    tab_id: str
    target_id: str
    session_id: str
    url: str = ""
    title: str = ""
    main_frame: str = ""
    world_id: int | None = None
    next_ref: int = 1
    mouse: tuple[float, float] = (0.0, 0.0)
    load_count: int = 0
    doc_status: dict[str, int] = field(default_factory=dict)
    console: list[str] = field(default_factory=list)
    network: "OrderedDict[str, dict]" = field(default_factory=OrderedDict)
    ready: threading.Event = field(default_factory=threading.Event)


class NavegadorCDP(BetaAbstractBrowserToolset20260801):
    """Toolset de navegador sobre o Chrome DevTools Protocol."""

    def __init__(
        self,
        *,
        url_policy: URLPolicy,
        headless: bool = False,
        viewport: tuple[int, int] = (1280, 720),
        chrome_path: str | None = None,
        cdp_endpoint: str | None = None,
        download_dir: str | None = None,
        **options: Any,
    ) -> None:
        # read_console e read_network vêm desativados no SDK; este driver os oferece por padrão.
        configs = {"read_console": {"enabled": True}, "read_network": {"enabled": True}}
        configs.update(options.pop("configs", None) or {})
        super().__init__(url_policy=url_policy, configs=configs, **options)
        self.policy = url_policy
        self.viewport = viewport
        self.download_dir = Path(download_dir).resolve() if download_dir else None
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._tabs: "OrderedDict[str, Tab]" = OrderedDict()
        self._by_target: dict[str, Tab] = {}
        self._by_session: dict[str, Tab] = {}
        self._tab_counter = 0
        self._active: str | None = None
        self._front: str | None = None
        self._changes: list[Any] = []
        self._downloads: dict[str, dict] = {}
        self._initialized = False

        self._chrome: ChromeProcess | None = None
        if cdp_endpoint:
            ws_url = cdp_endpoint if cdp_endpoint.startswith("ws://") else ws_url_from_http(cdp_endpoint)
        else:
            self._chrome = ChromeProcess(executable=chrome_path, headless=headless,
                                         window_size=(viewport[0], viewport[1] + 120))
            ws_url = self._chrome.ws_url
        try:
            self.conn = CDPConnection(ws_url)
            self.conn.add_listener(self._on_event)
            self._start()
        except Exception:
            self._shutdown_process()
            raise

    # ------------------------------------------------------------------ inicialização

    def _start(self) -> None:
        send = self.conn.send
        if self.download_dir:
            self.download_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            send("Browser.setDownloadBehavior",
                 {"behavior": "allowAndName", "downloadPath": str(self.download_dir), "eventsEnabled": True})
        else:
            send("Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": True})
        send("Target.setDiscoverTargets", {"discover": True})
        send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        targets = send("Target.getTargets").get("targetInfos", [])
        for info in targets:
            if info.get("type") == "page" and not info.get("attached"):
                try:
                    send("Target.attachToTarget", {"targetId": info["targetId"], "flatten": True})
                except CDPError:
                    pass
        if not any(t.get("type") == "page" for t in targets):
            send("Target.createTarget", {"url": "about:blank"})
        deadline = time.monotonic() + 15
        with self._changed:
            while not any(t.ready.is_set() for t in self._tabs.values()):
                if time.monotonic() > deadline:
                    raise RuntimeError("nenhuma aba do navegador ficou pronta")
                self._changed.wait(0.2)
            first = next(t for t in self._tabs.values() if t.ready.is_set())
            self._active = first.tab_id
            self._changes.clear()  # as abas iniciais não foram abertas por uma chamada
            self._initialized = True
        self._bring_to_front(first)

    def _setup_page(self, session_id: str, info: dict, waiting: bool) -> None:
        with self._lock:
            if info["targetId"] in self._by_target:
                # já anexada por outra via (anexo automático e anexo explícito): descarta a sessão extra
                self.conn.send_nowait("Target.detachFromTarget", {"sessionId": session_id})
                return
            self._tab_counter += 1
            tab = Tab(tab_id=f"t{self._tab_counter}", target_id=info["targetId"], session_id=session_id,
                      url=info.get("url", ""), title=info.get("title", ""))
            self._tabs[tab.tab_id] = tab
            self._by_target[tab.target_id] = tab
            self._by_session[session_id] = tab
        s = lambda method, params=None: self.conn.send(method, params, session_id=session_id, timeout=10)
        try:
            s("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            s("Page.enable")
            s("Runtime.enable")
            s("Network.enable")
            s("Log.enable")
            s("Network.setBypassServiceWorker", {"bypass": True})
            s("Page.setInterceptFileChooserDialog", {"enabled": False})
            s("Emulation.setDeviceMetricsOverride", {"width": self.viewport[0], "height": self.viewport[1],
                                                      "deviceScaleFactor": 1, "mobile": False})
            s("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
            tree = s("Page.getFrameTree")
            tab.main_frame = tree["frameTree"]["frame"]["id"]
        except (CDPError, CDPTimeout, WebSocketClosed) as exc:
            log.debug("falha ao preparar a aba %s: %s", tab.tab_id, exc)
        finally:
            if waiting:
                self.conn.send_nowait("Runtime.runIfWaitingForDebugger", session_id=session_id)
        with self._changed:
            tab.ready.set()
            if self._initialized:
                self._changes.append({"type": "tab_opened", "tab_id": tab.tab_id})
            self._changed.notify_all()

    def _setup_child(self, session_id: str, waiting: bool) -> None:
        """iframes de outro processo e workers: a mesma interceptação de requisições da página."""
        try:
            self.conn.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                           session_id=session_id, timeout=10)
            self.conn.send("Target.setAutoAttach",
                           {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                           session_id=session_id, timeout=10)
        except (CDPError, CDPTimeout, WebSocketClosed):
            pass
        if waiting:
            self.conn.send_nowait("Runtime.runIfWaitingForDebugger", session_id=session_id)

    # ------------------------------------------------------------------ eventos

    def _on_event(self, method: str, params: dict, session_id: str | None) -> None:
        if method == "Fetch.requestPaused":
            self._on_request(params, session_id)
            return
        if method == "Target.attachedToTarget":
            info = params["targetInfo"]
            if info.get("type") == "page":
                self._setup_page(params["sessionId"], info, params.get("waitingForDebugger", False))
            elif info.get("type") == "service_worker":
                # Service workers não são usados: as abas os ignoram (setBypassServiceWorker).
                self.conn.send_nowait("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session_id=params["sessionId"])
                self.conn.send_nowait("Runtime.runIfWaitingForDebugger", session_id=params["sessionId"])
            else:
                self._setup_child(params["sessionId"], params.get("waitingForDebugger", False))
            return
        if method == "Target.targetInfoChanged":
            info = params["targetInfo"]
            with self._lock:
                tab = self._by_target.get(info["targetId"])
                if tab:
                    tab.url, tab.title = info.get("url", tab.url), info.get("title", tab.title)
            return
        if method in ("Target.targetDestroyed", "Target.detachedFromTarget", "Inspector.detached"):
            with self._changed:
                if method == "Target.targetDestroyed":
                    tab = self._by_target.get(params.get("targetId", ""))
                else:
                    tab = self._by_session.get(params.get("sessionId") or session_id or "")
                if tab:
                    self._remove_tab(tab)
                self._changed.notify_all()
            return
        if method == "Browser.downloadWillBegin":
            self._on_download_start(params)
            return
        if method == "Browser.downloadProgress":
            self._on_download_progress(params)
            return
        if session_id is None:
            return
        tab = self._by_session.get(session_id)
        if tab is None:
            return
        if method == "Page.loadEventFired":
            with self._changed:
                tab.load_count += 1
                self._changed.notify_all()
        elif method == "Page.frameNavigated":
            frame = params["frame"]
            if not frame.get("parentId"):
                with self._lock:
                    tab.main_frame = frame["id"]
                    tab.world_id = None
        elif method == "Runtime.executionContextsCleared":
            tab.world_id = None
        elif method == "Runtime.executionContextDestroyed":
            if params.get("executionContextId") == tab.world_id:
                tab.world_id = None
        elif method == "Page.javascriptDialogOpening":
            kind = params.get("type", "alert")
            # beforeunload é aceito para que a navegação pedida prossiga; os demais são recusados.
            self.conn.send_nowait("Page.handleJavaScriptDialog", {"accept": kind == "beforeunload"}, session_id=session_id)
            with self._lock:
                self._changes.append(BetaDialogDismissed(kind=kind, message=params.get("message", "")))
        elif method == "Network.responseReceived":
            if params.get("type") == "Document" and params.get("frameId") == tab.main_frame:
                tab.doc_status[params.get("loaderId", "")] = params["response"].get("status", 0)
            entry = tab.network.get(params["requestId"])
            if entry is not None:
                entry["status"] = params["response"].get("status")
                entry["mime"] = params["response"].get("mimeType", "")
        elif method == "Network.requestWillBeSent":
            req = params["request"]
            tab.network[params["requestId"]] = {
                "method": req.get("method", "GET"), "url": req.get("url", ""), "type": params.get("type", ""),
                "start": params.get("timestamp", 0.0),
            }
            while len(tab.network) > BUFFER_LINES:
                tab.network.popitem(last=False)
        elif method == "Network.loadingFinished":
            entry = tab.network.get(params["requestId"])
            if entry is not None:
                entry["ms"] = int((params.get("timestamp", 0.0) - entry["start"]) * 1000)
        elif method == "Network.loadingFailed":
            entry = tab.network.get(params["requestId"])
            if entry is not None:
                entry["error"] = params.get("blockedReason") or params.get("errorText", "falhou")
        elif method == "Runtime.consoleAPICalled":
            text = " ".join(_remote_to_text(a) for a in params.get("args", []))
            self._console(tab, f"[{params.get('type', 'log')}] {text}")
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            desc = details.get("exception", {}).get("description") or details.get("text", "")
            self._console(tab, f"[exception] {desc}")
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            self._console(tab, f"[{entry.get('level', 'info')}] {entry.get('text', '')} {entry.get('url', '')}".rstrip())

    def _console(self, tab: Tab, line: str) -> None:
        tab.console.append(line[:500])
        if len(tab.console) > BUFFER_LINES:
            del tab.console[: len(tab.console) - BUFFER_LINES]

    def _on_request(self, params: dict, session_id: str | None) -> None:
        url = params["request"]["url"]
        reason = None if url.startswith(("data:", "blob:")) else self.policy.reason_blocked(url)
        if reason is None:
            self.conn.send_nowait("Fetch.continueRequest", {"requestId": params["requestId"]}, session_id=session_id)
            return
        self.conn.send_nowait("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                              session_id=session_id)
        if params.get("resourceType") == "Document":
            with self._lock:
                self._changes.append(BetaNavigationRefused())

    def _on_download_start(self, params: dict) -> None:
        guid = params["guid"]
        with self._lock:
            self._downloads[guid] = {"url": params.get("url", ""), "name": params.get("suggestedFilename", "")}
            if self.download_dir:
                self._changes.append({"type": "download_started", "download_id": guid, "url": params.get("url", "")})

    def _on_download_progress(self, params: dict) -> None:
        guid = params["guid"]
        state = params.get("state")
        with self._lock:
            info = self._downloads.get(guid, {"url": "", "name": ""})
            if state == "completed" and self.download_dir:
                path = self.download_dir / guid
                final = self.download_dir / _safe_filename(info["name"] or guid)
                if final.exists():
                    final = final.with_name(f"{final.stem}-{guid[:8]}{final.suffix}")
                try:
                    path.rename(final)
                except OSError:
                    final = path
                self._changes.append({"type": "download_completed", "download_id": guid, "url": info["url"],
                                      "path": str(final), "size_bytes": int(params.get("receivedBytes", 0))})
            elif state == "canceled":
                error = "Downloads estão desativados." if not self.download_dir else "O download foi cancelado."
                self._changes.append({"type": "download_failed", "download_id": guid, "url": info["url"], "error": error})

    def _remove_tab(self, tab: Tab) -> None:
        self._tabs.pop(tab.tab_id, None)
        self._by_target.pop(tab.target_id, None)
        self._by_session.pop(tab.session_id, None)
        self._changes = [c for c in self._changes if not (isinstance(c, dict) and c.get("tab_id") == tab.tab_id)]
        if self._active == tab.tab_id:
            self._active = next(reversed(self._tabs), None) if self._tabs else None
        if self._front == tab.tab_id:
            self._front = None

    # ------------------------------------------------------------------ utilidades

    def _tab(self, tab_id: str | None) -> Tab:
        with self._lock:
            key = tab_id or self._active
            tab = self._tabs.get(key) if key else None
        if tab is None:
            if tab_id:
                raise ToolError(f"Não existe aba com tab_id {tab_id!r}.")
            raise ToolError("Nenhuma aba aberta. Use new_tab.")
        return tab

    def _send(self, tab: Tab, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        return self.conn.send(method, params, session_id=tab.session_id, timeout=timeout)

    def _entry(self, tab: Tab) -> BetaBrowserStateTabEntryParam:
        return BetaBrowserStateTabEntryParam(tab_id=tab.tab_id, title=tab.title, url=tab.url,
                                             active=tab.tab_id == self._active)

    def _bring_to_front(self, tab: Tab) -> None:
        if self._front == tab.tab_id:
            return
        try:
            self._send(tab, "Page.bringToFront", timeout=10)
            self._front = tab.tab_id
        except (CDPError, CDPTimeout):
            pass

    def _world(self, tab: Tab) -> int:
        if tab.world_id is None:
            result = self._send(tab, "Page.createIsolatedWorld",
                                {"frameId": tab.main_frame, "worldName": "automacao", "grantUniveralAccess": False})
            tab.world_id = result["executionContextId"]
        return tab.world_id

    def _dom(self, tab: Tab, fn: str, args: dict, by_value: bool = True) -> Any:
        """Chama __ag.<fn>(args) no mundo isolado da aba."""
        expression = (
            dom_js.INSTALL
            + f";__ag.setNext({tab.next_ref});"
            + f"(() => {{ const r = __ag.{fn}({json.dumps(args)}); "
            + ("if (r && typeof r === 'object' && !(r instanceof Node)) r.__next = __ag.next(); " if by_value else "")
            + "return r; })()"
        )
        for attempt in range(2):
            try:
                result = self._send(tab, "Runtime.evaluate", {
                    "expression": expression, "contextId": self._world(tab),
                    "returnByValue": by_value, "awaitPromise": False,
                })
            except CDPError as exc:
                if attempt == 0 and "context" in exc.message.lower():
                    tab.world_id = None
                    continue
                raise
            if "exceptionDetails" in result:
                details = result["exceptionDetails"]
                raise ToolError("Erro ao inspecionar a página: " + (details.get("exception", {}).get("description")
                                                                    or details.get("text", "")))
            if not by_value:
                return result["result"]
            value = result["result"].get("value")
            if isinstance(value, dict):
                tab.next_ref = max(tab.next_ref, int(value.pop("__next", tab.next_ref)))
                if value.get("error") == "stale_ref":
                    raise ToolError(f"A referência {args.get('ref')} não existe mais nesta aba. "
                                    "Chame read_page ou find novamente.")
            return value
        raise ToolError("Não foi possível acessar a página.")

    def _point(self, tab: Tab, target: Any) -> tuple[float, float]:
        if getattr(target, "type", None) == "ref":
            result = self._dom(tab, "center", {"ref": target.ref})
            if result.get("error") == "not_visible":
                raise ToolError(f"O elemento {target.ref} não está visível.")
            return float(result["x"]), float(result["y"])
        x, y = target.x, target.y
        width, height = self.viewport
        if not (0 <= x < width and 0 <= y < height):
            raise ToolError(f"[{x}, {y}] está fora da área visível de {width}x{height}.")
        return float(x), float(y)

    def _mouse(self, tab: Tab, kind: str, x: float, y: float, button: str = "none",
               click_count: int = 0, modifiers: int = 0, **extra: Any) -> None:
        params = {"type": kind, "x": x, "y": y, "button": button, "clickCount": click_count, "modifiers": modifiers,
                  "buttons": _MOUSE_BUTTONS.get(button, 0) if kind == "mousePressed" else 0, **extra}
        self._send(tab, "Input.dispatchMouseEvent", params)
        tab.mouse = (x, y)

    def _click(self, input: Any, button: str, count: int) -> None:
        tab = self._tab(input.tab_id)
        x, y = self._point(tab, input.target)
        mods = sum(_MODIFIER_BITS[m] for m in parse_modifiers(getattr(input, "modifiers", None)))
        self._mouse(tab, "mouseMoved", x, y, modifiers=mods)
        for n in range(1, count + 1):
            self._mouse(tab, "mousePressed", x, y, button, n, mods)
            self._mouse(tab, "mouseReleased", x, y, button, n, mods)
        self._settle(tab)

    def _settle(self, tab: Tab, seconds: float = 0.3) -> None:
        """Pausa curta após uma ação para a página reagir (navegação iniciada por clique, por exemplo)."""
        before = tab.load_count
        time.sleep(seconds)
        if tab.load_count == before and tab.url and self._is_loading(tab):
            self._wait_load(tab, before, 10)

    def _is_loading(self, tab: Tab) -> bool:
        try:
            result = self._send(tab, "Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True,
                                                          "contextId": self._world(tab)}, timeout=5)
            return result["result"].get("value") != "complete"
        except (CDPError, CDPTimeout, KeyError):
            return False

    def _wait_load(self, tab: Tab, before: int, timeout: float = LOAD_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        with self._changed:
            while tab.load_count <= before:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or tab.tab_id not in self._tabs:
                    return False
                self._changed.wait(remaining)
        return True

    def _key(self, tab: Tab, kind: str, key: str, modifiers: int, commands: list[str] | None = None) -> None:
        fields = _key_event_fields(key)
        params: dict[str, Any] = {"type": kind, "modifiers": modifiers, **fields}
        if kind == "keyUp":
            params.pop("text", None)
        elif "text" not in fields or modifiers & (_MODIFIER_BITS["Control"] | _MODIFIER_BITS["Meta"] | _MODIFIER_BITS["Alt"]):
            params["type"] = "rawKeyDown"
            params.pop("text", None)
        if commands:
            params["commands"] = commands
        self._send(tab, "Input.dispatchKeyEvent", params)

    def _press_combo(self, tab: Tab, mods: list[str], key: str | None) -> None:
        bits = 0
        for m in mods:
            bits |= _MODIFIER_BITS[m]
            self._key(tab, "keyDown", m, bits)
        if key is not None:
            lookup = key.lower() if len(key) == 1 else key
            command = _EDIT_COMMANDS.get((mods[0], lookup)) if len(mods) == 1 else None
            if len(key) == 1 and bits & _MODIFIER_BITS["Shift"] and key.isalpha():
                key = key.upper()
            self._key(tab, "keyDown", key, bits, [command] if command else None)
            self._key(tab, "keyUp", key, bits)
        for m in reversed(mods):
            bits &= ~_MODIFIER_BITS[m]
            self._key(tab, "keyUp", m, bits)

    # ------------------------------------------------------------------ estado

    def _browser_state(self, context: BetaToolsetCallContext) -> BetaBrowserState:
        with self._lock:
            tabs = [self._entry(t) for t in self._tabs.values() if t.ready.is_set()]
            if tabs and not any(t["active"] for t in tabs):
                self._active = tabs[-1]["tab_id"]
                tabs[-1]["active"] = True
            open_ids = {t["tab_id"] for t in tabs}
            changes = [c for c in self._changes
                       if not (isinstance(c, dict) and c.get("type") == "tab_opened" and c["tab_id"] not in open_ids)]
            self._changes = []
        return BetaBrowserState(tabs=tabs, state_changes=changes)

    def execute(self, context: BetaToolsetCallContext, name: Any, input: Any) -> Any:
        try:
            return super().execute(context, name, input)
        except ToolError:
            raise
        except CDPTimeout:
            raise ToolError("O navegador não respondeu a tempo.") from None
        except CDPError as exc:
            raise ToolError(f"O navegador recusou a ação: {exc.message}") from None
        except WebSocketClosed:
            raise ToolError("A conexão com o navegador foi encerrada.") from None

    # ------------------------------------------------------------------ navegação e abas

    def navigate(self, context: BetaToolsetCallContext, input: Any) -> BetaBrowserNavigateResult:
        tab = self._tab(input.tab_id)
        self._bring_to_front(tab)
        target = input.url.strip()
        before = tab.load_count
        loader_id = ""
        if target.lower() in ("back", "forward"):
            history = self._send(tab, "Page.getNavigationHistory")
            index = history["currentIndex"] + (-1 if target.lower() == "back" else 1)
            if not 0 <= index < len(history["entries"]):
                raise ToolError("Não há página para onde voltar." if target.lower() == "back"
                                else "Não há página para onde avançar.")
            entry_url = history["entries"][index]["url"]
            if not self.policy.is_allowed(entry_url):
                raise ToolError("Navegação recusada pela política de URL.")
            self._send(tab, "Page.navigateToHistoryEntry", {"entryId": history["entries"][index]["id"]})
            self._wait_load(tab, before, 10)
        elif target.lower() == "reload":
            self._send(tab, "Page.reload", {"ignoreCache": False})
            self._wait_load(tab, before)
        else:
            url = with_scheme(target)
            result = self._send(tab, "Page.navigate", {"url": url})
            error = result.get("errorText")
            if error:
                if "BLOCKED_BY_CLIENT" in error:
                    raise ToolError("Navegação bloqueada pela política de URL.")
                raise ToolError(f"Não foi possível abrir a página: {error}")
            loader_id = result.get("loaderId", "")
            if loader_id:  # sem loaderId: navegação dentro do mesmo documento (âncora)
                self._wait_load(tab, before)
        history = self._send(tab, "Page.getNavigationHistory")
        current = history["entries"][history["currentIndex"]]
        with self._lock:
            tab.url, tab.title = current.get("url", tab.url), current.get("title", tab.title)
        status = tab.doc_status.get(loader_id) if loader_id else None
        return BetaBrowserNavigateResult(url=tab.url, status=status, title=tab.title)

    def new_tab(self, context: BetaToolsetCallContext, input: Any) -> BetaBrowserStateTabEntryParam:
        target_id = self.conn.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        deadline = time.monotonic() + 15
        with self._changed:
            while not ((tab := self._by_target.get(target_id)) and tab.ready.is_set()):
                if time.monotonic() > deadline:
                    raise ToolError("A nova aba não ficou pronta a tempo.")
                self._changed.wait(0.2)
            self._active = tab.tab_id
        self._bring_to_front(tab)
        return self._entry(tab)

    def list_tabs(self, context: BetaToolsetCallContext, input: Any) -> list[BetaBrowserStateTabEntryParam]:
        with self._lock:
            return [self._entry(t) for t in self._tabs.values() if t.ready.is_set()]

    def switch_tab(self, context: BetaToolsetCallContext, input: Any) -> BetaBrowserStateTabEntryParam:
        tab = self._tab(input.tab_id)
        with self._lock:
            self._active = tab.tab_id
        self._bring_to_front(tab)
        return self._entry(tab)

    def close_tab(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        self.conn.send("Target.closeTarget", {"targetId": tab.target_id})
        deadline = time.monotonic() + 10
        with self._changed:
            while tab.tab_id in self._tabs and time.monotonic() < deadline:
                self._changed.wait(0.2)
            if tab.tab_id in self._tabs:
                self._remove_tab(tab)
        if self._active and self._active in self._tabs:
            self._bring_to_front(self._tabs[self._active])

    # ------------------------------------------------------------------ leitura

    def screenshot(self, context: BetaToolsetCallContext, input: Any) -> BetaScreenshotResult:
        tab = self._tab(input.tab_id)
        self._bring_to_front(tab)
        data = self._send(tab, "Page.captureScreenshot", {"format": "png", "fromSurface": True})["data"]
        return BetaScreenshotResult(data=data, media_type="image/png")

    def zoom(self, context: BetaToolsetCallContext, input: Any) -> BetaScreenshotResult:
        tab = self._tab(input.tab_id)
        if len(input.region) != 4:
            raise ToolError("region deve ser [x0, y0, x1, y1].")
        x0, y0, x1, y1 = input.region
        width, height = self.viewport
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ToolError(f"region {input.region} precisa estar dentro de {width}x{height}, com x0 < x1 e y0 < y1.")
        w, h = x1 - x0, y1 - y0
        scale = max(1.0, min(4.0, 1280 / w, 800 / h))
        self._bring_to_front(tab)
        data = self._send(tab, "Page.captureScreenshot", {
            "format": "png", "fromSurface": True,
            "clip": {"x": x0, "y": y0, "width": w, "height": h, "scale": scale},
        })["data"]
        return BetaScreenshotResult(data=data, media_type="image/png")

    def read_page(self, context: BetaToolsetCallContext, input: Any) -> str:
        tab = self._tab(input.tab_id)
        result = self._dom(tab, "readPage", {"filter": input.filter, "depth": input.depth, "ref": input.ref,
                                             "limit": READ_PAGE_LIMIT})
        header = (f"Página: {tab.title} | {tab.url}\nViewport {result['viewport'][0]}x{result['viewport'][1]}, "
                  f"rolagem {result['scrollY']} de {result['height']} px")
        body = result["text"] or "(nenhum elemento visível)"
        if result.get("truncated"):
            body += "\n[saída truncada em 50.000 caracteres: use ref ou um depth menor]"
        return f"{header}\n{body}"

    def find(self, context: BetaToolsetCallContext, input: Any) -> str:
        tab = self._tab(input.tab_id)
        result = self._dom(tab, "find", {"query": input.query, "max": 20})
        return result["text"] or f'Nenhum elemento encontrado para "{input.query}".'

    def get_page_text(self, context: BetaToolsetCallContext, input: Any) -> str:
        tab = self._tab(input.tab_id)
        result = self._dom(tab, "pageText", {"limit": MAX_TEXT_CHARS})
        return f"{tab.title}\n{tab.url}\n\n{result['text']}"

    def read_console(self, context: BetaToolsetCallContext, input: Any) -> str:
        tab = self._tab(input.tab_id)
        lines, tab.console = tab.console, []
        return "\n".join(lines)

    def read_network(self, context: BetaToolsetCallContext, input: Any) -> str:
        tab = self._tab(input.tab_id)
        entries, tab.network = list(tab.network.values()), OrderedDict()
        lines = []
        for e in entries:
            status = e.get("error") or e.get("status") or "pendente"
            timing = f" {e['ms']}ms" if "ms" in e else ""
            lines.append(f"{e['method']} {status} {e.get('mime', '')}{timing} {e['url'][:300]}".replace("  ", " "))
        return "\n".join(lines)

    # ------------------------------------------------------------------ mouse

    def left_click(self, context: BetaToolsetCallContext, input: Any) -> None:
        self._click(input, "left", 1)

    def right_click(self, context: BetaToolsetCallContext, input: Any) -> None:
        self._click(input, "right", 1)

    def middle_click(self, context: BetaToolsetCallContext, input: Any) -> None:
        self._click(input, "middle", 1)

    def double_click(self, context: BetaToolsetCallContext, input: Any) -> None:
        self._click(input, "left", 2)

    def triple_click(self, context: BetaToolsetCallContext, input: Any) -> None:
        self._click(input, "left", 3)

    def hover(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        x, y = self._point(tab, input.target)
        self._mouse(tab, "mouseMoved", x, y)

    def mouse_move(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        x, y = self._point(tab, input.target)
        self._mouse(tab, "mouseMoved", x, y)

    def left_mouse_down(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        x, y = self._point(tab, input.target)
        self._mouse(tab, "mouseMoved", x, y)
        self._mouse(tab, "mousePressed", x, y, "left", 1)

    def left_mouse_up(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        x, y = self._point(tab, input.target)
        self._mouse(tab, "mouseMoved", x, y, buttons=1)
        self._mouse(tab, "mouseReleased", x, y, "left", 1)

    def left_click_drag(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        x0, y0 = self._point(tab, input.from_)
        x1, y1 = self._point(tab, input.target)
        self._mouse(tab, "mouseMoved", x0, y0)
        self._mouse(tab, "mousePressed", x0, y0, "left", 1)
        steps = 10
        for i in range(1, steps + 1):
            self._mouse(tab, "mouseMoved", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, buttons=1)
            time.sleep(0.02)
        self._mouse(tab, "mouseReleased", x1, y1, "left", 1)

    def scroll(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        x, y = self._point(tab, input.target)
        amount = 3 if input.scroll_amount is None else input.scroll_amount
        if not 0 < amount <= MAX_SCROLL_AMOUNT:
            raise ToolError(f"scroll_amount deve estar entre 1 e {MAX_SCROLL_AMOUNT}.")
        dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}[input.scroll_direction]
        self._mouse(tab, "mouseWheel", x, y, deltaX=dx * amount * 100, deltaY=dy * amount * 100)
        time.sleep(0.3)

    def scroll_to(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        self._dom(tab, "scrollTo", {"ref": input.target.ref})
        time.sleep(0.2)

    # ------------------------------------------------------------------ teclado e formulários

    def type(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        if len(input.text) > MAX_TEXT_CHARS:
            raise ToolError("Texto longo demais para digitar de uma vez.")
        self._send(tab, "Input.insertText", {"text": input.text})

    def key(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        repeat = 1 if input.repeat is None else input.repeat
        if not 1 <= repeat <= MAX_REPEAT:
            raise ToolError(f"repeat deve estar entre 1 e {MAX_REPEAT}.")
        sequence = parse_sequence(input.text)
        for _ in range(repeat):
            for mods, key in sequence:
                self._press_combo(tab, mods, key)
        self._settle(tab, 0.2)

    def hold_key(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        if not 0 < input.duration <= MAX_HOLD_SECONDS:
            raise ToolError(f"duration deve estar entre 0 e {MAX_HOLD_SECONDS} segundos.")
        sequence = parse_sequence(input.text)
        if len(sequence) != 1:
            raise ToolError("hold_key aceita uma única combinação de teclas.")
        mods, key = sequence[0]
        keys = mods + ([key] if key else [])
        bits = 0
        for k in keys:
            if k in MODIFIERS:
                bits |= _MODIFIER_BITS[k]
            self._key(tab, "keyDown", k, bits)
        time.sleep(input.duration)
        for k in reversed(keys):
            self._key(tab, "keyUp", k, bits)
            if k in MODIFIERS:
                bits &= ~_MODIFIER_BITS[k]

    def form_input(self, context: BetaToolsetCallContext, input: Any) -> str | None:
        tab = self._tab(input.tab_id)
        result = self._dom(tab, "formInput", {"ref": input.target.ref, "value": input.value})
        error = result.get("error")
        if error == "option_not_found":
            raise ToolError("Opção não encontrada. Opções disponíveis: " + "; ".join(result.get("options", [])))
        if error == "not_a_form_field":
            raise ToolError(f"{input.target.ref} não é um campo de formulário.")
        return f"Valor definido: {result['set']}" if "set" in result else None

    def wait(self, context: BetaToolsetCallContext, input: Any) -> None:
        if not 0 <= input.duration <= MAX_WAIT_SECONDS:
            raise ToolError(f"duration deve estar entre 0 e {MAX_WAIT_SECONDS} segundos.")
        time.sleep(input.duration)

    # ------------------------------------------------------------------ membros controlados (desativados por padrão)

    def javascript_exec(self, context: BetaToolsetCallContext, input: Any) -> str:
        tab = self._tab(input.tab_id)
        result = self._send(tab, "Runtime.evaluate", {
            "expression": input.text, "returnByValue": True, "awaitPromise": True, "replMode": True,
            "userGesture": True, "timeout": 20000,
        })
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise ToolError(details.get("exception", {}).get("description") or details.get("text", "Erro de script"))
        return _remote_to_text(result["result"], limit=MAX_TEXT_CHARS)

    def file_upload(self, context: BetaToolsetCallContext, input: Any) -> None:
        tab = self._tab(input.tab_id)
        if input.document_ids:
            raise ToolError("document_ids não é suportado por este driver: use paths.")
        if not input.paths:
            raise ToolError("Informe ao menos um caminho em paths.")
        remote = self._dom(tab, "get", input.target.ref, by_value=False)
        object_id = remote.get("objectId")
        if not object_id or remote.get("subtype") != "node":
            raise ToolError(f"A referência {input.target.ref} não existe mais nesta aba.")
        self._send(tab, "DOM.setFileInputFiles", {"files": list(input.paths), "objectId": object_id})

    # ------------------------------------------------------------------ encerramento

    def close(self) -> None:
        super().close()
        try:
            self.conn.close()
        finally:
            self._shutdown_process()

    def _shutdown_process(self) -> None:
        if self._chrome is not None:
            self._chrome.close()
            self._chrome = None


def _remote_to_text(obj: dict, limit: int = 2000) -> str:
    if "value" in obj:
        value = obj["value"]
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    elif "unserializableValue" in obj:
        text = obj["unserializableValue"]
    elif obj.get("type") == "undefined":
        text = "undefined"
    else:
        text = obj.get("description", obj.get("type", ""))
    return text[:limit]


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", name).strip(" .")[:150]
    return name or f"download-{uuid.uuid4().hex[:8]}"
