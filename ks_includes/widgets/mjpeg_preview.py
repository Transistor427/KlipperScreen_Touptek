import logging
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

import requests

PREVIEW_INTERVAL_SEC = 1.0
FULLSCREEN_INTERVAL_SEC = 0.35


class MainMenuCameraPreview:
    """Camera preview using lightweight snapshot polling."""

    def __init__(self, screen, url):
        self._screen = screen
        self.url = self._to_snapshot_url(url)
        self._running = False
        self._thread = None
        self._fullscreen = False
        self._fullscreen_box = None
        self._last_raw_pixbuf = None
        self._raw_lock = threading.Lock()
        self._session = requests.Session()
        self._last_target_size = (0, 0)
        self._last_source_size = (0, 0)
        self._last_scaled_pixbuf = None

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
    def _to_snapshot_url(url):
        if url.endswith("/stream"):
            return f"{url[:-7]}/snapshot"
        return url

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
            started = time.monotonic()
            try:
                with self._session.get(
                    self.url,
                    timeout=(2.5, 2.5),
                    headers={"Connection": "close", "Cache-Control": "no-cache"},
                ) as response:
                    response.raise_for_status()
                    frame = response.content
                    if frame:
                        self._decode_and_store(frame)
            except requests.RequestException as exc:
                logging.warning("Camera snapshot fetch error: %s", exc)
            except Exception as exc:
                logging.warning("Camera snapshot error: %s", exc)

            if self._running:
                interval = FULLSCREEN_INTERVAL_SEC if self._fullscreen else PREVIEW_INTERVAL_SEC
                elapsed = time.monotonic() - started
                pause = max(0.05, interval - elapsed)
                time.sleep(pause)

    def _decode_and_store(self, jpeg_bytes):
        if not self._running:
            return
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(jpeg_bytes)
            loader.close()
            pixbuf = loader.get_pixbuf()
        except Exception:
            return
        if pixbuf is None:
            return

        GLib.idle_add(self._set_frame, pixbuf)

    def _set_frame(self, pixbuf):
        with self._raw_lock:
            self._last_raw_pixbuf = pixbuf
        self._refresh_scaled()
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
        source_size = (raw_w, raw_h)
        target_size = (new_w, new_h)
        if (
            self._last_scaled_pixbuf is not None
            and self._last_source_size == source_size
            and self._last_target_size == target_size
        ):
            self.image.set_from_pixbuf(self._last_scaled_pixbuf)
            return False

        scaled = raw.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)
        if scaled is None:
            return False
        self._last_scaled_pixbuf = scaled
        self._last_source_size = source_size
        self._last_target_size = target_size
        self.image.set_from_pixbuf(scaled)
        return False
