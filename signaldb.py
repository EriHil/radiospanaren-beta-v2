# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Signaldatabas för Signalspanarens signalanalys.

Varje post beskriver de *publicerade* tekniska kännetecknen för en signaltyp
(modulation, symboltakt, shift, antal toner, bandbredd, typiska frekvenser).
Analysen mäter samma storheter och jämför – det är igenkänning av vågformen,
inte avkodning av innehållet. Många militära vågformer är krypterade på
länknivå och går inte att läsa; de kan bara identifieras.

Lägg till egna poster i ~/.config/signalspanaren/signaler.json (samma fält,
en lista med objekt) – de läses in automatiskt.

Fält:
  namn     visningsnamn
  kat      kategori: militär, myndighet, flyg, sjöfart, amatör, kommersiell, rundradio, navigation
  mod      lista med modulationsfamiljer som analysen kan rapportera:
           BÄRVÅG, OOK, AM, FM, SSB, FSK2, FSK4, MFSK, MSK, BPSK, QPSK, PSK8, LIN, OFDM
  baud     symboltakt (Bd) eller None
  shift    tonavstånd/shift (Hz) mellan närliggande toner, eller None
  toner    antal toner (MFSK/OFDM) eller None
  bw       ungefärlig bandbredd (Hz)
  ljud     mittfrekvens i ljudbandet vid USB-mottagning (HF-modem), eller None
  f        lista med frekvensområden i MHz [(lo, hi), ...]; tom = var som helst
  info     kort beskrivning
