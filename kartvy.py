# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 erihil
"""
Gemensam kartvy för Radiospanaren (används av fartygskartan).

MapPanel visar objekt (fartyg, flygplan, stationer) på en OpenStreetMap-karta via
libshumate, med symbol i kursriktningen, etikett och spår. Saknas libshumate visas
en enkel radarvy och en knapp som installerar kartan.
"""
import os
import math
import shutil
import logging
import subprocess
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib   # noqa: E402

try:
    if os.environ.get("RADIOSPANAREN_UTAN_KARTA"):
        raise ImportError("avstängd för test")
    gi.require_version("Shumate", "1.0")
    from gi.repository import Shumate
except (ValueError, ImportError):
    Shumate = None

log = logging.getLogger("radiospanaren")

# Färger (r, g, b) per kategori
COLORS = {
    "last": (0.25, 0.75, 0.3), "tank": (0.85, 0.2, 0.2), "passagerare": (0.2, 0.45, 0.95),
    "fiske": (1.0, 0.55, 0.1), "militar": (0.3, 0.32, 0.35), "fritid": (0.65, 0.35, 0.85),
    "tjanst": (0.0, 0.7, 0.75), "sar": (1.0, 0.0, 0.0), "okand": (0.6, 0.6, 0.6),
}
CATEGORY_NAMES = {
    "last": "Lastfartyg", "tank": "Tankfartyg", "passagerare": "Passagerarfartyg", "fiske": "Fiske",
    "militar": "Militärt", "fritid": "Fritid/segel", "tjanst": "Bogser/lots/SAR/myndighet",
    "sar": "Nödsändare (AIS-SART)", "okand": "Okänd typ",
}


def map_install_cmd():
    """(argv för pkexec, manuell instruktion) för att installera libshumate."""
    ids = ""
    try:
        with open("/etc/os-release") as f:
            osr = dict(line.rstrip().split("=", 1) for line in f if "=" in line)
        ids = (osr.get("ID", "") + " " + osr.get("ID_LIKE", "")).replace('"', "").lower()
    except OSError:
        pass
    if any(k in ids for k in ("fedora", "rhel", "centos")):
        cmd, txt = ["dnf", "install", "-y", "libshumate"], "sudo dnf install libshumate"
    elif any(k in ids for k in ("debian", "ubuntu", "linuxmint")):
        cmd = ["sh", "-c", "apt-get update && apt-get install -y gir1.2-shumate-1.0"]
        txt = "sudo apt install gir1.2-shumate-1.0"
    else:
        return None, "installera libshumate med systemets pakethanterare"
    if os.environ.get("RADIOSPANAREN_TORRKORNING"):
        return ["true"], txt
    if not shutil.which("pkexec"):
        return None, txt
    return ["pkexec"] + cmd, txt


