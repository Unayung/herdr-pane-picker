from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "pane_picker.py"
SPEC = importlib.util.spec_from_file_location("pane_picker", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pane_picker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pane_picker)


def pane(pane_id: str, x: int, y: int, width: int = 40, height: int = 20):
    return {
        "pane_id": pane_id,
        "focused": False,
        "rect": {"x": x, "y": y, "width": width, "height": height},
    }


class PaneLayoutTests(unittest.TestCase):
    def test_assigns_home_row_hints_in_spatial_order(self):
        layout = {
            "panes": [
                pane("bottom-right", 40, 20),
                pane("top-right", 40, 0),
                pane("top-left", 0, 0),
                pane("bottom-left", 0, 20),
            ]
        }

        assignments = pane_picker.assign_hints(layout)

        self.assertEqual(
            [(hint, item["pane_id"]) for hint, item in assignments],
            [
                ("a", "top-left"),
                ("s", "top-right"),
                ("d", "bottom-left"),
                ("f", "bottom-right"),
            ],
        )

    def test_rejects_more_panes_than_hints(self):
        layout = {"panes": [pane(str(index), index, 0) for index in range(3)]}
        with self.assertRaisesRegex(RuntimeError, "supports 2"):
            pane_picker.assign_hints(layout, alphabet="as")

    def test_excludes_temporary_picker_pane(self):
        layout = {"panes": [pane("target", 0, 0), pane("picker", 0, 20)]}

        assignments = pane_picker.assign_hints(layout, excluded_pane_ids={"picker"})

        self.assertEqual([(hint, item["pane_id"]) for hint, item in assignments], [("a", "target")])

    def test_centers_badge_in_pane_cells(self):
        placement = pane_picker.badge_placement({"width": 81, "height": 31})
        self.assertEqual(
            placement,
            {"viewport_col": 36, "viewport_row": 13, "grid_cols": 8, "grid_rows": 4},
        )


class BadgeRenderingTests(unittest.TestCase):
    def test_prebuilt_badge_avoids_runtime_pillow_rendering(self):
        with mock.patch.object(pane_picker, "render_badge_png") as render:
            image_format, width, height, payload = pane_picker.render_badge_payload("a", 8, 16)

        self.assertEqual((image_format, width, height), ("png", 128, 128))
        self.assertTrue(payload.startswith(b"\x89PNG"))
        render.assert_not_called()

    def test_rgba_badge_has_transparent_corners_and_opaque_content(self):
        width, height, rgba = pane_picker.render_badge_rgba("a", 10, 20)

        self.assertEqual((width, height), (128, 128))
        self.assertEqual(len(rgba), width * height * 4)
        self.assertEqual(rgba[3], 0)
        center = ((height // 2) * width + width // 2) * 4
        self.assertEqual(rgba[center + 3], 255)

    def test_graphics_payload_uses_compact_png_when_available(self):
        payload = pane_picker.graphics_params("s", pane("p2", 0, 0), 8, 16)

        self.assertEqual(payload["pane_id"], "p2")
        self.assertIn(payload["format"], {"png", "rgba"})
        self.assertGreater(len(payload["data_base64"]), 100)
        if payload["format"] == "png":
            self.assertTrue(base64.b64decode(payload["data_base64"]).startswith(b"\x89PNG"))
        self.assertEqual(payload["placement"]["grid_cols"], 8)
        self.assertEqual(payload["placement"]["grid_rows"], 4)

    def test_cell_size_requires_client_restart_when_host_pixels_are_unavailable(self):
        def request(method, params):
            raise pane_picker.HerdrApiError("host_unavailable", "host cell size is unavailable")

        with self.assertRaisesRegex(RuntimeError, "Restart this Herdr client once"):
            pane_picker.graphics_cell_size("p1", request)


class GraphicsLifecycleTests(unittest.TestCase):
    def test_partial_show_failure_clears_already_drawn_badges(self):
        calls = []

        def request(method, params):
            calls.append((method, dict(params)))
            if method == "pane.graphics.set" and params["pane_id"] == "p2":
                raise pane_picker.HerdrApiError("failed", "nope")
            return {"type": "ok"}

        assignments = [("a", pane("p1", 0, 0)), ("s", pane("p2", 40, 0))]

        with self.assertRaises(pane_picker.HerdrApiError):
            pane_picker.show_hints(assignments, 8, 16, request=request)

        self.assertIn(("pane.graphics.clear", {"pane_id": "p1"}), calls)


class OverlayPickerTests(unittest.TestCase):
    def test_overlay_only_picker_draws_without_opening_a_popup(self):
        calls = []

        def request(method, params):
            calls.append((method, dict(params)))
            if method == "pane.layout":
                return {"layout": {"panes": [pane("p1", 0, 0), pane("p2", 40, 0)]}}
            if method == "pane.graphics.info":
                return {"cell_width_px": 8, "cell_height_px": 16}
            return {"type": "ok"}

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "selection.json"
            with mock.patch.object(pane_picker, "OVERLAY_STATE_PATH", state_path):
                result = pane_picker.show_overlay(timeout_seconds=0.01, request=request)

        self.assertEqual(result, 0)
        methods = [method for method, _ in calls]
        self.assertNotIn("plugin.pane.open", methods)
        self.assertNotIn("pane.graphics.info", methods)
        self.assertEqual(methods.count("pane.graphics.set"), 2)
        self.assertEqual(methods.count("pane.graphics.clear"), 2)

    def test_wezterm_choice_focuses_target_and_clears_overlay(self):
        calls = []

        def request(method, params):
            calls.append((method, dict(params)))
            return {"type": "ok"}

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "selection.json"
            with mock.patch.object(pane_picker, "OVERLAY_STATE_PATH", state_path):
                pane_picker.write_overlay_state(
                    {
                        "token": "test-token",
                        "socket_path": "/test/herdr.sock",
                        "expires_at": time.time() + 5,
                        "targets": {"a": "p1", "s": "p2"},
                        "pane_ids": ["p1", "p2"],
                    }
                )
                with mock.patch.dict(os.environ, {}, clear=False):
                    result = pane_picker.choose_overlay("s", wait_seconds=0.1, request=request)
                self.assertFalse(state_path.exists())

        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ("pane.focus", {"pane_id": "p2"}))
        self.assertCountEqual(
            calls[1:],
            [
                ("pane.graphics.clear", {"pane_id": "p1"}),
                ("pane.graphics.clear", {"pane_id": "p2"}),
            ],
        )

    def test_uppercase_choice_focuses_then_zooms(self):
        calls = []

        def request(method, params):
            calls.append((method, dict(params)))
            return {"type": "ok"}

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "selection.json"
            with mock.patch.object(pane_picker, "OVERLAY_STATE_PATH", state_path):
                pane_picker.write_overlay_state(
                    {
                        "token": "test-token",
                        "socket_path": "/test/herdr.sock",
                        "expires_at": time.time() + 5,
                        "targets": {"a": "p1", "s": "p2"},
                        "pane_ids": ["p1", "p2"],
                    }
                )
                with mock.patch.dict(os.environ, {}, clear=False):
                    result = pane_picker.choose_overlay("S", wait_seconds=0.1, request=request)

        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ("pane.focus", {"pane_id": "p2"}))
        self.assertEqual(calls[1], ("pane.zoom", {"pane_id": "p2", "mode": "on"}))