"""
import json
import os

HF = [(1.5, 30.0)]
VHF_UHF = [(30.0, 1000.0)]

SIGNALER = [
    # ── Militära HF-modem (NATO/USA) ──────────────────────────────────
    # ram_ms: verifierad mot riktiga sändningar (6429,3 och 6778,5 kHz, 2026-11-06): 256 symboler à 2400 Bd
    dict(namn="STANAG 4285", kat="militär", mod=["PSK8", "LIN", "QPSK", "BPSK"], baud=2400, ram_ms=106.667,
         shift=None, toner=None, bw=3000,
         ljud=1800, f=HF, info="NATO HF-modem, 8PSK 2400 Bd på 1800 Hz underbärvåg. Marinens sändningar och "
                               "dataöverföring. Ofta krypterat."),
    dict(namn="MIL-STD-188-110A/B (seriell)", kat="militär", mod=["PSK8", "LIN"], baud=2400, shift=None,
         toner=None, bw=3000, ljud=1800, f=HF,
         info="USA/NATO HF-modem, 8PSK 2400 Bd på 1800 Hz (75–4800 bit/s). Samma signatur som STANAG 4285, "
              "skiljs på ramstrukturen."),
    dict(namn="STANAG 4539 / 188-110B App. C", kat="militär", mod=["LIN", "PSK8"], baud=2400, shift=None,
         toner=None, bw=3000, ljud=1800, f=HF,
         info="Höghastighets-HF-modem upp till 64QAM (3200–9600 bit/s). Används bl.a. av Link 22."),
    dict(namn="STANAG 4529", kat="militär", mod=["PSK8", "LIN"], baud=1200, shift=None, toner=None, bw=1240,
         ljud=1800, f=HF, info="Smalbandsversion av STANAG 4285: 8PSK 1200 Bd, ca 1,24 kHz bred."),
    dict(namn="Link 11 SLEW", kat="militär", mod=["PSK8", "LIN"], baud=2400, shift=None, toner=None, bw=3000,
         ljud=1800, f=HF, info="Taktisk datalänk (enkelton), 8PSK 2400 Bd. Krypterad."),
    dict(namn="Link 11 CLEW (TADIL-A)", kat="militär", mod=["OFDM"], baud=75, shift=110, toner=16, bw=2400,
         ljud=1925, f=HF,
         info="Taktisk datalänk: 16 parallella DQPSK-toner, 935–2915 Hz med 110 Hz avstånd, plus en "
              "dopplerton på 605 Hz. 75 (eller 45,45) Bd. Krypterad."),
    dict(namn="MIL-STD-188-110 App. A (16 toner)", kat="militär", mod=["OFDM"], baud=75, shift=110, toner=16,
         bw=2400, ljud=1925, f=HF, info="16-tons parallellmodem, samma tonraster som Link 11."),
    dict(namn="MIL-STD-188-110 App. B / ANDVT (39 toner)", kat="militär", mod=["OFDM"], baud=44.44, shift=56.25,
         toner=39, bw=2300, ljud=1650, f=HF,
         info="39-tons QDPSK-modem, 44,44 Bd med 56,25 Hz tonavstånd. Används för ANDVT (krypterat digitalt tal)."),
    dict(namn="MIL-STD-188-141 ALE (2G)", kat="militär", mod=["MFSK"], baud=125, shift=250, toner=8, bw=2000,
         ljud=1625, f=HF,
         info="Automatisk länketablering: 8-FSK 125 Bd, toner 750–2500 Hz med 250 Hz avstånd. Anropsadresser "
              "sänds i klartext och kan avkodas (t.ex. med PC-ALE)."),
    dict(namn="MIL-STD-188-141B ALE 3G", kat="militär", mod=["PSK8", "LIN"], baud=2400, shift=None, toner=None,
         bw=3000, ljud=1800, f=HF, info="Tredje generationens ALE: korta 8PSK-skurar 2400 Bd."),
    dict(namn="Marin FSK-sändning (STANAG 4481/5065)", kat="militär", mod=["FSK2"], baud=75, shift=850, toner=None,
         bw=1000, ljud=None, f=HF + [(0.01, 0.15)],
         info="Marinens sändningar till fartyg: FSK 75 Bd, 850 Hz shift (även 50 Bd). Krypterat, ofta "
              "kontinuerlig 'idle'-trafik."),
    dict(namn="Rysk marin FSK 50/1000 (CIS 50-50)", kat="militär", mod=["FSK2"], baud=50, shift=1000, toner=None,
         bw=1200, ljud=None, f=HF, info="FSK 50 Bd, 1000 Hz shift. Krypterad telegrafi."),
    dict(namn="STANAG 4197 (ANDVT HF-tal)", kat="militär", mod=["OFDM"], baud=44.44, shift=56.25, toner=39,
         bw=2300, ljud=1650, f=HF, info="Krypterat digitalt tal (LPC-10 2400 bit/s) med 39-tonsmodem."),

    # ── Militärt/myndighet VHF/UHF ────────────────────────────────────
    dict(namn="Militär flyg-UHF (AM-tal)", kat="militär", mod=["AM"], baud=None, shift=None, toner=None,
         bw=8000, ljud=None, f=[(225.0, 400.0)], info="Militär flygradio, AM-tal 225–400 MHz. Ofta klartext."),
    dict(namn="TETRA / Rakel", kat="myndighet", mod=["LIN", "QPSK", "PSK8"], baud=18000, shift=None, toner=None,
         bw=25000, ljud=None, f=[(380.0, 400.0), (410.0, 430.0)],
         info="π/4-DQPSK 18 ksym/s, 25 kHz. Rakel (blåljus) är krypterat; kan bara identifieras."),

    # ── Flyg ─────────────────────────────────────────────────────────
    dict(namn="HFDL (ARINC 635)", kat="flyg", mod=["PSK8", "LIN", "QPSK", "BPSK"], baud=1800, shift=None,
         toner=None, bw=2400, ljud=1440, f=HF, info="HF-datalänk för flyg, PSK 1800 Bd på 1440 Hz. Klartext."),
    dict(namn="ACARS (VHF)", kat="flyg", mod=["AM"], baud=None, shift=None, toner=None, bw=8000, ljud=None,
         f=[(129.0, 137.0)], info="AM med 2400 bit/s MSK-ljud (1200/2400 Hz). Klartext – avkodas på fliken Avkodat."),
    dict(namn="Flygradio (AM-tal)", kat="flyg", mod=["AM"], baud=None, shift=None, toner=None, bw=8000, ljud=None,
         f=[(118.0, 137.0)], info="Civil flygradio, AM-tal. Klartext."),

    # ── Sjöfart / navigation ─────────────────────────────────────────
    dict(namn="NAVTEX / SITOR-B", kat="sjöfart", mod=["FSK2"], baud=100, shift=170, toner=None, bw=300,
         ljud=None, f=[(0.48, 0.53), (4.2, 4.21)], info="Sjöfartsvarningar, FSK 100 Bd 170 Hz shift. Klartext."),
    dict(namn="AIS", kat="sjöfart", mod=["MSK", "FSK2", "FM"], baud=9600, shift=4800, toner=None, bw=12000,
         ljud=None, f=[(161.9, 162.1)], info="GMSK 9600 bit/s. Klartext – avkodas på fliken Avkodat."),
    dict(namn="DGPS-referens", kat="navigation", mod=["MSK", "FSK2"], baud=200, shift=100, toner=None, bw=300,
         ljud=None, f=[(0.283, 0.325)], info="Differentiell GPS: MSK 100/200 Bd på långvåg."),
    dict(namn="Marin VHF (FM-tal)", kat="sjöfart", mod=["FM"], baud=None, shift=None, toner=None, bw=12500,
         ljud=None, f=[(156.0, 162.1)], info="Sjöfartsradio, FM-tal. Klartext."),

    # ── Väder / kommersiellt HF ──────────────────────────────────────
    dict(namn="RTTY 50 Bd / 450 Hz (väder)", kat="kommersiell", mod=["FSK2"], baud=50, shift=450, toner=None,
         bw=600, ljud=None, f=HF, info="Baudot-RTTY, t.ex. tyska vädertjänsten DWD. Klartext."),
    dict(namn="RTTY 45,45 Bd / 170 Hz", kat="amatör", mod=["FSK2"], baud=45.45, shift=170, toner=None, bw=300,
         ljud=None, f=HF, info="Amatörradio-RTTY (Baudot). Klartext."),
    dict(namn="Väderfax (HF-fax)", kat="kommersiell", mod=["FM", "FSK2"], baud=None, shift=800, toner=None,
         bw=1100, ljud=1900, f=HF, info="Analog fax: 1500–2300 Hz, 120 rader/min. Bilder av väderkartor."),
    dict(namn="PACTOR-I", kat="kommersiell", mod=["FSK2"], baud=200, shift=200, toner=None, bw=500, ljud=None,
         f=HF, info="ARQ-protokoll, FSK 100/200 Bd 200 Hz shift."),
    dict(namn="DRM (digital rundradio)", kat="rundradio", mod=["OFDM"], baud=None, shift=None, toner=200,
         bw=10000, ljud=None, f=HF + [(0.15, 1.7)], info="OFDM med ~200 bärvågor, 9–10 kHz."),

    # ── Personsökning / digital landmobil ────────────────────────────
    dict(namn="POCSAG", kat="kommersiell", mod=["FSK2", "FM"], baud=1200, shift=9000, toner=None, bw=12500,
         ljud=None, f=VHF_UHF, info="Personsökare, FSK 512/1200/2400 Bd ±4,5 kHz. Ofta klartext."),
    dict(namn="DMR", kat="kommersiell", mod=["FSK4", "MFSK", "FM"], baud=4800, shift=1296, toner=4, bw=12500, ljud=None,
         f=VHF_UHF, info="4FSK 4800 sym/s (±648/±1944 Hz), TDMA. Kan vara krypterat."),
    dict(namn="P25 fas 1 (C4FM)", kat="myndighet", mod=["FSK4", "MFSK", "FM"], baud=4800, shift=1200, toner=4, bw=12500,
         ljud=None, f=VHF_UHF, info="4FSK 4800 sym/s (±600/±1800 Hz). Kan vara krypterat."),
    dict(namn="NXDN / dPMR (6,25 kHz)", kat="kommersiell", mod=["FSK4", "MFSK", "FM"], baud=2400, shift=700, toner=4,
         bw=6250, ljud=None, f=VHF_UHF, info="4FSK 2400 sym/s. Kan vara krypterat."),
    dict(namn="APRS / AFSK 1200", kat="amatör", mod=["FM"], baud=None, shift=None, toner=None, bw=12500,
         ljud=None, f=[(144.79, 144.81)], info="Paketradio, AFSK 1200 i FM. Avkodas på fliken Avkodat."),
    dict(namn="Analog FM-tal (landmobil)", kat="kommersiell", mod=["FM"], baud=None, shift=None, toner=None,
         bw=12500, ljud=None, f=VHF_UHF, info="Analog FM-radio (PMR, jakt, amatör m.m.). Klartext."),

    # ── Övrigt ───────────────────────────────────────────────────────
    dict(namn="FT8", kat="amatör", mod=["MFSK", "FSK4", "FSK2"], baud=6.25, shift=6.25, toner=8, bw=50,
         ljud=None, f=[(1.8, 1.85), (3.57, 3.58), (5.35, 5.36), (7.074, 7.077), (10.136, 10.139),
                       (14.074, 14.077), (18.1, 18.103), (21.074, 21.077), (24.915, 24.918), (28.074, 28.077),
                       (50.313, 50.316), (144.174, 144.177)],
         info="Amatörradio, 8-FSK 6,25 Bd med 6,25 Hz tonavstånd (ca 50 Hz brett), 15 s sändningsperioder. "
              "Många signaler sida vid sida på standardfrekvenserna (USB 7,074 m.fl.)."),
    dict(namn="Morse (CW)", kat="amatör", mod=["OOK"], baud=None, shift=None, toner=None, bw=150, ljud=None,
         f=[], info="Telegrafi med på/av-nycklad bärvåg. Klartext – avkodas här."),
    dict(namn="Omodulerad bärvåg", kat="kommersiell", mod=["BÄRVÅG"], baud=None, shift=None, toner=None, bw=50,
         ljud=None, f=[], info="Ren bärvåg – t.ex. fyr, störning, spur från dongeln eller en sändare i viloläge."),
    dict(namn="SSB-tal", kat="amatör", mod=["SSB"], baud=None, shift=None, toner=None, bw=2700, ljud=None,
         f=HF + [(144.0, 146.0), (430.0, 440.0)], info="Enkelt sidband, tal. Klartext."),
]


def load(user_file=None):
    """Inbyggda poster + egna från signaler.json."""
    out = list(SIGNALER)
    if user_file and os.path.exists(user_file):
        try:
            with open(user_file, encoding="utf-8") as fh:
                extra = json.load(fh)
            for e in extra:
                e.setdefault("kat", "egen")
                e.setdefault("f", [])
                for k in ("baud", "shift", "toner", "bw", "ljud"):
                    e.setdefault(k, None)
                e["f"] = [tuple(r) for r in e["f"]]
                if isinstance(e.get("mod"), str):
                    e["mod"] = [e["mod"]]
                out.append(e)
        except Exception as ex:      # en trasig användarfil får inte stoppa programmet
            import logging
            logging.getLogger("signalspanaren").error("Kunde inte läsa %s: %s", user_file, ex)
    return out


def _rel(a, b):
    return abs(a - b) / max(abs(b), 1e-9)


def match(feat, freq_hz, db=None, audio_center=None, top=4):
    """Jämför uppmätta egenskaper med databasen. Returnerar [(poäng 0–100, post, motiv)]."""
    db = db if db is not None else SIGNALER
    fam = feat.get("fam")
    if not fam or fam == "BRUS":
        return []
    fmhz = freq_hz / 1e6
    res = []
    for e in db:
        if fam not in e["mod"]:
            continue
        s, smax, why = 3.0, 3.0, []
        if e["mod"][0] == fam:
            s += 1
        smax += 1
        b = feat.get("baud")
        if e.get("baud") and b:
            smax += 4
            if True:
                r = _rel(b, e["baud"])
                if r < 0.03:
                    s += 4
                    why.append(f"baud {b:.1f}≈{e['baud']:g}")
                elif r < 0.08:
                    s += 2
                    why.append(f"baud {b:.1f}~{e['baud']:g}")
                else:
                    s -= 3
        elif e.get("baud") and not b and fam in ("FM", "AM"):
            s -= 1.5              # digital signaltyp men ingen symboltakt hittad
        rm = feat.get("ram_ms")
        ram_bonus = 0.0
        if e.get("ram_ms") and rm:
            # ramperioden är ett fingeravtryck: väger tyngre än allt annat
            smax += 6
            r = _rel(rm, e["ram_ms"])
            if r < 0.015:
                s += 6
                ram_bonus = 15.0       # utöver andelen: annars når poster med färre kriterier högre
                why.append(f"ram {rm:.1f} ms")
            elif r > 0.03:
                s -= 3
        elif rm and e.get("baud") and fam in e["mod"]:
            ram_bonus = -10.0          # ram uppmätt men posten saknar känd ramperiod: svagare stöd
        sh = feat.get("shift")
        if e.get("shift") and sh and fam in ("FSK2", "FSK4", "MFSK", "MSK", "OFDM"):
            smax += 3
            if True:
                r = _rel(sh, e["shift"])
                if r < 0.08:
                    s += 3
                    why.append(f"shift {sh:.0f}≈{e['shift']:g} Hz")
                elif r < 0.2:
                    s += 1
                else:
                    s -= 2
        nt = feat.get("toner")
        if e.get("toner") and nt and fam in ("OFDM", "MFSK"):
            smax += 2
            if True:
                if abs(nt - e["toner"]) <= max(1, 0.15 * e["toner"]):
                    s += 2
                    why.append(f"{nt} toner")
                else:
                    s -= 1
        bw = feat.get("occ_bw")
        if e.get("bw") and bw:
            smax += 1
            ratio = bw / e["bw"]
            if 0.5 <= ratio <= 1.6:
                s += 1
            elif ratio > 3 or ratio < 0.25:
                s -= 2
        if e.get("ljud") and audio_center is not None:
            smax += 2
            if abs(audio_center - e["ljud"]) < 150:
                s += 2
                why.append(f"ljud {audio_center:.0f} Hz")
            elif abs(audio_center - e["ljud"]) > 600:
                s -= 1
        if e.get("f"):
            smax += 2
            if any(lo <= fmhz <= hi for lo, hi in e["f"]):
                s += 2
            else:
                s -= 2
        score = max(0.0, min(100.0, 100.0 * s / smax + ram_bonus))
        if score >= 35:
            res.append((score, e, ", ".join(why)))
    res.sort(key=lambda r: -r[0])
    return res[:top]
