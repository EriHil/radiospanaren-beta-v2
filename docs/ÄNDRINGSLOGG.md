# Ändringslogg

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
