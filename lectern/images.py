"""Markdown images: one embedded widget per `![alt](src)`, plus the
loading policy behind them.

Two quite different sources sit behind one widget:

- **local paths**, resolved against the document's own directory (the
  same base Gio.File `window.py`'s `_open_href` resolves links against)
  and loaded straight off disk when the document renders;
- **http(s) URLs**, which are deliberately *not* fetched on open. Opening
  a document shouldn't tell a third party you opened it, and a per-image
  URL makes a serviceable read receipt. `window.py` raises a banner and
  calls `load_remote()` only if the reader asks for it.

**Loading never cascades.** Every raster format GdkPixbuf handles here is
self-contained, and librsvg resolves no external references whatsoever --
verified against 2.60, where remote and local `href`s, CSS `@import`,
`<use>`, `@font-face` and `xi:include` are all silently dropped. So one
Markdown image is exactly one load, and can never trigger another. If
that ever stops being true (an older or differently-built librsvg), the
Flatpak's lack of `--share=network` is the real backstop, not this note.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, Gio, GLib, Adw

from .i18n import _

# Remote fetches share one session per process. Created lazily so a
# document with no remote images never constructs one at all.
_session = None

# Hard ceiling on a fetched image, so a hostile or merely careless URL
# can't balloon memory. Generous enough for any plausible screenshot.
MAX_REMOTE_BYTES = 20 * 1024 * 1024

# Chunk size for the bounded read in _on_remote_chunk -- small enough that
# a response that blows past MAX_REMOTE_BYTES is caught not far past the
# limit, large enough not to turn a normal-sized image into thousands of
# round trips through the GLib main loop.
_REMOTE_READ_CHUNK = 64 * 1024


def _soup_session():
    global _session
    if _session is None:
        import gi as _gi
        _gi.require_version("Soup", "3.0")
        from gi.repository import Soup
        _session = Soup.Session()
        _session.set_user_agent("Lectern/0.1 ")
    return _session


def is_remote(src):
    scheme = GLib.uri_parse_scheme(src or "")
    return scheme in ("http", "https")


def _resolve_local(src, base_dir):
    """Local `src` -> Gio.File, or None if it can't be placed. Mirrors
    window.py's link resolution: anything with a scheme is taken as a URI,
    anything else is relative to the document's directory."""
    if not src:
        return None
    scheme = GLib.uri_parse_scheme(src)
    if scheme == "file":
        return Gio.File.new_for_uri(src)
    if scheme is not None:
        return None  # some other scheme we don't serve
    if src.startswith("/"):
        return Gio.File.new_for_path(src)
    return base_dir.resolve_relative_path(src) if base_dir else None


