#!/usr/bin/env python3
"""WezTerm-style character hint picker for Herdr panes."""

from __future__ import annotations

import base64
import io
import itertools
import json
import os
from pathlib import Path
import socket
import sys
import termios
import time
import tty
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PLUGIN_ID = "ugurtarlig.pane-picker"
HINT_ALPHABET = "asdfghjklqwertyuiopzxcvbnm"
BADGE_GRID_COLS = 8
BADGE_GRID_ROWS = 4
# The badge artwork is authored in a fixed 64-unit coordinate space; rendered
# output is BADGE_IMAGE_SIZE so the terminal never has to upscale.
BADGE_DESIGN_SIZE = 64
BADGE_IMAGE_SIZE = 128
BADGE_SUPERSAMPLE = 4
REQUEST_IDS = itertools.count(1)
SYSTEM_BADGE_FONTS = (
    "/System/Library/Fonts/SFNSRounded.ttf",
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
)
BADGE_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "badges"
OVERLAY_STATE_PATH = Path.home() / ".local" / "state" / "herdr-pane-picker" / "selection.json"
OVERLAY_TIMEOUT_SECONDS = 6.0

# Compact 5x7 glyphs keep the plugin dependency-free. Hints are rendered as
# uppercase shapes for legibility but selected with their lowercase key.
FONT_5X7 = {
    char: tuple(rows.split("/"))
    for char, rows in {
        "A": "01110/10001/10001/11111/10001/10001/10001",
        "B": "11110/10001/10001/11110/10001/10001/11110",
        "C": "01111/10000/10000/10000/10000/10000/01111",
        "D": "11110/10001/10001/10001/10001/10001/11110",
        "E": "11111/10000/10000/11110/10000/10000/11111",
        "F": "11111/10000/10000/11110/10000/10000/10000",
        "G": "01111/10000/10000/10111/10001/10001/01111",
        "H": "10001/10001/10001/11111/10001/10001/10001",
        "I": "11111/00100/00100/00100/00100/00100/11111",
        "J": "00111/00010/00010/00010/10010/10010/01100",
        "K": "10001/10010/10100/11000/10100/10010/10001",
        "L": "10000/10000/10000/10000/10000/10000/11111",
        "M": "10001/11011/10101/10101/10001/10001/10001",
        "N": "10001/11001/10101/10011/10001/10001/10001",
        "O": "01110/10001/10001/10001/10001/10001/01110",
        "P": "11110/10001/10001/11110/10000/10000/10000",
        "Q": "01110/10001/10001/10001/10101/10010/01101",
        "R": "11110/10001/10001/11110/10100/10010/10001",
        "S": "01111/10000/10000/01110/00001/00001/11110",
        "T": "11111/00100/00100/00100/00100/00100/00100",
        "U": "10001/10001/10001/10001/10001/10001/01110",
        "V": "10001/10001/10001/10001/10001/01010/00100",
        "W": "10001/10001/10001/10101/10101/10101/01010",
        "X": "10001/10001/01010/00100/01010/10001/10001",
        "Y": "10001/10001/01010/00100/00100/00100/00100",
        "Z": "11111/00001/00010/00100/01000/10000/11111",
    }.items()
}


class HerdrApiError(RuntimeError):
    """A structured error returned by Herdr."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def herdr_socket_path() -> str:
    explicit = os.environ.get("HERDR_SOCKET_PATH")
    if explicit:
        return explicit
    session = os.environ.get("HERDR_SESSION")
    config = Path.home() / ".config" / "herdr"
    if session and session != "default":
        return str(config / "sessions" / session / "herdr.sock")
    return str(config / "herdr.sock")


def api_request(method: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    """Send one newline-delimited request to Herdr's Unix socket."""

    request_id = f"pane-picker-{os.getpid()}-{next(REQUEST_IDS)}"
    payload = json.dumps(
        {"id": request_id, "method": method, "params": dict(params)},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3.0)
            client.connect(herdr_socket_path())
            client.sendall(payload)
            with client.makefile("rb") as stream:
                line = stream.readline()
    except OSError as error:
        raise RuntimeError(f"could not reach Herdr: {error}") from error

    if not line:
        raise RuntimeError("Herdr closed the socket without a response")
    try:
        response = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Herdr returned an invalid response") from error
    if response.get("id") != request_id:
        raise RuntimeError("Herdr returned a response with the wrong request id")
    if "error" in response:
        body = response["error"]
        raise HerdrApiError(str(body.get("code", "unknown")), str(body.get("message", "")))
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Herdr returned a response without a result object")
    return result


