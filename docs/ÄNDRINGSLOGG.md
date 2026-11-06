# Ändringslogg

## 2.8.0 (2026-11-06)

- **Nytt namn: Signalspanaren** (tidigare Radiospanaren). Kommandot heter `signalspanaren`, och `radiospanaren` fungerar fortfarande. Inställningar, bokmärken, loggar, cache och inspelningsmappen flyttas automatiskt vid första start. Installationsskriptet tar bort den gamla installationen. Flatpak-ID:t är nu `se.eh.Signalspanaren`.
- **Skärmanpassning**: programmet känner av skärmens upplösning. Fönstren får en storlek som ryms på skärmen, och på låga skärmar används ett kompakt läge (Visning → Skärmanpassning).
- **Justerbar nedre panel**: en synlig list mellan vattenfallet och flikarna. Höjden sparas.
- **Signalanalys**: inget klipps längre. Signaltypen står kvar medan klartexten rullar, och mätvärdena får den höjd de behöver. Signalfamiljen stabiliseras: en ny typ visas när den har setts 3 av de 5 senaste gångerna, och "ingen signal" efter 2.
- Förkortade texter i sidopanelen visas nu hela. dB-skalan i spektrumet trängs inte ihop på låga skärmar, och fartygskartans färgförklaring radbryts.
- Rättat: `frontpanel.py` saknades i installationsskriptet och i flatpak-paketet.

## 2.1.1 (2026-11-05)

- **Flatpak: GNOME 51-körtid** i stället för GNOME 48, som nått slutet på sin livstid. GTK 4.24, libadwaita 1.10, Python 3.14, libshumate 1.5.5, numpy 2.5.4 och scipy 1.18.1.
- Inga funktionsändringar; samma program som 2.1.0.

## 2.1.0 – "beta v2.1" (2026-11-05)

- **Flatpak-paketering** (`flatpak/`): kör på valfri distribution oavsett systemets GTK-version, med egna bibliotek (GTK4, libadwaita, libshumate, librtlsdr, numpy, scipy). Byggt och verifierat mot GNOME 48-körtiden.
- Verifierat att programmen kör på Debian 13 (trixie).

- **Frekvenskalibrering**: "Kalibrera frekvens (ppm) …" i Mottagare (eller `--kalibrera`) mäter dongelns frekvensfel mot en känd bärvåg (t.ex. tidssignalen RWM) och sätter ppm automatiskt.
- Rättat: loggraden "Mottagning startad" visade dongelns mittfrekvens i stället för den inställda frekvensen.

## 2.0.0 – "beta v2" (2026-11-05)

Samlar funktionerna från 1.2–1.4 under en version: AIS, ADS-B och spektrumanalys.
- **Flygradar (ADS-B, 1090 MHz)** – flygplan på OpenStreetMap-karta (1.2.0).
- **Fartygskarta (AIS)** – fartyg på karta med typfärg och spår (1.3.0).
- **Aktivitetskarta (spektrumanalys)** – värmekarta över ett band, med WFM 96–108 MHz uteslutet (1.4.0).
- Avkodare för ACARS, AIS och APRS, CRC-kontrollerade.
- Fungerar på Fedora 40+, Ubuntu 24.04+, Linux Mint 22+ och Debian 13+; kartan installeras automatiskt.

Detaljerna för varje del står under respektive delversion nedan.


## 1.4.0 (2026-11-05)

### Nytt: Aktivitetskarta
- Fönstret "Aktivitetskarta" (sidopanelen → Avkodare, eller `--aktivitetskarta`) sveper ett valt band och ritar en värmekarta: frekvens i sidled, tid nedåt, färg efter signalnivå över brusgolvet.
- Upptagenhetsremsa (andel svep per kanal som var aktiv) och lista över de mest aktiva kanalerna; klicka för att lyssna.
- Färdiga band eller eget intervall, valbar tröskel och färgskala. Ömsesidigt uteslutande med skannern.
- WFM-rundradion 96–108 MHz räknas aldrig med och ritas grå.
- Ny modul `aktivitet.py` (BandMonitor, bara numpy).

