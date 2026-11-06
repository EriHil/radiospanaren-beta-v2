# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 erihil
"""
Avkodare och signalidentifiering för Radiospanaren.

  • KNOWN_SIGNALS / identify(): känner igen signaltyper utifrån frekvens
  • ACARS: AM-modulerad MSK, 2400 bit/s (toner 1200/2400 Hz), avkodas helt
    med paritets- och CRC-kontroll. Bara meddelanden med korrekt CRC visas.
  • AIS: GMSK 9600 bit/s (fartyg), HDLC + CRC, meddelandetyp 1–5, 18, 19, 21, 24
  • APRS: AFSK 1200 bit/s (Bell 202) på NFM, AX.25 + CRC, position/Mic-E/status
  • make_*_burst(): syntetiska testsignaler för självtest och simulator
"""
import re
import time
import threading
import queue
import logging
from collections import deque

import numpy as np
from scipy.signal import firwin, oaconvolve, butter, sosfiltfilt, hilbert

log = logging.getLogger("radiospanaren")

# ── Kända signaler (MHz, tolerans kHz, namn) ───────────────────────────
ACARS_FREQS = [129.125, 130.025, 130.450, 131.125, 131.450, 131.525, 131.550,
               131.725, 131.825, 136.700, 136.750, 136.800, 136.850, 136.900]

KNOWN_SIGNALS = (
    [(f, 6, "ACARS (AM/MSK 2400) – avkodas") for f in ACARS_FREQS] +
    [(f, 12, "VDL Mode 2 (D8PSK, flygdata)") for f in (136.725, 136.775, 136.875, 136.975)] +
    [(446.00625 + 0.0125 * i, 4, f"PMR446 kanal {i + 1}") for i in range(16)] + [
        (121.5, 8, "Civil nödfrekvens (AM)"),
        (243.0, 8, "Militär nödfrekvens/guard (AM)"),
        (156.8, 8, "Marin kanal 16 – anrop/nöd (NFM)"),
        (156.525, 8, "Marin DSC kanal 70 (digital)"),
        (161.975, 10, "AIS kanal A (GMSK 9600) – avkodas"),
        (162.025, 10, "AIS kanal B (GMSK 9600) – avkodas"),
        (144.8, 8, "APRS (AFSK 1200) – avkodas"),
        (1090.0, 1500, "ADS-B / Mode S (PPM)"),
        (1030.0, 1500, "SSR-förfrågan (radar)"),
        (137.1, 25, "Vädersatellit (NOAA 19 / Meteor)"),
        (137.62, 25, "Vädersatellit NOAA 15 (APT)"),
        (137.9125, 25, "Vädersatellit NOAA 18 / Meteor"),
        (433.92, 60, "ISM-sensorer/fjärrkontroller (OOK/FSK)"),
        (868.3, 100, "SRD-sensorer (FSK)"),
        (4.625, 3, "UVB-76 \"The Buzzer\" (rysk militär, AM)"),
        (7.0755, 1.6, "FT8 40 m (8-FSK)"), (14.0755, 1.6, "FT8 20 m (8-FSK)"),
        (10.1375, 1.6, "FT8 30 m (8-FSK)"), (18.1015, 1.6, "FT8 17 m (8-FSK)"),
        (21.0755, 1.6, "FT8 15 m (8-FSK)"), (28.0755, 1.6, "FT8 10 m (8-FSK)"),
    ])

EXTRA_BANDS = [
    (225.0, 400.0, "Militärflyg UHF (AM)"),
    (380.0, 385.0, "Rakel/TETRA (krypterat)"),
    (390.0, 395.0, "Rakel/TETRA (krypterat)"),
    (400.0, 406.0, "Radiosonder"),
    (136.7, 137.0, "ACARS/VDL2 flygdata"),
]

EXTRA_BOOKMARKS = [
    {"name": "ACARS 131,725 (Europa)", "freq": 131.725e6, "mode": "AM"},
    {"name": "ACARS 131,525 (Europa)", "freq": 131.525e6, "mode": "AM"},
    {"name": "ACARS 136,900", "freq": 136.9e6, "mode": "AM"},
    {"name": "Militär nödfrekvens 243,0", "freq": 243.0e6, "mode": "AM"},
    {"name": "AIS kanal A", "freq": 161.975e6, "mode": "NFM"},
    {"name": "APRS 144,800", "freq": 144.8e6, "mode": "NFM"},
    {"name": "UVB-76 (HF, direktsampling)", "freq": 4.625e6, "mode": "AM"},
]


def identify(f_hz, bw_hz=0):
    """Returnerar namnet på en känd signal nära f_hz, eller ''."""
    k = round(f_hz / 28.8e6)
    if k >= 1 and abs(f_hz - k * 28.8e6) < 3e3 and bw_hz < 3000:
        return f"Trolig störning (RTL-kristall 28,8 MHz × {k})"
    best, bestd = "", 1e18
    for fm, tol_k, name in KNOWN_SIGNALS:
        d = abs(f_hz - fm * 1e6)
        if d <= tol_k * 1e3 + bw_hz * 0.25 and d < bestd:
            best, bestd = name, d
    return best


# ── ACARS ───────────────────────────────────────────────────────────────
AF = 48000
SPB = 20                      # samples per bit vid 48 kHz och 2400 bit/s
SOH, STX, ETX, ETB, DEL, SYN, NAK = 0x01, 0x02, 0x03, 0x17, 0x7F, 0x16, 0x15