def spatial_panes(layout: Mapping[str, Any]) -> List[Dict[str, Any]]:
    panes = [dict(pane) for pane in layout.get("panes", []) if isinstance(pane, dict)]
    return sorted(
        panes,
        key=lambda pane: (
            int(pane.get("rect", {}).get("y", 0)),
            int(pane.get("rect", {}).get("x", 0)),
            str(pane.get("pane_id", "")),
        ),
    )


def assign_hints(
    layout: Mapping[str, Any],
    alphabet: str = HINT_ALPHABET,
    excluded_pane_ids: Iterable[str] = (),
) -> List[Tuple[str, Dict[str, Any]]]:
    excluded = set(excluded_pane_ids)
    panes = [pane for pane in spatial_panes(layout) if pane.get("pane_id") not in excluded]
    if len(panes) > len(alphabet):
        raise RuntimeError(
            f"this tab has {len(panes)} panes, but the picker supports {len(alphabet)}"
        )
    return list(zip(alphabet, panes))


def graphics_cell_size(
    pane_id: str,
    request: Callable[[str, Mapping[str, Any]], Dict[str, Any]] = api_request,
) -> Tuple[int, int]:
    try:
        info = request("pane.graphics.info", {"pane_id": pane_id})
    except HerdrApiError as error:
        message = error.message.lower()
        if "cell size" in message and "unavailable" in message:
            raise RuntimeError(
                "Restart this Herdr client once: kitty_graphics was enabled after it started."
            ) from error
        raise
    cell_width = int(info.get("cell_width_px", 0))
    cell_height = int(info.get("cell_height_px", 0))
    if cell_width <= 0 or cell_height <= 0:
        raise RuntimeError(
            "Restart this Herdr client once: it has not reported terminal pixel geometry."
        )
    return cell_width, cell_height