## 1.3.0 (2026-11-05)

### Nytt: Fartygskarta (AIS)
- Fönstret "Fartyg (AIS)" (sidopanelen → Avkodare, eller `--fartygskarta`) med OpenStreetMap-karta och fartygstabell.
- AIS-meddelanden slås ihop per fartyg: position, fart, kurs, bäring, status, namn, anrop, typ och destination.
- Symboler i kursriktningen med färg efter fartygstyp och spår. Förtöjda och ankrade fartyg visas som en ring, basstationer och fyrar som en prick.
- Varning för AIS-nödsändare (AIS-SART/MOB/EPIRB).
- Ny gemensam kartmodul, `kartvy.py`, med reserv-radarvy och knappen "Installera kartan".
- Simulatorn har fyra fartyg som rör sig.

### Rättat
- "Visa alla" och anpassningen av kartan behåller nu zoomnivån (`go_to_full`).

## 1.2.0 (2026-11-05)

### Nytt: Flygradar (ADS-B)
- Eget program, `adsb_karta.py` / `flygradar`, med OpenStreetMap-karta (libshumate) och flygplanstabell.
- ADS-B/Mode S-avkodare (`adsb.py`): DF17/18, DF11 och DF4/5/20/21, CRC-24 med rättning av enstaka bitfel, och CPR-positioner.
- Startas med knappen "✈ Öppna Flygradar" i Radiospanaren, som först släpper dongeln. Finns också i programmenyn.
- Varning för squawk 7500, 7600 och 7700. Visar höjd i fot eller meter.
- Saknas kartan visas en enkel radarvy och knappen "Installera kartan" (pkexec med bekräftelse).
- Installationsskriptet installerar kartan (libshumate / gir1.2-shumate-1.0) automatiskt.

### Rättat
- Stängning vid SIGTERM fungerar på både ny GLib (GLibUnix) och äldre (Ubuntu 24.04 / Mint 22).
- Kompatibel med libshumate 1.2 (Ubuntu 24.04 / Mint 22).
- RTL-SDR- och rtl_tcp-källorna har valbar samplingstakt.

## 1.1.0 – "beta v1" (2026-11-05)

Första publicerade versionen.

### Mottagare
- Spektrum och vattenfall med bandplan, bokmärken och markering av aktiva signaler.
- Zoom-FFT med upplösning ner till cirka 12 Hz när man zoomar in.
- Demodulering i WFM, NFM, AM, USB, LSB och CW.
- Källor: RTL-SDR (librtlsdr via ctypes), rtl_tcp och simulator.
- Direktsampling för HF (I- och Q-gren). Testat med FT8 på 20 m.
- Ljud via PipeWire/PulseAudio (pacat/pw-cat). WAV-, auto- och IQ-inspelning.

### Skanner
- Bokmärkes- och intervallskanning, där hela fönster analyseras på en gång.
- Topp-detektering: grannkanaler till starka sändare räknas inte.
- Automatisk överhoppning av omodulerade bärvågor (dongelstörningar), med en lista över hittade störningar.
- Spärrlista, väntetid, max lyssningstid och knappen "Hoppa över".
- Squelchen nollställs vid frekvensbyte, så att den inte "ärver" läget från förra frekvensen.

### Signallogg och avkodare
- Automatisk signaldetektering med filter i stil med Wireshark och export till CSV.
- Identifiering av kända signaltyper och dongelns kristallövertoner.
- ACARS-, AIS- och APRS-avkodare med CRC-kontroll. Fliken "Avkodat" med karta.

### Övrigt
- Webbgränssnitt med vattenfall, inställning och ljudström.
- Loggfil, felrapport (`--rapport`), självtest (`--sjalvtest`) och skärmbild (`--skarmbild`).
- Installationsskript för Fedora, Ubuntu, Linux Mint och Debian.
- Kompatibelt med libadwaita 1.5 (Ubuntu 24.04 / Linux Mint 22).
