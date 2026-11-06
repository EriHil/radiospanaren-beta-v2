#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 erihil
"""
Flygradar – ADS-B-mottagare med karta. Del av Signalspanaren.

Tar emot flygplanens ADS-B-sändningar på 1090 MHz med en RTL-SDR och visar dem
på en OpenStreetMap-karta (libshumate) och i en tabell. Utan libshumate visas en
enkel radarvy i stället.

Starta:  flygradar            (eller knappen "Öppna Flygradar" i Signalspanaren)
         flygradar --simulator --start
         flygradar --sjalvtest
"""
import os
import sys
import json
import math
import time
import argparse
import shutil
import subprocess
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
import signalspanaren as R      # noqa: E402  (källor, loggning, felrapport)
import adsb as A               # noqa: E402

log = R.log
RADAR_ID = "se.eh.Signalspanaren.Flygradar"
RADAR_CONF = os.path.join(R.CONF_DIR, "flygradar.json")
SOURCE_NAMES = ["RTL-SDR (USB)", "rtl_tcp (nätverk)", "Simulator (test utan SDR)", "adsb.lol (internet, ingen dongel)"]
SOURCES = ["rtl", "tcp", "sim", "adsblol"]
ADSBLOL_UA = f"Signalspanaren-Flygradar/{R.VERSION} (+https://github.com/EriHil/signalspanaren)"
EMERGENCY = {"7500": "KAPNING", "7600": "RADIOFEL", "7700": "NÖDLÄGE"}


def load_rconf():
    base = R.load_conf()
    conf = {"source": base.get("source", "rtl") if base.get("source") in SOURCES else "rtl",
            "tcp_addr": base.get("tcp_addr", "127.0.0.1:1234"), "ppm": base.get("ppm", 0),
            "gain": None, "lat": 59.33, "lon": 18.06, "zoom": 7.5, "window": [1350, 850],
            "trails": True, "units": "ft"}
    try:
        with open(RADAR_CONF, encoding="utf-8") as f:
            conf.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error("Kunde inte läsa %s: %s", RADAR_CONF, e)
    return conf


def save_rconf(conf):
    try:
        with open(RADAR_CONF + ".tmp", "w", encoding="utf-8") as f:
            json.dump(conf, f, indent=2, ensure_ascii=False)
        os.replace(RADAR_CONF + ".tmp", RADAR_CONF)
    except OSError as e:
        log.error("Kunde inte spara %s: %s", RADAR_CONF, e)


class SimAdsbSource(R.BaseSource):
    kind = "sim"
    name = "Simulator (ADS-B)"

    def open(self):
        self.sim = A.AdsbSim()
        self.t_next = time.monotonic()
        self._take()

    def read(self):
        self._take()
        self.t_next += A.ADSB_BLOCK / A.ADSB_RATE
        d = self.t_next - time.monotonic()
        if d > 0:
            time.sleep(d)
        elif d < -0.5:
            self.t_next = time.monotonic()
        return self.sim.block(A.ADSB_BLOCK), A.ADSB_FREQ, None


def make_source(conf):
    k = conf["source"]
    if k == "sim":
        return SimAdsbSource()
    if k == "tcp":
        s = R.TcpSource(conf["tcp_addr"], rate=A.ADSB_RATE, block=A.ADSB_BLOCK)
    else:
        s = R.RtlSource(rate=A.ADSB_RATE, block=A.ADSB_BLOCK)
    s.set("freq", A.ADSB_FREQ)
    s.set("gain", 49.6 if conf.get("gain") is None else conf["gain"])   # ADS-B vill ha hög förstärkning
    if conf.get("ppm"):
        s.set("ppm", conf["ppm"])
    return s