ACARS_LABELS = {
    "_\x7f": "Allmän kvittens",
    "SQ": "Squitter (markstationens anrop)",
    "Q0": "Länktest",
    "H1": "Meddelande besättning/system",
    "5Z": "Flygbolagsmeddelande",
    "B6": "ADS-C / ATC",
    "AA": "CPDLC / ATC",
}

_CRC_TAB = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0x8408 if _c & 1 else _c >> 1
    _CRC_TAB.append(_c)


def crc_kermit(data, crc=0):
    for b in data:
        crc = (crc >> 8) ^ _CRC_TAB[(crc ^ b) & 0xFF]
    return crc


def odd_parity(c):
    c &= 0x7F
    return c | (0x80 if bin(c).count("1") % 2 == 0 else 0)


def _parity_ok(b):
    return bin(b).count("1") % 2 == 1


def build_acars_frame(reg=".SE-RTD", label="H1", blk="3", text="M01ASK1234HEJ FRAN RADIOSPANAREN",
                      mode="2", ack=NAK):
    """Bygger ett ACARS-block (byte-lista) enligt ARINC 618."""
    addr = reg.rjust(7, ".")[:7]
    body = [ord(mode)] + [ord(ch) for ch in addr] + [ack] + [ord(ch) for ch in label] + [ord(blk)]
    body += [STX] + [ord(ch) for ch in text] + [ETX]
    body = [odd_parity(b) for b in body]
    crc = crc_kermit(body)
    pre = [0xFF] * 16 + [odd_parity(0x2B), odd_parity(0x2A), odd_parity(SYN), odd_parity(SYN), odd_parity(SOH)]
    return pre + body + [crc & 0xFF, crc >> 8, odd_parity(DEL)]


def make_acars_audio(frame, nrzi=False):
    """MSK-ljud (48 kHz) för ett ACARS-block. Bitar skickas LSB först."""
    bits = [(b >> j) & 1 for b in frame for j in range(8)]
    tones, prev = [], 1
    for bit in bits:
        tone = (1 if bit == prev else 0) if nrzi else bit
        if nrzi:
            prev = tone
        tones.append(tone)
    f = np.repeat(np.where(np.array(tones) == 1, 2400.0, 1200.0), SPB)
    ph = 2 * np.pi * np.cumsum(f) / AF
    return np.sin(ph).astype(np.float32)


def make_acars_burst(fs, nrzi=False, **kw):
    """AM-modulerad ACARS-skur i basband (komplex), samplingstakt fs."""
    a = make_acars_audio(build_acars_frame(**kw), nrzi=nrzi)
    a = np.concatenate((np.zeros(2400, np.float32), a, np.zeros(2400, np.float32)))
    t_out = np.arange(int(len(a) * fs / AF)) / fs
    a_up = np.interp(t_out, np.arange(len(a)) / AF, a)
    env = np.ones_like(a_up)
    env[: int(0.05 * fs)] = 0
    env[-int(0.05 * fs):] = 0
    return (env * (1 + 0.6 * a_up)).astype(np.complex64)


def _bytes_at(bits, pos, n):
    out = []
    for k in range(n):
        s = pos + 8 * k
        if s + 8 > len(bits):
            break
        out.append(int(np.dot(bits[s:s + 8], 1 << np.arange(8))))
    return out


def _parse(bits):
    """Letar SYN SYN SOH i en bitström och tolkar ACARS-blocket."""
    if len(bits) < 200:
        return None, "kort"
    w = np.lib.stride_tricks.sliding_window_view(bits, 8) @ (1 << np.arange(8))
    cand = np.where((w[:-16] == SYN) & (w[8:-8] == SYN) & (w[16:] == SOH))[0]
    last_err = "ingen synk"
    for i in cand:
        p = i + 24
        data = []
        while len(data) < 250:
            b = _bytes_at(bits, p + 8 * len(data), 1)
            if not b:
                break
            data.append(b[0])
            if len(data) >= 13 and (b[0] & 0x7F) in (ETX, ETB):
                break
        if len(data) < 13 or (data[-1] & 0x7F) not in (ETX, ETB):
            last_err = "inget slut"
            continue
        bcs = _bytes_at(bits, p + 8 * len(data), 2)
        if len(bcs) < 2:
            last_err = "ingen BCS"
            continue
        perr = sum(1 for b in data if not _parity_ok(b))
        if crc_kermit(data + bcs) != 0:
            last_err = f"CRC-fel ({perr} paritetsfel)"
            continue
        return data, "ok"
    return None, last_err


def _to_text(codes):
    out = []
    for c in codes:
        c &= 0x7F
        if c in (0x0D,):
            continue
        out.append(" ⏎ " if c == 0x0A else (chr(c) if 32 <= c < 127 else "·"))
    return "".join(out)


