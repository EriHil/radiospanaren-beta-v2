# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Signalanalys för Radiospanaren.

  ChannelTap   körs i DSP-tråden: klipper ut en kanal kring en vald bärvåg,
               decimerar och gör vattenfallsrader för den lilla vyn.
  analyze()    mäter signalens parametrar (SNR, bandbredd, envelop, momentan
               frekvens, symboltakt, PSK-ordning, toner) och klassar modulationen.
  Avkodare     insticksmoduler som demodulerar en viss signaltyp och visar
               värden/klartext (RTTY, Morse – fler läggs till i AVKODARE).
  Analyzer     arbetstråd som kopplar ihop allt; GUI:t läser .result.

Igenkänningen jämför mätvärdena mot signaldb.py.
"""
import math
import threading
import time
from collections import deque

import numpy as np
from scipy.signal import firwin, upfirdn, find_peaks
from scipy.ndimage import gaussian_filter1d, uniform_filter1d

import signaldb

FAM_NAMN = {
    "BRUS": "Ingen signal / brus",
    "BÄRVÅG": "Omodulerad bärvåg",
    "OOK": "På/av-nyckling (CW/OOK)",
    "AM": "AM",
    "FM": "FM (analog)",
    "SSB": "SSB / AM utan bärvåg",
    "FSK2": "2-FSK",
    "FSK4": "4-FSK",
    "MFSK": "MFSK (flerton, en åt gången)",
    "MSK": "MSK/GMSK",
    "BPSK": "BPSK",
    "QPSK": "QPSK",
    "PSK8": "8PSK (eller π/4-DQPSK)",
    "LIN": "Digital linjär (PSK/QAM)",
    "OFDM": "Parallelltonmodem / OFDM",
    "OKÄND": "Okänd",
}

CTCSS = [67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2, 110.9,
         114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3,
         173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1,
         233.6, 241.8, 250.3, 254.1]


# ════════════════════════════════════════════════════════════════════
# Kanalutsnitt (DSP-tråden)
# ════════════════════════════════════════════════════════════════════
class _Decim:
    """Tillståndsbärande FIR-decimering (polyfas via upfirdn)."""

    def __init__(self, h, D):
        self.D = D
        self.h = np.asarray(h, np.float32)
        assert (len(self.h) - 1) % D == 0
        self.tail = np.zeros(len(self.h) - 1, np.complex64)
        self.rem = np.zeros(0, np.complex64)

    def __call__(self, x):
        if len(self.rem):
            x = np.concatenate((self.rem, x))
        n = (len(x) // self.D) * self.D
        self.rem = x[n:].copy()
        if n == 0:
            return np.zeros(0, np.complex64)
        xin = np.concatenate((self.tail, x[:n]))
        self.tail = xin[-(len(self.h) - 1):]
        m = (len(self.h) - 1) // self.D
        y = upfirdn(self.h, xin, down=self.D)
        return y[m:m + n // self.D].astype(np.complex64)


def plan_decimation(sr, bw):
    need = 2.5 * max(bw, 200.0)
    for D in (256, 128, 64, 32, 16, 8, 4, 2):
        if sr % D == 0 and sr / D >= need:
            return D
    return 1


class ChannelTap:
    WF_N = 512

    def __init__(self, sr):
        self.sr = sr
        self.lock = threading.Lock()
        self.target = None
        self.bw = None
        self.gen = 0               # ökar vid varje nytt mål
        self.lines = deque(maxlen=128)
        self.status = "inget mål"
        self._key = None
        self.fs = None
        self.total = 0
        self.ring = None

    def set_target(self, f, bw):
        with self.lock:
            self.target = float(f)
            self.bw = float(bw)
            self.gen += 1
            self._key = None
            self.lines.clear()

    def clear(self):
        with self.lock:
            self.target = None
            self.gen += 1
            self._key = None
            self.lines.clear()
            self.status = "inget mål"

    def _rebuild(self, c):
        sr = self.sr
        D = plan_decimation(sr, self.bw)
        self.D = D
        self.fs = sr / D
        st = []
        if D > 8:
            st.append(_Decim(firwin(129, 0.35 * sr / 8, fs=sr), 8))
            D2 = D // 8
            st.append(_Decim(firwin(24 * D2 + 1, 0.4 * self.fs, fs=sr / 8), D2))
        elif D > 1:
            st.append(_Decim(firwin(24 * D + 1, 0.4 * self.fs, fs=sr), D))
        self.stages = st
        self.ph = 0.0
        cap = int(self.fs * 4.0)
        self.ring = np.zeros(cap, np.complex64)
        self.total = 0
        self.win = np.hanning(self.WF_N).astype(np.float32)
        self.win_norm = float((self.win ** 2).sum())
        self._key = (self.target, self.bw)

    def feed(self, x, c):
        """Anropas från DSP-tråden för varje block."""
        with self.lock:
            if self.target is None:
                return
            off = self.target - c
            if abs(off) > self.sr * 0.47:
                self.status = "utanför mottagningsfönstret"
                return
            if self._key != (self.target, self.bw):
                self._rebuild(c)
            self.status = "ok"
            n = len(x)
            w = -2 * np.pi * off / self.sr
            ph = self.ph + w * np.arange(n)
            y = x * np.exp(1j * ph).astype(np.complex64)
            self.ph = float((self.ph + w * n) % (2 * np.pi))
            for s in self.stages:
                y = s(y)
            m = len(y)
            if m == 0:
                return
            N = self.WF_N
            seg = y[-(m // N) * N:].reshape(-1, N) if m >= N else None
            cap = len(self.ring)
            yy = y
            if m > cap:
                self.total += m - cap
                yy = y[-cap:]
            i = self.total % cap
            k = min(len(yy), cap - i)
            self.ring[i:i + k] = yy[:k]
            if k < len(yy):
                self.ring[:len(yy) - k] = yy[k:]
            self.total += len(yy)
            if seg is None:
                last = self._last(N)
                if last is None or len(last) != N:
                    return
                seg = last[None, :]
            p = np.fft.fftshift((np.abs(np.fft.fft(seg * self.win, axis=1)) ** 2).mean(axis=0)) / self.win_norm
            self.lines.append((10 * np.log10(p + 1e-20).astype(np.float32), self.fs, self.target, self.bw))

    def _last(self, n):
        cap = len(self.ring)
        n = min(n, cap, self.total)
        if n <= 0:
            return None
        i = self.total % cap
        if n <= i:
            return self.ring[i - n:i].copy()
        return np.concatenate((self.ring[cap - (n - i):], self.ring[:i]))

    def snapshot(self, seconds):
        with self.lock:
            if self.target is None or self.ring is None or self.status != "ok":
                return None
            n = int(self.fs * seconds)
            z = self._last(n)
            if z is None:
                return None
            return z, self.fs, self.target, self.bw, self.gen, self.total

    def since(self, total_from):
        """Samples som tillkommit sedan räknarvärdet total_from (högst ringens längd)."""
        with self.lock:
            if self.ring is None:
                return None, self.total
            n = self.total - total_from
            if n <= 0:
                return np.zeros(0, np.complex64), self.total
            return self._last(n), self.total


# ════════════════════════════════════════════════════════════════════
# Parametrar och klassning
# ════════════════════════════════════════════════════════════════════
def _pow2(n):
    return 1 << int(round(math.log2(max(2, n))))


def _welch(z, fs, nfft):
    m = len(z) // nfft
    if m < 1:
        z = np.concatenate((z, np.zeros(nfft - len(z), z.dtype)))
        m = 1
    w = np.hanning(nfft).astype(np.float32)
    seg = z[:m * nfft].reshape(m, nfft) * w
    P = np.fft.fftshift((np.abs(np.fft.fft(seg, axis=1)) ** 2).mean(axis=0)) / float((w ** 2).sum())
    f = (np.arange(nfft) - nfft / 2) * fs / nfft
    return f, P


def _line(sig, fs, fmin, fmax, nfft=None):
    """Starkaste spektrallinje i [fmin, fmax]. Returnerar (frekvens, styrka i dB över omgivningen)."""
    sig = sig - sig.mean()
    n = len(sig)
    nfft = nfft or _pow2(n * 2)
    S = np.abs(np.fft.rfft(sig * np.hanning(n), nfft)) ** 2
    f = np.arange(len(S)) * fs / nfft
    k0, k1 = int(np.searchsorted(f, fmin)), int(np.searchsorted(f, fmax))
    if k1 - k0 < 8:
        return None, 0.0
    reg = S[k0:k1]
    k = int(np.argmax(reg))
    ref = float(np.median(reg)) + 1e-30
    strength = 10 * math.log10(float(reg[k]) / ref)
    kk = k0 + k
    if 0 < kk < len(S) - 1:
        a, b, c = math.log(S[kk - 1] + 1e-30), math.log(S[kk] + 1e-30), math.log(S[kk + 1] + 1e-30)
        d = a - 2 * b + c
        frac = 0.5 * (a - c) / d if d != 0 else 0.0
    else:
        frac = 0.0
    return (kk + float(np.clip(frac, -0.5, 0.5))) * fs / nfft, strength


def _power_line(y, fs, n, radius, nfft):
    """Linje i y**n nära 0 Hz (PSK-ordning / bärvåg). Returnerar (frekvens/n, styrka dB)."""
    Y = np.abs(np.fft.fftshift(np.fft.fft(y ** n, nfft))) ** 2
    f = (np.arange(nfft) - nfft / 2) * fs / nfft
    sel = np.abs(f) <= radius
    if not sel.any():
        return 0.0, 0.0
    idx = np.flatnonzero(sel)
    k = idx[int(np.argmax(Y[idx]))]
    lo, hi = max(0, k - 60), min(nfft, k + 61)
    nb = np.concatenate((Y[lo:max(lo, k - 3)], Y[min(hi, k + 4):hi]))
    ref = float(np.mean(nb)) + 1e-30 if len(nb) else 1e-30
    return float(f[k]) / n, 10 * math.log10(float(Y[k]) / ref)


def _smooth(x, n):
    n = int(max(1, n))
    return uniform_filter1d(x, n, mode="nearest") if n > 1 else x


def analyze(z, fs, bw):
    """Mät och klassa signalen i z (komplext basband, mitt = analysens målfrekvens)."""
    t0 = time.perf_counter()
    z = np.asarray(z, np.complex64)
    N = len(z)
    out = {"fs": fs, "bw": bw, "n": N, "fam": "BRUS", "klass": FAM_NAMN["BRUS"]}
    if N < 256:
        return out

    # ── 1. Spektrum, brusgolv, upptagen bandbredd ──
    res_want = max(fs / 16384, min(bw / 500, fs / 256))
    nfft = int(np.clip(_pow2(fs / res_want), 256, 16384))
    while nfft > 256 and N // nfft < 6:
        nfft //= 2
    f, P = _welch(z, fs, nfft)
    res = fs / nfft
    floor = float(np.median(P)) + 1e-30
    inb = np.abs(f) <= bw / 2
    Pin, fin = P[inb], f[inb]
    peak_snr = 10 * math.log10(float(Pin.max()) / floor)
    out.update(res=res, floor_db=10 * math.log10(floor), peak_snr=peak_snr,
               spec_f=fin, spec_db=10 * np.log10(Pin + 1e-30))
    strong = Pin > floor * 2.5
    ex = np.where(strong, Pin - floor, 0.0)
    if peak_snr < 6 or ex.sum() <= 0:
        out["snr"] = peak_snr
        return out
    cs = np.cumsum(ex) / ex.sum()
    lo = float(fin[min(len(fin) - 1, int(np.searchsorted(cs, 0.005)))])
    hi = float(fin[min(len(fin) - 1, int(np.searchsorted(cs, 0.995)))])
    occ = hi - lo + res
    mid = (lo + hi) / 2
    centroid = float((fin * ex).sum() / ex.sum())
    out.update(occ_bw=occ, f_mid=mid, f_centroid=centroid, f_lo=lo, f_hi=hi)

    # ── 2. Isolera signalen och flytta den till 0 Hz ──
    Z = np.fft.fft(z)
    fz = np.fft.fftfreq(N, 1 / fs)
    half = occ / 2 * 1.1 + 2 * res
    Z[np.abs(fz - mid) > half] = 0
    tt = np.arange(N) / fs
    zf = (np.fft.ifft(Z) * np.exp(-2j * np.pi * mid * tt)).astype(np.complex64)
    nb = max(1, int(np.count_nonzero(np.abs(fin - mid) <= half)))
    sig_p = float(ex[np.abs(fin - mid) <= half].sum())
    snr_lin = max(sig_p / (floor * nb), 1e-3)
    out["snr"] = 10 * math.log10(snr_lin)

    # ── 3. Envelop: på/av och variation ──
    a = np.abs(zf)
    sm = _smooth(a, max(3, fs * 0.003))
    p95 = float(np.percentile(sm, 95)) + 1e-12
    low = sm < 0.35 * p95
    high = sm > 0.6 * p95
    fl, fh = float(low.mean()), float(high.mean())
    onoff = fl > 0.12 and fh > 0.12 and fl + fh > 0.85
    on = high if onoff else np.ones(N, bool)
    out["skurar"] = onoff
    out["duty"] = fh
    a_on = a[on]
    cv = float(a_on.std() / (a_on.mean() + 1e-12))
    cv_c = math.sqrt(max(cv * cv - 1 / (2 * snr_lin), 0.0))
    out["cv"] = cv_c
    out["am_djup"] = float((np.percentile(sm, 97) - np.percentile(sm, 3)) /
                           (np.percentile(sm, 97) + np.percentile(sm, 3) + 1e-12))

    # ── 4. Momentan frekvens ──
    d = np.angle(zf[1:] * np.conj(zf[:-1])) * fs / (2 * np.pi)
    v = on[1:] & on[:-1] & (a[1:] > 0.3 * np.median(a_on))
    fi = _smooth(d, fs / max(occ, 50) * 0.8)
    fi_v = fi[v] if v.sum() > 50 else fi
    span = occ / 2 + 4 * res
    hist, edges = np.histogram(np.clip(fi_v, -span, span), bins=120, range=(-span, span))
    hs = gaussian_filter1d(hist.astype(float), 1.5)
    centers = (edges[:-1] + edges[1:]) / 2
    pk, _ = find_peaks(hs, height=0.18 * hs.max(), prominence=0.12 * hs.max(), distance=4)
    tones = centers[pk]
    out["hist"] = (centers, hs)
    out["fi_peaks"] = tones.tolist()
    nlev = len(tones)
    if nlev >= 2:
        sp = np.diff(np.sort(tones))
        shift = float(np.median(sp))
        # toner som ligger alldeles för nära varandra är brus i histogrammet
        if shift < max(3 * res, occ * 0.06):
            nlev = 1
    else:
        shift = None
    trans = 0.0
    if nlev == 2:
        t0_, t1_ = np.sort(tones)[:2]
        trans = float(np.mean(np.abs(fi_v - (t0_ + t1_) / 2) < 0.25 * (t1_ - t0_)))
    out["overgang"] = trans
    dev = float(np.percentile(np.abs(fi_v - np.median(fi_v)), 99))
    out["deviation"] = dev

    # ── 5. Symboltakt (cykliska linjer) ──
    fmax = min(1.3 * occ, fs / 2.2)
    cands = []
    if fmax > 20:
        env2 = (a * a)[on] if onoff else a * a
        cands.append(("envelop", _line(env2.astype(np.float64), fs, 8, fmax)))
        tr = np.abs(np.diff(fi))[v[1:]] if v.sum() > 100 else np.abs(np.diff(fi))
        cands.append(("frekvens", _line(tr.astype(np.float64), fs, 8, fmax)))
        ph = np.abs(d - np.median(d))
        cands.append(("fas", _line(ph[v].astype(np.float64) if v.sum() > 100 else ph, fs, 8, fmax)))
    baud, bstr, bsrc = None, 0.0, ""
    for name, (fb, s) in cands:
        if fb and s > bstr:
            baud, bstr, bsrc = fb, s, name
    if baud and bstr >= 11:
        src = {"envelop": (a * a), "frekvens": np.abs(np.diff(fi)), "fas": np.abs(d - np.median(d))}[bsrc]
        fb2, s2 = _line(src.astype(np.float64), fs, baud / 2 * 0.9, baud / 2 * 1.1)
        if fb2 and s2 > bstr - 4 and s2 >= 11:
            baud, bstr = fb2, s2
        out["baud"] = float(baud)
        out["baud_styrka"] = bstr
    else:
        out["baud"] = None
        out["baud_styrka"] = bstr

    # ── 6. Linjer i z^n: bärvåg och PSK-ordning ──
    zo = zf[on] if onoff else zf
    zo = zo / (np.sqrt(np.mean(np.abs(zo) ** 2)) + 1e-12)
    nf = _pow2(len(zo) * 2)
    lines = {}
    for n in (1, 2, 4, 8):
        lines[n] = _power_line(zo, fs, n, n * (3 * res + 15), nf)
    out["linjer"] = {n: round(s, 1) for n, (_, s) in lines.items()}
    car = lines[1][1]

    # ── 7. Toner i spektrum (parallellmodem/MFSK) ──
    sel = (fin >= lo - res) & (fin <= hi + res)
    sdb = 10 * np.log10(Pin[sel] + 1e-30)
    tp, _ = find_peaks(sdb, prominence=4, distance=max(2, int(20 / res)))
    tf = fin[sel][tp]
    ntones = len(tf)
    t_sp = float(np.median(np.diff(tf))) if ntones >= 3 else None
    regular = ntones >= 3 and np.std(np.diff(tf)) / (t_sp + 1e-9) < 0.3
    acf_str = 0.0
    if sel.sum() > 24:
        # periodicitet i spektrumet (autokorrelation) – hittar tonrastret även när topparna är svaga
        x = sdb - _smooth(sdb, max(3, int(len(sdb) / 6)))
        x = x - x.mean()
        ac = np.correlate(x, x, "full")[len(x) - 1:]
        ac = ac / (ac[0] + 1e-12)
        l0, l1 = max(2, int(25 / res)), min(len(ac) - 1, int(len(sdb) / 4))
        if l1 > l0 + 2:
            L = l0 + int(np.argmax(ac[l0:l1]))
            acf_str = float(ac[L])
            if acf_str > 0.35 and (not regular or ntones < 6):
                t_sp = L * res
                ntones = int(round(occ / t_sp))
                regular = True
    out["tonraster"] = acf_str
    flat = float(np.exp(np.mean(np.log(Pin[sel] + 1e-30))) / (np.mean(Pin[sel]) + 1e-30)) if sel.any() else 0.0
    out.update(spec_toner=tf.tolist(), n_spec_toner=ntones, tonavstand=t_sp, flathet=flat)

    # ── 8. Tal/ljud-aktivitet ──
    fr = max(1, int(fs * 0.02))
    nfr = N // fr
    syll = 0.0
    if nfr >= 10:
        e = (a[:nfr * fr] ** 2).reshape(nfr, fr).mean(axis=1)
        syll = float(np.std(10 * np.log10(e + 1e-20)))
    out["ljudvariation"] = syll

    # ── 9. Klassning ──
    narrow = occ < max(5 * res, 160)
    psk = None
    if car < 14 and not narrow:
        for n, nm in ((2, "BPSK"), (4, "QPSK"), (8, "PSK8")):
            if lines[n][1] >= 18:
                psk = nm
                break
    fsk_baud = None
    if nlev >= 2 and shift and cv_c < 0.35 and not psk:
        fsk_baud = _fsk_baud(fi_v, fs, tones)
    if onoff and narrow and cv_c < 0.3 and nlev <= 1:
        fam = "OOK"
    elif narrow and cv_c < 0.2 and not onoff:
        fam = "BÄRVÅG"
    elif psk:
        fam = psk
    elif nlev >= 2 and cv_c < 0.35 and shift and (nlev > 2 or trans < 0.18 or
                                                  (fsk_baud and abs(shift / fsk_baud - 0.5) < 0.12)):
        if fsk_baud:
            baud, bstr = fsk_baud, max(bstr, 20.0)
            out["baud"], out["baud_styrka"] = baud, bstr
        if nlev == 2:
            fam = "MSK" if baud and abs(shift / baud - 0.5) < 0.12 else "FSK2"
        elif nlev <= 4:
            fam = "FSK4"
        else:
            fam = "MFSK"
    elif ntones >= 6 and regular and cv_c > 0.3:
        fam = "OFDM"
    elif car >= 15 and cv_c > 0.06:
        fam = "AM"
    elif baud and bstr >= 14 and cv_c > 0.15:
        fam = "LIN"
    elif cv_c < 0.3 and dev > max(3 * res, 0.08 * occ):
        fam = "FM"
    elif cv_c >= 0.3:
        fam = "SSB"
    else:
        fam = "OKÄND"
    out["fam"] = fam
    out["klass"] = FAM_NAMN[fam]
    if fam in ("BRUS", "BÄRVÅG", "OOK", "AM", "FM", "SSB", "OKÄND", "OFDM"):
        out["baud"] = None         # en ljudton är ingen symboltakt
    if fam in ("FSK2", "FSK4", "MFSK", "MSK"):
        out["shift"] = shift
        out["toner"] = nlev
    elif fam == "OFDM":
        out["shift"] = t_sp
        out["toner"] = ntones
    else:
        out["shift"] = None
        out["toner"] = None

    # ── 10. Detaljer per typ ──
    if fam == "FM":
        ct = _ctcss(fi, fs)
        if ct:
            out["ctcss"] = ct
    if fam in ("BPSK", "QPSK", "PSK8", "LIN") and out.get("baud"):
        M = {"BPSK": 2, "QPSK": 4, "PSK8": 8}.get(fam, 4)
        nl = {"BPSK": 2, "QPSK": 4, "PSK8": 8}.get(fam, 4)
        out["konst"] = _constellation(zo, fs, out["baud"], lines[nl][0], nl)
        out["psk_m"] = M
    if fam in ("FSK2", "FSK4", "MSK") and out.get("baud"):
        out["symboler"] = _fsk_symbols(fi, fs, out["baud"], tones)
    if fam in ("AM", "SSB", "OOK"):
        k = max(1, int(len(sm) / 400))
        out["kurva"] = sm[-400 * k::k] / p95
    out["ms"] = (time.perf_counter() - t0) * 1000
    return out


def _fsk_baud(fi, fs, tones):
    """Symboltakt för FSK ur runlängderna mellan tonbyten (robust mot teckenstruktur som RYRY)."""
    tones = np.sort(np.asarray(tones))
    idx = np.argmin(np.abs(fi[:, None] - tones[None, :]), axis=1)
    ch = np.flatnonzero(np.diff(idx) != 0)
    if len(ch) < 12:
        return None
    runs = np.diff(ch).astype(float)
    runs = runs[runs > max(2.0, 0.2 * float(np.percentile(runs, 90)))]
    if len(runs) < 10:
        return None
    T0 = float(np.percentile(runs, 15))
    errs = []
    for T in np.linspace(0.4 * T0, 1.3 * T0, 400):
        q = runs / T
        k = np.round(q)
        ok = (k >= 1) & (k <= 16)
        if ok.sum() < 0.8 * len(q):
            errs.append((T, np.inf))
            continue
        errs.append((T, float(np.mean((q[ok] - k[ok]) ** 2))))
    good = [T for T, e in errs if e < 0.02]
    if not good:
        return None
    # största takten som passar alla runlängder (T/2, T/3 … passar också men är fel)
    bT = max(good)
    # finjustera kring vald takt
    near = [(e, T) for T, e in errs if bT * 0.88 <= T <= bT]
    bT = min(near)[1]
    b0 = fs / bT
    imp = np.zeros(len(idx))
    imp[ch] = 1.0
    fb, st = _line(imp, fs, b0 * 0.95, b0 * 1.05)
    return fb if fb and st >= 8 else b0


def _ctcss(fi, fs):
    """Subton (CTCSS) i FM-signalens momentanfrekvens."""
    k = max(1, int(fs / 1000))
    x = fi[:len(fi) // k * k].reshape(-1, k).mean(axis=1)
    if len(x) < 500:
        return None
    fl, s = _line(x.astype(np.float64), fs / k, 60, 260, nfft=_pow2(len(x) * 4))
    if not fl or s < 14:
        return None
    best = min(CTCSS, key=lambda c: abs(c - fl))
    return best if abs(best - fl) < 1.6 else None


def _sample(x, pos):
    i = np.floor(pos).astype(np.int64)
    fr = (pos - i).astype(np.float32)
    ok = (i >= 0) & (i + 1 < len(x))
    i, fr = i[ok], fr[ok]
    return x[i] * (1 - fr) + x[i + 1] * fr


def _constellation(zo, fs, baud, f_line, n):
    sps = fs / baud
    tail = zo[-int(min(len(zo), fs * 0.3)):]
    t = np.arange(len(tail)) / fs
    y = tail * np.exp(-2j * np.pi * f_line * t).astype(np.complex64)
    best, bpos = -1, None
    nsym = int(len(y) / sps) - 2
    if nsym < 20:
        return None
    for k in range(16):
        pos = (k / 16 + np.arange(nsym)) * sps
        s = _sample(y, pos)
        m = float(np.mean(np.abs(s)))
        if m > best:
            best, bpos = m, pos
    s = _sample(y, bpos)
    s = s * np.exp(-1j * np.angle(np.mean(s ** n)) / n)
    s = s / (np.sqrt(np.mean(np.abs(s) ** 2)) + 1e-12)
    return s[-500:]


def _fsk_symbols(fi, fs, baud, tones):
    sps = fs / baud
    tones = np.sort(np.asarray(tones))
    nsym = int(len(fi) / sps) - 2
    if nsym < 8:
        return ""
    best, bs = -np.inf, None
    for k in range(8):
        s = _sample(fi, (k / 8 + np.arange(nsym)) * sps)
        dist = np.min(np.abs(s[:, None] - tones[None, :]), axis=1)
        m = -float(np.mean(dist))
        if m > best:
            best, bs = m, s
    idx = np.argmin(np.abs(bs[:, None] - tones[None, :]), axis=1)
    return "".join(str(int(i)) for i in idx[-64:])


# ════════════════════════════════════════════════════════════════════
# Avkodare (insticksmoduler) – lägg till nya i AVKODARE längst ner
# ════════════════════════════════════════════════════════════════════
class Avkodare:
    """Basklass. passar(feat) avgör om avkodaren ska köras; feed() får nya samples
    (komplext basband kring analysens mål) och returnerar ny klartext."""
    namn = "?"

    def passar(self, feat):
        return False

    def reset(self):
        pass

    def feed(self, z, fs, feat):
        return ""

    def varden(self):
        """[(rubrik, värde)] att visa i värderutan."""
        return []

    def har_synk(self):
        """Sant när avkodaren har synk – då får den företräde framför andra."""
        return False

    def fakta(self):
        """Säkra värden från en avkodare med synk (t.ex. exakt symboltakt), ersätter mätningen."""
        return {}


ITA2_LTRS = "\0E\nA SIU\rDRJNFCKTZLWHYPQOBG\x0eMXV\x0f"
ITA2_FIGS = "\x003\n- '87\r\x054\x07,!:(5+)2#6019?&\x0e./=\x0f"


class RttyAvkodare(Avkodare):
    """Baudot/ITA2-RTTY: 1 startbit, 5 databitar (LSB först), 1–2 stoppbitar."""
    namn = "RTTY (Baudot/ITA2)"
    STD = (45.45, 50.0, 75.0, 100.0)

    def __init__(self):
        self.reset()

    def reset(self):
        self.key = None
        self.prev = None
        self.thr = 0.0
        self.dtail = np.zeros(0, np.float32)
        self.sig = np.zeros(0, np.float32)
        self.m = [self._mk(), self._mk()]     # två polariteter
        self.best = None
        self.emitted = [0, 0]

    @staticmethod
    def _mk():
        return {"ok": 0, "err": 0, "hist": deque(maxlen=40), "text": [], "figs": False, "pos": 0}

    def passar(self, feat):
        b, s = feat.get("baud"), feat.get("shift")
        if b and (abs(b - 100) < 5 or abs(b - 50) < 2.5) and s and 130 <= s <= 230:
            return False                 # SITOR-B/NAVTEX-signatur (fasningen ser ut som 50 Bd) – sköts av NAVTEX
        return feat.get("fam") == "FSK2" and b and 40 <= b <= 115 and s and 60 <= s <= 1100

    def feed(self, z, fs, feat):
        if len(z) == 0:
            return ""
        baud = feat["baud"]
        for sb in self.STD:
            if abs(baud - sb) / sb < 0.05:
                baud = sb
        tones = np.sort(np.asarray(feat["fi_peaks"])) + feat.get("f_mid", 0.0)
        thr = float(tones.mean()) if len(tones) >= 2 else feat.get("f_mid", 0.0)
        key = round(baud, 2)
        if key != self.key:
            self.key = key
            self.sig = np.zeros(0, np.float32)
            self.dtail = np.zeros(0, np.float32)
            self.thr = thr
            for m in self.m:
                m["pos"] = 0
        self.baud = baud
        self.thr = self.thr * 0.7 + thr * 0.3
        if self.prev is not None:
            z = np.concatenate(([self.prev], z))
        self.prev = z[-1]
        d = (np.angle(z[1:] * np.conj(z[:-1])) * fs / (2 * np.pi)).astype(np.float32)
        sps = fs / baud
        K = int(sps)                         # utjämningen behöver lite historik över skarven
        dd = np.concatenate((self.dtail, d))
        v = _smooth(dd, sps / 3).astype(np.float32)[len(self.dtail):]
        self.dtail = dd[-K:]
        self.sig = np.concatenate((self.sig, v))
        for pol in (0, 1):
            x = self.sig - self.thr
            self.m[pol]["pos"] = self._run(self.m[pol], x if pol == 0 else -x, sps)
        cut = min(m["pos"] for m in self.m)
        self.sig = self.sig[cut:]
        for m in self.m:
            m["pos"] -= cut
        # välj polaritet med bäst ramning
        sc = [(m["hist"].count(True) / max(1, len(m["hist"]))) if len(m["hist"]) >= 8 else 0 for m in self.m]
        best = int(np.argmax(sc)) if max(sc) >= 0.7 else None
        if best is None:
            return ""
        if best != self.best:
            self.best = best
            self.emitted[best] = max(0, len(self.m[best]["text"]) - 20)
        t = self.m[best]["text"]
        new = "".join(t[self.emitted[best]:])
        self.emitted[best] = len(t)
        if len(t) > 5000:
            del t[:4000]
            self.emitted[best] = len(t)
        return new

    def _run(self, m, s, sps):
        """Kör ramningen över s från m["pos"]; returnerar nästa position."""
        n = len(s)
        i = int(m.get("pos", 0))
        hw = max(1, int(sps * 0.2))
        frame = int(sps * 7.5)
        mark = s > 0

        def bit(c):
            c = int(c)
            return float(s[max(0, c - hw):c + hw + 1].mean()) > 0

        while i + frame < n:
            # nästa flank mark → space (startbit)
            seg = mark[i:n - frame]
            edges = np.flatnonzero(seg[:-1] & ~seg[1:])
            if len(edges) == 0:
                i = n - frame
                break
            j = i + int(edges[0]) + 1
            if not bit(j + 0.5 * sps) and bit(j + 6.6 * sps):
                code = 0
                for k in range(5):
                    if bit(j + (1.5 + k) * sps):
                        code |= 1 << k
                m["ok"] += 1
                m["hist"].append(True)
                ch = None
                if code == 0x1B:
                    m["figs"] = True
                elif code == 0x1F:
                    m["figs"] = False
                else:
                    ch = (ITA2_FIGS if m["figs"] else ITA2_LTRS)[code]
                    if code == 0x04:
                        m["figs"] = False      # 'unshift on space'
                if ch and ch not in "\0\r\x05\x07":
                    m["text"].append(ch)
                i = j + int(sps * 7.0)
            else:
                m["err"] += 1
                m["hist"].append(False)
                i = j + 1
        return max(0, i)

    def varden(self):
        if self.best is None:
            return [("RTTY", "söker ramsynk …")]
        m = self.m[self.best]
        ok = m["hist"].count(True) / max(1, len(m["hist"]))
        return [("RTTY", f"{self.baud:g} Bd, {'normal' if self.best == 0 else 'omvänd'} polaritet"),
                ("Ramar rätt", f"{ok * 100:.0f} %")]


MORSE = {".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F", "--.": "G", "....": "H",
         "..": "I", ".---": "J", "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O", ".--.": "P",
         "--.-": "Q", ".-.": "R", "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
         "-.--": "Y", "--..": "Z", ".----": "1", "..---": "2", "...--": "3", "....-": "4", ".....": "5",
         "-....": "6", "--...": "7", "---..": "8", "----.": "9", "-----": "0", ".-.-.-": ".", "--..--": ",",
         "..--..": "?", "-..-.": "/", "-...-": "=", ".-.-.": "+", "-....-": "-", ".--.-.": "@",
         ".-...": "&", "...-.-": "<SK>", "-.--.": "(", "-.--.-": ")", "---...": ":"}


class CwAvkodare(Avkodare):
    namn = "Morse (CW)"
    RATE = 500.0     # Hz, upplösning för nyckelläget

    def __init__(self):
        self.reset()

    def reset(self):
        self.acc = np.zeros(0, np.float32)
        self.state = False
        self.run = 0
        self.ons = deque(maxlen=60)
        self.sym = ""
        self.lvl_hi = None
        self.lvl_lo = None
        self.wpm = None
        self._last_dot = None
        self.space_done = True

    def passar(self, feat):
        return feat.get("fam") == "OOK"

    def feed(self, z, fs, feat):
        if len(z) == 0:
            return ""
        a = np.abs(z).astype(np.float32)
        k = max(1, int(fs / self.RATE))
        self.acc = np.concatenate((self.acc, a))
        n = len(self.acc) // k
        if n == 0:
            return ""
        e = self.acc[:n * k].reshape(n, k).mean(axis=1)
        self.acc = self.acc[n * k:]
        hi, lo = float(np.percentile(e, 99)), float(np.percentile(e, 10))
        self.lvl_hi = hi if self.lvl_hi is None else max(hi, self.lvl_hi * 0.93)
        self.lvl_lo = lo if self.lvl_lo is None else min(lo, self.lvl_lo * 0.7 + lo * 0.3)
        thr = (self.lvl_hi + self.lvl_lo) / 2
        es = _smooth(e, 4)
        out = []
        dot = self._dot()
        R = self.RATE
        on_t, off_t = thr * 1.15, thr * 0.85     # hysteres mot brus
        key = np.empty(len(es), bool)
        st = self.state
        for i, v in enumerate(es):
            if st and v < off_t:
                st = False
            elif not st and v > on_t:
                st = True
            key[i] = st
        for b in key:
            if b == self.state:
                self.run += 1
                if not b and dot:
                    if self.sym and self.run >= 2.2 * dot * R:
                        out.append(MORSE.get(self.sym, "*"))
                        self.sym = ""
                        self.space_done = False
                    if not self.space_done and self.run >= 5.5 * dot * R:
                        out.append(" ")
                        self.space_done = True
                continue
            if self.state:        # tonen slutade
                dur = self.run / R
                if dur < 0.008 or (dot and dur < 0.35 * dot):
                    self.state = bool(b)       # störtopp – räknas inte
                    self.run = 1
                    continue
                self.ons.append(dur)
                dot = self._dot()
                if dot:
                    self.sym += "." if dur < 2 * dot else "-"
            else:                 # pausen slutade – en ny ton börjar
                self.space_done = True if not self.sym and self.run < 5.5 * (dot or 1) * R else self.space_done
            self.state = bool(b)
            self.run = 1
        return "".join(out)

    def _dot(self):
        if len(self.ons) < 6:
            return None
        x = np.asarray(self.ons)
        x = x[x > 0.012]
        if len(x) < 4:
            return None
        p10, p90 = np.percentile(x, 10), np.percentile(x, 90)
        if p90 / p10 > 2:
            dot = float(np.median(x[x < math.sqrt(p10 * p90)]))
        else:
            dot = self._last_dot or float(np.median(x))
        self._last_dot = dot
        self.wpm = 1.2 / dot
        return dot

    def varden(self):
        return [("Morse", f"{self.wpm:.0f} ord/min" if self.wpm else "lär sig takten …")]


AVKODARE = [RttyAvkodare, CwAvkodare]   # fler läggs till längst ner (analysavkodare.py)


# ════════════════════════════════════════════════════════════════════
# Arbetstråd
# ════════════════════════════════════════════════════════════════════
class Analyzer:
    def __init__(self, sr, user_db=None):
        self.tap = ChannelTap(sr)
        self.db = signaldb.load(user_db)
        self.result = None
        self.result_id = 0
        self.dial = None          # GUI: inställd frekvens och modulering (för ljudfrekvens i USB/LSB)
        self.mode = None
        self.dec = [k() for k in AVKODARE]
        self.text_q = deque(maxlen=200)
        self.msg_q = deque(maxlen=200)     # hela meddelanden till fliken Avkodat
        self.tvinga = None                 # namn på avkodare som alltid ska köras (None = automatiskt)
        self._gen = None
        self._ptr = 0
        self._last_total = -1
        self._stop = threading.Event()
        self._thr = None
        self.paused = False

    def start(self):
        if self._thr is None:
            self._thr = threading.Thread(target=self._loop, name="Analys", daemon=True)
            self._thr.start()

    def stop(self):
        self._stop.set()

    def set_target(self, f, bw):
        self.tap.set_target(f, bw)
        self.result = None
        self.result_id += 1

    def step(self, now=None):
        """Ett analyssteg: mät, jämför med databasen och kör avkodarna på nya samples.
        Returnerar (feat, ny text) eller None om inget nytt finns."""
        snap = self.tap.snapshot(1.0 if (self.tap.fs or 0) <= 100e3 else 0.4)
        if snap is None:
            return None
        z, fs, tgt, bw, gen, total = snap
        if total == self._last_total or len(z) < fs * 0.25:
            return None
        self._last_total = total
        now = time.monotonic() if now is None else now
        if gen != self._gen:
            self._gen = gen
            self._ptr = total
            for dd in self.dec:
                dd.reset()
                dd._ok_feat = None
                dd._ptr = None
        feat = analyze(z, fs, bw)
        f_abs = tgt + feat.get("f_mid", 0.0)
        feat["f_abs"] = f_abs
        feat["target"] = tgt
        ac = None
        if self.dial is not None and self.mode in ("USB", "LSB") and feat.get("fam") != "BRUS":
            ac = (f_abs - self.dial) if self.mode == "USB" else (self.dial - f_abs)
            if not (150 < ac < 4000):
                ac = None
        feat["ljud_mitt"] = ac
        feat["kandidater"] = signaldb.match(feat, f_abs, self.db, ac)
        zn, self._ptr = self.tap.since(self._ptr)
        vals, new = [], ""
        locked = [dd for dd in self.dec if dd.har_synk() and getattr(dd, "_ok_feat", None) is not None
                  and now - dd._ok_t < 4]
        for dd in self.dec:
            if locked and dd not in locked and self.tvinga is None:
                continue
            use = None
            if self.tvinga is not None:
                if dd.namn != self.tvinga:
                    continue
                if feat.get("fam") != "BRUS":
                    use = feat
            elif dd.passar(feat):
                dd._ok_t, dd._ok_feat = now, feat
                use = feat
            elif getattr(dd, "_ok_feat", None) is not None and now - dd._ok_t < 4 and feat.get("fam") != "BRUS":
                use = dd._ok_feat            # en enstaka missad mätning ska inte bryta avkodningen
            if use is not None and zn is not None:
                # varje avkodare har en egen läspekare: när den startar får den hela ringbufferten
                # (ca 4 s), så att början av en sändning inte går förlorad medan klassningen kommer ikapp
                zd, dd._ptr = self.tap.since(dd._ptr if getattr(dd, "_ptr", None) is not None else -10 ** 12)
                if zd is None or not len(zd):
                    continue
                txt = dd.feed(zd, fs, use)
                if txt:
                    self.text_q.append((dd.namn, txt))
                    new += txt
                vals += dd.varden()
                feat["avkodare"] = dd.namn
                if dd.har_synk():
                    fk = dd.fakta()
                    if fk:
                        feat.update(fk)
                        feat["fakta_fran"] = dd.namn
                for m in getattr(dd, "msgs", [])[:]:
                    m["freq"] = f_abs
                    m.setdefault("level", feat.get("snr", 0.0))
                    m.setdefault("details", [("Avkodare", dd.namn), ("SNR", f"{feat.get('snr', 0):.1f} dB")])
                    self.msg_q.append(m)
                if getattr(dd, "msgs", None):
                    dd.msgs.clear()
                if getattr(dd, "bild", None) is not None:
                    feat["bild"] = dd
        feat["avk_varden"] = vals
        if feat.get("fakta_fran"):
            feat["kandidater"] = signaldb.match(feat, f_abs, self.db, ac)
        self.result = feat
        self.result_id += 1
        return feat, new

    def _loop(self):
        import logging
        log = logging.getLogger("radiospanaren")
        while not self._stop.wait(0.35):
            if self.paused:
                continue
            try:
                self.step()
            except Exception:
                log.exception("Fel i signalanalysen")
                time.sleep(1)


# ════════════════════════════════════════════════════════════════════
# Testsignaler (simulatorn och självtestet)
# ════════════════════════════════════════════════════════════════════
def _rrc(beta, sps, span=8):
    n = np.arange(-span * sps, span * sps + 1) / sps
    h = np.zeros_like(n)
    for i, t in enumerate(n):
        if abs(t) < 1e-9:
            h[i] = 1 - beta + 4 * beta / np.pi
        elif beta and abs(abs(4 * beta * t) - 1) < 1e-9:
            h[i] = beta / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * beta)) +
                                        (1 - 2 / np.pi) * np.cos(np.pi / (4 * beta)))
        else:
            h[i] = (np.sin(np.pi * t * (1 - beta)) + 4 * beta * t * np.cos(np.pi * t * (1 + beta))) / \
                   (np.pi * t * (1 - (4 * beta * t) ** 2))
    return h / np.sqrt((h ** 2).sum())


def make_psk(fs, baud, M, seconds, beta=0.35, seed=1):
    rng = np.random.default_rng(seed)
    sps = int(round(fs / baud))
    nsym = int(seconds * baud)
    sym = np.exp(2j * np.pi * rng.integers(0, M, nsym) / M)
    up = np.zeros(nsym * sps, np.complex128)
    up[::sps] = sym
    y = np.convolve(up, _rrc(beta, sps), mode="same")
    return (y / np.sqrt(np.mean(np.abs(y) ** 2))).astype(np.complex64)


def baudot_bits(text):
    rev_l = {c: i for i, c in enumerate(ITA2_LTRS)}
    rev_f = {c: i for i, c in enumerate(ITA2_FIGS)}
    bits = [1] * 20
    figs = False
    for ch in text.upper():
        if ch in rev_l and ch not in "\0\x0e\x0f":
            if figs and ch != " ":
                bits += [0] + [(0x1F >> k) & 1 for k in range(5)] + [1, 1]
                figs = False
            code = rev_l[ch]
        elif ch in rev_f:
            if not figs:
                bits += [0] + [(0x1B >> k) & 1 for k in range(5)] + [1, 1]
                figs = True
            code = rev_f[ch]
        else:
            continue
        bits += [0] + [(code >> k) & 1 for k in range(5)] + [1, 1]
        if ch == " ":
            figs = False          # mottagaren växlar till bokstäver vid mellanslag
    return bits + [1] * 10


def make_fsk(fs, baud, shift, bits, mark_high=True):
    sps = fs / baud
    n = int(len(bits) * sps)
    idx = np.minimum((np.arange(n) / sps).astype(np.int64), len(bits) - 1)
    b = np.asarray(bits)[idx]
    f = np.where(b == 1, shift / 2, -shift / 2) * (1 if mark_high else -1)
    ph = 2 * np.pi * np.cumsum(f) / fs
    return np.exp(1j * ph).astype(np.complex64)


def make_rtty(fs, baud, shift, text, mark_high=True):
    return make_fsk(fs, baud, shift, baudot_bits(text), mark_high)


def make_mfsk(fs, baud, tones, seconds, seed=2):
    rng = np.random.default_rng(seed)
    nsym = int(seconds * baud)
    sps = fs / baud
    s = rng.integers(0, len(tones), nsym)
    n = int(nsym * sps)
    idx = np.minimum((np.arange(n) / sps).astype(np.int64), nsym - 1)
    f = np.asarray(tones, float)[s[idx]]
    return np.exp(2j * np.pi * np.cumsum(f) / fs).astype(np.complex64)


def make_ofdm(fs, baud, tones, seconds, seed=3):
    """Parallelltonmodem: DQPSK på varje ton (som Link 11 / 16-tonsmodem)."""
    rng = np.random.default_rng(seed)
    nsym = int(seconds * baud)
    sps = fs / baud
    n = int(nsym * sps)
    t = np.arange(n) / fs
    idx = np.minimum((np.arange(n) / sps).astype(np.int64), nsym - 1)
    y = np.zeros(n, np.complex128)
    for f in tones:
        ph = np.cumsum(rng.integers(0, 4, nsym) * np.pi / 2 + np.pi / 4)
        y += np.exp(1j * (2 * np.pi * f * t + ph[idx]))
    return (y / np.sqrt(np.mean(np.abs(y) ** 2))).astype(np.complex64)


def make_morse(fs, text, wpm=18):
    rev = {v: k for k, v in MORSE.items()}
    dot = 1.2 / wpm
    key = []
    for w in text.upper().split():
        for ch in w:
            for s in rev.get(ch, ""):
                key += [1] * (1 if s == "." else 3) + [0]
            key += [0, 0]
        key += [0] * 4
    k = np.repeat(np.asarray(key, np.float32), int(dot * fs))
    k = _smooth(k, fs * 0.004)
    return k.astype(np.complex64)


def loop_sample(sig, pos):
    """Linjär interpolation i en slinga (snabbare än np.interp med period)."""
    L = len(sig)
    i = np.floor(pos).astype(np.int64)
    fr = (pos - i).astype(np.float32)
    i %= L
    return sig[i] * (1 - fr) + sig[(i + 1) % L] * fr


def test_signals(fs=48000):
    """Signaler för simulatorn: [(offset i Hz från 99,9 MHz, namn, komplex slinga vid fs, amplitud)].
    99,73 RTTY · 99,77 8PSK · 99,83 8-FSK (ALE-lik) · 99,86 16 toner (Link 11-lik) · 100,2 BPSK; Morse på 99,9."""
    ale = [750 + 250 * i - 1625 for i in range(8)]
    link11 = [935 + 110 * i - 1925 for i in range(16)]
    return [
        (-170e3, "RTTY 50/450", make_rtty(fs, 50, 450, "RYRYRY CQ CQ DE SIMULATORN TEST 1234 RYRYRY " * 6), 0.05),
        (-130e3, "8PSK 2400", make_psk(fs, 2400, 8, 8.0), 0.05),
        (-70e3, "8-FSK 125", make_mfsk(fs, 125, ale, 8.0), 0.05),
        (-40e3, "16 toner 75 Bd", make_ofdm(fs, 75, link11, 8.0), 0.05),
        (300e3, "BPSK 1200", make_psk(fs, 1200, 2, 8.0, seed=5), 0.05),
    ]


# ── registrera de större avkodarna (egen modul) ──
try:
    import analysavkodare as _av        # om modulen laddas först registrerar den sig själv i slutet
    for _k in getattr(_av, "AVKODARE_NYA", []):
        if _k not in AVKODARE:
            AVKODARE.append(_k)
except ImportError:                     # pragma: no cover
    pass