def map_install_cmd():
    """Kommando som installerar libshumate (kartan) på den här distributionen, via pkexec.
    Returnerar (argv, beskrivning) eller (None, manuell instruktion)."""
    osr = {}
    try:
        with open("/etc/os-release") as f:
            osr = dict(line.rstrip().split("=", 1) for line in f if "=" in line)
    except OSError:
        pass
    ids = (osr.get("ID", "") + " " + osr.get("ID_LIKE", "")).replace('"', "").lower()
    if any(k in ids for k in ("fedora", "rhel", "centos")):
        cmd, txt = ["dnf", "install", "-y", "libshumate"], "sudo dnf install libshumate"
    elif any(k in ids for k in ("debian", "ubuntu", "linuxmint")):
        cmd = ["sh", "-c", "apt-get update && apt-get install -y gir1.2-shumate-1.0"]
        txt = "sudo apt install gir1.2-shumate-1.0"
    else:
        return None, "Installera libshumate (GObject-introspection, Shumate 1.0) med systemets pakethanterare."
    if os.environ.get("SIGNALSPANAREN_TORRKORNING"):        # test: kör inget på riktigt
        return ["true"], txt
    if not shutil.which("pkexec"):
        return None, txt
    return ["pkexec"] + cmd, txt


def fmt_alt(ft, units):
    if ft is None:
        return ""
    return f"{ft:,} ft".replace(",", " ") if units == "ft" else f"{ft * 0.3048:,.0f} m".replace(",", " ")