def decode_acars_audio(audio):
    """Avkodar ACARS ur demodulerat AM-ljud (48 kHz). Returnerar (meddelande|None, orsak)."""
    if len(audio) < SPB * 200:
        return None, "kort"
    sos = butter(4, [700, 3200], "bandpass", fs=AF, output="sos")
    bp = sosfiltfilt(sos, audio - np.mean(audio))
    an = hilbert(bp)
    fi = np.diff(np.unwrap(np.angle(an))) * AF / (2 * np.pi)
    fi = np.convolve(fi, np.ones(5) / 5, mode="same")
    tone = fi > 1800
    trans = np.nonzero(np.diff(tone.astype(np.int8)))[0] + 1
    if len(trans) < 20:
        return None, "ingen MSK"
    phase = int(np.argmax(np.bincount(trans % SPB, minlength=SPB)))
    nb = (len(fi) - phase) // SPB
    F = fi[phase:phase + nb * SPB].reshape(nb, SPB)[:, 4:16].mean(axis=1)
    t = (F > 1800).astype(np.uint8)
    same = np.concatenate(([1], (t[1:] == t[:-1]).astype(np.uint8)))
    reason = "ingen synk"
    for bits in (t, 1 - t, same, 1 - same):
        data, why = _parse(bits)
        if data is not None:
            return _make_msg(data), "ok"
        if why.startswith("CRC"):
            reason = why
    return None, reason


def _make_msg(data):
    ch = [b & 0x7F for b in data]
    mode = chr(ch[0])
    addr = "".join(chr(c) for c in ch[1:8])
    ack = "NAK" if ch[8] == NAK else chr(ch[8])
    label = "".join(chr(c) for c in ch[9:11])
    blk = chr(ch[11])
    txt_codes = ch[13:-1] if len(ch) > 13 and ch[12] == STX else []
    text = _to_text(txt_codes)
    down = blk.isdigit()
    msn = flight = ""
    if down and len(text) >= 10:
        msn, flight, text = text[:4], text[4:10].strip(), text[10:]
    lab = label.replace("\x7f", "d")
    desc = ACARS_LABELS.get(label, "")
    reg = addr.lstrip(".")
    return {
        "mode": mode, "reg": reg, "ack": ack, "label": lab,
        "label_desc": desc, "blk": blk, "dir": "Från flygplan" if down else "Till flygplan",
        "msn": msn, "flight": flight, "text": text.strip(),
        # gemensamma fält för fliken Avkodat
        "src": reg, "name": flight, "kind": lab + (f" – {desc}" if desc else ""), "pos": None,
        "details": [("Riktning", "Från flygplan" if down else "Till flygplan"), ("Registrering", reg),
                    ("Flight", flight or "–"), ("Label", f"{lab} {desc}"), ("Block", blk), ("Mode", mode),
                    ("Kvittens", ack), ("Meddelandenr", msn or "–")],
    }


class BurstChannel:
    """En kanal: blandar ner, decimerar till 48 kHz och hittar skurar (paket)."""
    DEC = 32

    def __init__(self, f, sr, cutoff=8000, kind="ACARS"):
        self.f = f
        self.sr = sr
        self.kind = kind
        self.h = firwin(401, cutoff, fs=sr).astype(np.float32)
        self.tail = np.zeros(len(self.h) - 1, np.complex64)
        self.ph = 0.0
        self.floor = None
        self.active = False
        self.buf = []
        self.quiet = 0
        self.pre = deque(maxlen=4)
        self.carry = np.zeros(0, np.complex64)

    def feed(self, x, c):
        w = -2 * np.pi * (self.f - c) / self.sr
        xm = (x * np.exp(1j * (self.ph + w * np.arange(len(x))))).astype(np.complex64)
        self.ph = (self.ph + w * len(x)) % (2 * np.pi)
        xin = np.concatenate((self.tail, xm))
        self.tail = xm[-(len(self.h) - 1):]
        y = oaconvolve(xin, self.h, mode="valid")[::self.DEC].astype(np.complex64)
        y = np.concatenate((self.carry, y))
        n = len(y) // 480
        self.carry = y[n * 480:]
        bursts = []
        for k in range(n):
            ck = y[k * 480:(k + 1) * 480]
            pdb = 10 * np.log10(float(np.mean(np.abs(ck) ** 2)) + 1e-20)
            if self.floor is None:
                self.floor = pdb
            if not self.active:
                if pdb > self.floor + 7:
                    self.active = True
                    self.buf = list(self.pre) + [ck]
                    self.quiet = 0
                else:
                    self.floor = pdb if pdb < self.floor else self.floor + 0.02 * (pdb - self.floor)
                    self.pre.append(ck)
            else:
                self.buf.append(ck)
                self.quiet = self.quiet + 1 if pdb < self.floor + 4 else 0
                if self.quiet >= 5 or len(self.buf) > 400:
                    self.active = False
                    seg = np.concatenate(self.buf)
                    self.buf = []
                    level = 10 * np.log10(float(np.mean(np.abs(seg) ** 2)) + 1e-20) - self.floor
                    bursts.append((seg, level))
        return bursts


def AcarsChannel(f, sr):
    return BurstChannel(f, sr, 8000, "ACARS")


# ── HDLC (gemensamt för AIS och APRS) ──────────────────────────────────
def crc_x25_ok(data):
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TAB[(crc ^ b) & 0xFF]
    return crc == 0xF0B8


