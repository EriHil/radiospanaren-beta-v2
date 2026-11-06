# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 erihil
"""
Webbmottagare – listor över SDR-mottagare på nätet (KiwiSDR, OpenWebRX, WebSDR).

Källor (hämtas i bakgrunden och sparas i en cache, högst var 6:e timme):
  • rx.linkfanel.net/kiwisdr_com.js – datafilen bakom KiwiSDR-kartan, genererad automatiskt
    ur kiwisdr.com/public. Ger namn, plats, GPS, band, användare, SNR och antenn.
  • receiverbook.de/map – katalog över KiwiSDR, OpenWebRX och WebSDR (listan `receivers = [...]`
    i sidan). robots.txt tillåter allt.
  • En inbyggd svensk lista: länktipsen på sa0bmc.se ("Länktips till SDR-mottagare") och
    SK6AG:s mottagare (sk6ag1…5.ddns.net – flera av dem finns inte i de publika listorna).
  • Egna mottagare som användaren lagt till (sparas i inställningarna).

websdr.org:s egen lista används INTE: den får enligt PA3FWM inte återanvändas i
"another website or automated system without prior permission". WebSDR-mottagarna tas
i stället från receiverbook.

Bara KiwiSDR (och KiwiSDR-kompatibla, t.ex. Web-888) kan användas som mottagarkälla i
Signalspanaren – de andra öppnas i webbläsaren.

Live-status hämtas direkt från mottagaren: KiwiSDR /status (nyckel=värde) och
OpenWebRX /status.json (namn, plats, GPS och frekvensprofiler).
"""

import concurrent.futures
import html
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("signalspanaren")

UA = "Signalspanaren (SDR-mottagare för Linux)"
URL_KIWI = "http://rx.linkfanel.net/kiwisdr_com.js"
URL_RB = "https://www.receiverbook.de/map"
MAX_AGE = 6 * 3600
TYPES = ("KiwiSDR", "OpenWebRX", "WebSDR")

# ── inbyggd svensk lista ──────────────────────────────────────────────
# (url, namn, grupp, typ) – namnen uppdateras från mottagarens egen status när den svarar.
SA0BMC = "SA0BMC:s länktips"
SK6AG = "SK6AG"
BUILTIN = [
    ("http://websdr.sk4ko.com:8901/", "WebSDR SK4KO, Mora", SA0BMC, "WebSDR"),
    ("http://sa4bna.hopto.org:8073", "SA4BNA 0–32 MHz, Arvika", SA0BMC, "KiwiSDR"),
    ("http://lodsdr.proxy.kiwisdr.com:8073", "SM5VXO 0–30 MHz, Linköping", SA0BMC, "KiwiSDR"),
    ("http://kiwi-sm2gct.mooo.com:8073", "SM2GCT", SA0BMC, "KiwiSDR"),
    ("http://kiwisdrsk3jr2021.ddns.net:8073", "SK3JR 0–30 MHz", SA0BMC, "KiwiSDR"),
    ("http://greatexpectations.se:8073", "greatexpectations.se", SA0BMC, "KiwiSDR"),
    ("http://78.71.162.126:8073", "78.71.162.126", SA0BMC, "KiwiSDR"),
    ("http://kiwisdr.thulesius.se:8073", "Thulesius 1", SA0BMC, "KiwiSDR"),
    ("http://kiwisdr.thulesius.se:8074", "Thulesius 2", SA0BMC, "KiwiSDR"),
    ("http://kiwicam.sk3w.se:8073", "SK3W", SA0BMC, "KiwiSDR"),
    ("http://kiwisdr.sk2hg.se:8073", "SK2HG Siknäs Fortress", SA0BMC, "KiwiSDR"),
    ("http://sm4fge.ddns.net:8073", "SM4FGE", SA0BMC, "KiwiSDR"),
    ("http://sm2byc.ddns.net:8073", "SM2BYC 10 kHz–30 MHz", SA0BMC, "KiwiSDR"),
    ("http://radiotestsyd.ddns.net:5073/", "SDXF Radiotest Syd (Skåne)", SA0BMC, "KiwiSDR"),
    ("http://radiotestvast.ddns.net:5073/", "SDXF Radiotest Väst (Västra Götaland)", SA0BMC, "KiwiSDR"),
    ("http://sk6ag1.ddns.net:8071", "SK6AG Svenljunga", SK6AG, "KiwiSDR"),
    ("http://sk6ag3.ddns.net:8073", "SK6AG Grötö (Beverage 40 m)", SK6AG, "KiwiSDR"),
    ("http://sk6ag4.ddns.net:8074", "SK6AG Öresten (Beverage 49 m)", SK6AG, "KiwiSDR"),
    ("http://sk6ag5.ddns.net:8073", "SK6RM SDR1, Göteborg", SK6AG, "KiwiSDR"),
    ("http://sk6ag5.ddns.net:8074", "SK6RM SDR2", SK6AG, "KiwiSDR"),
    ("http://sk6ag5.ddns.net:8075", "Göteborg 0–30 MHz", SK6AG, "KiwiSDR"),
]


