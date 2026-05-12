import logging
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

import requests

MAX_BUFFER_LEN = 2_000_000


class MainMenuCameraPreview:
    """MJPEG preview with tap-to-fullscreen."""

    def __init__(self, screen, url):
        self._screen = screen
        self.url = url
        self._running = False
        self._thread = None
        self._fullscreen = False
        self._fullscreen_box = None
        self._last_raw_pixbuf = None
        self._raw_lock = threading.Lock()

        self.image = Gtk.Image(hexpand=True, vexpand=True)
        self.image.set_from_pixbuf(self._solid_pixbuf(4, 3, 0x1A, 0x1D, 0x26))

        self.event_box = Gtk.EventBox(hexpand=True, vexpand=True)
        self.event_box.add(self.image)

        self.preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.preview_box.pack_start(self.event_box, True, True, 0)

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.widget.get_style_context().add_class("camera-preview-pane")
        self.widget.pack_start(self.preview_box, True, True, 0)

        caption = Gtk.Label(label=_("Tap for fullscreen"))
        caption.set_sensitive(False)
        caption.get_style_context().add_class("camera-preview-caption")
        self.widget.pack_start(caption, False, False, 4)

        self.event_box.connect("button-press-event", self._on_press)
        self.widget.connect_after("size-allocate", lambda *_: GLib.idle_add(self._refresh_scaled))
        self.widget.show_all()

    @staticmethod
    def _solid_pixbuf(width, height, r, g, b):
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, max(1, width), max(1, height))
        pixbuf.fill(((r << 24) | (g << 16) | (b << 8) | 0xFF) & 0xFFFFFFFF)
        return pixbuf

    def _on_press(self, _widget, event):
        if event.button == Gdk.BUTTON_PRIMARY:
            GLib.idle_add(self._toggle_fullscreen)
            return True
        return False

    def _toggle_fullscreen(self):
        if self._fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()
        return False

    def _enter_fullscreen(self):
        if self._fullscreen:
            return
        parent = self.event_box.get_parent()
        if parent is not None:
            parent.remove(self.event_box)

        self._fullscreen_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._fullscreen_box.set_name("camera-fs")

        rgba = Gdk.RGBA()
        rgba.red = rgba.green = rgba.blue = 0.0
        rgba.alpha = 1.0
        self._fullscreen_box.override_background_color(Gtk.StateFlags.NORMAL, rgba)
        self._fullscreen_box.pack_start(self.event_box, True, True, 0)

        self._screen.overlay.add_overlay(self._fullscreen_box)
        self._fullscreen_box.show_all()
        self._fullscreen = True
        GLib.idle_add(self._refresh_scaled)

    def _exit_fullscreen(self):
        if not self._fullscreen or self._fullscreen_box is None:
            return
        self._screen.overlay.remove(self._fullscreen_box)
        self._fullscreen_box.remove(self.event_box)
        self.preview_box.pack_start(self.event_box, True, True, 0)
        self.preview_box.show_all()
        self._fullscreen_box = None
        self._fullscreen = False
        GLib.idle_add(self._refresh_scaled)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            self._running = True
            return
        self._running = True
        self._thread = threading.Thread(target=self._feed_loop, name="mjpeg_preview", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._fullscreen:
            GLib.idle_add(self._exit_fullscreen)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

    def _feed_loop(self):
        while self._running:
            try:
                with requests.get(
                    self.url, stream=True, timeout=(8, None), headers={"Connection": "close"}
                ) as response:
                    response.raise_for_status()
                    buffer = b""
                    for chunk in response.iter_content(chunk_size=8192):
                        if not self._running:
                            break
                        if not chunk:
                            continue
                        buffer += chunk
                        if len(buffer) > MAX_BUFFER_LEN:
                            buffer = buffer[-500_000:]

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
                            GLib.idle_add(self._decode_and_store, bytes(frame))
            except requests.RequestException as exc:
                logging.warning("Camera MJPEG fetch error: %s", exc)
            except Exception as exc:
                logging.warning("Camera MJPEG error: %s", exc)

            if self._running:
                time.sleep(1)

    def _decode_and_store(self, jpeg_bytes):
        if not self._running:
            return False
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(jpeg_bytes)
            loader.close()
            pixbuf = loader.get_pixbuf()
        except Exception:
            return False
        if pixbuf is None:
            return False

        with self._raw_lock:
            self._last_raw_pixbuf = pixbuf
        GLib.idle_add(self._refresh_scaled)
        return False

    def _get_target_size(self):
        current = self.event_box.get_parent()
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