# ════════════════════════════════════════════════════════════════════
def run_gui(args):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, Gdk, GLib, Gio, GObject
    try:
        if os.environ.get("SIGNALSPANAREN_UTAN_KARTA"):
            raise ImportError("avstängd för test")
        gi.require_version("Shumate", "1.0")
        from gi.repository import Shumate
    except (ValueError, ImportError):
        Shumate = None

    class Row(GObject.Object):
        __gsignals__ = {"updated": (GObject.SignalFlags.RUN_FIRST, None, ())}

        def __init__(self, data):
            super().__init__()
            self.data = data

    def _bind(f, li, fn):
        lbl = li.get_child()
        row = li.get_item()

        def upd(r):
            lbl.set_text(fn(r.data))
            if r.data.get("emergency"):
                lbl.add_css_class("error")
            else:
                lbl.remove_css_class("error")
        upd(row)
        lbl._hid = (row, row.connect("updated", upd))

    def _unbind(f, li):
        lbl = li.get_child()
        h = getattr(lbl, "_hid", None)
        if h:
            h[0].disconnect(h[1])
            lbl._hid = None

    # ── Flygplanssymbol ──
    class PlaneIcon(Gtk.DrawingArea):
        def __init__(self):
            super().__init__()
            self.set_content_width(26)
            self.set_content_height(26)
            self.track = 0
            self.alt = None
            self.emergency = False
            self.selected = False
            self.set_draw_func(self._draw)

        def _draw(self, area, cr, w, h, *_):
            cr.translate(w / 2, h / 2)
            cr.rotate(math.radians(self.track or 0))
            # färg efter höjd: låg = orange, hög = blå; nödläge = röd
            if self.emergency:
                r, g, b = 1, 0.15, 0.15
            else:
                t = min(1.0, max(0.0, (self.alt or 0) / 40000))
                r, g, b = 1 - 0.8 * t, 0.55 + 0.1 * t, 0.1 + 0.85 * t
            pts = [(0, -11), (2, -4), (10, 2), (10, 4), (2, 1), (1.5, 7), (4, 10), (4, 11.5),
                   (0, 10), (-4, 11.5), (-4, 10), (-1.5, 7), (-2, 1), (-10, 4), (-10, 2), (-2, -4)]
            cr.move_to(*pts[0])
            for p in pts[1:]:
                cr.line_to(*p)
            cr.close_path()
            cr.set_source_rgb(r, g, b)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.8)
            cr.set_line_width(1.6 if self.selected else 0.8)
            if self.selected:
                cr.set_source_rgb(1, 1, 1)
            cr.stroke()

    # ── Enkel radarvy (om libshumate saknas) ──
    class FallbackRadar(Gtk.DrawingArea):
        def __init__(self, win):
            super().__init__(hexpand=True, vexpand=True)
            self.win = win
            self.km_per_px = 0.6
            self.set_draw_func(self._draw)
            sc = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            sc.connect("scroll", self._scroll)
            self.add_controller(sc)
            dr = Gtk.GestureDrag()
            dr.connect("drag-begin", lambda g, x, y: setattr(self, "_c0", (win.conf["lat"], win.conf["lon"])))
            dr.connect("drag-update", self._drag)
            self.add_controller(dr)

        def _scroll(self, c, dx, dy):
            self.km_per_px *= 1.25 if dy > 0 else 0.8
            self.queue_draw()
            return True

        def _drag(self, g, dx, dy):
            lat0, lon0 = self._c0
            self.win.conf["lat"] = lat0 + dy * self.km_per_px / 111.0
            self.win.conf["lon"] = lon0 - dx * self.km_per_px / (111.0 * math.cos(math.radians(lat0)))
            self.queue_draw()

        def center_on(self, lat, lon):
            self.win.conf["lat"], self.win.conf["lon"] = lat, lon
            self.queue_draw()

        def _xy(self, lat, lon, W, H):
            c = self.win.conf
            x = W / 2 + (lon - c["lon"]) * 111.0 * math.cos(math.radians(c["lat"])) / self.km_per_px
            y = H / 2 - (lat - c["lat"]) * 111.0 / self.km_per_px
            return x, y

        def _draw(self, area, cr, W, H, *_):
            cr.set_source_rgb(0.05, 0.09, 0.08)
            cr.paint()
            cr.set_source_rgba(0.3, 1, 0.5, 0.25)
            cr.set_line_width(1)
            for km in (25, 50, 100, 200, 300):
                cr.new_sub_path()
                cr.arc(W / 2, H / 2, km / self.km_per_px, 0, 2 * math.pi)
                cr.stroke()
                cr.move_to(W / 2 + 3, H / 2 - km / self.km_per_px - 2)
                cr.show_text(f"{km} km")
            for ac in self.win.planes.values():
                d = ac["data"]
                if d.get("lat") is None:
                    continue
                x, y = self._xy(d["lat"], d["lon"], W, H)
                cr.save()
                cr.translate(x, y)
                cr.rotate(math.radians(d.get("track") or 0))
                cr.move_to(0, -8)
                cr.line_to(5, 6)
                cr.line_to(-5, 6)
                cr.close_path()
                cr.set_source_rgb(1, 0.2, 0.2) if d.get("emergency") else cr.set_source_rgb(0.4, 1, 0.6)
                cr.fill()
                cr.restore()
                cr.set_source_rgb(0.85, 1, 0.9)
                cr.move_to(x + 8, y - 2)
                cr.show_text(f"{d.get('callsign') or d['icao']}  {fmt_alt(d.get('alt'), self.win.conf['units'])}")

    # ── Huvudfönster ──
    class RadarWindow(Adw.ApplicationWindow):
        def __init__(self, app, conf):
            super().__init__(application=app, title="Flygradar – Signalspanaren")
            self.conf = conf
            self.rx = None
            self.planes = {}          # icao -> {"data", "row", "marker", "icon", "label", "path"}
            self.selected = None
            self.last_valid = 0
            self.last_t = time.time()
            self.auto_centered = False
            import skarm
            skarm.installera()
            w, h = skarm.ryms(*(conf.get("window") or [1350, 850]))
            self.set_default_size(w, h)

            self.toasts = Adw.ToastOverlay()
            tv = Adw.ToolbarView()
            hb = Adw.HeaderBar()
            self.btn = Gtk.ToggleButton(label="▶ Starta")
            self.btn.add_css_class("suggested-action")
            self.btn.connect("toggled", self._on_toggle)
            hb.pack_start(self.btn)
            self.src_dd = Gtk.DropDown.new_from_strings(SOURCE_NAMES)
            self.src_dd.set_selected(SOURCES.index(conf["source"]))
            self.src_dd.connect("notify::selected", lambda d, p: conf.__setitem__("source", SOURCES[d.get_selected()]))
            hb.pack_start(self.src_dd)
            self.status = Gtk.Label(label="Tryck ▶ Starta för att lyssna på 1090 MHz")
            hb.set_title_widget(self.status)
            b = Gtk.Button(label="Visa alla")
            b.set_tooltip_text("Centrera kartan på flygplanen")
            b.connect("clicked", lambda *_: self.fit_all())
            hb.pack_end(b)
            self.units_btn = Gtk.Button(label="ft")
            self.units_btn.set_tooltip_text("Byt höjdenhet (fot/meter)")
            self.units_btn.connect("clicked", lambda *_: self._toggle_units())
            self.units_btn.set_label(conf["units"])
            hb.pack_end(self.units_btn)
            tv.add_top_bar(hb)

            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            paned.set_start_child(self._build_map())
            paned.set_end_child(self._build_table())
            paned.set_position(int(w * 0.58))
            self.toasts.set_child(paned)
            tv.set_content(self.toasts)
            self.set_content(tv)

            GLib.timeout_add(1000, self._tick)
            self.connect("close-request", self._on_close)

        # ── karta ──
        def _build_map(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            if Shumate is not None:
                if hasattr(Shumate, "set_user_agent"):
                    Shumate.set_user_agent(f"Signalspanaren-Flygradar/{R.VERSION} "
                                           "(+https://github.com/EriHil/signalspanaren)")
                self.smap = Shumate.SimpleMap(vexpand=True, hexpand=True)
                reg = Shumate.MapSourceRegistry.new_with_defaults()
                self.smap.set_map_source(reg.get_by_id(Shumate.MAP_SOURCE_OSM_MAPNIK))
                self.viewport = self.smap.get_viewport()
                self.viewport.set_zoom_level(self.conf["zoom"])
                self.smap.get_map().center_on(self.conf["lat"], self.conf["lon"])
                self.marker_layer = Shumate.MarkerLayer.new(self.viewport)
                self.smap.add_overlay_layer(self.marker_layer)
                box.append(self.smap)
                self.fallback = None
            else:
                cmd, txt = map_install_cmd()
                self.map_cmd = cmd
                bar = Gtk.Box(spacing=10)
                bar.add_css_class("toolbar")
                bar.set_margin_start(8)
                bar.set_margin_end(8)
                self.map_lbl = Gtk.Label(xalign=0, hexpand=True, wrap=True)
                bar.append(self.map_lbl)
                self.map_btn = Gtk.Button(label="Installera kartan")
                self.map_btn.add_css_class("suggested-action")
                self.map_btn.connect("clicked", lambda *_: self._confirm_install())
                bar.append(self.map_btn)
                if os.environ.get("SIGNALSPANAREN_EFTER_KARTINSTALLATION"):
                    self.map_lbl.set_text("Kartan installerades men kan ändå inte laddas – starta om datorn "
                                          f"eller installera manuellt: {txt}")
                    self.map_btn.set_visible(False)
                    log.error("libshumate går inte att ladda trots installation")
                elif cmd:
                    self.map_lbl.set_text("Kartan är inte installerad – en enkel radarvy visas i stället.")
                else:
                    self.map_lbl.set_text(f"Kartan är inte installerad. Installera den med: {txt}")
                    self.map_btn.set_visible(False)
                box.append(bar)
                self.fallback = FallbackRadar(self)
                box.append(self.fallback)
                self.smap = None
                log.warning("libshumate saknas – visar enkel radarvy i stället för karta")
            return box

        def _confirm_install(self):
            if getattr(self, "_installing", False):
                return
            _cmd, txt = map_install_cmd()
            dlg = Adw.AlertDialog(heading="Installera kartan?",
                                  body="Kartan (libshumate) installeras med systemets pakethanterare "
                                       f"({txt}). Du får ange ditt lösenord. Flygradar startar sedan om.")
            dlg.add_response("cancel", "Avbryt")
            dlg.add_response("install", "Installera")
            dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")
            dlg.connect("response", lambda d, r: self._install_map() if r == "install" else None)
            dlg.present(self)

        def _install_map(self):
            if getattr(self, "_installing", False):
                return
            self._installing = True
            self.map_lbl.set_text("Installerar kartan … ange ditt lösenord i fönstret som öppnas.")
            self.map_btn.set_sensitive(False)
            log.info("Installerar kartan: %s", " ".join(self.map_cmd))

            def work():
                try:
                    r = subprocess.run(self.map_cmd, capture_output=True, text=True, timeout=900)
                    code, out = r.returncode, (r.stdout + r.stderr)[-1500:]
                except Exception as e:
                    code, out = -1, str(e)
                GLib.idle_add(self._install_done, code, out)
            threading.Thread(target=work, name="Kartinstallation", daemon=True).start()

        def _install_done(self, code, out):
            self._installing = False
            if code == 0:
                log.info("Kartan installerad – startar om Flygradar")
                self.map_lbl.set_text("Kartan är installerad – Flygradar startar om …")
                GLib.timeout_add(1200, self._restart)
                return False
            if code in (126, 127):
                log.info("Kartinstallationen avbröts (pkexec %d)", code)
                self.map_lbl.set_text("Installationen avbröts. Kartan är inte installerad.")
            else:
                log.error("Kartinstallationen misslyckades (kod %d): %s", code, out)
                self.map_lbl.set_text("Installationen misslyckades – se Signalspanaren → Felsökning → Kopiera felrapport.")
            self.map_btn.set_label("Försök igen")
            self.map_btn.set_sensitive(True)
            return False

        def _restart(self):
            was_running = self.btn.get_active()
            self._on_close()
            argv = [sys.executable, os.path.abspath(__file__)] + [a for a in sys.argv[1:] if a != "--start"]
            if was_running:
                argv.append("--start")
            log.info("Startar om: %s", " ".join(argv))
            os.environ["SIGNALSPANAREN_EFTER_KARTINSTALLATION"] = "1"     # högst en omstart
            os.execv(sys.executable, argv)

        def center_on(self, lat, lon, zoom=None):
            if self.smap:
                mp = self.smap.get_map()
                if zoom and hasattr(mp, "go_to_full"):
                    mp.go_to_full(lat, lon, zoom)
                else:
                    if zoom:
                        self.viewport.set_zoom_level(zoom)
                    mp.go_to(lat, lon)
            elif self.fallback:
                self.fallback.center_on(lat, lon)

        def fit_all(self):
            pts = [(p["data"]["lat"], p["data"]["lon"]) for p in self.planes.values() if p["data"].get("lat")]
            if not pts:
                self.toast("Inga flygplan med position ännu")
                return
            lats = [p[0] for p in pts]
            lons = [p[1] for p in pts]
            clat, clon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
            span = max(max(lats) - min(lats), (max(lons) - min(lons)) * math.cos(math.radians(clat)), 0.05)
            zoom = max(3.0, min(12.0, math.log2(170 / span)))
            self.center_on(clat, clon, zoom)

        # ── tabell ──
        def _build_table(self):
            self.store = Gio.ListStore(item_type=Row)
            cv = Gtk.ColumnView()
            cv.add_css_class("data-table")
            u = lambda: self.conf["units"]
            cols = [
                ("ICAO", lambda d: d["icao"], "icao"),
                ("Anrop", lambda d: d.get("callsign") or "", "callsign"),
                ("Höjd", lambda d: fmt_alt(d.get("alt"), u()), "alt"),
                ("Fart", lambda d: f"{d['gs']} kn" if d.get("gs") is not None else "", "gs"),
                ("Kurs", lambda d: f"{d['track']}°" if d.get("track") is not None else "", "track"),
                ("Stig/sjunk", lambda d: f"{d['vr']:+} ft/min" if d.get("vr") is not None else "", "vr"),
                ("Squawk", lambda d: (d.get("squawk") or "") + (f" {EMERGENCY[d['squawk']]}"
                                                                 if d.get("squawk") in EMERGENCY else ""), "squawk"),
                ("Position", lambda d: f"{d['lat']:.3f}, {d['lon']:.3f}" if d.get("lat") is not None else "", "lat"),
                ("Medd.", lambda d: str(d["msgs"]), "msgs"),
                ("Senast", lambda d: f"{max(0, time.time() - d['last']):.0f} s", "last"),
            ]
            for title, fn, key in cols:
                fac = Gtk.SignalListItemFactory()
                fac.connect("setup", lambda f, li: li.set_child(Gtk.Label(xalign=0)))
                fac.connect("bind", _bind, fn)
                fac.connect("unbind", _unbind)
                col = Gtk.ColumnViewColumn(title=title, factory=fac)
                col.set_resizable(True)

                def cmp(a, b, *_, key=key):
                    va, vb = a.data.get(key), b.data.get(key)
                    if va == vb:
                        return Gtk.Ordering.EQUAL
                    if va is None:
                        return Gtk.Ordering.LARGER
                    if vb is None:
                        return Gtk.Ordering.SMALLER
                    return Gtk.Ordering.SMALLER if va < vb else Gtk.Ordering.LARGER
                col.set_sorter(Gtk.CustomSorter.new(cmp, None))
                cv.append_column(col)
                if key == "msgs":
                    msgs_col = col
            sortm = Gtk.SortListModel(model=self.store, sorter=cv.get_sorter())
            self.sel = Gtk.SingleSelection(model=sortm, autoselect=False, can_unselect=True)
            self.sel.connect("notify::selected-item", self._on_select)
            cv.set_model(self.sel)
            cv.sort_by_column(msgs_col, Gtk.SortType.DESCENDING)
            cv.connect("activate", lambda c, pos: self._open_osm(self.sel.get_item(pos)))
            sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
            sw.set_child(cv)
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            hint = Gtk.Label(label="Klicka: visa på kartan · Dubbelklicka: öppna i OpenStreetMap", xalign=0)
            hint.add_css_class("dim-label")
            hint.set_margin_start(8)
            hint.set_margin_top(4)
            hint.set_margin_bottom(4)
            v.append(hint)
            v.append(sw)
            return v

        def _on_select(self, sel, _p):
            item = sel.get_selected_item()
            old = self.planes.get(self.selected)
            if old and old.get("icon"):
                old["icon"].selected = False
                old["icon"].queue_draw()
            self.selected = item.data["icao"] if item else None
            p = self.planes.get(self.selected)
            if p:
                if p.get("icon"):
                    p["icon"].selected = True
                    p["icon"].queue_draw()
                if p["data"].get("lat") is not None:
                    self.center_on(p["data"]["lat"], p["data"]["lon"])

        def _open_osm(self, item):
            if not item or item.data.get("lat") is None:
                return
            d = item.data
            uri = f"https://www.openstreetmap.org/?mlat={d['lat']:.5f}&mlon={d['lon']:.5f}#map=10/{d['lat']:.5f}/{d['lon']:.5f}"
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
            except GLib.Error as e:
                log.error("Kunde inte öppna kartan: %s", e)

        def _toggle_units(self):
            self.conf["units"] = "m" if self.conf["units"] == "ft" else "ft"
            self.units_btn.set_label(self.conf["units"])
            for p in self.planes.values():
                p["row"].emit("updated")
                self._update_marker(p)

        # ── mottagning ──
        def _on_toggle(self, btn):
            if btn.get_active():
                on_error = lambda m: GLib.idle_add(self._on_error, m)
                if self.conf["source"] == "adsblol":
                    self.rx = A.AdsbLolFeed(self.conf["lat"], self.conf["lon"], ADSBLOL_UA, on_error=on_error)
                else:
                    try:
                        src = make_source(self.conf)
                    except R.SourceError as e:
                        self.toast(str(e))
                        btn.set_active(False)
                        return
                    self.rx = A.AdsbReceiver(src, on_error=on_error,
                                             on_stopped=lambda: GLib.idle_add(self._on_stopped))
                self.rx.start()
                btn.set_label("■ Stoppa")
                btn.remove_css_class("suggested-action")
                btn.add_css_class("destructive-action")
                self.src_dd.set_sensitive(False)
                self.last_valid, self.last_t = 0, time.time()
            else:
                if self.rx:
                    self.rx.stop()
                btn.set_label("▶ Starta")
                btn.remove_css_class("destructive-action")
                btn.add_css_class("suggested-action")
                self.src_dd.set_sensitive(True)

        def _on_error(self, msg):
            self.toast(msg, 8)
            return False

        def _on_stopped(self):
            if self.btn.get_active():
                self.btn.set_active(False)
            return False

        # ── uppdatering ──
        def _update_marker(self, p):
            d = p["data"]
            if d.get("lat") is None:
                return
            if self.smap:
                if p.get("marker") is None:
                    icon = PlaneIcon()
                    lbl = Gtk.Label()
                    lbl.add_css_class("plane-label")
                    bx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                    bx.append(icon)
                    bx.append(lbl)
                    g = Gtk.GestureClick()
                    g.connect("pressed", lambda *_a, icao=d["icao"]: self._select_icao(icao))
                    bx.add_controller(g)
                    m = Shumate.Marker()
                    m.set_child(bx)
                    self.marker_layer.add_marker(m)
                    p.update(marker=m, icon=icon, label=lbl)
                    if self.conf.get("trails"):
                        path = Shumate.PathLayer.new(self.viewport)
                        col = Gdk.RGBA()
                        col.parse("rgba(30,120,220,0.7)")
                        path.set_stroke_color(col)
                        path.set_stroke_width(2)
                        self._add_trail_layer(path)
                        p["path"] = path
                        p["trail"] = []
                p["marker"].set_location(d["lat"], d["lon"])
                ic = p["icon"]
                ic.track, ic.alt, ic.emergency = d.get("track") or 0, d.get("alt"), bool(d.get("emergency"))
                ic.queue_draw()
                p["label"].set_text(f"{d.get('callsign') or d['icao']}\n{fmt_alt(d.get('alt'), self.conf['units'])}")
                if p.get("path") is not None:
                    tr = p["trail"]
                    if not tr or tr[-1] != (d["lat"], d["lon"]):
                        tr.append((d["lat"], d["lon"]))
                        p["path"].add_node(Shumate.Coordinate.new_full(d["lat"], d["lon"]))
            elif self.fallback:
                self.fallback.queue_draw()

        def _add_trail_layer(self, path):
            """Lägger spårlagret under flygplanssymbolerna, på både nya och gamla libshumate."""
            try:
                if hasattr(self.smap, "insert_overlay_layer_behind"):        # libshumate 1.3+
                    self.smap.insert_overlay_layer_behind(path, self.marker_layer)
                    return
            except GLib.Error:
                pass
            # libshumate 1.2 (Ubuntu 24.04, Mint 22): lägg till spåret och flytta symbolerna överst igen
            self.smap.remove_overlay_layer(self.marker_layer)
            self.smap.add_overlay_layer(path)
            self.smap.add_overlay_layer(self.marker_layer)

        def _select_icao(self, icao):
            m = self.sel.get_model()
            for i in range(m.get_n_items()):
                if m.get_item(i).data["icao"] == icao:
                    self.sel.set_selected(i)
                    return

        def _remove(self, icao):
            p = self.planes.pop(icao)
            ok, pos = self.store.find(p["row"])
            if ok:
                self.store.remove(pos)
            if p.get("marker") is not None:
                self.marker_layer.remove_marker(p["marker"])
            if p.get("path") is not None:
                self.smap.remove_overlay_layer(p["path"])

        def _tick(self):
            if not self.rx:
                return True
            snap, new = self.rx.dec.snapshot(max_age=120)
            seen = set()
            for d in snap:
                icao = d["icao"]
                seen.add(icao)
                d["emergency"] = d.get("squawk") in EMERGENCY
                p = self.planes.get(icao)
                if p is None:
                    p = {"data": d, "row": Row(d)}
                    self.planes[icao] = p
                    self.store.append(p["row"])
                    log.info("ADS-B: nytt flygplan %s", icao)
                else:
                    was_em = p["data"].get("emergency")
                    p["data"] = d
                    p["row"].data = d
                    p["row"].emit("updated")
                    if d["emergency"] and not was_em:
                        msg = f"{d.get('callsign') or icao} sänder squawk {d['squawk']} – {EMERGENCY[d['squawk']]}"
                        self.toast(msg, 15)
                        log.warning("ADS-B: %s", msg)
                self._update_marker(p)
            for icao in [k for k in self.planes if k not in seen]:
                self._remove(icao)
            if not self.auto_centered and any(p["data"].get("lat") for p in self.planes.values()):
                self.auto_centered = True
                self.fit_all()
            st = self.rx.dec.stats
            now = time.time()
            rate = (st["valid"] - self.last_valid) / max(0.1, now - self.last_t)
            self.last_valid, self.last_t = st["valid"], now
            npos = sum(1 for p in self.planes.values() if p["data"].get("lat") is not None)
            if isinstance(self.rx, A.AdsbLolFeed):
                fart = (f"data från adsb.lol · uppdaterad för {now - self.rx.last_ok:.0f} s sedan"
                        if self.rx.last_ok else "väntar på adsb.lol")
            else:
                fart = f"{rate:.0f} meddelanden/s"
            self.status.set_text(f"{len(self.planes)} flygplan · {npos} med position · "
                                 f"{fart}" if self.rx.running else "Stoppad")
            return True

        def toast(self, msg, timeout=4):
            t = Adw.Toast.new(msg)
            t.set_timeout(timeout)
            self.toasts.add_toast(t)
            return False

        def save_screenshot(self, path, tries=10):
            try:
                paint = Gtk.WidgetPaintable.new(self)
                snap = Gtk.Snapshot()
                paint.snapshot(snap, self.get_width(), self.get_height())
                node = snap.to_node()
                if node is None:
                    if tries:
                        GLib.timeout_add(500, lambda: self.save_screenshot(path, tries - 1) and False)
                    return False
                self.get_renderer().render_texture(node, None).save_to_png(path)
                log.info("Skärmbild sparad: %s", path)
            except Exception:
                log.exception("Kunde inte spara skärmbild")
            return False

        def _on_close(self, *_):
            try:
                if self.smap:
                    self.conf["zoom"] = float(self.viewport.get_zoom_level())
                    self.conf["lat"] = float(self.viewport.get_latitude())
                    self.conf["lon"] = float(self.viewport.get_longitude())
                self.conf["window"] = [self.get_width(), self.get_height()]
                save_rconf(self.conf)
                if self.rx:
                    self.rx.stop()
            except Exception:
                log.exception("Fel vid avslut av Flygradar")
            log.info("Flygradar avslutas")
            return False

    class RadarApp(Adw.Application):
        def __init__(self, args):
            super().__init__(application_id=RADAR_ID,
                             flags=Gio.ApplicationFlags.DEFAULT_FLAGS if len(sys.argv) <= 1
                             else Gio.ApplicationFlags.NON_UNIQUE)
            self.args = args
            self.win = None
            self.connect("activate", self._activate)

        def _activate(self, app):
            if self.win:
                self.win.present()
                return
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            css = Gtk.CssProvider()
            css.load_from_string(".plane-label { background: rgba(0,0,0,0.65); color: white; border-radius: 4px; "
                                 "padding: 0 3px; font-size: 8pt; font-weight: bold; }")
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            conf = load_rconf()
            a = self.args
            if a.source:
                conf["source"] = a.source
            if a.tcp:
                conf["source"], conf["tcp_addr"] = "tcp", a.tcp
            if a.gain is not None:
                conf["gain"] = a.gain
            self.win = RadarWindow(self, conf)
            self.win.present()
            R.on_unix_signals(lambda: self.win.close())
            if a.start:
                GLib.idle_add(lambda: self.win.btn.set_active(True) or False)
            if a.skarmbild:
                GLib.timeout_add(int(float(a.skarmbild[1]) * 1000),
                                 lambda: self.win.save_screenshot(a.skarmbild[0]) and False)
            log.info("Flygradar öppnad (karta: %s)", "libshumate" if Shumate else "enkel radarvy")

    log.info("=== Flygradar (Signalspanaren %s) startar ===", R.VERSION)
    return RadarApp(args).run([sys.argv[0]])


def main():
    p = argparse.ArgumentParser(prog="flygradar", description="Flygradar – ADS-B-mottagare med karta (Signalspanaren)")
    p.add_argument("--version", action="version", version=f"Flygradar (Signalspanaren {R.VERSION})")
    p.add_argument("--kalla", dest="source", choices=SOURCES, help="mottagarkälla: rtl, tcp, sim eller adsblol")
    p.add_argument("--simulator", action="store_true", help="samma som --kalla sim")
    p.add_argument("--tcp", metavar="VÄRD:PORT", help="anslut till en rtl_tcp-server")
    p.add_argument("--forstarkning", dest="gain", type=float, metavar="DB", help="förstärkning (standard 49,6 dB)")
    p.add_argument("--start", action="store_true", help="börja ta emot direkt")
    p.add_argument("--sjalvtest", action="store_true", help="testa ADS-B-avkodaren (utan fönster)")
    p.add_argument("--skarmbild", nargs=2, metavar=("FIL", "SEKUNDER"), help="spara skärmbild efter angiven tid")
    p.add_argument("--debug", action="store_true", help="detaljerad loggning")
    args = p.parse_args()
    if args.simulator:
        args.source = "sim"
    R.setup_logging(verbose=args.debug)
    if args.sjalvtest:
        ok = A.selftest()
        print("RESULTAT: " + ("OK" if ok else "FEL"))
        return 0 if ok else 1
    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