class Symbol(Gtk.DrawingArea):
    """Symbol som pekar i kursriktningen. shape: 'ship', 'plane', 'ring' eller 'dot'."""

    def __init__(self, shape="ship", size=24):
        super().__init__()
        self.set_content_width(size)
        self.set_content_height(size)
        self.shape = shape
        self.heading = 0
        self.color = (0.6, 0.6, 0.6)
        self.selected = False
        self.alert = False
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, w, h, *_):
        cr.translate(w / 2, h / 2)
        r, g, b = (1, 0, 0) if self.alert else self.color
        if self.shape in ("ring", "dot"):
            cr.arc(0, 0, 5 if self.shape == "ring" else 4, 0, 2 * math.pi)
            if self.shape == "ring":
                cr.set_source_rgb(r, g, b)
                cr.set_line_width(3)
                cr.stroke_preserve()
                cr.set_source_rgba(0, 0, 0, 0.7)
                cr.set_line_width(1)
                cr.stroke()
            else:
                cr.set_source_rgb(r, g, b)
                cr.fill()
        else:
            cr.rotate(math.radians(self.heading or 0))
            if self.shape == "plane":
                pts = [(0, -11), (2, -4), (10, 2), (10, 4), (2, 1), (1.5, 7), (4, 10), (4, 11.5),
                       (0, 10), (-4, 11.5), (-4, 10), (-1.5, 7), (-2, 1), (-10, 4), (-10, 2), (-2, -4)]
            else:   # fartyg: spetsig för, rak akter
                pts = [(0, -11), (5, -3), (5, 9), (-5, 9), (-5, -3)]
            cr.move_to(*pts[0])
            for p in pts[1:]:
                cr.line_to(*p)
            cr.close_path()
            cr.set_source_rgb(r, g, b)
            cr.fill_preserve()
            if self.selected:
                cr.set_source_rgb(1, 1, 1)
                cr.set_line_width(2)
            else:
                cr.set_source_rgba(0, 0, 0, 0.85)
                cr.set_line_width(0.9)
            cr.stroke()
        if self.alert:
            cr.identity_matrix()
            cr.arc(w / 2, h / 2, min(w, h) / 2 - 1, 0, 2 * math.pi)
            cr.set_source_rgb(1, 0, 0)
            cr.set_line_width(2)
            cr.stroke()


class _Radar(Gtk.DrawingArea):
    """Enkel radarvy när libshumate saknas."""

    def __init__(self, panel):
        super().__init__(hexpand=True, vexpand=True)
        self.p = panel
        self.km_per_px = 0.15
        self.set_draw_func(self._draw)
        sc = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        sc.connect("scroll", lambda c, dx, dy: (setattr(self, "km_per_px", self.km_per_px * (1.25 if dy > 0 else 0.8)),
                                                self.queue_draw(), True)[-1])
        self.add_controller(sc)
        dr = Gtk.GestureDrag()
        dr.connect("drag-begin", lambda g, x, y: setattr(self, "_c0", tuple(self.p.center)))
        dr.connect("drag-update", self._drag)
        self.add_controller(dr)

    def _drag(self, g, dx, dy):
        lat0, lon0 = self._c0
        self.p.center[0] = lat0 + dy * self.km_per_px / 111.0
        self.p.center[1] = lon0 - dx * self.km_per_px / (111.0 * math.cos(math.radians(lat0)))
        self.queue_draw()

    def _draw(self, area, cr, W, H, *_):
        lat_c, lon_c = self.p.center
        cr.set_source_rgb(0.04, 0.08, 0.12)
        cr.paint()
        cr.set_source_rgba(0.4, 0.8, 1, 0.25)
        cr.set_line_width(1)
        for km in (5, 10, 25, 50, 100):
            cr.new_sub_path()
            cr.arc(W / 2, H / 2, km / self.km_per_px, 0, 2 * math.pi)
            cr.stroke()
            cr.move_to(W / 2 + 3, H / 2 - km / self.km_per_px - 2)
            cr.show_text(f"{km} km")
        for o in self.p.objects.values():
            if o["lat"] is None:
                continue
            x = W / 2 + (o["lon"] - lon_c) * 111.0 * math.cos(math.radians(lat_c)) / self.km_per_px
            y = H / 2 - (o["lat"] - lat_c) * 111.0 / self.km_per_px
            cr.save()
            cr.translate(x, y)
            cr.rotate(math.radians(o.get("heading") or 0))
            cr.move_to(0, -8)
            cr.line_to(4, 6)
            cr.line_to(-4, 6)
            cr.close_path()
            cr.set_source_rgb(*(o.get("color") or (0.6, 0.6, 0.6)))
            cr.fill()
            cr.restore()
            cr.set_source_rgb(0.9, 0.95, 1)
            cr.move_to(x + 8, y - 2)
            cr.show_text(o.get("title", ""))


