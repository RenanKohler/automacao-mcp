"""Driver de PC próprio: o toolset de computador do SDK sobre os backends nativos.

`ComputadorNativo` é uma subclasse de `BetaAbstractComputerToolset20260801` que implementa as 17
ferramentas de uso de computador. As capturas são reduzidas para caber nos limites de imagem do
modelo; cada coordenada que o modelo envia é convertida de volta para pixels da tela.
"""

from __future__ import annotations

import time
from typing import Any

from anthropic.tools import ToolError
from anthropic.tools.computer import (
    BetaAbstractComputerToolset20260801,
    BetaComputerCursorPositionResult,
    BetaScreenshotResult,
    BetaToolsetCallContext,
)

from .desktop import Desktop, open_desktop
from .imagem import bgra_to_rgb_scaled, fit_size, png_base64
from .teclas import parse_modifiers, parse_sequence

MAX_WAIT_SECONDS = 30
MAX_HOLD_SECONDS = 10
MAX_REPEAT = 100
MAX_SCROLL_AMOUNT = 50
MAX_TYPE_CHARS = 5000
ZOOM_LONG_EDGE = 1280


class ComputadorNativo(BetaAbstractComputerToolset20260801):
    def __init__(self, desktop: Desktop | None = None, *, action_delay: float = 0.15,
                 max_shot: tuple[int, int] = (1920, 1080), **options: Any) -> None:
        super().__init__(**options)
        self.desktop = desktop or open_desktop()
        self.action_delay = action_delay
        self.display_size = self.desktop.size()
        # O tamanho das capturas é fixo durante a sessão: as coordenadas do modelo se referem a ele.
        self.shot_size = fit_size(*self.display_size, max_long=max(max_shot), max_pixels=max_shot[0] * max_shot[1])

    # ------------------------------------------------------------------ coordenadas

    def _to_display(self, coordinate: list[int] | None, field: str = "coordinate") -> tuple[int, int]:
        if coordinate is None:
            return self.desktop.cursor()
        if len(coordinate) != 2:
            raise ToolError(f"{field} deve ser [x, y].")
        x, y = coordinate
        width, height = self.shot_size
        if not (0 <= x < width and 0 <= y < height):
            # Recusar em vez de ajustar: mover o ponto clicaria onde o modelo não escolheu.
            raise ToolError(f"[{x}, {y}] está fora da captura de {width}x{height}.")
        return x * self.display_size[0] // width, y * self.display_size[1] // height

    def _to_screenshot(self, x: int, y: int) -> tuple[int, int]:
        width, height = self.display_size
        sx = min(self.shot_size[0] - 1, max(0, x * self.shot_size[0] // width))
        sy = min(self.shot_size[1] - 1, max(0, y * self.shot_size[1] // height))
        return sx, sy

    # ------------------------------------------------------------------ utilidades

    def _pause(self) -> None:
        time.sleep(self.action_delay)

    def _with_modifiers(self, text: str | None, action) -> None:
        mods = parse_modifiers(text)
        for m in mods:
            self.desktop.key(m, True)
        try:
            action()
        finally:
            for m in reversed(mods):
                self.desktop.key(m, False)

    def _click(self, input: Any, button: str, count: int) -> None:
        x, y = self._to_display(input.coordinate)

        def action() -> None:
            self.desktop.move(x, y)
            time.sleep(0.03)
            for i in range(count):
                self.desktop.button(button, True)
                self.desktop.button(button, False)
                if i + 1 < count:
                    time.sleep(0.06)

        self._with_modifiers(input.text, action)
        self._pause()

    def _press_combo(self, mods: list[str], key: str | None) -> None:
        keys = mods + ([key] if key else [])
        pressed: list[str] = []
        try:
            for k in keys:
                self.desktop.key(k, True)
                pressed.append(k)
        finally:
            for k in reversed(pressed):
                self.desktop.key(k, False)

    # ------------------------------------------------------------------ tela

    def screenshot(self, context: BetaToolsetCallContext, input: Any) -> BetaScreenshotResult:
        width, height, stride, data = self.desktop.capture()
        out_w, out_h = self.shot_size
        rgb = bgra_to_rgb_scaled(data, stride, (0, 0, width, height), (out_w, out_h))
        return BetaScreenshotResult(data=png_base64(out_w, out_h, rgb), media_type="image/png")

    def zoom(self, context: BetaToolsetCallContext, input: Any) -> BetaScreenshotResult:
        if len(input.region) != 4:
            raise ToolError("region deve ser [x0, y0, x1, y1].")
        x0, y0, x1, y1 = input.region
        if not (x0 < x1 and y0 < y1):
            raise ToolError("region precisa ter x0 < x1 e y0 < y1.")
        dx0, dy0 = self._to_display([x0, y0], "region")
        width, height = self.shot_size
        if not (0 < x1 <= width and 0 < y1 <= height):
            raise ToolError(f"region {input.region} precisa estar dentro de {width}x{height}.")
        dx1, dy1 = x1 * self.display_size[0] // width, y1 * self.display_size[1] // height
        dx1, dy1 = max(dx1, dx0 + 1), max(dy1, dy0 + 1)
        rw, rh = dx1 - dx0, dy1 - dy0
        factor = min(4.0, ZOOM_LONG_EDGE / max(rw, rh))
        out_w, out_h = fit_size(max(1, int(rw * factor)), max(1, int(rh * factor)))
        cap_w, cap_h, stride, data = self.desktop.capture()
        rgb = bgra_to_rgb_scaled(data, stride, (dx0, dy0, min(dx1, cap_w), min(dy1, cap_h)), (out_w, out_h))
        return BetaScreenshotResult(data=png_base64(out_w, out_h, rgb), media_type="image/png")

    def cursor_position(self, context: BetaToolsetCallContext, input: Any) -> BetaComputerCursorPositionResult:
        x, y = self._to_screenshot(*self.desktop.cursor())
        return BetaComputerCursorPositionResult(x=x, y=y)

    def wait(self, context: BetaToolsetCallContext, input: Any) -> None:
        if not 0 <= input.duration <= MAX_WAIT_SECONDS:
            raise ToolError(f"duration deve estar entre 0 e {MAX_WAIT_SECONDS} segundos.")
        time.sleep(input.duration)

    # ------------------------------------------------------------------ mouse

    def mouse_move(self, context: BetaToolsetCallContext, input: Any) -> None:
        x, y = self._to_display(input.coordinate)
        self.desktop.move(x, y)
        self._pause()

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

    def left_mouse_down(self, context: BetaToolsetCallContext, input: Any) -> None:
        self.desktop.button("left", True)

    def left_mouse_up(self, context: BetaToolsetCallContext, input: Any) -> None:
        self.desktop.button("left", False)
        self._pause()

    def left_click_drag(self, context: BetaToolsetCallContext, input: Any) -> None:
        x0, y0 = self._to_display(input.start_coordinate, "start_coordinate")
        x1, y1 = self._to_display(input.coordinate)

        def action() -> None:
            self.desktop.move(x0, y0)
            time.sleep(0.05)
            self.desktop.button("left", True)
            try:
                steps = 15
                for i in range(1, steps + 1):
                    self.desktop.move(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)
                    time.sleep(0.015)
            finally:
                self.desktop.button("left", False)

        self._with_modifiers(input.text, action)
        self._pause()

    def scroll(self, context: BetaToolsetCallContext, input: Any) -> None:
        if not 0 < input.scroll_amount <= MAX_SCROLL_AMOUNT:
            raise ToolError(f"scroll_amount deve estar entre 1 e {MAX_SCROLL_AMOUNT}.")
        x, y = self._to_display(input.coordinate)

        def action() -> None:
            self.desktop.move(x, y)
            time.sleep(0.03)
            self.desktop.wheel(input.scroll_direction, input.scroll_amount)

        self._with_modifiers(input.text, action)
        self._pause()

    # ------------------------------------------------------------------ teclado

    def type(self, context: BetaToolsetCallContext, input: Any) -> None:
        if len(input.text) > MAX_TYPE_CHARS:
            raise ToolError(f"Texto com mais de {MAX_TYPE_CHARS} caracteres: divida em partes.")
        self.desktop.type_text(input.text)
        self._pause()

    def key(self, context: BetaToolsetCallContext, input: Any) -> None:
        repeat = 1 if input.repeat is None else input.repeat
        if not 1 <= repeat <= MAX_REPEAT:
            raise ToolError(f"repeat deve estar entre 1 e {MAX_REPEAT}.")
        sequence = parse_sequence(input.text)
        for _ in range(repeat):
            for mods, key in sequence:
                self._press_combo(mods, key)
                time.sleep(0.02)
        self._pause()

    def hold_key(self, context: BetaToolsetCallContext, input: Any) -> None:
        if not 0 < input.duration <= MAX_HOLD_SECONDS:
            raise ToolError(f"duration deve estar entre 1 e {MAX_HOLD_SECONDS} segundos.")
        sequence = parse_sequence(input.text)
        if len(sequence) != 1:
            raise ToolError("hold_key aceita uma única combinação de teclas.")
        mods, key = sequence[0]
        keys = mods + ([key] if key else [])
        pressed: list[str] = []
        try:
            for k in keys:
                self.desktop.key(k, True)
                pressed.append(k)
            time.sleep(input.duration)
        finally:
            for k in reversed(pressed):
                self.desktop.key(k, False)

    def close(self) -> None:
        super().close()
        self.desktop.close()