def badge_placement(rect: Mapping[str, Any]) -> Dict[str, int]:
    width = max(0, int(rect.get("width", 0)))
    height = max(0, int(rect.get("height", 0)))
    return {
        "viewport_col": max(0, (width - BADGE_GRID_COLS) // 2),
        "viewport_row": max(0, (height - BADGE_GRID_ROWS) // 2),
        "grid_cols": min(BADGE_GRID_COLS, max(1, width)),
        "grid_rows": min(BADGE_GRID_ROWS, max(1, height)),
    }


def _blend_pixel(
    pixels: bytearray, width: int, x: int, y: int, color: Tuple[int, int, int, int]
) -> None:
    offset = (y * width + x) * 4
    source_alpha = color[3] / 255.0
    if source_alpha <= 0:
        return
    destination_alpha = pixels[offset + 3] / 255.0
    output_alpha = source_alpha + destination_alpha * (1.0 - source_alpha)
    if output_alpha <= 0:
        return
    for channel in range(3):
        source = color[channel] / 255.0
        destination = pixels[offset + channel] / 255.0
        output = (
            source * source_alpha
            + destination * destination_alpha * (1.0 - source_alpha)
        ) / output_alpha
        pixels[offset + channel] = round(output * 255)
    pixels[offset + 3] = round(output_alpha * 255)


def _inside_rounded_rect(x: int, y: int, width: int, height: int, radius: int) -> bool:
    if radius <= 0:
        return True
    near_left = x < radius
    near_right = x >= width - radius
    near_top = y < radius
    near_bottom = y >= height - radius
    if not ((near_left or near_right) and (near_top or near_bottom)):
        return True
    center_x = radius if near_left else width - radius - 1
    center_y = radius if near_top else height - radius - 1
    dx = x - center_x
    dy = y - center_y
    return dx * dx + dy * dy <= radius * radius


def _paint_rounded_rect(
    pixels: bytearray,
    canvas_width: int,
    x0: int,
    y0: int,
    width: int,
    height: int,
    radius: int,
    color: Tuple[int, int, int, int],
) -> None:
    for y in range(height):
        for x in range(width):
            if _inside_rounded_rect(x, y, width, height, radius):
                _blend_pixel(pixels, canvas_width, x0 + x, y0 + y, color)


def _paint_rounded_gradient(
    pixels: bytearray,
    canvas_width: int,
    x0: int,
    y0: int,
    width: int,
    height: int,
    radius: int,
    top: Tuple[int, int, int, int],
    bottom: Tuple[int, int, int, int],
) -> None:
    denominator = max(1, height - 1)
    for y in range(height):
        ratio = y / denominator
        color = tuple(
            round(top[channel] + (bottom[channel] - top[channel]) * ratio)
            for channel in range(4)
        )
        for x in range(width):
            if _inside_rounded_rect(x, y, width, height, radius):
                _blend_pixel(pixels, canvas_width, x0 + x, y0 + y, color)


def _downsample_rgba(pixels: bytearray, width: int, scale: int) -> bytes:
    output_width = width // scale
    output = bytearray(output_width * output_width * 4)
    samples = scale * scale
    for output_y in range(output_width):
        for output_x in range(output_width):
            alpha_sum = 0
            weighted = [0, 0, 0]
            for sample_y in range(scale):
                for sample_x in range(scale):
                    source_x = output_x * scale + sample_x
                    source_y = output_y * scale + sample_y
                    source = (source_y * width + source_x) * 4
                    alpha = pixels[source + 3]
                    alpha_sum += alpha
                    for channel in range(3):
                        weighted[channel] += pixels[source + channel] * alpha
            target = (output_y * output_width + output_x) * 4
            if alpha_sum:
                for channel in range(3):
                    output[target + channel] = round(weighted[channel] / alpha_sum)
            output[target + 3] = round(alpha_sum / samples)
    return bytes(output)


def render_badge_png(char: str) -> Optional[bytes]:
    """Render the normal macOS badge through Pillow's native C-backed path."""

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    font_path = next((path for path in SYSTEM_BADGE_FONTS if Path(path).is_file()), None)
    if font_path is None:
        return None
    try:
        scale = BADGE_SUPERSAMPLE * BADGE_IMAGE_SIZE // BADGE_DESIGN_SIZE
        size = BADGE_DESIGN_SIZE * scale
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        draw.rounded_rectangle(
            (7 * scale, 8 * scale, 57 * scale, 58 * scale),
            radius=15 * scale,
            fill=(6, 10, 24, 105),
        )
        draw.rounded_rectangle(
            (5 * scale, 4 * scale, 59 * scale, 58 * scale),
            radius=17 * scale,
            fill=(202, 220, 255, 245),
        )

        surface_size = 50 * scale
        gradient = Image.new("RGBA", (surface_size, surface_size), (0, 0, 0, 0))
        gradient_draw = ImageDraw.Draw(gradient)
        top = (137, 177, 255, 255)
        bottom = (76, 112, 222, 255)
        for y in range(surface_size):
            ratio = y / max(1, surface_size - 1)
            color = tuple(
                round(top[channel] + (bottom[channel] - top[channel]) * ratio)
                for channel in range(4)
            )
            gradient_draw.line((0, y, surface_size, y), fill=color)
        surface_mask = Image.new("L", (surface_size, surface_size), 0)
        ImageDraw.Draw(surface_mask).rounded_rectangle(
            (0, 0, surface_size - 1, surface_size - 1),
            radius=15 * scale,
            fill=255,
        )
        image.paste(gradient, (7 * scale, 6 * scale), surface_mask)
        highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(highlight).rounded_rectangle(
            (10 * scale, 8 * scale, 54 * scale, 23 * scale),
            radius=10 * scale,
            fill=(255, 255, 255, 24),
        )
        image = Image.alpha_composite(image, highlight)
        draw = ImageDraw.Draw(image)

        font = ImageFont.truetype(font_path, 39 * scale)
        label = char.upper()
        bounds = draw.textbbox((0, 0), label, font=font)
        glyph_width = bounds[2] - bounds[0]
        glyph_height = bounds[3] - bounds[1]
        x = (size - glyph_width) / 2 - bounds[0]
        y = (size - glyph_height) / 2 - bounds[1] - scale
        draw.text((x, y), label, font=font, fill=(250, 252, 255, 255))

        resampling = getattr(Image, "Resampling", Image).LANCZOS
        image = image.resize((BADGE_IMAGE_SIZE, BADGE_IMAGE_SIZE), resampling)
        output = io.BytesIO()
        image.save(output, format="PNG")
    except (OSError, ValueError):
        return None
    return output.getvalue()


def load_badge_png(char: str) -> Optional[bytes]:
    """Load the pre-rendered badge used by the latency-sensitive picker path."""

    try:
        payload = (BADGE_ASSET_DIR / f"{char.lower()}.png").read_bytes()
    except OSError:
        return None
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return payload


def build_badge_assets() -> int:
    """Pre-render every hint so opening the picker never initializes Pillow."""

    BADGE_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for char in HINT_ALPHABET:
        payload = render_badge_png(char)
        if payload is None:
            raise RuntimeError("Pillow and a rounded system font are required to build badges")
        (BADGE_ASSET_DIR / f"{char}.png").write_bytes(payload)
    return 0


def _paint_dot_glyph(
    pixels: bytearray, width: int, glyph: Sequence[str], scale: int
) -> None:
    glyph_cell = 5 * scale
    glyph_block = 4 * scale
    glyph_width = 5 * glyph_cell
    glyph_height = 7 * glyph_cell
    start_x = (width - glyph_width) // 2
    start_y = (width - glyph_height) // 2
    for row_index, row in enumerate(glyph):
        for column_index, bit in enumerate(row):
            if bit != "1":
                continue
            _paint_rounded_rect(
                pixels,
                width,
                start_x + column_index * glyph_cell,
                start_y + row_index * glyph_cell,
                glyph_block,
                glyph_block,
                scale,
                (250, 252, 255, 255),
            )


def render_badge_rgba(
    char: str, _cell_width: int, _cell_height: int
) -> Tuple[int, int, bytes]:
    glyph = FONT_5X7.get(char.upper())
    if glyph is None:
        raise ValueError(f"unsupported hint character: {char!r}")

    scale = BADGE_SUPERSAMPLE * BADGE_IMAGE_SIZE // BADGE_DESIGN_SIZE
    output_size = BADGE_IMAGE_SIZE
    width = BADGE_DESIGN_SIZE * scale
    pixels = bytearray(width * width * 4)

    # A soft shadow and a single luminous surface read as a keyboard key,
    # unlike the old border-plus-dark-inset design which resembled a phone.
    _paint_rounded_rect(
        pixels,
        width,
        7 * scale,
        8 * scale,
        50 * scale,
        50 * scale,
        15 * scale,
        (6, 10, 24, 105),
    )
    _paint_rounded_rect(
        pixels,
        width,
        5 * scale,
        4 * scale,
        54 * scale,
        54 * scale,
        17 * scale,
        (202, 220, 255, 245),
    )
    _paint_rounded_gradient(
        pixels,
        width,
        7 * scale,
        6 * scale,
        50 * scale,
        50 * scale,
        15 * scale,
        (137, 177, 255, 255),
        (76, 112, 222, 255),
    )
    _paint_rounded_rect(
        pixels,
        width,
        10 * scale,
        8 * scale,
        44 * scale,
        15 * scale,
        10 * scale,
        (255, 255, 255, 24),
    )

    _paint_dot_glyph(pixels, width, glyph, scale)
    return output_size, output_size, _downsample_rgba(pixels, width, width // output_size)


def render_badge_payload(
    char: str, cell_width: int, cell_height: int
) -> Tuple[str, int, int, bytes]:
    png = load_badge_png(char)
    if png is None:
        png = render_badge_png(char)
    if png is not None:
        return "png", BADGE_IMAGE_SIZE, BADGE_IMAGE_SIZE, png
    width, height, rgba = render_badge_rgba(char, cell_width, cell_height)
    return "rgba", width, height, rgba


def graphics_params(
    char: str, pane: Mapping[str, Any], cell_width: int, cell_height: int
) -> Dict[str, Any]:
    image_format, image_width, image_height, image = render_badge_payload(
        char, cell_width, cell_height
    )
    return {
        "pane_id": str(pane["pane_id"]),
        "format": image_format,
        "image_width": image_width,
        "image_height": image_height,
        "data_base64": base64.b64encode(image).decode("ascii"),
        "placement": badge_placement(pane.get("rect", {})),
    }


def clear_hints(
    pane_ids: Iterable[str], request: Callable[[str, Mapping[str, Any]], Dict[str, Any]] = api_request
) -> None:
    unique_pane_ids = list(dict.fromkeys(pane_ids))
    if not unique_pane_ids:
        return

    def clear_one(pane_id: str) -> None:
        try:
            request("pane.graphics.clear", {"pane_id": pane_id})
        except (HerdrApiError, RuntimeError) as error:
            log_error(f"could not clear hint from {pane_id}: {error}")

    # Herdr handles these tiny local-socket requests much faster sequentially.
    # Multiple simultaneous socket clients contend with its render loop and can
    # turn a few milliseconds of work into roughly a tenth of a second.
    for pane_id in unique_pane_ids:
        clear_one(pane_id)


def show_hints(
    assignments: Sequence[Tuple[str, Mapping[str, Any]]],
    cell_width: int,
    cell_height: int,
    request: Callable[[str, Mapping[str, Any]], Dict[str, Any]] = api_request,
) -> List[str]:
    prepared = [
        (
            str(pane["pane_id"]),
            graphics_params(char, pane, cell_width, cell_height),
        )
        for char, pane in assignments
    ]
    if not prepared:
        return []
    shown: List[str] = []
    try:
        for pane_id, params in prepared:
            request("pane.graphics.set", params)
            shown.append(pane_id)
    except (HerdrApiError, RuntimeError):
        clear_hints(shown, request)
        raise
    return shown


def read_selection(valid: Mapping[str, str]) -> Optional[Tuple[str, bool]]:
    """Return (pane_id, zoom); an uppercase (shifted) hint requests zoom."""

    if not sys.stdin.isatty():
        return None
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        while True:
            raw = os.read(descriptor, 1)
            if not raw or raw in {b"\x03", b"\x07", b"\x1b"}:
                return None
            try:
                key = raw.decode("ascii")
            except UnicodeDecodeError:
                continue
            if key.lower() in valid:
                return valid[key.lower()], key.isupper()
            sys.stdout.write("\a")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def popup_header(keys: str) -> None:
    sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l")
    sys.stdout.write(f"\x1b[1mPane picker\x1b[0m  {keys}  ·  Shift zooms  ·  Esc/Ctrl-G cancels")
    sys.stdout.flush()


def restore_popup_cursor() -> None:
    sys.stdout.write("\x1b[?25h")
    sys.stdout.flush()


def read_overlay_state() -> Optional[Dict[str, Any]]:
    try:
        state = json.loads(OVERLAY_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def write_overlay_state(state: Mapping[str, Any]) -> None:
    OVERLAY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OVERLAY_STATE_PATH.with_name(f".{OVERLAY_STATE_PATH.name}.{os.getpid()}")
    temporary.write_text(json.dumps(dict(state), separators=(",", ":")), encoding="utf-8")
    temporary.replace(OVERLAY_STATE_PATH)


def remove_overlay_state(token: str) -> None:
    current = read_overlay_state()
    if current is not None and current.get("token") != token:
        return
    try:
        OVERLAY_STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def use_state_socket(state: Mapping[str, Any]) -> None:
    socket_path = state.get("socket_path")
    if isinstance(socket_path, str) and socket_path:
        os.environ["HERDR_SOCKET_PATH"] = socket_path


def clear_overlay_state(
    state: Mapping[str, Any],
    request: Callable[[str, Mapping[str, Any]], Dict[str, Any]] = api_request,
) -> None:
    use_state_socket(state)
    pane_ids = state.get("pane_ids", [])
    if isinstance(pane_ids, list):
        clear_hints((str(pane_id) for pane_id in pane_ids), request=request)


def show_overlay(
    timeout_seconds: float = OVERLAY_TIMEOUT_SECONDS,
    request: Callable[[str, Mapping[str, Any]], Dict[str, Any]] = api_request,
) -> int:
    """Draw hints without a terminal UI; WezTerm owns the next-key capture."""

    current_socket_path = herdr_socket_path()
    previous = read_overlay_state()
    if previous is not None:
        clear_overlay_state(previous, request=request)
        token = previous.get("token")
        if isinstance(token, str):
            remove_overlay_state(token)
        os.environ["HERDR_SOCKET_PATH"] = current_socket_path

    result = request("pane.layout", {})
    layout = result.get("layout")
    if not isinstance(layout, dict):
        raise RuntimeError("Herdr did not return a pane layout")
    assignments = assign_hints(layout)
    if len(assignments) < 2:
        raise RuntimeError("This tab only has one pane")

    # Pre-rendered PNGs and cell-grid placement do not depend on pixel geometry.
    # Skipping pane.graphics.info removes one socket round trip from the hot path;
    # pane.graphics.set still validates that graphics are available.
    shown = show_hints(assignments, 0, 0, request=request)
    token = f"{os.getpid()}-{time.monotonic_ns()}"
    state = {
        "version": 1,
        "token": token,
        "socket_path": current_socket_path,
        "expires_at": time.time() + timeout_seconds,
        "targets": {char: str(pane["pane_id"]) for char, pane in assignments},
        "pane_ids": shown,
    }
    try:
        write_overlay_state(state)
    except OSError:
        clear_hints(shown, request=request)
        raise

    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            current = read_overlay_state()
            if current is None or current.get("token") != token:
                return 0
            time.sleep(0.025)
    finally:
        current = read_overlay_state()
        if current is not None and current.get("token") == token:
            clear_overlay_state(current, request=request)
            remove_overlay_state(token)
    return 0


def choose_overlay(
    choice: Optional[str],
    wait_seconds: float = 1.0,
    request: Callable[[str, Mapping[str, Any]], Dict[str, Any]] = api_request,
) -> int:
    """Complete the overlay-only picker from WezTerm's one-shot key table."""

    deadline = time.monotonic() + wait_seconds
    state: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        candidate = read_overlay_state()
        if candidate is not None and float(candidate.get("expires_at", 0)) > time.time():
            state = candidate
            break
        time.sleep(0.01)
    if state is None:
        raise RuntimeError("no active pane picker overlay")

    use_state_socket(state)
    targets = state.get("targets", {})
    selected = targets.get(choice.lower()) if isinstance(targets, dict) and choice else None
    if isinstance(selected, str):
        request("pane.focus", {"pane_id": selected})
        if choice and choice.isupper():
            request("pane.zoom", {"pane_id": selected, "mode": "on"})
    clear_overlay_state(state, request=request)
    token = state.get("token")
    if isinstance(token, str):
        remove_overlay_state(token)
    return 0


def show_error(message: str) -> None:
    sys.stdout.write("\x1b[2J\x1b[H\x1b[31;1mPane picker\x1b[0m\n")
    sys.stdout.write(message)
    if sys.stdin.isatty():
        sys.stdout.write(" · press any key")
        sys.stdout.flush()
        descriptor = sys.stdin.fileno()
        previous = termios.tcgetattr(descriptor)
        try:
            tty.setraw(descriptor)
            os.read(descriptor, 1)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def state_dir() -> Path:
    configured = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if configured:
        return Path(configured)
    return Path.home() / ".config" / "herdr" / "plugin-state" / PLUGIN_ID


def log_error(message: str) -> None:
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "errors.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
    except OSError:
        pass


def pick_pane() -> int:
    result = api_request("pane.layout", {})
    layout = result.get("layout")
    if not isinstance(layout, dict):
        raise RuntimeError("Herdr did not return a pane layout")
    picker_pane = os.environ.get("HERDR_PANE_ID", "")
    assignments = assign_hints(layout, excluded_pane_ids={picker_pane} if picker_pane else ())
    if len(assignments) < 2:
        show_error("This tab only has one pane.")
        return 0

    targets = {char: str(pane["pane_id"]) for char, pane in assignments}
    shown: List[str] = []
    selected: Optional[str] = None
    popup_header("".join(targets))
    try:
        cell_width, cell_height = graphics_cell_size(str(assignments[0][1]["pane_id"]))
        shown = show_hints(assignments, cell_width, cell_height)
        selected = read_selection(targets)
    finally:
        restore_popup_cursor()
        clear_hints(shown)
    if selected:
        pane_id, zoom = selected
        api_request("pane.focus", {"pane_id": pane_id})
        if zoom:
            api_request("pane.zoom", {"pane_id": pane_id, "mode": "on"})
    return 0


def open_picker() -> int:
    api_request(
        "plugin.pane.open",
        {
            "plugin_id": PLUGIN_ID,
            "entrypoint": "picker",
            "placement": "popup",
            "focus": True,
        },
    )
    return 0


def main(argv: Sequence[str]) -> int:
    command = argv[1] if len(argv) > 1 else "pick"
    if command == "build-assets":
        try:
            return build_badge_assets()
        except RuntimeError as error:
            print(f"pane-picker: {error}", file=sys.stderr)
            return 1
    if command == "show":
        try:
            return show_overlay()
        except (HerdrApiError, RuntimeError, OSError) as error:
            log_error(str(error))
            print(f"pane-picker: {error}", file=sys.stderr)
            return 1
    if command in {"choose", "cancel"}:
        choice = argv[2] if command == "choose" and len(argv) > 2 else None
        try:
            return choose_overlay(choice)
        except (HerdrApiError, RuntimeError, OSError, ValueError) as error:
            log_error(str(error))
            print(f"pane-picker: {error}", file=sys.stderr)
            return 1
    if command == "open":
        try:
            return open_picker()
        except (HerdrApiError, RuntimeError) as error:
            log_error(str(error))
            print(f"pane-picker: {error}", file=sys.stderr)
            return 1
    if command == "pick":
        try:
            return pick_pane()
        except HerdrApiError as error:
            log_error(f"{error.code}: {error.message}")
            if error.code == "feature_disabled":
                show_error("Enable [experimental] kitty_graphics = true.")
            else:
                show_error(f"Herdr error: {error.message}")
            return 1
        except RuntimeError as error:
            log_error(str(error))
            show_error(str(error))
            return 1
    print(
        f"usage: {Path(argv[0]).name} [build-assets|show|choose KEY|cancel|open|pick]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