def crc_x25(data):
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TAB[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFF


def nrzi_decode(raw):
    raw = np.asarray(raw, np.uint8)
    return np.concatenate(([1], (raw[1:] == raw[:-1]).astype(np.uint8)))


def hdlc_frames(bits, min_bytes=4):
    """Hittar HDLC-ramar (flagga 0x7E), tar bort bitstoppning, returnerar ramar med korrekt CRC."""
    s = "".join("1" if v else "0" for v in bits)
    flags = [m.start() for m in re.finditer("(?=01111110)", s)]
    out = []
    for a, c in zip(flags, flags[1:]):
        seg = s[a + 8:c]
        if len(seg) < min_bytes * 8:
            continue
        bb, ones, ok = [], 0, True
        for ch in seg:
            if ch == "1":
                ones += 1
                if ones > 5:
                    ok = False
                    break
                bb.append(1)
            else:
                if ones == 5:
                    ones = 0
                    continue
                ones = 0
                bb.append(0)
        if not ok or len(bb) % 8 or len(bb) < min_bytes * 8:
            continue
        data = bytes(sum(bb[i + j] << j for j in range(8)) for i in range(0, len(bb), 8))
        if crc_x25_ok(data):
            out.append(data[:-2])
    return out


def hdlc_encode(payload, preamble_flags=3, train=None):
    """Bygger bitar för en HDLC-ram (för testsignaler): flaggor + stoppad data + FCS + flagga."""
    fcs = crc_x25(payload)
    data = list(payload) + [fcs & 0xFF, fcs >> 8]
    bits = []
    ones = 0
    for b in data:
        for j in range(8):
            v = (b >> j) & 1
            bits.append(v)
            if v:
                ones += 1
                if ones == 5:
                    bits.append(0)
                    ones = 0
            else:
                ones = 0
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    return (train or []) + flag * preamble_flags + bits + flag * 2


def nrzi_encode(bits):
    out, lvl = [], 1
    for b in bits:
        if b == 0:
            lvl ^= 1
        out.append(lvl)
    return out


def _active_region(y, frac=0.3):
    pw = np.convolve(np.abs(y) ** 2, np.ones(48) / 48, mode="same")
    act = np.nonzero(pw > frac * pw.max())[0]
    if len(act) == 0:
        return y
    return y[max(0, act[0] - 24):act[-1] + 24]


def _fm_disc(y):
    return np.angle(y[1:] * np.conj(y[:-1]))


def _bits_from_decision(dec, spb, offsets):
    """Klockåterställning: bitgränser = vanligaste fasen för övergångar; samplar mitt i biten."""
    raw = dec.astype(np.int8)
    trans = np.nonzero(np.diff(raw))[0] + 1
    if len(trans) < 10:
        return []
    ph = int(np.argmax(np.bincount(trans % spb, minlength=spb)))
    res = []
    for off in offsets:
        start = (ph + off) % spb
        res.append(raw[start::spb].astype(np.uint8))
    return res


# ── AIS ─────────────────────────────────────────────────────────────────
AIS_FREQS = [161.975, 162.025]
AIS_NAV = {0: "Under gång (motor)", 1: "Till ankars", 2: "Ej manöverfärdig", 3: "Begränsad manöverförmåga",
           4: "Begränsad av djupgående", 5: "Förtöjd", 6: "Grundstött", 7: "Fiskar", 8: "Under segel",
           14: "AIS-SART (nödsändare)", 15: "Ej angiven"}
AIS_TYPES = {1: "Position klass A", 2: "Position klass A", 3: "Position klass A", 4: "Basstation",
             5: "Fartygsdata klass A", 9: "SAR-flygplan", 18: "Position klass B", 19: "Utökad position klass B",
             21: "Navigationshjälpmedel", 24: "Fartygsdata klass B", 27: "Långdistansposition"}


def ais_ship_type(t):
    if t == 35:
        return "Militärt fartyg"
    names = {30: "Fiskefartyg", 31: "Bogsering", 32: "Bogsering (stor)", 33: "Muddring", 34: "Dykning",
             36: "Segelbåt", 37: "Fritidsbåt", 50: "Lotsbåt", 51: "Sjöräddning (SAR)", 52: "Bogserbåt",
             53: "Hamnfartyg", 54: "Miljöskydd", 55: "Polis/myndighet", 58: "Sjukvård"}
    if t in names:
        return names[t]
    for lo, n in ((40, "Snabbgående fartyg"), (60, "Passagerarfartyg"), (70, "Lastfartyg"), (80, "Tankfartyg")):
        if lo <= t <= lo + 9:
            return n
    return f"Typ {t}" if t else "Okänd"


def ais_category(shiptype, mmsi=""):
    """Grov kategori för kartans färger."""
    t = shiptype or 0
    if str(mmsi).startswith("97"):
        return "sar"          # AIS-SART/MOB/EPIRB
    if t == 35:
        return "militar"
    if t in (36, 37):
        return "fritid"
    if t == 30:
        return "fiske"
    if 60 <= t <= 69:
        return "passagerare"
    if 70 <= t <= 79:
        return "last"
    if 80 <= t <= 89:
        return "tank"
    if t in (31, 32, 50, 51, 52, 53, 54, 55, 58) or 40 <= t <= 49:
        return "tjanst"
    return "okand"


def _u(b, s, n):
    v = 0
    for i in range(s, s + n):
        v = (v << 1) | int(b[i])
    return v


def _i(b, s, n):
    v = _u(b, s, n)
    return v - (1 << n) if v & (1 << (n - 1)) else v


def _txt6(b, s, nch):
    out = []
    for k in range(nch):
        if s + 6 * (k + 1) > len(b):
            break
        v = _u(b, s + 6 * k, 6)
        out.append(chr(v + 64 if v < 32 else v))
    return "".join(out).replace("@", " ").strip()


def _pos(lat, lon):
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    return (round(lat, 6), round(lon, 6))


def parse_ais(payload, ships=None):
    bits = [(byte >> (7 - j)) & 1 for byte in payload for j in range(8)]
    if len(bits) < 38:
        return None
    t = _u(bits, 0, 6)
    # rimlighetskontroll utöver CRC: typ 1–27, och positionsrapporterna har fast längd (168 bitar)
    if not 1 <= t <= 27 or (t in (1, 2, 3, 18) and len(bits) != 168):
        return None
    mmsi = _u(bits, 8, 30)
    m = {"type": "AIS", "src": str(mmsi), "name": "", "kind": AIS_TYPES.get(t, f"Meddelandetyp {t}"),
         "pos": None, "text": "", "details": [("MMSI", str(mmsi)), ("Meddelandetyp", f"{t} – "
                                              f"{AIS_TYPES.get(t, 'övrigt')}")], "ais_type": t, "ais": {}}
    d = m["details"]
    v = m["ais"]          # strukturerade värden för fartygskartan
    try:
        if t in (1, 2, 3) and len(bits) >= 168:
            nav = _u(bits, 38, 4)
            sog = _u(bits, 50, 10) / 10
            lon = _i(bits, 61, 28) / 600000
            lat = _i(bits, 89, 27) / 600000
            cog = _u(bits, 116, 12) / 10
            hdg = _u(bits, 128, 9)
            m["pos"] = _pos(lat, lon)
            m["text"] = f"{AIS_NAV.get(nav, nav)}, {sog:.1f} knop, kurs {cog:.0f}°"
            v.update(nav=nav, status=AIS_NAV.get(nav, str(nav)), sog=None if sog >= 102.3 else sog,
                     cog=None if cog >= 360 else cog, hdg=None if hdg == 511 else hdg, cls="A")
            d += [("Status", AIS_NAV.get(nav, str(nav))), ("Fart", f"{sog:.1f} knop"), ("Kurs", f"{cog:.1f}°"),
                  ("Bäring", "–" if hdg == 511 else f"{hdg}°")]
        elif t in (18, 19) and len(bits) >= 168:
            sog = _u(bits, 46, 10) / 10
            lon = _i(bits, 57, 28) / 600000
            lat = _i(bits, 85, 27) / 600000
            cog = _u(bits, 112, 12) / 10
            m["pos"] = _pos(lat, lon)
            m["text"] = f"{sog:.1f} knop, kurs {cog:.0f}°"
            d += [("Fart", f"{sog:.1f} knop"), ("Kurs", f"{cog:.1f}°")]
            hdg_b = _u(bits, 124, 9)
            v.update(sog=None if sog >= 102.3 else sog, cog=None if cog >= 360 else cog,
                     hdg=None if hdg_b == 511 else hdg_b, cls="B")
            if t == 19 and len(bits) >= 312:
                m["name"] = _txt6(bits, 143, 20)
                d.append(("Fartygstyp", ais_ship_type(_u(bits, 263, 8))))
                v.update(shiptype=_u(bits, 263, 8))
        elif t == 4 and len(bits) >= 168:
            lon = _i(bits, 79, 28) / 600000
            lat = _i(bits, 107, 27) / 600000
            m["pos"] = _pos(lat, lon)
            ts = (f"{_u(bits, 38, 14)}-{_u(bits, 52, 4):02d}-{_u(bits, 56, 5):02d} "
                  f"{_u(bits, 61, 5):02d}:{_u(bits, 66, 6):02d}:{_u(bits, 72, 6):02d} UTC")
            m["text"] = f"Tid {ts}"
            d.append(("Tid", ts))
            v.update(base=True)
        elif t == 5 and len(bits) >= 420:
            call = _txt6(bits, 70, 7)
            name = _txt6(bits, 112, 20)
            st = _u(bits, 232, 8)
            dest = _txt6(bits, 302, 20)
            imo = _u(bits, 40, 30)
            length = _u(bits, 240, 9) + _u(bits, 249, 9)
            m["name"] = name
            m["text"] = f"{ais_ship_type(st)}, anrop {call}, {length} m, mot {dest or '–'}"
            v.update(call=call, shiptype=st, dest=dest, length=length, imo=imo or None, cls="A")
            d += [("Namn", name), ("Anropssignal", call), ("IMO", str(imo) if imo else "–"),
                  ("Fartygstyp", ais_ship_type(st)), ("Längd", f"{length} m"), ("Destination", dest or "–")]
            if ships is not None and name:
                ships[mmsi] = name
        elif t == 24 and len(bits) >= 160:
            part = _u(bits, 38, 2)
            if part == 0:
                name = _txt6(bits, 40, 20)
                m["name"] = name
                m["text"] = "Namn (del A)"
                d.append(("Namn", name))
                if ships is not None and name:
                    ships[mmsi] = name
            else:
                st = _u(bits, 40, 8)
                call = _txt6(bits, 90, 7)
                m["text"] = f"{ais_ship_type(st)}, anrop {call}"
                v.update(call=call, shiptype=st, cls="B")
                d += [("Fartygstyp", ais_ship_type(st)), ("Anropssignal", call)]
        elif t == 21 and len(bits) >= 272:
            m["name"] = _txt6(bits, 43, 20)
            lon = _i(bits, 164, 28) / 600000
            lat = _i(bits, 192, 27) / 600000
            m["pos"] = _pos(lat, lon)
            m["text"] = "Navigationshjälpmedel (fyr/boj)"
            v.update(aton=True)
    except IndexError:
        pass
    if ships is not None and not m["name"]:
        m["name"] = ships.get(mmsi, "")
    if m["name"]:
        d.insert(1, ("Namn", m["name"]))
    return m


def _decode_ais_window(y, smooth):
    d = _fm_disc(y)
    d = np.convolve(d, np.ones(smooth) / smooth, mode="same")
    d = d - np.median(d)                      # tar bort sändarens frekvensfel
    dec = (d > 0).astype(np.uint8)
    for off in range(5):                      # alla fem klockfaser (5 samples per bit)
        frames = hdlc_frames(nrzi_decode(dec[off::5]), min_bytes=8)
        if frames:
            return frames
    return []


def decode_ais(y):
    """y: komplex basband 48 kHz kring AIS-kanalen. Returnerar lista med payloads (bytes).
    Flera fartyg kan sända direkt efter varandra (26,7 ms-luckor) och ett meddelande kan
    vara upp till 5 luckor långt, därför glidande fönster i flera storlekar."""
    y = _active_region(y)
    if len(y) < 5 * 100:
        return []
    found = []
    for smooth in (4, 3):
        for frm in _decode_ais_window(y, smooth):
            if frm not in found:
                found.append(frm)
        if len(y) > 1800:
            for win in (1700, 2900):
                for st in range(0, max(1, len(y) - win + 400), 400):
                    for frm in _decode_ais_window(y[st:st + win], smooth):
                        if frm not in found:
                            found.append(frm)
        if found:
            break
    return found


def make_ais_payload(mmsi=265123456, lat=59.3293, lon=18.0686, sog=12.3, cog=87.5, nav=0, t=1):
    fields = [(t, 6), (0, 2), (mmsi, 30), (nav, 4), (128, 8), (int(sog * 10), 10), (1, 1),
              (int(round(lon * 600000)) & ((1 << 28) - 1), 28), (int(round(lat * 600000)) & ((1 << 27) - 1), 27),
              (int(cog * 10), 12), (int(round(cog)) % 360, 9), (30, 6), (0, 2), (0, 3), (0, 1), (0, 19)]
    bits = []
    for v, n in fields:
        bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]
    return bytes(sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8))


