#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 erihil
"""
Radiospanaren – SDR-mottagare, radioskanner och signallogg för Fedora.

Delar:
  1. Loggning och felrapport
  2. Konfiguration, bandplan, hjälpfunktioner
  3. Mottagarkällor (RTL-SDR via librtlsdr, rtl_tcp, simulator)
  4. DSP-motor (spektrum, demodulering, squelch, inspelning)
  5. Signaldetektor (signalloggen)
  6. Ljudutgång och webbgränssnitt
  7. Grafiskt gränssnitt (GTK4 / libadwaita)
  8. Kommandorad
"""

# ════════════════════════════════════════════════════════════════════
# 1. LOGGNING OCH FELRAPPORT
# ════════════════════════════════════════════════════════════════════
import os
import sys
import logging
import logging.handlers
import traceback
import threading

APP_NAME = "Radiospanaren"
APP_ID = "se.eh.Radiospanaren"
VERSION = "2.4.0"

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.join(HOME, ".local/state")), "radiospanaren")
CONF_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "radiospanaren")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(STATE_DIR, "radiospanaren.log")
STDERR_FILE = os.path.join(STATE_DIR, "stderr.log")
REPORT_FILE = os.path.join(STATE_DIR, "felrapport.txt")
CONF_FILE = os.path.join(CONF_DIR, "config.json")
CSS_FILE = os.path.join(CONF_DIR, "style.css")
MANUAL_FILE = os.path.join(APP_DIR, "MANUAL.txt")

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(CONF_DIR, exist_ok=True)

log = logging.getLogger("radiospanaren")


def _trim_file(path, max_bytes=1_000_000, keep=200_000):
    try:
        if os.path.getsize(path) > max_bytes:
            with open(path, "rb") as f:
                f.seek(-keep, os.SEEK_END)
                data = f.read()
            with open(path, "wb") as f:
                f.write(b"[... aldre rader borttagna ...]\n" + data)
    except OSError:
        pass


def setup_logging(verbose=False):
    _trim_file(STDERR_FILE)
    log.setLevel(logging.DEBUG)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s: %(message)s"))
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        log.addHandler(sh)

    def excepthook(t, v, tb):
        log.critical("Ohanterat fel:\n%s", "".join(traceback.format_exception(t, v, tb)))
        if _gui_error_hook:
            try:
                _gui_error_hook(f"{t.__name__}: {v}")
            except Exception:
                pass

    def thread_excepthook(args):
        log.critical("Ohanterat fel i tråd %s:\n%s", args.thread.name if args.thread else "?",
                     "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


_gui_error_hook = None


def _tail(path, n):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except OSError:
        return []


def build_report(extra=None):
    """Bygger en felrapport som kan klistras in i Claude Code."""
    import platform
    import subprocess
    import datetime
    lines = []
    lines.append(f"=== {APP_NAME} felrapport ({datetime.datetime.now():%Y-%m-%d %H:%M:%S}) ===")
    lines.append(f"Programversion: {VERSION}")
    lines.append(f"Programfil: {os.path.abspath(__file__)}")
    try:
        with open("/etc/os-release") as f:
            osr = dict(l.strip().split("=", 1) for l in f if "=" in l)
        lines.append(f"System: {osr.get('PRETTY_NAME', '?').strip(chr(34))}")
    except OSError:
        pass
    lines.append(f"Kärna: {platform.release()}")
    lines.append(f"Skrivbord: {os.environ.get('XDG_CURRENT_DESKTOP', '?')} / {os.environ.get('XDG_SESSION_TYPE', '?')}")
    lines.append(f"Python: {sys.version.split()[0]}")
    for mod in ("numpy", "scipy"):
        try:
            m = __import__(mod)
            lines.append(f"{mod}: {m.__version__}")
        except Exception as e:
            lines.append(f"{mod}: SAKNAS ({e})")
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw
        lines.append(f"GTK: {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
        lines.append(f"libadwaita: {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}")
    except Exception as e:
        lines.append(f"GTK/libadwaita: FEL ({e})")
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=3).stdout
        sdr = [l for l in out.splitlines() if any(k in l.lower() for k in ("0bda:2838", "0bda:2832", "rtl", "realtek"))]
        lines.append("SDR-enheter (lsusb): " + ("; ".join(sdr) if sdr else "inga hittade"))
    except Exception:
        lines.append("SDR-enheter (lsusb): kunde inte köras")
    try:
        out = subprocess.run(["lsmod"], capture_output=True, text=True, timeout=3).stdout
        if "dvb_usb_rtl28xxu" in out:
            lines.append("VARNING: kärnmodulen dvb_usb_rtl28xxu är laddad (blockerar RTL-SDR)")
    except Exception:
        pass
    if extra:
        lines.append(extra)
    try:
        with open(CONF_FILE) as f:
            lines.append("--- config.json ---")
            lines.append(f.read().strip()[:3000])
    except OSError:
        pass
    lines.append("--- senaste raderna i radiospanaren.log ---")
    lines.extend(l.rstrip("\n") for l in _tail(LOG_FILE, 150))
    err = _tail(STDERR_FILE, 60)
    if err:
        lines.append("--- senaste raderna i stderr.log ---")
        lines.extend(l.rstrip("\n") for l in err)
    lines.append("=== slut på rapport ===")
    text = "\n".join(lines)
    try:
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    return text


# ════════════════════════════════════════════════════════════════════
# 2. KONFIGURATION, BANDPLAN, HJÄLPFUNKTIONER
# ════════════════════════════════════════════════════════════════════
import json
import time
import math
import re
import queue
import socket
import struct
import shutil
import subprocess
import wave
import ctypes
import ctypes.util
import datetime
import csv
from collections import deque

try:
    import numpy as np
    from scipy.signal import firwin, lfilter, butter, oaconvolve
    from scipy import ndimage
    sys.path.insert(0, APP_DIR)
    import avkodare
    import analys
    import analysavkodare
    import iqfil
except ImportError as _e:  # pragma: no cover
    setup_logging()
    log.critical("Saknar Python-paket: %s. Installera med: sudo dnf install python3-numpy python3-scipy", _e)
    print(f"Saknar Python-paket: {_e}\nInstallera med: sudo dnf install python3-numpy python3-scipy", file=sys.stderr)
    sys.exit(1)

SR = 1_536_000          # samplingstakt från SDR:en
BLOCK = 65536           # samples per block (~43 ms)
FFT_N = 4096            # FFT-storlek (375 Hz per bin)
D1 = 4                  # första decimering -> 384 kHz
FS1 = SR // D1
D2 = 8                  # andra decimering -> 48 kHz
AF = 48000              # ljudsamplingstakt
BIN_HZ = SR / FFT_N
CW_TONE = 700.0

MODES = ["WFM", "NFM", "AM", "USB", "LSB", "CW"]
DEFAULT_BW = {"WFM": 180000, "NFM": 12500, "AM": 8000, "USB": 2700, "LSB": 2700, "CW": 500}
DEFAULT_STEP = {"WFM": 100000, "NFM": 12500, "AM": 5000, "USB": 100, "LSB": 100, "CW": 10}
STEPS = [0, 10, 100, 1000, 5000, 9000, 10000, 12500, 25000, 100000]
STEP_NAMES = ["Auto", "10 Hz", "100 Hz", "1 kHz", "5 kHz", "9 kHz", "10 kHz", "12,5 kHz", "25 kHz", "100 kHz"]
IQ_FORMATS = [("SigMF cu8 – 8 bitar, 3 MB/s", "sigmf_cu8"), ("SigMF cf32 – flyttal, 12 MB/s", "sigmf_cf32"),
              ("WAV 16 bitar (SDR++/SDR#), 6 MB/s", "wav16")]
SOURCES = ["rtl", "tcp", "sim", "file"]
SOURCE_NAMES = ["RTL-SDR (USB)", "rtl_tcp (nätverk)", "Simulator (test utan SDR)", "IQ-fil (uppspelning)"]

# Bandplan (MHz): från, till, namn
BANDPLAN = [
    (0.1485, 0.2835, "LV-rundradio"), (0.5265, 1.6065, "MV-rundradio"), (1.81, 2.0, "160 m amatör"),
    (3.5, 3.8, "80 m amatör"), (5.9, 6.2, "49 m rundradio"), (7.0, 7.2, "40 m amatör"),
    (7.2, 7.45, "41 m rundradio"), (9.4, 9.9, "31 m rundradio"), (10.1, 10.15, "30 m amatör"),
    (11.6, 12.1, "25 m rundradio"), (13.57, 13.87, "22 m rundradio"), (14.0, 14.35, "20 m amatör"),
    (15.1, 15.8, "19 m rundradio"), (18.068, 18.168, "17 m amatör"), (21.0, 21.45, "15 m amatör"),
    (24.89, 24.99, "12 m amatör"), (26.965, 27.405, "27 MHz PR"), (28.0, 29.7, "10 m amatör"),
    (50.0, 52.0, "6 m amatör"), (87.5, 108.0, "FM-rundradio"), (108.0, 118.0, "Flygnavigering"),
    (118.0, 137.0, "Flygradio (AM)"), (137.0, 138.0, "Vädersatelliter"), (144.0, 146.0, "2 m amatör"),
    (156.0, 162.025, "Marin VHF"), (174.0, 240.0, "DAB"), (430.0, 440.0, "70 cm amatör"),
    (433.05, 434.79, "ISM 433"), (446.0, 446.2, "PMR446"), (863.0, 870.0, "SRD 868"),
    (1089.0, 1091.0, "ADS-B"), (1240.0, 1300.0, "23 cm amatör"),
]

BANDPLAN += avkodare.EXTRA_BANDS

DEFAULT_BOOKMARKS = [
    {"name": "Flyg nödfrekvens", "freq": 121.5e6, "mode": "AM"},
    {"name": "Marin VHF kanal 16", "freq": 156.8e6, "mode": "NFM"},
    {"name": "PMR446 kanal 1", "freq": 446.00625e6, "mode": "NFM"},
    {"name": "PMR446 kanal 8", "freq": 446.09375e6, "mode": "NFM"},
    {"name": "2 m anropsfrekvens", "freq": 145.5e6, "mode": "NFM"},
    {"name": "70 cm anropsfrekvens", "freq": 433.5e6, "mode": "NFM"},
    {"name": "ISM 433,92 MHz", "freq": 433.92e6, "mode": "NFM"},
    {"name": "FT8 40 m", "freq": 7.074e6, "mode": "USB"},
    {"name": "FT8 20 m", "freq": 14.074e6, "mode": "USB"},
    {"name": "Simulator: FM-station", "freq": 100.0e6, "mode": "WFM"},
    {"name": "Simulator: AM", "freq": 100.35e6, "mode": "AM"},
    {"name": "Simulator: NFM-skurar", "freq": 100.5e6, "mode": "NFM"},
] + avkodare.EXTRA_BOOKMARKS
BOOKMARK_VERSION = 2

DEFAULT_CONF = {
    "source": "rtl", "tcp_addr": "127.0.0.1:1234", "auto_gain": True, "gain": 30.0, "ppm": 0,
    "direct": 0, "biast": False, "freq": 100.0e6, "mode": "WFM", "bw": None, "squelch": -150.0,
    "volume": 80, "step_idx": 0, "db_min": -100.0, "db_max": -20.0, "zoom": 1, "spec_frac": 0.35,
    "colormap": "spanare", "theme": 0, "skin": "spanare", "analys_bw_idx": 0, "analys_follow": False, "scan_mode": 0, "scan_start": "144.0", "scan_stop": "146.0",
    "scan_step_khz": 12.5, "scan_hold": 2.0, "scan_max_listen": 0.0, "scan_dwell_ms": 300, "det_thresh": 12.0,
    "det_enabled": True, "auto_rec": False, "web_enabled": False, "web_port": 8073,
    "web_public": False, "iq_format": 0, "play_file": "", "play_speed": 0, "play_loop": False,
    "play_override": {}, "lockout": [], "bookmarks": DEFAULT_BOOKMARKS, "window": [1400, 900], "sidebar_width": 330,
}

COLORMAPS = {
    "spanare": [(0, (2, 6, 14)), (0.2, (4, 30, 60)), (0.4, (0, 110, 130)), (0.6, (40, 220, 170)),
                (0.78, (255, 200, 70)), (0.92, (255, 90, 40)), (1, (255, 245, 230))],
    "klassisk": [(0, (0, 0, 10)), (0.15, (0, 0, 90)), (0.35, (0, 90, 220)), (0.55, (0, 220, 200)),
                 (0.7, (240, 230, 0)), (0.85, (255, 60, 0)), (1, (255, 255, 255))],
    "grå": [(0, (0, 0, 0)), (1, (255, 255, 255))],
    "grön": [(0, (0, 5, 0)), (0.5, (0, 160, 40)), (0.85, (180, 255, 120)), (1, (255, 255, 255))],
    "inferno": [(0, (0, 0, 4)), (0.25, (87, 16, 110)), (0.5, (188, 55, 84)), (0.75, (249, 142, 9)),
                (1, (252, 255, 164))],
}


# Utseenden: färger för spektrumvyn (r, g, b[, a]) och programmets CSS
SKINS = {
    "spanare": dict(namn="Spanare (mörk marin)", bg=(0.02, 0.04, 0.06), spec_bg=(0.015, 0.035, 0.05),
                    grid=(0.4, 0.9, 0.85, 0.07), text=(0.6, 0.95, 0.9, 0.6), axis_bg=(0.035, 0.065, 0.09),
                    fill_top=(0.15, 0.95, 0.75, 0.38), fill_bot=(0.05, 0.35, 0.4, 0.04), line=(0.45, 1.0, 0.85),
                    band_bg=(0.03, 0.06, 0.085), tuned=(1.0, 0.62, 0.15), chan=(1, 0.75, 0.3, 0.12),
                    analys=(0.95, 0.4, 1.0), track=(0.3, 1, 0.5, 0.85), bm=(1, 0.8, 0.3, 0.95)),
    "klassisk": dict(namn="Klassisk", bg=(0.05, 0.06, 0.08), spec_bg=(0.04, 0.05, 0.07), grid=(1, 1, 1, 0.07),
                     text=(1, 1, 1, 0.45), axis_bg=(0.09, 0.1, 0.13), fill_top=(0.35, 0.75, 1, 0.45),
                     fill_bot=(0.1, 0.3, 0.6, 0.08), line=(0.55, 0.82, 1), band_bg=(0.08, 0.09, 0.11),
                     tuned=(1, 0.25, 0.25), chan=(1, 1, 1, 0.13), analys=(1, 0.75, 0.2), track=(0.3, 1, 0.4, 0.8),
                     bm=(1, 0.85, 0.2, 0.9)),
}

SKIN_CSS = {
    "spanare": """
@define-color accent_color #ffb547;
@define-color accent_bg_color #c97d16;
@define-color accent_fg_color #ffffff;
@define-color window_bg_color #0c131b;
@define-color window_fg_color #d5e6ee;
@define-color view_bg_color #091017;
@define-color view_fg_color #d5e6ee;
@define-color headerbar_bg_color #0f1923;
@define-color headerbar_fg_color #e2f1f7;
@define-color headerbar_border_color #1c2b39;
@define-color headerbar_backdrop_color #0c131b;
@define-color sidebar_bg_color #0e1720;
@define-color card_bg_color rgba(110, 200, 220, 0.05);
@define-color card_shade_color rgba(0, 0, 0, 0.4);
@define-color popover_bg_color #121d28;
@define-color dialog_bg_color #111b25;
@define-color success_color #3ee6a8;
@define-color destructive_bg_color #c0392b;
.freq-entry { font-family: "DejaVu Sans Mono", monospace; font-size: 17pt; font-weight: bold; color: #59ffd0;
  background: #031016; border: 1px solid #13414b; border-radius: 6px; text-shadow: 0 0 6px rgba(89,255,208,0.5); }
notebook > header { background: #0a1118; border-bottom: 1px solid #1c2b39; }
notebook > header tab { padding: 4px 14px; color: alpha(@window_fg_color, 0.6); }
notebook > header tab:checked { color: @accent_color; box-shadow: inset 0 -3px @accent_color; }
levelbar > trough > block.filled { background: linear-gradient(to right, #1fd1a5, #ffb547 70%, #ff5a3c); }
columnview.data-table { font-size: 9.5pt; }
.analys-klass { font-size: 15pt; font-weight: bold; color: @accent_color; }
.analys-text { font-family: monospace; }
.analys-text text { background: #040b10; color: #9dffcf; }
.analys-ram { background: alpha(#6ec8dc, 0.04); border: 1px solid #1c2b39; border-radius: 8px; padding: 6px; }
""",
    "klassisk": """
.analys-klass { font-size: 15pt; font-weight: bold; color: @accent_color; }
.analys-text { font-family: monospace; }
.analys-ram { border: 1px solid alpha(currentColor, 0.15); border-radius: 8px; padding: 6px; }
""",
}


def load_conf():
    conf = json.loads(json.dumps(DEFAULT_CONF))
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            conf.update(json.load(f))
        if conf.get("bm_version", 1) < BOOKMARK_VERSION:
            have = {round(b["freq"]) for b in conf["bookmarks"]}
            new = [b for b in avkodare.EXTRA_BOOKMARKS if round(b["freq"]) not in have]
            conf["bookmarks"] += new
            log.info("La till %d nya standardbokmärken", len(new))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error("Kunde inte läsa %s: %s (standardinställningar används)", CONF_FILE, e)
    conf["bm_version"] = BOOKMARK_VERSION
    return conf


def save_conf(conf):
    try:
        tmp = CONF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(conf, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONF_FILE)
    except Exception as e:
        log.error("Kunde inte spara inställningar: %s", e)


def parse_freq(s):
    """'145.5' -> 145.5 MHz, '7074k' -> 7.074 MHz, '433920000' -> Hz."""
    s = s.strip().lower().replace(" ", "").replace(",", ".")
    m = re.match(r"^([0-9]*\.?[0-9]+)(hz|khz|k|mhz|m|ghz|g)?$", s)
    if not m:
        raise ValueError(f"Ogiltig frekvens: {s!r}")
    v = float(m.group(1))
    u = m.group(2)
    if u in ("hz",):
        return v
    if u in ("k", "khz"):
        return v * 1e3
    if u in ("m", "mhz"):
        return v * 1e6
    if u in ("g", "ghz"):
        return v * 1e9
    if v <= 6000:
        return v * 1e6
    if v <= 6e6:
        return v * 1e3
    return v


def parse_bw(s):
    s = s.strip().lower().replace(",", ".")
    m = re.match(r"^([0-9]*\.?[0-9]+)(hz|k|khz|m|mhz)?$", s)
    if not m:
        raise ValueError(s)
    v = float(m.group(1))
    u = m.group(2)
    if u in ("hz",):
        return v
    if u in ("m", "mhz"):
        return v * 1e6
    return v * 1e3  # standard: kHz


def fmt_freq(hz):
    s = f"{hz / 1e6:.6f}"
    a, b = s.split(".")
    return f"{a}.{b[:3]} {b[3:]}"


def fmt_mhz(hz):
    return f"{hz / 1e6:.4f}"


def fmt_bw(hz):
    if hz < 1000:
        return f"{hz:.0f} Hz"
    return f"{hz / 1000:.1f} kHz".replace(".0 kHz", " kHz")


def band_name(hz):
    mhz = hz / 1e6
    best = ""
    width = 1e9
    for lo, hi, name in BANDPLAN:
        if lo <= mhz <= hi and hi - lo < width:
            best, width = name, hi - lo
    return best


def classify(bw):
    if bw < 800:
        return "Bärvåg/CW"
    if bw < 4000:
        return "SSB/smal data"
    if bw < 25000:
        return "NFM/AM"
    if bw < 300000:
        return "WFM/bred"
    return "Bredband/digital"


def make_lut(name):
    stops = COLORMAPS.get(name, COLORMAPS["klassisk"])
    x = np.linspace(0, 1, 256)
    pos = [s[0] for s in stops]
    r = np.interp(x, pos, [s[1][0] for s in stops]).astype(np.uint32)
    g = np.interp(x, pos, [s[1][1] for s in stops]).astype(np.uint32)
    b = np.interp(x, pos, [s[1][2] for s in stops]).astype(np.uint32)
    return (0xFF000000 | (r << 16) | (g << 8) | b).astype(np.uint32)


def rec_dir():
    base = None
    try:
        from gi.repository import GLib
        base = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC)
    except Exception:
        pass
    d = os.path.join(base or HOME, "Radiospanaren")
    os.makedirs(d, exist_ok=True)
    return d


# ════════════════════════════════════════════════════════════════════
# 3. MOTTAGARKÄLLOR
# ════════════════════════════════════════════════════════════════════
LUT_U8 = ((np.arange(256, dtype=np.float32) - 127.4) / 128.0).astype(np.float32)


class SourceError(Exception):
    pass


class BaseSource:
    kind = "base"
    name = "Källa"

    def __init__(self):
        self.center = 100e6
        self._pending = {}
        self._plock = threading.Lock()

    def set(self, key, val):
        with self._plock:
            self._pending[key] = val
        if key == "freq":
            self.center = float(val)

    def _take(self):
        with self._plock:
            p, self._pending = self._pending, {}
        return p

    def open(self):
        pass

    def close(self):
        pass

    def read(self):
        """Returnerar (iq complex64[BLOCK], center_hz, rå uint8 eller None)."""
        raise NotImplementedError


