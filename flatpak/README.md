# Signalspanaren som Flatpak

Flatpak-paketet kör på i stort sett alla Linux-distributioner, oavsett systemets
GTK-version, eftersom det tar med egna bibliotek (GTK 4, libadwaita, libshumate,
librtlsdr, numpy, scipy). Bra för t.ex. äldre Ubuntu/Debian/Mint där
systempaketen är för gamla för det vanliga installationsskriptet.

> Programmet hette tidigare Radiospanaren (`se.eh.Radiospanaren`). Det nya ID:t `se.eh.Signalspanaren` är ett eget
> flatpak-program. Ta bort det gamla med `flatpak uninstall --user se.eh.Radiospanaren`. Inställningarna i
> `~/.var/app/se.eh.Radiospanaren` följer inte med automatiskt, men kan kopieras till `~/.var/app/se.eh.Signalspanaren`.

## Bygga

```sh
# Verktyg och körtid (en gång)
flatpak install flathub org.flatpak.Builder org.gnome.Platform//51 org.gnome.Sdk//51

# Bygg och installera för den inloggade användaren
cd signalspanaren
flatpak run org.flatpak.Builder --user --install --force-clean \
    build-dir flatpak/se.eh.Signalspanaren.yaml

# Kör
flatpak run se.eh.Signalspanaren
```

Flygradar (ADS-B): `flatpak run --command=flygradar se.eh.Signalspanaren`
(eller knappen i programmet). Simulator utan dongel:
`flatpak run se.eh.Signalspanaren --simulator --start`.

## Ett delbart paket (en fil)

```sh
flatpak run org.flatpak.Builder --user --force-clean --repo=repo \
    build-dir flatpak/se.eh.Signalspanaren.yaml
flatpak build-bundle repo signalspanaren.flatpak se.eh.Signalspanaren
```

Filen `signalspanaren.flatpak` installeras på en annan dator med:
`flatpak install --user signalspanaren.flatpak`.

## Behörigheter

Paketet begär rå USB-åtkomst (`--device=all`) för dongeln, nätverk (kartrutor,
rtl_tcp, webbgränssnittet), ljud (PulseAudio/PipeWire) och skrivåtkomst till
musikmappen för inspelningar.

## Att tänka på

- **SDR-dongeln:** udev-reglerna som ger åtkomst till dongeln ligger på värdsystemet,
  inte i Flatpak. De installeras av paketet `rtl-sdr` (eller manuellt). Utan dem
  når bara root dongeln.
- **Körtid:** manifestet använder GNOME 51 (Python 3.14). Höj `runtime-version` när du byter till
  en nyare GNOME-körtid (och uppdatera Python-hjulens `cp3XX` samt `PYTHONPATH` vid behov).
- **Wayland utan GPU:** i en GPU-lös miljö (t.ex. vissa virtuella maskiner) kan GTK:s
  Wayland-läge krascha i dmabuf-förhandlingen. Starta då med `GDK_BACKEND=x11`
  (XWayland). På en vanlig skrivbordsdator med grafikdrivrutin behövs det inte.
