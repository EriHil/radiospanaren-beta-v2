"""Skärmanpassning för Signalspanaren.

Känner av upplösningen på den skärm fönstret visas på och anpassar efter den:
  * huvudfönstret får en startstorlek som ryms på skärmen (maximeras på små skärmar),
  * alla andra fönster krymps vid första visningen så att de ryms (fast storlek i koden
    som 1250×780 blir annars större än en 1366×768-skärm),
  * kompakt läge (tätare rader och flikar) på låga skärmar – växlar automatiskt när
    fönstret flyttas till en annan skärm.

Storlekar är logiska bildpunkter (efter skalning), alltså det GTK ritar i.
Lägen: "auto" (efter skärmen), "normal", "kompakt".
"""
import logging

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk

log = logging.getLogger("signalspanaren")

KOMPAKT_HOJD = 900          # logisk skärmhöjd under detta → kompakt läge
LITEN_BREDD = 1440          # mindre än så (eller lägre än KOMPAKT_HOJD) → maximera huvudfönstret
MARGINAL_B = 0.96           # andel av skärmen ett fönster får ta (lämnar plats för panel/aktivitetsfält)
MARGINAL_H = 0.90
FALLBACK = (1920, 1080, 1)

# Väljarna matchar libadwaitas egna (row > box.header har min-height 50px där).
KOMPAKT_CSS = """
window.kompakt row > box.header { min-height: 36px; }
window.kompakt preferencesgroup > box > box.header { margin-bottom: 4px; }
window.kompakt preferencesgroup > box > box.header > box.single-line { min-height: 26px; }
window.kompakt notebook > header tab { padding: 2px 10px; min-height: 24px; }
window.kompakt toolbarview > .top-bar headerbar, window.kompakt headerbar.titlebar { min-height: 38px; }
window.kompakt button:not(.circular) { min-height: 26px; padding-top: 2px; padding-bottom: 2px; }
window.kompakt dropdown > button, window.kompakt .analys-ram { padding-top: 3px; padding-bottom: 3px; }
window.kompakt .analys-klass { font-size: 13pt; }
"""

_installerad = False
_css = None


def skarm(widget=None):
    """(bredd, höjd, skala) för skärmen widgeten visas på, annars första skärmen."""
    disp = Gdk.Display.get_default()
    if disp is None:
        return FALLBACK
    mon = None
    if widget is not None:
        root = widget.get_root() if hasattr(widget, "get_root") else widget
        surf = root.get_surface() if root is not None else None
        if surf is not None:
            try:
                mon = disp.get_monitor_at_surface(surf)
            except Exception:
                mon = None
    if mon is None:
        mons = disp.get_monitors()
        if mons.get_n_items() == 0:
            return FALLBACK
        mon = mons.get_item(0)
    g = mon.get_geometry()
    if g.width <= 0 or g.height <= 0:
        return FALLBACK
    return g.width, g.height, mon.get_scale_factor()


def beskriv(widget=None):
    w, h, s = skarm(widget)
    return f"{w}×{h} (skala {s})"


def ryms(w, h, widget=None):
    """Krymper (w, h) så att det ryms på skärmen."""
    mw, mh, _s = skarm(widget)
    return max(320, min(int(w), int(mw * MARGINAL_B))), max(240, min(int(h), int(mh * MARGINAL_H)))


def ar_kompakt(lage, widget=None):
    if lage == "kompakt":
        return True
    if lage == "normal":
        return False
    return skarm(widget)[1] < KOMPAKT_HOJD


def startstorlek(sparad, widget=None):
    """Startstorlek för huvudfönstret: (bredd, höjd, maximera)."""
    mw, mh, _s = skarm(widget)
    if mw < LITEN_BREDD or mh < KOMPAKT_HOJD:
        w, h = ryms(*(sparad or (mw, mh)), widget)
        return w, h, True
    if sparad:
        return (*ryms(*sparad, widget), False)
    return int(mw * 0.9), int(mh * 0.88), False


def sidopanel_bredd(sparad, widget=None):
    """Sidopanelens bredd: sparad om den är rimlig för skärmen, annars efter skärmbredden."""
    mw = skarm(widget)[0]
    hogst = int(mw * 0.28)
    if sparad:
        return max(240, min(int(sparad), hogst))
    return max(260, min(380, int(mw * 0.17)))


def satt_kompakt(win, on):
    if on:
        win.add_css_class("kompakt")
    else:
        win.remove_css_class("kompakt")


def folj_skarm(win, lage_fn, andrad=None):
    """Byter kompakt läge när fönstret flyttas till en annan skärm. lage_fn() ger aktuellt läge."""
    def koppla(*_):
        surf = win.get_surface()
        if surf is None or getattr(win, "_skarm_kopplad", False):
            return
        win._skarm_kopplad = True

        def ny_skarm(_s, mon):
            g = mon.get_geometry()
            on = ar_kompakt(lage_fn(), win)
            satt_kompakt(win, on)
            log.info("Skärm: %d×%d (skala %d) – %s läge", g.width, g.height, mon.get_scale_factor(),
                     "kompakt" if on else "normalt")
            if andrad is not None:
                andrad(on)
        surf.connect("enter-monitor", ny_skarm)
    win.connect("realize", koppla)
    if win.get_realized():
        koppla()


def installera():
    """Kompakt-CSS och att alla fönster krymps till skärmen när de visas första gången."""
    global _installerad, _css
    if _installerad:
        return
    _installerad = True
    disp = Gdk.Display.get_default()
    if disp is not None:
        _css = Gtk.CssProvider()
        _css.load_from_string(KOMPAKT_CSS)
        Gtk.StyleContext.add_provider_for_display(disp, _css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)
    orig = Gtk.Window.present

    def present(self, *a):
        try:
            if not self.get_mapped() and not self.is_maximized():
                dw, dh = self.get_default_size()
                if dw > 0 or dh > 0:
                    ref = self.get_transient_for() or self
                    mw, mh, _s = skarm(ref)
                    nw = min(dw, int(mw * MARGINAL_B)) if dw > 0 else dw
                    nh = min(dh, int(mh * MARGINAL_H)) if dh > 0 else dh
                    if (nw, nh) != (dw, dh):
                        log.info("Skärm %d×%d: fönstret \"%s\" krymps från %d×%d till %d×%d",
                                 mw, mh, self.get_title() or type(self).__name__, dw, dh, nw, nh)
                        self.set_default_size(nw, nh)
        except Exception:
            log.exception("Skärmanpassning misslyckades")
        return orig(self, *a)

    Gtk.Window.present = present