def make_ais5_payload(mmsi=265123456, call="SABC", name="RADIOSPANAREN", ship_type=35, dest="STOCKHOLM"):
    def t6(txt, n):
        txt = (txt.upper() + "@" * n)[:n]
        out = []
        for ch in txt:
            v = ord(ch)
            v = v - 64 if v >= 64 else v
            out.append((v, 6))
        return out
    fields = [(5, 6), (0, 2), (mmsi, 30), (0, 2), (9123456, 30)] + t6(call, 7) + t6(name, 20) + \
             [(ship_type, 8), (50, 9), (20, 9), (5, 6), (5, 6), (1, 4), (11, 4), (5, 5), (12, 5), (0, 6),
              (60, 8)] + t6(dest, 20) + [(0, 1), (0, 1), (0, 2)]
    bits = []
    for v, n in fields:
        bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]
    bits += [0] * (-len(bits) % 8)
    return bytes(sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8))


def make_ais_burst(fs, payload=None):
    """GMSK-modulerad AIS-skur (komplex basband, samplingstakt fs)."""
    payload = payload or make_ais_payload()
    bits = nrzi_encode(hdlc_encode(payload, preamble_flags=1, train=[0, 1] * 12))
    nrz = np.repeat(np.array(bits, np.float32) * 2 - 1, 5)
    tt = np.arange(-10, 11) / 5.0
    bt = 0.4
    g = np.exp(-2 * (np.pi * bt * tt) ** 2 / np.log(2))
    f = np.convolve(nrz, g / g.sum(), mode="same") * 2400.0
    ph = 2 * np.pi * np.cumsum(f) / AF
    base = np.exp(1j * ph)
    base = np.concatenate((np.zeros(480), base, np.zeros(480)))
    t_out = np.arange(int(len(base) * fs / AF)) / fs
    tb = np.arange(len(base)) / AF
    env = np.interp(t_out, tb, np.abs(base))
    phase = np.interp(t_out, tb, np.unwrap(np.angle(base + 1e-12)))
    return (env * np.exp(1j * phase)).astype(np.complex64)