# ── hjälpfunktioner ───────────────────────────────────────────────────
def norm_url(u):
    """Jämförbar nyckel: värd:port i gemener (http:80 / https:443 som standard)."""
    u = (u or "").strip()
    if "://" not in u:
        u = "http://" + u
    p = urllib.parse.urlsplit(u)
    host = (p.hostname or "").lower()
    if host.endswith(".proxy.kiwisdr.com"):
        return host                                  # proxyn svarar på både 80 och 8073
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        port = 80
    return f"{host}:{port}"


def _name_key(r):
    """Andra dubblettnyckel: samma typ, namn och plats (±1 km) men olika adresser."""
    if not r.get("pos") or not r.get("namn"):
        return None
    n = re.sub(r"[^a-z0-9åäö]", "", r["namn"].lower())
    return (r["typ"], n, round(r["pos"][0], 2), round(r["pos"][1], 2))


def clean_url(u):
    u = (u or "").strip()
    if "://" not in u:
        u = "http://" + u
    return u.rstrip("/")


def locator_to_latlon(loc):
    """Maidenhead-lokator (4, 6 eller 8 tecken) → (lat, lon) i rutans mitt."""
    s = (loc or "").strip().upper()
    if not re.fullmatch(r"[A-R]{2}\d{2}([A-X]{2}(\d{2})?)?", s):
        raise ValueError("ogiltig lokator (t.ex. JO99bg)")
    lon = (ord(s[0]) - 65) * 20 - 180
    lat = (ord(s[1]) - 65) * 10 - 90
    lon += int(s[2]) * 2
    lat += int(s[3])
    dlon, dlat = 2.0, 1.0
    if len(s) >= 6:
        lon += (ord(s[4]) - 65) * 5 / 60
        lat += (ord(s[5]) - 65) * 2.5 / 60
        dlon, dlat = 5 / 60, 2.5 / 60
    if len(s) == 8:
        lon += int(s[6]) * 0.5 / 60
        lat += int(s[7]) * 0.25 / 60
        dlon, dlat = 0.5 / 60, 0.25 / 60
    return lat + dlat / 2, lon + dlon / 2


def parse_position(txt):
    """'JO99bg' eller '59.33, 18.07' → (lat, lon)."""
    t = (txt or "").strip()
    if not t:
        return None
    m = re.fullmatch(r"\s*(-?\d+(?:[.,]\d+)?)\s*[;, ]\s*(-?\d+(?:[.,]\d+)?)\s*", t.replace(", ", ";"))
    if m:
        lat, lon = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        if abs(lat) <= 90 and abs(lon) <= 180:
            return lat, lon
        raise ValueError("latitud/longitud utanför tillåtna värden")
    return locator_to_latlon(t)


def distance_km(a, b):
    if not a or not b:
        return None
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _gps(s):
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", s or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if lat == 0 and lon == 0:
        return None
    return lat, lon


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def kiwi_ok(r):
    """Kan Signalspanaren ta emot från den här? (KiwiSDR som inte stängt ute andra program än webbläsaren)"""
    return r["typ"] == "KiwiSDR" and r.get("ext_api") != 0


def _bands(s):
    out = []
    for part in (s or "").split(","):
        m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", part)
        if m:
            out.append((float(m.group(1)), float(m.group(2))))
    return out