class RtlSource(BaseSource):
    kind = "rtl"
    name = "RTL-SDR"

    def __init__(self, index=0, rate=SR, block=BLOCK):
        super().__init__()
        self.index = index
        self.rate = int(rate)
        self.block = int(block)
        self.dev = None
        libname = ctypes.util.find_library("rtlsdr") or "librtlsdr.so.2"
        try:
            self.lib = ctypes.CDLL(libname)
        except OSError as e:
            raise SourceError(f"Kunde inte ladda librtlsdr ({e}). Installera: sudo dnf install rtl-sdr")
        L = self.lib
        vp = ctypes.c_void_p
        L.rtlsdr_get_device_count.restype = ctypes.c_uint32
        L.rtlsdr_get_device_name.argtypes = [ctypes.c_uint32]
        L.rtlsdr_get_device_name.restype = ctypes.c_char_p
        L.rtlsdr_open.argtypes = [ctypes.POINTER(vp), ctypes.c_uint32]
        L.rtlsdr_close.argtypes = [vp]
        L.rtlsdr_set_sample_rate.argtypes = [vp, ctypes.c_uint32]
        L.rtlsdr_set_center_freq.argtypes = [vp, ctypes.c_uint32]
        L.rtlsdr_set_tuner_gain_mode.argtypes = [vp, ctypes.c_int]
        L.rtlsdr_set_tuner_gain.argtypes = [vp, ctypes.c_int]
        L.rtlsdr_set_freq_correction.argtypes = [vp, ctypes.c_int]
        L.rtlsdr_set_direct_sampling.argtypes = [vp, ctypes.c_int]
        L.rtlsdr_set_agc_mode.argtypes = [vp, ctypes.c_int]
        L.rtlsdr_reset_buffer.argtypes = [vp]
        L.rtlsdr_read_sync.argtypes = [vp, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.has_biast = hasattr(L, "rtlsdr_set_bias_tee")
        if self.has_biast:
            L.rtlsdr_set_bias_tee.argtypes = [vp, ctypes.c_int]

    def open(self):
        L = self.lib
        n = L.rtlsdr_get_device_count()
        if n == 0:
            raise SourceError("Ingen RTL-SDR hittades. Är dongeln inkopplad? (Testa i terminalen: rtl_test)")
        name = L.rtlsdr_get_device_name(self.index)
        self.dev = ctypes.c_void_p()
        r = L.rtlsdr_open(ctypes.byref(self.dev), self.index)
        if r < 0:
            self.dev = None
            raise SourceError(f"Kunde inte öppna RTL-SDR (felkod {r}). Används den av ett annat program "
                              f"(t.ex. SDR++ eller WaveShark), eller är DVB-drivrutinen laddad?")
        log.info("Öppnade RTL-SDR: %s", name.decode(errors="replace") if name else "?")
        L.rtlsdr_set_sample_rate(self.dev, self.rate)
        L.rtlsdr_set_center_freq(self.dev, int(self.center))
        self._apply(self._take())
        L.rtlsdr_reset_buffer(self.dev)
        self.buf = (ctypes.c_ubyte * (self.block * 2))()
        self.arr = np.ctypeslib.as_array(self.buf)

    def _apply(self, p):
        L, d = self.lib, self.dev
        if "direct" in p:
            L.rtlsdr_set_direct_sampling(d, int(p["direct"]))
            log.info("Direktsampling: %s", p["direct"])
        if "freq" in p:
            r = L.rtlsdr_set_center_freq(d, int(p["freq"]))
            if r < 0:
                log.warning("RTL-SDR kunde inte ställa in %.3f MHz (felkod %d)", p["freq"] / 1e6, r)
        if "gain" in p:
            g = p["gain"]
            if g is None:
                L.rtlsdr_set_tuner_gain_mode(d, 0)
            else:
                L.rtlsdr_set_tuner_gain_mode(d, 1)
                L.rtlsdr_set_tuner_gain(d, int(round(g * 10)))
        if "ppm" in p:
            L.rtlsdr_set_freq_correction(d, int(p["ppm"]))
        if "biast" in p and self.has_biast:
            L.rtlsdr_set_bias_tee(d, 1 if p["biast"] else 0)

    def read(self):
        p = self._take()
        if p:
            self._apply(p)
        n = ctypes.c_int(0)
        r = self.lib.rtlsdr_read_sync(self.dev, self.buf, self.block * 2, ctypes.byref(n))
        if r < 0:
            raise SourceError(f"Läsfel från RTL-SDR (felkod {r}). Drogs dongeln ur?")
        if n.value < self.block * 2:
            return None
        return LUT_U8[self.arr].view(np.complex64), self.center, self.arr

    def close(self):
        if self.dev:
            self.lib.rtlsdr_close(self.dev)
            self.dev = None
            log.info("Stängde RTL-SDR")


class TcpSource(BaseSource):
    """Klient för rtl_tcp – lyssna på en SDR på en annan dator."""
    kind = "tcp"
    name = "rtl_tcp"

    def __init__(self, addr, rate=SR, block=BLOCK):
        super().__init__()
        self.rate = int(rate)
        host, _, port = addr.strip().rpartition(":")
        if not host:
            host, port = addr.strip(), "1234"
        self.host, self.port = host, int(port)
        self.sock = None
        self.buf = bytearray(int(block) * 2)

    def _cmd(self, c, v):
        self.sock.sendall(struct.pack(">BI", c, int(v) & 0xFFFFFFFF))

    def open(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=5)
        except OSError as e:
            raise SourceError(f"Kunde inte ansluta till rtl_tcp på {self.host}:{self.port}: {e}")
        hdr = self._recv_exact(12)
        if hdr[:4] != b"RTL0":
            raise SourceError(f"{self.host}:{self.port} svarar inte som en rtl_tcp-server")
        log.info("Ansluten till rtl_tcp %s:%d", self.host, self.port)
        self._cmd(2, self.rate)
        self._cmd(1, self.center)
        self._apply(self._take())

    def _apply(self, p):
        if "direct" in p:
            self._cmd(9, p["direct"])
        if "freq" in p:
            self._cmd(1, p["freq"])
        if "gain" in p:
            if p["gain"] is None:
                self._cmd(3, 0)
            else:
                self._cmd(3, 1)
                self._cmd(4, round(p["gain"] * 10))
        if "ppm" in p:
            self._cmd(5, p["ppm"])
        if "biast" in p:
            self._cmd(14, 1 if p["biast"] else 0)

    def _recv_exact(self, n, into=None):
        buf = into if into is not None else bytearray(n)
        mv = memoryview(buf)
        got = 0
        while got < n:
            try:
                k = self.sock.recv_into(mv[got:], n - got)
            except socket.timeout:
                raise SourceError("Tidsgräns: rtl_tcp-servern skickar ingen data")
            if k == 0:
                raise SourceError("rtl_tcp-servern stängde anslutningen")
            got += k
        return buf

    def read(self):
        p = self._take()
        if p:
            self._apply(p)
        self._recv_exact(len(self.buf), self.buf)
        arr = np.frombuffer(self.buf, np.uint8)
        return LUT_U8[arr].view(np.complex64), self.center, arr

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class SimSource(BaseSource):
    """Testkälla utan hårdvara: brus + några signaler runt 100 MHz."""
    kind = "sim"
    name = "Simulator"
    SIGS = [(100.0e6, "WFM", 0.30), (100.35e6, "AM", 0.08), (99.8e6, "CW", 0.05),
            (100.5e6, "NFM", 0.10), (99.62e6, "USB", 0.03), (145.5e6, "NFM", 0.05),
            (131.725e6, "ACARS", 0.05), (161.975e6, "AIS", 0.08), (162.025e6, "AIS5", 0.06),
            (144.8e6, "APRS", 0.08)]

    def open(self):
        self.n0 = 0
        self.t_next = time.monotonic()
        self.rng = np.random.default_rng()
        # Morsekod "CQ" som på/av-mönster
        code = {"C": "-.-.", "Q": "--.-"}
        pat = []
        for ch in "CQ":
            for s in code[ch]:
                pat += [1] * (3 if s == "-" else 1) + [0]
            pat += [0, 0]
        pat += [0] * 7
        self.morse = np.array(pat, dtype=np.float32)
        A = avkodare
        self.bursts = {   # namn: (komplex skur, period i sekunder)
            "ACARS": (A.make_acars_burst(SR, reg=".SE-SIM", label="H1", blk="2",
                                         text="M02ASK0042TESTMEDDELANDE FRAN SIMULATORN"), 6),
            "AIS": (A.make_ais_burst(SR, A.make_ais_payload(mmsi=265999001, lat=59.3245, lon=18.0712,
                                                            sog=8.4, cog=135.0)), 1.5),
            "AIS5": (A.make_ais_burst(SR, A.make_ais5_payload(mmsi=265999001, call="SIMU", name="SIM TESTFARTYG",
                                                              ship_type=60, dest="VISBY")), 4),
            "APRS": (A.make_aprs_burst(SR, A.make_ax25_frame(src="SA0SIM-9", path=("WIDE1-1", "WIDE2-1"),
                                                              info="!5919.47N/01804.27E>Simulator-APRS")), 8),
        }
        self.burst_start = {k: int(SR * (0.5 + i * 0.37)) for i, k in enumerate(self.bursts)}
        # simulerade fartyg: (mmsi, lat, lon, fart kn, kurs, status, namn, anrop, typ, destination)
        self.ais_ships = [(265999001, 59.3245, 18.0712, 8.4, 135.0, 0, "SIM PASSAGERARE", "SIMA", 60, "VISBY"),
                          (265999002, 59.2900, 18.2600, 11.0, 60.0, 0, "SIM TANKER", "SIMB", 80, "ROTTERDAM"),
                          (265999003, 59.3550, 18.1400, 4.5, 300.0, 8, "SIM SEGLARE", "SIMC", 36, "SANDHAMN"),
                          (265999004, 59.3170, 18.0950, 0.0, 0.0, 5, "SIM FORTOJD", "SIMD", 70, "STOCKHOLM")]
        self.ais_i = 0
        self.ais_t0 = time.monotonic()
        self.low = [(99.9e6 + off, sig, amp) for off, _n, sig, amp in analys.test_signals(48000)]
        self.low.append((99.9e6, analys.make_morse(48000, "CQ CQ DE SM5SIM SM5SIM PSE K   ", 20), 0.05))
        V = analysavkodare
        silence = np.zeros(48000 * 3, np.complex64)
        self.low += [
            (99.68e6, V.make_sitor_b(48000, "ZCZC SA01\nSIMULATOR NAVTEX. NAVIGATIONAL WARNING 42. "
                                            "UNLIT BUOY 59-10N 018-40E.\nNNNN\n"), 0.05),
            (100.12e6, np.concatenate((V.make_ale(48000, [("TO", "SM5"), ("DATA", "SIM")] * 4 +
                                                  [("TIS", "HQ1")] * 2), silence,
                                       V.make_ale(48000, [("TWAS", "NET")] * 2), silence)), 0.05),
            (100.25e6, V.make_fax(48000, rows=240), 0.05),
            (100.42e6, np.concatenate((V.make_pocsag(48000, 1200, [
                (1234567, 3, "SIMULATOR: LARM BRAND KV 12", False), (200401, 0, "0701234567", True)]),
                silence)), 0.05),
            (100.62e6, np.concatenate((V.make_dmr(48000, bursts=60), silence)), 0.05),
        ]
        self._lowcache = {}
        self._take()
        log.info("Simulatorn startad")

    def read(self):
        self._take()
        self.t_next += BLOCK / SR
        d = self.t_next - time.monotonic()
        if d > 0:
            time.sleep(d)
        elif d < -0.5:
            self.t_next = time.monotonic()
        t = (self.n0 + np.arange(BLOCK)) / SR                       # absolut tid (för av/på-mönster)
        t1 = (((self.n0 + np.arange(BLOCK)) % SR) / SR).astype(np.float32)   # tid modulo 1 s (toner i hela Hz)
        w = np.float32(2 * np.pi)
        x = ((self.rng.standard_normal(BLOCK, np.float32) + 1j * self.rng.standard_normal(BLOCK, np.float32))
             * np.float32(0.006)).astype(np.complex64)

        def carrier(idx, fo, a):
            key = ("sig", idx, round(fo))
            car = self._lowcache.get(key)
            if car is None:
                car = (a * np.exp(2j * np.pi * fo * np.arange(BLOCK) / SR)).astype(np.complex64)
                self._lowcache[key] = car
            return car * np.complex64(np.exp(2j * np.pi * ((fo * self.n0 / SR) % 1.0)))
        for si, (f, kind, a) in enumerate(self.SIGS):
            fo = f - self.center
            if abs(fo) > SR / 2:
                continue
            if kind == "WFM":
                mod = -37.5 * np.cos(w * 1000 * t1) - np.float32(22500 / 440) * np.cos(w * 440 * t1)
                x += carrier(si, fo, a) * np.exp(1j * mod).astype(np.complex64)
            elif kind == "AM":
                x += carrier(si, fo, a) * (1 + np.float32(0.7) * np.sin(w * 600 * t1))
            elif kind == "CW":
                key = self.morse[(t / 0.07).astype(np.int64) % len(self.morse)]
                x += carrier(si, fo, a) * key
            elif kind == "NFM":
                on = ((t + f / 1e5) % 8.0) < 3.0
                if on.any():
                    x += carrier(si, fo, a) * on * np.exp(1j * 3 * np.sin(w * 1000 * t1)).astype(np.complex64)
            elif kind in self.bursts:
                arr, period = self.bursts[kind]
                st = self.burst_start[kind]
                new = False
                while st + len(arr) <= self.n0:          # nästa skur
                    st += int(SR * period)
                    new = True
                self.burst_start[kind] = st
                if new and kind in ("AIS", "AIS5"):      # nytt fartyg och ny position för varje skur
                    arr = self._next_ais(kind)
                    self.bursts[kind] = (arr, period)
                lo, hi = max(st, self.n0), min(st + len(arr), self.n0 + BLOCK)
                if lo < hi:
                    env = np.zeros(BLOCK, np.complex64)
                    env[lo - self.n0:hi - self.n0] = arr[lo - st:hi - st]
                    x += carrier(si, fo, a) * env
            elif kind == "USB":
                on = (t % 5.0) < 2.5
                if on.any():
                    x += carrier(si, fo, a) * on * (np.exp(1j * w * 700 * t1) +
                                                    0.7 * np.exp(1j * w * 1900 * t1)).astype(np.complex64)
        if len(self._lowcache) > 200:                   # mittfrekvensen har bytts många gånger
            self._lowcache = {k: v for k, v in self._lowcache.items() if k == "pat"}
        # analysens testsignaler (48 kHz-slingor). Interpolationsmönster och bärvåg räknas en gång
        # per mittfrekvens – blocket (65536) är en multipel av 32, så mönstret upprepas varje block.
        if "pat" not in self._lowcache:
            ar = np.arange(BLOCK)
            self._lowcache["pat"] = (ar // 32, ((ar % 32) / 32).astype(np.float32))
        bi, fr = self._lowcache["pat"]
        for k, (f, sig, a) in enumerate(self.low):
            fo = f - self.center
            if abs(fo) > SR / 2:
                continue
            key = ("low", k, round(fo))
            car = self._lowcache.get(key)
            if car is None:
                car = (a * np.exp(2j * np.pi * fo * np.arange(BLOCK) / SR)).astype(np.complex64)
                self._lowcache[key] = car
            L = len(sig)
            i = (self.n0 // 32 + bi) % L
            y = sig[i] * (1 - fr) + sig[(i + 1) % L] * fr
            ph = np.complex64(np.exp(2j * np.pi * ((fo * self.n0 / SR) % 1.0)))
            x += y * car * ph
        self.n0 += BLOCK
        return x.astype(np.complex64), self.center, None


def _sim_ship_payload(sim, kind):
    A = avkodare
    sim.ais_i += 1
    mmsi, lat, lon, sog, cog, nav, name, call, st, dest = sim.ais_ships[sim.ais_i % len(sim.ais_ships)]
    if kind == "AIS5":
        return A.make_ais_burst(SR, A.make_ais5_payload(mmsi=mmsi, call=call, name=name, ship_type=st, dest=dest))
    t = time.monotonic() - sim.ais_t0
    d = sog * 1.852 / 3600 * t / 111.0 * 20       # 20 gånger snabbare, så att rörelsen syns
    lat2 = lat + d * math.cos(math.radians(cog))
    lon2 = lon + d * math.sin(math.radians(cog)) / math.cos(math.radians(lat))
    return A.make_ais_burst(SR, A.make_ais_payload(mmsi=mmsi, lat=lat2, lon=lon2, sog=sog, cog=cog, nav=nav))


SimSource._next_ais = _sim_ship_payload


def open_iq_reader(conf):
    path = conf.get("play_file") or ""
    if not path or not os.path.exists(path):
        raise SourceError("Ingen IQ-fil vald (Mottagare → IQ-fil → Välj …)" if not path else
                          f"IQ-filen finns inte: {path}")
    extra = conf.get("play_override", {}).get(path, {})
    try:
        return iqfil.IQReader(path, rate=extra.get("rate"), center=extra.get("center"))
    except Exception as ex:
        raise SourceError(f"Kunde inte läsa IQ-filen {os.path.basename(path)}: {ex}")


def make_source(conf):
    k = conf["source"]
    if k == "rtl":
        return RtlSource()
    if k == "tcp":
        return TcpSource(conf["tcp_addr"])
    if k == "file":
        r = open_iq_reader(conf)
        if not r.rate:
            raise SourceError("Samplingstakten för IQ-filen är okänd – ange den när du väljer filen")
        sp = {0: 1.0, 1: 2.0, 2: 4.0, 3: 8.0, 4: 0.0}.get(conf.get("play_speed", 0), 1.0)
        log.info("Spelar upp %s (%s, %.0f S/s, %.1f s, mitt %s)", r.path, r.info, r.rate, r.seconds(),
                 f"{r.center_at(0) / 1e6:.6f} MHz" if r.center_at(0) else "okänd")
        return iqfil.FileSource(r, SR, BLOCK, speed=sp, loop=bool(conf.get("play_loop", False)))
    return SimSource()


# ════════════════════════════════════════════════════════════════════
# 4. DSP-MOTOR
# ════════════════════════════════════════════════════════════════════
def wav_header(nbytes=0x7FFFFFF0):
    return (b"RIFF" + struct.pack("<I", 36 + nbytes) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, AF, AF * 2, 2, 16) + b"data" + struct.pack("<I", nbytes))


class Engine:
    def __init__(self):
        self.mode = "WFM"
        self.bw = DEFAULT_BW["WFM"]
        self.offset = 0.0
        self.center = 100e6
        self.squelch = -150.0
        self.volume = 0.8
        self.source = None
        self.thread = None
        self.running = False
        self.spec_q = deque(maxlen=32)
        self.decoders = None        # avkodare.DecoderHub
        self.analyzer = None        # analys.Analyzer (signalanalysfliken)
        self.view_zoom = 1          # sätts av GUI: zoomfaktor för visningen
        self.view_center = None     # sätts av GUI: mittfrekvens för zoomad visning
        self._zkey = None
        self.level = -150.0
        self.sq_open = False
        self._sq_hang = 0
        self._sq_fresh = 0
        self.act_hist = deque(maxlen=70)   # ljudeffekt per block (dB) när squelch är öppen, ca 3 s
        self.activity = None               # spridning (std, dB): tal >> 3 dB, konstant bärvåg < 2 dB
        self.sinks = []
        self.sink_lock = threading.Lock()
        self.rec_lock = threading.Lock()
        self.rec_wav = None
        self.rec_wav_path = None
        self.rec_iq = None
        self.rec_iq_path = None
        self.auto_rec = False
        self._auto_closed_blocks = 0
        self.on_error = None       # anropas från DSP-tråden
        self.on_stopped = None
        self.on_event = None
        self.detector = Detector()
        self.avg_lin = None
        self.avg_center = None
        self.avg_count = 0
        self.avg_lock = threading.Lock()
        self.stats = {"blocks": 0, "proc_ms": 0.0, "overruns": 0}
        self._filt_key = None
        self._ph = 0.0
        self._ph_bfo = 0.0
        self.win = np.hanning(FFT_N).astype(np.float32)
        self.win_norm = float(self.win.sum() ** 2)
        self.bin_f = (np.arange(FFT_N) - FFT_N / 2) * BIN_HZ

    # ── styrning ──
    def start(self, source):
        if self.running:
            return
        self.source = source
        self.running = True
        self.thread = threading.Thread(target=self._run, name="DSP", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=6)
        self.thread = None

    def reset_squelch(self):
        """Anropas vid frekvensbyte: squelch får inte ärva läget från förra frekvensen."""
        self.sq_open = False
        self._sq_hang = 99
        self._sq_fresh = 0
        self.act_hist.clear()
        self.activity = None

    def set_center(self, c):
        if getattr(self.source, "fixed_center", False):
            return                          # uppspelning: filen styr mittfrekvensen
        c = max(1000.0, float(c))
        self.center = c
        if self.source:
            self.source.set("freq", c)

    def add_sink(self, s):
        with self.sink_lock:
            self.sinks.append(s)

    def remove_sink(self, s):
        with self.sink_lock:
            if s in self.sinks:
                self.sinks.remove(s)

    def get_avg(self):
        with self.avg_lock:
            if self.avg_lin is None:
                return None, None, 0
            return self.avg_lin.copy(), self.avg_center, self.avg_count

    # ── inspelning ──
    def start_wav(self, path):
        with self.rec_lock:
            if self.rec_wav:
                return
            w = wave.open(path, "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(AF)
            self.rec_wav, self.rec_wav_path = w, path
        log.info("Ljudinspelning startad: %s", path)

    def stop_wav(self):
        with self.rec_lock:
            w, p = self.rec_wav, self.rec_wav_path
            self.rec_wav = None
        if w:
            w.close()
            log.info("Ljudinspelning sparad: %s", p)
        return p

    def start_iq(self, writer):
        """writer: iqfil.SigMFWriter eller iqfil.WavWriter."""
        with self.rec_lock:
            if self.rec_iq:
                return
            self.rec_iq, self.rec_iq_path = writer, getattr(writer, "path", "")
        log.info("IQ-inspelning startad: %s (%d S/s)", self.rec_iq_path, SR)

    def stop_iq(self):
        with self.rec_lock:
            f, p = self.rec_iq, self.rec_iq_path
            self.rec_iq = None
        if f:
            try:
                p = f.close() or p
                log.info("IQ-inspelning sparad: %s (%.1f s)", p, f.seconds())
            except Exception:
                log.exception("Kunde inte avsluta IQ-inspelningen")
        return p

    # ── filter ──
    def _rebuild(self, mode, bw):
        self._filt_key = (mode, bw)
        cut1 = float(np.clip(bw / 2 + 10e3, 30e3, 150e3)) if mode == "WFM" else 60e3
        self.h1 = firwin(63, cut1, fs=SR)
        self.z1 = np.zeros(len(self.h1) - 1, np.complex128)
        if mode == "WFM":
            self.hw = firwin(127, 15e3, fs=FS1)
            self.zw = np.zeros(len(self.hw) - 1)
            x = math.exp(-1.0 / (FS1 * 50e-6))   # 50 µs de-emfas (Europa)
            self.bde, self.ade = [1 - x], [1, -x]
            self.zd = np.zeros(1)
            self._last = 0j
        else:
            self.h2 = firwin(127, 22e3, fs=FS1)
            self.z2 = np.zeros(len(self.h2) - 1, np.complex128)
            n = np.arange(255) - 127
            if mode in ("NFM", "AM"):
                hc = firwin(129, min(bw / 2, 23000), fs=AF).astype(np.complex128)
            else:
                half = min(bw / 2, 6000)
                if mode == "USB":
                    fc = 200 + half
                elif mode == "LSB":
                    fc = -(200 + half)
                else:
                    fc = CW_TONE
                hc = firwin(255, half, fs=AF) * np.exp(2j * np.pi * fc * n / AF)
            self.hc = hc
            self.zc = np.zeros(len(hc) - 1, np.complex128)
            hp = {"NFM": 250, "AM": 60}.get(mode, 100)
            self.bh, self.ah = butter(2, hp, "highpass", fs=AF)
            self.zh = np.zeros(max(len(self.bh), len(self.ah)) - 1)
            self._last = 0j
        self.agc_g = 1.0

    def _activity(self, a):
        """Mäter hur mycket ljudet varierar (tal/musik varierar, en ren bärvåg eller brus gör det inte)."""
        if not self.sq_open:
            return
        self.act_hist.append(10 * math.log10(float(np.mean(a * a)) + 1e-20))
        if len(self.act_hist) >= 50:
            self.activity = float(np.std(self.act_hist))

    def _agc(self, a):
        chunk = 240
        n = len(a) // chunk
        peaks = np.abs(a[:n * chunk]).reshape(n, chunk).max(axis=1)
        gains = np.empty(n)
        g = self.agc_g
        for i, pk in enumerate(peaks):
            tgt = min(0.5 / max(pk, 1e-6), 3000.0)
            g = tgt if tgt < g else g + (tgt - g) * 0.02
            gains[i] = g
        self.agc_g = g
        gs = np.repeat(gains, chunk)
        if len(gs) < len(a):
            gs = np.concatenate((gs, np.full(len(a) - len(gs), g)))
        return a * gs

    def _chan_bounds(self, mode, bw, off):
        if mode == "USB":
            return off + 200, off + 200 + bw
        if mode == "LSB":
            return off - 200 - bw, off - 200
        return off - bw / 2, off + bw / 2

    def chan_power(self, p_lin, lo, hi):
        k0 = int(np.clip(np.floor(lo / BIN_HZ + FFT_N / 2), 0, FFT_N - 1))
        k1 = int(np.clip(np.ceil(hi / BIN_HZ + FFT_N / 2), k0 + 1, FFT_N))
        return 10 * math.log10(float(p_lin[k0:k1].sum()) + 1e-20)

    # ── huvudloop ──
    def _run(self):
        src = self.source
        try:
            src.open()
            log.info("Mottagning startad (%s, inställd %.4f MHz, mitt %.4f MHz)", src.name,
                     (self.center + self.offset) / 1e6, self.center / 1e6)
            while self.running:
                r = src.read()
                if r is None:
                    continue
                t0 = time.perf_counter()
                self._process(*r)
                dt = (time.perf_counter() - t0) * 1000
                st = self.stats
                st["blocks"] += 1
                st["proc_ms"] = st["proc_ms"] * 0.95 + dt * 0.05
                if dt > BLOCK / SR * 1000:
                    st["overruns"] += 1
                    if st["overruns"] % 50 == 1:
                        log.warning("DSP hinner inte med (%.1f ms per block, max %.1f)", dt, BLOCK / SR * 1000)
        except EOFError:
            log.info("Uppspelningen nådde slutet av filen")
            if self.on_event:
                self.on_event("play_end", None)
        except SourceError as e:
            log.error("%s", e)
            if self.on_error:
                self.on_error(str(e))
        except Exception as e:
            log.exception("Fel i DSP-tråden")
            if self.on_error:
                self.on_error(f"Internt fel i signalbehandlingen: {e}")
        finally:
            try:
                src.close()
            except Exception:
                log.exception("Fel när källan stängdes")
            self.running = False
            self.stop_wav()
            self.stop_iq()
            log.info("Mottagning stoppad")
            if self.on_stopped:
                self.on_stopped()

    def _zoom_line(self, x, c):
        """Zoom-FFT: blanda ner visningsfönstret, decimera och gör FFT med finare upplösning.
        Returnerar None (ingen zoom), False (inte nog data än) eller (db, mitt, spännvidd)."""
        z = int(self.view_zoom or 1)
        vc = self.view_center
        if z <= 1 or vc is None:
            self._zkey = None
            return None
        vc = float(vc)
        key = (z, round(vc), round(c))
        if key != self._zkey:
            self._zkey = key
            self._zh = firwin(64 * z + 1, 0.45 * SR / z, fs=SR).astype(np.float32)
            self._ztail = np.zeros(len(self._zh) - 1, np.complex64)
            self._zph = 0.0
            self._zbuf = np.zeros(0, np.complex64)
        w = -2 * np.pi * (vc - c) / SR
        xm = (x * np.exp(1j * (self._zph + w * np.arange(len(x))))).astype(np.complex64)
        self._zph = (self._zph + w * len(x)) % (2 * np.pi)
        xin = np.concatenate((self._ztail, xm))
        self._ztail = xm[-(len(self._zh) - 1):]
        y = oaconvolve(xin, self._zh, mode="valid")[::z].astype(np.complex64)
        self._zbuf = np.concatenate((self._zbuf, y))[-4 * FFT_N:]
        m = len(self._zbuf) // FFT_N
        if m == 0:
            return False
        seg = self._zbuf[-m * FFT_N:].reshape(m, FFT_N) * self.win
        p = np.fft.fftshift((np.abs(np.fft.fft(seg, axis=1)) ** 2).mean(axis=0)) / self.win_norm
        return (10 * np.log10(p + 1e-20)).astype(np.float32), vc, SR / z

    def _process(self, x, c, raw):
        now = time.time()
        if getattr(self.source, "fixed_center", False) and abs(c - self.center) > 1:
            # uppspelning: filen bestämmer mittfrekvensen – behåll den inställda (absoluta) frekvensen
            self.offset += self.center - c
            self.center = c
        if self.rec_iq:
            with self.rec_lock:
                if self.rec_iq:
                    try:
                        self.rec_iq.write(x, c, raw)
                    except OSError as ex:
                        log.error("IQ-inspelningen stoppades: %s", ex)
                        f, self.rec_iq = self.rec_iq, None
                        if self.on_event:
                            self.on_event("iq_error", str(ex))

        # Spektrum (Welch, 16 segment)
        seg = x.reshape(-1, FFT_N) * self.win
        p = np.fft.fftshift((np.abs(np.fft.fft(seg, axis=1)) ** 2).mean(axis=0)) / self.win_norm
        db = (10 * np.log10(p + 1e-20)).astype(np.float32)
        self.spec_q.append((db, c, self._zoom_line(x, c)))
        if self.decoders is not None:
            self.decoders.feed(x, c)
        if self.analyzer is not None:
            try:
                self.analyzer.tap.feed(x, c)
            except Exception:
                log.exception("Fel i signalanalysens kanalutsnitt")
                self.analyzer = None
        with self.avg_lock:
            if self.avg_center != c or self.avg_lin is None:
                self.avg_lin = p.copy()
                self.avg_center = c
                self.avg_count = 1
            else:
                self.avg_lin = self.avg_lin * 0.6 + p * 0.4
                self.avg_count += 1
            avg = self.avg_lin

        mode, bw, off = self.mode, self.bw, self.offset
        off = float(np.clip(off, -SR / 2 + 5000, SR / 2 - 5000))

        # Squelch / nivå
        lo, hi = self._chan_bounds(mode, bw, off)
        self.level = self.chan_power(p, lo, hi)
        if abs(c - self.center) > 1:
            # Block från förra mittfrekvensen (hårdvaran har inte hunnit byta än)
            self.sq_open = False
            self._sq_fresh = 0
        elif self._sq_fresh < 2:
            self._sq_fresh += 1
            self.sq_open = False
        elif self.level >= self.squelch:
            self.sq_open = True
            self._sq_hang = 0
        elif self.level < self.squelch - 2:
            self._sq_hang += 1
            if self._sq_hang > 6:
                self.sq_open = False

        # Detektor (varannan block)
        if self.detector.enabled and self.stats["blocks"] % 2 == 0:
            self.detector.update(10 * np.log10(avg + 1e-20), c, now)

        # Demodulering
        if self._filt_key != (mode, bw):
            self._rebuild(mode, bw)
        n = np.arange(BLOCK)
        w = -2 * np.pi * off / SR
        y = x * np.exp(1j * (self._ph + w * n))
        self._ph = (self._ph + w * BLOCK) % (2 * np.pi)
        y, self.z1 = lfilter(self.h1, 1.0, y, zi=self.z1)
        y = y[::D1]
        if mode == "WFM":
            prev = np.concatenate(([self._last], y[:-1]))
            self._last = y[-1]
            d = np.angle(y * np.conj(prev))
            d, self.zd = lfilter(self.bde, self.ade, d, zi=self.zd)
            a, self.zw = lfilter(self.hw, 1.0, d, zi=self.zw)
            a = a[::D2] * (0.7 / (2 * np.pi * 75e3 / FS1))
        else:
            y, self.z2 = lfilter(self.h2, 1.0, y, zi=self.z2)
            y = y[::D2]
            if mode == "CW":
                m = np.arange(len(y))
                wb = 2 * np.pi * CW_TONE / AF
                y = y * np.exp(1j * (self._ph_bfo + wb * m))
                self._ph_bfo = (self._ph_bfo + wb * len(y)) % (2 * np.pi)
            y, self.zc = lfilter(self.hc, 1.0, y, zi=self.zc)
            if mode == "NFM":
                prev = np.concatenate(([self._last], y[:-1]))
                self._last = y[-1]
                a = np.angle(y * np.conj(prev)) * (0.6 / (2 * np.pi * 5000 / AF))
                a, self.zh = lfilter(self.bh, self.ah, a, zi=self.zh)
                self._activity(a)
            elif mode == "AM":
                a, self.zh = lfilter(self.bh, self.ah, np.abs(y), zi=self.zh)
                self._activity(a)
                a = self._agc(a)
            else:
                a, self.zh = lfilter(self.bh, self.ah, y.real, zi=self.zh)
                a = self._agc(a)
        if not self.sq_open:
            a = a * 0.0
        pcm = (np.clip(a * self.volume, -1, 1) * 32767).astype("<i2").tobytes()

        # Automatisk inspelning
        if self.auto_rec:
            if self.sq_open:
                self._auto_closed_blocks = 0
                if not self.rec_wav:
                    path = os.path.join(rec_dir(), f"auto_{datetime.datetime.now():%Y%m%d-%H%M%S}_"
                                                   f"{(c + off) / 1e6:.4f}MHz_{mode}.wav")
                    self.start_wav(path)
                    if self.on_event:
                        self.on_event("rec_auto_start", path)
            elif self.rec_wav and self.rec_wav_path and os.path.basename(self.rec_wav_path).startswith("auto_"):
                self._auto_closed_blocks += 1
                if self._auto_closed_blocks > 47:  # ~2 s
                    p2 = self.stop_wav()
                    if self.on_event:
                        self.on_event("rec_auto_stop", p2)

        if self.rec_wav:
            with self.rec_lock:
                if self.rec_wav:
                    self.rec_wav.writeframes(pcm)
        with self.sink_lock:
            sinks = list(self.sinks)
        for s in sinks:
            s.push(pcm)


# ════════════════════════════════════════════════════════════════════
# 5. SIGNALDETEKTOR (signalloggen)
# ════════════════════════════════════════════════════════════════════
class Detector:
    def __init__(self):
        self.enabled = True
        self.thresh = 12.0
        self.min_hits = 3
        self.lock = threading.Lock()
        self.tracks = {}
        self.active = []
        self.changed = set()
        self.next_id = 1
        self.floor = -100.0

    def update(self, db, center, now):
        N = len(db)
        floor = float(np.median(db))
        self.floor = floor
        a = db.copy()
        a[N // 2 - 3:N // 2 + 4] = floor          # DC-spik
        edge = int(N * 0.06)
        a[:edge] = floor
        a[-edge:] = floor
        mask = a > floor + self.thresh
        mask = ndimage.binary_closing(mask, structure=np.ones(5, bool))
        lab, nseg = ndimage.label(mask)
        segs = []
        if nseg:
            for sl in ndimage.find_objects(lab)[:400]:
                s = sl[0]
                k = np.arange(s.start, s.stop)
                pw = 10 ** (db[s] / 10)
                f = center + float(((k - N / 2) * BIN_HZ * pw).sum() / pw.sum())
                peak = float(db[s].max())
                segs.append((f, (s.stop - s.start) * BIN_HZ, peak))
            segs.sort(key=lambda z: -z[2])
            segs = segs[:60]
        with self.lock:
            active = [t for t in self.tracks.values() if t["status"] == "Aktiv"]
            for f, bw, peak in segs:
                best = None
                for t in active:
                    if abs(t["f"] - f) < max(t["bw"], bw) / 2 + 2000:
                        best = t
                        break
                if best:
                    best["f"] = best["f"] * 0.8 + f * 0.2
                    best["bw"] = max(best["bw"] * 0.9, bw)
                    best["peak"] = max(best["peak"], peak)
                    best["snr"] = max(best["snr"], peak - floor)
                    best["last"] = now
                    best["hits"] += 1
                    best["dur"] = now - best["first"]
                    if best["hits"] >= self.min_hits:
                        self.changed.add(best["id"])
                else:
                    t = {"id": self.next_id, "first": now, "last": now, "f": f, "bw": bw, "peak": peak,
                         "snr": peak - floor, "hits": 1, "dur": 0.0, "status": "Aktiv"}
                    self.next_id += 1
                    self.tracks[t["id"]] = t
                    active.append(t)
            for t in active:
                if now - t["last"] > 1.5:
                    t["status"] = "Avslutad"
                    t["dur"] = t["last"] - t["first"]
                    if t["hits"] >= self.min_hits:
                        self.changed.add(t["id"])
                    else:
                        self.tracks.pop(t["id"], None)
            self.active = [dict(t) for t in self.tracks.values()
                           if t["status"] == "Aktiv" and t["hits"] >= self.min_hits]
            if len(self.tracks) > 3000:
                for tid in sorted(self.tracks)[:1000]:
                    if self.tracks[tid]["status"] != "Aktiv":
                        self.tracks.pop(tid, None)

    def pop_changes(self):
        with self.lock:
            out = [dict(self.tracks[i]) for i in self.changed if i in self.tracks]
            self.changed.clear()
            act = list(self.active)
        for t in out:
            t["cls"] = avkodare.identify(t["f"], t["bw"]) or classify(t["bw"])
            t["band"] = band_name(t["f"])
        return out, act

    def clear(self):
        with self.lock:
            self.tracks.clear()
            self.changed.clear()
            self.active = []


# ════════════════════════════════════════════════════════════════════
# 6. LJUDUTGÅNG OCH WEBBGRÄNSSNITT
# ════════════════════════════════════════════════════════════════════
class AudioOut:
    """Skickar ljud till PipeWire via pacat (eller pw-cat)."""

    def __init__(self):
        self.q = queue.Queue(maxsize=12)
        self.proc = None
        self.alive = False
        self.thread = None

    def _spawn(self):
        cmds = [["pacat", "--playback", "--raw", "--format=s16le", "--rate=48000", "--channels=1",
                 "--latency-msec=150", "--client-name=Radiospanaren", "--stream-name=Mottagare"],
                ["pw-cat", "--playback", "--format=s16", "--rate=48000", "--channels=1", "-"]]
        for c in cmds:
            if shutil.which(c[0]):
                try:
                    err = open(STDERR_FILE, "ab")
                    self.proc = subprocess.Popen(c, stdin=subprocess.PIPE, stderr=err)
                    log.info("Ljud via %s", c[0])
                    return True
                except OSError as e:
                    log.error("Kunde inte starta %s: %s", c[0], e)
        log.error("Hittar varken pacat eller pw-cat – inget ljud. Installera: sudo dnf install pulseaudio-utils")
        return False

    def start(self):
        if self.alive:
            return
        if not self._spawn():
            return
        self.alive = True
        self.thread = threading.Thread(target=self._run, name="Ljud", daemon=True)
        self.thread.start()

    def push(self, b):
        if not self.alive:
            return
        try:
            self.q.put_nowait(b)
        except queue.Full:
            try:
                self.q.get_nowait()
                self.q.put_nowait(b)
            except (queue.Empty, queue.Full):
                pass

    def _run(self):
        restarts = 0
        while self.alive:
            try:
                b = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.proc.stdin.write(b)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, AttributeError) as e:
                log.error("Ljudutgången avbröts (%s)", e)
                restarts += 1
                if restarts > 3 or not self._spawn():
                    log.error("Ger upp ljudutgången efter flera försök")
                    self.alive = False
                    return

    def stop(self):
        self.alive = False
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None


class QueueSink:
    def __init__(self):
        self.q = queue.Queue(maxsize=60)

    def push(self, b):
        try:
            self.q.put_nowait(b)
        except queue.Full:
            pass


WEB_HTML = r"""<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Radiospanaren – webb</title>
<style>
body{margin:0;background:#111418;color:#dde3ea;font:14px system-ui,sans-serif}
#top{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 12px;background:#1b2027}
#f{font:600 22px ui-monospace,monospace;color:#7fe0d0;min-width:13ch}
canvas{display:block;width:100%;cursor:crosshair}
button{background:#2a313b;color:#dde3ea;border:1px solid #3a4350;border-radius:6px;padding:5px 10px;cursor:pointer}
button.on{background:#2f6fb0;border-color:#4a8ad0}
input{background:#0d1014;color:#dde3ea;border:1px solid #3a4350;border-radius:6px;padding:5px;width:130px}
#lvl{font-family:ui-monospace,monospace;color:#9ab}
</style></head><body>
<div id="top"><span id="f">–</span><input id="fi" placeholder="MHz, t.ex. 145.5">
<span id="modes"></span><button id="play">▶ Lyssna</button><span id="lvl"></span></div>
<canvas id="sp" height="160"></canvas><canvas id="wf" height="420"></canvas><audio id="au"></audio>
<script>
const M=["WFM","NFM","AM","USB","LSB","CW"];let st=null;
const sp=document.getElementById('sp'),wf=document.getElementById('wf'),sc=sp.getContext('2d'),wc=wf.getContext('2d');
const row=document.createElement('canvas');row.width=1024;row.height=1;const rc=row.getContext('2d');const img=rc.createImageData(1024,1);
const stops=[[0,[0,0,10]],[.15,[0,0,90]],[.35,[0,90,220]],[.55,[0,220,200]],[.7,[240,230,0]],[.85,[255,60,0]],[1,[255,255,255]]];
const cmap=[];for(let i=0;i<256;i++){const x=i/255;let j=0;while(j<stops.length-2&&x>stops[j+1][0])j++;
const a=stops[j],b=stops[j+1],t=(x-a[0])/(b[0]-a[0]);cmap.push(a[1].map((v,k)=>Math.round(v+(b[1][k]-v)*t)));}
function fit(){for(const c of [sp,wf]){if(c.width!==c.clientWidth)c.width=c.clientWidth;}}
window.addEventListener('resize',fit);fit();
const md=document.getElementById('modes');
for(const m of M){const b=document.createElement('button');b.textContent=m;b.onclick=()=>tune(st.freq,m);b.id='m'+m;md.appendChild(b);}
function tune(f,m){fetch('/tune?f='+Math.round(f)+(m?'&mode='+m:''));}
function click(e){if(!st)return;const r=e.target.getBoundingClientRect();
let f=st.center-st.sr/2+(e.clientX-r.left)/r.width*st.sr;f=Math.round(f/st.step)*st.step;tune(f);}
sp.onclick=click;wf.onclick=click;
document.getElementById('fi').onkeydown=e=>{if(e.key==='Enter'){const v=parseFloat(e.target.value.replace(',','.'));if(v>0)tune(v*1e6);}};
document.getElementById('play').onclick=()=>{const a=document.getElementById('au');
if(a.paused){a.src='/audio.wav?'+Date.now();a.play();play.textContent='■ Stoppa';}else{a.pause();a.src='';play.textContent='▶ Lyssna';}};
function draw(){const W=sp.width,H=sp.height,L=st.line;sc.fillStyle='#0b0e12';sc.fillRect(0,0,W,H);
sc.strokeStyle='#222a33';for(let i=1;i<6;i++){sc.beginPath();sc.moveTo(0,i*H/6);sc.lineTo(W,i*H/6);sc.stroke();}
sc.beginPath();sc.moveTo(0,H);for(let i=0;i<L.length;i++){sc.lineTo(i/L.length*W,H-L[i]/255*H);}sc.lineTo(W,H);
sc.fillStyle='rgba(80,180,255,.25)';sc.fill();sc.strokeStyle='#7cc8ff';sc.stroke();
const x0=(st.freq+st.lo-(st.center-st.sr/2))/st.sr*W,x1=(st.freq+st.hi-(st.center-st.sr/2))/st.sr*W;
sc.fillStyle='rgba(255,255,255,.12)';sc.fillRect(x0,0,Math.max(1,x1-x0),H);
const xf=(st.freq-(st.center-st.sr/2))/st.sr*W;sc.fillStyle='#ff4a4a';sc.fillRect(xf-1,0,2,H);
for(let i=0;i<1024;i++){const c=cmap[L[i]];img.data[i*4]=c[0];img.data[i*4+1]=c[1];img.data[i*4+2]=c[2];img.data[i*4+3]=255;}
rc.putImageData(img,0,0);wc.drawImage(wf,0,0,wf.width,wf.height-1,0,1,wf.width,wf.height-1);wc.drawImage(row,0,0,wf.width,1);
document.getElementById('f').textContent=(st.freq/1e6).toFixed(4)+' MHz';
document.getElementById('lvl').textContent=st.mode+'  '+st.level.toFixed(1)+' dB '+(st.sq?'● signal':'');
for(const m of M)document.getElementById('m'+m).className=(m===st.mode?'on':'');}
async function loop(){try{const r=await fetch('/status.json');st=await r.json();if(st.line)draw();}catch(e){}setTimeout(loop,100);}
loop();
</script></body></html>"""


class WebServer:
    """Enkel KiwiSDR-liknande webbmottagare: vattenfall, klick för att ställa in, ljudström."""

    def __init__(self, app, port, public):
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
        from urllib.parse import urlparse, parse_qs
        self.app = app
        host = "0.0.0.0" if public else "127.0.0.1"
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("webb: " + fmt, *args)

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                try:
                    if u.path == "/":
                        self._send(200, "text/html; charset=utf-8", WEB_HTML.encode())
                    elif u.path == "/status.json":
                        self._send(200, "application/json", json.dumps(srv.status()).encode())
                    elif u.path == "/tune":
                        q = parse_qs(u.query)
                        f = float(q.get("f", ["0"])[0])
                        m = q.get("mode", [None])[0]
                        if m not in MODES:
                            m = None
                        if 1e4 < f < 6e9:
                            srv.app.remote_tune(f, m)
                        self._send(200, "text/plain", b"ok")
                    elif u.path == "/audio.wav":
                        self._audio()
                    else:
                        self._send(404, "text/plain", b"finns inte")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    log.exception("Fel i webbgränssnittet")

            def _audio(self):
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(wav_header())
                sink = QueueSink()
                srv.app.engine.add_sink(sink)
                log.info("Webblyssnare ansluten: %s", self.client_address[0])
                try:
                    while True:
                        self.wfile.write(sink.q.get(timeout=5))
                except (BrokenPipeError, ConnectionResetError, queue.Empty, OSError):
                    pass
                finally:
                    srv.app.engine.remove_sink(sink)
                    log.info("Webblyssnare frånkopplad: %s", self.client_address[0])

        try:
            self.httpd = ThreadingHTTPServer((host, port), H)
        except OSError as e:
            raise RuntimeError(f"Kunde inte starta webbservern på port {port}: {e}")
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="Webb", daemon=True)
        self.thread.start()
        self.url = f"http://{'<datorns-ip>' if public else '127.0.0.1'}:{port}/"
        log.info("Webbgränssnitt startat på %s:%d", host, port)

    def status(self):
        app = self.app
        e = app.engine
        line = None
        db = app.latest_db
        if db is not None:
            v = db.reshape(1024, -1).max(axis=1)
            line = np.clip((v - app.conf["db_min"]) / max(1, app.conf["db_max"] - app.conf["db_min"]) * 255,
                           0, 255).astype(int).tolist()
        lo, hi = e._chan_bounds(e.mode, e.bw, 0)
        return {"center": app.latest_c or app.engine.center, "sr": SR, "freq": app.freq, "mode": e.mode, "bw": e.bw,
                "lo": lo, "hi": hi, "level": e.level, "sq": e.sq_open, "step": app.step(), "line": line}

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        log.info("Webbgränssnitt stoppat")


# ════════════════════════════════════════════════════════════════════
# 7. GRAFISKT GRÄNSSNITT
# ════════════════════════════════════════════════════════════════════
def on_unix_signals(cb):
    """Anropar cb vid SIGTERM/SIGINT i GTK:s huvudloop (GLibUnix i ny PyGObject, annars GLib)."""
    import signal
    from gi.repository import GLib
    add = None
    try:
        import gi
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        add = getattr(GLibUnix, "signal_add", None)       # GLib 2.84+ (t.ex. Fedora 42+)
    except (ImportError, ValueError):
        pass
    if add is None:
        add = GLib.unix_signal_add                          # äldre GLib (Ubuntu 24.04, Mint 22)
    for sig in (signal.SIGTERM, signal.SIGINT):
        add(GLib.PRIORITY_DEFAULT, sig, lambda *_: cb() or False)


MIN_GTK = (4, 12)
MIN_ADW = (1, 5)


def run_gui(args):
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw, Gdk, GLib, Gio, GObject, Pango
        import cairo
    except (ImportError, ValueError) as e:
        msg = (f"Saknar GTK4/libadwaita för Python ({e}).\n"
               "Fedora:          sudo dnf install python3-gobject python3-cairo gtk4 libadwaita\n"
               "Ubuntu/Mint:     sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1")
        log.critical(msg)
        print(msg, file=sys.stderr)
        return 1
    gtk_v = (Gtk.get_major_version(), Gtk.get_minor_version())
    adw_v = (Adw.get_major_version(), Adw.get_minor_version())
    if gtk_v < MIN_GTK or adw_v < MIN_ADW:
        msg = (f"För gamla versioner: GTK {gtk_v[0]}.{gtk_v[1]} och libadwaita {adw_v[0]}.{adw_v[1]}. "
               f"Radiospanaren kräver GTK {MIN_GTK[0]}.{MIN_GTK[1]}+ och libadwaita {MIN_ADW[0]}.{MIN_ADW[1]}+ "
               "(t.ex. Fedora 40+, Ubuntu 24.04+, Linux Mint 22+).")
        log.critical(msg)
        print(msg, file=sys.stderr)
        return 1

    class Row(GObject.Object):
        __gsignals__ = {"updated": (GObject.SignalFlags.RUN_FIRST, None, ())}

        def __init__(self, data):
            super().__init__()
            self.data = data

    def _bind(f, li, fn):
        lbl = li.get_child()
        row = li.get_item()
        lbl.set_text(fn(row.data))
        lbl._hid = (row, row.connect("updated", lambda r: lbl.set_text(fn(r.data))))

    def _unbind(f, li):
        lbl = li.get_child()
        h = getattr(lbl, "_hid", None)
        if h:
            h[0].disconnect(h[1])
            lbl._hid = None

    def make_table(columns, filter_func=None):
        """columns: [(rubrik, visningsfunktion, sorteringsnyckel, expandera)]"""
        store = Gio.ListStore(item_type=Row)
        cv = Gtk.ColumnView()
        cv.add_css_class("data-table")
        cv.set_show_row_separators(True)
        cols = []
        for title, fn, key, expand in columns:
            fac = Gtk.SignalListItemFactory()
            fac.connect("setup", lambda f, li: li.set_child(Gtk.Label(xalign=0)))
            fac.connect("bind", _bind, fn)
            fac.connect("unbind", _unbind)
            col = Gtk.ColumnViewColumn(title=title, factory=fac)
            col.set_resizable(True)
            col.set_expand(expand)

            def cmp(a, b, *_, key=key):
                va, vb = a.data.get(key), b.data.get(key)
                if va == vb:
                    return Gtk.Ordering.EQUAL
                try:
                    return Gtk.Ordering.SMALLER if va < vb else Gtk.Ordering.LARGER
                except TypeError:
                    return Gtk.Ordering.EQUAL
            col.set_sorter(Gtk.CustomSorter.new(cmp, None))
            cv.append_column(col)
            cols.append(col)
        cfilter = Gtk.CustomFilter.new(filter_func, None) if filter_func else None
        model = Gtk.FilterListModel(model=store, filter=cfilter) if cfilter else store
        sortm = Gtk.SortListModel(model=model, sorter=cv.get_sorter())
        sel = Gtk.SingleSelection(model=sortm)
        cv.set_model(sel)
        sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        sw.set_child(cv)
        return store, cv, sel, cfilter, cols, sw

    # ── Spektrum + vattenfall ──
    class SpectrumView(Gtk.DrawingArea):
        WF_ROWS = 600

        def __init__(self, win):
            super().__init__(hexpand=True, vexpand=True)
            self.win = win
            self.wf = np.zeros((self.WF_ROWS, FFT_N), np.uint8)
            self.wf_span = float(SR)
            self.disp = None
            self.mouse = None
            self.lut = make_lut(win.conf["colormap"])
            self.set_draw_func(self.on_draw)
            g = Gtk.GestureClick(button=1)
            g.connect("pressed", self.on_click)
            self.add_controller(g)
            sc = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            sc.connect("scroll", self.on_scroll)
            self.add_controller(sc)
            mo = Gtk.EventControllerMotion()
            mo.connect("motion", lambda c, x, y: self._set_mouse((x, y)))
            mo.connect("leave", lambda c: self._set_mouse(None))
            self.add_controller(mo)
            dr = Gtk.GestureDrag(button=3)
            dr.connect("drag-begin", self.on_drag_begin)
            dr.connect("drag-update", self.on_drag_update)
            self.add_controller(dr)
            self._drag_c = None

        def _set_mouse(self, m):
            self.mouse = m
            self.queue_draw()

        def push_line(self, db, c, span, to_waterfall=True):
            w = self.win
            if span != self.wf_span:
                self.wf[:] = 0
                self.wf_span = span
                w.wf_center = c
                self.disp = None
            if w.wf_center != c:
                delta = int(round((c - w.wf_center) / (self.wf_span / FFT_N)))
                if abs(delta) >= FFT_N:
                    self.wf[:] = 0
                elif delta > 0:
                    self.wf[:, :-delta] = self.wf[:, delta:]
                    self.wf[:, -delta:] = 0
                elif delta < 0:
                    self.wf[:, -delta:] = self.wf[:, :delta]
                    self.wf[:, :-delta] = 0
                w.wf_center = c
                self.disp = None
            lo, hi = w.conf["db_min"], w.conf["db_max"]
            q = np.clip((db - lo) * (255.0 / max(1.0, hi - lo)), 0, 255).astype(np.uint8)
            if to_waterfall:
                self.wf[1:] = self.wf[:-1]
                self.wf[0] = q
            self.disp = db.copy() if self.disp is None else self.disp * 0.5 + db * 0.5

        def view(self):
            """Visat frekvensområde = området som datan i vattenfallet täcker."""
            return self.win.wf_center - self.wf_span / 2, self.wf_span

        def requested_view(self):
            """Mittfrekvens som DSP-motorn ska zooma kring (följer inställd frekvens)."""
            w = self.win
            z = max(1, w.conf["zoom"])
            c = w.engine.center
            if z <= 1:
                return 1, None
            span = SR / z
            return z, float(np.clip(w.freq, c - SR / 2 + span / 2, c + SR / 2 - span / 2))

        def x_to_f(self, x):
            lo, span = self.view()
            return lo + x / max(1, self.get_width()) * span

        def layout(self, H):
            band_h, axis_h = 18, 18
            spec_h = int((H - band_h - axis_h) * self.win.conf["spec_frac"])
            return band_h, spec_h, axis_h, band_h + spec_h + axis_h

        def on_click(self, g, n, x, y):
            w = self.win
            st = w.step()
            fc = self.x_to_f(x)
            shift = bool(g.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
            w.tune(round(fc / st) * st)
            if shift:              # Skift + klick: bara ställ in, ingen analys
                return
            hit = w.carrier_at(fc)
            if hit:
                w.analyse(hit[0], hit[1], show=True)
            elif w.analys_shown():
                e = w.engine
                lo_, hi_ = e._chan_bounds(e.mode, e.bw, w.freq)
                w.analyse((lo_ + hi_) / 2, hi_ - lo_, show=False)

        def on_scroll(self, ctrl, dx, dy):
            state = ctrl.get_current_event_state()
            if state & Gdk.ModifierType.CONTROL_MASK:
                z = self.win.conf["zoom"]
                z = min(32, z * 2) if dy < 0 else max(1, z // 2)
                self.win.set_zoom(z)
            else:
                st = self.win.step()
                self.win.tune(round((self.win.freq - dy * st) / st) * st)
            return True

        def on_drag_begin(self, g, x, y):
            self._drag_c = self.win.engine.center

        def on_drag_update(self, g, dx, dy):
            if self._drag_c is None:
                return
            lo, span = self.view()
            newc = self._drag_c - dx / max(1, self.get_width()) * span
            if abs(newc - self.win.engine.center) > BIN_HZ * 20:
                self.win.set_center_keep(newc)

        def on_draw(self, area, cr, W, H, *_):
            w = self.win
            conf = w.conf
            S = w.skin()
            cr.set_source_rgb(*S["bg"][:3])
            cr.paint()
            if W < 10 or H < 60:
                return
            band_h, spec_h, axis_h, wf_y = self.layout(H)
            wf_h = H - wf_y
            lo, span = self.view()
            c = w.wf_center
            xs = np.arange(W + 1)
            fpx = lo + xs * span / W
            binhz = self.wf_span / FFT_N
            kf = (fpx - c) / binhz + FFT_N / 2
            k = np.clip(np.floor(kf).astype(np.int64), 0, FFT_N - 1)
            starts = k[:-1]
            outside = (fpx[:-1] < c - self.wf_span / 2) | (fpx[:-1] > c + self.wf_span / 2)

            # Vattenfall
            rows = int(min(self.WF_ROWS, max(1, wf_h)))
            img = np.maximum.reduceat(self.wf[:rows], starts, axis=1)
            img[:, outside] = 0
            rgba = np.ascontiguousarray(self.lut[img])
            surf = cairo.ImageSurface.create_for_data(memoryview(rgba), cairo.FORMAT_ARGB32, W, rows, W * 4)
            cr.save()
            cr.rectangle(0, wf_y, W, wf_h)
            cr.clip()
            cr.translate(0, wf_y)
            cr.scale(1, wf_h / rows)
            cr.set_source_surface(surf, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
            cr.restore()

            # Spektrumbakgrund och dB-rutnät
            sy = band_h
            cr.set_source_rgb(*S["spec_bg"][:3])
            cr.rectangle(0, sy, W, spec_h)
            cr.fill()
            dbmin, dbmax = conf["db_min"], conf["db_max"]
            rng = max(1.0, dbmax - dbmin)
            cr.set_font_size(10)
            cr.set_line_width(1)
            step_db = 10 if rng > 30 else 5
            d = math.ceil(dbmin / step_db) * step_db
            while d < dbmax:
                yy = sy + (dbmax - d) / rng * spec_h
                cr.set_source_rgba(*S["grid"])
                cr.move_to(0, int(yy) + 0.5)
                cr.line_to(W, int(yy) + 0.5)
                cr.stroke()
                cr.set_source_rgba(*S["text"])
                cr.move_to(3, yy - 2)
                cr.show_text(f"{d:.0f}")
                d += step_db

            # Frekvensaxel
            ay = sy + spec_h
            cr.set_source_rgb(*S["axis_bg"][:3])
            cr.rectangle(0, ay, W, axis_h)
            cr.fill()
            raw = span / 8
            mag = 10 ** math.floor(math.log10(raw))
            tick = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
            dec = max(0, min(6, -int(math.floor(math.log10(tick / 1e6))) if tick < 1e6 else 0))
            f0 = math.ceil(lo / tick) * tick
            ff = f0
            while ff < lo + span:
                x = (ff - lo) / span * W
                cr.set_source_rgba(*S["grid"])
                cr.move_to(int(x) + 0.5, sy)
                cr.line_to(int(x) + 0.5, sy + spec_h)
                cr.stroke()
                cr.set_source_rgba(*S["text"][:3], 0.75)
                cr.move_to(int(x) + 0.5, ay)
                cr.line_to(int(x) + 0.5, ay + 4)
                cr.stroke()
                txt = f"{ff / 1e6:.{dec}f}"
                ext = cr.text_extents(txt)
                cr.move_to(x - ext.width / 2, ay + 14)
                cr.show_text(txt)
                ff += tick

            # Spektrumkurva
            if self.disp is not None:
                v = np.maximum.reduceat(self.disp, starts)
                yv = sy + np.clip((dbmax - v) / rng, 0, 1) * spec_h
                cr.move_to(0, sy + spec_h)
                for i in range(W):
                    if not outside[i]:
                        cr.line_to(i, yv[i])
                cr.line_to(W, sy + spec_h)
                cr.close_path()
                grad = cairo.LinearGradient(0, sy, 0, sy + spec_h)
                grad.add_color_stop_rgba(0, *S["fill_top"])
                grad.add_color_stop_rgba(1, *S["fill_bot"])
                cr.set_source(grad)
                cr.fill_preserve()
                cr.set_source_rgb(*S["line"][:3])
                cr.set_line_width(1.2)
                cr.stroke()

            # Bandplan
            cr.set_source_rgb(*S["band_bg"][:3])
            cr.rectangle(0, 0, W, band_h)
            cr.fill()
            for blo, bhi, name in BANDPLAN:
                x0 = (blo * 1e6 - lo) / span * W
                x1 = (bhi * 1e6 - lo) / span * W
                if x1 < 0 or x0 > W:
                    continue
                hsh = (sum(map(ord, name)) * 47) % 360 / 360.0
                r, g, b = _hsv(hsh, 0.45, 0.55)
                cr.set_source_rgba(r, g, b, 0.85)
                cr.rectangle(max(0, x0), 2, min(W, x1) - max(0, x0), band_h - 4)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                ext = cr.text_extents(name)
                if min(W, x1) - max(0, x0) > ext.width + 6:
                    cr.move_to(max(0, x0) + (min(W, x1) - max(0, x0) - ext.width) / 2, band_h - 5)
                    cr.show_text(name)

            # Aktiva signaler från detektorn
            for t in w.active_tracks:
                x0 = (t["f"] - t["bw"] / 2 - lo) / span * W
                x1 = (t["f"] + t["bw"] / 2 - lo) / span * W
                if x1 < 0 or x0 > W:
                    continue
                cr.set_source_rgba(*S["track"])
                cr.set_line_width(2)
                cr.move_to(x0, sy + 3)
                cr.line_to(max(x1, x0 + 2), sy + 3)
                cr.stroke()

            # Bokmärken
            cr.set_font_size(10)
            for i, bm in enumerate(conf["bookmarks"]):
                x = (bm["freq"] - lo) / span * W
                if 0 <= x <= W:
                    yy = sy + 16 + (i % 3) * 12
                    cr.set_source_rgba(*S["bm"])
                    cr.move_to(x, sy + 6)
                    cr.line_to(x - 3, sy + 1)
                    cr.line_to(x + 3, sy + 1)
                    cr.close_path()
                    cr.fill()
                    cr.move_to(x + 4, yy)
                    cr.show_text(bm["name"])

            # Inställd kanal
            e = w.engine
            clo, chi = e._chan_bounds(e.mode, e.bw, 0)
            x0 = (w.freq + clo - lo) / span * W
            x1 = (w.freq + chi - lo) / span * W
            cr.set_source_rgba(*S["chan"])
            cr.rectangle(x0, sy, max(2, x1 - x0), spec_h)
            cr.fill()
            cr.set_source_rgba(*S["chan"][:3], S["chan"][3] * 0.55)
            cr.rectangle(x0, wf_y, max(2, x1 - x0), wf_h)
            cr.fill()
            xf = (w.freq - lo) / span * W
            cr.set_source_rgb(*S["tuned"][:3])
            cr.set_line_width(1.5)
            cr.move_to(xf, sy)
            cr.line_to(xf, H)
            cr.stroke()

            # Analysmål (signalanalysfliken)
            ta = w.analyzer.tap.target if w.analyzer else None
            if ta is not None:
                bwa = w.analyzer.tap.bw or 1000
                xa0 = (ta - bwa / 2 - lo) / span * W
                xa1 = (ta + bwa / 2 - lo) / span * W
                if xa1 >= 0 and xa0 <= W:
                    if xa1 - xa0 < 8:
                        xa0, xa1 = (xa0 + xa1) / 2 - 4, (xa0 + xa1) / 2 + 4
                    yb = sy + spec_h - 2
                    cr.set_source_rgba(*S["analys"][:3], 0.95)
                    cr.set_line_width(2)
                    cr.move_to(xa0, yb - 8)
                    cr.line_to(xa0, yb)
                    cr.line_to(xa1, yb)
                    cr.line_to(xa1, yb - 8)
                    cr.stroke()
                    cr.set_source_rgba(*S["analys"][:3], 0.10)
                    cr.rectangle(xa0, wf_y, xa1 - xa0, wf_h)
                    cr.fill()
                    cr.set_font_size(10)
                    cr.set_source_rgba(*S["analys"][:3], 1)
                    cr.move_to(xa1 + 3, yb - 2)
                    cr.show_text("analys")

            # Muspekare
            if self.mouse:
                mx, my = self.mouse
                fm = lo + mx / W * span
                txt = f"{fm / 1e6:.4f} MHz"
                if self.disp is not None and sy <= my <= sy + spec_h:
                    kk = int(np.clip((fm - c) / binhz + FFT_N / 2, 0, FFT_N - 1))
                    txt += f"   {self.disp[kk]:.1f} dB"
                b = band_name(fm)
                if b:
                    txt += f"   {b}"
                cr.set_font_size(12)
                ext = cr.text_extents(txt)
                cr.set_source_rgba(0, 0, 0, 0.7)
                cr.rectangle(W - ext.width - 14, sy + 4, ext.width + 10, 18)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(W - ext.width - 9, sy + 17)
                cr.show_text(txt)
                cr.set_source_rgba(1, 1, 1, 0.35)
                cr.set_line_width(1)
                cr.move_to(int(mx) + 0.5, sy)
                cr.line_to(int(mx) + 0.5, H)
                cr.stroke()

            if not e.running:
                cr.set_font_size(16)
                msg = "Tryck på ▶ Starta för att börja ta emot"
                ext = cr.text_extents(msg)
                cr.set_source_rgba(1, 1, 1, 0.6)
                cr.move_to((W - ext.width) / 2, wf_y + wf_h / 2)
                cr.show_text(msg)

    def _hsv(h, s, v):
        import colorsys
        return colorsys.hsv_to_rgb(h, s, v)

    # ── Skanner ──
    class Scanner:
        def __init__(self, win):
            self.win = win
            self.active = False
            self.state = "idle"
            self.status = ""
            self.force_skip = False

        def receivable(self, f):
            """Direktsampling tar bara emot under 28,8 MHz; tunern (R820T m.fl.) bara över ca 24 MHz."""
            return f < 28.8e6 if self.win.conf.get("direct") else f >= 24e6

        def locked(self, f):
            e = self.win.engine
            lo, hi = e._chan_bounds(e.mode, e.bw, f)
            return any(lo - 500 <= x <= hi + 500 for x in self.win.conf.get("lockout", []) + self.win.suggested)

        def start(self):
            w = self.win
            conf = w.conf
            if not w.engine.running:
                w.toast("Starta mottagningen först")
                return False
            self.mode = conf["scan_mode"]
            self.hold = float(conf["scan_hold"])
            self.max_listen = float(conf.get("scan_max_listen", 0) or 0)
            self.dwell = conf["scan_dwell_ms"] / 1000.0
            self.stops = 0
            self.t0 = time.monotonic()
            self.skip_carriers = conf.get("scan_skip_carriers", True)
            self.force_skip = False
            if self.mode == 0:
                self.items = [b for b in conf["bookmarks"]
                              if not self.locked(b["freq"]) and self.receivable(b["freq"])]
                if not self.items:
                    w.toast("Inga bokmärken går att ta emot i nuvarande läge"
                            + (" (direktsampling = bara under 28,8 MHz)" if conf.get("direct") else ""))
                    return False
                self.idx = -1
                self.active = True
                self._next_bm(time.monotonic())
                desc = f"bokmärken ({len(self.items)} st)"
            else:
                try:
                    a = parse_freq(conf["scan_start"])
                    b = parse_freq(conf["scan_stop"])
                except ValueError as e:
                    w.toast(str(e))
                    return False
                if b <= a:
                    w.toast("Stoppfrekvensen måste vara högre än startfrekvensen")
                    return False
                self.a, self.b = a, b
                self.chstep = max(100.0, float(conf["scan_step_khz"]) * 1000)
                usable = SR * 0.8
                self.spans = []
                cc = a + usable / 2 + self.chstep / 2
                while cc - usable / 2 < b:
                    self.spans.append(cc)
                    cc += usable
                self.span_idx = -1
                self.active = True
                self._next_span(time.monotonic())
                desc = (f"intervall {fmt_mhz(a)}–{fmt_mhz(b)} MHz, steg {fmt_bw(self.chstep)}, "
                        f"{len(self.spans)} fönster")
            if w.engine.squelch <= -140:
                w.toast("Tips: höj squelch så att bruset inte stoppar skanningen")
            log.info("Skanning startad: %s, %s, squelch %.0f dB, väntetid %.1f s",
                     desc, w.engine.mode, w.engine.squelch, self.hold)
            return True

        def stop(self):
            if self.active:
                log.info("Skanning stoppad efter %.0f s, %d stopp på signaler",
                         time.monotonic() - self.t0, self.stops)
            self.active = False
            self.state = "idle"
            self.status = ""

        def skip(self):
            """Spärra aktuell frekvens och gå vidare."""
            f = self.win.freq
            lst = self.win.conf.setdefault("lockout", [])
            lst.append(f)
            log.info("Skanner: spärrade %.4f MHz (%d spärrade totalt)", f / 1e6, len(lst))
            self.win.toast(f"{fmt_mhz(f)} MHz spärrad – skannern hoppar över den")
            if self.mode == 0:
                self.items = [b for b in self.items if not self.locked(b["freq"])] or self.items
            self.force_skip = True

        def _listen(self, now, settle):
            self.state = "listen"
            self.settle = now + settle
            self.hold_until = self.settle
            self.heard = False

        def _next_bm(self, now):
            self.idx = (self.idx + 1) % len(self.items)
            bm = self.items[self.idx]
            self.win.tune(bm["freq"], bm.get("mode"))
            self._listen(now, self.dwell)
            self.status = f"Skannar: {bm['name']}"

        def _next_span(self, now):
            self.span_idx = (self.span_idx + 1) % len(self.spans)
            c = self.spans[self.span_idx]
            self.win.engine.set_center(c)
            self.span_c = c
            self.visited = set()
            self.state = "span"
            self.settle = now + self.dwell
            self.status = f"Skannar {fmt_mhz(max(self.a, c - SR * 0.4))}–{fmt_mhz(min(self.b, c + SR * 0.4))} MHz"

        def _leave(self, now):
            if self.heard:
                log.info("Skanner: lämnar %.4f MHz efter %.1f s", self.win.freq / 1e6, now - self.heard_t)
            if self.mode == 0:
                self._next_bm(now)
            else:
                self.state = "span"      # fortsätt med nästa signal i samma fönster
                self.settle = now + 0.05

        def tick(self):
            if not self.active:
                return
            now = time.monotonic()
            e = self.win.engine
            if self.force_skip:
                self.force_skip = False
                if self.state == "listen":
                    self._leave(now)
                return
            if now < self.settle:
                return
            if self.state == "listen":
                if e.sq_open:
                    if not self.heard:
                        self.heard = True
                        self.heard_t = now
                        self.stops += 1
                        log.info("Skanner: signal på %.4f MHz (%s), nivå %.1f dB", self.win.freq / 1e6,
                                 e.mode, e.level)
                        self.status = "Lyssnar: " + fmt_mhz(self.win.freq) + " MHz"
                    if (self.skip_carriers and e.mode in ("AM", "NFM") and e.activity is not None
                            and e.activity < 3.0 and now - self.heard_t > 2.5):
                        f = self.win.freq
                        log.info("Skanner: %.4f MHz är en omodulerad bärvåg (ljudvariation %.1f dB) – "
                                 "troligen störning, hoppar över", f / 1e6, e.activity)
                        if all(abs(f - x) > 500 for x in self.win.suggested):
                            self.win.suggested.append(f)
                        self._leave(now)
                        return
                    if self.max_listen > 0 and now - self.heard_t > self.max_listen:
                        log.info("Skanner: maxtid %.0f s nådd på %.4f MHz, fortsätter",
                                 self.max_listen, self.win.freq / 1e6)
                        self._leave(now)
                        return
                    self.hold_until = now + self.hold
                    return
                if now < self.hold_until:
                    return
                self._leave(now)
            elif self.state == "span":
                avg, c, cnt = e.get_avg()
                if avg is None or c != self.span_c or cnt < 4:
                    return
                # Fönstren täcker c ± 0,4·SR utan glapp. Jämförelsen med grannkanaler går
                # en bit utanför, så att en kanal i kanten inte väljs om grannen är starkare.
                k = max(1, int(math.ceil(e.bw / 2 / self.chstep)))
                sel_lo = max(self.a, c - SR * 0.4)
                sel_hi = min(self.b, c + SR * 0.4)
                lo = max(self.a, sel_lo - k * self.chstep, c - SR * 0.48)
                hi = min(self.b, sel_hi + k * self.chstep, c + SR * 0.48)
                first = math.ceil((lo - self.a) / self.chstep - 1e-9)
                last = math.floor((hi - self.a) / self.chstep + 1e-9)
                chans = self.a + np.arange(first, last + 1) * self.chstep
                chans = chans[np.abs(chans - c) > e.bw / 2 + 3 * BIN_HZ]
                pws = np.array([e.chan_power(avg, *e._chan_bounds(e.mode, e.bw, f - c)) for f in chans])
                # Bara lokala toppar: grannkanaler inom bandbredden som är svagare är
                # sidband/läckage från samma sändare och ska inte räknas som egna signaler.
                best_f, best_p = None, -1e9
                for i, f in enumerate(chans):
                    pw = pws[i]
                    if not (sel_lo <= f <= sel_hi):
                        continue
                    if pw < e.squelch or int(f) in self.visited or self.locked(f):
                        continue
                    if pw < pws[max(0, i - k):i + k + 1].max():
                        continue
                    if pw > best_p:
                        best_f, best_p = f, pw
                if best_f is not None:
                    self.visited.add(int(best_f))
                    self.win.tune(best_f, keep_center=True)
                    self._listen(now, 0.2)
                    self.status = "Kollar: " + fmt_mhz(best_f) + " MHz"
                else:
                    self._next_span(now)

    # ── Fartygskarta (AIS) ──
    class ShipWindow(Adw.Window):
        MAX_AGE = 30 * 60          # fartyg försvinner efter 30 min utan meddelanden

        def __init__(self, main):
            super().__init__(title="Fartyg (AIS) – Radiospanaren")
            import kartvy
            self.K = kartvy
            self.main = main
            self.rows = {}
            self.set_default_size(1250, 780)
            self.set_hide_on_close(True)
            tv = Adw.ToolbarView()
            hb = Adw.HeaderBar()
            self.status = Gtk.Label(label="Väntar på AIS …")
            hb.set_title_widget(self.status)
            b = Gtk.Button(label="Visa alla")
            b.connect("clicked", lambda *_: self.mp.fit_all() or main.toast("Inga fartyg med position ännu"))
            hb.pack_end(b)
            b2 = Gtk.Button(label="Gå till AIS (162 MHz)")
            b2.set_tooltip_text("Ställ in mottagaren på AIS-kanalerna och slå på AIS-avkodningen")
            b2.connect("clicked", lambda *_: main._goto_decoder("AIS"))
            hb.pack_start(b2)
            tv.add_top_bar(hb)

            lat, lon, zoom = main.conf.get("ship_map_view", [59.33, 18.06, 9.0])
            self.mp = kartvy.MapPanel(center=(lat, lon), zoom=zoom or 9.0, on_select=self._select,
                                      user_agent=f"Radiospanaren/{VERSION} (+https://github.com/EriHil/radiospanaren-beta-v2)",
                                      toast=main.toast)
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            left.append(self.mp)
            legend = Gtk.Box(spacing=12)
            legend.set_margin_start(8)
            legend.set_margin_top(4)
            legend.set_margin_bottom(4)
            for cat in ("last", "tank", "passagerare", "fiske", "militar", "fritid", "tjanst", "okand"):
                r, g, bb = kartvy.COLORS[cat]
                lb = Gtk.Label()
                lb.set_markup(f'<span foreground="#{int(r * 255):02x}{int(g * 255):02x}{int(bb * 255):02x}">■</span> '
                              f'{GLib.markup_escape_text(kartvy.CATEGORY_NAMES[cat])}')
                lb.add_css_class("caption")
                legend.append(lb)
            lb = Gtk.Label(label="○ förtöjd/till ankars")
            lb.add_css_class("caption")
            legend.append(lb)
            sw_l = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER)
            sw_l.set_child(legend)
            left.append(sw_l)

            cols = [
                ("MMSI", lambda d: d["mmsi"], "mmsi", False),
                ("Namn", lambda d: d["name"] or d["special"], "name", False),
                ("Anrop", lambda d: d["call"], "call", False),
                ("Typ", lambda d: avkodare.ais_ship_type(d["shiptype"]) if d["shiptype"] is not None else "", "shiptype", False),
                ("Status", lambda d: d["status"], "status", False),
                ("Fart", lambda d: f"{d['sog']:.1f} kn" if d["sog"] is not None else "", "sog", False),
                ("Kurs", lambda d: f"{d['cog']:.0f}°" if d["cog"] is not None else "", "cog", False),
                ("Position", lambda d: f"{d['pos'][0]:.4f}, {d['pos'][1]:.4f}" if d["pos"] else "", "pos", False),
                ("Destination", lambda d: d["dest"], "dest", True),
                ("Senast", lambda d: _ago(d["last"]), "last", False),
            ]
            (self.store, self.cv, self.sel, _f, tcols, sw) = make_table(cols)
            self.cv.sort_by_column(tcols[-1], Gtk.SortType.DESCENDING)
            self.sel.set_autoselect(False)
            self.sel.set_can_unselect(True)
            self.sel.connect("notify::selected-item", self._on_row)
            self.cv.connect("activate", lambda cv, pos: self._osm(self.sel.get_item(pos)))
            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            hint = Gtk.Label(label="Klicka: visa på kartan · Dubbelklicka: öppna i OpenStreetMap", xalign=0)
            hint.add_css_class("dim-label")
            hint.set_margin_start(8)
            hint.set_margin_top(4)
            hint.set_margin_bottom(4)
            right.append(hint)
            right.append(sw)
            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            paned.set_start_child(left)
            paned.set_end_child(right)
            paned.set_position(700)
            tv.set_content(paned)
            self.set_content(tv)
            self._fit_n = 0
            self._opened = time.time()
            self._auto_fit = "ship_map_view" not in main.conf
            self.connect("close-request", self._on_close)

        def _on_close(self, *_):
            self.main.conf["ship_map_view"] = list(self.mp.view())
            return False

        def _select(self, mmsi):
            m = self.sel.get_model()
            for i in range(m.get_n_items()):
                if m.get_item(i).data["mmsi"] == mmsi:
                    self.sel.set_selected(i)
                    return

        def _on_row(self, sel, _p):
            it = sel.get_selected_item()
            if it:
                self._auto_fit = False          # användaren styr kartan nu
                self.mp.select(it.data["mmsi"])

        def _osm(self, item):
            if item and item.data["pos"]:
                lat, lon = item.data["pos"]
                try:
                    Gio.AppInfo.launch_default_for_uri(
                        f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=13/{lat:.5f}/{lon:.5f}", None)
                except GLib.Error as ex:
                    log.error("Kunde inte öppna kartan: %s", ex)

        def refresh(self):
            K = self.K
            now = time.time()
            ships = self.main.ships
            for mmsi in [k for k, v in ships.items() if now - v["last"] > self.MAX_AGE]:
                ships.pop(mmsi)
                row = self.rows.pop(mmsi, None)
                if row:
                    ok, pos = self.store.find(row)
                    if ok:
                        self.store.remove(pos)
                self.mp.remove(mmsi)
            for mmsi, d in ships.items():
                row = self.rows.get(mmsi)
                if row is None:
                    row = Row(d)
                    self.rows[mmsi] = row
                    self.store.append(row)
                else:
                    row.data = d
                    row.emit("updated")
                if d["pos"]:
                    cat = avkodare.ais_category(d["shiptype"], mmsi)
                    still = d["nav"] in (1, 5) or (d["sog"] is not None and d["sog"] < 0.5)
                    shape = "dot" if d["special"] else ("ring" if still else "ship")
                    head = d["hdg"] if d["hdg"] is not None else (d["cog"] or 0)
                    title = d["name"] or d["special"] or mmsi
                    if d["sog"] is not None and not still and not d["special"]:
                        title += f"\n{d['sog']:.1f} kn"
                    self.mp.update(mmsi, d["pos"][0], d["pos"][1], heading=head, title=title,
                                   color=K.COLORS[cat], shape=shape, alert=d["alert"], trail=not d["special"])
            npos = sum(1 for d in ships.values() if d["pos"])
            self.status.set_text(f"{len(ships)} fartyg · {npos} med position")
            # anpassa kartan när fler fartyg dyker upp de första minuterna
            if self._auto_fit and npos > self._fit_n and time.time() - self._opened < 180:
                self._fit_n = npos
                self.mp.fit_all()

    def _ago(t):
        d = max(0, time.time() - t)
        return f"{d:.0f} s" if d < 120 else f"{d / 60:.0f} min"

    # ── Aktivitetskarta (värmekarta över ett band) ──
    class ActivityView(Gtk.DrawingArea):
        def __init__(self, win):
            super().__init__(hexpand=True, vexpand=True)
            self.win = win
            self.mon = None
            self.lut = make_lut(win.conf.get("act_colormap", "klassisk"))
            self.dmax = 35.0
            self.mouse = None
            self.set_draw_func(self.on_draw)
            g = Gtk.GestureClick(button=1)
            g.connect("pressed", self._click)
            self.add_controller(g)
            mo = Gtk.EventControllerMotion()
            mo.connect("motion", lambda c, x, y: (setattr(self, "mouse", (x, y)), self.queue_draw()))
            mo.connect("leave", lambda c: (setattr(self, "mouse", None), self.queue_draw()))
            self.add_controller(mo)

        def layout(self, H):
            occ_h, axis_h = 72, 20
            return occ_h, axis_h, max(1, H - occ_h - axis_h)

        def _x_to_freq(self, x, W):
            m = self.mon
            return m.lo + max(0.0, min(1.0, x / max(1, W))) * (m.hi - m.lo)

        def _click(self, g, n, x, y):
            if self.mon:
                self.win.tune_to(self._x_to_freq(x, self.get_width()))

        def on_draw(self, area, cr, W, H, *_):
            cr.set_source_rgb(0.05, 0.06, 0.08)
            cr.paint()
            m = self.mon
            if m is None or W < 20 or H < 60:
                cr.set_source_rgba(1, 1, 1, 0.6)
                cr.set_font_size(15)
                msg = "Välj band och tryck ▶ Svep"
                e = cr.text_extents(msg)
                cr.move_to((W - e.width) / 2, H / 2)
                cr.show_text(msg)
                return
            occ_h, axis_h, heat_h = self.layout(H)
            heat_y = occ_h
            arr, times = m.image()
            occ = m.occupancy()
            peak = m.peak_hold()

            # ── värmekarta ──
            if arr is not None and len(arr):
                rows = arr.shape[0]
                idx = np.clip(arr / self.dmax * 255.0, 0, 255).astype(np.uint8)
                rgba = np.ascontiguousarray(self.lut[idx])
                surf = cairo.ImageSurface.create_for_data(memoryview(rgba), cairo.FORMAT_ARGB32,
                                                          m.ncols, rows, m.ncols * 4)
                cr.save()
                cr.rectangle(0, heat_y, W, heat_h)
                cr.clip()
                cr.translate(0, heat_y)
                cr.scale(W / m.ncols, heat_h / rows)
                cr.set_source_surface(surf, 0, 0)
                cr.get_source().set_filter(cairo.FILTER_NEAREST)
                cr.paint()
                cr.restore()

            # ── spärrade kanaler (t.ex. WFM 96–108 MHz) ──
            if getattr(m, "excluded", None) is not None and m.excluded.any():
                cr.set_source_rgba(0.5, 0.5, 0.5, 0.33)
                col = 0
                while col < m.ncols:
                    if m.excluded[col]:
                        c2 = col
                        while c2 < m.ncols and m.excluded[c2]:
                            c2 += 1
                        x0 = col / m.ncols * W
                        x1 = c2 / m.ncols * W
                        cr.rectangle(x0, heat_y, x1 - x0, heat_h)
                        cr.fill()
                        cx = (x0 + x1) / 2
                        if x1 - x0 > 60:
                            cr.set_source_rgba(1, 1, 1, 0.6)
                            cr.set_font_size(11)
                            t = "utesluten (WFM)"
                            e = cr.text_extents(t)
                            cr.move_to(cx - e.width / 2, heat_y + heat_h / 2)
                            cr.show_text(t)
                            cr.set_source_rgba(0.5, 0.5, 0.5, 0.33)
                        col = c2
                    else:
                        col += 1

            # ── upptagenhetsremsa ──
            cr.set_source_rgb(0.08, 0.09, 0.12)
            cr.rectangle(0, 0, W, occ_h)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.5)
            cr.set_font_size(10)
            cr.move_to(4, 12)
            cr.show_text("Upptagenhet")
            base = occ_h - 3
            for col in range(m.ncols):
                o = occ[col]
                if o <= 0:
                    continue
                x0 = col / m.ncols * W
                x1 = (col + 1) / m.ncols * W
                h = o * (occ_h - 16)
                r, g, b = (0.3 + 0.7 * o, 0.8 - 0.6 * o, 0.2)
                cr.set_source_rgb(r, g, b)
                cr.rectangle(x0, base - h, max(1, x1 - x0), h)
                cr.fill()
            # topp-håll som ljus linje
            cr.set_source_rgba(0.7, 0.85, 1, 0.5)
            cr.set_line_width(1)
            for col in range(0, m.ncols, max(1, m.ncols // W or 1)):
                x = col / m.ncols * W
                yy = base - min(1.0, peak[col] / self.dmax) * (occ_h - 16)
                cr.line_to(x, yy)
            cr.stroke()

            # ── frekvensaxel + bandplan ──
            ay = heat_y + heat_h
            cr.set_source_rgb(0.09, 0.1, 0.13)
            cr.rectangle(0, ay, W, axis_h)
            cr.fill()
            for blo, bhi, name in BANDPLAN:
                x0 = (blo * 1e6 - m.lo) / (m.hi - m.lo) * W
                x1 = (bhi * 1e6 - m.lo) / (m.hi - m.lo) * W
                if x1 < 0 or x0 > W:
                    continue
                hh = (sum(map(ord, name)) * 47) % 360 / 360.0
                import colorsys
                r, g, b = colorsys.hsv_to_rgb(hh, 0.5, 0.6)
                cr.set_source_rgba(r, g, b, 0.9)
                cr.rectangle(max(0, x0), ay, min(W, x1) - max(0, x0), 3)
                cr.fill()
            span = m.hi - m.lo
            raw = span / 9
            mag = 10 ** math.floor(math.log10(raw))
            tick = next(mm * mag for mm in (1, 2, 2.5, 5, 10) if mm * mag >= raw)
            dec = max(0, min(4, -int(math.floor(math.log10(tick / 1e6)))) if tick < 1e6 else 0)
            cr.set_font_size(10)
            ff = math.ceil(m.lo / tick) * tick
            while ff < m.hi:
                x = (ff - m.lo) / span * W
                cr.set_source_rgba(1, 1, 1, 0.07)
                cr.move_to(int(x) + 0.5, heat_y)
                cr.line_to(int(x) + 0.5, ay)
                cr.stroke()
                cr.set_source_rgba(1, 1, 1, 0.65)
                txt = f"{ff / 1e6:.{dec}f}"
                e = cr.text_extents(txt)
                cr.move_to(x - e.width / 2, ay + 14)
                cr.show_text(txt)
                ff += tick

            # ── muspekare ──
            if self.mouse:
                mx, my = self.mouse
                fm = self._x_to_freq(mx, W)
                col = m.freq_col(fm)
                txt = f"{fm / 1e6:.4f} MHz   upptagen {occ[col] * 100:.0f}%   topp {peak[col]:.0f} dB"
                b = band_name(fm)
                if b:
                    txt += f"   {b}"
                if arr is not None and len(arr) and heat_y <= my <= heat_y + heat_h:
                    ri = int((my - heat_y) / heat_h * len(arr))
                    if 0 <= ri < len(arr):
                        txt += f"   [{arr[ri][col]:.0f} dB]"
                cr.set_source_rgba(1, 1, 1, 0.3)
                cr.set_line_width(1)
                cr.move_to(int(mx) + 0.5, heat_y)
                cr.line_to(int(mx) + 0.5, heat_y + heat_h)
                cr.stroke()
                cr.set_font_size(12)
                e = cr.text_extents(txt)
                cr.set_source_rgba(0, 0, 0, 0.7)
                cr.rectangle(W - e.width - 14, heat_y + 3, e.width + 10, 18)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(W - e.width - 9, heat_y + 16)
                cr.show_text(txt)

    class ActivityWindow(Adw.Window):
        def __init__(self, main):
            super().__init__(title="Aktivitetskarta – Radiospanaren")
            import aktivitet
            self.AK = aktivitet
            self.main = main
            self.mon = None
            self.mode = "NFM"
            self.timer = None
            self.set_default_size(1180, 760)
            self.set_hide_on_close(True)

            tv = Adw.ToolbarView()
            hb = Adw.HeaderBar()
            self.btn = Gtk.ToggleButton(label="▶ Svep")
            self.btn.add_css_class("suggested-action")
            self.btn.connect("toggled", self._on_toggle)
            hb.pack_start(self.btn)
            names = [p[0] for p in aktivitet.PRESETS] + ["Eget intervall"]
            self.preset = Gtk.DropDown.new_from_strings(names)
            self.preset.connect("notify::selected", self._on_preset)
            hb.pack_start(self.preset)
            self.status = Gtk.Label(label="Välj band och tryck ▶ Svep")
            hb.set_title_widget(self.status)
            tv.add_top_bar(hb)

            bar = Gtk.Box(spacing=6)
            bar.add_css_class("toolbar")
            self.e_lo = Gtk.Entry(width_chars=8, placeholder_text="Från MHz")
            self.e_hi = Gtk.Entry(width_chars=8, placeholder_text="Till MHz")
            bar.append(Gtk.Label(label="Band:"))
            bar.append(self.e_lo)
            bar.append(Gtk.Label(label="–"))
            bar.append(self.e_hi)
            bar.append(Gtk.Label(label="MHz"))
            self.thr = Gtk.SpinButton.new_with_range(3, 40, 1)
            self.thr.set_value(main.conf.get("act_thresh", 6))
            self.thr.connect("value-changed", self._on_thr)
            bar.append(Gtk.Label(label="   Tröskel:"))
            bar.append(self.thr)
            bar.append(Gtk.Label(label="dB   Färg:"))
            cmaps = list(COLORMAPS)
            self.cmap = Gtk.DropDown.new_from_strings(cmaps)
            self.cmap.set_selected(cmaps.index(main.conf.get("act_colormap", "klassisk"))
                                   if main.conf.get("act_colormap", "klassisk") in cmaps else 0)
            self.cmap.connect("notify::selected", self._on_cmap)
            bar.append(self.cmap)
            tv.add_top_bar(bar)

            self.view = ActivityView(main)
            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            paned.set_start_child(self.view)
            paned.set_resize_start_child(True)
            cols = [("Frekvens (MHz)", lambda d: fmt_mhz(d["f"]), "f", False),
                    ("Upptagen", lambda d: f"{d['occ'] * 100:.0f} %", "occ", False),
                    ("Topp (dB)", lambda d: f"{d['pk']:.0f}", "pk", True)]
            (self.bstore, self.bcv, self.bsel, _f, _c, bsw) = make_table(cols)
            self.bcv.sort_by_column(_c[1], Gtk.SortType.DESCENDING)
            self.bcv.connect("activate", lambda cv, pos: self.tune_to(self.bsel.get_item(pos).data["f"]))
            rbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            lbl = Gtk.Label(label="Mest aktiva kanaler", xalign=0)
            lbl.add_css_class("heading")
            lbl.set_margin_start(8)
            lbl.set_margin_top(6)
            rbox.append(lbl)
            hint = Gtk.Label(label="Klicka i kartan eller en rad för att lyssna", xalign=0)
            hint.add_css_class("dim-label")
            hint.set_margin_start(8)
            rbox.append(hint)
            rbox.append(bsw)
            paned.set_end_child(rbox)
            paned.set_position(880)
            tv.set_content(paned)
            self.set_content(tv)
            self._set_preset(0)
            self.connect("close-request", self._on_close)

        def _set_preset(self, i):
            P = self.AK.PRESETS
            if 0 <= i < len(P):
                _n, lo, hi, mode, _step = P[i]
                self.e_lo.set_text(f"{lo:g}")
                self.e_hi.set_text(f"{hi:g}")
                self.mode = mode

        def _on_preset(self, dd, _p):
            self._set_preset(dd.get_selected())

        def _on_thr(self, sb):
            self.main.conf["act_thresh"] = sb.get_value()
            if self.mon:
                self.mon.thresh = sb.get_value()
            self.view.queue_draw()

        def _on_cmap(self, dd, _p):
            name = list(COLORMAPS)[dd.get_selected()]
            self.main.conf["act_colormap"] = name
            self.view.lut = make_lut(name)
            self.view.queue_draw()

        def tune_to(self, freq):
            self.main.tune(freq, self.mode)
            self.main.toast(f"Mottagaren ställd på {fmt_mhz(freq)} MHz ({self.mode}) – "
                            "svepet pausas medan du lyssnar" if self.btn.get_active() else
                            f"Mottagaren ställd på {fmt_mhz(freq)} MHz")
            if self.btn.get_active():
                self.btn.set_active(False)     # pausa svepet så man kan lyssna

        def _on_toggle(self, btn):
            if btn.get_active():
                if not self.main.engine.running:
                    self.main.toast("Starta mottagningen först")
                    btn.set_active(False)
                    return
                try:
                    lo = parse_freq(self.e_lo.get_text())
                    hi = parse_freq(self.e_hi.get_text())
                    self.mon = self.AK.BandMonitor(lo, hi, thresh_db=self.thr.get_value())
                except ValueError as ex:
                    self.main.toast(str(ex))
                    btn.set_active(False)
                    return
                self.main.scanner.stop()
                self.main.scan_btn.set_title("Starta skanning")
                self.main.activity_active = True
                self.view.mon = self.mon
                btn.set_label("■ Stoppa")
                btn.remove_css_class("suggested-action")
                btn.add_css_class("destructive-action")
                log.info("Aktivitetskarta startad: %.3f–%.3f MHz, %d kanaler, %d fönster",
                         lo / 1e6, hi / 1e6, self.mon.ncols, len(self.mon.spans))
                if self.timer is None:
                    self.timer = GLib.timeout_add(120, self._tick)
            else:
                self.main.activity_active = False
                btn.set_label("▶ Svep")
                btn.remove_css_class("destructive-action")
                btn.add_css_class("suggested-action")

        def _tick(self):
            if not self.btn.get_active():
                self.timer = None
                return False
            if not self.main.engine.running:
                self.btn.set_active(False)
                self.timer = None
                return False
            try:
                self.mon.tick(self.main.engine)
            except Exception:
                log.exception("Fel i aktivitetskartan")
                self.btn.set_active(False)
                self.timer = None
                return False
            self.view.queue_draw()
            self._update_list()
            return True

        def _update_list(self):
            busy = self.mon.busiest(40)
            have = {round(self.bstore.get_item(i).data["f"]): self.bstore.get_item(i)
                    for i in range(self.bstore.get_n_items())}
            seen = set()
            for f, occ, pk in busy:
                seen.add(round(f))
                row = have.get(round(f))
                d = {"f": f, "occ": occ, "pk": pk}
                if row is None:
                    self.bstore.append(Row(d))
                else:
                    row.data = d
                    row.emit("updated")
            for key, row in have.items():
                if key not in seen:
                    ok, pos = self.bstore.find(row)
                    if ok:
                        self.bstore.remove(pos)
            n = self.mon.sweeps
            self.status.set_text(f"{fmt_mhz(self.mon.lo)}–{fmt_mhz(self.mon.hi)} MHz · "
                                 f"{n} svep · {self.mon.sweep_dt:.1f} s/svep")

        def _on_close(self, *_):
            if self.btn.get_active():
                self.btn.set_active(False)
            return False

    # ── Huvudfönster ──
    # ── Signalanalys ──
    ANALYS_BW = [None, 500, 1000, 3000, 6000, 12500, 25000, 50000, 200000]
    ANALYS_BW_NAMES = ["Auto (signalens bredd)", "500 Hz", "1 kHz", "3 kHz", "6 kHz", "12,5 kHz", "25 kHz",
                       "50 kHz", "200 kHz"]

    def _fmt_hz(v, signed=False):
        if v is None:
            return "–"
        a = abs(v)
        sg = ("+" if v >= 0 else "−") if signed else ("−" if v < 0 else "")
        if a >= 1e6:
            return f"{sg}{a / 1e6:.3f} MHz"
        if a >= 1e3:
            return f"{sg}{a / 1e3:.2f} kHz".replace(".", ",")
        return f"{sg}{a:.0f} Hz"

    class MiniWaterfall(Gtk.DrawingArea):
        """Litet vattenfall som täcker den analyserade kanalen."""
        ROWS = 220

        def __init__(self, panel):
            super().__init__(hexpand=True, vexpand=True)
            self.panel = panel
            self.set_size_request(260, 90)
            self.wf = np.zeros((self.ROWS, analys.ChannelTap.WF_N), np.uint8)
            self.spec = None
            self.fs = None
            self.bw = None
            self.lo_db, self.hi_db = None, None
            self.zoom = 1.0
            self.mouse = None
            self.set_draw_func(self.on_draw)
            g = Gtk.GestureClick(button=1)
            g.connect("pressed", self.on_click)
            self.add_controller(g)
            sc = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            sc.connect("scroll", self.on_scroll)
            self.add_controller(sc)
            mo = Gtk.EventControllerMotion()
            mo.connect("motion", lambda c, x, y: self._mouse((x, y)))
            mo.connect("leave", lambda c: self._mouse(None))
            self.add_controller(mo)

        def _mouse(self, m):
            self.mouse = m
            self.queue_draw()

        def clear(self):
            self.wf[:] = 0
            self.spec = None
            self.lo_db = self.hi_db = None
            self.queue_draw()

        def push(self, db, fs, bw):
            if fs != self.fs or bw != self.bw:
                self.wf[:] = 0
                self.fs, self.bw = fs, bw
                self.lo_db = None
            fl, pk = float(np.median(db)), float(np.percentile(db, 99.5))
            if self.lo_db is None:
                self.lo_db, self.hi_db = fl - 4, max(pk, fl + 25)
            else:
                self.lo_db = self.lo_db * 0.95 + (fl - 4) * 0.05
                self.hi_db = self.hi_db * 0.95 + max(pk, fl + 25) * 0.05
            q = np.clip((db - self.lo_db) * (255.0 / max(1.0, self.hi_db - self.lo_db)), 0, 255).astype(np.uint8)
            self.wf[1:] = self.wf[:-1]
            self.wf[0] = q
            self.spec = db if self.spec is None else self.spec * 0.6 + db * 0.4

        def span(self):
            if not self.fs:
                return 1.0
            return float(min(self.fs, max(200.0, (self.bw or self.fs) * 1.3 * self.zoom)))

        def x_to_off(self, x):
            sp = self.span()
            return (x / max(1, self.get_width()) - 0.5) * sp

        def on_click(self, g, n, x, y):
            if self.fs:
                self.panel.retarget_offset(self.x_to_off(x))

        def on_scroll(self, ctrl, dx, dy):
            self.zoom = float(np.clip(self.zoom * (1.25 if dy > 0 else 0.8), 0.15, 4.0))
            self.queue_draw()
            return True

        def on_draw(self, area, cr, W, H, *_):
            S = self.panel.win.skin()
            cr.set_source_rgb(*S["bg"][:3])
            cr.paint()
            if not self.fs or W < 20 or H < 40:
                cr.set_source_rgba(*S["text"])
                cr.set_font_size(12)
                msg = self.panel.empty_msg()
                ext = cr.text_extents(msg)
                cr.move_to(max(4, (W - ext.width) / 2), H / 2)
                cr.show_text(msg)
                return
            N = self.wf.shape[1]
            sp = self.span()
            k0 = int(round(N / 2 - sp / self.fs * N / 2))
            k1 = int(round(N / 2 + sp / self.fs * N / 2))
            k0, k1 = max(0, k0), min(N, max(k0 + 2, k1))
            spec_h = int(H * 0.3)
            axis_h = 14
            wf_y = spec_h + axis_h
            wf_h = H - wf_y
            # vattenfall
            cols = np.linspace(k0, k1 - 1e-6, W).astype(np.int64)
            rows = int(min(self.ROWS, max(1, wf_h)))
            img = self.wf[:rows][:, cols]
            rgba = np.ascontiguousarray(self.panel.win.view.lut[img])
            surf = cairo.ImageSurface.create_for_data(memoryview(rgba), cairo.FORMAT_ARGB32, W, rows, W * 4)
            cr.save()
            cr.rectangle(0, wf_y, W, wf_h)
            cr.clip()
            cr.translate(0, wf_y)
            cr.scale(1, wf_h / rows)
            cr.set_source_surface(surf, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_NEAREST)
            cr.paint()
            cr.restore()
            # spektrum
            cr.set_source_rgb(*S["spec_bg"][:3])
            cr.rectangle(0, 0, W, spec_h)
            cr.fill()
            if self.spec is not None:
                v = self.spec[cols]
                rng = max(1.0, self.hi_db - self.lo_db + 6)
                yv = np.clip((self.hi_db + 3 - v) / rng, 0, 1) * spec_h
                cr.move_to(0, spec_h)
                for i in range(W):
                    cr.line_to(i, yv[i])
                cr.line_to(W, spec_h)
                cr.close_path()
                grad = cairo.LinearGradient(0, 0, 0, spec_h)
                grad.add_color_stop_rgba(0, *S["fill_top"])
                grad.add_color_stop_rgba(1, *S["fill_bot"])
                cr.set_source(grad)
                cr.fill_preserve()
                cr.set_source_rgb(*S["line"][:3])
                cr.set_line_width(1.1)
                cr.stroke()
            # axel
            cr.set_source_rgb(*S["axis_bg"][:3])
            cr.rectangle(0, spec_h, W, axis_h)
            cr.fill()
            cr.set_font_size(9)
            cr.set_source_rgba(*S["text"])
            for fr in (0.0, 0.25, 0.5, 0.75, 1.0):
                off = (fr - 0.5) * sp
                txt = "0" if abs(off) < 1e-6 else _fmt_hz(off, True)
                ext = cr.text_extents(txt)
                xx = fr * W - ext.width / 2
                cr.move_to(min(max(2, xx), W - ext.width - 2), spec_h + 11)
                cr.show_text(txt)

            def xo(off):
                return (off / sp + 0.5) * W
            # kanalgränser
            if self.bw:
                cr.set_source_rgba(*S["analys"][:3], 0.8)
                cr.set_line_width(1)
                cr.set_dash([4, 3])
                for e in (-self.bw / 2, self.bw / 2):
                    cr.move_to(int(xo(e)) + 0.5, 0)
                    cr.line_to(int(xo(e)) + 0.5, H)
                cr.stroke()
                cr.set_dash([])
            # mätta toner och mittfrekvens
            r = self.panel.last
            if r and r.get("fam") not in (None, "BRUS"):
                mid = r.get("f_mid", 0.0)
                cr.set_source_rgba(*S["tuned"][:3], 0.9)
                cr.set_line_width(1.5)
                cr.move_to(xo(mid), 0)
                cr.line_to(xo(mid), spec_h)
                cr.stroke()
                tones = [mid + t for t in r.get("fi_peaks", [])] if r.get("toner") and r.get("fam") != "OFDM" \
                    else (r.get("spec_toner", []) if r.get("fam") == "OFDM" else [])
                cr.set_source_rgba(*S["track"][:3], 0.95)
                for t in tones:
                    x = xo(t)
                    cr.move_to(x, spec_h - 1)
                    cr.line_to(x - 3, spec_h - 7)
                    cr.line_to(x + 3, spec_h - 7)
                    cr.close_path()
                    cr.fill()
                if r.get("occ_bw"):
                    cr.set_source_rgba(*S["track"][:3], 0.35)
                    cr.rectangle(xo(r["f_lo"]), 1, max(2, xo(r["f_hi"]) - xo(r["f_lo"])), 3)
                    cr.fill()
            if self.mouse:
                mx, my = self.mouse
                off = self.x_to_off(mx)
                txt = _fmt_hz(off, True)
                cr.set_font_size(11)
                ext = cr.text_extents(txt)
                cr.set_source_rgba(0, 0, 0, 0.7)
                cr.rectangle(W - ext.width - 12, 3, ext.width + 8, 16)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(W - ext.width - 8, 15)
                cr.show_text(txt)
                cr.set_source_rgba(1, 1, 1, 0.35)
                cr.move_to(int(mx) + 0.5, 0)
                cr.line_to(int(mx) + 0.5, H)
                cr.stroke()

    class AnalysDiagram(Gtk.DrawingArea):
        """Konstellation (PSK/QAM), frekvensfördelning (FSK/FM), envelop (AM/SSB/CW) eller tonspektrum (OFDM)."""

        def __init__(self, panel):
            super().__init__()
            self.panel = panel
            self.set_size_request(200, 120)
            self.set_draw_func(self.on_draw)

        def on_draw(self, area, cr, W, H, *_):
            S = self.panel.win.skin()
            cr.set_source_rgb(*S["bg"][:3])
            cr.paint()
            r = self.panel.last
            cr.set_font_size(10)
            title = "Diagram"
            pad = 8
            x0, y0, w, h = pad, 18, W - 2 * pad, H - 18 - pad
            cr.set_source_rgba(*S["grid"][:3], 0.25)
            cr.rectangle(x0 + 0.5, y0 + 0.5, w, h)
            cr.set_line_width(1)
            cr.stroke()
            if not r or r.get("fam") in (None, "BRUS"):
                cr.set_source_rgba(*S["text"])
                cr.move_to(pad, 12)
                cr.show_text(title)
                return
            col = S["line"][:3]
            fd = r.get("bild")
            if fd is not None and fd.bild is not None and len(fd.bild):
                title = f"Faxbild ({len(fd.rows)} rader)"
                img = fd.bild[-int(h * fd.W / max(1, w)):] if len(fd.bild) else fd.bild
                surf = gray_surface(img)
                cr.save()
                cr.rectangle(x0, y0, w, h)
                cr.clip()
                sc = w / fd.W
                cr.translate(x0, y0 + h - len(img) * sc)
                cr.scale(sc, sc)
                cr.set_source_surface(surf, 0, 0)
                cr.paint()
                cr.restore()
            elif r.get("konst") is not None:
                title = f"Konstellation ({r['klass'].split(' ')[0]})"
                s = np.asarray(r["konst"])
                cx, cy, rad = x0 + w / 2, y0 + h / 2, min(w, h) / 2 * 0.62
                cr.set_source_rgba(*S["grid"][:3], 0.35)
                cr.arc(cx, cy, rad, 0, 2 * math.pi)
                cr.stroke()
                cr.move_to(x0, cy)
                cr.line_to(x0 + w, cy)
                cr.move_to(cx, y0)
                cr.line_to(cx, y0 + h)
                cr.stroke()
                cr.set_source_rgba(*col, 0.75)
                for p in s:
                    cr.rectangle(cx + p.real * rad - 1, cy - p.imag * rad - 1, 2.2, 2.2)
                cr.fill()
            elif r.get("fam") == "OFDM" and r.get("spec_f") is not None:
                title = f"Tonspektrum ({r.get('toner') or '?'} toner)"
                f, d = np.asarray(r["spec_f"]), np.asarray(r["spec_db"])
                self._curve(cr, f, d, x0, y0, w, h, col)
                lo, hi = f[0], f[-1]
                cr.set_source_rgba(*S["track"][:3], 0.9)
                for t in r.get("spec_toner", []):
                    xx = x0 + (t - lo) / max(1e-9, hi - lo) * w
                    cr.move_to(xx, y0 + h)
                    cr.line_to(xx, y0 + h - 6)
                cr.stroke()
            elif r.get("kurva") is not None:
                title = "Envelop (senaste ~sekunden)"
                k = np.asarray(r["kurva"], float)
                self._curve(cr, np.arange(len(k)), np.clip(k, 0, 1.4), x0, y0, w, h, col, fill=False, ymin=0)
            elif r.get("hist") is not None:
                title = "Frekvensfördelning"
                c, hs = r["hist"]
                self._curve(cr, np.asarray(c), np.asarray(hs, float), x0, y0, w, h, col, ymin=0)
                cr.set_source_rgba(*S["track"][:3], 0.95)
                lo, hi = c[0], c[-1]
                for t in r.get("fi_peaks", []):
                    xx = x0 + (t - lo) / max(1e-9, hi - lo) * w
                    cr.move_to(xx, y0)
                    cr.line_to(xx, y0 + 6)
                cr.stroke()
            cr.set_source_rgba(*S["text"])
            cr.move_to(pad, 12)
            cr.show_text(title)

        @staticmethod
        def _curve(cr, x, y, x0, y0, w, h, col, fill=True, ymin=None):
            if len(x) < 2:
                return
            lo = float(np.min(y)) if ymin is None else ymin
            hi = float(np.max(y))
            rng = max(1e-9, hi - lo)
            xs = x0 + (x - x[0]) / max(1e-9, x[-1] - x[0]) * w
            ys = y0 + h - (y - lo) / rng * (h - 4)
            cr.move_to(xs[0], ys[0])
            for a, b in zip(xs[1:], ys[1:]):
                cr.line_to(a, b)
            if fill:
                cr.line_to(xs[-1], y0 + h)
                cr.line_to(xs[0], y0 + h)
                cr.close_path()
                cr.set_source_rgba(*col, 0.25)
                cr.fill_preserve()
            cr.set_source_rgba(*col, 0.95)
            cr.set_line_width(1.2)
            cr.stroke()

    _GRAY = (0xFF000000 | (np.arange(256, dtype=np.uint32) * 0x010101)).astype(np.uint32)

    def gray_surface(img):
        img = np.ascontiguousarray(img)
        rgba = np.ascontiguousarray(_GRAY[img])
        h, w = img.shape
        return cairo.ImageSurface.create_for_data(memoryview(rgba), cairo.FORMAT_ARGB32, w, h, w * 4)

    class FaxWindow(Adw.Window):
        """Hela faxbilden med spara, ny bild och manuell justering av radstarten."""

        def __init__(self, panel):
            super().__init__(title="Väderfax – Radiospanaren", transient_for=panel.win,
                             default_width=820, default_height=900)
            self.panel = panel
            tv = Adw.ToolbarView()
            hb = Adw.HeaderBar()
            for label, tip, cb in (("💾 Spara PNG", "Spara bilden i inspelningsmappen", self.save),
                                   ("Ny bild", "Börja om med en tom bild", self.new),
                                   ("◀", "Flytta radstarten åt vänster", lambda: self.shift(-12)),
                                   ("▶", "Flytta radstarten åt höger", lambda: self.shift(12))):
                b = Gtk.Button(label=label, tooltip_text=tip)
                b.connect("clicked", lambda *_, cb=cb: cb())
                hb.pack_start(b)
            tv.add_top_bar(hb)
            self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
            self.area.set_draw_func(self.on_draw)
            sw = Gtk.ScrolledWindow()
            sw.set_child(self.area)
            self.sw = sw
            tv.set_content(sw)
            self.set_content(tv)
            self.connect("close-request", lambda *_: self.set_visible(False) or True)

        def dec(self):
            return self.panel.fax_dec

        def on_draw(self, area, cr, W, H, *_):
            cr.set_source_rgb(0.1, 0.1, 0.1)
            cr.paint()
            d = self.dec()
            if d is None or d.bild is None or not len(d.bild):
                return
            sc = W / d.W
            need = int(len(d.bild) * sc) + 1
            if abs(area.get_content_height() - need) > 2:
                area.set_content_height(need)
            cr.scale(sc, sc)
            cr.set_source_surface(gray_surface(d.bild), 0, 0)
            cr.paint()

        def save(self):
            d = self.dec()
            if d is None or d.bild is None:
                return
            path = os.path.join(rec_dir(), f"fax_{datetime.datetime.now():%Y%m%d-%H%M%S}.png")
            try:
                gray_surface(d.bild).write_to_png(path)
                self.panel.win.toast(f"Faxbilden sparad: {path}")
                log.info("Faxbild sparad: %s", path)
            except Exception as ex:
                self.panel.win.toast(f"Kunde inte spara: {ex}")

        def new(self):
            d = self.dec()
            if d is not None:
                d._new_image()
                self.area.queue_draw()

        def shift(self, px):
            d = self.dec()
            if d is not None:
                d.offset = (d.offset + px) % d.W
                if d.rows:
                    d.bild = np.roll(np.asarray(d.rows[-1600:]), -d.offset, axis=1)
                self.area.queue_draw()

    class AnalysPanel(Gtk.Box):
        ROWS = [("klass", "Signaltyp"), ("freq", "Frekvens (mitt)"), ("avv", "Avvikelse från mål"),
                ("snr", "SNR"), ("occ", "Upptagen bandbredd"), ("baud", "Symboltakt"),
                ("shift", "Shift / tonavstånd"), ("toner", "Antal toner"), ("ljud", "Ljudfrekvens (USB/LSB)"),
                ("dev", "Deviation"), ("am", "Modulationsgrad"), ("ctcss", "Subton (CTCSS)"),
                ("skur", "Sändningsmönster"), ("env", "Envelopvariation"), ("trafik", "Trafik"),
                ("avk1", ""), ("avk2", ""), ("avk3", "")]

        def __init__(self, win):
            super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self.win = win
            self.an = win.analyzer
            self.last = None
            self._rid = -1
            self.text_t = 0
            self.set_margin_top(6)
            self.set_margin_start(6)
            self.set_margin_end(6)
            self.set_margin_bottom(6)

            tb = Gtk.Box(spacing=6)
            self.title = Gtk.Label(xalign=0, hexpand=True)
            self.title.set_markup("<b>Signalanalys</b> – klicka på en bärvåg i vattenfallet")
            self.title.set_ellipsize(Pango.EllipsizeMode.END)
            tb.append(self.title)
            self.bw_dd = Gtk.DropDown.new_from_strings(ANALYS_BW_NAMES)
            self.bw_dd.set_selected(int(win.conf.get("analys_bw_idx", 0)) % len(ANALYS_BW))
            self.bw_dd.set_tooltip_text("Analysbandbredd")
            self.bw_dd.connect("notify::selected", self._on_bw)
            tb.append(self.bw_dd)
            self.dec_names = [k.namn for k in analys.AVKODARE]
            self.dec_dd = Gtk.DropDown.new_from_strings(["Avkodare: automatiskt"] + self.dec_names + ["Ingen avkodare"])
            self.dec_dd.set_tooltip_text("Välj avkodare – automatiskt efter signaltyp, eller tvinga fram en viss")
            self.dec_dd.connect("notify::selected", self._on_dec)
            tb.append(self.dec_dd)
            self.fax_btn = Gtk.Button(label="🖼 Faxbild")
            self.fax_btn.set_tooltip_text("Visa den mottagna faxbilden i ett eget fönster")
            self.fax_btn.connect("clicked", lambda *_: self.open_fax())
            self.fax_btn.set_visible(False)
            self.fax_win = None
            self.fax_dec = None
            tb.append(self.fax_btn)
            self.follow = Gtk.ToggleButton(label="Följ inställd")
            self.follow.set_tooltip_text("Analysera alltid den kanal som demodulatorn är inställd på")
            self.follow.set_active(bool(win.conf.get("analys_follow", False)))
            self.follow.connect("toggled", self._on_follow)
            tb.append(self.follow)
            b = Gtk.Button(label="🎧 Lyssna")
            b.set_tooltip_text("Ställ in demodulatorn på den analyserade signalen")
            b.connect("clicked", lambda *_: self.listen())
            tb.append(b)
            b = Gtk.Button(icon_name="edit-clear-symbolic")
            b.set_tooltip_text("Rensa klartext")
            b.connect("clicked", lambda *_: self.textbuf.set_text(""))
            tb.append(b)
            b = Gtk.Button(icon_name="edit-copy-symbolic")
            b.set_tooltip_text("Kopiera mätvärden och klartext")
            b.connect("clicked", lambda *_: self.copy())
            tb.append(b)
            self.detach_btn = Gtk.Button(icon_name="window-new-symbolic")
            self.detach_btn.set_tooltip_text("Visa analysen i ett eget fönster")
            self.detach_btn.connect("clicked", lambda *_: win.toggle_analys_window())
            tb.append(self.detach_btn)
            self.append(tb)

            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
            # vänster: litet vattenfall överst, mätvärden under
            left = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
            self.mini = MiniWaterfall(self)
            fr = Gtk.Frame()
            fr.set_child(self.mini)
            left.set_start_child(fr)
            left.set_resize_start_child(True)
            left.set_shrink_start_child(False)
            grid = Gtk.Grid(column_spacing=14, row_spacing=2)
            grid.add_css_class("analys-ram")
            self.cells = {}
            self.grid = grid
            self._cols = 0
            for key, title in self.ROWS:
                l1 = Gtk.Label(label=title, xalign=0, valign=Gtk.Align.START)
                l1.add_css_class("dim-label")
                l2 = Gtk.Label(label="–", xalign=0, hexpand=True, selectable=True, wrap=True)
                l2.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                l2.add_css_class("analys-klass" if key == "klass" else "monospace")
                self.cells[key] = (l1, l2)
            self._layout_cells(2)
            sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.set_size_request(-1, 60)
            sw.set_child(grid)
            left.set_end_child(sw)
            left.set_shrink_end_child(False)
            left.set_position(160)
            paned.set_start_child(left)
            paned.set_resize_start_child(True)
            paned.set_shrink_start_child(False)

            # höger: diagram, möjliga signaltyper och klartext
            right = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
            self.diag = AnalysDiagram(self)
            fr = Gtk.Frame()
            fr.set_child(self.diag)
            right.set_start_child(fr)
            right.set_shrink_start_child(False)
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.kand = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True)
            self.kand.add_css_class("analys-ram")
            self.kand.set_markup("<b>Möjliga signaltyper</b>\n–")
            info.append(self.kand)
            info.append(Gtk.Label(label="Klartext från avkodaren", xalign=0, css_classes=["dim-label"]))
            self.textbuf = Gtk.TextBuffer()
            tv = Gtk.TextView(buffer=self.textbuf, editable=False, wrap_mode=Gtk.WrapMode.CHAR, monospace=True,
                              vexpand=True)
            tv.add_css_class("analys-text")
            tv.set_left_margin(6)
            tv.set_top_margin(4)
            tv.set_size_request(-1, 70)
            self.textview = tv
            info.append(tv)
            sw2 = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw2.set_child(info)
            self.info_sw = sw2
            right.set_end_child(sw2)
            right.set_shrink_end_child(False)
            right.set_position(150)
            right.set_size_request(300, -1)
            paned.set_end_child(right)
            paned.set_resize_end_child(False)
            paned.set_shrink_end_child(False)
            self.append(paned)
            self._update_rows(None)

        def _layout_cells(self, cols):
            """Två kolumner med mätvärden när det finns plats, annars en."""
            if cols == self._cols:
                return
            self._cols = cols
            for l1, l2 in self.cells.values():
                if l1.get_parent() is not None:
                    self.grid.remove(l1)
                    self.grid.remove(l2)
            for i, (key, _t) in enumerate(self.ROWS):
                l1, l2 = self.cells[key]
                c, r = (i % cols) * 2, i // cols
                self.grid.attach(l1, c, r, 1, 1)
                self.grid.attach(l2, c + 1, r, 1, 1)

        # ── mål ──
        def empty_msg(self):
            st = self.an.tap.status
            if self.an.tap.target is None:
                return "Klicka på en bärvåg i det stora vattenfallet"
            if not self.win.engine.running:
                return "Starta mottagningen"
            if st != "ok":
                return f"Målet ligger {st}"
            return "Väntar på data …"

        def chosen_bw(self, auto_bw):
            bw = ANALYS_BW[self.bw_dd.get_selected()] if self.bw_dd.get_selected() < len(ANALYS_BW) else None
            if bw is None:
                bw = float(np.clip(auto_bw * 1.35, 400, 250e3))
            return bw

        def set_target(self, f, auto_bw):
            bw = self.chosen_bw(auto_bw)
            self.auto_bw = auto_bw
            self.an.set_target(f, bw)
            self.fax_dec = None
            self.fax_btn.set_visible(False)
            self.last = None
            self._rid = -1
            self.mini.clear()
            self.mini.zoom = 1.0
            self.title.set_markup(f"<b>Analys:</b> {fmt_mhz(f)} MHz  ·  {_fmt_hz(bw)}  "
                                  f"<span alpha='60%'>{GLib.markup_escape_text(band_name(f) or '')}</span>")
            self._update_rows(None)
            self.diag.queue_draw()
            log.info("Signalanalys: %.6f MHz, bandbredd %.0f Hz", f / 1e6, bw)

        def retarget_offset(self, off):
            t = self.an.tap.target
            if t is None:
                return
            f = t + off
            snap = self.win.carrier_at(f, tol=max(200, (self.an.tap.bw or 3000) * 0.08))
            self.set_target(snap[0] if snap else f, snap[1] if snap else getattr(self, "auto_bw", self.an.tap.bw))

        def _on_bw(self, *_):
            self.win.conf["analys_bw_idx"] = int(self.bw_dd.get_selected())
            t = self.an.tap.target
            if t is not None:
                self.set_target(t, getattr(self, "auto_bw", self.an.tap.bw or 3000))

        def _on_dec(self, *_):
            i = int(self.dec_dd.get_selected())
            if i == 0:
                self.an.tvinga = None
            elif i <= len(self.dec_names):
                self.an.tvinga = self.dec_names[i - 1]
            else:
                self.an.tvinga = ""          # ingen avkodare körs
            log.info("Signalanalys: avkodare %s", self.an.tvinga if self.an.tvinga is not None else "automatiskt")

        def open_fax(self):
            if self.fax_dec is None:
                return
            if self.fax_win is None:
                self.fax_win = FaxWindow(self)
            self.fax_win.present()

        def _on_follow(self, b):
            self.win.conf["analys_follow"] = b.get_active()
            if b.get_active():
                self.follow_tuned()

        def follow_tuned(self):
            e = self.win.engine
            lo, hi = e._chan_bounds(e.mode, e.bw, self.win.freq)
            f = (lo + hi) / 2
            t = self.an.tap.target
            if t is None or abs(t - f) > 1 or abs((self.an.tap.bw or 0) - self.chosen_bw(hi - lo)) > 1:
                self.set_target(f, hi - lo)

        def listen(self):
            r = self.last
            f = (r or {}).get("f_abs") or self.an.tap.target
            if f is None:
                return
            m = self.win.engine.mode
            fam = (r or {}).get("fam")
            if fam in ("AM",) and m not in ("AM",):
                m = "AM"
            elif fam == "FM" and m not in ("NFM", "WFM"):
                m = "NFM" if (r.get("occ_bw") or 0) < 40e3 else "WFM"
            elif fam in ("FSK2", "MFSK", "OFDM", "PSK8", "QPSK", "BPSK", "LIN", "SSB", "MSK", "FSK4") \
                    and f < 30e6 and m not in ("USB", "LSB"):
                m = "USB"
            elif fam == "OOK" and m not in ("CW", "USB", "LSB"):
                m = "CW"
            if m == "USB":
                ac = (r or {}).get("ljud_mitt") or 1500
                self.win.tune(f - ac, m)
            elif m == "LSB":
                self.win.tune(f + 1500, m)
            else:
                self.win.tune(f, m)

        # ── uppdatering ──
        def tick(self):
            an = self.an
            got = False
            while an.tap.lines:
                try:
                    db, fs, tgt, bw = an.tap.lines.popleft()
                except IndexError:
                    break
                self.mini.push(db, fs, bw)
                got = True
            if got or an.tap.target is None:
                self.mini.queue_draw()
            gw = self.grid.get_width()
            if gw > 0:
                self._layout_cells(2 if gw >= 620 else 1)
            if self.follow.get_active():
                self.follow_tuned()
            an.dial, an.mode = self.win.freq, self.win.engine.mode
            if an.result_id != self._rid:
                self._rid = an.result_id
                r = an.result
                if r is not None and r.get("target") == an.tap.target:
                    self.last = r
                    self._update_rows(r)
                    self.diag.queue_draw()
                    fd = r.get("bild")
                    if fd is not None:
                        self.fax_dec = fd
                        self.fax_btn.set_visible(True)
                        if self.fax_win is not None and self.fax_win.get_visible():
                            self.fax_win.area.queue_draw()
            while an.msg_q:
                try:
                    m = an.msg_q.popleft()
                except IndexError:
                    break
                try:
                    self.win._on_decoded(m)
                except Exception:
                    log.exception("Kunde inte visa avkodat meddelande")
            while an.text_q:
                try:
                    name, txt = an.text_q.popleft()
                except IndexError:
                    break
                self.textbuf.insert(self.textbuf.get_end_iter(), txt)
                self.text_t = time.time()
                if self.textbuf.get_char_count() > 20000:
                    s_ = self.textbuf.get_start_iter()
                    e_ = self.textbuf.get_iter_at_offset(5000)
                    self.textbuf.delete(s_, e_)
                adj = self.info_sw.get_vadjustment()
                GLib.idle_add(lambda: adj.set_value(adj.get_upper()) or False)

        def _set(self, key, val, title=None):
            l1, l2 = self.cells[key]
            show = val not in (None, "", "–")
            l1.set_visible(show)
            l2.set_visible(show)
            if show:
                l2.set_text(str(val))
                if title is not None:
                    l1.set_text(title)

        def traffic(self, r):
            fam = r.get("fam")
            if time.time() - self.text_t < 15:
                return "Klartext – avkodas (se rutan till höger)"
            if fam in ("AM", "FM", "SSB"):
                return ("Analogt tal/ljud – okrypterat, tryck Lyssna" if r.get("ljudvariation", 0) > 2.5
                        else "Analog – ingen tydlig talaktivitet just nu")
            if fam == "OOK":
                return "Morse – klartext om avkodningen ger läsbar text"
            if fam == "BÄRVÅG":
                return "Ingen information (ren bärvåg)"
            k = r.get("kandidater") or []
            if k:
                info = k[0][1].get("info", "")
                if "rypter" in info:
                    return "Digital – ofta krypterad; kan identifieras men inte läsas"
                if "lartext" in info:
                    return "Digital – klartextprotokoll (avkodare för typen saknas än)"
            return "Digital – innehåll okänt" if fam not in ("BRUS", None) else None

        def _update_rows(self, r):
            if not r or r.get("fam") in (None, "BRUS"):
                for key, _t in self.ROWS:
                    self._set(key, None)
                self._set("klass", "Ingen signal" if r else "–")
                if r:
                    self._set("snr", f"{r.get('snr', 0):.1f} dB")
                self.kand.set_markup("<b>Möjliga signaltyper</b>\n–")
                return
            fam = r["fam"]
            self._set("klass", r["klass"])
            self._set("freq", f"{fmt_mhz(r['f_abs'])} MHz")
            self._set("avv", _fmt_hz(r.get("f_mid"), True))
            self._set("snr", f"{r.get('snr', 0):.1f} dB (topp {r.get('peak_snr', 0):.0f})")
            self._set("occ", _fmt_hz(r.get("occ_bw")))
            b = r.get("baud")
            self._set("baud", f"{b:.2f} Bd".replace(".", ",") if b else None)
            sh = r.get("shift")
            self._set("shift", _fmt_hz(sh) if sh else None)
            self._set("toner", r.get("toner") if r.get("toner") else None)
            ac = r.get("ljud_mitt")
            self._set("ljud", f"{ac:.0f} Hz" if ac else None)
            self._set("dev", f"±{_fmt_hz(r.get('deviation'))}" if fam == "FM" else None)
            self._set("am", f"{r.get('am_djup', 0) * 100:.0f} %" if fam == "AM" else None)
            self._set("ctcss", f"{r['ctcss']:.1f} Hz" if r.get("ctcss") else None)
            self._set("skur", f"skurar ({r.get('duty', 0) * 100:.0f} % på)" if r.get("skurar") else "kontinuerlig")
            self._set("env", f"{r.get('cv', 0):.2f}")
            self._set("trafik", self.traffic(r))
            vals = r.get("avk_varden") or []
            for i, key in enumerate(("avk1", "avk2", "avk3")):
                if i < len(vals):
                    self._set(key, vals[i][1], vals[i][0])
                else:
                    self._set(key, None)
            if r.get("symboler") and len(vals) < 3:
                self._set(("avk1", "avk2", "avk3")[len(vals)], r["symboler"][-40:], "Symboler (senaste)")
            k = r.get("kandidater") or []
            lines = ["<b>Möjliga signaltyper</b>"]
            if not k:
                lines.append("<span alpha='60%'>ingen känd typ passar</span>")
            for sc, e, why in k:
                kat = e.get("kat", "")
                colr = {"militär": "#ff7a5c", "myndighet": "#ffb547"}.get(kat, "#7fe0d0")
                lines.append(f"<span foreground='{colr}'>●</span> <b>{GLib.markup_escape_text(e['namn'])}</b> "
                             f"<span alpha='70%'>{sc:.0f} % · {GLib.markup_escape_text(kat)}</span>")
            if k:
                lines.append(f"<span size='small' alpha='75%'>{GLib.markup_escape_text(k[0][1].get('info', ''))}</span>")
            self.kand.set_markup("\n".join(lines))

        def copy(self):
            r = self.last or {}
            out = [f"Radiospanaren signalanalys {time.strftime('%Y-%m-%d %H:%M:%S')}"]
            for key, title in self.ROWS:
                l1, l2 = self.cells[key]
                if l2.get_visible():
                    out.append(f"{l1.get_text() or title}: {l2.get_text()}")
            for sc, e, why in r.get("kandidater") or []:
                out.append(f"Kandidat: {e['namn']} ({sc:.0f} %) {why}")
            txt = self.textbuf.get_text(self.textbuf.get_start_iter(), self.textbuf.get_end_iter(), False)
            if txt:
                out.append("Klartext:\n" + txt[-2000:])
            self.get_clipboard().set("\n".join(out))
            self.win.toast("Analysen kopierad")

    class MainWindow(Adw.ApplicationWindow):
        def __init__(self, app, conf, args):
            super().__init__(application=app, title=APP_NAME)
            self.conf = conf
            self.args = args
            self.engine = Engine()
            self.audio = AudioOut()
            self.web = None
            self.wf_center = conf["freq"] + SR * 0.15
            self.freq = conf["freq"]
            self.latest_db = None
            self.latest_c = None
            self._relevel_at = 0
            self.active_tracks = []
            self.log_rows = {}
            self._updating = False
            self._auto_level_pending = True
            self.scanner = Scanner(self)
            w, h = conf.get("window", [1400, 900])
            self.set_default_size(w, h)

            e = self.engine
            e.mode = conf["mode"] if conf["mode"] in MODES else "WFM"
            e.bw = conf["bw"] or DEFAULT_BW[e.mode]
            e.squelch = conf["squelch"]
            e.volume = conf["volume"] / 100
            e.auto_rec = conf["auto_rec"]
            e.detector.thresh = conf["det_thresh"]
            e.detector.enabled = conf["det_enabled"]
            e.on_error = lambda msg: GLib.idle_add(self._on_engine_error, msg)
            e.on_stopped = lambda: GLib.idle_add(self._on_engine_stopped)
            e.on_event = lambda k, v: GLib.idle_add(self._on_engine_event, k, v)
            e.add_sink(self.audio)
            self.decoded_rows = 0
            self.suggested = []     # bärvågor som skannern hoppat över denna session
            self.ships = {}         # MMSI -> sammanslagen fartygsinformation (AIS)
            self.ship_win = None
            self.activity_win = None
            self.activity_active = False
            self._ship_tick = 0
            self.hub = avkodare.DecoderHub(SR, lambda m: GLib.idle_add(self._on_decoded, m))
            for k in ("ACARS", "AIS", "APRS"):
                self.hub.enabled[k] = bool(conf.get(k.lower(), False))
            e.decoders = self.hub
            self.analyzer = analys.Analyzer(SR, os.path.join(CONF_DIR, "signaler.json"))
            e.analyzer = self.analyzer
            self.analyzer.start()
            self.an_win = None
            e.center = self.wf_center
            e.offset = self.freq - e.center

            self.toasts = Adw.ToastOverlay()
            tv = Adw.ToolbarView()
            tv.add_top_bar(self._build_header())
            outer = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            outer.set_start_child(self._build_sidebar())
            outer.set_resize_start_child(False)
            outer.set_shrink_start_child(False)
            outer.set_position(conf.get("sidebar_width", 330))
            self.outer = outer
            right = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
            self.view = SpectrumView(self)
            right.set_start_child(self.view)
            right.set_end_child(self._build_bottom())
            right.set_position(int(h * 0.62))
            self.right_paned = right
            outer.set_end_child(right)
            self.toasts.set_child(outer)
            tv.set_content(self.toasts)
            self.set_content(tv)

            self._load_bookmarks()
            self.connect("close-request", self._on_close)
            GLib.timeout_add(33, self._tick)
            GLib.timeout_add(400, self._slow_tick)
            self._update_freq_label()

            global _gui_error_hook
            _gui_error_hook = lambda msg: GLib.idle_add(
                self.toast, f"Fel: {msg} – se Felsökning → Kopiera felrapport")

            if conf["web_enabled"]:
                GLib.idle_add(self._start_web)
            if args.start or args.skanna:
                GLib.idle_add(lambda: self.btn_start.set_active(True) or False)
            if args.kalibrera:
                GLib.timeout_add(900, lambda: self._open_calibration() or False)
            if args.aktivitetskarta:
                GLib.timeout_add(800, lambda: self._open_activity() or False)
            if args.fartygskarta:
                GLib.timeout_add(800, lambda: self._open_ship_map() or False)
            if args.flik:
                GLib.idle_add(lambda: self.bottom_nb.set_current_page(
                    ["signallogg", "bokmarken", "avkodat", "analys"].index(args.flik)) or False)
            if args.skarmbild:
                path, secs = args.skarmbild[0], float(args.skarmbild[1])
                GLib.timeout_add(int(secs * 1000), lambda: self.save_screenshot(path) and False)
            if args.skanna:
                GLib.timeout_add(1500, lambda: (self.scanner.active or self._toggle_scan()) and False)
            if getattr(args, "analysera", None):
                fa = parse_freq(args.analysera)

                def _start_analys():
                    hit = self.carrier_at(fa, tol=5000)
                    self.analyse(*(hit or (fa, DEFAULT_BW.get(self.engine.mode, 3000))))
                    return False
                GLib.timeout_add(2500, _start_analys)

        # ── delar av fönstret ──
        def _build_header(self):
            hb = Adw.HeaderBar()
            self.btn_start = Gtk.ToggleButton(label="▶ Starta")
            self.btn_start.add_css_class("suggested-action")
            self.btn_start.connect("toggled", self._on_start_toggled)
            hb.pack_start(self.btn_start)

            self.freq_entry = Gtk.Entry()
            self.freq_entry.add_css_class("freq-entry")
            self.freq_entry.set_width_chars(14)
            self.freq_entry.set_tooltip_text("Frekvens. Skriv t.ex. 145.5, 7074k eller 1090 MHz och tryck Enter")
            self.freq_entry.connect("activate", self._on_freq_entry)
            hb.pack_start(self.freq_entry)
            mhz = Gtk.Label(label="MHz")
            mhz.add_css_class("dim-label")
            hb.pack_start(mhz)

            self.mode_dd = Gtk.DropDown.new_from_strings(MODES)
            self.mode_dd.set_selected(MODES.index(self.engine.mode))
            self.mode_dd.set_tooltip_text("Modulering")
            self.mode_dd.connect("notify::selected", self._on_mode_dd)
            hb.pack_start(self.mode_dd)

            box = Gtk.Box(spacing=8)
            self.level_bar = Gtk.LevelBar(min_value=0, max_value=120)
            self.level_bar.set_size_request(160, -1)
            self.level_bar.set_valign(Gtk.Align.CENTER)
            self.level_lbl = Gtk.Label(label="– dB")
            self.level_lbl.add_css_class("monospace")
            self.level_lbl.set_width_chars(16)
            self.sq_lbl = Gtk.Label(label="")
            box.append(self.level_bar)
            box.append(self.level_lbl)
            box.append(self.sq_lbl)
            hb.set_title_widget(box)

            self.btn_skip = Gtk.Button(label="⏭ Hoppa över")
            self.btn_skip.set_tooltip_text("Spärra den här frekvensen och fortsätt skanna")
            self.btn_skip.connect("clicked", lambda *_: self.scanner.skip())
            self.btn_skip.set_visible(False)
            hb.pack_end(self.btn_skip)
            self.scan_lbl = Gtk.Label(label="")
            self.scan_lbl.add_css_class("accent")
            hb.pack_end(self.scan_lbl)
            self.btn_rec = Gtk.ToggleButton(label="● Spela in")
            self.btn_rec.set_tooltip_text("Spela in ljudet till en WAV-fil")
            self.btn_rec.connect("toggled", self._on_rec_toggled)
            hb.pack_end(self.btn_rec)
            return hb

        def _section(self, title, desc=None):
            g = Adw.PreferencesGroup(title=title)
            if desc:
                g.set_description(desc)
            return g

        def _spin(self, group, title, lo, hi, step, value, digits, cb, subtitle=None):
            r = Adw.SpinRow.new_with_range(lo, hi, step)
            r.set_title(title)
            if subtitle:
                r.set_subtitle(subtitle)
            r.set_digits(digits)
            r.set_value(value)
            r.connect("notify::value", lambda row, _p: cb(row.get_value()))
            group.add(r)
            return r

        def _combo(self, group, title, items, sel, cb):
            r = Adw.ComboRow(title=title)
            r.set_model(Gtk.StringList.new(items))
            r.set_selected(sel)
            r.connect("notify::selected", lambda row, _p: cb(row.get_selected()))
            group.add(r)
            return r

        def _switch(self, group, title, active, cb, subtitle=None):
            r = Adw.SwitchRow(title=title)
            if subtitle:
                r.set_subtitle(subtitle)
            r.set_active(active)
            r.connect("notify::active", lambda row, _p: cb(row.get_active()))
            group.add(r)
            return r

        def _button(self, group, title, cb):
            if hasattr(Adw, "ButtonRow"):                 # libadwaita 1.6+
                r = Adw.ButtonRow(title=title)
            else:                                         # libadwaita 1.5 (Ubuntu 24.04, Linux Mint 22)
                r = Adw.ActionRow(title=title, activatable=True)
                r.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            r.connect("activated", lambda *_: cb())
            group.add(r)
            return r

        def _build_sidebar(self):
            c = self.conf
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_start(12)
            box.set_margin_end(12)

            g = self._section("Mottagare")
            self._combo(g, "Källa", SOURCE_NAMES, SOURCES.index(c["source"]) if c["source"] in SOURCES else 0,
                        self._on_source)
            self.tcp_row = Adw.EntryRow(title="rtl_tcp-adress (värd:port)")
            self.tcp_row.set_text(c["tcp_addr"])
            self.tcp_row.set_show_apply_button(True)
            self.tcp_row.connect("apply", lambda r: self._set_conf("tcp_addr", r.get_text().strip()))
            g.add(self.tcp_row)
            self.hw_rows = [self._switch(g, "Automatisk förstärkning", c["auto_gain"], self._on_auto_gain)]
            self.gain_row = self._spin(g, "Förstärkning (dB)", 0, 49.6, 0.5, c["gain"], 1, self._on_gain)
            self.gain_row.set_sensitive(not c["auto_gain"])
            self.ppm_row = self._spin(g, "Frekvenskorrigering (ppm)", -200, 200, 1, c["ppm"], 0, self._on_ppm)
            self.hw_rows += [self.gain_row, self.ppm_row,
                             self._button(g, "Kalibrera frekvens (ppm) …", self._open_calibration),
                             self._combo(g, "Direktsampling (HF under 28 MHz)",
                                         ["Av", "I-gren", "Q-gren (RTL-SDR v3)"], c["direct"], self._on_direct),
                             self._switch(g, "Bias-T (matar antennförstärkare)", c["biast"], self._on_biast)]
            box.append(g)

            g = self.play_group = self._section("Uppspelning av IQ-fil",
                                                "SigMF, WAV (SDR++/SDR#), rtl_sdr .cu8, .cs8, .cs16, .cf32/.cfile")
            self.play_file_row = Adw.ActionRow(title="Ingen fil vald")
            self.play_file_row.set_subtitle_lines(3)
            b = Gtk.Button(label="Välj …", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: self._choose_play_file())
            self.play_file_row.add_suffix(b)
            g.add(self.play_file_row)
            pos_row = Adw.ActionRow(title="Position")
            self.play_pause_btn = Gtk.Button(icon_name="media-playback-pause-symbolic", valign=Gtk.Align.CENTER,
                                             tooltip_text="Paus / fortsätt")
            self.play_pause_btn.connect("clicked", lambda *_: self._toggle_play_pause())
            pos_row.add_prefix(self.play_pause_btn)
            self.play_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.1)
            self.play_scale.set_draw_value(False)
            self.play_scale.set_hexpand(True)
            self.play_scale.set_valign(Gtk.Align.CENTER)
            self.play_scale.set_size_request(130, -1)
            self.play_scale.connect("change-value", self._on_play_seek)
            pos_row.add_suffix(self.play_scale)
            self.play_pos_lbl = Gtk.Label(label="0:00 / 0:00", valign=Gtk.Align.CENTER)
            self.play_pos_lbl.add_css_class("numeric")
            pos_row.add_suffix(self.play_pos_lbl)
            pos_row.set_title("")
            g.add(pos_row)
            self._combo(g, "Hastighet", ["1× (realtid)", "2×", "4×", "8×", "Så fort det går"], c["play_speed"],
                        self._on_play_speed)
            self._switch(g, "Spela i slinga", c["play_loop"], self._on_play_loop)
            self._button(g, "Gå till filens mittfrekvens", self._play_goto_center)
            box.append(g)
            self._update_play_info()
            self._show_source_rows(c["source"])

            g = self._section("Demodulator")
            self.bw_row = self._spin(g, "Bandbredd (Hz)", 100, 250000, 100, self.engine.bw, 0, self._on_bw)
            self.sq_row = self._spin(g, "Squelch (dB)", -150, 0, 1, c["squelch"], 0, self._on_squelch,
                                     "-150 = alltid öppen")
            self._spin(g, "Volym (%)", 0, 300, 5, c["volume"], 0, self._on_volume)
            self._combo(g, "Frekvenssteg", STEP_NAMES, c["step_idx"], lambda i: self._set_conf("step_idx", i))
            box.append(g)

            g = self._section("Visning")
            self.dbmin_row = self._spin(g, "Min nivå (dB)", -160, 0, 1, c["db_min"], 0,
                                        lambda v: self._set_conf("db_min", v))
            self.dbmax_row = self._spin(g, "Max nivå (dB)", -150, 20, 1, c["db_max"], 0,
                                        lambda v: self._set_conf("db_max", v))
            self._button(g, "Automatisk nivå", self.auto_level)
            self.zoom_row = self._spin(g, "Zoom", 1, 32, 1, c["zoom"], 0, lambda v: self.set_zoom(int(v)),
                                       "Ctrl + mushjul i vattenfallet")
            cmaps = list(COLORMAPS)
            self._combo(g, "Tema", ["Mörkt", "Ljust", "Följ systemet"], c.get("theme", 0), self._on_theme)
            skins = list(SKINS)
            self._combo(g, "Utseende", [SKINS[k]["namn"] for k in skins],
                        skins.index(c.get("skin")) if c.get("skin") in skins else 0, self._on_skin)
            self._combo(g, "Färgskala", cmaps, cmaps.index(c["colormap"]) if c["colormap"] in cmaps else 0,
                        self._on_colormap)
            box.append(g)

            g = self._section("Skanner")
            self._combo(g, "Skanna", ["Bokmärken", "Frekvensintervall"], c["scan_mode"],
                        lambda i: self._set_conf("scan_mode", i))
            for key, title in (("scan_start", "Start (MHz)"), ("scan_stop", "Stopp (MHz)")):
                r = Adw.EntryRow(title=title)
                r.set_text(str(c[key]))
                r.connect("changed", lambda row, key=key: self._set_conf(key, row.get_text()))
                g.add(r)
            self._spin(g, "Kanalsteg (kHz)", 0.1, 1000, 0.5, c["scan_step_khz"], 2,
                       lambda v: self._set_conf("scan_step_khz", v))
            self._spin(g, "Väntetid efter signal (s)", 0, 60, 0.5, c["scan_hold"], 1,
                       lambda v: self._set_conf("scan_hold", v))
            self._spin(g, "Tid per steg (ms)", 100, 5000, 50, c["scan_dwell_ms"], 0,
                       lambda v: self._set_conf("scan_dwell_ms", int(v)))
            self._spin(g, "Max lyssningstid (s)", 0, 600, 5, c.get("scan_max_listen", 0), 0,
                       lambda v: self._set_conf("scan_max_listen", v), "0 = stanna så länge signalen finns")
            self.scan_btn = self._button(g, "Starta skanning", self._toggle_scan)
            self._switch(g, "Hoppa över omodulerade bärvågor", c.get("scan_skip_carriers", True),
                         lambda v: self._set_conf("scan_skip_carriers", v),
                         "Störningar från dongeln/datorn (gäller AM och NFM)")
            self.sugg_row = Adw.ActionRow(title="Hittade störningar", subtitle="inga än")
            self.sugg_btn = Gtk.Button(label="Spärra", valign=Gtk.Align.CENTER)
            self.sugg_btn.set_tooltip_text("Spara de hittade störningarna i spärrlistan permanent")
            self.sugg_btn.connect("clicked", lambda *_: self._lock_suggested())
            self.sugg_row.add_suffix(self.sugg_btn)
            g.add(self.sugg_row)
            self._button(g, "Rensa spärrade frekvenser", self._clear_lockout)
            box.append(g)

            g = self._section("Signallogg", "Hittar signaler automatiskt i hela bandet")
            self._switch(g, "Logga signaler", c["det_enabled"], self._on_det_enabled)
            self._spin(g, "Tröskel över brus (dB)", 3, 40, 1, c["det_thresh"], 0, self._on_det_thresh)
            box.append(g)

            g = self._section("Avkodare", "Avkodade meddelanden visas i fliken Avkodat. Fönstret är ca "
                              "1,2 MHz brett, så välj ett band i taget med knapparna.")
            self._switch(g, "ACARS (flygplan, 129–137 MHz)", c.get("acars", False),
                         lambda v: self._on_decoder("ACARS", v), "AM/MSK 2400 – alla ACARS-kanaler i fönstret")
            self._switch(g, "AIS (fartyg, 161,975/162,025)", c.get("ais", False),
                         lambda v: self._on_decoder("AIS", v), "GMSK 9600 – båda AIS-kanalerna")
            self._switch(g, "APRS (144,800 MHz)", c.get("aprs", False),
                         lambda v: self._on_decoder("APRS", v), "AFSK 1200 – positioner och meddelanden")
            self.acars_row = Adw.ActionRow(title="Status", subtitle="av")
            g.add(self.acars_row)
            self._button(g, "Gå till ACARS (131,7 MHz)", lambda: self._goto_decoder("ACARS"))
            self._button(g, "Gå till AIS (162 MHz)", lambda: self._goto_decoder("AIS"))
            self._button(g, "Gå till APRS (144,800 MHz)", lambda: self._goto_decoder("APRS"))
            self._button(g, "📊 Aktivitetskarta (värmekarta över ett band)", self._open_activity)
            self._button(g, "🚢 Visa fartyg på karta (AIS)", self._open_ship_map)
            self._button(g, "✈ Öppna Flygradar (ADS-B, 1090 MHz)", self._open_radar)
            box.append(g)

            g = self._section("Inspelning", f"Sparas i {rec_dir()}")
            self._switch(g, "Spela in automatiskt vid signal", c["auto_rec"], self._on_auto_rec)
            self._combo(g, "IQ-format", [n for n, _k in IQ_FORMATS], c["iq_format"],
                        lambda i: self._set_conf("iq_format", i))
            self.iq_btn = self._button(g, "Starta IQ-inspelning", self._toggle_iq)
            self._button(g, "Öppna inspelningsmappen", lambda: self._open_path(rec_dir()))
            box.append(g)

            g = self._section("Webbgränssnitt", "Lyssna och styr från en webbläsare")
            self._switch(g, "Aktivera webbgränssnitt", c["web_enabled"], self._on_web_enabled)
            self._spin(g, "Port", 1024, 65535, 1, c["web_port"], 0, lambda v: self._set_conf("web_port", int(v)))
            self._switch(g, "Tillåt andra datorer i nätverket", c["web_public"],
                         lambda v: self._set_conf("web_public", v), "Av = bara den här datorn")
            self.web_row = Adw.ActionRow(title="Adress", subtitle="avstängt")
            self.web_row.set_subtitle_selectable(True)
            g.add(self.web_row)
            box.append(g)

            g = self._section("Felsökning", f"Loggfil: {LOG_FILE}")
            self._button(g, "Kopiera felrapport", self.copy_report)
            self._button(g, "Öppna loggfilen", lambda: self._open_path(LOG_FILE))
            self._button(g, "Visa manualen", lambda: self._open_path(MANUAL_FILE))
            self.stats_row = Adw.ActionRow(title="Prestanda", subtitle="–")
            g.add(self.stats_row)
            box.append(g)

            def plain_text(w):
                # Äldre libadwaita tolkar rubriker som markup – "<" och "&" ska visas som text
                if isinstance(w, Adw.PreferencesRow):
                    w.set_use_markup(False)
                ch = w.get_first_child()
                while ch is not None:
                    plain_text(ch)
                    ch = ch.get_next_sibling()
            plain_text(box)

            sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.set_child(box)
            sw.set_size_request(280, -1)
            return sw

        def _build_bottom(self):
            nb = Gtk.Notebook()
            self.bottom_nb = nb

            # Signallogg
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            v.set_margin_top(6)
            v.set_margin_start(6)
            v.set_margin_end(6)
            tb = Gtk.Box(spacing=6)
            self.filter_entry = Gtk.SearchEntry(hexpand=True)
            self.filter_entry.set_placeholder_text("Filter, t.ex.  freq>144 freq<146  bw<25k  peak>-50  typ~NFM  aktiv")
            self.filter_entry.connect("search-changed", self._on_filter_changed)
            tb.append(self.filter_entry)
            for label, cb in (("Spara som bokmärke", self._log_to_bookmark), ("Exportera CSV", self._export_csv),
                              ("Rensa", self._clear_log)):
                b = Gtk.Button(label=label)
                b.connect("clicked", lambda *_, cb=cb: cb())
                tb.append(b)
            self.log_count = Gtk.Label(label="0 signaler")
            self.log_count.add_css_class("dim-label")
            tb.append(self.log_count)
            v.append(tb)
            self._filter = []
            cols = [
                ("Nr", lambda d: str(d["id"]), "id", False),
                ("Först sedd", lambda d: time.strftime("%H:%M:%S", time.localtime(d["first"])), "first", False),
                ("Frekvens (MHz)", lambda d: fmt_mhz(d["f"]), "f", False),
                ("Bandbredd", lambda d: fmt_bw(d["bw"]), "bw", False),
                ("Topp (dB)", lambda d: f"{d['peak']:.1f}", "peak", False),
                ("SNR (dB)", lambda d: f"{d['snr']:.1f}", "snr", False),
                ("Längd", lambda d: f"{d['dur']:.1f} s", "dur", False),
                ("Typ (gissning)", lambda d: d["cls"], "cls", False),
                ("Band", lambda d: d["band"], "band", True),
                ("Status", lambda d: d["status"], "status", False),
            ]
            (self.log_store, self.log_cv, self.log_sel, self.log_filter, lcols, sw) = make_table(cols, self._filter_func)
            self.log_cv.connect("activate", lambda cv, pos: self._tune_row(self.log_sel.get_item(pos)))
            self.log_cv.sort_by_column(lcols[0], Gtk.SortType.DESCENDING)
            v.append(sw)
            nb.append_page(v, Gtk.Label(label="Signallogg"))

            # Bokmärken
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            v.set_margin_top(6)
            v.set_margin_start(6)
            v.set_margin_end(6)
            tb = Gtk.Box(spacing=6)
            self.bm_name = Gtk.Entry(hexpand=True, placeholder_text="Namn på nytt bokmärke")
            self.bm_name.connect("activate", lambda *_: self._add_bookmark())
            tb.append(self.bm_name)
            for label, cb in (("Lägg till aktuell frekvens", self._add_bookmark),
                              ("Ta bort markerat", self._del_bookmark)):
                b = Gtk.Button(label=label)
                b.connect("clicked", lambda *_, cb=cb: cb())
                tb.append(b)
            v.append(tb)
            cols = [
                ("Namn", lambda d: d["name"], "name", True),
                ("Frekvens (MHz)", lambda d: fmt_freq(d["freq"]), "freq", False),
                ("Modulering", lambda d: d.get("mode", ""), "mode", False),
                ("Band", lambda d: band_name(d["freq"]), "freq", False),
            ]
            (self.bm_store, self.bm_cv, self.bm_sel, _f, _c, sw) = make_table(cols)
            self.bm_cv.connect("activate", lambda cv, pos: self._tune_row(self.bm_sel.get_item(pos)))
            v.append(sw)
            nb.append_page(v, Gtk.Label(label="Bokmärken"))

            # Avkodat
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            v.set_margin_top(6)
            v.set_margin_start(6)
            v.set_margin_end(6)
            tb = Gtk.Box(spacing=6)
            self.dec_filter = Gtk.SearchEntry(hexpand=True)
            self.dec_filter.set_placeholder_text("Sök i avkodade meddelanden (registrering, MMSI, anropssignal, text …)")
            self.dec_filter.connect("search-changed", lambda *_: self.dec_cfilter.changed(Gtk.FilterChange.DIFFERENT))
            tb.append(self.dec_filter)
            for label, cb in (("Exportera CSV", self._export_decoded), ("Rensa", self._clear_decoded)):
                b = Gtk.Button(label=label)
                b.connect("clicked", lambda *_, cb=cb: cb())
                tb.append(b)
            self.dec_count = Gtk.Label(label="0 meddelanden")
            self.dec_count.add_css_class("dim-label")
            tb.append(self.dec_count)
            v.append(tb)
            cols = [
                ("Tid", lambda d: time.strftime("%H:%M:%S", time.localtime(d["time"])), "time", False),
                ("Frekvens", lambda d: fmt_mhz(d["freq"]), "freq", False),
                ("Typ", lambda d: d["type"], "type", False),
                ("Avsändare", lambda d: d["src"], "src", False),
                ("Namn / flight / väg", lambda d: d["name"], "name", False),
                ("Kategori", lambda d: d["kind"], "kind", False),
                ("Position", lambda d: f"{d['pos'][0]:.4f}, {d['pos'][1]:.4f}" if d.get("pos") else "", "pos", False),
                ("Text", lambda d: d["text"][:200], "text", True),
            ]
            (self.dec_store, self.dec_cv, self.dec_sel, self.dec_cfilter, dcols, sw) = make_table(
                cols, self._dec_filter_func)
            self.dec_cv.connect("activate", lambda cv, pos: self._show_decoded(self.dec_sel.get_item(pos)))
            self.dec_cv.sort_by_column(dcols[0], Gtk.SortType.DESCENDING)
            v.append(sw)
            self.dec_tab_label = Gtk.Label(label="Avkodat")
            nb.append_page(v, self.dec_tab_label)

            # Signalanalys
            self.an_panel = AnalysPanel(self)
            self.an_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.an_holder.append(self.an_panel)
            self.an_placeholder = Adw.StatusPage(title="Signalanalysen visas i ett eget fönster",
                                                 icon_name="window-new-symbolic")
            bb = Gtk.Button(label="Hämta tillbaka hit", halign=Gtk.Align.CENTER)
            bb.add_css_class("pill")
            bb.connect("clicked", lambda *_: self.toggle_analys_window())
            self.an_placeholder.set_child(bb)
            self.an_tab_label = Gtk.Label(label="Signalanalys")
            self.an_page = nb.append_page(self.an_holder, self.an_tab_label)
            return nb

        # ── signalanalys ──
        def skin(self):
            return SKINS.get(self.conf.get("skin", "spanare"), SKINS["spanare"])

        def analys_shown(self):
            return self.an_win is not None or self.bottom_nb.get_current_page() == self.an_page

        def carrier_at(self, f, tol=None):
            """Detekterad bärvåg (eller tydlig topp i spektrumet) nära f. Returnerar (mitt, bandbredd) eller None."""
            lo, span = self.view.view()
            hz_px = span / max(1, self.view.get_width())
            tol = tol or max(hz_px * 6, 300)
            best = None
            for t in self.active_tracks:
                if t["f"] - t["bw"] / 2 - tol <= f <= t["f"] + t["bw"] / 2 + tol:
                    dd = abs(t["f"] - f)
                    if best is None or dd < best[2]:
                        best = (t["f"], max(t["bw"], 300.0), dd)
            if best:
                return best[0], best[1]
            disp = self.view.disp
            if disp is None:
                return None
            c = self.wf_center
            binhz = self.view.wf_span / FFT_N
            k = int((f - c) / binhz + FFT_N / 2)
            r = max(2, int(tol / binhz))
            k0, k1 = max(0, k - r), min(FFT_N, k + r + 1)
            if k1 - k0 < 3:
                return None
            kp = k0 + int(np.argmax(disp[k0:k1]))
            floor = float(np.median(disp))
            pk = float(disp[kp])
            if pk < floor + 8:
                return None
            thr = max(floor + 3, pk - 25)
            a = kp
            while a > 0 and disp[a - 1] > thr:
                a -= 1
            b = kp
            while b < FFT_N - 1 and disp[b + 1] > thr:
                b += 1
            kk = np.arange(a, b + 1)
            pw = 10 ** (disp[a:b + 1] / 10)
            fcen = c + float(((kk - FFT_N / 2) * binhz * pw).sum() / pw.sum())
            return fcen, max((b - a + 1) * binhz, 300.0)

        def analyse(self, f, bw, show=True):
            if self.an_panel.follow.get_active():
                self.an_panel.follow.set_active(False)
            self.an_panel.set_target(f, bw)
            if show and self.an_win is None and self.bottom_nb.get_current_page() != self.an_page:
                self.bottom_nb.set_current_page(self.an_page)
            if show and self.an_win is None:
                rp = self.right_paned
                H = rp.get_height()
                if H > 500 and H - rp.get_position() < 370:
                    rp.set_position(H - 370)       # ge analysen plats
            if not self.engine.running:
                self.toast("Starta mottagningen för att analysera signalen")
            self.view.queue_draw()

        def toggle_analys_window(self):
            p = self.an_panel
            self.set_focus(None)
            if self.an_win is not None:
                self.an_win.set_focus(None)
            if self.an_win is None:
                self.an_holder.remove(p)
                self.an_holder.append(self.an_placeholder)
                win = Adw.Window(title="Signalanalys – Radiospanaren", transient_for=self,
                                 default_width=self.conf.get("an_win", [760, 520])[0],
                                 default_height=self.conf.get("an_win", [760, 520])[1])
                tv = Adw.ToolbarView()
                tv.add_top_bar(Adw.HeaderBar())
                tv.set_content(p)
                win.set_content(tv)
                win.connect("close-request", lambda *_: self.toggle_analys_window() or True)
                self.an_win = win
                p.detach_btn.set_icon_name("view-restore-symbolic")
                p.detach_btn.set_tooltip_text("Lägg tillbaka analysen som flik")
                win.present()
            else:
                win = self.an_win
                self.conf["an_win"] = [win.get_width(), win.get_height()]
                win.get_content().set_content(None)
                self.an_win = None
                self.an_holder.remove(self.an_placeholder)
                self.an_holder.append(p)
                p.detach_btn.set_icon_name("window-new-symbolic")
                p.detach_btn.set_tooltip_text("Visa analysen i ett eget fönster")
                win.destroy()
                self.bottom_nb.set_current_page(self.an_page)

        # ── frekvens / läge ──
        def step(self):
            s = STEPS[self.conf["step_idx"]] if 0 <= self.conf["step_idx"] < len(STEPS) else 0
            return s or DEFAULT_STEP[self.engine.mode]

        def tune(self, freq, mode=None, keep_center=False):
            e = self.engine
            freq = float(max(1e4, freq))
            playing = e.running and getattr(e.source, "fixed_center", False)
            if playing:
                pass                                         # uppspelning: direktsampling spelar ingen roll
            elif self.conf.get("direct") and freq > 28.8e6 >= self.freq:
                self.toast("Direktsampling är på – den tar bara emot under 28,8 MHz. Stäng av den för VHF/UHF.")
            elif not self.conf.get("direct") and freq < 24e6 <= self.freq:
                self.toast("Under 24 MHz behövs direktsampling (Mottagare → Direktsampling → Q-gren)")
            if mode and mode in MODES and mode != e.mode:
                self.set_mode(mode)
            d = freq - e.center
            ok_offset = abs(d) <= SR * 0.42 and abs(d) >= e.bw / 2 + 3000
            if e.running and getattr(e.source, "fixed_center", False):
                if abs(d) > SR * 0.48:                      # filen styr mitten – kan inte stämma utanför den
                    self.toast(f"Utanför inspelningen ({(e.center - SR / 2) / 1e6:.3f}–"
                               f"{(e.center + SR / 2) / 1e6:.3f} MHz)")
                    freq = e.center + math.copysign(SR * 0.48, d)
                    d = freq - e.center
                ok_offset = True
            if not (ok_offset or keep_center):
                newc = freq + SR * 0.15
                e.set_center(newc)
                d = freq - newc
            e.offset = d
            e.reset_squelch()
            self.freq = freq
            self.conf["freq"] = freq
            self._update_freq_label()
            self.view.queue_draw()

        def remote_tune(self, f, m):
            GLib.idle_add(lambda: self.tune(f, m) or False)

        def set_center_keep(self, c):
            e = self.engine
            e.set_center(c)
            c = e.center                                     # oförändrad vid uppspelning
            d = self.freq - c
            if abs(d) > SR * 0.45:
                self.freq = c + math.copysign(SR * 0.45, d)
                d = self.freq - c
            e.offset = d
            self._update_freq_label()

        def set_mode(self, mode):
            e = self.engine
            e.mode = mode
            e.bw = DEFAULT_BW[mode]
            self.conf["mode"] = mode
            self.conf["bw"] = None
            self._updating = True
            self.mode_dd.set_selected(MODES.index(mode))
            self.bw_row.set_value(e.bw)
            self._updating = False
            self.view.queue_draw()

        def set_zoom(self, z):
            if int(z) != self.conf["zoom"] and self.engine.running:
                self._relevel_at = self.engine.stats["blocks"] + 8   # nytt brusgolv vid ny upplösning
            self.conf["zoom"] = max(1, int(z))
            if int(self.zoom_row.get_value()) != self.conf["zoom"]:
                self.zoom_row.set_value(self.conf["zoom"])
            self.view.queue_draw()

        def auto_level(self):
            if self.latest_db is None:
                self.toast("Ingen signal ännu – starta mottagningen först")
                return
            src = self.view.disp if self.view.disp is not None else self.latest_db
            floor = float(np.median(src))
            self.dbmin_row.set_value(round(floor - 6))
            self.dbmax_row.set_value(round(floor + 40))

        def _update_freq_label(self):
            if not self.freq_entry.has_focus():
                self.freq_entry.set_text(fmt_freq(self.freq))

        # ── händelser ──
        def _set_conf(self, k, v):
            self.conf[k] = v
            self.view.queue_draw()

        def _on_freq_entry(self, entry):
            try:
                f = parse_freq(entry.get_text())
            except ValueError as ex:
                self.toast(str(ex))
                return
            self.tune(f)
            self.view.grab_focus()
            self.freq_entry.set_text(fmt_freq(self.freq))

        def _on_mode_dd(self, dd, _p):
            if self._updating:
                return
            self.set_mode(MODES[dd.get_selected()])

        def _on_bw(self, v):
            if self._updating:
                return
            self.engine.bw = float(v)
            self.conf["bw"] = float(v)
            self.view.queue_draw()

        def _on_squelch(self, v):
            self.engine.squelch = v
            self.conf["squelch"] = v

        def _on_volume(self, v):
            self.engine.volume = v / 100
            self.conf["volume"] = v

        def _on_source(self, i):
            self.conf["source"] = SOURCES[i]
            self._show_source_rows(SOURCES[i])
            if self.engine.running:
                self.toast("Ny källa används nästa gång du startar")
            elif SOURCES[i] == "file" and not self.conf.get("play_file"):
                self._choose_play_file()

        def _show_source_rows(self, src):
            self.play_group.set_visible(src == "file")
            self.tcp_row.set_visible(src == "tcp")
            for r in self.hw_rows:
                r.set_visible(src != "file")

        # ── uppspelning av IQ-fil ──
        def _playing(self):
            s = self.engine.source
            return s if (self.engine.running and isinstance(s, iqfil.FileSource)) else None

        def _choose_play_file(self):
            dlg = Gtk.FileDialog(title="Välj IQ-fil att spela upp")
            flt = Gtk.FileFilter()
            flt.set_name("IQ-filer")
            for pat in ("*.sigmf-meta", "*.sigmf-data", "*.wav", "*.WAV", "*.cu8", "*.raw", "*.bin", "*.iq",
                        "*.cs8", "*.ci8", "*.cs16", "*.ci16", "*.cf32", "*.cfile", "*.fc32"):
                flt.add_pattern(pat)
            alla = Gtk.FileFilter()
            alla.set_name("Alla filer")
            alla.add_pattern("*")
            fl = Gio.ListStore.new(Gtk.FileFilter)
            fl.append(flt)
            fl.append(alla)
            dlg.set_filters(fl)
            dlg.set_default_filter(flt)
            cur = self.conf.get("play_file")
            try:
                dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(cur) if cur and os.path.exists(cur)
                                                             else rec_dir()))
            except Exception:
                pass

            def done(d, res):
                try:
                    f = d.open_finish(res)
                except GLib.Error:
                    return
                self._set_play_file(f.get_path())
            dlg.open(self, None, done)

        def _set_play_file(self, path, ask=True):
            try:
                r = iqfil.IQReader(path)
            except Exception as ex:
                log.error("Kunde inte läsa IQ-filen %s: %s", path, ex)
                self.toast(f"Kunde inte läsa filen: {ex}", timeout=8)
                return
            ov = self.conf.setdefault("play_override", {}).get(path, {})
            if ask and (not (r.rate or ov.get("rate")) or not (r.center_at(0) or ov.get("center"))):
                self._ask_play_params(path, r, ov)
                return
            self.conf["play_file"] = path
            log.info("IQ-fil vald: %s (%s, %s S/s, %.1f s)", path, r.info, r.rate or ov.get("rate"), r.seconds())
            self._update_play_info()
            if self.engine.running and isinstance(self.engine.source, iqfil.FileSource):
                self.btn_start.set_active(False)
                self.btn_start.set_active(True)

        def _ask_play_params(self, path, r, ov):
            """Filen saknar takt och/eller mittfrekvens (vanligt för rå .cu8/.cf32) – fråga användaren."""
            gf, gr = iqfil.guess_from_name(path)
            dlg = Adw.MessageDialog(transient_for=self, heading="Uppgifter saknas för IQ-filen",
                                    body=f"{os.path.basename(path)}\n\nRåa IQ-filer innehåller inte samplingstakt "
                                         "och mittfrekvens. Ange dem (rtl_sdr använder ofta 2048000 eller 2400000 S/s).")
            lb = Gtk.ListBox()
            lb.add_css_class("boxed-list")
            rate = Adw.EntryRow(title="Samplingstakt (S/s)")
            rate.set_text(str(int(ov.get("rate") or r.rate or gr or 2048000)))
            cen = Adw.EntryRow(title="Mittfrekvens (MHz)")
            c0 = ov.get("center") or r.center_at(0) or gf
            cen.set_text(f"{c0 / 1e6:.6f}" if c0 else "")
            lb.append(rate)
            lb.append(cen)
            dlg.set_extra_child(lb)
            dlg.add_response("cancel", "Avbryt")
            dlg.add_response("ok", "Använd")
            dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("ok")

            def resp(_d, rid):
                if rid != "ok":
                    return
                try:
                    rv = float(rate.get_text().replace(",", ".").replace(" ", ""))
                    cv = parse_freq(cen.get_text()) if cen.get_text().strip() else 0.0
                    if not 1000 <= rv <= 100e6:
                        raise ValueError("orimlig samplingstakt")
                except ValueError as ex:
                    self.toast(f"Ogiltigt värde: {ex}")
                    return
                self.conf["play_override"][path] = {"rate": rv, "center": cv or None}
                self._set_play_file(path, ask=False)
            dlg.connect("response", resp)
            dlg.present()

        def _update_play_info(self):
            path = self.conf.get("play_file") or ""
            if not path:
                self.play_file_row.set_title("Ingen fil vald")
                self.play_file_row.set_subtitle("Spela in med ”Starta IQ-inspelning” eller välj en fil från "
                                                "t.ex. SDR++, SDR#, gqrx eller rtl_sdr")
                return
            self.play_file_row.set_title(GLib.markup_escape_text(os.path.basename(path)))
            try:
                ov = self.conf.get("play_override", {}).get(path, {})
                r = iqfil.IQReader(path, rate=ov.get("rate"), center=ov.get("center"))
                c0 = r.center_at(0)
                caps = len(r.captures)
                sub = (f"{r.info}, {r.rate / 1e3:.1f} kS/s, " if r.rate else f"{r.info}, okänd takt, ") + \
                      (f"mitt {c0 / 1e6:.4f} MHz" if c0 else "okänd mittfrekvens") + \
                      (f" ({caps} frekvenser)" if caps > 1 else "") + \
                      (f", {self._fmt_t(r.seconds())}" if r.rate else "")
                if r.rate and abs(r.rate - SR) > 1 and r.rate < SR * 0.5:
                    sub += f"\nSmal inspelning – bara ±{r.rate / 2e3:.0f} kHz runt mitten har signal"
                self.play_file_row.set_subtitle(GLib.markup_escape_text(sub))
                self.play_scale.set_range(0, max(0.1, r.seconds()))
                self.play_pos_lbl.set_text(f"0:00 / {self._fmt_t(r.seconds())}")
            except Exception as ex:
                self.play_file_row.set_subtitle(GLib.markup_escape_text(f"Kan inte läsas: {ex}"))

        @staticmethod
        def _fmt_t(t):
            t = int(t)
            return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"

        def _toggle_play_pause(self):
            s = self._playing()
            if not s:
                self.toast("Starta uppspelningen med ▶ Starta")
                return
            s.paused = not s.paused
            self.play_pause_btn.set_icon_name("media-playback-start-symbolic" if s.paused
                                              else "media-playback-pause-symbolic")

        def _on_play_seek(self, _scale, _scroll, value):
            s = self._playing()
            if s:
                s.seek(value)
                self.engine.reset_squelch()
            return False

        def _on_play_speed(self, i):
            self.conf["play_speed"] = i
            s = self._playing()
            if s:
                s.speed = {0: 1.0, 1: 2.0, 2: 4.0, 3: 8.0, 4: 0.0}.get(i, 1.0)
                s.t_next = time.monotonic()

        def _on_play_loop(self, v):
            self.conf["play_loop"] = v
            s = self._playing()
            if s:
                s.loop = v

        def _play_goto_center(self):
            s = self._playing()
            c = s.center if s else None
            if c is None:
                try:
                    c = open_iq_reader(self.conf).center_at(0)
                except SourceError as ex:
                    self.toast(str(ex))
                    return
            if c:
                self.tune(c - SR * 0.15 if (not s or (s.r.rate or 0) >= SR * 0.6) else c + 1000)

        def _play_tick(self):
            s = self._playing()
            if not s:
                return
            t, tot = s.position(), s.r.seconds()
            self.play_scale.set_value(t)
            self.play_pos_lbl.set_text(f"{self._fmt_t(t)} / {self._fmt_t(tot)}")

        def _src_set(self, k, v):
            if self.engine.running and self.engine.source:
                self.engine.source.set(k, v)

        def _on_auto_gain(self, v):
            self.conf["auto_gain"] = v
            self.gain_row.set_sensitive(not v)
            self._src_set("gain", None if v else self.conf["gain"])

        def _on_gain(self, v):
            self.conf["gain"] = v
            if not self.conf["auto_gain"]:
                self._src_set("gain", v)

        def _on_ppm(self, v):
            self.conf["ppm"] = int(v)
            self._src_set("ppm", int(v))

        # ── frekvenskalibrering ──
        def _calib_measure(self, true_hz):
            """Mäter var en bärvåg på true_hz faktiskt hamnar och räknar ut korrigeringen.
            Returnerar dict, "range" (utanför fönstret) eller None (ingen data än)."""
            e = self.engine
            avg, c, cnt = e.get_avg()
            if avg is None or c is None or cnt < 4:
                return None
            N = FFT_N
            k_exp = (true_hz - c) / BIN_HZ + N / 2
            if not (5 < k_exp < N - 5):
                return "range"
            win = 60
            k0 = int(max(1, k_exp - win))
            k1 = int(min(N - 1, k_exp + win))
            kp = k0 + int(np.argmax(avg[k0:k1]))
            floor = float(np.median(avg))
            snr = 10 * math.log10(avg[kp] / (floor + 1e-20) + 1e-20)
            d = 0.0
            if 0 < kp < N - 1:
                a = math.log(avg[kp - 1] + 1e-20)
                b = math.log(avg[kp] + 1e-20)
                cc = math.log(avg[kp + 1] + 1e-20)
                den = a - 2 * b + cc
                if den:
                    d = max(-1.0, min(1.0, 0.5 * (a - cc) / den))
            meas = c + (kp + d - N / 2) * BIN_HZ
            apparent_ppm = (meas - true_hz) / true_hz * 1e6
            return {"snr": snr, "offset": meas - true_hz, "apparent_ppm": apparent_ppm,
                    "suggested": self.conf["ppm"] - apparent_ppm}

        def _open_calibration(self):
            from collections import deque as _dq
            win = Adw.Window(title="Kalibrera frekvens", modal=True, transient_for=self, default_width=470)
            tv = Adw.ToolbarView()
            tv.add_top_bar(Adw.HeaderBar())
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_start(12)
            box.set_margin_end(12)
            info = Gtk.Label(wrap=True, xalign=0)
            info.set_markup("Ställ in mottagaren på en <b>känd, stabil bärvåg</b> – t.ex. en tidssignal "
                            "(RWM 4,996/9,996/14,996 MHz via direktsampling), en fyrsändare, en repeater "
                            "eller en mellanvågssändare. Skriv den exakta frekvensen och tryck Mät.")
            box.append(info)
            g = Adw.PreferencesGroup()
            fr = Adw.EntryRow(title="Känd frekvens (MHz)")
            fr.set_text(fmt_mhz(self.freq))
            g.add(fr)
            sig_row = Adw.ActionRow(title="Signal", subtitle="–")
            off_row = Adw.ActionRow(title="Uppmätt avvikelse", subtitle="–")
            sug_row = Adw.ActionRow(title="Föreslagen korrigering", subtitle="–")
            for r in (sig_row, off_row, sug_row):
                g.add(r)
            box.append(g)
            btns = Gtk.Box(spacing=8, halign=Gtk.Align.END)
            meas_btn = Gtk.ToggleButton(label="Mät")
            meas_btn.add_css_class("suggested-action")
            apply_btn = Gtk.Button(label="Använd")
            apply_btn.set_sensitive(False)
            btns.append(meas_btn)
            btns.append(apply_btn)
            box.append(btns)
            tv.set_content(box)
            win.set_content(tv)

            state = {"timer": None, "hist": _dq(maxlen=10), "suggested": None, "true": None}

            def stop_timer():
                if state["timer"]:
                    GLib.source_remove(state["timer"])
                    state["timer"] = None

            def tick():
                r = self._calib_measure(state["true"])
                if r is None:
                    sig_row.set_subtitle("väntar på signal …")
                    return True
                if r == "range":
                    sig_row.set_subtitle("frekvensen är utanför mottagningsfönstret")
                    off_row.set_subtitle("–")
                    sug_row.set_subtitle("–")
                    apply_btn.set_sensitive(False)
                    return True
                sig_row.set_subtitle(f"{r['snr']:.0f} dB över brus"
                                     + ("" if r["snr"] >= 15 else "  (svag – sikta på minst 15 dB)"))
                state["hist"].append(r["apparent_ppm"])
                med_app = float(np.median(state["hist"]))
                sug = self.conf["ppm"] - med_app
                off_hz = med_app * state["true"] / 1e6
                off_row.set_subtitle(f"{off_hz:+.0f} Hz  ({med_app:+.1f} ppm)  – {len(state['hist'])} mätningar")
                sug_row.set_subtitle(f"{round(sug):+d} ppm")
                state["suggested"] = int(round(sug))
                apply_btn.set_sensitive(r["snr"] >= 10 and len(state["hist"]) >= 3)
                return True

            def on_meas(btn):
                if btn.get_active():
                    if not self.engine.running:
                        self.toast("Starta mottagningen först")
                        btn.set_active(False)
                        return
                    try:
                        state["true"] = parse_freq(fr.get_text())
                    except ValueError as ex:
                        self.toast(str(ex))
                        btn.set_active(False)
                        return
                    self.tune(state["true"])      # ställ mottagaren på frekvensen
                    state["hist"].clear()
                    btn.set_label("■ Mäter …")
                    fr.set_sensitive(False)
                    if state["timer"] is None:
                        state["timer"] = GLib.timeout_add(500, tick)
                else:
                    stop_timer()
                    btn.set_label("Mät")
                    fr.set_sensitive(True)

            def on_apply(_b):
                if state["suggested"] is None:
                    return
                v = max(-200, min(200, state["suggested"]))
                self.ppm_row.set_value(v)       # uppdaterar conf + source via _on_ppm
                log.info("Frekvenskalibrering: ppm satt till %+d (mot %.4f MHz)", v, state["true"] / 1e6)
                self.toast(f"Frekvenskorrigering satt till {v:+d} ppm")
                state["hist"].clear()           # mät om för att bekräfta ~0

            meas_btn.connect("toggled", on_meas)
            apply_btn.connect("clicked", on_apply)
            win.connect("close-request", lambda *_: (stop_timer(), False)[1])
            win.present()

        def _on_direct(self, i):
            self.conf["direct"] = i
            self._src_set("direct", i)
            if i and self.freq > 28.8e6:
                self.tune(7.074e6, "USB")

        def _on_biast(self, v):
            self.conf["biast"] = v
            self._src_set("biast", v)

        def _on_colormap(self, i):
            name = list(COLORMAPS)[i]
            self.conf["colormap"] = name
            self.view.lut = make_lut(name)
            self.view.queue_draw()

        def _on_theme(self, i):
            self.conf["theme"] = i
            apply_theme(i)
            apply_skin(self.conf.get("skin", "spanare"), i)

        def _on_skin(self, i):
            name = list(SKINS)[i]
            self.conf["skin"] = name
            apply_skin(name, self.conf.get("theme", 0))
            self.view.queue_draw()
            self.an_panel.mini.queue_draw()
            self.an_panel.diag.queue_draw()

        def _on_det_enabled(self, v):
            self.conf["det_enabled"] = v
            self.engine.detector.enabled = v

        def _on_det_thresh(self, v):
            self.conf["det_thresh"] = v
            self.engine.detector.thresh = v

        def _on_auto_rec(self, v):
            self.conf["auto_rec"] = v
            self.engine.auto_rec = v

        def _on_start_toggled(self, btn):
            e = self.engine
            if btn.get_active():
                if e.running:
                    return
                try:
                    src = make_source(self.conf)
                except SourceError as ex:
                    log.error("%s", ex)
                    self.toast(str(ex))
                    btn.set_active(False)
                    return
                src.set("freq", e.center)
                src.set("gain", None if self.conf["auto_gain"] else self.conf["gain"])
                if self.conf["ppm"]:
                    src.set("ppm", self.conf["ppm"])
                if self.conf["direct"]:
                    src.set("direct", self.conf["direct"])
                if self.conf["biast"]:
                    src.set("biast", True)
                if isinstance(src, iqfil.FileSource):
                    fc = src.center
                    e.center = fc
                    span = min(SR, src.r.rate or SR) * 0.45
                    if abs(self.freq - fc) > span:          # inställd frekvens ligger utanför filen
                        self.freq = fc - SR * 0.15 if span > SR * 0.3 else fc + 1000
                        self.conf["freq"] = self.freq
                    e.offset = self.freq - fc
                    e.reset_squelch()
                    self._update_freq_label()
                    self.play_pause_btn.set_icon_name("media-playback-pause-symbolic")
                self.audio.start()
                self._auto_level_pending = True
                e.start(src)
                btn.set_label("■ Stoppa")
                btn.remove_css_class("suggested-action")
                btn.add_css_class("destructive-action")
            else:
                self.scanner.stop()
                e.stop()
                self.audio.stop()
                btn.set_label("▶ Starta")
                btn.remove_css_class("destructive-action")
                btn.add_css_class("suggested-action")
            self.view.queue_draw()

        def _on_engine_error(self, msg):
            self.toast(msg, timeout=8)
            return False

        def _on_engine_stopped(self):
            if self.activity_active and self.activity_win:
                self.activity_win.btn.set_active(False)
            if self.btn_start.get_active():
                self.btn_start.set_active(False)
            self.btn_rec.set_active(False)
            self.iq_btn.set_title("Starta IQ-inspelning")
            return False

        def _on_engine_event(self, kind, val):
            if kind == "rec_auto_start":
                self.toast(f"Spelar in: {os.path.basename(val)}")
            elif kind == "play_end":
                self.toast("Uppspelningen är klar")
                if self.btn_start.get_active():
                    self.btn_start.set_active(False)
            elif kind == "iq_error":
                self.iq_btn.set_title("Starta IQ-inspelning")
                self.toast(f"IQ-inspelningen stoppades: {val}", timeout=8)
            return False

        def _on_rec_toggled(self, btn):
            e = self.engine
            if btn.get_active():
                if not e.running:
                    self.toast("Starta mottagningen först")
                    btn.set_active(False)
                    return
                path = os.path.join(rec_dir(), f"{datetime.datetime.now():%Y%m%d-%H%M%S}_"
                                               f"{self.freq / 1e6:.4f}MHz_{e.mode}.wav")
                try:
                    e.start_wav(path)
                except OSError as ex:
                    log.error("Kunde inte starta inspelning: %s", ex)
                    self.toast(f"Kunde inte spela in: {ex}")
                    btn.set_active(False)
                    return
                btn.set_label("■ Stoppa inspelning")
                btn.add_css_class("destructive-action")
            else:
                p = e.stop_wav()
                btn.set_label("● Spela in")
                btn.remove_css_class("destructive-action")
                if p:
                    self.toast(f"Sparad: {os.path.basename(p)}")

        def _toggle_iq(self):
            e = self.engine
            if e.rec_iq:
                p = e.stop_iq()
                self.iq_btn.set_title("Starta IQ-inspelning")
                self.toast(f"IQ sparad: {os.path.basename(p)}")
                return
            if not e.running:
                self.toast("Starta mottagningen först")
                return
            if isinstance(e.source, iqfil.FileSource):
                self.toast("Du spelar redan upp en IQ-fil – den behöver inte spelas in igen")
                return
            fmt = IQ_FORMATS[min(self.conf.get("iq_format", 0), len(IQ_FORMATS) - 1)][1]
            base = os.path.join(rec_dir(), f"{datetime.datetime.now():%Y%m%d-%H%M%S}_"
                                           f"{e.center / 1e6:.4f}MHz_{SR}sps")
            try:
                if fmt == "wav16":                          # SDR++-namn: baseband_<Hz>Hz_<tid>_<datum>.wav
                    w = iqfil.WavWriter(os.path.join(rec_dir(), "baseband"), SR, e.center)
                    rate_txt = "6 MB"
                else:
                    dt = "cu8" if fmt == "sigmf_cu8" else "cf32_le"
                    w = iqfil.SigMFWriter(base, SR, e.center, dt,
                                          description=f"{e.source.name}, inställd {self.freq / 1e6:.4f} MHz {e.mode}",
                                          extra={"core:hw": e.source.name})
                    rate_txt = "3 MB" if dt == "cu8" else "12 MB"
            except OSError as ex:
                log.error("Kunde inte starta IQ-inspelning: %s", ex)
                self.toast(f"Kunde inte spela in: {ex}")
                return
            e.start_iq(w)
            self.iq_btn.set_title("Stoppa IQ-inspelning")
            self.toast(f"IQ-inspelning: ca {rate_txt} per sekund")

        def _lock_suggested(self):
            if not self.suggested:
                self.toast("Inga hittade störningar att spärra")
                return
            self.conf.setdefault("lockout", []).extend(self.suggested)
            log.info("Spärrade %d störningar: %s", len(self.suggested),
                     ", ".join(fmt_mhz(f) for f in self.suggested))
            self.toast(f"{len(self.suggested)} störningar spärrade")
            self.suggested = []
            save_conf(self.conf)

        def _clear_lockout(self):
            n = len(self.conf.get("lockout", []))
            self.conf["lockout"] = []
            log.info("Spärrlistan rensad (%d frekvenser)", n)
            self.toast(f"{n} spärrade frekvenser borttagna")

        def _toggle_scan(self):
            if self.activity_active and self.activity_win:
                self.activity_win.btn.set_active(False)
            if self.scanner.active:
                self.scanner.stop()
                self.scan_btn.set_title("Starta skanning")
            elif self.scanner.start():
                self.scan_btn.set_title("Stoppa skanning")

        def _on_web_enabled(self, v):
            self.conf["web_enabled"] = v
            if v:
                self._start_web()
            elif self.web:
                self.web.stop()
                self.web = None
                self.web_row.set_subtitle("avstängt")

        def _start_web(self):
            if self.web:
                return False
            try:
                self.web = WebServer(self, int(self.conf["web_port"]), self.conf["web_public"])
                self.web_row.set_subtitle(self.web.url)
            except RuntimeError as ex:
                log.error("%s", ex)
                self.toast(str(ex))
            return False

        # ── signallogg ──
        def _parse_filter(self, text):
            terms = []
            for tok in text.split():
                m = re.match(r"^(freq|f|bw|peak|snr|dur|typ|class|band|status)(>=|<=|==|!=|>|<|~|=)(.+)$",
                             tok, re.I)
                if not m:
                    if tok.lower() in ("aktiv", "active"):
                        terms.append(("status", "==", "aktiv"))
                    else:
                        terms.append(("text", "~", tok.lower()))
                    continue
                k, op, v = m.group(1).lower(), m.group(2), m.group(3)
                try:
                    if k in ("freq", "f"):
                        terms.append(("f", op, parse_freq(v)))
                    elif k == "bw":
                        terms.append(("bw", op, parse_bw(v)))
                    elif k in ("peak", "snr", "dur"):
                        terms.append((k, op, float(v.rstrip("s"))))
                    else:
                        terms.append(({"typ": "cls", "class": "cls"}.get(k, k), op, v.lower()))
                except ValueError:
                    terms.append(("text", "~", tok.lower()))
            return terms

        def _filter_func(self, item, *_):
            d = item.data
            for k, op, v in self._filter:
                if k == "text":
                    hay = f"{fmt_mhz(d['f'])} {d['cls']} {d['band']} {d['status']}".lower()
                    if v not in hay:
                        return False
                    continue
                x = d.get(k)
                if isinstance(x, str):
                    x = x.lower()
                    ok = (v in x) if op == "~" else (x == v if op in ("==", "=") else x != v if op == "!=" else True)
                else:
                    ok = {">": x > v, "<": x < v, ">=": x >= v, "<=": x <= v, "==": abs(x - v) < 1e-6,
                          "=": abs(x - v) < 1e-6, "!=": x != v, "~": True}[op] if not isinstance(v, str) else True
                if not ok:
                    return False
            return True

        def _on_filter_changed(self, entry):
            self._filter = self._parse_filter(entry.get_text())
            self.log_filter.changed(Gtk.FilterChange.DIFFERENT)

        def _update_log(self):
            changes, active = self.engine.detector.pop_changes()
            self.active_tracks = active
            for t in changes:
                row = self.log_rows.get(t["id"])
                if row is None:
                    row = Row(t)
                    self.log_rows[t["id"]] = row
                    self.log_store.append(row)
                    if len(self.log_rows) > 5000:
                        old = self.log_store.get_item(0)
                        self.log_store.remove(0)
                        self.log_rows.pop(old.data["id"], None)
                else:
                    row.data = t
                    row.emit("updated")
            if changes and self._filter:
                self.log_filter.changed(Gtk.FilterChange.DIFFERENT)
            if changes:
                self.log_count.set_text(f"{len(self.log_rows)} signaler")

        def _clear_log(self):
            self.engine.detector.clear()
            self.log_store.remove_all()
            self.log_rows.clear()
            self.log_count.set_text("0 signaler")

        def _export_csv(self):
            dlg = Gtk.FileDialog(initial_name=f"signallogg_{datetime.datetime.now():%Y%m%d-%H%M}.csv")

            def done(d, res):
                try:
                    f = d.save_finish(res)
                except GLib.Error:
                    return
                path = f.get_path()
                try:
                    with open(path, "w", newline="", encoding="utf-8") as fh:
                        wr = csv.writer(fh)
                        wr.writerow(["nr", "forst_sedd", "frekvens_hz", "bandbredd_hz", "topp_db", "snr_db",
                                     "langd_s", "typ", "band", "status"])
                        for i in range(self.log_store.get_n_items()):
                            dd = self.log_store.get_item(i).data
                            wr.writerow([dd["id"], datetime.datetime.fromtimestamp(dd["first"]).isoformat(timespec="seconds"),
                                         round(dd["f"]), round(dd["bw"]), round(dd["peak"], 1), round(dd["snr"], 1),
                                         round(dd["dur"], 1), dd["cls"], dd["band"], dd["status"]])
                    self.toast(f"Exporterade till {path}")
                    log.info("Signallogg exporterad: %s", path)
                except OSError as ex:
                    log.error("CSV-export misslyckades: %s", ex)
                    self.toast(f"Kunde inte spara: {ex}")
            dlg.save(self, None, done)

        def _log_to_bookmark(self):
            item = self.log_sel.get_selected_item()
            if not item:
                self.toast("Markera en rad i signalloggen först")
                return
            d = item.data
            mode = "WFM" if d["bw"] > 60000 else ("NFM" if d["bw"] > 4000 else ("USB" if d["bw"] > 800 else "CW"))
            f = round(d["f"] / 500) * 500
            name = f"{d['cls']} {fmt_mhz(f)}"
            self.conf["bookmarks"].append({"name": name, "freq": f, "mode": mode})
            self._load_bookmarks()
            save_conf(self.conf)
            self.toast(f"Bokmärke sparat: {name}")

        def _tune_row(self, item):
            if not item:
                return
            d = item.data
            if "name" in d:
                self.tune(d["freq"], d.get("mode"))
            else:
                self.tune(d["f"])

        # ── avkodare ──
        DEC_TUNE = {"ACARS": (131.725e6, "AM"), "AIS": (161.975e6, "NFM"), "APRS": (144.8e6, "NFM")}

        def _on_decoder(self, kind, v):
            self.conf[kind.lower()] = v
            self.hub.enabled[kind] = v
            log.info("%s-avkodning %s", kind, "på" if v else "av")
            f, _m = self.DEC_TUNE[kind]
            if v and abs(self.engine.center - f) > SR * 0.44:
                self.toast(f"{kind} ligger utanför fönstret – tryck \"Gå till {kind}\"")

        def _open_activity(self):
            try:
                if self.activity_win is None:
                    self.activity_win = ActivityWindow(self)
                self.activity_win.present()
            except Exception as ex:
                log.exception("Kunde inte öppna aktivitetskartan")
                self.toast(f"Kunde inte öppna aktivitetskartan: {ex}")
                return
            if not self.engine.running:
                self.toast("Starta mottagningen, välj band och tryck ▶ Svep")

        def _open_ship_map(self):
            try:
                if self.ship_win is None:
                    self.ship_win = ShipWindow(self)
                self.ship_win.refresh()
                self.ship_win.present()
            except Exception as ex:
                log.exception("Kunde inte öppna fartygskartan")
                self.toast(f"Kunde inte öppna fartygskartan: {ex}")
                return
            if not self.hub.enabled["AIS"]:
                self.toast("AIS-avkodningen är av – tryck \"Gå till AIS\" i fartygsfönstret")

        def _ais_update(self, msg):
            v = msg.get("ais") or {}
            mmsi = msg["src"]
            now = msg.get("time", time.time())
            d = self.ships.get(mmsi)
            if d is None:
                d = {"mmsi": mmsi, "name": "", "call": "", "shiptype": None, "status": "", "nav": None,
                     "sog": None, "cog": None, "hdg": None, "pos": None, "dest": "", "first": now, "last": now,
                     "msgs": 0, "special": "", "alert": False}
                self.ships[mmsi] = d
            for k in ("call", "shiptype", "status", "nav", "sog", "cog", "hdg", "dest"):
                if k in v and v[k] not in (None, ""):
                    d[k] = v[k]
            if msg.get("name"):
                d["name"] = msg["name"]
            if msg.get("pos"):
                d["pos"] = tuple(msg["pos"])
            if v.get("base"):
                d["special"] = "Basstation"
            elif v.get("aton"):
                d["special"] = "Fyr/boj"
            d["last"] = now
            d["msgs"] += 1
            alert = mmsi.startswith("97") or v.get("nav") == 14
            if alert and not d["alert"]:
                self.toast(f"AIS-NÖDSÄNDARE: {mmsi} {d['name']} – se fartygskartan", 15)
                log.warning("AIS-nödsändare (SART/MOB/EPIRB): %s %s %s", mmsi, d["name"], d["pos"])
            d["alert"] = alert

        def _open_radar(self):
            """Startar Flygradar (adsb_karta.py) som eget program. Dongeln kan bara användas av ett program."""
            path = os.path.join(APP_DIR, "adsb_karta.py")
            if not os.path.exists(path):
                self.toast("Hittar inte adsb_karta.py – installera om programmet")
                log.error("Saknar %s", path)
                return
            src = self.conf["source"]
            if self.engine.running:
                if src in ("rtl",):
                    self.btn_start.set_active(False)       # släpp dongeln
                    self.toast("Mottagningen stoppad – Flygradar använder dongeln")
            if src == "file":
                src = "rtl"                                # Flygradar spelar inte upp IQ-filer
            cmd = [sys.executable, path, "--kalla", src, "--start"]
            if src == "tcp":
                cmd += ["--tcp", self.conf["tcp_addr"]]

            def launch():
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(STDERR_FILE, "ab"),
                                     start_new_session=True)
                    log.info("Startade Flygradar: %s", " ".join(cmd))
                except OSError as ex:
                    log.error("Kunde inte starta Flygradar: %s", ex)
                    self.toast(f"Kunde inte starta Flygradar: {ex}")
                return False
            GLib.timeout_add(700 if self.engine.running or self.engine.thread else 0, launch)

        def _goto_decoder(self, kind):
            f, m = self.DEC_TUNE[kind]
            if not self.hub.enabled[kind]:
                self.hub.enabled[kind] = True
                self.conf[kind.lower()] = True
                self.toast(f"{kind}-avkodning påslagen")
            if self.conf.get("direct"):
                self.toast("Stäng av direktsampling för VHF")
            self.tune(f, m)

        def _on_decoded(self, msg):
            if msg.get("type") == "AIS":
                self._ais_update(msg)
            self.dec_store.append(Row(msg))
            self.decoded_rows += 1
            self.dec_count.set_text(f"{self.decoded_rows} meddelanden")
            self.dec_tab_label.set_text(f"Avkodat ({self.decoded_rows})")
            try:
                with open(os.path.join(STATE_DIR, "avkodat.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            except OSError as ex:
                log.error("Kunde inte spara avkodat meddelande: %s", ex)
            return False

        def _dec_filter_func(self, item, *_):
            q = self.dec_filter.get_text().strip().lower()
            if not q:
                return True
            d = item.data
            return q in f"{d['src']} {d['name']} {d['kind']} {d['text']} {d['type']} {fmt_mhz(d['freq'])}".lower()

        def _show_decoded(self, item):
            if not item:
                return
            d = item.data
            lines = [f"{d['type']} på {fmt_mhz(d['freq'])} MHz, "
                     f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(d['time']))}", ""]
            lines += [f"{k}: {v}" for k, v in d.get("details", [])]
            if d.get("pos"):
                lines.append(f"Position: {d['pos'][0]:.5f}, {d['pos'][1]:.5f}")
            lines += [f"Nivå: {d['level']:.0f} dB över brus", "", d["text"] or "(ingen text)"]
            dlg = Adw.AlertDialog(heading="Avkodat meddelande", body="\n".join(lines))
            dlg.set_body_use_markup(False)
            if d.get("pos"):
                dlg.add_response("map", "Visa på karta")
            dlg.add_response("ok", "Stäng")
            dlg.set_default_response("ok")

            def resp(dl, r):
                if r == "map":
                    lat, lon = d["pos"]
                    uri = f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=12/{lat:.5f}/{lon:.5f}"
                    try:
                        Gio.AppInfo.launch_default_for_uri(uri, None)
                    except GLib.Error as ex:
                        log.error("Kunde inte öppna kartan: %s", ex)
            dlg.connect("response", resp)
            dlg.present(self)

        def _clear_decoded(self):
            self.dec_store.remove_all()
            self.decoded_rows = 0
            self.dec_count.set_text("0 meddelanden")
            self.dec_tab_label.set_text("Avkodat")

        def _export_decoded(self):
            dlg = Gtk.FileDialog(initial_name=f"avkodat_{datetime.datetime.now():%Y%m%d-%H%M}.csv")

            def done(d, res):
                try:
                    path = d.save_finish(res).get_path()
                except GLib.Error:
                    return
                try:
                    with open(path, "w", newline="", encoding="utf-8") as fh:
                        wr = csv.writer(fh)
                        wr.writerow(["tid", "frekvens_hz", "typ", "avsandare", "namn", "kategori", "lat", "lon",
                                     "niva_db", "text"])
                        for i in range(self.dec_store.get_n_items()):
                            dd = self.dec_store.get_item(i).data
                            pos = dd.get("pos") or ("", "")
                            wr.writerow([datetime.datetime.fromtimestamp(dd["time"]).isoformat(timespec="seconds"),
                                         round(dd["freq"]), dd["type"], dd["src"], dd["name"], dd["kind"],
                                         pos[0], pos[1], round(dd["level"], 1), dd["text"]])
                    self.toast(f"Exporterade till {path}")
                except OSError as ex:
                    log.error("CSV-export misslyckades: %s", ex)
                    self.toast(f"Kunde inte spara: {ex}")
            dlg.save(self, None, done)

        # ── bokmärken ──
        def _load_bookmarks(self):
            self.bm_store.remove_all()
            for bm in self.conf["bookmarks"]:
                self.bm_store.append(Row(bm))
            self.view.queue_draw()

        def _add_bookmark(self):
            name = self.bm_name.get_text().strip() or f"{fmt_mhz(self.freq)} MHz {self.engine.mode}"
            self.conf["bookmarks"].append({"name": name, "freq": self.freq, "mode": self.engine.mode})
            self.bm_name.set_text("")
            self._load_bookmarks()
            save_conf(self.conf)
            self.toast(f"Bokmärke sparat: {name}")

        def _del_bookmark(self):
            item = self.bm_sel.get_selected_item()
            if not item:
                self.toast("Markera ett bokmärke först")
                return
            try:
                self.conf["bookmarks"].remove(item.data)
            except ValueError:
                pass
            self._load_bookmarks()
            save_conf(self.conf)

        # ── felsökning ──
        def copy_report(self):
            e = self.engine
            extra = (f"Mottagning igång: {e.running}, källa: {e.source.name if e.source else '-'}, "
                     f"block: {e.stats['blocks']}, DSP: {e.stats['proc_ms']:.1f} ms/block, "
                     f"överskridanden: {e.stats['overruns']}")
            text = build_report(extra)
            clip = self.get_clipboard()
            clip.set_content(Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8",
                                                               GLib.Bytes.new(text.encode("utf-8"))))
            self.toast("Felrapporten är kopierad – klistra in den i Claude Code")
            log.info("Felrapport kopierad till urklipp")

        def save_screenshot(self, path, tries=10):
            """Renderar fönstret till en PNG-fil (fungerar även under Wayland)."""
            try:
                win = self
                for w in (getattr(self, "activity_win", None), getattr(self, "ship_win", None),
                          getattr(self, "an_win", None)):
                    if w is not None and w.get_visible():
                        win = w
                        break
                w, h = win.get_width(), win.get_height()
                paint = Gtk.WidgetPaintable.new(win)
                snap = Gtk.Snapshot()
                paint.snapshot(snap, w, h)
                node = snap.to_node()
                if node is None:
                    # Fönstret är inte ritat just nu (t.ex. minimerat) – försök igen strax
                    if tries > 0:
                        GLib.timeout_add(500, lambda: self.save_screenshot(path, tries - 1) and False)
                    else:
                        log.warning("Skärmbild: fönstret ritades inte (är det minimerat?)")
                    return False
                tex = win.get_renderer().render_texture(node, None)
                tex.save_to_png(path)
                log.info("Skärmbild sparad: %s", path)
            except Exception:
                log.exception("Kunde inte spara skärmbild")
            return False

        def _open_path(self, path):
            try:
                Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(path, None), None)
            except GLib.Error as ex:
                log.error("Kunde inte öppna %s: %s", path, ex)
                self.toast(f"Kunde inte öppna {path}")

        def toast(self, msg, timeout=4):
            t = Adw.Toast.new(msg)
            t.set_timeout(timeout)
            self.toasts.add_toast(t)
            return False

        # ── timers ──
        def _tick(self):
            e = self.engine
            got = False
            e.view_zoom, e.view_center = self.view.requested_view()
            while e.spec_q:
                try:
                    db, c, zl = e.spec_q.popleft()
                except IndexError:
                    break
                self.latest_db, self.latest_c = db, c
                if zl is None:
                    zl = (db, c, float(SR))
                if zl is not False:
                    self.view.push_line(*zl, to_waterfall=not self._auto_level_pending)
                    got = True
            if got:
                if self._auto_level_pending and e.stats["blocks"] > 15:
                    self._auto_level_pending = False
                    self.auto_level()
                if self._relevel_at and e.stats["blocks"] >= self._relevel_at:
                    self._relevel_at = 0
                    self.auto_level()
                self.view.queue_draw()
            if e.running:
                self.level_bar.set_value(float(np.clip(e.level + 120, 0, 120)))
                self.level_lbl.set_text(f"{e.level:6.1f} dB")
                self.sq_lbl.set_text("● SIGNAL" if e.sq_open and e.squelch > -150 else "")
            self.scanner.tick()
            try:
                self.an_panel.tick()
            except Exception:
                log.exception("Fel vid uppdatering av signalanalysen")
            return True

        def _slow_tick(self):
            try:
                self._update_log()
            except Exception:
                log.exception("Fel vid uppdatering av signalloggen")
            e = self.engine
            self._play_tick()
            self.scan_lbl.set_text(self.scanner.status)
            self._ship_tick += 1
            if self.ship_win is not None and self.ship_win.get_visible() and self._ship_tick % 5 == 0:
                try:
                    self.ship_win.refresh()
                except Exception:
                    log.exception("Fel vid uppdatering av fartygskartan")
            h = self.hub
            h.extra = {"ACARS": self.freq if (e.mode == "AM" and 118e6 <= self.freq <= 137e6) else None,
                       "APRS": self.freq if (e.mode == "NFM" and 144e6 <= self.freq <= 146e6) else None}
            on = [k for k, v in h.enabled.items() if v]
            if on and e.running:
                lines = []
                for k in on:
                    chs = [f for kk, f in h.stats_channels if kk == k]
                    st = h.stats[k]
                    lines.append(f"{k}: " + (", ".join(f"{f / 1e6:.3f}" for f in chs) if chs else "utanför fönstret")
                                 + f" – {st['decoded']} avkodade, {st['bursts']} skurar")
                self.acars_row.set_subtitle("\n".join(lines))
            else:
                self.acars_row.set_subtitle(("på (" + ", ".join(on) + ") – starta mottagningen") if on else "av")
            self.btn_skip.set_visible(self.scanner.active)
            self.sugg_row.set_subtitle(", ".join(fmt_mhz(f) for f in self.suggested[-8:]) + " MHz"
                                       if self.suggested else "inga än")
            self.sugg_btn.set_sensitive(bool(self.suggested))
            if e.running:
                self.stats_row.set_subtitle(f"{e.stats['proc_ms']:.1f} ms per block (max {BLOCK / SR * 1000:.0f}), "
                                            f"{e.stats['overruns']} överskridanden")
            return True

        def _on_close(self, *_):
            try:
                self.conf["window"] = [self.get_width(), self.get_height()]
                self.conf["sidebar_width"] = self.outer.get_position()
                if self.ship_win is not None:
                    self.conf["ship_map_view"] = list(self.ship_win.mp.view())
                save_conf(self.conf)
                if self.an_win is not None:
                    self.conf["an_win"] = [self.an_win.get_width(), self.an_win.get_height()]
                self.analyzer.stop()
                self.scanner.stop()
                self.engine.stop()
                self.audio.stop()
                for k, on in self.hub.enabled.items():
                    if on:
                        st = self.hub.stats[k]
                        log.info("%s-sammanfattning: %d skurar, %d avkodade, %d ej avkodbara", k,
                                 st["bursts"], st["decoded"], st["failed"])
                self.hub.stop()
                if self.web:
                    self.web.stop()
            except Exception:
                log.exception("Fel vid avslut")
            log.info("Programmet avslutas")
            return False

    def apply_theme(i):
        schemes = [Adw.ColorScheme.FORCE_DARK, Adw.ColorScheme.FORCE_LIGHT, Adw.ColorScheme.DEFAULT]
        Adw.StyleManager.get_default().set_color_scheme(schemes[i if 0 <= i < 3 else 0])

    _skin_css = {}

    def apply_skin(name, theme):
        """Laddar utseendets CSS. Färgerna i 'Spanare' gäller bara när programmet visas mörkt."""
        disp = Gdk.Display.get_default()
        old = _skin_css.pop("p", None)
        if old is not None:
            Gtk.StyleContext.remove_provider_for_display(disp, old)
        css = SKIN_CSS.get(name, "")
        dark = Adw.StyleManager.get_default().get_dark()
        if name == "spanare" and not dark:
            css = SKIN_CSS["klassisk"]
        prov = Gtk.CssProvider()
        prov.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(disp, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        _skin_css["p"] = prov

    class App(Adw.Application):
        def __init__(self, args):
            # Start utan flaggor (t.ex. från ikonen): bara ett fönster, en andra start visar det
            # befintliga. Start med flaggor från terminalen: egen instans, så flaggorna gäller.
            unique = len(sys.argv) <= 1
            super().__init__(application_id=APP_ID,
                             flags=Gio.ApplicationFlags.DEFAULT_FLAGS if unique else Gio.ApplicationFlags.NON_UNIQUE)
            self.args = args
            self.win = None
            self.connect("activate", self.on_activate)

        def on_activate(self, app):
            if self.win is not None:
                log.info("Programmet är redan öppet – visar befintligt fönster")
                self.win.present()
                return
            css = Gtk.CssProvider()
            css.load_from_string(".freq-entry { font-family: monospace; font-size: 16pt; font-weight: bold; "
                                 "color: #7fe0d0; } .map-label { background: rgba(0,0,0,0.65); color: white; "
                                 "border-radius: 4px; padding: 0 3px; font-size: 8pt; font-weight: bold; }")
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            if os.path.exists(CSS_FILE):
                user = Gtk.CssProvider()
                try:
                    user.load_from_path(CSS_FILE)
                    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), user,
                                                              Gtk.STYLE_PROVIDER_PRIORITY_USER)
                    log.info("Laddade egen stil: %s", CSS_FILE)
                except Exception as ex:
                    log.error("Fel i %s: %s", CSS_FILE, ex)
            conf = load_conf()
            apply_theme(conf.get("theme", 0))
            apply_skin(conf.get("skin", "spanare"), conf.get("theme", 0))
            Adw.StyleManager.get_default().connect(
                "notify::dark", lambda sm, _p: apply_skin(self.win.conf.get("skin", "spanare") if self.win else
                                                          conf.get("skin", "spanare"), 0))
            a = self.args
            if a.source:
                conf["source"] = a.source
            if a.spela:
                pth = os.path.abspath(a.spela)
                conf["source"], conf["play_file"] = "file", pth
                if a.takt or a.mittfrekvens:
                    conf.setdefault("play_override", {})[pth] = {
                        "rate": a.takt, "center": parse_freq(a.mittfrekvens) if a.mittfrekvens else None}
            if a.tcp:
                conf["tcp_addr"] = a.tcp
                conf["source"] = "tcp"
            if a.freq:
                conf["freq"] = parse_freq(a.freq)
            if a.mode:
                conf["mode"] = a.mode.upper()
                conf["bw"] = None
            if a.acars:
                conf["acars"] = True
            if a.ais:
                conf["ais"] = True
            if a.aprs:
                conf["aprs"] = True
            if a.direktsampling:
                conf["direct"] = {"av": 0, "i": 1, "q": 2}[a.direktsampling]
            if a.squelch is not None:
                conf["squelch"] = a.squelch
            if a.skanna:
                conf["scan_mode"] = 0 if a.skanna == "bokmarken" else 1
            if a.intervall:
                lo_s, _, hi_s = a.intervall.partition("-")
                conf["scan_start"], conf["scan_stop"] = lo_s, hi_s
            if a.kanalsteg:
                conf["scan_step_khz"] = a.kanalsteg
            if a.max_lyssning is not None:
                conf["scan_max_listen"] = a.max_lyssning
            if a.vantetid is not None:
                conf["scan_hold"] = a.vantetid
            if a.web is not None:
                conf["web_enabled"] = True
                if a.web:
                    conf["web_port"] = a.web
            win = MainWindow(self, conf, a)
            self.win = win
            win.present()
            on_unix_signals(lambda: win.close())           # stäng snyggt (sparar inställningar, loggar)
            log.info("Fönstret öppnat")

    log.info("=== %s %s startar (Python %s) ===", APP_NAME, VERSION, sys.version.split()[0])
    app = App(args)
    return app.run([sys.argv[0]])


