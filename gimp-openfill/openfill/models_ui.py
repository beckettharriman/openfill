# OpenFill - generative fill for GIMP 3
# Copyright (C) 2026 Beckett Harriman
# SPDX-License-Identifier: Apache-2.0
"""The Models tab (download manager): a flat list in sections, one row per
model, and a single state button per row whose label follows the state
(Download -> Downloading 43% - Cancel -> Set active -> Active).

Sections (built by ``catalog.build_sections``): Installed, Recommended for
this PC, Verified (the bundled list), More from Hugging Face and Civitai
(collapsed). The live search and the hardware check run in a worker thread;
until they finish the tab shows the bundled list. Nothing is downloaded
unless the user presses Download.

Rows: name and status on one line; size, license and whether it fits this PC
as small grey text below; the state button and a small menu at the right; thin
separators, no hover highlight. A model that needs a Hugging Face login opens
a step-by-step panel (open the model page, accept the license, create a read
key, paste it) - the only place the key field appears.

Pure GTK 3 (as shipped with GIMP). The panel polls the supervisor's task
status file once per second through a GLib timeout and never blocks the UI.
The view model (rows, sections, labels) lives in ``catalog.py`` and is unit
tested without GTK.
"""

from __future__ import annotations

import threading
import time
import webbrowser
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from . import catalog as cat  # noqa: E402
from . import hardware, hub, ui_text  # noqa: E402
from .backend import BackendError  # noqa: E402
from .dialogs import style  # noqa: E402

if TYPE_CHECKING:
    from .app import App

CSS = b"""
.sb-models list, .sb-models list row { background-color: transparent; background-image: none; }
.sb-models list row:hover, .sb-models list row:focus, .sb-models list row:selected,
.sb-models list row:active {
  background-color: transparent; background-image: none; box-shadow: none;
  outline-width: 0; outline-style: none; border-color: transparent;
}
.sb-models list row { padding: 0; border: none; }
.sb-models separator.sb-sep { min-height: 1px; margin: 0 4px; opacity: 0.5; }
.sb-models .sb-section { margin-top: 10px; }
.sb-models .sb-steps { padding: 8px; border-radius: 4px; background-color: alpha(currentColor, 0.05); }
.sb-models button.sb-menu { padding: 2px; min-width: 0; min-height: 0; }
"""
_css_installed = False


def _install_css() -> None:
    global _css_installed
    if _css_installed:
        return
    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_installed = True


def _grey(text: str = "", small: bool = True) -> Gtk.Label:
    label = Gtk.Label(xalign=0)
    label.get_style_context().add_class("dim-label")
    label.set_ellipsize(Pango.EllipsizeMode.END)
    if text:
        _set_markup(label, text, small)
    return label


def _set_markup(label: Gtk.Label, text: str, small: bool = True) -> None:
    escaped = GLib.markup_escape_text(text)
    label.set_markup(f"<small>{escaped}</small>" if small else escaped)


def _open(url: str | None) -> None:
    if url:
        webbrowser.open(url)