# ── APRS (AX.25 på AFSK 1200) ───────────────────────────────────────────
APRS_FREQS = [144.800]


def _ax25_addr(b):
    call = "".join(chr(c >> 1) for c in b[:6]).strip()
    ssid = (b[6] >> 1) & 0x0F
    return call + (f"-{ssid}" if ssid else ""), bool(b[6] & 0x80), bool(b[6] & 0x01)


def parse_ax25(frame):
    if len(frame) < 16:
        return None
    addrs = []
    i = 0
    while i + 7 <= len(frame):
        call, hbit, last = _ax25_addr(frame[i:i + 7])
        addrs.append((call, hbit))
        i += 7
        if last:
            break
    if len(addrs) < 2 or i + 2 > len(frame):
        return None
    ctrl, pid = frame[i], frame[i + 1]
    info = frame[i + 2:].decode("latin-1", errors="replace")
    dest, src = addrs[0][0], addrs[1][0]
    path = [c + ("*" if h else "") for c, h in addrs[2:]]
    return {"dest": dest, "src": src, "path": path, "ctrl": ctrl, "pid": pid, "info": info}


def _b91(s):
    v = 0
    for ch in s:
        v = v * 91 + (ord(ch) - 33)
    return v


def parse_aprs(ax):
    """Tolkar APRS-informationsfältet. Returnerar (kategori, position, text)."""
    info, dest = ax["info"], ax["dest"].split("-")[0]
    if not info:
        return "Tomt paket", None, ""
    t = info[0]
    pos_re = re.compile(r"(\d{2})(\d{2}\.\d+)([NS]).(\d{3})(\d{2}\.\d+)([EW])(.)(.*)", re.S)
    try:
        if t in "!=/@":
            body = info[1:] if t in "!=" else info[8:]
            m = pos_re.match(body)
            if m:
                lat = int(m.group(1)) + float(m.group(2)) / 60
                lon = int(m.group(4)) + float(m.group(5)) / 60
                lat = -lat if m.group(3) == "S" else lat
                lon = -lon if m.group(6) == "W" else lon
                return "Position", _pos(lat, lon), m.group(8).strip()
            if len(body) >= 13 and body[0] in "/\\" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij":
                lat = 90 - _b91(body[1:5]) / 380926
                lon = -180 + _b91(body[5:9]) / 190463
                return "Position (komprimerad)", _pos(lat, lon), body[13:].strip()
            return "Position (okänt format)", None, body
        if t in "`'\x1c\x1d" and len(dest) >= 6 and len(info) >= 9:
            dg = []
            for ch in dest[:6]:
                if ch.isdigit():
                    dg.append(int(ch))
                elif "A" <= ch <= "J":
                    dg.append(ord(ch) - 65)
                elif "P" <= ch <= "Y":
                    dg.append(ord(ch) - 80)
                else:
                    dg.append(0)
            lat = dg[0] * 10 + dg[1] + (dg[2] * 10 + dg[3] + (dg[4] * 10 + dg[5]) / 100) / 60
            north = "P" <= dest[3] <= "Z"
            lon_off = 100 if "P" <= dest[4] <= "Z" else 0
            west = "P" <= dest[5] <= "Z"
            dd = ord(info[1]) - 28 + lon_off
            if 180 <= dd <= 189:
                dd -= 80
            elif 190 <= dd <= 199:
                dd -= 190
            mm = ord(info[2]) - 28
            if mm >= 60:
                mm -= 60
            hh = ord(info[3]) - 28
            lon = dd + (mm + hh / 100) / 60
            sp, dc, se = ord(info[4]) - 28, ord(info[5]) - 28, ord(info[6]) - 28
            speed = sp * 10 + dc // 10
            speed = speed - 800 if speed >= 800 else speed
            course = (dc % 10) * 100 + se
            course = course - 400 if course >= 400 else course
            lat = lat if north else -lat
            lon = -lon if west else lon
            return "Position (Mic-E)", _pos(lat, lon), f"{speed * 1.852:.0f} km/h, kurs {course}° {info[9:].strip()}"
        if t == ">":
            return "Status", None, info[1:]
        if t == ":" and len(info) >= 11:
            return f"Meddelande till {info[1:10].strip()}", None, info[11:]
        if t == ";" and len(info) >= 18:
            return f"Objekt {info[1:10].strip()}", None, info[11:]
        if t == "_":
            return "Väder", None, info
        if t == "T":
            return "Telemetri", None, info
        if t == "}":
            return "Tredjepartstrafik", None, info[1:]
    except (ValueError, IndexError):
        pass
    return "Övrigt", None, info


