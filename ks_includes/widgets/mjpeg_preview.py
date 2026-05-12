import logging
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Gtk

import requests

MAX_FPS = 4.0
MIN_FRAME_INTERVAL = 1.0 / MAX_FPS
STREAM_BUFFER_LIMIT = 1_000_000


class MainMenuCameraPreview:
    """Realtime camera preview with frame throttling."""

    def __init__(self, screen, url):
        self._screen = screen
        self.url = url
        self._running = False
        self._thread = None
        self._last_raw_pixbuf = None
        self._raw_lock = threading.Lock()
        self._session = requests.Session()
        self._last_frame_at = 0.0

        self.image = Gtk.Image(hexpand=True, vexpand=True)
        self.image.set_from_pixbuf(self._solid_pixbuf(4, 3, 0x1A, 0x1D, 0x26))

        self.preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.preview_box.pack_start(self.image, True, True, 0)

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.widget.get_style_context().add_class("camera-preview-pane")
        self.widget.pack_start(self.preview_box, True, True, 0)

        self.widget.connect_after("size-allocate", lambda *_: GLib.idle_add(self._refresh_scaled))
        self.widget.show_all()

    @staticmethod
    def _solid_pixbuf(width, height, r, g, b):
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, max(1, width), max(1, height))
        pixbuf.fill(((r << 24) | (g << 16) | (b << 8) | 0xFF) & 0xFFFFFFFF)
        return pixbuf

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            self._running = True
            return
        self._running = True
        self._thread = threading.Thread(target=self._feed_loop, name="mjpeg_preview", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

    def _feed_loop(self):
        while self._running:
            try:
                with self._session.get(
                    self.url,
                    stream=True,
                    timeout=(4.0, None),
                    headers={"Connection": "close", "Cache-Control": "no-cache"},
                ) as response:
                    response.raise_for_status()
                    buffer = b""
                    for chunk in response.iter_content(chunk_size=4096):
                        if not self._running:
                            break
                        if not chunk:
                            continue
                        buffer += chunk
                        if len(buffer) > STREAM_BUFFER_LIMIT:
                            buffer = buffer[-200_000:]

                        while self._running:
                            start = buffer.find(b"\xff\xd8")
                            if start == -1:
                                buffer = buffer[-1024:] if buffer else buffer
                                break
                            end = buffer.find(b"\xff\xd9", start + 2)
                            if end == -1:
                                buffer = buffer[start:]
                                break
                            frame = buffer[start:end + 2]
                            buffer = buffer[end + 2:]
                            now = time.monotonic()
                            if now - self._last_frame_at < MIN_FRAME_INTERVAL:
                                continue
                            self._last_frame_at = now
                            # PixbufLoader must run on GTK main thread; only pass raw bytes here.
                            GLib.idle_add(self._decode_jpeg_main, bytes(frame))
            except requests.RequestException as exc:
                logging.warning("Camera stream fetch error: %s", exc)
            except Exception as exc:
                logging.warning("Camera stream error: %s", exc)

            if self._running:
                time.sleep(0.2)

    def _decode_jpeg_main(self, jpeg_bytes):
        if not self._running:
            return False
        pixbuf = None
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(jpeg_bytes)
            loader.close()
            pixbuf = loader.get_pixbuf()
        except Exception:
            return False
        if pixbuf is None:
            return False

        self._set_frame(pixbuf)
        return False

    def _set_frame(self, pixbuf):
        with self._raw_lock:
            self._last_raw_pixbuf = pixbuf
        self._refresh_scaled()

    def _get_target_size(self):
        current = self.image.get_parent()
        while current is not None:
            width = current.get_allocated_width()
            height = current.get_allocated_height()
            if width >= 32 and height >= 32:
                return max(24, width - 8), max(24, height - 8)
            current = current.get_parent()
        return 320, 240

    def _refresh_scaled(self):
        with self._raw_lock:
            raw = self._last_raw_pixbuf
        if raw is None:
            return False

        target_w, target_h = self._get_target_size()
        raw_w = raw.get_width()
        raw_h = raw.get_height()
        if raw_w <= 0 or raw_h <= 0:
            return False

        scale = min(target_w / raw_w, target_h / raw_h)
        if scale <= 0:
            return False

        new_w = max(2, int(raw_w * scale))
        new_h = max(2, int(raw_h * scale))
        scaled = raw.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)
        if scaled is None:
            return False
        self.image.set_from_pixbuf(scaled)
        return False