class ModelRow(Gtk.ListBoxRow):
    """One model: name + status, grey details, progress bar, state button and a small menu."""

    def __init__(self, panel: ModelsPanel, row: dict) -> None:
        super().__init__()
        self.panel = panel
        self.model_id = row["id"]
        self.data = row
        self.action: str | None = None
        self.set_activatable(False)
        self.set_selectable(False)
        self.set_can_focus(False)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.set_margin_top(6)
        outer.set_margin_bottom(6)
        outer.set_margin_start(6)
        outer.set_margin_end(4)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.name = Gtk.Label(xalign=0)
        self.name.set_ellipsize(Pango.EllipsizeMode.END)
        self.state = _grey()
        self.state.set_ellipsize(Pango.EllipsizeMode.NONE)
        line.pack_start(self.name, False, True, 0)
        line.pack_start(self.state, False, False, 0)
        self.details = _grey()
        self.bar = Gtk.ProgressBar()
        self.bar.set_no_show_all(True)
        text.pack_start(line, False, False, 0)
        text.pack_start(self.details, False, False, 0)
        text.pack_start(self.bar, False, False, 2)
        outer.pack_start(text, True, True, 0)

        self.primary = Gtk.Button()
        self.primary.set_size_request(150, -1)
        self.primary.set_valign(Gtk.Align.CENTER)
        self.primary.connect("clicked", lambda *_: panel.primary_clicked(self.model_id))
        outer.pack_start(self.primary, False, False, 0)

        more = Gtk.MenuButton()
        more.set_relief(Gtk.ReliefStyle.NONE)
        more.get_style_context().add_class("sb-menu")
        more.set_valign(Gtk.Align.CENTER)
        more.set_tooltip_text("More actions")
        more.add(Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.MENU))
        menu = Gtk.Menu()
        self.remove_item = Gtk.MenuItem(label="Delete downloaded files...")
        self.remove_item.connect("activate", lambda *_: panel.remove_clicked(self.model_id))
        menu.append(self.remove_item)
        page = Gtk.MenuItem(label="Open the model page")
        page.connect("activate", lambda *_: _open(self.data.get("page")))
        menu.append(page)
        lic = Gtk.MenuItem(label="Read the license")
        lic.connect("activate", lambda *_: _open(self.data.get("license_url")))
        menu.append(lic)
        self.login_item = Gtk.MenuItem(label="Hugging Face login...")
        self.login_item.connect("activate", lambda *_: panel.show_login(self.model_id))
        menu.append(self.login_item)
        menu.show_all()
        more.set_popup(menu)
        outer.pack_start(more, False, False, 0)
        self.add(outer)
        self.show_all()
        self.update(row, {}, [], True)

    def update(self, row: dict, current: dict, pending: list[str], has_token: bool) -> None:
        """Redraw from a view-model row and the supervisor's current/pending tasks."""
        self.data = row
        self.name.set_markup(f"<b>{GLib.markup_escape_text(row['name'])}</b>")
        self.name.set_tooltip_text(row["name"])
        status = cat.row_status(row, current, pending)
        _set_markup(self.state, status)
        self.state.set_visible(bool(status))
        details = cat.row_details(row)
        _set_markup(self.details, details)
        self.details.set_tooltip_text(row.get("fit_reason") or None)
        label, action, sensitive = cat.state_button(row, current, pending, has_token)
        self.primary.set_label(label)
        self.primary.set_sensitive(sensitive)
        style(
            self.primary,
            "primary"
            if action in ("download", "activate", "nodes", "token")
            else "danger"
            if action == "cancel"
            else None,
        )
        self.action = action
        if current.get("model_id") == row["id"] and current.get("type") == "download":
            total = current.get("total") or 0
            self.bar.set_fraction(min(1.0, (current.get("done") or 0) / total) if total else 0.0)
            self.bar.show()
        else:
            self.bar.hide()
        self.remove_item.set_sensitive(bool(row["installed"] or row["partial"]) and not current)
        self.login_item.set_visible(bool(row.get("gated")) and row.get("origin") != "civitai")


class _AllRows:
    """``get_row_at_index`` over every section in display order (test hook compatibility)."""

    def __init__(self, panel: ModelsPanel) -> None:
        self.panel = panel

    def get_row_at_index(self, index: int) -> ModelRow | None:
        order = self.panel.display_order()
        return self.panel._widgets.get(order[index]) if 0 <= index < len(order) else None