# ════════════════════════════════════════════════════════════════════
# 8. KOMMANDORAD
# ════════════════════════════════════════════════════════════════════
def selftest_iq():
    """IQ-inspelning och uppspelning: skriv SigMF (med omstämning), WAV och rå cf32 i en annan takt,
    spela upp dem med FileSource och kontrollera frekvens, mittfrekvens och nivå."""
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory(prefix="radiospanaren-iq-") as d:
        t = np.arange(BLOCK) / SR

        def tone(f, k):
            return (0.3 * np.exp(2j * np.pi * f * (t + k * BLOCK / SR))).astype(np.complex64)

        def peak(y, rate):
            S = np.abs(np.fft.fft(y))
            return np.fft.fftfreq(len(y), 1 / rate)[int(np.argmax(S))]

        def play(path, n_blocks):
            src = iqfil.FileSource(iqfil.IQReader(path), SR, BLOCK, speed=0)
            src.open()
            out = []
            try:
                for _ in range(n_blocks):
                    out.append(src.read())
            except EOFError:
                pass
            return out

        for fmt in ("cu8", "cf32_le", "wav"):
            base = os.path.join(d, f"t_{fmt}")
            w = iqfil.WavWriter(base, SR, 145e6) if fmt == "wav" else iqfil.SigMFWriter(base, SR, 145e6, fmt)
            for k in range(8):
                w.write(tone(50e3, k), 145e6)
            if fmt != "wav":
                for k in range(8):
                    w.write(tone(-80e3, k), 146e6)        # omstämning mitt i inspelningen
            path = w.close()
            if fmt == "wav":
                path = w.paths[0]
            r = play(path, 40)
            good = len(r) >= 7 and abs(peak(r[2][0], SR) - 50e3) < 30 and r[2][1] == 145e6
            if fmt != "wav":
                good &= len(r) >= 15 and abs(peak(r[12][0], SR) + 80e3) < 30 and r[12][1] == 146e6
            lvl = float(np.sqrt(np.mean(np.abs(r[2][0]) ** 2))) if r else 0
            good &= abs(lvl - 0.3) < 0.02
            ok &= good
            print(f"  IQ {fmt:7s} spela in → spela upp: {'OK' if good else 'FEL'} ({len(r)} block, nivå {lvl:.3f})")
        n = int(48000 * 3)
        x = (0.3 * np.exp(2j * np.pi * 1000 * np.arange(n) / 48000)).astype(np.complex64)
        path = os.path.join(d, "ton_48000sps_7.074MHz.cf32")
        x.tofile(path)
        r = play(path, 30)
        y = np.concatenate([b[0] for b in r[2:10]])
        f = peak(y, SR)
        lvl = float(np.sqrt(np.mean(np.abs(y) ** 2)))
        good = abs(f - 1000) < 5 and r[0][1] == 7.074e6 and abs(lvl - 0.3) < 0.02
        ok &= good
        print(f"  IQ rå cf32 48 kS/s (namn ger takt/frekvens): {'OK' if good else 'FEL'} (topp {f:.0f} Hz, nivå {lvl:.3f})")
    return ok


