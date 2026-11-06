# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 erihil
"""
ADS-B / Mode S-avkodare för Signalspanaren (1090 MHz, PPM 1 Mbit/s).

  • Mottagning med 2 MS/s (2 samples per bit), preambeldetektering i numpy
  • CRC-24 (polynom 0xFFF409) med rättning av enstaka bitfel för DF17/18
  • DF17/18: identitet (anropssignal), position (CPR global + lokal), höjd, hastighet
  • DF11: adress. DF4/5/20/21: höjd och squawk för redan kända flygplan
  • Simulator och kodare för självtest
"""
import math
import time
import threading
import logging
import json
import urllib.request

import numpy as np

log = logging.getLogger("signalspanaren")

ADSB_FREQ = 1090e6
ADSB_RATE = 2_000_000
ADSB_BLOCK = 262144            # 0,13 s
MSG_SAMPLES = 16 + 112 * 2     # preambel + långt meddelande

# ── CRC-24 ──────────────────────────────────────────────────────────────
_POLY = 0xFFF409
_TAB = []
for _i in range(256):
    _c = _i << 16
    for _ in range(8):
        _c = ((_c << 1) ^ _POLY) if _c & 0x800000 else (_c << 1)
    _TAB.append(_c & 0xFFFFFF)


def crc24(data):
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _TAB[((crc >> 16) ^ b) & 0xFF]) & 0xFFFFFF
    return crc


def syndrome(msg):
    """CRC över data XOR paritetsfältet. 0 = korrekt (DF11/17/18)."""
    n = len(msg)
    return crc24(msg[:n - 3]) ^ int.from_bytes(msg[n - 3:], "big")