class SocketClientTests(unittest.TestCase):
    def run_server(self, server_socket, response_factory):
        def serve():
            with server_socket, server_socket.makefile("rb") as stream:
                request = json.loads(stream.readline())
                response = response_factory(request)
                server_socket.sendall(json.dumps(response).encode("utf-8") + b"\n")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread

    def patched_client(self, client_socket):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.__exit__.side_effect = lambda *_: client_socket.close()
        client.settimeout.side_effect = client_socket.settimeout
        client.connect.return_value = None
        client.sendall.side_effect = client_socket.sendall
        client.makefile.side_effect = client_socket.makefile
        return mock.patch.object(pane_picker.socket, "socket", return_value=client)

    def test_round_trips_one_ndjson_request(self):
        client_socket, server_socket = socket.socketpair()

        def respond(request):
            self.assertEqual(request["method"], "pane.layout")
            return {"id": request["id"], "result": {"type": "pane_layout", "layout": {}}}

        thread = self.run_server(server_socket, respond)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/test/herdr.sock"}):
            with self.patched_client(client_socket):
                result = pane_picker.api_request("pane.layout", {})
        thread.join(timeout=2)

        self.assertEqual(result["type"], "pane_layout")

    def test_surfaces_structured_api_error(self):
        client_socket, server_socket = socket.socketpair()

        def respond(request):
            return {
                "id": request["id"],
                "error": {"code": "feature_disabled", "message": "graphics are disabled"},
            }

        thread = self.run_server(server_socket, respond)
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/test/herdr.sock"}):
            with self.patched_client(client_socket):
                with self.assertRaises(pane_picker.HerdrApiError) as caught:
                    pane_picker.api_request("pane.graphics.info", {"pane_id": "p1"})
        thread.join(timeout=2)

        self.assertEqual(caught.exception.code, "feature_disabled")


class ActionTests(unittest.TestCase):
    def test_open_action_launches_layout_neutral_popup(self):
        calls = []

        def request(method, params):
            calls.append((method, dict(params)))
            return {"type": "ok"}

        with mock.patch.object(pane_picker, "api_request", side_effect=request):
            with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "target"}):
                result = pane_picker.open_picker()

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [
                (
                    "plugin.pane.open",
                    {
                        "plugin_id": "ugurtarlig.pane-picker",
                        "entrypoint": "picker",
                        "placement": "popup",
                        "focus": True,
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