def decode_aprs(y):
    """y: komplex basband 48 kHz (NFM). Returnerar lista med AX.25-ramar (bytes)."""
    y = _active_region(y, 0.2)
    if len(y) < 40 * 60:
        return []
    a = _fm_disc(y)
    a = a - np.mean(a)
    n = np.arange(40)
    c1 = np.abs(np.convolve(a, np.exp(-2j * np.pi * 1200 * n / AF), mode="same"))
    c2 = np.abs(np.convolve(a, np.exp(-2j * np.pi * 2200 * n / AF), mode="same"))
    dec = c1 / (np.mean(c1) + 1e-12) - c2 / (np.mean(c2) + 1e-12)   # tål förbetoning
    for bits in _bits_from_decision(dec > 0, 40, (20, 15, 25)):
        frames = hdlc_frames(nrzi_decode(bits), min_bytes=17)
        if frames:
            return frames
    return []


def make_ax25_frame(src="SA0ABC-9", dest="APRS", path=("WIDE1-1",), info="!5919.76N/01804.12E>Test"):
    def enc(call, last):
        c, _, ss = call.partition("-")
        b = [ord(ch) << 1 for ch in (c.upper() + "      ")[:6]]
        return b + [0x60 | ((int(ss or 0) & 0x0F) << 1) | (1 if last else 0)]
    calls = [dest, src] + list(path)
    out = []
    for k, cl in enumerate(calls):
        out += enc(cl, k == len(calls) - 1)
    return bytes(out + [0x03, 0xF0]) + info.encode("latin-1")