class MapPanel(Gtk.Box):
    def __init__(self, center=(59.33, 18.06), zoom=9.0, user_agent="Radiospanaren", on_select=None, toast=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.center = [center[0], center[1]]
        self.objects = {}
        self.on_select = on_select
        self.toast = toast or (lambda m: None)
        self.selected = None
        if Shumate is not None:
            if hasattr(Shumate, "set_user_agent"):
                Shumate.set_user_agent(user_agent)
            self.smap = Shumate.SimpleMap(vexpand=True, hexpand=True)
            reg = Shumate.MapSourceRegistry.new_with_defaults()
            self.smap.set_map_source(reg.get_by_id(Shumate.MAP_SOURCE_OSM_MAPNIK))
            self.viewport = self.smap.get_viewport()
            self.viewport.set_zoom_level(zoom)
            self.smap.get_map().center_on(center[0], center[1])
            self.markers = Shumate.MarkerLayer.new(self.viewport)
            self.smap.add_overlay_layer(self.markers)
            self.append(self.smap)
            self.radar = None
        else:
            self.smap = None
            cmd, txt = map_install_cmd()
            self._cmd = cmd
            bar = Gtk.Box(spacing=10)
            bar.add_css_class("toolbar")
            self.bar_lbl = Gtk.Label(xalign=0, hexpand=True, wrap=True)
            bar.append(self.bar_lbl)
            self.bar_btn = Gtk.Button(label="Installera kartan")
            self.bar_btn.add_css_class("suggested-action")
            self.bar_btn.connect("clicked", lambda *_: self._confirm_install())
            bar.append(self.bar_btn)
            if cmd:
                self.bar_lbl.set_text("Kartan är inte installerad – en enkel radarvy visas i stället.")
            else:
                self.bar_lbl.set_text(f"Kartan är inte installerad. Installera den med: {txt}")
                self.bar_btn.set_visible(False)
            self.append(bar)
            self.radar = _Radar(self)
            self.append(self.radar)

    # ── objekt ──
    def update(self, oid, lat, lon, heading=0, title="", color=(0.6, 0.6, 0.6), shape="ship", alert=False,
               trail=True):
        o = self.objects.get(oid)
        if o is None:
            o = {"lat": None, "lon": None, "trail": []}
            self.objects[oid] = o
        o.update(lat=lat, lon=lon, heading=heading, title=title, color=color, shape=shape, alert=alert)
        if lat is None:
            return
        if self.smap:
            if "marker" not in o:
                sym = Symbol(shape)
                lbl = Gtk.Label()
                lbl.add_css_class("map-label")
                bx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                bx.append(sym)
                bx.append(lbl)
                g = Gtk.GestureClick()
                g.connect("pressed", lambda *_a, i=oid: self.on_select and self.on_select(i))
                bx.add_controller(g)
                m = Shumate.Marker()
                m.set_child(bx)
                self.markers.add_marker(m)
                o.update(marker=m, sym=sym, lbl=lbl)
                if trail:
                    path = Shumate.PathLayer.new(self.viewport)
                    c = Gdk.RGBA()
                    c.parse("rgba(20,90,200,0.65)")
                    path.set_stroke_color(c)
                    path.set_stroke_width(2)
                    self._add_trail_layer(path)
                    o["path"] = path
            o["marker"].set_location(lat, lon)
            s = o["sym"]
            s.shape, s.heading, s.color, s.alert = shape, heading or 0, color, alert
            s.selected = (oid == self.selected)
            s.queue_draw()
            o["lbl"].set_text(title)
            if o.get("path") is not None and (not o["trail"] or o["trail"][-1] != (lat, lon)):
                o["trail"].append((lat, lon))
                o["path"].add_node(Shumate.Coordinate.new_full(lat, lon))
        elif self.radar:
            self.radar.queue_draw()

    def _add_trail_layer(self, path):
        try:
            if hasattr(self.smap, "insert_overlay_layer_behind"):     # libshumate 1.3+
                self.smap.insert_overlay_layer_behind(path, self.markers)
                return
        except GLib.Error:
            pass
        self.smap.remove_overlay_layer(self.markers)                   # libshumate 1.2
        self.smap.add_overlay_layer(path)
        self.smap.add_overlay_layer(self.markers)

    def remove(self, oid):
        o = self.objects.pop(oid, None)
        if not o or not self.smap:
            return
        if "marker" in o:
            self.markers.remove_marker(o["marker"])
        if o.get("path") is not None:
            self.smap.remove_overlay_layer(o["path"])

    def select(self, oid):
        old = self.objects.get(self.selected)
        if old and "sym" in old:
            old["sym"].selected = False
            old["sym"].queue_draw()
        self.selected = oid
        o = self.objects.get(oid)
        if o and o["lat"] is not None:
            if "sym" in o:
                o["sym"].selected = True
                o["sym"].queue_draw()
            self.center_on(o["lat"], o["lon"])

    def center_on(self, lat, lon, zoom=None):
        if self.smap:
            mp = self.smap.get_map()
            if zoom and hasattr(mp, "go_to_full"):
                mp.go_to_full(lat, lon, zoom)
            else:
                if zoom:
                    self.viewport.set_zoom_level(zoom)
                mp.go_to(lat, lon)
        else:
            self.center[0], self.center[1] = lat, lon
            self.radar.queue_draw()

    def fit_all(self):
        pts = [(o["lat"], o["lon"]) for o in self.objects.values() if o["lat"] is not None]
        if not pts:
            return False
        lats, lons = [p[0] for p in pts], [p[1] for p in pts]
        clat, clon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
        span = max(max(lats) - min(lats), (max(lons) - min(lons)) * math.cos(math.radians(clat)), 0.02)
        self.center_on(clat, clon, max(3.0, min(14.0, math.log2(170 / span))))
        return True

    def view(self):
        if self.smap:
            return float(self.viewport.get_latitude()), float(self.viewport.get_longitude()), \
                float(self.viewport.get_zoom_level())
        return self.center[0], self.center[1], None

    # ── installation av kartan ──
    def _confirm_install(self):
        _c, txt = map_install_cmd()
        dlg = Adw.AlertDialog(heading="Installera kartan?",
                              body=f"Kartan (libshumate) installeras med systemets pakethanterare ({txt}). "
                                   "Du får ange ditt lösenord. Starta sedan om Radiospanaren.")
        dlg.add_response("cancel", "Avbryt")
        dlg.add_response("install", "Installera")
        dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda d, r: self._install() if r == "install" else None)
        dlg.present(self.get_root())

    def _install(self):
        self.bar_btn.set_sensitive(False)
        self.bar_lbl.set_text("Installerar kartan … ange ditt lösenord i fönstret som öppnas.")
        log.info("Installerar kartan: %s", " ".join(self._cmd))

        def work():
            try:
                r = subprocess.run(self._cmd, capture_output=True, text=True, timeout=900)
                code, out = r.returncode, (r.stdout + r.stderr)[-1500:]
            except Exception as e:
                code, out = -1, str(e)
            GLib.idle_add(self._installed, code, out)
        threading.Thread(target=work, name="Kartinstallation", daemon=True).start()

    def _installed(self, code, out):
        if code == 0:
            log.info("Kartan installerad")
            self.bar_lbl.set_text("Kartan är installerad – starta om Radiospanaren för att visa den.")
            self.bar_btn.set_visible(False)
        else:
            if code in (126, 127):
                self.bar_lbl.set_text("Installationen avbröts.")
            else:
                log.error("Kartinstallationen misslyckades (kod %d): %s", code, out)
                self.bar_lbl.set_text("Installationen misslyckades – se Felsökning → Kopiera felrapport.")
            self.bar_btn.set_label("Försök igen")
            self.bar_btn.set_sensitive(True)
        return False
