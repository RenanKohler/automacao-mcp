"""Imagens sem dependências: redimensionamento (vizinho mais próximo) e codificação PNG.

As capturas da área de trabalho chegam como BGRA/BGRX cru (Windows GDI, X11). Aqui elas são
recortadas, redimensionadas para o tamanho que o modelo aceita e codificadas em PNG com zlib.
"""

from __future__ import annotations

import base64
import struct
import zlib
from operator import itemgetter

# A API não redimensiona: a imagem já deve caber nos limites do modelo (lado maior até 2576 px,
# ~3,75 MP). 1080p é o equilíbrio recomendado entre precisão e custo para uso de computador.
MAX_LONG_EDGE = 1920
MAX_PIXELS = 1920 * 1080


def fit_size(width: int, height: int, max_long: int = MAX_LONG_EDGE, max_pixels: int = MAX_PIXELS) -> tuple[int, int]:
    """Maior tamanho com a mesma proporção que cabe nos limites (nunca amplia)."""
    scale = min(1.0, max_long / max(width, height), (max_pixels / (width * height)) ** 0.5)
    return max(1, int(width * scale)), max(1, int(height * scale))


def bgra_to_rgb_scaled(
    data: bytes,
    stride: int,
    region: tuple[int, int, int, int],
    out_size: tuple[int, int],
    bytes_per_pixel: int = 4,
) -> bytes:
    """Recorta `region` (x0, y0, x1, y1, em pixels da origem) e redimensiona para `out_size`, em RGB."""
    x0, y0, x1, y1 = region
    out_w, out_h = out_size
    src_w, src_h = x1 - x0, y1 - y0
    offsets: list[int] = []
    for ox in range(out_w):
        sx = x0 + min(src_w - 1, ox * src_w // out_w)
        base = sx * bytes_per_pixel
        offsets.extend((base + 2, base + 1, base))  # BGR(A) -> RGB
    pick = itemgetter(*offsets)
    rows: list[bytes] = []
    last_sy = -1
    last_row = b""
    for oy in range(out_h):
        sy = y0 + min(src_h - 1, oy * src_h // out_h)
        if sy != last_sy:
            start = sy * stride
            last_row = bytes(pick(data[start : start + stride]))
            last_sy = sy
        rows.append(last_row)
    return b"".join(rows)


def encode_png_rgb(width: int, height: int, rgb: bytes, level: int = 6) -> bytes:
    row_len = width * 3
    raw = b"".join(b"\x00" + rgb[y * row_len : (y + 1) * row_len] for y in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack("!I", len(payload)) + tag + payload + struct.pack("!I", zlib.crc32(tag + payload))

    header = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b"")


def png_base64(width: int, height: int, rgb: bytes) -> str:
    return base64.b64encode(encode_png_rgb(width, height, rgb)).decode("ascii")


def png_size(data: bytes) -> tuple[int, int]:
    """Largura e altura lidas do cabeçalho IHDR de um PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("não é um PNG")
    return struct.unpack("!II", data[16:24])