def fmt_bands(bands):
    if not bands:
        return ""
    def f(x):
        if x >= 1e9:
            return f"{x / 1e9:g} GHz"
        if x >= 1e6:
            return f"{x / 1e6:.3g} MHz"
        return f"{x / 1e3:.3g} kHz"
    bands = sorted(bands)
    if len(bands) > 3:
        return f"{f(bands[0][0])}–{f(bands[-1][1])} ({len(bands)} delband)"
    return ", ".join(f"{f(a)}–{f(b)}" for a, b in bands)


def _get(url, timeout=20, limit=8 << 20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "replace")


def _rx(url, name, typ, **kw):
    d = {"url": clean_url(url), "namn": html.unescape(name or "").strip(), "typ": typ, "plats": "", "pos": None, "band": [],
         "users": None, "users_max": None, "ext_api": None, "auth": "", "snr": "", "antenn": "", "källor": [], "grupp": "", "status": None}
    d.update(kw)
    for f in ("plats", "antenn"):
        d[f] = html.unescape(d[f] or "").strip()
    return d


# ── hämtning av listorna ──────────────────────────────────────────────
def fetch_kiwisdr_com():
    t = _get(URL_KIWI)
    a = t[t.index("["): t.rindex("]") + 1]
    a = re.sub(r",\s*\]", "]", a)                 # avslutande komma (JavaScript, inte JSON)
    out = []
    for x in json.loads(a):
        if x.get("offline") == "yes" or not x.get("url"):
            continue
        try:
            u, um = int(x.get("users", "")), int(x.get("users_max", ""))
        except ValueError:
            u = um = None
        out.append(_rx(x["url"], x.get("name", ""), "KiwiSDR", plats=x.get("loc", ""), pos=_gps(x.get("gps")),
                       band=_bands(x.get("bands")), users=u, users_max=um, snr=x.get("snr", ""),
                       ext_api=_int(x.get("ext_api")),
                       antenn=x.get("antenna", ""), källor=["kiwisdr.com"]))
    return out


def fetch_receiverbook():
    h = _get(URL_RB)
    i = h.index("receivers = [") + len("receivers = ")
    groups, _ = json.JSONDecoder().raw_decode(h[i:])
    out = []
    for g in groups:
        c = (g.get("location") or {}).get("coordinates")
        pos = (float(c[1]), float(c[0])) if c and len(c) == 2 else None
        for r in g.get("receivers", []):
            typ = r.get("type")
            if typ not in TYPES or not r.get("url"):
                continue
            name = re.sub(r"<[^>]+>", " ", r.get("label") or "").strip()
            out.append(_rx(r["url"], name, typ, plats=(g.get("label") or "").strip(), pos=pos,
                           källor=["receiverbook"]))
    return out


def builtin():
    return [_rx(u, n, t, grupp=g, källor=[g]) for u, n, g, t in BUILTIN]


def merge(*lists):
    """Slå ihop listor; första förekomsten av en adress vinner, men tomma fält fylls på."""
    by = {}
    order = []
    alias = {}
    for L in lists:
        for r in L:
            k = norm_url(r["url"])
            nk = _name_key(r)
            if (k not in by and nk is not None and nk in alias
                    and not set(by[alias[nk]]["källor"]) & set(r["källor"])):   # bara mellan olika listor
                k = alias[nk]
            if k not in by:
                by[k] = dict(r)
                order.append(k)
                if nk is not None:
                    alias.setdefault(nk, k)
                continue
            d = by[k]
            for f in ("namn", "plats", "pos", "band", "users", "users_max", "ext_api", "auth", "snr", "antenn", "grupp"):
                if d.get(f) in (None, "", []) and r.get(f) not in (None, "", []):
                    d[f] = r[f]
            if d["typ"] != r["typ"] and r["typ"] == "KiwiSDR":
                d["typ"] = "KiwiSDR"
            d["källor"] = d["källor"] + [s for s in r["källor"] if s not in d["källor"]]
    return [by[k] for k in order]


