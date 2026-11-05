# Ändringslogg

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