# Syndrom för varje enskilt bitfel (112 bitar) → bitposition
_SYN1 = {}
for _p in range(112):
    _m = bytearray(14)
    _m[_p // 8] |= 0x80 >> (_p % 8)
    _SYN1[syndrome(bytes(_m))] = _p

# ── Hjälpfunktioner ─────────────────────────────────────────────────────
_CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


def _bits(msg, s, n):
    v = int.from_bytes(msg, "big")
    tot = len(msg) * 8
    return (v >> (tot - s - n)) & ((1 << n) - 1)


def cpr_nl(lat):
    if lat == 0:
        return 59
    a = abs(lat)
    if a == 87:
        return 2
    if a > 87:
        return 1
    nz = 15
    x = 1 - (1 - math.cos(math.pi / (2 * nz))) / math.cos(math.pi / 180 * a) ** 2
    return int(math.floor(2 * math.pi / math.acos(x)))


def cpr_global(e, o, latest_odd):
    """e, o: (lat_cpr, lon_cpr) som tal 0..1. Returnerar (lat, lon) eller None."""
    dlat_e, dlat_o = 360 / 60, 360 / 59
    j = math.floor(59 * e[0] - 60 * o[0] + 0.5)
    lat_e = dlat_e * (j % 60 + e[0])
    lat_o = dlat_o * (j % 59 + o[0])
    if lat_e >= 270:
        lat_e -= 360
    if lat_o >= 270:
        lat_o -= 360
    if cpr_nl(lat_e) != cpr_nl(lat_o):
        return None
    if latest_odd:
        lat, nl = lat_o, cpr_nl(lat_o)
        ni = max(nl - 1, 1)
        m = math.floor(e[1] * (nl - 1) - o[1] * nl + 0.5)
        lon = (360 / ni) * (m % ni + o[1])
    else:
        lat, nl = lat_e, cpr_nl(lat_e)
        ni = max(nl, 1)
        m = math.floor(e[1] * (nl - 1) - o[1] * nl + 0.5)
        lon = (360 / ni) * (m % ni + e[1])
    if lon >= 180:
        lon -= 360
    return lat, lon


def cpr_local(ref_lat, ref_lon, cpr, odd):
    dlat = 360 / (59 if odd else 60)
    j = math.floor(ref_lat / dlat) + math.floor(0.5 + (ref_lat % dlat) / dlat - cpr[0])
    lat = dlat * (j + cpr[0])
    dlon = 360 / max(cpr_nl(lat) - (1 if odd else 0), 1)
    m = math.floor(ref_lon / dlon) + math.floor(0.5 + (ref_lon % dlon) / dlon - cpr[1])
    return lat, dlon * (m + cpr[1])


def cpr_encode(lat, lon, odd):
    dlat = 360 / (59 if odd else 60)
    yz = math.floor(2 ** 17 * ((lat % dlat) / dlat) + 0.5)
    rlat = dlat * (yz / 2 ** 17 + math.floor(lat / dlat))
    dlon = 360 / max(cpr_nl(rlat) - (1 if odd else 0), 1)
    xz = math.floor(2 ** 17 * ((lon % dlon) / dlon) + 0.5)
    return yz & 0x1FFFF, xz & 0x1FFFF


def _dist_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


def _ac13_alt(ac):
    """Höjd ur 13-bitars AC-fält (DF0/4/16/20). Bara 25-fotsupplösning (Q=1) stöds."""
    m_bit = (ac >> 6) & 1
    q_bit = (ac >> 4) & 1
    if m_bit or not q_bit:
        return None
    n = ((ac & 0x1F80) >> 2) | ((ac & 0x20) >> 1) | (ac & 0x0F)
    return n * 25 - 1000


def _squawk(idf):
    """13-bitars ID-fält (DF5/21) → squawk-kod, t.ex. '7000'."""
    b = lambda k: (idf >> (12 - k)) & 1   # k = 0..12, bitordning C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4
    a = b(5) * 4 + b(3) * 2 + b(1)
    bb = b(11) * 4 + b(9) * 2 + b(7)
    c = b(4) * 4 + b(2) * 2 + b(0)
    d = b(12) * 4 + b(10) * 2 + b(8)
    return f"{a}{bb}{c}{d}"


# ── Flygplanstabell ─────────────────────────────────────────────────────
class Aircraft:
    __slots__ = ("icao", "callsign", "alt", "gs", "track", "vr", "lat", "lon", "squawk", "msgs",
                 "first", "last", "cpr", "pos_time", "category")

    def __init__(self, icao, now):
        self.icao = icao
        self.callsign = ""
        self.alt = self.gs = self.track = self.vr = self.lat = self.lon = None
        self.squawk = ""
        self.msgs = 0
        self.first = self.last = now
        self.cpr = {0: None, 1: None}     # jämn/udda: (lat_cpr, lon_cpr, tid)
        self.pos_time = 0
        self.category = ""

    def as_dict(self):
        return {k: getattr(self, k) for k in ("icao", "callsign", "alt", "gs", "track", "vr", "lat", "lon",
                                               "squawk", "msgs", "first", "last", "category")}


class ModeSDecoder:
    def __init__(self):
        self.aircraft = {}
        self.lock = threading.Lock()
        self.stats = {"candidates": 0, "valid": 0, "fixed": 0, "df": {}}
        self.new_aircraft = []

    # ── demodulering ──
    def demod(self, mag):
        """mag: amplitud vid 2 MS/s. Returnerar antal giltiga meddelanden."""
        n = len(mag) - MSG_SAMPLES
        if n <= 0:
            return 0
        m = [mag[k:n + k] for k in range(10)]
        c = ((m[0] > m[1]) & (m[1] < m[2]) & (m[2] > m[3]) & (m[3] < m[0]) & (m[4] < m[0]) &
             (m[5] < m[0]) & (m[6] < m[0]) & (m[7] > m[8]) & (m[8] < m[9]) & (m[9] > m[6]))
        high = (m[0] + m[2] + m[7] + m[9]) * 0.25
        low = (m[1] + m[3] + m[4] + m[5] + m[6] + m[8]) * (1 / 6)
        noise = float(np.median(mag)) + 1e-6
        c &= (high > 2.0 * low) & (high > 3.0 * noise)
        idx = np.nonzero(c)[0]
        self.stats["candidates"] += len(idx)
        good = 0
        skip_to = -1
        for i in idx:
            if i < skip_to:
                continue
            d = mag[i + 16:i + 16 + 224]
            bits = (d[0::2] > d[1::2]).astype(np.uint8)
            msg = self._check(np.packbits(bits).tobytes())
            if msg is not None:
                good += 1
                skip_to = i + 16 + len(msg) * 16
                self._handle(msg, time.time())
        return good

    def _check(self, raw):
        df = raw[0] >> 3
        if df in (17, 18):
            msg = raw[:14]
            s = syndrome(msg)
            if s == 0:
                return msg
            p = _SYN1.get(s)
            if p is not None and p >= 5:        # rätta ett bitfel (inte i DF-fältet)
                mb = bytearray(msg)
                mb[p // 8] ^= 0x80 >> (p % 8)
                self.stats["fixed"] += 1
                return bytes(mb)
            return None
        if df == 11:
            msg = raw[:7]
            return msg if syndrome(msg) == 0 else None   # squitter: II = 0
        if df in (4, 5, 20, 21):
            msg = raw[:7] if df < 16 else raw[:14]
            addr = syndrome(msg)
            with self.lock:
                if f"{addr:06X}" in self.aircraft:
                    return msg
            return None
        return None

    # ── tolkning ──
    def _get(self, icao, now):
        ac = self.aircraft.get(icao)
        if ac is None:
            ac = Aircraft(icao, now)
            self.aircraft[icao] = ac
            self.new_aircraft.append(icao)
        ac.last = now
        ac.msgs += 1
        return ac

    def _handle(self, msg, now):
        df = msg[0] >> 3
        st = self.stats
        st["valid"] += 1
        st["df"][df] = st["df"].get(df, 0) + 1
        with self.lock:
            if df in (11, 17, 18):
                icao = msg[1:4].hex().upper()
                ac = self._get(icao, now)
                if df in (17, 18):
                    self._me(ac, msg[4:11], now)
            elif df in (4, 20):
                ac = self._get(f"{syndrome(msg):06X}", now)
                alt = _ac13_alt(_bits(msg, 19, 13))
                if alt is not None:
                    ac.alt = alt
            elif df in (5, 21):
                ac = self._get(f"{syndrome(msg):06X}", now)
                ac.squawk = _squawk(_bits(msg, 19, 13))

    def _me(self, ac, me, now):
        tc = me[0] >> 3
        if 1 <= tc <= 4:
            cs = "".join(_CHARS[_bits(me, 8 + 6 * k, 6)] for k in range(8))
            ac.callsign = cs.replace("#", "").strip()
            ac.category = "ABCD"[4 - tc] + str(me[0] & 7)
        elif 9 <= tc <= 18 or 20 <= tc <= 22:
            a12 = _bits(me, 8, 12)
            if tc <= 18 and a12 and (a12 & 0x10):
                n = ((a12 & 0xFE0) >> 1) | (a12 & 0x0F)
                ac.alt = n * 25 - 1000
            odd = _bits(me, 21, 1)
            cpr = (_bits(me, 22, 17) / 131072, _bits(me, 39, 17) / 131072)
            ac.cpr[odd] = (cpr, now)
            pos = None
            other = ac.cpr[1 - odd]
            if other and now - other[1] < 10:
                e = ac.cpr[0][0]
                o = ac.cpr[1][0]
                pos = cpr_global(e, o, latest_odd=bool(odd))
            if pos is None and ac.lat is not None and now - ac.pos_time < 600:
                pos = cpr_local(ac.lat, ac.lon, cpr, odd)
            if pos and abs(pos[0]) <= 90 and abs(pos[1]) <= 180:
                if ac.lat is None or now - ac.pos_time > 120 or _dist_km((ac.lat, ac.lon), pos) < 30:
                    ac.lat, ac.lon = round(pos[0], 5), round(pos[1], 5)
                    ac.pos_time = now
        elif tc == 19:
            sub = _bits(me, 5, 3)
            if sub in (1, 2):
                f = 4 if sub == 2 else 1
                dew, vew = _bits(me, 13, 1), _bits(me, 14, 10)
                dns, vns = _bits(me, 24, 1), _bits(me, 25, 10)
                if vew and vns:
                    vx = (vew - 1) * f * (-1 if dew else 1)
                    vy = (vns - 1) * f * (-1 if dns else 1)
                    ac.gs = round(math.hypot(vx, vy))
                    ac.track = round(math.degrees(math.atan2(vx, vy)) % 360)
            elif sub in (3, 4):
                if _bits(me, 13, 1):
                    ac.track = round(_bits(me, 14, 10) * 360 / 1024)
                asp = _bits(me, 25, 10)
                if asp:
                    ac.gs = (asp - 1) * (4 if sub == 4 else 1)
            vr = _bits(me, 37, 9)
            if vr:
                ac.vr = (vr - 1) * 64 * (-1 if _bits(me, 36, 1) else 1)

    def snapshot(self, max_age=120):
        now = time.time()
        with self.lock:
            for k in [k for k, a in self.aircraft.items() if now - a.last > max_age]:
                del self.aircraft[k]
            out = [a.as_dict() for a in self.aircraft.values()]
            new, self.new_aircraft = self.new_aircraft, []
        return out, new


# ── Kodare (för simulator och självtest) ────────────────────────────────
def _frame(df, ca, icao, me_bits):
    v = (df << 83) | (ca << 80) | (int(icao, 16) << 56) | me_bits
    body = v.to_bytes(11, "big")
    return body + crc24(body).to_bytes(3, "big")


def enc_ident(icao, callsign):
    v = (4 << 51) | (3 << 48)                      # TC 4, kategori A3
    cs = (callsign.upper() + "        ")[:8]
    for k, ch in enumerate(cs):
        v |= (_CHARS.index(ch) if ch in _CHARS and ch != "#" else 32) << (42 - 6 * k)
    return _frame(17, 5, icao, v)


def enc_position(icao, lat, lon, alt_ft, odd):
    n = int(round((alt_ft + 1000) / 25))
    a12 = ((n & 0x7F0) << 1) | 0x10 | (n & 0x0F)
    yz, xz = cpr_encode(lat, lon, odd)
    v = (11 << 51) | (a12 << 36) | (odd << 34) | (yz << 17) | xz
    return _frame(17, 5, icao, v)


def enc_velocity(icao, gs, track, vr):
    vx = gs * math.sin(math.radians(track))
    vy = gs * math.cos(math.radians(track))
    dew, vew = (1 if vx < 0 else 0), int(round(abs(vx))) + 1
    dns, vns = (1 if vy < 0 else 0), int(round(abs(vy))) + 1
    sv = 1 if vr < 0 else 0
    vrr = int(round(abs(vr) / 64)) + 1
    v = (19 << 51) | (1 << 48) | (dew << 42) | (vew << 32) | (dns << 31) | (vns << 21) | (sv << 19) | (vrr << 10)
    return _frame(17, 5, icao, v)


def ppm(msg, amp=1.0):
    """PPM-modulering vid 2 MS/s: preambel + bitar (1 = puls i första halvan)."""
    bits = np.unpackbits(np.frombuffer(msg, np.uint8))
    s = np.zeros(16 + len(bits) * 2, np.float32)
    s[[0, 2, 7, 9]] = 1
    s[16 + 2 * np.arange(len(bits)) + (1 - bits)] = 1
    return s * amp


class AdsbSim:
    """Simulerade flygplan runt Stockholm."""
    PLANES = [("4AC9E1", "SAS1442", 59.65, 17.92, 35000, 450, 200, 0),
              ("4A8E32", "NAX4TV", 59.35, 18.30, 21000, 380, 20, 1500),
              ("43C6F5", "RRR4521", 59.10, 17.60, 28000, 410, 95, -1200)]

    def __init__(self):
        self.t0 = time.time()
        self.rng = np.random.default_rng()

    def block(self, n):
        x = (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)) * 0.03
        t = time.time() - self.t0
        for icao, cs, lat0, lon0, alt, gs, trk, vr in self.PLANES:
            d = gs * 1.852 / 3600 * t / 111.0
            lat = lat0 + d * math.cos(math.radians(trk))
            lon = lon0 + d * math.sin(math.radians(trk)) / math.cos(math.radians(lat0))
            msgs = [enc_position(icao, lat, lon, alt + vr * t / 60, int(t * 2) % 2),
                    enc_velocity(icao, gs, trk, vr)]
            if int(t * 4) % 20 == 0:
                msgs.append(enc_ident(icao, cs))
            for mm in msgs:
                p = ppm(mm, amp=0.3 + 0.2 * self.rng.random())
                i = int(self.rng.integers(0, n - len(p)))
                x[i:i + len(p)] += p * np.exp(1j * self.rng.uniform(0, 2 * np.pi))
        return x.astype(np.complex64)


# ── Mottagartråd ────────────────────────────────────────────────────────
_MAG_LUT = None


def _mag_from_u8(raw):
    global _MAG_LUT
    if _MAG_LUT is None:
        v = np.arange(65536, dtype=np.uint32)
        i = (v & 0xFF).astype(np.float32) - 127.4
        q = (v >> 8).astype(np.float32) - 127.4
        _MAG_LUT = np.sqrt(i * i + q * q).astype(np.float32) / 128.0
    return _MAG_LUT[np.frombuffer(raw.tobytes() if hasattr(raw, "tobytes") else raw, np.uint16)]


class AdsbReceiver:
    """Egen tråd som äger mottagaren i ADS-B-läge."""

    def __init__(self, source, on_error=None, on_stopped=None):
        self.source = source
        self.dec = ModeSDecoder()
        self.on_error = on_error
        self.on_stopped = on_stopped
        self.running = False
        self.thread = None
        self.level = 0.0
        self.t_start = 0

    def start(self):
        self.running = True
        self.t_start = time.time()
        self.thread = threading.Thread(target=self._run, name="ADS-B", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def _run(self):
        src = self.source
        tail = np.zeros(MSG_SAMPLES, np.float32)
        try:
            src.open()
            log.info("ADS-B-läge startat (%s, 1090 MHz, 2 MS/s)", src.name)
            while self.running:
                r = src.read()
                if r is None:
                    continue
                x, _c, raw = r
                mag = _mag_from_u8(raw) if raw is not None else np.abs(x).astype(np.float32)
                self.level = float(np.median(mag))
                buf = np.concatenate((tail, mag))
                self.dec.demod(buf)
                tail = mag[-MSG_SAMPLES:]
        except Exception as e:
            log.exception("Fel i ADS-B-mottagningen")
            if self.on_error:
                self.on_error(str(e))
        finally:
            try:
                src.close()
            except Exception:
                pass
            self.running = False
            st = self.dec.stats
            log.info("ADS-B-sammanfattning: %d giltiga meddelanden (%d rättade), %d flygplan, DF: %s",
                     st["valid"], st["fixed"], len(self.dec.aircraft),
                     ", ".join(f"{k}:{v}" for k, v in sorted(st["df"].items())) or "-")
            if self.on_stopped:
                self.on_stopped()


# ── Internetkälla (adsb.lol) ────────────────────────────────────────────
class AdsbLolFeed:
    """Flygplan från adsb.lol:s öppna API i stället för en egen mottagare. Ingen SDR behövs.
    Fyller en ModeSDecoder, så att Flygradar visar dem på samma sätt som vanligt."""
    RADIUS_NM = 250
    INTERVAL = 5.0
    MAX_WAIT = 60.0

    def __init__(self, lat, lon, user_agent, on_error=None):
        self.url = f"https://api.adsb.lol/v2/point/{lat:.5f}/{lon:.5f}/{self.RADIUS_NM}"
        self.user_agent = user_agent
        self.on_error = on_error
        self.dec = ModeSDecoder()
        self.running = False
        self.last_ok = 0.0
        self._halt = threading.Event()

    def start(self):
        self.running = True
        threading.Thread(target=self._run, name="adsb.lol", daemon=True).start()

    def stop(self):
        self.running = False
        self._halt.set()

    def _run(self):
        log.info("adsb.lol-läge startat (radie %d NM)", self.RADIUS_NM)
        wait, failing = self.INTERVAL, False
        while not self._halt.is_set():
            try:
                self._update(self._fetch())
                self.last_ok = time.time()
                if failing:
                    log.info("adsb.lol svarar igen")
                wait, failing = self.INTERVAL, False
            except Exception as e:
                wait = min(self.MAX_WAIT, wait * 2)
                if not failing and not self._halt.is_set():
                    failing = True
                    log.warning("adsb.lol: %s", e)
                    if self.on_error:
                        self.on_error(f"adsb.lol svarar inte: {e}")
            self._halt.wait(wait)
        log.info("adsb.lol-läge stoppat")

    def _fetch(self):
        req = urllib.request.Request(self.url, headers={"Accept": "application/json", "User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        if not isinstance(data.get("ac"), list):
            raise ValueError(data.get("msg") or "oväntat svar")
        return data

    def _update(self, data):
        now = time.time()
        dec = self.dec
        with dec.lock:
            seen = set()
            for a in data["ac"]:
                icao = (a.get("hex") or "").upper()
                if not icao:
                    continue
                seen.add(icao)
                ac = dec.aircraft.get(icao)
                if ac is None:
                    ac = dec.aircraft[icao] = Aircraft(icao, now)
                    dec.new_aircraft.append(icao)
                if a.get("messages") is not None:
                    ac.msgs = a["messages"]
                ac.callsign = (a.get("flight") or "").strip()
                alt = a.get("alt_baro")
                ac.alt = 0 if alt == "ground" else (round(alt) if isinstance(alt, (int, float)) else None)
                ac.gs = None if a.get("gs") is None else round(a["gs"])
                ac.track = None if a.get("track") is None else round(a["track"])
                ac.vr = None if a.get("baro_rate") is None else round(a["baro_rate"])
                ac.squawk = a.get("squawk") or ""
                ac.category = a.get("category") or ""
                ac.last = now - (a.get("seen") or 0)
                lat, lon = a.get("lat"), a.get("lon")
                if lat is not None and lon is not None and (a.get("seen_pos") or 0) <= 60:
                    ac.lat, ac.lon = round(lat, 5), round(lon, 5)
                else:
                    ac.lat = ac.lon = None
            for k in [k for k in dec.aircraft if k not in seen]:
                del dec.aircraft[k]


# ── Självtest ───────────────────────────────────────────────────────────
def selftest(verbose=True):
    """Kodar meddelanden för tre flygplan, lägger på brus och avkodar dem igen."""
    rng = np.random.default_rng(5)
    n = 600000
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.03
    truth = {}
    pos = 1000
    for icao, cs, lat, lon, alt, gs, trk, vr in AdsbSim.PLANES:
        truth[icao] = (cs, lat, lon, alt, gs, trk)
        frames = [enc_ident(icao, cs), enc_position(icao, lat, lon, alt, 0),
                  enc_position(icao, lat, lon, alt, 1), enc_velocity(icao, gs, trk, vr)]
        if icao == "4A8E32":                       # ett bitfel som ska rättas
            b = bytearray(frames[3])
            b[7] ^= 0x10
            frames[3] = bytes(b)
        for fr in frames:
            p = ppm(fr, amp=0.25)
            x[pos:pos + len(p)] += p * np.exp(1j * rng.uniform(0, 2 * np.pi))
            pos += 3000
    dec = ModeSDecoder()
    dec.demod(np.abs(x).astype(np.float32))
    ok = True
    lines = []
    for icao, (cs, lat, lon, alt, gs, trk) in truth.items():
        a = dec.aircraft.get(icao)
        good = (a is not None and a.callsign == cs and a.alt == alt and a.lat is not None
                and abs(a.lat - lat) < 0.001 and abs(a.lon - lon) < 0.001
                and a.gs is not None and abs(a.gs - gs) <= 2 and a.track is not None and abs(a.track - trk) <= 1)
        ok &= good
        if a:
            lines.append(f"    {icao} {a.callsign:8} {a.alt} ft  {a.gs} kn  {a.track}°  "
                         f"{a.lat}, {a.lon}  {'OK' if good else 'FEL'}")
        else:
            lines.append(f"    {icao} saknas  FEL")
    ok &= dec.stats["fixed"] >= 1
    if verbose:
        print(f"  ADS-B-avkodare: {'OK' if ok else 'FEL'} ({dec.stats['valid']} meddelanden, "
              f"{dec.stats['fixed']} rättade bitfel)")
        print("\n".join(lines))
    return ok