class LoginPanel(Gtk.Box):
    """Step-by-step help for a model that needs a Hugging Face login; holds the key field."""

    def __init__(self, panel: ModelsPanel) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.panel = panel
        self.get_style_context().add_class("sb-steps")
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.title = Gtk.Label(xalign=0)
        self.title.set_line_wrap(True)
        head.pack_start(self.title, True, True, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Hide these steps")
        close.connect("clicked", lambda *_: panel.hide_login())
        head.pack_end(close, False, False, 0)
        self.pack_start(head, False, False, 0)
        self.note = _grey(small=True)
        self.note.set_line_wrap(True)
        self.note.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.pack_start(self.note, False, False, 0)
        self.grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.pack_start(self.grid, False, False, 0)
        self.token = Gtk.Entry()
        self.token.set_visibility(False)
        self.token.set_placeholder_text("hf_...")
        self.token.set_hexpand(True)
        self.token.connect("activate", lambda *_: panel.save_token())
        self.save = Gtk.Button(label="Save")
        self.save.connect("clicked", lambda *_: panel.save_token())
        style(self.save, "primary")

    def show_for(self, row: dict, token: str, note: str = "") -> None:
        """Fill the steps for ``row`` (see ``catalog.gated_steps``)."""
        for child in self.grid.get_children():
            self.grid.remove(child)
        self.title.set_markup(
            f"<b>{GLib.markup_escape_text(row['name'])} needs a free Hugging Face login.</b> "
            "Four steps, once for all such models:"
        )
        _set_markup(self.note, note)
        self.note.set_visible(bool(note))
        steps = cat.gated_steps(row)
        for i, (text, button, url) in enumerate(steps):
            number = Gtk.Label(label=f"{i + 1}.", xalign=1, yalign=0)
            words = Gtk.Label(label=text, xalign=0)
            words.set_line_wrap(True)
            words.set_max_width_chars(60)
            self.grid.attach(number, 0, i, 1, 1)
            self.grid.attach(words, 1, i, 1, 1)
            if button and url:
                b = Gtk.Button(label=button)
                b.set_valign(Gtk.Align.START)
                b.connect("clicked", lambda _b, u=url: _open(u))
                self.grid.attach(b, 2, i, 1, 1)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for w in (self.token, self.save):
            parent = w.get_parent()
            if parent is not None:
                parent.remove(w)
        line.pack_start(self.token, True, True, 0)
        line.pack_start(self.save, False, False, 0)
        self.grid.attach(line, 1, len(steps), 2, 1)
        self.token.set_text(token)
        self.show_all()
        self.note.set_visible(bool(note))


class ModelsPanel(Gtk.Box):
    """The Models tab of the Settings dialog (polls the supervisor once per second)."""

    def __init__(self, app: App, intro: str = "") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        _install_css()
        self.app = app
        self.get_style_context().add_class("sb-models")
        self.set_border_width(12)
        self._server_files = None
        self._server_checked = 0.0
        self._server_busy = False
        self._last_rows_refresh = 0.0
        self._rows: list[dict] = []
        self._widgets: dict[str, ModelRow] = {}
        self._layout_key: tuple = ()
        self._shown_error = None
        self._selected: str | None = None
        self._login_for: str | None = None
        self._timer = 0
        self._alive = True
        self._hw: hardware.Hardware | None = None
        self._live: list[cat.ModelEntry] = []
        self._live_raw: dict[str, dict] = {}
        self._repo_stats: dict[str, dict] = {}
        self._hub_note = ""
        self.list = _AllRows(self)

        app.require_backend_install()  # without ComfyUI the tab shows that message instead (dialogs.py)
        self._bundled = [e for e in app.catalog if e.source == "bundled"]
        app.catalog = cat.merge_entries(app.catalog, cat.load_selected(app.paths.base, self._bundled))

        if intro:
            first = Gtk.Label(xalign=0)
            first.set_markup(f"<b>{GLib.markup_escape_text(intro)}</b>")
            first.set_line_wrap(True)
            self.pack_start(first, False, False, 0)
            hint = Gtk.Label(label=cat.FIRST_RUN_HINT, xalign=0)  # the starting set and the quality upgrade
            hint.set_line_wrap(True)
            self.pack_start(hint, False, False, 0)
            self._starters = [e for e in app.catalog if e.id in cat.STARTER_SET and e.source == "bundled"]
            if self._starters:
                size = sum(int(f.size or 0) for e in self._starters for f in e.files)
                self.starter_button = Gtk.Button(label=f"Download both starting models ({cat.human_size(size)})")
                self.starter_button.set_halign(Gtk.Align.START)
                self.starter_button.connect("clicked", self._download_starters)
                self.pack_start(self.starter_button, False, False, 0)
        self.hw_label = _grey("Checking this PC...")
        self.pack_start(self.hw_label, False, False, 0)

        self.sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._boxes: dict[str, Gtk.ListBox] = {}
        self._headers: dict[str, Gtk.Widget] = {}
        for key in (cat.SECTION_INSTALLED, cat.SECTION_RECOMMENDED, cat.SECTION_VERIFIED, cat.SECTION_MORE):
            box = Gtk.ListBox()
            box.set_selection_mode(Gtk.SelectionMode.NONE)
            box.set_activate_on_single_click(False)
            box.set_header_func(self._separator)
            self._boxes[key] = box
            if key == cat.SECTION_MORE:
                expander = Gtk.Expander()
                expander.set_label_widget(self._section_label(cat.SECTION_TITLES[key]))
                expander.add(box)
                expander.get_style_context().add_class("sb-section")
                self._headers[key] = expander
                self.sections.pack_start(expander, False, False, 0)
            else:
                header = self._section_label(cat.SECTION_TITLES[key])
                header.get_style_context().add_class("sb-section")
                wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                wrapper.pack_start(header, False, False, 0)
                wrapper.pack_start(box, False, False, 0)
                self._headers[key] = wrapper
                self.sections.pack_start(wrapper, False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.NONE)
        scroller.set_min_content_height(320)
        scroller.add(self.sections)
        self.scroller = scroller
        self.pack_start(scroller, True, True, 0)

        self.login = LoginPanel(self)
        self.login_revealer = Gtk.Revealer()
        self.login_revealer.add(self.login)
        self.pack_start(self.login_revealer, False, False, 0)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.message = Gtk.Label(xalign=0)
        self.message.set_line_wrap(True)
        self.message.set_selectable(True)
        footer.pack_start(self.message, True, True, 0)
        folder = Gtk.Button(label="Open models folder")
        folder.connect("clicked", self._on_open_folder)
        footer.pack_end(folder, False, False, 0)
        self.pack_start(footer, False, False, 0)

        self.show_all()
        self._refresh_rows(force=True)
        self._start_hub()
        self._timer = GLib.timeout_add(1000, self._tick)
        self.connect("destroy", lambda *_: self.stop())

    # layout helpers ------------------------------------------------------

    @staticmethod
    def _section_label(title: str) -> Gtk.Label:
        label = Gtk.Label(xalign=0)
        label.set_markup(f"<small><b>{GLib.markup_escape_text(title.upper())}</b></small>")
        label.get_style_context().add_class("dim-label")
        label.set_margin_start(6)
        label.set_margin_bottom(2)
        return label

    @staticmethod
    def _separator(row: Gtk.ListBoxRow, before: Gtk.ListBoxRow | None) -> None:
        if before is None:
            row.set_header(None)
        elif row.get_header() is None:
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.get_style_context().add_class("sb-sep")
            row.set_header(sep)

    def display_order(self) -> list[str]:
        """Model ids in the order they are drawn (all sections, collapsed ones included)."""
        out: list[str] = []
        for key in (cat.SECTION_INSTALLED, cat.SECTION_RECOMMENDED, cat.SECTION_VERIFIED, cat.SECTION_MORE):
            out += [r.model_id for r in self._boxes[key].get_children() if isinstance(r, ModelRow)]
        return out

    # helpers -------------------------------------------------------------

    def stop(self) -> None:
        """Stop polling and close the downloads window (the dialog is closing)."""
        self._alive = False
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._close_downloads_window()

    def select(self, model_id: str) -> None:
        """Scroll to a model row (opening the collapsed section when needed)."""
        widget = self._widgets.get(model_id)
        if widget is None:
            return
        self._selected = model_id
        if widget.get_parent() is self._boxes[cat.SECTION_MORE]:
            self._headers[cat.SECTION_MORE].set_expanded(True)
        widget.primary.grab_focus()

    def _row(self, model_id: str) -> dict | None:
        for r in self._rows:
            if r["id"] == model_id:
                return r
        return None

    def _say(self, text: str) -> None:
        self.message.set_text(text)

    def _has_token(self) -> bool:
        return bool((self.app.settings.hf_token or "").strip())

    def _entries(self) -> list[cat.ModelEntry]:
        return cat.merge_entries(self.app.catalog, self._live)

    # live search ---------------------------------------------------------

    def _start_hub(self) -> None:
        settings = self.app.settings
        search = bool(getattr(settings, "hub_search", True))
        sources = ("huggingface", "civitai") if getattr(settings, "hub_civitai", True) else ("huggingface",)
        if search:
            self._hub_note = "Looking for current models on Hugging Face and Civitai..."
            self._say(self._hub_note)
        models_root = self.app.models_root
        comfy_url = self.app.server.url()
        base = self.app.paths.base
        bundled = list(self._bundled)

        def work() -> None:
            hw = hardware.detect(models_root, comfy_url)
            GLib.idle_add(self._hw_done, hw)  # the fit labels do not wait for the search
            result = None
            if search:
                try:
                    result = hub.search(bundled, hw, hub.cache_path_for(base), sources=sources)
                except Exception as err:  # noqa: BLE001 - the bundled list is always there
                    result = hub.HubResult(problems=[str(err)], offline=True)
            GLib.idle_add(self._hub_done, result)

        threading.Thread(target=work, name="models-hub", daemon=True).start()

    def _hw_done(self, hw: hardware.Hardware) -> bool:
        if not self._alive:
            return False
        self._hw = hw
        _set_markup(self.hw_label, "This PC: " + (hw.summary() or "could not be checked"))
        self._refresh_rows()
        return False

    def _hub_done(self, result: hub.HubResult | None) -> bool:
        if not self._alive:
            return False  # the dialog was closed meanwhile
        if result is not None:
            self._live = cat.parse_live_entries(result.entries, self._bundled)
            self._live_raw = {r["id"]: r for r in result.entries}
            self._repo_stats = dict(result.repo_stats)
            for problem in result.problems:
                self.app.log.info("model search: %s", problem)
            self._hub_note = result.summary()
            self._say(self._hub_note)
        self._refresh_rows(force=True)
        return False

    # refresh -------------------------------------------------------------

    def _pending(self) -> list[str]:
        try:
            return [t.get("model_id") for t in self.app.server.pending_tasks()]
        except Exception:  # noqa: BLE001 - status files are best effort
            return []

    def build_rows(self, server_files: list[str] | None = None) -> list[dict]:
        """View-model rows for every known model (bundled, remembered and live)."""
        entries = self._entries()
        root, nodes = self.app.models_root, self.app.custom_nodes_dir
        active = cat.effective_active(entries, root, nodes, self.app.settings.active_model)
        rows = []
        for e in entries:
            st = cat.install_status(e, root, nodes, entries, server_files)
            on_disk = sum(f.size or 0 for f in e.files if cat.file_installed(root, f))
            verdict = hardware.fit(e, self._hw, on_disk) if self._hw else None  # None until detection finished
            rows.append(
                cat.model_row(
                    e,
                    st,
                    active,
                    self._has_token(),
                    verdict,
                    cat.gated_files_missing(e, root),
                    cat.login_pages(e, root),
                    bool(self._hw and self._hw.is_amd),
                )
            )
        cat.apply_repo_stats(rows, self._repo_stats)
        return rows

    def _start_server_check(self) -> None:
        """Ask the running server which model files it lists, off the GTK main loop
        (a slow server could otherwise freeze the dialog for many seconds)."""
        if self._server_busy:
            return
        self._server_busy = True

        def work() -> None:
            try:
                files = self.app.server_model_files()
            except Exception:  # noqa: BLE001
                files = None
            GLib.idle_add(self._server_done, files)

        threading.Thread(target=work, name="models-server", daemon=True).start()

    def _server_done(self, files: list[str] | None) -> bool:
        self._server_busy = False
        if not self._alive:
            return False
        changed = files != self._server_files
        self._server_files = files
        if changed:
            self._refresh_rows()
        return False

    def _refresh_rows(self, force: bool = False) -> None:
        now = time.monotonic()
        if now - self._server_checked > 15 or force:
            self._server_checked = now
            self._start_server_check()
        self._rows = self.build_rows(self._server_files)
        self._maybe_activate()
        current = self.app.task_status().get("current") or {}
        pending = self._pending()
        has_token = self._has_token()
        sections = cat.build_sections(self._rows)
        layout = tuple((key, tuple(r["id"] for r in rows)) for key, _title, rows in sections)
        for _key, _title, rows in sections:
            for r in rows:
                widget = self._widgets.get(r["id"])
                if widget is None:
                    widget = ModelRow(self, r)
                    self._widgets[r["id"]] = widget
                widget.update(r, current, pending, has_token)
        if layout != self._layout_key:
            self._layout_key = layout
            self._place(sections)
        self._last_rows_refresh = now

    def _place(self, sections: list[tuple[str, str, list[dict]]]) -> None:
        for box in self._boxes.values():
            for child in box.get_children():
                box.remove(child)
        present = {key for key, _t, _r in sections}
        for key, title, rows in sections:
            for r in rows:
                self._boxes[key].add(self._widgets[r["id"]])
            if key == cat.SECTION_MORE:
                self._headers[key].set_label_widget(self._section_label(f"{title} ({len(rows)})"))
                self._headers[key].get_label_widget().show()
        for key, header in self._headers.items():
            header.set_visible(key in present)
        for box in self._boxes.values():
            box.invalidate_headers()

    def _maybe_activate(self) -> None:
        """The first model that finishes downloading becomes the active model."""
        installed = [e for e in self._entries() if any(r["id"] == e.id and r["installed"] for r in self._rows)]
        usable = cat.runnable_installed(installed, self.app.models_root, self.app.custom_nodes_dir, "fill")
        suggestion = cat.suggest_active(usable, self.app.settings.active_model)
        if not suggestion:
            return
        try:
            entry = self.app.set_active(suggestion)
        except (BackendError, OSError) as err:
            self.app.log.warning("could not make %s the active model: %s", suggestion, err)
            return
        for r in self._rows:
            r["active"] = r["id"] == suggestion
        self._say(f"{entry.name} is installed and is now the active model.")

    def _tick(self) -> bool:
        try:
            self._tick_once()
        except Exception as err:  # noqa: BLE001 - a raising GLib callback would stop polling for good
            self.app.log.warning("models tab refresh failed: %s", err)
        return True

    def _tick_once(self) -> None:
        if time.monotonic() - self._last_rows_refresh > 3:
            self._refresh_rows()
            return
        status = self.app.task_status()
        current = status.get("current") or {}
        pending = self._pending()
        has_token = self._has_token()
        for r in self._rows:
            widget = self._widgets.get(r["id"])
            if widget is not None:
                widget.update(r, current, pending, has_token)
        if not current:
            history = status.get("history") or []
            if history and history[0].get("state") == "error" and history[0].get("finished") != self._shown_error:
                last = history[0]
                self._shown_error = last.get("finished")
                row = self._row(str(last.get("model_id") or "")) or {}
                if last.get("code") == "auth" and row and row.get("origin") != "civitai":
                    self.show_login(
                        row["id"],
                        "Hugging Face refused the download. Usually the license was not accepted yet "
                        "(step 2), access is still pending, or the key was not copied completely.",
                    )
                self._say(
                    ui_text.with_next_step(
                        f"The last task for {row.get('name', last.get('model_id'))} failed: {last.get('message')}"
                    )
                )

    # actions -------------------------------------------------------------

    def primary_clicked(self, model_id: str) -> None:
        """The row's state button: download, cancel, set active, install nodes or ask for a login."""
        widget = self._widgets.get(model_id)
        action = getattr(widget, "action", None)
        self._selected = model_id
        if action == "download":
            self._download(model_id)
        elif action == "cancel":
            try:
                self.app.server.request_cancel()
            except OSError as err:
                self._say(ui_text.with_next_step(f"The download could not be cancelled: {err}"))
                return
            self._say("Download cancelled. The part already downloaded is kept, so Resume download continues.")
        elif action == "activate":
            self._set_active(model_id)
        elif action == "nodes":
            self._install_nodes(model_id)
        elif action == "token":
            self.show_login(model_id)
        self._refresh_rows(force=True)

    def show_login(self, model_id: str, note: str = "") -> None:
        """Open the step-by-step Hugging Face login panel for ``model_id``."""
        row = self._row(model_id)
        if row is None:
            return
        self._login_for = model_id
        self.login.show_for(row, self.app.settings.hf_token or "", note)
        self.login_revealer.set_reveal_child(True)
        self.login.token.grab_focus()
        self._say(f"{row['name']} needs a Hugging Face login first; follow the steps below.")

    def hide_login(self) -> None:
        """Close the login panel."""
        self.login_revealer.set_reveal_child(False)
        self._login_for = None

    def save_token(self) -> None:
        """Save the key typed into the login panel."""
        text = self.login.token.get_text().strip()
        try:
            self.app.save_token(text)
        except (BackendError, OSError) as err:
            self._say(ui_text.save_failed(str(self.app.paths.config), str(err)))
            return
        row = self._row(self._login_for or "") or {}
        if not text:
            self._say("Key removed.")
        elif row:
            self._say(f"Key saved. Press Download on {row['name']} to start.")
        else:
            self._say("Key saved.")
        self.hide_login()
        self._refresh_rows(force=True)

    def _entry(self, model_id: str) -> cat.ModelEntry | None:
        try:
            return cat.by_id(self._entries(), model_id)
        except cat.CatalogError:
            return None

    def _download_starters(self, button: Gtk.Button) -> None:
        """One click for the starting set (LaMa and DreamShaper 8), skipping what is already installed."""
        have = {m.id for m in self.app.installed_models()}
        button.set_sensitive(False)
        for entry in self._starters:
            if entry.id not in have:
                self._download(entry.id)

    def _download(self, model_id: str) -> None:
        entry = self._entry(model_id)
        if entry is None:
            return
        if not entry.checksum_known and not self._confirm_unverified(entry):
            return
        try:
            if not entry.verified:
                raw = self._live_raw.get(model_id)
                if raw is not None:
                    entry = cat.remember_live_entry(self.app.paths.base, raw, self._bundled)
                self.app.catalog = cat.merge_entries(self.app.catalog, [entry])
            ids = self.app.queue_download(model_id)
            self._say(
                f"Downloading {entry.name}"
                + (f" ({len(ids)} parts)" if len(ids) > 1 else "")
                + ". It continues in the background even if you close this window."
            )
            self._open_downloads_window()
        except cat.CatalogError as err:
            self._say(ui_text.with_next_step(f"{entry.name} cannot be downloaded: {err}"))
        except BackendError as err:
            self._say(ui_text.with_next_step(str(err)))
        except OSError as err:
            self._say(ui_text.save_failed(str(self.app.paths.config), str(err)))

    def _confirm_unverified(self, entry: cat.ModelEntry) -> bool:
        """Ask before downloading a file whose publisher lists no SHA-256 checksum."""
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Download a file that cannot be checked?",
        )
        dialog.format_secondary_text(ui_text.unverified_download_question(entry.name))
        answer = dialog.run()
        dialog.destroy()
        return answer == Gtk.ResponseType.OK

    def _set_active(self, model_id: str) -> None:
        try:
            entry = self.app.set_active(model_id)
            self._say(f"{entry.name} is now the active model.")
        except BackendError as err:
            self._say(ui_text.with_next_step(str(err)))
        except OSError as err:
            self._say(ui_text.save_failed(str(self.app.paths.config), str(err)))

    def _install_nodes(self, model_id: str) -> None:
        try:
            nodes = self.app.nodes_to_install(model_id)
        except BackendError as err:
            self._say(ui_text.with_next_step(str(err)))
            return
        if not nodes:
            self._say("Nothing to install.")
            return
        lines = "\n".join(f"- {n.repo} @ {n.commit[:10]} ({n.license or 'see repository'})" for n in nodes)
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Install extra parts into ComfyUI?",
        )
        dialog.format_secondary_text(
            f"This model needs extra ComfyUI nodes. This downloads and installs them (with ComfyUI's own "
            f"Python):\n{lines}\n\nThe AI backend restarts afterwards; a running generation would be interrupted."
        )
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            return
        try:
            self.app.queue_node_install(model_id)
            self._say("Installing the extra parts. The AI backend restarts when it is done.")
        except BackendError as err:
            self._say(ui_text.with_next_step(str(err)))

    def remove_clicked(self, model_id: str) -> None:
        """Menu entry Delete downloaded files: confirm, then queue the removal."""
        r = self._row(model_id)
        if not r:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Delete the files of '{r['name']}' ({r['size']})?",
        )
        dialog.format_secondary_text(
            "Files shared with other downloaded models are kept. You can download it again later."
        )
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            return
        try:
            self.app.remove_model(model_id)
        except (BackendError, OSError, cat.CatalogError) as err:
            self._say(ui_text.with_next_step(f"The files of {r['name']} could not be deleted: {err}"))
            return
        self._say(f"Deleting the files of {r['name']}...")
        self._refresh_rows(force=True)

    def _on_open_folder(self, *_: object) -> None:
        try:
            self.app.open_models_folder()
        except OSError as err:
            self._say(
                f"Could not open the folder: {err}. Next step: open {self.app.models_root} in the "
                "file manager yourself."
            )

    # floating downloads window (same window class as generation progress) ----

    _dl_window = None
    _dl_timer = 0

    def _open_downloads_window(self) -> None:
        if not self.app.settings.progress_window or self._dl_window is not None:
            return
        try:
            from .progress_window import ProgressWindow

            self._dl_window = ProgressWindow(
                "Model downloads",
                "",
                on_cancel=self.app.server.request_cancel,
                position=(self.app.settings.progress_window_x, self.app.settings.progress_window_y),
                show_preview=False,
                quit_main_on_close=False,
            )
            self._dl_timer = GLib.timeout_add(1000, self._tick_downloads)
        except Exception as err:  # noqa: BLE001 - the window is optional
            self._dl_window = None
            self._say(
                f"The download continues, but the progress window could not be opened: {err}. "
                "Next step: watch the row's progress here."
            )

    def _tick_downloads(self) -> bool:
        win = self._dl_window
        if win is None:
            return False
        try:
            status = self.app.task_status()
            pending = [t.get("model_id") or t.get("type", "task") for t in self.app.server.pending_tasks()]
        except Exception as err:  # noqa: BLE001 - keep polling; the status files are best effort
            self.app.log.debug("download status poll failed: %s", err)
            return True
        current = status.get("current") or {}
        win.set_downloads(current, pending)
        if current.get("type") == "download":
            done, total = current.get("done") or 0, current.get("total") or 0
            win.set_step(
                done / total if total else 0.0, f"Downloading {cat.human_size(done)} of {cat.human_size(total)}"
            )
            return True
        if current or pending:
            win.set_step(0.0, f"{current.get('type', 'task')}: {current.get('state', 'working')}")
            return True
        history = status.get("history") or []
        last = history[0] if history else {}
        ok = last.get("state") == "done"
        win.finish(
            ok, "Download complete" if ok else f"{last.get('state', 'stopped')}: {last.get('message', '')}", 1500
        )
        self._dl_window = None
        self._dl_timer = 0
        return False

    def _close_downloads_window(self) -> None:
        if self._dl_timer:
            GLib.source_remove(self._dl_timer)
            self._dl_timer = 0
        if self._dl_window is not None:
            try:
                self._dl_window.destroy()
            except Exception:  # noqa: BLE001 - already destroyed with the dialog
                pass
            self._dl_window = None