class Catalog:
    """Cache + bakgrundsuppdatering. all() ger den sammanslagna listan."""

    def __init__(self, cache_file, own=None):
        self.cache_file = cache_file
        self.own = own or []
        self.lock = threading.Lock()
        self.lists = {"kiwisdr.com": [], "receiverbook": []}
        self.fetched = 0.0
        self.errors = {}
        self.busy = False
        self.status = {}             # norm_url → statusdict från mottagaren
        self._load()

    def _load(self):
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                d = json.load(f)
            for k in self.lists:
                L = d.get("lists", {}).get(k, [])
                for r in L:
                    r["pos"] = tuple(r["pos"]) if r.get("pos") else None
                    r["band"] = [tuple(b) for b in r.get("band", [])]
                self.lists[k] = L
            self.fetched = float(d.get("fetched", 0))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Kunde inte läsa cachen för webbmottagare: %s", e)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"fetched": self.fetched, "lists": self.lists}, f, ensure_ascii=False)
            os.replace(tmp, self.cache_file)
        except OSError as e:
            log.warning("Kunde inte spara cachen för webbmottagare: %s", e)

    def stale(self):
        return time.time() - self.fetched > MAX_AGE

    def refresh(self, done=None):
        """Hämtar listorna i en bakgrundstråd; done() anropas därifrån när det är klart."""
        if self.busy:
            return False
        self.busy = True

        def run():
            errs = {}
            for k, fn in (("kiwisdr.com", fetch_kiwisdr_com), ("receiverbook", fetch_receiverbook)):
                try:
                    L = fn()
                    if L:
                        with self.lock:
                            self.lists[k] = L
                    log.info("Webbmottagare: %d från %s", len(L), k)
                except Exception as e:
                    errs[k] = str(e)
                    log.warning("Webbmottagare: kunde inte hämta %s: %s", k, e)
            with self.lock:
                self.errors = errs
                if len(errs) < 2:
                    self.fetched = time.time()
                    self._save()
            self.busy = False
            if done:
                done()
        threading.Thread(target=run, name="Webbmottagare", daemon=True).start()
        return True

    def all(self):
        own = [_rx(o["url"], o.get("namn", ""), o.get("typ", "KiwiSDR"), grupp="Mina", källor=["Mina"])
               for o in self.own if o.get("url")]
        with self.lock:
            L = merge(own, builtin(), self.lists["kiwisdr.com"], self.lists["receiverbook"])
            for r in L:
                st = self.status.get(norm_url(r["url"]))
                if st:
                    r["status"] = st
                    for f in ("namn", "plats", "pos", "band", "users", "users_max", "ext_api", "auth", "antenn"):
                        if st.get(f) not in (None, "", []):
                            r[f] = st[f]
                    if st.get("typ"):
                        r["typ"] = st["typ"]
        return L

    # ── live-status ──
    def probe(self, urls, done=None, each=None):
        """Status för urls i bakgrunden; each() efter varje svar (i den ordning de kommer), done() sist."""
        def run():
            with concurrent.futures.ThreadPoolExecutor(12) as ex:
                futs = {ex.submit(probe, u): u for u in urls}
                for f in concurrent.futures.as_completed(futs):
                    with self.lock:
                        self.status[norm_url(futs[f])] = f.result()
                    if each:
                        each()
            if done:
                done()
        threading.Thread(target=run, name="Webbmottagare-status", daemon=True).start()