def make_aprs_burst(fs, frame=None, deviation=3000.0):
    """AFSK 1200 (1200/2200 Hz) på FM, komplex basband vid fs."""
    frame = frame or make_ax25_frame()
    bits = nrzi_encode(hdlc_encode(frame, preamble_flags=25))
    f = np.repeat(np.where(np.array(bits) == 1, 1200.0, 2200.0), 40)
    audio = np.sin(2 * np.pi * np.cumsum(f) / AF)
    audio = np.concatenate((np.zeros(2400), audio, np.zeros(2400)))
    t_out = np.arange(int(len(audio) * fs / AF)) / fs
    au = np.interp(t_out, np.arange(len(audio)) / AF, audio)
    ph = 2 * np.pi * deviation * np.cumsum(au) / fs
    return np.exp(1j * ph).astype(np.complex64)


# ── Avkodartråd ─────────────────────────────────────────────────────────
DECODERS = {
    "ACARS": {"freqs": ACARS_FREQS, "cutoff": 8000},
    "AIS": {"freqs": AIS_FREQS, "cutoff": 7500},
    "APRS": {"freqs": APRS_FREQS, "cutoff": 8000},
}


class DecoderHub:
    """Egen tråd som kör avkodarna, så att DSP-tråden inte blir långsammare."""

    def __init__(self, sr, on_message):
        self.sr = sr
        self.on_message = on_message
        self.enabled = {"ACARS": False, "AIS": False, "APRS": False}
        self.extra = {}            # kind -> extra frekvens (inställd frekvens)
        self.q = queue.Queue(maxsize=8)
        self.channels = {}
        self.ships = {}            # MMSI -> namn (från AIS typ 5/24)
        self.stats = {k: {"bursts": 0, "decoded": 0, "failed": 0} for k in DECODERS}
        self.stats_channels = []
        self.dropped = 0
        self.running = True
        self.thread = threading.Thread(target=self._run, name="Avkodare", daemon=True)
        self.thread.start()

    # bakåtkompatibelt
    @property
    def acars_enabled(self):
        return self.enabled["ACARS"]

    @acars_enabled.setter
    def acars_enabled(self, v):
        self.enabled["ACARS"] = bool(v)

    def any_enabled(self):
        return any(self.enabled.values())

    def feed(self, x, c, wait=False):
        """wait=True (uppspelning snabbare än realtid): vänta på avkodartråden i stället för att kasta."""
        if not self.any_enabled():
            return
        try:
            if wait:
                self.q.put((x, c), timeout=5.0)
            else:
                self.q.put_nowait((x, c))
        except queue.Full:
            self.dropped += 1

    def _wanted(self, c):
        out = []
        for kind, spec in DECODERS.items():
            if not self.enabled[kind]:
                continue
            fs = [f * 1e6 for f in spec["freqs"]]
            ex = self.extra.get(kind)
            if ex and all(abs(ex - f) > 5e3 for f in fs):
                fs.append(ex)
            out += [(kind, f) for f in fs if 15e3 < abs(f - c) < self.sr * 0.44]
        return out[:10]

    def _emit(self, kind, f, level, msg):
        self.stats[kind]["decoded"] += 1
        msg.update({"time": time.time(), "freq": f, "type": kind, "level": level})
        log.info("%s %.3f MHz: %s %s %s %s", kind, f / 1e6, msg.get("src", ""), msg.get("name", ""),
                 msg.get("kind", ""), str(msg.get("text", ""))[:60])
        self.on_message(msg)

    def _decode(self, kind, f, seg, level):
        st = self.stats[kind]
        st["bursts"] += 1
        if kind == "ACARS":
            msg, why = decode_acars_audio(np.abs(seg))
            if msg:
                self._emit(kind, f, level, msg)
                return
        elif kind == "AIS":
            frames = decode_ais(seg)
            for fr in frames:
                m = parse_ais(fr, self.ships)
                if m:
                    self._emit(kind, f, level, m)
            if frames:
                return
            why = "ingen giltig ram"
        else:
            frames = decode_aprs(seg)
            for fr in frames:
                ax = parse_ax25(fr)
                if not ax:
                    continue
                cat, pos, text = parse_aprs(ax)
                hdr = f"{ax['src']}>{ax['dest']}" + ("," + ",".join(ax["path"]) if ax["path"] else "")
                self._emit(kind, f, level, {
                    "src": ax["src"], "name": ",".join(ax["path"]), "kind": cat, "pos": pos, "text": text,
                    "details": [("Från", ax["src"]), ("Till", ax["dest"]), ("Väg", ",".join(ax["path"]) or "–"),
                                ("Typ", cat), ("Rått paket", hdr + ":" + ax["info"])]})
            if frames:
                return
            why = "ingen giltig ram"
        st["failed"] += 1
        log.debug("%s-skur %.3f MHz (%.0f dB, %.0f ms) gick inte att avkoda: %s",
                  kind, f / 1e6, level, len(seg) / AF * 1000, why)

    def _run(self):
        while self.running:
            try:
                x, c = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                want = self._wanted(c)
                if set(want) != set(self.channels):
                    self.channels = {k: self.channels.get(k) or
                                     BurstChannel(k[1], self.sr, DECODERS[k[0]]["cutoff"], k[0]) for k in want}
                    self.stats_channels = sorted(want, key=lambda z: z[1])
                for (kind, f), ch in self.channels.items():
                    for seg, level in ch.feed(x, c):
                        self._decode(kind, f, seg, level)
            except Exception:
                log.exception("Fel i avkodartråden")

    def stop(self):
        self.running = False