def _apply_test_hooks(app: App, panel: ModelsPanel, dialog: Gtk.Dialog) -> None:
    """Test hooks: ``models_starter_button`` presses the starting-set button, ``models_download`` ([ids])
    presses those rows; the dialog closes once the models are installed (or a download failed)."""
    from .ui_setup import test_hooks

    hooks = test_hooks()
    ids = list(hooks.get("models_download") or [])
    use_button = bool(hooks.get("models_starter_button")) and getattr(panel, "starter_button", None) is not None
    if use_button:
        ids = list(cat.STARTER_SET)
    if not ids:
        return

    def press() -> bool:
        if use_button:
            panel.starter_button.clicked()
        else:
            for model_id in ids:
                panel.primary_clicked(model_id)
        GLib.timeout_add(3000, check)
        return False

    def check() -> bool:
        status = app.task_status()
        busy = status.get("current") or app.server.pending_tasks()
        history = status.get("history") or []
        failed = [h for h in history[: len(ids) + 1] if h.get("state") not in ("done", None)]
        if not busy and (failed or set(ids) <= {m.id for m in app.installed_models()}):
            dialog.response(Gtk.ResponseType.CLOSE)
            return False
        return True

    GLib.timeout_add(4000, press)


def run_models_dialog(app: App, message: str = cat.NO_MODEL_MESSAGE, parent: Any = None) -> None:
    """The download manager on its own (first run: no model installed yet).

    Shows ``message`` above the Models tab. Returns when the window is closed;
    downloads continue in the background.
    """
    dialog = Gtk.Dialog(title="AI models", transient_for=parent, modal=True)
    dialog.add_button("Close", Gtk.ResponseType.CLOSE)
    dialog.set_default_size(760, 600)
    try:
        panel: Gtk.Widget = ModelsPanel(app, intro=message)
    except BackendError as err:
        panel = Gtk.Label(label=ui_text.with_next_step(str(err)), xalign=0)
        panel.set_line_wrap(True)
        panel.set_margin_start(12)
        panel.set_margin_end(12)
    dialog.get_content_area().pack_start(panel, True, True, 0)
    dialog.show_all()
    if isinstance(panel, ModelsPanel):
        _apply_test_hooks(app, panel, dialog)
    try:
        dialog.run()
    finally:
        if isinstance(panel, ModelsPanel):
            panel.stop()
        dialog.destroy()