class ImageView(Gtk.Box):
    """The widget anchored into the buffer for one Markdown image.

    Shows a Gtk.Picture once loaded, and a dimmed alt-text placeholder
    before that or on failure -- an image that didn't load should still
    leave its alt text readable rather than a silent gap, since alt text
    is frequently the only description of what was meant to be there.
    """

    def __init__(self, src, alt, base_dir):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.START)
        self.src = src
        self.alt = alt or src
        self._base_dir = base_dir
        self._texture = None
        self._available_width = 0
        self._picture = None
        self._placeholder = None
        self._remote_chunks = None  # accumulated GLib.Bytes while a fetch streams in
        self._remote_total = 0
        self.remote = is_remote(src)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        if self.remote:
            self._show_placeholder(_("Remote image not loaded"), "globe-symbolic")
        else:
            self._load_local()

    # -- public API ------------------------------------------------------

    @property
    def texture(self):
        """The loaded Gdk.Texture, or None if it hasn't loaded (or
        failed). printing.py reads this at print time, so an image loaded
        after the document opened still prints."""
        return self._texture

    def load_remote(self):
        """Fetch an http(s) image. No-op for local images, or if this one
        already loaded -- window.py fires this at every image in the
        document when the banner's Load is pressed."""
        if not self.remote or self._texture is not None:
            return
        self._show_placeholder(_("Loading…"), "content-loading-symbolic")
        message = self._build_message()
        if message is None:
            self._fail(_("Bad image URL"))
            return
        _soup_session().send_async(
            message, GLib.PRIORITY_DEFAULT, None, self._on_remote_send, message
        )

    def set_available_width(self, width):
        """Width the view can give this image. Driven by window.py's
        anchored-widget width sync, same as tables and separators."""
        self._available_width = width
        self._apply_size()

    # -- loading ---------------------------------------------------------

    def _build_message(self):
        import gi as _gi
        _gi.require_version("Soup", "3.0")
        from gi.repository import Soup
        try:
            return Soup.Message.new("GET", self.src)
        except (GLib.Error, TypeError):
            return None

    def _load_local(self):
        gfile = _resolve_local(self.src, self._base_dir)
        if gfile is None:
            self._fail(_("Image not found"))
            return
        try:
            self._set_texture(Gdk.Texture.new_from_file(gfile))
        except GLib.Error:
            self._fail(_("Couldn’t load image"))

    def _on_remote_send(self, session, result, message):
        """`send_async` (unlike `send_and_read_async`) hands back the
        response as soon as headers arrive, before any of the body is
        read -- letting an oversized reply be rejected off a declared
        Content-Length, or off our own running total as it streams in
        `_on_remote_chunk`, well before the whole thing sits buffered in
        memory. `send_and_read_async` used to do that buffering itself,
        so MAX_REMOTE_BYTES was only ever checked after the damage
        (memory-wise) was already done.
        """
        try:
            stream = session.send_finish(result)
        except GLib.Error:
            self._fail(_("Couldn’t fetch image"))
            return
        status = message.get_status()
        if status != 200:
            stream.close_async(GLib.PRIORITY_DEFAULT, None, None)
            self._fail(_("Image request failed ({status})").format(status=int(status)))
            return
        content_length = message.get_response_headers().get_content_length()
        if content_length and content_length > MAX_REMOTE_BYTES:
            stream.close_async(GLib.PRIORITY_DEFAULT, None, None)
            self._fail(_("Image too large"))
            return
        self._remote_chunks = []
        self._remote_total = 0
        self._read_remote_chunk(stream)

    def _read_remote_chunk(self, stream):
        stream.read_bytes_async(
            _REMOTE_READ_CHUNK, GLib.PRIORITY_DEFAULT, None, self._on_remote_chunk, stream
        )

    def _on_remote_chunk(self, stream, result, _stream_again):
        try:
            chunk = stream.read_bytes_finish(result)
        except GLib.Error:
            stream.close_async(GLib.PRIORITY_DEFAULT, None, None)
            self._remote_chunks = None
            self._fail(_("Couldn’t fetch image"))
            return
        if chunk.get_size() == 0:
            stream.close_async(GLib.PRIORITY_DEFAULT, None, None)
            self._finish_remote()
            return
        self._remote_total += chunk.get_size()
        if self._remote_total > MAX_REMOTE_BYTES:
            stream.close_async(GLib.PRIORITY_DEFAULT, None, None)
            self._remote_chunks = None
            self._fail(_("Image too large"))
            return
        self._remote_chunks.append(chunk)
        self._read_remote_chunk(stream)

    def _finish_remote(self):
        chunks, self._remote_chunks = self._remote_chunks, None
        if not chunks:
            self._fail(_("Empty image response"))
            return
        data = GLib.Bytes.new(b"".join(bytes(chunk.get_data()) for chunk in chunks))
        try:
            self._set_texture(Gdk.Texture.new_from_bytes(data))
        except GLib.Error:
            self._fail(_("Couldn’t decode image"))

    # -- presentation ----------------------------------------------------

    def _set_texture(self, texture):
        self._texture = texture
        self._clear()
        self._picture = Gtk.Picture(
            halign=Gtk.Align.START, can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN
        )
        self._picture.set_paintable(texture)
        self._picture.set_tooltip_text(self.alt)
        self.append(self._picture)
        self._apply_size()

    def _fail(self, reason):
        self._show_placeholder(reason, "image-missing-symbolic")

    def _show_placeholder(self, reason, icon_name):
        self._clear()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.add_css_class("dim-label")
        box.append(Gtk.Image.new_from_icon_name(icon_name))
        # Nothing pushes a width onto an anchored child, so a wrapping
        # label here takes its *minimum* -- which is one word per line,
        # and with ellipsize on would be a bare "...". width-chars sets
        # the natural width that minimum is taken from; max-width-chars
        # then caps how wide a long alt is allowed to run.
        label = Gtk.Label(label=self.alt, xalign=0.0, wrap=True)
        label.set_width_chars(24)
        label.set_max_width_chars(60)
        box.append(label)
        box.set_tooltip_text(_("{reason}: {src}").format(reason=reason, src=self.src))
        frame = Gtk.Frame()
        frame.set_child(box)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)
        self._placeholder = frame
        self.append(frame)

    def _clear(self):
        for child in (self._picture, self._placeholder):
            if child is not None:
                self.remove(child)
        self._picture = None
        self._placeholder = None

    def _apply_size(self):
        """Cap the displayed size at the view's usable width, never
        upscaling past the image's own pixels -- blowing a 32px icon up to
        column width looks broken, and Gtk.Picture would happily do it."""
        if self._picture is None or self._texture is None:
            return
        natural_w = self._texture.get_width()
        natural_h = self._texture.get_height()
        if natural_w <= 0 or natural_h <= 0:
            return
        width = natural_w
        if self._available_width > 0:
            width = min(natural_w, self._available_width)
        height = round(natural_h * (width / natural_w))
        self._picture.set_size_request(round(width), height)
