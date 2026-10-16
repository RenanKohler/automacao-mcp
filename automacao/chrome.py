"""Localiza, inicia e encerra o Chrome/Chromium com a porta de depuração remota aberta."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

_WINDOWS_CANDIDATES = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Chromium\Application\chrome.exe",
]
_MAC_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
_LINUX_NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"]


def find_chrome() -> str:
    """Caminho do executável: CHROME_PATH, depois os locais usuais de cada sistema."""
    override = os.environ.get("CHROME_PATH")
    if override:
        if Path(override).is_file():
            return override
        raise FileNotFoundError(f"CHROME_PATH aponta para um arquivo inexistente: {override}")
    if sys.platform == "win32":
        candidates = [os.path.expandvars(p) for p in _WINDOWS_CANDIDATES]
    elif sys.platform == "darwin":
        candidates = list(_MAC_CANDIDATES)
    else:
        candidates = [found for name in _LINUX_NAMES if (found := shutil.which(name))]
        candidates.append("/opt/pw-browsers/chromium")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("Chrome/Chromium não encontrado. Defina CHROME_PATH com o caminho do executável.")


class ChromeProcess:
    """Um Chrome iniciado por nós, com perfil novo e temporário (sem contas conectadas)."""

    def __init__(
        self,
        executable: str | None = None,
        headless: bool = False,
        window_size: tuple[int, int] = (1280, 800),
        extra_args: list[str] | None = None,
    ) -> None:
        self.executable = executable or find_chrome()
        self.profile_dir = tempfile.mkdtemp(prefix="automacao-chrome-")
        args = [
            self.executable,
            f"--user-data-dir={self.profile_dir}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--disable-sync",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            "--password-store=basic",
            f"--window-size={window_size[0]},{window_size[1]}",
        ]
        if headless:
            args.append("--headless=new")
        if sys.platform.startswith("linux") and os.geteuid() == 0:
            args.append("--no-sandbox")  # o Chromium recusa rodar como root com sandbox
        args.extend(extra_args or [])
        args.extend(shlex.split(os.environ.get("CHROME_ARGS", "")))  # ex.: em container, "--no-sandbox"
        args.append("about:blank")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self.ws_url = self._wait_for_endpoint()

    def _wait_for_endpoint(self, timeout: float = 30.0) -> str:
        port_file = Path(self.profile_dir) / "DevToolsActivePort"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"o Chrome encerrou ao iniciar (código {self.process.returncode})")
            if port_file.is_file():
                lines = port_file.read_text().splitlines()
                if len(lines) >= 2 and lines[0].strip().isdigit():
                    return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
            time.sleep(0.1)
        self.close()
        raise TimeoutError("o Chrome não abriu a porta de depuração a tempo")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        shutil.rmtree(self.profile_dir, ignore_errors=True)


def ws_url_from_http(endpoint: str) -> str:
    """Endereço WebSocket de um Chrome já aberto com --remote-debugging-port (ex.: http://127.0.0.1:9222)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # endereço local: sem proxy
    with opener.open(endpoint.rstrip("/") + "/json/version", timeout=10) as response:
        return json.load(response)["webSocketDebuggerUrl"]