def probe(url, timeout=6):
    """Fråga mottagaren själv. Returnerar dict med 'ok' och det som gick att läsa ut."""
    base = clean_url(url)
    t0 = time.time()
    try:
        s = _get(base + "/status", timeout, 8000)
        if "users=" in s and "name=" in s:
            kv = dict(l.split("=", 1) for l in s.splitlines() if "=" in l)
            try:
                u, um = int(kv.get("users", "")), int(kv.get("users_max", ""))
            except ValueError:
                u = um = None
            return {"ok": kv.get("offline", "no") != "yes", "typ": "KiwiSDR", "namn": html.unescape(kv.get("name", "")),
                    "plats": html.unescape(kv.get("loc", "")), "pos": _gps(kv.get("gps")), "band": _bands(kv.get("bands")),
                    "users": u, "users_max": um, "antenn": kv.get("antenna", ""), "ext_api": _int(kv.get("ext_api")),
                    "auth": "password" if kv.get("auth") == "password" else "",   # status=private = ej i publika listan
                    "ms": int((time.time() - t0) * 1000), "tid": time.time()}
    except urllib.error.HTTPError as e:
        err = e                                 # servern svarar – men är ingen KiwiSDR
    except Exception as e:
        return {"ok": False, "fel": _short_err(e), "tid": time.time()}    # nås inte alls
    else:
        err = None
    try:
        s = _get(base + "/status.json", timeout, 200000)
        d = json.loads(s)
        rcv = d.get("receiver", {})
        g = rcv.get("gps") or {}
        bands = []
        for sdr in d.get("sdrs", []):
            for p in sdr.get("profiles", []):
                c, sr = p.get("center_freq"), p.get("sample_rate")
                if c and sr:
                    bands.append((c - sr / 2, c + sr / 2))
        return {"ok": True, "typ": "OpenWebRX", "namn": re.sub(r"<[^>]+>", " ", rcv.get("name", "")).strip(),
                "plats": rcv.get("location", ""), "pos": (g["lat"], g["lon"]) if "lat" in g else None,
                "band": bands, "users_max": d.get("max_clients"), "ms": int((time.time() - t0) * 1000),
                "tid": time.time()}
    except Exception as e:
        err = err or e
    try:
        h = _get(base + "/", timeout, 30000)
        low = h.lower()
        typ = "OpenWebRX" if "openwebrx" in low else "WebSDR" if ("websdr" in low or "pa3fwm" in low) else None
        m = re.search(r"<title>(.*?)</title>", h, re.S | re.I)
        return {"ok": True, "typ": typ, "namn": re.sub(r"\s+", " ", m.group(1)).strip()[:100] if m else "",
                "ms": int((time.time() - t0) * 1000), "tid": time.time()}
    except Exception as e:
        return {"ok": False, "fel": _short_err(e or err), "tid": time.time()}


def _short_err(e):
    s = str(getattr(e, "reason", e))
    for k, v in (("Name or service not known", "namnet finns inte (DNS)"), ("Connection refused", "vägrar anslutning"),
                 ("timed out", "svarar inte (timeout)"), ("No route to host", "nås inte"),
                 ("Network is unreachable", "inget nätverk")):
        if k in s:
            return v
    return s[:60]


SWE_CALL = re.compile(r"\b(?:S[A-M]\d[A-Z]{1,3}|[78]S\d[A-Z]{1,3})\b")


def is_swedish(r):
    """Svensk mottagare: inbyggd lista, 'Sweden/Sverige' i texten, svensk anropssignal eller .se-adress."""
    if r.get("grupp") in (SA0BMC, SK6AG):
        return True
    txt = r["namn"] + " " + r["plats"]
    if re.search(r"swed|sverige|schweden|su[eè]de", txt.lower()):
        return True
    host = urllib.parse.urlsplit(r["url"]).hostname or ""
    return bool(SWE_CALL.search(txt.upper())) or host.endswith(".se")


def covers(r, hz):
    return any(a <= hz <= b for a, b in r.get("band") or [])


def matches(r, text):
    t = text.lower()
    return all(w in " ".join((r["namn"], r["plats"], r["url"], r["antenn"], r["typ"], r["grupp"])).lower()
               for w in t.split())


def summary(r, home=None):
    """En rad med det viktigaste om mottagaren (för listan och kommandoraden)."""
    bits = [r["typ"]]
    if r.get("plats"):
        bits.append(r["plats"])
    d = distance_km(home, r.get("pos"))
    if d is not None:
        bits.append(f"{d:.0f} km")
    if r.get("band"):
        bits.append(fmt_bands(r["band"]))
    if r.get("users") is not None and r.get("users_max"):
        bits.append(f"{r['users']}/{r['users_max']} lyssnare")
    if r["typ"] == "KiwiSDR" and r.get("ext_api") == 0:
        bits.append("bara webbläsare")
    if r.get("auth") == "password":
        bits.append("🔒 lösenord krävs")
    st = r.get("status")
    if st and not st.get("ok"):
        bits.append("⚠ " + st.get("fel", "svarar inte"))
    return " · ".join(bits)