def selftest(seconds=4.0):
    """Kör DSP-motorn mot simulatorn utan fönster och skriver ut resultat."""
    print(f"{APP_NAME} självtest – kör simulatorn i {seconds:.0f} s ...")
    e = Engine()
    e.center = 100.0e6 + SR * 0.15
    e.mode = "WFM"
    e.bw = DEFAULT_BW["WFM"]
    e.offset = 100.0e6 - e.center
    bytes_out = [0]

    class Count:
        def push(self, b):
            bytes_out[0] += len(b)
    e.add_sink(Count())
    src = SimSource()
    src.set("freq", e.center)
    errors = []
    e.on_error = errors.append
    e.start(src)
    t_end = time.time() + seconds
    modes_ok = []
    for m in MODES:
        e.mode = m
        e.bw = DEFAULT_BW[m]
        time.sleep(seconds / len(MODES))
        modes_ok.append((m, e.level))
    e.stop()
    changes, _ = e.detector.pop_changes()
    st = e.stats
    print(f"  Block behandlade: {st['blocks']}  (förväntat ca {int(seconds * SR / BLOCK)})")
    print(f"  DSP-tid: {st['proc_ms']:.1f} ms per block (gräns {BLOCK / SR * 1000:.1f} ms), "
          f"överskridanden: {st['overruns']}")
    print(f"  Ljud producerat: {bytes_out[0] / 2 / AF:.1f} s")
    print("  Nivå per läge: " + ", ".join(f"{m} {lv:.1f} dB" for m, lv in modes_ok))
    print(f"  Signaler hittade: {len(changes)}")
    for t in sorted(changes, key=lambda t: t["f"]):
        print(f"    {t['f'] / 1e6:10.4f} MHz  {fmt_bw(t['bw']):>10}  {t['peak']:6.1f} dB  {t['cls']}")
    # ACARS: syntetiskt meddelande genom hela avkodarkedjan, båda bitkonventionerna
    acars_ok = True
    for nrzi in (False, True):
        burst = avkodare.make_acars_burst(SR, nrzi=nrzi, reg=".SE-TST", text="M01ATS0001SJALVTEST")
        total = (int(SR * 0.6) + len(burst)) // BLOCK * BLOCK + 2 * BLOCK
        sig = np.zeros(total, np.complex64)
        sig[int(SR * 0.3):int(SR * 0.3) + len(burst)] = burst * 0.05
        n = np.arange(total)
        sig = sig * np.exp(2j * np.pi * -150e3 * n / SR)
        rng = np.random.default_rng(7)
        sig += (0.003 * (rng.standard_normal(total) + 1j * rng.standard_normal(total))).astype(np.complex64)
        ch = avkodare.AcarsChannel(131.725e6, SR)
        msgs = []
        for k in range(0, total, BLOCK):
            for seg, lvl in ch.feed(sig[k:k + BLOCK].astype(np.complex64), 131.875e6):
                m, why = avkodare.decode_acars_audio(np.abs(seg))
                if m:
                    msgs.append(m)
        good = any(m["reg"] == "SE-TST" and m["flight"] == "TS0001" and m["text"] == "SJALVTEST" for m in msgs)
        acars_ok &= good
        print(f"  ACARS-avkodare ({'NRZI' if nrzi else 'direkt'}): {'OK' if good else 'FEL'}")
    # AIS och APRS genom hela kedjan
    def chain(burst, f, c, cutoff, kind):
        sig = np.concatenate((np.zeros(int(SR * 0.3), np.complex64), burst * 0.05,
                              np.zeros(int(SR * 0.3), np.complex64)))
        sig = np.concatenate((sig, np.zeros(-len(sig) % BLOCK, np.complex64)))
        sig = sig * np.exp(2j * np.pi * (f - c) * np.arange(len(sig)) / SR)
        rng2 = np.random.default_rng(11)
        sig = (sig + 0.002 * (rng2.standard_normal(len(sig)) + 1j * rng2.standard_normal(len(sig)))).astype(np.complex64)
        ch = avkodare.BurstChannel(f, SR, cutoff, kind)
        segs = []
        for k in range(0, len(sig), BLOCK):
            segs += ch.feed(sig[k:k + BLOCK], c)
        return segs
    fr = [x for sg, _l in chain(avkodare.make_ais_burst(SR), 161.975e6, 162.2e6, 7500, "AIS")
          for x in avkodare.decode_ais(sg)]
    m = avkodare.parse_ais(fr[0]) if fr else None
    ais_ok = bool(m and m["src"] == "265123456" and m["pos"] == (59.3293, 18.0686))
    print(f"  AIS-avkodare: {'OK' if ais_ok else 'FEL'}")
    fr = [x for sg, _l in chain(avkodare.make_aprs_burst(SR), 144.8e6, 145.03e6, 8000, "APRS")
          for x in avkodare.decode_aprs(sg)]
    ax = avkodare.parse_ax25(fr[0]) if fr else None
    aprs_ok = bool(ax and ax["src"] == "SA0ABC-9" and avkodare.parse_aprs(ax)[0] == "Position")
    print(f"  APRS-avkodare: {'OK' if aprs_ok else 'FEL'}")
    # Signalanalys: RTTY till klartext och igenkänning av ett militärt HF-modem
    def tap_run(sig, off, bw, secs=3.5, dial=None):
        an = analys.Analyzer(SR)
        an.set_target(14.0e6 + off, bw)
        L = len(sig)
        rng3 = np.random.default_rng(3)
        text, feat = "", None
        for k in range(int(secs * SR / BLOCK)):
            n = k * BLOCK + np.arange(BLOCK)
            pos = n / (SR / 48000)
            y = analys.loop_sample(sig, pos)
            x = 0.05 * y * np.exp(2j * np.pi * off * n / SR) + 0.006 * (rng3.standard_normal(BLOCK) +
                                                                         1j * rng3.standard_normal(BLOCK))
            an.tap.feed(x.astype(np.complex64), 14.0e6)
            if k % 8 == 7 and k * BLOCK > SR:
                r = an.step(now=k * BLOCK / SR)
                if r:
                    feat, txt = r
                    text += txt
        ac = (14.0e6 + off + feat.get("f_mid", 0) - dial) if dial else None
        return feat, text, analys.signaldb.match(feat, 14.0e6 + off, None, ac)
    feat, text, _k = tap_run(analys.make_rtty(48000, 50, 450, "RYRY CQ DE SJALVTEST 73 " * 8), 20e3, 1000, secs=6)
    rtty_ok = feat["fam"] == "FSK2" and "DE SJALVTEST" in text
    print(f"  Signalanalys RTTY 50/450: {'OK' if rtty_ok else 'FEL'} ({feat['klass']}, {feat.get('baud') or 0:.1f} Bd, "
          f"text {text.strip()[:40]!r})")
    feat, _t, kand = tap_run(analys.make_psk(48000, 2400, 8, 8.0), 51800, 3000, dial=14.05e6)
    psk_ok = feat["fam"] == "PSK8" and bool(kand) and kand[0][1]["namn"] in ("STANAG 4285", "MIL-STD-188-110A/B (seriell)")
    print(f"  Signalanalys 8PSK 2400 Bd: {'OK' if psk_ok else 'FEL'} ({feat['klass']}, "
          f"{kand[0][1]['namn'] if kand else 'ingen kandidat'})")
    V = analysavkodare
    tests = [
        ("NAVTEX", V.make_sitor_b(48000, "ZCZC SA01\nSJALVTEST NAVTEX\nNNNN\n"), 20e3, 500, 24, "SJALVTEST NAVTEX"),
        ("POCSAG", np.concatenate((V.make_pocsag(48000, 1200, [(1234567, 3, "SJALVTEST POCSAG", False)]),
                                   np.zeros(48000, np.complex64))), 25e3, 15000, 7, "SJALVTEST POCSAG"),
        ("ALE", V.make_ale(48000, [("TO", "SM5")] * 3 + [("TIS", "HQ1")] * 2), 41625, 3000, 6, "HQ1 anropar SM5"),
        ("DMR", V.make_dmr(48000, bursts=36), 25e3, 12500, 4, "grupp 2402 från 2400123"),
    ]
    dec_ok = True
    for name, sig, off, bw, secs, want in tests:
        feat, text, _k = tap_run(sig, off, bw, secs=secs)
        good = want in text
        dec_ok &= good
        print(f"  Analysavkodare {name}: {'OK' if good else 'FEL'} ({feat['klass'] if feat else '–'})")
    iq_ok = selftest_iq()
    try:
        import adsb
        adsb_ok = adsb.selftest(verbose=True)
    except Exception as ex:
        print(f"  ADS-B-avkodare: FEL ({ex})")
        adsb_ok = False
    ok = not errors and st["blocks"] > 0 and bytes_out[0] > 0 and acars_ok and ais_ok and aprs_ok and adsb_ok and rtty_ok and psk_ok and dec_ok and iq_ok
    print("RESULTAT: " + ("OK" if ok else f"FEL {errors}"))
    log.info("Självtest: %s (%d block, %.1f ms/block)", "OK" if ok else "FEL", st["blocks"], st["proc_ms"])
    return 0 if ok else 1


