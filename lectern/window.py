"""One LecternWindow per opened file -- Evince/Papers-style: no sidebar, no
editing surface, minimal headerbar, a top find-bar revealer and a floating
bottom-right zoom pill (the same idiom Papers/Loupe use for zoom controls).
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib, Gdk

from . import tags as tagdefs
from .i18n import _, ngettext
from .document import Document, DocumentLoadError
from .renderer import MarkdownRenderer
from .findbar import FindController
from .zoom import ZoomController
from .filewatch import FileWatcher
from .printing import PrintCoordinator
from .decorated_textview import DecoratedTextView
from . import sticky_settings
from . import recent


class LecternWindow(Adw.ApplicationWindow):
    def __init__(self, application, gfile=None):
        super().__init__(application=application, default_width=760, default_height=900)
        self._document = None
        self._renderer = None
        self._watcher = None
        self._style_manager = Adw.StyleManager.get_default()
        self._dark_handler_id = self._style_manager.connect("notify::dark", self._on_dark_changed)

        self._build_ui()
        self._install_actions()
        self._sync_window_title()

        if gfile is not None:
            self._open_file(gfile)
        else:
            self._show_empty_state()

    # -- UI construction --------------------------------------------------

    def _build_ui(self):
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        self._toolbar_view = Adw.ToolbarView()
        self._toast_overlay.set_child(self._toolbar_view)
        self._toolbar_view.add_top_bar(self._build_headerbar())

        # Remote images are not fetched on open -- opening a document
        # shouldn't tell whoever hosts its images that you opened it, and
        # a per-image URL is a serviceable read receipt. This banner is
        # the opt-in, per document.
        self._remote_images_banner = Adw.Banner(button_label=_("Load"))
        self._remote_images_banner.connect("button-clicked", self._on_load_remote_images)
        self._toolbar_view.add_top_bar(self._remote_images_banner)

        self._content_overlay = Gtk.Overlay()
        self._toolbar_view.set_content(self._content_overlay)

        self._build_textview()
        self._content_overlay.set_child(self._scrolled)
        self._content_overlay.add_overlay(self._build_findbar_widget())
        self._content_overlay.add_overlay(self._build_zoom_widget())

    def _build_headerbar(self):
        header = Adw.HeaderBar()
        self._window_title = Adw.WindowTitle(title="Lectern")
        header.set_title_widget(self._window_title)

        self._find_toggle = Gtk.ToggleButton(icon_name="edit-find-symbolic", tooltip_text=_("Find"))
        self._find_toggle.connect("toggled", self._on_find_toggled)
        header.pack_start(self._find_toggle)

        menu = Gio.Menu()

        file_section = Gio.Menu()
        file_section.append(_("Open…"), "win.open")
        self._recent_menu = Gio.Menu()
        file_section.append_submenu(_("Open Recent"), self._recent_menu)
        menu.append_section(None, file_section)

        doc_section = Gio.Menu()
        doc_section.append(_("Print…"), "win.print-doc")
        doc_section.append(_("Header and Footer When Printing"), "win.print-header-footer")
        doc_section.append(_("Document Properties"), "win.properties")
        doc_section.append(_("Keyboard Shortcuts"), "win.show-help-overlay")
        doc_section.append(_("About Lectern"), "app.about")
        menu.append_section(None, doc_section)

        self._menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text=_("Main Menu"))
        self._menu_button.set_menu_model(menu)
        # Rebuild the recent submenu each time the popover opens rather than
        # subscribing to Gtk.RecentManager::changed -- the signal would be
        # on a process-wide singleton and need disconnecting per window
        # (see do_close_request), whereas this connection dies with the
        # button.
        self._menu_button.connect("notify::active", self._on_menu_active)
        header.pack_end(self._menu_button)
        return header

    def _on_menu_active(self, button, _pspec):
        if button.get_active():
            self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self._recent_menu.remove_all()
        infos = recent.markdown_items()
        for info in infos:
            label = info.get_display_name() or GLib.path_get_basename(info.get_uri())
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                "win.open-recent", GLib.Variant.new_string(info.get_uri())
            )
            self._recent_menu.append_item(item)
        if not infos:
            # An item with no action renders insensitive -- a visible
            # "nothing here yet" rather than an empty popover.
            self._recent_menu.append_item(Gio.MenuItem.new(_("No Recent Documents"), None))
            return
        trailing = Gio.Menu()
        trailing.append(_("Clear Recently Opened"), "win.clear-recent")
        self._recent_menu.append_section(None, trailing)

    def _build_textview(self):
        # A plain, untagged buffer for now -- real content (and the full,
        # ~30-tag style table) only gets built in _render_document, once a
        # file is actually being opened. Building the real tag table here
        # unconditionally would be pure wasted startup work for the (very
        # common) case where _render_document replaces it immediately
        # after, or the empty-state page replaces this view entirely.
        # No margins passed: the view sets its own four, since where the
        # content column starts depends on its width and the zoom level.
        self._textview = DecoratedTextView(
            editable=False, cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        self._textview.add_css_class("lectern-content")

        click = Gtk.GestureClick()
        click.connect("released", self._on_textview_click)
        self._textview.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_textview_motion)
        self._textview.add_controller(motion)

        self._scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC
        )
        self._scrolled.set_child(self._textview)

        # Gtk.TextView never stretches an anchored child widget to fill
        # the line -- hexpand/halign on the widget itself are silently
        # ignored, confirmed empirically -- so table frames and hr
        # separators (Lectern's only two anchored widget kinds) would
        # otherwise sit at their own tiny natural width forever, in a sea
        # of unused space, and never react to the window being resized.
        # The adjustment's page-size is the one reliable, signal-based way
        # to observe the view's actual content width changing (Gtk.Widget
        # itself has no public size-change signal in GTK4); connecting
        # here, before the window is ever presented, also catches the
        # initial layout pass, so newly opened documents get correctly
        # sized tables immediately, not just after the first resize.
        self._fill_width_widgets = []
        self._images = []
        self._diagrams = []
        self._textview.get_hadjustment().connect("notify::page-size", self._on_content_width_changed)

        self._zoom = ZoomController(self._textview)
        self._find = FindController(self._textview)

        scroll_ctrl = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_ctrl.connect("scroll", self._on_ctrl_scroll)
        self._scrolled.add_controller(scroll_ctrl)

    def _build_findbar_widget(self):
        self._search_entry = Gtk.SearchEntry(placeholder_text=_("Find in document"))
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("activate", self._on_find_activate)
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_find_key)
        self._search_entry.add_controller(key_ctrl)

        self._find_word = Gtk.ToggleButton(label=_("Whole word"), tooltip_text=_("Match whole word only"))
        self._find_word.connect("toggled", self._on_find_options_changed)
        self._find_case = Gtk.ToggleButton(label=_("Case"), tooltip_text=_("Case sensitive"))
        self._find_case.connect("toggled", self._on_find_options_changed)

        prev_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Previous match"))
        prev_btn.connect("clicked", lambda b: self._advance_find(-1))
        next_btn = Gtk.Button(icon_name="go-down-symbolic", tooltip_text=_("Next match"))
        next_btn.connect("clicked", lambda b: self._advance_find(1))

        self._find_label = Gtk.Label(label="")
        self._find_label.add_css_class("dim-label")

        close_btn = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Close"))
        close_btn.connect("clicked", lambda b: self._find_toggle.set_active(False))

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for widget in (self._search_entry, self._find_word, self._find_case,
                       prev_btn, next_btn, self._find_label, close_btn):
            inner.append(widget)
        inner.set_margin_top(6)
        inner.set_margin_bottom(6)
        inner.set_margin_start(6)
        inner.set_margin_end(6)
        inner.add_css_class("toolbar")
        inner.add_css_class("osd")

        self._find_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._find_revealer.set_child(inner)
        self._find_revealer.set_valign(Gtk.Align.START)
        self._find_revealer.set_halign(Gtk.Align.FILL)
        return self._find_revealer

    def _build_zoom_widget(self):
        zoom_out_btn = Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text=_("Zoom out"))
        zoom_out_btn.connect("clicked", lambda b: self._zoom.zoom_out())
        zoom_in_btn = Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text=_("Zoom in"))
        zoom_in_btn.connect("clicked", lambda b: self._zoom.zoom_in())
        self._zoom_label = Gtk.Label(label="100%")
        self._zoom_label.set_width_chars(5)
        self._zoom.connect("changed", self._on_zoom_changed)

        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for widget in (zoom_out_btn, self._zoom_label, zoom_in_btn):
            pill.append(widget)
        pill.set_margin_top(6)
        pill.set_margin_bottom(6)
        pill.set_margin_start(8)
        pill.set_margin_end(8)
        pill.add_css_class("osd")
        pill.add_css_class("toolbar")

        # Always-on-screen, it would permanently cover whatever content
        # happens to scroll under this corner -- most noticeably the
        # last few lines once scrolled to the document's end, since the
        # overlay sits outside the ScrolledWindow's own notion of
        # "content" entirely. Auto-hide after a moment of inactivity
        # instead, matching the OSD convention GNOME uses for floating
        # transient controls (volume/brightness, video player chrome):
        # visible right after a zoom change or while the pointer is over
        # it, gone otherwise.
        self._zoom_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.CROSSFADE, reveal_child=False
        )
        self._zoom_hide_source_id = 0
        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *a: self._flash_zoom_osd())
        pill.add_controller(hover)
        self._zoom_revealer.set_child(pill)

        wrapper = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.END)
        wrapper.set_margin_end(16)
        wrapper.set_margin_bottom(16)
        wrapper.append(self._zoom_revealer)
        return wrapper

    def _flash_zoom_osd(self):
        self._zoom_revealer.set_reveal_child(True)
        if self._zoom_hide_source_id:
            GLib.source_remove(self._zoom_hide_source_id)
        self._zoom_hide_source_id = GLib.timeout_add(1500, self._hide_zoom_osd)

    def _hide_zoom_osd(self):
        self._zoom_hide_source_id = 0
        self._zoom_revealer.set_reveal_child(False)
        return GLib.SOURCE_REMOVE

    # -- actions / shortcuts -----------------------------------------------

    def _install_actions(self):
        actions = [
            ("open", lambda a, p: self._open_dialog()),
            ("find", self._action_find),
            ("zoom-in", lambda a, p: self._zoom.zoom_in()),
            ("zoom-out", lambda a, p: self._zoom.zoom_out()),
            ("zoom-reset", lambda a, p: self._zoom.zoom_reset()),
            ("print-doc", self._action_print),
            ("properties", self._action_properties),
        ]
        for name, handler in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        # Parameterised: the URI to open. From the menu, always a new
        # window -- one-window-per-document, same as opening while a file
        # is already on screen.
        open_recent = Gio.SimpleAction.new("open-recent", GLib.VariantType.new("s"))
        open_recent.connect(
            "activate",
            lambda a, p: self.get_application().open([Gio.File.new_for_uri(p.get_string())], ""),
        )
        self.add_action(open_recent)

        clear_recent = Gio.SimpleAction.new("clear-recent", None)
        clear_recent.connect("activate", lambda a, p: recent.clear())
        self.add_action(clear_recent)

        # A checkable menu item rather than a print-dialog option: GTK's
        # print dialog is routed through the xdg-desktop-portal on this
        # (and most modern Wayland) desktops, which flatly refuses to host
        # an app-supplied custom widget ("create-custom-widget not
        # supported with portal") -- so the toggle has to live in Lectern's
        # own UI instead. Defaults on here (most people printing a document
        # want to know which page they're holding); _open_file overrides
        # this with whatever was last set for this document, if anything
        # was (see sticky_settings).
        header_footer_action = Gio.SimpleAction.new_stateful(
            "print-header-footer", None, GLib.Variant.new_boolean(True)
        )
        header_footer_action.connect("change-state", self._action_toggle_header_footer)
        self.add_action(header_footer_action)

    def _action_find(self, action, param):
        self._find_toggle.set_active(True)

    def _action_toggle_header_footer(self, action, value):
        action.set_state(value)
        key = sticky_settings.key_for(
            self._document.title if self._document else None,
            self._document.basename if self._document else None,
        )
        sticky_settings.set_sticky(key, "header_footer", value.get_boolean())

    def _action_print(self, action, param):
        if self._renderer is None:
            return
        coordinator = PrintCoordinator()
        file_name = self._document.basename if self._document else "document"
        doc_title = self._document.title if self._document else None
        header_footer = self.lookup_action("print-header-footer").get_state().get_boolean()
        coordinator.print_document(
            # Paper is white regardless of the desktop theme, so print
            # always renders in the light palette -- following the app's
            # live dark-mode state here left code-block/diagram
            # backgrounds and heading rules dark on a white page.
            self, self._renderer.print_model, False, doc_title, file_name,
            header_footer=header_footer,
            # So a link in the document prints as a real clickable PDF
            # annotation, not just blue underlined text.
            link_targets=self._renderer.dispatch_targets,
        )

    def _action_properties(self, action, param):
        if self._document is None:
            return
        dialog = Adw.AlertDialog(
            heading=_("Document Properties"),
            body="\n".join([
                _("Name: {name}").format(name=self._document.basename),
                _("Location: {location}").format(location=self._document.parent_path or "—"),
                _("Size: {size} bytes").format(size=f"{self._document.size_bytes():,}"),
                _("Words: {count}").format(count=f"{self._document.word_count():,}"),
                _("Est. reading time: {minutes} min").format(
                    minutes=self._document.reading_time_minutes()
                ),
            ]),
        )
        dialog.add_response("ok", _("OK"))
        dialog.present(self)

    # -- find bar wiring -----------------------------------------------

    def _on_find_toggled(self, button):
        active = button.get_active()
        self._find_revealer.set_reveal_child(active)
        if active:
            self._search_entry.grab_focus()
        else:
            self._find.clear()
            self._sync_find_label()
            self._textview.grab_focus()

    def _on_search_changed(self, entry):
        self._find.search(entry.get_text())
        self._sync_find_label()

    def _on_find_options_changed(self, button):
        self._find.case_sensitive = self._find_case.get_active()
        self._find.whole_word = self._find_word.get_active()
        self._find.search(self._search_entry.get_text())
        self._sync_find_label()

    def _on_find_activate(self, entry):
        self._advance_find(1)

    def _on_find_key(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self._find_toggle.set_active(False)
            return True
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval == Gdk.KEY_Return and shift:
            self._advance_find(-1)
            return True
        return False

    def _advance_find(self, direction):
        self._find.advance(direction)
        self._sync_find_label()

    def _sync_find_label(self):
        count = self._find.match_count
        text = _("{position} / {count}").format(
            position=self._find.current_position, count=count
        ) if count else _("No matches")
        self._find_label.set_text(text if self._search_entry.get_text() else "")

    # -- remote images ----------------------------------------------------

    def _sync_remote_images_banner(self):
        pending = [img for img in self._images if img.remote]
        self._remote_images_banner.set_revealed(bool(pending))
        if pending:
            count = len(pending)
            self._remote_images_banner.set_title(
                ngettext(
                    "This document contains {count} remote image, not loaded",
                    "This document contains {count} remote images, not loaded",
                    count,
                ).format(count=count)
            )

    def _on_load_remote_images(self, banner):
        for image in self._images:
            image.load_remote()
        self._remote_images_banner.set_revealed(False)

    # -- anchored-widget width sync ---------------------------------------

    def _on_content_width_changed(self, hadjustment, pspec):
        self._sync_fill_width_widgets()

    def _sync_fill_width_widgets(self):
        # page-size is the raw viewport width, not reduced by the
        # TextView's own left/right margins (confirmed empirically -- it
        # does NOT subtract them), so that has to happen here to match
        # the width normal text actually wraps to.
        usable = (
            self._textview.get_hadjustment().get_page_size()
            - self._textview.get_left_margin()
            - self._textview.get_right_margin()
        )
        if usable <= 0:
            return  # not yet laid out
        for widget in self._fill_width_widgets:
            widget.set_size_request(round(usable), -1)
        # Images and diagrams take the same number as a ceiling to scale
        # down to, rather than a width to fill -- see
        # ImageView._apply_size and DiagramView.set_available_width.
        for scaled in self._images + self._diagrams:
            scaled.set_available_width(round(usable))

    # -- zoom ------------------------------------------------------------

    def _on_zoom_changed(self, controller, factor):
        self._zoom_label.set_text(f"{round(factor * 100)}%")
        # Diagrams are drawn, not laid out by the TextView, so the CSS
        # font-size the controller sets doesn't reach them -- they're
        # scaled by hand to keep step with the text around them.
        for diagram in self._diagrams:
            diagram.set_zoom(factor)
        # Zooming moves the view's margins without the viewport changing
        # size, so the page-size signal the width sync rides on never
        # fires -- anchored tables and separators would otherwise keep the
        # previous zoom's width until the next window resize. Diagrams are
        # width-synced there too, so this has to follow their set_zoom.
        self._sync_fill_width_widgets()
        self._flash_zoom_osd()

    def _on_ctrl_scroll(self, controller, dx, dy):
        state = controller.get_current_event_state()
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        if dy < 0:
            self._zoom.zoom_in()
        elif dy > 0:
            self._zoom.zoom_out()
        return True

    # -- link / footnote click dispatch -----------------------------------

    def _iter_at_widget_xy(self, x, y):
        """Widget-relative (x, y) -> Gtk.TextIter at that position, or None."""
        bx, by = self._textview.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        found, it = self._textview.get_iter_at_location(bx, by)
        return it if found else None

    def _on_textview_click(self, gesture, n_press, x, y):
        if self._renderer is None:
            return
        it = self._iter_at_widget_xy(x, y)
        target = self._renderer.target_at_iter(it) if it is not None else None
        if target is not None:
            self._activate_target(target)

    def _on_textview_motion(self, controller, x, y):
        target = None
        if self._renderer is not None:
            it = self._iter_at_widget_xy(x, y)
            target = self._renderer.target_at_iter(it) if it is not None else None
        self._textview.set_cursor_from_name("pointer" if target is not None else "text")

    def _open_href(self, href):
        """Open a link/table-cell href, resolving it against the open
        document's own directory first if it has no URI scheme -- a bare
        Gio.AppInfo.launch_default_for_uri("../app/foo/") fails outright
        (GLib.Error: g-io-error-quark: Operation not supported), since
        that's not a URI at all, just a relative filesystem path. This is
        the common case for documentation that cross-links to files
        sitting next to it, e.g. this app's own tests/fixtures/*.md.
        A resolved link to another .md file opens in Lectern itself,
        for free, since we're the registered default handler for
        text/markdown -- no special-casing needed here.

        A bare `#section` fragment is neither of those -- it names a
        heading in *this* document, not a file, so it's resolved against
        the renderer's own heading anchors instead of the filesystem. A
        `file.md#section` href gets its fragment stripped before the path
        part is resolved, so the file it names opens rather than a
        literal (and nonexistent) "file.md#section" -- landing on that
        file's own #section is less useful without renderer-to-renderer
        wiring across windows, so isn't attempted here.
        """
        if not href:
            return
        if href.startswith("#"):
            self._scroll_to_heading(href[1:])
            return
        if GLib.uri_parse_scheme(href) is not None:
            target_uri = href
        else:
            path, _, _fragment = href.partition("#")
            base_dir = self._document.gfile.get_parent() if self._document else None
            target_uri = base_dir.resolve_relative_path(path).get_uri() if base_dir else None
        if not target_uri:
            return
        try:
            Gio.AppInfo.launch_default_for_uri(target_uri, None)
        except GLib.Error:
            self._toast_overlay.add_toast(Adw.Toast(title=_("Couldn’t open link"), timeout=3))

    def _on_table_link_activated(self, label, uri):
        self._open_href(uri)
        return True  # stop Gtk.Label's own default handling

    def _activate_target(self, target):
        kind = target["type"]
        if kind == "url":
            self._open_href(target["href"])
        elif kind == "footnote-jump":
            mark_name = self._renderer.footnote_def_mark_name(target["label"])
            self._scroll_to_mark_name(mark_name)
        elif kind == "footnote-back":
            mark_name = self._renderer.footnote_ref_mark_name(target["label"])
            self._scroll_to_mark_name(mark_name)

    def _scroll_to_heading(self, slug):
        if self._renderer is None:
            return
        self._scroll_to_mark_name(self._renderer.heading_mark_name(slug))

    def _scroll_to_mark_name(self, mark_name):
        if not mark_name:
            return
        buffer = self._textview.get_buffer()
        mark = buffer.get_mark(mark_name)
        if mark is not None:
            self._textview.scroll_to_mark(mark, 0.1, True, 0.0, 0.1)

    # -- document loading / reload -----------------------------------------

    def _show_empty_state(self):
        status = Adw.StatusPage(
            title=_("No Document"),
            description=_("Open a Markdown file to view it here."),
            icon_name="text-x-generic-symbolic",
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, halign=Gtk.Align.CENTER)

        open_button = Gtk.Button(label=_("Open File…"), halign=Gtk.Align.CENTER)
        open_button.add_css_class("suggested-action")
        open_button.add_css_class("pill")
        open_button.connect("clicked", lambda b: self._open_dialog())
        box.append(open_button)

        infos = recent.markdown_items(limit=8)
        if infos:
            listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            listbox.add_css_class("boxed-list")
            listbox.set_size_request(380, -1)
            for info in infos:
                label = info.get_display_name() or GLib.path_get_basename(info.get_uri())
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(label),
                    subtitle=GLib.markup_escape_text(self._friendly_dir(info.get_uri())),
                    activatable=True,
                )
                row.add_prefix(Gtk.Image.new_from_icon_name("text-x-generic-symbolic"))
                row.connect(
                    "activated", lambda r, uri=info.get_uri(): self._open_recent_uri(uri)
                )
                listbox.append(row)
            box.append(listbox)

        status.set_child(box)
        self._content_overlay.set_child(status)

    def _open_recent_uri(self, uri):
        gfile = Gio.File.new_for_uri(uri)
        # The empty-state page only shows with no document loaded, so this
        # window is free to take the file itself rather than leave a blank
        # window behind.
        if self._document is None:
            self._open_file(gfile)
        else:
            self.get_application().open([gfile], "")

    def _friendly_dir(self, uri):
        parent = Gio.File.new_for_uri(uri).get_parent()
        path = parent.get_path() if parent else None
        if not path:
            return uri
        home = GLib.get_home_dir()
        # startswith(home) alone treats /home/alice as inside /home/al --
        # a shared string prefix, not a shared directory. Requiring the
        # next character to be a path separator (or nothing, for home
        # itself) keeps the substitution to home and its descendants.
        if path == home:
            return "~"
        if path.startswith(home + "/"):
            return "~" + path[len(home):]
        return path

    def _open_dialog(self):
        dialog = Gtk.FileDialog(title=_("Open Markdown File"))
        filter_md = Gtk.FileFilter(name=_("Markdown files"))
        filter_md.add_pattern("*.md")
        filter_md.add_pattern("*.markdown")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_md)
        dialog.set_filters(filters)
        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile is None:
            return
        if self._document is None:
            self._open_file(gfile)
        else:
            self.get_application().open([gfile], "")

    def _open_file(self, gfile):
        document = Document(gfile)
        try:
            document.load()
        except DocumentLoadError as ex:
            self._show_load_error(str(ex))
            return
        self._document = document
        recent.record(gfile.get_uri())
        # Once only, on the initial open -- not in _render_document, which
        # also runs on every reload, so a live-edited file changing its
        # own title mid-session can't flip the checkbox out from under
        # whatever the reader already chose for this window.
        key = sticky_settings.key_for(self._document.title, self._document.basename)
        header_footer = sticky_settings.get_sticky(key, "header_footer", True)
        self.lookup_action("print-header-footer").set_state(GLib.Variant.new_boolean(header_footer))
        self._content_overlay.set_child(self._scrolled)
        self._window_title.set_title(self._document.basename)
        self._window_title.set_subtitle(self._document.parent_path or "")
        self._render_document()
        self._watcher = FileWatcher(gfile)
        self._watcher.connect("reload-needed", self._on_reload_needed)
        self._watcher.connect("file-missing", self._on_file_missing)

    def _sync_window_title(self):
        """Set Gtk.Window's own title, which is what the shell's window
        list and Alt-Tab read.

        The headerbar's Adw.WindowTitle is a *widget* and setting it does
        nothing for the window property, so before this every window
        reported no title at all and the shell fell back to the
        application name -- leaving several open documents all listed
        identically as "Lectern".

        No " - Lectern" suffix: the GNOME HIG wants a window titled after
        its document, and the shell already shows which application the
        window belongs to.
        """
        if self._document is None:
            self.set_title("Lectern")
            return
        self.set_title(self._document.title or self._document.basename)

    def _show_load_error(self, message):
        status = Adw.StatusPage(
            title=_("Couldn’t Open File"), description=message, icon_name="dialog-error-symbolic"
        )
        self._content_overlay.set_child(status)

    def _render_document(self):
        dark = self._style_manager.get_dark()
        buffer = Gtk.TextBuffer(tag_table=tagdefs.create_tag_table(dark))
        self._renderer = MarkdownRenderer()
        base_dir = self._document.gfile.get_parent() if self._document else None
        self._renderer.render(self._document.tree, buffer, dark=dark, base_dir=base_dir)
        self._textview.set_buffer(buffer)
        self._textview.dispatch_targets = self._renderer.dispatch_targets
        self._textview.anchor_descriptors = self._renderer.anchor_descriptors
        self._images = self._renderer.images
        self._diagrams = self._renderer.diagrams
        for diagram in self._diagrams:
            diagram.set_zoom(self._zoom.factor)
        self._fill_width_widgets = self._renderer.attach_pending_widgets(self._textview)
        self._sync_fill_width_widgets()
        self._sync_remote_images_banner()
        # Here rather than in _open_file, so a reload that changed the
        # document's first heading retitles the window too.
        self._sync_window_title()
        for label in self._renderer.table_link_labels:
            label.connect("activate-link", self._on_table_link_activated)
        self._find = FindController(self._textview, self._renderer.tables)
        self._sync_find_label()

    def _on_reload_needed(self, watcher):
        adjustment = self._scrolled.get_vadjustment()
        max_scroll = max(adjustment.get_upper() - adjustment.get_page_size(), 1.0)
        scroll_fraction = adjustment.get_value() / max_scroll
        had_query = self._search_entry.get_text() if self._find_toggle.get_active() else None

        try:
            self._document.reload()
        except DocumentLoadError:
            return  # keep showing the last-good content
        self._render_document()

        def restore_scroll():
            adj = self._scrolled.get_vadjustment()
            new_max = max(adj.get_upper() - adj.get_page_size(), 1.0)
            adj.set_value(scroll_fraction * new_max)
            if had_query:
                self._find.search(had_query)
                self._sync_find_label()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(restore_scroll)
        self._toast_overlay.add_toast(Adw.Toast(title=_("Reloaded"), timeout=2))

    def _on_file_missing(self, watcher):
        self._toast_overlay.add_toast(Adw.Toast(title=_("File no longer available"), timeout=3))

    def _on_dark_changed(self, style_manager, pspec):
        if self._textview.get_buffer() is not None:
            tagdefs.update_tag_colors(self._textview.get_buffer().get_tag_table(), style_manager.get_dark())

    def do_close_request(self):
        if self._watcher is not None:
            self._watcher.close()
        self._zoom.close()
        # Adw.StyleManager is a process-wide singleton that outlives every
        # window -- without this, each closed window's bound-method
        # callback (and everything it closes over: buffer, renderer,
        # document...) would stay reachable forever.
        self._style_manager.disconnect(self._dark_handler_id)
        return False
