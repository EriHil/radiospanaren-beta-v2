# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 erihil
"""
Aktivitetskarta för Radiospanaren.

BandMonitor sveper ett frekvensband om och om igen med SDR:n och bygger en
värmekarta: för varje svep mäts signalnivån över brusgolvet i ett rutnät av
kanaler över bandet. Raderna (ett svep = en rad) staplas över tid, och
andelen svep då varje kanal var aktiv sparas som "upptagenhet".

Modulen använder bara numpy. Den drivs av det grafiska gränssnittet, som
anropar tick() med motorns get_avg()/set_center().
"""
import time
import math
import threading
from collections import deque

import numpy as np

import radiospanaren as R      # SR, FFT_N, BIN_HZ, bandplan m.m.

# Färdiga band att välja mellan (från, till i MHz, namn, läge, kanalsteg kHz).
# Inga myndighets-/blåljusband – det här är för allmän spektrumöversikt.
# WFM-rundradio (96–108 MHz) är medvetet uteslutet – lågt intresse och starka
# bärvågor som annars dominerar värmekartan. Se EXCLUDE nedan.
PRESETS = [
    ("Flygband (AM)", 118.0, 137.0, "AM", 25.0),
    ("2 m amatör", 144.0, 146.0, "NFM", 12.5),
    ("Marin VHF", 156.0, 162.05, "NFM", 25.0),
    ("PMR446", 446.0, 446.2, "NFM", 12.5),
    ("70 cm amatör", 430.0, 440.0, "NFM", 12.5),
    ("ISM 433 MHz", 433.05, 434.79, "NFM", 25.0),
    ("ISM 868 MHz", 863.0, 870.0, "NFM", 25.0),
    ("Flygdata (ACARS/VDL)", 129.0, 137.0, "AM", 25.0),
    ("Amatör HF 40 m", 7.0, 7.3, "USB", 1.0),
    ("Amatör HF 20 m", 14.0, 14.35, "USB", 1.0),
]


EXCLUDE = [(96.0e6, 108.0e6)]      # WFM-rundradio – räknas aldrig med


class BandMonitor:
    def __init__(self, lo_hz, hi_hz, cell_hz=3000.0, settle=4, thresh_db=6.0, max_rows=720,
                 exclude=EXCLUDE):
        self.lo = float(lo_hz)
        self.hi = float(hi_hz)
        span = self.hi - self.lo
        if span <= 0:
            raise ValueError("Stoppfrekvensen måste vara högre än startfrekvensen")
        self.ncols = int(min(1400, max(40, round(span / max(cell_hz, 500)))))
        self.edges = np.linspace(self.lo, self.hi, self.ncols + 1)
        self.cell_hz = span / self.ncols
        self.exclude = list(exclude or [])
        cf = 0.5 * (self.edges[:-1] + self.edges[1:])
        self.excluded = np.zeros(self.ncols, bool)
        for elo, ehi in self.exclude:
            self.excluded |= (cf >= elo) & (cf <= ehi)

        self.usable = R.SR * 0.8
        if span <= self.usable:
            self.spans = [(self.lo + self.hi) / 2]
        else:
            self.spans = []
            c = self.lo + self.usable / 2
            while c - self.usable / 2 < self.hi:
                self.spans.append(c)
                c += self.usable
        self.settle = int(settle)
        self.thresh = float(thresh_db)

        self.lock = threading.Lock()
        self.cur = np.full(self.ncols, -np.inf, np.float32)   # pågående svep, dB över brus
        self.rows = deque(maxlen=max_rows)                    # (tid, np.array[ncols])
        self.idx = -1
        self.sweeps = 0
        self.started = time.time()
        self.last_sweep_t = None
        self.sweep_dt = 0.0

    # ── drivs av gränssnittet ──
    def tick(self, engine):
        """Avancerar svepet ett steg. Returnerar True när ett helt svep just blev klart."""
        if self.idx < 0:
            self._goto(0, engine)
            return False
        avg, c, cnt = engine.get_avg()
        target = self.spans[self.idx]
        if c is None or abs(c - target) > 1 or cnt < self.settle:
            return False
        self._fold(avg, c)
        nxt = self.idx + 1
        done = nxt >= len(self.spans)
        if done:
            self._commit()
            nxt = 0
        self._goto(nxt, engine)
        return done

    def _goto(self, idx, engine):
        self.idx = idx
        engine.set_center(self.spans[idx])

    def _fold(self, avg_lin, c):
        db = 10.0 * np.log10(avg_lin + 1e-20)
        floor = float(np.median(db))
        above = (db - floor).astype(np.float32)
        freqs = c + (np.arange(R.FFT_N) - R.FFT_N / 2) * R.BIN_HZ
        half = self.usable / 2
        m = (np.abs(freqs - c) <= half) & (freqs >= self.lo) & (freqs < self.hi)
        if not m.any():
            return
        col = np.clip(((freqs[m] - self.lo) / (self.hi - self.lo) * self.ncols).astype(np.int64),
                      0, self.ncols - 1)
        with self.lock:
            np.maximum.at(self.cur, col, above[m])
            self.cur[self.excluded] = -np.inf

    def _commit(self):
        now = time.time()
        with self.lock:
            row = np.where(np.isfinite(self.cur), self.cur, 0.0).astype(np.float32)
            np.clip(row, 0.0, None, out=row)
            row[self.excluded] = 0.0
            self.rows.append((now, row))
            self.cur[:] = -np.inf
            self.sweeps += 1
            if self.last_sweep_t is not None:
                dt = now - self.last_sweep_t
                self.sweep_dt = dt if self.sweep_dt == 0 else self.sweep_dt * 0.7 + dt * 0.3
            self.last_sweep_t = now

    # ── läsning (gränssnittet) ──
    def image(self, max_rows=None):
        """Returnerar (2D-array [rader, ncols] med dB över brus, tider) – nyaste raden först."""
        with self.lock:
            rows = list(self.rows)
        if not rows:
            return None, []
        if max_rows:
            rows = rows[-max_rows:]
        times = [t for t, _ in rows]
        arr = np.array([r for _, r in rows], np.float32)[::-1]   # nyaste överst
        return arr, times[::-1]

    def occupancy(self):
        """Andel av sveparna (0–1) då kanalen var över tröskeln – räknas live, så
        tröskeln kan ändras i efterhand."""
        with self.lock:
            if not self.rows:
                return np.zeros(self.ncols)
            arr = np.array([r for _, r in self.rows], np.float32)
        occ = (arr > self.thresh).mean(axis=0)
        occ[self.excluded] = 0.0
        return occ

    def peak_hold(self):
        with self.lock:
            if not self.rows:
                return np.zeros(self.ncols)
            return np.array([r for _, r in self.rows], np.float32).max(axis=0)

    def col_freq(self, col):
        return self.lo + (col + 0.5) * self.cell_hz

    def freq_col(self, hz):
        return int(np.clip((hz - self.lo) / (self.hi - self.lo) * self.ncols, 0, self.ncols - 1))

    def busiest(self, n=10):
        """De n mest aktiva kanalerna: lista med (frekvens, upptagenhet 0–1, topp dB)."""
        occ = self.occupancy()
        peak = self.peak_hold()
        order = np.argsort(occ)[::-1]
        out = []
        for col in order:
            if occ[col] <= 0:
                break
            out.append((self.col_freq(int(col)), float(occ[col]), float(peak[col])))
            if len(out) >= n:
                break
        return out