def main():
    import argparse
    p = argparse.ArgumentParser(
        prog="radiospanaren",
        description="Radiospanaren – SDR-mottagare, radioskanner och signallogg. Se: radiospanaren --manual")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    p.add_argument("--manual", action="store_true", help="visa manualen")
    p.add_argument("--rapport", action="store_true", help="skriv ut en felrapport (för att klistra in i Claude Code)")
    p.add_argument("--logg", nargs="?", type=int, const=100, metavar="N", help="visa de senaste N raderna i loggen")
    p.add_argument("--fel", action="store_true", help="visa bara fel och varningar ur loggen")
    p.add_argument("--rensa-logg", action="store_true", help="töm loggfilerna")
    p.add_argument("--sokvagar", action="store_true", help="visa var program, inställningar och loggar ligger")
    p.add_argument("--sjalvtest", action="store_true", help="testa signalbehandlingen med simulatorn (utan fönster)")
    p.add_argument("--aterstall", action="store_true", help="återställ alla inställningar till standard")
    p.add_argument("--kalla", dest="source", choices=SOURCES, help="mottagarkälla: rtl, tcp, sim eller file (IQ-fil, se --spela)")
    p.add_argument("--simulator", action="store_true", help="samma som --kalla sim")
    p.add_argument("--tcp", metavar="VÄRD:PORT", help="anslut till en rtl_tcp-server")
    p.add_argument("--frekvens", dest="freq", metavar="FREKV", help="startfrekvens, t.ex. 145.5 eller 7074k")
    p.add_argument("--mod", dest="mode", type=str.upper, choices=MODES, help="modulering")
    p.add_argument("--start", action="store_true", help="börja ta emot direkt")
    p.add_argument("--spela", metavar="FIL", help="spela upp en IQ-fil (SigMF, WAV, .cu8, .cf32 …) som källa")
    p.add_argument("--takt", type=float, metavar="S/s", help="samplingstakt för rå IQ-fil (med --spela)")
    p.add_argument("--mittfrekvens", metavar="FREKV", help="mittfrekvens för rå IQ-fil (med --spela)")
    p.add_argument("--webb", dest="web", nargs="?", type=int, const=0, metavar="PORT",
                   help="starta webbgränssnittet (standardport 8073)")
    p.add_argument("--squelch", type=float, metavar="DB", help="squelchnivå i dB, t.ex. -8")
    p.add_argument("--skanna", choices=["bokmarken", "intervall"], help="starta skanning direkt")
    p.add_argument("--intervall", metavar="START-STOPP", help="skanningsintervall i MHz, t.ex. 87.5-108")
    p.add_argument("--kanalsteg", type=float, metavar="KHZ", help="kanalsteg vid intervallskanning, t.ex. 12.5")
    p.add_argument("--vantetid", type=float, metavar="S", help="sekunder att stanna efter att signalen försvunnit")
    p.add_argument("--max-lyssning", type=float, metavar="S",
                   help="lämna en signal efter högst S sekunder (0 = av)")
    p.add_argument("--acars", action="store_true", help="slå på ACARS-avkodning")
    p.add_argument("--ais", action="store_true", help="slå på AIS-avkodning (fartyg)")
    p.add_argument("--aprs", action="store_true", help="slå på APRS-avkodning")
    p.add_argument("--direktsampling", choices=["av", "i", "q"], help="direktsampling för HF (q för RTL-SDR v3/NESDR)")
    p.add_argument("--fartygskarta", action="store_true", help="öppna fartygskartan (AIS) direkt")
    p.add_argument("--aktivitetskarta", action="store_true", help="öppna aktivitetskartan direkt")
    p.add_argument("--kalibrera", action="store_true", help="öppna frekvenskalibreringen direkt")
    p.add_argument("--analysera", metavar="FREKVENS",
                   help="analysera signalen på FREKVENS i fliken Signalanalys (t.ex. 99.77 eller 14074k)")
    p.add_argument("--flik", choices=["signallogg", "bokmarken", "avkodat", "analys"], help="flik att visa längst ner")
    p.add_argument("--skarmbild", nargs=2, metavar=("FIL", "SEKUNDER"),
                   help="spara en skärmbild av fönstret (PNG) efter angivet antal sekunder")
    p.add_argument("--debug", action="store_true", help="detaljerad loggning, även i terminalen")
    args = p.parse_args()
    if args.simulator:
        args.source = "sim"

    setup_logging(verbose=args.debug)

    if args.manual:
        try:
            with open(MANUAL_FILE, encoding="utf-8") as f:
                txt = f.read()
        except OSError:
            print(f"Hittar inte manualen ({MANUAL_FILE})")
            return 1
        if sys.stdout.isatty() and shutil.which("less"):
            subprocess.run(["less", "-R"], input=txt, text=True)
        else:
            print(txt)
        return 0
    if args.rapport:
        print(build_report())
        print(f"\n(Rapporten är också sparad i {REPORT_FILE})")
        return 0
    if args.logg is not None or args.fel:
        lines = _tail(LOG_FILE, 100000 if args.fel else args.logg)
        if args.fel:
            out, keep = [], False
            for l in lines:
                if re.match(r"^\d{4}-\d\d-\d\d ", l):
                    keep = any(k in l for k in ("[ERROR]", "[WARNING]", "[CRITICAL]"))
                if keep:
                    out.append(l)
            lines = out[-(args.logg or 200):]
            if not lines:
                print("Inga fel eller varningar i loggen.")
        sys.stdout.write("".join(lines))
        return 0
    if args.rensa_logg:
        for f in (LOG_FILE, STDERR_FILE, REPORT_FILE):
            try:
                open(f, "w").close()
            except OSError:
                pass
        print("Loggfilerna är tömda.")
        return 0
    if args.sokvagar:
        print(f"Program:        {os.path.abspath(__file__)}")
        print(f"Manual:         {MANUAL_FILE}")
        print(f"Inställningar:  {CONF_FILE}")
        print(f"Egen stil:      {CSS_FILE}")
        print(f"Logg:           {LOG_FILE}")
        print(f"Felutskrifter:  {STDERR_FILE}")
        print(f"Felrapport:     {REPORT_FILE}")
        print(f"Inspelningar:   {rec_dir()}")
        return 0
    if args.aterstall:
        if os.path.exists(CONF_FILE):
            os.replace(CONF_FILE, CONF_FILE + ".bak")
            print(f"Inställningarna är återställda (gamla sparade som {CONF_FILE}.bak)")
        else:
            print("Inga sparade inställningar fanns.")
        return 0
    if args.sjalvtest:
        return selftest()
    if args.intervall:
        try:
            lo_s, _, hi_s = args.intervall.partition("-")
            if parse_freq(hi_s) <= parse_freq(lo_s):
                raise ValueError("stopp måste vara högre än start")
        except ValueError as e:
            p.error(f"--intervall: {e} (exempel: 87.5-108)")
    if args.freq:
        try:
            parse_freq(args.freq)
        except ValueError as e:
            p.error(str(e))
    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
